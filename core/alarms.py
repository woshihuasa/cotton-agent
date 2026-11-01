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


#: 行政区划后缀（取最后一个作为称呼，如「广西…桂林市全州县」→「全州县」）
#: 强边界：出现这些就说明前面是更高一级的行政区
_STRONG_BOUNDARY_RE = re.compile(r"自治区|地区|省|市")
#: 「XX气象台1日22时32分发布大风蓝色预警信号：」这一截
_LEAD_RE = re.compile(r"^.*?预警信号\s*[：:]\s*")

_WINDOW_RE = re.compile(r"未来\s*(\d+)\s*(小时|分钟|天)")


def short_area(unit: str) -> str:
    """「广西壮族自治区桂林市全州县气象台」→「全州县」。

    ★ 只取最后一级行政区：用户要的是"哪里"，不是"省级+市级+县级"的完整链条。
      认不出就返回去掉"气象台"之后的全串（不硬猜）。
    """
    text = str(unit or "").strip()
    if not text:
        return ""
    base = text[:-3] if text.endswith("气象台") else text
    # ★ 只按**强边界**（自治区/地区/省/市）切，取最后一段。
    #   为什么不去切 县/区/旗/州：像「全州县」这种，"州" 是名字的一部分而不是边界，
    #   按后缀逐个切会切出孤零零一个「县」。强边界不存在时，整串就是最后一级。
    strong = list(_STRONG_BOUNDARY_RE.finditer(base))
    if not strong:
        return base
    cut = strong[-1].end()
    tail = base[cut:]
    if tail:
        return tail
    prev = strong[-2].end() if len(strong) > 1 else 0
    return base[prev:cut]


#: 乡镇级地名的连续列举：「赤水镇、甘竹镇、…、长桥乡」
#: ★ 必须**从句读边界开始**（前面是逗号/分号/冒号）。
#:   松弛地匹配会把「未来6小时内甲镇」里的「内甲镇」也吃进去，替换后变成
#:   「未来6全市将出现」—— 这种"吞掉半边句子"的错，比多留几个乡镇名糟得多。
_TOWNS_RE = re.compile(
    r"(?<=[，。；：、\s])[\u4e00-\u9fa5]{2,8}(?:镇|乡|街道|苏木|团场|农场|村)"
    r"(?:、[\u4e00-\u9fa5]{2,8}(?:镇|乡|街道|苏木|团场|农场|村))+")

#: 单个乡镇名（不在列举里时只保留区县一级也够用）
_ONE_TOWN_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}(?:镇|乡|街道|苏木|团场|农场|村)")


#: 句首时效：「预计未来6小时内」「48小时内」
_TIME_LEAD_RE = re.compile(r"^(?:预计)?\s*(?:未来)?\s*\d+\s*(?:小时|分钟|天)\s*内?")

#: 天气词与谓词：用来在"地名清单 + 天气描述"混在一起时找到天气那一半
_WEATHER_WORDS: tuple[str, ...] = (
    "阵风", "平均风", "风力", "大风", "气温", "温度", "降温", "升温", "高温", "低温",
    "降雨", "雨量", "降水", "暴雨", "降雪", "积雪", "能见度", "雷电", "冰雹",
    "沙尘", "寒潮", "冷空气", "霜冻", "大雾", "霾",
    "将出现", "可出现", "可达", "达到", "伴有",
)


def _strip_place_prefix(seg: str) -> str:
    """一句里若混着地名清单，就从第一个天气词开始留。

    例：「赤水镇、甘竹镇……高海拔山区阵风可达7-8级」→「阵风可达7-8级」
        「受冷空气南下影响」→ 原样（没有地名清单，且本身就是天气）
    """
    text = str(seg or "").strip()
    if not text:
        return ""
    # ★ 单个乡镇也照样去掉：分辨率统一到区县一级，不做"有的句子带镇名、
    #   有的不带"的半吊子状态。
    has_list = (bool(_TOWNS_RE.search(text)) or bool(_ONE_TOWN_RE.search(text))
                or ("、" in text))
    if not has_list:
        return text
    # ★ 先保住句首的时效（"预计未来6小时内"），它属于天气信息，不是地名。
    #   没有这一步，"预计未来6小时内城关镇降雨量将达50毫米" 会被截成
    #   "降雨量将达50毫米"，把最重要的"什么时候"丢掉了。
    lead = ""
    m = _TIME_LEAD_RE.match(text)
    if m:
        lead = m.group(0)
        text = text[m.end():].lstrip("，、 ")
    positions = [text.find(w) for w in _WEATHER_WORDS if text.find(w) >= 0]
    if not positions:
        return lead                     # 剩下全是地名 → 只留时效
    cut = min(positions)
    return lead + text[cut:]


def clean_brief(body: str, area: str = "") -> str:
    """详情正文 → 一句能直接读的话。

    例：
      「广昌县气象台2026年11月01日22时39分发布大风蓝色预警信号：预计未来24小时内，
        受冷空气南下影响，……阵风可达7-8级，请注意防范。」
      →「广昌县预计未来24小时内，受冷空气南下影响，……阵风可达7-8级，请注意防范。」

    ★ 去掉的是"发布单位 + 发布时间 + 信号名"这一截**已经出现在标题里**的信息；
      正文的定量描述一个字不动（那是唯一有信息量的部分）。
    """
    text = str(body or "").strip()
    if not text:
        return ""
    m = _LEAD_RE.match(text)
    desc = text[m.end():].strip() if m else text
    if not desc:
        desc = text
    # ★ 地名清单**整段删掉**，不要用「全县」之类的词去顶替 —— 用户要的是天气本身。
    #   做法：按逗号分句，凡含乡镇/地形列举的那一句，从**第一个天气词**起截取
    #   （"赤水镇、甘竹镇……山区阵风可达7-8级" → "阵风可达7-8级"）。
    #   纯天气的句子（"受冷空气南下影响"）原样保留。
    segs = []
    for seg in desc.split("，"):
        segs.append(_strip_place_prefix(seg.strip()))
    desc = "，".join(x for x in segs if x)
    desc = re.sub(r"[，、]{2,}", "，", desc).strip("，、 ")
    # 「我县/本县」→ 去掉，前面已经带上地名
    for word in ("我县", "本县", "我区", "本区", "我市", "本市"):
        desc = desc.replace(word, "")
    return f"{area}{desc}" if area else desc

_TAG_RE = re.compile(r"<[^>]+>")


def parse_detail(html: str) -> dict:
    """预警详情页 → `{body, defense, window}`。

    ★ 为什么要抓详情：列表接口只给标题（"大风蓝色预警信号"），**定量的时效与量级
      全在详情正文里** —— 「预计未来 24 小时内我县将出现 6 级（或阵风 7 级）以上大风」。
      没有这一段，就只剩一个等级标签，说不清"多大、多久"。

    ★ 时效**必须从正文读**，不能预设：大风蓝色是 24 小时，雷雨大风黄色是 6 小时，
      各信号不同。写死"未来 6 小时"会张冠李戴。
    """
    if not html:
        return {"body": "", "defense": "", "window": ""}
    text = re.sub(r"(?s)<script.*?</script>", " ", html)
    text = re.sub(r"(?s)<style.*?</style>", " ", text)
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
    text = re.sub(r"\s+", " ", text).strip()

    # ★ 正文从**最后一个「X气象台」**开始截 —— 页面头部还有导航与发布日期，
    #   直接从"发布"起截会把"进入 恭城 天气预报"这类导航文字一起带进来。
    cut = text.find("防御指南")
    head = text[:cut] if cut > 0 else text
    start = -1
    for mm in re.finditer(r"[\u4e00-\u9fa5]{2,12}气象台", head):
        start = mm.start()
    body = head[start:].strip() if start >= 0 else ""
    if not body:
        i = text.find("预警信号")
        body = text[i:].strip() if i >= 0 else ""

    defense = ""
    d = re.search(r"防御指南[::]?\s*(.+?)(?:推荐服务|$)", text)
    if d:
        defense = d.group(1).lstrip("：: ").strip()[:600]

    w = _WINDOW_RE.search(body or text)
    window = f"{w.group(1)}{w.group(2)}" if w else ""
    return {"body": body[:800], "defense": defense, "window": window}


def fetch_detail(alert: Alert, *, timeout_s: float = 15.0) -> dict:
    """取一条预警的详情（**只取被选中的那一条**，不做批量）。失败返回空字典。"""
    path = str(getattr(alert, "url", "") or "").strip()
    if not path:
        return {}
    url = path if path.startswith("http") else "http://www.nmc.cn" + path
    try:
        resp = requests.get(url, headers=_UA, timeout=timeout_s)
        resp.raise_for_status()
        # ★ 这个站点不返回 charset，`resp.text` 会按 latin-1 解出乱码，必须自己解。
        html = resp.content.decode("utf-8", errors="replace")
        got = parse_detail(html)
        got["url"] = url
        return got
    except Exception as e:                                    # noqa: BLE001
        log.warning("预警详情取不到（%s）：%s: %s", url, type(e).__name__, e)
        return {}


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
           "clean_brief", "fetch_detail", "match", "parse_detail", "parse_title",
           "short_area",
           "topic_keywords"]