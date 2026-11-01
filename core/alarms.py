# -*- coding: utf-8 -*-
"""气象预警信号 —— 直接读中央气象台已发布的官方预警，不自己判等级。

【为什么改成"订阅"而不是"自算"】
  预警的发布权在气象台。之前那套「夜间低温 ≤5℃ / 风力 ≥6 级」的规则，本质是
  用我们自己的阈值去逼近官方的判定 —— 阈值对不对没法保证，报出来的东西也没有
  权威出处。现在改成：**等级、发布单位、时间全部来自官方原文**，我们只做两件事：
  按地区筛出来，再交给模型翻译成农事含义。

【数据从哪来】中央气象台预警列表（网站前端自己用的 REST，非正式开放 API）：

      GET http://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=100
      → {"data":{"page":{"count":804,"list":[{"alertid","issuetime","title","url"}]}}}

  · **没有区域参数**（试过 province / provinceCode / areaCode / stationid，
    全部被忽略），所以只能翻页拉全量、再在本地过滤；
  · 标题形如「广西壮族自治区桂林市全州县气象台发布大风蓝色预警信号」，
    `alertid` 前 6 位是**行政区划代码**（450324 = 全州县）；
  · 全量约 800 条 ≈ 9 页 ≈ 100 KB，每 6 小时一次完全不是负担。

【三条纪律】
  ① **拉不到就说拉不到**：网络失败 / 结构变了 → 返回 `error`，绝不用"没有预警"
     冒充（那是把"不知道"说成"没事"，在灾害场景里最危险）。
  ② **礼貌**：串行请求、页间 0.3 秒、带 UA/Referer、失败即止不重试风暴。
  ③ **短缓存**：10 分钟内复用同一份结果，避免聊天与定时任务同时拉两遍。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("Alarms")

LIST_API = "http://www.nmc.cn/rest/findAlarm"
PAGE_SIZE = 100
MAX_PAGES = 12                    #: 约 1200 条，够任何时刻的全量
PAGE_GAP_S = 0.3                  #: 页间隔（礼貌，不是限流要求）
TIMEOUT_S = 20
CACHE_TTL_S = 600                 #: 10 分钟内复用同一份

# ★ HTTP 头只能装 latin-1：这里**不许出现中文**。
#   第一版写了「农业决策辅助工具」，requests 直接抛 UnicodeEncodeError ——
#   而且它会被下面的兜底当成"结构异常"，把原因指错了地方。
_UA = {"User-Agent": "CottonAgent/0.4 (+weather-alert reader)",
       "Referer": "http://www.nmc.cn/"}

#: 「XX气象台发布YY蓝色预警信号」→ 单位 / 信号 / 等级
_TITLE_RE = re.compile(
    r"^(?P<unit>.+?气象台)发布(?P<signal>.+?)(?P<level>蓝色|黄色|橙色|红色)预警信号$")

#: 等级强弱（用于"升级要重报、降级不重报"）
LEVEL_RANK = {"蓝色": 1, "黄色": 2, "橙色": 3, "红色": 4}


@dataclass
class Alert:
    """一条预警。字段全部来自官方原文，不做加工。"""

    alertid: str
    title: str
    issuetime: str
    url: str = ""
    unit: str = ""              #: 发布单位（区县级气象台）
    signal: str = ""            #: 信号种类，如「大风」
    level: str = ""             #: 蓝 / 黄 / 橙 / 红
    area_code: str = ""         #: alertid 前 6 位（行政区划代码）
    extra: dict = field(default_factory=dict)

    @property
    def rank(self) -> int:
        """等级强弱，未知为 0。"""
        return LEVEL_RANK.get(self.level, 0)

    def to_dict(self) -> dict:
        return {"alertid": self.alertid, "title": self.title,
                "issuetime": self.issuetime, "url": self.url, "unit": self.unit,
                "signal": self.signal, "level": self.level,
                "area_code": self.area_code}

    #: 界面/模型看的中文一行
    def line(self) -> str:
        if self.signal and self.level:
            return f"{self.signal}{self.level}预警（{self.unit}，{self.issuetime}）"
        return f"{self.title}（{self.issuetime}）"


#: 信号种类 → **知识库检索主题词**。
#:
#: ★ 为什么需要这张表：知识库里是**散文**（"大风天不要化控""蕾期遇高温易脱落"），
#:   没有数字。拿"风力 6 级"去检索散文是检索不到的（向量模型也会跑偏），
#:   必须换成**农事动作词**去查，而数字只用来说清"有多大、什么时候"。
#:
#: 键按包含关系匹配：信号名里出现左侧任一词就取对应主题词。
SIGNAL_TOPICS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("大风", "沙尘暴", "沙尘", "扬沙"), "防风 压膜 化控 喷药 沙尘防护"),
    (("高温", "干旱", "干热风"), "高温 滴灌 降温 蕾铃脱落 花粉"),
    (("低温", "霜冻", "寒潮", "道路结冰"), "低温 防霜冻 出苗 抗寒 保全苗"),
    (("暴雨", "暴雪", "冰雹", "雷电", "强对流"), "暴雨 排水 防倒伏 灾后管理"),
    (("大雾", "霾"), "湿度 病害 通风 打药时机"),
)

#: 认不出信号时用的兜底主题词（通用田间管理）
DEFAULT_TOPIC = "田间管理 水肥调控 病虫害防治"


def topic_keywords(signal: str) -> str:
    """信号种类 → 检索主题词。认不出就返回通用词（不返回空）。

    ★ 返回空串会让检索 query 退化成"地区 + 棉花"，命中一堆泛泛的段落；
      给一个通用主题至少保证拿到的资料与田间管理相关。
    """
    text = str(signal or "")
    for keys, topic in SIGNAL_TOPICS:
        if any(k in text for k in keys):
            return topic
    return DEFAULT_TOPIC


def parse_title(title: str) -> dict:
    """标题 → `{unit, signal, level}`。**认不出就返回空字典**（不猜）。

    例：「广西壮族自治区桂林市全州县气象台发布大风蓝色预警信号」
        → 单位=…全州县气象台，信号=大风，等级=蓝色
    """
    m = _TITLE_RE.match(str(title or "").strip())
    if not m:
        return {}
    return {"unit": m.group("unit"), "signal": m.group("signal"),
            "level": m.group("level")}


def area_code_of(alertid: str) -> str:
    """`45032441600000_2026...` → `450324`（行政区划代码，6 位）。"""
    head = str(alertid or "").split("_", 1)[0]
    return head[:6] if len(head) >= 6 and head[:6].isdigit() else ""


# ---------------------------------------------------------------------------
# 拉取（带 10 分钟缓存）
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "alerts": [], "error": ""}


def _fetch_all(*, now: float | None = None) -> tuple[list[Alert], str]:
    """翻页拉全量。返回 `(预警列表, 错误说明)`。**出错时列表为空。**"""
    alerts: list[Alert] = []
    try:
        first = requests.get(LIST_API, params={"pageNo": 1, "pageSize": PAGE_SIZE},
                             headers=_UA, timeout=TIMEOUT_S)
        first.raise_for_status()
        payload = first.json()
        page = ((payload or {}).get("data") or {}).get("page") or {}
        total = int(page.get("count") or 0)
        pages = max(1, min(MAX_PAGES, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        for pno in range(1, pages + 1):
            if pno == 1:
                data = payload
            else:
                time.sleep(PAGE_GAP_S)
                resp = requests.get(LIST_API,
                                    params={"pageNo": pno, "pageSize": PAGE_SIZE},
                                    headers=_UA, timeout=TIMEOUT_S)
                resp.raise_for_status()
                data = resp.json()
            items = (((data or {}).get("data") or {}).get("page") or {}).get("list") or []
            for it in items:
                title = str(it.get("title") or "")
                parsed = parse_title(title)
                alerts.append(Alert(
                    alertid=str(it.get("alertid") or ""),
                    title=title,
                    issuetime=str(it.get("issuetime") or ""),
                    url=str(it.get("url") or ""),
                    area_code=area_code_of(it.get("alertid")),
                    **parsed,
                ))
        return alerts, ""
    except requests.RequestException as e:
        log.warning("预警列表拉取失败：%s: %s", type(e).__name__, e)
        return [], f"预警源不可达：{type(e).__name__}: {e}"
    except Exception as e:                                    # noqa: BLE001
        # 解析失败、结构变化、编码问题都落在这里。**不细分到崩，但要如实带上类型** ——
        # 上一版只捕获 ValueError/TypeError/AttributeError，编码错误被归成"结构异常"，
        # 排查时被指错了方向。
        log.warning("预警列表处理失败：%s: %s", type(e).__name__, e)
        return [], f"预警源返回异常：{type(e).__name__}: {e}"


def fetch_alerts(*, now: float | None = None, refresh: bool = False
                 ) -> tuple[list[Alert], str]:
    """取全量预警（带缓存）。返回 `(列表, 错误说明)`。"""
    stamp = time.time() if now is None else now
    with _cache_lock:
        fresh = (not refresh) and _cache["ts"] and \
                (stamp - _cache["ts"] < CACHE_TTL_S)
        if fresh:
            return list(_cache["alerts"]), _cache["error"]
        alerts, error = _fetch_all(now=stamp)
        # ★ 失败**不覆盖**上一次的好数据，但要把错误带出去 ——
        #   "上次的清单"比"空"有用，可调用方必须知道这次没拉到。
        if alerts:
            _cache["alerts"] = alerts
            _cache["ts"] = stamp
        _cache["error"] = error
        return list(alerts if not error else _cache["alerts"]), error


def clear_cache() -> None:
    """清缓存（守卫与测试用）。"""
    with _cache_lock:
        _cache.update({"ts": 0.0, "alerts": [], "error": ""})


def match(alerts: list[Alert], *, area_hint: str = "",
          area_code: str = "") -> list[Alert]:
    """按地区筛。`area_code` 优先（精确），否则用 `area_hint` 在标题里找。"""
    code = str(area_code or "").strip()
    hint = str(area_hint or "").strip()
    if code:
        return [a for a in alerts if a.area_code.startswith(code)]
    if hint:
        return [a for a in alerts if hint in a.title]
    return list(alerts)


def format_for_model(alerts: list[Alert], *, limit: int = 8) -> str:
    """给模型看的一段文字（等级、单位、时间都来自官方原文）。"""
    if not alerts:
        return "（当前没有生效的预警信号）"
    lines = []
    for a in alerts[:limit]:
        if a.signal and a.level:
            lines.append(f"- {a.unit}：{a.signal}{a.level}预警信号，{a.issuetime} 发布")
        else:
            lines.append(f"- {a.title}，{a.issuetime} 发布")
    if len(alerts) > limit:
        lines.append(f"（另有 {len(alerts) - limit} 条未列出）")
    return "\n".join(lines)


__all__ = ["Alert", "DEFAULT_TOPIC", "LEVEL_RANK", "LIST_API", "SIGNAL_TOPICS",
           "area_code_of", "clear_cache", "fetch_alerts", "format_for_model",
           "match", "parse_title", "topic_keywords"]