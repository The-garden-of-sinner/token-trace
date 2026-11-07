#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成项目图标（Windows .ico 多尺寸 + Linux .png）。

    python3 -B scripts/make_icon.py

只用 Pillow，不依赖字体或外部 SVG 渲染器。配色取自页面 CSS：
深蓝 #185FA5 打底，米白 #F7F6F2 画轨迹，暖橙 #FFB75C 标出当前调用。
产物同时被 build_win_bat.py（内嵌进单 bat）与 build_deb.sh（装进 hicolor）使用。
"""
import os
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(BASE, "assets")
ICO = os.path.join(ASSETS, "TokenTrace.ico")
PNG = os.path.join(ASSETS, "token-trace.png")

BLUE = (24, 95, 165, 255)
CREAM = (247, 246, 242, 255)
AMBER = (255, 183, 92, 255)
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def rounded_mask(s, radius):
    """Pillow 7 没有 rounded_rectangle，用矩形 + 四角圆拼圆角蒙版。"""
    m = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(m)
    r = int(s * radius)
    d.rectangle((r, 0, s - r, s), fill=255)
    d.rectangle((0, r, s, s - r), fill=255)
    for box in ((0, 0, 2 * r, 2 * r), (s - 2 * r, 0, s, 2 * r),
                (0, s - 2 * r, 2 * r, s), (s - 2 * r, s - 2 * r, s, s)):
        d.ellipse(box, fill=255)
    return m


def render(size):
    s = size * 4                       # 4 倍超采样，缩小后边缘平滑
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, s, s), fill=BLUE)
    im.putalpha(rounded_mask(s, .22))

    # 逐次调用的 token 轨迹：一条折线 + 节点，末节点是当前调用
    pts = [(.14, .63), (.33, .43), (.52, .55), (.70, .29), (.86, .39)]
    pix = [(int(x * s), int(y * s)) for x, y in pts]
    d.line(pix, fill=CREAM, width=max(2, int(s * .072)), joint="curve")
    r = int(s * .052)
    for x, y in pix[:-1]:
        d.ellipse((x - r, y - r, x + r, y + r), fill=CREAM)
    x, y = pix[-1]
    d.ellipse((x - r * 1.15, y - r * 1.15, x + r * 1.15, y + r * 1.15), fill=AMBER)

    # 下方三条短线：进入上下文的文本块
    for i, x1 in enumerate((.43, .35, .51)):
        yy = .755 + i * .068
        h = max(1, int(s * .032))
        d.rectangle((int(.14 * s), int(yy * s), int(x1 * s), int(yy * s) + h),
                    fill=(247, 246, 242, 165))
    return im.resize((size, size), Image.LANCZOS)


def main():
    if not os.path.isdir(ASSETS):
        os.makedirs(ASSETS)
    base = render(256)
    base.save(PNG)
    base.save(ICO, format="ICO", sizes=SIZES)
    print("PNG %s  %d bytes" % (PNG, os.path.getsize(PNG)))
    print("ICO %s  %d bytes  sizes=%s" %
          (ICO, os.path.getsize(ICO), ", ".join(str(w) for w, _ in SIZES)))


if __name__ == "__main__":
    main()
