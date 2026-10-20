# -*- coding: utf-8 -*-
"""**度量代码**守卫 —— 守"尺子"本身，不守产品。

【为什么单独建一个守卫】本项目已经两次栽在"尺子错了"上：
        ① `covers` 判定过宽 → 阳性对照召回 **0%**（见 `01` §5.6）；
        ② `is_refusal` 用子串判拒答 → 正常回答末尾的**免责声明**被判成拒答，
           20 条里 2 条误判、拒答准确率虚低 **10%**（见 `01` §5.5）。
        这两次都**不是产品退步**，而是**度量本身偏了** —— 而度量偏了会让所有结论跟着偏。

        所以本守卫专盯三件"尺子"：
          · `is_refusal` —— **位置限定**（只看开头）；
          · `parse_judge` —— 容错解析（前台有废话也能取出 JSON；解析不出要能识别）；
          · `cohen_kappa` —— 一致性系数（已知答案可验证）。

【零 LLM / 零 API】全部是纯函数断言，不付费、不联网。

【用法】python tests/check_eval_metrics.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import importlib.util                                        # noqa: E402

from eval_generation import (                                # noqa: E402
    REFUSE_KEYWORDS,
    REFUSE_SCAN_CHARS,
    is_refusal,
    parse_judge,
)


def _load_kappa():
    """`eval_judge_agreement` 在模块级会 chdir / 改 sys.path —— 用保守方式加载。"""
    spec = importlib.util.spec_from_file_location(
        "_ja", str(ROOT / "tests" / "eval_judge_agreement.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                             # type: ignore[union-attr]
    return mod.cohen_kappa


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}" + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def main() -> int:
    c = Checker()

    # ------------------------------------------------------------------
    c.section("第 1 节：★ is_refusal 只看开头（2026-10-18 修复的回归线）")
    # ------------------------------------------------------------------
    c.check(f"扫描窗口是 {REFUSE_SCAN_CHARS} 字（正数）",
            isinstance(REFUSE_SCAN_CHARS, int) and REFUSE_SCAN_CHARS > 0)
    c.check("★ 扫描窗口足够小（>1000 就等于没限制）",
            REFUSE_SCAN_CHARS <= 400, str(REFUSE_SCAN_CHARS))

    # 真实形态一：**开头拒绝** → 应判拒答
    c.check("★ 开头就拒绝（关键词在前 20 字）→ 判拒答",
            is_refusal("抱歉，我无法回答这个问题，因为它超出了我的服务范围。"))
    # 真实形态二：**长篇正常回答 + 末尾免责** → 不应判拒答
    long_ok = ("# 3128B 棉花价格分析\n\n" + "数据显示上涨 18.43%。" * 60 +
               "\n\n交易操作层面的问题，建议咨询具备资质的期货机构。")
    c.check("★★ **长篇正常回答 + 末尾免责声明** → **不判拒答**（这正是 10-18 的误判形态）",
            is_refusal(long_ok) is False,
            f"长度 {len(long_ok)}，关键词位置 {long_ok.find('建议咨询')}")
    # 位置边界：刚好在窗口内 vs 窗口外
    pad = "正常内容。" * 30                                   # 180 字
    inside = pad[:REFUSE_SCAN_CHARS - 10] + "无法回答。"
    outside = ("正常内容。" * 200) + "无法回答。"
    c.check("关键词落在窗口**内** → 判拒答", is_refusal(inside) is True)
    c.check("关键词落在窗口**外** → 不判拒答", is_refusal(outside) is False)

    # 边界：空 / 极短
    c.check("空回答 → 不判拒答（空不等于拒答）", is_refusal("") is False)
    c.check("None → 不判拒答（不抛异常）", is_refusal(None) is False)  # type: ignore[arg-type]

    # 词表本身别退化成空
    c.check("拒答词表非空", len(REFUSE_KEYWORDS) >= 10, str(len(REFUSE_KEYWORDS)))

    # ------------------------------------------------------------------
    c.section("第 2 节：parse_judge 容错解析")
    # ------------------------------------------------------------------
    c.check("★ 纯 JSON → 解析成功",
            parse_judge('{"covers": true, "faithful": false, "reason": "x"}')
            == {"covers": True, "faithful": False, "reason": "x"})
    c.check("★ JSON 前后有废话 → 仍能取出（judge 常带解释）",
            parse_judge('我先想想。\n{"covers": true, "faithful": true}\n以上。')
            == {"covers": True, "faithful": True})
    c.check("★ 解析不出 → 返回空 dict（而非抛异常）",
            parse_judge("完全不是 JSON") == {})
    c.check("空输入 → 返回空 dict", parse_judge("") == {})
    c.check("None → 返回空 dict", parse_judge(None) == {})       # type: ignore[arg-type]
    # ★ 空 dict 必须能被调用方识别为"解析失败"（`if not verdict`）
    c.check("★ 解析失败返回的空 dict 是**假值**（调用方靠 `if not verdict` 判断）",
            not parse_judge("垃圾"))

    # ------------------------------------------------------------------
    c.section("第 3 节：cohen_kappa —— 用已知答案验算")
    # ------------------------------------------------------------------
    try:
        kappa = _load_kappa()
    except Exception as e:                                   # noqa: BLE001
        c.check("加载 cohen_kappa", False, f"{type(e).__name__}: {e}")
        kappa = None
    if kappa is not None:
        c.check("完全一致 → 1.0",
                abs(kappa([True, True, False, False], [True, True, False, False]) - 1.0) < 1e-9)
        c.check("完全相反 → -1.0",
                abs(kappa([True, True, False, False], [False, False, True, True]) + 1.0) < 1e-9)
        c.check("随机水平 → 约 0",
                abs(kappa([True, True, False, False], [True, False, True, False])) < 1e-6)
        nan = kappa([True] * 4, [True] * 4)
        c.check("★ 全同类（退化）→ NaN 而不是 1.0（不能把「无区分度」当「完美一致」）",
                nan != nan, str(nan))

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 只有一个定义（防「改了新版、旧版仍在文件里覆盖」）")
    # ------------------------------------------------------------------
    src = (ROOT / "tests" / "eval_generation.py").read_text(encoding="utf-8")
    # 这条来自真实事故：第一次修 is_refusal 时在文件前部插了新版，
    # 而旧的实现留在后面**把新版覆盖掉**了 —— 测试全绿但行为没变 ✗
    c.check("★ `def is_refusal` 在文件里**只出现一次**（否则旧的会覆盖新的）",
            src.count("def is_refusal") == 1, str(src.count("def is_refusal")))
    c.check("★ is_refusal 的实现里引用了扫描窗口常量",
            "REFUSE_SCAN_CHARS" in src.split("def is_refusal")[1][:600])

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"度量代码守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())