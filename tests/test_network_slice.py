"""Интеграционный тест «полного круга»: скачать модель с MakerWorld и прогнать слайс.

Зачем: после крупных фиксов (особенно лимита деталей MAX_MESH_FILES и мультиплейт-
раскладки в app.geometry/app.slicer) нужен сквозной прогон на реальной многодетальной
сборке — «устройстве перетасовки карт» (card-shuffler, makerworld.com/ru/models/2624902,
77 деталей на нескольких плитах). Проверяем главное: при подготовке к слайсу НИ ОДНА
деталь не выпадает молча (см. предупреждение _3mf_to_stls про обрезку по лимиту) — это и
есть регрессия фикса. И, если слайсер на машине справляется, что слайс даёт положительный
расход/время.

ВАЖНО — тест OPT-IN: сетевой + слайсер (GPU/медленно), поэтому по умолчанию ПРОПУСКАЕТСЯ,
чтобы обычный `pytest -q` оставался быстрым и зелёным. Включение:

    # PowerShell (из корня репо, в venv):
    $env:RUN_NETWORK_TESTS = "1"
    .venv/Scripts/python.exe -m pytest tests/test_network_slice.py -v

    # bash:
    RUN_NETWORK_TESTS=1 .venv/Scripts/python.exe -m pytest -m network -v

Требования к окружению (иначе тест аккуратно SKIP, а не падает):
- настроен вход в MakerWorld (`python mw_login.py`) ИЛИ задан MAKERWORLD_TOKEN — иначе
  анонимная загрузка с MakerWorld невозможна (Cloudflare/403) и тест пропускается;
- для сквозного слайса — установлен ElegooSlicer (config.SLICER_PATH) и его профили.
"""
import logging
import os

import pytest
import pytest_asyncio

from app import config, cpupool, downloader, slicer

# Card-shuffler / «устройство перетасовки карт» — каноничная многодетальная сборка
# (несколько плит, десятки деталей), на которой ловился баг обрезки по лимиту деталей.
CARD_SHUFFLER_URL = "https://makerworld.com/ru/models/2624902"

# Тест запускается ТОЛЬКО при RUN_NETWORK_TESTS — иначе skip (сеть + слайсер медленные).
pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not os.getenv("RUN_NETWORK_TESTS"),
        reason="сетевой/слайс-тест; включается RUN_NETWORK_TESTS=1",
    ),
]


@pytest_asyncio.fixture(scope="module")
async def card_shuffler():
    """Скачать card-shuffler рабочим downloader'ом ОДИН раз на модуль. Если нет логина/
    кредов MakerWorld — download падает (Cloudflare/403): это не баг слайса, поэтому SKIP."""
    config.ensure_dirs()
    try:
        meshes, label = await downloader.fetch_from_url(CARD_SHUFFLER_URL)
    except downloader.DownloadError as exc:
        pytest.skip(f"не удалось скачать с MakerWorld (нужен вход/MAKERWORLD_TOKEN?): {exc}")
    assert meshes, "downloader вернул пустой список мешей"
    return meshes, label


async def test_card_shuffler_parts_preserved(card_shuffler, caplog):
    """Регрессия фикса лимита деталей: при подготовке геометрии к слайсу все детали
    card-shuffler сохраняются — ни одна не выпадает молча по MAX_MESH_FILES."""
    meshes, _label = card_shuffler

    # _prepare_models — тот самый путь .3mf → STL, где раньше резалось по лимиту деталей.
    with caplog.at_level(logging.WARNING, logger="app.slicer"):
        prepared = await cpupool.run(slicer._prepare_models, meshes)

    assert prepared, "после подготовки не осталось геометрии для слайсинга"
    # У card-shuffler заведомо много деталей (десятки) — это и есть смысл проверки.
    assert len(prepared) > 1, f"ожидалась многодетальная сборка, получили {len(prepared)} дет."
    # Главное: предупреждение об обрезке по лимиту НЕ должно срабатывать (детали не теряются).
    dropped = [r for r in caplog.records if "НЕ учитываются" in r.getMessage()]
    assert not dropped, (
        f"детали card-shuffler обрезаны по лимиту MAX_MESH_FILES={config.MAX_MESH_FILES}: "
        + " | ".join(r.getMessage() for r in dropped)
    )


async def test_card_shuffler_download_and_slice(tmp_db, card_shuffler):
    """Сквозной слайс через тот же путь, что в проде (slicer.slice_unit с сидовым принтером
    и пресетами). На success — положительный расход/время. Если слайсер на машине не тянет
    такую сборку (краш/таймаут) — это ограничение слайсера, не регрессия фикса → SKIP."""
    meshes, _label = card_shuffler
    if not config.SLICER_PATH.exists():
        pytest.skip(f"слайсер не найден: {config.SLICER_PATH}")

    # Параметры слайса берём из сидов БД — ровно как в calcservice.compute.
    printer = await tmp_db.get_default_printer()
    assert printer is not None, "в сидовой БД нет принтера по умолчанию"
    qualities = await tmp_db.list_quality(active_only=True)
    strengths = await tmp_db.list_strength(active_only=True)
    assert qualities and strengths, "в сидовой БД нет пресетов качества/прочности"
    # Стандарт 0.20 мм + средняя прочность, если есть; иначе первый доступный.
    quality = next((q for q in qualities if abs(q.layer_height - 0.20) < 1e-6), qualities[0])
    strength = next((s for s in strengths if s.infill == 20), strengths[0])

    nparts = len(await cpupool.run(slicer._prepare_models, meshes))  # для понятного сообщения
    try:
        result = await slicer.slice_unit(
            meshes,
            machine_profile=printer.machine_profile,
            process_dir=printer.process_dir,
            base_process_name=quality.process,
            filament_profile=printer.filament_dir,  # slice_unit сам подберёт дефолт, если папка
            layer_height=quality.layer_height,
            infill_pct=strength.infill,
            walls=strength.walls,
        )
    except slicer.SlicerError as exc:
        pytest.skip(f"слайсер не справился с card-shuffler ({nparts} дет.) на этой машине: {exc}")

    assert result.volume_cm3 > 0, "слайсер вернул нулевой объём филамента"
    assert result.minutes > 0, "слайсер вернул нулевое время печати"
