"""B1 (3D-просмотр): эндпоинт /api/modelfile/{token} отдаёт СЫРОЙ файл модели клиентскому
three.js-вьюеру. Файл — оригинал авторской модели, поэтому авторизация строже превью:
токен-uuid4 (capability) + сверка initData→uid == m['uid']. Проверяем весь контракт:
  • валидный токен своего владельца → 200 + Content-Type по расширению;
  • чужой uid → 403; нет/протух токен, файла нет → 404;
  • .step/.stp и слишком тяжёлый меш НЕ отдаём (гейт как у model_viewable) → 404;
  • токен — ключ словаря, а не путь: обхода каталога через «../» нет.
"""
import pytest
from aiohttp import web
from conftest import stub_auth

from app import config, webapp
from app.web import mesh3d

USER_ID = 501
OTHER_ID = 777


class _FakeRequest:
    """Мини-заглушка aiohttp-запроса: хендлеру нужны только match_info + query."""
    def __init__(self, token, init_data="good", query=None):
        self.match_info = {"token": token}
        self.query = {"initData": init_data}
        if query:
            self.query.update(query)
        self.headers = {}


@pytest.fixture(autouse=True)
def _auth_and_models(monkeypatch):
    # initData → uid: "good" = владелец, "other" = другой клиент, "" / прочее = не авторизован
    stub_auth(monkeypatch,
              _verify_init_data=lambda init_data: {"good": USER_ID, "other": OTHER_ID}.get(init_data))
    webapp._models.clear()
    yield
    webapp._models.clear()


def _seed(token, path, uid=USER_ID):
    webapp._models[token] = {"paths": [str(path)], "uid": uid, "label": "M"}


async def _call(token, init_data="good"):
    return await webapp._api_modelfile(_FakeRequest(token, init_data))


async def test_valid_owner_served_extracted_glb(tmp_path, monkeypatch):
    # эндпоинт отдаёт НЕ сырой файл, а извлечённый сервером бинарный GLB со сценой из деталей
    # (webapp._model3d_glb_bytes) — мокаем извлечение, проверяем 200 + Content-Type + тело.
    f = tmp_path / "model.stl"
    f.write_bytes(b"solid test\nendsolid test\n")
    _seed("tok-stl", f)
    monkeypatch.setattr(webapp, "_model3d_glb_bytes", lambda paths, *a, **kw: b"glTF-FAKE-BINARY")
    monkeypatch.setattr(mesh3d, "_model3d_glb_bytes", lambda paths, *a, **kw: b"glTF-FAKE-BINARY")
    resp = await _call("tok-stl")
    assert isinstance(resp, web.Response)
    assert resp.headers["Content-Type"] == "model/gltf-binary"
    assert resp.body == b"glTF-FAKE-BINARY"


async def test_all_formats_served_as_glb(tmp_path, monkeypatch):
    # ЛЮБОЙ формат (в т.ч. Bambu/MakerWorld .3mf, на котором клиентский 3MFLoader спотыкался) сервер
    # отдаёт как GLB со сценой из деталей — клиент рисует одним надёжным GLTFLoader'ом и красит части.
    a = tmp_path / "m.3mf"; a.write_bytes(b"PK\x03\x04rest")
    b = tmp_path / "m.obj"; b.write_text("o cube\nv 0 0 0\n")
    _seed("t3mf", a); _seed("tobj", b)
    monkeypatch.setattr(webapp, "_model3d_glb_bytes", lambda paths, *a, **kw: b"glTF")
    monkeypatch.setattr(mesh3d, "_model3d_glb_bytes", lambda paths, *a, **kw: b"glTF")
    r3 = await _call("t3mf")
    ro = await _call("tobj")
    assert r3.headers["Content-Type"] == "model/gltf-binary"
    assert ro.headers["Content-Type"] == "model/gltf-binary"


async def test_unparseable_mesh_not_found(tmp_path, monkeypatch):
    # меш не разобрался (битый файл) → 404, мини-апп тихо остаётся на PNG
    f = tmp_path / "broken.3mf"; f.write_bytes(b"not a real 3mf")
    _seed("tok-broken", f)
    monkeypatch.setattr(webapp, "_model3d_glb_bytes", lambda paths, *a, **kw: None)
    monkeypatch.setattr(mesh3d, "_model3d_glb_bytes", lambda paths, *a, **kw: None)
    with pytest.raises(web.HTTPNotFound):
        await _call("tok-broken")


async def test_wrong_uid_forbidden(tmp_path):
    f = tmp_path / "model.stl"; f.write_bytes(b"solid x\n")
    _seed("tok", f, uid=USER_ID)
    with pytest.raises(web.HTTPForbidden):
        await _call("tok", init_data="other")   # чужой клиент — оригинал не отдаём


async def test_missing_initdata_forbidden(tmp_path):
    f = tmp_path / "model.stl"; f.write_bytes(b"solid x\n")
    _seed("tok", f)
    with pytest.raises(web.HTTPForbidden):
        await _call("tok", init_data="")         # подпись не прошла


async def test_unknown_or_evicted_token_not_found():
    with pytest.raises(web.HTTPNotFound):
        await _call("nope")                       # токена нет в _models (вытеснен по _MODELS_MAX)


async def test_file_gone_not_found(tmp_path):
    _seed("tok", tmp_path / "vanished.stl")       # путь есть, файла на диске нет
    with pytest.raises(web.HTTPNotFound):
        await _call("tok")


async def test_step_format_not_served(tmp_path):
    f = tmp_path / "part.step"; f.write_text("ISO-10303-21;\n")
    _seed("tok-step", f)
    with pytest.raises(web.HTTPNotFound):          # three.js не парсит STEP → PNG-фолбэк
        await _call("tok-step")


async def test_oversize_mesh_not_served(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_MODEL3D_MAX_SRC_BYTES", 16)
    monkeypatch.setattr(mesh3d, "_MODEL3D_MAX_SRC_BYTES", 16)
    f = tmp_path / "big.stl"; f.write_bytes(b"solid " + b"x" * 64)
    _seed("tok-big", f)
    with pytest.raises(web.HTTPNotFound):          # неподъёмный исходник → серверу не разбирать
        await _call("tok-big")


async def test_no_path_traversal(tmp_path):
    # токен используется как ключ словаря, а не как путь: «../» просто не найдётся → 404,
    # никакого чтения файла вне зарегистрированных paths.
    f = tmp_path / "model.stl"; f.write_bytes(b"solid x\n")
    _seed("tok", f)
    for evil in ("../../etc/passwd", "..\\..\\secret", "tok/../tok"):
        with pytest.raises(web.HTTPNotFound):
            await _call(evil)


# ── _model3d_glb_bytes: серверная сборка GLB-сцены из деталей (для мультицвета на клиенте) ──────
# Настоящий разбор + trimesh-экспорт: 2-детальная модель → непустой GLB, который читается обратно
# и содержит ≥1 геометрию. Именно детали (part0..N) нужны фронту для покраски каждой части.

def test_glb_bytes_two_part_scene_roundtrips(tmp_path, monkeypatch):
    # кэш готового GLB — в tmp_path, иначе синтетическая сцена оседает в БОЕВОМ data/cache/geo
    # (ключ = хэш содержимого временного файла, и .3mf-экспорт trimesh кладёт внутрь свежий uuid
    # → каждый прогон pytest минтил НОВЫЙ файл в рабочей папке студии)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    trimesh = pytest.importorskip("trimesh")
    b1 = trimesh.creation.box(extents=(10, 10, 10))
    b2 = trimesh.creation.box(extents=(6, 6, 20)); b2.apply_translation((20, 0, 0))
    src = trimesh.Scene(); src.add_geometry(b1, node_name="a"); src.add_geometry(b2, node_name="b")
    f = tmp_path / "twobox.3mf"; src.export(str(f))

    data = webapp._model3d_glb_bytes([str(f)])
    assert data and data[:4] == b"glTF"                 # валидный бинарный glTF (GLB-магия)
    loaded = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb")
    n_geom = len(loaded.geometry) if hasattr(loaded, "geometry") else 1
    assert n_geom >= 1                                  # хотя бы одна деталь дошла в сцену
    # .3mf сохраняет отдельные детали → минимум две части с ожидаемыми именами part0/part1
    if hasattr(loaded, "geometry"):
        assert n_geom >= 2
        assert "part0" in loaded.geometry and "part1" in loaded.geometry


def test_glb_bytes_bad_paths_return_none(tmp_path, monkeypatch):
    # битый/непарсибельный файл → None (мини-апп остаётся на PNG), без исключения наружу
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")   # не трогать боевой кэш (mkdir)
    f = tmp_path / "broken.3mf"; f.write_bytes(b"not a real 3mf")
    assert webapp._model3d_glb_bytes([str(f)]) is None
    assert webapp._model3d_glb_bytes([]) is None


# ── _model3d_meta: гейт «показывать 3D», который решает клиент БЕЗ скачивания меша ──────────
# Если model_viewable=false — мини-апп не рисует кнопку «3D» и остаётся на PNG-превью.
# Это вторая половина контракта B1 (первая — отдача файла эндпоинтом выше).

def test_meta_viewable_stl_within_size(tmp_path):
    f = tmp_path / "ok.stl"; f.write_bytes(b"solid x\nendsolid x\n")
    meta = webapp._model3d_meta([str(f)])
    assert meta["model_ext"] == ".stl"
    assert meta["model_bytes"] == f.stat().st_size
    assert meta["model_viewable"] is True


def test_meta_step_not_viewable(tmp_path):
    # .step/.stp three.js не парсит → 3D не предлагаем, клиент держит PNG
    f = tmp_path / "part.step"; f.write_text("ISO-10303-21;\n")
    assert webapp._model3d_meta([str(f)])["model_viewable"] is False


def test_meta_oversize_not_viewable(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_MODEL3D_MAX_SRC_BYTES", 8)
    monkeypatch.setattr(mesh3d, "_MODEL3D_MAX_SRC_BYTES", 8)
    f = tmp_path / "big.stl"; f.write_bytes(b"solid " + b"x" * 64)
    assert webapp._model3d_meta([str(f)])["model_viewable"] is False


def test_meta_multifile_set_sums_sizes(tmp_path, monkeypatch):
    """Набор из нескольких файлов (кейс «Флекси Дарт Вейдер», c23def0d): гейт считает СУММУ —
    GLB собирается из всего набора. По первому файлу набор и резало (38 МБ > старого капа 30),
    и пропускало бы неподъёмные наборы из мелких файлов."""
    a = tmp_path / "a.stl"; a.write_bytes(b"x" * 10)
    b = tmp_path / "b.stl"; b.write_bytes(b"y" * 10)
    monkeypatch.setattr(webapp, "_MODEL3D_MAX_SRC_BYTES", 25)
    monkeypatch.setattr(mesh3d, "_MODEL3D_MAX_SRC_BYTES", 25)
    meta = webapp._model3d_meta([str(a), str(b)])
    assert meta["model_bytes"] == 20 and meta["model_viewable"] is True
    monkeypatch.setattr(webapp, "_MODEL3D_MAX_SRC_BYTES", 15)
    monkeypatch.setattr(mesh3d, "_MODEL3D_MAX_SRC_BYTES", 15)   # каждый по-отдельности ≤ 15
    assert webapp._model3d_meta([str(a), str(b)])["model_viewable"] is False


def test_meta_missing_file_not_viewable(tmp_path):
    # файла нет на диске → size 0 → не viewable (гейт «0 < size» защищает от пустышки)
    meta = webapp._model3d_meta([str(tmp_path / "gone.stl")])
    assert meta["model_bytes"] == 0
    assert meta["model_viewable"] is False


def test_meta_empty_paths_not_viewable():
    # токен без путей (напр. только-что вытесненный/битый) → безопасный дефолт: не viewable
    meta = webapp._model3d_meta([])
    assert meta["model_ext"] == ""
    assert meta["model_viewable"] is False
