"""Run QWidget tests in their own process so QCoreApplication cannot mask GUI bugs."""
import os
from pathlib import Path
import subprocess
import sys


def test_close_waits_for_initializing_worker_and_exits_cleanly(tmp_path):
    script = r'''
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow
import main

loaded = threading.Event()
closed_at = []
still_visible = []

class Whisper:
    def __init__(self, **kwargs): pass
    def load(self):
        loaded.set()
        time.sleep(.65)

class Vad:
    def load(self): pass
    def reset(self): pass

class Capture:
    def __init__(self, output_queue, stop_event, pause_event, **kwargs): self.stop = stop_event
    def run(self): self.stop.wait()

sys.modules['asr.whisper_engine'] = SimpleNamespace(WhisperEngine=Whisper)
sys.modules['audio.vad'] = SimpleNamespace(VadSegmenter=Vad)
sys.modules['audio.capture'] = SimpleNamespace(LoopbackCapture=Capture, list_loopback_devices=lambda: [])

original_exec = QApplication.exec
def window():
    return next(w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))
def start(): window().start_button.click()
def close():
    assert loaded.is_set(), 'ASR initialization was never started'
    closed_at.append(time.monotonic())
    window().close()
def check_waiting(): still_visible.append(window().isVisible())
def event_loop(app):
    QTimer.singleShot(100, start)
    QTimer.singleShot(200, close)
    QTimer.singleShot(300, check_waiting)
    QTimer.singleShot(5000, lambda: app.exit(99))
    return original_exec()

with patch.object(QApplication, 'exec', event_loop):
    result = main.gui(main.cli_parser().parse_args([]))
assert result == 0
assert still_visible == [True], 'Window closed before the worker finished'
assert time.monotonic() - closed_at[0] >= .4
assert not [t.name for t in threading.enumerate() if t.name.startswith('subtitle-')]
print('controlled shutdown passed')
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
                       LOCALAPPDATA=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "controlled shutdown passed" in result.stdout
