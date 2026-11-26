"""Конвейер: документ → нарезка → правила + консилиум ревьюеров (параллельно) → проверка цитат →
склейка и голосование → оркестратор → судья → вердикт."""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from . import prompts
from .criteria import SEVERITY_ORDER, criterion_for
from .grounding import Grounder, normalize
from .ingest import load_document
from .llm import PRIORITY, LLMClient, LLMError, available_clients
from .models import Document, Finding, ProviderInfo, Question, Quote, Requirement, RunStats
from .rules import run_rules
from .segment import KIND_RU, segment

CATEGORY_ORDER = ["contradiction", "incorrect", "unverifiable", "incompleteness", "ambiguity", "loophole", "security",
                  "non_singular", "terminology", "design", "traceability", "template", "other"]
FAMILY = {"contradiction": "conflict", "incorrect": "conflict", "incompleteness": "gap", "security": "gap",
          "ambiguity": "vague", "unverifiable": "vague", "loophole": "vague", "terminology": "vague",
          "template": "template", "traceability": "template", "design": "design", "non_singular": "design"}
VALID_CATEGORIES = set(FAMILY) | {"other"}


class RunResult(BaseModel):
    doc: Document
    reqs: list[Requirement]
    findings: list[Finding]
    rejected: list[Finding] = Field(default_factory=list)
    dropped_ungrounded: list[Finding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    priority: list[str] = Field(default_factory=list)
    checklist: list[dict] = Field(default_factory=list)
    readiness: dict[str, str] = Field(default_factory=dict)
    reviewers: list[ProviderInfo] = Field(default_factory=list)
    reviewer_labels: list[str] = Field(default_factory=list)
    orchestrator: ProviderInfo | None = None
    judge: ProviderInfo | None = None
    judge_independent: bool = False
    failures: list[str] = Field(default_factory=list)
    stats: RunStats = Field(default_factory=RunStats)
    mode: str = "full"
    answers: list[dict] = Field(default_factory=list)


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
    for raw in data.get("findings") or []:
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
            search_scope=str(raw.get("search_scope") or "").strip(), votes=[label],
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
        if len(members) > 1:
            rep.severity = min((m.severity for m in members), key=lambda s: SEVERITY_ORDER[s]) \
                if len(set(votes)) >= 2 else rep.severity
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


def pick_roles(clients: dict[str, LLMClient]):
    names = list(clients)
    env_rev = [x.strip() for x in os.getenv("REQAUDIT_REVIEWERS", "").split(",") if x.strip() in clients]
    reviewers = env_rev or names[:3]
    judge_env = os.getenv("REQAUDIT_JUDGE", "").strip()
    spare = [n for n in names if n not in reviewers]
    judge = judge_env if judge_env in clients else (spare[0] if spare else (reviewers[-1] if reviewers else None))
    orch_env = os.getenv("REQAUDIT_ORCHESTRATOR", "").strip()
    orch = orch_env if orch_env in clients else (reviewers[0] if reviewers else None)
    if judge and judge == orch and len(reviewers) > 1 and not judge_env and not spare:
        judge = reviewers[1]
    jm = os.getenv("REQAUDIT_JUDGE_MODEL", "").strip()
    if judge and jm and jm != clients[judge].model:
        base = clients[judge]
        clients["judge"] = LLMClient(base.name, base.company, jm, base.url, base.api_key, base.stats, base.cache_dir)
        judge = "judge"
    return reviewers, orch, judge


def run(doc_path: str, *, use_llm: bool = True, cache_dir: Path | None = None) -> RunResult:
    t0 = time.time()
    stats = RunStats()
    doc = load_document(doc_path)
    log(f"документ: {doc.name}, строк: {len(doc.lines)}" + (f", страниц: {doc.pages}" if doc.pages else ""))
    reqs = segment(doc)
    log(f"нарезка: {len(reqs)} требований")
    rule_findings, notes = run_rules(doc, reqs)
    log(f"правила: {len(rule_findings)} находок")
    grounder = Grounder(doc, reqs)
    result = RunResult(doc=doc, reqs=reqs, findings=[], notes=notes, stats=stats)
    all_findings: list[Finding] = []
    for f in rule_findings:
        if grounder.ground(f):
            all_findings.append(f)
        else:
            result.dropped_ungrounded.append(f)

    clients = available_clients(stats, cache_dir) if use_llm else {}
    if use_llm and not clients:
        log("LLM-ключи не найдены — работаю только на правилах (см. .env.example)")
    if not clients:
        result.mode = "rules-only"
    else:
        reviewers, orch_name, judge_name = pick_roles(clients)
        variants = int(os.getenv("REQAUDIT_VARIANTS", "2" if len(reviewers) == 1 else "1"))
        tasks = []
        for i, name in enumerate(reviewers):
            focus_env = os.getenv(f"{name.upper()}_FOCUS", "").strip()
            if focus_env:
                foci = [int(x) % 3 for x in focus_env.split(",") if x.strip().isdigit()]
            else:
                foci = [(i + v) % 3 for v in range(int(os.getenv(f"{name.upper()}_VARIANTS", variants)))]
            for v, focus in enumerate(foci):
                label = name if len(foci) == 1 else f"{name}#{v + 1}"
                tasks.append((clients[name], focus, label))
        result.reviewers = [clients[n].info for n in reviewers]
        result.reviewer_labels = [t[2] for t in tasks]
        numbered = doc.numbered_text()
        user = prompts.reviewer_user(req_table(reqs), numbered)
        log(f"консилиум: {', '.join(t[2] for t in tasks)} (параллельно)")

        def review(task):
            client, variant, label = task
            started = time.time()
            cap = int(os.getenv(f"{client.name.upper()}_MAX_FINDINGS", "25"))
            data = client.chat_json(prompts.reviewer_system(variant, cap), user, max_tokens=16000,
                                    deadline=float(os.getenv("REQAUDIT_REVIEW_DEADLINE", "480")))
            fs = _to_findings(data, label)
            log(f"  ревьюер {label}: {len(fs)} находок за {time.time() - started:.0f} с")
            return fs

        with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as ex:
            futures = [(t, ex.submit(review, t)) for t in tasks]
            for t, fut in futures:
                try:
                    for f in fut.result():
                        if grounder.ground(f):
                            all_findings.append(f)
                        else:
                            result.dropped_ungrounded.append(f)
                except (LLMError, ValueError) as e:
                    result.failures.append(f"Ревьюер {t[2]} недоступен: {str(e)[:200]}")
                    log(f"  ревьюер {t[2]} упал: {str(e)[:160]}")
        log(f"проверка цитат: подтверждено {len(all_findings)}, отброшено {len(result.dropped_ungrounded)}")

    merged = merge_findings(all_findings)
    merged.sort(key=_sort_key)
    n_llm_voters = len(result.reviewer_labels)
    for f in merged:
        llm_votes = [v for v in f.votes if v != "правила"]
        if n_llm_voters >= 2 and len(llm_votes) == 1 and "правила" not in f.votes:
            f.status_note = (f.status_note + " " if f.status_note else "") + f"Найдено одним ревьюером из {n_llm_voters}."
    log(f"склейка: {len(merged)} уникальных находок")

    # ---------------------------------------------------------------- оркестратор
    ids: dict[str, Finding] = {}
    if clients and merged:
        orch = clients[orch_name]
        if os.getenv("REQAUDIT_ORCH_EFFORT"):
            orch.reasoning_effort = os.environ["REQAUDIT_ORCH_EFFORT"]
        result.orchestrator = orch.info
        ids = {f"C-{i:02d}": f for i, f in enumerate(merged, start=1)}
        payload = json.dumps([_compact(f, k) for k, f in ids.items()], ensure_ascii=False)
        try:
            log(f"оркестратор: {orch.label}")
            data = orch.chat_json(prompts.ORCH_SYSTEM, prompts.orch_user(payload, doc.numbered_text()), max_tokens=32000)
            dup = set()
            for item in data.get("findings") or []:
                fid = str(item.get("id") or "")
                f = ids.get(fid)
                if not f:
                    continue
                d_of = item.get("duplicate_of")
                if d_of and d_of in ids and d_of != fid:
                    tgt = ids[d_of]
                    for v in f.votes:
                        if v not in tgt.votes:
                            tgt.votes.append(v)
                    tgt.rids = sorted(set(tgt.rids) | set(f.rids))
                    seen = {(q.line_start, normalize(q.text)) for q in tgt.quotes}
                    for q in f.quotes:
                        if (q.line_start, normalize(q.text)) not in seen and len(tgt.quotes) < 5:
                            tgt.quotes.append(q)
                    dup.add(fid)
                    continue
                _apply_orch(f, item)
            result.priority = [str(x) for x in (data.get("priority") or []) if str(x) in ids and str(x) not in dup]
            result.checklist = [c for c in (data.get("checklist") or []) if isinstance(c, dict) and c.get("item")]
            rd = data.get("readiness") or {}
            result.readiness = {str(k): str(v) for k, v in rd.items() if v} if isinstance(rd, dict) else {}
            merged = [f for k, f in ids.items() if k not in dup]
            prio_objs = [ids[k] for k in result.priority]
        except (LLMError, ValueError) as e:
            result.failures.append(f"Оркестратор недоступен: {str(e)[:200]} — применена детерминированная склейка.")
            log(f"  оркестратор упал: {str(e)[:160]}")
            prio_objs = []
    else:
        prio_objs = []

    merged.sort(key=_sort_key)
    for i, f in enumerate(merged, start=1):
        f.fid = f"F-{i:02d}"

    # ---------------------------------------------------------------- судья
    if clients and merged:
        judge = clients[judge_name]
        if os.getenv("REQAUDIT_JUDGE_EFFORT"):
            judge.reasoning_effort = os.environ["REQAUDIT_JUDGE_EFFORT"]
        result.judge = judge.info
        result.judge_independent = judge.label not in [clients[n].label for n in reviewers]
        payload = json.dumps([_compact(f, f.fid) for f in merged if f.proof != "Ш"], ensure_ascii=False)
        try:
            log(f"судья: {judge.label}" + ("" if result.judge_independent else " (не независим: участвовал в ревью)"))
            data = judge.chat_json(prompts.JUDGE_SYSTEM, prompts.judge_user(payload, doc.numbered_text()), max_tokens=16000)
            by_id = {f.fid: f for f in merged}
            for v in data.get("verdicts") or []:
                f = by_id.get(str(v.get("id") or ""))
                if not f:
                    continue
                verdict = str(v.get("verdict") or "").lower()
                f.judge = verdict if verdict in ("confirmed", "disputed", "rejected") else ""
                f.judge_note = str(v.get("note") or "")[:300]
                if str(v.get("proof")) in ("Д", "У", "Ш"):
                    if not (f.is_rule and f.proof == "Д"):
                        f.proof = str(v["proof"])
                if str(v.get("severity")) in SEVERITY_ORDER:
                    f.severity = str(v["severity"])
            kept = []
            for f in merged:
                if f.judge == "rejected":
                    if f.is_rule or "правила" in f.votes:
                        f.judge = "disputed"  # детерминированные находки не удаляем, только помечаем
                        kept.append(f)
                    else:
                        result.rejected.append(f)
                else:
                    kept.append(f)
            merged = kept
        except (LLMError, ValueError) as e:
            result.failures.append(f"Судья недоступен: {str(e)[:200]} — находки не перепроверены.")
            log(f"  судья упал: {str(e)[:160]}")

    merged.sort(key=_sort_key)
    for i, f in enumerate(merged, start=1):
        f.fid = f"F-{i:02d}"
    result.findings = merged
    alive = {id(f) for f in merged}
    result.priority = [f.fid for f in prio_objs if id(f) in alive]
    for c in result.checklist:
        c["why"] = _remap_ids(str(c.get("why", "")), alive, ids)
    stats.seconds = time.time() - t0
    log(f"готово: {len(merged)} находок, вызовов LLM: {stats.llm_calls} (кэш: {stats.llm_cached}), {stats.seconds:.0f} с")
    return result


def _remap_ids(text: str, alive: set[int], ids: dict[str, Finding]) -> str:
    import re

    def repl(m):
        f = ids.get(m.group(0))
        return f.fid if f is not None and id(f) in alive else m.group(0)

    return re.sub(r"C-\d{2}", repl, text)


def _apply_orch(f: Finding, item: dict) -> None:
    for fld in ("title", "explanation", "scenario", "impact", "suggestion"):
        val = item.get(fld)
        if isinstance(val, str) and val.strip():
            setattr(f, fld, val.strip())
    cat = str(item.get("category") or "").lower()
    if cat in VALID_CATEGORIES and not f.is_rule:
        f.category = cat
        f.criterion = criterion_for(cat)
    sev = str(item.get("severity") or "").lower()
    if sev in SEVERITY_ORDER:
        f.severity = sev
    pr = str(item.get("proof") or "")
    if pr in ("Д", "У", "Ш") and not (f.is_rule and f.proof == "Д"):
        f.proof = pr
    q = item.get("question")
    if isinstance(q, dict) and q.get("text"):
        f.question = Question(text=str(q["text"]), options=[str(o) for o in (q.get("options") or [])][:4],
                              addressee=str(q.get("addressee") or (f.question.addressee if f.question else "")))
