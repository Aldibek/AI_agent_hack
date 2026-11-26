"""ReqAudit — веб-приложение для проверки ТЗ.

Запуск локально:   streamlit run streamlit_app.py
Streamlit Cloud:   ключ в Secrets — ANTHROPIC_API_KEY (или GEMINI_API_KEY); по желанию APP_PASSWORD — код доступа
                   к платной проверке (быстрая проверка правилами доступна всем).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import base64

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
</style>""", unsafe_allow_html=True)

st.caption("ReqAudit · ISO/IEC/IEEE 29148 · IEEE 830")
st.title("Проверка требований до разработки")
st.write("Загрузите ТЗ — агент найдёт противоречия, двусмысленности и пробелы, проверит **каждое требование** "
         "и подготовит заключение со ссылками на строки документа.")


def run_agent(src: Path, fast: bool) -> Path:
    """Запуск того же агента, что из командной строки; ход работы — в статусе."""
    out_dir = RUNS / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "reqaudit", str(src), str(out_dir / "report.md")] + (["--no-llm"] if fast else [])
    with st.status("Проверяю документ…", expanded=True) as status:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=os.environ.copy())
        for line in proc.stdout:
            line = line.strip().replace("[reqaudit] ", "")
            if line and not line.startswith(("Отчёт:", "Трассировка:")):
                st.write(line)
        code = proc.wait()
        if (out_dir / "report.html").exists():
            status.update(label="Готово" if code == 0 else "Готово, часть шагов пропущена (см. журнал)",
                          state="complete" if code == 0 else "error", expanded=False)
        else:
            status.update(label="Проверка не завершилась — см. журнал выше", state="error", expanded=True)
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
    cost = (data.get("costs") or {}).get("cost_usd")
    c = st.columns(4)
    c[0].metric("Требований", len(reqs))
    c[1].metric("Блокеров", sev["blocker"])
    c[2].metric("Существенных", sev["major"])
    c[3].metric("Стоимость", f"${cost:.2f}" if cost else "$0")

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
    mode = st.radio("Режим", ["Полная проверка — Claude, 2–4 мин, около $1",
                              "Быстрая — только правила, бесплатно, меньше секунды"], index=0)
    fast = mode.startswith("Быстрая")
    has_key = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"))
    allowed = True
    if not fast and not has_key:
        st.warning("Ключ модели не задан — доступна только быстрая проверка правилами.")
        allowed = False
    if not fast and APP_PASSWORD:
        code = st.text_input("Код доступа к полной проверке", type="password",
                             help="Полная проверка тратит деньги на API, поэтому закрыта кодом.")
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
             "полная проверка Claude Opus 5.5. Открывается мгновенно и бесплатно.")
    if (SAMPLE_REPORT / "report.json").exists():
        show(SAMPLE_REPORT, "Уведомление о расходах по карте · requirements.md")
    else:
        st.info("Готового отчёта нет: запустите `python -m reqaudit docs/requirements.md submission/report.md`.")

st.caption("Агент: код извлекает текст и проверяет цитаты, три прохода Claude ищут дефекты, критик пытается их "
           "опровергнуть. Исходный код и инструкция — в репозитории.")
