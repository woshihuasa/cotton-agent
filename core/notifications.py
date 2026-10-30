# -*- coding: utf-8 -*-
"""通知中心 —— 客户端级的推送收件箱。

【为什么不是往会话里塞一条消息】

    推送不属于某个对话，它属于**这台客户端**。塞进 `session.conversation_store`
    会带来三个必然的后果：换一个会话就找不到了；L2 压缩把它当成普通对话压掉；
    空会话里推一条还会触发标题生成，于是摘要可能变成「每日农情简报」。
    所以它单独存在这里，界面也从这里读。

【存在哪】`app_data_dir()/notifications/index.json`

    原子写（临时文件 + `os.replace`），与 `core/orchestration/store.py`、
    `core/credentials.py` 同一条纪律。

【为什么正文要在 index 里留一份】

    一开始想的是「只存 `reports/*.md` 的路径，正文不复制」，省得两处不一致。
    但那样抽屉就依赖文件还在：用户清一次 `reports/`，收件箱就只剩一堆打不开的
    链接，而这正是「点进去发现什么都没有」的那种失望。
    所以正文进 index，`path` 只作为「打开存档」的入口 —— 两者分工是
    **收件箱自包含、存档是长期档案**，不是同一份数据的两个副本。

【同一天同一任务只留一条】

    `id = f"{job_id}-{YYYY-MM-DD}"`。同一天同一个任务再推一次是**替换**而不是
    新增，于是重启补发、失败重试都不会堆出两条一模一样的简报。
    替换时正文没变就保留已读状态，正文变了才重新算未读 —— 重试不该把用户
    已经看过的推送又标成新的。

【保留策略】最近 200 条、或 90 天内，写盘时顺手裁掉。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("Notifications")

#: 最多保留多少条
DEFAULT_KEEP = 200

#: 最多保留多少天
DEFAULT_KEEP_DAYS = 90

#: id 只允许这些字符 —— 它会出现在文件名与日志里。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+-\d{4}-\d{2}-\d{2}$")


def notifications_dir() -> Path:
    """通知目录（惰性取 `app_data_dir()`，避免 import 期触碰配置）。"""
    from config import app_data_dir
    d = app_data_dir() / "notifications"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON：临时文件 + `os.replace`。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp",
                                    prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class Notification:
    """一条推送。**不可变** —— 改已读状态时由 store 换一个新对象。"""

    id: str
    job_id: str
    title: str
    body: str
    created_at: str
    read_at: str = ""
    #: 长期档案的位置（可能为空，也可能文件已被删）—— 只用于「打开存档」
    path: str = ""

    @property
    def unread(self) -> bool:
        return not self.read_at

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id, "job_id": self.job_id, "title": self.title,
            "body": self.body, "created_at": self.created_at,
            "read_at": self.read_at, "path": self.path,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Notification | None":
        """容错解析。**读不懂就返回 None**，由调用方丢掉这一条。"""
        if not isinstance(raw, dict):
            return None
        nid = str(raw.get("id") or "").strip()
        body = str(raw.get("body") or "").strip()
        if not nid or not body:
            return None
        return cls(
            id=nid,
            job_id=str(raw.get("job_id") or "").strip(),
            title=str(raw.get("title") or "").strip(),
            body=body,
            created_at=str(raw.get("created_at") or "").strip(),
            read_at=str(raw.get("read_at") or "").strip(),
            path=str(raw.get("path") or "").strip(),
        )


def make_id(job_id: str, when: datetime) -> str:
    """`任务id-YYYY-MM-DD`。同一天同一任务只会有一条。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(job_id or "").strip()) or "job"
    return f"{safe}-{when:%Y-%m-%d}"


class NotificationStore:
    """收件箱。**全部同步、单线程**用；界面在同一个线程里读它即可。

    `index_path` 可注入，测试据此不需要碰用户真实数据。
    """

    def __init__(
        self,
        *,
        index_path: Path | None = None,
        keep: int = DEFAULT_KEEP,
        keep_days: int = DEFAULT_KEEP_DAYS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._index_path = Path(index_path) if index_path else None
        self.keep = max(1, int(keep))
        self.keep_days = max(1, int(keep_days))
        self._clock = clock
        #: 新的在前
        self._items: list[Notification] = []
        self._loaded = False

    # ---- 位置与时钟 ----

    @property
    def index_file(self) -> Path:
        return self._index_path or (notifications_dir() / "index.json")

    def now(self) -> datetime:
        return (self._clock() if self._clock else datetime.now()).replace(microsecond=0)

    # ---- 读 ----

    def load(self) -> None:
        """从磁盘读。**坏文件不抛**（读不出来就当空收件箱，留一条日志）。"""
        path = self.index_file
        items: list[Notification] = []
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("通知索引读不出来（%s），按空收件箱处理：%s", path, e)
                raw = []
            rows = raw.get("items") if isinstance(raw, dict) else raw
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                item = Notification.from_dict(row)
                if item is not None:
                    items.append(item)
        self._items = sorted(items, key=lambda n: n.created_at, reverse=True)
        self._loaded = True

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def items(self) -> tuple[Notification, ...]:
        """全部通知，新的在前。返回元组 —— 外部改不动内部状态。"""
        self._ensure()
        return tuple(self._items)

    def unread_count(self) -> int:
        return sum(1 for n in self.items() if n.unread)

    def get(self, nid: str) -> Notification | None:
        self._ensure()
        for n in self._items:
            if n.id == nid:
                return n
        return None

    # ---- 写 ----

    def add(self, *, job_id: str, title: str, body: str,
            path: str = "", when: datetime | None = None) -> Notification:
        """收一条推送。同一天同一任务是**替换**，不会堆两条。"""
        when = (when or self.now()).replace(microsecond=0)
        text = str(body or "").strip()
        if not text:
            raise ValueError("通知正文为空 —— 不往收件箱里塞空条目")
        nid = make_id(job_id, when)
        if not _ID_RE.match(nid):
            raise ValueError(f"通知 id 不合法：{nid!r}")
        self._ensure()

        item = Notification(
            id=nid, job_id=str(job_id or "").strip(),
            title=str(title or "").strip() or nid,
            body=text, created_at=when.isoformat(timespec="seconds"),
            read_at="", path=str(path or "").strip(),
        )
        old = self.get(nid)
        if old is not None:
            # ★ 正文没变就保留已读状态：失败重试不该把看过的推送重新标成未读。
            if old.body == item.body and old.read_at:
                item = Notification(**{**item.to_dict(), "read_at": old.read_at,
                                       "created_at": old.created_at})
            self._items = [n for n in self._items if n.id != nid]
        self._items.append(item)
        self._sort_and_prune(when)
        self.save()
        return item

    def mark_all_read(self, when: datetime | None = None) -> int:
        """全部标为已读。返回本次被改动的条数。"""
        when = (when or self.now()).replace(microsecond=0)
        self._ensure()
        stamp = when.isoformat(timespec="seconds")
        changed = 0
        updated: list[Notification] = []
        for n in self._items:
            if n.unread:
                updated.append(Notification(**{**n.to_dict(), "read_at": stamp}))
                changed += 1
            else:
                updated.append(n)
        if changed:
            self._items = updated
            self.save()
        return changed

    def mark_read(self, nid: str, when: datetime | None = None) -> bool:
        """单条标为已读（抽屉现在是「打开即全读」，但接口留着给以后用）。"""
        when = (when or self.now()).replace(microsecond=0)
        self._ensure()
        for i, n in enumerate(self._items):
            if n.id == nid:
                if n.unread:
                    self._items[i] = Notification(
                        **{**n.to_dict(),
                           "read_at": when.isoformat(timespec="seconds")})
                    self.save()
                return True
        return False

    def remove(self, nid: str) -> bool:
        self._ensure()
        before = len(self._items)
        self._items = [n for n in self._items if n.id != nid]
        if len(self._items) != before:
            self.save()
            return True
        return False

    def clear(self) -> int:
        """清空收件箱。返回删掉的条数。"""
        self._ensure()
        count = len(self._items)
        if count:
            self._items = []
            self.save()
        return count

    # ---- 落盘 ----

    def save(self) -> None:
        payload = {"version": 1, "items": [n.to_dict() for n in self._items]}
        _atomic_write_json(self.index_file, payload)

    def _sort_and_prune(self, now: datetime) -> None:
        self._items.sort(key=lambda n: n.created_at, reverse=True)
        self._items = self._pruned(self._items, now)

    def _pruned(self, items: list[Notification], now: datetime) -> list[Notification]:
        """按条数上限与天数上限裁剪。**解析不出时间的条目保留**（不静默丢数据）。"""
        kept = list(items)
        cutoff = now - timedelta(days=self.keep_days)
        out: list[Notification] = []
        for n in kept:
            try:
                created = datetime.fromisoformat(n.created_at)
            except ValueError:
                out.append(n)          # 时间读不懂 → 留着，不因为解析失败就删
                continue
            if created >= cutoff:
                out.append(n)
        if len(out) > self.keep:
            out = out[: self.keep]
        return out

    def prune(self, now: datetime | None = None) -> int:
        """手动裁一次。返回裁掉的条数。"""
        now = (now or self.now()).replace(microsecond=0)
        self._ensure()
        before = len(self._items)
        kept = self._pruned(self._items, now)
        if len(kept) != before:
            self._items = kept
            self.save()
        return before - len(kept)


__all__ = [
    "DEFAULT_KEEP", "DEFAULT_KEEP_DAYS", "Notification", "NotificationStore",
    "make_id", "notifications_dir",
]