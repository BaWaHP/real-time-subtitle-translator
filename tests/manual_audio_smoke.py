"""Real Windows playback → WASAPI → Silero → local CPU Whisper smoke test.

Run from the project root:
    .venv\Scripts\python.exe -m tests.manual_audio_smoke --model tiny.en

This plays a synthetic English sentence on the default output. It never opens a
microphone and never calls a translation service. Results are saved as JSON.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import wave
import winsound

import numpy as np

from audio.capture import LoopbackCapture, list_loopback_devices
from audio.vad import VadSegmenter
from asr.whisper_engine import WhisperEngine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="tiny.en")
    parser.add_argument("--output", default="artifacts/manual_audio_smoke.json")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sample = output.with_suffix(".wav")
    source = "The local subtitle translator is working. We are testing system audio capture on Windows."
    environment = dict(os.environ, SUBTITLE_SMOKE_WAV=str(sample), SUBTITLE_SMOKE_TEXT=source)
    subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet, "
        "[System.Speech.Synthesis.VoiceAge]::NotSet, 0, "
        "[System.Globalization.CultureInfo]::GetCultureInfo('en-US')); "
        "$s.Rate = -1; $s.SetOutputToWaveFile($env:SUBTITLE_SMOKE_WAV); "
        "$s.Speak($env:SUBTITLE_SMOKE_TEXT); $s.Dispose()",
    ], env=environment, check=True, capture_output=True, text=True)
    with wave.open(str(sample)) as wav:
        duration = wav.getnframes() / wav.getframerate()
    report = {"source": source, "model": args.model, "playback_seconds": duration,
              "devices": [asdict(d) for d in list_loopback_devices()]}
    engine = WhisperEngine(model=args.model, device="cpu", on_status=print)
    engine.load()
    vad = VadSegmenter()
    vad.load()
    audio = queue.Queue(maxsize=150)
    stop, pause = threading.Event(), threading.Event()
    capture = LoopbackCapture(audio, stop, pause, on_status=print)
    thread = threading.Thread(target=capture.run, name="ManualLoopbackSmoke", daemon=True)
    thread.start()
    deadline = time.monotonic() + 12
    while capture.device is None and time.monotonic() < deadline:
        time.sleep(.1)
    if capture.device is None:
        stop.set()
        raise RuntimeError("WASAPI loopback endpoint did not become available")
    report["selected_device"] = asdict(capture.device)
    # Confirm that paused capture discards live playback, then resume the same stream.
    pause.set()
    time.sleep(.2)
    while not audio.empty():
        audio.get_nowait()
    winsound.PlaySound(str(sample), winsound.SND_FILENAME | winsound.SND_ASYNC)
    time.sleep(.5)
    report["paused_queue_size"] = audio.qsize()
    winsound.PlaySound(None, 0)
    pause.clear()
    time.sleep(.2)
    while not audio.empty():
        audio.get_nowait()
    started = time.monotonic()
    winsound.PlaySound(str(sample), winsound.SND_FILENAME | winsound.SND_ASYNC)
    deadline = started + duration + 1.5
    transcripts = []
    chunks = samples = 0
    peak = 0.0
    try:
        while time.monotonic() < deadline:
            try:
                chunk = audio.get(timeout=.1)
            except queue.Empty:
                continue
            chunks += 1
            samples += len(chunk.samples)
            peak = max(peak, float(np.max(np.abs(chunk.samples))))
            for segment in vad.feed(chunk):
                inference_start = time.monotonic()
                transcript = engine.transcribe(segment)
                if transcript:
                    row = {"id": transcript.segment_id, "text": transcript.text,
                           "segment_seconds": len(segment.samples) / 16000,
                           "asr_seconds": round(time.monotonic() - inference_start, 3),
                           "end_to_asr_seconds": round(time.monotonic() - segment.ended_at, 3)}
                    transcripts.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
        for segment in vad.flush():
            transcript = engine.transcribe(segment)
            if transcript:
                transcripts.append({"id": transcript.segment_id, "text": transcript.text,
                                    "segment_seconds": len(segment.samples) / 16000})
    finally:
        stop.set()
        winsound.PlaySound(None, 0)
        thread.join(timeout=6)
    report.update(chunks=chunks, captured_seconds=samples / 16000, peak=peak,
                  raw_dropped=capture.raw_dropped, output_dropped=capture.output_dropped,
                  stopped=not thread.is_alive(), transcripts=transcripts)
    text = " ".join(row["text"] for row in transcripts).lower()
    report["passed"] = (report["paused_queue_size"] == 0 and chunks > 0 and peak > .01
                        and report["stopped"]
                        and sum(term in text for term in ("subtitle", "translator", "audio", "windows")) >= 2)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {output}; passed={report['passed']}", flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
