# 棉花种植决策智能体（CottonMind）

面向新疆棉花种植场景的智能问答与决策系统：**桌面端应用（PyQt6）**，
内核是「检索增强 + 四层记忆 + 工具编排 + 工作流引擎」。

---

## 一、核心能力

### 检索增强问答
农技文档向量化入库，检索采用 **向量召回 top-20 → Rerank 精排 top-3** 两阶段流程。
回答优先依据知识库内容；无相关资料时退化为模型通用知识，并在回复中标明。
知识库支持**增量同步**：改 1 篇文档只重嵌该篇，启动时自动检测差异。

### 四层记忆 + 技能库
对话状态分层管理，各层逐级压缩并持久化：

| 层 | 内容 | 存储 |
|---|---|---|
| L1 | 实时上下文组装 | System Prompt 注入 L3/L4/技能/背景知识 |
| L2 | 近期对话（Token 超阈值压缩） | 每会话独立 `conversation_store` |
| L3 | 长期摘要（向量化，可按语义检索历史情节） | 每会话独立 `summary_memory` |
| L4 | 实体记忆，按作用域分**画像库 / 项目事实库** | ChromaDB（冲突消解由模型判定） |

**技能库（程序性记忆）** 是 L4 容器的第三库，两条来源方向相反：

- **技能结晶档**：引擎从成功工具调用轨迹中零沉淀的程序性记忆（skill）；
- **手写技能**：`skills/` 下手写的skill。


### 工具调用与三层防线
基于模型原生 Function Calling 的多轮工具循环，内置六个工具：天气查询、联网搜索、
产量与面积统计、棉花价格指数与波动率、趋势绘图、产量预测与风险分级。
桌面端与 MCP 服务端**共用同一份实现与契约**。

工具调度有三层防线，把"工具调错"从静默失效变成可纠正：

1. **名字解析** —— 别名归一，失败时给出带候选的纠正提示；
2. **参数多级降级** —— 标准 JSON → 剥围栏 → 抽 `{...}` → `ast.literal_eval`，全失败则**返回失败并回灌模型**（不再静默降级为 `{}`）；
3. **循环守卫** —— 检测原地打转（相同参数反复调用同一工具）。

另有**能力声明 + 单一 fail-closed 闸门**（`core/tools/capabilities.py`）：
注解放在与 schema 平行的表里（不污染 `TOOL_SCHEMAS` 的单一真相），
未声明即按「非只读 · 可能破坏 · 开放世界」处理，**漏声明的失败方向是更严**。

### 编排层（workflow引擎）
它是一条**并行第二路径**，不替代原有工具循环；只有明确写成工作流的长任务才进这里。

- **可声明**：流程写在 YAML 里，能 diff、能 code review、能被守卫校验；
- **可校验**：环、孤立节点、未知节点类型、必填缺失 —— 静态就能查出来，零成本；
- **可续跑**：人工确认/长任务落盘断点，重载后**不重复执行已完成节点**。

执行语义：拓扑排序 → ready batch → 批内并发（**matplotlib 渲染类强制串行**）、
节点级重试（**仅幂等节点**）+ 指数退避、超时、`fail_fast`/`degrade`/`skip_if_optional` 失败策略、熔断。

编排层通过命令 `/perception` 调用

### 主动触发（定时与预警）
编排层管「怎么跑」，这一层管「什么时候跑」。它按 `schedule.json` 的配置，在每天固定
时刻拉起农情简报，或者按固定间隔取次日天气、由规则判断要不要发预警。两类任务都只是
提交一次工作流，重试、断点与失败策略仍然由编排层负责。

判定写成纯函数，只有三条规则：到点才触发；一个计划时刻只处理一次；错过补发窗口就
不再补。天气接口失败或者字段解析不出来时一律按「没有信号」处理，不会当成正常放行。
推送内容取自工作流里指定节点的字段，取不到就报失败，不拿空内容凑一条。默认配置是
每天 08:00 给阿克苏出一份简报，每三小时看一次次日预报。

### 分级网关与不确定性门控
三道**正交**闸门，各管一件事：

| 闸门 | 管什么 | 实现 |
|---|---|---|
| 分级网关 | **要不要检索** | `core/query_router.py` —— 白名单式准入，没把握一律付全价（走全链） |
| 能力闸门 | **准不准调用这个工具** | `core/tools/capabilities.py` —— fail-closed |
| 不确定性门控 | **这个数值能不能拿去做决策** | `core/uncertainty.py` —— 四档动作 |

不确定性门控按「区间是否跨阈值 × 区间宽窄」给出四档动作：
`ADVISE` / `ADVISE_WITH_INTERVAL`（必须同时给区间）/ `QUALITATIVE`（只许定性）/
`ESCALATE`（弃答，说明信息不足）。**关键点是"LLM 不可越权硬门"** —— 措辞由门控决定，模型无法自行升级。

### 上下文预算与提示分层
L1 组装有五个来源（L3 摘要 / L4 记忆 / 结晶档 / 手写技能 / 背景知识），
预算分两级：**逐源硬上限**（防单源吞掉整个窗口）→ **全局代价最小化**（按 `priority / cost` 贪心填充）。
`BudgetRow` 逐源记录 `original_cost / final_cost / kept / truncated / dropped`。

提示分层（`core/prompt_layers.py`）让**上下文缓存命中**可测量：稳定段（含每日只变一次的日期）
在前、易变内容在后，`stable_hash` 进日志，守卫断言同一日期内恒定 ——
任何"把可变内容插进前缀"的改动都会被当场抓住。

### 多服务商接入
支持多种api服务商接入，由网关统一管理。

### 感知层与轨迹（目前仅有框架，功能未上线）
- **感知层**（`core/perception/`）：密度估计 + 分层校准 → 带区间的估计 → 不确定性门控 → 决策报告。
  输出是**估计量而非真值**，故必须过门控才允许下结论。
  对话中输入 `/perception` 即端到端触发这条链（`workflows/perception_report.yaml`：
  契约 → 门控 → Markdown 报告），**不经过 LLM**。
- **轨迹**（`core/trajectory.py`）：录制全保真原始轨迹，导出为 ShareGPT 格式（纯函数派生，
  格式改了只重导、不必重跑问答）。`TRAJECTORY_PATH` 为空即关闭（**路径本身就是配置**）。

---

## 二、技术栈

| 层次 | 选型 |
|------|------|
| LLM / Embedding | LLM可兼容多种服务商 / 硅基流动（OpenAI 兼容接口） |
| RAG | LangChain + ChromaDB |
| 数据处理 | pandas + numpy + matplotlib |
| UI | PyQt6 |
| 工具暴露 | MCP Python SDK（stdio 传输） |
| 打包 | PyInstaller（onedir）+ 自动更新器 |

---

## 三、快速开始

```bash
pip install -r requirements.txt      # 或 requirements.lock.txt 复刻精确版本
```

复制 `.env.example` 为 `.env` 并填入配置，或通过应用内「设置」对话框配置。
凭据也可写入 `credentials.json`（优先级：`credentials.json` → `.env` → 空），
UI **只能**查询"是否已配置"与指纹，**永远拿不到完整密钥**。
这两个文件（`credentials.json` / `providers.json`）是**本地凭据存储，已在 `.gitignore` 中**。

> ⚠️ **凭据是明文**。桌面端加密属安全剧场（解密密钥必须和密文放在同一台机器上），
> 真正的保护来自操作系统的用户级 ACL —— 文件落在 `%APPDATA%`，同机其他 Windows 用户读不到。

| 配置项 | 说明 |
|--------|------|
| `DEEPSEEK_API_KEY` | **必填**。大模型密钥（或改用其他服务商，见 `LLM_PROVIDER`） |
| `EMBEDDING_API_KEY` | **必填**。向量化服务密钥（构建知识库必需） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | 默认硅基流动 / `BAAI/bge-large-zh-v1.5`（1024 维） |
| `LLM_PROVIDER` / `LLM_MODEL` | 选择默认服务商与模型（登记表见 `core/providers.py`） |
| `AMAP_API_KEY` / `TAVILY_API_KEY` | 可选。天气查询 / 联网搜索 |
| `SHIPPED_SKILL_DIR` / `EXTERNAL_SKILL_DIRS` | 技能挂载点（默认 `skills`；外部目录用 `;` 分隔） |
| `TRAJECTORY_PATH` | 轨迹录制**输出路径，留空 = 关闭** |

切换 Embedding 模型会导致向量维度变化，此时必须全量重建知识库
（`python tools/kb_admin.py rebuild`；exe 版启动时会自动检测并重建）。

```bash
python build_kb.py                    # 全量构建知识库（data/ 下的 PDF/TXT/MD）
python tools/kb_admin.py sync         # 增量同步：只重新处理有变化的文档
python main.py                        # 启动桌面端
python mcp_server.py                  # 以 MCP 服务端暴露六个工具（stdio）
```

**知识库维护**（`tools/kb_admin.py`）：
`status`（查看差异）· `sync`（增量同步）· `add`（加入新文档）· `remove`（移除文档）·
`list`（查看已索引文档）· `rebuild`（全量重建）。应用启动时也会自动检测差异并增量同步。

> ⚠️ `add` 会把文件**拍平到 `data/` 根目录**，破坏目录分类。要保留分类请
> 「按目录手动放入 `data/` → 跑 `sync`」。

**打包发布**：

```bash
python build_release.py               # 构建 → 剔除运行期残留 → 签名 → 密钥自检 → 压缩
```

产物统一收纳在 `release/`（路径常量集中在 `paths.py`，是单一真相）。
发布前会跑 `tests/check_secrets.py` 自检 —— **不过就不压缩**（防止把 `.env` 打进发布包）。

**运行期资产必须随包分发**，否则对应功能在打包版直接失效。当前清单（`cotton_agent.spec` 的 `datas`）：

| 资产 | 缺了会怎样 |
|---|---|
| `data/` `skills/` `web/` `ui/icons/` `build_version.txt` | 知识库 / 技能 / 页面 / 图标 / 版本号失效 |
| `workflows/` | `/perception` 报"工作流文件不存在"、`/workflow` 页面恒空 |
| `prompts/` | `llm.decide` / `llm.summarize` 节点必失败（`prompt_ref` 是必填） |
| `data-gym-cache/` | token 计数在**离线**环境会去 OpenAI 下载词表，无网即报错 |


---

## 四、评测与守卫

```bash
python tests/run_all.py               # 产物: eval_report.md（自动区块重建，人工章节保留）
```

`run_all.py` 实际编排的是下面这些（**10 个条目、7 个脚本**）：

| 评测项 | 规模 | 指标 |
|---|---|---|
| 评测集完整性 | 6 个数据集 | 结构校验（`verify_datasets.py`） |
| 数据范围一致性 | 9 项 | 工具描述里的数据范围 vs 实际 CSV（防写死的范围过期） |
| 数值一致性 | 20 条 | 与 CSV 直读对拍（目标 100%） |
| 工具选择准确率 | 30 条 | 选择准确率（支持 `--repeat` 评估稳定性） |
| 检索质量 | 27 条 | Hit@1/3/10/20、MRR（生产口径：向量召回 top-20 → Rerank 精排 top-3） |
| 检索召回上限 | 27 条 | Hit@20 探针 + 未命中自动归因（排序问题 vs 召回问题） |
| 生成质量 | 20 条 | 拒答准确率 / 要点覆盖率 / 忠实度（LLM-as-judge，端到端完整链路） |
| 记忆质量 | 45 条 | 冲突消解准确率 / 去重率 / 记忆检索 Hit@K（三组共用一份集） |

**另有 6 个独立评测脚本**（未接入 `run_all.py`，需单独运行）：

| 脚本 | 测什么 |
|---|---|
| `eval_procedural.py` | 技能库检索质量（25 条「问题 → 期望路线」+ 4 条已知边界） |
| `eval_skill_acceptance.py` | 技能 `acceptance` 用例端到端（工具选择层 + 结晶档首次触发） |
| `eval_scenarios.py` | 产物级场景评测 —— 断言「机制是否触发」，而非「答案像不像」 |
| `eval_error_injection.py` | 感知误差注入（零 LLM、零外部数据） |
| `eval_judge_agreement.py` | judge 一致性 —— 用多个**独立评委**复核同一批真实回答 |
| `eval_judge_defects.py` | judge **阳性对照** —— 用"已知有缺陷的回答"测评委的判别力 |


**回归守卫**共 **36 个**（34 个 `check_*.py` + 2 个 `smoke_*.py`），
加上 11 个 `eval_*.py` 评测脚本，`tests/` 合计 **47 个脚本 / 1,198 条断言**；

| 主题 | 守卫 |
|---|---|
| 架构与信任 | `check_architecture` · `check_skill_loader` · `check_skill_wiring` · `check_memory_split` |
| Agent 运行时 | `check_context_budget` · `check_prompt_layers` · `check_tool_dispatch` · `check_tool_gate` · `check_uncertainty` · `check_query_router` · `check_trajectory_export` |
| 编排层 | `check_workflow_validate` · `check_workflow_engine` · `check_workflow_gates` · `check_workflow_contracts` · `check_workflow_checkpoint` · `check_workflow_review` · `check_workflow_llm` · `check_workflow_web` · `check_handoff` |
| 服务商与凭据 | `check_providers` · `check_providers_ui` · `check_credentials` |
| 感知与记忆 | `check_perception` · `check_procedural_memory` · `check_l4_conflict` · `smoke_memory` |
| 数据与契约 | `check_data_ranges` · `check_kb_freshness` · `check_secrets` · `smoke_mcp` · `check_eval_metrics` · `check_error_injection` |
| 其他 | `check_imports` · `check_ui_attrs` · `check_interview_docs` |

---

## 五、项目结构

```
cotton_agent/
├── main.py                    # 桌面端入口（bootstrap → 日志/excepthook → config → UI）
├── mcp_server.py              # MCP 服务端（stdio）
├── bootstrap.py               # 启动引导：可写数据目录探测 + 临时目录重定向 + tiktoken 词表缓存钉死 + .env 查找
├── config.py                  # 全局配置（三区分离：资源 / 运行数据 / 构建产物）
├── paths.py                   # 构建产物路径单一真相（release/ 布局）
├── build_kb.py                # 知识库全量构建
├── build_exe.py               # 开发用构建（产出可试跑目录）
├── build_release.py           # 发布包构建（剔除残留 + 签名 + 密钥自检 + 压缩）
├── updater_runner.py          # 自动更新安装器
├── cotton_agent.spec / updater.spec
├── core/
│   ├── rag_engine.py          # RAG 引擎（消息组装 / 工具循环 / L1~L4 编排）
│   ├── knowledge_base.py      # 向量库 / 增量索引 / 检索与 Rerank
│   ├── kb_builder.py          # 文档指纹 / 同步规划 / 全量重建
│   ├── user_memory.py         # L4 容器读写（画像库 + 项目事实库 + L3 摘要）
│   ├── procedural_memory.py   # 技能结晶档（零 LLM，本机私有）
│   ├── memory_scope.py        # 记忆作用域判据（唯一实现，写入与治理共用）
│   ├── skill_loader.py        # 手写技能加载器（现扫挂载点 + 解析 + 匹配 + 渲染）
│   ├── query_router.py        # 分级网关（要不要检索）
│   ├── uncertainty.py         # 不确定性门控（数值能不能用）
│   ├── context_budget.py      # 上下文双层预算 + 逐源审计
│   ├── prompt_layers.py       # 提示分层与稳定前缀指纹（缓存可测量）
│   ├── providers.py           # 模型服务商登记表（唯一真相）
│   ├── credentials.py         # 凭据层（独立文件 + 只写接口）
│   ├── handoff.py             # 未完成任务的「部分结果」提示（移交机制已停用）
│   ├── task_runner.py         # 后台线程跑工作流（不阻塞对话）
│   ├── schedule.py            # 定时与预警触发（到点跑一次 / 按规则报警；判定是纯函数）
│   ├── trajectory.py          # 轨迹录制与 ShareGPT 导出
│   ├── orchestration/         # 工作流引擎：model / loader / catalog / validator / engine / store / gates
│   ├── perception/            # 感知层：推理 / 几何 / 校准 / 契约 / 门控 / 报告
│   ├── tools/                 # 工具层（桌面端与 MCP 共用同一份）
│   │   ├── schemas.py         #   工具契约（单一真相，字节稳定）
│   │   ├── capabilities.py    #   能力声明 + fail-closed 闸门
│   │   ├── dispatch.py        #   名字解析 / 参数降级 / 循环守卫
│   │   ├── external.py        #   天气 / 联网搜索
│   │   ├── local_data.py      #   统计 / 价格 / 产量分析
│   │   ├── plot.py            #   趋势绘图（渲染与落盘分离）
│   │   ├── mcp_adapter.py     #   MCP 改形
│   │   ├── result.py          #   ToolResult（文本 + 可选图片）
│   │   └── json_utils.py      #   JSON 序列化（含 numpy 兜底）
│   ├── cotton_stats.py / cotton_price.py / trend_plot.py / yield_analysis.py
│   ├── config_manager.py      # 设置读写
│   ├── updater.py             # 更新检查
│   └── llm_client.py          # LLM API 封装
├── skills/                    # ★ 手写技能（随版本分发，只读挂载）
├── workflows/                 # ★ 工作流 YAML（权威定义；runs/ 为运行态，已忽略）
├── prompts/                   # ★ 提示词资产：llm.decide / llm.summarize 节点的 prompt_ref 依赖（必填）
├── tools/
│   ├── kb_admin.py            # 知识库维护 CLI
│   └── export_trajectory.py   # 轨迹导出为 ShareGPT
├── ui/                        # PyQt6 界面（主窗口 / 设置 / 服务商编辑器 / 侧栏）
├── tests/                     # 评测（eval_*）+ 回归守卫（check_* / smoke_*）
├── data/                      # RAG 文档源（按主题分目录）
├── docs/                      # 设计文档（本地文档，不进版本控制）
├── reports/                   # 感知分析报告（运行时产物，已忽略）
├── release/                   # 构建产物（已忽略）
└── requirements.txt / requirements.lock.txt
```
---

## License

MIT