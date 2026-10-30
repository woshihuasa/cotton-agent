# -*- coding: utf-8 -*-
"""主动触发层 —— 让工作流在「没人问」的时候自己跑起来。

【它补的是哪一块】

    `docs/ORCHESTRATION.md` 里 `notify.*` 一直标着「依赖『主动 Agent』，尚未实现」。
    缺的其实不是推送通道，而是**一个叫醒工作流的来源**：引擎已经有 DAG 调度、
    断点、幂等重试与指数退避，唯独没有「什么时候该跑」。

【两种触发】

    · `daily`     —— 每天固定时刻跑一次（每日农情简报）。
    · `interval`  —— 每 N 分钟轮询一次，由规则判定该不该报警（极端天气）。
    两种都落在同一个 `trigger` 字段上，判定函数 `due_at()` 是**纯函数**。

【三条纪律】

    ① **一个计划时刻只触发一次。** 依据是「计划时刻 vs 上次触发时刻」，
       不是「今天有没有跑过」—— 后者在跨天、补发、手动重跑时会重复推送。
    ② **错过就不补。** `grace_min` 之外只记一笔「已跳过」，不补发。
       早上八点的农情简报在晚上十点发出来没有意义，而补发会连着发好几条。
    ③ **判定不出来就不触发。** 天气接口失败、字段解析不出来、规则引用的字段
       不存在 —— 一律视为「没有信号」，绝不当成「正常」。
       这是 Fail-Closed 在触发层的写法：宁可少报一次，不可错报一次。

【它不做什么】

    · 不管怎么送达（聊天窗/托盘/邮件）。它只产出 `notice` 事件，由上层决定。
    · 不重试工作流。节点级重试已经够了，触发层再重试只会放大重复推送。
    · 不 import PyQt。界面接成信号，命令行与测试直接传回调（`tests/check_schedule.py`）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("Schedule")

#: 轮询间隔（秒）。判定本身是纯计算，30 秒足够准到「分钟内」。
TICK_SECONDS = 30.0

#: 默认补发窗口（分钟）。错过超过这个宽度就只记「已跳过」。
DEFAULT_GRACE_MIN = 360

#: 同一组预警信号的静默期（分钟）。避免一天里同一条大风预警反复推送。
DEFAULT_REPEAT_MIN = 720

#: 等一次工作流跑完的超时（秒）。超时**如实报告未完成**，不假装成功。
NOTICE_TIMEOUT = 300.0

#: 默认地区（新疆棉花主产区之一，也是项目数据集覆盖最全的地区）。
DEFAULT_CITY = "阿克苏"
DEFAULT_REGION = "阿克苏地区"

#: 工作流文件名只允许这个形状 —— 它会拼进 `resource_path()`，必须挡住路径穿越。
_WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.yaml$")

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


# ---------------------------------------------------------------------------
# 时刻判定（纯函数 —— 守卫直接测这些，不需要起线程）
# ---------------------------------------------------------------------------


def parse_clock(text: str) -> dtime:
    """`"08:00"` → `time(8, 0)`。格式不对就抛 `ValueError`（不猜）。"""
    m = _CLOCK_RE.match(str(text or "").strip())
    if not m:
        raise ValueError(f"时刻格式应为 HH:MM，收到 {text!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时刻越界：{text!r}")
    return dtime(hour=hour, minute=minute)


@dataclass(frozen=True)
class Trigger:
    """什么时候该跑。

    `weekdays` 用 ISO 编号（周一 1 … 周日 7），空表示每天。
    """

    kind: str = "daily"
    at: str = "08:00"
    every_min: int = 60
    weekdays: tuple[int, ...] = ()

    def validate(self) -> str:
        """返回空串表示合法；否则是一句人话的原因。"""
        if self.kind not in ("daily", "interval"):
            return f"未知的触发类型 {self.kind!r}（只支持 daily / interval）"
        if self.kind == "daily":
            try:
                parse_clock(self.at)
            except ValueError as e:
                return str(e)
        else:
            if int(self.every_min or 0) < 1:
                return f"interval 的 every_min 必须 ≥ 1，收到 {self.every_min!r}"
        for d in self.weekdays:
            if not (1 <= int(d) <= 7):
                return f"weekdays 只能用 1-7（周一=1），收到 {d!r}"
        return ""

    @property
    def ok(self) -> bool:
        return not self.validate()


def due_at(
    trigger: Trigger, last_fired: datetime | None, now: datetime
) -> datetime | None:
    """现在该不该触发？该就返回**计划时刻**（不是 now），否则 `None`。

    ★ 返回计划时刻而不是 now 是为了让记录诚实：状态里写的是「08:00 那次」，
      而不是「10:23 补的那次」。补发窗口也据此判断，与排查时看到的对得上。

    ★ `last_fired` 是**上一次真正处理过的计划时刻**（跳过也算处理），
      所以同一时刻不会被处理第二遍 —— 重启、崩溃恢复后都不会重复推送。
    """
    if not trigger.ok:
        return None

    if trigger.kind == "interval":
        step = timedelta(minutes=max(1, int(trigger.every_min)))
        if last_fired is None:
            return now
        nxt = last_fired + step
        return nxt if nxt <= now else None

    at = parse_clock(trigger.at)
    today_at = datetime.combine(now.date(), at)
    if trigger.weekdays and now.isoweekday() not in trigger.weekdays:
        return None
    if today_at > now:
        return None
    if last_fired is not None and last_fired >= today_at:
        return None
    return today_at


def next_run(
    trigger: Trigger, last_fired: datetime | None, now: datetime
) -> datetime | None:
    """下一次触发的时刻（只用于展示）。`interval` 返回上次 + 周期。"""
    if not trigger.ok:
        return None
    if trigger.kind == "interval":
        step = timedelta(minutes=max(1, int(trigger.every_min)))
        return now if last_fired is None else last_fired + step

    at = parse_clock(trigger.at)
    for offset in range(0, 9):
        day = (now + timedelta(days=offset)).date()
        if trigger.weekdays and day.isoweekday() not in trigger.weekdays:
            continue
        cand = datetime.combine(day, at)
        if cand > now:
            return cand
    return None


# ---------------------------------------------------------------------------
# 天气读数与预警规则
# ---------------------------------------------------------------------------

#: 天气工具返回的是**中文键值文本**（`温度: 12°C`），不是 JSON —— 这里做一次
#: 到字段名的翻译。键名与 `core/tools/external.py` 的输出**逐字对应**。
_WEATHER_KEYS: dict[str, str] = {
    "城市": "city",
    "日期": "date",
    "天气": "weather",
    "温度": "temperature",
    "风向": "wind_direction",
    "风力": "wind_power",
    "湿度": "humidity",
    "发布时间": "report_time",
    "白天天气": "day_weather",
    "夜间天气": "night_weather",
    "白天温度": "day_temp",
    "夜间温度": "night_temp",
    "白天风力": "day_wind_power",
    "夜间风力": "night_wind_power",
}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

#: 一行里可能挤着两个键值对 —— `core/tools/external.py` 的实况分支就是
#: `"风向: 西北风 " + "风力: 3级"` 拼在同一行（中间没有换行）。
#: 所以不能按行 `partition(":")`，必须**按出现的已知键切**。
#: ★ 键按长度倒序参与匹配，否则 `白天天气:` 会被 `天气:` 抢先切开。
_KEY_SPLIT_RE = re.compile(
    r"(?:^|\s)(" + "|".join(sorted((re.escape(k) for k in _WEATHER_KEYS),
                                   key=len, reverse=True)) + r")\s*:")

#: 规则允许的比较方式。
OPS: tuple[str, ...] = (">=", ">", "<=", "<", "contains")


def parse_weather(text: str) -> dict[str, str]:
    """天气文本 → `{字段: 原值}`。**一行里的多个键都能取到**，解析不出的忽略。"""
    out: dict[str, str] = {}
    pieces = _KEY_SPLIT_RE.split(str(text or ""))
    # re.split 带一个捕获组 → [前置, 键1, 值1, 键2, 值2, ...]
    for i in range(1, len(pieces) - 1, 2):
        name = _WEATHER_KEYS.get(pieces[i])
        if not name:
            continue
        value = pieces[i + 1].strip()
        if value:
            out[name] = value
    return out


def as_number(text: Any) -> float | None:
    """从 `"35°C"` / `"3-4"` / `"≤3"` 里取第一个数。取不到返回 `None`。"""
    m = _NUM_RE.search(str(text if text is not None else ""))
    return float(m.group(0)) if m else None


@dataclass(frozen=True)
class Rule:
    """一条预警规则。`field` 是 `parse_weather()` 的字段名。"""

    id: str
    field: str
    op: str
    value: Any
    hint: str = ""

    def validate(self) -> str:
        if not self.id:
            return "规则缺少 id"
        if self.field not in set(_WEATHER_KEYS.values()):
            return f"规则 {self.id} 引用了未知字段 {self.field!r}"
        if self.op not in OPS:
            return f"规则 {self.id} 的 op {self.op!r} 不支持（可用：{list(OPS)}）"
        if self.op != "contains" and as_number(self.value) is None:
            return f"规则 {self.id} 的阈值不是数字：{self.value!r}"
        return ""

    @property
    def ok(self) -> bool:
        return not self.validate()


def evaluate_rules(readings: dict[str, str], rules: list[Rule]) -> list[dict]:
    """按规则判。返回命中的规则（含实际读数），**不命中就是空列表**。

    ★ 缺字段 / 取不到数字 → **判为不命中**，绝不"跳过这条继续乐观往下走"。
      天气接口出错时 `readings` 是空的，于是全不命中 —— 不会误报。
    """
    hits: list[dict] = []
    for rule in rules:
        if not rule.ok:
            continue
        raw = readings.get(rule.field)
        if raw is None or raw == "":
            continue
        if rule.op == "contains":
            needle = str(rule.value)
            if needle and needle in raw:
                hits.append({"id": rule.id, "field": rule.field, "op": rule.op,
                             "threshold": needle, "actual": raw,
                             "hint": rule.hint})
            continue
        actual = as_number(raw)
        if actual is None:
            continue
        limit = as_number(rule.value)
        ok = {
            ">=": actual >= limit,
            ">": actual > limit,
            "<=": actual <= limit,
            "<": actual < limit,
        }.get(rule.op, False)
        if ok:
            hits.append({"id": rule.id, "field": rule.field, "op": rule.op,
                         "threshold": limit, "actual": raw, "hint": rule.hint})
    return hits


#: 字段名的中文说法（拼预警文字用）。
_FIELD_LABEL: dict[str, str] = {
    "temperature": "今天气温", "wind_power": "今天风力", "weather": "今天天气",
    "day_temp": "明天白天温度", "night_temp": "明天夜间温度",
    "day_wind_power": "明天白天风力", "night_wind_power": "明天夜间风力",
    "day_weather": "明天白天天气", "night_weather": "明天夜间天气",
    "humidity": "湿度",
}


def format_alarm(hits: list[dict]) -> str:
    """命中项 → 给模型看的一行行文字（也用于日志与状态）。"""
    lines: list[str] = []
    for h in hits:
        label = _FIELD_LABEL.get(h["field"], h["field"])
        lines.append(
            f"- {h['id']}：{label} {h['actual']}，"
            f"触发条件 {h['op']} {h['threshold']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 任务定义
# ---------------------------------------------------------------------------


def _rule_from_dict(raw: Any) -> Rule | None:
    if not isinstance(raw, dict):
        return None
    return Rule(
        id=str(raw.get("id") or "").strip(),
        field=str(raw.get("field") or "").strip(),
        op=str(raw.get("op") or ">=").strip(),
        value=raw.get("value"),
        hint=str(raw.get("hint") or ""),
    )


@dataclass
class Job:
    """一个触发项：什么时候、跑哪个工作流、把哪段文字推送出去。"""

    id: str
    title: str = ""
    workflow: str = ""
    kind: str = "workflow"          # workflow | weather_watch
    enabled: bool = True
    trigger: Trigger = field(default_factory=Trigger)
    inputs: dict[str, Any] = field(default_factory=dict)
    grace_min: int = DEFAULT_GRACE_MIN
    repeat_min: int = DEFAULT_REPEAT_MIN
    rules: list[Rule] = field(default_factory=list)
    #: 从哪个节点的哪个字段取推送文字。写死而不靠"猜最后一个节点"，
    #: 因为猜错的表现是"推送了一段 JSON 原文"，很难与"模型答得差"区分。
    notice_node: str = ""
    notice_field: str = "body"

    def validate(self) -> str:
        """空串表示合法。不合法的一律**不执行**（配置错了不该跑出半截结果）。"""
        if not self.id:
            return "任务缺少 id"
        if self.kind not in ("workflow", "weather_watch"):
            return f"未知任务类型 {self.kind!r}（只支持 workflow / weather_watch）"
        if not _WORKFLOW_NAME_RE.match(self.workflow or ""):
            return f"workflow 必须是 workflows/ 下的单个 .yaml 文件名，收到 {self.workflow!r}"
        msg = self.trigger.validate()
        if msg:
            return msg
        if self.kind == "weather_watch":
            if not self.rules:
                return "weather_watch 没有配任何规则 —— 没有规则就没有信号"
            for r in self.rules:
                bad = r.validate()
                if bad:
                    return bad
            if not self.inputs.get("city"):
                return "weather_watch 缺少 city 输入"
        if not self.notice_node:
            return "没有配 notice_node —— 不知道把哪段文字推送出去"
        return ""

    @property
    def ok(self) -> bool:
        return not self.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "workflow": self.workflow,
            "kind": self.kind,
            "enabled": bool(self.enabled),
            "trigger": {
                "kind": self.trigger.kind,
                "at": self.trigger.at,
                "every_min": int(self.trigger.every_min),
                "weekdays": list(self.trigger.weekdays),
            },
            "inputs": dict(self.inputs),
            "grace_min": int(self.grace_min),
            "repeat_min": int(self.repeat_min),
            "rules": [
                {"id": r.id, "field": r.field, "op": r.op,
                 "value": r.value, "hint": r.hint}
                for r in self.rules
            ],
            "notice": {"node": self.notice_node, "field": self.notice_field},
        }


def _job_from_dict(raw: Any) -> Job | None:
    """容错解析。**字段缺失取保守默认**，读不懂的整条丢掉（宁可少一条任务）。

    ★ 「整条丢掉」这句话是**承诺**，不是修辞：`schedule.json` 是给人手改的，
      一个字段写错不该让其余任务跟着停摆。所以每个可能抛的转换都在这里兜住。
      第一版漏了 `every_min` —— 它在 `Trigger(...)` 的构造参数里，没有 try，
      写一个 `"每3小时"` 就让整份配置加载失败（`load()` 里 `self.jobs` 永远
      赋不上值），且界面上一个字都看不到，只有每 30 秒一条日志。
      `tests/check_schedule.py` 第 3 节把三类坏值都钉住了。
    """
    if not isinstance(raw, dict):
        return None
    trig_raw = raw.get("trigger") or {}
    if not isinstance(trig_raw, dict):
        trig_raw = {}
    weekdays_raw = trig_raw.get("weekdays") or []
    try:
        weekdays = tuple(int(x) for x in weekdays_raw)
    except (TypeError, ValueError):
        weekdays = ()
    notice = raw.get("notice") or {}
    if not isinstance(notice, dict):
        notice = {}
    inputs_raw = raw.get("inputs")
    # ★ `dict()` 同样会抛（传进来一个长度不齐的列表就会 ValueError），
    #   所以它也必须在 try 里，而不是直接写在构造参数里。
    try:
        inputs = dict(inputs_raw or {})
    except (TypeError, ValueError):
        inputs = {}
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        rules_raw = []
    rules = [r for r in (_rule_from_dict(x) for x in rules_raw) if r]
    try:
        grace = int(raw.get("grace_min", DEFAULT_GRACE_MIN))
    except (TypeError, ValueError):
        grace = DEFAULT_GRACE_MIN
    try:
        repeat = int(raw.get("repeat_min", DEFAULT_REPEAT_MIN))
    except (TypeError, ValueError):
        repeat = DEFAULT_REPEAT_MIN
    try:
        every_min = int(trig_raw.get("every_min") or 60)
    except (TypeError, ValueError):
        # ★ 退回 60 而不是丢掉整条：其余字段可能都是对的，而且这一条任务
        #   仍然有意义。但必须留日志 —— 静默把「每 3 小时」变成「每 1 小时」
        #   是那种「看起来在正常工作」的错误。
        log.warning("trigger.every_min 不是数字（%r），已退回 60 分钟",
                    trig_raw.get("every_min"))
        every_min = 60
    return Job(
        id=str(raw.get("id") or "").strip(),
        title=str(raw.get("title") or ""),
        workflow=str(raw.get("workflow") or "").strip(),
        kind=str(raw.get("kind") or "workflow").strip(),
        enabled=bool(raw.get("enabled", True)),
        trigger=Trigger(
            kind=str(trig_raw.get("kind") or "daily").strip(),
            at=str(trig_raw.get("at") or "08:00").strip(),
            every_min=every_min,
            weekdays=weekdays,
        ),
        inputs=inputs,
        grace_min=grace,
        repeat_min=repeat,
        rules=rules,
        notice_node=str(notice.get("node") or "").strip(),
        notice_field=str(notice.get("field") or "body").strip(),
    )


def default_jobs() -> list[Job]:
    """出厂配置：每日农情 + 极端天气轮询。

    ★ 阈值取自新疆棉花的公开农艺口径，**不是模型编的**：
      连续低温（≤5℃）影响出苗与吐絮、日间风力 ≥6 级影响化控与打顶作业、
      沙尘与冰雹属直接致灾天气。要改就在 `schedule.json` 里改，不用改代码。
    """
    return [
        Job(
            id="daily_brief",
            title="每日农情简报",
            workflow="daily_brief.yaml",
            kind="workflow",
            enabled=True,
            trigger=Trigger(kind="daily", at="08:00"),
            inputs={"city": DEFAULT_CITY, "region": DEFAULT_REGION},
            grace_min=DEFAULT_GRACE_MIN,
            notice_node="brief",
            notice_field="body",
        ),
        Job(
            id="weather_watch",
            title="极端天气预警",
            workflow="weather_alert.yaml",
            kind="weather_watch",
            enabled=True,
            trigger=Trigger(kind="interval", every_min=180),
            inputs={"city": DEFAULT_CITY, "region": DEFAULT_REGION},
            repeat_min=DEFAULT_REPEAT_MIN,
            rules=[
                Rule(id="低温", field="night_temp", op="<=", value=5,
                     hint="夜间低温，注意防霜冻与出苗风险"),
                Rule(id="高温", field="day_temp", op=">=", value=35,
                     hint="高温胁迫，注意滴水降温"),
                Rule(id="大风", field="day_wind_power", op=">=", value=6,
                     hint="大风天不宜化控、打顶与喷药"),
                Rule(id="沙尘", field="day_weather", op="contains", value="沙尘",
                     hint="沙尘会损伤幼叶，注意防护"),
            ],
            notice_node="advice",
            notice_field="body",
        ),
    ]


# ---------------------------------------------------------------------------
# 落盘（配置与状态分开 —— 改配置不该动运行记录）
# ---------------------------------------------------------------------------


def schedule_dir() -> Path:
    """配置与状态目录（惰性取 `app_data_dir()`，避免 import 期触碰配置）。

    ★ 刻意**不放** `workflows/` 下面：打包脚本把整个 `workflows/` 目录收进
      datas，放进去就会把本机的调度状态一起打进安装包（`workflows/runs/`
      已经踩过同一个坑）。运行态数据只应该待在运行数据目录里。
    """
    from config import app_data_dir
    d = app_data_dir() / "schedule"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON：临时文件 + `os.replace`（与 `core/orchestration/store.py` 同一条纪律）。"""
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


@dataclass
class JobState:
    """一个任务上次跑成什么样。全部用 ISO 文本存，跨进程读回不需要额外解析规则。"""

    last_fired: str = ""
    last_status: str = ""       # fired / delivered / skipped / suppressed / failed
    last_run_id: str = ""
    last_note: str = ""
    last_alert_key: str = ""
    last_alert_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "last_fired": self.last_fired, "last_status": self.last_status,
            "last_run_id": self.last_run_id, "last_note": self.last_note,
            "last_alert_key": self.last_alert_key,
            "last_alert_at": self.last_alert_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "JobState":
        if not isinstance(raw, dict):
            return cls()
        return cls(**{k: str(raw.get(k) or "") for k in cls().to_dict()})


def _iso(dt: datetime | None) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


def _parse_iso(text: str) -> datetime | None:
    """容错解析 ISO 时间（读到坏值当作"从没跑过"，而不是抛异常让整个调度停摆）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class Scheduler:
    """按 `schedule.json` 触发工作流，把产出交给 `on_event`。

    【注入点】全部可选，测试据此不需要网络与线程：
        · `runner`      —— 有 `submit_workflow(wf, inputs)` 与 `wait(run_id, timeout)`
        · `kb_provider` —— 返回知识库实例（`data.kb_query` 需要）；返回 None 则跳过
        · `fetcher`     —— `(city, date) -> str`，天气原文
        · `on_event`    —— 收 `{"type": ...}` 的字典
        · `now` 参数    —— `tick(now=...)` 用假时钟跑判定
    """

    def __init__(
        self,
        *,
        runner: Any = None,
        kb_provider: Callable[[], Any] | None = None,
        fetcher: Callable[[str, str], str] | None = None,
        on_event: Callable[[dict], None] | None = None,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self._runner = runner
        self._kb_provider = kb_provider
        self._fetcher = fetcher
        self.on_event = on_event
        self.tick_seconds = max(1.0, float(tick_seconds))

        self.jobs: list[Job] = []
        self._state: dict[str, JobState] = {}
        self._inflight: dict[str, str] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._loaded = False

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 配置与状态 ----

    @property
    def config_file(self) -> Path:
        return schedule_dir() / "schedule.json"

    @property
    def state_file(self) -> Path:
        return schedule_dir() / "state.json"

    def load(self) -> None:
        """读配置与状态。**配置文件不存在就写一份出厂配置**，让用户有东西可改。"""
        jobs: list[Job] = []
        path = self.config_file
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("调度配置读不出来（%s），本轮不跑任何任务：%s", path, e)
                raw = {"jobs": []}
            if not isinstance(raw, dict):
                raw = {"jobs": []}
            items = raw.get("jobs")
            if not isinstance(items, list):
                # ★ 顶层形状不对就当没有任务。不能直接 for 一个 dict ——
                #   那会拿键名当任务项，一条条解析成「什么都没有」。
                log.warning("调度配置的 jobs 不是列表（%s），本轮不跑任何任务",
                            type(items).__name__)
                items = []
            for item in items:
                # ★ 这一层是最后一道网：`_job_from_dict` 已经把已知字段都兜住了，
                #   但以后加字段还会再漏。单条解析失败只丢这一条，
                #   绝不让整份配置作废 —— 那正是 F2 的前身。
                try:
                    job = _job_from_dict(item)
                except Exception as e:                        # noqa: BLE001
                    log.warning("调度任务配置解析失败，已跳过：%s: %s",
                                type(e).__name__, e)
                    continue
                if job is not None:
                    jobs.append(job)
        else:
            jobs = default_jobs()
            try:
                _atomic_write_json(path, {"jobs": [j.to_dict() for j in jobs]})
            except OSError as e:
                log.warning("出厂调度配置写不出去（%s），仅本次内存生效", e)

        self.jobs = jobs
        self._state = self._read_state()
        self._loaded = True

    def _read_state(self) -> dict[str, JobState]:
        path = self.state_file
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # ★ 状态读不出来只能按「未运行过」处理，代价是可能补发一次。
            #   这里必须留痕 —— 否则"为什么又推了一遍"永远查不到原因。
            log.warning("调度状态文件损坏，按「未运行过」处理：%s", path)
            return {}
        jobs = raw.get("jobs") if isinstance(raw, dict) else {}
        if not isinstance(jobs, dict):
            return {}
        return {k: JobState.from_dict(v) for k, v in jobs.items()}

    def save_state(self) -> None:
        if not self._dirty:
            return
        payload = {"jobs": {k: v.to_dict() for k, v in self._state.items()}}
        try:
            _atomic_write_json(self.state_file, payload)
            self._dirty = False
        except OSError as e:
            # ★ 状态写不出去**不该让调度停摆**，但必须留痕 ——
            #   代价是下次判定可能重复触发一次，这是可接受的降级。
            log.warning("调度状态写不出去：%s", e)

    def state_of(self, job_id: str) -> JobState:
        return self._state.setdefault(job_id, JobState())

    def job(self, job_id: str) -> Job | None:
        for j in self.jobs:
            if j.id == job_id:
                return j
        return None

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        for j in self.jobs:
            if j.id == job_id:
                j.enabled = bool(enabled)
                try:
                    _atomic_write_json(self.config_file,
                                       {"jobs": [x.to_dict() for x in self.jobs]})
                except OSError as e:
                    log.warning("开关写不回去：%s", e)
                return True
        return False

    # ---- 判定与触发 ----

    def tick(self, now: datetime | None = None) -> list[str]:
        """跑一轮判定。返回本轮真正提交了工作的 `job.id` 列表。"""
        if not self._loaded:
            self.load()
        now = (now or datetime.now()).replace(microsecond=0)
        fired: list[str] = []

        for job in list(self.jobs):
            if not job.enabled:
                continue
            reason = job.validate()
            if reason:
                # ★ 配错了就**不动它**，只报一次（不每 30 秒刷一遍日志）
                st = self.state_of(job.id)
                if st.last_note != reason:
                    st.last_note = reason
                    st.last_status = "invalid"
                    self._dirty = True
                    self._emit({"type": "invalid", "job": job.id,
                                "text": f"「{job.title or job.id}」配置有误，已停用：{reason}"})
                continue
            try:
                if job.kind == "weather_watch":
                    got = self._tick_watch(job, now)
                else:
                    got = self._tick_once(job, now)
            except Exception as e:                            # noqa: BLE001
                log.exception("调度任务 %s 判定失败", job.id)
                self._emit({"type": "error", "job": job.id,
                            "text": f"{type(e).__name__}: {e}"})
                continue
            if got:
                fired.append(job.id)

        self.save_state()
        return fired

    def _tick_once(self, job: Job, now: datetime) -> str:
        """每日类任务：到点就跑，错过就记一笔。"""
        st = self.state_of(job.id)
        scheduled = due_at(job.trigger, _parse_iso(st.last_fired), now)
        if scheduled is None:
            return ""

        late_min = (now - scheduled).total_seconds() / 60.0
        if late_min > job.grace_min:
            self._record(job, st, "skipped", scheduled,
                         note=f"错过 {late_min:.0f} 分钟（窗口 {job.grace_min}），不补发")
            self._emit({"type": "skipped", "job": job.id, "text":
                        f"「{job.title or job.id}」{scheduled:%H:%M} 那次已错过 "
                        f"{late_min:.0f} 分钟，按不补发处理。"})
            return ""

        if job.id in self._inflight:
            return ""
        if not self._submit(job, scheduled, extra_inputs=None):
            return ""
        return job.id

    def _tick_watch(self, job: Job, now: datetime) -> str:
        """轮询类任务：取数 → 判规则 → 命中且不在静默期才报警。"""
        st = self.state_of(job.id)
        scheduled = due_at(job.trigger, _parse_iso(st.last_fired), now)
        if scheduled is None:
            return ""

        # ★ 无论有没有命中，本轮都算「处理过了」——
        #   否则接口一直正常无事时，每个 tick 都要重取一次天气。
        st.last_fired = _iso(scheduled)
        self._dirty = True

        if job.id in self._inflight:
            return ""

        raw = self._fetch(str(job.inputs.get("city") or ""), "tomorrow")
        readings = parse_weather(raw)
        if not readings:
            st.last_status = "no-alarm"
            st.last_note = "天气数据不可解析，本轮不判（Fail-Closed）"
            self._dirty = True
            self._emit({"type": "degraded", "job": job.id,
                        "text": f"「{job.title or job.id}」拿不到可解析的天气，本轮跳过。"})
            return ""

        hits = evaluate_rules(readings, job.rules)
        if not hits:
            st.last_status = "no-alarm"
            st.last_note = "本轮无信号"
            self._dirty = True
            return ""

        key = "|".join(sorted(h["id"] for h in hits))
        last_at = _parse_iso(st.last_alert_at)
        if (key == st.last_alert_key and last_at is not None
                and (now - last_at).total_seconds() < job.repeat_min * 60):
            st.last_status = "suppressed"
            st.last_note = f"同一组信号（{key}）在 {job.repeat_min} 分钟静默期内"
            self._dirty = True
            return ""

        if not self._submit(job, scheduled,
                            extra_inputs={"alarm": format_alarm(hits)}):
            return ""
        st.last_alert_key = key
        st.last_alert_at = _iso(now)
        self._dirty = True
        self._emit({"type": "alarm", "job": job.id,
                    "text": f"{job.title or job.id}：命中 {key}"})
        return job.id

    # ---- 提交 ----

    def _submit(self, job: Job, scheduled: datetime,
                *, extra_inputs: dict | None) -> str:
        """校验 → 注入运行时对象 → 交执行器。成功返回 run_id，失败返回空串。"""
        wf, issues = self._load_workflow(job.workflow)
        if wf is None:
            # ★ 不消费：YAML 是随包发的，读不出来多半是构建问题或临时读盘失败，
            #   当天还有六小时可以重试，没理由第一次就放弃。
            changed = self._record(
                job, self.state_of(job.id), "failed", scheduled,
                note="工作流不可用：" + "；".join(issues[:3]), consume=False)
            if changed:
                self._emit({"type": "invalid", "job": job.id, "text":
                            f"「{job.title or job.id}」的工作流没跑起来："
                            + "；".join(issues[:3])})
            return ""

        inputs: dict[str, Any] = dict(job.inputs)
        inputs["__scheduled_at__"] = _iso(scheduled)
        inputs.setdefault("today", f"{scheduled:%Y-%m-%d}")
        inputs.update(extra_inputs or {})

        # ★ Fail-Closed：工作流声明了 required 的输入而这次没给，会一路跑到节点里
        #   变成字面量 `{{region}}`（`NodeContext` 刻意不把解析失败静默成空串），
        #   最终表现为「提示词里出现两个大括号」。所以在这里就拦住。
        missing = [name for name, spec in (getattr(wf, "inputs", {}) or {}).items()
                   if getattr(spec, "required", False) and name not in inputs]
        if missing:
            # ★ 不消费：缺输入多半是配置写漏了，用户当天改好重启就该补上，
            #   而不是「今天的名额已经用掉了」。
            changed = self._record(
                job, self.state_of(job.id), "skipped", scheduled,
                note="缺少必需输入：" + "、".join(missing), consume=False)
            if changed:
                self._emit({"type": "invalid", "job": job.id, "text":
                            f"「{job.title or job.id}」缺少必需输入：{'、'.join(missing)}"})
            return ""

        # ★ 只有真用到 `data.kb_query` 才去要知识库 —— 否则白加载一次向量库。
        if any(n.type == "data.kb_query" for n in wf.nodes):
            kb = self._kb()
            if kb is None:
                # ★ 这是本模块最典型的一个「没做成」：知识库在后台同步，
                #   通常一分钟就好了。不消费计划时刻，下一个 tick 再来一次，
                #   六小时窗口内几乎必然能补上。
                changed = self._record(
                    job, self.state_of(job.id), "skipped", scheduled,
                    note="知识库未就绪，本次不跑（不拿空检索冒充结果）",
                    consume=False)
                if changed:
                    self._emit({"type": "skipped", "job": job.id, "text":
                                f"「{job.title or job.id}」需要知识库，但知识库未就绪，"
                                f"本次不跑（{job.grace_min} 分钟窗口内会重试）。"})
                return ""
            inputs["__kb__"] = kb

        # ★ 模型预检：工作流里只要有 `llm.*` 节点，就先确认客户端能构造出来。
        #   缺 Key 时 `providers.default_client()` 在**构造阶段**就抛（包括未设
        #   `LLM_PROVIDER` 时 `OpenAI(...)` 因空 Key 抛的那种），不花一次请求。
        #
        #   它拦的是「根本没配」，拦不住「配了但额度用完 / 网络不通 / 输出不符合
        #   契约」—— 那三种由 `_watch()` 看 `run.state` 报。两段各管一段，不重复。
        if any(n.type.startswith("llm.") for n in wf.nodes):
            problem = self._model_problem()
            if problem:
                changed = self._record(
                    job, self.state_of(job.id), "skipped", scheduled,
                    note="模型不可用：" + problem, consume=False)
                if changed:
                    self._emit({"type": "skipped", "job": job.id, "text":
                                f"「{job.title or job.id}」要用模型，但当前没有可用配置："
                                f"{problem}。配好之后当天会补上（{job.grace_min} 分钟"
                                f"窗口内会重试）。"})
                return ""

        try:
            handle = self._get_runner().submit_workflow(wf, inputs)
        except Exception as e:                                # noqa: BLE001
            # ★ 不消费：抛异常说明执行器本身出了问题，不是「今天不发了」。
            changed = self._record(
                job, self.state_of(job.id), "failed", scheduled,
                note=f"提交失败：{type(e).__name__}: {e}", consume=False)
            if changed:
                self._emit({"type": "failed", "job": job.id, "text":
                            f"「{job.title or job.id}」提交失败：{e}"})
            return ""

        if not getattr(handle, "launched", False):
            issues2 = list(getattr(handle, "issues", []) or [])
            changed = self._record(
                job, self.state_of(job.id), "failed", scheduled,
                note="未启动：" + ("；".join(issues2[:2]) or "未知原因"), consume=False)
            if changed:
                self._emit({"type": "invalid", "job": job.id, "text":
                            f"「{job.title or job.id}」没有被启动："
                            + ("；".join(issues2[:2]) or "未知原因")})
            return ""

        run_id = str(getattr(handle, "run_id", "") or "")
        with self._lock:
            self._inflight[job.id] = run_id
        self._record(job, self.state_of(job.id), "fired", scheduled,
                     note="已提交", run_id=run_id)

        threading.Thread(target=self._watch, name=f"sched-{job.id}",
                         args=(job, run_id), daemon=True).start()
        return run_id

    def run_now(self, job_id: str) -> str:
        """手动跑一次（界面的「立即运行」）。

        ★ 它**会**更新「上次触发」记录：手动看过一遍之后，同一个计划时刻不再自动
          推一遍（否则用户刚点完，一分钟后还会收到同样的内容）。
        """
        job = self.job(job_id)
        if job is None or not job.ok:
            return ""
        now = datetime.now().replace(microsecond=0)
        if job.kind == "weather_watch":
            hits = evaluate_rules(
                parse_weather(self._fetch(str(job.inputs.get("city") or ""),
                                          "tomorrow")), job.rules)
            return self._submit(job, now,
                                extra_inputs={"alarm": format_alarm(hits)
                                              or "（手动运行，未命中规则）"})
        return self._submit(job, now, extra_inputs=None)

    def _watch(self, job: Job, run_id: str) -> None:
        """等工作流跑完 → 取文字 → 存档 → 推送。**在线程里跑，不阻塞调度。**"""
        try:
            run = self._get_runner().wait(run_id, timeout=NOTICE_TIMEOUT)
        except Exception as e:                                # noqa: BLE001
            run = None
            log.warning("等待 %s 失败：%s", run_id, e)
        finally:
            with self._lock:
                self._inflight.pop(job.id, None)

        st = self.state_of(job.id)
        if run is None:
            st.last_status = "failed"
            st.last_note = f"工作流 {run_id} 未在 {NOTICE_TIMEOUT:.0f} 秒内完成"
            self._dirty = True
            self.save_state()
            self._emit({"type": "failed", "job": job.id, "run_id": run_id,
                        "text": f"「{job.title or job.id}」没有跑完：{st.last_note}"})
            return

        # ★ 先看**工作流成没成**，再看能不能取到文字。
        #   第一版只看后者，于是模型没配、节点失败时，用户收到的是
        #   「跑完了，但没取到要推送的文字 —— 检查 notice 配置」：
        #   三处都说错（没跑完、不是取文字的问题、notice 配置是对的），
        #   把人指向一个本来就没毛病的地方。真正的原因在 run 里躺着没人读。
        state_name = str(getattr(getattr(run, "state", None), "name", "") or "").strip()
        if state_name and state_name != "DONE":
            reason = _first_error(run) or "没有可用的节点错误"
            st.last_status = "failed"
            st.last_note = f"工作流结束状态 {state_name}：{reason}"
            self._dirty = True
            self.save_state()
            self._emit({"type": "failed", "job": job.id, "run_id": run_id,
                        "text": f"「{job.title or job.id}」没有生成内容（{state_name}）："
                                f"{reason}"})
            return

        text = extract_notice(run, job.notice_node, job.notice_field)
        if not text:
            st.last_status = "failed"
            st.last_note = (f"节点 {job.notice_node!r} 没有字段 "
                            f"{job.notice_field!r} —— 检查 notice 配置")
            self._dirty = True
            self.save_state()
            self._emit({"type": "failed", "job": job.id, "run_id": run_id,
                        "text": f"「{job.title or job.id}」跑完了，但没取到要推送的文字："
                                f"{st.last_note}"})
            return

        path = self._archive(job, text)
        st.last_status = "delivered"
        st.last_note = f"已推送；存档 {path.name if path else '（未写成）'}"
        self._dirty = True
        self.save_state()
        self._emit({"type": "notice", "job": job.id, "run_id": run_id,
                    "title": job.title or job.id, "text": text,
                    "path": str(path) if path else ""})

    def _archive(self, job: Job, text: str) -> Path | None:
        """把推送内容落一份 Markdown 存档（用户事后能翻，而不是只有聊天气泡）。"""
        try:
            from config import app_data_dir
            d = app_data_dir() / "reports"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{job.id}-{datetime.now():%Y-%m-%d}.md"
            p.write_text(f"# {job.title or job.id}\n\n{text}\n", encoding="utf-8")
            return p
        except OSError as e:
            log.warning("简报存档写不出去：%s", e)
            return None

    # ---- 依赖（懒创建，便于测试替换）----

    def _get_runner(self) -> Any:
        """取执行器。

        ★ 方法名**不能**叫 `_runner` —— 那会和 `self._runner` 属性同名覆盖，
          于是 `self._runner()` 变成「调用一个实例」，直接 TypeError。
          （第一版就是这么写的，守卫当场把它照出来了。）
        """
        if self._runner is None:
            from core import handoff
            self._runner = handoff.get_runner()
        return self._runner

    def _kb(self) -> Any:
        if self._kb_provider is None:
            return None
        try:
            return self._kb_provider()
        except Exception as e:                                # noqa: BLE001
            log.warning("取知识库失败：%s", e)
            return None

    @staticmethod
    def _model_problem() -> str:
        """能不能构造出默认模型客户端。空串表示可以。

        ★ 只构造、不调用：成本是零，也不依赖网络。它回答的问题是
          「Key 配齐了没有」，不是「Key 还有没有额度」。
        """
        try:
            from core import providers
            providers.default_client()
            return ""
        except Exception as e:                                # noqa: BLE001
            return f"{type(e).__name__}: {e}"

    def _fetch(self, city: str, when: str) -> str:
        """取天气原文。默认走真实工具，测试注入假实现。"""
        if self._fetcher is not None:
            try:
                return str(self._fetcher(city, when) or "")
            except Exception as e:                            # noqa: BLE001
                log.warning("取天气失败：%s", e)
                return ""
        try:
            from core.tools import execute_tool
            return execute_tool("get_weather", {"city": city, "date": when})
        except Exception as e:                                # noqa: BLE001
            log.warning("取天气失败：%s", e)
            return ""

    @staticmethod
    def _load_workflow(name: str) -> tuple[Any, list[str]]:
        """按文件名加载并静态校验。**校验不过不跑**（不留"看起来在跑"的空任务）。"""
        from config import resource_path
        from core.orchestration import loader, validator

        if not _WORKFLOW_NAME_RE.match(name or ""):
            return None, [f"非法的 workflow 名 {name!r}"]
        path = resource_path(f"workflows/{name}")
        if not path.is_file():
            return None, [f"工作流文件不存在：{path}"]
        try:
            wf = loader.load_file(path)
        except Exception as e:                                # noqa: BLE001
            return None, [f"YAML 解析失败：{type(e).__name__}: {e}"]
        try:
            errors = validator.errors_only(validator.validate(wf))
        except Exception as e:                                # noqa: BLE001
            return None, [f"静态校验异常：{type(e).__name__}: {e}"]
        if errors:
            return None, [str(i) for i in errors]
        return wf, []

    # ---- 线程 ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        # ★ 不立刻 tick：启动时就把每个 interval 任务当成"到期一次"，
        #   会让每次开程序都去请求一遍天气。第一轮等一个周期。
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception:                                 # noqa: BLE001
                log.exception("调度轮询异常")

    # ---- 状态展示 ----

    def status(self, now: datetime | None = None) -> list[dict]:
        """给界面看的摘要（下次何时跑、上次怎么样）。"""
        if not self._loaded:
            self.load()
        now = (now or datetime.now()).replace(microsecond=0)
        out: list[dict] = []
        for job in self.jobs:
            st = self.state_of(job.id)
            try:
                nxt = next_run(job.trigger, _parse_iso(st.last_fired), now)
            except ValueError:
                nxt = None
            out.append({
                "id": job.id,
                "title": job.title or job.id,
                "enabled": bool(job.enabled),
                "kind": job.kind,
                "valid": job.ok,
                "reason": job.validate(),
                "next_run": _iso(nxt),
                "last_fired": st.last_fired,
                "last_status": st.last_status,
                "last_note": st.last_note,
                "last_run_id": st.last_run_id,
            })
        return out

    # ---- 内部 ----

    def _record(self, job: Job, st: JobState, status: str, scheduled: datetime,
                *, note: str = "", run_id: str = "", consume: bool = True) -> bool:
        """记录一次处理结果。返回「`note` 是否变了」，供调用方决定要不要发事件。

        ★ `consume` 是这个模块最要紧的一个开关，含义只有一个：
          **这一次算不算把这个计划时刻处理掉了。**

          · `True`  —— 真的处理过了（已提交出去，或者决定放弃这一次）。
            写 `last_fired`，于是 `due_at()` 不会再为同一时刻返回它。
          · `False` —— **没做成**（知识库没就绪、提交抛异常、缺输入……）。
            **不写 `last_fired`**，下一个 tick 会重新判，只要还在补发窗口内就再试。

        为什么必须区分：`last_fired` 同时被读成「这个时刻处理过了」和「该不该再跑」。
        第一版把「没做成」也写进去，后果是 08:00 那天库晚好一分钟，当天的简报就
        永久没了 —— 而补发窗口本来给了六个小时。

        代价：不消费就意味着失败分支每 30 秒重走一遍，所以调用方**必须**用返回值
        去重，只有 `note` 变了才发事件，否则用户会收到一整天每半分钟一条的提示。
        """
        changed = (st.last_note != note)
        if consume:
            st.last_fired = _iso(scheduled)
        st.last_status = status
        st.last_note = note
        if run_id:
            st.last_run_id = run_id
        self._dirty = True
        return changed

    def _emit(self, payload: dict) -> None:
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(payload)
        except Exception:                                    # noqa: BLE001
            # ★ 回调抛异常不能弄死调度线程（与 `core/task_runner.py` 同一条纪律）
            log.exception("调度事件回调异常")


def _first_error(run: Any) -> str:
    """取第一条节点错误（**按节点在流程里的顺序**，不是随便挑一条）。

    ★ 为什么要按顺序：`{"brief": "模型没配", "report": "写盘失败"}` 这种情形，
      真正的原因是前者；随机挑一条会把人引到次要问题上。
    """
    steps = getattr(run, "steps", None) or {}
    values = steps.values() if hasattr(steps, "values") else []
    for step in values:
        err = str(getattr(step, "error", "") or "").strip()
        if err:
            return err
    return ""


def extract_notice(run: Any, node_id: str, field_name: str = "body") -> str:
    """从跑完的 `Run` 里取要推送的那段文字。取不到返回空串（**不猜**）。"""
    steps = getattr(run, "steps", None) or {}
    step = steps.get(node_id) if hasattr(steps, "get") else None
    out = getattr(step, "output", None)
    if not isinstance(out, dict):
        return ""
    value = out.get(field_name)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(str(x) for x in value).strip()
    return ""


__all__ = [
    "DEFAULT_CITY", "DEFAULT_GRACE_MIN", "DEFAULT_REGION", "DEFAULT_REPEAT_MIN",
    "Job", "JobState", "NOTICE_TIMEOUT", "OPS", "Rule", "Scheduler",
    "TICK_SECONDS", "Trigger", "as_number", "default_jobs", "due_at",
    "evaluate_rules", "extract_notice", "format_alarm", "next_run",
    "parse_clock", "parse_weather", "schedule_dir",
]