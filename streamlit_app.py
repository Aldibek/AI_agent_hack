"""ReqAudit — веб-приложение для проверки ТЗ.

Запуск локально:   streamlit run streamlit_app.py
Streamlit Cloud:   ключ в Secrets — ANTHROPIC_API_KEY (или GEMINI_API_KEY); по желанию APP_PASSWORD — код доступа
                   к полной проверке (быстрая проверка правилами доступна всем).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "reports" / "app"
SAMPLE_DOC = ROOT / "docs" / "requirements.md"
SAMPLE_REPORT = ROOT / "submission"

st.set_page_config(page_title="ReqAudit — проверка требований", page_icon="📄", layout="centered")

try:  # локально ключи лежат в .env (как у командной строки)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ключи из Streamlit Secrets → в окружение процесса агента
try:
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "REQAUDIT_BUDGET_USD", "REQAUDIT_DAILY_BUDGET_USD"):
        if k in st.secrets and not os.environ.get(k):
            os.environ[k] = str(st.secrets[k])
    APP_PASSWORD = str(st.secrets.get("APP_PASSWORD", "")) or os.environ.get("APP_PASSWORD", "")
except Exception:  # noqa: BLE001 — локально secrets.toml может не быть
    APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

st.markdown("""
<style>
.block-container{max-width:760px;padding-top:3rem}
h1{font-family:"Iowan Old Style",Palatino,Georgia,serif;font-weight:600;letter-spacing:-.01em}
.verdict{font-family:"Iowan Old Style",Palatino,Georgia,serif;font-size:2.1rem;line-height:1.15;font-weight:600;margin:.4rem 0 .2rem}
.verdict .no{color:#c8372d}.verdict .warn{color:#c27412}.verdict .ok{color:#2f7d4f}
.muted{color:#8a8a90;font-size:.9rem}
.steps{border:1px solid #e3e1da;border-radius:16px;padding:18px 20px;margin:8px 0 4px;background:#fff}
.step{display:grid;grid-template-columns:26px 1fr auto;gap:10px;align-items:baseline;padding:7px 0;
  border-bottom:1px solid #f0eee8;font-size:.98rem}
.step:last-of-type{border-bottom:0}
.step .ic{font-size:.95rem;text-align:center}
.step .t{color:#1b1b1d}.step .d{color:#8a8a90;font-size:.88rem;text-align:right}
.step.wait .t{color:#a6a6ac}.step.wait .ic{color:#c9c7c0}
.step.done .ic{color:#2f7d4f}.step.fail .ic,.step.fail .d{color:#c8372d}
.step.run .ic{color:#c27412;animation:pulse 1.1s ease-in-out infinite}.step.run .t{font-weight:600}
.step.sub{padding-left:22px;font-size:.93rem}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.clock{display:flex;justify-content:space-between;color:#8a8a90;font-size:.86rem;margin-top:10px}
</style>""", unsafe_allow_html=True)

st.caption("ReqAudit · ISO/IEC/IEEE 29148 · IEEE 830")
st.title("Проверка требований до разработки")
st.write("Загрузите ТЗ — агент найдёт противоречия, двусмысленности и пробелы, проверит **каждое требование** "
         "и подготовит заключение со ссылками на строки документа.")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    w = one if n10 == 1 and n100 != 11 else (few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many)
    return f"{n} {w}"


PASS_NAMES = {1: "проход 1 · каждое требование по отдельности", 2: "проход 2 · общий разбор за пять ролей",
              3: "проход 3 · «два разработчика» и лазейки"}
HIDDEN = re.compile(r"оценка стоимости|стоимость прогона|бюджет|расход|\$\d", re.I)


class Progress:
    """Шаги проверки с отметками — вместо сырого журнала."""

    def __init__(self, fast: bool):
        self.fast, self.t0 = fast, time.time()
        keys = ["read", "seg", "rules"] + ([] if fast else ["p1", "p2", "p3", "quotes", "critic"]) + ["report"]
        names = {"read": "Читаю документ", "seg": "Делю на отдельные требования", "rules": "Проверяю правилами",
                 "quotes": "Сверяю цитаты с документом", "critic": "Критик пытается опровергнуть каждую находку",
                 "report": "Пишу заключение", **{f"p{k}": v.capitalize() for k, v in PASS_NAMES.items()}}
        self.steps = {k: {"name": names[k], "state": "wait", "detail": ""} for k in keys}
        self.steps["read"]["state"] = "run"
        self.box = st.empty()
        self.draw()

    def set(self, key: str, state: str, detail: str = "") -> None:
        if key in self.steps:
            self.steps[key]["state"] = state
            if detail:
                self.steps[key]["detail"] = detail

    def feed(self, line: str) -> None:
        if m := re.match(r"документ: .+?, строк: (\d+)(?:, страниц: (\d+))?", line):
            self.set("read", "done", _plural(int(m[2]), "страница", "страницы", "страниц") if m[2] else
                     _plural(int(m[1]), "строка", "строки", "строк"))
            self.set("seg", "run")
        elif m := re.match(r"нарезка: (\d+)", line):
            self.set("seg", "done", _plural(int(m[1]), "требование", "требования", "требований"))
            self.set("rules", "run")
        elif m := re.match(r"правила: (\d+)", line):
            self.set("rules", "done", _plural(int(m[1]), "находка", "находки", "находок"))
            if self.fast:
                self.set("report", "run")
        elif line.startswith("консилиум"):
            for k in ("p1", "p2", "p3"):
                self.set(k, "run")
        elif m := re.match(r"ревьюер \S+?#(\d+): (\d+) находок за (\d+) с", line):
            self.set(f"p{m[1]}", "done", f"{_plural(int(m[2]), 'находка', 'находки', 'находок')} · {m[3]} с")
        elif m := re.match(r"ревьюер \S+?#(\d+) упал", line):
            self.set(f"p{m[1]}", "fail", "не ответил")
        elif m := re.match(r"проверка цитат: подтверждено (\d+), отброшено (\d+)", line):
            self.set("quotes", "done", f"подтверждено {m[1]}, отброшено {m[2]}")
            self.set("critic", "run")
        elif m := re.match(r"\s*критик склеил дублей: (\d+)", line):
            self.steps["critic"]["detail"] = f"склеено дублей: {m[1]}"
        elif m := re.match(r"готово: (\d+)", line):
            if self.steps.get("critic", {}).get("state") == "run":
                self.set("critic", "done")
            self.set("report", "run", _plural(int(m[1]), "замечание", "замечания", "замечаний"))
        self.draw()

    def finish(self, ok: bool) -> None:
        for v in self.steps.values():
            if v["state"] == "run":
                v["state"] = "done" if ok else "fail"
        self.draw(final=True)

    def draw(self, final: bool = False) -> None:
        ic = {"wait": "○", "run": "●", "done": "✓", "fail": "✕"}
        rows = "".join(f"<div class='step {v['state']}{' sub' if k.startswith('p') else ''}'><span class='ic'>"
                       f"{ic[v['state']]}</span><span class='t'>{v['name']}</span><span class='d'>{v['detail']}</span></div>"
                       for k, v in self.steps.items())
        sec = int(time.time() - self.t0)
        hint = "" if final or self.fast else "обычно 2–3 минуты"
        self.box.markdown(f"<div class='steps'>{rows}<div class='clock'><span>{'Заняло' if final else 'Прошло'} "
                          f"{sec // 60}:{sec % 60:02d}</span><span>{hint}</span></div></div>", unsafe_allow_html=True)


def run_agent(src: Path, fast: bool) -> Path:
    """Запуск того же агента, что из командной строки; ход работы — по шагам."""
    out_dir = RUNS / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "reqaudit", str(src), str(out_dir / "report.md")] + (["--no-llm"] if fast else [])
    prog = Progress(fast)
    log: list[str] = []
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=os.environ.copy())
    for raw in proc.stdout:
        line = raw.replace("[reqaudit] ", "").strip()
        if not line or line.startswith(("Отчёт:", "Трассировка:", "===", "OCR on", "Warning: Rapid", "Using Tesseract")):
            continue
        prog.feed(line)
        if not HIDDEN.search(line):
            log.append(line)
    code = proc.wait()
    ok = (out_dir / "report.html").exists()
    prog.finish(ok)
    if not ok:
        st.error("Проверка не завершилась. Подробности — в журнале ниже.")
    elif code != 0:
        st.info("Часть шагов пропущена — это отмечено в паспорте заключения.")
    with st.expander("Подробный журнал"):
        st.code("\n".join(log) or "—", language=None)
    return out_dir


def show(out_dir: Path, title: str) -> None:
    data = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    from reqaudit.models import Finding, Requirement
    from reqaudit.verdict import compute

    findings = [Finding.model_validate(f) for f in data["findings"]]
    reqs = [Requirement.model_validate(r) for r in data["reqs"]]
    v = compute(findings, reqs)
    dev = v["dev"][0]
    html_v = {"Нельзя": "Разработку начинать <span class='no'>нельзя</span>.",
              "С оговорками": "Разработку можно начинать <span class='warn'>с оговорками</span>.",
              "Можно": "Документ <span class='ok'>готов</span> к разработке."}[dev]
    st.divider()
    st.markdown(f"<div class='muted'>{title}</div><div class='verdict'>{html_v}</div>"
                f"<div class='muted'>Тестирование: {v['test'][0].lower()}.</div>", unsafe_allow_html=True)
    sev = {"blocker": 0, "major": 0, "minor": 0}
    for f in findings:
        sev[f.severity] += 1
    secs = (data.get("stats") or {}).get("seconds") or 0
    n_q = sum(1 for f in findings if f.question and f.question.text)
    c = st.columns(4)
    c[0].metric("Требований", len(reqs))
    c[1].metric("Блокеров", sev["blocker"])
    c[2].metric("Вопросов автору", n_q)
    c[3].metric("Время проверки", f"{secs / 60:.1f} мин" if secs >= 60 else f"{secs:.0f} с")

    d = st.columns(4)
    for col, (name, ext, mime) in zip(d, (("Markdown", "md", "text/markdown"), ("PDF", "pdf", "application/pdf"),
                                         ("HTML", "html", "text/html"), ("JSON", "json", "application/json"))):
        f = out_dir / f"report.{ext}"
        if f.exists():
            col.download_button(name, f.read_bytes(), file_name=f"reqaudit_report.{ext}", mime=mime,
                                width="stretch", key=f"{out_dir.name}-{ext}")
    html_file = out_dir / "report.html"
    if html_file.exists():
        # data:-URL — отчёт открывается в изолированном фрейме без доступа к приложению
        b64 = base64.b64encode(html_file.read_bytes()).decode("ascii")
        st.iframe(f"data:text/html;base64,{b64}", height=1100)


tab_new, tab_sample = st.tabs(["Проверить свой документ", "Пример: документ хакатона"])

with tab_new:
    up = st.file_uploader("Документ требований", type=["pdf", "docx", "md", "txt"],
                          help="PDF (в том числе сканы — распознаются автоматически), DOCX, Markdown или текст; до 25 МБ")
    mode = st.radio("Режим", ["Полная проверка — Claude Opus 5.5, обычно 2–3 минуты",
                              "Быстрая — только детерминированные правила, мгновенно"], index=0)
    fast = mode.startswith("Быстрая")
    has_key = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"))
    allowed = True
    if not fast and not has_key:
        st.warning("Ключ модели не задан — доступна только быстрая проверка правилами.")
        allowed = False
    if not fast and APP_PASSWORD:
        code = st.text_input("Код доступа к полной проверке", type="password",
                             help="Код выдаёт команда.")
        allowed = allowed and code == APP_PASSWORD
    if st.button("Проверить", type="primary", disabled=up is None or not allowed):
        if up.size > 25 * 1024 * 1024:
            st.error("Файл больше 25 МБ.")
        else:
            tmp = RUNS / "uploads"
            tmp.mkdir(parents=True, exist_ok=True)
            src = tmp / f"{uuid.uuid4().hex[:8]}_{Path(up.name).name}"
            src.write_bytes(up.getvalue())
            st.session_state["last"] = (str(run_agent(src, fast)), up.name)
    if "last" in st.session_state:
        out, name = st.session_state["last"]
        if (Path(out) / "report.json").exists():
            show(Path(out), name)

with tab_sample:
    st.write("Готовое заключение по документу, выданному организаторами "
             f"([`docs/requirements.md`](https://github.com/Aldibek/AI_agent_hack/blob/main/docs/requirements.md)): "
             "полная проверка Claude Opus 5.5. Открывается мгновенно.")
    if (SAMPLE_REPORT / "report.json").exists():
        show(SAMPLE_REPORT, "Уведомление о расходах по карте · requirements.md")
    else:
        st.info("Готового отчёта нет: запустите `python -m reqaudit docs/requirements.md submission/report.md`.")

st.caption("Агент: код извлекает текст и проверяет цитаты, три прохода Claude ищут дефекты, критик пытается их "
           "опровергнуть. Исходный код и инструкция — в репозитории.")
