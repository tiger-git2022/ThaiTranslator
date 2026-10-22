"""字幕状态机：合并扩展发来的增量，判断一句话何时说完，安排翻译。

- 文本有变化 → 标记为“未定稿”，记录最后变化时间
- 同一说话人出现了更新的一条，或 FINAL_IDLE 秒没再变化 → 定稿，送去翻译（带上文）
- 未定稿时，翻译线程空闲且距上次临时翻译超过 PREVIEW_INTERVAL → 先翻一次临时译文
"""
import time
import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from teams_desktop import speaker_key

log = logging.getLogger("tracker")
FINAL_IDLE = 1.6
FINAL_IDLE_ASR = 15.0   # 语音识别的句子由识别端断句定稿，这里只做兜底
PREVIEW_INTERVAL = 1.0
RETRY_DELAY = 3.0
CONTEXT_LINES = 2
CONTEXT_CHARS = 300


@dataclass
class Segment:
    id: str
    index: int
    speaker: str
    text: str
    start: float          # 第一次出现的时间（time.time()）
    last_change: float    # 最后变化（time.monotonic()）
    rev: int = 0
    final: bool = False
    zh: str = ""
    zh_src: str = ""      # 当前译文对应的原文
    zh_final: bool = False
    last_preview: float = 0.0
    retry_at: float = 0.0
    queued: tuple | None = None   # 已提交、还没返回的 (原文, 是否定稿)


class Tracker(QObject):
    added = Signal(object)      # Segment
    changed = Signal(object)    # Segment
    cleared = Signal()

    def __init__(self, translator, cfg):
        super().__init__()
        self.translator = translator
        self.cfg = cfg
        self.segments = {}
        self.order = []
        self.active = set()     # 还需要处理（未定稿或译文过期）的字幕 id
        self.names = {}         # 说话人 key -> 显示的名字
        translator.done.connect(self._on_done)
        translator.failed.connect(self._on_failed)
        self.timer = QTimer(self, interval=200, timeout=self.tick)
        self.timer.start()

    # --- 输入 ---
    def feed(self, items):
        now = time.monotonic()
        for it in items:
            sid, text = str(it.get("id", "")), str(it.get("text", "")).strip()
            if not sid or not text:
                continue
            speaker, rev = self._speaker(str(it.get("speaker", ""))), int(it.get("rev", 0))
            seg = self.segments.get(sid)
            if seg is None:
                seg = Segment(sid, len(self.order), speaker, text, time.time(), now, rev)
                self.segments[sid] = seg
                self.order.append(sid)
                self.active.add(sid)
                # 同一说话人开始了新的一条，说明之前的已经说完
                for other in self._recent_active():
                    if other is not seg and other.speaker == speaker and not other.final:
                        self._finalize(other)
                self.added.emit(seg)
                continue
            if rev and rev <= seg.rev:
                continue
            seg.rev = rev or seg.rev
            if seg.text == text and seg.speaker == speaker:
                continue
            seg.text, seg.speaker = text, speaker
            seg.last_change = now
            seg.final = False
            self.active.add(sid)
            self.changed.emit(seg)

    def _speaker(self, name):
        """同一个人的名字统一成第一次见到的写法（Teams 会在“Tiger Wang”和“Wang Tiger”之间变）。"""
        return self.names.setdefault(speaker_key(name), name.strip())

    def finalize(self, sid):
        """输入源明确知道这句已经说完（例如语音识别断句）时调用，立即定稿。"""
        seg = self.segments.get(sid)
        if seg is not None and not seg.final:
            self._finalize(seg)

    def clear(self):
        self.translator.clear()
        self.segments.clear()
        self.order.clear()
        self.active.clear()
        self.cleared.emit()

    # --- 调度 ---
    def _recent_active(self):
        return [self.segments[i] for i in list(self.active)]

    def _finalize(self, seg):
        log.debug("定稿 %s [%s]：%s", seg.id, seg.speaker, seg.text)
        seg.final = True
        self.changed.emit(seg)

    def context_for(self, seg):
        if not self.cfg.get("use_context"):
            return []
        ctx, total = [], 0
        for sid in reversed(self.order[:seg.index]):
            text = self.segments[sid].text
            if total + len(text) > CONTEXT_CHARS:
                break
            ctx.insert(0, text)
            total += len(text)
            if len(ctx) >= CONTEXT_LINES:
                break
        return ctx

    def tick(self):
        now = time.monotonic()
        preview = None
        for seg in sorted(self._recent_active(), key=lambda s: s.index):
            idle = FINAL_IDLE_ASR if seg.id.startswith("asr-") else FINAL_IDLE
            if not seg.final and now - seg.last_change >= idle:
                self._finalize(seg)
            if now < seg.retry_at:
                continue
            if seg.final:
                if seg.zh_src == seg.text and not seg.zh_final and seg.queued is None:
                    # 最后一次临时译文已经是完整原文（上文也相同），直接当作定稿
                    log.info("翻译定稿 %s（沿用临时译文）：%s → %s", seg.id, seg.text, seg.zh)
                    seg.zh_final = True
                    self.changed.emit(seg)
                if seg.zh_src == seg.text and seg.zh_final:
                    self.active.discard(seg.id)
                elif seg.queued != (seg.text, True):
                    seg.queued = (seg.text, True)
                    self.translator.submit(seg.id, seg.text, True, self.context_for(seg))
            elif seg.zh_src != seg.text:
                preview = seg   # 取最新的一条未定稿字幕
        # 刚冒出一两个字母的片段先不翻（模型拿不到内容时容易去翻上文），等长一点再说
        if (preview and self.cfg.get("live_preview") and self.translator.idle()
                and len("".join(preview.text.split())) >= 4
                and now - preview.last_preview >= PREVIEW_INTERVAL):
            preview.last_preview = now
            preview.queued = (preview.text, False)
            self.translator.submit(preview.id, preview.text, False, self.context_for(preview))

    # --- 翻译结果 ---
    def _on_done(self, sid, src, out, final, _elapsed):
        seg = self.segments.get(sid)
        if seg is None:
            return
        if seg.queued == (src, final):
            seg.queued = None
        # 临时译文回来时原文可能已经定稿重翻了，不要覆盖更新的译文
        if not final and seg.zh_final and seg.zh_src == seg.text:
            return
        seg.zh, seg.zh_src, seg.zh_final = out, src, final and src == seg.text
        if not seg.final or seg.zh_src != seg.text or not seg.zh_final:
            self.active.add(sid)
        self.changed.emit(seg)

    def _on_failed(self, sid, src, _err):
        seg = self.segments.get(sid)
        if seg is not None:
            seg.queued = None
            seg.retry_at = time.monotonic() + RETRY_DELAY
