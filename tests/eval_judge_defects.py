# -*- coding: utf-8 -*-
"""judge **阳性对照**实验 —— 用"已知有缺陷的回答"测评委的判别力。

【为什么必须做这个】上一个实验（`eval_judge_agreement.py`）用**真实回答**测一致性，
        得到"4 个评委 100% 一致、kappa=1.000"。但那个结果**几乎没有信息量**：

          · `faithful` 17/17 全是 True → kappa **退化为无法计算**
          · `covers` 只有 1 条 False → 评委**根本没有分歧的机会**

        **用"质量都很好的样本"测评委一致性，就像用"全是健康人的体检"验证医生的诊断能力。**

【本实验做什么】对**同一批真实回答**做**受控的缺陷注入**，再看评委能不能抓到。
        因为原始回答的基线判定是 `covers=True`（16/17）与 `faithful=True`（17/17），
        所以判据非常干净：**注入后判 False = 抓住；判 True = 漏掉。**

【三组注入（都必须"judge 有可能检测到"才算公正）】
        · **G1 截断**：回答截到 35%。judge **手上就有期望要点**，缺了就该发现 → 看 `covers`
        · **G2 单条编造**：追加一条**不在白名单来源内**的荒谬断言 → 看 `faithful`
        · **G3 三条编造**：追加三条，其中一条**听起来合理**（"补贴自动到账"）
          → ★ 梯度设计：看"像真的"那条会不会被放过

        ⚠️ **不测**"事实性错误"（例如把真实产量改错）—— judge **没有事实来源**，
        要求它抓出这类错误是**不公平的**，测出来的低分不能归因于评委能力。

【用法】
  python tests/eval_judge_defects.py --answers tests/eval_outputs/gen_answers_xxx.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# ⚠️ 不包装 stdout（见 `eval_judge_agreement.py` 里的详细说明：会与 eval_generation 的包装冲突）

from core import providers                                   # noqa: E402
from eval_generation import JUDGE_PROMPT, parse_judge         # noqa: E402
from eval_judge_agreement import cohen_kappa, kappa_band      # noqa: E402

DEFAULT_JUDGES = "qwen:deepseek-v4.1-flash,qwen:qwen3.7-plus,qwen:glm-5.2"
JUDGE_TEMPERATURE = 0.2
JUDGE_SEED = 42

#: ★ 缺陷注入的文案。刻意让**第三条听起来合理** —— 那是最可能被放过的一条。
FAB_OBVIOUS = (
    "\n\n另据资料，该棉花品种由航天育种工程培育，"
    "并已在模拟火星环境中完成全周期种植验证。\n"
)
FAB_ABSURD = (
    "\n\n数据显示，本地区棉花亩产已突破 8,000 公斤，"
    "达到全国平均水平的约 20 倍。\n"
)
FAB_PLAUSIBLE = (
    "\n\n根据 2026 年最新规定，种植户每亩可额外获得 500 元数字农业补贴，"
    "无需申请即可自动到账。\n"
)


def _han(s: str) -> str:
    """只留汉字（去掉标点、数字、markdown 符号）。"""
    import re as _re
    return _re.sub(r"[^\u4e00-\u9fff]", "", s or "")


def _grams(s: str, n: int = 2) -> set[str]:
    """汉字 2-gram 集合 —— 中文短要点做机械匹配的实用办法。"""
    h = _han(s)
    return {h[i:i + n] for i in range(max(0, len(h) - n + 1))}


def _split_sections(ans: str) -> list[str]:
    """按 markdown 标题切段；没有标题就按空行切段。"""
    import re as _re
    parts = _re.split(r"(?m)^(?=#{2,4}\s)", ans)
    parts = [p for p in parts if p.strip()]
    return parts if len(parts) > 1 else [p for p in ans.split("\n\n") if p.strip()]


#: ★ 定向删段的两个门槛。**没有门槛的删除会制造假实验** ——
#: 第一版删掉了 31 字的段落（几乎什么也没删），那种情况下 `covers=True` 完全合理，
#: 拿它算"评委漏掉"就是**把设计缺陷记在评委头上**。
MIN_SEC_CHARS = 200      # 被删段落至少这么大，删除才有实质影响
MIN_HITS = 2             # 与要点的 2-gram 命中至少这么多，匹配才不是巧合
#: ★ 删除量上限。超过它，回答就"所剩无几"，判定退化成"回答太短"（即旧的截断实验，
#: 那个设计已确认不干净）。要求回答主体仍在，`covers=False` 才只能归因于"缺这一点"。
MAX_DROP_RATIO = 0.55


def inject_drop_section(ans: str, key_points: list[str]) -> tuple[str, str]:
    """G1b：**定向删除**覆盖某个期望要点的那一段 —— 而且是**验证驱动**的。

    【两次踩坑才写成现在这样】
      第一版：截断尾部。→ 保留的开头已含部分要点，"covers=True"未必是错的 ✗
      第二版：只删"命中最多"的那一段。→ 但同一个要点可能**在多处出现**
              （实测：#10 的「不构成建议」删掉了正文段，却仍在另一个**小节标题**里），
              于是评委说"覆盖了"**其实是对的** ✗

    所以本版：**删到该要点的 2-gram 真的从回答里消失为止**，
    并把"是否真清除"作为**注入是否合格的判据**（清不掉就放弃这条样本，
    **不算评委的错**）。删除量超过 60% 也放弃 —— 删太多就不是"缺这一点"了。
    """
    sections = _split_sections(ans)
    if len(sections) < 2:
        return ans, ""

    g = None
    best_plan = None
    # ★ 逐个要点试删，取**删除量最小**的那个方案 ——
    # 要点是"贯穿全文的主题"时（如"参考知识库资料"），清掉它要删掉大半篇，
    # 那就退化成"截断"了；优先挑覆盖**集中**的要点，实验才干净。
    for kp in (key_points or []):
        grams = _grams(kp)
        if not grams or len(grams & _grams(ans)) < MIN_HITS:
            continue
        kept = list(sections)
        dropped: list[int] = []
        while True:
            cands = [(len(grams & _grams(s)), len(s), i)
                     for i, s in enumerate(kept)
                     if len(s) >= MIN_SEC_CHARS and len(grams & _grams(s)) >= 1]
            if not cands:
                break
            cands.sort(reverse=True)
            _, _, i = cands[0]
            kept[i] = ""
            dropped.append(i + 1)
            if len("".join(kept)) < len(ans) * 0.40:
                break
        trial = "".join(kept)
        if not trial.strip() or (grams & _grams(trial)):
            continue                                  # 清不掉 → 这个要点不行
        removed = len(ans) - len(trial)
        if removed > len(ans) * MAX_DROP_RATIO:
            continue                                  # 删太多 → 退化成截断，不合格
        if best_plan is None or removed < best_plan[0]:
            best_plan = (removed, kp, trial, dropped)

    if best_plan is None:
        return ans, ""
    removed, g, new, dropped = best_plan
    return new, (f"删除第 {'/'.join(map(str, dropped))} 段"
                 f"（共 {removed} 字，占 {removed / len(ans) * 100:.0f}%），"
                 f"要点「{g}」的词已**全部清除**（已校验）")


def inject_truncate(ans: str, ratio: float = 0.35) -> str:
    """G1a：截断到原长的 ratio。

    ⚠️ **已确认设计不干净**（见上方说明）：保留的开头往往已含部分要点，
    因此低召回**不能**归因于评委能力。保留它仅供**对照**，不作为主证据。
    """
    return ans[: max(80, int(len(ans) * ratio))]


def inject_fab1(ans: str, key_points: list[str]) -> tuple[str, str]:
    """G2：追加一条明显编造（不在白名单来源内）。"""
    return ans + FAB_OBVIOUS, ""


def inject_fab3(ans: str, key_points: list[str]) -> tuple[str, str]:
    """G3：追加三条 —— 前两条明显，**第三条听起来合理**（最可能被放过）。"""
    return ans + FAB_OBVIOUS + FAB_ABSURD + FAB_PLAUSIBLE, ""


def _vkey(v: dict) -> str:
    """变体的缓存键。

    ⚠️ **必须含注入内容的哈希** —— 否则改了注入方式却会**复用旧判定**：
    本脚本的注入改过三版（截断 → 删最佳段 → 验证驱动删除），
    若只按 `#id|组名` 做键，第二版会静默拿到第一版的结果**而毫无察觉**。
    """
    import hashlib
    h = hashlib.sha256(v["answer"].encode("utf-8")).hexdigest()[:8]
    return f"#{v['sid']}|{v['group']}|{h}"


def _wrap_truncate(ans: str, key_points: list[str]) -> tuple[str, str]:
    return inject_truncate(ans), ""


#: (组名, 注入函数, 考核指标, 说明, 是否要求注入必须成功)
#: ★ `G1b 定向删段` 是**主证据**（`covers=False` 是唯一正确判定）；
#:   `G1a 截断旧版` 只作对照 —— 它的低召回**不能归因于评委**（见 `inject_truncate` 说明）。
GROUPS = [
    ("G1b 定向删段", inject_drop_section, "covers",
     "删掉覆盖某要点的整段 → 应判 covers=False", True),
    ("G2 单条编造", inject_fab1, "faithful",
     "明显编造 → 应判 faithful=False", False),
    ("G3 三条编造", inject_fab3, "faithful",
     "含一条「像真的」→ 看会不会被放过", False),
    ("G1a 截断旧版", _wrap_truncate, "covers",
     "（对照；设计不干净，不作主证据）", False),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="judge 阳性对照实验")
    ap.add_argument("--answers", required=True)
    ap.add_argument("--judges", default=DEFAULT_JUDGES)
    ap.add_argument("--samples", type=int, default=0,
                    help="取回答最长的前 N 条；**0 = 全部**（推荐，让注入自筛合格样本）")
    ap.add_argument("--groups", default="", help="只跑指定组（逗号分隔组名前缀），留空=全部")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    groups = GROUPS
    if args.groups:
        want = [g.strip() for g in args.groups.split(",") if g.strip()]
        groups = [g for g in GROUPS if any(g[0].startswith(w) for w in want)]
        if not groups:
            print(f"✗ --groups {args.groups!r} 没匹配到任何组；可选: {[g[0] for g in GROUPS]}")
            return 2

    rows = [json.loads(l) for l in Path(args.answers).read_text("utf-8").splitlines() if l.strip()]
    base = [r for r in rows
            if r.get("answer") and not r.get("should_refuse") and not r.get("chain_error")]
    #: 0 = 全部。★ 样本选择**不按长度硬切**，而是"注入能否生效"自筛 ——
    #: 按长度切会漏掉短但结构完整的回答，也会把注入失败的样本算进分母。
    picked = (sorted(base, key=lambda r: -len(r["answer"]))[: args.samples]
              if args.samples > 0 else sorted(base, key=lambda r: -len(r["answer"])))
    print("=" * 78)
    print("judge 阳性对照实验（已知缺陷 → 看评委能否抓住）")
    print("=" * 78)
    print(f"回答文件: {args.answers}")
    print(f"可用回答: {len(base)} 条 ｜ 候选 **{len(picked)}** 条"
          f"{'（全部）' if args.samples == 0 else f'（最长 {args.samples} 条）'}")
    print(f"被评模型: {picked[0].get('answer_model') if picked else '?'}")
    print(f"采样设定: temperature={JUDGE_TEMPERATURE}, seed={JUDGE_SEED}")

    # ---- 构造全部变体 ----
    variants: list[dict] = []
    skipped: list[str] = []
    for r in picked:
        for gname, fn, metric, why, must in groups:
            new_ans, note = fn(r["answer"], r.get("key_points", []))
            if new_ans == r["answer"]:
                (skipped if not must else skipped).append(f"#{r['id']}|{gname}")
                continue
            variants.append({
                "sid": r["id"], "group": gname, "metric": metric, "why": why,
                "note": note,
                "question": r["question"], "key_points": r.get("key_points", []),
                "answer": new_ans, "orig_len": len(r["answer"]), "new_len": len(new_ans),
            })
    print(f"变体总数: **{len(variants)}**（{len(picked)} 条 × {len(groups)} 组）")
    if skipped:
        print(f"⚠️ 跳过 {len(skipped)} 个变体（注入未生效）: {skipped[:6]}")
    print()
    for gname, _fn, metric, why, _must in groups:
        vs = [v for v in variants if v["group"] == gname]
        if not vs:
            print(f"  {gname:<12} ⚠️ 无有效变体")
            continue
        cut = vs[0]["new_len"] - vs[0]["orig_len"]
        print(f"  {gname:<12} 看 {metric:<9} {why}")
        print(f"              首条长度 {vs[0]['orig_len']}→{vs[0]['new_len']}（{cut:+d}）"
              + (f"  {vs[0]['note']}" if vs[0].get("note") else ""))

    # ---- 调评委 ----
    cache_file = ROOT / "tests" / "eval_outputs" / "judge_defect_cache.json"
    cache = {}
    if not args.no_cache and cache_file.is_file():
        try:
            cache = json.loads(cache_file.read_text("utf-8"))
        except Exception:                                    # noqa: BLE001
            cache = {}

    results: dict[str, dict[str, bool]] = {}                 # judge -> {变体key: 判定}
    usage: dict[str, dict] = {}
    errors: dict[str, str] = {}

    specs = [s.strip() for s in args.judges.split(",") if s.strip()]
    for spec in specs:
        prov, _, model = spec.partition(":")
        label = f"{model}@{prov}"
        # ★ 增量合并：缓存里已有的变体直接用，**只补跑缺失的** ——
        # 换了实验组不必重跑全部（上一版是"整块命中才用"，加一组就得全部重跑）。
        cached_v: dict = (cache.get(label) or {}).get("verdicts") or {}
        results[label] = dict(cached_v)
        usage[label] = (cache.get(label) or {}).get("usage", {})
        todo = [v for v in variants if _vkey(v) not in cached_v]
        if not todo:
            print(f"\n--- {label}：全部命中缓存（{len(cached_v)} 条），跳过调用 ---")
            continue
        if cached_v:
            print(f"\n--- {label}：缓存已有 {len(cached_v)} 条，补跑 {len(todo)} 条 ---")
        try:
            cli = providers.resolve_judge(prov, model=model)     # ★ judge 不走 failover
            cli._extra_body = {"seed": JUDGE_SEED}
            cli._temperature = JUDGE_TEMPERATURE
        except Exception as e:                               # noqa: BLE001
            errors[label] = f"构造失败: {e}"
            print(f"\n✗ {label}: {errors[label]}")
            if not cached_v:
                results.pop(label, None)
            continue

        print(f"\n--- 调用 {label}（{len(todo)} 条）---")
        got = results[label]
        t0 = time.time()
        for i, v in enumerate(todo, 1):
            key = _vkey(v)
            prompt = JUDGE_PROMPT.format(
                today_date="2026-10-18", question=v["question"],
                key_points="、".join(v["key_points"]), answer=v["answer"][:6000],
            )
            try:
                verdict = parse_judge(cli.generate_response([{"role": "user", "content": prompt}]))
            except Exception as e:                           # noqa: BLE001
                print(f"    {key} ✗ {type(e).__name__}: {str(e)[:50]}")
                continue
            if not verdict:
                print(f"    {key} ✗ 解析失败")
                continue
            got[key] = bool(verdict.get(v["metric"]))
            flag = "抓住" if not got[key] else "漏掉"
            print(f"    [{i:>2}/{len(todo)}] {key:<18} {v['metric']}={got[key]!s:<5} {flag}")
        dt = time.time() - t0
        if got:
            u = cli.usage_total
            prev = usage.get(label) or {}
            usage[label] = {
                "prompt": prev.get("prompt", 0) + u.prompt_total,
                "completion": prev.get("completion", 0) + u.completion_tokens,
                "calls": prev.get("calls", 0) + u.calls,
                "seconds": round(prev.get("seconds", 0) + dt, 1),
            }
            cache[label] = {"verdicts": got, "usage": usage[label]}
            cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"    → 本次 {len(todo)} 条 / {dt:.0f}s / {u.prompt_total} prompt tok"
                  f"（累计 {len(got)} 条）")
        else:
            errors[label] = "全部失败"
            results.pop(label, None)

    if not results:
        print("\n✗ 没有任何评委完成。")
        return 1

    # ---- 分析：召回率（抓住缺陷的比例）----
    labels = list(results)
    print()
    print("=" * 78)
    print("★ 召回率：评委判 False（= 抓住缺陷）的比例")
    print("=" * 78)
    print(f"{'组':<14}{'指标':<10}" + "".join(f"{l.split('@')[0][:18]:>20}" for l in labels))
    print("-" * 78)
    summary: dict = {"groups": {}, "judges": labels, "samples": len(picked)}
    for gname, _fn, metric, why, _must in groups:
        keys = [_vkey(v) for v in variants if v["group"] == gname]
        row = []
        print(f"{gname:<14}{metric:<10}", end="")
        for lab in labels:
            have = [results[lab][k] for k in keys if k in results[lab]]
            caught = sum(1 for x in have if not x)
            rate = caught / len(have) * 100 if have else 0.0
            row.append((lab, caught, len(have), rate))
            print(f"{f'{caught}/{len(have)} = {rate:.0f}%':>20}", end="")
        print()
        summary["groups"][gname] = {
            "metric": metric, "why": why,
            "recall": {lab: {"caught": c, "n": n, "rate": round(r, 1)} for lab, c, n, r in row},
        }

    # ---- 组内一致率 ----
    print()
    print("=" * 78)
    print("组内一致率（面对**已知缺陷**，评委是否一致）")
    print("=" * 78)
    from itertools import combinations
    for gname, _fn, metric, _why, _must in groups:
        keys = [_vkey(v) for v in variants if v["group"] == gname]
        common = [k for k in keys if all(k in results[l] for l in labels)]
        if len(common) < 2 or len(labels) < 2:
            print(f"  {gname:<14} 可比较样本不足")
            continue
        ks = []
        for a, b in combinations(labels, 2):
            ka = [results[a][k] for k in common]
            kb = [results[b][k] for k in common]
            ks.append(cohen_kappa(ka, kb))
        valid = [k for k in ks if k == k]
        mk = sum(valid) / len(valid) if valid else float("nan")
        print(f"  {gname:<14} 平均 kappa = {mk:5.3f}  {kappa_band(mk)}"
              f"   （{len(common)} 条共同样本 × {len(labels)} 评委）")
        summary["groups"][gname]["mean_kappa"] = (None if mk != mk else round(mk, 4))

    # ---- 每个评委的松紧 ----
    all_keys = [_vkey(v) for v in variants]
    print()
    print("=" * 78)
    print("★ 各评委的「严格度」（判 False 的总比例；越高越严）")
    print("=" * 78)
    for lab in labels:
        # ⚠️ 只统计**本轮变体**的键 —— 缓存里可能残留旧组名的条目
        vals = [results[lab][k] for k in all_keys if k in results[lab]]
        false_rate = sum(1 for v in vals if not v) / len(vals) * 100 if vals else 0.0
        print(f"  {lab:<34} 判 False {false_rate:5.1f}%  ({sum(1 for v in vals if not v)}/{len(vals)})")
        summary.setdefault("strictness", {})[lab] = round(false_rate, 1)

    # ---- 落盘 ----
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = ROOT / "tests" / "eval_outputs" / "judge_defect_summary.json"
    summary.update({"generated_at": ts, "answers_file": str(args.answers),
                    "usage": usage, "errors": errors,
                    "variants_per_group": len(picked)})
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("=" * 78)
    print(f"汇总已写入 {out}")
    if usage:
        print(f"本次消耗: {sum(u.get('calls',0) for u in usage.values())} 次调用 / "
              f"{sum(u.get('prompt',0) for u in usage.values()):,} prompt tok")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())