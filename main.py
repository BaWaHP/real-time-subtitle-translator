from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading


def cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Real-Time Subtitle Translator")
    parser.add_argument("--list-devices", action="store_true", help="列出系统播放设备")
    parser.add_argument("--diagnose", action="store_true", help="检查依赖、GPU 和音频设备")
    parser.add_argument("--login", action="store_true", help="在浏览器中登录新的 ChatGPT 账户")
    parser.add_argument("--test-translation", metavar="ENGLISH", help="翻译一段英文")
    parser.add_argument("--model", help="翻译模型的 slug，默认使用账户列表中的第一个模型")
    parser.add_argument("--demo", action="store_true", help="预览字幕窗口")
    parser.add_argument("--smoke-gui", action="store_true", help="离屏检查 GUI 并在 2 秒后退出")
    return parser


def diagnostics() -> int:
    import importlib.metadata
    import ctypes.util
    import platform
    print(f"Python {platform.python_version()}, {platform.system()} {platform.machine()}")
    for package in ("PySide6", "faster-whisper", "PyAudioWPatch", "onnxruntime", "soxr", "httpx", "PyJWT"):
        try:
            print(f"{package}: {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"{package}: MISSING")
    import ctranslate2
    print(f"CUDA devices: {ctranslate2.get_cuda_device_count()}")
    for library in ("cublas64_12", "cudnn64_9"):
        print(f"{library}: {ctypes.util.find_library(library) or 'not on PATH; CPU fallback available'}")
    from audio.capture import list_loopback_devices
    devices = list_loopback_devices()
    for device in devices:
        print(device)
    return 0 if devices else 1


def gui(args) -> int:
    if args.smoke_gui:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject, QTimer, Signal, Slot
    from PySide6.QtWidgets import QApplication
    from audio.capture import list_loopback_devices
    from config import AppConfig
    from core.pipeline import Pipeline
    from core.subtitle_queue import SubtitleQueue
    from core.types import Subtitle
    from translator.chatgpt_auth import ChatGPTAuth
    from translator.openai_translator import MockTranslator, OpenAITranslator
    from ui.main_window import MainWindow
    from video.bridge import EdgeBridge
    from video.pipeline import VideoPipeline
    import time

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Real-Time Subtitle Translator")
    app.setOrganizationName("RealtimeAISubtitle")
    app.setQuitOnLastWindowClosed(False)
    config = AppConfig.load()

    class Controller(QObject):
        completed = Signal(object, object)
        failed = Signal(str)
        status = Signal(str)

        def __init__(self):
            super().__init__()
            self.cancel = threading.Event()
            self._pending_tasks = 0
            self._closing = False
            self.auth = ChatGPTAuth(on_status=self.status.emit)
            self.window = MainWindow(config, self.auth)
            self.pipeline = Pipeline(self)
            self.video_pipeline = VideoPipeline(self)
            self.edge_bridge = EdgeBridge()
            self._edge_bridge_ok = False
            self.queue = SubtitleQueue(display_seconds=config.overlay.display_seconds,
                                       max_age=config.max_segment_age)
            self.completed.connect(self.deliver)
            self.failed.connect(self.handle_failed)
            self.status.connect(self.window.set_status)
            self.pipeline.status.connect(self.window.set_status)
            self.pipeline.error.connect(self.window.show_error)
            self.pipeline.metrics.connect(self.window.update_metrics)
            self.pipeline.running_changed.connect(self.running_changed)
            self.pipeline.subtitle.connect(self.queue_subtitle)
            self.video_pipeline.status.connect(self.window.set_status)
            self.video_pipeline.error.connect(self.window.show_error)
            self.video_pipeline.metrics.connect(self.window.update_metrics)
            self.video_pipeline.running_changed.connect(self.running_changed)
            self.video_pipeline.subtitle.connect(self.show_video_subtitle)
            self.window.start_requested.connect(self.start)
            self.window.stop_requested.connect(self.stop)
            self.window.pause_requested.connect(self.pause)
            self.window.login_requested.connect(self.login)
            self.window.logout_requested.connect(self.logout)
            self.window.account_selected.connect(self.select_account)
            self.window.refresh_models_requested.connect(self.refresh_models)
            self.window.refresh_devices_requested.connect(
                lambda: self.background(list_loopback_devices, self.window.set_devices))
            self.window.test_translation_requested.connect(self.test_translation)
            self.window.close_requested.connect(self.request_close)
            self.window.refresh_video_sessions_requested.connect(self.refresh_video_sessions)
            self.tick = QTimer(self)
            self.tick.setInterval(80)
            self.tick.timeout.connect(self.display_tick)
            self.tick.start()
            self.edge_tick = QTimer(self)
            self.edge_tick.setInterval(1000)
            self.edge_tick.timeout.connect(self.refresh_video_sessions)
            try:
                self.edge_bridge.start()
                self._edge_bridge_ok = True
                self.window.set_edge_bridge(self.edge_bridge.port, self.edge_bridge.token)
                self.edge_tick.start()
            except OSError:
                self.window.set_edge_status("Edge 连接服务无法启动，请重启软件或切换到系统声音模式。")
            app.aboutToQuit.connect(self.shutdown)
            self.window.set_auth_status(self.auth.status())
            self.window.set_accounts(self.auth.accounts(), self.auth.active_account_id)
            if not args.smoke_gui:
                self.background(list_loopback_devices, self.window.set_devices)
                if self.auth.is_authenticated:
                    self.refresh_models()
            self.window.show()

        def background(self, work, callback):
            self._pending_tasks += 1
            self.window.set_busy(True)
            def run():
                try:
                    value = work()
                    self.completed.emit(callback, value)
                except Exception as exc:
                    self.failed.emit(f"{type(exc).__name__}: {exc}")
            threading.Thread(target=run, daemon=True, name="subtitle-control-task").start()

        @Slot(object, object)
        def deliver(self, callback, value):
            try:
                if not self._closing:
                    callback(value)
            except Exception as exc:
                self.window.show_error(f"{type(exc).__name__}: {exc}")
            finally:
                self._pending_tasks -= 1
                self.window.set_busy(self._closing or self._pending_tasks > 0)
                self.check_close()

        @Slot(str)
        def handle_failed(self, message):
            self._pending_tasks = max(0, self._pending_tasks - 1)
            self.window.set_busy(self._closing or self._pending_tasks > 0)
            if not self._closing:
                self.window.set_auth_status(self.auth.status())
                self.window.show_error(message)
                self.window.set_test_result(message)
            self.check_close()

        @Slot(object)
        def login(self, account_id=None):
            if self.any_running():
                self.window.show_error("请先点击 Stop，再更换账户或重新授权。")
                return
            self.window.set_auth_status("等待浏览器完成授权…")
            def done(_):
                self.window.set_auth_status(self.auth.status())
                self.window.set_accounts(self.auth.accounts(), self.auth.active_account_id)
                self.refresh_models()
            needs_consent = bool(account_id and any(a["id"] == account_id and not a["plan_enabled"]
                                                    for a in self.auth.accounts()))
            self.background(lambda: self.auth.login(account_id=account_id, cancel_event=self.cancel,
                                                    request_consent=needs_consent), done)

        @Slot()
        def logout(self):
            if self.any_running():
                self.window.show_error("请先点击 Stop，再退出 ChatGPT。")
                return
            def done(_):
                self.window.set_models([])
                self.window.set_auth_status(self.auth.status())
                self.window.set_accounts(self.auth.accounts(), self.auth.active_account_id)
            self.background(self.auth.logout, done)

        @Slot(str)
        def select_account(self, account_id):
            if self.any_running():
                self.window.show_error("请先点击 Stop，再更换账户。")
                self.window.set_accounts(self.auth.accounts(), self.auth.active_account_id)
                return
            self.window.set_models([])
            def done(_):
                self.window.set_auth_status(self.auth.status())
                self.refresh_models()
            self.background(lambda: self.auth.select_account(account_id), done)

        @Slot()
        def refresh_models(self):
            if not self.auth.is_authenticated:
                self.window.set_models([])
                return
            def work():
                translator = OpenAITranslator(self.auth, "")
                try:
                    return translator.list_models()
                finally:
                    translator.close()
            self.background(work, self.window.set_models)

        @Slot(str)
        def test_translation(self, text):
            if not self.auth.is_authenticated:
                self.window.show_error("请先点击 Continue with ChatGPT 登录。")
                return
            model = config.translation_model
            if not model:
                self.window.show_error("请选择翻译模型，列表为空时可点击「刷新模型」。")
                return
            self.status.emit("翻译中…")
            def work():
                translator = OpenAITranslator(self.auth, model)
                try:
                    return translator.translate(text, [], self.cancel)
                finally:
                    translator.close()
            self.background(work,
                            lambda text: self.window.set_status(f"翻译完成：{text}"))

        @Slot(object)
        def start(self, selected_config):
            if self.any_running():
                self.window.show_error("请先点击 Stop，再切换模式。")
                return
            if self._pending_tasks:
                self.window.show_error("正在连接，请稍候再点击 Start。")
                return
            if selected_config.source_mode == "edge":
                if not self._edge_bridge_ok:
                    self.window.show_error("Edge 连接服务未启动，请重启应用后重试。")
                    return
                if not selected_config.video_session_id or not self.edge_bridge.snapshot(selected_config.video_session_id):
                    self.window.show_error("请在 Edge 扩展中连接视频，再从列表中选择。")
                    return
            if selected_config.translation_mode == "mock":
                translator = MockTranslator()
            else:
                if not self.auth.is_authenticated or not selected_config.translation_model:
                    self.window.show_error("请先登录 ChatGPT 并选择模型。也可用 Mock 模式试运行。")
                    return
                translator = OpenAITranslator(self.auth, selected_config.translation_model,
                                              on_status=self.status.emit)
            self.queue.clear()
            self.window.overlay.clear()
            self.queue.display_seconds = selected_config.overlay.display_seconds
            self.queue.max_age = selected_config.max_segment_age
            selected_config.save()
            if selected_config.source_mode == "edge":
                self.video_pipeline.start(selected_config, translator, self.edge_bridge)
            else:
                self.pipeline.start(selected_config, translator)

        @Slot()
        def stop(self):
            self.pipeline.stop()
            self.video_pipeline.stop()
            self.queue.clear()
            self.window.overlay.clear()

        @Slot(bool)
        def pause(self, paused):
            pipeline = self.video_pipeline if config.source_mode == "edge" else self.pipeline
            (pipeline.pause if paused else pipeline.resume)()
            self.queue.clear()
            self.window.overlay.clear()

        @Slot(bool)
        def running_changed(self, running):
            self.window.set_running(self.any_running())
            if self._closing:
                self.window.set_busy(True)
            if not self.any_running():
                self.queue.clear()
                self.window.overlay.clear()
            self.check_close()

        @Slot(object)
        def queue_subtitle(self, subtitle):
            if self.pipeline.accepts(subtitle):
                self.queue.push(subtitle)

        @Slot()
        def display_tick(self):
            if config.source_mode == "edge":
                return
            changed, subtitle = self.queue.tick(time.monotonic())
            if changed:
                if subtitle:
                    self.window.overlay.show_subtitle(subtitle)
                else:
                    self.window.overlay.clear()

        @Slot()
        def shutdown(self):
            self.cancel.set()
            self.pipeline.stop()
            self.video_pipeline.stop()

        @Slot()
        def request_close(self):
            self._closing = True
            self.cancel.set()
            self.tick.stop()
            self.edge_tick.stop()
            self.queue.clear()
            self.window.overlay.clear()
            self.window.set_busy(True)
            self.pipeline.stop()
            self.video_pipeline.stop()
            self.check_close()

        def check_close(self):
            if self._closing and not self.any_running() and self._pending_tasks == 0:
                self.edge_bridge.stop()
                self.auth.close()
                self.window.finish_close()
                app.quit()

        def any_running(self):
            return self.pipeline.is_running or self.video_pipeline.is_running

        @Slot()
        def refresh_video_sessions(self):
            if not self._edge_bridge_ok or self._closing:
                return
            sessions = self.edge_bridge.sessions()
            self.window.set_video_sessions(sessions)
            if sessions:
                latest = max(sessions, key=lambda s: s["received_at"])
                warning = self.edge_bridge.warning(latest["session_id"])
                self.window.set_edge_status(warning or f"已连接 {len(sessions)} 个视频")
            else:
                self.window.set_edge_status("等待 Edge：复制连接码，在扩展中连接当前视频。")

        @Slot(object)
        def show_video_subtitle(self, subtitle):
            if self._closing or config.source_mode != "edge" or not self.video_pipeline.is_running:
                return
            if subtitle is None:
                self.window.overlay.clear()
            elif self.video_pipeline.accepts(subtitle):
                self.window.overlay.show_timed_subtitle(subtitle)

    controller = Controller()
    if args.demo or args.smoke_gui:
        QTimer.singleShot(200, lambda: controller.window.overlay.show_subtitle(
            Subtitle(1, "This is a subtitle preview.",
                     "这是字幕预览。", 0, 0, time.monotonic())))
    if args.smoke_gui:
        QTimer.singleShot(1200, lambda: controller.window.grab().save(str(Path("artifacts") / "main-window.png")))
        QTimer.singleShot(1300, lambda: controller.window.overlay.grab().save(str(Path("artifacts") / "overlay.png")))
        QTimer.singleShot(2000, controller.request_close)
    return app.exec()


def main() -> int:
    args = cli_parser().parse_args()
    try:
        if args.diagnose:
            return diagnostics()
        if args.list_devices:
            from audio.capture import list_loopback_devices
            for device in list_loopback_devices():
                print(device)
            return 0
        if args.login or args.test_translation:
            from translator.chatgpt_auth import ChatGPTAuth
            from translator.openai_translator import OpenAITranslator
            auth = ChatGPTAuth(on_status=print)
            try:
                if args.login:
                    auth.login()
                    print(auth.status())
                if args.test_translation:
                    translator = OpenAITranslator(auth, args.model or "")
                    try:
                        models = translator.list_models()
                        if not models:
                            raise RuntimeError("账户没有可用模型，请确认订阅授权资格或重新登录。")
                        translator.model = args.model or models[0]["slug"]
                        if translator.model not in {m["slug"] for m in models}:
                            raise RuntimeError("当前账户无法使用指定的模型。")
                        print(f"Model: {translator.model}")
                        print(translator.translate(args.test_translation, []))
                    finally:
                        translator.close()
            finally:
                auth.close()
            return 0
        if args.smoke_gui:
            Path("artifacts").mkdir(exist_ok=True)
        return gui(args)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
