"""Загрузка документа: PDF / DOCX / MD / TXT → Document с пронумерованными строками.

Текст извлекает код, а не модель: так у каждой строки есть точный номер (и страница для PDF),
и цитаты из ответов LLM можно проверить по исходному тексту.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .models import Document, Line

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean_heading(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text).strip()
    return text.rstrip(":").strip()


def _assign_sections(lines: list[Line]) -> None:
    stack: list[tuple[int, str]] = []
    for ln in lines:
        m = HEADING_RE.match(ln.text)
        if m:
            level = len(m.group(1))
            title = _clean_heading(m.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        ln.section = " > ".join(t for _, t in stack)


def _from_text(raw: str, pages: list[int] | None = None) -> list[Line]:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    out = []
    for i, t in enumerate(raw.split("\n"), start=1):
        out.append(Line(n=i, text=t.rstrip(), page=pages[i - 1] if pages else None))
    return out


def _load_pdf(path: Path) -> tuple[list[Line], int, list[str]]:
    import contextlib
    import io

    warnings: list[str] = []
    buf = io.StringIO()
    # разборщик PDF печатает служебные сообщения (OCR и т.п.) — не засоряем вывод агента
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        import pymupdf  # noqa: F401  (pymupdf4llm требует pymupdf)
        import pymupdf4llm

        chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True, show_progress=False)
    texts: list[str] = []
    pages: list[int] = []
    for idx, ch in enumerate(chunks, start=1):
        meta = ch.get("metadata", {}) if isinstance(ch, dict) else {}
        page_no = meta.get("page") or meta.get("page_number") or idx
        text = ch.get("text", "") if isinstance(ch, dict) else str(ch)
        text = re.sub(r"<br\s*/?>", " ", text)
        page_lines = text.replace("\r", "").split("\n")
        texts.extend(page_lines)
        pages.extend([int(page_no)] * len(page_lines))
    n_pages = len(chunks)
    total_chars = sum(len(t.strip()) for t in texts)
    if n_pages and total_chars < 80 * n_pages:
        warnings.append(
            "В PDF почти нет текстового слоя (похоже на скан). Текст извлечён не полностью; "
            "для сканов нужен OCR — результаты проверки неполные."
        )
    lines = [Line(n=i, text=t.rstrip(), page=p) for i, (t, p) in enumerate(zip(texts, pages), start=1)]
    return lines, n_pages, warnings


def _load_docx(path: Path) -> list[Line]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    out: list[str] = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                out.append("")
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.search(r"(Heading|Заголовок)\s*(\d)", style)
            if m:
                out.append("#" * int(m.group(2)) + " " + text)
            elif "List" in style or "Спис" in style:
                out.append("* " + text)
            else:
                out.append(text)
        elif tag == "tbl":
            t = Table(child, d)
            for r_i, row in enumerate(t.rows):
                cells = [" ".join(c.text.split()) for c in row.cells]
                out.append("| " + " | ".join(cells) + " |")
                if r_i == 0:
                    out.append("|" + "|".join([" --- "] * len(cells)) + "|")
            out.append("")
    return _from_text("\n".join(out))


def load_document(path: str | Path) -> Document:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {p}")
    ext = p.suffix.lower()
    warnings: list[str] = []
    pages = None
    if ext == ".pdf":
        fmt = "pdf"
        lines, pages, warnings = _load_pdf(p)
    elif ext == ".docx":
        fmt = "docx"
        lines = _load_docx(p)
    elif ext in (".md", ".markdown", ".txt", ".text", ""):
        fmt = "md" if ext in (".md", ".markdown") else "txt"
        lines = _from_text(p.read_text(encoding="utf-8", errors="replace"))
    else:
        raise ValueError(f"Неподдерживаемый формат: {ext} (поддерживаются .pdf, .docx, .md, .txt)")
    _assign_sections(lines)
    if not any(ln.text.strip() for ln in lines):
        warnings.append("Документ пустой или текст не извлечён.")
    return Document(path=str(p), name=p.name, sha256=_sha256(p), fmt=fmt, lines=lines, pages=pages, warnings=warnings)
