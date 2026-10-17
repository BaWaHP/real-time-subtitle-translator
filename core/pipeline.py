from __future__ import annotations

from collections import deque
from queue import Empty, Queue
import threading
import time

from PySide6.QtCore import QObject, Signal

from config import AppConfig
from core.subtitle_queue import clear_queue, put_latest
from core.types import Subtitle


class SegmentCancellation:
    """Cancel a segment on stop, state change, or expiry."""

    def __init__(self, stop_event, active, expires_at):
        self.stop_event = stop_event
        self.active = active
        self.expires_at = expires_at

    def is_set(self) -> bool:
        return self.stop_event.is_set() or not self.active() or time.monotonic() >= self.expires_at

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = min(deadline, self.expires_at) - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            self.stop_event.wait(min(.05, remaining))
        return True


class Pipeline(QObject):
    """Run each stage in a worker and update widgets through Qt signals."""

    status = Signal(str)
    error = Signal(str)
    subtitle = Signal(object)
    metrics = Signal(dict)
    running_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._running = False
        self._generation = 0
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._capture_ready = threading.Event()
        self._asr_ready = threading.Event()
        self._counts = {"dropped": 0, "latency": 0.0}
        self._launch_complete = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    def accepts(self, subtitle: Subtitle) -> bool:
        return self._running and self._active(subtitle.generation)

    def start(self, config: AppConfig, translator) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._generation += 1
        self.config = config
        self.translator = translator
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._capture_ready = threading.Event()
        self._asr_ready = threading.Event()
        self._launch_complete = threading.Event()
        self._capture_worker = None
        self.audio_queue: Queue = Queue(maxsize=config.audio_queue_size)
        self.segment_queue: Queue = Queue(maxsize=config.segment_queue_size)
        self.transcript_queue: Queue = Queue(maxsize=config.transcript_queue_size)
        self._counts = {"dropped": 0, "latency": 0.0}
        targets = (self._segment, self._recognize, self._translate, self._capture, self._report)
        self._threads = [threading.Thread(target=self._guard, args=(fn,), daemon=True,
                                          name=f"subtitle-{fn.__name__}") for fn in targets]
        self.running_changed.emit(True)
        try:
            for thread in self._threads:
                thread.start()
        except Exception:
            self.stop()
            raise
        finally:
            self._launch_complete.set()

    def _guard(self, fn) -> None:
        try:
            self._launch_complete.wait()
            if self._stop.is_set():
                return
            fn()
        except Exception as exc:
            # Exception messages must omit callback URLs and credentials.
            self.error.emit(f"{threading.current_thread().name}: {type(exc).__name__}: {exc}")
            self.stop()

    def stop(self) -> None:
        with self._lock:
            if not self._running or self._stop.is_set():
                return
            self._generation += 1
            self._stop.set()
        self.status.emit("正在停止，等待当前处理结束…")
        threading.Thread(target=self._finish_stop, daemon=True, name="subtitle-shutdown").start()

    def _finish_stop(self) -> None:
        self._launch_complete.wait()
        for thread in self._threads:
            if thread.ident is not None:
                thread.join()
        for queue in (self.audio_queue, self.segment_queue, self.transcript_queue):
            clear_queue(queue)
        close = getattr(self.translator, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass
        with self._lock:
            self._running = False
        self.status.emit("已停止")
        self.running_changed.emit(False)

    def pause(self) -> None:
        if self._running and not self._pause.is_set():
            self._pause.set()
            self._generation += 1
            self._clear()
            self.status.emit("已暂停音频处理")

    def resume(self) -> None:
        if self._running and self._pause.is_set():
            self._generation += 1
            self._clear()
            self._pause.clear()
            self.status.emit("已恢复音频处理")

    def _clear(self) -> None:
        for queue in (self.audio_queue, self.segment_queue, self.transcript_queue):
            clear_queue(queue)

    def _active(self, generation: int) -> bool:
        return not self._stop.is_set() and not self._pause.is_set() and generation == self._generation

    def _wait_ready(self, event: threading.Event) -> bool:
        while not self._stop.is_set():
            if event.wait(0.1):
                return True
        return False

    def _capture(self) -> None:
        from audio.capture import LoopbackCapture
        if not self._wait_ready(self._capture_ready) or not self._wait_ready(self._asr_ready):
            return
        if self._stop.is_set():
            return
        capture = LoopbackCapture(self.audio_queue, self._stop, self._pause,
                                  device_name=self.config.device_name, on_status=self.status.emit)
        self._capture_worker = capture
        capture.run()

    def _segment(self) -> None:
        from audio.vad import VadSegmenter
        self.status.emit("正在加载语音检测模型…")
        vad = VadSegmenter()
        vad.load()
        self._capture_ready.set()
        generation = self._generation
        while not self._stop.is_set():
            if generation != self._generation:
                vad.reset()
                generation = self._generation
            try:
                chunk = self.audio_queue.get(timeout=0.1)
            except Empty:
                continue
            if not self._active(generation):
                continue
            for segment in vad.feed(chunk):
                if self._active(generation):
                    if put_latest(self.segment_queue, (generation, segment)):
                        self._counts["dropped"] += 1

    def _recognize(self) -> None:
        from asr.whisper_engine import WhisperEngine
        engine = WhisperEngine(model=self.config.asr_model, device=self.config.asr_device,
                               on_status=self.status.emit)
        engine.load()
        self._asr_ready.set()
        while not self._stop.is_set():
            try:
                generation, segment = self.segment_queue.get(timeout=0.1)
            except Empty:
                continue
            if not self._active(generation) or self._stale(segment):
                continue
            result = engine.transcribe(segment)
            if result and result.text.strip() and self._active(generation) and not self._stale(result):
                if put_latest(self.transcript_queue, (generation, result)):
                    self._counts["dropped"] += 1

    def _stale(self, item) -> bool:
        stale = time.monotonic() - item.ended_at > self.config.max_segment_age
        if stale:
            self._counts["dropped"] += 1
        return stale

    def _translate(self) -> None:
        from translator.openai_translator import TranslationCancelled, TranslationPaused
        context: deque[str] = deque(maxlen=3)
        context_generation = self._generation
        paused_reason = ""
        while not self._stop.is_set():
            try:
                generation, transcript = self.transcript_queue.get(timeout=0.1)
            except Empty:
                continue
            if not self._active(generation) or self._stale(transcript):
                continue
            if generation != context_generation:
                context.clear()
                context_generation = generation
            translated = ""
            if not paused_reason:
                try:
                    cancellation = SegmentCancellation(
                        self._stop, lambda: self._active(generation),
                        transcript.ended_at + self.config.max_segment_age)
                    translated = self.translator.translate(transcript.text, list(context),
                                                           cancel_event=cancellation)
                except TranslationCancelled:
                    continue
                except TranslationPaused as exc:
                    paused_reason = str(exc)
                    self.error.emit(f"翻译已暂停：{paused_reason}\n处理后点击 Stop，再点击 Start 重试。")
                except Exception as exc:
                    if self._active(generation):
                        self.error.emit(f"本段翻译失败：{type(exc).__name__}: {exc}")
            context.append(transcript.text)
            if self._active(generation) and not self._stale(transcript):
                if not translated:
                    translated = "[翻译已暂停]" if paused_reason else "[本段翻译暂不可用]"
                now = time.monotonic()
                self._counts["latency"] = now - transcript.ended_at
                self.subtitle.emit(Subtitle(transcript.segment_id, transcript.text, translated,
                                            transcript.started_at, transcript.ended_at, now, generation))

    def _report(self) -> None:
        while not self._stop.wait(0.5):
            audio_dropped = (getattr(self._capture_worker, "raw_dropped", 0) +
                             getattr(self._capture_worker, "output_dropped", 0))
            self.metrics.emit({**self._counts, "dropped": self._counts["dropped"] + audio_dropped,
                               "audio": self.audio_queue.qsize(),
                               "segments": self.segment_queue.qsize(),
                               "transcripts": self.transcript_queue.qsize(),
                               "paused": self._pause.is_set()})
