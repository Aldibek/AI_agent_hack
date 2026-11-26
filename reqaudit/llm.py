"""Клиент LLM: любой OpenAI-совместимый API (AlemLLM, Gemini, Groq, OpenRouter, OpenAI, Ollama).

- JSON-режим с автоматическим отключением, если провайдер его не поддерживает;
- повторы с экспоненциальной задержкой при 429/5xx/таймаутах;
- кэш ответов на диске (повторный прогон даёт тот же отчёт без вызовов);
- учёт вызовов и токенов для паспорта прогона.
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
    "alem": ("alem.ai (Казахстан)", "ALEM_API_KEY", "ALEM_MODEL", "alemllm", "ALEM_BASE_URL", "https://llm.alem.ai/v1"),
    "gemma": ("Google Gemma (через alem.ai)", "GEMMA_API_KEY", "GEMMA_MODEL", "gemma4", "GEMMA_BASE_URL",
              "https://llm.alem.ai/v1"),
    "gemini": ("Google", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-flash-latest", "GEMINI_BASE_URL",
               "https://generativelanguage.googleapis.com/v1beta/openai"),
    "groq": ("Groq", "GROQ_API_KEY", "GROQ_MODEL", "llama-3.3-70b-versatile", "GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    "openrouter": ("OpenRouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "", "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    "openai": ("OpenAI", "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o-mini", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "ollama": ("локальная модель (Ollama)", "", "OLLAMA_MODEL", "", "OLLAMA_BASE_URL", ""),
}
PRIORITY = ["alem", "gemma", "gemini", "groq", "openrouter", "openai", "ollama"]


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, name: str, company: str, model: str, base_url: str, api_key: str | None,
                 stats: RunStats, cache_dir: Path | None, timeout: float = 240.0):
        self.name = name
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

    # ------------------------------------------------------------------ cache
    def _cache_path(self, payload: dict) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(json.dumps({"u": self.url, **payload}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
        return self.cache_dir / f"{self.name}_{key}.json"

    # ------------------------------------------------------------------ calls
    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None,
             json_mode: bool = True, deadline: float | None = None) -> str:
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
            return json.loads(cache.read_text(encoding="utf-8"))["content"]
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
                  deadline: float | None = None) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        content = self.chat(messages, temperature=temperature, max_tokens=max_tokens, deadline=deadline)
        try:
            return parse_json(content)
        except ValueError:
            messages += [{"role": "assistant", "content": content[:4000]},
                         {"role": "user", "content": "Ответ не является валидным JSON. Верни тот же ответ строго одним JSON-объектом, без пояснений и markdown."}]
            content = self.chat(messages, temperature=0.0, max_tokens=max_tokens)
            return parse_json(content)


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


def available_clients(stats: RunStats, cache_dir: Path | None) -> dict[str, LLMClient]:
    clients: dict[str, LLMClient] = {}
    for name in PRIORITY:
        company, key_env, model_env, default_model, base_env, default_base = PROVIDERS[name]
        key = os.getenv(key_env, "").strip() if key_env else ""
        model = os.getenv(model_env, "").strip() or default_model
        base = os.getenv(base_env, "").strip() or default_base
        if name == "ollama":
            if not (base and model):
                continue
            base = base.rstrip("/")
            if not base.endswith("/v1") and not base.endswith("/chat/completions"):
                base += "/v1"
        elif not key or not model:
            continue
        clients[name] = LLMClient(name, company, model, base, key or None, stats, cache_dir)
    return clients
