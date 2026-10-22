"""置顶半透明字幕浮窗：显示最近几条字幕（译文大字、原文小字），最新的一条在最下面。

在文字上按住拖动可以选中（选中期间暂停刷新），Ctrl+C 或右键复制；在空白处拖动是移动浮窗。
"""
import ctypes
import html
import logging
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor, QFont, QGuiApplication, QKeySequence, QPainter,
                           QPainterPath, QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import QApplication, QSizeGrip, QWidget
from qfluentwidgets import Action, CheckableMenu, FluentIcon as FIF, MenuIndicatorType
import plat
from thaibreak import soft_breaks, strip_breaks

if plat.HAS_BACKDROP:
    from qframelesswindow import WindowEffect

log = logging.getLogger("overlay")
FONTS = "'Microsoft YaHei UI','Leelawadee UI','Segoe UI','PingFang SC','Thonburi',sans-serif"
PAD = 18


class Overlay(QWidget):
    geometry_changed = Signal(list)   # [x, y, w, h]
    option_changed = Signal(str, object)
    hide_requested = Signal()

    def __init__(self, cfg):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("泰语字幕翻译 - 浮窗")
        self.setMinimumSize(320, 90)
        self.cfg = cfg
        self.segments = []
        self.offset = 0          # 往上翻看历史时，离最新一条差几条（0 = 实时显示最新）
        self.drag = None
        self.frozen = None       # 选中文字期间冻结显示的内容（HTML）；None = 正常实时刷新
        self.sel = None          # [起点, 终点]，文档里的字符位置
        self.selecting = False
        self.notice = ""         # 顶部临时提示，如“已复制”
        self._doc, self._dy, self._shown = None, 0.0, ""
        # 选中后忘了复制也不会一直停在旧内容上
        self.freeze_timer = QTimer(self, singleShot=True, interval=30000, timeout=self.clear_selection)
        self.notice_timer = QTimer(self, singleShot=True, interval=1500,
                                   timeout=lambda: (setattr(self, "notice", ""), self.update()))
        self.setFocusPolicy(Qt.ClickFocus)
        self.grip = QSizeGrip(self)
        self.grip.resize(18, 18)
        self.save_timer = QTimer(self, singleShot=True, interval=500,
                                 timeout=lambda: self.geometry_changed.emit(self.geometry().getRect()))
        geo = cfg.get("overlay_geometry")
        if geo and len(geo) == 4:
            self.setGeometry(*geo)
            self.ensure_on_screen()
        else:
            self.default_geometry()
        self.effect = WindowEffect(self) if plat.HAS_BACKDROP else None
        self.blur_on = False
        self.set_click_through(cfg.get("click_through", False))

    # --- 毛玻璃 ---
    def apply_blur(self):
        """开启时用 Win11 亚克力模糊做背景，底色和不透明度跟随“背景不透明度”设置。"""
        if not self.isVisible() or self.effect is None:   # Mac：没有毛玻璃，用纯色半透明底
            return
        hwnd = int(self.winId())
        if self.cfg.get("overlay_blur"):
            alpha = round(255 * int(self.cfg.get("overlay_opacity", 80)) / 100)
            self.effect.setAcrylicEffect(hwnd, f"121418{alpha:02X}", enableShadow=False)
            # 圆角（DWMWA_WINDOW_CORNER_PREFERENCE = 33，2 = 圆角）
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
            self.blur_on = True
        elif self.blur_on:
            self.effect.removeBackgroundEffect(hwnd)
            self.blur_on = False
        self.update()

    # --- 位置 ---
    def default_geometry(self):
        scr = QGuiApplication.primaryScreen().availableGeometry()
        w, h = min(1100, scr.width() - 80), min(220, scr.height() // 3)
        self.setGeometry(scr.x() + (scr.width() - w) // 2, scr.bottom() - h - 60, w, h)

    def ensure_on_screen(self):
        """保存的位置可能在屏幕外（换了显示器、分辨率或缩放比例），露出不到一半就放回默认位置。"""
        g = self.geometry()
        visible = max((g.intersected(s.availableGeometry()) for s in QGuiApplication.screens()),
                      key=lambda r: r.width() * r.height())
        if visible.width() * visible.height() < g.width() * g.height() * 0.5:
            log.info("浮窗位置 %s 不在屏幕内，已放回默认位置", g.getRect())
            self.default_geometry()

    def showEvent(self, e):
        self.ensure_on_screen()
        super().showEvent(e)
        self.blur_on = False
        self.apply_blur()

    # --- 内容 ---
    def set_segments(self, segments):
        """segments 是全部字幕（按时间顺序）。翻看历史时新字幕进来，保持当前看的位置不动。"""
        if self.offset and segments and self.segments and len(segments) >= len(self.segments):
            self.offset += len(segments) - len(self.segments)
        if not segments:
            self.offset = 0
        self.segments = segments
        self.offset = min(self.offset, max(0, len(segments) - 1))
        self.update()

    def scroll_lines(self, n):
        """n > 0 往上翻看更早的字幕，n < 0 往回看新的。"""
        if self.frozen is not None:
            self.clear_selection()
        self.offset = max(0, min(self.offset + n, max(0, len(self.segments) - 1)))
        self.update()

    def _visible(self):
        lines = int(self.cfg.get("overlay_lines", 3))
        end = len(self.segments) - self.offset
        return self.segments[max(0, end - lines):end]

    def _html(self):
        size = int(self.cfg.get("overlay_font", 22))
        small = max(11, round(size * 0.62))
        show_src = self.cfg.get("overlay_show_source", True)
        rows = []
        for seg in self._visible():
            zh = html.escape(soft_breaks(seg.zh)) if seg.zh else "…"
            done = seg.final and seg.zh_final and seg.zh_src == seg.text
            color = "#ffffff" if done else "#c9d4e3"
            head = f"<span style='color:#7fb8ff;font-size:{small}px'>{html.escape(seg.speaker)}</span><br>" \
                if seg.speaker else ""
            src = f"<br><span style='color:#a8a8a8;font-size:{small}px'>{html.escape(soft_breaks(seg.text))}</span>" \
                if show_src else ""
            rows.append(f"<p style='margin:0 0 10px 0'>{head}"
                        f"<span style='color:{color};font-size:{size}px'>{zh}</span>{src}</p>")
        if rows:
            rows[-1] = rows[-1].replace("margin:0 0 10px 0", "margin:0", 1)
        if not rows:
            rows.append(f"<p style='color:#a8a8a8;font-size:{small}px'>等待字幕…（右键可调整浮窗）</p>")
        if self.offset:
            # 翻看历史时在最下面提示，滚到底或双击回到实时
            rows.append(f"<p style='margin:8px 0 0 0;color:#ffc94d;font-size:{small}px'>"
                        f"↑ 正在看历史字幕，后面还有 {self.offset} 条 · 向下滚动或双击回到最新</p>")
        return f"<div style=\"font-family:{FONTS}\">{''.join(rows)}</div>"

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.blur_on:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), 12, 12)
            alpha = round(255 * int(self.cfg.get("overlay_opacity", 80)) / 100)
            p.fillPath(path, QColor(18, 20, 24, alpha))
        shown = self.frozen if self.frozen is not None else self._html()
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        doc.setHtml(shown)
        doc.setTextWidth(self.width() - 2 * PAD)
        avail = self.height() - 2 * PAD
        # 像字幕一样贴底显示，内容超出时旧的从上方截掉
        dy = PAD + avail - doc.size().height()
        self._doc, self._dy, self._shown = doc, dy, shown   # 鼠标选字时按画出来的这份内容定位
        p.save()
        p.setClipRect(PAD, PAD, self.width() - 2 * PAD, avail)
        p.translate(PAD, dy)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.clip = QRectF(0, PAD - dy, self.width() - 2 * PAD, avail)
        if self.sel and self.sel[0] != self.sel[1]:
            s = QAbstractTextDocumentLayout.Selection()
            s.cursor = self._cursor(doc)
            f = QTextCharFormat()
            f.setBackground(QColor(0, 103, 192))
            f.setForeground(QColor("#ffffff"))
            s.format = f
            ctx.selections = [s]
        doc.documentLayout().draw(p, ctx)
        p.restore()
        tip = self.notice or (f"已暂停刷新 · {plat.MOD}C 复制 · Esc 继续" if self.frozen is not None else "")
        if tip:
            font = QFont(self.font())
            font.setPixelSize(11)
            p.setFont(font)
            p.setPen(QColor("#ffc94d"))
            p.drawText(QRectF(PAD, 0, self.width() - 2 * PAD, PAD), Qt.AlignRight | Qt.AlignVCenter, tip)
        p.end()

    # --- 选中 / 复制 ---
    def _cursor(self, doc):
        c = QTextCursor(doc)
        c.setPosition(self.sel[0])
        c.setPosition(self.sel[1], QTextCursor.KeepAnchor)
        return c

    def _hit(self, pos, exact):
        """窗口坐标 → 文档里的字符位置；exact 时不在文字上返回 -1。"""
        if self._doc is None:
            return -1
        y = pos.y() if exact else min(max(pos.y(), PAD), self.height() - PAD)
        pt = QPointF(pos.x() - PAD, y - self._dy)
        return self._doc.documentLayout().hitTest(pt, Qt.ExactHit if exact else Qt.FuzzyHit)

    def _freeze(self):
        if self.frozen is None:
            self.frozen = self._shown
        self.freeze_timer.start()

    def clear_selection(self):
        self.sel, self.frozen, self.selecting = None, None, False
        self.freeze_timer.stop()
        self.update()

    def select_all(self):
        if self._doc is None:
            return
        self._freeze()
        self.sel = [0, self._doc.characterCount() - 1]
        self.update()

    def selected_text(self):
        if not self.sel or self.sel[0] == self.sel[1] or self._doc is None:
            return ""
        return strip_breaks(self._cursor(self._doc).selection().toPlainText()).strip("\n")

    def _copy(self, text, note="已复制"):
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.clear_selection()
        self.notice = note
        self.notice_timer.start()
        self.update()

    def visible_text(self):
        """浮窗上正在显示的几条字幕，带时间和说话人。"""
        out = []
        for seg in self._visible():
            stamp = time.strftime("%H:%M:%S", time.localtime(seg.start))
            out.append(f"[{stamp}]{' ' + seg.speaker if seg.speaker else ''}\n{seg.text}\n{seg.zh or ''}".rstrip())
        return "\n\n".join(out)

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.Copy):
            self._copy(self.selected_text())
        elif e.matches(QKeySequence.SelectAll):
            self.select_all()
        elif e.key() == Qt.Key_Escape:
            self.clear_selection()
        else:
            super().keyPressEvent(e)

    # --- 拖动 / 缩放 ---
    def resizeEvent(self, e):
        self.grip.move(self.width() - self.grip.width(), self.height() - self.grip.height())
        self.save_timer.start()
        super().resizeEvent(e)

    def moveEvent(self, e):
        self.save_timer.start()
        super().moveEvent(e)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position().toPoint()
        hit = self._hit(pos, True) if PAD <= pos.y() <= self.height() - PAD else -1
        if hit >= 0:
            # 按在文字上：开始选中，期间冻结显示，免得字幕刷新把选区冲掉
            self._freeze()
            self.sel, self.selecting = [hit, hit], True
            self.activateWindow()
            self.setFocus()
            self.update()
            return
        if self.frozen is not None:
            self.clear_selection()
        self.drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self.selecting and e.buttons() & Qt.LeftButton:
            pos = self._hit(e.position().toPoint(), False)
            if pos >= 0 and pos != self.sel[1]:
                self.sel[1] = pos
                self.freeze_timer.start()
                self.update()
        elif self.drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, _):
        self.drag = None
        if self.selecting:
            self.selecting = False
            if self.sel[0] == self.sel[1]:      # 只是点了一下文字，没有拖选
                self.clear_selection()

    def wheelEvent(self, e):
        # 鼠标滚轮一格 = 120；触控板是很多个小数值，累计满 120 才算一格，免得一划翻过一大串
        dy = e.angleDelta().y()
        if not dy:
            return
        self._wheel = getattr(self, "_wheel", 0) + dy
        steps = int(self._wheel / 120)
        if not steps:
            return
        self._wheel -= steps * 120
        if e.modifiers() & Qt.ControlModifier:
            self._font_step(2 * steps)
        else:
            self.scroll_lines(steps)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clear_selection()
            self.scroll_lines(-len(self.segments))

    def _font_step(self, d):
        self.option_changed.emit("overlay_font", min(60, max(12, int(self.cfg.get("overlay_font", 22)) + d)))

    def contextMenuEvent(self, e):
        m = CheckableMenu(parent=self, indicatorType=MenuIndicatorType.CHECK)
        sel = self.selected_text()
        if sel:
            m.addAction(Action(FIF.COPY, f"复制选中的文字（{plat.MOD}C）", triggered=lambda: self._copy(sel)))
        if self.segments:
            last = self.segments[-1]
            shown = self.visible_text()
            m.addActions([Action(FIF.DOCUMENT, "复制浮窗上的字幕（带时间和说话人）",
                                 triggered=lambda: self._copy(shown)),
                          Action(FIF.LANGUAGE, "复制最新译文", triggered=lambda: self._copy(last.zh)),
                          Action(FIF.FONT, "复制最新原文", triggered=lambda: self._copy(last.text))])
        if sel or self.segments:
            m.addSeparator()
        m.addActions([Action(FIF.ZOOM_IN, f"字号放大（{plat.MOD}滚轮）", triggered=lambda: self._font_step(2)),
                      Action(FIF.ZOOM_OUT, "字号缩小", triggered=lambda: self._font_step(-2))])
        m.addSeparator()
        src = Action(FIF.FONT, "显示泰文原文", checkable=True)
        src.setChecked(bool(self.cfg.get("overlay_show_source", True)))
        src.toggled.connect(lambda v: self.option_changed.emit("overlay_show_source", v))
        blur = Action(FIF.BACKGROUND_FILL, "毛玻璃背景", checkable=True)
        blur.setChecked(bool(self.cfg.get("overlay_blur")))
        blur.toggled.connect(lambda v: self.option_changed.emit("overlay_blur", v))
        m.addActions([src, blur] if plat.HAS_BACKDROP else [src])
        m.addSeparator()
        m.addActions([Action(FIF.TRANSPARENT, "鼠标穿透（在托盘取消）",
                             triggered=lambda: self.option_changed.emit("click_through", True)),
                      Action(FIF.HIDE, "隐藏浮窗", triggered=lambda: self.hide_requested.emit())])
        m.exec(e.globalPos())

    def set_click_through(self, on):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowTransparentForInput, bool(on))
        self.grip.setVisible(not on)
        if visible:
            self.show()

    def reset_position(self):
        self.default_geometry()
