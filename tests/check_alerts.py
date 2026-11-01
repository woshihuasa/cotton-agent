# -*- coding: utf-8 -*-
"""气象预警守卫 —— 解析、筛选、缓存、失败语义。

【为什么值得单独守】这条链路上的错误都会"看起来正常"：

  ① **把"没拉到"说成"没预警"** —— 灾害场景里最危险的一种谎。所以失败必须是
     `error`，不能是空列表。
  ② **等级解析错位** —— 「大风蓝色预警」解析成「大」+「风蓝」，等级就丢了，
     推送再好看也是错的。
  ③ **缓存吞掉刷新** —— 失败时用旧数据没问题，但**必须把错误带出去**。

【零网络】HTTP 全部用桩替掉。
【用法】python tests/check_alerts.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_alerts_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from core import alarms                                           # noqa: E402
from core import schedule as S                                    # noqa: E402
from core.orchestration import catalog, engine as E               # noqa: E402
from core.tools import TOOL_HANDLERS                              # noqa: E402
from core.tools import capabilities as caps                       # noqa: E402
from core.tools import alerts as tool_alerts                      # noqa: E402
from core.tools.schemas import TOOL_SCHEMAS                       # noqa: E402


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


def section_parse(c: Checker) -> None:
    c.section("第 1 节：标题解析（等级必须落对位置）")

    got = alarms.parse_title("广西壮族自治区桂林市全州县气象台发布大风蓝色预警信号")
    c.check("★ 标准标题 → 单位/信号/等级 三段都对",
            got == {"unit": "广西壮族自治区桂林市全州县气象台",
                    "signal": "大风", "level": "蓝色"}, str(got))

    got2 = alarms.parse_title("新疆维吾尔自治区阿克苏地区气象台发布沙尘暴黄色预警信号")
    c.check("★ 多字信号名（沙尘暴）不会被切碎",
            got2.get("signal") == "沙尘暴" and got2.get("level") == "黄色", str(got2))

    c.check("台风白色预警不在四级体系里 → 解析不出等级（不猜）",
            alarms.parse_title("广东省气象台发布台风白色预警信号") == {})
    c.check("缺「发布」二字 → 认不出，返回空字典",
            alarms.parse_title("阿克苏地区气象台大风蓝色预警信号") == {})
    c.check("空标题 → 空字典", alarms.parse_title("") == {})

    c.check("alertid 前 6 位取行政区划代码",
            alarms.area_code_of("45032441600000_20261101172712") == "450324")
    c.check("alertid 形状不认识 → 空串", alarms.area_code_of("xyz") == "")


def _mk(alertid: str, title: str, when: str = "2026/11/01 17:27") -> alarms.Alert:
    return alarms.Alert(alertid=alertid, title=title, issuetime=when,
                        area_code=alarms.area_code_of(alertid),
                        **alarms.parse_title(title))


def section_topic(c: Checker) -> None:
    c.section("第 2 节（续）：★ 信号 → 检索主题词")

    c.check("★ 大风 → 防风/化控/喷药 这类**动作词**",
            "化控" in alarms.topic_keywords("大风"), alarms.topic_keywords("大风"))
    c.check("沙尘暴也归到防风那一组",
            alarms.topic_keywords("沙尘暴") == alarms.topic_keywords("大风"))
    c.check("★ 高温 → 滴灌/降温/花粉",
            "滴灌" in alarms.topic_keywords("高温"), alarms.topic_keywords("高温"))
    c.check("★ 霜冻 → 防霜冻/出苗",
            "防霜冻" in alarms.topic_keywords("霜冻"), alarms.topic_keywords("霜冻"))
    c.check("★ 主题词里**不含数字**（知识库是散文，用数字检索会跑偏）",
            not any(ch.isdigit() for ch in alarms.topic_keywords("大风")))
    c.check("认不出的信号 → 通用主题词（不返回空）",
            alarms.topic_keywords("某个没见过的信号") == alarms.DEFAULT_TOPIC)


def section_match(c: Checker) -> None:
    c.section("第 2 节：按地区筛")

    data = [
        _mk("45032441600000_1", "广西壮族自治区桂林市全州县气象台发布大风蓝色预警信号"),
        _mk("65290141600000_1", "新疆维吾尔自治区阿克苏地区阿克苏市气象台发布高温黄色预警信号"),
        _mk("65292241600000_1", "新疆维吾尔自治区阿克苏地区温宿县气象台发布大风蓝色预警信号"),
    ]
    c.check("★ 按名字筛（阿克苏）能同时匹配市县两条",
            len(alarms.match(data, area_hint="阿克苏")) == 2)
    c.check("★ 按区划码筛（652901）只出阿克苏市那条",
            [a.area_code for a in alarms.match(data, area_code="652901")] == ["652901"])
    c.check("按 6529 前缀能筛出全地区",
            len(alarms.match(data, area_code="6529")) == 2)
    c.check("区划码优先于名字",
            len(alarms.match(data, area_hint="桂林", area_code="6529")) == 2)
    c.check("两个都不给 → 返回全部", len(alarms.match(data)) == 3)
    c.check("筛不到 → 空列表（不是全部）", alarms.match(data, area_hint="北京") == [])


_DETAIL_HTML = """
<html><head><style>.x{color:red}</style><script>var a=1;</script></head><body>
<div>中央气象台 导航 进入 恭城 天气预报</div>
<div class="content">恭城县气象台1日22时32分发布大风蓝色预警信号：预计未来24小时内我县将出现
6级（或阵风7级）以上大风，请做好防范。</div>
<div>防御指南： 1.政府及相关部门按照职责做好防大风工作； 2.关好门窗，加固围板。</div>
<div>推荐服务 妈祖（MAZU）早期预警</div>
</body></html>
"""


def section_detail(c: Checker) -> None:
    c.section("第 3 节（续）：★ 预警详情（未来数小时的定量描述在这里）")

    got = alarms.parse_detail(_DETAIL_HTML)
    c.check("★ 正文只保留预警那句（页面导航不混进来）",
            got["body"].startswith("恭城县气象台") and "天气预报" not in got["body"],
            got["body"][:60])
    c.check("★ 正文含定量的时效与量级",
            "未来24小时内" in got["body"] and "阵风7级" in got["body"], got["body"][:80])
    c.check("★ 时效从正文读出来（不是写死的 6 小时）",
            got["window"] == "24小时", str(got["window"]))
    c.check("防御指南单独提取且不留前导冒号",
            got["defense"].startswith("1.政府"), got["defense"][:20])
    c.check("脚本与样式被剥掉", "<" not in got["body"] and "color:red" not in got["body"])

    # 6 小时的信号也能读对（不同信号时效不同）
    html6 = ("某县气象台1日22时32分发布雷雨大风黄色预警信号：预计未来6小时内，"
             "平均风力可达8级，阵风9级并伴有雷电。防御指南：1.停止户外作业；")
    c.check("★ 6 小时时效同样从原文读出",
            alarms.parse_detail(html6)["window"] == "6小时",
            str(alarms.parse_detail(html6)["window"]))

    c.check("空页面 → 空结构（不抛）",
            alarms.parse_detail("") == {"body": "", "defense": "", "window": ""})

    # fetch_detail 失败要安静降级（不抛、不缓存坏值）
    real_get = alarms.requests.get

    def boom(*_a, **_k):
        raise alarms.requests.RequestException("超时")
    alarms.requests.get = boom
    try:
        a = _mk("65290141600000_1", "某气象台发布大风蓝色预警信号")
        c.check("★ 详情取不到 → 返回空字典（不抛异常）",
                alarms.fetch_detail(a) == {})
    finally:
        alarms.requests.get = real_get


def section_format(c: Checker) -> None:
    c.section("第 3 节：给模型看的文字（等级与单位来自原文）")

    a = _mk("65290141600000_1", "新疆维吾尔自治区阿克苏地区气象台发布大风蓝色预警信号")
    text = alarms.format_for_model([a])
    c.check("★ 带上信号+等级", "大风蓝色预警" in text, text)
    c.check("★ 带上发布单位与时间", "气象台" in text and "2026/11/01" in text, text)
    c.check("没有预警时明确说没有", "没有生效" in alarms.format_for_model([]))

    many = [_mk(f"65290141600{i:03d}_1", "某气象台发布大风蓝色预警信号") for i in range(12)]
    c.check("超过上限会说明还剩几条", "另有 4 条" in alarms.format_for_model(many, limit=8))


class _Resp:
    def __init__(self, payload) -> None:
        self._p = payload
    def raise_for_status(self) -> None:
        return None
    def json(self):
        return self._p


def _page(items: list, total: int) -> dict:
    return {"data": {"page": {"count": total, "list": items}}}


def section_fetch(c: Checker) -> None:
    c.section("第 4 节：拉取、缓存与失败语义")

    alarms.clear_cache()
    real_get = alarms.requests.get
    calls: list[int] = []

    items = [{"alertid": "65290141600000_1",
              "title": "新疆维吾尔自治区阿克苏地区气象台发布大风蓝色预警信号",
              "issuetime": "2026/11/01 17:00", "url": "/x"},
             {"alertid": "45032441600000_1",
              "title": "广西壮族自治区桂林市全州县气象台发布大风蓝色预警信号",
              "issuetime": "2026/11/01 17:10", "url": "/y"}]

    def fake_get(url, params=None, headers=None, timeout=None):
        page_no = int((params or {}).get("pageNo") or 1)
        calls.append(page_no)
        return _Resp(_page(items if page_no == 1 else [], 2))

    alarms.requests.get = fake_get
    try:
        got, err = alarms.fetch_alerts(now=1000.0)
        c.check("★ 首次拉取成功，无错误", err == "" and len(got) == 2, err)
        c.check("首页只请求一次（总数 2 条 → 只翻 1 页）", calls == [1], str(calls))

        calls.clear()
        got2, err2 = alarms.fetch_alerts(now=1000.0 + 60)
        c.check("★ 10 分钟内复用缓存（不再发请求）", calls == [] and len(got2) == 2,
                str(calls))

        # 失败：不覆盖旧数据，但**错误必须带出去**
        def boom(*_a, **_k):
            raise alarms.requests.RequestException("连接被重置")
        alarms.requests.get = boom
        got3, err3 = alarms.fetch_alerts(now=1000.0 + alarms.CACHE_TTL_S + 1, refresh=True)
        c.check("★ 拉取失败 → 返回错误说明（不是空列表冒充「无预警」）",
                bool(err3) and "不可达" in err3, err3)
        c.check("★ 失败时仍返回上一次的好数据（旧清单比空有用）",
                len(got3) == 2, str(len(got3)))
    finally:
        alarms.requests.get = real_get
        alarms.clear_cache()


def section_tool(c: Checker) -> None:
    c.section("第 5 节：工具封装（聊天与定时任务同源）")

    real = alarms.fetch_alerts
    try:
        # ① 失败 → error JSON（编排层据此判失败）
        alarms.fetch_alerts = lambda **k: ([], "预警源不可达：测试")
        out = json.loads(tool_alerts.get_weather_alerts({"region": "阿克苏"}))
        c.check("★ 取不到时返回 error，而不是「无预警」",
                bool(out.get("error")) and out.get("summary") == "", str(out)[:80])

        # ② 命中
        a = _mk("65290141600000_1",
                "新疆维吾尔自治区阿克苏地区气象台发布大风蓝色预警信号")
        alarms.fetch_alerts = lambda **k: ([a], "")
        out = json.loads(tool_alerts.get_weather_alerts({"region": "阿克苏"}))
        c.check("★ 命中 → count/items/summary 齐全",
                out["count"] == 1 and out["items"][0]["level"] == "蓝色"
                and "大风蓝色预警" in out["summary"], str(out)[:100])

        # ③ 未命中：这是**确认过**的无预警
        alarms.fetch_alerts = lambda **k: ([a], "")
        out = json.loads(tool_alerts.get_weather_alerts({"region": "喀什"}))
        c.check("★ 未命中 → count=0 且措辞是「没有生效」",
                out["count"] == 0 and "没有生效" in out["summary"], out["summary"])
    finally:
        alarms.fetch_alerts = real

    names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    c.check("★ 契约里有 get_weather_alerts", "get_weather_alerts" in names, str(names))
    c.check("★ 实现里有", "get_weather_alerts" in TOOL_HANDLERS)
    c.check("★ 能力注解里有",
            "get_weather_alerts" in caps.CAPABILITIES, str(sorted(caps.CAPABILITIES)))
    c.check("三处键集一致",
            set(names) == set(TOOL_HANDLERS) == set(caps.CAPABILITIES))
    cap = caps.CAPABILITIES.get("get_weather_alerts")
    c.check("声明为只读 + 联网", cap and cap.read_only and cap.open_world, str(cap))
    c.check("节点目录里有 tool.get_weather_alerts",
            catalog.get("tool.get_weather_alerts") is not None)
    c.check("派发到工具执行器",
            E.executor_for("tool.get_weather_alerts") is E._exec_tool)


def section_trigger(c: Checker) -> None:
    c.section("第 6 节：★ 预警的节奏（每天 06/12/18/00，时钟对齐）")

    jobs = {j.id: j for j in S.default_jobs()}
    job = jobs.get("weather_watch")
    c.check("出厂任务里有 weather_watch", job is not None)
    if job:
        trig = job.trigger
        c.check("★ 触发类型是 daily + 四个时刻（不是 interval 漂移）",
                trig.kind == "daily" and len(trig.times) == 4, str(trig))
        c.check("★ 时刻正是 06/12/18/00",
                sorted(trig.clock_times()) == sorted(
                    [S.parse_clock(x) for x in ("06:00", "12:00", "18:00", "00:00")]),
                str(trig.clock_times()))
        c.check("配置合法", job.ok, job.validate())

        # 06:05 应当正好落在 06:00 那一格
        from datetime import datetime as dt
        due = S.due_at(trig, None, dt(2026, 11, 2, 6, 5))
        c.check("★ 06:05 → 计划时刻是 06:00（不是「现在」）",
                due == dt(2026, 11, 2, 6, 0), str(due))
        c.check("★ 已经跑过 06:00 之后，今天不再触发",
                S.due_at(trig, dt(2026, 11, 2, 6, 0), dt(2026, 11, 2, 7, 0)) is None)
        c.check("★ 05:59 该补发当天 00:00 那一格（上次是前一天 18:00）",
                S.due_at(trig, dt(2026, 11, 1, 18, 0), dt(2026, 11, 2, 5, 59))
                == dt(2026, 11, 2, 0, 0),
                str(S.due_at(trig, dt(2026, 11, 1, 18, 0), dt(2026, 11, 2, 5, 59))))
        c.check("★ 00:00 跑过之后，05:59 不再触发",
                S.due_at(trig, dt(2026, 11, 2, 0, 0), dt(2026, 11, 2, 5, 59)) is None)
        nxt = S.next_run(trig, dt(2026, 11, 2, 6, 0), dt(2026, 11, 2, 7, 0))
        c.check("★ 下次是 12:00", nxt == dt(2026, 11, 2, 12, 0), str(nxt))
        c.check("跨天：18:00 之后下一次是次日 00:00",
                S.next_run(trig, dt(2026, 11, 2, 18, 0), dt(2026, 11, 2, 19, 0))
                == dt(2026, 11, 3, 0, 0),
                str(S.next_run(trig, dt(2026, 11, 2, 18, 0), dt(2026, 11, 2, 19, 0))))

    c.check("times 里格式不对 → 配置不合法（不静默忽略）",
            not S.Trigger(kind="daily", times=("06:00", "25:00")).ok)
    c.check("times 为空时仍按单个 at 走（向后兼容）",
            S.Trigger(kind="daily", at="08:00").ok
            and S.Trigger(kind="daily", at="08:00").clock_times()[0]
            == S.parse_clock("08:00"))


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_parse(c)
        section_topic(c)
        section_match(c)
        section_detail(c)
        section_format(c)
        section_fetch(c)
        section_tool(c)
        section_trigger(c)
    finally:
        print()
        print("=" * 78)
        print(f"气象预警守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())