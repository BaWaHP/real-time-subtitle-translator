"""Real MV3 extension integration smoke, using an isolated Chromium profile.

Run: .venv\Scripts\python.exe tests/manual_extension_smoke.py
Requires Playwright and imageio-ffmpeg; never touches an existing Edge profile.
Set SUBTITLE_TEST_CHROMIUM to an installed Edge executable to test real Edge.
The test uses actual extension scripts and authenticated HTTP EdgeBridge.
"""
from __future__ import annotations

import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import imageio_ffmpeg  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from video.bridge import EdgeBridge  # noqa: E402
from video.timeline import MediaClock  # noqa: E402

PANOPTO_FIXTURE_HOST = "subtitle-smoke.hosted.panopto.com"
PANOPTO_FIXTURE_IDS = {
    "english": "11111111-1111-4111-8111-111111111111",
    "default": "22222222-2222-4222-8222-222222222222",
    "unauthorized": "33333333-3333-4333-8333-333333333333",
}


class InstrumentedBridge(EdgeBridge):
    """Test-only slow caption consumer; normal HTTP/state validation stays real."""

    def __init__(self):
        super().__init__(port=0)
        self.caption_delay_seconds = 0.0
        self.caption_started = threading.Event()
        self.caption_finished = threading.Event()
        self.caption_delay_elapsed = 0.0
        self.clock_posts_during_caption = 0
        self._metrics_lock = threading.Lock()

    def receive(self, event):
        delay = 0.0
        with self._metrics_lock:
            if event.get("type") == "captions" and self.caption_delay_seconds:
                delay = self.caption_delay_seconds
                self.caption_delay_seconds = 0.0
            elif (event.get("type") == "state" and self.caption_started.is_set()
                  and not self.caption_finished.is_set()):
                self.clock_posts_during_caption += 1
        if delay:
            started = time.perf_counter()
            self.caption_started.set()
            # Only a single caption handler sleeps. The genuine bridge uses
            # ThreadingHTTPServer, so independent clock POSTs can still arrive.
            time.sleep(delay)
            self.caption_delay_elapsed = time.perf_counter() - started
            self.caption_finished.set()
        return super().receive(event)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_head(self):
        # SimpleHTTPRequestHandler in Python 3.11 has no byte-range support.
        # A browser must receive ranges to reliably seek generated media.
        self._range_remaining = None
        parsed = urlsplit(self.path)
        if parsed.path == "/Panopto/Pages/Viewer.aspx":
            delivery_id = parse_qs(parsed.query).get("id", [""])[0]
            scenario = next((key for key, value in PANOPTO_FIXTURE_IDS.items() if value == delivery_id), None)
            if not scenario:
                self.send_error(404)
                return None
            language = "{}" if scenario == "default" else '{language:0,label:"English"}'
            payload = (
                '<!doctype html><meta charset="utf-8"><title>Synthetic Panopto-shaped subtitle fixture</title>'
                '<style>body{background:#141b27;color:#e8effb;font:18px Segoe UI;padding:30px}'
                'video{width:640px;height:360px}</style>'
                '<h1>Generated local test — no real Panopto account or course</h1>'
                '<video id="video" controls muted preload="auto" src="/sample.webm"></video>'
                f'<script>globalThis.PanoptoViewer={{Language:{{getCaptionLanguages:()=>[{language}],'
                f'getCurrentCaptionLanguage:()=>({language})}}}};</script>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            # This synthetic HttpOnly cookie can only be sent by the browser;
            # production extension code never reads or exports its value.
            cookie = "subtitle_smoke_session=fixture-only; HttpOnly; SameSite=Strict; Path=/Panopto/"
            if scenario == "unauthorized":
                cookie += "; Max-Age=0"
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            return io.BytesIO(payload)
        path = Path(self.translate_path(self.path))
        if path.suffix not in {".mp4", ".webm"} or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        requested = self.headers.get("Range", "")
        start, end = 0, size - 1
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                self.send_error(416)
                return None
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(size - 1, int(right)) if right else size - 1
            else:
                start = max(0, size - int(right))
        if not 0 <= start <= end < size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        stream = path.open("rb")
        stream.seek(start)
        self.send_response(206 if requested else 200)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self._range_remaining = end - start + 1
        return stream

    def do_POST(self):
        if urlsplit(self.path).path != "/Panopto/Pages/Viewer/DeliveryInfo.aspx":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length < 8192:
            self.send_error(400)
            return
        fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        delivery_id = fields.get("deliveryId", [""])[0]
        scenario = next((key for key, value in PANOPTO_FIXTURE_IDS.items() if value == delivery_id), None)
        is_captions = fields.get("getCaptions") == ["true"]
        language = fields.get("language", [None])[0]
        expected_origin = f"http://{PANOPTO_FIXTURE_HOST}:{self.server.server_port}"
        cookie_present = "subtitle_smoke_session=fixture-only" in self.headers.get("Cookie", "")
        request_origin = self.headers.get("Origin", "")
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        # This fixture deliberately serves HTTP on a loopback-mapped public
        # hostname. Chromium may omit Fetch Metadata outside secure contexts;
        # the POST Origin must still match the authorized page exactly.
        same_origin = request_origin == expected_origin and fetch_site in {"", "same-origin"}
        form_ok = (scenario is not None and fields.get("responseType") == ["json"]
                   and self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"))
        authorized = cookie_present and same_origin and scenario != "unauthorized"
        self.server.panopto_requests.append({
            "scenario": scenario, "captions": is_captions, "language": language,
            "synthetic_http_only_cookie_present": cookie_present,
            "browser_same_origin_headers": same_origin, "form_fields_valid": form_ok,
            "request_origin": request_origin, "sec_fetch_site": fetch_site,
            "status": 200 if form_ok and authorized else 401 if form_ok else 400,
        })
        status = 200 if form_ok and authorized else 401 if form_ok else 400
        if status != 200:
            result = {"ErrorCode": 2, "Message": "Synthetic test session is not authorized"}
        elif is_captions:
            expected_language = "" if scenario == "default" else "0"
            if language != expected_language:
                status, result = 400, {"ErrorCode": 1}
            else:
                result = [
                    {"Time": 0, "CaptionDuration": 3, "Data": "Panopto first English sentence."},
                    {"Time": 3, "CaptionDuration": 3, "Caption": "Second custom-player sentence."},
                    {"Time": 8, "CaptionDuration": 3, "Data": "Later subtitle after seeking."},
                ]
        else:
            result = {"Delivery": {"AvailableCaptions": [{"Language": None if scenario == "default" else 0}], "Duration": 16}}
        payload = json.dumps(result).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def copyfile(self, source, output):
        if self._range_remaining is None:
            return super().copyfile(source, output)
        while self._range_remaining:
            block = source.read(min(65536, self._range_remaining))
            if not block:
                break
            output.write(block)
            self._range_remaining -= len(block)


def wait_for(predicate, page, *, timeout=12, interval=.1, label="condition"):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = predicate()
        if result:
            return result
        page.wait_for_timeout(interval * 1000)
    raise AssertionError(f"Timed out waiting for {label}")


def launch_context(playwright, profile, extension):
    return playwright.chromium.launch_persistent_context(
        str(profile), channel="chromium", headless=True,
        executable_path=os.environ.get("SUBTITLE_TEST_CHROMIUM") or None,
        chromium_sandbox=True,
        ignore_default_args=["--disable-extensions", "--unsafely-disable-devtools-self-xss-warnings", "--enable-unsafe-swiftshader"],
        args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}",
              f"--host-resolver-rules=MAP outer.test 127.0.0.1, MAP player.test 127.0.0.1, MAP replacement.test 127.0.0.1, MAP {PANOPTO_FIXTURE_HOST} 127.0.0.1",
              "--no-proxy-server"],
        viewport={"width": 1100, "height": 800},
    )


def extension_pages(context, page_url):
    worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
    extension_id = worker.url.split("/")[2]
    video = context.new_page()
    video.goto(page_url)
    popup = context.new_page()
    popup.goto(f"chrome-extension://{extension_id}/popup.html")
    tab_id = worker.evaluate("""async (url) => (await chrome.tabs.query({})).find(tab => tab.url === url)?.id""", page_url)
    assert isinstance(tab_id, int), "Fixture tab not visible to extension"
    return worker, extension_id, video, popup, tab_id


def connect(popup, tab_id, bridge):
    result = popup.evaluate("""async (payload) => await chrome.runtime.sendMessage(payload)""", {
        "type": "popup_connect", "tabId": tab_id, "port": bridge.port, "token": bridge.token,
    })
    assert result.get("ok"), result


def popup_status(popup):
    return popup.evaluate("""async () => await chrome.runtime.sendMessage({type:'popup_status'})""")


def stop(popup, video, bridge):
    result = popup.evaluate("""async () => await chrome.runtime.sendMessage({type:'popup_stop'})""")
    assert result.get("ok"), result
    wait_for(lambda: not bridge.sessions(), video, label="disconnect cleared desktop session")


def measure_playback_clock(video, bridge, session_id, *, samples=16):
    """Compare the production clock prediction with native HTMLMediaElement time."""
    observations = []
    clock = MediaClock()
    for index in range(samples):
        # Periodically keep the actual renderer busy for 65 ms. This exercises
        # delayed probe sampling without introducing a false browser timestamp.
        if index % 4 == 0:
            video.evaluate("const until = performance.now() + 65; while(performance.now() < until) {}")
        state = bridge.snapshot(session_id)
        assert state and state.get("sampled_at_ms"), "Real content probe did not supply its sampling time"
        before = time.monotonic()
        read_started = time.perf_counter()
        actual = video.evaluate("""() => { const v=document.querySelector('video'); return {
            current_time:v.currentTime, paused:v.paused, seeking:v.seeking,
            buffering:v.readyState < 3, playback_rate:v.playbackRate}; }""")
        after = time.monotonic()
        read_elapsed = time.perf_counter() - read_started
        clock.update(state)
        prediction = clock.position((before + after) / 2)
        if not (actual["paused"] or actual["seeking"] or actual["buffering"]):
            assert prediction is not None, "Desktop media clock became stale during actual browser playback"
            observations.append({
                "browser_time": round(actual["current_time"], 5),
                "desktop_prediction": round(prediction, 5),
                "absolute_error_ms": round(abs(prediction - actual["current_time"]) * 1000, 3),
                "browser_read_roundtrip_ms": round(read_elapsed * 1000, 3),
            })
        video.wait_for_timeout(70)
    assert len(observations) >= 10, observations
    maximum = max(item["absolute_error_ms"] for item in observations)
    assert maximum < 500, f"Desktop clock diverged from actual video by {maximum:.1f} ms"
    return {"samples": observations, "max_absolute_error_ms": maximum,
            "production_monotonic_resolution_ms": time.get_clock_info("monotonic").resolution * 1000,
            "renderer_load": "65 ms synchronous JavaScript busy loop before every fourth sample",
            "prediction": "Production MediaClock using authenticated bridge state and server monotonic sample anchor"}


def media_event_latency(video, bridge, session_id, action, predicate, *, label):
    started = time.perf_counter()
    video.evaluate(action)
    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and predicate(state) else None),
             video, timeout=2, interval=.01, label=label)
    elapsed = time.perf_counter() - started
    actual = video.evaluate("document.querySelector('video').currentTime")
    state = bridge.snapshot(session_id)
    clock = MediaClock()
    clock.update(state)
    predicted = clock.position()
    error = abs(predicted - actual) if predicted is not None else float("inf")
    assert elapsed < .5, f"{label} took {elapsed:.3f} seconds to reach desktop"
    assert error < .15, f"{label} clock differs from native video by {error:.3f} seconds"
    return {"event_to_matching_desktop_state_ms": round(elapsed * 1000, 3),
            "native_video_time": round(actual, 5), "desktop_predicted_time": round(predicted, 5),
            "absolute_error_ms": round(error * 1000, 3)}


def run_panopto_fixture(video, popup, tab_id, bridge, server, report, artifacts):
    """Exercise the real MAIN-world adapter against a local provider-shaped server."""
    report["panopto_fixture"] = {
        "scope": "Synthetic local server and generated video only. Official-shape Language API and DeliveryInfo responses are fixtures, not a real Panopto session or course verification.",
        "session": "Synthetic HttpOnly SameSite=Strict cookie; server requires same-origin browser request headers. Cookie value is never returned by page evaluation or written to this report.",
        "requests": server.panopto_requests,
    }
    origin = f"http://{PANOPTO_FIXTURE_HOST}:{server.server_port}"

    def open_scenario(scenario):
        page_url = f"{origin}/Panopto/Pages/Viewer.aspx?id={PANOPTO_FIXTURE_IDS[scenario]}"
        video.goto(page_url)
        video.wait_for_function("document.querySelector('video').readyState >= 2")
        assert video.evaluate("document.querySelector('video').textTracks.length") == 0
        assert video.evaluate("!document.cookie.includes('subtitle_smoke_session')"), "Synthetic session cookie must stay HttpOnly"
        connect(popup, tab_id, bridge)
        return wait_for(lambda: next((state for state in bridge.sessions() if state["page_url"] == page_url), None),
                        video, label=f"{scenario} synthetic Panopto video state")

    session = open_scenario("english")
    cues = wait_for(lambda: bridge.captions(session["session_id"], session["source_id"]), video,
                    label="custom Panopto cues with no native text tracks")
    assert len(cues) == 3 and cues[0]["text"] == "Panopto first English sentence." and cues[2]["start"] == 8, cues
    requests = [item for item in server.panopto_requests if item["scenario"] == "english"]
    assert requests and all(item["captions"] and item["language"] == "0" for item in requests), requests
    assert all(item["synthetic_http_only_cookie_present"] and item["browser_same_origin_headers"] and item["form_fields_valid"] for item in requests), requests
    report["checks"].append("Panopto-shaped player with zero native TextTracks retrieves three custom captions via real MAIN same-origin form POST and browser-managed HttpOnly session")
    report["panopto_fixture"]["english_cues"] = cues
    video.evaluate("const v=document.querySelector('video');v.currentTime=8.5;v.playbackRate=1.5")
    wait_for(lambda: (state if (state := bridge.snapshot(session["session_id"])) and abs(state["current_time"] - 8.5) < .1 and state["playback_rate"] == 1.5 else None),
             video, label="custom player seek and speed")
    video.evaluate("document.querySelector('video').play()")
    wait_for(lambda: (state if (state := bridge.snapshot(session["session_id"])) and not state["paused"] else None), video, label="custom player play")
    video.evaluate("document.querySelector('video').pause()")
    paused = wait_for(lambda: (state if (state := bridge.snapshot(session["session_id"])) and state["paused"] else None), video, label="custom player pause")
    video.wait_for_timeout(400)
    assert abs(bridge.snapshot(session["session_id"])["current_time"] - paused["current_time"]) < .08
    report["checks"].append("Custom-caption player keeps normal seek, speed, play and pause synchronization")
    stop(popup, video, bridge)

    session = open_scenario("default")
    unconfirmed = wait_for(lambda: (state if "Default" in (state := popup_status(popup)).get("warning", "") else None),
                           video, label="unlabelled default-language confirmation warning")
    assert not bridge.captions(session["session_id"], session["source_id"])
    requests = [item for item in server.panopto_requests if item["scenario"] == "default"]
    assert requests and not any(item["captions"] for item in requests), requests
    assert not unconfirmed["panoptoDefaultEnglish"]
    report["checks"].append("Unlabelled Panopto Default track is not requested or uploaded until explicit per-source English confirmation")
    result = popup.evaluate("""async () => await chrome.runtime.sendMessage({type:'popup_panopto_language',enabled:true})""")
    assert result.get("ok"), result
    cues = wait_for(lambda: bridge.captions(session["session_id"], session["source_id"]), video,
                    label="confirmed default English custom captions")
    assert len(cues) == 3 and cues[1]["text"] == "Second custom-player sentence."
    requests = [item for item in server.panopto_requests if item["scenario"] == "default" and item["captions"]]
    assert requests and all(item["language"] == "" and item["synthetic_http_only_cookie_present"] and item["browser_same_origin_headers"] for item in requests), requests
    assert popup_status(popup)["panoptoDefaultEnglish"]
    report["checks"].append("Explicit English confirmation reads the Default caption track with an empty language form field")
    popup.screenshot(path=str(artifacts / "extension_panopto_fixture.png"), full_page=True)
    result = popup.evaluate("""async () => await chrome.runtime.sendMessage({type:'popup_panopto_language',enabled:false})""")
    assert result.get("ok"), result
    wait_for(lambda: not bridge.sessions(), video, label="English confirmation revocation disconnects source")
    report["checks"].append("Revoking Default-track English confirmation disconnects and clears its desktop session")

    session = open_scenario("unauthorized")
    unauthorized = wait_for(lambda: (state if "登录权限" in (state := popup_status(popup)).get("warning", "") else None),
                            video, label="synthetic expired-session authentication warning")
    assert not bridge.captions(session["session_id"], session["source_id"])
    requests = [item for item in server.panopto_requests if item["scenario"] == "unauthorized"]
    assert requests and all(item["status"] == 401 and not item["synthetic_http_only_cookie_present"] for item in requests), requests
    report["panopto_fixture"]["unauthorized_warning"] = unauthorized["warning"]
    report["checks"].append("Missing synthetic Panopto session produces HTTP 401 and explicit login warning, without treating the request as successful captions")
    stop(popup, video, bridge)


def main():
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    report = {
        "browser": os.environ.get("SUBTITLE_TEST_CHROMIUM") or "Playwright bundled Chromium",
        "profile": "new isolated temporary profile",
        "extension": "native MV3 service worker, real Chrome runtime/permissions/scripting APIs; no messaging mocks",
        "translator": "not involved; this verifies extension + real HTTP bridge",
        "permission_confirmation": "Headless mode cannot confirm Chrome's native optional-host permission prompt. Ungranted discovery uses the production manifest; granted nested playback uses a temporary extension copy with only generated fixture origins added as required test permissions.",
        "clock_limits": "Generated local muted 5 fps video, local HTTP bridge, deliberate renderer stalls and a single delayed caption POST; no real course, network translation or visible Qt subtitle rendering is measured by this script.",
        "checks": [],
    }
    bridge = InstrumentedBridge()
    bridge.start()
    server = None
    try:
        with tempfile.TemporaryDirectory(prefix="subtitle-extension-smoke-") as temporary:
            workspace = Path(temporary)
            subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=0x233348:s=640x360:r=5:d=16",
                 "-c:v", "libvpx-vp9", "-deadline", "realtime", "-an", str(workspace / "sample.webm")],
                check=True, timeout=45,
            )
            (workspace / "captions.vtt").write_text(
                "WEBVTT\n\n00:00.000 --> 00:03.000\nFirst English sentence.\n\n"
                "00:03.000 --> 00:06.000\nSecond sentence to translate ahead.\n\n"
                "00:08.000 --> 00:11.000\nA later sentence after seeking.\n",
                encoding="utf-8",
            )
            (workspace / "index.html").write_text(
                '<!doctype html><html lang="en"><meta charset="utf-8"><title>Extension smoke video</title>'
                '<style>body{background:#141b27;color:#e8effb;font:18px Segoe UI;padding:30px}'
                'video{width:640px;height:360px}h1{font-size:24px}</style>'
                '<h1>Isolated extension integration test</h1><video id="video" controls muted '
                'preload="auto" src="sample.webm"><track kind="subtitles" srclang="en" '
                'label="English manual" src="captions.vtt"></video></html>', encoding="utf-8",
            )
            handler = functools.partial(QuietHandler, directory=str(workspace))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.panopto_requests = []
            threading.Thread(target=server.serve_forever, daemon=True).start()
            page_url = f"http://127.0.0.1:{server.server_port}/index.html"
            nested_url = f"http://127.0.0.1:{server.server_port}/nested.html"
            outer_url = f"http://outer.test:{server.server_port}/outer.html"
            player_url = f"http://player.test:{server.server_port}/index.html"
            replacement_url = f"http://replacement.test:{server.server_port}/index.html"
            (workspace / "nested.html").write_text(
                '<!doctype html><title>Nested course fixture</title><style>body{margin:0}iframe{width:950px;height:650px;border:0}</style>'
                f'<iframe id="outer" src="{outer_url}"></iframe>', encoding="utf-8",
            )
            (workspace / "outer.html").write_text(
                '<!doctype html><title>Embedded player wrapper</title><style>body{margin:0}iframe{width:900px;height:620px;border:0}</style>'
                f'<iframe id="player" src="{player_url}"></iframe>', encoding="utf-8",
            )
            extension = ROOT / "edge_extension"
            with sync_playwright() as playwright:
                context = launch_context(playwright, workspace / "production-profile", extension)
                try:
                    worker, extension_id, video, popup, tab_id = extension_pages(context, page_url)
                    report["browser_version"] = context.browser.version
                    report["extension_id"] = extension_id
                    video.wait_for_function("document.querySelector('video').readyState >= 2")
                    # Exercise the actual popup entrypoint with a real sender;
                    # no Chrome APIs or page probes are mocked by this test.
                    bridge.caption_delay_seconds = 1.4
                    connect(popup, tab_id, bridge)
                    session = wait_for(lambda: next(iter(bridge.sessions()), None), video, label="real playback state")
                    session_id, first_source = session["session_id"], session["source_id"]
                    wait_for(bridge.caption_started.is_set, video, label="deliberately delayed caption HTTP handler")
                    video.evaluate("document.querySelector('video').play()")
                    wait_for(lambda: video.evaluate("document.querySelector('video').currentTime > .15"), video, label="actual native video playback")
                    report["media_clock"] = measure_playback_clock(video, bridge, session_id)
                    wait_for(bridge.caption_finished.is_set, video, label="delayed caption handler completion")
                    assert bridge.clock_posts_during_caption >= 3, bridge.clock_posts_during_caption
                    report["slow_caption_transport"] = {
                        "actual_caption_handler_delay_ms": round(bridge.caption_delay_elapsed * 1000, 3),
                        "independent_clock_posts_during_delay": bridge.clock_posts_during_caption,
                    }
                    report["checks"].append("Real clock POSTs continue while the first caption HTTP handler is delayed 1.4 seconds")
                    report["checks"].append("Production MediaClock predictions match native playing video time within 500 ms during renderer load")
                    report["event_latency"] = {}
                    report["event_latency"]["pause"] = media_event_latency(
                        video, bridge, session_id, "document.querySelector('video').pause()",
                        lambda state: state["paused"], label="immediate media pause event")
                    report["event_latency"]["seek"] = media_event_latency(
                        video, bridge, session_id, "document.querySelector('video').currentTime=6.5",
                        lambda state: not state["seeking"] and abs(state["current_time"] - 6.5) < .05,
                        label="immediate media seek event")
                    report["checks"].append("Native pause and seek events reach matching desktop media time in under 500 ms")
                    cues = wait_for(lambda: bridge.captions(session_id, first_source), video, label="full native VTT cue document")
                    assert len(cues) == 3 and cues[2]["start"] == 8, cues
                    assert cues[0]["text"] == "First English sentence."
                    report["checks"].append("Authenticated real extension -> real desktop bridge state + complete English cues")
                    original_mode = video.evaluate("document.querySelector('video').textTracks[0].mode")
                    assert original_mode == "hidden", original_mode
                    report["checks"].append("Disabled native text track loads in hidden mode")
                    video.evaluate("document.querySelector('video').play()")
                    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and not state["paused"] and state["current_time"] > .2 else None), video, label="playing state")
                    video.evaluate("document.querySelector('video').pause()")
                    paused = wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and state["paused"] else None), video, label="pause")
                    video.wait_for_timeout(600)
                    assert abs(bridge.snapshot(session_id)["current_time"] - paused["current_time"]) < .08
                    report["checks"].append("Pause freezes actual browser playback time")
                    video.evaluate("document.querySelector('video').currentTime = 8.5")
                    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and abs(state["current_time"] - 8.5) < .1 else None), video, label="seek")
                    report["checks"].append("Seek forwards reports new media timestamp")
                    video.evaluate("document.querySelector('video').currentTime = 1.5")
                    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and abs(state["current_time"] - 1.5) < .1 else None), video, label="seek backwards")
                    report["checks"].append("Seek backwards reports new media timestamp")
                    video.evaluate("document.querySelector('video').playbackRate = 1.5")
                    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and state["playback_rate"] == 1.5 else None), video, label="playback rate")
                    report["checks"].append("1.5x playback rate reaches desktop")
                    video.evaluate("const v = document.querySelector('video'); v.src = 'sample.webm?second=1'; v.load();")
                    replaced = wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and state["source_id"] != first_source else None), video, label="new video source isolation")
                    assert replaced["session_id"] == session_id
                    assert bridge.captions(session_id, first_source) == []
                    report["checks"].append("New source keeps connection ID while old source captions are discarded")
                    # Optional authorization must preserve the connection.
                    # The same browser document need not be reinjected.
                    reinjected = popup.evaluate("""async () => await chrome.runtime.sendMessage({type:'popup_permissions_granted'})""")
                    assert reinjected.get("ok"), reinjected
                    video.evaluate("document.querySelector('video').currentTime = 4.5")
                    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and abs(state["current_time"] - 4.5) < .1 else None), video, label="playback continues after optional permission update")
                    report["checks"].append("Permission-triggered reinjection preserves stable tab/frame connection")
                    popup.screenshot(path=str(artifacts / "extension_browser_smoke.png"), full_page=True)
                    stop(popup, video, bridge)
                    assert video.evaluate("document.querySelector('video').textTracks[0].mode") == "disabled"
                    report["checks"].append("Stop clears bridge session and restores native text track mode")
                    video.goto(nested_url)
                    wait_for(lambda: len(video.frames) == 3, video, label="nested fixture frames load")
                    connect(popup, tab_id, bridge)
                    expected = {"http://outer.test/*", "http://player.test/*"}
                    state = wait_for(lambda: (state if expected.issubset((state := popup_status(popup))["pendingOrigins"]) else None), video, label="both nested ungranted origins listed")
                    assert not bridge.sessions(), "Unauthorized embedded player should not send media to desktop"
                    assert not worker.evaluate("""async () => await chrome.permissions.contains({origins:['http://player.test/*']})""")
                    report["checks"].append("Production manifest lists both direct and second-level cross-origin iframe permissions without granting or reading either player")
                    report["ungranted_nested_status"] = {key: state[key] for key in ("status", "warning", "pendingOrigins")}
                    stop(popup, video, bridge)
                finally:
                    context.close()
                # The only test configuration change is a temporary manifest
                # adding local fixture aliases. Runtime code is copied
                # byte-for-byte. No existing browser profile is used.
                test_extension = workspace / "test-extension"
                shutil.copytree(extension, test_extension)
                manifest_path = test_extension / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                fixture_origins = ["http://outer.test/*", "http://player.test/*", "http://replacement.test/*", f"http://{PANOPTO_FIXTURE_HOST}/*"]
                manifest["host_permissions"] += fixture_origins
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                report["nested_fixture_pregranted_origins"] = fixture_origins
                context = launch_context(playwright, workspace / "pregranted-profile", test_extension)
                try:
                    worker, _extension_id, video, popup, tab_id = extension_pages(context, nested_url)
                    wait_for(lambda: len(video.frames) == 3, video, label="pregranted nested fixture frames load")
                    player = next(frame for frame in video.frames if frame.url == player_url)
                    player.wait_for_function("document.querySelector('video').readyState >= 2")
                    connect(popup, tab_id, bridge)
                    session = wait_for(lambda: next((s for s in bridge.sessions() if s["page_url"] == player_url), None), video, label="nested iframe video reaches desktop")
                    session_id = session["session_id"]
                    assert session_id != f"{tab_id}:0", "The detected video must be inside a child frame"
                    cues = wait_for(lambda: bridge.captions(session_id, session["source_id"]), video, label="nested native text track cues")
                    assert len(cues) == 3 and cues[0]["text"] == "First English sentence."
                    report["checks"].append("Pregranted nested cross-origin iframe sends actual video state and full native English cues through native MV3 APIs")
                    player.evaluate("const v=document.querySelector('video');v.currentTime=8.5;v.playbackRate=1.5")
                    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and abs(state["current_time"] - 8.5) < .1 and state["playback_rate"] == 1.5 else None), video, label="nested seek and playback rate")
                    player.evaluate("document.querySelector('video').play()")
                    wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and not state["paused"] else None), video, label="nested playing state")
                    player.evaluate("document.querySelector('video').pause()")
                    paused = wait_for(lambda: (state if (state := bridge.snapshot(session_id)) and state["paused"] else None), video, label="nested pause")
                    video.wait_for_timeout(600)
                    assert abs(bridge.snapshot(session_id)["current_time"] - paused["current_time"]) < .08
                    report["checks"].append("Nested player forwards seek, 1.5x speed, play and stable paused timestamp")
                    # Keep the course tab connected while the inner frame
                    # navigates to a different already-authorized origin.
                    outer = next(frame for frame in video.frames if frame.url == outer_url)
                    outer.evaluate("url => document.querySelector('iframe').src = url", replacement_url)
                    replacement = wait_for(lambda: next((s for s in bridge.sessions() if s["page_url"] == replacement_url), None), video, label="new nested frame document is detected without reconnecting")
                    assert replacement["source_id"] != session["source_id"]
                    cues = wait_for(lambda: bridge.captions(replacement["session_id"], replacement["source_id"]), video, label="replacement nested English cues")
                    assert len(cues) == 3
                    report["checks"].append("Already-connected nested frame navigation to another pregranted origin is detected and captions refreshed without reconnecting")
                    video.screenshot(path=str(artifacts / "extension_nested_fixture.png"), full_page=True)
                    stop(popup, video, bridge)
                    report["checks"].append("Nested Stop disconnects real desktop bridge")
                    run_panopto_fixture(video, popup, tab_id, bridge, server, report, artifacts)
                    report["passed"] = True
                finally:
                    context.close()
    except Exception as error:
        report["passed"] = False
        report["error"] = repr(error)
        raise
    finally:
        if server:
            server.shutdown()
            server.server_close()
        bridge.stop()
        destination = artifacts / "extension_browser_smoke.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
