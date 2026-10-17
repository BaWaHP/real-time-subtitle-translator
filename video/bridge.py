"""Loopback bridge for paired Edge extensions.

Caption and playback messages use a random pairing secret, separate from OAuth.
"""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

MAX_BODY = 4 * 1024 * 1024
EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")


class BridgeError(ValueError):
    pass


def _string(value, name: str, limit=1024, required=False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value):
        raise BridgeError(f"Invalid {name}")
    return value


def _number(value, name: str, low=0, high=1e10) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError(f"Invalid {name}")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise BridgeError(f"Invalid {name}")
    return result


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3.0)
        return connection, address


class EdgeBridge:
    def __init__(self, port: int = 18765, *, token: str | None = None):
        self.requested_port = port
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._states: dict[str, dict] = {}
        self._captions: dict[tuple[str, str], list[dict]] = {}
        self._warnings: dict[str, str] = {}
        self._caption_revisions: dict[tuple[str, str], int] = {}
        self._server = None
        self._thread = None

    @property
    def pairing_code(self) -> str:
        return json.dumps({"port": self.port, "token": self.token}, separators=(",", ":"))

    def start(self) -> int:
        if self._server:
            return self.port
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                pass

            def _origin(self):
                origin = self.headers.get("Origin", "")
                return origin if EXTENSION_ORIGIN.fullmatch(origin) else ""

            def _host_ok(self):
                return self.headers.get("Host") == f"127.0.0.1:{bridge.port}"

            def reply(self, status, body):
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                if self._origin():
                    self.send_header("Access-Control-Allow-Origin", self._origin())
                    self.send_header("Vary", "Origin")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self):
                origin = self.headers.get("Origin", "")
                if not self._host_ok() or (origin and not self._origin()):
                    self.reply(403, {"error": "Only the paired browser extension may connect"})
                    return False
                header = self.headers.get("Authorization", "")
                if not secrets.compare_digest(header, "Bearer " + bridge.token):
                    self.reply(401, {"error": "Pair the extension with this desktop app"})
                    return False
                return True

            def do_OPTIONS(self):
                if not self._host_ok() or not self._origin():
                    self.reply(403, {"error": "Origin denied"})
                    return
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", self._origin())
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", "300")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path == "/v1/health":
                    self.reply(200, {"ok": True, "version": 1})
                else:
                    self.reply(404, {"error": "Not found"})

            def do_POST(self):
                if not self.authorized():
                    return
                if self.path != "/v1/event":
                    self.reply(404, {"error": "Not found"})
                    return
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self.reply(415, {"error": "JSON required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        self.reply(413, {"error": "Message too large or empty"})
                        return
                    data = self.rfile.read(length)
                    if len(data) != length:
                        raise BridgeError("Incomplete message")
                    bridge.receive(json.loads(data))
                    self.reply(200, {"ok": True})
                except (ValueError, UnicodeError, TypeError, socket.timeout) as exc:
                    self.reply(400, {"error": str(exc)[:200]})

        try:
            self._server = _Server(("127.0.0.1", self.requested_port), Handler)
        except OSError:
            if self.requested_port == 0:
                raise
            self._server = _Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=lambda: self._server.serve_forever(poll_interval=.1),
                                        daemon=True, name="edge-video-bridge")
        self._thread.start()
        return self.port

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=2)
            self._server = None

    def receive(self, event: dict) -> None:
        if not isinstance(event, dict):
            raise BridgeError("Event must be an object")
        kind = event.get("type")
        session = _string(event.get("session_id"), "session_id", 256, True)
        with self._lock:
            self._prune()
            if kind == "disconnect":
                self._states.pop(session, None)
                self._warnings.pop(session, None)
                self._captions = {k: v for k, v in self._captions.items() if k[0] != session}
                self._caption_revisions = {k: v for k, v in self._caption_revisions.items() if k[0] != session}
                return
            if kind == "state":
                arrived_at = time.monotonic()
                sampled_at_ms = event.get("sampled_at_ms")
                sample_age = 0.0
                if sampled_at_ms is not None:
                    sampled_at_ms = _number(sampled_at_ms, "sampled_at_ms", 0, 1e14)
                    # Measure transport age with the shared PC wall clock, then
                    # anchor playback to the bridge's monotonic clock.
                    wall_now_ms = time.time() * 1000
                    sample_age = (wall_now_ms - sampled_at_ms) / 1000
                    if not -.25 <= sample_age <= 2.0:
                        raise BridgeError("Playback sample is expired or in the future")
                    sample_age = max(0.0, sample_age)
                    old = self._states.get(session)
                    old_sample = (old or {}).get("sampled_at_ms")
                    # Discard out-of-order samples while the previous timestamp is fresh.
                    # The age check also permits recovery after a wall-clock change.
                    if (old_sample is not None and old_sample > sampled_at_ms
                            and -.25 <= (wall_now_ms - old_sample) / 1000 <= 2.0):
                        return
                source = _string(event.get("source_id"), "source_id", 2048, True)
                page_url = _string(event.get("page_url", ""), "page_url", 16384)
                media_url = _string(event.get("media_url", ""), "media_url", 32768)
                if page_url and urlsplit(page_url).scheme not in {"http", "https", "file"}:
                    raise BridgeError("Unsupported page URL")
                if media_url and urlsplit(media_url).scheme not in {"http", "https", "blob", "file"}:
                    raise BridgeError("Unsupported media URL")
                state = {"session_id": session, "source_id": source,
                         "title": _string(event.get("title", "视频"), "title", 1024),
                         "page_url": page_url, "media_url": media_url,
                         "current_time": _number(event.get("current_time", 0), "current_time"),
                         "duration": _number(event.get("duration", 0) or 0, "duration"),
                         "playback_rate": _number(event.get("playback_rate", 1), "playback_rate", .05, 16),
                         "received_at": arrived_at - sample_age,
                         "sampled_at_ms": sampled_at_ms}
                for key, default in (("paused", True), ("seeking", False), ("ended", False), ("buffering", False)):
                    if not isinstance(event.get(key, default), bool):
                        raise BridgeError(f"Invalid {key}")
                    state[key] = event.get(key, default)
                old = self._states.get(session)
                if old and old["source_id"] != source:
                    self._captions = {k: v for k, v in self._captions.items() if k[0] != session}
                    self._caption_revisions = {k: v for k, v in self._caption_revisions.items() if k[0] != session}
                    self._warnings.pop(session, None)
                if len(self._states) >= 16 and session not in self._states:
                    raise BridgeError("Too many video sessions")
                self._states[session] = state
            elif kind == "captions":
                source = _string(event.get("source_id"), "source_id", 2048, True)
                state = self._states.get(session)
                if not state or state["source_id"] != source:
                    raise BridgeError("Captions belong to an inactive video")
                language = _string(event.get("language", "en"), "language", 32)
                if not language.lower().startswith("en"):
                    raise BridgeError("Only English source captions are supported")
                raw = event.get("cues")
                if not isinstance(raw, list) or len(raw) > 20_000:
                    raise BridgeError("Invalid captions")
                cues = []
                for cue in raw:
                    if not isinstance(cue, dict):
                        raise BridgeError("Invalid cue")
                    start = _number(cue.get("start"), "start")
                    end = _number(cue.get("end"), "end")
                    text = _string(cue.get("text"), "text", 8000).strip()
                    if end <= start:
                        raise BridgeError("Caption ends before it starts")
                    if text:
                        cues.append({"start": start, "end": end, "text": text})
                cues.sort(key=lambda c: (c["start"], c["end"]))
                key = (session, source)
                if self._captions.get(key) != cues:
                    self._captions[key] = cues
                    self._caption_revisions[key] = self._caption_revisions.get(key, 0) + 1
            elif kind == "warning":
                self._warnings[session] = _string(event.get("message", ""), "message", 1500)
            else:
                raise BridgeError("Unknown event type")

    def _prune(self):
        stale = [key for key, state in self._states.items() if time.monotonic() - state["received_at"] > 30]
        for key in stale:
            self._states.pop(key, None)
            self._warnings.pop(key, None)
            self._captions = {k: v for k, v in self._captions.items() if k[0] != key}
            self._caption_revisions = {k: v for k, v in self._caption_revisions.items() if k[0] != key}

    def sessions(self) -> list[dict]:
        with self._lock:
            self._prune()
            return [copy.deepcopy(value) for value in self._states.values()
                    if time.monotonic() - value["received_at"] < 3]

    def snapshot(self, session_id: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._states.get(session_id))

    def captions(self, session_id: str, source_id: str) -> list[dict]:
        with self._lock:
            # Return a copy so callers cannot mutate shared cues.
            return copy.deepcopy(self._captions.get((session_id, source_id), []))

    def warning(self, session_id: str) -> str:
        with self._lock:
            return self._warnings.get(session_id, "")

    def caption_revision(self, session_id: str, source_id: str) -> int:
        with self._lock:
            return self._caption_revisions.get((session_id, source_id), 0)
