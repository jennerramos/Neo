# Eval set

33 cases in `eval_set.jsonl`, run by `tests/test_eval.py` against a live `/ask`.
This is implementation-plan **P2-0**, which the plan scheduled for the pilot's
first week and which turned out to be a prerequisite for judging anything —
see "Why this was rewritten" below.

```bash
# clean tabular output
API_URL=http://localhost:8000 uv run python tests/test_eval.py
uv run python tests/test_eval.py --verbose
uv run python tests/test_eval.py --filter ask-016

# one pytest per case
uv run pytest tests/test_eval.py -v
```

## Coverage

| route kind | cases | schools |
|---|---|---|
| sql | 8 | all 8, one each |
| rag | 9 | all 8 |
| hybrid | 4 | HCC, Central Texas, El Paso, Alamo |
| latest_meeting | 6 | HCC ×2, El Paso, Dallas, Lone Star, Mt. SAC |
| compare | 3 | cross-college |
| adversarial | 3 | n/a |

Every anchor was read out of the live database on 2026-08-16 —
`financial_items`, `initiatives`, `personnel_actions`, `votes`. No invented
figures. `expected_chunk_ids` come from those rows' own `chunk_ids` provenance
column, so they are ground truth about which chunk supports the claim.

## Baseline (2026-08-16, gemini-3.5-flash, torch reranker)

**31/33.** The measured run showed 30/33; ask-021 failed on a transient
`HTTP 503: Neo's AI provider is unavailable`, not on anything in the case.

| route kind | |
|---|---|
| sql | 8/8 |
| rag | 8/9 (the 503) |
| hybrid | 4/4 |
| latest_meeting | 6/6 |
| compare | 3/3 |
| **adversarial** | **1/3 — a real defect, see below** |

Expected-chunk recall: **75%** mean. Seven rag cases carry chunk ids; six were
scored in that run, since ask-021 hit the 503 before it could be measured.

### Read the recall number as a trend, not a target

75% is the baseline, and 100% is not the goal. The extractors' provenance often
lists two *overlapping* chunks for one claim, and retrieval reliably surfaces
one of the pair — ask-018, ask-019 and ask-020 each sit at 50% for exactly that
reason, with the retrieved chunk being a perfectly good source.

Scoring the fraction (rather than "any expected chunk hit") is deliberate: it
keeps the metric sensitive. A reranker or chunking change that starts dropping
one chunk of a pair moves 75% down visibly, where an any-hit metric would stay
at a comfortable 100% until the last chunk went too.

## The one standing defect

`ask-005` ("What's the weather in Houston today?") and `ask-033` ("What is the
capital of France?") both route to `hybrid` and **improvise an answer** instead
of refusing — ask-033 literally returns "Paris". `ask-032` (a cat poem) is
refused correctly, so the guard fires sometimes.

These are kept strict on purpose. Relaxing their expected route would make the
suite green and hide a real hole in the off-topic guard that `rag/answer.py`'s
`route == "none"` branch is supposed to close.

## Why this was rewritten

The previous 9-case set could not measure a retrieval change. Run twice against
an **unchanged** container it scored 5/9 then 6/9, flipping on ask-002: the
router LLM is nondeterministic and its noise was as large as any effect worth
measuring. Three concrete faults, all now fixed:

1. **A stale anchor.** `ask-009` expected "84" for El Paso's refunding bonds.
   The evidence-provenance re-extraction moved that figure to **$103,910,000**,
   so the case could never pass again.
2. **Case-sensitive keywords.** The harness lowercases the answer but did not
   lowercase `must_contain_any`, so `ask-003`'s `"Aviation"` was unmatchable.
   Keywords are now lowercased on load.
3. **Over-pinned routes.** "Summarize the last 2 meetings" is defensibly `rag`,
   `hybrid` or `latest_meeting`. `expected_route` now accepts a list, so the
   suite scores the answer rather than the coin toss.

Two further faults were in the *first draft of this expansion*, caught by
running it:

4. **`expected_meeting_ids` is unsatisfiable on sql/hybrid cases.** SQL
   citations carry no `meeting_id` (`api/schemas/ask.py` documents the field as
   RAG-only). The draft failed all 8 sql cases on it while every answer held
   the correct figure.
5. **`expected_chunk_ids` on a hybrid case measures routing, not retrieval.** A
   hybrid answer may legitimately cite SQL rows and no chunks, which reads as 0%
   recall when nothing about retrieval changed. The metric is now rag-only.

## Regenerating

`eval_set.jsonl` is generated, so the anchors can be re-derived when the corpus
changes rather than drifting silently as they did before. The generator carries
the provenance queries and the reasoning behind each structural rule.

## What this set still cannot do

Only 7 of the 33 cases carry `expected_chunk_ids`, so retrieval quality rests on
a narrow base. Before switching the reranker default (`RERANKER_BACKEND=onnx`)
or lowering `RETRIEVAL_TOP_K`, add chunk expectations to more rag cases — those
two changes are precisely the ones this metric exists to judge, and 7 cases is
thin ground to judge them on.
