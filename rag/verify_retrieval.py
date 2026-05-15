"""
Neo v2 — Phase 9 Verification: Retrieval Quality Check

Runs 5 representative trustee questions through the full pipeline and prints
side-by-side comparison of:
  A) Raw Qdrant RRF order  (no reranker)
  B) BGE cross-encoder reranked order

This lets you visually confirm that reranking surfaces more relevant chunks.

Usage:
    uv run python rag/verify_retrieval.py
    uv run python rag/verify_retrieval.py --school houston_city_college
    uv run python rag/verify_retrieval.py --top-k 5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.retriever import _hybrid_search, _rerank
import config

# ---------------------------------------------------------------------------
# 5 representative HCC trustee questions
# ---------------------------------------------------------------------------

TEST_QUESTIONS = [
    "What budget amendments or financial adjustments did the board approve?",
    "Who was hired, appointed, or promoted at the executive or administrative level?",
    "What construction or capital improvement projects were discussed or approved?",
    "Did the board vote on any contracts with outside vendors or contractors?",
    "What actions were taken regarding student tuition, fees, or financial aid?",
]


def _print_results(label: str, results: list[dict], score_key: str, n: int = 5) -> None:
    print(f"\n  ── {label} ──")
    for i, c in enumerate(results[:n], 1):
        score = c.get(score_key, 0)
        score_str = f"{score:.4f}" if isinstance(score, float) else str(score)
        date   = (c.get("published_date") or "?")[:10]
        title  = (c.get("title") or "Untitled")[:50]
        text   = (c.get("text") or "")[:150].replace("\n", " ")
        print(f"  [{i}] {score_str}  {date}  {title}")
        print(f"       {text} …")


def run_verification(
    school_slug: Optional[str] = None,
    top_k: int = 5,
    retrieval_top_k: int = 20,
) -> None:
    print("Neo v2 — Phase 9 Retrieval Verification")
    print("=" * 70)
    if school_slug:
        print(f"School filter : {school_slug}")
    print(f"Candidates    : {retrieval_top_k}  →  show top {top_k}")
    print()

    improvements = []

    for qi, question in enumerate(TEST_QUESTIONS, 1):
        print(f"\n{'='*70}")
        print(f"Q{qi}: {question}")

        # Stage 1 — fetch candidates once
        print("  Fetching … ", end="", flush=True)
        candidates = _hybrid_search(
            question,
            school_slug=school_slug,
            top_k=retrieval_top_k,
        )
        print(f"{len(candidates)} candidates")

        if not candidates:
            print("  ⚠️  No results — check Qdrant collection and school slug.")
            continue

        # Path A — raw Qdrant RRF order
        raw_order = sorted(candidates, key=lambda x: x.get("_qdrant_score", 0), reverse=True)

        # Path B — BGE reranked
        print("  Reranking … ", end="", flush=True)
        reranked = _rerank(question, candidates, top_k=top_k)
        print("done")

        _print_results("Raw Qdrant RRF (no reranker)", raw_order, "_qdrant_score", n=top_k)
        _print_results("BGE Reranked",                 reranked,  "score",         n=top_k)

        # Did the top-1 chunk change?
        raw_top    = (raw_order[0].get("chunk_id")  if raw_order else None)
        rerank_top = (reranked[0].get("chunk_id")   if reranked  else None)
        changed = raw_top != rerank_top
        improvements.append(changed)
        print(f"\n  Top-1 changed by reranker: {'YES ✅' if changed else 'no (same chunk)'}")

    # Summary
    print(f"\n{'='*70}")
    print(f"Verification complete — {sum(improvements)}/{len(improvements)} questions had top-1 reranked differently")
    print()
    if sum(improvements) >= 3:
        print("✅  Reranker is actively reordering results — pipeline looks healthy.")
    else:
        print("ℹ️  Reranker changed few top-1 results — Qdrant RRF alone may already be good,")
        print("    or the collection is small. Check individual results above for relevance.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Neo v2 Phase 9 — Retrieval verification")
    parser.add_argument("--school",  help="Filter by school slug", default=None)
    parser.add_argument("--top-k",   type=int, default=5, help="Results to display per question")
    parser.add_argument("--candidates", type=int, default=config.RETRIEVAL_TOP_K)
    args = parser.parse_args()

    run_verification(
        school_slug=args.school,
        top_k=args.top_k,
        retrieval_top_k=args.candidates,
    )


if __name__ == "__main__":
    main()
