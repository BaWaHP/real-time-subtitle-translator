"""Exercise the actual controller's input-mode branch in an isolated Qt process."""
import os
from pathlib import Path
import subprocess
import sys


def test_video_start_routes_to_video_pipeline_and_preserves_system_mode(tmp_path):
    script = r'''
import time
from unittest.mock import patch
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow
from video.bridge import EdgeBridge
import main

calls = []
class FakeVideo(QObject):
    status=Signal(str)
    error=Signal(str)
    metrics=Signal(dict)
    running_changed=Signal(bool)
    subtitle=Signal(object)
    is_running=False
    def start(self, config, translator, bridge):
        assert config.source_mode == 'edge'
        assert bridge.snapshot(config.video_session_id)
        self.translator=translator
        self.is_running=True
        calls.append('edge')
        self.running_changed.emit(True)
    def stop(self):
        if self.is_running:
            self.translator.close()
            self.is_running=False
            self.running_changed.emit(False)
    def accepts(self, subtitle): return self.is_running

original_start=EdgeBridge.start
def bridge_start(self):
    result=original_start(self)
    def heartbeat():
        self.receive({'type':'state','session_id':'test-tab:0','source_id':'test-video',
                      'page_url':'https://example.org/watch','current_time':0,'paused':True,
                      'duration':60,'title':'Test video'})
    heartbeat()
    self.fixture_timer=QTimer()
    self.fixture_timer.timeout.connect(heartbeat)
    self.fixture_timer.start(200)
    return result
def window():
    return next(w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))
def choose_video():
    w=window()
    # Real WASAPI device discovery runs asynchronously at startup. A fixed
    # 200ms click races that worker under full-suite/browser-test load.
    if not w.start_button.isEnabled():
        QTimer.singleShot(25,choose_video)
        return
    w.source_mode_combo.setCurrentIndex(w.source_mode_combo.findData('edge'))
    w.video_refresh_button.click()
    w.start_button.click()
    assert calls == ['edge']
    assert w.stop_button.isEnabled()
    QTimer.singleShot(50,stop_and_return)
def stop_and_return():
    w=window()
    w.stop_button.click()
    w.source_mode_combo.setCurrentIndex(w.source_mode_combo.findData('system'))
    assert w.config.source_mode == 'system'
    assert w.device_combo.isEnabled()
    w.close()
original_exec=QApplication.exec
def event_loop(app):
    QTimer.singleShot(200,choose_video)
    QTimer.singleShot(5000,lambda:app.exit(99))
    return original_exec()
with patch('video.pipeline.VideoPipeline',FakeVideo), patch.object(EdgeBridge,'start',bridge_start), \
     patch.object(QApplication,'exec',event_loop):
    result=main.gui(main.cli_parser().parse_args([]))
assert result == 0 and calls == ['edge']
print('video routing passed')
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
                       LOCALAPPDATA=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "video routing passed" in result.stdout
