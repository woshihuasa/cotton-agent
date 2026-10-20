"""
棉花问答智能体 — 程序入口
启动 PyQt6 图形界面。

日志：写入运行数据目录（%APPDATA%/CottonAgent；该处不可写时降级到 exe 同级
      runtime_data/），便于用户反馈问题。目录不可写时自动退到临时目录；再不行
      则只写标准错误输出 —— **任何情况下都不因为写不了日志而阻止应用启动。**

⚠️ 顺序约束（v0.3.3 修复）：
      1. `bootstrap` 必须最先导入 —— 它把「可写数据目录」与「临时目录」定下来。
         chromadb 的 Rust 后端在**没有可用临时目录**时，新建向量库必然失败并报
         `code: 14 unable to open database file`（实测：%TEMP% 被安全软件的
         文件系统过滤驱动拒绝写入，tempfile.gettempdir() 于是退化到当前工作目录）。
      2. 日志初始化与 excepthook **必须**在 `from config import ...` **之前**装好。
         因为 config.py 在模块顶层就调用 app_data_dir()；若 excepthook 尚未就位，
         应用将静默消失、不留任何信息 ——这正是 v0.3.2 在多台机器"打不开且
         零反馈"的根因。
"""
import faulthandler
import logging
import sys
import tempfile
import traceback
from pathlib import Path

import bootstrap


def _setup_logging() -> Path | None:
    """初始化日志与崩溃转储，返回实际使用的目录（均不可用则返回 None）。

    目录选择复用 `bootstrap.app_data_dir()` —— 与 config 用的是**同一套**候选与
    写探测。升级前这里直连 %APPDATA%、兜底用 `tempfile.gettempdir()`，而后者在
    %TEMP% 不可写时会退化到**当前工作目录**：双击运行时那就是 exe 所在目录，于是
    日志被写进 `dist/CottonAgent/CottonAgent/` 这种谁都找不到的地方（v0.3.3 实测）。
    两处各自独立判断数据目录，本就是同一个 bug 的两副面孔。

    为什么不能调用 config.app_data_dir()：config 尚未导入，而且本函数的目的正是
    在 config 导入失败前就把兜底装上 —— 所以走 bootstrap 这份无依赖实现。
    """
    primary = bootstrap.app_data_dir()
    # 先把临时目录修好，后面的 tempfile.gettempdir() 才不会退到当前工作目录
    bootstrap.ensure_writable_temp(primary)
    candidates = [primary, Path(tempfile.gettempdir()) / "CottonAgent"]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue

        try:
            faulthandler.enable(open(d / "faulthandler.log", "w"))
        except OSError:
            pass                                  # 没有崩溃转储不致命

        try:
            logging.basicConfig(
                filename=d / "cotton_app.log",
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                encoding="utf-8",
            )
            return d
        except OSError:
            continue

    logging.basicConfig(level=logging.INFO)       # 兜底：只写标准错误输出
    return None


_log_dir = _setup_logging()
log = logging.getLogger("main")
if _log_dir is None:
    log.warning("日志目录不可写，已退化为只写标准错误输出（不影响使用）。")


def _excepthook(exc_type, exc_value, exc_tb):
    """未捕获异常：写日志并弹窗提示，避免静默退出。"""
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log.critical("未捕获异常:\n%s", tb_text)
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        QMessageBox.critical(
            None, "程序错误",
            f"发生未预期的错误:\n{exc_value}\n\n详细信息已写入日志文件:\n{_log_dir}",
        )
    except Exception:
        # PyQt 都起不来时，至少写一份崩溃文件到 exe 同级（通常可写）
        try:
            import os
            crash = Path(sys.executable).resolve().parent / "startup_error.log"
            crash.write_text(tb_text, encoding="utf-8")
        except Exception:
            pass


sys.excepthook = _excepthook

# ⚠️ excepthook 与日志已就位后，才导入 config（其模块顶层可能抛 PermissionError）
from config import app_data_dir  # noqa: E402

from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import CottonAgentWindow  # noqa: E402

if __name__ == "__main__":
    log.info("应用启动...")
    app = QApplication(sys.argv)
    try:
        window = CottonAgentWindow()
        window.show()
        log.info("主窗口显示完成。")
        sys.exit(app.exec())
    except Exception as e:
        log.exception("启动失败: %s", e)
        tb_text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        try:
            import os
            crash = Path(sys.executable).resolve().parent / "startup_error.log"
            crash.write_text(tb_text, encoding="utf-8")
        except Exception:
            pass
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "启动失败", f"应用启动失败:\n{e}")
        sys.exit(1)
