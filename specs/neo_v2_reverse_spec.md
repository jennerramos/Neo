# Neo v2 — Reverse-Engineered Specification

> **Method note.** This specification was reverse-engineered from source using the `spec-miner` skill on 2026-08-04. All requirements are grounded in code evidence, with file paths and line-level anchors. Where implementation and prior docs (README, refactor_candidates) agree, this document consolidates them; where they diverge, the code is treated as authoritative and the divergence is called out under "Uncertainties".

---

## 1. Overview

**Neo v2** is a full-stack pipeline that turns community-college board-meeting videos into structured, cited intelligence for trustees. It ingests platform recordings, transcribes audio when captions are unavailable, extracts votes / financial items / personnel actions / initiatives via LLM, indexes chunks for retrieval, and serves a chat + dashboard UI.

- **Repo root:** `Neo_v2/`
- **Tracked colleges (verified from `database/seed.py`):** 8 total across 3 adapter platforms — YouTube: Houston City, Lone Star, El Paso Community, Central Texas, Mt. San Antonio · Panopto: Austin Community, Alamo Colleges · Ravnur: Dallas College.
- **Deployment target:** operator-run on a single Windows/Linux workstation; three long-lived services (Qdrant, FastAPI backend, Next.js frontend) plus manually invoked pipeline scripts.

Evidence: `README.md`, `pipeline/*.py`, `api/main.py`, `frontend/src/`.

---

## 2. Technology Stack (observed)

| Layer | Tech | Evidence |
|---|---|---|
| Backend API | FastAPI, Pydantic v2 | `api/main.py:10`, `api/schemas/*` `model_config = {...}` |
| Frontend | Next.js 14 App Router, TypeScript, Tailwind | `frontend/src/app/`, `frontend/src/lib/api.ts` |
| Relational DB | PostgreSQL (JSONB, ARRAY, `Numeric(15,2)`) | `database/models.py:19` |
| Vector DB | Qdrant (server or embedded local mode) | `rag/retriever.py:86-93`, `config.py:98-112` |
| LLM (extraction) | Ollama, `qwen2.5:14b` default | `config.py:78-81`, `pipeline/extractor.py` |
| LLM (RAG generator) | Ollama `qwen2.5:14b` (README also mentions Claude — Claude key exists in config but generator invokes Ollama) | `rag/generator.py:214-255`, `config.py:87-88` |
| Dense embeddings | `nomic-ai/nomic-embed-text-v1.5` (768-dim) via `fastembed` | `rag/retriever.py:57-62` |
| Sparse embeddings | `Qdrant/bm25` via `fastembed` | `rag/retriever.py:65-70` |
| Reranker | `BAAI/bge-reranker-v2-m3` (cross-encoder, `max_length=512`) | `config.py:120`, `rag/retriever.py:73-82` |
| ASR | WhisperX large-v3 with pyannote diarization | `pipeline/asr_processor.py` |
| Migrations | Alembic | `alembic.ini`, `database/migrations/` |
| Dependency mgmt | `uv` (with manual WhisperX + CUDA-pinned torch wheels) | `pyproject.toml`, `README.md:214-229` |
| Observability | JSONL trace log (`data/query_log.jsonl`) + eval harness | `observability/query_log.py`, `tests/test_eval.py` |

---

## 3. High-Level Architecture

Two loosely coupled halves that meet at PostgreSQL + Qdrant:

```
┌──────────────────── Ingestion (batch, operator-run) ─────────────────────┐
│                                                                          │
│  channel_validator ─── (verifies YouTube channels)                       │
│                                                                          │
│  collector ── DiscoveredMeeting ──►  meetings table (status='discovered')│
│    │                                                                      │
│    └──► pipeline.sources.for_school()  ── YouTube / Panopto / Ravnur     │
│                                                                          │
│  caption_downloader  → captioned | needs_asr | caption_unavailable       │
│  asr_processor       → transcribed | asr_failed                          │
│  data_packager       → processed | processing_failed  (writes Chunks)   │
│  ┌── quality_gate   → approved | rejected                                │
│  │   extractor       → extracted (votes, financial_items, personnel)     │
│  └── initiative_extractor → adds Initiative rows                         │
│  indexer             → indexed  (upserts dense+sparse vectors to Qdrant) │
│  pattern_builder     → aggregates initiatives into pattern_signals       │
└──────────────────────────────────────────────────────────────────────────┘
                          │                                    │
                          ▼                                    ▼
                    Postgres (facts)                   Qdrant (chunks)
                          │                                    │
┌──────────────── Query surface (long-running) ────────────────────────────┐
│                                                                          │
│  Next.js UI  →  /api rewrite  →  FastAPI                                 │
│                                                                          │
│  /schools /meetings /votes /financials /insights /export  (structured)   │
│  POST /ask  →  rag.answer:                                               │
│      query_router  ──(pattern / off-topic / LLM fallback)                │
│           │                                                              │
│           ├─► build_sql_context (Postgres) — needs_review=FALSE only     │
│           ├─► retriever.retrieve (Qdrant hybrid + BGE rerank)            │
│           └─► generator.generate (Ollama, few-shot [N] citation format)  │
│                                                                          │
│  observability.log_query → data/query_log.jsonl                          │
└──────────────────────────────────────────────────────────────────────────┘
```

Evidence:
- Ingestion phases: `pipeline/*.py`, coordinated via `meetings.status` string.
- RAG orchestrator: `rag/answer.py:48-376`.
- FastAPI wiring: `api/main.py:17-77`.
- Frontend proxy: `frontend/next.config.mjs` (via README).

---

## 4. Module & Directory Layout

Observed from repo root:

| Path | Purpose |
|---|---|
| `api/` | FastAPI backend — `main.py` factory + 7 routers + services + Pydantic schemas + raw-SQL query layer |
| `rag/` | Query-side pipeline — `answer.py` (orchestrator), `query_router.py`, `sql_context.py`, `retriever.py`, `generator.py`, `meeting_lookup.py`, `verify_retrieval.py` |
| `pipeline/` | Ingestion phases 1–8 + candidate finder + cleaner + adapters (`sources/`) + `states.py` (state-machine single source of truth) |
| `pipeline/sources/` | Caption-source adapters: `base.py` (Protocol), `youtube.py`, `panopto.py`, `ravnur.py`, `__init__.py` (registry) |
| `database/` | SQLAlchemy `Base` + 11 ORM models + `seed.py` + Alembic `migrations/` |
| `frontend/src/` | Next.js 14 App Router (`app/`), `components/`, `lib/api.ts` typed client, `types/index.ts` |
| `observability/` | `query_log.py` — JSONL append-only per-`/ask` trace |
| `eval/` | Trustee-question eval set + README |
| `tests/` | Pytest suite + `test_eval.py` runner |
| `scripts/` | Ops utilities — `reset_status.py`, `recover_failed.py`, `audit_query_log.py`, seed/status helpers |
| `data/` | Runtime artifacts (raw VTT, processed transcripts, `query_log.jsonl`); layout defined in `config.py:23-31` |
| `config.py` | Sole `os.getenv` reader; every module imports settings from here |
| `graphify-out/` | Knowledge graph over the codebase (gitignored; rebuild per clone) |

---

## 5. Data Model (observed requirements)

Source: `database/models.py`. Eleven tables, all with `created_at` DEFAULT `now()`.

### 5.1 Core

- **schools** — `school_id` PK, `slug` UNIQUE, `default_source_type` NOT NULL DEFAULT `youtube_caption` (adapter dispatch key), legacy `source_type` retained for compat, `discovery_config` JSONB (free-form per-platform config), `is_active` BOOL, soft-delete via `deleted_at`.
- **channels** — one row per YouTube channel of a school; `youtube_channel_id` UNIQUE; `verified` BOOL (set by Phase 1).
- **meetings** — one row per recording; `video_id` UNIQUE; `status` TEXT (state-machine column, see §7); soft-delete via `is_active` + `deleted_at`; provenance paths (`raw_vtt_path`, `clean_txt_path`, `meeting_json_path`, `chunks_jsonl_path`); quality fields (`word_count`, `speaker_count`, `quality_score`, `rejection_reason`); reproducibility (`file_hash`, `processing_version`).
- **pipeline_runs** — one row per phase execution per meeting or per channel (nullable both FKs); `phase`, `status`, `error_message`, `duration_seconds`, `retry_count`, `gpu_time_seconds`, `memory_mb`. **Write-only god node**: nine phases write, no code reads (refactor_candidates cross-cutting finding).
- **chunks** — `chunk_id` UUID text PK; joins meeting + school; carries `chunk_index`, `text`, `speaker`, `start_time`, `end_time`, `token_count`, `quality_score`, `topic_label`, `qdrant_indexed` BOOL.

### 5.2 Phase-6 extraction (share provenance shape)

`votes`, `financial_items`, `personnel_actions` each carry the same provenance columns:

- `chunk_ids` `ARRAY(TEXT)`, `evidence_text`, `source_type`, `start_time_sec`, `end_time_sec`
- Audit: `extractor_version` (`v2.0` default), `confidence` FLOAT, `needs_review` BOOL DEFAULT TRUE, `reviewed_at/by`, `review_notes`
- Denormalized `school_slug`, `video_id`, `school_id`, `meeting_id` — kept redundantly so grep/CSV export don't need joins.

### 5.3 Phase-6.5 extraction (initiatives + patterns)

- **initiatives** — same provenance shape; adds the *four evidence levels*: `observed_action`, `stated_rationale`, `claimed_outcome`, `measured_outcome`. Extractor comment forbids conflation across levels (`pipeline/initiative_extractor.py:7-14`). Default `extractor_version = 'v2.5'`.
- **pattern_signals** — aggregated (not per-meeting) — cross-school signal with `school_count`, `meeting_count`, `first/last_observed_date`, `supporting_initiative_ids` `ARRAY(INTEGER)`.

### 5.4 Query telemetry

- **query_logs** — DB table for query telemetry. Notably, the *live* logging path (`observability/query_log.py`) writes to a **JSONL file**, not this table. `query_logs` looks unused by current write code — see Uncertainties §11.

---

## 6. Configuration Contract

`config.py` is the sole `os.getenv` reader. Contract:

- **REQ-CFG-1** — The system shall read `DATABASE_URL` from the environment at import time; if unset, importing `config` raises `KeyError`. (`config.py:37`)
- **REQ-CFG-2** — All secrets (`YOUTUBE_API_KEY`, `HUGGINGFACE_TOKEN`, `ANTHROPIC_API_KEY`, `PROXY_LIST`) default to empty string; downstream code is responsible for feature-gating.
- **REQ-CFG-3** — Where `QDRANT_LOCAL_PATH` is set, the retriever shall use the embedded `QdrantClient(path=...)` and shall not contact `QDRANT_HOST:QDRANT_PORT`. (`rag/retriever.py:89-92`)
- **REQ-CFG-4** — On `import config`, `ensure_dirs()` shall create `RAW_DIR`, `PROCESSED_DIR`, `AUDIO_DIR`, `EXPORTS_DIR` if missing. (`config.py:155-161`)
- **REQ-CFG-5** — Retrieval defaults: `RETRIEVAL_TOP_K=20` candidates, `RERANK_TOP_K=8` after cross-encoder. Quality-gate defaults: `MIN_WORD_COUNT=500`, `MIN_QUALITY_SCORE=0.6`, `MIN_CHUNK_QUALITY_SCORE=0.6`.

---

## 7. Pipeline State Machine

Authoritative source: `pipeline/states.py` (single-source-of-truth refactor per refactor_candidates.md #3, resolved 2026-05-09). Every phase imports its eligibility tuple from `INPUTS`/`RECHECK_INPUTS`; scripts derive `ALL_STATUSES` and `RECOVERY_TARGETS` from the same module.

### 7.1 States (observed)

Forward: `discovered → captioned | needs_asr → (downloading | transcribing) → transcribed → processed → extracted | approved → indexed`

Terminal / semi-terminal:
- `rejected` — re-runnable via `quality_gate --recheck`
- `asr_failed`, `processing_failed`, `failed` — recovered by `scripts/recover_failed.py` per `RECOVERY_TARGETS`
- `caption_unavailable` — **truly terminal** (platform has no captions AND no audio path); *not* in `RECOVERY_TARGETS` by design (`pipeline/states.py:54-57, 128-145`)
- `pending` — legacy, kept for backward compat

### 7.2 Diamond ordering

`quality_gate` and `extractor` are order-independent:

- **gate-first:** `packager → quality_gate → extractor → indexer` (meeting ends at `extracted`)
- **extract-first:** `packager → extractor → quality_gate → indexer` (meeting ends at `approved`)

The indexer accepts either terminal (`INPUTS["indexer"] = (APPROVED, EXTRACTED)`). Evidence: `pipeline/states.py:86-94`; documented in `database/models.py:108-121`.

### 7.3 Phase eligibility (from `INPUTS`)

| Phase | Default inputs | Extra when `--recheck/--reextract/--reindex` |
|---|---|---|
| caption_downloader | `discovered` | — |
| asr_processor | `needs_asr` | — |
| data_packager | `transcribed`, `captioned` | `processed`, `rejected` |
| quality_gate | `processed`, `extracted` | `rejected` |
| extractor | `processed`, `approved` | `extracted`, `indexed` |
| indexer | `approved`, `extracted` | `indexed` |
| initiative_extractor | `extracted`, `indexed` | — |

### 7.4 Requirements (EARS)

- **REQ-PIPE-1 (Ubiquitous)** — Every phase shall read `meetings.status` to select input rows and shall update it on success or failure. No phase-to-phase Python imports coordinate the pipeline.
- **REQ-PIPE-2 (Event-driven)** — When a phase completes, the phase shall append a `PipelineRun` row containing `phase`, `status`, `duration_seconds`, and, where applicable, `error_message` / `gpu_time_seconds` / `memory_mb`.
- **REQ-PIPE-3 (Event-driven)** — When the caption downloader receives a 200-OK response whose body is not a WEBVTT payload, it shall not write the file and shall fall through to the failure/needs-ASR path. (`pipeline/caption_downloader.py:58-68`)
- **REQ-PIPE-4 (State-driven)** — While a meeting is in a terminal-failure state (`asr_failed`, `processing_failed`, `rejected`), the default pipeline shall not pick it up; only `scripts/recover_failed.py` or the phase's `--recheck` flag shall re-enter it into the forward path.
- **REQ-PIPE-5 (Optional)** — Where `School.default_source_type` names a registered adapter, `pipeline.sources.for_school` shall dispatch to that adapter; where unregistered or missing, it shall fall back to the legacy `School.source_type` then to `"youtube_caption"`. (`pipeline/sources/__init__.py:36-50`)

---

## 8. Caption-Source Adapter Contract

`pipeline/sources/base.py` defines a `@runtime_checkable` `CaptionSourceAdapter` Protocol. Every adapter provides:

```python
source_type: str
def discover_meetings(school: School) -> Iterable[DiscoveredMeeting]: ...
def fetch_captions(meeting: Meeting) -> FetchResult: ...
```

- Adapters return **bytes** (VTT); the orchestrator owns disk writes, SHA-256 hashing, and `PipelineRun` logging (`pipeline/caption_downloader.py` docstring).
- `FetchResult.reason` taxonomy is a closed vocabulary: `fetched`, `no_captions`, `private`, `rate_limited`, `error:<details>`. `no_captions`/`private` are terminal; `rate_limited`/`error:*` are retryable. (`pipeline/sources/base.py:47-64`)
- Registered adapters (per `pipeline/sources/__init__.py:24-28`): `youtube_caption`, plus Panopto and Ravnur (via their modules' `adapter` singleton). Class-level: `PanoptoAdapter` is re-exported for tests.
- Platform quirks captured in source: Panopto `SessionStartTime` is Windows FILETIME (`FILETIME_TO_UNIX_OFFSET_SECONDS = 11_644_473_600`, `pipeline/sources/panopto.py:24-25`). Both non-YouTube adapters share `DATE_CUTOFF = 2023-04-08` and 30 s timeout with `(2, 5, 15)` s backoff.

Requirements (EARS):

- **REQ-ADAPTER-1** — Every registered adapter shall satisfy the `CaptionSourceAdapter` Protocol at import time (enforced by `runtime_checkable`).
- **REQ-ADAPTER-2** — Where an adapter cannot fetch captions and cannot supply audio for ASR, the orchestrator shall mark the meeting `caption_unavailable`; no automatic retry shall be attempted. (Reason taxonomy in `base.py:47-64`, terminal set in `states.py:70-72`.)

---

## 9. API Surface

Base URL contract: in-browser calls use `/api` (proxied by Next.js rewrite); server-side rendering hits `NEO_API_TARGET` (default `http://localhost:8000`). (`frontend/src/lib/api.ts:35-38`)

CORS: origins come from `NEO_CORS_ORIGINS` (comma-separated, trailing slashes stripped), falling back to `http://localhost:3000`, `http://localhost:3001`, `http://127.0.0.1:3000` when unset. `allow_credentials` is true unless the origin list is `*`, in which case it downgrades to false (a wildcard origin plus credentials is rejected by browsers). `X-Process-Time` is in `expose_headers`. (`config.py:CORS_ORIGINS`, `api/main.py:71-90`) — *updated by P0-3, was a hard-coded localhost list.*

Every response carries `X-Process-Time` (seconds, 3-decimal) via middleware. (`api/main.py:44-50`)

### 9.1 Endpoint inventory

| Method | Path | Response model | Notes |
|---|---|---|---|
| GET | `/health` | inline dict | `{"status":"ok","version":"2.0.0"}` |
| GET | `/` | inline dict | root pointer to `/docs` and `/health` |
| GET | `/docs`, `/redoc` | FastAPI-generated | |
| GET | `/schools` | `list[School]` | active schools only, ordered by name |
| GET | `/meetings` | `MeetingListResponse` | filters: `school`, `date_from`, `date_to`, `status`, `limit≤200`, `offset` |
| GET | `/meetings/{id}` | `MeetingOverview` | 404 if missing; nests VoteSummary/FinancialSummary/PersonnelSummary/TranscriptChunk |
| GET | `/meetings/{id}/transcript` | `MeetingTranscript` | full ordered chunks |
| GET | `/votes` | `VoteListResponse` | filters: `school`, `meeting_id`, `passed`, `date_from/to`, `limit≤500`, `offset` |
| GET | `/votes/summary` | `VotesStats` | `total`, `passed`, `failed`, `unanimous`, `pass_rate`, `unanimous_rate`, `top_movers` |
| GET | `/financials` | `FinancialListResponse` | filters incl. `action_type`, `category`, `amount_min/max` |
| GET | `/financials/summary` | `FinancialsStats` | `by_action_type[]`, `top_vendors[]`, `largest_item` |
| GET | `/insights/matrix` | `InsightMatrix` | rows=themes T1–T7, cols=schools, cell=list of up to 3 `InsightCell` |
| GET | `/insights/detail/{insight_id}` | `InsightDetail` | includes supporting_meetings, evidence chunks, peer cells |
| POST | `/ask` | `AskResponse` | body `AskRequest`; delegates to `rag.answer.ask` |
| GET | `/export/votes.csv` | streaming CSV | filters mirror `/votes`; `limit=5000` |
| GET | `/export/financials.csv` | streaming CSV | filters mirror `/financials`; `limit=5000` |

### 9.2 EARS requirements

- **REQ-API-1 (Event-driven)** — When a request completes, the API shall attach an `X-Process-Time` response header set to the round-trip seconds. (`api/main.py:44-50`)
- **REQ-API-2 (Ubiquitous)** — The `/ask` endpoint shall persist a JSONL trace record to `data/query_log.jsonl` after every request. Logging failures shall not fail the response. (`api/services/ask_service.py:47-49`, `observability/query_log.py:120-123`)
- **REQ-API-3 (Ubiquitous)** — Structured list endpoints (`/votes`, `/financials`, `/meetings`) shall filter by school + date range using the shared `_filters.py` helper (refactor_candidates #1 partial fix).
- **REQ-API-4 (Ubiquitous)** — `AskRequest.query` shall be validated to `3 ≤ len ≤ 1000`. `force_route` shall match `^(sql|rag|hybrid|compare)$` when present. (`api/schemas/ask.py:8-13`)
- **REQ-API-5 (Optional)** — Where `NEO_QUERY_LOG=off`, the query log writer shall no-op. (`observability/query_log.py:49-50`)

---

## 10. RAG Query Pipeline

### 10.1 Orchestrator (`rag/answer.py:ask`)

Six routes; branching logic centralised in `answer.py`:

| Route | Trigger | Behavior |
|---|---|---|
| `none` | Off-topic hard pattern OR router LLM says NONE | Returns canned assistant intro; no DB/Qdrant hit |
| `sql` | Structured facts pattern (votes/financial/personnel) | Only `build_sql_context` called |
| `rag` | Narrative / rationale pattern | Only `retriever.retrieve` called |
| `hybrid` | Mixed OR router default fallback | Both SQL + RAG contexts |
| `compare` | Cross-school comparison pattern | Fetches SQL per mentioned school (or combined); RAG unfiltered by school |
| `latest_meeting` | "last/latest/most recent … meeting(s)" without a count guard | Resolves to a single meeting via `meeting_lookup.get_latest_meeting`; retrieval & SQL scoped to that meeting |

`school_slug` auto-detection: when caller omits `school_slug` and exactly one school is mentioned in the query text, that school becomes the effective filter. Multi-mention queries route to `compare` first, so this fallback never fires for them. (`rag/answer.py:299-311`)

### 10.2 Query Router (`rag/query_router.py`)

Two-pass:

1. Fast regex pattern router (submillisecond). Priority: off-topic → compare → latest_meeting → hybrid → sql-only → rag-only → mixed → ambiguous.
2. Ambiguous → Ollama classifier (`_CLASSIFY_PROMPT`, `num_predict=5`, temperature=0). On any exception the router defaults to `hybrid`. (`query_router.py:407-431`)

School slug detection: word-boundary regex over 5 seeded aliases; slugs sorted DESC by pattern length so `houston community college` matches before bare `hcc`.

Off-topic banks are narrow by design — comment says "when in doubt, let RAG try — if there's nothing in the transcripts the LLM will say so". (`query_router.py:54-70`)

### 10.3 SQL Context (`rag/sql_context.py`)

- Table-driven — one `_TABLE_SPECS` dict drives the same JOIN+filter+order query for all 4 extraction tables. Adding a fifth is a one-entry change (refactor_candidates #1).
- **Trust filter:** the RAG path applies `needs_review = FALSE`; the browsable API list endpoints do not. This is an intentional asymmetry (cross-cutting finding in refactor_candidates).
- `financial_items` additionally excludes `action_type = 'discussed'` (speculative chatter).
- Default `limit=100` per table; ordered by `published_date DESC`.
- Dedup: passing both `"financial_actions"` and `"financial_items"` results in one section (public-facing name → spec-key mapping in `_SECTIONS`).

### 10.4 Retriever (`rag/retriever.py`)

Two-stage:

- **Stage 1 — Hybrid Qdrant.** Dense (`nomic-embed-text-v1.5`) + sparse (`Qdrant/bm25`) prefetched separately at `top_k * 2`, then fused with `Fusion.RRF` server-side.
- **Stage 2 — Cross-encoder rerank.** BGE reranker (`max_length=512`) scores `(query, chunk_text)` pairs; sorted DESC; top `RERANK_TOP_K`.
- Filters (all AND'd): `meeting_id`, `school_slug`, date range via `meeting_year` int range (tightened to `meeting_month` when both bounds share a year), `speaker` (exact match), `meeting_type` (source_type).
- Models are lazy-loaded module singletons.

### 10.5 Generator (`rag/generator.py`)

- Ollama chat with a system prompt + a **one-shot fake turn** demonstrating `[N]` inline citations (`_ONESHOT_USER` / `_ONESHOT_ASSISTANT`). The redundant "reminder" is also appended to the user turn because "recent instructions weigh more heavily on smaller models". (`generator.py:161-166`)
- Citation numbering is unified across SQL + RAG so that `[N]` in the prompt matches `citations[i].index` (`_build_prompt_and_citations`).
- `temperature=0.1`, `num_predict=1024`.
- Streaming and non-streaming modes.

### 10.6 EARS requirements

- **REQ-RAG-1 (Ubiquitous)** — Every factual sentence produced by the generator shall be followed by `[N]` markers referencing a source in the prompt (system prompt hard rule + reminder + few-shot).
- **REQ-RAG-2 (Event-driven)** — When the query is classified as `none`, the orchestrator shall return the canned "I'm Neo…" message without contacting Postgres or Qdrant. (`answer.py:118-140`)
- **REQ-RAG-3 (Event-driven)** — When the router matches "the last N meetings" (N ≥ 2), it shall NOT route to `latest_meeting`; it shall fall through to `rag/hybrid`. (`query_router.py:164-170, 316-317`)
- **REQ-RAG-4 (State-driven)** — While the caller passes a `meeting_id` filter, the retriever shall AND it into the Qdrant filter as `MatchValue` on `meeting_id`. (`retriever.py:146-147`)
- **REQ-RAG-5 (Optional)** — Where the LLM classifier call raises, `_llm_route` shall default to `hybrid` and log nothing. (`query_router.py:417-418`)

---

## 11. Frontend Contract (Next.js 14)

Pages under `frontend/src/app/`:

| Route | Component | Backend calls |
|---|---|---|
| `/` | `HomePage` | `fetchMeetings`, `fetchSchools` |
| `/meetings` | list | `fetchMeetings` |
| `/meetings/[id]` | detail | `fetchMeeting` (MeetingOverview) |
| `/meetings/[id]/transcript` | transcript | `fetchMeetingTranscript` |
| `/votes` | list + stats | `fetchVotes`, `fetchVotesSummary`, `exportVotesCsvUrl` |
| `/financials` | list + stats | `fetchFinancials`, `fetchFinancialsSummary`, `exportFinancialsCsvUrl` |
| `/insights` | matrix + drawer | `fetchInsightMatrix` |
| `/insights/[insightId]` | detail | `fetchInsightDetail` |
| `/ask` | chat UI | `askNeo` |

Notable behaviors (from `frontend/src/lib/api.ts` and `types/index.ts`):

- `TypeScript Pick<>` is used to derive `VoteSummary`/`FinancialSummary` from `Vote`/`Financial` — mirrors the Python-side `pick()` helper for a single source of truth. (refactor_candidates #2 resolved.)
- The insights fetcher normalizes each cell to `InsightCell[]` (accepts both list and legacy single-object shape) so the frontend is tolerant to API evolution.
- Ask persists state to `sessionStorage` (per graph community 30 note).
- `AskResponse` may include `meeting_*` fields when route is `latest_meeting` — the UI renders a meeting header + "View full meeting →" link.

---

## 12. Non-Functional Observations

- **Reproducibility.** Meetings carry `file_hash` (sha256 of source VTT/audio) and `processing_version`. Chunks carry `processing_version`. Indexer computes stable Qdrant point IDs via `uuid5(NAMESPACE_DNS, chunk_id)` so re-index is idempotent. (`indexer.py:67-73`)
- **Idempotency.** Every pipeline phase filters by status → safe to re-run any time; already-done work is skipped by design.
- **Windows-first workstation.** README specifies PowerShell examples; `observability/query_log.py:44-46` uses a `threading.Lock` around append specifically because "Windows file handles are stricter" than POSIX atomic-line appends.
- **CUDA-pinned wheels.** Torch/torchvision/torchaudio pinned to cu128 wheel index; WhisperX only installable from GitHub. README explicitly instructs `uv run --no-sync` to prevent `uv sync` from wiping WhisperX.
- **Trust asymmetry.** RAG SQL context enforces `needs_review = FALSE`; the human-browsable list endpoints do not. Cross-cutting undocumented-until-inline design choice.
- **Observability**: JSONL rather than OTel/Phoenix — deliberate. `log_query` shape mirrors OTel span attributes so a future swap is call-site compatible. (`observability/query_log.py:9-13`)
- **Quality-gate thresholds** are stricter in `pipeline/quality_gate.py` (MIN_CHUNK_QUALITY=0.50, MIN_CHUNK_COUNT=5) than in `config.py` (MIN_CHUNK_QUALITY_SCORE=0.60). See Uncertainties.

---

## 13. Inferred Acceptance Criteria

1. **Pipeline end-to-end.** A newly seeded YouTube channel, given a fresh `discovered` row, shall walk to `indexed` after running each phase once, with a `PipelineRun` row per phase and non-null `raw_vtt_path` / `chunks_jsonl_path`.
2. **RAG citation contract.** For a happy-path `/ask` call, `AskResponse.answer` shall contain at least one `[N]` marker matching a `citations[i].index`. (`observability.query_log._MARKER_RE` audits this.)
3. **Adapter dispatch.** Setting `School.default_source_type = 'panopto'` shall route `collector`/`caption_downloader` through `PanoptoAdapter` without any change to those phases.
4. **State-machine safety.** Any code path that filters `meetings.status` shall import the eligibility tuple from `pipeline.states`; a byte-equivalent test exists in the states-refactor evidence (refactor_candidates #3).
5. **RAG trust filter.** No row with `needs_review = TRUE` shall appear in a `build_sql_context` output.
6. **Eval regression.** `uv run --no-sync python tests/test_eval.py` shall pass 8/9 cases (baseline from `feedback_phase2_caption_adapters` memory as of 2026-05-18). Regressions require a prompt / retrieval investigation before merging.

---

## 14. Uncertainties & Questions

Items that could not be resolved from code alone; flagging for human owner.

1. **`query_logs` DB table vs JSONL file.** `database/models.py:473-488` defines a `query_logs` table, but the live writer (`observability/query_log.py`) writes JSONL. Is the DB table dead code, a future OTel target, or fed by an out-of-tree job? — spec-miner did not find any INSERT.
2. **Two `quality_gate` threshold sources.** `config.py` exposes `MIN_QUALITY_SCORE=0.6` and `MIN_CHUNK_QUALITY_SCORE=0.6`; `pipeline/quality_gate.py` hard-codes `MIN_CHUNK_QUALITY=0.50` and `MIN_CHUNK_COUNT=5` at module level, ignoring `config`. Which is authoritative? (Guess: the module constants — `config` values look vestigial.)
3. **Claude vs Ollama for RAG generation.** README table says "Cloud LLM (RAG answers): Anthropic Claude". `rag/generator.py` calls `ollama.chat` with `config.OLLAMA_MODEL` and never touches `ANTHROPIC_API_KEY`. Was Claude an intended migration that never landed, or does an alternate generator exist off-tree?
4. **Panopto adapter class export vs singleton.** `pipeline/sources/__init__.py` imports both `PanoptoAdapter, adapter as _panopto`; only `adapter` is registered. Class re-export is presumably for tests — worth confirming no runtime path uses the class.
5. **Terminal state `failed`.** `states.py` defines a generic `failed` status, but no phase writes it and no recovery target handles it (only `asr_failed`, `processing_failed`, `rejected` are in `RECOVERY_TARGETS`). Is it purely for legacy rows?
6. **Auth / rate limiting.** No authentication middleware, API keys, or per-IP throttling observed on FastAPI. Deployment context (single-operator workstation) implies this is intentional but worth confirming before external exposure.
7. **`AskResponse.intent`.** Field is declared (`api/schemas/ask.py:34`) but never populated by `handle_ask` (`api/services/ask_service.py:34-45`) — always null on the wire.

---

## 15. Recommendations

Low-risk, spec-driven:

- **R1 — Resolve #2 uncertainty.** Either remove the vestigial `config.MIN_*_SCORE` values or wire them into `quality_gate.py`. Two sources of truth is exactly the pattern the states.py refactor set out to eliminate.
- **R2 — Populate or drop `AskResponse.intent`.** It's on the wire type but never set — easy footgun for a frontend consumer.
- **R3 — Add an auth story before ngrok/deploy.** Even a shared-secret header would prevent accidental public exposure of trustee data.
- **R4 — Document the trust-threshold asymmetry.** The `needs_review=FALSE` filter is a real design decision (RAG trusts less than the browsable API); it deserves a top-level README paragraph, not just an inline comment.
- **R5 — Consider closing the `query_logs` vs JSONL loop.** Either promote the JSONL writer to also insert into `query_logs`, or delete the table.
- **R6 — File the graphify `extract_votes` miss** (refactor_candidates #8) upstream — reproducible across builds.

Deferred (in `refactor_candidates.md`, still open):
- #1 full 4-target unification (partial complete)
- #9 graphify-side 0.5-confidence filter
- #10 custom `meeting.status` AST extractor for the graph

---

*End of spec — sections roughly follow the `spec-miner` template (technology stack, module structure, EARS requirements, non-functional observations, inferred acceptance criteria, uncertainties, recommendations).*
