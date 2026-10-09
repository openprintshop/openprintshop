"""Редакция сборки: личные инструменты против продуктовых (OpenTodo f98aa1ef).

Главное, что защищают тесты, — НЕ логика включения, а два обещания:

* без явного EDITION=dev любая установка — студия (установщик сам делает git clone, так что
  .git ничего не говорит о машине);
* личного модуля нет в сборке — его фичу не включить ничем и не видно в настройках;
* выключенная возможность закрыта НА СЕРВЕРЕ, а не только спрятана в интерфейсе —
  иначе ручка осталась бы доступной по прямому адресу.
"""
import pytest

from app import config, edition, webapp


@pytest.fixture(autouse=True)
def _clean_overrides():
    """Переключатели владельца — глобальный словарь модуля: без сброса выбор одного теста
    протекал бы в следующие."""
    edition._overrides.clear()
    yield
    edition._overrides.clear()


def test_explicit_edition_wins(monkeypatch):
    monkeypatch.setattr(config, "EDITION", "studio")
    assert edition.edition() == edition.STUDIO
    monkeypatch.setattr(config, "EDITION", "dev")
    assert edition.edition() == edition.DEV


def test_unknown_value_is_studio(monkeypatch, tmp_path):
    """Мусор в .env — студия: безопасный дефолт, особые фичи не включаются сами собой."""
    monkeypatch.setattr(config, "EDITION", "полный")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    (tmp_path / ".git").mkdir()
    assert edition.edition() == edition.STUDIO


def test_git_checkout_is_not_dev(monkeypatch, tmp_path):
    """Установка открытой версии = git clone: .git есть у всех, это не признак разработчика."""
    monkeypatch.setattr(config, "EDITION", "")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    assert edition.edition() == edition.STUDIO
    (tmp_path / ".git").mkdir()
    assert edition.edition() == edition.STUDIO


def test_absent_private_module_cannot_be_enabled(monkeypatch, tmp_path):
    """В открытой версии нет файлов MakerWorld и планировщика: фичу не включает ни редакция
    разработчика, ни явный переключатель, и в настройках её нет."""
    monkeypatch.setattr(config, "EDITION", "dev")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)          # ни одного файла сборки
    edition.apply_settings({edition.setting_key("makerworld"): 1})
    assert edition.enabled("makerworld") is False
    assert edition.enabled("planner") is False
    keys = {f["key"] for f in edition.snapshot()}
    assert "makerworld" not in keys and "planner" not in keys
    assert "lcd" in keys                                        # обычная особая фича на месте


def test_selfupdate_works_in_studio(monkeypatch):
    """Установка — git clone, поэтому обновление нужно всем, а не только разработчику."""
    monkeypatch.setattr(config, "EDITION", "studio")
    assert edition.enabled("selfupdate") is True


def test_dev_build_has_everything(monkeypatch):
    monkeypatch.setattr(config, "EDITION", "dev")
    assert all(f["enabled"] for f in edition.snapshot())


def test_studio_build_hides_personal_tools(monkeypatch):
    monkeypatch.setattr(config, "EDITION", "studio")
    state = {f["key"]: f["enabled"] for f in edition.snapshot()}
    # в открытой версии MakerWorld нет вовсе — тогда его нет и в списке (это тоже «выключено»)
    assert state.get("makerworld", False) is False and state["gpu_avatars"] is False
    # Фича без dev_only (требует программу на машине, но не «моя личная») остаётся включённой.
    assert state["slicer_tools"] is True


def test_owner_override_beats_edition_both_ways(monkeypatch):
    monkeypatch.setattr(config, "EDITION", "studio")
    edition.apply_settings({edition.setting_key("parametric"): 1})
    assert edition.enabled("parametric") is True     # покупатель поставил OpenSCAD — включил

    monkeypatch.setattr(config, "EDITION", "dev")
    edition.apply_settings({edition.setting_key("lcd"): 0})
    assert edition.enabled("lcd") is False           # своя фича, но мешает — выключил


@pytest.mark.parametrize("raw,expect", [
    # db.get_settings() приводит ВСЮ таблицу настроек к float: выключенный переключатель
    # приезжает как 0.0, и строковое сравнение с «0» его не ловило — «выключить» молча
    # не срабатывало. Поймано живьём на /api/parametric, поэтому форма закреплена тестом.
    (0.0, False), (1.0, True), (0, False), (1, True),
    ("0", False), ("1", True), ("off", False), ("true", True),
])
def test_switch_survives_numeric_form(monkeypatch, raw, expect):
    monkeypatch.setattr(config, "EDITION", "dev")
    edition.apply_settings({edition.setting_key("lcd"): raw})
    assert edition.enabled("lcd") is expect


def test_reset_returns_to_edition_default(monkeypatch):
    monkeypatch.setattr(config, "EDITION", "studio")
    edition.apply_settings({edition.setting_key("lcd"): 1})
    assert edition.enabled("lcd") is True
    edition.apply_settings({})                       # запись удалена = «как решает редакция»
    assert edition.enabled("lcd") is False


def test_unknown_feature_is_enabled():
    """Ключа нет в реестре → это обычная продуктовая фича, ограничивать её нечем."""
    assert edition.enabled("orders") is True


def test_routes_are_guarded_by_their_feature():
    assert edition.guard_key("/api/parametric/models") == "parametric"
    assert edition.guard_key("/api/admin/mw_accounts") == "makerworld"
    assert edition.guard_key("/api/lcd/data") == "lcd"
    # Продуктовые ручки не ограничены ничем — иначе выключение фичи ломало бы заказы.
    assert edition.guard_key("/api/admin/orders") == ""
    assert edition.guard_key("/api/options") == ""


def test_every_route_belongs_to_a_real_feature():
    """Карта закрытых ручек строится из реестра — второму списку взяться неоткуда."""
    for prefix, key in edition.ROUTE_GUARD:
        assert key in edition.FEATURES_BY_KEY
        assert prefix.startswith("/")


# Признаки принадлежности ручки фиче — СВОЙ список, намеренно НЕ выведенный из Feature.routes:
# иначе проверка ниже сверяла бы реестр сам с собой и молчала бы ровно в том случае, ради
# которого написана — когда ручку завели, а в реестр вписать забыли. Здесь куски пути, по
# которым человек и так узнаёт фичу; они шире префиксов реестра (подстрока, а не начало).
#
# Что СОЗНАТЕЛЬНО не помечено: /api/avatar/generate и /avatar/{uid} — процедурные аватарки
# остаются у всех и от видеокарты не зависят (фича закрывает только /local/avatar_gpu/*),
# а /slicershot/{rid} отдаёт уже сохранённую картинку и слайсера на машине не требует.
_FEATURE_ROUTE_MARKS: dict[str, tuple[str, ...]] = {
    "makerworld":   ("mw_account",),
    "model_search": ("model_search",),
    "gpu_avatars":  ("avatar_gpu",),
    "parametric":   ("parametric",),
    "lcd":          ("lcd", "screen-status", "screen-power", "screen_power"),
    "selfupdate":   ("update_check", "update_apply", "update_mode"),
    "slicer_tools": ("order/slice", "order/open_slicer"),
}


def test_no_feature_route_slips_past_the_registry():
    """Обратное направление: пройти по РЕАЛЬНОЙ таблице маршрутов и упасть на ручке, которая
    принадлежит фиче, но не закрыта ни одним префиксом реестра.

    Соседний test_routes_are_guarded_by_their_feature проверяет только «что записано — то и
    работает» и по построению не видит пропущенного. А пропуск здесь тихий и дорогой: в
    студийной сборке ручка выключенной фичи остаётся живой по прямому адресу — именно так
    /lcd, /api/screen-status и тумблер /api/admin/screen_power пережили появление реестра."""
    missed: list[str] = []
    for route in webapp.make_app().router.routes():
        if route.method == "HEAD":              # aiohttp заводит его сам к каждому GET
            continue
        path = route.resource.canonical
        for key, marks in sorted(_FEATURE_ROUTE_MARKS.items()):
            if not any(m in path for m in marks):
                continue
            got = edition.guard_key(path)
            if got != key:
                missed.append(f"    {route.method} {path} — ожидали «{key}», "
                              f"получили «{got or 'не закрыта вовсе'}»")
    assert not missed, ("ручки фич мимо реестра (допишите префикс в Feature.routes "
                        "в app/edition.py):\n" + "\n".join(missed))


def test_snapshot_explains_itself():
    """Экран настроек должен объяснять КАЖДУЮ фичу: без причины это просто тумблер."""
    for f in edition.snapshot():
        assert f["title"] and f["why"]
        assert isinstance(f["enabled"], bool) and isinstance(f["available"], bool)


def test_flags_cover_every_feature_with_explicit_false(monkeypatch, tmp_path):
    """Фронт считает неизвестный ключ включённым: личный модуль, которого нет в сборке, обязан
    приехать явным False, иначе его раздел нарисовался бы в открытой версии."""
    monkeypatch.setattr(config, "EDITION", "dev")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)          # файлов личных модулей нет
    flags = edition.flags()
    assert set(flags) == set(edition.FEATURES_BY_KEY)
    assert flags["makerworld"] is False and flags["planner"] is False
