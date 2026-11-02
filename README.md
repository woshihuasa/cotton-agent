# 棉花种植决策智能体（CottonMind）

面向新疆棉花种植的桌面端智能助手。它把农技资料、产量与价格数据、天气预报和日常问答放在同一个窗口里，也会在固定时间主动推送简报与预警。

程序在本机运行，知识库、对话记录与记忆都存在本地，模型和外部数据接口通过配置接入。它面向农技人员、种植大户，以及需要查数据、整理材料的使用者。

## 功能特性

**资料问答**

导入的农技文档会被切分、向量化并存入本地知识库。检索时先用向量召回一批候选段落，再用精排模型重新排序，最后交给大模型组织答案。回答会标注出处与年份，便于核对原文。知识库支持增量同步，改动一篇文档只重新处理这一篇。

**分层记忆**

对话上下文分层管理，逐级压缩并持久化保存。近期对话超过长度阈值时自动摘要，长期摘要可以按语义检索，实体记忆按作用域分成作物画像与项目事实两个库。程序性记忆记录成功的操作路线，下次遇到同类问题可以直接复用。

**工具调用**

内置八个工具，覆盖天气查询、官方气象预警、联网搜索、当日棉花价格、产量与面积统计、价格波动分析、趋势绘图和产量预测。工具由模型自主选择，桌面端与 MCP 服务端共用同一套实现。

**工作流编排**

长流程可以写成 YAML，支持并行节点、断点续跑、节点级重试和失败策略，运行前会做静态校验。需要人工确认的环节可以暂停等待，恢复后不会重复执行已完成的节点。

**主动推送**

程序会在固定时间自己工作：每天清晨推送当天与未来两天的天气和田间建议，在气象台发布预警时说明影响与应对，收盘后推送棉花价格。三类消息进入通知中心，可以翻看历史，也可以单独关闭。

**多服务商接入**

对话模型与向量化服务都可以替换，支持任意兼容 OpenAI 协议的服务商。联网搜索可以在 Tavily 与 DeepSeek 之间切换。凭据保存在本地文件里，界面上只能看到是否已配置。

**感知与轨迹（框架已就位，功能尚未上线）**

感知层用密度估计与分层校准给出带区间的估计，再由不确定性门控决定这个数值能不能用于决策。轨迹模块录制完整对话过程，可导出为 ShareGPT 格式用于后续训练。

## 界面

主窗口由三部分组成。左侧是会话列表，可以新建、重命名和删除对话。中间是聊天区，支持 Markdown 渲染、图表展示和文件引用。右侧是推送抽屉，集中存放简报、预警与价格消息，未读时有角标提示。

设置窗口分为五页：模型、Embedding、其他 API、Web 服务和推送。模型与 Embedding 两页用卡片列出已配置的服务商，可以添加任意兼容端点。推送页用来填写所在地区、开关各项推送，并支持立即运行一次。

## 快速开始

```bash
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env` 并填入配置，或者启动程序后通过设置窗口填写。凭据也可以写入 `credentials.json`，读取优先级为凭据文件、环境变量、空。

```bash
python build_kb.py        # 构建知识库
python main.py            # 启动桌面端
python mcp_server.py      # 以 MCP 服务端形式暴露工具
```

构建知识库需要向量化服务的密钥。更换向量模型会改变向量维度，此时必须重建知识库，桌面端启动时会自动检测并提示。

## 配置项

| 配置项 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | 对话模型密钥，必填 |
| `EMBEDDING_API_KEY` | 向量化服务密钥，构建知识库必需 |
| `EMBEDDING_BASE_URL` | 向量化服务地址，默认使用硅基流动 |
| `EMBEDDING_MODEL` | 向量模型，默认 `BAAI/bge-large-zh-v1.5` |
| `LLM_PROVIDER` | 默认对话服务商，取值见 `core/providers.py` |
| `LLM_MODEL` | 默认模型名称 |
| `EMBEDDING_PROVIDER` | 默认向量化服务商，留空时沿用上面三个 `EMBEDDING_` 开头的配置 |
| `RERANK_BASE_URL` | 精排服务地址，留空时继承向量化服务 |
| `RERANK_API_KEY` | 精排服务密钥，留空时继承向量化服务 |
| `AMAP_API_KEY` | 高德地图密钥，用于地名解析与天气兜底 |
| `TAVILY_API_KEY` | Tavily 搜索密钥 |
| `WEATHER_SOURCE` | 天气数据源，可选 `auto`、`nmc`、`amap` |
| `WEB_SEARCH_BACKEND` | 搜索优先级，可选 Tavily 或 DeepSeek |
| `WEB_SEARCH_TAVILY_ON` | Tavily 搜索开关 |
| `WEB_SEARCH_DEEPSEEK_ON` | DeepSeek 搜索开关 |
| `DEEPSEEK_SEARCH_API_KEY` | DeepSeek 搜索专用密钥，留空时沿用对话模型密钥 |
| `SHIPPED_SKILL_DIR` | 手写技能目录，默认为 `skills` |
| `EXTERNAL_SKILL_DIRS` | 外部技能目录，多个目录用分号分隔 |
| `TRAJECTORY_PATH` | 轨迹录制输出路径，留空表示关闭 |

凭据以明文保存在本机 `%APPDATA%\CottonAgent` 目录下，靠操作系统的用户权限隔离，同一台机器上的其他账户无法读取。

## 常用命令

| 命令 | 用途 |
|---|---|
| `python tools/kb_admin.py status` | 查看知识库与文档的差异 |
| `python tools/kb_admin.py sync` | 只重新处理有变化的文档 |
| `python tools/kb_admin.py add <文件>` | 加入新文档 |
| `python tools/kb_admin.py remove <文件>` | 移除文档 |
| `python tools/kb_admin.py list` | 查看已索引的文档 |
| `python tools/kb_admin.py rebuild` | 全量重建知识库 |
| `python build_release.py` | 构建发布包 |
| `python tests/run_all.py` | 运行评测并生成报告 |

## 项目结构

```
cotton_agent/
├── main.py                   桌面端入口
├── mcp_server.py             MCP 服务端（stdio）
├── bootstrap.py              启动引导：数据目录探测与环境加载
├── config.py                 全局配置
├── paths.py                  构建产物路径
├── build_kb.py               知识库构建
├── build_exe.py              开发构建
├── build_release.py          发布构建
├── updater_runner.py         自动更新安装器
├── core/
│   ├── rag_engine.py         问答引擎
│   ├── knowledge_base.py     向量库与检索
│   ├── kb_builder.py         文档同步与重建
│   ├── user_memory.py        长期记忆读写
│   ├── procedural_memory.py  技能结晶档
│   ├── skill_loader.py       手写技能加载
│   ├── query_router.py       分级网关
│   ├── uncertainty.py        不确定性门控
│   ├── context_budget.py     上下文预算
│   ├── prompt_layers.py      提示分层
│   ├── providers.py          对话服务商登记表
│   ├── embedding_providers.py 向量化服务商登记表
│   ├── credentials.py        凭据读写
│   ├── model_cache.py        模型清单缓存
│   ├── schedule.py           定时触发
│   ├── alarms.py             气象预警抓取与解析
│   ├── weather.py            天气数据源
│   ├── notifications.py      通知存储
│   ├── orchestration/        工作流引擎
│   ├── perception/           感知层
│   └── tools/                工具实现与契约
├── ui/                       PyQt6 界面
├── workflows/                工作流定义
├── prompts/                  提示词
├── skills/                   手写技能
├── data/                     知识库文档源
├── tools/                    维护脚本
├── tests/                    回归守卫与评测
└── requirements.txt
```

## 测试与打包

回归守卫共 45 个，覆盖架构约束、工具契约、编排语义、数据源与界面行为，全部可以离线运行。另有 11 个评测脚本，用固定数据集衡量检索质量、工具选择准确率、生成质量与记忆治理效果。

```bash
python tests/run_all.py       # 产出 eval_report.md
```

打包使用 PyInstaller：

```bash
python build_release.py
```

产物存放在 `release/` 目录。构建脚本会剔除运行期残留文件，检查是否误打包了密钥，然后压缩成发布包。

## 已知限制

知识库中的技术指导文件来自公开渠道，其中一部分文件名不含年份，程序无法判断它们的时效，回答时请结合原文核对。

气象预警与天气数据来自中央气象台网页背后的接口，不是正式开放 API，接口变动会导致取数失败。此时程序会如实报告失败，不会用旧数据冒充当天情况。

感知层目前只有框架和命令行入口，尚未接入正式数据。

## License

MIT