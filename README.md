# 棉花智能问答助手

面向新疆棉花种植场景的桌面端问答系统。
## 主要功能

**检索增强问答**　农技文档向量化入库，检索采用向量召回—Rerank 精排两阶段流程。
回答优先依据知识库内容；无相关资料时退化为模型通用知识，会在回复中标明。

**四层记忆机制**　对话状态分四层管理：近期对话（L2）、长期摘要（L3，向量化存储，
支持按语义检索历史情节）、跨会话实体记忆（L4，按 fact / preference / episodic 分类，
写入时由模型完成冲突消解）。各层逐级压缩并持久化，用以控制长对话的上下文规模。

**工具调用**　基于模型原生 Function Calling 的多轮工具循环，内置六个工具，覆盖天气查询、
联网搜索、产量与面积统计、棉花价格指数与波动率、趋势绘图、产量预测与产区风险分级。


## 技术栈

| 层次 | 选型 |
|------|------|
| LLM / Embedding | DeepSeek API / 硅基流动（OpenAI 兼容接口） |
| RAG | LangChain + ChromaDB |
| 数据处理 | pandas + numpy + matplotlib |
| UI | PyQt6 |
| 工具暴露 | MCP Python SDK（stdio 传输） |

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

`DEEPSEEK_BASE_URL` 与 `EMBEDDING_BASE_URL` 的默认值即为对应服务商的官方地址，
同一服务商的用户通常无需修改；仅在更换服务商时才需同步调整 URL 与模型名，
例如将 Embedding 换为 OpenAI 官方（`https://api.openai.com/v1`）或阿里百炼等兼容接口。

切换 Embedding 模型会导致向量维度变化，此时必须全量重建知识库
（`python tools/kb_admin.py rebuild`；exe 版启动时会自动检测并重建）。

```bash
python build_kb.py              # 全量构建知识库（data/ 下的 PDF/TXT/MD）
python tools/kb_admin.py sync   # 增量同步：只重新处理有变化的文档
python main.py                  # 启动应用
```

知识库维护通过 `tools/kb_admin.py` 完成。增删改 `data/` 下的文档后无需重建整个向量库，
可用子命令包括 `status`（查看差异）、`sync`（增量同步）、`add`（加入新文档）、
`remove`（移除文档）、`list`（查看已索引文档）与 `rebuild`（全量重建）。
应用启动时也会自动检测差异并增量同步，进度显示在状态栏。

打包 exe：

```bash
pip install pyinstaller
python build_exe.py  # 产物: dist/CottonAgent/
```


## 评测体系

tests文件夹下的程序可用于模型评测：

```bash
python tests/run_all.py        # 产物: eval_report.md（自动区块重建，人工章节保留）
```

| 维度 | 规模 | 指标 |
|---|---|---|
| 检索质量 | 27 条 | Hit@1/3/20、MRR（生产口径：向量召回 top-20 → Rerank 精排 top-3） |
| 工具选择 | 30 条 | 选择准确率（支持 `--repeat` 多次运行评估稳定性） |
| 数值一致性 | 20 条 | 与 CSV 直读对拍（目标 100%） |
| 生成质量 | 20 条 | 拒答准确率 / 要点覆盖率 / 忠实度（LLM-as-judge，端到端完整链路） |
| 长期记忆 | 44 条 | 冲突消解准确率 / 去重率 / 记忆检索 Hit@K |

另有若干不调用 LLM 的回归守卫，适合改动后随手执行：

```bash
python tests/eval_numeric.py        # 数值一致性
python tests/check_data_ranges.py   # 数据范围一致性（防写死的范围随数据更新而过期）
python tests/smoke_memory.py        # 记忆链路冒烟（增删改查隔离性）
python tests/smoke_mcp.py           # MCP 服务端冒烟（改形\协议/stdio 集成）
python tests/check_imports.py       # 未使用导入检查
```

评测全程运行在隔离沙箱中（会话存档与记忆库均为副本），不会触碰生产数据。

## 项目结构

```
cotton_agent/
├── main.py                  # 程序入口（桌面端）
├── mcp_server.py            
├── build_kb.py              # 知识库构建脚本（全量，兼容旧用法）
├── build_exe.py             # exe 打包脚本（主程序/updater）
├── updater_runner.py        # 自动更新安装器（打包为 updater.exe）
├── cotton_agent.spec        # 主程序打包配置（PyInstaller）
├── updater.spec             # updater 打包配置
├── config.py                # 全局配置
├── core/
│   ├── rag_engine.py        # RAG 引擎（消息组装\工具循环\L1~L4 编排）
│   ├── tools/               # 工具层：契约与实现（桌面端与 MCP 共用同一份）
│   │   ├── schemas.py       #   工具契约
│   │   ├── external.py      #   天气 / 联网搜索
│   │   ├── local_data.py    #   统计查询 / 价格指数 / 产量分析
│   │   ├── plot.py          #   趋势绘图（渲染与落盘分离）
│   │   ├── mcp_adapter.py   #   MCP 改形（schema → Tool，ToolResult → content blocks）
│   │   ├── result.py        #   ToolResult（文本 + 可选图片）
│   │   └── json_utils.py    #   工具结果 JSON 序列化（含 numpy 兜底）
│   ├── knowledge_base.py    # 向量库/增量索引维护/RAG 检索
│   ├── user_memory.py       # L3/L4 记忆存储（含 NullMemoryStore）
│   ├── kb_builder.py        # 文档指纹 / 同步规划 / 全量重建
│   ├── cotton_stats.py      # 产量/面积统计查询
│   ├── cotton_price.py      # 价格指数与波动率
│   ├── trend_plot.py        # 图表生成（内存渲染 / 落盘两用）
│   ├── yield_analysis.py    # 产量预测与风险分级
│   ├── updater.py           # 更新检查
│   └── llm_client.py        # DeepSeek API 封装
├── tools/
│   └── kb_admin.py          # 知识库维护 CLI（status/sync/add/remove/list/rebuild）
├── tests/                   # 评测与回归
│   ├── eval_data/           #   评测集（检索/工具/数值/端到端/记忆，共五类）
│   ├── run_all.py           #   全量评测并生成 eval_report.md
│   └── smoke_*.py           #   回归守卫（记忆链路 / MCP）
├── ui/                      # PyQt6 界面
└── data/                    # RAG 文档源
```

## License

MIT
