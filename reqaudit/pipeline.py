"""Конвейер: документ → нарезка → правила + консилиум ревьюеров (параллельно) → проверка цитат →
склейка → критик (опровержение, дубли, приоритет) → вердикт."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from . import costs, prompts
from .criteria import SEVERITY_ORDER, criterion_for
from .grounding import Grounder, normalize
from .ingest import load_document
from .costs import BudgetExceeded
from .llm import PRIORITY, LLMClient, LLMError, available_clients
from .models import Document, Finding, ProviderInfo, Question, Quote, Requirement, RunStats
from .rules import run_rules
from .segment import KIND_RU, segment

CATEGORY_ORDER = ["contradiction", "incorrect", "unverifiable", "incompleteness", "ambiguity", "loophole", "security",
                  "non_singular", "terminology", "design", "traceability", "template", "other"]
FAMILY = {"contradiction": "conflict", "incorrect": "conflict", "incompleteness": "gap", "security": "gap",
          "ambiguity": "vague", "unverifiable": "vague", "loophole": "vague", "terminology": "vague",
          "template": "template", "traceability": "template", "design": "design", "non_singular": "design",
          "feasibility": "gap", "modifiability": "design", "ranked": "template"}
VALID_CATEGORIES = set(FAMILY) | {"other"}


class RunResult(BaseModel):
    doc: Document
    reqs: list[Requirement]
    findings: list[Finding]
    rejected: list[Finding] = Field(default_factory=list)
    dropped_ungrounded: list[Finding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    priority: list[str] = Field(default_factory=list)
    reviewers: list[ProviderInfo] = Field(default_factory=list)
    reviewer_labels: list[str] = Field(default_factory=list)
    judge: ProviderInfo | None = None
    judge_independent: bool = False
    failures: list[str] = Field(default_factory=list)
    stats: RunStats = Field(default_factory=RunStats)
    mode: str = "full"
    answers: list[dict] = Field(default_factory=list)
    costs: dict = Field(default_factory=dict)  # учёт расходов (reqaudit.costs.CostTracker.summary)
    budget_stop: str = ""  # причина остановки по бюджету (пусто — бюджет не исчерпан)
    finished_at: str = ""  # дата и время окончания прогона (для паспорта и HTML)
    budget_blocked: list[str] = Field(default_factory=list)  # отдельные вызовы, не поместившиеся в бюджет прогона
    coverage: dict[str, dict] = Field(default_factory=dict)
    ac_coverage: list[dict] = Field(default_factory=list)  # [{rid, covered_by: [R-xx критериев приёмки], gap}]  # R-xx → {"status": ok|defect|unchecked, "note": ...}
    steps: dict[str, str] = Field(default_factory=dict)  # шаг → ok | budget | error | skipped (для честного «Режима»)


def log(msg: str) -> None:
    print(f"[reqaudit] {msg}", file=sys.stderr, flush=True)


def req_table(reqs: list[Requirement]) -> str:
    rows = []
    for r in reqs:
        lines = f"{r.line_start}" if r.line_start == r.line_end else f"{r.line_start}-{r.line_end}"
        rows.append(f"{r.rid} | {r.doc_ref or '—'} | {lines} | {KIND_RU.get(r.kind, r.kind)} | {r.summary}")
    return "\n".join(rows)


def _to_findings(data: dict, label: str) -> list[Finding]:
    out = []
    for n, raw in enumerate(data.get("findings") or [], start=1):
        if not isinstance(raw, dict):
            continue
        cat = str(raw.get("category") or "other").strip().lower()
        cat = cat if cat in VALID_CATEGORIES else "other"
        sev = str(raw.get("severity") or "minor").strip().lower()
        sev = sev if sev in SEVERITY_ORDER else "minor"
        quotes = []
        for q in raw.get("quotes") or []:
            if isinstance(q, dict) and q.get("text"):
                line = q.get("line")
                try:
                    line = int(line) if line is not None else None
                except (TypeError, ValueError):
                    line = None
                quotes.append(Quote(text=str(q["text"]), line=line))
            elif isinstance(q, str):
                quotes.append(Quote(text=q))
        qraw = raw.get("question")
        question = None
        if isinstance(qraw, dict) and qraw.get("text"):
            question = Question(text=str(qraw["text"]), options=[str(o) for o in (qraw.get("options") or [])][:4],
                                addressee=str(qraw.get("addressee") or ""))
        elif isinstance(qraw, str) and qraw.strip():
            question = Question(text=qraw.strip())
        out.append(Finding(
            source=f"llm:{label}", category=cat, criterion=criterion_for(cat), severity=sev,
            proof="Д" if cat in ("contradiction", "incorrect") else ("Ш" if cat == "template" else "У"),
            rids=[str(x) for x in (raw.get("rids") or []) if x], quotes=quotes,
            title=str(raw.get("title") or "").strip() or "Без названия",
            explanation=str(raw.get("explanation") or "").strip(), scenario=str(raw.get("scenario") or "").strip(),
            impact=str(raw.get("impact") or "").strip(), suggestion=str(raw.get("suggestion") or "").strip(),
            question=question, roles=[str(x) for x in (raw.get("roles") or [])][:5],
            search_scope=str(raw.get("search_scope") or "").strip(), votes=[label], src_index=n,
            origins=[f"{label}#{n}"],
        ))
    return out


def _quote_sim(a: Finding, b: Finding) -> float:
    best = 0.0
    for qa in a.quotes:
        for qb in b.quotes:
            best = max(best, fuzz.partial_ratio(normalize(qa.text), normalize(qb.text)))
    return best


def _same_issue(a: Finding, b: Finding) -> bool:
    if FAMILY.get(a.category, a.category) != FAMILY.get(b.category, b.category):
        return False
    la, lb = a.lines(), b.lines()
    if not la or not lb:
        return False
    inter = len(la & lb)
    if not inter:
        return False
    jacc = inter / len(la | lb)
    title_sim = fuzz.token_set_ratio(normalize(a.title), normalize(b.title))
    if jacc >= 0.99 and len(la) >= 2:
        return True  # одинаковый набор мест (обычно противоречие двух пунктов)
    if jacc >= 0.99 and _quote_sim(a, b) >= 95:
        return True  # одна и та же цитата, один тип проблемы
    return jacc >= 0.5 and (_quote_sim(a, b) >= 75 and title_sim >= 45 or title_sim >= 70)


def merge_findings(findings: list[Finding]) -> list[Finding]:
    parent = list(range(len(findings)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(findings)):
        for j in range(i + 1, len(findings)):
            if findings[i].votes and findings[j].votes and set(findings[i].votes) == set(findings[j].votes) \
                    and not findings[i].is_rule:
                continue  # один и тот же ревьюер дважды — не голосование
            if _same_issue(findings[i], findings[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[Finding]] = {}
    for i, f in enumerate(findings):
        groups.setdefault(find(i), []).append(f)
    merged = []
    for members in groups.values():
        llm_members = [m for m in members if not m.is_rule]
        rep = max(llm_members or members, key=lambda m: (len(m.quotes), len(m.explanation) + len(m.scenario)))
        rep = rep.model_copy(deep=True)
        votes = []
        for m in members:
            for v in m.votes:
                if v not in votes:
                    votes.append(v)
        rep.votes = votes
        rep.origins = list(dict.fromkeys(o for m in members for o in m.origins))
        if len(members) > 1:  # тяжесть за число голосов не поднимаем: её заново оценивает критик
            seen = {(q.line_start, normalize(q.text)) for q in rep.quotes}
            for m in members:
                for q in m.quotes:
                    k = (q.line_start, normalize(q.text))
                    if k not in seen and len(rep.quotes) < 5:
                        rep.quotes.append(q)
                        seen.add(k)
                for fld in ("scenario", "impact", "suggestion", "search_scope"):
                    if not getattr(rep, fld) and getattr(m, fld):
                        setattr(rep, fld, getattr(m, fld))
                if "Разработчик А" in m.scenario and "Разработчик А" not in rep.scenario:
                    rep.scenario = m.scenario  # сценарий «два разработчика» — самое наглядное доказательство
                if not rep.question and m.question:
                    rep.question = m.question
                rep.rids = sorted(set(rep.rids) | set(m.rids))
                rep.roles = list(dict.fromkeys(rep.roles + m.roles))
            if any(m.is_rule for m in members):
                rep.source = "rule+llm" if llm_members else rep.source
        merged.append(rep)
    return merged


def _sort_key(f: Finding):
    first = min(f.lines()) if f.lines() else 10**6
    return (SEVERITY_ORDER.get(f.severity, 3), {"Д": 0, "У": 1, "Ш": 2}.get(f.proof, 3),
            CATEGORY_ORDER.index(f.category) if f.category in CATEGORY_ORDER else 99, first)


def _compact(f: Finding, fid: str, with_text: bool = True) -> dict:
    d = {"id": fid, "category": f.category, "severity": f.severity, "proof": f.proof, "title": f.title,
         "votes": f.votes, "rids": f.rids,
         "quotes": [{"line": q.line_start, "text": q.text[:200]} for q in f.quotes]}
    if with_text:
        d.update({"explanation": f.explanation[:700], "scenario": f.scenario[:400], "suggestion": f.suggestion[:400]})
    if f.search_scope:
        d["search_scope"] = f.search_scope
    return d


def pick_roles(clients: dict):
    names = list(clients)
    env_rev = [x.strip() for x in os.getenv("REQAUDIT_REVIEWERS", "").split(",") if x.strip() in clients]
    reviewers = env_rev or names[:3]
    judge_env = os.getenv("REQAUDIT_JUDGE", "").strip()
    spare = [n for n in names if n not in reviewers]
    judge = judge_env if judge_env in clients else (spare[0] if spare else (reviewers[-1] if reviewers else None))
    # модель роли подменяем, только если роль досталась тому провайдеру, что указан в .env
    # (иначе, например, при одном ключе Gemini получили бы запрос claude-opus-5-5 к Gemini)
    jm = os.getenv("REQAUDIT_JUDGE_MODEL", "").strip()
    if judge and jm and judge == judge_env and jm != clients[judge].model:
        clients["judge"] = clients[judge].clone(jm)  # работает для любого провайдера (anthropic, openai, ...)
        judge = "judge"
    return reviewers, judge


def _budget_stopped(result: RunResult, e: BudgetExceeded, step: str) -> None:
    if getattr(e, "kind", "run") == "daily":
        if not result.budget_stop:
            result.budget_stop = str(e)  # дневной лимит — дальше ничего не вызываем
    else:
        result.budget_blocked.append(f"{step}: {e}")  # пропущен только этот шаг
    key = {"Судья": "judge"}.get(step)
    if key:
        result.steps[key] = "budget"
    result.failures.append(f"{step} не выполнен: {e}")
    log(f"  {step}: {e}")


def finalize_costs(result: RunResult, tracker) -> None:
    """Итоги расходов в результат прогона (паспорт, report.json) и проверка аномалии."""
    if tracker is None:
        return
    result.costs = tracker.summary()
    if tracker.exhausted and not result.budget_stop:
        result.budget_stop = tracker.exhausted


def run(doc_path: str, *, use_llm: bool = True, cache_dir: Path | None = None,
        force_budget = False) -> RunResult:
    """Проверка документа как граф LangGraph: узлы — шаги конвейера, общее состояние — результат прогона.

    ingest (текст, нарезка, правила) → review (5 ревьюеров параллельно + покрытие приёмкой, проверка цитат)
    → merge (склейка) → critic (опровержение; пропускается, если нечего проверять) → finalize (калибровка, вердикт).
    """
    from langgraph.graph import END, START, StateGraph

    _label = ac_future = ac_pass = ac_rids = alive = all_findings = anomaly = blocked = by_id = changes = cid = cites_line = client = clients = d_of = data = doc = down = dup = dup_map = e = est = ex = f = fld = foci = focus = focus_env = fut = futures = grounder = i = ids = judge = judge_name = kept = kind_by_rid = kinds = kinds_of = label = llm_votes = merged = n_llm_voters = n_var = name = new_proof = new_sev = notes = numbered = ok_rev = payload = planned = prio_objs = protected = q = raw_cov = reqs = result = review = reviewers = rule_findings = seen_q = stats = t = t0 = tasks = tgt = tracker = unpriced = user = v = val = verdict = vote = None
    t0 = time.time()

    def n_ingest(state: dict) -> dict:
        """Текст с номерами строк, нарезка на требования, детерминированные правила, проверка их цитат."""
        nonlocal _label, ac_future, ac_pass, ac_rids, alive, all_findings, anomaly, blocked, by_id, changes, cid, cites_line, client, clients, d_of, data, doc, down, dup, dup_map, e, est, ex, f, fld, foci, focus, focus_env, fut, futures, grounder, i, ids, judge, judge_name, kept, kind_by_rid, kinds, kinds_of, label, llm_votes, merged, n_llm_voters, n_var, name, new_proof, new_sev, notes, numbered, ok_rev, payload, planned, prio_objs, protected, q, raw_cov, reqs, result, review, reviewers, rule_findings, seen_q, stats, t, t0, tasks, tgt, tracker, unpriced, user, v, val, verdict, vote
        stats = RunStats()
        doc = load_document(doc_path)
        log(f"документ: {doc.name}, строк: {len(doc.lines)}" + (f", страниц: {doc.pages}" if doc.pages else ""))
        reqs = segment(doc)
        log(f"нарезка: {len(reqs)} требований")
        rule_findings, notes = run_rules(doc, reqs)
        log(f"правила: {len(rule_findings)} находок")
        grounder = Grounder(doc, reqs)
        result = RunResult(doc=doc, reqs=reqs, findings=[], notes=notes, stats=stats)
        all_findings = []
        for f in rule_findings:
            if grounder.ground(f):
                all_findings.append(f)
            else:
                result.dropped_ungrounded.append(f)
        return {"trail": state.get("trail", []) + ["ingest"]}

    def n_review(state: dict) -> dict:
        """Пять независимых ревьюеров и покрытие критериями приёмки параллельно; цитаты сверяет код."""
        nonlocal _label, ac_future, ac_pass, ac_rids, alive, all_findings, anomaly, blocked, by_id, changes, cid, cites_line, client, clients, d_of, data, doc, down, dup, dup_map, e, est, ex, f, fld, foci, focus, focus_env, fut, futures, grounder, i, ids, judge, judge_name, kept, kind_by_rid, kinds, kinds_of, label, llm_votes, merged, n_llm_voters, n_var, name, new_proof, new_sev, notes, numbered, ok_rev, payload, planned, prio_objs, protected, q, raw_cov, reqs, result, review, reviewers, rule_findings, seen_q, stats, t, t0, tasks, tgt, tracker, unpriced, user, v, val, verdict, vote
        tracker = costs.start_run(document=doc.name, stats=stats, log=log) if use_llm else None
        clients = available_clients(stats, cache_dir, tracker=tracker) if use_llm else {}
        if use_llm and not clients:
            log("LLM-ключи не найдены — работаю только на правилах (см. .env.example)")
        if not clients:
            result.mode = "rules-only"
        else:
            reviewers, judge_name = pick_roles(clients)
            tasks = []
            for i, name in enumerate(reviewers):
                focus_env = os.getenv(f"{name.upper()}_FOCUS", "").strip()
                n_var = os.getenv(f"{name.upper()}_VARIANTS") or os.getenv("REQAUDIT_VARIANTS")
                if focus_env:
                    foci = [int(x) for x in focus_env.split(",") if x.strip().isdigit() and int(x) in prompts.FOCUS]
                elif n_var:
                    foci = [prompts.PASSES[(i + v) % len(prompts.PASSES)] for v in range(int(n_var))]
                else:
                    # по умолчанию три прохода (каждое требование / 5 ролей / два разработчика) делятся между ревьюерами
                    foci = [f for k, f in enumerate(prompts.PASSES) if k % len(reviewers) == i]
                for v, focus in enumerate(foci):
                    label = name if len(foci) == 1 else f"{name}#{v + 1}"
                    tasks.append((clients[name], focus, label))
            result.reviewers = [clients[n].info for n in reviewers]
            result.reviewer_labels = [t[2] for t in tasks]
            numbered = doc.numbered_text()
            if len(numbered) > 150_000:
                result.failures.append(f"Документ большой ({len(numbered)} символов): модели получают его целиком, "
                                       "возможна неполнота анализа удалённых разделов.")
            user = prompts.reviewer_user(req_table(reqs), numbered)

            # ---- предварительная оценка стоимости прогона
            planned = []
            for client, focus, _label in tasks:
                planned.append((client.model, "reviewer", len(prompts.reviewer_system(focus)) + len(user)))
            planned.append((clients[judge_name].model, "judge",
                            len(prompts.JUDGE_SYSTEM) + len(prompts.judge_user("", numbered)) + 40_000))
            est, unpriced = tracker.preflight(planned)
            log(f"оценка стоимости прогона: ${est:.2f} за {len(planned)} вызовов"
                + (f" (+ н/д для {', '.join(unpriced)})" if unpriced else "")
                + (f"; бюджет прогона ${tracker.run_limit:.2f}" if tracker.run_limit >= 0 else ""))
            if tracker.run_limit >= 0 and est > tracker.run_limit and not force_budget:
                raise BudgetExceeded(f"Оценка стоимости прогона {costs.fmt_usd(est)} превышает бюджет REQAUDIT_BUDGET_USD="
                                     f"{costs.fmt_usd(tracker.run_limit)}. Прогон не начат; увеличьте бюджет или запустите "
                                     f"с --force-budget.", limit_usd=tracker.run_limit, preflight=True)
            log(f"консилиум: {', '.join(t[2] for t in tasks)} (параллельно)")

            raw_cov = {}  # покрытие по требованиям от прохода 0: метка прохода → тройки
            ac_rids = [r.rid for r in reqs if r.kind == "AC"]

            def ac_pass():
                """Какие требования проверяются критериями приёмки и чего не хватает (параллельно ревьюерам)."""
                started = time.time()
                client = clients[reviewers[0]]
                data = client.chat_json(prompts.AC_SYSTEM, user, max_tokens=24000, role="coverage",
                                        deadline=float(os.getenv("REQAUDIT_REVIEW_DEADLINE", "0")))
                known = {r.rid: r for r in reqs}
                rows = []
                for it in data.get("ac_coverage") or []:
                    if not isinstance(it, dict) or str(it.get("rid")) not in known:
                        continue
                    rid = str(it["rid"])
                    if known[rid].kind in ("AC", "GOAL", "REQ"):
                        continue
                    by = [str(x) for x in (it.get("covered_by") or []) if str(x) in ac_rids]
                    rows.append({"rid": rid, "covered_by": sorted(set(by), key=by.index),
                                 "gap": str(it.get("gap") or "")[:300]})
                covered = sum(1 for x in rows if x["covered_by"])
                log(f"  покрытие критериями приёмки: {covered} из {len(rows)} требований за {time.time() - started:.0f} с")
                return rows


            def review(task):
                client, variant, label = task
                started = time.time()
                data = client.chat_json(prompts.reviewer_system(variant), user, max_tokens=64000,
                                        deadline=float(os.getenv("REQAUDIT_REVIEW_DEADLINE", "0")), role="reviewer")
                fs = _to_findings(data, label)
                if variant == 0 and isinstance(data.get("coverage"), list):
                    raw_cov[label] = data["coverage"]
                log(f"  ревьюер {label}: {len(fs)} находок за {time.time() - started:.0f} с")
                return fs

            with ThreadPoolExecutor(max_workers=max(1, len(tasks)) + 1) as ex:
                ac_future = ex.submit(ac_pass) if ac_rids else None
                futures = [(t, ex.submit(review, t)) for t in tasks]
                for t, fut in futures:
                    try:
                        for f in fut.result():
                            if grounder.ground(f):
                                all_findings.append(f)
                            else:
                                result.dropped_ungrounded.append(f)
                    except BudgetExceeded as e:
                        _budget_stopped(result, e, f"Ревьюер {t[2]}")
                    except (LLMError, ValueError) as e:
                        result.failures.append(f"Ревьюер {t[2]} недоступен: {str(e)[:200]}")
                        log(f"  ревьюер {t[2]} упал: {str(e)[:160]}")
                if ac_future is not None:
                    try:
                        result.ac_coverage = ac_future.result()
                        result.steps["ac"] = "ok"
                    except BudgetExceeded as e:
                        _budget_stopped(result, e, "Покрытие критериями приёмки")
                    except (LLMError, ValueError) as e:
                        result.steps["ac"] = "error"
                        result.failures.append(f"Покрытие критериями приёмки не построено: {str(e)[:200]}")
            ok_rev = len(tasks) - sum(1 for x in result.failures if x.startswith("Ревьюер ")) \
                - sum(1 for x in result.budget_blocked if x.startswith("Ревьюер "))
            result.steps["reviewers"] = f"{max(ok_rev, 0)}/{len(tasks)}"
            result.coverage = {"_raw": raw_cov}
            log(f"проверка цитат: подтверждено {len(all_findings)}, отброшено {len(result.dropped_ungrounded)}")
        return {"trail": state.get("trail", []) + ["review"]}

    def n_merge(state: dict) -> dict:
        """Склейка одинаковых находок кодом, временные номера для критика."""
        nonlocal _label, ac_future, ac_pass, ac_rids, alive, all_findings, anomaly, blocked, by_id, changes, cid, cites_line, client, clients, d_of, data, doc, down, dup, dup_map, e, est, ex, f, fld, foci, focus, focus_env, fut, futures, grounder, i, ids, judge, judge_name, kept, kind_by_rid, kinds, kinds_of, label, llm_votes, merged, n_llm_voters, n_var, name, new_proof, new_sev, notes, numbered, ok_rev, payload, planned, prio_objs, protected, q, raw_cov, reqs, result, review, reviewers, rule_findings, seen_q, stats, t, t0, tasks, tgt, tracker, unpriced, user, v, val, verdict, vote
        merged = merge_findings(all_findings)
        merged.sort(key=_sort_key)
        n_llm_voters = len(result.reviewer_labels)
        for f in merged:
            llm_votes = [v for v in f.votes if v != "правила"]
            if n_llm_voters >= 2 and len(llm_votes) == 1 and "правила" not in f.votes:
                f.status_note = (f.status_note + " " if f.status_note else "") + f"Найдено одним ревьюером из {n_llm_voters}."
        log(f"склейка: {len(merged)} уникальных находок")

        # Отдельного оркестратора нет: тексты находок остаются авторскими (от ревьюеров), а дубли, приоритет вопросов
        # и опровержение делает один вызов-критик (судья).
        prio_objs = []
        merged.sort(key=_sort_key)
        ids = {f"C-{n:02d}": f for n, f in enumerate(merged, start=1)}  # временные номера для критика
        dup_map = {}
        return {"trail": state.get("trail", []) + ["merge"]}

    def n_critic(state: dict) -> dict:
        """Критик пытается опровергнуть каждую находку, склеивает дубли, расставляет приоритет вопросов."""
        nonlocal _label, ac_future, ac_pass, ac_rids, alive, all_findings, anomaly, blocked, by_id, changes, cid, cites_line, client, clients, d_of, data, doc, down, dup, dup_map, e, est, ex, f, fld, foci, focus, focus_env, fut, futures, grounder, i, ids, judge, judge_name, kept, kind_by_rid, kinds, kinds_of, label, llm_votes, merged, n_llm_voters, n_var, name, new_proof, new_sev, notes, numbered, ok_rev, payload, planned, prio_objs, protected, q, raw_cov, reqs, result, review, reviewers, rule_findings, seen_q, stats, t, t0, tasks, tgt, tracker, unpriced, user, v, val, verdict, vote
        # ---------------------------------------------------------------- судья
        if clients and merged and tracker.exhausted:
            result.failures.append("Судья пропущен: исчерпан бюджет — находки не перепроверены.")
            result.steps["judge"] = "budget"
        elif clients and merged:
            judge = clients[judge_name]
            if os.getenv("REQAUDIT_JUDGE_EFFORT"):
                judge.reasoning_effort = os.environ["REQAUDIT_JUDGE_EFFORT"]
            result.judge = judge.info
            result.judge_independent = judge.label not in [clients[n].label for n in reviewers]
            payload = json.dumps([_compact(f, cid) for cid, f in ids.items() if f.proof != "Ш"], ensure_ascii=False)
            try:
                log(f"критик: {judge.label}" + ("" if result.judge_independent else " (отдельный вызов той же модели)"))
                data = judge.chat_json(prompts.JUDGE_SYSTEM, prompts.judge_user(payload, doc.numbered_text()), max_tokens=40000,
                                       role="judge")
                by_id = ids
                kinds_of = {r.rid: r.kind for r in reqs}
                dup = set()
                for v in data.get("verdicts") or []:
                    cid = str(v.get("id") or "")
                    f = by_id.get(cid)
                    if not f:
                        continue
                    d_of = str(v.get("duplicate_of") or "")
                    if d_of and d_of in by_id and d_of != cid and d_of not in dup_map:
                        tgt = by_id[d_of]  # та же проблема: голоса, требования и цитаты переносим в основную находку
                        for vote in f.votes:
                            if vote not in tgt.votes:
                                tgt.votes.append(vote)
                        tgt.rids = sorted(set(tgt.rids) | set(f.rids))
                        tgt.origins = list(dict.fromkeys(tgt.origins + f.origins))
                        seen_q = {(q.line_start, normalize(q.text)) for q in tgt.quotes}
                        for q in f.quotes:
                            if (q.line_start, normalize(q.text)) not in seen_q and len(tgt.quotes) < 5:
                                tgt.quotes.append(q)
                        if not tgt.question and f.question:
                            tgt.question = f.question
                        if not tgt.scenario and f.scenario:
                            tgt.scenario = f.scenario
                        dup.add(cid)
                        dup_map[cid] = d_of
                        continue
                    verdict = str(v.get("verdict") or "").lower()
                    f.judge = verdict if verdict in ("confirmed", "disputed", "rejected") else ""
                    f.judge_note = str(v.get("note") or "")[:300]
                    _apply_patch(f, v.get("patch"))
                    cites_line = any(1 <= int(n) <= len(doc.lines) for n in
                                     re.findall(r"стр(?:ок\w*|\.)?\s*(\d+)", f.judge_note, flags=re.I))
                    new_proof = str(v.get("proof") or "")
                    new_sev = str(v.get("severity") or "")
                    # Доказанное противоречие в ФТ / критериях приёмки (≥2 несовместимые цитаты) судья может только
                    # отклонить с обоснованием — понижать тяжесть или доказанность ему не даём.
                    protected = (f.category == "contradiction" and f.proof == "Д"
                                 and len({q.line_start for q in f.quotes}) >= 2
                                 and bool({kinds_of.get(r) for r in f.rids} & {"FR", "AC"}))
                    changes, blocked = [], []
                    if new_proof in ("Д", "У", "Ш") and new_proof != f.proof:
                        if (f.is_rule and f.proof == "Д") or (protected and f.judge != "rejected" and not cites_line):
                            blocked.append(f"доказанность {f.proof}→{new_proof}")
                        else:
                            changes.append(f"доказанность {f.proof}→{new_proof}")
                            f.proof = new_proof
                    if new_sev in SEVERITY_ORDER and new_sev != f.severity:
                        down = SEVERITY_ORDER[new_sev] > SEVERITY_ORDER[f.severity]
                        if protected and down and f.judge != "rejected" and not cites_line:
                            blocked.append(f"тяжесть {f.severity}→{new_sev}")
                        else:
                            changes.append(f"тяжесть {f.severity}→{new_sev}")
                            f.severity = new_sev
                    f.judge_change = "; ".join(changes)
                    if blocked:
                        f.judge_change = (f.judge_change + "; " if f.judge_change else "") + \
                            "не принято без ссылки на строку (доказанное противоречие): " + ", ".join(blocked)
                merged = [f for cid, f in ids.items() if cid not in dup]
                prio_objs = [ids[str(x)] for x in (data.get("priority") or []) if str(x) in ids and str(x) not in dup]
                if dup:
                    log(f"  критик склеил дублей: {len(dup)}")
                kept = []
                for f in merged:
                    # судья может отклонить любую находку, в том числе от правил: на незнакомом документе правило
                    # может сработать ложно; отклонённые остаются в трассировке (report.json)
                    (result.rejected if f.judge == "rejected" else kept).append(f)
                merged = kept
                result.steps["judge"] = "ok"
            except BudgetExceeded as e:
                _budget_stopped(result, e, "Судья")
            except (LLMError, ValueError) as e:
                result.steps["judge"] = "error"
                result.failures.append(f"Судья недоступен: {str(e)[:200]} — находки не перепроверены.")
                log(f"  судья упал: {str(e)[:160]}")
        return {"trail": state.get("trail", []) + ["critic"]}

    def n_finalize(state: dict) -> dict:
        """Детерминированная калибровка тяжести, итоговые номера, покрытие, учёт расходов."""
        nonlocal _label, ac_future, ac_pass, ac_rids, alive, all_findings, anomaly, blocked, by_id, changes, cid, cites_line, client, clients, d_of, data, doc, down, dup, dup_map, e, est, ex, f, fld, foci, focus, focus_env, fut, futures, grounder, i, ids, judge, judge_name, kept, kind_by_rid, kinds, kinds_of, label, llm_votes, merged, n_llm_voters, n_var, name, new_proof, new_sev, notes, numbered, ok_rev, payload, planned, prio_objs, protected, q, raw_cov, reqs, result, review, reviewers, rule_findings, seen_q, stats, t, t0, tasks, tgt, tracker, unpriced, user, v, val, verdict, vote
        # Детерминированная калибровка тяжести: подтверждённое противоречие в ключевой логике (ФТ / критерии приёмки)
        # по определению блокирует разработку — не даём модели «плавать» между прогонами.
        kind_by_rid = {r.rid: r.kind for r in reqs}
        for f in merged:
            kinds = {kind_by_rid.get(r) for r in f.rids}
            # «противоречие» и «некорректность» с двумя несовместимыми цитатами — по сути одно и то же
            if (f.category in ("contradiction", "incorrect") and f.proof == "Д"
                    and (not f.judge or (f.judge == "confirmed" and "тяжесть" not in (f.judge_change or "")))
                    and len({q.line_start for q in f.quotes}) >= 2
                    and (kinds & {"FR", "API", "INT", "REQ"}) and f.severity != "blocker"):
                f.severity = "blocker"
                f.status_note = (f.status_note + " " if f.status_note else "") + \
                    "Тяжесть повышена до блокера правилом: доказанное противоречие в функциональных требованиях или контракте API."

        merged.sort(key=_sort_key)
        for i, f in enumerate(merged, start=1):
            f.fid = f"F-{i:02d}"
        result.findings = merged
        alive = {id(f) for f in merged}
        result.priority = [f.fid for f in prio_objs if id(f) in alive]
        result.coverage = build_coverage(reqs, merged, result)
        for f in merged:  # временные номера C-xx в текстах → итоговые F-xx
            for fld in ("explanation", "scenario", "impact", "suggestion", "status_note", "judge_note"):
                val = getattr(f, fld, "")
                if isinstance(val, str) and "C-" in val:
                    setattr(f, fld, _remap_prose(val, alive, ids, dup_map))
        stats.seconds = time.time() - t0
        result.finished_at = datetime.now().strftime("%d.%m.%Y %H:%M")
        if tracker is not None:
            anomaly = tracker.anomaly()
            if anomaly:
                result.failures.append(anomaly)
            finalize_costs(result, tracker)
            log(f"стоимость прогона: {costs.passport_line(result.costs)}")
        log(f"готово: {len(merged)} находок, вызовов LLM: {stats.llm_calls} (кэш: {stats.llm_cached}), {stats.seconds:.0f} с")
        return {"trail": state.get("trail", []) + ["finalize"]}

    graph = StateGraph(dict)
    for name, fn in (("ingest", n_ingest), ("review", n_review), ("merge", n_merge), ("critic", n_critic),
                     ("finalize", n_finalize)):
        graph.add_node(name, fn)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "review")
    graph.add_edge("review", "merge")
    graph.add_conditional_edges("merge", lambda s: "critic" if clients and merged else "finalize",
                                {"critic": "critic", "finalize": "finalize"})
    graph.add_edge("critic", "finalize")
    graph.add_edge("finalize", END)
    state = graph.compile().invoke({"trail": []})
    result.steps["graph"] = " → ".join(state.get("trail", []))
    return result


COV_OK = {1: "условия и результат определены", 2: "тип и ограничения определены", 3: "назначение и связи ясны"}
COV_SKIP = {1: "не хватает контекста", 2: "не удалось разобрать текст", 3: "проверка не завершена"}


def build_coverage(reqs, findings: list[Finding], result: RunResult) -> dict[str, dict]:
    """Честный статус по каждому требованию: «дефект» / «проверено, дефектов нет» / «не проверено».

    Источник — тройки [числовая часть R-xx, статус, основание] от прохода 0. Требование без корректной записи
    считается непроверенным: отсутствие замечаний само по себе ничего не доказывает.
    """
    raw = (result.coverage or {}).get("_raw") or {}
    known = {r.rid for r in reqs}
    by_rid: dict[str, list[Finding]] = {}
    for f in findings:
        for rid in f.rids:
            by_rid.setdefault(rid, []).append(f)
    live_by_origin = {o: f for f in findings for o in f.origins}
    rejected_origins = {o for f in result.rejected for o in f.origins}
    entries: dict[str, tuple[int, int, str]] = {}
    bad: set[str] = set()
    for label, triples in raw.items():
        for t in triples if isinstance(triples, list) else []:
            if not (isinstance(t, list) and len(t) == 3 and all(type(x) is int for x in t)):
                continue  # не тройка целых чисел — игнорируем, требование останется «не проверено»
            rid, status, reason = f"R-{t[0]:02d}", t[1], t[2]
            if rid not in known or status not in (0, 1, 2):
                continue
            if (status == 0 and reason not in COV_OK) or (status == 2 and reason not in COV_SKIP) or \
                    (status == 1 and reason < 1):
                bad.add(rid)
                continue
            if rid in entries:  # повторная запись об одном требовании — не доверяем ни одной
                bad.add(rid)
                continue
            entries[rid] = (status, reason, label)
    out: dict[str, dict] = {}
    for r in reqs:
        live = by_rid.get(r.rid, [])
        if live:
            out[r.rid] = {"status": "defect", "note": ", ".join(f.fid for f in live)}
            continue
        e = None if r.rid in bad else entries.get(r.rid)
        if e is None:
            why = "только правила, без модели" if result.mode == "rules-only" else \
                ("проход покрытия не выполнен" if not raw else
                 ("некорректная запись о проверке в ответе модели" if r.rid in bad else "нет в ответе модели"))
            out[r.rid] = {"status": "unchecked", "note": why}
        elif e[0] == 0:
            out[r.rid] = {"status": "ok", "note": COV_OK[e[1]]}
        elif e[0] == 1:
            key = f"{e[2]}#{e[1]}"
            if key in live_by_origin:  # замечание живо, но после склейки привязано к другим требованиям
                out[r.rid] = {"status": "defect", "note": live_by_origin[key].fid}
            elif key in rejected_origins:
                out[r.rid] = {"status": "ok", "note": "оценено; единственное замечание проверено и снято критиком"}
            else:
                out[r.rid] = {"status": "unchecked",
                              "note": "замечание модели не прошло проверку цитат — нужна повторная проверка"}
        else:
            out[r.rid] = {"status": "unchecked", "note": COV_SKIP[e[1]]}
    return out


def _apply_patch(f: Finding, patch) -> None:
    """Минимальная правка полей от критика: убрать неподтверждённое утверждение, поправить вопрос."""
    if not isinstance(patch, dict) or not patch:
        return
    changed = []
    for fld in ("title", "explanation", "impact", "suggestion"):
        val = patch.get(fld)
        if isinstance(val, str) and val.strip() and val.strip() != getattr(f, fld):
            setattr(f, fld, val.strip())
            changed.append(fld)
    sc = patch.get("scenario")
    if isinstance(sc, str) and sc.strip() != f.scenario:
        if not ("Разработчик А" in f.scenario and sc.strip() and "Разработчик А" not in sc):
            f.scenario = sc.strip()  # пустая строка — убрать неподтверждённый сценарий
            changed.append("scenario")
    q = patch.get("question")
    if isinstance(q, dict) and isinstance(q.get("text"), str):
        opts = q.get("options")
        opts = [o.strip() for o in opts if isinstance(o, str) and o.strip()][:4] if isinstance(opts, list) else []
        addressee = q.get("addressee") if isinstance(q.get("addressee"), str) else (f.question.addressee if f.question else "")
        f.question = Question(text=q["text"].strip(), options=opts, addressee=addressee) if q["text"].strip() else None
        changed.append("question")
    if changed:
        f.status_note = (f.status_note + " " if f.status_note else "") + f"Текст уточнён критиком: {', '.join(changed)}."


def _remap_prose(text: str, alive: set[int], ids: dict[str, Finding], dup_map: dict[str, str]) -> str:
    """C-xx → F-xx (через цепочку дублей); если находки нет в итоге — «другое замечание»."""
    import re

    def repl(m):
        cid = m.group(0)
        seen = set()
        while cid in dup_map and cid not in seen:  # дубль → основная находка
            seen.add(cid)
            cid = dup_map[cid]
        f = ids.get(cid)
        return f.fid if f is not None and id(f) in alive else "другое замечание"

    return re.sub(r"\bC-\d{2,3}\b", repl, text)
