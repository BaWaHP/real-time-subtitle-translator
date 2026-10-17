from __future__ import annotations

from queue import Queue
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication

from config import AppConfig
from core.pipeline import Pipeline, SegmentCancellation
from core.subtitle_queue import SubtitleQueue, put_latest
from core.types import AudioChunk, SpeechSegment, Subtitle, Transcript


def sub(identifier):
    return Subtitle(identifier, f"Sentence {identifier}", "测试字幕", 0, 1, 2)


def test_subtitle_lifetime_begins_when_actually_shown():
    queue = SubtitleQueue()
    queue.push(sub(1))
    assert queue.tick(100) == (True, sub(1))
    queue.push(sub(2))
    assert queue.tick(101) == (False, sub(1))
    assert queue.tick(102) == (True, sub(2))
    assert queue.tick(103) == (False, sub(2))
    assert queue.tick(104) == (True, None)


def test_bounded_subtitle_backlog_preserves_active_and_order():
    queue = SubtitleQueue(max_pending=2)
    queue.push(sub(1))
    queue.tick(10)
    for n in (2, 3, 4):
        queue.push(sub(n))
    assert len(queue.pending) == 2
    assert queue.tick(11)[1].segment_id == 1
    assert queue.tick(12)[1].segment_id == 3
    assert not queue.push(sub(2))
    assert not queue.push(sub(4))


def test_audio_backpressure_never_blocks():
    queue = Queue(maxsize=2)
    for i in range(1000):
        put_latest(queue, i)
    assert [queue.get_nowait(), queue.get_nowait()] == [998, 999]


def pump_until(predicate, timeout=3):
    app = QCoreApplication.instance() or QCoreApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("worker did not finish within deadline")


@pytest.fixture
def fake_workers(monkeypatch):
    produced = []

    class Capture:
        def __init__(self, output_queue, stop_event, pause_event, **_):
            self.queue, self.stop, self.pause = output_queue, stop_event, pause_event

        def run(self):
            while not self.stop.wait(.02):
                if not self.pause.is_set():
                    put_latest(self.queue, AudioChunk(np.zeros(512, dtype=np.float32), time.monotonic()))
                    produced.append(1)

    class Vad:
        def __init__(self):
            self.n = 0

        def load(self):
            pass

        def reset(self):
            pass

        def feed(self, chunk):
            self.n += 1
            return [SpeechSegment(self.n, chunk.samples, chunk.started_at, chunk.started_at)]

    class Whisper:
        def __init__(self, **_):
            pass

        def load(self):
            pass

        def transcribe(self, s):
            return Transcript(s.segment_id, str(s.segment_id), s.started_at, s.ended_at)

    monkeypatch.setitem(sys.modules, "audio.capture", SimpleNamespace(LoopbackCapture=Capture))
    monkeypatch.setitem(sys.modules, "audio.vad", SimpleNamespace(VadSegmenter=Vad))
    monkeypatch.setitem(sys.modules, "asr.whisper_engine", SimpleNamespace(WhisperEngine=Whisper))
    monkeypatch.setitem(sys.modules, "translator.openai_translator",
                        SimpleNamespace(TranslationPaused=type("TranslationPaused", (Exception,), {}),
                                        TranslationCancelled=type("TranslationCancelled", (Exception,), {})))
    return produced


def test_slow_translation_keeps_capture_bounded_and_stop_suppresses_results(fake_workers):
    app = QCoreApplication.instance() or QCoreApplication([])
    began = threading.Event()
    release = threading.Event()

    class Translator:
        def translate(self, text, context, cancel_event=None):
            began.set()
            release.wait(2)
            return "译文"

    pipeline = Pipeline()
    received = []
    errors = []
    pipeline.subtitle.connect(received.append)
    pipeline.error.connect(errors.append)
    pipeline.start(AppConfig(), Translator())
    try:
        pump_until(began.is_set)
        time.sleep(.3)
        assert len(fake_workers) >= 8
        assert pipeline.transcript_queue.qsize() <= 4
        assert pipeline.segment_queue.qsize() <= 3
        assert pipeline._counts["dropped"] > 0
        pipeline.stop()
        release.set()
        pump_until(lambda: not pipeline.is_running)
        app.processEvents()
        assert received == []
        assert errors == []
    finally:
        release.set()
        pipeline.stop()
        pump_until(lambda: not pipeline.is_running)


def test_pause_discards_inflight_translation_then_resume_continues(fake_workers):
    began = threading.Event()
    release = threading.Event()
    calls = []

    class Translator:
        def translate(self, text, context, cancel_event=None):
            calls.append((text, context))
            if len(calls) == 1:
                began.set()
                release.wait(2)
            return "译文"

    pipeline = Pipeline()
    received = []
    pipeline.subtitle.connect(received.append)
    pipeline.start(AppConfig(), Translator())
    try:
        pump_until(began.is_set)
        pipeline.pause()
        release.set()
        time.sleep(.1)
        pump_until(lambda: pipeline.transcript_queue.empty())
        assert received == []
        pipeline.resume()
        pump_until(lambda: bool(received))
        assert received[0].english != calls[0][0]
        assert calls[1][1] == []  # no context from before the pause
    finally:
        release.set()
        pipeline.stop()
        pump_until(lambda: not pipeline.is_running)


def test_config_roundtrip_and_corrupt_settings(tmp_path):
    path = tmp_path / "settings.json"
    cfg = AppConfig(asr_model="tiny.en")
    cfg.save(path)
    assert AppConfig.load(path) == cfg
    path.write_text("bad json", encoding="utf-8")
    assert AppConfig.load(path) == AppConfig()


def test_pending_subtitles_expire_without_cutting_short_current():
    queue = SubtitleQueue(max_age=12)
    queue.push(sub(1))
    queue.tick(12)
    queue.push(sub(2))
    assert queue.tick(13) == (False, sub(1))
    assert queue.tick(14) == (True, None)
    assert queue.dropped == 1


def test_segment_retry_cancelled_by_deadline_or_pause():
    stop = threading.Event()
    deadline = SegmentCancellation(stop, lambda: True, time.monotonic() + .03)
    assert deadline.wait(.5)
    paused = SegmentCancellation(stop, lambda: False, time.monotonic() + 10)
    assert paused.is_set()
    assert paused.wait(0)


def test_worker_startup_failure_stops_every_started_thread(fake_workers, monkeypatch):
    class FailingWhisper:
        def __init__(self, **_):
            pass

        def load(self):
            raise RuntimeError("model unavailable")

    monkeypatch.setitem(sys.modules, "asr.whisper_engine", SimpleNamespace(WhisperEngine=FailingWhisper))
    pipeline = Pipeline()
    errors = []
    pipeline.error.connect(errors.append)
    pipeline.start(AppConfig(), object())
    pump_until(lambda: not pipeline.is_running)
    assert any("model unavailable" in message for message in errors)
    assert not any(thread.is_alive() for thread in pipeline._threads)
