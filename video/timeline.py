"""Pure media clock and bounded lookup helpers, independent of Qt/networking."""
from __future__ import annotations

from bisect import bisect_right
import math
import time

from .types import Cue


class MediaClock:
    def __init__(self, stale_seconds: float = 2.0):
        self.stale_seconds = stale_seconds
        self.state: dict | None = None

    def fresh(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.state is not None and 0 <= now - self.state["received_at"] <= self.stale_seconds

    def position(self, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        if not self.fresh(now):
            return None
        value = self.state["current_time"]
        if not (self.state.get("paused") or self.state.get("seeking") or self.state.get("ended")
                or self.state.get("buffering")):
            value += max(0.0, now - self.state["received_at"]) * self.state["playback_rate"]
        duration = self.state.get("duration", 0.0)
        return min(value, duration) if duration > 0 else value

    def update(self, state: dict, now: float | None = None) -> tuple[bool, bool]:
        """Return (source_changed, discontinuity); rate/pause changes are continuous."""
        now = time.monotonic() if now is None else now
        try:
            data = dict(state)
            for field, default in (("current_time", 0), ("playback_rate", 1),
                                   ("received_at", now), ("duration", 0)):
                data[field] = float(data.get(field, default))
                if not math.isfinite(data[field]):
                    data[field] = default
            data["current_time"] = max(0, data["current_time"])
            data["playback_rate"] = max(.0625, min(16, data["playback_rate"]))
        except (TypeError, ValueError):
            return False, False
        previous = self.state
        source_changed = previous is None or any(
            data.get(key) != previous.get(key) for key in ("session_id", "source_id"))
        predicted = self.position(data["received_at"])
        discontinuity = source_changed or bool(data.get("seeking") and not (previous or {}).get("seeking"))
        if predicted is not None and abs(data["current_time"] - predicted) > .8:
            discontinuity = True
        self.state = data
        return source_changed, discontinuity


class CueTimeline:
    def __init__(self, cues: list[Cue] | None = None):
        self.replace(cues or [])

    def replace(self, cues: list[Cue]) -> None:
        self.cues = sorted({cue.key: cue for cue in cues}.values(), key=lambda cue: (cue.start, cue.end))
        self.starts = [cue.start for cue in self.cues]

    def at(self, position: float) -> Cue | None:
        index = bisect_right(self.starts, position) - 1
        # Prefer the latest active cue when tracks overlap; return None in gaps.
        for cue in reversed(self.cues[max(0, index - 30):index + 1]):
            if cue.start <= position < cue.end:
                return cue
        return None

    def upcoming(self, position: float, seconds: float, limit: int = 32) -> list[Cue]:
        active = self.at(position)
        index = bisect_right(self.starts, position)
        result = [active] if active else []
        for cue in self.cues[index:]:
            if cue.start > position + seconds or len(result) >= limit:
                break
            result.append(cue)
        return result

    def context(self, cue: Cue, limit: int = 3) -> list[str]:
        index = bisect_right(self.starts, cue.start - .001)
        return [item.text for item in self.cues[max(0, index - limit):index]]
