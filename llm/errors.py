"""
Neo v2 — provider-neutral LLM error taxonomy.

Every provider module maps its SDK's exceptions into these five classes so
callers (``rag/generator.py``, ``api/services/ask_service.py``) can branch on
failure *kind* without importing any vendor SDK.

Rule: never put the API key — or a prefix of it — into an exception message.
Providers sanitize before raising.
"""
from __future__ import annotations


class LLMError(Exception):
    """Base for every provider failure."""


class LLMUnavailable(LLMError):
    """Connection refused, Ollama not running, or a provider 5xx."""


class LLMTimeout(LLMError):
    """Connect or read budget exceeded."""


class LLMRateLimited(LLMError):
    """HTTP 429. Cloud-only — local Ollama has no rate limit."""


class LLMAuthError(LLMError):
    """HTTP 401/403: missing, malformed, or revoked API key. Cloud-only."""


class LLMConfigError(LLMError):
    """Bad configuration: unknown provider, missing key, missing SDK.

    Raised at provider-construction time rather than mid-request, so a
    misconfigured deploy fails loudly at the first /ask instead of silently
    falling back to a different model.
    """


__all__ = [
    "LLMError",
    "LLMUnavailable",
    "LLMTimeout",
    "LLMRateLimited",
    "LLMAuthError",
    "LLMConfigError",
]
