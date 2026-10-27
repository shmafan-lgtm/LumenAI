# -*- coding: utf-8 -*-
"""从用户提供的星球图生成 Lumen 全套图标资源。

产物：
  web/assets/lumen.ico          多尺寸 ICO（PyInstaller exe 图标）
  web/assets/icon-192.png       PWA / Android 图标
  web/assets/icon-512.png       PWA / Android 图标
  web/assets/icon-maskable.png  Android adaptive icon 前景
  web/assets/favicon.ico        浏览器标签页
  web/assets/logo.png           界面用方形 logo
  web/assets/logo-round.png     圆形裁切 logo
  web/assets/icon.jpg           背景图（Android adaptive icon 背景）

纯离线运行，不依赖第三方服务。
"""
import os
from PIL import Image, ImageDraw, ImageFilter

# 品牌源图（青碧星球 + 轨道环）。放在仓库内的 tools/assets/ 下，
# 这样脚本在任何机器上都能跑，不依赖某个开发环境的本地路径。
# 想换图：覆盖 tools/assets/brand-source.png，或设环境变量 LUMEN_BRAND_SRC。
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("LUMEN_BRAND_SRC") or os.path.join(HERE, "assets", "brand-source.png")
OUT = os.path.join(HERE, "..", "web", "assets")
OUT = os.path.abspath(OUT)

# 与品牌一致的青碧色
TEAL = (94, 214, 226)
DEEP = (10, 32, 40)


def load() -> Image.Image:
    im = Image.open(SRC).convert("RGBA")
    # 源图是 1080x1080 黑底，先把纯黑转成透明，保留星球和环
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            # 黑色背景按亮度做 alpha 衰减，避免边缘一圈硬黑
            lum = max(r, g, b)
            if lum < 26:
                px[x, y] = (r, g, b, 0)
            elif lum < 70:
                fade = (lum - 26) / 44.0
                px[x, y] = (r, g, b, int(a * fade * 0.9))
    return im


def fit_square(im: Image.Image, size: int, pad_ratio: float = 0.0,
               bg=None) -> Image.Image:
    """把图等比缩放到 size，并可选加内边距与纯色底。"""
    inner = int(size * (1 - pad_ratio))
    scale = inner / max(im.size)
    nw, nh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
    r = im.resize((nw, nh), Image.LANCZOS)
    if bg is None:
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    else:
        out = Image.new("RGBA", (size, size), bg)
    out.paste(r, ((size - nw) // 2, (size - nh) // 2), r)
    return out


def circle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size * 4, size * 4), 0)
    d = ImageDraw.Draw(m)
    d.ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def small_size_variant(im: Image.Image, size: int) -> Image.Image:
    """小尺寸专用：裁掉上下环带、放大星球，避免缩成一团糊影。

    原图是横向扁椭圆，缩到 16px 时环会糊成一条线。这里只取中央的
    「星球 + 内环」区域并放大，16/24px 下依然能看出是颗带环的星球。
    """
    w, h = im.size
    # 中央 46% 高度区域（星球 + 最内侧环）
    top = int(h * 0.27)
    bot = int(h * 0.73)
    crop = im.crop((0, top, w, bot))
    return fit_square(crop, size)


def main():
    if not os.path.isfile(SRC):
        raise SystemExit(f"找不到品牌源图：{SRC}\n"
                         f"请把源图放到 tools/assets/brand-source.png，"
                         f"或设环境变量 LUMEN_BRAND_SRC 指向它。")
    os.makedirs(OUT, exist_ok=True)
    src = load()

    # --- 主 logo：透明底，原比例 ---
    logo = fit_square(src, 512)
    logo.save(os.path.join(OUT, "logo.png"))

    # --- 圆形 logo（Android/iOS 风格）---
    rnd = fit_square(src, 512)
    mask = circle_mask(512)
    rnd.putalpha(Image.composite(rnd.getchannel("A"), Image.new("L", (512, 512), 0), mask))
    rnd.save(os.path.join(OUT, "logo-round.png"))

    # --- 深底圆版：浅色任务栏/桌面下轮廓更清楚 ---
    dark = Image.new("RGBA", (512, 512), DEEP + (255,))
    d = ImageDraw.Draw(dark)
    for i in range(200):
        v = int(120 * (1 - i / 200.0) ** 1.8)
        d.ellipse((i, i, 511 - i, 511 - i),
                  outline=(DEEP[0] + v // 3, DEEP[1] + v, DEEP[2] + v, 255),
                  width=2)
    inner = fit_square(src, 420)
    dark.paste(inner, (46, 46), inner)
    dm = circle_mask(512)
    dark.putalpha(Image.composite(dark.getchannel("A"), Image.new("L", (512, 512), 0), dm))
    dark.save(os.path.join(OUT, "logo-dark.png"))

    # --- PWA / Android 常规图标 ---
    for s in (192, 512):
        fit_square(src, s).save(os.path.join(OUT, f"icon-{s}.png"))

    # --- Android adaptive icon ---
    # 前景必须留出 66% 安全区，否则系统裁切成圆形时主体会被切掉
    fit_square(src, 512, pad_ratio=0.34).save(os.path.join(OUT, "icon-maskable.png"))
    # 背景：深色径向渐变
    bg = Image.new("RGBA", (512, 512))
    d = ImageDraw.Draw(bg)
    for i in range(256):
        v = int(255 * (1 - i / 255.0) ** 1.6)
        c = (int(6 + v * 0.05), int(16 + v * 0.16), int(22 + v * 0.20), 255)
        d.ellipse((i * 2 - 512, i * 2 - 512, 512 - i * 2, 512 - i * 2), fill=c)
    bg = bg.filter(ImageFilter.GaussianBlur(28)).convert("RGB")
    bg.save(os.path.join(OUT, "icon-bg.jpg"), quality=92)

    # --- 多尺寸 ICO（PyInstaller / 资源管理器 / 任务栏）---
    # 16/24 用裁切变体，32 以上用原图，兼顾小尺寸辨识度与完整构图
    ico_sizes = (16, 24, 32, 48, 64, 128, 256)
    ico_imgs = []
    for s in ico_sizes:
        ico_imgs.append(small_size_variant(src, s) if s <= 24 else fit_square(src, s))
    ico_imgs[-1].save(os.path.join(OUT, "lumen.ico"),
                      format="ICO", sizes=[(s, s) for s in ico_sizes])

    # --- favicon（16/32 为主，浏览器会自己挑）---
    fav = Image.open(os.path.join(OUT, "lumen.ico"))
    fav.save(os.path.join(OUT, "favicon.ico"),
             format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])

    print("生成完成 ->", OUT)
    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        if f.endswith((".png", ".ico", ".jpg")):
            print(f"  {f:22s} {os.path.getsize(p)/1024:8.1f} KB")


if __name__ == "__main__":
    main()
