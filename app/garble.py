"""识别乱码检测：会议口语语言设为泰语时，Teams 会把英语发言识别成一串看似英文的乱码
（比如 “Importjes so thuse me s now internet of scale to plants”），hymt2 会把它编成通顺的假句子，
所以本地模式下判为乱码的句子改送线上 API（按读音还原成英文再翻）。

把句子里连续的拉丁字母“单词”当成一段，一段里不认识的词（不在常用英文词表里）占比够高，
就判定这一段是乱码。全大写的缩写（UAT、PO、SAP）和数字不参与判断。
"""
import re
import sys
from pathlib import Path

WORDS_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "models" / "english_words.txt"
# 一段至少有这么多个词才判断，太短的（一两个英文术语）不管
MIN_WORDS = 3
# 不认识的词占比达到这个值就算乱码。真实英语里人名、生僻词偶尔有一两个，乱码里通常有 1/4 以上
UNKNOWN_RATIO = 0.25
# 词表之外、会议里常见的词
EXTRA = {"ok", "okay", "email", "emails", "online", "offline", "login", "logout", "dashboard", "workflow",
         "approve", "approval", "approver", "invoice", "invoices", "vendor", "vendors", "finance", "deadline",
         "timeline", "checklist", "feedback", "sync", "meeting", "teams", "excel", "powerpoint", "template",
         "go", "live", "sign", "off", "follow", "up", "kickoff", "onboarding", "deploy", "deployment",
         "reverse", "accrual", "accruals", "reconcile", "reconciliation", "ledger", "posting", "upload", "download"}

RUN_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*(?:[\s,.]+[A-Za-z][A-Za-z'’-]*)*")
WORD_RE = re.compile(r"[A-Za-z]+")
_words = None


def words():
    global _words
    if _words is None:
        try:
            raw = WORDS_FILE.read_text(encoding="utf-8").split()
        except OSError:
            raw = []
        # 词表里有单个字母，除了 a、i 都去掉（乱码里常见孤立的 s、e）
        _words = {w.lower() for w in raw if len(w) > 1 or w in ("a", "i")} | EXTRA
    return _words


def known(w):
    lw = w.lower()
    if len(w) >= 2 and w.isupper():   # 缩写
        return True
    vocab = words()
    if lw in vocab:
        return True
    for suf in ("s", "es", "ed", "d", "ing", "ly", "er"):
        if lw.endswith(suf) and len(lw) - len(suf) >= 3:
            base = lw[:-len(suf)]
            if base in vocab or base + "e" in vocab:
                return True
    return False


def runs(text):
    """返回 [(起, 止, 这一段的词数, 不认识的词数)]。"""
    out = []
    for m in RUN_RE.finditer(text):
        ws = WORD_RE.findall(m.group())
        out.append((m.start(), m.end(), len(ws), sum(not known(w) for w in ws)))
    return out


def garbled_runs(text):
    return [(a, b, n, u) for a, b, n, u in runs(text) if n >= MIN_WORDS and u / n >= UNKNOWN_RATIO]


def pieces(text):
    """返回被判为乱码的片段列表。"""
    return [text[a:b] for a, b, _n, _u in garbled_runs(text)]
