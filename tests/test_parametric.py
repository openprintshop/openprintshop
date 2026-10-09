"""Параметрические модели на OpenSCAD (OpenTodo c1cc840a): разбор Customizer-параметров,
санитизация значений (анти-инъекция + клампы), и — если OpenSCAD установлен — реальный рендер."""
import pytest

from app import parametric


def _keychain():
    return parametric.get_model("keychain_plate")


def test_parse_customizer_extracts_params():
    m = _keychain()
    schema = parametric.schema_for(m)
    # числовые с диапазоном
    assert schema["Length"]["type"] == "number" and schema["Length"]["min"] == 10.0 and schema["Length"]["max"] == 230.0
    assert schema["Width"]["type"] == "number" and schema["Width"]["max"] == 200.0
    # строка
    assert schema["Text"]["type"] == "string"
    # Font — простой строковый параметр (длинный список Google-шрифтов убран; шрифт задаёт фронт)
    assert schema["Font"]["type"] == "string"
    # enum (список значений в комментарии) — на Font_Style
    assert schema["Font_Style"]["type"] == "enum" and "Bold" in schema["Font_Style"]["options"]
    # вычисляемые/спец-переменные НЕ попадают
    assert "surname_font" not in schema and "$fn" not in schema


def test_public_params_only_show_list():
    m = _keychain()
    names = [p["name"] for p in parametric.public_params(m)]
    assert names == ["Text", "Length", "Width", "Text_size"]   # порядок из show


def test_keychain_euro_toggle_maps_strip():
    """Варианты «метка/бирка» (cable/pet/cloak) — это keychain_plate с euro:false → Euro=0 (без
    синей полосы); номер авто (euro:true) → Euro=1. Один _keychain_to_scad обслуживает оба."""
    m = _keychain()
    base = {"text": "Имя", "fontSize": 30, "widthMm": 70, "thickMm": 3,
            "font": "round", "shape": "rounded", "color": "#fff", "ring": True, "border": False}
    v_tag = m["to_scad"]({**base, "euro": False})
    assert v_tag["Euro"] == 0 and v_tag["Text"] == "Имя"
    v_plate = m["to_scad"]({**base, "euro": True})
    assert v_plate["Euro"] == 1
    # модель помечена euro:true (показывать слот полосы), но скрыта из старой формы — живёт в кастомайзере
    assert m.get("euro") is True and m.get("hidden") is True


def test_validate_clamps_and_sanitizes():
    m = _keychain()
    v = parametric.validate_values(m, {
        "Text": "Я" * 100,          # длинная строка → обрезается
        "Length": 9999,             # > max → 230
        "Width": -5,                # < min → 1
        "Text_size": "abc",         # не число → дефолт
        "Font": "evil()",           # не из show → отброшен
        "Hole_size": 50,            # не из show → отброшен
    })
    assert len(v["Text"]) == parametric.MAX_TEXT_LEN
    assert v["Length"] == 230 and v["Width"] == 1
    assert v["Text_size"] == 8                  # дефолт из .scad
    assert "Font" not in v and "Hole_size" not in v


def test_fmt_value_quotes_strings_no_injection():
    sp = {"type": "string", "default": ""}
    # попытка инъекции кода в значение → остаётся строковым литералом (экранированные кавычки)
    out = parametric._fmt_value(sp, 'a"; cube(50); s="b')
    assert out.startswith('"') and out.endswith('"')
    assert 'cube(50)' in out and '\\"' in out   # внутри строки, кавычки экранированы
    # число форматируется как число
    assert parametric._fmt_value({"type": "number", "default": 1}, 42) == "42.0"


def test_label_for():
    m = _keychain()
    assert parametric.label_for(m, {"Text": "Аня"}) == "Брелок номер авто «Аня»"
    assert parametric.label_for(m, {"Text": ""}) == "Брелок номер авто"


def test_prepare_values_maps_customizer_schema():
    """Кастомайзер (Коллекция → «Создай своё») шлёт схему фронта → маппится на параметры .scad."""
    m = _keychain()
    v = parametric.prepare_values(m, {
        "text": "МИЛА", "fontSize": 30, "widthMm": 74, "thickMm": 3,
        "font": "mono", "shape": "pill", "color": "#ff7a1a", "ring": True, "border": False,
    })
    # имена/значения — уже Customizer-параметры .scad
    assert v["Text"] == "МИЛА"
    assert v["Length"] == 74 and v["Thickness"] == 3
    assert v["Text_size"] == round(30 / parametric.PREVIEW_PX_PER_MM, 2)   # px→мм как в превью
    assert v["Width"] == round((30 * 1.7 + 16) / parametric.PREVIEW_PX_PER_MM)  # высота из fontSize
    assert v["Shape"] == 2 and v["Ring"] == 1 and v["Border"] == 0          # pill / кольцо / без рамки
    assert v["Font"] == "Liberation Mono"                                   # семейство → шрифт OpenSCAD (кириллица)
    assert "color" not in v and "_color" not in v                          # цвет — косметика, в геометрию не идёт


def test_prepare_values_clamps_and_whitelists_customizer():
    m = _keychain()
    v = parametric.prepare_values(m, {
        "text": "Я" * 100,        # длинная строка → обрезается до MAX_TEXT_LEN
        "fontSize": 999,          # > max → 46
        "widthMm": 5,             # < min → 40
        "thickMm": 99,            # > max → 6
        "font": "evil()",         # не из options → дефолт round → "Noto Sans"
        "shape": "drop;",         # не из options → дефолт rounded → 1
        "ring": "true", "border": "1",  # строковые булевы → True
    })
    assert len(v["Text"]) == parametric.MAX_TEXT_LEN
    assert v["Text_size"] == round(46 / parametric.PREVIEW_PX_PER_MM, 2)
    assert v["Length"] == 30 and v["Thickness"] == 6   # widthMm < min(30) → 30 (авто-длина клампится широко)
    assert v["Font"] == "Liberation Sans" and v["Shape"] == 1
    assert v["Ring"] == 1 and v["Border"] == 1


def test_prepare_values_customizer_text_no_injection():
    """Текст из кастомайзера, попавший в Text, остаётся строковым литералом при сборке -D."""
    m = _keychain()
    v = parametric.prepare_values(m, {"text": 'a"; cube(50); s="b', "fontSize": 30, "widthMm": 60,
                                      "thickMm": 3, "font": "round", "shape": "rect",
                                      "color": "#fff", "ring": False, "border": False})
    schema = parametric.schema_for(m)
    out = parametric._fmt_value(schema["Text"], v["Text"])
    assert out.startswith('"') and out.endswith('"') and '\\"' in out   # экранировано, не код


def test_prepare_values_backward_compat_scad_native():
    """Старая форма шлёт «сырые» имена .scad → идёт через validate_values без маппинга."""
    m = _keychain()
    v = parametric.prepare_values(m, {"Text": "Аня", "Length": 9999, "Width": -5})
    assert v["Text"] == "Аня"
    assert v["Length"] == 230 and v["Width"] == 1          # клампы validate_values
    assert v["Text_size"] == 8                             # дефолт из .scad
    assert set(v) == {"Text", "Length", "Width", "Text_size"}   # только show-параметры


@pytest.mark.skipif(not parametric.openscad_available(), reason="OpenSCAD не установлен")
async def test_render_produces_stl_and_caches(tmp_path, monkeypatch):
    m = _keychain()
    monkeypatch.setattr(parametric.config, "PARAM_RENDER_DIR", tmp_path)
    vals = parametric.validate_values(m, {"Text": "Тест", "Length": 70})
    stl = await parametric.render(m, vals)
    assert stl.exists() and stl.stat().st_size > 0
    stl2 = await parametric.render(m, vals)     # те же параметры → тот же файл (кэш)
    assert stl2 == stl
    stl3 = await parametric.render(m, parametric.validate_values(m, {"Text": "Другой"}))
    assert stl3 != stl                          # другой текст → другой файл


@pytest.mark.skipif(not parametric.openscad_available(), reason="OpenSCAD не установлен")
async def test_render_customizer_shapes_and_toggles(tmp_path, monkeypatch):
    """Полный поток кастомайзера: схема фронта → prepare_values → реальный STL для всех форм
    и комбинаций кольцо/рамка/толщина."""
    m = _keychain()
    monkeypatch.setattr(parametric.config, "PARAM_RENDER_DIR", tmp_path)
    base = {"text": "АРТЁМ", "fontSize": 30, "widthMm": 74, "thickMm": 3,
            "font": "round", "color": "#2f6fd6"}
    cases = [
        {**base, "shape": "rect", "ring": True, "border": False},
        {**base, "shape": "rounded", "ring": False, "border": True},
        {**base, "shape": "pill", "ring": True, "border": True, "thickMm": 6},
    ]
    seen = set()
    for raw in cases:
        stl = await parametric.render(m, parametric.prepare_values(m, raw))
        assert stl.exists() and stl.stat().st_size > 0
        seen.add(stl)
    assert len(seen) == len(cases)              # каждая комбинация — свой STL


@pytest.mark.skipif(not parametric.openscad_available(), reason="OpenSCAD не установлен")
async def test_render_no_code_injection_via_text(tmp_path, monkeypatch):
    """Текст с попыткой инъекции OpenSCAD-кода идёт строковым ЛИТЕРАЛОМ (JSON-набор -p/-P) и НЕ
    исполняется — модель остаётся брелоком (≈4 мм по Z), а не «вырастает» от вставленного cube."""
    import trimesh
    m = _keychain()
    monkeypatch.setattr(parametric.config, "PARAM_RENDER_DIR", tmp_path)
    vals = parametric.prepare_values(m, {"text": 'X"; translate([0,0,40]) cube(40); s="', "fontSize": 30,
                                         "widthMm": 80, "thickMm": 3, "font": "round", "shape": "rect",
                                         "ring": False, "border": False})
    stl = await parametric.render(m, vals)
    mesh = trimesh.load(str(stl), force="mesh")
    z_height = float(mesh.bounds[1][2] - mesh.bounds[0][2])
    assert z_height < 12, f"высота {z_height} мм — похоже, инъекция добавила геометрию"


@pytest.mark.skipif(not parametric.openscad_available(), reason="OpenSCAD не установлен")
async def test_autosize_length_tracks_text_width(tmp_path, monkeypatch):
    """Length подгоняется под РЕАЛЬНУЮ ширину текста: больше символов → длиннее; широкие символы (Ш)
    дают длиннее, чем узкие (I) при равном числе (мерится реальная ширина, а не средняя оценка)."""
    m = _keychain()
    monkeypatch.setattr(parametric.config, "PARAM_RENDER_DIR", tmp_path)

    async def length(text):
        v = parametric.prepare_values(m, {"text": text, "fontSize": 30, "widthMm": 80, "thickMm": 3,
                                          "font": "round", "shape": "rect", "ring": True, "border": True})
        return (await parametric.autosize_keychain(m, v))["Length"]
    assert await length("АБВГДЕ") > await length("АБ")     # больше символов → длиннее
    assert await length("ШШШ") > await length("III")        # широкие символы длиннее узких


@pytest.mark.skipif(not parametric.openscad_available(), reason="OpenSCAD не установлен")
async def test_render_parts_splits_body_ink_euro(tmp_path, monkeypatch):
    """Многоцветный поток: модель режется на корпус (Part=1), краску текст+рамку (Part=2) и
    евробанд-полосу (Part=3) — все три STL валидны и отличаются от полной модели (Part=0)."""
    m = _keychain()
    monkeypatch.setattr(parametric.config, "PARAM_RENDER_DIR", tmp_path)
    vals = parametric.prepare_values(m, {"text": "АРТ", "fontSize": 32, "widthMm": 80, "thickMm": 3,
                                         "font": "round", "shape": "rounded", "ring": True, "border": True})
    body, ink, euro = await parametric.render_parts(m, vals)
    for part in (body, ink, euro):
        assert part is not None and part.exists() and part.stat().st_size > 0
    full = await parametric.render(m, vals)
    assert len({body, ink, euro, full}) == 4    # четыре разных STL (корпус / краска / полоса / полная)


@pytest.mark.skipif(not parametric.openscad_available(), reason="OpenSCAD не установлен")
async def test_render_colored_parts_has_all_colors(tmp_path, monkeypatch):
    """preview.render_colored_parts красит части в РАЗНЫЕ цвета — в картинке есть все три."""
    import numpy as np
    from PIL import Image

    from app import preview
    m = _keychain()
    monkeypatch.setattr(parametric.config, "PARAM_RENDER_DIR", tmp_path)
    monkeypatch.setattr(preview.config, "PREVIEW_DIR", tmp_path)
    vals = parametric.prepare_values(m, {"text": "АБ", "fontSize": 40, "widthMm": 90, "thickMm": 3,
                                         "font": "round", "shape": "rect", "ring": False, "border": True})
    body, ink, euro = await parametric.render_parts(m, vals)
    parts = [([str(body)], (0.9, 0.1, 0.1))]      # корпус — красный
    if euro:
        parts.append(([str(euro)], (0.1, 0.7, 0.2)))   # полоса — зелёная
    if ink:
        parts.append(([str(ink)], (0.1, 0.1, 0.9)))    # текст+рамка — синяя
    img = preview.render_colored_parts(parts)
    assert img is not None and img.exists()
    arr = np.asarray(Image.open(str(img)).convert("RGB")).reshape(-1, 3).astype(int)
    reddish = int(((arr[:, 0] - arr[:, 1] > 40) & (arr[:, 0] - arr[:, 2] > 40)).sum())
    greenish = int(((arr[:, 1] - arr[:, 0] > 30) & (arr[:, 1] - arr[:, 2] > 30)).sum())
    bluish = int(((arr[:, 2] - arr[:, 0] > 40) & (arr[:, 2] - arr[:, 1] > 40)).sum())
    assert reddish > 300, f"нет красного корпуса ({reddish})"
    assert greenish > 100, f"нет зелёной полосы ({greenish})"
    assert bluish > 30, f"нет синего текста/рамки ({bluish})"


# ─────────────────────────── QR-брелок (idea #5) ───────────────────────────

def test_qr_model_registered_and_plate_hidden():
    """QR-брелок есть в каталоге и помечен qr; табличка скрыта из старой формы (живёт в кастомайзере)."""
    qr = parametric.get_model("keychain_qr")
    assert qr and qr.get("qr") is True
    assert parametric.get_model("keychain_plate").get("hidden") is True


def test_qr_public_params_from_model_not_scad():
    """Параметры QR берутся из model['params'] (не из .scad — его нет)."""
    qr = parametric.get_model("keychain_qr")
    names = [p["name"] for p in parametric.public_params(qr)]
    assert names == ["Text", "Size"]


def test_qr_sanitize_clamps_and_keeps_text():
    """prepare_values для QR: текст сохраняется как ДАННЫЕ, размер/толщина клампятся."""
    qr = parametric.get_model("keychain_qr")
    v = parametric.prepare_values(qr, {"Text": "https://t.me/example", "Size": 999, "Ring": True})
    assert v["Text"] == "https://t.me/example"
    assert v["Size"] == 60.0 and 2.0 <= v["Thickness"] <= 6.0 and v["Ring"] == 1
    # текст — данные QR, инъекция невозможна (в OpenSCAD не попадает)
    v2 = parametric.prepare_values(qr, {"Text": 'a"; cube(50); s="b'})
    assert v2["Text"].startswith('a"; cube(50)')


async def test_qr_render_produces_stl_parts(tmp_path, monkeypatch):
    """QR генерится в Python (qrcode+trimesh) без OpenSCAD: целиком и по частям (база/модули)."""
    qr = parametric.get_model("keychain_qr")
    monkeypatch.setattr(parametric.config, "PARAM_RENDER_DIR", tmp_path)
    vals = parametric.prepare_values(qr, {"Text": "https://t.me/example", "Size": 40})
    whole = await parametric.render(qr, vals)
    base = await parametric.render(qr, {**vals, "Part": 1})
    mods = await parametric.render(qr, {**vals, "Part": 2})
    for p in (whole, base, mods):
        assert p.exists() and p.stat().st_size > 0
