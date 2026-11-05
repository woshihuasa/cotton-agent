# -*- coding: utf-8 -*-
"""从线稿生成应用图标（.ico 与 .png）。

源图是 `docs/棉花图标.jfif`：2752×1536 的 JPEG，白底、深灰线稿。直接拿来当图标有三个问题：

  ① **白底**：任务栏通常是深色，贴一块白斑很难看；托盘图标尤其明显。
  ② **留白太多**：主体只占中间一小块，缩到 16 像素时线条细得看不见。
  ③ **JPEG 没有透明通道**：没法只保留线条。

所以这里做三步：把线条从白底里"抠"成 alpha、裁到主体、再合成到深色圆角底上反白。
小尺寸（16/24）单独按该尺寸重绘，不是把大图缩下去 —— 缩放会让 1 像素的线断成虚线。

生成物（都要进 git，打包要用）：
  ui/icons/app.ico    多尺寸 Windows 图标（16 到 256）
  ui/icons/app.png    256 像素方形，给窗口与代码引用
  ui/icons/boll_source.png  裁好留白的线稿（未来重生成用，不依赖 docs/）

用法：python tools/make_app_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "棉花图标.jfif"
OUT = ROOT / "ui" / "icons"

#: 图标尺寸。Windows 会按 DPI 自己挑，所以每一档都要有。
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

BG = (42, 47, 54, 255)           # 深灰，比任务栏底色略亮，避免"陷进去"
BORDER = (255, 255, 255, 40)    # 一圈细描边，让它在纯黑与纯白桌面上都能分离
INK = (255, 255, 255, 255)      # 反白后的线条
#: 线条占画布的比例。留出四周空气，圆角底才不会显得挤。
GLYPH_RATIO = 0.72
#: 小于等于这个尺寸改用**简化图形**（三团棉铃 + 果柄）。线稿缩到这个尺寸
#: 必然糊成灰雾；实心剪影试过，是一团认不出的白斑。三个圆团在 16 像素仍然
#: 能读出"棉铃"，和托盘图标用的是同一套几何。
SOLID_UPTO = 24


def _ink_mask(path: Path) -> Image.Image:
    """把白底线稿变成"墨迹遮罩"：线条处 255，白底处 0。"""
    im = Image.open(path).convert("L")
    # JPEG 的白不是 255，边缘还有压缩噪点，先轻微模糊再取反
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    mask = im.point(lambda v: max(0, min(255, int((235 - v) * 6))))
    return mask


def _crop_to_ink(mask: Image.Image, pad_ratio: float = 0.06) -> Image.Image:
    """裁到墨迹边界，四周留一点空。"""
    box = mask.getbbox()
    if box is None:
        raise SystemExit("源图里找不到线条（整张是白的？）")
    left, top, right, bottom = box
    side = max(right - left, bottom - top)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    pad = side * pad_ratio
    half = side / 2 + pad
    return mask.crop((int(cx - half), int(cy - half),
                      int(cx + half), int(cy + half)))


def _glyph_layer(mask: Image.Image, size: int) -> Image.Image:
    """把遮罩缩放到目标尺寸的一层白色线条（透明底）。"""
    side = max(8, int(size * GLYPH_RATIO))
    small = mask.resize((side, side), Image.LANCZOS)
    # 小尺寸再收一次阈值：Lanczos 会把细线糊成灰雾，这里让它要么是线要么是空
    if size <= 24:
        small = small.point(lambda v: 255 if v > 60 else 0)
    layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    layer.paste(INK, (0, 0), small)
    return layer


def _simple_glyph(size: int) -> Image.Image:
    """简化棉铃：三团白色圆 + 一根果柄。给 16/20/24 这类极小尺寸用。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx = cy = size / 2.0
    r = size * 0.135
    for x, y in ((cx, cy - size * 0.155),
                 (cx - size * 0.185, cy + size * 0.085),
                 (cx + size * 0.185, cy + size * 0.085)):
        d.ellipse((x - r, y - r, x + r, y + r), fill=INK)
    w = max(1, int(size * 0.05))
    d.rectangle((cx - w / 2, cy + size * 0.24, cx + w / 2, cy + size * 0.36),
                fill=(200, 205, 212, 255))
    return layer


def _solid(mask: Image.Image) -> Image.Image:
    """把线稿变成实心剪影：从四角泛洪标出外部，剩下的空白就是内部。"""
    binary = mask.point(lambda v: 255 if v > 110 else 0)
    w, h = binary.size
    rgb = Image.new("L", (w, h), 0)
    rgb.paste(binary, (0, 0))
    px = rgb.load()
    # 从一圈边缘往里泛洪，把"外面"标成 128
    from collections import deque
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if px[x, y] == 0:
                px[x, y] = 128
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if px[x, y] == 0:
                px[x, y] = 128
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and px[nx, ny] == 0:
                px[nx, ny] = 128
                q.append((nx, ny))
    # 128 → 透明，0（内部）与 255（线条）→ 实心
    return rgb.point(lambda v: 0 if v == 128 else 255)


def _write_ico(frames: list[Image.Image], path: Path) -> None:
    """自己拼 ICO 容器，每个尺寸放自己那份 PNG。

    ★ 不能用 `Image.save(..., sizes=[...])`：Pillow 会把**同一张图**缩放到各个
      尺寸，我们按尺寸重绘的小图标（简化图形、剪影）会被整体丢掉 —— 表现就是
      "16 像素看起来像大图糊掉"，而且很难看出是保存环节的问题。
    """
    import io
    import struct

    blobs = []
    for im in frames:
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries = b""
    for im, blob in zip(frames, blobs):
        w = 0 if im.width >= 256 else im.width
        h = 0 if im.height >= 256 else im.height
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(header + entries + b"".join(blobs))


def _tile(size: int) -> Image.Image:
    """深色圆角底，线条居中。"""
    # 先在 4 倍尺寸上画，再降采样 —— 这样圆角与线条边缘才平滑
    scale = 4
    big = size * scale
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.22),
                        fill=BG)
    # 描边画在内侧，避免被裁掉一半
    d.rounded_rectangle((1, 1, big - 2, big - 2), radius=int(big * 0.21),
                        outline=BORDER, width=max(1, int(big * 0.012)))

    if size <= SOLID_UPTO:
        glyph = _simple_glyph(big)
    else:
        glyph = _glyph_layer(mask_main, size * scale)
    off = (big - glyph.width) // 2
    tile.alpha_composite(glyph, (off, off))
    return tile.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    if not SRC.is_file():
        raise SystemExit(f"找不到源图：{SRC}")
    OUT.mkdir(parents=True, exist_ok=True)

    mask_main = _crop_to_ink(_ink_mask(SRC))
    mask_solid = _solid(mask_main)
    # 留一份裁好的线稿，供以后重新生成（不依赖被 gitignore 的 docs/）
    src_png = Image.new("RGBA", mask_main.size, (0, 0, 0, 0))
    src_png.paste(INK, (0, 0), mask_main)
    src_png.thumbnail((512, 512), Image.LANCZOS)
    src_png.save(OUT / "boll_source.png", "PNG", optimize=True)

    frames = [_tile(s) for s in SIZES]
    biggest = frames[-1]
    biggest.save(OUT / "app.png", "PNG", optimize=True)
    _write_ico(frames, OUT / "app.ico")
    print(f"生成 {OUT / 'app.ico'}（{len(SIZES)} 个尺寸）与 app.png（256）")
    print(f"裁好的线稿 {OUT / 'boll_source.png'}：{src_png.size}")