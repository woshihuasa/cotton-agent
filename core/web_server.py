# -*- coding: utf-8 -*-
"""
Web 服务（FastAPI + SSE）—— 可嵌入实现
========================================

【为什么放在 core/ 而不是只放 server.py】
  桌面端的「设置 → Web 服务」需要能在**打包后的 exe 里**启动服务。
  打包后独立的 `server.py` 并不存在于运行环境，因此把 app 定义放在 core/ 内，
  由两处复用：

    · `server.py`            —— 命令行独立启动（开发调试 / 服务器部署）
    · `ui/settings_dialog.py` —— 桌面端点按钮启动（含打包环境）

【接口】
  GET  /            聊天页面（web/index.html）
  POST /session     建会话，返回 session_id
  POST /chat        SSE 流式问答；请求头 `X-DeepSeek-Key` 传用户自备 Key
  GET  /charts/*    工具生成的图表（静态文件）
  GET  /health      健康检查（含访问控制状态）

【访问控制】
  · 口令：请求头 `X-Access-Token` 与 .env 中 WEB_ACCESS_TOKEN 比对；**留空则
    完全不校验**（本机 / 家庭局域网自用）。一旦暴露到公网必须设置。
  · 限流：单 IP 每分钟 WEB_RATE_PER_MIN 次 + 全站每日 WEB_DAILY_LIMIT 次，
    防止服务端 Embedding 配额被脚本刷爆。

【Key 模式】
  · **LLM（DeepSeek）**：用户在页面填写 → 存浏览器 localStorage → 随请求头传入；
    服务端不保存、不落盘；Key 变化时自动重建该会话的引擎。
  · **Embedding（硅基流动）**：由服务端配置提供 —— 检索在服务端进行，
    且向量库是用服务端那套模型建的（换 Key 会导致维度不匹配）。
"""
from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import AppConfig, resource_path
from core.rag_engine import RAGEngine

app = FastAPI(title="棉花种植决策智能体 · Web", docs_url=None, redoc_url=None)

# ── 会话池 ────────────────────────────────────────────────
# 每个浏览器会话一个 RAGEngine 实例（引擎内部持有当前会话状态，不能全局共享）。
SESSION_TTL = 2 * 3600      # 2 小时无活动则清理
MAX_SESSIONS = 50           # 上限保护，超出时淘汰最久未用

_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_shared_kb = None           # 知识库连接全局复用（只读检索）


def _get_shared_kb():
    """惰性创建并复用同一个 KnowledgeBase（避免每个会话重复加载向量库）。

    只共享**公共知识库**——用户私有记忆已由 S1 拆分出去（`core/user_memory.py`），
    Web 场景不参与（见 `_get_engine` 的 `owns_user_data=False`）。
    """
    global _shared_kb
    if _shared_kb is None:
        from core.knowledge_base import KnowledgeBase

        _shared_kb = KnowledgeBase()
    return _shared_kb


def _cleanup_locked() -> None:
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v["ts"] > SESSION_TTL]:
        _sessions.pop(sid, None)


def _get_engine(session_id: str, api_key: str) -> RAGEngine:
    """取（或创建）会话引擎。用户 Key 变化时重建，保证使用当前 Key。"""
    with _lock:
        _cleanup_locked()
        ent = _sessions.get(session_id)
        if ent and ent["key"] == api_key:
            ent["ts"] = time.time()
            return ent["engine"]
        _sessions.pop(session_id, None)        # Key 变了 → 丢弃旧引擎

    # owns_user_data=False：Web 访客既不拥有本地会话存档，也不参与 L4 长期记忆
    #   · 会话仅存在于内存（本引擎实例）——不再写生产 session_state.json
    #   · L4 使用 NullMemoryStore——不读不写生产 chroma_user_memory，
    #     也就彻底消除了"跨访客共享用户记忆"（ROADMAP 短板 #17① / #18）
    engine = RAGEngine(api_key=api_key or None, owns_user_data=False)
    try:
        engine.kb = _get_shared_kb()            # 只共享公共知识库（只读）
    except Exception:
        pass
    engine.create_session()                     # 建立该引擎的会话上下文

    with _lock:
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda k: _sessions[k]["ts"])
            _sessions.pop(oldest, None)
        _sessions[session_id] = {"engine": engine, "ts": time.time(), "key": api_key}
    return engine


# ── 访问控制（口令 + 限流）────────────────────────────────
# 口令：.env 的 WEB_ACCESS_TOKEN；留空 = 不校验（本机 / 家庭局域网自用）。
#       一旦把服务暴露到公网，必须设置 —— 否则任何人都能消耗服务端的
#       Embedding 配额（LLM Key 由用户自备，风险相对小）。
# 限流：单 IP 每分钟 + 全站每日两个上限，防止被脚本刷爆。

_hits: dict[str, list[float]] = {}      # IP → 最近一分钟内的提问时间戳
_day_count = 0                          # 全站当日问答总数
_day_mark = ""                          # 当日标记（YYYY-MM-DD），跨天自动重置


def _check_access(token: str) -> tuple[bool, str]:
    """校验访问口令。未配置口令时不校验（本机 / 局域网自用场景）。"""
    expected = (AppConfig.WEB_ACCESS_TOKEN or "").strip()
    if not expected:
        return True, ""
    # compare_digest 定长比较，避免时序侧信道
    if secrets.compare_digest((token or "").strip(), expected):
        return True, ""
    return False, "访问口令不正确（点击右上角「设置」填写）"


def _check_rate(ip: str) -> tuple[bool, str]:
    """按 IP 限流 + 全站每日总量限制。"""
    global _day_count, _day_mark

    now = time.time()
    with _lock:
        today = time.strftime("%Y-%m-%d")
        if today != _day_mark:                     # 跨天 → 重置计数
            _day_mark = today
            _day_count = 0
            _hits.clear()

        if _day_count >= AppConfig.WEB_DAILY_LIMIT:
            return False, f"今日提问额度已用完（全站上限 {AppConfig.WEB_DAILY_LIMIT} 次）"

        recent = [t for t in _hits.get(ip, []) if now - t < 60.0]
        if len(recent) >= AppConfig.WEB_RATE_PER_MIN:
            return False, f"提问过于频繁，请稍后再试（每分钟上限 {AppConfig.WEB_RATE_PER_MIN} 次）"

        recent.append(now)
        _hits[ip] = recent
        _day_count += 1

    return True, ""


# ── 图表路径改写 ──────────────────────────────────────────
# 引擎以 markdown 图片标记返回图表，路径是**本地绝对路径**
# （如 E:\\cotton_agent\\charts\\yield_20260928_120000.png），浏览器无法访问，
# 统一改写为静态路由 /charts/<文件名>。
_IMG_RE = re.compile(r"(!\[[^\]]*\]\()([^)]+)(\))")


def rewrite_chart_paths(text: str) -> str:
    return _IMG_RE.sub(
        lambda m: f"{m.group(1)}/charts/{Path(m.group(2)).name}{m.group(3)}", text
    )


# ── 请求模型 ──────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: str
    question: str
    thinking: bool = False


# ── 路由 ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """返回聊天页面（打包后从 _MEIPASS 读取）。"""
    page = Path(resource_path("web")) / "index.html"
    if not page.exists():
        return "<h1>缺少 web/index.html</h1>"
    return page.read_text("utf-8")


# ── 编排层运行视图（P3 只读）──────────────────────────────
# ⚠️ 这一组接口**只读**：列定义、列运行、看单次运行。
#    P4 的编辑接口另开（`POST /api/workflows/{id}`），本组不做写入。

def _project_root() -> Path:
    """项目根目录 —— 与 `config.py` 用的是同一套定位方式（`__file__` 的父目录）。

    ★ 不用 `cwd`：从别的目录启动服务时 `cwd` 会变，而配置/工作流应该跟着代码走。
    ⚠️ 第一版这里瞎猜了个 `config.BASE_DIR`，**运行期才炸**（守卫/测试都过了，
    因为没人 import 到这个函数）。
    """
    return Path(__file__).resolve().parent.parent


def _workflows_dir() -> Path:
    """工作流定义目录（`workflows/*.yaml`，YAML 是权威定义 —— 决策 3a）。

    ★★ 2026-10-20 修了两个叠加的错：

      ① **路径推导错**：原先是 `Path(__file__).parent.parent / "workflows"`。
         打包后 `__file__` 在 `_MEIPASS` 里，推导出来是 `_MEIPASS/workflows` ——
         而 `workflows/` 当时**根本没打进包**（spec 的 datas 漏了）。

      ② **mkd​ir 掩盖了缺资产**：原代码 `d.mkdir(parents=True, exist_ok=True)`
         会在包内造出一个**空目录**，于是错误表现从
         「工作流文件缺失」变成「工作流列表为空」—— 后者看起来像"正常但没数据"，
         排查成本高得多。**目录若不存在，就该让调用方看见空结果 + 明确日志，
         而不是伪造一个能用但为空的目录。**

         所以这里**不再建目录**：打包内是只读的，本来也不该建。
         真要写入（P4 的编辑接口）应写到 `app_data_dir()` 而非资源目录。
    """
    from config import resource_path
    return resource_path("workflows")


@app.get("/workflow", response_class=HTMLResponse)
def workflow_page() -> str:
    """工作流可视化页面（手写 SVG，零构建）。"""
    page = Path(resource_path("web")) / "workflow.html"
    if not page.exists():
        return "<h1>缺少 web/workflow.html</h1>"
    return page.read_text("utf-8")


@app.get("/api/workflows")
def list_workflows() -> dict:
    """列出所有工作流定义（含节点与边，供前端布局）。"""
    from core.orchestration import loader

    out: list[dict] = []
    for p in sorted(_workflows_dir().glob("*.yaml")):
        try:
            wf = loader.load_file(p)
        except Exception as e:                    # noqa: BLE001
            # ★ 单个文件坏了不该让整个列表 500 —— 把错误当数据返回
            out.append({"id": p.stem, "error": f"{type(e).__name__}: {e}"})
            continue
        out.append({
            "id": wf.id,
            "version": wf.version,
            "description": wf.description,
            "nodes": [
                {"id": n.id, "type": n.type, "needs": list(n.needs),
                 "side_effect": n.effective_side_effect.value,
                 "optional": bool(n.optional)}
                for n in wf.nodes
            ],
            "edges": [{"from": a, "to": b} for a, b in wf.edges()],
        })
    return {"workflows": out}


@app.get("/api/workflows/runs")
def list_workflow_runs(workflow_id: str = "") -> dict:
    """列出运行记录（摘要）。"""
    from core.orchestration import store

    try:
        return {"runs": store.list_runs(workflow_id)}
    except Exception as e:                        # noqa: BLE001
        return {"runs": [], "error": f"{type(e).__name__}: {e}"}


@app.get("/api/workflows/runs/{run_id}")
def get_workflow_run(run_id: str) -> dict:
    """单次运行的完整状态（节点状态 / 耗时 / 错误）—— 可视化的数据源。"""
    from core.orchestration import store

    run = store.load_run(run_id)
    if run is None:
        return {"error": f"找不到运行 {run_id}"}
    cp = store.load_checkpoint(run_id)
    steps = []
    for nid, s in run.steps.items():
        steps.append({
            "node_id": nid,
            "state": s.state.value,
            "attempts": s.attempts,
            "rejects": s.rejects,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "error": s.error,
            # 产出可能很大：只把前 2000 字交给前端
            "output": str(s.output)[:2000] if s.output is not None else None,
        })
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "state": run.state.value,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "steps": steps,
        "waiting_node": (cp.waiting_node if cp else ""),
    }


class WorkflowSaveRequest(BaseModel):
    """P4：把前端编辑好的图存回 YAML。"""

    yaml_text: str
    overwrite: bool = False


@app.get("/api/workflows/{workflow_id}/yaml")
def get_workflow_yaml(workflow_id: str) -> dict:
    """取原始 YAML 文本（P4 编辑用）。"""
    p = _workflows_dir() / f"{_safe_wf_id(workflow_id)}.yaml"
    if not p.is_file():
        return {"error": f"工作流 {workflow_id} 不存在", "yaml_text": ""}
    return {"workflow_id": workflow_id, "yaml_text": p.read_text(encoding="utf-8")}


@app.post("/api/workflows/{workflow_id}")
def save_workflow(workflow_id: str, req: WorkflowSaveRequest) -> dict:
    """保存工作流 YAML（**先校验，再落盘**）。

    ★ 这是"YAML 权威（决策 3a）"的兑现：拖拽的结果**写回 YAML**，
    于是 `git diff` 能看见、守卫能校验、下次加载走同一条路。

    ★ **校验不过就不写盘** —— 让错误停在编辑阶段，而不是等运行时才炸。
    也**不允许**前端把 `id` 改成别的（文件名与 `id` 必须一致，否则以后找不到）。
    """
    from core.orchestration import loader, validator

    safe = _safe_wf_id(workflow_id)
    try:
        wf = loader.load_text(req.yaml_text, source=f"{safe}.yaml")
    except loader.LoaderError as e:
        return {"ok": False, "stage": "load", "error": str(e)}

    if safe != workflow_id:
        return {"ok": False, "stage": "id",
                "error": f"工作流 id 只能是字母/数字/下划线/短横，收到 {workflow_id!r}"}
    if wf.id != workflow_id:
        # `id` 与文件名不一致会让"以后按 id 找文件"失效
        return {"ok": False, "stage": "id",
                "error": f"YAML 里的 id ({wf.id!r}) 必须与文件名 ({workflow_id!r}) 一致"}

    issues = validator.validate(wf)
    errors = [str(i) for i in validator.errors_only(issues)]
    if errors:
        return {"ok": False, "stage": "validate",
                "error": "校验未通过，未写入", "issues": errors}

    p = _workflows_dir() / f"{safe}.yaml"
    if p.exists() and not req.overwrite:
        return {"ok": False, "stage": "exists",
                "error": f"{p.name} 已存在；如需覆盖请带 overwrite=true"}
    p.write_text(req.yaml_text, encoding="utf-8")
    return {"ok": True, "path": str(p),
            "warnings": [str(i) for i in issues if i.level == "warn"]}


def _safe_wf_id(workflow_id: str) -> str:
    """把工作流 id 收敛成安全文件名（**挡住路径穿越**）。"""
    import re as _re
    s = _re.sub(r"[^A-Za-z0-9_-]", "-", str(workflow_id or "")).strip("-")
    if not s or ".." in s:
        return ""
    return s[:64]


@app.post("/session")
def new_session() -> dict:
    """建会话：仅返回会话 ID，真正的引擎在首次提问时惰性创建。"""
    return {"session_id": uuid.uuid4().hex}


@app.post("/chat")
def chat(
    req: ChatRequest,
    request: Request,
    x_deepseek_key: str = Header(default=""),
    x_access_token: str = Header(default=""),
):
    """流式问答（SSE）。

    注意：这里用同步 `def` 而非 `async def` —— 引擎是同步阻塞实现，
    FastAPI 会把同步端点放入线程池执行，从而不阻塞事件循环。
    """
    # 访问控制：先验口令，再限流（避免拿口令爆破顺带刷计数）
    ok, err = _check_access(x_access_token)
    if not ok:
        return JSONResponse(status_code=401, content={"detail": err})

    client_ip = request.client.host if request.client else "unknown"
    ok, err = _check_rate(client_ip)
    if not ok:
        return JSONResponse(status_code=429, content={"detail": err})

    api_key = (x_deepseek_key or "").strip()

    def sse(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    def generate():
        if not api_key:
            yield sse({"type": "error", "data": "未填写 DeepSeek API Key（点击右上角设置填写）"})
            yield sse({"type": "done"})
            return
        try:
            engine = _get_engine(req.session_id, api_key)
            for msg_type, chunk in engine.ask(req.question, enable_thinking=req.thinking):
                if msg_type == "content":
                    chunk = rewrite_chart_paths(chunk)
                yield sse({"type": msg_type, "data": chunk})
        except Exception as e:
            yield sse({"type": "error", "data": f"处理失败：{e}"})
        yield sse({"type": "done"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health() -> dict:
    """健康检查（会话数 + 访问控制是否启用）。"""
    with _lock:
        n = len(_sessions)
    return {
        "ok": True,
        "sessions": n,
        "token_required": bool((AppConfig.WEB_ACCESS_TOKEN or "").strip()),
        "rate_per_min": AppConfig.WEB_RATE_PER_MIN,
        "daily_limit": AppConfig.WEB_DAILY_LIMIT,
    }


# ── 图表静态目录 ─────────────────────────────────────────
_chart_dir = Path(AppConfig.CHART_DIR)
_chart_dir.mkdir(parents=True, exist_ok=True)
app.mount("/charts", StaticFiles(directory=str(_chart_dir)), name="charts")


# ── 供桌面端调用的启停控制 ────────────────────────────────
_server = None
_thread: threading.Thread | None = None


def lan_ip() -> str:
    """尽力探测本机局域网 IP（用于展示可分享的访问地址）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def access_urls(port: int) -> tuple[str, str]:
    """返回 (本机地址, 局域网地址)。"""
    return f"http://127.0.0.1:{port}", f"http://{lan_ip()}:{port}"


def is_running() -> bool:
    return _server is not None and _thread is not None and _thread.is_alive()


def start(port: int = 8000, host: str = "0.0.0.0") -> tuple[bool, str]:
    """在后台线程启动服务（非阻塞）。

    Returns:
        (是否成功, 信息文本)
    """
    global _server, _thread
    if is_running():
        return True, "服务已在运行中"

    try:
        import uvicorn

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        _server = uvicorn.Server(config)
        _thread = threading.Thread(target=_server.run, daemon=True, name="web-server")
        _thread.start()
        local, lan = access_urls(port)
        return True, f"已启动：{local}（局域网 {lan}）"
    except Exception as e:
        _server = None
        _thread = None
        return False, f"启动失败：{e}"


def stop() -> tuple[bool, str]:
    """停止服务（优雅退出）。"""
    global _server, _thread
    if not is_running():
        _server = None
        _thread = None
        return True, "服务未在运行"
    try:
        _server.should_exit = True            # uvicorn 的优雅退出标志
        _server = None
        _thread = None
        return True, "服务已停止"
    except Exception as e:
        return False, f"停止失败：{e}"
