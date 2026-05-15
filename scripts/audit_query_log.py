"""
Audit data/query_log.jsonl — quick health-check for the /ask pipeline.

Reports per-query: route, marker count, citation count, latency, and the
question. Sorts most-recent last so you can re-run after asking a question
and see your latest entry at the bottom.

Usage:
    uv run python scripts/audit_query_log.py
    uv run python scripts/audit_query_log.py --tail 5
    uv run python scripts/audit_query_log.py --no-markers     # only show queries with 0 markers
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parents[1] / "data" / "query_log.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=0,
                    help="Show only the last N entries (default: all).")
    ap.add_argument("--no-markers", action="store_true",
                    help="Filter to queries with zero citation markers (the failures).")
    ap.add_argument("--route", default=None,
                    help="Filter to one route (sql / rag / hybrid / latest_meeting / none / compare).")
    args = ap.parse_args()

    if not LOG_PATH.exists():
        print(f"No log file at {LOG_PATH}.", file=sys.stderr)
        return 1

    rows = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if args.route:
        rows = [r for r in rows if r.get("route") == args.route]
    if args.no_markers:
        rows = [r for r in rows if r.get("n_markers", 0) == 0]
    if args.tail:
        rows = rows[-args.tail:]

    print(f"{'route':<15} {'markers':>7} {'cits':>4} {'sec':>6}  query")
    print("-" * 80)
    for r in rows:
        route   = r.get("route") or "?"
        markers = r.get("n_markers", "-")
        cits    = r.get("n_citations", 0)
        sec     = r.get("elapsed_sec", 0)
        q       = (r.get("query") or "")[:60]
        print(f"{route:<15} {markers!s:>7} {cits!s:>4} {sec:>6}  {q}")

    if rows:
        n_zero = sum(1 for r in rows if r.get("n_markers", 0) == 0)
        print(f"\n{len(rows)} entries — {n_zero} with zero markers ({100 * n_zero // max(1, len(rows))}%).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
