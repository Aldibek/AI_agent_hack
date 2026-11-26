"""Учёт расходов на API LLM и защита от перерасхода (MLOps).

- таблица цен ($ за 1M токенов), переопределяется JSON-файлом из REQAUDIT_PRICES_FILE;
- журнал вызовов `.reqaudit_costs/ledger.jsonl` (каждый реальный вызов и попадание в кэш);
- бюджеты на прогон (REQAUDIT_BUDGET_USD) и на сутки (REQAUDIT_DAILY_BUDGET_USD): перед вызовом
  оценивается его стоимость, при превышении — BudgetExceeded (вызов не делается);
- предупреждение при 80% любого лимита, сравнение прогона с медианой последних 10 прогонов;
- просмотр: `python -m reqaudit.costs [--days 7]`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .llm import LLMError

# $ за 1M токенов: input / output / cache read / cache write
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-2": {"input": 8.00, "output": 40.00, "cache_read": 0.20, "cache_write": 10.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
CHARS_PER_TOKEN = 3.5
MIN_OUTPUT_ESTIMATE = 2000  # роль без дефолта и без истории
# выход на вызов по роли, если в журнале нет истории этой роли и модели (reasoning-токены входят в выход)
DEFAULT_OUTPUT_ESTIMATE = {"reviewer": 12000, "judge": 8000}
OUTPUT_HISTORY_WINDOW = 5  # среднее по последним N успешным вызовам роли и модели
WARN_SHARE = 0.8
ANOMALY_FACTOR = 2.0
ANOMALY_WINDOW = 10


class BudgetExceeded(LLMError):
    """Вызов LLM не выполнен: он превысил бы бюджет прогона или суток."""

    def __init__(self, message: str, *, limit_usd: float = 0.0, kind: str = "run", preflight: bool = False):
        super().__init__(message)
        self.limit_usd = limit_usd
        self.kind = kind  # run | daily
        self.preflight = preflight


def costs_dir() -> Path:
    env = os.getenv("REQAUDIT_COSTS_DIR", "").strip()
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent / ".reqaudit_costs"


def ledger_path() -> Path:
    return costs_dir() / "ledger.jsonl"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def run_budget() -> float:
    return _env_float("REQAUDIT_BUDGET_USD", 2.0)


def daily_budget() -> float:
    return _env_float("REQAUDIT_DAILY_BUDGET_USD", 15.0)


def load_prices() -> dict[str, dict[str, float]]:
    prices = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    path = os.getenv("REQAUDIT_PRICES_FILE", "").strip()
    if path:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        for model, p in (data or {}).items():
            if isinstance(p, dict):
                prices[str(model)] = {k: float(p.get(k, prices.get(model, {}).get(k, 0.0)))
                                      for k in ("input", "output", "cache_read", "cache_write")}
    return prices


def call_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0,
              prices: dict | None = None) -> float | None:
    """Стоимость вызова в $; None — цена модели неизвестна."""
    p = (prices if prices is not None else load_prices()).get(model)
    if not p:
        return None
    return (input_tokens * p["input"] + output_tokens * p["output"] + cache_read * p["cache_read"]
            + cache_write * p["cache_write"]) / 1_000_000


def read_ledger(path: Path | None = None) -> list[dict]:
    path = path or ledger_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _day(ts: str) -> str:
    return str(ts)[:10]


def fmt_usd(x: float | None) -> str:
    if x is None:
        return "н/д"
    return f"${x:.4f}" if 0 < abs(x) < 0.01 else f"${x:.2f}"


class CostTracker:
    """Учёт расходов одного прогона. Потокобезопасен (ревьюеры работают параллельно)."""

    def __init__(self, document: str = "", run_id: str | None = None, stats=None, log=None):
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.document = document
        self.stats = stats
        self.log = log or (lambda m: print(f"[reqaudit] {m}", file=sys.stderr, flush=True))
        self.prices = load_prices()
        self.run_limit = run_budget()
        self.daily_limit = daily_budget()
        self.path = ledger_path()
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.reserved = 0.0
        self.exhausted: str = ""  # причина полной остановки (дневной лимит)
        self.blocked: list[str] = []  # вызовы, не поместившиеся в бюджет прогона
        self._warned: set[str] = set()
        history = read_ledger(self.path)
        today = datetime.now().strftime("%Y-%m-%d")
        self.daily_before = sum(float(r.get("cost_usd") or 0) for r in history if _day(r.get("ts", "")) == today)
        self._out_hist: dict[tuple[str, str], list[int]] = defaultdict(list)
        for r in history:
            if r.get("ok") and not r.get("cached") and r.get("output_tokens"):
                self._out_hist[(str(r.get("role") or ""), str(r.get("model") or ""))].append(int(r["output_tokens"]))

    # ------------------------------------------------------------------ состояние
    @property
    def run_spent(self) -> float:
        return sum(c["cost_usd"] or 0.0 for c in self.calls)

    @property
    def daily_spent(self) -> float:
        return self.daily_before + self.run_spent

    def price_known(self, model: str) -> bool:
        return model in self.prices

    def avg_output(self, role: str, model: str) -> float:
        """Средний выход по последним успешным вызовам этой роли и модели; 0 — истории нет."""
        h = (self._out_hist.get((role, model)) or [])[-OUTPUT_HISTORY_WINDOW:]
        return sum(h) / len(h) if h else 0.0

    def expected_output(self, role: str, model: str) -> float:
        return self.avg_output(role, model) or DEFAULT_OUTPUT_ESTIMATE.get(role, MIN_OUTPUT_ESTIMATE)

    def estimate(self, model: str, role: str, input_chars: int) -> float | None:
        """Оценка вызова в $. Вход считается по цене записи в кэш, если она задана (Claude: cache_control
        ephemeral — весь вход пишется в кэш), иначе по обычной цене входа."""
        p = self.prices.get(model)
        if not p:
            return None
        in_price = p.get("cache_write") or p["input"]
        out = self.expected_output(role, model)
        return (input_chars / CHARS_PER_TOKEN) * in_price / 1_000_000 + out * p["output"] / 1_000_000

    # ------------------------------------------------------------------ события
    def _event(self, msg: str) -> None:
        self.log(msg)
        if self.stats is not None:
            self.stats.events.append(msg)

    def _maybe_warn(self) -> None:
        for kind, spent, limit, ru in (("run", self.run_spent + self.reserved, self.run_limit, "прогона"),
                                       ("daily", self.daily_spent + self.reserved, self.daily_limit, "суток")):
            if limit > 0 and spent >= WARN_SHARE * limit and kind not in self._warned:
                self._warned.add(kind)
                self._event(f"⚠️ Расход {ru} достиг {spent / limit:.0%} лимита: {fmt_usd(spent)} из {fmt_usd(limit)}.")

    # ------------------------------------------------------------------ бюджет
    def check(self, model: str, role: str, input_chars: int) -> float:
        """Проверить бюджет перед вызовом. Возвращает зарезервированную оценку (освободить в record)."""
        est = self.estimate(model, role, input_chars) or 0.0
        with self._lock:
            if self.exhausted:
                raise BudgetExceeded(self.exhausted, limit_usd=self.daily_limit, kind="daily")
            run_after = self.run_spent + self.reserved + est
            day_after = self.daily_spent + self.reserved + est
            if self.run_limit >= 0 and run_after > self.run_limit:
                # блокируем только этот вызов: следующие шаги выполнятся, если помещаются в остаток
                msg = (f"Вызов {role} ({model}) пропущен: не помещается в бюджет {fmt_usd(self.run_limit)} на прогон "
                       f"(потрачено {fmt_usd(self.run_spent)}, оценка вызова {fmt_usd(est)}).")
                self.blocked.append(msg)
                self._event(msg)
                raise BudgetExceeded(msg, limit_usd=self.run_limit, kind="run")
            if self.daily_limit >= 0 and day_after > self.daily_limit:
                self.exhausted = (f"Прогон остановлен: исчерпан бюджет {fmt_usd(self.daily_limit)} за сутки "
                                  f"(потрачено сегодня {fmt_usd(self.daily_spent)}, оценка следующего вызова "
                                  f"({role}, {model}) {fmt_usd(est)}).")
                self._event(self.exhausted)
                raise BudgetExceeded(self.exhausted, limit_usd=self.daily_limit, kind="daily")
            self.reserved += est
            self._maybe_warn()
        return est

    def record(self, *, provider: str, model: str, role: str, input_tokens: int = 0, output_tokens: int = 0,
               cache_read: int = 0, cache_write: int = 0, latency_s: float = 0.0, ok: bool = True,
               cached: bool = False, reserved: float = 0.0, error: str = "") -> dict:
        cost = 0.0 if cached else call_cost(model, input_tokens, output_tokens, cache_read, cache_write, self.prices)
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "run_id": self.run_id, "document": self.document,
               "provider": provider, "model": model, "role": role, "input_tokens": int(input_tokens),
               "output_tokens": int(output_tokens), "cache_read": int(cache_read), "cache_write": int(cache_write),
               "cost_usd": None if cost is None else round(cost, 6), "latency_s": round(latency_s, 2), "ok": ok,
               "cached": cached}
        if error:
            row["error"] = error[:300]
        with self._lock:
            self.reserved = max(0.0, self.reserved - reserved)
            self.calls.append(row)
            if ok and not cached and output_tokens:
                self._out_hist[(role, model)].append(int(output_tokens))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._maybe_warn()
        return row

    # ------------------------------------------------------------------ предварительная оценка
    def preflight(self, planned: list[tuple[str, str, int]]) -> tuple[float, list[str]]:
        """planned: (model, role, input_chars). Возвращает (оценка $ по моделям с известной ценой, модели без цены)."""
        total, unknown = 0.0, []
        for model, role, chars in planned:
            est = self.estimate(model, role, chars)
            if est is None:
                if model not in unknown:
                    unknown.append(model)
            else:
                total += est
        return total, unknown

    # ------------------------------------------------------------------ итоги
    def summary(self) -> dict:
        real = [c for c in self.calls if not c["cached"]]
        unknown = sorted({c["model"] for c in real if c["cost_usd"] is None})

        def group(key: str) -> dict:
            g: dict[str, dict] = {}
            for c in self.calls:
                d = g.setdefault(c[key], {"calls": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0,
                                          "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "priced": True})
                if c["cached"]:
                    d["cached"] += 1
                    continue
                d["calls"] += 1
                for k in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
                    d[k] += c[k]
                if c["cost_usd"] is None:
                    d["priced"] = False
                else:
                    d["cost_usd"] += c["cost_usd"]
            for d in g.values():
                d["cost_usd"] = round(d["cost_usd"], 6) if d["priced"] else None
                d.pop("priced")
            return g

        return {
            "run_id": self.run_id,
            "cost_usd": round(self.run_spent, 6),
            "unpriced_models": unknown,
            "calls": len(real),
            "cached_calls": len(self.calls) - len(real),
            "failed_calls": sum(1 for c in real if not c["ok"]),
            "input_tokens": sum(c["input_tokens"] for c in real),
            "output_tokens": sum(c["output_tokens"] for c in real),
            "cache_read": sum(c["cache_read"] for c in real),
            "cache_write": sum(c["cache_write"] for c in real),
            "budget_run_usd": self.run_limit,
            "budget_daily_usd": self.daily_limit,
            "daily_spent_usd": round(self.daily_spent, 6),
            "stopped": self.exhausted,
            "blocked": list(self.blocked),
            "by_model": group("model"),
            "by_role": group("role"),
            "ledger": str(self.path),
        }

    def anomaly(self) -> str:
        """Сравнить стоимость прогона с медианой последних прогонов; вернуть предупреждение или ""."""
        if not self.calls or self.run_spent <= 0:
            return ""
        per_run: dict[str, list] = {}
        for r in read_ledger(self.path):
            rid = r.get("run_id")
            if not rid or rid == self.run_id:
                continue
            e = per_run.setdefault(rid, [r.get("ts", ""), 0.0])
            e[0] = max(e[0], r.get("ts", ""))
            e[1] += float(r.get("cost_usd") or 0)
        prev = [c for _, c in sorted(per_run.values()) if c > 0][-ANOMALY_WINDOW:]
        if not prev:
            return ""
        med = statistics.median(prev)
        if med > 0 and self.run_spent > ANOMALY_FACTOR * med:
            msg = (f"⚠️ Аномалия расходов: прогон стоил {fmt_usd(self.run_spent)} — в {self.run_spent / med:.1f} раза "
                   f"больше медианы последних {len(prev)} прогонов ({fmt_usd(med)}).")
            self._event(msg)
            return msg
        return ""


# ---------------------------------------------------------------------- текущий прогон
_current: CostTracker | None = None


def start_run(document: str = "", stats=None, log=None) -> CostTracker:
    global _current
    _current = CostTracker(document=document, stats=stats, log=log)
    return _current


def current() -> CostTracker:
    global _current
    if _current is None:
        _current = CostTracker()
    return _current


def passport_line(summary: dict) -> str:
    s = summary or {}
    cost = fmt_usd(s.get("cost_usd", 0.0))
    if s.get("unpriced_models"):
        cost += f" + н/д для {', '.join(s['unpriced_models'])}"
    return (f"{cost} (вызовов {s.get('calls', 0)}, из кэша {s.get('cached_calls', 0)}; токены вход/выход "
            f"{s.get('input_tokens', 0)}/{s.get('output_tokens', 0)}, кэш чтение/запись "
            f"{s.get('cache_read', 0)}/{s.get('cache_write', 0)})")


# ---------------------------------------------------------------------- CLI: python -m reqaudit.costs
def _table(title: str, header: list[str], rows: list[list]) -> list[str]:
    out = ["", title]
    if not rows:
        return out + ["  (нет данных)"]
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    out.append("  " + "  ".join(str(h).ljust(w) for h, w in zip(header, widths)))
    out.append("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  " + "  ".join(str(x).ljust(w) for x, w in zip(r, widths)))
    return out


def report(days: int = 7, path: Path | None = None) -> str:
    rows = read_ledger(path)
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = [r for r in rows if _day(r.get("ts", "")) >= since]

    def agg(key):
        g: dict[str, list] = {}
        for r in rows:
            k = key(r)
            e = g.setdefault(k, [0, 0, 0, 0, 0.0, True])
            if r.get("cached"):
                e[1] += 1
                continue
            e[0] += 1
            e[2] += int(r.get("input_tokens") or 0)
            e[3] += int(r.get("output_tokens") or 0)
            if r.get("cost_usd") is None:
                e[5] = False
            else:
                e[4] += float(r["cost_usd"])
        return g

    def fmt(g, sort_by_key=False):
        items = sorted(g.items()) if sort_by_key else sorted(g.items(), key=lambda kv: -kv[1][4])
        return [[k, e[0], e[1], e[2], e[3], f"${e[4]:.4f}" + ("" if e[5] else " + н/д")] for k, e in items]

    hdr = ["", "вызовов", "из кэша", "токены вход", "токены выход", "стоимость"]
    L = [f"Расходы на LLM за {days} дн. (журнал: {path or ledger_path()}, записей: {len(rows)})"]
    L += _table("По дням:", ["день"] + hdr[1:], fmt(agg(lambda r: _day(r.get("ts", ""))), sort_by_key=True))
    L += _table("По моделям:", ["модель"] + hdr[1:], fmt(agg(lambda r: str(r.get("model")))))
    L += _table("По ролям:", ["роль"] + hdr[1:], fmt(agg(lambda r: str(r.get("role")))))
    runs: dict[str, list] = {}
    for r in rows:
        e = runs.setdefault(str(r.get("run_id")), [r.get("ts", ""), str(r.get("document") or ""), 0, 0.0])
        e[0] = min(e[0], r.get("ts", ""))
        if not r.get("cached"):
            e[2] += 1
        e[3] += float(r.get("cost_usd") or 0)
    top = sorted(runs.items(), key=lambda kv: -kv[1][3])[:5]
    L += _table("Топ-5 самых дорогих прогонов:", ["run_id", "начало", "документ", "вызовов", "стоимость"],
                [[k, e[0], e[1], e[2], f"${e[3]:.4f}"] for k, e in top])
    today = datetime.now().strftime("%Y-%m-%d")
    spent = sum(float(r.get("cost_usd") or 0) for r in read_ledger(path) if _day(r.get("ts", "")) == today)
    lim = daily_budget()
    share = f" ({spent / lim:.0%})" if lim > 0 else ""
    L += ["", f"Сегодня ({today}): {fmt_usd(spent)} из дневного лимита {fmt_usd(lim)}{share}; "
              f"лимит на прогон {fmt_usd(run_budget())}."]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env")
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass
    p = argparse.ArgumentParser(prog="python -m reqaudit.costs", description="Просмотр расходов на API LLM по журналу")
    p.add_argument("--days", type=int, default=7, help="за сколько последних дней (по умолчанию 7)")
    p.add_argument("--ledger", help="путь к ledger.jsonl (по умолчанию .reqaudit_costs/ledger.jsonl)")
    args = p.parse_args(argv)
    print(report(max(1, args.days), Path(args.ledger) if args.ledger else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
