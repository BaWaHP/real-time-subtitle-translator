from __future__ import annotations

from collections import deque
from queue import Empty, Full, Queue
from typing import TypeVar

from core.types import Subtitle

T = TypeVar("T")


def put_latest(queue: Queue[T], value: T) -> bool:
    """Nonblocking producer: discard oldest when the bounded queue is full."""
    dropped = False
    while True:
        try:
            queue.put_nowait(value)
            return dropped
        except Full:
            try:
                queue.get_nowait()
                dropped = True
            except Empty:
                pass


def clear_queue(queue: Queue) -> None:
    while True:
        try:
            queue.get_nowait()
        except Empty:
            return


class SubtitleQueue:
    """Schedule subtitles on the GUI thread; measure display duration from presentation."""

    def __init__(self, max_pending: int = 3, display_seconds: float = 5.0,
                 max_age: float | None = None):
        self.pending: deque[Subtitle] = deque(maxlen=max_pending)
        self.display_seconds = display_seconds
        self.current: Subtitle | None = None
        self.expires_at = 0.0
        self._last_id = -1
        self.max_age = max_age
        self.dropped = 0

    def push(self, subtitle: Subtitle) -> bool:
        if subtitle.segment_id <= self._last_id:
            return False
        self._last_id = subtitle.segment_id
        self.pending.append(subtitle)
        return True

    def tick(self, now: float) -> tuple[bool, Subtitle | None]:
        if self.current is not None and now < self.expires_at:
            return False, self.current
        while self.pending and self.max_age is not None and now - self.pending[0].ended_at > self.max_age:
            self.pending.popleft()
            self.dropped += 1
        if self.pending:
            self.current = self.pending.popleft()
            # Scale display time by text length, capped to limit backlog.
            length = max(len(self.current.chinese), len(self.current.english) / 3)
            duration = max(2.0, min(self.display_seconds, 1.0 + length / 12))
            self.expires_at = now + duration
            return True, self.current
        if self.current is not None:
            self.current = None
            return True, None
        return False, None

    def clear(self) -> None:
        self.pending.clear()
        self.current = None
        self.expires_at = 0.0
        self._last_id = -1
        self.dropped = 0
