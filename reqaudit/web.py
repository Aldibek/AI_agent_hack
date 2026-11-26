"""Локальный веб-интерфейс: загрузить документ → проверка → отчёт.

Запуск:  python -m reqaudit.web            → http://127.0.0.1:8000
         python -m reqaudit.web --port 8080

Только стандартная библиотека. Сервер слушает 127.0.0.1 (наружу не открыт). Каждый документ проверяется тем же
агентом, что и из командной строки (`python -m reqaudit вход отчёт`), в отдельном процессе; одновременно идёт
не больше одной проверки, лимиты расходов из .env действуют как обычно.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "reports" / "web"
ALLOWED = {".pdf", ".md", ".markdown", ".docx", ".txt"}
MAX_BYTES = 25 * 1024 * 1024
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
BUSY = threading.Semaphore(1)

PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ReqAudit — проверка требований</title>
<style>
:root{--bg:#fbfaf7;--ink:#1b1b1d;--ink-2:#5b5b61;--ink-3:#93939a;--line:#e3e1da;--soft:#f3f1ec;--accent:#1b1b1d;--red:#c8372d;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#141415;--ink:#ececee;--ink-2:#a9a9b0;--ink-3:#77777e;--line:#2c2c30;--soft:#1c1c1f;
  --accent:#ececee;--red:#ff7a70}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 var(--sans)}
.page{max-width:680px;margin:0 auto;padding:88px 24px 80px}
@media (max-width:640px){.page{padding:48px 18px}}
.kicker{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3)}
h1{font:600 clamp(34px,6vw,50px)/1.08 var(--serif);letter-spacing:-.015em;margin:16px 0 14px}
.lede{color:var(--ink-2);font-size:19px;margin:0 0 40px}
.drop{border:1.5px dashed var(--line);border-radius:18px;padding:46px 24px;text-align:center;cursor:pointer;
  transition:border-color .15s,background .15s}
.drop:hover,.drop.over{border-color:var(--ink);background:var(--soft)}
.drop b{display:block;font:600 22px var(--serif);margin-bottom:6px}
.drop span{color:var(--ink-3);font-size:15px}
.opts{display:flex;gap:18px;flex-wrap:wrap;margin:18px 2px 0;font-size:15px;color:var(--ink-2)}
.opts label{display:flex;gap:8px;align-items:center;cursor:pointer}
.run{margin-top:26px;font:500 16px var(--sans);color:var(--bg);background:var(--ink);border:0;border-radius:999px;
  padding:14px 28px;cursor:pointer}.run:disabled{opacity:.35;cursor:default}
.file{margin-top:16px;font-size:15px;color:var(--ink)}
.status{margin-top:40px;display:none}
.status h2{font:600 24px var(--serif);margin:0 0 6px}
.bar{height:3px;background:var(--line);border-radius:3px;overflow:hidden;margin:14px 0}
.bar i{display:block;height:100%;width:30%;background:var(--ink);animation:mv 1.4s ease-in-out infinite}
@keyframes mv{0%{transform:translateX(-100%)}100%{transform:translateX(340%)}}
pre{font:13px/1.6 var(--mono);color:var(--ink-2);background:var(--soft);border-radius:12px;padding:14px 16px;
  max-height:300px;overflow:auto;white-space:pre-wrap;margin:0}
.err{color:var(--red)}
.done a{display:inline-block;margin:8px 10px 0 0;color:var(--ink);border:1px solid var(--ink);border-radius:999px;
  padding:10px 20px;text-decoration:none;font-size:15px}
.done a.main{background:var(--ink);color:var(--bg)}
.fine{margin-top:56px;font-size:13px;color:var(--ink-3)}
</style></head><body><main class="page">
<div class="kicker">ReqAudit · ISO/IEC/IEEE 29148 · IEEE 830</div>
<h1>Проверка требований до разработки</h1>
<p class="lede">Загрузите ТЗ — агент найдёт противоречия, двусмысленности и пробелы, проверит каждое требование
и подготовит заключение со ссылками на строки документа.</p>
<div class="drop" id="drop"><b>Перетащите файл сюда</b><span>или нажмите, чтобы выбрать · PDF, DOCX, MD, TXT · до 25 МБ</span>
<input type="file" id="inp" accept=".pdf,.md,.markdown,.docx,.txt" hidden></div>
<div class="file" id="fname"></div>
<div class="opts"><label><input type="checkbox" id="fast"> Быстро, без модели — только правила (бесплатно, &lt; 1 с)</label></div>
<button class="run" id="run" disabled>Проверить</button>
<section class="status" id="st"><h2 id="sth">Идёт проверка…</h2><div class="bar" id="bar"><i></i></div><pre id="log"></pre>
<div class="done" id="done"></div></section>
<p class="fine">Полная проверка занимает 2–4 минуты и стоит около $1 (Claude Opus 5.5); действует лимит расходов из .env.
Сервер работает только на этом компьютере.</p>
</main><script>
const drop=document.getElementById('drop'),inp=document.getElementById('inp'),run=document.getElementById('run');
let file=null;
function pick(f){if(!f)return;file=f;document.getElementById('fname').textContent='Файл: '+f.name+' · '+Math.ceil(f.size/1024)+' КБ';run.disabled=false;}
drop.onclick=()=>inp.click();inp.onchange=()=>pick(inp.files[0]);
['dragenter','dragover'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.add('over');}));
['dragleave','drop'].forEach(t=>drop.addEventListener(t,e=>{e.preventDefault();drop.classList.remove('over');}));
drop.addEventListener('drop',e=>pick(e.dataTransfer.files[0]));
run.onclick=async()=>{
  if(!file)return;run.disabled=true;const st=document.getElementById('st');st.style.display='block';
  document.getElementById('sth').textContent='Идёт проверка…';document.getElementById('bar').style.display='';
  document.getElementById('done').innerHTML='';document.getElementById('log').textContent='';
  const r=await fetch('/api/run',{method:'POST',body:file,headers:{'X-Filename':encodeURIComponent(file.name),
    'X-Mode':document.getElementById('fast').checked?'rules':'full'}});
  const j=await r.json();if(!r.ok){fail(j.error||'Ошибка');return;}poll(j.id);};
function fail(m){document.getElementById('sth').innerHTML='<span class="err">'+m+'</span>';
  document.getElementById('bar').style.display='none';run.disabled=false;}
async function poll(id){const r=await fetch('/api/status/'+id);const j=await r.json();
  document.getElementById('log').textContent=j.log.join('\n');const pre=document.getElementById('log');pre.scrollTop=pre.scrollHeight;
  if(j.state==='running'||j.state==='queued'){setTimeout(()=>poll(id),1500);return;}
  document.getElementById('bar').style.display='none';run.disabled=false;
  if(!j.files||!j.files.html){fail(j.error||'Проверка не завершилась');return;}
  document.getElementById('sth').textContent=j.state==='partial'?'Готово, часть шагов пропущена (см. журнал)':'Готово';
  const b='/r/'+id+'/';document.getElementById('done').innerHTML=
   '<a class="main" href="'+b+j.files.html+'" target="_blank">Открыть заключение</a>'+
   '<a href="'+b+j.files.md+'" download>Markdown</a><a href="'+b+j.files.json+'" download>JSON</a>';}
</script></body></html>"""


def _safe_name(name: str) -> str:
    name = Path(unquote(name or "")).name
    stem = re.sub(r"[^\w.\-]+", "_", Path(name).stem, flags=re.U).strip("._") or "document"
    return stem[:80] + Path(name).suffix.lower()


def _worker(job_id: str) -> None:
    job = JOBS[job_id]
    with BUSY:
        job["state"] = "running"
        d = Path(job["dir"])
        cmd = [sys.executable, "-m", "reqaudit", str(d / job["input"]), str(d / "report.md")]
        if job["mode"] == "rules":
            cmd.append("--no-llm")
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        for line in proc.stdout:
            line = line.rstrip()
            if line and not line.startswith(("Отчёт:", "Трассировка:")):  # пути на диске пользователю не нужны
                with LOCK:
                    job["log"].append(line.replace("[reqaudit] ", ""))
        code = proc.wait()
        if (d / "report.html").exists():
            job["files"] = {"html": "report.html", "md": "report.md", "json": "report.json"}
            job["state"] = "done" if code == 0 else "partial"
        else:
            job["state"] = "error"
            job["error"] = "Проверка не завершилась: " + (job["log"][-1] if job["log"] else f"код {code}")


class Handler(BaseHTTPRequestHandler):
    server_version = "ReqAudit"

    def log_message(self, fmt, *args):  # тихий сервер
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        m = re.fullmatch(r"/api/status/([0-9a-f]{12})", path)
        if m and m.group(1) in JOBS:
            j = JOBS[m.group(1)]
            with LOCK:
                return self._json(200, {k: j.get(k) for k in ("state", "log", "files", "error")})
        m = re.fullmatch(r"/r/([0-9a-f]{12})/(report\.(?:html|md|json))", path)
        if m and m.group(1) in JOBS:
            f = Path(JOBS[m.group(1)]["dir"]) / m.group(2)
            if f.exists():
                ctype = {"html": "text/html", "md": "text/markdown", "json": "application/json"}[f.suffix[1:]]
                extra = {} if f.suffix == ".html" else {"Content-Disposition": f'attachment; filename="{f.name}"'}
                return self._send(200, f.read_bytes(), ctype + "; charset=utf-8", extra)
        self._json(404, {"error": "не найдено"})

    def do_POST(self):  # noqa: N802
        if self.path != "/api/run":
            return self._json(404, {"error": "не найдено"})
        name = _safe_name(self.headers.get("X-Filename", ""))
        if Path(name).suffix not in ALLOWED:
            return self._json(400, {"error": "Поддерживаются PDF, DOCX, MD и TXT"})
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0 or size > MAX_BYTES:
            return self._json(400, {"error": "Файл пустой или больше 25 МБ"})
        data = self.rfile.read(size)
        job_id = uuid.uuid4().hex[:12]
        d = RUNS / (time.strftime("%Y%m%d-%H%M%S-") + job_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        mode = "rules" if self.headers.get("X-Mode") == "rules" else "full"
        JOBS[job_id] = {"dir": str(d), "input": name, "mode": mode, "state": "queued",
                        "log": ["в очереди…" if BUSY._value == 0 else "запуск…"], "files": None, "error": ""}
        threading.Thread(target=_worker, args=(job_id,), daemon=True).start()
        self._json(200, {"id": job_id})


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="reqaudit.web", description="Локальный веб-интерфейс ReqAudit")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    RUNS.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ReqAudit: http://127.0.0.1:{args.port}  (Ctrl+C — остановить)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
