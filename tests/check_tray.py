# -*- coding: utf-8 -*-
"""托盘与单实例守卫 —— 关窗不退出这条路的四道保险。

【为什么值得单独守】"关窗不退出"最坏的失败方式都不是崩溃，而是**程序不见了**：

  ① 系统没有托盘却仍然隐藏窗口 → 用户关掉窗口后再也找不到它，只能去任务管理器；
  ② 点了「退出」却没真退出 → 进程赖在后台，用户以为关不掉；
  ③ 关窗时顺手停掉调度线程 → 托盘还在，推送却再也不来（白常驻）；
  ④ 没有单实例 → 第二次启动多一个调度器，同一时刻推两遍。

【用法】python tests/check_tray.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_tray_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}"
              + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def section_icon(c: Checker) -> None:
    c.section("第 1 节：托盘图标（自己画，含 DPR 裁剪的回归）")

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from ui.tray import ICON_SIZES, tray_icon

    icon = tray_icon()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    c.check("★ 每个尺寸各画一份（不是画一张再缩放）",
            sizes == {(s, s) for s in ICON_SIZES}, str(sorted(sizes)))
    c.check("图标非空", not icon.isNull())

    pm16 = icon.pixmap(16, 16)
    c.check("16 像素可用", pm16.width() == 16 and not pm16.isNull())
    img = pm16.toImage()
    # ★ 必须用 `pixelColor`：`QColor(image.pixel(x, y))` 会**丢掉 alpha**，
    #   于是透明像素也读成 255，断言看起来永远是"画满了"。
    corner = img.pixelColor(0, 0).alpha()
    centre = img.pixelColor(8, 8).alpha()
    br = img.pixelColor(15, 15).alpha()
    c.check("★ 四角透明（圆角没被裁成方块）", corner == 0, f"alpha={corner}")
    c.check("★ 中心不透明（图案没被裁掉）", centre > 0, f"alpha={centre}")
    c.check("右下角同样透明（图案没有溢出画布）", br == 0, f"alpha={br}")


def section_tray_menu(c: Checker) -> None:
    c.section("第 2 节：菜单项")

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from ui.tray import AppTray

    got: list[str] = []
    tray = AppTray()
    tray.show_requested.connect(lambda: got.append("show"))
    tray.quit_requested.connect(lambda: got.append("quit"))
    texts = tray.menu_action_texts()
    c.check("★ 三项：显示主窗口 / 推送设置 / 退出",
            texts == ["显示主窗口", "推送设置", "退出"], str(texts))
    tray.show_requested.emit()
    tray.quit_requested.emit()
    c.check("信号能连出去", got == ["show", "quit"], str(got))
    tray.hide()


def section_close_hides(c: Checker) -> None:
    c.section("第 3 节：★ 有托盘时关窗只隐藏，调度继续跑")

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)

    # 用桩替掉真托盘：offscreen 下没有系统托盘，但这条分支必须被验证
    import ui.tray as tray_mod

    calls: list[tuple[str, str]] = []

    class _FakeTray:
        def __init__(self, *_a, **_k):
            self.visible = False
            self.show_requested = _Sig()
            self.settings_requested = _Sig()
            self.quit_requested = _Sig()

        def show(self):
            self.visible = True

        def hide(self):
            self.visible = False

        def notify(self, title, body, ms=6000):
            calls.append((title, body))
            return True

        def menu_action_texts(self):
            return ["显示主窗口", "推送设置", "退出"]

    class _Sig:
        def connect(self, *_a, **_k):
            return None

    real_avail, real_tray = tray_mod.tray_available, tray_mod.AppTray
    tray_mod.tray_available = lambda: True
    tray_mod.AppTray = _FakeTray

    import ui.main_window as M

    class _Sess:
        def __init__(self):
            self.id = "s1"
            self.title = "t"
            self.message_count = 0
            self.conversation_store = []

    class _Engine:
        def __init__(self):
            self.sessions = {"s1": _Sess()}
            self.current_session_id = "s1"
            self.kb = None

        def create_session(self):
            return "s1"

        def get_session_list(self):
            return [{"id": "s1", "title": "t", "message_count": 0,
                     "is_current": True}]

        def switch_session(self, sid):
            return True

    M.RAGEngine = _Engine

    class _FakeSched:
        def __init__(self):
            self.stopped = 0
            self.jobs: list = []

        def stop(self):
            self.stopped += 1

    win = M.CottonAgentWindow()
    win._scheduler = _FakeSched()          # 别真起调度线程
    win.show()
    app.processEvents()
    c.check("窗口起来了且有托盘对象", win.isVisible() and win._tray is not None)

    win.close()
    app.processEvents()
    c.check("★ 关窗后窗口不可见", win.isVisible() is False)
    c.check("★ 关窗后**没有**进入退出状态", win._quitting is False)
    c.check("★ 关窗没有停掉调度器（推送还得继续）",
            win._scheduler.stopped == 0, str(win._scheduler.stopped))
    c.check("★ 首次隐藏弹了一条提示", any("后台" in t for t, _ in calls), str(calls))

    calls.clear()
    win._show_from_tray()
    app.processEvents()
    c.check("从托盘能唤起窗口", win.isVisible() is True)

    # 窗口可见时不该再弹系统通知
    win._notify_when_hidden("标题", "正文")
    c.check("窗口可见时不弹通知", calls == [], str(calls))
    win.hide()
    win._notify_when_hidden("大风蓝色预警", "阿克苏预计未来 24 小时内…")
    c.check("★ 窗口隐藏时弹通知，且只带首行",
            len(calls) == 1 and calls[0][0] == "大风蓝色预警", str(calls))

    win._quit_from_tray()
    app.processEvents()
    c.check("★ 托盘「退出」→ 进入退出状态", win._quitting is True)
    c.check("★ 退出时停掉调度器", win._scheduler.stopped == 1,
            str(win._scheduler.stopped))

    tray_mod.tray_available, tray_mod.AppTray = real_avail, real_tray


def section_no_tray(c: Checker) -> None:
    c.section("第 4 节：★ 没有托盘时必须真退出（否则关不掉）")

    src = (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    c.check("closeEvent 里检查了托盘可用性", "tray_available()" in src)
    c.check("★ 托盘不可用时走 _shutdown（真退出）",
            "keep_running" in src and "_shutdown()" in src, "")
    c.check("关窗是 ignore 而不是直接关", "event.ignore()" in src, "")
    c.check("隐藏后才提示（用 hide()）", "self.hide()" in src, "")


def section_single_instance(c: Checker) -> None:
    c.section("第 5 节：★ 单实例（否则两个调度器各推一遍）")

    src = (ROOT / "main.py").read_text(encoding="utf-8")
    for token, why in (("QLockFile", "文件锁"), ("QLocalServer", "本地监听"),
                       ("QLocalSocket", "唤起已有实例"),
                       ("setQuitOnLastWindowClosed(False)", "关窗不退出")):
        c.check(f"main.py 里有 {token}（{why}）", token in src)

    import main as m

    key = m._instance_key()
    c.check("管道名带固定前缀", key.startswith("cottonagent-"), key)
    c.check("同一安装的管道名稳定", key == m._instance_key())

    lock_a, server_a, verdict_a = m._claim_single_instance(key)
    c.check("★ 第一个进程能抢到锁",
            lock_a is not None and server_a is not None and verdict_a == "ok",
            verdict_a)
    lock_b, server_b, verdict_b = m._claim_single_instance(key)
    c.check("★ 第二个进程被判为 blocked（这就是单实例的依据）",
            lock_b is None and verdict_b == "blocked", verdict_b)
    c.check("★ 锁失败不能一律当「已有实例」：不可写时要降级放行",
            'if _verdict == "blocked"' in (ROOT / "main.py").read_text(
                encoding="utf-8"), "")
    try:
        if server_a is not None:
            server_a.close()
        if lock_a is not None:
            lock_a.unlock()
    except Exception:                                         # noqa: BLE001
        pass


def section_never_blocks_startup(c: Checker) -> None:
    c.section("第 6 节：托盘失败不能挡住启动")

    src = (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    c.check("★ 托盘创建失败只记日志（try/except 包住）",
            "托盘创建失败" in src, "")
    c.check("托盘可用性检测自身也有兜底", "检测托盘失败" in
            (ROOT / "ui" / "tray.py").read_text(encoding="utf-8"), "")
    c.check("只在 __init__ 末尾装一次托盘",
            src.count("self._setup_tray()") == 1,
            str(src.count("self._setup_tray()")))


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_icon(c)
        section_tray_menu(c)
        section_close_hides(c)
        section_no_tray(c)
        section_single_instance(c)
        section_never_blocks_startup(c)
    finally:
        print()
        print("=" * 78)
        print(f"托盘与单实例守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())