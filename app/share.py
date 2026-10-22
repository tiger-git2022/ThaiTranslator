"""观看端：在局域网里发现正在共享的主机，用 SSE 实时接收字幕。"""
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface

from share_server import DISCOVER_MSG, DISCOVERY_PORT
from tracker import Segment

log = logging.getLogger("watch")
SEG_FIELDS = ("id", "index", "speaker", "text", "start", "zh", "zh_src", "final", "zh_final")
SKIP_IF = ("tailscale", "vethernet", "vmware", "virtualbox", "wsl", "loopback", "bluetooth", "zerotier")


def seg_to_dict(seg):
    return {k: getattr(seg, k) for k in SEG_FIELDS}


def seg_from_dict(d):
    seg = Segment(str(d.get("id")), int(d.get("index", 0)), str(d.get("speaker", "")), str(d.get("text", "")),
                  float(d.get("start") or time.time()), 0.0)
    seg.zh, seg.zh_src = str(d.get("zh", "")), str(d.get("zh_src", ""))
    seg.final, seg.zh_final = bool(d.get("final")), bool(d.get("zh_final"))
    return seg


def lan_addresses():
    """本机的局域网 IPv4 地址 [(网卡名, IP, 广播地址)]，跳过 Tailscale、虚拟网卡等。"""
    out = []
    for iface in QNetworkInterface.allInterfaces():
        flags = iface.flags()
        if not (flags & QNetworkInterface.IsUp and flags & QNetworkInterface.IsRunning) \
                or flags & QNetworkInterface.IsLoopBack:
            continue
        name = iface.humanReadableName()
        if any(k in name.lower() for k in SKIP_IF):
            continue
        for entry in iface.addressEntries():
            ip = entry.ip()
            if ip.protocol() != QAbstractSocket.IPv4Protocol:
                continue
            text = ip.toString()
            if text.startswith(("169.254.", "100.")):   # 自动私有地址、Tailscale（100.64.0.0/10）
                continue
            out.append((name, text, entry.broadcast().toString()))
    # Wi-Fi / 以太网排前面
    out.sort(key=lambda a: not any(k in a[0].lower() for k in ("wi-fi", "wlan", "以太网", "ethernet")))
    return out


def discover(timeout=1.5):
    """广播询问局域网里正在共享的主机，返回 [{"name", "url", "code", "ip"}]。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)
    targets = {"255.255.255.255"} | {b for _, _, b in lan_addresses() if b}
    found, end = {}, time.monotonic() + timeout
    try:
        sock.bind(("0.0.0.0", 0))
        # 也问一下本机（主机和观看端在同一台电脑上测试时）
        for t in targets | {"127.0.0.1"}:
            try:
                sock.sendto(DISCOVER_MSG, (t, DISCOVERY_PORT))
            except OSError:
                pass
        while time.monotonic() < end:
            try:
                data, (ip, _) = sock.recvfrom(1024)
                info = json.loads(data.decode("utf-8"))
            except (socket.timeout, ValueError):
                continue
            except OSError:
                break
            if info.get("app") == "ThaiCaption":
                url = f"http://{ip}:{info['port']}"
                key = (info.get("name"), info["port"], info.get("code"))
                # 同一台主机可能从多个地址回复（含 127.0.0.1），优先保留局域网地址
                if key not in found or found[key]["ip"] == "127.0.0.1":
                    found[key] = {"name": info.get("name", ip), "url": url, "code": info.get("code", ""), "ip": ip}
    finally:
        sock.close()
    return list(found.values())


def parse_address(text):
    """把用户输入的 “192.168.1.5:47322/#k=123456” 之类的地址拆成 (url, 观看码)。"""
    text = text.strip()
    code = ""
    if "#k=" in text:
        text, code = text.split("#k=", 1)
    if "://" not in text:
        text = "http://" + text
    return text.rstrip("/").rstrip("#").rstrip("/"), code.strip()


class RemoteStore(QObject):
    """观看时的字幕存储，接口和 Tracker 一致（segments / order / added / changed / cleared）。"""
    added = Signal(object)
    changed = Signal(object)
    cleared = Signal()

    def __init__(self):
        super().__init__()
        self.segments, self.order = {}, []

    def load(self, items):
        self.segments, self.order = {}, []
        self.cleared.emit()
        self.apply(items)

    def apply(self, items):
        for d in sorted(items, key=lambda x: x.get("index", 0)):
            seg = seg_from_dict(d)
            if seg.id in self.segments:
                self.segments[seg.id] = seg
                self.changed.emit(seg)
            else:
                self.segments[seg.id] = seg
                self.order.append(seg.id)
                self.added.emit(seg)


class Watcher(QObject):
    snapshot = Signal(str, list)     # 共享名称, 全部字幕
    update = Signal(list)
    state = Signal(str)              # 连接状态说明（空 = 正常）
    ended = Signal(str)              # 原因

    def __init__(self):
        super().__init__()
        self.token = None

    def watch(self, url, code):
        token = object()
        self.token = token
        threading.Thread(target=self._loop, args=(token, url, code), name="观看", daemon=True).start()

    def stop(self):
        self.token = None

    def _loop(self, token, url, code):
        log.info("开始观看 %s", url)
        fails = 0
        while self.token is token:
            try:
                req = urllib.request.Request(url + "/events", headers={"Authorization": f"Bearer {code}"})
                with urllib.request.urlopen(req, timeout=45) as r:
                    if fails:
                        log.info("观看重新连上 %s", url)
                    fails = 0
                    self.state.emit("")
                    kind = None
                    for raw in r:
                        if self.token is not token:
                            return
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line.startswith("event: "):
                            kind = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:])
                            if kind == "snapshot":
                                self.snapshot.emit(data.get("name", ""), data.get("segments", []))
                            elif kind == "update":
                                self.update.emit(data.get("segments", []))
                            elif kind == "end":
                                log.info("主机已停止共享")
                                self.token = None
                                self.ended.emit("主机已停止共享")
                                return
            except urllib.error.HTTPError as e:
                log.warning("观看连接 HTTP %s", e.code)
                if e.code == 401:
                    self.token = None
                    self.ended.emit("观看码不对")
                    return
                fails += 1
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                log.warning("观看连接中断（第 %d 次）：%s", fails + 1, e)
                fails += 1
            if self.token is token:
                self.state.emit(f"连接中断，正在重连（第 {fails} 次）…")
                time.sleep(min(10, 1 + fails))
