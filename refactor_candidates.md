# Refactor Candidates — consolidated from 5 graph traces

Ranked by leverage (impact × scope of safety net the graph already provides). Each item is grounded in graph evidence verified against source.

**Resolution status (2026-05-09):** items #1–#8 all resolved or investigated. Remaining (#9, #10) are graphify-side, not in this codebase.

Source traces:
1. `PipelineRun` observability convergence
2. `extract_meeting()` LLM extraction fan-out
3. Pydantic ↔ ORM twin pairs
4. `Meeting.status` state machine reconstruction
5. End-to-end `Vote` (DB → ORM → query → service → schema → router → fetch → TS → page)
6. Pagination god-node myth-bust
7. RAG `ask()` orchestration mirror

---

## TIER 1 — Architectural ("worth a multi-day effort")

### 1. Unify the votes/financial/personnel/initiatives quadrology into one shape contract

**Status: PARTIAL (2026-05-09).** Realistic first-cut shipped: `rag/sql_context.py` collapsed 4 near-identical `_query_*` functions into one `_query_table()` driven by `_TABLE_SPECS`; `api/db/queries/_filters.py` extracted the `school + date_from + date_to` filter pattern, used by all 4 list/summary call sites in votes.py + financials.py. Full pipeline-extractor unification (`pipeline/extractor.py:extract_*`) deliberately not attempted — those share structure but not implementation (per-target prompts, validators, ORM models). That's the next slice when the appetite is there.


**Graph evidence:** Three independent semantic-similarity clusters at score 0.95:
- `pipeline/extractor.py`: `extract_votes ↔ extract_financial ↔ extract_personnel`
- `api/db/queries/`: `list_votes ↔ list_financials`, `get_votes_summary ↔ get_financials_summary`
- `rag/sql_context.py`: `_query_votes ↔ _query_financial ↔ _query_personnel`

The same 4-target taxonomy (votes / financial / personnel / initiatives) has three parallel implementations of "filter by school + date range, JOIN meetings+schools, return rows". A shared row-shape module (or a code generator from a single spec) would consolidate ~600 lines of near-duplicate SQL across `api/db/queries/votes.py`, `api/db/queries/financials.py`, `rag/sql_context.py`, and `pipeline/extractor.py`.

**Risk:** Touches the most active code paths. Don't bundle with anything else.
**Graph as safety net:** The 7+ semantic-similarity edges between sibling functions are the test surface — after refactor, those edges should remain in the graph or be replaced by a single `implements` edge from each caller to the shared abstraction.

---

### 2. Two-`Vote` problem in the type system

**Status: RESOLVED (2026-05-09).** `api/schemas/common.py` gained a `pick()` helper (Pydantic `Pick<T,K>`); `VoteSummary` and `FinancialSummary` in `api/schemas/meetings.py` are now derived projections of `VoteRow` / `FinancialRow`. TS side (`frontend/src/types/index.ts`) uses `Pick<Vote, ...>` / `Pick<Financial, ...>`. Drop a field on the row and import / typecheck fails fast. `tsc --noEmit` passes. `PersonnelSummary` left alone — no full `Personnel` row to project from.


**Graph evidence:** Two distinct shapes named `Vote*` exist for two API paths:
- `api/schemas/votes.py:VoteRow` (15 fields, for `/votes`) ↔ TS `Vote` (15 fields)
- `api/schemas/meetings.py:VoteSummary` (7 fields, nested in `MeetingOverview`) ↔ TS `VoteSummary` (7 fields)

Same entity, two shapes, no relationship between them in any type system. Shipping a new vote field means deciding (manually, every time) whether it goes in the list endpoint, the meeting overview, or both — and updating 4 files in lockstep.

**Fix shape:** Make `VoteSummary` a `Pick<>` of the full `Vote` (TypeScript) or a Pydantic subclass (Python). Single source of truth, projections derived.
**Risk:** Low — purely additive type plumbing.
**Graph as safety net:** The 5 INFERRED `semantically_similar_to` edges across api/schemas ↔ database/models are the contract map. After refactor, those edges become explicit `references` instead of inferred.

---

### 3. The diamond pipeline ordering is a load-bearing convention with no enforcement

**Status: RESOLVED (2026-05-09).** New `pipeline/states.py` is the single source of truth: 15 status constants, `INPUTS` / `RECHECK_INPUTS` per phase, `eligible_inputs(phase, rerun=...)` helper, plus `ALL_STATUSES`, `TERMINAL_FAILURES`, and `RECOVERY_TARGETS`. Wired into 5 phase files (extractor, quality_gate, indexer, data_packager, initiative_extractor) and 2 scripts (reset_status, recover_failed). Every eligibility tuple verified byte-equivalent to the prior literal list. `database/models.py` docstring now points at the executable spec. Adding a new status is a one-file change.


**Graph evidence:** The state machine reconstruction showed `quality_gate` and `extractor` are interchangeable in order:
- `extractor.py:1276-1278` accepts `["processed", "approved"]`
- `quality_gate.py:283` accepts `["processed", "extracted"]`
- `indexer.py:433` accepts `["approved", "extracted"]`

Three files contain the *same architectural decision* expressed as three independent literal lists. Changing the ordering requires synchronized edits with no compile-time check.

**Fix shape:** A single `pipeline/states.py` enum + transition table. Each phase imports its eligible-input set instead of redefining it. Adding `processing_failed` to the recovery list (see Tier 2 #5) becomes a 1-file change.
**Risk:** Touches every pipeline phase. Schedule for a quiet week.

---

## TIER 2 — Hardening ("a clear day's work each")

### 4. Inconsistent `from_attributes = True` on twin Pydantic schemas

**Status: RESOLVED (2026-05-09).** Convention chosen: every twin gets `model_config = {"from_attributes": True}`, even if dormant today. Applied to `VoteRow` and `FinancialRow`; `MeetingRow` migrated from old `class Config:` syntax to modern `model_config = {...}`. Convention documented inline in `MeetingRow`. All 5 twins (MeetingRow / VoteRow / FinancialRow / Pagination / School) now uniform.


**Graph evidence:** Five INFERRED twin pairs, mixed conventions:
- `MeetingRow ↔ Meeting` — has `from_attributes` ✓
- `Pagination` — has `from_attributes` ✓
- `School` — has `from_attributes` ✓
- `VoteRow ↔ Vote` — **does not** ✗ (uses manual `VoteRow(**dict)` unpack)
- `FinancialSummary`, `PersonnelSummary`, `InsightCell` — n/a (projections, not twins)

The list-endpoint flow uses raw SQL → dict → manual unpack. The Meeting flow uses ORM → ORM-mode. Two patterns serving the same purpose.

**Fix shape:** Pick one convention. Either add `from_attributes` to all twins, or change `MeetingRow` to manual unpack. Document the choice.
**Risk:** Low.

---

### 5. Three terminal-failure states are write-only dead ends

**Status: RESOLVED (2026-05-09).** New `scripts/recover_failed.py` resets meetings stuck in `asr_failed` → `needs_asr`, `processing_failed` → `transcribed`/`captioned` (routed by `source_type`), `rejected` → `processed`. Supports `--dry-run`, `--school`, `all`. `scripts/reset_status.py` `_KNOWN_STATUSES` extended (now sourced from `pipeline.states.ALL_STATUSES` per #3). The `_RECOVERY_PLAN` is also imported from `pipeline.states.RECOVERY_TARGETS`, so the dead-end → recovery routing is single-sourced.


**Graph evidence:** The state machine trace identified `asr_failed`, `processing_failed`, `rejected` as written but only `rejected` has a reader (via `quality_gate --recheck`). `asr_failed` and `processing_failed` accumulate forever with no recovery path.

**Fix shape:** Either (a) extend the existing `--recheck` flag pattern from `quality_gate` to `asr_processor` and `data_packager`, or (b) write `scripts/recover_failed.py` that resets these meetings to their phase's input status. There's already precedent: `scripts/reset_status.py` exists.
**Risk:** Low. New code, no existing logic to break.
**Graph as safety net:** After fix, the dead-end nodes should have new incoming edges (the recovery readers).

---

### 6. `/votes/summary` has no Pydantic `response_model`

**Status: RESOLVED (2026-05-09).** Audit of `/summary` endpoints surfaced the same gap on `/financials/summary` — fixed both. Added `VotesStats` (+ `TopMover`) to `api/schemas/votes.py` and `FinancialsStats` (+ `ActionTypeBucket`, `VendorBucket`, `LargestItem`) to `api/schemas/financials.py`, declared `response_model=` on each route. Field-for-field match to existing TS interfaces — no frontend changes needed.


**Graph evidence:** `api/routers/votes.py:31` — the `votes_summary()` endpoint returns the raw dict from `get_votes_summary()`. Its sibling `list_votes()` (line 13) declares `response_model=VoteListResponse`. Inconsistent guarantees on neighboring endpoints.

The frontend types it as `VotesStats` in `frontend/src/types/index.ts` — entirely manual, drifts silently.

**Fix shape:** Add `class VotesStats(BaseModel)` to `api/schemas/votes.py`, declare `response_model=VotesStats` on the route. Likely true for the other `/summary` endpoints too — audit them.
**Risk:** Low. Pure addition. Discovers latent bugs if any field types disagreed.

---

## TIER 3 — Hygiene ("an afternoon each")

### 7. Documented status vocabulary is missing two states

**Status: RESOLVED (2026-05-09).** `database/models.py:97` state-machine comment updated to list all 11 forward-progress states + the 3 terminal failures, plus a note about the `extracted`/`approved` diamond. After #3, the comment also points at `pipeline.states` as the executable spec, so future drift fails fast at import time rather than rotting silently in a docstring.


**Graph evidence:** `database/models.py:182` docstring lists 8 statuses; the actual code uses 11 (`asr_failed` and `processing_failed` are missing from the docstring). The state machine trace pulled all 11 from the source.

**Fix shape:** Update the comment. Trivial. Worth doing because that comment is the only "spec" of the state machine.

---

### 8. `extract_votes` has no INFERRED edge to a `Vote` ORM

**Status: INVESTIGATED — graphify miss confirmed (2026-05-09).**

**Graph evidence:** In the `extract_meeting` trace: `extract_personnel → PersonnelAction` and `extract_financial → FinancialItem` both got INFERRED edges. `extract_votes → Vote` did not. Either the LLM missed it, or `extract_votes` doesn't write directly.

**Verified behavior:** `extract_votes` DOES write to the `votes` table directly, symmetric with the other two:
- `pipeline/extractor.py:778` — `db_session.execute(delete(Vote).where(...))`
- `pipeline/extractor.py:852` — `db_session.add(Vote(...))` with the full field set

All three Phase-6 extractors follow the same delete-then-bulk-insert pattern.

**Conclusion:** Legitimate graphify miss, not a code asymmetry. `extract_votes` is the longest of the three (~110 lines), so the function header and the `Vote(...)` instantiation likely landed in different chunks during semantic extraction, breaking the cross-symbol inference. Reproducible — re-running graphify on this codebase will likely still miss the edge.

**Codebase action:** none — the source is correct.
**Upstream action:** worth filing against graphify when convenient.

---

### 9. Confidence-score = 0.5 INFERRED edges should be filtered

**Graph evidence:** The Pagination god-node turned out to have 11 hallucinated edges, all with `confidence_score = 0.5` — exactly the value graphify's own rubric forbids. The 0.5 is the LLM's "I don't actually know" signal.

**Fix shape (graphify-side, not this codebase):** Build-time filter that drops or downweights INFERRED edges with `confidence_score == 0.5`. Would have cut Pagination's degree from 16 to 5 and removed it from the god-node ranking. Worth filing upstream if graphify is used regularly.

**Action for this codebase:** When trusting the graph for design decisions, manually verify INFERRED edges with score = 0.5 (treat them as AMBIGUOUS).

---

## TIER 4 — Domain-aware graph extension (optional)

### 10. State-machine and PipelineRun.phase string literals are invisible to AST

The `meeting.status` state machine is invisible to AST and only partially captured by semantic extraction. A small project-specific extractor that grepped for `meeting.status = "<X>"` and `Meeting.status.in_([...])` and emitted those as graph edges would let the graph carry the full state machine as queryable structure (rather than a one-off reconstruction). Same idea for the `PipelineRun.phase=` string literals. This is the highest-value graphify customization for this codebase.

---

## Suggested order

If nothing else, do **#5** (failure-state dead ends) and **#7** (docstring fix) — under an hour combined, removes genuine operational friction. Then **#6** (response_model on summary endpoints) for safety. Save **#1, #2, #3** for a focused refactor week. **#10** is the meta-move — making graphify's future runs more useful for this specific codebase.

## The pattern across all traces

This codebase is **loose-coupled by convention, not by types**. Pipeline phases coordinate via shared status strings; API contracts replicate across SQL/Pydantic/TS; observability flows through `PipelineRun.phase` literals. The graph's role is making those conventions queryable. Most of the Tier 1–2 items above are about *making the convention explicit in code* so the type system can enforce what reviewers currently catch.

## Cross-cutting findings (not actionable, but useful framing)

- **`PipelineRun` is the observability god-node** — 9 of 9 pipeline phases write to it, no readers. The natural target for a metrics dashboard.
- **`Meeting.status` is the coordination god-node** — the forward dual of `PipelineRun`. Together they're the two sides of how this pipeline coordinates without phase-to-phase imports.
- **The RAG side filters `needs_review = FALSE`; the API does not.** Same data, different trust threshold per consumer. Real architectural choice, currently undocumented except inline.
- **Pagination is *not* a god node** — only 5 real edges (3 valid `uses` + 2 structural). The graph's #2 ranking was an LLM artifact.
