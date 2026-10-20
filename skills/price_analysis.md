---
id: price_analysis
name: 棉花价格波动分析
version: "0.1"

triggers:
  - 价格波动
  - 价格涨跌
  - 棉花行情
  - 价格走势
  - 多少钱一吨

tools_used:
  - calc_price_volatility
  - plot_trend

summary: |
  分析棉花现货价格指数的走势与波动率。
  可提供历史分位、变异系数等统计指标，并可绘制走势图。

acceptance:
  - question: 最近棉花价格波动大吗
    expected_tools: [calc_price_volatility]
    key_points: [波动率, 变异系数]
  - question: 画一下棉花价格的走势图
    expected_tools: [plot_trend]
    key_points: [图表, 趋势]

inject_priority: 50
---

# 棉花价格波动分析

## 编排步骤

1. 判断用户意图：**查波动率**还是**画图**还是**两者都要**
2. 若查波动率：调用 `calc_price_volatility`，获取变异系数、历史分位
3. 若画图：调用 `plot_trend`（metric=price），生成价格指数走势图
4. 若两者都要：先调 `calc_price_volatility` 取数值，再调 `plot_trend` 出图
5. 呈现时说明：本助手只提供**现货指数**数据，不含期货行情

## 边界

- 数据范围：2016–2026 年月度现货价格指数
- 不提供买卖建议、不预测涨跌（合规边界，见 SYSTEM_PROMPT §3b）
- 期货相关问题明确说明超出能力范围
