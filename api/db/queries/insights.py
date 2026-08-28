"""DB queries for insights endpoints — builds InsightMatrix and InsightDetail.

Two rules govern everything in this module, and both exist because the page
previously broke them:

1. An insight may only claim the meetings that actually produced it. The old
   detail builder attributed every meeting in the theme to every insight in
   it, so a one-meeting item announced "appeared across 48 board meeting(s)".
   `_SelectedInsight.meeting_ids` is now the single source for that count and
   is carried from the matrix into the detail unchanged.

2. Nothing is shown next to an insight unless it came from the same transcript
   passage. The old "Related Financials" sorted a whole theme's spending by
   amount, which parked a $1.048bn district budget beside a home-visit
   partnership. Related rows are now matched on chunk overlap.
"""
from __future__ import annotations
import calendar
import logging
import re
from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

log = logging.getLogger(__name__)

# ── Rolling window ───────────────────────────────────────────────────────────
#
# The page shows current board business only. Anything older is real history
# but it is not what a trustee is walking into a meeting to discuss, and
# undated older items read as present tense ("COVID-19 protocols · CONTINUED"
# from March 2021 was on the page before this filter existed).
#
# The window is measured on the date the board MET, not the date the recording
# was uploaded. `meetings.published_date` is the upload date and there is no
# meeting-date column, so the held date is resolved below.
INSIGHTS_WINDOW_MONTHS = 6


def _window_start(today: Optional[date] = None) -> date:
    """First day included in the rolling window."""
    today = today or date.today()
    month = today.month - INSIGHTS_WINDOW_MONTHS
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# ── When the board actually met ──────────────────────────────────────────────
#
# Anchoring the window on `published_date` was wrong in a way this corpus can
# demonstrate rather than merely risk. A channel that backfills puts months of
# old business on one recent upload date: Alamo published seven meetings two to
# five months after they were held, and Lone Star bulk-uploaded 41 meetings
# spanning ten months of board business on a single day. Every one of those
# would read as current board business under an upload-date filter.
#
# The title is the reliable source. pipeline/meeting_dates.py measured the
# title's year against `published_date` across the corpus and found the title
# right wherever it states one; day precision comes free with it, because these
# channels title recordings with the meeting date. Over the 375 indexed
# meetings: 331 resolve from the title, 43 fall back to the upload date, 1 is
# rejected as contradictory.
#
# That module resolves a year, not a date, and it lives in `pipeline/`, which
# the API image does not ship (see Dockerfile — only api, rag, llm, database
# and observability are copied). Hence a resolver here rather than an import.
_MONTHS = {name.lower(): n for n, name in enumerate(calendar.month_name) if n}
_MONTHS.update({name.lower(): n for n, name in enumerate(calendar.month_abbr) if n})

# "August 6, 2026", "Aug. 6 2026", "March 31st, 2026"
_TITLE_MDY = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(20\d{2})\b", re.I)
# "4/22/2026", "04-16-2024", "3-26-24"
_TITLE_NUMERIC = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2}|\d{2})\b")
# "2026-04-22"
_TITLE_ISO = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_TITLE_YEAR = re.compile(r"\b(20[0-3]\d)\b")


def _title_date(title: Optional[str]) -> Optional[date]:
    """A full calendar date stated in the meeting title, if there is one."""
    if not title:
        return None
    for rx in (_TITLE_ISO, _TITLE_MDY, _TITLE_NUMERIC):
        m = rx.search(title)
        if not m:
            continue
        if rx is _TITLE_ISO:
            year, month, day = int(m[1]), int(m[2]), int(m[3])
        elif rx is _TITLE_MDY:
            month = _MONTHS[m[1].lower()]
            day, year = int(m[2]), int(m[3])
        else:
            month, day, year = int(m[1]), int(m[2]), int(m[3])
            if year < 100:
                year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            continue    # e.g. "13/40/2026" — try the next shape
    return None


def resolve_held_date(title: Optional[str],
                      published_date: Optional[date]) -> tuple[Optional[date], str]:
    """The date the board met, and how it was established.

    Returns ``(held_date, source)`` where source is "title", "published" or a
    rejection reason. A None date means the meeting cannot be placed in time,
    and the page must not claim it is recent — unlike the pipeline, where
    pipeline/meeting_dates.py keeps undateable meetings because dropping one is
    irreversible. Here the cost is inverted: the meeting stays on the Meetings
    page either way, and showing it here would break the period this page
    states in its own header.
    """
    stated = _title_date(title)
    if stated is not None:
        if published_date and stated > published_date:
            # A recording cannot be uploaded before the meeting it records, so
            # one of the two is wrong and neither can be trusted for this row.
            return None, "after_upload"
        return stated, "title"

    year_only = _TITLE_YEAR.search(title or "")
    if year_only and published_date and int(year_only[1]) != published_date.year:
        # The title names a year the upload date contradicts — the backfill
        # signature. Without a day there is nothing to fall back to.
        return None, "year_conflict"

    if published_date is not None:
        return published_date, "published"
    return None, "undated"


class _EligibleMeeting:
    """A meeting inside the window, with the date it was actually held."""

    __slots__ = ("meeting_id", "school_slug", "held_date", "duration_seconds")

    def __init__(self, meeting_id: int, school_slug: str, held_date: date,
                 duration_seconds: Optional[int]):
        self.meeting_id       = meeting_id
        self.school_slug      = school_slug
        self.held_date        = held_date
        self.duration_seconds = duration_seconds


# ── One board session, however many times it was ingested ────────────────────
#
# The corpus contains the same session recorded twice. Alamo's 2026-05-12
# special meeting is in `meetings` as 639 and 640 — same school, same date,
# both 4,636 seconds, the titles differing only by a "Committiee"/"Committee"
# typo — and Lone Star's 2025-10-02 tax-rate hearing is there three times
# (45, 46, 49). Counted as recordings, two cells on the page announced "2
# meetings" for one afternoon of board business.
#
# Date and school are not enough on their own: 61 same-day pairs in this corpus
# are genuinely different meetings (a Special Meeting and a Regular Meeting on
# one evening, a Workshop and a Special Meeting). Duration to the second is
# what separates a second recording of one session from a second session, so a
# meeting with no duration is never collapsed.
#
# The copies are re-extractions of the same transcript, and the extractor is
# nondeterministic, so they hold overlapping-but-unequal initiative sets. The
# richest copy is kept whole and the others dropped, rather than the union
# being merged: a union would have to reconcile two sets of chunk ids against
# one session and would put near-identical quotes side by side under the same
# insight.
def _collapse_duplicate_recordings(
    db: Session, meetings: dict[int, _EligibleMeeting]
) -> dict[int, _EligibleMeeting]:
    """Keep one recording per board session."""
    clusters: dict[tuple, list[int]] = {}
    for m in meetings.values():
        if m.duration_seconds is None:
            continue    # cannot confirm it is the same session
        clusters.setdefault(
            (m.school_slug, m.held_date, m.duration_seconds), []).append(m.meeting_id)

    duplicated = {k: v for k, v in clusters.items() if len(v) > 1}
    if not duplicated:
        return meetings

    # Prefer the copy the extractor got the most out of, so dropping the others
    # costs as little as possible. meeting_id breaks ties, which keeps the
    # choice stable across requests — a cell's label becomes its URL.
    contested = sorted({mid for ids in duplicated.values() for mid in ids})
    ph, params = _bind_params("dup", contested)
    counts = dict(db.execute(text(f"""
        SELECT meeting_id, COUNT(*) AS n
        FROM initiatives
        WHERE meeting_id IN ({ph})
          AND needs_review = FALSE
        GROUP BY meeting_id
    """), params).fetchall())

    kept = dict(meetings)
    dropped = 0
    for key, ids in duplicated.items():
        canonical = max(ids, key=lambda mid: (counts.get(mid, 0), -mid))
        for mid in ids:
            if mid != canonical:
                del kept[mid]
                dropped += 1
        # Per cluster at DEBUG: this is a stable property of the corpus, and
        # the page resolves it on every request.
        log.debug("insights: %s on %s (%ss) ingested %d times; kept %s, dropped %s",
                  key[0], key[1], key[2], len(ids), canonical,
                  sorted(set(ids) - {canonical}))
    log.info("insights: %d duplicated session(s) in window; dropped %d extra recording(s)",
             len(duplicated), dropped)
    return kept


def eligible_meetings(db: Session, window_start: date,
                      window_end: date) -> dict[int, _EligibleMeeting]:
    """Every meeting the Insights page may draw on, keyed by meeting_id.

    Resolved once per request and threaded through both the matrix and the
    detail, so the two can never disagree about what is in the window.

    The SQL prefilter on `published_date` is safe rather than a second opinion:
    a recording cannot precede its meeting, so `held <= published`, and any
    meeting held on or after `window_start` must have been uploaded on or after
    it too. It only keeps the scan small.
    """
    rows = db.execute(text("""
        SELECT m.meeting_id, s.slug AS school_slug, m.title, m.published_date,
               m.duration_seconds
        FROM meetings m
        JOIN schools s ON s.school_id = m.school_id
        WHERE m.status = 'indexed'
          AND m.is_active
          AND m.published_date >= :window_start
    """), {"window_start": window_start}).fetchall()

    out: dict[int, _EligibleMeeting] = {}
    unplaced = 0
    for r in rows:
        held, source = resolve_held_date(r.title, r.published_date)
        if held is None:
            unplaced += 1
            log.debug("insights: meeting %s excluded (%s): %r",
                      r.meeting_id, source, r.title)
            continue
        if window_start <= held <= window_end:
            out[r.meeting_id] = _EligibleMeeting(
                r.meeting_id, r.school_slug, held, r.duration_seconds)

    if unplaced:
        log.info("insights: %d of %d candidate meetings could not be dated",
                 unplaced, len(rows))
    return _collapse_duplicate_recordings(db, out)


# Eligibility, written once so the matrix and the detail can never disagree.
# A cell that the matrix renders must resolve in the detail, and vice versa.
# The meeting set is resolved in Python by eligible_meetings() above, because
# the held date is not a column.
_ELIGIBLE = """
      i.needs_review = FALSE
  AND i.meeting_id = ANY(CAST(:eligible_ids AS int[]))
"""


# ── Themes ───────────────────────────────────────────────────────────────────
THEMES = {
    "T1": "Academic & Student Affairs",
    "T2": "Career Services & Workforce",
    "T3": "Budget & Policy",
    "T4": "Technology & Innovation",
    "T5": "Community & Partnerships",
    "T6": "Facilities",
    "T7": "Governance & Strategy",
    "T8": "Other Business",
}

# Explicit map from the extractor's fixed category vocabulary
# (pipeline/initiative_extractor.py::_INITIATIVE_CATEGORIES) to a theme row.
#
# This replaces a keyword-substring scorer. That scorer had no entry that
# matched "governance" or "other", so both fell through to a hard-coded
# default — which is how every college's multi-year strategic plan came to be
# filed under a row labelled "Procedural Items". It also scored
# "financial_sustainability" against the Technology keyword "ai" (via
# sust-AI-nability) and "career_technical_education" against "tech".
#
# A dict cannot drift from the vocabulary silently: _check_theme_map() below
# fails loudly when a category has no home.
_CATEGORY_THEME = {
    "student_success":            "T1",
    "dual_enrollment":            "T1",
    "curriculum":                 "T1",
    "transfer_pathway":           "T1",
    "accreditation":              "T1",
    "workforce_development":      "T2",
    "career_technical_education": "T2",
    "financial_sustainability":   "T3",
    "technology":                 "T4",
    "community_partnership":      "T5",
    "equity_and_inclusion":       "T5",
    "facilities":                 "T6",
    "governance":                 "T7",
    "other":                      "T8",
}

_UNKNOWN_THEME = "T8"


def _category_to_theme(category: Optional[str]) -> str:
    """Map an extractor category to its theme row."""
    key = (category or "").strip().lower()
    theme = _CATEGORY_THEME.get(key)
    if theme is None:
        # Not silently swallowed: an unmapped category means the extractor
        # vocabulary moved and this table needs an entry.
        log.warning("insights: category %r has no theme mapping", category)
        return _UNKNOWN_THEME
    return theme


def check_theme_map() -> list[str]:
    """Categories the extractor can emit that this module cannot place.

    Exposed for tests — the extractor owns the vocabulary, so a new category
    should fail a build rather than quietly land in "Other Business".
    """
    from pipeline.initiative_extractor import _INITIATIVE_CATEGORIES
    return sorted(set(_INITIATIVE_CATEGORIES) - set(_CATEGORY_THEME))


# ── Evidence levels ──────────────────────────────────────────────────────────
#
# What replaces the confidence percentage on screen. The old badge read
# "100% confidence", which a trustee reads as "we are certain this is true".
# It was 0.6 x (three fields populated) + 0.4 x (keyword density / 8): a
# measure of how completely the extractor filled a form, not of whether the
# claim holds. 31 of the items on the page carried it at 100%.
#
# These three states are each defensible from data the page already holds.
_BOARD_ACTIONS = frozenset({"approved", "launched"})

EVIDENCE_LEVELS = {
    "measured": (
        "Measured result reported",
        "The college reported a figure for this initiative in the meeting. "
        "Figures are self-reported by college staff.",
    ),
    "action": (
        "Board action recorded",
        "The transcript records the board approving or launching this, quoted below.",
    ),
    "discussion": (
        "Mentioned in discussion",
        "This was discussed or described in the meeting. No board action or "
        "measured result was recorded for it.",
    ),
}


def _evidence_level(has_measured: bool, action_type: Optional[str]) -> str:
    if has_measured:
        return "measured"
    if (action_type or "").lower() in _BOARD_ACTIONS:
        return "action"
    return "discussion"


# ── Slugs and ids ────────────────────────────────────────────────────────────

def _make_slug(value: str) -> str:
    """Convert a string to a URL-safe slug."""
    slug = re.sub(r"[^\w\s-]", "", (value or "").lower())
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
    """Tokens for fuzzy comparison — lowercased words >= 2 chars, stopwords stripped."""
    toks = re.findall(r"[a-z0-9]+", (label or "").lower())
    return {t for t in toks if len(t) >= 2 and t not in _DEDUP_STOPWORDS}


def _is_fuzzy_dup(a_tokens: set[str], b_tokens: set[str], threshold: float = 0.5) -> bool:
    """Jaccard >= threshold AND at least 2 shared tokens (avoids false-positives
    where two short labels share 1 stopword)."""
    if not a_tokens or not b_tokens:
        return False
    inter = a_tokens & b_tokens
    if len(inter) < 2:
        return False
    union = a_tokens | b_tokens
    return len(inter) / len(union) >= threshold


# ── The unit the page is built from ──────────────────────────────────────────

class _SelectedInsight:
    """One matrix cell: every initiative row that was folded into one label.

    Both `meeting_ids` and `initiative_ids` are unions across the fold. The
    detail view reads its supporting meetings from the first and its evidence
    quotes from the second, so the count on the card and the meetings on the
    page are the same set by construction.
    """

    __slots__ = ("label", "action_type", "score", "meeting_ids", "initiative_ids",
                 "has_measured", "first_date", "last_date", "best_initiative_id")

    def __init__(self, row: dict):
        self.label             = row["initiative_name"]
        self.action_type       = row["action_type"] or "discussed"
        self.score             = row["confidence"] or 0.0
        self.meeting_ids       = {row["meeting_id"]}
        self.initiative_ids    = {row["initiative_id"]}
        self.has_measured      = bool(row.get("has_measured"))
        self.first_date        = row.get("meeting_date")
        self.last_date         = row.get("meeting_date")
        self.best_initiative_id = row["initiative_id"]

    def absorb(self, row: dict, adopt_label: bool) -> None:
        """Fold another initiative row into this insight."""
        self.meeting_ids.add(row["meeting_id"])
        self.initiative_ids.add(row["initiative_id"])
        self.has_measured = self.has_measured or bool(row.get("has_measured"))

        d = row.get("meeting_date")
        if d:
            if self.first_date is None or d < self.first_date:
                self.first_date = d
            if self.last_date is None or d > self.last_date:
                self.last_date = d

        # The label, the action state and the representative row all follow
        # the strongest evidence, not merely the highest extraction score —
        # otherwise a well-described *proposal* overrides the board's actual
        # *approval* of the same item, which is how the Electrical Engineering
        # Technology AAS displayed as "proposed" a week after it passed.
        if adopt_label:
            self.label              = row["initiative_name"]
            self.action_type        = row["action_type"] or self.action_type
            self.best_initiative_id = row["initiative_id"]
        if (row["confidence"] or 0.0) > self.score:
            self.score = row["confidence"] or 0.0

    def merge(self, other: "_SelectedInsight") -> None:
        """Absorb a fuzzy-matched sibling. Caller has decided self is canonical."""
        self.meeting_ids    |= other.meeting_ids
        self.initiative_ids |= other.initiative_ids
        self.has_measured    = self.has_measured or other.has_measured
        if other.first_date and (self.first_date is None or other.first_date < self.first_date):
            self.first_date = other.first_date
        if other.last_date and (self.last_date is None or other.last_date > self.last_date):
            self.last_date = other.last_date
        self.score = max(self.score, other.score)

    @property
    def evidence_level(self) -> str:
        return _evidence_level(self.has_measured, self.action_type)

    @property
    def rank_key(self) -> tuple:
        """Ordering for the three slots in a cell.

        Evidence strength first, then breadth, then recency — extraction score
        only breaks remaining ties. The old key was (confidence, meetings),
        which ranked by how many fields the model populated.

        `best_initiative_id` closes the key so the order is total: ties here
        decide which label survives a fuzzy merge, and that label becomes the
        URL, so it has to be reproducible across requests.
        """
        return (
            1 if self.has_measured else 0,
            1 if (self.action_type or "").lower() in _BOARD_ACTIONS else 0,
            len(self.meeting_ids),
            self.last_date or date.min,
            self.score,
            -self.best_initiative_id,
        )


def _stronger(candidate: dict, current: _SelectedInsight) -> bool:
    """Should `candidate` take over as the canonical row for this insight?"""
    cand = (
        1 if candidate.get("has_measured") else 0,
        1 if (candidate.get("action_type") or "").lower() in _BOARD_ACTIONS else 0,
        candidate.get("meeting_date") or date.min,
        candidate.get("confidence") or 0.0,
    )
    cur = (
        1 if current.has_measured else 0,
        1 if (current.action_type or "").lower() in _BOARD_ACTIONS else 0,
        current.last_date or date.min,
        current.score,
    )
    return cand > cur


def _fuzzy_merge_bucket(bucket: dict[str, _SelectedInsight]) -> list[_SelectedInsight]:
    """Collapse near-duplicate entries in a single (school x theme) bucket.

    Walks entries strongest-first; the stronger label wins as canonical and
    absorbs the meetings and evidence of any fuzzy-matched sibling.
    """
    entries = sorted(bucket.values(), key=lambda e: e.rank_key, reverse=True)
    merged: list[_SelectedInsight] = []
    merged_tokens: list[set[str]] = []
    for e in entries:
        toks = _dedup_tokens(e.label)
        dup_idx = None
        for i, kept_toks in enumerate(merged_tokens):
            if _is_fuzzy_dup(toks, kept_toks):
                dup_idx = i
                break
        if dup_idx is None:
            merged.append(e)
            merged_tokens.append(toks)
        else:
            merged[dup_idx].merge(e)
    return merged


# ── Shared row fetch ─────────────────────────────────────────────────────────

_ROWS_SQL = f"""
    SELECT
        i.initiative_id, i.school_slug,
        i.initiative_name, i.category,
        i.action_type, i.confidence,
        i.meeting_id,
        (i.measured_outcome IS NOT NULL) AS has_measured
    FROM initiatives i
    WHERE {_ELIGIBLE}
      AND i.initiative_name IS NOT NULL
      AND i.initiative_name <> ''
    ORDER BY i.confidence DESC, i.initiative_id
"""
# The trailing initiative_id is load-bearing, not tidiness. Confidence ties are
# common (384 rows share one value corpus-wide) and Postgres does not promise an
# order within them. The matrix scans every school and the detail scans one, so
# an unstable tie let the two runs pick different canonical labels out of the
# same fuzzy-merge group — the matrix would render a cell whose id then 404'd.


def _build_buckets(rows: list[dict]) -> dict[tuple[str, str], list[_SelectedInsight]]:
    """Group eligible rows into (school, theme) -> ranked insights."""
    raw: dict[tuple[str, str], dict[str, _SelectedInsight]] = {}

    for r in rows:
        theme_key = _category_to_theme(r["category"])
        key = (r["school_slug"], theme_key)
        label_slug = _make_slug(r["initiative_name"])
        if not label_slug:
            continue

        bucket = raw.setdefault(key, {})
        existing = bucket.get(label_slug)
        if existing is None:
            bucket[label_slug] = _SelectedInsight(r)
        else:
            existing.absorb(r, adopt_label=_stronger(r, existing))

    out: dict[tuple[str, str], list[_SelectedInsight]] = {}
    for key, bucket in raw.items():
        merged = _fuzzy_merge_bucket(bucket)
        out[key] = sorted(merged, key=lambda e: e.rank_key, reverse=True)
    return out


def _fetch_rows(db: Session, eligible: dict[int, _EligibleMeeting],
                school_slug: Optional[str] = None) -> list[dict]:
    """Initiative rows from the meetings in the window, stamped with the date
    the board met.

    `meeting_date` is attached here rather than selected, so that no downstream
    reader can reach for `published_date` and quietly go back to describing an
    upload as a meeting.
    """
    if not eligible:
        return []

    sql = _ROWS_SQL
    params: dict = {"eligible_ids": sorted(eligible)}
    if school_slug is not None:
        sql = sql.replace("ORDER BY", "AND i.school_slug = :slug\n    ORDER BY")
        params["slug"] = school_slug

    rows = []
    for r in db.execute(text(sql), params).fetchall():
        row = dict(r._mapping)
        row["meeting_date"] = eligible[row["meeting_id"]].held_date
        rows.append(row)
    return rows


# ── Matrix builder ───────────────────────────────────────────────────────────

# How many insights a cell shows before the reader asks for the rest. This is a
# display default, not a filter: the payload carries every insight in the
# window, because a cap here made the rest of them unreachable. There was no
# filter, no search and no way to open a cell past its third item, so an
# insight ranked fourth existed in the database and nowhere else.
CELL_PREVIEW_LIMIT = 3


def get_insight_matrix(db: Session, today: Optional[date] = None) -> dict:
    """Build the cross-college insight matrix from recent board business."""
    window_start = _window_start(today)
    window_end   = today or date.today()

    schools = db.execute(text("SELECT slug, name FROM schools ORDER BY name")).fetchall()
    school_slugs = [r.slug for r in schools]
    school_names = {r.slug: r.name for r in schools}

    eligible = eligible_meetings(db, window_start, window_end)
    rows = _fetch_rows(db, eligible)
    buckets = _build_buckets(rows)

    # Per-college coverage, so the page can say why a column is short rather
    # than letting a reader infer that the college does less. Mt. SAC showing
    # four items next to Alamo's twenty-one is a corpus fact, not a governance
    # fact.
    #
    # Counted off the same resolved set the cells are built from — a separate
    # SQL rollup would be a second definition of "in the window", and it was
    # the one that still counted by upload date.
    coverage = {
        slug: {
            "school_slug":         slug,
            "school_name":         school_names.get(slug, slug),
            "meeting_count":       0,
            "latest_meeting_date": None,
            "insight_count":       0,
        }
        for slug in school_slugs
    }
    for m in eligible.values():
        entry = coverage.get(m.school_slug)
        if entry is None:
            continue
        entry["meeting_count"] += 1
        held = str(m.held_date)
        if entry["latest_meeting_date"] is None or held > entry["latest_meeting_date"]:
            entry["latest_meeting_date"] = held

    theme_rows = []
    total_delivered = 0
    for theme_key, theme_label in THEMES.items():
        cells: dict[str, list[dict]] = {}
        for slug in school_slugs:
            ranked = buckets.get((slug, theme_key), [])
            total_delivered += len(ranked)
            if slug in coverage:
                coverage[slug]["insight_count"] += len(ranked)

            cells[slug] = [
                {
                    "insight_id":     _make_insight_id(slug, theme_key, e.label),
                    "school_slug":    slug,
                    "school_name":    school_names.get(slug, slug),
                    "theme_key":      theme_key,
                    "theme_label":    theme_label,
                    "label":          e.label,
                    "action_type":    e.action_type,
                    "meeting_count":  len(e.meeting_ids),
                    "first_meeting_date": str(e.first_date) if e.first_date else None,
                    "last_meeting_date":  str(e.last_date) if e.last_date else None,
                    "evidence_level": e.evidence_level,
                    "evidence_label": EVIDENCE_LEVELS[e.evidence_level][0],
                    "has_detail":     True,
                }
                for e in ranked
            ]

        theme_rows.append({
            "theme_key":   theme_key,
            "theme_label": theme_label,
            "cells":       cells,
        })

    total_available = sum(len(v) for v in buckets.values())

    return {
        "school_slugs":    school_slugs,
        "school_names":    school_names,
        "themes":          theme_rows,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "window_start":    str(window_start),
        "window_end":      str(window_end),
        "window_months":   INSIGHTS_WINDOW_MONTHS,
        "preview_limit":   CELL_PREVIEW_LIMIT,
        "insight_count":   total_delivered,
        "available_count": total_available,
        "coverage":        [coverage[s] for s in school_slugs if s in coverage],
    }


# ── Detail builder ───────────────────────────────────────────────────────────

def _resolve(db: Session, insight_id: str, eligible: dict[int, _EligibleMeeting]
             ) -> Optional[tuple[str, str, _SelectedInsight]]:
    """Find the insight this id names, or None.

    There is deliberately no fallback. The previous version answered an
    unmatched label with the theme's highest-scoring initiative and echoed the
    requested id back, so `/insights/detail/alamo_colleges__T1__free-tuition-
    for-everyone` returned HTTP 200 showing "29 by 29 dual-credit growth
    strategy". That made fabricated URLs look authoritative and let stale
    bookmarks silently point at a different insight.
    """
    parts = insight_id.split("__", 2)
    if len(parts) != 3:
        return None
    school_slug, theme_key, label_slug = parts
    if theme_key not in THEMES:
        return None

    rows = _fetch_rows(db, eligible, school_slug=school_slug)
    if not rows:
        return None

    buckets = _build_buckets(rows)
    for entry in buckets.get((school_slug, theme_key), []):
        if _make_slug(entry.label) == label_slug:
            return school_slug, theme_key, entry
    return None


def _bind_params(prefix: str, values) -> tuple[str, dict]:
    """Render an IN (...) list with named params."""
    values = list(values)
    placeholders = ", ".join(f":{prefix}{i}" for i in range(len(values)))
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return placeholders, params


def get_insight_detail(db: Session, insight_id: str,
                       today: Optional[date] = None) -> Optional[dict]:
    """Full detail for one insight, or None if the id names nothing."""
    window_start = _window_start(today)
    window_end   = today or date.today()

    eligible = eligible_meetings(db, window_start, window_end)
    resolved = _resolve(db, insight_id, eligible)
    if resolved is None:
        return None
    school_slug, theme_key, entry = resolved

    theme_label = THEMES[theme_key]
    level       = entry.evidence_level
    level_label, level_note = EVIDENCE_LEVELS[level]

    school_name = db.execute(
        text("SELECT name FROM schools WHERE slug = :slug"), {"slug": school_slug}
    ).scalar() or school_slug

    # ── Supporting meetings ────────────────────────────────────────────────
    # Exactly the meetings that produced this insight — the same set the
    # matrix counted. Nothing theme-wide leaks in.
    mid_ph, mid_params = _bind_params("mid", sorted(entry.meeting_ids))
    meeting_rows = db.execute(text(f"""
        SELECT meeting_id, title
        FROM meetings
        WHERE meeting_id IN ({mid_ph})
    """), mid_params).fetchall()
    # Newest first, by the date the board met. Ordering in Python rather than
    # SQL for the same reason the date is displayed from here: the column the
    # database could sort on is the upload date.
    supporting_meetings = [
        {
            "meeting_id":  r.meeting_id,
            "title":       r.title,
            "date":        str(eligible[r.meeting_id].held_date),
            "school_slug": school_slug,
        }
        for r in sorted(meeting_rows,
                        key=lambda r: (eligible[r.meeting_id].held_date, r.meeting_id),
                        reverse=True)
    ]

    # ── The four evidence levels, kept apart ───────────────────────────────
    # pipeline/initiative_extractor.py separates these deliberately ("four
    # distinct evidence levels, never conflated") and the API used to
    # concatenate them into one prose blob prefixed "Rationale: ...
    # Expected outcome: ... Measured outcome: ...". A college's *prediction*
    # then read like a result. They travel as separate fields now.
    iid_ph, iid_params = _bind_params("iid", sorted(entry.initiative_ids))
    field_rows = db.execute(text(f"""
        SELECT initiative_id, description, observed_action, stated_rationale,
               claimed_outcome, measured_outcome, action_type, confidence,
               meeting_id
        FROM initiatives
        WHERE initiative_id IN ({iid_ph})
    """), iid_params).fetchall()
    by_id = {r.initiative_id: r for r in field_rows}
    best = by_id.get(entry.best_initiative_id) or (field_rows[0] if field_rows else None)

    def _pick(field: str) -> Optional[str]:
        """Prefer the canonical row's value, else any sibling that has one."""
        if best is not None and getattr(best, field, None):
            return getattr(best, field)
        for r in field_rows:
            if getattr(r, field, None):
                return getattr(r, field)
        return None

    description      = _pick("description")
    observed_action  = _pick("observed_action")
    stated_rationale = _pick("stated_rationale")
    claimed_outcome  = _pick("claimed_outcome")
    measured_outcome = _pick("measured_outcome")
    summary          = observed_action or description or entry.label

    # ── Why this appears — stated from the numbers, not a theme rollup ─────
    #
    # Count only. Dates stay out of this sentence: the API would have to emit
    # them pre-formatted, and the page already shows the range in the header
    # and every individual date under "Meetings this comes from". The evidence
    # note stays out for the same reason — the header carries it.
    n = len(entry.meeting_ids)
    why_it_appears = (
        f"Drawn from {n} board meeting{'s' if n != 1 else ''} in this period."
    )

    # ── Evidence, from every row folded into this insight ──────────────────
    evidence_rows = db.execute(text(f"""
        SELECT e.evidence_id, e.initiative_id, e.chunk_id, e.exact_quote,
               e.supports, e.start_time_sec, i.meeting_id,
               m.title AS meeting_title
        FROM extraction_evidence e
        JOIN initiatives i ON i.initiative_id = e.initiative_id
        JOIN meetings    m ON m.meeting_id    = i.meeting_id
        WHERE e.initiative_id IN ({iid_ph})
    """), iid_params).fetchall()
    # Most recent meeting first, then in transcript order within it.
    evidence_rows = sorted(
        evidence_rows,
        key=lambda r: (-eligible[r.meeting_id].held_date.toordinal(),
                       r.start_time_sec if r.start_time_sec is not None else 1 << 31,
                       r.evidence_id),
    )

    evidence = []
    seen_quotes: set[tuple] = set()
    evidence_chunks: list[str] = []
    for r in evidence_rows:
        dedup_key = (r.chunk_id, (r.exact_quote or "").strip())
        if dedup_key in seen_quotes:
            continue
        seen_quotes.add(dedup_key)
        if r.chunk_id:
            evidence_chunks.append(r.chunk_id)
        evidence.append({
            "text":          r.exact_quote,
            "speaker":       None,
            "timestamp_sec": r.start_time_sec,
            "meeting_title": r.meeting_title,
            "meeting_date":  str(eligible[r.meeting_id].held_date),
            "meeting_id":    r.meeting_id,
            "chunk_id":      r.chunk_id,
            "supports":      list(r.supports) if r.supports else [],
            "verified":      True,
        })

    # ── Rows cited in the same transcript passage ──────────────────────────
    # Chunk overlap, not "anything in this theme sorted by amount". The old
    # query put a $1.048bn all-funds budget under a new degree programme on
    # 58% of detail pages; bound this way the same page shows the $750,000
    # five-year expense projection that the presentation actually cited.
    #
    # Chunks overlap each other, so a boundary chunk can carry two topics.
    # The section is titled by what is literally true — these rows were cited
    # in the same passage — rather than asserting they are about the insight.
    related_votes: list[dict] = []
    related_financials: list[dict] = []
    if evidence_chunks:
        chunk_params = {**mid_params, "chunks": sorted(set(evidence_chunks))}
        related_votes = [dict(r._mapping) for r in db.execute(text(f"""
            SELECT vote_id, motion_text, vote_result_text,
                   yes_count, no_count, passed, unanimous,
                   meeting_id, chunk_ids
            FROM votes
            WHERE meeting_id IN ({mid_ph})
              AND needs_review = FALSE
              AND chunk_ids && CAST(:chunks AS text[])
            ORDER BY vote_id
            LIMIT 10
        """), chunk_params).fetchall()]

        related_financials = [dict(r._mapping) for r in db.execute(text(f"""
            SELECT item_id, action_type, category, vendor, amount, description,
                   meeting_id, chunk_ids
            FROM financial_items
            WHERE meeting_id IN ({mid_ph})
              AND needs_review = FALSE
              AND chunk_ids && CAST(:chunks AS text[])
            ORDER BY amount DESC NULLS LAST
            LIMIT 10
        """), chunk_params).fetchall()]

    # ── Peers: the same theme at other colleges, in the same window ────────
    peer_cells = []
    peer_rows = _fetch_rows(db, eligible)
    peer_buckets = _build_buckets(peer_rows)
    peer_names = {
        r.slug: r.name
        for r in db.execute(text("SELECT slug, name FROM schools ORDER BY name")).fetchall()
    }
    for slug, name in peer_names.items():
        if slug == school_slug:
            continue
        ranked = peer_buckets.get((slug, theme_key), [])
        if not ranked:
            continue
        top = ranked[0]
        peer_cells.append({
            "school_slug":    slug,
            "school_name":    name,
            "label":          top.label,
            "action_type":    top.action_type,
            "evidence_level": top.evidence_level,
            "evidence_label": EVIDENCE_LEVELS[top.evidence_level][0],
            "insight_id":     _make_insight_id(slug, theme_key, top.label),
        })

    return {
        "insight_id":          insight_id,
        "school_slug":         school_slug,
        "school_name":         school_name,
        "theme_key":           theme_key,
        "theme_label":         theme_label,
        "label":               entry.label,
        "action_type":         entry.action_type,
        "evidence_level":      level,
        "evidence_label":      level_label,
        "evidence_note":       level_note,
        "meeting_count":       len(entry.meeting_ids),
        "first_meeting_date":  str(entry.first_date) if entry.first_date else None,
        "last_meeting_date":   str(entry.last_date) if entry.last_date else None,
        "summary":             summary,
        "description":         description,
        "observed_action":     observed_action,
        "stated_rationale":    stated_rationale,
        "claimed_outcome":     claimed_outcome,
        "measured_outcome":    measured_outcome,
        "why_it_appears":      why_it_appears,
        "supporting_meetings": supporting_meetings,
        "evidence":            evidence,
        "related_votes":       related_votes,
        "related_financials":  related_financials,
        "peer_cells":          peer_cells,
    }
