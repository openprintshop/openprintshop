"""Проводка бота: run.wire_bot() обязан дойти до КАЖДОГО модуля с модульным `_bot`.

Забытый set_bot — самый тихий отказ в этом проекте: все ветки отправки закрыты проверкой
`if _bot is None`, поэтому модуль не падает и даже не пишет в лог — клиент просто перестаёт
получать сообщения (таймлапсы, алерты), и замечают это через сутки. До волны 3 проводку
не проверяло ничто.

Тест намеренно НЕ перечисляет модули руками: он обходит уже импортированные `app.*` и требует
бота у каждого, кто держит модульный `_bot` и умеет `set_bot`. Значит следующий разрез
webapp.py, который заведёт себе такой же модульный бот и забудет строчку в wire_bot,
уронит этот файл, а не тишину у клиента.
"""
import ast
import inspect
import sys

import run
from app import webapp

# webapp держит модульный `_bot`, но проводит его САМ — в webapp.start(), под
# config.WEBAPP_ENABLED. Тянуть его в wire_bot нельзя: у студии с выключенной мини-аппой
# ожили бы ветки, которые год молчали (быстрая смета по ссылке из чата, «оцените печать»
# с доставкой таймлапса, заказ из чата, реферальная награда) — все они достижимы из Telegram
# без веб-сервера. Это продуктовое решение, а не побочный эффект переноса кода.
# С волны 12 endgame (вариант «б» вопроса №1) так же ведут себя модули веера
# webapp._BOT_FAN: их `_bot` раздаёт webapp.set_bot из webapp.start(), а не wire_bot.
# Полноту самого веера сторожит test_webapp_set_bot_reaches_every_web_holder ниже.


def _wires_itself() -> set:
    return {"app.webapp"} | {m.__name__ for m in webapp._BOT_FAN}


def _bot_holders() -> dict:
    """Импортированные модули app.*, которые держат бота в модульной переменной."""
    wired = _wires_itself()
    return {name: mod for name, mod in list(sys.modules.items())
            if name.startswith("app.") and mod is not None
            and name not in wired
            and hasattr(mod, "_bot") and callable(getattr(mod, "set_bot", None))}


def _web_bot_holders() -> dict:
    """Импортированные модули app.web.*, которые держат бота в модульной переменной."""
    return {name: mod for name, mod in list(sys.modules.items())
            if name.startswith("app.web.") and mod is not None
            and hasattr(mod, "_bot") and callable(getattr(mod, "set_bot", None))}


def test_webapp_set_bot_reaches_every_web_holder():
    """Прибор полноты веера (волна 12): app.web.* с модульным `_bot`, забытый в
    webapp._BOT_FAN, должен ронять ЭТОТ тест, а не тишину у клиента. Обход по
    sys.modules намеренно не перечисляет модули руками — как у wire_bot выше."""
    holders = _web_bot_holders()
    fan = {m.__name__ for m in webapp._BOT_FAN}
    assert fan == set(holders), (
        f"веер webapp._BOT_FAN разошёлся с фактическими держателями: "
        f"в веере лишние {sorted(fan - set(holders))}, "
        f"забыты {sorted(set(holders) - fan)}")
    sentinel = object()
    prev = webapp.set_bot(sentinel)
    try:
        missed = sorted(n for n, m in holders.items() if m._bot is not sentinel)
        assert not missed, (f"webapp.set_bot не поставил бота модулям {missed} — их ветки "
                            f"отправки закрыты `if _bot is None` и промолчат")
        assert webapp._bot is sentinel
    finally:
        webapp.set_bot(prev)


def test_main_actually_calls_wire_bot():
    """Сам ВЫЗОВ проводки тоже под присмотром, иначе тест ниже закрывает половину сценария.

    wire_bot зовётся из main() ровно одной строкой. Унеси её следующая волна, двигающая старт, —
    и оба теста проводки останутся зелёными, а printmedia._bot будет None: доставка таймлапсов
    замолчит без единой строки в логе. Проверяем по дереву разбора, а не грепом по тексту:
    греп поймался бы и на упоминании в комментарии."""
    tree = ast.parse(inspect.getsource(run))
    main = next((n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main"), None)
    assert main is not None, "в run.py не стало функции main — проверку надо переписать, а не удалять"
    called = {n.func.id for n in ast.walk(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "wire_bot" in called, ("run.main() больше не зовёт wire_bot — модульные `_bot` останутся "
                                  "None, и отправка клиенту молча выключится")


def test_wire_bot_reaches_every_module_that_holds_a_bot():
    holders = _bot_holders()
    # известные на момент волны 3; проверка не даёт тесту выродиться в пустой обход
    assert {"app.notify", "app.printmedia"} <= set(holders), \
        f"ожидались notify/printmedia, вижу {sorted(holders)}"

    saved = {name: mod._bot for name, mod in holders.items()}
    sentinel = object()
    try:
        run.wire_bot(sentinel)
        missed = sorted(name for name, mod in holders.items() if mod._bot is not sentinel)
        assert not missed, (f"run.wire_bot не поставил бота модулям {missed} — их ветки отправки "
                            f"закрыты `if _bot is None` и промолчат без единой строки в логе")
    finally:
        for name, mod in holders.items():
            mod.set_bot(saved[name])


def test_wire_bot_is_idempotent():
    """Повторный вызов ничего не ломает: старт зовёт проводку до поднятия веб-слоя."""
    holders = _bot_holders()
    saved = {name: mod._bot for name, mod in holders.items()}
    sentinel = object()
    try:
        run.wire_bot(sentinel)
        run.wire_bot(sentinel)
        assert all(mod._bot is sentinel for mod in holders.values())
    finally:
        for name, mod in holders.items():
            mod.set_bot(saved[name])
