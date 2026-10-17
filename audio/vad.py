"""Stateful Silero ONNX VAD and bounded, overlapping speech segmentation."""
from __future__ import annotations

from collections import deque
import math
from typing import Callable

import numpy as np

from core.types import AudioChunk, SpeechSegment

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512


class StreamingSilero:
    """Keep Silero state across 512-sample frames with 64 samples of context.

    Uses faster-whisper's bundled ONNX model and supports v5/v6 input layouts.
    """
    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self.session = get_vad_model().session
        self.names = {x.name for x in self.session.get_inputs()}
        self.reset()

    def reset(self):
        self.context = np.zeros((1, 64), dtype=np.float32)
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        audio = np.concatenate((self.context, frame.reshape(1, -1)), axis=1)
        if {"h", "c"}.issubset(self.names):
            output, self.h, self.c = self.session.run(None, {"input": audio, "h": self.h, "c": self.c})
        elif {"state", "sr"}.issubset(self.names):
            output, self.state = self.session.run(None, {"input": audio, "state": self.state,
                                                        "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
        else:
            raise RuntimeError(f"不兼容的 Silero ONNX 输入：{sorted(self.names)}")
        self.context = frame[-64:].reshape(1, -1).copy()
        return float(np.asarray(output).reshape(-1)[0])


class VadSegmenter:
    def __init__(self, threshold: float = .5, silence_seconds: float = .5,
                 max_seconds: float = 4, overlap_seconds: float = .25,
                 min_speech_seconds: float = .16,
                 probability_fn: Callable[[np.ndarray], float] | None = None):
        if not (0 < threshold < 1 and 0 < overlap_seconds < max_seconds
                and .1 <= silence_seconds < max_seconds):
            raise ValueError("无效的 VAD 分段设置")
        self.threshold = threshold
        self.silence_frames = math.ceil(silence_seconds * SAMPLE_RATE / FRAME_SAMPLES)
        self.max_frames = math.ceil(max_seconds * SAMPLE_RATE / FRAME_SAMPLES)
        self.overlap_frames = math.ceil(overlap_seconds * SAMPLE_RATE / FRAME_SAMPLES)
        self.min_speech_samples = int(min_speech_seconds * SAMPLE_RATE)
        self._probability = probability_fn
        self._next_id = 1
        self.reset()

    def reset(self) -> None:
        """Clear partial audio after pause/hotplug, retaining the segment id counter."""
        self._pending = np.empty(0, dtype=np.float32)
        self._pending_at: float | None = None
        self._pre_roll: deque = deque(maxlen=5)  # 160 ms onset protection.
        self._active: list[tuple[np.ndarray, float]] = []
        self._silence = 0
        self._new_speech = 0
        self._overlap = 0.0
        if self._probability is not None and hasattr(self._probability, "reset"):
            self._probability.reset()

    def load(self) -> None:
        """Load the model in the VAD worker."""
        if self._probability is None:
            self._probability = StreamingSilero()

    def _emit(self, frames: list[tuple[np.ndarray, float]]) -> SpeechSegment | None:
        if not frames or self._new_speech < self.min_speech_samples:
            return None
        samples = np.concatenate([frame for frame, _ in frames])
        start = frames[0][1]
        result = SpeechSegment(self._next_id, samples, start,
                               start + samples.size / SAMPLE_RATE, self._overlap)
        self._next_id += 1
        return result

    def _frame(self, frame: np.ndarray, at: float) -> list[SpeechSegment]:
        assert self._probability is not None
        probability = float(self._probability(frame))
        speech = probability >= (self.threshold - .15 if self._active else self.threshold)
        if not self._active:
            self._pre_roll.append((frame, at))
            if not speech:
                return []
            self._active = list(self._pre_roll)
            self._pre_roll.clear()
        else:
            self._active.append((frame, at))
        if speech:
            self._silence = 0
            self._new_speech += FRAME_SAMPLES
        else:
            self._silence += 1
        result = []
        if self._silence >= self.silence_frames:
            # Keep 128 ms of trailing silence, discard the rest of the pause.
            trim = max(0, self._silence - 4)
            segment = self._emit(self._active[:-trim] if trim else self._active)
            if segment:
                result.append(segment)
            self._pre_roll.extend(self._active[-5:])
            self._active = []
            self._silence = self._new_speech = 0
            self._overlap = 0
        elif len(self._active) >= self.max_frames:
            segment = self._emit(self._active)
            if segment:
                result.append(segment)
            self._active = self._active[-self.overlap_frames:]
            self._overlap = len(self._active) * FRAME_SAMPLES / SAMPLE_RATE
            self._new_speech = 0
        return result

    def feed(self, chunk: AudioChunk) -> list[SpeechSegment]:
        self.load()
        samples = np.asarray(chunk.samples, dtype=np.float32).reshape(-1)
        if not samples.size:
            return []
        expected = None if self._pending_at is None else self._pending_at + self._pending.size / SAMPLE_RATE
        if expected is not None and abs(chunk.started_at - expected) > .15:
            self.reset()
        if self._pending_at is None:
            self._pending_at = chunk.started_at
        self._pending = np.concatenate((self._pending, samples))
        result = []
        while self._pending.size >= FRAME_SAMPLES:
            frame = self._pending[:FRAME_SAMPLES].copy()
            self._pending = self._pending[FRAME_SAMPLES:]
            result.extend(self._frame(frame, self._pending_at))
            self._pending_at += FRAME_SAMPLES / SAMPLE_RATE
        return result

    def flush(self) -> list[SpeechSegment]:
        """Flush new speech at Stop, excluding overlap-only audio."""
        if self._active and self._pending.size:
            self._active.append((self._pending.copy(), self._pending_at))
        segment = self._emit(self._active)
        self.reset()
        return [segment] if segment else []
