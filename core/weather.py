# -*- coding: utf-8 -*-
"""天气取数 —— **中央气象台（nmc）为主，高德兜底**。

【为什么要换主源】同一座城市、同一天，两个源给的东西差一整截：

    高德：白天/夜间天气、温度、风力等级（4 天，**没有降水量**）
    nmc ：上面这些 + **降水量** + 湿度 + 实况 + 7 天 + 更新时间

  对农事判断来说降水量是决定性的（滴灌要不要停、雨后要不要补、干旱成不成立），
  高德完全给不了。而且预警已经走中央气象台，天气同源之后不会出现
  "高德说 1-3 级、气象台发大风预警"这种自相矛盾的组合。

【为什么不能只用 nmc】nmc 只认**县级站点**（全国约 2000~3000 个）。
  用户在设置里填的是自由文本（"阿克苏红旗坡农场"这种它也认不出），
  这时必须退回高德 —— 它的地名解析能力强得多。

【口径】`WEATHER_SOURCE`
    · `auto`（默认）：站点索引里找得到就用 nmc，找不到退高德；
    · `nmc` / `amap`：只用指定源（失败就**如实报错**，不偷偷换源 ——
      静默换源会让"为什么这次的风力和上次不一样"变成无头案）。

【站点索引】`app_data_dir()/weather_stations.json`，
    `城市名 → {code, province}`，30 天有效。
    构建方式：省级列表（34 条）→ 逐省拉区县（每省 1 次请求，边拉边存，可续建）。
    全国建一次约 10 秒，之后永久命中缓存。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import date
from pathlib import Path

import requests

log = logging.getLogger("Weather")

PROVINCE_API = "http://www.nmc.cn/rest/province"
CITY_API = "http://www.nmc.cn/rest/province/{code}"
WEATHER_API = "http://www.nmc.cn/rest/weather"
AMAP_API = "https://restapi.amap.com/v3/weather/weatherInfo"

#: 索引有效期（秒）：30 天。站点增减很慢，没必要每次重建。
INDEX_TTL_S = 30 * 24 * 3600

TIMEOUT_S = 15

_UA = {"User-Agent": "CottonAgent/0.4 (+weather reader)",
       "Referer": "http://www.nmc.cn/"}


# ---------------------------------------------------------------------------
# 站点索引
# ---------------------------------------------------------------------------

def index_path() -> Path:
    from config import app_data_dir
    return Path(app_data_dir()) / "weather_stations.json"


def _read_index() -> dict:
    try:
        p = index_path()
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                         # noqa: BLE001
        return {}


def _write_index(payload: dict) -> None:
    p = index_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".st_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError as e:
        log.warning("站点索引写不进去：%s", e)


def clear_index() -> None:
    """清空索引（守卫与"重建"用）。"""
    try:
        index_path().unlink(missing_ok=True)
    except OSError:
        pass


def _index_fresh(data: dict) -> bool:
    try:
        return (time.time() - float(data.get("ts") or 0)) < INDEX_TTL_S \
            and bool(data.get("stations"))
    except (TypeError, ValueError):
        return False


def build_index(*, budget_s: float = 25.0) -> dict:
    """拉全量站点索引（**边拉边存**，中断下次可续）。

    返回 `{"ts": 时间戳, "stations": {城市名: {code, province}}, "provinces": [...]}`。
    任何一步失败都保留已完成的部分 —— 半个索引也比没有强。
    """
    data = _read_index()
    if _index_fresh(data):
        return data
    stations: dict[str, dict] = dict(data.get("stations") or {})
    done: list[str] = list(data.get("provinces") or [])
    started = time.time()

    try:
        provinces = requests.get(PROVINCE_API, headers=_UA,
                                 timeout=TIMEOUT_S).json()
    except Exception as e:                                    # noqa: BLE001
        log.warning("省级列表取不到：%s: %s", type(e).__name__, e)
        return data or {"ts": 0, "stations": {}, "provinces": []}

    # ★ 串行拉 34 个省要 20 秒以上（实测 24 秒），放进聊天工具调用就是"卡住"。
    #   并发 6 路后约 4 秒 —— 这些是静态列表接口，并发不会给站点压力。
    todo = [str((p or {}).get("code") or "").strip()
            for p in (provinces if isinstance(provinces, list) else [])]
    todo = [c for c in todo if c and c not in done]

    def _one(code: str) -> tuple[str, list]:
        try:
            cities = requests.get(CITY_API.format(code=code), headers=_UA,
                                  timeout=TIMEOUT_S).json()
            return code, cities if isinstance(cities, list) else []
        except Exception as e:                                # noqa: BLE001
            log.warning("省份 %s 的区县取不到：%s", code, e)
            return code, []

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        for code, cities in pool.map(_one, todo):
            for c in cities:
                name = str((c or {}).get("city") or "").strip()
                sid = str((c or {}).get("code") or "").strip()
                if name and sid:
                    stations[name] = {
                        "code": sid,
                        "province": str((c or {}).get("province") or "")}
            done.append(code)
            if time.time() - started > budget_s:
                log.info("站点索引建到一半（%d 省），下次继续", len(done))
                break
    _write_index({"ts": time.time(), "stations": stations, "provinces": done})

    _write_index({"ts": time.time(), "stations": stations, "provinces": done})
    return {"ts": time.time(), "stations": stations, "provinces": done}


def find_station(city: str) -> tuple[dict, str]:
    """城市名 → `({code, province}, 说明)`。找不到时第一项为空字典。

    ★ 先精确匹配，再退"包含"匹配（用户写「阿克苏地区」，站点叫「阿克苏」）——
      但反过来的包含**不做**（站点叫「阿克苏」时，不能因为用户写「阿」就命中）。
    """
    name = str(city or "").strip()
    if not name:
        return {}, "城市名为空"
    data = build_index()
    stations = data.get("stations") or {}
    if name in stations:
        return stations[name], ""
    # 「阿克苏地区」→ 「阿克苏」：去掉常见行政后缀再试
    for suffix in ("地区", "市", "县", "区", "州", "旗"):
        if name.endswith(suffix) and name[:-len(suffix)] in stations:
            return stations[name[:-len(suffix)]], ""
    hits = [(k, v) for k, v in stations.items() if k and k in name]
    if len(hits) == 1:
        return hits[0][1], ""
    if hits:
        return {}, f"「{name}」对应多个气象站：{'、'.join(k for k, _ in hits[:5])}"
    return {}, f"站点索引里没有「{name}」"


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def _target_date(date_str: str) -> tuple[date | None, str]:
    """`today` / `tomorrow` / `YYYY-MM-DD` → `(date, 错误说明)`。"""
    raw = str(date_str or "today").strip()
    today = date.today()
    if raw in ("today", "", "现在", "今天"):
        return today, ""
    if raw in ("tomorrow", "明天"):
        return today.fromordinal(today.toordinal() + 1), ""
    try:
        return date.fromisoformat(raw), ""
    except ValueError:
        return None, f"日期格式错误 [{raw}]，请用 YYYY-MM-DD / today / tomorrow"


def _valid(value) -> str:
    """nmc 用 `9999` 表示"该字段无数据"。**统一在出口处挡掉**，绝不输出 9999。"""
    text = str(value if value is not None else "").strip()
    if not text or text in ("9999", "9999.0", "None", "nan"):
        return ""
    if text.replace(".", "", 1).lstrip("-").isdigit() and float(text) == 9999.0:
        return ""
    return text


def nmc_text(city: str, date_str: str = "today") -> tuple[str, str]:
    """从中央气象台取。返回 `(文本, 错误说明)`；成功时错误为空串。"""
    station, why = find_station(city)
    if not station:
        return "", why
    target, err = _target_date(date_str)
    if target is None:
        return "", err
    try:
        data = requests.get(WEATHER_API, params={"stationid": station["code"]},
                            headers=_UA, timeout=TIMEOUT_S).json().get("data") or {}
    except Exception as e:                                    # noqa: BLE001
        return "", f"气象台取数失败：{type(e).__name__}: {e}"

    payload = data.get("predict") or {}
    detail = payload.get("detail") or []
    day = next((d for d in detail if str(d.get("date")) == target.isoformat()), None)
    if day is None:
        return "", (f"气象台预报只到 {len(detail)} 天，没有 {target.isoformat()} 的数据")

    def _side(side: str, label: str) -> str:
        info = (day.get(side) or {}).get("weather") or {}
        wind = (day.get(side) or {}).get("wind") or {}
        text, temp = _valid(info.get("info")), _valid(info.get("temperature"))
        if not text and not temp:
            return ""                    # 该时段没数据（例如当天的白天已过）
        bits = []
        if text:
            bits.append(f"{label}天气: {text}")
        if temp:
            bits.append(f"{label}温度: {temp}°C")
        direct, power = _valid(wind.get("direct")), _valid(wind.get("power"))
        if direct or power:
            bits.append(f"{label}风力: {(direct + ' ' + power).strip()}")
        return "\n".join(bits)

    lines = [f"城市: {city}", f"日期: {target.isoformat()}"]
    for side, label in (("day", "白天"), ("night", "夜间")):
        got = _side(side, label)
        if got:
            lines.append(got)
    precip = _valid(day.get("precipitation"))
    if precip:
        lines.append(f"降水量: {precip} mm")

    real = (data.get("real") or {})
    if target == date.today() and real:
        w = real.get("weather") or {}
        bits = []
        if _valid(w.get("temperature")):
            bits.append(f"{_valid(w.get('temperature'))}°C")
        if _valid(w.get("humidity")):
            bits.append(f"湿度 {w['humidity']}%")
        if _valid(w.get("rain")):
            bits.append(f"降水 {w['rain']}mm")
        wind = real.get("wind") or {}
        direct, power = _valid(wind.get("direct")), _valid(wind.get("power"))
        if direct or power:
            bits.append((direct + " " + power).strip())
        if bits:
            lines.append("实况: " + "，".join(bits))
    if payload.get("publish_time"):
        lines.append(f"更新时间: {payload['publish_time']}")

    text = "\n".join(lines)
    return (text, "") if len(lines) > 2 else ("", "气象台没有返回可用的天气字段")


def amap_text(city: str, date_str: str = "today") -> tuple[str, str]:
    """从高德取（地名解析强，字段少）。返回 `(文本, 错误说明)`。"""
    from config import AppConfig
    api_key = AppConfig.AMAP_API_KEY
    if not api_key:
        return "", "未配置高德地图 API Key"
    target, err = _target_date(date_str)
    if target is None:
        return "", err
    offset = (target - date.today()).days
    if offset < 0:
        return "", f"无法查询过去的日期 [{date_str}]"
    try:
        if offset == 0:
            resp = requests.get(AMAP_API, timeout=10,
                                params={"key": api_key, "city": city,
                                        "extensions": "base"})
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "1":
                return "", f"高德返回 {data.get('info', '未知错误')}"
            lives = data.get("lives") or []
            if not lives:
                return "", f"未找到城市 [{city}] 的天气数据"
            w = lives[0]
            return ("\n".join([
                f"城市: {w.get('city', city)}",
                f"天气: {w.get('weather', '未知')}",
                f"温度: {w.get('temperature', '未知')}°C",
                f"风向: {w.get('winddirection', '未知')} 风力: {w.get('windpower', '未知')}级",
                f"湿度: {w.get('humidity', '未知')}%",
                f"发布时间: {w.get('reporttime', '未知')}",
            ]), "")
        if offset > 3:
            return "", (f"高德只支持今天起 4 天内，{date_str} 超出范围")
        resp = requests.get(AMAP_API, timeout=10,
                            params={"key": api_key, "city": city,
                                    "extensions": "all"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "1":
            return "", f"高德返回 {data.get('info', '未知错误')}"
        casts = ((data.get("forecasts") or [{}])[0] or {}).get("casts") or []
        if offset >= len(casts):
            return "", f"高德未返回 {date_str} 的预报"
        t = casts[offset]
        return ("\n".join([
            f"城市: {data['forecasts'][0].get('city', city)}",
            f"日期: {t.get('date', date_str)}",
            f"白天天气: {t.get('dayweather', '未知')}",
            f"夜间天气: {t.get('nightweather', '未知')}",
            f"白天温度: {t.get('daytemp', '未知')}°C",
            f"夜间温度: {t.get('nighttemp', '未知')}°C",
            f"白天风力: {t.get('daypower', '未知')}级",
            f"夜间风力: {t.get('nightpower', '未知')}级",
        ]), "")
    except requests.RequestException as e:
        return "", f"高德网络错误 - {e}"
    except Exception as e:                                    # noqa: BLE001
        return "", f"高德数据解析错误 - {e}"


_DAY_LABELS = ("今日", "明日", "后天", "第4天", "第5天", "第6天", "第7天")


def _compact_line(label: str, day: dict, *, real_text: str = "") -> str:
    """一天压成一行：`【明日】晴，4~17℃，东风微风，无降水`。

    ★ 为什么要压成一行：简报里三天要占三行，分白天/夜间写就是六行，
      抽屉里那点空间一屏放不下。判读需要的是"冷暖、风、有没有雨"。
    """
    def _side(name: str) -> dict:
        return (day.get(name) or {})

    night = _side("night")
    dayp = _side("day")
    n_info, n_temp = _valid((night.get("weather") or {}).get("info")), \
        _valid((night.get("weather") or {}).get("temperature"))
    d_info, d_temp = _valid((dayp.get("weather") or {}).get("info")), \
        _valid((dayp.get("weather") or {}).get("temperature"))
    info = d_info or n_info or "—"
    low, high = n_temp, d_temp
    temp = f"{low}~{high}℃" if (low and high) else (f"{high or low}℃" if (low or high) else "")
    wind = _side("day").get("wind") or _side("night").get("wind") or {}
    w_direct, w_power = _valid(wind.get("direct")), _valid(wind.get("power"))
    wind_text = (w_direct + w_power) if (w_direct and w_power) else (w_direct or w_power or "")
    precip = _valid(day.get("precipitation"))
    try:
        rain = "无降水" if (precip and float(precip) == 0.0) else (f"降水 {precip}mm" if precip else "")
    except (TypeError, ValueError):
        rain = f"降水 {precip}mm" if precip else ""
    bits = [f"【{label}】{info}"]
    if temp:
        bits.append(temp)
    if wind_text:
        bits.append(wind_text)
    if rain:
        bits.append(rain)
    line = "，".join(bits)
    return line + (f"（{real_text}）" if real_text else "")


def _real_brief(real: dict) -> str:
    """实况压成半句：`实况 5.2℃，湿度 75%`。"""
    w = (real or {}).get("weather") or {}
    bits = []
    if _valid(w.get("temperature")):
        bits.append(f"实况 {w['temperature']}℃")
    if _valid(w.get("humidity")):
        bits.append(f"湿度 {w['humidity']}%")
    pub = str((real or {}).get("publish_time") or "").strip()
    if pub:
        bits.append(f"{pub[-5:]} 更新")
    return "，".join(bits)


def daily_lines(city: str, days: int = 3) -> tuple[list[str], str]:
    """未来 N 天压成 N 行（今日/明日/后天…）。返回 `(行列表, 错误说明)`。

    用途：农情简报的"客观块"。★ 与 `fetch()` 分开，是因为那两个消费方要的形状不同：
    聊天里问"明天天气"要看字段明细（键值文本），简报要的是能一眼扫过去的一行一天。
    """
    n = max(1, min(len(_DAY_LABELS), int(days or 1)))
    station, why = find_station(city)
    lines: list[str] = []
    if station:
        try:
            data = requests.get(WEATHER_API, params={"stationid": station["code"]},
                                headers=_UA, timeout=TIMEOUT_S).json().get("data") or {}
            detail = ((data.get("predict") or {}).get("detail") or [])
            today = date.today()
            # ★ 标签必须按**日期差**算，不能按数组下标：凌晨没有翻页时
            #   `detail[0]` 还是昨天，按下标会把"明天"标成"今日"。
            for day in detail:
                try:
                    delta = (date.fromisoformat(str(day.get("date"))) - today).days
                except (TypeError, ValueError):
                    continue
                if delta < 0:
                    continue                      # 昨天的预报，丢掉
                if not (0 <= delta < len(_DAY_LABELS)):
                    continue
                label = _DAY_LABELS[delta]
                real_text = _real_brief(data.get("real") or {}) if delta == 0 else ""
                line = _compact_line(label, day, real_text=real_text)
                if line.strip(f"【{label}】 "):
                    lines.append(line)
                if len(lines) >= n:
                    break
            if lines:
                return lines, ""
        except Exception as e:                                # noqa: BLE001
            log.warning("气象台多天取数失败：%s: %s", type(e).__name__, e)
        else:
            why = why or "气象台没有返回可用的多日预报"
    # 退高德（4 天）
    from config import AppConfig
    key = getattr(AppConfig, "AMAP_API_KEY", "")
    if key:
        try:
            data = requests.get(AMAP_API, timeout=10,
                                params={"key": key, "city": city,
                                        "extensions": "all"}).json()
            casts = ((data.get("forecasts") or [{}])[0] or {}).get("casts") or []
            today = date.today()
            picked = []
            for t in casts:
                try:
                    delta = (date.fromisoformat(str(t.get("date"))) - today).days
                except (TypeError, ValueError):
                    continue
                if 0 <= delta < len(_DAY_LABELS):
                    picked.append((delta, t))
            for i, (delta, t) in enumerate(picked[:n]):
                label = _DAY_LABELS[delta]
                info = f"{t.get('dayweather', '')}" or "—"
                if t.get("nightweather") and t.get("nightweather") != t.get("dayweather"):
                    info = f"{info}转{t['nightweather']}"
                bits = [f"【{label}】{info}",
                        f"{t.get('nighttemp', '')}~{t.get('daytemp', '')}℃",
                        f"{t.get('winddirection', '')}{t.get('daypower', '')}级"]
                lines.append("，".join(x for x in bits if x and not x.startswith("~")))
            if lines:
                return lines, ""
        except Exception as e:                                # noqa: BLE001
            log.warning("高德多天取数失败：%s: %s", type(e).__name__, e)
        return [], f"两个源都没取到多日预报（高德：{why or '无数据'}）"
    return [], f"取不到多日预报（nmc：{why}；高德未配置 Key）"


def fetch(city: str, date_str: str = "today", *,
          source: str = "") -> tuple[str, str]:
    """按 `WEATHER_SOURCE` 取天气。返回 `(文本, 错误说明)`。"""
    from config import AppConfig
    mode = str(source or getattr(AppConfig, "WEATHER_SOURCE", "auto") or "auto")
    mode = mode.strip().lower()

    if mode == "amap":
        return amap_text(city, date_str)
    if mode == "nmc":
        return nmc_text(city, date_str)

    # auto：先 nmc，认不出地名再退高德
    text, why_nmc = nmc_text(city, date_str)
    if text:
        return text, ""
    log.info("nmc 取不到（%s），改用高德", why_nmc)
    text2, why_amap = amap_text(city, date_str)
    if text2:
        return text2, ""
    return "", f"两个源都取不到：nmc（{why_nmc}）；高德（{why_amap}）"


__all__ = ["INDEX_TTL_S", "amap_text", "build_index", "clear_index",
           "daily_lines", "fetch", "find_station", "index_path", "nmc_text"]