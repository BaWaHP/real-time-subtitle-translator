"""Read public media windows into memory using yt-dlp and FFmpeg.

FFmpeg outputs 16 kHz float PCM. The local gateway validates redirect and HLS
URLs and rejects encrypted playlists. Browser cookies stay in the browser.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import re
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx
import numpy as np

from .captions import parse_captions
from .types import MediaSource


class MediaError(RuntimeError):
    pass


class MediaCancelled(MediaError):
    pass


def validate_public_url(url: str, *, allow_local: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise MediaError("媒体地址格式无效。") from exc
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise MediaError("媒体地址必须使用 HTTP(S)，且不能包含用户名或密码。")
    if not allow_local:
        try:
            addresses = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                           type=socket.SOCK_STREAM)
        except OSError as exc:
            raise MediaError("媒体服务器域名无法解析。") from exc
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise MediaError("无法预读私网或本机视频，请使用系统声音模式。")
    return url


@contextmanager
def safe_response(client: httpx.Client, url: str, *, allow_local: bool = False, headers=None):
    """Validate each redirect target and limit the redirect count."""
    for _ in range(6):
        validate_public_url(url, allow_local=allow_local)
        with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise MediaError("媒体服务器返回了无效跳转。")
                url = urljoin(url, location)
                continue
            if not response.is_success:
                raise MediaError(f"媒体服务器拒绝预读（HTTP {response.status_code}），请使用系统声音模式。")
            yield response
            return
    raise MediaError("媒体服务器跳转次数过多。")


def fetch_text(url: str, *, allow_local=False, headers=None, limit=8 * 1024 * 1024) -> str:
    with httpx.Client(timeout=10, trust_env=False) as client:
        with safe_response(client, url, allow_local=allow_local, headers=headers) as response:
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise MediaError("字幕或播放列表过大，已停止预读。")
            return bytes(data).decode("utf-8-sig", errors="replace")


class _QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class MediaResolver:
    def __init__(self, *, allow_local: bool = False):
        self.allow_local = allow_local

    def resolve(self, snapshot: dict, cancel_event=None) -> MediaSource:
        if cancel_event is not None and cancel_event.is_set():
            raise MediaCancelled("预读已取消。")
        if snapshot.get("encrypted") or snapshot.get("drm"):
            raise MediaError("该视频受 DRM 保护，无法预读。请使用系统声音模式。")
        media_url = str(snapshot.get("media_url") or "")
        page_url = str(snapshot.get("page_url") or "")
        direct = media_url and urlsplit(media_url).scheme in {"http", "https"}
        if direct:
            validate_public_url(media_url, allow_local=self.allow_local)
            # Try the browser's media URL without its session cookies.
            return MediaSource(audio_url=media_url,
                               headers={"Referer": page_url} if page_url.startswith("https://") else {})
        if not page_url:
            raise MediaError("页面没有可读取的音轨或英文字幕，请开启英文字幕或使用系统声音模式。")
        validate_public_url(page_url, allow_local=self.allow_local)
        try:
            import yt_dlp
        except ImportError as exc:
            raise MediaError("缺少 yt-dlp，请重新安装项目依赖。") from exc
        # Extract public metadata without browser credentials or a disk cache.
        options = {"quiet": True, "no_warnings": True, "logger": _QuietLogger(),
                   "skip_download": True, "noplaylist": True, "cachedir": False,
                   "socket_timeout": 8, "retries": 0, "extractor_retries": 0,
                   "format": "bestaudio/best", "ignore_no_formats_error": True,
                   "writesubtitles": False, "writeautomaticsub": False}
        try:
            with yt_dlp.YoutubeDL(options) as extractor:
                info = extractor.extract_info(page_url, download=False)
        except Exception as exc:
            raise MediaError("无法读取此网站的字幕或音轨，请开启播放器英文字幕或使用系统声音模式。") from exc
        if cancel_event is not None and cancel_event.is_set():
            raise MediaCancelled("预读已取消。")
        if not isinstance(info, dict):
            raise MediaError("网站未返回可用视频信息。")
        if info.get("is_live") or info.get("live_status") == "is_live":
            raise MediaError("直播音频无法提前读取，请使用系统声音模式。")
        if info.get("has_drm"):
            raise MediaError("该视频受 DRM 保护，请使用系统声音模式。")
        headers = {str(key): str(value) for key, value in (info.get("http_headers") or {}).items()
                   if str(key).lower() in {"user-agent", "referer", "origin", "accept"}}
        cues = []
        for kind in ("subtitles", "automatic_captions"):
            tracks = info.get(kind) or {}
            languages = sorted((key for key in tracks if key == "en" or key.startswith("en-")),
                               key=lambda key: (key != "en", key))
            for language in languages:
                formats = sorted(tracks[language], key=lambda item: {
                    "json3": 0, "vtt": 1, "srv3": 2, "srv1": 3, "srt": 4}.get(item.get("ext"), 99))
                for track in formats:
                    if track.get("ext") not in {"json3", "vtt", "srv3", "srv1", "srt"}:
                        continue
                    try:
                        raw = fetch_text(track["url"], allow_local=self.allow_local, headers=headers)
                        cues = parse_captions(raw, track.get("ext", ""))
                    except (MediaError, httpx.HTTPError, ValueError, KeyError, TypeError):
                        continue
                    if cues:
                        break
                if cues:
                    break
            if cues:
                break
        audio_url = info.get("url") or ""
        if audio_url:
            validate_public_url(audio_url, allow_local=self.allow_local)
        if not audio_url and not cues:
            raise MediaError("此视频没有可读取的英文字幕或音轨，请使用系统声音模式。")
        return MediaSource(audio_url=audio_url, headers=headers, cues=cues)


@dataclass
class _Route:
    url: str


class MediaGateway:
    """Proxy one media source through loopback; keep signed URLs out of shell commands."""
    def __init__(self, source: MediaSource, *, allow_local=False):
        self.source, self.allow_local = source, allow_local
        self.routes: dict[str, _Route] = {}
        self.by_url: dict[str, str] = {}
        self.lock = threading.Lock()
        self.error = ""
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                with gateway.lock:
                    route = gateway.routes.get(self.path)
                if route is None:
                    self.send_error(404)
                    return
                try:
                    headers = dict(gateway.source.headers)
                    if self.headers.get("Range"):
                        headers["Range"] = self.headers["Range"]
                    with httpx.Client(timeout=10, trust_env=False) as client:
                        with safe_response(client, route.url, allow_local=gateway.allow_local,
                                           headers=headers) as response:
                            content_type = response.headers.get("content-type", "")
                            if ("dash+xml" in content_type.lower()
                                    or urlsplit(str(response.url)).path.lower().endswith(".mpd")):
                                raise MediaError("暂不支持预读 DASH 音轨，请开启英文字幕或使用系统声音模式。")
                            is_hls = ("mpegurl" in content_type.lower()
                                      or urlsplit(str(response.url)).path.lower().endswith(".m3u8"))
                            if is_hls:
                                data = bytearray()
                                for chunk in response.iter_bytes():
                                    data.extend(chunk)
                                    if len(data) > 2 * 1024 * 1024:
                                        raise MediaError("HLS 播放列表过大。")
                                text = data.decode("utf-8-sig")
                                if re.search(r"#EXT-X-(?:KEY|SESSION-KEY):.*METHOD=(?!NONE)", text):
                                    raise MediaError("无法预读加密的 HLS 音轨，请使用系统声音模式。")
                                if "#EXT-X-ENDLIST" not in text and "#EXT-X-STREAM-INF" not in text:
                                    raise MediaError("该 HLS 音轨仍在直播，无法提前读取。请使用系统声音模式。")
                                base = str(response.url)
                                rewritten = []
                                for line in text.splitlines():
                                    if line and not line.startswith("#"):
                                        line = gateway.route(urljoin(base, line))
                                    elif 'URI="' in line:
                                        line = re.sub(r'URI="([^"]+)"',
                                                      lambda match: 'URI="' + gateway.route(
                                                          urljoin(base, match.group(1))) + '"', line)
                                    rewritten.append(line)
                                payload = "\n".join(rewritten).encode("utf-8")
                                self.send_response(200)
                                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                                self.send_header("Content-Length", str(len(payload)))
                                self.end_headers()
                                self.wfile.write(payload)
                            else:
                                status, skip, remaining = response.status_code, 0, None
                                extra_headers = {}
                                requested = self.headers.get("Range", "")
                                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                                if match and status == 200 and response.headers.get("Content-Length"):
                                    # Emulate Range by skipping bytes when the origin returns 200.
                                    total = int(response.headers["Content-Length"])
                                    skip = int(match.group(1))
                                    end = min(total - 1, int(match.group(2)) if match.group(2) else total - 1)
                                    if skip >= total:
                                        self.send_error(416)
                                        return
                                    remaining = max(0, end - skip + 1)
                                    status = 206
                                    extra_headers = {"Content-Length": str(remaining),
                                                     "Content-Range": f"bytes {skip}-{end}/{total}"}
                                self.send_response(status)
                                for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                                    value = extra_headers.get(header, response.headers.get(header))
                                    if value:
                                        self.send_header(header, value)
                                self.end_headers()
                                for chunk in response.iter_bytes(64 * 1024):
                                    if skip:
                                        trim = min(skip, len(chunk))
                                        skip -= trim
                                        chunk = chunk[trim:]
                                    if remaining is not None:
                                        chunk = chunk[:remaining]
                                        remaining -= len(chunk)
                                    if chunk:
                                        self.wfile.write(chunk)
                                    if remaining == 0:
                                        break
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    gateway.error = str(exc) if isinstance(exc, MediaError) else "媒体网络读取失败。"
                    try:
                        self.send_error(502)
                    except OSError:
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="media-gateway")
        try:
            self.input_url = self.route(source.audio_url)
            self.thread.start()
        except Exception:
            self.server.server_close()
            raise

    def route(self, url: str) -> str:
        validate_public_url(url, allow_local=self.allow_local)
        with self.lock:
            existing = self.by_url.get(url)
            if existing is not None:
                return f"http://127.0.0.1:{self.server.server_address[1]}{existing}"
            # Cap manifest routes to bound gateway memory.
            if len(self.routes) >= 10_000:
                raise MediaError("媒体播放列表引用过多。")
            suffix = urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
            suffix = "." + suffix if suffix.lower() in {"m3u8", "ts", "m4s", "mp4", "aac", "m4a", "wav"} else ""
            key = "/" + secrets.token_urlsafe(24) + suffix
            self.routes[key] = _Route(url)
            self.by_url[url] = key
        return f"http://127.0.0.1:{self.server.server_address[1]}{key}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MediaReader:
    def __init__(self, source: MediaSource, *, allow_local: bool = False, timeout: float = 35):
        if not source.audio_url:
            raise MediaError("没有可读取的音轨。")
        self.source, self.timeout = source, timeout
        self.gateway = MediaGateway(source, allow_local=allow_local)

    def read(self, start: float, duration: float, cancel_event=None) -> np.ndarray:
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise MediaError("缺少 imageio-ffmpeg，请重新安装项目依赖。") from exc
        duration = max(.1, min(12, float(duration)))
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-protocol_whitelist", "http,tcp", "-rw_timeout", "10000000",
                   "-ss", f"{max(0, start):.3f}", "-i", self.gateway.input_url,
                   "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                   "-f", "f32le", "pipe:1"]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise MediaCancelled("音轨预读已取消。")
                if time.monotonic() > deadline:
                    raise MediaError("音轨预读超时，请重试或使用系统声音模式。")
                try:
                    output, _stderr = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise MediaError(self.gateway.error or "音轨无法解码，或服务器不允许预读。请使用系统声音模式。")
            if len(output) > int((duration + 1) * 16000 * 4):
                raise MediaError("解码音频超出预读窗口，已停止。")
            return np.frombuffer(output[:len(output) - len(output) % 4], dtype="<f4").copy()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def close(self):
        self.gateway.close()
