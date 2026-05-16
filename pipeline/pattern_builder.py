"""
Neo v2 — Phase 6.5: Pattern Builder

Aggregates initiative rows into pattern_signals — cross-college intelligence
for trustee insights.  No LLM. Pure SQL aggregation + Python.

A pattern signal is created when the same category of initiative appears
across multiple schools and/or multiple meetings.

Evidence quality tiers:
  - confidence >= 0.8 and measured_outcome present  → strong signal
  - confidence >= 0.6 and claimed_outcome present   → moderate signal
  - confidence >= 0.4 or only observed_action       → weak signal

CRITICAL: Pattern signals never claim "best practices" unless at least
two different schools have measured_outcome populated.

Usage:
    uv run python pipeline/pattern_builder.py
    uv run python pipeline/pattern_builder.py --min-schools 2
    uv run python pipeline/pattern_builder.py --rebuild
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Date, cast, create_engine, delete, func, text
from sqlalchemy.orm import sessionmaker

import config
from database.models import Initiative, PatternSignal

EXTRACTOR_VERSION = "v2.5"


# ---------------------------------------------------------------------------
# Signal strength classification
# ---------------------------------------------------------------------------

def _signal_confidence(
    school_count: int,
    meeting_count: int,
    measured_count: int,
    avg_initiative_confidence: float,
) -> tuple[float, bool]:
    """
    Compute pattern signal confidence from aggregation metrics.
    Returns (confidence, needs_review).
    """
    # Base: proportion of schools showing this
    school_factor = min(1.0, school_count / 3.0)    # saturates at 3+ schools

    # Boost for measured outcomes (hardest evidence)
    measured_factor = min(1.0, measured_count / 2.0) # saturates at 2+ measured

    # Penalize if underlying initiative confidence is low
    conf_factor = avg_initiative_confidence

    confidence = round(
        school_factor * 0.4
        + measured_factor * 0.4
        + conf_factor * 0.2,
        3
    )
    confidence = max(0.0, min(1.0, confidence))

    # needs_review = True unless we have 2+ schools AND some measured evidence
    needs_review = not (school_count >= 2 and measured_count >= 1)

    return confidence, needs_review


# ---------------------------------------------------------------------------
# Aggregation queries
# ---------------------------------------------------------------------------

def _build_recurring_initiative_signals(session, min_schools: int) -> list[dict]:
    """
    Find initiative categories that appear in >= min_schools different schools.
    Returns list of signal dicts.
    """
    rows = (
        session.query(
            Initiative.category,
            func.count(func.distinct(Initiative.school_id)).label("school_count"),
            func.count(func.distinct(Initiative.meeting_id)).label("meeting_count"),
            func.count(Initiative.measured_outcome).label("measured_count"),
            func.avg(Initiative.confidence).label("avg_confidence"),
            func.min(cast(
                func.date(func.coalesce(
                    # Approximate from meeting published_date via join — use
                    # created_at as a safe fallback
                    Initiative.created_at
                )), Date
            )).label("first_date"),
            func.max(cast(
                func.date(Initiative.created_at), Date
            )).label("last_date"),
            func.array_agg(Initiative.initiative_id).label("initiative_ids"),
        )
        .filter(Initiative.needs_review == False)     # noqa: E712
        .filter(Initiative.confidence >= 0.5)
        .group_by(Initiative.category)
        .having(func.count(func.distinct(Initiative.school_id)) >= min_schools)
        .all()
    )

    signals = []
    for row in rows:
        conf, needs_review = _signal_confidence(
            row.school_count,
            row.meeting_count,
            row.measured_count or 0,
            float(row.avg_confidence or 0),
        )

        # Build a factual description — no overclaiming
        desc_parts = [
            f"{row.school_slug_list if hasattr(row, 'school_slug_list') else 'Multiple schools'} "
            f"({row.school_count} institution{'s' if row.school_count > 1 else ''}) "
            f"have addressed '{row.category.replace('_', ' ')}' initiatives "
            f"across {row.meeting_count} meeting{'s' if row.meeting_count > 1 else ''}."
        ]
        if row.measured_count and row.measured_count > 0:
            desc_parts.append(
                f"{row.measured_count} instance{'s' if row.measured_count > 1 else ''} "
                f"include measured outcomes."
            )
        else:
            desc_parts.append(
                "No measured outcomes recorded — evidence is observational only."
            )

        signals.append({
            "signal_type":              "recurring_initiative",
            "category":                 row.category,
            "description":              " ".join(desc_parts),
            "school_count":             row.school_count,
            "meeting_count":            row.meeting_count,
            "supporting_initiative_ids": list(row.initiative_ids or []),
            "confidence":               conf,
            "needs_review":             needs_review,
        })

    return signals


def _build_budget_trend_signals(session, min_schools: int) -> list[dict]:
    """
    Detect when the same financial category appears with approved actions
    across multiple schools in the same period.
    Uses financial_items table.
    """
    from database.models import FinancialItem

    rows = (
        session.query(
            FinancialItem.category,
            func.count(func.distinct(FinancialItem.school_id)).label("school_count"),
            func.count(FinancialItem.item_id).label("meeting_count"),
            func.avg(FinancialItem.confidence).label("avg_confidence"),
        )
        .filter(FinancialItem.action_type == "approved")
        .filter(FinancialItem.needs_review == False)    # noqa: E712
        .filter(FinancialItem.confidence >= 0.5)
        .group_by(FinancialItem.category)
        .having(func.count(func.distinct(FinancialItem.school_id)) >= min_schools)
        .all()
    )

    signals = []
    for row in rows:
        conf = round(
            min(1.0, row.school_count / 3.0) * 0.5
            + float(row.avg_confidence or 0) * 0.5,
            3
        )
        signals.append({
            "signal_type":              "budget_trend",
            "category":                 row.category,
            "description": (
                f"{row.school_count} school{'s' if row.school_count > 1 else ''} "
                f"approved {row.category.replace('_',' ')} expenditures "
                f"across {row.meeting_count} recorded actions."
            ),
            "school_count":             row.school_count,
            "meeting_count":            row.meeting_count,
            "supporting_initiative_ids": [],
            "confidence":               conf,
            "needs_review":             row.school_count < 2,
        })

    return signals


def _build_personnel_trend_signals(session, min_schools: int) -> list[dict]:
    """
    Detect recurring personnel action types across schools
    (e.g., widespread interim appointments may signal leadership instability).
    """
    from database.models import PersonnelAction

    rows = (
        session.query(
            PersonnelAction.action_type,
            func.count(func.distinct(PersonnelAction.school_id)).label("school_count"),
            func.count(PersonnelAction.action_id).label("action_count"),
            func.avg(PersonnelAction.confidence).label("avg_confidence"),
        )
        .filter(PersonnelAction.needs_review == False)     # noqa: E712
        .filter(PersonnelAction.confidence >= 0.5)
        .group_by(PersonnelAction.action_type)
        .having(func.count(func.distinct(PersonnelAction.school_id)) >= min_schools)
        .all()
    )

    signals = []
    for row in rows:
        conf = round(
            min(1.0, row.school_count / 3.0) * 0.5
            + float(row.avg_confidence or 0) * 0.5,
            3
        )
        signals.append({
            "signal_type":              "personnel_trend",
            "category":                 row.action_type or "other",
            "description": (
                f"{row.school_count} school{'s' if row.school_count > 1 else ''} "
                f"recorded '{row.action_type}' personnel actions "
                f"({row.action_count} total instances)."
            ),
            "school_count":             row.school_count,
            "meeting_count":            row.action_count,
            "supporting_initiative_ids": [],
            "confidence":               conf,
            "needs_review":             row.school_count < 2,
        })

    return signals


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_patterns(session, min_schools: int = 2, rebuild: bool = False) -> dict:
    """
    Run all signal builders and write results to pattern_signals table.
    Returns summary dict.
    """
    if rebuild:
        session.execute(delete(PatternSignal))
        session.flush()

    all_signals = []

    all_signals.extend(_build_recurring_initiative_signals(session, min_schools))
    all_signals.extend(_build_budget_trend_signals(session, min_schools))
    all_signals.extend(_build_personnel_trend_signals(session, min_schools))

    inserted = 0
    for sig in all_signals:
        session.add(PatternSignal(
            signal_type              = sig["signal_type"],
            category                 = sig["category"],
            description              = sig["description"],
            school_count             = sig["school_count"],
            meeting_count            = sig["meeting_count"],
            supporting_initiative_ids= sig.get("supporting_initiative_ids") or [],
            extractor_version        = EXTRACTOR_VERSION,
            confidence               = sig["confidence"],
            needs_review             = sig["needs_review"],
        ))
        inserted += 1

    session.commit()
    return {
        "recurring_initiatives": len([s for s in all_signals if s["signal_type"] == "recurring_initiative"]),
        "budget_trends":         len([s for s in all_signals if s["signal_type"] == "budget_trend"]),
        "personnel_trends":      len([s for s in all_signals if s["signal_type"] == "personnel_trend"]),
        "total":                 inserted,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Neo v2 Phase 6.5 — Pattern Builder"
    )
    parser.add_argument("--min-schools", type=int, default=2,
                        help="Minimum number of schools for a signal (default: 2)")
    parser.add_argument("--rebuild",     action="store_true",
                        help="Delete all existing signals and rebuild from scratch")
    args = parser.parse_args()

    print("Neo v2 — Phase 6.5: Pattern Builder")
    print("=" * 55)
    print(f"Min schools : {args.min_schools}")
    print(f"Rebuild     : {args.rebuild}")
    print()

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Sanity check: how many clean initiatives exist?
        total_initiatives = (
            session.query(func.count(Initiative.initiative_id))
            .filter(Initiative.needs_review == False)   # noqa: E712
            .scalar()
        )
        print(f"Clean initiatives available : {total_initiatives}")

        if total_initiatives < 5:
            print("\n⚠️  Very few validated initiatives found.")
            print("   Run initiative_extractor.py first, then review flagged rows.")
            if total_initiatives == 0:
                sys.exit(0)

        results = build_patterns(session, args.min_schools, args.rebuild)

    print("Signals created:")
    print(f"  Recurring initiatives : {results['recurring_initiatives']}")
    print(f"  Budget trends         : {results['budget_trends']}")
    print(f"  Personnel trends      : {results['personnel_trends']}")
    print(f"  Total                 : {results['total']}")
    print()
    print("Validate:")
    print('  psql -U postgres -d neo_v2 -c "SELECT signal_type, category, school_count, meeting_count, ROUND(confidence::numeric,2), needs_review FROM pattern_signals ORDER BY confidence DESC;"')
    print()
    print("⚠️  Remember: pattern_signals with needs_review=true require human")
    print("   review before surfacing to trustees as actionable insights.")


if __name__ == "__main__":
    main()
