"""Локальный генератор аватарок профиля (OpenTodo fca4246e)."""
import random

from PIL import Image

from app import avatars, config


def test_wordbank_prompt_format():
    p = avatars._wordbank_prompt(random.Random(42))
    assert isinstance(p, str) and 3 <= len(p) <= 60
    assert avatars._wordbank_prompt(random.Random(42)) == p     # детерминирован по сиду


def test_funny_prompt_fallback(monkeypatch):
    monkeypatch.setattr(avatars, "_ollama_prompt", lambda: None)   # Ollama недоступен → словарь
    assert avatars.funny_prompt("x") == avatars.funny_prompt("x")  # тот же сид → та же фраза
    assert isinstance(avatars.funny_prompt("y"), str)


def test_generate_makes_512_png_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(config, "AVATAR_AI_URL", "")              # без AI-бэкенда — процедурный рендер
    r1 = avatars.generate(7, "космический ёжик")
    assert r1["ok"] and r1["prompt"] == "космический ёжик"
    p = tmp_path / "avatars" / "7.png"
    assert p.exists()
    with Image.open(p) as im:
        assert im.size == (512, 512) and im.mode == "RGB"
    r2 = avatars.generate(7, "космический ёжик")
    assert r2["seed"] == r1["seed"]                              # тот же промт → тот же сид/рисунок


def test_generate_randomize_new_seed_each_time(tmp_path, monkeypatch):
    """randomize=True (кнопка «Сгенерировать» у release notes, OpenTodo 83e1a735): тот же
    промт даёт РАЗНЫЕ сиды — повтор кнопки рисует новую картинку. Дефолт не трогаем."""
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(config, "AVATAR_AI_URL", "")
    r1 = avatars.generate(0, "робот у принтера", randomize=True)
    r2 = avatars.generate(0, "робот у принтера", randomize=True)
    assert r1["ok"] and r2["ok"]
    assert r1["seed"] != r2["seed"]      # сид 48-битный случайный: совпадение ~2⁻⁴⁸


def test_ai_backend_off_returns_none():
    assert avatars._ai_image("что угодно") is None               # AVATAR_AI_URL пуст по умолчанию


async def test_user_avatar_db_roundtrip(tmp_db):
    db = tmp_db
    assert await db.get_user_avatar(1) is None
    await db.set_user_avatar(1, "дракон", "abc123")
    a = await db.get_user_avatar(1)
    assert a["prompt"] == "дракон" and a["seed"] == "abc123"
    await db.set_user_avatar(1, "кот", "def456")                 # upsert
    a = await db.get_user_avatar(1)
    assert a["prompt"] == "кот" and a["seed"] == "def456"
