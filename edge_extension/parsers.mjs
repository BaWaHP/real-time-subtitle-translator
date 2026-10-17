// Caption parsers shared with the service worker.
export function cleanText(value) {
  return String(value ?? "")
    .replace(/<br\s*\/?\s*>/gi, " ")
    .replace(/<[^>]*>/g, "")
    .replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (_, entity) => {
      const named = {amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " "};
      if (entity[0] !== "#") return named[entity.toLowerCase()] ?? "";
      const n = entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : parseInt(entity.slice(1), 10);
      return n > 0 && n <= 0x10ffff && !(n >= 0xd800 && n <= 0xdfff) ? String.fromCodePoint(n) : "";
    })
    .replace(/[\u200b\u200e\u200f\ufeff]/g, "")
    .replace(/\s+/g, " ").trim();
}

export function parseTime(value) {
  const parts = String(value).trim().replace(",", ".").split(":").map(Number);
  if (parts.length < 2 || parts.length > 3 || parts.some(x => !Number.isFinite(x) || x < 0)) return NaN;
  return parts.reduce((total, x) => total * 60 + x, 0);
}

export function normalizeCues(items) {
  const cues = [];
  const seen = new Set();
  for (const item of items ?? []) {
    const start = Number(item.start), end = Number(item.end), text = cleanText(item.text);
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || !text) continue;
    const key = `${start.toFixed(3)}:${end.toFixed(3)}:${text}`;
    if (!seen.has(key)) { seen.add(key); cues.push({start, end, text}); }
    if (cues.length > 20000) throw new RangeError("字幕超过 20000 条上限");
  }
  return cues.sort((a, b) => a.start - b.start || a.end - b.end);
}

export function parseVtt(input) {
  const lines = String(input).replace(/^\ufeff/, "").replace(/\r/g, "").split("\n");
  const cues = [];
  for (let i = 0; i < lines.length; i++) {
    const match = lines[i].match(/^\s*((?:\d+:)?\d{2}:\d{2}[.,]\d+)\s+-->\s+((?:\d+:)?\d{2}:\d{2}[.,]\d+)/);
    if (!match) continue;
    const body = [];
    while (++i < lines.length && lines[i].trim()) body.push(lines[i]);
    cues.push({start: parseTime(match[1]), end: parseTime(match[2]), text: body.join(" ")});
  }
  return normalizeCues(cues);
}

export function parseYouTubeJson(input) {
  const data = typeof input === "string" ? JSON.parse(input) : input;
  return normalizeCues((data.events ?? []).filter(e => e.segs).map(e => ({
    start: Number(e.tStartMs) / 1000,
    end: (Number(e.tStartMs) + Number(e.dDurationMs || 2000)) / 1000,
    text: e.segs.map(s => s.utf8 ?? "").join("")
  })));
}

export function parseTimedXml(input) {
  // Legacy timedtext and srv3 captions.
  const cues = [];
  const expression = /<(text|p)\b([^>]*)>([\s\S]*?)<\/\1>/gi;
  for (const match of String(input).matchAll(expression)) {
    const attributes = Object.fromEntries([...match[2].matchAll(/([\w:-]+)\s*=\s*["']([^"']*)["']/g)].map(m => [m[1], m[2]]));
    const milliseconds = match[1].toLowerCase() === "p";
    const start = Number(milliseconds ? attributes.t : attributes.start) / (milliseconds ? 1000 : 1);
    const duration = Number(milliseconds ? attributes.d : attributes.dur) / (milliseconds ? 1000 : 1);
    cues.push({start, end: start + duration, text: match[3]});
  }
  return normalizeCues(cues);
}

export function parseCaptionDocument(text, contentType = "") {
  const raw = String(text).trim();
  if (raw.startsWith("{") || contentType.includes("json")) return parseYouTubeJson(raw);
  if (raw.startsWith("<")) return parseTimedXml(raw);
  return parseVtt(raw);
}

export function dedupeRollingCues(items) {
  const result = [];
  for (const cue of normalizeCues(items)) {
    const last = result[result.length - 1];
    if (!last || cue.start >= last.end - 0.03) { result.push({...cue}); continue; }
    const before = last.text.split(/\s+/), after = cue.text.split(/\s+/);
    const lower = s => s.toLocaleLowerCase("en-US").replace(/[.,!?;:]$/u, "");
    let overlap = 0;
    for (let n = Math.min(before.length, after.length); n > 0; n--) {
      if (before.slice(-n).map(lower).join(" ") === after.slice(0, n).map(lower).join(" ")) { overlap = n; break; }
    }
    // Merge overlap without removing later spoken repetitions.
    if (overlap && (overlap >= 2 || before.length === overlap || after.length === overlap)) {
      const tail = after.slice(overlap), merged = [...before, ...tail].join(" ");
      if (!tail.length || (cue.end - last.start <= 8 && merged.length <= 240)) {
        last.text = merged;
        last.end = Math.max(last.end, cue.end);
      } else {
        // Split long rolling-caption runs into shorter translation units.
        if (cue.start > last.start) last.end = cue.start;
        result.push({...cue, text: tail.join(" ")});
      }
    } else {
      // End the previous cue when the next one starts.
      if (cue.start > last.start) last.end = cue.start;
      result.push({...cue});
    }
  }
  return result;
}

export function isEnglishCaptionTrack(track) {
  const kind = String(track.kind ?? "").trim().toLowerCase();
  if (kind && !["subtitles", "captions", "asr"].includes(kind)) return false;
  const language = [track.languageCode, track.language, track.srclang]
    .map(value => String(value ?? "").trim()).find(Boolean) || "";
  // Language codes, including ISO 639-2 "eng", take precedence over labels.
  if (language) return /^(?:en|eng)(?:[-_]|$)/i.test(language);
  return /^english\b/i.test(String(track.label ?? "").trim());
}

export function chooseEnglishTrack(tracks) {
  return (tracks ?? []).filter(isEnglishCaptionTrack)
    .sort((a, b) => Number(a.kind === "asr" || /auto|asr/i.test(a.label || "")) - Number(b.kind === "asr" || /auto|asr/i.test(b.label || "")))[0] ?? null;
}

export function permissionOrigin(value) {
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return null;
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (host === "localhost" || host === "::1" || host.endsWith(".localhost") || host.endsWith(".local") || !host.includes(".") || /^[\d.]+$/.test(host) || host.includes(":")) return null;
    return `${url.protocol}//${url.hostname}/*`;
  } catch { return null; }
}

export function pageOrigin(value) {
  // Intranet pages are allowed here; caption fetch permissions are separate.
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return null;
    return `${url.protocol}//${url.hostname}/*`;
  } catch { return null; }
}
