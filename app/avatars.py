"""Локальный генератор аватарок профиля (OpenTodo fca4246e).

Две части:
1. «Весёлый промт» — короткая смешная RU-фраза. Если запущен локальный Ollama (модели уже стоят
   в системе) — берём у него; иначе собираем из встроенного словаря (мгновенно, без сети).
2. Картинка в стилистике приложения. По умолчанию — процедурный рендер на Pillow (фирменные
   градиенты + «слои» 3D-печати + дружелюбный персонаж, сид из промта). Если задан AVATAR_AI_URL
   (локальный A1111-совместимый diffusion-сервер на GPU) — пробуем сгенерировать картинку им,
   с откатом на процедурный рендер при любой ошибке.

Всё синхронное (PIL + urllib) — вызывать через asyncio.to_thread, чтобы не блокировать event loop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import urllib.request

from PIL import Image, ImageDraw, ImageFilter

from . import avatar_words as _words
from . import config

log = logging.getLogger(__name__)

# ───────── фирменная палитра (из app.css / assets/make_icons) ─────────
_GRADIENTS = [
    ((255, 122, 26), (191, 70, 10)),     # оранжевый «филамент» (accent)
    ((45, 212, 126), (13, 122, 96)),     # зелёный «деньги» (price)
    ((169, 139, 255), (108, 58, 200)),   # фиолетовый
    ((58, 134, 255), (30, 80, 200)),     # синий
    ((255, 91, 77), (190, 50, 50)),      # коралл
    ((14, 165, 233), (30, 90, 190)),     # циан→синий
]
_BODY = [(243, 239, 230), (189, 240, 212), (255, 217, 179), (191, 224, 255), (224, 212, 255)]
_ACCENT = (255, 122, 26)

# ───────── «весёлые промты» ─────────
# Большой словарь вынесен в avatar_words (OpenTodo 43b7e79e): ~125×165×95 ≈ 1.9 млн сочетаний.
def _wordbank_prompt(rng: random.Random, avoid: str = "") -> str:
    """Случайный промт из большого словаря. avoid — предыдущий промт пользователя:
    не повторяем то же СУЩЕСТВО два раза подряд (анти-«опять робот-тостер»)."""
    for _ in range(6):
        creature = rng.choice(_words.CREATURE)
        if not (avoid and creature in avoid):
            break
    extra = rng.choice(_words.EXTRA)
    base = f"{rng.choice(_words.ADJ)} {creature}"
    return (base + (" " + extra if extra else "")).strip()


def _ollama_prompt() -> str | None:
    """Короткая смешная RU-фраза от локального Ollama. None, если он недоступен/выключен."""
    if not config.OLLAMA_URL or not config.AVATAR_PROMPT_MODEL:
        return None
    body = json.dumps({
        "model": config.AVATAR_PROMPT_MODEL,
        "prompt": ("Придумай ОДНО короткое смешное описание для аватарки, на русском, 3–6 слов, "
                   "в духе «бешеный кальмар в шапочке из фольги». Ответь ТОЛЬКО самим описанием, "
                   "без кавычек, без пояснений, без префиксов."),
        "stream": False,
        "options": {"temperature": 1.0, "num_predict": 40},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(config.OLLAMA_URL.rstrip("/") + "/api/generate",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        text = (data.get("response") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()   # на случай reasoning-моделей
        text = text.splitlines()[0].strip().strip('"«».').strip() if text else ""
        text = re.sub(r"\s+", " ", text)
        if 3 <= len(text) <= 60 and not text.lower().startswith(("вот", "конечно", "описание")):
            return text
    except Exception:  # noqa: BLE001
        log.debug("ollama prompt failed", exc_info=True)
    return None


def funny_prompt(seed: str | None = None, *, skip_ollama: bool = False, avoid: str = "") -> str:
    """Смешной RU-промт для аватарки: Ollama, иначе словарь-фолбэк.

    skip_ollama — когда картинку рисует GPU-SDXL (OpenTodo 0afd7b48): qwen-7B у Ollama сам
    живёт на той же видеокарте и его вызов добавлял 1–2 с и конкуренцию за VRAM; словарь
    мгновенный и не хуже. avoid — предыдущий промт (не повторяем существо подряд)."""
    if skip_ollama:
        return _wordbank_prompt(random.Random(seed), avoid)
    return _ollama_prompt() or _wordbank_prompt(random.Random(seed), avoid)


# ───────── процедурный рендер в стиле приложения ─────────
def _vgrad(w: int, h: int, c1, c2) -> Image.Image:
    base = Image.new("RGB", (w, h), c1)
    top = Image.new("RGB", (w, h), c2)
    mask = Image.new("L", (w, h))
    mask.putdata([int(255 * (y / max(1, h - 1))) for y in range(h) for _ in range(w)])
    base.paste(top, (0, 0), mask)
    return base


# ── СЛОВО промта → форма персонажа (OpenTodo b288a948: процедурный рисунок читает промт, ──
# а не только seed — «кальмар» и «медвежонок» больше не один и тот же безликий прямоугольник).
# Ключ — подстрока в нижнем регистре (совпадение по началу слова покрывает падежи РУ).
_KW_EARS_POINTED = ("кот", "кош", "кіт", "лис", "волч", "волк", "тигр", "рысь", "манул", "гепард",
                    "оцелот", "барс", "пантер", "шакал", "динго", "фенек", "щен", "пёс", "пес",
                    "собак", "шиб", "корги", "хаск", "овчар", "гиен")
_KW_EARS_ROUND = ("медвед", "мишк", "миш", "панд", "коал", "капибар", "мыш", "крыс", "хомяк",
                  "хомяч", "бобёр", "бобр", "сурок", "суслик", "обезьян", "тануки", "енот",
                  "барсук", "вомбат", "опоссум", "ленивец", "выдр", "морск", "белк", "шиншилл")
_KW_EARS_LONG = ("кролик", "зайч", "заяц", "осёл", "осл", "лама", "альпак", "кенгуру")
_KW_BEAK = ("пингвин", "филин", "сов", "утк", "утён", "ворон", "воробь", "попуга", "цыпл", "кур",
            "петух", "тукан", "фламинго", "пелика", "страус", "дятел", "чайк", "гус", "птиц", "птен")
_KW_ROBOT = ("робот", "дрон", "андроид", "тостер", "кибер", "механ", "бот")
_KW_AQUATIC = ("кальмар", "осьминог", "рыб", "акул", "кит", "дельфин", "краб", "аксолотл", "медуз",
               "креветк", "омар", "тюлен", "морж", "нарвал", "скат", "лягуш", "жаб", "черепах")
_KW_HAT = ("шапоч", "шляп", "цилиндр", "корон", "берет", "каск", "треуголк", "колпак", "панам",
           "кепк", "ушанк", "сомбреро", "фес", "тюрбан", "венок", "шлем")
_KW_GLASSES = ("очк", "монокль")
_KW_HEADPHONES = ("наушник",)
_KW_FOIL = ("фольг",)
_HAT_COLORS = [(232, 66, 74), (58, 134, 255), (45, 190, 120), (170, 96, 240), (255, 150, 40)]


def _has(p: str, keys) -> bool:
    # Совпадение по НАЧАЛУ слова, а не подстрокой: русский склоняется в хвосте, поэтому префикс
    # надёжен и не ловит ложные («очк» внутри «шапОЧКе», «утк» внутри «шУТКа»).
    words = re.findall(r"[а-яёa-z]+", p)
    return any(w.startswith(k) for w in words for k in keys)


def _char_features(prompt: str) -> dict:
    """Черты персонажа из промта: тип ушей, клюв, шляпа, очки, наушники. Детерминированно
    (только по тексту), поэтому не ломает «тот же промт → тот же рисунок»."""
    p = (prompt or "").lower()
    if _has(p, _KW_ROBOT):
        ears = "antenna"
    elif _has(p, _KW_EARS_LONG):
        ears = "long"
    elif _has(p, _KW_EARS_POINTED):
        ears = "pointed"
    elif _has(p, _KW_EARS_ROUND):
        ears = "round"
    elif _has(p, _KW_BEAK) or _has(p, _KW_AQUATIC):
        ears = None                      # птицам/морским ушки не рисуем
    else:
        ears = "round"                   # дефолт — милые круглые ушки, не безликий блоб
    hat = "foil" if _has(p, _KW_FOIL) else ("hat" if _has(p, _KW_HAT) else None)
    return {"ears": None if hat else ears,  # головной убор закрывает уши
            "hat": hat, "beak": _has(p, _KW_BEAK), "aquatic": _has(p, _KW_AQUATIC),
            "glasses": _has(p, _KW_GLASSES), "headphones": _has(p, _KW_HEADPHONES)}


def _radial_glow(s: int, cx: int, cy: int, r: int, color) -> Image.Image:
    """Мягкое световое пятно за персонажем — объём вместо плоского градиента."""
    g = Image.new("L", (s, s), 0)
    ImageDraw.Draw(g).ellipse([cx - r, cy - r, cx + r, cy + r], fill=120)
    g = g.filter(ImageFilter.GaussianBlur(r // 2))
    layer = Image.new("RGB", (s, s), color)
    return Image.composite(layer, Image.new("RGB", (s, s), (0, 0, 0)), g), g


def _draw_ears(d, kind, cx, top_y, bw, body, rng, ss):
    """Уши/антенны на макушке — рисуем ДО тела там, где торчат из-за него."""
    dx = int(bw * 0.30)
    dark = tuple(max(0, c - 40) for c in body)
    if kind == "pointed":                          # кошачьи/лисьи треугольники
        h = int(bw * 0.34)
        for sx in (-1, 1):
            ex = cx + sx * dx
            d.polygon([(ex - int(bw * 0.11), top_y + int(bw * 0.05)),
                       (ex + int(bw * 0.11), top_y + int(bw * 0.05)),
                       (ex + sx * int(bw * 0.02), top_y - h)], fill=body)
            d.polygon([(ex - int(bw * 0.05), top_y),
                       (ex + int(bw * 0.05), top_y),
                       (ex + sx * int(bw * 0.01), top_y - int(h * 0.55))], fill=(*_ACCENT, 150))
    elif kind == "round":                          # медвежьи/мышиные кружки
        er = int(bw * 0.17)
        for sx in (-1, 1):
            ex = cx + sx * dx
            d.ellipse([ex - er, top_y - er, ex + er, top_y + er], fill=body)
            d.ellipse([ex - er // 2, top_y - er // 2, ex + er // 2, top_y + er // 2], fill=dark)
    elif kind == "long":                           # заячьи длинные уши
        ew, eh = int(bw * 0.11), int(bw * 0.5)
        for sx in (-1, 1):
            ex = cx + sx * int(bw * 0.18)
            d.rounded_rectangle([ex - ew, top_y - eh, ex + ew, top_y + int(bw * 0.06)],
                                radius=ew, fill=body)
            d.rounded_rectangle([ex - ew // 2, top_y - eh + ew, ex + ew // 2, top_y - ew],
                                radius=ew // 2, fill=(*_ACCENT, 150))
    elif kind == "antenna":                        # роботские антенны с шариками
        for sx in (-1, 1):
            ex = cx + sx * int(bw * 0.16)
            tip = top_y - int(bw * 0.28)
            d.line([(ex, top_y), (ex, tip)], fill=(120, 130, 140), width=max(2, ss * 3))
            br = int(bw * 0.055)
            d.ellipse([ex - br, tip - br, ex + br, tip + br], fill=_ACCENT)


def _draw_hat(d, kind, cx, top_y, bw, rng, ss):
    """Головной убор поверх макушки."""
    if kind == "foil":                             # шапочка из фольги — серебристый конус
        tip = top_y - int(bw * 0.42)
        d.polygon([(cx - int(bw * 0.26), top_y + int(bw * 0.04)),
                   (cx + int(bw * 0.26), top_y + int(bw * 0.04)),
                   (cx + int(bw * 0.03), tip)], fill=(205, 210, 218))
        d.line([(cx - int(bw * 0.10), top_y - int(bw * 0.06)), (cx + int(bw * 0.06), tip)],
               fill=(240, 244, 250), width=max(2, ss * 2))
    else:                                          # цветная шапочка/колпак
        col = rng.choice(_HAT_COLORS)
        brim = top_y + int(bw * 0.05)
        d.rounded_rectangle([cx - int(bw * 0.30), brim - int(bw * 0.05),
                             cx + int(bw * 0.30), brim + int(bw * 0.05)],
                            radius=int(bw * 0.05), fill=col)
        d.pieslice([cx - int(bw * 0.26), brim - int(bw * 0.42),
                    cx + int(bw * 0.26), brim + int(bw * 0.10)], 180, 360, fill=col)
        pr = int(bw * 0.06)                        # помпон
        d.ellipse([cx - pr, brim - int(bw * 0.42) - pr, cx + pr, brim - int(bw * 0.42) + pr],
                  fill=(245, 245, 245))


def _render_procedural(prompt: str, seed_int: int) -> Image.Image:
    """Процедурный маскот в стиле приложения. Читает промт (тип существа/аксессуар) и собирает
    персонажа из ЧАСТЕЙ (уши, глаза, клюв/рот, шапка, очки, наушники), а не рисует один и тот
    же блоб. Детерминирован по seed_int (тот же промт → тот же рисунок). OpenTodo b288a948."""
    rng = random.Random(seed_int)
    ss = 2
    s = 512 * ss
    feat = _char_features(prompt)
    g1, g2 = rng.choice(_GRADIENTS)
    img = _vgrad(s, s, g1, g2)
    # световое пятно за персонажем — глубина вместо плоского фона
    glow, gmask = _radial_glow(s, s // 2, int(s * 0.5), int(s * 0.42),
                               tuple(min(255, c + 60) for c in g1))
    img = Image.composite(glow, img, gmask.point(lambda v: v * 3 // 5))
    d = ImageDraw.Draw(img, "RGBA")

    cx, cy = s // 2, int(s * 0.56)
    # морские существа — тело повыше/поуже (силуэт кальмара/рыбки), прочие — кругляш
    bw = int(s * (rng.uniform(0.40, 0.46) if feat["aquatic"] else rng.uniform(0.44, 0.52)))
    bh = int(bw * (rng.uniform(1.10, 1.30) if feat["aquatic"] else rng.uniform(0.94, 1.12)))
    body = rng.choice(_BODY)
    top_y = cy - bh // 2
    # мягкая тень под персонажем
    sh = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([cx - bw // 2, cy + bh // 2 - bh // 8, cx + bw // 2, cy + bh // 2 + bh // 10],
                               fill=(0, 0, 0, 95))
    img.paste(Image.new("RGB", (s, s), (0, 0, 0)), (0, 0), sh.filter(ImageFilter.GaussianBlur(s // 40)))
    d = ImageDraw.Draw(img, "RGBA")

    # уши — до тела (торчат из-за макушки)
    if feat["ears"]:
        _draw_ears(d, feat["ears"], cx, top_y, bw, body, rng, ss)
    # тело — вертикальный градиент (светлее сверху) вместо плоской заливки. Коробку считаем
    # ЯВНО (bw//2 округляет — маска и картинка тела обязаны совпасть по размеру пиксель-в-пиксель)
    x0, y0, x1, y1 = cx - bw // 2, top_y, cx + bw // 2, cy + bh // 2
    bw2, bh2 = x1 - x0, y1 - y0
    body_top = tuple(min(255, c + 18) for c in body)
    body_bot = tuple(max(0, c - 22) for c in body)
    bimg = _vgrad(bw2, bh2, body_top, body_bot)
    bmask = Image.new("L", (bw2, bh2), 0)
    ImageDraw.Draw(bmask).rounded_rectangle([0, 0, bw2 - 1, bh2 - 1], radius=int(bw * 0.42), fill=255)
    img.paste(bimg, (x0, y0), bmask)
    d = ImageDraw.Draw(img, "RGBA")
    # «слои» 3D-печати — фирменный мотив
    step = max(6, int(s * 0.013))
    for y in range(top_y, cy + bh // 2, step):
        d.line([(cx - bw // 2 + 2, y), (cx + bw // 2 - 2, y)], fill=(0, 0, 0, 14), width=ss)
    # блик сверху-слева — объём
    d.ellipse([cx - int(bw * 0.34), top_y + int(bh * 0.06), cx - int(bw * 0.02), top_y + int(bh * 0.32)],
              fill=(255, 255, 255, 42))

    # глаза
    eye_dx = int(bw * rng.uniform(0.19, 0.24))
    eye_y = cy - int(bh * rng.uniform(0.04, 0.12))
    er = int(bw * rng.uniform(0.09, 0.12))
    for sx in (-1, 1):
        ex = cx + sx * eye_dx
        d.ellipse([ex - er, eye_y - er, ex + er, eye_y + er], fill=(26, 30, 38))
        gr = max(2, er // 3)
        d.ellipse([ex - er + gr, eye_y - er + gr, ex - er + 2 * gr, eye_y - er + 2 * gr],
                  fill=(255, 255, 255, 235))
    # румяные щёчки
    chr_ = int(bw * 0.075)
    for sx in (-1, 1):
        cxx = cx + sx * int(bw * 0.30)
        d.ellipse([cxx - chr_, eye_y + er, cxx + chr_, eye_y + er + int(chr_ * 1.4)],
                  fill=(255, 150, 120, 70))
    # клюв (птицы) ИЛИ рот по настроению
    my = eye_y + int(er * 2.4)
    if feat["beak"]:
        bwd = int(bw * 0.13)
        d.polygon([(cx - bwd, my), (cx + bwd, my), (cx, my + int(bwd * 1.2))], fill=(255, 176, 32))
    else:
        mw = int(bw * 0.22)
        mood = rng.choice(["smile", "grin", "o", "line", "cat"])
        if mood == "smile":
            d.arc([cx - mw, my - mw // 2, cx + mw, my + mw], 20, 160, fill=(26, 30, 38), width=max(2, ss * 2))
        elif mood == "grin":
            d.pieslice([cx - mw, my - mw // 2, cx + mw, my + mw], 20, 160, fill=(26, 30, 38))
            d.line([(cx - mw + 2, my + mw // 4), (cx + mw - 2, my + mw // 4)],
                   fill=(255, 255, 255), width=max(2, ss))
        elif mood == "o":
            d.ellipse([cx - mw // 3, my - mw // 3, cx + mw // 3, my + mw // 3], fill=(26, 30, 38))
        elif mood == "cat":                        # ω-ротик
            d.arc([cx - mw, my - mw // 2, cx, my + mw // 2], 20, 160, fill=(26, 30, 38), width=max(2, ss * 2))
            d.arc([cx, my - mw // 2, cx + mw, my + mw // 2], 20, 160, fill=(26, 30, 38), width=max(2, ss * 2))
        else:
            d.line([(cx - mw // 2, my), (cx + mw // 2, my)], fill=(26, 30, 38), width=max(2, ss * 2))

    # наушники — дуга над головой + чашки по бокам
    if feat["headphones"]:
        d.arc([cx - int(bw * 0.44), top_y - int(bw * 0.10), cx + int(bw * 0.44), top_y + int(bw * 0.55)],
              180, 360, fill=(40, 44, 52), width=max(3, ss * 4))
        cr = int(bw * 0.11)
        for sx in (-1, 1):
            ex = cx + sx * int(bw * 0.44)
            d.rounded_rectangle([ex - cr, eye_y - cr, ex + cr, eye_y + cr], radius=cr // 2, fill=(40, 44, 52))
    # очки — оправа поверх глаз
    if feat["glasses"]:
        for sx in (-1, 1):
            ex = cx + sx * eye_dx
            d.ellipse([ex - int(er * 1.4), eye_y - int(er * 1.4), ex + int(er * 1.4), eye_y + int(er * 1.4)],
                      outline=(30, 34, 42), width=max(2, ss * 2))
        d.line([(cx - eye_dx + int(er * 1.4), eye_y), (cx + eye_dx - int(er * 1.4), eye_y)],
               fill=(30, 34, 42), width=max(2, ss * 2))
    # головной убор — поверх всего
    if feat["hat"]:
        _draw_hat(d, feat["hat"], cx, top_y, bw, rng, ss)

    return img.resize((512, 512), Image.LANCZOS)


# ───────── опциональный AI-бэкенд (локальный diffusion-сервер) ─────────
def _ai_image(prompt: str) -> bytes | None:
    """PNG от локального A1111-совместимого diffusion-сервера (если задан AVATAR_AI_URL). None при сбое."""
    if not config.AVATAR_AI_URL:
        return None
    style = (f"{prompt}, cute mascot character, 3d printed figurine, isometric, glossy, "
             "centered, app icon, warm orange accent, dark background, high quality")
    payload = json.dumps({
        "prompt": style, "negative_prompt": "text, watermark, blurry, deformed",
        "width": 512, "height": 512, "steps": 20, "cfg_scale": 6,
        "sampler_name": "Euler a",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(config.AVATAR_AI_URL.rstrip("/") + "/sdapi/v1/txt2img",
                                     data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        imgs = data.get("images") or []
        if not imgs:
            return None
        import base64
        raw = imgs[0].split(",", 1)[-1]
        return base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        log.warning("avatar AI backend failed — откат на процедурный рендер", exc_info=True)
        return None


def generate(user_id: int, prompt: str | None = None, avoid: str = "", *,
             randomize: bool = False, scene: bool = False) -> dict:
    """Сгенерировать и сохранить аватарку пользователя. Синхронно — звать через to_thread.
    {ok, path, prompt, seed}. Картинка детерминирована промтом (тот же промт → тот же рисунок).
    avoid — предыдущий промт пользователя (существо не повторяется два раза подряд).

    randomize=True — сид случайный, а не из промта: кнопка «Сгенерировать» у release notes
    (OpenTodo 83e1a735) зовётся с одним и тем же промтом, и детерминированный сид выдавал бы
    одну и ту же картинку на каждый повтор. Дефолт не трогаем: у аватарок «тот же промт →
    тот же рисунок» — осознанное свойство (тесты и повторная генерация профиля).

    scene=True — иллюстрация к release note, а не аватарка (02.09.2026): стиль «сцена с
    маскотами» вместо «портрет для иконки приложения», горизонтальный кадр 4:3 под пост в
    Telegram, и AI-картинка сохраняется в родном размере (~1 Мпикс), а не ужимается до 512².
    Процедурный фолбэк у сцены тот же квадратный маскот — своей «сцены» у него нет.

    Приоритет картинки: внешний diffusion-сервер (AVATAR_AI_URL, если задан) → локальная
    видеокарта (avatars_gpu, OpenTodo a327643f) → процедурный рендер (всегда срабатывает)."""
    if not prompt:
        from . import avatars_gpu
        prompt = funny_prompt(skip_ollama=avatars_gpu.available(), avoid=avoid)
    seed = (f"{random.getrandbits(48):012x}" if randomize
            else hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12])
    seed_int = int(seed, 16)
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.AVATAR_DIR / f"{user_id}.png"
    # engine — чем реально нарисовано (фидбэк 21.07 «почему процедурный»): UI честно скажет,
    # что вышел запасной/процедурный рисунок, а не сделает вид, что так и задумано.
    ai, engine = _ai_image(prompt), "ai-url"
    if not ai:
        from . import avatars_gpu
        gpu = avatars_gpu.generate_png(prompt, seed_int, scene=scene)
        ai, engine = (gpu if gpu else (None, "procedural"))
    try:
        if ai:
            img = Image.open(__import__("io").BytesIO(ai)).convert("RGB")
            if not scene:
                img = img.resize((512, 512), Image.LANCZOS)
        else:
            img = _render_procedural(prompt, seed_int)
        img.save(dest, "PNG", optimize=True)
    except Exception:  # noqa: BLE001
        log.warning("avatar render failed", exc_info=True)
        return {"ok": False, "error": "Не удалось нарисовать аватар"}
    return {"ok": True, "path": str(dest), "prompt": prompt, "seed": seed, "engine": engine}
