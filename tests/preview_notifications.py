# -*- coding: utf-8 -*-
"""离屏渲染通知中心，出几张 PNG 预览。

【它解决什么】改界面时如果只能等程序跑起来再看，一轮改动就要开一次 60 秒的
        RAG 引擎初始化。这个脚本把「铃铛 + 抽屉」单独搭在一个假窗口里，
        `QT_QPA_PLATFORM=offscreen` 渲染成图，不需要显示器、不需要知识库、
        **不碰用户数据**。

【产物】`tmp/preview_*.png`

【用法】
        .venv\\Scripts\\python.exe tests/preview_notifications.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# ★ 必须在 import PyQt6 **之前**设成离屏，否则会尝试连显示器
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ★ 离屏插件用的是 basic 字体库，默认找不到系统字体 —— 不指一下，所有中文
#   都会渲染成方块（第一次出图就是这样，图上只剩方块）。
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
# ★ 隔离数据目录：万一以后这个脚本要读写收件箱，也不碰真实数据
os.environ["COTTON_AGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="cotton_preview_")

from PyQt6.QtCore import Qt                              # noqa: E402
from PyQt6.QtGui import QFont                            # noqa: E402
from PyQt6.QtWidgets import (                            # noqa: E402-QtWidgets
    QApplication, QFrame, QHBoxLayout, QLabel, QToolButton,
    QVBoxLayout, QWidget,
)

from core.notifications import Notification               # noqa: E402
from ui.notifications import (                           # noqa: E402
    NOTIFY_STYLE, NotificationButton, NotificationDrawer, drawer_width_for,
)

OUT = ROOT / "tmp"
OUT.mkdir(parents=True, exist_ok=True)

BASE_STYLE = """
QWidget { background-color: #F7F7F8; }
QFrame#fakeBubble { background-color: #FFFFFF; border-radius: 10px; }
QFrame#fakeBubbleUser { background-color: #DCEBFF; border-radius: 10px; }
QLabel#fakeText { color: #1f1f1f; font-size: 13px; }
QFrame#fakeToolbar { background-color: #F7F7F8; }
QToolButton#fakeHamburger {
    background: transparent; border: none; font-size: 15px; color: #3A3A3C;
}
"""


def _bubble(text: str, *, user: bool = False) -> QFrame:
    f = QFrame()
    f.setObjectName("fakeBubbleUser" if user else "fakeBubble")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(12, 9, 12, 9)
    lab = QLabel(text)
    lab.setObjectName("fakeText")
    lab.setWordWrap(True)
    lay.addWidget(lab)
    align = Qt.AlignmentFlag.AlignRight if user else Qt.AlignmentFlag.AlignLeft
    f.setMaximumWidth(430)
    f.setProperty("align", align)
    return f


def _items() -> list[Notification]:
    now = datetime.now().replace(microsecond=0)
    return [
        Notification(
            id="daily_brief-2026-10-31", job_id="daily_brief",
            title="每日农情简报 · 阿克苏地区",
            body="今天多云转晴，白天最高 17℃，夜间降到 1℃。"
                 "夜间低温对未吐絮的棉铃有风险，建议关注北坡地块。"
                 "田间事项：一、白天完成最后一次化控；二、检查滴灌带末端压力；"
                 "三、夜间降温前不要灌水。",
            created_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"),
            read_at="", path="reports/daily_brief-2026-10-31.md"),
        Notification(
            id="weather_watch-2026-10-31", job_id="weather_watch",
            title="极端天气预警：低温",
            body="明天夜间最低 1℃，低于 5℃ 阈值。建议提前收拢已吐絮地块的采棉机作业安排。",
            created_at=(now - timedelta(hours=4)).isoformat(timespec="seconds"),
            read_at="", path="reports/weather_watch-2026-10-31.md"),
        Notification(
            id="daily_brief-2026-10-30", job_id="daily_brief",
            title="每日农情简报 · 阿克苏地区",
            body="昨天晴，18℃，风力 3 级。适合整枝与田间检查。",
            created_at=(now - timedelta(days=1)).isoformat(timespec="seconds"),
            read_at=(now - timedelta(hours=20)).isoformat(timespec="seconds"),
            path="reports/daily_brief-2026-10-30.md"),
    ]


def build_window() -> tuple[QWidget, NotificationButton, NotificationDrawer]:
    """搭一个假窗口：顶部工具栏 + 假聊天区 + 右侧抽屉。"""
    win = QWidget()
    win.setWindowTitle("预览")
    win.resize(960, 560)
    win.setStyleSheet(BASE_STYLE + NOTIFY_STYLE)

    root = QHBoxLayout(win)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    chat = QWidget()
    cl = QVBoxLayout(chat)
    cl.setContentsMargins(10, 6, 10, 10)
    cl.setSpacing(8)

    # 顶部工具栏：汉堡 + 弹性 + 铃铛（和主窗口一致）
    bar = QFrame()
    bar.setObjectName("fakeToolbar")
    bl = QHBoxLayout(bar)
    bl.setContentsMargins(4, 4, 4, 0)
    ham = QToolButton()
    ham.setObjectName("fakeHamburger")
    ham.setText("☰")
    bl.addWidget(ham)
    bl.addStretch(1)
    bell = NotificationButton()
    bl.addWidget(bell)
    cl.addWidget(bar)

    cl.addWidget(_bubble("明天阿克苏会不会降温？", user=True))
    cl.addWidget(_bubble("明天夜间最低 1℃，低于 5℃ 的霜冻阈值。"
                         "未吐絮的棉铃有风险，建议今晚不要灌水。"))
    cl.addWidget(_bubble("那化控还做吗？", user=True))
    cl.addWidget(_bubble("白天风小可以做，但要在下午四点前结束，"
                         "避免夜间低温加湿土。"))
    cl.addStretch(1)
    root.addWidget(chat, 1)

    drawer = NotificationDrawer()
    drawer.setFixedWidth(0)          # 默认收起，出图时按状态改
    root.addWidget(drawer, 0)
    return win, bell, drawer


def shoot(win: QWidget, name: str) -> Path:
    win.show()
    QApplication.processEvents()
    path = OUT / f"preview_{name}.png"
    win.grab().save(str(path), "PNG")
    return path


def main() -> int:
    app = QApplication([sys.argv[0]])
    app.setFont(QFont("Microsoft YaHei UI", 9))

    made: list[Path] = []

    # ① 有 3 条未读、抽屉关着 —— 只看铃铛和角标
    win, bell, drawer = build_window()
    bell.set_unread(3)
    drawer.set_items(_items())
    made.append(shoot(win, "1_badge"))

    # ② 抽屉打开
    drawer.setFixedWidth(drawer_width_for(win.height()))
    bell.set_unread(0)
    made.append(shoot(win, "2_drawer"))

    # ③ 空态
    drawer.set_items([])
    bell.set_unread(0)
    made.append(shoot(win, "3_empty"))

    for p in made:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())