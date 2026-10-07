# -*- coding: utf-8 -*-
"""趋势绘图工具 —— **MCP 化改造的重点案例**。

问题
----
改造前：本工具把 PNG **写到本地磁盘**，并把绝对路径 `chart_path` 塞进返回 JSON，
由 LLM 用 `![图表](E:/.../trend_xxx.png)` 嵌进回答。这在桌面端成立，但在 MCP 场景
下**前提不成立**：MCP 宿主（Claude Desktop / Cursor）拿不到也显示不了本机文件路径。

改造
----
`chart` 的产出拆成两个**正交**的动作：

  1. **渲染**（`render_trend_chart`）—— 内存出 PNG 字节，**从不落盘**；
  2. **物化**（`_materialize`）—— 是否额外写一份到磁盘以取得 `chart_path`。

于是同一个工具能同时服务两条路径，且渲染结果**逐字节一致**（同一份 PNG 两种用法）：

  ┌ 桌面端（`_materialize=True`，默认）：text 里有 `chart_path` → LLM 嵌图
  └ MCP（`_materialize=False`）：images 里有 PNG 字节 → 转 `ImageContent` 返回

顺带修掉一个隔离缺口：评测/沙箱场景此前也会往生产 `charts/` 写图，
现在由调用方决定要不要落盘，评测侧传 `_materialize=False` 即可完全不写。
"""
from __future__ import annotations

from core.cotton_price import monthly_series
from core.cotton_stats import series_by_year
from core.tools.json_utils import dumps
from core.tools.result import ToolResult
from core.trend_plot import next_chart_path, render_trend_chart


def plot_trend(args: dict) -> ToolResult:
    """绘制趋势折线图：组装数据序列 → **内存渲染 PNG** → 返回序列 + 图片。

    `ToolResult.text` 为 JSON: `{"chart_path"?, "title", "series": [...], "note"?}`；
    `ToolResult.images` 携带 `(png_bytes, "image/png")`。

    `_materialize` 是本工具的内部开关，**不在对外 schema 里**（LLM 不会也不应传它），
    由调用方显式指定：桌面端默认 `True`（要路径嵌图），MCP 传 `False`（要字节不落盘）。
    """
    metric = args.get("metric", "")
    regions = args.get("regions") or []
    start = args.get("start")
    end = args.get("end")
    grade = args.get("grade", "3128B")
    title = (args.get("title") or "").strip()
    materialize = bool(args.get("_materialize", True))

    PRICE_METRICS = {"price"}
    YEARLY_METRICS = {"yield", "area", "mu", "long_yield", "long_area"}

    if metric not in PRICE_METRICS and metric not in YEARLY_METRICS:
        return ToolResult(text=dumps(
            {"error": f"不支持的指标: {metric!r}，可选 "
                      "yield/area/mu/long_yield/long_area/price"},
            ensure_ascii=False,
        ))

    try:
        if metric in PRICE_METRICS:
            # ── 价格月度序列（每等级一条线，regions 忽略） ──
            result = monthly_series(grade, start, end)
            if "error" in result:
                return ToolResult(text=dumps(result, ensure_ascii=False))
            series = [{
                "label": f"{grade}（元/吨）",
                "x": [p["month"] for p in result["points"]],
                "y": [p["value"] for p in result["points"]],
            }]
            notes = []
            default_title = f"中国棉花价格指数 {grade} 月度走势"
            xlabel, ylabel = "月份", "价格（元/吨）"
        else:
            # ── 年度序列（每地区一条线） ──
            if not regions:
                regions = ["全区"]
            series = []
            notes = []
            unit = ""
            for r in regions:
                res = series_by_year(r, metric, int(start or 2015),
                                     int(end or 2022))
                if "error" in res:
                    return ToolResult(text=dumps(
                        {"error": f"地区「{r}」: {res['error']}",
                         "candidates": res.get("candidates")},
                        ensure_ascii=False))
                if res.get("note"):
                    notes.append(res["note"])
                unit = res.get("unit", unit)
                series.append({
                    "label": res["region"],
                    "x": [str(p["year"]) for p in res["points"]],
                    "y": [p["value"] for p in res["points"]],
                })
            metric_label = {
                "yield": "棉花产量", "area": "棉花播种面积", "mu": "棉花亩产",
                "long_yield": "长绒棉产量", "long_area": "长绒棉播种面积",
            }[metric]
            default_title = f"新疆{metric_label}趋势对比（{'、'.join(r for r in regions)}）"
            xlabel, ylabel = "年份", f"{metric_label}（{unit}）"
    except (TypeError, ValueError) as e:
        return ToolResult(text=dumps({"error": f"参数解析失败: {e}"}, ensure_ascii=False))

    # ── 渲染：一律先出内存字节；需要路径时再落盘（同一份字节，不二次渲染）──
    try:
        png = render_trend_chart(series, title or default_title, xlabel, ylabel)
    except Exception as e:
        return ToolResult(text=dumps({"error": f"图表生成失败: {e}"}, ensure_ascii=False))

    resp: dict = {
        "title": title or default_title,
        "series": series,
    }
    if materialize:
        try:
            out_path = next_chart_path("trend")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(png)
            resp["chart_path"] = out_path.as_posix()
        except OSError as e:
            # 落盘失败不应让整个工具失败：图片仍在 images 中可正常交付
            resp["chart_note"] = f"图片已生成，但写入本地文件失败: {e}"
    if notes:
        resp["note"] = "；".join(notes)

    return ToolResult(text=dumps(resp, ensure_ascii=False),
                      images=[(png, "image/png")])
