"""触控板滚动减速。

qfluentwidgets 的平滑滚动只处理鼠标滚轮（每格 120），触控板的细碎滚动会交还给 Qt 默认处理，
在高缩放比例的屏幕上滚得很快。这里在滚动区域上装一个事件过滤器，触控板事件按比例自己滚动。
"""
from PySide6.QtCore import QEvent, QObject


class TouchpadScroll(QObject):
    def __init__(self, area, speed):
        """speed：返回滚动速度百分比（10~100）的函数，设置里改了立即生效。"""
        super().__init__(area)
        self.area, self.speed = area, speed
        self.acc = 0.0
        area.viewport().installEventFilter(self)   # 后装的过滤器先执行，排在组件库的平滑滚动之前

    def eventFilter(self, obj, e):
        if e.type() != QEvent.Type.Wheel:
            return False
        ady = e.angleDelta().y()
        pdy = e.pixelDelta().y()
        if not pdy and (not ady or ady % 120 == 0):
            return False                           # 普通鼠标滚轮：交给组件库的平滑滚动
        # Qt 默认大约每 120 单位滚 60 像素；按设置的百分比缩小
        dy = pdy if pdy else ady * 0.5
        self.acc += dy * self.speed() / 100
        step = int(self.acc)
        if step:
            self.acc -= step
            bar = self.area.verticalScrollBar()
            bar.setValue(bar.value() - step)
        return True


def tame(area, speed):
    return TouchpadScroll(area, speed)
