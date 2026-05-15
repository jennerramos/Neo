import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from pipeline.sources import ravnur


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ravnur"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_listing_parser_filters_captioned_board_meetings(monkeypatch):
    adapter = ravnur.RavnurAdapter()
    monkeypatch.setattr(adapter, "_get_json", lambda url: load_json("media_all.json"))
    school = SimpleNamespace(
        discovery_config={"portal_url": "https://mediaportal.dallascollege.edu"}
    )

    meetings = list(adapter.discover_meetings(school))

    # Post-DATE_CUTOFF (2023-04-08) the Dallas fixture yields ~96 board meetings;
    # the upper bound is the raw "Board Meetings" Organization count.
    assert 50 <= len(meetings) <= 161
    assert all(m.published_date is None or m.published_date >= ravnur.DATE_CUTOFF for m in meetings)
    meeting = next(item for item in meetings if item.video_id == "mDe9VD")
    assert meeting.title == "Regular Board Meeting - May 12, 2026"
    assert meeting.duration_seconds == 3211
    assert meeting.published_date == date(2026, 5, 13)
    assert meeting.raw_metadata["ravnur_category"] == "Regular Board Meetings"


def test_no_captions_path(monkeypatch):
    adapter = ravnur.RavnurAdapter()
    monkeypatch.setattr(adapter, "_get_json", lambda url: {"mediaSources": [{"cc": []}]})
    meeting = SimpleNamespace(
        video_id="mDe9VD",
        video_url="https://mediaportal.dallascollege.edu/media/mDe9VD",
    )

    result = adapter.fetch_captions(meeting)

    assert result.success is False
    assert result.reason == "no_captions"


def test_approved_en_track_selection(monkeypatch):
    adapter = ravnur.RavnurAdapter()
    calls = []
    src = "https://example.com/cc/en-US?format=vtt"
    monkeypatch.setattr(
        adapter,
        "_get_json",
        lambda url: {
            "mediaSources": [
                {
                    "cc": [
                        {"src": "https://example.com/cc/es?format=vtt", "srclang": "es", "stateName": "Draft"},
                        {"src": src, "srclang": "en-US", "stateName": "Approved"},
                        {"src": "https://example.com/cc/vi?format=vtt", "srclang": "vi", "stateName": "Draft"},
                    ]
                }
            ]
        },
    )

    def fake_get_bytes(url):
        calls.append(url)
        return b"WEBVTT\n\ncontent"

    monkeypatch.setattr(adapter, "_get_bytes", fake_get_bytes)
    meeting = SimpleNamespace(
        video_id="mDe9VD",
        video_url="https://mediaportal.dallascollege.edu/media/mDe9VD",
    )

    result = adapter.fetch_captions(meeting)

    assert result.success is True
    assert calls == [src]


def test_vtt_pass_through(monkeypatch):
    adapter = ravnur.RavnurAdapter()
    fixture = (FIXTURES / "captions_mDe9VD.vtt").read_bytes()
    monkeypatch.setattr(
        adapter,
        "_get_json",
        lambda url: {
            "mediaSources": [
                {
                    "cc": [
                        {
                            "src": "https://mediaportal.dallascollege.edu/api/v1.0/source/mDe9VD/cc/en-US?format=vtt",
                            "srclang": "en-US",
                            "stateName": "Approved",
                        }
                    ]
                }
            ]
        },
    )
    monkeypatch.setattr(adapter, "_get_bytes", lambda url: fixture)
    meeting = SimpleNamespace(
        video_id="mDe9VD",
        video_url="https://mediaportal.dallascollege.edu/media/mDe9VD",
    )

    result = adapter.fetch_captions(meeting)

    assert result.success is True
    assert result.reason == "fetched"
    assert result.vtt_bytes == fixture


def test_host_parsing_for_source_detail_url(monkeypatch):
    adapter = ravnur.RavnurAdapter()
    calls = []

    def fake_get_json(url):
        calls.append(url)
        return {"mediaSources": [{"cc": []}]}

    monkeypatch.setattr(adapter, "_get_json", fake_get_json)
    meeting = SimpleNamespace(
        video_id="mDe9VD",
        video_url="https://mediaportal.dallascollege.edu/media/mDe9VD",
    )

    adapter.fetch_captions(meeting)

    assert calls == ["https://mediaportal.dallascollege.edu/api/v1.0/source/mDe9VD"]
