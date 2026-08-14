"""
Provider selection from config.

``get_provider()`` is ``lru_cache``d, which reproduces the module-singleton
behavior the old ``_get_client()`` had: one client per (provider, profile) per
process. Switching providers is therefore env + restart — ``config.py`` reads
the environment once at import and these handles live for the process.

Two profiles, because the two call sites have genuinely different budgets:

  "generate"  5s connect / 120s read, 2 retries — a board-meeting answer on
              qwen2.5:14b legitimately takes ~60s.
  "route"     2s connect / 5s read, 0 retries — the classifier is a 5-token
              call, and rag/query_router.py falls back to route="hybrid" on
              any failure. Retrying there would defeat the fast-fail design.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from llm.base import LLMProvider
from llm.errors import LLMConfigError

# profile -> (connect_timeout, read_timeout, max_retries)
_PROFILES: dict[str, tuple[float, float, int]] = {
    "generate": (config.LLM_CONNECT_TIMEOUT, config.LLM_READ_TIMEOUT, 2),
    "route":    (config.LLM_ROUTER_CONNECT_TIMEOUT, config.LLM_ROUTER_READ_TIMEOUT, 0),
}

_KNOWN = ("ollama", "gemini", "openai", "anthropic", "deepseek")


@lru_cache(maxsize=None)
def get_provider(profile: str = "generate", model: str | None = None) -> LLMProvider:
    """Build (once) the provider handle for a timeout profile.

    ``model`` overrides ``LLM_MODEL`` for this handle only — it is how
    ``rag/answer.py --model`` lets you A/B a stronger model against the
    configured one without editing .env. The provider itself (and therefore
    the API key and endpoint) always comes from ``LLM_PROVIDER``.

    Raises LLMConfigError for an unknown LLM_PROVIDER, a missing API key, or a
    provider whose SDK is not installed — never a silent fallback to a
    different model, which would corrupt the query-log audit trail.
    """
    if profile not in _PROFILES:
        raise LLMConfigError(
            f"Unknown LLM profile {profile!r}; expected one of {sorted(_PROFILES)}"
        )
    connect_timeout, read_timeout, max_retries = _PROFILES[profile]

    name  = config.LLM_PROVIDER
    model = model or config.LLM_MODEL

    if name == "ollama":
        from llm.providers.ollama import OllamaProvider

        return OllamaProvider(
            model=model,
            base_url=config.LLM_BASE_URL or config.OLLAMA_HOST,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

    common = dict(
        model=model,
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_retries=max_retries,
    )

    if name == "gemini":
        from llm.providers.gemini import GeminiProvider

        effort = (
            config.LLM_ROUTER_REASONING_EFFORT
            if profile == "route"
            else config.LLM_REASONING_EFFORT
        )
        return GeminiProvider(reasoning_effort=effort, **common)

    if name == "openai":
        from llm.providers.openai import OpenAIProvider

        return OpenAIProvider(**common)

    if name == "deepseek":
        from llm.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(**common)

    if name == "anthropic":
        from llm.providers.anthropic import AnthropicProvider

        return AnthropicProvider(**common)

    raise LLMConfigError(
        f"Unknown LLM_PROVIDER={name!r}. Supported: {', '.join(_KNOWN)}"
    )


def reset_provider_cache() -> None:
    """Drop cached handles. For tests that monkeypatch config — not hot reload."""
    get_provider.cache_clear()


__all__ = ["get_provider", "reset_provider_cache"]
