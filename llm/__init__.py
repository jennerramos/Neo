"""
Neo v2 — provider-neutral LLM layer for the serving path.

The only thing ``rag/`` imports:

    from llm import get_provider

    provider = get_provider("generate")
    result   = provider.complete(system=..., messages=[...],
                                 temperature=0.1, max_tokens=1024)
    print(result.text, result.model, result.provider)

Everything vendor-shaped — auth headers, base URLs, ``num_predict`` vs
``max_tokens``, response unwrapping, streaming frame decoding, error mapping —
lives under ``llm/providers/`` and is invisible above this line. Switching
provider is env + restart; adding one is a single new file.

The offline extraction pipeline (``pipeline/extractor.py``) deliberately does
NOT use this layer: it depends on Ollama's ``format="json"``, which has no
portable equivalent, and runs free on the local GPU.
"""
from llm.base import LLMProvider, LLMResult
from llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from llm.factory import get_provider, reset_provider_cache

__all__ = [
    "get_provider",
    "reset_provider_cache",
    "LLMProvider",
    "LLMResult",
    "LLMError",
    "LLMUnavailable",
    "LLMTimeout",
    "LLMRateLimited",
    "LLMAuthError",
    "LLMConfigError",
]
