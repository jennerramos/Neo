"""
Neo v2 — structured-JSON LLM call shared by the offline extractors.

``pipeline/extractor.py`` and ``pipeline/initiative_extractor.py`` each carried
their own near-identical ``_call_ollama()``. Both now call ``call_json()``
here, which runs on the ``"extract"`` provider profile (PIPELINE_LLM_*, see
config.py) instead of POSTing to Ollama directly.

Three behaviors that are NOT in the pre-refactor code, each covering a way the
old shape failed quietly once a cloud provider is in play:

1. **Truncation is loud.** A response cut off at max_tokens is invalid JSON. It
   parsed to ``[]``, which downstream is indistinguishable from "this meeting
   genuinely had no votes". Providers report the cap in ``finish_reason``, so a
   hit is logged at WARNING with the model and cap named.

2. **Auth/config failures are fatal, transient ones retry.** The old code
   returned ``[]`` for every exception. Against local Ollama that was fine — a
   wrong host is obvious immediately. Against a cloud provider a bad API key
   would produce zero rows for the entire corpus while every meeting still
   reported success. LLMAuthError and LLMConfigError therefore propagate;
   only LLMRateLimited / LLMTimeout / LLMUnavailable are retried, then give up
   to ``[]`` so one bad window can't abort a long batch.

3. **Token usage is accumulated** so a run can price itself. An Ollama run
   costs nothing and still yields the token profile that decides whether a
   cloud provider is affordable here.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from llm import get_provider
from llm.errors import (
    LLMAuthError, LLMConfigError, LLMError, LLMRateLimited, LLMTimeout,
    LLMUnavailable,
)

log = logging.getLogger(__name__)

# Providers spell "I hit max_tokens" differently; none of them is mapped into
# the error taxonomy because it is a successful response, just a clipped one.
_TRUNCATED = frozenset({"length", "max_tokens", "MAX_TOKENS", "model_length"})

_RETRYABLE = (LLMRateLimited, LLMTimeout, LLMUnavailable)

_FENCE_OPEN  = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE = re.compile(r"\s*```$")


# ---------------------------------------------------------------------------
# Run accounting
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    """Per-run LLM totals. Reset by the caller at the start of a batch."""

    calls:             int = 0
    prompt_tokens:     int = 0
    completion_tokens: int = 0
    truncations:       int = 0
    failures:          int = 0
    retries:           int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def summary(self) -> str:
        parts = [
            f"{self.calls} calls",
            f"{self.prompt_tokens:,} in",
            f"{self.completion_tokens:,} out",
            f"({self.total_tokens:,} total)",
        ]
        if self.retries:
            parts.append(f"{self.retries} retries")
        if self.truncations:
            parts.append(f"⚠ {self.truncations} TRUNCATED")
        if self.failures:
            parts.append(f"⚠ {self.failures} failed")
        return " · ".join(parts)


_usage = Usage()


def usage() -> Usage:
    """Totals accumulated since the last reset_usage()."""
    return _usage


def reset_usage() -> None:
    global _usage
    _usage = Usage()


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def describe() -> str:
    """One-line 'provider / model' for CLI banners."""
    return f"{config.PIPELINE_LLM_PROVIDER} / {config.PIPELINE_LLM_MODEL}"


def check_llm() -> tuple[bool, str]:
    """Provider-neutral preflight. Returns (ok, message).

    Replaces the old ``check_ollama()`` /api/tags probe. Construction alone
    catches the config-shaped failures (unknown provider, missing key, missing
    SDK); a one-token completion catches an unreachable endpoint or a model
    name the provider does not recognize.
    """
    try:
        provider = get_provider("extract")
    except LLMError as exc:
        return False, str(exc)

    try:
        # Probe with the configured budget, not a token or two: on a reasoning
        # model the thinking tokens come out of this same allowance, so a tiny
        # cap fails for reasons that say nothing about whether the config works.
        provider.complete(
            system="",
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=config.PIPELINE_LLM_TEMPERATURE,
            max_tokens=config.PIPELINE_LLM_MAX_TOKENS,
        )
    except (LLMAuthError, LLMConfigError) as exc:
        return False, str(exc)
    except _RETRYABLE as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - preflight must not raise
        return False, f"{type(exc).__name__}: {exc}"

    return True, describe()


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def call_json(
    *,
    system:      str,
    prompt:      str,
    unwrap_keys: tuple[str, ...],
    label:       str = "",
) -> list[dict]:
    """Ask the extract-profile provider for JSON; return a list of dicts.

    Returns ``[]`` on any recoverable failure — malformed JSON, an exhausted
    retry budget, an unexpected shape — matching the pre-refactor contract that
    callers already handle. ``unwrap_keys`` are tried in order when the model
    wraps its array in an object (``{"votes": [...]}``).

    Raises LLMAuthError / LLMConfigError: those are deploy mistakes, not data
    problems, and must stop the batch rather than empty it.
    """
    provider    = get_provider("extract")
    max_retries = config.PIPELINE_LLM_MAX_RETRIES
    tag         = f" [{label}]" if label else ""

    for attempt in range(max_retries + 1):
        try:
            result = provider.complete(
                system=system,
                messages=[{"role": "user", "content": prompt}],
                temperature=config.PIPELINE_LLM_TEMPERATURE,
                max_tokens=config.PIPELINE_LLM_MAX_TOKENS,
                json_mode=True,
            )
        except (LLMAuthError, LLMConfigError):
            raise
        except _RETRYABLE as exc:
            if attempt < max_retries:
                _usage.retries += 1
                delay = min(2 ** attempt + random.uniform(0, 1), 60)
                log.warning(
                    "%s%s — retry %d/%d in %.1fs",
                    type(exc).__name__, tag, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                continue
            _usage.failures += 1
            log.warning("%s%s — giving up after %d retries", type(exc).__name__, tag, max_retries)
            return []
        except Exception as exc:  # noqa: BLE001 - one window must not kill a batch
            _usage.failures += 1
            log.warning("LLM call failed%s: %s: %s", tag, type(exc).__name__, exc)
            return []

        _usage.calls += 1
        _usage.prompt_tokens     += result.prompt_tokens or 0
        _usage.completion_tokens += result.completion_tokens or 0

        if result.finish_reason in _TRUNCATED:
            _usage.truncations += 1
            log.warning(
                "TRUNCATED%s — %s hit the %d-token cap; the JSON is incomplete and "
                "this window will yield no rows. Raise PIPELINE_LLM_MAX_TOKENS.",
                tag, result.model, config.PIPELINE_LLM_MAX_TOKENS,
            )

        return _parse(result.text, unwrap_keys, tag)

    return []


def _parse(text: str, unwrap_keys: tuple[str, ...], tag: str) -> list[dict]:
    """Fence-strip, JSON-decode, and coerce to a list of dicts."""
    raw = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", (text or "").strip()))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _usage.failures += 1
        log.warning("Bad JSON%s: %s", tag, exc)
        return []

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in unwrap_keys:
            if isinstance(parsed.get(key), list):
                return parsed[key]
        return [parsed]
    return []


__all__ = ["call_json", "check_llm", "describe", "usage", "reset_usage", "Usage"]
