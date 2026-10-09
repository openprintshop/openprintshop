"""Генерация картинки release note по промту мастера (action image_regen).

Кнопка «✨ Сгенерировать» в админке открывает диалог с редактируемым промтом (и
заготовками); сервер зовёт ту же цепочку, что аватарки, и запоминает промт в
payload.gen_prompt — при следующем открытии диалога мастер правит его, а не пишет заново.
"""
from io import BytesIO
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import stub_auth
from PIL import Image

from app import avatars, config, webapp


def _png(tmp_path: Path) -> Path:
    p = tmp_path / "gen.png"
    buf = BytesIO()
    Image.new("RGB", (512, 512), (200, 120, 40)).save(buf, "PNG")
    p.write_bytes(buf.getvalue())
    return p


async def _client() -> TestClient:
    client = TestClient(TestServer(webapp.make_app()))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_image_regen_saves_image_and_prompt(tmp_db, tmp_path, monkeypatch):
    """Промт → картинка у черновика + gen_prompt в payload (предзаполнение диалога)."""
    stub_auth(monkeypatch, _admin_body=lambda body: (1, None))
    monkeypatch.setattr(config, "NOTIF_IMG_DIR", tmp_path / "notif")
    src = _png(tmp_path)
    asked = []

    def _gen(uid, prompt=None, avoid="", *, randomize=False, scene=False):
        asked.append((prompt, randomize))
        assert scene is True, "картинка release note — сцена (кадр 4:3, стиль иллюстрации), не аватарка"
        return {"ok": True, "path": str(src), "prompt": prompt, "seed": "x"}
    monkeypatch.setattr(avatars, "generate", _gen)

    nid = await tmp_db.add_admin_notification("release_note", title="Т", body="Б",
                                              payload={"gen_prompt": "старый промт"})
    client = await _client()
    try:
        resp = await client.post("/api/admin/notification_action",
                                 json={"id": nid, "action": "image_regen",
                                       "prompt": "робот у принтера"})
        j = await resp.json()
    finally:
        await client.close()

    assert j["ok"] and j["image_url"], j
    assert asked == [("робот у принтера", True)], \
        "генератор должен получить промт мастера как есть и randomize=True (повтор → новая картинка)"
    n = await tmp_db.get_admin_notification(nid)
    assert n["image_path"] and Path(n["image_path"]).exists()
    assert n["payload"]["gen_prompt"] == "робот у принтера", \
        "промт обязан запомниться для следующего открытия диалога"


@pytest.mark.asyncio
async def test_image_regen_rejects_empty_prompt_and_foreign_kind(tmp_db, tmp_path, monkeypatch):
    """Пустой промт и не-release_note — честный отказ, генератор не дёргается."""
    stub_auth(monkeypatch, _admin_body=lambda body: (1, None))
    called = []
    monkeypatch.setattr(avatars, "generate",
                        lambda *a, **kw: called.append(a) or {"ok": True})

    rn = await tmp_db.add_admin_notification("release_note", title="Т", body="Б")
    pc = await tmp_db.add_admin_notification("print_classify", title="Печать", body="")
    client = await _client()
    try:
        for nid, prompt in ((rn, "   "), (pc, "робот")):
            resp = await client.post("/api/admin/notification_action",
                                     json={"id": nid, "action": "image_regen", "prompt": prompt})
            j = await resp.json()
            assert not j["ok"], (nid, prompt)
    finally:
        await client.close()
    assert not called, "при отказе генерация запускаться не должна"
