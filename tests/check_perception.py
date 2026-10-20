# -*- coding: utf-8 -*-
"""感知接入守卫（`docs/PERCEPTION.md` V1 的第 1–8 步）。

【它守什么】
    V1 把感知层接进项目：三模块迁入 → 契约 → 适配器 → 任务执行器
    → 编排节点 → 示例工作流 → 报告 → 对话入口。

【为什么每条都要"真跑"】
    这个项目的纪律是 **"写了没跑过的守卫等于不存在"**。
    更早的教训是 U1 的门控第一版形同虚设（把 ε=0 算进可用区），
    是守卫真跑才抓出来的。

    所以本文件**尽可能用感知侧的真函数**（`calibrate()` / `compute_density()`）
    而不是假数据 —— 因为契约/适配/管线三层的价值全在"能不能对上真东西"。

【用法】python tests/check_perception.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ★★ 数据隔离（必须在 import core.* 之前设置）——
#   开发模式下数据目录固定为项目根，不隔离的话跑守卫会往**真实数据**里写：
#   会话（session_state.json）、L4 记忆（chroma_user_memory/）全都污染。
#   实测过一次：几轮下来多出 85 个空会话。
#
# ★ 用**固定路径**而不是 mkdtemp：
#   ① mkdtemp 每次新建一个目录，跑 N 次就留 N 个（实测累积到 12 个）；
#   ② atexit 清理在 Windows 上不可靠 —— chromadb 的 sqlite 句柄还没释放，
#      rmtree 失败而 ignore_errors 把它吞掉，看起来清理了其实没有。
#   改成固定路径 + **启动时清空**，残留最多一个，且每次运行都是干净状态。
import os as _os
import shutil as _shutil
from pathlib import Path as _Path

_TEST_DIR = _Path(__file__).resolve().parent / "eval_outputs" / "_tmp_data"
#   ↑ 落在 tests/eval_outputs/ 下，已被 .gitignore 覆盖
_shutil.rmtree(_TEST_DIR, ignore_errors=True)
_TEST_DIR.mkdir(parents=True, exist_ok=True)
_os.environ.setdefault("COTTON_AGENT_DATA_DIR", str(_TEST_DIR))

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


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


def _make_calibration(path: Path) -> object:
    """用**感知侧真 `calibrate()`** 造一个校准模型并落盘。"""
    import numpy as np

    from core.perception import calibrate as P

    rng = np.random.default_rng(7)
    truth = rng.uniform(50, 200, 80)
    detected = truth * 0.75 * np.exp(rng.normal(0, 0.18, 80))
    pts = [P.CalibrationPoint(detected=float(d), truth=float(t))
           for d, t in zip(detected, truth)]
    model = P.calibrate(pts, domain="mavic3m-5m", alpha=0.10)
    path.write_text(json.dumps(model.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return model


def main() -> int:
    c = Checker()

    # ==================================================================
    c.section("第 1 步：★ 三个模块原样迁入，自检在新位置仍通过")
    # ==================================================================
    perc = ROOT / "core" / "perception"
    for name in ("infer.py", "geometry.py", "calibrate.py", "config.yaml"):
        c.check(f"已迁入 {name}", (perc / name).is_file())

    # 真跑三个自检（子进程，与用户手动跑等价）
    for mod in ("infer.py", "geometry.py", "calibrate.py"):
        # ★ 必须显式指定 UTF-8：Windows 上子进程默认用 cp936，
        #   中文输出会被打乱，导致 "全部通过" 匹配不到（退出码却是 0）
        _env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, mod, "--selftest"],
                           cwd=str(perc), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=_env, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        c.check(f"★★ {mod} --selftest 通过（退出码 {r.returncode}）",
                r.returncode == 0 and "全部通过" in out, out[-160:])

    # ==================================================================
    c.section("第 2 步：★ 契约 15 字段 + 校验区分 error/warning")
    # ==================================================================
    from core.perception.contract import InferenceSpec, PerceptionRecord

    empty = PerceptionRecord()
    e0, w0 = empty.validate()
    c.check("★ 空记录：缺密度是 error", any("密度" in x for x in e0), str(e0[:2]))
    c.check("★ 空记录：缺 field_id 只是 warning",
            any("field_id" in x for x in w0))
    c.check("★ 空记录不可决策", empty.is_decidable is False)

    spec = InferenceSpec(640, 0.15, 640, 0.25, 0.5, "v1")
    full = PerceptionRecord(
        field_id="F03", date="2025-10-17", platform="Mavic3M", altitude_m=4.2,
        detector_id="YOLO", inference_spec=spec,
        density_hat_per_m2=312.0, density_interval_per_m2=(268.0, 358.0),
        imaged_area_m2=1850.0, field_area_m2=2100.0,
        area_coverage_fraction=0.88, valid_frame_ratio=0.91,
        alpha=0.10, calibration_id="cal-x", calibration_reliable=True,
        calibration_domain="mavic3m-5m")
    ef, wf = full.validate()
    c.check("★★ 完整记录：无 error 无 warning", not ef and not wf,
            f"{ef} {wf}")
    c.check("★ 完整记录可决策", full.is_decidable)
    c.check("★★ to_dict 可 JSON 序列化",
            bool(json.dumps(full.to_dict(), ensure_ascii=False)))

    # 区间反向必须被拦
    bad = PerceptionRecord(density_hat_per_m2=10.0,
                           density_interval_per_m2=(20.0, 5.0),
                           alpha=0.1, calibration_id="x")
    eb, _ = bad.validate()
    c.check("★★ 区间反向（lo>hi）是 error",
            any("反向" in x for x in eb), str(eb))

    # reliable=False 必须产生显著警告
    rel = PerceptionRecord(density_hat_per_m2=10.0,
                           density_interval_per_m2=(8.0, 12.0),
                           alpha=0.1, calibration_id="x",
                           calibration_reliable=False)
    _, wr = rel.validate()
    c.check("★★★ reliable=False 必须产生警告（小样本区间无意义）",
            any("样本量不足" in x for x in wr), str(wr))

    # ==================================================================
    c.section("第 3 步：★★ 适配器用真函数拼装，缺字段要点名")
    # ==================================================================
    from core.perception.adapter import AdapterError, to_record

    with tempfile.TemporaryDirectory() as td:
        cal_file = Path(td) / "cal.json"
        model = _make_calibration(cal_file)

        from core.perception import geometry as G

        dr = G.DensityResult(
            n_boxes=1414, imaged_area_m2=30.54, density_per_m2=46.302,
            gsd_m_per_px=0.00122, altitude_m=5.0,
            field_area_m2=None, area_coverage_fraction=None)

        class _Tile:
            def __init__(self, ok):
                self.valid = ok

        class _Img:
            def __init__(self, n_ok, n_bad):
                self.tiles = [_Tile(True)] * n_ok + [_Tile(False)] * n_bad

        rec = to_record(density_result=dr, calibration=model,
                        image_result=_Img(91, 9), field_id="F03")
        c.check("★ 有效帧占比算对（91/100）",
                abs((rec.valid_frame_ratio or 0) - 0.91) < 1e-9,
                str(rec.valid_frame_ratio))
        # ★★ 点估计必须是"区间中心"（偏置校正后），不是原始检测密度
        lo, hi = rec.density_interval_per_m2
        c.check("★★★ 点估计落在区间内（不是原始检测密度）",
                lo <= rec.density_hat_per_m2 <= hi,
                f"hat={rec.density_hat_per_m2} 区间=[{lo:.1f},{hi:.1f}]")
        c.check("★ 点估计取几何中心 sqrt(lo*hi)",
                abs(rec.density_hat_per_m2 - (lo * hi) ** 0.5) < 1e-6)
        c.check("★★ 区间来自真 calibrate()（区间非退化）", hi > lo)

        # 缺必需对象 → 点名
        try:
            to_record(density_result=None, calibration=model)
            c.check("★★ 缺 density_result 必须报错", False)
        except AdapterError as ex:
            c.check("★★ 缺 density_result 必须报错", "密度" in str(ex), str(ex))

        class _Broken:
            pass

        try:
            to_record(density_result=_Broken(), calibration=model)
            c.check("★★ 对象缺字段要**点名**缺哪个", False)
        except AdapterError as ex:
            c.check("★★ 对象缺字段要**点名**缺哪个",
                    "density_per_m2" in str(ex), str(ex))

        # ==============================================================
        c.section("第 5/6 步：★★★ 工作流端到端（真密度 + 真区间 + 真报告）")
        # ==============================================================
        from core.task_runner import TaskRunner, load_and_validate

        yml = (ROOT / "workflows" / "perception_report.yaml").read_text(
            encoding="utf-8")
        wf, issues = load_and_validate(yml)
        c.check("★★ 示例工作流静态校验通过", wf is not None and not issues,
                str(issues[:2]))

        evs: list[str] = []
        runner = TaskRunner(on_event=lambda e: evs.append(e.kind))
        out_dir = Path(td) / "reports"
        h = runner.submit_yaml(yml, {
            "field_id": "2025-F03", "n_boxes": 1414, "altitude_m": 5.0,
            "threshold": 75.0, "calibration_path": str(cal_file),
            "camera": "Mavic3Classic", "date": "2025-10-17",
            "platform": "Mavic3M", "detector_id": "YOLO-Cotton-v1",
            "invalid_tiles": 8, "out_dir": str(out_dir),
        })
        c.check("★ 提交后真的启动了线程", h.launched, str(h.issues))
        run = runner.wait(h.run_id, timeout=180)
        state = getattr(getattr(run, "state", None), "name", str(run))
        c.check("★★★ 工作流跑到 DONE", state == "DONE", state)
        c.check("★ 事件序列完整（started…finished）",
                evs[0] == "started" and "finished" in evs, str(evs))

        steps = getattr(run, "steps", {})
        c.check("★★ 三步全部 DONE",
                all(getattr(steps.get(n), "state", None) is not None
                    and steps[n].state.name == "DONE"
                    for n in ("record", "gate", "report")),
                str({n: getattr(steps.get(n), "state", None) for n in
                     ("record", "gate", "report")}))

        gate_out = (getattr(steps.get("gate"), "output", None) or {})
        c.check("★★ 门控产出了四档结论",
                gate_out.get("level") in
                ("ADVISE", "ADVISE_WITH_INTERVAL", "QUALITATIVE", "ESCALATE"),
                str(gate_out.get("level")))
        # 本例：区间 [53,93.5] 跨阈值 75 且宽 58% → 必须 ESCALATE
        c.check("★★★ 跨阈值且过宽 → ESCALATE（不是 ADVISE）",
                gate_out.get("level") == "ESCALATE", str(gate_out.get("level")))

        rep_out = (getattr(steps.get("report"), "output", None) or {})
        md_path = Path(rep_out.get("path") or "")
        c.check("★★ 报告文件真的写出来了", md_path.is_file())
        md = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
        c.check("★★★ 报告含「决策结论」段（不是只有数字）", "决策结论" in md)
        c.check("★★ 报告写明档位", "ESCALATE" in md)
        c.check("★★ 报告同时给点估计与区间（不能只给一个数）",
                "密度点估计" in md and "校准区间" in md)
        c.check("★ 报告写明校准外键（可追溯）",
                "校准 ID" in md and "cal-" in md)
        c.check("★ 报告含边界声明（V1 不含真推理）",
                "不含真实 YOLO 推理" in md or "V1" in md)

        # ==============================================================
        c.section("第 4 步：★★ 任务执行器 —— 不阻塞、坏流程不启动")
        # ==============================================================
        import time as _t

        bad_yaml = "id: x\nnodes:\n  - { id: a, type: 瞎写, params: {} }\n"
        r2 = TaskRunner(on_event=lambda e: None)
        h2 = r2.submit_yaml(bad_yaml)
        c.check("★★★ 坏 YAML 不启动线程（不留'看起来在跑'的空任务）",
                h2.launched is False and bool(h2.issues), str(h2.issues[:1]))

        # 不阻塞：submit 后立刻返回，wait 之前线程仍在跑
        t0 = _t.time()
        h3 = r2.submit_yaml(yml, {
            "field_id": "T", "n_boxes": 100, "altitude_m": 5.0,
            "threshold": 75.0, "calibration_path": str(cal_file),
            "out_dir": str(out_dir)})
        submit_cost = _t.time() - t0
        c.check("★★ submit 立即返回（<2s，不阻塞调用方）",
                submit_cost < 2.0, f"{submit_cost:.2f}s")
        r2.wait(h3.run_id, timeout=180)

    # ==================================================================
    c.section("第 7 步：★★★ 显式感知意图优先，handoff 让位")
    # ==================================================================
    from core.perception import intent

    for q, want in [
        ("/perception 分析这批影像", True),
        ("/感知 n_boxes=1414", True),
        ("/PC", True),
        ("   /Perception   ", True),
        # ★ 下面这些**必须不命中** —— 词面判定一旦放宽就会误伤
        ("分析这批影像", False),
        ("影像质量对检测的影响是什么", False),
        ("perception 是什么", False),
        ("帮我看看这块地能不能收", False),
    ]:
        got = intent.detect(q) is not None
        c.check(f"★ detect({q!r}) == {want}", got == want, f"实际 {got}")

    r = intent.detect("/感知 n_boxes=1414 altitude=5")
    c.check("★ 指令参数被解析成键值",
            r is not None and r.params.get("n_boxes") == "1414",
            str(r.params if r else None))

    # ★★★ 端到端：显式指令时 handoff 不触发
    import json as _json

    class _Msg:
        def __init__(self, content="", n=0):
            self.content = content
            self.reasoning_content = None
            self.tool_calls = []
            for i in range(n):
                tc = type("TC", (), {})()
                tc.id = f"c{i}"
                tc.function = type("F", (), {})()
                tc.function.name = "get_weather"
                tc.function.arguments = _json.dumps({"city": "阿克苏"})
                self.tool_calls.append(tc)

    class _LLM:
        def __init__(self):
            self._model = "fake"
            self.last_usage = None

        def generate_response(self, messages, model_name=None, **kw):
            return "能不能收"

        def chat_with_tools(self, messages, tools, model_name=None):
            return _Msg("", n=1)          # 永远要工具 → 一定跑满

        def stream_response(self, messages, model_name=None,
                            enable_thinking=False, reasoning_effort=None):
            yield ("content", "（桩）")

    from core.rag_engine import RAGEngine

    try:
        eng = RAGEngine()
        eng.create_session()
        eng.llm = _LLM()
        eng._owns_user_data = True
        eng._execute_tool = lambda n, a: '{"ok": true, "text": "桩"}'

        # ① 显式指令 → 走感知路径，handoff 让位
        txt = "".join(ch for k, ch in eng.ask("/perception 分析这批影像")
                      if k == "content")
        c.check("★★★ 显式指令时**没有**出现〔部分结果〕（handoff 让位）",
                "〔部分结果〕" not in txt, txt[-120:])
        c.check("★★ 显式指令时给出感知任务提示", "感知分析任务" in txt,
                txt[-120:])

        # ② 普通问题 → handoff 照常触发（对照组）
        eng.create_session()
        txt2 = "".join(ch for k, ch in eng.ask("这块地能不能收")
                       if k == "content")
        c.check("★★★ 对照组：普通问题仍触发 handoff（〔部分结果〕）",
                "〔部分结果〕" in txt2, txt2[-120:])
    except Exception as e:                                   # noqa: BLE001
        c.check("★★★ 对话入口的意图分流跑通", False,
                f"{type(e).__name__}: {e}")

    # ==================================================================
    c.section("附加：★★ 感知节点**没有**注册成 LLM 工具")
    # ==================================================================
    from core.tools import TOOL_SCHEMAS

    names = [(s.get("function", s) or {}).get("name") for s in TOOL_SCHEMAS]
    c.check("★★ LLM 工具清单里没有 perception（避免必然失败的调用）",
            not any("perception" in str(n) for n in names), str(names))

    from core.orchestration import catalog

    c.check("★ 但编排层能识别 perception.record",
            catalog.get("perception.record") is not None)
    c.check("★ 且它在 EXECUTABLE_TYPES 白名单里",
            "perception.record" in catalog.EXECUTABLE_TYPES)
    # ★★ 两份登记必须一致（本项目一直在防「两份地图」）
    from core.orchestration import engine as _eng

    for t in ("perception.record", "report.markdown"):
        c.check(f"★★ {t}：白名单与执行器表**两处一致**",
                (t in catalog.EXECUTABLE_TYPES) and
                (_eng.executor_for(t) is not None))

    # ==================================================================
    c.section("第 9 步：★★★ `/perception` 真启动（不再只是打印提示）")
    # ==================================================================
    # 【这一步补的是什么】
    #   此前 `/perception` 只做一件事：跳过 handoff，然后**打印一段提示**。
    #   等于"入口"是假的。现在它会**真的在后台跑工作流**。

    from core.perception import launch as _pl
    from core.perception import intent as _pi2

    # ① 缺必需参数 → **不启动**，且点名缺什么
    _r_missing = _pl.build_inputs(_pi2.detect("/perception n_boxes=100"))
    c.check("★★★ 缺必需参数时不启动（ok=False）", _r_missing.ok is False)
    c.check("★★ 且**点名**缺哪几个参数",
            set(_r_missing.missing) == {"altitude_m", "threshold",
                                        "calibration_path"},
            str(_r_missing.missing))

    # ② 给全参数 → 组装出正确的输入
    _q_full = ("/perception field_id=F1 n_boxes=1414 altitude_m=5 "
               "threshold=75 calibration_path=cal.json")
    _r_full = _pl.build_inputs(_pi2.detect(_q_full))
    c.check("★★ 参数齐全时组装成功（真实路径，非演示）",
            _r_full.ok is True and _r_full.demo is False)
    c.check("★ 输入里带着用户给的数值与校准路径",
            _r_full.inputs.get("n_boxes") == 1414
            and _r_full.inputs.get("calibration_path") == "cal.json",
            str(_r_full.inputs)[:90])

    # ③ ★★ 演示路径**必须显式索取**（否则就是"用假数据演示真机制"）
    _r_plain = _pl.build_inputs(_pi2.detect("/perception"))
    c.check("★★★ 不写 --demo 时**不会**走演示路径",
            _r_plain.demo is False and _r_plain.ok is False,
            f"demo={_r_plain.demo} ok={_r_plain.ok}")
    _r_demo = _pl.build_inputs(_pi2.detect("/perception --demo"))
    c.check("★★ `--demo` 显式索取才走演示路径",
            _r_demo.ok is True and _r_demo.demo is True)

    # ④ ★★★ 演示校准必须**自带标记**（能被下游识别）
    from core.perception import demo as _pd
    c.check("★★★ 合成校准的 calibration_id 带 `cal-demo-` 前缀",
            _pd.DEMO_PREFIX == "cal-demo-synthetic")
    _m = _pd.make_demo_calibration()
    c.check("★★★ 生成的模型 id 确实带前缀（不是只在常量里）",
            _pd.is_demo(_m.calibration_id), _m.calibration_id)
    c.check("★ 且对真实校准返回 False（不误判）",
            _pd.is_demo("cal-2025-3to5m-mavic3m") is False)

    # ⑤ ★★★ 端到端：真启动 → 真出报告
    import tempfile as _tf9

    from core.task_runner import TaskRunner as _TR9

    with _tf9.TemporaryDirectory() as _td9:
        _runner9 = _TR9(on_event=lambda e: None)
        _res9 = _pl.launch(_pi2.detect("/perception --demo"),
                           runner=_runner9, out_dir=_td9)
        c.check("★★★ launch() 真的启动了工作流",
                _res9.ok is True and bool(_res9.run_id), _res9.message)
        _run9 = _runner9.wait(_res9.run_id, timeout=180)
        _st9 = getattr(getattr(_run9, "state", None), "name", str(_run9))
        c.check("★★★ 工作流跑到 DONE", _st9 == "DONE", _st9)

        _steps9 = getattr(_run9, "steps", {}) or {}
        _g9 = (getattr(_steps9.get("gate"), "output", None) or {})
        c.check("★★★ 门控产出了四档结论",
                _g9.get("level") in ("ADVISE", "ADVISE_WITH_INTERVAL",
                                     "QUALITATIVE", "ESCALATE"),
                str(_g9.get("level")))

        _rep9 = (getattr(_steps9.get("report"), "output", None) or {})
        _p9 = Path(_rep9.get("path") or "")
        c.check("★★★ 报告文件真的落盘", _p9.is_file(), str(_p9))
        _md9 = _p9.read_text(encoding="utf-8") if _p9.is_file() else ""

        # ★★★ 最关键：合成校准**必须在报告里被显著标注**
        c.check("★★★ 报告显著标注「使用合成校准」（不能让人误以为是真数据）",
                "合成校准" in _md9, _md9[:200])
        c.check("★★ 报告写明校准 ID（可追溯）",
                "cal-demo-synthetic" in _md9)
        c.check("★★ 报告写清用途是演示机制、不是种植建议",
                "不是给出可用的种植建议" in _md9
                or "演示机制" in _md9)
        c.check("★ 报告含四档结论与区间",
                "决策结论" in _md9 and "校准区间" in _md9)

        # ⑥ ★★ 真实校准**不该**触发那个警告（防误报）
        import json as _json9
        import numpy as _np9

        from core.perception import calibrate as _P9

        _rng9 = _np9.random.default_rng(3)
        _truth9 = _rng9.uniform(50, 200, 60)
        _det9 = _truth9 * 0.8 * _np9.exp(_rng9.normal(0, 0.15, 60))
        _pts9 = [_P9.CalibrationPoint(detected=float(a), truth=float(b))
                 for a, b in zip(_det9, _truth9)]
        _real9 = _P9.calibrate(_pts9, domain="real-domain", alpha=0.10)
        _cal9 = Path(_td9) / "real_cal.json"
        _cal9.write_text(_json9.dumps(_real9.to_dict(), ensure_ascii=False),
                         encoding="utf-8")
        _res9b = _pl.launch(
            _pi2.detect(f"/perception field_id=REAL n_boxes=1200 altitude_m=5 "
                        f"threshold=75 calibration_path={_cal9}"),
            runner=_runner9, out_dir=_td9)
        c.check("★★ 真实校准路径也能启动", _res9b.ok is True, _res9b.message)
        _run9b = _runner9.wait(_res9b.run_id, timeout=180)
        _rep9b = (getattr((getattr(_run9b, "steps", {}) or {}).get("report"),
                          "output", None) or {})
        _p9b = Path(_rep9b.get("path") or "")
        _md9b = _p9b.read_text(encoding="utf-8") if _p9b.is_file() else ""
        c.check("★★★ 真实校准的报告**不出现**合成警告（防误报）",
                "合成校准" not in _md9b, _md9b[:160])

    # ==================================================================
    c.section("第 10 步：★ 产物落在项目内（不碰用户工作区）")
    # ==================================================================
    from core.rag_engine import RAGEngine as _RE10
    # ★ 必须是**绝对路径**且锚在项目根（2026-10-20 修：相对串会跟着 cwd 跑，
    #   导致报告落到用户找不到的地方，且与界面白名单的根对不上）
    _rd10 = _RE10._report_dir()
    _p10 = Path(_rd10)
    c.check("★★★ 默认产物目录是**绝对路径**（不随 cwd 漂移）",
            _p10.is_absolute(), _rd10)
    c.check("★★ 且就是项目根下的 `reports`",
            _p10.name == "reports" and _p10.parent == ROOT.resolve(),
            f"{_p10.parent} vs {ROOT.resolve()}")

    # ==================================================================
    c.section("第 11 步：★★★ `/perception` 在入口拦截（**不经过 LLM**）")
    # ==================================================================
    # 【这一步修的是什么 bug】
    #   perception 的检测原本在 `ask()` **末尾**，于是打一句 `/perception --demo` 会
    #     ① 白跑最多 3 轮工具循环（3 次 LLM）
    #     ② 再白跑一次完整流式作答（1 次 LLM）
    #     ③ 然后才启动感知任务
    #   显式指令根本不需要模型判断。

    _rag_src = (ROOT / "core" / "rag_engine.py").read_text(encoding="utf-8")

    # ① 源码顺序断言：**只在 `ask()` 体内**比较。
    #    ★ 第一版拿整个文件比，结果被文件**别处**的 classify 干扰（假失败）——
    #      断言的作用域必须和被断言的东西对齐。
    _ask_start = _rag_src.find("    def ask(")
    _ask_end = _rag_src.find("\n    def ", _ask_start + 10)
    _ask_body = _rag_src[_ask_start:_ask_end if _ask_end > 0 else len(_rag_src)]
    _i_detect = _ask_body.find("_pintent.detect(question)")
    _i_classify = _ask_body.find("query_router.classify(question)")
    c.check("★★★ `ask()` 体内 detect 出现在 classify **之前**",
            _i_detect > 0 and _i_classify > 0 and _i_detect < _i_classify,
            f"detect@{_i_detect} classify@{_i_classify}")

    # ② 旧的末尾分支必须已删除
    c.check("★★ 末尾那段旧的 `perception_request` 分支已删除",
            "perception_request = None" not in _rag_src)

    # ③ ★★★ 行为断言：把 LLM 换成一调就炸的桩，跑 `/perception --demo`
    
    class _Boom11:
        def __getattr__(self, name):
            def _f(*a, **k):
                raise AssertionError(f"不该调用 LLM：{name}()")
            return _f

    try:
        from core.rag_engine import RAGEngine as _RE11

        # ★ 落在 _tmp_data 下并用固定名：mkdtemp 在 %TEMP% 不可写时会退到**项目根**
        #   （本机实测），每跑一次就在仓库里留一个 tmpXXXX。
        _td11 = _TEST_DIR / "_p11_reports"
        _shutil.rmtree(_td11, ignore_errors=True)
        _td11.mkdir(parents=True, exist_ok=True)
        _td11 = str(_td11)
        _orig_rd = _RE11._report_dir
        _RE11._report_dir = staticmethod(lambda: _td11)
        try:
            _eng11 = _RE11()
            _eng11.create_session()
            _eng11._owns_user_data = True
            _eng11.llm = _Boom11()
            _eng11._execute_tool = lambda n, a: '{"ok": true, "text": "桩"}'
            _txt11 = "".join(
                ch for k, ch in _eng11.ask("/perception --demo")
                if k == "content")
            _boom11 = None
        except AssertionError as _e:
            _txt11, _boom11 = "", _e
        finally:
            _RE11._report_dir = _orig_rd

        c.check("★★★ 执行 `/perception --demo` **完全没调用 LLM**",
                _boom11 is None, str(_boom11))
        c.check("★★ 回显里带 run_id（可追踪）",
                "run_id" in _txt11, _txt11[:140])
        c.check("★★ 回显里带**可点链接**（Markdown 形式）",
                "](" in _txt11, _txt11[:140])
        c.check("★★★ 回显里明确标注「合成校准」",
                "合成校准" in _txt11)
    except Exception as e:                                   # noqa: BLE001
        c.check("★★★ `/perception` 入口跑通", False,
                f"{type(e).__name__}: {e}")

    # ==================================================================
    c.section("第 11.5 步：★★★ 报告入口是**真按钮**（不依赖链接命中区域）")
    # ==================================================================
    # 【为什么改按钮】
    #   QLabel 的 <a href> 需要**精确命中锚文本**才触发 linkActivated，
    #   在真实界面上很容易"点了没反应"，且没有任何视觉反馈。
    #   一个 QPushButton 是明确的可点目标 —— 对"打开报告"这件事，可靠性优先。

    
    # ★★ 必须有 QApplication 才能创建控件 ——
    #   没有它 Qt 会**直接崩**（实测 exit code 0xC0000409 = STACK_BUFFER_OVERRUN），
    #   而不是抛一个可捕获的异常。
    from PyQt6.QtWidgets import QApplication as _QA115
    _app115 = _QA115.instance() or _QA115([])

    from PyQt6.QtWidgets import QPushButton as _PB115

    try:
        from ui.main_window import CottonAgentWindow as _W115

        _td115 = _TEST_DIR / "_p115_reports"
        _shutil.rmtree(_td115, ignore_errors=True)
        _td115.mkdir(parents=True, exist_ok=True)
        _td115 = str(_td115)
        _rep115 = Path(_td115) / "reports"
        _rep115.mkdir(parents=True, exist_ok=True)
        _f115 = _rep115 / "DEMO-x.md"
        _f115.write_text("# r", encoding="utf-8")

        _host115 = _W115.__new__(_W115)
        from PyQt6.QtWidgets import QVBoxLayout as _VL115
        from PyQt6.QtWidgets import QWidget as _QW115
        _host115._bubble_container = _QW115()
        _lay115 = _VL115(_host115._bubble_container)
        _lay115.addStretch(1)
        _host115._bubble_layout = _lay115
        _got115: list = []
        _host115._on_link_clicked = lambda h: _got115.append(h)

        _NOTE = ("已生成：[📄 打开报告](" + str(_f115) + ")\n")
        _b115 = _host115._add_bubble("思考中...", is_user=False)
        _b115.update_text(_NOTE)
        _b115.set_streaming(False)

        _btns = _b115._bubble.findChildren(_PB115)
        c.check("★★★ 报告链接被渲染成**按钮**（不是靠链接命中）",
                len(_btns) == 1, f"按钮数 {len(_btns)}")
        if _btns:
            c.check("★ 按钮文本含「打开报告」",
                    "打开报告" in _btns[0].text(), _btns[0].text())
            c.check("★ 按钮用专用样式（明确可点）",
                    _btns[0].objectName() == "reportLinkBtn",
                    _btns[0].objectName())
            _btns[0].click()
            c.check("★★★ 点按钮 → 到达 _on_link_clicked",
                    bool(_got115), str(_got115))
        c.check("★★ 标签里不残留 markdown 链接标记",
                "](" not in _b115._label.text(), _b115._label.text()[:80])
    except Exception as e:                                   # noqa: BLE001
        c.check("★★★ 报告按钮链路跑通", False, f"{type(e).__name__}: {e}")

    # ==================================================================
    c.section("第 12 步：★★★ 报告链接可点，且**白名单校验**（安全关键）")
    # ==================================================================
    # 【为什么这条必须有白名单】
    #   气泡里的 href 来自**模型输出**或我们拼接的路径。
    #   `QDesktopServices.openUrl` 会把它原样交给系统 ——
    #   模型完全可能写出 `file:///C:/Windows/System32/calc.exe`。
    #   所以只允许打开**产物目录下**的**已知类型**文件。

    try:
        from ui.main_window import CottonAgentWindow as _W12
        from ui.main_window import MessageBubble as _MB12

        # ① 气泡必须**转出**链接信号（而不是自己 openExternalLinks）
        c.check("★★ MessageBubble 有 `link_clicked` 信号",
                hasattr(_MB12, "link_clicked"))

        # ② 交互标志必须**含** LinksAccessibleByMouse（否则链接点不动）
        from PyQt6.QtCore import Qt as _Qt12
        _flags = _MB12._link_flags()
        c.check("★★★ 交互标志含 `LinksAccessibleByMouse`（否则点了没反应）",
                bool(_flags & _Qt12.TextInteractionFlag.LinksAccessibleByMouse),
                str(_flags))
        c.check("★ 同时保留可划词",
                bool(_flags & _Qt12.TextInteractionFlag.TextSelectableByMouse))

        # ③ ★★★ 白名单：允许的必须放行
        _rd = Path.cwd() / "reports"
        _cd = Path.cwd() / "charts"
        _rd.mkdir(exist_ok=True)
        _cd.mkdir(exist_ok=True)
        _f_md = _rd / "_g12.md"
        _f_md.write_text("# t", encoding="utf-8")
        _f_png = _rd / "_g12.png"
        _f_png.write_bytes(b"x")
        _f_cmd = _cd / "_g12.md"
        _f_cmd.write_text("# c", encoding="utf-8")
        try:
            for _h in ("reports/_g12.md", "reports/_g12.png",
                       "charts/_g12.md"):
                c.check(f"★★ 允许打开 {_h}",
                        _W12._resolve_openable(_h) is not None)
        finally:
            for _x in (_f_md, _f_png, _f_cmd):
                _x.unlink(missing_ok=True)

        # ④ ★★★ 白名单：**必须拒绝**的那些（安全关键）
        _root_md = Path.cwd() / "_g12_root.md"
        _root_md.write_text("# r", encoding="utf-8")
        _core_md = Path.cwd() / "core" / "_g12.md"
        _core_md.write_text("# c", encoding="utf-8")
        _evil_exe = _rd / "_g12.exe"
        _evil_exe.write_bytes(b"x")
        try:
            DENY = [
                ("项目根下的文件", "_g12_root.md"),
                ("core/ 下的文件", "core/_g12.md"),
                ("路径穿越出产物目录", "reports/../_g12_root.md"),
                ("不允许的扩展名", "reports/_g12.exe"),
                ("http 外链", "https://evil.example.com/x"),
                ("file:// 协议", "file:///C:/Windows/System32/calc.exe"),
                ("系统目录绝对路径", "C:/Windows/System32/notepad.exe"),
                ("空串", ""),
                ("不存在的文件", "reports/__nope_g12__.md"),
            ]
            for _label, _h in DENY:
                c.check(f"★★★ 拒绝{_label}", _W12._resolve_openable(_h) is None,
                        f"竟然放行: {_h!r}")
        finally:
            for _x in (_root_md, _core_md, _evil_exe):
                _x.unlink(missing_ok=True)
            for _d in (_rd, _cd):
                try:
                    _d.rmdir()
                except OSError:
                    pass
    except Exception as e:                                   # noqa: BLE001
        c.check("★★★ 链接白名单跑通", False, f"{type(e).__name__}: {e}")

    print()
    print("=" * 78)
    print(f"感知接入守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())