"""Service layer for /ask endpoint — wraps rag.answer.ask().

Two entry points:

* ``handle_ask`` — non-streaming; returns a full ``AskResponse`` after
  generation completes. Called from ``POST /ask`` (default).
* ``handle_ask_stream`` — async generator yielding SSE frames as tokens
  arrive. Called from ``POST /ask?stream=true``. Emits three frame types:

    ``data: {"type": "meta",  "route": …, "citations": [...], …}``
    ``data: {"type": "token", "text": "…"}`` (many)
    ``data: {"type": "done",  "elapsed_sec": …}``

  Also emits ``{"type": "error", "message": …}`` if the underlying pipeline
  raises before or during streaming.

Both entry points run the sync-in-nature RAG pipeline (Postgres, Qdrant,
sentence-transformers, Ollama) via ``run_in_threadpool`` so the FastAPI
event loop stays responsive under concurrent /ask traffic.
"""
from __future__ import annotations
import json
import logging
import sys
import time
from pathlib import Path
from typing import AsyncIterator

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from starlette.concurrency import run_in_threadpool

from rag.answer import ask as _ask
from api.schemas.ask import AskRequest, AskResponse, Citation
from observability.query_log import log_query

log = logging.getLogger("neo.ask")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _to_citation_dicts(raw: list) -> list[dict]:
    """Project raw citation dicts to only fields Citation knows about.

    Keeps the wire schema stable if rag.generator adds new internal keys.
    """
    allowed = set(Citation.model_fields.keys())
    return [
        {k: v for k, v in c.items() if k in allowed}
        for c in (raw or [])
        if isinstance(c, dict)
    ]


def _build_response(result: dict, elapsed: float) -> AskResponse:
    citations = [Citation(**c) for c in _to_citation_dicts(result.get("citations", []))]
    return AskResponse(
        answer=result.get("answer", ""),
        route=result.get("route", "unknown"),
        citations=citations,
        model=result.get("model", "qwen2.5:14b"),
        elapsed_sec=elapsed,
        meeting_id=result.get("meeting_id"),
        meeting_title=result.get("meeting_title"),
        meeting_date=result.get("meeting_date"),
        school_slug=result.get("school_slug"),
        school_name=result.get("school_name"),
    )


def _sse(payload: dict) -> bytes:
    """Encode one Server-Sent Events data frame."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Non-streaming path
# ---------------------------------------------------------------------------

async def handle_ask(req: AskRequest) -> AskResponse:
    t0 = time.perf_counter()

    # rag.answer.ask does sync SQL + Qdrant + rerank + Ollama work; offload
    # so the event loop keeps serving other requests.
    result = await run_in_threadpool(
        _ask,
        query=req.query,
        school_slug=req.school_slug,
        date_from=req.date_from,
        date_to=req.date_to,
        top_k=req.top_k,
        force_route=req.force_route,
    )
    elapsed = round(time.perf_counter() - t0, 2)

    response = _build_response(result, elapsed)

    # Append a structured trace line to data/query_log.jsonl. Failure here
    # is swallowed by log_query — never breaks the user's request.
    log_query(request=req, response=response, elapsed_sec=elapsed)

    return response


# ---------------------------------------------------------------------------
# Streaming (SSE) path
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _next_or_sentinel(it):
    """Called via run_in_threadpool so the sync token generator (which
    blocks on each Ollama read) doesn't stall the event loop."""
    try:
        return next(it)
    except StopIteration:
        return _SENTINEL


async def handle_ask_stream(req: AskRequest) -> AsyncIterator[bytes]:
    t0 = time.perf_counter()

    # ── Phase 1: run router + SQL + retrieval + rerank + prompt build ────────
    # rag.answer.ask(stream=True) returns quickly with a "stream" generator
    # once retrieval is done; token generation happens lazily on iteration.
    try:
        result = await run_in_threadpool(
            _ask,
            query=req.query,
            school_slug=req.school_slug,
            date_from=req.date_from,
            date_to=req.date_to,
            top_k=req.top_k,
            stream=True,
            force_route=req.force_route,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("ask pipeline failed before streaming")
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return

    # ── Phase 2: emit meta frame so the UI can render citations immediately ──
    citations_dicts = _to_citation_dicts(result.get("citations", []))
    meta = {
        "type":          "meta",
        "route":         result.get("route", "unknown"),
        "model":         result.get("model", "unknown"),
        "citations":     citations_dicts,
        "meeting_id":    result.get("meeting_id"),
        "meeting_title": result.get("meeting_title"),
        "meeting_date":  result.get("meeting_date"),
        "school_slug":   result.get("school_slug"),
        "school_name":   result.get("school_name"),
    }
    yield _sse(meta)

    # ── Phase 3: drain the token generator, yielding one SSE frame per token ─
    answer_parts: list[str] = []
    stream_gen = result.get("stream")

    if stream_gen is not None:
        iterator = iter(stream_gen)
        try:
            while True:
                token = await run_in_threadpool(_next_or_sentinel, iterator)
                if token is _SENTINEL:
                    break
                if not token:
                    continue
                answer_parts.append(token)
                yield _sse({"type": "token", "text": token})
        except Exception as exc:  # noqa: BLE001
            log.exception("ask streaming failed mid-generation")
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            # fall through to done frame + logging so the client always sees end

    # ── Phase 4: final frame + trace log ────────────────────────────────────
    elapsed = round(time.perf_counter() - t0, 2)
    yield _sse({"type": "done", "elapsed_sec": elapsed})

    try:
        response = AskResponse(
            answer="".join(answer_parts),
            route=result.get("route", "unknown"),
            citations=[Citation(**c) for c in citations_dicts],
            model=result.get("model", "qwen2.5:14b"),
            elapsed_sec=elapsed,
            meeting_id=result.get("meeting_id"),
            meeting_title=result.get("meeting_title"),
            meeting_date=result.get("meeting_date"),
            school_slug=result.get("school_slug"),
            school_name=result.get("school_name"),
        )
        log_query(request=req, response=response, elapsed_sec=elapsed)
    except Exception:  # noqa: BLE001
        # Logging must never break the response — client already got done frame.
        log.exception("query_log failed for streaming ask")
