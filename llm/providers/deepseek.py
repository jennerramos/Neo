"""
DeepSeek provider — OpenAI-wire-compatible, so nearly free.

Caveat worth knowing before switching ``LLM_MODEL``: ``deepseek-reasoner``
ignores ``temperature`` and emits its chain of thought in a separate
``reasoning_content`` field. The OpenAI-compatible ``content`` field carries
only the final answer, so nothing leaks into a trustee-facing response.
"""
from __future__ import annotations

from llm.providers.openai import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    provider = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"


__all__ = ["DeepSeekProvider"]
