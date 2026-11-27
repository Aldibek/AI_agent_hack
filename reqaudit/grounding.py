"""Проверка цитат кодом: каждая цитата из находки должна реально быть в документе.

Номер строки берётся не со слов модели, а из найденного места. Находка без подтверждённой
цитаты отбрасывается (кроме находок «отсутствия» от правил, у которых указана область поиска).
"""

from __future__ import annotations

import re

from difflib import SequenceMatcher

from rapidfuzz import fuzz

from .models import Document, Finding, Requirement

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = dict.fromkeys(map(ord, "«»“”„‟\"'`’‘"), "")


def _light(s: str) -> str:
    """Нормализация без смены регистра: индексы совпадают с normalize()."""
    s = s.replace("\u00a0", " ").replace("\u202f", " ").replace("ё", "е").replace("Ё", "Е")
    s = s.translate(_DASHES).translate(_QUOTES)
    s = re.sub(r"[*_#|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" .,;:")


def normalize(s: str) -> str:
    return _light(s).lower()


_WORD = re.compile(r"\w+")


def _same_word(a: str, b: str) -> bool:
    """Та же словоформа с точностью до окончания/опечатки; числа — только точно."""
    if a == b:
        return True
    if a.isdigit() or b.isdigit() or any(c.isdigit() for c in a + b):
        return False
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k >= 3 and k >= 0.6 * min(len(a), len(b))


def _same_meaning(quote: str, span: str) -> bool:
    """Нечёткая цитата допустима, только если слова совпадают по смыслу: окончания и опечатки — да,
    подмена слова («совершения» → «проведения», «один» → «два») или числа — нет."""
    qa, sa = _WORD.findall(quote), _WORD.findall(span)
    extra = 0
    for op, i1, i2, j1, j2 in SequenceMatcher(a=qa, b=sa, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "replace" and i2 - i1 == j2 - j1:
            if not all(_same_word(x, y) for x, y in zip(qa[i1:i2], sa[j1:j2])):
                return False
        else:
            extra += max(i2 - i1, j2 - j1)
    return extra <= 1


class Grounder:
    def __init__(self, doc: Document, reqs: list[Requirement]):
        self.doc = doc
        self.reqs = reqs
        self.light = [_light(ln.text) for ln in doc.lines]
        self.norm = [x.lower() for x in self.light]
        self.rid_set = {r.rid for r in reqs}

    def locate(self, quote: str, hint: int | None = None) -> tuple[int, int, str] | None:
        hit = self.find(quote, hint)
        return hit[:3] if hit else None

    def find(self, quote: str, hint: int | None = None) -> tuple[int, int, str, str] | None:
        """(строка начала, строка конца, exact|fuzzy, текст цитаты из документа) или None."""
        q = normalize(quote)
        if not q:
            return None
        n = len(self.norm)
        order = list(range(n))
        if hint and 1 <= hint <= n:
            order.sort(key=lambda i: abs(i + 1 - hint))
        if len(q) < 4:
            # короткая ячейка таблицы («GET», «404», «KZT»): только целым словом и только рядом с указанной строкой,
            # иначе слово привяжется к случайному вхождению
            if not hint or not 1 <= hint <= n:
                return None
            pat = re.compile(rf"(?<!\w){re.escape(q)}(?!\w)")
            for i in range(max(0, hint - 3), min(n, hint + 2)):
                if pat.search(self.norm[i]):
                    return i + 1, i + 1, "exact", quote
            return None
        for i in order:
            if q in self.norm[i]:
                return i + 1, i + 1, "exact", quote
        # цитата через несколько строк (абзац, перенос в PDF)
        for i in order:
            if not self.norm[i]:
                continue
            joined = self.norm[i]
            for j in range(i + 1, min(i + 6, n)):
                if not self.norm[j]:
                    continue
                joined = f"{joined} {self.norm[j]}"
                if q in joined:
                    return i + 1, j + 1, "exact", quote
        if len(q) < 12:
            return None
        best, best_i, best_span = 0.0, -1, ""
        for i in order:
            if not self.norm[i] or len(self.norm[i]) < len(q) * 0.6:
                continue
            al = fuzz.partial_ratio_alignment(q, self.norm[i])
            if al and al.score > best:
                span = self.light[i][al.dest_start:al.dest_end]
                if _same_meaning(q, span.lower()):
                    best, best_i, best_span = al.score, i, span
        if best >= 90:
            # в отчёт идёт текст документа, а не пересказ модели
            return best_i + 1, best_i + 1, "fuzzy", best_span.strip()
        return None

    def ground(self, f: Finding) -> bool:
        """Проверяет цитаты и проставляет строки и требования. Возвращает False, если находку надо отбросить."""
        for q in f.quotes:
            loc = self.find(q.text, q.line)
            if loc:
                q.line_start, q.line_end, q.match, q.text = loc
                q.grounded = True
            else:
                q.grounded = False
        grounded = [q for q in f.quotes if q.grounded]
        if not grounded:
            return bool(f.is_rule and f.search_scope)
        f.quotes = grounded
        # требования: проверяем названные моделью и добавляем по строкам цитат
        rids = [r for r in f.rids if r in self.rid_set]
        for q in grounded:
            for r in self.reqs:
                if r.line_start <= (q.line_end or q.line_start) and q.line_start <= r.line_end and r.rid not in rids:
                    rids.append(r.rid)
        f.rids = sorted(set(rids))
        if f.category == "contradiction":
            distinct = {q.line_start for q in grounded}
            if len(distinct) < 2 and f.is_rule is False:
                f.proof = "У"
                f.status_note = (f.status_note + " " if f.status_note else "") + \
                    "Подтверждена только одна сторона противоречия — понижено до вопроса."
        return True
