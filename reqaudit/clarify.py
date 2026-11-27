"""Человек в контуре: ответы на уточняющие вопросы → перепроверка → исправленные формулировки (ТЗ v2).

Где агент не уверен (критик спорит, блокер без ответа в документе, нашёл один проход), он спрашивает человека —
не больше REQAUDIT_QUESTIONS вопросов (по умолчанию 3). Ответы перепроверяются отдельным вызовом модели: находка
снимается, подтверждается или получает исправленную формулировку («ТЗ v2»), вердикт пересчитывается.

- в терминале: python -m reqaudit doc.md out.md --interactive
- в веб-приложении: кнопки-варианты → python -m reqaudit.clarify report.json answers.json out/report.md
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import costs, prompts
from .criteria import SEVERITY_ORDER
from .costs import BudgetExceeded
from .llm import LLMError, available_clients
from .models import Finding
from .pipeline import RunResult, finalize_costs, log


def uncertainty(f: Finding, priority: list[str]) -> tuple[int, list[str]]:
    """Насколько агент не уверен в находке и почему. 0 — спрашивать человека незачем."""
    score, why = 0, []
    if f.judge == "disputed":
        score += 3
        why.append("критик нашёл в документе другое возможное толкование")
    if f.proof == "У" and f.severity == "blocker":
        score += 3
        why.append("блокирует разработку, а ответа в документе нет")
    elif f.proof == "У" and f.severity == "major":
        score += 1
        why.append("в документе нет ответа")
    if "одним ревьюером" in (f.status_note or ""):
        score += 1
        why.append("нашёл только один проход ревью")
    if score and f.fid in priority[:5]:
        score += 2
        why.append("критик считает вопрос одним из главных")
    return score, why


def _ordered_questions(result: RunResult) -> list[tuple[Finding, list[str]]]:
    """Вопросы только по находкам, где агент не уверен; самые неопределённые и тяжёлые — первыми."""
    scored = []
    for f in result.findings:
        if not f.question:
            continue
        score, why = uncertainty(f, result.priority)
        if score > 0:
            scored.append((-score, SEVERITY_ORDER[f.severity], f.fid, f, why))
    scored.sort(key=lambda t: t[:3])
    return [(f, why) for *_, f, why in scored]


def question_limit() -> int:
    return max(1, min(int(os.getenv("REQAUDIT_QUESTIONS", "3")), 10))


def pending_questions(result: RunResult, n: int | None = None) -> list[tuple[Finding, list[str]]]:
    """Вопросы, которые агент задал бы человеку: только там, где он не уверен, не больше лимита."""
    return _ordered_questions(result)[: n or question_limit()]


def _ask(questions: list[tuple[Finding, list[str]]], n: int) -> dict[str, str]:
    answers: dict[str, str] = {}
    asked = questions[:n]
    print(f"\n=== Агент не уверен в {len(asked)} местах и просит решения автора "
          "(Enter — пропустить, номер — выбрать вариант, или свой ответ) ===", file=sys.stderr)
    for k, (f, why) in enumerate(asked, start=1):
        q = f.question
        print(f"\n[{k}/{len(asked)}] {f.fid} · {f.title}", file=sys.stderr)
        print(f"Почему спрашиваю: {'; '.join(why)}.", file=sys.stderr)
        for qt in f.quotes[:2]:
            print(f"  строка {qt.line_start}: «{qt.text[:160]}»", file=sys.stderr)
        print(f"Вопрос{(' (' + q.addressee + ')') if q.addressee else ''}: {q.text}", file=sys.stderr)
        for i, o in enumerate(q.options, start=1):
            print(f"  {i}) {o}", file=sys.stderr)
        try:
            a = input("> ").strip()
        except EOFError:
            break
        if not a:
            continue
        if a.isdigit() and 1 <= int(a) <= len(q.options):
            a = q.options[int(a) - 1]
        answers[f.fid] = a
    return answers


def apply_answers(result: RunResult, *, cache_dir: Path | None, answers: dict[str, str] | None = None) -> None:
    """Ответы автора → перепроверка. answers=None — спросить в терминале; иначе ответы уже собраны (веб-приложение)."""
    if answers is None:
        questions = _ordered_questions(result)
        if not questions:
            log("агент уверен во всех находках — уточнять у автора нечего")
            return
        if not sys.stdin.isatty():
            log("нет интерактивного терминала — вопросы только в отчёте")
            return
        answers = _ask(questions, question_limit())
    by_id = {f.fid: f for f in result.findings}
    answers = {k: v.strip() for k, v in answers.items() if k in by_id and v and v.strip()}
    if not answers:
        return
    items = [{"id": fid, "title": by_id[fid].title, "explanation": by_id[fid].explanation[:500],
              "quotes": [{"line": q.line_start, "text": q.text} for q in by_id[fid].quotes],
              "question": by_id[fid].question.text if by_id[fid].question else "", "answer": a}
             for fid, a in answers.items()]
    updates = {}
    tracker = costs.current()
    clients = available_clients(result.stats, cache_dir, tracker=tracker)
    client = None
    if result.judge and result.judge.name in clients:
        client = clients[result.judge.name]
        if result.judge.model and result.judge.model != client.model:
            client = client.clone(result.judge.model)  # та же модель, что у критика
        if os.getenv("REQAUDIT_JUDGE_EFFORT"):
            client.reasoning_effort = os.environ["REQAUDIT_JUDGE_EFFORT"]
    elif clients:
        client = next(iter(clients.values()))
    if client:
        try:
            log(f"перепроверка по ответам: {client.label}")
            data = client.chat_json(prompts.RECHECK_SYSTEM,
                                    "Ответы автора (JSON):\n" + json.dumps(items, ensure_ascii=False) +
                                    "\n\nДокумент:\n<<<DOCUMENT\n" + result.doc.numbered_text() + "\nDOCUMENT>>>",
                                    max_tokens=6000, role="clarify")
            updates = {str(u.get("id")): u for u in data.get("updates") or [] if isinstance(u, dict)}
            result.steps["answers"] = "ok"
        except BudgetExceeded as e:
            result.steps["answers"] = "budget"
            result.budget_blocked.append(f"Перепроверка по ответам: {e}")
            result.failures.append(f"Перепроверка по ответам не выполнена: {str(e)[:200]}")
        except (LLMError, ValueError) as e:
            result.steps["answers"] = "error"
            result.failures.append(f"Перепроверка по ответам не выполнена: {str(e)[:200]}")
        finalize_costs(result, tracker)
    status_ru = {"resolved": "решение получено — исправить документ", "partial": "частично", "open": "остаётся открытым",
                 "not_defect": "снято по ответу автора: дефекта нет"}
    removed = []
    for fid, a in answers.items():
        u = updates.get(fid, {})
        f = by_id[fid]
        code = str(u.get("status", "")).lower()
        st = status_ru.get(code, "ответ получен")
        rewrite = str(u.get("rewrite") or "")
        result.answers.append({"fid": fid, "question": f.question.text if f.question else "", "answer": a,
                               "status": st, "rewrite": rewrite, "new_issue": str(u.get("new_issue") or "")})
        f.status_note = (f.status_note + " " if f.status_note else "") + f"Ответ автора: «{a}» — {st}."
        if code == "not_defect":
            f.judge, f.judge_note = "rejected", f"Снято по ответу автора: «{a[:200]}»"
            removed.append(f)
            continue
        if code == "resolved":
            f.proof = "Д"  # неопределённость снята человеком: что исправлять, теперь известно
        if rewrite:
            f.suggestion = rewrite
    if removed:
        result.rejected.extend(removed)
        result.findings = [f for f in result.findings if f not in removed]


def main(argv: list[str] | None = None) -> int:
    """python -m reqaudit.clarify <report.json> <answers.json> <новый report.md> — перепроверка по ответам автора."""
    import argparse

    from dotenv import load_dotenv

    root = Path(__file__).resolve().parent.parent
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(root / ".env")
    p = argparse.ArgumentParser(prog="reqaudit.clarify", description="Перепроверка находок по ответам автора ТЗ")
    p.add_argument("trace", help="report.json первого прогона")
    p.add_argument("answers", help='JSON {"F-01": "ответ", ...}')
    p.add_argument("output", help="куда записать обновлённый отчёт (.md; рядом .json/.html/.pdf)")
    args = p.parse_args(argv)

    from .report import render, write_source, write_trace
    from .report_html import render_html

    result = RunResult.model_validate_json(Path(args.trace).read_text(encoding="utf-8"))
    if not result.doc.lines:
        print("Ошибка: в report.json нет текста документа — перезапустите проверку новой версией агента.", file=sys.stderr)
        return 2
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    before = dict(result.costs or {})
    costs.start_run(document=result.doc.name, stats=result.stats, log=log)
    apply_answers(result, cache_dir=None, answers=answers)
    extra = (result.costs or {}).get("cost_usd") or 0
    result.costs = {**before, "cost_usd": round((before.get("cost_usd") or 0) + extra, 6)}
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    render(result, out)
    write_trace(result, out.with_suffix(".json"))
    write_source(result, out.with_suffix(".source.txt"))
    try:
        from .report_pdf import render_pdf

        render_pdf(result, out.with_suffix(".pdf"))
    except Exception as e:  # noqa: BLE001
        print(f"Внимание: PDF не создан ({e})", file=sys.stderr)
    render_html(result, out.with_suffix(".html"))
    log(f"перепроверено ответов: {len(result.answers)}; отчёт: {out}")
    return 0 if result.steps.get("answers") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
