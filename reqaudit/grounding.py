"""Проверка цитат кодом: каждая цитата из находки должна реально быть в документе.

Номер строки берётся не со слов модели, а из найденного места. Находка без подтверждённой
цитаты отбрасывается (кроме находок «отсутствия» от правил, у которых указана область поиска).
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .models import Document, Finding, Requirement

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = dict.fromkeys(map(ord, "«»“”„‟\"'`’‘"), "")


def normalize(s: str) -> str:
    s = s.replace(" ", " ").replace(" ", " ").replace("ё", "е").replace("Ё", "Е")
    s = s.translate(_DASHES).translate(_QUOTES)
    s = re.sub(r"[*_#|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.strip(" .,;:")


class Grounder:
    def __init__(self, doc: Document, reqs: list[Requirement]):
        self.doc = doc
        self.reqs = reqs
        self.norm = [normalize(ln.text) for ln in doc.lines]
        self.rid_set = {r.rid for r in reqs}

    def locate(self, quote: str, hint: int | None = None) -> tuple[int, int, str] | None:
        q = normalize(quote)
        if len(q) < 4:
            return None
        n = len(self.norm)
        order = list(range(n))
        if hint and 1 <= hint <= n:
            order.sort(key=lambda i: abs(i + 1 - hint))
        for i in order:
            if q in self.norm[i]:
                return i + 1, i + 1, "exact"
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
                    return i + 1, j + 1, "exact"
        if len(q) < 12:
            return None
        best, best_i = 0.0, -1
        for i in order:
            if not self.norm[i]:
                continue
            score = fuzz.partial_ratio(q, self.norm[i]) if len(self.norm[i]) >= len(q) * 0.6 else 0
            if score > best:
                best, best_i = score, i
        if best >= 90:
            return best_i + 1, best_i + 1, "fuzzy"
        return None

    def ground(self, f: Finding) -> bool:
        """Проверяет цитаты и проставляет строки и требования. Возвращает False, если находку надо отбросить."""
        for q in f.quotes:
            loc = self.locate(q.text, q.line)
            if loc:
                q.line_start, q.line_end, q.match = loc
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
