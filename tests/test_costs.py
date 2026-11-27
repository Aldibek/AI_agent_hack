"""Тесты провайдера Claude (Anthropic) и учёта расходов. Без сети и реальных ключей: SDK замокан.

Запуск: python -m unittest discover -s tests -v
"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from reqaudit import costs
from reqaudit.costs import BudgetExceeded, CostTracker, call_cost
from reqaudit.llm import PRIORITY, PROVIDERS, AnthropicClient, LLMError
from reqaudit.models import RunStats


# ---------------------------------------------------------------------- фейковый SDK
class FakeStream:
    def __init__(self, sdk, kwargs):
        self.sdk, self.kwargs = sdk, kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self.sdk.respond(self.kwargs)


class FakeMessages:
    def __init__(self, sdk):
        self.sdk = sdk

    def stream(self, **kwargs):
        self.sdk.requests.append(kwargs)
        return FakeStream(self.sdk, kwargs)


class FakeSDK:
    """Имитирует anthropic.Anthropic: client.messages.stream / client.beta.messages.stream."""

    def __init__(self, text='{"findings": []}', stop_reason="end_turn", stop_details=None,
                 usage=(1000, 500, 0, 0), model=None):
        self.requests: list[dict] = []
        self.text, self.stop_reason, self.stop_details, self.usage, self.model = \
            text, stop_reason, stop_details, usage, model
        self.messages = FakeMessages(self)
        self.beta = SimpleNamespace(messages=FakeMessages(self))

    def respond(self, kwargs):
        text = self.text(kwargs) if callable(self.text) else self.text
        i, o, cr, cw = self.usage
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            stop_reason=self.stop_reason, stop_details=self.stop_details, model=self.model or kwargs["model"],
            usage=SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr,
                                  cache_creation_input_tokens=cw))
        msg._request_id = "req_test"
        return msg


ENV_KEYS = [PROVIDERS[n][1] for n in PROVIDERS if PROVIDERS[n][1]] + [
    "REQAUDIT_REVIEWERS", "REQAUDIT_JUDGE",
    "REQAUDIT_JUDGE_MODEL", "REQAUDIT_ORCH_MODEL", "REQAUDIT_VARIANTS", "ANTHROPIC_FOCUS", "ANTHROPIC_EFFORT", "ANTHROPIC_MODEL",
    "REQAUDIT_ORCH_EFFORT", "REQAUDIT_JUDGE_EFFORT", "REQAUDIT_PRICES_FILE", "ANTHROPIC_FALLBACK",
    "ANTHROPIC_BASE_URL"]


class CostsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"REQAUDIT_COSTS_DIR": self.tmp.name, "REQAUDIT_FAILED_DIR": self.tmp.name,
                                                "REQAUDIT_BUDGET_USD": "3.0",
                                                "REQAUDIT_DAILY_BUDGET_USD": "15.0"})
        self.env.start()
        for k in ENV_KEYS:  # реальные ключи и роли из окружения не должны влиять на тесты
            os.environ.pop(k, None)
        self.logs: list[str] = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def tracker(self, **kw) -> CostTracker:
        return CostTracker(document="spec.md", stats=kw.pop("stats", RunStats()), log=self.logs.append, **kw)

    def client(self, sdk, tracker=None, model="claude-opus-5-5", cache_dir=None, stats=None):
        return AnthropicClient("anthropic", "Anthropic", model, "test-key", stats or RunStats(), cache_dir,
                               tracker=tracker, sdk=sdk)

    def ledger(self) -> list[dict]:
        return costs.read_ledger(Path(self.tmp.name) / "ledger.jsonl")


# ---------------------------------------------------------------------- стоимость
class CostCalcTest(CostsBase):
    def test_known_prices(self):
        self.assertAlmostEqual(call_cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000), 29.20)
        self.assertAlmostEqual(call_cost("claude-sonnet-5-5", 1_000_000, 100_000), 3.0)
        self.assertAlmostEqual(call_cost("claude-haiku-5-5", 2_000_000, 0, 0, 1_000_000), 0.325)

    def test_unknown_model_cost_is_none(self):
        self.assertIsNone(call_cost("gemini-flash-latest", 1000, 1000))
        tr = self.tracker()
        row = tr.record(provider="gemini", model="gemini-flash-latest", role="reviewer", input_tokens=10, output_tokens=5)
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["input_tokens"], 10)
        self.assertEqual(tr.summary()["unpriced_models"], ["gemini-flash-latest"])
        self.assertIn("н/д", costs.passport_line(tr.summary()))

    def test_prices_file_override(self):
        p = Path(self.tmp.name) / "prices.json"
        p.write_text(json.dumps({"gemini-flash-latest": {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
                                 "claude-opus-5-5": {"input": 10}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"REQAUDIT_PRICES_FILE": str(p)}):
            prices = costs.load_prices()
        self.assertAlmostEqual(call_cost("gemini-flash-latest", 1_000_000, 1_000_000, prices=prices), 3.0)
        self.assertEqual(prices["claude-opus-5-5"]["input"], 10)
        self.assertEqual(prices["claude-opus-5-5"]["output"], 20.0)  # не указанное — из таблицы по умолчанию

    def test_estimate_formula(self):
        tr = self.tracker()
        # 3500 символов ≈ 1000 токенов входа по цене записи в кэш; без истории выход судьи — 3000 токенов
        self.assertAlmostEqual(tr.estimate("claude-opus-5-5", "judge", 3500), 1000 * 5e-6 + 8000 * 20e-6)
        self.assertAlmostEqual(tr.estimate("claude-opus-5-5", "reviewer", 3500), 1000 * 5e-6 + 12000 * 20e-6)
        self.assertAlmostEqual(tr.estimate("claude-opus-5-5", "clarify", 3500), 1000 * 5e-6 + 2000 * 20e-6)
        tr.record(provider="anthropic", model="claude-opus-5-5", role="judge", input_tokens=1, output_tokens=10_000)
        self.assertAlmostEqual(tr.estimate("claude-opus-5-5", "judge", 3500), 1000 * 5e-6 + 10_000 * 20e-6)
        # история другой модели той же роли не влияет
        self.assertAlmostEqual(tr.estimate("claude-sonnet-5-5", "judge", 3500), 1000 * 2.5e-6 + 8000 * 10e-6)
        self.assertIsNone(tr.estimate("gemini-flash-latest", "judge", 3500))

    def test_estimate_uses_last_successful_calls_from_ledger(self):
        tr = self.tracker()
        for out in (100, 100, 15000, 15000, 15000, 15000, 15000):
            tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=out)
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=1, ok=False)
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", cached=True)
        tr2 = self.tracker()  # новый прогон читает историю из журнала
        self.assertEqual(tr2.avg_output("reviewer", "claude-opus-5-5"), 15000)
        self.assertEqual(tr2.avg_output("reviewer", "claude-sonnet-5-5"), 0)

    def test_preflight_current_config_realistic(self):
        """3 прохода Opus + критик Opus на docs/requirements.md: $0.8–1.6, а не $0.3."""
        from reqaudit import prompts
        from reqaudit.ingest import load_document
        from reqaudit.pipeline import req_table
        from reqaudit.segment import segment

        doc = load_document(str(Path(__file__).resolve().parent.parent / "docs" / "requirements.md"))
        reqs = segment(doc)
        numbered = doc.numbered_text()
        user = prompts.reviewer_user(req_table(reqs), numbered)
        planned = [("claude-opus-5-5", "reviewer", len(prompts.reviewer_system(f)) + len(user)) for f in (0, 2, 3)]
        planned.append(("claude-opus-5-5", "judge",
                        len(prompts.JUDGE_SYSTEM) + len(prompts.judge_user("", numbered)) + 40_000))
        est, unpriced = self.tracker().preflight(planned)
        self.assertEqual(unpriced, [])
        self.assertGreaterEqual(est, 0.8)
        self.assertLessEqual(est, 1.6)


class LedgerTest(CostsBase):
    def test_real_and_cached_calls_written(self):
        cache = Path(self.tmp.name) / "cache"
        tr = self.tracker()
        sdk = FakeSDK(usage=(1000, 500, 200, 100))
        c = self.client(sdk, tracker=tr, cache_dir=cache)
        msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
        self.assertEqual(c.chat(msgs, role="judge"), '{"findings": []}')
        self.assertEqual(c.chat(msgs, role="judge"), '{"findings": []}')  # из кэша
        self.assertEqual(len(sdk.requests), 1)
        self.assertTrue(list(cache.glob("anthropic_*.json")))
        rows = self.ledger()
        self.assertEqual(len(rows), 2)
        real, cached = rows
        for k in ("ts", "run_id", "document", "provider", "model", "role", "input_tokens", "output_tokens",
                  "cache_read", "cache_write", "cost_usd", "latency_s", "ok"):
            self.assertIn(k, real)
        self.assertEqual((real["provider"], real["model"], real["role"], real["document"]),
                         ("anthropic", "claude-opus-5-5", "judge", "spec.md"))
        self.assertEqual((real["input_tokens"], real["output_tokens"], real["cache_read"], real["cache_write"]),
                         (1000, 500, 200, 100))
        self.assertAlmostEqual(real["cost_usd"], (1000 * 4 + 500 * 20 + 200 * 0.2 + 100 * 5) / 1e6)
        self.assertTrue(real["ok"])
        self.assertFalse(real["cached"])
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["cost_usd"], 0)
        s = tr.summary()
        self.assertEqual((s["calls"], s["cached_calls"]), (1, 1))
        self.assertIn("judge", s["by_role"])
        self.assertIn("claude-opus-5-5", s["by_model"])


# ---------------------------------------------------------------------- бюджеты
class BudgetTest(CostsBase):
    def test_run_budget_blocks_call(self):
        with mock.patch.dict(os.environ, {"REQAUDIT_BUDGET_USD": "0.01"}):
            tr = self.tracker()
        sdk = FakeSDK()
        c = self.client(sdk, tracker=tr)
        with self.assertRaises(BudgetExceeded) as cm:
            c.chat([{"role": "user", "content": "x" * 35_000}], role="reviewer")
        self.assertIsInstance(cm.exception, LLMError)
        self.assertEqual(cm.exception.kind, "run")
        self.assertEqual(sdk.requests, [])  # вызов не сделан
        self.assertEqual(tr.exhausted, "")  # прогон не остановлен целиком — заблокирован только этот вызов
        self.assertTrue(tr.blocked and "не помещается в бюджет $0.01" in tr.blocked[0])

    def test_blocked_expensive_call_does_not_stop_cheap_one(self):
        # дорогой вызов Opus не помещается, дешёвый вызов Sonnet — помещается и выполняется
        with mock.patch.dict(os.environ, {"REQAUDIT_BUDGET_USD": "0.10"}):
            tr = self.tracker()
        sdk = FakeSDK()
        opus = self.client(sdk, tracker=tr)
        with self.assertRaises(BudgetExceeded):
            opus.chat([{"role": "user", "content": "x" * 35_000}], role="reviewer")
        sonnet = self.client(sdk, tracker=tr, model="claude-sonnet-5-5")
        sonnet.chat([{"role": "user", "content": "x" * 1000}], role="judge")
        self.assertEqual(len(sdk.requests), 1)
        self.assertEqual(sdk.requests[0]["model"], "claude-sonnet-5-5")
        self.assertEqual(tr.exhausted, "")

    def test_daily_limit_stops_everything(self):
        with mock.patch.dict(os.environ, {"REQAUDIT_DAILY_BUDGET_USD": "0.01"}):
            tr = self.tracker()
        with self.assertRaises(BudgetExceeded) as cm:
            tr.check("claude-opus-5-5", "reviewer", 3500)
        self.assertEqual(cm.exception.kind, "daily")
        self.assertTrue(tr.exhausted)
        with self.assertRaises(BudgetExceeded) as cm2:  # после дневного лимита блокируется всё, даже дешёвое
            tr.check("claude-haiku-5-5", "judge", 10)
        self.assertEqual(cm2.exception.kind, "daily")

    def test_run_budget_counts_spent(self):
        with mock.patch.dict(os.environ, {"REQAUDIT_BUDGET_USD": "1.0"}):
            tr = self.tracker()
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=45_000)  # $0.90
        with self.assertRaises(BudgetExceeded):
            tr.check("claude-opus-5-5", "reviewer", 3500)  # оценка ≥ $0.044 → > $1.00

    def test_daily_budget_from_ledger(self):
        path = Path(self.tmp.name) / "ledger.jsonl"
        today = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps({"ts": today, "run_id": "old", "cost_usd": 14.99, "role": "reviewer",
                                    "ok": True}) + "\n" +
                        json.dumps({"ts": "2000-01-01T00:00:00", "run_id": "older", "cost_usd": 100.0}) + "\n",
                        encoding="utf-8")
        tr = self.tracker()
        self.assertAlmostEqual(tr.daily_spent, 14.99)  # вчерашние и более старые не считаются
        with self.assertRaises(BudgetExceeded) as cm:
            tr.check("claude-opus-5-5", "reviewer", 3500)
        self.assertEqual(cm.exception.kind, "daily")
        self.assertIn("за сутки", str(cm.exception))

    def test_warning_at_80_percent_once(self):
        stats = RunStats()
        tr = self.tracker(stats=stats)
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=100_000)  # $2.00
        self.assertFalse([e for e in stats.events if "лимита" in e])
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=25_000)  # $2.50
        tr.record(provider="anthropic", model="claude-opus-5-5", role="judge", output_tokens=1_000)
        warns = [e for e in stats.events if "лимита" in e]
        self.assertEqual(len(warns), 1)
        self.assertIn("прогона", warns[0])
        self.assertTrue(any("лимита" in m for m in self.logs))  # и в консоль

    def test_anomaly_2x_median(self):
        path = Path(self.tmp.name) / "ledger.jsonl"
        rows = [{"ts": f"2026-11-{10 + i:02d}T10:00:00", "run_id": f"r{i}", "cost_usd": 0.10} for i in range(12)]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        stats = RunStats()
        tr = self.tracker(stats=stats)
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=10_000)  # $0.20
        self.assertEqual(tr.anomaly(), "")  # ровно 2× — не аномалия
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=10_000)  # $0.40
        msg = tr.anomaly()
        self.assertIn("Аномалия", msg)
        self.assertIn(msg, stats.events)

    def test_costs_report_command(self):
        tr = self.tracker()
        tr.record(provider="anthropic", model="claude-opus-5-5", role="reviewer", output_tokens=10_000)
        tr.record(provider="anthropic", model="claude-sonnet-5-5", role="judge", output_tokens=10_000)
        text = costs.report(7)
        for s in ("По дням", "По моделям", "По ролям", "Топ-5", "claude-sonnet-5-5", "дневного лимита"):
            self.assertIn(s, text)


# ---------------------------------------------------------------------- запрос к Claude
class AnthropicRequestTest(CostsBase):
    def test_system_moved_and_no_sampling_params(self):
        sdk = FakeSDK()
        c = self.client(sdk)
        c.chat([{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}], temperature=0.7,
               max_tokens=16000)
        req = sdk.requests[0]
        self.assertEqual(req["system"], "SYS")
        self.assertEqual(req["messages"], [{"role": "user", "content": "U"}])
        for k in ("temperature", "top_p", "top_k", "thinking"):
            self.assertNotIn(k, req)
        self.assertEqual(req["output_config"], {"effort": "high"})
        self.assertEqual(req["cache_control"], {"type": "ephemeral"})
        self.assertEqual(req["max_tokens"], 40000)  # минимальный потолок ANTHROPIC_MIN_MAX_TOKENS
        self.assertEqual(req["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(req["fallbacks"], "default")
        c.chat([{"role": "user", "content": "U2"}])
        self.assertEqual(sdk.requests[1]["max_tokens"], 64000)
        self.assertNotIn("system", sdk.requests[1])

    def test_effort_env_and_fallback_off(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_EFFORT": "xhigh", "ANTHROPIC_FALLBACK": "0"}):
            sdk = FakeSDK()
            c = self.client(sdk)
        c.chat([{"role": "user", "content": "U"}])
        req = sdk.requests[0]
        self.assertEqual(req["output_config"], {"effort": "xhigh"})
        self.assertNotIn("betas", req)
        self.assertNotIn("fallbacks", req)

    def test_refusal_raises_with_category(self):
        stats = RunStats()
        tr = self.tracker()
        sdk = FakeSDK(text="", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
        c = self.client(sdk, tracker=tr, stats=stats)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "U"}])
        self.assertIn("cyber", str(cm.exception))
        self.assertIn("req_test", str(cm.exception))
        self.assertEqual(stats.llm_failed, 1)
        self.assertFalse(self.ledger()[-1]["ok"])

    def test_max_tokens_warning(self):
        stats = RunStats()
        c = self.client(FakeSDK(stop_reason="max_tokens"), stats=stats)
        c.chat([{"role": "user", "content": "U"}], max_tokens=100)
        self.assertTrue(any("max_tokens" in e for e in stats.events))

    def test_min_max_tokens_env(self):
        sdk = FakeSDK()
        with mock.patch.dict(os.environ, {"ANTHROPIC_MIN_MAX_TOKENS": "1000"}):
            self.client(sdk).chat([{"role": "user", "content": "U"}], max_tokens=6000)
        self.client(sdk).chat([{"role": "user", "content": "U"}], max_tokens=50000)
        self.assertEqual([r["max_tokens"] for r in sdk.requests], [6000, 50000])

    def test_truncated_json_retried_once_with_double_limit(self):
        calls = []

        class Trunc(FakeSDK):
            def respond(self, kwargs):
                calls.append(kwargs["max_tokens"])
                self.stop_reason = "max_tokens" if len(calls) == 1 else "end_turn"
                self.text = '{"findings": [' if len(calls) == 1 else '{"findings": []}'
                return super().respond(kwargs)

        c = self.client(Trunc())
        self.assertEqual(c.chat_json("S", "U", max_tokens=40000), {"findings": []})
        self.assertEqual(calls, [40000, 64000])

    def test_truncated_at_max_limit_not_retried(self):
        sdk = FakeSDK(text='{"findings": [', stop_reason="max_tokens")
        with self.assertRaises(LLMError) as cm:
            self.client(sdk).chat_json("S", "U", max_tokens=64000)
        self.assertIn("обрезан", str(cm.exception))
        self.assertEqual(len(sdk.requests), 1)

    def test_truncated_twice_raises(self):
        sdk = FakeSDK(text='{"findings": [', stop_reason="max_tokens")
        with self.assertRaises(LLMError):
            self.client(sdk).chat_json("S", "U", max_tokens=40000)
        self.assertEqual([r["max_tokens"] for r in sdk.requests], [40000, 64000])

    def test_truncated_at_max_limit_keeps_finished_findings(self):
        sdk = FakeSDK(text='{"findings": [{"title": "A", "quotes": []}, {"title": "B", "expl', stop_reason="max_tokens")
        data = self.client(sdk).chat_json("S", "U", max_tokens=64000)
        self.assertEqual(data["findings"][0]["title"], "A")
        self.assertTrue(data["_truncated"])
        self.assertEqual(len(sdk.requests), 1)

    def test_invalid_json_without_truncation_asks_again(self):
        answers = iter(["не JSON", '{"ok": 1}'])
        sdk = FakeSDK(text=lambda kw: next(answers))
        self.assertEqual(self.client(sdk).chat_json("S", "U", max_tokens=40000), {"ok": 1})
        self.assertEqual(len(sdk.requests), 2)
        self.assertEqual(sdk.requests[1]["max_tokens"], 40000)
        self.assertEqual(sdk.requests[1]["messages"][-1]["role"], "user")
        self.assertEqual(len(sdk.requests[1]["messages"]), 3)

    def test_sdk_error_is_llm_error(self):
        import anthropic
        import httpx2

        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx2.Response(401, request=req, headers={"request-id": "req_401"})
        err = anthropic.AuthenticationError("bad key", response=resp, body=None)

        class Boom(FakeSDK):
            def respond(self, kwargs):
                raise err

        stats = RunStats()
        c = self.client(Boom(), stats=stats)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "U"}])
        self.assertIn("req_401", str(cm.exception))
        self.assertEqual(stats.llm_failed, 1)

    def test_provider_registered_first(self):
        self.assertEqual(PRIORITY[0], "anthropic")
        self.assertEqual(PROVIDERS["anthropic"][:4],
                         ("Anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-opus-5-5"))


# ---------------------------------------------------------------------- конвейер целиком (с фейковым Claude)
SPEC = """# ТЗ

## Функциональные требования
* **Ф-1.** Клиент задаёт порог от 1 000 до 50 000 ₸ включительно.
* **Ф-2.** Система быстро отправляет уведомление.

## Требования к данным
Хранить идентификатор карты и порог в тенге.
"""


def _answer(kwargs):
    return '{"findings": [], "verdicts": []}'


class PipelineCostsTest(CostsBase):
    def setUp(self):
        super().setUp()
        self.spec = Path(self.tmp.name) / "spec.md"
        self.spec.write_text(SPEC, encoding="utf-8")
        self.sdk = FakeSDK(text=_answer)
        os.environ.update({"ANTHROPIC_API_KEY": "test-key", "REQAUDIT_REVIEWERS": "anthropic",
                           "REQAUDIT_JUDGE": "anthropic",
                           "REQAUDIT_JUDGE_MODEL": "claude-sonnet-5-5", "ANTHROPIC_FOCUS": "0,2,3"})
        self.patch = mock.patch("anthropic.Anthropic", return_value=self.sdk)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_full_run_roles_and_costs(self):
        from reqaudit.pipeline import run
        from reqaudit.report import render

        result = run(str(self.spec), use_llm=True, cache_dir=None)
        models = [r["model"] for r in self.sdk.requests]
        self.assertEqual(models.count("claude-opus-5-5"), 3)  # 3 прохода ревьюера, оркестратора нет
        self.assertEqual(models[-1], "claude-sonnet-5-5")  # критик — другая модель
        self.assertTrue(result.judge_independent)
        self.assertEqual(result.judge.model, "claude-sonnet-5-5")
        self.assertEqual(set(result.costs["by_role"]), {"reviewer", "judge"})
        self.assertEqual(set(result.costs["by_model"]), {"claude-opus-5-5", "claude-sonnet-5-5"})
        self.assertGreater(result.costs["cost_usd"], 0)
        self.assertEqual(result.budget_stop, "")
        self.assertEqual(len(self.ledger()), 4)
        text = render(result, Path(self.tmp.name) / "r.md")
        self.assertNotIn("Стоимость", text)  # расходы — в report.json и журнале, не в отчёте для читателя
        self.assertIn("cost_usd", json.dumps(result.costs))

    def test_judge_effort(self):
        from reqaudit.pipeline import run

        with mock.patch.dict(os.environ, {"REQAUDIT_JUDGE_EFFORT": "medium"}):
            run(str(self.spec), use_llm=True, cache_dir=None)
        efforts = [r["output_config"]["effort"] for r in self.sdk.requests]
        self.assertEqual(efforts, ["high", "high", "high", "medium"])

    def test_preflight_over_budget_stops_before_calls(self):
        from reqaudit.cli import main

        with mock.patch.dict(os.environ, {"REQAUDIT_BUDGET_USD": "0.0001"}), \
                mock.patch("reqaudit.cli.load_dotenv"):
            code = main([str(self.spec), str(Path(self.tmp.name) / "r.md")])
        self.assertEqual(code, 3)
        self.assertEqual(self.sdk.requests, [])
        self.assertFalse((Path(self.tmp.name) / "r.md").exists())

    def test_budget_exhausted_mid_run_report_written(self):
        from reqaudit.cli import main

        out = Path(self.tmp.name) / "r.md"
        with mock.patch.dict(os.environ, {"REQAUDIT_BUDGET_USD": "0.0001"}), \
                mock.patch("reqaudit.cli.load_dotenv"):
            code = main([str(self.spec), str(out), "--force-budget", "--no-cache"])
        self.assertEqual(code, 3)
        self.assertEqual(self.sdk.requests, [])  # ни один вызов не прошёл проверку бюджета
        text = out.read_text(encoding="utf-8")
        self.assertIn("не помещается в бюджет $0.0001", text)
        self.assertIn("без критика (бюджет)", text)  # режим в паспорте — по фактически выполненным шагам
        self.assertNotIn("+ критик |", text)
        data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIn("costs", data)
        self.assertTrue(data["budget_blocked"])
        self.assertEqual(data["budget_stop"], "")

    def test_mode_line_and_no_orchestrator(self):
        from reqaudit.pipeline import run
        from reqaudit.report import render

        result = run(str(self.spec), use_llm=True, cache_dir=None)
        text = render(result, Path(self.tmp.name) / "r.md")
        self.assertIn("| Режим | правила + LLM-консилиум (3/3 проходов) + критик |", text)
        self.assertNotIn("ркестратор", text)


class JudgeProtectionTest(CostsBase):
    """Судья не может понизить доказанное противоречие в ФТ — только отклонить с обоснованием."""

    SPEC = "# ТЗ\n\n## Функциональные требования\nФ-1. Не более одного push в месяц.\nФ-2. При новом пороге отправить push.\n"

    def _run(self, verdict: str, note: str = "условия разные"):
        from reqaudit import pipeline
        from reqaudit.models import Finding, Quote

        spec = Path(self.tmp.name) / "s.md"
        spec.write_text(self.SPEC, encoding="utf-8")

        def answer(kw):
            sys_txt = kw.get("system", "")
            if "Ты — критик" in sys_txt:
                return json.dumps({"verdicts": [{"id": f"C-{n:02d}", "verdict": verdict, "proof": "У",
                                                 "severity": "major", "note": note} for n in range(1, 10)],
                                   "priority": ["C-01"]}, ensure_ascii=False)
            return json.dumps({"findings": [{"category": "contradiction", "severity": "blocker", "proof": "Д",
                                             "title": "Лимит push против push при новом пороге",
                                             "quotes": [{"text": "Не более одного push в месяц."},
                                                        {"text": "При новом пороге отправить push."}]}]},
                              ensure_ascii=False)

        sdk = FakeSDK(text=answer)
        env = {"ANTHROPIC_API_KEY": "k", "REQAUDIT_REVIEWERS": "anthropic", "ANTHROPIC_FOCUS": "0",
               "REQAUDIT_JUDGE": "anthropic", "REQAUDIT_JUDGE_MODEL": "claude-sonnet-5-5"}
        with mock.patch.dict(os.environ, env), mock.patch("anthropic.Anthropic", return_value=sdk):
            return pipeline.run(str(spec), use_llm=True, cache_dir=None)

    def test_disputed_cannot_downgrade_proven_contradiction(self):
        res = self._run("disputed")
        f = next(f for f in res.findings if f.category == "contradiction")
        self.assertEqual((f.severity, f.proof), ("blocker", "Д"))
        self.assertIn("не принято", f.judge_change)

    def test_downgrade_allowed_when_critic_cites_line(self):
        res = self._run("confirmed", note="строка 5 описывает другой случай")
        f = next(f for f in res.findings if f.category == "contradiction")
        self.assertEqual(f.proof, "У")
        self.assertNotIn("не принято", f.judge_change)

    def test_critic_merges_duplicates(self):
        from reqaudit import pipeline

        spec = Path(self.tmp.name) / "d.md"
        spec.write_text(self.SPEC, encoding="utf-8")
        seen = {"n": 0}

        def answer(kw):
            if "Ты — критик" in kw.get("system", ""):
                items = json.loads(kw["messages"][0]["content"].split("Находки (JSON):\n")[1].split("\n\nДокумент")[0])
                llm = [i["id"] for i in items if "правила" not in i["votes"]]
                return json.dumps({"verdicts": [{"id": llm[0], "verdict": "confirmed"},
                                                {"id": llm[1], "duplicate_of": llm[0]}], "priority": [llm[0]]})
            seen["n"] += 1
            line = "Не более одного push в месяц." if seen["n"] == 1 else "При новом пороге отправить push."
            return json.dumps({"findings": [{"category": "ambiguity", "severity": "major", "title": f"Тема {seen['n']}",
                                             "quotes": [{"text": line}]}]}, ensure_ascii=False)

        sdk = FakeSDK(text=answer)
        env = {"ANTHROPIC_API_KEY": "k", "REQAUDIT_REVIEWERS": "anthropic", "ANTHROPIC_FOCUS": "0,2",
               "REQAUDIT_JUDGE": "anthropic"}
        with mock.patch.dict(os.environ, env), mock.patch("anthropic.Anthropic", return_value=sdk):
            res = pipeline.run(str(spec), use_llm=True, cache_dir=None)
        llm = [f for f in res.findings if not f.is_rule]
        self.assertEqual(len(llm), 1)  # две находки склеены критиком в одну
        self.assertEqual(len(llm[0].votes), 2)  # голоса обоих проходов сохранены
        self.assertEqual(len(llm[0].quotes), 2)
        self.assertEqual(res.priority, [llm[0].fid])

    def test_rejected_is_allowed(self):
        res = self._run("rejected")
        self.assertFalse([f for f in res.findings if f.category == "contradiction" and not f.is_rule])


class CoverageTest(CostsBase):
    """У каждого требования явный статус; молчание модели не считается проверкой."""

    SPEC = ("# ТЗ\n\n## Функциональные требования\nФ-1. Клиент задаёт порог от 1 000 до 50 000 ₸.\n"
            "Ф-2. Система отправляет push при достижении порога.\nФ-3. Клиент может отключить уведомления.\n"
            "Ф-4. Отчёт формируется раз в месяц.\n")

    def test_statuses(self):
        from reqaudit import pipeline
        from reqaudit.report import render

        spec = Path(self.tmp.name) / "c.md"
        spec.write_text(self.SPEC, encoding="utf-8")

        def answer(kw):
            if "Ты — критик" in kw.get("system", ""):
                return '{"verdicts": [], "priority": []}'
            return json.dumps({
                "findings": [{"category": "ambiguity", "severity": "major", "rids": ["R-02"], "title": "Когда уходит push",
                              "quotes": [{"text": "Система отправляет push при достижении порога."}]},
                             {"category": "ambiguity", "severity": "minor", "rids": ["R-03"], "title": "Выдуманная цитата",
                              "quotes": [{"text": "такого текста в документе нет совсем"}]}],
                # R-01 проверено; R-02 дефект (находка 1); R-03 дефект, но цитата не подтвердится; R-04 пропущено;
                # [9,0,1] — чужой ID, [1.5,...] и строки — мусор
                "coverage": [[1, 0, 1], [2, 1, 1], [3, 1, 2], [9, 0, 1], ["R-04", 0, 1], [4.0, 0, 1]]}, ensure_ascii=False)

        sdk = FakeSDK(text=answer)
        env = {"ANTHROPIC_API_KEY": "k", "REQAUDIT_REVIEWERS": "anthropic", "ANTHROPIC_FOCUS": "0", "REQAUDIT_JUDGE": "anthropic"}
        with mock.patch.dict(os.environ, env), mock.patch("anthropic.Anthropic", return_value=sdk):
            res = pipeline.run(str(spec), use_llm=True, cache_dir=None)
        cov = res.coverage
        self.assertEqual(cov["R-01"]["status"], "ok")
        self.assertEqual(cov["R-02"]["status"], "defect")
        self.assertEqual(cov["R-03"]["status"], "unchecked")  # замечание удалено проверкой цитат
        self.assertEqual(cov["R-04"]["status"], "unchecked")  # записи нет (мусор не засчитан)
        text = render(res, Path(self.tmp.name) / "c_report.md")
        self.assertIn("не проверено — 2", text)
        self.assertIn("Готовность установлена не полностью", text)

    def test_rules_only_is_unchecked(self):
        from reqaudit import pipeline

        spec = Path(self.tmp.name) / "c2.md"
        spec.write_text(self.SPEC, encoding="utf-8")
        res = pipeline.run(str(spec), use_llm=False)
        self.assertTrue(all(c["status"] in ("defect", "unchecked") for c in res.coverage.values()))


class StreamDropTest(CostsBase):
    """Обрыв соединения посреди потока (ReadTimeout) не роняет прогон: повтор, затем понятная ошибка."""

    def _sdk(self, fails: int):
        sdk = FakeSDK()
        state = {"n": 0}

        class DropStream(FakeStream):
            def __iter__(self):
                state["n"] += 1
                if state["n"] <= fails:
                    raise TimeoutError("The read operation timed out")
                return iter(())

        sdk.beta.messages.stream = lambda **kw: (sdk.requests.append(kw), DropStream(sdk, kw))[1]
        return sdk

    def test_retry_after_drop(self):
        sdk = self._sdk(fails=1)
        c = self.client(sdk)
        with mock.patch("time.sleep"):
            self.assertEqual(json.loads(c.chat([{"role": "user", "content": "x"}])), {"findings": []})
        self.assertEqual(len(sdk.requests), 2)

    def test_gives_llm_error_not_crash(self):
        sdk = self._sdk(fails=10)
        c = self.client(sdk)
        with mock.patch("time.sleep"), self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "x"}])
        self.assertIn("обрыв соединения", str(cm.exception))
        self.assertEqual(len(sdk.requests), 3)  # 1 попытка + 2 повтора


class AcceptanceCoverageTest(CostsBase):
    def test_ac_matrix_parallel_and_filtered(self):
        from reqaudit import pipeline
        from reqaudit.report import render

        spec = Path(self.tmp.name) / "ac.md"
        spec.write_text("# ТЗ\n\n## Функциональные требования\nФ-1. Клиент задаёт порог.\n\nФ-2. Push при достижении порога.\n\n"
                        "## Критерии приемки\n\n| Условие | Действие | Результат |\n| :--- | :--- | :--- |\n"
                        "| Порог 100 | Покупка 100 | Push |\n", encoding="utf-8")

        def answer(kw):
            sys_txt = kw.get("system", "")
            if "покрывают требования" in sys_txt:
                return json.dumps({"ac_coverage": [{"rid": "R-01", "covered_by": [], "gap": "порог не проверяется"},
                                                   {"rid": "R-02", "covered_by": ["R-03", "R-99"], "gap": ""},
                                                   {"rid": "R-03", "covered_by": ["R-03"]},
                                                   {"rid": "R-77", "covered_by": ["R-03"]}]}, ensure_ascii=False)
            if "Ты — критик" in sys_txt:
                return '{"verdicts": [], "priority": []}'
            return '{"findings": []}'

        sdk = FakeSDK(text=answer)
        env = {"ANTHROPIC_API_KEY": "k", "REQAUDIT_REVIEWERS": "anthropic", "ANTHROPIC_FOCUS": "0", "REQAUDIT_JUDGE": "anthropic"}
        with mock.patch.dict(os.environ, env), mock.patch("anthropic.Anthropic", return_value=sdk):
            res = pipeline.run(str(spec), use_llm=True, cache_dir=None)
        kinds = {r.rid: r.kind for r in res.reqs}
        self.assertEqual(kinds.get("R-03"), "AC")
        self.assertEqual([x["rid"] for x in res.ac_coverage], ["R-01", "R-02"])  # AC и неизвестные ID отброшены
        self.assertEqual(res.ac_coverage[1]["covered_by"], ["R-03"])  # несуществующий критерий R-99 отброшен
        text = render(res, Path(self.tmp.name) / "ac_report.md")
        self.assertIn("Критериями приёмки проверяется **1 из 2**", text)


class HumanInLoopTest(CostsBase):
    """Человека спрашиваем только там, где агент не уверен, и не больше 3 вопросов."""

    def _result(self):
        from reqaudit.ingest import load_document
        from reqaudit.models import Finding, Question, Quote
        from reqaudit.pipeline import RunResult

        spec = Path(self.tmp.name) / "h.md"
        spec.write_text("# ТЗ\n\n## Функциональные требования\nФ-1. Система быстро отправляет push.\n", encoding="utf-8")
        doc = load_document(str(spec))

        def mk(fid, sev, proof, judge="confirmed", note=""):
            return Finding(fid=fid, source="llm:a", category="ambiguity", severity=sev, proof=proof, title=f"Тема {fid}",
                           quotes=[Quote(text="Система быстро отправляет push.", line=4, line_start=4, grounded=True)],
                           question=Question(text=f"Вопрос {fid}?", options=["да", "нет"]), judge=judge,
                           status_note=note, votes=["a"])

        fs = [mk("F-01", "blocker", "Д"),                      # доказано — спрашивать незачем
              mk("F-02", "major", "У", judge="disputed"),      # критик сомневается
              mk("F-03", "blocker", "У"),                      # блокер без ответа в документе
              mk("F-04", "major", "У"),
              mk("F-05", "minor", "У", note="Найдено одним ревьюером из 3."),
              mk("F-06", "minor", "Ш")]                        # оформление — не спрашиваем
        return RunResult(doc=doc, reqs=[], findings=fs, priority=["F-03"])

    def test_only_uncertain_and_ordered(self):
        from reqaudit.clarify import _ordered_questions

        ids = [f.fid for f, _why in _ordered_questions(self._result())]
        self.assertNotIn("F-01", ids)
        self.assertNotIn("F-06", ids)
        self.assertEqual(ids[:2], ["F-03", "F-02"])  # блокер из приоритета критика, затем спорное

    def test_limit_three_and_answers_change_findings(self):
        from reqaudit import clarify

        res = self._result()
        asked = []

        def fake_input(_prompt=""):
            asked.append(1)
            return "1"

        def answer(kw):
            return json.dumps({"updates": [{"id": "F-03", "status": "resolved", "rewrite": "Push уходит за 5 с."},
                                           {"id": "F-02", "status": "not_defect", "rewrite": "строка 4"}]},
                              ensure_ascii=False)

        sdk = FakeSDK(text=answer)
        costs.start_run(document="h.md", stats=res.stats, log=lambda m: None)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch("anthropic.Anthropic", return_value=sdk), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", fake_input):
            clarify.apply_answers(res, cache_dir=None)
        self.assertEqual(len(asked), 3)  # не больше трёх вопросов
        by = {f.fid: f for f in res.findings}
        self.assertNotIn("F-02", by)  # снято по ответу автора
        self.assertEqual(by["F-03"].proof, "Д")  # неопределённость снята
        self.assertEqual(by["F-03"].suggestion, "Push уходит за 5 с.")
        self.assertEqual(len(sdk.requests), 1)  # одна перепроверка на все ответы


    def test_answers_from_app_without_terminal(self):
        """Веб-приложение передаёт готовые ответы: терминал не нужен, пустые ответы пропускаются."""
        from reqaudit import clarify

        res = self._result()
        self.assertEqual([f.fid for f, _ in clarify.pending_questions(res)], ["F-03", "F-02", "F-04"])

        def answer(kw):
            return json.dumps({"updates": [{"id": "F-03", "status": "resolved", "rewrite": "Push уходит за 5 с."}]},
                              ensure_ascii=False)

        sdk = FakeSDK(text=answer)
        costs.start_run(document="h.md", stats=res.stats, log=lambda m: None)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch("anthropic.Anthropic", return_value=sdk), \
                mock.patch("builtins.input", side_effect=AssertionError("терминал не должен спрашивать")):
            clarify.apply_answers(res, cache_dir=None, answers={"F-03": "Да, 5 секунд", "F-04": "  "})
        self.assertEqual([a["fid"] for a in res.answers], ["F-03"])
        self.assertEqual({f.fid: f for f in res.findings}["F-03"].suggestion, "Push уходит за 5 с.")


class RoleModelTest(unittest.TestCase):
    def test_role_model_not_applied_to_other_provider(self):
        from reqaudit.llm import LLMClient
        from reqaudit.pipeline import pick_roles

        stats = RunStats()
        gem = LLMClient("gemini", "Google", "gemini-flash-latest", "https://x/v1", "k", stats, None)
        env = {"REQAUDIT_REVIEWERS": "anthropic", "REQAUDIT_JUDGE": "anthropic",
               "REQAUDIT_JUDGE_MODEL": "claude-sonnet-5-5"}
        with mock.patch.dict(os.environ, env):
            clients = {"gemini": gem}
            reviewers, judge = pick_roles(clients)
        self.assertEqual(reviewers, ["gemini"])
        self.assertEqual(clients[judge].model, "gemini-flash-latest")


class JsonRepairTest(unittest.TestCase):
    def test_parse_broken_json_locally(self):
        from reqaudit.llm import parse_json, repair_json

        broken = '{"findings": [{"title": "Поле "сумма" не описано", "lines": [1, 2,],},], "priority": ["C-01"]}'
        with self.assertRaises(ValueError):
            parse_json(broken)
        fixed = repair_json(broken)
        self.assertIsInstance(fixed, dict)
        self.assertEqual(fixed["priority"], ["C-01"])
        self.assertEqual(len(fixed["findings"]), 1)

    def test_chat_json_repairs_without_second_call(self):
        sdk = FakeSDK(text=lambda kw: '{"verdicts": [{"id": "F-01", "note": "см. "пример""}]}')
        stats = RunStats()
        c = AnthropicClient("anthropic", "Anthropic", "claude-sonnet-5-5", "k", stats, None, sdk=sdk)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"REQAUDIT_FAILED_DIR": d}):
            data = c.chat_json("sys", "user", role="judge")
            self.assertEqual(len(list(Path(d).iterdir())), 1)  # сырой ответ сохранён
        self.assertEqual(data["verdicts"][0]["id"], "F-01")
        self.assertEqual(len(sdk.requests), 1)  # повторного платного вызова не было
        self.assertTrue(any("исправлен локально" in e for e in stats.events))


if __name__ == "__main__":
    unittest.main()
