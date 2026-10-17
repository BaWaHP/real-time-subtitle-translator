import {pageOrigin} from "./parsers.mjs";
const element = id => document.getElementById(id);
let requestedOrigins = [], busy = false, initialized = false, localError = "";
async function message(payload) {
  const result = await chrome.runtime.sendMessage(payload);
  if (!result?.ok) throw new Error(result?.error || "扩展后台没有响应，请重新加载扩展。");
  return result;
}
function displayError(error) { localError = error.message || String(error); element("warning").textContent = localError; }
async function refresh() {
  if (busy) return;
  try {
    const state = await message({type: "popup_status"});
    element("status").textContent = state.status;
    element("source").textContent = [state.sourceTitle, state.sourceHost].filter(Boolean).join(" · ");
    const diagnostics = state.captionDiagnostics;
    element("diagnostic").textContent = [diagnostics
      ? `网页字幕轨 ${diagnostics.native_tracks ?? 0} · 英语 ${diagnostics.english_tracks ?? 0} · 字幕 ${diagnostics.cue_count ?? 0} 条`
      : "", state.providerCueCount ? `Panopto 字幕 ${state.providerCueCount} 条` : ""].filter(Boolean).join(" · ");
    element("panoptoLanguageBox").hidden = !/(^|\.)panopto\.(com|eu)$/i.test(state.sourceHost || "");
    element("panoptoDefaultEnglish").checked = !!state.panoptoDefaultEnglish;
    element("warning").textContent = localError || state.warning || "";
    if (!initialized) {
      element("port").value = state.port;
      element("token").value = state.token;
      initialized = true;
    }
    requestedOrigins = state.pendingOrigins || [];
    element("permissionBox").hidden = requestedOrigins.length === 0;
    element("origins").replaceChildren(...requestedOrigins.map(origin => {
      const item = document.createElement("li"); item.textContent = origin; return item;
    }));
    element("stop").disabled = !state.connected;
  } catch (error) { displayError(error); }
}
function setBusy(value) {
  busy = value;
  for (const id of ["connect", "stop", "grant"]) element(id).disabled = value;
}
element("connect").addEventListener("click", async () => {
  setBusy(true); localError = ""; element("warning").textContent = "";
  try {
    const tabs = await chrome.tabs.query({active: true, currentWindow: true});
    const tab = tabs[0], origin = pageOrigin(tab?.url || "");
    if (!origin) throw new Error("请在视频网页中连接，浏览器设置等内部页面无法使用。");
    let port = Number(element("port").value), token = element("token").value.trim();
    if (element("code").value.trim()) {
      let code;
      try { code = JSON.parse(element("code").value.trim()); } catch { throw new Error("连接码格式不正确，请从桌面应用重新复制。"); }
      port = Number(code.port); token = String(code.token || "").trim();
    }
    if (!Number.isInteger(port) || port < 1024 || port > 65535 || token.length < 16) throw new Error("连接码不完整，请在桌面应用重新复制后粘贴。");
    // Chrome requires a user gesture for host permission requests.
    const granted = await chrome.permissions.request({origins: [origin]});
    if (!granted) throw new Error("尚未允许访问当前网站，请点击「连接当前视频」后确认授权。");
    element("status").textContent = "正在连接桌面应用…";
    await message({type: "popup_connect", tabId: tab.id, port, token});
    element("port").value = port; element("token").value = token; element("code").value = "";
  } catch (error) { displayError(error); }
  finally { setBusy(false); }
  await refresh();
});
element("stop").addEventListener("click", async () => {
  setBusy(true); localError = "";
  try { await message({type: "popup_stop"}); } catch (error) { displayError(error); }
  finally { setBusy(false); }
  await refresh();
});
element("grant").addEventListener("click", async () => {
  if (!requestedOrigins.length) return;
  setBusy(true); localError = "";
  try {
    const granted = await chrome.permissions.request({origins: requestedOrigins});
    if (!granted) throw new Error("尚未允许访问以上网站，请确认域名后重新授权，才能读取其中的视频或字幕。");
    await message({type: "popup_permissions_granted"});
  } catch (error) { displayError(error); }
  finally { setBusy(false); }
  await refresh();
});

element("panoptoDefaultEnglish").addEventListener("change", async () => {
  localError = "";
  try { await message({type: "popup_panopto_language", enabled: element("panoptoDefaultEnglish").checked}); }
  catch (error) { displayError(error); }
  await refresh();
});
void refresh();
setInterval(() => void refresh(), 1200);
