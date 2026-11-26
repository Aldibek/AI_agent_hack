"""Сравнение отчёта агента с эталоном: найдено / пропущено / ложные срабатывания на не-дефектах.

Использование:
    python eval/score.py reports/report.json eval/gold_requirements.json
"""

from __future__ import annotations

import json
import re
import sys


def finding_text(f: dict) -> str:
    return " ".join(str(f.get(k, "")) for k in ("title", "explanation", "suggestion", "scenario")).lower()


def finding_lines(f: dict) -> set[int]:
    out = set()
    for q in f.get("quotes", []):
        if q.get("line_start"):
            out.update(range(q["line_start"], (q.get("line_end") or q["line_start"]) + 1))
    return out


def matches(item: dict, f: dict) -> bool:
    lines, text = finding_lines(f), finding_text(f)
    want = set(item.get("lines", []))
    if want:
        ok = want <= lines if item.get("mode") == "all" else bool(want & lines)
        if not ok:
            return False
    kw = item.get("kw")
    return bool(re.search(kw, text)) if kw else bool(want)


def main(report_path: str, gold_path: str) -> None:
    rep = json.load(open(report_path, encoding="utf-8"))
    gold = json.load(open(gold_path, encoding="utf-8"))
    findings = rep["findings"]
    found, missed = [], []
    for item in gold["defects"]:
        hits = [f["fid"] for f in findings if matches(item, f)]
        (found if hits else missed).append((item, hits))
    by_sev = {}
    for item, hits in found + missed:
        s = by_sev.setdefault(item["sev"], [0, 0])
        s[1] += 1
        if hits:
            s[0] += 1
    fp = []
    for item in gold.get("non_defects", []):
        hits = [f["fid"] for f in findings if matches(item, f) and f.get("judge") != "rejected"]
        if hits:
            fp.append((item, hits))
    total = len(gold["defects"])
    print(f"Найдено эталонных проблем: {len(found)} из {total} ({100 * len(found) / total:.0f}%)")
    names = {"B": "блокеры", "M": "существенные", "m": "незначительные"}
    for k in ("B", "M", "m"):
        if k in by_sev:
            a, b = by_sev[k]
            print(f"  {names[k]}: {a} из {b}")
    print(f"Ложные срабатывания на не-дефектах: {len(fp)} из {len(gold.get('non_defects', []))}")
    for item, hits in fp:
        print(f"  ✗ {item['id']} {item['title']} → {', '.join(hits)}")
    print(f"Всего находок в отчёте: {len(findings)}")
    print("\nПропущено:")
    for item, _ in missed:
        print(f"  - {item['id']} [{item['sev']}] {item['title']}")
    print("\nНайдено:")
    for item, hits in found:
        print(f"  + {item['id']} [{item['sev']}] {item['title']} → {', '.join(hits[:4])}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "reports/report.json",
         sys.argv[2] if len(sys.argv) > 2 else "eval/gold_requirements.json")
