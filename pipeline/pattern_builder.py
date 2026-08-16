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
two DIFFERENT schools have measured_outcome populated.  "Two measured
outcomes" is not the same as "two schools measured it" — one college
reporting eight numbers is a single institution's experience, not a
cross-college pattern.  See _signal_confidence.

Signals are a fully derived table: every run recomputes them from scratch.
There is no incremental mode, because a stale signal that no longer follows
from the underlying rows is worse than no signal.

Usage:
    uv run python pipeline/pattern_builder.py
    uv run python pipeline/pattern_builder.py --min-schools 2
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Date, cast, create_engine, delete, distinct, exists, func, text
from sqlalchemy.orm import sessionmaker

import config
from database.models import (
    ExtractionEvidence, FinancialItem, Initiative, Meeting, PatternSignal,
    PersonnelAction,
)

# v2.6 — signals are now built from evidence-backed initiatives, date ranges
# come from meeting dates rather than extraction time, and the two-school
# measured-outcome rule is actually enforced.  Bump whenever signal semantics
# change: a version that does not move makes old and new signals
# indistinguishable in the same table.
EXTRACTOR_VERSION = "v2.6"

# A signal may not be presented as trustee-ready unless measured outcomes come
# from at least this many DIFFERENT institutions.
MIN_MEASURED_SCHOOLS = 2

# How many school names to list before collapsing into "+N more".
_MAX_NAMED_SCHOOLS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _school_phrase(slugs: list[str]) -> str:
    """Render school slugs as readable prose: 'Alamo, Dallas and 3 more'."""
    names = [s.replace("_", " ").title() for s in sorted(set(slugs or []))]
    if not names:
        return "Multiple schools"
    if len(names) <= _MAX_NAMED_SCHOOLS:
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " and " + names[-1]
    shown = ", ".join(names[:_MAX_NAMED_SCHOOLS])
    return f"{shown} and {len(names) - _MAX_NAMED_SCHOOLS} more"


def _plural(n: int, word: str, suffix: str = "s") -> str:
    return f"{n} {word}{'' if n == 1 else suffix}"


# ---------------------------------------------------------------------------
# Signal strength classification
# ---------------------------------------------------------------------------

def _signal_confidence(
    school_count: int,
    meeting_count: int,
    measured_school_count: int,
    avg_initiative_confidence: float,
) -> tuple[float, bool]:
    """
    Compute pattern signal confidence from aggregation metrics.
    Returns (confidence, needs_review).

    `measured_school_count` is the number of DISTINCT schools with a measured
    outcome, not the number of measured outcomes.  This is the difference
    between "five colleges saw this work" and "one college said so five times".
    """
    # Base: proportion of schools showing this
    school_factor = min(1.0, school_count / 3.0)    # saturates at 3+ schools

    # Boost for measured outcomes from independent institutions (hardest evidence)
    measured_factor = min(1.0, measured_school_count / 2.0)  # saturates at 2+ schools

    # Penalize if underlying initiative confidence is low
    conf_factor = avg_initiative_confidence

    confidence = round(
        school_factor * 0.4
        + measured_factor * 0.4
        + conf_factor * 0.2,
        3
    )
    confidence = max(0.0, min(1.0, confidence))

    # Trustee-ready only with corroboration across institutions AND measured
    # outcomes from at least two of them.
    needs_review = not (
        school_count >= 2 and measured_school_count >= MIN_MEASURED_SCHOOLS
    )

    return confidence, needs_review


# ---------------------------------------------------------------------------
# Aggregation queries
# ---------------------------------------------------------------------------

def _build_recurring_initiative_signals(session, min_schools: int) -> list[dict]:
    """
    Find initiative categories that appear in >= min_schools different schools.
    Only initiatives carrying verified source-chunk evidence are counted, so a
    signal can always be traced back to words someone actually said.
    """
    has_evidence = exists().where(
        ExtractionEvidence.initiative_id == Initiative.initiative_id
    )

    rows = (
        session.query(
            Initiative.category,
            func.count(distinct(Initiative.school_id)).label("school_count"),
            func.count(distinct(Initiative.meeting_id)).label("meeting_count"),
            func.count(Initiative.initiative_id).label("initiative_count"),
            func.count(Initiative.measured_outcome).label("measured_count"),
            func.count(distinct(Initiative.school_id))
                .filter(Initiative.measured_outcome.isnot(None))
                .label("measured_school_count"),
            func.avg(Initiative.confidence).label("avg_confidence"),
            func.min(cast(Meeting.published_date, Date)).label("first_date"),
            func.max(cast(Meeting.published_date, Date)).label("last_date"),
            func.array_agg(distinct(Initiative.school_slug)).label("school_slugs"),
            func.array_agg(distinct(Initiative.initiative_id)).label("initiative_ids"),
        )
        .join(Meeting, Meeting.meeting_id == Initiative.meeting_id)
        .filter(Initiative.needs_review == False)     # noqa: E712
        .filter(Initiative.confidence >= 0.5)
        .filter(has_evidence)
        .group_by(Initiative.category)
        .having(func.count(distinct(Initiative.school_id)) >= min_schools)
        .all()
    )

    signals = []
    for row in rows:
        measured_schools = row.measured_school_count or 0
        conf, needs_review = _signal_confidence(
            row.school_count,
            row.meeting_count,
            measured_schools,
            float(row.avg_confidence or 0),
        )

        # Build a factual description — no overclaiming
        desc_parts = [
            f"{_school_phrase(row.school_slugs)} "
            f"({_plural(row.school_count, 'institution')}) "
            f"have addressed '{row.category.replace('_', ' ')}' initiatives "
            f"across {_plural(row.meeting_count, 'meeting')}."
        ]
        if measured_schools >= MIN_MEASURED_SCHOOLS:
            desc_parts.append(
                f"{_plural(row.measured_count or 0, 'instance')} across "
                f"{_plural(measured_schools, 'school')} include measured outcomes."
            )
        elif measured_schools == 1:
            # The distinction that keeps a single college's numbers from
            # reading as a cross-college result.
            desc_parts.append(
                f"{_plural(row.measured_count or 0, 'instance')} include measured "
                f"outcomes, but all from a single school — not corroborated "
                f"across institutions."
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
            "first_date":               row.first_date,
            "last_date":                row.last_date,
            "supporting_initiative_ids": list(row.initiative_ids or []),
            "confidence":               conf,
            "needs_review":             needs_review,
        })

    return signals


def _build_budget_trend_signals(session, min_schools: int) -> list[dict]:
    """
    Detect when the same financial category appears with approved actions
    across multiple schools.  Uses financial_items.
    """
    rows = (
        session.query(
            FinancialItem.category,
            func.count(distinct(FinancialItem.school_id)).label("school_count"),
            func.count(distinct(FinancialItem.meeting_id)).label("meeting_count"),
            func.count(FinancialItem.item_id).label("item_count"),
            func.avg(FinancialItem.confidence).label("avg_confidence"),
            func.min(cast(Meeting.published_date, Date)).label("first_date"),
            func.max(cast(Meeting.published_date, Date)).label("last_date"),
            func.array_agg(distinct(FinancialItem.school_slug)).label("school_slugs"),
        )
        .join(Meeting, Meeting.meeting_id == FinancialItem.meeting_id)
        .filter(FinancialItem.action_type == "approved")
        .filter(FinancialItem.needs_review == False)    # noqa: E712
        .filter(FinancialItem.confidence >= 0.5)
        .group_by(FinancialItem.category)
        .having(func.count(distinct(FinancialItem.school_id)) >= min_schools)
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
                f"{_school_phrase(row.school_slugs)} "
                f"({_plural(row.school_count, 'school')}) "
                f"approved {row.category.replace('_',' ')} expenditures — "
                f"{_plural(row.item_count, 'recorded action')} "
                f"across {_plural(row.meeting_count, 'meeting')}."
            ),
            "school_count":             row.school_count,
            "meeting_count":            row.meeting_count,
            "first_date":               row.first_date,
            "last_date":                row.last_date,
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
    rows = (
        session.query(
            PersonnelAction.action_type,
            func.count(distinct(PersonnelAction.school_id)).label("school_count"),
            func.count(distinct(PersonnelAction.meeting_id)).label("meeting_count"),
            func.count(PersonnelAction.action_id).label("action_count"),
            func.avg(PersonnelAction.confidence).label("avg_confidence"),
            func.min(cast(Meeting.published_date, Date)).label("first_date"),
            func.max(cast(Meeting.published_date, Date)).label("last_date"),
            func.array_agg(distinct(PersonnelAction.school_slug)).label("school_slugs"),
        )
        .join(Meeting, Meeting.meeting_id == PersonnelAction.meeting_id)
        .filter(PersonnelAction.needs_review == False)     # noqa: E712
        .filter(PersonnelAction.confidence >= 0.5)
        .group_by(PersonnelAction.action_type)
        .having(func.count(distinct(PersonnelAction.school_id)) >= min_schools)
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
                f"{_school_phrase(row.school_slugs)} "
                f"({_plural(row.school_count, 'school')}) "
                f"recorded '{row.action_type}' personnel actions — "
                f"{_plural(row.action_count, 'instance')} "
                f"across {_plural(row.meeting_count, 'meeting')}."
            ),
            "school_count":             row.school_count,
            "meeting_count":            row.meeting_count,
            "first_date":               row.first_date,
            "last_date":                row.last_date,
            "supporting_initiative_ids": [],
            "confidence":               conf,
            "needs_review":             row.school_count < 2,
        })

    return signals


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_patterns(session, min_schools: int = 2) -> dict:
    """
    Run all signal builders and write results to pattern_signals table.

    pattern_signals is fully derived, so every run clears it first.  Appending
    would leave signals that no longer follow from the current rows, with no
    way to tell them apart.

    Returns summary dict.
    """
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
            first_observed_date      = sig.get("first_date"),
            last_observed_date       = sig.get("last_date"),
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
        "trustee_ready":         len([s for s in all_signals if not s["needs_review"]]),
        "initiatives_corroborated": len([
            s for s in all_signals
            if s["signal_type"] == "recurring_initiative" and not s["needs_review"]
        ]),
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
    parser.add_argument("--rebuild", action="store_true",
                        help="Accepted for compatibility; every run is a full "
                             "rebuild because pattern_signals is derived")
    args = parser.parse_args()

    print("Neo v2 — Phase 6.5: Pattern Builder")
    print("=" * 55)
    print(f"Min schools : {args.min_schools}")
    print(f"Version     : {EXTRACTOR_VERSION}")
    print("Mode        : full rebuild (derived table)")
    print()

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Sanity check: how many clean, evidence-backed initiatives exist?
        total_initiatives = (
            session.query(func.count(Initiative.initiative_id))
            .filter(Initiative.needs_review == False)   # noqa: E712
            .scalar()
        )
        with_evidence = (
            session.query(func.count(Initiative.initiative_id))
            .filter(Initiative.needs_review == False)   # noqa: E712
            .filter(exists().where(
                ExtractionEvidence.initiative_id == Initiative.initiative_id))
            .scalar()
        )
        print(f"Clean initiatives available : {total_initiatives}")
        print(f"  ...with verified evidence : {with_evidence}")

        if with_evidence < total_initiatives:
            print(f"  ⚠️  {total_initiatives - with_evidence} clean initiatives have "
                  f"no verified evidence and are excluded from signals.")

        if total_initiatives < 5:
            print("\n⚠️  Very few validated initiatives found.")
            print("   Run initiative_extractor.py first, then review flagged rows.")
            if total_initiatives == 0:
                sys.exit(0)

        results = build_patterns(session, args.min_schools)

    print()
    print("Signals created:")
    print(f"  Recurring initiatives : {results['recurring_initiatives']}")
    print(f"  Budget trends         : {results['budget_trends']}")
    print(f"  Personnel trends      : {results['personnel_trends']}")
    print(f"  Total                 : {results['total']}")
    print(f"  Trustee-ready         : {results['trustee_ready']} of {results['total']} "
          f"(needs_review=false)")
    print(f"    of which initiatives: {results['initiatives_corroborated']} of "
          f"{results['recurring_initiatives']} — measured outcomes from "
          f"{MIN_MEASURED_SCHOOLS}+ schools")
    print("    budget/personnel signals are gated on school count only; they")
    print("    carry no measured-outcome requirement.")
    print()
    print("Validate:")
    print('  psql -U postgres -d neo_v2 -c "SELECT signal_type, category, school_count, meeting_count, ROUND(confidence::numeric,2), needs_review FROM pattern_signals ORDER BY confidence DESC;"')
    print()
    print("⚠️  Remember: pattern_signals with needs_review=true require human")
    print("   review before surfacing to trustees as actionable insights.")


if __name__ == "__main__":
    main()
