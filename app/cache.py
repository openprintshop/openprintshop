"""Кэш результатов слайсинга: одинаковая модель + параметры -> мгновенный ответ."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


_hash_memo: dict = {}   # (путь, размер, mtime_ns) -> sha1


def _hash_file(p: Path) -> str:
    """SHA1 содержимого файла, мемоизированный по (путь, размер, mtime_ns). Один и тот же файл
    (повтор расчёта, модель из коллекции, тот же материал) больше не перечитывается и не
    перехэшируется целиком. Алгоритм НЕ меняем (иначе сбросился бы весь кэш слайсинга).
    Файлы моделей write-once (uuid-имена / неизменные сохранённые) → ложного попадания нет."""
    key = None
    try:
        st = p.stat()
        key = (str(p), st.st_size, st.st_mtime_ns)
        hit = _hash_memo.get(key)
        if hit:
            return hit
    except OSError:
        pass
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if key is not None:
        if len(_hash_memo) >= 256:   # предохранитель от роста
            _hash_memo.clear()
        _hash_memo[key] = digest
    return digest


def model_hash(model_paths) -> str:
    """Хэш только по содержимому файлов модели (без параметров) — для истории.

    C2: путь приводим через config.abs_data. На OSError хэш считается от СТРОКИ пути, и
    относительный путь, не найденный от текущего каталога, тихо дал бы другой хэш —
    задвоение коллекции и промах кэша расчётов без единой ошибки в логе."""
    h = hashlib.sha1()
    for p in sorted(str(config.abs_data(x)) for x in model_paths):
        try:
            h.update(_hash_file(Path(p)).encode())
        except OSError:
            h.update(p.encode())
    return h.hexdigest()


def make_key(model_paths, *, machine_profile: str, process: str, layer_height: float,
             infill: int, walls: int, filament: str, extra: str = "") -> str:
    h = hashlib.sha1()
    for p in sorted(str(config.abs_data(x)) for x in model_paths):   # C2, см. model_hash
        try:
            h.update(_hash_file(Path(p)).encode())
        except OSError:
            h.update(p.encode())
    # v6: профиль процесса И машинный профиль раньше отдавались слайсеру листом с опорой на резолв
    #     inherits в CLI; резолв молчком не срабатывал → терялись скорости/ускорения процесса
    #     (default_acceleration=10000) И кинематика машины (machine_max_acceleration_x/y=20000,
    #     machine_max_speed_x/y=500), по которой планировщик считает время → время печати
    #     кэшировалось завышенным ~×2 (заказ #20: 15ч38м вместо ~7.5ч). Теперь оба профиля
    #     расплющиваются целиком (_flatten_profile), но ключ кэша содержимое профиля не учитывает →
    #     бампаем ревизию, чтобы сбросить все завышенные по времени записи.
    # v5: предыдущий бамп лимита 40→256 жил только в дефолте config — в .env остался MAX_MESH_FILES=40,
    #     и сборки реально резались по 40 деталей (ветряк «Pompe à vent»: 61 дет.→40, 367 вместо ~590 cm³).
    #     .env исправлен; ключ кэша лимит НЕ учитывает, поэтому ревизию бампаем, чтобы сбросить занижённые
    #     записи. (v4: мультиплейт без дедупа + дефолт 40→256; v3: дедуп копий.)
    # v8: детектор мис-ассембли production-3mf ослеп на файлах ElegooSlicer — тот не пишет
    #     face_count в model_settings.config (только <mesh_stat>), declared=0 → ручная сборка
    #     не включалась и двухкомпонентные объекты считались дважды («пивные открывашка»
    #     044a4bcd: 227 см³ вместо 140). Теперь ловим по дублям геометрий + p:path; сбрасываем
    #     завышенные записи.
    # v7: production-3mf (компонентные сборки) собираются вручную при кратном расхождении
    #     граней с метаданными (springy kitty: trimesh раздувал геометрию ×8 → объём и цена
    #     считались кратно завышенными) — сбрасываем потенциально раздутые записи.
    h.update(f"|v8|{machine_profile}|{process}|{layer_height}|{infill}|{walls}|{filament}".encode())
    # extra — доп. параметры расчёта, влияющие на результат (напр. выбранные столы мультиплейт-
    # модели «plates:0,2»). Подмешиваем ТОЛЬКО непустой, чтобы ключи обычных полных расчётов
    # не изменились (иначе весь накопленный кэш инвалидировался бы впустую).
    if extra:
        h.update(f"|{extra}".encode())
    return h.hexdigest()


def get(key: str) -> dict | None:
    f = config.CACHE_DIR / f"{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def put(key: str, data: dict) -> None:
    try:
        (config.CACHE_DIR / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("cache write failed", exc_info=True)
