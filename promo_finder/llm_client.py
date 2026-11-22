"""Step 2: Model-Swappable LLM Client with JSON mode, retry/backoff, caching, and cost tracking.

Supports:
  - gemini   (default: gemini-2.5-flash)
  - openai   (default: gpt-4o-mini)
  - anthropic (default: claude-3-5-haiku-latest)

Uses official SDKs when installed and automatically falls back to stdlib HTTPS
REST calls so the pipeline runs even in minimal environments.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class PipelineCancelled(Exception):
    """Raised when a running pipeline is cancelled by the user."""

DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "runcrate": "Qwen/Qwen3.8-Flash",
    "custom": "",
}

# Approximate USD per 1M tokens (input, output) for cost estimation in reports
MODEL_PRICING_PER_1M = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.075, 0.30),
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash-lite": (0.075, 0.30),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
}


def load_dotenv_simple(env_path: str | Path = ".env") -> None:
    """Load key=value pairs from .env without requiring python-dotenv."""
    p = Path(env_path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")  # drop inline comments
        if k and v and k not in os.environ:
            os.environ[k] = v


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Robustly parse JSON from an LLM response, stripping thinking tags & markdown fences."""
    text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"items": parsed}
    except json.JSONDecodeError:
        # Fallback: find first { ... } block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


class LLMClient:
    """Unified, model-swappable JSON LLM client with disk caching and rate-limit handling."""

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        cache_dir: str | Path = "outputs/.cache",
        temperature: float = 0.2,
        min_delay_sec: float = 0.5,
        max_retries: int = 5,
        cancel_event: threading.Event | None = None,
    ) -> None:
        load_dotenv_simple()
        env_provider = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
        self.provider = (provider or env_provider).strip().lower()
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(f"Unsupported provider {self.provider!r}. Choose from: {list(DEFAULT_MODELS)}")
        same_provider = self.provider == env_provider
        env_model = os.environ.get("LLM_MODEL") if same_provider else None
        self.model = (model or env_model or DEFAULT_MODELS[self.provider]).strip()
        # Optional per-role model overrides: LLM_MODEL_BIBLE / _FINDER / _STITCHER / _JUDGE
        self.role_models: dict[str, str] = {}
        if same_provider:
            for role in ("bible", "finder", "stitcher", "judge"):
                v = os.environ.get(f"LLM_MODEL_{role.upper()}", "").strip()
                if v:
                    self.role_models[role] = v
        self.temperature = temperature
        self.min_delay_sec = min_delay_sec
        self.max_retries = max_retries
        self.cancel_event = cancel_event
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_call_ts = 0.0

        # Resolve API key & optional custom OpenAI-compatible base URL
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).strip()
        env_key_map = {
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "runcrate": "RUNCRATE_API_KEY",
            "custom": "CUSTOM_API_KEY",
        }
        self.api_key = (api_key or "").strip() or os.environ.get(env_key_map[self.provider], "").strip()
        if self.provider == "gemini" and not self.api_key:
            self.api_key = os.environ.get("GOOGLE_API_KEY", "").strip()

        self.stats: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "api_calls": 0,
            "cache_hits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "stages": {},
        }

    def _model_for(self, stage: str) -> str:
        if stage == "show_bible":
            role = "bible"
        elif stage.startswith("ep_"):
            role = "finder"
        elif stage == "cross_episode_stitcher":
            role = "stitcher"
        elif stage.startswith("blind_judge"):
            role = "judge"
        else:
            role = ""
        return self.role_models.get(role, self.model)

    def _cache_key(self, stage: str, system_prompt: str, user_prompt: str) -> Path:
        payload = json.dumps(
            {
                "provider": self.provider,
                "model": self._model_for(stage),
                "stage": stage,
                "system": system_prompt,
                "user": user_prompt,
            },
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()[:24]
        return self.cache_dir / f"{stage}_{digest}.json"

    def _record_usage(self, stage: str, in_tok: int, out_tok: int, from_cache: bool) -> None:
        m = self._model_for(stage)
        in_rate, out_rate = MODEL_PRICING_PER_1M.get(m, (0.15, 0.60))
        self.stats["cost_usd"] = self.stats.get("cost_usd", 0.0) + (
            in_tok / 1e6 * in_rate + out_tok / 1e6 * out_rate
        )
        st = self.stats["stages"].setdefault(
            stage, {"api_calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
        )
        if from_cache:
            self.stats["cache_hits"] += 1
            st["cache_hits"] += 1
        else:
            self.stats["api_calls"] += 1
            st["api_calls"] += 1
        self.stats["input_tokens"] += in_tok
        self.stats["output_tokens"] += out_tok
        st["input_tokens"] += in_tok
        st["output_tokens"] += out_tok

    def estimate_cost_usd(self) -> float:
        return round(self.stats.get("cost_usd", 0.0), 4)

    def _check_cancel(self) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            raise PipelineCancelled("Stopped by user.")

    def _sleep_or_cancel(self, seconds: float) -> None:
        if seconds <= 0:
            self._check_cancel()
            return
        if self.cancel_event:
            if self.cancel_event.wait(seconds):
                raise PipelineCancelled("Stopped by user.")
        else:
            time.sleep(seconds)

    def call_json(
        self,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Execute a JSON-structured LLM call with caching and exponential backoff."""
        self._check_cancel()
        cache_file = self._cache_key(stage, system_prompt, user_prompt)
        if use_cache and cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            self._record_usage(
                stage,
                cached.get("input_tokens", 0),
                cached.get("output_tokens", 0),
                from_cache=True,
            )
            return cached["data"]

        if not self.api_key:
            raise RuntimeError(
                f"Missing API key for provider '{self.provider}'. "
                f"Set {self.provider.upper()}_API_KEY in .env or your environment."
            )

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._check_cancel()
            elapsed = time.time() - self._last_call_ts
            if elapsed < self.min_delay_sec:
                self._sleep_or_cancel(self.min_delay_sec - elapsed)
            self._check_cancel()
            self._last_call_ts = time.time()

            try:
                raw_text, in_tok, out_tok = self._dispatch(
                    self._model_for(stage), system_prompt, user_prompt
                )
                self._check_cancel()
                data = _extract_json_object(raw_text)
                self._record_usage(stage, in_tok, out_tok, from_cache=False)
                if use_cache:
                    cache_file.write_text(
                        json.dumps(
                            {
                                "provider": self.provider,
                                "model": self.model,
                                "stage": stage,
                                "input_tokens": in_tok,
                                "output_tokens": out_tok,
                                "data": data,
                            },
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                return data
            except PipelineCancelled:
                raise
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                last_err = RuntimeError(f"HTTP {e.code} from {self.provider}: {body[:400]}")
                if e.code in (429, 500, 502, 503, 504):
                    wait = min(60.0, (2 ** attempt) + 1.5)
                    print(f"  [{stage}] HTTP {e.code} (attempt {attempt}/{self.max_retries}), retrying in {wait:.1f}s...")
                    self._sleep_or_cancel(wait)
                else:
                    raise last_err
            except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, Exception) as e:
                last_err = e
                wait = min(30.0, (2 ** attempt))
                print(f"  [{stage}] Error '{type(e).__name__}: {str(e)[:120]}' (attempt {attempt}/{self.max_retries}), retrying in {wait:.1f}s...")
                self._sleep_or_cancel(wait)

        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts in stage '{stage}': {last_err}")

    def _dispatch(self, model: str, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        if self.provider == "gemini":
            return self._call_gemini_rest(model, system_prompt, user_prompt)
        if self.provider == "openai":
            return self._call_openai_rest(model, system_prompt, user_prompt)
        if self.provider == "runcrate":
            return self._call_openai_rest(
                model, system_prompt, user_prompt, url="https://api.runcrate.ai/v1/chat/completions"
            )
        if self.provider == "custom":
            if not self.base_url:
                raise RuntimeError("Custom provider requires a Base URL (e.g. https://api.groq.com/openai/v1).")
            u = self.base_url.rstrip("/")
            if not u.endswith("/chat/completions"):
                u = f"{u}/chat/completions"
            return self._call_openai_rest(model, system_prompt, user_prompt, url=u)
        if self.provider == "anthropic":
            return self._call_anthropic_rest(model, system_prompt, user_prompt)
        raise ValueError(f"Unknown provider: {self.provider}")

    def _post_json(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        req_headers = {"User-Agent": "PromoFinder/1.0", **headers}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=req_headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call_gemini_rest(self, model: str, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.api_key}"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
            },
        }
        resp = self._post_json(url, {"Content-Type": "application/json"}, payload)
        candidates = resp.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {json.dumps(resp)[:400]}")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        usage = resp.get("usageMetadata", {})
        return text, int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0))

    def _call_openai_rest(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        url: str = "https://api.openai.com/v1/chat/completions",
    ) -> tuple[str, int, int]:
        payload = {
            "model": model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            resp = self._post_json(url, headers, payload)
        except urllib.error.HTTPError as e:
            # Some OpenAI-compatible gateways/models reject response_format=json_object
            if e.code == 400 and "response_format" in payload:
                payload.pop("response_format", None)
                resp = self._post_json(url, headers, payload)
            else:
                raise
        text = resp["choices"][0]["message"]["content"] or ""
        usage = resp.get("usage", {})
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    def _call_anthropic_rest(self, model: str, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 4096,
            "temperature": self.temperature,
            "system": system_prompt + "\nReturn ONLY valid JSON with no surrounding text.",
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = self._post_json(url, headers, payload)
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        usage = resp.get("usage", {})
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
