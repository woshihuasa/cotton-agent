# 棉花智能问答助手

基于 Agentic RAG 的桌面端棉花种植问答助手，本地知识库 + 原生工具调用 + 数据统计分析于一体。

## 项目特点

- **Agentic RAG**：农技文档向量化检索，回答优先基于知识库。
- **四层记忆架构**：近期对话（L2）、长期摘要（L3，向量化存储、可语义检索历史情节）、跨会话实体记忆（L4，分类存储 + LLM 冲突消解）逐层压缩与持久化，防止长对话上下文丢失。
- **原生工具调用**：基于 Function Calling 的多轮工具循环，内置 6 个工具：
  - 天气查询、联网搜索
  - 新疆棉花产量 / 面积 / 亩产统计查询
  - 棉花价格指数查询与波动率分析
  - 趋势折线图绘制（自动嵌入回复）
  - 产量预测区间与产区波动风险分级
- **Web 服务**：内置 FastAPI + SSE 服务，桌面端一键启动或命令行启动后，可用浏览器（含手机、平板）访问同一套问答能力。

## 技术栈

| 层次 | 选型 |
|------|------|
| LLM / Embedding | DeepSeek API / 硅基流动（OpenAI 兼容接口） |
| RAG | LangChain + ChromaDB |
| 数据处理 | pandas + numpy + matplotlib |
| UI | PyQt6 |
| Web 服务 | FastAPI + Uvicorn（SSE 流式输出） |

## 快速开始

```bash
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env` 并填入配置，或通过应用内「设置」对话框配置（会自动写入 `.env`）。

| 配置项 | 说明 | 本项目开发时选型 |
|--------|------|-----------------|
| `DEEPSEEK_API_KEY` | **必填**。DeepSeek 大模型密钥 | DeepSeek 官方 API |
| `DEEPSEEK_BASE_URL` | DeepSeek API 地址，**默认即官方** `https://api.deepseek.com`，一般无需修改 | — |
| `DEEPSEEK_MODEL` | 对话模型名 | `deepseek-v4-flash` |
| `EMBEDDING_API_KEY` | **必填**。向量化服务密钥（构建知识库必需） | 硅基流动 |
| `EMBEDDING_BASE_URL` | Embedding 服务地址，**默认硅基流动** `https://api.siliconflow.cn/v1` | 硅基流动 |
| `EMBEDDING_MODEL` | 向量模型名 | `BAAI/bge-large-zh-v1.5`（1024 维） |
| `AMAP_API_KEY` | 可选。高德地图（天气查询） | — |
| `TAVILY_API_KEY` | 可选。Tavily（联网搜索） | — |
| `WEB_ACCESS_TOKEN` | 可选。Web 服务访问口令，**留空则不校验**（本机 / 局域网自用）；对外暴露前建议设置 | — |
| `WEB_RATE_PER_MIN` | 可选。Web 服务单 IP 每分钟提问上限，默认 20 | — |
| `WEB_DAILY_LIMIT` | 可选。Web 服务全站每日提问上限，默认 2000 | — |

> **关于 URL**：`DEEPSEEK_BASE_URL` 与 `EMBEDDING_BASE_URL` 对所有使用同一服务商的用户是统一的（默认值即官方/硅基流动地址，通常不需要改）。仅当**更换服务商**时才需要修改——例如将 Embedding 换成 OpenAI 官方（`https://api.openai.com/v1`）或阿里百炼等兼容接口时，同步修改 URL 与模型名。
>
> **切换 Embedding 模型后**：向量维度会变化，必须**全量重建**知识库（`python tools/kb_admin.py rebuild`，或 exe 打包版启动时会自动检测并重建）。

```bash
python build_kb.py              # 全量构建知识库（data/ 下的 PDF/TXT/MD）
python tools/kb_admin.py sync   # 增量同步：只重新处理有变化的文档
python main.py                  # 启动应用
```

> **知识库维护**（`tools/kb_admin.py`）：增删改 `data/` 下的文档后**无需重建整个向量库**——
> `status` 查看差异 · `sync` 增量同步 · `add` 加入新文档 · `remove` 移除文档 · `list` 查看已索引文档 · `rebuild` 全量重建。
> 应用启动时也会**自动检测并增量同步**，并在状态栏提示进度。

打包 exe：

```bash
pip install pyinstaller
python build_exe.py  # 产物: dist/CottonAgent/
```

## Web 服务（浏览器访问）

除桌面端外，项目内置了一个 Web 服务，可用浏览器（含手机、平板）访问同一套问答能力。

**启动方式（二选一）**：

```bash
# ① 命令行启动
python server.py                # 默认 0.0.0.0:8000
python server.py --port 8080    # 换端口
```

② 桌面端：**设置 → Web 服务** → 点「启动服务」；点击状态栏里的地址可直接用默认浏览器打开。

**本机访问**：浏览器打开 `http://127.0.0.1:8000`。

> **必须写 `http://`**：服务为纯 HTTP（无 TLS 证书）。若浏览器自动补成 `https://`，会报「发送了无效的响应 / 连接不安全」——手动改成 `http://` 即可。

**局域网访问（手机 / 平板）**：

1. 手机与电脑连**同一 WiFi**
2. 浏览器打开 `http://<电脑局域网IP>:8000`（IP 见设置面板状态栏，或用 `ipconfig` 查看）
3. 首次启动时 Windows 会弹窗询问是否允许 Python 访问网络，**需选择允许**；若已点掉，用管理员 PowerShell 补一条放行规则：

```powershell
New-NetFirewallRule -DisplayName "CottonAgent Web 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Profile Private
```

**Key 与访问控制**：

- **LLM Key（DeepSeek）**：由用户在网页上填写，存浏览器 `localStorage`，随请求头传入，**服务端不保存、不落盘**。
- **Embedding**：由服务端配置提供——检索在服务端进行，且向量库与所用模型绑定。
- **访问口令**：在 `.env` 中设置 `WEB_ACCESS_TOKEN` 后，访问者需在网页「设置」中填写同一口令；留空则不校验。
- **限流**：单 IP 每分钟与全站每日两级上限，防止脚本刷爆服务端 Embedding 配额。

> **公网部署提示**：`192.168.x.x` 是局域网私有地址，外网无法直接访问。若要对外提供服务，需部署到云服务器并配置反向代理（如 Caddy 可自动申请 HTTPS 证书）。**暴露到公网前务必设置 `WEB_ACCESS_TOKEN` 并调低限流阈值**，否则服务端的 Embedding 配额可能被刷爆。

## 项目结构

```
cotton_agent/
├── main.py                  # 程序入口（桌面端）
├── server.py                # Web 服务命令行入口
├── build_kb.py              # 知识库构建脚本（全量，兼容旧用法）
├── build_exe.py             # exe 打包脚本（主程序 + updater）
├── updater_runner.py        # 自动更新安装器（打包为 updater.exe）
├── cotton_agent.spec        # 主程序打包配置（PyInstaller）
├── updater.spec             # updater 打包配置（自动生成）
├── config.py                # 全局配置
├── core/
│   ├── web_server.py        # Web 服务（FastAPI 路由 + 会话池 + 访问控制）
│   ├── rag_engine.py        # RAG 引擎 + 工具循环
│   ├── knowledge_base.py    # 向量库 + 增量索引维护 + L4 记忆
│   ├── kb_builder.py        # 文档指纹 / 同步规划 / 全量重建
│   ├── cotton_stats.py      # 产量/面积统计查询
│   ├── cotton_price.py      # 价格指数与波动率
│   ├── trend_plot.py        # 图表生成
│   ├── yield_analysis.py    # 产量预测与风险分级
│   ├── updater.py           # 更新检查
│   └── llm_client.py        # DeepSeek API 封装
├── tools/
│   └── kb_admin.py          # 知识库维护 CLI（status/sync/add/remove/list/rebuild）
├── ui/                      # PyQt6 界面
├── web/                     # Web 前端页面（index.html）
└── data/                    # RAG 文档源
```

## License

MIT
