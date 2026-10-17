"""faster-whisper wrapper with CUDA recovery and timestamp-scoped deduplication."""
from __future__ import annotations

from collections import deque
import ctypes
import os
import re
import sys
from typing import Callable

from core.types import SpeechSegment, Transcript


class WhisperEngine:
    def __init__(self, model: str = "auto", device: str = "auto",
                 on_status: Callable[[str], None] | None = None):
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError("识别设备必须是 auto、cpu 或 cuda")
        self.requested_model = model
        self.requested_device = device
        self.on_status = on_status or (lambda _message: None)
        self.device = "cpu"
        self.model_name = "small.en" if model == "auto" else model
        self.compute_type = "int8"
        self._model = None
        self._cuda_libraries: list = []
        self._recent_words: deque[tuple[str, float, float]] = deque(maxlen=80)
        self._last_segment_end = float("-inf")

    def _load_cpu(self):
        from faster_whisper import WhisperModel
        self.device = "cpu"
        self.compute_type = "int8"
        self.model_name = "small.en" if self.requested_model == "auto" else self.requested_model
        # Leave headroom for capture, UI and ONNX VAD on smaller processors.
        threads = max(1, min(8, (os.cpu_count() or 4) - 2))
        self.on_status(f"正在加载识别模型：{self.model_name} / CPU INT8（首次使用需下载）")
        self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                   cpu_threads=threads, num_workers=1)

    def load(self) -> None:
        if self._model is not None:
            return
        import ctranslate2
        from faster_whisper import WhisperModel
        use_cuda = self.requested_device == "cuda"
        if self.requested_device == "auto":
            try:
                use_cuda = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                use_cuda = False
        if use_cuda:
            if sys.platform == "win32":
                # Missing CUDA/cuDNN DLLs can terminate CTranslate2 during inference.
                # Load them before the model and retain their handles.
                try:
                    self._cuda_libraries = [ctypes.WinDLL(name) for name in (
                        "cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll",
                        "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll",
                    )]
                except OSError as exc:
                    self.on_status(f"无法加载 CUDA 12 / cuDNN 9 运行库，改用 CPU：{exc}")
                    self._load_cpu()
                    return
            self.device = "cuda"
            self.compute_type = "float16"
            self.model_name = "medium.en" if self.requested_model == "auto" else self.requested_model
            self.on_status(f"正在加载识别模型：{self.model_name} / CUDA FP16（首次使用需下载）")
            try:
                self._model = WhisperModel(self.model_name, device="cuda", compute_type="float16",
                                           num_workers=1)
                return
            except Exception as exc:
                self.on_status(f"GPU 初始化失败，改用 CPU：{exc}")
        self._load_cpu()

    @staticmethod
    def _key(word: str) -> str:
        return re.sub(r"[^\w']", "", word).casefold()

    def _recognize(self, segment: SpeechSegment):
        # Consume the lazy result here so inference errors reach the CPU fallback.
        segments, _info = self._model.transcribe(
            segment.samples, language="en", task="transcribe", beam_size=1,
            word_timestamps=True, condition_on_previous_text=False,
            vad_filter=False, temperature=0.0, no_speech_threshold=.6,
            compression_ratio_threshold=2.4, log_prob_threshold=-1.0,
        )
        return list(segments)

    def transcribe(self, segment: SpeechSegment) -> Transcript | None:
        self.load()
        try:
            recognized = self._recognize(segment)
        except Exception as exc:
            if self.device != "cuda":
                raise
            self.on_status(f"GPU 识别失败，改用 CPU：{exc}")
            self._model = None
            self._load_cpu()
            recognized = self._recognize(segment)
        # Match words by time within the overlap; later repetitions are separate speech.
        words = []
        previous = list(self._recent_words)
        for part in recognized:
            if getattr(part, "no_speech_prob", 0) > .8 and getattr(part, "avg_logprob", 0) < -.5:
                continue
            part_words = getattr(part, "words", None)
            if not part_words:
                # Without word timestamps, drop only segments fully inside the overlap.
                if segment.overlap_seconds and part.end <= segment.overlap_seconds:
                    continue
                words.append(part.text)
                continue
            for word in part_words:
                start = segment.started_at + word.start
                end = segment.started_at + word.end
                key = self._key(word.word)
                is_overlap = word.start < segment.overlap_seconds and start <= self._last_segment_end + .08
                duplicate = is_overlap and any(
                    key and key == old_key and abs(start - old_start) < .35 and abs(end - old_end) < .35
                    for old_key, old_start, old_end in previous
                )
                if duplicate:
                    continue
                words.append(word.word)
                self._recent_words.append((key, start, end))
        self._last_segment_end = max(self._last_segment_end, segment.ended_at)
        text = "".join(words).strip()
        if not text:
            return None
        return Transcript(segment.segment_id, text, segment.started_at, segment.ended_at)
