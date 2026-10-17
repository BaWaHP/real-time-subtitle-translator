import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import {inspectPageCaptionMetadata} from "../edge_extension/page_probe.js";

const id = "11111111-1111-4111-8111-111111111111";
const page = `https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=${id}&start=60`;
const media = "blob:https://demo.hosted.panopto.com/example";
const plain = value => JSON.parse(JSON.stringify(value));

function harness({languages = [{language: 0}], selected = {language: 0}, responses = [], fetcher,
  href = page, duration = 90, mediaUrl = media, globals = {}} = {}) {
  const calls = [];
  const location = {href};
  const context = vm.createContext({URL, URLSearchParams, AbortController, TextEncoder, TextDecoder,
    setTimeout, clearTimeout, location, document: {
      querySelector: () => null,
      querySelectorAll: () => [{currentSrc: mediaUrl, duration}],
    }, PanoptoViewer: {Language: {getCaptionLanguages: () => languages,
      getCurrentCaptionLanguage: () => selected}},
    fetch: async (url, options) => {
      calls.push({url, options, fields: Object.fromEntries(options.body)});
      if (fetcher) return fetcher(url, options);
      const next = responses.shift();
      if (next instanceof Response) return next;
      if (next === undefined) throw new Error("Unexpected request");
      return new Response(JSON.stringify(next), {headers: {"Content-Type": "application/json"}});
    }, ...globals});
  return {calls, location, context, run: args => vm.runInContext(
    `(${inspectPageCaptionMetadata.toString()})(${JSON.stringify(args || {})})`, context)};
}

test("Panopto English captions use the current frame session, form POST and existing browser login", async () => {
  const h = harness({responses: [[{Time: 1, CaptionDuration: 2, Data: "Hello"},
    {Time: 4, Duration: 3, Caption: "World"}]]});
  const result = await h.run({page_url: page, media_url: media});
  assert.equal(result.provider, "panopto");
  assert.equal(result.video_id, id);
  assert.deepEqual(plain(result.cues), [{start: 1, end: 3, text: "Hello"}, {start: 4, end: 7, text: "World"}]);
  assert.equal(h.calls.length, 1);
  const {url, options, fields} = h.calls[0];
  assert.equal(url, "https://demo.hosted.panopto.com/Panopto/Pages/Viewer/DeliveryInfo.aspx");
  assert.equal(options.method, "POST");
  assert.equal(options.credentials, "same-origin");
  assert.equal(options.redirect, "error");
  assert.equal(options.signal instanceof AbortSignal, true);
  assert.deepEqual(fields, {deliveryId: id, responseType: "json", getCaptions: "true", language: "0"});
  assert.deepEqual(Object.keys(options.headers), ["Accept"]);
});

test("Panopto metadata discovers explicit English language and resolves missing cue durations", async () => {
  const h = harness({languages: [], duration: 0, responses: [
    {Delivery: {Duration: 20, AvailableCaptions: [{Language: 5}, {Language: 1}]}},
    [{Time: 10, Caption: "Second"}, {Time: 2, Data: "First"}, {Time: null, Caption: "Invalid"}],
  ]});
  const result = await h.run();
  assert.deepEqual(plain(result.cues), [{start: 2, end: 10, text: "First"}, {start: 10, end: 20, text: "Second"}]);
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].fields.language, "1");
  assert.equal(h.calls[0].fields.getCaptions, undefined);
});

test("Panopto current language is preferred only if it is actually available", async () => {
  const h = harness({languages: [{language: 0}, {language: 15}], selected: {language: 15},
    responses: [[{Time: 0, CaptionDuration: 2, Caption: "Australia"}]]});
  const result = await h.run();
  assert.equal(h.calls[0].fields.language, "15");
  assert.match(result.label, /en-AU/);
});

test("an empty explicitly English track does not hide another English track", async () => {
  const h = harness({languages: [{language: 0}, {language: 1}], responses: [[], [{Time: 0, Caption: "British"}]]});
  const result = await h.run();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].fields.language, "1");
  assert.equal(result.cues[0].text, "British");
});

test("default or unknown captions are never silently interpreted as English", async () => {
  const h = harness({languages: [{language: undefined}], selected: {language: undefined},
    responses: [{Delivery: {AvailableCaptions: [{Language: null}], HasCaptions: true}}]});
  const result = await h.run();
  assert.equal(result.default_available, true);
  assert.match(result.warning, /默认字幕是英语/);
  assert.equal(result.cues.length, 0);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].fields.getCaptions, undefined);
});

test("explicit confirmation reads the unlabelled track with official empty-language semantics", async () => {
  const h = harness({languages: [{language: undefined}], selected: {language: undefined}, responses: [
    {Delivery: {AvailableCaptions: [{Language: null}]}},
    [{Time: 1, CaptionDuration: 2, Caption: "Confirmed English"}],
  ]});
  const result = await h.run({allow_default_english: true});
  assert.equal(result.cues[0].text, "Confirmed English");
  assert.equal(h.calls[1].fields.language, "");
  assert.match(result.label, /用户确认为英语/);
});

test("current-menu default English alone cannot prove that an English track exists", async () => {
  const h = harness({languages: [], selected: {language: 0}, responses: [
    {Delivery: {AvailableLanguages: [5, 8]}},
  ]});
  const result = await h.run();
  assert.equal(result.cues.length, 0);
  assert.match(result.warning, /未提供明确标记为英语/);
  assert.equal(h.calls.length, 1);
});

test("Panopto non-English track is not made English by user confirmation", async () => {
  const h = harness({languages: [{language: 5}], selected: {language: 5}, responses: [
    {Delivery: {AvailableCaptions: [{Language: 5}]}},
  ]});
  const result = await h.run({allow_default_english: true});
  assert.equal(result.cues.length, 0);
  assert.equal(h.calls.length, 1);
});

test("stale source is rejected before any request and after a delayed response", async () => {
  const before = harness();
  const a = await before.run({page_url: page.replace(id, "00000000-0000-0000-0000-000000000000")});
  assert.equal(a.pending, true);
  assert.equal(before.calls.length, 0);
  const after = harness({fetcher: () => {
    after.location.href = page.replace(id, "00000000-0000-0000-0000-000000000000");
    return new Response(JSON.stringify([{Time: 1, Duration: 2, Caption: "Stale"}]));
  }});
  const b = await after.run({page_url: page, media_url: media});
  assert.equal(b.pending, true);
  assert.equal(b.cues.length, 0);
});

test("login errors and login HTML are diagnosed without exporting response or credentials", async () => {
  for (const response of [new Response("denied", {status: 403}), {ErrorCode: 2},
    new Response("<!DOCTYPE html><html>SSO login</html>"), new Response("garbage")]) {
    const h = harness({responses: [response]});
    const result = await h.run();
    assert.equal(result.cues.length, 0);
    assert.match(result.warning, /登录|格式无法识别/);
    assert.doesNotMatch(JSON.stringify(result), /SSO login|denied|garbage|ASPXAUTH/);
  }
});

test("caption bytes and cue count are bounded with an explicit failure", async () => {
  const bytes = harness({responses: [new Response("[]", {headers: {"Content-Length": String(4 * 1024 * 1024 + 1)}})]});
  assert.match((await bytes.run()).warning, /4 MiB/);
  const cues = harness({responses: [Array.from({length: 20001}, (_, Time) => ({Time, Caption: "x"}))]});
  assert.match((await cues.run()).warning, /20000/);
});

test("a hung Panopto request is aborted and diagnosed within the shared budget", async () => {
  const h = harness({fetcher: (_, options) => new Promise((_, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
  }), globals: {setTimeout: callback => setTimeout(callback, 5)}});
  const result = await h.run();
  assert.match(result.warning, /超过 6 秒/);
});

test("non-Panopto pages never use this provider, including fake Panopto host suffixes", () => {
  for (const href of [page.replace("demo.hosted.panopto.com", "panopto.com.attacker.invalid"),
    page.replace("Viewer.aspx", "Auth/Login.aspx"), page.replace(id, "not-a-uuid")]) {
    const h = harness({href});
    const result = h.run();
    assert.equal(result instanceof Promise, false);
    assert.equal(result.provider, undefined);
    assert.equal(h.calls.length, 0);
  }
});
