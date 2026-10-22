"""平台差异集中放这里：Windows 是完整版；Mac 版先只支持“浏览器扩展抓 Teams 网页版字幕 + 翻译 + 浮窗 + 共享”，
Teams 桌面版字幕（UI Automation）和语音识别（WASAPI 录系统声音）只有 Windows 有。"""
import os
import socket
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Windows 专用功能
HAS_TEAMS_DESKTOP = IS_WIN
HAS_ASR = IS_WIN
HAS_BACKDROP = IS_WIN      # 云母 / 亚克力窗口材质、浮窗毛玻璃

# 界面文字里的快捷键：Mac 上 Ctrl 对应 ⌘
MOD = "⌘" if IS_MAC else "Ctrl+"


def data_dir():
    """配置、记录、日志放哪：Windows %APPDATA%\\ThaiCaption；Mac ~/Library/Application Support/ThaiCaption。"""
    if os.environ.get("THAICAPTION_HOME"):
        return Path(os.environ["THAICAPTION_HOME"])
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "ThaiCaption"
    return Path(os.environ.get("APPDATA") or Path.home()) / "ThaiCaption"


def local_data_dir():
    """不需要漫游的数据（浏览器扩展的固定安装位置）。"""
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ThaiCaption"
    return data_dir()


def computer_name():
    name = os.environ.get("COMPUTERNAME")
    if not name and IS_MAC:
        try:
            name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True,
                                  timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
    return name or socket.gethostname().removesuffix(".local") or "本机"


def reveal(path):
    """在资源管理器 / 访达里显示并选中这个文件或文件夹。"""
    path = str(path)
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", path])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])
