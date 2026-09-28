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

app = FastAPI(title="棉花智能问答助手 · Web", docs_url=None, redoc_url=None)

# ── 会话池 ────────────────────────────────────────────────
# 每个浏览器会话一个 RAGEngine 实例（引擎内部持有当前会话状态，不能全局共享）。
SESSION_TTL = 2 * 3600      # 2 小时无活动则清理
MAX_SESSIONS = 50           # 上限保护，超出时淘汰最久未用

_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_shared_kb = None           # 向量库连接全局复用（只读检索）


def _get_shared_kb():
    """惰性创建并复用同一个 KnowledgeBase（避免每个会话重复加载向量库）。"""
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

    engine = RAGEngine(api_key=api_key or None)
    try:
        engine.kb = _get_shared_kb()           # 复用向量库连接
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
