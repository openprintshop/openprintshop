"""Иконка: сопло печатает игрушку-динозавра. python assets/make_icon_dino.py"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
HERE = Path(__file__).resolve().parent

ORANGE = (255, 150, 46)
ORANGE_D = (210, 110, 24)
ORANGE_DD = (170, 86, 16)
EDGE = (120, 60, 12)


def vgrad(w, h, c1, c2):
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return im


def bg():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = vgrad(S, S, (60, 76, 120), (28, 38, 78)).convert("RGBA")
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S - 1, S - 1], radius=215, fill=255)
    img.paste(g, (0, 0), m)
    return img


def taper(d, p1, w1, p2, w2, fill):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L, dx / L
    d.polygon([(p1[0] + nx * w1, p1[1] + ny * w1), (p2[0] + nx * w2, p2[1] + ny * w2),
               (p2[0] - nx * w2, p2[1] - ny * w2), (p1[0] - nx * w1, p1[1] - ny * w1)], fill=fill)


def dino(d):
    # ноги (задние тёмнее)
    for (x, col) in [(386, ORANGE_DD), (470, ORANGE_DD), (560, ORANGE_D), (632, ORANGE_D)]:
        d.rounded_rectangle([x, 720, x + 64, 856], radius=26, fill=col)
    # хвост
    taper(d, (430, 660), 96, (250, 690), 40, ORANGE)
    taper(d, (270, 685), 44, (150, 628), 10, ORANGE)
    # тело
    d.ellipse([330, 560, 700, 800], fill=ORANGE)
    # шея
    taper(d, (610, 612), 92, (690, 470), 60, ORANGE)
    taper(d, (676, 500), 64, (724, 408), 46, ORANGE)
    # голова
    d.ellipse([676, 350, 812, 470], fill=ORANGE)
    d.ellipse([786, 404, 850, 456], fill=ORANGE)  # морда
    d.ellipse([738, 388, 770, 420], fill=(40, 40, 50))  # глаз
    d.ellipse([748, 396, 762, 410], fill=(245, 245, 250))
    # слои печати на теле
    for yy in range(600, 790, 30):
        x0 = 332 + (abs(yy - 690) * 0.32)
        d.line([(x0, yy), (700 - (x0 - 332), yy)], fill=ORANGE_D, width=5)
    # контур тела (лёгкий)
    d.ellipse([330, 560, 700, 800], outline=EDGE, width=4)


def nozzle(d):
    cx = 470
    d.rounded_rectangle([cx - 96, 150, cx + 96, 268], radius=20, fill=(168, 176, 190))
    d.rounded_rectangle([cx - 96, 150, cx + 96, 268], radius=20, outline=(96, 104, 120), width=7)
    d.ellipse([cx - 70, 196, cx - 44, 222], fill=(96, 104, 120))
    d.ellipse([cx + 44, 196, cx + 70, 222], fill=(96, 104, 120))
    d.polygon([(cx - 78, 268), (cx + 78, 268), (cx + 22, 372), (cx - 22, 372)], fill=(202, 208, 220))
    d.polygon([(cx - 78, 268), (cx + 78, 268), (cx + 22, 372), (cx - 22, 372)], outline=(110, 118, 134))
    # струя на спину динозавра
    d.line([(cx, 372), (cx + 18, 556)], fill=ORANGE, width=15)
    d.ellipse([cx - 16, 360, cx + 16, 392], fill=ORANGE)


def build():
    img = bg()
    d = ImageDraw.Draw(img)
    # мягкая тень под динозавром
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([300, 820, 720, 910], fill=(0, 0, 0, 110))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(30)))
    nozzle(d)
    dino(d)
    return img


def main():
    img = build()
    img.resize((512, 512), Image.LANCZOS).save(HERE / "icon_dino.png")
    img.save(HERE / "icon_dino_1024.png")
    print("saved icon_dino.png")


if __name__ == "__main__":
    main()
