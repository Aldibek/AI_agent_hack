"""HTML-отчёт в виде заключения: одна колонка, крупная типографика, минимум элементов.

Порядок: вердикт → что исправить → вопросы автору → проверка каждого требования → все замечания (свёрнуты)
→ паспорт прогона → скачать (Markdown, PDF через печать, HTML). Один файл без внешних зависимостей.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .criteria import PROOF_RU, SEVERITY_ORDER, SEVERITY_RU, category_ru
from .pipeline import RunResult
from .verdict import compute

CSS = r"""
:root{--bg:#fbfaf7;--ink:#1b1b1d;--ink-2:#5b5b61;--ink-3:#93939a;--line:#e8e6e0;--soft:#f3f1ec;
  --red:#c8372d;--amber:#c27412;--yellow:#a68a12;--green:#2f7d4f;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#141415;--ink:#ececee;--ink-2:#a9a9b0;--ink-3:#77777e;--line:#2a2a2e;--soft:#1c1c1f;
  --red:#ff7a70;--amber:#f2a65a;--yellow:#e2c55a;--green:#6fcf97}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.65 var(--sans);-webkit-font-smoothing:antialiased}
.page{max-width:740px;margin:0 auto;padding:72px 24px 96px}
@media (max-width:640px){.page{padding:40px 18px 64px}body{font-size:16px}}
h1,h2,h3{font-family:var(--serif);font-weight:600;letter-spacing:-.01em;color:var(--ink)}
.kicker{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3)}
.doc{font-family:var(--serif);font-size:20px;color:var(--ink-2);margin:6px 0 0}
.verdict{font-size:clamp(34px,6vw,52px);line-height:1.08;margin:40px 0 18px}
.verdict em{font-style:normal;color:var(--red)}
.verdict em.warn{color:var(--amber)}.verdict em.ok{color:var(--green)}
.lede{font-size:19px;color:var(--ink-2);margin:0}
.facts{display:flex;flex-wrap:wrap;gap:6px 22px;margin:28px 0 0;padding:16px 0 0;border-top:1px solid var(--line);
  font-size:14px;color:var(--ink-3)}
.facts b{color:var(--ink);font-weight:600}
.s{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.s.blocker{background:var(--red)}.s.major{background:var(--amber)}.s.minor{background:var(--yellow)}.s.ok{background:var(--green)}
.s.unchecked{background:transparent;border:1.5px solid var(--ink-3)}
h2{font-size:28px;margin:84px 0 6px}
.sub{color:var(--ink-3);font-size:15px;margin:0 0 28px}
ol.top{list-style:none;padding:0;margin:0;counter-reset:n}
ol.top>li{counter-increment:n;position:relative;padding:0 0 34px 56px;margin:0 0 34px;border-bottom:1px solid var(--line)}
ol.top>li:last-child{border-bottom:0;margin-bottom:0}
ol.top>li::before{content:counter(n);position:absolute;left:0;top:-4px;font:600 34px/1 var(--serif);color:var(--ink-3)}
@media (max-width:640px){ol.top>li{padding-left:40px}ol.top>li::before{font-size:28px}}
.meta{font-size:13px;color:var(--ink-3);letter-spacing:.02em}
h3{font-size:22px;line-height:1.3;margin:4px 0 12px}
blockquote{margin:12px 0;padding:2px 0 2px 16px;border-left:2px solid var(--line);font:15px/1.55 var(--mono);color:var(--ink-2);
  overflow-wrap:anywhere}
blockquote cite{display:block;font:12px var(--sans);color:var(--ink-3);font-style:normal;margin-top:2px}
.ab{margin:14px 0;font-size:16px}
.ab p{margin:4px 0;overflow-wrap:anywhere}
.ab .who{display:inline-block;min-width:118px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
.fix{margin:12px 0 0;overflow-wrap:anywhere}.fix b{font-weight:600}
ol.q{padding-left:22px;margin:0}
ol.q li{margin:0 0 18px;padding-left:6px}
ol.q .opts{display:block;color:var(--ink-2);font-size:15px;margin-top:2px}
ol.q .to{display:block;color:var(--ink-3);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14.5px}
th{text-align:left;font-weight:500;color:var(--ink-3);font-size:12px;letter-spacing:.08em;text-transform:uppercase;
  padding:0 10px 10px 0;border-bottom:1px solid var(--line)}
td{padding:9px 10px 9px 0;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
td.id{font:13px var(--mono);color:var(--ink-2);white-space:nowrap}
td.st{white-space:nowrap}
details{border-bottom:1px solid var(--line);padding:14px 0}
details>summary{cursor:pointer;list-style:none;display:flex;gap:12px;align-items:baseline}
details>summary::-webkit-details-marker{display:none}
details>summary .fid{font:13px var(--mono);color:var(--ink-3);min-width:40px}
details>summary .t{flex:1}
details>summary::after{content:"+";color:var(--ink-3);font-size:18px}
details[open]>summary::after{content:"–"}
details .body{padding:6px 0 4px 52px;font-size:16px;overflow-wrap:anywhere}
@media (max-width:640px){details .body{padding-left:0}}
details .body p{margin:8px 0}
details .body .k{color:var(--ink-3)}
.passport{font-size:14px;color:var(--ink-2)}
.passport td{padding:7px 10px 7px 0}
.passport td:first-child{color:var(--ink-3);width:38%}
.dl{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px}
.dl button{font:500 15px var(--sans);color:var(--ink);background:transparent;border:1px solid var(--ink);border-radius:999px;
  padding:12px 22px;cursor:pointer;transition:background .15s,color .15s}
.dl button:hover{background:var(--ink);color:var(--bg)}
.dl button:focus-visible{outline:2px solid var(--ink-3);outline-offset:3px}
.fine{margin-top:22px;font-size:13px;color:var(--ink-3)}
.note{font-size:15px;color:var(--ink-2);background:var(--soft);border-radius:10px;padding:12px 16px;margin:16px 0 0}
@media print{
  body{background:#fff;font-size:11pt}.page{padding:0;max-width:none}
  .dl,.noprint{display:none!important}
  h2{margin-top:28pt;break-after:avoid}ol.top>li,details,tr{break-inside:avoid}
  details>summary::after{content:""}details .body{padding-left:0}
}
"""

JS = r"""
(function(){
  function save(name,text,type){const blob=new Blob([text],{type});const a=document.createElement('a');
    a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();
    setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},500);}
  const base=document.body.dataset.base||'report', md=document.getElementById('md-src');
  document.getElementById('dl-md').onclick=()=>save(base+'.md',md?JSON.parse(md.textContent):'','text/markdown;charset=utf-8');
  document.getElementById('dl-html').onclick=()=>save(base+'.html','<!doctype html>\n'+document.documentElement.outerHTML,'text/html;charset=utf-8');
  const pdf=document.getElementById('pdf-src');
  document.getElementById('dl-pdf').onclick=()=>{
    if(pdf){const b=atob(pdf.textContent.trim()),u=new Uint8Array(b.length);for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);
      const blob=new Blob([u],{type:'application/pdf'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);
      a.download=base+'.pdf';document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},500);return;}
    const ds=[...document.querySelectorAll('details')], was=ds.map(d=>d.open);
    ds.forEach(d=>d.open=true); window.print(); ds.forEach((d,i)=>d.open=was[i]);};
})();
"""

SCEN_RE = re.compile(r"^\s*(?:Сценарий:\s*)?(?P<case>.*?)\s*Разработчик\s*А:\s*(?P<a>.*?)\s*Разработчик\s*Б:\s*(?P<b>.*)$", re.S)
SEV_WORD = {"blocker": "блокер", "major": "существенное", "minor": "незначительное"}


def _clean(text: str) -> str:
    return (text or "").replace("**", "").replace("`", "")


def _scenario(scenario: str, e) -> str:
    m = SCEN_RE.match(scenario or "")
    if not m:
        return f"<p>{e(scenario)}</p>" if scenario else ""
    case = m["case"].strip()
    return ("<div class='ab'>" + (f"<p>{e(case)}</p>" if case else "") +
            f"<p><span class='who'>Разработчик А</span>{e(m['a'].strip())}</p>"
            f"<p><span class='who'>Разработчик Б</span>{e(m['b'].strip())}</p></div>")


def _quotes(f, e, limit: int = 2) -> str:
    out = []
    for q in f.quotes[:limit]:
        if q.text:
            out.append(f"<blockquote>{e(_clean(q.text))}<cite>строка {q.line_start}</cite></blockquote>")
    return "".join(out)


def render_html(result: RunResult, out_path: Path) -> None:
    e = html.escape
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
    n_ok = sum(1 for r in reqs if cov.get(r.rid, {}).get("status") == "ok")
    n_def = sum(1 for r in reqs if cov.get(r.rid, {}).get("status") == "defect")
    n_un = len(reqs) - n_ok - n_def
    title = doc.title()
    md_path = out_path.with_suffix(".md")
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    pdf_path = out_path.with_suffix(".pdf")
    pdf_b64 = ""
    if pdf_path.exists() and pdf_path.stat().st_size < 3_000_000:
        import base64

        pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    when = result.finished_at or datetime.now().strftime("%d.%m.%Y %H:%M")

    dev, test = v["dev"][0], v["test"][0]
    verdict = {"Нельзя": "Разработку начинать <em>нельзя</em>.",
               "С оговорками": "Разработку можно начинать <em class='warn'>с оговорками</em>.",
               "Можно": "Документ <em class='ok'>готов</em> к разработке."}[dev]
    test_txt = {"Нельзя": "Тестирование — тоже нельзя", "С оговорками": "Тестирование — с оговорками",
                "Можно": "Тестирование можно начинать"}[test]
    n_block = sev["blocker"]
    lede = f"{test_txt}. " + (
        f"{n_block} {'блокер' if n_block == 1 else ('блокера' if 2 <= n_block <= 4 else 'блокеров')} "
        "не дают однозначно реализовать или проверить ключевое поведение." if n_block else
        "Блокеров нет; существенные замечания стоит закрыть до начала работ." if sev["major"] else
        "Существенных замечаний нет.")

    P: list[str] = []
    P.append("<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
             "<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Заключение — {e(title)}</title><style>{CSS}</style></head>"
             f"<body data-base='{e(out_path.stem)}'><article class='page'>")
    P.append(f"<div class='kicker'>Заключение о проверке требований · ISO/IEC/IEEE 29148 · IEEE 830</div>"
             f"<div class='doc'>{e(title)}</div>"
             f"<h1 class='verdict'>{verdict}</h1><p class='lede'>{e(lede)}</p>")
    facts = [f"<span><b>{len(reqs)}</b> требований</span>",
             f"<span><span class='s blocker'></span><b>{sev['blocker']}</b> блок.</span>",
             f"<span><span class='s major'></span><b>{sev['major']}</b> сущ.</span>",
             f"<span><span class='s minor'></span><b>{sev['minor']}</b> незнач.</span>",
             f"<span>{e(when)}</span>"]
    P.append(f"<div class='facts'>{''.join(facts)}</div>")

    # что исправить
    top = v["fix_first"][:5]
    if top:
        P.append("<h2>Что исправить в первую очередь</h2><p class='sub'>Пять замечаний, от которых зависит начало работ.</p>"
                 "<ol class='top'>")
        for f in top:
            lines = sorted({q.line_start for q in f.quotes if q.line_start})
            where = ("строки " + ", ".join(map(str, lines[:5]))) if len(lines) > 1 else (f"строка {lines[0]}" if lines else "")
            P.append(f"<li><div class='meta'><span class='s {f.severity}'></span>{e(SEV_WORD[f.severity])} · "
                     f"{e(category_ru(f.category).lower())} · {e(where)} · {f.fid}</div>"
                     f"<h3>{e(_clean(f.title))}</h3>{_quotes(f, e)}{_scenario(f.scenario, e)}")
            if f.suggestion:
                P.append(f"<p class='fix'><b>Исправить:</b> {e(_clean(f.suggestion))}</p>")
            P.append("</li>")
        P.append("</ol>")

    # вопросы
    qs = [f for fid in result.priority for f in findings if f.fid == fid and f.question and f.question.text]
    qs += [f for f in sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])
           if f.question and f.question.text and f not in qs]
    if qs:
        P.append("<h2>Вопросы автору</h2><p class='sub'>Решения, без которых требования нельзя закрыть. "
                 "Самые важные — первыми.</p><ol class='q'>")
        for f in qs[:7]:
            opts = " · ".join(e(o) for o in f.question.options[:3])
            P.append(f"<li>{e(f.question.text)}"
                     + (f"<span class='opts'>{opts}</span>" if opts else "")
                     + f"<span class='to'>{e(f.question.addressee or 'Автор')} · {f.fid}</span></li>")
        P.append("</ol>")
        if len(qs) > 7:
            P.append(f"<p class='sub'>Ещё {len(qs) - 7} — в карточках замечаний ниже.</p>")

    # проверка каждого требования
    P.append("<h2>Проверка каждого требования</h2>"
             f"<p class='sub'>С замечаниями — {n_def}, проверено без замечаний — {n_ok}"
             + (f", не проверено — {n_un}" if n_un else "") + ".</p>"
             "<table><tr><th>ID</th><th>В документе</th><th>Итог</th><th>Замечания</th></tr>")
    for r in reqs:
        c = cov.get(r.rid) or {"status": "unchecked", "note": "нет данных о проверке"}
        fs = sorted(per_req.get(r.rid, []), key=lambda f: SEVERITY_ORDER[f.severity])
        if fs:
            st, cls = SEV_WORD[fs[0].severity], fs[0].severity
            notes = ", ".join(f.fid for f in fs)
        elif c["status"] == "ok":
            st, cls, notes = "без замечаний", "ok", e(c.get("note", ""))
        else:
            st, cls, notes = "не проверено", "unchecked", e(c.get("note", ""))
        P.append(f"<tr><td class='id'>{r.rid}</td><td>{e(r.doc_ref or '—')}</td>"
                 f"<td class='st'><span class='s {cls}'></span>{e(st)}</td><td>{notes}</td></tr>")
    P.append("</table>")
    if n_un:
        P.append(f"<p class='note'>{n_un} требований не проверены — по ним вывод о качестве не сделан.</p>")

    # покрытие критериями приёмки
    acc = result.ac_coverage or []
    if acc:
        by_r = {r.rid: r for r in reqs}
        n_cov = sum(1 for x in acc if x["covered_by"])
        P.append("<h2>Покрытие критериями приёмки</h2>"
                 f"<p class='sub'>Критериями приёмки проверяется {n_cov} из {len(acc)} проверяемых требований "
                 f"({100 * n_cov / len(acc):.0f}%).</p>"
                 "<table><tr><th>ID</th><th>В документе</th><th>Критерии</th><th>Что не проверяется</th></tr>")
        for x in acc:
            r = by_r.get(x["rid"])
            cls = "ok" if x["covered_by"] and not x.get("gap") else ("minor" if x["covered_by"] else "blocker")
            P.append(f"<tr><td class='id'>{e(x['rid'])}</td><td>{e((r.doc_ref if r else '') or '—')}</td>"
                     f"<td class='st'><span class='s {cls}'></span>{e(', '.join(x['covered_by']) or 'нет')}</td>"
                     f"<td>{e(x.get('gap') or '—')}</td></tr>")
        P.append("</table>")

    # все замечания
    P.append(f"<h2>Все замечания</h2><p class='sub'>{len(findings)} · нажмите, чтобы раскрыть. "
             "У каждого — цитата документа, проверенная программой.</p>")
    judge_ru = {"confirmed": "подтверждено", "disputed": "спорно", "rejected": "отклонено"}
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.fid)):
        P.append(f"<details><summary><span class='fid'>{f.fid}</span><span class='t'><span class='s {f.severity}'></span>"
                 f"{e(_clean(f.title))}</span></summary><div class='body'>"
                 f"<div class='meta'>{e(SEVERITY_RU[f.severity])} · {e(category_ru(f.category))} · "
                 f"{e(PROOF_RU.get(f.proof, ''))}{' · ' + e(', '.join(f.rids[:4])) if f.rids else ''}</div>"
                 f"{_quotes(f, e, 3)}")
        if f.explanation:
            P.append(f"<p>{e(_clean(f.explanation))}</p>")
        P.append(_scenario(f.scenario, e))
        if f.impact:
            P.append(f"<p><span class='k'>Последствия.</span> {e(_clean(f.impact))}</p>")
        if f.suggestion:
            P.append(f"<p><span class='k'>Исправить.</span> {e(_clean(f.suggestion))}</p>")
        if f.question and f.question.text:
            opts = " · ".join(e(o) for o in f.question.options[:3])
            P.append(f"<p><span class='k'>Вопрос.</span> {e(f.question.text)}{(' — ' + opts) if opts else ''}</p>")
        P.append(f"<p class='meta'>Нашёл: {e(', '.join(f.votes) or '—')} · критик: {e(judge_ru.get(f.judge, 'не проверялось'))}"
                 f"{' — ' + e(f.judge_note) if f.judge_note else ''}</p></div></details>")

    # паспорт
    costs = result.costs or {}
    rows = [("Документ", f"{title} ({doc.name})"), ("Формат", f"{doc.fmt.upper()}, строк {len(doc.lines)}"
                                                   + (f", страниц {doc.pages}" if doc.pages else "")),
            ("SHA-256", doc.sha256[:16] + "…"), ("Дата прогона", when)]
    if result.reviewers:
        rows.append(("Модель-ревьюер", f"{result.reviewers[0].model}, проходов {len(result.reviewer_labels)}"))
    if result.judge:
        rows.append(("Критик", result.judge.model))
    if result.stats.seconds:
        rows.append(("Время", f"{result.stats.seconds / 60:.1f} мин"))
    P.append("<h2>Паспорт прогона</h2><table class='passport'>" +
             "".join(f"<tr><td>{e(k)}</td><td>{e(str(val))}</td></tr>" for k, val in rows) + "</table>")

    # скачать
    P.append("<h2 class='noprint'>Скачать</h2><div class='dl'>"
             "<button id='dl-md'>Markdown</button><button id='dl-pdf'>PDF</button><button id='dl-html'>HTML</button></div>"
             + ("" if pdf_b64 else "<p class='fine noprint'>PDF сохраняется через печать браузера: «Сохранить как PDF».</p>")
             + "<p class='fine'>Отчёт сформирован агентом ReqAudit автоматически.</p></article>")
    # JSON внутри <script> не декодирует HTML-сущности, поэтому Markdown скачается байт в байт
    P.append("<script type='application/json' id='md-src'>" + json.dumps(md_text).replace("</", "<\\/") + "</script>")
    if pdf_b64:
        P.append(f"<script type='text/plain' id='pdf-src'>{pdf_b64}</script>")
    P.append(f"<script>{JS}</script></body></html>")
    out_path.write_text("".join(P), encoding="utf-8")
