# -*- coding: utf-8 -*-
"""定时触发层守卫 —— **一个计划时刻只触发一次**。

【为什么需要这一节】主动推送的失效方式与问答不同：问答答错了用户当场看得见，
推送错了（重复发、半夜发、天天发同一条）用户会**直接关掉这个功能**。
而这类错误全部落在「时刻判定」与「状态记录」上，全都是纯函数，测起来很便宜。

本节锁四件事：

  ① **时刻判定**：到点才触发、同一天不重复、跨天正常。
  ② **不补发**：错过补发窗口就只记「已跳过」，且**不会每 30 秒重试一次**。
  ③ **重启不重复**：状态落盘，进程重启后同一时刻不会被处理第二遍。
  ④ **判定不出来就不触发**：天气接口坏、字段缺、规则写错 —— 一律没有信号。

外加两条契约检查：两个工作流必须静态校验通过、节点类型必须在
`catalog.EXECUTABLE_TYPES` 与 `engine._EXECUTORS` **两处同时**登记。

【零 LLM / 零网络 / 零真实数据】天气用假实现，工作流用假执行器，状态写临时目录
（`COTTON_AGENT_DATA_DIR` 隔离，绝不碰用户真实数据）。

【用法】python tests/check_schedule.py
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

#: ★ 先隔离数据目录，再 import 任何会碰 config 的模块。
#:   否则本节会把调度状态写进用户真实的运行目录（= 影响下一次真实推送）。
SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_sched_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from core import schedule as S                                  # noqa: E402
from core.orchestration import catalog, engine as orch_engine   # noqa: E402
from core.orchestration import loader, validator                # noqa: E402

WEATHER_TODAY = (
    "城市: 阿克苏\n"
    "天气: 晴\n"
    "温度: 18°C\n"
    "风向: 西北风 风力: 3级\n"
    "湿度: 42%\n"
    "发布时间: 2026-10-30 08:00:00"
)

WEATHER_TOMORROW = (
    "城市: 阿克苏\n"
    "日期: 2026-10-31\n"
    "白天天气: 晴\n"
    "夜间天气: 多云\n"
    "白天温度: 36°C\n"
    "夜间温度: 4°C\n"
    "白天风力: 7级\n"
    "夜间风力: 3级"
)


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


# ---------------------------------------------------------------------------
# 假执行器：不需要线程、网络、真实工作流
# ---------------------------------------------------------------------------


class _Step:
    def __init__(self, output=None, error: str = "") -> None:
        self.output = output
        self.error = error


class _Run:
    def __init__(self, outputs: dict, *, state=None) -> None:
        self.steps = {k: _Step(v) for k, v in outputs.items()}
        self.state = state


class _FailedRun:
    """跑到一半失败的那种 Run：`state` 不是 DONE，某个节点带 error。"""

    def __init__(self, error: str) -> None:
        self.state = orch_engine.StepState.FAILED
        self.steps = {
            "weather": _Step({"text": "晴"}),
            "brief": _Step(None, error),
        }


class _Handle:
    def __init__(self, run_id: str, launched: bool = True, issues=None) -> None:
        self.run_id = run_id
        self.launched = launched
        self.issues = list(issues or [])


class _Runner:
    """记录被提交了什么，`wait()` 立刻返回一份固定产出。"""

    def __init__(self, outputs: dict | None = None, *, launched: bool = True) -> None:
        self.submitted: list[tuple[str, str, dict]] = []
        self._outputs = outputs
        self._launched = launched

    def submit_workflow(self, wf, inputs=None):
        rid = f"stub-{len(self.submitted) + 1}"
        self.submitted.append((rid, getattr(wf, "id", ""), dict(inputs or {})))
        return _Handle(rid, launched=self._launched)

    def wait(self, run_id, timeout=None):
        return None if self._outputs is None else _Run(self._outputs)


class _FailedRunner(_Runner):
    """`wait()` 总是返回一个失败的 Run（模拟工作流跑失败）。"""

    def __init__(self, run) -> None:
        super().__init__(None)
        self._run = run

    def wait(self, run_id, timeout=None):
        return self._run


class _FlakyRunner(_Runner):
    """前 N 次提交抛异常，之后恢复正常。用来验证「失败不消费计划时刻」。"""

    def __init__(self, outputs: dict | None = None, *, fail_times: int = 1) -> None:
        super().__init__(outputs)
        self.fail_left = fail_times

    def submit_workflow(self, wf, inputs=None):
        if self.fail_left > 0:
            self.fail_left -= 1
            raise RuntimeError("执行器炸了")
        return super().submit_workflow(wf, inputs)


def _isolate() -> None:
    """换一个新的数据目录，避免上一组的 `state.json` 影响下一组判定。"""
    os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX / f"{time.time_ns()}")


def _daily_job() -> S.Job:
    """测试用的**每日**任务：08:00 一次。

    ★ 不从 `default_jobs()[0]` 直接拿节奏：出厂配置可能为了验收改成每小时一次
      （`COTTON_BRIEF_EVERY_MIN` 那类临时口径），而「跨天 / 错过窗口 / 补发」
      这些语义只有在**每日**节奏下才成立。测试自己钉住节奏，才不会因为产品
      调了默认值而跟着漂。
    """
    return replace(S.default_jobs()[0],
                   trigger=S.Trigger(kind="daily", at="08:00"))


def _drain(sched: S.Scheduler, timeout: float = 3.0) -> None:
    """等后台的 `_watch` 线程收工。

    ★ 必须等：`schedule_dir()` 是在调用时读环境变量的，下一组 `_isolate()`
      之后，上一组还没写完的状态会落到**新目录**里，把新用例的状态污染掉。
    """
    _wait_for(lambda: not sched._inflight, timeout)


def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _fresh(job: S.Job | None = None, *, outputs=None, fetcher=None,
           launched: bool = True) -> tuple[S.Scheduler, _Runner, list]:
    """造一个隔离的调度器：状态目录在沙箱里，天气用假实现。"""
    # 每轮换一个状态文件，避免前面的用例影响后面的判定
    _isolate()
    events: list[dict] = []
    runner = _Runner(outputs, launched=launched)
    sched = S.Scheduler(
        runner=runner,
        kb_provider=lambda: {"name": "stub-kb"},
        fetcher=fetcher or (lambda city, when: WEATHER_TOMORROW),
        on_event=events.append,
    )
    sched.load()
    sched.jobs = [job] if job is not None else S.default_jobs()
    return sched, runner, events


# ---------------------------------------------------------------------------
# 各节
# ---------------------------------------------------------------------------


def section_time(c: Checker) -> None:
    c.section("第 1 节：时刻判定（到点才跑、同一天不重复、跨天正常）")
    daily = S.Trigger(kind="daily", at="08:00")
    t_0759 = datetime(2026, 10, 30, 7, 59)
    t_0800 = datetime(2026, 10, 30, 8, 0)
    t_0930 = datetime(2026, 10, 30, 9, 30)

    c.check("★ 08:00 的任务在 07:59 **不**触发", S.due_at(daily, None, t_0759) is None)
    c.check("★ 08:00 的任务在 08:01 触发", S.due_at(daily, None, t_0930) is not None)
    c.check("★ 返回的是**计划时刻**（08:00）而不是当前时刻",
            S.due_at(daily, None, t_0930) == t_0800,
            str(S.due_at(daily, None, t_0930)))
    c.check("★ 今天已经跑过（last_fired=今天 08:00）→ 09:30 **不**重复",
            S.due_at(daily, t_0800, t_0930) is None)
    c.check("★ 上次是**昨天** 08:00 → 今天 09:30 仍然触发（跨天正常）",
            S.due_at(daily, t_0800 - timedelta(days=1), t_0930) == t_0800)

    weekday = S.Trigger(kind="daily", at="08:00", weekdays=(1, 2, 3, 4, 5))
    saturday = datetime(2026, 10, 31, 9, 0)          # 2026-10-31 是周六
    c.check("仅工作日：周六不触发", S.due_at(weekday, None, saturday) is None)
    c.check("仅工作日：周五触发",
            S.due_at(weekday, None, datetime(2026, 10, 30, 9, 0)) == t_0800)

    every3h = S.Trigger(kind="interval", every_min=180)
    c.check("interval 首次（无记录）→ 立即到期", S.due_at(every3h, None, t_0930) == t_0930)
    c.check("interval 未到周期 → 不到期",
            S.due_at(every3h, t_0930 - timedelta(hours=1), t_0930) is None)
    c.check("interval 到周期 → 按「上次 + 周期」到期",
            S.due_at(every3h, t_0930 - timedelta(hours=3), t_0930)
            == t_0930)

    c.check("非法时刻格式 → 判定直接返回 None（不猜）",
            S.due_at(S.Trigger(kind="daily", at="8点"), None, t_0930) is None)
    c.check("非法时刻被 validate() 报出来",
            bool(S.Trigger(kind="daily", at="8点").validate()))
    c.check("未知触发类型被 validate() 报出来",
            bool(S.Trigger(kind="hourly").validate()))

    c.check("展示用的下一次：今天已过 08:00 → 明天 08:00",
            S.next_run(daily, None, t_0930) == datetime(2026, 10, 31, 8, 0),
            str(S.next_run(daily, None, t_0930)))


def section_weather(c: Checker) -> None:
    c.section("第 2 节：天气读数与规则（判定不出来就不触发）")

    today = S.parse_weather(WEATHER_TODAY)
    c.check("实况文本能解析出城市/天气/温度/风力",
            {"city", "weather", "temperature", "wind_power"} <= set(today),
            str(sorted(today)))
    c.check("实况里的温度原值保留（不在这里做单位换算）",
            today.get("temperature") == "18°C", today.get("temperature"))

    fc = S.parse_weather(WEATHER_TOMORROW)
    c.check("预报文本里 `白天温度` 不会被误读成 `温度`",
            fc.get("day_temp") == "36°C" and "temperature" not in fc,
            str(sorted(fc)))

    # ★ 下面这条是**接口真实返回的逐字形状**（2026-10-30 实测，阿克苏）。
    #   它同时踩了两个坑：`风向` 与 `风力` 挤在同一行、风力带 `≤` 前缀。
    REAL = ("城市: 阿克苏地区\n天气: 霾\n温度: 3°C\n"
            "风向: 东北 风力: ≤3级\n湿度: 73%\n发布时间: 2026-10-30 11:01:54")
    real = S.parse_weather(REAL)
    c.check("★ 真实接口格式：同一行的 `风向` 与 `风力` 都能取到",
            real.get("wind_direction") == "东北" and real.get("wind_power") == "≤3级",
            str(real))
    c.check("带 `≤` / `-` 前缀的风力能取到数字（3、1、7）",
            (S.as_number("≤3级"), S.as_number("1-3级"), S.as_number("≥7级"))
            == (3.0, 1.0, 7.0),
            str((S.as_number("≤3级"), S.as_number("1-3级"), S.as_number("≥7级"))))

    rules = [
        S.Rule(id="高温", field="day_temp", op=">=", value=35),
        S.Rule(id="低温", field="night_temp", op="<=", value=5),
        S.Rule(id="大风", field="day_wind_power", op=">=", value=6),
        S.Rule(id="沙尘", field="day_weather", op="contains", value="沙尘"),
    ]
    hits = S.evaluate_rules(fc, rules)
    ids = [h["id"] for h in hits]
    c.check("命中的是我们预期的三条（沙尘不命中）", ids == ["高温", "低温", "大风"], str(ids))
    c.check("命中项带着**实际读数**（预警里要写明）",
            any("36" in str(h["actual"]) for h in hits))

    c.check("★ 接口失败（空文本）→ 一个都不命中",
            S.evaluate_rules(S.parse_weather("天气查询失败: 网络错误"), rules) == [])
    only_temp = S.evaluate_rules({"day_temp": "36°C"}, rules)
    c.check("★ 只有 1 个字段时只命中对应那条（其余不误报）",
            [h["id"] for h in only_temp] == ["高温"], str([h["id"] for h in only_temp]))
    c.check("边界值：36 ≥ 35 命中、34 ≥ 35 不命中",
            bool(S.evaluate_rules({"day_temp": "36"}, rules[:1]))
            and not S.evaluate_rules({"day_temp": "34"}, rules[:1]))

    bad_field = S.Rule(id="x", field="不存在的字段", op=">=", value=1)
    c.check("引用未知字段的规则被判不合法", bool(bad_field.validate()))
    c.check("判不合法的规则在评估时被**跳过**（不会静默按 0 比较）",
            S.evaluate_rules({"day_temp": "99"}, [bad_field]) == [])
    c.check("阈值不是数字的规则被判不合法",
            bool(S.Rule(id="x", field="day_temp", op=">=", value="很热").validate()))

    text = S.format_alarm(hits)
    c.check("预警文字里带着规矩名、读数和触发条件",
            "高温" in text and "36" in text and ">=" in text, text)


def section_config(c: Checker) -> None:
    c.section("第 3 节：配置（出厂可用、往返不失真、写错就停用）")

    jobs = S.default_jobs()
    c.check("出厂配置有三个触发项（每日农情 + 天气预警 + 每日价格）",
            len(jobs) == 3, str(len(jobs)))
    c.check("出厂配置**全部合法**",
            all(j.ok for j in jobs), str({j.id: j.validate() for j in jobs}))
    c.check("出厂配置默认地区是阿克苏", jobs[0].inputs.get("city") == "阿克苏")

    roundtrip = [S._job_from_dict(j.to_dict()).to_dict() for j in jobs]
    c.check("★ 配置往返不丢字段（to_dict → from_dict → to_dict 相等）",
            roundtrip == [j.to_dict() for j in jobs])

    base = S.default_jobs()[0]
    bad_wf = S.Job(**{**base.__dict__, "workflow": "../../etc/passwd.yaml"})
    c.check("★ 工作流名带路径穿越 → 判不合法（它会拼进 resource_path）",
            not bad_wf.ok, bad_wf.validate())
    bad_wf2 = S.Job(**{**base.__dict__, "workflow": "not yaml.txt"})
    c.check("工作流不是 .yaml → 判不合法", not bad_wf2.ok)

    no_notice = S.Job(**{**base.__dict__, "notice_node": ""})
    c.check("★ 没配 notice_node → 判不合法（否则不知道推什么）", not no_notice.ok)

    watch = S.default_jobs()[1]
    no_rules = S.Job(**{**watch.__dict__, "rules": []})
    c.check("★ weather_watch 没有规则 → 判不合法（没有规则就没有信号）",
            not no_rules.ok, no_rules.validate())
    no_city = S.Job(**{**watch.__dict__, "inputs": {}})
    c.check("weather_watch 缺 city → 判不合法", not no_city.ok)

    # 坏配置在 tick 里**不执行**，只报一次
    sched, runner, events = _fresh(bad_wf)
    fired = sched.tick(datetime(2026, 10, 30, 9, 0))
    c.check("★ 配置不合法的任务：tick 不提交任何工作",
            fired == [] and runner.submitted == [], str(fired))
    c.check("★ 配置不合法会发出一条 invalid 事件（让人看得见）",
            any(e.get("type") == "invalid" for e in events), str(events))
    c.check("状态里记成 invalid", sched.state_of(bad_wf.id).last_status == "invalid")

    off = S.Job(**{**base.__dict__, "enabled": False})
    sched2, runner2, _ = _fresh(off)
    c.check("enabled=False → 不触发", sched2.tick(datetime(2026, 10, 30, 9, 0)) == []
            and runner2.submitted == [])

    # ------------------------------------------------------------------
    # ★ 坏字段不该让整份配置作废：`schedule.json` 是给人手改的，
    #   一个 `"every_min": "每3小时"` 曾让 load() 抛出、self.jobs 永远赋不上值，
    #   于是两个任务一起停摆，界面上一个字都看不到。
    # ------------------------------------------------------------------
    _isolate()
    raw_cfg = {
        "jobs": [
            {   # ① 写对的一条，必须活下来
                "id": "daily_brief", "title": "每日农情简报",
                "workflow": "daily_brief.yaml", "kind": "workflow", "enabled": True,
                "trigger": {"kind": "daily", "at": "08:00"},
                "inputs": {"city": "阿克苏", "region": "阿克苏地区"},
                "notice": {"node": "brief", "field": "body"},
            },
            {   # ② every_min 写了中文单位
                "id": "weather_watch", "title": "极端天气预警",
                "workflow": "weather_alert.yaml", "kind": "weather_watch",
                "trigger": {"kind": "interval", "every_min": "每3小时"},
                "inputs": {"city": "阿克苏"},
                "rules": [{"id": "大风", "field": "day_wind_power", "op": ">=",
                           "value": 6}],
                "notice": {"node": "advice"},
            },
            {   # ③ trigger 不是字典、inputs 是列表
                "id": "broken", "workflow": "daily_brief.yaml",
                "trigger": "不是字典", "inputs": [1, 2], "notice": {"node": "x"},
            },
            "干脆不是字典",        # ④ 条目本身就不对
        ]
    }
    (S.schedule_dir() / "schedule.json").write_text(
        json.dumps(raw_cfg, ensure_ascii=False), encoding="utf-8")
    sched3 = S.Scheduler(fetcher=lambda city, when: "")
    loaded, boom = True, ""
    try:
        sched3.load()
    except Exception as e:                                    # noqa: BLE001
        loaded, boom = False, f"{type(e).__name__}: {e}"
    c.check("★ 一个坏字段不让 load() 抛出（整份配置不再作废）", loaded, boom)

    ids = [j.id for j in sched3.jobs]
    c.check("★ 写对的那条照常加载", "daily_brief" in ids, str(ids))
    c.check("every_min 读不懂 → 退回 60 分钟，而不是丢掉整条任务",
            any(j.id == "weather_watch" and j.trigger.every_min == 60
                for j in sched3.jobs),
            str([(j.id, j.trigger.every_min) for j in sched3.jobs]))
    c.check("trigger 不是字典 → 按默认解析，不抛",
            any(j.id == "broken" and j.trigger.kind == "daily" for j in sched3.jobs),
            str(ids))
    c.check("inputs 是列表 → 退回空映射，不抛",
            any(j.id == "broken" and j.inputs == {} for j in sched3.jobs),
            str([(j.id, j.inputs) for j in sched3.jobs]))
    c.check("条目根本不是字典 → 整条丢掉",
            len(sched3.jobs) == 3, f"实际加载 {len(sched3.jobs)} 条: {ids}")

    # 反向：every_min 是数字时按原值生效（证明上面那条不是「永远退回 60」）
    raw_cfg["jobs"][1]["trigger"]["every_min"] = 45
    (S.schedule_dir() / "schedule.json").write_text(
        json.dumps(raw_cfg, ensure_ascii=False), encoding="utf-8")
    sched4 = S.Scheduler(fetcher=lambda city, when: "")
    sched4.load()
    c.check("★ 反向验证：every_min 是数字时按原值生效（不是恒退回 60）",
            [j.trigger.every_min for j in sched4.jobs
             if j.id == "weather_watch"] == [45],
            str([(j.id, j.trigger.every_min) for j in sched4.jobs]))


def section_fire_once(c: Checker) -> None:
    c.section("第 4 节：★ 一个计划时刻只触发一次（重启也不重复）")

    job = _daily_job()
    sched, runner, _ = _fresh(job, outputs={"brief": {"title": "T", "body": "晴，正常田管。"}})

    fired = sched.tick(datetime(2026, 10, 30, 8, 30))
    c.check("08:30 触发一次", fired == ["daily_brief"], str(fired))
    c.check("真的提交了工作流，且注入了知识库",
            len(runner.submitted) == 1 and "__kb__" in runner.submitted[0][2],
            str(runner.submitted))
    c.check("注入的输入带着触发日期",
            runner.submitted[0][2].get("today") == "2026-10-30",
            str(runner.submitted[0][2].get("today")))

    again = sched.tick(datetime(2026, 10, 30, 8, 31))
    c.check("★ 同一分钟再 tick → **不**重复触发", again == [], str(again))
    later = sched.tick(datetime(2026, 10, 30, 17, 0))
    c.check("★ 同一天晚些时候 → 仍然**不**重复", later == [], str(later))
    c.check("总提交次数仍是 1", len(runner.submitted) == 1, str(len(runner.submitted)))

    # 重启：同一份状态文件，新的调度器实例
    sched_b = S.Scheduler(runner=_Runner({}), kb_provider=lambda: {},
                          fetcher=lambda city, when: WEATHER_TOMORROW)
    sched_b.load()
    sched_b.jobs = [job]
    c.check("★ 重启后同一时刻**不**重复推送（状态落盘生效）",
            sched_b.tick(datetime(2026, 10, 30, 9, 0)) == [])

    c.check("★ 第二天 08:05 正常触发",
            sched_b.tick(datetime(2026, 10, 31, 8, 5)) == ["daily_brief"])

    # 补发窗口
    job2 = _daily_job()
    sched3, runner3, events3 = _fresh(job2)
    late = sched3.tick(datetime(2026, 10, 30, 20, 0))     # 迟到 12 小时 > 6 小时窗口
    c.check("★ 错过补发窗口 → **不**触发", late == [] and runner3.submitted == [],
            str(late))
    c.check("★ 错过也要留痕（状态 skipped）",
            sched3.state_of("daily_brief").last_status == "skipped",
            sched3.state_of("daily_brief").last_status)
    c.check("★ 跳过之后不会每个 tick 都重试一次",
            sched3.tick(datetime(2026, 10, 30, 20, 1)) == [])
    c.check("跳过失落时发出一条 skip 事件",
            any(e.get("type") == "skipped" for e in events3), str(events3))

    # 知识库没就绪
    sched4 = S.Scheduler(runner=_Runner({}), kb_provider=lambda: None,
                         fetcher=lambda city, when: WEATHER_TOMORROW)
    sched4.load()
    sched4.jobs = [job]
    c.check("★ 知识库未就绪 → 跳过，不拿空检索冒充结果",
            sched4.tick(datetime(2026, 10, 30, 8, 30)) == []
            and sched4.state_of("daily_brief").last_status == "skipped",
            sched4.state_of("daily_brief").last_note)


def section_consume(c: Checker) -> None:
    c.section("第 5 节：★ 没做成 ≠ 处理过（补发窗口必须真的能用）")

    job = _daily_job()
    t = datetime(2026, 10, 30, 8, 30)

    # ── 知识库晚到一分钟 ──
    _isolate()
    holder = {"kb": None}
    events: list = []
    runner = _Runner({"brief": {"title": "T", "body": "晴，正常田管。"}})
    sched = S.Scheduler(runner=runner, kb_provider=lambda: holder["kb"],
                        fetcher=lambda city, when: WEATHER_TOMORROW,
                        on_event=events.append)
    sched.load()
    sched.jobs = [job]

    c.check("知识库没好 → 不提交", sched.tick(t) == [] and runner.submitted == [])
    c.check("★ 知识库没好**不消费计划时刻**",
            sched.state_of(job.id).last_fired == "",
            repr(sched.state_of(job.id).last_fired))
    c.check("状态记成 skipped，能看出卡在哪",
            sched.state_of(job.id).last_status == "skipped",
            sched.state_of(job.id).last_status)

    sched.tick(t + timedelta(seconds=30))
    skipped = [e for e in events if e.get("type") == "skipped"]
    c.check("★ 同一个原因只提示一次（不消费意味着每 30 秒会重走一遍）",
            len(skipped) == 1, f"实际 {len(skipped)} 条")

    holder["kb"] = {"name": "stub-kb"}
    c.check("★ 库好了之后，**同一个计划时刻**仍然会跑（这就是补发窗口的意义）",
            sched.tick(t + timedelta(minutes=1)) == [job.id], str(runner.submitted))
    c.check("直到真的提交了，才写 last_fired",
            sched.state_of(job.id).last_fired == "2026-10-30T08:00:00",
            repr(sched.state_of(job.id).last_fired))
    _drain(sched)

    # ── 缺必需输入 ──
    _isolate()
    bad_inputs = S.Job(**{**job.__dict__, "inputs": {"region": "阿克苏地区"}})
    runner2 = _Runner({"brief": {"body": "x"}})
    sched2 = S.Scheduler(runner=runner2, kb_provider=lambda: {},
                         fetcher=lambda city, when: WEATHER_TOMORROW)
    sched2.load()
    sched2.jobs = [bad_inputs]
    c.check("缺 city → 不提交", sched2.tick(t) == [] and runner2.submitted == [])
    c.check("★ 缺输入也不消费计划时刻",
            sched2.state_of(job.id).last_fired == "",
            repr(sched2.state_of(job.id).last_fired))

    sched2.jobs = [job]
    c.check("★ 补上输入后，同一个时刻还能跑",
            sched2.tick(t + timedelta(minutes=2)) == [job.id])
    _drain(sched2)

    # ── 执行器抛异常 ──
    _isolate()
    flaky = _FlakyRunner({"brief": {"body": "x"}})
    sched3 = S.Scheduler(runner=flaky, kb_provider=lambda: {},
                         fetcher=lambda city, when: WEATHER_TOMORROW)
    sched3.load()
    sched3.jobs = [job]
    c.check("执行器抛异常 → 本轮没有提交", sched3.tick(t) == [] and flaky.submitted == [])
    c.check("★ 提交异常也不消费计划时刻",
            sched3.state_of(job.id).last_fired == "",
            repr(sched3.state_of(job.id).last_fired))
    c.check("★ 下一个 tick 会重试提交（不需要等到明天）",
            sched3.tick(t + timedelta(seconds=30)) == [job.id],
            str(flaky.submitted))
    _drain(sched3)

    # ── 反向：错过窗口仍然消费，不能矫枉过正 ──
    _isolate()
    runner4 = _Runner({"brief": {"body": "x"}})
    sched4 = S.Scheduler(runner=runner4, kb_provider=lambda: {},
                         fetcher=lambda city, when: WEATHER_TOMORROW)
    sched4.load()
    sched4.jobs = [job]
    c.check("★ 错过补发窗口**仍然消费**计划时刻（不是所有失败都重试）",
            sched4.tick(datetime(2026, 10, 30, 20, 0)) == []
            and sched4.state_of(job.id).last_fired == "2026-10-30T08:00:00",
            repr(sched4.state_of(job.id).last_fired))
    c.check("消费之后再 tick 不会复活",
            sched4.tick(datetime(2026, 10, 30, 20, 30)) == [])


def section_failure(c: Checker) -> None:
    c.section("第 6 节：★ 失败要说清是哪儿失败（别把人引到没问题的地方去改）")

    from core import providers

    job = _daily_job()
    t = datetime(2026, 10, 30, 8, 30)
    orig = providers.default_client

    # ── ① 模型根本没配：预检拦住，不提交、不消费、只提示一次 ──
    _isolate()
    events: list = []
    runner = _Runner({"brief": {"body": "x"}})
    sched = S.Scheduler(runner=runner, kb_provider=lambda: {},
                        fetcher=lambda city, when: WEATHER_TOMORROW,
                        on_event=events.append)
    sched.load()
    sched.jobs = [job]

    def _no_key(**kw):
        raise RuntimeError("没有可用密钥")

    providers.default_client = _no_key
    try:
        fired = sched.tick(t)
        sched.tick(t + timedelta(seconds=30))
    finally:
        providers.default_client = orig

    c.check("模型没配 → 不提交给执行器", fired == [] and runner.submitted == [],
            str(runner.submitted))
    c.check("★ 模型没配不消费计划时刻（配好之后当天还能补）",
            sched.state_of(job.id).last_fired == "",
            repr(sched.state_of(job.id).last_fired))
    told = [e for e in events if "模型" in str(e.get("text", ""))]
    c.check("★ 提示只有一条（不消费也不能刷屏）", len(told) == 1,
            str([e.get("text") for e in events]))
    c.check("★ 提示说的是模型配置，而不是「没取到文字」",
            bool(told) and "没取到" not in str(told[0].get("text")),
            str(told[0].get("text") if told else None))

    # 配好之后同一个时刻能补跑
    providers.default_client = lambda **kw: object()
    try:
        c.check("★ 模型恢复后，同一个计划时刻能补跑",
                sched.tick(t + timedelta(minutes=1)) == [job.id],
                str(runner.submitted))
    finally:
        providers.default_client = orig
    _drain(sched)

    # ── ② 跑了但失败：报失败节点自己的 error ──
    _isolate()
    events2: list = []
    providers.default_client = lambda **kw: object()
    try:
        sched2 = S.Scheduler(
            runner=_FailedRunner(_FailedRun("RuntimeError: 没有可用密钥")),
            kb_provider=lambda: {},
            fetcher=lambda city, when: WEATHER_TOMORROW,
            on_event=events2.append)
        sched2.load()
        sched2.jobs = [job]
        fired2 = sched2.tick(t)
    finally:
        providers.default_client = orig
    _wait_for(lambda: any(e.get("type") == "failed" for e in events2))
    fail = next((e for e in events2 if e.get("type") == "failed"), {})
    c.check("工作流失败时仍然提交并进入回收", fired2 == [job.id], str(fired2))
    c.check("★ 报的是失败节点自己的错误原文",
            "没有可用密钥" in str(fail.get("text")), str(fail.get("text")))
    c.check("★ 不再把失败说成「没取到要推送的文字」",
            "没取到" not in str(fail.get("text")), str(fail.get("text")))
    c.check("状态里带着结束状态，便于排查",
            "FAILED" in sched2.state_of(job.id).last_note,
            sched2.state_of(job.id).last_note)

    # ── ③ 取错的节点：`_first_error` 按流程顺序取第一条 ──
    c.check("_first_error 返回第一条节点错误",
            S._first_error(_FailedRun("第一处")) == "第一处")
    c.check("_first_error 在没有错误时返回空串",
            S._first_error(_Run({})) == "")
    c.check("_first_error 对没有 steps 的对象也不抛",
            S._first_error(object()) == "")

    # ── ④ 反向：不带 `state` 的旧式 Run 仍然走「取文字」那条路 ──
    c.check("★ 反向验证：Run 没有 state 时不误判为失败",
            S.extract_notice(_Run({"brief": {"body": "正文"}}), "brief") == "正文")


def section_deliver(c: Checker) -> None:
    c.section("第 7 节：取文字与推送（取不到就说取不到）")

    job = S.default_jobs()[0]
    sched, _runner, events = _fresh(
        job, outputs={"brief": {"title": "T", "body": "今天晴，适宜整枝。"}})
    sched.tick(datetime(2026, 10, 30, 8, 30))
    got = _wait_for(lambda: any(e.get("type") == "notice" for e in events))
    notice = next((e for e in events if e.get("type") == "notice"), {})
    c.check("★ 工作流跑完 → 发出 notice 事件", got, str(events))
    c.check("推送的就是节点里的那段正文", "今天晴" in str(notice.get("text")),
            str(notice.get("text")))
    c.check("推送带着标题与任务 id",
            notice.get("title") == "每日农情简报" and notice.get("job") == "daily_brief",
            str(notice))
    c.check("同时落了一份存档文件",
            bool(notice.get("path")) and Path(str(notice.get("path"))).is_file(),
            str(notice.get("path")))
    c.check("推送后状态记成 delivered",
            _wait_for(lambda: sched.state_of("daily_brief").last_status == "delivered"))

    # notice 节点写错
    typo = S.Job(**{**job.__dict__, "notice_node": "简报"})
    sched2, _r2, events2 = _fresh(typo, outputs={"brief": {"body": "有正文"}})
    sched2.tick(datetime(2026, 10, 30, 8, 30))
    got2 = _wait_for(lambda: any(e.get("type") == "failed" for e in events2))
    c.check("★ notice_node 写错 → 报 failed（不静默丢弃）", got2, str(events2))
    c.check("状态里写明是哪个字段取不到",
            "简报" in sched2.state_of(typo.id).last_note,
            sched2.state_of(typo.id).last_note)

    # 工作流没跑完
    sched3, _r3, events3 = _fresh(job, outputs=None)
    sched3.tick(datetime(2026, 10, 30, 8, 30))
    c.check("★ 工作流没跑完 → 报 failed，不假装已推送",
            _wait_for(lambda: any(e.get("type") == "failed" for e in events3)),
            str(events3))

    c.check("extract_notice 对不存在的节点返回空串",
            S.extract_notice(_Run({}), "没这个节点") == "")
    c.check("extract_notice 支持数组字段（拼成多行）",
            S.extract_notice(_Run({"n": {"body": ["一", "二"]}}), "n", "body") == "一\n二")


def section_alarm(c: Checker) -> None:
    c.section("第 8 节：预警的静默期（同一条不会一天内反复推）")

    watch = S.default_jobs()[1]
    sched, runner, events = _fresh(
        watch, outputs={"advice": {"title": "T", "body": "明天大风，今天别打药。"}},
        fetcher=lambda city, when: WEATHER_TOMORROW)

    t0 = datetime(2026, 10, 30, 9, 0)
    fired = sched.tick(t0)
    c.check("首轮命中规则 → 报警一次", fired == ["weather_watch"], str(fired))
    c.check("报警时把命中项传给了工作流",
            "大风" in str(runner.submitted[0][2].get("alarm")),
            str(runner.submitted[0][2].get("alarm")))

    t1 = t0 + timedelta(hours=3)
    c.check("★ 静默期内同一组信号**不**重复报警", sched.tick(t1) == [], "重复了")
    c.check("状态记成 suppressed",
            sched.state_of("weather_watch").last_status == "suppressed",
            sched.state_of("weather_watch").last_status)

    t2 = t0 + timedelta(minutes=watch.repeat_min + 1)
    c.check("★ 过了静默期仍命中 → 再报一次（提醒是有意义的）",
            sched.tick(t2) == ["weather_watch"])

    # 天气坏掉
    sched2, runner2, events2 = _fresh(watch, fetcher=lambda city, when: "天气查询失败: 网络错误")
    c.check("★ 接口坏掉 → 不报警（Fail-Closed）",
            sched2.tick(t0) == [] and runner2.submitted == [])
    c.check("★ 接口坏掉会留一条 degraded 事件（不静默）",
            any(e.get("type") == "degraded" for e in events2), str(events2))

    # 没有命中
    sched3, runner3, _ = _fresh(watch, fetcher=lambda city, when: WEATHER_TODAY)
    c.check("天气正常且无极端值 → 不报警，也不留噪声",
            sched3.tick(t0) == [] and runner3.submitted == [])


def section_contracts(c: Checker) -> None:
    c.section("第 9 节：工作流与节点契约（两处登记必须一致）")

    for name in ("daily_brief.yaml", "weather_alert.yaml", "price_brief.yaml"):
        path = ROOT / "workflows" / name
        c.check(f"{name} 存在", path.is_file())
        wf = loader.load_file(path)
        issues = validator.validate(wf)
        c.check(f"★ {name} 静态校验通过", not issues, validator.format_issues(issues))

        types = {n.type for n in wf.nodes}
        # ★ `tool.*` 不在白名单里 —— 它们由 `core/tools/schemas.py` **派生**，
        #   与 `validator._check_types()` 的口径一致（那里也是分开判的）。
        unrunnable = {t for t in types
                      if t not in catalog.EXECUTABLE_TYPES and not t.startswith("tool.")}
        c.check(f"{name} 的节点类型都可执行（tool.* 派生于工具表，其余须在白名单）",
                not unrunnable, str(unrunnable))
        c.check(f"{name} 的每个节点类型都在节点目录里",
                all(catalog.get(t) is not None for t in types),
                str([t for t in types if catalog.get(t) is None]))
        c.check(f"{name} 的节点类型都在引擎分派表里",
                all(orch_engine.executor_for(t) is not None for t in types),
                str([t for t in types if orch_engine.executor_for(t) is None]))
        c.check(f"{name} 每个节点都声明了 side_effect",
                all(n.side_effect is not None for n in wf.nodes))

    # 触发层给的输入必须覆盖工作流声明的必填项
    for job, wf_name in ((S.default_jobs()[0], "daily_brief.yaml"),
                         (S.default_jobs()[1], "weather_alert.yaml"),
                         (S.default_jobs()[2], "price_brief.yaml")):
        wf = loader.load_file(ROOT / "workflows" / wf_name)
        required = {k for k, v in wf.inputs.items() if v.required}
        supplied = set(job.inputs)
        if job.kind == "weather_watch":
            supplied |= {"alarm", "today"}
        else:
            supplied |= {"today"}
        c.check(f"★ {job.id} 提供了 {wf_name} 的全部必填输入",
                required <= supplied, str(required - supplied))

    # 提示词文件存在（`llm.*` 节点靠它，缺了打包版必失败）
    for ref in ("prompts/daily_brief.md", "prompts/weather_alert.md",
                "prompts/orch_judge.md"):
        c.check(f"{ref} 存在", (ROOT / ref).is_file())

    # ★ 契约必须包含执行器的硬要求
    spec = catalog.get("llm.summarize")
    c.check("★ `llm.summarize` 的契约声明了 output_schema（执行器强制要求）",
            spec is not None and "output_schema" in spec.param_names(),
            str(spec.param_names() if spec else None))
    c.check("★ `llm.summarize` 把 output_schema 标成必填",
            spec is not None and "output_schema" in spec.required_params())

    # 反向验证：每个 llm.* 节点的 catalog 参数必须覆盖执行器实际读取的键
    exec_keys = {"prompt_ref", "inputs", "output_schema"}
    for t in ("llm.decide", "llm.summarize"):
        s = catalog.get(t)
        c.check(f"★ {t} 的契约覆盖执行器读取的全部参数 {sorted(exec_keys)}",
                s is not None and exec_keys <= set(s.param_names()),
                str(exec_keys - set(s.param_names() if s else ())))


def section_pure(c: Checker) -> None:
    c.section("第 10 节：★ 触发层不依赖界面（否则测试与命令行都用不了）")

    src = (ROOT / "core" / "schedule.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    c.check("★ core/schedule.py 不 import PyQt（框架无关）", "PyQt6" not in mods, str(mods))
    c.check("★ 也不 import 浏览器/界面相关模块",
            not (mods & {"webview", "PySide6", "tkinter"}), str(mods))
    c.check("调度层只在这里被界面引用（唯一接线点）",
            (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
            .count("from core import schedule as sched") == 1)

    # 反向验证：把 PyQt 塞进去，第 8 节必须报警
    fake = src + "\nimport PyQt6\n"
    mods2: set[str] = set()
    for node in ast.walk(ast.parse(fake)):
        if isinstance(node, ast.Import):
            mods2 |= {a.name.split(".")[0] for a in node.names}
    c.check("★ 反向验证：塞入 PyQt 导入 → 第 10 节必须报警", "PyQt6" in mods2)

    # 对外接口：状态摘要与手动运行
    sched_b, runner_b, _ = _fresh(None, outputs={"brief": {"body": "x"}})
    rows = sched_b.status(datetime(2026, 10, 30, 7, 0))
    by_id = {r["id"]: r for r in rows}
    c.check("status() 给出每个任务的下次运行时刻（ISO 格式，非空）",
            bool(by_id["daily_brief"]["next_run"])
            and by_id["daily_brief"]["next_run"] == S.next_run(
                sched_b.job("daily_brief").trigger, None,
                datetime(2026, 10, 30, 7, 0)).isoformat(timespec="seconds"),
            str(by_id.get("daily_brief")))
    c.check("status() 对 interval 任务给的是「上次 + 周期」",
            by_id["weather_watch"]["next_run"] == "2026-10-30T07:00:00",
            str(by_id.get("weather_watch")))
    c.check("status() 报告配置是否合法", all(r["valid"] for r in rows))

    rid = sched_b.run_now("daily_brief")
    c.check("run_now() 手动跑一次并返回 run_id",
            bool(rid) and len(runner_b.submitted) == 1, rid)
    c.check("run_now() 之后同一计划时刻不再自动推一遍",
            "daily_brief" not in sched_b.tick(datetime(2026, 10, 30, 8, 30)),
            str(sched_b.state_of("daily_brief").last_fired))
    c.check("run_now() 对未知任务返回空串", sched_b.run_now("没有这个任务") == "")

    # ★ 反向验证：把 last_fired 清空，第 4 节的"不重复"必须失效
    sched, runner, _ = _fresh(_daily_job(), outputs={"brief": {"body": "x"}})
    sched.tick(datetime(2026, 10, 30, 8, 30))
    sched.state_of("daily_brief").last_fired = ""
    c.check("★ 反向验证：清空 last_fired → 同一时刻会再触发一次"
            "（证明第 4 节的断言不是恒真）",
            sched.tick(datetime(2026, 10, 30, 8, 31)) == ["daily_brief"])


def main() -> int:
    print(f"沙箱数据目录：{SANDBOX}")
    c = Checker()
    try:
        section_time(c)
        section_weather(c)
        section_config(c)
        section_fire_once(c)
        section_consume(c)
        section_failure(c)
        section_deliver(c)
        section_alarm(c)
        section_contracts(c)
        section_pure(c)
    finally:
        print()
        print("=" * 78)
        print(f"定时触发层守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())