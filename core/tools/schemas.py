# -*- coding: utf-8 -*-
"""工具契约定义 —— **全项目工具的唯一真相**。

本文件由 `core/rag_engine.py` 中迁出（阶段 1 工具层抽取），格式为
OpenAI / DeepSeek Function Calling 规范：

    {"type": "function", "function": {"name", "description", "parameters"}}

**为什么强调"唯一真相"**：MCP 的 `Tool` 与本格式**同构** ——
`function.name/description` 直接对应，`function.parameters` 就是 `inputSchema`。
因此 MCP 侧（`mcp_server.py`）**不重新声明工具**，而是把本文件改形后直接使用，
从而避免"两份定义各自演化、改一处忘一处"的漂移。见 ROADMAP 方向 D1。
"""

TOOL_SCHEMAS: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取指定城市某一天的天气情况（今天/明天/指定日期）。当用户询问天气、温度、是否能下雨、能否打药时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "城市名称，如：阿拉尔、阿克苏",
                        },
                        "date": {
                            "type": "string",
                            "description": "要查询的日期，可以是 'today'（今天）、'tomorrow'（明天），或 YYYY-MM-DD 格式的具体日期（如 2025-06-15）。默认为 'today'。最多支持未来 3 天预报。",
                        },
                    },
                    "required": ["city"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "在互联网上搜索最新信息。当知识库中没有相关信息, 或用户询问最新政策、新闻、市场行情（如期货价格/期货行情）等实时信息时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_cotton_stats",
                "description": "查询/计算新疆棉花产量与播种面积统计数据。分地区数据覆盖 2015-2022 年，全区主要年份数据覆盖 1978-2022 年。支持：单点查询某地区某年产量/面积、亩产计算（公斤/亩）、多年合计/平均、某年产区排名、全区主要年份查询。当用户询问棉花产量、面积、亩产、产区排名等数据问题时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "region": {
                            "type": "string",
                            "description": "地区名，如：阿克苏地区、塔城地区-沙湾市、生产建设兵团；全区汇总或主要年份查询用 '全区'。",
                        },
                        "year": {
                            "type": "integer",
                            "description": "年份，如 2020。分地区可查 2015-2022，主要年份可查 1978-2022。",
                        },
                        "metric": {
                            "type": "string",
                            "enum": ["yield", "area", "mu", "long_yield", "long_area", "long_mu"],
                            "description": "指标：yield=棉花产量、area=棉花播种面积、mu=棉花亩产（公斤/亩）、long_yield=长绒棉产量、long_area=长绒棉播种面积、long_mu=长绒棉亩产（公斤/亩）。",
                        },
                        "stat": {
                            "type": "string",
                            "enum": ["value", "total", "avg", "rank", "major"],
                            "description": "统计类型：value=单点查询（需 region+year）、total=多年合计、avg=多年平均、rank=某年产区排名（需 year）、major=全区主要年份（需 year）。",
                        },
                        "top": {
                            "type": "integer",
                            "description": "rank 时返回前 N 名，默认 5。",
                        },
                    },
                    "required": ["metric", "stat"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calc_price_volatility",
                "description": "查询/计算中国棉花现货价格指数（CC Index）数据。支持：单日/单月价格查询、波动率（日/年化）、区间涨跌幅、历史价格分位。六个等级：1129B/2129B/3128B/4128B/1228B/2227B，价格单位元/吨。数据范围 2016-01 ~ 2026-07。当用户询问棉花现货价格、价格波动、涨跌幅、价格历史位置等问题时调用。注意：本数据为现货价格指数，不含期货行情；用户询问期货价格/期货行情时请改用 web_search。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "grade": {
                            "type": "string",
                            "enum": ["1129B", "2129B", "3128B", "4128B", "1228B", "2227B"],
                            "description": "价格等级，默认 3128B（CC Index 328 标准等级）。",
                        },
                        "metric": {
                            "type": "string",
                            "enum": ["price", "volatility", "pct_change", "percentile"],
                            "description": "操作：price=查询价格（需 date）、volatility=波动率（需 start/end，可加 period）、pct_change=区间涨跌幅（需 start/end）、percentile=历史价格分位（需 date）。",
                        },
                        "date": {
                            "type": "string",
                            "description": "价格/分位查询的日期，支持 YYYY-MM-DD（当日）或 YYYY-MM（当月最后一个交易日）。",
                        },
                        "start": {
                            "type": "string",
                            "description": "区间起始日期，YYYY-MM-DD 或 YYYY-MM。",
                        },
                        "end": {
                            "type": "string",
                            "description": "区间结束日期，YYYY-MM-DD 或 YYYY-MM（结束月份取当月最后交易日）。",
                        },
                        "period": {
                            "type": "string",
                            "enum": ["monthly", "annual"],
                            "description": "波动率类型：monthly=日波动率（日收益标准差）、annual=年化波动率（×√交易日数），默认 monthly。",
                        },
                    },
                    "required": ["metric"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plot_trend",
                "description": "绘制新疆棉花产量/面积/亩产/价格趋势折线图，支持多地区多系列对比。数据范围：分地区 2015-2022、全区主要年份 1978-2022、价格 2016-2026。当用户要求画图、图表、趋势、走势、对比图时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {
                            "type": "string",
                            "enum": ["yield", "area", "mu", "long_yield", "long_area", "price"],
                            "description": "指标：yield=棉花产量、area=播种面积、mu=亩产（公斤/亩）、long_yield=长绒棉产量、long_area=长绒棉面积、price=价格指数（需 grade，按月取月末价）。",
                        },
                        "regions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "地区列表（产量/面积/亩产指标时用），如 [\"阿克苏地区\", \"喀什地区\"]；省略或空 = 全区。",
                        },
                        "start": {
                            "type": "string",
                            "description": "起始年份（如 2018）或起始月份（price 指标时，如 2021-01）。",
                        },
                        "end": {
                            "type": "string",
                            "description": "结束年份（如 2022）或结束月份（price 指标时，如 2026-07）。",
                        },
                        "grade": {
                            "type": "string",
                            "enum": ["1129B", "2129B", "3128B", "4128B", "1228B", "2227B"],
                            "description": "仅 price 指标使用，默认 3128B。",
                        },
                        "title": {
                            "type": "string",
                            "description": "可选，自定义图表标题。",
                        },
                    },
                    "required": ["metric"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_yield",
                "description": "产量预测区间与产区波动风险分级。forecast：基于 2015-2022 历史数据线性趋势外推，预测任意未来年份产量/面积，返回点预测+置信区间+趋势+R2；risk：按变异系数（CV）对产区波动风险分级（低<0.15/中0.15-0.35/高≥0.35）。当用户问预测产量、未来产量、风险评估、波动风险时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {
                            "type": "string",
                            "enum": ["yield", "area", "long_yield", "long_area"],
                            "description": "指标：yield=棉花产量、area=播种面积、long_yield=长绒棉产量、long_area=长绒棉面积。",
                        },
                        "stat": {
                            "type": "string",
                            "enum": ["forecast", "risk"],
                            "description": "操作：forecast=产量预测（需 region+target_year）、risk=风险分级（可加 regions 筛选）。",
                        },
                        "region": {
                            "type": "string",
                            "description": "forecast 时必填：要预测的地区名（必须与用户原话一致，如：阿克苏地区、喀什地区、生产建设兵团）。",
                        },
                        "target_year": {
                            "type": "integer",
                            "description": "forecast 时必填：预测目标年份（必须晚于 2022）。",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "置信度，默认 0.90（可选 0.68/0.90/0.95，其他值线性插值）。",
                        },
                        "regions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "risk 时可选：仅分析指定地区；省略则分析全部产区。",
                        },
                    },
                    "required": ["metric", "stat"],
                },
            },
        },
    ]
