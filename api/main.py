"""Neo v2 FastAPI application."""
from __future__ import annotations
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
from api.routers import schools, meetings, votes, financials, insights, ask, export

log = logging.getLogger("neo.startup")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Warm the retriever's lazy model singletons at boot.

    Without this, the first ``/ask`` request in a fresh process pays a
    ~5–15 s hit for loading fastembed dense + sparse + BGE reranker.
    That first request is exactly the one most likely to time out on the
    client (browser fetch, proxy idle-timeout). Loading here trades a
    one-time boot cost for a consistent p95.

    Each getter is lazy-guarded, so calling them from tests or from the
    pipeline still works — this is just a first-touch.
    """
    log.info("startup: warming RAG retriever models …")
    t0 = time.perf_counter()
    try:
        from rag.retriever import _get_dense_model, _get_sparse_model, _get_reranker
        _get_dense_model()
        _get_sparse_model()
        _get_reranker()
        log.info(
            "startup: RAG models ready in %.1fs", time.perf_counter() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        # Never block the app from starting because a model failed to load —
        # /ask will surface a clearer error at request time, and /health
        # stays green so a supervisor can restart cleanly.
        log.warning(
            "startup: RAG warm-up failed (%s). "
            "First /ask will pay cold-start cost.", exc,
        )
    yield
    # Nothing to tear down; model handles are GC'd with the process.


def create_app() -> FastAPI:
    app = FastAPI(
        title="Neo v2 — Board Meeting Intelligence",
        description=(
            "API for community-college board meeting intelligence. "
            "Provides structured data on meetings, votes, financials, "
            "personnel actions, and cross-college insights."
        ),
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=_lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Origins come from NEO_CORS_ORIGINS (see config.py); unset falls back to
    # the localhost dev hosts.
    log.info(
        "startup: CORS origins=%s credentials=%s",
        config.CORS_ORIGINS, config.CORS_ALLOW_CREDENTIALS,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=config.CORS_ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
        # X-Process-Time is set by the timing middleware below; without this the
        # browser can't read it on a cross-origin response.
        expose_headers=["X-Process-Time"],
    )

    # ── Request timing middleware ─────────────────────────────────────────────
    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed = round(time.perf_counter() - t0, 3)
        response.headers["X-Process-Time"] = str(elapsed)
        return response

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(schools.router)
    app.include_router(meetings.router)
    app.include_router(votes.router)
    app.include_router(financials.router)
    app.include_router(insights.router)
    app.include_router(ask.router)
    app.include_router(export.router)

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "version": "2.0.0"}

    @app.get("/", tags=["system"])
    def root():
        return {
            "name": "Neo v2 API",
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
