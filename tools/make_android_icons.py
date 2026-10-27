# -*- coding: utf-8 -*-
"""从 Lumen 品牌图生成 Android 各密度图标。

产物：
  app/src/main/res/mipmap-<dpi>/ic_launcher.png          常规方形图标
  app/src/main/res/mipmap-<dpi>/ic_launcher_round.png     圆形图标
  app/src/main/res/mipmap-<dpi>/ic_launcher_foreground.png  adaptive 前景（留 66% 安全区）

复用 tools/make_icons.py 的加载与裁切逻辑，保证和桌面端图标是同一张图。
"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from make_icons import load, fit_square, circle_mask, small_size_variant  # noqa: E402

RES = os.path.join(ROOT, "app", "app", "src", "main", "res")

# Android 各密度启动图标标准尺寸
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

# adaptive 前景：108dp 画布，主体限制在中央 66%
FOREGROUND = {
    "mdpi": 108,
    "hdpi": 162,
    "xhdpi": 216,
    "xxhdpi": 324,
    "xxxhdpi": 432,
}


def main():
    src = load()
    for dpi, size in DENSITIES.items():
        out = os.path.join(RES, "mipmap-" + dpi)
        os.makedirs(out, exist_ok=True)

        # 常规图标：小尺寸用裁切变体，保证 48px 下仍能看出是带环星球
        ic = small_size_variant(src, size) if size <= 48 else fit_square(src, size)
        ic.save(os.path.join(out, "ic_launcher.png"))

        # 圆形图标
        rnd = fit_square(src, size)
        mask = circle_mask(size)
        rnd.putalpha(Image.composite(rnd.getchannel("A"), Image.new("L", (size, size), 0), mask))
        rnd.save(os.path.join(out, "ic_launcher_round.png"))

        # adaptive 前景：108dp 画布，可见区是中央 72dp。
        # 原图是横向扁椭圆，铺满会把两侧轨道环切掉 —— 缩到 60% 让主体落在安全区内。
        fg_size = FOREGROUND[dpi]
        fg = fit_square(src, fg_size, pad_ratio=0.40)
        fg.save(os.path.join(out, "ic_launcher_foreground.png"))

        print(f"  mipmap-{dpi:8s} {size}px / fg {fg_size}px")

    print("Android 图标生成完成 ->", RES)


if __name__ == "__main__":
    main()
