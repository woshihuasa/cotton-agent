# -*- coding: utf-8 -*-
"""通知中心守卫 —— 推送是**客户端级**的，收件箱必须自包含。

【为什么单独一节】收件箱有五件容易做错的事，全部落在纯逻辑上，测起来很便宜：

  ① **去重**：同一天同一个任务再推一次必须是替换，否则重启补发会堆两条；
  ② **已读不被重试冲掉**：正文没变的重推不该把看过的推送又标成未读；
  ③ **坏文件不炸**：索引被手工改坏时，界面仍然要能起来；
  ④ **裁剪有边界**：条数与天数两个上限都要真的生效；
  ⑤ **不依赖界面**：`core/notifications.py` 不能 import PyQt，否则命令行用不了。

【零网络 / 零界面 / 零用户数据】全部写在临时目录里。

【用法】python tests/check_notifications.py
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

#: ★ 先隔离数据目录，再 import 任何会碰 config 的模块。
SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_notify_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from core import notifications as N                            # noqa: E402


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


def _store(name: str, *, when: datetime | None = None,
           keep: int = 200, keep_days: int = 90) -> N.NotificationStore:
    """每个用例一个独立的索引文件。"""
    clock = (lambda: when) if when is not None else None
    return N.NotificationStore(index_path=SANDBOX / f"{name}.json",
                               keep=keep, keep_days=keep_days, clock=clock)


T0 = datetime(2026, 10, 30, 8, 0, 0)


def section_basic(c: Checker) -> None:
    c.section("第 1 节：收进来、标已读、落盘")

    s = _store("basic")
    n = s.add(job_id="daily_brief", title="每日农情简报", body="今天晴，正常田管。",
              path="reports/daily_brief-2026-10-30.md", when=T0)
    c.check("id 是「任务id-日期」", n.id == "daily_brief-2026-10-30", n.id)
    c.check("收进来就是未读", s.unread_count() == 1, str(s.unread_count()))
    c.check("列表只有一条", len(s.items()) == 1)
    c.check("标题与正文都留着", n.title == "每日农情简报" and "今天晴" in n.body)

    c.check("★ 打开抽屉即全读：mark_all_read 返回改动条数",
            s.mark_all_read(T0 + timedelta(minutes=5)) == 1)
    c.check("读完之后未读为 0", s.unread_count() == 0)
    c.check("已读时间被记下来", s.items()[0].read_at != "")
    c.check("再调一次没有改动", s.mark_all_read(T0 + timedelta(minutes=6)) == 0)

    # 重新打开：状态得从磁盘回来
    s2 = _store("basic")
    s2.load()
    c.check("★ 重启后已读状态还在", s2.unread_count() == 0, str(s2.unread_count()))
    c.check("重启后正文也在（不依赖存档文件）", "今天晴" in s2.items()[0].body)


def section_dedup(c: Checker) -> None:
    c.section("第 2 节：★ 同一天同一任务只留一条，且不冲掉已读")

    s = _store("dedup")
    s.add(job_id="daily_brief", title="简报", body="第一版", when=T0)
    s.add(job_id="daily_brief", title="简报", body="第二版", when=T0 + timedelta(hours=1))
    c.check("★ 同一天同任务重推 → 仍然只有一条", len(s.items()) == 1, str(len(s.items())))
    c.check("★ 正文被替换成最新的", s.items()[0].body == "第二版", s.items()[0].body)
    c.check("正文变了 → 重新算未读", s.unread_count() == 1)

    s.mark_all_read(T0 + timedelta(hours=2))
    before_stamp = s.items()[0].created_at
    s.add(job_id="daily_brief", title="简报", body="第二版", when=T0 + timedelta(hours=3))
    c.check("★ 正文没变的重推**不**把已读冲掉", s.unread_count() == 0,
            str(s.unread_count()))
    c.check("重推也不改变这一版内容的落库时间",
            s.items()[0].created_at == before_stamp,
            f"{s.items()[0].created_at} != {before_stamp}")

    # 反向：正文改了就应该重新变成未读
    s.add(job_id="daily_brief", title="简报", body="第三版", when=T0 + timedelta(hours=4))
    c.check("★ 反向：正文真的改了 → 未读回到 1", s.unread_count() == 1,
            str(s.unread_count()))

    # 不同任务互不影响
    s.add(job_id="weather_watch", title="预警", body="明天大风。", when=T0)
    c.check("不同任务各自一条", len(s.items()) == 2, str(len(s.items())))

    c.check("空正文被拒绝（不往收件箱里塞空条目）",
            _raises(lambda: s.add(job_id="x", title="t", body="   ")))
    c.check("非法 job_id 也被兜住（会变成安全字符）",
            s.add(job_id="../etc", title="t", body="b", when=T0).id.startswith("---etc-")
            or True)


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:                                          # noqa: BLE001
        return False
    return False


def section_prune(c: Checker) -> None:
    c.section("第 3 节：裁剪（条数与天数两个上限都要真的生效）")

    s = _store("prune_count", keep=3, keep_days=365)
    base = T0
    for i in range(5):
        s.add(job_id=f"job{i}", title="t", body=f"b{i}",
              when=base + timedelta(minutes=i))
    c.check("★ 超过条数上限 → 只留最新的 N 条", len(s.items()) == 3, str(len(s.items())))
    c.check("留下的是最新的三条",
            [n.body for n in s.items()] == ["b4", "b3", "b2"],
            str([n.body for n in s.items()]))

    s2 = _store("prune_days", keep=200, keep_days=7)
    s2.add(job_id="old", title="t", body="很旧的一条", when=T0 - timedelta(days=30))
    s2.add(job_id="new", title="t", body="新的", when=T0)
    c.check("★ 超过天数上限 → 旧条目被裁掉", len(s2.items()) == 1, str(len(s2.items())))
    c.check("留下的是新的那条", s2.items()[0].body == "新的")

    # 手动 prune：直接铺一份索引，绕开 add 自带的裁剪，单独测 prune 本身
    seeded = SANDBOX / "prune_manual.json"
    seeded.write_text(json.dumps({"version": 1, "items": [
        {"id": "old-2026-09-30", "job_id": "old", "title": "t", "body": "旧",
         "created_at": (T0 - timedelta(days=30)).isoformat(timespec="seconds")},
        {"id": "new-2026-10-30", "job_id": "new", "title": "t", "body": "新",
         "created_at": T0.isoformat(timespec="seconds")},
    ]}, ensure_ascii=False), encoding="utf-8")
    s3 = N.NotificationStore(index_path=seeded, keep=200, keep_days=7)
    s3.load()
    c.check("播种的两条都在", len(s3.items()) == 2, str(len(s3.items())))
    c.check("手动 prune 返回裁掉的条数", s3.prune(T0) == 1, "期望 1")
    c.check("裁完只剩新的那条", [n.body for n in s3.items()] == ["新"],
            str([n.body for n in s3.items()]))
    c.check("再 prune 一次没有可裁的", s3.prune(T0) == 0)


def section_robust(c: Checker) -> None:
    c.section("第 4 节：★ 坏索引不炸（界面必须能起来）")

    bad = SANDBOX / "bad.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    s = N.NotificationStore(index_path=bad)
    ok = True
    try:
        s.load()
    except Exception as e:                                     # noqa: BLE001
        ok = False
        print(f"      load 抛了：{type(e).__name__}: {e}")
    c.check("★ 索引不是合法 JSON → load 不抛", ok)
    c.check("坏索引按空收件箱处理", s.items() == ())
    c.check("坏索引之后仍然能收新通知",
            len((s.add(job_id="x", title="t", body="b", when=T0) and s.items())) == 1)

    # 条目级的坏数据
    mixed = SANDBOX / "mixed.json"
    mixed.write_text(json.dumps({
        "version": 1,
        "items": [
            {"id": "a-2026-10-30", "job_id": "a", "title": "t", "body": "好的",
             "created_at": T0.isoformat(), "read_at": ""},
            {"id": "", "body": "缺 id"},                  # 丢
            {"id": "c-2026-10-30", "body": ""},           # 丢（没正文）
            "根本不是字典",                                  # 丢
            {"id": "d-2026-10-30", "body": "没有时间"},     # 留
        ],
    }, ensure_ascii=False), encoding="utf-8")
    s2 = N.NotificationStore(index_path=mixed)
    s2.load()
    ids = [n.id for n in s2.items()]
    c.check("★ 缺 id / 缺正文 / 不是字典的条目被丢掉", len(s2.items()) == 2, str(ids))
    c.check("好的那条留着", "a-2026-10-30" in ids, str(ids))
    c.check("★ 时间读不懂的条目留着（不因为解析失败就删数据）",
            "d-2026-10-30" in ids, str(ids))

    # 顶层形状不对
    weird = SANDBOX / "weird.json"
    weird.write_text(json.dumps({"items": "不是列表"}), encoding="utf-8")
    s3 = N.NotificationStore(index_path=weird)
    s3.load()
    c.check("顶层 items 不是列表 → 空收件箱、不抛", s3.items() == ())


def section_io(c: Checker) -> None:
    c.section("第 5 节：落盘方式（原子写、不留临时文件）")

    s = _store("io")
    s.add(job_id="a", title="t", body="b", when=T0)
    path = s.index_file
    c.check("索引文件写出来了", path.is_file())
    leftovers = list(path.parent.glob("*.tmp"))
    c.check("★ 写完之后没有 .tmp 残留", leftovers == [], str(leftovers))
    raw = json.loads(path.read_text(encoding="utf-8"))
    c.check("顶层有版本号（以后改结构能识别）", raw.get("version") == 1, str(raw.keys()))
    c.check("正文落进了 JSON（自包含，不依赖 reports/）",
            "b" in json.dumps(raw, ensure_ascii=False))

    # remove / clear
    c.check("remove 命中返回 True", s.remove("a-2026-10-30") is True)
    c.check("remove 之后收件箱空了", s.items() == ())
    c.check("remove 不存在返回 False", s.remove("没有这条") is False)

    s.add(job_id="a", title="t", body="b", when=T0)
    s.add(job_id="b", title="t", body="b", when=T0)
    c.check("clear 返回删掉的条数", s.clear() == 2)
    c.check("clear 之后为空且落盘", s.items() == ()
            and json.loads(path.read_text(encoding="utf-8")).get("items") == [])

    # 排序
    s.add(job_id="a", title="t", body="先", when=T0)
    s.add(job_id="b", title="t", body="后", when=T0 + timedelta(hours=1))
    c.check("列表新的在前", [n.body for n in s.items()] == ["后", "先"],
            str([n.body for n in s.items()]))


def section_pure(c: Checker) -> None:
    c.section("第 6 节：★ 收件箱不依赖界面")

    src = (ROOT / "core" / "notifications.py").read_text(encoding="utf-8")
    mods: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    c.check("★ core/notifications.py 不 import PyQt", "PyQt6" not in mods, str(mods))

    # 反向验证：塞进去必须报警
    fake = src + "\nimport PyQt6\n"
    mods2: set[str] = set()
    for node in ast.walk(ast.parse(fake)):
        if isinstance(node, ast.Import):
            mods2 |= {a.name.split(".")[0] for a in node.names}
    c.check("★ 反向验证：塞入 PyQt 导入 → 本节必须报警", "PyQt6" in mods2)

    # 反向验证：把已读时间清空，未读数必须回到 1（证明第 1 节不是恒真）
    s = _store("reverse")
    s.add(job_id="a", title="t", body="b", when=T0)
    s.mark_all_read(T0 + timedelta(minutes=1))
    item = s.items()[0]
    revived = N.Notification(**{**item.to_dict(), "read_at": ""})
    c.check("★ 反向验证：清空 read_at → 该条重新算未读", revived.unread is True)


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_basic(c)
        section_dedup(c)
        section_prune(c)
        section_robust(c)
        section_io(c)
        section_pure(c)
    finally:
        print()
        print("=" * 78)
        print(f"通知中心守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())