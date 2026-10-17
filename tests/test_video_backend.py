from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import wave
import subprocess

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication

from config import AppConfig
from translator.openai_translator import TranslationCancelled, TranslationPaused
import httpx

from video.captions import normalize_cues, parse_captions, subtitle_segments
from video.media import MediaCancelled, MediaError, MediaReader, safe_response, validate_public_url
from video.pipeline import VideoPipeline
from video.timeline import CueTimeline, MediaClock
from video.types import Cue, MediaSource


def state(position=0, *, stamp=None, **values):
    return {"session_id": "edge-test", "source_id": "video-one", "title": "Video",
            "current_time": position, "received_at": time.monotonic() if stamp is None else stamp,
            "paused": True, "seeking": False, "ended": False, "playback_rate": 1,
            "duration": 120, **values}


def test_media_clock_handles_rate_pause_seek_and_stale():
    clock = MediaClock()
    assert clock.update(state(10, stamp=100, paused=False), 100) == (True, True)
    assert clock.position(100.5) == 10.5
    assert clock.update(state(10.5, stamp=100.5, paused=False, playback_rate=2), 100.5) == (False, False)
    assert clock.position(101) == 11.5
    assert clock.update(state(11.5, stamp=101, paused=True), 101) == (False, False)
    assert clock.position(102) == 11.5
    assert clock.position(103.01) is None
    for stamp in (102, 103, 104, 105, 106, 107):
        clock.update(state(11.5, stamp=stamp, paused=True), stamp)
        assert clock.position(stamp + .5) == 11.5  # paused for > 5 seconds, with live heartbeat
    assert clock.update(state(50, stamp=107.1, seeking=True), 107.1) == (False, True)
    assert clock.position(107.2) == 50
    assert clock.update(state(0, stamp=107.2, source_id="another"), 107.2) == (True, True)


def test_timeline_exact_boundaries_gaps_overlap_and_prefetch_bound():
    cues = [Cue(1, 2, "One"), Cue(2, 3, "Two"), Cue(4, 6, "Three"), Cue(5, 6, "Four")]
    timeline = CueTimeline(cues)
    assert timeline.at(0) is None
    assert timeline.at(2).text == "Two"
    assert timeline.at(3) is None
    assert timeline.at(5.5).text == "Four"
    assert timeline.at(6) is None
    assert [cue.text for cue in timeline.upcoming(1.5, 3, 2)] == ["One", "Two"]
    assert timeline.context(cues[2]) == ["One", "Two"]


def test_caption_formats_and_invalid_entry_bounds():
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000 align:start\n<b>Hello</b> &amp; world.\n"
    assert parse_captions(raw) == [Cue(1, 2, "Hello & world.")]
    assert parse_captions('{"events":[{"tStartMs":1000,"dDurationMs":1500,"segs":[{"utf8":"Hello"}]}]}') == [Cue(1, 2.5, "Hello")]
    assert parse_captions('<transcript><text start="1" dur="2">A &amp; B</text></transcript>') == [Cue(1, 3, "A & B")]
    values = [{"start": float("nan"), "end": 3, "text": "x"},
              {"start": -1, "end": 2, "text": "x"}, {"start": 0, "end": 1, "text": "x"}]
    assert normalize_cues(values) == [Cue(0, 1, "x")]


def test_rolling_and_word_caption_segments_are_merged_before_api_calls():
    rolling = [Cue(0, 2, "Hello"), Cue(1, 3, "Hello world"), Cue(2, 4, "world today"),
               Cue(5, 6, "A separate sentence")]
    assert subtitle_segments(rolling) == [Cue(0, 4, "Hello world today"), Cue(5, 6, "A separate sentence")]
    tiny = [Cue(index / 10, (index + 1) / 10, f"word{index}") for index in range(100)]
    grouped = subtitle_segments(tiny)
    assert len(grouped) == 3
    assert all(cue.end - cue.start <= 4.5 for cue in grouped)


def test_network_redirect_to_private_address_is_rejected(monkeypatch):
    def dns(host, port, **_):
        return [(2, 1, 6, "", ("127.0.0.1" if host == "localhost" else "1.1.1.1", port))]

    monkeypatch.setattr("video.media.socket.getaddrinfo", dns)
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://localhost/private"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MediaError, match="私网"):
            with safe_response(client, "https://public.example/video"):
                pass
    assert requests == ["https://public.example/video"]


@pytest.mark.parametrize("url", ["file:///c:/secret", "http://127.0.0.1/a", "http://192.168.1.2/a",
                                  "http://169.254.169.254/latest/meta-data", "http://user:pass@example.com/",
                                  "https://[::1]/video"])
def test_default_media_url_policy_rejects_non_public_or_credential_urls(url):
    with pytest.raises(MediaError):
        validate_public_url(url)


def test_media_reader_decodes_real_pcm_window_over_http(tmp_path):
    pytest.importorskip("imageio_ffmpeg")
    samples = np.sin(np.arange(48_000 * 3) * (2 * np.pi * 440 / 48_000)) * .25
    filename = tmp_path / "tone.wav"
    with wave.open(str(filename), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes((samples * 32767).astype("<i2").tobytes())

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reader = MediaReader(MediaSource(f"http://127.0.0.1:{server.server_port}/tone.wav"), allow_local=True)
    try:
        audio = reader.read(.75, .6)
        assert audio.dtype == np.float32
        assert abs(audio.size - 9600) < 64
        assert .15 < float(np.sqrt(np.mean(audio**2))) < .20
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(MediaCancelled):
            reader.read(0, 1, cancelled)
    finally:
        reader.close()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("media_format", ["mp4", "hls"])
def test_media_reader_real_mp4_and_hls_windows(tmp_path, media_format):
    ffmpeg = pytest.importorskip("imageio_ffmpeg")
    filename = tmp_path / ("audio.mp4" if media_format == "mp4" else "audio.m3u8")
    command = [ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-c:a", "aac"]
    if media_format == "mp4":
        command.extend(["-movflags", "+faststart"])
    else:
        command.extend(["-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
                        "-hls_segment_filename", str(tmp_path / "part%02d.ts")])
    command.append(str(filename))
    result = subprocess.run(command, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reader = MediaReader(MediaSource(f"http://127.0.0.1:{server.server_port}/{filename.name}"), allow_local=True)
    try:
        audio = reader.read(.75, .6)
        assert abs(audio.size - 9600) < 160
        assert float(np.sqrt(np.mean(audio**2))) > .05
    finally:
        reader.close()
        assert not reader.gateway.thread.is_alive()
        server.shutdown()
        server.server_close()


def pump_until(predicate, timeout=3):
    app = QCoreApplication.instance() or QCoreApplication([])
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("video worker did not reach expected state")


class Bridge:
    def __init__(self, position=0):
        self.state = state(position)
        self.values = [{"start": index * 2, "end": index * 2 + 1.5, "text": f"Sentence {index}"}
                       for index in range(100)]
        self.revision = 1

    def snapshot(self, session):
        return dict(self.state)

    def sessions(self):
        return [dict(self.state)]

    def captions(self, session, source):
        return list(self.values)

    def caption_revision(self, session, source):
        return self.revision

    def move(self, position=None, **values):
        self.state = {**self.state, **values, "received_at": time.monotonic()}
        if position is not None:
            self.state["current_time"] = position


class Translator:
    def __init__(self):
        self.calls = []
        self.closed = False

    def translate(self, text, context, cancel_event=None):
        if cancel_event and cancel_event.is_set():
            raise TranslationCancelled("cancelled")
        self.calls.append((text, context))
        return "译文 " + text

    def close(self):
        self.closed = True


@pytest.fixture
def video():
    app = QCoreApplication.instance() or QCoreApplication([])
    bridge, translator = Bridge(), Translator()
    pipeline = VideoPipeline()
    received, errors = [], []
    pipeline.subtitle.connect(received.append)
    pipeline.error.connect(errors.append)
    config = AppConfig()
    config.video_session_id = "edge-test"
    config.video_prefetch_seconds = 10
    config.video_transcript_cache_limit = 32
    pipeline.start(config, translator, bridge)
    yield pipeline, bridge, translator, received, errors
    pipeline.stop()
    pump_until(lambda: not pipeline.is_running)
    app.processEvents()


def test_video_prefetch_is_bounded_prioritizes_current_and_keeps_pause_display(video):
    pipeline, bridge, translator, received, errors = video
    pump_until(lambda: len(translator.calls) >= 5)
    assert translator.calls[0] == ("Sentence 0", [])
    assert all(int(text.split()[-1]) <= 5 for text, _ in translator.calls)
    assert len(translator.calls) <= 6
    bridge.move(0)
    pump_until(lambda: any(item and item.chinese == "译文 Sentence 0" for item in received))
    # Browser pause freezes on media time; no five-second wall-clock expiry.
    for _ in range(3):
        bridge.move(0)
        time.sleep(.1)
    assert received[-1].english == "Sentence 0"
    assert len(pipeline._cache) <= 32
    assert errors == []


def test_video_seek_chooses_media_cue_and_clears_gap_stale_and_source_cache(video):
    pipeline, bridge, translator, received, errors = video
    pump_until(lambda: any(item and item.chinese == "译文 Sentence 0" for item in received))
    bridge.move(40)
    pump_until(lambda: received[-1] and received[-1].english == "Sentence 20")
    bridge.move(41.8)
    pump_until(lambda: received[-1] is None)
    bridge.move(40)
    pump_until(lambda: received[-1] is not None)
    bridge.state["received_at"] = time.monotonic() - 3
    pump_until(lambda: received[-1] is None)
    bridge.values = [{"start": 0, "end": 4, "text": "Different source"}]
    bridge.revision += 1
    bridge.move(0, source_id="video-two")
    pump_until(lambda: received[-1] and received[-1].chinese == "译文 Different source")
    assert list(pipeline._cache.values()) == ["译文 Different source"]
    assert errors == []


def test_video_app_pause_cancels_and_resume_reuses_completed_cache(video):
    pipeline, bridge, translator, received, errors = video
    pump_until(lambda: any(item and item.chinese == "译文 Sentence 0" for item in received))
    pipeline.pause()
    pump_until(lambda: received[-1] is None)
    previous = len(translator.calls)
    bridge.move(2)
    time.sleep(.15)
    assert len(translator.calls) == previous
    pipeline.resume()
    pump_until(lambda: received[-1] and received[-1].english == "Sentence 1")
    assert errors == []


def test_slow_video_translation_is_cancelled_on_seek_and_does_not_block_clock():
    bridge = Bridge()
    began = threading.Event()
    tokens = []

    class Slow(Translator):
        def translate(self, text, context, cancel_event=None):
            self.calls.append((text, context))
            tokens.append(cancel_event)
            if len(self.calls) == 1:
                began.set()
                if cancel_event.wait(2):
                    raise TranslationCancelled("seek")
            return "译文 " + text

    translator, pipeline, received = Slow(), VideoPipeline(), []
    pipeline.subtitle.connect(received.append)
    config = AppConfig()
    config.video_prefetch_seconds = 10
    pipeline.start(config, translator, bridge)
    try:
        pump_until(began.is_set)
        bridge.move(40)
        pump_until(lambda: tokens[0].is_set())
        pump_until(lambda: received[-1] and received[-1].chinese == "译文 Sentence 20")
        assert all(not item or item.chinese != "译文 Sentence 0" for item in received)
        assert len(translator.calls) < 10
    finally:
        pipeline.stop()
        pump_until(lambda: not pipeline.is_running)


def test_quota_pause_stops_all_future_video_translation_requests():
    class Quota(Translator):
        def translate(self, text, context, cancel_event=None):
            self.calls.append((text, context))
            raise TranslationPaused("quota")

    bridge, translator, pipeline = Bridge(), Quota(), VideoPipeline()
    errors = []
    pipeline.error.connect(errors.append)
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(lambda: bool(errors))
        bridge.move(10)
        time.sleep(.2)
        assert len(translator.calls) == 1
        assert "quota" in errors[0]
    finally:
        pipeline.stop()
        pump_until(lambda: not pipeline.is_running)


def test_native_caption_arrival_cancels_audio_fallback_and_never_mixes_asr(monkeypatch):
    bridge = Bridge()
    bridge.values = []
    original_snapshot = bridge.snapshot
    monkeypatch.setattr(bridge, "snapshot", lambda session: {
        **original_snapshot(session), "received_at": time.monotonic()})
    entered, closed = threading.Event(), threading.Event()

    class Resolver:
        def resolve(self, snapshot, cancel_event):
            return MediaSource("https://public.example/audio.mp4")

    class Reader:
        def __init__(self, *_):
            pass

        def read(self, start, duration, cancel_event):
            entered.set()
            if cancel_event.wait(2):
                raise MediaCancelled("native subtitles arrived")
            raise AssertionError("caption arrival failed to cancel audio prefetch")

        def close(self):
            closed.set()

    monkeypatch.setattr("video.pipeline.MediaResolver", Resolver)
    monkeypatch.setattr("video.pipeline.MediaReader", Reader)
    pipeline, received = VideoPipeline(), []
    pipeline.subtitle.connect(received.append)
    pipeline.start(AppConfig(), Translator(), bridge)
    try:
        pump_until(entered.is_set, timeout=4)
        bridge.values = [{"start": 0, "end": 2, "text": "Native captions"}]
        bridge.revision += 1
        bridge.move(0)
        pump_until(closed.is_set)
        pump_until(lambda: received[-1] and received[-1].chinese == "译文 Native captions")
        assert pipeline._caption_origin == "browser"
        assert [cue.text for cue in pipeline._timeline.cues] == ["Native captions"]
    finally:
        pipeline.stop()
        pump_until(lambda: not pipeline.is_running)


def test_pipeline_stops_cleanly_if_watch_fails_before_prepare_launch(monkeypatch):
    pipeline = VideoPipeline()
    called = []

    def bad_watch():
        raise RuntimeError("failed immediately")

    def prepare():
        called.append(True)
        while not pipeline._stop.wait(.01):
            pass

    monkeypatch.setattr(pipeline, "_watch", bad_watch)
    monkeypatch.setattr(pipeline, "_prepare", prepare)
    translator = Translator()
    pipeline.start(AppConfig(), translator, Bridge())
    pump_until(lambda: not pipeline.is_running)
    assert translator.closed
    assert all(not thread.is_alive() for thread in pipeline._threads)


def test_video_translation_errors_have_finite_retry_and_bounded_failure_cache():
    class Broken(Translator):
        def translate(self, text, context, cancel_event=None):
            self.calls.append((text, context))
            raise RuntimeError("simulated network outage")

    bridge, translator, pipeline = Bridge(), Broken(), VideoPipeline()
    bridge.values = [{"start": 0, "end": 10, "text": "One long subtitle"}]
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(lambda: len(translator.calls) == 1)
        for count in (2, 3):
            # Advance just the retry deadline; retain real media/cancel clocks.
            with pipeline._lock:
                for key, value in list(pipeline._translation_errors.items()):
                    pipeline._translation_errors[key] = (value[0], 0)
            pump_until(lambda: len(translator.calls) == count)
        time.sleep(.2)
        assert len(translator.calls) == 3
        assert list(pipeline._cache.values()) == ["[本段翻译暂不可用]"]
        assert len(pipeline._translation_errors) <= pipeline._cache_limit
    finally:
        pipeline.stop()
        pump_until(lambda: not pipeline.is_running)
