"""浏览器扩展安装向导。

浏览器不允许程序静默安装扩展，也不允许用命令行或网页直接打开 chrome:// 页面，
所以这里做成半自动：打开浏览器、把要粘贴的内容放进剪贴板，再自动检测扩展有没有连上。
"""
import os
import shutil
import subprocess
import winreg
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, FluentIcon as FIF, InfoBar, InfoBarPosition,
                            MessageBoxBase, PushButton, SimpleCardWidget, StrongBodyLabel, SubtitleLabel)

EXT_URL = "chrome://extensions"   # Edge 也认这个地址（会自动跳到 edge://extensions）
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ThaiCaption" / "extension"
GREY = (QColor("#606060"), QColor("#a0a0a0"))


def sync_extension(bundled):
    """把软件自带的扩展复制到固定位置；以后挪动 exe 文件夹，浏览器里的扩展也不会失效。"""
    if Path(bundled).is_dir():
        shutil.copytree(bundled, INSTALL_DIR, dirs_exist_ok=True)
    return INSTALL_DIR


def find_browsers():
    found = []

    def add(name, path):
        if path and Path(path).is_file() and all(Path(p) != Path(path) for _, p in found):
            found.append((name, str(path)))

    def label(path):
        low = str(path).lower()
        if "msedge" in low:
            return "Microsoft Edge"
        if "google" in low:
            return "Google Chrome"
        if "cent" in low:
            return "Cent Browser"
        return f"Chrome（{Path(path).parent.parent.name}）"

    for exe in ("chrome.exe", "msedge.exe"):
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                path = winreg.QueryValue(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}")
            except OSError:
                continue
            add(label(path), path)
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            add("Google Chrome", Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
            add("Microsoft Edge", Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    found.sort(key=lambda b: (b[0] != "Google Chrome", b[0] == "Microsoft Edge"))   # Chrome 优先，Edge 最后
    return found


def step_card(title, body, *buttons):
    card = SimpleCardWidget()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(18, 12, 18, 12)
    lay.setSpacing(4)
    lay.addWidget(StrongBodyLabel(title))
    text = BodyLabel(body)
    text.setWordWrap(True)
    lay.addWidget(text)
    if buttons:
        row = QHBoxLayout()
        row.setSpacing(8)
        for b in buttons:
            row.addWidget(b)
        row.addStretch()
        lay.addSpacing(4)
        lay.addLayout(row)
    return card


class InstallDialog(MessageBoxBase):
    def __init__(self, parent, bundled_dir, ext_version):
        """ext_version：返回最近一次报到的扩展版本号（没连上返回 None）的函数。"""
        super().__init__(parent)
        self.ext_version = ext_version
        self.ext_dir = sync_extension(bundled_dir)
        self.browsers = find_browsers()

        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(SubtitleLabel("安装浏览器扩展"))
        tip = CaptionLabel("每台电脑只需要装一次。浏览器不允许程序自动安装扩展，下面几步要你手动点一下，约 30 秒。")
        tip.setWordWrap(True)
        tip.setTextColor(*GREY)
        self.viewLayout.addWidget(tip)

        self.browser_box = ComboBox()
        self.browser_box.addItems([name for name, _ in self.browsers] or ["未找到 Chrome / Edge"])
        self.browser_box.setMinimumWidth(200)
        open_btn = PushButton(FIF.GLOBE, "打开浏览器并复制地址")
        open_btn.clicked.connect(self.open_browser)
        open_btn.setEnabled(bool(self.browsers))
        self.viewLayout.addWidget(step_card(
            "① 打开扩展管理页",
            f"点按钮会打开浏览器，并把 {EXT_URL} 复制好。在浏览器地址栏按 Ctrl+V，再按回车。",
            self.browser_box, open_btn))
        self.viewLayout.addWidget(step_card(
            "② 打开“开发者模式”", "扩展管理页右上角（Edge 在左下角）的“开发者模式 / 开发人员模式”开关，打开它。"))
        copy_btn = PushButton(FIF.COPY, "复制扩展路径")
        copy_btn.clicked.connect(self.copy_path)
        show_btn = PushButton(FIF.FOLDER, "在文件夹中显示")
        show_btn.clicked.connect(lambda: subprocess.Popen(["explorer", "/select,", str(self.ext_dir)]))
        self.viewLayout.addWidget(step_card(
            "③ 加载扩展",
            "点“加载已解压的扩展程序”。在弹出窗口顶部的地址栏按 Ctrl+V 粘贴路径并回车，再点“选择文件夹”。"
            "也可以直接把 extension 文件夹拖进扩展管理页。\n" + str(self.ext_dir),
            copy_btn, show_btn))

        self.status = StrongBodyLabel()
        self.viewLayout.addWidget(self.status)

        self.yesButton.setText("完成")
        self.cancelButton.setText("关闭")
        self.widget.setMinimumWidth(640)
        self.timer = QTimer(self, interval=500, timeout=self.refresh)
        self.timer.start()
        self.refresh()

    def toast(self, title, content=""):
        InfoBar.success(title, content, parent=self, duration=3000, position=InfoBarPosition.TOP)

    def open_browser(self):
        _, path = self.browsers[self.browser_box.currentIndex()]
        QGuiApplication.clipboard().setText(EXT_URL)
        subprocess.Popen([path, "--new-window"])
        self.toast("已复制 " + EXT_URL, "在浏览器地址栏按 Ctrl+V 再回车")

    def copy_path(self):
        QGuiApplication.clipboard().setText(str(self.ext_dir))
        self.toast("已复制扩展路径", "在“加载已解压的扩展程序”窗口里粘贴")

    def refresh(self):
        ver = self.ext_version()
        if ver:
            self.status.setText(f"✓ 扩展已安装并连上软件（v{ver}），可以关闭本窗口了")
            self.status.setTextColor(QColor("#2e7d32"), QColor("#6ccb5f"))
        else:
            self.status.setText("… 等待扩展连接（装好后几秒内会自动显示）")
            self.status.setTextColor(*GREY)
