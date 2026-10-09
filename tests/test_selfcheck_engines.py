"""Selfcheck знает про выбор слайсера (остаток варианта B из orcaslicer-fork-adr).

Пропавший exe движка карточки НЕ роняет расчёт: calcservice ловит SlicerError и молча отдаёт
приблизительную оценку — поломка выглядит как «цена стала другой», а не как ошибка. Поэтому
run_checks обязан сказать об этом вслух при старте. Движок по умолчанию без override не
дублируется — его проверяет первая строка (config.SLICER_PATH).
"""
from app import selfcheck


async def test_missing_engine_of_card_is_loud(tmp_db):
    ps = await tmp_db.list_printers(active_only=True)
    assert ps, "в tmp_db нет принтера — фикстура изменилась"
    pid = ps[0].id
    await tmp_db.update_printer(pid, slicer="orca",
                                slicer_path=r"C:\нет\такого\orca-slicer.exe")
    issues = await selfcheck.run_checks()
    assert any("Orca Slicer не найден" in i for i in issues), issues


async def test_existing_engine_is_quiet(tmp_db, tmp_path):
    ps = await tmp_db.list_printers(active_only=True)
    pid = ps[0].id
    exe = tmp_path / "orca-slicer.exe"
    exe.write_bytes(b"MZ")
    await tmp_db.update_printer(pid, slicer="orca", slicer_path=str(exe))
    issues = await selfcheck.run_checks()
    assert not any("Orca Slicer" in i for i in issues), issues


async def test_default_engine_not_duplicated(tmp_db):
    """Карточка на движке по умолчанию без override: про слайсер говорит только строка
    config.SLICER_PATH — второй жалобы на ту же пропажу быть не должно."""
    ps = await tmp_db.list_printers(active_only=True)
    pid = ps[0].id
    await tmp_db.update_printer(pid, slicer="", slicer_path="")
    issues = await selfcheck.run_checks()
    assert not any("ElegooSlicer не найден" in i for i in issues), issues
