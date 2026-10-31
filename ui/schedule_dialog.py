# -*- coding: utf-8 -*-
"""推送设置 —— 触发层的控制面。

【为什么需要它】在那之前，改城市、关掉某个任务、立刻跑一次，都只能手工编辑
`schedule.json` 再重启。三个接口（`status` / `set_enabled` / `run_now`）早就
写好了却**零调用点** —— 这种"半个功能"比没有更糟，因为它让人以为已经能配了。

【改动怎么生效】三项都即时生效，不需要重启：
    · 开关 → `Scheduler.set_enabled()`（写回 `schedule.json`）
    · 城市/地区 → `Scheduler.set_inputs()`（写回，下一次触发就用新值）
    · 立即运行 → `Scheduler.run_now()`（不影响定时状态）
  正在跑的那一轮不会被改，改动从下一次判定开始算。

【它不管什么】不编辑时刻、不改阈值、不动静默期 —— 那些属于「流程定义」，
写错了会让推送变哑，留给人改 JSON 更稳。这一版只收敛最常用的三件事。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

DIALOG_STYLE = """
QDialog { background-color: #F7F7F8; }
QLabel#schedHint { font-size: 11px; color: #8A8A8E; }
QLabel#schedSection { font-size: 12px; font-weight: 600; color: #1f1f1f; }
QFrame#schedRow {
    background-color: #FFFFFF;
    border: 1px solid #E8E8EC;
    border-radius: 8px;
}
QLabel#schedTitle { font-size: 13px; font-weight: 600; color: #1f1f1f; }
QLabel#schedMeta  { font-size: 11px; color: #8A8A8E; }
QLabel#schedState { font-size: 11px; color: #4A4A4E; }
QLineEdit#schedEdit {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E4;
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}
QPushButton#schedRun {
    background-color: #EAF3FF;
    border: 1px solid #B6D6F5;
    border-radius: 6px;
    color: #1A73E8;
    font-size: 12px;
    padding: 4px 12px;
}
QPushButton#schedRun:hover { background-color: #DCEBFF; }
QPushButton#schedRun:disabled { color: #AAAAAE; border-color: #E4E4E8; }
QPushButton#schedClose {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E4;
    border-radius: 6px;
    font-size: 12px;
    padding: 5px 16px;
}
QPushButton#schedClose:hover { background-color: #F0F0F2; }
"""


class ScheduleDialog(QDialog):
    """触发层的设置窗口。**改动即时生效**，没有"保存"按钮。"""

    def __init__(self, scheduler, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sched = scheduler
        self.setWindowTitle("推送设置")
        self.resize(520, 380)
        self.setStyleSheet(DIALOG_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        section = QLabel("地区")
        section.setObjectName("schedSection")
        root.addWidget(section)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        self._city = QLineEdit()
        self._city.setObjectName("schedEdit")
        self._region = QLineEdit()
        self._region.setObjectName("schedEdit")
        form.addRow("城市", self._city)
        form.addRow("地区", self._region)
        root.addLayout(form)

        hint = QLabel("城市用高德口径（如 阿克苏）；地区用于检索农事要点（如 阿克苏地区）。"
                      "改完立即生效，从下一次触发开始算。")
        hint.setObjectName("schedHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        section2 = QLabel("任务")
        section2.setObjectName("schedSection")
        root.addWidget(section2)

        self._rows_host = QWidget()
        self._rows = QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(8)
        self._rows.addStretch(1)
        root.addWidget(self._rows_host, 1)

        self._status = QLabel("")
        self._status.setObjectName("schedHint")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("schedClose")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        root.addLayout(bottom)

        self._row_widgets: list[tuple] = []
        self._load_from_scheduler()

    # ---- 读 ----

    def _load_from_scheduler(self) -> None:
        """把调度器的当前状态填进界面（打开时一次，之后按动作刷新）。"""
        if self._sched is None:
            self._status.setText("定时触发层还没启动，暂时不能配置。")
            self._city.setEnabled(False)
            self._region.setEnabled(False)
            return
        jobs = list(getattr(self._sched, "jobs", []) or [])
        first = jobs[0].inputs if jobs else {}
        self._city.setText(str(first.get("city") or ""))
        self._region.setText(str(first.get("region") or ""))
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        while self._rows.count() > 1:                 # 保留末尾的弹性
            item = self._rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._row_widgets.clear()

        for row in self._sched.status():
            frame = self._make_row(row)
            self._rows.insertWidget(self._rows.count() - 1, frame)

    def _make_row(self, row: dict) -> QFrame:
        frame = QFrame()
        frame.setObjectName("schedRow")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        box = QCheckBox(str(row.get("title") or row.get("id")))
        box.setObjectName("schedTitle")
        box.setChecked(bool(row.get("enabled")))
        box.setCursor(Qt.CursorShape.PointingHandCursor)
        box.toggled.connect(
            lambda on, jid=row.get("id"): self._on_toggle(jid, on))
        head.addWidget(box, 1)

        nxt = str(row.get("next_run") or "").replace("T", " ")
        when = QLabel(f"下次 {nxt[:16]}" if nxt else "下次 ——")
        when.setObjectName("schedMeta")
        head.addWidget(when, 0)
        lay.addLayout(head)

        line = QHBoxLayout()
        line.setSpacing(8)
        note = str(row.get("last_note") or "").strip()
        last = str(row.get("last_fired") or "").replace("T", " ")
        state = str(row.get("last_status") or "从未运行")
        text = f"{state}　{last[:16]}" + (f"　{note}" if note else "")
        label = QLabel(text)
        label.setObjectName("schedState")
        label.setWordWrap(True)
        line.addWidget(label, 1)

        run = QPushButton("立即运行")
        run.setObjectName("schedRun")
        run.setCursor(Qt.CursorShape.PointingHandCursor)
        run.setEnabled(bool(row.get("valid")))
        run.clicked.connect(lambda _=False, jid=row.get("id"): self._on_run(jid))
        line.addWidget(run, 0)
        lay.addLayout(line)
        return frame

    # ---- 写 ----

    def _on_toggle(self, job_id: str, enabled: bool) -> None:
        if self._sched is None:
            return
        try:
            ok = self._sched.set_enabled(str(job_id), bool(enabled))
        except Exception as e:                                # noqa: BLE001
            self._status.setText(f"写入失败：{type(e).__name__}: {e}")
            return
        self._status.setText("已启用。" if enabled and ok else
                             ("已停用。" if ok else "没有找到这个任务。"))
        self._rebuild_rows()

    def _on_run(self, job_id: str) -> None:
        if self._sched is None:
            return
        try:
            run_id = self._sched.run_now(str(job_id))
        except Exception as e:                                # noqa: BLE001
            self._status.setText(f"启动失败：{type(e).__name__}: {e}")
            return
        if run_id:
            self._status.setText(f"已提交，run_id = {run_id}（结果稍后推到抽屉里）")
        else:
            self._status.setText("没能启动 —— 看状态栏或日志，那里会写卡在哪一步。")
        self._rebuild_rows()

    # ---- 地区输入：失焦即保存 ----

    def _apply_inputs(self) -> None:
        if self._sched is None:
            return
        city = self._city.text().strip()
        region = self._region.text().strip()
        try:
            count = self._sched.set_inputs(city=city, region=region)
        except Exception as e:                                # noqa: BLE001
            self._status.setText(f"写入失败：{type(e).__name__}: {e}")
            return
        self._status.setText(f"已更新 {count} 个任务的城市/地区。")
        self._rebuild_rows()

    def accept(self) -> None:
        """关闭前把地区输入落盘 —— 不必逼用户先点一下别处。"""
        self._apply_inputs()
        super().accept()