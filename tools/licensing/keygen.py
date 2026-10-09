"""Разовая генерация ПАРЫ ключей выпуска лицензий (Ed25519).

Запускать ОДИН РАЗ и только на машине владельца:

    .venv\\Scripts\\python.exe tools\\licensing\\keygen.py --out D:\\secrets\\print-studio-license.key

Приватный ключ (32 байта seed, hex) сохраняется по указанному пути — он НИКОГДА не
должен попасть в репозиторий, в установку покупателя, в переписку или в облако: кто
им владеет, тот выписывает лицензии от имени владельца. Путь по умолчанию нарочно
указывать нельзя — скрипт требует --out явно, чтобы файл не «случайно» лёг в дерево.

Публичный ключ печатается в консоль: его строкой вставляют в app/license.py в
PUBLIC_KEYS. Только он и едет покупателю — проверять им можно, подписывать нельзя.

Утеря приватного ключа = невозможность выпускать НОВЫЕ ключи (уже выданные продолжают
работать). Компрометация = нужно выпустить новую пару, добавить новый публичный ключ в
PUBLIC_KEYS рядом со старым и перевыпустить лицензии; отзыв старых офлайн невозможен —
см. docs/Перспективные разработки/licensing-2026-08-23.md.
"""
from __future__ import annotations

import argparse
import io
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sign import public_key_hex  # noqa: E402


def main() -> int:
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Пара ключей выпуска лицензий (Ed25519)")
    ap.add_argument("--out", required=True, help="куда положить ПРИВАТНЫЙ ключ (вне репозитория!)")
    ap.add_argument("--force", action="store_true", help="перезаписать существующий файл")
    a = ap.parse_args()

    dst = Path(a.out).expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if repo in dst.parents:
        print("ОТКАЗ: приватный ключ нельзя класть внутрь репозитория "
              f"({repo}). Выберите путь снаружи.", file=out)
        return 2
    if dst.exists() and not a.force:
        print(f"ОТКАЗ: {dst} уже существует. Перезапись — только с --force "
              "(потеря приватного ключа необратима).", file=out)
        return 2

    seed = secrets.token_bytes(32)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(seed.hex() + "\n", encoding="ascii")
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    print(f"Приватный ключ записан: {dst}", file=out)
    print("Храните его как пароль: копия в менеджере паролей/офлайн, в git — НИКОГДА.", file=out)
    print("", file=out)
    print("Публичный ключ — вставьте в app/license.py:", file=out)
    print(f'PUBLIC_KEYS: tuple[str, ...] = ("{public_key_hex(seed)}",)', file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
