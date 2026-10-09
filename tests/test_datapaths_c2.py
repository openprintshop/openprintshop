"""C2: отвязка data/ от захардкоженного пути — помощники, сквозные смоки и окружение.

Что здесь закреплено:

* ``config.rel_data``/``config.abs_data``/``config.abs_str``/``config.under_data_dir`` —
  включая идемпотентность (значение можно прогнать через них дважды), сохранение чужих
  путей дословно и страховку «перекоренить по известному сегменту», которая ОБЯЗАНА
  срабатывать только когда перекоренённый файл реально существует;
* хэш модели не зависит от формы пути (иначе задвоение коллекции и промах кэша расчётов);
* суточная чистка видит относительные ссылки БД (иначе она удалит живые превью);
* сквозные write→read: превью и файлы модели, фото пластика, фото готовой печати —
  в БД лежит относительное, наружу приходит абсолютное и существующее;
* override DATA_DIR через окружение (в subprocess: config читается на импорте) — на
  ЗАГЛУШКЕ проекта со своим .env, чтобы результат не зависел от .env владельца;
* инструменты в tools/ (vision_tagger читает пути, vk_board_import пишет) — у них свои
  локальные зеркала помощников, и они обязаны совпадать с config по контракту.

Изоляция: тесты, которым нужен свой DATA_DIR, подменяют его ЯВНО (вместе с зависимыми
модульными константами вроде COLLECTION_DIR) — автоюзной подмены в conftest сознательно нет,
иначе поплыли бы полтора десятка чужих ассертов, зелёных ровно потому, что их каталоги лежат
ВНЕ DATA_DIR.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import cache, config, maintenance

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Свой DATA_DIR + производные каталоги, которые трогают тесты ниже."""
    d = tmp_path / "studio data"
    (d / "previews").mkdir(parents=True)
    (d / "collection_photos").mkdir()
    (d / "spool_photos").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "PREVIEW_DIR", d / "previews")
    monkeypatch.setattr(config, "COLLECTION_DIR", d / "collection_photos")
    return d


# ─────────────────────────────── помощники rel/abs ───────────────────────────────
def test_rel_data_relativizes_only_under_data_dir(data_dir):
    p = data_dir / "previews" / "prev_1.png"
    assert config.rel_data(p) == "previews/prev_1.png"
    assert config.rel_data(str(p)) == "previews/prev_1.png"
    # чужой путь — дословно, без единой правки
    alien = r"C:\Program Files\ElegooSlicer\resources\profiles\Elegoo\x.json"
    assert config.rel_data(alien) == alien
    assert config.rel_data("/tmp/a.jpg") == "/tmp/a.jpg"
    # пустое остаётся пустым, None — None (очистка поля обязана остаться очисткой)
    assert config.rel_data(None) is None
    assert config.rel_data("") == ""


def test_rel_data_is_idempotent(data_dir):
    p = data_dir / "collection_photos" / "done_7.jpg"
    once = config.rel_data(p)
    assert config.rel_data(once) == once
    assert config.rel_data(config.abs_data(once)) == once


def test_abs_data_expands_relative_and_keeps_alien(data_dir):
    assert config.abs_data("previews/x.png") == data_dir / "previews" / "x.png"
    p = data_dir / "previews" / "x.png"
    assert config.abs_data(str(p)) == p
    assert config.abs_str("/tmp/a.jpg") == "/tmp/a.jpg", "чужой путь не должен менять вид"
    assert config.abs_str(None) is None and config.abs_str("") == ""


def test_abs_data_reroots_foreign_absolute_path_when_file_exists(data_dir):
    """БД, восстановленная на другой машине: путь чужой, но файл лежит в НАШЕМ data/."""
    real = data_dir / "previews" / "moved.png"
    real.write_bytes(b"x")
    foreign = r"D:\OldStudio\print_cost_bot\data\previews\moved.png"
    assert config.abs_data(foreign) == real


def test_abs_data_does_not_reroot_when_target_missing(data_dir, tmp_path):
    """Ложное срабатывание: чужой временный каталог с именем известного сегмента.

    Без условия «перекоренённый файл существует» такой путь увёл бы в боевой data/.
    """
    alien = tmp_path / "collection_photos" / "chat_1.jpg"
    alien.parent.mkdir(parents=True)
    assert config.abs_data(str(alien)) == alien          # файла нет и в data/ — отдаём как есть
    alien.write_bytes(b"x")
    assert config.abs_data(str(alien)) == alien          # существует у себя — тем более


def test_helpers_follow_monkeypatched_data_dir(tmp_path, monkeypatch):
    """DATA_DIR читается НА ВЫЗОВЕ: «запечь» его в константу нельзя — сломаются и тесты
    с monkeypatch, и override из окружения."""
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(config, "DATA_DIR", a)
    assert config.rel_data(a / "previews" / "x.png") == "previews/x.png"
    monkeypatch.setattr(config, "DATA_DIR", b)
    assert config.rel_data(a / "previews" / "x.png") == str(a / "previews" / "x.png")
    assert config.abs_data("previews/x.png") == b / "previews" / "x.png"


def test_under_data_dir_guard(data_dir, tmp_path):
    assert config.under_data_dir(data_dir / "previews" / "x.png")
    assert config.under_data_dir(config.abs_data("previews/x.png"))
    assert not config.under_data_dir(tmp_path / "elsewhere" / "x.png")
    assert not config.under_data_dir("")


# ─────────────────────────────── хэш модели ───────────────────────────────
def test_model_hash_same_for_relative_and_absolute(data_dir):
    """cache.model_hash на OSError хэширует СТРОКУ пути: разная форма пути дала бы разный
    хэш — задвоение коллекции и промах calc_cache, причём молча."""
    f = data_dir / "previews" / "model.3mf"
    f.write_bytes(b"solid")
    assert cache.model_hash([str(f)]) == cache.model_hash(["previews/model.3mf"])
    assert cache.make_key([str(f)], machine_profile="m", process="p", layer_height=0.2,
                          infill=15, walls=2, filament="f") == \
        cache.make_key(["previews/model.3mf"], machine_profile="m", process="p",
                       layer_height=0.2, infill=15, walls=2, filament="f")


# ─────────────────────────────── суточная чистка ───────────────────────────────
async def test_cleanup_keeps_files_referenced_by_relative_paths(data_dir, tmp_db):
    """САМОЕ ОПАСНОЕ МЕСТО C2: _referenced_paths сравнивает ссылки БД с абсолютными путями
    файлов при обходе каталогов. Без abs_data ни одна ссылка не совпала бы, и свип снёс бы
    живые превью коллекции."""
    keep = data_dir / "previews" / "keep.png"
    keep.write_bytes(b"png")
    model = data_dir / "previews" / "keep.3mf"
    model.write_bytes(b"3mf")
    await tmp_db.add_to_collection(hash="h1", label="Кубик", paths=[str(model)],
                                   preview_path=str(keep), added_by=1)
    refs = await maintenance._referenced_paths()
    models = await maintenance._referenced_model_paths()
    assert str(keep) in refs, "ссылка на превью потеряна — свип удалит живой файл"
    assert str(model) in models, "ссылка на файл модели потеряна"


# ─────────────────────────────── сквозные write → read ───────────────────────────────
async def _raw(db, sql, *args):
    cur = await db._conn().execute(sql, args)
    row = await cur.fetchone()
    return row[0] if row else None


async def test_roundtrip_saved_model_and_collection(data_dir, tmp_db):
    prev = data_dir / "previews" / "prev.png"
    prev.write_bytes(b"png")
    model = data_dir / "previews" / "m.3mf"
    model.write_bytes(b"3mf")
    await tmp_db.upsert_saved_model(user_id=7, hash="hh", label="М", paths=[str(model)],
                                    preview_path=str(prev), printer_id=None, plastic_id=None,
                                    quality_id=None, strength_id=None, quantity=1)
    assert await _raw(tmp_db, "SELECT preview_path FROM saved_models WHERE hash='hh'") \
        == "previews/prev.png"
    assert await _raw(tmp_db, "SELECT paths FROM saved_models WHERE hash='hh'") \
        == '["previews/m.3mf"]'
    sm = (await tmp_db.list_saved_models(7))[0]
    assert sm.preview_path == str(prev) and Path(sm.preview_path).exists()
    assert sm.paths == [str(model)] and Path(sm.paths[0]).exists()


async def test_roundtrip_plastic_photo(data_dir, tmp_db):
    photo = data_dir / "spool_photos" / "pla.jpg"
    photo.write_bytes(b"jpg")
    pid = await tmp_db.add_plastic("PLA C2", 20.0, 1.24)
    await tmp_db.update_plastic(pid, photo_path=str(photo))
    assert await _raw(tmp_db, "SELECT photo_path FROM plastics WHERE id=?", pid) \
        == "spool_photos/pla.jpg"
    p = await tmp_db.get_plastic(pid)
    assert p.photo_path == str(photo) and Path(p.photo_path).exists()


async def test_roundtrip_order_done_photo_and_journal(data_dir, tmp_db):
    done = data_dir / "collection_photos" / "done_1.jpg"
    done.write_bytes(b"jpg")
    rid = await tmp_db.add_request(
        user_id=1, user_name="t", model_label="m", printer="p", plastic_name="x", quantity=1,
        quality="q", strength="s", grams_total=1, minutes_total=1, price=1,
        preview_path=str(done), file_path=str(done))
    await tmp_db.update_request(rid, done_photo=str(done))
    assert await _raw(tmp_db, "SELECT done_photo FROM requests WHERE id=?", rid) \
        == "collection_photos/done_1.jpg"
    assert await _raw(tmp_db, "SELECT file_path FROM requests WHERE id=?", rid) \
        == "collection_photos/done_1.jpg"
    r = await tmp_db.get_request(rid)
    assert r.done_photo == str(done) and Path(r.done_photo).exists()
    assert r.file_path == str(done) and r.preview_path == str(done)
    # очистка поля остаётся очисткой (rel_data не превращает None в "")
    await tmp_db.update_request(rid, done_photo=None)
    assert (await tmp_db.get_request(rid)).done_photo is None

    jid = await tmp_db.add_print_journal(label="j", plastic_id=None, plastic_name="x",
                                         grams=1, minutes=1, timelapse=str(done))
    await tmp_db.set_print_journal_fields(jid, done_photo=str(done))
    assert await _raw(tmp_db, "SELECT timelapse FROM print_journal WHERE id=?", jid) \
        == "collection_photos/done_1.jpg"
    e = await tmp_db.get_print_journal(jid)
    assert e["done_photo"] == str(done) and e["timelapse"] == str(done)


async def test_photo_url_survives_relative_value_in_db(data_dir, tmp_db):
    """Тихая деградация: _photo_url отдаёт None, если Path(значение).exists() ложь —
    картинка пропадает у клиента и мастера БЕЗ ошибки в логе."""
    from app.web import common

    photo = data_dir / "collection_photos" / "col.jpg"
    photo.write_bytes(b"jpg")
    cid = await tmp_db.add_to_collection(hash="h9", label="К", paths=[], preview_path=None,
                                         added_by=1)
    await tmp_db.set_collection_photo(cid, str(photo))
    stored = await _raw(tmp_db, "SELECT photo_path FROM collection WHERE id=?", cid)
    assert stored == "collection_photos/col.jpg"
    assert common._photo_url("collection", cid, stored), "URL пуст — картинка исчезнет молча"
    assert common._file_ver(stored) > 0, "версия файла 0 — браузер залипнет на старой картинке"
    assert common._file_ver(None) == 0 and common._file_ver("") == 0


def test_selfcheck_reports_unwritable_data_dir(tmp_path, monkeypatch):
    """DATA_DIR из .env может указывать на несмонтированный том — бот тогда молча теряет
    базу, логи и фото. Проверка НЕ создаёт пробных файлов: сорить в боевом каталоге
    (а run_checks зовётся на каждом открытии статуса) незачем."""
    from app import selfcheck

    ok = tmp_path / "ok"
    monkeypatch.setattr(config, "DATA_DIR", ok)
    assert selfcheck.data_dir_writable() and ok.is_dir()
    assert not any(ok.iterdir()), "проверка не должна оставлять файлов в каталоге данных"

    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="ascii")
    monkeypatch.setattr(config, "DATA_DIR", blocker / "data")   # каталог поверх файла
    assert not selfcheck.data_dir_writable()


# ─────────────────────────────── окружение ───────────────────────────────
_DUMP_DIRS = textwrap.dedent("""
    import json
    from app import config
    print(json.dumps({
        "data": str(config.DATA_DIR), "db": str(config.DB_PATH),
        "prev": str(config.PREVIEW_DIR), "logs": str(config.LOG_DIR),
        "backups": str(config.BACKUP_DIR), "coll": str(config.COLLECTION_DIR)}))
""")


def _stub_project(tmp_path, env_text: str):
    """Мини-«репозиторий»: копия app/config.py + НАШ .env (и больше ничего).

    Тесты окружения обязаны быть независимы от .env владельца — там строки DATA_DIR нет
    вовсе, и промах вида «пустое ``DATA_DIR=`` из .env.example гасит переменную окружения»
    на его машине прошёл бы незамеченным, а на свежей установке сломал бы всё."""
    root = tmp_path / "stub_repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "config.py").write_bytes((REPO / "app" / "config.py").read_bytes())
    (root / ".env").write_text(env_text, encoding="utf-8")
    return root


def _run_config(root, data_dir_env=None, code=_DUMP_DIRS):
    """Импорт config отдельным процессом из корня ``root`` (sys.path[0] = cwd)."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "DATA_DIR"}
    env.pop("PYTHONPATH", None)          # чтобы боевой app/ не подменил заглушку
    if data_dir_env is not None:
        env["DATA_DIR"] = str(data_dir_env)
    return subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)


def _dirs(out):
    import json
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_data_dir_env_override_in_subprocess(tmp_path):
    """DATA_DIR читается на импорте config, поэтому проверяем отдельным процессом: все
    подкаталоги обязаны поехать за корнем."""
    root = _stub_project(tmp_path, "BOT_TOKEN=1:x\n")
    target = tmp_path / "elsewhere data"
    got = _dirs(_run_config(root, target))
    assert got["data"] == str(target)
    for key in ("db", "prev", "logs", "backups", "coll"):
        assert got[key].startswith(str(target)), f"{key} не поехал за DATA_DIR: {got[key]}"


def test_env_example_does_not_clobber_data_dir_from_environment(tmp_path):
    """Регрессия: .env грузится с override=True, поэтому НЕЗАКОММЕНТИРОВАННАЯ пустая
    строка ``DATA_DIR=`` в .env.example затирала бы переменную окружения, и свежая
    установка молча уезжала бы в <папка бота>/data. Проверяем на настоящем .env.example
    (install.ps1 копирует именно его)."""
    example = (REPO / ".env.example").read_text(encoding="utf-8-sig")
    root = _stub_project(tmp_path, example)
    target = tmp_path / "elsewhere data"
    assert _dirs(_run_config(root, target))["data"] == str(target)


def test_env_file_beats_environment(tmp_path):
    """Обратная сторона override=True: заданный в .env путь ПОБЕЖДАЕТ окружение. Это
    задокументировано в docstring config и .env.example — фиксируем контракт."""
    chosen = tmp_path / "from dotenv"
    root = _stub_project(tmp_path, f"DATA_DIR={chosen}\n")
    assert _dirs(_run_config(root, tmp_path / "from env"))["data"] == str(chosen)


def test_data_dir_absent_everywhere_falls_back_to_base_dir(tmp_path):
    """Ни в .env, ни в окружении — прежнее поведение: <папка бота>/data."""
    root = _stub_project(tmp_path, "BOT_TOKEN=1:x\n")
    assert _dirs(_run_config(root))["data"] == str(root / "data")


def test_relative_data_dir_is_rejected(tmp_path):
    """Относительный DATA_DIR — это «почти правильный» префикс: миграция промахнётся, а
    бот запишет данные мимо. Падаем понятно и сразу."""
    root = _stub_project(tmp_path, "BOT_TOKEN=1:x\n")
    out = _run_config(root, "some/relative", code="from app import config")
    assert out.returncode != 0 and "DATA_DIR" in out.stderr


# ─────────────────────────────── инструменты в tools/ ───────────────────────────────
def _load_tool(name: str):
    """tools/*.py — не пакет: грузим по файлу. У них СВОЙ парсер DATA_DIR (их запускают
    отдельным python без venv бота), поэтому в тестах подменяем модульную константу."""
    import importlib.util
    path = REPO / "tools" / f"{name}.py"
    if not path.exists():           # личный инструмент, которого нет в открытой версии проекта
        pytest.skip(f"tools/{name}.py нет в этой сборке")
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vision_tagger_reads_relative_paths(tmp_path, monkeypatch):
    """После миграции v8 в collection лежит 'previews/x.png'. Инструмент обязан развернуть
    его от DATA_DIR: иначе он МОЛЧА (веткой «нет картинки») пропустит весь каталог."""
    mod = _load_tool("vision_tagger")
    d = tmp_path / "studio data"
    (d / "previews").mkdir(parents=True)
    (d / "previews" / "p.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(mod, "DATA_DIR", d)

    assert mod._abs("previews/p.png") == d / "previews" / "p.png"
    assert mod._abs(None) is None and mod._abs("") is None
    assert mod._abs(r"C:\elsewhere\a.png") == Path(r"C:\elsewhere\a.png")   # чужой — как есть

    assert mod.pick_image({"photo_path": "", "preview_path": "previews/p.png"}) == \
        d / "previews" / "p.png"
    # фото важнее превью, а отсутствующий на диске файл — это «нет картинки»
    assert mod.pick_image({"photo_path": "previews/p.png", "preview_path": ""}) is not None
    assert mod.pick_image({"photo_path": "", "preview_path": "previews/gone.png"}) is None
    assert mod.pick_image({"photo_path": None, "preview_path": None}) is None


def test_vk_import_writes_relative_paths(tmp_path, monkeypatch):
    """Импортёр — источник почти всех путевых элементов каталога, а миграция v8 разовая:
    писать он обязан относительным, иначе новые записи снова привязаны к машине."""
    mod = _load_tool("vk_board_import")
    d = tmp_path / "studio data"
    (d / "vk_catalog").mkdir(parents=True)
    monkeypatch.setattr(mod, "DATA_DIR", d)

    assert mod._rel(d / "vk_catalog" / "1" / "preview.jpg") == "vk_catalog/1/preview.jpg"
    assert mod._rel(str(d / "vk_catalog" / "m.stl")) == "vk_catalog/m.stl"
    assert mod._rel("vk_catalog/1/preview.jpg") == "vk_catalog/1/preview.jpg"   # идемпотентно
    assert mod._rel(Path(r"C:\elsewhere\a.stl")) == r"C:\elsewhere\a.stl"       # чужой — как есть
    assert mod._rel(None) == "" and mod._rel("") == ""


def test_tool_helpers_match_config_contract(tmp_path, monkeypatch):
    """Локальные зеркала в tools/ обязаны давать то же, что config.rel_data/abs_data —
    иначе инструмент и бот разойдутся в форме хранения путей."""
    vis, vk = _load_tool("vision_tagger"), _load_tool("vk_board_import")
    d = tmp_path / "studio data"
    (d / "previews").mkdir(parents=True)
    for mod in (vis, vk):
        monkeypatch.setattr(mod, "DATA_DIR", d)
    monkeypatch.setattr(config, "DATA_DIR", d)

    p = d / "previews" / "x.png"
    assert vk._rel(p) == config.rel_data(p)
    assert vis._abs("previews/x.png") == config.abs_data("previews/x.png")
