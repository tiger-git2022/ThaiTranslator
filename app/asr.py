"""语音识别输入源：录系统声音（WASAPI 回环）→ silero-vad 断句 → 发给识别服务（OpenAI 兼容
/v1/audio/transcriptions，tiger-pc 上的 whisper.cpp）→ 把结果当作“字幕”交给 Tracker 翻译。

说话过程中每隔 PARTIAL_EVERY 秒把这一句目前的音频送去识别一次（临时结果）；
停顿 END_SILENCE 秒或一句超过 MAX_UTTER 秒就断句，送最终识别。
"""
import io
import itertools
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

log = logging.getLogger("asr")
SR = 16000
FRAME = 512                 # silero-vad v5：16kHz 下每帧 512 个采样（32ms）
CONTEXT = 64
START_PROB, END_PROB = 0.5, 0.35
END_SILENCE = 0.6           # 停顿多久算一句结束（秒）
MIN_SPEECH = 0.3            # 太短的声音（咳嗽、点击）丢掉
PRE_ROLL = 0.25             # 句首往前多带一点，避免吃掉第一个音
SOFT_MAX = 6.0              # 一句超过这么多秒后，遇到短停顿就断开
SOFT_SILENCE = 0.25         # “短停顿”的长度
# 泰语口语的句尾语气词：最近一次临时识别以这些结尾，才允许在短停顿处提前断句，
# 免得在“ไม่ได้ว่า（并不是说）”这类没说完的地方切开，把否定和后半句分到两段导致意思翻反
SENTENCE_END = ("ครับ", "ครับผม", "ค่ะ", "คะ", "นะ", "เนาะ", "จ้ะ", "จ้า", "จ๊ะ", "ฮะ", "เลย", "แล้ว", "ไหม",
                "มั้ย", "หรอ", "เหรอ", "ล่ะ", "อะ", "ด้วย", "ไง", "สิ", "ซิ", "หรือเปล่า", "ป่ะ", "ปะ", "เอง")


def ends_sentence(text):
    t = text.rstrip(" .,!?…ๆ​")
    return bool(t) and t.endswith(SENTENCE_END)
MAX_UTTER = 12.0            # 一句最长这么多秒就强制断开
PARTIAL_EVERY = 0.8

MODEL_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "models"
# Whisper 在静音 / 噪声上常见的幻觉句子，整句等于这些就丢掉
HALLUCINATIONS = {"ขอบคุณที่รับชม", "ขอบคุณครับ", "ขอบคุณค่ะ", "ติดตามช่องของเรา", "subscribe", "you", "thank you.",
                  "ซับไตเติ้ลโดย", "คำบรรยายโดย",
                  # 背景音乐 / 音效时 Whisper 输出的标记（“音乐声”“歌声”“掌声”“笑声”等）
                  "เสียงดนตรี", "ดนตรี", "เสียงเพลง", "เพลง", "เสียงปรบมือ", "เสียงหัวเราะ", "music", "[music]"}


def list_loopback_devices():
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        return [d["name"] for d in p.get_loopback_device_info_generator()]
    finally:
        p.terminate()


class SileroVad:
    def __init__(self):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(str(MODEL_DIR / "silero_vad.onnx"), opts,
                                         providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context = np.zeros((1, CONTEXT), np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.context, frame[None, :]], axis=1)
        out, self.state = self.sess.run(None, {"input": x, "state": self.state, "sr": np.array(SR, np.int64)})
        self.context = x[:, -CONTEXT:]
        return float(out[0, 0])


def wav_bytes(samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(cfg, samples, timeout=30):
    """调用 OpenAI 兼容的语音识别接口，返回文本。"""
    boundary = uuid.uuid4().hex
    fields = {"model": cfg.get("asr_model", "whisper-th"), "language": cfg.get("asr_language", "th"),
              "response_format": "json", "temperature": "0"}
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
               f"Content-Type: audio/wav\r\n\r\n".encode())
    body.write(wav_bytes(samples))
    body.write(f"\r\n--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    req = urllib.request.Request(cfg["api_base"].rstrip("/") + "/audio/transcriptions", body.getvalue(), headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"识别服务 HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上识别服务：{e.reason}") from None
    except TimeoutError:
        raise RuntimeError("识别服务响应超时") from None
    return " ".join(str(data.get("text", "")).split())


def clean(text):
    t = text.strip().strip("。.").strip()
    if not t or t.lower() in HALLUCINATIONS or t.startswith(("[", "(")) and t.endswith(("]", ")")):
        return ""
    return text.strip()


class AsrSource(QObject):
    """发出与浏览器扩展相同格式的字幕条目：items=[{id, speaker, text, rev}]，以及定稿通知。"""
    items = Signal(list)
    finalized = Signal(str)
    level = Signal(float)           # 当前音量（0~1），界面上显示“正在听”
    status = Signal(str, bool)      # 状态说明, 是否正常

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.running = False
        self.thread = None
        self.cond = threading.Condition()
        self.jobs = {}              # 字幕 id -> (音频, 是否最终)
        self.busy = False
        self.finished = set()       # 已经出了最终结果的句子；之后回来的临时结果要丢掉
        self.latest = {}            # 句子 id -> 最近一次临时识别的文字（断句时判断是否说完）
        self.rev = itertools.count(1)
        self.session = uuid.uuid4().hex[:6]
        threading.Thread(target=self._asr_loop, name="识别", daemon=True).start()

    # ---------- 启停 ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._capture_loop, name="录音", daemon=True)
        self.thread.start()

    def join(self, timeout=1.5):
        """等录音线程真正停下（退出软件前调用，免得线程往已销毁的界面发信号）。"""
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout)

    def stop(self):
        if self.running:
            log.info("停止录音")
        self.running = False

    # ---------- 录音 + 断句 ----------
    def _open_stream(self, pa_mod, p):
        name = self.cfg.get("asr_device") or ""
        dev = None
        if name:
            dev = next((d for d in p.get_loopback_device_info_generator() if d["name"] == name), None)
        if dev is None:
            dev = p.get_default_wasapi_loopback()
        rate, ch = int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"]))
        stream = p.open(format=pa_mod.paFloat32, channels=ch, rate=rate, input=True,
                        input_device_index=dev["index"], frames_per_buffer=int(rate * 0.05))
        return stream, rate, ch, dev["name"]

    def _capture_loop(self):
        try:
            import pyaudiowpatch as pa_mod
            from scipy.signal import resample_poly
            vad = SileroVad()
            p = pa_mod.PyAudio()
            stream, rate, ch, dev_name = self._open_stream(pa_mod, p)
        except Exception as e:  # noqa: BLE001
            log.exception("无法打开录音设备")
            self.status.emit(f"无法录制系统声音：{e}", False)
            self.running = False
            return
        log.info("开始录音：%s（%d Hz，%d 声道）", dev_name, rate, ch)
        self.status.emit(f"正在听：{dev_name}", True)
        from math import gcd
        g = gcd(rate, SR)
        up, down = SR // g, rate // g
        pending = np.zeros(0, np.float32)
        pre = np.zeros(0, np.float32)            # 没说话时保留最近一小段，作为句首
        utter = None                              # 当前这句的音频片段列表
        utter_len = silence = 0.0
        seg_id, last_partial = None, 0.0
        seq = itertools.count(1)
        chunk = int(rate * 0.05)
        last_data = time.monotonic()
        try:
            while self.running:
                # 电脑没在放声音时，WASAPI 回环设备不给任何数据（read 会一直卡住），
                # 所以只在有数据时读；超过 0.1 秒没数据就按流逝的时间补静音，让断句照常进行
                if stream.get_read_available() >= chunk:
                    raw = stream.read(chunk, exception_on_overflow=False)
                    x = np.frombuffer(raw, np.float32).reshape(-1, ch).mean(axis=1)
                    x = resample_poly(x, up, down).astype(np.float32) if (up, down) != (1, 1) else x
                    last_data = time.monotonic()
                else:
                    gap = time.monotonic() - last_data
                    if gap < 0.1:
                        time.sleep(0.01)
                        continue
                    x = np.zeros(int(SR * gap), np.float32)
                    last_data = time.monotonic()
                self.level.emit(float(min(1.0, np.sqrt(np.mean(x ** 2)) * 8)) if len(x) else 0.0)
                pending = np.concatenate([pending, x])
                while len(pending) >= FRAME:
                    frame, pending = pending[:FRAME], pending[FRAME:]
                    prob = vad(frame)
                    if utter is None:
                        pre = np.concatenate([pre, frame])[-int(SR * PRE_ROLL):]
                        if prob >= START_PROB:
                            utter, utter_len, silence = [pre.copy(), frame], len(pre) / SR + FRAME / SR, 0.0
                            seg_id, last_partial = f"asr-{self.session}-{next(seq)}", time.monotonic()
                        continue
                    utter.append(frame)
                    utter_len += FRAME / SR
                    silence = silence + FRAME / SR if prob < END_PROB else 0.0
                    soft = (utter_len >= SOFT_MAX and silence >= SOFT_SILENCE
                            and ends_sentence(self.latest.get(seg_id, "")))
                    ended = silence >= END_SILENCE or soft
                    if ended or utter_len >= MAX_UTTER:
                        audio = np.concatenate(utter)
                        if utter_len - silence >= MIN_SPEECH:
                            log.debug("断句 %s：%.1fs（%s）", seg_id, utter_len, "停顿" if ended else "超长强制断开")
                            self._submit(seg_id, audio, True)
                        else:
                            log.debug("丢弃过短的声音 %s：%.2fs", seg_id, utter_len - silence)
                        utter, pre = None, np.zeros(0, np.float32)
                        if not ended:   # 超长被强制断开：后面紧接着的话算新的一句
                            utter, utter_len, silence = [], 0.0, 0.0
                            seg_id, last_partial = f"asr-{self.session}-{next(seq)}", time.monotonic()
                        vad.reset() if ended else None
                    elif time.monotonic() - last_partial >= PARTIAL_EVERY and utter_len >= 0.8:
                        last_partial = time.monotonic()
                        self._submit(seg_id, np.concatenate(utter), False)
        except Exception as e:  # noqa: BLE001
            log.exception("录音中断")
            self.status.emit(f"录音中断：{e}", False)
        finally:
            try:
                stream.stop_stream()
                stream.close()
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.running = False
            try:
                self.level.emit(0.0)
            except RuntimeError:     # 软件退出时界面对象可能已经销毁
                pass

    # ---------- 识别 ----------
    def _submit(self, seg_id, audio, final):
        with self.cond:
            old = self.jobs.get(seg_id)
            if old and old[1] and not final:
                return          # 已经有最终任务了，不再用临时结果覆盖
            if not final and self.busy:
                # 识别忙时临时结果只保留最新一条，免得排队越积越多
                self.jobs = {k: v for k, v in self.jobs.items() if v[1]}
            self.jobs[seg_id] = (audio, final)
            self.cond.notify()

    def _asr_loop(self):
        while True:
            with self.cond:
                while not self.jobs:
                    self.cond.wait()
                finals = [k for k, v in self.jobs.items() if v[1]]
                seg_id = finals[0] if finals else next(iter(self.jobs))
                audio, final = self.jobs.pop(seg_id)
                self.busy = True
            try:
                t = time.perf_counter()
                raw = transcribe(self.cfg, audio)
                text = clean(raw)
                dt = time.perf_counter() - t
                if final:
                    log.info("识别 %s（%.1fs 音频，%.2fs）：%s%s", seg_id, len(audio) / SR, dt, raw,
                             "" if text or not raw else "  ← 判定为幻觉，已丢弃")
                else:
                    log.debug("临时识别 %s（%.1fs 音频，%.2fs）：%s", seg_id, len(audio) / SR, dt, raw)
                if not final:
                    self.latest[seg_id] = text
                    if len(self.latest) > 20:
                        self.latest.pop(next(iter(self.latest)))
                if not final and seg_id in self.finished:
                    continue
                if final:
                    self.finished.add(seg_id)
                if text:
                    self.items.emit([{"id": seg_id, "speaker": "", "text": text, "rev": next(self.rev)}])
                if final:
                    # 最终结果被判为幻觉丢掉时也要定稿：保留最后一次临时结果，免得一直停在“正在说”
                    self.finalized.emit(seg_id)
                self.status.emit(f"正在听 · 识别 {time.perf_counter() - t:.1f} 秒", True)
            except Exception as e:  # noqa: BLE001
                log.warning("识别失败 %s：%s", seg_id, e, exc_info=not isinstance(e, RuntimeError))
                self.status.emit(str(e), False)
            finally:
                with self.cond:
                    self.busy = False
