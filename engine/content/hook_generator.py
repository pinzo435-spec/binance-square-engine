"""Generates an Arabic Binance Square hook using Gemini + few-shot examples.

Architecture: provider-agnostic. `LLMProvider` is the abstract interface; we
ship implementations for Gemini and a deterministic mock (for tests / offline
dev). To swap to Claude/OpenAI, add a new provider class.
"""

from __future__ import annotations

import asyncio
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


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        # Lazy import to keep tests importable without the SDK.
        import google.generativeai as genai  # type: ignore

        genai.configure(api_key=api_key)
        self._genai = genai
        self._model_name = model

    async def generate(self, system_prompt: str, user_prompt: str) -> tuple[str, dict[str, Any]]:
        def _call() -> tuple[str, dict[str, Any]]:
            model = self._genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=system_prompt,
                generation_config={
                    "temperature": 1.05,
                    "top_p": 0.95,
                    "max_output_tokens": 200,
                },
            )
            r = model.generate_content(user_prompt)
            text = (r.text or "").strip()
            return text, {"candidates": len(r.candidates or []), "model": self._model_name}

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                return await asyncio.to_thread(_call)
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
        for _ in range(3):  # up to 3 generation attempts to satisfy sanitiser
            text, raw = await self.provider.generate(self._system_prompt, user_prompt)
            last_raw = raw
            clean = _sanitise(text, req.ticker)
            if clean:
                log.info("hook_generated", provider=self.provider.name, ticker=req.ticker)
                return HookResult(
                    text=clean,
                    provider=self.provider.name,
                    model=getattr(self.provider, "_model_name", "n/a"),
                    raw_response=raw,
                )
        # Fallback: pick a few-shot example for the same trigger/tendency
        fallback = self._fallback_text(req)
        log.warning("hook_fallback_used", ticker=req.ticker, trigger=req.trigger)
        return HookResult(
            text=fallback,
            provider=f"{self.provider.name}+fallback",
            model="few-shot",
            raw_response=last_raw,
        )

    def _fallback_text(self, req: HookRequest) -> str:
        candidates = [e for e in self._examples if e["trigger"] == req.trigger] or self._examples
        chosen = random.choice(candidates)
        # Swap the example's ticker for ours
        text = chosen["output"]
        text = re.sub(r"\$[A-Z][A-Z0-9]{1,15}", f"${req.ticker.upper()}", text)
        return _sanitise(text, req.ticker) or f"${req.ticker.upper()} الوضع يحتاج له احتراف ${req.ticker.upper()}"
