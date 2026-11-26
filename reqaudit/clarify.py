"""Человек в контуре: ответы на уточняющие вопросы → перепроверка → исправленные формулировки (ТЗ v2).

Режим --interactive: агент задаёт главные вопросы в терминале (REQAUDIT_QUESTIONS, по умолчанию 5),
ответы и перепроверка — в рамках того же прогона.
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


def _ordered_questions(result: RunResult) -> list[Finding]:
    with_q = [f for f in result.findings if f.question]
    prio = [f for fid in result.priority for f in with_q if f.fid == fid]
    rest = sorted([f for f in with_q if f not in prio], key=lambda f: (SEVERITY_ORDER[f.severity], f.fid))
    return prio + rest


def _ask(findings: list[Finding], n: int) -> dict[str, str]:
    answers: dict[str, str] = {}
    print("\n=== Уточняющие вопросы (Enter — пропустить, номер — выбрать вариант, или свой ответ) ===", file=sys.stderr)
    for f in findings[:n]:
        q = f.question
        print(f"\n[{f.fid}] {f.title}\nВопрос{(' (' + q.addressee + ')') if q.addressee else ''}: {q.text}", file=sys.stderr)
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


def apply_answers(result: RunResult, *, cache_dir: Path | None) -> None:
    questions = _ordered_questions(result)
    if not questions:
        return
    if not sys.stdin.isatty():
        log("нет интерактивного терминала — вопросы только в отчёте")
        return
    answers = _ask(questions, int(os.getenv("REQAUDIT_QUESTIONS", "5")))
    by_id = {f.fid: f for f in result.findings}
    answers = {k: v for k, v in answers.items() if k in by_id}
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
    if result.orchestrator and result.orchestrator.name in clients:
        client = clients[result.orchestrator.name]
        if result.orchestrator.model and result.orchestrator.model != client.model:
            client = client.clone(result.orchestrator.model)  # та же модель, что у оркестратора
        if os.getenv("REQAUDIT_ORCH_EFFORT"):
            client.reasoning_effort = os.environ["REQAUDIT_ORCH_EFFORT"]
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
    status_ru = {"resolved": "решение получено — исправить документ", "partial": "частично", "open": "остаётся открытым"}
    for fid, a in answers.items():
        u = updates.get(fid, {})
        f = by_id[fid]
        st = status_ru.get(str(u.get("status", "")).lower(), "ответ получен")
        rewrite = str(u.get("rewrite") or "")
        result.answers.append({"fid": fid, "question": f.question.text if f.question else "", "answer": a,
                               "status": st, "rewrite": rewrite, "new_issue": str(u.get("new_issue") or "")})
        f.status_note = (f.status_note + " " if f.status_note else "") + f"Ответ автора: «{a}» — {st}."
        if rewrite:
            f.suggestion = rewrite
