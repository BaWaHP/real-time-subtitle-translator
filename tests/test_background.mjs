// Run the production service worker with deterministic Chrome API boundaries.
// These tests verify discovery/permission behavior, not browser installation.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import * as parsers from "../edge_extension/parsers.mjs";
import {inspectPageCaptionMetadata} from "../edge_extension/page_probe.js";

const source = fs.readFileSync(new URL("../edge_extension/background.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?\n/gm, "");
const event = () => {
  const listeners = [];
  return {addListener: f => listeners.push(f), removeListener: f => listeners.splice(listeners.indexOf(f), 1),
    fire: (...args) => Promise.all(listeners.map(f => f(...args))), listeners};
};
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

function harness(initialFrames = [{frameId: 0, parentFrameId: -1, url: "https://course.example/lesson", documentId: "top"}], options = {}) {
  let now = 1_000_000, frameTree = initialFrames;
  const granted = new Set(["https://course.example/*"]), failures = new Set();
  const injected = [], bridgeEvents = [], timers = new Map(), stopMessages = [], treeReads = [];
  const saved = {session: {}, local: {}};
  const storage = name => ({get: async () => ({...saved[name]}), set: async value => Object.assign(saved[name], value),
    remove: async key => { delete saved[name][key]; }});
  const chrome = {
    runtime: {id: "a".repeat(32), getURL: path => `chrome-extension://${"a".repeat(32)}/${path}`, onMessage: event(), lastError: undefined},
    storage: {session: storage("session"), local: storage("local")},
    tabs: {get: async id => ({id, url: "https://course.example/lesson"}),
      sendMessage: async (...args) => { stopMessages.push(args); }, onRemoved: event(), onUpdated: event()},
    permissions: {contains: async ({origins}) => origins.every(origin => granted.has(origin)), onAdded: event(), onRemoved: event()},
    webNavigation: {getAllFrames: async ({tabId}) => { treeReads.push(tabId); return frameTree; },
      onCommitted: event(), onCompleted: event(), onDOMContentLoaded: event(), onHistoryStateUpdated: event(), onErrorOccurred: event()},
    scripting: {executeScript: async details => {
      const ids = details.target.frameIds ?? (details.target.documentIds
        ? details.target.documentIds.map(id => frameTree.find(f => f.documentId === id)?.frameId)
        : details.target.allFrames ? frameTree.map(f => f.frameId) : [0]);
      if (details.files) {
        assert.equal(details.target.allFrames, undefined, "Inject each permitted frame independently");
        assert.equal(ids.length, 1, "A failed iframe must not block its siblings");
        if (failures.has(ids[0])) throw new Error("Frame was detached during injection");
        injected.push(ids[0]);
      }
      return await Promise.all(ids.map(async frameId => ({frameId,
        documentId: frameTree.find(f => f.frameId === frameId)?.documentId,
        result: details.world === "MAIN" ? options.metadata ? await options.metadata(details, frameId) : {tracks: []} : []})));
    }}
  };
  let timerId = 0;
  class FakeDate extends Date { static now() { return now; } }
  const context = vm.createContext({...parsers, inspectPageCaptionMetadata, chrome, URL, TextEncoder, TextDecoder,
    AbortSignal, Date: FakeDate, console,
    fetch: async (url, request) => {
      const body = request?.body ? JSON.parse(request.body) : null;
      if (body) bridgeEvents.push(body);
      if (options.fetch) await options.fetch(body, url, request);
      return {ok: true, status: 200, json: async () => ({ok: true})};
    },
    setInterval: (fn, ms) => { const id = ++timerId; timers.set(id, {fn, ms, interval: true}); return id; },
    clearInterval: id => timers.delete(id),
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
    clearTimeout: id => timers.delete(id)});
  vm.runInContext(source, context, {filename: "background.js"});
  const sender = {id: chrome.runtime.id, url: chrome.runtime.getURL("popup.html")};
  const send = (message, from = sender) => new Promise(resolve => chrome.runtime.onMessage.listeners[0](message, from, resolve));
  return {chrome, granted, failures, injected, bridgeEvents, stopMessages, treeReads, send,
    now: () => now, advance: elapsed => { now += elapsed; },
    setFrames: value => { frameTree = value; },
    status: () => send({type: "popup_status"}),
    connect: () => send({type: "popup_connect", tabId: 42, port: 18765, token: "isolated-test-token-not-a-user-credential"}),
    tick: async (elapsed = 3000) => {
      now += elapsed;
      for (const [id, timer] of [...timers]) {
        if (!timers.has(id)) continue;
        if (!timer.interval) timers.delete(id);
        await timer.fn();
      }
      await flush();
    }};
}

const nestedFrames = () => [
  {frameId: 0, parentFrameId: -1, url: "https://course.example/lesson", documentId: "top"},
  {frameId: 7, parentFrameId: 0, url: "https://course.example/wrapper", documentId: "wrapper"},
  {frameId: 11, parentFrameId: 7, url: "https://player.example/actual-redirected-video", documentId: "player"},
];

test("nested actual frame origins are listed before permission; no third-party injection", async () => {
  const h = harness(nestedFrames());
  assert.equal((await h.connect()).ok, true);
  await flush();
  assert.ok(h.treeReads.length > 0);
  assert.ok(h.treeReads.every(id => id === 42));
  assert.ok(h.injected.includes(0) && h.injected.includes(7));
  assert.ok(!h.injected.includes(11));
  const state = await h.status();
  assert.ok(state.pendingOrigins.includes("https://player.example/*"));
});

function playback(h, current_time, values = {}) {
  return h.send({type: "probe_state", document_id: "top", media_id: "media", source_id: "source",
    area: 230400, title: "Lesson", page_url: "https://course.example/lesson", frame_layout: [],
    media_url: "https://media.example/video.mp4", current_time, sampled_at_ms: h.now(), paused: false,
    seeking: false, playback_rate: 1, duration: 861, ended: false, ...values},
  {tab: {id: 42}, frameId: 0, documentId: "top"});
}

test("large caption upload cannot block playback, pause, seek, or rate updates", async () => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  const h = harness(undefined, {fetch: body => body?.type === "captions" ? blocked : undefined});
  await h.connect();
  await playback(h, 10);
  await flush();
  try {
    await h.send({type: "probe_captions", source_id: "source", cues: [{start: 0, end: 20, text: "One subtitle"}]},
      {tab: {id: 42}, frameId: 0, documentId: "top"});
    await flush();
    assert.ok(h.bridgeEvents.some(e => e.type === "captions"));
    for (const values of [{}, {paused: true}, {seeking: true, current_time: 40}, {playback_rate: 2}]) {
      h.advance(250);
      await playback(h, 10.25, values);
      await flush();
    }
    assert.equal(h.bridgeEvents.filter(e => e.type === "state").length, 5);
    assert.ok(h.bridgeEvents.some(e => e.type === "state" && e.paused));
    assert.ok(h.bridgeEvents.some(e => e.type === "state" && e.seeking && e.current_time === 40));
    assert.ok(h.bridgeEvents.some(e => e.type === "state" && e.playback_rate === 2));
  } finally { release(); await flush(); }
});

test("failed playback samples are superseded instead of retried", async () => {
  const h = harness(undefined, {fetch: body => { if (body?.type === "state" && body.current_time === 10) throw new Error("temporary failure"); }});
  await h.connect();
  await playback(h, 10);
  await flush();
  assert.equal(h.bridgeEvents.filter(e => e.type === "state").length, 1);
  h.advance(250);
  await playback(h, 10.25);
  await flush();
  assert.equal(h.bridgeEvents.filter(e => e.type === "state").length, 2);
  assert.equal(h.bridgeEvents.at(-1).current_time, 10.25);
});

test("clock capture timestamp survives browser messaging and old queued samples are skipped", async () => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  const h = harness(undefined, {fetch: body => body?.type === "state" && body.current_time === 10 ? blocked : undefined});
  await h.connect();
  await playback(h, 10, {sampled_at_ms: h.now() - 900});
  await flush();
  assert.equal(h.bridgeEvents.find(e => e.type === "state").sampled_at_ms, h.now() - 900);
  await playback(h, 10.25);
  h.advance(2500);
  release();
  await flush();
  assert.equal(h.bridgeEvents.filter(e => e.type === "state").length, 1);
  await playback(h, 13);
  await flush();
  assert.equal(h.bridgeEvents.filter(e => e.type === "state").at(-1).current_time, 13);
});

test("granting the listed origin starts its nested player and reaches desktop bridge", async () => {
  const h = harness(nestedFrames());
  await h.connect();
  h.granted.add("https://player.example/*");
  assert.equal((await h.send({type: "popup_permissions_granted"})).ok, true);
  await flush();
  assert.ok(h.injected.includes(11));
  assert.ok(!(await h.status()).pendingOrigins.includes("https://player.example/*"));
  const response = await h.send({type: "probe_state", document_id: "player", media_id: "media", source_id: "source",
    area: 640 * 360, title: "Nested lesson", page_url: "https://player.example/actual-redirected-video", frame_layout: [],
    media_url: "https://media.example/video.mp4", current_time: 86, paused: true, seeking: false,
    playback_rate: 1, duration: 861, ended: false}, {tab: {id: 42}, frameId: 11, url: "https://player.example/actual-redirected-video"});
  assert.equal(response.selected, true);
  await flush();
  assert.ok(h.bridgeEvents.some(e => e.type === "state" && e.current_time === 86 && e.session_id === "42:11"));
});

test("one detached authorized frame does not stop a sibling player", async () => {
  const h = harness(nestedFrames());
  h.failures.add(7);
  h.granted.add("https://player.example/*");
  assert.equal((await h.connect()).ok, true);
  await flush();
  assert.ok(h.injected.includes(11));
  assert.equal((await h.status()).connected, true);
});

test("a dynamically added frame is discovered after connecting", async () => {
  const h = harness();
  await h.connect();
  h.setFrames(nestedFrames());
  await h.chrome.webNavigation.onCommitted.fire({tabId: 42, frameId: 11, url: "https://player.example/actual-redirected-video"});
  await h.tick();
  assert.ok((await h.status()).pendingOrigins.includes("https://player.example/*"));
  assert.ok(!h.injected.includes(11));
});

test("search without a video ends in a useful diagnostic and stop rejects future probes", async () => {
  const h = harness();
  await h.connect();
  await h.tick(20_000);
  const state = await h.status();
  assert.notEqual(state.status, "正在查找当前页面视频");
  assert.match(`${state.status} ${state.warning}`, /未发现|未找到|没有|读取|框架|播放器/);
  await h.send({type: "popup_stop"});
  assert.equal((await h.status()).connected, false);
  assert.equal((await h.send({type: "probe_absent", frame_layout: []}, {tab: {id: 42}, frameId: 0})).stop, true);
  assert.ok(h.stopMessages.length);
});

test("a live probe keeps its media identity without periodic reinjection", async () => {
  const h = harness();
  await h.connect();
  const count = h.injected.length;
  // Browsers issue heartbeats for documents without a video too. A normal
  // discovery cycle must not restart them and reset the selected source IDs.
  for (let i = 0; i < 8; i++) {
    await h.send({type: "probe_absent", document_id: "opaque-probe-id", page_url: "https://course.example/lesson", frame_layout: []},
      {tab: {id: 42}, frameId: 0, documentId: "top"});
    await h.tick(1000);
  }
  assert.equal(h.injected.length, count);
});

test("navigation rejects late messages from the previous browser document", async () => {
  const h = harness();
  await h.connect();
  h.setFrames([{frameId: 0, parentFrameId: -1, url: "https://course.example/other", documentId: "replacement"}]);
  await h.chrome.webNavigation.onCommitted.fire({tabId: 42, frameId: 0});
  await h.tick();
  const reply = await h.send({type: "probe_absent", frame_layout: []}, {tab: {id: 42}, frameId: 0, documentId: "top"});
  assert.equal(reply.stop, true);
});

test("unrelated tab navigation is never inspected and its messages are rejected", async () => {
  const h = harness();
  await h.connect();
  const reads = h.treeReads.length;
  await h.chrome.webNavigation.onCommitted.fire({tabId: 99, frameId: 0, url: "https://private.example/"});
  await flush();
  assert.equal(h.treeReads.length, reads);
  const reply = await h.send({type: "probe_absent", frame_layout: []}, {tab: {id: 99}, frameId: 0});
  assert.equal(reply.stop, true);
});

test("removed iframe domains disappear from the pending permission list", async () => {
  const h = harness(nestedFrames());
  await h.connect();
  assert.ok((await h.status()).pendingOrigins.includes("https://player.example/*"));
  h.setFrames(nestedFrames().slice(0, 2));
  await h.tick();
  assert.ok(!(await h.status()).pendingOrigins.includes("https://player.example/*"));
});

test("revoking a used website grant stops the connection and rejects its probes", async () => {
  const h = harness();
  await h.connect();
  h.granted.delete("https://course.example/*");
  await h.chrome.permissions.onRemoved.fire({origins: ["https://course.example/*"]});
  await flush();
  const state = await h.status();
  assert.equal(state.connected, false);
  assert.match(state.status, /权限已移除/);
  assert.ok(h.stopMessages.length);
  assert.equal((await h.send({type: "probe_absent"}, {tab: {id: 42}, frameId: 0})).stop, true);
});

test("Panopto metadata captions reach the bridge once and expose only safe diagnostics", async () => {
  let requests = 0;
  const h = harness(undefined, {metadata: async details => {
    requests++;
    assert.equal(details.args[0].allow_default_english, false);
    return {provider: "panopto", label: "Panopto English", cues: [{start: 0, end: 4, text: "Caption from signed-in page"}]};
  }});
  await h.connect();
  const page_url = "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=fixture&secret=not-for-diagnostics";
  await playback(h, 1, {page_url, caption_diagnostics: {
    native_tracks: 0, english_tracks: 0, readable_tracks: 0, cue_count: 0, blocked_tracks: 0,
    raw_cookie: "never copied", unbounded: 9999999999,
  }});
  await flush();
  assert.ok(h.bridgeEvents.some(body => body.type === "captions" && body.cues[0].text === "Caption from signed-in page"));
  const diagnosed = await h.status();
  assert.deepEqual(JSON.parse(JSON.stringify(diagnosed.captionDiagnostics)), {
    native_tracks: 0, english_tracks: 0, readable_tracks: 0, cue_count: 0, blocked_tracks: 0,
  });
  assert.equal(diagnosed.providerCueCount, 1);
  h.advance(3500);
  await playback(h, 2, {page_url});
  await flush();
  assert.equal(requests, 1, "Full Panopto captions are not fetched and reposted on every heartbeat");
  const state = await h.status();
  assert.equal(state.sourceHost, "demo.hosted.panopto.com");
  assert.ok(!state.sourceHost.includes("secret"));
});

test("default-English confirmation is tied to one source and revocation stops it", async () => {
  const h = harness();
  await h.connect();
  await playback(h, 1, {page_url: "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=fixture"});
  await h.send({type: "popup_panopto_language", enabled: true});
  assert.equal((await h.status()).panoptoDefaultEnglish, true);
  await playback(h, 1, {source_id: "another-video", page_url: "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=other"});
  assert.equal((await h.status()).panoptoDefaultEnglish, false, "An unlabelled new language needs its own confirmation");
  await h.send({type: "popup_panopto_language", enabled: true});
  assert.equal((await h.status()).panoptoDefaultEnglish, true);
  await h.send({type: "popup_panopto_language", enabled: false});
  assert.equal((await h.status()).connected, false);
});
