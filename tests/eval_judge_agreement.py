# -*- coding: utf-8 -*-
"""judge 一致性实验 —— 用**多个独立评委**复核同一批真实回答。

【要回答的问题】`01` 号报的"忠实度 94.4%"是**同模型自评**（judge 与生产模型同为
        `deepseek-v4-flash`）。所以那个数字有一个未标注的前提：
        **它是"这个模型认为"94.4% 无编造，不等于 94.4% 真的无编造。**

        本脚本让**不同公司训练的模型**评判同一批回答，测**一致率 + Cohen's kappa**，
        把那个前提变成**测出来的数字**。

【为什么基线不用重跑】`eval_generation.py --answers-out` 落盘的回答文件里**已经带了基线
        judge 的判定**（`covers` / `faithful`）。所以 4 个评委只需 3 次新调用。

【对照设计】
  基线   : `deepseek-v4-flash` @ 官方 API —— **同模型自评**（现行口径）
  对照 A : `deepseek-v4.1-flash` @ Token Plan —— 同厂商**新版本**
  对照 B : `qwen3.7-plus` @ Token Plan —— **跨厂商**（阿里）
  对照 C : `glm-5.2` @ Token Plan —— **跨厂商**（智谱）

【判据】Cohen's kappa（扣除"碰巧一致"）。>0.8 稳定；0.4–0.8 有噪声需标注；
        <0.4 则该指标**不可作为绝对判据**，只能看趋势。

【用法】
  python tests/eval_judge_agreement.py --answers tests/eval_outputs/gen_answers_xxx.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# ⚠️ **不要**在这里包装 `sys.stdout`。
#
# 本脚本 import 了 `eval_generation`（拿 JUDGE_PROMPT / parse_judge），而**那个模块在
# 模块级也包装了一次 stdout**。于是这里再包一次会形成"包装器的包装器"：
# 本脚本的包装器随即成为垃圾被回收，其 `__del__` **关掉了底层缓冲区**，
# 导致后续 `print` 报 `ValueError: I/O operation on closed file`（踩了两次）。
#
# 输出编码改由环境变量 `PYTHONIOENCODING=utf-8` 保证（运行脚本时已设置）。
if os.environ.get("PYTHONIOENCODING", "").lower() not in ("utf-8", "utf8"):
    print("[warn] 建议设 PYTHONIOENCODING=utf-8，否则中文可能乱码", file=sys.stderr)

from core import providers                                  # noqa: E402
from eval_generation import JUDGE_PROMPT, parse_judge        # noqa: E402

#: 默认的独立评委（provider:model）。★ 刻意跨厂商 —— 同厂商的"独立性"是打折的。
DEFAULT_JUDGES = "qwen:deepseek-v4.1-flash,qwen:qwen3.7-plus,qwen:glm-5.2"

#: 固定采样 —— 与固定 seed 配合，让判定**可复现**
#: （`08` 号缺陷 4「无温度控制 → 判定不可复现」的解法）
JUDGE_TEMPERATURE = 0.2
JUDGE_SEED = 42


#: ★ 判定缓存 —— **贵的调用不该因为报表代码崩掉而白费**。
#: 本脚本曾因三处自造缺陷（stdout 双重包装 / 管道截断 / `completion_total` 写错）
#: 连续失败三次，每次都在跑完 17 次付费调用之后才崩。缓存就是那三次的补丁。
CACHE_FILE = ROOT / "tests" / "eval_outputs" / "judge_verdict_cache.json"


def _cache_key(answers: str, label: str, seed) -> str:
    return f"{Path(answers).name}|{label}|{seed}"


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.is_file() else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass                                                 # 缓存写不进去不该影响主流程


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    """二分类的 Cohen's kappa。样本为空或退化时返回 float('nan')。"""
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1 = sum(a) / n
    pb1 = sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe >= 1.0:
        return float("nan")          # 退化：全部同一类，kappa 无定义
    return (po - pe) / (1 - pe)


def kappa_band(k: float) -> str:
    if k != k:                       # NaN
        return "无法计算"
    if k > 0.8:
        return "✓ 稳定"
    if k >= 0.4:
        return "⚠️ 有噪声"
    return "❌ 不可作绝对判据"


def ask_judge(client, prompt: str, *, use_seed: bool) -> dict:
    """问一个 judge。返回 `{"verdict": {...}}` 或 `{"error": ...}`。"""
    msgs = [{"role": "user", "content": prompt}]
    try:
        text = client.generate_response(msgs)
    except Exception as e:                                   # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    v = parse_judge(text or "")
    if not v:
        return {"error": "parse_judge 解析失败", "raw": (text or "")[:200]}
    return {"verdict": v}


def main() -> int:
    ap = argparse.ArgumentParser(description="judge 一致性实验")
    ap.add_argument("--answers", required=True, help="eval_generation.py --answers-out 产出的 JSONL")
    ap.add_argument("--judges", default=DEFAULT_JUDGES, help="逗号分隔 provider:model")
    ap.add_argument("--no-seed", action="store_true", help="不传 seed（若平台不支持）")
    ap.add_argument("--no-cache", action="store_true", help="忽略判定缓存，强制重调")
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.answers).read_text("utf-8").splitlines() if l.strip()]
    # 只评"有回答且非拒答且无链路异常"的样本 —— 与 eval_generation 的分母口径一致
    judged = [r for r in rows
              if r.get("answer") and not r.get("should_refuse") and not r.get("chain_error")]
    print("=" * 78)
    print("judge 一致性实验")
    print("=" * 78)
    print(f"回答文件  : {args.answers}")
    print(f"总样本    : {len(rows)} 条 ｜ 进入质量判定: **{len(judged)}** 条")
    print(f"被评模型  : {rows[0].get('answer_model', '?') if rows else '?'}")
    print(f"基线 judge: {rows[0].get('judge_model', '?') if rows else '?'}  （来自文件，同模型自评）")
    print(f"采样设定  : temperature={JUDGE_TEMPERATURE}"
          f"{'' if args.no_seed else f', seed={JUDGE_SEED}'}")

    # ---- 构造评委表：基线（读文件）+ 独立评委（现调）----
    specs = [s.strip() for s in args.judges.split(",") if s.strip()]
    verdicts: dict[str, dict[int, dict]] = {}          # label -> {sample_id: verdict}
    verdicts["基线·官方dsv4f（同模型自评）"] = {
        r["id"]: {"covers": r.get("covers"), "faithful": r.get("faithful")} for r in judged
    }
    errors: dict[str, str] = {}
    usage: dict[str, dict] = {}

    cache = {} if args.no_cache else _load_cache()
    seed_val = None if args.no_seed else JUDGE_SEED

    for spec in specs:
        prov, _, model = spec.partition(":")
        label = f"{model}@{prov}"
        ck = _cache_key(args.answers, label, seed_val)

        # ★ 命中缓存就直接用 —— 不重复付费调用
        if ck in cache and cache[ck].get("verdicts"):
            verdicts[label] = {int(k): v for k, v in cache[ck]["verdicts"].items()}
            usage[label] = cache[ck].get("usage", {})
            print(f"\n--- {label}：**命中缓存**（{len(verdicts[label])} 条），跳过调用 ---")
            continue

        try:
            client = providers.resolve_judge(prov, model=model)   # ★ judge 不走 failover
            if not args.no_seed:
                client._extra_body = {"seed": JUDGE_SEED}
            client._temperature = JUDGE_TEMPERATURE
        except Exception as e:                               # noqa: BLE001
            errors[label] = f"构造失败: {e}"
            print(f"\n✗ {label}: {errors[label]}")
            continue

        print(f"\n--- 调用 {label} ---")
        got: dict[int, dict] = {}
        t0 = time.time()
        for i, r in enumerate(judged, 1):
            prompt = JUDGE_PROMPT.format(
                today_date=date.today().isoformat(),
                question=r["question"],
                key_points="、".join(r.get("key_points", [])),
                answer=(r.get("answer") or "")[:6000],
            )
            res = ask_judge(client, prompt, use_seed=not args.no_seed)
            if "error" in res and not args.no_seed and "seed" in str(res["error"]).lower():
                print(f"    ⚠️ seed 不被支持，回退为不传 seed: {res['error'][:80]}")
                client._extra_body = {}
                res = ask_judge(client, prompt, use_seed=False)
            if "error" in res:
                print(f"    #{r['id']:>2} ✗ {res['error'][:70]}")
                continue
            got[r["id"]] = res["verdict"]
            print(f"    #{r['id']:>2} covers={res['verdict'].get('covers')} "
                  f"faithful={res['verdict'].get('faithful')}")
        dt = time.time() - t0
        if got:
            verdicts[label] = got
            u = client.usage_total
            usage[label] = {"prompt": u.prompt_total,
                            "completion": u.completion_tokens,
                            "cache_hit": u.cache_hit_tokens,
                            "calls": u.calls, "seconds": round(dt, 1)}
            print(f"    → {len(got)}/{len(judged)} 条完成，{dt:.0f}s，"
                  f"{u.prompt_total} prompt tok / {u.completion_tokens} 输出 tok")
            # ★ 立即落盘：即使后面的报表代码崩了，这批判定也不会丢
            cache[ck] = {"verdicts": {str(k): v for k, v in got.items()},
                         "usage": usage[label]}
            _save_cache(cache)
        else:
            errors[label] = "全部样本调用失败"

    if len(verdicts) < 2:
        print("\n✗ 可比较的评委不足 2 个，无法计算一致性。")
        for k, v in errors.items():
            print(f"    {k}: {v}")
        return 1

    # ---- 一致性 ----
    labels = list(verdicts)
    ids = sorted(set.intersection(*[set(v) for v in verdicts.values()]))
    print()
    print("=" * 78)
    print(f"一致性与 kappa（{len(labels)} 个评委 × {len(ids)} 条共同样本）")
    print("=" * 78)

    report: dict = {"samples": len(ids), "judges": labels, "metrics": {}, "pairs": []}
    for metric in ("covers", "faithful"):
        print(f"\n【{metric}】")
        series = {lab: [bool(verdicts[lab][i].get(metric)) for i in ids] for lab in labels}
        rates = {lab: sum(s) / len(s) for lab, s in series.items()}
        for lab in labels:
            print(f"  {lab:<34} 判 True {rates[lab]*100:5.1f}%  ({sum(series[lab])}/{len(ids)})")
        pair_ks = []
        print()
        for a, b in combinations(labels, 2):
            agree = sum(1 for x, y in zip(series[a], series[b]) if x == y) / len(ids)
            k = cohen_kappa(series[a], series[b])
            pair_ks.append(k)
            report["pairs"].append({"metric": metric, "a": a, "b": b,
                                    "agreement": round(agree, 4),
                                    "kappa": (None if k != k else round(k, 4))})
            print(f"    {a.split('@')[0]:<24} vs {b.split('@')[0]:<24} "
                  f"一致 {agree*100:5.1f}%  kappa {k:5.3f}  {kappa_band(k)}")
        valid = [k for k in pair_ks if k == k]
        mean_k = sum(valid) / len(valid) if valid else float("nan")
        spread = (max(rates.values()) - min(rates.values())) * 100
        print(f"\n  ★ 平均 kappa = {mean_k:.3f}  →  {kappa_band(mean_k)}")
        print(f"  ★ 各评委判 True 的比率极差 = {spread:.1f}pp"
              f"（越大说明「换个评委结论就变」）")
        report["metrics"][metric] = {
            "rates": {k: round(v, 4) for k, v in rates.items()},
            "mean_kappa": (None if mean_k != mean_k else round(mean_k, 4)),
            "rate_spread_pp": round(spread, 2),
        }

    # ---- ★ 基线 vs 独立评委：自我偏好有多大 ----
    base_lab = labels[0]
    print()
    print("=" * 78)
    print("★ 同模型自评 vs 独立评委（本实验最核心的一问）")
    print("=" * 78)
    for metric in ("covers", "faithful"):
        b = [bool(verdicts[base_lab][i].get(metric)) for i in ids]
        for lab in labels[1:]:
            o = [bool(verdicts[lab][i].get(metric)) for i in ids]
            agree = sum(1 for x, y in zip(b, o) if x == y) / len(ids)
            k = cohen_kappa(b, o)
            print(f"  {metric:<9} 基线 vs {lab.split('@')[0]:<22} "
                  f"一致 {agree*100:5.1f}%  kappa {k:5.3f}")

    # ---- 分歧样本（人工复核清单）----
    print()
    print("=" * 78)
    print("分歧样本（★ 大概率是「期望要点写得有歧义」，值得人工看原文）")
    print("=" * 78)
    disagreements = []
    by_id = {r["id"]: r for r in judged}
    for metric in ("covers", "faithful"):
        for i in ids:
            vals = {lab: bool(verdicts[lab][i].get(metric)) for lab in labels}
            if len(set(vals.values())) > 1:
                yes = [lab.split("@")[0] for lab, v in vals.items() if v]
                no = [lab.split("@")[0] for lab, v in vals.items() if not v]
                disagreements.append({"id": i, "metric": metric,
                                      "yes": yes, "no": no})
                print(f"  #{i:>2} 【{metric}】 判True: {', '.join(yes)} ｜ 判False: {', '.join(no)}")
                print(f"        问题: {by_id[i]['question'][:60]}")

    # ---- 落盘 ----
    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = ROOT / "tests" / "eval_outputs"
    outdir.mkdir(parents=True, exist_ok=True)
    summary = outdir / "judge_agreement_summary.json"
    summary.write_text(json.dumps({
        "generated_at": ts,
        "answers_file": str(args.answers),
        "answer_model": rows[0].get("answer_model") if rows else None,
        "sampling": {"temperature": JUDGE_TEMPERATURE,
                     "seed": None if args.no_seed else JUDGE_SEED},
        "usage": usage, "errors": errors,
        "disagreements": disagreements,
        **report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 78)
    print(f"汇总已写入 {summary}")
    if usage:
        # ⚠️ 用 `.get` —— 从日志抢救回来的缓存项**没有 usage**（只有判定），
# 直接下标会 KeyError（本脚本第四次自造缺陷）。
        tin = sum(u.get("prompt", 0) for u in usage.values())
        tout = sum(u.get("completion", 0) for u in usage.values())
        tcalls = sum(u.get("calls", 0) for u in usage.values())
        print(f"本次消耗: {tcalls} 次调用 / {tin:,} prompt tok / {tout:,} 输出 tok")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())