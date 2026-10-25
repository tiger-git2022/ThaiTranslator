"""修正 qfluentwidgets 菜单项宽度：长菜单项（如带全角括号的）右边被截掉。

原来的算法有两个问题：
1. 第一次加菜单项时样式表还没生效，用的是默认的小字号量宽度，实际显示是 14px 字号；
2. 用 boundingRect（字形实际墨迹）量宽度，全角括号、省略号这类右边留白的字会少算。
这里先让样式表生效，再按字的排版宽度（horizontalAdvance）补足差值，并多留一点余量。
所有 RoundMenu 及其子类（托盘菜单、亚克力菜单、勾选菜单）都会用到。
"""
from PySide6.QtCore import QSize
from qfluentwidgets import RoundMenu

PADDING = 12


def install():
    if getattr(RoundMenu, "_width_fixed", False):
        return
    original = RoundMenu._adjustItemText

    def adjust(self, item, action):
        self.view.ensurePolished()
        w = original(self, item, action)
        fm = self.view.fontMetrics()
        text = item.text()
        w += max(0, fm.horizontalAdvance(text) - fm.boundingRect(text).width()) + PADDING
        item.setSizeHint(QSize(w, self.itemHeight))
        return w

    RoundMenu._adjustItemText = adjust
    RoundMenu._width_fixed = True


def needed_width(menu, text):
    """菜单项至少要有的宽度（自检用）：图标和左右边距约 60px，加上文字的排版宽度。"""
    menu.view.ensurePolished()
    return 60 + menu.view.fontMetrics().horizontalAdvance(text)
