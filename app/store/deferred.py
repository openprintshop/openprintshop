"""Очередь досчёта цены (Э0, docs/Перспективные разработки/vps-relay-2026-10-04.md §1).

Заказ, цену которого ещё никто не посчитал, должен пережить рестарт процесса. До Э0 связь
«заказ ↔ досчёт» жила только в памяти (`m["pending_order_id"]` токена мини-аппы): рестарт
между «Оставить заказ» и концом слайса оставлял заявку с ценой 0 навсегда. Здесь эта связь
лежит в БД — таблица deferred_calc, по строке-заданию на заказ, — а заказ несёт флаг
requests.price_pending=1 («цену посчитает ПК», не «оценит мастер»).

Модель исполнения — ОДИН сборщик на процесс (app/deferred.py) и одно соединение БД на
процесс (core._conn). Отсюда правила, на которых держится вся корректность:

* ЗАХВАТ условный: `status='queued' → 'running'` одним UPDATE с проверкой rowcount, к нему
  выдаётся claim_token. Таймаута захвата нет: при старте процесса все 'running' — сироты
  прошлого процесса и возвращаются в очередь (requeue_running).
* ПРИМЕНЕНИЕ — одна функция и один commit (apply_deferred_result): условие и по заданию
  (running + наш токен), и по заказу (status='new' AND price_pending=1). Заказ, который за
  время ожидания ушёл к мастеру (тот назвал цену), отменён клиентом или удалён, не
  перетирается ничем — задание просто отменяется. Возврат True получает РОВНО тот, кто
  применил: уведомления шлются после commit и только по True («дубль хуже пропуска»).
* Соединение общее, транзакции неявные (store.md §3): между `await` в нашу транзакцию
  попадают чужие DML, а `rollback()` откатил бы и их. Поэтому здесь нет ни одного
  rollback — только условные UPDATE и проверка rowcount.

Здесь же две дедуп-таблицы приёмной (Э1): tg_seen (update_id Telegram) и relay_events_seen
(id событий приёмной). Сами по себе они к досчёту отношения не имеют, но живут тем же
правилом «отметка — условной вставкой, ответ — rowcount».

Модуль НЕ импортирует app.db (правило пакета store): соединение — только через core._conn().
"""
from __future__ import annotations

import json
import time
import uuid

from .core import _conn, _jload, _rel

# Сколько раз задание пробуют, прежде чем отдать заказ мастеру. Счётчик растёт при каждом
# ЗАХВАТЕ, а не при каждой ошибке: процесс, умерший посреди расчёта (модель-«убийца» съела
# память), тоже тратит попытку — иначе такая модель роняла бы бота по кругу вечно.
MAX_ATTEMPTS = 6
BACKOFF_BASE_SEC = 30          # пауза после попытки N: min(30·2^N, 3600) с
BACKOFF_MAX_SEC = 3600
TG_SEEN_KEEP_SEC = 3 * 86400   # приёмная передоставляет «подвисшее» через 600 с — трёх суток с запасом

_ACTIVE = ("queued", "running")


def _job(row) -> dict:
    """Строка deferred_calc → dict; source/params — уже разобранный JSON (битое → {})."""
    d = dict(row)
    d["source"] = _jload(d.get("source"), {}) if not isinstance(d.get("source"), dict) else d["source"]
    d["params"] = _jload(d.get("params"), {}) if not isinstance(d.get("params"), dict) else d["params"]
    return d


async def enqueue_deferred(rid: int, *, source: dict, params: dict, client_req_id: str | None = None,
                           kind: str = "order", delay_sec: float = 0.0) -> str:
    """Поставить заказ rid в очередь досчёта; заказу — price_pending=1 тем же commit-ом.

    Идемпотентно по client_req_id ('now:<rid>', 'rly:<event_id>' …): повторный вызов с тем же
    ключом ничего не меняет и возвращает id уже существующего задания. delay_sec — «придержать»
    задание: у order_now цену прямо сейчас досчитывает живой расчёт мини-аппы, и сборщику
    незачем гнать второй такой же тяжёлый расчёт параллельно (см. release_deferred_hold)."""
    now = time.time()
    jid = uuid.uuid4().hex
    cur = await _conn().execute(
        "INSERT INTO deferred_calc(id, rid, kind, source, params, status, attempts, next_try_at, "
        "client_req_id, created_at, updated_at) VALUES(?,?,?,?,?, 'queued', 0, ?, ?, ?, ?) "
        "ON CONFLICT(client_req_id) DO NOTHING",
        (jid, int(rid), kind or "order", json.dumps(source or {}, ensure_ascii=False),
         json.dumps(params or {}, ensure_ascii=False), now + max(0.0, float(delay_sec or 0)),
         client_req_id or None, now, now))
    if cur.rowcount == 1:
        # Флаг ставим только живому заказу: отменённый/взятый мастером «ждать ПК» не может.
        await _conn().execute(
            "UPDATE requests SET price_pending = 1 WHERE id = ? AND status = 'new'", (int(rid),))
        await _conn().commit()
        return jid
    await _conn().commit()
    existing = await deferred_by_client_req(client_req_id or "")
    return existing["id"] if existing else jid


async def get_deferred(job_id: str) -> dict | None:
    cur = await _conn().execute("SELECT * FROM deferred_calc WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    return _job(row) if row else None


async def deferred_by_client_req(client_req_id: str) -> dict | None:
    """Задание по ключу идемпотентности приёма — так приём события приёмной узнаёт свой заказ."""
    if not client_req_id:
        return None
    cur = await _conn().execute("SELECT * FROM deferred_calc WHERE client_req_id = ?", (client_req_id,))
    row = await cur.fetchone()
    return _job(row) if row else None


async def deferred_for_request(rid: int) -> list[dict]:
    """Все задания заказа, свежие первыми (для карточки мастера и тестов)."""
    cur = await _conn().execute(
        "SELECT * FROM deferred_calc WHERE rid = ? ORDER BY created_at DESC", (int(rid),))
    return [_job(r) for r in await cur.fetchall()]


async def next_due_deferred(now: float | None = None) -> str | None:
    """id самого старого созревшего задания (queued и next_try_at наступил) или None."""
    now = time.time() if now is None else now
    cur = await _conn().execute(
        "SELECT id FROM deferred_calc WHERE status = 'queued' AND next_try_at <= ? "
        "ORDER BY next_try_at, created_at LIMIT 1", (now,))
    row = await cur.fetchone()
    return row["id"] if row else None


async def claim_deferred(job_id: str, now: float | None = None) -> dict | None:
    """Захватить задание: условный queued→running, attempts+1, свежий claim_token.

    None — задание не наше (уже захвачено, не созрело, исчезло) ЛИБО заказ больше не ждёт
    расчёта: мастер назвал цену, клиент отменил, заказ удалён. Во втором случае задание
    тут же отменяется — считать то, что уже никому не нужно, значит занимать ПК впустую,
    а применить результат всё равно не дало бы условие apply_deferred_result."""
    now = time.time() if now is None else now
    token = uuid.uuid4().hex
    cur = await _conn().execute(
        "UPDATE deferred_calc SET status = 'running', claim_token = ?, attempts = attempts + 1, "
        "updated_at = ? WHERE id = ? AND status = 'queued' AND next_try_at <= ?",
        (token, now, job_id, now))
    if cur.rowcount != 1:
        return None
    cur = await _conn().execute(
        "SELECT d.*, r.status AS req_status, r.price_pending AS req_pending "
        "FROM deferred_calc d LEFT JOIN requests r ON r.id = d.rid WHERE d.id = ?", (job_id,))
    row = await cur.fetchone()
    if row is None or row["req_status"] != "new" or not row["req_pending"]:
        why = ("заказ удалён" if row is None or row["req_status"] is None
               else f"заказ уже не ждёт расчёта (статус {row['req_status']})")
        await _conn().execute(
            "UPDATE deferred_calc SET status = 'cancelled', error = ?, updated_at = ? "
            "WHERE id = ? AND claim_token = ?", (why, now, job_id, token))
        await _conn().commit()
        return None
    await _conn().commit()
    job = _job(row)
    job.pop("req_status", None)
    job.pop("req_pending", None)
    return job


async def claim_next_deferred(now: float | None = None) -> dict | None:
    """Следующее созревшее задание, уже захваченное; None — очередь пуста.

    Отменённые при захвате (заказ больше не ждёт) пропускаются тут же; предел итераций —
    страховка от вечного цикла, если строку кто-то трогает параллельно."""
    for _ in range(50):
        jid = await next_due_deferred(now)
        if jid is None:
            return None
        job = await claim_deferred(jid, now)
        if job is not None:
            return job
    return None


async def requeue_running(*, release_holds: bool = True) -> int:
    """Старт процесса: все 'running' — сироты умершего процесса, назад в очередь.

    Сборщик один на процесс, и до этого вызова он ничего не захватывал, — значит ни одно
    'running' сейчас никем не исполняется. release_holds: задания, придержанные под живой
    расчёт мини-аппы (order_now, ещё ни разу не захватывались), созревают сразу — того
    живого расчёта больше нет, он умер вместе с прошлым процессом. Возвращает число
    возвращённых 'running'.

    Тот же вызов с release_holds=False делает сборщик между проходами, если прошлый проход
    упал мимо release_claim (review:e0#1): между проходами он тоже ничего не исполняет, а
    живые расчёты мини-аппы при этом живы — их придержки трогать нельзя."""
    now = time.time()
    cur = await _conn().execute(
        "UPDATE deferred_calc SET status = 'queued', claim_token = NULL, updated_at = ? "
        "WHERE status = 'running'", (now,))
    n = cur.rowcount or 0
    if release_holds:
        await _conn().execute(
            "UPDATE deferred_calc SET next_try_at = ? WHERE status = 'queued' AND attempts = 0 "
            "AND next_try_at > ?", (now, now))
    await _conn().commit()
    return n


async def release_deferred_hold(rid: int) -> int:
    """Живой расчёт мини-аппы отказался от заказа (слайс упал, клиент пересчитал с другими
    параметрами) — придержанное под него задание созревает немедленно. Число заданий."""
    now = time.time()
    cur = await _conn().execute(
        "UPDATE deferred_calc SET next_try_at = ?, updated_at = ? WHERE rid = ? "
        "AND status = 'queued' AND attempts = 0 AND next_try_at > ?", (now, now, int(rid), now))
    await _conn().commit()
    return cur.rowcount or 0


# Поля requests, которые может записать результат досчёта. Белый список: результат собирает
# сборщик, но значения в нём частично пришли из файла клиента (имя модели), а имя колонки
# в SQL подставляется строкой.
_RESULT_FIELDS = ("grams_total", "minutes_total", "breakdown", "plastic_name", "quality",
                  "strength", "quality_id", "strength_id", "printer", "quantity", "dims",
                  "preview_path", "file_path", "hash", "plates", "multicolor", "model_label",
                  "stock_plan", "price_comment")
_RESULT_PATH_FIELDS = ("preview_path", "file_path")


async def apply_deferred_result(job_id: str, claim_token: str, result: dict, *,
                                auto_accept: bool = False) -> bool:
    """Записать посчитанную цену в заказ — ОДНОЙ транзакцией и только если всё ещё можно.

    Условия: задание наше и в работе (status='running' AND claim_token), заказ всё ещё ждёт
    именно нас (status='new' AND price_pending=1). Цена уходит клиенту на подтверждение:
    status='price_changed', pending_price=цена (без этого кнопка «Принять» ответила бы
    «Действие недоступно»). auto_accept — клиент сам разрешил принимать цену без
    подтверждения (профиль; решает вызывающий по db.price_auto_accept): тогда цена сразу
    боевая, заказ — «В работе», как у ordermoney._apply_price_or_ask.

    True — применили мы (вызывающему слать уведомления); False — заказ ушёл от нас или
    задание у нас отобрали; задание в этом случае отменяется, заказ не трогается."""
    now = time.time()
    cur = await _conn().execute(
        "SELECT rid FROM deferred_calc WHERE id = ? AND status = 'running' AND claim_token = ?",
        (job_id, claim_token))
    row = await cur.fetchone()
    if row is None:
        return False
    rid = int(row["rid"])
    price = round(float(result["price"]), 2)
    sets: dict = {}
    for k in _RESULT_FIELDS:
        if k not in result or result[k] is None:
            continue
        v = result[k]
        if k in ("breakdown", "stock_plan") and isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        elif k in _RESULT_PATH_FIELDS:
            v = _rel(str(v))
        sets[k] = v
    status = "in_progress" if auto_accept else "price_changed"
    if auto_accept:
        sets.update(price=price, pending_price=None)
    else:
        sets["pending_price"] = price
    sets.update(status=status, price_pending=0, unsliced=0, updated_at=int(now))
    assignments = ", ".join(f"{k} = ?" for k in sets)
    cur = await _conn().execute(
        f"UPDATE requests SET {assignments} WHERE id = ? AND status = 'new' AND price_pending = 1 "
        "AND EXISTS(SELECT 1 FROM deferred_calc WHERE id = ? AND status = 'running' "
        "AND claim_token = ?)", (*sets.values(), rid, job_id, claim_token))
    if cur.rowcount != 1:
        await _conn().execute(
            "UPDATE deferred_calc SET status = 'cancelled', error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running' AND claim_token = ?",
            ("заказ ушёл из очереди, пока шёл расчёт", now, job_id, claim_token))
        await _conn().commit()
        return False
    await _conn().execute(
        "INSERT INTO req_status_history(request_id, status, at) VALUES(?,?,?)", (rid, status, int(now)))
    # Задание закрываем безусловно по id: заказ уже наш и записан, а «отменить» его между
    # двумя операторами могло только то, что само смотрит на price_pending=1 — его уже нет.
    await _conn().execute(
        "UPDATE deferred_calc SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
        (now, job_id))
    await _conn().commit()
    return True


async def fail_deferred(job_id: str, claim_token: str, error: str) -> bool:
    """Повтор не поможет (битая/ненайденная модель, исчерпаны попытки): задание 'failed',
    заказ — к мастеру на ручную оценку (price_pending=0, unsliced=1 — «Оценит мастер»).

    True — заказ действительно переведён к мастеру сейчас (слать алерт и клиенту); False —
    задание у нас отобрали или заказ уже ушёл (мастер назвал цену сам) — молчать."""
    now = time.time()
    cur = await _conn().execute(
        "SELECT rid FROM deferred_calc WHERE id = ? AND status = 'running' AND claim_token = ?",
        (job_id, claim_token))
    row = await cur.fetchone()
    if row is None:
        return False
    await _conn().execute(
        "UPDATE deferred_calc SET status = 'failed', error = ?, updated_at = ? "
        "WHERE id = ? AND claim_token = ?", ((error or "")[:500], now, job_id, claim_token))
    cur = await _conn().execute(
        "UPDATE requests SET price_pending = 0, unsliced = 1, updated_at = ? "
        "WHERE id = ? AND status = 'new' AND price_pending = 1", (int(now), int(row["rid"])))
    await _conn().commit()
    return cur.rowcount == 1


def backoff_sec(attempts: int) -> float:
    """Пауза перед следующей попыткой после попытки №attempts."""
    return float(min(BACKOFF_BASE_SEC * (2 ** max(0, int(attempts))), BACKOFF_MAX_SEC))


async def retry_deferred(job_id: str, claim_token: str, error: str, now: float | None = None) -> bool:
    """Временный сбой (сеть, 418 MakerWorld, getFile, нехватка памяти, что угодно ещё):
    задание обратно в очередь с экспоненциальной паузой.

    False — повторять больше нельзя (попытки кончились) или задание уже не наше: вызывающий
    идёт в fail_deferred (тот сам промолчит, если задание не наше)."""
    now = time.time() if now is None else now
    cur = await _conn().execute(
        "SELECT attempts FROM deferred_calc WHERE id = ? AND status = 'running' AND claim_token = ?",
        (job_id, claim_token))
    row = await cur.fetchone()
    if row is None or int(row["attempts"] or 0) >= MAX_ATTEMPTS:
        return False
    cur = await _conn().execute(
        "UPDATE deferred_calc SET status = 'queued', claim_token = NULL, next_try_at = ?, "
        "error = ?, updated_at = ? WHERE id = ? AND status = 'running' AND claim_token = ?",
        (now + backoff_sec(int(row["attempts"] or 0)), (error or "")[:500], now, job_id, claim_token))
    await _conn().commit()
    return cur.rowcount == 1


async def release_claim(job_id: str, claim_token: str, error: str, now: float | None = None) -> bool:
    """Исполнение задания оборвалось ИСКЛЮЧЕНИЕМ вне классификации ошибок (review:e0#1): упала
    сама store-операция — apply/retry/fail_deferred на занятой БД, I/O-сбой диска. Без этого
    задание осталось бы 'running' до рестарта процесса (подбирает сборщик только 'queued', а
    сирот возвращает лишь старт): заказ висел бы «Цена считается», и никто бы этого не видел.

    Возврат в очередь с той же экспонентой, что у временного сбоя. Таймаута захвата нарочно
    нет — честный долгий расчёт гигантской модели он превратил бы в двойное исполнение; от
    бесконечного круга страхует счётчик attempts: он растёт на каждом захвате, и после
    MAX_ATTEMPTS сборщик отдаёт заказ мастеру. True — задание было нашим и вернулось в очередь."""
    now = time.time() if now is None else now
    cur = await _conn().execute(
        "SELECT attempts FROM deferred_calc WHERE id = ? AND status = 'running' AND claim_token = ?",
        (job_id, claim_token))
    row = await cur.fetchone()
    if row is None:
        return False
    cur = await _conn().execute(
        "UPDATE deferred_calc SET status = 'queued', claim_token = NULL, next_try_at = ?, "
        "error = ?, updated_at = ? WHERE id = ? AND status = 'running' AND claim_token = ?",
        (now + backoff_sec(int(row["attempts"] or 0)), (error or "")[:500], now, job_id, claim_token))
    await _conn().commit()
    return cur.rowcount == 1


async def _cancel_jobs_nocommit(rid: int, reason: str = "") -> int:
    """Отменить живые задания заказа БЕЗ commit — для тех, кто правит заказ своим commit-ом
    (store/orders.update_request при уходе заказа из 'new')."""
    cur = await _conn().execute(
        "UPDATE deferred_calc SET status = 'cancelled', error = ?, updated_at = ? "
        "WHERE rid = ? AND status IN ('queued', 'running')",
        ((reason or "отменено")[:500], time.time(), int(rid)))
    return cur.rowcount or 0


async def cancel_deferred_for_request(rid: int, reason: str = "") -> int:
    """Цену заказа решили иначе (клиент сам пересчитал в «Изменить», заказ отменён…):
    живые задания — 'cancelled', флаг ожидания снят. Задание, которое прямо сейчас
    считается, доработает, но применить результат не сможет (условие по статусу задания).
    Возвращает число отменённых заданий."""
    n = await _cancel_jobs_nocommit(rid, reason)
    await _conn().execute(
        "UPDATE requests SET price_pending = 0 WHERE id = ? AND price_pending = 1", (int(rid),))
    await _conn().commit()
    return n


# Поля requests, которые пишет живой хвост order_now (белый список: имя колонки идёт в SQL
# строкой). price — ПЕРВЫМ: на этом порядке держится тест «запись цены упала» (review:e0#2).
_LIVE_FIELDS = ("price", "grams_total", "minutes_total", "breakdown", "plastic_name", "quality",
                "strength", "quality_id", "strength_id", "dims", "preview_path", "stock_plan")
# Куда мастер может увести заказ order_now, НЕ решив его цену: «В работу», «Отложить», «Ждёт
# оплаты» — /api/admin/order/status цену не проверяет, а мастеру пришло «цена досчитается
# автоматически». До Э0 живой хвост писал цену таким заказам без условий (review:regress#0).
# Решённая цена — это price_changed/invoiced (ждёт клиента), отказ/отмена, или цена/пересчёт
# мастера (price > 0 / unsliced = 0 / pending_price) — такие заказы не трогаем никогда.
_LIVE_OPEN_STATUSES = ("new", "in_progress", "deferred", "awaiting_payment")


async def apply_live_price(rid: int, fields: dict) -> bool:
    """Живой расчёт мини-аппы (order_now) досчитал заказ: записать цену — ОДНИМ условным
    UPDATE и одним commit-ом вместе с отменой задания очереди (контракт §1 п.3, п.7).

    Раньше захват (снять флаг + отменить задание) коммитился отдельно, а цена писалась
    следующим вызовом: сбой или смерть процесса между ними теряли и цену, и задание
    (review:e0#2). Теперь до commit не меняется ничего: упали раньше — заказ всё ещё
    price_pending=1, его задание придержано, и цену досчитает сборщик.

    Условие — «цену заказа ещё никто не решил»: заказ ждёт ПК (new + price_pending=1) ЛИБО
    мастер увёл его из «Новый» без цены (см. _LIVE_OPEN_STATUSES), ЛИБО сборщик отдал его
    мастеру («Оценит мастер»), а живой слайс всё-таки досчитал. В любом случае цены ещё нет:
    price <= 0, pending_price пуст, unsliced=1. Досчитала очередь (price_changed / автоприём
    с ценой), назвал мастер, пересчитал клиент в «Изменить» — условие ложно, хвост молчит.

    fields — значения для _LIVE_FIELDS (breakdown/stock_plan — dict/list, пути — как есть).
    True — цену записали мы: вызывающему слать уведомления; False — заказ уже не наш."""
    now = time.time()
    sets: dict = {}
    for k in _LIVE_FIELDS:
        if k not in fields or fields[k] is None:
            continue
        v = fields[k]
        if k in ("breakdown", "stock_plan") and isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        elif k == "preview_path":
            v = _rel(str(v))
        sets[k] = v
    if "price" not in sets:
        raise ValueError("apply_live_price: нет цены")
    sets.update(unsliced=0, price_pending=0, updated_at=int(now))
    assignments = ", ".join(f"{k} = ?" for k in sets)
    ph = ",".join("?" for _ in _LIVE_OPEN_STATUSES)
    cur = await _conn().execute(
        f"UPDATE requests SET {assignments} WHERE id = ? AND status IN ({ph}) AND unsliced = 1 "
        "AND COALESCE(price, 0) <= 0 AND pending_price IS NULL",
        (*sets.values(), int(rid), *_LIVE_OPEN_STATUSES))
    if cur.rowcount != 1:
        await _conn().commit()      # закрыть неявную транзакцию UPDATE, ничего не изменившего
        return False
    await _cancel_jobs_nocommit(rid, "цену применил живой расчёт мини-аппы")
    await _conn().commit()
    return True


async def deferred_counts() -> dict:
    """Сводка очереди для админки/пульта: задания по статусам, сколько заказов ждут ПК и
    возраст самого старого созревшего задания (с) — «очередь стоит» видно по нему."""
    cur = await _conn().execute("SELECT status, COUNT(*) AS n FROM deferred_calc GROUP BY status")
    out = {s: 0 for s in ("queued", "running", "done", "failed", "cancelled")}
    for r in await cur.fetchall():
        out[r["status"]] = int(r["n"])
    cur = await _conn().execute(
        "SELECT COUNT(*) AS n FROM requests WHERE status = 'new' AND price_pending = 1")
    out["pending_orders"] = int((await cur.fetchone())["n"])
    now = time.time()
    cur = await _conn().execute(
        "SELECT MIN(created_at) AS t FROM deferred_calc WHERE status = 'queued' AND next_try_at <= ?",
        (now,))
    t = (await cur.fetchone())["t"]
    out["oldest_due_age"] = round(now - float(t), 1) if t is not None else 0.0
    return out


# ---- дедуп входящих для приёмной (Э1) ----
async def tg_seen_mark(update_id: int) -> bool:
    """Отметить апдейт Telegram увиденным. True — впервые (обрабатывать), False — уже был
    (приёмная передоставила его после потерянного ack — только подтвердить)."""
    cur = await _conn().execute(
        "INSERT OR IGNORE INTO tg_seen(update_id, at) VALUES(?, ?)", (int(update_id), time.time()))
    await _conn().commit()
    return cur.rowcount == 1


async def prune_tg_seen(keep_sec: float = TG_SEEN_KEEP_SEC) -> int:
    """Забыть давние апдейты: передоставка у приёмной — через 600 с, держать вечно незачем."""
    cur = await _conn().execute("DELETE FROM tg_seen WHERE at < ?", (time.time() - float(keep_sec),))
    await _conn().commit()
    return cur.rowcount or 0


async def relay_event_seen_mark(event_id: str) -> bool:
    """Отметить событие приёмной обработанным. True — впервые."""
    if not event_id:
        return False
    cur = await _conn().execute(
        "INSERT OR IGNORE INTO relay_events_seen(event_id, at) VALUES(?, ?)",
        (str(event_id), time.time()))
    await _conn().commit()
    return cur.rowcount == 1


async def relay_event_was_seen(event_id: str) -> bool:
    """Без отметки: было ли событие уже обработано (для приёма, который отмечает ПОСЛЕ успеха)."""
    if not event_id:
        return False
    cur = await _conn().execute("SELECT 1 FROM relay_events_seen WHERE event_id = ?", (str(event_id),))
    return await cur.fetchone() is not None
