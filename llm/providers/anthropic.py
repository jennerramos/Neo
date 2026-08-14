"""
Anthropic Claude provider.

Two shape differences from every other vendor drove the interface design:

1. The system prompt is a top-level ``system=`` kwarg. A ``{"role": "system"}``
   entry inside ``messages`` is an API error — which is exactly why
   ``LLMProvider.complete`` takes ``system`` separately.
2. ``max_tokens`` is required, not optional.

Streaming is a context manager, so ``stream()`` is a generator that OWNS the
``with`` block: if the SSE consumer abandons iteration mid-answer (browser
disconnect), GeneratorExit unwinds through the ``with`` and the HTTP stream is
closed instead of leaked.

Note on the module name: this file is ``llm.providers.anthropic``, but Python 3
uses absolute imports, so ``from anthropic import Anthropic`` resolves to the
installed SDK, not to this module.
"""
from __future__ import annotations

from typing import Iterator, Optional

from llm.base import LLMResult
from llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)

try:
    import anthropic
    from anthropic import Anthropic, Timeout
except ImportError as exc:  # pragma: no cover - install-time failure
    raise LLMConfigError(
        "LLM_PROVIDER=anthropic needs the 'anthropic' package: uv sync --extra llm"
    ) from exc

# The SDK's own re-exported Timeout — see the note in llm/providers/openai.py
# about openai>=3 running on httpx2 while the rest of the tree is on httpx.


class AnthropicProvider:
    provider = "anthropic"

    def __init__(
        self,
        *,
        model:           str,
        api_key:         str,
        base_url:        str = "",
        connect_timeout: float = 5.0,
        read_timeout:    float = 120.0,
        max_retries:     int = 2,
    ) -> None:
        if not api_key:
            raise LLMConfigError(
                "LLM_PROVIDER=anthropic requires LLM_API_KEY to be set in .env"
            )
        self.model = model
        self._key = api_key
        self._client = Anthropic(
            api_key=api_key,
            base_url=base_url or None,
            timeout=Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=read_timeout,
            ),
            max_retries=max_retries,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _temp(temperature: float) -> float:
        """Anthropic accepts 0.0–1.0; Ollama and OpenAI allow up to 2.0."""
        return max(0.0, min(1.0, temperature))

    def _scrub(self, text: str) -> str:
        if self._key and self._key in text:
            text = text.replace(self._key, "***")
        return text

    def _raise(self, exc: Exception) -> None:
        msg = self._scrub(str(exc))
        if isinstance(exc, anthropic.APITimeoutError):
            raise LLMTimeout(f"anthropic timed out: {msg}") from exc
        if isinstance(exc, anthropic.AuthenticationError):
            raise LLMAuthError("anthropic rejected the API key — check LLM_API_KEY") from exc
        if isinstance(exc, anthropic.PermissionDeniedError):
            raise LLMAuthError(
                f"anthropic denied access to model '{self.model}': {msg}"
            ) from exc
        if isinstance(exc, anthropic.RateLimitError):
            raise LLMRateLimited(f"anthropic rate limit hit: {msg}") from exc
        if isinstance(exc, anthropic.APIConnectionError):
            raise LLMUnavailable(f"Cannot reach anthropic: {msg}") from exc
        if isinstance(exc, anthropic.APIStatusError):
            raise LLMUnavailable(
                f"anthropic returned HTTP {exc.status_code}: {msg}"
            ) from exc
        raise LLMError(f"anthropic call failed: {type(exc).__name__}: {msg}") from exc

    # -- contract -----------------------------------------------------------

    def complete(
        self,
        *,
        system:      str,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
        json_mode:   bool = False,
    ) -> LLMResult:
        # Anthropic has no JSON mode — it uses tool-calling or assistant
        # prefill. Neo's serving path never asks for one (only the offline
        # extractor does, and that stays on Ollama), so this is a loud no.
        if json_mode:
            raise LLMConfigError(
                "anthropic has no JSON mode; use Ollama or OpenAI for structured output"
            )
        try:
            resp = self._client.messages.create(
                model=self.model,
                system=system,          # ← top-level, not a message
                messages=messages,
                temperature=self._temp(temperature),
                max_tokens=max_tokens,  # ← required
            )
        except Exception as exc:  # noqa: BLE001
            self._raise(exc)

        # content is a list of blocks; keep only the text ones.
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        usage = resp.usage
        return LLMResult(
            text=text,
            model=self.model,
            provider=self.provider,
            prompt_tokens=getattr(usage, "input_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "output_tokens", None) if usage else None,
            finish_reason=resp.stop_reason,
        )

    def stream(
        self,
        *,
        system:      str,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
    ) -> Iterator[str]:
        # The `with` lives inside the generator on purpose: closing this
        # generator (client disconnect) unwinds through __exit__ and releases
        # the HTTP connection.
        try:
            with self._client.messages.stream(
                model=self.model,
                system=system,
                messages=messages,
                temperature=self._temp(temperature),
                max_tokens=max_tokens,
            ) as s:
                yield from s.text_stream
        except GeneratorExit:
            raise
        except Exception as exc:  # noqa: BLE001
            self._raise(exc)


__all__ = ["AnthropicProvider"]
