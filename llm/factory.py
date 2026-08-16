"""
Provider selection from config.

``get_provider()`` is ``lru_cache``d, which reproduces the module-singleton
behavior the old ``_get_client()`` had: one client per (profile, model) per
process. Switching providers is therefore env + restart — ``config.py`` reads
the environment once at import and these handles live for the process.

Three profiles. A profile is not just a timeout budget: it names *which config
namespace* the handle is built from, which is what lets the serving path and
the extraction pipeline run different providers in the same codebase.

  "generate"  LLM_*          5s connect / 120s read, 2 retries — a board-meeting
                             answer on qwen2.5:14b legitimately takes ~60s.
  "route"     LLM_* (router  2s connect / 5s read, 0 retries — the classifier is
              knobs)         a 5-token call, and rag/query_router.py falls back
                             to route="hybrid" on any failure. Retrying there
                             would defeat the fast-fail design.
  "extract"   PIPELINE_LLM_* 5s connect / 150s read, 5 retries — offline batch.
                             Fans out N windows x 4 extraction types per
                             meeting, so it meets cloud rate limits that a
                             single /ask never does.

The "extract" namespace falls back to OLLAMA_* and never to LLM_*, so pointing
/ask at a paid provider cannot silently move the whole corpus extraction there
too. See the config.py comment for why that asymmetry is deliberate.

Profiles are resolved inside ``get_provider`` rather than at import, so tests
that monkeypatch ``config`` and call ``reset_provider_cache()`` see the change.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from llm.base import LLMProvider
from llm.errors import LLMConfigError

_KNOWN = ("ollama", "gemini", "openai", "anthropic", "deepseek")


@dataclass(frozen=True)
class _Profile:
    """Everything needed to build one provider handle."""

    provider:         str
    model:            str
    base_url:         str
    api_key:          str
    connect_timeout:  float
    read_timeout:     float
    max_retries:      int
    reasoning_effort: str


def _resolve(profile: str) -> _Profile:
    """Read the config namespace this profile is bound to.

    Deliberately reads ``config`` attributes at call time, not import time.
    """
    if profile == "generate":
        return _Profile(
            provider=config.LLM_PROVIDER,
            model=config.LLM_MODEL,
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            connect_timeout=config.LLM_CONNECT_TIMEOUT,
            read_timeout=config.LLM_READ_TIMEOUT,
            max_retries=2,
            reasoning_effort=config.LLM_REASONING_EFFORT,
        )

    if profile == "route":
        return _Profile(
            provider=config.LLM_PROVIDER,
            model=config.LLM_MODEL,
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            connect_timeout=config.LLM_ROUTER_CONNECT_TIMEOUT,
            read_timeout=config.LLM_ROUTER_READ_TIMEOUT,
            max_retries=0,
            reasoning_effort=config.LLM_ROUTER_REASONING_EFFORT,
        )

    if profile == "extract":
        return _Profile(
            provider=config.PIPELINE_LLM_PROVIDER,
            model=config.PIPELINE_LLM_MODEL,
            base_url=config.PIPELINE_LLM_BASE_URL,
            api_key=config.PIPELINE_LLM_API_KEY,
            connect_timeout=config.PIPELINE_LLM_CONNECT_TIMEOUT,
            read_timeout=config.PIPELINE_LLM_READ_TIMEOUT,
            max_retries=config.PIPELINE_LLM_MAX_RETRIES,
            reasoning_effort=config.PIPELINE_LLM_REASONING_EFFORT,
        )

    raise LLMConfigError(
        f"Unknown LLM profile {profile!r}; expected one of "
        "['extract', 'generate', 'route']"
    )


@lru_cache(maxsize=None)
def get_provider(profile: str = "generate", model: str | None = None) -> LLMProvider:
    """Build (once) the provider handle for a profile.

    ``model`` overrides the profile's configured model for this handle only —
    it is how ``rag/answer.py --model`` lets you A/B a stronger model against
    the configured one without editing .env. The provider itself (and therefore
    the API key and endpoint) always comes from the profile's namespace.

    Raises LLMConfigError for an unknown provider, a missing API key, or a
    provider whose SDK is not installed — never a silent fallback to a
    different model, which would corrupt the query-log audit trail.
    """
    p     = _resolve(profile)
    name  = p.provider
    model = model or p.model

    if name == "ollama":
        from llm.providers.ollama import OllamaProvider

        return OllamaProvider(
            model=model,
            base_url=p.base_url or config.OLLAMA_HOST,
            connect_timeout=p.connect_timeout,
            read_timeout=p.read_timeout,
        )

    common = dict(
        model=model,
        api_key=p.api_key,
        base_url=p.base_url,
        connect_timeout=p.connect_timeout,
        read_timeout=p.read_timeout,
        max_retries=p.max_retries,
    )

    if name == "gemini":
        from llm.providers.gemini import GeminiProvider

        return GeminiProvider(reasoning_effort=p.reasoning_effort, **common)

    if name == "openai":
        from llm.providers.openai import OpenAIProvider

        return OpenAIProvider(reasoning_effort=p.reasoning_effort, **common)

    if name == "deepseek":
        from llm.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(**common)

    if name == "anthropic":
        from llm.providers.anthropic import AnthropicProvider

        return AnthropicProvider(**common)

    raise LLMConfigError(
        f"Unknown provider {name!r} for profile {profile!r}. "
        f"Supported: {', '.join(_KNOWN)}"
    )


def reset_provider_cache() -> None:
    """Drop cached handles. For tests that monkeypatch config — not hot reload."""
    get_provider.cache_clear()


__all__ = ["get_provider", "reset_provider_cache"]
