# -*- coding: utf-8 -*-
"""
Web 服务命令行入口
====================

真正的服务实现在 `core/web_server.py`（设计为**可嵌入**，桌面端「设置 → Web 服务」
用的是同一份代码）。本文件只做命令行包装，便于开发调试与服务器部署。

【用法】
  .venv\\Scripts\\python.exe server.py              # 默认 0.0.0.0:8000
  .venv\\Scripts\\python.exe server.py --port 8080  # 换端口

【Key 模式】
  · LLM（DeepSeek）：用户在页面填写，存浏览器本地，随请求头传给服务端
  · Embedding（硅基流动）：由服务端 .env 配置提供（检索在服务端进行）
"""
import argparse
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import web_server        # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="棉花智能问答助手 · Web 服务")
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址（默认 0.0.0.0，允许局域网访问）")
    ap.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    args = ap.parse_args()

    local, lan = web_server.access_urls(args.port)
    print("=" * 58)
    print("  棉花智能问答助手 · Web 服务")
    print("=" * 58)
    print(f"  本机访问   ：{local}")
    print(f"  局域网访问 ：{lan}   （手机连同一 WiFi 可用）")
    print()
    print("  · LLM Key 由用户在页面右上角「设置」填写（存浏览器本地）")
    print("  · Embedding 由服务端配置提供，检索在服务端进行")
    print("  · 按 Ctrl+C 停止服务")
    print("=" * 58)

    import uvicorn

    uvicorn.run(web_server.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
