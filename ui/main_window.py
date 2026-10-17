"""Controls for audio capture, translation and subtitle display."""

from __future__ import annotations

import re
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal, Slot, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLayout, QMainWindow, QPlainTextEdit,
    QPushButton, QScrollArea, QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .subtitle_overlay import SubtitleOverlay, _value


STYLE = """
QWidget { background: #101824; color: #e5edf5; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QMainWindow { background: #101824; }
QLabel#title { font-size: 24px; font-weight: 700; color: #f3f7fb; }
QLabel#muted { color: #91a3b7; }
QLabel#badge { background: #183a37; color: #8ce4cc; border: 1px solid #25564e; border-radius: 12px; padding: 5px 12px; }
QLabel#metric { font-family: 'Segoe UI'; font-size: 22px; font-weight: 600; color: #eaf4ff; }
QFrame#card { background: #172230; border: 1px solid #2b3a4c; border-radius: 10px; }
QFrame#card QLabel { background: transparent; }
QLabel#cardTitle { font-size: 14px; font-weight: 600; color: #f0f6fc; }
QPushButton { background: #223346; border: 1px solid #3b5066; border-radius: 6px; min-height: 20px; padding: 8px 12px; }
QPushButton:hover { background: #2e465e; border-color: #6b8ba8; }
QPushButton:pressed { background: #152535; }
QPushButton:disabled { background: #18232f; color: #617286; border-color: #2a3848; }
QPushButton#primary { background: #6de3c5; color: #092722; border: 0; font-weight: 700; padding: 10px 20px; }
QPushButton#primary:hover { background: #9cefd9; }
QPushButton#primary:disabled { background: #284b45; color: #799b92; }
QPushButton#login { background: #edf4fa; color: #182431; border-color: #edf4fa; font-weight: 600; }
QPushButton#login:hover { background: #d2e2ef; }
QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit { background: #101b28; border: 1px solid #3a4c60; border-radius: 5px; padding: 6px 8px; selection-background-color: #2c6b61; }
QComboBox, QSpinBox, QDoubleSpinBox { min-height: 20px; }
QComboBox:focus, QPlainTextEdit:focus { border-color: #6de3c5; }
QComboBox:disabled { color: #64778b; border-color: #2b3a4b; }
QComboBox QAbstractItemView { background: #172736; color: #e5edf5; selection-background-color: #285448; }
QTabWidget::pane { border: 0; background: #101824; padding-top: 14px; }
QTabBar::tab { background: transparent; color: #91a3b7; padding: 10px 15px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #86e7cf; border-bottom: 2px solid #6de3c5; }
QTabBar::tab:hover { color: #e6f6f0; }
QCheckBox { background: transparent; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #52687d; background: #101b28; }
QCheckBox::indicator:checked { background: #6de3c5; border-color: #6de3c5; image: none; }
QSlider::groove:horizontal { height: 5px; background: #34465b; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #6de3c5; border-radius: 2px; }
QSlider::handle:horizontal { background: #d4fff2; width: 14px; margin: -5px 0; border-radius: 7px; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: #101824; width: 10px; margin: 2px 0; }
QScrollBar::handle:vertical { background: #3b5066; min-height: 32px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #637e99; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: #263b4f; color: #f1f6fa; border: 1px solid #55728d; padding: 5px; }
"""


class MainWindow(QMainWindow):
    start_requested = Signal(object)
    stop_requested = Signal()
    pause_requested = Signal(bool)
    login_requested = Signal(object)
    logout_requested = Signal()
    account_selected = Signal(str)
    refresh_models_requested = Signal()
    refresh_devices_requested = Signal()
    refresh_video_sessions_requested = Signal()
    test_translation_requested = Signal(str)
    close_requested = Signal()

    def __init__(self, config: Any, auth: Any = None) -> None:
        super().__init__()
        self.config = config
        self.auth = auth
        self._running = False
        self._active_source_mode: str | None = None
        self._paused = False
        self._busy = False
        self._allow_close = False
        self._closing = False
        self._building = True
        self._account_id = ""
        self._edge_port = 0
        self._edge_token = ""
        self._video_sessions: dict[str, dict[str, Any]] = {}
        self.overlay = SubtitleOverlay(config.overlay)
        self.setWindowTitle("实时字幕翻译")
        self.setMinimumSize(760, 600)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(max(760, min(980, available.width() - 48)),
                    max(600, min(880, available.height() - 48)))
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(self._label("实时字幕翻译", "title"), 1)
        self.run_badge = self._label("已停止", "badge")
        header.addWidget(self.run_badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live_tab(), "实时字幕")
        self.edge_tab = self._build_video_tab()
        self.tabs.addTab(self.edge_tab, "Edge 视频")
        self.tabs.addTab(self._build_style_tab(), "字幕样式")
        self.tabs.addTab(self._build_diagnostics_tab(), "连接诊断")
        layout.addWidget(self.tabs, 1)

        status_card, status_layout = self._card()
        self.status_label = QLabel("选择音源和翻译模式，点击 Start 开始。")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        self.active_source_label = self._label("", "muted")
        self.active_source_label.setWordWrap(True)
        status_layout.addWidget(self.active_source_label)
        metrics = QHBoxLayout()
        self.latency_label = self._label("—", "metric")
        self.queue_label = self._label("0 / 0 / 0", "metric")
        self.dropped_label = self._label("0", "metric")
        self.latency_title_label = self._label("当前延迟", "muted")
        for title, value in ((self.latency_title_label, self.latency_label), ("音频 / 识别 / 翻译队列", self.queue_label), ("已跳过片段", self.dropped_label)):
            group = QVBoxLayout()
            group.setSpacing(3)
            group.addWidget(title if isinstance(title, QLabel) else self._label(title, "muted"))
            group.addWidget(value)
            metrics.addLayout(group, 1)
        status_layout.addLayout(metrics)
        layout.addWidget(status_card)

        actions = QHBoxLayout()
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumWidth(118)
        self.stop_button = QPushButton("Stop")
        self.pause_button = QPushButton("暂停")
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.preview_button = QPushButton("预览字幕")
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.pause_button.clicked.connect(self._toggle_pause)
        self.preview_button.clicked.connect(self.overlay.preview)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.pause_button)
        actions.addStretch()
        actions.addWidget(self.preview_button)
        layout.addLayout(actions)
        self._building = False
        self._mode_changed()
        self._source_mode_changed()

    @staticmethod
    def _label(text: str, name: str = "") -> QLabel:
        label = QLabel(text)
        if name:
            label.setObjectName(name)
        return label

    @staticmethod
    def _card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(14)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            layout.addWidget(label)
        return frame, layout

    @staticmethod
    def _form() -> QFormLayout:
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return form

    @staticmethod
    def _scroll_page(content: QWidget) -> QScrollArea:
        # Scroll instead of compressing fields on small or scaled displays.
        content.layout().setContentsMargins(0, 0, 8, 0)
        content.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        for combo in content.findChildren(QComboBox):
            # Keep long names within the page width.
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    @staticmethod
    def _row() -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        return row

    def _build_live_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        card, box = self._card("音源与语音识别")
        form = self._form()
        self.source_mode_combo = QComboBox()
        self.source_mode_combo.addItem("系统声音", "system")
        self.source_mode_combo.addItem("Edge 视频", "edge")
        self._select_data(self.source_mode_combo, getattr(self.config, "source_mode", "system"))
        self.source_mode_combo.currentIndexChanged.connect(self._source_mode_changed)
        form.addRow("输入模式", self.source_mode_combo)
        self.device_combo = QComboBox()
        self.device_combo.addItem("系统默认播放设备", None)
        self.device_combo.setToolTip("选择正在播放声音的扬声器或耳机。")
        refresh = QPushButton("重新检测")
        refresh.clicked.connect(self.refresh_devices_requested.emit)
        self.device_refresh_button = refresh
        row = self._row()
        row.addWidget(self.device_combo, 1)
        row.addWidget(refresh)
        form.addRow("播放设备", row)
        self.asr_model_combo = QComboBox()
        for key, text in (("auto", "自动 · CPU small.en / NVIDIA medium.en"), ("tiny.en", "tiny.en · 最快"), ("base.en", "base.en · 轻量"), ("small.en", "small.en · CPU 推荐"), ("medium.en", "medium.en · NVIDIA 推荐")):
            self.asr_model_combo.addItem(text, key)
        self._select_data(self.asr_model_combo, getattr(self.config, "asr_model", "auto"))
        form.addRow("识别模型", self.asr_model_combo)
        self.asr_device_combo = QComboBox()
        for label, value in (("自动检测（GPU 不可用时使用 CPU）", "auto"), ("CPU · INT8", "cpu"), ("NVIDIA GPU · FP16（不可用时使用 CPU）", "cuda")):
            self.asr_device_combo.addItem(label, value)
        self._select_data(self.asr_device_combo, getattr(self.config, "asr_device", "auto"))
        form.addRow("运行设备", self.asr_device_combo)
        box.addLayout(form)
        self.source_hint = self._label("首次使用时会下载识别模型，请保持网络连接。", "muted")
        self.source_hint.setWordWrap(True)
        box.addWidget(self.source_hint)
        outer.addWidget(card)

        card, box = self._card("翻译设置")
        form = self._form()
        self.translation_mode_combo = QComboBox()
        self.translation_mode_combo.addItem("Mock · 仅显示原文", "mock")
        self.translation_mode_combo.addItem("ChatGPT", "chatgpt")
        self._select_data(self.translation_mode_combo, getattr(self.config, "translation_mode", "mock"))
        self.translation_mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("翻译模式", self.translation_mode_combo)
        self.account_combo = QComboBox()
        self.account_combo.addItem("尚未登录", "")
        self.account_combo.currentIndexChanged.connect(self._account_changed)
        self.login_button = QPushButton("Continue with ChatGPT")
        self.login_button.setObjectName("login")
        self.login_button.clicked.connect(lambda: self.login_requested.emit(self.account_combo.currentData() or None))
        row = self._row()
        row.addWidget(self.account_combo, 1)
        row.addWidget(self.login_button)
        form.addRow("ChatGPT 账户", row)
        self.translation_model_combo = QComboBox()
        self.translation_model_combo.addItem("请先登录", "")
        self.models_refresh_button = QPushButton("刷新模型")
        self.models_refresh_button.clicked.connect(self.refresh_models_requested.emit)
        row = self._row()
        row.addWidget(self.translation_model_combo, 1)
        row.addWidget(self.models_refresh_button)
        form.addRow("翻译模型", row)
        box.addLayout(form)
        self.auth_label = self._label("尚未登录", "muted")
        self.auth_label.setWordWrap(True)
        box.addWidget(self.auth_label)
        outer.addWidget(card)
        outer.addStretch()
        return self._scroll_page(tab)

    def _build_video_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        card, box = self._card("连接 Edge 视频")
        box.setSpacing(8)
        hint = self._label(
            "在 edge://extensions 中启用开发人员模式，点击「加载解压缩的扩展」并选择 edge_extension 文件夹。\n"
            "复制连接码并粘贴到扩展中，再点击「连接当前视频」。更换标签页后需重新连接。", "muted"
        )
        hint.setWordWrap(True)
        box.addWidget(hint)
        row = self._row()
        self.extension_folder_button = QPushButton("打开扩展文件夹")
        self.extension_folder_button.clicked.connect(self._open_extension_folder)
        self.copy_edge_code_button = QPushButton("复制连接码")
        self.copy_edge_code_button.setEnabled(False)
        self.copy_edge_code_button.clicked.connect(self._copy_edge_code)
        row.addWidget(self.extension_folder_button)
        row.addWidget(self.copy_edge_code_button)
        row.addStretch()
        box.addLayout(row)
        self.edge_address_label = self._label("本地连接尚未就绪。", "muted")
        self.edge_address_label.setWordWrap(True)
        box.addWidget(self.edge_address_label)
        self.edge_status_label = QLabel("等待 Edge 扩展连接。")
        self.edge_status_label.setWordWrap(True)
        box.addWidget(self.edge_status_label)
        outer.addWidget(card)

        card, box = self._card("视频与字幕预读")
        box.setSpacing(8)
        form = self._form()
        self.video_session_combo = QComboBox()
        self.video_session_combo.setMinimumHeight(32)
        self.video_session_combo.addItem("尚无已连接的视频", "")
        self.video_session_combo.setToolTip("在 Edge 扩展中连接视频后，从列表中选择。")
        self.video_session_combo.currentIndexChanged.connect(self._video_selection_changed)
        self.video_refresh_button = QPushButton("刷新视频")
        self.video_refresh_button.clicked.connect(self.refresh_video_sessions_requested.emit)
        self.edge_start_button = QPushButton("使用此视频并开始")
        self.edge_start_button.setObjectName("primary")
        self.edge_start_button.setToolTip("切换到 Edge 视频模式，字幕跟随播放进度。")
        self.edge_start_button.clicked.connect(self._start_video)
        row = self._row()
        row.addWidget(self.video_session_combo, 1)
        row.addWidget(self.video_refresh_button)
        row.addWidget(self.edge_start_button)
        form.addRow("已连接视频", row)
        self.video_prefetch_spin = QDoubleSpinBox()
        self.video_prefetch_spin.setMinimumHeight(32)
        self.video_prefetch_spin.setRange(10, 120)
        self.video_prefetch_spin.setSingleStep(5)
        self.video_prefetch_spin.setDecimals(0)
        self.video_prefetch_spin.setSuffix(" 秒")
        self.video_prefetch_spin.setValue(float(getattr(self.config, "video_prefetch_seconds", 45)))
        self.video_prefetch_spin.setToolTip("设置提前翻译的时长，开始播放或跳转后需等待预读。")
        form.addRow("预读时长", self.video_prefetch_spin)
        box.addLayout(form)
        self.video_audio_check = QCheckBox("无英文字幕时识别音轨")
        self.video_audio_check.setChecked(bool(getattr(self.config, "video_audio_fallback", True)))
        self.video_audio_check.toggled.connect(self._source_options_changed)
        box.addWidget(self.video_audio_check)
        self.video_details_label = self._label("请选择已连接的视频。", "muted")
        self.video_details_label.setWordWrap(True)
        box.addWidget(self.video_details_label)
        self.video_prepared_label = QLabel("尚未开始预读。")
        self.video_prepared_label.setWordWrap(True)
        box.addWidget(self.video_prepared_label)
        hint = self._label(
            "字幕随播放器暂停、跳转和变速；软件中的「暂停」只暂停翻译。\n"
            "无法读取视频时，可切换到系统声音模式。", "muted"
        )
        hint.setWordWrap(True)
        box.addWidget(hint)
        outer.addWidget(card)
        outer.addStretch()
        return self._scroll_page(tab)

    def _open_extension_folder(self) -> None:
        folder = Path(__file__).resolve().parents[1] / "edge_extension"
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _copy_edge_code(self) -> None:
        if not self._edge_port or not self._edge_token:
            return
        QApplication.clipboard().setText(json.dumps({"port": self._edge_port, "token": self._edge_token}))
        self.edge_status_label.setText("连接码已复制，请粘贴到 Edge 扩展。")

    def _build_style_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        card, box = self._card("字幕悬浮窗")
        form = self._form()
        settings = self.config.overlay
        self.screen_combo = QComboBox()
        for index, screen in enumerate(QApplication.screens()):
            size = screen.geometry().size()
            primary = " · 主显示器" if screen == QApplication.primaryScreen() else ""
            self.screen_combo.addItem(f"{screen.name()} · {size.width()} × {size.height()}{primary}", index)
        self._select_data(self.screen_combo, getattr(settings, "screen_index", 0))
        self.screen_combo.currentIndexChanged.connect(self._style_changed)
        form.addRow("字幕显示器", self.screen_combo)
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(16, 72)
        self.font_size_spin.setValue(int(getattr(settings, "font_size", 28)))
        self.font_size_spin.setSuffix(" px")
        self.font_size_spin.valueChanged.connect(self._style_changed)
        form.addRow("中文字号", self.font_size_spin)
        self.color_button = QPushButton(str(getattr(settings, "color", "#FFFFFF")))
        self.color_button.clicked.connect(lambda: self._choose_color("color", self.color_button))
        self.outline_color_button = QPushButton(str(getattr(settings, "outline_color", "#000000")))
        self.outline_color_button.clicked.connect(lambda: self._choose_color("outline_color", self.outline_color_button))
        row = self._row()
        row.addWidget(self.color_button)
        row.addWidget(self.outline_color_button)
        form.addRow("字幕 / 描边颜色", row)
        self.outline_spin = QDoubleSpinBox()
        self.outline_spin.setRange(0, 6)
        self.outline_spin.setSingleStep(0.5)
        self.outline_spin.setSuffix(" px")
        self.outline_spin.setValue(float(getattr(settings, "outline_width", 2)))
        self.outline_spin.valueChanged.connect(self._style_changed)
        form.addRow("描边宽度", self.outline_spin)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(round(float(getattr(settings, "background_opacity", 0.48)) * 100))
        self.opacity_label = QLabel(f"{self.opacity_slider.value()}%")
        self.opacity_label.setMinimumWidth(42)
        self.opacity_slider.valueChanged.connect(self._style_changed)
        row = self._row()
        row.addWidget(self.opacity_slider, 1)
        row.addWidget(self.opacity_label)
        form.addRow("背景不透明度", row)
        self.display_seconds_spin = QDoubleSpinBox()
        self.display_seconds_spin.setRange(2, 15)
        self.display_seconds_spin.setSingleStep(0.5)
        self.display_seconds_spin.setSuffix(" 秒")
        self.display_seconds_spin.setValue(float(getattr(settings, "display_seconds", 5)))
        self.display_seconds_spin.valueChanged.connect(self._style_changed)
        form.addRow("字幕最长显示时间", self.display_seconds_spin)
        box.addLayout(form)
        self.english_check = QCheckBox("同时显示英文原文")
        self.english_check.setChecked(bool(getattr(settings, "show_english", True)))
        self.english_check.toggled.connect(self._style_changed)
        self.clickthrough_check = QCheckBox("鼠标穿透")
        self.clickthrough_check.setToolTip("允许鼠标操作字幕下方的窗口。")
        self.clickthrough_check.setChecked(bool(getattr(settings, "click_through", False)))
        self.clickthrough_check.toggled.connect(self._style_changed)
        box.addWidget(self.english_check)
        box.addWidget(self.clickthrough_check)
        hint = self._label(
            "关闭鼠标穿透后可拖动字幕，切换显示器会将字幕移到底部中央。\n"
            "普通窗口和无边框全屏均可显示字幕，独占全屏可能无法显示。", "muted"
        )
        hint.setWordWrap(True)
        box.addWidget(hint)
        reset = QPushButton("重置字幕位置")
        reset.clicked.connect(lambda: self.overlay.place_on_screen(self.screen_combo.currentData() or 0))
        box.addWidget(reset)
        outer.addWidget(card)
        outer.addStretch()
        return self._scroll_page(tab)

    def _build_diagnostics_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        card, box = self._card("翻译测试")
        hint = self._label("登录并选择模型后，输入英文即可测试翻译。", "muted")
        hint.setWordWrap(True)
        box.addWidget(hint)
        self.test_input = QPlainTextEdit()
        self.test_input.setPlainText("We need to reach the next checkpoint before the storm arrives.")
        self.test_input.setMinimumHeight(76)
        self.test_input.setMaximumHeight(110)
        self.test_input.setPlaceholderText("输入英文…")
        self.test_input.setTabChangesFocus(True)
        box.addWidget(self.test_input)
        row = self._row()
        self.test_button = QPushButton("翻译")
        self.test_button.clicked.connect(self._test_translation)
        logout = QPushButton("退出当前账户")
        logout.clicked.connect(self.logout_requested.emit)
        self.logout_button = logout
        self.add_account_button = QPushButton("添加账户")
        self.add_account_button.clicked.connect(lambda: self.login_requested.emit(None))
        row.addWidget(self.test_button)
        row.addWidget(self.add_account_button)
        row.addWidget(logout)
        row.addStretch()
        box.addLayout(row)
        self.test_result = QLabel("尚无翻译结果。")
        self.test_result.setWordWrap(True)
        self.test_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.test_result)
        outer.addWidget(card)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(120)
        self.log.setPlaceholderText("运行日志")
        self.log.setMinimumHeight(150)
        outer.addWidget(self.log, 1)
        return self._scroll_page(tab)

    @staticmethod
    def _select_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _persist(self) -> None:
        save = getattr(self.config, "save", None)
        if callable(save):
            try:
                save()
            except OSError as exc:
                self.show_error(f"无法保存设置：{exc}")

    def _collect_config(self) -> Any:
        self.config.device_name = self.device_combo.currentData()
        self.config.asr_model = self.asr_model_combo.currentData() or "auto"
        self.config.asr_device = self.asr_device_combo.currentData() or "auto"
        self.config.translation_mode = self.translation_mode_combo.currentData() or "mock"
        self.config.translation_model = self.translation_model_combo.currentData() or ""
        self.config.source_mode = self.source_mode_combo.currentData() or "system"
        self.config.video_session_id = self.video_session_combo.currentData() or ""
        self.config.video_prefetch_seconds = self.video_prefetch_spin.value()
        self.config.video_audio_fallback = self.video_audio_check.isChecked()
        self._persist()
        return self.config

    def current_config(self) -> Any:
        return self._collect_config()

    def _start(self) -> None:
        if self._running or self._busy:
            return
        config = self._collect_config()
        if config.source_mode == "edge" and config.video_session_id not in self._video_sessions:
            self.show_error("请在「Edge 视频」中连接并选择视频。")
            self.tabs.setCurrentWidget(self.edge_tab)
            return
        if config.translation_mode == "chatgpt" and not self.translation_model_combo.currentData():
            self.show_error("请先登录 ChatGPT，再选择翻译模型。")
            return
        self.start_requested.emit(config)

    def _start_video(self) -> None:
        if self._running or self._busy:
            return
        if self.video_session_combo.currentData() not in self._video_sessions:
            self.show_error("请先通过 Edge 扩展连接视频，再从列表中选择。")
            return
        self._select_data(self.source_mode_combo, "edge")
        self._start()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText("恢复" if self._paused else "暂停")
        self.run_badge.setText("已暂停" if self._paused else "运行中")
        self.pause_requested.emit(self._paused)

    def _mode_changed(self, *_args) -> None:
        if self._building:
            return
        is_real = self.translation_mode_combo.currentData() == "chatgpt"
        self.translation_model_combo.setEnabled(is_real and not self._running and not self._busy)
        self.models_refresh_button.setEnabled(not self._running and not self._busy)
        self.config.translation_mode = self.translation_mode_combo.currentData() or "mock"
        self._persist()

    def _source_mode_changed(self, *_args) -> None:
        if self._building:
            return
        if self._running or self._busy:
            self.source_mode_combo.blockSignals(True)
            self._select_data(self.source_mode_combo, self._active_source_mode or self.config.source_mode)
            self.source_mode_combo.blockSignals(False)
            return
        video = self.source_mode_combo.currentData() == "edge"
        self.config.source_mode = "edge" if video else "system"
        self.source_hint.setText(
            "在「Edge 视频」中连接当前标签页，读取英文字幕或使用上方模型识别音轨。"
            if video else "首次使用时会下载识别模型，请保持网络连接。"
        )
        self._update_enabled()
        self._persist()

    def _update_source_status(self) -> None:
        source = self._active_source_mode if self._running else self.source_mode_combo.currentData()
        title = "Edge 视频" if source == "edge" else "系统声音"
        behavior = "跟随播放器时间，提前准备字幕" if source == "edge" else "播放后采集声音并识别"
        self.active_source_label.setText(f"{'正在使用' if self._running else '开始时使用'}：{title} · {behavior}")
        self.start_button.setText(f"Start · {title}")
        if not self._running:
            self.latency_title_label.setText("播放器状态更新" if source == "edge" else "当前延迟")
            self.latency_label.setText("—")

    def _source_options_changed(self, *_args) -> None:
        if not self._building:
            self._update_enabled()

    def _video_selection_changed(self, *_args) -> None:
        session = self._video_sessions.get(self.video_session_combo.currentData() or "")
        if session is None:
            self.video_details_label.setText("尚未连接视频，请在 Edge 扩展中连接当前标签页。")
            self._update_enabled()
            return
        try:
            position = max(0, float(session.get("current_time", 0)))
            rate = float(session.get("playback_rate", 1))
        except (TypeError, ValueError):
            position, rate = 0.0, 1.0
        state = "已暂停" if session.get("paused", True) else "播放中"
        try:
            host = urlsplit(str(session.get("page_url", ""))).hostname or "当前标签页"
        except ValueError:
            host = "当前标签页"
        self.video_details_label.setText(f"{host} · {state} · {position:.1f} 秒 · {rate:g} 倍速")
        self._update_enabled()

    def set_edge_bridge(self, port: int, token: str) -> None:
        """Store the pairing code in memory only."""
        self._edge_port = int(port)
        self._edge_token = str(token)
        ready = 0 < self._edge_port <= 65535 and bool(self._edge_token)
        self.copy_edge_code_button.setEnabled(ready)
        self.edge_address_label.setText(
            f"本地连接：127.0.0.1:{self._edge_port} · 点击「复制连接码」连接 Edge。"
            if ready else "本地连接尚未就绪。"
        )

    def set_edge_status(self, text: str) -> None:
        self.edge_status_label.setText(self._redact(str(text)))

    def set_video_sessions(self, sessions: list[dict[str, Any]]) -> None:
        previous = self.video_session_combo.currentData() or getattr(self.config, "video_session_id", "")
        previous_label = self.video_session_combo.currentText()
        self._video_sessions = {str(s["session_id"]): s for s in sessions if s.get("session_id")}
        self.video_session_combo.blockSignals(True)
        self.video_session_combo.clear()
        for session_id, session in self._video_sessions.items():
            title = str(session.get("title") or "未命名视频")[:100]
            self.video_session_combo.addItem(title, session_id)
        if self._running and previous and previous not in self._video_sessions:
            self.video_session_combo.addItem(f"{previous_label.removesuffix(' · 等待重新连接')} · 等待重新连接", previous)
        if not self.video_session_combo.count():
            self.video_session_combo.addItem("尚无已连接的视频", "")
        self._select_data(self.video_session_combo, previous)
        self.video_session_combo.blockSignals(False)
        self._video_selection_changed()

    def _account_changed(self, *_args) -> None:
        if self._building:
            return
        account_id = self.account_combo.currentData() or ""
        if account_id != self._account_id:
            self._account_id = account_id
            self.account_selected.emit(account_id)

    def _style_changed(self, *_args) -> None:
        if self._building:
            return
        settings = self.config.overlay
        settings.font_size = self.font_size_spin.value()
        settings.outline_width = self.outline_spin.value()
        settings.background_opacity = self.opacity_slider.value() / 100
        settings.show_english = self.english_check.isChecked()
        settings.click_through = self.clickthrough_check.isChecked()
        settings.screen_index = self.screen_combo.currentData() or 0
        settings.display_seconds = self.display_seconds_spin.value()
        self.opacity_label.setText(f"{self.opacity_slider.value()}%")
        self.overlay.configure(settings)
        self._persist()

    def _choose_color(self, attribute: str, button: QPushButton) -> None:
        current = QColor(str(getattr(self.config.overlay, attribute)))
        color = QColorDialog.getColor(current, self, "选择字幕颜色" if attribute == "color" else "选择描边颜色")
        if color.isValid():
            setattr(self.config.overlay, attribute, color.name(QColor.NameFormat.HexRgb))
            button.setText(color.name(QColor.NameFormat.HexRgb).upper())
            self._style_changed()

    def _test_translation(self) -> None:
        text = self.test_input.toPlainText().strip()
        if not text:
            self.show_error("请输入英文。")
            return
        self._collect_config()
        self.test_result.setText("翻译中…")
        self.test_translation_requested.emit(text)

    def set_devices(self, devices: list[Any]) -> None:
        previous = self.device_combo.currentData() or getattr(self.config, "device_name", None)
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("系统默认播放设备", None)
        for device in devices:
            name = str(_value(device, ("name", "display_name", "device_name"), str(device)))
            default = bool(_value(device, ("is_default", "default"), False))
            label = f"{name}{' · 默认' if default else ''}"
            self.device_combo.addItem(label, name)
        self._select_data(self.device_combo, previous)
        self.device_combo.blockSignals(False)

    def set_models(self, models: list[Any]) -> None:
        previous = self.translation_model_combo.currentData() or getattr(self.config, "translation_model", "")
        self.translation_model_combo.blockSignals(True)
        self.translation_model_combo.clear()
        for model in models:
            if isinstance(model, str):
                slug = model
                label = model
            else:
                slug = str(_value(model, ("slug", "id", "model")))
                label = str(_value(model, ("display_name", "name"), slug))
            if slug:
                self.translation_model_combo.addItem(label, slug)
        if self.translation_model_combo.count() == 0:
            self.translation_model_combo.addItem("当前账户没有可用模型", "")
        self._select_data(self.translation_model_combo, previous)
        self.translation_model_combo.blockSignals(False)
        self.config.translation_model = self.translation_model_combo.currentData() or ""
        self._persist()

    def set_accounts(self, accounts: list[Any], active_id: str = "") -> None:
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in accounts:
            account_id = str(_value(account, ("id", "account_id")))
            name = str(_value(account, ("label", "email", "name"), account_id))
            if not _value(account, ("label",), ""):
                client_id = str(_value(account, ("client_id",), ""))
                if client_id:
                    name += f" · {client_id[-8:]}"
            self.account_combo.addItem(name, account_id)
        if self.account_combo.count() == 0:
            self.account_combo.addItem("尚未登录", "")
        self._select_data(self.account_combo, active_id)
        self._account_id = self.account_combo.currentData() or ""
        self.account_combo.blockSignals(False)
        self.logout_button.setEnabled(bool(self._account_id) and not self._running and not self._busy)

    def set_auth_status(self, text: str) -> None:
        self.auth_label.setText(self._redact(str(text)))

    @Slot(str)
    def set_status(self, text: str) -> None:
        safe = self._redact(str(text))
        prefix = "翻译完成："
        if safe.startswith(prefix):
            self.set_test_result(safe.removeprefix(prefix))
            safe = "翻译完成，结果见「连接诊断」。"
        self.status_label.setStyleSheet("")
        self.status_label.setText(safe)
        self._append_log(safe)

    @Slot(str)
    def show_error(self, text: str) -> None:
        safe = self._redact(str(text))
        self.status_label.setStyleSheet("color: #ffb5a9; background: transparent;")
        self.status_label.setText(safe)
        self._append_log(f"错误：{safe}")

    def _redact(self, text: str) -> str:
        if self._edge_token:
            text = text.replace(self._edge_token, "[连接密钥已隐藏]")
        text = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[已隐藏]", text)
        text = re.sub(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[令牌已隐藏]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[密钥已隐藏]", text)
        return text[:5000]

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

    def set_test_result(self, text: str) -> None:
        self.test_result.setText(str(text))

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._update_enabled()

    def _update_enabled(self) -> None:
        editable = not self._busy and not self._running
        for widget in (
            self.source_mode_combo, self.video_session_combo, self.video_refresh_button,
            self.video_prefetch_spin, self.video_audio_check,
            self.translation_mode_combo, self.account_combo,
            self.login_button, self.add_account_button,
        ):
            widget.setEnabled(editable)
        video = self.source_mode_combo.currentData() == "edge"
        self.device_combo.setEnabled(editable and not video)
        self.device_refresh_button.setEnabled(editable and not video)
        needs_asr = not video or self.video_audio_check.isChecked()
        self.asr_model_combo.setEnabled(editable and needs_asr)
        self.asr_device_combo.setEnabled(editable and needs_asr)
        self.test_button.setEnabled(not self._busy)
        self.models_refresh_button.setEnabled(editable)
        self.start_button.setEnabled(editable)
        self.logout_button.setEnabled(editable and bool(self._account_id))
        self.translation_model_combo.setEnabled(editable and self.translation_mode_combo.currentData() == "chatgpt")
        self.edge_start_button.setEnabled(editable and self.video_session_combo.currentData() in self._video_sessions)
        self._update_source_status()

    @Slot(dict)
    def update_metrics(self, metrics: dict[str, Any]) -> None:
        video = "video_position" in metrics or "prepared_until" in metrics
        if video:
            self.latency_title_label.setText("播放器状态更新")
            age = metrics.get("video_state_age")
            self.latency_label.setText(
                "等待连接" if not metrics.get("video_connected", True)
                else f"{float(age):.1f} 秒前" if age is not None else "已连接"
            )
            self.latency_label.setToolTip("距离上次收到 Edge 播放进度的时间。")
        else:
            self.latency_title_label.setText("当前延迟")
            self.latency_label.setToolTip("从采集声音到生成字幕所需的时间。")
            latency = _value(metrics, ("latency_seconds", "latency", "delay_seconds", "delay"), None)
            if latency is None and "latency_ms" in metrics:
                latency = float(metrics["latency_ms"]) / 1000
            if latency is not None:
                self.latency_label.setText(f"{float(latency):.1f} 秒")
        audio = _value(metrics, ("audio_queue", "audio_queue_size", "audio"), 0)
        asr = _value(metrics, ("segment_queue", "segment_queue_size", "asr_queue", "segments"), 0)
        translation = _value(metrics, ("transcript_queue", "transcript_queue_size", "translation_queue", "transcripts"), 0)
        self.queue_label.setText(f"{audio} / {asr} / {translation}")
        dropped = _value(metrics, ("dropped", "dropped_segments", "skipped"), 0)
        self.dropped_label.setText(str(dropped))
        if video:
            position = float(metrics.get("video_position", 0))
            prepared = float(metrics.get("prepared_until", 0))
            ahead = float(metrics.get("video_prefetch_seconds", max(0, prepared - position)))
            origin = {"browser": "浏览器英文字幕", "public": "公开字幕", "audio": "本地音轨识别"}.get(
                metrics.get("video_caption_origin"), "正在读取字幕或音轨"
            )
            inflight = int(metrics.get("video_translation_inflight", 0))
            request = float(metrics.get("video_translation_request_seconds", 0))
            request_text = f"翻译请求进行中 {request:.1f} 秒" if inflight else (
                f"上次翻译请求 {request:.1f} 秒" if request else "等待翻译任务"
            )
            preparation = f"连续就绪 {ahead:.1f} 秒" if ahead > 0 else "字幕准备中"
            self.video_prepared_label.setText(
                f"来源：{origin} · {preparation}\n"
                f"当前播放 {position:.1f} 秒 · 字幕已准备至 {prepared:.1f} 秒 · {request_text}"
            )

    @Slot(bool)
    def set_running(self, running: bool) -> None:
        if running and not self._running:
            self._active_source_mode = self.config.source_mode
        self._running = bool(running)
        if not running:
            self._active_source_mode = None
        self._paused = False
        self.run_badge.setText("运行中" if running else "已停止")
        self.pause_button.setText("暂停")
        self.stop_button.setEnabled(running)
        self.pause_button.setEnabled(running)
        self._update_enabled()

    def finish_close(self) -> None:
        """Close after the controller has stopped its workers."""
        self._allow_close = True
        self.close()

    def closeEvent(self, event) -> None:
        if not self._allow_close:
            event.ignore()
            if not self._closing:
                self._closing = True
                self.set_busy(True)
                self.set_status("正在停止，请稍候…")
                self.close_requested.emit()
            return
        self._collect_config()
        self.overlay.clear()
        self.overlay.close()
        event.accept()
