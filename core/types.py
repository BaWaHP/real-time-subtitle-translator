from __future__ import annotations

from dataclasses import dataclass
import numpy as np


@dataclass(slots=True)
class AudioChunk:
    samples: np.ndarray  # float32 mono, 16 kHz
    started_at: float  # time.monotonic clock


@dataclass(slots=True)
class SpeechSegment:
    segment_id: int
    samples: np.ndarray
    started_at: float
    ended_at: float
    overlap_seconds: float = 0.0


@dataclass(slots=True)
class Transcript:
    segment_id: int
    text: str
    started_at: float
    ended_at: float


@dataclass(slots=True)
class Subtitle:
    segment_id: int
    english: str
    chinese: str
    started_at: float
    ended_at: float
    translated_at: float
    generation: int = 0
