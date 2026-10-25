"""泰语字幕翻译：接收 Teams 字幕（浏览器扩展转发的网页版字幕、直接读取的桌面版字幕），调用本地大模型翻译，在窗口和置顶浮窗里显示。"""
import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QSystemTrayIcon,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (Action, BodyLabel, CaptionLabel, ComboBox, DropDownPushButton, EditableComboBox, FlowLayout,
                            FluentIcon as FIF,
                            FluentWindow, InfoBar, InfoBarPosition, LineEdit, MessageBox, ProgressBar, SegmentedWidget, ListWidget, MessageBoxBase, NavigationItemPosition,
                            PasswordLineEdit, PlainTextEdit, PrimaryPushButton, PushButton, PushSettingCard,
                            RoundMenu, ScrollArea, SettingCard, SettingCardGroup, SimpleCardWidget, Slider, SpinBox,
                            StrongBodyLabel, SubtitleLabel, SwitchButton, SystemTrayMenu, Theme, TitleLabel,
                            isDarkTheme, setTheme, setThemeColor)

import config
import log as applog
import updater
from asr import AsrSource, list_loopback_devices
from bridge import Bridge
from share import RemoteStore, Watcher, discover, lan_addresses, parse_address, seg_to_dict
from share_dialog import ShareDialog
from touchpad import tame
from share_server import ShareServer, new_code
from models import ModelKeeper
from install_dialog import INSTALL_DIR, InstallDialog, sync_extension
from overlay import Overlay
from tracker import Tracker
from transcript_view import TranscriptView
from translator import Translator, active, list_models, translate
import plat

if plat.HAS_TEAMS_DESKTOP:
    from teams_desktop import TeamsDesktopSource
else:
    class TeamsDesktopSource(QObject):
        """Mac 上没有 Teams 桌面版字幕读取（只用浏览器扩展），保留同样的接口。"""
        captions = Signal(dict)
        status = Signal(dict)

        def start(self):
            pass

        def stop(self, wait=0):
            pass

class UpdateSignals(QObject):
    """检查/下载更新在后台线程里做，结果通过信号回到界面线程。"""
    checked = Signal(object, object, bool)   # 新版本信息或 None、错误、是否手动
    progress = Signal(int)
    downloaded = Signal(object, object)      # 文件路径、错误


APP_NAME = "泰语字幕翻译"
APP_VERSION = "1.5.11"
log = logging.getLogger("app")
INSTANCE = "ThaiCaption-instance"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
# 文件夹版：exe 旁边的 extension；单文件版：打包在 exe 里（解压到 _MEIPASS）
EXTENSION_DIR = next((d for d in (Path(sys.executable).parent / "extension", ROOT / "extension")
                      if getattr(sys, "frozen", False) and d.is_dir()), ROOT / "extension")
MAX_ROWS = 300
CAPTION_HANDOVER = 8   # 网页版、桌面版同时出字幕时，当前来源停多少秒后才换另一边
THEMES = [("浅色", Theme.LIGHT), ("深色", Theme.DARK), ("跟随系统", Theme.AUTO)]
# Win11 DWMWA_SYSTEMBACKDROP_TYPE：2=云母 3=亚克力（模糊透出窗口后面的内容） 4=云母 Alt；0 表示不用特效
BACKDROPS = [("云母", 2), ("亚克力（半透明）", 3), ("云母 Alt", 4), ("纯色", 0)]
LANGS = ["中文", "English", "日语", "泰语"]
GREY = (QColor("#606060"), QColor("#a0a0a0"))


def app_icon():
    for base in (Path(getattr(sys, "_MEIPASS", "")) / "assets", ROOT / "assets"):
        if (base / "app.png").exists():
            return QIcon(str(base / "app.png"))
    return QIcon()


class Worker(QObject):
    """在后台线程跑耗时操作（测试连接、取模型列表），结果回到界面线程。"""
    finished = Signal(object, object)   # (回调, (结果, 错误))

    def __init__(self):
        super().__init__()
        self.finished.connect(lambda cb, res: cb(*res))

    def run(self, fn, cb):
        def task():
            try:
                self.finished.emit(cb, (fn(), None))
            except Exception as e:  # noqa: BLE001
                self.finished.emit(cb, (None, str(e) or type(e).__name__))
        threading.Thread(target=task, daemon=True).start()


def page(name):
    area = ScrollArea()
    area.setObjectName(name)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.enableTransparentBackground()
    view = QWidget()
    view.setObjectName(name + "View")
    view.setStyleSheet(f"#{name}View{{background:transparent}}")
    area.setWidget(view)
    lay = QVBoxLayout(view)
    lay.setContentsMargins(36, 28, 36, 28)
    lay.setSpacing(12)
    return area, lay


def widget_card(icon, title, content, *widgets):
    card = SettingCard(icon, title, content)
    for w in widgets:
        card.hBoxLayout.addWidget(w)
        card.hBoxLayout.addSpacing(8)
    card.hBoxLayout.addSpacing(8)
    return card


class WatchDialog(MessageBoxBase):
    """在局域网里找正在共享的主机，或手动输入主机地址。"""

    def __init__(self, parent, cfg, worker):
        super().__init__(parent)
        self.cfg, self.worker, self.items = cfg, worker, []
        self.viewLayout.addWidget(SubtitleLabel("观看共享字幕"))
        tip = CaptionLabel("看同一局域网里其他电脑共享的字幕：本机不需要装扩展、不用开 Teams，也不调用翻译模型。")
        tip.setWordWrap(True)
        tip.setTextColor(*GREY)
        self.viewLayout.addWidget(tip)
        self.list = ListWidget()
        self.list.setMinimumHeight(160)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        self.list.currentRowChanged.connect(lambda _: self._update_ok())
        self.viewLayout.addWidget(self.list)
        row = QHBoxLayout()
        self.state = CaptionLabel()
        self.state.setTextColor(*GREY)
        refresh = PushButton(FIF.SYNC, "重新查找")
        refresh.clicked.connect(self.scan)
        row.addWidget(self.state, 1)
        row.addWidget(refresh)
        self.viewLayout.addLayout(row)
        self.manual = LineEdit()
        self.manual.setPlaceholderText("或粘贴主机的共享链接，例如 http://192.168.1.5:47322/#k=123456")
        clip = QApplication.clipboard().text().strip()
        self.manual.setText(clip if "#k=" in clip and clip.startswith(("http://", "https://")) and len(clip) < 200
                            else cfg.get("watch_last", ""))
        self.manual.textChanged.connect(lambda _: self._update_ok())
        self.viewLayout.addWidget(self.manual)
        self.yesButton.setText("观看")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(560)
        self.scan()

    def scan(self):
        self.state.setText("正在局域网里查找…")

        def done(found, err):
            self.list.clear()
            self.items = found or []
            for h in self.items:
                self.list.addItem(f"{h['name']}    {h['ip']}")
            if self.items:
                self.list.setCurrentRow(0)
            self.state.setText(err or (f"找到 {len(self.items)} 台正在共享的电脑" if self.items
                                       else "没找到正在共享的电脑（确认在同一个局域网，或手动输入地址）"))
            self._update_ok()
        self.worker.run(discover, done)

    def _update_ok(self):
        self.yesButton.setEnabled(bool(self.manual.text().strip()) or self.list.currentRow() >= 0)

    def selected(self):
        """返回 (名称, 地址, 观看码)；手动输入优先。"""
        text = self.manual.text().strip()
        if text and "#k=" in text:
            url, code = parse_address(text)
            self.cfg["watch_last"] = text
            return url.split("//", 1)[-1], url, code
        i = self.list.currentRow()
        if 0 <= i < len(self.items):
            h = self.items[i]
            return h["name"], h["url"], h["code"]
        return None


class MainWindow(FluentWindow):
    def __init__(self, cfg, server):
        super().__init__()
        self.cfg = cfg
        self.quitting = False
        self.notified = False
        self.ext_status = None
        self.ext_status_at = 0.0
        self.ext_hello = (None, -1e9)   # (扩展版本, 最近一次报到的时间)
        self.desk_status = None         # Teams 桌面版的状态（软件自己读取）
        self.desk_status_at = 0.0
        self.caption_owner = (None, -1e9)   # (正在出字幕的来源 ext/desktop, 最近一次收到的时间)
        self.last_caption_at = 0.0
        self.tr_state = ("等待字幕", None)
        self.dirty = False
        self.session_file = None
        self.viewing = None             # 正在观看的共享名称；None 表示本机模式
        self.share_viewers = 0
        self.watch_note = ""

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        avail = QApplication.primaryScreen().availableGeometry()
        w, h = min(940, avail.width() - 40), min(800, avail.height() - 40)
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2)
        self.setMicaEffectEnabled(True)
        self.navigationInterface.setExpandWidth(180)
        # 让导航栏在常规窗口宽度下内嵌展开（共用窗口背景材质），窄窗口弹出时也用亚克力
        self.navigationInterface.setMinimumExpandWidth(700)
        self.navigationInterface.setAcrylicEnabled(True)

        self.worker = Worker()
        self.translator = Translator(cfg)
        self.tracker = Tracker(self.translator, cfg)
        self.bridge = Bridge()
        self.desktop = TeamsDesktopSource()
        self.overlay = Overlay(cfg)
        self.server = ShareServer()
        self.asr = AsrSource(cfg)
        self.keeper = ModelKeeper(cfg)
        self.model_states, self.model_err = {}, ""
        self.asr_state = ("未启动", None)
        self.asr_level = 0.0
        self.watcher = Watcher()
        self.remote = RemoteStore()

        self._build_home()
        self._build_settings()
        self._build_help()
        self.addSubInterface(self.home, FIF.CHAT, "字幕")
        self.addSubInterface(self.settings, FIF.SETTING, "设置")
        self.addSubInterface(self.help, FIF.HELP, "使用说明", position=NavigationItemPosition.BOTTOM)
        self.navigationInterface.addItem("theme", FIF.CONSTRACT, "切换深浅色", onClick=self.toggle_theme,
                                         selectable=False, position=NavigationItemPosition.BOTTOM,
                                         tooltip="切换深色 / 浅色")
        self._build_tray()
        self._init_updates()

        server.newConnection.connect(lambda: self._on_instance(server))
        self.bridge.captions.connect(lambda body: self._on_captions(body, "ext"))
        self.desktop.captions.connect(lambda body: self._on_captions(body, "desktop"))
        self.desktop.status.connect(self._on_desk_status)
        self.asr.items.connect(self._on_asr_items)
        self.keeper.status.connect(lambda st, err: (setattr(self, "model_states", st), setattr(self, "model_err", err)))
        self.translator.done.connect(lambda *_: self.tr_models() and self.keeper.touched(self.cfg["model"]))
        self.asr.finalized.connect(self.tracker.finalize)
        self.asr.status.connect(lambda text, ok: setattr(self, "asr_state", (text, ok)))
        self.asr.level.connect(lambda v: setattr(self, "asr_level", v))
        self.bridge.status.connect(self._on_ext_status)
        self.bridge.hello.connect(self._on_hello)
        # 本机字幕：共享中就推给共享服务；观看别人时不显示
        self.tracker.added.connect(self._on_local_added)
        self.tracker.changed.connect(self._on_local_changed)
        self.tracker.cleared.connect(self._on_local_cleared)
        self.remote.added.connect(lambda seg: self.viewing is not None and self._on_seg_added(seg))
        self.remote.changed.connect(lambda seg: self.viewing is not None and self._on_seg_changed(seg))
        self.remote.cleared.connect(lambda: self.viewing is not None and self._on_cleared())
        self.server.viewers_changed.connect(lambda n: setattr(self, "share_viewers", n))
        self.watcher.snapshot.connect(self._on_watch_snapshot)
        self.watcher.update.connect(self.remote.apply)
        self.watcher.state.connect(lambda t: setattr(self, "watch_note", t))
        self.watcher.ended.connect(self._on_watch_ended)
        self.translator.done.connect(self._on_tr_done)
        self.translator.failed.connect(self._on_tr_failed)
        self.translator.warned.connect(self._on_tr_warned)
        self.overlay.geometry_changed.connect(lambda g: self.set_option("overlay_geometry", list(g)))
        self.overlay.option_changed.connect(self.set_option)
        self.overlay.hide_requested.connect(lambda: self.set_option("overlay_visible", False))

        speed = lambda: int(self.cfg.get("touchpad_speed", 40))
        self._touchpad = [tame(a, speed) for a in (self.list_area, self.home, self.settings, self.help)]
        self.save_timer = QTimer(self, singleShot=True, interval=400, timeout=lambda: config.save(self.cfg))
        QTimer(self, interval=1000, timeout=self._refresh_status).start()
        QTimer(self, interval=15000, timeout=self._autosave).start()

        if INSTALL_DIR.exists():
            try:
                sync_extension(EXTENSION_DIR)   # 软件升级后顺带更新已安装的扩展文件
            except OSError:
                pass
        err = self.bridge.start(int(cfg["port"]))
        log.info("本地接收端口 %s：%s", cfg["port"], err or "正常")
        if err:
            QTimer.singleShot(500, lambda: self.toast(False, "本地端口不可用", err + "（可在设置里换端口，扩展端也要同步修改）"))
        if cfg.get("overlay_visible"):
            self.overlay.show()
        if cfg.get("source") == "asr":
            self.asr.start()
        else:
            self.desktop.start()
        self._refresh_status()

    # ---------- 页面 ----------
    def _build_home(self):
        self.home, lay = page("home")
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("实时字幕"))
        head.addStretch()
        self.source_tabs = SegmentedWidget()
        self.source_tabs.addItem("teams", "Teams 字幕", lambda: self.set_source("teams"))
        self.source_tabs.addItem("asr", "语音识别", lambda: self.set_source("asr"))
        self.source_tabs.setCurrentItem(self.cfg.get("source", "teams"))
        self.source_tabs.setVisible(plat.HAS_ASR)
        head.addWidget(self.source_tabs)
        lay.addLayout(head)
        self.ext_label = BodyLabel()
        self.tr_label = BodyLabel()
        self.share_label = BodyLabel()
        for lab in (self.ext_label, self.tr_label, self.share_label):
            lab.setTextColor(*GREY)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        self.level_bar = ProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setFixedWidth(220)
        lay.insertWidget(lay.indexOf(self.ext_label) + 1, self.level_bar)

        # 按钮放不下时自动折到下一行，窗口拉窄也不会把页面撑宽、右边被截掉
        bar_box = QWidget()
        bar = FlowLayout(bar_box, isTight=True)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setHorizontalSpacing(8)
        bar.setVerticalSpacing(8)
        self.overlay_btn = PrimaryPushButton(FIF.VIEW, "")
        self.overlay_btn.clicked.connect(lambda: self.set_option("overlay_visible", not self.cfg["overlay_visible"]))
        record_btn = DropDownPushButton(FIF.HISTORY, "记录")
        menu = RoundMenu(parent=record_btn)
        menu.addActions([Action(FIF.COPY, "复制全部字幕", triggered=self.copy_transcript),
                         Action(FIF.SAVE, "导出…", triggered=self.export_transcript),
                         Action(FIF.FOLDER, "打开历史记录文件夹",
                                triggered=lambda: self.open_path(config.TRANSCRIPT_DIR)),
                         Action(FIF.DOCUMENT, "打开日志文件夹（排查问题用）",
                                triggered=lambda: self.open_path(applog.LOG_DIR)),
                         Action(FIF.DELETE, "清空当前字幕", triggered=self.clear_transcript)])
        record_btn.setMenu(menu)
        self.install_btn = PushButton(FIF.DOWNLOAD, "安装扩展")
        self.install_btn.clicked.connect(self.install_extension)
        self.share_btn = PushButton(FIF.SHARE, "共享字幕")
        self.share_btn.clicked.connect(self.toggle_share)
        self.watch_btn = PushButton(FIF.PEOPLE, "观看共享")
        self.watch_btn.clicked.connect(self.toggle_watch)
        self.qr_btn = PushButton(FIF.QRCODE, "二维码")
        self.qr_btn.clicked.connect(self.show_share_dialog)
        for b in (self.overlay_btn, self.share_btn, self.qr_btn, self.watch_btn, record_btn, self.install_btn):
            bar.addWidget(b)
        lay.addWidget(bar_box)

        card = SimpleCardWidget()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(4, 0, 0, 0)
        # 一整块只读文本：宽度跟随窗口换行，可以跨多条拖选、Ctrl+A 全选、Ctrl+C 复制
        self.list_area = TranscriptView(MAX_ROWS, "还没有字幕。Teams 字幕模式：用 Teams 桌面版或浏览器开会并打开实时字幕；"
                                                  "语音识别模式：电脑开始播放泰语声音后会自动出现。")
        self.list_area.copy_all_requested.connect(self.copy_transcript)
        cl.addWidget(self.list_area)
        lay.addWidget(card, 1)
        self.list_area.verticalScrollBar().rangeChanged.connect(self._stick_bottom)
        self.stick = True
        self.list_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _build_settings(self):
        self.settings, lay = page("settings")
        lay.addWidget(TitleLabel("设置"))
        cfg = self.cfg

        g = SettingCardGroup("翻译服务")
        self.tr_group = g
        self.mode_box = ComboBox()
        self.mode_box.addItems(["本地模型（llama-swap）", "线上 API"])
        self.mode_box.setCurrentIndex(1 if cfg.get("translate_mode") == "online" else 0)
        self.mode_box.setMinimumWidth(220)
        self.mode_box.currentIndexChanged.connect(lambda i: self.set_option("translate_mode", "online" if i else "local"))
        g.addSettingCard(widget_card(FIF.GLOBE, "翻译走哪里", "本地：tiger-pc 上的模型；线上：填好地址、密钥和模型名的 OpenAI 兼容接口",
                                     self.mode_box))
        self.url_edit = LineEdit()
        self.url_edit.setText(cfg["api_base"])
        self.url_edit.setPlaceholderText("http://<tiger-pc 的 Tailscale 地址>:8080/v1")
        self.url_edit.setFixedWidth(320)
        self.url_edit.editingFinished.connect(lambda: self.set_option("api_base", self.url_edit.text().strip()))
        self.key_edit = PasswordLineEdit()
        self.key_edit.setText(cfg["api_key"])
        self.key_edit.setFixedWidth(320)
        self.key_edit.editingFinished.connect(lambda: self.set_option("api_key", self.key_edit.text().strip()))
        self.model_box = EditableComboBox()
        self.model_box.addItems(sorted({cfg["model"], "hymt2", "hymt2-32k"}))
        self.model_box.setText(cfg["model"])
        self.model_box.setFixedWidth(220)
        self.model_box.currentTextChanged.connect(lambda t: t.strip() and self.set_option("model", t.strip()))
        refresh = PushButton(FIF.SYNC, "刷新")
        refresh.clicked.connect(self.refresh_models)
        self.lang_box = ComboBox()
        self.lang_box.addItems(LANGS if cfg["target_lang"] in LANGS else LANGS + [cfg["target_lang"]])
        self.lang_box.setCurrentText(cfg["target_lang"])
        self.lang_box.currentTextChanged.connect(lambda t: self.set_option("target_lang", t))
        test = PrimaryPushButton(FIF.SEND, "测试翻译")
        test.clicked.connect(self.test_connection)
        self.local_cards = [
            widget_card(FIF.LINK, "接口地址", "OpenAI 兼容接口，远程电脑需先连上 Tailscale", self.url_edit),
            widget_card(FIF.FINGERPRINT, "API 密钥", "llama-swap 的密钥", self.key_edit),
            widget_card(FIF.ROBOT, "模型", "推荐 hymt2；可点“刷新”从服务器读取可用模型", self.model_box, refresh)]
        self.online_url = LineEdit()
        self.online_url.setText(cfg.get("online_api_base", ""))
        self.online_url.setPlaceholderText("https://…/v1")
        self.online_url.setFixedWidth(320)
        self.online_url.editingFinished.connect(
            lambda: self.set_option("online_api_base", self.online_url.text().strip()))
        self.online_key = PasswordLineEdit()
        self.online_key.setText(cfg.get("online_api_key", ""))
        self.online_key.setPlaceholderText("sk-…")
        self.online_key.setFixedWidth(320)
        self.online_key.editingFinished.connect(
            lambda: self.set_option("online_api_key", self.online_key.text().strip()))
        self.online_model = LineEdit()
        self.online_model.setText(cfg.get("online_model", ""))
        self.online_model.setFixedWidth(220)
        self.online_model.editingFinished.connect(
            lambda: self.set_option("online_model", self.online_model.text().strip()))
        self.thinking_box = ComboBox()
        self.thinking_box.addItems(["关闭（推荐）", "low"])
        self.thinking_box.setCurrentIndex(1 if cfg.get("online_thinking") == "low" else 0)
        self.thinking_box.setMinimumWidth(160)
        self.thinking_box.currentIndexChanged.connect(
            lambda i: self.set_option("online_thinking", "low" if i else "off"))
        self.online_cards = [
            widget_card(FIF.LINK, "线上接口地址", "OpenAI 兼容接口，一般以 /v1 结尾",
                        self.online_url),
            widget_card(FIF.FINGERPRINT, "线上 API 密钥", "只保存在本机设置里，不会写进日志", self.online_key),
            widget_card(FIF.ROBOT, "线上模型名称", "服务商给的模型名，例如 gpt-5.6-luna、deepseek-flash", self.online_model),
            widget_card(FIF.SPEED_HIGH, "思考模式", "关闭最快；low 翻得更细，但每句要多等几秒（只影响定稿，临时译文总是关闭）",
                        self.thinking_box)]
        for c in self.local_cards + self.online_cards:
            g.addSettingCard(c)
        g.addSettingCard(widget_card(FIF.LANGUAGE, "译成", "测试会让服务器加载所选模型", self.lang_box, test))
        lay.addWidget(g)
        self._show_tr_cards()

        g = SettingCardGroup("字幕共享")
        name_edit = LineEdit()
        name_edit.setText(cfg.get("share_name", ""))
        name_edit.setPlaceholderText(self.default_share_name())
        name_edit.setFixedWidth(320)
        name_edit.editingFinished.connect(lambda: self.set_option("share_name", name_edit.text().strip()))
        g.addSettingCard(widget_card(FIF.EDIT, "共享名称", "别人在“观看共享”列表里看到的名字", name_edit))
        share_port = SpinBox()
        share_port.setRange(1024, 65535)
        share_port.setValue(int(cfg.get("share_port", 47322)))
        share_port.setFixedWidth(160)
        share_port.editingFinished.connect(lambda: self.set_option("share_port", share_port.value()))
        g.addSettingCard(widget_card(FIF.CONNECT, "共享端口", "局域网里观看用的端口，下次开始共享时生效", share_port))
        reset = PushSettingCard("重置", FIF.SYNC, "观看码", "重置后，之前扫过码的手机要重新扫码")
        reset.clicked.connect(self.reset_view_code)
        g.addSettingCard(reset)
        lay.addWidget(g)

        g = SettingCardGroup("语音识别")
        g.setVisible(plat.HAS_ASR)
        asr_model = LineEdit()
        asr_model.setText(cfg.get("asr_model", "whisper-th"))
        asr_model.setFixedWidth(220)
        asr_model.editingFinished.connect(lambda: self.set_option("asr_model", asr_model.text().strip() or "whisper-th"))
        g.addSettingCard(widget_card(FIF.MICROPHONE, "识别模型", "tiger-pc 上 llama-swap 里的语音识别模型名", asr_model))
        self.device_box = ComboBox()
        self.device_box.setMinimumWidth(320)
        self.device_box.currentTextChanged.connect(
            lambda t: t and self.set_option("asr_device", "" if t.startswith("默认") else t))
        dev_refresh = PushButton(FIF.SYNC, "刷新")
        dev_refresh.clicked.connect(self.refresh_devices)
        g.addSettingCard(widget_card(FIF.SPEAKERS, "录哪个设备的声音", "开会时声音从哪个耳机 / 音箱放出来就选哪个",
                                     self.device_box, dev_refresh))
        lay.addWidget(g)
        self.refresh_devices()

        g = SettingCardGroup("翻译方式")
        self.switch(g, FIF.SPEED_HIGH, "边说边翻", "说话过程中先显示临时译文，说完后用定稿译文覆盖", "live_preview")
        self.switch(g, FIF.HISTORY, "参考上文", "把前两句原文一起发给模型，代词和省略翻得更准", "use_context")
        topic = LineEdit()
        topic.setText(cfg.get("meeting_topic", ""))
        topic.setPlaceholderText("例如：保险行业会议（理赔、核保、保单）")
        topic.setFixedWidth(360)
        topic.editingFinished.connect(lambda: self.set_option("meeting_topic", topic.text().strip()))
        g.addSettingCard(widget_card(FIF.TAG, "会议背景", "告诉线上模型这是什么行业的会议，专业词翻得更准（本地 hymt2 不用）",
                                     topic))
        gl = PlainTextEdit()
        gl.setPlainText(cfg.get("glossary", ""))
        gl.setPlaceholderText("每行一条：泰文=译文，例如\nสมชาย=颂猜\nไตรมาส=季度")
        gl.setFixedHeight(120)
        gl.textChanged.connect(lambda: self.set_option("glossary", gl.toPlainText()))
        glc = SettingCard(FIF.DICTIONARY, "术语表", "人名、公司名、专有名词的固定译法；原文里出现时才会发给模型")
        glc.setFixedHeight(180)
        glc.hBoxLayout.addWidget(gl, 1)
        glc.hBoxLayout.addSpacing(16)
        g.addSettingCard(glc)
        lay.addWidget(g)

        g = SettingCardGroup("浮窗")
        self.switch(g, FIF.VIEW, "显示浮窗", "置顶显示最近几条字幕，空白处可拖动、拖右下角改大小，文字可选中复制", "overlay_visible")
        self.switch(g, FIF.FONT, "显示泰文原文", "在译文下方用小字显示原文", "overlay_show_source")
        self.switch(g, FIF.TRANSPARENT, "鼠标穿透", "开启后浮窗不挡鼠标点击；从托盘或这里关闭", "click_through")
        if plat.HAS_BACKDROP:
            self.switch(g, FIF.BACKGROUND_FILL, "毛玻璃背景", "模糊浮窗后面的画面，底色深浅由“背景不透明度”控制",
                        "overlay_blur")
        self.slider(g, FIF.FONT_SIZE, "字号", f"也可以在浮窗上按 {plat.MOD}滚轮", "overlay_font", 12, 60)
        self.slider(g, FIF.ALIGNMENT, "显示条数", "浮窗里保留最近几条", "overlay_lines", 1, 8)
        self.slider(g, FIF.BRIGHTNESS, "背景不透明度", "%", "overlay_opacity", 20, 100)
        reset = PushSettingCard("复位", FIF.PIN, "浮窗位置", "浮窗跑到屏幕外时点这里")
        reset.clicked.connect(lambda: (self.overlay.reset_position(), self.set_option("overlay_visible", True)))
        g.addSettingCard(reset)
        lay.addWidget(g)

        g = SettingCardGroup("连接")
        port = SpinBox()
        port.setRange(1024, 65535)
        port.setValue(int(cfg["port"]))
        port.setFixedWidth(160)
        port.editingFinished.connect(lambda: self.change_port(port.value()))
        g.addSettingCard(widget_card(FIF.CONNECT, "本机端口", "浏览器扩展把字幕发到这个端口（默认 47321，改了要同步改扩展）", port))
        ext = PushSettingCard("安装", FIF.DOWNLOAD, "浏览器扩展", "Chrome / Edge 扩展，负责把 Teams 字幕转给本软件")
        ext.clicked.connect(self.install_extension)
        g.addSettingCard(ext)
        lay.addWidget(g)

        g = SettingCardGroup("日志")
        logs = PushSettingCard("打开", FIF.DOCUMENT, "日志文件夹",
                               f"{applog.LOG_DIR}（每天一个文件，保留 7 天；出问题时把当天的日志发出来）")
        logs.clicked.connect(lambda: self.open_path(applog.LOG_DIR))
        g.addSettingCard(logs)
        self.switch(g, FIF.DEVELOPER_TOOLS, "详细日志", "连每次临时识别 / 临时翻译都记录，排查问题时再开", "debug_log")
        lay.addWidget(g)

        g = SettingCardGroup("外观")
        self.theme_box = ComboBox()
        self.theme_box.addItems([t[0] for t in THEMES])
        self.theme_box.setCurrentIndex(int(cfg.get("theme", 0)) % len(THEMES))
        self.theme_box.setMinimumWidth(120)
        self.theme_box.currentIndexChanged.connect(self.change_theme)
        g.addSettingCard(widget_card(FIF.BRUSH, "主题", "浅色 / 深色 / 跟随系统设置", self.theme_box))
        self.backdrop_box = ComboBox()
        self.backdrop_box.addItems([b[0] for b in BACKDROPS])
        self.backdrop_box.setCurrentIndex(int(cfg.get("backdrop", 0)) % len(BACKDROPS))
        self.backdrop_box.setMinimumWidth(160)
        self.backdrop_box.currentIndexChanged.connect(self.set_backdrop)
        backdrop_card = widget_card(FIF.TRANSPARENT, "背景材质", "Win11 窗口特效；窗口失去焦点时系统会自动显示为纯色",
                                    self.backdrop_box)
        g.addSettingCard(backdrop_card)
        self.tint_slider = Slider(Qt.Horizontal)
        self.tint_slider.setRange(0, 95)
        self.tint_slider.setValue(int(cfg.get("tint", 55)))
        self.tint_slider.setFixedWidth(220)
        self.tint_value = BodyLabel(f"{self.tint_slider.value()}%")
        self.tint_value.setFixedWidth(44)
        self.tint_slider.valueChanged.connect(self.set_tint)
        self.tint_card = widget_card(FIF.BRIGHTNESS, "亚克力遮罩", "越高文字越清晰，越低越通透",
                                     self.tint_slider, self.tint_value)
        g.addSettingCard(self.tint_card)
        if not plat.HAS_BACKDROP:
            backdrop_card.hide()
            self.tint_card.hide()
        self.slider(g, FIF.SCROLL, "触控板滚动速度", "字幕列表和设置页用触控板滚动时的速度（%），鼠标滚轮不受影响",
                    "touchpad_speed", 10, 100)
        lay.addWidget(g)
        lay.addStretch()

    def _build_help(self):
        self.help, lay = page("help")
        lay.addWidget(TitleLabel("使用说明"))
        steps = [
            ("1. 连接翻译服务",
             "在运行模型的电脑上启动 llama-swap。其他电脑要先安装 Tailscale 并加入同一网络，"
             "然后在“设置”里填好接口地址和密钥，点“测试翻译”。"
             "也可以在“设置 → 翻译走哪里”选“线上 API”，填线上服务的地址、密钥和模型名，不用连 tiger-pc。"),
            ("2. Teams 桌面版：直接入会就行",
             "“字幕”页右上角选“Teams 字幕”，软件会自动读取 Teams 桌面版会议窗口里的实时字幕和说话人，"
             "不用装扩展、不用开别的程序，Teams 窗口最小化或被挡住也能读。"),
            ("用浏览器开会：先装浏览器扩展（每台电脑只需一次）",
             "点“字幕”页上的“安装扩展”，按向导操作（支持 Chrome 和 Edge）。"
             "装好后浏览器工具栏的扩展图标显示 ON 表示已连上本软件，显示 ! 表示本软件没开。"
             "网页版和桌面版可以都装着，软件会自动用正在出字幕的那一边，同一句不会翻译两次。"),
            ("不想用 Teams 字幕？用语音识别",
             "在“字幕”页右上角切到“语音识别”：软件直接录电脑放出来的声音（Teams、Zoom、视频都行），"
             "交给 tiger-pc 上的泰语识别模型转成文字再翻译。没有说话人姓名；声音要从选定的扬声器 / 耳机放出来。"),
            ("3. 入会并开启字幕",
             "用 Teams 桌面版（或 Edge/Chrome 打开 teams.microsoft.com）入会 → “更多” → “语言和语音” → “显示实时字幕”。"
             "然后在字幕设置里把“会议口语语言”设为泰语，否则泰语会被当成英语识别。"),
            ("4. 看译文",
             "字幕会实时出现在本窗口和置顶浮窗里。灰色是“正在说”的临时译文，说完一句后变成定稿。"
             "记录每 15 秒自动保存到“历史记录”文件夹，也可以手动导出。"),
            ("5. 共享给其他电脑（可选）",
             "开会的这台电脑点“共享字幕”，会弹出二维码。同一个 Wi-Fi / 局域网里的手机或电脑扫码，用浏览器就能看，"
             "不用装任何东西；装了本软件的电脑点“观看共享”会自动找到它，还能用浮窗看。点“停止共享”随时取消。"),
        ]
        if not plat.HAS_TEAMS_DESKTOP:
            # Mac 版：只能用浏览器开会；没有语音识别
            steps = [s for s in steps if not s[0].startswith(("2. Teams 桌面版", "不想用 Teams 字幕"))]
            steps.insert(1, ("2. 用浏览器开会：先装浏览器扩展（每台电脑只需一次）",
                             "Mac 版只能读取 Teams 网页版的字幕：用 Chrome 或 Edge 打开 teams.microsoft.com 开会。"
                             "点“字幕”页上的“安装扩展”，按向导操作。装好后浏览器工具栏的扩展图标显示 ON 表示已连上本软件。"
                             "Teams 桌面版字幕和语音识别目前只有 Windows 版支持。"))
            steps = [s for s in steps if not s[0].startswith("用浏览器开会：")]
        for title, body in steps:
            card = SimpleCardWidget()
            c = QVBoxLayout(card)
            c.setContentsMargins(24, 16, 24, 16)
            c.addWidget(StrongBodyLabel(title))
            t = BodyLabel(body)
            t.setWordWrap(True)
            c.addWidget(t)
            lay.addWidget(card)
        lay.addStretch()

    def switch(self, group, icon, title, content, key):
        sw = SwitchButton()
        sw.setOnText("开")
        sw.setOffText("关")
        sw.setChecked(bool(self.cfg.get(key)))
        sw.checkedChanged.connect(lambda v: self.set_option(key, v))
        setattr(self, f"sw_{key}", sw)
        group.addSettingCard(widget_card(icon, title, content, sw))

    def slider(self, group, icon, title, content, key, lo, hi):
        s = Slider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(self.cfg.get(key)))
        s.setFixedWidth(200)
        val = BodyLabel(str(s.value()))
        val.setFixedWidth(32)
        s.valueChanged.connect(lambda v: (val.setText(str(v)), self.set_option(key, v)))
        setattr(self, f"sl_{key}", s)
        group.addSettingCard(widget_card(icon, title, content, val, s))

    def _build_tray(self):
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        # Windows 上托盘右键菜单用亚克力样式（截取菜单后面的屏幕做模糊，半透明圆角）；Mac 用普通的
        if plat.HAS_BACKDROP:
            from qfluentwidgets.components.material import AcrylicSystemTrayMenu
            menu = AcrylicSystemTrayMenu(parent=self)
        else:
            menu = SystemTrayMenu(parent=self)
        self.act_overlay = Action(FIF.VIEW, "显示 / 隐藏浮窗",
                                  triggered=lambda: self.set_option("overlay_visible", not self.cfg["overlay_visible"]))
        self.act_through = Action(FIF.TRANSPARENT, "取消鼠标穿透",
                                  triggered=lambda: self.set_option("click_through", not self.cfg["click_through"]))
        menu.addActions([Action(FIF.HOME, "显示主窗口", triggered=self.show_window), self.act_overlay, self.act_through])
        menu.addSeparator()
        self.act_update = Action(FIF.SYNC, "检查更新…", triggered=lambda: self.check_updates(manual=True))
        menu.addAction(self.act_update)
        menu.addSeparator()
        menu.addActions([Action(FIF.CLOSE, "退出", triggered=self.quit_app)])
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_window() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    # ---------- 检查更新 ----------
    def _init_updates(self):
        """启动 20 秒后检查一次，之后每 12 小时检查一次；发现新版本时托盘弹出提示，点提示或托盘菜单更新。"""
        self.update_info = None
        self.update_busy = False
        self.update_notified = None
        self.update_signals = UpdateSignals(self)
        self.update_signals.checked.connect(self._update_checked)
        self.update_signals.progress.connect(lambda p: self.act_update.setText(f"正在下载更新… {p}%"))
        self.update_signals.downloaded.connect(self._update_downloaded)
        self.tray.messageClicked.connect(lambda: self.update_info and self.install_update())
        QTimer.singleShot(20000, lambda: self.check_updates(manual=False))
        QTimer(self, interval=12 * 3600 * 1000, timeout=lambda: self.check_updates(manual=False)).start()

    def _set_update_action(self):
        busy, info = self.update_busy, self.update_info
        self.act_update.setEnabled(not busy)
        if not busy:
            self.act_update.setText(f"更新到 v{info['version']}…" if info else "检查更新…")

    def check_updates(self, manual):
        if self.update_busy:
            return
        self.update_busy = True
        self.act_update.setText("正在检查更新…")
        self._set_update_action()

        def work():
            try:
                self.update_signals.checked.emit(updater.check(APP_VERSION), None, manual)
            except Exception as e:  # noqa: BLE001
                self.update_signals.checked.emit(None, e, manual)
        threading.Thread(target=work, daemon=True, name="update-check").start()

    def _update_checked(self, info, err, manual):
        self.update_busy = False
        if err is not None:
            log.warning("检查更新失败：%s", err)
            self._set_update_action()
            if manual:
                self.toast(False, "检查更新失败", str(err))
            return
        self.update_info = info
        self._set_update_action()
        if not info:
            if manual:
                self.show_window()
                self.toast(True, "已经是最新版本", f"当前 v{APP_VERSION}")
            return
        log.info("发现新版本 v%s", info["version"])
        if manual:
            self.install_update()
        elif self.update_notified != info["version"]:
            self.update_notified = info["version"]
            self.tray.showMessage(f"{APP_NAME}有新版本 v{info['version']}",
                                  "点这里更新，或者在托盘菜单里选“更新到 v%s”。" % info["version"],
                                  QSystemTrayIcon.Information, 8000)

    def _ask(self, title, content, yes="确定", no="取消"):
        self.show_window()
        box = MessageBox(title, content, self)
        box.yesButton.setText(yes)
        box.cancelButton.setText(no)
        return bool(box.exec())

    def install_update(self):
        info = self.update_info
        if not info or self.update_busy:
            return
        notes = info["notes"] if len(info["notes"]) <= 1200 else info["notes"][:1200] + "…"
        if not updater.can_install(info):
            # Mac 版、源码运行：打开下载页面
            if self._ask(f"发现新版本 v{info['version']}", f"当前 v{APP_VERSION}。\n\n{notes}\n\n要打开下载页面吗？", "打开下载页面"):
                QDesktopServices.openUrl(QUrl(info["page"]))
            return
        if not self._ask(f"发现新版本 v{info['version']}",
                         f"当前 v{APP_VERSION}。\n\n{notes}\n\n现在下载并更新吗？更新时软件会先退出（字幕翻译会中断），完成后自动重新打开。",
                         "下载并更新", "以后再说"):
            return
        if not updater.can_write_app_dir():
            if self._ask("没法自动更新", "软件所在的文件夹不能写入（可能放在了 Program Files 里）。要打开下载页面手动更新吗？", "打开下载页面"):
                QDesktopServices.openUrl(QUrl(info["page"]))
            return
        self.update_busy = True
        self._set_update_action()
        self.act_update.setText("正在下载更新… 0%")
        log.info("开始下载 v%s：%s", info["version"], info["url"])

        def work():
            try:
                path = updater.download(info, self.update_signals.progress.emit)
                self.update_signals.downloaded.emit(path, None)
            except Exception as e:  # noqa: BLE001
                self.update_signals.downloaded.emit(None, e)
        threading.Thread(target=work, daemon=True, name="update-download").start()

    def _update_downloaded(self, path, err):
        if err is None:
            try:
                updater.start_install(path)
            except Exception as e:  # noqa: BLE001
                err = e
        self.update_busy = False
        self._set_update_action()
        if err is not None:
            log.warning("更新失败：%s", err)
            if self._ask("更新失败", f"{err}\n\n要打开下载页面手动更新吗？", "打开下载页面"):
                QDesktopServices.openUrl(QUrl(self.update_info["page"]))
            return
        log.info("更新文件已准备好，退出软件进行替换")
        self.quit_app()

    # ---------- 选项 ----------
    def set_option(self, key, value):
        if self.cfg.get(key) == value:
            return
        self.cfg[key] = value
        self.save_timer.start()
        if key not in ("overlay_geometry", "glossary", "overlay_font", "overlay_opacity", "tint"):
            log.info("设置 %s = %s", key, "（已填写）" if key in ("api_key", "online_api_key") and value else value)
        if key == "debug_log":
            applog.set_debug(bool(value))
        if key in ("api_base", "api_key", "model", "target_lang", "glossary", "translate_mode",
                   "online_api_base", "online_api_key", "online_model", "online_thinking", "meeting_topic"):
            self.translator.configure(self.cfg)
            self.tr_state = ("等待字幕", None)
        if key == "translate_mode":
            self._show_tr_cards()
        if key == "overlay_visible":
            self.overlay.setVisible(bool(value))
        if key in ("overlay_blur", "overlay_opacity"):
            self.overlay.apply_blur()
        if key == "click_through":
            self.overlay.set_click_through(value)
            if value:
                self.toast(True, "浮窗已开启鼠标穿透", "要拖动或调整浮窗时，从托盘图标菜单取消穿透")
        # 同步设置页上的开关和滑块（浮窗右键菜单也会改这些选项）
        for prefix in ("sw_", "sl_"):
            w = getattr(self, prefix + key, None)
            if w is not None and (w.isChecked() if prefix == "sw_" else w.value()) != value:
                w.setChecked(value) if prefix == "sw_" else w.setValue(value)
        self._refresh_overlay_controls()
        self.overlay.update()

    def _refresh_overlay_controls(self):
        on = self.cfg["overlay_visible"]
        self.overlay_btn.setText("隐藏浮窗" if on else "显示浮窗")
        self.act_through.setText("取消鼠标穿透" if self.cfg["click_through"] else "开启鼠标穿透")

    def change_port(self, port):
        if port == int(self.cfg["port"]):
            return
        err = self.bridge.start(port)
        if err:
            self.toast(False, "端口不可用", err)
            self.bridge.start(int(self.cfg["port"]))
            return
        self.set_option("port", port)
        self.toast(True, "端口已修改", f"记得把扩展 background.js 里的 PORT 也改成 {port}，再重新加载扩展")

    def change_theme(self, idx):
        setTheme(THEMES[idx][1])
        self.set_option("theme", idx)

    def toggle_theme(self):
        # 下拉框变化会触发 change_theme，顺带保存
        self.theme_box.setCurrentIndex(0 if isDarkTheme() else 1)

    def set_backdrop(self, idx):
        self.set_option("backdrop", idx)
        self.apply_backdrop()

    def apply_backdrop(self):
        if not plat.HAS_BACKDROP:
            return
        kind = BACKDROPS[self.backdrop_box.currentIndex()][1]
        self.tint_card.setEnabled(kind == 3)
        if kind == 0:
            if self.isMicaEffectEnabled():
                self.setMicaEffectEnabled(False)
            return
        if not self.isMicaEffectEnabled():
            self.setMicaEffectEnabled(True)  # 让控件库把窗口背景设为透明并扩展边框
        ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()), 38, ctypes.byref(ctypes.c_int(kind)), 4)
        self.setBackgroundColor(self._normalBackgroundColor())

    def set_tint(self, v):
        self.tint_value.setText(f"{v}%")
        self.set_option("tint", v)
        self.setBackgroundColor(self._normalBackgroundColor())

    def _normalBackgroundColor(self):
        # 亚克力时在窗口底层画一层半透明底色，压低背后内容的对比度，提升可读性
        if self.isMicaEffectEnabled() and hasattr(self, "backdrop_box")                 and BACKDROPS[self.backdrop_box.currentIndex()][1] == 3:
            c = QColor(32, 32, 32) if isDarkTheme() else QColor(243, 243, 243)
            c.setAlpha(int(self.cfg.get("tint", 55) / 100 * 255))
            return c
        return super()._normalBackgroundColor()

    # 控件库在显示窗口、切换主题后会重新设成云母，这里之后再套用用户选的材质
    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.apply_backdrop)
        if not getattr(self, "_nav_expanded", False) and self.width() >= 700:
            self._nav_expanded = True
            QTimer.singleShot(0, lambda: self.navigationInterface.expand(useAni=False))

    def _onThemeChangedFinished(self):
        super()._onThemeChangedFinished()
        QTimer.singleShot(0, self.apply_backdrop)

    def _show_tr_cards(self):
        online = self.cfg.get("translate_mode") == "online"
        for c in self.local_cards:
            c.setVisible(not online)
        for c in self.online_cards:
            c.setVisible(online)
        self.tr_group.adjustSize()

    def tr_models(self):
        """需要 llama-swap 常驻的翻译模型；走线上 API 时不需要。"""
        return set() if self.cfg.get("translate_mode") == "online" else {self.cfg["model"]}

    def refresh_models(self):
        def done(models, err):
            if err:
                return self.toast(False, "获取模型列表失败", err)
            cur = self.model_box.text()
            self.model_box.blockSignals(True)
            self.model_box.clear()
            self.model_box.addItems(models)
            self.model_box.setText(cur)
            self.model_box.blockSignals(False)
            self.toast(True, "模型列表已更新", "、".join(models))
        self.worker.run(lambda: list_models(self.cfg["api_base"], self.cfg["api_key"]), done)

    def test_connection(self):
        self.set_option("api_base", self.url_edit.text().strip())
        self.set_option("api_key", self.key_edit.text().strip())
        self.set_option("online_api_base", self.online_url.text().strip())
        self.set_option("online_api_key", self.online_key.text().strip())
        self.set_option("online_model", self.online_model.text().strip())
        cfg = active(self.cfg)
        if cfg["online"]:
            self.toast(True, "正在测试…", f"线上 API · 模型 {cfg['model']}")
        else:
            self.toast(True, "正在测试…", f"模型 {cfg['model']}，首次加载可能要几十秒")

        def run():
            t = time.perf_counter()
            out = translate(cfg, "สวัสดีครับ วันนี้เราจะคุยเรื่องแผนงานไตรมาสหน้า", timeout=None if cfg["online"] else 300)
            return out, time.perf_counter() - t

        def done(res, err):
            if err:
                return self.toast(False, "测试失败", err)
            self.toast(True, f"连接正常（{res[1]:.1f} 秒）", res[0])
        self.worker.run(run, done)

    # ---------- 字幕 ----------
    @property
    def store(self):
        return self.remote if self.viewing is not None else self.tracker

    def _on_local_added(self, seg):
        self.server.update(seg_to_dict(seg))
        if self.viewing is None:
            self._on_seg_added(seg)

    def _on_local_changed(self, seg):
        self.server.update(seg_to_dict(seg))
        if self.viewing is None:
            self._on_seg_changed(seg)

    def _on_local_cleared(self):
        self.server.clear()
        if self.viewing is None:
            self._on_cleared()

    def set_source(self, src):
        log.info("字幕来源切换为：%s", "语音识别" if src == "asr" else "Teams 字幕")
        self.set_option("source", src)
        if src == "asr" and self.viewing is None:
            self.asr.start()
        else:
            self.asr.stop()
            self.asr_state = ("未启动", None)
        if src == "asr":
            self.desktop.stop()
            self.desk_status = None
        else:
            self.desktop.start()
        self._refresh_status()

    def refresh_devices(self):
        if not plat.HAS_ASR:
            return
        try:
            devs = list_loopback_devices()
        except Exception:  # noqa: BLE001
            devs = []
        cur = self.cfg.get("asr_device", "")
        self.device_box.blockSignals(True)
        self.device_box.clear()
        self.device_box.addItems(["默认扬声器（跟随 Windows 设置）"] + devs)
        self.device_box.setCurrentIndex(devs.index(cur) + 1 if cur in devs else 0)
        self.device_box.blockSignals(False)

    def _on_asr_items(self, items):
        self.keeper.touched(self.cfg.get("asr_model"))
        if self.viewing is None and self.cfg.get("source") == "asr":
            self.tracker.feed(items)

    def _on_captions(self, body, src):
        now = time.monotonic()
        self.last_caption_at = now
        if self.viewing is not None or self.cfg.get("source") == "asr":
            return   # 观看别人共享、或用语音识别时，不处理 Teams 字幕
        # 网页版和桌面版同时在会议里时，两边是同一场会的同一批字幕：只用先出字幕的那一边，
        # 它停了 CAPTION_HANDOVER 秒以后才换成另一边，免得同一句翻译两次
        owner, at = self.caption_owner
        if owner not in (None, src) and now - at < CAPTION_HANDOVER:
            return
        if owner != src:
            log.info("Teams 字幕来源：%s", "桌面版" if src == "desktop" else "网页版（浏览器扩展）")
        self.caption_owner = (src, now)
        self.tracker.feed(body.get("items", []))

    def _on_desk_status(self, body):
        old = self.desk_status
        if old is None or (old.get("inMeeting"), old.get("captionsOn")) != (body.get("inMeeting"), body.get("captionsOn")):
            log.info("Teams 桌面版状态：在会议中=%s · 字幕已开=%s · 标题=%s", body.get("inMeeting"),
                     body.get("captionsOn"), str(body.get("title", ""))[:80])
        self.desk_status, self.desk_status_at = body, time.monotonic()

    def _on_hello(self, body):
        ver = str(body.get("version", "?"))
        if self.ext_hello[0] != ver:
            log.info("浏览器扩展已连接：v%s（%s）", ver, str(body.get("ua", ""))[:120])
        self.ext_hello = (ver, time.monotonic())

    def _on_ext_status(self, body):
        """Teams 页面里的每个框架（主页面、内嵌的空白框架等）都会各自报告状态，
        合并最近 10 秒内所有框架的报告：任何一个看到会议 / 字幕就算有，免得互相覆盖。"""
        now = time.monotonic()
        frames = getattr(self, "_ext_frames", {})
        frames[str(body.get("session", ""))] = (body, now)
        frames = {k: v for k, v in frames.items() if now - v[1] <= 10}
        self._ext_frames = frames
        bodies = [b for b, _ in frames.values()]
        main = next((b for b in bodies if b.get("captionsOn")), None) \
            or next((b for b in bodies if b.get("inMeeting")), None) \
            or next((b for b in bodies if b.get("host")), body)
        merged = dict(main, inMeeting=any(b.get("inMeeting") for b in bodies),
                      captionsOn=any(b.get("captionsOn") for b in bodies))
        key = (merged.get("host"), merged["inMeeting"], merged["captionsOn"])
        old = self.ext_status
        if old is None or key != (old.get("host"), bool(old.get("inMeeting")), bool(old.get("captionsOn"))):
            log.info("Teams 状态：%s · 在会议中=%s · 字幕已开=%s · 标题=%s", key[0], key[1], key[2],
                     str(merged.get("title", ""))[:80])
        self.ext_status, self.ext_status_at = merged, now

    def ext_version(self):
        """扩展最近 75 秒内报到过（或正在发 Teams 状态）就算已连上，返回版本号。"""
        ver, at = self.ext_hello
        now = time.monotonic()
        if now - at < 75:
            return ver
        if self.ext_status is not None and now - self.ext_status_at < 10:
            return ver or "?"
        return None

    def install_extension(self):
        try:
            InstallDialog(self, EXTENSION_DIR, self.ext_version).exec()
        except OSError as e:
            self.toast(False, "复制扩展文件失败", str(e))

    def _on_seg_added(self, seg):
        if self.session_file is None:
            prefix = "观看-" if self.viewing is not None else ""
            self.session_file = config.TRANSCRIPT_DIR / (prefix + time.strftime("%Y%m%d-%H%M%S.md",
                                                                                 time.localtime(seg.start)))
        self.list_area.add(seg)
        self._seg_updated()

    def _on_seg_changed(self, seg):
        self.list_area.update_seg(seg)
        self._seg_updated()

    def _seg_updated(self):
        self.dirty = True
        self.overlay.set_segments([self.store.segments[i] for i in self.store.order[-500:]])

    def _on_cleared(self):
        self.list_area.clear_all()
        self.overlay.set_segments([])

    def _on_tr_done(self, _sid, _src, _out, _final, elapsed):
        self.tr_state = (f"正常 · 最近一句 {elapsed:.1f} 秒", True)

    def _on_tr_warned(self, msg):
        self.tr_state = (f"注意：{msg}", None)
        now = time.monotonic()
        if now - getattr(self, "_tr_warn_at", -1e9) > 60:
            self._tr_warn_at = now
            self.toast(False, "线上 API 思考用光 max_tokens", f"{msg}。会议时建议把“思考模式”设为关闭")

    def _on_tr_failed(self, _sid, _src, err):
        self.tr_state = (f"出错：{err}（3 秒后重试）", False)
        # 线上 API 出错多半要用户处理（密钥、余额、网络），弹一次提示，1 分钟内不重复弹
        now = time.monotonic()
        if self.cfg.get("translate_mode") == "online" and now - getattr(self, "_online_err_at", -1e9) > 60:
            self._online_err_at = now
            self.toast(False, "线上 API 翻译失败", f"{err}。可在“设置 → 翻译走哪里”换回本地模型")

    def _on_scroll(self, v):
        bar = self.list_area.verticalScrollBar()
        self.stick = v >= bar.maximum() - 40

    def _stick_bottom(self, _lo, hi):
        if self.stick:
            self.list_area.verticalScrollBar().setValue(hi)

    def _refresh_status(self):
        green, red, grey = QColor("#2e7d32"), QColor("#c62828"), GREY[0]
        sharing = self.server.running
        self.share_btn.setText("停止共享" if sharing else "共享字幕")
        self.share_btn.setVisible(self.viewing is None)
        self.qr_btn.setVisible(sharing)
        self.watch_btn.setText("退出观看" if self.viewing is not None else "观看共享")
        self.watch_btn.setVisible(not sharing)
        if self.viewing is not None:
            note = f" · {self.watch_note}" if self.watch_note else ""
            self.ext_label.setText(f"● 正在观看「{self.viewing}」{note}")
            c = red if self.watch_note else green
            self.ext_label.setTextColor(c, c)
            self.tr_label.setText("● 翻译：由共享的电脑负责，本机不调用模型")
            self.tr_label.setTextColor(*GREY)
            self.keeper.need(set())
            self.share_label.hide()
            self.install_btn.hide()
            self._refresh_overlay_controls()
            return
        self.share_label.setVisible(sharing)
        if sharing:
            ip = self.cfg.get("share_ip") or "本机IP"
            self.share_label.setText(f"● 正在局域网共享（http://{ip}:{self.server.port}/，观看码 {self.server.code}）"
                                     f" · {self.share_viewers} 台设备在看")
            self.share_label.setTextColor(green, green)
        now = time.monotonic()
        st = self.ext_status
        asr_mode = self.cfg.get("source") == "asr"
        self.level_bar.setVisible(asr_mode)
        if asr_mode:
            asr_model = self.cfg.get("asr_model")
            # 正在听就需要识别 + 翻译两个模型常驻
            self.keeper.need(self.tr_models() | {asr_model} if self.asr.running else set())
            text, ok = self.asr_state
            mst = self.model_states.get(asr_model, "")
            if ok and mst and mst != "已就绪":
                text, ok = f"识别模型{mst}", None
            self.ext_label.setText(f"● 语音识别（{asr_model}）：{text}")
            c = grey if ok is None else (green if ok else red)
            self.ext_label.setTextColor(c, GREY[1] if ok is None else c)
            self.level_bar.setValue(int(self.asr_level * 100))
            self.install_btn.hide()
            self._render_tr()
            self._refresh_overlay_controls()
            return
        # Teams 模式：网页版（扩展报告）和桌面版（软件自己读）的状态，取最近 10 秒内的
        live = [s for s, at in ((st, self.ext_status_at), (self.desk_status, self.desk_status_at))
                if s is not None and now - at <= 10]
        # 字幕开着（或 10 分钟内收到过字幕）才让翻译模型常驻
        captions_on = any(s.get("captionsOn") for s in live)
        active = captions_on or (self.last_caption_at and now - self.last_caption_at < 600)
        self.keeper.need(self.tr_models() if active else set())
        on = next((s for s in live if s.get("captionsOn")), None)
        meeting = next((s for s in live if s.get("inMeeting")), None)
        if on:
            if len([s for s in live if s.get("captionsOn")]) > 1:
                owner = "桌面版" if self.caption_owner[0] == "desktop" else "网页版"
                text = f"Teams：字幕接收中（网页版和桌面版都开着字幕，正在用{owner}的）"
            else:
                text = f"Teams：字幕接收中（{on.get('host', '')}）"
        elif meeting:
            text = f"Teams：在会议中（{meeting.get('host', '')}），但还没打开实时字幕（更多 → 语言和语音 → 显示实时字幕）"
        elif live:
            text = "Teams：已连接（" + "、".join(s.get("host", "") for s in live) + "），当前不在会议中"
        elif self.ext_version():
            text = "Teams：扩展已连接，用 Teams 桌面版或浏览器打开 Teams 入会即可"
        elif not plat.HAS_TEAMS_DESKTOP:
            text = "Teams：用 Chrome / Edge 打开 Teams 网页版开会；第一次用要先点“安装扩展”"
        else:
            text = "Teams：用 Teams 桌面版入会即可；用浏览器开会的话，需要先点“安装扩展”"
        self.ext_label.setText("● " + text)
        self.install_btn.setVisible(not self.ext_version()
                                     and not (self.desk_status and now - self.desk_status_at <= 10))
        self.ext_label.setTextColor(green if on else grey, green if on else GREY[1])
        self._render_tr()
        self._refresh_overlay_controls()

    def _render_tr(self):
        """翻译状态一行：模型是否就绪 + 最近一次翻译的结果。"""
        green, red, grey = QColor("#2e7d32"), QColor("#c62828"), GREY[0]
        model = self.cfg["model"]
        if self.cfg.get("translate_mode") == "online":
            msg, ok = self.tr_state
            self.tr_label.setText(f"● 翻译（线上 API · {self.cfg.get('online_model')}）：{msg}")
            c = grey if ok is None else (green if ok else red)
            self.tr_label.setTextColor(c, GREY[1] if ok is None else c)
            return
        if self.model_err:
            self.tr_label.setText(f"● 翻译（{model}）：{self.model_err}")
            self.tr_label.setTextColor(red, red)
            return
        mst = self.model_states.get(model, "未加载")
        msg, ok = self.tr_state
        if mst == "已就绪":
            text = f"模型已就绪 · {msg}" if ok is not None else "模型已就绪，等待字幕"
        elif mst == "加载中…":
            text, ok = "模型加载中…（首次约 10 秒）", None
        else:
            text, ok = "模型未加载（开始收到字幕 / 声音时会自动加载）", None
        self.tr_label.setText(f"● 翻译（{model}）：{text}")
        c = grey if ok is None else (green if ok else red)
        self.tr_label.setTextColor(c, GREY[1] if ok is None else c)

    # ---------- 记录 ----------
    def transcript_text(self):
        lines = [f"# 会议字幕 {time.strftime('%Y-%m-%d %H:%M')}\n"]
        for sid in self.store.order:
            seg = self.store.segments[sid]
            stamp = time.strftime("%H:%M:%S", time.localtime(seg.start))
            who = f" {seg.speaker}" if seg.speaker else ""
            lines.append(f"**[{stamp}]{who}**  \n{seg.text}  \n{seg.zh or '（未翻译）'}\n")
        return "\n".join(lines)

    def _autosave(self):
        if not self.dirty or self.session_file is None or not self.store.order:
            return
        try:
            config.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            self.session_file.write_text(self.transcript_text(), encoding="utf-8")
            self.dirty = False
        except OSError:
            pass

    def copy_transcript(self):
        if not self.store.order:
            return self.toast(False, "没有可复制的字幕", "")
        lines = []
        for sid in self.store.order:
            seg = self.store.segments[sid]
            stamp = time.strftime("%H:%M:%S", time.localtime(seg.start))
            lines.append(f"[{stamp}]{' ' + seg.speaker if seg.speaker else ''}\n{seg.text}\n{seg.zh}\n")
        QApplication.clipboard().setText("\n".join(lines))
        self.toast(True, "已复制全部字幕", f"共 {len(self.store.order)} 条，原文和译文都在里面")

    def export_transcript(self):
        if not self.store.order:
            return self.toast(False, "没有可导出的字幕", "")
        default = str(Path.home() / "Documents" / time.strftime("会议字幕-%Y%m%d-%H%M.md"))
        path, _ = QFileDialog.getSaveFileName(self, "导出字幕", default, "Markdown (*.md);;文本 (*.txt)")
        if path:
            Path(path).write_text(self.transcript_text(), encoding="utf-8")
            self.toast(True, "已导出", path)

    def clear_transcript(self):
        self._autosave()
        self.session_file = None
        if self.viewing is not None:
            self.remote.load([])
        else:
            self.tracker.clear()

    # ---------- 共享 ----------
    def default_share_name(self):
        return f"{plat.computer_name()} 的字幕"

    def toggle_share(self):
        if self.server.running:
            self.stop_share()
            return
        if not self.cfg.get("view_code"):
            self.set_option("view_code", new_code())
        name = self.cfg.get("share_name") or self.default_share_name()
        err = self.server.start(name, self.cfg["view_code"], int(self.cfg.get("share_port", 47322)),
                                [seg_to_dict(self.tracker.segments[i]) for i in self.tracker.order])
        if err:
            return self.toast(False, "无法开始共享", err + "（可以在设置里换一个共享端口）")
        self.share_viewers = 0
        self._refresh_status()
        self.show_share_dialog()

    def stop_share(self):
        if self.server.running:
            self.server.stop()
            self.toast(True, "已停止共享", "观看的设备会收到提示")
            self._refresh_status()

    def show_share_dialog(self):
        if not self.server.running:
            return
        ShareDialog(self, self.server.name, self.server.code, self.server.port, lan_addresses(),
                    self.cfg.get("share_ip", ""), lambda: self.share_viewers,
                    lambda ip: self.set_option("share_ip", ip), self.stop_share).exec()

    def reset_view_code(self):
        self.set_option("view_code", new_code())
        if self.server.running:
            self.toast(True, "观看码已重置", "下次开始共享时生效；正在共享的观看码不变")
        else:
            self.toast(True, "观看码已重置", f"新的观看码：{self.cfg['view_code']}")

    def toggle_watch(self):
        if self.viewing is not None:
            self.exit_view()
            return
        dlg = WatchDialog(self, self.cfg, self.worker)
        if dlg.exec() and dlg.selected():
            name, url, code = dlg.selected()
            log.info("观看共享「%s」：%s", name, url)
            self.save_timer.start()
            self._switch_view(name)
            self.watcher.watch(url, code)

    def _on_watch_snapshot(self, name, segs):
        if self.viewing is not None and name:
            self.viewing = name
        self.remote.load(segs)

    def _switch_view(self, name):
        """在“本机字幕”和“观看共享”之间切换，重建列表和浮窗。"""
        if name is not None:
            self.asr.stop()
        elif self.cfg.get("source") == "asr":
            self.asr.start()
        self._autosave()
        self.session_file = None
        self.viewing = name
        self.watch_note = ""
        self._on_cleared()
        if name is None:
            for sid in self.tracker.order[-MAX_ROWS:]:
                self._on_seg_added(self.tracker.segments[sid])
        else:
            self.remote.load([])
        self._refresh_status()

    def exit_view(self):
        self.watcher.stop()
        self._switch_view(None)

    def _on_watch_ended(self, reason):
        log.info("观看结束：%s", reason)
        # 保留已收到的字幕继续显示，等用户自己点“退出观看”
        if self.viewing is not None:
            self.watch_note = f"{reason}，点“退出观看”返回"
            self._autosave()
            self.toast(False, "共享已结束", reason + "（收到的字幕已保存在历史记录里）")
            self._refresh_status()

    # ---------- 窗口 ----------
    def toast(self, ok, title, content):
        (log.info if ok else log.warning)("提示：%s %s", title, content)
        (InfoBar.success if ok else InfoBar.error)(title, content, parent=self, duration=4000 if ok else 8000,
                                                   position=InfoBarPosition.TOP_RIGHT)

    def open_path(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_instance(self, server):
        conn = server.nextPendingConnection()
        if conn:
            conn.readyRead.connect(self.show_window)

    def closeEvent(self, e):
        if self.quitting:
            return super().closeEvent(e)
        e.ignore()
        self.hide()
        if not self.notified:
            self.notified = True
            self.tray.showMessage(APP_NAME, "已缩到菜单栏，字幕翻译继续运行。点菜单栏图标可以退出。" if plat.IS_MAC
                                  else "已缩到托盘，字幕翻译继续运行。右键托盘图标可退出。",
                                  QSystemTrayIcon.Information, 3000)

    def quit_app(self):
        log.info("退出软件")
        self.quitting = True
        # 每一步单独兜底：哪一步出错都只记日志，保证软件一定能退出
        steps = [("停止 Teams 桌面版读取", lambda: self.desktop.stop(wait=1.0)),
                 ("停止共享", self.server.stop), ("停止观看", self.watcher.stop),
                 ("停止语音识别", lambda: (self.asr.stop(), self.asr.join())),
                 ("保存记录", self._autosave), ("保存配置", lambda: config.save(self.cfg)),
                 ("关闭本地端口", self.bridge.stop), ("隐藏托盘", self.tray.hide), ("关闭浮窗", self.overlay.close)]
        for name, fn in steps:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("退出时%s失败，继续退出", name)
        QApplication.quit()


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)

    sock = QLocalSocket()
    sock.connectToServer(INSTANCE)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return
    QLocalServer.removeServer(INSTANCE)
    server = QLocalServer()
    server.listen(INSTANCE)

    cfg = config.load()
    if not plat.HAS_ASR:
        cfg["source"] = "teams"   # Mac 版没有语音识别
    applog.setup(bool(cfg.get("debug_log")))
    log.info("========== %s v%s 启动 ==========", APP_NAME, APP_VERSION)
    log.info("程序：%s · Python %s · 配置目录 %s", sys.executable, sys.version.split()[0], config.APP_DIR)
    log.info("翻译接口 %s · 模型 %s · 识别模型 %s · 字幕来源 %s · 密钥%s", cfg["api_base"], cfg["model"],
             cfg.get("asr_model"), cfg.get("source"), "已填写" if cfg.get("api_key") else "未填写")
    if cfg.get("translate_mode") == "online":
        log.info("翻译走线上 API：%s · 模型 %s · 思考%s · 密钥%s", cfg.get("online_api_base"), cfg.get("online_model"),
                 "low" if cfg.get("online_thinking") == "low" else "关闭",
                 "已填写" if cfg.get("online_api_key") else "未填写")
    setTheme(THEMES[int(cfg.get("theme", 0)) % len(THEMES)][1])
    setThemeColor("#0067c0")
    win = MainWindow(cfg, server)
    if "--minimized" not in sys.argv:
        win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    main()
