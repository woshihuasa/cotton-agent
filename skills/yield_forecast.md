---
id: yield_forecast
name: 产量预测与风险评估
version: "0.1"

triggers:
  - 预测产量
  - 产量预测
  - 风险评估
  - 风险分级
  - 能收多少
  - 产区风险

tools_used:
  - analyze_yield
  - query_cotton_stats

summary: |
  预测指定地区未来年份的棉花产量并评估产区波动风险。
  使用线性回归外推 + 置信区间，结果仅供参考。

acceptance:
  - question: 预测一下2027年阿克苏地区的棉花产量
    expected_tools: [analyze_yield]
    key_points: [预测值, 置信区间, 仅供参考]
  - question: 各产区的产量波动风险怎么样
    expected_tools: [analyze_yield]
    key_points: [风险, 分级]

inject_priority: 50
---

# 产量预测与风险评估

## 编排步骤

1. 判断用户意图：**预测**还是**风险评估**
2. 若预测：调用 `analyze_yield`（stat=forecast），获取预测值、置信区间、R²
3. 若风险评估：调用 `analyze_yield`（stat=risk），获取各产区 CV 值与风险等级
4. 若用户同时要求历史数据对比：先调 `query_cotton_stats` 取历史值，再调 `analyze_yield`
5. 呈现结果时**必须声明**："该预测基于历史趋势的统计外推，仅供参考"

## 边界

- 预测仅适用于有 ≥5 年历史数据的地区
- 不做因果归因（"为什么减产"），只做趋势外推
- 风险分级阈值：CV < 0.15 低风险 / 0.15–0.35 中风险 / > 0.35 高风险
