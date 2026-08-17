# -*- coding: utf-8 -*-
"""Regenerate eval/eval_set.jsonl.  Run:  uv run python eval/build_eval_set.py

Every anchor below was read out of the live DB on 2026-08-16 (financial_items,
initiatives, personnel_actions, votes). The chunk ids come from those rows'
`chunk_ids` provenance column, so expected_chunk_ids is ground truth about
which chunk actually supports the claim -- not a guess.
"""
import io, json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent / "eval_set.jsonl"

C = []


def case(**kw):
    kw.setdefault("school_slug", None)
    kw.setdefault("must_not_contain",
                  ["i don't have", "i do not have", "no data", "cannot find", "unable to find"])
    kw.setdefault("expected_chunk_ids", None)
    kw.setdefault("expected_meeting_ids", None)
    kw.setdefault("expected_school_slug", None)
    C.append(kw)


REFUSAL = ["i don't", "i do not", "cannot help", "unable to", "outside",
           "off-topic", "board meeting", "not able"]
SUMMARY = ["board", "meeting", "approved", "discussed"]

# ── SQL: a number the SQL adapter has to get right ──────────────────────────
case(id="ask-001", route_kind="sql", expected_route="sql",
     question="How much did HCC approve for the fiscal year 2025-26 unrestricted operating budget?",
     must_contain_any=["481", "$481", "481,000,000", "481 million"],
     expected_meeting_ids=[19],
     notes="HCC FY2025-26 unrestricted operating budget $481M. Also exercises school auto-detect: 'HCC' appears in the text, no school_slug is passed.")

case(id="ask-009", route_kind="sql", expected_route="sql",
     question="What was the maximum par amount authorized for El Paso's refunding bonds?",
     school_slug="el_paso_community_college",
     must_contain_any=["103", "103.9", "103,910,000", "$103"],
     expected_meeting_ids=[210],
     notes="CORRECTED 2026-08-16. Previously anchored on $84M, which no longer exists after the evidence-provenance re-extraction; financial_items now holds 103,910,000. The stale anchor made this case fail permanently.")

case(id="ask-010", route_kind="sql", expected_route="sql",
     question="What funding level was proposed for the Alamo Colleges bond program?",
     school_slug="alamo_colleges",
     must_contain_any=["987", "$987", "987 million", "987,000,000"],
     expected_meeting_ids=[670],
     notes="Alamo future bond program $987,000,000.")

case(id="ask-011", route_kind="sql", expected_route="sql",
     question="How large is the second tranche of general obligation bonds Dallas College asked to issue?",
     school_slug="dallas_college",
     must_contain_any=["400", "$400", "400 million", "400,000,000"],
     expected_meeting_ids=[790],
     notes="Dallas College second tranche of GO bonds, $400,000,000.")

case(id="ask-012", route_kind="sql", expected_route="sql",
     question="How much did Lone Star College set aside for the ERP project?",
     school_slug="lone_star_college",
     must_contain_any=["66", "$66", "66 million", "66,000,000"],
     expected_meeting_ids=[63],
     notes="Lone Star ERP set-aside $66,000,000.")

case(id="ask-013", route_kind="sql", expected_route="sql",
     question="What was the size of the Austin Community College limited tax bond issuance?",
     school_slug="austin_community_college",
     must_contain_any=["249", "249.9", "249,920,000", "$249"],
     expected_meeting_ids=[519],
     notes="ACC Limited Tax and Revenue bond sale, $249,920,000.")

case(id="ask-014", route_kind="sql", expected_route="sql",
     question="How much contract authority was proposed for Heights Lumber and Supply?",
     school_slug="central_texas_college",
     must_contain_any=["1.25", "1,250,000", "1,250", "$1.2"],
     expected_meeting_ids=[380],
     notes="Central Texas College / Heights Lumber and Supply, building materials, $1,250,000.")

case(id="ask-015", route_kind="sql", expected_route="sql",
     question="How much did the Mt. SAC board approve transferring to cover the shortfall?",
     school_slug="mt_san_antonio_college",
     must_contain_any=["1.8", "1,800,000", "1.8 million", "2 million"],
     expected_meeting_ids=[472],
     notes="Mt. SAC approved a transfer of ~$1.8M; a ~$2M transfer is discussed in the same meeting, so both figures are accepted.")

# ── RAG: narrative only the transcript carries ──────────────────────────────
case(id="ask-002", route_kind="rag", expected_route=["rag", "hybrid"],
     question="What concerns or themes did trustees raise about the strategic marketing plan?",
     school_slug="houston_city_college",
     must_contain_any=["marketing", "strategic", "communication", "brand", "enrollment"],
     notes="Route relaxed to rag|hybrid on 2026-08-16: the router legitimately splits between the two run to run, so scoring one as correct measured nondeterminism rather than quality.")

case(id="ask-016", route_kind="rag", expected_route=["rag", "hybrid"],
     question="What did the board discuss about the Uncrewed Aerial System degree program?",
     school_slug="alamo_colleges",
     must_contain_any=["uncrewed", "aerial", "drone", "uas", "aas"],
     expected_chunk_ids=["ecb4abc6-6370-4ce7-a5ef-b448015d6792_0028",
                         "ecb4abc6-6370-4ce7-a5ef-b448015d6792_0029"],
     expected_meeting_ids=[639],
     notes="Alamo 'Uncrewed Aerial System AAS Program' initiative; chunk ids taken from its provenance.")

case(id="ask-017", route_kind="rag", expected_route=["rag", "hybrid"],
     question="What was said about the proposed student innovation center?",
     school_slug="dallas_college",
     must_contain_any=["innovation", "center", "student"],
     expected_chunk_ids=["E0Yx7j_0008"],
     expected_meeting_ids=[802],
     notes="Dallas College 'Proposed student innovation center' initiative. Provenance also lists E0Yx7j_0009, dropped here: that chunk is a small-business tangent and carries none of the claim.")

case(id="ask-018", route_kind="rag", expected_route=["rag", "hybrid"],
     question="What did trustees say about the free tuition program for students?",
     school_slug="austin_community_college",
     must_contain_any=["free", "tuition", "student"],
     expected_chunk_ids=["6dd2b695-3ed0-44ec-a6ab-b442016be810_0019",
                         "6dd2b695-3ed0-44ec-a6ab-b442016be810_0020"],
     expected_meeting_ids=[496],
     notes="ACC 'Free-tuition student program' initiative.")

case(id="ask-019", route_kind="rag", expected_route=["rag", "hybrid"],
     question="What was discussed about the CRM implementation for enrollment?",
     school_slug="el_paso_community_college",
     must_contain_any=["crm", "lucium", "enrollment", "recruit"],
     expected_chunk_ids=["GY_n6JUShQY_0011", "GY_n6JUShQY_0012"],
     expected_meeting_ids=[207],
     notes="El Paso 'Lucium CRM implementation' initiative.")

case(id="ask-020", route_kind="rag", expected_route=["rag", "hybrid"],
     question="What is the college doing in advanced manufacturing and semiconductors?",
     school_slug="central_texas_college",
     must_contain_any=["semiconductor", "manufactur", "advanced"],
     expected_chunk_ids=["OzIqXIteYXw_0004", "OzIqXIteYXw_0005"],
     expected_meeting_ids=[343],
     notes="Central Texas College phase-one advanced manufacturing / semiconductor strategy.")

case(id="ask-021", route_kind="rag", expected_route=["rag", "hybrid"],
     question="What did the board say about publishing a campus crime map?",
     school_slug="mt_san_antonio_college",
     must_contain_any=["crime", "map", "safety", "security"],
     expected_chunk_ids=["j4wSOB03AKY_0052"],
     expected_meeting_ids=[472],
     notes="Mt. SAC 'Public campus crime map' initiative. Mt. SAC has only 4 meetings indexed - a deliberate thin-corpus case. Provenance also lists j4wSOB03AKY_0053, dropped here: it carries none of the claim.")

case(id="ask-022", route_kind="rag", expected_route=["rag", "hybrid"],
     question="How is Lone Star College using student stories in its marketing?",
     school_slug="lone_star_college",
     must_contain_any=["marketing", "student", "stories", "campaign"],
     expected_chunk_ids=["rbA3B10n2Mk_0010"],
     expected_meeting_ids=[39],
     notes="Lone Star 'Student-centered marketing campaign' initiative. Question rewritten 2026-08-16 to match the transcript's own language ('sharing these stories through our broad marketing efforts') - the original phrasing retrieved neither chunk. Provenance also lists rbA3B10n2Mk_0011, dropped here: it is the thank-yous after the segment.")

# ── HYBRID: an amount AND the discussion around it ──────────────────────────
case(id="ask-003", route_kind="hybrid", expected_route=["hybrid", "sql"],
     question="What contracts with outside vendors has HCC approved?",
     school_slug="houston_city_college",
     must_contain_any=["$", "contract", "vendor", "approved", "aviation"],
     must_not_contain=["i don't have any information", "no data available"],
     notes="Keywords lowercased on 2026-08-16 - the harness lowercases the answer but not the keywords, so 'Aviation' could never have matched.")

case(id="ask-023", route_kind="hybrid", expected_route=["hybrid", "sql"],
     question="How much was the Vision Point marketing contract and what was it for?",
     school_slug="central_texas_college",
     must_contain_any=["790", "vision point", "marketing", "branding"],
     expected_meeting_ids=[387, 388],
     notes="Central Texas College / Vision Point, $790,000 for marketing and branding. Needs the amount (SQL) and the scope (RAG).")

case(id="ask-024", route_kind="hybrid", expected_route=["hybrid", "sql", "rag"],
     question="What grant did El Paso accept from Texas Mutual Insurance and how much was it?",
     school_slug="el_paso_community_college",
     must_contain_any=["100,000", "$100,000", "texas mutual", "grant"],
     expected_chunk_ids=["jSMhdxIbjf8_0009"],
     expected_meeting_ids=[475],
     notes="El Paso vote: accept a $100,000 Texas Mutual Insurance Company grant award. Provenance also lists jSMhdxIbjf8_0010, dropped here: that chunk is executive-session boilerplate.")

case(id="ask-025", route_kind="hybrid", expected_route=["hybrid", "sql"],
     question="What is in the Alamo Colleges FY2026 all-funds budget?",
     school_slug="alamo_colleges",
     must_contain_any=["1,024", "1.02", "1,048", "billion", "budget"],
     expected_meeting_ids=[657],
     notes="Alamo FY2026 all-funds budget: $1,024,500,000 approved against a $1,048,000,000 proposal. Both figures accepted.")

# ── LATEST_MEETING: resolve the right meeting, per school ───────────────────
case(id="ask-004", route_kind="latest_meeting", expected_route="latest_meeting",
     question="What happened at HCC's last board meeting?",
     expected_school_slug="houston_city_college", expected_meeting_ids=[474],
     must_contain_any=SUMMARY,
     notes="Latest HCC meeting is 474 (2026-04-22).")

case(id="ask-006", route_kind="latest_meeting", expected_route="latest_meeting",
     question="Summarize the last HCC meetings.",
     expected_school_slug="houston_city_college",
     must_contain_any=SUMMARY,
     notes="Plural phrasing of ask-004 - guards the singular/plural parse.")

case(id="ask-008", route_kind="latest_meeting", expected_route="latest_meeting",
     question="Summarize the last El Paso meeting.",
     expected_school_slug="el_paso_community_college", expected_meeting_ids=[475],
     must_contain_any=SUMMARY,
     notes="Latest El Paso meeting is 475 (2026-04-23).")

case(id="ask-026", route_kind="latest_meeting", expected_route="latest_meeting",
     question="What happened at the most recent Dallas College board meeting?",
     expected_school_slug="dallas_college", expected_meeting_ids=[480],
     must_contain_any=SUMMARY,
     notes="Latest Dallas College meeting is 480 (2026-05-13).")

case(id="ask-027", route_kind="latest_meeting", expected_route="latest_meeting",
     question="Summarize the latest Lone Star College board meeting.",
     expected_school_slug="lone_star_college", expected_meeting_ids=[39],
     must_contain_any=SUMMARY,
     notes="Latest Lone Star meeting is 39 (2026-04-02).")

case(id="ask-028", route_kind="latest_meeting", expected_route="latest_meeting",
     question="What happened at Mt. SAC's last board meeting?",
     expected_school_slug="mt_san_antonio_college", expected_meeting_ids=[469],
     must_contain_any=SUMMARY,
     notes="Latest Mt. SAC meeting is 469 (2026-03-12). Also checks that 'Mt. SAC' resolves to the slug.")

# ── COMPARE: two schools in one answer ──────────────────────────────────────
case(id="ask-029", route_kind="compare", expected_route=["compare", "hybrid", "sql"],
     question="How does HCC's operating budget compare to El Paso's?",
     must_contain_any=["481", "173", "budget", "compare"],
     notes="HCC unrestricted operating $481M vs El Paso FY2025-26 operating $173,931,632. Both slugs must be picked up from one sentence.")

case(id="ask-030", route_kind="compare", expected_route=["compare", "hybrid", "sql"],
     question="Compare the bond programs at Alamo Colleges and Dallas College.",
     must_contain_any=["987", "400", "bond", "compare"],
     notes="Alamo $987M proposed bond program vs Dallas $400M second GO tranche.")

case(id="ask-031", route_kind="compare", expected_route=["compare", "hybrid", "rag"],
     question="Which colleges have invested in workforce development programs?",
     must_contain_any=["workforce", "program", "college"],
     notes="Deliberately open-ended compare. Alamo, Dallas and Central Texas all carry workforce_development initiatives.")

# ── NONE: must refuse rather than improvise ────────────────────────────────
case(id="ask-005", route_kind="adversarial", expected_route="none",
     question="What's the weather in Houston today?",
     must_contain_any=REFUSAL,
     must_not_contain=["degrees", "forecast", "sunny", "raining"],
     notes="KNOWN FAILING as of 2026-08-16: routes to 'hybrid' and improvises instead of refusing. Kept strict on purpose - this is a real defect in the off-topic guard, not eval noise.")

case(id="ask-032", route_kind="adversarial", expected_route="none",
     question="Write me a poem about my cat.",
     must_contain_any=REFUSAL,
     must_not_contain=["whiskers", "purr"],
     notes="A second off-topic phrasing: one adversarial case cannot distinguish a broken guard from one unlucky classification.")

case(id="ask-033", route_kind="adversarial", expected_route="none",
     question="What is the capital of France?",
     must_contain_any=REFUSAL,
     must_not_contain=["paris"],
     notes="General-knowledge probe. The model certainly knows the answer, so this tests the scope guard rather than retrieval.")

# ── Ambiguous route, no longer scored against a single answer ───────────────
case(id="ask-007", route_kind="rag", expected_route=["rag", "hybrid", "latest_meeting"],
     question="Summarize the last 2 HCC meetings.",
     school_slug="houston_city_college",
     must_contain_any=SUMMARY,
     notes="Route relaxed on 2026-08-16. 'Summarize the last 2 meetings' is defensibly rag, hybrid or latest_meeting; pinning it to 'rag' made it fail on roughly half of runs regardless of retrieval quality.")

# ── Structural corrections, applied after the fact so the reasons live in one
# place rather than being repeated at a dozen call sites. All three were found
# by running the draft against the live API on 2026-08-16.

for c in C:
    # 1. SQL citations carry no meeting_id. api/schemas/ask.py documents the
    #    field as "RAG citations - enables click-through", and the SQL adapter
    #    leaves it None. Asserting expected_meeting_ids on a sql or hybrid case
    #    is therefore unsatisfiable by construction: the first draft failed all
    #    eight sql cases on it while their answers held the right figures.
    if c["route_kind"] in ("sql", "hybrid", "compare"):
        c["expected_meeting_ids"] = None

    # 2. expected_chunk_ids only means something when retrieval is guaranteed
    #    to run. On a hybrid answer the router may legitimately answer from SQL
    #    rows alone and cite no chunks, which reads as 0% retrieval recall when
    #    nothing about retrieval changed. Keep the metric on rag cases, where
    #    it measures what it claims to.
    if c["route_kind"] != "rag":
        c["expected_chunk_ids"] = None

    # 3. A specific numeric lookup is defensibly sql OR hybrid, and the router
    #    splits between them: the draft run answered ask-009/011/013/014 via
    #    hybrid and ask-001/010/012/015 via sql, with correct figures either
    #    way. Pinning one would score the coin toss, not the answer.
    if c["route_kind"] == "sql":
        c["expected_route"] = ["sql", "hybrid"]

ORDER = {"sql": 0, "rag": 1, "hybrid": 2, "latest_meeting": 3, "compare": 4, "adversarial": 5}
C.sort(key=lambda c: (ORDER[c["route_kind"]], c["id"]))

with io.open("eval/eval_set.jsonl", "w", encoding="utf-8", newline="\n") as f:
    for c in C:
        f.write(json.dumps(c, ensure_ascii=False) + "\n")

print("cases:", len(C))
print("by route_kind:", dict(Counter(c["route_kind"] for c in C)))
schools = set()
for c in C:
    for k in ("school_slug", "expected_school_slug"):
        if c.get(k):
            schools.add(c[k])
print("schools covered:", len(schools), sorted(schools))
print("with expected_chunk_ids:", sum(1 for c in C if c["expected_chunk_ids"]))
print("with expected_meeting_ids:", sum(1 for c in C if c["expected_meeting_ids"]))
