(() => {
  "use strict";
  // Stop the previous probe before reinjection.
  globalThis.__subtitleEdgeProbe?.stop();
  // Document/media ID, with a fallback for HTTP pages without randomUUID.
  const documentId = (() => {
    try { if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID(); } catch { /* Fall back below. */ }
    try {
      if (globalThis.crypto?.getRandomValues) return [...globalThis.crypto.getRandomValues(new Uint32Array(4))].map(n => n.toString(16).padStart(8, "0")).join("");
    } catch { /* Use the timestamp fallback. */ }
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  })();
  const mediaIds = new WeakMap();
  const oldModes = new Map();
  let timer, listener, stopped = false, tickRunning = false, activeVideo, sourceEpoch = 0;
  let tickQueued = false;
  let lastCaptionKey = "", lastTrackAttempt = 0, lastWarning = "", lastTrackKey = "", lastSource = "";
  let cachedVideos = [], cachedFrames = [], discoveredAt = -Infinity;
  let diagnosticAt = -Infinity;
  const clean = s => String(s ?? "").replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
  const hash = value => { let h = 2166136261; for (let i = 0; i < value.length; i++) h = Math.imul(h ^ value.charCodeAt(i), 16777619); return (h >>> 0).toString(16); };
  // Keep aligned with parsers.mjs; injected classic scripts cannot import it.
  function isEnglishCaptionTrack(track) {
    const kind = String(track.kind ?? "").trim().toLowerCase();
    if (kind && !["subtitles", "captions", "asr"].includes(kind)) return false;
    const language = [track.languageCode, track.language, track.srclang]
      .map(value => String(value ?? "").trim()).find(Boolean) || "";
    if (language) return /^(?:en|eng)(?:[-_]|$)/i.test(language);
    return /^english\b/i.test(String(track.label ?? "").trim());
  }
  function nativeCaptionTracks(video) {
    return Array.from(video.textTracks ?? []).slice(0, 100).filter(isEnglishCaptionTrack)
      .sort((a, b) => Number(/auto|asr/i.test(a.label || "")) - Number(/auto|asr/i.test(b.label || "")));
  }
  function captionDiagnostics(video) {
    const tracks = nativeCaptionTracks(video);
    const result = {native_tracks: Math.min(Number(video.textTracks?.length) || 0, 20001),
      english_tracks: tracks.length, readable_tracks: 0, cue_count: 0, blocked_tracks: 0};
    for (const track of tracks) {
      try {
        const count = Math.min(Number(track.cues?.length) || 0, 20001);
        if (count) result.readable_tracks++;
        result.cue_count = Math.min(result.cue_count + count, 20001);
      } catch { result.blocked_tracks++; }
    }
    return result;
  }
  const restoreTracks = () => {
    for (const [track, saved] of oldModes) {
      try { if (track.mode === "hidden") track.mode = saved; } catch { /* Detached track. */ }
    }
    oldModes.clear();
  };
  function discoverMedia() {
    const videos = [], frames = [], roots = [document], visited = new Set();
    // Search open shadow roots for video and iframe elements.
    while (roots.length) {
      const root = roots.pop();
      if (visited.has(root)) continue;
      visited.add(root);
      videos.push(...root.querySelectorAll("video"));
      frames.push(...root.querySelectorAll("iframe"));
      for (const element of root.querySelectorAll("*")) {
        try { if (element.shadowRoot) roots.push(element.shadowRoot); } catch { /* Detached/custom element. */ }
      }
    }
    cachedVideos = videos;
    cachedFrames = frames.slice(0, 200);
    discoveredAt = Date.now();
  }
  function visibleArea(video) {
    try {
      const r = video.getBoundingClientRect(), style = getComputedStyle(video);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return 0;
      return Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
    } catch { return 0; } // A frame/video may detach while the DOM is scanned.
  }
  function selectVideo() {
    if (Date.now() - discoveredAt > 1000) discoverMedia();
    const candidates = cachedVideos.filter(v => v.isConnected).map(v => [v, visibleArea(v)]).filter(([, area]) => area > 0);
    candidates.sort((a, b) => b[1] - a[1]);
    return candidates[0] ?? [null, 0];
  }
  const changedSource = () => { sourceEpoch++; lastCaptionKey = ""; lastTrackKey = ""; lastTrackAttempt = 0; };
  const clockEvents = ["play", "pause", "seeking", "seeked", "ratechange", "ended", "waiting", "playing"];
  const clockChanged = () => {
    if (tickRunning) tickQueued = true;
    else void tick();
  };
  function setVideo(video) {
    if (activeVideo === video) return;
    if (activeVideo) {
      activeVideo.removeEventListener("loadedmetadata", changedSource);
      for (const event of clockEvents) activeVideo.removeEventListener(event, clockChanged);
    }
    restoreTracks();
    activeVideo = video;
    sourceEpoch = 0;
    lastCaptionKey = ""; lastTrackKey = ""; lastTrackAttempt = 0; lastWarning = "";
    if (video) {
      video.addEventListener("loadedmetadata", changedSource);
      for (const event of clockEvents) video.addEventListener(event, clockChanged);
    }
  }
  function sourceInfo(video) {
    if (!mediaIds.has(video)) mediaIds.set(video, `${documentId}-${mediaIds.counter = (mediaIds.counter ?? 0) + 1}`);
    const mediaId = mediaIds.get(video);
    let pageKey = location.href;
    try {
      const url = new URL(location.href);
      if (/(^|\.)youtube\.com$/i.test(url.hostname) && url.searchParams.get("v")) pageKey = `youtube:${url.searchParams.get("v")}`;
      else if (/(^|\.)panopto\.(?:com|eu)$/i.test(url.hostname)
          && /^\/Panopto\/Pages\/(?:Viewer|Embed)\.aspx$/i.test(url.pathname)
          && /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i.test(url.searchParams.get("id") || "")) {
        pageKey = `${url.origin}/panopto:${url.searchParams.get("id").toLowerCase()}`;
      }
      else if (/^\/shorts\//.test(url.pathname)) pageKey = `${url.origin}${url.pathname}`;
    } catch { /* URL remains usable as an opaque source key. */ }
    return {media_id: mediaId, source_id: `${mediaId}:${hash(`${pageKey}|${video.currentSrc}|${sourceEpoch}`)}`};
  }
  async function send(message) {
    if (stopped) return {};
    try {
      const reply = await chrome.runtime.sendMessage(message);
      if (reply?.stop) stop();
      return reply;
    }
    catch { stop(); return {}; }
  }
  function frameLayout() {
    return cachedFrames.filter(frame => frame.isConnected).map(frame => {
      let url = "about:blank";
      try { url = frame.hasAttribute("srcdoc") ? "about:srcdoc" : frame.src || "about:blank"; } catch { /* Detached frame. */ }
      return {url, area: visibleArea(frame)};
    });
  }
  async function tick() {
    if (stopped || tickRunning) return;
    tickRunning = true;
    try {
      const [video, area] = selectVideo();
      setVideo(video);
      const layout = frameLayout();
      if (!video) {
        await send({type: "probe_absent", document_id: documentId, page_url: location.href, frame_layout: layout});
        return;
      }
      const info = sourceInfo(video);
      if (lastSource !== info.source_id) {
        lastSource = info.source_id; lastCaptionKey = ""; lastTrackKey = ""; lastTrackAttempt = 0; lastWarning = "";
      }
      const reply = await send({type: "probe_state", ...info, area, frame_layout: layout, title: document.title.slice(0, 500),
        page_url: location.href, media_url: video.currentSrc || video.src || "",
        current_time: Number.isFinite(video.currentTime) ? video.currentTime : 0,
        sampled_at_ms: Date.now(),
        caption_diagnostics: captionDiagnostics(video),
        paused: video.paused, seeking: video.seeking,
        buffering: Number.isFinite(video.readyState) && video.readyState < 3,
        playback_rate: video.playbackRate,
        duration: Number.isFinite(video.duration) ? video.duration : 0, ended: video.ended});
      if (stopped) return;
      if (!reply?.selected || Date.now() - lastTrackAttempt < 1500) return;
      lastTrackAttempt = Date.now();
      const tracks = nativeCaptionTracks(video);
      let oversized = false;
      for (const track of tracks) {
        let cues = [];
        try {
          if (track.mode === "disabled") { if (!oldModes.has(track)) oldModes.set(track, track.mode); track.mode = "hidden"; }
          if ((track.cues?.length ?? 0) > 20000) {
            oversized = true;
            continue;
          }
          cues = [...(track.cues ?? [])].slice(0, 20000).map(c => ({start: c.startTime, end: c.endTime, text: clean(c.text)}))
            .filter(c => c.text && c.start >= 0 && c.end > c.start);
        } catch { /* Some websites protect access to cross-origin cues. */ }
        if (cues.length) {
          const key = `${info.source_id}:${cues.length}:${hash(JSON.stringify(cues))}`;
          if (key !== lastCaptionKey) {
            await send({type: "probe_captions", ...info, cues, language: "en", track_label: track.label || "网页英文字幕", rolling: /auto|asr/i.test(track.label)});
            lastCaptionKey = key;
          }
          return;
        }
      }
      if (oversized && lastWarning !== "too_many_cues") {
        lastWarning = "too_many_cues";
        await send({type: "probe_warning", ...info, message: "网页字幕超过 20000 条上限，请使用音轨预读或系统声音模式。"});
      }
      const sources = [...video.querySelectorAll("track[src]")].map(t => ({url: t.src, languageCode: t.srclang, label: t.label, kind: t.kind}));
      const warningKey = `${info.source_id}:${hash(JSON.stringify(sources))}`;
      if (warningKey !== lastTrackKey) {
        lastTrackKey = warningKey;
        await send({type: "probe_tracks", ...info, tracks: sources});
      }
    } catch (error) {
      // Report DOM/player errors while discovery continues.
      if (!stopped && Date.now() - diagnosticAt >= 5000) {
        diagnosticAt = Date.now();
        const kind = /^[A-Za-z]+Error$/.test(error?.name || "") ? error.name : "Error";
        await send({type: "probe_diagnostic", document_id: documentId, page_url: location.href,
          frame_layout: frameLayout(), message: `网页视频查找失败（${kind}），正在重试。`});
      }
    } finally {
      tickRunning = false;
      if (tickQueued && !stopped) { tickQueued = false; void tick(); }
    }
  }
  function stop() {
    stopped = true; clearInterval(timer);
    if (activeVideo) {
      activeVideo.removeEventListener("loadedmetadata", changedSource);
      for (const event of clockEvents) activeVideo.removeEventListener(event, clockChanged);
    }
    restoreTracks();
    if (listener) chrome.runtime.onMessage.removeListener(listener);
  }
  listener = (message, _sender, respond) => {
    if (message?.type === "stop_probe") { stop(); respond({ok: true}); }
  };
  chrome.runtime.onMessage.addListener(listener);
  globalThis.__subtitleEdgeProbe = {stop};
  timer = setInterval(() => void tick(), 250);
  void tick();
})();
