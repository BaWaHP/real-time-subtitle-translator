from __future__ import annotations

import math
import time

import httpx
import pytest

from video.bridge import BridgeError, EdgeBridge
from video.timeline import MediaClock


def state(**changes):
    value = {"type": "state", "session_id": "tab:1", "source_id": "video:a",
             "title": "Example", "page_url": "https://example.com/watch",
             "media_url": "https://example.com/movie.mp4", "current_time": 1.0,
             "duration": 120.0, "playback_rate": 1.0, "paused": False}
    return {**value, **changes}


def captions(source="video:a"):
    return {"type": "captions", "session_id": "tab:1", "source_id": source,
            "language": "en-US", "cues": [{"start": 0, "end": 5, "text": "Hello."}]}


def test_pairing_token_origin_and_host_are_required():
    bridge = EdgeBridge(0)
    bridge.start()
    try:
        base = f"http://127.0.0.1:{bridge.port}"
        with httpx.Client(base_url=base, trust_env=False) as client:
            assert client.get("/v1/health").status_code == 401
            headers = {"Authorization": "Bearer " + bridge.token}
            assert client.get("/v1/health", headers=headers).json()["ok"]
            assert client.get("/v1/health", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
            assert client.get("/v1/health", headers={**headers, "Host": "evil.example"}).status_code == 403
            origin = "chrome-extension://" + "a" * 32
            response = client.post("/v1/event", json=state(), headers={**headers, "Origin": origin})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
            assert len(bridge.sessions()) == 1
    finally:
        bridge.stop()
    assert not bridge._thread.is_alive()


def test_source_change_and_disconnect_clear_old_captions():
    bridge = EdgeBridge()
    bridge.receive(state())
    bridge.receive(captions())
    assert len(bridge.captions("tab:1", "video:a")) == 1
    bridge.receive(state(source_id="video:b"))
    assert bridge.captions("tab:1", "video:a") == []
    with pytest.raises(BridgeError, match="inactive"):
        bridge.receive(captions())
    bridge.receive(captions("video:b"))
    bridge.receive({"type": "disconnect", "session_id": "tab:1"})
    assert bridge.snapshot("tab:1") is None
    assert bridge.captions("tab:1", "video:b") == []


@pytest.mark.parametrize("change", [{"current_time": math.nan}, {"playback_rate": 0},
                                    {"paused": "false"}, {"media_url": "filex://x"}])
def test_untrusted_state_rejected(change):
    with pytest.raises(BridgeError):
        EdgeBridge().receive(state(**change))


def test_clock_is_server_assigned_and_stale_session_hidden(monkeypatch):
    bridge = EdgeBridge()
    now = time.monotonic()
    bridge.receive(state(received_at=now + 10000))
    assert bridge.snapshot("tab:1")["received_at"] < now + 1
    monkeypatch.setattr("video.bridge.time.monotonic", lambda: now + 4)
    assert bridge.sessions() == []


def test_snapshot_is_independent_and_caption_shapes_validated():
    bridge = EdgeBridge()
    bridge.receive(state())
    snapshot = bridge.snapshot("tab:1")
    snapshot["current_time"] = 99
    assert bridge.snapshot("tab:1")["current_time"] == 1
    event = captions()
    event["cues"][0]["end"] = -1
    with pytest.raises(BridgeError):
        bridge.receive(event)


def test_capture_age_corrects_clock_without_advancing_pause_seek_or_buffering(monkeypatch):
    monkeypatch.setattr("video.bridge.time.time", lambda: 1000.0)
    monkeypatch.setattr("video.bridge.time.monotonic", lambda: 100.0)
    for flags in ({}, {"paused": True}, {"seeking": True}, {"buffering": True}):
        bridge, clock = EdgeBridge(), MediaClock()
        bridge.receive(state(current_time=10, sampled_at_ms=999_000, playback_rate=2, **flags))
        sample = bridge.snapshot("tab:1")
        assert sample["received_at"] == 99
        clock.update(sample, 100)
        assert clock.position(100) == (12 if not flags else 10)
        assert clock.position(101.01) is None  # never freshen an old sample


@pytest.mark.parametrize("timestamp", [997_999, 1_000_251, math.inf, "999000", True])
def test_expired_future_or_invalid_capture_timestamps_are_rejected(monkeypatch, timestamp):
    monkeypatch.setattr("video.bridge.time.time", lambda: 1000.0)
    with pytest.raises(BridgeError):
        EdgeBridge().receive(state(sampled_at_ms=timestamp))


def test_late_request_cannot_rewind_newer_playback_or_reactivate_old_source(monkeypatch):
    monkeypatch.setattr("video.bridge.time.time", lambda: 1000.0)
    bridge = EdgeBridge()
    bridge.receive(state(current_time=20, source_id="new", sampled_at_ms=999_900))
    bridge.receive(state(current_time=10, source_id="old", sampled_at_ms=999_500))
    assert bridge.snapshot("tab:1")["current_time"] == 20
    assert bridge.snapshot("tab:1")["source_id"] == "new"


def test_pc_wall_clock_adjustment_does_not_lock_out_new_heartbeats(monkeypatch):
    wall = [1000.0]
    monkeypatch.setattr("video.bridge.time.time", lambda: wall[0])
    bridge = EdgeBridge()
    bridge.receive(state(current_time=20, sampled_at_ms=1_000_000))
    wall[0] = 900.0
    bridge.receive(state(current_time=21, sampled_at_ms=900_000))
    assert bridge.snapshot("tab:1")["current_time"] == 21
