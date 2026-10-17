// Runs in the authorized player's MAIN world to read caption metadata.
export function inspectPageCaptionMetadata(options = {}) {
  const page = new URL(location.href);
  const panoptoId = page.searchParams.get("id") || "";
  if (/(?:^|\.)panopto\.(?:com|eu)$/i.test(page.hostname)
      && /^\/Panopto\/Pages\/(?:Viewer|Embed)\.aspx$/i.test(page.pathname)
      && /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i.test(panoptoId)) {
    // Panopto disables native TextTracks. Caption endpoint found in:
    // official CDN /Panopto/Cache/17.13.0.00001/Scripts/Panopto/Bundles/viewer.umd.js
    // Services.getCaptions (module 1223); Language (module 7816).
    // Fetch from the player frame using its existing session.
    return (async () => {
      const result = {provider: "panopto", video_id: panoptoId.toLowerCase(), tracks: [], cues: []};
      const sourceMatches = () => {
        if (options.page_url) {
          const expected = new URL(options.page_url); expected.hash = "";
          const actual = new URL(location.href); actual.hash = "";
          if (expected.href !== actual.href) return false;
        }
        if (options.media_url) {
          const videos = [...document.querySelectorAll("video")].slice(0, 200);
          if (!videos.some(video => (video.currentSrc || video.src || "") === options.media_url)) return false;
        }
        return true;
      };
      if (!sourceMatches()) return {...result, pending: true};
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 6000);
      const maxBytes = 4 * 1024 * 1024;
      const endpoint = new URL("/Panopto/Pages/Viewer/DeliveryInfo.aspx", page.origin).href;
      const english = new Map([[0, "en-US"], [1, "en-GB"], [15, "en-AU"]]);
      // Panopto ContentLanguage values; empty/-1 means unlabelled.
      const languageNumber = value => {
        if (value === null || value === undefined || value === "") return null;
        const number = Number(value);
        return Number.isInteger(number) ? number : null;
      };
      const request = async fields => {
        if (!sourceMatches()) throw new Error("source_changed");
        const response = await fetch(endpoint, {
          method: "POST", credentials: "same-origin", redirect: "error", signal: controller.signal,
          headers: {Accept: "application/json"},
          body: new URLSearchParams({deliveryId: panoptoId, responseType: "json", ...fields}),
        });
        if (response.status === 401 || response.status === 403) throw new Error("login_required");
        if (!response.ok || response.redirected) throw new Error("request_failed");
        if (Number(response.headers.get("Content-Length")) > maxBytes) throw new Error("too_large");
        let text;
        if (response.body?.getReader) {
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          const chunks = [];
          let bytes = 0;
          try {
            while (true) {
              const {value, done} = await reader.read();
              if (done) break;
              bytes += value.byteLength;
              if (bytes > maxBytes) throw new Error("too_large");
              chunks.push(decoder.decode(value, {stream: true}));
            }
            chunks.push(decoder.decode()); text = chunks.join("");
          } catch (error) { await reader.cancel().catch(() => {}); throw error; }
          finally { reader.releaseLock(); }
        } else {
          text = await response.text();
          if (new TextEncoder().encode(text).byteLength > maxBytes) throw new Error("too_large");
        }
        if (/^\s*(?:<!doctype\s+html|<html)/i.test(text)) throw new Error("login_required");
        let data;
        try { data = JSON.parse(text.replace(/^\ufeff/, "")); }
        catch { throw new Error("invalid_data"); }
        if (data?.ErrorCode === 2) throw new Error("login_required");
        if (data?.ErrorCode) throw new Error("request_failed");
        return data;
      };
      try {
        let languages = [], currentLanguage = null;
        try {
          languages = globalThis.PanoptoViewer?.Language?.getCaptionLanguages?.() || [];
          currentLanguage = languageNumber(globalThis.PanoptoViewer?.Language?.getCurrentCaptionLanguage?.()?.language);
        } catch { /* The player language menu may still be initializing. */ }
        languages = Array.isArray(languages) ? languages.slice(0, 100).map(item => languageNumber(item?.language)) : [];
        let duration = 0;
        try {
          const videos = [...document.querySelectorAll("video")].slice(0, 200);
          const video = videos.find(item => !options.media_url || (item.currentSrc || item.src || "") === options.media_url);
          duration = Number(video?.duration) || 0;
        } catch { /* Missing video duration does not prevent explicit cue timing. */ }
        if (!languages.some(language => english.has(language))) {
          const metadata = await request({});
          const delivery = metadata?.Delivery;
          if (!delivery || typeof delivery !== "object") throw new Error("invalid_data");
          // Older players use AvailableLanguages instead of AvailableCaptions.
          const available = Array.isArray(delivery.AvailableCaptions) && delivery.AvailableCaptions.length
            ? delivery.AvailableCaptions.map(item => item?.Language)
            : delivery.AvailableLanguages;
          languages = Array.isArray(available) ? available.slice(0, 100).map(languageNumber) : [];
          duration = Number(delivery.Duration) || duration;
        }
        const defaultAvailable = languages.some(language => language === null || language === -1);
        const defaultWarning = "Panopto 的 Default 字幕未标注语言。确认画面中的字幕为英语后，勾选「默认字幕是英语」即可重新读取。";
        const candidates = [...new Set(languages.filter(language => english.has(language)))];
        if (candidates.includes(currentLanguage)) candidates.sort((a, b) => Number(b === currentLanguage) - Number(a === currentLanguage));
        // Unlabelled Default tracks require the popup's English confirmation.
        if (defaultAvailable && options.allow_default_english === true) {
          if (currentLanguage === null || currentLanguage === -1) candidates.unshift(null);
          else candidates.push(null);
        }
        if (!candidates.length) return {...result, default_available: defaultAvailable,
          warning: defaultAvailable ? defaultWarning : "Panopto 未提供明确标记为英语的字幕轨。请在播放器的 CC 菜单选择 English 后重新连接。"};
        for (const language of candidates.slice(0, 4)) {
          const data = await request({getCaptions: "true", language: language === null ? "" : String(language)});
          if (!Array.isArray(data)) throw new Error("invalid_data");
          if (data.length > 20000) throw new Error("too_many_cues");
          const events = data.map(item => ({
            start: item?.Time === null || item?.Time === undefined ? NaN : Number(item.Time),
            duration: Number(item?.CaptionDuration ?? item?.Duration),
            text: typeof item?.Data === "string" && item.Data ? item.Data : typeof item?.Caption === "string" ? item.Caption : "",
          })).filter(item => Number.isFinite(item.start) && item.start >= 0 && item.text.trim())
            .sort((a, b) => a.start - b.start);
          const cues = events.map((item, index) => ({start: item.start,
            end: Number.isFinite(item.duration) && item.duration > 0 ? item.start + item.duration
              : events[index + 1]?.start > item.start ? events[index + 1].start
              : Number.isFinite(duration) && duration > item.start ? duration : item.start + 4,
            text: item.text,
          }));
          if (cues.length) {
            if (!sourceMatches()) return {...result, pending: true};
            return {...result, cues, language: "en", label: language === null
              ? "Panopto 默认字幕（用户确认为英语）" : `Panopto English (${english.get(language)})`};
          }
        }
        return {...result, default_available: defaultAvailable,
          warning: defaultAvailable && options.allow_default_english !== true ? defaultWarning
            : "Panopto 英语字幕为空，请在播放器的 CC 菜单选择 English，确认字幕显示后重新连接。"};
      } catch (error) {
        if (!sourceMatches() || error.message === "source_changed") return {...result, pending: true};
        const messages = {
          login_required: "无法读取 Panopto 字幕，请确认已登录播放器并打开英语字幕，再重新连接。",
          too_large: "Panopto 字幕数据超过 4 MiB 上限，已停止读取。",
          too_many_cues: "Panopto 字幕超过 20000 条上限，已停止读取。",
          invalid_data: "Panopto 字幕格式无法识别，请确认播放器已显示英语字幕后重新连接。",
        };
        const warning = controller.signal.aborted ? "Panopto 字幕读取超过 6 秒，请确认播放器已登录后重新连接。"
          : messages[error.message] || "Panopto 字幕读取失败，登录可能已过期，请在播放器确认登录后重新连接。";
        return {...result, warning};
      } finally { clearTimeout(timer); }
    })();
  }
  const expectedId = (() => {
    const url = new URL(location.href);
    return url.searchParams.get("v") || url.pathname.match(/^\/(?:shorts|embed)\/([^/?]+)/)?.[1] || "";
  })();
  let response;
  try { response = document.querySelector("#movie_player")?.getPlayerResponse?.(); } catch { /* Not a YouTube player. */ }
  if (!response) response = globalThis.ytInitialPlayerResponse;
  if (!response && globalThis.ytplayer?.config?.args?.player_response) {
    try { response = JSON.parse(globalThis.ytplayer.config.args.player_response); } catch { /* Metadata not ready. */ }
  }
  const videoId = response?.videoDetails?.videoId || "";
  if (expectedId && videoId !== expectedId) return {tracks: [], video_id: expectedId, pending: true};
  const tracks = response?.captions?.playerCaptionsTracklistRenderer?.captionTracks ?? [];
  return {video_id: videoId, tracks: tracks.slice(0, 100).map(t => ({
    url: String(t.baseUrl || ""), languageCode: String(t.languageCode || ""), kind: String(t.kind || ""),
    label: String(t.name?.simpleText || t.name?.runs?.map(r => r.text || "").join("") || t.languageCode || "")
  }))};
}
