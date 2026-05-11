"""Generates an Arabic Binance Square hook using Gemini + few-shot examples.

Architecture: provider-agnostic. `LLMProvider` is the abstract interface; we
ship implementations for Gemini and a deterministic mock (for tests / offline
dev). To swap to Claude/OpenAI, add a new provider class.
"""

from __future__ import annotations

import json
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from engine.config import get_settings
from engine.logging_setup import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = ROOT / "prompts" / "hook_arabic.txt"
EXAMPLES_PATH = ROOT / "prompts" / "few_shot_examples.json"

# Safety guards on output
MIN_LEN = 30
MAX_LEN = 280

# Strip these — they're either bot tells or formatting noise
BANNED_PATTERNS = (
    re.compile(r"^\s*[`*]+"),         # leading code/markdown
    re.compile(r"\bclick\s+here\b", re.I),
    re.compile(r"\bhttps?://"),
)


@dataclass(slots=True)
class HookRequest:
    ticker: str
    trigger: str
    template_hint: str
    tendency: int
    context: str = ""


@dataclass(slots=True)
class HookResult:
    text: str
    provider: str
    model: str
    raw_response: dict[str, Any]


class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def generate(self, system_prompt: str, user_prompt: str) -> tuple[str, dict[str, Any]]:
        ...


class MockProvider(LLMProvider):
    name = "mock"

    async def generate(self, system_prompt: str, user_prompt: str) -> tuple[str, dict[str, Any]]:
        examples = _load_examples()
        choice = random.choice(examples)
        return choice["output"], {"mock": True, "picked": choice["template_hint"]}


class GeminiQuotaError(Exception):
    """Raised when Gemini returns a 429 / quota error so we can fall back fast."""


class GeminiTransientError(Exception):
    """Raised on retriable 5xx errors (overload / unavailable)."""


class GeminiProvider(LLMProvider):
    """REST-based client for Gemini's generateContent endpoint.

    Uses the public v1beta REST API directly so we avoid the deprecated
    `google-generativeai` package and its gRPC stack.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"
    # Models tried in order if the primary returns 503 / overload.
    FALLBACK_CHAIN: tuple[str, ...] = ("gemini-2.0-flash", "gemini-2.5-flash-lite")

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model_name = model

    async def generate(self, system_prompt: str, user_prompt: str) -> tuple[str, dict[str, Any]]:
        # Try the configured model first; if persistently overloaded, walk the
        # fallback chain. Each model gets its own retry budget.
        candidates_to_try: list[str] = [self._model_name]
        candidates_to_try.extend(m for m in self.FALLBACK_CHAIN if m != self._model_name)

        last_error: Exception | None = None
        for model in candidates_to_try:
            try:
                return await self._call_model(model, system_prompt, user_prompt)
            except (GeminiQuotaError, GeminiTransientError) as e:
                # Each model on the free tier has its own quota — keep trying
                # the next model in the chain before giving up.
                last_error = e
                log.warning(
                    "gemini_model_unavailable",
                    model=model,
                    error=str(e),
                    kind=type(e).__name__,
                )
                continue
        # Exhausted fallback chain
        raise last_error or RuntimeError("gemini_all_models_failed")

    async def _call_model(
        self, model: str, system_prompt: str, user_prompt: str
    ) -> tuple[str, dict[str, Any]]:
        import httpx  # local import keeps tests importable

        url = f"{self.BASE}/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 1.05,
                "topP": 0.95,
                "maxOutputTokens": 200,
            },
        }

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.6, min=0.6, max=4.0),
            retry=retry_if_exception_type((httpx.HTTPError, GeminiTransientError)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=30.0) as c:
                    r = await c.post(
                        url,
                        params={"key": self._api_key},
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
                    if r.status_code == 429:
                        raise GeminiQuotaError("gemini_quota_exhausted")
                    if r.status_code in (500, 502, 503, 504):
                        raise GeminiTransientError(
                            f"gemini_http_{r.status_code}: {r.text[:160]}"
                        )
                    if r.status_code >= 400:
                        try:
                            err = r.json().get("error", {})
                        except Exception:
                            err = {"message": r.text[:200]}
                        raise RuntimeError(
                            f"gemini_http_{r.status_code}: {err.get('status', '')} {err.get('message', '')[:200]}"
                        )
                    body = r.json()
                    candidates = body.get("candidates") or []
                    text = ""
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts).strip()
                    return text, {
                        "candidates": len(candidates),
                        "model": model,
                        "prompt_token_count": body.get("usageMetadata", {}).get("promptTokenCount"),
                        "finish_reason": (candidates[0].get("finishReason") if candidates else None),
                    }
        return "", {}  # pragma: no cover


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _load_examples() -> list[dict[str, Any]]:
    return json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))


def _build_user_prompt(req: HookRequest, examples: list[dict[str, Any]]) -> str:
    # Pick examples that share the same trigger family first, then fill with others.
    matching = [e for e in examples if e["trigger"] == req.trigger]
    others = [e for e in examples if e["trigger"] != req.trigger]
    chosen = (matching + others)[:6]

    parts: list[str] = ["أمثلة على الأسلوب المطلوب:\n"]
    for e in chosen:
        parts.append(
            f"- trigger={e['trigger']} | template={e['template_hint']} | tendency={e['tendency']}\n"
            f"  output: {e['output']}"
        )
    parts.append("\nالطلب الحالي:")
    parts.append(f"- ticker: {req.ticker}")
    parts.append(f"- trigger: {req.trigger}")
    parts.append(f"- template_hint: {req.template_hint}")
    parts.append(f"- tendency: {req.tendency}")
    if req.context:
        parts.append(f"- context: {req.context}")
    parts.append("\nأعد فقط نص المنشور الواحد، بدون شرح أو علامات إضافية.")
    return "\n".join(parts)


def _sanitise(text: str, ticker: str) -> str:
    """Enforce hard rules: cashtag bookends, length, no banned patterns."""
    if not text:
        return ""
    # Pull single line only
    line = text.splitlines()[0].strip()
    line = line.strip("` *_\"'")
    for pat in BANNED_PATTERNS:
        if pat.search(line):
            return ""
    # Remove all "!" characters per style guide
    line = line.replace("!", "")

    tag = f"${ticker.upper()}"
    if tag not in line:
        line = f"{tag} {line}"
    # Ensure the post ends with the cashtag too (signature pattern from analysis)
    if not line.endswith(tag):
        line = f"{line} {tag}"

    if not (MIN_LEN <= len(line) <= MAX_LEN):
        # Hard fail outside the empirically-validated band
        return ""
    return line


def build_provider() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "mock" or not s.gemini_api_key:
        if s.llm_provider == "gemini" and not s.gemini_api_key:
            log.warning("gemini_api_key_missing_using_mock")
        return MockProvider()
    if s.llm_provider == "gemini":
        return GeminiProvider(api_key=s.gemini_api_key, model=s.llm_model)
    raise ValueError(f"Unknown LLM provider: {s.llm_provider}")


class HookGenerator:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or build_provider()
        self._system_prompt = _load_system_prompt()
        self._examples = _load_examples()

    async def generate(self, req: HookRequest) -> HookResult:
        user_prompt = _build_user_prompt(req, self._examples)
        last_raw: dict[str, Any] = {}
        last_error: str | None = None
        for _ in range(3):  # up to 3 generation attempts to satisfy sanitiser
            try:
                text, raw = await self.provider.generate(self._system_prompt, user_prompt)
            except Exception as e:
                last_error = f"{type(e).__name__}:{e}"
                log.warning(
                    "hook_provider_failed",
                    provider=self.provider.name,
                    ticker=req.ticker,
                    error=last_error,
                )
                break  # Don't keep hammering on quota/network errors
            last_raw = raw
            clean = _sanitise(text, req.ticker)
            if clean:
                log.info("hook_generated", provider=self.provider.name, ticker=req.ticker)
                return HookResult(
                    text=clean,
                    provider=self.provider.name,
                    # Prefer the model actually used (the provider may have
                    # walked a fallback chain), then fall back to configured.
                    model=str(raw.get("model")) if raw.get("model") else getattr(self.provider, "_model_name", "n/a"),
                    raw_response=raw,
                )
        # Fallback: pick a few-shot example for the same trigger/tendency
        fallback = self._fallback_text(req)
        log.warning(
            "hook_fallback_used",
            ticker=req.ticker,
            trigger=req.trigger,
            error=last_error,
        )
        return HookResult(
            text=fallback,
            provider=f"{self.provider.name}+fallback",
            model="few-shot",
            raw_response={"error": last_error, **last_raw},
        )

    def _fallback_text(self, req: HookRequest) -> str:
        candidates = [e for e in self._examples if e["trigger"] == req.trigger] or self._examples
        chosen = random.choice(candidates)
        # Swap the example's ticker for ours
        text = chosen["output"]
        text = re.sub(r"\$[A-Z][A-Z0-9]{1,15}", f"${req.ticker.upper()}", text)
        return _sanitise(text, req.ticker) or f"${req.ticker.upper()} الوضع يحتاج له احتراف ${req.ticker.upper()}"
