"""Итоговый вердикт — детерминированное правило, а не мнение модели."""

from __future__ import annotations

from .criteria import SEVERITY_ORDER
from .models import Finding, Requirement

CORE_KINDS = {"FR", "API", "INT", "DATA", "AC", "NFR", "REQ"}  # REQ — требование без стандартного раздела


def _kinds(f: Finding, reqs_by_id: dict[str, Requirement]) -> set[str]:
    return {reqs_by_id[r].kind for r in f.rids if r in reqs_by_id}


def critical_groups(findings: list[Finding]) -> dict[str, list[Finding]]:
    live = [f for f in findings if f.judge != "rejected"]
    return {
        "Противоречивые": [f for f in live if f.category in ("contradiction", "incorrect") and f.proof == "Д"
                           and SEVERITY_ORDER[f.severity] <= 1],
        "Непроверяемые": [f for f in live if f.category == "unverifiable" and SEVERITY_ORDER[f.severity] <= 1],
        "Неполные": [f for f in live if f.category in ("incompleteness", "security", "loophole")
                     and SEVERITY_ORDER[f.severity] == 0],
    }


def compute(findings: list[Finding], reqs: list[Requirement]) -> dict:
    by_id = {r.rid: r for r in reqs}
    live = [f for f in findings if f.judge != "rejected"]
    blockers = [f for f in live if f.severity == "blocker"]
    majors = [f for f in live if f.severity == "major"]
    dev_block = [f for f in blockers if (_kinds(f, by_id) & CORE_KINDS) or not f.rids]
    test_block = [f for f in live if f.severity == "blocker" or (
        f.severity == "major" and (
            (f.category == "unverifiable" and _kinds(f, by_id) & {"FR", "NFR", "AC"}) or
            (f.category == "contradiction" and "AC" in _kinds(f, by_id))))]

    def status(block, warn):
        if block:
            return "Нельзя", block
        if warn:
            return "С оговорками", warn
        return "Можно", []

    dev_status, dev_reasons = status(dev_block, majors)
    test_status, test_reasons = status(test_block, [f for f in majors if f.category in ("incompleteness", "ambiguity")])
    ranked = sorted(live, key=lambda f: (SEVERITY_ORDER[f.severity], {"Д": 0, "У": 1, "Ш": 2}.get(f.proof, 3),
                                         -len(f.rids), -len(f.votes)))
    fix_first = [f for f in ranked if f.proof != "Ш"][:5]
    return {"dev": (dev_status, dev_reasons), "test": (test_status, test_reasons), "fix_first": fix_first,
            "blockers": blockers, "majors": majors, "critical": critical_groups(findings)}
