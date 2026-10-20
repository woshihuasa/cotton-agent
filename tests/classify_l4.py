# -*- coding: utf-8 -*-
"""D6 第一步：把生产 L4 的条目**分类**（只读，不改任何数据）。

【背景】L4 长期记忆容器按作用域分三库（画像库 / 项目事实库 / 技能库）。
        存量 540 条目前**拍平在同一个集合里**，需要分家。本脚本只做第一步：
        **给每条打上类别标签并输出可复核清单**，不写库、不删除。

【四步走（本脚本是第 1 步）】
        1. 分类（本脚本）：规则粗筛 → 可复核清单        ← 只读
        2. 人工确认：待删/待合并逐条看，A/B 抽样复核
        3. 分家：建两个库写入，原库先备份，对账           ← 需另行批准
        4. 回归：`smoke_memory` 等

【分类判据（顺序即优先级）】
        | 类 | 子标签 | 判据 | 去向 |
        |---|---|---|---|
        | D | 题面回执·操作 | 回执 + 指向"我自己地里的具体作业"（打药/滴水/灌溉/种植计划/采收…） | **删** |
        | A | 待观察(回执式兴趣) | 回执 + 指向"某个主题/领域"（产量/价格/政策/品种/预测…） | 画像库 · **待观察** |
        | A | 农事规则 | 含气象词，但形式是**条件规则**（「在…时使用/推迟/不打药」） | 画像库 |
        | C | 天气流水 | 含气象数值、且**不是**条件规则 | **删** |
        | A | 待观察(关注式) | 以「用户关注」开头（**必须排在 B 之前**） | 画像库 · **待观察** |
        | B | 项目事实 | **有阶段/时间锚点**（地块锚点**可选** —— 见下） | 项目事实库 |
        | A | 地点属性 | **纯地点陈述**（有"位于/地处/所在/属于"，且不含其它属性词） | 画像库（**并合并真重复**） |
        | A | 画像 | 其余（稳定属性/偏好/禁忌） | 画像库 |

【D8 决议：项目 = 生产季（地块可选）】
        原定义是「地块 × 生产季」。但实测显示绝大多数事实**只有季节/农事阶段、没有地块**
        （128 条 vs 21 条），照原定义它们无家可归。故改为：**以生产季为项目键，地块作为可选子键**；
        无地块的事实挂到该季的「汇总」桶。理由：库里就是一个农场两个地块，
        而大部分事实本来就不分地块陈述。

【人工复核带来的三处修正（v2–v3）】
        1. **回执必须先于天气判定**：`用户在2026年10月4日询问阿拉尔地区天气是否适合…`
           同时含「询问」与「天气」，否则会被当成天气流水。
        2. **天气词先过"规则闸"**：`用户在阴雨、风力≥4级时推迟打药` 是**稳定农事规则**，
           只因含「阴雨/风力」被误抓。加条件式判定后 C 类 17 → 13。
        3. **E 类取消**：原「仅阶段 / 仅地块」两桶按 D8 决议与人工确认分别并入 B 与 A/地点属性。
        4. **⚠️ v4 关键修复 —— 差点误删真实信息**：曾把"提到了地区"当成"是关于地点的"，
           于是 `用户在阿拉尔拥有20亩滴灌棉田`、`用户阿拉尔棉田种植双29棉`、
           `用户阿拉尔20亩棉田的目标皮棉产量为3至4吨` 等**地块属性**被装进地点桶；
           而按"包含哪些地区词"分组去重，会把 18 条语义不同的事实判为同一条 →
           **照单合并将删除 17 条真实信息**（盐碱地/品种/亩数/产量目标/施肥做法）。
           现改为：地点属性必须**既有地点谓语、又不含任何其它属性词**；
           去重键改为**去掉地点措辞后的剩余文本**（措辞归一后完全相同才算重复）——
           **宁可漏合并，绝不误删。**
        5. **⚠️ v5 —— 同一个错误升高了一层**：D8 把"有阶段锚点"一律判为项目事实，
           但 `用户吐絮期慎喷药并宜在傍晚打药` 这类**稳定做法换季仍成立**，属画像。
           抽样发现 project_facts 149 条里混入 23 条做法式、4 条统计查询残留
           （后者正是审计记录过的污染类型：`用户此前记录阿瓦提县2021年棉花总产为18.40万吨`）。
           现增加两条规则：① 统计查询残留 → 删；② 有阶段词 + 做法词 + **无强时间锚点** → 画像。
           **注意："提到了阶段"≠"是关于某一季的事实"，与 v4 的"提到了地区"≠"是关于地点的"同源。**

【为什么不用大模型分类】540 条跑一次 LLM 既花钱又**不可复现**，而这些锚点是**可枚举的**
        （地区词表、年份月份、农事阶段词）→ 正则就够。规则允许误判：
        本步产物是**给人看的清单**，不是最终决定。

【复用而非另造】地块词表直接 import `core.rag_engine._L4_REGION_TOKENS`（要素闸用的那份），
        避免"两份地区词表各自演化"——那正是本项目一直在防的"两份真相"。

【输出】`tests/eval_outputs/d6_classification.tsv`  全量逐条清单（含 doc_id，执行时按它定位）
        `tests/eval_outputs/d6_to_review.txt`       待删 + 待合并 + 改判说明，供人工确认

【用法】python tests/classify_l4.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import chromadb                                            # noqa: E402
from core.rag_engine import _L4_REGION_TOKENS              # noqa: E402

DB_PATH = "chroma_user_memory"
COLLECTION = "user_memory"
OUT_DIR = ROOT / "tests" / "eval_outputs"
OUT_TSV = OUT_DIR / "d6_classification.tsv"
OUT_REVIEW = OUT_DIR / "d6_to_review.txt"

# ---- 天气信号 ----
W_HARD = re.compile(r"\d+\s*℃|风力|湿度\s*\d")
W_SOFT = re.compile(r"天气|有雨|阵雨|降雨|多云|阴天|晴天")
DATE = re.compile(r"\d{4}年|\d{1,2}月\d{0,2}日?|[上中下]旬")
RULE_COND = re.compile(r"在[^。；]{1,40}时|时(使用|推迟|不打药|不施药|不宜)")

# ---- 回执信号 ----
D_RECEIPT = re.compile(
    r"曾(咨询|询问|查询|要求|以|问)|(咨询|询问|查询|要求)过"
    r"|当前(请求|询问|要求)|用户(于|在)?[\d年月日]{4,}(咨询|询问|查询)"
    r"|^用户(咨询|询问|查询)"
)
D_OPERATIONAL = re.compile(
    r"打药|滴水|灌溉|种植计划|采收|施药|高温干旱|几方|多少方|假设|为假设"
)

# ---- 统计查询残留：把"查到的统计数字"当成了用户事实 ----
# 审计早已记录过这类的兄弟条目（「用户此前记录阿瓦提县2021年棉花总产为18.40万吨」）。
# 它不是关于用户的事实，是知识库/统计口径里的数字被误抽取。与 D 类同源 → 建议删。
# ⚠️ 正则必须锚定**统计数据**，不能只认"此前记录"四个字 ——
#    实测踩过一次：`用户此前记录傍晚慎喷。` 里的"此前记录"是"以前记下过"的意思，
#    内容"傍晚慎喷"是**真实农事偏好**，若照删会误杀一条有效信息。
STAT_POLLUTION = re.compile(
    r"此前记录[^。]{0,30}(万亩|万吨|公斤/亩|公斤)"
    r"|用户[0-9]{4}年棉花亩产[0-9.]+公斤"
)

# ---- 稳定做法：提到农事阶段，但描述的是"怎么做"而非"这一季发生了什么" ----
# ⚠️ v5 修正：D8 把"有阶段锚点"一律判为项目事实，过于宽松 ——
#    `用户吐絮期慎喷药并宜在傍晚打药` 换季仍然成立，属**画像**而非某一季的事实。
#    判据：含做法词（宜/慎/忌/使用/偏好…）**且无强时间锚点**（年份/月/日/当前/已/本季）。
PRACTICE = re.compile(r"宜|慎|忌|避免|应当|应采用|采用|使用|偏好|不宜|注重")
STRONG_TIME = re.compile(
    r"\d{4}年|\d{1,2}月|\d{1,2}日|本季|今年|明年|生产季|全季|当前|已|尚未"
)
# ⚠️ v4 修正：曾把「提到了地区」误当成「是关于地点的」，导致
#    「用户在阿拉尔拥有20亩滴灌棉田」「用户阿拉尔棉田种植双29棉」这类
#    **地块属性**被装进地点桶，再按地区分组去重 → 会**误删 17 条真实信息**。
#    现在要求：有地点谓语（位于/地处/所在/属于）**且**不含任何其它属性词。
PURE_LOC = re.compile(r"位于|地处|所在|属于")
OTHER_ATTR = re.compile(
    r"亩|吨|公斤|方|品种|塔河|新陆|长绒棉|细绒棉|盐碱|滴灌|施|打药|风险|产量"
    r"|皮棉|籽棉|主栽|辅种|目标|预计|计划|偏好|忌|采用|注重|种植|面积|销售|售棉"
)

# ---- 阶段 / 时间锚点 ----
STAGE_TOKENS = (
    "播种", "播前", "出苗", "苗期", "现蕾", "蕾期", "开花", "花铃期", "结铃",
    "吐絮", "打顶", "封顶", "停水", "停灌", "滴水", "冬灌", "春灌", "灌溉",
    "施肥", "基肥", "追肥", "化控", "脱叶", "催熟", "机采", "采收", "整地",
)
TIME_ANCHOR = re.compile(
    r"\d{4}年|\d{1,2}月|\d{1,2}日|[上中下]旬|今年|明年|本季|生产季|全季|全年"
)

DELETE = (("C", "天气流水"), ("D", "题面回执·操作"), ("D", "统计查询残留"))

ACTION = {
    ("A", "画像"): "画像库",
    ("A", "农事规则"): "画像库",
    ("A", "地点属性"): "画像库（合并重复）",
    ("A", "待观察(关注式)"): "画像库 · 标「待观察」",
    ("A", "待观察(回执式兴趣)"): "画像库 · 标「待观察」（含兴趣信号，用户确认保留）",
    ("B", "项目事实"): "项目事实库（项目 = 生产季，地块可选）",
    ("C", "天气流水"): "**删（已确认）**",
    ("D", "题面回执·操作"): "**删（待确认）**",
    ("D", "统计查询残留"): "**删（待确认）** ← 审计记录过的同类污染",
}


def classify(text: str) -> tuple[str, str]:
    """返回 (类, 子标签)。顺序即优先级，与模块 docstring 的表格一致。"""
    tokens = sorted({t for t in _L4_REGION_TOKENS if t in text})
    has_region = bool(tokens)
    has_stage = bool(TIME_ANCHOR.search(text)) or any(t in text for t in STAGE_TOKENS)

    # 0) 统计查询残留：优先，且与 D 同源（都是"把问过/查到的东西当成事实"）
    if STAT_POLLUTION.search(text):
        return "D", "统计查询残留"

    # 1) 回执：必须先于天气判定
    if D_RECEIPT.search(text):
        if D_OPERATIONAL.search(text):
            return "D", "题面回执·操作"
        return "A", "待观察(回执式兴趣)"

    # 2) 天气：先过规则闸
    is_weather = bool(W_HARD.search(text)) or bool(W_SOFT.search(text) and DATE.search(text))
    if is_weather:
        if RULE_COND.search(text):
            return "A", "农事规则"
        return "C", "天气流水"

    # 3) 关注式：整类留画像库并标待观察。必须排在 B 之前。
    if text.startswith("用户关注"):
        return "A", "待观察(关注式)"

    # 4.5) 稳定做法：有阶段词但无强时间锚点 → 换季仍成立，属画像（见 v5 修正说明）
    if has_stage and PRACTICE.search(text) and not STRONG_TIME.search(text):
        return "A", "农事规则"

    # 5) 项目事实：**以生产季为键，地块可选**（D8 决议）
    if has_stage:
        return "B", "项目事实"

    # 5) 纯地点陈述 → 地点属性（人工确认归画像库，并合并**真重复**）
    if has_region and PURE_LOC.search(text) and not OTHER_ATTR.search(text):
        return "A", "地点属性"

    return "A", "画像"


_LOC_FILLER = (
    "用户", "位于", "地处", "所在", "属于", "新疆", "农场", "棉田", "棉区",
    "辖区", "区域", "地区", "市", "县", "州", "的", "在", "有", "，", "。", "、", " ",
)


def loc_key(text: str) -> tuple[str, ...]:
    """地点属性的去重键 = **去掉地点措辞后的剩余文本**。

    ⚠️ 刻意**不用**"包含哪些地区词"作键：那会把「阿拉尔20亩盐碱地」
    与「位于阿拉尔市」当成同一条。只有措辞归一后**完全相同**才算重复，
    宁可漏合并，绝不误删。
    """
    t = text
    for w in _LOC_FILLER:
        t = t.replace(w, "")
    return (t.strip(),)


def main() -> int:
    client = chromadb.PersistentClient(path=DB_PATH)
    got = client.get_collection(COLLECTION).get(include=["documents", "metadatas"])
    ids, docs, metas = got["ids"], got["documents"], got["metadatas"]

    rows: list[tuple[str, str, str, str]] = []
    tally: dict[tuple[str, str], int] = {}
    samples: dict[str, list[str]] = {}
    plot_groups: dict[tuple[str, ...], list[str]] = {}

    for doc_id, text, meta in zip(ids, docs, metas):
        cls, sub = classify(text)
        rows.append((cls, sub, doc_id, text))
        tally[(cls, sub)] = tally.get((cls, sub), 0) + 1
        samples.setdefault(f"{cls}/{sub}", []).append(text)
        if (cls, sub) == ("A", "地点属性"):
            plot_groups.setdefault(loc_key(text), []).append(text)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 全量清单 ----
    with open(OUT_TSV, "w", encoding="utf-8", newline="\n") as f:
        f.write("class\tsub\thas_category\tdoc_id\ttext\n")
        for (cls, sub, doc_id, text), meta in zip(rows, metas):
            cat = (meta or {}).get("category") or "(无)"
            f.write(f"{cls}\t{sub}\t{cat}\t{doc_id}\t{text}\n")

    to_del = [(c, s, t) for (c, s, _i, t) in rows if (c, s) in DELETE]
    dup_groups = {k: v for k, v in plot_groups.items() if len(v) > 1}
    dup_extra = sum(len(v) - 1 for v in dup_groups.values())

    # ---- 待删 / 待合并清单（人工确认 + 留档） ----
    review: list[str] = [
        "D6 待执行清单 —— 请逐条确认",
        "本文件仅为留档与人工复核用，**未执行任何删除**。",
        f"A. 建议删除 {len(to_del)} 条（C 天气流水 {tally.get(('C', '天气流水'), 0)} + "
        f"D 题面回执·操作 {tally.get(('D', '题面回执·操作'), 0)}）",
        f"B. 建议合并重复 {dup_extra} 条（地点属性共 {tally.get(('A', '地点属性'), 0)} 条，"
        f"其中 {len(dup_groups)} 组有重复，每组保留 1 条）",
        "",
    ]
    for cls, sub in DELETE:
        items = [t for (c, s, t) in to_del if (c, s) == (cls, sub)]
        review.append(f"===== A{cls} · {sub}（{len(items)} 条，建议删除）=====")
        for i, t in enumerate(items, 1):
            review.append(f"  {cls}{i:02d}  {t}")
        review.append("")

    review.append(f"===== B · 地点属性重复组（共 {len(dup_groups)} 组，"
                  f"建议每组保留 1 条、删除其余 {dup_extra} 条）=====")
    if not dup_groups:
        review.append("  （未发现重复）")
    for key, items in sorted(dup_groups.items(), key=lambda kv: -len(kv[1])):
        review.append(f"  [{'+'.join(key)}] {len(items)} 条 → 保留 1 条")
        for t in items:
            review.append(f"      · {t}")
    review.append("")

    review.append("===== 已改判、不删（人工复核结论）=====")
    for key in ("A/农事规则", "A/待观察(回执式兴趣)"):
        items = samples.get(key, [])
        review.append(f"  [{key}] {len(items)} 条")
        for t in items:
            review.append(f"      · {t}")
    review.append("")
    with open(OUT_REVIEW, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(review) + "\n")

    # ---- 控制台汇总 ----
    total = len(rows)
    print("=" * 78)
    print(f"D6 第一步：L4 存量分类（只读）—— 共 {total} 条")
    print("=" * 78)
    print(f"{'类':<4}{'子标签':<24}{'条数':>6}  去向")
    print("-" * 78)
    for (cls, sub), n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"{cls:<4}{sub:<24}{n:>6}  {ACTION.get((cls, sub), '?')}")

    print("-" * 78)
    print(f"对账：各类合计 {sum(tally.values())} / 总数 {total}  →",
          "守恒 ✓" if sum(tally.values()) == total else "**不守恒 ✗**")
    print(f"待删除 {len(to_del)} 条 + 待合并 {dup_extra} 条 = "
          f"**共动 {len(to_del) + dup_extra} 条**（占 {100 * (len(to_del) + dup_extra) / total:.1f}%）")

    no_cat = sum(1 for m in metas if not (m or {}).get("category"))
    old_schema = sum(1 for m in metas if "updated_at" not in (m or {}))
    print(f"元数据缺口：无 category 的 {no_cat} 条；无 updated_at（旧 schema）的 {old_schema} 条")

    for key in sorted(samples):
        print()
        print(f"── {key}（{len(samples[key])} 条）样例 ──")
        for s in samples[key][:3]:
            print(f"   · {s[:70]}")

    print()
    print(f"全量清单: {OUT_TSV.relative_to(ROOT).as_posix()}")
    print(f"人工确认: {OUT_REVIEW.relative_to(ROOT).as_posix()}")
    print("=" * 78)
    print("⚠️ 本步**只读**：未改动记忆库、未删除任何条目。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())