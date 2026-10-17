import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../edge_extension/content.js", import.meta.url), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));
const plain = value => JSON.parse(JSON.stringify(value));

function element(kind, extra = {}) {
  const listeners = new Map();
  return {
    kind, isConnected: true, src: "", style: {display: "block", visibility: "visible", opacity: "1"},
    getBoundingClientRect: () => ({left: 0, top: 0, right: 640, bottom: 360}),
    hasAttribute: () => false,
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type, fn) { if (listeners.get(type) === fn) listeners.delete(type); },
    listeners, ...extra,
  };
}
function video(extra = {}) {
  return element("video", {
    currentSrc: "https://media.example.org/lesson.mp4", currentTime: 12, paused: true,
    seeking: false, playbackRate: 1.5, duration: 60, ended: false, textTracks: [],
    querySelectorAll: () => [], ...extra,
  });
}
function root(elements = [], extra = {}) {
  return {
    title: "Embedded lesson",
    querySelectorAll(selector) { return selector === "*" ? elements : elements.filter(item => item.kind === selector); },
    ...extra,
  };
}
async function harness(document, {reply = () => ({selected: false}), crypto} = {}) {
  let now = 10000;
  const messages = [], timers = new Set(), runtimeListeners = new Set();
  const context = vm.createContext({
    document, crypto, Uint32Array, URL, innerWidth: 1280, innerHeight: 720,
    location: {href: "http://course.example.org/lesson"},
    Date: class extends Date { static now() { return now; } },
    getComputedStyle: item => item.style,
    setInterval(callback) { timers.add(callback); return callback; },
    clearInterval(callback) { timers.delete(callback); },
    chrome: {runtime: {
      async sendMessage(message) { messages.push(plain(message)); return reply(message); },
      onMessage: {addListener: fn => runtimeListeners.add(fn), removeListener: fn => runtimeListeners.delete(fn)},
    }},
  });
  vm.runInContext(source, context);
  await settle();
  return {
    messages, timers, runtimeListeners, context,
    async tick(ms = 250) { now += ms; for (const callback of [...timers]) callback(); await settle(); },
    async reinject() { vm.runInContext(source, context); await settle(); },
    stop() { context.__subtitleEdgeProbe.stop(); },
  };
}

test("HTTP/insecure pages work when crypto.randomUUID and crypto are unavailable", async () => {
  const probe = await harness(root([video()]));
  const state = probe.messages.find(message => message.type === "probe_state");
  assert.ok(state?.media_id);
  assert.equal(state.current_time, 12);
  assert.equal(state.paused, true);
  assert.equal(state.playback_rate, 1.5);
  assert.equal(probe.timers.size, 1);
  probe.stop();
});

test("fallback uses getRandomValues when UUID support is missing", async () => {
  const probe = await harness(root([video()]), {crypto: {getRandomValues: array => array.fill(42)}});
  assert.match(probe.messages[0].media_id, /^0000002a0000002a0000002a0000002a-/);
  probe.stop();
});

test("Panopto start-link changes preserve cue identity while a new delivery changes it", async () => {
  const probe = await harness(root([video()]));
  probe.context.location.href = "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=11111111-1111-4111-8111-111111111111&start=60";
  await probe.tick();
  const source = probe.messages.filter(message => message.type === "probe_state").at(-1).source_id;
  probe.context.location.href = probe.context.location.href.replace("start=60", "start=120");
  await probe.tick();
  assert.equal(probe.messages.filter(message => message.type === "probe_state").at(-1).source_id, source);
  probe.context.location.href = probe.context.location.href.replace("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222");
  await probe.tick();
  assert.notEqual(probe.messages.filter(message => message.type === "probe_state").at(-1).source_id, source);
  probe.stop();
});

test("open shadow roots expose video and iframe layouts including blank/srcdoc frames", async () => {
  const nested = root([
    video(),
    element("iframe", {src: "https://player.example.org/embed"}),
    element("iframe"),
    element("iframe", {hasAttribute: name => name === "srcdoc"}),
  ]);
  const probe = await harness(root([element("player-component", {shadowRoot: nested})]));
  const state = probe.messages.find(message => message.type === "probe_state");
  assert.deepEqual(state.frame_layout, [
    {url: "https://player.example.org/embed", area: 230400},
    {url: "about:blank", area: 230400},
    {url: "about:srcdoc", area: 230400},
  ]);
  probe.stop();
});

test("absent-video reports include shadow iframe layouts and obey a stop reply", async () => {
  const probe = await harness(root([element("component", {shadowRoot: root([element("iframe", {src: "https://embed.example.org/lesson"})])})]),
    {reply: () => ({stop: true})});
  assert.equal(probe.messages.length, 1);
  assert.equal(probe.messages[0].type, "probe_absent");
  assert.equal(probe.messages[0].frame_layout[0].url, "https://embed.example.org/lesson");
  assert.equal(probe.timers.size, 0);
  assert.equal(probe.runtimeListeners.size, 0);
  await probe.tick();
  assert.equal(probe.messages.length, 1);
});

test("a failed DOM scan reports bounded diagnostics and recovers on a later tick", async () => {
  let broken = true;
  const healthy = root([video()]);
  const probe = await harness(root([], {querySelectorAll(selector) {
    if (broken) throw new TypeError("private page data must not be reported");
    return healthy.querySelectorAll(selector);
  }}));
  assert.equal(probe.messages[0].type, "probe_diagnostic");
  assert.match(probe.messages[0].message, /TypeError/);
  assert.ok(!JSON.stringify(probe.messages).includes("private page data"));
  await probe.tick();
  assert.equal(probe.messages.length, 1);
  await probe.tick(5000);
  assert.equal(probe.messages.filter(message => message.type === "probe_diagnostic").length, 2);
  broken = false;
  await probe.tick();
  assert.ok(probe.messages.some(message => message.type === "probe_state"));
  probe.stop();
});

test("a detached frame cannot hide another valid video or iframe", async () => {
  const probe = await harness(root([
    element("iframe", {getBoundingClientRect() { throw new Error("detached"); }}),
    video(),
    element("iframe", {src: "https://player.example.org/valid"}),
  ]));
  assert.equal(probe.messages[0].type, "probe_state");
  assert.equal(probe.messages[0].frame_layout[0].area, 0);
  assert.equal(probe.messages[0].frame_layout[1].area, 230400);
  probe.stop();
});

test("native English cues remain deduplicated and changed sources get a new identity", async () => {
  const track = {language: "en", label: "English", mode: "disabled", cues: [{startTime: 1, endTime: 3, text: "<b>Hello</b> there"}]};
  const media = video({textTracks: [track]});
  const probe = await harness(root([media]), {reply: () => ({selected: true})});
  assert.equal(track.mode, "hidden");
  assert.deepEqual(probe.messages.find(message => message.type === "probe_captions").cues, [{start: 1, end: 3, text: "Hello there"}]);
  await probe.tick(1600);
  assert.equal(probe.messages.filter(message => message.type === "probe_captions").length, 1);
  const oldSource = probe.messages[0].source_id;
  media.listeners.get("loadedmetadata")();
  await probe.tick();
  assert.notEqual(probe.messages.filter(message => message.type === "probe_state").at(-1).source_id, oldSource);
  assert.equal(probe.messages.filter(message => message.type === "probe_captions").length, 2);
  probe.stop();
  assert.equal(track.mode, "disabled");
});

test("course native captions accept eng and language-less English labels, excluding metadata and conflicting languages", async () => {
  for (const candidate of [
    {language: "eng", label: "Original"}, {language: "", label: "English (United States)"},
  ]) {
    const track = {...candidate, kind: "captions", mode: "disabled", cues: [{startTime: 1, endTime: 3, text: "Course caption"}]};
    const ignored = [
      {language: "en", kind: "metadata", label: "Metadata", mode: "disabled", cues: [{startTime: 1, endTime: 3, text: "not a caption"}]},
      {language: "fr", kind: "captions", label: "English", mode: "showing", cues: [{startTime: 1, endTime: 3, text: "French"}]},
      {language: "", kind: "captions", label: "Default", mode: "disabled", cues: []},
    ];
    const probe = await harness(root([video({textTracks: [...ignored, track]})]), {reply: () => ({selected: true})});
    assert.equal(probe.messages.find(message => message.type === "probe_captions").cues[0].text, "Course caption");
    assert.equal(track.mode, "hidden");
    assert.deepEqual(ignored.map(item => item.mode), ["disabled", "showing", "disabled"]);
    assert.deepEqual(probe.messages[0].caption_diagnostics, {native_tracks: 4, english_tracks: 1, readable_tracks: 1, cue_count: 1, blocked_tracks: 0});
    probe.stop();
    assert.equal(track.mode, "disabled");
  }
});

test("an empty or protected preferred track does not hide another readable English track", async () => {
  const blocked = {language: "en", kind: "captions", mode: "disabled", label: "English manual", get cues() { throw new DOMException("secret signed URL", "SecurityError"); }};
  const empty = {language: "en", kind: "captions", mode: "disabled", label: "English alternate", cues: []};
  const readable = {language: "en", kind: "captions", mode: "showing", label: "English auto", cues: [{startTime: 2, endTime: 4, text: "Readable English"}]};
  const probe = await harness(root([video({textTracks: [blocked, empty, readable]})]), {reply: () => ({selected: true})});
  assert.equal(probe.messages.find(message => message.type === "probe_captions").cues[0].text, "Readable English");
  assert.deepEqual(probe.messages[0].caption_diagnostics, {native_tracks: 3, english_tracks: 3, readable_tracks: 1, cue_count: 1, blocked_tracks: 1});
  assert.ok(!JSON.stringify(probe.messages).includes("secret signed URL"));
  assert.equal(readable.mode, "showing");
  probe.stop();
  assert.equal(blocked.mode, "disabled");
  assert.equal(empty.mode, "disabled");
  assert.equal(readable.mode, "showing");
});

test("unknown-language captions are diagnosed but never enabled or transmitted as English", async () => {
  const track = {language: "", kind: "captions", mode: "showing", label: "Default", cues: [{startTime: 1, endTime: 4, text: "Unknown language"}]};
  const probe = await harness(root([video({textTracks: [track]})]), {reply: () => ({selected: true})});
  assert.equal(probe.messages.filter(message => message.type === "probe_captions").length, 0);
  assert.deepEqual(probe.messages[0].caption_diagnostics, {native_tracks: 1, english_tracks: 0, readable_tracks: 0, cue_count: 0, blocked_tracks: 0});
  assert.equal(track.mode, "showing");
  probe.stop();
});

test("reinjection and explicit stop clean up timers, listeners, and track changes", async () => {
  const track = {language: "en", label: "English", mode: "disabled", cues: []};
  const media = video({textTracks: [track]});
  const probe = await harness(root([media]), {reply: () => ({selected: true})});
  await probe.reinject();
  assert.equal(probe.timers.size, 1);
  assert.equal(probe.runtimeListeners.size, 1);
  assert.equal(media.listeners.size, 9);
  for (const listener of [...probe.runtimeListeners]) listener({type: "stop_probe"}, {}, () => {});
  assert.equal(probe.timers.size, 0);
  assert.equal(probe.runtimeListeners.size, 0);
  assert.equal(media.listeners.size, 0);
  assert.equal(track.mode, "disabled");
});

test("pause seek and rate events send an immediate timestamped clock sample", async () => {
  const media = video({readyState: 4});
  const probe = await harness(root([media]));
  assert.equal(probe.messages[0].sampled_at_ms, 10000);
  assert.equal(probe.messages[0].buffering, false);
  media.currentTime = 35;
  media.seeking = true;
  media.listeners.get("seeking")();
  await settle();
  assert.equal(probe.messages.at(-1).current_time, 35);
  assert.equal(probe.messages.at(-1).seeking, true);
  media.seeking = false;
  media.playbackRate = 2;
  media.listeners.get("ratechange")();
  await settle();
  assert.equal(probe.messages.at(-1).playback_rate, 2);
  media.readyState = 2;
  media.listeners.get("waiting")();
  await settle();
  assert.equal(probe.messages.at(-1).buffering, true);
  probe.stop();
  assert.equal(media.listeners.size, 0);
});
