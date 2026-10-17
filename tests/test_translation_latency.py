"""Network boundaries only: no user account or external inference is used."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from translator.openai_translator import OpenAITranslator, TranslationCancelled, TranslationError


class FakeAuth:
    active_account_id = "latency-fixture-account"

    def access_token(self, force_refresh=False):
        return "synthetic-test-access-token"


def completed(text):
    return "data: " + json.dumps({"type": "response.completed", "response": {
        "status": "completed", "output": [{"content": [{"type": "output_text", "text": text}]}]
    }}, ensure_ascii=False) + "\n\n"


def translator_for(service, **kwargs):
    def models(_request):
        return httpx.Response(200, json={"models": [{"slug": "fixture-model", "visibility": "list"}]})
    # Both boundaries are synthetic; catalogue and Responses have different
    # paths but always carry an OAuth Bearer rather than an API key.
    def route(request):
        return models(request) if request.url.path.endswith("/models") else service(request)
    client = httpx.Client(transport=httpx.MockTransport(route))
    return OpenAITranslator(FakeAuth(), "fixture-model", client=client, max_retries=0, **kwargs), client


def test_batch_translates_many_cues_in_one_supported_responses_request():
    requests = []
    def service(request):
        body = json.loads(request.content)
        requests.append(body)
        assert set(body) == {"model", "input", "instructions", "store", "stream"}
        assert body["store"] is False and body["stream"] is True
        values = json.loads(body["input"][0]["content"])
        assert values["previous_english_for_context_only"] == ["earlier sentence"]
        assert values["current_english_segments"] == [{"id": 0, "text": "first"}, {"id": 1, "text": "second"}]
        return httpx.Response(200, text=completed(json.dumps([
            {"id": 0, "translation": "第一句"}, {"id": 1, "translation": "第二句"}])))
    translator, client = translator_for(service)
    try:
        assert translator.translate_batch(["first", "second"], ["earlier sentence"]) == ["第一句", "第二句"]
        assert len(requests) == 1
    finally:
        translator.close()
        client.close()


@pytest.mark.parametrize("result", [
    "not JSON", '[{"id":0,"translation":"一"}]',
    '[{"id":1,"translation":"二"},{"id":0,"translation":"一"}]',
    '[{"id":0,"translation":"一"},{"id":0,"translation":"二"}]',
    '[{"id":0,"translation":"一"},{"id":1,"translation":""}]',
])
def test_batch_refuses_missing_reordered_duplicate_or_empty_subtitles(result):
    translator, client = translator_for(lambda _request: httpx.Response(200, text=completed(result)))
    try:
        with pytest.raises(TranslationError) as raised:
            translator.translate_batch(["first", "second"])
        assert raised.value.code == "invalid_subtitle_batch"
    finally:
        translator.close()
        client.close()


class IdleStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.started = threading.Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.sleep(30)
        yield b"never reached"

    async def aclose(self):
        self.closed = True


def test_idle_sse_is_cancelled_promptly_and_the_next_request_can_run():
    idle = IdleStream()
    calls = []
    def service(_request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, stream=idle)
        return httpx.Response(200, text=completed("新的字幕"))
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(service))
    translator, client = translator_for(service, async_client=async_client)
    cancel = threading.Event()
    def cancel_after_request_starts():
        if idle.started.wait(2):
            cancel.set()
    canceller = threading.Thread(target=cancel_after_request_starts)
    canceller.start()
    start = time.monotonic()
    try:
        with pytest.raises(TranslationCancelled):
            translator.translate("old sentence", cancel_event=cancel)
        assert time.monotonic() - start < 1.0
        assert idle.closed
        assert translator.translate("new sentence") == "新的字幕"
        assert len(calls) == 2
    finally:
        canceller.join(2)
        translator.close()
        client.close()
        asyncio.run(async_client.aclose())


def test_async_partial_stream_still_requires_completed_event():
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(
        200, text='data: {"type":"response.output_text.delta","delta":"不完整"}\n\n')))
    translator, client = translator_for(lambda _request: None, async_client=async_client)
    try:
        with pytest.raises(TranslationError, match="未收到完整译文"):
            translator.translate("source")
    finally:
        translator.close()
        client.close()
        asyncio.run(async_client.aclose())
