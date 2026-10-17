import {chooseEnglishTrack, parseCaptionDocument, dedupeRollingCues, normalizeCues, permissionOrigin, pageOrigin} from "./parsers.mjs";
import {inspectPageCaptionMetadata} from "./page_probe.js";

const MAX_BYTES = 4 * 1024 * 1024;
const frames = new Map();
const frameLayouts = new Map();
const frameCatalog = new Map(), probeAttempts = new Map(), probeDiagnostics = new Map();
const pendingOrigins = new Set();
const pendingCaptionOrigins = new Set();
const connectedOrigins = new Set();
const captionAttempts = new Map();
const activeSessions = new Set(), pendingDisconnects = new Set();
let connection = null, selected = null, status = "未连接", warning = "";
let pendingState, pendingCaptions, pendingWarning, pumping = false, pumpingDocuments = false, acknowledgedState = null, metadataAt = 0, metadataBusy = false;
let discoveryBusy = false, discoveryAt = 0, searchStartedAt = 0;
let metadataLoadedSource = "", metadataRetrySource = "", metadataRetryAt = 0, metadataCaptionCount = 0;
const ready = chrome.storage.session.get("connection").then(value => {
  connection = value.connection ?? null;
  if (connection) { searchStartedAt = Date.now(); status = "正在重新查找当前页面视频"; }
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const originName = pattern => { try { return new URL(pattern.replace("/*", "/")).hostname; } catch { return pattern; } };
function captionDiagnostics(value) {
  if (!value || typeof value !== "object") return null;
  const result = {};
  for (const key of ["native_tracks", "english_tracks", "readable_tracks", "cue_count", "blocked_tracks"]) {
    if (Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 20000) result[key] = value[key];
  }
  return Object.keys(result).length ? result : null;
}

// source_id invalidates cue caches when the video changes within a frame.
function sessionFor(frame) { return `${connection.tabId}:${frame.frameId}`; }
function confirmedDefaultEnglish(frame) {
  return !!frame && !!connection?.panoptoDefaultEnglish
    && connection.panoptoDefaultEnglishSource === `${frame.frameId}:${frame.source_id}`;
}
function bridgePayload(message, frame) {
  return {...message, session_id: sessionFor(frame), source_id: frame.source_id};
}
async function requestBridge(body, attempts = 2, credentials = connection) {
  if (!credentials) return false;
  const raw = JSON.stringify(body);
  if (new TextEncoder().encode(raw).length > MAX_BYTES) throw new Error("字幕超过 4 MiB 上限，请在桌面应用改用音轨预读。");
  for (let attempt = 0; attempt <= attempts; attempt++) {
    try {
      const response = await fetch(`http://127.0.0.1:${credentials.port}/v1/event`, {
        method: "POST", headers: {"Authorization": `Bearer ${credentials.token}`, "Content-Type": "application/json"},
        body: raw, signal: AbortSignal.timeout(1800), cache: "no-store", credentials: "omit"
      });
      if (response.status === 401 || response.status === 403) throw new Error("Edge 连接码无效，请在桌面应用重新复制后粘贴。");
      if (!response.ok) throw new Error(`桌面应用返回 HTTP ${response.status}`);
      return true;
    } catch (error) {
      if (attempt === attempts || /连接码无效/.test(error.message)) throw error;
      await sleep(250 * (attempt + 1));
      if (connection !== credentials) return false;
    }
  }
  return false;
}
async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    while (connection && (pendingState || pendingDisconnects.size)) {
      // Send playback updates separately from caption uploads.
      const state = pendingState; pendingState = null;
      const disconnected = [...pendingDisconnects]; pendingDisconnects.clear();
      const current = connection;
      try {
        for (const session_id of disconnected) { await requestBridge({type: "disconnect", session_id}, 0, current); activeSessions.delete(session_id); }
        // Drop stale clock samples; the next heartbeat replaces them.
        if (state && (!Number.isFinite(state.sampled_at_ms) || Date.now() - state.sampled_at_ms <= 2000)) {
          if (await requestBridge(state, 0, current) && connection === current) acknowledgedState = state;
        }
        if (connection !== current) continue;
        void pumpDocuments();
        if (connection === current) {
          if (selected && Date.now() - selected.seen < 1800) status = "已连接，播放进度同步中";
          else updateSearchStatus();
        }
      } catch (error) {
        if (connection === current) { warning = error.message || "桌面应用连接失败"; status = "连接异常"; }
      }
    }
  } finally { pumping = false; }
}
async function pumpDocuments() {
  if (pumpingDocuments) return;
  pumpingDocuments = true;
  try {
    while (connection && (pendingCaptions || pendingWarning)) {
      // Wait for the desktop to acknowledge this source before sending captions.
      if (pendingCaptions && (!acknowledgedState || pendingCaptions.source_id !== acknowledgedState.source_id
          || pendingCaptions.session_id !== acknowledgedState.session_id)) break;
      const captions = pendingCaptions; pendingCaptions = null;
      const notice = pendingWarning; pendingWarning = null;
      const current = connection;
      try {
        if (captions && selected && captions.source_id === selected.source_id && captions.session_id === sessionFor(selected)) await requestBridge(captions, 1, current);
        if (notice && connection === current && selected && notice.session_id === sessionFor(selected)) await requestBridge(notice, 0, current);
      } catch (error) {
        if (connection === current) { warning = error.message || "字幕传输失败"; status = "字幕传输失败，播放进度仍在同步"; }
      }
    }
  } finally { pumpingDocuments = false; }
}
function queueWarning(message, frame = selected) {
  warning = message;
  if (frame && connection) {
    pendingWarning = {type: "warning", session_id: sessionFor(frame), message};
    void pumpDocuments();
  }
}
function acceptCaptions(cues, frame, label, rolling = false) {
  if (!connection || !selected || frame.source_id !== selected.source_id || frame.frameId !== selected.frameId) return;
  const normalized = rolling ? dedupeRollingCues(cues) : normalizeCues(cues);
  if (!normalized.length) return;
  const payload = bridgePayload({type: "captions", cues: normalized, language: "en", track_label: label || "English"}, frame);
  if (new TextEncoder().encode(JSON.stringify(payload)).length > MAX_BYTES) { queueWarning("英文字幕超过 4 MiB 上限，请在桌面应用改用音轨预读。", frame); return; }
  pendingCaptions = payload;
  warning = "";
  pendingWarning = {type: "warning", session_id: sessionFor(frame), message: ""};
  void pumpDocuments();
}
function chooseFrame() {
  const now = Date.now();
  const sameUrl = (left, right) => { try { const a = new URL(left), b = new URL(right); a.hash = b.hash = ""; return a.href === b.href; } catch { return false; } };
  const effectiveArea = (frame, visited = new Set()) => {
    if (frame.frameId === 0 || visited.has(frame.frameId)) return frame.area;
    visited.add(frame.frameId);
    const parentBounds = [];
    const actualParent = frameCatalog.get(frame.frameId)?.parentFrameId;
    for (const [parentId, layout] of frameLayouts) {
      if (Number.isInteger(actualParent) && parentId !== actualParent) continue;
      if (parentId === frame.frameId || now - layout.seen >= 1800) continue;
      for (const item of layout.iframes) if (sameUrl(item.url, frame.page_url)) {
        const parent = {frameId: parentId, area: Infinity, page_url: layout.page_url};
        parentBounds.push(Math.min(item.area, effectiveArea(parent, new Set(visited))));
      }
    }
    return parentBounds.length ? Math.min(frame.area, Math.max(...parentBounds)) : frame.area;
  };
  const fresh = [...frames.values()].filter(f => now - f.seen < 1800 && f.area > 0)
    .map(f => ({...f, effectiveArea: effectiveArea(f)})).filter(f => f.effectiveArea > 0);
  fresh.sort((a, b) => b.effectiveArea - a.effectiveArea);
  const candidate = fresh[0];
  if (!candidate) return null;
  const previous = selected && fresh.find(f => f.frameId === selected.frameId && f.media_id === selected.media_id);
  return previous && previous.effectiveArea >= candidate.effectiveArea * 0.85 ? previous : candidate;
}
async function hasCaptionPermission(url) {
  const pattern = permissionOrigin(url);
  if (!pattern) return false;
  if (await chrome.permissions.contains({origins: [pattern]})) { connectedOrigins.add(pattern); return true; }
  pendingOrigins.add(pattern);
  pendingCaptionOrigins.add(pattern);
  return false;
}
async function fetchCaption(track, frame, retry = 0) {
  if (!connection || selected?.source_id !== frame.source_id || selected.frameId !== frame.frameId) return;
  const attemptKey = `${frame.source_id}:${track.url}`;
  if (retry === 0 && captionAttempts.has(attemptKey)) return;
  captionAttempts.set(attemptKey, true);
  if (captionAttempts.size > 100) captionAttempts.delete(captionAttempts.keys().next().value);
  const url = new URL(track.url, frame.page_url);
  if (!permissionOrigin(url.href)) { queueWarning("字幕地址不支持读取，请改用系统声音模式。", frame); return; }
  if (!await hasCaptionPermission(url.href)) {
    captionAttempts.delete(attemptKey);
    queueWarning(`字幕来自 ${url.hostname}，请在扩展弹窗确认并授权该域名。`, frame); return;
  }
  // Request JSON3 for YouTube timedtext URLs supplied by the page.
  const youtube = /(^|\.)youtube\.com$/i.test(url.hostname) && /\/api\/timedtext$/.test(url.pathname);
  if (youtube) url.searchParams.set("fmt", "json3");
  try {
    const response = await fetch(url.href, {credentials: "include", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const declared = Number(response.headers.get("content-length") || 0);
    if (declared > MAX_BYTES) throw new Error("字幕文件超过 4 MiB");
    const reader = response.body.getReader(), chunks = []; let length = 0;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BYTES) { await reader.cancel(); throw new Error("字幕文件超过 4 MiB"); }
      chunks.push(value);
    }
    const merged = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { merged.set(chunk, offset); offset += chunk.length; }
    const cues = parseCaptionDocument(new TextDecoder().decode(merged), response.headers.get("content-type") || "");
    if (!cues.length) throw new Error("网站返回空字幕或尚不支持的字幕格式");
    acceptCaptions(cues, frame, track.label, track.kind === "asr");
  } catch (error) {
    const message = error.message || String(error);
    if (retry < 2 && !/超过|空字幕|格式|HTTP (?:400|401|403|404)/.test(message)) {
      await sleep(400 * (retry + 1));
      return fetchCaption(track, frame, retry + 1);
    }
    if (selected?.source_id === frame.source_id) queueWarning(`英文字幕读取失败：${message}。请在桌面应用改用音轨预读或系统声音模式。`, frame);
  }
}
async function inspectMetadata(frame) {
  const sourceKey = `${frame.frameId}:${frame.source_id}`;
  if (metadataBusy || Date.now() - metadataAt < 3000 || !connection
      || metadataLoadedSource === sourceKey
      || metadataRetrySource === sourceKey && Date.now() < metadataRetryAt) return;
  metadataAt = Date.now(); metadataBusy = true;
  const current = connection;
  const defaultEnglishConfirmed = confirmedDefaultEnglish(frame);
  try {
    const result = await chrome.scripting.executeScript({target: {tabId: current.tabId, frameIds: [frame.frameId]},
      world: "MAIN", func: inspectPageCaptionMetadata, args: [{page_url: frame.page_url, media_url: frame.media_url,
        allow_default_english: defaultEnglishConfirmed}]});
    if (connection !== current || selected?.source_id !== frame.source_id || selected?.frameId !== frame.frameId) return;
    if (confirmedDefaultEnglish(frame) !== defaultEnglishConfirmed) return;
    const metadata = result[0]?.result ?? {};
    if (metadata.provider === "panopto") {
      if (Array.isArray(metadata.cues) && metadata.cues.length) {
        acceptCaptions(metadata.cues, frame, metadata.label || "Panopto English", false);
        metadataLoadedSource = sourceKey;
        metadataCaptionCount = metadata.cues.length;
      } else if (metadata.warning) {
        metadataRetrySource = sourceKey; metadataRetryAt = Date.now() + 15000;
        queueWarning(String(metadata.warning).slice(0, 1500), frame);
      }
      return;
    }
    const track = chooseEnglishTrack(metadata.tracks ?? []);
    if (track) await fetchCaption(track, frame);
  } catch { /* The frame may have navigated between ticks. */ }
  finally { metadataBusy = false; }
}
function updateSearchStatus() {
  if (!connection || selected && Date.now() - selected.seen < 1800 || status === "连接异常") return;
  if (pendingOrigins.size) {
    status = "发现嵌入页面，等待额外网站授权";
    warning = "嵌入播放器或字幕来自其他网站，请确认下方域名后允许访问。";
  } else if (Date.now() - searchStartedAt >= 6000) {
    status = "未找到可读取视频，仍在自动检查";
    const errors = [...probeDiagnostics.values()];
    warning = errors.length ? `有 ${errors.length} 个嵌入页面无法读取：${errors[0]}`
      : "当前页面未找到视频，请先开始播放；如果播放器在独立窗口中，请在那个窗口连接。";
  } else status = "正在查找当前页面视频";
}
function framePermission(frame, visited = new Set()) {
  const direct = pageOrigin(frame?.url || "");
  if (direct) return direct;
  if (!frame || visited.has(frame.frameId)) return null;
  visited.add(frame.frameId);
  // Resolve inherited origins for srcdoc/about:blank. executeScript checks permissions.
  if (/^(?:about:|data:|blob:|filesystem:)/i.test(frame.url || "")) {
    try {
      const inherited = pageOrigin(new URL(frame.url).origin);
      if (inherited) return inherited;
    } catch { /* Opaque about:/data: origin; check the parent below. */ }
    return framePermission(frameCatalog.get(frame.parentFrameId), visited);
  }
  return null;
}
async function discoverProbes(force = false) {
  const current = connection, now = Date.now();
  if (!current || discoveryBusy || !force && now - discoveryAt < 1500) return;
  discoveryBusy = true; discoveryAt = now;
  try {
    // Include nested, redirected and srcdoc frames in the connected tab.
    const found = await chrome.webNavigation.getAllFrames({tabId: current.tabId});
    if (connection !== current) return;
    const catalog = (found || []).slice(0, 100);
    frameCatalog.clear();
    for (const frame of catalog) frameCatalog.set(frame.frameId, frame);
    for (const map of [frames, frameLayouts, probeAttempts, probeDiagnostics]) {
      for (const id of map.keys()) if (!frameCatalog.has(id)) map.delete(id);
    }
    if (selected && !frameCatalog.has(selected.frameId)) {
      pendingDisconnects.add(sessionFor(selected)); selected = null;
      pendingState = pendingCaptions = null; void pump();
    }
    const permissionCache = new Map();
    const allowed = async pattern => {
      if (!permissionCache.has(pattern)) permissionCache.set(pattern, await chrome.permissions.contains({origins: [pattern]}));
      if (permissionCache.get(pattern)) connectedOrigins.add(pattern);
      return permissionCache.get(pattern);
    };
    for (const pattern of [...pendingOrigins]) {
      if (await allowed(pattern)) { pendingOrigins.delete(pattern); pendingCaptionOrigins.delete(pattern); }
      else if (!pendingCaptionOrigins.has(pattern)) pendingOrigins.delete(pattern);
      if (connection !== current) return;
    }
    for (const frame of catalog) {
      if (connection !== current) return;
      const pattern = framePermission(frame);
      if (!pattern) continue;
      if (!await allowed(pattern)) {
        const remote = permissionOrigin(frame.url);
        if (remote && pendingOrigins.size < 100) pendingOrigins.add(remote);
        continue;
      }
      if (connection !== current) return;
      const documentKey = frame.documentId || frame.url;
      const prior = probeAttempts.get(frame.frameId);
      const heartbeat = frameLayouts.get(frame.frameId);
      // Preserve media IDs; reinject for new documents or missing heartbeats.
      if (prior?.documentKey === documentKey && (heartbeat && now - heartbeat.seen < 1800 || now - prior.at < 10000)) continue;
      probeAttempts.set(frame.frameId, {documentKey, at: now});
      try {
        const target = frame.documentId ? {tabId: current.tabId, documentIds: [frame.documentId]}
          : {tabId: current.tabId, frameIds: [frame.frameId]};
        await chrome.scripting.executeScript({target, files: ["content.js"], injectImmediately: true});
        if (connection === current) probeDiagnostics.delete(frame.frameId);
      } catch {
        if (connection === current) probeDiagnostics.set(frame.frameId,
          /^(?:about:|data:|blob:|filesystem:)/i.test(frame.url || "")
            ? "浏览器限制了嵌入播放器的读取，请尝试在播放器独立页面连接，或改用系统声音模式。"
            : "浏览器不允许读取嵌入播放器，请检查网站权限，刷新网页后重新连接。");
        // Continue probing other frames after a permission failure.
      }
    }
    updateSearchStatus();
  } catch {
    if (connection === current) {
      status = "无法读取嵌入页面";
      warning = "请在 edge://extensions 重新加载扩展并检查网站权限，然后刷新视频网页，重新连接。";
    }
  } finally { discoveryBusy = false; }
}
async function disconnect() {
  const previous = connection, oldSelected = selected;
  const previousSessions = new Set(activeSessions);
  if (previous && oldSelected) previousSessions.add(`${previous.tabId}:${oldSelected.frameId}`);
  connection = null; selected = null; frames.clear(); frameLayouts.clear(); frameCatalog.clear(); probeAttempts.clear(); probeDiagnostics.clear(); captionAttempts.clear(); pendingOrigins.clear(); pendingCaptionOrigins.clear(); connectedOrigins.clear();
  metadataLoadedSource = metadataRetrySource = ""; metadataRetryAt = 0;
  metadataCaptionCount = 0;
  discoveryAt = searchStartedAt = 0;
  activeSessions.clear(); pendingDisconnects.clear();
  pendingState = pendingCaptions = pendingWarning = acknowledgedState = null; metadataAt = 0;
  await chrome.storage.session.remove("connection");
  if (previous) {
    try { await chrome.tabs.sendMessage(previous.tabId, {type: "stop_probe"}); } catch { /* Tab closed. */ }
    // Serialized after existing POSTs by waiting briefly for the bounded pump.
    for (let i = 0; i < 50 && (pumping || pumpingDocuments); i++) await sleep(100);
    for (const session_id of previousSessions) {
      try { await requestBridge({type: "disconnect", session_id}, 0, previous); } catch { /* Desktop heartbeat expiry also clears it. */ }
    }
  }
  status = "已停止"; warning = "";
}
async function connect(message) {
  const port = Number(message.port), token = String(message.token || "").trim();
  if (!Number.isInteger(port) || port < 1024 || port > 65535 || token.length < 16 || token.length > 1024) throw new Error("请粘贴桌面应用提供的完整 Edge 连接码。");
  await disconnect();
  const tab = await chrome.tabs.get(message.tabId);
  if (!pageOrigin(tab.url)) throw new Error("请在视频网页中连接，浏览器设置等内部页面无法使用。");
  const health = await fetch(`http://127.0.0.1:${port}/v1/health`, {headers: {Authorization: `Bearer ${token}`}, signal: AbortSignal.timeout(2500), cache: "no-store", credentials: "omit"});
  if (!health.ok) throw new Error(health.status === 401 || health.status === 403 ? "Edge 连接码无效，请在桌面应用重新复制后粘贴。" : `桌面应用返回 HTTP ${health.status}`);
  connection = {tabId: message.tabId, port, token};
  searchStartedAt = Date.now();
  await chrome.storage.session.set({connection});
  await chrome.storage.local.set({bridgePort: port, bridgeToken: token});
  status = "正在查找当前页面视频";
  await discoverProbes(true);
  return {ok: true};
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  (async () => {
    await ready;
    const extensionPage = sender.id === chrome.runtime.id && String(sender.url || "").startsWith(chrome.runtime.getURL(""));
    if (sender.tab && !extensionPage) {
      if (!connection || sender.tab.id !== connection.tabId) return {stop: true};
      const frameId = sender.frameId ?? 0;
      if (frameLayouts.size >= 100 && !frameLayouts.has(frameId)) return {stop: true};
      const knownDocument = frameCatalog.get(frameId)?.documentId;
      if (knownDocument && sender.documentId && sender.documentId !== knownDocument) return {stop: true};
      if (["probe_state", "probe_absent", "probe_diagnostic"].includes(message.type)) frameLayouts.set(frameId, {
        page_url: message.page_url, seen: Date.now(), iframes: Array.isArray(message.frame_layout) ? message.frame_layout.slice(0, 200) : []
      });
      if (["probe_state", "probe_absent", "probe_diagnostic"].includes(message.type)) void discoverProbes();
      if (message.type === "probe_diagnostic") {
        probeDiagnostics.set(frameId, String(message.message || "网页视频查找失败。").slice(0, 500));
        updateSearchStatus(); return {ok: true};
      }
      if (message.type === "probe_absent") {
        frames.delete(frameId);
        if (selected?.frameId === frameId) { pendingDisconnects.add(sessionFor(selected)); selected = null; pendingState = pendingCaptions = null; void pump(); }
        updateSearchStatus(); return {ok: true};
      }
      if (message.type === "probe_state") {
        const frame = {...message, frameId, seen: Date.now()};
        frames.set(frameId, frame);
        const previous = selected; selected = chooseFrame();
        if (!selected || selected.frameId !== frameId) return {selected: false};
        if (previous && sessionFor(previous) !== sessionFor(selected)) pendingDisconnects.add(sessionFor(previous));
        if (previous?.source_id !== selected.source_id || previous?.frameId !== selected.frameId) {
          metadataAt = 0; pendingCaptions = null; warning = "";
          for (const pattern of pendingCaptionOrigins) pendingOrigins.delete(pattern);
          pendingCaptionOrigins.clear();
        }
        const {current_time, paused, seeking, playback_rate, duration, ended, title, page_url, media_url, sampled_at_ms, buffering} = frame;
        pendingState = bridgePayload({type: "state", current_time, paused, seeking, playback_rate, duration, ended, title, page_url, media_url, sampled_at_ms, buffering}, frame);
        activeSessions.add(pendingState.session_id);
        void pump(); void inspectMetadata(frame);
        return {selected: true};
      }
      const frame = frames.get(frameId);
      if (!frame || message.source_id !== selected?.source_id || frameId !== selected.frameId) return {ok: true};
      if (message.type === "probe_captions") acceptCaptions(message.cues, frame, message.track_label, message.rolling);
      if (message.type === "probe_warning") queueWarning(String(message.message || "字幕读取失败").slice(0, 1500), frame);
      if (message.type === "probe_tracks") {
        const track = chooseEnglishTrack(message.tracks);
        if (track) void fetchCaption(track, frame);
        else if (!/\.panopto\.(?:com|eu)\//i.test(frame.page_url || "")) queueWarning("未找到可读取的英文字幕，请先打开播放器的英语字幕。仍无法读取时，可在桌面应用改用音轨预读。", frame);
      }
      return {ok: true};
    }
    // Popup commands require an extension-page sender.
    if (!extensionPage) throw new Error("无效扩展来源");
    if (message.type === "popup_status") {
      await discoverProbes(); updateSearchStatus();
      const saved = await chrome.storage.local.get(["bridgePort", "bridgeToken"]);
      return {ok: true, connected: !!connection, tabId: connection?.tabId, status, warning,
        sourceTitle: selected?.title || "", pendingOrigins: [...pendingOrigins],
        sourceHost: (() => { try { return new URL(selected?.page_url).hostname; } catch { return ""; } })(),
        captionDiagnostics: captionDiagnostics(selected?.caption_diagnostics),
        panoptoDefaultEnglish: confirmedDefaultEnglish(selected),
        providerCueCount: selected && metadataLoadedSource === `${selected.frameId}:${selected.source_id}` ? metadataCaptionCount : 0,
        port: saved.bridgePort || 18765, token: saved.bridgeToken || ""};
    }
    if (message.type === "popup_connect") return await connect(message);
    if (message.type === "popup_stop") { await disconnect(); return {ok: true}; }
    if (message.type === "popup_panopto_language" && connection) {
      if (!selected) return {ok: false, error: "请先连接并选择当前 Panopto 视频。"};
      if (!message.enabled && confirmedDefaultEnglish(selected)) {
        await disconnect();
        status = "已取消英语字幕确认，连接已停止";
        warning = "请在播放器选择英语字幕后重新连接；使用未标注语言的默认字幕时，需要再次确认其为英语。";
        return {ok: true};
      }
      connection.panoptoDefaultEnglish = message.enabled === true;
      connection.panoptoDefaultEnglishSource = `${selected.frameId}:${selected.source_id}`;
      await chrome.storage.session.set({connection});
      metadataLoadedSource = metadataRetrySource = ""; metadataRetryAt = 0; metadataAt = 0;
      if (selected) void inspectMetadata(selected);
      return {ok: true};
    }
    if (message.type === "popup_permissions_granted" && connection) {
      for (const pattern of [...pendingOrigins]) if (await chrome.permissions.contains({origins: [pattern]})) pendingOrigins.delete(pattern);
      metadataAt = 0; captionAttempts.clear();
      metadataLoadedSource = metadataRetrySource = ""; metadataRetryAt = 0;
      discoveryAt = 0; await discoverProbes(true); return {ok: true};
    }
    return {ok: false, error: "未知命令"};
  })().then(respond).catch(error => respond({ok: false, error: error.message || String(error)}));
  return true;
});
chrome.tabs.onRemoved.addListener(tabId => { void ready.then(() => { if (connection?.tabId === tabId) return disconnect(); }); });
chrome.tabs.onUpdated.addListener((tabId, change) => {
  void ready.then(async () => {
    if (connection?.tabId !== tabId) return;
    if (change.status === "loading") {
      if (selected) pendingDisconnects.add(sessionFor(selected));
      frames.clear(); frameLayouts.clear(); frameCatalog.clear(); probeAttempts.clear(); probeDiagnostics.clear(); pendingOrigins.clear(); pendingCaptionOrigins.clear(); selected = null; pendingState = pendingCaptions = null; metadataAt = 0; searchStartedAt = Date.now();
      void pump();
      status = "正在切换页面，加载完成后继续读取";
    }
    if (change.status === "complete") {
      await discoverProbes(true);
    }
  });
});
chrome.webNavigation.onCommitted.addListener(details => {
  void ready.then(() => {
    if (connection?.tabId !== details.tabId) return;
    probeAttempts.delete(details.frameId); frameLayouts.delete(details.frameId); frames.delete(details.frameId);
    if (selected?.frameId === details.frameId) {
      pendingDisconnects.add(sessionFor(selected)); selected = null;
      pendingState = pendingCaptions = null; void pump();
    }
    return discoverProbes(true);
  });
});
chrome.permissions.onRemoved.addListener(removed => {
  void ready.then(async () => {
    const current = connection;
    if (!current || !removed.origins?.length) return;
    for (const pattern of connectedOrigins) {
      if (!await chrome.permissions.contains({origins: [pattern]})) {
        if (connection !== current) return;
        // Stop injected probes when host permission is revoked.
        await disconnect();
        status = "网站权限已移除，已停止连接";
        warning = "请点击「连接当前视频」，确认网站授权后继续读取。";
        return;
      }
      if (connection !== current) return;
    }
  });
});
// Find late-loading player frames when no probe is sending heartbeats.
setInterval(() => { void ready.then(() => discoverProbes()); }, 1500);
