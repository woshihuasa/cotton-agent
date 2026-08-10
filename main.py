"""
棉花问答智能体 — 程序入口
启动 PyQt6 图形界面。

日志：运行日志与崩溃转储写入 %APPDATA%/CottonAgent/（打包模式）
      或项目根（开发模式），便于用户反馈问题。
"""
import faulthandler
import logging
import sys

from config import app_data_dir

# ── 日志与崩溃捕获（在任何业务逻辑之前初始化）──
_log_dir = app_data_dir()
faulthandler.enable(open(_log_dir / "faulthandler.log", "w"))
logging.basicConfig(
    filename=_log_dir / "cotton_app.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("main")


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
