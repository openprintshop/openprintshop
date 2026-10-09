"""Любимый цвет клиента (ux-paths-next.md, идея 4): последний выбранный цвет запоминается
и подставляется первым в палитре."""


async def test_set_get_last_color_lazy_row(tmp_db):
    assert await tmp_db.get_user_last_color(777) == ""      # нет строки клиента → пусто
    await tmp_db.set_user_last_color(777, "Тёмно-синий")     # лениво создаёт строку
    assert await tmp_db.get_user_last_color(777) == "Тёмно-синий"
    await tmp_db.set_user_last_color(777, "Красный")         # перезапись
    assert await tmp_db.get_user_last_color(777) == "Красный"
    await tmp_db.set_user_last_color(777, "   ")             # пустое/пробелы — не затирает
    assert await tmp_db.get_user_last_color(777) == "Красный"


async def test_last_color_does_not_clobber_other_customer_fields(tmp_db):
    """Запоминание цвета не ломает уже сохранённые поля клиента (name/phone)."""
    await tmp_db.upsert_customer(555, name="Иван", phone="+375...")
    await tmp_db.set_user_last_color(555, "Зелёный")
    c = await tmp_db.get_customer(555)
    assert c["name"] == "Иван" and c["phone"] == "+375..." and c["last_color"] == "Зелёный"
