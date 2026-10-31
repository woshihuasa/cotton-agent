# -*- coding: utf-8 -*-
"""当日价格工具守卫 —— 抓得到、认得清、取不到就报错。

【为什么单独一节】这条链上的错误都是"看起来正常"的那种：

  ① **解析错位**：页面上有两个都叫 `3128B` 的指数（全国 CC Index 与新疆指数），
     数值差几百元/吨。取错一个，涨跌结论可能是反的。
  ② **失败当成功**：抓不到数时如果返回一句话而不是 `{"error": ...}`，
     工作流会把这句话当正文推出去，用户以为那就是今天的行情。
  ③ **登记漏项**：工具要同时出现在 contract / handler / capability 三处，
     漏一处就是"模型能看见但调不动"或者反过来。

【零网络】HTTP 层全部用 monkeypatch 替掉，本文件不发任何请求。
【用法】python tests/check_cotton_index.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="cotton_test_price_"))
os.environ["COTTON_AGENT_DATA_DIR"] = str(SANDBOX)

from core import schedule as S                                   # noqa: E402
from core.orchestration import catalog, engine as E              # noqa: E402
from core.orchestration import loader, validator                 # noqa: E402
from core.tools import TOOL_HANDLERS, TOOL_SCHEMAS               # noqa: E402
from core.tools import capabilities as caps                      # noqa: E402
from core.tools import market                                    # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, desc: str, cond: object, detail: str = "") -> bool:
        self.total += 1
        ok = bool(cond)
        self.passed += int(ok)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {desc}"
              + (f"   ← {detail}" if detail and not ok else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print("=" * 78)
        print(title)
        print("=" * 78)


# ---------------------------------------------------------------------------
# 真实页面片段（摘自 2026-10-31 的 cottonchina.org.cn 首页）
# ---------------------------------------------------------------------------

BLOCK_NATIONAL = """<div class='nd_cotton_block'>
<div class='nd_block_t'>CC Index 3128B</div>
<div class='nd_block_m'><a href='https://www.cottonchina.org.cn/newprice/ccindex_more.php' target='_blank'>更多</a></div>
<div class='nd_block_d'>2026-10-30</div>
<div class='nd_block_p'><span>16762</span> 元/吨</div>
<div class='p_color_g'>-48</div>
</div>
"""

BLOCK_CF = """<div class='nd_cotton_block'>
<div class='nd_block_t'>CF701</div>
<div class='nd_block_m'><a href='#'>更多</a></div>
<div class='nd_block_d'>2026-10-30</div>
<div class='nd_block_p'><span>15255</span> 元/吨</div>
<div class='p_color_r'>+85</div>
</div>
"""

BLOCK_EMPTY = """<div class='nd_cotton_block'>
<div class='nd_block_t'>内外棉价差</div>
<div class='nd_block_m'><a href='#'>更多</a></div>
<div class='nd_block_d'>今日数据暂未更新</div>
<div class='nd_block_p'><span>-</span> 元/吨</div>
<div class='p_color_g'>-20</div>
</div>
"""

XINJIANG = """<div class="newdata_t">
<div class="nd_t_bt1">新疆棉花棉纱价格指数 <span style='font-size:16px;'>2026年10月29日</span></div>
</div>
<div class="xj_zhishu"><div class="cardbox">
<div class="card1">
<div class="pinzhong">3128B</div>
<div class="price">16204 <span>元/吨</span></div>
<div class="change"><span style='color:green;'>-81</span> <span style='padding-left: 12px;'> -0.50%</span></div>
</div>
<div class="card1">
<div class="pinzhong">3129B</div>
<div class="price">16302 <span>元/吨</span></div>
<div class="change"><span style='color:green;'>-87</span></div>
</div>
</div></div>
"""

PAGE = BLOCK_NATIONAL + BLOCK_CF + BLOCK_EMPTY + XINJIANG


def section_parse(c: Checker) -> None:
    c.section("第 1 节：解析（两个都叫 3128B 的指数必须分得开）")

    blocks = market.parse_homepage(PAGE)
    national = blocks.get("CC Index 3128B")
    c.check("★ 取到全国 CC Index 3128B", national is not None, str(sorted(blocks)))
    c.check("★ 全国口径数值正确（16762 / -48）",
            national and national["value"] == 16762 and national["change"] == -48,
            str(national))
    c.check("全国口径带日期", national and national["date"] == "2026-10-30", str(national))

    cf = blocks.get("CF701")
    c.check("取到郑棉 CF701（15255 / +85）",
            cf and cf["value"] == 15255 and cf["change"] == 85, str(cf))

    c.check("★ 当日未更新的条目（值是「-」）被跳过，不当成 0",
            "内外棉价差" not in blocks, str(sorted(blocks)))

    xj = market.parse_xinjiang(PAGE)
    c.check("★ 取到新疆指数 3128B（16204 / -81）",
            xj and xj["value"] == 16204 and xj["change"] == -81, str(xj))
    c.check("新疆指数的日期从栏目抬头取（2026-10-29）",
            xj and xj["date"] == "2026-10-29", str(xj))
    c.check("★ 疆内与全国是两个数，不会被解析成一个",
            national["value"] != xj["value"], f"{national['value']} vs {xj['value']}")

    c.check("空页面 → 什么都取不到（不抛）", market.parse_homepage("") == {})
    c.check("空页面的疆内指数返回 None", market.parse_xinjiang("") is None)
    c.check("结构变了（没有 nd_block_t）→ 空结果",
            market.parse_homepage("<html><body>改版了</body></html>") == {})


def section_format(c: Checker) -> None:
    c.section("第 2 节：正文排版（三行、千分位、带正负号）")

    data = {
        "national": {"value": 16762, "change": -48, "date": "2026-10-30"},
        "xinjiang": {"value": 16204, "change": -81, "date": "2026-10-29"},
        "futures": {"value": 15255, "change": 85, "date": "2026-10-30"},
    }
    text = market.format_summary(data)
    lines = text.splitlines()
    c.check("恰好三行", len(lines) == 3, text)
    c.check("★ 三行分别标了口径（全国 / 新疆 / 郑棉）",
            all(k in text for k in ("全国", "新疆", "郑棉")), text)
    c.check("数值带千分位", "16,762" in text and "16,204" in text, text)
    c.check("★ 涨跌带符号（-48 / +85）", "（-48）" in text and "（+85）" in text, text)
    c.check("每行带日期", "2026-10-30" in text and "2026-10-29" in text, text)

    missing = market.format_summary({"national": {"value": None}})
    c.check("缺失的值显示成 —，不显示 0", "—" in missing, missing)


def section_fail(c: Checker) -> None:
    c.section("第 3 节：★ 取不到就报错，不给半个结果")

    class _Resp:
        def __init__(self, content: bytes) -> None:
            self.content = content
        def raise_for_status(self) -> None:
            return None

    real_get = market.requests.get

    # ① 网络异常
    def _boom(*_a, **_k):
        raise market.requests.RequestException("连接被重置")
    market.requests.get = _boom
    try:
        raw = market.get_cotton_index({})
    finally:
        market.requests.get = real_get
    data = json.loads(raw)
    c.check("★ 网络异常 → 返回 error", bool(data.get("error")), raw[:80])
    c.check("网络异常时 summary 为空（不会被当成正文推出去）",
            data.get("summary") == "", str(data.get("summary")))

    # ② 页面改版（拿不到 3128B）
    market.requests.get = lambda *a, **k: _Resp(
        "<html><body>改版了</body></html>".encode("utf-8"))
    try:
        raw = market.get_cotton_index({})
    finally:
        market.requests.get = real_get
    data = json.loads(raw)
    c.check("★ 页面结构变了 → 返回 error 而不是空数据",
            "解析不到" in str(data.get("error")), str(data.get("error")))

    # ③ 正常页面
    market.requests.get = lambda *a, **k: _Resp(PAGE.encode("gb18030"))
    try:
        raw = market.get_cotton_index({})
    finally:
        market.requests.get = real_get
    data = json.loads(raw)
    c.check("正常页面 → 没有 error", not data.get("error"), str(data.get("error"))[:80])
    c.check("正常页面 → summary 是三行", len(str(data.get("summary")).splitlines()) == 3,
            str(data.get("summary"))[:60])
    c.check("正常页面 → 疆内取不到时也不编数（这里是取到的）",
            data.get("xinjiang", {}).get("value") == 16204,
            str(data.get("xinjiang")))


def section_registry(c: Checker) -> None:
    c.section("第 4 节：三处登记必须一致")

    names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    c.check("★ 契约里有 get_cotton_index", "get_cotton_index" in names, str(names))
    c.check("★ 实现里有", "get_cotton_index" in TOOL_HANDLERS, str(sorted(TOOL_HANDLERS)))
    c.check("★ 能力注解里有（缺了就是「未声明＝可破坏」，网页工具会被当成写盘工具）",
            "get_cotton_index" in caps.CAPABILITIES, str(sorted(caps.CAPABILITIES)))
    c.check("三处键集完全相等",
            set(names) == set(TOOL_HANDLERS) == set(caps.CAPABILITIES),
            f"{len(names)} / {len(TOOL_HANDLERS)} / {len(caps.CAPABILITIES)}")

    cap = caps.CAPABILITIES.get("get_cotton_index")
    c.check("它被声明为只读 + 联网", cap and cap.read_only and cap.open_world,
            str(cap))

    spec = catalog.get("tool.get_cotton_index")
    c.check("★ 节点目录里有 tool.get_cotton_index", spec is not None, "")
    c.check("编排层派到工具执行器",
            E.executor_for("tool.get_cotton_index") is E._exec_tool, "")


def section_engine(c: Checker) -> None:
    c.section("第 5 节：★ 工具报错要变成节点失败（不然会推一条假消息）")

    class _Res:
        def __init__(self, text: str) -> None:
            self.text = text
            self.images = []

    import core.tools as tools_mod
    orig = tools_mod.execute_tool_result

    wf = loader.load_text("""
id: price_probe
nodes:
  - { id: index, type: tool.get_cotton_index, params: {}, side_effect: network }
""")

    tools_mod.execute_tool_result = lambda name, args: _Res(
        json.dumps({"error": "指数页解析不到「CC Index 3128B」", "summary": ""},
                   ensure_ascii=False))
    try:
        run = E.Engine(wf, persist=False).run()
    finally:
        tools_mod.execute_tool_result = orig

    c.check("★ 工具返回 error → 整条工作流失败", run.state == E.StepState.FAILED,
            run.state.value)
    c.check("★ 失败原因原样保留（不是含糊的「未知错误」）",
            "解析不到" in run.steps["index"].error, run.steps["index"].error[:60])

    # 反向：正常返回 → 成功，且 summary 出现在产出里
    tools_mod.execute_tool_result = lambda name, args: _Res(
        json.dumps({"national": {"value": 16762}, "summary": "三行正文"},
                   ensure_ascii=False))
    try:
        run2 = E.Engine(wf, persist=False).run()
    finally:
        tools_mod.execute_tool_result = orig
    c.check("★ 反向验证：正常返回时节点成功（上面的断言不是恒真）",
            run2.state == E.StepState.DONE, run2.state.value)
    c.check("产出里有 summary（推送正文从它取）",
            (run2.steps["index"].output or {}).get("summary") == "三行正文",
            str(run2.steps["index"].output)[:80])


def section_job(c: Checker) -> None:
    c.section("第 6 节：工作流与出厂任务")

    path = ROOT / "workflows" / "price_brief.yaml"
    c.check("price_brief.yaml 存在", path.is_file())
    wf = loader.load_file(path)
    issues = validator.validate(wf)
    c.check("★ 静态校验通过", not issues, validator.format_issues(issues))
    c.check("只有一个节点（这条推送不需要模型）",
            len(wf.nodes) == 1 and wf.nodes[0].type == "tool.get_cotton_index",
            str([n.type for n in wf.nodes]))
    c.check("节点声明了 network 副作用",
            wf.nodes[0].side_effect is not None, "")

    jobs = [j for j in S.default_jobs() if j.id == "price_daily"]
    c.check("出厂任务里有 price_daily", bool(jobs), "")
    if jobs:
        job = jobs[0]
        c.check("★ 每天 17:00（收盘后）", job.trigger.kind == "daily"
                and job.trigger.at == "17:00", str(job.trigger))
        c.check("★ 推送正文取 index.summary", job.notice_node == "index"
                and job.notice_field == "summary",
                f"{job.notice_node}.{job.notice_field}")
        c.check("配置合法", job.ok, job.validate())
        c.check("不依赖知识库（这条链只取数）",
                not any(n.type == "data.kb_query"
                        for n in loader.load_file(path).nodes), "")


def main() -> int:
    print(f"沙箱目录：{SANDBOX}")
    c = Checker()
    try:
        section_parse(c)
        section_format(c)
        section_fail(c)
        section_registry(c)
        section_engine(c)
        section_job(c)
    finally:
        print()
        print("=" * 78)
        print(f"当日价格工具守卫: {c.passed}/{c.total} 通过")
        print("=" * 78)
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())