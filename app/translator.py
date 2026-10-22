"""翻译线程：调用 OpenAI 兼容接口（本地 llama-swap 或线上 API），同一时间只发一个请求。

同一条字幕只保留最新的待翻译文本；定稿优先于临时译文，临时译文只翻最新的一条。
"""
import collections
import difflib
import itertools
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

import garble
from config import parse_glossary

log = logging.getLogger("translate")
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


@dataclass
class Job:
    seg_id: str
    text: str
    final: bool
    context: list = field(default_factory=list)
    order: int = 0


def is_hy(model):
    m = model.lower()
    return "hymt" in m or "hy-mt" in m or "hunyuan" in m


def build_messages(model, text, target, context, glossary, extra="", topic=""):
    """混元翻译模型用官方提示模板（无 system）；其他通用模型用同传指令。"""
    terms = [(s, d) for s, d in glossary if s in text]
    if is_hy(model):
        parts = []
        if terms:
            parts.append("参考下面的翻译：\n" + "\n".join(f"{s} 翻译成 {d}" for s, d in terms) + "\n")
        if context:
            parts.append("\n".join(context))
            parts.append(f"参考上面的信息，把下面的文本翻译成{target}，注意不需要翻译上文，也不要额外解释：\n{text}")
        else:
            parts.append(f"将以下文本翻译为{target}，注意只需要输出翻译后的结果，不要额外解释：\n{text}")
        return [{"role": "user", "content": "\n".join(parts)}]

    system = GENERIC_PROMPT.format(target=target, topic=(
        f"\n会议背景：{topic}。专业词按这个行业的习惯译法翻（比如保险里 claim=理赔、premium=保费、policy=保单）。"
        if topic else ""))
    user = []
    if terms:
        user.append("术语：\n" + "\n".join(f"{s} → {d}" for s, d in terms))
    if context:
        user.append("上文（仅供参考，不要翻译）：\n" + "\n".join(context))
    user.append(("要翻译：\n" if user else "") + text)
    if extra:
        user.append(extra)
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user)}]


# 通用模型（DeepSeek 等线上 API）的同传提示词，按泰英混说实测调过。
# 第 1 条一定要写“普通英文词也要译出来”，否则模型会把 review、finance、go live 原样留着
GENERIC_PROMPT = """你是会议实时字幕的同声传译，把字幕翻译成{target}。{topic}
1. 字幕可能是泰语、英语，或泰语里夹着英文单词、整句英语。全部译成通顺的{target}：普通英文词（review、finance、approve、go live 等）也要译出来；只有缩写（UAT、PO、SAP）、产品名、系统名和人名保留原文。
2. 用泰文字母拼写的英文词（如 ดีไซน์=design、โปรเซส=process、เซฟ=save、ดูพลิเคท=duplicate），按英文原意翻译。
3. 字幕来自语音识别，可能有错字、断句不全或乱码。每一部分都要尽量翻出来：错字按读音和上下文猜最可能的词；英语被识别成乱码时，按读音还原成最可能的英文再翻。不要写“（听不清）”，也不要保留乱码原文。
4. 口语化、简洁，保留数字；ค่ะ、ครับ、นะ、เออ 这类语气词不必逐字翻。
5. 如果给了“上文”，它只用来帮助理解，不要翻译；只输出“要翻译”那一句的译文，不要解释，不要加引号。
6. 译文里不能出现任何泰文字母：泰语人名、昵称一律写成汉字音译（如 สมชาย=颂猜、ฝน=芬），不要用拉丁字母拼写人名；泰文字母的名称写成拉丁转写（如 กอไก่ 写成“字母 ko kai”）；其他泰语词一律译出意思。"""

THAI_RE = re.compile(r"[฀-๿]+")

# 内置术语：泰文字母拼写的英文词，hymt2 容易翻错（ดูพลิเคท 翻成“查看应用”）。用户术语表里有同一个词时以用户的为准
DEFAULT_GLOSSARY = [("ดูพลิเคท", "复制"), ("ดีไซน์", "设计"), ("โปรเซส", "流程"), ("เซฟ", "保存"), ("สเต็ป", "步骤")]


def merge_glossary(user_pairs):
    have = {s for s, _ in user_pairs}
    return list(user_pairs) + [(s, d) for s, d in DEFAULT_GLOSSARY if s not in have]


class TransientError(RuntimeError):
    """网络抖动、超时、服务端 5xx 等，重试一次可能就好。"""


class BadRequest(RuntimeError):
    """HTTP 400 / 422：多半是请求里有服务不认识的字段。"""


class EmptyOutput(RuntimeError):
    """finish_reason=length 且没有译文：推理模型的思考把 max_tokens 用光了。"""


THINKING_LEVELS = ("off", "low")
THINKING_LABEL = {"off": "思考关闭", "low": "思考 low"}
ONLINE_MAX_TOKENS = 4096    # low 档单句思考最多约 2300 token，high 档见过约 3900，不要低于 4096
ONLINE_TIMEOUT = 60         # low 档单句最慢约 13 秒，high 约 20 秒
# 不支持思考档位字段的服务：(接口地址, 字段名)。本次运行内记住，不再发送
_unsupported = set()


def active(cfg):
    """按“翻译走哪里”取出实际使用的接口地址、密钥和模型。"""
    online = cfg.get("translate_mode") == "online"
    p = "online_" if online else ""
    thinking = cfg.get("online_thinking", "off")
    return {"online": online, "api_base": cfg.get(p + "api_base", ""), "api_key": cfg.get(p + "api_key", ""),
            "model": cfg.get(p + "model", ""), "target_lang": cfg["target_lang"],
            "thinking": thinking if thinking in THINKING_LEVELS else "off",
            "topic": cfg.get("meeting_topic", "").strip()}


def online_fallback(cfg):
    """本地模式下，线上 API 填全了就返回它的配置（乱码句改送线上翻），否则 None。"""
    c = active(dict(cfg, translate_mode="online"))
    return c if c["api_base"] and c["api_key"] and c["model"] else None


def thinking_fields(level):
    return {"thinking": {"type": "disabled"}} if level == "off" else {"reasoning_effort": level}


def _hide_key(text, api_key):
    """服务端报错有时会带上密钥，写进日志 / 界面前抹掉。"""
    return text.replace(api_key, "***") if api_key else text


def request(api_base, api_key, path, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    # 有的线上服务挡在 Cloudflare 后面，会拒绝 Python 默认的 User-Agent（403 error code: 1010）
    headers = {"Content-Type": "application/json", "User-Agent": "ThaiCaption"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if not api_base:
        raise RuntimeError("还没填接口地址")
    req = urllib.request.Request(api_base.rstrip("/") + path, data, headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:    # OpenAI 格式的报错只取 message，界面上好读一些
            raw = json.loads(raw)["error"]["message"] or raw
        except (ValueError, KeyError, TypeError):
            pass
        detail = _hide_key(str(raw)[:200], api_key)
        if e.code == 401:
            raise RuntimeError(f"密钥不正确（HTTP 401）：{detail}") from None
        if e.code == 404 or "model_not_found" in detail:
            raise RuntimeError(f"接口地址或模型名不对（HTTP {e.code}）：{detail}") from None
        if e.code in (400, 422):
            raise BadRequest(f"HTTP {e.code}：{detail}") from None
        raise (TransientError if e.code >= 500 or e.code == 429 else RuntimeError)(f"HTTP {e.code}：{detail}") from None
    except urllib.error.URLError as e:
        raise TransientError(f"连不上翻译服务：{e.reason}") from None
    except (TimeoutError, ConnectionError) as e:
        raise TransientError("翻译服务响应超时" if isinstance(e, TimeoutError) else f"连接被断开：{e}") from None
    except ValueError:
        raise RuntimeError("翻译服务返回的不是 JSON，检查接口地址是否以 /v1 结尾") from None


def list_models(api_base, api_key):
    data = request(api_base, api_key, "/models", timeout=8)
    return [m["id"] for m in data.get("data", [])]


def translate(cfg, text, context=(), glossary=(), timeout=None, thinking=None, extra=""):
    """cfg 是 active() 的结果。线上 API 只发标准参数（不少服务会拒绝 llama.cpp 专用字段），
    另加思考档位（thinking 不传时用 cfg 里的档位）；网络抖动时自动重试一次。"""
    model, online = cfg["model"], cfg.get("online")
    if not model:
        raise RuntimeError("还没填模型名称")
    if timeout is None:
        timeout = ONLINE_TIMEOUT if online else 60
    body = {"model": model, "messages": build_messages(model, text, cfg["target_lang"], list(context), glossary, extra,
                                                   cfg.get("topic", "")),
            "max_tokens": ONLINE_MAX_TOKENS if online else 512, "stream": False}  # 推理模型的思考也算在 max_tokens 里
    if online:
        for k, v in thinking_fields(thinking or cfg.get("thinking", "off")).items():
            if (cfg["api_base"], k) not in _unsupported:
                body[k] = v
    elif not is_hy(model):
        body["temperature"] = 0.3
        body["chat_template_kwargs"] = {"enable_thinking": False}
    attempt = 0
    while True:
        attempt += 1
        try:
            data = request(cfg["api_base"], cfg["api_key"], "/chat/completions", body, timeout)
            break
        except BadRequest as e:
            # 服务不认识思考档位字段：去掉字段重试，本次运行内不再发送
            bad = [k for k in ("thinking", "reasoning_effort") if k in body and k in str(e)]
            if not online or not bad or attempt > 3:
                raise RuntimeError(f"线上 API：{e}" if online else str(e)) from None
            for k in bad:
                body.pop(k)
                _unsupported.add((cfg["api_base"], k))
            log.warning("线上服务 %s 不支持 %s 字段，去掉后重试，本次运行内不再发送：%s",
                        cfg["api_base"], "、".join(bad), e)
        except TransientError as e:
            if not online or attempt >= 2:
                raise RuntimeError(f"线上 API：{e}" if online else str(e)) from None
            log.info("线上 API 出错，重试一次：%s", e)
        except RuntimeError as e:
            raise RuntimeError(f"线上 API：{e}" if online else str(e)) from None
    try:
        choice = data["choices"][0]
        # 推理模型的 reasoning_content 直接忽略，只取 content
        out = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(("线上 API：" if online else "") + "返回格式不对（不是 /chat/completions 的格式）："
                           + _hide_key(json.dumps(data, ensure_ascii=False)[:200], cfg["api_key"])) from None
    out = THINK_RE.sub("", out).strip()
    if not out and choice.get("finish_reason") == "length":
        used = (data.get("usage") or {}).get("completion_tokens")
        raise EmptyOutput(("线上 API：" if online else "") + "思考用光 max_tokens，没有输出译文"
                          + (f"（输出 {used} token）" if used else ""))
    if len(out) >= 2 and out[0] in "\"“「'" and out[-1] in "\"”」'":
        out = out[1:-1].strip()
    return out


class Translator(QObject):
    done = Signal(str, str, str, bool, float)   # 字幕 id, 原文, 译文, 是否定稿, 耗时（秒）
    failed = Signal(str, str, str)              # 字幕 id, 原文, 错误信息
    warned = Signal(str)                        # 翻出来了，但有需要提示的情况（思考用光 max_tokens 等）

    def __init__(self, cfg):
        super().__init__()
        self.cond = threading.Condition()
        self.jobs = {}
        self.busy = False
        self.counter = itertools.count()
        self.recent = collections.OrderedDict()   # 字幕 id -> 最近一次译文（含临时译文），用来发现“把上文翻出来”的错误
        self.configure(cfg)
        threading.Thread(target=self._run, name="翻译", daemon=True).start()

    def configure(self, cfg):
        with self.cond:
            self.cfg = active(cfg)
            self.fallback = online_fallback(cfg)
            self.glossary = merge_glossary(parse_glossary(cfg.get("glossary", "")))

    def submit(self, seg_id, text, final, context=()):
        with self.cond:
            self.jobs[seg_id] = Job(seg_id, text, final, list(context), next(self.counter))
            self.cond.notify()

    def idle(self):
        with self.cond:
            return not self.busy and not self.jobs

    def clear(self):
        with self.cond:
            self.jobs.clear()

    def _remember(self, seg_id, out):
        self.recent[seg_id] = out
        self.recent.move_to_end(seg_id)
        while len(self.recent) > 4:
            self.recent.popitem(last=False)

    def _echoes_context(self, seg_id, text, out, context):
        """判断模型是不是把“上文”翻出来当成了这一句的结果。

        - 译文一半以上都能在前几句的译文里找到；或
        - 原文很长、译文却短得不成比例（泰文约 3 个字母对应 1 个汉字，低于 1/7 就明显缺了内容）
        原文本身就在重复上文（比如对方跟着说了一遍）时不算。
        """
        norm = "".join(text.split())
        for c in context:
            c = "".join(c.split())
            if norm in c or difflib.SequenceMatcher(None, norm, c, autojunk=False).ratio() > 0.7:
                return False
        flat = "".join(out.split())
        if len(text) >= 60 and len(flat) < len(text) / 7:
            return True
        prev = "".join("".join(o.split()) for sid, o in self.recent.items() if sid != seg_id)
        if len(flat) < 8 or not prev:     # 太短的译文容易和上文撞字，不做重复比对
            return False
        sm = difflib.SequenceMatcher(None, flat, prev, autojunk=False)
        covered = sum(b.size for b in sm.get_matching_blocks() if b.size >= 4)
        return covered >= len(flat) * 0.6

    def _pick(self):
        finals = [j for j in self.jobs.values() if j.final]
        if finals:
            job = min(finals, key=lambda j: j.order)
        else:
            job = max(self.jobs.values(), key=lambda j: j.order)
        # 临时译文只保留最新一条，旧的不再翻（等它定稿再翻）
        if not job.final:
            self.jobs = {k: j for k, j in self.jobs.items() if j.final}
        else:
            del self.jobs[job.seg_id]
        return job

    def _translate_job(self, job, cfg, glossary, fallback):
        """返回 (译文, 实际用的配置, 思考档位)；临时译文没出结果时译文为 None。"""
        text = job.text
        # 很短的片段不带上文：内容太少时模型最容易把上文当成结果翻出来
        context = job.context if len("".join(text.split())) >= 6 else []
        if not cfg["online"] and is_hy(cfg["model"]):
            # hymt2 遇到识别乱码容易编句子：填了线上 API 就整句改送线上（按读音猜着翻），没填照常交给 hymt2
            pieces = garble.pieces(text)
            if pieces:
                log.info("判定为识别乱码%s %s：%s（乱码片段：%s）", "，改送线上 API" if fallback else "",
                         job.seg_id, text, " | ".join(pieces))
                if fallback:
                    cfg = fallback
        # 临时译文刷新很频繁，一律关闭思考；只有定稿走设置里的档位
        thinking = cfg.get("thinking", "off") if job.final else "off"
        tr = lambda ctx, th: translate(cfg, text, ctx, glossary, thinking=th)  # noqa: E731
        try:
            out = tr(context, thinking)
        except EmptyOutput as e:
            if not job.final:
                log.debug("临时翻译 %s 没出结果，沿用上一版：%s", job.seg_id, e)
                return None, cfg, thinking
            if thinking == "off":
                raise
            log.warning("翻译定稿 %s：%s（%s），改为关闭思考重翻", job.seg_id, e, THINKING_LABEL[thinking])
            self.warned.emit(f"有一句{e.args[0].removeprefix('线上 API：')}，已关闭思考重翻")
            thinking = "off"
            out = tr(context, thinking)
        if context and self._echoes_context(job.seg_id, job.text, out, context):
            log.warning("译文和前几句的译文高度重复，判定模型翻了上文，去掉上文重翻 %s：%s", job.seg_id, out)
            out = tr([], thinking)
        left = THAI_RE.findall(out)
        if left and job.final:
            # 译文里不能留泰文：通用模型带上提示重翻一次；hymt2 有线上 API 就改送线上
            if is_hy(cfg["model"]) and fallback:
                log.info("译文里还有泰文 %s（%s），改送线上 API 重翻：%s", job.seg_id, "、".join(left), out)
                cfg = fallback
                out = translate(cfg, text, context, glossary, thinking="off")
            elif not is_hy(cfg["model"]):
                log.info("译文里还有泰文 %s（%s），重翻一次：%s", job.seg_id, "、".join(left), out)
                out = translate(
                    cfg, text, context, glossary, thinking=thinking,
                    extra=f"注意：上一次的译文“{out}”里还留着泰文（{'、'.join(left)}）。重新翻译，"
                          "人名用中文音译，泰文字母写拉丁转写，其他词译出意思，译文里不能有任何泰文字母。")
            if THAI_RE.search(out):
                log.info("重翻后译文里仍有泰文 %s：%s", job.seg_id, out)
        return out, cfg, thinking

    def _run(self):
        while True:
            with self.cond:
                while not self.jobs:
                    self.cond.wait()
                job = self._pick()
                cfg, glossary, fallback = dict(self.cfg), list(self.glossary), self.fallback
                self.busy = True
            t = time.perf_counter()
            try:
                out, cfg, thinking = self._translate_job(job, cfg, glossary, fallback)
                if out is None:     # 临时译文没出结果：界面上沿用上一版
                    continue
                self._remember(job.seg_id, out)
                dt = time.perf_counter() - t
                tag = f"{cfg['model']}，{THINKING_LABEL[thinking]}" if cfg["online"] else cfg["model"]
                if job.final:
                    log.info("翻译定稿 %s（%.2fs，%s）：%s → %s", job.seg_id, dt, tag, job.text, out)
                else:
                    log.debug("临时翻译 %s（%.2fs，%s）：%s → %s", job.seg_id, dt, tag, job.text, out)
                self.done.emit(job.seg_id, job.text, out, job.final, dt)
            except Exception as e:  # noqa: BLE001 — 任何错误都要报给界面，线程不能死
                log.warning("翻译失败 %s（%s，%.2fs）：%s", job.seg_id, "定稿" if job.final else "临时",
                            time.perf_counter() - t, e, exc_info=not isinstance(e, RuntimeError))
                self.failed.emit(job.seg_id, job.text, str(e) or type(e).__name__)
            finally:
                with self.cond:
                    self.busy = False
