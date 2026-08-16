"""DB queries for insights endpoints — builds InsightMatrix and InsightDetail."""
from __future__ import annotations
import re
import hashlib
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

# ── Theme mapping ────────────────────────────────────────────────────────────
THEMES = {
    "T1": "Academic & Student Affairs",
    "T2": "Career Services & Workforce",
    "T3": "Budget & Policy",
    "T4": "Technology & Innovation",
    "T5": "Community & Recognition",
    "T6": "Personnel & Facilities",
    "T7": "Procedural Items",
}

# Keywords that map a category string to a theme
_THEME_KEYWORDS: list[tuple[str, list[str]]] = [
    ("T1", ["academic", "student", "curriculum", "course", "instruction", "enrollment",
            "graduation", "transfer", "tutoring", "advising", "faculty", "degree",
            "program", "accreditation", "learning"]),
    ("T2", ["career", "workforce", "job", "employer", "industry", "training", "apprentice",
            "internship", "placement", "cte", "technical education", "workforce development"]),
    ("T3", ["budget", "finance", "financial", "fiscal", "grant", "contract", "bond",
            "expenditure", "revenue", "tax", "audit", "procurement", "vendor", "policy",
            "fee", "tuition"]),
    ("T4", ["technology", "tech", "ai", "artificial intelligence", "digital", "software",
            "infrastructure", "data", "cybersecurity", "system", "platform", "tool",
            "innovation", "automation", "it ", "information technology"]),
    ("T5", ["community", "recognition", "partnership", "donation", "award", "ceremony",
            "event", "outreach", "diversity", "equity", "inclusion", "dei",
            "engagement", "alumni"]),
    ("T6", ["personnel", "staff", "hire", "appointment", "resignation", "facilities",
            "construction", "renovation", "building", "campus", "maintenance",
            "interim", "position", "employee", "human resources", "hr"]),
    ("T7", ["procedural", "minutes", "agenda", "quorum", "adjournment", "committee",
            "board meeting", "resolution", "consent", "approval", "motion",
            "compliance", "bylaw", "legal"]),
]


def _category_to_theme(category: str) -> str:
    """Map an initiative category string to the best-matching theme key."""
    low = category.lower()
    best_theme = "T7"
    best_score = 0
    for theme_key, keywords in _THEME_KEYWORDS:
        score = sum(1 for kw in keywords if kw in low)
        if score > best_score:
            best_score = score
            best_theme = theme_key
    return best_theme


def _make_slug(text: str) -> str:
    """Convert a string to a URL-safe slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:60]


def _make_insight_id(school_slug: str, theme_key: str, label: str) -> str:
    return f"{school_slug}__{theme_key}__{_make_slug(label)}"


# ── Fuzzy dedup ──────────────────────────────────────────────────────────────
# The slug-based dedup catches exact-rewrite duplicates ("AI AAS" from two
# meetings). It misses near-duplicates like "Aviation Program Launch" vs
# "Aviation Maintenance Program Launch" because the slugs differ. A token-set
# Jaccard pass catches those without needing embeddings.
_DEDUP_STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "in", "on", "to", "with", "at",
    "by", "from", "is", "are", "be", "new", "proposed",
}

def _dedup_tokens(label: str) -> set[str]:
    """Tokens for fuzzy comparison — lowercased words ≥ 2 chars, stopwords stripped."""
    toks = re.findall(r"[a-z0-9]+", (label or "").lower())
    return {t for t in toks if len(t) >= 2 and t not in _DEDUP_STOPWORDS}


def _is_fuzzy_dup(a_tokens: set[str], b_tokens: set[str], threshold: float = 0.5) -> bool:
    """Jaccard ≥ threshold AND at least 2 shared tokens (avoids false-positives
    where two short labels share 1 stopword)."""
    if not a_tokens or not b_tokens:
        return False
    inter = a_tokens & b_tokens
    if len(inter) < 2:
        return False
    union = a_tokens | b_tokens
    return len(inter) / len(union) >= threshold


def _fuzzy_merge_bucket(bucket: dict) -> list[dict]:
    """Collapse near-duplicate entries in a single (school × theme) bucket.

    Walks entries confidence-desc; higher-conf label wins as canonical and
    absorbs the meeting_ids of any fuzzy-matched lower-conf sibling.
    """
    entries = sorted(bucket.values(), key=lambda b: b["confidence"], reverse=True)
    merged: list[dict] = []
    merged_tokens: list[set[str]] = []
    for e in entries:
        toks = _dedup_tokens(e["label"])
        dup_idx = None
        for i, kept_toks in enumerate(merged_tokens):
            if _is_fuzzy_dup(toks, kept_toks):
                dup_idx = i
                break
        if dup_idx is None:
            merged.append(e)
            merged_tokens.append(toks)
        else:
            # Absorb meetings into the canonical (higher-conf) entry.
            merged[dup_idx]["meetings"] |= e["meetings"]
    return merged


# ── Matrix builder ────────────────────────────────────────────────────────────

def get_insight_matrix(db: Session) -> dict:
    """Build the cross-college insight matrix from the initiatives table."""
    # Fetch all approved schools
    schools = db.execute(text("""
        SELECT slug, name FROM schools ORDER BY name
    """)).fetchall()
    school_slugs  = [r.slug for r in schools]
    school_names  = {r.slug: r.name for r in schools}

    # Fetch all approved initiatives with meeting date
    rows = db.execute(text("""
        SELECT
            i.initiative_id, i.school_slug,
            i.initiative_name, i.category,
            i.action_type, i.confidence,
            i.meeting_id, m.published_date
        FROM initiatives i
        JOIN meetings m ON m.meeting_id = i.meeting_id
        WHERE i.needs_review = FALSE
        ORDER BY i.confidence DESC
    """)).fetchall()

    # Group by (school_slug, theme_key) → collect ALL initiatives, dedup by normalised
    # label so we don't stack "AI AAS" twice from two meetings. We keep the highest-
    # confidence row per label and roll the meeting_count up across duplicates.
    MAX_PER_CELL = 3
    # key → { label_slug → {…row, "meetings": set(meeting_ids)} }
    cell_buckets: dict[tuple[str, str], dict[str, dict]] = {}

    for r in rows:
        r = dict(r._mapping)
        theme_key = _category_to_theme(r["category"] or "")
        key = (r["school_slug"], theme_key)
        label = r["initiative_name"] or ""
        if not label:
            continue
        label_slug = _make_slug(label)

        bucket = cell_buckets.setdefault(key, {})
        existing = bucket.get(label_slug)
        if existing is None:
            bucket[label_slug] = {
                "label":       label,
                "action_type": r["action_type"] or "discussed",
                "confidence":  r["confidence"],
                "meetings":    {r["meeting_id"]},
            }
        else:
            existing["meetings"].add(r["meeting_id"])
            if r["confidence"] > existing["confidence"]:
                existing["confidence"] = r["confidence"]
                existing["action_type"] = r["action_type"] or existing["action_type"]
                existing["label"] = label  # prefer higher-conf phrasing

    # Build ThemeRow list with up to MAX_PER_CELL cells per (school × theme).
    theme_rows = []
    for theme_key, theme_label in THEMES.items():
        cells: dict[str, list[dict]] = {}
        for slug in school_slugs:
            bucket = cell_buckets.get((slug, theme_key), {})
            # Second-pass fuzzy merge so "Aviation Program Launch" and "Aviation
            # Maintenance Program Launch" don't both occupy top-3 slots.
            merged = _fuzzy_merge_bucket(bucket)
            # Rank: confidence, tie-break on meeting recurrence (privileges items
            # that appear across multiple meetings).
            ranked = sorted(
                merged,
                key=lambda b: (b["confidence"], len(b["meetings"])),
                reverse=True,
            )[:MAX_PER_CELL]

            cells[slug] = [
                {
                    "insight_id":    _make_insight_id(slug, theme_key, b["label"]),
                    "school_slug":   slug,
                    "school_name":   school_names.get(slug, slug),
                    "theme_key":     theme_key,
                    "theme_label":   theme_label,
                    "label":         b["label"],
                    "action_type":   b["action_type"],
                    "meeting_count": len(b["meetings"]),
                    "confidence":    round(b["confidence"], 3),
                    "has_detail":    True,
                }
                for b in ranked
            ]

        theme_rows.append({
            "theme_key":   theme_key,
            "theme_label": theme_label,
            "cells":       cells,
        })

    return {
        "school_slugs":  school_slugs,
        "school_names":  school_names,
        "themes":        theme_rows,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
    }


# ── Detail builder ────────────────────────────────────────────────────────────

def get_insight_detail(db: Session, insight_id: str) -> Optional[dict]:
    """
    Resolve insight_id → "{school_slug}__{theme_key}__{label_slug}"
    Find best matching initiative row, then build full detail.
    """
    parts = insight_id.split("__", 2)
    if len(parts) != 3:
        return None
    school_slug, theme_key, label_slug = parts

    if theme_key not in THEMES:
        return None

    # Fetch all initiatives for this school and map to theme
    rows = db.execute(text("""
        SELECT
            i.initiative_id, i.school_slug,
            i.initiative_name, i.category,
            i.description, i.observed_action, i.stated_rationale,
            i.claimed_outcome, i.measured_outcome,
            i.action_type, i.evidence_text,
            i.confidence, i.meeting_id,
            m.title AS meeting_title, m.published_date,
            s.name AS school_name
        FROM initiatives i
        JOIN meetings m ON m.meeting_id = i.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        WHERE i.school_slug = :slug AND i.needs_review = FALSE
        ORDER BY i.confidence DESC
    """), {"slug": school_slug}).fetchall()

    # Find the initiative that matches the label_slug
    matched = None
    for r in rows:
        r = dict(r._mapping)
        t_key = _category_to_theme(r["category"] or "")
        if t_key != theme_key:
            continue
        if _make_slug(r["initiative_name"]) == label_slug:
            matched = r
            break

    # Fallback: pick highest-confidence row for this theme
    if not matched:
        for r in rows:
            r = dict(r._mapping)
            if _category_to_theme(r["category"] or "") == theme_key:
                matched = r
                break

    if not matched:
        return None

    label        = matched["initiative_name"]
    action_type  = matched["action_type"] or "discussed"
    school_name  = matched["school_name"]
    theme_label  = THEMES[theme_key]

    # All meetings containing initiatives in this theme/school
    theme_rows = [
        dict(r._mapping) for r in rows
        if _category_to_theme((dict(r._mapping).get("category") or "")) == theme_key
    ]
    supporting_meetings = []
    seen_meetings: set[int] = set()
    for r in theme_rows:
        mid = r["meeting_id"]
        if mid not in seen_meetings:
            seen_meetings.add(mid)
            supporting_meetings.append({
                "meeting_id": mid,
                "title":      r.get("meeting_title"),
                "date":       str(r["published_date"]) if r.get("published_date") else None,
                "school_slug": school_slug,
            })

    # Build summary from evidence fields
    parts_summary = []
    if matched.get("observed_action"):
        parts_summary.append(matched["observed_action"])
    if matched.get("stated_rationale"):
        parts_summary.append(f"Rationale: {matched['stated_rationale']}")
    if matched.get("claimed_outcome"):
        parts_summary.append(f"Expected outcome: {matched['claimed_outcome']}")
    if matched.get("measured_outcome"):
        parts_summary.append(f"Measured outcome: {matched['measured_outcome']}")
    summary = " ".join(parts_summary) or matched.get("description") or label

    why_it_appears = (
        f"This initiative appeared across {len(supporting_meetings)} board meeting(s) "
        f"under the theme '{theme_label}', with the board taking a '{action_type}' stance."
    )

    # ── Evidence ───────────────────────────────────────────────────────────
    # Prefer verified source-chunk provenance (extraction_evidence, migration
    # 0006): each row is a quotation located character-for-character in a named
    # chunk, so a reader can open the transcript at that timestamp and check it.
    # Rows extracted before 0006 have no evidence rows — fall back to the older
    # evidence_text snippet so those insights keep rendering.
    evidence = []
    ev_rows = db.execute(text("""
        SELECT chunk_id, exact_quote, supports, start_time_sec, end_time_sec
        FROM extraction_evidence
        WHERE initiative_id = :iid
        ORDER BY start_time_sec NULLS LAST, evidence_id
    """), {"iid": matched["initiative_id"]}).fetchall()

    for r in ev_rows:
        evidence.append({
            "text":          r.exact_quote,
            "speaker":       None,
            "timestamp_sec": r.start_time_sec,
            "meeting_title": matched.get("meeting_title"),
            "meeting_date":  str(matched["published_date"]) if matched.get("published_date") else None,
            "meeting_id":    matched["meeting_id"],
            "score":         matched["confidence"],
            "chunk_id":      r.chunk_id,
            "supports":      list(r.supports) if r.supports else [],
            "verified":      True,
        })

    if not evidence and matched.get("evidence_text"):
        evidence.append({
            "text":          matched["evidence_text"],
            "speaker":       None,
            "timestamp_sec": None,
            "meeting_title": matched.get("meeting_title"),
            "meeting_date":  str(matched["published_date"]) if matched.get("published_date") else None,
            "meeting_id":    matched["meeting_id"],
            "score":         matched["confidence"],
            "chunk_id":      None,
            "supports":      [],
            "verified":      False,
        })

    # Related votes from the same meetings
    meeting_ids = list(seen_meetings)
    related_votes = []
    if meeting_ids:
        placeholders = ", ".join(f":mid{i}" for i in range(len(meeting_ids)))
        vote_params  = {f"mid{i}": mid for i, mid in enumerate(meeting_ids)}
        vote_rows = db.execute(text(f"""
            SELECT vote_id, motion_text, vote_result_text,
                   yes_count, no_count, passed, unanimous
            FROM votes
            WHERE meeting_id IN ({placeholders}) AND needs_review = FALSE
            ORDER BY vote_id
            LIMIT 10
        """), vote_params).fetchall()
        related_votes = [dict(r._mapping) for r in vote_rows]

    # Related financials
    related_financials = []
    if meeting_ids:
        fin_rows = db.execute(text(f"""
            SELECT item_id, action_type, category, vendor, amount, description
            FROM financial_items
            WHERE meeting_id IN ({placeholders}) AND needs_review = FALSE
            ORDER BY amount DESC NULLS LAST
            LIMIT 10
        """), vote_params).fetchall()
        related_financials = [dict(r._mapping) for r in fin_rows]

    # Related personnel actions
    related_personnel = []
    if meeting_ids:
        per_rows = db.execute(text(f"""
            SELECT action_id, action_type, person_name, position, department
            FROM personnel_actions
            WHERE meeting_id IN ({placeholders}) AND needs_review = FALSE
            ORDER BY action_id
            LIMIT 10
        """), vote_params).fetchall()
        related_personnel = [dict(r._mapping) for r in per_rows]

    # Peer cells — same theme, other schools
    all_schools = db.execute(text("SELECT slug, name FROM schools ORDER BY name")).fetchall()
    peer_cells = []
    for s in all_schools:
        if s.slug == school_slug:
            continue
        peer_rows = db.execute(text("""
            SELECT initiative_name, action_type, category, confidence
            FROM initiatives i
            JOIN meetings m ON m.meeting_id = i.meeting_id
            WHERE i.school_slug = :sl AND i.needs_review = FALSE
            ORDER BY i.confidence DESC
        """), {"sl": s.slug}).fetchall()
        for pr in peer_rows:
            pr = dict(pr._mapping)
            if _category_to_theme(pr.get("category") or "") == theme_key:
                peer_cells.append({
                    "school_slug":  s.slug,
                    "school_name":  s.name,
                    "label":        pr["initiative_name"],
                    "action_type":  pr["action_type"] or "discussed",
                    "insight_id":   _make_insight_id(s.slug, theme_key, pr["initiative_name"]),
                })
                break  # one peer cell per school

    return {
        "insight_id":          insight_id,
        "school_slug":         school_slug,
        "school_name":         school_name,
        "theme_key":           theme_key,
        "theme_label":         theme_label,
        "label":               label,
        "action_type":         action_type,
        "summary":             summary,
        "why_it_appears":      why_it_appears,
        "confidence":          round(matched["confidence"], 3),
        "supporting_meetings": supporting_meetings,
        "evidence":            evidence,
        "related_votes":       related_votes,
        "related_financials":  related_financials,
        "related_personnel":   related_personnel,
        "peer_cells":          peer_cells,
    }
