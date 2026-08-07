"""Ask router — RAG Q&A endpoint.

Two response modes, controlled by ``?stream``:

* ``stream=false`` (default) — buffered JSON body, matches ``AskResponse``.
  Suitable for eval scripts and any client that wants the full answer at once.
* ``stream=true`` — Server-Sent Events. Frontend consumes the meta + token
  + done frames incrementally. This is the path that survives long
  generation without tripping proxy idle-timeouts (the ECONNRESET class
  of failures we saw on the sync-def endpoint).
"""
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from api.services.ask_service import handle_ask, handle_ask_stream
from api.schemas.ask import AskRequest, AskResponse

router = APIRouter(prefix="/ask", tags=["ask"])


@router.post("", response_model=AskResponse, response_model_exclude_none=False)
async def ask(
    req: AskRequest,
    stream: bool = Query(
        False,
        description="If true, respond with text/event-stream (SSE). "
                    "Frames: meta, token (many), done, error.",
    ),
):
    """Submit a natural language question to Neo's RAG pipeline."""
    if stream:
        return StreamingResponse(
            handle_ask_stream(req),
            media_type="text/event-stream",
            headers={
                # Disable proxy buffering (nginx / some CDNs) so tokens flush
                # to the client the moment they arrive.
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
    return await handle_ask(req)
