"""
Google Gemini provider.

Google ships an OpenAI-wire-compatible endpoint, so this rides the exact same
SDK, streaming decoder and error mapping as ``OpenAIProvider`` — only the base
URL differs. No ``google-genai`` dependency. If the compatibility layer ever
blocks something Neo needs, swapping in the native SDK is a change to this one
file; no caller notices.

Model choice is pure config (``LLM_MODEL``):
    gemini-2.5-flash       cheap + fast — the sensible default
    gemini-2.5-pro         escalate here when Flash reasons too shallowly
Take the exact current model ID from Google AI Studio; this layer just
forwards the string.

Thinking budget: Gemini 2.5 models reason before answering, and those thinking
tokens are billed against the SAME ``max_tokens`` budget as the visible answer.
A cap that is fine for qwen2.5:14b can therefore be consumed entirely before
the first answer token. Two levers:
  - raise ``LLM_MAX_TOKENS`` (2048+ is a reasonable floor here), and/or
  - set ``LLM_REASONING_EFFORT=none|low|medium|high`` to cap the thinking.
``OpenAIProvider`` turns the truncated-empty case into a clear error rather
than a blank trustee answer.
"""
from __future__ import annotations

from typing import Optional

from llm.providers.openai import OpenAIProvider


class GeminiProvider(OpenAIProvider):
    provider = "gemini"
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __init__(self, *, reasoning_effort: str = "", **kwargs) -> None:
        self._reasoning_effort = (reasoning_effort or "").strip().lower()
        super().__init__(**kwargs)

    def _extra(self) -> dict:
        if self._reasoning_effort:
            return {"reasoning_effort": self._reasoning_effort}
        return {}


__all__ = ["GeminiProvider"]
