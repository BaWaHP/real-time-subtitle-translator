"""Video latency regressions with deterministic local translators, no OAuth."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QCoreApplication

from config import AppConfig
from translator.openai_translator import TranslationCancelled, TranslationError
from video.pipeline import VideoPipeline
from video.types import Cue, MediaSource
from video.media import MediaError


def pump_until(predicate, timeout=3):
    app = QCoreApplication.instance() or QCoreApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("video latency regression timed out")


class Bridge:
    def __init__(self, cues):
        self.values = [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in cues]
        self.revision = 1
        self.state = {"session_id": "latency-test", "source_id": "video-one", "title": "Test",
                      "current_time": 0, "paused": True, "seeking": False,
                      "ended": False, "playback_rate": 1, "duration": 300}

    def snapshot(self, _session):
        # This test bridge represents regular browser heartbeats.
        return {**self.state, "received_at": time.monotonic()}

    def sessions(self):
        return [self.snapshot("")]

    def captions(self, _session, _source):
        return list(self.values)

    def caption_revision(self, _session, _source):
        return self.revision

    def move(self, position, **values):
        self.state = {**self.state, "current_time": position, **values}


class BatchTranslator:
    def __init__(self):
        self.calls = []
        self.closed = False

    def translate(self, text, context, cancel_event=None):
        self.calls.append(([text], context))
        return "译文 " + text

    def translate_batch(self, texts, context, cancel_event=None):
        self.calls.append((texts, context))
        if cancel_event.wait(.04):
            raise TranslationCancelled("cancelled")
        return ["译文 " + text for text in texts]

    def close(self):
        self.closed = True


def finish(pipeline):
    pipeline.stop()
    pump_until(lambda: not pipeline.is_running)


def test_batching_builds_45_second_buffer_in_two_bounded_round_trips():
    cues = [Cue(index * 3, index * 3 + 2, f"Sentence {index}") for index in range(50)]
    bridge, translator, pipeline = Bridge(cues), BatchTranslator(), VideoPipeline()
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(lambda: cues[15].key in pipeline._cache)
        # Sixteen subtitle timings share two network waits instead of sixteen.
        assert [len(texts) for texts, _ in translator.calls] == [8, 8]
        assert translator.calls[0][0][0] == "Sentence 0"
        assert translator.calls[1][1] == ["Sentence 5", "Sentence 6", "Sentence 7"]
        assert len(pipeline._cache) == 16
        assert pipeline._last_request_seconds >= .03
        with pipeline._lock:
            upcoming = pipeline._timeline.upcoming(0, pipeline._prefetch)
            assert pipeline._prepared_until(0, upcoming) == 45
    finally:
        finish(pipeline)


def test_expired_first_item_does_not_discard_useful_future_batch_items():
    cues = [Cue(0, .1, "Expired"), Cue(.4, 1.4, "Current"), Cue(2, 3, "Future")]
    began, release = threading.Event(), threading.Event()
    tokens = []

    class Held(BatchTranslator):
        def translate_batch(self, texts, context, cancel_event=None):
            self.calls.append((texts, context))
            tokens.append(cancel_event)
            began.set()
            release.wait(2)
            return ["译文 " + text for text in texts]

    bridge, translator, pipeline, received = Bridge(cues), Held(), VideoPipeline(), []
    pipeline.subtitle.connect(received.append)
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(began.is_set)
        # Less than the clock's seek threshold: the first cue simply expires.
        bridge.move(.5)
        pump_until(lambda: received[-1] and received[-1].english == "Current")
        assert not tokens[0].is_set()
        release.set()
        pump_until(lambda: received[-1] and received[-1].chinese == "译文 Current")
        assert cues[0].key not in pipeline._cache
        assert pipeline._cache[cues[2].key] == "译文 Future"
        assert len(translator.calls) == 1
    finally:
        release.set()
        finish(pipeline)


def test_seek_cancels_entire_batch_and_only_commits_new_position():
    began, cancelled = threading.Event(), threading.Event()

    class SlowFirst(BatchTranslator):
        def translate_batch(self, texts, context, cancel_event=None):
            self.calls.append((texts, context))
            if len(self.calls) == 1:
                began.set()
                if cancel_event.wait(2):
                    cancelled.set()
                    raise TranslationCancelled("seek")
            return ["译文 " + text for text in texts]

    cues = [Cue(index * 3, index * 3 + 2, f"Sentence {index}") for index in range(60)]
    bridge, translator, pipeline, received = Bridge(cues), SlowFirst(), VideoPipeline(), []
    pipeline.subtitle.connect(received.append)
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(began.is_set)
        bridge.move(60)
        pump_until(cancelled.is_set)
        pump_until(lambda: received[-1] and received[-1].chinese == "译文 Sentence 20")
        assert all(int(text.removeprefix("译文 Sentence ")) >= 20
                   for text in pipeline._cache.values())
        assert all(not item or item.chinese != "译文 Sentence 0" for item in received)
    finally:
        finish(pipeline)


def test_blocked_translation_never_blocks_live_english_clock_updates():
    began, release = threading.Event(), threading.Event()

    class Blocked(BatchTranslator):
        def translate_batch(self, texts, context, cancel_event=None):
            self.calls.append((texts, context))
            if len(self.calls) == 1:
                began.set()
                # A deliberately uncooperative fake verifies the clock thread
                # itself is independent, even before HTTP cancellation exits.
                release.wait(2)
            return ["译文 " + text for text in texts]

    bridge = Bridge([Cue(index * 3, index * 3 + 2, f"Sentence {index}") for index in range(8)])
    pipeline, translator, received = VideoPipeline(), Blocked(), []
    pipeline.subtitle.connect(received.append)
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(began.is_set)
        bridge.move(3.2)
        pump_until(lambda: received[-1] and received[-1].english == "Sentence 1")
        assert received[-1].chinese == "[翻译准备中]"
        assert not release.is_set()
        assert len(translator.calls) == 1
        release.set()
        pump_until(lambda: received[-1] and received[-1].chinese == "译文 Sentence 1")
    finally:
        release.set()
        finish(pipeline)


def test_invalid_batch_disables_batch_once_then_falls_back_to_single_cues():
    class Invalid(BatchTranslator):
        def translate_batch(self, texts, context, cancel_event=None):
            self.calls.append((texts, context))
            raise TranslationError("bad batch format", code="invalid_subtitle_batch")

    cues = [Cue(index * 3, index * 3 + 2, f"Sentence {index}") for index in range(8)]
    bridge, translator, pipeline = Bridge(cues), Invalid(), VideoPipeline()
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(lambda: len(pipeline._cache) == 8)
        assert [len(texts) for texts, _ in translator.calls] == [8] + [1] * 8
        assert pipeline._batch_disabled
        assert pipeline._translation_errors == {}
        assert "[本段翻译暂不可用]" not in pipeline._cache.values()
    finally:
        finish(pipeline)


def test_late_browser_captions_translate_while_public_resolver_is_still_blocked(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Resolver:
        def resolve(self, snapshot, cancel_event):
            entered.set()
            # Model an extractor that checks cancellation only after network I/O.
            release.wait(2)
            return MediaSource("https://public.example/audio.mp4")

    monkeypatch.setattr("video.pipeline.MediaResolver", Resolver)
    bridge, translator, pipeline, received = Bridge([]), BatchTranslator(), VideoPipeline(), []
    pipeline.subtitle.connect(received.append)
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(lambda: pipeline._clock.state is not None)
        pipeline._source_connected_at = time.monotonic() - 3
        pump_until(entered.is_set)
        bridge.values = [{"start": 0, "end": 5, "text": "Late browser captions"}]
        bridge.revision += 1
        pump_until(lambda: received[-1] and received[-1].chinese == "译文 Late browser captions")
        assert not release.is_set()
        assert pipeline._caption_origin == "browser"
    finally:
        release.set()
        finish(pipeline)


def test_browser_caption_recovery_clears_the_old_public_extraction_status(monkeypatch):
    class UnavailablePublicMedia:
        def resolve(self, snapshot, cancel_event):
            raise MediaError("Public media needs login")
    monkeypatch.setattr("video.pipeline.MediaResolver", UnavailablePublicMedia)
    bridge, pipeline = Bridge([]), VideoPipeline()
    errors, statuses = [], []
    pipeline.error.connect(errors.append)
    pipeline.status.connect(statuses.append)
    pipeline.start(AppConfig(), BatchTranslator(), bridge)
    try:
        pump_until(lambda: pipeline._clock.state is not None)
        pipeline._source_connected_at = time.monotonic() - 3
        pump_until(lambda: bool(errors))
        bridge.values = [{"start": 0, "end": 10, "text": "Panopto captions from the browser"}]
        bridge.revision += 1
        pump_until(lambda: any("已读取播放器英文字幕" in value for value in statuses))
        assert pipeline._caption_origin == "browser"
        pump_until(lambda: bool(pipeline._cache))
    finally:
        finish(pipeline)


def test_panopto_browser_diagnostic_prevents_pointless_public_login_extraction(monkeypatch):
    class MustNotExtract:
        def resolve(self, *_args):
            raise AssertionError("Authenticated Panopto diagnostic must be surfaced directly")
    monkeypatch.setattr("video.pipeline.MediaResolver", MustNotExtract)
    bridge, pipeline, errors = Bridge([]), VideoPipeline(), []
    bridge.state["page_url"] = "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=fixture"
    bridge.warning = lambda _session: "Panopto 默认字幕未标注语言，请在扩展确认默认字幕是英语。"
    pipeline.error.connect(errors.append)
    pipeline.start(AppConfig(), BatchTranslator(), bridge)
    try:
        pump_until(lambda: bool(errors))
        assert errors == [bridge.warning("")]
    finally:
        finish(pipeline)


def test_prepared_buffer_stops_at_first_missing_cue_despite_farther_cache():
    pipeline = VideoPipeline()
    pipeline._prefetch = 45
    cues = [Cue(0, 2, "Now"), Cue(3, 5, "Missing"), Cue(6, 8, "Already future")]
    pipeline._cache[cues[2].key] = "后续已完成"
    assert pipeline._prepared_until(1, cues) == 1
    pipeline._cache[cues[0].key] = "当前已完成"
    assert pipeline._prepared_until(1, cues) == 3
    assert pipeline._prepared_until(9, []) == 9


def test_request_metrics_identify_actual_work_and_elapsed_time():
    began, release = threading.Event(), threading.Event()

    class Held(BatchTranslator):
        def translate_batch(self, texts, context, cancel_event=None):
            self.calls.append((texts, context))
            began.set()
            release.wait(2)
            return ["译文 " + text for text in texts]

    bridge = Bridge([Cue(index * 3, index * 3 + 2, f"Sentence {index}") for index in range(8)])
    pipeline, translator, metrics = VideoPipeline(), Held(), []
    pipeline.metrics.connect(metrics.append)
    pipeline.start(AppConfig(), translator, bridge)
    try:
        pump_until(began.is_set)
        pump_until(lambda: any(item.get("video_translation_inflight") == 8
                               and item["video_translation_request_seconds"] > .3 for item in metrics))
        busy = next(item for item in reversed(metrics) if item["video_translation_inflight"])
        assert busy["video_caption_origin"] == "browser"
        assert busy["video_prefetch_seconds"] == 0
        assert busy["video_state_age"] < .2
        release.set()
        pump_until(lambda: any(item.get("video_translation_inflight") == 0
                               and item["video_translation_last_seconds"] > .3 for item in metrics))
    finally:
        release.set()
        finish(pipeline)


def test_batch_time_and_text_bounds_and_legacy_translator_compatibility():
    pipeline = VideoPipeline()
    pipeline.translator = BatchTranslator()
    cues = [Cue(index * 5, index * 5 + 1, f"Sentence {index}") for index in range(20)]
    assert len(pipeline._translation_batch(cues)) == 5
    long = [Cue(index * 2, index * 2 + 1, "word " * 300) for index in range(20)]
    assert sum(len(cue.text) for cue in pipeline._translation_batch(long)) <= 8000
    pipeline.translator = object()
    assert pipeline._translation_batch(cues) == cues[:1]
