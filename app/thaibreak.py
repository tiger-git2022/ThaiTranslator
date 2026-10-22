"""泰文按词断行。

泰文词与词之间不写空格，Qt 找不到可以换行的地方：普通标签会把整句当成一个词，撑宽到窗口外；
文本区域则只能在任意字母处硬断。这里借用 Windows 自带的 ICU（icu.dll，Win10 1703 起系统自带）
找出词边界，插入零宽空格，Qt 就会在词与词之间换行。复制文字时用 strip_breaks 去掉。
"""
import ctypes
import logging
import re
import unicodedata

log = logging.getLogger("thaibreak")
ZWSP = "​"
UBRK_LINE = 2
UBRK_DONE = -1
THAI_RUN = re.compile(r"[฀-๿]{2,}")
_icu = False   # False = 还没加载；None = 加载失败，不再尝试


def _load():
    global _icu
    if _icu is False:
        try:
            icu = ctypes.WinDLL("icu.dll")
            icu.ubrk_open.restype = ctypes.c_void_p
            icu.ubrk_open.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_wchar_p, ctypes.c_int32,
                                      ctypes.POINTER(ctypes.c_int)]
            icu.ubrk_next.restype = ctypes.c_int32
            icu.ubrk_next.argtypes = [ctypes.c_void_p]
            icu.ubrk_close.restype = None
            icu.ubrk_close.argtypes = [ctypes.c_void_p]
            _icu = icu
        except (OSError, AttributeError) as e:
            log.info("系统没有可用的 icu.dll，泰文改为按字母换行：%s", e)
            _icu = None
    return _icu


def _split(icu, run):
    buf = ctypes.create_unicode_buffer(run)   # ICU 迭代期间一直引用这块内存，必须活到 close 之后
    status = ctypes.c_int(0)
    it = icu.ubrk_open(UBRK_LINE, b"th", buf, len(run), ctypes.byref(status))
    if not it or status.value > 0:            # ICU 里负数是警告，正数才是错误
        return run
    try:
        out, prev = [], 0
        while (pos := icu.ubrk_next(it)) != UBRK_DONE and pos < len(run):
            # 不在元音、声调这类附加符号前面断开，否则会单独显示成虚线圆圈
            if pos > prev and not unicodedata.category(run[pos]).startswith("M"):
                out.append(run[prev:pos])
                prev = pos
        out.append(run[prev:])
        return ZWSP.join(out)
    finally:
        icu.ubrk_close(it)


def soft_breaks(text):
    """在泰文词之间插入零宽空格；没有泰文或系统不支持时原样返回。"""
    if not text or not THAI_RUN.search(text):
        return text
    icu = _load()
    if icu is None:
        return text
    return THAI_RUN.sub(lambda m: _split(icu, m.group()), text)


def strip_breaks(text):
    return text.replace(ZWSP, "")
