// 把页面脚本收集到的字幕转发到本机软件（http://127.0.0.1:47321）。
// 由扩展后台发请求，不受 Teams 页面的跨域和本地网络访问限制。
const PORT = 47321;
let lastOk = null;

function setBadge(ok) {
  if (ok === lastOk) return;
  lastOk = ok;
  chrome.action.setBadgeText({ text: ok ? "ON" : "!" });
  chrome.action.setBadgeBackgroundColor({ color: ok ? "#2e7d32" : "#c62828" });
  chrome.action.setTitle({ title: ok ? "已连接泰语字幕翻译软件" : "未连接：请先打开“泰语字幕翻译”软件" });
}

async function post(path, body) {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setBadge(r.ok);
    return r.ok;
  } catch (e) {
    setBadge(false);
    return false;
  }
}

// 向软件报到：软件据此显示“扩展已安装”。软件没开时图标显示 “!”
function hello() {
  post("hello", { version: chrome.runtime.getManifest().version, ua: navigator.userAgent });
}
chrome.runtime.onInstalled.addListener(hello);
chrome.runtime.onStartup.addListener(hello);
chrome.alarms.create("hello", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(hello);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.path) return false;
  post(msg.path, msg.body).then((ok) => sendResponse({ ok }));
  return true;
});
