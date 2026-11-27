"""Клиенты LLM: Claude (Anthropic, официальный SDK) и любой OpenAI-совместимый API
(Gemini — бесплатный ключ AI Studio, OpenAI).

- JSON-режим с автоматическим отключением, если провайдер его не поддерживает;
- повторы с экспоненциальной задержкой при 429/5xx/таймаутах;
- кэш ответов на диске (повторный прогон даёт тот же отчёт без вызовов);
- учёт вызовов и токенов для паспорта прогона;
- учёт расходов и бюджеты (reqaudit.costs): трекер передаётся клиенту, перед вызовом проверяется бюджет.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from .models import ProviderInfo, RunStats

PROVIDERS = {
    # name: (company, env key, env model, default model, env base, default base)
    "anthropic": ("Anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-opus-5-5", "ANTHROPIC_BASE_URL", ""),
    "gemini": ("Google", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-flash-latest", "GEMINI_BASE_URL",
               "https://generativelanguage.googleapis.com/v1beta/openai"),
    "openai": ("OpenAI", "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o-mini", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
}
PRIORITY = ["anthropic", "gemini", "openai"]


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, name: str, company: str, model: str, base_url: str, api_key: str | None,
                 stats: RunStats, cache_dir: Path | None, timeout: float = 240.0, tracker=None):
        self.name = name
        self.tracker = tracker  # reqaudit.costs.CostTracker | None
        self.company = company
        self.model = model
        url = base_url.rstrip("/")
        self.url = url if url.endswith("/chat/completions") else url + "/chat/completions"
        self.api_key = api_key
        self.stats = stats
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.json_mode = os.getenv(f"{name.upper()}_JSON_MODE", "1") != "0"
        # рассуждающие модели (gpt-5+, o-серия) не принимают temperature и max_tokens
        self.reasoning = bool(re.match(r"^(gpt-[5-9]|o\d)", model))
        self.no_temperature = self.reasoning
        self.tokens_param = "max_completion_tokens" if self.reasoning else "max_tokens"
        self.reasoning_effort = os.getenv(f"{name.upper()}_REASONING_EFFORT", "").strip()
        self._lock = threading.Lock()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name=self.name, company=self.company, model=self.model, base_url=self.url)

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    def clone(self, model: str) -> "LLMClient":
        """Тот же провайдер, другая модель (например, отдельная модель судьи)."""
        return LLMClient(self.name, self.company, model, self.url, self.api_key, self.stats, self.cache_dir,
                         self.timeout, tracker=self.tracker)

    # ------------------------------------------------------------------ cache
    def _cache_path(self, payload: dict) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(json.dumps({"u": self.url, **payload}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
        return self.cache_dir / f"{self.name}_{key}.json"

    # ------------------------------------------------------------------ calls
    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None,
             json_mode: bool = True, deadline: float | None = None, role: str = "reviewer",
             meta: dict | None = None) -> str:
        payload: dict = {"model": self.model, "messages": messages}
        if not self.no_temperature:
            payload["temperature"] = temperature
        if max_tokens:
            payload[self.tokens_param] = max_tokens
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        use_json = json_mode and self.json_mode
        cache = self._cache_path({**payload, "json": use_json})
        if cache and cache.exists():
            with self._lock:
                self.stats.llm_cached += 1
            if self.tracker:
                self.tracker.record(provider=self.name, model=self.model, role=role, cached=True)
            return json.loads(cache.read_text(encoding="utf-8"))["content"]
        reserved = self.tracker.check(self.model, role, _chars(messages)) if self.tracker else 0.0
        started = time.time()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        deadline_at = time.time() + (deadline or float(os.getenv("REQAUDIT_CALL_DEADLINE", "900")))
        delays = [3, 8, 20, 45]
        last_err = ""
        for attempt in range(len(delays) + 1):
            body = dict(payload)
            if use_json:
                body["response_format"] = {"type": "json_object"}
            with self._lock:
                self.stats.llm_calls += 1
            status, content, usage, err, retry_after = self._post(body, headers, deadline_at)
            if status == 200:
                with self._lock:
                    self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    self.stats.completion_tokens += int(usage.get("completion_tokens") or 0)
                if self.tracker:
                    self.tracker.record(provider=self.name, model=self.model, role=role,
                                        input_tokens=int(usage.get("prompt_tokens") or 0),
                                        output_tokens=int(usage.get("completion_tokens") or 0),
                                        latency_s=time.time() - started, reserved=reserved)
                if cache and content.strip():
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps({"content": content}, ensure_ascii=False), encoding="utf-8")
                return content
            last_err = f"HTTP {status}: {err}" if status else err
            if status == -1:
                break  # исчерпан бюджет времени
            if status in (400, 422) and use_json and re.search(r"response_format|json_object", err, re.I):
                use_json = False  # провайдер не поддерживает JSON-режим — повторяем без него
                self.json_mode = False
                continue
            if status in (400, 422) and "temperature" in payload and re.search(r"temperature", err, re.I):
                payload.pop("temperature", None)
                self.no_temperature = True
                continue
            if status in (400, 422) and "max_tokens" in payload and re.search(r"max_tokens|max_completion_tokens", err, re.I):
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                self.tokens_param = "max_completion_tokens"
                continue
            if status in (401, 403, 404):
                break
            if status and status not in (408, 409, 425, 429, 500, 502, 503, 504):
                break
            if retry_after and attempt < len(delays):
                delays[attempt] = min(retry_after + 1, 90)
            if attempt < len(delays):
                if time.time() + delays[attempt] > deadline_at:
                    break
                time.sleep(delays[attempt])
        with self._lock:
            self.stats.llm_failed += 1
        if self.tracker:
            self.tracker.record(provider=self.name, model=self.model, role=role, latency_s=time.time() - started,
                                ok=False, reserved=reserved, error=last_err)
        raise LLMError(f"{self.label}: {last_err}")

    def _post(self, body: dict, headers: dict, deadline_at: float) -> tuple[int, str, dict, str, int]:
        """Потоковый запрос: не зависает на медленных моделях, соблюдает общий бюджет времени."""
        body = {**body, "stream": True}
        if self.name == "openai":
            body["stream_options"] = {"include_usage": True}
        parts: list[str] = []
        usage: dict = {}
        try:
            timeout = httpx.Timeout(30.0, read=180.0)
            with httpx.stream("POST", self.url, headers=headers, json=body, timeout=timeout) as r:
                if r.status_code != 200:
                    text = r.read().decode("utf-8", "replace")[:500]
                    ra = r.headers.get("retry-after", "")
                    return r.status_code, "", {}, text, int(ra) if ra.isdigit() else 0
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" not in ctype:
                    data = json.loads(r.read())
                    return 200, _extract_content(data), data.get("usage") or {}, "", 0
                for line in r.iter_lines():
                    if time.time() > deadline_at:
                        return -1, "", {}, f"превышен бюджет времени ({len(''.join(parts))} символов получено)", 0
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        ev = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("usage"):
                        usage = ev["usage"]
                    for ch in ev.get("choices") or []:
                        delta = (ch.get("delta") or {}).get("content") or (ch.get("message") or {}).get("content")
                        if delta:
                            parts.append(delta)
            return 200, "".join(parts), usage, "", 0
        except httpx.HTTPError as e:
            return 0, "", {}, f"{type(e).__name__}: {e}", 0

    def chat_json(self, system: str, user: str, temperature: float = 0.0, max_tokens: int | None = None,
                  deadline: float | None = None, role: str = "reviewer") -> dict:
        return _chat_json(self, system, user, temperature, max_tokens, deadline, role)


def _chat_json(client, system: str, user: str, temperature: float, max_tokens: int | None,
               deadline: float | None, role: str) -> dict:
    """Вызов с разбором JSON.

    - ответ обрезан по max_tokens → один повтор того же запроса с удвоенным лимитом (до ANTHROPIC_MAX_TOKENS);
      если лимит уже максимальный или повтор снова обрезан — LLMError (повтор с тем же лимитом только жжёт деньги);
    - JSON невалиден без обрезки → одна просьба переписать ответ строго JSON-объектом.
    """
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    meta: dict = {}
    content = client.chat(messages, temperature=temperature, max_tokens=max_tokens, deadline=deadline, role=role,
                          meta=meta)
    try:
        return parse_json(content)
    except ValueError:
        _save_raw(client, role, content)
    if not meta.get("truncated"):
        repaired = repair_json(content)
        if repaired is not None:  # мелкая порча (кавычка, запятая) — чиним локально, без нового платного вызова
            _event(client, f"{client.label} ({role}): JSON ответа исправлен локально (json-repair), повтор не нужен")
            return repaired
    if meta.get("truncated"):
        used = int(meta.get("max_tokens") or max_tokens or ANTHROPIC_MAX_TOKENS)
        if used >= ANTHROPIC_MAX_TOKENS:
            salvaged = _salvage(client, role, content, used)
            if salvaged is not None:
                return salvaged
            raise LLMError(f"{client.label} ({role}): ответ обрезан по max_tokens={used}, JSON неполный")
        bigger = min(used * 2, ANTHROPIC_MAX_TOKENS)
        meta = {}
        content = client.chat(messages, temperature=temperature, max_tokens=bigger, deadline=deadline, role=role,
                              meta=meta)
        try:
            return parse_json(content)
        except ValueError as e:
            why = f"обрезан по max_tokens={bigger}" if meta.get("truncated") else "невалидный JSON"
            salvaged = _salvage(client, role, content, bigger)
            if salvaged is not None:
                return salvaged
            raise LLMError(f"{client.label} ({role}): после повтора с max_tokens={bigger} ответ {why}") from e
    messages += [{"role": "assistant", "content": content[:4000]},
                 {"role": "user", "content": "Ответ не является валидным JSON. Верни тот же ответ строго одним JSON-объектом, без пояснений и markdown."}]
    content = client.chat(messages, temperature=0.0, max_tokens=max_tokens, role=role)
    return parse_json(content)


def _salvage(client, role: str, content: str, limit: int) -> dict | None:
    """Обрезанный ответ: оставляем то, что модель успела дописать (законченные находки), вместо потери всего прохода.
    Недописанный последний объект отбрасывает обычная проверка находок (нет цитат — нет находки)."""
    data = repair_json(content)
    if not data or not any(isinstance(v, list) and v for v in data.values()):
        return None  # спасать нечего — честная ошибка лучше пустого «проверено»
    _event(client, f"{client.label} ({role}): ответ обрезан по max_tokens={limit} — использована дописанная часть")
    data["_truncated"] = True
    return data


def repair_json(text: str) -> dict | None:
    """Последняя попытка разобрать почти-JSON (неэкранированные кавычки, висячие запятые и т.п.)."""
    try:
        import json_repair
    except ImportError:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.S)
    start = t.find("{")
    if start < 0:
        return None
    try:
        v = json_repair.loads(t[start:])
    except Exception:  # noqa: BLE001 — библиотека может бросить что угодно на мусоре
        return None
    return v if isinstance(v, dict) and v else None


def _event(client, msg: str) -> None:
    stats = getattr(client, "stats", None)
    if stats is not None:
        with client._lock:
            stats.events.append(msg)


def _save_raw(client, role: str, content: str) -> None:
    """Сохранить сырой ответ, который не разобрался как JSON, — для разбора причин."""
    try:
        tracker = getattr(client, "tracker", None)
        run_id = getattr(tracker, "run_id", "") or time.strftime("%Y%m%d-%H%M%S")
        d = Path(os.getenv("REQAUDIT_FAILED_DIR", ".reqaudit_failed"))
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{run_id}_{role}_{re.sub(r'[^A-Za-z0-9_.-]', '_', client.label)}_{int(time.time() * 1000) % 100000}.txt"
        path.write_text(content or "", encoding="utf-8")
        _event(client, f"{client.label} ({role}): невалидный JSON, сырой ответ сохранён в {path}")
    except OSError:
        pass


def _chars(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else len(json.dumps(m.get("content"),
               ensure_ascii=False)) for m in messages)


def _extract_content(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        for k in ("content", "text", "output"):
            if isinstance(data.get(k), str):
                return data[k]
        msg = data.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    return ""


def parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    # модели «с рассуждением» иногда добавляют <think>…</think>
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"items": v}
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        chunk = t[start : end + 1]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            chunk2 = re.sub(r",\s*([}\]])", r"\1", chunk)  # висячие запятые
            try:
                return json.loads(chunk2)
            except json.JSONDecodeError as e:
                raise ValueError(f"invalid JSON: {e}") from e
    raise ValueError("JSON not found in response")


EFFORTS = ("low", "medium", "high", "xhigh", "max")
ANTHROPIC_MAX_TOKENS = 64000
# Минимальный потолок ответа Claude: reasoning-токены входят в max_tokens, при 16000 JSON обрезался.
# Это потолок, а не плата — оплачиваются только реально сгенерированные токены.
ANTHROPIC_MIN_MAX_TOKENS = 40000


def anthropic_min_max_tokens() -> int:
    try:
        return int(os.getenv("ANTHROPIC_MIN_MAX_TOKENS", "").strip() or ANTHROPIC_MIN_MAX_TOKENS)
    except ValueError:
        return ANTHROPIC_MIN_MAX_TOKENS
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    """Claude через официальный SDK `anthropic`: потоковый вызов, effort, server-side fallback при отказе.

    Интерфейс совпадает с LLMClient: chat(), chat_json(), info, label, тот же дисковый кэш и учёт в RunStats.
    temperature/top_p/top_k и thinking не передаются (на текущих моделях это 400) — глубину задаёт effort.
    """

    def __init__(self, name: str, company: str, model: str, api_key: str | None, stats: RunStats,
                 cache_dir: Path | None, timeout: float = 240.0, tracker=None, base_url: str | None = None,
                 sdk=None):
        self.name = name
        self.company = company
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.url = base_url or "https://api.anthropic.com/v1/messages"
        self.stats = stats
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.tracker = tracker
        self.reasoning_effort = os.getenv("ANTHROPIC_EFFORT", "").strip() or "high"
        self.fallback = os.getenv("ANTHROPIC_FALLBACK", "1").strip() != "0"
        self._lock = threading.Lock()
        self._sdk = sdk

    @property
    def sdk(self):
        if self._sdk is None:
            import anthropic

            kw = {"api_key": self.api_key, "max_retries": int(os.getenv("ANTHROPIC_MAX_RETRIES", "2")),
                  "timeout": anthropic.Timeout(self.timeout, connect=30.0, read=180.0)}
            if self.base_url:
                kw["base_url"] = self.base_url
            self._sdk = anthropic.Anthropic(**kw)
        return self._sdk

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name=self.name, company=self.company, model=self.model, base_url=self.url)

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    def clone(self, model: str) -> "AnthropicClient":
        c = AnthropicClient(self.name, self.company, model, self.api_key, self.stats, self.cache_dir, self.timeout,
                            tracker=self.tracker, base_url=self.base_url, sdk=self._sdk)
        c.reasoning_effort, c.fallback = self.reasoning_effort, self.fallback
        return c

    def _effort(self) -> str:
        e = (self.reasoning_effort or "high").strip().lower()
        if e not in EFFORTS:
            with self._lock:
                self.stats.events.append(f"{self.label}: недопустимый effort «{e}», использую high")
            e = "high"
        return e

    def build_request(self, messages: list[dict], max_tokens: int | None = None) -> dict:
        """Параметры для client.beta.messages.stream(...): system-сообщения → параметр system."""
        system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        msgs = [{"role": m["role"], "content": m.get("content") or ""} for m in messages if m.get("role") != "system"]
        req: dict = {
            "model": self.model,
            "max_tokens": (min(max(int(max_tokens), anthropic_min_max_tokens()), ANTHROPIC_MAX_TOKENS) if max_tokens
                           else ANTHROPIC_MAX_TOKENS),
            "messages": msgs,
            "output_config": {"effort": self._effort()},
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            req["system"] = system
        if self.fallback:
            req["betas"] = [FALLBACK_BETA]
            req["fallbacks"] = "default"
        return req

    def _cache_path(self, req: dict) -> Path | None:
        if not self.cache_dir:
            return None
        key_src = {k: v for k, v in req.items() if k not in ("betas", "fallbacks", "cache_control")}
        key = hashlib.sha256(json.dumps({"u": self.url, **key_src}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
        return self.cache_dir / f"{self.name}_{key}.json"

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None,
             json_mode: bool = True, deadline: float | None = None, role: str = "reviewer",
             meta: dict | None = None) -> str:
        """meta (необязательно) — словарь, куда пишутся truncated (ответ обрезан по max_tokens) и max_tokens."""
        # temperature и json_mode игнорируются: Claude не принимает temperature, JSON задаётся промптом
        import anthropic

        req = self.build_request(messages, max_tokens)
        cache = self._cache_path(req)
        if cache and cache.exists():
            with self._lock:
                self.stats.llm_cached += 1
            if self.tracker:
                self.tracker.record(provider=self.name, model=self.model, role=role, cached=True)
            return json.loads(cache.read_text(encoding="utf-8"))["content"]
        reserved = self.tracker.check(self.model, role, _chars(messages)) if self.tracker else 0.0
        deadline_at = time.time() + (deadline or float(os.getenv("REQAUDIT_CALL_DEADLINE", "900")))
        started = time.time()
        with self._lock:
            self.stats.llm_calls += 1
        msg, err, request_id = None, "", ""
        usage = None
        attempts = int(os.getenv("ANTHROPIC_STREAM_RETRIES", "2")) + 1
        for attempt in range(1, attempts + 1):
            msg, err, retryable = None, "", False
            try:
                api = self.sdk.beta.messages if self.fallback else self.sdk.messages
                body = req if self.fallback else {k: v for k, v in req.items() if k not in ("betas", "fallbacks")}
                with api.stream(**body) as stream:
                    request_id = _stream_request_id(stream)
                    for _event in stream:
                        if time.time() > deadline_at:
                            usage = getattr(_snapshot(stream), "usage", None)
                            err = "превышен бюджет времени"
                            break
                    if not err:
                        msg = stream.get_final_message()
                        request_id = getattr(msg, "_request_id", None) or request_id
            except anthropic.AuthenticationError as e:
                err, request_id = f"неверный ANTHROPIC_API_KEY (401): {e.message}", e.request_id or ""
            except anthropic.NotFoundError as e:
                err, request_id = f"модель {self.model} не найдена (404): {e.message}", e.request_id or ""
            except anthropic.RateLimitError as e:
                err, request_id, retryable = f"лимит запросов (429) после повторов: {e.message}", e.request_id or "", True
            except anthropic.APIStatusError as e:
                err, request_id = f"HTTP {e.status_code}: {e.message}", e.request_id or ""
                retryable = e.status_code >= 500
            except anthropic.APIConnectionError as e:
                err, retryable = f"{type(e).__name__}: {e}", True
            except Exception as e:  # noqa: BLE001 — обрыв посреди потока приходит «сырой» ошибкой транспорта (ReadTimeout и т.п.)
                err, retryable = f"обрыв соединения во время ответа ({type(e).__name__}: {str(e)[:120]})", True
            if not (err and retryable and attempt < attempts and time.time() + 10 * attempt < deadline_at):
                break
            with self._lock:
                self.stats.events.append(f"{self.label} ({role}): {err[:120]} — повтор {attempt}/{attempts - 1}")
            time.sleep(10 * attempt)
        latency = time.time() - started
        if msg is not None:
            usage = msg.usage
        tok = _usage(usage)
        with self._lock:
            self.stats.prompt_tokens += tok["input_tokens"] + tok["cache_read"] + tok["cache_write"]
            self.stats.completion_tokens += tok["output_tokens"]
        if msg is not None and msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            err = f"модель отказалась отвечать (refusal, категория: {cat or 'не указана'})"
        if err:
            if request_id:
                err += f" [request_id {request_id}]"
            with self._lock:
                self.stats.llm_failed += 1
            if self.tracker:
                self.tracker.record(provider=self.name, model=self.model, role=role, latency_s=latency, ok=False,
                                    reserved=reserved, error=err, **tok)
            raise LLMError(f"{self.label}: {err}")
        served = str(getattr(msg, "model", "") or self.model)
        if self.tracker:
            self.tracker.record(provider=self.name, model=served, role=role, latency_s=latency, reserved=reserved, **tok)
        if msg.stop_reason == "max_tokens":
            with self._lock:
                self.stats.events.append(f"{self.label} ({role}): ответ обрезан по max_tokens={req['max_tokens']}"
                                         + (f" [request_id {request_id}]" if request_id else ""))
        if meta is not None:
            meta["truncated"] = msg.stop_reason == "max_tokens"
            meta["max_tokens"] = req["max_tokens"]
        content = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if cache and content.strip() and msg.stop_reason != "max_tokens":
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"content": content}, ensure_ascii=False), encoding="utf-8")
        return content

    def chat_json(self, system: str, user: str, temperature: float = 0.0, max_tokens: int | None = None,
                  deadline: float | None = None, role: str = "reviewer") -> dict:
        return _chat_json(self, system, user, temperature, max_tokens, deadline, role)


def _usage(u) -> dict:
    def g(name):
        return int(getattr(u, name, 0) or 0) if u is not None else 0

    return {"input_tokens": g("input_tokens"), "output_tokens": g("output_tokens"),
            "cache_read": g("cache_read_input_tokens"), "cache_write": g("cache_creation_input_tokens")}


def _snapshot(stream):
    try:
        return stream.current_message_snapshot
    except Exception:
        return None


def _stream_request_id(stream) -> str:
    try:
        return stream.response.headers.get("request-id") or ""
    except Exception:
        return ""


def available_clients(stats: RunStats, cache_dir: Path | None, tracker=None) -> dict[str, "LLMClient | AnthropicClient"]:
    clients: dict = {}
    for name in PRIORITY:
        company, key_env, model_env, default_model, base_env, default_base = PROVIDERS[name]
        key = os.getenv(key_env, "").strip() if key_env else ""
        model = os.getenv(model_env, "").strip() or default_model
        base = os.getenv(base_env, "").strip() or default_base
        if not key or not model:
            continue
        if name == "anthropic":
            clients[name] = AnthropicClient(name, company, model, key, stats, cache_dir, tracker=tracker,
                                            base_url=base or None)
            continue
        clients[name] = LLMClient(name, company, model, base, key or None, stats, cache_dir, tracker=tracker)
    return clients
