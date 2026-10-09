"""Раскладка набора деталей по столам (OpenTodo defe5dc0, кейс «Ам Ням из Cut the Rope»):
смета показывала «1 стол» для набора, который печатается несколькими столами.

Два сигнала: полочная укладка XY-футпринтов (fastmeasure.estimate_plates) — когда детали
геометрически не помещаются на один стол; цветовые группы в именах файлов
(calcservice._filename_color_groups) — когда автор разбил набор по цветам («(Green) Body» +
«(White) Eyes» → одноцветный стол на цвет, печать без продувки)."""
from app import calcservice
from app.fastmeasure import estimate_plates


# ─────────────────────────── полочная укладка ───────────────────────────
def test_estimate_plates_trivial():
    assert estimate_plates([], 256.0) == 1
    assert estimate_plates([(50.0, 40.0)], 256.0) == 1


def test_estimate_plates_small_parts_share_bed():
    """9 мелких деталей Ам Няма (самая большая 108×84) геометрически влезают на один стол —
    укладка не должна раздувать число столов (за 3 стола тут отвечают цвета, не габариты)."""
    fps = [(108.6, 84.3), (25.3, 12.8), (49.4, 39.7), (9.7, 4.4), (12.3, 4.5),
           (9.7, 4.4), (12.2, 4.6), (7.3, 13.1), (7.3, 13.1)]
    assert estimate_plates(fps, 256.0) == 1


def test_estimate_plates_large_parts_split():
    """Детали ~150 мм: две такие рядом (300 мм + зазор) на стол 256 не встают — по столу на
    деталь. Детали ~100 мм: 2×2 на стол → 5 штук = 2 стола."""
    assert estimate_plates([(150.0, 150.0)] * 3, 256.0) == 3
    assert estimate_plates([(100.0, 100.0)] * 4, 256.0) == 1
    assert estimate_plates([(100.0, 100.0)] * 5, 256.0) == 2


def test_estimate_plates_oversize_part_counts_own_bed():
    """Негабарит (длиннее рабочей зоны в любой ориентации) считается отдельным столом —
    сам факт «не влезет вовсе» рисует DFM-вердикт, а не укладка."""
    assert estimate_plates([(300.0, 50.0), (50.0, 50.0)], 256.0) == 2


def test_estimate_plates_rotation():
    """Деталь 230×40 кладётся длинной стороной вдоль полки: 5 штук — это ленты по 48 мм
    высоты, все на одном столе (5×48 = 240 ≈ рабочая зона)."""
    assert estimate_plates([(40.0, 230.0)] * 5, 256.0) == 1


# ─────────────────────────── цветовые группы имён ───────────────────────────
def test_color_groups_om_nom():
    paths = ["obj_1_(Green) Body.stl", "obj_2_(Green) Top.stl", "obj_3_(White) Eyes.stl",
             "obj_4_(White) Teeth L1.stl.stl", "obj_5_(White) Teeth L2.stl.stl",
             "obj_6_(White) Teeth R1.stl.stl", "obj_7_(White) Teeth R2.stl.stl",
             "obj_8_(Black) Eye (Print 2)(1).stl", "obj_9_(Black) Eye (Print 2).stl"]
    assert calcservice._filename_color_groups(paths) == 3


def test_color_groups_requires_majority_and_two_colors():
    # один цвет — не разбивка
    assert calcservice._filename_color_groups(["(Red) a.stl", "(Red) b.stl"]) == 0
    # цветов два, но помечено меньшинство файлов — не доверяем
    assert calcservice._filename_color_groups(
        ["(Red) a.stl", "(Blue) b.stl", "c.stl", "d.stl", "e.stl"]) == 0
    # обычные имена без цветов
    assert calcservice._filename_color_groups(["body.stl", "head.stl", "arm(v2).stl"]) == 0


def test_color_groups_light_dark_prefix():
    assert calcservice._filename_color_groups(
        ["(Light Blue) wing.stl", "(Dark Blue) tail.stl", "(Red) body.stl"]) == 3


# ─────────────── дубль модели в карточке: полный 3mf + STL-россыпь «Plate N» ───────────────
def test_drop_redundant_plate_stls(tmp_path):
    """Фалкон/Валли (аудит избранного 24.07): карточка несёт полный многостольный .3mf ПЛЮС
    те же столы россыпью «Plate N - Color.stl» — вес/детали/цена удваивались. При живом 3mf
    плитные STL выкидываются из расчёта; чистая STL-россыпь (без 3mf) не трогается."""
    from app.inspect3mf import drop_redundant_plate_stls as drop
    falcon = ["Plate 1 - Muted White.stl", "Plate 2 - Muted White.stl",
              "Plate 8 - Arctic Teal.stl", "Millennium Falcon 3MF (For Bambu Printers).3mf"]
    assert drop(falcon) == ["Millennium Falcon 3MF (For Bambu Printers).3mf"]
    # регистр/подчёркивание/«PRINT STRONG» в имени — всё равно плитный STL (Валли: .STL)
    assert drop(["Plate 18 - PRINT STRONG - Dark Gray or Black.STL", "w.3mf"]) == ["w.3mf"]
    assert drop(["plate_2 arm.stl", "m.3mf"]) == ["m.3mf"]
    # чистая россыпь без 3mf (Дарт Вейдер 5236) — модель и есть россыпь, не трогаем
    vader = ["Plate 1 - Charcoal Black.stl", "Plate 2 - Fossil Gray.stl"]
    assert drop(vader) == vader
    # 3mf + обычные STL (не «Plate N») — не трогаем (это разные детали, не дубль)
    mixed = ["gold5.stl", "red1.stl", "iron man.3mf"]
    assert drop(mixed) == mixed
    # одиночный 3mf — как был
    assert drop(["Timmy+No+AMS.3mf"]) == ["Timmy+No+AMS.3mf"]
