"""Управление ИИ-генератором с экрана «Статус» (OpenTodo ee064f93): вкл/выкл файлом-флагом,
ручная выгрузка из VRAM, порог автовыгрузки по простою (settings поверх .env),
выбор модели генерации (text_settings поверх .env, 22.07)."""
import json

from app import avatars_gpu, config, db, webapp


class _FakeRequest:
    def __init__(self, body=None, query=None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


async def _act(body):
    resp = await webapp._api_admin_ai_action(_FakeRequest(body))
    return json.loads(resp.body.decode())


async def test_ai_toggle_flag_roundtrip(tmp_db, tmp_path, monkeypatch):
    """Выключение создаёт тот же файл-флаг, что ярлык на рабочем столе; включение убирает.
    Ответ сразу несёт свежий статус — тумблер на фронте не врёт."""
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (1, None))
    flag = tmp_path / "avatar_gpu.off"
    monkeypatch.setattr(avatars_gpu, "_OFF_FLAG", flag)

    j = await _act({"action": "toggle", "on": False})
    assert j["ok"] and flag.exists() and j["ai"]["off_by_flag"] is True
    j = await _act({"action": "toggle", "on": True})
    assert j["ok"] and not flag.exists() and j["ai"]["off_by_flag"] is False


async def test_ai_idle_setting_overrides_env_default(tmp_db, monkeypatch):
    """Порог автовыгрузки: без настройки — дефолт из .env; после правки из «Статуса» —
    значение из settings (переживает рестарт, действует без перезапуска). 0 — не выгружать."""
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (1, None))
    monkeypatch.setattr(config, "AVATAR_GPU_IDLE_MIN", 5.0)
    assert await webapp.ai_idle_min() == 5.0             # настройки нет — дефолт .env

    j = await _act({"action": "idle", "minutes": 30})
    assert j["ok"] and j["ai"]["idle_unload_min"] == 30
    assert await webapp.ai_idle_min() == 30.0            # настройка главнее .env

    j = await _act({"action": "idle", "minutes": 0})     # 0 = держать в VRAM
    assert j["ok"] and j["ai"]["idle_unload_min"] == 0

    assert not (await _act({"action": "idle", "minutes": -5}))["ok"]
    assert not (await _act({"action": "idle", "minutes": "мусор"}))["ok"]


async def test_ai_model_choice_applies_and_persists(tmp_db, monkeypatch):
    """Выбор модели из «Статуса»: применяется сразу (set_model) и сохраняется в text_settings
    (переживает рестарт — run._warmup_avatar_gpu применит). Чужой repo id отклоняется и
    ничего не пишет; пустая строка возвращает модель из .env."""
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (1, None))
    monkeypatch.setattr(config, "AVATAR_GPU_MODEL", "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(avatars_gpu, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(avatars_gpu, "_PIPE", None)
    monkeypatch.setattr(avatars_gpu, "_WORKER", None)

    j = await _act({"action": "model", "model": "stabilityai/sd-turbo"})
    assert j["ok"] and j["ai"]["model"] == "stabilityai/sd-turbo"
    assert j["ai"]["model_from_env"] is False
    assert [m["id"] for m in j["ai"]["models"] if m["current"]] == ["stabilityai/sd-turbo"]
    assert (await db.get_text_settings()).get("avatar_gpu_model") == "stabilityai/sd-turbo"

    j = await _act({"action": "model", "model": "evil/unknown"})
    assert j["ok"] is False and "Неизвестная модель" in j["error"]
    assert (await db.get_text_settings()).get("avatar_gpu_model") == "stabilityai/sd-turbo"

    j = await _act({"action": "model", "model": ""})
    assert j["ok"] and j["ai"]["model"] == "Tongyi-MAI/Z-Image-Turbo"
    assert j["ai"]["model_from_env"] is True
    assert (await db.get_text_settings()).get("avatar_gpu_model") == ""


async def test_ai_unload_honest_when_nothing_loaded(tmp_db, monkeypatch):
    """Выгрузка при незагруженной модели — честный отказ, а не ложное «выгружено»."""
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (1, None))
    j = await _act({"action": "unload"})
    assert j["ok"] is False and "не загружена" in j["error"]


async def test_ai_unknown_action_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (1, None))
    assert (await _act({"action": "wat"}))["ok"] is False


async def _test_img(body):
    resp = await webapp._api_admin_ai_test(_FakeRequest(body))
    return json.loads(resp.body.decode())


async def test_ai_test_image_endpoint(tmp_db, monkeypatch):
    """Проба генерации из «Статуса» (22.07): PNG уезжает base64-датой в ответе вместе с
    честным движком; styled пробрасывается; пустой промт, выключенный генератор и None от
    generate_png (занят/сломан) — честные отказы, а не пустая картинка."""
    import base64
    monkeypatch.setattr(webapp, "_admin_body", lambda b: (1, None))
    monkeypatch.setattr(avatars_gpu, "available", lambda: True)
    got = {}

    def fake_gen(prompt, seed, styled=True):
        got.update(prompt=prompt, styled=styled)
        return b"PNGDATA", "sd-turbo"
    monkeypatch.setattr(avatars_gpu, "generate_png", fake_gen)

    j = await _test_img({"prompt": "ёжик", "styled": False})
    assert j["ok"] and j["engine"] == "sd-turbo" and j["seconds"] >= 0
    assert j["image"].startswith("data:image/png;base64,")
    assert base64.b64decode(j["image"].split(",", 1)[1]) == b"PNGDATA"
    assert got == {"prompt": "ёжик", "styled": False}

    assert (await _test_img({"prompt": "   "}))["ok"] is False

    monkeypatch.setattr(avatars_gpu, "generate_png", lambda p, s, styled=True: None)
    j = await _test_img({"prompt": "ёж"})
    assert j["ok"] is False and "видеокарта занята" in j["error"]

    monkeypatch.setattr(avatars_gpu, "available", lambda: False)
    j = await _test_img({"prompt": "ёж"})
    assert j["ok"] is False and "выключена" in j["error"]
