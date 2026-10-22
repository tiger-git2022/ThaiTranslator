// 读取 Teams 网页版的实时字幕（说话人 + 文本），把有变化的条目发给本机的“泰语字幕翻译”软件。
// Teams 的字幕是边说边改的：同一条字幕会不断变长、修正，所以这里只负责“发变化”，
// 断句、去重和翻译都交给软件做。
(() => {
  if (window.__thaiCaptionForwarder) return;
  window.__thaiCaptionForwarder = true;

  const SEL = {
    text: "[data-tid='closed-caption-text']",
    item: ".fui-ChatMessageCompact",
    author: "[data-tid='author']",
    inMeeting: "[data-tid='hangup-main-btn'], [data-tid='hangup-leave-button'], [data-tid='hangup-end-meeting-button'], #hangup-button",
  };
  const SCAN_MS = 250;
  const STATUS_MS = 3000;

  const session = Math.random().toString(36).slice(2, 8);
  let seq = 0;
  let rev = 0;
  let sent = new Map(); // 条目 id -> 上次发出的 "说话人\0文本"
  let lastStatus = 0;
  let timer = null;

  // 一条字幕所在的元素：优先用 Teams 的消息容器，类名变了就往上找到包含说话人的那一层
  function itemOf(textEl) {
    const item = textEl.closest(SEL.item);
    if (item) return item;
    let node = textEl.parentElement;
    for (let i = 0; node && i < 6; i++, node = node.parentElement) {
      if (node.querySelector(SEL.author)) return node;
    }
    return textEl.parentElement || textEl;
  }

  // Teams 对同一个人有时显示“Tiger Wang”，有时显示“Wang Tiger”：按词排序后比较
  function speakerKey(name) {
    return name.toLowerCase().replace(/,/g, " ").split(/\s+/).filter(Boolean).sort().join(" ");
  }

  function send(path, body) {
    try {
      chrome.runtime.sendMessage({ path, body }).catch(() => {});
    } catch (e) {
      // 扩展被重新加载后旧脚本会失效，停掉自己，等页面刷新
      clearInterval(timer);
    }
  }

  function scan() {
    const texts = document.querySelectorAll(SEL.text);
    const now = Date.now();
    const seen = new Map();
    const changed = [];

    texts.forEach((textEl) => {
      const item = itemOf(textEl);
      const authorEl = item.querySelector(SEL.author);
      const speaker = authorEl ? authorEl.innerText.trim() : "";
      const text = textEl.innerText.trim();
      if (!text) return;

      let id = item.dataset.tcId;
      const prev = id && sent.get(id);
      if (prev) {
        // Teams 可能复用旧元素显示新字幕：说话人变了或开头完全不同，就当作新的一条
        const [prevSpeaker, prevText] = prev.split("\u0000");
        if (speakerKey(prevSpeaker) !== speakerKey(speaker) || prevText.slice(0, 3) !== text.slice(0, 3)) {
          id = null;
        }
      }
      if (!id) {
        id = `${session}-${++seq}`;
        item.dataset.tcId = id;
      }

      const key = speaker + "\u0000" + text;
      seen.set(id, key);
      if (sent.get(id) !== key) changed.push({ id, speaker, text, rev: ++rev });
    });
    sent = seen;

    if (changed.length) send("captions", { session, items: changed, ts: now });
    if (now - lastStatus >= STATUS_MS) {
      lastStatus = now;
      send("status", {
        session,
        host: location.host,
        title: document.title,
        inMeeting: !!document.querySelector(SEL.inMeeting),
        captionsOn: texts.length > 0,
        ts: now,
      });
    }
  }

  timer = setInterval(scan, SCAN_MS);
})();
