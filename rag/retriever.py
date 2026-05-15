"""
Neo v2 — Phase 9: Hybrid Retrieval + Cross-Encoder Reranking

Two-stage retrieval pipeline:

  Stage 1 — Hybrid Qdrant query (dense + sparse → Reciprocal Rank Fusion)
    • Dense  : nomic-ai/nomic-embed-text-v1.5 (768-dim cosine) via fastembed
    • Sparse : Qdrant/bm25 via fastembed
    • Fusion : Qdrant's built-in RRF (score = Σ 1/(rank + k), k=60)
    • Returns RETRIEVAL_TOP_K candidates (default 20)

  Stage 2 — Cross-encoder reranking
    • Model  : BAAI/bge-reranker-v2-m3 via sentence-transformers
    • Input  : (query, chunk_text) pairs for all Stage 1 candidates
    • Output : Top RERANK_TOP_K chunks (default 8) by relevance score

Public API:
    from rag.retriever import retrieve

    chunks = retrieve(
        query="What did the board vote on regarding the budget?",
        school_slug="houston_city_college",   # optional filter
        top_k=8,
    )
    for c in chunks:
        print(c["score"], c["text"][:120])

Each returned chunk is a dict with keys:
    chunk_id, meeting_id, video_id, school_slug, title,
    published_date, speaker, start_time_sec, end_time_sec,
    token_count, quality_score, text, score (reranker score)

Usage (standalone smoke-test):
    uv run python rag/retriever.py "What was the budget discussion about?"
    uv run python rag/retriever.py "Who was appointed chancellor?" --school houston_city_college
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config

# ---------------------------------------------------------------------------
# Lazy-loaded model singletons (loaded once per process)
# ---------------------------------------------------------------------------

_dense_model  = None   # fastembed TextEmbedding
_sparse_model = None   # fastembed SparseTextEmbedding
_reranker     = None   # sentence_transformers CrossEncoder
_qdrant       = None   # QdrantClient


def _get_dense_model():
    global _dense_model
    if _dense_model is None:
        from fastembed import TextEmbedding
        _dense_model = TextEmbedding(model_name="nomic-ai/nomic-embed-text-v1.5")
    return _dense_model


def _get_sparse_model():
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding
        _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _sparse_model


def _get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(
            config.RERANKER_MODEL,
            max_length=512,
            device="cuda" if _cuda_available() else "cpu",
        )
    return _reranker


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from qdrant_client import QdrantClient
        if config.QDRANT_LOCAL_PATH:
            _qdrant = QdrantClient(path=str(config.QDRANT_LOCAL_PATH))
        else:
            _qdrant = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT)
    return _qdrant


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _embed_query_dense(text: str) -> list[float]:
    """Single-query dense embedding (768-dim)."""
    model = _get_dense_model()
    vectors = list(model.embed([text]))
    return vectors[0].tolist()


def _embed_query_sparse(text: str) -> tuple[list[int], list[float]]:
    """Single-query BM25 sparse embedding."""
    model = _get_sparse_model()
    results = list(model.embed([text]))
    sv = results[0]
    return sv.indices.tolist(), sv.values.tolist()


# ---------------------------------------------------------------------------
# Stage 1 — Hybrid Qdrant retrieval
# ---------------------------------------------------------------------------

def _build_filter(
    school_slug: Optional[str] = None,
    date_from:   Optional[str] = None,   # "YYYY-MM-DD"
    date_to:     Optional[str] = None,   # "YYYY-MM-DD"
    speaker:     Optional[str] = None,
    meeting_type: Optional[str] = None,  # source_type: "vtt" | "asr"
    meeting_id:  Optional[int] = None,   # exact meeting scope
):
    """
    Build a Qdrant Filter from optional metadata constraints.
    All conditions are ANDed (must).
    Returns None when no filters are active.
    """
    from qdrant_client.models import (
        Filter, FieldCondition, MatchValue, Range,
    )

    must = []

    if meeting_id is not None:
        must.append(FieldCondition(key="meeting_id", match=MatchValue(value=int(meeting_id))))

    if school_slug:
        must.append(FieldCondition(key="school_slug", match=MatchValue(value=school_slug)))

    if date_from or date_to:
        # meeting_year is stored as int — use it for year-level range filtering.
        # Parse year from "YYYY-MM-DD"; month-level filtering handled below.
        year_from = int(date_from[:4]) if date_from else 2000
        year_to   = int(date_to[:4])   if date_to   else 2100
        must.append(FieldCondition(
            key="meeting_year",
            range=Range(gte=year_from, lte=year_to),
        ))
        # Tighten with month when same year on both ends
        if date_from and date_to and year_from == year_to:
            month_from = int(date_from[5:7])
            month_to   = int(date_to[5:7])
            must.append(FieldCondition(
                key="meeting_month",
                range=Range(gte=month_from, lte=month_to),
            ))

    if speaker:
        # Case-insensitive substring match via Qdrant text index would be ideal,
        # but for now exact match (speaker names come from ASR, not standardised).
        must.append(FieldCondition(key="speaker", match=MatchValue(value=speaker)))

    if meeting_type:
        must.append(FieldCondition(key="source_type", match=MatchValue(value=meeting_type)))

    if not must:
        return None
    return Filter(must=must)


def _hybrid_search(
    query: str,
    school_slug:  Optional[str] = None,
    date_from:    Optional[str] = None,
    date_to:      Optional[str] = None,
    speaker:      Optional[str] = None,
    meeting_type: Optional[str] = None,
    meeting_id:   Optional[int] = None,
    top_k: int = 20,
) -> list[dict]:
    """
    Run a hybrid dense+sparse search via Qdrant's query_points API with RRF fusion.
    Returns up to top_k candidate dicts (payload + score).
    """
    from qdrant_client.models import (
        SparseVector,
        FusionQuery,
        Prefetch,
        Fusion,
    )

    client = _get_qdrant()

    dense_vec    = _embed_query_dense(query)
    s_idx, s_val = _embed_query_sparse(query)
    sparse_vec   = SparseVector(indices=s_idx, values=s_val)

    query_filter = _build_filter(
        school_slug=school_slug,
        date_from=date_from,
        date_to=date_to,
        speaker=speaker,
        meeting_type=meeting_type,
        meeting_id=meeting_id,
    )

    # Prefetch dense and sparse separately, then fuse with RRF.
    # In qdrant-client ≥1.9 pass the raw vector to `query`; `using` names which
    # vector field to search against.
    prefetch = [
        Prefetch(
            query=dense_vec,    # list[float] — searches the "dense" named vector
            using="dense",
            limit=top_k * 2,
            filter=query_filter,
        ),
        Prefetch(
            query=sparse_vec,   # SparseVector — searches the "sparse" named vector
            using="sparse",
            limit=top_k * 2,
            filter=query_filter,
        ),
    ]

    results = client.query_points(
        collection_name=config.QDRANT_COLLECTION,
        prefetch=prefetch,
        query=FusionQuery(fusion=Fusion.RRF),
        limit=top_k,
        with_payload=True,
    )

    candidates = []
    for hit in results.points:
        payload = hit.payload or {}
        payload["_qdrant_score"] = round(float(hit.score), 6)
        candidates.append(payload)

    return candidates


# ---------------------------------------------------------------------------
# Stage 2 — Cross-encoder reranking
# ---------------------------------------------------------------------------

def _rerank(
    query: str,
    candidates: list[dict],
    top_k: int = 8,
) -> list[dict]:
    """
    Score every (query, chunk_text) pair with the BGE cross-encoder.
    Returns top_k dicts sorted by descending reranker score.
    """
    if not candidates:
        return []

    reranker = _get_reranker()
    texts    = [c.get("text", "") for c in candidates]
    pairs    = [(query, t) for t in texts]
    scores   = reranker.predict(pairs).tolist()

    for c, s in zip(candidates, scores):
        c["score"] = round(float(s), 6)

    ranked = sorted(candidates, key=lambda x: x["score"], reverse=True)
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def retrieve(
    query: str,
    school_slug:  Optional[str] = None,
    date_from:    Optional[str] = None,
    date_to:      Optional[str] = None,
    speaker:      Optional[str] = None,
    meeting_type: Optional[str] = None,
    meeting_id:   Optional[int] = None,
    top_k:        Optional[int] = None,
    retrieval_top_k: Optional[int] = None,
    rerank: bool = True,
) -> list[dict]:
    """
    Full two-stage retrieval: hybrid Qdrant → cross-encoder rerank.

    Args:
        query:           Natural-language question from the trustee.
        school_slug:     Filter to one school slug (e.g. 'houston_city_college').
        date_from:       ISO date string 'YYYY-MM-DD' — earliest meeting date.
        date_to:         ISO date string 'YYYY-MM-DD' — latest meeting date.
        speaker:         Filter to a specific speaker name (exact match).
        meeting_type:    Filter by source_type: 'vtt' or 'asr'.
        top_k:           Chunks returned after reranking (default config.RERANK_TOP_K=8).
        retrieval_top_k: Qdrant candidates before reranking (default config.RETRIEVAL_TOP_K=20).
        rerank:          If False, skip cross-encoder and return raw Qdrant RRF order.

    Returns:
        List of chunk dicts sorted by relevance score (highest first).
        Each dict contains:
            chunk_id, meeting_id, video_id, school_slug, school_name,
            title, published_date, meeting_year, meeting_month,
            source_type, speaker, start_time_sec, end_time_sec,
            token_count, quality_score, text,
            score         (reranker score, or absent if rerank=False),
            _qdrant_score (RRF fusion score from Qdrant).
    """
    if top_k is None:
        top_k = config.RERANK_TOP_K
    if retrieval_top_k is None:
        retrieval_top_k = config.RETRIEVAL_TOP_K

    candidates = _hybrid_search(
        query,
        school_slug=school_slug,
        date_from=date_from,
        date_to=date_to,
        speaker=speaker,
        meeting_type=meeting_type,
        meeting_id=meeting_id,
        top_k=retrieval_top_k,
    )

    if not rerank:
        return sorted(candidates, key=lambda x: x.get("_qdrant_score", 0), reverse=True)[:top_k]

    return _rerank(query, candidates, top_k=top_k)


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Neo v2 Phase 9 — Retrieval smoke-test")
    parser.add_argument("query",         help="Question to search for")
    parser.add_argument("--school",      help="Filter by school slug", default=None)
    parser.add_argument("--date-from",   help="Start date YYYY-MM-DD", default=None)
    parser.add_argument("--date-to",     help="End date YYYY-MM-DD",   default=None)
    parser.add_argument("--speaker",     help="Filter by speaker name (exact)", default=None)
    parser.add_argument("--type",        help="Filter by source_type: vtt|asr",  default=None)
    parser.add_argument("--top-k",       type=int, default=config.RERANK_TOP_K)
    parser.add_argument("--candidates",  type=int, default=config.RETRIEVAL_TOP_K,
                        help="Qdrant candidates before rerank")
    parser.add_argument("--no-rerank",   action="store_true",
                        help="Skip reranking — show raw Qdrant RRF scores")
    args = parser.parse_args()

    print(f"\nQuery       : {args.query}")
    filters = {k: v for k, v in {
        "school": args.school, "date_from": args.date_from,
        "date_to": args.date_to, "speaker": args.speaker, "type": args.type,
    }.items() if v}
    if filters:
        print(f"Filters     : {filters}")
    print(f"Pipeline    : Qdrant top-{args.candidates} → {'raw RRF' if args.no_rerank else f'BGE rerank → top-{args.top_k}'}")
    print("=" * 70)

    # Stage 1
    print("Stage 1 — Hybrid Qdrant search … ", end="", flush=True)
    candidates = _hybrid_search(
        args.query,
        school_slug=args.school,
        date_from=args.date_from,
        date_to=args.date_to,
        speaker=args.speaker,
        meeting_type=args.type,
        top_k=args.candidates,
    )
    print(f"{len(candidates)} candidates")

    if args.no_rerank:
        results   = sorted(candidates, key=lambda x: x.get("_qdrant_score", 0), reverse=True)[:args.top_k]
        score_key = "_qdrant_score"
        score_label = "qdrant_rrf"
    else:
        print("Stage 2 — Cross-encoder rerank  … ", end="", flush=True)
        results   = _rerank(args.query, candidates, top_k=args.top_k)
        score_key = "score"
        score_label = "reranker"
        print(f"done  (top {len(results)})")

    print()
    for i, chunk in enumerate(results, 1):
        reranker_score = chunk.get("score", "—")
        qdrant_score   = chunk.get("_qdrant_score", "—")
        date   = chunk.get("published_date", "?")
        school = chunk.get("school_slug", "?")[:25]
        title  = (chunk.get("title") or "Untitled")[:55]
        spkr   = chunk.get("speaker") or "unknown"
        text   = (chunk.get("text") or "")[:220].replace("\n", " ")

        reranker_str = f"{reranker_score:.4f}" if isinstance(reranker_score, float) else reranker_score
        qdrant_str   = f"{qdrant_score:.4f}"   if isinstance(qdrant_score,   float) else qdrant_score

        print(f"[{i}] reranker={reranker_str}  qdrant_rrf={qdrant_str}  {date}  {school}")
        print(f"     {title}")
        print(f"     speaker: {spkr}")
        print(f"     {text} …")
        print()


if __name__ == "__main__":
    main()
