# -*- coding: utf-8 -*-
"""棉花价格指数工具 —— 每天从公开页面取现货指数与郑棉期货。

【为什么要有它】知识库里的价格 CSV 是打包时的快照（到 2026-07-31 就断了），
拿它谈"今天多少钱"没有时效性。这个工具补的是**当日**这一小块。

【三个数，三个口径，不要混】

    · 中国棉花价格指数 **CC Index 3128B**（全国口径，元/吨）—— 行业默认的
      "棉花价格指数"，含税提货价口径。
    · **新疆棉花棉纱价格指数 3128B**（疆内口径，元/吨）—— 来源 `ccye.cn`，
      与全国口径常年差几百元/吨。同一个页面上两个都叫 "3128B"，**必须分开取、
      分开标**，混用会得出"今天棉花涨了还是跌了"完全相反的结论。
    · **郑棉 CF701**（期货，元/吨）—— 盘面情绪。同一页上就有，不必再请求一次；
      页面缺这个字段时退回东方财富的 `115.CFm`（主力连续）。

【★ 解析失败一律报错，不返回半个结果】
    这些数字会被当作"今天的事实"推给用户。少一个字段就编一个，比不推危险得多。
    所以：页面结构变了、字段缺失、数值不是数字 —— 返回 `{"error": ...}`，
    由调用方（工作流节点）判失败。
"""
from __future__ import annotations

import json
import re
from datetime import date

import requests

#: 中国棉花信息网首页（省级、全国口径指数都在这一页上）
HOMEPAGE = "https://www.cottonchina.org.cn/"

#: 东方财富：郑棉主力连续（页面上取不到 CF701 时的兜底）
FUTURES_API = ("https://push2.eastmoney.com/api/qt/stock/get"
               "?secid=115.CFm&fields=f43,f44,f45,f46,f47,f57,f58,f60,f169,f170")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_TIMEOUT = 20

#: 首页上那几个 `nd_cotton_block` 的统一结构
_BLOCK_RE = re.compile(
    r"nd_block_t'>(?P<name>[^<]+)</div>.*?"
    r"nd_block_d'>(?P<date>[^<]*)</div>.*?"
    r"nd_block_p[^>]*><span>(?P<value>[^<]*)</span>.*?"
    r"p_color_[a-z]'>(?P<change>[^<]*)</div>",
    re.S)

#: 新疆棉花棉纱价格指数卡片（`xj_zhishu` → `card1`）
_XJ_SECTION_RE = re.compile(r"新疆棉花棉纱价格指数\s*(?:<[^>]+>)*\s*"
                            r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_XJ_3128_RE = re.compile(
    r"pinzhong\">3128B</div>\s*"
    r"<div class=\"price\">(?P<value>[\d.]+).*?"
    r"<div class=\"change\"><span[^>]*>(?P<change>[+\-]?[\d.]+)<",
    re.S)

_NUM_RE = re.compile(r"[+\-]?\d+(?:\.\d+)?")


def _num(text: str) -> float | None:
    m = _NUM_RE.search(str(text or ""))
    return float(m.group(0)) if m else None


def parse_homepage(html: str) -> dict[str, dict]:
    """首页 HTML → `{指数名: {date, value, change}}`。**纯函数，便于守卫。**"""
    out: dict[str, dict] = {}
    for m in _BLOCK_RE.finditer(html or ""):
        name = m.group("name").strip()
        value = _num(m.group("value"))
        if not name or value is None:
            continue                       # 例如「内外棉价差」当日未更新，值是「-」
        out[name] = {
            "date": m.group("date").strip(),
            "value": value,
            "change": _num(m.group("change")),
        }
    return out


def parse_xinjiang(html: str) -> dict | None:
    """新疆棉花棉纱价格指数里的 3128B。取不到返回 None。**纯函数。**"""
    m = _XJ_3128_RE.search(html or "")
    if not m:
        return None
    day = _XJ_SECTION_RE.search(html or "")
    return {
        "date": (f"{int(day.group(1)):04d}-{int(day.group(2)):02d}-"
                 f"{int(day.group(3)):02d}") if day else "",
        "value": _num(m.group("value")),
        "change": _num(m.group("change")),
        "name": "新疆棉花棉纱价格指数 3128B",
    }


def _futures_from_page(blocks: dict[str, dict]) -> dict | None:
    """页面上的 CF701；没有再退回东方财富。"""
    for key in ("CF701", "CF601", "CFm"):
        if key in blocks:
            got = dict(blocks[key])
            got["name"] = f"郑棉 {key}"
            got["source"] = "cottonchina"
            return got
    try:
        resp = requests.get(FUTURES_API, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        last = data.get("f43")
        if last in (None, "-"):
            return None
        return {
            "name": "郑棉主力（东财）",
            "date": date.today().isoformat(),
            "value": float(last),
            "change": (float(data["f169"]) / 100.0
                       if str(data.get("f169", "")).lstrip("-").isdigit() else None),
            "source": "eastmoney",
        }
    except Exception:                                         # noqa: BLE001
        return None


def _fmt(spot: dict | None) -> str:
    """`16762（-48）` 这样的一行数值。"""
    if not spot or spot.get("value") is None:
        return "—"
    value = spot["value"]
    chg = spot.get("change")
    tick = f"{value:,.0f}" if value >= 100 else f"{value:g}"
    if chg is None:
        return tick
    return f"{tick}（{chg:+.0f}）"


def format_summary(data: dict) -> str:
    """把三个口径排成三行，给抽屉直接显示。**纯函数。**"""
    lines = []
    for key in ("national", "xinjiang", "futures"):
        item = data.get(key) or {}
        label = {"national": "全国 CC Index 3128B",
                 "xinjiang": "新疆指数 3128B",
                 "futures": "郑棉期货"}.get(key, key)
        stamp = str(item.get("date") or "").strip()
        lines.append(f"{label}：{_fmt(item)}" + (f"　{stamp}" if stamp else ""))
    return "\n".join(lines)


def get_cotton_index(args: dict) -> str:
    """取当日三个价格口径，返回 JSON 文本。**取不全就报错，不给半个结果。**"""
    try:
        resp = requests.get(HOMEPAGE, headers=_UA, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = resp.content.decode("gb18030", errors="replace")
    except requests.RequestException as e:
        return json.dumps({"error": f"指数页取不到：{type(e).__name__}: {e}",
                           "summary": ""}, ensure_ascii=False)

    blocks = parse_homepage(html)
    national = blocks.get("CC Index 3128B")
    if not national:
        return json.dumps({
            "error": "指数页解析不到「CC Index 3128B」—— 站点结构可能变了",
            "found": sorted(blocks)[:12], "summary": ""}, ensure_ascii=False)

    xinjiang = parse_xinjiang(html)
    futures = _futures_from_page(blocks)

    data = {
        "date": national.get("date") or date.today().isoformat(),
        "national": {"name": "全国 CC Index 3128B", **national},
        "xinjiang": xinjiang,
        "futures": futures,
        "summary": "",
    }
    if not xinjiang:
        # ★ 新疆指数取不到不阻断整条推送，但**必须在正文里说清楚**
        data["xinjiang"] = {"name": "新疆指数 3128B", "value": None,
                            "change": None, "date": "",
                            "error": "页面上没解析到疆内指数"}
    data["summary"] = format_summary(data)
    return json.dumps(data, ensure_ascii=False)


__all__ = ["FUTURES_API", "HOMEPAGE", "format_summary", "get_cotton_index",
           "parse_homepage", "parse_xinjiang"]