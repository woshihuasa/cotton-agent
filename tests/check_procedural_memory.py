# -*- coding: utf-8 -*-
"""技能库守卫 —— 六道写入门禁、确定性检索、注入上限与持久化容错。

【定位】与既有守卫的分工：
        · `smoke_memory.py`  验证画像库/L3"记忆**还活着**吗"
        · `check_l4_conflict.py` 验证冲突判定"**准不准**"（需 LLM）
        · **本脚本**          验证技能库"**门禁与检索是否按设计工作**" ——
                              **零 LLM、零 embedding、零网络**，全程可复现

【为什么全绿才算过】ROADMAP 已把"阈值不是门禁"列为产品缺陷（指标只被打印、
        没有任何代码在守）。技能库的门禁必须逐条配反例，否则等于没有门禁。

【沙箱】全部读写落在 `tests/eval_outputs/procedural_sandbox/`（该目录已 gitignore），
        **不触碰生产路径** —— 显式传 path 时本模块不会 import config。

【用法】python tests/check_procedural_memory.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.procedural_memory import (          # noqa: E402
    MAX_SECTION_CHARS,
    ProceduralMemoryStore,
    entry_key,
    extract_triggers,
    normalize_shape,
    normalize_signature,
)

SANDBOX = ROOT / "tests" / "eval_outputs" / "procedural_sandbox"


class Checker:
    """极简断言收集器：逐条打印并统计，全绿才允许退出码 0。"""

    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        mark = "OK  " if ok else "FAIL"
        line = f"  [{mark}] {desc}"
        if detail and not ok:
            line += f"   ← {detail}"
        print(line)
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


def _fresh(name: str) -> Path:
    """沙箱内的全新存储文件路径。"""
    SANDBOX.mkdir(parents=True, exist_ok=True)
    p = SANDBOX / f"{name}.jsonl"
    if p.exists():
        p.unlink()
    return p


CHAIN = ["get_weather", "query_cotton_stats"]
SHAPE = {"get_weather": ["location", "date"], "query_cotton_stats": ["region", "year"]}


def main() -> int:
    c = Checker()

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)

    # ------------------------------------------------------------------
    c.section("第 1 部分：写入门禁 G1–G3（拒绝路径）")
    # ------------------------------------------------------------------
    st = ProceduralMemoryStore(_fresh("gates"))
    c.check("G1 空链 → rejected/empty_chain",
            st.record_trajectory([]).reason == "empty_chain")
    c.check("G1 全空白链 → rejected/empty_chain",
            st.record_trajectory(["  ", ""]).reason == "empty_chain")
    c.check("G2 工具报错 → rejected/tool_error",
            st.record_trajectory(CHAIN, SHAPE, tool_ok=False).reason == "tool_error")
    c.check("G3 未产出回答 → rejected/no_answer",
            st.record_trajectory(CHAIN, SHAPE, answered=False).reason == "no_answer")
    c.check("G1–G3 全部拒绝时库内无条目", st.counts()["entries"] == 0,
            f"实际 {st.counts()['entries']}")
    c.check("G1–G3 拒绝时不留文件（未触发落盘）", not st.store_path.exists())

    # ------------------------------------------------------------------
    c.section("第 2 部分：G4 签名归一化去重（膨胀防线）")
    # ------------------------------------------------------------------
    st = ProceduralMemoryStore(_fresh("dedup"))
    r1 = st.record_trajectory(CHAIN, SHAPE, triggers=["打药", "天气"])
    c.check("首次登记 → added", r1.action == "added" and r1.reason == "added",
            f"{r1.action}/{r1.reason}")
    c.check("首次登记后条目数 = 1", st.counts()["entries"] == 1)

    r2 = st.record_trajectory(CHAIN, SHAPE, triggers=["适合", "能不能"])
    c.check("同链同形状再次登记 → hit（不新增）",
            r2.action == "hit" and r2.reason == "duplicate_hit", f"{r2.action}/{r2.reason}")
    c.check("去重后条目数仍 = 1", st.counts()["entries"] == 1)
    c.check("hit 返回的是同一条目 ID", r2.entry_id == r1.entry_id)

    e = st.all_entries()[0]
    c.check("hit_count 累加为 1", e.get("hit_count") == 1, f"实际 {e.get('hit_count')}")
    c.check("triggers 合并了两次的标签（并集）",
            set(e.get("triggers") or []) == {"打药", "天气", "适合", "能不能"},
            f"实际 {e.get('triggers')}")

    # 形状指纹与书写顺序无关
    shuffled = {"query_cotton_stats": ["year", "region"], "get_weather": ["date", "location"]}
    c.check("参数顺序打乱后仍判为同一条（形状指纹与顺序无关）",
            st.record_trajectory(CHAIN, shuffled).action == "hit")

    # 同链不同形状 → 新变体
    r3 = st.record_trajectory(CHAIN, {"get_weather": ["location"], "query_cotton_stats": ["region"]})
    c.check("同链不同参数形状 → added（新变体）", r3.action == "added")
    c.check("变体使条目数 = 2", st.counts()["entries"] == 2)

    # 形状指纹本身
    c.check("normalize_shape 只含参数名、不含参数值",
            normalize_shape({"t": ["a", "b"]}) == "t:a,b")
    c.check("normalize_signature 丢弃空白项",
            normalize_signature(["a", " ", "b"]) == "a>b")

    # ------------------------------------------------------------------
    c.section("第 3 部分：G5 容量上限与淘汰")
    # ------------------------------------------------------------------
    st = ProceduralMemoryStore(_fresh("capacity"), max_entries=3)
    for tool in ("t_a", "t_b", "t_c"):
        st.record_trajectory([tool])
    c.check("容量 3 → 登记 3 条后条目数 = 3", st.counts()["entries"] == 3)

    st.record_trajectory(["t_d"])
    sigs = [e["signature"] for e in st.all_entries()]
    c.check("超容量时淘汰最久未用的一条（t_a）", "t_a" not in sigs, f"实际 {sigs}")
    c.check("新条目未被误淘汰", "t_d" in sigs, f"实际 {sigs}")
    c.check("条目数被压在容量内 = 3", st.counts()["entries"] == 3)

    # 命中过的条目应当比 0 命中的更晚淘汰
    st.record_trajectory(["t_c"])          # t_c.hit_count → 1
    st.record_trajectory(["t_e"])
    sigs = [e["signature"] for e in st.all_entries()]
    c.check("hit_count 高的条目不被淘汰（t_c 保留）", "t_c" in sigs, f"实际 {sigs}")
    c.check("淘汰掉 0 命中且更早的 t_b", "t_b" not in sigs, f"实际 {sigs}")

    # ------------------------------------------------------------------
    c.section("第 4 部分：G6 同签名变体封顶")
    # ------------------------------------------------------------------
    st = ProceduralMemoryStore(_fresh("variants"), max_variants=2)
    st.record_trajectory(["t_x"], {"t_x": ["p1"]})
    st.record_trajectory(["t_x"], {"t_x": ["p2"]})
    r = st.record_trajectory(["t_x"], {"t_x": ["p3"]})
    c.check("同签名第 3 个变体 → rejected/variant_cap",
            r.action == "rejected" and r.reason == "variant_cap", f"{r.action}/{r.reason}")
    c.check("封顶不影响条目数（仍为 2）", st.counts()["entries"] == 2)
    c.check("封顶只作用于同签名，不误伤其它工具链",
            st.record_trajectory(["t_y"], {"t_y": ["p1"]}).action == "added")

    # ------------------------------------------------------------------
    c.section("第 5 部分：确定性检索（零 LLM）")
    # ------------------------------------------------------------------
    st = ProceduralMemoryStore(_fresh("match"))
    st.record_trajectory(["query_cotton_stats"], triggers=["产量", "统计"])
    st.record_trajectory(["plot_trend"], triggers=["趋势", "绘图"])
    st.record_trajectory(["get_weather"], triggers=["天气", "打药"])

    c.check("无任何标签命中 → 返回空", st.match("今天心情不错") == [])
    c.check("空问句 → 返回空", st.match("") == [])
    c.check("k=0 → 返回空", st.match("天气", k=0) == [])

    hit = st.match("今天天气适合打药吗")
    c.check("命中标签的条目被召回", len(hit) == 1 and hit[0]["signature"] == "get_weather",
            f"实际 {[h['signature'] for h in hit]}")

    # 排序：命中数降序。query_cotton_stats 命中 2 个（产量/统计），plot_trend 只命中 1 个（趋势）
    hit = st.match("阿克苏产量统计和趋势", k=2)
    c.check("命中数多的排在前面（2 命中 > 1 命中）",
            len(hit) == 2 and hit[0]["signature"] == "query_cotton_stats",
            f"实际 {[(h['signature'], h.get('triggers')) for h in hit]}")

    c.check("单字标签被丢弃（否则「图」会命中几乎所有问句）",
            all(len(t) >= 2 for e in st.all_entries() for t in (e.get("triggers") or [])))
    c.check("显式传入的单字标签也被丢弃",
            ["图"] not in [e.get("triggers") for e in st.all_entries()])

    c.check("top-k 硬上限生效（k=2 只回 2 条）",
            len(st.match("产量 趋势 天气 打药 统计 绘图", k=2)) == 2)

    before = {e["id"]: (e["hit_count"], e["last_seen"]) for e in st.all_entries()}
    st.match("今天天气适合打药吗")
    after = {e["id"]: (e["hit_count"], e["last_seen"]) for e in st.all_entries()}
    c.check("match 是只读的（不改 hit_count / last_seen）", before == after)

    # ------------------------------------------------------------------
    c.section("第 6 部分：注入段落（反锁死声明 + 硬上限）")
    # ------------------------------------------------------------------
    st = ProceduralMemoryStore(_fresh("section"))
    c.check("无条目 → 空串", st.format_section([]) == "")

    st.record_trajectory(["get_weather", "query_cotton_stats"],
                         triggers=["打药", "天气", "能不能", "适合"])
    section = st.format_for_question("今天能不能打药")
    c.check("段落声明了性质（不是答案）", "不是答案" in section)
    c.check("段落含反锁死语句（以当前问题为准）", "以当前问题为准" in section)
    c.check("段落含工具链路径", "get_weather>query_cotton_stats" in section)
    c.check("段落长度在上限内", len(section) <= MAX_SECTION_CHARS,
            f"实际 {len(section)}")

    # 超长压力：5 条各带长标签的条目 → 必须被裁到上限内
    st2 = ProceduralMemoryStore(_fresh("section_long"), max_variants=50)
    for i in range(5):
        st2.record_trajectory(
            [f"tool_with_a_rather_long_name_{i}_xxxxxxxxxxxxxxxxxxxxxxxxxxx"],
            triggers=["很长的检索标签" * 4],
        )
    long_section = st2.format_section(st2.all_entries())
    c.check("条目数很多时段落仍不超上限", len(long_section) <= MAX_SECTION_CHARS,
            f"实际 {len(long_section)}")
    c.check("裁剪后仍保留反锁死语句", "以当前问题为准" in long_section)
    c.check("裁剪是从末位条目开始的（保留了第 1 条）", "1. " in long_section)

    # ------------------------------------------------------------------
    c.section("第 7 部分：持久化与容错")
    # ------------------------------------------------------------------
    path = _fresh("persist")
    st = ProceduralMemoryStore(path)
    st.record_trajectory(CHAIN, SHAPE, triggers=["打药"])
    st.record_trajectory(["plot_trend"], triggers=["趋势"])

    c.check("落盘后无 .tmp 残留（原子写完成）",
            not path.with_name(path.name + ".tmp").exists())

    st_reload = ProceduralMemoryStore(path)
    c.check("重新载入后条目数一致", st_reload.counts()["entries"] == 2)
    c.check("重新载入后签名一致",
            sorted(e["signature"] for e in st_reload.all_entries())
            == ["get_weather>query_cotton_stats", "plot_trend"])
    c.check("重新载入后 hit_count 一致", st_reload.all_entries()[0]["hit_count"] == 0)
    c.check("重载后去重仍生效（不会因重载而重复登记）",
            st_reload.record_trajectory(CHAIN, SHAPE).action == "hit")

    # 坏行容错：追加一行垃圾 + 一行合法
    good = {"id": "exp_9999", "signature": "t_manual", "triggers": ["手工"],
            "arg_shape": {}, "hit_count": 0}
    with open(path, "a", encoding="utf-8") as f:
        f.write("{这不是合法 JSON\n")
        f.write(json.dumps(good, ensure_ascii=False) + "\n")

    st_bad = ProceduralMemoryStore(path)
    c.check("坏行被跳过并计数（skipped_lines = 1）", st_bad.skipped_lines == 1,
            f"实际 {st_bad.skipped_lines}")
    c.check("坏行之后的合法行仍被载入", st_bad.counts()["entries"] == 3)
    c.check("结构化但不完整的行也计入坏行",
            ProceduralMemoryStore(_write(path, [{"id": "x"}, good])).skipped_lines == 1)

    # ------------------------------------------------------------------
    c.section("第 8 部分：检索标签提取（确定性）")
    # ------------------------------------------------------------------
    c.check("空问句 → 空标签", extract_triggers("") == [])
    t = extract_triggers("2026年阿克苏棉花产量是多少")
    c.check("提取到年份锚点 2026", "2026" in t, f"实际 {t}")
    t = extract_triggers("第一次滴水要滴30方")
    c.check("提取到「数值+单位」锚点 30方", "30方" in t, f"实际 {t}")
    # 已知边界（记录而非隐藏）：没有数字的「多少方」不产生单位锚点。
    # 农户口语里这类说法常见，若将来检索质量不达标，再评估引入「多少/几 + 单位」模式。
    t = extract_triggers("第一次滴水要滴多少方")
    c.check("已知边界：无数字的「多少方」不产生单位锚点",
            not any(str(x).endswith("方") for x in t), f"实际 {t}")
    c.check("标签去重", len(extract_triggers("天气天气天气")) == len(set(extract_triggers("天气天气天气"))))
    c.check("标签数量不超过上限", len(extract_triggers("阿克苏 喀什 和田 巴州 塔城 博州 昌吉 伊犁 哈密 吐鲁番")) <= 8)

    # ------------------------------------------------------------------
    c.section("第 9 部分：条目不含用户数据（可分发前提）")
    # ------------------------------------------------------------------
    st = ProceduralMemoryStore(_fresh("privacy"))
    st.record_trajectory(
        ["query_cotton_stats"],
        {"query_cotton_stats": ["region", "year"]},
        triggers=["产量"],
    )
    blob = json.dumps(st.all_entries(), ensure_ascii=False)
    c.check("条目里只有参数名、没有参数值（无「阿克苏」「阿拉尔」）",
            "阿克苏" not in blob and "阿拉尔" not in blob)
    c.check("条目里无结果摘要字段（无 result / answer）",
            "result" not in blob and "answer" not in blob)
    c.check("entry_key 可从落盘数据现算（shape_key 不落盘，单一真相）",
            entry_key(st.all_entries()[0]) == entry_key(
                {"signature": "query_cotton_stats",
                 "arg_shape": {"query_cotton_stats": ["region", "year"]}}))

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"技能库守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


def _write(path: Path, lines: list[dict]) -> Path:
    """把给定对象写成 JSONL（供坏行/容错用例构造）。"""
    p = path.with_name(path.stem + "_manual.jsonl")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return p


if __name__ == "__main__":
    raise SystemExit(main())