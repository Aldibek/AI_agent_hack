"""Генерация отчёта (Markdown) и трассировки прогона (JSON)."""

from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__
from .criteria import (CATEGORY, INDIVIDUAL, INDIVIDUAL_MAP, PROOF_RU, SET, SET_MAP, SEVERITY_ORDER, SEVERITY_RU,
                       category_ru)
from .models import Finding, Requirement
from .pipeline import RunResult
from .segment import KIND_RU
from .verdict import compute

SEV_ICON = {"blocker": "🔴", "major": "🟠", "minor": "🟡"}
JUDGE_RU = {"confirmed": "подтверждено", "disputed": "спорно — требует проверки аналитиком", "rejected": "отклонено", "": "не проверялось"}
COLS = ["unambiguous", "complete", "verifiable", "consistent", "singular", "correct", "appropriate", "conforming"]
COL_RU = {"unambiguous": "Однозн.", "complete": "Полнота", "verifiable": "Проверяем.", "consistent": "Непротив.",
          "singular": "Единичн.",
          "correct": "Корректн.", "appropriate": "Уместн.", "conforming": "Шаблон"}


def agent_version() -> str:
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(["git", "-C", str(root), "describe", "--tags", "--always", "--dirty"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return f"v{__version__}, коммит {out.stdout.strip()}"
    except Exception:
        pass
    return f"v{__version__} (git недоступен)"


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def _lines_str(f: Finding) -> str:
    ls = sorted({q.line_start for q in f.quotes if q.line_start})
    if not ls:
        return "—"
    return ("строка " if len(ls) == 1 else "строки ") + ", ".join(str(x) for x in ls)


def _rid_label(rid: str, by_id: dict[str, Requirement]) -> str:
    r = by_id.get(rid)
    if not r:
        return rid
    return f"{rid} ({r.doc_ref})" if r.doc_ref else rid


def _votes_str(f: Finding, total: int) -> str:
    llm = [v for v in f.votes if v != "правила"]
    parts = []
    if llm:
        parts.append(", ".join(llm) + (f" ({len(llm)} из {total})" if total else ""))
    if "правила" in f.votes:
        parts.append("детерминированные правила")
    return "; ".join(parts) or f.source


PAGES: dict[int, int] = {}  # номер строки → страница PDF (заполняется в render)


def finding_block(f: Finding, by_id: dict[str, Requirement], total_reviewers: int) -> list[str]:
    out = [f"#### {f.fid} · {category_ru(f.category)} · {SEV_ICON.get(f.severity, '')} {SEVERITY_RU.get(f.severity, f.severity)} · [{f.proof}] {PROOF_RU.get(f.proof, '')}",
           "", f"**Суть:** {f.title}", ""]
    where = _lines_str(f)
    if f.rids:
        where += " — " + ", ".join(_rid_label(r, by_id) for r in f.rids)
    out += [f"**Где:** {where}", ""]
    for q in f.quotes:
        loc = f"строка {q.line_start}" if q.line_start == q.line_end else f"строки {q.line_start}–{q.line_end}"
        if PAGES.get(q.line_start):
            loc += f", с. {PAGES[q.line_start]}"
        out.append(f"> {loc}: «{q.text.strip()}»")
    if f.quotes:
        out.append("")
    if f.search_scope:
        out += [f"**Где искали:** {f.search_scope}", ""]
    out += [f"**Нарушено:** {f.criterion}", ""]
    if f.explanation:
        out += [f"**Почему это дефект:** {f.explanation}", ""]
    if f.scenario:
        out += [f"**Сценарий, на котором документ ломается:** {f.scenario}", ""]
    if f.impact:
        out += [f"**Последствия:** {f.impact}", ""]
    if f.suggestion:
        out += [f"**Что сделать:** {f.suggestion}", ""]
    if f.question:
        q = f.question
        opts = ""
        if q.options:
            letters = "абвгд"
            opts = " Варианты: " + "; ".join(f"({letters[i]}) {o}" for i, o in enumerate(q.options[:5]))
        out += [f"**Вопрос автору{(' (' + q.addressee + ')') if q.addressee else ''}:** {q.text}{opts}", ""]
    meta = f"**Кто нашёл:** {_votes_str(f, total_reviewers)} · **Судья:** {JUDGE_RU.get(f.judge, f.judge)}"
    if f.judge_note:
        meta += f" — {f.judge_note}"
    out.append(meta)
    if f.status_note:
        out.append(f"\n_Примечание: {f.status_note.strip()}_")
    out.append("")
    return out


def is_set_level(f: Finding) -> bool:
    return len(f.rids) != 1 or f.category in ("contradiction", "terminology", "template", "traceability")


def render(result: RunResult, out_path: Path) -> str:
    doc, reqs, findings = result.doc, result.reqs, result.findings
    PAGES.clear()
    PAGES.update({ln.n: ln.page for ln in doc.lines if ln.page})
    by_id = {r.rid: r for r in reqs}
    total = len(result.reviewer_labels)
    v = compute(findings, reqs)
    L: list[str] = []
    L += [f"# Отчёт о проверке требований: {doc.name}", ""]
    L += ["> Отчёт сформирован агентом ReqAudit автоматически. Каждое замечание привязано к дословной цитате и номеру строки "
          "проверяемого документа; цитаты проверены программно.", ""]

    # 1. паспорт
    L += ["## 1. Паспорт прогона", "", "| Параметр | Значение |", "| :--- | :--- |"]
    title = next((ln.text.lstrip("# ").strip() for ln in doc.lines if ln.text.startswith("# ")), doc.name)
    L.append(f"| Документ | {_esc(title)} (файл `{doc.name}`) |")
    L.append(f"| Формат / объём | {doc.fmt.upper()}, строк: {len(doc.lines)}" + (f", страниц: {doc.pages}" if doc.pages else "") + " |")
    L.append(f"| SHA-256 файла | `{doc.sha256[:16]}…` |")
    L.append(f"| Дата прогона | {datetime.now().strftime('%d.%m.%Y %H:%M')} |")
    L.append(f"| Версия агента | {agent_version()} |")
    if result.reviewers:
        revs = "; ".join(f"{p.company} — `{p.model}`" for p in result.reviewers)
        L.append(f"| Модели-ревьюеры | {revs} (проходов: {total}) |")
    if result.orchestrator:
        L.append(f"| Оркестратор | {result.orchestrator.company} — `{result.orchestrator.model}` |")
    if result.judge:
        ind = "независимый, не участвовал в ревью" if result.judge_independent else "участвовал в ревью (независимого судьи нет)"
        L.append(f"| Судья | {result.judge.company} — `{result.judge.model}` ({ind}) |")
    mode = {"full": "полный (правила + LLM-консилиум + судья)", "rules-only": "только детерминированные правила (LLM недоступна)"}
    L.append(f"| Режим | {mode.get(result.mode, result.mode)} |")
    s = result.stats
    L.append(f"| Вызовы LLM | {s.llm_calls} (из кэша: {s.llm_cached}, ошибок: {s.llm_failed}); токены: {s.prompt_tokens} → {s.completion_tokens} |")
    L.append(f"| Время прогона | {s.seconds:.0f} с |")
    L.append("")

    # коротко
    L += ["## Коротко", ""]
    L.append(f"- **Разработку начинать:** {v['dev'][0]}. **Тестирование начинать:** {v['test'][0]}.")
    sev = Counter(f.severity for f in findings)
    L.append(f"- Требований: **{len(reqs)}**. Замечаний: **{len(findings)}** — блокеров {sev.get('blocker', 0)}, "
             f"существенных {sev.get('major', 0)}, незначительных {sev.get('minor', 0)}.")
    if v["fix_first"]:
        L.append("- Исправить в первую очередь: " + "; ".join(f"{f.fid} — {f.title}" for f in v["fix_first"][:3]) + ".")
    L.append("")

    # 2. нарезка
    L += ["## 2. Как агент нарезал требования", "",
          "Идентификаторы R-xx присвоены агентом. Если в документе номера не было, в колонке «В документе» стоит «—»; "
          "цитата и номер строки сохранены.", "",
          "| ID в отчёте | ID в документе | Где | Тип | Краткая формулировка (цитата) |", "| :--- | :--- | :--- | :--- | :--- |"]
    for r in reqs:
        where = f"стр. {r.line_start}" if r.line_start == r.line_end else f"стр. {r.line_start}–{r.line_end}"
        page = doc.lines[r.line_start - 1].page
        if page:
            where += f", с. {page}"
        L.append(f"| {r.rid} | {_esc(r.doc_ref or '—')} | {where} | {KIND_RU.get(r.kind, r.kind)} | {_esc(r.summary)} |")
    L.append("")

    # 3. проверка каждого требования
    per_req: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        for rid in f.rids:
            per_req[rid].append(f)
    L += ["## 3. Проверка каждого требования", "",
          "Характеристики ISO/IEC/IEEE 29148:2018 §5.2.5 и IEEE 830 §4.3. В ячейке — номера замечаний; «✓» — замечаний нет.", "",
          "| Требование | " + " | ".join(COL_RU[c] for c in COLS) + " |", "| :--- | " + " | ".join(":---:" for _ in COLS) + " |"]
    for r in reqs:
        cells = []
        for c in COLS:
            ids = [f.fid for f in per_req.get(r.rid, []) if INDIVIDUAL_MAP.get(f.category) == c]
            cells.append(", ".join(ids) if ids else "✓")
        L.append(f"| {_rid_label(r.rid, by_id)} | " + " | ".join(cells) + " |")
    L.append("")
    clean = [r.rid for r in reqs if not per_req.get(r.rid)]
    if clean:
        L += [f"Без замечаний: {', '.join(clean)}.", ""]
    single = [f for f in findings if not is_set_level(f)]
    if single:
        L += ["### Замечания к отдельным требованиям", ""]
        for f in single:
            L += finding_block(f, by_id, total)

    # 4. проверка набора
    L += ["## 4. Проверка всего набора требований", "",
          "Характеристики набора ISO/IEC/IEEE 29148:2018 §5.2.6 и IEEE 830 §4.3.", "",
          "| Характеристика | Оценка | Замечания |", "| :--- | :--- | :--- |"]
    set_hits: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        key = SET_MAP.get(f.category)
        if key:
            set_hits[key].append(f)
    for key in ("complete", "consistent", "feasible", "comprehensible", "validatable", "traceable", "modifiable", "ranked"):
        name, ref, _ = SET[key]
        hits = set_hits.get(key, [])
        if key == "ranked":
            has_prio = any(w in " ".join(ln.text.lower() for ln in doc.lines) for w in ("приоритет", "must", "should", "важност"))
            grade = "соответствует" if has_prio else "не соответствует: приоритеты требований не указаны"
            L.append(f"| {name} ({ref}) | {grade} | — |")
            continue
        if not hits:
            grade = "замечаний нет" if key not in ("feasible", "modifiable") else "явных проблем не выявлено"
        else:
            worst = min(hits, key=lambda f: SEVERITY_ORDER[f.severity]).severity
            grade = {"blocker": "не соответствует", "major": "частично соответствует", "minor": "в целом соответствует"}[worst]
        L.append(f"| {name} ({ref}) | {grade} | {', '.join(f.fid for f in hits) or '—'} |")
    L.append("")
    setf = [f for f in findings if is_set_level(f)]
    if setf:
        L += ["### Замечания к набору требований (связи между разделами, терминология, документ)", ""]
        for f in setf:
            L += finding_block(f, by_id, total)

    # 5. вопросы
    with_q = [f for f in findings if f.question]
    if with_q:
        L += ["## 5. Уточняющие вопросы к автору требований", "",
              "Сначала — вопросы, ответы на которые сильнее всего разблокируют разработку и тестирование.", ""]
        prio = [f for fid in result.priority for f in with_q if f.fid == fid]
        rest = [f for f in with_q if f not in prio]
        rest.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.fid))
        ordered = prio + rest
        top = ordered[:7]
        L += ["### Главные вопросы", ""]
        for i, f in enumerate(top, start=1):
            L += _question_line(i, f)
        groups: dict[str, list[Finding]] = defaultdict(list)
        for f in ordered[7:]:
            groups[(f.question.addressee or "Аналитик").strip()].append(f)
        if groups:
            L += ["", "### Остальные вопросы по адресатам (повестка встреч)", ""]
            i = len(top)
            for addressee, fs in sorted(groups.items()):
                L += [f"**{addressee}:**", ""]
                for f in fs:
                    i += 1
                    L += _question_line(i, f)
                L.append("")
        L.append("")

    if result.answers:
        L += ["### Ответы автора и перепроверка", "", "| Вопрос | Ответ | Статус | Исправленная формулировка (ТЗ v2) |", "| :--- | :--- | :--- | :--- |"]
        for a in result.answers:
            L.append(f"| {a.get('fid')}: {_esc(a.get('question', ''))} | {_esc(a.get('answer', ''))} | {_esc(a.get('status', ''))} | {_esc(a.get('rewrite', ''))} |")
        L.append("")

    # чек-лист банка
    if result.checklist:
        L += ["## 6. Черновик заполнения чек-листа «Ревью требований» из документа", "",
              "Агент предлагает ответы на пункты чек-листа шаблона по результатам проверки (для подтверждения ответственным).", "",
              "| Проверка | Ответ агента | Обоснование |", "| :--- | :--- | :--- |"]
        for c in result.checklist:
            L.append(f"| {_esc(str(c.get('item', '')))} | {_esc(str(c.get('answer', '')))} | {_esc(str(c.get('why', '')))} |")
        L.append("")

    # сводка
    L += ["## 7. Сводка", ""]
    kinds = Counter(KIND_RU.get(r.kind, r.kind) for r in reqs)
    L.append(f"- **Требований:** {len(reqs)} (" + ", ".join(f"{k}: {n}" for k, n in kinds.most_common()) + ").")
    L.append(f"- **Требований с замечаниями:** {len(per_req)} из {len(reqs)}.")
    L.append(f"- **Замечаний (нарушений):** {len(findings)}.")
    proof = Counter(f.proof for f in findings)
    L.append(f"- По доказанности: доказано текстом [Д] — {proof.get('Д', 0)}, вопросы [У] — {proof.get('У', 0)}, "
             f"шаблон/оформление [Ш] — {proof.get('Ш', 0)}. Категории не суммируются в «число дефектов»: вопрос — не доказанная ошибка.")
    L.append(f"- По тяжести: 🔴 блокеров — {sev.get('blocker', 0)}, 🟠 существенных — {sev.get('major', 0)}, 🟡 незначительных — {sev.get('minor', 0)}.")
    cats = Counter(category_ru(f.category) for f in findings)
    L.append("- По типам: " + ", ".join(f"{k} — {n}" for k, n in cats.most_common()) + ".")
    L += ["", "**Критичные дефекты:**", ""]
    any_crit = False
    for group, fs in v["critical"].items():
        if fs:
            any_crit = True
            L.append(f"- **{group}:** " + "; ".join(f"{f.fid} ({_lines_str(f)}) — {f.title}" for f in fs))
    if not any_crit:
        L.append("- не выявлено")
    L.append("")
    if result.readiness:
        L += ["**Готовность по ролям:**", ""]
        for k, val in result.readiness.items():
            L.append(f"- {k}: {val}")
        L.append("")

    # итог
    L += ["## 8. Итог", ""]
    for label, key in (("Разработка", "dev"), ("Тестирование", "test")):
        st, reasons = v[key]
        line = f"- **{label}: {st.lower()} начинать по текущей версии документа.**" if st == "Нельзя" else \
            (f"- **{label}: можно начинать с оговорками.**" if st == "С оговорками" else f"- **{label}: можно начинать.**")
        if reasons:
            line += " Основания: " + ", ".join(f.fid for f in reasons[:8]) + "."
        L.append(line)
    L += ["", "**Что исправить в первую очередь:**", ""]
    for i, f in enumerate(v["fix_first"], start=1):
        L.append(f"{i}. **{f.fid}** ({_lines_str(f)}) — {f.title}. {f.suggestion or ''}".rstrip())
    if not v["fix_first"]:
        L.append("Критичных исправлений не требуется.")
    L.append("")

    # покрытие
    L += ["## 9. Покрытие прогона и ограничения", ""]
    L.append(f"- Обработано строк: {len(doc.lines)}; выделено требований: {len(reqs)}.")
    if result.notes:
        L += ["- Проверки, которые прошли успешно:"] + [f"  - {n}" for n in result.notes]
    L.append(f"- Отброшено находок без подтверждённой цитаты: {len(result.dropped_ungrounded)}.")
    if result.rejected:
        L.append(f"- Отклонено судьёй как ложные срабатывания: {len(result.rejected)} — " +
                 "; ".join(f"«{f.title}»" for f in result.rejected[:10]) + ".")
    for w in doc.warnings:
        L.append(f"- ⚠️ {w}")
    for e in result.failures:
        L.append(f"- ⚠️ {e}")
    if result.mode == "rules-only":
        L.append("- ⚠️ LLM-анализ не выполнялся: смысловые противоречия и пропуски проверены только правилами. "
                 "Добавьте ключ в `.env` для полной проверки.")
    L.append("- Изображения и диаграммы в виде картинок не анализируются; проверяется текст.")
    L.append("")

    # методика
    L += ["## Приложение. Методика", "",
          "1. Текст документа извлекается кодом с сохранением номеров строк (и страниц PDF).",
          "2. Нарезка на требования — детерминированно по разделам шаблона (ФТ, НФТ, данные, интеграция, критерии приёмки, API).",
          "3. Детерминированные правила: заглушки шаблона, размытые слова (29148 §5.2.7), семантика HTTP, коды ответов, единицы, "
          "часовой пояс, границы диапазонов и их покрытие критериями приёмки, арифметика критериев приёмки, ссылки на документы.",
          "4. Консилиум LLM: независимые проходы ревьюеров (разные модели и/или формулировки), каждый — за 5 ролей: разработчик, "
          "тестировщик, архитектор/ИБ, бизнес-аналитик, злонамеренный подрядчик.",
          "5. Проверка цитат кодом: находка без дословной цитаты из документа удаляется.",
          "6. Склейка одинаковых находок и голосование; оркестратор формулирует итог и вопросы; независимый судья отсеивает "
          "ложные срабатывания.",
          "7. Вердикт — по правилу: есть блокер в ключевых разделах → начинать нельзя; есть существенные → с оговорками.",
          "", "**Шкалы:** тяжесть — 🔴 блокер (нельзя начинать разработку/тест), 🟠 существенный, 🟡 незначительный; "
          "доказанность — [Д] доказано текстом, [У] вопрос к автору, [Ш] шаблон/оформление.", "",
          "**Характеристики отдельного требования:** " + "; ".join(f"{n} — {d}" for n, _, d in INDIVIDUAL.values()) + ".", ""]
    text = "\n".join(L).rstrip() + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text


def _question_line(i: int, f: Finding) -> list[str]:
    q = f.question
    letters = "абвгд"
    opts = ("  \n   Варианты: " + "; ".join(f"({letters[k]}) {o}" for k, o in enumerate(q.options[:5]))) if q.options else ""
    who = f" · {q.addressee}" if q.addressee else ""
    return [f"{i}. **В-{i:02d}** [{SEV_ICON.get(f.severity, '')} {SEVERITY_RU[f.severity]}{who}] → {f.fid}, {_lines_str(f)}: {q.text}{opts}"]


def write_trace(result: RunResult, path: Path) -> None:
    data = result.model_dump(mode="json")
    data["doc"] = {k: v for k, v in data["doc"].items() if k != "lines"}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def write_source(result: RunResult, path: Path) -> None:
    path.write_text(result.doc.numbered_text() + "\n", encoding="utf-8")
