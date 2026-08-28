"""Invariants for the Insights page.

Every assertion here corresponds to a defect the page actually shipped:

  * detail pages announced "appeared across 48 board meeting(s)" for
    single-meeting items, because supporting meetings were collected
    theme-wide rather than per-insight;
  * an unmatched insight id returned HTTP 200 showing a different insight;
  * "Related Financials" listed a $1.048bn all-funds budget beside a
    home-visit partnership;
  * a "100% confidence" badge reported extraction form-completeness;
  * undated items from 2021 rendered in present tense.

The suite runs against whatever corpus the configured database holds. Where a
check needs data that may legitimately be absent (a measured outcome, a
chunk-bound financial), it skips rather than fails — a thin corpus is not a
regression, but a broken invariant is.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.db.session import SessionLocal
from api.db.queries import insights as q
from api.routers import insights as insights_router


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(insights_router.router)
    return TestClient(app)


@pytest.fixture(scope="module")
def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="module")
def matrix(client: TestClient) -> dict:
    r = client.get("/insights/matrix")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def cells(matrix: dict) -> list[dict]:
    out = [c for t in matrix["themes"] for v in t["cells"].values() for c in v]
    if not out:
        pytest.skip("no insights in the rolling window for this corpus")
    return out


@pytest.fixture(scope="module")
def details(client: TestClient, cells: list[dict]) -> dict[str, dict]:
    out = {}
    for c in cells:
        r = client.get(f"/insights/detail/{c['insight_id']}")
        assert r.status_code == 200, f"{c['insight_id']} -> {r.status_code}"
        out[c["insight_id"]] = r.json()
    return out


# ── the rolling window ──────────────────────────────────────────────────────

def test_window_start_is_six_calendar_months_back():
    assert q.INSIGHTS_WINDOW_MONTHS == 6
    assert q._window_start(dt.date(2026, 8, 27)) == dt.date(2026, 2, 27)
    # Month-end clamping: 31 August back six months is 28/29 February.
    assert q._window_start(dt.date(2026, 8, 31)) == dt.date(2026, 2, 28)
    # Year rollover.
    assert q._window_start(dt.date(2026, 3, 15)) == dt.date(2025, 9, 15)


@pytest.mark.parametrize("title, expected", [
    ("Special Board of Trustees Meeting: June 25, 2026 at 2:00 PM", dt.date(2026, 6, 25)),
    ("Regular Board Meeting - June 02, 2026",                       dt.date(2026, 6, 2)),
    ("Board of Trustees Meeting (March 31st, 2026)",                dt.date(2026, 3, 31)),
    ("Regular Board Meeting 4/22/2026,",                            dt.date(2026, 4, 22)),
    ("Regular Board Meeting 04-16-2024",                            dt.date(2024, 4, 16)),
    ("Committee of the Whole Meeting 3-19-24",                      dt.date(2024, 3, 19)),
    ("Board Workshop 2026-04-22",                                   dt.date(2026, 4, 22)),
])
def test_the_meeting_date_comes_from_the_title(title: str, expected: dt.date):
    """Every title shape this corpus actually uses resolves to a full date."""
    held, source = q.resolve_held_date(title, dt.date(2026, 8, 27))
    assert (held, source) == (expected, "title")


def test_a_backfilled_upload_is_dated_by_the_meeting_not_the_upload():
    """The defect this filter exists for. Alamo published seven meetings two to
    five months after they were held and Lone Star bulk-uploaded 41 spanning ten
    months of business on one day; anchored on the upload date every one of them
    reads as current board business."""
    held, source = q.resolve_held_date(
        "Regular Board Meeting 01-23-2024", dt.date(2024, 6, 21))
    assert held == dt.date(2024, 1, 23), "the upload date won"
    assert source == "title"


def test_an_undated_title_falls_back_to_the_upload_date():
    """Upload is a hard upper bound on when the board met, and for the 43
    meetings whose titles state no date it is the only evidence there is."""
    assert q.resolve_held_date("Regular Board Meeting", dt.date(2026, 5, 19)) \
        == (dt.date(2026, 5, 19), "published")


def test_a_year_only_title_contradicting_the_upload_is_not_placed():
    """"Mt. SAC Board of Trustees April 2026 meeting" uploaded in March 2025:
    the year disagrees and there is no day to fall back on, so the page cannot
    honestly say when it happened — and therefore cannot claim it is recent."""
    held, source = q.resolve_held_date(
        "Mt. SAC Board of Trustees April 2026 meeting", dt.date(2025, 3, 14))
    assert held is None
    assert source == "year_conflict"


def test_a_meeting_dated_after_its_own_upload_is_not_placed():
    """A recording cannot be published before the meeting it records, so one of
    the two dates is wrong and neither can carry the row."""
    held, source = q.resolve_held_date(
        "Board Meeting January 5, 2027", dt.date(2026, 8, 27))
    assert held is None
    assert source == "after_upload"


def test_a_year_only_title_agreeing_with_the_upload_still_resolves():
    assert q.resolve_held_date(
        "Mt. SAC Board of Trustees August 2026 meeting", dt.date(2026, 8, 13)) \
        == (dt.date(2026, 8, 13), "published")


def test_eligible_meetings_are_placed_by_when_the_board_met(db: Session):
    """End to end against the live corpus: the date every eligible meeting is
    filtered and displayed by is the one its own title states."""
    start = q._window_start()
    end   = dt.date.today()
    eligible = q.eligible_meetings(db, start, end)
    assert eligible, "no meetings in the rolling window for this corpus"

    from sqlalchemy import text

    placeholders = ", ".join(f":m{i}" for i in range(len(eligible)))
    params = {f"m{i}": v for i, v in enumerate(sorted(eligible))}
    titles = db.execute(text(
        f"SELECT meeting_id, title FROM meetings WHERE meeting_id IN ({placeholders})"
    ), params).fetchall()

    checked = 0
    for r in titles:
        stated = q._title_date(r.title)
        if stated is None:
            continue
        checked += 1
        assert eligible[r.meeting_id].held_date == stated, (
            f"meeting {r.meeting_id} {r.title!r} is filed under "
            f"{eligible[r.meeting_id].held_date}, not the {stated} it states"
        )
    assert checked, "no eligible meeting states a date in its title"

    for m in eligible.values():
        assert start <= m.held_date <= end


# ── P1-5: one board session, however many recordings ────────────────────────

def test_a_session_ingested_twice_is_counted_once(db: Session):
    """Alamo's 2026-05-12 special meeting is in `meetings` twice (639 and 640,
    both 4,636s, titles differing by a "Committiee" typo) and Lone Star's
    2025-10-02 tax-rate hearing three times. Counted as recordings, two cells
    announced "2 meetings" for one afternoon of board business."""
    from sqlalchemy import text

    eligible = q.eligible_meetings(db, q._window_start(), dt.date.today())
    assert eligible, "no meetings in the rolling window for this corpus"

    placeholders = ", ".join(f":m{i}" for i in range(len(eligible)))
    params = {f"m{i}": v for i, v in enumerate(sorted(eligible))}
    sessions = db.execute(text(f"""
        SELECT s.slug, m.published_date, m.duration_seconds, COUNT(*) AS n
        FROM meetings m
        JOIN schools s ON s.school_id = m.school_id
        WHERE m.meeting_id IN ({placeholders})
          AND m.duration_seconds IS NOT NULL
        GROUP BY s.slug, m.published_date, m.duration_seconds
        HAVING COUNT(*) > 1
    """), params).fetchall()
    assert not sessions, f"duplicate recordings survived: {[tuple(r) for r in sessions]}"

    seen: dict[tuple, int] = {}
    for m in eligible.values():
        if m.duration_seconds is None:
            continue
        key = (m.school_slug, m.held_date, m.duration_seconds)
        assert key not in seen, (
            f"meetings {seen[key]} and {m.meeting_id} are the same session")
        seen[key] = m.meeting_id


def test_two_real_meetings_on_one_day_are_not_collapsed(db: Session):
    """The guard against over-collapsing. 61 same-day pairs in this corpus are
    genuinely different meetings — a Special Meeting and a Regular Meeting on
    one evening — and duration is what separates them from a second recording
    of one session."""
    eligible = q.eligible_meetings(db, q._window_start(), dt.date.today())
    same_day: dict[tuple, int] = {}
    for m in eligible.values():
        key = (m.school_slug, m.held_date)
        same_day[key] = same_day.get(key, 0) + 1
    assert any(n > 1 for n in same_day.values()), (
        "no college held two meetings on one day in this window — the guard is "
        "untested against this corpus"
    )


def test_no_insight_counts_the_same_session_twice(cells: list[dict],
                                                  details: dict[str, dict], db: Session):
    """End to end: the meeting count on a card is board sessions, not files."""
    eligible = q.eligible_meetings(db, q._window_start(), dt.date.today())
    for c in cells:
        d = details[c["insight_id"]]
        keys = [
            (eligible[m["meeting_id"]].school_slug,
             eligible[m["meeting_id"]].held_date,
             eligible[m["meeting_id"]].duration_seconds)
            for m in d["supporting_meetings"]
        ]
        assert len(keys) == len(set(keys)), (
            f"{c['insight_id']} claims {c['meeting_count']} meetings but they "
            f"are not distinct sessions: {keys}"
        )


def test_matrix_reports_the_window_it_covers(matrix: dict):
    assert matrix["window_months"] == 6
    assert matrix["window_start"] < matrix["window_end"]
    assert matrix["insight_count"] <= matrix["available_count"]


# ── P2-1: everything in the window is reachable ─────────────────────────────

def test_every_insight_in_the_window_is_delivered(matrix: dict, cells: list[dict]):
    """Cells were truncated to the top three, and the page had no filter, no
    search and no way to open a cell past its third item — so an insight
    ranked fourth existed in the database and nowhere a reader could get to."""
    assert matrix["insight_count"] == matrix["available_count"], (
        f"{matrix['available_count'] - matrix['insight_count']} insights in the "
        f"window are not in the payload"
    )
    assert len(cells) == matrix["available_count"]


def test_the_preview_limit_is_a_display_default_not_a_cap(matrix: dict):
    """`preview_limit` tells the page what to collapse to. It must not be what
    the API sends, or the cap is simply back."""
    limit = matrix["preview_limit"]
    assert limit >= 1
    biggest = max(
        (len(group) for t in matrix["themes"] for group in t["cells"].values()),
        default=0,
    )
    assert biggest > limit, (
        "no cell exceeds the preview limit, so this corpus cannot show whether "
        "the payload is still being truncated"
    )


def test_every_delivered_insight_is_reachable_by_filtering(cells: list[dict]):
    """The filter bar narrows by college, theme, action, evidence, date and
    free text. Every item has to survive at least the filter combination that
    describes it, or it is on the page but unreachable."""
    for c in cells:
        assert c["school_slug"] and c["theme_key"], c["insight_id"]
        assert c["action_type"], c["insight_id"]
        assert c["evidence_level"] in {"measured", "action", "discussion"}, c["insight_id"]
        # The date filter compares against the span the insight covers.
        assert c["first_meeting_date"] and c["last_meeting_date"], c["insight_id"]
        assert c["first_meeting_date"] <= c["last_meeting_date"], c["insight_id"]
        # Free-text search reads label, school name, theme label and action.
        assert (c["label"] or "").strip(), c["insight_id"]


def test_coverage_counts_every_item_not_just_the_visible_ones(
    matrix: dict, cells: list[dict]
):
    """The coverage table's per-college count came from the truncated set, so
    it under-reported every college with a busy theme."""
    per_school: dict[str, int] = {}
    for c in cells:
        per_school[c["school_slug"]] = per_school.get(c["school_slug"], 0) + 1
    for row in matrix["coverage"]:
        assert row["insight_count"] == per_school.get(row["school_slug"], 0), (
            f"{row['school_slug']} coverage says {row['insight_count']}, "
            f"payload has {per_school.get(row['school_slug'], 0)}"
        )


def test_no_displayed_insight_predates_the_window(matrix: dict, cells: list[dict]):
    start = matrix["window_start"]
    stale = [c for c in cells if (c["first_meeting_date"] or "") < start]
    assert not stale, f"{len(stale)} insights predate the window: {stale[:3]}"


def test_every_date_the_page_shows_is_inside_the_window(
    matrix: dict, cells: list[dict], details: dict[str, dict]
):
    """Filtering and display have to agree. When the window was measured on the
    upload date and the dates were rendered from it too they agreed by accident;
    they are now both the meeting date, and this fails if either drifts."""
    start, end = matrix["window_start"], matrix["window_end"]

    def check(value, where):
        assert value is None or start <= value <= end, f"{where}: {value}"

    for c in matrix["coverage"]:
        if c["meeting_count"]:
            check(c["latest_meeting_date"], f"coverage {c['school_slug']}")
    for c in cells:
        check(c["first_meeting_date"], f"cell {c['insight_id']} first")
        check(c["last_meeting_date"], f"cell {c['insight_id']} last")
    for insight_id, d in details.items():
        check(d["first_meeting_date"], f"{insight_id} first")
        check(d["last_meeting_date"], f"{insight_id} last")
        for m in d["supporting_meetings"]:
            check(m["date"], f"{insight_id} meeting {m['meeting_id']}")
        for e in d["evidence"]:
            check(e["meeting_date"], f"{insight_id} evidence {e['chunk_id']}")


def test_supporting_meetings_are_newest_first(details: dict[str, dict]):
    """The list is ordered on the meeting date, which is not a column the
    database can sort on."""
    for insight_id, d in details.items():
        dates = [m["date"] for m in d["supporting_meetings"]]
        assert dates == sorted(dates, reverse=True), f"{insight_id}: {dates}"


def test_no_supporting_meeting_is_archived_or_soft_deleted(
    db: Session, details: dict[str, dict]
):
    """The query joined `meetings` with no status filter, so 18 of 146 displayed
    insights came only from meetings the pipeline had archived out of the pilot
    — including a COVID-19 protocol item from March 2021."""
    from sqlalchemy import text

    cited = sorted({m["meeting_id"] for d in details.values() for m in d["supporting_meetings"]})
    assert cited, "no supporting meetings to check"

    placeholders = ", ".join(f":m{i}" for i in range(len(cited)))
    params = {f"m{i}": v for i, v in enumerate(cited)}
    params["start"] = q._window_start()

    ineligible = db.execute(text(f"""
        SELECT meeting_id, status, is_active, published_date
        FROM meetings
        WHERE meeting_id IN ({placeholders})
          AND (status <> 'indexed' OR NOT is_active OR published_date < :start)
    """), params).fetchall()
    assert not ineligible, (
        f"{len(ineligible)} cited meetings are archived, soft-deleted, or out of "
        f"window: {[tuple(r) for r in ineligible[:3]]}"
    )


# ── P0-1: support inflation ─────────────────────────────────────────────────

def test_supporting_meetings_matches_the_advertised_count(
    cells: list[dict], details: dict[str, dict]
):
    bad = [
        (c["insight_id"], len(details[c["insight_id"]]["supporting_meetings"]), c["meeting_count"])
        for c in cells
        if len(details[c["insight_id"]]["supporting_meetings"]) != c["meeting_count"]
    ]
    assert not bad, f"support inflation on {len(bad)} pages: {bad[:3]}"


def test_why_it_appears_states_the_real_meeting_count(
    cells: list[dict], details: dict[str, dict]
):
    for c in cells:
        d = details[c["insight_id"]]
        n = d["meeting_count"]
        noun = "meeting" if n == 1 else "meetings"
        assert f"{n} board {noun}" in d["why_it_appears"], d["why_it_appears"]


def test_evidence_comes_only_from_the_supporting_meetings(details: dict[str, dict]):
    for iid, d in details.items():
        allowed = {m["meeting_id"] for m in d["supporting_meetings"]}
        stray = {e["meeting_id"] for e in d["evidence"]} - allowed - {None}
        assert not stray, f"{iid} quotes meetings it does not claim: {stray}"


# ── P0-2: related rows bound to the passage ─────────────────────────────────

def test_related_rows_share_a_chunk_with_the_insight(details: dict[str, dict]):
    """The old query sorted a whole theme's spending by amount, so 58% of pages
    led with a figure over $100M that had nothing to do with the insight."""
    checked = 0
    for iid, d in details.items():
        evidence_chunks = {e["chunk_id"] for e in d["evidence"] if e["chunk_id"]}
        for row in d["related_votes"] + d["related_financials"]:
            checked += 1
            assert set(row["chunk_ids"] or []) & evidence_chunks, (
                f"{iid}: related row {row} shares no transcript chunk with the insight"
            )
    if checked == 0:
        pytest.skip("no chunk-bound related rows in this corpus")


def test_related_rows_come_from_supporting_meetings(details: dict[str, dict]):
    for iid, d in details.items():
        allowed = {m["meeting_id"] for m in d["supporting_meetings"]}
        for row in d["related_votes"] + d["related_financials"]:
            assert row["meeting_id"] in allowed, f"{iid}: related row from an unclaimed meeting"


def test_personnel_section_is_gone(details: dict[str, dict]):
    """No initiative category maps to a personnel source, so the section could
    only ever show unrelated rows."""
    for d in details.values():
        assert "related_personnel" not in d


# ── P0-3: dates ─────────────────────────────────────────────────────────────

def test_every_cell_carries_a_meeting_date(cells: list[dict]):
    undated = [c["insight_id"] for c in cells if not c["last_meeting_date"]]
    assert not undated, f"{len(undated)} cells have no date: {undated[:3]}"


def test_first_date_never_follows_last_date(cells: list[dict]):
    for c in cells:
        if c["first_meeting_date"] and c["last_meeting_date"]:
            assert c["first_meeting_date"] <= c["last_meeting_date"], c["insight_id"]


# ── P0-4: unresolvable ids ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad_id", [
    "alamo_colleges__T1__free-tuition-for-everyone",   # fabricated claim, real school+theme
    "lone_star_college__T7__zzzz",                     # nonsense slug
    "dallas_college__T2__this-initiative-does-not-exist",
    "nonexistent_school__T1__x",                       # unknown school
    "alamo_colleges__T99__x",                          # unknown theme
    "garbage",                                         # malformed
    "a__b",                                            # too few segments
])
def test_unresolvable_insight_ids_return_404(client: TestClient, bad_id: str):
    r = client.get(f"/insights/detail/{bad_id}")
    assert r.status_code == 404, (
        f"{bad_id} returned {r.status_code} with label "
        f"{r.json().get('label')!r} — the substitution fallback is back"
    )


def test_every_rendered_cell_resolves(details: dict[str, dict], cells: list[dict]):
    """Matrix and detail must apply identical eligibility, or a cell 404s.

    They can also disagree through unstable ordering: `confidence` ties are
    common, and without a total order the two queries pick different canonical
    labels out of the same fuzzy-merge group.
    """
    assert len(details) == len(cells)
    for c in cells:
        assert details[c["insight_id"]]["insight_id"] == c["insight_id"]


def test_resolution_is_stable_across_repeat_calls(client: TestClient, cells: list[dict]):
    second = client.get("/insights/matrix").json()
    again = {c["insight_id"] for t in second["themes"] for v in t["cells"].values() for c in v}
    assert {c["insight_id"] for c in cells} == again


# ── P0-5: the confidence percentage is gone ─────────────────────────────────

def test_confidence_is_not_serialized_anywhere(client: TestClient, cells: list[dict]):
    assert "confidence" not in client.get("/insights/matrix").text
    for c in cells[:20]:
        body = client.get(f"/insights/detail/{c['insight_id']}").text
        assert '"confidence"' not in body, f"{c['insight_id']} leaks a confidence score"


def test_every_cell_has_a_defensible_evidence_level(cells: list[dict]):
    assert {c["evidence_level"] for c in cells} <= {"measured", "action", "discussion"}
    for c in cells:
        assert c["evidence_label"]


def test_evidence_level_follows_the_record(details: dict[str, dict]):
    for iid, d in details.items():
        if d["evidence_level"] == "measured":
            assert d["measured_outcome"], f"{iid} claims a measured result but has none"
        elif d["evidence_level"] == "action":
            assert d["action_type"] in {"approved", "launched"}, iid
            assert not d["measured_outcome"], iid
        else:
            assert d["action_type"] not in {"approved", "launched"}, iid
            assert not d["measured_outcome"], iid


# ── P1-2: theme mapping ─────────────────────────────────────────────────────

def test_every_extractor_category_has_an_explicit_theme():
    """A keyword-substring scorer put `governance` and `other` in a default
    bucket because neither matched any keyword — which is how multi-year
    strategic plans came to sit under "Procedural Items"."""
    assert q.check_theme_map() == [], (
        f"categories with no theme mapping: {q.check_theme_map()}"
    )


def test_theme_mapping_is_exact_not_substring():
    assert q._category_to_theme("financial_sustainability") == "T3"   # not T4 via "ai"
    assert q._category_to_theme("career_technical_education") == "T2"  # not T4 via "tech"
    assert q._category_to_theme("governance") == "T7"                 # not a default
    assert q._category_to_theme("other") == "T8"


def test_cells_only_use_known_themes(cells: list[dict]):
    assert {c["theme_key"] for c in cells} <= set(q.THEMES)


# ── P1-1: ranking ───────────────────────────────────────────────────────────

def test_measured_outcomes_outrank_discussion_within_a_cell(matrix: dict):
    """Ranking was (confidence, meetings), i.e. how many fields the extractor
    populated — which let a well-described proposal outrank a board approval."""
    order = {"measured": 2, "action": 1, "discussion": 0}
    for t in matrix["themes"]:
        for slug, group in t["cells"].items():
            scores = [order[c["evidence_level"]] for c in group]
            assert scores == sorted(scores, reverse=True), (
                f"{slug}/{t['theme_key']} ranks weaker evidence first: "
                f"{[(c['label'], c['evidence_level']) for c in group]}"
            )


# ── P1-3 / P1-4: what the detail exposes to the UI ──────────────────────────

def test_evidence_quotes_are_chunk_bound_and_verified(details: dict[str, dict]):
    """The transcript deep-link and the "Verified" marker both depend on this."""
    for iid, d in details.items():
        assert d["evidence"], f"{iid} has no evidence"
        for e in d["evidence"]:
            assert e["chunk_id"], f"{iid} has a quote with no chunk_id"
            assert e["verified"] is True
            assert e["meeting_id"] is not None


def test_the_four_evidence_levels_travel_separately(details: dict[str, dict]):
    """The API used to concatenate them into one prose blob, so a college's
    forecast read like a measured result."""
    for iid, d in details.items():
        for field in ("observed_action", "stated_rationale", "claimed_outcome",
                      "measured_outcome"):
            assert field in d, f"{iid} is missing {field}"
        assert "Expected outcome:" not in d["summary"]
        assert "Measured outcome:" not in d["summary"]
        assert "Rationale:" not in d["summary"]


def test_a_measured_insight_has_a_quote_supporting_the_figure(details: dict[str, dict]):
    measured = [d for d in details.values() if d["evidence_level"] == "measured"]
    if not measured:
        pytest.skip("no measured outcomes in the rolling window")
    for d in measured:
        supports = {s for e in d["evidence"] for s in (e["supports"] or [])}
        assert "measured_outcome" in supports, (
            f"{d['insight_id']} reports a figure with no quote tagged to it"
        )


# ── peers ───────────────────────────────────────────────────────────────────

def test_peer_cells_are_other_schools_in_the_same_theme(details: dict[str, dict]):
    for iid, d in details.items():
        slugs = [p["school_slug"] for p in d["peer_cells"]]
        assert d["school_slug"] not in slugs, f"{iid} lists itself as a peer"
        assert len(slugs) == len(set(slugs)), f"{iid} lists a peer twice"
        assert all(p["theme_key"] == d["theme_key"] for p in d["peer_cells"]
                   if "theme_key" in p)


def test_peer_links_resolve(client: TestClient, details: dict[str, dict]):
    seen: set[str] = set()
    for d in details.values():
        for p in d["peer_cells"]:
            if p["insight_id"] in seen:
                continue
            seen.add(p["insight_id"])
            r = client.get(f"/insights/detail/{p['insight_id']}")
            assert r.status_code == 200, f"peer link {p['insight_id']} -> {r.status_code}"
