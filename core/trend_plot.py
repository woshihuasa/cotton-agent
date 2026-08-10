"""
趋势折线图生成模块（plot_trend 工具后端）

职责：
  - 配置 matplotlib 中文字体（Windows SimHei），防止标题 / 轴标签乱码
  - 将多系列数据渲染为折线图 PNG，保存至 data/charts/

对外接口：
  generate_trend_chart(series, title, xlabel, ylabel, output_path) -> str
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无 GUI 后端，纯文件输出
import matplotlib.pyplot as plt
from matplotlib import rcParams

from config import AppConfig

# ── 中文字体配置 ──
rcParams["font.sans-serif"] = [
    "SimHei",            # Windows 黑体
    "Microsoft YaHei",   # Windows 微软雅黑（备选）
    "Arial Unicode MS",  # macOS 备选
    "DejaVu Sans",       # 最终兜底
]
rcParams["axes.unicode_minus"] = False  # 负号正常显示


def ensure_chart_dir() -> Path:
    """确保图表输出目录存在（AppConfig.CHART_DIR），返回其 Path。"""
    chart_dir = Path(AppConfig.CHART_DIR)
    chart_dir.mkdir(parents=True, exist_ok=True)
    return chart_dir


def warmup() -> None:
    """主线程预热 matplotlib。

    在 GUI 启动时（主线程）调用，预构建字体缓存并做一次空渲染，
    避免首次生成图表时在工作线程（RAGWorker/QThread）触发
    Windows 字体扫描导致 STATUS_IN_PAGE_ERROR 崩溃。
    """
    from matplotlib import font_manager

    _ = font_manager.fontManager  # 触发字体扫描并写缓存
    fig, ax = plt.subplots(figsize=(1, 1), dpi=10)
    ax.plot([0, 1], [0, 1])
    fig.savefig(ensure_chart_dir() / "_warmup.png")
    plt.close(fig)
    (ensure_chart_dir() / "_warmup.png").unlink(missing_ok=True)


def next_chart_path(prefix: str = "trend") -> Path:
    """生成带时间戳的输出路径，避免文件名冲突。

    Args:
        prefix: 文件名前缀，如 "trend" / "price"。

    Returns:
        Path: data/charts/{prefix}_YYYYMMDD_HHMMSS.png
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ensure_chart_dir() / f"{prefix}_{ts}.png"


def generate_trend_chart(
    series: list[dict],
    title: str,
    xlabel: str,
    ylabel: str,
    output_path: str | Path,
) -> str:
    """将多系列数据渲染为折线图 PNG。

    Args:
        series: 系列列表，每项为 {"label": 系列名, "x": [类别/日期], "y": [数值]}。
        title: 图表标题。
        xlabel: X 轴标签。
        ylabel: Y 轴标签。
        output_path: PNG 输出路径。

    Returns:
        str: 生成的图片路径（绝对或相对路径字符串）。

    Raises:
        ValueError: series 为空或无有效数据点。
    """
    if not series:
        raise ValueError("series 为空，无法绘图")

    fig, ax = plt.subplots(figsize=(10, 6), dpi=150)
    plotted = 0

    for s in series:
        xs = s.get("x") or []
        ys = s.get("y") or []
        if not xs or not ys or len(xs) != len(ys):
            continue
        ax.plot(xs, ys, marker="o", markersize=4, linewidth=1.8,
                label=s.get("label") or "系列")
        plotted += 1

    if plotted == 0:
        plt.close(fig)
        raise ValueError("所有系列均无有效数据点，无法绘图")

    ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(fontsize=10)

    # X 轴标签防重叠（类别多时旋转）
    fig.autofmt_xdate() if len(series[0].get("x") or []) > 8 else None
    fig.tight_layout()

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out.as_posix()  # 正斜杠路径，兼容 Markdown 图片链接


def generate_forecast_chart(
    region: str,
    metric_label: str,
    unit: str,
    history: list[dict],
    target_year: int,
    forecast: float,
    ci_lower: float,
    ci_upper: float,
    output_path: str | Path,
) -> str:
    """预测区间带图：历史折线 + 预测点 + 置信区间锥形带。

    Args:
        region: 地区名（图题用）。
        metric_label: 指标中文名（如"棉花产量"）。
        unit: 单位（如"吨"）。
        history: [{"year": 2020, "value": 933733.0}, ...] 历史序列。
        target_year: 预测目标年。
        forecast: 点预测值。
        ci_lower / ci_upper: 置信区间上下界。
        output_path: PNG 输出路径。

    Returns:
        str: 图片路径（正斜杠）。
    """
    years = [p["year"] for p in history if p["value"] is not None]
    vals = [p["value"] for p in history if p["value"] is not None]
    if not years:
        raise ValueError("历史序列为空，无法绘图")

    fig, ax = plt.subplots(figsize=(10, 6), dpi=150)

    # 历史折线
    ax.plot(years, vals, marker="o", markersize=5, linewidth=1.8,
            color="#2196F3", label="历史产量")

    # 置信区间锥形带（从历史末端展开到目标年）
    last_year, last_val = years[-1], vals[-1]
    ax.fill_between(
        [last_year, target_year],
        [last_val, ci_lower],
        [last_val, ci_upper],
        color="#2196F3", alpha=0.18,
        label=f"置信区间 ({ci_lower:.0f} ~ {ci_upper:.0f})",
    )
    # 预测点
    ax.plot([target_year], [forecast], marker="*", markersize=18,
            color="#F44336", label=f"预测 {forecast:.0f}")

    ax.set_title(f"{region}{metric_label}预测（{target_year} 年）", fontsize=14, pad=12)
    ax.set_xlabel("年份", fontsize=11)
    ax.set_ylabel(f"{metric_label}（{unit}）", fontsize=11)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(fontsize=10)
    fig.tight_layout()

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out.as_posix()


def generate_risk_chart(
    ranking: list[dict],
    metric_label: str,
    unit: str,
    output_path: str | Path,
) -> str:
    """产区波动风险横向条形图：低/中/高风险分别绿/橙/红。

    Args:
        ranking: risk_ranking 的返回列表（按 CV 降序，最危险在最上）。
        metric_label: 指标中文名。
        unit: 单位。
        output_path: PNG 输出路径。

    Returns:
        str: 图片路径（正斜杠）。
    """
    if not ranking:
        raise ValueError("ranking 为空，无法绘图")

    color_map = {"low": "#4CAF50", "medium": "#FF9800", "high": "#F44336"}

    # 自下而上绘制，使最高风险位于顶部
    regions = [r["region"] for r in reversed(ranking)]
    cvs = [r["cv"] for r in reversed(ranking)]
    colors = [color_map.get(r["level"], "#9E9E9E") for r in reversed(ranking)]

    fig, ax = plt.subplots(figsize=(10, max(6, len(ranking) * 0.42)), dpi=150)
    bars = ax.barh(regions, cvs, color=colors, height=0.62)

    ax.set_title(f"新疆产区{metric_label}波动风险分级", fontsize=14, pad=12)
    ax.set_xlabel("变异系数 CV（标准差/均值）", fontsize=11)
    ax.set_ylabel("产区", fontsize=11)
    ax.grid(True, axis="x", linestyle="--", alpha=0.4)

    # 阈值参考线
    ax.axvline(0.15, color="#888888", linestyle=":", linewidth=1.2)
    ax.axvline(0.35, color="#888888", linestyle=":", linewidth=1.2)
    ax.text(0.15, -0.6, "低/中阈值 0.15", fontsize=9, color="#666666", ha="center")
    ax.text(0.35, -0.6, "中/高阈值 0.35", fontsize=9, color="#666666", ha="center")

    # 条形末端标注 CV 值与等级
    for bar, item in zip(bars, reversed(ranking)):
        ax.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height() / 2,
                f"{item['cv']:.2f}（{item['label']}）",
                va="center", fontsize=9, color="#333333")

    fig.tight_layout()

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out.as_posix()
