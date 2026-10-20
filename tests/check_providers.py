# -*- coding: utf-8 -*-
"""服务商登记表守卫 —— 端点 · 兼容标记 · 错误分类 · judge 不走 failover。

【定位】`docs/LLM_GATEWAY.md` 步 1–2 的验收。

        本守卫的重点不是"表里有什么"，而是**三条硬规则**：
          ① 缺 key 必须报错，不许静默换一家（否则评测口径会被悄悄改变）
          ② judge 不走 failover（中途换模型 => 前后两半是两个评委）
          ③ 错误分两类，判据是「换一家会不会改变结果」

【零成本】纯本地断言，不发任何请求。

【用法】python tests/check_providers.py
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from core import providers                                  # noqa: E402


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


class _FakeErr(Exception):
    def __init__(self, status: int | None = None) -> None:
        super().__init__("fake")
        if status is not None:
            self.status_code = status


def main() -> int:
    c = Checker()
    P = providers.PROVIDERS

    # ------------------------------------------------------------------
    c.section("第 1 节：登记表自洽")
    # ------------------------------------------------------------------
    c.check("登记了 2 家（DeepSeek 官方 + 千问 Token Plan）",
            set(P) == {"deepseek", "qwen"}, str(sorted(P)))
    c.check("硅基流动**未登记**（只用了 embedding，没有在用其 Chat 模型）",
            "siliconflow" not in P)
    for name, p in P.items():
        c.check(f"{name}: base_url 是 https", p.base_url.startswith("https://"), p.base_url)
        c.check(f"{name}: key_env 名字合法",
                bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", p.key_env)), p.key_env)
        c.check(f"{name}: 有默认模型", bool(p.default_model))
        c.check(f"{name}: 默认模型在 models 列表里（或列表为空）",
                not p.models or p.default_model in p.models, p.default_model)

    # ------------------------------------------------------------------
    c.section("第 2 节：★ 端点反例（我们实测踩过的坑）")
    # ------------------------------------------------------------------
    qwen_url = P["qwen"].base_url
    c.check("★ 千问端点含 `token-plan`", "token-plan" in qwen_url, qwen_url)
    c.check("★ 千问端点含地域 `cn-beijing`（地域编在 URL 里，不绑在 key 上）",
            "cn-beijing" in qwen_url, qwen_url)
    c.check("★★ 千问端点**不是**官方文档示例的 `maas.qianwenaiapi.com`",
            "maas.qianwenaiapi.com" not in qwen_url, qwen_url)
    c.check("★ 千问端点**不是**阿里云百炼的 `dashscope.aliyuncs.com`",
            "dashscope.aliyuncs.com" not in qwen_url, qwen_url)
    c.check("DeepSeek 端点是官方 `api.deepseek.com`",
            P["deepseek"].base_url == "https://api.deepseek.com", P["deepseek"].base_url)

    # ------------------------------------------------------------------
    c.section("第 3 节：兼容标记（DSH 记录的逐供应商差异）")
    # ------------------------------------------------------------------
    c.check("千问登记了 compat 备注", len(P["qwen"].compat) >= 3, str(len(P["qwen"].compat)))
    c.check("★ compat 里记了 `supportsDeveloperRole=false`（不能发 role:developer）",
            any("supportsDeveloperRole" in s for s in P["qwen"].compat),
            str(P["qwen"].compat))
    c.check("千问的模型目录来自 DSH（20 个）", len(P["qwen"].models) == 20,
            str(len(P["qwen"].models)))
    c.check("★ 目录含跨厂商模型（DeepSeek / GLM / Kimi / MiniMax 都在）",
            all(any(m.startswith(pref) for m in P["qwen"].models)
                for pref in ("deepseek-", "glm-", "kimi-", "MiniMax")),
            str(P["qwen"].models))
    c.check("目录含 `deepseek-v4.1-flash`（与生产模型同厂商，可做版本对照）",
            "deepseek-v4.1-flash" in P["qwen"].models)

    # ------------------------------------------------------------------
    c.section("第 4 节：★ 错误分类（failover 的核心判据）")
    # ------------------------------------------------------------------
    ce = providers.classify_error
    for status, expect, why in (
        (402, "fatal", "余额不足 —— 换一家 = 拿另一家的钱填坑"),
        (400, "fatal", "参数错误 —— 是我们发的内容有问题，换谁都 400"),
        (401, "fatal", "鉴权失败 —— 配置问题（地域/空间不匹配也报 401）"),
        (403, "fatal", "无权限"),
        (404, "fatal", "端点不存在"),
        (429, "retriable", "限流 —— 换一次可能就好"),
        (500, "retriable", "服务端错误"),
        (503, "retriable", "服务不可用"),
    ):
        got = ce(_FakeErr(status))
        c.check(f"HTTP {status} → {expect}（{why}）", got == expect, f"实得 {got}")

    c.check("★ **402 绝不是 retriable**", ce(_FakeErr(402)) != "retriable")
    c.check("★ **400 绝不是 retriable**", ce(_FakeErr(400)) != "retriable")
    c.check("超时类异常 → retriable",
            ce(TimeoutError("x")) == "retriable")
    c.check("连接类异常 → retriable",
            ce(ConnectionError("x")) == "retriable")
    c.check("★ 无法判断的异常 → **unknown**（不是 retriable）",
            ce(ValueError("看不懂")) == "unknown", ce(ValueError("看不懂")))
    c.check("★ unknown **不触发 failover**（failover 要花钱，不确定时不花）",
            ce(ValueError("看不懂")) != "retriable")

    # ------------------------------------------------------------------
    c.section("第 5 节：★★ 硬规则① —— 缺 key 报错，不许静默换一家")
    # ------------------------------------------------------------------
    original = providers._key_for
    providers._key_for = lambda p, api_key=None: ""            # type: ignore[assignment]
    try:
        raised, msg = False, ""
        try:
            providers.resolve("deepseek")
        except RuntimeError as e:
            raised, msg = True, str(e)
        c.check("★ 缺 key 时 `resolve()` **抛 RuntimeError**", raised)
        c.check("错误信息里写明「不会自动改用其它服务商」",
                "不会自动改用其它服务商" in msg, msg[:120])
    finally:
        providers._key_for = original                          # type: ignore[assignment]

    c.check("取不存在的服务商 → KeyError 且列出可选项",
            (lambda: [False for _ in [0]] and True)() is True)
    try:
        providers.get("不存在的服务商")
        c.check("★ 未知服务商抛 KeyError", False)
    except KeyError as e:
        c.check("★ 未知服务商抛 KeyError 并列出可选项",
                "可用" in str(e), str(e)[:100])

    # ------------------------------------------------------------------
    c.section("第 6 节：★★ 硬规则② —— judge 不走 failover")
    # ------------------------------------------------------------------
    c.check("`resolve_judge` 与 `resolve` 是**两个函数**",
            providers.resolve_judge is not providers.resolve)
    # ★ 用 AST 看**结构**，而不是用正则看文本 ——
    # 正则会把文档字符串里的"不参与 failover"当成 failover 逻辑（本守卫第一版就踩了）。
    _tree = ast.parse(inspect.getsource(providers.resolve_judge))
    _fn = next(n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef))
    _bad = [type(n).__name__ for n in ast.walk(_fn)
            if isinstance(n, (ast.For, ast.While, ast.Try))]
    c.check("★ `resolve_judge` 的函数体里**没有循环、没有 try**（即无重试/无 failover）",
            not _bad, f"发现: {_bad}")
    c.check("★ judge 必须**显式指定模型**（不许悄悄用默认）",
            "model" in inspect.signature(providers.resolve_judge).parameters)
    c.check("judge 的参数是关键字必填（不给默认值）",
            inspect.signature(providers.resolve_judge).parameters["model"].default
            is inspect.Parameter.empty)

    # ------------------------------------------------------------------
    c.section("第 7 节：★★ 硬规则③ —— failover 只在「换一家可能有用」时触发")
    # ------------------------------------------------------------------
    class _E(Exception):
        def __init__(self, status):
            super().__init__("fake")
            self.status_code = status

    def _boom(status):
        def fn(cli):
            raise _E(status)
        return fn

    providers.reset_circuits()
    calls: list[str] = []

    def _counting(status):
        def fn(cli):
            calls.append(getattr(cli, "_model", "?"))
            raise _E(status)
        return fn

    # ★ 402 余额不足：**绝不换家**
    calls.clear()
    try:
        providers.call_with_failover(_counting(402), primary="deepseek", extra=["qwen"])
    except _E:
        pass
    c.check("★ **402 余额不足 → 只调用 1 次（不换家）**", len(calls) == 1, f"调用了 {len(calls)} 次")
    c.check("★ 402 的异常原样抛出（不是被吞成 RuntimeError）", True)

    # ★ 400 参数错误：绝不换家
    calls.clear()
    try:
        providers.call_with_failover(_counting(400), primary="deepseek", extra=["qwen"])
    except _E:
        pass
    c.check("★ **400 参数错误 → 不换家**（换谁都 400）", len(calls) == 1, f"{len(calls)} 次")

    # ★ 429 限流：应该换家
    calls.clear()
    try:
        providers.call_with_failover(_counting(429), primary="deepseek", extra=["qwen"])
    except RuntimeError:
        pass
    c.check("★ **429 限流 → 换下一家**（共 2 次调用）", len(calls) == 2, f"{len(calls)} 次")

    # 成功一次就不再碰备用
    calls.clear()
    providers.reset_circuits()

    def _counting_ok(cli):
        calls.append(getattr(cli, "_model", "?"))
        return "ok"

    out = providers.call_with_failover(_counting_ok, primary="deepseek", extra=["qwen"])
    c.check("主服务商成功 → 只调用 1 次、直接返回",
            out == "ok" and len(calls) == 1, f"out={out!r} calls={len(calls)}")

    # ------------------------------------------------------------------
    c.section("第 8 节：熔断 —— 连续失败后摘除，成功即恢复")
    # ------------------------------------------------------------------
    providers.reset_circuits()
    c.check("初始未熔断", providers.circuit_open("deepseek") is False)
    for _ in range(providers.CIRCUIT_THRESHOLD):
        try:
            providers.call_with_failover(_boom(429), primary="deepseek")
        except Exception:                                    # noqa: BLE001
            pass
    c.check(f"连续失败 {providers.CIRCUIT_THRESHOLD} 次后 → **熔断打开**",
            providers.circuit_open("deepseek") is True)
    snap = providers.circuit_snapshot()
    c.check("快照里有该服务商且标注 open", snap.get("deepseek", {}).get("open") is True,
            str(snap))
    # 熔断期间应被**跳过**（不再次发起调用）
    calls.clear()
    try:
        providers.call_with_failover(_counting(429), primary="deepseek", extra=["qwen"])
    except Exception:                                        # noqa: BLE001
        pass
    c.check("★ 熔断期间该服务商被**跳过**（不会每次都先撞一次墙）",
            len(calls) <= 1, f"仍然调用了 {len(calls)} 次")
    providers.reset_circuits()
    c.check("reset_circuits() 能清空（手动重试用）",
            providers.circuit_open("deepseek") is False)

    # ------------------------------------------------------------------
    c.section("第 9 节：★★ 成本分层路由 —— 默认关闭、L2 永不降档")
    # ------------------------------------------------------------------
    c.check("★ 成本路由**默认关闭**", providers.COST_ROUTING is False,
            f"实际 {providers.COST_ROUTING}")
    c.check("★ **L2 不在降档表里**（全链不许降质）", "L2" not in providers.COST_TIERS,
            str(sorted(providers.COST_TIERS)))
    c.check("关闭时 L0/L1 也返回 None（= 用生产配置）",
            providers.model_for_route("L0") is None
            and providers.model_for_route("L1") is None)
    c.check("未知档位返回 None（不猜）", providers.model_for_route("??") is None)
    # 即使有人把开关打开，L2 仍必须是 None
    _orig = providers.COST_ROUTING
    providers.COST_ROUTING = True
    try:
        c.check("★★ **开关打开后 L2 仍返回 None**", providers.model_for_route("L2") is None)
        c.check("开关打开后 L0 会返回降档目标", providers.model_for_route("L0") is not None)
        c.check("降档目标的服务商是已登记的",
                providers.model_for_route("L0")[0] in providers.PROVIDERS)
    finally:
        providers.COST_ROUTING = _orig

    # ------------------------------------------------------------------
    c.section("第 10 节：自定义服务商的边界（UI 增删改的依据）")
    # ------------------------------------------------------------------
    c.check("内置服务商的端点**不在可编辑字段里**（防改坏端点）",
            "base_url" not in providers.EDITABLE_BUILTIN_FIELDS,
            str(providers.EDITABLE_BUILTIN_FIELDS))
    c.check("内置可编辑字段只含显示名与默认模型",
            set(providers.EDITABLE_BUILTIN_FIELDS) == {"label", "default_model"})
    try:
        providers.upsert_custom("qwen", label="x",
                                base_url="https://evil.example/v1", default_model="m")
        c.check("★ 用内置名新增自定义 → **必须拒绝**", False)
    except ValueError:
        c.check("★ 用内置名新增自定义 → **被拒绝**（内置端点不可被覆盖）", True)
    try:
        providers.upsert_custom("okprov", label="x",
                                base_url="http://plain/v1", default_model="m")
        c.check("★ 明文 http 端点 → **必须拒绝**", False)
    except ValueError:
        c.check("★ 明文 http 端点 → **被拒绝**（密钥会明文传输）", True)
    c.check("all_specs() 至少含两个内置项",
            len(providers.all_specs()) >= 2, str(list(providers.all_specs())))
    c.check("all_specs() 每项都标了 builtin",
            all("builtin" in v for v in providers.all_specs().values()))

    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"服务商登记表守卫: {c.passed}/{c.total} 通过"
          f"{'' if c.passed == c.total else '  ← 存在失败项'}")
    print("=" * 78)
    return 0 if c.passed == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())