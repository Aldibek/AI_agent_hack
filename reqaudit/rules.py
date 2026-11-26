"""Детерминированные проверки (без LLM).

Правила ловят то, что модели пропускают или находят нестабильно: заглушки шаблона,
размытые слова, семантику HTTP, коды ответов, единицы, часовой пояс, границы диапазонов,
арифметику в критериях приёмки, неразрешённые ссылки. Правила работают и без ключа LLM.
Они намеренно консервативны: лучше промолчать, чем выдать ложное замечание.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .criteria import criterion_for
from .models import Document, Finding, Question, Quote, Requirement
from .segment import section_kind

HTTP_REASON = {
    200: "ok", 201: "created", 202: "accepted", 204: "no content", 301: "moved permanently", 302: "found",
    304: "not modified", 400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found",
    405: "method not allowed", 409: "conflict", 412: "precondition failed", 415: "unsupported media type",
    422: "unprocessable entity", 429: "too many requests", 500: "internal server error", 502: "bad gateway",
    503: "service unavailable", 504: "gateway timeout",
}

WEAK_WORDS: list[tuple[str, str]] = [
    (r"быстр\w*", "«быстро» без числа"), (r"оперативн\w*", "«оперативно» без срока"),
    (r"своевременн\w*", "«своевременно» без срока"), (r"мгновенн\w*", "«мгновенно» без числа"),
    (r"в реальном времени", "«в реальном времени» без допустимой задержки"),
    (r"в момент\b", "«в момент» без допустимой задержки"), (r"\bсразу\b", "«сразу» без допустимой задержки"),
    (r"удобн\w*", "субъективное «удобно»"), (r"интуитивн\w*", "субъективное «интуитивно»"),
    (r"дружелюбн\w*", "субъективное «дружелюбный»"), (r"гибк\w*", "размытое «гибкий»"),
    (r"над[её]жн\w*", "«надёжный» без метрики"), (r"эффективн\w*", "«эффективно» без метрики"),
    (r"оптимальн\w*", "«оптимально» без критерия"), (r"достаточн\w*", "«достаточно» без меры"),
    (r"приемлем\w*", "«приемлемо» без меры"), (r"современн\w*", "размытое «современный»"),
    (r"качественн\w*", "«качественно» без критерия"), (r"при необходимости", "условие «при необходимости»"),
    (r"по возможности", "лазейка «по возможности»"), (r"если возможно", "лазейка «если возможно»"),
    (r"как правило", "«как правило» — неясны исключения"), (r"\bобычно\b", "«обычно» — неясны исключения"),
    (r"\bоколо\b", "«около» — неточное значение"), (r"примерно", "«примерно» — неточное значение"),
    (r"приблизительно", "«приблизительно» — неточное значение"), (r"и т\.\s?п\.", "открытый список «и т.п.»"),
    (r"и т\.\s?д\.", "открытый список «и т.д.»"), (r"и др\.", "открытый список «и др.»"),
    (r"и далее\b", "открытый список «и далее»"), (r"и/или", "двусмысленное «и/или»"),
    (r"по усмотрению", "«по усмотрению» — неясно чьему"), (r"в разумные сроки", "«в разумные сроки» без числа"),
    (r"максимально\b", "«максимально» без числа"), (r"высок\w+ нагрузк\w*", "«высокая нагрузка» без числа"), (r"user[- ]friendly", "субъективное user-friendly"),
    (r"\betc\b", "открытый список etc."), (r"as appropriate", "лазейка as appropriate"),
    (r"if possible", "лазейка if possible"), (r"\bquickly\b|\bfast\b", "«fast» без числа"),
]
TIME_WORDS = re.compile(r"в момент|сразу|мгновенн|в реальном времени|быстр|оперативн|своевременн|quickly|fast")

PLACEHOLDER_RE = re.compile(
    r"\bTBD\b|\bTODO\b|\bуточн(?:ить|яется|им)\b|заполня(?:ется|ются)\b|будет определ|не заполнен|\bxxx\b",
    re.I,
)
EMPTY_CELL_RE = re.compile(r"^\s*(?:—|-|–|нет|n/?a|tbd)?\s*$", re.I)
CITY_TZ_RE = re.compile(r"\b(?:по|в)\s+(Алматы|Астан[еыа]|Нур-Султан\w*|Москв[еы]|Ташкент\w*|Бишкек\w*|Шымкент\w*|Караганд\w*|Актобе|Атырау|Минск\w*|местному времени|времени клиента)", re.I)
TIME_CONTEXT_RE = re.compile(r"дат|врем|месяц|сутк|день|дн[яеи]|час|календар", re.I)
AMOUNT_RE = re.compile(r"(\d{1,3}(?:[   ]\d{3})+|\d+)\s*(?:₸|тенге|тг\b|KZT)", re.I)
RANGE_RE = re.compile(r"(?:от\s+)?(\d{1,3}(?:[   ]\d{3})+|\d{3,})\s*(?:до|–|—|-)\s*(\d{1,3}(?:[   ]\d{3})+|\d{3,})")
DOCREF_RE = re.compile(r"(?:документ\w*|спецификаци\w*|приложени\w*)\s+«([^»]{5,})»", re.I)


def _strip_md(s: str) -> str:
    return re.sub(r"[*`#]+", "", s).strip()


def _q(doc: Document, n: int, fragment: str | None = None) -> Quote:
    text = fragment if fragment else _strip_md(doc.lines[n - 1].text).strip("| ").strip()
    return Quote(text=text[:300], line=n)


def _f(source: str, category: str, severity: str, proof: str, title: str, explanation: str,
       quotes: list[Quote], suggestion: str = "", question: Question | None = None, scenario: str = "",
       impact: str = "", search_scope: str = "") -> Finding:
    return Finding(source=f"rule:{source}", category=category, criterion=criterion_for(category), severity=severity,
                   proof=proof, title=title, explanation=explanation, quotes=quotes, suggestion=suggestion,
                   question=question, scenario=scenario, impact=impact, search_scope=search_scope,
                   votes=["правила"])


def _int(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _sentence(line: str, start: int, end: int) -> str:
    left = max(line.rfind(". ", 0, start), line.rfind("; ", 0, start))
    left = 0 if left < 0 else left + 2
    right_candidates = [i for i in (line.find(". ", end), line.find("; ", end)) if i >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(line)
    return line[left:right].strip()


def _table_blocks(doc: Document):
    """Блоки таблиц: (номера строк заголовка, список (номер строки, ячейки))."""
    from .segment import TABLE_SEP_RE, _cells

    i, n = 0, len(doc.lines)
    while i < n:
        if doc.lines[i].text.strip().startswith("|"):
            j = i
            while j < n and doc.lines[j].text.strip().startswith("|"):
                j += 1
            header = _cells(doc.lines[i].text)
            rows = [(k + 1, _cells(doc.lines[k].text)) for k in range(i + 1, j)
                    if not TABLE_SEP_RE.match(doc.lines[k].text.strip())]
            yield i + 1, header, rows
            i = j
        else:
            i += 1


# ---------------------------------------------------------------- правила

def rule_placeholders(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out: list[Finding] = []
    by_section: dict[str, list[int]] = defaultdict(list)
    for ln in doc.lines:
        s = ln.text.strip()
        if not s or s.startswith("#"):
            continue
        if PLACEHOLDER_RE.search(s) and "Да / Нет" not in s:
            by_section[ln.section].append(ln.n)
    for section, nums in by_section.items():
        kind = section_kind(section)
        sev = "major" if kind in {"TEST", "AC", "FR", "NFR"} else "minor"
        out.append(_f("placeholder", "template", sev, "Ш", f"Заглушка или незаполненное место в разделе «{section.split('>')[-1].strip()}»",
                      "Раздел содержит заглушку вместо содержания; по нему нельзя подтвердить готовность документа.",
                      [_q(doc, n) for n in nums[:3]], suggestion="Заполнить раздел или явно пометить его как «не требуется» с обоснованием."))
    # незаполненные поля в таблицах «поле — значение»
    for start, header, rows in _table_blocks(doc):
        if len(header) != 2:
            continue
        empty = []
        for n, cells in rows:
            if len(cells) < 2:
                continue
            val = cells[1]
            if EMPTY_CELL_RE.match(val) or re.search(r":\s*—\s*\.?\s*$", val):
                empty.append((n, cells[0]))
        if empty:
            names = ", ".join(name for _, name in empty)
            out.append(_f("empty_fields", "template", "minor", "Ш", f"Не заполнены поля: {names}",
                          "В таблице параметров документа поля оставлены пустыми или с прочерком.",
                          [_q(doc, n) for n, _ in empty[:4]],
                          suggestion="Заполнить значения или указать, почему поле не применяется."))
    return out


def rule_checklist_and_empty_tables(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out: list[Finding] = []
    for start, header, rows in _table_blocks(doc):
        unfilled = [n for n, cells in rows if any(re.fullmatch(r"Да\s*/\s*Нет(\s*/\s*Не требуется)?", c) for c in cells)]
        if len(unfilled) >= 2:
            out.append(_f("checklist", "template", "minor", "Ш", "Чек-лист ревью не заполнен",
                          f"В {len(unfilled)} строках чек-листа оставлены варианты «Да / Нет» из шаблона — проверки не проведены "
                          "или их результат не зафиксирован. Это нельзя считать подтверждением согласования.",
                          [_q(doc, n, rows_cell) for n, rows_cell in [(unfilled[0], next(c for c in dict(rows)[unfilled[0]] if c))]],
                          suggestion="Провести ревью по чек-листу и проставить результаты (агент заполняет черновик ниже в отчёте)."))
        if rows and all(not any(c for c in cells) for _, cells in rows):
            out.append(_f("empty_table", "template", "minor", "Ш", f"Таблица «{' / '.join(h for h in header if h)}» пуста",
                          "Таблица из шаблона не заполнена.", [_q(doc, start)],
                          suggestion="Заполнить таблицу (ответственные, подписи) до передачи в разработку."))
    return out


def rule_weak_words(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out: list[Finding] = []
    for r in reqs:
        hits = []
        for n in range(r.line_start, r.line_end + 1):
            line = _strip_md(doc.lines[n - 1].text)
            for pattern, hint in WEAK_WORDS:
                for m in re.finditer(pattern, line, re.I):
                    hits.append((n, m, hint, line))
        if not hits:
            continue
        hits.sort(key=lambda h: h[1].start())
        quotes, hints, seen = [], [], set()
        for n, m, hint, line in hits:
            hints.append(hint)
            frag = _sentence(line, m.start(), m.end())
            if frag in seen:
                continue
            seen.add(frag)
            quotes.append(Quote(text=frag, line=n))
        timing = any(TIME_WORDS.search(m.group(0).lower()) for _, m, _, _ in hits)
        category = "unverifiable" if timing else "ambiguity"
        sev = "major" if r.kind in {"FR", "NFR", "GOAL", "BR"} and timing else "minor"
        out.append(_f("weak_words", category, sev, "У", f"Размытая формулировка: {', '.join(dict.fromkeys(hints))}",
                      "Слово не задаёт измеримого критерия; разные читатели поймут его по-разному, а тест нельзя "
                      "однозначно признать пройденным (ISO 29148 §5.2.7 — избегать неопределённых терминов).",
                      quotes[:3], suggestion="Заменить на измеримое значение (число, предел, перечень)."))
    return out


def rule_http(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out: list[Finding] = []
    api = [r for r in reqs if r.kind == "API"]
    if not api:
        return out
    method_req = None
    for r in api:
        if r.doc_ref and re.search(r"rest type|method|метод|http", r.doc_ref, re.I) and re.search(r"\bGET\b", r.text):
            method_req = r
            break
    if method_req:
        mutating = []
        for r in api:
            if r is method_req:
                continue
            if re.search(r"сохран|замен|измен|созда|удал|обнов|\bsave|update|create|delete", r.text, re.I):
                mutating.append(r)
        if mutating:
            quotes = [_q(doc, method_req.line_start)] + [_q(doc, r.line_start) for r in mutating[:2]]
            out.append(_f("http_get_mutation", "incorrect", "major", "Д", "Метод GET используется для изменения состояния",
                          "Метод заявлен как GET, а по описанию он сохраняет/заменяет данные. GET по RFC 9110 §9.2.1 — "
                          "«безопасный» метод только для чтения: его кэшируют прокси, повторяют клиенты и браузеры, "
                          "параметры (идентификатор карты, сумма) попадают в URL, логи и историю.",
                          quotes, suggestion="Для сохранения использовать POST/PUT с параметрами в теле запроса; GET оставить для чтения настройки.",
                          impact="Повтор или предзагрузка запроса прокси/браузером может незаметно изменить настройку клиента; "
                                 "идентификатор карты и сумма оседают в логах веб-серверов."))
    for r in api:
        if re.search(r"content-type", r.text, re.I):
            near = [x for x in api if abs(x.line_start - r.line_start) <= 3 and re.search(r"тело не используется|без тела|body (is )?not used|no body", x.text, re.I)]
            if near:
                out.append(_f("content_type_no_body", "ambiguity", "minor", "У", "Указан Content-Type, но тело запроса не используется",
                              "Неясно, обязателен ли заголовок, относится ли он к ответу или это остаток шаблона.",
                              [_q(doc, r.line_start), _q(doc, near[0].line_start)],
                              suggestion="Убрать Content-Type для запроса без тела или указать, что он относится к ответу."))
                break
    return out


def rule_status_codes(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out: list[Finding] = []
    api_lines = [ln for ln in doc.lines if section_kind(ln.section) == "API"]
    if not api_lines:
        return out
    head_re = re.compile(r"^\W*([1-5]\d\d)\b\.?\s*(?:Successful|Unsuccessful|Успешн\w*|Неуспешн\w*)?\s*[—–-]\s*([A-Za-z][A-Za-z ]+?)\s*$")
    described: dict[int, int] = {}
    desc_re = re.compile(r"^\W*(?:ответ|код|статус|status|http)?\s*([1-5]\d\d)\b\s*(?:[.—–:-]|возвраща|returns?)", re.I)
    for ln in api_lines:
        s = _strip_md(ln.text)
        for sent in re.split(r"(?<=[.!?])\s+", s):
            m = desc_re.match(sent)
            if m:
                described.setdefault(int(m.group(1)), ln.n)
        hm = head_re.match(s)
        if hm:
            code, reason = int(hm.group(1)), hm.group(2).strip().lower()
            std = HTTP_REASON.get(code)
            if std and reason != std:
                right = [c for c, r in HTTP_REASON.items() if r == reason]
                hint = f" «{hm.group(2).strip()}» — это код {right[0]}." if right else ""
                out.append(_f("status_reason", "incorrect", "minor", "Д", f"Код {code} подписан как «{hm.group(2).strip()}»",
                              f"Стандартное название кода {code} — «{std.title()}».{hint} Неясно, какой код на самом деле возвращается.",
                              [_q(doc, ln.n)], suggestion="Согласовать код и подпись (RFC 9110 §15)."))
    # упомянутые, но не описанные коды; несоответствие кода раздела и описанного поведения
    mentioned: dict[int, int] = {}
    for ln in api_lines:
        s = _strip_md(ln.text)
        if s.startswith("{") or "curl" in s.lower():
            continue
        for m in re.finditer(r"(?<![\d.,])([1-5]\d\d)(?![\d.,])", s):
            mentioned.setdefault(int(m.group(1)), ln.n)
    for code, n in sorted(mentioned.items()):
        if code not in described and code in HTTP_REASON:
            out.append(_f("status_undescribed", "incompleteness", "minor", "У", f"Код ответа {code} упомянут, но не описан",
                          f"Код {code} встречается в тексте, но для него нет описания условий и тела ответа.",
                          [_q(doc, n)], suggestion=f"Описать, когда возвращается {code} и что в теле, либо убрать упоминание."))
    sec_code_re = re.compile(r"(?:^|>)\s*([1-5]\d\d)\b[^>]*$")
    for ln in api_lines:
        m = sec_code_re.search(ln.section)
        if not m:
            continue
        sec_code = int(m.group(1))
        s = _strip_md(ln.text)
        rm = re.search(r"возвраща\w*\s+(?:код\s+)?([1-5]\d\d)\b|returns?\s+([1-5]\d\d)\b", s, re.I)
        if rm:
            other = int(rm.group(1) or rm.group(2))
            if other != sec_code:
                out.append(_f("status_section_mismatch", "contradiction", "minor", "Д",
                              f"Раздел кода {sec_code} описывает ответ {other}",
                              f"Заголовок раздела относится к коду {sec_code}, а текст говорит, что возвращается {other}. "
                              f"Возврат {other} для чужого ресурса — допустимая практика (скрыть существование), но тогда "
                              f"нужно описать {other} отдельно и решить, возвращается ли {sec_code} вообще.",
                              [_q(doc, ln.n)], suggestion=f"Вынести {other} в отдельный раздел статусов; уточнить сценарий {sec_code}."))
    return out


def rule_units(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    pairs = [(r"тиын", r"тенге|₸|KZT|_kzt\b", "тиыны", "тенге"), (r"копе[ейк]", r"рубл|₽|RUB", "копейки", "рубли"),
             (r"\bcents?\b", r"dollars?|USD|\$", "центы", "доллары")]
    out: list[Finding] = []
    for minor_re, major_re, a, b in pairs:
        minor_lines = [r for r in reqs if re.search(minor_re, r.text, re.I)]
        major_lines = [r for r in reqs if re.search(major_re, r.text, re.I) and r.kind in {"DATA", "API", "FR"}]
        if minor_lines and major_lines:
            q = [_q(doc, minor_lines[0].line_start)]
            other = next((r for r in major_lines if r.line_start != minor_lines[0].line_start), None)
            if other:
                q.append(_q(doc, other.line_start))
            out.append(_f("units", "incompleteness", "minor", "У", f"Суммы хранятся в разных единицах ({a} и {b}) без правила приведения",
                          f"Разные единицы хранения допустимы, но не задано правило перевода и сравнения ({a} → {b}, округление). "
                          "Ошибка приведения даёт расхождение в 100 раз.",
                          q, suggestion=f"Явно указать единицу сравнения с порогом и правило пересчёта {a}↔{b}.",
                          impact="Порог может сработать в 100 раз раньше или позже ожидаемого."))
    return out


def rule_timezone(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out = []
    for r in reqs:
        m = CITY_TZ_RE.search(r.text)
        if m and TIME_CONTEXT_RE.search(r.text):
            out.append(_f("timezone_city", "ambiguity", "minor", "У", "Часовой пояс задан городом, а не явно",
                          f"«{m.group(0)}» — не идентификатор часового пояса. Нужен явный пояс (например, Asia/Almaty, UTC+5; "
                          "с 01.03.2024 Казахстан использует единый UTC+5), иначе граница суток/месяца трактуется по-разному.",
                          [_q(doc, r.line_start, _sentence(_strip_md(doc.lines[r.line_start - 1].text), *_span(doc, r.line_start, m.group(0))))],
                          suggestion="Указать часовой пояс явно: «по времени Asia/Almaty (UTC+5)»."))
            break
    return out


def _span(doc: Document, n: int, frag: str) -> tuple[int, int]:
    line = _strip_md(doc.lines[n - 1].text)
    i = line.find(frag)
    return (i, i + len(frag)) if i >= 0 else (0, len(line))


def rule_boundaries(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out: list[Finding] = []
    acs = [r for r in reqs if r.kind == "AC"]
    if not acs:
        return out
    ac_text = " ".join(r.text for r in acs)
    ac_nums = {_int(x) for x in re.findall(r"\d{1,3}(?:[   ]\d{3})+|\d+", ac_text)}
    seen = set()
    for r in reqs:
        if r.kind not in {"FR", "API", "NFR"}:
            continue
        for m in RANGE_RE.finditer(r.text):
            lo, hi = _int(m.group(1)), _int(m.group(2))
            if lo >= hi or (lo, hi) in seen:
                continue
            seen.add((lo, hi))
            probes = {lo - 1, lo, hi, hi + 1}
            if not probes & ac_nums:
                vals = ", ".join(_fmt(v) for v in sorted(probes))
                out.append(_f("boundaries", "incompleteness", "major", "У", f"Нет критериев приёмки для границ диапазона {_fmt(lo)}–{_fmt(hi)}",
                              "Диапазон задан, но ни один критерий приёмки не проверяет его границы и значения за ними; "
                              "включительность границ и сообщение об ошибке не подтверждены примерами.",
                              [_q(doc, r.line_start, _sentence(_strip_md(doc.lines[r.line_start - 1].text), *_span(doc, r.line_start, m.group(0))))],
                              suggestion=f"Добавить критерии приёмки для значений {vals}: допустимые сохраняются, остальные отклоняются с указанным сообщением."))
    return out


def rule_ac_arithmetic(doc: Document, reqs: list[Requirement]) -> tuple[list[Finding], list[str]]:
    out, notes = [], []
    for r in reqs:
        if r.kind != "AC":
            continue
        parts = {p.split(":", 1)[0].strip().lower(): p.split(":", 1)[1] for p in r.text.split("; ") if ":" in p}
        pre = next((v for k, v in parts.items() if "исходн" in k or "услови" in k or "given" in k or k == "дано"), "")
        act = next((v for k, v in parts.items() if "действ" in k or "when" in k or k == "когда"), "")
        res = next((v for k, v in parts.items() if "ожида" in k or "результат" in k or "then" in k or k == "тогда"), "")
        if not (pre and act and res):
            continue
        pre_m = re.search(r"(расход\w*|баланс\w*|остат\w*)\s+" + AMOUNT_RE.pattern, pre, re.I)
        act_m = AMOUNT_RE.search(act)
        res_m = re.search(r"(?:итог|сумм\w*|расход\w*|счётчик\w*|счетчик\w*|баланс\w*|остат\w*)[^\d]{0,25}" + AMOUNT_RE.pattern, res, re.I)
        if not (pre_m and act_m and res_m):
            continue
        a, b, c = _int(pre_m.group(2)), _int(act_m.group(1)), _int(res_m.group(1))
        spending = pre_m.group(1).lower().startswith("расход")
        if spending and re.search(r"покупк|оплат", act, re.I):
            expected, op = a + b, "+"
        elif spending and re.search(r"возврат", act, re.I):
            expected, op = a - b, "−"
        elif not spending and re.search(r"перев|списан|оплат|покупк|сняти", act, re.I):
            expected, op = a - b, "−"
        elif not spending and re.search(r"пополн|зачисл|возврат", act, re.I):
            expected, op = a + b, "+"
        else:
            continue
        if expected == c:
            notes.append(f"Арифметика критерия приёмки {r.rid} (строка {r.line_start}) верна: {_fmt(a)} {op} {_fmt(b)} = {_fmt(c)}.")
        else:
            out.append(_f("ac_arithmetic", "incorrect", "major", "Д", "Ошибка в арифметике критерия приёмки",
                          f"{_fmt(a)} {op} {_fmt(b)} = {_fmt(expected)}, а ожидаемый результат указан {_fmt(c)}.",
                          [_q(doc, r.line_start)], suggestion="Исправить ожидаемый результат или исходные данные."))
    return out, notes


def rule_version(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    text = "\n".join(ln.text for ln in doc.lines)
    has_version = re.search(r"верси[яи]\s*(?:документа)?\s*[:№]?\s*v?\d", text, re.I) or re.search(r"\bversion\s*[:]?\s*\d", text, re.I)
    if has_version:
        return []
    n = next((ln.n for ln in doc.lines if re.search(r"верси", ln.text, re.I)), None)
    return [_f("version", "template", "minor", "Ш", "Не указаны версия и дата документа",
               "Без версии нельзя однозначно сослаться на согласованную редакцию и отследить изменения (IEEE 830 §4.3.7).",
               [_q(doc, n)] if n else [], suggestion="Добавить номер версии, дату и историю изменений.",
               search_scope="весь документ: строка вида «Версия: N» не найдена")]


def rule_doc_refs(doc: Document, reqs: list[Requirement]) -> tuple[list[Finding], list[str]]:
    out, notes = [], []
    norm = [re.sub(r"\s+", " ", _strip_md(ln.text)).lower() for ln in doc.lines]
    for ln in doc.lines:
        for m in DOCREF_RE.finditer(_strip_md(ln.text)):
            title = re.sub(r"\s+", " ", m.group(1)).lower()
            found = [i + 1 for i, t in enumerate(norm) if title in t and i + 1 != ln.n]
            if found:
                notes.append(f"Ссылка на документ «{m.group(1)}» (строка {ln.n}) разрешена: документ найден во входном файле (строка {found[0]}).")
            else:
                out.append(_f("doc_ref", "incompleteness", "major", "У", f"Ссылка на документ «{m.group(1)}», которого нет во входных данных",
                              "Требование опирается на внешний документ; без него проверить согласованность невозможно.",
                              [_q(doc, ln.n)], suggestion="Приложить документ или включить нужные требования в текст.",
                              question=Question(text=f"Предоставьте документ «{m.group(1)}» или укажите его версию.", addressee="Аналитик")))
    return out, notes


def rule_traceability(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    core = [r for r in reqs if r.kind in {"NFR", "DATA", "INT", "AC"}]
    unnumbered = [r for r in core if not r.doc_ref]
    numbered_fr = [r for r in reqs if r.kind == "FR" and r.doc_ref]
    if len(unnumbered) >= 3 and numbered_fr:
        kinds = sorted({r.kind for r in unnumbered})
        from .segment import KIND_RU
        return [_f("traceability", "traceability", "minor", "У", "Часть требований не имеет идентификаторов",
                   f"ФТ пронумерованы ({', '.join(r.doc_ref for r in numbered_fr[:4])}), а {', '.join(KIND_RU[k] for k in kinds)} — нет. "
                   "На них нельзя сослаться из тестов и задач; агент присвоил собственные R-xx.",
                   [_q(doc, r.line_start) for r in unnumbered[:2]],
                   suggestion="Пронумеровать все требования и критерии приёмки (НФ-1, Д-1, И-1, КП-1…) и связать КП с ФТ.")]
    return []


STOP = {"для", "при", "если", "или", "это", "который", "которая", "поле", "значение"}


def _stems(text: str) -> list[str]:
    words = re.findall(r"[a-zа-яё]{4,}", text.lower().replace("ё", "е"))
    return [w[:5] for w in words if w not in STOP]


def rule_data_model(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    """Поля, которые по API хранятся в БД, но отсутствуют в требованиях к данным."""
    data = [r for r in reqs if r.kind == "DATA"]
    if not data:
        return []
    data_stems = set(_stems(" ".join(r.text for r in data)))
    missing = []
    for start, header, rows in _table_blocks(doc):
        h = [c.lower() for c in header]
        db_i = next((i for i, c in enumerate(h) if re.search(r"db|поле бд|бд|столбец|column", c)), None)
        desc_i = next((i for i, c in enumerate(h) if re.search(r"description|описание", c)), None)
        name_i = 0
        if db_i is None or desc_i is None:
            continue
        for n, cells in rows:
            if len(cells) <= max(db_i, desc_i):
                continue
            db, desc = cells[db_i], cells[desc_i]
            if not db or EMPTY_CELL_RE.match(db):
                continue
            stems = _stems(desc)
            if stems and any(s not in data_stems for s in stems) and db not in [m[1] for m in missing]:
                missing.append((n, db, desc, cells[name_i]))
    if not missing:
        return []
    names = ", ".join(f"«{d}» (`{db}`)" for _, db, d, _ in missing)
    return [_f("data_model", "incompleteness", "major", "У", f"Хранимые поля API отсутствуют в требованиях к данным: {names}",
               "По описанию API эти значения сохраняются в БД, но перечень «Требования к данным» их не содержит — "
               "модель данных в двух местах документа расходится.",
               [_q(doc, n) for n, *_ in missing[:2]] + [_q(doc, data[0].line_start)],
               suggestion="Дополнить требования к данным недостающими полями (тип, единица, обязательность) или убрать их из API.")]


def rule_glossary(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    if sum(1 for r in reqs if r.kind == "FR") < 3:
        return []
    if any(re.search(r"глоссари|термины и определения|^#+\s*определения|glossary|definitions", ln.text, re.I) for ln in doc.lines):
        return []
    return [_f("glossary", "terminology", "minor", "У", "В документе нет глоссария терминов",
               "Ключевые термины предметной области не определены; разные участники могут понимать их по-разному "
               "(ISO 29148 §5.2.6 — понятность набора).",
               [], suggestion="Добавить раздел «Термины и определения» для ключевых понятий бизнес-правил.",
               search_scope="весь документ: раздел «Глоссарий / Термины и определения» не найден")]


def rule_rollback(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    check = [ln for ln in doc.lines if section_kind(ln.section) == "REVIEW" and re.search(r"откат", ln.text, re.I)]
    if not check:
        return []
    described = any(re.search(r"откат|rollback|feature[- ]?flag|фич[еа]-?флаг|скрыти\w* функционал", ln.text, re.I)
                    for ln in doc.lines if section_kind(ln.section) != "REVIEW")
    if described:
        return []
    return [_f("rollback", "incompleteness", "minor", "У", "Не описана схема отката или скрытия функционала",
               "Чек-лист шаблона требует схему отката (или вариант скрытия функционала), но в требованиях она не описана.",
               [_q(doc, check[0].n)], suggestion="Описать откат: флаг включения функции, поведение при выключении, судьба накопленных данных.",
               search_scope="все разделы, кроме чек-листа: упоминаний отката/feature flag нет")]


# Группы синонимов предметной области: разные имена одного понятия в одном документе — признак дрейфа терминов.
TERM_GROUPS = [
    ("порог/лимит", [("порог", r"порог\w*"), ("лимит", r"лимит\w*"), ("limit", r"(?i)limit"), ("бюджет", r"бюджет\w*"),
                     ("threshold", r"(?i)threshold"), ("budget", r"(?i)budget")]),
    ("баланс/остаток", [("баланс", r"баланс\w*"), ("остаток", r"остат(?:о|к)\w*"), ("balance", r"(?i)balance")]),
    ("комиссия/плата", [("комиссия", r"комисси\w*"), ("плата за", r"плат[аы] за"), ("fee", r"(?i)\bfee")]),
]


def rule_term_drift(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    out = []
    for concept, variants in TERM_GROUPS:
        seen: list[tuple[str, int]] = []
        for name, pattern in variants:
            for ln in doc.lines:
                if section_kind(ln.section) in {"META", "REVIEW", "APPROVAL"}:
                    continue
                if re.search(pattern, ln.text, 0 if pattern.startswith("(?i)") else re.I):
                    seen.append((name, ln.n))
                    break
        names = {n for n, _ in seen}
        sections = {doc.lines[line - 1].section for _, line in seen}
        # английский идентификатор и русский термин одного корня (limit/лимит) — не дрейф
        if names >= {"limit", "лимит"} and len(names) == 2:
            continue
        if len(names) >= 2 and len(sections) >= 2:
            variants_str = ", ".join(f"«{n}» (строка {line})" for n, line in seen)
            out.append(_f("term_drift", "terminology", "minor", "У", f"Одно понятие ({concept}) названо по-разному",
                          f"В документе встречаются разные имена одного понятия: {variants_str}. Читатель не может быть уверен, "
                          "что это одно и то же; имена полей и методов API расходятся с бизнес-терминами.",
                          [_q(doc, line) for _, line in seen[:4]],
                          suggestion="Выбрать один термин, закрепить его в глоссарии и использовать в ФТ, UI, API и данных; "
                                     "либо явно указать соответствие (например, поле API = термин ФТ)."))
    return out


def rule_card_data(doc: Document, reqs: list[Requirement]) -> list[Finding]:
    hits = [r for r in reqs if re.search(r"цифр\w* карты|номер\w* карты|\bPAN\b|cardId|card_id|номер карт", r.text, re.I)]
    if not hits:
        return []
    text = " ".join(ln.text for ln in doc.lines)
    if re.search(r"маскир|PCI|токениз|персональн\w* данн|шифр", text, re.I):
        return []
    return [_f("card_data", "security", "major", "У", "Не определены требования к защите карточных данных",
               "Документ передаёт и хранит идентификаторы и цифры карты, но не определяет, что это за идентификатор "
               "(PAN или токен), как данные маскируются в push и логах и какие требования PCI DSS и закона о персональных "
               "данных применяются.",
               [_q(doc, r.line_start) for r in hits[:2]],
               suggestion="Указать, что идентификатор карты в API и событиях — токен, а не PAN; в уведомлениях и интерфейсе только "
                          "маска «•• 1234»; номер карты не пишется в логи и URL.",
               question=Question(text="Что такое идентификатор карты в API и событиях — номер карты (PAN) или токен?",
                                 options=["Токен карты", "Маскированный PAN", "Полный PAN (тогда нужен контур PCI DSS)"],
                                 addressee="ИБ"))]


def run_rules(doc: Document, reqs: list[Requirement]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    for rule in (rule_placeholders, rule_checklist_and_empty_tables, rule_weak_words, rule_http, rule_status_codes,
                 rule_units, rule_timezone, rule_boundaries, rule_version, rule_traceability, rule_data_model,
                 rule_glossary, rule_rollback, rule_card_data, rule_term_drift):
        try:
            findings.extend(rule(doc, reqs))
        except Exception as e:  # правило не должно ронять прогон
            notes.append(f"Правило {rule.__name__} завершилось с ошибкой: {e}")
    for rule in (rule_ac_arithmetic, rule_doc_refs):
        try:
            f, n = rule(doc, reqs)
            findings.extend(f)
            notes.extend(n)
        except Exception as e:
            notes.append(f"Правило {rule.__name__} завершилось с ошибкой: {e}")
    return findings, notes
