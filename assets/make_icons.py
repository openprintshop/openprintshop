"""Генерирует 5 вариантов иконки + контактный лист для выбора.
Запуск: python assets/make_icons.py"""
from pathlib import Path

import matplotlib
from PIL import Image, ImageDraw, ImageFilter, ImageFont

S = 1024
HERE = Path(__file__).resolve().parent
FONT = str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans-Bold.ttf")


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def dark(c, f):
    return tuple(max(0, int(x * f)) for x in c[:3])


def vgrad(w, h, c1, c2):
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return im


def bg(c1, c2):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = vgrad(S, S, c1, c2).convert("RGBA")
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S - 1, S - 1], radius=215, fill=255)
    img.paste(g, (0, 0), m)
    return img


def soft_shadow(img, poly_bbox, blur=34, alpha=95, dy=26):
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = poly_bbox
    ImageDraw.Draw(sh).ellipse([x0, y1 - 40 + dy, x1, y1 + 60 + dy], fill=(0, 0, 0, alpha))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))


def circle_grad(img, cx, cy, r, c1, c2):
    g = vgrad(2 * r, 2 * r, c1, c2).convert("RGBA")
    m = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(m).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
    img.alpha_composite(Image.new("RGBA", img.size, (0, 0, 0, 0)))
    img.paste(g, (cx - r, cy - r), m)


def cube(d, cx, top_y, w, L, ctop, cleft, cright, layers=6, edge=(96, 48, 12)):
    tt = (cx, top_y); tr = (cx + w, top_y + w * 0.5); tb = (cx, top_y + w); tl = (cx - w, top_y + w * 0.5)
    mll = (tl[0], tl[1] + L); mcb = (tb[0], tb[1] + L); mrb = (tr[0], tr[1] + L)
    d.polygon([tt, tr, tb, tl], fill=ctop)
    d.polygon([tl, tb, mcb, mll], fill=cleft)
    d.polygon([tb, tr, mrb, mcb], fill=cright)
    for i in range(1, layers):
        t = i / layers
        d.line([lerp(tl, mll, t), lerp(tb, mcb, t)], fill=dark(cleft, 0.82), width=5)
        d.line([lerp(tb, mcb, t), lerp(tr, mrb, t)], fill=dark(cright, 0.82), width=5)
    for a, b in [(tt, tr), (tr, tb), (tb, tl), (tl, tt), (tl, mll), (tb, mcb), (tr, mrb), (mll, mcb), (mcb, mrb)]:
        d.line([a, b], fill=edge, width=6)


# ---------- V1: куб + золотая монета (без текста) ----------
def v1():
    img = bg((92, 109, 240), (124, 58, 237))
    d = ImageDraw.Draw(img)
    soft_shadow(img, (250, 150, 700, 560))
    cube(d, 455, 175, 175, 180, (255, 170, 88), (238, 122, 48), (206, 92, 30))
    # монета: тень + ребро + золотой круг
    cc, cr = (760, 770), 165
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([cc[0]-cr, cc[1]-cr+30, cc[0]+cr, cc[1]+cr+40], fill=(0, 0, 0, 90))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(26)))
    d.ellipse([cc[0]-cr, cc[1]-cr+18, cc[0]+cr, cc[1]+cr+18], fill=(176, 132, 28))  # ребро
    circle_grad(img, cc[0], cc[1], cr, (255, 222, 120), (224, 168, 40))
    d = ImageDraw.Draw(img)
    d.ellipse([cc[0]-cr, cc[1]-cr, cc[0]+cr, cc[1]+cr], outline=(255, 240, 190), width=10)
    d.ellipse([cc[0]-cr+40, cc[1]-cr+40, cc[0]+cr-40, cc[1]+cr-40], outline=(206, 150, 36), width=8)
    return img


# ---------- V2: катушка филамента ----------
def v2():
    img = bg((14, 165, 165), (37, 99, 235))
    d = ImageDraw.Draw(img)
    cx, cy, R = 512, 470, 320
    soft_shadow(img, (cx-R, cy-R, cx+R, cy+R))
    # фланец (серый диск)
    circle_grad(img, cx, cy, R, (120, 130, 150), (70, 80, 100))
    d = ImageDraw.Draw(img)
    # намотка филамента
    d.ellipse([cx-R+44, cy-R+44, cx+R-44, cy+R-44], fill=(245, 150, 50))
    for rr in range(R-60, 150, -22):
        d.ellipse([cx-rr, cy-rr, cx+rr, cy+rr], outline=(210, 110, 28), width=3)
    # ступица + отверстие
    d.ellipse([cx-150, cy-150, cx+150, cy+150], fill=(150, 160, 178))
    d.ellipse([cx-150, cy-150, cx+150, cy+150], outline=(90, 100, 120), width=8)
    d.ellipse([cx-60, cy-60, cx+60, cy+60], fill=(60, 70, 90))
    # ребро катушки
    d.ellipse([cx-R, cy-R, cx+R, cy+R], outline=(230, 236, 245), width=12)
    # свободный конец нити
    pts = [(cx+R-50, cy+120), (cx+R+40, cy+210), (cx+R-10, cy+320), (cx+R+90, cy+360)]
    d.line(pts, fill=(245, 150, 50), width=20, joint="curve")
    d.ellipse([pts[-1][0]-18, pts[-1][1]-18, pts[-1][0]+18, pts[-1][1]+18], fill=(245, 150, 50))
    return img


# ---------- V3: сопло, печатающее слои ----------
def v3():
    img = bg((51, 65, 105), (30, 41, 82))
    d = ImageDraw.Draw(img)
    cx = 512
    # нагревательный блок
    d.rounded_rectangle([cx-150, 250, cx+150, 430], radius=28, fill=(150, 158, 172))
    d.rounded_rectangle([cx-150, 250, cx+150, 430], radius=28, outline=(90, 98, 112), width=8)
    for yy in (300, 350):
        d.ellipse([cx-110, yy-16, cx-78, yy+16], fill=(90, 98, 112))
        d.ellipse([cx+78, yy-16, cx+110, yy+16], fill=(90, 98, 112))
    # сопло (трапеция вниз)
    d.polygon([(cx-120, 430), (cx+120, 430), (cx+34, 560), (cx-34, 560)], fill=(196, 202, 214))
    d.polygon([(cx-120, 430), (cx+120, 430), (cx+34, 560), (cx-34, 560)], outline=(110, 118, 132))
    # капля
    d.ellipse([cx-30, 556, cx+30, 616], fill=(255, 150, 40))
    # напечатанные слои (стопка скруглённых баров)
    base_y = 690
    for i, wdt in enumerate([300, 320, 330, 330]):
        yy = base_y + i * 60
        col = (255, 150, 40) if i == 0 else (235, 126, 30)
        d.rounded_rectangle([cx-wdt//2, yy, cx+wdt//2, yy+48], radius=24, fill=col)
    return img


# ---------- V4: чистый «слоёный» куб (без монеты) ----------
def v4():
    img = bg((124, 58, 237), (236, 72, 153))
    d = ImageDraw.Draw(img)
    # башня из слоёв (стопка тонких изо-плит)
    cx, top, w = 512, 250, 230
    dh = 60
    n = 6
    for k in range(n):
        ty = top + k * dh
        shade = 1.0 - k * 0.04
        cube(d, cx, ty, w, dh,
             (int(255*shade), int(255*shade), int(255*shade)),
             (int(225*shade), int(228*shade), int(238*shade)),
             (int(196*shade), int(201*shade), int(220*shade)),
             layers=1, edge=(150, 120, 200))
    return img


# ---------- V5: ценник с кубом ----------
def v5():
    img = bg((16, 185, 129), (5, 122, 110))
    d = ImageDraw.Draw(img)
    # тег: прямоугольник со срезанным верх-левым углом
    tag = [(360, 230), (800, 230), (800, 800), (360, 800), (250, 690), (250, 340)]
    soft_shadow(img, (250, 230, 800, 800))
    d.polygon(tag, fill=(248, 246, 238))
    d.polygon(tag, outline=(180, 178, 168))
    # дырка + кольцо
    d.ellipse([300, 300, 372, 372], fill=(16, 150, 120))
    d.ellipse([300, 300, 372, 372], outline=(120, 120, 110), width=8)
    # куб внутри
    cube(d, 560, 380, 120, 120, (255, 170, 88), (238, 122, 48), (206, 92, 30), layers=4)
    return img


def main():
    variants = [("v1", v1()), ("v2", v2()), ("v3", v3()), ("v4", v4()), ("v5", v5())]
    for name, im in variants:
        im.resize((512, 512), Image.LANCZOS).save(HERE / f"icon_{name}.png")
    # контактный лист
    cell, pad, lab = 300, 36, 46
    cols, rows = 3, 2
    W = cols * cell + (cols + 1) * pad
    H = rows * (cell + lab) + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), (32, 34, 40))
    dd = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(FONT, 34)
    for i, (name, im) in enumerate(variants):
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad)
        y = pad + r * (cell + lab + pad)
        sheet.paste(im.resize((cell, cell), Image.LANCZOS), (x, y))
        dd.text((x + cell // 2, y + cell + 8), name.upper(), font=f, fill=(235, 235, 240), anchor="ma")
    sheet.save(HERE / "icon_variants.png")
    print("saved 5 variants + icon_variants.png")


if __name__ == "__main__":
    main()
