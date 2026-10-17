"""Real Edge DOM -> test transport -> HTTP bridge -> video pipeline -> Qt overlay.

Run with the project Python after installing the optional Playwright package:
    uv pip install playwright
    .venv\Scripts\python.exe tests/manual_edge_video_smoke.py

Requires an installed Microsoft Edge. This creates only generated local media
and a new temporary browser context; it never opens the user's existing tabs.
The real content.js reads native video/text-track APIs. Its runtime messaging
alone is replaced by a test binding that posts authenticated EdgeBridge events.
This does NOT verify extension installation, activeTab grants, or background.js.
Translation uses the explicitly labelled MockTranslator, without login/network
translation. The Qt app uses the offscreen platform for unattended execution.
"""

from __future__ import annotations

import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import imageio_ffmpeg  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from PySide6.QtCore import QObject, QTimer, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from config import AppConfig  # noqa: E402
from translator.openai_translator import MockTranslator  # noqa: E402
from ui.subtitle_overlay import SubtitleOverlay  # noqa: E402
from video.bridge import EdgeBridge  # noqa: E402
from video.pipeline import VideoPipeline  # noqa: E402


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_head(self):
        # Edge requires byte ranges for seeking MP4. Python 3.11's generic
        # SimpleHTTPRequestHandler sends the whole file and offers no ranges.
        path = Path(self.translate_path(self.path))
        if path.suffix != ".mp4" or not path.is_file():
            self._range_remaining = None
            return super().send_head()
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range", "")
        if requested.startswith("bytes="):
            bounds = requested[6:].split("-", 1)
            start = int(bounds[0] or 0)
            end = min(size - 1, int(bounds[1])) if bounds[1] else size - 1
        if not 0 <= start <= end < size:
            self.send_error(416)
            return None
        stream = path.open("rb")
        stream.seek(start)
        self.send_response(206 if requested else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self._range_remaining = end - start + 1
        return stream

    def copyfile(self, source, output):
        if self._range_remaining is None:
            return super().copyfile(source, output)
        while self._range_remaining:
            block = source.read(min(65536, self._range_remaining))
            if not block:
                break
            output.write(block)
            self._range_remaining -= len(block)


class GuiHarness(QObject):
    def __init__(self, config, bridge):
        super().__init__()
        self.config, self.bridge = config, bridge
        self.pipeline = VideoPipeline(self)
        self.overlay = SubtitleOverlay(config.overlay)
        self.commands = queue.Queue()
        self.events = []
        self.errors = []
        self.pipeline.subtitle.connect(self.on_subtitle)
        self.pipeline.error.connect(self.errors.append)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump)
        self.timer.start(10)

    @Slot(object)
    def on_subtitle(self, subtitle):
        if subtitle is None:
            self.overlay.clear()
            self.events.append({"event": "clear", "wall_time": time.monotonic()})
        elif self.pipeline.accepts(subtitle):
            self.overlay.show_timed_subtitle(subtitle)
            self.events.append({"event": "subtitle", "english": subtitle.english,
                                "chinese": subtitle.chinese, "generation": subtitle.generation,
                                "start": subtitle.started_at, "end": subtitle.ended_at})

    @Slot()
    def pump(self):
        while True:
            try:
                function, complete, result = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                result.append((True, function()))
            except Exception as error:
                result.append((False, error))
            finally:
                complete.set()

    def call(self, function):
        complete, result = threading.Event(), []
        self.commands.put((function, complete, result))
        if not complete.wait(8):
            raise TimeoutError("Qt main thread did not process an integration command")
        success, value = result[0]
        if not success:
            raise value
        return value

    def visible(self):
        return {"visible": self.overlay.isVisible(), "english": self.overlay._english,
                "chinese": self.overlay._chinese, "expiry_active": self.overlay._expire.isActive()}


def wait_for(predicate, page, *, timeout=10, label="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        # Pump Playwright's binding delivery while the real Qt loop runs on main.
        page.wait_for_timeout(75)
    raise AssertionError(f"Timed out waiting for {label}")


def create_fixtures(directory):
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x233348:s=640x360:r=10:d=32", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(directory / "video-a.mp4")],
        check=True, capture_output=True, timeout=30,
    )
    shutil.copyfile(directory / "video-a.mp4", directory / "video-b.mp4")
    for suffix, first in (("a", "The first generated video stays readable while paused."),
                          ("b", "A different video source now has its own translated captions.")):
        (directory / f"captions-{suffix}.vtt").write_text(
            f"WEBVTT\n\n00:00.000 --> 00:12.000\n{first}\n\n"
            f"00:15.000 --> 00:22.000\nThis is the later sentence in source {suffix}.\n\n"
            "00:25.000 --> 00:31.000\nThe generated fixture ends after this caption.\n", encoding="utf-8",
        )
    (directory / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Generated Edge subtitle fixture</title>'
        '<style>body{background:#141b27;color:#e8effb;font:18px Segoe UI;padding:30px}'
        'video{width:640px;height:360px}h1{font-size:24px}p{max-width:720px}</style>'
        '<h1>Real Microsoft Edge · generated video integration fixture</h1>'
        '<p>Native playback and English VTT cues. Translation is Mock, not Chinese translation. '
        'Extension messaging uses a test binding; the extension is not installed in this test.</p>'
        '<video id="video" controls muted preload="auto" src="video-a.mp4">'
        '<track kind="subtitles" srclang="en" label="Generated English" src="captions-a.vtt">'
        '</video></html>', encoding="utf-8",
    )


def exercise_edge(harness, report, artifacts):
    bridge = harness.bridge
    session_id = "manual-real-edge-generated-fixture"
    report["event_counts"] = {}

    def post(event):
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        request = Request(f"http://127.0.0.1:{bridge.port}/v1/event", data=body,
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + bridge.token})
        with urlopen(request, timeout=3) as response:
            assert response.status == 200

    def transport(_source, message):
        kind = message.get("type", "")
        counts = report["event_counts"]
        counts[kind] = counts.get(kind, 0) + 1
        mapped = {"probe_state": "state", "probe_captions": "captions", "probe_warning": "warning"}.get(kind)
        if mapped:
            event = dict(message, type=mapped, session_id=session_id)
            post(event)
        return {"selected": True} if kind == "probe_state" else {"ok": True}

    server = None
    try:
        with tempfile.TemporaryDirectory(prefix="subtitle-real-edge-dom-") as temporary:
            directory = Path(temporary)
            create_fixtures(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(directory)))
            threading.Thread(target=server.serve_forever, daemon=True, name="manual-edge-fixtures").start()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    report["browser_version"] = browser.version
                    context = browser.new_context(viewport={"width": 1100, "height": 800})
                    page = context.new_page()
                    page.expose_binding("__subtitleTestTransport", transport)
                    page.add_init_script("""globalThis.chrome = globalThis.chrome || {};
                        chrome.runtime = {sendMessage: message => window.__subtitleTestTransport(message),
                          onMessage: {addListener() {}, removeListener() {}}};""")
                    page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                    page.wait_for_function("document.querySelector('video').readyState >= 2")
                    page.evaluate("document.querySelector('video').currentTime = 2")
                    page.wait_for_function("(() => { const v=document.querySelector('video'); return !v.seeking && Math.abs(v.currentTime-2)<.1; })()")
                    page.add_script_tag(path=str(ROOT / "edge_extension" / "content.js"))
                    first = wait_for(lambda: bridge.snapshot(session_id), page, label="real Edge state at desktop")
                    first_source = first["source_id"]
                    cues = wait_for(lambda: bridge.captions(session_id, first_source), page,
                                    label="native VTT cues from real content.js")
                    assert len(cues) == 3 and cues[1]["start"] == 15
                    assert page.evaluate("document.querySelector('video').textTracks[0].mode") == "hidden"
                    report["checks"].append("Real content.js loaded complete native English VTT cues through authenticated HTTP")
                    harness.config.video_session_id = session_id
                    harness.call(lambda: harness.pipeline.start(harness.config, MockTranslator(), bridge))
                    first_caption = cues[0]["text"]

                    def translated(expected):
                        state = harness.call(harness.visible)
                        return state if state["visible"] and state["english"] == expected and state["chinese"].startswith("[Mock") else None

                    wait_for(lambda: translated(first_caption), page, label="real Qt overlay Mock subtitle")
                    page.wait_for_timeout(2400)
                    paused = translated(first_caption)
                    assert paused and not paused["expiry_active"]
                    harness.call(lambda: harness.overlay.grab().save(str(artifacts / "edge-video-overlay-paused.png")))
                    report["checks"].append("Paused native video retained its overlay subtitle beyond the 2-second system expiry")

                    page.evaluate("document.querySelector('video').currentTime = 16")
                    try:
                        wait_for(lambda: translated(cues[1]["text"]), page, label="forward seek selects later caption")
                    except AssertionError:
                        report["seek_debug"] = {"native": page.evaluate("() => { const v=document.querySelector('video'); return {time:v.currentTime,seeking:v.seeking,ready:v.readyState,duration:v.duration,error:v.error?.message}; }"),
                                                "bridge": bridge.snapshot(session_id),
                                                "overlay": harness.call(harness.visible),
                                                "pipeline": harness.call(lambda: {"generation":harness.pipeline._generation,"clock":harness.pipeline._clock.state,"cues":[c.text for c in harness.pipeline._timeline.cues],"cache":dict(harness.pipeline._cache)})}
                        raise
                    page.evaluate("document.querySelector('video').currentTime = 13")
                    wait_for(lambda: not harness.call(harness.visible)["visible"], page, label="subtitle gap clears overlay")
                    page.evaluate("document.querySelector('video').currentTime = 2")
                    wait_for(lambda: translated(first_caption), page, label="backward seek restores cached first caption")
                    report["checks"].append("Forward/backward native seeks select the right cue; an actual VTT gap clears the overlay")

                    report["rate_samples"] = []
                    for rate in (.5, 2.0):
                        page.evaluate("rate => { const v=document.querySelector('video'); v.currentTime=2; v.playbackRate=rate; }", rate)
                        page.wait_for_function("document.querySelector('video').seeking === false")
                        start = page.evaluate("async () => { const v=document.querySelector('video'); await v.play(); return v.currentTime; }")
                        wall = time.monotonic()
                        page.wait_for_timeout(1000)
                        finish = page.evaluate("() => { const v=document.querySelector('video'); v.pause(); return v.currentTime; }")
                        elapsed = time.monotonic() - wall
                        state = wait_for(lambda: (s if (s := bridge.snapshot(session_id)) and s["paused"]
                                                 and s["playback_rate"] == rate and abs(s["current_time"] - finish) < .15 else None),
                                         page, label=f"{rate}x browser rate reaches desktop")
                        observed = (finish - start) / elapsed
                        assert abs(observed - rate) < max(.25, rate * .3), (rate, observed)
                        assert translated(first_caption)
                        report["rate_samples"].append({"requested_rate": rate, "observed_rate": round(observed, 3),
                                                        "bridge_time": round(state["current_time"], 3)})
                    report["checks"].append("Actual 0.5x and 2x HTMLVideoElement playback clocks reached bridge and kept the correct Qt cue")

                    page.evaluate("""() => { document.querySelector('video').outerHTML =
                      '<video id="video" controls muted preload="auto" src="video-b.mp4">' +
                      '<track kind="subtitles" srclang="en" label="Generated English" src="captions-b.vtt"></video>'; }""")
                    page.wait_for_function("document.querySelector('video').readyState >= 2")
                    page.evaluate("document.querySelector('video').currentTime = 2")
                    changed = wait_for(lambda: (s if (s := bridge.snapshot(session_id)) and s["source_id"] != first_source else None),
                                       page, label="new native video source")
                    new_cues = wait_for(lambda: bridge.captions(session_id, changed["source_id"]), page, label="replacement VTT track")
                    wait_for(lambda: translated(new_cues[0]["text"]), page, label="new source subtitle")
                    assert bridge.captions(session_id, first_source) == []
                    assert harness.call(lambda: first_caption not in [cue.text for cue in harness.pipeline._timeline.cues])
                    report["checks"].append("Replacing the real video element discarded previous source captions/cache and displayed new source text")
                    page.screenshot(path=str(artifacts / "edge-video-dom.png"), full_page=True)

                    page.evaluate("globalThis.__subtitleEdgeProbe.stop()")
                    wait_for(lambda: not harness.call(harness.visible)["visible"], page, timeout=5,
                             label="stale browser clock clears subtitle")
                    report["checks"].append("Stopping native probe updates cleared the Qt subtitle after the real clock stale timeout")
                    page.add_script_tag(path=str(ROOT / "edge_extension" / "content.js"))
                    wait_for(lambda: translated(new_cues[0]["text"]), page, label="probe reconnect restores subtitle")
                    page.evaluate("globalThis.__subtitleEdgeProbe.stop()")
                    post({"type": "disconnect", "session_id": session_id})
                    assert bridge.snapshot(session_id) is None
                    wait_for(lambda: not harness.call(harness.visible)["visible"], page, timeout=5,
                             label="disconnected session clears subtitle")
                    report["checks"].append("Explicit authenticated disconnect removed the session and cleared the overlay")
                    harness.call(harness.pipeline.stop)
                    wait_for(lambda: not harness.call(lambda: harness.pipeline.is_running), page, label="video workers stop")
                    assert not harness.call(harness.visible)["visible"]
                    assert not [t.name for t in threading.enumerate() if t.name.startswith("video-")]
                    report["checks"].append("Stop cleared overlay and joined video workers without residual subtitle threads")
                    assert not harness.errors, harness.errors
                    report["overlay_events"] = harness.call(lambda: list(harness.events))
                    report["passed"] = True
                finally:
                    browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()


def main():
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    report = {"browser": "Microsoft Edge (installed msedge channel)", "profile": "new isolated temporary browser context",
              "extension_transport": "test_binding", "extension_installed": False,
              "translation": "MockTranslator; explicitly labelled; no subscription/API translation exercised",
              "qt_platform": os.environ["QT_QPA_PLATFORM"], "checks": [], "passed": False,
              "scope": "Real generated MP4/VTT, native DOM/media clock, original content.js, authenticated HTTP EdgeBridge, real VideoPipeline and Qt overlay. Does not validate extension install/grants/background worker."}
    app = QApplication([])
    config = AppConfig(source_mode="edge", video_audio_fallback=False)
    config.overlay.display_seconds = 2
    bridge = EdgeBridge(port=0)
    bridge.start()
    harness = GuiHarness(config, bridge)
    finished = threading.Event()

    def worker():
        try:
            exercise_edge(harness, report, artifacts)
        except Exception as error:
            report["error"] = repr(error)
            report["traceback"] = traceback.format_exc()
        finally:
            finished.set()

    thread = threading.Thread(target=worker, daemon=True, name="manual-edge-playwright")
    thread.start()
    deadline = time.monotonic() + 100

    def check_finished():
        if finished.is_set():
            if harness.pipeline.is_running:
                harness.pipeline.stop()
            else:
                app.quit()
        elif time.monotonic() > deadline:
            report["error"] = "Integration timeout after 100 seconds"
            harness.pipeline.stop()
            app.quit()

    monitor = QTimer()
    monitor.timeout.connect(check_finished)
    monitor.start(50)
    try:
        app.exec()
    finally:
        monitor.stop()
        thread.join(timeout=5)
        harness.overlay.clear()
        harness.overlay.close()
        bridge.stop()
        report["remaining_video_threads"] = [t.name for t in threading.enumerate() if t.name.startswith("video-")]
        (artifacts / "edge-video-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
