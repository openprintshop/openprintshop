"""Оттенки админа в materials.color_to_rgbs: составное имя («Тёмно-синий») должно
краситься оттенком целиком, а не распадаться по тире на «тёмно»+«синий» → встроенный синий."""
from app import materials

DARK_BLUE = (0x1b / 255, 0x2a / 255, 0x4a / 255)
BUILTIN_BLUE = materials.COLOR_RGB["син"]


def test_full_name_shade_beats_hyphen_split():
    ov = {"темно-синий": DARK_BLUE}
    assert materials.color_to_rgbs("Темно-синий", ov) == [DARK_BLUE]


def test_full_name_shade_yo_insensitive_both_ways():
    # оттенок записан с «ё», цвет пластика — без (и наоборот)
    assert materials.color_to_rgbs("Темно-синий", {"тёмно-синий": DARK_BLUE}) == [DARK_BLUE]
    assert materials.color_to_rgbs("Тёмно-синий", {"темно-синий": DARK_BLUE}) == [DARK_BLUE]


def test_without_shade_falls_back_to_builtin_blue():
    # без оттенка админа поведение прежнее: «синий» из встроенной таблицы
    assert materials.color_to_rgbs("Тёмно-синий") == [BUILTIN_BLUE]


def test_gradient_split_still_works_with_part_override():
    ov = {"красный": (1.0, 0.0, 0.0)}
    rgbs = materials.color_to_rgbs("зелёный-красный-синий", ov)
    assert len(rgbs) == 3
    assert rgbs[1] == (1.0, 0.0, 0.0)          # часть градиента берёт оттенок админа
    assert rgbs[2] == BUILTIN_BLUE


def test_builtin_yo_folding():
    assert materials.color_to_rgb("Чёрный") == materials.color_to_rgb("Черный")
    assert materials.color_to_rgb("Жёлтый") == materials.color_to_rgb("Желтый")
