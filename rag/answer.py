"""
Neo v2 — Phase 10: RAG Answer Orchestrator

Main entry point for the full Neo pipeline:
  1. route_query()      → decide SQL / RAG / hybrid
  2. sql_context()      → query structured extraction tables (if sql or hybrid)
  3. retrieve()         → hybrid Qdrant retrieval + BGE rerank (if rag or hybrid)
  4. generate()         → Ollama qwen2.5:14b answer with inline citations

Public API:
    from rag.answer import ask

    # Non-streaming
    result = ask("What budget items did the board approve?",
                 school_slug="houston_city_college")
    print(result["answer"])

    # Streaming (for Streamlit / FastAPI SSE)
    for token in ask("Who was appointed?", stream=True):
        print(token, end="", flush=True)

Usage (smoke-test):
    uv run python rag/answer.py "What budget items did the board approve?"
    uv run python rag/answer.py "Who was appointed chancellor?" --school houston_city_college
    uv run python rag/answer.py "What concerns were raised about capital projects?" --stream
    uv run python rag/answer.py "What vendor contracts were approved?" --route hybrid
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Generator, Optional, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from rag.query_router import route_query, RouteDecision
from rag.sql_context   import build_sql_context
from rag.retriever     import retrieve
from rag.generator     import generate


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def ask(
    query:        str,
    school_slug:  Optional[str] = None,
    date_from:    Optional[str] = None,
    date_to:      Optional[str] = None,
    speaker:      Optional[str] = None,
    meeting_type: Optional[str] = None,
    top_k:        Optional[int] = None,
    stream:       bool = False,
    model:        Optional[str] = None,
    force_route:  Optional[str] = None,   # override router: "sql"|"rag"|"hybrid"
    verbose:      bool = False,
) -> dict:
    """
    Full Neo v2 RAG pipeline: route → context → generate.

    Args:
        query:        The trustee's question.
        school_slug:  Filter to one school (e.g. 'houston_city_college').
        date_from:    ISO date YYYY-MM-DD — earliest meeting to include.
        date_to:      ISO date YYYY-MM-DD — latest meeting to include.
        speaker:      Filter RAG results to a specific speaker.
        meeting_type: Filter RAG results by source_type ('vtt' | 'asr').
        top_k:        RAG chunks after reranking (default config.RERANK_TOP_K = 8).
        stream:       If True, the returned dict carries a ``stream`` key
                      instead of ``answer`` (see Returns below).
        model:        Ollama model override (default config.OLLAMA_MODEL).
        force_route:  Skip the router and use this route ('sql'|'rag'|'hybrid').
        verbose:      Print routing/retrieval details to stdout.

    Returns (stream=False):
        {
          "answer":       str,
          "citations":    list[dict],
          "route":        str,
          "model":        str,
          "decision":     RouteDecision,
          "rag_chunks":   list[dict] | None,
          "sql_records":  dict | None,
          "elapsed_sec":  float,
        }

    Returns (stream=True):
        Same shape as above, except ``answer`` is replaced by
        ``stream`` — a generator yielding text tokens. Metadata
        (route, citations, decision, elapsed_sec-at-start) is delivered
        up-front so callers can emit it before the first token arrives.
        Consumers must fully drain the generator to obtain the answer.
    """
    t0 = time.perf_counter()

    if top_k is None:
        top_k = config.RERANK_TOP_K

    # ── Step 1: Route ────────────────────────────────────────────────────────
    if force_route:
        from rag.query_router import _infer_tables
        decision: RouteDecision = {
            "route":     force_route,
            "tables":    _infer_tables(query) if force_route in ("sql", "hybrid") else [],
            "intent":    "manual override",
            "confident": True,
        }
    else:
        decision = route_query(query)

    route = decision["route"]

    if verbose:
        flag = "✓" if decision["confident"] else "~"
        print(f"[{flag}] Route: {route.upper()}  |  {decision['intent']}")
        if decision["tables"]:
            print(f"    SQL tables: {decision['tables']}")

    # ── Off-topic: return immediately without touching DB or Qdrant ──────────
    if route == "none":
        # P2-1 (per implementation plan): message now covers all tracked
        # institutions, not just HCC.
        _msg = (
            "I'm Neo, an assistant for community-college board meetings "
            "across our tracked institutions. "
            "I can answer questions about board votes, budget decisions, personnel actions, "
            "contracts, initiatives, and discussions from official board meeting transcripts. "
            "I'm not able to help with questions outside that scope. "
            "Try asking something like: \"What did the board vote on in the last meeting?\" "
            "or \"Who was recently appointed to an executive role?\""
        )
        base = {
            "citations":   [],
            "route":       "none",
            "model":       model or config.OLLAMA_MODEL,
            "decision":    decision,
            "rag_chunks":  None,
            "sql_records": None,
            "elapsed_sec": round(time.perf_counter() - t0, 2),
        }
        if stream:
            def _msg_gen():
                yield _msg
            return {**base, "stream": _msg_gen()}
        return {**base, "answer": _msg}

    # ── Latest-meeting: pin retrieval to one specific meeting ────────────────
    if route == "latest_meeting":
        from rag.meeting_lookup import get_latest_meeting

        # Resolve the target school: query mention > explicit filter > any
        target_school: Optional[str] = None
        mentioned = decision.get("schools") or []
        if mentioned:
            target_school = mentioned[0]
        elif school_slug:
            target_school = school_slug

        meeting = get_latest_meeting(target_school)

        if meeting is None:
            who = target_school.replace("_", " ").title() if target_school else "the tracked colleges"
            _msg = (
                f"I couldn't find any completed meetings for {who} yet. "
                "Once transcripts are processed this question will pick them up automatically."
            )
            base = {
                "citations":   [],
                "route":       "latest_meeting",
                "model":       model or config.OLLAMA_MODEL,
                "decision":    decision,
                "rag_chunks":  None,
                "sql_records": None,
                "elapsed_sec": round(time.perf_counter() - t0, 2),
            }
            if stream:
                def _m():
                    yield _msg
                return {**base, "stream": _m()}
            return {**base, "answer": _msg}

        meeting_date = str(meeting["published_date"])[:10]

        # Scope RAG strictly to this meeting.
        lm_chunks = retrieve(
            query,
            meeting_id=meeting["meeting_id"],
            top_k=top_k,
        )
        if verbose:
            print(f"    Latest meeting: {meeting['title']} ({meeting_date})")
            print(f"    RAG chunks (meeting-scoped): {len(lm_chunks)}")

        # Scope SQL to the same meeting via date pin + school.
        lm_sql_text, lm_sql_records = build_sql_context(
            tables=["votes", "financial_actions", "personnel_actions"],
            school_slug=meeting["school_slug"],
            date_from=meeting_date,
            date_to=meeting_date,
            query=query,
        )

        lm_extra = {
            "route":         "latest_meeting",
            "decision":      decision,
            "rag_chunks":    lm_chunks,
            "sql_records":   lm_sql_records,
            "meeting_id":    meeting["meeting_id"],
            "meeting_title": meeting.get("title"),
            "meeting_date":  meeting_date,
            "school_slug":   meeting["school_slug"],
            "school_name":   meeting.get("school_name"),
        }
        if stream:
            gen_result = generate(
                query=query, route="hybrid",
                sql_context=lm_sql_text,
                rag_chunks=lm_chunks,
                stream=True, model=model,
            )
            return {
                **lm_extra,
                "citations":   gen_result["citations"],
                "model":       gen_result["model"],
                "stream":      gen_result["stream"],
                "elapsed_sec": round(time.perf_counter() - t0, 2),
            }

        result = generate(
            query=query, route="hybrid",
            sql_context=lm_sql_text,
            rag_chunks=lm_chunks,
            stream=False, model=model,
        )
        elapsed = round(time.perf_counter() - t0, 2)
        return {
            **lm_extra,
            "answer":      result["answer"],
            "citations":   result["citations"],
            "model":       result["model"],
            "elapsed_sec": elapsed,
        }

    # ── Compare: run across multiple schools, then generate ─────────────────
    if route == "compare":
        compare_schools = decision.get("schools") or []  # empty = all schools
        if verbose:
            print(f"    Compare schools: {compare_schools or 'all'}")

        # Fetch SQL context per school (or combined if no specific schools)
        compare_sql_text = None
        compare_sql_records = {}
        if decision["tables"]:
            if compare_schools:
                parts = []
                for slug in compare_schools:
                    txt, recs = build_sql_context(
                        tables=decision["tables"],
                        school_slug=slug,
                        date_from=date_from,
                        date_to=date_to,
                        query=query,
                    )
                    parts.append(f"## {slug.replace('_', ' ').title()}\n{txt}")
                    for k, v in recs.items():
                        compare_sql_records.setdefault(k, []).extend(v)
                compare_sql_text = "\n\n".join(parts)
            else:
                compare_sql_text, compare_sql_records = build_sql_context(
                    tables=decision["tables"],
                    school_slug=None,
                    date_from=date_from,
                    date_to=date_to,
                    query=query,
                )

        # RAG: no school filter so results span all schools
        compare_chunks = retrieve(
            query,
            school_slug=None,
            date_from=date_from,
            date_to=date_to,
            top_k=top_k,
        )
        if verbose:
            print(f"    RAG chunks (cross-school): {len(compare_chunks)}")

        compare_extra = {
            "route":       "compare",
            "decision":    decision,
            "rag_chunks":  compare_chunks,
            "sql_records": compare_sql_records,
        }
        if stream:
            gen_result = generate(
                query=query, route="hybrid",
                sql_context=compare_sql_text,
                rag_chunks=compare_chunks,
                stream=True, model=model,
            )
            return {
                **compare_extra,
                "citations":   gen_result["citations"],
                "model":       gen_result["model"],
                "stream":      gen_result["stream"],
                "elapsed_sec": round(time.perf_counter() - t0, 2),
            }

        result = generate(
            query=query, route="hybrid",
            sql_context=compare_sql_text,
            rag_chunks=compare_chunks,
            stream=False, model=model,
        )
        elapsed = round(time.perf_counter() - t0, 2)
        return {
            **compare_extra,
            "answer":      result["answer"],
            "citations":   result["citations"],
            "model":       result["model"],
            "elapsed_sec": elapsed,
        }

    # ── Effective school filter for sql/rag/hybrid ───────────────────────────
    # Trustees usually phrase questions like "How much did HCC approve…?" with
    # the college name baked into the text rather than as a separate filter.
    # The router extracts those mentions into decision["schools"]. Use the
    # first (and only) one as a fallback ONLY when the caller didn't pass an
    # explicit school_slug and exactly one school was mentioned. Multi-school
    # mentions should already route to "compare" — leave them alone here.
    effective_school = school_slug
    mentioned = decision.get("schools") or []
    if effective_school is None and len(mentioned) == 1:
        effective_school = mentioned[0]
        if verbose:
            print(f"    Auto-detected school from query: {effective_school}")

    # ── Step 2: SQL context (if sql or hybrid) ───────────────────────────────
    sql_text    = None
    sql_records = None

    if route in ("sql", "hybrid") and decision["tables"]:
        sql_text, sql_records = build_sql_context(
            tables=decision["tables"],
            school_slug=effective_school,
            date_from=date_from,
            date_to=date_to,
            query=query,
        )
        if verbose:
            total_rows = sum(len(v) for v in sql_records.values())
            print(f"    SQL rows fetched: {total_rows}")

    # ── Step 3: RAG chunks (if rag or hybrid) ────────────────────────────────
    rag_chunks = None

    if route in ("rag", "hybrid"):
        rag_chunks = retrieve(
            query,
            school_slug=effective_school,
            date_from=date_from,
            date_to=date_to,
            speaker=speaker,
            meeting_type=meeting_type,
            top_k=top_k,
        )
        if verbose:
            print(f"    RAG chunks retrieved: {len(rag_chunks)}")

    # ── Step 4: Generate ─────────────────────────────────────────────────────
    common_extra = {
        "route":       route,
        "decision":    decision,
        "rag_chunks":  rag_chunks,
        "sql_records": sql_records,
    }

    if stream:
        gen_result = generate(
            query=query,
            route=route,
            sql_context=sql_text,
            rag_chunks=rag_chunks,
            stream=True,
            model=model,
        )
        return {
            **common_extra,
            "citations":   gen_result["citations"],
            "model":       gen_result["model"],
            "stream":      gen_result["stream"],
            "elapsed_sec": round(time.perf_counter() - t0, 2),
        }

    result = generate(
        query=query,
        route=route,
        sql_context=sql_text,
        rag_chunks=rag_chunks,
        stream=False,
        model=model,
    )

    elapsed = round(time.perf_counter() - t0, 2)

    return {
        **common_extra,
        "answer":      result["answer"],
        "citations":   result["citations"],
        "model":       result["model"],
        "elapsed_sec": elapsed,
    }


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Neo v2 Phase 10 — full RAG pipeline")
    parser.add_argument("query",        help="Question to answer")
    parser.add_argument("--school",     help="Filter by school slug", default=None)
    parser.add_argument("--date-from",  help="Start date YYYY-MM-DD", default=None)
    parser.add_argument("--date-to",    help="End date YYYY-MM-DD",   default=None)
    parser.add_argument("--top-k",      type=int, default=config.RERANK_TOP_K)
    parser.add_argument("--stream",     action="store_true")
    parser.add_argument("--route",      choices=["sql","rag","hybrid"], default=None,
                        help="Force a specific route (skip router)")
    parser.add_argument("--model",      default=config.OLLAMA_MODEL)
    parser.add_argument("--verbose",    action="store_true", default=True)
    args = parser.parse_args()

    print(f"\nQuery  : {args.query}")
    if args.school:
        print(f"School : {args.school}")
    print(f"Model  : {args.model}")
    print("=" * 70)

    if args.stream:
        print("── Answer (streaming) ──────────────────────────────────")
        stream_result = ask(
            args.query,
            school_slug=args.school,
            date_from=args.date_from,
            date_to=args.date_to,
            top_k=args.top_k,
            stream=True,
            model=args.model,
            force_route=args.route,
            verbose=args.verbose,
        )
        # Stream shape (new): dict with metadata + a "stream" generator.
        for token in stream_result["stream"]:
            print(token, end="", flush=True)
        print("\n")
        return

    result = ask(
        args.query,
        school_slug=args.school,
        date_from=args.date_from,
        date_to=args.date_to,
        top_k=args.top_k,
        stream=False,
        model=args.model,
        force_route=args.route,
        verbose=args.verbose,
    )

    print(f"\n[Route: {result['route'].upper()}  |  {result['decision']['intent']}]")

    if result["sql_records"]:
        total = sum(len(v) for v in result["sql_records"].values())
        print(f"SQL records : {total}")

    if result["rag_chunks"]:
        print(f"RAG chunks  : {len(result['rag_chunks'])}")
        print("\n── RAG context ─────────────────────────────────────────")
        for i, c in enumerate(result["rag_chunks"], 1):
            score = c.get("score", "—")
            score_str = f"{score:.4f}" if isinstance(score, float) else str(score)
            date  = (c.get("published_date") or "?")[:10]
            title = (c.get("title") or "Untitled")[:50]
            text  = (c.get("text") or "")[:120].replace("\n", " ")
            print(f"[{i}] {score_str}  {date}  {title}")
            print(f"     {text} …")

    print(f"\n── Answer ──────────────────────────────────────────────")
    print(result["answer"])
    print(f"\n── Done in {result['elapsed_sec']}s ────────────────────")


if __name__ == "__main__":
    main()
