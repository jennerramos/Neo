# Neo v2 — Consolidated Implementation Plan

> Date: 2026-08-04 · Author: consolidated from the three prior reviews.
> Inputs: `specs/neo_v2_reverse_spec.md`, `specs/neo_v2_architecture_review.md`, `specs/neo_v2_rag_review.md`.
> Purpose: one ranked, deduplicated, evidence-grounded work list to take Neo v2 from workstation demo to a 5-user public pilot.
> Verification pass (2026-08-04): re-confirmed the three blockers against source before writing — `/ask` is `def` not `async def` (`api/routers/ask.py:10`), no `search_query:` / `search_document:` prefixes anywhere in the repo, and zero auth-related imports in `api/`.

---

## 0. How this document was built

- **Combined** overlapping items from the three reviews (11 pairs collapsed — see §5 map at the end).
- **Separated** confirmed defects (file:line + reproducible behaviour) from hypotheses (well-founded but unverified without a runtime test).
- **Resolved** two apparent contradictions:
  1. README says the RAG generator uses "Anthropic Claude"; code uses Ollama. Source is authoritative — README is stale. Reverse-spec §14 and RAG-review §1 already flagged this. Plan treats generation as Ollama-based.
  2. Reverse-spec §3 said in-browser calls use `/api` and SSR uses `NEO_API_TARGET` — technically correct, but incomplete: SSR pages (`/meetings`, `/insights`) *also* require the backend reachable at request time. Deployment topology in the plan reflects this.
- **Prioritized** by: reliability → security → RAG answer quality → performance → maintainability. Two exceptions where a maintainability fix is *coupled* to a higher-priority item — noted inline.
- **Excluded** speculative infrastructure: no Kubernetes, no service mesh, no message broker, no observability platform beyond the existing JSONL trace. Nothing added until pilot traffic warrants it.

---

## 1. Priority ladder (at a glance)

| Phase | Trigger to begin | Trigger to consider done |
|---|---|---|
| **P0 — Reliability & security blockers** | now | all P0 items merged, tested locally |
| **P1 — Deploy to VPS** | P0 green | pilot users can reach `neo.<domain>` over TLS |
| **P2 — RAG quality tightening** | first pilot week | eval delta ≥ 0 on expanded set |
| **P3 — Hygiene & follow-ups** | pilot stable | as time allows |

Each phase is independently shippable. Nothing in P1 depends on P2, and nothing in P2 depends on P3.

---

## 2. P0 — Reliability & security blockers (pre-deploy)

These four items are the difference between "demo runs on your laptop" and "pilot works over the internet". Nothing below this phase matters until they are done.

### P0-1 — Rewrite `/ask` as async streaming with explicit LLM timeouts

- **Type:** Confirmed defect (multi-layer, all four contributors verified).
- **Sources merged:** RAG-review F-1 (BLOCKER), architecture-review ADR-003, architecture-review §4 root-cause trace.
- **Files:** `api/routers/ask.py:10`, `api/services/ask_service.py:16-51`, `rag/generator.py:214-255`, `rag/query_router.py:407-418`.
- **Evidence:**
  - `api/routers/ask.py:10` — `def ask(req: AskRequest)`, not `async def`. FastAPI shunts to a threadpool (default 40 threads). Under any concurrency spike, requests queue and their clients time out before they run.
  - `rag/generator.py:249-255` — `ollama.chat(...)` called with no `timeout=` argument. Hung Ollama = hung request.
  - `rag/query_router.py:407-418` — same missing timeout on the classifier call.
  - Response is a single JSON blob only after full generation. Every intermediate proxy (Next.js undici, Caddy, Cloudflare) has an idle-timeout policy that trips during 30–90 s generations.
- **Fix (approach, not code):**
  1. Change `/ask` to `async def`; call sync retriever/rerank code via `starlette.concurrency.run_in_threadpool`.
  2. Add a `stream: bool = False` query param. When true, return `StreamingResponse(media_type="text/event-stream")` wrapping the generator's already-existing `stream=True` mode.
  3. Add `timeout=(5.0, 120.0)` to the generator's `ollama.chat`; add `timeout=(2.0, 5.0)` to the router's classifier call. Failure of the classifier defaults to `hybrid` — already the fallback (`query_router.py:417-418`).
  4. Frontend `AskBox.tsx` consumes SSE via a `ReadableStream` reader; keeps the existing non-streaming fallback for eval scripts.
- **Verification test:** run two `POST /ask` calls in parallel via curl against local; both complete. Under `?stream=true`, first byte < 3 s.
- **Effort:** ~200 LoC total across backend and frontend.
- **Blocks pilot?** Yes.

### P0-2 — Add authentication before public exposure

- **Type:** Confirmed defect.
- **Sources merged:** architecture-review ADR-004, reverse-spec §14 Uncertainty #6.
- **Files:** `api/main.py:17-77` (routers registered here; no `Depends(current_user)` on any); grep for `Depends.*current_user|Bearer|Authorization` in `api/` returns zero hits.
- **Evidence:** No auth middleware, no header check, no dependency guarding any router. `/export/votes.csv` and `/export/financials.csv` return up to 5000 rows to any caller.
- **Fix (approach):** magic-link auth via a hosted provider (Clerk or Supabase Auth free tier — 5 users free). Frontend sets a session cookie; FastAPI validates the JWT in a `Depends(current_user)` on every non-`/health` route. Log `user_id` into `observability/query_log.py:91-114`.
- **Alternative** if we want zero code: HTTP Basic auth at Caddy with a five-line `.htpasswd`. Loses per-user logging.
- **Verification test:** curl `POST /ask` without a token → 401. With a valid token → 200. `/health` unaffected.
- **Effort:** ~100 LoC + provider setup. Basic-auth alternative is 5 lines of Caddyfile.
- **Blocks pilot?** Yes.

### P0-3 — Make CORS + `NEO_API_TARGET` env-configurable

- **Type:** Confirmed defect (deployment blocker).
- **Sources merged:** architecture-review §2 verification row, RAG-review "not blocking pilot but blocks deploy".
- **Files:** `api/main.py:31-41`, `frontend/next.config.mjs:6`, `frontend/src/lib/api.ts:35-38`.
- **Evidence:** CORS `allow_origins` is a hard-coded list of `localhost:3000`, `localhost:3001`, `127.0.0.1:3000`. Any other hostname (VPS, custom domain) will be rejected by the browser.
- **Fix (approach):** read `NEO_CORS_ORIGINS` env var (comma-separated), parse into a list at startup, fall back to the current dev defaults if unset. Same env-driven approach for `NEO_API_TARGET` (already env-configurable in `next.config.mjs:6`).
- **Verification test:** set `NEO_CORS_ORIGINS=https://neo.example.com`, hit `POST /ask` from a page on that origin, no CORS error in browser console.
- **Effort:** ~15 LoC.
- **Blocks pilot?** Yes (any non-localhost deployment).

### P0-4 — Warm retriever models at startup

- **Type:** Confirmed defect (materially affects first-user experience).
- **Sources merged:** RAG-review F-5, architecture-review §4 "First-request model loading" note.
- **Files:** `api/main.py` (add lifespan), `rag/retriever.py:57-93` (existing lazy loaders).
- **Evidence:** BGE reranker, fastembed dense, fastembed sparse are all lazy singletons. First `/ask` in a fresh process pays 5–15 s of model-load time. That first user is the one whose request most-likely gets ECONNRESET (compounds P0-1).
- **Fix (approach):** register a FastAPI `lifespan` context that calls `_get_dense_model()`, `_get_sparse_model()`, `_get_reranker()` once at boot.
- **Verification test:** time 10 sequential `/ask` calls in a fresh process; first-call latency should drop ≥ 10 s.
- **Effort:** ~20 LoC.
- **Blocks pilot?** No, but ship in the same PR as P0-1 — same file, same test.

---

## 3. P1 — Deploy to a small VPS

Only start once every P0 item is merged and locally verified.

### P1-1 — Dockerize FastAPI backend

- **Sources:** architecture-review ADR-001 (Compose stack).
- **Deliverable:** `Dockerfile` (multistage; no CUDA layer — serving is CPU-only), `.dockerignore`, single Gunicorn+Uvicorn command (`gunicorn api.main:app -k uvicorn.workers.UvicornWorker -w 1` — see F-20 sizing note).
- **Constraint:** **one worker** on a 2 GB VPS. Each worker loads ~1.4 GB of RAG models (F-20). Bump to 2 workers only when running on 4 GB+.
- **Verification:** `docker run` locally, hit `/health`, hit `/ask` with an auth token.
- **Effort:** ~50 LoC of Dockerfile + compose entry.

### P1-2 — Dockerize Next.js frontend

- **Deliverable:** multistage `Dockerfile` producing a `next start` image. Bake `NEXT_PUBLIC_API_URL` at build time; leave `NEO_API_TARGET` runtime-configurable for SSR.
- **Verification:** SSR pages (`/meetings`, `/insights`) render with data from a Compose-hosted backend.
- **Effort:** ~30 LoC.

### P1-3 — Compose stack with Caddy TLS

- **Deliverable:** `docker-compose.yml` with services: `caddy`, `web`, `api`, `qdrant`, `postgres`. Caddyfile grants long idle timeouts to `/api/ask*` (`timeouts { idle 300s read_body 30s }` for that matcher). Automatic TLS via Let's Encrypt.
- **DNS:** `neo.<domain>` → Caddy → `web`; `neo.<domain>/api/*` → Caddy → `api`. Two subdomains keep browser calls same-origin and let SSR call `http://api:8000` internally.
- **Verification:** `curl https://neo.<domain>/health` returns 200 with a valid cert.
- **Effort:** one afternoon.

### P1-4 — Provision VPS and cut over

- **Recommended shape:** Hetzner CX22 (2 vCPU, 4 GB, ~€6/mo) or DigitalOcean Basic 4 GB (~$24/mo). Not the 2 GB tier — F-20 says one worker fits, but Postgres + Qdrant + Caddy need headroom.
- **First deploy checklist:** run `alembic upgrade head`, seed schools via `database/seed.py`, snapshot workstation Qdrant → restore on VPS, verify `/ask` end-to-end.
- **Effort:** half a day incl. DNS wait.

### P1-5 — Workstation → VPS data flow

- **Sources:** architecture-review ADR-005 (updated 2026-08-04 with cadence note).
- **Deliverable:** WireGuard tunnel between workstation and VPS; pipeline `.env` on workstation points `DATABASE_URL` and `QDRANT_HOST` at the VPS over the tunnel. Weekly `wg-quick up` window is enough (8 colleges × 1–2 meetings/month = 8–16/month; no continuous traffic).
- **Alternative if the WireGuard route feels heavy:** run pipeline against a local Postgres, then `pg_dump` + `qdrant` snapshot → upload → restore on VPS. Same weekly cadence.
- **Verification:** run pipeline end-to-end from workstation; check new meetings appear in the UI.
- **Effort:** two hours WireGuard, plus one full pipeline run.

### P1-6 — Nightly backup

- **Deliverable:** cron on the VPS running `pg_dump | gzip | aws s3 cp` (or rclone to Backblaze B2 / Cloudflare R2). Keep 14 daily + 4 weekly. Restore drill once before pilot begins.
- **Verification:** delete a row on a staging DB, restore from last night's dump, confirm row returns.
- **Effort:** two hours.

---

## 4. P2 — RAG quality tightening (first pilot week)

Start in the pilot's first week, not before. Every item below is measurable against an eval set — expand that first.

### P2-0 — Expand eval set (prerequisite, no code change under test)

- **Type:** Confirmed defect in tooling.
- **Sources:** RAG-review F-17.
- **Files:** `eval/eval_set.jsonl` (9 cases today), `tests/test_eval.py` (harness).
- **Fix (approach):** grow to ≥ 25 cases. Distribute across the 6 routes (`sql`, `rag`, `hybrid`, `compare`, `latest_meeting`, `none`) and all 8 schools (per updated architecture-review §3.1a: 5 YouTube, 2 Panopto, 1 Ravnur). Add `expected_chunk_ids` (a list of Qdrant point IDs that should appear in `AskResponse.citations`). Extend `test_eval.py:107-178` to compute precision@8 against `expected_chunk_ids`.
- **Rationale for order:** P2-1 through P2-4 change embeddings, chunking, or the prompt. Without a broader eval, we can't safely tell if the change helped or hurt.
- **Verification test:** re-run before every P2 change; commit the resulting `n_passed / total` in the PR description.
- **Effort:** 4–6 hours of case authoring (needs a human with domain judgement).

### P2-1 — Remove "HCC" hard-coding from the off-topic message

- **Type:** Confirmed defect (user-visible after school 6+ was seeded).
- **Sources:** RAG-review F-11.
- **Files:** `rag/answer.py:118-140` — the `route == "none"` message says `"I'm Neo, an assistant for HCC board meeting intelligence"`.
- **Fix (approach):** replace with a phrase covering all tracked institutions, e.g. `"I'm Neo, an assistant for board meetings across our tracked community colleges."` Keep the "try asking …" examples generic.
- **Verification test:** issue an off-topic query with Dallas College `school_slug` and confirm the response references the general system, not HCC.
- **Effort:** 5 LoC.

### P2-2 — Fix overlap-tail sentence splitting

- **Type:** Confirmed defect.
- **Sources:** RAG-review F-3.
- **Files:** `pipeline/data_packager.py:277-279`.
- **Evidence:** Splitting on the literal `"."` fragments money (`$1,234.50` → two tokens), decimals (`3.5%`), abbreviations (`Mt. San Antonio`), initials, URLs. The bad prefix ends up carried into the *next* chunk as `[…] {overlap_tail}`.
- **Fix (approach):** replace `all_text.split(".")` with the existing `_SENT_END_RE` regex from `pipeline/cleaner.py:71` (`re.compile(r"(?<=[\.!?])\s+")`). Same file already imports from `cleaner`. No new dependency.
- **Requires:** re-package + re-index all indexed meetings. Bump `PROCESSING_VERSION` in `data_packager.py:66` so the marker in `chunks.jsonl` reflects the fix.
- **Verification test:** unit test with `"$1,234.50 was approved on 3.5% terms by Mt. San Antonio College."`; assert overlap tail is the whole sentence, not fragments. Then re-run eval set (P2-0 required).
- **Effort:** 20 LoC + one weekly ingest cycle for re-index.

### P2-3 — Add nomic task prefixes on both sides

- **Type:** Hypothesis (well-founded — nomic model card is explicit; fastembed does not auto-prefix at time of audit; both sides verified missing).
- **Sources:** RAG-review F-2 (HIGH).
- **Files:** `pipeline/indexer.py:148-157` (`_embed_dense`), `rag/retriever.py:108-120` (`_embed_query_dense`).
- **Fix (approach):**
  - In `_embed_dense`, prepend `"search_document: "` to every text before passing to `model.embed`.
  - In `_embed_query_dense`, prepend `"search_query: "`.
  - Symmetry matters: **do both together**, and re-index the full Qdrant collection. Asymmetric prefixing is worse than none.
  - Bump `PROCESSING_VERSION` (again; can bundle with P2-2's re-index).
- **Verification test:** pick 5 known-answer eval cases (from the P2-0 expanded set). Log reranker scores of the intended chunk before vs after. Expect the prefixed version to score higher on at least 3/5. Overall eval delta ≥ 0.
- **Rollback:** if eval regresses, revert both files; re-index again from the JSONL on disk (chunks aren't lost).
- **Effort:** 10 LoC. Re-index amortizes with P2-2 in the same weekly cycle.

### P2-4 — Instrument reranker truncation, then decide

- **Type:** Confirmed hidden behaviour, remediation is a judgement call.
- **Sources:** RAG-review F-4.
- **Files:** `rag/retriever.py:258-279` (`_rerank`), plus `pipeline/data_packager.py:67-68` (`TARGET_TOKENS=400`, `MAX_TOKENS=500`).
- **Evidence:** BGE reranker `max_length=512` includes the query + `[SEP]` tokens; chunks up to 500 tokens can be silently right-truncated at rerank time.
- **Fix (two-step, no code change first):**
  1. Instrument: log `len(bge_tokenizer.encode(query + chunk_text))` and count how often it exceeds 512. Ship the counter into `observability/query_log.py`.
  2. Based on the counter after ~50 real queries, choose one of:
     - Shrink `MAX_TOKENS` to 350 (repackage + reindex — bundle with P2-2/P2-3).
     - Leave as-is and document "reranker scores leading context, not full chunk".
     - Do not upgrade the reranker for the pilot — bge-reranker-large is 3× VRAM.
- **Verification test:** the counter's own output.
- **Effort:** 30 LoC for instrumentation. Decision cost varies.

### P2-5 — Explicit connection-pool sizing on the API's DB engine

- **Type:** Confirmed defect (latent, will surface under sync-heavy load).
- **Sources:** architecture-review §2 verification, implicit in RAG-review F-1's downstream effects.
- **Files:** `api/db/session.py:12` — `create_engine(config.DATABASE_URL, pool_pre_ping=True)` uses default pool (5+10).
- **Fix (approach):** set `pool_size=10, max_overflow=10, pool_recycle=1800`. Small but explicit. Once P0-1 is done, the effective hold time on each connection drops materially — this is the safety belt.
- **Verification test:** open 15 parallel `/ask` calls; DB connections stay ≤ 20 in `pg_stat_activity`.
- **Effort:** 3 LoC.

---

## 5. P3 — Hygiene & follow-ups

Batch these once the pilot is stable. Every item is a footgun or a small quality-of-life fix, none block anything.

| ID | Source | Title | Files | Effort |
|---|---|---|---|---|
| P3-1 | reverse-spec §14 #2, RAG-review F-6 | Delete or wire `config.MIN_*_SCORE` values that `quality_gate.py` ignores | `config.py:126-128`, `pipeline/quality_gate.py:58-61` | 5 LoC |
| P3-2 | reverse-spec §14 #7, ask-response note | Populate `AskResponse.intent` from `decision["intent"]` (currently always null on the wire) | `api/services/ask_service.py:34-45`, `api/schemas/ask.py:34` | 3 LoC |
| P3-3 | reverse-spec §14 #1 | Decide `query_logs` DB table: wire the JSONL writer to also INSERT, or drop the table via Alembic | `database/models.py:473-488`, `observability/query_log.py` | 30 LoC or migration |
| P3-4 | RAG-review F-9 | Change date filter from `meeting_year` int-range to a range on the ISO-string `published_date` payload field | `rag/retriever.py:152-168`, `pipeline/indexer.py:269` (field already present) | 15 LoC |
| P3-5 | RAG-review F-7 | Hide `speaker` filter from the API until name-resolution exists, or add a per-meeting speaker→name mapping | `api/schemas/ask.py` (if exposed), `rag/retriever.py:170-173` | 5–100 LoC |
| P3-6 | RAG-review F-10 | Count and log chunks exceeding the 2000-char truncation limit in `pipeline/indexer.py:311` | `pipeline/indexer.py:307-320` | 10 LoC |
| P3-7 | RAG-review F-8 | Ablation-test dense-only vs hybrid on expanded eval; drop sparse if no lift | `rag/retriever.py:183-251`, eval harness | 1 hour of analysis |
| P3-8 | RAG-review F-15 | Add a small `functools.lru_cache` on `handle_ask` keyed on the request signature | `api/services/ask_service.py` | 15 LoC |
| P3-9 | RAG-review F-13 | On 0-chunk retrieval, retry once with `school_slug=None` and prepend a caveat in the answer | `rag/answer.py:329-343` | 25 LoC |
| P3-10 | RAG-review F-16 | Log full answer (or store separately) instead of `[:240]` preview only | `observability/query_log.py:103` | 10 LoC |
| P3-11 | RAG-review F-18 | Detect Ollama `finish_reason=length` and mark truncated answers in the response | `rag/generator.py:214-255` | 20 LoC |
| P3-12 | architecture-review §7 recommendations | Update README to reflect: 8 colleges (not 5), Ollama not Claude, VPS deploy story | `README.md` | 30 lines of markdown |
| P3-13 | reverse-spec cross-cutting finding | Document the `needs_review=FALSE` trust-asymmetry in the README so future contributors don't remove it | `README.md`, inline in `sql_context.py` | 15 lines of markdown |
| P3-14 | architecture-review §8 monitoring | Add UptimeRobot (free) ping on `https://neo.<domain>/health` | ops-only, no code | 10 min |

None of these are eval-affecting *except* P3-4, P3-7, P3-9 — those must re-run the P2-0 eval before merging.

---

## 6. Explicitly out of scope

Called out so they don't get added later without justification:

- **Kubernetes / Nomad / service mesh.** Explicit non-goal.
- **Managed OpenTelemetry / Datadog / Sentry.** JSONL + UptimeRobot is enough for the pilot. Revisit if MTTR drills fail.
- **Migrating to a hosted LLM by default.** ADR-006 says keep this as a config switch, but do not do the migration until pilot cost data justifies it.
- **Rewriting the ingestion pipeline as an orchestrated DAG (Prefect / Airflow).** README mentions "Prefect planned (Phase 13)" — do not do this at 8–16 meetings/month. A weekly cron of the 8-script sequence is the whole ops story.
- **`from_attributes = True` consistency work** beyond what refactor_candidates #4 already resolved.
- **Refactor-candidates #1 (full 4-target unification).** Partial fix already shipped; the remaining pipeline-extractor work is not pilot-affecting.
- **Splitting `PipelineRun` writes into a proper metrics store.** Cross-cutting finding, not blocking.
- **Any change to WhisperX / pyannote / CUDA stack.** They work; leave them alone until Torch or WhisperX forces our hand.
- **Frontend redesign, new pages, dark mode.** Feature-freeze the frontend for the pilot.
- **A second Panopto / Ravnur / YouTube adapter improvement pass** unless a specific adapter breaks in production.

---

## 7. Cross-review deduplication map

For traceability — where the same issue was described by more than one report.

| Issue | Reverse spec | Arch review | RAG review | Plan item |
|---|---|---|---|---|
| Sync `/ask` + no timeout + no streaming | — | §4 root cause, ADR-003 | F-1 | **P0-1** |
| No auth on any endpoint | §14 #6 | ADR-004 | (referenced) | **P0-2** |
| CORS locked to localhost | — | §2 verification | (referenced) | **P0-3** |
| Cold-start model loading | — | §4 note | F-5 | **P0-4** |
| Nomic embed prefixes missing | — | — | F-2 | **P2-3** |
| Overlap tail broken by `.` split | — | — | F-3 | **P2-2** |
| BGE reranker truncation at 512 | — | — | F-4 | **P2-4** |
| DB engine has no explicit pool size | — | §2 verification | (implicit) | **P2-5** |
| Two chunk-quality thresholds | §14 #2 | §7 R1 | F-6 | **P3-1** |
| Ollama not Claude (README stale) | §14 #3 | §2 verification | §1 verification | (README fix in P3-12) |
| `query_logs` table unused | §14 #1 | §7 R5 | — | **P3-3** |
| `AskResponse.intent` never populated | §14 #7 | §7 R2 | — | **P3-2** |
| "HCC" hard-coded in canned message | — | — | F-11 | **P2-1** |
| Date filter is year/month | — | — | F-9 | **P3-4** |
| Speaker filter unusable | — | — | F-7 | **P3-5** |
| Weak eval set (9 cases) | — | — | F-17 | **P2-0** (prereq to P2-1..P2-4) |
| 8 colleges (README says 5) | (updated) | §3.1a (updated) | (updated) | (README fix in P3-12) |
| Ingestion pipeline low volume | — | §3.1a | — | Frames P1-5 |

---

## 8. Effort roll-up

- **P0:** ~1–2 days engineering.
- **P1:** ~1–2 days ops.
- **P2:** ~1 week including eval-set authoring (mostly domain work, not code).
- **P3:** as time allows; each item is < 100 LoC.

**Path to pilot go-live:** P0 + P1 = ~1 week of focused work. Everything after that is quality iteration on a live system with real feedback loops.

---

*End of plan. No source files were modified.*
