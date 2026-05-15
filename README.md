# Neo v2 — Board Meeting Intelligence

Neo turns community-college board meeting videos into structured, searchable
intelligence for trustees. It ingests video, transcribes audio, extracts
votes / financials / personnel / strategic initiatives, indexes everything
for retrieval, and serves a chat + dashboard UI grounded in cited sources.

**Tracked colleges today:** Houston City, Lone Star, El Paso Community,
Central Texas, Mt. San Antonio. Adapters for Dallas / Alamo / Austin
Community are next.

---

## Stack

| Layer | Tech |
|---|---|
| Backend API | FastAPI + Pydantic v2 |
| Frontend | Next.js 14 (App Router) + Tailwind |
| Relational DB | PostgreSQL |
| Vector DB | Qdrant (HTTP, Docker) |
| Local LLM (extraction) | Ollama / `qwen2.5:14b` |
| Cloud LLM (RAG answers) | Anthropic Claude |
| ASR | WhisperX (GPU) |
| Diarization | pyannote.audio |
| Embeddings | `nomic-embed-text` |
| Reranker | `BAAI/bge-reranker-v2-m3` |
| Orchestration | Manual today; Prefect planned (Phase 13) |
| Observability | Per-query JSONL trace log + eval set (Phase 14 slim) |

---

## Architecture

The pipeline is a chain of single-purpose phases. Each one reads meetings
in a specific status, does its job, and writes them to the next status.
Re-running any phase only picks up new work — every phase is incremental
by default.

```
collector         (YouTube Data API)         → status='discovered'
caption_downloader (youtube-transcript-api,  → status='captioned' | 'needs_asr'
                   yt-dlp fallback, Webshare proxy)
asr_processor      (WhisperX + pyannote)     → status='transcribed' | 'asr_failed'
data_packager      (cleaner.py → chunks)     → status='processed'
quality_gate       (regex/heuristics)        → status='approved' | 'rejected'
extractor          (Ollama JSON extraction)  → status='extracted'
                   • votes
                   • financial_items
                   • personnel_actions
initiative_extractor (Ollama)                → adds initiative rows
indexer            (Qdrant)                  → status='indexed'
```

Once a meeting is `indexed`, the API can answer questions about it via the
`/ask` endpoint (RAG over chunks + structured SQL lookups), and the
Insights matrix shows cross-college themes.

---

## Quickstart

### Prerequisites

- Python 3.10+ (project uses [uv](https://github.com/astral-sh/uv))
- Node 18+ (for the frontend)
- PostgreSQL running (default: localhost:5432)
- Docker Desktop (just for Qdrant)
- NVIDIA GPU with CUDA 12.6+ if you want ASR (CPU works but is 10-20× slower)
- Optional but recommended: an Ollama install with `qwen2.5:14b` pulled

### One-time setup

```powershell
# Clone, then:
uv sync --extra collection --extra cleaning --extra extraction --extra vector --extra llm --extra api

# Manual installs (NOT in pyproject.toml — see Development notes)
uv pip install git+https://github.com/m-bain/whisperX.git
uv pip install --force-reinstall --no-deps "torch==2.11.0" "torchvision==0.26.0" "torchaudio==2.11.0" --index-url https://download.pytorch.org/whl/cu128
uv pip install pyannote.audio

# Set up the DB
createdb neo_v2
uv run alembic upgrade head

# Configure
cp .env.example .env
# Edit .env: DATABASE_URL, YOUTUBE_API_KEY, HUGGINGFACE_TOKEN, PROXY_LIST, etc.

# Frontend
cd frontend && npm install
```

### Run the stack

Three services, in order. See [docs](#what-runs-where) below.

```powershell
# 1. Qdrant
docker start qdrant   # or: docker run -d --name qdrant -p 6333:6333 qdrant/qdrant

# 2. Backend
uv run --no-sync uvicorn api.main:app --reload --port 8000

# 3. Frontend
cd frontend && npm run dev
```

Open <http://localhost:3000>.

> **Always use `--no-sync`** with `uv run`. Plain `uv run` re-syncs the venv to
> `pyproject.toml`, which wipes the manual WhisperX install. See
> [Development notes](#development-notes).

### Run the pipeline (ingest new meetings)

```powershell
uv run --no-sync python pipeline/collector.py
uv run --no-sync python pipeline/caption_downloader.py
uv run --no-sync python pipeline/asr_processor.py
uv run --no-sync python pipeline/data_packager.py
uv run --no-sync python pipeline/extractor.py
uv run --no-sync python pipeline/initiative_extractor.py
uv run --no-sync python pipeline/quality_gate.py
uv run --no-sync python pipeline/indexer.py
```

Each phase only processes meetings in the right status, so the whole
sequence is safe to re-run anytime — already-done work is skipped.

---

## Pipeline phases

| Phase | Module | Reads | Writes | Notes |
|---|---|---|---|---|
| 1 | `collector.py` | (new YouTube IDs) | `discovered` | Uses YouTube Data API |
| 2 | `caption_downloader.py` | `discovered` | `captioned` / `needs_asr` | Webshare proxy + Firefox cookies |
| 3 | `asr_processor.py` | `needs_asr` | `transcribed` | WhisperX large-v3 on CUDA |
| 4 | `data_packager.py` | `transcribed` / `captioned` | `processed` | Normalizes VTT/JSON → chunks |
| 5 | `quality_gate.py` | `processed` / `extracted` | `approved` / `rejected` | Filters junk |
| 6 | `extractor.py` | `processed` / `approved` | `extracted` | Ollama JSON for votes/financials/personnel |
| 6.5 | `initiative_extractor.py` | `extracted` / `indexed` (with no Initiative rows) | (adds rows) | Strategic themes |
| 7 | `indexer.py` | `approved` / `extracted` | `indexed` | Embeddings + Qdrant upsert |

Status flow has two valid orderings; both produce the same final state:

- **gate-first:** packager → quality_gate → extractor → indexer
- **extract-first:** packager → extractor → quality_gate → indexer

Pick gate-first if Ollama time matters (skips junk before extraction).

---

## What runs where

| Service | Port | Where | Required for |
|---|---|---|---|
| Postgres | 5432 | Native install or Docker | Everything |
| Qdrant | 6333 | Docker container | RAG (`/ask`) and indexer |
| Ollama | 11434 | Native install | Extraction phases + `/ask` |
| FastAPI backend | 8000 | `uvicorn` | All API + UI |
| Next.js frontend | 3000 | `npm run dev` | Browser UI |

The Next.js dev server proxies `/api/*` to the FastAPI backend (see
`frontend/next.config.mjs`), so ngrok of port 3000 alone exposes both the UI
and the API.

---

## Eval set (Phase 14, slim)

A small, curated set of trustee questions with checkable expectations.
Run it after any prompt or retrieval change to catch regressions.

```powershell
uv run --no-sync python tests/test_eval.py
uv run --no-sync python tests/test_eval.py --verbose
uv run --no-sync python tests/test_eval.py --filter ask-001
```

See `eval/README.md` for case schema and how to expand it.

The per-query trace log (`data/query_log.jsonl`) records every `/ask` call.
Audit with:

```powershell
uv run --no-sync python scripts/audit_query_log.py --tail 10
```

---

## Project layout

```
api/                   FastAPI backend (routers, services, schemas)
rag/                   RAG pipeline (router, retriever, generator, sql_context)
pipeline/              Ingestion phases (collector → … → indexer)
database/              SQLAlchemy models
frontend/              Next.js 14 App Router UI
observability/         Per-query trace log writer
eval/                  Eval set + docs
tests/                 Pytest suite + eval runner
scripts/               One-off scripts (audit_query_log, reset_status)
alembic/               DB migrations
config.py              Single source of truth for env-driven settings
```

---

## Development notes

### `uv run` vs `uv pip` vs manual installs

Three categories of dependency:

1. **In `pyproject.toml`** — `fastapi`, `qdrant-client`, `transformers`, etc.
   Installed by `uv sync --extra <group>`. Safe to `uv sync` anytime.

2. **Manual GitHub installs** — `whisperx` only. Not on PyPI. Wiped by `uv sync`.

3. **CUDA-pinned wheels** — `torch`, `torchvision`, `torchaudio`. Pinned via
   `[tool.uv.sources]` in `pyproject.toml` to the cu128 wheel index. Should
   survive `uv sync` now; if not, reinstall with
   `uv pip install --force-reinstall --no-deps "torch==2.11.0+cu128" ...`.

**Always use `uv run --no-sync`** — defensive against future `uv sync` surprises
that might happen if a new extras group gets pulled in.

### Knowledge graph (graphify)

The repo can be navigated as a graph:

```powershell
graphify ./api ./rag ./pipeline ./frontend/src   # initial build
```

Produces `graphify-out/GRAPH_REPORT.md` with god nodes, surprising
cross-file connections, and a queryable graph. Useful for cross-module
"how does X relate to Y" questions:

```
graphify query "what connects build_sql_context to the matrix UI?"
graphify path "VoteRow" "Vote ORM"
graphify explain "WebshareProxyConfig"
```

`graphify-out/` is gitignored — rebuild on each clone.

### Common gotchas

- **CUDA not detected after a reinstall** — torch got replaced with CPU build.
  Reinstall from cu128 wheel index (see Quickstart).
- **Pipeline finds 0 meetings** — check status with
  `psql -c "SELECT status, COUNT(*) FROM meetings GROUP BY status;"`. Phases
  only pick up specific statuses by design.
- **`/ask` returns 500 with `PreTrainedModel` not found** — torch/transformers
  ABI mismatch. Same fix as the CUDA one.
- **Webshare proxy returns 400** — likely a username format issue. Run a
  curl direct-test through the proxy; if curl works but the script doesn't,
  the special-case Webshare wrapper in `pipeline/caption_downloader.py`
  handles it.

---

## License

(Specify a license — MIT / Apache-2.0 / proprietary — before publishing publicly.)
