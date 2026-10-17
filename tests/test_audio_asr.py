from __future__ import annotations

import queue
from types import SimpleNamespace

import numpy as np
import pytest

from audio.capture import LoopbackCapture, StreamingConverter, _devices, put_latest
from audio.vad import VadSegmenter, FRAME_SAMPLES
from asr.whisper_engine import WhisperEngine
from core.types import AudioChunk, SpeechSegment


def chunk(seconds, value=1.0, started_at=100.0):
    return AudioChunk(np.full(round(seconds * 16000), value, dtype=np.float32), started_at)


def energy_probability(frame):
    return .99 if np.max(np.abs(frame)) > .5 else .01


def test_queue_overload_discards_oldest_without_blocking():
    target = queue.Queue(maxsize=2)
    assert not put_latest(target, "a")
    assert not put_latest(target, "b")
    assert put_latest(target, "c")
    assert [target.get_nowait(), target.get_nowait()] == ["b", "c"]


def test_converter_downmixes_resamples_and_preserves_time():
    converter = StreamingConverter(48000, 2)
    frequency = 440
    mono = (.4 * np.sin(2 * np.pi * frequency * np.arange(48000) / 48000))
    stereo = np.stack([mono, mono], axis=1)
    pcm = (stereo * 32767).astype("<i2")
    chunks = []
    for begin in range(0, 48000, 960):
        result = converter.convert(pcm[begin:begin + 960].tobytes(), 10 + begin / 48000)
        if result is not None:
            chunks.append(result)
    output = np.concatenate([c.samples for c in chunks])
    assert 15500 < len(output) <= 16000  # Stateful filter holds a small tail.
    assert output.dtype == np.float32 and output.ndim == 1
    assert chunks[0].started_at == 10
    for previous, current in zip(chunks, chunks[1:]):
        assert current.started_at == pytest.approx(previous.started_at + len(previous.samples) / 16000)
    peak = np.argmax(abs(np.fft.rfft(output))) * 16000 / len(output)
    assert peak == pytest.approx(frequency, abs=2)
    restarted = converter.convert(pcm[:960].tobytes(), 30)
    # May be buffered until another input arrives after reset.
    if restarted is None:
        restarted = converter.convert(pcm[960:1920].tobytes(), 30.02)
    assert restarted is not None and restarted.started_at == 30


def test_device_enumeration_excludes_microphones():
    class Manager:
        def get_default_wasapi_loopback(self):
            return {"index": 7}
        def get_loopback_device_info_generator(self):
            return iter([
                {"index": 7, "name": "Speakers [Loopback]", "isLoopbackDevice": True,
                 "maxInputChannels": 2, "defaultSampleRate": 48000},
                {"index": 8, "name": "Microphone", "isLoopbackDevice": False,
                 "maxInputChannels": 1, "defaultSampleRate": 44100},
            ])
    devices = _devices(Manager())
    assert len(devices) == 1
    assert devices[0].default and devices[0].name == "Speakers [Loopback]"


def test_reconnect_matches_loopback_name_after_device_index_changes():
    import threading
    from audio.capture import AudioDevice
    capture = LoopbackCapture(queue.Queue(maxsize=1), threading.Event(), threading.Event(),
                              device_name="USB Headset [Loopback]")
    class Manager:
        def get_default_wasapi_loopback(self):
            return {"index": 99}
        def get_loopback_device_info_generator(self):
            return iter([{"index": 99, "name": "USB Headset [Loopback]",
                          "isLoopbackDevice": True, "maxInputChannels": 2,
                          "defaultSampleRate": 48000}])
    assert capture._select(Manager()) == AudioDevice(99, "USB Headset [Loopback]", True, 2, 48000)


def test_vad_ends_after_half_second_silence_and_filters_silence():
    vad = VadSegmenter(probability_fn=energy_probability)
    assert vad.feed(chunk(.8, 0)) == []
    assert vad.feed(chunk(1, started_at=100.8)) == []
    assert vad.feed(chunk(.45, 0, 101.8)) == []
    result = vad.feed(chunk(.2, 0, 102.25))
    assert len(result) == 1
    assert result[0].started_at == pytest.approx(100.672, abs=.033)
    assert result[0].ended_at < 102.1
    assert result[0].overlap_seconds == 0
    assert vad.flush() == []


def test_vad_hard_split_overlaps_without_losing_samples():
    vad = VadSegmenter(probability_fn=energy_probability)
    result = vad.feed(chunk(8)) + vad.flush()
    assert len(result) == 3
    assert [s.segment_id for s in result] == [1, 2, 3]
    assert len(result[0].samples) == 64000
    assert result[1].overlap_seconds == pytest.approx(.256)
    assert result[1].started_at == pytest.approx(result[0].ended_at - .256)
    assert np.array_equal(result[0].samples[-4096:], result[1].samples[:4096])
    assert result[-1].ended_at == pytest.approx(108)


def test_vad_reset_on_capture_gap_discards_partial_and_keeps_ids():
    vad = VadSegmenter(probability_fn=energy_probability)
    first = vad.feed(chunk(1)) + vad.flush()
    vad.feed(chunk(1, started_at=102))
    assert vad.feed(chunk(.7, 0, started_at=110)) == []
    assert vad.flush() == []
    later = vad.feed(chunk(.5, started_at=111)) + vad.flush()
    assert first[0].segment_id == 1
    assert later[0].segment_id == 2
    assert later[0].started_at >= 110


def test_short_noise_and_overlap_without_new_speech_are_not_emitted():
    vad = VadSegmenter(probability_fn=energy_probability)
    assert vad.feed(chunk(.064)) == []
    assert vad.feed(chunk(.6, 0, 100.064)) == []
    assert vad.flush() == []
    vad.reset()
    result = vad.feed(chunk(4, started_at=102))
    assert len(result) == 1
    assert vad.feed(chunk(.6, 0, 106)) == []
    assert vad.flush() == []


def word(text, start, end):
    return SimpleNamespace(word=text, start=start, end=end)


def part(words):
    return SimpleNamespace(words=words, no_speech_prob=0, avg_logprob=0)


def test_asr_removes_acoustically_overlapped_words_but_keeps_later_repetition():
    engine = WhisperEngine()
    engine._model = object()
    batches = iter([
        [part([word("Hello", 3.5, 3.8), word(" world.", 3.8, 4)])],
        [part([word(" world.", .05, .25), word(" Welcome!", .3, .8)])],
        [part([word("Hello", .1, .4), word(" world.", .4, .7)])],
    ])
    engine._recognize = lambda _: next(batches)
    first = engine.transcribe(SpeechSegment(1, np.zeros(64000), 100, 104))
    second = engine.transcribe(SpeechSegment(2, np.zeros(64000), 103.75, 107.75, .25))
    third = engine.transcribe(SpeechSegment(3, np.zeros(16000), 110, 111))
    assert first.text == "Hello world."
    assert second.text == "Welcome!"
    assert third.text == "Hello world."


def test_asr_lazy_cuda_failure_retries_same_segment_on_cpu():
    engine = WhisperEngine()
    engine.device = "cuda"
    engine._model = object()
    calls = []
    def recognize(segment):
        calls.append(engine.device)
        if engine.device == "cuda":
            raise RuntimeError("cublas64 missing")
        return [part([word("Recovered.", 0, .5)])]
    def cpu():
        engine.device = "cpu"
        engine._model = object()
    engine._recognize = recognize
    engine._load_cpu = cpu
    result = engine.transcribe(SpeechSegment(1, np.zeros(16000), 0, 1))
    assert calls == ["cuda", "cpu"]
    assert result.text == "Recovered."


def test_cuda_missing_dll_falls_back_before_constructing_cuda_model(monkeypatch):
    import ctranslate2
    import asr.whisper_engine as module
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    def missing(_name):
        raise OSError("missing runtime DLL")
    monkeypatch.setattr(module.ctypes, "WinDLL", missing, raising=False)
    engine = WhisperEngine()
    loaded = []
    engine._load_cpu = lambda: loaded.append("cpu")
    engine.load()
    assert loaded == ["cpu"]


def test_real_bundled_silero_accepts_continuous_windows():
    # Real ONNX execution, with no download or acoustic fixture required.
    from audio.vad import StreamingSilero
    probability = StreamingSilero()
    for _ in range(4):
        assert 0 <= probability(np.zeros(FRAME_SAMPLES, dtype=np.float32)) < .1
    probability.reset()
    assert 0 <= probability(np.zeros(FRAME_SAMPLES, dtype=np.float32)) < .1
