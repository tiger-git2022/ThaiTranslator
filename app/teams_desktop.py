"""Teams 桌面版字幕：用 Windows UI Automation 读取 Teams 会议窗口里的“实时字幕”区域。

新版 Teams 桌面版是 WebView2 网页，界面元素和网页版一致：HTML 的 id 就是 UIA 的 AutomationId
（例如退出按钮 hangup-button），class 就是 ClassName。字幕区是一个名为“实时字幕”的区域，
每条字幕是“说话人 + 泰文”两段文字。窗口最小化或被挡住时也能读到。

读到的字幕和浏览器扩展发来的格式完全一样（captions / status 两个信号），交给主窗口统一处理。
"""
import ctypes
import itertools
import logging
import os
import re
import sys
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("teams_desktop")

# 新版 Teams 是 ms-teams.exe，经典版是 Teams.exe；测试时可以用环境变量换成 msedge.exe 等
TEAMS_EXES = {s.strip().lower() for s in
              (os.environ.get("THAICAPTION_TEAMS_EXE") or "ms-teams.exe,teams.exe").split(",") if s.strip()}
REGION_NAMES = ("实时字幕", "即時字幕", "Live captions", "Live Captions")
REGION_IDS = ("closed-caption-v2-window-wrapper", "closed-captions-renderer", "closed-caption-v2-virtual-list-content")
HANGUP_IDS = ("hangup-button", "hangup-main-btn", "hangup-leave-button", "hangup-end-meeting-button")
# 字幕区里的口语语言标签（如“泰语 (泰国)”“Thai (Thailand)”），不是字幕，不要当成一句话去翻译
LANG_LABEL = re.compile(r"^\S{1,12}语\s*[(（][^()（）]{1,20}[)）]$|"
                        r"^(Thai|English|Chinese|Japanese|Korean|Vietnamese|Malay|Indonesian|Lao|Burmese|Khmer)"
                        r"\s*\([^()]{1,30}\)$", re.I)
ITEM_CLASS = "ChatMessageCompact"     # 网页版里每条字幕的容器 class（fui-ChatMessageCompact）

SCAN_EVERY = 0.3        # 读字幕区的间隔
FIND_EVERY = 2.0        # 还没找到字幕区时，多久重新找一次
REFIND_EVERY = 5.0      # 找到后也定期重新确认，Teams 重新绘制界面后旧元素可能失效
STATUS_EVERY = 3.0

# UI Automation 常量
NAME, CONTROL_TYPE, AUTOMATION_ID, CLASS_NAME = 30005, 30003, 30011, 30012
TEXT, LIST_ITEM = 50020, 50007
NOT_REGION = {50000, 50002, 50011, 50020, 50031, 50003}   # 按钮、复选框、菜单项、文字、拆分按钮、下拉框
SCOPE_DESCENDANTS, SCOPE_SUBTREE = 4, 7
IGNORE_CASE = 1


def speaker_key(name):
    """Teams 对同一个人有时显示“Tiger Wang”，有时显示“Wang Tiger”：按词排序后比较。"""
    return " ".join(sorted(name.casefold().replace(",", " ").split()))


# 用自己的 WinDLL 实例并写明参数、返回类型：64 位下句柄按默认的 int 处理会被截断，
# 也不和其他库共用 ctypes.windll 上的函数设置
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD))
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.CloseHandle.restype = wintypes.BOOL
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_u32.EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
_u32.EnumWindows.restype = wintypes.BOOL
_u32.IsWindowVisible.argtypes = (wintypes.HWND,)
_u32.IsWindowVisible.restype = wintypes.BOOL
_u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_u32.GetWindowThreadProcessId.restype = wintypes.DWORD
_u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_u32.GetWindowTextW.restype = ctypes.c_int


def _exe_name(pid, cache={}):
    if pid in cache:
        return cache[pid]
    name = ""
    h = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            _k32.CloseHandle(h)
    cache[pid] = name
    return name


class _Reader:
    """在采集线程里使用（COM 对象不能跨线程），负责找窗口、找字幕区、读出 (说话人, 文本, 元素标识)。"""

    def __init__(self):
        sys.coinit_flags = getattr(sys, "coinit_flags", 0)   # 采集线程用多线程套间
        import comtypes
        import comtypes.client
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            from comtypes.gen import UIAutomationClient as uia_mod
        except ImportError:
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia_mod
        self.COMError = comtypes.COMError
        self.uia = comtypes.client.CreateObject(uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation)
        u = self.uia

        def any_of(prop, values):
            conds = [u.CreatePropertyConditionEx(prop, v, IGNORE_CASE) for v in values]
            return u.CreateOrConditionFromArray(conds) if len(conds) > 1 else conds[0]

        self.region_cond = u.CreateOrCondition(any_of(NAME, REGION_NAMES), any_of(AUTOMATION_ID, REGION_IDS))
        self.hangup_cond = any_of(AUTOMATION_ID, HANGUP_IDS)
        self.tree_req = u.CreateCacheRequest()
        for p in (NAME, CONTROL_TYPE, CLASS_NAME):
            self.tree_req.AddProperty(p)
        self.tree_req.TreeScope = SCOPE_SUBTREE
        # 原始视图：没有角色的 div（每条字幕的容器）在控件视图里看不到，原始视图里有，ClassName 就是 HTML 的 class
        self.tree_req.TreeFilter = u.RawViewCondition

    @staticmethod
    def _items(arr):
        return [arr.GetElement(i) for i in range(arr.Length)] if arr else []

    def windows(self):
        """Teams 的所有顶层窗口（包括最小化的），返回 [(元素, 标题)]。
        用 Win32 枚举窗口，只碰 Teams 自己的窗口：别的程序窗口卡住或出错不会影响这里。"""
        hwnds = []
        cb = _WNDENUMPROC(lambda h, _: hwnds.append(h) or True)
        _u32.EnumWindows(cb, 0)
        out = []
        for h in hwnds:
            if not h or not _u32.IsWindowVisible(h):
                continue
            pid = wintypes.DWORD()
            _u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if _exe_name(pid.value) not in TEAMS_EXES:
                continue
            buf = ctypes.create_unicode_buffer(512)
            _u32.GetWindowTextW(h, buf, 512)
            try:
                out.append((self.uia.ElementFromHandle(h), buf.value))
            except self.COMError:
                pass
        return out

    def _each(self, wins, fn):
        for w, _ in wins:
            try:
                r = fn(w)
            except self.COMError:
                continue     # 某个窗口正在关闭或暂时无响应，跳过它
            if r:
                return r
        return None

    def find_region(self, wins):
        def find(w):
            return next((el for el in self._items(w.FindAll(SCOPE_DESCENDANTS, self.region_cond))
                         if el.CurrentControlType not in NOT_REGION), None)
        return self._each(wins, find)

    def in_meeting(self, wins):
        return bool(self._each(wins, lambda w: w.FindFirst(SCOPE_DESCENDANTS, self.hangup_cond)))

    def read(self, region):
        """返回 [(说话人, 文本)]，按界面上的先后顺序。"""
        tree = region.BuildUpdatedCache(self.tree_req)
        out = []

        def kids(el):
            return self._items(el.GetCachedChildren())

        def texts(el):
            """el 下面所有最底层的文字。"""
            ch = kids(el)
            if el.CachedControlType == TEXT and not any(c.CachedControlType == TEXT for c in ch):
                name = (el.CachedName or "").strip()
                return [name] if name and not LANG_LABEL.match(name) else []
            return list(itertools.chain.from_iterable(texts(c) for c in ch))

        def is_item(el):
            return ITEM_CLASS in (el.CachedClassName or "") or el.CachedControlType == LIST_ITEM

        def walk(el):
            if el is not tree and is_item(el):
                ts = texts(el)
                if ts:
                    out.append(("", ts[0]) if len(ts) == 1 else (ts[0], "".join(ts[1:])))
                return
            for c in kids(el):
                walk(c)

        walk(tree)
        if not out:
            # 每条字幕的容器在无障碍树里看不到（没有角色的 div 会被省略）：按“说话人、文本”两两配对
            ts = texts(tree)
            # 单数说明前面多了标题、提示之类的文字（字幕总是成对的，新字幕加在末尾），从开头去掉
            ts = ts[len(ts) % 2:]
            out = [(ts[i], ts[i + 1]) for i in range(0, len(ts) - 1, 2)]
        return out


class TeamsDesktopSource(QObject):
    captions = Signal(dict)   # 和浏览器扩展一样：{"session", "items": [{"id","speaker","text","rev"}], "ts"}
    status = Signal(dict)     # {"session", "host", "title", "inMeeting", "captionsOn", "ts"}

    def __init__(self):
        super().__init__()
        self.thread = None
        self.stop_evt = threading.Event()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.stop_evt = threading.Event()   # 每次启动用新的，旧线程还没退出也不会被“复活”
        self.thread = threading.Thread(target=self._run, args=(self.stop_evt,), name="teams-desktop", daemon=True)
        self.thread.start()

    def stop(self, wait=0.0):
        self.stop_evt.set()
        if wait and self.thread is not None:
            self.thread.join(wait)
        self.thread = None

    def _run(self, stop_evt):
        try:
            reader = _Reader()
        except Exception:  # noqa: BLE001
            log.exception("UI Automation 初始化失败，Teams 桌面版字幕不可用")
            return
        session = "desk" + format(int(time.time()) % 100000, "05d")
        seq, rev = itertools.count(1), itertools.count(1)
        prev = []        # 上一次读到的字幕，按界面顺序：[字幕 id, 说话人 key, 文本]
        sent = {}        # 字幕 id -> 已经发出的文本
        region, wins, title = None, [], ""
        next_find = next_status = 0.0
        meeting, last_state, fails = False, None, 0
        while not stop_evt.is_set():
            now = time.monotonic()
            try:
                if now >= next_find:
                    wins = reader.windows()
                    region = reader.find_region(wins) if wins else None
                    next_find = now + (REFIND_EVERY if region else FIND_EVERY)
                    meeting = bool(wins) and (region is not None or reader.in_meeting(wins))
                    title = next((t for _, t in wins if t), "")
                rows = None     # None = 这次没读到（不是“字幕区是空的”），保留已知字幕，免得重复翻译
                if region is not None:
                    try:
                        rows = reader.read(region)
                    except reader.COMError:
                        region, next_find = None, 0.0   # 字幕区没了（关掉字幕或界面重绘），马上重新找
            except Exception as e:  # noqa: BLE001
                fails += 1
                if fails <= 3 or fails % 100 == 0:
                    log.warning("读取 Teams 桌面版失败（第 %s 次）：%r", fails, e)
                region, rows, next_find = None, None, now + 0.5
            state = (bool(wins), meeting, region is not None)
            if state != last_state:
                log.info("Teams 桌面版：窗口=%s · 在会议中=%s · 字幕区=%s", *state)
                last_state = state
            changed = []
            if rows is not None:
                prev = self._align(prev, rows, lambda: f"{session}-{next(seq)}")
                for (sid, _spk, text), (speaker, _) in zip(prev, rows):
                    if sid not in sent or sent[sid] != text:
                        sent[sid] = text
                        changed.append({"id": sid, "speaker": speaker, "text": text, "rev": next(rev)})
                sent = {sid: sent[sid] for sid, _, _ in prev}
            ts = int(time.time() * 1000)
            if changed:
                self.captions.emit({"session": session, "items": changed, "ts": ts})
            if wins and now >= next_status:
                next_status = now + STATUS_EVERY
                self.status.emit({"session": session, "host": "Teams 桌面版", "title": title,
                                  "inMeeting": meeting, "captionsOn": region is not None, "ts": ts})
            stop_evt.wait(SCAN_EVERY if region is not None else min(FIND_EVERY, max(0.1, next_find - now)))

    @staticmethod
    def _align(prev, rows, new_id):
        """把这次读到的每条字幕和上次的对上号，返回 [字幕 id, 说话人 key, 文本]（和 rows 一一对应）。

        界面元素的标识靠不住（字幕的容器在无障碍树里看不到，Teams 还会整块重新绘制），所以按内容对：
        字幕列表只会在末尾新增、从顶部移走，正在说的那几条会变长或被修正。按顺序往后找上次的条目，
        同一个人、开头相同（或一方是另一方的开头）就算同一条；对不上的才是新的一句。"""
        out, j = [], 0
        for speaker, text in rows:
            spk = speaker_key(speaker)
            for k in range(j, len(prev)):
                sid, pspk, ptext = prev[k]
                if pspk == spk and (ptext[:3] == text[:3] or ptext.startswith(text) or text.startswith(ptext)):
                    out.append([sid, spk, text])
                    j = k + 1
                    break
            else:
                out.append([new_id(), spk, text])
        return out
