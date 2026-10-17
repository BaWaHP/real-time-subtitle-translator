"""WASAPI playback capture.

The audio callback enqueues bytes; the worker mixes and resamples them.
"""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable

import numpy as np

from core.types import AudioChunk

SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class AudioDevice:
    index: int
    name: str
    default: bool
    channels: int
    sample_rate: int


def _devices(manager) -> list[AudioDevice]:
    try:
        default_index = int(manager.get_default_wasapi_loopback()["index"])
    except (OSError, ValueError, KeyError, LookupError):
        default_index = -1
    return [
        AudioDevice(int(d["index"]), str(d["name"]), int(d["index"]) == default_index,
                    int(d["maxInputChannels"]), int(d["defaultSampleRate"]))
        for d in manager.get_loopback_device_info_generator()
        if d.get("isLoopbackDevice") and int(d["maxInputChannels"]) > 0
    ]


def list_loopback_devices() -> list[AudioDevice]:
    """Refresh the list of WASAPI playback loopback endpoints."""
    import pyaudiowpatch as pa
    with pa.PyAudio() as manager:
        return _devices(manager)


def put_latest(target: queue.Queue, item) -> bool:
    """Drop the oldest queued item on overflow without blocking capture."""
    try:
        target.put_nowait(item)
        return False
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(item)
        except queue.Full:
            pass
        return True


class StreamingConverter:
    """Convert interleaved signed 16-bit PCM to continuous 16 kHz mono float32."""
    def __init__(self, sample_rate: int, channels: int):
        import soxr
        self.sample_rate = sample_rate
        self.channels = channels
        self.resampler = soxr.ResampleStream(sample_rate, SAMPLE_RATE, 1,
                                              dtype="float32", quality="HQ")
        self.origin: float | None = None
        self.expected_input: float | None = None
        self.emitted = 0

    def reset(self) -> None:
        self.resampler.clear()
        self.origin = self.expected_input = None
        self.emitted = 0

    def convert(self, data: bytes, started_at: float) -> AudioChunk | None:
        pcm = np.frombuffer(data, dtype="<i2")
        if pcm.size == 0:
            return None
        if pcm.size % self.channels:
            raise ValueError("音频帧的声道数不完整")
        if self.expected_input is not None and abs(started_at - self.expected_input) > 0.15:
            self.reset()  # Restart resampling after an audio gap.
        if self.origin is None:
            self.origin = started_at
        mono = pcm.reshape(-1, self.channels).astype(np.float32).mean(axis=1) / 32768.0
        self.expected_input = started_at + mono.size / self.sample_rate
        samples = self.resampler.resample_chunk(mono)
        if not samples.size:
            return None
        result = AudioChunk(np.ascontiguousarray(samples, dtype=np.float32),
                            self.origin + self.emitted / SAMPLE_RATE)
        self.emitted += samples.size
        return result


class LoopbackCapture:
    """Capture worker; ``pause_event.set()`` pauses delivery.

    Reconnect by device name because PortAudio indices can change after hotplug.
    ``device_name=None`` follows the Windows default playback endpoint.
    """
    def __init__(self, output_queue: queue.Queue, stop_event: threading.Event,
                 pause_event: threading.Event, device_name: str | None = None,
                 on_status: Callable[[str], None] | None = None):
        if output_queue.maxsize <= 0:
            raise ValueError("音频输出队列必须设置 maxsize")
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.device_name = device_name
        self.on_status = on_status or (lambda _message: None)
        self.raw_dropped = 0
        self.output_dropped = 0
        self.device: AudioDevice | None = None

    def _select(self, manager) -> AudioDevice:
        devices = _devices(manager)
        if self.device_name:
            for device in devices:
                if device.name == self.device_name:
                    return device
            raise OSError(f"音频输出设备未连接：{self.device_name}")
        for device in devices:
            if device.default:
                return device
        raise OSError("未找到默认 WASAPI 系统声音设备")

    def run(self) -> None:
        import pyaudiowpatch as pa
        while not self.stop_event.is_set():
            stream = None
            manager = None
            try:
                manager = pa.PyAudio()
                device = self._select(manager)
                self.device = device
                converter = StreamingConverter(device.sample_rate, device.channels)
                raw: queue.Queue = queue.Queue(maxsize=100)  # Roughly two seconds.
                last_callback = time.monotonic()

                def callback(data, frame_count, time_info, status):
                    nonlocal last_callback
                    last_callback = time.monotonic()
                    if self.stop_event.is_set():
                        return None, pa.paComplete
                    if not self.pause_event.is_set():
                        start = last_callback - frame_count / device.sample_rate
                        if put_latest(raw, (data, start, status)):
                            self.raw_dropped += 1
                    return None, pa.paContinue

                stream = manager.open(format=pa.paInt16, channels=device.channels,
                                      rate=device.sample_rate, input=True,
                                      input_device_index=device.index,
                                      frames_per_buffer=max(256, int(device.sample_rate * .02)),
                                      stream_callback=callback)
                self.on_status(f"正在采集系统音频：{device.name}")
                was_paused = False
                while not self.stop_event.is_set():
                    if not stream.is_active():
                        raise OSError("WASAPI 音频流已断开")
                    if self.pause_event.is_set():
                        converter.reset()
                        while True:
                            try:
                                raw.get_nowait()
                            except queue.Empty:
                                break
                        was_paused = True
                        self.stop_event.wait(.05)
                        continue
                    if was_paused:
                        converter.reset()
                        was_paused = False
                    try:
                        data, timestamp, status = raw.get(timeout=.1)
                    except queue.Empty:
                        # Some drivers stop callbacks on disconnect but still report active.
                        if time.monotonic() - last_callback > 5:
                            raise OSError("系统音频流无响应，正在重新连接")
                        continue
                    if status & pa.paInputOverflow:
                        converter.reset()
                        self.raw_dropped += 1
                    chunk = converter.convert(data, timestamp)
                    if chunk is not None and put_latest(self.output_queue, chunk):
                        self.output_dropped += 1
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.on_status(f"音频连接中断，正在重连：{exc}")
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
                if manager is not None:
                    manager.terminate()
            self.stop_event.wait(1)
