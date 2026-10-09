"""GPU-генерация аватарок (OpenTodo a327643f): гейтинг, цепочка тиров, откат на процедурный.

С фидбэка 21.07 («почему процедурный вместо ИИ») цепочка трёхуровневая:
настроенная модель (Z-Image в воркере) → лёгкий запасной sd-turbo → процедурный рендер.
generate_png отдаёт (png, имя движка) — UI честно говорит, чем нарисовано.
"""
from app import avatars, avatars_gpu, config


def _reset(monkeypatch, model: str) -> None:
    monkeypatch.setattr(config, "AVATAR_GPU", True)
    monkeypatch.setattr(config, "AVATAR_GPU_MODEL", model)
    monkeypatch.setattr(avatars_gpu, "_MODEL_OVERRIDE", None)
    monkeypatch.setattr(avatars_gpu, "_LOAD_FAILED", False)
    monkeypatch.setattr(avatars_gpu, "_ZIMAGE_FAILED", False)
    monkeypatch.setattr(avatars_gpu, "_PIPE", None)
    monkeypatch.setattr(avatars_gpu, "_PIPE_MODEL", "")
    monkeypatch.setattr(avatars_gpu, "_WORKER", None)
    monkeypatch.setattr(avatars_gpu, "_ZIMAGE_FAILED_AT", 0.0)
    monkeypatch.setattr(avatars_gpu, "_LOAD_FAILED_AT", 0.0)
    monkeypatch.setattr(avatars_gpu, "_STACK_WARNED", False)
    monkeypatch.setattr(avatars_gpu, "available", lambda: True)
    # стек ИИ считаем полным независимо от venv машины (тест проверки стека подменяет сам)
    monkeypatch.setattr(avatars_gpu, "stack_missing", lambda: [])
    # снимок nvidia-smi в тестах не делаем — машина может быть занята реальным ботом
    monkeypatch.setattr(avatars_gpu, "_gpu_snapshot", lambda: None)


def test_available_respects_flag(monkeypatch):
    monkeypatch.setattr(config, "AVATAR_GPU", False)
    assert avatars_gpu.available() is False


def test_generate_png_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(config, "AVATAR_GPU", False)
    assert avatars_gpu.generate_png("ёжик", 42) is None


def test_load_failure_marks_broken_and_falls_back(monkeypatch, tmp_path):
    """Сбой загрузки пайплайна: один честный warning, in-process тир выключен до перезапуска,
    avatars.generate всё равно отдаёт картинку (процедурную) и честный engine."""
    _reset(monkeypatch, "stabilityai/sd-turbo")

    def boom(*_a):
        raise RuntimeError("нет модели")
    monkeypatch.setattr(avatars_gpu, "_load", boom)
    # промт латиницей: переводчики в тестах заглушены, а русский промт CLIP-семейство теперь
    # не получает вовсе (см. test_inprocess_refuses_untranslated_cyrillic) — до _load не дошли бы
    assert avatars_gpu.generate_png("cat", 7) is None
    assert avatars_gpu._LOAD_FAILED is True        # available() учтёт флаг → GPU не дёргаем, пока идёт кулдаун

    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path)
    res = avatars.generate(1, "космический ёжик")  # процедурный фолбэк живёт
    assert res["ok"] and (tmp_path / "1.png").exists()
    assert res["engine"] == "procedural"


def test_zimage_failure_falls_back_to_light_model(monkeypatch):
    """Z-Image не поднялся → рисует запасной sd-turbo, а НЕ процедурная заглушка (фидбэк 21.07)."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(avatars_gpu, "_ensure_worker", lambda lite=False: False)
    asked = []

    def fake_inprocess(en, seed, snap, model, styled=True, scene=False):
        asked.append(model)
        return b"png-bytes"
    monkeypatch.setattr(avatars_gpu, "_inprocess_png", fake_inprocess)

    assert avatars_gpu.generate_png("кот", 7) == (b"png-bytes", "sd-turbo")
    assert asked == [avatars_gpu._FALLBACK_MODEL], "запасной обязан быть sd-turbo"


def test_zimage_vram_gate_uses_fallback_not_procedural(monkeypatch):
    """Мало VRAM даже под облегчённый Z-Image (порог ~5.5 ГиБ, свободно 5000) → воркер даже
    не поднимаем, но картинку рисует запасной, а не процедурный рендер."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(avatars_gpu, "_gpu_snapshot", lambda: (5.0, 5000.0))

    def no_worker(lite=False):
        raise AssertionError("при нехватке VRAM воркер Z-Image подниматься не должен")
    monkeypatch.setattr(avatars_gpu, "_ensure_worker", no_worker)
    monkeypatch.setattr(avatars_gpu, "_inprocess_png",
                        lambda en, s, snap, model, styled=True, scene=False: b"light")

    assert avatars_gpu.generate_png("кот", 7) == (b"light", "sd-turbo")


def test_zimage_vram_gate_picks_lite_quant_between_thresholds(monkeypatch):
    """Свободно 6000 МБ: на Q6_K (7168) не хватает, на Q4_K_M (5632) хватает → воркер
    поднимается ОБЛЕГЧЁННЫМ, а не сразу sd-turbo (02.09: рабочий стол держал 5.9–6.3 ГБ
    из 12, и прежний одноступенчатый гейт ронял на sd-turbo при каждом клике)."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(config, "AVATAR_GPU_GGUF_LITE", "https://huggingface.co/x/y/blob/main/z-Q4_K_M.gguf")
    monkeypatch.setattr(avatars_gpu, "_gpu_snapshot", lambda: (5.0, 6000.0))
    asked = []

    def fake_ensure(lite=False):
        asked.append(lite)
        return False                      # «не поднялся» — дальше запасной, тир уже выбран
    monkeypatch.setattr(avatars_gpu, "_ensure_worker", fake_ensure)
    monkeypatch.setattr(avatars_gpu, "_inprocess_png",
                        lambda en, s, snap, model, styled=True, scene=False: b"light")

    assert avatars_gpu.generate_png("кот", 7) == (b"light", "sd-turbo")
    assert asked == [True], "между порогами Z-Image обязан идти облегчённым квантом"

    # без облегчённого кванта в конфиге прежнее поведение: сразу запасной, воркер не трогаем
    asked.clear()
    monkeypatch.setattr(config, "AVATAR_GPU_GGUF_LITE", "")
    assert avatars_gpu.generate_png("кот", 7) == (b"light", "sd-turbo")
    assert asked == []


def test_zimage_success_reports_engine(monkeypatch):
    """Успех основной модели: engine = z-image-turbo (или -lite — как сказал тир), запасной
    не дёргается."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(avatars_gpu, "_zimage_png",
                        lambda en, s, snap, styled=True, scene=False: (b"big", "z-image-turbo"))

    def no_light(*_a, **_k):
        raise AssertionError("при успехе Z-Image запасной не нужен")
    monkeypatch.setattr(avatars_gpu, "_inprocess_png", no_light)

    assert avatars_gpu.generate_png("кот", 7) == (b"big", "z-image-turbo")

    monkeypatch.setattr(avatars_gpu, "_zimage_png",
                        lambda en, s, snap, styled=True, scene=False: (b"soft", "z-image-turbo-lite"))
    assert avatars_gpu.generate_png("кот", 7) == (b"soft", "z-image-turbo-lite")


def test_zimage_skipped_when_stack_incomplete(monkeypatch):
    """Нет accelerate/gguf в окружении (02.09: пропал из .venv) → воркер Z-Image не спавним
    (он упал бы на ImportError через 10 с, и так каждые 15 мин), сразу запасной; предупреждение
    в логе одно на процесс."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(avatars_gpu, "stack_missing", lambda: ["accelerate"])

    def no_worker(lite=False):
        raise AssertionError("без стека воркер спавнить нельзя")
    monkeypatch.setattr(avatars_gpu, "_ensure_worker", no_worker)
    monkeypatch.setattr(avatars_gpu, "_inprocess_png",
                        lambda en, s, snap, model, styled=True, scene=False: b"light")

    assert avatars_gpu.generate_png("cat", 7) == (b"light", "sd-turbo")
    assert avatars_gpu._STACK_WARNED is True
    assert avatars_gpu.generate_png("cat", 8) == (b"light", "sd-turbo")
    assert "accelerate" in avatars_gpu.status()["stack_missing"]


def test_inprocess_refuses_untranslated_cyrillic(monkeypatch):
    """Перевод не удался (gtx 429 и офлайн-переводчика нет) → русский промт в CLIP-модель
    НЕ отдаём (02.09: sd-turbo рисовал случайные сюжеты), _load не дёргаем, а вызывающий
    получит None → процедурный маскот, который хотя бы читает промт."""
    _reset(monkeypatch, "stabilityai/sd-turbo")

    def boom(*_a, **_k):
        raise AssertionError("с непереведённым промтом модель грузить не надо")
    monkeypatch.setattr(avatars_gpu, "_load", boom)
    assert avatars_gpu.generate_png("бешеный кальмар", 7) is None
    assert avatars_gpu._LOAD_FAILED is False       # это не поломка генератора


def test_to_english_chain_and_cache(monkeypatch):
    """Перевод: gtx → офлайн → оригинал; успешный перевод кэшируется (повторный клик с тем же
    промтом не ходит в сеть — так 02.09 и словили 429), неудача — нет."""
    avatars_gpu._TR_CACHE.clear()
    calls = {"gtx": 0, "off": 0}

    def gtx(p):
        calls["gtx"] += 1
        return None                       # 429

    def off(p):
        calls["off"] += 1
        return "space hedgehog"
    monkeypatch.setattr(avatars_gpu, "_gtx_translate", gtx)
    monkeypatch.setattr(avatars_gpu, "_offline_translate", off)

    assert avatars_gpu._to_english("космический ёжик") == "space hedgehog"
    assert avatars_gpu._to_english("космический ёжик") == "space hedgehog"
    assert calls == {"gtx": 1, "off": 1}, "второй раз — из кэша"

    monkeypatch.setattr(avatars_gpu, "_offline_translate", lambda p: None)
    assert avatars_gpu._to_english("грустный пингвин") == "грустный пингвин"
    assert avatars_gpu._to_english("грустный пингвин") == "грустный пингвин"
    assert calls["gtx"] == 3, "неудачу не кэшируем — следующий клик пробует снова"

    monkeypatch.setattr(avatars_gpu, "_gtx_translate", lambda p: "sad penguin")
    assert avatars_gpu._to_english("грустный пингвин") == "sad penguin"


def test_scene_style_and_sizes():
    """Иллюстрация к release note: стиль сцены без «app avatar/centered portrait», кадр 4:3;
    облегчённый квант рисует на 768-классе. Стороны кратны 16."""
    z = avatars_gpu._final_prompt("robot sorts parts", "Tongyi-MAI/Z-Image-Turbo", True, scene=True)
    s = avatars_gpu._final_prompt("robot sorts parts", "stabilityai/sd-turbo", True, scene=True)
    assert z.startswith("robot sorts parts, ") and "wide shot" in z and "app avatar" not in z
    assert "wide shot" in s and "detailed" not in s
    assert avatars_gpu._final_prompt("x", "Tongyi-MAI/Z-Image-Turbo", False, scene=True) == "x"

    assert avatars_gpu._zimage_size(False, False) == (1024, 1024)
    assert avatars_gpu._zimage_size(True, False) == (768, 768)
    w, h = avatars_gpu._zimage_size(False, True)
    assert (w, h) == (1152, 864) and w * 3 == h * 4
    w, h = avatars_gpu._zimage_size(True, True)
    assert w * 3 == h * 4 and w % 16 == 0 and h % 16 == 0 and w < 1152

    assert avatars_gpu._gguf_label("https://huggingface.co/a/b/blob/main/z_image_turbo-Q4_K_M.gguf") == "Q4_K_M"
    assert avatars_gpu._gguf_label("") == "bf16"
    assert avatars_gpu._need_mb("Tongyi-MAI/Z-Image-Turbo", lite=True) < avatars_gpu._need_mb("Tongyi-MAI/Z-Image-Turbo")


def test_set_model_override_reset_and_steps(monkeypatch):
    """Выбор модели из админки (22.07): каталожная модель применяется поверх .env, пустое
    значение возвращает .env, чужой repo id отклоняется. Шаги — рецепт выбранной модели,
    а не глобальный AVATAR_GPU_STEPS (9 шагов Z-Image не должны достаться sd-turbo)."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(config, "AVATAR_GPU_STEPS", 9)

    assert avatars_gpu.set_model("stabilityai/sd-turbo") is True
    assert avatars_gpu.current_model() == "stabilityai/sd-turbo"
    assert avatars_gpu._is_zimage() is False
    assert avatars_gpu._steps_for("stabilityai/sd-turbo") == 4

    assert avatars_gpu.set_model("evil/unknown") is False, "неизвестный repo id не принимаем"
    assert avatars_gpu.current_model() == "stabilityai/sd-turbo"

    assert avatars_gpu.set_model(None) is True
    assert avatars_gpu.current_model() == "Tongyi-MAI/Z-Image-Turbo"
    assert avatars_gpu._is_zimage() is True
    # без выбора из админки ручная настройка шагов из .env продолжает уважаться
    assert avatars_gpu._steps_for("Tongyi-MAI/Z-Image-Turbo") == 9


def test_set_model_switch_unloads_pipeline(monkeypatch):
    """Смена модели выгружает текущий пайплайн/воркер (в 12 ГБ два не живут); повторный
    выбор той же модели ничего не выгружает и не сбрасывает пометки сбоев."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    calls: list[str] = []
    monkeypatch.setattr(avatars_gpu, "unload", lambda reason="": calls.append(reason) or True)

    monkeypatch.setattr(avatars_gpu, "_ZIMAGE_FAILED", True)
    assert avatars_gpu.set_model("stabilityai/sd-turbo") is True
    assert calls, "смена модели обязана выгружать старый пайплайн"
    assert avatars_gpu._ZIMAGE_FAILED is False, "у новой модели свой шанс загрузиться"

    calls.clear()
    assert avatars_gpu.set_model("stabilityai/sd-turbo") is True
    assert not calls, "выбор той же модели — не повод дёргать выгрузку"


def test_status_models_catalog_marks_current(monkeypatch):
    """status().models — каталог для переключателя в админке: ровно одна текущая; модель из
    .env вне каталога добавляется отдельной строкой, чтобы было куда вернуться."""
    _reset(monkeypatch, "somebody/custom-model")
    st = avatars_gpu.status()
    ids = [m["id"] for m in st["models"]]
    assert "somebody/custom-model" in ids and len(ids) == len(avatars_gpu.MODELS) + 1
    assert [m["id"] for m in st["models"] if m["current"]] == ["somebody/custom-model"]

    assert avatars_gpu.set_model("stabilityai/sdxl-turbo") is True
    st = avatars_gpu.status()
    assert st["model"] == "stabilityai/sdxl-turbo" and st["model_label"] == "SDXL-Turbo"
    assert [m["id"] for m in st["models"] if m["current"]] == ["stabilityai/sdxl-turbo"]
    assert st["model_from_env"] is False


def test_final_prompt_styles_by_family_and_raw_mode():
    """Промт для модели: фирменный стиль по семейству (Z-Image/SDXL — длинный, SD2 — без
    вотермарк-слов), styled=False (панель «Проба генерации») — сырой промт как есть."""
    zp = avatars_gpu._final_prompt("cat", "Tongyi-MAI/Z-Image-Turbo", True)
    xp = avatars_gpu._final_prompt("cat", "stabilityai/sdxl-turbo", True)
    sp = avatars_gpu._final_prompt("cat", "stabilityai/sd-turbo", True)
    assert zp == xp != sp and all(p.startswith("cat, cute mascot") for p in (zp, xp, sp))
    assert "detailed" in zp and "detailed" not in sp   # SD2: «detailed» провоцирует вотермарки
    assert avatars_gpu._final_prompt("cat", "stabilityai/sd-turbo", False) == "cat"


def test_generate_png_raw_mode_reaches_tiers(monkeypatch):
    """styled=False доезжает до обоих тиров: и до воркера Z-Image, и до запасного."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    got = {}

    def fake_zimage(en, seed, snap, styled=True, scene=False):
        got["zimage"] = styled
        return None                       # Z-Image «не смог» — уходим на запасной

    def fake_inprocess(en, seed, snap, model, styled=True, scene=False):
        got["inprocess"] = styled
        return b"png"
    monkeypatch.setattr(avatars_gpu, "_zimage_png", fake_zimage)
    monkeypatch.setattr(avatars_gpu, "_inprocess_png", fake_inprocess)

    assert avatars_gpu.generate_png("cat", 7, styled=False) == (b"png", "sd-turbo")
    assert got == {"zimage": False, "inprocess": False}


def test_to_english_passthrough_for_latin():
    assert avatars_gpu._to_english("space hedgehog") == "space hedgehog"


def test_generate_png_skips_when_gpu_busy(monkeypatch):
    """GPU занята игрой → мгновенный None (процедурный фолбэк), без попытки загрузки (655318e9)."""
    _reset(monkeypatch, "stabilityai/sd-turbo")
    monkeypatch.setattr(avatars_gpu, "_gpu_snapshot", lambda: (95.0, 11000.0))

    def boom(*_a):
        raise AssertionError("не должны грузить модель при занятой GPU")
    monkeypatch.setattr(avatars_gpu, "_load", boom)
    monkeypatch.setattr(avatars_gpu, "_ensure_worker", boom)
    assert avatars_gpu.generate_png("кот", 7) is None
    assert avatars_gpu._LOAD_FAILED is False       # это не поломка генератора


def test_generate_png_skips_when_lock_busy(monkeypatch):
    """Параллельная генерация держит лок дольше таймаута → None, очередь не строим (655318e9)."""
    monkeypatch.setattr(config, "AVATAR_GPU", True)
    monkeypatch.setattr(avatars_gpu, "available", lambda: True)
    monkeypatch.setattr(avatars_gpu, "_gpu_snapshot", lambda: None)
    monkeypatch.setattr(avatars_gpu, "_PIPE", None)
    monkeypatch.setattr(avatars_gpu, "_WORKER", None)

    class BusyLock:                                   # «занято» без ожидания 10 с в тесте
        def acquire(self, timeout=None):
            return False
        def release(self):
            raise AssertionError("release без успешного acquire")

    monkeypatch.setattr(avatars_gpu, "_LOCK", BusyLock())
    assert avatars_gpu.generate_png("кот", 7) is None


# ───────── предполётная проверка перед промоушеном 03.09.2026 (три регрессии батча 02.09) ─────────


def test_load_failed_is_not_forever(monkeypatch):
    """Разовый OOM при загрузке не гасит ИИ-генерацию НАВСЕГДА: после кулдауна — новая попытка.

    Флаг _LOAD_FAILED читает available(), а generate_png начинается с `if not available()`, то есть
    один неудачный _load() выключал ИИ ВСЕМ клиентам до перезапуска бота (тумблер в админке флаг
    не снимает). У соседнего _ZIMAGE_FAILED кулдаун был, у этого — нет."""
    import time
    # available() здесь нужна НАСТОЯЩАЯ (в _reset она заглушена), поэтому globals ставим сами
    monkeypatch.setattr(config, "AVATAR_GPU", True)
    monkeypatch.setattr(avatars_gpu, "_LOAD_FAILED", True)
    monkeypatch.setattr(avatars_gpu, "_LOAD_FAILED_AT", time.time())
    assert avatars_gpu._load_failed_now() is True, "пока кулдаун идёт — тир выключен"
    assert avatars_gpu.available() is False, "и вся GPU-генерация с ним"

    monkeypatch.setattr(avatars_gpu, "_LOAD_FAILED_AT",
                        time.time() - avatars_gpu._ZIMAGE_RETRY_SEC - 1)
    assert avatars_gpu._load_failed_now() is False, "кулдаун истёк — даём загрузке ещё шанс"
    assert avatars_gpu._LOAD_FAILED is False


def test_fallback_does_not_load_blind_next_to_live_worker(monkeypatch):
    """Воркер Z-Image сорвал ГЕНЕРАЦИЮ и остался жив (карту держит) → запасной sd-turbo обязан
    получить снимок VRAM, иначе грузится вслепую в остаток памяти и ловит OOM."""
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    monkeypatch.setattr(avatars_gpu, "_worker_alive", lambda: True)   # воркер жив → снимок на входе не берут
    monkeypatch.setattr(avatars_gpu, "_zimage_png", lambda *a, **k: None)   # сбой генерации, воркер НЕ погашен
    monkeypatch.setattr(avatars_gpu, "_gpu_snapshot", lambda: (5.0, 400.0))   # свободно 400 МБ
    seen = {}

    def spy(_en, _seed, snap, model, *_a, **_k):
        seen["snap"] = snap
        return None
    monkeypatch.setattr(avatars_gpu, "_inprocess_png", spy)
    assert avatars_gpu.generate_png("cat", 7) is None
    assert seen["snap"] == (5.0, 400.0), "гейт свободной VRAM должен получить реальный снимок, а не None"


def test_status_survives_worker_vanishing_mid_read(monkeypatch):
    """status() зовётся ПРЯМО в event loop без try/except: TypeError там = 500 на весь экран
    «Статус системы».

    Гонку воспроизводим детерминированно: воркер исчезает ровно в том вызове, что читает
    zimage_lite (в живой системе это делает _stop_worker из другого потока — выгрузка по
    простою, кнопка «Выгрузить», таймаут генерации, — а окно широкое, потому что между двумя
    чтениями глобала стоит proc.is_alive(), отпускающий GIL). Счётчик нужен, чтобы первый
    вызов (ключ "loaded") прошёл штатно и гонка пришлась именно на второе чтение.
    """
    _reset(monkeypatch, "Tongyi-MAI/Z-Image-Turbo")
    calls = {"n": 0}

    def alive():
        calls["n"] += 1
        if calls["n"] >= 2:                  # ровно то, что делает _stop_worker в другом потоке
            avatars_gpu._WORKER = None
        return True

    monkeypatch.setattr(avatars_gpu, "_worker_alive", alive)
    monkeypatch.setattr(avatars_gpu, "_WORKER", (type("P", (), {"is_alive": lambda self: True})(), None, True))
    st = avatars_gpu.status()                # раньше здесь был TypeError: 'NoneType' not subscriptable
    assert st["zimage_lite"] is True
