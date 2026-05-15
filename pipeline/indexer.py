"""
Neo v2 — Phase 8: Qdrant Embedding & Hybrid Indexing

Reads approved meetings from PostgreSQL, embeds every chunk with two
vector types, and upserts to Qdrant:

  Dense  — nomic-embed-text via Ollama (768-dim cosine)
  Sparse — BM25 via fastembed (Qdrant/bm25) — keyword matching

Hybrid search (Phase 9) combines both vectors with Reciprocal Rank
Fusion for best-of-both retrieval on board meeting text.

Each Qdrant point payload carries full provenance:
  chunk_id, meeting_id, video_id, school_slug, published_date,
  speaker, start_time_sec, end_time_sec, token_count, quality_score,
  title, source_type, meeting_year, meeting_month

Status flow:
  approved → (indexer) → indexed

Usage:
    uv run python pipeline/indexer.py
    uv run python pipeline/indexer.py --school houston_city_college
    uv run python pipeline/indexer.py --limit 5
    uv run python pipeline/indexer.py --reindex   # re-process already-indexed meetings

Dependencies (add once):
    uv add qdrant-client fastembed

Setup (run once before first index):
    ollama pull nomic-embed-text
    docker run -d -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Meeting, PipelineRun, School
from pipeline.states import eligible_inputs

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DENSE_BATCH_SIZE  = 8    # chunks per Ollama embedding batch (sequential calls)
UPSERT_BATCH_SIZE = 100   # points per Qdrant upsert call

# Deterministic UUID namespace so re-indexing produces the same point IDs
_UUID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_DNS


def _chunk_uuid(chunk_id: str) -> str:
    """Stable point ID derived from chunk_id. Safe to re-upsert."""
    return str(uuid.uuid5(_UUID_NS, chunk_id))


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def _check_qdrant_client():
    """Import qdrant_client or give a clear error."""
    try:
        from qdrant_client import QdrantClient  # noqa: F401
        from qdrant_client.models import (     # noqa: F401
            Distance, VectorParams, SparseVectorParams, SparseIndexParams,
            PointStruct, SparseVector, NamedVector, NamedSparseVector,
        )
    except ImportError:
        print("❌  qdrant-client not installed.")
        print("    Fix:  uv add qdrant-client fastembed")
        sys.exit(1)


def _check_fastembed():
    """Import fastembed or give a clear error."""
    try:
        from fastembed import SparseTextEmbedding  # noqa: F401
    except ImportError:
        print("❌  fastembed not installed.")
        print("    Fix:  uv add qdrant-client fastembed")
        sys.exit(1)


def _check_fastembed_dense():
    """Verify fastembed TextEmbedding is importable (dense model)."""
    try:
        from fastembed import TextEmbedding  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Dense embedding via fastembed (nomic-ai/nomic-embed-text-v1.5)
# ---------------------------------------------------------------------------
#
# fastembed processes true GPU batches natively — ~50x faster than sequential
# Ollama calls.  The nomic-embed-text-v1.5 model produces 768-dim vectors,
# identical to Ollama's nomic-embed-text, so existing Qdrant collections are
# fully compatible.
#
# First run: fastembed auto-downloads the ONNX model (~270 MB) to
#   ~/.cache/fastembed/  (one-time, then cached).
# ---------------------------------------------------------------------------

_dense_model = None


def _get_dense_model():
    """Lazy-load the fastembed dense embedding model (cached after first call)."""
    global _dense_model
    if _dense_model is None:
        try:
            from fastembed import TextEmbedding
        except ImportError:
            print("❌  fastembed not installed.")
            print("    Fix:  uv add qdrant-client fastembed")
            sys.exit(1)
        print("    Loading nomic-embed-text-v1.5 via fastembed … ", end="", flush=True)
        _dense_model = TextEmbedding(
            model_name="nomic-ai/nomic-embed-text-v1.5",
            # Use ONNX GPU if available; falls back to CPU silently
        )
        print("ready")
    return _dense_model


def _embed_dense(texts: list[str]) -> list[list[float]]:
    """
    Embed a list of texts with nomic-embed-text-v1.5 via fastembed.

    fastembed processes the full batch in one GPU pass — typically
    ~0.02–0.05 s/chunk vs ~2 s/chunk with sequential Ollama calls.
    """
    model = _get_dense_model()
    # model.embed() returns a generator of numpy arrays
    return [vec.tolist() for vec in model.embed(texts, batch_size=DENSE_BATCH_SIZE)]


# ---------------------------------------------------------------------------
# Sparse embedding via fastembed BM25
# ---------------------------------------------------------------------------

_sparse_model = None


def _get_sparse_model():
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding
        _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _sparse_model


def _embed_sparse(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """
    Embed texts with BM25 sparse model.
    Returns list of (indices, values) tuples.
    """
    model = _get_sparse_model()
    results = []
    for sparse in model.embed(texts):
        idx = sparse.indices.tolist()
        val = sparse.values.tolist()
        results.append((idx, val))
    return results


# ---------------------------------------------------------------------------
# Qdrant collection setup
# ---------------------------------------------------------------------------

def _ensure_collection(client) -> None:
    """
    Create the Qdrant collection if it doesn't exist.
    Idempotent — safe to call on every run.

    Named vectors:
      'dense'  — 768-dim cosine (nomic-embed-text)
      'sparse' — BM25 (fastembed Qdrant/bm25)
    """
    from qdrant_client.models import (
        Distance, VectorParams, SparseVectorParams, SparseIndexParams,
    )

    existing = [c.name for c in client.get_collections().collections]
    if config.QDRANT_COLLECTION in existing:
        return

    client.create_collection(
        collection_name=config.QDRANT_COLLECTION,
        vectors_config={
            "dense": VectorParams(
                size=config.EMBED_DIM,
                distance=Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(
                index=SparseIndexParams(on_disk=False),
            ),
        },
    )
    print(f"  [Qdrant] Created collection '{config.QDRANT_COLLECTION}'")


# ---------------------------------------------------------------------------
# Per-meeting indexer
# ---------------------------------------------------------------------------

def _load_chunks(meeting: Meeting, school: School) -> list[dict]:
    """Load chunks.jsonl for a meeting. Returns list of chunk dicts."""
    jsonl_path = None
    if meeting.chunks_jsonl_path:
        jsonl_path = Path(meeting.chunks_jsonl_path)
    if not jsonl_path or not jsonl_path.exists():
        jsonl_path = (
            config.PROCESSED_DIR / school.slug / meeting.video_id / "chunks.jsonl"
        )
    if not jsonl_path.exists():
        raise FileNotFoundError(f"chunks.jsonl not found: {jsonl_path}")

    chunks = []
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


def _build_payload(
    chunk: dict,
    meeting: Meeting,
    school: School,
) -> dict[str, Any]:
    """
    Build the Qdrant point payload for a chunk.
    All fields included so Phase 9 retrieval can filter without a DB round-trip.
    """
    pub_date = meeting.published_date
    return {
        "chunk_id":       chunk.get("chunk_id", ""),
        "meeting_id":     meeting.meeting_id,
        "video_id":       meeting.video_id,
        "school_slug":    school.slug,
        "school_name":    school.name,
        "title":          meeting.title or "",
        "published_date": pub_date.isoformat() if pub_date else None,
        "meeting_year":   pub_date.year  if pub_date else None,
        "meeting_month":  pub_date.month if pub_date else None,
        "source_type":    meeting.source_type or "vtt",
        "speaker":        chunk.get("speaker"),
        "start_time_sec": chunk.get("start_time"),
        "end_time_sec":   chunk.get("end_time"),
        "token_count":    chunk.get("token_count"),
        "quality_score":  chunk.get("quality_score"),
        "text":           chunk.get("text", ""),   # stored for snippet display
    }


def index_meeting(
    meeting: Meeting,
    school: School,
    client,
    dry_run: bool = False,
) -> dict:
    """
    Embed and upsert all chunks for one meeting.
    Returns {'chunks': N, 'status': 'success'|'error: ...'}.
    """
    from qdrant_client.models import PointStruct, SparseVector

    started_at = datetime.now(timezone.utc)

    try:
        chunks = _load_chunks(meeting, school)
    except FileNotFoundError as e:
        return {"chunks": 0, "status": f"error: {e}"}

    if not chunks:
        return {"chunks": 0, "status": "error: empty chunks.jsonl"}

    total_upserted = 0

    # Process in dense-batch-sized windows
    for batch_start in range(0, len(chunks), DENSE_BATCH_SIZE):
        batch = chunks[batch_start : batch_start + DENSE_BATCH_SIZE]
        # Truncate to 2000 chars — nomic-embed handles up to ~8k tokens but
        # oversized chunks from bad VTT files stall the ONNX tokenizer.
        texts = [c.get("text", "")[:2000] for c in batch]
        log.info(
            "Embedding batch %s-%s of %s for %s",
            batch_start + 1,
            min(batch_start + DENSE_BATCH_SIZE, len(chunks)),
            len(chunks),
            meeting.video_id,
        )


        # ── Dense vectors ────────────────────────────────────────────────
        dense_vecs = _embed_dense(texts)

        # ── Sparse vectors ───────────────────────────────────────────────
        sparse_vecs = _embed_sparse(texts)

        # ── Build Qdrant points ──────────────────────────────────────────
        points = []
        for chunk, dense, (sp_idx, sp_val) in zip(batch, dense_vecs, sparse_vecs):
            point_id = _chunk_uuid(chunk.get("chunk_id", str(batch_start)))
            payload  = _build_payload(chunk, meeting, school)

            points.append(PointStruct(
                id      = point_id,
                vector  = {
                    "dense":  dense,
                    "sparse": SparseVector(indices=sp_idx, values=sp_val),
                },
                payload = payload,
            ))

        if not dry_run:
            # ── Upsert to Qdrant ─────────────────────────────────────────
            for upsert_start in range(0, len(points), UPSERT_BATCH_SIZE):
                upsert_batch = points[upsert_start : upsert_start + UPSERT_BATCH_SIZE]
                client.upsert(
                    collection_name=config.QDRANT_COLLECTION,
                    points=upsert_batch,
                )
            total_upserted += len(points)

        del texts, dense_vecs, sparse_vecs, points, batch
        gc.collect()
    

    return {"chunks": total_upserted if not dry_run else len(chunks), "status": "success"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Neo v2 Phase 8 — Qdrant Embedding & Hybrid Indexing"
    )
    parser.add_argument("--school",   help="Process only this school slug")
    parser.add_argument("--limit",    type=int, help="Process at most N meetings")
    parser.add_argument("--reindex",  action="store_true",
                        help="Re-index meetings already marked indexed")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Embed and build points but skip Qdrant upsert")
    args = parser.parse_args()

    # ── Dependency checks ────────────────────────────────────────────────
    _check_qdrant_client()
    _check_fastembed()

    print("Neo v2 — Phase 8: Qdrant Hybrid Indexing")
    print("=" * 55)
    print(f"Embed model  : {config.EMBED_MODEL} (dim={config.EMBED_DIM})")
    print(f"Sparse model : Qdrant/bm25 (fastembed)")
    print(f"Qdrant       : {config.QDRANT_HOST}:{config.QDRANT_PORT}")
    print(f"Collection   : {config.QDRANT_COLLECTION}")
    if args.dry_run:
        print("\n[DRY RUN — Qdrant upsert skipped]\n")

    if not _check_fastembed_dense():
        print("\n❌  fastembed TextEmbedding not available.")
        print("    Fix:  uv add qdrant-client fastembed")
        sys.exit(1)

    # ── Connect to Qdrant ────────────────────────────────────────────────
    try:
        from qdrant_client import QdrantClient

        if config.QDRANT_LOCAL_PATH:
            # Embedded mode — no Docker/server required.
            # All data persisted to a local directory.
            local_path = Path(config.QDRANT_LOCAL_PATH).resolve()
            local_path.mkdir(parents=True, exist_ok=True)
            client = QdrantClient(path=str(local_path))
            print(f"\n✅  Qdrant embedded mode — storage: {local_path}")
        else:
            # Remote server mode (Docker / managed Qdrant)
            client = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT)
            client.get_collections()   # connectivity check
            print(f"\n✅  Qdrant ready at {config.QDRANT_HOST}:{config.QDRANT_PORT}")
    except Exception as e:
        print(f"\n❌  Cannot connect to Qdrant: {e}")
        print()
        print("  Option A — embedded (no Docker, recommended for dev):")
        print("    Add to .env:  QDRANT_LOCAL_PATH=./data/qdrant_local")
        print()
        print("  Option B — Docker server:")
        print("    Install Docker Desktop: https://www.docker.com/products/docker-desktop")
        print("    Then: docker run -d -p 6333:6333 -v qdrant_storage:/qdrant/storage qdrant/qdrant")
        sys.exit(1)

    if not args.dry_run:
        _ensure_collection(client)

    print(f"✅  Ollama embed ready — {config.EMBED_MODEL}\n")

    # ── Query meetings ───────────────────────────────────────────────────
    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Eligibility from pipeline.states. The indexer accepts both
        # quality-gated states because either pipeline ordering is valid:
        #   packager → quality_gate → extractor → indexer  ('extracted')
        #   packager → extractor    → quality_gate → indexer  ('approved')
        # Either way the meeting has been through the gate AND extraction.
        eligible = list(eligible_inputs("indexer", rerun=args.reindex))

        query = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            .filter(Meeting.status.in_(eligible))
            .order_by(School.school_id, Meeting.published_date.desc())
        )
        if args.school:
            query = query.filter(School.slug == args.school)
        if args.limit:
            query = query.limit(args.limit)

        meetings = query.all()

        if not meetings:
            print("[INFO] No approved meetings to index.")
            print("       Run quality_gate.py first.")
            sys.exit(0)

        total = len(meetings)
        print(f"Meetings : {total}\n")

        total_chunks = total_errors = 0

        for i, (meeting, school) in enumerate(meetings, 1):
            title_short = (meeting.title or meeting.video_id)[:50]
            started_at  = datetime.now(timezone.utc)
            print(f"[{i:>3}/{total}] {school.slug[:20]:<20} | {title_short}")

            result = index_meeting(meeting, school, client, dry_run=args.dry_run)

            duration = (datetime.now(timezone.utc) - started_at).total_seconds()

            if result["status"] != "success":
                total_errors += 1
                print(f"          ❌ {result['status']}")
            else:
                total_chunks += result["chunks"]
                print(f"          ✅  {result['chunks']} chunks  ({duration:.0f}s)")
                if not args.dry_run:
                    meeting.status = "indexed"

            if not args.dry_run:
                session.add(PipelineRun(
                    meeting_id       = meeting.meeting_id,
                    channel_id       = None,
                    phase            = "indexing",
                    status           = "success" if result["status"] == "success" else "failed",
                    error_message    = None if result["status"] == "success" else result["status"],
                    duration_seconds = duration,
                    gpu_time_seconds = 0,
                    started_at       = started_at,
                    finished_at      = datetime.now(timezone.utc),
                    retry_count      = 0,
                ))

            if not args.dry_run:
                session.commit()
                if i % 5 == 0:
                    print(f"          [checkpoint {i}/{total}]")

            gc.collect()

        if not args.dry_run:
            session.commit()

    print("\n" + "=" * 55)
    print("Phase 8 complete:")
    print(f"  Meetings : {total}")
    print(f"  Chunks   : {total_chunks:,}")
    print(f"  Errors   : {total_errors}")
    print()
    if config.QDRANT_LOCAL_PATH:
        print("Verify (embedded mode):")
        print("  uv run python -c \"")
        print("  from qdrant_client import QdrantClient; import config")
        print("  from pathlib import Path")
        print("  c = QdrantClient(path=str(Path(config.QDRANT_LOCAL_PATH).resolve()))")
        print(f"  info = c.get_collection(config.QDRANT_COLLECTION)")
        print("  print(f'Points indexed: {info.points_count}')\"")
    else:
        print("Verify in Qdrant dashboard:")
        print(f"  http://{config.QDRANT_HOST}:6333/dashboard")
        print()
        print("  uv run python -c \"")
        print("  from qdrant_client import QdrantClient; import config")
        print("  c = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT)")
        print(f"  info = c.get_collection(config.QDRANT_COLLECTION)")
        print("  print(f'Points indexed: {info.points_count}')\"")

if __name__ == "__main__":
    main()        