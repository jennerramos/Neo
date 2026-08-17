"""
Neo v2 — Phase 14 (slim): RAG eval harness.

Reads eval/eval_set.jsonl, hits the live /ask endpoint for each case, and
checks three things per case:

  1. The router classified into the expected route (sql/rag/hybrid/
     latest_meeting/none/...). If the expected_route is "any", skipped.
  2. The answer contains at least one keyword from must_contain_any
     (case-insensitive).
  3. The answer contains none of the strings in must_not_contain.

``expected_route`` may be a single route, a list of acceptable routes, or
"any". Several questions are defensibly more than one route, and scoring those
against a single answer measures router nondeterminism rather than quality.

Plus optional assertions:
  - expected_school_slug — for latest_meeting route, the AskResponse
    must resolve to that school.
  - expected_meeting_ids — at least one of these IDs must show up in
    citations or in the resolved meeting_id field.
  - expected_chunk_ids — the chunks that actually carry the evidence, taken
    from the extractors' `chunk_ids` provenance column. Reported as a recall
    percentage rather than asserted: it is the number to watch when changing
    anything upstream of the LLM (reranker, chunking, embeddings), because it
    moves independently of how the model happened to phrase the answer.

Usage:
    # Run as a standalone script (clean output, easier to iterate on):
    uv run python tests/test_eval.py
    uv run python tests/test_eval.py --verbose
    uv run python tests/test_eval.py --filter ask-001

    # Run as pytest (CI-friendly, one test per case):
    uv run pytest tests/test_eval.py -v

The /ask backend must be running at API_URL (default http://localhost:8000).
Set API_URL env var to point elsewhere.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_FILE = REPO_ROOT / "eval" / "eval_set.jsonl"
API_URL   = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
ASK_PATH  = "/ask"
# Generous — RAG + LLM call can be slow on first hit. Raise it when profiling a
# cloud reasoning model, so a slow provider reports a score instead of a wall of
# read-timeouts.
TIMEOUT_S = int(os.getenv("EVAL_TIMEOUT_S", "60"))


# ─────────────────────────────────────────────────────────────────────────────
# Case loading
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EvalCase:
    id: str
    question: str
    route_kind: str
    expected_routes: list[str]
    must_contain_any: list[str]
    must_not_contain: list[str]
    school_slug: str | None
    expected_school_slug: str | None
    expected_meeting_ids: list[int]
    expected_chunk_ids: list[str]
    notes: str

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> "EvalCase":
        # expected_route accepts a string or a list. Several questions are
        # defensibly more than one route -- "summarize the last 2 meetings" is
        # reasonably rag, hybrid or latest_meeting -- and pinning those to a
        # single answer made the suite fail on router nondeterminism rather
        # than on anything about answer quality. "any" means don't check.
        raw = row.get("expected_route")
        if raw is None or raw == "any":
            routes: list[str] = []
        elif isinstance(raw, str):
            routes = [raw]
        else:
            routes = list(raw)

        return cls(
            id=row["id"],
            question=row["question"],
            route_kind=row.get("route_kind", "unknown"),
            expected_routes=routes,
            # Keywords are lowercased here because they are matched against a
            # lowercased answer. Without this a capitalised keyword in the
            # JSONL can never match, and the case fails forever for a reason
            # that looks like a model problem.
            must_contain_any=[s.lower() for s in row.get("must_contain_any", [])],
            must_not_contain=[s.lower() for s in row.get("must_not_contain", [])],
            school_slug=row.get("school_slug"),
            expected_school_slug=row.get("expected_school_slug"),
            expected_meeting_ids=row.get("expected_meeting_ids") or [],
            expected_chunk_ids=row.get("expected_chunk_ids") or [],
            notes=row.get("notes", ""),
        )


def load_cases(path: Path = EVAL_FILE) -> list[EvalCase]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(EvalCase.from_json(json.loads(line)))
    return cases


# ─────────────────────────────────────────────────────────────────────────────
# Single-case runner
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    case: EvalCase
    ok: bool
    failures: list[str]
    response: dict[str, Any] | None
    elapsed_s: float
    # Fraction of the case's expected_chunk_ids that appeared in citations.
    # None when the case declares none.
    chunk_recall: float | None = None


def run_case(case: EvalCase) -> CaseResult:
    """Hit /ask, evaluate the response against the case's assertions."""
    payload: dict[str, Any] = {"query": case.question}
    if case.school_slug:
        payload["school_slug"] = case.school_slug

    failures: list[str] = []
    response: dict[str, Any] | None = None
    t0 = time.perf_counter()

    try:
        r = requests.post(f"{API_URL}{ASK_PATH}", json=payload, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        return CaseResult(case, False, [f"request failed: {e}"], None, 0.0)
    elapsed = time.perf_counter() - t0

    if not r.ok:
        return CaseResult(
            case, False,
            [f"HTTP {r.status_code}: {r.text[:200]}"],
            None, elapsed,
        )

    response = r.json()
    answer = (response.get("answer") or "").lower()
    route = response.get("route") or ""

    # 1. Route check — any of the accepted routes will do
    if case.expected_routes and route not in case.expected_routes:
        expected = " | ".join(case.expected_routes)
        failures.append(f"route mismatch: expected '{expected}', got '{route}'")

    # 2. must_contain_any — at least one keyword present
    if case.must_contain_any:
        if not any(kw in answer for kw in case.must_contain_any):
            failures.append(
                f"answer contained none of must_contain_any={case.must_contain_any!r}"
            )

    # 3. must_not_contain — none of the strings present
    bad = [kw for kw in case.must_not_contain if kw in answer]
    if bad:
        failures.append(f"answer contained banned phrases: {bad!r}")

    # 4. Optional: school slug for latest_meeting
    if case.expected_school_slug:
        got = response.get("school_slug")
        if got != case.expected_school_slug:
            failures.append(
                f"school_slug mismatch: expected '{case.expected_school_slug}', got '{got}'"
            )

    # 5. Optional: at least one expected meeting_id should show up.
    # Match against the resolved meeting_id (latest_meeting route) OR any
    # citation's meeting_id (rag/hybrid).
    if case.expected_meeting_ids:
        seen_ids: set[int] = set()
        if response.get("meeting_id") is not None:
            seen_ids.add(int(response["meeting_id"]))
        for c in response.get("citations") or []:
            mid = c.get("meeting_id")
            if mid is not None:
                seen_ids.add(int(mid))
        if not (set(case.expected_meeting_ids) & seen_ids):
            failures.append(
                f"none of expected_meeting_ids={case.expected_meeting_ids} "
                f"appeared in citations or resolved meeting (saw {sorted(seen_ids)})"
            )

    # 6. Optional: retrieval precision. expected_chunk_ids are the chunks that
    # actually carry the evidence — taken from the extractor's own `chunk_ids`
    # provenance column, so this is ground truth about which chunk supports the
    # claim, not a guess.
    #
    # Recorded rather than asserted. Retrieval quality is a trend to watch
    # across a change (a new reranker, new chunking, task prefixes), and a
    # single case dropping one expected chunk is not by itself a defect worth
    # failing a build over. The suite summary reports the aggregate.
    recall = None
    if case.expected_chunk_ids:
        got = [c.get("chunk_id") for c in (response.get("citations") or [])]
        got_set = {g for g in got if g}
        hit = set(case.expected_chunk_ids) & got_set
        recall = len(hit) / len(case.expected_chunk_ids)

    return CaseResult(case, not failures, failures, response, elapsed, recall)


# ─────────────────────────────────────────────────────────────────────────────
# Pytest entrypoint — one test per case so failures are isolated
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.id)
def test_eval_case(case: EvalCase) -> None:
    """Each eval case becomes its own test — pytest reports per-case pass/fail."""
    result = run_case(case)
    if not result.ok:
        msg = f"\n{case.id} ({case.route_kind}): {case.question}\n"
        msg += "\n".join(f"  - {f}" for f in result.failures)
        if result.response:
            msg += f"\n  route={result.response.get('route')!r}"
            msg += f"  answer={(result.response.get('answer') or '')[:200]!r}..."
        pytest.fail(msg)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entrypoint — clean tabular output for quick iteration
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv
    filter_id = None
    if "--filter" in argv:
        filter_id = argv[argv.index("--filter") + 1]

    cases = [c for c in load_cases() if filter_id is None or c.id == filter_id]
    if not cases:
        print(f"No cases match filter {filter_id!r}")
        return 1

    print(f"Neo eval — {len(cases)} cases against {API_URL}{ASK_PATH}\n")
    results: list[CaseResult] = []
    for case in cases:
        result = run_case(case)
        results.append(result)
        flag = "PASS" if result.ok else "FAIL"
        rec = "" if result.chunk_recall is None else f"  chunks {result.chunk_recall:.0%}"
        print(f"  [{flag}] {case.id:<10} {case.route_kind:<14} {case.question[:56]}{rec}")
        if not result.ok or verbose:
            for f in result.failures:
                print(f"           ! {f}")
            if verbose and result.response:
                print(f"           route={result.response.get('route')!r}")
                ans = (result.response.get("answer") or "")[:160].replace("\n", " ")
                print(f"           answer={ans!r}")
                print(f"           elapsed={result.elapsed_s:.1f}s")

    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed
    print(f"\n{passed} passed, {failed} failed")

    # Per-route-kind breakdown: an aggregate score hides which part regressed.
    kinds: dict[str, list[bool]] = {}
    for r in results:
        kinds.setdefault(r.case.route_kind, []).append(r.ok)
    print("\nby route kind:")
    for kind in sorted(kinds):
        oks = kinds[kind]
        print(f"  {kind:<15} {sum(oks)}/{len(oks)}")

    # Retrieval recall, reported separately from answer correctness. This is
    # the number to watch when changing anything upstream of the LLM — the
    # reranker, chunking, embeddings — because it moves independently of
    # whether the model phrased the answer the way a keyword list expects.
    scored = [r for r in results if r.chunk_recall is not None]
    if scored:
        mean = sum(r.chunk_recall for r in scored) / len(scored)
        full = sum(1 for r in scored if r.chunk_recall == 1.0)
        none_ = sum(1 for r in scored if r.chunk_recall == 0.0)
        print(f"\nexpected-chunk recall over {len(scored)} cases: "
              f"mean {mean:.0%}  (all {full}, none {none_})")
        for r in scored:
            if r.chunk_recall < 1.0:
                print(f"  {r.case.id:<10} {r.chunk_recall:.0%}  {r.case.expected_chunk_ids}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
