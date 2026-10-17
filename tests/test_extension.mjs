import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {cleanText, parseTime, parseVtt, parseTimedXml, parseYouTubeJson, parseCaptionDocument, dedupeRollingCues, chooseEnglishTrack, isEnglishCaptionTrack, permissionOrigin, pageOrigin, normalizeCues} from "../edge_extension/parsers.mjs";

test("VTT accepts cue IDs, settings, multiline, timestamps and escaped text", () => {
  const cues = parseVtt("WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.500 align:start\n<c>Tom &amp; Jerry</c>\nare here.\n\n00:01:00.000 --> 00:01:01.000\nAgain\n");
  assert.deepEqual(cues, [{start: 1, end: 3.5, text: "Tom & Jerry are here."}, {start: 60, end: 61, text: "Again"}]);
});
test("parser rejects invalid windows and deduplicates only identical time windows", () => {
  assert.deepEqual(normalizeCues([{start: 0, end: 1, text: "a"}, {start: 0, end: 1, text: "a"}, {start: 3, end: 4, text: "a"}, {start: 4, end: 1, text: "bad"}, {start: NaN, end: 1, text: "bad"}]), [{start: 0, end: 1, text: "a"}, {start: 3, end: 4, text: "a"}]);
  assert.ok(Number.isNaN(parseTime("bad")));
  assert.equal(parseTime("02:03,250"), 123.25);
});
test("json3 joins word spans and skips non-text windows", () => {
  assert.deepEqual(parseYouTubeJson({events: [{tStartMs: 1000, dDurationMs: 2000, segs: [{utf8: "hello "}, {utf8: "world"}]}, {tStartMs: 1}]}), [{start: 1, end: 3, text: "hello world"}]);
});
test("legacy and srv3 timedtext both parse", () => {
  assert.deepEqual(parseTimedXml('<transcript><text start="1.2" dur="2">A &quot;B&quot;</text></transcript>'), [{start: 1.2, end: 3.2, text: 'A "B"'}]);
  assert.deepEqual(parseTimedXml('<timedtext><p t="1200" d="800"><s>Word</s></p></timedtext>'), [{start: 1.2, end: 2, text: "Word"}]);
});
test("rolling ASR windows remove overlap while later speech repetitions survive", () => {
  const result = dedupeRollingCues([{start: 0, end: 3, text: "we need to go"}, {start: 2, end: 5, text: "to go now"}, {start: 8, end: 10, text: "we need to go"}]);
  assert.deepEqual(result, [{start: 0, end: 5, text: "we need to go now"}, {start: 8, end: 10, text: "we need to go"}]);
});
test("rolling unrelated lines cannot erase previous text", () => {
  assert.deepEqual(dedupeRollingCues([{start: 0, end: 3, text: "one thing"}, {start: 2, end: 5, text: "another thing"}]), [{start: 0, end: 2, text: "one thing"}, {start: 2, end: 5, text: "another thing"}]);
});
test("continuous rolling speech is split into bounded translation units", () => {
  const cues = Array.from({length: 20}, (_, i) => ({start: i * 2, end: i * 2 + 3, text: `word${i} word${i + 1}`}));
  const result = dedupeRollingCues(cues);
  assert.ok(result.length >= 5);
  assert.ok(result.every(c => c.end - c.start <= 8));
  assert.ok(result.map(c => c.text).join(" ").includes("word20"));
});
test("manual English subtitles preferred over automatic and other languages", () => {
  const tracks = [{languageCode: "en", kind: "asr"}, {languageCode: "fr"}, {languageCode: "en-US", kind: ""}];
  assert.equal(chooseEnglishTrack(tracks), tracks[2]);
  assert.equal(chooseEnglishTrack([{languageCode: "zh"}]), null);
});
test("English course captions accept eng or missing-language English labels without guessing", () => {
  for (const track of [
    {language: "eng", kind: "captions"}, {languageCode: " eng-US ", kind: "subtitles"},
    {srclang: "", label: "English (United States)", kind: "captions"},
    {languageCode: "", language: "en_US", label: "captions"},
  ]) assert.equal(isEnglishCaptionTrack(track), true);
  for (const track of [
    {language: "fr", label: "English", kind: "captions"}, {label: "Unspecified", kind: "subtitles"},
    {label: "French / English", kind: "captions"}, {language: "en", kind: "metadata"},
    {language: "eng", kind: "descriptions"}, {language: "en", kind: "chapters"},
    {language: "englishless", kind: "captions"},
  ]) assert.equal(isEnglishCaptionTrack(track), false);
});
test("permissions are exact origins and reject local or credentialed remote URLs", () => {
  assert.equal(permissionOrigin("https://www.youtube.com/api/timedtext?secret=1"), "https://www.youtube.com/*");
  for (const url of ["file:///x", "data:text/plain,x", "http://127.0.0.1/a", "http://localhost/a", "http://192.168.1.1/a", "http://[::1]/a", "http://server.local/a", "https://u:p@example.com/a"]) assert.equal(permissionOrigin(url), null);
});
test("explicit page selection supports local pages without granting private caption fetch", () => {
  assert.equal(pageOrigin("http://127.0.0.1:18766/index.html"), "http://127.0.0.1/*");
  assert.equal(permissionOrigin("http://127.0.0.1:18766/caption.vtt"), null);
  assert.equal(pageOrigin("edge://extensions"), null);
});
test("oversized caption timelines fail explicitly instead of truncating", () => {
  assert.throws(() => normalizeCues(Array.from({length: 20001}, (_, i) => ({start: i, end: i + 1, text: "a"}))), /20000/);
});
test("text cleanup never evaluates subtitle content", () => {
  assert.equal(cleanText('<script>alert(1)</script> A &#x1f600; &nbsp; &lt;word&gt;'), "alert(1) A 😀 <word>");
  assert.equal(cleanText("&#99999999;"), "");
});
test("format dispatcher detects JSON, XML, VTT", () => {
  assert.equal(parseCaptionDocument('{"events":[]}').length, 0);
  assert.equal(parseCaptionDocument('<text start="0" dur="1">x</text>')[0].text, "x");
  assert.equal(parseCaptionDocument("WEBVTT\n\n00:00.000 --> 00:01.000\nx")[0].text, "x");
});
test("manifest has no default remote host access or externally-connectable pages", () => {
  const manifest = JSON.parse(fs.readFileSync(new URL("../edge_extension/manifest.json", import.meta.url), "utf8"));
  assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1/*"]);
  assert.ok(manifest.permissions.includes("activeTab"));
  assert.equal(manifest.externally_connectable, undefined);
  assert.equal(manifest.content_scripts, undefined);
});
test("page metadata probe refuses stale YouTube SPA player response", async () => {
  const {inspectPageCaptionMetadata} = await import("../edge_extension/page_probe.js");
  const context = vm.createContext({URL, location: {href: "https://www.youtube.com/watch?v=current"}, document: {querySelector: () => ({getPlayerResponse: () => ({videoDetails: {videoId: "old"}, captions: {playerCaptionsTracklistRenderer: {captionTracks: [{baseUrl: "https://example.com/old"}]}}})})}});
  const result = vm.runInContext(`(${inspectPageCaptionMetadata.toString()})()`, context);
  assert.equal(result.pending, true);
  assert.equal(result.tracks.length, 0);
});
