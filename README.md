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

## 技术栈

| 层次 | 选型 |
|------|------|
| LLM / Embedding | DeepSeek API / 硅基流动（OpenAI 兼容接口） |
| RAG | LangChain + ChromaDB |
| 数据处理 | pandas + numpy + matplotlib |
| UI | PyQt6 |

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

## 项目结构

```
cotton_agent/
├── main.py                  # 程序入口
├── build_kb.py              # 知识库构建脚本（全量，兼容旧用法）
├── build_exe.py             # exe 打包脚本（主程序 + updater）
├── updater_runner.py        # 自动更新安装器（打包为 updater.exe）
├── cotton_agent.spec        # 主程序打包配置（PyInstaller）
├── updater.spec             # updater 打包配置（自动生成）
├── config.py                # 全局配置
├── core/
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
└── data/                    # RAG 文档源
```

## License

MIT
