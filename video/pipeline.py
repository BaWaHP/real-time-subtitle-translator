"""Prefetch video subtitles with a separate playback clock.

The clock runs independently of decoding, ASR, and text translation requests.
"""
from __future__ import annotations

from collections import OrderedDict
import math
import threading
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal

from core.types import AudioChunk, Subtitle
from translator.openai_translator import TranslationCancelled, TranslationError, TranslationPaused

from .captions import normalize_cues, subtitle_segments
from .media import MediaCancelled, MediaError, MediaReader, MediaResolver
from .timeline import CueTimeline, MediaClock
from .types import Cue


class VideoCancellation:
    def __init__(self, stopped, active):
        self.stopped, self.active = stopped, active

    def is_set(self):
        return self.stopped.is_set() or not self.active()

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            self.stopped.wait(min(.05, remaining))
        return True


class VideoPipeline(QObject):
    status = Signal(str)
    error = Signal(str)
    subtitle = Signal(object)
    metrics = Signal(dict)
    running_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._running = False
        self._generation = 0
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._threads = []
        self._clock = MediaClock()
        self._timeline = CueTimeline()
        self._cache = OrderedDict()
        self._source_epoch = 0
        self._display_key = None
        self._translation_paused = ""
        self._launch_complete = threading.Event()

    @property
    def is_running(self):
        return self._running

    def accepts(self, subtitle):
        return self._running and not self._stop.is_set() and (
            subtitle is None or self._active(subtitle.generation))

    def start(self, config, translator, bridge):
        with self._lock:
            if self._running:
                return
            self.config, self.translator, self.bridge = config, translator, bridge
            self._running = True
            self._generation += 1
            self._stop, self._pause = threading.Event(), threading.Event()
            self._clock, self._timeline = MediaClock(), CueTimeline()
            self._cache = OrderedDict()
            self._source_epoch += 1
            self._display_key = None
            self._translation_paused = ""
            self._translation_errors = {}
            self._counts = {"dropped": 0, "latency": 0.0}
            self._request_started = 0.0
            self._request_count = 0
            self._last_request_seconds = 0.0
            self._batch_disabled = False
            self._selected_session = str(getattr(config, "video_session_id", "") or "")
            self._caption_revision = None
            self._caption_poll = 0.0
            self._caption_origin = ""
            self._source_connected_at = time.monotonic()
            self._launch_complete = threading.Event()
            self._prefetch = max(10, min(180, float(getattr(config, "video_prefetch_seconds", 45))))
            self._cache_limit = max(32, min(10_000, int(getattr(config, "video_transcript_cache_limit", 2000))))
            self._threads = [threading.Thread(target=self._guard, args=(function,), daemon=True,
                                              name=f"video-{function.__name__}")
                             for function in (self._watch, self._prepare, self._translate_worker)]
        self.running_changed.emit(True)
        self.status.emit("视频模式已启动，正在等待 Edge 视频连接…")
        try:
            for thread in self._threads:
                thread.start()
        except Exception:
            self.stop()
            raise
        finally:
            self._launch_complete.set()

    def _guard(self, function):
        try:
            self._launch_complete.wait()
            if self._stop.is_set():
                return
            function()
        except Exception as exc:
            self.error.emit(f"视频模式异常：{type(exc).__name__}: {exc}")
            self.stop()

    def stop(self):
        with self._lock:
            if not self._running or self._stop.is_set():
                return
            self._generation += 1
            self._stop.set()
            self._display_key = None
        self.subtitle.emit(None)
        self.status.emit("正在停止视频预读…")
        threading.Thread(target=self._finish_stop, daemon=True, name="video-shutdown").start()

    def _finish_stop(self):
        self._launch_complete.wait()
        for thread in self._threads:
            if thread.ident is not None:
                thread.join()
        try:
            close = getattr(self.translator, "close", None)
            if close:
                close()
        finally:
            with self._lock:
                self._cache.clear()
                self._timeline.replace([])
                self._running = False
            self.status.emit("视频模式已停止")
            self.running_changed.emit(False)

    def pause(self):
        with self._lock:
            if self._running and not self._pause.is_set():
                self._pause.set()
                self._generation += 1
                self._display_key = None
        self.subtitle.emit(None)
        self.status.emit("视频翻译已暂停，播放器仍可继续播放")

    def resume(self):
        with self._lock:
            self._generation += 1
            self._pause.clear()
            self._display_key = None
        self.status.emit("视频翻译已恢复，正在准备当前进度的字幕")

    def _active(self, generation):
        with self._lock:
            return (not self._stop.is_set() and not self._pause.is_set()
                    and generation == self._generation and self._clock.fresh())

    def _select_snapshot(self):
        if self._selected_session:
            snapshot = self.bridge.snapshot(self._selected_session)
            if snapshot is not None:
                return snapshot
            # Wait for the selected tab to reconnect.
            if getattr(self.config, "video_session_id", ""):
                return None
        sessions = self.bridge.sessions()
        if not sessions:
            return None
        chosen = max(sessions, key=lambda item: (not item.get("paused", True), item.get("received_at", 0)))
        self._selected_session = chosen["session_id"]
        return self.bridge.snapshot(self._selected_session)

    def _watch(self):
        disconnected = False
        report_at = 0.0
        while not self._stop.wait(.05):
            now = time.monotonic()
            snapshot = self._select_snapshot()
            if snapshot is not None:
                with self._lock:
                    source_changed, discontinuity = self._clock.update(snapshot, now)
                    if discontinuity:
                        self._generation += 1
                        self._display_key = None
                    if source_changed:
                        self._source_epoch += 1
                        self._timeline.replace([])
                        self._cache.clear()
                        self._translation_errors.clear()
                        self._caption_revision = None
                        self._caption_poll = 0
                        self._caption_origin = ""
                        self._source_connected_at = now
                if source_changed:
                    self.subtitle.emit(None)
                    self.status.emit("已连接 Edge 视频，正在读取英文字幕…")
                elif discontinuity:
                    self.subtitle.emit(None)
                self._load_browser_captions(snapshot, now)
            with self._lock:
                position = self._clock.position(now)
                fresh = position is not None
                if not fresh and not disconnected:
                    self._generation += 1
                    self._display_key = None
                    self.subtitle.emit(None)
                self._render(position)
                if now >= report_at:
                    upcoming = self._timeline.upcoming(position or 0, self._prefetch)
                    pending = sum(cue.key not in self._cache for cue in upcoming)
                    prepared = self._prepared_until(position or 0, upcoming)
                    request_seconds = (now - self._request_started if self._request_count
                                       else self._last_request_seconds)
                    self.metrics.emit({**self._counts, "audio": 0, "segments": 0,
                                       "transcripts": pending, "paused": self._pause.is_set(),
                                       "video_position": position or 0, "prepared_until": prepared,
                                       "video_prefetch_seconds": max(0, prepared - (position or 0)),
                                       "video_connected": fresh, "video_cues": len(self._timeline.cues),
                                       "video_cache": len(self._cache),
                                       "video_caption_origin": self._caption_origin,
                                       "video_state_age": max(0, now - (self._clock.state or {}).get(
                                           "received_at", now)),
                                       "video_translation_inflight": self._request_count,
                                       "video_translation_request_seconds": max(0, request_seconds),
                                       "video_translation_last_seconds": self._last_request_seconds})
                    report_at = now + .5
            if not fresh and not disconnected:
                self.status.emit("等待 Edge 扩展恢复视频同步…")
            disconnected = not fresh

    def _load_browser_captions(self, snapshot, now):
        if now - self._caption_poll < .4:
            return
        self._caption_poll = now
        session, source = snapshot["session_id"], snapshot.get("source_id", "")
        revision_fn = getattr(self.bridge, "caption_revision", None)
        revision = revision_fn(session, source) if revision_fn else None
        if revision_fn and revision == self._caption_revision:
            return
        values = self.bridge.captions(session, source)
        self._caption_revision = revision
        if not values:
            return
        cues = subtitle_segments(normalize_cues(values))
        connected = False
        with self._lock:
            if self._clock.state and self._clock.state.get("source_id") == source:
                connected = self._caption_origin != "browser"
                if self._caption_origin not in {"", "browser"}:
                    self._generation += 1
                    self._display_key = None
                    self.subtitle.emit(None)
                self._timeline.replace(cues)
                self._caption_origin = "browser"
        if connected:
            self.status.emit("已读取播放器英文字幕，正在翻译…")

    def _render(self, position):
        cue = self._timeline.at(position) if position is not None and not self._pause.is_set() else None
        if self._clock.state and (self._clock.state.get("seeking") or self._clock.state.get("ended")):
            cue = None
        if cue is None:
            if self._display_key is not None:
                self._display_key = None
                self.subtitle.emit(None)
            return
        chinese = self._cache.get(cue.key)
        if chinese is None:
            chinese = "[翻译已暂停]" if self._translation_paused else "[翻译准备中]"
        key = (self._generation, cue.key, chinese)
        if key != self._display_key:
            self._display_key = key
            # Use a stable cue id for replacement. Video subtitles stay visible
            # until the media clock advances, bypassing the live-audio expiry queue.
            identifier = int(cue.key[:12], 16)
            self.subtitle.emit(Subtitle(identifier, cue.text, chinese, cue.start, cue.end,
                                        time.monotonic(), self._generation))

    def _prepared_until(self, position, upcoming):
        """Measure readiness up to the first untranslated cue.

        Subtitle gaps count as ready; a missing active cue stops readiness here.
        """
        prepared = position
        for cue in upcoming:
            if cue.key not in self._cache:
                return max(position, min(cue.start, position + self._prefetch))
            prepared = max(prepared, cue.end)
        return min(prepared, position + self._prefetch)

    def _media_active(self, generation):
        with self._lock:
            return self._active(generation) and self._caption_origin != "browser"

    def _prepare(self):
        """Resolve/decode audio independently of the translation network worker."""
        resolver = MediaResolver()
        reader = None
        media = None
        resolved_epoch = -1
        resolution_failed = False
        completed_blocks = set()
        engine = None
        asr_generation = -1
        try:
            while not self._stop.wait(.03):
                with self._lock:
                    position = self._clock.position()
                    state = dict(self._clock.state or {})
                    generation, epoch = self._generation, self._source_epoch
                    caption_origin = self._caption_origin
                    if position is None or self._pause.is_set() or state.get("seeking"):
                        continue
                    token = VideoCancellation(self._stop, lambda gen=generation: self._media_active(gen))
                if epoch != resolved_epoch:
                    if reader:
                        reader.close()
                        reader = None
                    media = None
                    resolution_failed = False
                    completed_blocks.clear()
                    resolved_epoch = epoch
                if reader is not None and caption_origin == "browser":
                    reader.close()
                    reader = None
                with self._lock:
                    has_captions = bool(self._timeline.cues)
                if media is None and not resolution_failed and not has_captions:
                    # Allow the authenticated browser's Panopto caption request
                    # to finish before trying public extraction.
                    try:
                        page = urlsplit(str(state.get("page_url", "")))
                        panopto = bool(page.hostname and (page.hostname.endswith(".panopto.com")
                                                         or page.hostname.endswith(".panopto.eu")))
                    except ValueError:
                        panopto = False
                    if panopto:
                        warning_fn = getattr(self.bridge, "warning", None)
                        browser_warning = warning_fn(state.get("session_id", "")) if callable(warning_fn) else ""
                        if isinstance(browser_warning, str) and browser_warning.startswith("Panopto"):
                            resolution_failed = True
                            self.error.emit(browser_warning)
                            continue
                        if time.monotonic() - self._source_connected_at < 8:
                            continue
                    # Wait briefly for the browser's English caption track.
                    if time.monotonic() - self._source_connected_at < 2:
                        continue
                    try:
                        self.status.emit("未找到页面英文字幕，正在查找可用字幕或音轨…")
                        result = resolver.resolve(state, token)
                        if token.is_set():
                            continue
                        media = result
                        if media.cues:
                            with self._lock:
                                if self._active(generation):
                                    self._timeline.replace(subtitle_segments(media.cues))
                                    self._caption_origin = "public"
                            self.status.emit("已读取英文字幕，正在翻译…")
                        elif getattr(self.config, "video_audio_fallback", True):
                            reader = MediaReader(media)
                            self.status.emit("未找到英文字幕，正在识别视频音轨…")
                        else:
                            resolution_failed = True
                            self.status.emit("未找到英文字幕，音轨预读也未开启。请开启英文字幕或使用系统声音模式。")
                    except MediaCancelled:
                        continue
                    except Exception as exc:
                        if token.is_set():
                            continue
                        resolution_failed = True
                        self.error.emit(str(exc) if isinstance(exc, MediaError)
                                        else "无法读取视频字幕或音轨，请使用系统声音模式。")
                    continue
                if reader is None or resolution_failed or self._translation_paused:
                    continue
                # Decode one six-second block at a time to bound audio memory.
                first = max(0, math.floor(position / 6))
                last = math.floor((position + self._prefetch) / 6)
                block = next((value for value in range(first, last + 1) if value not in completed_blocks), None)
                if block is None:
                    continue
                duration = float(state.get("duration") or 0)
                if duration > 0 and block * 6 >= duration:
                    completed_blocks.add(block)
                    continue
                start = max(0.0, block * 6 - .3)
                try:
                    samples = reader.read(start, min(6.6, duration - start) if duration > 0 else 6.6, token)
                    if token.is_set():
                        continue
                    if engine is None:
                        from asr.whisper_engine import WhisperEngine
                        engine = WhisperEngine(self.config.asr_model, self.config.asr_device,
                                               on_status=self.status.emit)
                    if asr_generation != generation:
                        engine._recent_words.clear()
                        engine._last_segment_end = float("-inf")
                        asr_generation = generation
                    cues = self._recognize_window(engine, samples, start, token)
                    if token.is_set():
                        continue
                    completed_blocks.add(block)
                    # Retain nearby block ids; recognize evicted blocks again on replay.
                    completed_blocks = {value for value in completed_blocks if abs(value - first) <= 500}
                    with self._lock:
                        merged = self._timeline.cues + [cue for cue in cues if cue.start < (block + 1) * 6
                                                       and cue.end > block * 6]
                        if len(merged) > self._cache_limit:
                            merged = sorted(merged, key=lambda cue: abs(cue.start - position))[:self._cache_limit]
                        self._timeline.replace(merged)
                        self._caption_origin = "audio"
                except MediaCancelled:
                    continue
                except Exception as exc:
                    if token.is_set():
                        continue
                    resolution_failed = True
                    self.error.emit(str(exc) if isinstance(exc, MediaError)
                                    else f"视频语音识别失败：{type(exc).__name__}。请使用系统声音模式。")
        finally:
            if reader:
                reader.close()

    def _recognize_window(self, engine, samples, start, token):
        from audio.vad import VadSegmenter
        vad = VadSegmenter()
        segments = vad.feed(AudioChunk(samples, start)) + vad.flush()
        cues = []
        for segment in segments:
            if token.is_set():
                raise MediaCancelled("预读已取消。")
            previous_end = engine._last_segment_end
            segment.overlap_seconds = max(segment.overlap_seconds, min(
                segment.ended_at - segment.started_at, max(0, previous_end - segment.started_at)))
            transcript = engine.transcribe(segment)
            if transcript:
                cues.append(Cue(transcript.started_at, transcript.ended_at, transcript.text))
        return cues

    def _translate_worker(self):
        """Translate current/upcoming cues in batches, separately from media work.

        Future cues remain useful if the current cue expires during the request.
        """
        while not self._stop.wait(.03):
            with self._lock:
                position = self._clock.position()
                state = self._clock.state or {}
                if (position is None or self._pause.is_set() or state.get("seeking")
                        or state.get("ended") or self._translation_paused):
                    continue
                generation = self._generation
                now = time.monotonic()
                pending = [cue for cue in self._timeline.upcoming(position, self._prefetch)
                           if cue.key not in self._cache
                           and self._translation_errors.get(cue.key, (0, 0))[1] <= now]
                if not pending:
                    continue
                batch = self._translation_batch(pending)
            token = VideoCancellation(self._stop, lambda gen=generation: self._active(gen))
            self._translate_batch(batch, generation, token)

    def _translation_batch(self, pending):
        """Limit batch count, text size, and time span, starting with the current cue."""
        if (getattr(self, "_batch_disabled", False)
                or not callable(getattr(self.translator, "translate_batch", None))):
            return pending[:1]
        result = []
        characters = 0
        for cue in pending:
            # Keep the first cue even if it exceeds the batch span.
            # The translator enforces the per-cue text limit.
            if result and (len(result) >= 8 or cue.start - result[0].start >= 24
                           or characters + len(cue.text) > 8000):
                break
            result.append(cue)
            characters += len(cue.text)
        return result

    def _translate(self, cue, generation, token):
        # Retain the single-cue helper for callers and diagnostic translators.
        self._translate_batch([cue], generation, token)

    def _translate_batch(self, cues, generation, token):
        with self._lock:
            context = self._timeline.context(cues[0])

        def relevant():
            with self._lock:
                position = self._clock.position()
                return (self._active(generation) and position is not None
                        and any(cue.end >= position - .2 and cue.start <= position + self._prefetch
                                for cue in cues))

        cue_token = VideoCancellation(self._stop, relevant)
        started = time.monotonic()
        with self._lock:
            self._request_started = started
            self._request_count = len(cues)
        try:
            if len(cues) > 1:
                results = self.translator.translate_batch([cue.text for cue in cues], context,
                                                          cancel_event=cue_token)
            else:
                results = [self.translator.translate(cues[0].text, context, cancel_event=cue_token)]
            if cue_token.is_set():
                return
            if (not isinstance(results, list) or len(results) != len(cues)
                    or any(not isinstance(value, str) or not value.strip() for value in results)):
                raise TranslationError("批量字幕译文数量或格式无效", code="invalid_subtitle_batch")
            with self._lock:
                position = self._clock.position()
                if position is None or not self._active(generation):
                    return
                for cue, result in zip(cues, results):
                    if cue.end < position - .2 or cue.start > position + self._prefetch:
                        self._counts["dropped"] += 1
                        continue
                    self._cache[cue.key] = result.strip()
                    self._cache.move_to_end(cue.key)
                    self._translation_errors.pop(cue.key, None)
                while len(self._cache) > self._cache_limit:
                    self._cache.popitem(last=False)
                self._counts["latency"] = max(0, position - cues[0].start)
        except (TranslationCancelled, MediaCancelled):
            return
        except TranslationPaused as exc:
            if not token.is_set():
                self._translation_paused = str(exc)
                self.error.emit(f"视频翻译已暂停：{exc}\n处理后点击 Stop，再点击 Start 重试。")
        except Exception as exc:
            if cue_token.is_set():
                return
            if len(cues) > 1 and getattr(exc, "code", "") == "invalid_subtitle_batch":
                # Retry individually if the model returns an invalid batch format.
                self._batch_disabled = True
                self.status.emit("模型返回的字幕格式有误，已改为逐段翻译。若仍有问题，请更换模型。")
                return
            with self._lock:
                for cue in cues:
                    count = self._translation_errors.get(cue.key, (0, 0))[0] + 1
                    self._translation_errors[cue.key] = (count, time.monotonic() + min(8, 2**count))
                    if count >= 3:
                        self._cache[cue.key] = "[本段翻译暂不可用]"
                    self._counts["dropped"] += 1
                while len(self._cache) > self._cache_limit:
                    self._cache.popitem(last=False)
                while len(self._translation_errors) > self._cache_limit:
                    self._translation_errors.pop(next(iter(self._translation_errors)))
            self.error.emit(f"视频片段翻译失败：{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._last_request_seconds = time.monotonic() - started
                self._request_started = 0.0
                self._request_count = 0
