"""
Neo v2 — the provider contract.

Two design decisions carry the whole abstraction:

1. ``system`` is a separate parameter, NOT a message. Ollama, OpenAI, Gemini
   and DeepSeek providers prepend it internally as ``{"role": "system"}``;
   Anthropic passes it as a top-level ``system=`` kwarg (a
   ``{"role": "system"}`` entry is an API error there). Without this split
   every call site would need an ``if provider == "anthropic"`` branch —
   exactly what this layer exists to prevent.

2. ``stream()`` yields plain ``str`` text deltas. That is precisely what
   ``api/services/ask_service.py:_next_or_sentinel`` already consumes, so the
   SSE layer needed zero changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMResult:
    """One non-streaming completion, normalized across providers."""

    text:     str
    model:    str
    provider: str

    # Token counts are normalized from each vendor's own field names
    # (Ollama prompt_eval_count/eval_count, OpenAI usage.prompt_tokens,
    # Anthropic usage.input_tokens...). None when the provider omits them.
    prompt_tokens:     Optional[int] = None
    completion_tokens: Optional[int] = None

    # Raw provider string ("stop", "length", "max_tokens"...). Kept unmapped
    # on purpose: it is diagnostic, not control flow.
    finish_reason: Optional[str] = None


@runtime_checkable
class LLMProvider(Protocol):
    """What ``rag/`` is allowed to know about an LLM."""

    provider: str   # "ollama" | "gemini" | "openai" | "anthropic" | "deepseek"
    model:    str

    def complete(
        self,
        *,
        system:      str,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
        json_mode:   bool = False,
    ) -> LLMResult:
        ...

    def stream(
        self,
        *,
        system:      str,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
    ) -> Iterator[str]:
        ...


__all__ = ["LLMResult", "LLMProvider"]
