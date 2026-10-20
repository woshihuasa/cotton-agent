# 感知层 · 检测器 → 计数区间

> 论文第 3 章（SQ1 可测性）+ 第 4 章（不确定性契约）的技术底座。
> 路线依据：[YOLO侧技术路线.md](../YOLO侧技术路线.md)

## 这是什么

把**师兄的 YOLO 检测器**（黑箱，不训练不修改）的输出，变成**带覆盖率保证的密度区间**。

```
影像 → [infer.py] → 框列表 → [geometry.py] → 密度 → [calibrate.py] → 区间
        滑窗推理      原图坐标     几何换算   个/m²      校准          [lo, hi]
```

## 三个模块，各自可单独验证

| 模块 | 职责 | 验证命令 |
|---|---|---|
| `infer.py` | 滑窗切图 → 推理 → 跨切片去重 → 原图坐标 | `python infer.py --selftest` |
| `geometry.py` | 框数 ÷ 覆盖面积 → 密度 | `python geometry.py --selftest` |
| `calibrate.py` | 测偏置散度 → 分层校准 → 区间 + 覆盖率 | `python calibrate.py --selftest` |

**三个自检都不需要 .pt 权重**，能在拿到权重之前就验证除模型外的全部逻辑。

## 快速开始

```bash
# 1. 装依赖
pip install ultralytics opencv-python numpy pyyaml

# 2. 跑三个自检（确认逻辑正确）
cd perception
python infer.py --selftest
python geometry.py --selftest
python calibrate.py --selftest

# 3. 填配置
#    编辑 config.yaml：
#      - detector.weights → 师兄的 .pt 路径
#      - inference.conf_threshold / nms_iou → 师兄的现用值（★ 关键）
#      - paths.image_dir → 影像目录

# 4. 先跑 1 张验证
python infer.py --config config.yaml --limit 1

# 5. 全量
python infer.py --config config.yaml
```

## 三个自检在验证什么

### `infer.py --selftest`
- 切片规划：5280×3956 → 约 80 片，**全图覆盖率必须 100%**
- 坐标还原：切片内坐标 + 偏移 = 原图坐标
- 跨切片去重：重合框被抑制，独立框保留
- 质量判定：全白图被判过曝

### `geometry.py --selftest`（对账已有算术）
- GSD：5/7/9m → 应为 0.12 / 0.17 / 0.22 cm/px
- **单切片面积随高度变化**：5m 约 0.59 m²、9m 约 1.9 m²（比值约 3.24）
  → 这就是**为什么必须换算成密度**：同一个框数在不同高度下不是同一个量
- 切片数 ≈ 80 片
- **重叠面积只算一次**：朴素算法（片数×单片面积）会高估约 60%，去重后应≈全图面积
- 无效切片被正确剔除

### `calibrate.py --selftest`
- 有限样本修正：100 个分数取 alpha=0.1 → 分位数是 91 不是 90
- log 空间残差：检测 700/真值 1000 → 负值（少计）
- **合成数据覆盖率**：偏置 0.7 能被正确估计；域内覆盖率 ≥ 名义值
- **跨域覆盖率下降**：偏置变成 0.5 时覆盖率明显掉（这正是论文要找的信号）
- 分层校准：域 A（偏置 0.8）和域 B（偏置 0.5）各自被正确捕捉
- 小样本警示：n=4 被标为不可靠

## 关键设计（都是踩过坑才有的）

| 设计 | 为什么 |
|---|---|
| **估计量是密度不是计数** | 5m 切片覆盖 0.59 m²、9m 约 2 m²，同一个「1240」不是同一个量 |
| **log 空间残差** | NMS 少计是乘性的（密度越高漏越多），log 空间里乘性变加性 |
| **重叠面积只算一次** | 80 片重叠后实际只拍 31 m²，朴素相加会虚高 60% |
| **分位数取 91 不是 90** | 有限样本 +1 修正，是覆盖率保证的来源 |
| **分层校准（Mondrian）** | 保证每个域内部达标，而不是整体平均达标 |
| **n<10 标不可靠** | 小样本算出的分位数没有意义 |
| **必须报 Wilson 区间** | n=36 时单个点即 2.8pp，不报区间会被误读 |

## 契约输出（对接 cotton_agent）

`calibrate.py` 的 `CalibrationModel.to_dict()` 产出校准表条目；
每条计数记录的完整契约见 [测控路线操作手册.md](../测控路线操作手册.md) §8。

`calibration_id` 是外键 —— 覆盖率是**校准程序的属性**，不是单条记录的属性。

## 待填的卡点

| 项 | 在哪填 | 拿不到怎么办 |
|---|---|---|
| `.pt` 权重路径 | `config.yaml` → `detector.weights` | 先跑自检 |
| **conf / NMS 阈值** | `config.yaml` → `inference.*` | 用默认值 + 敏感性分析，论文标注 |
| 影像目录 | `config.yaml` → `paths.image_dir` | — |
| 相机覆盖系数 | `config.yaml` → `geometry.camera.coverage_factor` | **Mavic3M/3T 必须单独确认** |
| 飞行高度 | `config.yaml` → `geometry.altitude_m` | 从 EXIF 读或问师兄 |
| **样方真值** | 校准输入（CSV） | 决定校准点数量 |
| **田块面积** | 田块清单 CSV | 没有则只能做影像级密度 |

---

## ⚠️ 两件必读的事

### 1. 三个自检**还没实际跑过**

本会话 shell 无法启动进程（`exit code 3221225794`），代码只做了人工审查。
**拿到可运行环境后，第一件事就是跑这三个自检** —— 都不需要权重。

### 2. GSD 公式已修正（重要）

原实现用传感器物理尺寸算，结果 5m GSD = 0.068 cm/px，
而报告实测值是 0.096–0.128 cm/px —— **差 1.8 倍，面积差 3 倍**。

已改用**实测反推的水平覆盖系数**（`coverage_factor`）：
```
Mavic 3 Classic: 1.286  →  5/7/9m 得 0.122/0.171/0.219 cm/px ✅
```

**但 `Mavic3M` / `Mavic3T` 的系数目前是暂借 Classic 的**，
两者焦距不同（12.29mm / 4.4mm），**系数必然不同，必须单独确认**。

详见 [验证状态.md](验证状态.md)。