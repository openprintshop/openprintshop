"""Общие помощники после выноса в app/web/common.py: фасад, ЗАМОРОЖЕННЫЕ префиксы, `_bad`.

ЗАЧЕМ ЭТОТ ФАЙЛ. Волна 4 цикла 2 вынесла из app/webapp.py девять сквозных помощников и
завела рядом три новые функции подписи — `sig`, `check_sig`, `signed_url`. Снимок маршрутов
(tests/test_routes_inventory.py) на это не реагирует вовсе: пути и имена обработчиков не
менялись. Поэтому три поломки закрываются только здесь.

  * ЗАБЫТОЕ ИМЯ В ФАСАДЕ либо оставшаяся в webapp вторая копия. Импорт от этого не падает:
    `_color_css` или `_served_img`, потерянные в фасаде, дадут NameError не на старте, а при
    первом же открытии коллекции у живого клиента. `getattr(webapp, n) is getattr(common, n)`
    ловит и пропажу, и вторую копию разом.

  * ОПЕЧАТКА В ПРЕФИКСЕ ПОДПИСИ. Это худшее, что может случиться с этим шагом, и починить это
    задним числом НЕЛЬЗЯ: подписанные ссылки уже разосланы — они лежат в старых сообщениях
    Telegram, а `/csample` ещё и напечатан на QR-этикетках, наклеенных на физические образцы
    калибровки. Опечатка не падает и не логируется: подпись просто становится другой, и все
    выданные раньше ссылки начинают отвечать 403/404. Ниже — ЗОЛОТАЯ таблица: одиннадцать
    подписей, посчитанных на фиксированном токене ДО переноса. Любая правка префикса, входной
    строки, алгоритма или длины среза красит эти тесты.

  * РАЗЪЕХАВШИЕСЯ СБОРЩИК И ПРОВЕРЯЮЩИЙ. Раньше строку подписи набирали дважды — в месте
    сборки URL и в месте проверки, — и разойтись они могли молча. Тест гоняет каждый префикс
    туда-обратно: то, что собрал `signed_url`, обязано приниматься `check_sig`, а соседний
    ident — отвергаться.

Плюс здесь закреплён проводной вид ответа `_bad()`: этим шагом на него переведены 79
инлайновых литералов, и клиентам не должно было прилететь ничего нового.
"""
import ast
import io
import os
import pathlib
import subprocess
import sys

import pytest
from conftest import get_req

from app import config, webapp
from app.web import common

REPO = pathlib.Path(__file__).resolve().parents[1]

# Имена, которые app/webapp.py обязан реэкспортировать из app/web/common.py. Список ведётся
# руками именно потому, что он и есть контракт фасада.
FACADE = ("_toint", "_fnum", "_num", "_bad", "_ago", "_color_css",
          "_served_img", "_img_headers", "_to_webp", "_maybe_webp",
          "sig", "check_sig", "signed_url",
          # приехали сюда следующим шагом волны, вместе с выносом калибровки: их зовут и
          # webapp, и app/web/calib.py, поэтому дом им нужен такой, который не тянет ни того,
          # ни другого (иначе разрез калибровки упирается в цикл)
          "_photo_url", "_print_settings_view",
          # волна 1b endgame-плана: сборщики фото/аватарки/товара — тем же правилом, что и
          # _photo_url (их будут звать и webapp, и будущие web/*-домены: витрина, CRM)
          "_spool_photo_path", "_avatar_url", "_product_photo_url")

# ЗАМОРОЖЕННЫЕ ПОДПИСИ. Значения посчитаны тем же hmac-sha256[:12] на токене _TOKEN до переноса
# (см. app/websign.py::_sign). Таблица описывает ВСЕ одиннадцать живых префиксов: восемь
# «путевых» из common._SIGNED_ROUTES и три составных из common._SIGNED_COMPOSITE.
_TOKEN = "123456:FROZEN-TEST-TOKEN"
FROZEN = {
    ("av", "77"): "14515e4c0f10",
    ("req", "5"): "a913e7106999",
    ("jtl", "9"): "ec097b816f40",
    ("csample", "12"): "1d6bb5bd7864",
    ("prod", "3"): "c2f5ae5ef78a",
    ("slicer", "5"): "6163a3041efb",
    ("tl", "5"): "26e140deef53",
    ("sm", "8"): "65e2feed9d22",
    ("cust", "5"): "40af6398ac19",
    ("batch", "b7f2"): "1de45fe061a8",
    ("aphoto", "calib12"): "7c9e52fec6e2",
}

# Полный вид ссылки, как её увидит клиент. Путь скопирован из app/webapp.py::make_app
# (router.add_get) — то есть проверяется не только подпись, но и что ссылка ведёт на
# зарегистрированную ручку.
FROZEN_URLS = {
    ("av", 77): "/avatar/77?s=14515e4c0f10",
    ("req", 5): "/reqprev/5?s=a913e7106999",
    ("jtl", 9): "/jtl/9?s=ec097b816f40",
    ("csample", 12): "/csample/12?s=1d6bb5bd7864",
    ("prod", 3): "/prodphoto/3?s=c2f5ae5ef78a",
    ("slicer", 5): "/slicershot/5?s=6163a3041efb",
    ("tl", 5): "/timelapse/5?s=26e140deef53",
    ("sm", 8): "/smprev/8?s=65e2feed9d22",
}


@pytest.fixture
def frozen_token(monkeypatch):
    """Токен бота — фиксированный: подпись обязана зависеть только от него и от входной строки."""
    monkeypatch.setattr(config, "BOT_TOKEN", _TOKEN)


# ─────────────────────────────── фасад ───────────────────────────────

@pytest.mark.parametrize("name", FACADE)
def test_facade_reexports_the_same_object(name):
    """webapp.<имя> — ТОТ ЖЕ объект, что и app.web.common.<имя>, а не копия."""
    assert hasattr(webapp, name), f"имя {name} потеряно в фасаде app/webapp.py"
    assert getattr(webapp, name) is getattr(common, name), (
        f"webapp.{name} — не тот объект, что common.{name}: "
        "либо в webapp осталась вторая копия, либо фасад импортирует что-то другое")


def test_no_leftover_definitions_in_webapp():
    """В app/webapp.py не осталось СОБСТВЕННЫХ определений перенесённых имён.

    Вторая копия не ломает ни импорт, ни тесты, которые ходят через webapp.*, — но с этого
    момента правка живёт в одном файле, а работает другой."""
    src = io.open(REPO / "app" / "webapp.py", encoding="utf-8").read()
    own = {n.name for n in ast.parse(src).body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FACADE}
    assert not own, f"в webapp.py остались определения: {sorted(own)}"


def test_common_does_not_import_webapp():
    """app/web/common.py не тянет webapp — иначе доменные разрезы получат цикл.

    Отдельный процесс: в текущем webapp уже импортирован, и sys.modules ничего не докажет."""
    code = ("import app.web.common, sys; "
            "print(int('app.webapp' in sys.modules), int('app.webguard' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    assert out == ["0", "0"], f"app.web.common подтянул webapp/webguard: {out}"


# ─────────────────────────── замороженные префиксы ───────────────────────────

@pytest.mark.parametrize(("key", "expected"), sorted(FROZEN.items()))
def test_signature_prefixes_are_frozen(frozen_token, key, expected):
    """Подпись префикса не менялась с момента выноса.

    Красный тест здесь означает не «поправь ожидание», а «ты только что сломал все ссылки
    этого вида, которые уже у клиентов»."""
    prefix, ident = key
    assert common.sig(prefix, ident) == expected


@pytest.mark.parametrize(("key", "expected"), sorted(FROZEN_URLS.items()))
def test_signed_url_matches_frozen_link(frozen_token, key, expected):
    """signed_url собирает ровно ту ссылку, что рассылалась до переноса (путь + подпись)."""
    prefix, ident = key
    assert common.signed_url(prefix, ident) == expected


def test_signed_url_appends_extra_after_signature(frozen_token):
    """Версия файла идёт ПОСЛЕ подписи: `?s=…&v=…`, как во всех выданных ссылках."""
    assert common.signed_url("req", 5, {"v": 1700000000}) == \
        "/reqprev/5?s=a913e7106999&v=1700000000"


def test_photo_url_link_is_frozen(frozen_token, tmp_path):
    """Ссылка на фото собирается ровно как до переноса: /admin/photo/{kind}/{id}?s=…&v=mtime.

    Заморожена ОТДЕЛЬНО, потому что статический скан префиксов ниже этот файл не смотрит
    (он и есть дом подписи), а `_photo_url` переехал сюда вместе с выносом калибровки —
    его вызов `sig("aphoto", …)` иначе не проверялся бы ничем. Ссылки этого вида разосланы:
    их отдаёт и карточка образца калибровки, и страница /csample с QR-этикетки."""
    f = tmp_path / "sample.jpg"
    f.write_bytes(b"x")
    os.utime(f, (1700000000, 1700000000))
    assert common._photo_url("calib", 12, str(f)) == \
        "/admin/photo/calib/12?s=7c9e52fec6e2&v=1700000000"
    assert common._photo_url("calib", 12, str(tmp_path / "нет.jpg")) is None
    assert common._photo_url("calib", 12, None) is None


def test_frozen_table_covers_every_known_prefix():
    """Таблица заморозки полна: новый префикс нельзя завести, не заморозив его подпись."""
    assert {p for p, _ in FROZEN} == set(common._SIG_PREFIXES)
    assert {p for p, _ in FROZEN_URLS} == set(common._SIGNED_ROUTES)


# ───────────────────── префиксы на местах вызова (статически) ─────────────────────

_SIG_CALLS = {"sig": 0, "signed_url": 0, "check_sig": 1}   # позиция аргумента с префиксом


def prefix_call_sites(src: str):
    """Все вызовы sig/check_sig/signed_url в исходнике → [(имя, префикс|None, строка)].

    Префикс = None означает «аргумент не строковый литерал», то есть посчитан на лету и
    статически не читается. Вынесено отдельной функцией, чтобы поломку можно было
    смоделировать на изменённом тексте, не трогая файл на диске."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        pos = _SIG_CALLS.get(node.func.id)
        if pos is None or len(node.args) <= pos:
            continue
        arg = node.args[pos]
        val = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
        out.append((node.func.id, val, node.lineno))
    return out


# Два файла, где подпись живёт по праву и потому из скана исключены: websign.py её считает,
# web/common.py оборачивает (там prefix — параметр, а не литерал). Оба закреплены выше
# таблицами заморозки; всему остальному коду руками подписывать нечего.
SIGNING_HOME = ("app/websign.py", "app/web/common.py")


def app_sources():
    """Весь пакет app/, кроме двух файлов подписи. Сканируется ЦЕЛИКОМ нарочно.

    Подписанные ссылки собирает не только веб-слой: app/payments.py кладёт превью заказа в
    счёт Telegram. И следующие волны увозят домены в app/web/*.py. Скан по одному webapp.py
    молча переставал бы покрывать всё, что уехало."""
    out = {}
    for path in sorted(REPO.glob("app/**/*.py")):
        name = path.relative_to(REPO).as_posix()
        if name not in SIGNING_HOME:
            out[name] = io.open(path, encoding="utf-8").read()
    return out


def test_every_call_site_uses_a_known_prefix():
    """Ни одного вызова с чужим или вычисляемым префиксом во всём пакете app/.

    Это единственная проверка, которая ловит опечатку в префиксе НА МЕСТЕ ВЫЗОВА. Без неё
    `signed_url("avv", …)` жил бы до первого запроса конкретной ручки: sig() бросит KeyError,
    но только когда клиент туда придёт. Заодно требуется строковый литерал: вычисляемый
    префикс нельзя ни прочитать глазами, ни проверить здесь."""
    bad, total = [], 0
    for name, src in app_sources().items():
        sites = prefix_call_sites(src)
        total += len(sites)
        bad += [(name, n, p, ln) for n, p, ln in sites if p not in common._SIG_PREFIXES]
    assert total, "вызовы sig/check_sig/signed_url не нашлись — проверка перестала что-либо ловить"
    assert not bad, ("префикс подписи неизвестен либо не литерал: " + str(bad) +
                     f"; известны {sorted(common._SIG_PREFIXES)}")


def test_no_hand_rolled_signing_left_in_app():
    """Прямых вызовов `_sign(` во всём app/ осталось ровно два — оба у прокси внешних превью.

    Там подписывается ЦЕЛЫЙ внешний URL, а не «префикс + id», и своей ручки с {ident} у него
    нет. Всё остальное обязано идти через sig()/signed_url(): собранная руками подпись — это
    ровно тот способ, которым префикс расходится с проверяющим. Так, до этого шага счёт
    Telegram (app/payments.py) собирал /reqprev вручную, повторяя префикс «req» за webapp."""
    holders = []
    for name, src in app_sources().items():
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                   and c.func.id == "_sign" for c in ast.walk(node)):
                holders.append(f"{name}::{node.name}")
    assert sorted(holders) == ["app/web/storefront.py::_api_thumb",
                               "app/web/storefront.py::_thumb_proxy"], (
        "появилась ручная подпись вне прокси превью: " + str(sorted(holders)) +
        " — собирайте ссылку через signed_url(), а проверяйте через check_sig()")


# Какой обработчик какой префикс проверяет. Замыкает круг: путь ↔ префикс заморожен таблицей
# FROZEN_URLS, обработчик ↔ путь — снимком маршрутов (tests/test_routes_inventory.py), а здесь
# обработчик ↔ префикс. Без этой строчки перепутанные местами СУЩЕСТВУЮЩИЕ префиксы (jtl вместо
# tl — оба живые, оба про таймлапс) прошли бы мимо всех трёх проверок: sig() не ругается, путь
# зарегистрирован, снимок цел. Замер зондом: из одиннадцати обработчиков сквозной тест есть
# ровно у одного (_csample_page), у остальных десяти поломка проверяющего не краснеет ничем.
HANDLER_PREFIX = {
    "_avatar_file": "av",
    "_jtl_file": "jtl",
    "_csample_page": "csample",
    "_product_photo_file": "prod",
    "_request_preview": "req",
    "_slicer_shot_file": "slicer",
    "_timelapse_file": "tl",
    "_custom_photo": "cust",
    "_saved_preview": "sm",
    "_batch_preview": "batch",
    "_admin_photo_file": "aphoto",
}


def test_each_handler_checks_its_own_prefix():
    """Каждая подписанная ручка проверяет ИМЕННО свой префикс, и других проверок в ней нет.

    Ручку ищем по всему пакету: когда следующая волна увезёт калибровку в app/web/calib.py,
    `_csample_page` просто найдётся там, и правки тесту не потребуется."""
    found = {}
    for src in app_sources().values():
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in HANDLER_PREFIX:
                continue
            found[node.name] = sorted(
                p for n, p, _ in prefix_call_sites(ast.unparse(node)) if n == "check_sig")
    assert found == {name: [pref] for name, pref in HANDLER_PREFIX.items()}, (
        "обработчик проверяет не свой префикс (или проверка из него пропала): " + str(found))


# ─────────────────────── сборщик и проверяющий заодно ───────────────────────

@pytest.mark.parametrize(("prefix", "ident"), sorted(FROZEN))
def test_check_sig_accepts_own_signature(frozen_token, prefix, ident):
    """Что подписал sig — то check_sig принимает; чужой ident — отвергает."""
    good = common.sig(prefix, ident)
    assert common.check_sig(get_req({"s": good}), prefix, ident)
    assert not common.check_sig(get_req({"s": good}), prefix, str(ident) + "0")
    assert not common.check_sig(get_req(), prefix, ident)


def test_check_sig_rejects_signature_of_another_prefix(frozen_token):
    """Префикс — часть подписи: подписью аватарки нельзя открыть превью заказа с тем же id."""
    assert not common.check_sig(get_req({"s": common.sig("av", 5)}), "req", 5)


def test_unknown_prefix_is_loud(frozen_token):
    """Опечатка в префиксе обязана падать, а не считать «почти правильную» подпись молча."""
    with pytest.raises(KeyError):
        common.sig("reqq", 5)
    with pytest.raises(KeyError):
        common.signed_url("reqq", 5)
    with pytest.raises(KeyError):
        common.check_sig(get_req({"s": "x"}), "reqq", 5)


def test_ident_is_stringified_the_same_way(frozen_token):
    """int и str одного id дают одну подпись — хендлеры берут id из match_info строкой,
    а сборщики ссылок — числом из БД. Разойтись они не должны."""
    assert common.sig("req", 5) == common.sig("req", "5")
    assert common.check_sig(get_req({"s": common.signed_url("req", 5)[-12:]}), "req", "5")


# ─────────────────────────────── _bad ───────────────────────────────

def test_bad_keeps_the_wire_format():
    """Тело и статус ответа `_bad()` — те же, что у 79 заменённых инлайновых литералов."""
    resp = common._bad()
    assert resp.status == 400
    assert resp.body == b'{"ok": false, "error": "bad request"}'


def test_bad_returns_a_fresh_response_each_call():
    """Каждый вызов даёт свой объект: aiohttp отдаёт Response один раз, общий сломал бы вторую
    отдачу. Литерал в 79 местах это свойство имел, и оно обязано было сохраниться."""
    assert common._bad() is not common._bad()
