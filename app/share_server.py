"""主机内置的局域网共享服务：开始共享后，同一局域网的手机 / 电脑用浏览器或本软件就能看字幕。

    GET /                    观看网页（web/viewer.html）
    GET /events              SSE：先发 snapshot，再发 update，停止共享时发 end（需要观看码）
    GET /info                共享名称、字幕条数、观看人数（需要观看码）
    UDP 47323                局域网发现：收到 DISCOVER 就回复名称、端口和观看码

观看码放在 Authorization: Bearer <码> 或 ?k=<码> 里。
"""
import json
import logging
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("share")
DISCOVERY_PORT = 47323
DISCOVER_MSG = b"THAICAPTION_DISCOVER"
KEEPALIVE = 15
WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"


def new_code():
    return f"{secrets.randbelow(10**6):06d}"


class ShareServer(QObject):
    viewers_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.httpd = None
        self.udp = None
        self.cond = threading.Condition()
        self.name = self.code = ""
        self.port = 0
        self._reset()

    def _reset(self):
        self.segs, self.seg_ver = {}, {}
        self.version = self.epoch = self.viewers = 0
        self.ended = False

    @property
    def running(self):
        return self.httpd is not None

    # ---------- 启停 ----------
    def start(self, name, code, port, segments):
        """开始共享；端口被占用时返回错误信息，成功返回 None。"""
        self.stop()
        with self.cond:
            self._reset()
            self.name, self.code, self.port = name, code, port
            for seg in segments:
                self.segs[seg["id"]] = seg
                self.seg_ver[seg["id"]] = 0
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), self._handler())
        except OSError as e:
            log.warning("共享端口 %d 无法监听：%s", port, e)
            return f"端口 {port} 无法监听：{e}"
        httpd.daemon_threads = True
        self.httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(("0.0.0.0", DISCOVERY_PORT))
            self.udp = udp
            threading.Thread(target=self._discovery, args=(udp,), daemon=True).start()
        except OSError as e:
            log.warning("局域网发现端口 %d 不可用（仍可扫码观看）：%s", DISCOVERY_PORT, e)
            self.udp = None   # 发现功能不可用时，仍可扫码或手动输入地址
        log.info("开始共享「%s」：端口 %d，已有 %d 条字幕", name, port, len(segments))
        return None

    def stop(self):
        httpd, udp = self.httpd, self.udp
        self.httpd = self.udp = None
        with self.cond:
            self.ended = True
            self.cond.notify_all()
        if udp:
            udp.close()
        if httpd:
            log.info("停止共享")
            # 先让观看端收到 end 事件再关
            threading.Timer(0.5, lambda: (httpd.shutdown(), httpd.server_close())).start()

    # ---------- 主机推送 ----------
    def update(self, seg):
        with self.cond:
            if not self.running:
                return
            self.version += 1
            self.segs[seg["id"]] = seg
            self.seg_ver[seg["id"]] = self.version
            self.cond.notify_all()

    def clear(self):
        with self.cond:
            self.segs.clear()
            self.seg_ver.clear()
            self.epoch += 1
            self.version += 1
            self.cond.notify_all()

    def _snapshot(self):
        return {"name": self.name, "segments": sorted(self.segs.values(), key=lambda s: s.get("index", 0))}

    def _set_viewers(self, delta):
        with self.cond:
            self.viewers += delta
            n = self.viewers
        self.viewers_changed.emit(n)

    # ---------- 局域网发现 ----------
    def _discovery(self, udp):
        while True:
            try:
                data, addr = udp.recvfrom(256)
            except OSError:
                return
            if data.strip() == DISCOVER_MSG:
                reply = json.dumps({"app": "ThaiCaption", "name": self.name, "port": self.port,
                                    "code": self.code}, ensure_ascii=False).encode("utf-8")
                try:
                    udp.sendto(reply, addr)
                except OSError:
                    pass

    # ---------- HTTP ----------
    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self, query):
                given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                given = given or (query.get("k") or [""])[0]
                if secrets.compare_digest(given.encode(), server.code.encode()):
                    return True
                self._json(401, {"error": "观看码不对"})
                return False

            def do_GET(self):
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path in ("/", "/index.html"):
                    return self._page()
                if url.path == "/favicon.png":
                    return self._page("favicon.png", "image/png")
                if url.path == "/info":
                    if self._authorized(query):
                        with server.cond:
                            info = {"name": server.name, "segments": len(server.segs), "viewers": server.viewers}
                        self._json(200, info)
                    return
                if url.path == "/events":
                    if self._authorized(query):
                        self._events()
                    return
                self._json(404, {"error": "not found"})

            def _page(self, name="viewer.html", ctype="text/html; charset=utf-8"):
                try:
                    data = (WEB_DIR / name).read_bytes()
                except OSError:
                    return self._json(404, {"error": "viewer.html 不存在"})
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send(self, kind, data):
                payload = json.dumps(data, ensure_ascii=False)
                self.wfile.write(f"event: {kind}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                server._set_viewers(1)
                log.info("观看设备接入：%s（%s）", self.client_address[0], self.headers.get("User-Agent", "")[:80])
                try:
                    with server.cond:
                        seen, epoch, snap = server.version, server.epoch, server._snapshot()
                    self._send("snapshot", snap)
                    while True:
                        with server.cond:
                            server.cond.wait_for(lambda: server.ended or server.version > seen, timeout=KEEPALIVE)
                            ended = server.ended
                            if not ended:
                                if server.epoch != epoch:
                                    epoch = server.epoch
                                    kind, data = "snapshot", server._snapshot()
                                else:
                                    changed = [server.segs[i] for i, v in server.seg_ver.items() if v > seen]
                                    kind, data = "update", {"segments": changed}
                                seen = server.version
                        if ended:
                            self._send("end", {})
                            return
                        if kind == "snapshot" or data["segments"]:
                            self._send(kind, data)
                        else:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except OSError:
                    pass
                finally:
                    server._set_viewers(-1)
                    log.info("观看设备离开：%s", self.client_address[0])

            def log_message(self, *args):
                pass

        return Handler
