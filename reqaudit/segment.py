"""Нарезка документа на отдельные требования (детерминированно, без LLM).

Раздел определяется по заголовкам (банковский шаблон ТЗ: Цели, ФТ, НФТ, Данные, Интеграция,
Критерии приёмки, REST API ...). Единица нарезки — пункт списка, строка таблицы или абзац.
Если в документе нет номеров, агент присваивает свои R-xx и сохраняет цитату и номера строк.
"""

from __future__ import annotations

import re

from .models import Document, Requirement

# (регулярка по названию раздела, тип). Проверяется от самого глубокого заголовка к корню.
SECTION_KINDS: list[tuple[str, str | None]] = [
    (r"нефункциональ|non[- ]?functional|функционалдық емес", "NFR"),
    (r"безопасн|security|защит\w* (?:информ|данн)|қауіпсіз|производительн|performance|нагрузк|надёжн|надежн"
     r"|reliability|доступност|availability|эксплуатац|сопровожд|масштабир", "NFR"),
    (r"функциональн|functional req|функционалдық", "FR"),
    (r"критери\w*\s+при[её]мк|сценари\w*\s+при[её]мк|при[её]мочн|acceptance|тест-кейс|тестов\w+ сценари|қабылдау", "AC"),
    (r"бизнес-требован|business req", "BR"),
    (r"требования к данным|модель данных|\bdata\b", "DATA"),
    (r"интеграц|integration", "INT"),
    (r"^цел[ьи]\b|\bgoals?\b|бизнес-цел|назначени|^\d+[.)]?\s*цел[ьи]\b|\bpurpose\b", "GOAL"),
    (r"дизайн|интерфейс|\bui\b|\bux\b|экран", "UI"),
    (r"архитектур", "ARCH"),
    (r"bpmn|схема процесса|бизнес-процесс", "PROC"),
    (r"sequence|диаграмма последовательност", "SEQ"),
    (r"общие сведения|паспорт|общая информация", "META"),
    (r"ревью требований|чек-лист", "REVIEW"),
    (r"согласующ|согласован", "APPROVAL"),
    (r"тестирован", "TEST"),
    (r"ограничени|constraint|допущени|assumption|шектеу|требовани\w* к|талаптар", "REQ"),
    (r"логика|особые правила|параметр|query|path|request|ответ|статус|response|общие параметры"
     r"|rest|\bapi\b|метод|endpoint|схема", "API"),
]
REQ_KINDS = {"REQ", "FR", "NFR", "AC", "DATA", "INT", "GOAL", "BR", "UI", "ARCH", "PROC", "SEQ", "API"}
NON_REQ_KINDS = {"META", "REVIEW", "APPROVAL", "TEST"}

KIND_RU = {
    "FR": "функциональное", "NFR": "нефункциональное", "AC": "критерий приёмки", "DATA": "данные",
    "INT": "интеграция", "GOAL": "цель", "BR": "бизнес-требование", "UI": "интерфейс", "ARCH": "архитектура", "PROC": "процесс",
    "SEQ": "взаимодействие", "API": "API", "REQ": "требование",
}

LIST_RE = re.compile(r"^\s*(?:[*\-+•]|(\d+(?:\.\d+)+\.?|\d+[.)]))\s+(.*)$")  # «-», «1.», «1)», «2.3», «2.3.1.»
# модальность требования: пункт из нераспознанного раздела остаётся требованием, если он что-то предписывает
MODAL_RE = re.compile(r"(?<!\w)(?:долж(?:ен|на|но|ны)|обязан\w*|обеспечива\w*|обеспечит\w*|необходимо|требуется|не допуска\w*"
                      r"|запрещ\w*|предусм\w*|shall|must|тиіс|қажет|міндетті|қамтамасыз)(?!\w)", re.I)
ID_RE = re.compile(r"^\W*((?:[А-ЯЁA-Z]{1,5})[-–]\d+(?:\.\d+)*)\b")
TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}")


def section_kind(section: str) -> str | None:
    parts = [p.strip().lower() for p in section.split(">") if p.strip()]
    for part in reversed(parts):
        for pattern, kind in SECTION_KINDS:
            if re.search(pattern, part):
                return kind
    return None


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [re.sub(r"[*`]+", "", c).strip() for c in row.split("|")]


def _units(doc: Document):
    """Генерирует (line_start, line_end, text, unit_type, extra) по документу."""
    lines = doc.lines
    i = 0
    n = len(lines)
    while i < n:
        t = lines[i].text
        s = t.strip()
        if not s or s.startswith("#") or s == "---":
            i += 1
            continue
        if s.startswith("```"):
            j = i + 1
            while j < n and not lines[j].text.strip().startswith("```"):
                j += 1
            body = "\n".join(l.text for l in lines[i + 1 : j]).strip()
            if body:
                yield (i + 1, min(j + 1, n), body, "code", None)
            i = j + 1
            continue
        if s.startswith("|"):
            j = i
            while j < n and lines[j].text.strip().startswith("|"):
                j += 1
            rows = list(range(i, j))
            header = _cells(lines[rows[0]].text)
            body_rows = [r for r in rows[1:] if not TABLE_SEP_RE.match(lines[r].text.strip())]
            for k, r in enumerate(body_rows, start=1):
                cells = _cells(lines[r].text)
                yield (r + 1, r + 1, lines[r].text.strip(), "row", {"header": header, "cells": cells, "idx": k})
            i = j
            continue
        m = LIST_RE.match(t)
        if m:
            j = i + 1
            while j < n and lines[j].text.startswith(("   ", "\t")) and lines[j].text.strip() \
                    and not LIST_RE.match(lines[j].text):
                j += 1
            text = " ".join(l.text.strip() for l in lines[i:j])
            yield (i + 1, j, text, "list", {"num": m.group(1)})
            i = j
            continue
        # абзац: подряд идущие строки; строка, начинающаяся с ** или с идентификатора требования, начинает новый абзац
        j = i + 1
        while j < n:
            nt = lines[j].text.strip()
            if not nt or nt.startswith(("#", "|", "```", "**")) or LIST_RE.match(lines[j].text) or nt == "---" \
                    or ID_RE.match(nt):  # строка с собственным идентификатором (Ф-2, FR-3…) — новое требование
                break
            j += 1
        text = " ".join(l.text.strip() for l in lines[i:j])
        yield (i + 1, j, text, "para", None)
        i = j


def _row_text(header: list[str], cells: list[str]) -> str:
    if header and len(header) == len(cells) and any(header):
        parts = [f"{h}: {c}" if h else c for h, c in zip(header, cells) if c]
        return "; ".join(parts)
    return " | ".join(c for c in cells if c)


META_ROW_RE = re.compile(r"^(author|developer|gitlab|framework|автор|разработчик|исполнитель)\b", re.I)
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")


def _section_title(section: str) -> str:
    return section.split(">")[-1].strip()


def segment(doc: Document) -> list[Requirement]:
    units = list(_units(doc))
    kinds = [section_kind(doc.lines[u[0] - 1].section) for u in units]
    structured = any(k in REQ_KINDS for k in kinds)
    reqs: list[Requirement] = []
    for (ls, le, text, utype, extra), kind in zip(units, kinds):
        section = doc.lines[ls - 1].section
        if structured:
            if kind is None and MODAL_RE.search(text):
                kind = "REQ"  # раздел не распознан (не стандартное название), но пункт предписывает — проверяем
            if kind not in REQ_KINDS:
                continue
        else:
            kind = "REQ"
        if utype == "code":
            if kind != "API":
                continue
            # пример (JSON/код) присоединяем к предыдущему пункту того же раздела
            if reqs and reqs[-1].section == section:
                reqs[-1].line_end = le
                reqs[-1].text += f" Пример: {' '.join(text.split())}"
            else:
                reqs.append(Requirement(rid="", doc_ref=None, kind=kind, section=section, line_start=ls,
                                        line_end=le, text=f"{_section_title(section)}. Пример: {' '.join(text.split())}"))
            continue
        if len(WORD_RE.findall(re.sub(r"\*\*|`", "", text))) < 2:
            # «или», «404» и подобные обрывки — присоединяем к предыдущему пункту раздела
            if reqs and reqs[-1].section == section:
                reqs[-1].line_end = max(reqs[-1].line_end, le)
                reqs[-1].text += " " + re.sub(r"\*\*|`", "", text).strip()
            continue
        doc_ref = None
        body = text
        if utype == "row":
            header, cells = extra["header"], extra["cells"]
            if not any(cells):
                continue
            if cells and META_ROW_RE.match(cells[0] or ""):
                continue  # служебные поля (автор, разработчик) — не требования
            body = _row_text(header, cells)
            if kind == "AC":
                doc_ref = None  # в таблице приёмки нет номеров — используем свои R-xx
            elif cells and cells[0] and len(cells[0]) <= 40:
                doc_ref = cells[0]
        elif utype == "list":
            m = LIST_RE.match(text)
            item = m.group(2) if m else text
            idm = ID_RE.match(item)
            if idm:
                doc_ref = idm.group(1).replace("–", "-")
            elif extra and extra.get("num"):
                last = section.split(">")[-1].strip() or "список"
                num = extra["num"].rstrip(".)")
                doc_ref = f"п. {num}" if "." in num else f"{last}, п. {num}"
            body = item
        else:
            idm = ID_RE.match(text)
            if idm:
                doc_ref = idm.group(1).replace("–", "-")
        body = re.sub(r"\*\*|__|`", "", body).strip()
        if len(body) < 3:
            continue
        reqs.append(Requirement(rid="", doc_ref=doc_ref, kind=kind, section=section,
                                line_start=ls, line_end=le, text=body))
    width = max(2, len(str(len(reqs))))
    for i, r in enumerate(reqs, start=1):
        r.rid = f"R-{str(i).zfill(width)}"
    return reqs


def req_for_line(reqs: list[Requirement], line: int) -> Requirement | None:
    for r in reqs:
        if r.line_start <= line <= r.line_end:
            return r
    return None
