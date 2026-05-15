"""
Lightweight per-query log writer.

This is the *minimum* observability we want before iterating on prompts and
retrieval: one structured JSON line per /ask call recording inputs, the
route taken, citations, latency, and answer length.

Why JSONL and not Phoenix/OTel yet?
  - Zero infra: just a file under data/.
  - Same data shape as OTel attributes — when we adopt Phoenix later, swap
    `log_query()` for an OTel span exporter; callers don't change.
  - Greppable / pandas-loadable — fine for the iteration phase.

Usage (in api.services.ask_service):

    from observability.query_log import log_query
    ...
    log_query(request=req, response=ask_response, elapsed_sec=elapsed)

Disable by setting NEO_QUERY_LOG=off in the env. Failures are swallowed —
we never want logging to break a user's query.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Used to count inline [N] citation markers — a quality signal we audit
# after prompt changes. Matches "[3]" and the inside of "[3, 7]" but only
# the outermost brackets; we don't try to split combined groups here, just
# count the marker occurrences.
_MARKER_RE = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")

# Where to write. Override with NEO_QUERY_LOG_PATH for tests.
_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "query_log.jsonl"
_LOG_PATH     = Path(os.getenv("NEO_QUERY_LOG_PATH", str(_DEFAULT_PATH)))

# Single mutex protects the append. JSONL append is atomic per line on POSIX
# but Windows file handles are stricter; the lock keeps us safe across both.
_LOCK = threading.Lock()


def _enabled() -> bool:
    return os.getenv("NEO_QUERY_LOG", "on").lower() not in ("off", "0", "false", "no")


def _serialize_citations(citations: Any) -> list[dict[str, Any]]:
    """
    Pull just the fields useful for grepping / quality analysis. Preserves
    enough to answer "did the right meeting show up in retrieval?" without
    bloating the log file.
    """
    out: list[dict[str, Any]] = []
    for c in citations or []:
        if hasattr(c, "model_dump"):
            d = c.model_dump()
        elif isinstance(c, dict):
            d = c
        else:
            continue
        out.append({
            "type":       d.get("type"),
            "meeting_id": d.get("meeting_id"),
            "chunk_id":   d.get("chunk_id"),
            "score":      d.get("score"),
            "title":      d.get("title") or d.get("label"),
        })
    return out


def log_query(*, request: Any, response: Any, elapsed_sec: float) -> None:
    """
    Append one structured record describing this /ask call.

    `request` and `response` are the AskRequest / AskResponse pydantic
    models, but we only read attributes we know exist — so any duck-typed
    object also works (handy for tests).
    """
    if not _enabled():
        return

    try:
        answer = getattr(response, "answer", "") or ""
        n_markers = len(_MARKER_RE.findall(answer))
        record = {
            "ts":            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query":         getattr(request, "query", ""),
            "school_slug":   getattr(request, "school_slug", None),
            "date_from":     getattr(request, "date_from", None),
            "date_to":       getattr(request, "date_to", None),
            "force_route":   getattr(request, "force_route", None),

            "route":         getattr(response, "route", None),
            "model":         getattr(response, "model", None),
            "elapsed_sec":   round(float(elapsed_sec), 3),
            "answer_chars":  len(answer),
            "answer_preview": answer[:240],   # enough to skim, not full bloat
            # Audit signal — how many [N] citation markers the LLM produced.
            # 0 means the prompt is being ignored; non-zero is a healthy sign.
            "n_markers":     n_markers,

            # Latest-meeting / RAG context resolved by the backend.
            "meeting_id":    getattr(response, "meeting_id", None),
            "meeting_date":  getattr(response, "meeting_date", None),

            "citations":     _serialize_citations(getattr(response, "citations", None)),
            "n_citations":   len(getattr(response, "citations", []) or []),
        }

        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        # Logging must never break the request. Print to stderr for dev
        # awareness but otherwise stay silent.
        print(f"[query_log] write failed: {e!r}", file=sys.stderr)


def log_path() -> Path:
    """Where the JSONL is being written (handy for tests + scripts)."""
    return _LOG_PATH
