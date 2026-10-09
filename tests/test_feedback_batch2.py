"""Фидбэк-батч 2: многоцвет столов, очередь разбора, большой словарь промтов."""
import random
import zipfile

from app import avatar_words, avatars, inspect3mf

_SETTINGS_MC = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="5">
    <metadata key="extruder" value="2"/>
    <part id="1" subtype="normal_part"><metadata key="extruder" value="15"/></part>
    <part id="2" subtype="normal_part"><metadata key="extruder" value="2"/></part>
  </object>
  <object id="6">
    <metadata key="extruder" value="4"/>
    <part id="1" subtype="normal_part"><metadata key="extruder" value="4"/></part>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <model_instance><metadata key="object_id" value="5"/></model_instance>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <model_instance><metadata key="object_id" value="6"/></model_instance>
  </plate>
</config>"""


def test_plate_multicolor(tmp_path):
    p = tmp_path / "m.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/model_settings.config", _SETTINGS_MC)
        z.writestr("3D/3dmodel.model", "<model/>")
    assert inspect3mf.plate_multicolor(p) == [True, False]   # стол 1: экструдеры {15,2}; стол 2: {4}
    assert inspect3mf.plate_multicolor(tmp_path / "нет.3mf") == []


async def test_calc_queue_roundtrip(tmp_db):
    db = tmp_db
    qid = await db.add_calc_queue(7, "@user", "https://example.com/m", "капча")
    assert await db.count_calc_queue() == 1
    items = await db.list_calc_queue()
    assert items[0]["id"] == qid and items[0]["url"] == "https://example.com/m"
    await db.resolve_calc_queue(qid, "готово, качайте")
    assert await db.count_calc_queue() == 0
    q = await db.get_calc_queue(qid)
    assert q["status"] == "done" and q["note"] == "готово, качайте"
    assert await db.list_calc_queue() == []                      # new-список пуст
    assert len(await db.list_calc_queue(include_done=True)) == 1


def test_wordbank_huge_and_avoids_repeat():
    assert len(avatar_words.ADJ) >= 100
    assert len(avatar_words.CREATURE) >= 150
    assert len(avatar_words.EXTRA) >= 90
    # анти-повтор: существо из avoid не выпадает подряд (на любом сиде из сотни)
    rng = random.Random(1)
    prev = "бешеный робот-тостер в наушниках"
    for _ in range(100):
        p = avatars._wordbank_prompt(rng, avoid=prev)
        assert "робот-тостер" not in p
    # вариативность: 200 промтов почти не повторяются
    rng2 = random.Random(2)
    prompts = {avatars._wordbank_prompt(rng2) for _ in range(200)}
    assert len(prompts) >= 195


async def test_print_journal(tmp_db):
    """Журнал с ползунком брака (60e0b2a8): fail_pct 0–100, % в сводке взвешен по граммам,
    старый бинарный result принимается как фолбэк (fail → 100%)."""
    db = tmp_db
    await db.add_print_journal(label="Кот", plastic_id=None, plastic_name="PLA",
                               grams=30, minutes=60, result="ok")
    await db.add_print_journal(label="Кронштейн", plastic_id=None, plastic_name="PETG",
                               grams=40, minutes=90, result="fail", fail_reason="отлип")
    st = await db.print_journal_stats()
    # брак: 40 г из 70 → 57% взвешенно, в мусор 40 г
    assert st["total"] == 2 and st["fails"] == 1
    assert st["fail_pct"] == 57 and st["waste"] == 40
    assert {x["plastic"] for x in st["by_plastic"]} == {"PLA", "PETG"}
    items = await db.list_print_journal()
    assert items[0]["label"] == "Кронштейн" and items[0]["fail_reason"] == "отлип"
    assert items[0]["fail_pct"] == 100 and items[1]["fail_pct"] == 0
    await db.delete_print_journal(items[0]["id"])
    assert (await db.print_journal_stats())["total"] == 1


async def test_print_journal_partial_fail_and_update(tmp_db):
    """Частичный брак ползунком + правка прошлой записи (60e0b2a8)."""
    db = tmp_db
    jid = await db.add_print_journal(label="Шестерёнки х6", plastic_id=None,
                                     plastic_name="PLA", grams=90, minutes=200, fail_pct=30,
                                     fail_reason="две отлипли")
    st = await db.print_journal_stats()
    assert st["fails"] == 1 and st["waste"] == 27 and st["fail_pct"] == 30   # 90×30%
    # правка: на деле брак 50% и граммы больше
    ok = await db.update_print_journal(jid, label="Шестерёнки х6", plastic_id=None,
                                       plastic_name="PLA", grams=100, minutes=200,
                                       fail_pct=50, fail_reason="три отлипли")
    assert ok
    it = (await db.list_print_journal())[0]
    assert it["grams"] == 100 and it["fail_pct"] == 50 and it["result"] == "fail"
    assert it["fail_reason"] == "три отлипли"
    st = await db.print_journal_stats()
    assert st["waste"] == 50 and st["fail_pct"] == 50
    # ползунок нормализуется в 0..100; несуществующий id → False
    await db.update_print_journal(jid, label="x", plastic_id=None, plastic_name="PLA",
                                  grams=10, minutes=1, fail_pct=250)
    assert (await db.list_print_journal())[0]["fail_pct"] == 100
    assert not await db.update_print_journal(99999, label="x", plastic_id=None,
                                             plastic_name="", grams=1, minutes=1, fail_pct=0)


async def test_activity_name_fallback(tmp_db):
    db = tmp_db
    await db.add_request(user_id=42, user_name="@vasya", model_label="m", printer="p",
                         plastic_name="x", quantity=1, quality="q", strength="s",
                         grams_total=1, minutes_total=1, price=1, preview_path=None)
    await db.log_event(42, "тест", "без имени")           # имя подтянется из заказов
    ev = (await db.list_activity())[0]
    assert ev["name"] == "@vasya"
