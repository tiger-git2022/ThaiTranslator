"""日志：写到 %APPDATA%\\ThaiCaption\\logs\\thaicaption.log，每天零点换一个文件，保留 7 天。

各模块用 logging.getLogger("模块名")；未捕获的异常（主线程、子线程、Qt 警告）也都会记进来。
"""
import logging
import sys
import threading
from logging.handlers import TimedRotatingFileHandler

import config

LOG_DIR = config.APP_DIR / "logs"
FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def setup(debug=False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fh = TimedRotatingFileHandler(LOG_DIR / "thaicaption.log", when="midnight", backupCount=7, encoding="utf-8")
    fh.setFormatter(logging.Formatter(FMT, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    if sys.stderr is not None:     # 源码运行时同时输出到控制台；打包后的 exe 没有控制台
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(FMT, "%H:%M:%S"))
        root.addHandler(sh)
    for noisy in ("urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    crash = logging.getLogger("crash")
    sys.excepthook = lambda t, v, tb: crash.critical("未捕获的异常", exc_info=(t, v, tb))
    threading.excepthook = lambda a: crash.critical(f"线程 {a.thread.name if a.thread else '?'} 未捕获的异常",
                                                    exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    qt = logging.getLogger("qt")
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}
    def on_qt(mode, ctx, msg):
        # Qt 退出时的 QThreadStorage 提示是正常现象，降为调试级别，免得误导排查
        level = logging.DEBUG if "QThreadStorage" in msg else levels.get(mode, logging.INFO)
        qt.log(level, msg)
    qInstallMessageHandler(on_qt)


def set_debug(on):
    logging.getLogger().setLevel(logging.DEBUG if on else logging.INFO)
    logging.getLogger("app").info("详细日志：%s", "开" if on else "关")
