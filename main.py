"""
棉花问答智能体 — 程序入口
启动 PyQt6 图形界面。

日志：运行日志与崩溃转储优先写入 %APPDATA%/CottonAgent/（打包模式）
      或项目根（开发模式），便于用户反馈问题。
      该目录不可写时自动退到系统临时目录；再不行则只写标准错误输出 ——
      **任何情况下都不因为写不了日志而阻止应用启动。**
"""
import faulthandler
import logging
import sys
import tempfile
from pathlib import Path

from config import app_data_dir


def _setup_logging() -> Path | None:
    """初始化日志与崩溃转储，返回实际使用的目录（均不可用则返回 None）。

    ⚠️ 为什么必须容错（v0.3.2 修复）：
    原实现是**两行无保护**的写操作，且执行时机在日志与异常钩子之前：

        faulthandler.enable(open(_log_dir / "faulthandler.log", "w"))
        logging.basicConfig(filename=_log_dir / "cotton_app.log", ..., encoding="utf-8")

    只要 `%APPDATA%/CottonAgent` 不可写（企业策略、文件夹重定向、杀软锁定、漫游
    配置异常，或外部沙箱限制），二者都会抛 OSError，应用随即**不留日志、不弹错误
    框、直接消失**——现场只剩一句没有上下文的启动错误。

    实测确认：`logging.basicConfig` 在传了 `encoding` 时**会**把 OSError 重新抛出，
    所以两行都是致命的，不只是 faulthandler 那一行。

    目录优先级：%APPDATA%（打包）/ 项目根（开发） → 系统临时目录 → 放弃文件日志。
    """
    candidates = [app_data_dir(), Path(tempfile.gettempdir()) / "CottonAgent"]
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
    import traceback

    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log.critical("未捕获异常:\n%s", tb_text)
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        QMessageBox.critical(
            None, "程序错误",
            f"发生未预期的错误:\n{exc_value}\n\n详细信息已写入日志文件。",
        )
    except Exception:
        pass


sys.excepthook = _excepthook

from PyQt6.QtWidgets import QApplication

from ui.main_window import CottonAgentWindow

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
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "启动失败", f"应用启动失败:\n{e}")
        sys.exit(1)
