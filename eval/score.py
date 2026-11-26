"""Сравнение отчёта агента с эталоном: найдено / пропущено / ложные срабатывания на не-дефектах.

Использование:
    python eval/score.py reports/report.json eval/gold_requirements.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reqaudit.grounding import normalize  # noqa: E402

GOLD_TEXT: dict[int, str] = {}  # строка эталонного markdown → нормализованный текст (для PDF/DOCX)


def finding_text(f: dict) -> str:
    return " ".join(str(f.get(k, "")) for k in ("title", "explanation", "suggestion", "scenario")).lower()


def finding_lines(f: dict) -> set[int]:
    out = set()
    for q in f.get("quotes", []):
        if q.get("line_start"):
            out.update(range(q["line_start"], (q.get("line_end") or q["line_start"]) + 1))
    return out


def _text_hit(line_no: int, f: dict) -> bool:
    gold = GOLD_TEXT.get(line_no, "")
    if not gold:
        return False
    for q in f.get("quotes", []):
        t = normalize(q.get("text", ""))
        if len(t) >= 8 and (t in gold or fuzz.partial_ratio(t, gold) >= 90):
            return True
    return False


def matches(item: dict, f: dict) -> bool:
    lines, text = finding_lines(f), finding_text(f)
    want = set(item.get("lines", []))
    if want:
        if GOLD_TEXT:
            hits = {n for n in want if _text_hit(n, f)}
            ok = hits == want if item.get("mode") == "all" else bool(hits)
        else:
            ok = want <= lines if item.get("mode") == "all" else bool(want & lines)
        if not ok:
            return False
    kw = item.get("kw")
    return bool(re.search(kw, text)) if kw else bool(want)


def main(report_path: str, gold_path: str) -> None:
    rep = json.load(open(report_path, encoding="utf-8"))
    gold = json.load(open(gold_path, encoding="utf-8"))
    findings = rep["findings"]
    if rep["doc"].get("fmt") != "md":
        src = Path(gold_path).resolve().parent.parent.parent / gold["document"]
        if src.exists():
            for i, t in enumerate(src.read_text(encoding="utf-8").split("\n"), start=1):
                GOLD_TEXT[i] = normalize(t)
            print(f"(сопоставление по тексту цитат: формат {rep['doc'].get('fmt')}, эталон {src.name})")
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
