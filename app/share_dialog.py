"""主机共享对话框：显示局域网观看地址的二维码和观看码，手机 / 电脑扫码或输入地址即可看。"""
import qrcode
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, FluentIcon as FIF, InfoBar, InfoBarPosition,
                            MessageBoxBase, PushButton, StrongBodyLabel, SubtitleLabel)

GREY = (QColor("#606060"), QColor("#a0a0a0"))


def qr_pixmap(text, size):
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    cell = max(1, size // n)
    pm = QPixmap(cell * n, cell * n)
    pm.fill(Qt.white)
    p = QPainter(pm)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if on:
                p.drawRect(x * cell, y * cell, cell, cell)
    p.end()
    return pm


class ShareDialog(MessageBoxBase):
    def __init__(self, parent, name, code, port, addresses, preferred_ip, viewers, on_ip_changed, on_stop):
        """addresses：[(网卡名, IP, 广播地址)]；viewers：返回当前观看人数的函数。"""
        super().__init__(parent)
        self.code, self.port, self.viewers = code, port, viewers
        self.addresses = addresses
        self.on_ip_changed = on_ip_changed
        self.url = ""

        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(SubtitleLabel(f"正在共享：{name}"))
        tip = BodyLabel("手机或其他电脑连上和本机相同的 Wi-Fi / 局域网，扫下面的二维码即可观看，不用装任何东西。"
                        "装了本软件的电脑也可以点“观看共享”自动找到本机。")
        tip.setWordWrap(True)
        self.viewLayout.addWidget(tip)

        self.ip_box = ComboBox()
        for nic, ip, _ in addresses:
            self.ip_box.addItem(f"{ip}（{nic}）")
        if not addresses:
            self.ip_box.addItem("没有找到局域网地址")
            self.ip_box.setEnabled(False)
        row = QHBoxLayout()
        lab = CaptionLabel("本机地址")
        lab.setTextColor(*GREY)
        row.addWidget(lab)
        row.addWidget(self.ip_box, 1)
        self.viewLayout.addLayout(row)

        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignCenter)
        self.viewLayout.addWidget(self.qr, 0, Qt.AlignCenter)

        self.addr = StrongBodyLabel()
        self.addr.setAlignment(Qt.AlignCenter)
        self.addr.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.viewLayout.addWidget(self.addr)
        code_lab = SubtitleLabel(f"观看码  {code[:3]} {code[3:]}")
        f = code_lab.font()
        f.setLetterSpacing(QFont.AbsoluteSpacing, 2)
        code_lab.setFont(f)
        code_lab.setAlignment(Qt.AlignCenter)
        self.viewLayout.addWidget(code_lab)

        self.count = CaptionLabel()
        self.count.setAlignment(Qt.AlignCenter)
        self.count.setTextColor(*GREY)
        self.viewLayout.addWidget(self.count)
        warn = CaptionLabel("连不上时：确认在同一个 Wi-Fi（访客网络通常禁止设备互访）；第一次共享时 Windows 防火墙弹窗要点“允许”。")
        warn.setWordWrap(True)
        warn.setTextColor(*GREY)
        self.viewLayout.addWidget(warn)

        btns = QHBoxLayout()
        copy = PushButton(FIF.COPY, "复制链接")
        copy.clicked.connect(self.copy)
        stop = PushButton(FIF.CLOSE, "停止共享")
        stop.clicked.connect(lambda: (on_stop(), self.accept()))
        btns.addWidget(copy)
        btns.addWidget(stop)
        btns.addStretch()
        self.viewLayout.addLayout(btns)

        self.yesButton.setText("关闭（继续共享）")
        self.cancelButton.hide()
        self.widget.setMinimumWidth(540)

        ips = [ip for _, ip, _ in addresses]
        self.ip_box.setCurrentIndex(ips.index(preferred_ip) if preferred_ip in ips else 0)
        self.ip_box.currentIndexChanged.connect(self._refresh_qr)
        self._refresh_qr()
        self.timer = QTimer(self, interval=1000, timeout=self._refresh_count)
        self.timer.start()
        self._refresh_count()

    def _refresh_qr(self):
        if not self.addresses:
            self.addr.setText("本机没有连接局域网")
            return
        ip = self.addresses[max(0, self.ip_box.currentIndex())][1]
        self.on_ip_changed(ip)
        base = f"http://{ip}:{self.port}/"
        self.url = f"{base}#k={self.code}"
        dpr = self.devicePixelRatioF()
        pm = qr_pixmap(self.url, int(240 * dpr))
        pm.setDevicePixelRatio(dpr)
        self.qr.setPixmap(pm)
        self.addr.setText(base)

    def _refresh_count(self):
        n = self.viewers()
        self.count.setText(f"{n} 台设备在看" if n else "还没有设备在看")

    def copy(self):
        if self.url:
            QGuiApplication.clipboard().setText(self.url)
            InfoBar.success("已复制", "链接里带着观看码，打开就能看", parent=self, duration=2500,
                            position=InfoBarPosition.TOP)
