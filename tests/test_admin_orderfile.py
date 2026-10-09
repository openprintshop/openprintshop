"""B1 (3D для мастера): эндпоинт /api/admin/orderfile отдаёт меш РАЗМЕЩЁННОГО заказа
бинарным GLB (сцена из деталей со слотами цветов — a3dff103) для 3D-вьюера ПК-админки.

Отличие от /api/modelfile (клиент): источник — ПЕРСИСТЕНТНЫЙ requests.file_path заказа, а не
transient-токен из _models. Авторизация — как у всех /api/admin/*: только админ. Проверяем:
  • админ + валидный 3D-файл → 200 + Content-Type model/gltf-binary + тело (меш мокаем);
  • не админ → 403;
  • заказа нет / у заказа пустой file_path / файла нет на диске → 404;
  • не 3D-формат (.step) и слишком тяжёлый меш → 404 (тот же гейт, что у клиента).
"""
import pytest
from aiohttp import web

from app import webapp
from app.web import mesh3d


class _FakeRequest:
    """Мини-заглушка aiohttp-запроса: хендлеру нужны только query."""
    def __init__(self, rid="1", init_data="admin"):
        self.query = {"initData": init_data, "rid": str(rid)}
        self.headers = {}


class _Req:
    def __init__(self, file_path):
        self.file_path = file_path


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    # initData → uid только для admin-токена; иначе None (не админ / подпись не прошла).
    # Подмена в ОБА модуля: ручка живёт в app/web/mesh3d.py (волна 9 endgame) и читает
    # _verify_admin из СВОИХ глобалов; webapp — лишь фасад.
    fn = lambda init_data: 999 if init_data == "admin" else None  # noqa: E731
    monkeypatch.setattr(webapp, "_verify_admin", fn)
    monkeypatch.setattr(mesh3d, "_verify_admin", fn)
    yield


def _seed_order(monkeypatch, mapping):
    async def _get(rid):
        return mapping.get(rid)
    monkeypatch.setattr(webapp.db, "get_request", _get)


async def _call(rid="1", init_data="admin"):
    return await webapp._api_admin_orderfile(_FakeRequest(rid, init_data))


async def test_admin_served_extracted_glb(tmp_path, monkeypatch):
    f = tmp_path / "order.3mf"; f.write_bytes(b"PK\x03\x04rest")   # даже Bambu/MakerWorld .3mf
    _seed_order(monkeypatch, {1: _Req(str(f))})
    monkeypatch.setattr(webapp, "_model3d_glb_bytes", lambda paths, *a, **kw: b"FAKE-BINARY-GLB")
    monkeypatch.setattr(mesh3d, "_model3d_glb_bytes", lambda paths, *a, **kw: b"FAKE-BINARY-GLB")
    resp = await _call(1)
    assert isinstance(resp, web.Response)
    assert resp.headers["Content-Type"] == "model/gltf-binary"
    assert resp.body == b"FAKE-BINARY-GLB"


async def test_non_admin_forbidden(tmp_path, monkeypatch):
    f = tmp_path / "order.stl"; f.write_bytes(b"solid x\n")
    _seed_order(monkeypatch, {1: _Req(str(f))})
    resp = await _call(1, init_data="nope")
    assert isinstance(resp, web.Response)
    assert resp.status == 403


async def test_missing_order_not_found(monkeypatch):
    _seed_order(monkeypatch, {})            # заказа с таким rid нет
    with pytest.raises(web.HTTPNotFound):
        await _call(42)


async def test_empty_file_path_not_found(monkeypatch):
    _seed_order(monkeypatch, {1: _Req("")})   # заказ есть, но модель не сохранена
    with pytest.raises(web.HTTPNotFound):
        await _call(1)


async def test_file_gone_not_found(tmp_path, monkeypatch):
    _seed_order(monkeypatch, {1: _Req(str(tmp_path / "vanished.stl"))})
    with pytest.raises(web.HTTPNotFound):
        await _call(1)


async def test_step_format_not_served(tmp_path, monkeypatch):
    f = tmp_path / "part.step"; f.write_text("ISO-10303-21;\n")
    _seed_order(monkeypatch, {1: _Req(str(f))})
    with pytest.raises(web.HTTPNotFound):     # three.js не парсит STEP → PNG-фолбэк
        await _call(1)


async def test_oversize_mesh_not_served(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_MODEL3D_MAX_SRC_BYTES", 16)
    monkeypatch.setattr(mesh3d, "_MODEL3D_MAX_SRC_BYTES", 16)
    f = tmp_path / "big.stl"; f.write_bytes(b"solid " + b"x" * 64)
    _seed_order(monkeypatch, {1: _Req(str(f))})
    with pytest.raises(web.HTTPNotFound):     # тяжёлый исходник не разбираем
        await _call(1)


async def test_unparseable_mesh_not_found(tmp_path, monkeypatch):
    f = tmp_path / "broken.3mf"; f.write_bytes(b"not a real 3mf")
    _seed_order(monkeypatch, {1: _Req(str(f))})
    monkeypatch.setattr(webapp, "_model3d_glb_bytes", lambda paths, *a, **kw: None)
    monkeypatch.setattr(mesh3d, "_model3d_glb_bytes", lambda paths, *a, **kw: None)
    with pytest.raises(web.HTTPNotFound):     # меш не разобрался → деск остаётся на PNG
        await _call(1)
