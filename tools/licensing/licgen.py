"""Выпуск лицензионного ключа покупателю (машина владельца).

    .venv\\Scripts\\python.exe tools\\licensing\\licgen.py ^
        --key D:\\secrets\\print-studio-license.key ^
        --studio "Студия «Пример», ИП Иванов" --id 007 --plan subscription --months 12

Печатает две вещи: сам ключ (одна строка — её отдают покупателю, он вставляет в .env
строкой LICENSE_KEY= или кладёт файлом license.key в каталог данных) и строку реестра
выданных лицензий (CSV: id;дата;студия;редакция;план;срок;отпечаток) — реестр владелец
ведёт сам, вне репозитория, см. docs/Перспективные разработки/licensing-2026-08-23.md.

Приватный ключ подписи берётся ТОЛЬКО из файла по --key и по умолчанию не ищется нигде:
ключ живёт вне дерева проекта, и «удобный дефолт» здесь — прямой путь к утечке.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sign import load_seed, public_key_hex, sign  # noqa: E402

PREFIX = "PSB1"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_key(payload: dict, seed: bytes) -> str:
    """Ключ = PSB1.<base64url(JSON)>.<base64url(подпись)>. Подпись — по БАЙТАМ среднего
    сегмента: у выпускающего и у проверяющего он байт-в-байт один и тот же, канонизация
    JSON не нужна (и не существует)."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    seg = _b64u(raw)
    return f"{PREFIX}.{seg}.{_b64u(sign(seg.encode('ascii'), seed))}"


def build_payload(studio: str, key_id: str, edition: str, plan: str,
                  expires: str | None, issued: str | None = None) -> dict:
    return {
        "studio": studio,
        "id": key_id,
        "edition": edition,
        "plan": plan,
        "issued": issued or date.today().isoformat(),
        "expires": expires,          # null = бессрочная лицензия (разовая продажа)
    }


def main() -> int:
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Выпуск лицензионного ключа Print Studio")
    ap.add_argument("--key", required=True, help="файл приватного ключа выпуска (вне репозитория)")
    ap.add_argument("--studio", required=True, help="кому выдана лицензия (студия/покупатель)")
    ap.add_argument("--id", required=True, help="номер лицензии для реестра")
    ap.add_argument("--edition", default="studio", help="редакция продукта (studio/dev)")
    ap.add_argument("--plan", default="", help="perpetual (по умолчанию при отсутствии срока) / subscription / своё")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--months", type=int, help="срок подписки в месяцах (30 дней)")
    g.add_argument("--days", type=int, help="срок подписки в днях")
    g.add_argument("--until", help="дата окончания YYYY-MM-DD")
    g.add_argument("--perpetual", action="store_true", help="бессрочная лицензия (без срока)")
    ap.add_argument("--issued", help="дата выдачи YYYY-MM-DD (по умолчанию сегодня)")
    a = ap.parse_args()

    expires: str | None = None
    if a.until:
        try:
            expires = date.fromisoformat(a.until).isoformat()
        except ValueError:
            ap.error(f"--until: нечитаемая дата {a.until!r}, нужен вид ГГГГ-ММ-ДД")
    elif a.days:
        expires = (date.today() + timedelta(days=a.days)).isoformat()
    elif a.months:
        expires = (date.today() + timedelta(days=30 * a.months)).isoformat()
    elif not a.perpetual:
        print("Укажите срок (--months/--days/--until) или явно --perpetual: "
              "модель продажи выбирается для каждой лицензии осознанно.", file=out)
        out.flush()
        return 2
    plan = a.plan or ("subscription" if expires else "perpetual")

    if a.issued:
        # Дату выдачи проверяем ДО подписи: опечатка здесь уедет покупателю в карточку
        # «Лицензия», а проверяющий такой ключ теперь честно отвергает.
        try:
            date.fromisoformat(a.issued)
        except ValueError:
            print("Дата выдачи --issued должна быть в формате YYYY-MM-DD.", file=out)
            out.flush()
            return 2

    seed = load_seed(a.key)
    payload = build_payload(a.studio, a.id, a.edition, plan, expires, a.issued)
    key = make_key(payload, seed)
    fp = hashlib.sha256(key.encode("ascii")).hexdigest()[:16]

    print("Ключ (одна строка — покупателю):", file=out)
    print(key, file=out)
    print("", file=out)
    print("Строка реестра (id;выдан;студия;редакция;план;до;отпечаток):", file=out)
    print(";".join([payload["id"], payload["issued"], payload["studio"], payload["edition"],
                    payload["plan"], expires or "бессрочно", fp]), file=out)
    print("", file=out)
    print(f"Публичный ключ этой пары (должен совпадать с PUBLIC_KEYS в app/license.py):"
          f" {public_key_hex(seed)}", file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
