"""HTML-отчёт: документ с подсветкой проблемных строк + список замечаний (самодостаточный файл)."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

from .criteria import PROOF_RU, SEVERITY_ORDER, SEVERITY_RU, category_ru
from .pipeline import RunResult
from .verdict import compute

CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e6e5e1;--blocker:#d93025;--major:#e8710a;--minor:#c9a100;
--blocker-bg:#fde8e6;--major-bg:#fef0e1;--minor-bg:#fdf8dc;--accent:#1a5fb4}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--panel:#1f1f22;--ink:#ececee;--muted:#9a9aa1;--line:#2e2e33;
--blocker-bg:#3a1d1b;--major-bg:#3a2814;--minor-bg:#34300f;--accent:#7fb0ff}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:20px 28px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{font-size:20px;margin:0 0 6px}.sub{color:var(--muted);font-size:13px}
.kpis{display:flex;gap:12px;margin-top:14px;flex-wrap:wrap}.kpi{padding:10px 14px;border:1px solid var(--line);border-radius:10px;min-width:120px}
.kpi b{display:block;font-size:20px}.kpi span{color:var(--muted);font-size:12px}
.verdict{margin-top:12px;font-weight:600}.no{color:var(--blocker)}.warn{color:var(--major)}.ok{color:#188038}
main{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:0;height:calc(100vh - 190px)}
@media (max-width:1000px){main{grid-template-columns:1fr;height:auto}}
.doc,.list{overflow:auto;padding:16px 20px}.doc{border-right:1px solid var(--line);background:var(--panel)}
.ln{display:grid;grid-template-columns:44px 1fr;gap:10px;padding:1px 6px;border-radius:4px;white-space:pre-wrap;word-break:break-word;
font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;cursor:default}
.ln .no{color:var(--muted);text-align:right;user-select:none;font-weight:400}
.ln.s-blocker{background:var(--blocker-bg);box-shadow:inset 3px 0 var(--blocker);cursor:pointer}
.ln.s-major{background:var(--major-bg);box-shadow:inset 3px 0 var(--major);cursor:pointer}
.ln.s-minor{background:var(--minor-bg);box-shadow:inset 3px 0 var(--minor);cursor:pointer}
.ln.flash{outline:2px solid var(--accent)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;position:sticky;top:0;background:var(--bg);padding:6px 0}
.filters button{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;padding:4px 10px;cursor:pointer;font-size:12px}
.filters button.on{border-color:var(--accent);color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px;border-left:4px solid var(--minor)}
.card.blocker{border-left-color:var(--blocker)}.card.major{border-left-color:var(--major)}.card.hl{outline:2px solid var(--accent)}
.card h3{font-size:14px;margin:0 0 4px}.meta{font-size:12px;color:var(--muted);margin-bottom:6px}
.card blockquote{margin:6px 0;padding:4px 10px;border-left:2px solid var(--line);color:var(--muted);font-size:12.5px;cursor:pointer}
.card p{margin:5px 0}.q{background:var(--bg);border-radius:8px;padding:8px 10px;margin-top:6px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);margin-right:4px}
"""

JS = """
const F = JSON.parse(document.getElementById('data').textContent);
function go(line){const el=document.getElementById('L'+line); if(!el) return; el.scrollIntoView({block:'center',behavior:'smooth'});
 el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1400);}
function show(fid){const c=document.getElementById(fid); if(!c) return; c.scrollIntoView({block:'start',behavior:'smooth'});
 document.querySelectorAll('.card.hl').forEach(x=>x.classList.remove('hl')); c.classList.add('hl');}
document.querySelectorAll('.ln[data-f]').forEach(el=>el.addEventListener('click',()=>show(el.dataset.f.split(',')[0])));
document.querySelectorAll('blockquote[data-line]').forEach(el=>el.addEventListener('click',()=>go(el.dataset.line)));
let cur='all';
document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{cur=b.dataset.k;
 document.querySelectorAll('.filters button').forEach(x=>x.classList.toggle('on',x===b));
 document.querySelectorAll('.card').forEach(c=>{c.style.display=(cur==='all'||c.dataset.sev===cur||c.dataset.proof===cur)?'':'none';});}));
"""


def render_html(result: RunResult, out_path: Path) -> None:
    doc, findings = result.doc, result.findings
    v = compute(findings, result.reqs)
    by_line: dict[int, list] = defaultdict(list)
    for f in findings:
        for q in f.quotes:
            if q.line_start:
                for n in range(q.line_start, (q.line_end or q.line_start) + 1):
                    by_line[n].append(f)
    e = html.escape
    parts = ["<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>ReqAudit — {e(doc.name)}</title><style>{CSS}</style></head><body>"]
    sev = defaultdict(int)
    for f in findings:
        sev[f.severity] += 1

    def vclass(s):
        return {"Нельзя": "no", "С оговорками": "warn"}.get(s, "ok")

    parts.append(f"<header><h1>Проверка требований: {e(doc.name)}</h1>"
                 f"<div class='sub'>ReqAudit · ISO/IEC/IEEE 29148 · IEEE 830 · строк {len(doc.lines)} · требований {len(result.reqs)}"
                 f"{' · модели: ' + e(', '.join(p.model for p in result.reviewers)) if result.reviewers else ''}</div>"
                 "<div class='kpis'>"
                 f"<div class='kpi'><b>{len(findings)}</b><span>замечаний</span></div>"
                 f"<div class='kpi'><b style='color:var(--blocker)'>{sev['blocker']}</b><span>блокеров</span></div>"
                 f"<div class='kpi'><b style='color:var(--major)'>{sev['major']}</b><span>существенных</span></div>"
                 f"<div class='kpi'><b>{sev['minor']}</b><span>незначительных</span></div>"
                 f"<div class='kpi'><b>{sum(1 for f in findings if f.question)}</b><span>вопросов автору</span></div></div>"
                 f"<div class='verdict'>Разработка: <span class='{vclass(v['dev'][0])}'>{e(v['dev'][0].lower())}</span> · "
                 f"Тестирование: <span class='{vclass(v['test'][0])}'>{e(v['test'][0].lower())}</span></div></header><main>")
    parts.append("<section class='doc'>")
    for ln in doc.lines:
        fs = sorted(by_line.get(ln.n, []), key=lambda f: SEVERITY_ORDER[f.severity])
        if fs:
            ids = ",".join(dict.fromkeys(f.fid for f in fs))
            tip = " | ".join(f"{f.fid}: {f.title}" for f in fs[:4])
            parts.append(f"<div class='ln s-{fs[0].severity}' id='L{ln.n}' data-f='{ids}' title='{e(tip)}'>"
                         f"<span class='no'>{ln.n}</span><span>{e(ln.text.replace('**', '')) or ' '}</span></div>")
        else:
            parts.append(f"<div class='ln' id='L{ln.n}'><span class='no'>{ln.n}</span><span>{e(ln.text.replace('**', '')) or ' '}</span></div>")
    parts.append("</section><section class='list'><div class='filters'>"
                 "<button class='on' data-k='all'>Все</button><button data-k='blocker'>Блокеры</button>"
                 "<button data-k='major'>Существенные</button><button data-k='minor'>Незначительные</button>"
                 "<button data-k='Д'>Доказано [Д]</button><button data-k='У'>Вопросы [У]</button><button data-k='Ш'>Шаблон [Ш]</button></div>")
    for f in findings:
        parts.append(f"<article class='card {f.severity}' id='{f.fid}' data-sev='{f.severity}' data-proof='{e(f.proof)}'>"
                     f"<h3>{f.fid} · {e(f.title)}</h3><div class='meta'><span class='tag'>{e(SEVERITY_RU[f.severity])}</span>"
                     f"<span class='tag'>{e(category_ru(f.category))}</span><span class='tag'>[{e(f.proof)}] {e(PROOF_RU.get(f.proof, ''))}</span>"
                     f"{' · ' + e(', '.join(v for v in f.votes)) if f.votes else ''}</div>")
        for q in f.quotes:
            parts.append(f"<blockquote data-line='{q.line_start}'>строка {q.line_start}: «{e(q.text)}»</blockquote>")
        if f.explanation:
            parts.append(f"<p>{e(f.explanation)}</p>")
        if f.scenario:
            parts.append(f"<p><b>Сценарий:</b> {e(f.scenario)}</p>")
        if f.impact:
            parts.append(f"<p><b>Последствия:</b> {e(f.impact)}</p>")
        if f.suggestion:
            parts.append(f"<p><b>Что сделать:</b> {e(f.suggestion)}</p>")
        if f.question:
            opts = "".join(f"<br>({'абвгд'[i]}) {e(o)}" for i, o in enumerate(f.question.options[:5]))
            parts.append(f"<div class='q'><b>Вопрос{(' · ' + e(f.question.addressee)) if f.question.addressee else ''}:</b> "
                         f"{e(f.question.text)}{opts}</div>")
        if f.judge:
            parts.append(f"<div class='meta' style='margin-top:6px'>Критик: {e({'confirmed': 'подтверждено', 'disputed': 'спорно'}.get(f.judge, f.judge))}"
                         f"{' — ' + e(f.judge_note) if f.judge_note else ''}</div>")
        parts.append("</article>")
    parts.append("</section></main>")
    parts.append(f"<script id='data' type='application/json'>{json.dumps([f.fid for f in findings])}</script><script>{JS}</script></body></html>")
    out_path.write_text("".join(parts), encoding="utf-8")
