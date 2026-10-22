"""字幕页的字幕记录：所有字幕排在一整块只读文本里。

宽度跟随窗口自动换行；可以跨多条连续拖选、Ctrl+A 全选、Ctrl+C 复制，复制出来带时间和说话人。
每条字幕固定占 BLOCKS 个段落（时间 + 说话人、译文、原文、空行），按序号就能找到某条字幕的位置，
更新时只改那几个段落，不影响正在拖选的内容。
"""
import time

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import (QColor, QPalette, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextFormat,
                           QTextOption)
from PySide6.QtWidgets import QApplication, QFrame, QTextBrowser
from qfluentwidgets import (Action, FluentIcon as FIF, RoundMenu, SmoothScrollDelegate, isDarkTheme, qconfig,
                            setFont)

from thaibreak import soft_breaks, strip_breaks

BLOCKS = 4
HEAD, ZH, SRC, GAP = range(BLOCKS)


class TranscriptView(QTextBrowser):
    copy_all_requested = Signal()

    def __init__(self, max_rows, placeholder, parent=None):
        super().__init__(parent)
        self.max_rows = max_rows
        self.order = []    # 显示中的字幕 id（按时间顺序）
        self.segs = {}     # id -> 最新的字幕对象（切换深浅色时重画、右键复制用）
        self.setFrameShape(QFrame.NoFrame)
        self.setOpenLinks(False)
        self.setUndoRedoEnabled(False)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.setCursorWidth(0)     # 只读，不显示闪烁的光标
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextBrowser.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(16)
        self.setPlaceholderText(placeholder)
        self.setStyleSheet("QTextBrowser{background:transparent;border:none}")
        setFont(self, 14)
        self.scroll_delegate = SmoothScrollDelegate(self)
        self._apply_palette()
        qconfig.themeChanged.connect(self.rebuild)

    # --- 格式 ---
    def _apply_palette(self):
        dark = isDarkTheme()
        pal = self.palette()
        pal.setColor(QPalette.Text, QColor("#f0f0f0") if dark else QColor("#1a1a1a"))
        pal.setColor(QPalette.PlaceholderText, QColor("#a0a0a0") if dark else QColor("#606060"))
        self.setPalette(pal)

    def _fmt(self, kind, seg):
        dark = isDarkTheme()
        f = QTextCharFormat()
        if kind == ZH:
            done = seg.final and seg.zh_final and seg.zh_src == seg.text
            f.setProperty(QTextFormat.FontPixelSize, 17)
            f.setForeground(QColor("#f0f0f0" if dark else "#1a1a1a") if done
                            else QColor("#a0a0a0" if dark else "#707070"))
        else:
            f.setProperty(QTextFormat.FontPixelSize, 12)
            f.setForeground(QColor("#a0a0a0") if dark else QColor("#606060"))
        return f

    @staticmethod
    def _block_fmt(kind):
        b = QTextBlockFormat()
        if kind == GAP:
            b.setLineHeight(10, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        elif kind == ZH:
            b.setTopMargin(2)
            b.setBottomMargin(2)
        return b

    @staticmethod
    def _texts(seg):
        done = seg.final and seg.zh_final and seg.zh_src == seg.text
        state = "" if done else ("  · 翻译中" if seg.final else "  · 正在说")
        head = f"{time.strftime('%H:%M:%S', time.localtime(seg.start))}  {seg.speaker}{state}"
        return head, soft_breaks(seg.zh) or "…", soft_breaks(seg.text), ""

    # --- 增删改 ---
    def add(self, seg):
        self.segs[seg.id] = seg
        c = QTextCursor(self.document())
        c.movePosition(QTextCursor.End)
        c.beginEditBlock()
        for kind, text in enumerate(self._texts(seg)):
            if self.order or kind:      # 空文档本来就有一个段落，第一条字幕直接用它
                c.insertBlock()
            c.setBlockFormat(self._block_fmt(kind))
            fmt = self._fmt(kind, seg)
            c.setBlockCharFormat(fmt)
            c.insertText(text, fmt)
        c.endEditBlock()
        self.order.append(seg.id)
        while len(self.order) > self.max_rows:
            self._remove_first()

    def update_seg(self, seg):
        try:
            i = len(self.order) - 1 - self.order[::-1].index(seg.id)   # 通常是最后几条，从后往前找
        except ValueError:
            return
        self.segs[seg.id] = seg
        doc = self.document()
        c = QTextCursor(doc)
        c.beginEditBlock()
        for kind, text in enumerate(self._texts(seg)[:GAP]):
            block = doc.findBlockByNumber(i * BLOCKS + kind)
            fmt = self._fmt(kind, seg)
            if block.text() == text and block.charFormat().foreground() == fmt.foreground():
                continue    # 没变化就不动，免得打断正在拖选的文字
            c.setPosition(block.position())
            c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            c.setBlockCharFormat(fmt)
            c.insertText(text, fmt)
        c.endEditBlock()

    def _remove_first(self):
        sid = self.order.pop(0)
        self.segs.pop(sid, None)
        c = QTextCursor(self.document())
        c.movePosition(QTextCursor.NextBlock, QTextCursor.KeepAnchor, BLOCKS)
        c.removeSelectedText()

    def clear_all(self):
        self.order.clear()
        self.segs.clear()
        self.clear()

    def rebuild(self):
        """切换深浅色后按新颜色重画一遍。"""
        self._apply_palette()
        segs = [self.segs[i] for i in self.order]
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 40
        self.clear_all()
        for seg in segs:
            self.add(seg)
        if at_bottom:
            bar.setValue(bar.maximum())

    # --- 复制 ---
    def selected_text(self):
        return strip_breaks(self.textCursor().selection().toPlainText()).strip("\n")

    def createMimeDataFromSelection(self):
        md = QMimeData()
        md.setText(self.selected_text())
        return md

    def seg_at(self, pos):
        n = self.cursorForPosition(pos).blockNumber() // BLOCKS
        return self.segs.get(self.order[n]) if 0 <= n < len(self.order) else None

    def contextMenuEvent(self, e):
        clip = QApplication.clipboard()
        m = RoundMenu(parent=self)
        if self.textCursor().hasSelection():
            sel = self.selected_text()
            m.addAction(Action(FIF.COPY, "复制选中的内容", shortcut="Ctrl+C", triggered=lambda: clip.setText(sel)))
        m.addAction(Action(FIF.CHECKBOX, "全选", shortcut="Ctrl+A", triggered=self.selectAll))
        seg = self.seg_at(e.pos())
        if seg is not None:
            m.addSeparator()
            zh, th = seg.zh or "", seg.text
            m.addActions([Action(FIF.LANGUAGE, "复制这条译文", triggered=lambda: clip.setText(zh)),
                          Action(FIF.FONT, "复制这条原文", triggered=lambda: clip.setText(th)),
                          Action(FIF.DOCUMENT, "复制这条原文和译文", triggered=lambda: clip.setText(f"{th}\n{zh}"))])
        m.addSeparator()
        m.addAction(Action(FIF.HISTORY, "复制全部字幕（含更早的）", triggered=self.copy_all_requested.emit))
        m.exec(e.globalPos())
