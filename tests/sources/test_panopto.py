import json
import re
from pathlib import Path
from types import SimpleNamespace

from pipeline.sources import panopto


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "panopto"
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_board_page_parser_extracts_unique_guids():
    for name in ("acc_board_page.html", "alamo_board_page.html"):
        html = (FIXTURES / name).read_text(encoding="utf-8")
        guids = panopto._extract_guids(html)
        assert len(guids) >= 10
        assert len(guids) == len(set(guids))
        assert all(GUID_RE.match(guid) for guid in guids)


def test_delivery_info_parser_shape():
    data = load_json("delivery_info_acc.json")
    delivery = data["Delivery"]
    assert delivery["SessionName"] == "September 8, 2025: Work Session"
    assert delivery["HasCaptions"] is True
    assert isinstance(delivery["Duration"], float)
    assert isinstance(delivery["SessionStartTime"], float)


def test_caption_json_to_vtt():
    events = load_json("captions_acc_page0.json") + load_json("captions_acc_last.json")
    output = panopto._events_to_vtt(events, session_start_time=13401921239.996)

    assert output.startswith(b"WEBVTT\n\n")
    assert b"Good afternoon" in output

    first_timestamp = output.decode("utf-8").splitlines()[2].split(" --> ")[0]
    hours, minutes, seconds = first_timestamp.split(":")
    start = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    assert 19.0 <= start <= 19.5

    expected_cues = sum(1 for event in events if str(event.get("Metadata") or "").strip())
    assert output.count(b" --> ") == expected_cues


def test_pagination_loop_termination(monkeypatch):
    adapter = panopto.PanoptoAdapter()
    calls = []

    def fake_post_delivery_info(host, guid):
        return {
            "Delivery": {
                "HasCaptions": True,
                "SessionStartTime": 13401921239.996,
            }
        }

    def fake_fetch_caption_page(host, guid, page, page_size):
        calls.append(page)
        return [], page == 0

    monkeypatch.setattr(panopto, "_post_delivery_info", fake_post_delivery_info)
    monkeypatch.setattr(adapter, "_fetch_caption_page", fake_fetch_caption_page)

    meeting = SimpleNamespace(
        video_id="1e07cc16-9e4d-4024-ad4f-b3530147e1d2",
        video_url="https://austincc.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=1e07cc16-9e4d-4024-ad4f-b3530147e1d2",
    )
    result = adapter.fetch_captions(meeting)

    assert result.success is True
    assert calls == [0, 1]


def test_no_captions_path(monkeypatch):
    adapter = panopto.PanoptoAdapter()

    def fake_post_delivery_info(host, guid):
        return {"Delivery": {"HasCaptions": False}}

    monkeypatch.setattr(panopto, "_post_delivery_info", fake_post_delivery_info)

    meeting = SimpleNamespace(
        video_id="1e07cc16-9e4d-4024-ad4f-b3530147e1d2",
        video_url="https://austincc.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=1e07cc16-9e4d-4024-ad4f-b3530147e1d2",
    )
    result = adapter.fetch_captions(meeting)

    assert result.success is False
    assert result.reason == "no_captions"
