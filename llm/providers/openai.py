"""
OpenAI provider — and the base class for every OpenAI-wire-compatible vendor.

Gemini and DeepSeek both expose an OpenAI-shaped ``/chat/completions``
endpoint, so they subclass this with nothing but a different default base URL.
That is why adding a provider is ~10 lines rather than a new integration.

Note on the module name: this file is ``llm.providers.openai``, but Python 3
uses absolute imports, so ``from openai import OpenAI`` below resolves to the
installed SDK, not to this module.
"""
from __future__ import annotations

import logging
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
    import openai
    from openai import OpenAI, Timeout
except ImportError as exc:  # pragma: no cover - install-time failure
    raise LLMConfigError(
        "This provider needs the 'openai' package: uv sync --extra llm"
    ) from exc

# Use the SDK's own re-exported Timeout rather than importing httpx here.
# openai>=3 is built on httpx2 while the rest of the tree is on httpx 0.28, so
# a hand-rolled httpx.Timeout would be the wrong type. `openai.Timeout` always
# matches whatever the installed SDK is using.


log = logging.getLogger(__name__)


class OpenAIProvider:
    provider = "openai"

    #: Subclasses override. None means "let the SDK use its own default".
    DEFAULT_BASE_URL: Optional[str] = None

    #: Human-readable name of the env var that supplies the key, used only in
    #: error messages.
    KEY_HINT = "LLM_API_KEY"

    def __init__(
        self,
        *,
        model:            str,
        api_key:          str,
        base_url:         str = "",
        connect_timeout:  float = 5.0,
        read_timeout:     float = 120.0,
        max_retries:      int = 2,
        reasoning_effort: str = "",
    ) -> None:
        if not api_key:
            raise LLMConfigError(
                f"LLM_PROVIDER={self.provider} requires {self.KEY_HINT} to be set in .env"
            )
        self.model = model
        self._key = api_key
        self._reasoning_effort = (reasoning_effort or "").strip().lower()

        # Reasoning-class models (o-series, gpt-5.x) take a different parameter
        # envelope from chat models: `max_completion_tokens` instead of
        # `max_tokens`, and only the default temperature. There is no reliable
        # way to tell which class a model id belongs to from its name — the
        # naming space keeps moving — so these start unknown and are learned
        # from the API's own 400 on the first call, then remembered for the
        # life of the handle (which get_provider caches per process).
        self._use_completion_tokens = False
        self._send_temperature      = True
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
            timeout=Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=read_timeout,
            ),
            max_retries=max_retries,
        )

    # -- helpers ------------------------------------------------------------

    def _msgs(self, system: str, messages: list[dict]) -> list[dict]:
        if not system:
            return list(messages)
        return [{"role": "system", "content": system}, *messages]

    def _extra(self) -> dict:
        """Vendor-specific kwargs merged into every request. Hook for subclasses."""
        if self._reasoning_effort:
            return {"reasoning_effort": self._reasoning_effort}
        return {}

    def _token_params(self, temperature: float, max_tokens: int) -> dict:
        """The half of the request that differs between chat and reasoning models."""
        if self._use_completion_tokens:
            params = {"max_completion_tokens": max_tokens}
        else:
            params = {"max_tokens": max_tokens}
        if self._send_temperature:
            params["temperature"] = temperature
        return params

    def _adapt(self, exc: Exception) -> bool:
        """Learn this model's parameter dialect from a 400 and retry.

        Returns True if something was changed and the call is worth repeating.
        Each branch flips at most once, so a genuinely bad request still fails
        instead of looping.
        """
        if not isinstance(exc, openai.BadRequestError):
            return False

        msg = self._scrub(str(exc))
        low = msg.lower()
        changed = False

        if "max_completion_tokens" in low and not self._use_completion_tokens:
            self._use_completion_tokens = True
            changed = True
        if (
            "temperature" in low
            and ("unsupported" in low or "does not support" in low)
            and self._send_temperature
        ):
            self._send_temperature = False
            changed = True
        if "reasoning_effort" in low and self._reasoning_effort:
            self._reasoning_effort = ""
            changed = True

        if changed:
            log.info(
                "%s/%s: adopting reasoning-model parameters "
                "(max_completion_tokens=%s, send_temperature=%s, reasoning_effort=%r)",
                self.provider, self.model, self._use_completion_tokens,
                self._send_temperature, self._reasoning_effort,
            )
        return changed

    def _create(self, *, system: str, messages: list[dict], temperature: float,
                max_tokens: int, extra: dict):
        """chat.completions.create, retrying once per learned dialect mismatch.

        At most three adaptations are possible (token param, temperature,
        reasoning_effort) and each flips only once, so this terminates.
        """
        last: Optional[Exception] = None
        for _ in range(4):
            try:
                return self._client.chat.completions.create(
                    model=self.model,
                    messages=self._msgs(system, messages),
                    **self._token_params(temperature, max_tokens),
                    **self._extra(),
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001 - mapped by the caller
                last = exc
                if not self._adapt(exc):
                    self._raise(exc)
        self._raise(last)

    def _scrub(self, text: str) -> str:
        """Never let the API key reach a log line or an HTTP error body."""
        if self._key and self._key in text:
            text = text.replace(self._key, "***")
        return text

    def _raise(self, exc: Exception) -> None:
        msg = self._scrub(str(exc))
        if isinstance(exc, openai.APITimeoutError):
            raise LLMTimeout(f"{self.provider} timed out: {msg}") from exc
        if isinstance(exc, openai.AuthenticationError):
            raise LLMAuthError(
                f"{self.provider} rejected the API key — check {self.KEY_HINT}"
            ) from exc
        if isinstance(exc, openai.PermissionDeniedError):
            raise LLMAuthError(
                f"{self.provider} denied access to model '{self.model}': {msg}"
            ) from exc
        if isinstance(exc, openai.RateLimitError):
            raise LLMRateLimited(f"{self.provider} rate limit hit: {msg}") from exc
        if isinstance(exc, openai.APIConnectionError):
            raise LLMUnavailable(f"Cannot reach {self.provider}: {msg}") from exc
        # Reasoning models 400 here instead of returning finish_reason="length"
        # when the whole budget went to thinking. It reads like a transport
        # error but it is a configuration one, and retrying cannot fix it.
        if isinstance(exc, openai.BadRequestError) and "output limit" in msg.lower():
            raise self._empty_answer_error() from exc
        if isinstance(exc, openai.APIStatusError):
            raise LLMUnavailable(
                f"{self.provider} returned HTTP {exc.status_code}: {msg}"
            ) from exc
        raise LLMError(f"{self.provider} call failed: {type(exc).__name__}: {msg}") from exc

    def _warn_truncated(self, visible_tokens: Optional[int], max_tokens: int) -> None:
        """A cut-off answer is worse than an error: it looks finished.

        The trustee sees a sentence that stops mid-word and has no way to tell
        it from the model's real conclusion. On a reasoning model the usual
        cause is not a long answer — it is thinking tokens spending the budget
        before the first visible token, so `visible` here is often tiny
        relative to max_tokens.
        """
        log.warning(
            "%s/%s answer TRUNCATED at the token limit (max_tokens=%d, "
            "visible completion tokens=%s). Raise LLM_MAX_TOKENS or lower "
            "LLM_REASONING_EFFORT.",
            self.provider, self.model, max_tokens, visible_tokens,
        )

    def _empty_answer_error(self) -> LLMError:
        """Truncation before any visible text — a real, actionable failure.

        Hits when max_tokens is too small for the model. On Gemini 2.5 the
        thinking tokens are billed against the same budget, so a 1024-token cap
        can be consumed entirely before the first answer token appears.
        """
        return LLMError(
            f"{self.provider}/{self.model} returned no text and stopped at the token "
            f"limit. Raise LLM_MAX_TOKENS (or lower the reasoning budget) and retry."
        )

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
        kw = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = self._create(
            system=system,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=kw,
        )

        choice = resp.choices[0] if resp.choices else None
        text   = (choice.message.content if choice else None) or ""
        finish = choice.finish_reason if choice else None

        if not text.strip() and finish == "length":
            raise self._empty_answer_error()

        usage = resp.usage
        if finish == "length":
            self._warn_truncated(
                getattr(usage, "completion_tokens", None) if usage else None,
                max_tokens,
            )

        return LLMResult(
            text=text,
            model=self.model,
            provider=self.provider,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            finish_reason=finish,
        )

    def stream(
        self,
        *,
        system:      str,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
    ) -> Iterator[str]:
        emitted = False
        finish: Optional[str] = None
        stream_iter = self._create(
            system=system,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra={"stream": True},
        )
        try:
            for chunk in stream_iter:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish = choice.finish_reason or finish
                token = choice.delta.content
                if token:
                    emitted = True
                    yield token
        except Exception as exc:  # noqa: BLE001
            self._raise(exc)

        # Surface silent truncation instead of handing the trustee a blank
        # answer. Mid-stream we cannot raise once tokens are already on the
        # wire without corrupting the SSE frame sequence, so a cut-off stream
        # is logged rather than raised.
        if not emitted and finish == "length":
            raise self._empty_answer_error()
        if emitted and finish == "length":
            self._warn_truncated(None, max_tokens)


__all__ = ["OpenAIProvider"]
