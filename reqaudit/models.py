"""Модели данных агента."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Line(BaseModel):
    n: int  # номер строки в извлечённом тексте (1-based)
    text: str
    page: int | None = None
    section: str = ""  # путь раздела: "Часть A > Требования к изменениям > Функциональные требования"


class Document(BaseModel):
    path: str
    name: str
    sha256: str
    fmt: str  # md | txt | pdf | docx
    lines: list[Line]
    pages: int | None = None
    warnings: list[str] = Field(default_factory=list)

    def line(self, n: int) -> Line | None:
        if 1 <= n <= len(self.lines):
            return self.lines[n - 1]
        return None

    def numbered_text(self) -> str:
        width = len(str(len(self.lines)))
        out = []
        for ln in self.lines:
            out.append(f"{str(ln.n).rjust(width)}| {ln.text}")
        return "\n".join(out)

    def where(self, start: int, end: int | None = None) -> str:
        end = end or start
        rng = f"строка {start}" if start == end else f"строки {start}–{end}"
        first = self.line(start)
        if first and first.page:
            rng += f" (с. {first.page} PDF)"
        return rng


class Requirement(BaseModel):
    rid: str  # R-01
    doc_ref: str | None = None  # идентификатор в документе: "Ф-1", "КП-2", "cardId"
    kind: str  # FR, NFR, DATA, INT, UI, GOAL, ARCH, PROC, SEQ, AC, API, REQ
    section: str
    line_start: int
    line_end: int
    text: str

    @property
    def summary(self) -> str:
        t = " ".join(self.text.split())
        return t if len(t) <= 140 else t[:137] + "…"


class Quote(BaseModel):
    text: str
    line: int | None = None  # строка, которую указала модель/правило
    line_start: int | None = None  # строки, найденные проверкой цитат
    line_end: int | None = None
    grounded: bool = False
    match: str = ""  # exact | fuzzy | ""


class Question(BaseModel):
    text: str
    options: list[str] = Field(default_factory=list)
    addressee: str = ""


class Finding(BaseModel):
    fid: str = ""
    source: str  # "rule:http_get" | "llm:gemini" | ...
    category: str  # contradiction | ambiguity | incompleteness | unverifiable | non_singular | incorrect
    #                | terminology | design | template | security | loophole | other
    criterion: str = ""
    severity: str = "minor"  # blocker | major | minor
    proof: str = "У"  # Д (доказано) | У (вопрос / требует решения) | Ш (шаблон / оформление)
    rids: list[str] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    title: str
    explanation: str = ""
    scenario: str = ""
    impact: str = ""
    suggestion: str = ""
    question: Question | None = None
    roles: list[str] = Field(default_factory=list)
    search_scope: str = ""
    votes: list[str] = Field(default_factory=list)  # кто нашёл (источники)
    judge: str = ""  # confirmed | disputed | rejected | ""
    judge_note: str = ""
    status_note: str = ""  # служебные пометки (понижено, найдено одной моделью и т.п.)

    def lines(self) -> set[int]:
        s: set[int] = set()
        for q in self.quotes:
            if q.grounded and q.line_start:
                s.update(range(q.line_start, (q.line_end or q.line_start) + 1))
        return s

    @property
    def is_rule(self) -> bool:
        return self.source.startswith("rule:")


class ProviderInfo(BaseModel):
    name: str
    company: str
    model: str
    base_url: str


class RunStats(BaseModel):
    llm_calls: int = 0
    llm_cached: int = 0
    llm_failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    events: list[str] = Field(default_factory=list)
