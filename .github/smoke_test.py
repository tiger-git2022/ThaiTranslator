"""启动自检：用源码建主窗口、打开浮窗和扩展安装向导，跑几秒后退出；有未捕获的异常就失败。
python .github/smoke_test.py            # 按当前系统
python .github/smoke_test.py --as-mac   # 在 Windows 上按 Mac 版的功能开关跑一遍界面
在 CI 里设 QT_QPA_PLATFORM=offscreen，不需要显示器。"""
import faulthandler
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ["THAICAPTION_HOME"] = tempfile.mkdtemp(prefix="thaicaption-smoke-")

import plat  # noqa: E402

if "--as-mac" in sys.argv:
    plat.HAS_TEAMS_DESKTOP = plat.HAS_ASR = plat.HAS_BACKDROP = False

faulthandler.enable()   # 原生代码崩溃（段错误）时也打印出是哪一行
errors = []


def hook(*exc):
    text = "".join(traceback.format_exception(*exc))
    errors.append(text)
    print(text, file=sys.stderr, flush=True)


sys.excepthook = hook

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtNetwork import QLocalServer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
server = QLocalServer()
server.listen("ThaiCaption-smoke")
cfg = config.load()
cfg["overlay_visible"] = True
win = main.MainWindow(cfg, server)
win.show()


def steps():
    try:
        dlg = main.InstallDialog(win, main.EXTENSION_DIR, win.ext_version)
        dlg.show()
        QTimer.singleShot(500, dlg.close)
        win.set_option("overlay_blur", True)
        win.set_option("click_through", True)
        win.set_option("click_through", False)
        win.switchTo(win.settings)
        win.switchTo(win.help)
        win._refresh_status()
        print("状态栏：", win.ext_label.text())
        menu = win.tray.contextMenu()   # 托盘菜单能打开；Windows 上是亚克力样式且模糊依赖齐全
        if plat.HAS_BACKDROP:
            from qfluentwidgets.common import image_utils  # noqa: F401  缺 scipy / colorthief 时这里报错
            if type(menu).__name__ != "AcrylicSystemTrayMenu":
                raise RuntimeError(f"托盘菜单不是亚克力样式：{type(menu).__name__}")
        menu.exec(win.geometry().center())
        QTimer.singleShot(300, menu.close)
        print("托盘菜单：", type(menu).__name__)
    except Exception:  # noqa: BLE001
        errors.append(traceback.format_exc())


QTimer.singleShot(1000, steps)
QTimer.singleShot(4000, win.quit_app)
app.exec()

if errors:
    print("\n".join(errors))
    sys.exit("启动自检失败")
print(f"启动自检通过（{sys.platform}{'，按 Mac 功能开关' if '--as-mac' in sys.argv else ''}）")
