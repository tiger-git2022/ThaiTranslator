"""本机 HTTP 接收端：浏览器扩展把字幕 POST 到 http://127.0.0.1:<port>/captions。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QObject, Signal


class Bridge(QObject):
    captions = Signal(dict)   # {"session", "items": [{"id","speaker","text","rev"}], "ts"}
    status = Signal(dict)     # {"session", "host", "title", "inMeeting", "captionsOn", "ts"}
    hello = Signal(dict)      # 扩展定时报到：{"version", "ua"}

    def __init__(self):
        super().__init__()
        self.server = None

    def start(self, port):
        """开始监听；端口被占用时返回错误信息，成功返回 None。"""
        self.stop()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")

            def _reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                self._reply(200, {"app": "ThaiCaption"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._reply(400, {"ok": False})
                path = self.path.strip("/")
                if path == "captions" and isinstance(body.get("items"), list):
                    bridge.captions.emit(body)
                elif path == "status":
                    bridge.status.emit(body)
                elif path == "hello":
                    bridge.hello.emit(body)
                else:
                    return self._reply(404, {"ok": False})
                self._reply(200, {"ok": True})

            def log_message(self, *args):
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            self.server = None
            return f"端口 {port} 无法监听：{e}"
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return None

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
