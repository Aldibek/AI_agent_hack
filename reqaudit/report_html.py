"""HTML-отчёт: один самодостаточный файл без внешних зависимостей.

Первый экран — вердикт, карта требований и что исправить первым; ниже — документ с подсветкой строк рядом
с карточками замечаний; внизу — скачивание отчёта в Markdown, PDF (через печать браузера) и HTML.
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
:root{
  --bg:#f6f5f1;--surface:#ffffff;--surface-2:#fbfaf7;--ink:#17171a;--ink-2:#55555c;--ink-3:#8b8b92;--line:#e7e5df;
  --accent:#3d5afe;--accent-soft:#eef1ff;
  --red:#d6352b;--red-soft:#fdecea;--amber:#d9790f;--amber-soft:#fdf1e2;--yellow:#b38f00;--yellow-soft:#fbf6dc;
  --green:#1e8a4c;--green-soft:#e6f4ec;--grey-soft:#efeee9;
  --radius:14px;--shadow:0 1px 2px rgba(20,20,30,.04),0 6px 24px rgba(20,20,30,.06);
  --mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","Inter",Roboto,"Helvetica Neue",Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#111113;--surface:#19191c;--surface-2:#1e1e22;--ink:#ededf0;--ink-2:#b4b4bb;--ink-3:#7c7c84;--line:#2b2b30;
  --accent:#8b9dff;--accent-soft:#1f2440;--red:#ff6b61;--red-soft:#3a1c1a;--amber:#ffa94d;--amber-soft:#392614;
  --yellow:#e8c547;--yellow-soft:#332d12;--green:#4cc47f;--green-soft:#15301f;--grey-soft:#24242a;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 28px rgba(0,0,0,.35);}}
:root[data-theme="dark"]{
  --bg:#111113;--surface:#19191c;--surface-2:#1e1e22;--ink:#ededf0;--ink-2:#b4b4bb;--ink-3:#7c7c84;--line:#2b2b30;
  --accent:#8b9dff;--accent-soft:#1f2440;--red:#ff6b61;--red-soft:#3a1c1a;--amber:#ffa94d;--amber-soft:#392614;
  --yellow:#e8c547;--yellow-soft:#332d12;--green:#4cc47f;--green-soft:#15301f;--grey-soft:#24242a;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 28px rgba(0,0,0,.35);}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
.wrap{max-width:1240px;margin:0 auto;padding:0 24px}
@media (max-width:640px){.wrap{padding:0 16px}}

/* верхняя полоса */
.top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 86%,transparent);
  backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
.top .wrap{display:flex;align-items:center;gap:14px;height:56px}
.brand{font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.brand i{width:10px;height:10px;border-radius:3px;background:var(--accent);display:inline-block;transform:rotate(45deg)}
.top .doc-name{color:var(--ink-2);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.top nav{display:flex;gap:4px}
.top nav a{color:var(--ink-2);text-decoration:none;font-size:13px;padding:6px 10px;border-radius:8px}
.top nav a:hover{background:var(--surface);color:var(--ink)}
@media (max-width:820px){.top nav{display:none}}
.icon-btn{border:1px solid var(--line);background:var(--surface);color:var(--ink-2);border-radius:10px;height:34px;min-width:34px;
  cursor:pointer;font-size:15px}

/* герой */
.hero{padding:44px 0 26px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);font-weight:600}
.verdict{font-size:clamp(28px,4.2vw,46px);line-height:1.08;letter-spacing:-.025em;font-weight:750;margin:10px 0 14px;max-width:900px}
.verdict .no{color:var(--red)}.verdict .warn{color:var(--amber)}.verdict .ok{color:var(--green)}
.verdict-sub{color:var(--ink-2);font-size:16px;max-width:760px}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--line);border-radius:999px;
  padding:5px 12px;font-size:13px;color:var(--ink-2)}
.pill b{color:var(--ink)}

/* сетка показателей */
.stats{display:grid;grid-template-columns:1.25fr 1fr;gap:16px;margin-top:26px}
@media (max-width:900px){.stats{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:20px 22px}
.panel h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);margin:0 0 14px;font-weight:650}
.sevbar{display:flex;height:12px;border-radius:999px;overflow:hidden;background:var(--grey-soft)}
.sevbar span{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin-top:14px;font-size:14px}
.legend div{display:flex;align-items:baseline;gap:8px}
.legend b{font-size:26px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.legend small{color:var(--ink-3)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.cats{margin-top:20px;display:grid;gap:8px}
.cat{display:grid;grid-template-columns:150px 1fr 28px;align-items:center;gap:10px;font-size:13px;color:var(--ink-2)}
.cat i{height:8px;border-radius:999px;background:var(--grey-soft);overflow:hidden;display:block}
.cat u{display:block;height:100%;background:var(--accent);border-radius:999px;opacity:.75}
.cat b{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink)}

/* карта требований */
.reqmap{display:grid;grid-template-columns:repeat(auto-fill,minmax(30px,1fr));gap:5px}
.cell{aspect-ratio:1;border-radius:7px;border:1px solid var(--line);background:var(--surface-2);display:flex;align-items:center;
  justify-content:center;font:600 10px/1 var(--mono);color:var(--ink-3);cursor:pointer;transition:transform .12s ease}
.cell:hover{transform:translateY(-2px) scale(1.06)}
.cell.blocker{background:var(--red-soft);border-color:color-mix(in srgb,var(--red) 45%,transparent);color:var(--red)}
.cell.major{background:var(--amber-soft);border-color:color-mix(in srgb,var(--amber) 45%,transparent);color:var(--amber)}
.cell.minor{background:var(--yellow-soft);border-color:color-mix(in srgb,var(--yellow) 45%,transparent);color:var(--yellow)}
.cell.ok{background:var(--green-soft);border-color:color-mix(in srgb,var(--green) 35%,transparent);color:var(--green)}
.cell.unchecked{background:repeating-linear-gradient(45deg,var(--surface-2),var(--surface-2) 4px,var(--grey-soft) 4px,var(--grey-soft) 8px)}
.map-note{font-size:13px;color:var(--ink-2);margin-top:12px}

/* раздел */
section.block{padding:30px 0 6px}
.block-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}
.block-head h2{font-size:24px;letter-spacing:-.02em;margin:0}
.block-head p{margin:0;color:var(--ink-3);font-size:14px}

/* топ-исправления */
.fix{display:grid;grid-template-columns:44px 1fr;gap:14px;padding:18px 20px;border-radius:var(--radius);background:var(--surface);
  border:1px solid var(--line);box-shadow:var(--shadow);margin-bottom:12px}
.fix .num{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-weight:750;font-size:18px}
.fix.blocker .num{background:var(--red-soft);color:var(--red)}
.fix.major .num{background:var(--amber-soft);color:var(--amber)}
.fix.minor .num{background:var(--yellow-soft);color:var(--yellow)}
.fix h3{margin:2px 0 6px;font-size:17px;letter-spacing:-.01em}
.fix .where{font-size:13px;color:var(--ink-3)}
.quote{font:13.5px/1.5 var(--mono);background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:8px 12px;
  margin:10px 0;color:var(--ink-2);cursor:pointer;position:relative}
.quote::before{content:attr(data-ln);position:absolute;top:-9px;left:10px;background:var(--surface);border:1px solid var(--line);
  border-radius:6px;padding:0 6px;font:600 10.5px/16px var(--sans);color:var(--ink-3)}
.quote:hover{border-color:var(--accent)}
.duel{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}
@media (max-width:640px){.duel{grid-template-columns:1fr}}
.duel .case{grid-column:1/-1;font-size:14px;color:var(--ink-2)}
.duel .dev{border-radius:10px;padding:10px 12px;font-size:14px;border:1px dashed var(--line)}
.duel .dev b{display:block;font-size:11px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:4px}
.duel .a{background:var(--accent-soft)}.duel .a b{color:var(--accent)}
.duel .b{background:var(--amber-soft)}.duel .b b{color:var(--amber)}
.fixit{font-size:14px;margin-top:8px}.fixit b{color:var(--green)}

/* вопросы */
.qgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.qcard{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px;box-shadow:var(--shadow)}
.qcard .to{font-size:12px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.qcard p{margin:6px 0 10px;font-weight:600}
.opt{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 10px;margin:0 6px 6px 0;font-size:13px;color:var(--ink-2)}

/* рабочая зона: документ + замечания */
.work{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}
@media (max-width:1000px){.work{grid-template-columns:1fr}}
.pane{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.pane-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:12px 14px;border-bottom:1px solid var(--line);
  background:var(--surface-2)}
.pane-head b{font-size:14px;margin-right:auto}
.scroll{max-height:78vh;overflow:auto}
@media (max-width:1000px){.scroll{max-height:none}}
.ln{display:grid;grid-template-columns:46px 1fr;gap:10px;padding:1px 12px 1px 0;font:12.8px/1.65 var(--mono);white-space:pre-wrap;
  word-break:break-word;border-left:3px solid transparent}
.ln .n{color:var(--ink-3);text-align:right;user-select:none}
.ln.s-blocker{background:var(--red-soft);border-left-color:var(--red);cursor:pointer}
.ln.s-major{background:var(--amber-soft);border-left-color:var(--amber);cursor:pointer}
.ln.s-minor{background:var(--yellow-soft);border-left-color:var(--yellow);cursor:pointer}
.ln.flash{outline:2px solid var(--accent);outline-offset:-2px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--ink-2);border-radius:999px;padding:4px 11px;font-size:12.5px;cursor:pointer}
.chip.on{background:var(--ink);color:var(--surface);border-color:var(--ink)}
.search{flex:1 1 160px;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:10px;padding:6px 10px;font-size:13px}
.cards{padding:12px}
.card{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px;background:var(--surface);position:relative}
.card::before{content:"";position:absolute;left:-1px;top:12px;bottom:12px;width:3px;border-radius:3px;background:var(--yellow)}
.card.blocker::before{background:var(--red)}.card.major::before{background:var(--amber)}
.card.hl{outline:2px solid var(--accent)}
.card h4{margin:0 0 6px;font-size:15px;letter-spacing:-.01em}
.card .fid{font:600 12px var(--mono);color:var(--ink-3);margin-right:6px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.tag{font-size:11.5px;padding:2px 8px;border-radius:999px;background:var(--grey-soft);color:var(--ink-2)}
.tag.blocker{background:var(--red-soft);color:var(--red)}.tag.major{background:var(--amber-soft);color:var(--amber)}
.tag.minor{background:var(--yellow-soft);color:var(--yellow)}
.card p{margin:6px 0;font-size:14px}.card p b{color:var(--ink-2);font-weight:600}
.verdict-line{font-size:12.5px;color:var(--ink-3);margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
.empty{padding:30px;text-align:center;color:var(--ink-3)}

/* длинные строки без пробелов (JSON, URL) не должны распирать колонки */
.fix>div,.work>*,.stats>*,.qgrid>*,.duel>*{min-width:0}
.quote,.card,.card p,.duel .dev,.duel .case,.qcard p,.verdict-sub,.fixit{overflow-wrap:anywhere}
@media (max-width:640px){
  .top .doc-name{display:none}
  .fix{grid-template-columns:1fr;padding:16px}
  .fix .num{width:36px;height:36px;font-size:16px}
  .cat{grid-template-columns:110px 1fr 24px}
  .block-head{flex-direction:column;align-items:flex-start;gap:2px}
  .legend b{font-size:22px}
}

/* подвал и скачивание */
footer{margin:46px 0 0;padding:34px 0 40px;border-top:1px solid var(--line)}
.dl{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media (max-width:700px){.dl{grid-template-columns:1fr}}
.dl button{all:unset;cursor:pointer;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;
  box-shadow:var(--shadow);display:flex;align-items:center;gap:14px;transition:transform .12s ease,border-color .12s ease}
.dl button:hover{transform:translateY(-2px);border-color:var(--accent)}
.dl button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.dl .ext{width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;font:700 13px var(--mono);
  background:var(--accent-soft);color:var(--accent)}
.dl .t b{display:block;font-size:15px}.dl .t small{color:var(--ink-3);font-size:13px}
.fine{color:var(--ink-3);font-size:12.5px;margin-top:18px;line-height:1.6}

/* печать → PDF */
@media print{
  :root{--bg:#fff;--surface:#fff;--surface-2:#fafafa;--shadow:none}
  body{font-size:11.5pt}
  .top,.dl,.pane-head .chip,.pane-head .search,.icon-btn,.no-print{display:none!important}
  .hero{padding:0 0 10px}.verdict{font-size:26pt}
  .stats,.work{display:block}.panel,.pane,.fix,.qcard,.card{box-shadow:none;break-inside:avoid;margin-bottom:10px}
  .scroll{max-height:none;overflow:visible}
  .pane.doc-pane{display:none}
  .card{display:block!important}
  section.block{padding-top:14px;break-before:auto}
  a{color:inherit;text-decoration:none}
}
"""

JS = r"""
(function(){
  const root=document.documentElement;
  try{const t=localStorage.getItem('reqaudit-theme'); if(t) root.dataset.theme=t;}catch(e){}
  const tb=document.getElementById('theme');
  if(tb) tb.addEventListener('click',()=>{
    const dark=root.dataset.theme? root.dataset.theme==='dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme=dark?'light':'dark';
    try{localStorage.setItem('reqaudit-theme',root.dataset.theme);}catch(e){}
  });
  function go(line){const el=document.getElementById('L'+line); if(!el) return;
    el.scrollIntoView({block:'center',behavior:'smooth'}); el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1500);}
  function show(fid){const c=document.getElementById(fid); if(!c) return; resetFilter();
    c.scrollIntoView({block:'center',behavior:'smooth'});
    document.querySelectorAll('.card.hl').forEach(x=>x.classList.remove('hl')); c.classList.add('hl');}
  document.querySelectorAll('[data-line]').forEach(el=>el.addEventListener('click',()=>go(el.dataset.line)));
  document.querySelectorAll('.ln[data-f]').forEach(el=>el.addEventListener('click',()=>show(el.dataset.f.split(',')[0])));
  document.querySelectorAll('.cell[data-go]').forEach(el=>el.addEventListener('click',()=>{
    const f=el.dataset.f; if(f) show(f.split(',')[0]); else go(el.dataset.go);}));
  let cur='all', q='';
  const cards=[...document.querySelectorAll('.card')], empty=document.getElementById('empty');
  function apply(){let n=0; cards.forEach(c=>{const okK=cur==='all'||c.dataset.sev===cur||c.dataset.proof===cur;
      const okQ=!q||c.textContent.toLowerCase().includes(q); const vis=okK&&okQ; c.style.display=vis?'':'none'; if(vis) n++;});
    if(empty) empty.style.display=n?'none':'';}
  function resetFilter(){cur='all';q='';const s=document.getElementById('search'); if(s) s.value='';
    document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('on',x.dataset.k==='all')); apply();}
  document.querySelectorAll('.chip').forEach(b=>b.addEventListener('click',()=>{cur=b.dataset.k;
    document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('on',x===b)); apply();}));
  const s=document.getElementById('search'); if(s) s.addEventListener('input',()=>{q=s.value.trim().toLowerCase(); apply();});
  function save(name,text,type){const blob=new Blob([text],{type}); const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download=name; document.body.appendChild(a); a.click();
    setTimeout(()=>{URL.revokeObjectURL(a.href); a.remove();},500);}
  const base=document.body.dataset.base||'report';
  const md=document.getElementById('md-src');
  document.getElementById('dl-md').addEventListener('click',()=>save(base+'.md',md?JSON.parse(md.textContent):'','text/markdown;charset=utf-8'));
  document.getElementById('dl-html').addEventListener('click',()=>save(base+'.html','<!doctype html>\n'+root.outerHTML,'text/html;charset=utf-8'));
  document.getElementById('dl-pdf').addEventListener('click',()=>{resetFilter(); window.print();});
})();
"""

SEV_CLASS = {"blocker": "blocker", "major": "major", "minor": "minor"}
VCLASS = {"Нельзя": "no", "С оговорками": "warn", "Можно": "ok"}
SCEN_RE = re.compile(r"^\s*(?:Сценарий:\s*)?(?P<case>.*?)\s*Разработчик\s*А:\s*(?P<a>.*?)\s*Разработчик\s*Б:\s*(?P<b>.*)$", re.S)


def _duel(scenario: str, e) -> str:
    """Сценарий «два разработчика» — как сравнение двух реализаций бок о бок."""
    m = SCEN_RE.match(scenario or "")
    if not m:
        return f"<p><b>Сценарий:</b> {e(scenario)}</p>" if scenario else ""
    return ("<div class='duel'>"
            f"<div class='case'>{e(m['case'].strip())}</div>"
            f"<div class='dev a'><b>Разработчик А</b>{e(m['a'].strip())}</div>"
            f"<div class='dev b'><b>Разработчик Б</b>{e(m['b'].strip())}</div></div>")


def _categories(findings, e) -> str:
    """Топ типов дефектов — горизонтальные полоски."""
    cnt: dict[str, int] = defaultdict(int)
    for f in findings:
        cnt[category_ru(f.category)] += 1
    if not cnt:
        return ""
    top = sorted(cnt.items(), key=lambda kv: -kv[1])[:6]
    mx = top[0][1]
    rows = "".join(f"<div class='cat'><span>{e(k)}</span><i><u style='width:{100 * n / mx:.0f}%'></u></i><b>{n}</b></div>"
                   for k, n in top)
    return f"<div class='cats'>{rows}</div>"


def render_html(result: RunResult, out_path: Path) -> None:
    e = html.escape
    doc, findings, reqs = result.doc, result.findings, result.reqs
    v = compute(findings, reqs)
    by_line: dict[int, list] = defaultdict(list)
    for f in findings:
        for q in f.quotes:
            if q.line_start:
                for n in range(q.line_start, (q.line_end or q.line_start) + 1):
                    by_line[n].append(f)
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
    title = next((ln.text.lstrip("# ").strip() for ln in doc.lines if ln.text.startswith("# ")), doc.name)
    md_path = out_path.with_suffix(".md")
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    dev, test = v["dev"][0], v["test"][0]
    if dev == "Нельзя":
        head = "Разработку по этому документу <span class='no'>начинать нельзя</span>"
    elif dev == "С оговорками":
        head = "Разработку можно начинать <span class='warn'>с оговорками</span>"
    else:
        head = "Документ <span class='ok'>готов к разработке</span>"
    test_txt = {"Нельзя": "тестирование тоже начинать нельзя", "С оговорками": "тестирование — с оговорками",
                "Можно": "тестирование можно начинать"}[test]
    reasons = v["dev"][1][:3] or v["test"][1][:3]
    sub = f"{test_txt[0].upper() + test_txt[1:]}." + (
        f" Главные причины: {', '.join(e(f.title.rstrip('.')) for f in reasons)}." if reasons else "")

    P: list[str] = []
    P.append("<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
             "<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Проверка требований — {e(title)}</title><style>{CSS}</style></head>"
             f"<body data-base='{e(out_path.stem)}'>")
    # верхняя полоса
    P.append("<div class='top'><div class='wrap'><div class='brand'><i></i>ReqAudit</div>"
             f"<div class='doc-name'>{e(title)} · {e(doc.name)}</div>"
             "<nav><a href='#first'>Главное</a><a href='#questions'>Вопросы</a><a href='#work'>Документ</a>"
             "<a href='#download'>Скачать</a></nav>"
             "<button class='icon-btn' id='theme' title='Светлая / тёмная тема' aria-label='Сменить тему'>◐</button>"
             "</div></div><main class='wrap'>")
    # герой
    costs = result.costs or {}
    cost = costs.get("cost_usd")
    pills = [f"<span class='pill'>Требований <b>{len(reqs)}</b></span>",
             f"<span class='pill'>Строк <b>{len(doc.lines)}</b>{f' · страниц <b>{doc.pages}</b>' if doc.pages else ''}</span>"]
    if result.reviewers:
        pills.append(f"<span class='pill'>Модель <b>{e(result.reviewers[0].model)}</b></span>")
    if result.stats.seconds:
        pills.append(f"<span class='pill'>Прогон <b>{result.stats.seconds / 60:.1f} мин</b></span>")
    if cost:
        pills.append(f"<span class='pill'>Стоимость <b>${cost:.2f}</b></span>")
    pills.append(f"<span class='pill'>{e(result.finished_at or datetime.now().strftime('%d.%m.%Y %H:%M'))}</span>")
    P.append("<section class='hero' id='first'><div class='eyebrow'>Статическая проверка требований · ISO/IEC/IEEE 29148 · IEEE 830</div>"
             f"<div class='verdict'>{head}</div><div class='verdict-sub'>{sub}</div>"
             f"<div class='pills'>{''.join(pills)}</div>")
    # показатели: тяжесть + карта требований
    total = max(len(findings), 1)
    bar = "".join(f"<span style='width:{100 * sev[k] / total:.2f}%;background:var(--{c})'></span>"
                  for k, c in (("blocker", "red"), ("major", "amber"), ("minor", "yellow")) if sev[k])
    P.append("<div class='stats'><div class='panel'><h2>Замечания</h2>"
             f"<div class='sevbar'>{bar}</div><div class='legend'>"
             f"<div><span class='dot' style='background:var(--red)'></span><b>{sev['blocker']}</b><small>блокеров</small></div>"
             f"<div><span class='dot' style='background:var(--amber)'></span><b>{sev['major']}</b><small>существенных</small></div>"
             f"<div><span class='dot' style='background:var(--yellow)'></span><b>{sev['minor']}</b><small>незначительных</small></div>"
             f"<div><b>{sum(1 for f in findings if f.question and f.question.text)}</b><small>вопросов автору</small></div>"
             "</div>" + _categories(findings, e) + "</div>")
    cells = []
    for r in reqs:
        st = cov.get(r.rid, {}).get("status", "unchecked")
        fs = sorted(per_req.get(r.rid, []), key=lambda f: SEVERITY_ORDER[f.severity])
        cls = fs[0].severity if fs else ("ok" if st == "ok" else ("unchecked" if st != "defect" else "minor"))
        tip = f"{r.rid}{' (' + r.doc_ref + ')' if r.doc_ref else ''}: " + (
            "; ".join(f"{f.fid} {f.title}" for f in fs[:3]) if fs else cov.get(r.rid, {}).get("note", "не проверено"))
        ids = ",".join(f.fid for f in fs)
        cells.append(f"<div class='cell {cls}' title='{e(tip)}' data-go='{r.line_start}' data-f='{ids}'>"
                     f"{e(r.rid.replace('R-', ''))}</div>")
    P.append("<div class='panel'><h2>Карта требований</h2>"
             f"<div class='reqmap'>{''.join(cells)}</div>"
             f"<div class='map-note'>С дефектами <b>{n_def}</b> · проверено без дефектов <b>{n_ok}</b>"
             + (f" · <b>не проверено {n_un}</b>" if n_un else "") +
             ". Нажмите на клетку, чтобы открыть замечание или строку документа.</div></div></div></section>")

    # что исправить первым
    top = v["fix_first"][:5]
    if top:
        P.append("<section class='block'><div class='block-head'><h2>Что исправить в первую очередь</h2>"
                 "<p>по тяжести и доказанности</p></div>")
        for k, f in enumerate(top, start=1):
            lines = sorted({q.line_start for q in f.quotes if q.line_start})
            where = ("строки " + ", ".join(map(str, lines[:5]))) if len(lines) > 1 else (f"строка {lines[0]}" if lines else "")
            P.append(f"<article class='fix {SEV_CLASS[f.severity]}'><div class='num'>{k}</div><div>"
                     f"<div class='where'>{e(SEVERITY_RU[f.severity])} · {e(category_ru(f.category))} · {e(where)} · {f.fid}</div>"
                     f"<h3>{e(f.title)}</h3>")
            for q in f.quotes[:2]:
                if q.text:
                    P.append(f"<div class='quote' data-line='{q.line_start}' data-ln='строка {q.line_start}'>{e(q.text.replace('**', ''))}</div>")
            P.append(_duel(f.scenario, e))
            if f.suggestion:
                P.append(f"<div class='fixit'><b>Как исправить:</b> {e(f.suggestion)}</div>")
            P.append("</div></article>")
        P.append("</section>")

    # вопросы автору
    qs = [f for fid in result.priority for f in findings if f.fid == fid and f.question and f.question.text]
    qs += [f for f in sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])
           if f.question and f.question.text and f not in qs]
    if qs:
        P.append("<section class='block' id='questions'><div class='block-head'><h2>Вопросы автору</h2>"
                 f"<p>{len(qs)} · самые важные первыми</p></div><div class='qgrid'>")
        for f in qs[:6]:
            opts = "".join(f"<span class='opt'>{e(o)}</span>" for o in f.question.options[:4])
            P.append(f"<div class='qcard'><div class='to'>{e(f.question.addressee or 'Автор документа')} · {f.fid}</div>"
                     f"<p>{e(f.question.text)}</p>{opts}</div>")
        P.append("</div>")
        if len(qs) > 6:
            P.append(f"<div class='map-note'>Остальные {len(qs) - 6} вопросов — в карточках замечаний ниже.</div>")
        P.append("</section>")

    # документ + замечания
    P.append("<section class='block' id='work'><div class='block-head'><h2>Документ и замечания</h2>"
             "<p>клик по строке → замечание, по цитате → строка</p></div><div class='work'>")
    P.append("<div class='pane doc-pane'><div class='pane-head'><b>Документ</b>"
             f"<span class='tag'>{e(doc.fmt.upper())}</span></div><div class='scroll'>")
    for ln in doc.lines:
        txt = e(ln.text.replace("**", "")) or " "
        fs = sorted(by_line.get(ln.n, []), key=lambda f: SEVERITY_ORDER[f.severity])
        if fs:
            ids = ",".join(dict.fromkeys(f.fid for f in fs))
            tip = " | ".join(f"{f.fid}: {f.title}" for f in fs[:4])
            P.append(f"<div class='ln s-{fs[0].severity}' id='L{ln.n}' data-f='{ids}' title='{e(tip)}'>"
                     f"<span class='n'>{ln.n}</span><span>{txt}</span></div>")
        else:
            P.append(f"<div class='ln' id='L{ln.n}'><span class='n'>{ln.n}</span><span>{txt}</span></div>")
    P.append("</div></div>")
    P.append("<div class='pane'><div class='pane-head'><b>Замечания · {}</b>".format(len(findings)) +
             "<button class='chip on' data-k='all'>Все</button><button class='chip' data-k='blocker'>Блокеры</button>"
             "<button class='chip' data-k='major'>Существенные</button><button class='chip' data-k='minor'>Мелкие</button>"
             "<button class='chip' data-k='Д'>Доказано</button><button class='chip' data-k='У'>Вопросы</button>"
             "<input class='search' id='search' type='search' placeholder='Поиск по замечаниям'>"
             "</div><div class='scroll cards'>")
    judge_ru = {"confirmed": "подтверждено", "disputed": "спорно", "rejected": "отклонено"}
    for f in findings:
        rids = ", ".join(f.rids[:4])
        P.append(f"<article class='card {SEV_CLASS[f.severity]}' id='{f.fid}' data-sev='{f.severity}' data-proof='{e(f.proof)}'>"
                 f"<h4><span class='fid'>{f.fid}</span>{e(f.title)}</h4><div class='tags'>"
                 f"<span class='tag {SEV_CLASS[f.severity]}'>{e(SEVERITY_RU[f.severity])}</span>"
                 f"<span class='tag'>{e(category_ru(f.category))}</span>"
                 f"<span class='tag'>[{e(f.proof)}] {e(PROOF_RU.get(f.proof, ''))}</span>"
                 + (f"<span class='tag'>{e(rids)}</span>" if rids else "") + "</div>")
        for q in f.quotes[:3]:
            if q.text:
                P.append(f"<div class='quote' data-line='{q.line_start}' data-ln='строка {q.line_start}'>{e(q.text.replace('**', ''))}</div>")
        if f.explanation:
            P.append(f"<p>{e(f.explanation)}</p>")
        P.append(_duel(f.scenario, e))
        if f.impact:
            P.append(f"<p><b>Последствия:</b> {e(f.impact)}</p>")
        if f.suggestion:
            P.append(f"<p><b>Как исправить:</b> {e(f.suggestion)}</p>")
        if f.question and f.question.text:
            opts = "".join(f"<span class='opt'>{e(o)}</span>" for o in f.question.options[:4])
            P.append(f"<p><b>Вопрос{(' · ' + e(f.question.addressee)) if f.question.addressee else ''}:</b> "
                     f"{e(f.question.text)}</p>{opts}")
        votes = ", ".join(f.votes)
        crit = judge_ru.get(f.judge, "не проверялось")
        P.append(f"<div class='verdict-line'>Нашёл: {e(votes) or '—'} · Критик: {e(crit)}"
                 f"{' — ' + e(f.judge_note) if f.judge_note else ''}</div></article>")
    P.append("<div class='empty' id='empty' style='display:none'>Ничего не найдено</div></div></div></div></section>")

    # скачивание
    P.append("<footer id='download'><div class='block-head'><h2>Скачать отчёт</h2><p>в удобном формате</p></div>"
             "<div class='dl'>"
             "<button id='dl-md'><span class='ext'>MD</span><span class='t'><b>Markdown</b>"
             "<small>для Git, Confluence, Jira</small></span></button>"
             "<button id='dl-pdf'><span class='ext'>PDF</span><span class='t'><b>PDF</b>"
             "<small>печать → «Сохранить как PDF»</small></span></button>"
             "<button id='dl-html'><span class='ext'>HTML</span><span class='t'><b>HTML</b>"
             "<small>этот интерактивный отчёт, один файл</small></span></button>"
             "</div>"
             f"<div class='fine'>Отчёт сформирован агентом ReqAudit автоматически. Каждое замечание опирается на дословную "
             f"цитату документа; цитаты проверены программой. Документ: {e(doc.name)}, SHA-256 {e(doc.sha256[:16])}…</div>"
             "</footer></main>")
    # JSON внутри <script> не декодирует HTML-сущности, поэтому Markdown скачается байт в байт
    P.append("<script type='application/json' id='md-src'>" + json.dumps(md_text).replace("</", "<\\/") + "</script>")
    P.append(f"<script>{JS}</script></body></html>")
    out_path.write_text("".join(P), encoding="utf-8")
