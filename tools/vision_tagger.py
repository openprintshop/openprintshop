# -*- coding: utf-8 -*-
"""ИИ-распознавалка каталога (OpenTodo e0dedc27): по превью модели vision-LLM (Ollama,
qwen3-vl:8b) даёт красивое русское название (2–4 слова) и 3–5 тегов.

- Целевая аудитория — записи с кривыми названиями из ВК-импорта (hash vk*), но флагом
  --all можно прогнать весь каталог.
- label ← новое название, исходное сохраняется в orig_label (один раз), tags ← через запятую.
- Резюмируемо: записи с непустыми tags пропускаются.
- На время прогона ставится файл-флаг data/avatar_gpu.off (ИИ-аватарки уходят в процедурные):
  vision 8B (~8 ГБ VRAM) и Z-Image-воркер (~9 ГБ) вдвоём в 12 ГБ не влезают. Если флаг стоял
  до запуска (выключено владельцем) — не трогаем и при выходе не снимаем.
- Валидация: в названии обязана быть кириллица (модель изредка съезжает в английский) — ретрай.

Запуск (из корня репо): .venv/Scripts/python tools/vision_tagger.py [--all] [--limit N] [--dry]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent

def _data_dir() -> Path:
    """Каталог данных: DATA_DIR из окружения/.env, иначе <папка бота>/data (C2).

    Свой парсер, а не app.config: инструмент запускают отдельным python без venv бота."""
    raw = (os.getenv("DATA_DIR") or "").strip().strip('"').strip("'")
    if not raw:
        try:
            for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == "DATA_DIR":
                    raw = v.strip().strip('"').strip("'")
                    break
        except OSError:
            raw = ""
    return Path(raw) if raw else ROOT / "data"


DATA_DIR = _data_dir()


def _abs(p: str | Path | None) -> Path | None:
    """Значение пути ИЗ БД → абсолютный Path (C2: в базе лежат относительные пути).

    Зеркало ``config.abs_data`` в упрощённом виде: относительное — от DATA_DIR, всё, у
    чего есть якорь (диск или корень), — как есть. Без этого после миграции v8 скрипт
    не нашёл бы ни одной картинки и МОЛЧА пропустил бы весь каталог."""
    if not p:
        return None
    q = Path(p)
    return q if q.anchor else DATA_DIR / q


def pick_image(row) -> Path | None:
    """Картинка записи каталога (фото важнее превью) или None, если её нет на диске."""
    img = _abs(row["photo_path"] or row["preview_path"])
    return img if img is not None and img.exists() else None


DB = DATA_DIR / "bot.db"
OFF_FLAG = DATA_DIR / "avatar_gpu.off"
OLLAMA = "http://127.0.0.1:11434"
# ВАЖНО: именно -instruct. Дефолтный тег qwen3-vl:8b — thinking-сборка: с format-схемой она
# бесконечно «думает» и возвращает пустой content (проверено живьём, think:false не помогает).
MODEL = "qwen3-vl:8b-instruct"

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5},
    },
    "required": ["title", "tags"],
}

SYSTEM_PROMPT = """Ты каталогизатор 3D-печатных моделей. На входе — превью-картинка модели \
для 3D-принтера и её исходное название (часто мусорное, латиницей).

Верни JSON:
- "title": красивое русское название объекта, 2–4 слова, с заглавной буквы, без кавычек \
и точки. Пример: "Флекси-робот Zippy", "Подставка для наушников", "Дракон с шарнирами". \
Имя собственное персонажа (Zippy, Baby Yoda) можно оставить латиницей, всё остальное — \
ТОЛЬКО по-русски.
- "tags": 3–5 тегов по-русски, в нижнем регистре, одно-два слова каждый. \
Пример: ["флекси", "робот", "игрушка", "print-in-place"].

Опирайся в первую очередь на картинку; исходное название используй как подсказку \
(персонаж, автор, тип). Не выдумывай деталей, которых не видно. Отвечай ТОЛЬКО JSON."""


def log(msg: str) -> None:
    print(msg, flush=True)


def ask(client: httpx.Client, img_path: str, vk_name: str, temp: float = 0.2) -> dict | None:
    img_b64 = base64.b64encode(Path(img_path).read_bytes()).decode()
    r = client.post(f"{OLLAMA}/api/chat", json={
        "model": MODEL, "stream": False, "format": SCHEMA,
        "keep_alive": "30m",
        "think": False,   # иначе весь num_predict сгорает на размышления, content пустой
        "options": {"temperature": temp, "num_predict": 400},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Исходное название (может содержать подсказку): «{vk_name}»",
             "images": [img_b64]},
        ]}, timeout=180)
    r.raise_for_status()
    try:
        return json.loads(r.json()["message"]["content"])
    except (KeyError, ValueError):
        return None


def tag_one(client: httpx.Client, img_path: str, vk_name: str) -> tuple[str, list[str]] | None:
    for attempt, temp in enumerate((0.2, 0.4, 0.6)):
        try:
            j = ask(client, img_path, vk_name, temp)
        except Exception as exc:  # noqa: BLE001
            log(f"    ! ollama error (try {attempt + 1}): {exc}")
            time.sleep(2)
            continue
        if not j:
            continue
        title = re.sub(r"\s+", " ", str(j.get("title") or "")).strip(" \"'«».")[:80]
        tags = [re.sub(r"\s+", " ", str(t)).strip().lower()[:30]
                for t in (j.get("tags") or []) if str(t).strip()][:5]
        # русское название обязано содержать кириллицу (модель изредка съезжает в английский)
        if title and re.search(r"[а-яА-ЯёЁ]", title) and len(tags) >= 2:
            return title, tags
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="весь каталог, а не только ВК-импорт")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    client = httpx.Client(timeout=180)
    try:
        names = [m["name"] for m in client.get(f"{OLLAMA}/api/tags").json().get("models", [])]
    except Exception:
        log("Ollama не отвечает на 11434 — запустите `ollama serve` и повторите.")
        sys.exit(1)
    if not any(n.startswith(MODEL.split(":")[0]) for n in names):
        log(f"Модель {MODEL} не установлена — выполните `ollama pull {MODEL}`.")
        sys.exit(1)

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    where = "tags = ''" + ("" if args.all else " AND hash LIKE 'vk%'")
    rows = con.execute(
        f"SELECT id, label, preview_path, photo_path FROM collection WHERE {where} "
        "ORDER BY id").fetchall()
    if args.limit:
        rows = rows[:args.limit]
    log(f"К обработке: {len(rows)} записей (модель {MODEL})")

    flag_was_set = OFF_FLAG.exists()
    if not flag_was_set and not args.dry:
        OFF_FLAG.write_text("vision_tagger", encoding="utf-8")   # аватарки → процедурные на время
        log("Поставил avatar_gpu.off на время прогона (VRAM нужен vision-модели)")
    done = skipped = failed = 0
    t_start = time.time()
    try:
        for i, r in enumerate(rows, 1):
            img = pick_image(r)
            if img is None:
                skipped += 1
                continue
            res = tag_one(client, str(img), r["label"] or "")
            if not res:
                failed += 1
                log(f"  [{i}/{len(rows)}] #{r['id']} не распознано: {r['label'][:40]!r}")
                continue
            title, tags = res
            if args.dry:
                log(f"  [{i}/{len(rows)}] #{r['id']} {r['label'][:36]!r} → {title!r} {tags}")
            else:
                con.execute(
                    "UPDATE collection SET label = ?, tags = ?, "
                    "orig_label = CASE WHEN orig_label = '' THEN label ELSE orig_label END "
                    "WHERE id = ?", (title, ", ".join(tags), r["id"]))
                con.commit()
                log(f"  [{i}/{len(rows)}] #{r['id']} → {title!r} · {', '.join(tags)}")
            done += 1
            if i % 50 == 0:
                rate = (time.time() - t_start) / max(1, i)
                log(f"  … {i}/{len(rows)}, ~{rate:.1f} c/шт, осталось ~{rate * (len(rows) - i) / 60:.0f} мин")
    finally:
        if not flag_was_set and OFF_FLAG.exists() and OFF_FLAG.read_text(encoding="utf-8").startswith("vision_tagger"):
            OFF_FLAG.unlink(missing_ok=True)
            log("Снял avatar_gpu.off — ИИ-аватарки снова доступны")
    log(f"ИТОГО: обработано {done}, пропущено (нет картинки) {skipped}, не распознано {failed}, "
        f"за {(time.time() - t_start) / 60:.0f} мин")
    con.close()


if __name__ == "__main__":
    main()
