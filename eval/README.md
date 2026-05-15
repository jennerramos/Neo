# Neo v2 — Eval Set

Slim version of Phase 14: a hand-curated set of trustee questions with
checkable expectations, plus a lightweight per-query trace log. The goal is
to answer **"did my prompt change make this better or worse?"** without
yet adopting Phoenix or full OpenTelemetry.

When the project later hits a debugging question we *can't* answer with
this setup ("why did this single query retrieve the wrong chunks?"), swap
`observability/query_log.py` for an OTel exporter — the call site doesn't
change.

---

## Files

| Path | What |
|---|---|
| `eval/eval_set.jsonl` | One JSON object per line — the test cases |
| `tests/test_eval.py`  | Runner — works as a CLI script *and* as pytest |
| `observability/query_log.py` | Per-query JSONL writer wired into `/ask` |
| `data/query_log.jsonl` | Auto-generated trace log (gitignored, real queries land here) |

---

## Running the eval

The backend (`uvicorn`) and Qdrant (Docker) must both be up — RAG and
hybrid cases need vector search. Start them per the dev README, then:

```powershell
# Clean tabular output, easier to skim while iterating
uv run python tests/test_eval.py
uv run python tests/test_eval.py --verbose
uv run python tests/test_eval.py --filter ask-001

# CI-friendly: one pytest test per case so failures isolate cleanly
uv run pytest tests/test_eval.py -v
```

Point at a different backend with `API_URL`:

```powershell
$env:API_URL = "http://staging.neo.local:8000"; uv run python tests/test_eval.py
```

---

## Case schema

Each line in `eval_set.jsonl` is one JSON object:

```json
{
  "id":               "ask-001",
  "route_kind":       "sql",
  "question":         "How much did HCC approve for the FY 2025-26 operating budget?",
  "school_slug":      null,
  "expected_route":   "sql",
  "must_contain_any": ["481", "$481", "481 million"],
  "must_not_contain": ["i don't have", "no data", "cannot find"],
  "expected_school_slug":  null,
  "expected_meeting_ids":  [],
  "notes": "Anchored on HCC FY2025-26 unrestricted operating budget $481M."
}
```

Fields:

- **id** — stable identifier; appears in pytest output.
- **route_kind** — informational tag (`sql`, `rag`, `hybrid`, `compare`,
  `latest_meeting`, `adversarial`). Not asserted.
- **question** — the trustee-style query sent to `/ask`.
- **school_slug** — *input* school filter (sent to `/ask` as `school_slug`
  param). Use when the question doesn't name the college explicitly.
- **expected_route** — the router must produce this. Use `"any"` to skip.
- **must_contain_any** — answer (lowercased) must contain at least *one*
  of these substrings. Use generous synonyms (`"481"`, `"$481"`,
  `"481 million"`) so paraphrase doesn't break the assertion.
- **must_not_contain** — answer must contain *none* of these. Best for
  catching refusal phrases ("I don't have data") leaking into answers
  that should have data.
- **expected_school_slug** — for `latest_meeting` route, the resolved
  meeting must belong to this school.
- **expected_meeting_ids** — at least one of these IDs must appear either
  as `meeting_id` (latest_meeting) or in any citation. Use when you have
  a stable ground-truth meeting; leave empty otherwise.
- **notes** — free-form. Why this case exists, what it's anchored on.

---

## Writing good cases

1. **Anchor on stable facts.** Use SQL the DB will reliably return —
   large round numbers, distinctive vendor names, the most-recent
   meeting. Avoid phrasing the question to depend on text that appears
   in only one chunk.
2. **Generous keyword sets.** The LLM paraphrases. `"$481M"` won't
   match `"481 million"`. Include both.
3. **Use `must_not_contain` for the refusal trap.** When a query *should*
   have an answer, banning `"I don't have"` catches the regression
   where the model bails out instead of citing the data.
4. **One assertion at a time when expanding.** Add a case, run it, fix
   anything that fails, *then* add the next. A growing red bar of vague
   failures is worse than a smaller green one.
5. **Mark adversarial cases clearly.** Off-topic / out-of-scope queries
   should set `expected_route` to `"none"` and assert refusal phrases.

---

## Per-query trace log

Every `/ask` call writes one JSONL line to `data/query_log.jsonl`:

```json
{
  "ts": "2026-04-29T18:42:00+00:00",
  "query": "How much did HCC approve for the operating budget?",
  "school_slug": null,
  "route": "sql",
  "elapsed_sec": 12.4,
  "answer_chars": 320,
  "answer_preview": "The fiscal year 2025-26 operating budget for HCC was approved at $481 million...",
  "meeting_id": null,
  "citations": [{"type": "sql", "meeting_id": 34, "score": null, "title": "..."}],
  "n_citations": 3
}
```

This is enough to answer iteration questions without Phoenix:

- "Which queries are slow?" → sort by `elapsed_sec`.
- "Which routes are getting picked?" → group by `route`.
- "Was retrieval involved?" → `n_citations > 0`.
- "Which meetings show up most in citations?" → flatten `citations[].meeting_id`.

Disable the writer with `NEO_QUERY_LOG=off`. Override the path with
`NEO_QUERY_LOG_PATH=/some/other/file.jsonl` (used by tests).

---

## Migrating to Phoenix later

When the iteration question becomes *"on this single query, why did the
reranker pick chunk X over chunk Y?"* — that's the signal to upgrade.
The migration is one file:

1. Install Arize Phoenix locally.
2. Replace `observability/query_log.py` with an OTel span exporter.
3. Keep the same field names (`route`, `elapsed_sec`, `n_citations`,
   etc.) as span attributes — the historical JSONL log still parses.

The `/ask` service code (`api/services/ask_service.py`) does not change.

---

## What this slim setup deliberately does *not* do

- ❌ Phoenix UI / Arize dashboard
- ❌ Full OpenTelemetry instrumentation across the pipeline
- ❌ Automated weekly eval runs (run it manually after prompt changes)
- ❌ Reranker-score visualization
- ❌ Answer-grounding scoring (citation precision/recall)

These are real Phase 14 deliverables, but the 80/20 is the eval set + the
trace log. Add them when an actual question forces the upgrade.
