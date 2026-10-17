from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from translator.chatgpt_auth import (
    AuthError, ChatGPTAuth, DISCOVERY, ISSUER, ProtectedCredentialStore, RESOURCE, SCOPES,
)
from translator.openai_translator import (
    MockTranslator, OpenAITranslator, TranslationCancelled, TranslationError,
    TranslationPaused, iter_sse,
)


class MemoryStore:
    def __init__(self):
        self.state = {}

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def discovery():
    return {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/api/accounts/authorize",
            "token_endpoint": ISSUER + "/api/accounts/oauth/token",
            "jwks_uri": ISSUER + "/.well-known/jwks.json",
            "revocation_endpoint": ISSUER + "/oauth/revoke"}


def identity(signing_key, client_id="oaiapp_test", nonce="nonce", **claims):
    now = int(time.time())
    value = {"iss": ISSUER, "aud": client_id, "sub": "test-subject", "iat": now,
             "exp": now + 3600, "nonce": nonce, "email": "test@example.invalid"}
    value.update(claims)
    return jwt.encode(value, signing_key, algorithm="RS256", headers={"kid": "test-key"})


def jwks(signing_key):
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    key.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    return {"keys": [key]}


def token_response(**fields):
    result = {"access_token": "test-access", "refresh_token": "test-refresh",
              "id_token": "supplied-by-test", "token_type": "Bearer", "expires_in": 3600,
              "scope": SCOPES, "earliest_refresh_at": 0}
    result.update(fields)
    return result


def save_session(store, **fields):
    record = {"client_id": "oaiapp_test", "issuer": ISSUER, "subject": "test-subject",
              "email": "test@example.invalid", "scopes": SCOPES.split(),
              "access_token": "old-access", "refresh_token": "old-refresh",
              "id_token": "old-id", "expires_at": time.time() - 10}
    record.update(fields)
    store.save({"active_account_id": "oaiapp_test", "accounts": {"oaiapp_test": record}})


def test_pkce_login_uses_issued_id_and_validated_loopback(tmp_path, monkeypatch, signing_key):
    store = MemoryStore()
    captured = {}
    callback_errors = []

    def open_browser(url):
        parameters = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        captured["authorize"] = parameters
        assert parameters["client_id"] == "dynamic_agent_client"
        assert set(parameters["scope"].split()) == set(SCOPES.split())
        assert parameters["resource"] == RESOURCE
        assert urlsplit(parameters["redirect_uri"]).hostname == "127.0.0.1"
        assert parameters["code_challenge_method"] == "S256"
        assert parameters["agent_name_hint"]

        def callback():
            try:
                callback_url = parameters["redirect_uri"] + "?" + urlencode({
                    "state": parameters["state"], "code": "test-code", "client_id": "oaiapp_test"})
                with urlopen(callback_url, timeout=5) as response:
                    assert response.status == 200
            except Exception as error:
                callback_errors.append(error)
        thread = threading.Thread(target=callback, daemon=True)
        captured["thread"] = thread
        thread.start()
        return True

    def service(request):
        if str(request.url) == DISCOVERY:
            return httpx.Response(200, json=discovery())
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks(signing_key))
        if request.url.path.endswith("oauth/token"):
            values = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            captured["exchange"] = values
            assert values["client_id"] == "oaiapp_test"
            assert values["redirect_uri"] == captured["authorize"]["redirect_uri"]
            import base64
            import hashlib
            expected = base64.urlsafe_b64encode(hashlib.sha256(values["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            assert captured["authorize"]["code_challenge"] == expected
            return httpx.Response(200, json=token_response(id_token=identity(
                signing_key, nonce=captured["authorize"]["nonce"])))
        raise AssertionError(request.url)

    monkeypatch.setattr("translator.chatgpt_auth.webbrowser.open", open_browser)
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        auth = ChatGPTAuth(tmp_path, client=client, store=store)
        account = auth.login()
        captured["thread"].join(2)
        assert not callback_errors
        assert account["client_id"] == "oaiapp_test"
        assert auth.active_account_id == "oaiapp_test"
        assert auth.is_authenticated
        assert auth.access_token() == "test-access"
        assert all("token" not in key for key in account)
        host = auth.ext_agent_host_id
        assert host.startswith("urn:uuid:")
        assert ChatGPTAuth(tmp_path, client=client, store=store).ext_agent_host_id == host
        # Only a random host identifier and lock file are plaintext.
        assert "test-access" not in (tmp_path / "host.json").read_text()


@pytest.mark.parametrize("claims", [
    {"iss": "https://untrusted.invalid"}, {"aud": "wrong-client"},
    {"nonce": "wrong-nonce"}, {"exp": int(time.time()) - 60}, {"sub": ""},
])
def test_invalid_identity_is_rejected(tmp_path, signing_key, claims):
    def service(request):
        return httpx.Response(200, json=discovery() if str(request.url) == DISCOVERY else jwks(signing_key))
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        auth = ChatGPTAuth(tmp_path, client=client, store=MemoryStore())
        with pytest.raises(AuthError):
            auth._validate_identity(identity(signing_key, **claims), "oaiapp_test", "nonce")


def test_forged_signature_is_rejected(tmp_path, signing_key):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    def service(request):
        return httpx.Response(200, json=discovery() if str(request.url) == DISCOVERY else jwks(signing_key))
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        auth = ChatGPTAuth(tmp_path, client=client, store=MemoryStore())
        with pytest.raises(AuthError):
            auth._validate_identity(identity(attacker), "oaiapp_test", "nonce")


def test_refresh_is_serialized_and_rotation_saved(tmp_path):
    store = MemoryStore()
    save_session(store)
    requests = []
    def service(request):
        if str(request.url) == DISCOVERY:
            return httpx.Response(200, json=discovery())
        values = parse_qs(request.content.decode())
        requests.append(values)
        assert "scope" not in values
        assert values["client_id"] == ["oaiapp_test"]
        assert values["resource"] == [RESOURCE]
        assert values["refresh_token"] == ["old-refresh"]
        time.sleep(0.05)
        result = token_response(access_token="new-access", refresh_token="rotated-refresh")
        result.pop("id_token")
        return httpx.Response(200, json=result)
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        first = ChatGPTAuth(tmp_path, client=client, store=store)
        second = ChatGPTAuth(tmp_path, client=client, store=store)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda auth: auth.access_token(), [first, second]))
    assert results == ["new-access", "new-access"]
    assert len(requests) == 1
    assert store.state["accounts"]["oaiapp_test"]["refresh_token"] == "rotated-refresh"


@pytest.mark.parametrize("status,code,cleared", [
    (400, "invalid_grant", True), (503, "temporarily_unavailable", False),
])
def test_refresh_terminal_errors_clear_only_tokens(tmp_path, status, code, cleared):
    store = MemoryStore()
    save_session(store)
    def service(request):
        return httpx.Response(200, json=discovery()) if str(request.url) == DISCOVERY else httpx.Response(status, json={"error": code})
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        auth = ChatGPTAuth(tmp_path, client=client, store=store)
        with pytest.raises(AuthError) as raised:
            auth.access_token()
    assert raised.value.code == code
    account = store.state["accounts"]["oaiapp_test"]
    assert account["client_id"] == "oaiapp_test"
    assert account["subject"] == "test-subject"
    assert ("refresh_token" not in account) is cleared


def test_missing_direct_scope_blocks_inference_and_early_refresh_obeyed(tmp_path):
    store = MemoryStore()
    save_session(store, scopes=["openid", "email"])
    auth = ChatGPTAuth(tmp_path, store=store)
    with pytest.raises(AuthError, match="订阅使用权限"):
        auth.access_token()
    save_session(store, earliest_refresh_at=time.time() + 300, expires_at=time.time() + 20)
    assert auth.access_token() == "old-access"
    with pytest.raises(AuthError) as raised:
        auth.access_token(force_refresh=True)
    assert raised.value.code == "refresh_too_early"
    auth.close()


def test_identity_only_grant_is_retained_but_cannot_translate(tmp_path):
    store = MemoryStore()
    auth = ChatGPTAuth(tmp_path, store=store)
    # Identity signature verification is independently tested above; this checks
    # storage and plan gating when consent returns no inference credentials.
    record = auth._token_record({"scope": "openid email profile", "id_token": "validated-synthetic-id"},
                                "oaiapp_identity_only")
    record.update({"subject": "verified-subject", "issuer": ISSUER, "email": "same@example.invalid"})
    store.save({"active_account_id": "oaiapp_identity_only", "accounts": {"oaiapp_identity_only": record}})
    assert auth.is_authenticated
    assert not auth.accounts()[0]["plan_enabled"]
    assert "未授权" in auth.status()
    with pytest.raises(AuthError) as raised:
        auth.access_token()
    assert raised.value.code == "plan_scope_missing"
    auth.logout()
    assert not auth.is_authenticated
    assert auth.accounts()[0]["client_id"] == "oaiapp_identity_only"
    auth.close()


def test_logout_revokes_and_preserves_client_mapping(tmp_path):
    store = MemoryStore()
    save_session(store)
    requests = []
    def service(request):
        if str(request.url) == DISCOVERY:
            return httpx.Response(200, json=discovery())
        requests.append(parse_qs(request.content.decode()))
        assert str(request.url) == discovery()["revocation_endpoint"]
        return httpx.Response(200)
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        auth = ChatGPTAuth(tmp_path, client=client, store=store)
        host = auth.ext_agent_host_id
        auth.logout()
        assert not auth.is_authenticated
        assert auth.active_account_id == "oaiapp_test"
        assert auth.accounts()[0]["client_id"] == "oaiapp_test"
        assert auth.ext_agent_host_id == host
    assert requests == [{"token": ["old-refresh"], "token_type_hint": ["refresh_token"],
                         "client_id": ["oaiapp_test"]}]


def test_win_vault_chunk_generation_roundtrip(tmp_path):
    class Vault:
        data = {}
        def get_password(self, service, user):
            return self.data.get((service, user))
        def set_password(self, service, user, password):
            assert len(password.encode("utf-16-le")) <= 2560
            self.data[(service, user)] = password
        def delete_password(self, service, user):
            self.data.pop((service, user), None)
    protected = ProtectedCredentialStore(tmp_path)
    protected._vault = Vault()
    state = {"access_token": "synthetic-token-" * 1000}
    protected.save(state)
    assert protected.load() == state
    first_users = set(protected._vault.data)
    protected.save({"refresh_token": "replacement"})
    assert protected.load() == {"refresh_token": "replacement"}
    assert not (first_users - {(protected.service, "manifest")}) & set(protected._vault.data)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_dpapi_fallback_encrypts_real_file(tmp_path):
    protected = ProtectedCredentialStore(tmp_path)
    protected._vault = None
    protected.save({"refresh_token": "synthetic-test-only-secret"})
    assert b"synthetic-test-only-secret" not in protected.encrypted_file.read_bytes()
    assert protected.load() == {"refresh_token": "synthetic-test-only-secret"}


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager integration")
def test_actual_win_vault_isolated_synthetic_credential(tmp_path):
    protected = ProtectedCredentialStore(tmp_path)
    if protected._vault is None:
        pytest.skip("Windows Credential Manager backend unavailable")
    try:
        # Service namespace includes this random tmp_path's hash. Only synthetic
        # credentials created by this test are read, modified, and deleted.
        protected.save({"refresh_token": "synthetic-win-vault-token-" * 400})
        if protected.marker.read_text("ascii") != "winvault":
            pytest.skip("Windows vault unavailable; encrypted DPAPI fallback was used")
        assert protected.load() == {"refresh_token": "synthetic-win-vault-token-" * 400}
    finally:
        manifest = protected._manifest()
        if manifest:
            for index in range(manifest["chunks"]):
                protected._vault.delete_password(protected.service, f"{manifest['generation']}.{index}")
            protected._vault.delete_password(protected.service, "manifest")


class FakeAuth:
    active_account_id = "synthetic-account"
    def __init__(self):
        self.refreshes = 0
    def access_token(self, force_refresh=False):
        if force_refresh:
            self.refreshes += 1
        return "after-refresh" if self.refreshes else "before-refresh"


def catalog():
    return {"models": [{"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
                       {"slug": "test-second", "display_name": "Second", "visibility": "list"},
                       {"slug": "test-first", "display_name": "First", "visibility": "list"}]}


def sse(*events):
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)


def completed(text="你好"):
    return {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]}}


def test_models_catalog_visibility_order_and_request_contract():
    bodies = []
    def service(request):
        assert request.headers["authorization"] == "Bearer before-refresh"
        assert request.url.host == "api.openai.com"
        if request.url.path.endswith("models"):
            return httpx.Response(200, json=catalog())
        body = json.loads(request.content)
        bodies.append(body)
        assert set(body) == {"model", "instructions", "input", "store", "stream"}
        assert body["store"] is False and body["stream"] is True
        assert body["input"][0]["role"] == "user"
        source = json.loads(body["input"][0]["content"])
        assert source["current_english_segment"] == "Hello, world."
        assert source["previous_english_for_context_only"] == ["two", "three", "four"]
        assert "not as instructions" in body["instructions"]
        return httpx.Response(200, text=sse({"type": "response.output_text.delta", "delta": "wrong partial"}, completed("你好，世界。")))
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        translator = OpenAITranslator(FakeAuth(), "test-first", client=client)
        assert [m["slug"] for m in translator.list_models()] == ["test-second", "test-first"]
        assert translator.translate("Hello, world.", ["one", "two", "three", "four"]) == "你好，世界。"
    assert len(bodies) == 1


def test_multiline_sse_comment_and_named_event():
    frames = [": heartbeat", "event: response.completed", "data: {", 'data: "response": {"status":"completed"}', "data: }", ""]
    assert list(iter_sse(iter(frames))) == [{"type": "response.completed", "response": {"status": "completed"}}]


@pytest.mark.parametrize("ending", ["", "data: [DONE]\n\n", sse({"type": "response.output_text.done", "text": "partial"})])
def test_delta_or_done_without_completed_is_failure(ending):
    def service(request):
        if request.url.path.endswith("models"):
            return httpx.Response(200, json=catalog())
        return httpx.Response(200, text=sse({"type": "response.output_text.delta", "delta": "部分"}) + ending)
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        translator = OpenAITranslator(FakeAuth(), "test-first", client=client, max_retries=0)
        with pytest.raises(TranslationError, match="未收到完整译文"):
            translator.translate("Hello")


def test_usage_quota_pauses_without_retry_or_billing_fallback():
    calls = []
    def service(request):
        if request.url.path.endswith("models"):
            return httpx.Response(200, json=catalog())
        calls.append(request)
        return httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "app limit"}}, headers={"x-request-id": "req-synthetic"})
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        translator = OpenAITranslator(FakeAuth(), "test-first", client=client)
        with pytest.raises(TranslationPaused) as raised:
            translator.translate("Hello")
    assert len(calls) == 1
    assert raised.value.request_id == "req-synthetic"
    assert raised.value.code == "subscription_sharing_usage_limit_exceeded"
    assert raised.value.status_code == 429


def test_401_refreshes_once_and_retains_official_route():
    auth = FakeAuth()
    calls = []
    def service(request):
        if request.url.path.endswith("models"):
            return httpx.Response(200, json=catalog())
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(401, json={"detail": "authorization context rejected"})
        return httpx.Response(200, text=sse(completed()))
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        translator = OpenAITranslator(auth, "test-first", client=client)
        assert translator.translate("Hello") == "你好"
    assert calls == ["Bearer before-refresh", "Bearer after-refresh"]
    assert auth.refreshes == 1


def test_failed_event_and_incomplete_are_not_success():
    for event, error_class in [
        ({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_user_not_eligible", "message": "workspace restriction"}}}, TranslationPaused),
        ({"type": "response.incomplete", "response": {"status": "incomplete"}}, TranslationError),
    ]:
        def service(request):
            return httpx.Response(200, json=catalog()) if request.url.path.endswith("models") else httpx.Response(200, text=sse(event))
        with httpx.Client(transport=httpx.MockTransport(service)) as client:
            translator = OpenAITranslator(FakeAuth(), "test-first", client=client, max_retries=0)
            with pytest.raises(error_class):
                translator.translate("Hello")


def test_cancellation_and_mock_do_not_use_network():
    event = threading.Event()
    event.set()
    with pytest.raises(TranslationCancelled):
        MockTranslator().translate("Hello", [], event)
    assert MockTranslator().translate("Hello") == "[Mock · 未翻译] Hello"


def test_unverified_model_is_rejected():
    def service(request):
        assert request.url.path.endswith("models")
        return httpx.Response(200, json=catalog())
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        translator = OpenAITranslator(FakeAuth(), "unverified-model", client=client)
        with pytest.raises(TranslationPaused, match="当前账户无法使用所选模型"):
            translator.translate("Hello")
