from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import re


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True, slots=True)
class Cue:
    """One English subtitle; all positions are seconds on the media clock."""

    start: float
    end: float
    text: str

    @property
    def key(self) -> str:
        raw = f"{self.start:.3f}|{self.end:.3f}|{self.text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def from_dict(cls, value: dict) -> Cue | None:
        try:
            start, end = float(value["start"]), float(value["end"])
            text = clean_text(str(value["text"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end and text):
            return None
        # Reject oversized cues before translation.
        if end - start > 120 or len(text) > 8000:
            return None
        return cls(start, end, text)


@dataclass(slots=True)
class MediaSource:
    audio_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cues: list[Cue] = field(default_factory=list)
    live: bool = False
