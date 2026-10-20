# 棉花种植决策智能体（CottonMind）

面向新疆棉花种植场景的智能问答与决策系统：**桌面端（PyQt6）+ Web 端（FastAPI）**，
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

- **技能结晶档**（自下而上）：引擎从成功工具调用轨迹中零 LLM 沉淀经验条目，本机私有；
- **手写技能**（自上而下）：`skills/` 下人写的编排指导，随版本分发、只读挂载。

高命中的结晶条目经人审后可晋升为手写技能。

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

### 编排层（工作流引擎）
把"一步步调工具"变成**可声明、可校验、可续跑**的工作流。
它是一条**并行第二路径**，不替代原有工具循环；只有明确写成工作流的长任务才进这里。

- **可声明**：流程写在 YAML 里，能 diff、能 code review、能被守卫校验；
- **可校验**：环、孤立节点、未知节点类型、必填缺失 —— 静态就能查出来，零成本；
- **可续跑**：人工确认/长任务落盘断点，重载后**不重复执行已完成节点**。

执行语义：拓扑排序 → ready batch → 批内并发（**matplotlib 渲染类强制串行**）、
节点级重试（**仅幂等节点**）+ 指数退避、超时、`fail_fast`/`degrade`/`skip_if_optional` 失败策略、熔断。

主链路与编排层之间由 **移交机制**（`core/handoff.py`）衔接：工具循环轮次撞墙时，
把已跑过的调用打包交给编排层续跑，**不猜"还剩哪些目标要查"**，做不到就如实告知"链条没走完"。

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
`BudgetRow` 逐源记录 `original_cost / final_cost / kept / truncated / dropped`，
把"prompt 为什么这么长"从猜测变成可复盘的事实。

提示分层（`core/prompt_layers.py`）让**上下文缓存命中**可测量：稳定段（含每日只变一次的日期）
在前、易变内容在后，`stable_hash` 进日志，守卫断言同一日期内恒定 ——
任何"把可变内容插进前缀"的改动都会被当场抓住。

### 多服务商接入
`core/providers.py` 是「哪家、什么端点、要什么 key、有什么坑」的唯一真相。
三条硬规则：**缺 key 必须报错、不许静默换一家**（否则指标口径悄悄变了）；
**judge 不走 failover**（评测中途换模型 = 两个评委打分，比没有分数更糟）；
**错误分两类**——换一家会改变结果的可重试，否则直接报错。

### 感知层与轨迹
- **感知层**（`core/perception/`）：密度估计 + 分层校准 → 带区间的估计 → 不确定性门控 → 决策报告。
  输出是**估计量而非真值**，故必须过门控才允许下结论。
- **轨迹**（`core/trajectory.py`）：录制全保真原始轨迹，导出为 ShareGPT 格式（纯函数派生，
  格式改了只重导、不必重跑问答）。`TRAJECTORY_PATH` 为空即关闭（**路径本身就是配置**）。

---

## 二、技术栈

| 层次 | 选型 |
|------|------|
| LLM / Embedding | DeepSeek / 千问 Token Plan / 硅基流动（OpenAI 兼容接口） |
| RAG | LangChain + ChromaDB |
| 数据处理 | pandas + numpy + matplotlib |
| UI | PyQt6（桌面端）· 原生 HTML/JS（Web 端） |
| 服务化 | FastAPI + SSE |
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
python server.py                      # 启动 Web 服务（FastAPI + SSE）
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
| `eval_procedural.py` | 技能库检索质量（20 条「问题 → 期望路线」+ 4 条已知边界） |
| `eval_skill_acceptance.py` | 技能 `acceptance` 用例端到端（工具选择层 + 结晶档首次触发） |
| `eval_scenarios.py` | 产物级场景评测 —— 断言「机制是否触发」，而非「答案像不像」 |
| `eval_error_injection.py` | 感知误差注入（零 LLM、零外部数据） |
| `eval_judge_agreement.py` | judge 一致性 —— 用多个**独立评委**复核同一批真实回答 |
| `eval_judge_defects.py` | judge **阳性对照** —— 用"已知有缺陷的回答"测评委的判别力 |

> 最后两个的存在理由值得单说：`eval_generation.py` 里的 judge 与被测系统**曾是同一个模型**
> （同模型自评 —— 模型倾向认为自己风格的答案更好，且它一贯理解错的点也会判对），
> 因此当时报出的忠实度指标带一个未标注的前提。这两个实验就是把那个前提**显式测出来** ——
> 见 `docs/LLM_GATEWAY.md`。

**回归守卫**共 **36 个**（`check_*.py` / `smoke_*.py`），绝大多数**零成本**（不调 LLM），
适合改动后随手执行。按主题分组：

| 主题 | 守卫 |
|---|---|
| 架构与信任 | `check_architecture` · `check_skill_loader` · `check_skill_wiring` · `check_memory_split` |
| Agent 运行时 | `check_context_budget` · `check_prompt_layers` · `check_tool_dispatch` · `check_tool_gate` · `check_uncertainty` · `check_query_router` · `check_trajectory_export` |
| 编排层 | `check_workflow_validate` · `check_workflow_engine` · `check_workflow_gates` · `check_workflow_contracts` · `check_workflow_checkpoint` · `check_workflow_review` · `check_workflow_llm` · `check_workflow_web` · `check_handoff` |
| 服务商与凭据 | `check_providers` · `check_providers_ui` · `check_credentials` |
| 感知与记忆 | `check_perception` · `check_procedural_memory` · `check_l4_conflict` · `smoke_memory` |
| 数据与契约 | `check_data_ranges` · `check_kb_freshness` · `check_secrets` · `smoke_mcp` · `check_eval_metrics` · `check_error_injection` |
| 其他 | `check_imports` · `check_ui_attrs` · `check_interview_docs` |

评测全程运行在隔离沙箱中（会话存档与记忆库均为副本），**不会触碰生产数据**。
守卫会为每次运行建一个 `cotton_test_*` 临时目录做隔离 ——
这些目录内含**真实 `.env` 的副本**，因此已在 `.gitignore` 中整体忽略。

> ⚠️ **一个已知取舍**：`tests/eval_fixtures/`（约 17.7 MB 固定数据）不进仓库。
> 代价是**克隆仓库后跑不了依赖 fixture 的评测**；若需要，请向维护者索取。

---

## 五、仓库约定：哪些内容不进版本控制

除常规的构建产物与缓存外，有几类**刻意排除**的路径，各自的理由不同：

| 路径 | 为什么不进仓库 |
|---|---|
| `.env` / `credentials.json` / `providers.json` | **含明文密钥**。后两者当前是空壳，但用户在界面填一次 Key 就会含真实凭据 |
| `cotton_test_*/` | 守卫的隔离目录，内含**真实 `.env` 副本** —— 提交等于扩散密钥 |
| `procedural_memory/` | 技能结晶档：引擎从**本机轨迹**生成，属本机私有，绝不入库 |
| `chroma_db/` `chroma_user_memory/` | 本地向量库与用户记忆（含个人数据） |
| `session_state.json*` | 会话存档（含对话内容） |
| `docs/` | 设计与决策记录，当前**暂为本地文档**。若要公开，需先修订 `.gitignore` 的放行策略 |
| `reports/` `charts/` `workflows/runs/` | 运行时产物 |
| `release/` `dist/` `build/` `*.zip` | 构建产物 |

**反过来，有几类必须进 git**（否则项目无法自洽）：

- `workflows/*.yaml` —— 工作流**权威定义**，要能 diff 与 code review（只忽略 `runs/` 运行态）；
- `skills/` —— 手写技能是"人写"档，生命周期归 git 管（见 `docs/ARCHITECTURE.md`）；
- `tests/eval_data/*.jsonl` —— 评测集，指标的可复现性依赖它。

> 判据来自项目的**核心不变量**：**运行区的物理目录只放「不是人写在 git 里的东西」。
> 凡是人写进 git 的，一律挂载只读，不复制** —— 复制会造成两份真相，而守卫守不住两份真相。

---

## 六、项目结构

```
cotton_agent/
├── main.py                    # 桌面端入口（bootstrap → 日志/excepthook → config → UI）
├── server.py                  # Web 服务入口（FastAPI + SSE）
├── mcp_server.py              # MCP 服务端（stdio）
├── bootstrap.py               # 启动引导：可写数据目录探测 + 临时目录重定向 + .env 查找
├── config.py                  # 全局配置（三区分离：资源 / 运行数据 / 构建产物）
├── paths.py                   # 构建产物路径单一真相（release/ 布局）
├── build_kb.py                # 知识库全量构建
├── build_exe.py               # 开发用构建（产出可试跑目录）
├── build_release.py           # 发布包构建（剔除残留 + 签名 + 密钥自检 + 压缩）
├── updater_runner.py          # 自动更新安装器
├── cotton_agent.spec / updater.spec
├── core/
│   ├── rag_engine.py          # RAG 引擎（消息组装 / 工具循环 / L1~L4 编排 / 移交）
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
│   ├── handoff.py             # 主链路 → 编排层移交
│   ├── task_runner.py         # 后台线程跑工作流（不阻塞对话）
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
│   ├── web_server.py          # Web 服务实现（可嵌入，桌面端一键启停）
│   └── llm_client.py          # LLM API 封装
├── skills/                    # ★ 手写技能（随版本分发，只读挂载）
├── workflows/                 # ★ 工作流 YAML（权威定义；runs/ 为运行态，已忽略）
├── prompts/                   # 提示词资产
├── tools/
│   ├── kb_admin.py            # 知识库维护 CLI
│   └── export_trajectory.py   # 轨迹导出为 ShareGPT
├── ui/                        # PyQt6 界面（主窗口 / 设置 / 服务商编辑器 / 侧栏）
├── tests/                     # 评测（eval_*）+ 回归守卫（check_* / smoke_*）
├── data/                      # RAG 文档源（按主题分目录）
├── web/                       # Web 前端页面（对话页 + 工作流页）
├── docs/                      # 设计文档（架构 / 编排 / 感知 / 记忆 / 网关 / 路线图；不进版本控制）
├── reports/                   # 感知分析报告（运行时产物，已忽略）
├── release/                   # 构建产物（已忽略）
└── requirements.txt / requirements.lock.txt
```

---

## 七、设计文档

`docs/` 下是各子系统的设计与决策记录（文档在公开仓库中默认被忽略，属本地文档）：

| 文档 | 内容 |
|---|---|
| `ARCHITECTURE.md` | 目录与信任架构（两个根 / 三档生产方式 / 挂载语义 / `SKILL.md` 规范） |
| `PROCEDURAL_MEMORY.md` | L4 容器与技能库（作用域框架 / 条目定义 / 写入门禁 / 确定性检索） |
| `ORCHESTRATION.md` | 编排层（节点类型系统 / 执行语义 / 断点 / 确定性门禁） |
| `HANDOFF.md` | 主链路 ↔ 编排层的移交与续跑 |
| `AGENT_RUNTIME.md` | Agent 运行时加固（上下文工程 / 工具闸门 / 循环可测性） |
| `PERCEPTION.md` | 感知层接入规划与契约 |
| `LLM_GATEWAY.md` | 多服务商接入与 judge 独立性 |
| `ABSORPTION.md` | 参照项目的吸收清单（吸收了什么 / 拒绝了什么） |
| `ROADMAP.md` | 能力现状、已知短板与升级方向 |

**几条贯穿全项目的工程纪律**（详见各文档）：

- **单一真相** —— 工具契约、地区名单、作用域判据、服务商登记、构建路径，各只有一份实现；
  「两份真相」是本项目反复在防的头号问题。
- **失败方向要安全** —— 闸门抛异常一律拒绝（守卫抛异常时若放行，等于没守卫）；
  未声明能力按更严处理；解析失败**回灌模型**而非静默降级。
- **判死优于挂起** —— 无法落地的设计明确判死或给出**可触发的复活条件**，
  避免长期挂着产生决策内耗。

---

## License

MIT