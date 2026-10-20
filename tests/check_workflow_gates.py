# -*- coding: utf-8 -*-
"""三类确定性门禁守卫 —— **必须 Fail-Closed**。

【定位】`docs/ORCHESTRATION.md` §6.1 的第 5 个守卫（P1）。

【本守卫的主力是"反例"】一个永远返回 `passed=True` 的门禁也能"通过"正向用例。
        所以核心是**每类错误各造一个样本，断言必须被拒**，
        并且专门断言那条最容易写错的：**门禁自身异常时必须拒绝，不能放行**。

【零 LLM / 零 API / 零磁盘】纯函数。

【用法】python tests/check_workflow_gates.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.orchestration import gates as G                     # noqa: E402


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
    c.section("第 1 节：数值一致性 —— 一致放行，不一致必须拒")
    # ------------------------------------------------------------------
    c.check("125.1 与来源 {'y':125} 一致（1% 容差内）",
            G.numeric_consistency([{"y": 125}], 125.1).passed)
    c.check("★ 125.1 与 120 不一致 → **拒绝**",
            G.numeric_consistency([{"y": 120}], 125.1).passed is False)
    # ★ 125.1 vs 125 的相对差是 0.08% —— 所以 0.1% 容差下**本来就该通过**，
    #   要制造"拒绝"必须把容差收紧到 0.08% 以下（0.01%）。
    c.check("容差 0.1% 时 125.1 vs 125 仍算一致（差 0.08%）",
            G.numeric_consistency([{"y": 125}], 125.1, tolerance=0.001).passed)
    c.check("★ 容差收紧到 0.01% 后 125.1 vs 125 → 拒绝",
            G.numeric_consistency([{"y": 125}], 125.1, tolerance=0.0001).passed is False)
    c.check("字符串里的数字也能抽（'亩产 125.1 公斤'）",
            G.numeric_consistency(["亩产 125 公斤"], "125.1").passed)
    c.check("★ 目标抽不出数字 → **拒绝**（不是放行）",
            G.numeric_consistency(["125"], "不知道").passed is False)
    c.check("★ 来源抽不出数字 → **拒绝**",
            G.numeric_consistency(["abc"], 125).passed is False)
    c.check("★ sources 为空 → **拒绝**",
            G.numeric_consistency([], 125).passed is False)

    # ------------------------------------------------------------------
    c.section("第 2 节：范围门禁 —— 越界拒绝，**无法判断也拒绝**")
    # ------------------------------------------------------------------
    c.check("125 在 [100,150] → 通过",
            G.range_check(125, minimum=100, maximum=150).passed)
    c.check("★ 160 越上界 → 拒绝", G.range_check(160, maximum=150).passed is False)
    c.check("★ 90 越下界 → 拒绝", G.range_check(90, minimum=100).passed is False)
    c.check("★ 无数字 → **拒绝**（无从判断不等于没问题）",
            G.range_check("不知道").passed is False)
    c.check("★ 多个数字 → **拒绝**（不许猜该比哪个）",
            G.range_check("100 和 200").passed is False)
    c.check("★ 上下界写反 → **拒绝**",
            G.range_check(50, minimum=150, maximum=100).passed is False)
    c.check("边界值本身算通过（闭区间）",
            G.range_check(100, minimum=100, maximum=100).passed)

    # ------------------------------------------------------------------
    c.section("第 3 节：要素互斥 —— 同时出现必须拒")
    # ------------------------------------------------------------------
    c.check("★ 南疆 + 北疆 → 拒绝",
            G.mutual_exclusion("按南疆标准", "北疆对照").passed is False)
    c.check("★ 机采 + 手采 → 拒绝",
            G.mutual_exclusion("机采作业要求", "手采的注意事项").passed is False)
    c.check("顺序反过来也要拒（右/左互换）",
            G.mutual_exclusion("北疆", "南疆").passed is False)
    c.check("只出现一个 → 通过", G.mutual_exclusion("南疆", "普通田").passed)
    c.check("★ 一侧为空 → **拒绝**（另一半无从比较）",
            G.mutual_exclusion("南疆", "").passed is False)
    c.check("两侧都为空 → 通过（确实不存在矛盾）",
            G.mutual_exclusion("", "").passed)
    c.check("自定义互斥对生效",
            G.mutual_exclusion("甲方案", "乙方案", pairs=(("甲", "乙"),)).passed is False)

    # ------------------------------------------------------------------
    c.section("第 4 节：★★★ Fail-Closed —— 门禁自身出错时必须拒绝")
    # ------------------------------------------------------------------
    # 直接验证 `_fail_closed` 的语义（这是整层的安全底线）
    fr = G._fail_closed("demo", RuntimeError("模拟内部故障"))
    c.check("★ `_fail_closed` 产出 **passed=False**", fr.passed is False)
    c.check("★ 拒绝理由里**写明是 Fail-Closed**", "Fail-Closed" in fr.reason, fr.reason)
    c.check("★ details 里带 fail_closed 标记", fr.details.get("fail_closed") is True)

    # 用一个必然抛异常的对象驱动真实门禁，验证它落到拒绝
    class _Boom:
        """`__str__` 抛异常 —— 这样 `_as_text()` 会真的炸，才能测到 Fail-Closed 分支。"""

        def __iter__(self):                                   # noqa: D105
            raise RuntimeError("模拟被遍历时爆炸")

        def __str__(self) -> str:                             # noqa: D105
            raise RuntimeError("模拟被转字符串时爆炸")

        def __repr__(self) -> str:                            # noqa: D105
            return "<Boom>"

    c.check("★★ `range_check` 遇到内部异常 → **拒绝**（不是放行）",
            G.range_check(_Boom()).passed is False)
    c.check("★★ `mutual_exclusion` 遇到内部异常 → **拒绝**",
            G.mutual_exclusion(_Boom(), "南疆").passed is False)
    c.check("★ 该拒绝被标记为 fail_closed（能区分「内部故障」与「业务不通过」）",
            G.mutual_exclusion(_Boom(), "南疆").details.get("fail_closed") is True)

    # ------------------------------------------------------------------
    c.section("第 5 节：`run_gate` 统一入口 —— 未知类型必须拒绝")
    # ------------------------------------------------------------------
    c.check("★ 未知门禁类型 → **拒绝**", G.run_gate("gate.不存在").passed is False)
    c.check("★ 参数名对不上 → **拒绝**（不是抛给调用方）",
            G.run_gate("gate.range", bogus=1).passed is False)
    c.check("已知类型能正常跑通",
            G.run_gate("gate.range", value=50, minimum=0, maximum=100).passed)
    c.check("三个门禁类型都在注册表里",
            set(G.GATES) == {"gate.numeric_consistency", "gate.range",
                             "gate.mutual_exclusion"}, str(sorted(G.GATES)))

    # ------------------------------------------------------------------
    c.section("第 6 节：数字抽取的边界")
    # ------------------------------------------------------------------
    c.check("bool 不被当成数字（True 是 int 的子类，必须挡掉）",
            G.extract_numbers(True) == [])
    c.check("千分位能正确解析（'1,234.5' → 1234.5）",
            G.extract_numbers("1,234.5") == [1234.5], str(G.extract_numbers("1,234.5")))
    c.check("None → 空列表（不抛异常）", G.extract_numbers(None) == [])

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"门禁守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())