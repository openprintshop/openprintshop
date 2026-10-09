"""Кто имеет право читать /api/lcd/data.

Ручка кормит дашборд на настольном экране TURZX и отдаёт три вещи, которых наружу быть не
должно: состояние принтера (что печатается прямо сейчас), очередь ненапечатанных заказов
С ИМЕНАМИ КЛИЕНТОВ и метрики машины владельца (загрузка CPU/GPU, температуры, память).

Защита была только по ключу — `if _LCD_KEY and ...`, — а сам `LCD_KEY` берётся из окружения
и по умолчанию ПУСТ. То есть при незаданном ключе (боевая конфигурация) условие вырождалось в
`if False` и ручка была открыта каждому, кто дотянулся до порта. Проверено запросом к живому
боту: `curl -H "Host: example.com" http://127.0.0.1:8081/api/lcd/data` возвращал имя печатаемого
файла, прогресс и температуры.

Отсюда два разных барьера, и тесты держат оба:
  * ЛОКАЛЬНОСТЬ — безусловна. Рендерер экрана живёт на той же машине (turzx-display/bot_screen.py
    ходит на 127.0.0.1:8081), поэтому ужесточение ничего не отрезает;
  * КЛЮЧ — сверху, когда задан. Он остаётся полезен: локальным считается и запрос из туннеля
    по peer, отсекаемый уже по Host, и любой другой процесс на машине.

Отдельно закреплено, что тело отказа НЕ содержит данных: 403 с телом, в котором лежит `printer`,
был бы дырой ровно того же размера.
"""
import json
from types import SimpleNamespace

import pytest

from app import elegoo, webapp
from app.web import screens


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Ручка кэширует ответ на 2 с в модульном словаре и ходит за метриками наружу (nvidia-smi,
    LibreHardwareMonitor по HTTP). И то, и другое протекало бы между тестами: кэш отдал бы чужой
    ответ, а внешние чтения сделали бы результат зависящим от того, что сейчас в машине.

    Ключ тоже прибиваем к пустому: он читается из окружения при импорте, и набор, зелёный у
    владельца с заданным LCD_KEY, обязан быть зелёным и на голой машине без него.

    Подмены ставятся на app/web/screens.py — ДОМ ручки после волны 3 endgame: `_api_lcd_data`
    читает `_LCD_KEY`/`psutil`/`_lcd_gpu` из СВОИХ глобалов, и подмена в webapp до них не
    достаёт (иначе тест молча полез бы за настоящим nvidia-smi и на localhost:8085). Словари
    состояния — те же объекты через фасад, их можно чистить с любой стороны."""
    screens._lcd_cache.update(ts=0.0, data=None)
    screens._lcd_prn.clear()      # трекер стола теперь по машинам
    monkeypatch.setattr(screens, "_LCD_KEY", "")
    monkeypatch.setattr(screens, "psutil", None)
    monkeypatch.setattr(screens, "_lcd_gpu", lambda: _empty_dict())
    monkeypatch.setattr(screens, "_lcd_cpu_temp", lambda: _none())
    monkeypatch.setattr(webapp, "_lcd_gpu", lambda: _empty_dict(), raising=False)
    monkeypatch.setattr(webapp, "_lcd_cpu_temp", lambda: _none(), raising=False)
    monkeypatch.setattr(elegoo, "cached_status", lambda *a, **k: None)
    yield
    screens._lcd_cache.update(ts=0.0, data=None)
    screens._lcd_prn.clear()      # трекер стола теперь по машинам


async def _empty_dict() -> dict:
    return {}


async def _none() -> None:
    return None


def _req(host="127.0.0.1", remote="127.0.0.1", headers=None, **query):
    """Фейк запроса под то, что реально читает хендлер: host/remote/headers для проверки
    локальности и query для ключа. Форма — та же, что в tests/test_screen_power.py у соседних
    ручек. headers появились вместе со слиянием предикатов (волна 4): единый
    webauth.is_local_request смотрит и заголовки прокси тоже."""
    return SimpleNamespace(host=host, remote=remote, headers=headers or {}, query=query)


def _payload(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


async def test_local_poller_gets_data(tmp_db):
    """Рендерер с этой же машины проходит: ключ не задан (боевая конфигурация), но локальность
    выполнена. Это тот запрос, ради которого горит физический экран на столе."""
    resp = await webapp._api_lcd_data(_req())
    assert resp.status == 200
    payload = _payload(resp)
    assert "printer" in payload                 # секция, ради которой экран и живёт
    assert isinstance(payload["printer"].get("queue"), list)


@pytest.mark.parametrize("req,why", [
    (_req(host="bot.example.ts.net"), "туннель: peer loopback, но Host публичный"),
    (_req(remote="203.0.113.7"), "запрос из сети, минуя туннель"),
    (_req(host="bot.example.ts.net", remote="203.0.113.7"), "и то, и другое чужое"),
])
async def test_remote_is_refused(tmp_db, req, why):
    """Нелокальный запрос — 403, и в теле отказа нет ни принтера, ни очереди, ни метрик."""
    resp = await webapp._api_lcd_data(req)
    assert resp.status == 403, why
    body = _payload(resp)
    assert body == {"ok": False, "error": "local only"}


async def test_remote_is_refused_even_before_cache(tmp_db):
    """Отказ идёт ДО кэша: иначе прогретый локальным поллером ответ утекал бы наружу целиком —
    ровно тот сценарий, в котором дыра и работала (поллер ходит каждые 2 с, кэш всегда тёплый)."""
    assert (await webapp._api_lcd_data(_req())).status == 200
    assert webapp._lcd_cache["data"] is not None          # кэш прогрет
    resp = await webapp._api_lcd_data(_req(host="bot.example.ts.net"))
    assert resp.status == 403
    assert "printer" not in _payload(resp)


async def test_key_still_checked_when_set(tmp_db, monkeypatch):
    """Ключ задан — поведение прежнее: правильный пускает, чужой и отсутствующий получают 403.
    Локальность его не отменяет, она проверяется отдельно и раньше."""
    monkeypatch.setattr(screens, "_LCD_KEY", "s3cret")
    assert (await webapp._api_lcd_data(_req(k="s3cret"))).status == 200
    webapp._lcd_cache.update(ts=0.0, data=None)
    assert (await webapp._api_lcd_data(_req(k="wrong"))).status == 403
    assert (await webapp._api_lcd_data(_req())).status == 403


async def test_key_does_not_open_the_door_from_outside(tmp_db, monkeypatch):
    """Знание ключа НЕ делает чужой запрос своим: утёкший LCD_KEY не должен превращаться
    в доступ к очереди заказов снаружи."""
    monkeypatch.setattr(screens, "_LCD_KEY", "s3cret")
    resp = await webapp._api_lcd_data(_req(host="bot.example.ts.net", k="s3cret"))
    assert resp.status == 403
    assert _payload(resp) == {"ok": False, "error": "local only"}
