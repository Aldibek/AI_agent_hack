"""PDF-версия заключения: та же структура, что у HTML, сверстанная средствами PyMuPDF (без браузера и внешних
программ). Кириллица и казахский выводятся встроенными шрифтами Noto.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .criteria import PROOF_RU, SEVERITY_ORDER, SEVERITY_RU, category_ru
from .pipeline import RunResult
from .verdict import compute

CSS = """
body{font-family:sans-serif;font-size:10pt;line-height:1.45;color:#1b1b1d}
h1{font-family:serif;font-size:24pt;margin:6pt 0 6pt 0}
h2{font-family:serif;font-size:15pt;margin:18pt 0 4pt 0}
h3{font-family:serif;font-size:12pt;margin:8pt 0 4pt 0}
p{margin:3pt 0}
.kicker{font-size:7.5pt;color:#8a8a90}
.doc{font-family:serif;font-size:12pt;color:#55555b}
.red{color:#c8372d}.amber{color:#c27412}.yellow{color:#a68a12}.green{color:#2f7d4f}
.muted{color:#8a8a90;font-size:8.5pt}
.lede{font-size:11pt;color:#55555b}
.q{font-family:monospace;font-size:8.5pt;color:#44444a;margin:3pt 0 3pt 10pt}
.who{color:#8a8a90;font-size:8pt}
.row{margin:1pt 0;font-size:9pt}
table{border-collapse:collapse;width:100%}
td,th{font-size:8.5pt;padding:2pt 4pt;border-bottom:0.5pt solid #dddbd4;text-align:left;vertical-align:top}
th{color:#8a8a90;font-weight:normal}
"""

SCEN_RE = re.compile(r"^\s*(?:Сценарий:\s*)?(?P<case>.*?)\s*Разработчик\s*А:\s*(?P<a>.*?)\s*Разработчик\s*Б:\s*(?P<b>.*)$", re.S)
SEV_CLS = {"blocker": "red", "major": "amber", "minor": "yellow"}
SEV_WORD = {"blocker": "блокер", "major": "существенное", "minor": "незначительное"}


def _c(t: str) -> str:
    return html.escape((t or "").replace("**", "").replace("`", ""))


def _scen(s: str) -> str:
    m = SCEN_RE.match(s or "")
    if not m:
        return f"<p>{_c(s)}</p>" if s else ""
    case = m["case"].strip()
    return ((f"<p>{_c(case)}</p>" if case else "") +
            f"<p><span class='who'>РАЗРАБОТЧИК А </span>{_c(m['a'].strip())}</p>"
            f"<p><span class='who'>РАЗРАБОТЧИК Б </span>{_c(m['b'].strip())}</p>")


def _quotes(f, n: int) -> str:
    return "".join(f"<p class='q'>«{_c(q.text)}» — строка {q.line_start}</p>" for q in f.quotes[:n] if q.text)


def build_html(result: RunResult) -> str:
    doc, findings, reqs = result.doc, result.findings, result.reqs
    v = compute(findings, reqs)
    sev = defaultdict(int)
    for f in findings:
        sev[f.severity] += 1
    cov = result.coverage or {}
    per_req: dict[str, list] = defaultdict(list)
    for f in findings:
        for rid in f.rids:
            per_req[rid].append(f)
    title = next((ln.text.lstrip("# ").strip() for ln in doc.lines if ln.text.startswith("# ")), doc.name)
    when = result.finished_at or datetime.now().strftime("%d.%m.%Y %H:%M")
    dev, test = v["dev"][0], v["test"][0]
    verdict = {"Нельзя": "Разработку начинать <span class='red'>нельзя</span>.",
               "С оговорками": "Разработку можно начинать <span class='amber'>с оговорками</span>.",
               "Можно": "Документ <span class='green'>готов</span> к разработке."}[dev]
    test_txt = {"Нельзя": "Тестирование — тоже нельзя.", "С оговорками": "Тестирование — с оговорками.",
                "Можно": "Тестирование можно начинать."}[test]
    H = [f"<p class='kicker'>ЗАКЛЮЧЕНИЕ О ПРОВЕРКЕ ТРЕБОВАНИЙ · ISO/IEC/IEEE 29148 · IEEE 830</p>"
         f"<p class='doc'>{_c(title)}</p><h1>{verdict}</h1><p class='lede'>{test_txt}</p>"
         f"<p class='muted'>{len(reqs)} требований · блокеров {sev['blocker']} · существенных {sev['major']} · "
         f"незначительных {sev['minor']} · {_c(when)}</p>"]
    top = v["fix_first"][:5]
    if top:
        H.append("<h2>Что исправить в первую очередь</h2>")
        for k, f in enumerate(top, start=1):
            H.append(f"<h3>{k}. {_c(f.title)}</h3><p class='muted'><span class='{SEV_CLS[f.severity]}'>"
                     f"{SEV_WORD[f.severity]}</span> · {_c(category_ru(f.category).lower())} · {f.fid}</p>"
                     f"{_quotes(f, 2)}{_scen(f.scenario)}")
            if f.suggestion:
                H.append(f"<p><b>Исправить:</b> {_c(f.suggestion)}</p>")
    qs = [f for fid in result.priority for f in findings if f.fid == fid and f.question and f.question.text]
    qs += [f for f in sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])
           if f.question and f.question.text and f not in qs]
    if qs:
        H.append("<h2>Вопросы автору</h2>")
        for k, f in enumerate(qs[:10], start=1):
            opts = " · ".join(_c(o) for o in f.question.options[:3])
            H.append(f"<p>{k}. {_c(f.question.text)}" + (f" <span class='muted'>({opts})</span>" if opts else "") +
                     f" <span class='muted'>— {_c(f.question.addressee or 'автор')}, {f.fid}</span></p>")
    H.append("<h2>Проверка каждого требования</h2>")
    for r in reqs:
        c = cov.get(r.rid) or {"status": "unchecked", "note": "нет данных о проверке"}
        fs = sorted(per_req.get(r.rid, []), key=lambda f: SEVERITY_ORDER[f.severity])
        if fs:
            st = f"<span class='{SEV_CLS[fs[0].severity]}'>{SEV_WORD[fs[0].severity]}</span>"
            notes = ", ".join(f.fid for f in fs)
        elif c["status"] == "ok":
            st, notes = "<span class='green'>без замечаний</span>", _c(c.get("note", ""))
        else:
            st, notes = "не проверено", _c(c.get("note", ""))
        ref = f" ({_c(r.doc_ref)})" if r.doc_ref else ""
        H.append(f"<p class='row'><b>{r.rid}</b>{ref} — {st}" + (f" <span class='muted'>{notes}</span>" if notes else "") + "</p>")
    H.append("<h2>Все замечания</h2>")
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.fid)):
        H.append(f"<h3>{f.fid}. {_c(f.title)}</h3><p class='muted'><span class='{SEV_CLS[f.severity]}'>"
                 f"{_c(SEVERITY_RU[f.severity])}</span> · {_c(category_ru(f.category))} · {_c(PROOF_RU.get(f.proof, ''))}"
                 f"{' · ' + _c(', '.join(f.rids[:4])) if f.rids else ''}</p>{_quotes(f, 3)}")
        if f.explanation:
            H.append(f"<p>{_c(f.explanation)}</p>")
        H.append(_scen(f.scenario))
        if f.suggestion:
            H.append(f"<p><b>Исправить:</b> {_c(f.suggestion)}</p>")
        if f.question and f.question.text:
            H.append(f"<p><b>Вопрос:</b> {_c(f.question.text)}</p>")
    rows = [("Документ", f"{title} ({doc.name})"), ("SHA-256", doc.sha256[:16] + "…"), ("Дата прогона", when)]
    if result.reviewers:
        rows.append(("Модель-ревьюер", f"{result.reviewers[0].model}, проходов {len(result.reviewer_labels)}"))
    if result.judge:
        rows.append(("Критик", result.judge.model))
    if (result.costs or {}).get("cost_usd") is not None:
        rows.append(("Стоимость", f"${result.costs['cost_usd']:.2f}"))
    H.append("<h2>Паспорт прогона</h2>" +
             "".join(f"<p class='row'><span class='muted'>{_c(k)}:</span> {_c(str(val))}</p>" for k, val in rows) +
             "<p class='muted'>Отчёт сформирован агентом ReqAudit автоматически.</p>")
    return "".join(H)


def render_pdf(result: RunResult, out_path: Path) -> None:
    import pymupdf

    story = pymupdf.Story(html=build_html(result), user_css=CSS)
    writer = pymupdf.DocumentWriter(str(out_path))
    page = pymupdf.paper_rect("a4")
    where = page + (48, 48, -48, -52)
    more, pages = 1, 0
    while more:
        if pages >= 120:  # защита от зацикливания движка вёрстки
            raise RuntimeError("PDF: вёрстка не сходится")
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        pages += 1
    writer.close()
    # номера страниц
    d = pymupdf.open(str(out_path))
    for i, p in enumerate(d, start=1):
        p.insert_text((page.width - 90, page.height - 26), f"{i} / {d.page_count}", fontsize=7.5, color=(0.55, 0.55, 0.58))
    d.subset_fonts()  # только использованные символы: файл в разы меньше
    tmp = out_path.with_suffix(".tmp.pdf")
    d.save(str(tmp), garbage=3, deflate=True)
    d.close()
    tmp.replace(out_path)
