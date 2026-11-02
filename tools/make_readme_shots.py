# -*- coding: utf-8 -*-
"""渲染 README 用的截图（离屏，不弹窗、不联网）。

生成四张：
  assets/readme/main.png        主界面（带一段问答）
  assets/readme/drawer.png      通知抽屉（三条推送）
  assets/readme/push.png        设置页的「推送」
  assets/readme/models.png      设置页的「模型」

用法：python tools/make_readme_shots.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("COTTON_AGENT_DATA_DIR", str(ROOT / ".shot_sandbox"))

OUT = ROOT / "assets" / "readme"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

    app = QApplication([sys.argv[0]])
    app.setFont(QFont("Microsoft YaHei UI", 9))

    # ---- 主窗口：用桩引擎，不碰真实会话与网络 ----
    import ui.main_window as M

    class _Sess:
        def __init__(self, sid, title, n=0):
            self.id = sid
            self.title = title
            self.message_count = n
            self.conversation_store = []

    class _Engine:
        def __init__(self):
            self.sessions = {"s1": _Sess("s1", "阿克苏 11 月田间管理"),
                             "s2": _Sess("s2", "今年机采棉含水率偏高怎么办"),
                             "s3": _Sess("s3", "近五年产量走势")}
            self.current_session_id = "s1"
            self.kb = None

        def create_session(self):
            return "s1"

        def get_session_list(self):
            return [{"id": s.id, "title": s.title,
                     "message_count": s.message_count,
                     "is_current": s.id == self.current_session_id}
                    for s in self.sessions.values()]

        def switch_session(self, sid):
            self.current_session_id = sid
            return True

    M.RAGEngine = _Engine

    win = M.CottonAgentWindow()
    win.resize(1080, 700)
    win.show()
    app.processEvents()

    # 一段问答，展示 Markdown 与表格的渲染
    win._add_bubble("阿克苏这两天适合打顶吗？", True)
    answer = (
        "**可以，建议在 28 日之前完成。**\n\n"
        "根据中央气象台预报，阿克苏未来三天以多云为主，白天温度 13 到 15 摄氏度，"
        "夜间 1 到 3 摄氏度，风力不大，适合打顶作业。\n\n"
        "| 日期 | 白天 | 夜间 | 风力 |\n"
        "|---|---|---|---|\n"
        "| 11-02 | 多云 15℃ | 多云 3℃ | 微风 |\n"
        "| 11-03 | 晴 16℃ | 晴 1℃ | 微风 |\n"
        "| 11-04 | 多云 14℃ | 多云 0℃ | 3 到 4 级 |\n\n"
        "打顶后一周内注意观察腋芽萌发情况。依据：2026 年棉花中后期生产技术指导意见。"
    )
    # ★ 气泡先装**转义后的纯文本**（模拟流式），结束时调 set_streaming(False)
    #   才会把 Markdown 渲染成 HTML。直接塞 HTML 会被当成文本显示。
    _b = win._add_bubble(answer, False)
    _b.set_streaming(False)
    import time as _t2
    for _ in range(40):
        app.processEvents()
        _t2.sleep(0.005)
    win.grab().save(str(OUT / "main.png"), "PNG")
    print("main.png")

    # ---- 通知抽屉：三条推送 ----
    notes = [
        ("weather_watch", "大风蓝色预警",
         "阿克苏预计未来 24 小时内将出现 6 级（或阵风 7 级）以上大风，请注意防范。\n\n"
         "农事建议：\n风来之前把地膜边角压土踩实，棚架与临时搭建物拉绳加固；"
         "风大时暂停喷药与化控，风停后及时巡田压膜。", "21:18"),
        ("daily_brief", "每日农情简报",
         "【今日】多云，3~15℃，东北风微风，无降水（实况 4.0℃，湿度 81%，08:02 更新）\n"
         "【明日】多云，3~14℃，北风微风，无降水\n"
         "【后天】多云，1~14℃，北风微风，无降水\n\n"
         "农事建议：\n今天白天风小、温度合适，适合化控收尾；"
         "明后天最低降到 1℃，夜里有霜冻就停灌。", "08:00"),
        ("price_daily", "每日棉花价格",
         "全国 CC Index 3128B：16,762（-48）\n"
         "新疆指数 3128B：16,204（-81）\n"
         "郑棉期货：15,255（+85）", "17:00"),
    ]
    for job, title, text, when in notes:
        win._on_schedule_notice({"type": "notice", "job": job, "title": title,
                                 "text": text, "path": "",
                                 "run_id": f"{job}-{when.replace(':','')}"})
    app.processEvents()
    win._refresh_notifications()
    win._open_notify_drawer()
    # ★ 抽屉宽度是动画展开的，必须让真实时间流逝，否则停在 0 宽（等于没开）
    import time as _t
    for _ in range(80):
        app.processEvents()
        _t.sleep(0.01)
    win.grab().save(str(OUT / "drawer.png"), "PNG")
    print("drawer.png")

    # ---- 设置页：推送与模型 ----
    from ui.settings_dialog import SettingsDialog
    from core import schedule as _sched_mod

    # ★ 推送页要显示任务行，必须给它一个真的调度器（沙箱目录，不碰本机配置）
    _s = _sched_mod.Scheduler(fetcher=lambda city, when: "")
    _s.load()
    dlg = SettingsDialog(scheduler=_s)
    dlg.resize(820, 640)
    dlg.show()
    names = [dlg._nav.item(i).text() for i in range(dlg._nav.count())]
    for page, name in (("推送", "push.png"), ("模型", "models.png")):
        if page in names:
            dlg._nav.setCurrentRow(names.index(page))
            import time as _t3
            for _ in range(30):
                app.processEvents()
                _t3.sleep(0.005)
            dlg.grab().save(str(OUT / name), "PNG")
            print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())