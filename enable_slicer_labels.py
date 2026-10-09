"""Включить метки объектов в пользовательских процесс-профилях ElegooSlicer.

Две галочки, обе — настройки ПРОЦЕССА:

* `gcode_label_objects` — метки-КОММЕНТАРИИ (`; printing object …`). По ним «Допечатка со слоя»
  (раздел «Инструменты», app/resumecut.py) вырезает сорвавшуюся деталь ТОЧНО и называет её по
  имени из проекта; без меток детали ищутся эвристикой по первому слою, и на плитах с бримом,
  поддержками или деталями разного размера она честно отказывает («детали не разделяются»).
* `exclude_object` — команды Klipper `EXCLUDE_OBJECT_DEFINE / START / END`. По ним прошивка
  сообщества CC2 (CC2 Control, кнопка «Снять деталь» в деске) снимает деталь НА ХОДУ, не
  останавливая печать. В системном профиле CC2 у Elegoo эта галочка выключена явно ("0"), и все
  пользовательские профили её наследуют. Стоковая прошивка эти команды просто пропускает.

Запуск: закрыть ElegooSlicer, затем `python enable_slicer_labels.py` (добавить `--dry-run`,
чтобы только посмотреть). Каждый изменённый файл сохраняется рядом как `<имя>.json.bak-<дата>`.
Под работающим слайсером скрипт отказывает: слайсер держит пресеты в памяти и перепишет файлы.
Старые задания меток не получат — их надо перенарезать.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

KEYS = ("gcode_label_objects", "exclude_object")


def slicer_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq elegoo-slicer.exe"],
                             capture_output=True, text=True, timeout=10).stdout.lower()
        return "elegoo-slicer" in out
    except Exception:  # noqa: BLE001
        return False


def _on(v) -> bool:
    return str(v if v is not None else "0").strip() in ("1", "true", "True")


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    # консоль Windows в cp1251 давится стрелками и частью кириллицы в именах пресетов
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    appdata = os.getenv("APPDATA") or ""
    root = Path(appdata) / "ElegooSlicer" / "user"
    if not root.is_dir():
        print("Профили ElegooSlicer не найдены:", root)
        return 1
    if slicer_running() and not dry:
        print("ElegooSlicer запущен — закройте его и повторите (иначе он перепишет профили).")
        return 2
    changed = skipped = 0
    stamp = time.strftime("%Y%m%d_%H%M")
    for p in sorted(root.glob("*/process/*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  ? {p.name}: не прочитать ({exc})")
            continue
        missing = [k for k in KEYS if not _on(data.get(k))]
        if not missing:
            skipped += 1
            continue
        print(f"  + {p.name}: " + ", ".join(f"{k} = 1" for k in missing))
        if not dry:
            bak = p.with_name(p.name + f".bak-{stamp}")
            bak.write_bytes(p.read_bytes())
            for k in missing:
                data[k] = "1"
            p.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        changed += 1
    print(f"Готово: изменено {changed}, уже с метками {skipped}{' (пробный прогон)' if dry else ''}.")
    if changed and not dry:
        print("Откройте ElegooSlicer заново: пресеты перечитаются, новые нарезки пойдут с метками.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
