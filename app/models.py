"""模型管家：需要用时自动让 llama-swap 加载翻译 / 识别模型，使用期间定时请求一次防止空闲卸载，并汇报状态。

llama-swap 按需加载、空闲 TTL（默认 300 秒）后卸载；所以这里在开始使用时先“预热”，
使用期间每 KEEPALIVE 秒发一个极小的真实请求刷新空闲计时。不再需要时就停止，让模型按 TTL 自然卸载。
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

import numpy as np
from PySide6.QtCore import QObject, Signal

from asr import transcribe

log = logging.getLogger("models")
KEEPALIVE = 240
POLL = 5            # 平时多久查一次状态
POLL_BUSY = 1       # 有模型在加载时更勤一些


class ModelKeeper(QObject):
    status = Signal(dict, str)      # {模型: 状态}，连接错误（空 = 正常）

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.lock = threading.Lock()
        self.needed = set()
        self.busy = set()           # 正在加载 / 刷新的模型
        self.last_touch = {}        # 模型 -> 最近一次请求的时间
        self.wake = threading.Event()
        threading.Thread(target=self._loop, name="模型管家", daemon=True).start()

    def need(self, models):
        """设置当前需要常驻的模型（空集合 = 都不需要，让它们按 TTL 自然卸载）。"""
        models = set(models)
        with self.lock:
            changed = models != self.needed
            self.needed = models
        if changed:
            self.wake.set()     # 需求变了立刻处理，不等下一轮

    def touched(self, model):
        """软件正常使用模型时调用（翻译 / 识别成功），省掉多余的保活请求。"""
        with self.lock:
            self.last_touch[model] = time.monotonic()

    # ---------- 内部 ----------
    def _api(self, path, timeout=5):
        req = urllib.request.Request(self.cfg["api_base"].rstrip("/").removesuffix("/v1") + path,
                                     headers={"Authorization": f"Bearer {self.cfg.get('api_key', '')}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _touch(self, model):
        """发一个极小的真实请求：没加载时会触发加载，已加载时刷新空闲计时。"""
        t = time.monotonic()
        log.debug("请求模型 %s（加载 / 保活）", model)
        try:
            if model == self.cfg.get("asr_model"):
                transcribe(self.cfg, np.zeros(8000, np.float32), timeout=180)
            else:
                body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}],
                                   "max_tokens": 1}).encode()
                req = urllib.request.Request(self.cfg["api_base"].rstrip("/") + "/chat/completions", body,
                                             {"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.cfg.get('api_key', '')}"})
                urllib.request.urlopen(req, timeout=180).read()
            with self.lock:
                self.last_touch[model] = time.monotonic()
            log.debug("模型 %s 响应正常（%.1fs）", model, time.monotonic() - t)
        except Exception as e:  # noqa: BLE001 — 失败由状态轮询报告
            log.warning("请求模型 %s 失败（%.1fs）：%s", model, time.monotonic() - t, e)
        finally:
            with self.lock:
                self.busy.discard(model)

    def _loop(self):
        while True:
            err, running = "", {}
            try:
                running = {r.get("model"): r.get("state") for r in self._api("/running").get("running", [])}
            except urllib.error.HTTPError as e:
                err = "密钥不正确" if e.code == 401 else f"HTTP {e.code}"
            except Exception:  # noqa: BLE001
                err = "连不上 tiger-pc 的模型服务（llama-swap 没启动，或本机没连 Tailscale）"
            now = time.monotonic()
            with self.lock:
                needed, busy = set(self.needed), set(self.busy)
                todo = []
                if not err:
                    for m in needed - busy:
                        state = running.get(m)
                        if state is None or now - self.last_touch.get(m, 0) >= KEEPALIVE:
                            if state not in ("starting",):
                                todo.append(m)
                    self.busy |= set(todo)
            for m in todo:
                threading.Thread(target=self._touch, args=(m,), name=f"加载-{m}", daemon=True).start()
            states = {}
            for m in needed | set(running):
                st = running.get(m)
                if st == "ready":
                    states[m] = "已就绪"
                elif st in ("stopping", "shutdown"):
                    states[m] = "卸载中…"
                elif st or m in busy or m in todo:
                    states[m] = "加载中…"
                else:
                    states[m] = "未加载"
            if (states, err) != getattr(self, "_last", None):
                self._last = (states, err)
                log.info("模型状态：%s%s", states or "（无）", f" · 错误：{err}" if err else "")
            self.status.emit(states, err)
            loading = any(v == "加载中…" for v in states.values())
            self.wake.wait(POLL_BUSY if loading else POLL)
            self.wake.clear()
