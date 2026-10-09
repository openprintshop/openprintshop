"""Выбор столов мультиплейт-модели (OpenTodo 77dc9b01): парсинг выбора, кэш-ключ, ярлык заказа."""
from app import cache, webapp


def test_parse_plates_sel_valid_subset():
    assert webapp._parse_plates_sel([2, 0], 5) == [0, 2]
    assert webapp._parse_plates_sel([1], 2) == [1]


def test_parse_plates_sel_full_or_invalid_means_all():
    assert webapp._parse_plates_sel([0, 1, 2], 3) is None          # полный набор = считать целиком
    assert webapp._parse_plates_sel([], 3) is None                 # пусто
    assert webapp._parse_plates_sel([7, -1], 3) is None            # все индексы мимо
    assert webapp._parse_plates_sel("0,1", 3) is None              # не список
    assert webapp._parse_plates_sel([0], 1) is None                # одностольная модель
    assert webapp._parse_plates_sel([0, 0, 1], 2) is None          # дубли → фактически полный набор


def test_parse_plates_sel_clamps_out_of_range():
    assert webapp._parse_plates_sel([0, 99], 3) == [0]             # мусорный индекс отбрасывается


def test_cache_key_distinguishes_plate_subsets(tmp_path):
    f = tmp_path / "m.3mf"
    f.write_bytes(b"model-bytes")
    kw = dict(machine_profile="m", process="p", layer_height=0.2, infill=15, walls=3, filament="f")
    full = cache.make_key([f], **kw)
    sel02 = cache.make_key([f], extra="plates:0,2", **kw)
    sel01 = cache.make_key([f], extra="plates:0,1", **kw)
    assert full != sel02 and full != sel01 and sel02 != sel01
    # пустой extra не меняет существующие ключи (кэш полных расчётов выживает)
    assert cache.make_key([f], extra="", **kw) == full

