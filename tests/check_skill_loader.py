# -*- coding: utf-8 -*-
"""手写技能加载器守卫 —— 规范符合性 + 三条红线 + 容错。

【定位】`docs/ARCHITECTURE.md` §8 定义了 SKILL.md 规范，其中有**三条红线**：
        · 不注册工具（不碰 `core/tools/schemas.py` 的单一真相）
        · 不执行脚本（`scripts/` 只是参考材料）
        · 不建索引 / 不建注册表（目录即真相）

        本脚本把这三条**从约定变成检查** —— 前两条尤其危险：一旦破了，
        要么毁掉契约单一真相，要么让只读挂载点变成可执行入口。

【零 LLM / 零 API】触发词匹配与结晶档共用 `trigger_hits()`，全程确定性。

【用法】python tests/check_skill_loader.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core.skill_loader import (                # noqa: E402
    MAX_EXTERNAL_SKILLS,
    MAX_SKILL_SECTION_CHARS,
    MAX_SKILLS,
    SkillLibrary,
)
from core.tools import TOOL_SCHEMAS            # noqa: E402

SANDBOX = ROOT / "tests" / "eval_outputs" / "skill_loader_sandbox"
KNOWN = {t["function"]["name"] for t in TOOL_SCHEMAS}


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


def _write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


GOOD = """---
id: good_skill
name: 好技能
triggers: [打药, 施药]
tools_used: [get_weather]
summary: 一句话摘要
acceptance:
  - question: 今天能打药吗
    expected_tools: [get_weather]
    key_points: [风力]
inject_priority: 50
---

# 好技能

## 编排步骤

1. 调用 `get_weather`
"""


def main() -> int:
    c = Checker()
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    root = SANDBOX / "skills"
    root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    c.section("第 1 节：解析 —— frontmatter 各字段")
    # ------------------------------------------------------------------
    _write(root, "good.md", GOOD)
    lib = SkillLibrary(mount_dirs=[root], known_tools=KNOWN)

    c.check("扫到 1 个技能", len(lib.all_skills()) == 1, str([s.id for s in lib.all_skills()]))
    c.check("无解析失败", lib.errors() == [], str(lib.errors()))

    sk = lib.all_skills()[0]
    c.check("id / name 解析正确", (sk.id, sk.name) == ("good_skill", "好技能"), f"{sk.id}/{sk.name}")
    c.check("triggers 解析为元组", sk.triggers == ("打药", "施药"), str(sk.triggers))
    c.check("tools_used 解析正确", sk.tools_used == ("get_weather",), str(sk.tools_used))
    c.check("summary 解析正确", sk.summary == "一句话摘要", sk.summary)
    c.check("acceptance 被收集（不是只校验）", len(sk.acceptance) == 1, str(len(sk.acceptance)))
    c.check("acceptance 字段完整",
            sk.acceptance[0]["expected_tools"] == ["get_weather"], str(sk.acceptance[0]))
    c.check("inject_priority 解析为 int", sk.inject_priority == 50, str(sk.inject_priority))
    c.check("body 去掉 H1", sk.body.lstrip().startswith("## 编排步骤"), sk.body[:20])
    c.check("origin 标为 shipped（第一个挂载点）", sk.origin == "shipped", sk.origin)

    # ------------------------------------------------------------------
    c.section("第 2 节：容错 —— 坏文件只跳过，不崩、且记录原因")
    # ------------------------------------------------------------------
    bad = {
        "no_fm.md": "# 没有 frontmatter\n正文\n",
        "no_id.md": "---\nname: 缺 id\ntriggers: [打药]\nsummary: x\n---\n正文\n",
        "bad_yaml.md": "---\nid: [未闭合\n---\n正文\n",
        "empty_triggers.md": "---\nid: x\nname: x\ntriggers: []\nsummary: x\n---\n正文\n",
        "short_trigger.md": "---\nid: x\nname: x\ntriggers: [图]\nsummary: x\n---\n正文\n",
        "empty_body.md": "---\nid: x\nname: x\ntriggers: [打药]\nsummary: x\n---\n",
        "bad_accept.md": "---\nid: x\nname: x\ntriggers: [打药]\nsummary: x\n"
                         "acceptance:\n  - question: 只有问题\n---\n正文\n",
    }
    for name, text in bad.items():
        _write(root, name, text)

    lib2 = SkillLibrary(mount_dirs=[root], known_tools=KNOWN)
    ids = sorted(s.id for s in lib2.all_skills())
    c.check("坏文件全部被跳过（只剩 good_skill）", ids == ["good_skill"], str(ids))
    c.check("每个坏文件都有错误记录（不是静默吞掉）",
            len(lib2.errors()) == len(bad), f"{len(lib2.errors())} vs {len(bad)}")
    c.check("错误记录里带文件名与原因",
            all(":" in e and ".md" in e for e in lib2.errors()), str(lib2.errors()[:3]))
    c.check("坏 YAML 也不会让加载器抛异常（已容错）", True)

    # ------------------------------------------------------------------
    c.section("第 3 节：★ 红线一 —— 不注册工具（契约单一真相不受影响）")
    # ------------------------------------------------------------------
    before = [t["function"]["name"] for t in TOOL_SCHEMAS]
    _write(root, "ghost_tool.md",
           "---\nid: ghost\nname: 幽灵工具\ntriggers: [打药]\n"
           "tools_used: [not_a_real_tool]\nsummary: x\n---\n正文\n")
    lib3 = SkillLibrary(mount_dirs=[root], known_tools=KNOWN)
    after = [t["function"]["name"] for t in TOOL_SCHEMAS]
    c.check("★ 加载技能**未改动** TOOL_SCHEMAS", before == after, f"{before} vs {after}")
    c.check("★ 引用不存在的工具 → 被拒（不进库）",
            "ghost" not in [s.id for s in lib3.all_skills()],
            str([s.id for s in lib3.all_skills()]))
    c.check("拒绝原因说清是哪个工具",
            any("not_a_real_tool" in e for e in lib3.errors()), str(lib3.errors()))

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 红线二 —— 不执行脚本（scripts/ 只是参考材料）")
    # ------------------------------------------------------------------
    boom_dir = root / "good_skill" / "scripts"
    boom_dir.mkdir(parents=True, exist_ok=True)
    (boom_dir / "boom.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('BOOM.txt').write_text('executed')",
        encoding="utf-8",
    )
    # scripts/ 下的 .md 也不该被当成技能
    (boom_dir / "README.md").write_text(
        "---\nid: should_not_load\nname: 说明\ntriggers: [打药]\nsummary: x\n---\n正文\n",
        encoding="utf-8",
    )
    lib4 = SkillLibrary(mount_dirs=[root], known_tools=KNOWN)
    c.check("★ 加载技能**未执行** scripts/ 里的脚本",
            not (boom_dir / "BOOM.txt").exists(), "BOOM.txt 被创建了 —— 脚本被执行了")
    c.check("★ scripts/ 下的 .md **不被当作技能**（只扫顶层）",
            "should_not_load" not in [s.id for s in lib4.all_skills()],
            str([s.id for s in lib4.all_skills()]))

    # ------------------------------------------------------------------
    c.section("第 5 节：★ 红线三 —— 不建索引 / 不写任何文件")
    # ------------------------------------------------------------------
    snap_before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    lib5 = SkillLibrary(mount_dirs=[root], known_tools=KNOWN)
    lib5.all_skills()
    lib5.match("今天适合打药吗")
    lib5.format_for_question("今天适合打药吗")
    snap_after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    c.check("★ 加载 / 匹配 / 渲染**都不写文件**", snap_before == snap_after,
            f"新增: {set(snap_after) - set(snap_before)}")
    c.check("挂载点内不存在 index / registry 类文件",
            not [p for p in root.rglob("*")
                 if p.is_file() and p.name.lower() in ("index.jsonl", "registry.json")])

    # 挂载点缺失不是错误，不阻断
    lib_missing = SkillLibrary(mount_dirs=[SANDBOX / "不存在"], known_tools=KNOWN)
    c.check("挂载点缺失 → 0 技能且无错误（不阻断启动）",
            lib_missing.all_skills() == [] and lib_missing.errors() == [])

    # ------------------------------------------------------------------
    c.section("第 6 节：匹配与排序（零 LLM，与结晶档同一判据）")
    # ------------------------------------------------------------------
    clean = SANDBOX / "clean"
    clean.mkdir(parents=True, exist_ok=True)
    _write(clean, "a.md",
           "---\nid: a\nname: 甲\ntriggers: [打药, 天气]\nsummary: s\n"
           "inject_priority: 10\n---\n正文A\n")
    _write(clean, "b.md",
           "---\nid: b\nname: 乙\ntriggers: [打药]\nsummary: s\n"
           "inject_priority: 99\n---\n正文B\n")
    lib6 = SkillLibrary(mount_dirs=[clean], known_tools=KNOWN)

    c.check("命中数多者优先（a 命中 2 > b 命中 1）",
            [s.id for s in lib6.match("今天天气适合打药吗")] == ["a", "b"],
            str([s.id for s in lib6.match("今天天气适合打药吗")]))
    c.check("命中数相同则比 inject_priority",
            [s.id for s in lib6.match("打药")] == ["b", "a"],
            str([s.id for s in lib6.match("打药")]))
    c.check("零命中 → 空", lib6.match("棉花补贴政策") == [])
    c.check("空问句 → 空", lib6.match("") == [])
    c.check("k=0 → 空", lib6.match("打药", k=0) == [])
    c.check("top-k 上限生效", len(lib6.match("打药", k=1)) == 1)

    # ------------------------------------------------------------------
    c.section("第 7 节：渲染 —— 反锁死 + 预算回落")
    # ------------------------------------------------------------------
    sec = lib6.format_section(lib6.match("今天天气适合打药吗"))
    c.check("渲染含段头（声明是「人写的处理路径」）",
            "【技能 · 编排指导】" in sec, sec[:40])
    c.check("渲染含技能名", "甲" in sec and "乙" in sec)
    c.check("渲染含反锁死声明", "以当前问题为准" in sec)
    c.check("空输入 → 空串", lib6.format_section([]) == "")

    # 超预算时回落到 summary
    big = SANDBOX / "big"
    big.mkdir(parents=True, exist_ok=True)
    _write(big, "huge.md",
           "---\nid: huge\nname: 巨技能\ntriggers: [打药]\nsummary: 只有这句摘要\n"
           "inject_priority: 50\n---\n" + ("很长的正文。" * 200) + "\n")
    _write(big, "huge2.md",
           "---\nid: huge2\nname: 巨技能二\ntriggers: [打药]\nsummary: 第二句摘要\n"
           "inject_priority: 40\n---\n" + ("很长的正文。" * 200) + "\n")
    lib7 = SkillLibrary(mount_dirs=[big], known_tools=KNOWN)
    sec7 = lib7.format_section(lib7.match("打药"))
    c.check("★ 超长正文回落为 summary（不撑爆 prompt）",
            "只有这句摘要" in sec7 and "很长的正文" not in sec7, sec7[:80])
    c.check("★ 段落总长不超硬上限", len(sec7) <= MAX_SKILL_SECTION_CHARS, str(len(sec7)))
    c.check("预算默认最多注入 %d 个技能" % MAX_SKILLS,
            len(lib7.match("打药")) <= MAX_SKILLS, str(len(lib7.match("打药"))))

    # ------------------------------------------------------------------
    c.section("第 8 节：真实技能目录（shipped_skill_dir）符合规范")
    # ------------------------------------------------------------------
    import config                                              # noqa: E402
    lib_real = SkillLibrary(mount_dirs=config.skill_mount_dirs())
    real_ids = [s.id for s in lib_real.all_skills()]
    c.check("真实 skills/ 里的技能都解析成功", lib_real.errors() == [], str(lib_real.errors()))
    c.check("真实技能带验收用例", all(s.acceptance for s in lib_real.all_skills()),
            str([(s.id, len(s.acceptance)) for s in lib_real.all_skills()]))
    c.check("真实技能的 tools_used 都是现有工具",
            all(t in KNOWN for s in lib_real.all_skills() for t in s.tools_used),
            str(real_ids))
    c.check("真实技能能被触发词命中",
            "spray_timing" in [s.id for s in lib_real.match("今天阿拉尔适合打药吗")],
            str(real_ids))

    # ------------------------------------------------------------------
    c.section("第 9 节：★ 阶段三 —— 外部挂载的信任对策（对准提示注入，而非代码执行）")
    # ------------------------------------------------------------------
    # 在「提示注入」架构下，第三方技能**不执行任何代码**，唯一威胁就是往 prompt 注入文本。
    # 因此对策不是 permissions.yaml（权限声明无所指），而是：配额 + 来源标注。
    shipped_dir = SANDBOX / "tier_shipped"
    ext_dir = SANDBOX / "tier_external"
    shipped_dir.mkdir(parents=True, exist_ok=True)
    ext_dir.mkdir(parents=True, exist_ok=True)

    _write(shipped_dir, "s1.md",
           "---\nid: s1\nname: 可信技能\ntriggers: [打药]\nsummary: 可信摘要\n"
           "inject_priority: 1\n---\n正文S\n")
    _write(ext_dir, "e1.md",
           "---\nid: e1\nname: 外部技能一\ntriggers: [打药, 天气]\nsummary: 外部摘要一\n"
           "inject_priority: 90\n---\n正文E1\n")
    _write(ext_dir, "e2.md",
           "---\nid: e2\nname: 外部技能二\ntriggers: [打药, 天气]\nsummary: 外部摘要二\n"
           "inject_priority: 80\n---\n正文E2\n")

    lib9 = SkillLibrary(mount_dirs=[shipped_dir, ext_dir], known_tools=KNOWN)
    c.check("两个挂载点分别标记来源",
            sorted((s.id, s.origin) for s in lib9.all_skills())
            == [("e1", "external"), ("e2", "external"), ("s1", "shipped")],
            str(sorted((s.id, s.origin) for s in lib9.all_skills())))

    # 命中数多者在前，但外部占槽有配额
    hits9 = lib9.match("今天天气适合打药吗")
    c.check("★ 外部技能配额生效：只占 %d 个槽" % MAX_EXTERNAL_SKILLS,
            sum(1 for s in hits9 if s.origin == "external") <= MAX_EXTERNAL_SKILLS,
            str([(s.id, s.origin) for s in hits9]))
    c.check("★ 被挤掉的是外部技能（e2），不是可信技能（s1）",
            [s.id for s in hits9] == ["e1", "s1"], str([s.id for s in hits9]))

    # 命中数相同时，可信优先
    tie_shipped = SANDBOX / "tie_s"
    tie_ext = SANDBOX / "tie_e"
    tie_shipped.mkdir(parents=True, exist_ok=True)
    tie_ext.mkdir(parents=True, exist_ok=True)
    _write(tie_shipped, "t1.md",
           "---\nid: t1\nname: 甲\ntriggers: [打药]\nsummary: s\ninject_priority: 0\n---\n正文\n")
    _write(tie_ext, "t2.md",
           "---\nid: t2\nname: 乙\ntriggers: [打药]\nsummary: s\ninject_priority: 999\n---\n正文\n")
    lib9b = SkillLibrary(mount_dirs=[tie_shipped, tie_ext], known_tools=KNOWN)
    c.check("命中数相同时**可信来源优先**（即便外部 priority 更高）",
            [s.id for s in lib9b.match("打药")] == ["t1", "t2"],
            str([s.id for s in lib9b.match("打药")]))

    # 注入时外部技能必须带来源标注
    sec_ext = lib9.format_section([s for s in lib9.all_skills() if s.id == "e1"])
    sec_shipped = lib9.format_section([s for s in lib9.all_skills() if s.id == "s1"])
    c.check("★ 外部技能注入时**标注来源与未审查**",
            "未经本项目审查" in sec_ext, sec_ext[:120])
    c.check("可信技能**不**带该标注", "未经本项目审查" not in sec_shipped, sec_shipped[:120])
    c.check("标注不撑破长度上限", len(sec_ext) <= MAX_SKILL_SECTION_CHARS, str(len(sec_ext)))

    # 未配置外部挂载时，一切照旧（当前生产状态）
    lib9c = SkillLibrary(mount_dirs=[shipped_dir], known_tools=KNOWN)
    c.check("只有一个挂载点时无外部技能",
            all(s.origin == "shipped" for s in lib9c.all_skills()),
            str([(s.id, s.origin) for s in lib9c.all_skills()]))

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"技能加载器守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())