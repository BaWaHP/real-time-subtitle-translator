"""Visible Windows end-to-end phase 1 smoke test.

    .venv\Scripts\python.exe -m tests.manual_pipeline_smoke

Starts real Qt, Pipeline, CPU tiny.en, MockTranslator and the subtitle overlay.
Plays only synthesized English through the default Windows playback endpoint.
No microphone, OAuth credentials or translation network requests are involved.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave
import winsound

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QApplication

from config import AppConfig
from core.pipeline import Pipeline
from core.subtitle_queue import SubtitleQueue
from translator.openai_translator import MockTranslator
from ui.subtitle_overlay import SubtitleOverlay


class ObservedOverlay(SubtitleOverlay):
    def __init__(self, settings):
        super().__init__(settings)
        self.drawn: list[dict] = []

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._english and self._text_rows:
            value = {"english": self._english, "chinese": self._chinese,
                     "rows": len(self._text_rows)}
            if not self.drawn or value != self.drawn[-1]:
                self.drawn.append(value)


def synthesize(sample: Path, source: str) -> float:
    if not sample.exists():
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
        return wav.getnframes() / wav.getframerate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="tiny.en")
    parser.add_argument("--output", default="artifacts/manual_pipeline_smoke.json")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    source = "The local subtitle translator is working. We are testing system audio capture on Windows."
    sample = output.parent / "manual_audio_smoke.wav"
    duration = synthesize(sample, source)
    config = AppConfig(asr_model=args.model, asr_device="cpu", translation_mode="mock")
    config.overlay.display_seconds = 3.0
    config.overlay.click_through = True
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    report = {"source": source, "config": asdict(config), "statuses": [], "errors": [],
              "subtitles": [], "metrics": [], "stopped": False, "screenshot_saved": False}

    class Bridge(QObject):
        def __init__(self):
            super().__init__()
            self.pipeline = Pipeline(self)
            self.overlay = ObservedOverlay(config.overlay)
            self.scheduler = SubtitleQueue(display_seconds=config.overlay.display_seconds)
            self.playing = False
            self.stopping = False
            self.capture_at = None
            self.pipeline.status.connect(self.status)
            self.pipeline.error.connect(self.error)
            self.pipeline.subtitle.connect(self.subtitle)
            self.pipeline.metrics.connect(self.metrics)
            self.pipeline.running_changed.connect(self.running)
            self.tick = QTimer(self)
            self.tick.setInterval(80)
            self.tick.timeout.connect(self.display)
            self.tick.start()
            self.overall = QTimer(self)
            self.overall.setSingleShot(True)
            self.overall.timeout.connect(self.timeout)
            self.overall.start(90000)

        @Slot(str)
        def status(self, value):
            print(value, flush=True)
            report["statuses"].append(value)
            if value.startswith("系统音频：") and not self.playing:
                self.playing = True
                QTimer.singleShot(300, self.play)

        @Slot()
        def play(self):
            self.capture_at = time.monotonic()
            winsound.PlaySound(str(sample), winsound.SND_FILENAME | winsound.SND_ASYNC)
            QTimer.singleShot(round((duration + 3) * 1000), self.stop)

        @Slot(str)
        def error(self, value):
            report["errors"].append(value)
            print(value, flush=True)
            self.stop()

        @Slot(object)
        def subtitle(self, value):
            if not self.pipeline.accepts(value):
                return
            report["subtitles"].append(asdict(value))
            self.scheduler.push(value)
            print(json.dumps(asdict(value), ensure_ascii=False), flush=True)

        @Slot(dict)
        def metrics(self, value):
            report["metrics"].append(value)

        @Slot()
        def display(self):
            changed, value = self.scheduler.tick(time.monotonic())
            if changed:
                if value:
                    self.overlay.show_subtitle(value)
                    QTimer.singleShot(200, self.snapshot)
                else:
                    self.overlay.clear()

        @Slot()
        def snapshot(self):
            # Natural WM_PAINT must have occurred before grab can trigger paint.
            if self.overlay.drawn and self.overlay.isVisible() and not report["screenshot_saved"]:
                report["paint_before_snapshot"] = list(self.overlay.drawn)
                report["screenshot_saved"] = self.overlay.grab().save(str(output.with_suffix(".png")))
                report["overlay_geometry"] = self.overlay.geometry().getRect()

        @Slot()
        def stop(self):
            if self.stopping:
                return
            self.stopping = True
            winsound.PlaySound(None, 0)
            self.stop_at = time.monotonic()
            self.pipeline.stop()
            QTimer.singleShot(12000, self.timeout)

        @Slot(bool)
        def running(self, running):
            if not running:
                report["stopped"] = True
                report["stop_seconds"] = round(time.monotonic() - self.stop_at, 3)
                QTimer.singleShot(100, self.finish)

        @Slot()
        def timeout(self):
            if report["stopped"]:
                return
            report["errors"].append("Smoke test timed out")
            self.pipeline.stop()
            self.finish()

        @Slot()
        def finish(self):
            self.tick.stop()
            self.overall.stop()
            self.overlay.clear()
            report["drawn"] = list(self.overlay.drawn)
            report["workers_alive"] = [t.name for t in self.pipeline._threads if t.is_alive()]
            report["passed"] = bool(report["subtitles"] and report["drawn"]
                                    and report["screenshot_saved"] and report["stopped"]
                                    and not report["workers_alive"] and not report["errors"]
                                    and all(s["chinese"].startswith("[Mock") for s in report["subtitles"]))
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Saved {output}; passed={report['passed']}", flush=True)
            app.quit()

    bridge = Bridge()
    QTimer.singleShot(100, lambda: bridge.pipeline.start(config, MockTranslator()))
    app.exec()
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
