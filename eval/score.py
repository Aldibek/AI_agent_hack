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
    kt = item.get("kw_title")
    if kt and not re.search(kt, str(f.get("title", "")).lower()):
        return False
    kw = item.get("kw")
    return bool(re.search(kw, text)) if kw else bool(want or kt)


def main(report_path: str, gold_path: str) -> None:
    rep = json.load(open(report_path, encoding="utf-8"))
    gold = json.load(open(gold_path, encoding="utf-8"))
    findings = rep["findings"]
    if rep["doc"].get("fmt") != "md":
        src = Path(gold_path).resolve().parent.parent / gold["document"]  # корень репозитория
        if src.exists():
            for i, t in enumerate(src.read_text(encoding="utf-8").split("\n"), start=1):
                GOLD_TEXT[i] = normalize(t)
            print(f"(сопоставление по тексту цитат: формат {rep['doc'].get('fmt')}, эталон {src.name})")
    found, missed = [], []
    for item in gold["defects"]:
        hits = [f["fid"] for f in findings if matches(item, f)]
        (found if hits else missed).append((item, hits))
    strict = strict_match(gold["defects"], findings)
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
    n_strict = len(strict)
    used = {f["fid"] for f in findings if any(matches(d, f) for d in gold["defects"])}
    print(f"\nСтрого (одна находка закрывает не больше одного дефекта): {n_strict} из {total} "
          f"({100 * n_strict / total:.0f}%)")
    print(f"  находок, совпавших хотя бы с одним дефектом эталона: {len(used)} из {len(findings)}; "
          f"остальные {len(findings) - len(used)} — вне эталона (новые дефекты или шум, нужен ручной просмотр)")
    sev_ok = [item for item, hits in found if item["sev"] == "B"
              and any(f["severity"] == "blocker" for f in findings if f["fid"] in hits)]
    n_b = sum(1 for item in gold["defects"] if item["sev"] == "B")
    print(f"Эталонные блокеры, отмеченные в отчёте как 🔴 блокер: {len(sev_ok)} из {n_b}")
    critic(rep, gold, found)
    print("\nПропущено:")
    for item, _ in missed:
        print(f"  - {item['id']} [{item['sev']}] {item['title']}")
    print("\nНайдено:")
    for item, hits in found:
        print(f"  + {item['id']} [{item['sev']}] {item['title']} → {', '.join(hits[:4])}")


def strict_match(defects: list, findings: list) -> dict[str, str]:
    """Паросочетание «дефект эталона ↔ находка»: каждая находка закрывает не больше одного дефекта."""
    cand = {d["id"]: [f["fid"] for f in findings if matches(d, f)] for d in defects}
    owner: dict[str, str] = {}  # fid → id дефекта

    def try_assign(did: str, seen: set) -> bool:
        for fid in cand[did]:
            if fid in seen:
                continue
            seen.add(fid)
            if fid not in owner or try_assign(owner[fid], seen):
                owner[fid] = did
                return True
        return False

    for d in sorted(defects, key=lambda d: len(cand[d["id"]])):
        try_assign(d["id"], set())
    return {did: fid for fid, did in owner.items()}


def critic(rep: dict, gold: dict, found: list) -> None:
    """Качество судьи-критика: не отклонил/не ослабил ли верное, отсеял ли ложное."""
    findings = rep["findings"]
    if not rep.get("judge"):
        print("\nКритик (судья): не запускался")
        return
    by_fid = {f["fid"]: f for f in findings}
    gold_fids = {h for _, hits in found for h in hits}
    gold_f = [by_fid[h] for h in gold_fids if h in by_fid]
    verdicts = {}
    for f in gold_f:
        verdicts[f.get("judge") or "—"] = verdicts.get(f.get("judge") or "—", 0) + 1
    weakened = [f for f in gold_f if "тяжесть" in (f.get("judge_change") or "") and "→" in f.get("judge_change", "")
                and "не принято" not in f.get("judge_change", "")]
    downs = [f for f in weakened if any(a + "→" + b in f["judge_change"] for a, b in
                                        (("blocker", "major"), ("blocker", "minor"), ("major", "minor")))]
    rej_gold = [d["id"] for d in gold["defects"] for f in rep.get("rejected", []) if matches(d, f)]
    rej_nd = [d["id"] for d in gold.get("non_defects", []) for f in rep.get("rejected", []) if matches(d, f)]
    other_rej = len(rep.get("rejected", [])) - len(set(rej_gold)) - len(set(rej_nd))
    print(f"\nКритик (судья {rep['judge']['model']}):")
    print(f"  находки, совпавшие с эталонными дефектами: {len(gold_f)} — вердикты {verdicts}")
    print(f"  эталонных дефектов отклонено (ошибка критика): {len(set(rej_gold))} {sorted(set(rej_gold))}")
    print(f"  эталонных находок понижено по тяжести: {len(downs)} " + ", ".join(f['fid'] for f in downs))
    print(f"  «не-дефектов» отклонено (польза критика): {len(set(rej_nd))}; прочих отклонённых: {max(other_rej, 0)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "reports/report.json",
         sys.argv[2] if len(sys.argv) > 2 else "eval/gold_requirements.json")
