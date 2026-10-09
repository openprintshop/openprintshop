"""Генератор иконки приложения. Запуск: python assets/make_icon.py
Создаёт assets/icon.png (512) и assets/icon_1024.png."""
from pathlib import Path

import matplotlib
from PIL import Image, ImageDraw, ImageFont

S = 1024
HERE = Path(__file__).resolve().parent


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def build() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # --- градиентный фон (индиго -> фиолетовый) со скруглением ---
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bot = (91, 108, 240), (124, 58, 237)
    for y in range(S):
        t = y / (S - 1)
        c = (int(top[0] + (bot[0] - top[0]) * t),
             int(top[1] + (bot[1] - top[1]) * t),
             int(top[2] + (bot[2] - top[2]) * t), 255)
        gd.line([(0, y), (S, y)], fill=c)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=210, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)

    # --- изометрический куб (3D-печать) ---
    top_top = (512, 175)
    top_right = (762, 300)
    top_bottom = (512, 425)
    top_left = (262, 300)
    L = 188
    mll = (top_left[0], top_left[1] + L)
    mcb = (top_bottom[0], top_bottom[1] + L)
    mrb = (top_right[0], top_right[1] + L)

    d.polygon([top_top, top_right, top_bottom, top_left], fill=(255, 167, 86))   # верх
    d.polygon([top_left, top_bottom, mcb, mll], fill=(240, 122, 48))             # лево
    d.polygon([top_bottom, top_right, mrb, mcb], fill=(208, 94, 30))            # право

    # слои печати на боковых гранях
    n = 7
    for i in range(1, n):
        t = i / n
        d.line([lerp(top_left, mll, t), lerp(top_bottom, mcb, t)], fill=(212, 104, 38), width=4)
        d.line([lerp(top_bottom, mcb, t), lerp(top_right, mrb, t)], fill=(184, 78, 22), width=4)
    # рёбра
    for a, b in [(top_top, top_right), (top_right, top_bottom), (top_bottom, top_left),
                 (top_left, top_top), (top_left, mll), (top_bottom, mcb), (top_right, mrb),
                 (mll, mcb), (mcb, mrb)]:
        d.line([a, b], fill=(120, 60, 18), width=5)

    # --- монета «Br» (стоимость) ---
    cc, cr = (812, 812), 150
    d.ellipse([cc[0] - cr, cc[1] - cr, cc[0] + cr, cc[1] + cr],
              fill=(250, 250, 252), outline=(91, 108, 240), width=12)
    fpath = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(str(fpath), 150)
    d.text(cc, "Br", font=font, fill=(91, 108, 240), anchor="mm")

    return img


def main():
    img = build()
    big = HERE / "icon_1024.png"
    small = HERE / "icon.png"
    img.save(big)
    img.resize((512, 512), Image.LANCZOS).save(small)
    print("saved:", small)
    print("saved:", big)


if __name__ == "__main__":
    main()
