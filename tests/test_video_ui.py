"""Keep QApplication isolation while testing timeline and Edge controls."""

import os
from pathlib import Path
import subprocess
import sys


def test_edge_controls_and_video_overlay_lifetime(tmp_path):
    script = r'''
import json
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from config import AppConfig
from ui.main_window import MainWindow

app = QApplication([])
config = AppConfig()
config.save = lambda: None
window = MainWindow(config)
assert window.device_combo.isEnabled()
assert not window.copy_edge_code_button.isEnabled()
window.set_edge_bridge(18765, 'local-pairing-secret-for-test')
window.copy_edge_code_button.click()
assert json.loads(app.clipboard().text()) == {
    'port': 18765, 'token': 'local-pairing-secret-for-test'
}
assert 'local-pairing-secret-for-test' not in window.edge_address_label.text()
window.set_edge_status('connection local-pairing-secret-for-test')
assert 'local-pairing-secret-for-test' not in window.edge_status_label.text()
window.source_mode_combo.setCurrentIndex(window.source_mode_combo.findData('edge'))
assert not window.device_combo.isEnabled()
assert window.asr_model_combo.isEnabled()
window.video_audio_check.setChecked(False)
assert not window.asr_model_combo.isEnabled()
window.video_audio_check.setChecked(True)
window.set_video_sessions([{
    'session_id': 'first', 'title': 'English video', 'page_url': 'https://example.com/watch',
    'current_time': 42.5, 'playback_rate': 1.5, 'paused': True,
}])
assert window.video_session_combo.currentData() == 'first'
assert '42.5' in window.video_details_label.text()
assert '1.5' in window.video_details_label.text()
window.video_prefetch_spin.setValue(60)
selected = window.current_config()
assert selected.source_mode == 'edge'
assert selected.video_session_id == 'first'
assert selected.video_prefetch_seconds == 60
assert selected.video_audio_fallback is True
starts = []
window.start_requested.connect(starts.append)
window.start_button.click()
assert starts == [config]
window.set_running(True)
assert not window.source_mode_combo.isEnabled()
assert not window.video_session_combo.isEnabled()
window.set_video_sessions([])
assert window.video_session_combo.currentData() == 'first'
window.set_running(False)
window.set_video_sessions([])
window.start_button.click()
assert starts == [config], 'Missing Edge sessions must not start a pipeline'
assert window.tabs.currentWidget() is window.edge_tab
window.update_metrics({'video_position': 9.5, 'prepared_until': 47.0})
assert '9.5' in window.video_prepared_label.text()
assert '47.0' in window.video_prepared_label.text()

overlay = window.overlay
config.overlay.display_seconds = 1
overlay.show_subtitle({'english': 'Previous audio', 'chinese': '上一条'})
assert overlay._expire.isActive()
overlay.show_timed_subtitle({'english': 'Paused video', 'chinese': '暂停的视频'})
assert not overlay._expire.isActive()
QTest.qWait(1100)
assert overlay.isVisible(), 'Video cues must survive the system-audio timer'
assert overlay._chinese == '暂停的视频'
overlay.show_timed_subtitle(None)
assert not overlay.isVisible()
overlay.show_subtitle({'chinese': '恢复系统声音'})
assert overlay._expire.isActive(), 'System mode must retain timed expiry'
window.finish_close()
print('Edge controls and timeline overlay passed')
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
                       LOCALAPPDATA=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Edge controls and timeline overlay passed" in result.stdout
