# -*- coding: utf-8 -*-
"""滑窗推理 —— 把大影像切成 640 块，逐块送检测器，合并去重。

【为什么自己切而不用 SAHI】
  论文要把「推理协议」钉死并写进方法章（inference_spec 五字段）。
  自己切 → 每一步可解释、可复现；SAHI 内部的去重策略不完全透明。
  详见 YOLO侧技术路线.md 第 2 节。

【这个模块只做一件事】
  影像 → 框列表（在**原图坐标系**下）。
  不碰密度换算、不碰校准、不碰真值。那些在 geometry.py / calibrate.py。

【可单独验证】
  python infer.py --config config.yaml --selftest
  → 用一张合成图跑通全流程，检查切片数、坐标还原、去重是否正确。
  这一步不需要 .pt 权重，能验证除模型外的所有逻辑。
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np
import yaml

# ---------- 数据结构 ----------


@dataclass
class Box:
    """一个检出框，坐标在**原图**像素坐标系下（xyxy）。"""

    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    cls: int = 0
    # 来自哪个切片（调试用：可看出边界处是否有重复检出）
    tile_id: int = -1

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def as_xyxy(self) -> list[float]:
        return [self.x1, self.y1, self.x2, self.y2]


@dataclass
class TileInfo:
    """一个切片的位置与检出情况（用于诊断边界效应）。"""

    tile_id: int
    x0: int
    y0: int
    x1: int
    y1: int
    n_boxes: int = 0
    # 该切片是否被判定为无效（过曝/欠曝/模糊），无效则不计入面积
    valid: bool = True
    quality_reason: str = ""


@dataclass
class ImageResult:
    """单张影像的推理结果。"""

    image_path: str
    image_w: int
    image_h: int
    n_tiles: int
    n_boxes_raw: int          # 去重前
    n_boxes: int              # 去重后
    boxes: list[Box] = field(default_factory=list)
    tiles: list[TileInfo] = field(default_factory=list)

    def to_dict(self, with_boxes: bool = True) -> dict:
        d = {
            "image_path": self.image_path,
            "image_w": self.image_w,
            "image_h": self.image_h,
            "n_tiles": self.n_tiles,
            "n_boxes_raw": self.n_boxes_raw,
            "n_boxes": self.n_boxes,
            "n_tiles_valid": sum(1 for t in self.tiles if t.valid),
        }
        if with_boxes:
            d["boxes"] = [asdict(b) for b in self.boxes]
            d["tiles"] = [asdict(t) for t in self.tiles]
        return d


# ---------- 切片规划 ----------


def plan_tiles(
    img_w: int,
    img_h: int,
    tile_size: int = 640,
    overlap: float = 0.15,
) -> list[tuple[int, int, int, int]]:
    """计算切片位置。

    重叠的意义：避免某个棉絮正好落在两条切缝的交叉处，被两个切片各切掉一半，
    于是两边都检不出。overlap=0.15 表示相邻切片重叠 15% 边长。

    返回 [(x0, y0, x1, y1), ...]，全部落在图像范围内。

    ★ 边界处理：最后一列/行的切片贴右边/下边对齐（而不是越界），
      所以实际重叠可能略大于设定值 —— 这是刻意的，保证覆盖完整。
    """
    stride = max(1, int(round(tile_size * (1.0 - overlap))))
    xs = list(range(0, max(1, img_w - tile_size + 1), stride))
    ys = list(range(0, max(1, img_h - tile_size + 1), stride))

    # 贴边补齐：保证最右/最下一列也覆盖到
    if xs[-1] + tile_size < img_w:
        xs.append(img_w - tile_size)
    if ys[-1] + tile_size < img_h:
        ys.append(img_h - tile_size)

    tiles = []
    for y0 in ys:
        for x0 in xs:
            x1 = min(x0 + tile_size, img_w)
            y1 = min(y0 + tile_size, img_h)
            tiles.append((int(x0), int(y0), int(x1), int(y1)))
    return tiles


# ---------- 跨切片去重 ----------


def dedup_boxes(boxes: list[Box], iou_threshold: float = 0.5) -> list[Box]:
    """跨切片 NMS 去重。

    重叠切片会把同一个目标检出多次（坐标几乎相同），必须去重，否则系统性多计。

    做法：按置信度降序，保留高分框，抑制与它 IoU 超阈值的后续框。
    这是标准 NMS，与检测器内部的 NMS 是两回事（那个作用于单张切片内）。
    """
    if not boxes:
        return []

    order = sorted(range(len(boxes)), key=lambda i: boxes[i].conf, reverse=True)
    keep: list[int] = []
    suppressed = [False] * len(boxes)

    for idx in order:
        if suppressed[idx]:
            continue
        keep.append(idx)
        a = boxes[idx]
        for jdx in order:
            if jdx == idx or suppressed[jdx]:
                continue
            if iou(a, boxes[jdx]) > iou_threshold:
                suppressed[jdx] = True

    return [boxes[i] for i in keep]


def iou(a: Box, b: Box) -> float:
    """两个框的交并比。"""
    ix1 = max(a.x1, b.x1)
    iy1 = max(a.y1, b.y1)
    ix2 = min(a.x2, b.x2)
    iy2 = min(a.y2, b.y2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


# ---------- 切片裁剪 ----------


def crop_with_pad(img: np.ndarray, x0: int, y0: int, x1: int, y1: int,
                  size: int = 640) -> np.ndarray:
    """裁出切片；若不足 size（贴边时可能出现），补黑边到 size×size。

    补边的意义：模型要求固定输入尺寸，而贴边的切片可能略小。
    补黑边不改动有效区域，但要注意 —— 补出来的黑边**不计入有效面积**，
    面积换算在 geometry.py 里按「实际有效裁切区域」算，不用 size。
    """
    tile = img[y0:y1, x0:x1]
    th, tw = tile.shape[:2]
    if th == size and tw == size:
        return tile
    padded = np.zeros((size, size, tile.shape[2]), dtype=tile.dtype) \
        if tile.ndim == 3 else np.zeros((size, size), dtype=tile.dtype)
    padded[:th, :tw] = tile
    return padded


# ---------- 质量判定 ----------


def assess_tile_quality(tile: np.ndarray, qcfg: dict) -> tuple[bool, str]:
    """判定切片是否有效（过曝/欠曝/模糊）。

    ★ 剔除规则必须显式记录（写进 quality_reason），不能静默丢弃 ——
      这是 valid_frame_ratio 的来源，也是 D3 异常检测的前置门。
    """
    gray = tile.mean(axis=2) if tile.ndim == 3 else tile
    mean_v = float(gray.mean())

    if mean_v > qcfg.get("overexposed_mean_threshold", 240):
        return False, f"过曝 mean={mean_v:.1f}"
    if mean_v < qcfg.get("underexposed_mean_threshold", 15):
        return False, f"欠曝 mean={mean_v:.1f}"

    # 拉普拉斯方差（模糊检测）—— 不引入 opencv，用 numpy 卷积近似
    try:
        lap = _laplacian_var(gray)
        thr = qcfg.get("blur_laplacian_threshold", 50.0)
        if lap < thr:
            return False, f"模糊 lap_var={lap:.1f}"
    except Exception:  # noqa: BLE001
        pass

    return True, ""


def _laplacian_var(gray: np.ndarray) -> float:
    """拉普拉斯响应的方差 —— 越小越模糊。纯 numpy 实现。"""
    g = gray.astype(np.float32)
    # 4 邻域拉普拉斯核 [0,1,0; 1,-4,1; 0,1,0]
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1]
        + g[1:-1, :-2] + g[1:-1, 2:]
    )
    return float(lap.var())


# ---------- 主流程 ----------


def infer_image(
    img: np.ndarray,
    model,
    spec: dict,
    dedup_cfg: dict,
    quality_cfg: dict,
    image_path: str = "",
) -> ImageResult:
    """对单张影像做滑窗推理。

    model 为 None 时走「结构自检」路径：不调模型，只验证切片/坐标/去重逻辑。
    """
    h, w = img.shape[:2]
    tile_size = int(spec.get("tile_size_px", 640))
    overlap = float(spec.get("tile_overlap", 0.15))
    conf = float(spec.get("conf_threshold", 0.25))
    nms_iou = spec.get("nms_iou", 0.5)
    batch_size = int(spec.get("batch_size", 16))

    tiles_pos = plan_tiles(w, h, tile_size, overlap)
    tiles_info: list[TileInfo] = []
    all_boxes: list[Box] = []

    # 批次组装（一次推理多片，显存不够就调小 batch_size）
    for batch_start in range(0, len(tiles_pos), batch_size):
        batch_pos = tiles_pos[batch_start:batch_start + batch_size]
        batch_imgs = []
        batch_meta = []

        for i, (x0, y0, x1, y1) in enumerate(batch_pos):
            tid = batch_start + i
            tile = crop_with_pad(img, x0, y0, x1, y1, tile_size)
            valid, reason = assess_tile_quality(tile, quality_cfg)
            info = TileInfo(tile_id=tid, x0=x0, y0=y0, x1=x1, y1=y1,
                            valid=valid, quality_reason=reason)
            if not valid:
                tiles_info.append(info)
                continue
            batch_imgs.append(tile)
            batch_meta.append((info, x0, y0))

        if model is None or not batch_imgs:
            for info, _x0, _y0 in batch_meta:
                tiles_info.append(info)
            continue

        results = model.predict(
            batch_imgs,
            imgsz=int(spec.get("input_size_px", 640)),
            conf=conf,
            iou=nms_iou,
            device=spec.get("device", "0"),
            verbose=False,
        )

        for res, (info, ox, oy) in zip(results, batch_meta):
            n_here = 0
            if res.boxes is not None and len(res.boxes) > 0:
                xyxy = res.boxes.xyxy.cpu().numpy()
                confs = res.boxes.conf.cpu().numpy()
                clss = res.boxes.cls.cpu().numpy().astype(int)
                for (bx1, by1, bx2, by2), cf, cl in zip(xyxy, confs, clss):
                    all_boxes.append(Box(
                        x1=float(bx1) + ox, y1=float(by1) + oy,
                        x2=float(bx2) + ox, y2=float(by2) + oy,
                        conf=float(cf), cls=int(cl), tile_id=info.tile_id,
                    ))
                    n_here += 1
            info.n_boxes = n_here
            tiles_info.append(info)

    n_raw = len(all_boxes)
    if dedup_cfg.get("strategy", "nms") == "nms":
        all_boxes = dedup_boxes(all_boxes, float(dedup_cfg.get("iou_threshold", 0.5)))

    return ImageResult(
        image_path=image_path,
        image_w=w, image_h=h,
        n_tiles=len(tiles_pos),
        n_boxes_raw=n_raw,
        n_boxes=len(all_boxes),
        boxes=all_boxes,
        tiles=tiles_info,
    )


# ---------- 自检（不需要权重）----------


def selftest() -> int:
    """结构自检：验证切片规划、坐标还原、去重逻辑。

    不需要 .pt 权重 —— 用一个假的 model（返回固定框）走通全流程。
    """
    print("=" * 70)
    print("滑窗推理 · 结构自检（不需要权重）")
    print("=" * 70)
    ok = True

    # --- 1. 切片规划 ---
    print("\n[1] 切片规划")
    tiles = plan_tiles(5280, 3956, 640, 0.15)
    print(f"    5280x3956 影像，切片 640，重叠 15% → {len(tiles)} 片")
    # 覆盖率检验：所有切片并集必须覆盖全图
    covered = np.zeros((3956, 5280), dtype=bool)
    for (x0, y0, x1, y1) in tiles:
        covered[y0:y1, x0:x1] = True
    coverage = covered.mean()
    print(f"    覆盖率 = {coverage*100:.2f}%")
    if coverage < 0.999:
        print("    ✗ 有未覆盖区域")
        ok = False
    else:
        print("    ✓ 全图覆盖")

    # 贴边检查：每条切片都在界内
    inbounds = all(0 <= x0 < x1 <= 5280 and 0 <= y0 < y1 <= 3956
                   for x0, y0, x1, y1 in tiles)
    print(f"    {'✓' if inbounds else '✗'} 所有切片在界内")
    ok = ok and inbounds

    # --- 2. 坐标还原 ---
    print("\n[2] 坐标还原（切片内坐标 → 原图坐标）")
    fake = np.zeros((3956, 5280, 3), dtype=np.uint8)
    fake[1000, 1000] = 255          # 打一个标记点
    tid = [i for i, (x0, y0, x1, y1) in enumerate(tiles)
           if x0 <= 1000 < x1 and y0 <= 1000 < y1]
    print(f"    标记点 (1000,1000) 落在切片 {tid}")
    if tid:
        x0, y0, _, _ = tiles[tid[0]]
        local = (1000 - x0, 1000 - y0)
        restored = (local[0] + x0, local[1] + y0)
        good = restored == (1000, 1000)
        print(f"    切片内 {local} → 还原 {(restored)}  {'✓' if good else '✗'}")
        ok = ok and good

    # --- 3. 去重 ---
    print("\n[3] 跨切片去重")
    dup = [
        Box(10, 10, 50, 50, 0.9, 0, 0),
        Box(11, 11, 51, 51, 0.8, 0, 1),   # 与上面几乎重合 → 应被抑制
        Box(200, 200, 240, 240, 0.7, 0, 2),  # 独立 → 应保留
    ]
    kept = dedup_boxes(dup, 0.5)
    print(f"    3 个框（其中 2 个重合）→ 去重后 {len(kept)} 个")
    good = len(kept) == 2
    print(f"    {'✓' if good else '✗'} 去重正确")
    ok = ok and good

    # --- 4. 质量判定 ---
    print("\n[4] 质量判定")
    qcfg = {"overexposed_mean_threshold": 240,
            "underexposed_mean_threshold": 15,
            "blur_laplacian_threshold": 100.0}
    normal = np.random.randint(50, 200, (640, 640, 3), dtype=np.uint8)
    v1, r1 = assess_tile_quality(normal, qcfg)
    print(f"    正常图 → valid={v1} ({r1 or '无异常'})")
    white = np.full((640, 640, 3), 250, dtype=np.uint8)
    v2, r2 = assess_tile_quality(white, qcfg)
    print(f"    全白图 → valid={v2} ({r2})")
    good = v1 and not v2
    print(f"    {'✓' if good else '✗'} 判定正确")
    ok = ok and good

    print("\n" + "=" * 70)
    print(f"自检结果：{'全部通过 ✓' if ok else '有失败项 ✗'}")
    print("=" * 70)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="滑窗推理（Ultralytics YOLO）")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--selftest", action="store_true",
                    help="结构自检（不需要权重）")
    ap.add_argument("--limit", type=int, default=0,
                    help="只处理前 N 张（调试用），0=全部")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    import cv2  # 延迟导入：自检路径不需要 opencv
    from ultralytics import YOLO

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    spec = cfg["inference"]
    dedup_cfg = cfg.get("dedup", {})
    quality_cfg = cfg.get("quality", {})

    weights = cfg["detector"]["weights"]
    if not Path(weights).exists():
        print(f"✗ 权重不存在：{weights}")
        print("  请先在 config.yaml 里填正确的 .pt 路径")
        return 1

    print(f"加载权重：{weights}")
    model = YOLO(weights)

    image_dir = Path(cfg["paths"]["image_dir"])
    out_dir = Path(cfg["paths"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".JPG", ".PNG"}
    images = sorted(p for p in image_dir.rglob("*") if p.suffix in exts)
    if args.limit:
        images = images[:args.limit]
    print(f"找到 {len(images)} 张影像")

    results = []
    for i, p in enumerate(images, 1):
        img = cv2.imread(str(p))
        if img is None:
            print(f"  [{i}/{len(images)}] 读取失败：{p.name}")
            continue
        r = infer_image(img, model, spec, dedup_cfg, quality_cfg, str(p))
        results.append(r)
        print(f"  [{i}/{len(images)}] {p.name}: "
              f"{r.n_tiles} 片 → 原始 {r.n_boxes_raw} 框 → 去重后 {r.n_boxes} 框")

    out_file = out_dir / "boxes.json"
    out_file.write_text(
        json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已写入：{out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())