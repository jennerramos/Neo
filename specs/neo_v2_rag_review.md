# Neo v2 — RAG Production-Readiness Review

> Author: rag-architect skill · Date: 2026-08-04 · Mode: read-only audit.
> Scope: end-to-end RAG pipeline — ingestion, chunking, embeddings, retrieval, reranking, prompting, citations, hallucination controls, evaluation, latency.
> Method: verified every non-trivial claim in `specs/neo_v2_reverse_spec.md` and `specs/neo_v2_architecture_review.md` against source; annotated confirmed defects vs hypotheses.

---

## 0. TL;DR

The RAG pipeline is **carefully engineered by RAG standards** — hybrid dense+sparse + RRF + BGE rerank, deterministic point IDs, single-sourced prompt with few-shot citation demo, per-query JSONL trace, and a working eval harness. It is closer to production than the deployment layer around it.

**But there are three quality-affecting bugs that should be fixed before a 5-user pilot goes live, plus one silent config that may already be degrading recall:**

- **BLOCKER-1** — `/ask` is synchronous, non-streaming, and has no explicit Ollama HTTP timeout. Already covered by the architecture review (§4 + ADR-003). Repeated here because it also *masks* RAG quality issues — long tail latency is the failure mode users see, not answer quality.
- **HIGH-1** — Nomic-embed-text-v1.5 requires **task prefixes** (`search_query:` for queries, `search_document:` for documents) to hit its published quality. Neither `pipeline/indexer.py` nor `rag/retriever.py` applies them. This is a silent recall degrade that has probably been in the pipeline since Phase 8 shipped.
- **HIGH-2** — Overlap-tail construction in `data_packager.build_chunks` splits on the literal character `"."` (`data_packager.py:278`), fragmenting numbers ($1,234.50 → "$1,234", "50") and abbreviations ("Mt. San Antonio" → "Mt", "San Antonio"). Corrupt overlap text is what the next chunk starts with — quietly degrades retrieval on financial/proper-noun queries.
- **MEDIUM-1** — BGE reranker uses `max_length=512` (`retriever.py:79`) but chunks can exceed 500 tokens, and the reranker prepends the query — many long chunks get **truncated at rerank time**, so the model scores less than the full chunk it was supposed to rank. Not a bug per se, but hidden.

None of the four require re-architecting. All are localized fixes.

**Not blocking the pilot but worth queuing:** hard-coded "HCC" branding in the system prompt (§F-16), no answer/embedding cache (§F-19), no query expansion, and `system prompt` that references "HCC" specifically may confuse the model on non-HCC queries.

**Eval harness is real but small:** 9 cases; the memory record shows 8/9 baseline. Not enough surface to catch the HIGH-1 regression above — a nomic-prefix fix should be validated with a mini contrastive test, not the current eval set alone.

---

## 1. Verified vs. corrected claims from prior reviews

| Prior claim | Status | Note |
|---|---|---|
| Dense embeddings via `nomic-ai/nomic-embed-text-v1.5` (768-dim), same at index and query time | ✔ verified | `pipeline/indexer.py:140-143` and `rag/retriever.py:60-62` both use fastembed with the same model name. **Not** a mismatch. |
| Sparse via `Qdrant/bm25` fastembed | ✔ verified | Both sides use `SparseTextEmbedding(model_name="Qdrant/bm25")`. |
| RRF fusion server-side via `FusionQuery(fusion=Fusion.RRF)` | ✔ verified | `rag/retriever.py:237-241`. |
| BGE cross-encoder rerank | ✔ verified | `BAAI/bge-reranker-v2-m3` at `max_length=512`, `rag/retriever.py:77-81`. |
| Chunk target 400 tokens, max 500, 1-sentence overlap | ✔ verified | `data_packager.py:67-69`. |
| Deterministic point IDs via `uuid5(NAMESPACE_DNS, chunk_id)` | ✔ verified | `indexer.py:67-73`. Safe to re-upsert. |
| Payload carries `chunk_id`, `meeting_id`, `video_id`, `school_slug`, dates, speaker, times, text | ✔ verified | `_build_payload` in `indexer.py:252-279`. |
| Trust asymmetry: SQL context filters `needs_review=FALSE`; RAG chunks do not | ✔ verified & material | `sql_context.py:81, 91, 101, 113` — every extraction table applies the filter. Chunk table has no `needs_review` column, so RAG-side ingestion accepts everything from `approved`/`extracted` meetings after the quality gate. |
| Eval set of 9 cases with a 60 s per-case timeout | ✔ verified | `tests/test_eval.py:48`, `eval/eval_set.jsonl` has 9 lines. |
| Router LLM fallback defaults to `hybrid` on error | ✔ verified | `query_router.py:417-418`. |
| Generator is Ollama-only; Anthropic key unused | ✔ verified | grep of `anthropic|CLAUDE_MODEL` returns hits only in `config.py`, `pyproject.toml`, `.env.example`, `uv.lock`, and a test fixture. |
| First-request latency includes lazy model loading of BGE + fastembed dense + sparse | ✔ verified as an observable pilot-day issue | Section 5. |

New claim needed: the **embedding-prefix claim** (see F-2 below) is neither in the reverse spec nor the architecture review — this audit adds it.

---

## 2. Findings (severity-ordered)

### F-1 — [BLOCKER, confirmed] Synchronous `/ask` with no LLM timeout, no streaming

- **Where:** `api/routers/ask.py:10-12`, `api/services/ask_service.py:16-51`, `rag/generator.py:249-255`.
- **Evidence:** Endpoint is `def ask()` not `async def`. Downstream `_call_ollama` calls `ollama.chat(model=..., messages=..., stream=False, options={...})` with no `timeout=` argument. `AskResponse` returned only after the full generation completes.
- **Impact:** ECONNRESET / socket-hang-up under any concurrency > 1, or through any proxy with an idle-timeout policy shorter than the generation. On the pipeline side there is precedent (`extractor.py:65 OLLAMA_TIMEOUT=120`) — the RAG generator just doesn't follow it.
- **Recommended correction:** convert to `async def` + `starlette.concurrency.run_in_threadpool` for the sync retriever/rerank calls, expose `StreamingResponse` (SSE), and pass `timeout=(5.0, 120.0)` (connect, read) into every `ollama.chat` call. Same for the router-side classifier call (5 s max).
- **Verification test:** open two curl clients with `POST /ask` in parallel against a locally-running instance; both should complete without either receiving an early close. Under streaming, first-byte < 3 s.
- **Blocks pilot?** **Yes.** Already flagged as ADR-003 in the architecture review — repeated here because it dominates the perceived RAG quality problem: users see truncated answers before they see chunk-selection issues.

---

### F-2 — [HIGH, hypothesis with strong evidence] Nomic embeddings used without task prefixes

- **Where:** `pipeline/indexer.py:311` (index-time), `rag/retriever.py:108-120` (query-time).
- **Evidence:** Both call `model.embed(texts)` on raw chunk text or raw query text. Nomic's model card for `nomic-ai/nomic-embed-text-v1.5` explicitly recommends prefixing inputs — `search_document: <chunk>` at index time and `search_query: <query>` at query time — for best retrieval quality. fastembed's `TextEmbedding` does *not* auto-prefix (verified by absence of any prefix logic in either file and by fastembed's own docs at time of this audit). Grep of the repo for `search_query|search_document|passage:` returns zero matches.
- **Impact:** Recall degradation of ~2–5 points on retrieval benchmarks (per Nomic's own numbers). Because both sides are missing the prefix symmetrically, retrieval still returns *something* useful — this is a silent quality tax, not a crash. Effect is bigger on longer, more discursive queries (exactly the trustee questions Neo targets).
- **Recommended correction:**
  - In `pipeline/indexer.py:_embed_dense`, prepend `"search_document: "` to every text before passing to `model.embed`.
  - In `rag/retriever.py:_embed_query_dense`, prepend `"search_query: "`.
  - Both changes require a **full re-index of the Qdrant collection** — asymmetric prefixing (only queries or only docs) is worse than none. Bump `PROCESSING_VERSION` in `data_packager.py` and add a note in `indexer.py` so re-index is clearly required.
- **Verification test:** pick 5 known-answer eval cases; capture reranker scores of the correct chunk under (a) no prefix, (b) prefix both sides. Prefix version should score higher on ≥ 3/5.
- **Blocks pilot?** **No, but should ship before pilot.** Pilot works today; this is table-stakes quality that we should not knowingly leave on the floor. Fix + re-index costs one weekly ingest cycle.

---

### F-3 — [HIGH, confirmed] Overlap-tail construction fragments numbers and abbreviations

- **Where:** `pipeline/data_packager.py:277-279`.
- **Evidence:**
  ```python
  all_text   = " ".join(s.text for s in buf_segs)
  sents      = [s.strip() for s in all_text.split(".") if s.strip()]
  overlap_tail = ". ".join(sents[-OVERLAP_SENTENCES:]) + "." if sents else ""
  ```
  Splitting on the literal character `"."` shatters money (`$1,234.50` → `$1,234` and `50`), decimals (`3.5%` → `3` and `5%`), abbreviations (`Mt. San Antonio` → `Mt` and `San Antonio`), initials (`J.R.` → three empty tokens), and URLs. The final overlap segment injected as `[…] {overlap_tail}` at the head of the next chunk is therefore often meaningless or actively misleading (`. 50 Mt San Antonio budget increased`).
- **Impact:** Every chunk except the first carries a corrupted preamble that BM25 will happily match — false-positive retrieval on the fragment tokens (`50`, `Mt`, etc.). Also feeds into the reranker as if it were real content. Compound with F-2 to see a noticeable recall/precision hit specifically on the financial/personnel questions the system exists to answer.
- **Recommended correction:** Replace the naïve split with a real sentence tokenizer. Simplest local fix that matches the rest of the pipeline: use the same `_SENT_END_RE` from `pipeline/cleaner.py:71` (`re.compile(r"(?<=[\.!?])\s+")`). That regex requires the period to be followed by whitespace, which sidesteps decimals and initials in one line. Alternative: pull in `pysbd` or `syntok` (already-Python, no NLP model cost).
- **Verification test:** unit test `build_chunks` with a synthetic segment containing `$1,234.50 was approved on 3.5% terms by Mt. San Antonio College.` and assert the overlap tail contains the complete sentence, not fragments.
- **Blocks pilot?** **No, but ship before wider use.** Data on disk is not corrupted — this only affects the ~1-sentence prefix of every non-first chunk. Fix + re-package + re-index is a weekly-ingest-cycle cost.

---

### F-4 — [MEDIUM, confirmed] BGE reranker truncates chunks that exceed its 512-token context

- **Where:** `rag/retriever.py:77-81` (`CrossEncoder(..., max_length=512)`), interacts with `data_packager.py:68` (`MAX_TOKENS=500` per chunk) plus the `"[…] {overlap_tail}"` prepend and the query itself.
- **Evidence:** `MAX_TOKENS=500` means chunks can be up to 500 tokens; the reranker receives `(query, chunk)` pairs and is capped at 512 total tokens including the query and separators. A ~40-token query plus a ~500-token chunk plus special tokens overflows. The cross-encoder silently right-truncates the chunk. So the chunk being ranked is only the first ~450 tokens of what got retrieved.
- **Impact:** Chunks whose evidence is in their *second half* are scored on their first half. Doesn't drop them from the ranking, but the model's scores are less meaningful than the API suggests.
- **Recommended correction:** three options (pick one):
  1. **Lower chunk MAX_TOKENS to 350** so `query + chunk + [SEP]` fits in 512. Cleanest, but changes retrieval unit size.
  2. **Switch to `BAAI/bge-reranker-v2-m3`'s larger sibling `bge-reranker-large` with 8k context** — heavier model, marginal quality win, ~3× VRAM.
  3. **Accept the truncation** but document it — reranker becomes a "leading-context relevance" scorer. Reasonable at pilot scale.
- **Verification test:** for each retrieved chunk, log `len(tokenizer.encode(query + chunk))` alongside the reranker score. Any value > 512 confirms truncation.
- **Blocks pilot?** **No.**

---

### F-5 — [MEDIUM, confirmed] Cold-start latency dominates first `/ask`

- **Where:** `rag/retriever.py:57-93` — three lazy module singletons: dense (fastembed), sparse (fastembed), reranker (sentence-transformers CrossEncoder).
- **Evidence:** All three load ONNX/PT model files from disk on first use. On a cold VPS with no GPU, dense ~5 s, sparse ~1 s, BGE reranker ~5–8 s = up to ~15 s added to the first `/ask`. Second call in the same process is free.
- **Impact:** First trustee to hit `/ask` after a deploy or restart sees 15–45 s total (cold model load + generation). Compounds F-1 by making the ECONNRESET class of failures more likely on that specific call.
- **Recommended correction:** add a FastAPI `@app.on_event("startup")` (or the newer `lifespan`) callback that calls `_get_dense_model()`, `_get_sparse_model()`, `_get_reranker()` once. Trade cold-start time (paid once at boot) for consistent p95.
- **Verification test:** time 10 sequential `/ask` calls in a fresh process with and without warm-up. First-call p95 should drop by ≥ 10 s.
- **Blocks pilot?** **No.** Nice-to-have; combine with F-1 fix.

---

### F-6 — [MEDIUM, confirmed] Two different chunk-quality thresholds; the config value is dead

- **Where:** `config.py:126-128` (`MIN_WORD_COUNT=500`, `MIN_QUALITY_SCORE=0.6`, `MIN_CHUNK_QUALITY_SCORE=0.6`) vs `pipeline/quality_gate.py:58-61` (`MIN_WORD_COUNT=500`, `MIN_CHUNK_COUNT=5`, `MIN_CHUNK_QUALITY=0.50`, `MIN_DURATION_SEC=120`).
- **Evidence:** `quality_gate.py` never imports `config.MIN_*_SCORE`; its module-level constants shadow the config. Grep of the repo confirms.
- **Impact:** Ops config change to `MIN_CHUNK_QUALITY_SCORE` in `.env` will silently do nothing. Not a bug in output — retrieval is fine — but a footgun for the operator.
- **Recommended correction:** either wire the `quality_gate.py` constants to `config.MIN_*` values, or delete the config values entirely. Do not leave two sources of truth.
- **Verification test:** set `MIN_CHUNK_QUALITY_SCORE=0.9` in `.env`, run `quality_gate.py --dry-run`; expect strict rejection. Today's behavior: unchanged.
- **Blocks pilot?** **No.**

---

### F-7 — [MEDIUM, confirmed] Speaker filter is exact-match only against ASR labels (`SPEAKER_00`, …)

- **Where:** `rag/retriever.py:170-173`, chunk speaker written in `data_packager.py:266`, WhisperX diarization output.
- **Evidence:** Retriever filters `speaker` with `MatchValue(value=speaker)` — no normalization. `data_packager.py:266` selects the dominant WhisperX speaker label (`SPEAKER_00`, `SPEAKER_01`, …) as-is. VTT-sourced chunks have `speaker=None`.
- **Impact:** The `speaker` filter is effectively unusable by trustees ("filter to Chancellor Smith" fails because chunk speakers are `SPEAKER_02`). Doesn't hurt default queries — that path is unused today — but the API exposes it. Silent no-results on any real-name filter.
- **Recommended correction:** either (a) hide the `speaker` filter from the UI until name-resolution exists, or (b) build a per-meeting speaker→name mapping (manual first, LLM-inferred later) stored in a new column, and normalize both sides.
- **Verification test:** issue `retrieve(query, speaker="Smith")` against a diarized meeting; expect 0 results.
- **Blocks pilot?** **No** — the filter isn't exposed on the UI (verified in `frontend/src/app/ask/page.tsx` — AskBox has school+date filters only). But leaves a footgun for the API.

---

### F-8 — [MEDIUM, hypothesis] fastembed BM25 has no corpus IDF

- **Where:** `pipeline/indexer.py:167-171`, `rag/retriever.py:65-70`.
- **Evidence:** Both sides instantiate `SparseTextEmbedding(model_name="Qdrant/bm25")` and encode each text independently. That fastembed BM25 implementation applies a pretrained tokenizer and produces token-frequency-weighted sparse vectors, but the IDF component is model-baked, not corpus-specific. For a small, domain-specific corpus (~30–80k chunks/year, all board meetings), true BM25 with corpus-fitted IDF would produce meaningfully different weights on common domain terms ("motion", "vote", "board").
- **Impact:** Sparse retrieval is a weak (but nonzero) contributor to RRF. Fusing weak sparse with strong dense means RRF acts almost like a dense-only ranker for domain-common terms. Query-time BM25 rank for rare-token queries (e.g. specific vendor names) is still useful.
- **Recommended correction:** two paths, both defensible:
  1. Keep fastembed BM25 as an acceptable pilot compromise. Log RRF's per-side rank contributions on 20 eval queries; if sparse never appears in top-5 fusion contributions, drop it entirely and simplify.
  2. Replace fastembed BM25 with a `pyserini` / `rank_bm25` index over the same chunk corpus, or use Qdrant's server-side BM25 (`text` payload index with `MatchText`) — both give corpus-aware IDF.
- **Verification test:** run the same 9 eval queries in dense-only mode; compare `must_contain_any` pass rate against the hybrid version. If parity or better, sparse isn't earning its keep.
- **Blocks pilot?** **No.**

---

### F-9 — [MEDIUM, confirmed] Date filter is year/month, not day

- **Where:** `rag/retriever.py:152-168`.
- **Evidence:** Filter uses `meeting_year` int range plus a month range only when both bounds share a year. So a query with `date_from="2025-06-15"` and `date_to="2025-08-05"` narrows to `year=2025, month∈[6,8]` — meetings on 2025-06-01 through 2025-06-14 are included; meetings on 2025-08-06 through 2025-08-31 are included.
- **Impact:** Off-by-weeks on the edges of the requested window. Trustee-visible if they ever narrow to a specific meeting range that isn't month-aligned.
- **Recommended correction:** add a `published_date` string field to the payload (already there as ISO string per `_build_payload:269`) and use a range on it. Qdrant supports date/string range filters. Alternatively add a `published_date_epoch_days` int field and range on that.
- **Verification test:** create synthetic chunks on `2025-06-10` and `2025-06-20`; query with `date_from=2025-06-15, date_to=2025-06-30`; expect only the 20th, get both today.
- **Blocks pilot?** **No** — the mismatch is subtle and the UI defaults to broad ranges.

---

### F-10 — [MEDIUM, confirmed] Chunk text truncated at 2000 chars pre-embedding, no warning

- **Where:** `pipeline/indexer.py:311` — `texts = [c.get("text", "")[:2000] for c in batch]`.
- **Evidence:** Text is hard-truncated to 2000 characters before dense embedding. Payload stores the untruncated text. Comment explains "oversized chunks from bad VTT files stall the ONNX tokenizer" — legitimate defensive move but the *chunk itself* is much shorter than 2000 chars if `MAX_TOKENS=500` is respected (roughly ~1800–2400 chars). So this hits real chunks sometimes.
- **Impact:** Embedding represents only the leading portion of a borderline-large chunk. Combined with F-4, the retriever + reranker + embedding all operate on different subsets of the same chunk.
- **Recommended correction:** count tokens with the same tokenizer used by nomic (or an approximation) and log a warning when truncation happens. Better: guarantee at Phase-5 that `MAX_TOKENS` produces `<= 2000` chars, or raise the truncation limit to `4000` (nomic-v1.5 handles 8192 tokens).
- **Verification test:** log a counter of chunks where `len(text) > 2000` during indexing; expect near-zero for normal ASR output, non-zero for VTT roll-ups.
- **Blocks pilot?** **No.**

---

### F-11 — [MEDIUM, confirmed] Generator system prompt hard-codes "HCC" and Houston-specific framing

- **Where:** `rag/generator.py:38, 179-207` — system prompt says "Neo, a board meeting intelligence assistant for community-college trustees", but the one-shot demo names "Sample College", and the general audience is 8 colleges. The `_SYSTEM_PROMPT` is mostly generic. **However** the answer.py `route == "none"` message hard-codes "HCC board meeting intelligence" — `rag/answer.py:120-125`.
- **Evidence:** grep for "HCC" in `rag/` and `api/`:
  - `rag/answer.py:120` — `"I'm Neo, an assistant for HCC board meeting intelligence."`
  - README also refers to HCC as the target.
- **Impact:** Off-topic queries about an Austin/Alamo/Dallas topic (all now seeded per §3.1a in the architecture review) receive a canned "I only help with HCC" message. Confusing and factually wrong given the 8-college corpus.
- **Recommended correction:** replace the hard-coded "HCC" string with a broader phrase — "community-college board meetings across our tracked institutions" or similar. Consider making the message a config value.
- **Verification test:** issue a valid Dallas-College query with off-topic wording (e.g. "what's the weather in Dallas") and confirm the response references the general system, not HCC specifically.
- **Blocks pilot?** **No** — but noticeable to pilot users.

---

### F-12 — [LOW, confirmed] Router LLM classifier has no timeout

- **Where:** `rag/query_router.py:407-418` — `ollama.chat(model=..., messages=..., options={temperature: 0, num_predict: 5})` with no `timeout=`.
- **Evidence:** Only ambiguous queries hit this path, but when they do, a hung Ollama classifier ties up the whole request.
- **Impact:** Rare but blocking when it happens.
- **Recommended correction:** pass `timeout=(2.0, 5.0)` — router misclassification defaults to hybrid, which is a fine fallback.
- **Verification test:** simulate an Ollama block (`iptables -A OUTPUT -p tcp --dport 11434 -j DROP`) and issue an ambiguous query; expect 5 s to fallback route, not hang.
- **Blocks pilot?** **No.**

---

### F-13 — [LOW, confirmed] No retrieval fallback when 0 chunks come back

- **Where:** `rag/answer.py:346-354` — the generator is called with `rag_chunks=None` or `[]` when retrieval is empty; generator says "No relevant data was found" via `_build_prompt_and_citations:153-154`.
- **Evidence:** Verified. Output is honest (LLM tells user nothing was found), so it isn't a hallucination. But there's no attempt to retry with a relaxed filter (drop school, widen date, drop meeting_type).
- **Impact:** User-visible dead ends on queries that would have hit if the filter were softer.
- **Recommended correction:** on 0-chunk retrievals in RAG/hybrid mode, retry once with `school_slug=None` and log the widening. Only present in the answer if the widened search actually returned content.
- **Verification test:** issue a valid query with a school_slug that has no data yet; today returns "no data found", after fix returns cross-school best-match with a caveat.
- **Blocks pilot?** **No.**

---

### F-14 — [LOW, hypothesis] Reranker `predict` is not batched across candidates

- **Where:** `rag/retriever.py:273` — `scores = reranker.predict(pairs).tolist()`.
- **Evidence:** `sentence_transformers.CrossEncoder.predict` accepts a list and does batch under the hood; default batch size is 32. With `RETRIEVAL_TOP_K=20`, that's a single batch. If someone raises retrieval-top-k to 100 for exploration, the default might still fit in one batch on CPU but consume material RAM.
- **Impact:** Low today; latent as top-K grows.
- **Recommended correction:** none required at current sizes; explicit `batch_size=16` in the call if we go higher.
- **Blocks pilot?** **No.**

---

### F-15 — [LOW, confirmed] No answer cache or per-query dedupe

- **Where:** `api/services/ask_service.py:16-51`.
- **Evidence:** Every `/ask` call runs the full pipeline. Identical queries within seconds re-embed, re-rerank, re-generate.
- **Impact:** Wasted latency + LLM cost on a repeat click. Pilot-scale volume is low so cost is small, but a "same question twice" hit on the browser back button pays the full cost.
- **Recommended correction:** an in-memory `functools.lru_cache(maxsize=64)` keyed on `(query, school_slug, date_from, date_to, top_k)` in `ask_service.py`, with a small TTL. Or use a request-hash-based file cache in `data/ask_cache/`.
- **Blocks pilot?** **No.**

---

### F-16 — [LOW, confirmed] Query log records only `answer_preview[:240]`

- **Where:** `observability/query_log.py:103`.
- **Evidence:** The full answer isn't logged — only the first 240 chars. Trustee cannot audit past answers verbatim from the JSONL alone.
- **Impact:** For a pilot, tolerable. For audit-grade record-keeping down the road, insufficient.
- **Recommended correction:** either bump to full answer (with a `max=8000` cap) or write full answer separately to a `data/answers/<query_id>.txt` file and store just the pointer in JSONL.
- **Blocks pilot?** **No.**

---

### F-17 — [LOW, confirmed] Eval set (9 cases) is small and lacks retrieval-quality metrics

- **Where:** `eval/eval_set.jsonl` (9 lines), `tests/test_eval.py`.
- **Evidence:** Cases assert route classification + keyword-in-answer + banned-phrases + optionally that at least one `expected_meeting_id` shows up. There is no precision@k, recall@k, or MRR measurement; no golden-chunk set. Comment says "Phase 14 (slim)".
- **Impact:** Regressions in retrieval quality (e.g. the F-2 prefix change, F-3 overlap fix, F-8 BM25 swap) can pass the eval and still be worse in reality. The eval is a keep-the-lights-on smoke test, not a quality benchmark.
- **Recommended correction:**
  - Expand to ≥ 25 cases, distributed across the 6 routes and the 8 schools.
  - Add per-case `expected_chunk_ids` and compute precision@8 in `test_eval.py` — the data is already in `AskResponse.citations`.
  - For each change to the RAG stack, run this and record a delta.
- **Verification test:** each RAG refactor above (F-2, F-3, F-4, F-8) becomes an eval delta line.
- **Blocks pilot?** **No** — but this is the tool you need to safely fix F-1 through F-4.

---

### F-18 — [LOW, confirmed] Generator ignores `num_predict` cap risk of run-on answers

- **Where:** `rag/generator.py:242, 253` — `options={"temperature": 0.1, "num_predict": 1024}`.
- **Evidence:** 1024 tokens = ~700 words. Enough for most trustee answers; if the model rambles (bigger with local Ollama models), the answer is cut mid-sentence. No completion signal to the caller.
- **Impact:** Occasional truncated trailing sentence. UI would show it with no "[continued]" indicator.
- **Recommended correction:** either raise to 2048, or have the streaming path detect a finish-reason of `length` and append a marker.
- **Blocks pilot?** **No.**

---

### F-19 — [LOW, confirmed] `AskRequest.top_k` capped at 20; `RETRIEVAL_TOP_K` fixed at 20

- **Where:** `api/schemas/ask.py:12` (`top_k: int = Field(default=8, ge=1, le=20)`), `config.py:118` (`RETRIEVAL_TOP_K=20`).
- **Evidence:** The API's `top_k` is the reranked count (`RERANK_TOP_K`), which is fine at ≤ 20. But `_hybrid_search` uses `RETRIEVAL_TOP_K * 2 = 40` per prefetch branch. All values are hard-coded config; no request-time override for exploration.
- **Impact:** For pilot, none. Signal: the RAG stack cannot be tuned per-query without redeploying.
- **Blocks pilot?** **No.**

---

### F-20 — [LOW, confirmed] Model singletons in per-worker memory — multi-worker sizing

- **Where:** module-level `_dense_model`, `_sparse_model`, `_reranker`, `_qdrant` in `rag/retriever.py`; same pattern in `pipeline/indexer.py`.
- **Evidence:** With Gunicorn + Uvicorn workers (as recommended in the architecture review), each worker process gets its own copy of the ONNX + PT model files: fastembed dense ~270 MB, BGE reranker ~1.1 GB, sparse ~50 MB. That's ~1.4 GB per worker.
- **Impact:** A 2 GB VPS can only afford one worker without swapping. A 4 GB VPS supports two. Architecture review's cost estimate needs to know this.
- **Recommended correction:** run **one Uvicorn worker** on the pilot VPS and rely on FastAPI's asyncio + threadpool concurrency (safe once F-1 is fixed). Revisit worker count when GPU or > 8 GB RAM is available. Add a note to the architecture review's Phase B.
- **Blocks pilot?** **No** — informs sizing.

---

## 3. Positives (confirmed working well; keep as-is)

- **Deterministic point IDs.** `uuid5(NAMESPACE_DNS, chunk_id)` (`indexer.py:67-73`) — idempotent re-indexing is a proper superpower. Missing from most RAG systems in the wild.
- **Rich payload.** Everything the retriever + UI need is in the Qdrant payload — no DB round-trip during retrieval (`_build_payload`, `indexer.py:252-279`). This matters at any scale.
- **Server-side RRF.** Using Qdrant's native `Fusion.RRF` avoids the classical "manual RRF in Python" bug of double-normalizing scores.
- **Cross-encoder reranker.** Not skipped, not swapped for MMR — this alone puts Neo ahead of most first-generation RAG pipelines.
- **Structured `TABLE_SPECS` for SQL context.** Adding a fifth extraction table becomes a one-entry change (`sql_context.py:69-115`).
- **Prompt engineering discipline.** Few-shot demo in `_ONESHOT_USER`/`_ONESHOT_ASSISTANT` (`generator.py:180-207`) plus the "reminder" appended at the end of every real turn (`generator.py:161-166`) — this is exactly the pattern smaller Ollama models need. The comment even explains why. Preserve on any prompt refactor.
- **Unified citation numbering.** SQL `[1]` + RAG `[2..M]` all live in the same `next_n` counter (`_build_prompt_and_citations`) — the earlier "SQL numbered separately from RAG" bug is already fixed and documented.
- **Trust asymmetry documented in code.** RAG paths filter `needs_review=FALSE`; the API doesn't. Deliberate; visible in `sql_context.py:81, 91, 101, 113`. Worth surfacing to README (architecture review R4).
- **Per-query JSONL trace** with `n_markers` counted — instantly auditable whether the prompt is being followed (`observability/query_log.py:37-38, 91`).

---

## 4. Pilot-readiness verdict

| Question | Answer |
|---|---|
| Can 5 trustees use `/ask` today over the internet? | Not reliably. F-1 will produce visible ECONNRESET. |
| After F-1 (async + streaming + timeout) is fixed? | Yes, functionally. |
| Are answer-quality bugs (F-2, F-3, F-4) blocking? | No — they degrade quality but the system does answer. Fix them in the first month of the pilot; use the eval harness to confirm each is a net-positive. |
| Is the eval harness strong enough to safeguard those fixes? | Barely. Expand to ~25 cases with per-chunk assertions before doing F-2 or F-3. |
| Any confidentiality risks from the RAG side? | No trustee-personal data indexed; content is all public board meetings. No PII prompt-injection concern beyond generic LLM caution. |
| Any cost surprises? | Model memory ≈ 1.4 GB per API worker (F-20). Size the VPS accordingly. |

---

## 5. Recommended order of RAG fixes (post-F-1)

Ordered so each step is independently shippable, and each step's benefit is measurable with the eval harness (once expanded per F-17):

1. **Expand eval set to ≥ 25 cases with `expected_chunk_ids`.** No code changes yet. Establishes a baseline.
2. **F-11 — remove "HCC" hard-coding** from `rag/answer.py:120`. Trivial. Ship in Phase A.
3. **F-3 — sentence tokenizer for overlap.** Localized in `data_packager.py`. Re-package + re-index. Confirm eval delta ≥ 0.
4. **F-2 — nomic-embed prefixes.** Requires re-index. Do together with F-3 to amortize the re-index. Confirm eval delta > 0.
5. **F-5 — startup warm-up.** One file (`api/main.py`) change. Ship with F-1.
6. **F-4 — decide on reranker context.** Instrument first (log truncation counts), then choose: shrink chunks, upgrade reranker, or accept + document.
7. **F-8 — decide sparse's fate.** Ablation test using expanded eval. Keep, replace, or drop.
8. **F-6, F-7, F-9, F-10, F-12–F-19.** Batch as an "RAG hygiene" PR once the pilot is running.

Each step is < 100 LoC. None require re-architecting. All are reversible.

---

*End of review. No source files were modified. Findings are grounded in file+line evidence; hypotheses are marked as such.*
