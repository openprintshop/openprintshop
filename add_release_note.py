"""Черновик release notes в центр уведомлений админки (2026-07-16).

Запускает Клод после деплоя изменений (или мастер вручную): кладёт в admin_notifications
черновик kind='release_note' с текстом для клиентов и (опционально) картинкой. Публикация —
ТОЛЬКО из мини-аппы (Админ → Ещё → Уведомления → «Опубликовать»): скрипт ничего не рассылает.

Примеры:
  .venv/Scripts/python add_release_note.py --title "Семейный счёт" --body-file note.txt
  .venv/Scripts/python add_release_note.py --title "..." --body "..." --image pic.jpg
  .venv/Scripts/python add_release_note.py --title "..." --body "..." --gen "семья роботов у принтера"

--gen генерит картинку той же цепочкой, что аватарки (Z-Image-Turbo → облегчённый квант →
sd-turbo → процедурный фолбэк), в режиме «сцена» (кадр 4:3, стиль иллюстрации, не иконки).
Если бот запущен и ПК-токен админки открыт, рисует САМ бот через свою ручку image_regen
(02.09.2026): у видеокарты один хозяин — иначе скрипт поднимал вторую копию модели рядом с
воркером бота, видел меньше свободной VRAM и уходил на слабый запасной тир. Бот лежит или
режим входа «личный» — рисуем из скрипта. Движок печатается честно («нарисовано: …»).
--image просто копирует файл (ужимая по длинной стороне до 1600px, чтобы пролезть в Telegram).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Вывод — UTF-8: заголовки и текст release notes кириллические, а финальное «Публикация: …»
# содержит стрелку и эмодзи. В консоли Windows PowerShell 5.1 (cp1251) print() падал на них
# UnicodeEncodeError уже ПОСЛЕ вставки черновика и генерации картинки — работа сделана, а
# скрипт «упал», что читалось как провал. reconfigure глушит это на любой консоли.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — очень старый Python / необычный поток: не критично
    pass

from app import config  # noqa: E402


def _insert(title: str, body: str) -> int:
    con = sqlite3.connect(config.DB_PATH, timeout=15)
    try:
        now = int(time.time())
        cur = con.execute(
            "INSERT INTO admin_notifications(kind, title, body, image_path, payload, status, "
            "created_at, updated_at) VALUES('release_note', ?, ?, NULL, ?, 'new', ?, ?)",
            (title.strip()[:120], body.strip()[:4000], json.dumps({}), now, now))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _set_image(nid: int, path: Path) -> None:
    config.NOTIF_IMG_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.NOTIF_IMG_DIR / f"notif_{nid}.jpg"
    try:
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((1600, 1600))
        img.convert("RGB").save(dest, "JPEG", quality=88)
    except Exception:  # noqa: BLE001 — нет PIL/битый файл: копируем как есть
        shutil.copyfile(path, dest)
    con = sqlite3.connect(config.DB_PATH, timeout=15)
    try:
        con.execute("UPDATE admin_notifications SET image_path = ?, updated_at = ? WHERE id = ?",
                    (str(dest), int(time.time()), nid))
        con.commit()
    finally:
        con.close()


def _set_gen_prompt(nid: int, prompt: str) -> None:
    con = sqlite3.connect(config.DB_PATH, timeout=15)
    try:
        con.execute("UPDATE admin_notifications SET payload = ?, updated_at = ? WHERE id = ?",
                    (json.dumps({"gen_prompt": prompt}, ensure_ascii=False), int(time.time()), nid))
        con.commit()
    finally:
        con.close()


_ENGINE_HUMAN = {
    "z-image-turbo": "Z-Image-Turbo (основной квант)",
    "z-image-turbo-lite": "Z-Image-Turbo в облегчённом кванте (мало свободной VRAM)",
    "sd-turbo": "запасной sd-turbo (слабая модель — картинку стоит перегенерить)",
    "sdxl-turbo": "SDXL-Turbo",
    "procedural": "ПРОЦЕДУРНЫЙ рисунок (ИИ не смог) — картинку стоит заменить",
    "ai-url": "внешний diffusion-сервер",
}


def _gen_via_bot(nid: int, prompt: str) -> dict | None:
    """Попросить ЗАПУЩЕННОГО бота нарисовать картинку черновика его же ручкой image_regen
    (та же цепочка и та же модель, что у кнопки «↻ Заменить» в админке). Возвращает ответ
    бота {ok, engine, image_url} или None — бот лежит / токена нет / вход «личный» (403) /
    сеть: тогда вызывающий рисует из скрипта сам."""
    tok = getattr(config, "ADMIN_PANEL_TOKEN", "")
    if not tok:
        return None
    import urllib.error
    import urllib.request
    base = f"http://127.0.0.1:{config.WEBAPP_PORT}"
    try:
        with urllib.request.urlopen(base + "/health", timeout=3) as r:
            if r.status != 200:
                return None
    except Exception:  # noqa: BLE001 — бот не запущен
        return None
    body = json.dumps({"initData": tok, "id": nid, "action": "image_regen",
                       "prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(base + "/api/admin/notification_action", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        # холодная модель грузится до 4 минут (воркер) + генерация ~20 с
        with urllib.request.urlopen(req, timeout=420) as r:
            res = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            print("Бот запущен, но ПК-токен закрыт (личный режим входа) — рисую из скрипта")
        else:
            print(f"Бот ответил HTTP {exc.code} — рисую из скрипта")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"Не достучался до бота ({exc.__class__.__name__}) — рисую из скрипта")
        return None
    return res if isinstance(res, dict) else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Черновик release notes в уведомления админки")
    ap.add_argument("--title", required=True, help="заголовок (клиенты увидят его жирным)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--body", help="текст для клиентов")
    g.add_argument("--body-file", help="файл с текстом (utf-8)")
    img = ap.add_mutually_exclusive_group()
    img.add_argument("--image", help="путь к готовой картинке")
    img.add_argument("--gen", help="промт — сгенерировать картинку (цепочка аватарок)")
    args = ap.parse_args()

    body = args.body if args.body else Path(args.body_file).read_text(encoding="utf-8")
    nid = _insert(args.title, body)
    print(f"Черновик #{nid} добавлен")

    if args.image:
        _set_image(nid, Path(args.image))
        print("Картинка приложена")
    elif args.gen:
        via_bot = _gen_via_bot(nid, args.gen)
        if via_bot is not None and via_bot.get("ok"):
            # бот сам сохранил notif_<id>.jpg и gen_prompt в payload
            eng = str(via_bot.get("engine") or "")
            print(f"Картинка сгенерирована ботом — нарисовано: {_ENGINE_HUMAN.get(eng, eng or '?')}")
        else:
            if via_bot is not None:
                print(f"Бот не смог ({via_bot.get('error')}) — рисую из скрипта")
            from app import avatars
            # 0 — служебный uid, файл data/avatars/0.png; scene — иллюстрация к посту (кадр 4:3,
            # стиль сцены), randomize — повтор команды даёт новую картинку, а не ту же
            res = avatars.generate(0, args.gen, randomize=True, scene=True)
            if res.get("ok"):
                _set_image(nid, Path(res["path"]))
                _set_gen_prompt(nid, args.gen)    # диалог «Сгенерировать» в админке предзаполнит его
                eng = str(res.get("engine") or "")
                print(f"Картинка сгенерирована — нарисовано: {_ENGINE_HUMAN.get(eng, eng or '?')}")
            else:
                print(f"Картинку сгенерировать не удалось ({res.get('error')}) — черновик без неё")
    print("Публикация: мини-аппа → Админ → Ещё → Уведомления → «Опубликовать».")


if __name__ == "__main__":
    main()
