"""
Ollama provider — wraps today's exact behavior.

Same ``ollama.Client``, same host, same ``httpx.Timeout``, same
``options={"temperature": ..., "num_predict": ...}`` envelope. The request
that reaches Ollama is byte-identical to the one the pre-refactor
``rag/generator.py`` sent; only the Python call stack differs.
"""
from __future__ import annotations

from typing import Iterator, Optional

import httpx

from llm.base import LLMResult
from llm.errors import LLMConfigError, LLMTimeout, LLMUnavailable

try:
    from ollama import Client as _Client
except ImportError as exc:  # pragma: no cover - install-time failure
    raise LLMConfigError(
        "LLM_PROVIDER=ollama needs the 'ollama' package: uv sync --extra llm"
    ) from exc


class OllamaProvider:
    provider = "ollama"

    def __init__(
        self,
        *,
        model:           str,
        base_url:        str,
        connect_timeout: float,
        read_timeout:    float,
    ) -> None:
        self.model = model
        self._client = _Client(
            host=base_url,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=read_timeout,
            ),
        )

    # -- helpers ------------------------------------------------------------

    def _msgs(self, system: str, messages: list[dict]) -> list[dict]:
        """Ollama takes the system prompt as the first message."""
        if not system:
            return list(messages)
        return [{"role": "system", "content": system}, *messages]

    @staticmethod
    def _opts(temperature: float, max_tokens: int) -> dict:
        # num_predict is Ollama's name for max output tokens.
        return {"temperature": temperature, "num_predict": max_tokens}

    @staticmethod
    def _raise(exc: Exception) -> None:
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
            raise LLMTimeout(f"Ollama timed out: {exc}") from exc
        if isinstance(exc, httpx.TimeoutException):
            raise LLMTimeout(f"Ollama timed out: {exc}") from exc
        if isinstance(exc, httpx.ConnectError):
            raise LLMUnavailable(
                f"Cannot reach Ollama — is it running? ({exc})"
            ) from exc
        raise LLMUnavailable(f"Ollama call failed: {type(exc).__name__}: {exc}") from exc

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
        kw = {"format": "json"} if json_mode else {}
        try:
            resp = self._client.chat(
                model=self.model,
                messages=self._msgs(system, messages),
                stream=False,
                options=self._opts(temperature, max_tokens),
                **kw,
            )
        except Exception as exc:  # noqa: BLE001 - mapped into the taxonomy
            self._raise(exc)

        return LLMResult(
            text=resp["message"]["content"],
            model=self.model,
            provider=self.provider,
            prompt_tokens=_get(resp, "prompt_eval_count"),
            completion_tokens=_get(resp, "eval_count"),
            finish_reason=_get(resp, "done_reason"),
        )

    def stream(
        self,
        *,
        system:      str,
        messages:    list[dict],
        temperature: float,
        max_tokens:  int,
    ) -> Iterator[str]:
        try:
            for chunk in self._client.chat(
                model=self.model,
                messages=self._msgs(system, messages),
                stream=True,
                options=self._opts(temperature, max_tokens),
            ):
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token
        except Exception as exc:  # noqa: BLE001
            self._raise(exc)


def _get(resp, key: str) -> Optional[int]:
    """Read a top-level field from either a dict or a pydantic ChatResponse."""
    try:
        return resp.get(key)
    except AttributeError:
        return getattr(resp, key, None)


__all__ = ["OllamaProvider"]
