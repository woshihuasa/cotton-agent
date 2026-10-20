# -*- coding: utf-8 -*-
"""几何换算 —— 框数 → 密度（个/m²）。

【为什么必须有这一层】
  原方案的契约用 count_point: 1240（纯个数）。
  但 5 m 高度下 640 切片覆盖约 0.59 m²，9 m 下约 2 m² —— 面积差 3.4 倍。
  同一个「1240」在不同高度下不是同一个量，跨高度/跨批比较全被面积混淆污染。

  所以估计量必须是 density = 框数 / 覆盖面积。

【这一层只做换算】
  输入：框列表 + 相机参数 + 飞行高度
  输出：密度 + 面积分母 + 覆盖率
  不碰检测器、不碰校准。

【可单独验证】
  python geometry.py --selftest
  → 用 YOLO侧技术路线.md 里的数值表对账（5m 棉絮 28px 等）
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass


# ---------- 相机参数 ----------


@dataclass
class Camera:
    """相机参数 —— 用「水平覆盖系数」建模（不是传感器物理尺寸）。

    ★ 为什么不用 sensor_width / focal_length 正推
      "Mavic 3 Classic 是 4/3 CMOS"里的 17.3mm 指的是**对角线**尺寸，
      不是传感器宽度，直接代入会得出 5m GSD ≈ 0.068 cm/px，
      而实测/报告值是 0.096–0.128 cm/px —— 差约 1.8 倍，面积差约 3 倍。

      可靠做法是用**实测反推的水平覆盖系数**：
          地面覆盖宽度 ≈ 高度 × coverage_factor
      这与 [研究方向探索.md] 第 10.2 节的算术一致（h × 1.286），
      且能复现报告里的 GSD 区间。

    Attributes:
        coverage_factor: 水平地面覆盖 / 飞行高度（无量纲）。
            即飞 1 m 高时，水平方向拍到多少米地面。
        image_width_px / image_height_px: 原生影像尺寸。
        note: 系数来源说明（便于追溯）。
    """

    coverage_factor: float
    image_width_px: int
    image_height_px: int
    note: str = ""

    @property
    def gsd_at_1m(self) -> float:
        """1 米高度下的 GSD（米/像素）。"""
        return self.coverage_factor / self.image_width_px

    def gsd(self, altitude_m: float) -> float:
        """指定高度下的 GSD（米/像素）。GSD ∝ 高度。"""
        return self.gsd_at_1m * altitude_m

    def ground_coverage(self, altitude_m: float) -> tuple[float, float]:
        """地面覆盖尺寸（宽, 高），单位米。"""
        g = self.gsd(altitude_m)
        return (g * self.image_width_px, g * self.image_height_px)


# 已知机型参数
# ⚠️ ⚠️ 关键：coverage_factor 必须**逐机型确认**，不能互相照抄（焦距不同，覆盖比差很多）
CAMERAS: dict[str, Camera] = {
    # Mavic 3 Classic：4/3 CMOS，20MP，5280×3956
    # 系数来自 [研究方向探索.md] 第 10.2 节（h×1.286），
    # 复现 5/7/9m → 0.122/0.171/0.219 cm/px，与报告 0.096–0.128 区间一致
    "Mavic3Classic": Camera(
        coverage_factor=1.286, image_width_px=5280, image_height_px=3956,
        note="探索日志10.2节实测反推",
    ),
    # Mavic 3M：多光谱机型，5280×3956
    # ⚠️ 待确认：焦距 12.29mm 与 Classic 不同，系数不能照抄
    "Mavic3M": Camera(
        coverage_factor=1.286, image_width_px=5280, image_height_px=3956,
        note="⚠️暂用Classic系数，必须核实",
    ),
    # Mavic 3T：4000×3000
    # ⚠️ 待确认：焦距 4.4mm，系数与上两者都不同
    "Mavic3T": Camera(
        coverage_factor=1.286, image_width_px=4000, image_height_px=3000,
        note="⚠️暂用Classic系数，必须核实",
    ),
}


def camera_from_gsd(gsd_cm_per_px: float, altitude_m: float,
                    image_width_px: int = 5280,
                    image_height_px: int = 3956) -> Camera:
    """★ 最可靠的方式：已知实测 GSD 时，直接反推覆盖系数。

    如果你手上有师兄报告里的 GSD 记录（如 0.096–0.128 cm/px @ 3–5m），
    用这个函数建相机会比用标称参数准得多。

    例：gsd=0.12cm/px @ 5m, 5280px 宽
        → coverage_factor = 0.0012 × 5280 / 5 = 1.267
    """
    gsd_m = gsd_cm_per_px / 100.0
    factor = gsd_m * image_width_px / altitude_m
    return Camera(coverage_factor=factor,
                  image_width_px=image_width_px,
                  image_height_px=image_height_px,
                  note=f"由实测GSD {gsd_cm_per_px}cm/px @ {altitude_m}m 反推")


# ---------- 密度换算 ----------


@dataclass
class DensityResult:
    """一次密度换算的完整结果。"""

    n_boxes: int
    imaged_area_m2: float          # 实际拍摄覆盖面积（分母）
    density_per_m2: float          # = n_boxes / imaged_area_m2
    gsd_m_per_px: float
    altitude_m: float
    # 可选：田块总面积与覆盖率（有田块面积记录时填）
    field_area_m2: float | None = None
    area_coverage_fraction: float | None = None

    def to_dict(self) -> dict:
        d = {
            "n_boxes": self.n_boxes,
            "imaged_area_m2": round(self.imaged_area_m2, 4),
            "density_per_m2": round(self.density_per_m2, 4),
            "gsd_cm_per_px": round(self.gsd_m_per_px * 100, 4),
            "altitude_m": self.altitude_m,
        }
        if self.field_area_m2 is not None:
            d["field_area_m2"] = round(self.field_area_m2, 2)
            d["area_coverage_fraction"] = round(self.area_coverage_fraction or 0, 4)
        return d


def tile_area_m2(tile_size_px: int, gsd_m_per_px: float) -> float:
    """单个切片的覆盖面积（m²）。

    ★ 注意：这里用 tile_size 而不是重叠后的步长 ——
      因为重叠区域的框会被去重，所以「有效面积」应该是
      去重后每个框代表的唯一地面区域。
      严谨做法见 imaged_area_from_tiles()。
    """
    side_m = tile_size_px * gsd_m_per_px
    return side_m * side_m


def imaged_area_from_tiles(
    tiles: list[dict],
    gsd_m_per_px: float,
    use_valid_only: bool = True,
) -> float:
    """由切片列表算「实际有效拍摄面积」（m²）。

    ★ 关键：重叠区域只算一次。
    做法：把每个有效切片的覆盖矩形投到网格上，取并集。
    这避免了「80 片 × 每片 0.59 m² = 47 m² 但实际只拍了 31 m²」的重复计算。

    use_valid_only=True 时剔除被判定为无效（过曝/模糊）的切片。
    """
    if not tiles:
        return 0.0

    # 收集有效切片的像素矩形
    rects = []
    for t in tiles:
        if use_valid_only and not t.get("valid", True):
            continue
        x0, y0 = int(t["x0"]), int(t["y0"])
        x1, y1 = int(t["x1"]), int(t["y1"])
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1, y1))

    if not rects:
        return 0.0

    # 用扫描线算矩形并集面积（纯像素坐标，不需要 numpy）
    # 按 x 切分事件点，对每个 x 区间求 y 方向的并集长度
    xs = sorted({x0 for x0, _, _, _ in rects} | {x1 for _, _, x1, _ in rects})
    total_px = 0.0
    for i in range(len(xs) - 1):
        xa, xb = xs[i], xs[i + 1]
        if xb <= xa:
            continue
        width = xb - xa
        # 该 x 区间内覆盖到的所有 y 区间
        yspans = [(y0, y1) for (x0, y0, x1, y1) in rects if x0 <= xa and x1 >= xb]
        if not yspans:
            continue
        total_px += width * _union_length(yspans)

    # 像素面积 → 物理面积
    return total_px * (gsd_m_per_px ** 2)


def _union_length(spans: list[tuple[int, int]]) -> float:
    """若干区间的并集总长度。"""
    if not spans:
        return 0.0
    spans = sorted(spans)
    total = 0.0
    cur_a, cur_b = spans[0]
    for a, b in spans[1:]:
        if a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
    total += cur_b - cur_a
    return float(total)


def compute_density(
    n_boxes: int,
    tiles: list[dict],
    camera: Camera,
    altitude_m: float,
    tile_size_px: int = 640,
    field_area_m2: float | None = None,
) -> DensityResult:
    """主换算：框数 + 切片信息 + 相机 + 高度 → 密度。

    参数
    ----
    n_boxes : 去重后的框数
    tiles : 切片列表（含 x0,y0,x1,y1,valid），来自 infer.py 的输出
    camera : 相机参数
    altitude_m : 飞行高度
    tile_size_px : 切片边长
    field_area_m2 : 田块总面积（可选，有则算覆盖率）
    """
    gsd = camera.gsd(altitude_m)
    area = imaged_area_from_tiles(tiles, gsd, use_valid_only=True)
    if area <= 0:
        area = tile_area_m2(tile_size_px, gsd)

    density = n_boxes / area if area > 0 else 0.0

    cov = None
    if field_area_m2 and field_area_m2 > 0:
        cov = min(1.0, area / field_area_m2)

    return DensityResult(
        n_boxes=n_boxes,
        imaged_area_m2=area,
        density_per_m2=density,
        gsd_m_per_px=gsd,
        altitude_m=altitude_m,
        field_area_m2=field_area_m2,
        area_coverage_fraction=cov,
    )


# ---------- 自检 ----------


def selftest() -> int:
    """与 [研究方向探索.md] 的算术表对账。"""
    print("=" * 70)
    print("几何换算 · 自检（对账 YOLO侧技术路线.md 的数值表）")
    print("=" * 70)
    ok = True

    cam = CAMERAS["Mavic3Classic"]

    # --- 0. 与报告 GSD 区间对账（★ 这是最重要的验证）---
    print("\n[0] GSD 与师兄报告对账（报告：3-5m 下 0.096-0.128 cm/px）")
    gsd5_check = cam.gsd(5.0) * 100
    in_range = 0.096 <= gsd5_check <= 0.128
    print(f"    5m → {gsd5_check:.4f} cm/px  报告区间 0.096-0.128  "
          f"{'✓ 落在区间内' if in_range else '✗ 偏离'}")
    ok = ok and in_range

    # --- 1. GSD 表 ---
    print("\n[1] GSD 与棉絮像素数（Mavic 3 Classic，棉絮 4cm）")
    print(f"    {'高度':>6} {'GSD原生':>12} {'覆盖(m)':>16} {'棉絮px':>8} {'裁切640后':>10}")
    expected_gsd = {5: 0.12, 7: 0.17, 9: 0.22}   # cm/px，来自探索日志
    for h in (5, 7, 9):
        gsd_cm = cam.gsd(h) * 100
        w_m, h_m = cam.ground_coverage(h)
        lint_px = 0.04 / cam.gsd(h)      # 4cm 棉絮的原生像素
        lint_tile = lint_px               # 裁切保持原生分辨率
        exp = expected_gsd[h]
        flag = "✓" if abs(gsd_cm - exp) < 0.02 else "✗"
        print(f"    {h:>5}m {gsd_cm:>10.3f}cm/px {w_m:>7.1f}x{h_m:<6.1f} "
              f"{lint_px:>7.1f} {lint_tile:>9.1f} {flag}")
        if abs(gsd_cm - exp) >= 0.02:
            ok = False
    print("    （期望 GSD ≈ 0.12 / 0.17 / 0.22 cm/px，棉絮 28/20/15 px）")

    # --- 2. 切片面积（★ 核心：证明分母随高度变）---
    print("\n[2] 单切片覆盖面积随高度变化（这是必须换算成密度的原因）")
    for h in (5, 7, 9):
        a = tile_area_m2(640, cam.gsd(h))
        print(f"    {h}m 高度：640 切片覆盖 {a:.3f} m²")
    a5 = tile_area_m2(640, cam.gsd(5))
    a9 = tile_area_m2(640, cam.gsd(9))
    ratio = a9 / a5
    print(f"    9m / 5m 面积比 = {ratio:.2f}×")
    if not (2.5 < ratio < 4.5):
        print("    ✗ 比值异常（应约 3.24 = (9/5)²）")
        ok = False
    else:
        print("    ✓ 符合预期（面积按高度平方增长）")

    # --- 3. 切片数（对账 80 片）---
    print("\n[3] 切片数（5280×3956，640 片，15% 重叠）")
    from infer import plan_tiles
    tiles_pos = plan_tiles(5280, 3956, 640, 0.15)
    print(f"    → {len(tiles_pos)} 片（探索日志第 11 节算的是 80 片）")
    if not (70 <= len(tiles_pos) <= 90):
        print("    ✗ 切片数偏离预期")
        ok = False
    else:
        print("    ✓ 与预期一致")

    # --- 4. 重叠面积只算一次（★ 关键正确性检验）---
    print("\n[4] 有效面积计算（重叠只算一次）")
    tiles_dict = [{"x0": x0, "y0": y0, "x1": x1, "y1": y1, "valid": True}
                  for (x0, y0, x1, y1) in tiles_pos]
    gsd5 = cam.gsd(5)
    naive = len(tiles_dict) * tile_area_m2(640, gsd5)
    actual = imaged_area_from_tiles(tiles_dict, gsd5)
    print(f"    朴素算法（片数×单片面积）= {naive:.2f} m²")
    print(f"    去重算法（矩形并集）      = {actual:.2f} m²")
    print(f"    重复计算掉了 {naive - actual:.2f} m²（{(1-actual/naive)*100:.1f}%）")
    # 全图覆盖应约等于 5280×3956 像素的地面面积
    full = 5280 * 3956 * gsd5 * gsd5
    print(f"    全图理论面积              = {full:.2f} m²")
    if abs(actual - full) / full > 0.02:
        print("    ✗ 去重后面积应≈全图面积")
        ok = False
    else:
        print("    ✓ 去重后面积≈全图面积（说明并集算对了）")
        ok = ok and True

    # --- 5. 无效切片剔除 ---
    print("\n[5] 无效切片剔除")
    tiles_dict[0]["valid"] = False
    a_without = imaged_area_from_tiles(tiles_dict, gsd5, use_valid_only=True)
    a_with = imaged_area_from_tiles(tiles_dict, gsd5, use_valid_only=False)
    print(f"    剔 1 片无效：{a_with:.2f} → {a_without:.2f} m²")
    good = a_without < a_with
    print(f"    {'✓' if good else '✗'} 无效切片被正确剔除")
    ok = ok and good

    # --- 6. 密度换算 ---
    print("\n[6] 密度换算（1414 框，全图）")
    r = compute_density(1414, tiles_dict, cam, 5.0, 640, field_area_m2=None)
    print(f"    {r.n_boxes} 框 / {r.imaged_area_m2:.2f} m² = "
          f"{r.density_per_m2:.3f} 个/m²  (GSD {r.gsd_m_per_px*100:.3f} cm/px)")
    good = r.density_per_m2 > 0
    print(f"    {'✓' if good else '✗'} 密度计算正常")
    ok = ok and good

    print("\n" + "=" * 70)
    print(f"自检结果：{'全部通过 ✓' if ok else '有失败项 ✗'}")
    print("=" * 70)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="几何换算（框数 → 密度）")
    ap.add_argument("--selftest", action="store_true", help="自检")
    ap.add_argument("--camera", default="Mavic3Classic",
                    choices=list(CAMERAS), help="机型")
    ap.add_argument("--altitude", type=float, default=5.0, help="飞行高度(m)")
    ap.add_argument("--boxes", type=int, default=0, help="框数（手动试算）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.boxes:
        cam = CAMERAS[args.camera]
        r = compute_density(args.boxes, [], cam, args.altitude)
        print(f"{args.camera} @ {args.altitude}m")
        print(f"  GSD = {r.gsd_m_per_px*100:.3f} cm/px")
        print(f"  单片覆盖 = {tile_area_m2(640, r.gsd_m_per_px):.3f} m²")
        print(f"  {args.boxes} 框 → {r.density_per_m2:.3f} 个/m²")
        return 0

    print("用法：python geometry.py --selftest  或  --boxes N --altitude H")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())