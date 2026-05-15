"""Neo v2 FastAPI application."""
from __future__ import annotations
import sys
import time
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routers import schools, meetings, votes, financials, insights, ask, export


def create_app() -> FastAPI:
    app = FastAPI(
        title="Neo v2 — Board Meeting Intelligence",
        description=(
            "API for HCC trustee board meeting intelligence. "
            "Provides structured data on meetings, votes, financials, "
            "personnel actions, and cross-college insights."
        ),
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",   # Next.js dev
            "http://localhost:3001",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
