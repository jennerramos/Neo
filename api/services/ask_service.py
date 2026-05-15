"""Service layer for /ask endpoint — wraps rag.answer.ask()."""
from __future__ import annotations
import time
import sys
from pathlib import Path
from typing import Iterator

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from rag.answer import ask as _ask
from api.schemas.ask import AskRequest, AskResponse, Citation
from observability.query_log import log_query


def handle_ask(req: AskRequest) -> AskResponse:
    t0 = time.perf_counter()
    result = _ask(
        query=req.query,
        school_slug=req.school_slug,
        date_from=req.date_from,
        date_to=req.date_to,
        top_k=req.top_k,
        force_route=req.force_route,
    )
    elapsed = round(time.perf_counter() - t0, 2)

    citations = [
        Citation(**{k: v for k, v in c.items() if k in Citation.model_fields})
        for c in result.get("citations", [])
        if isinstance(c, dict)
    ]

    response = AskResponse(
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

    # Append a structured trace line to data/query_log.jsonl. Failure here
    # is swallowed by log_query — never breaks the user's request.
    log_query(request=req, response=response, elapsed_sec=elapsed)

    return response
