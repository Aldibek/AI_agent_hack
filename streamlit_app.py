"""ReqAudit — веб-приложение для проверки ТЗ.

Запуск локально:   streamlit run streamlit_app.py
Streamlit Cloud:   ключ в Secrets — ANTHROPIC_API_KEY (или GEMINI_API_KEY); по желанию APP_PASSWORD — код доступа
                   к полной проверке (быстрая проверка правилами доступна всем).
"""

from __future__ import annotations

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
        keys = ["read", "seg", "rules"] + ([] if fast else ["p1", "p2", "p3", "ac", "quotes", "critic"]) + ["report"]
        names = {"read": "Читаю документ", "seg": "Делю на отдельные требования", "rules": "Проверяю правилами",
                 "quotes": "Сверяю цитаты с документом", "ac": "Сверяю требования с критериями приёмки", "critic": "Критик пытается опровергнуть каждую находку",
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
            for k in ("p1", "p2", "p3", "ac"):
                self.set(k, "run")
        elif m := re.match(r"ревьюер \S+?#(\d+): (\d+) находок за (\d+) с", line):
            self.set(f"p{m[1]}", "done", f"{_plural(int(m[2]), 'находка', 'находки', 'находок')} · {m[3]} с")
        elif m := re.match(r"ревьюер \S+?#(\d+) упал", line):
            self.set(f"p{m[1]}", "fail", "не ответил")
        elif m := re.match(r"покрытие критериями приёмки: (\d+) из (\d+)", line):
            self.set("ac", "done", f"проверяется {m[1]} из {m[2]}")
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
    # --no-cache: в приложении каждая проверка — заново, модель действительно думает над документом
    cmd = [sys.executable, "-m", "reqaudit", str(src), str(out_dir / "report.md")] + (["--no-llm"] if fast else ["--no-cache"])
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
    if data.get("answers"):
        answered(data)
    else:
        clarify_block(out_dir, data)
    conclusion(data, findings, reqs, v)


ANS_STYLE = {"решение получено": "success", "снято": "info", "частично": "warning", "остаётся": "warning"}


def answered(data: dict) -> None:
    """Итог перепроверки: что стало с каждой находкой после ответа автора, и исправленная формулировка."""
    st.subheader("Ответы автора и перепроверка")
    st.caption("Агент перепроверил свои находки по ответам: вердикт и замечания ниже уже пересчитаны.")
    for a in data["answers"]:
        with st.container(border=True):
            st.markdown(f"**{a.get('fid')}** · {_md(a.get('question', ''))}")
            st.markdown(f"Ответ: *{_md(a.get('answer', ''))}*")
            status = a.get("status", "")
            kind = next((k for w, k in ANS_STYLE.items() if status.startswith(w)), "info")
            getattr(st, kind)(status.capitalize())
            if a.get("rewrite"):
                st.markdown(f"**ТЗ v2:** {_md(a['rewrite'])}")
            if a.get("new_issue"):
                st.warning(f"Новый вопрос: {_md(a['new_issue'])}")


def clarify_block(out_dir: Path, data: dict) -> None:
    """Человек в контуре: агент спрашивает там, где не уверен, и перепроверяет себя по ответам."""
    from reqaudit.clarify import pending_questions
    from reqaudit.pipeline import RunResult

    doc = dict(data["doc"])
    has_text = bool(doc.get("lines"))
    doc.setdefault("lines", [])
    try:
        qs = pending_questions(RunResult.model_validate({**data, "doc": doc}))
    except Exception:  # noqa: BLE001 — старый формат следа
        qs = []
    if not qs:
        return
    st.subheader("Агент не уверен — уточните")
    st.caption(f"Здесь агенту не хватает решения автора ТЗ ({len(qs)} из {len(data['findings'])} замечаний). "
               "Ответьте — он перепроверит эти находки, предложит исправленную формулировку и пересчитает вердикт.")
    answers: dict[str, str] = {}
    with st.form(f"clarify-{out_dir.name}"):
        for k, (f, why) in enumerate(qs, start=1):
            q = f.question
            st.markdown(f"**{k}. {_md(q.text)}**")
            st.caption(f"{f.fid} · {_md(f.title)} · почему спрашиваю: {'; '.join(why)}"
                       + (f" · кому: {q.addressee}" if q.addressee else ""))
            for qt in f.quotes[:2]:
                st.markdown(f"> {_md(qt.text[:200])}  \n> <small>строка {qt.line_start}</small>", unsafe_allow_html=True)
            opts = ["— пропустить —"] + list(q.options[:5]) + ["Свой ответ"]
            pick = st.radio("Ответ", opts, key=f"{out_dir.name}-{f.fid}-pick", label_visibility="collapsed")
            own = st.text_input("Свой ответ", key=f"{out_dir.name}-{f.fid}-own", label_visibility="collapsed",
                                placeholder="Свой ответ (если выбран «Свой ответ»)")
            if pick == "Свой ответ":
                answers[f.fid] = own
            elif not pick.startswith("—"):
                answers[f.fid] = pick
        has_key = any(os.environ.get(x) for x in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"))
        go = st.form_submit_button("Перепроверить с ответами", type="primary", disabled=not (has_key and has_text))
    if not has_key:
        st.caption("Перепроверка доступна, когда задан ключ модели.")
    elif not has_text:
        st.caption("Этот отчёт сделан старой версией агента — запустите проверку заново, чтобы ответить на вопросы.")
    if go:
        answers = {k: v for k, v in answers.items() if v and v.strip()}
        if not answers:
            st.warning("Выберите хотя бы один ответ.")
            return
        new_dir = RUNS / (time.strftime("%Y%m%d-%H%M%S-") + "v2-" + uuid.uuid4().hex[:6])
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / "answers.json").write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
        with st.spinner("Агент перепроверяет находки по ответам…"):
            proc = subprocess.run([sys.executable, "-m", "reqaudit.clarify", str(out_dir / "report.json"),
                                   str(new_dir / "answers.json"), str(new_dir / "report.md")], cwd=str(ROOT),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", env=os.environ.copy())
        if not (new_dir / "report.json").exists():
            st.error("Перепроверка не удалась.")
            st.code("\n".join(l for l in proc.stderr.splitlines() if not HIDDEN.search(l))[-2000:] or "—", language=None)
            return
        st.session_state["last"] = (str(new_dir), f"{doc.get('name', 'документ')} · после ответов автора")
        st.session_state["tab_hint"] = True
        st.rerun()


SEV_ICON = {"blocker": "🔴", "major": "🟠", "minor": "🟡"}
SEV_WORD = {"blocker": "блокер", "major": "существенное", "minor": "незначительное"}
SCEN_RE = re.compile(r"^\s*(?:Сценарий:\s*)?(?P<case>.*?)\s*Разработчик\s*А:\s*(?P<a>.*?)\s*Разработчик\s*Б:\s*(?P<b>.*)$", re.S)


def _md(text: str) -> str:
    """Текст модели/документа → безопасный для Markdown (без случайной разметки)."""
    t = (text or "").replace("**", "").replace("`", "'")
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # текст документа не должен стать HTML
    return re.sub(r"([\\*_#>|\[\]$])", r"\\\1", t)


def _card_body(f, n_quotes: int = 2) -> None:
    for q in f.quotes[:n_quotes]:
        if q.text:
            st.markdown(f"> {_md(q.text)}  \n> <small>строка {q.line_start}</small>", unsafe_allow_html=True)
    m = SCEN_RE.match(f.scenario or "")
    if m:
        if m["case"].strip():
            st.markdown(_md(m["case"].strip()))
        a, b = st.columns(2)
        a.info(f"**Разработчик А** — {_md(m['a'].strip())}")
        b.warning(f"**Разработчик Б** — {_md(m['b'].strip())}")
    elif f.scenario:
        st.markdown(f"*Сценарий:* {_md(f.scenario)}")
    if f.suggestion:
        st.markdown(f"**Исправить:** {_md(f.suggestion)}")


def conclusion(data: dict, findings, reqs, v) -> None:
    from reqaudit.criteria import SEVERITY_ORDER, category_ru

    by_fid = {f.fid: f for f in findings}
    top = [by_fid[f.fid] for f in v["fix_first"][:5] if f.fid in by_fid]
    if top:
        st.subheader("Что исправить в первую очередь")
        for k, f in enumerate(top, start=1):
            with st.container(border=True):
                lines = sorted({q.line_start for q in f.quotes if q.line_start})
                st.caption(f"{SEV_ICON[f.severity]} {SEV_WORD[f.severity]} · {category_ru(f.category).lower()} · "
                           f"строки {', '.join(map(str, lines[:5]))} · {f.fid}")
                st.markdown(f"#### {k}. {_md(f.title)}")
                _card_body(f)

    prio = [by_fid[x] for x in data.get("priority") or [] if x in by_fid]
    qs = [f for f in prio if f.question and f.question.text]
    qs += [f for f in sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])
           if f.question and f.question.text and f not in qs]
    if qs:
        st.subheader("Вопросы автору")
        st.markdown("\n".join(f"{k}. {_md(f.question.text)}" +
                               (f"  \n   <small>{' · '.join(_md(o) for o in f.question.options[:3])} — "
                                f"{_md(f.question.addressee or 'автор')}, {f.fid}</small>" if f.question.options else "")
                               for k, f in enumerate(qs[:7], start=1)), unsafe_allow_html=True)

    st.subheader("Проверка каждого требования")
    cov = data.get("coverage") or {}
    per_req: dict[str, list] = {}
    for f in findings:
        for rid in f.rids:
            per_req.setdefault(rid, []).append(f)
    rows = []
    for r in reqs:
        fs = sorted(per_req.get(r.rid, []), key=lambda f: SEVERITY_ORDER[f.severity])
        c = cov.get(r.rid) or {"status": "unchecked", "note": ""}
        if fs:
            st_txt, note = f"{SEV_ICON[fs[0].severity]} {SEV_WORD[fs[0].severity]}", ", ".join(f.fid for f in fs)
        elif c.get("status") == "ok":
            st_txt, note = "🟢 без замечаний", c.get("note", "")
        else:
            st_txt, note = "⚪ не проверено", c.get("note", "")
        rows.append({"ID": r.rid, "В документе": r.doc_ref or "—", "Итог": st_txt, "Замечания": note,
                     "Формулировка": (r.summary or "")[:120]})
    st.dataframe(rows, hide_index=True, width="stretch", height=min(38 * len(rows) + 40, 420))

    acc = data.get("ac_coverage") or []
    if acc:
        n_cov = sum(1 for x in acc if x["covered_by"])
        st.subheader("Покрытие критериями приёмки")
        st.caption(f"Критериями приёмки проверяется {n_cov} из {len(acc)} проверяемых требований "
                   f"({100 * n_cov / len(acc):.0f}%).")
        st.dataframe([{"ID": x["rid"], "Проверяется критериями": ", ".join(x["covered_by"]) or "❌ нет",
                       "Что не проверяется": x.get("gap") or "—"} for x in acc],
                     hide_index=True, width="stretch", height=min(38 * len(acc) + 40, 360))

    st.subheader(f"Все замечания · {len(findings)}")
    for f in sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.fid)):
        with st.expander(f"{SEV_ICON[f.severity]}  {f.fid} · {f.title}"):
            st.caption(f"{SEV_WORD[f.severity]} · {category_ru(f.category).lower()}"
                       + (f" · {', '.join(f.rids[:4])}" if f.rids else ""))
            if f.explanation:
                st.markdown(_md(f.explanation))
            _card_body(f, 3)
            if f.question and f.question.text:
                st.markdown(f"**Вопрос автору:** {_md(f.question.text)}")
            crit = {"confirmed": "подтверждено", "disputed": "спорно"}.get(f.judge, "не проверялось")
            st.caption(f"Нашёл: {', '.join(f.votes) or '—'} · критик: {crit}" + (f" — {f.judge_note}" if f.judge_note else ""))


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
        st.caption("Ответы на вопросы агента откроют перепроверенное заключение во вкладке «Проверить свой документ».")
    else:
        st.info("Готового отчёта нет: запустите `python -m reqaudit docs/requirements.md submission/report.md`.")

st.caption("Агент: код извлекает текст и проверяет цитаты, три прохода Claude ищут дефекты, критик пытается их "
           "опровергнуть. Исходный код и инструкция — в репозитории.")
