# -*- coding: utf-8 -*-
"""天气取数守卫 —— 双源、兜底、以及"别把缺失字段当 0"。

【为什么要守】换源之后有三类错都很隐蔽：

  ① **把 9999 当数据**：nmc 用 `9999` 表示"该时段无数据"（例如当天的白天已过）。
     直接输出会变成"白天温度: 9999°C"。
  ② **日期错位**：nmc 的 `predict.detail` 是按日期索引的数组，按偏移取会错一天；
     这里必须按 `date` 字段匹配。
  ③ **静默换源**：`WEATHER_SOURCE=nmc` 时若偷偷用了高德，用户看到的风力会跟上次
     不一样却查不出原因。

【零网络】所有 HTTP 都是桩。
【用法】python tests/check_weather.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_weather_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from config import AppConfig                                    # noqa: E402
from core import weather                                        # noqa: E402
from core.tools import external                                 # noqa: E402


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


class _Resp:
    def __init__(self, payload) -> None:
        self._p = payload
    def raise_for_status(self) -> None:
        return None
    def json(self):
        return self._p


def _nmc_payload(today_iso: str, tomorrow_iso: str, *, day_sentinel: bool = False):
    """造一份 /rest/weather 的响应（结构照真实返回裁剪）。"""
    return {"msg": "success", "code": 0, "data": {
        "real": {"publish_time": "2026-11-01 20:00",
                 "weather": {"temperature": 13.0, "humidity": 35.0, "rain": 0.0},
                 "wind": {"direct": "东南风", "power": "微风"}},
        "predict": {"publish_time": "2026-11-01 20:00", "detail": [
            {"date": today_iso,
             "day": {"weather": {"info": "9999" if day_sentinel else "晴",
                                 "temperature": "9999" if day_sentinel else "16"},
                     "wind": {"direct": "东风", "power": "微风"}},
             "night": {"weather": {"info": "晴", "temperature": "3"},
                       "wind": {"direct": "北风", "power": "微风"}},
             "precipitation": 0.0},
            {"date": tomorrow_iso,
             "day": {"weather": {"info": "多云", "temperature": "15"},
                     "wind": {"direct": "东北风", "power": "微风"}},
             "night": {"weather": {"info": "多云", "temperature": "3"},
                       "wind": {"direct": "北风", "power": "微风"}},
             "precipitation": 0.0},
        ]}}}


def section_index(c: Checker) -> None:
    c.section("第 1 节：站点索引（地名 → 站点代码）")

    stations = {
        "阿克苏": {"code": "wPpQt", "province": "新疆维吾尔自治区"},
        "温宿": {"code": "oVenj", "province": "新疆维吾尔自治区"},
        "乌什": {"code": "x1", "province": "新疆维吾尔自治区"},
    }
    real_build = weather.build_index
    weather.build_index = lambda **k: {"ts": 9e9, "stations": stations,
                                       "provinces": ["AXJ"]}
    try:
        c.check("精确命中", weather.find_station("阿克苏")[0].get("code") == "wPpQt")
        c.check("★ 「阿克苏地区」靠去后缀命中「阿克苏」",
                weather.find_station("阿克苏地区")[0].get("code") == "wPpQt",
                str(weather.find_station("阿克苏地区")))
        c.check("「温宿县」也能去后缀命中",
                weather.find_station("温宿县")[0].get("code") == "oVenj")
        c.check("★ 不认识的返回空 + 说明（不猜）",
                weather.find_station("红旗坡农场")[0] == {}
                and "没有" in weather.find_station("红旗坡农场")[1],
                weather.find_station("红旗坡农场")[1])
        c.check("空城市名 → 说明原因", weather.find_station("")[1] == "城市名为空")
    finally:
        weather.build_index = real_build

    c.check("索引文件名固定（便于排查与清理）",
            weather.index_path().name == "weather_stations.json",
            weather.index_path().name)


def section_nmc(c: Checker) -> None:
    c.section("第 2 节：★ 中央气象台取数（降水 9999 处理与日期匹配）")

    today = date.today()
    tomorrow = today + timedelta(days=1)
    ok_payload = _nmc_payload(today.isoformat(), tomorrow.isoformat())
    calls: list = []

    real_get = weather.requests.get
    real_find = weather.find_station

    def _fake_get(url, params=None, headers=None, timeout=None, **k):
        calls.append((url, dict(params or {})))
        return _Resp(ok_payload)

    weather.requests.get = _fake_get
    weather.find_station = lambda city: ({"code": "wPpQt"}, "")
    try:
        text, why = weather.nmc_text("阿克苏", "tomorrow")
        c.check("★ 明日取到，且带降水量",
                why == "" and f"日期: {tomorrow.isoformat()}" in text
                and "降水量: 0.0 mm" in text, why or text[:80])
        c.check("明日是「多云 15°C」（按日期匹配，不是按偏移）",
                "白天天气: 多云" in text and "白天温度: 15°C" in text, text)
        c.check("明日不附带今天的实况",
                "实况:" not in text, text)
        c.check("请求打到 /rest/weather 且带 stationid",
                calls and calls[-1][0] == weather.WEATHER_API
                and calls[-1][1].get("stationid") == "wPpQt", str(calls[-1:]))

        text_today, _ = weather.nmc_text("阿克苏", "today")
        c.check("今天取到实况（温度/湿度/降水）",
                "实况:" in text_today and "湿度 35.0%" in text_today, text_today[:100])

        text_iso, _ = weather.nmc_text("阿克苏", tomorrow.isoformat())
        c.check("支持 YYYY-MM-DD",
                f"日期: {tomorrow.isoformat()}" in text_iso, text_iso[:60])

        # ★ 9999 = 该时段无数据，不能当数值输出
        sentinel = _nmc_payload(today.isoformat(), tomorrow.isoformat(),
                                day_sentinel=True)
        weather.requests.get = lambda *a, **k: _Resp(sentinel)
        text2, _ = weather.nmc_text("阿克苏", "today")
        c.check("★ 白天字段是 9999（当天的白天已过）→ 整行省略，不输出 9999",
                "9999" not in text2 and "夜间天气: 晴" in text2, text2[:120])

        # ★ 实况里的风向是 9999（缺失）→ 只保留风力，不能输出 9999
        sentinel2 = _nmc_payload(today.isoformat(), tomorrow.isoformat())
        sentinel2["data"]["real"]["wind"] = {"direct": "9999", "power": "微风"}
        weather.requests.get = lambda *a, **k: _Resp(sentinel2)
        text_w, _ = weather.nmc_text("阿克苏", "today")
        c.check("★ 实况风向为 9999 → 不输出 9999，只留风力",
                "9999" not in text_w and "微风" in text_w, text_w[:120])

        # 超出预报天数
        far = (today + timedelta(days=30)).isoformat()
        text3, why3 = weather.nmc_text("阿克苏", far)
        c.check("★ 超出预报天数 → 明确报错（不返回空文本冒充成功）",
                text3 == "" and "没有" in why3, why3)

        # 网络失败
        def boom(*a, **k):
            raise weather.requests.RequestException("超时")
        weather.requests.get = boom
        text4, why4 = weather.nmc_text("阿克苏", "today")
        c.check("★ 网络失败 → 返回错误说明",
                text4 == "" and "失败" in why4, why4)
    finally:
        weather.requests.get = real_get
        weather.find_station = real_find


def section_daily(c: Checker) -> None:
    c.section("第 2 节（续）：★ 多日紧凑行（简报用的客观块）")

    today = date.today()
    # ★ 故意让 detail 从**昨天**开始：凌晨没翻页时气象台就是这样，
    #   按数组下标贴标签会把"明天"标成"今日"。
    y = (today - timedelta(days=1)).isoformat()
    detail = [
        {"date": y, "day": {"weather": {"info": "晴", "temperature": "14"},
                            "wind": {"direct": "东风", "power": "微风"}},
         "night": {"weather": {"info": "晴", "temperature": "2"},
                   "wind": {"direct": "东风", "power": "微风"}},
         "precipitation": 0.0},
        {"date": today.isoformat(),
         "day": {"weather": {"info": "多云", "temperature": "15"},
                 "wind": {"direct": "东北风", "power": "微风"}},
         "night": {"weather": {"info": "多云", "temperature": "3"},
                   "wind": {"direct": "北风", "power": "微风"}},
         "precipitation": 0.0},
        {"date": (today + timedelta(days=1)).isoformat(),
         "day": {"weather": {"info": "晴", "temperature": "16"},
                 "wind": {"direct": "北风", "power": "微风"}},
         "night": {"weather": {"info": "晴", "temperature": "1"},
                   "wind": {"direct": "北风", "power": "微风"}},
         "precipitation": 2.5},
        {"date": (today + timedelta(days=2)).isoformat(),
         "day": {"weather": {"info": "阴", "temperature": "12"},
                 "wind": {"direct": "西风", "power": "3-4级"}},
         "night": {"weather": {"info": "阴", "temperature": "0"},
                   "wind": {"direct": "西风", "power": "3-4级"}},
         "precipitation": 0.0},
    ]
    payload = {"data": {"real": {"publish_time": "2026-11-02 00:30",
                                 "weather": {"temperature": 4.0, "humidity": 81.0},
                                 "wind": {"direct": "东北风", "power": "微风"}},
                        "predict": {"detail": detail}}}

    real_get = weather.requests.get
    real_find = weather.find_station
    weather.requests.get = lambda *a, **k: _Resp(payload)
    weather.find_station = lambda city: ({"code": "wPpQt"}, "")
    try:
        lines, why = weather.daily_lines("阿克苏", 3)
    finally:
        weather.requests.get = real_get
        weather.find_station = real_find

    c.check("★ 三天三行", why == "" and len(lines) == 3, f"{why} / {len(lines)}")
    c.check("★ 标签按日期差算：昨天那条被丢掉、今天排第一",
            lines[0].startswith("【今日】") and "多云" in lines[0], lines[0])
    c.check("★ 明日/后天都标对了",
            lines[1].startswith("【明日】") and lines[2].startswith("【后天】"),
            str(lines[1:]))
    c.check("★ 实况只挂在今日那一行",
            "实况" in lines[0] and "实况" not in lines[1] and "实况" not in lines[2],
            str(lines))
    c.check("一天一行：温度用「夜~昼」",
            "3~15℃" in lines[0], lines[0])
    c.check("有降水时写毫米数、无降水写「无降水」",
            "降水 2.5mm" in lines[1] and "无降水" in lines[0], str(lines[:2]))
    c.check("★ 一行里不带 9999 之类的哨兵值",
            not any("9999" in x for x in lines))


def section_amap(c: Checker) -> None:
    c.section("第 3 节：高德兜底（地名能认、字段少）")

    today = date.today()
    real_get = weather.requests.get
    key = AppConfig.AMAP_API_KEY
    AppConfig.AMAP_API_KEY = "test-key"

    live = {"status": "1", "lives": [{
        "city": "阿克苏地区", "weather": "多云", "temperature": "13",
        "winddirection": "东南", "windpower": "≤3", "humidity": "35",
        "reporttime": "2026-11-01 20:00"}]}
    casts = {"status": "1", "forecasts": [{"city": "阿克苏地区", "casts": [
        {"date": today.isoformat(), "dayweather": "多云", "nightweather": "晴",
         "daytemp": "16", "nighttemp": "3", "daypower": "1-3", "nightpower": "1-3"},
        {"date": (today + timedelta(days=1)).isoformat(),
         "dayweather": "晴", "nightweather": "晴",
         "daytemp": "17", "nighttemp": "4", "daypower": "1-3", "nightpower": "1-3"},
    ]}]}

    try:
        weather.requests.get = lambda url, params=None, **k: _Resp(
            live if (params or {}).get("extensions") == "base" else casts)
        text, why = weather.amap_text("阿克苏地区", "today")
        c.check("今天 → 走实况接口",
                why == "" and "温度: 13°C" in text, why or text[:60])
        text2, _ = weather.amap_text("阿克苏地区", "tomorrow")
        c.check("明天 → 走预报接口", "白天温度: 17°C" in text2, text2[:80])
        text3, why3 = weather.amap_text("阿克苏地区", (today + timedelta(days=9)).isoformat())
        c.check("★ 超出 4 天 → 报错", text3 == "" and "4 天" in why3, why3)
        text4, why4 = weather.amap_text("阿克苏地区", "不是日期")
        c.check("日期格式错 → 报错", text4 == "" and "格式" in why4, why4)
    finally:
        weather.requests.get = real_get
        AppConfig.AMAP_API_KEY = key

    AppConfig.AMAP_API_KEY = ""
    try:
        _, why5 = weather.amap_text("阿克苏", "today")
        c.check("没配 Key → 明确说没配", "未配置" in why5, why5)
    finally:
        AppConfig.AMAP_API_KEY = key


def section_dispatch(c: Checker) -> None:
    c.section("第 4 节：★ 选源与兜底（不静默换源）")

    real_nmc, real_amap = weather.nmc_text, weather.amap_text
    mode = AppConfig.WEATHER_SOURCE
    weather.nmc_text = lambda city, d="today", **k: ("NMC_TEXT", "")
    weather.amap_text = lambda city, d="today", **k: ("AMAP_TEXT", "")
    try:
        AppConfig.WEATHER_SOURCE = "nmc"
        c.check("源=nmc → 只用 nmc", weather.fetch("x")[0] == "NMC_TEXT")
        AppConfig.WEATHER_SOURCE = "amap"
        c.check("源=amap → 只用 amap", weather.fetch("x")[0] == "AMAP_TEXT")
        AppConfig.WEATHER_SOURCE = "auto"
        c.check("auto → 优先 nmc", weather.fetch("x")[0] == "NMC_TEXT")

        # ★ 指定 nmc 时即便它失败，也不许偷偷用高德
        weather.nmc_text = lambda city, d="today", **k: ("", "索引里没有")
        AppConfig.WEATHER_SOURCE = "nmc"
        text, why = weather.fetch("红旗坡农场")
        c.check("★ 源=nmc 且失败 → 报错，**不偷偷换高德**",
                text == "" and "索引里没有" in why, why)

        AppConfig.WEATHER_SOURCE = "auto"
        text2, why2 = weather.fetch("红旗坡农场")
        c.check("★ auto 下 nmc 失败 → 退高德并成功",
                text2 == "AMAP_TEXT", why2)

        weather.amap_text = lambda city, d="today", **k: ("", "高德也没返回")
        text3, why3 = weather.fetch("红旗坡农场")
        c.check("两个源都失败 → 一条说明里带两家原因",
                text3 == "" and "nmc" in why3 and "高德" in why3, why3)
    finally:
        weather.nmc_text, weather.amap_text = real_nmc, real_amap
        AppConfig.WEATHER_SOURCE = mode


def section_tool(c: Checker) -> None:
    c.section("第 5 节：工具封装与配置登记")

    real_fetch = weather.fetch
    weather.fetch = lambda city, d="today", **k: ("城市: 阿克苏\n日期: 明天", "")
    try:
        out = external.get_weather({"city": "阿克苏", "date": "tomorrow"})
        c.check("工具透传取数结果", "城市: 阿克苏" in out, out)
    finally:
        weather.fetch = real_fetch

    weather.fetch = lambda city, d="today", **k: ("", "站点索引里没有「x」")
    try:
        out = external.get_weather({"city": "x"})
        c.check("★ 取不到 → 「天气查询失败: 原因」",
                out.startswith("天气查询失败") and "没有" in out, out)
    finally:
        weather.fetch = real_fetch

    c.check("空城市名 → 直接报错，不取数",
            external.get_weather({"city": "  "}).startswith("天气查询失败"))
    c.check("`WEATHER_SOURCE` 在 AppConfig 上",
            hasattr(AppConfig, "WEATHER_SOURCE"), "")
    import config as cfgmod
    c.check("★ `WEATHER_SOURCE` 在热更新键表里（改完即生效）",
            "WEATHER_SOURCE" in cfgmod._RUNTIME_KEYS, "")


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_index(c)
        section_nmc(c)
        section_daily(c)
        section_amap(c)
        section_dispatch(c)
        section_tool(c)
    finally:
        print()
        print("=" * 78)
        print(f"天气取数守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())