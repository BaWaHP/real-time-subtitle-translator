"""Run real Qt controls in an isolated process without touching user settings."""

import os
from pathlib import Path
import subprocess
import sys


def test_edge_shortcut_explicit_source_and_latency_diagnostics(tmp_path):
    script = r'''
from PySide6.QtWidgets import QApplication
from config import AppConfig
from ui.main_window import MainWindow

app = QApplication([])
config = AppConfig()
config.save = lambda: None
window = MainWindow(config)
starts = []
window.start_requested.connect(lambda config: starts.append(config.source_mode))
assert not window.edge_start_button.isEnabled()
window.tabs.setCurrentWidget(window.edge_tab)
assert config.source_mode == 'system', 'Navigating a tab must not silently change the source'
assert '系统声音' in window.active_source_label.text()
assert '系统声音' in window.start_button.text()
window.start_button.click()
assert starts == ['system']

window.set_video_sessions([{
    'session_id': 'edge-test', 'title': 'English lesson',
    'page_url': 'https://example.com/lesson', 'current_time': 40,
    'playback_rate': 1, 'paused': False,
}])
assert window.edge_start_button.isEnabled()
window.edge_start_button.click()
assert starts == ['system', 'edge']
assert config.source_mode == 'edge'
assert config.video_session_id == 'edge-test'
assert 'Edge 视频' in window.active_source_label.text()
window.set_running(True)
assert '正在使用：Edge 视频' in window.active_source_label.text()
assert not window.edge_start_button.isEnabled()
assert not window.source_mode_combo.isEnabled()
window._start_video()
assert starts == ['system', 'edge'], 'A running shortcut must never start another pipeline'
window.source_mode_combo.setCurrentIndex(window.source_mode_combo.findData('system'))
assert config.source_mode == 'edge', 'Running source selection must remain fixed'
assert window.source_mode_combo.currentData() == 'edge'

window.update_metrics({
    'video_position': 40, 'prepared_until': 55,
    'video_prefetch_seconds': 15, 'video_connected': True,
    'video_state_age': .2, 'latency': 9,
    'video_caption_origin': 'browser', 'video_translation_inflight': 2,
    'video_translation_request_seconds': 1.4,
})
assert window.latency_label.text() == '0.2 秒前', 'Cue age must not be presented as clock delay'
assert window.latency_title_label.text() == '播放器状态更新'
assert '浏览器英文字幕' in window.video_prepared_label.text()
assert '连续就绪 15.0 秒' in window.video_prepared_label.text()
assert '翻译请求进行中 1.4 秒' in window.video_prepared_label.text()
window.update_metrics({
    'video_position': 42, 'prepared_until': 42, 'video_prefetch_seconds': 0,
    'video_connected': False, 'video_caption_origin': 'audio',
    'video_translation_inflight': 0, 'video_translation_request_seconds': 3.5,
})
assert window.latency_label.text() == '等待连接'
assert '本地音轨识别' in window.video_prepared_label.text()
assert '字幕准备中' in window.video_prepared_label.text()
assert '上次翻译请求 3.5 秒' in window.video_prepared_label.text()
window.set_running(False)
window.set_video_sessions([])
assert not window.edge_start_button.isEnabled()
window.source_mode_combo.setCurrentIndex(window.source_mode_combo.findData('system'))
window.update_metrics({'latency': 2.5})
assert window.latency_label.text() == '2.5 秒'
assert window.latency_title_label.text() == '当前延迟'
window.finish_close()
print('Explicit video source and latency diagnostics passed')
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
                       LOCALAPPDATA=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Explicit video source and latency diagnostics passed" in result.stdout
