"""ChatGPT OAuth login and token storage.

Reference: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
Run login, refresh and logout on a worker thread.
"""

from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid
import webbrowser

import httpx
import jwt
from filelock import FileLock

ISSUER = "https://auth.openai.com"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
APP_NAME = "Real-Time Subtitle Translator"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
}


class AuthError(RuntimeError):
    """Authorization error with optional server diagnostics."""

    def __init__(self, message: str, *, code: str = "", status_code: int | None = None,
                 request_id: str = ""):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Encrypt or decrypt with current-user DPAPI, without prompting."""
    if os.name != "nt":
        raise AuthError("安全凭据存储需要 Windows Credential Manager 或 Windows DPAPI。")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        operation = crypt.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                              ctypes.POINTER(Blob)]
        ok = operation(ctypes.byref(source), None, None, None, None, 1,
                       ctypes.byref(result))
    else:
        operation = crypt.CryptProtectData
        operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                              ctypes.POINTER(Blob)]
        ok = operation(ctypes.byref(source), APP_NAME, None, None, None, 1,
                       ctypes.byref(result))
    if not ok:
        raise AuthError("Windows DPAPI 无法读写凭据，请使用保存凭据时的 Windows 账户重试。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class ProtectedCredentialStore:
    """Store token chunks in WinVault, with DPAPI as a fallback.

    Writing the manifest publishes a complete generation of chunks.
    Load and save require the caller's cross-process lock.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.marker = self.data_dir / "credentials.backend"
        self.encrypted_file = self.data_dir / "credentials.dpapi"
        suffix = hashlib.sha256(str(self.data_dir.resolve()).encode()).hexdigest()[:20]
        self.service = "RealtimeSubtitle.ChatGPT." + suffix
        self._vault = None
        if os.name == "nt":
            try:
                from keyring.backends.Windows import WinVaultKeyring
                self._vault = WinVaultKeyring()
            except (ImportError, RuntimeError):
                pass

    def _manifest(self) -> dict | None:
        raw = self._vault.get_password(self.service, "manifest")
        return json.loads(raw) if raw else None

    def _read_vault(self) -> dict:
        manifest = self._manifest()
        if not manifest:
            return {}
        generation = manifest["generation"]
        count = int(manifest["chunks"])
        if len(generation) != 32 or not all(c in "0123456789abcdef" for c in generation):
            raise AuthError("Windows 凭据清单已损坏，请重新授权。")
        if not 1 <= count <= 1024:
            raise AuthError("Windows 凭据清单长度异常。")
        parts = [self._vault.get_password(self.service, f"{generation}.{i}")
                 for i in range(count)]
        if any(part is None for part in parts):
            raise AuthError("Windows 凭据不完整，请重新授权。")
        return json.loads(base64.b64decode("".join(parts), validate=True))

    def load(self) -> dict:
        backend = self.marker.read_text("ascii").strip() if self.marker.exists() else ""
        if backend == "dpapi":
            return json.loads(_dpapi(self.encrypted_file.read_bytes(), decrypt=True))
        if backend == "winvault":
            if self._vault is None:
                raise AuthError("Windows Credential Manager 暂时不可用，请稍后重试。")
            try:
                return self._read_vault()
            except AuthError:
                raise
            except Exception as exc:
                raise AuthError("无法读取 Windows Credential Manager，请稍后重试。") from exc
        return {}

    def save(self, state: dict) -> None:
        raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self._vault is not None:
            try:
                encoded = base64.b64encode(raw).decode("ascii")
                generation = uuid.uuid4().hex
                chunks = [encoded[i:i + 1000] for i in range(0, len(encoded), 1000)]
                old = self._manifest()
                for index, chunk in enumerate(chunks):
                    self._vault.set_password(self.service, f"{generation}.{index}", chunk)
                self._vault.set_password(self.service, "manifest", json.dumps({
                    "generation": generation, "chunks": len(chunks)}))
                _atomic_write(self.marker, b"winvault")
                self.encrypted_file.unlink(missing_ok=True)
                if old and 0 < int(old.get("chunks", 0)) <= 1024:
                    for index in range(int(old["chunks"])):
                        try:
                            self._vault.delete_password(self.service, f"{old['generation']}.{index}")
                        except Exception:
                            pass
                return
            except Exception:
                # Fall back to DPAPI if Credential Manager is unavailable.
                pass
        _atomic_write(self.encrypted_file, _dpapi(raw))
        _atomic_write(self.marker, b"dpapi")


def _timestamp(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return default


class ChatGPTAuth:
    def __init__(self, data_dir: Path | None = None, on_status: Callable[[str], None] | None = None,
                 *, client: httpx.Client | None = None, store=None):
        if data_dir is None:
            from config import app_data_dir
            data_dir = app_data_dir()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.on_status = on_status or (lambda _message: None)
        self._thread_lock = threading.RLock()
        self._process_lock = FileLock(str(self.data_dir / "auth.lock"), timeout=30)
        self._store = store if store is not None else ProtectedCredentialStore(self.data_dir)
        self._client_owned = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0),
                                             follow_redirects=False)
        self._discovery_cache: dict | None = None
        self._jwks: dict = {}
        self._jwks_at = 0.0
        self._login_lock = threading.Lock()
        with self._thread_lock, self._process_lock:
            host_path = self.data_dir / "host.json"
            if host_path.exists():
                host_data = json.loads(host_path.read_text("utf-8"))
                self.ext_agent_host_id = str(host_data["ext_agent_host_id"])
            else:
                self.ext_agent_host_id = "urn:uuid:" + str(uuid.uuid4())
                _atomic_write(host_path, json.dumps({
                    "ext_agent_host_id": self.ext_agent_host_id}).encode())

    def _read_state(self) -> dict:
        state = self._store.load() or {}
        state.setdefault("accounts", {})
        state.setdefault("active_account_id", None)
        return state

    @property
    def active_account_id(self) -> str | None:
        with self._thread_lock, self._process_lock:
            return self._read_state()["active_account_id"]

    @property
    def is_authenticated(self) -> bool:
        with self._thread_lock, self._process_lock:
            state = self._read_state()
            account = state["accounts"].get(state["active_account_id"], {})
            return bool(account.get("subject") and
                        (account.get("access_token") or account.get("refresh_token") or account.get("id_token")))

    def accounts(self) -> list[dict]:
        with self._thread_lock, self._process_lock:
            state = self._read_state()
            return [{"id": key, "email": value.get("email", ""),
                     "client_id": value["client_id"],
                     "label": (value.get("email") or "未完成授权") + " · " + value["client_id"][-10:],
                     "authenticated": bool(value.get("subject") and (value.get("access_token") or
                                            value.get("refresh_token") or value.get("id_token"))),
                     "plan_enabled": DIRECT_SCOPES.issubset(value.get("scopes", []))}
                    for key, value in state["accounts"].items()]

    def status(self) -> str:
        with self._thread_lock, self._process_lock:
            state = self._read_state()
            account = state["accounts"].get(state["active_account_id"], {})
            if not account.get("subject") or not any(account.get(field) for field in
                                                    ("access_token", "refresh_token", "id_token")):
                return "未登录 ChatGPT"
            if not DIRECT_SCOPES.issubset(account.get("scopes", [])):
                return "已登录，但未授权翻译，请重新登录并完成授权"
            return "已登录 ChatGPT · " + account.get("email", "账户")

    def select_account(self, account_id: str) -> None:
        with self._thread_lock, self._process_lock:
            state = self._read_state()
            if account_id not in state["accounts"]:
                raise AuthError("所选 ChatGPT 账户不存在。")
            state["active_account_id"] = account_id
            self._store.save(state)
        self.on_status(self.status())

    @staticmethod
    def _trusted_endpoint(url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "auth.openai.com" or parsed.fragment:
            raise AuthError("OpenAI 身份发现文档包含非官方地址，已停止授权。")
        return url

    def _discovery(self) -> dict:
        if self._discovery_cache is None:
            response = self._client.get(DISCOVERY)
            self._check_response(response, "获取 OpenAI 身份配置失败")
            data = response.json()
            if data.get("issuer") != ISSUER:
                raise AuthError("OpenAI 身份发行者不匹配。")
            for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                self._trusted_endpoint(data[field])
            self._discovery_cache = data
        return self._discovery_cache

    @staticmethod
    def _check_response(response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error", {}) if isinstance(body, dict) else {}
        code = error if isinstance(error, str) else error.get("code", "")
        request_id = response.headers.get("x-request-id", response.headers.get("openai-request-id", ""))
        hint = "；请重新登录" if code in TERMINAL_REFRESH_ERRORS else ""
        raise AuthError(f"{action}（HTTP {response.status_code}, {code or '无错误码'}）{hint}",
                        code=code, status_code=response.status_code, request_id=request_id)

    def _validate_identity(self, encoded: str, client_id: str, nonce: str | None) -> dict:
        config = self._discovery()
        try:
            header = jwt.get_unverified_header(encoded)
            algorithm = header.get("alg")
            if algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}:
                raise AuthError("OpenAI ID Token 使用了不支持的签名算法。")
            for attempt in range(2):
                if attempt or time.monotonic() - self._jwks_at > 3600 or not self._jwks:
                    response = self._client.get(config["jwks_uri"])
                    self._check_response(response, "获取 OpenAI 签名密钥失败")
                    self._jwks, self._jwks_at = response.json(), time.monotonic()
                matches = [key for key in self._jwks.get("keys", [])
                           if key.get("kid") == header.get("kid") and key.get("use", "sig") == "sig"
                           and key.get("alg", algorithm) == algorithm]
                if len(matches) == 1:
                    break
            else:
                raise AuthError("找不到 OpenAI ID Token 对应的唯一签名密钥。")
            key = jwt.PyJWK.from_dict(matches[0], algorithm=algorithm).key
            identity = jwt.decode(encoded, key, algorithms=[algorithm], audience=client_id,
                                  issuer=ISSUER, leeway=5,
                                  options={"require": ["sub", "exp", "iat", "iss", "aud"]})
            if not isinstance(identity.get("sub"), str) or not identity["sub"]:
                raise AuthError("OpenAI ID Token 缺少有效账户标识。")
            if nonce is not None and (not isinstance(identity.get("nonce"), str) or
                                      not secrets.compare_digest(identity["nonce"], nonce)):
                raise AuthError("OpenAI 登录 nonce 校验失败。")
            if isinstance(identity["aud"], list) and len(identity["aud"]) > 1:
                if identity.get("azp") != client_id:
                    raise AuthError("OpenAI ID Token 的授权客户端不匹配。")
            return identity
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise AuthError("OpenAI ID Token 签名、发行者、客户端或有效期校验失败。") from exc

    def _token_record(self, tokens: dict, client_id: str, old: dict | None = None) -> dict:
        old = old or {}
        # Omitted scope on refresh means the original grant remains in force.
        granted = tokens.get("scope")
        scopes = granted.split() if isinstance(granted, str) else old.get("scopes", [])
        access = tokens.get("access_token")
        plan_enabled = DIRECT_SCOPES.issubset(scopes)
        if not isinstance(access, str):
            access = ""
        if plan_enabled and not access:
            raise AuthError("登录响应缺少 Access Token，请重新登录。")
        if access and str(tokens.get("token_type", "")).lower() != "bearer":
            raise AuthError("OpenAI 返回了非 Bearer 的令牌类型。")
        try:
            expires_in = float(tokens["expires_in"]) if access else 0.0
            if access and not 0 < expires_in <= 86400:
                raise ValueError
        except (ValueError, TypeError, KeyError) as exc:
            raise AuthError("登录令牌的有效期不正确。") from exc
        now = time.time()
        record = copy.deepcopy(old)
        record.update({"client_id": client_id, "access_token": access,
                       "refresh_token": tokens.get("refresh_token", old.get("refresh_token", "")),
                       "id_token": tokens.get("id_token", old.get("id_token", "")),
                       "token_type": "Bearer", "scopes": scopes,
                       "expires_in": expires_in, "expires_at": now + expires_in,
                       "earliest_refresh_at": _timestamp(tokens.get("earliest_refresh_at")),
                       "saved_at": datetime.now(timezone.utc).isoformat(),
                       "ext_agent_host_id": self.ext_agent_host_id})
        return record

    def login(self, account_id: str | None = None, cancel_event: threading.Event | None = None,
              *, request_consent: bool = False) -> dict:
        """Run the blocking PKCE browser flow for a new or saved account.

        request_consent asks again after the user declined subscription access.
        """
        if not self._login_lock.acquire(blocking=False):
            raise AuthError("已有 ChatGPT 登录正在进行。")
        try:
            with self._thread_lock, self._process_lock:
                state = self._read_state()
                selected = copy.deepcopy(state["accounts"].get(account_id)) if account_id else None
                if account_id and selected is None:
                    raise AuthError("未找到所选账户的登录记录，请重新登录。")
            config = self._discovery()
            state_value, nonce, verifier = (secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                                             secrets.token_urlsafe(64))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            callback: dict = {}

            class CallbackHandler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass  # Callback URLs contain an authorization code.

                def do_GET(self):
                    parsed = urlsplit(self.path)
                    if parsed.path != "/auth/callback":
                        self.send_error(404)
                        return
                    values = parse_qs(parsed.query, keep_blank_values=True)
                    # Invalid callbacks leave the pending sign-in open for retry.
                    returned_state = values.get("state", [])
                    if (len(returned_state) != 1 or not secrets.compare_digest(returned_state[0], state_value)
                            or any(len(value) != 1 for value in values.values())):
                        self.send_error(400, "Invalid OAuth state")
                        return
                    if callback:
                        self.send_error(409)
                        return
                    callback.update({key: value[0] for key, value in values.items()})
                    body = ("<!doctype html><meta charset=utf-8><title>ChatGPT 登录</title>"
                            "<p>已收到登录信息，请返回字幕翻译软件查看结果。</p>").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'none'")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            with HTTPServer(("127.0.0.1", 0), CallbackHandler) as server:
                server.timeout = 0.25
                redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
                pending_client = selected["client_id"] if selected else "dynamic_agent_client"
                parameters = {"client_id": pending_client, "ext_agent_host_id": self.ext_agent_host_id,
                              "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
                              "resource": RESOURCE, "state": state_value, "nonce": nonce,
                              "code_challenge_method": "S256", "code_challenge": challenge}
                if selected:
                    if selected.get("id_token"):
                        parameters["id_token_hint"] = selected["id_token"]
                    if selected.get("email"):
                        parameters["login_hint"] = selected["email"]
                else:
                    parameters["agent_name_hint"] = APP_NAME
                if request_consent:
                    parameters["prompt"] = "consent"
                self.on_status("请在浏览器完成 ChatGPT 登录和授权，10 分钟内未完成将超时。")
                # Do not log sign-in URLs: they may contain id_token_hint.
                if not webbrowser.open(config["authorization_endpoint"] + "?" + urlencode(parameters)):
                    raise AuthError("无法打开默认浏览器，请设置 Windows 默认浏览器后重试。")
                deadline = time.monotonic() + 600
                while not callback:
                    if cancel_event is not None and cancel_event.is_set():
                        raise AuthError("已取消 ChatGPT 登录。", code="cancelled")
                    if time.monotonic() >= deadline:
                        raise AuthError("ChatGPT 登录已超时，请重新登录。")
                    server.handle_request()
            if cancel_event is not None and cancel_event.is_set():
                raise AuthError("已取消 ChatGPT 登录。", code="cancelled")
            if callback.get("error"):
                raise AuthError("ChatGPT 授权未完成，请重新登录并确认浏览器中的授权提示。",
                                code=callback["error"])
            code = callback.get("code")
            if not code:
                raise AuthError("ChatGPT 回调缺少授权码。")
            issued_client = callback.get("client_id", pending_client)
            if not issued_client or issued_client == "dynamic_agent_client":
                raise AuthError("ChatGPT 动态注册未返回正式 client_id。")
            if selected and issued_client != selected["client_id"]:
                raise AuthError("登录响应中的 client_id 与所选账户不一致，请重新登录。")
            response = self._client.post(config["token_endpoint"], data={
                "grant_type": "authorization_code", "client_id": issued_client, "code": code,
                "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE})
            try:
                self._check_response(response, "ChatGPT 授权码交换失败")
            except AuthError as exc:
                if exc.code == "invalid_grant" and not selected:
                    # Save the client ID for retry; it has no verified session yet.
                    with self._thread_lock, self._process_lock:
                        saved = self._read_state()
                        saved["accounts"].setdefault(issued_client, {"client_id": issued_client})
                        self._store.save(saved)
                raise
            tokens = response.json()
            identity = self._validate_identity(tokens.get("id_token", ""), issued_client, nonce)
            if selected and selected.get("subject") and (
                    identity["sub"] != selected["subject"] or identity["iss"] != selected["issuer"]):
                raise AuthError("登录身份与所选账户不一致，请使用对应的 ChatGPT 账户登录。")
            record = self._token_record(tokens, issued_client)
            record.update({"email": identity.get("email", ""), "issuer": identity["iss"],
                           "subject": identity["sub"]})
            if "offline_access" in record["scopes"] and not record["refresh_token"]:
                raise AuthError("已授权 offline_access，但 OpenAI 未返回 Refresh Token。")
            with self._thread_lock, self._process_lock:
                saved = self._read_state()
                existing = saved["accounts"].get(issued_client, {})
                if existing.get("subject") and existing["subject"] != identity["sub"]:
                    raise AuthError("此 client_id 已关联其他账户，请重新选择 ChatGPT 账户。")
                saved["accounts"][issued_client] = record
                saved["active_account_id"] = issued_client
                self._store.save(saved)
            self.on_status(self.status())
            return next(value for value in self.accounts() if value["id"] == issued_client)
        except httpx.HTTPError as exc:
            raise AuthError("ChatGPT 授权网络请求失败，请检查连接后重试。") from exc
        finally:
            self._login_lock.release()

    @staticmethod
    def _clear_tokens(account: dict) -> None:
        for field in ("access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at"):
            account.pop(field, None)

    def access_token(self, force_refresh: bool = False) -> str:
        # Read under both locks in case another process rotated the refresh token.
        with self._thread_lock, self._process_lock:
            state = self._read_state()
            account = state["accounts"].get(state["active_account_id"], {})
            if not account.get("subject") or not any(account.get(field) for field in
                                                    ("access_token", "refresh_token", "id_token")):
                raise AuthError("请先点击 Continue with ChatGPT 登录。", code="sign_in_required")
            if not DIRECT_SCOPES.issubset(account.get("scopes", [])):
                raise AuthError("未获得 ChatGPT 订阅使用权限，请重新授权。",
                                code="plan_scope_missing")
            now = time.time()
            expires_at = _timestamp(account.get("expires_at"))
            earliest = _timestamp(account.get("earliest_refresh_at"))
            if account.get("access_token") and not force_refresh and now < expires_at - 60:
                return account["access_token"]
            if now < earliest:
                if not force_refresh and now < expires_at and account.get("access_token"):
                    return account["access_token"]
                raise AuthError("OpenAI 尚未允许刷新此会话；请稍后重试。", code="refresh_too_early")
            if not account.get("refresh_token"):
                raise AuthError("ChatGPT 会话已过期，请重新登录。", code="sign_in_required")
            try:
                response = self._client.post(self._discovery()["token_endpoint"], data={
                    "grant_type": "refresh_token", "client_id": account["client_id"],
                    "refresh_token": account["refresh_token"], "resource": RESOURCE})
                self._check_response(response, "ChatGPT 令牌刷新失败")
                tokens = response.json()
                if not tokens.get("refresh_token"):
                    raise AuthError("刷新响应缺少新的 Refresh Token，请重新登录。")
                if tokens.get("id_token"):
                    identity = self._validate_identity(tokens["id_token"], account["client_id"], None)
                    if identity["sub"] != account["subject"] or identity["iss"] != account["issuer"]:
                        raise AuthError("刷新后的登录身份与当前账户不一致，请重新登录。")
                refreshed = self._token_record(tokens, account["client_id"], account)
                state["accounts"][state["active_account_id"]] = refreshed
                self._store.save(state)
                if not DIRECT_SCOPES.issubset(refreshed["scopes"]):
                    raise AuthError("刷新后的 ChatGPT 会话缺少订阅使用权限，请重新授权。",
                                    code="plan_scope_missing")
                return refreshed["access_token"]
            except AuthError as exc:
                if exc.code in TERMINAL_REFRESH_ERRORS:
                    self._clear_tokens(account)
                    self._store.save(state)
                raise
            except httpx.HTTPError as exc:
                raise AuthError("ChatGPT 登录状态刷新失败，请检查网络后重试。") from exc

    def logout(self) -> str:
        confirmed = True
        with self._thread_lock, self._process_lock:
            state = self._read_state()
            account = state["accounts"].get(state["active_account_id"], {})
            refresh = account.get("refresh_token")
            if refresh:
                confirmed = False
                for attempt in range(2):
                    try:
                        endpoint = self._trusted_endpoint(self._discovery()["revocation_endpoint"])
                        response = self._client.post(endpoint, data={
                            "token": refresh, "token_type_hint": "refresh_token",
                            "client_id": account["client_id"]})
                        if response.status_code == 200:
                            confirmed = True
                            break
                        if response.status_code < 500:
                            break
                    except (httpx.HTTPError, AuthError, KeyError):
                        pass
                    if attempt == 0:
                        time.sleep(0.5)
            self._clear_tokens(account)
            self._store.save(state)
        message = ("已退出 ChatGPT" if confirmed else
                   "已退出本机登录，但无法确认远程授权是否已撤销。请在 ChatGPT 设置中断开此应用。")
        self.on_status(message)
        return message

    def close(self) -> None:
        if self._client_owned:
            self._client.close()
