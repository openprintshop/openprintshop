"""Студия владельца: настройки и брендинг, команда, справочники, каталог, пул MakerWorld.

Один предмет: то, что НАСТРАИВАЕТ владелец мастерской, — база знаний, каталог коллекции,
настройки (ставки/тексты/бренд/оплата/возможности сборки), логотип, команда и роли,
телеметрия ошибок, оттенки цветов и пул аккаунтов MakerWorld.

Выделено из app/webapp.py волной 7 endgame-плана. Волна «свободна по-настоящему»: ни `_bot`,
ни модульного состояния — но у пяти групп НОЛЬ якорей `webapp.<имя>` в тестах, поэтому
фасадный тест tests/test_studio_facade.py — ЕДИНСТВЕННЫЙ прибор против забытой строки
фасада (см. риск волны в плане).

В webapp остались соседние по смыслу, но чужие по состоянию ручки: центр уведомлений и
семейные счета (читают `_bot` — волна 13), `_api_admin_photo` (общий узел шести доменов —
вопрос 4 плана), выгрузки/статистика использования (не вошли в этот заход сознательно).

ОБЯЗАТЕЛЬНАЯ строка `_forbidden_cap` в `_api_admin_brand_logo` уехала вместе с ручкой:
multipart проходит МИМО _caps_mw, и без неё загрузка логотипа открылась бы оператору.

Регистрация маршрутов осталась в make_app строками на фасадные имена. Тела перенесены
ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from aiohttp import web

from .. import brand, config, db, desklogin, edition, optional, payments, roles
from ..webauth import _admin_body, _admin_forbidden, _verify_admin
from ..webguard import _forbidden_cap
from ..websign import _file_ver
from .common import _bad, _num, _served_img, _toint

# Пул аккаунтов MakerWorld — личный модуль: в открытой версии проекта его нет (app/optional.py).
mwtoken = optional.load("mwtoken")
_MW_ABSENT = {"ok": False, "error": "MakerWorld не входит в эту сборку"}

log = logging.getLogger(__name__)

async def _api_admin_kb(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    return web.json_response({"ok": True, "articles": await db.kb_list(published_only=False)})

async def _api_admin_kb_save(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    title = (body.get("title") or "").strip()
    if not title:
        return web.json_response({"ok": False, "error": "Нужен заголовок статьи"})
    aid = await db.kb_save(_toint(body.get("id")) or None, title, body.get("body") or "",
                           _toint(body.get("sort")), bool(body.get("published")))
    await roles.log_action(uid, "правка базы знаний", title[:80])
    return web.json_response({"ok": True, "id": aid})

async def _api_admin_kb_delete(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    aid = _toint(body.get("id"))
    if not aid:
        return web.json_response({"ok": False, "error": "Не указана статья"})
    await db.kb_delete(aid)
    await roles.log_action(uid, "удалил статью базы знаний", f"#{aid}")
    return web.json_response({"ok": True})

async def _api_admin_collection(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()

    def _img(it) -> str | None:
        # фото печати, а без него — рендер: /img/{cid} сам выберет (OpenTodo b785c67f)
        p = _served_img(it)
        return f"/img/{it.id}?v={_file_ver(p)}" if p and Path(p).exists() else None

    def _item_json(it) -> dict:
        return {"id": it.id, "label": it.label, "uses": it.uses,
                "category": getattr(it, "category", "") or "",
                "has_photo": bool(it.photo_path and Path(it.photo_path).exists()),
                "photo": _img(it)}

    # разделы с count/sort — блок «Порядок разделов» + список разделов (475b413e, b38e9569)
    cats = [{"name": n, "count": c, "sort": s} for n, c, _t, s in await db.list_collection_categories()]

    # Drill-in (OpenTodo b38e9569): ?category=X — страница моделей одного раздела с пагинацией.
    # '__none__' — псевдо-раздел «вне каталога» (category=''). Разделы из ВК бывают на сотни
    # моделей, поэтому по разделу отдаём страницами, а не всё разом.
    cat = request.query.get("category", None)
    if cat is not None:
        offset = max(0, _toint(request.query.get("offset"), 0))
        limit = max(1, min(120, _toint(request.query.get("limit"), 40)))
        db_cat = "" if cat == "__none__" else cat
        items = await db.list_collection_by_category(db_cat, limit=limit, offset=offset)
        total = await db.count_collection_by_category(db_cat)
        return web.json_response({"ok": True, "category": cat, "categories": cats,
            "items": [_item_json(it) for it in items],
            "offset": offset, "total": total, "has_more": offset + len(items) < total})

    # Уровень 1 — список разделов + бакет «вне каталога» + общий счётчик (без плоского списка).
    return web.json_response({"ok": True, "categories": cats,
        "uncat_count": await db.count_collection_by_category(""),
        "total": await db.count_collection()})

async def _api_admin_collection_edit(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    action = body.get("action", "")
    cid = _toint(body.get("id"))
    if action == "rename":
        label = (body.get("label") or "").strip()
        if label:
            await db.set_collection_label(cid, label)
        return web.json_response({"ok": True})
    if action == "delete":
        await db.delete_collection_item(cid)
        return web.json_response({"ok": True})
    if action == "set_category":   # раздел каталога для записи (c9c42789); '' = вне каталога
        await db.set_collection_category(cid, (body.get("category") or "").strip())
        return web.json_response({"ok": True})
    if action == "cats_order":   # админский порядок разделов каталога (475b413e): весь список
        # одним запросом — частичный sort у пары строк прыгал бы мимо разделов с дефолтным 100
        names = [str(n).strip() for n in (body.get("names") or []) if str(n).strip()]
        if not names:
            return web.json_response({"ok": False, "error": "Пустой список разделов"})
        for i, n in enumerate(names):
            await db.set_collection_category_sort(n, i * 10)
        return web.json_response({"ok": True})
    if action == "rename_category":   # переименовать раздел целиком (b38e9569)
        old = (body.get("old") or "").strip()
        new = (body.get("new") or "").strip()
        if not old or not new:
            return web.json_response({"ok": False, "error": "Укажите новое имя раздела"})
        await db.rename_collection_category(old, new)
        return web.json_response({"ok": True})
    if action == "delete_category":   # удалить раздел; модели становятся «вне каталога» (b38e9569)
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"ok": False, "error": "Не указан раздел"})
        await db.delete_collection_category(name)
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})

async def _api_admin_settings(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    s = await db.get_settings()
    t = await db.get_text_settings()
    # CRM-пороги — отдельной группой (IA-редизайн, аудит #7): в UI они живут на экране
    # «Клиенты» (настройка сегментов), а не среди ценовых ставок.
    pricing_keys = [k for k in db.SETTINGS_ORDER if not k.startswith("crm_")]
    crm_keys = [k for k in db.SETTINGS_ORDER if k.startswith("crm_")]

    def _grp(keys):
        return {"order": keys,
                "labels": {k: db.SETTING_LABELS[k].format(cur=config.CURRENCY) for k in keys},
                "values": {k: s.get(k, 0) for k in keys}}

    def _txt(keys):
        return {"order": keys, "labels": {k: db.TEXT_SETTING_LABELS[k] for k in keys},
                "values": {k: t.get(k, "") for k in keys}}
    # Брендинг отдаём и «как настройки» (поля для редактирования), и «как результат»
    # (brand_current) — во втором уже подставлены дефолты, админке есть что показать
    # в предпросмотре, не повторяя у себя логику подстановки.
    return web.json_response({"ok": True, "currency": config.CURRENCY,
        "pricing": _grp(pricing_keys), "crm": _grp(crm_keys),
        "texts": _txt(db.TEXT_SETTINGS_ORDER),
        "brand": _txt(db.BRAND_SETTINGS_ORDER),
        "brand_current": brand.resolve(t),
        # онлайн-оплата (A1): своя группа — токен провайдера и валюта, в админках это
        # отдельная секция «Онлайн-оплата», а не строка среди реквизитов PDF
        "pay": _txt(db.PAY_SETTINGS_ORDER),
        # сообщество владельцев студий (c356387a): ссылка на внешнюю Telegram-группу
        "owners": _txt(db.OWNERS_SETTINGS_ORDER),
        # вход в настольную админку (app/desklogin.py): общий .env-токен или личный по коду
        "access": _txt(db.ACCESS_SETTINGS_ORDER),
        # Возможности сборки (f98aa1ef): что здесь «моё личное», а что продуктовое.
        "edition": {"edition": edition.edition(), "features": edition.snapshot()}})

async def _api_admin_feature(request: web.Request) -> web.Response:
    """Включить/выключить возможность сборки (OpenTodo f98aa1ef).

    Владелец сильнее редакции в ОБЕ стороны: свою личную фичу можно выключить, а покупателю
    с подходящей машиной — включить. `reset` возвращает к дефолту редакции."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    key = (body.get("key") or "").strip()
    if key not in edition.FEATURES_BY_KEY:
        return web.json_response({"ok": False, "error": "Неизвестная возможность"})
    if body.get("reset"):
        await db.delete_setting(edition.setting_key(key))
    else:
        await db.set_setting(edition.setting_key(key), 1 if body.get("on") else 0)
    edition.apply_settings(await db.get_settings())
    return web.json_response({"ok": True, "features": edition.snapshot()})

async def _api_admin_setting(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    key = body.get("key", "")
    if body.get("kind") == "num" and key in db.SETTING_LABELS:
        val = _num(body, "value")
        if val is None:
            return web.json_response({"ok": False, "error": "Нужно число"})
        await db.set_setting(key, val)
        return web.json_response({"ok": True})
    if body.get("kind") == "text" and key in db.TEXT_SETTING_LABELS:
        val = (body.get("value") or "").strip()
        # Акцентный цвет попадает в CSS-переменную мини-аппа. Пускать сюда произвольную
        # строку нельзя: сохраним мусор — сломается тема у всех клиентов, а «red;}» ещё и
        # закроет правило. Пустая строка разрешена — это «вернуть наш цвет».
        if key == "brand_accent" and val and not brand.clean_accent(val):
            return web.json_response({"ok": False, "error": "Цвет в формате #RRGGBB, например #2f6df6"})
        if key == "brand_accent":
            val = brand.clean_accent(val)
        # Токен провайдера валидируем по форме BotFather: сюда реально вписывают публичные
        # ключи из кабинета провайдера (случай 31.07, PAYMENT_PROVIDER_INVALID у клиентов) —
        # такой «токен» непустой, оплата «включается» и ломается у всех разом.
        if key == "pay_provider_token":
            cleaned = payments.clean_provider_token(val)
            if cleaned is None:
                return web.json_response(
                    {"ok": False, "error": "Это не токен из BotFather. Нужен вид "
                                           "123456789:TEST:… или 123456789:LIVE:… — его выдаёт "
                                           "BotFather (/mybots → Bot Settings → Payments). "
                                           "Ключи из кабинета провайдера не подходят."})
            val = cleaned
        if key == "pay_currency":
            val = val.strip().upper()
            if val and not re.fullmatch(r"[A-Z]{3}", val):
                return web.json_response(
                    {"ok": False, "error": "Валюта — трёхбуквенный ISO-код: RUB, BYN, USD…"})
        # Ссылка уходит в href кнопки админки: пускать туда javascript:/data: нельзя.
        if key == "owners_chat_url" and val and not re.match(r"^https://[\w.-]+/\S*$", val):
            return web.json_response(
                {"ok": False, "error": "Нужна ссылка вида https://t.me/…"})
        # Режим входа в деск — не свободный текст, а два состояния. Опечатка здесь означала бы
        # «ни то, ни это»: значение читается сравнением со строкой personal, и «Personal» с
        # заглавной тихо оставила бы общую дверь открытой.
        if key == "desk_login_mode":
            val = val.strip().lower()
            if val not in ("token", "personal"):
                return web.json_response(
                    {"ok": False, "error": "Режим входа — token (общий) или personal (личный)"})
        await db.set_text_setting(key, val)
        if key == "desk_login_mode":
            await desklogin.refresh()      # кэш читают из синхронного _verify_user
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестный параметр"})

async def _api_admin_brand_logo(request: web.Request) -> web.Response:
    """Логотип мастерской: multipart (initData, file) — залить, ?clear=1 в теле — снять.

    Не в set_text_setting: это бинарь, он живёт файлом в data/brand (см. app/brand.py)."""
    if (request.content_type or "").startswith("multipart/"):
        init_data, fname, data = "", "", b""
        reader = await request.multipart()
        async for field in reader:
            if field.name == "initData":
                init_data = (await field.read()).decode("utf-8", "ignore")
            elif field.name == "file":
                fname = field.filename or ""
                data = await field.read()
        buid = _verify_admin(init_data)
        if not buid:
            return _admin_forbidden()
        # multipart проходит мимо _caps_mw (тело нельзя читать дважды) — право проверяем здесь:
        # бренд мастерской это настройки, оператору туда нельзя.
        if not roles.can(buid, roles.CAP_SETTINGS):
            return _forbidden_cap(roles.CAP_SETTINGS)
        try:
            brand.save_logo(data, fname)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)})
        except OSError:
            log.warning("brand logo save failed", exc_info=True)
            return web.json_response({"ok": False, "error": "Не удалось сохранить файл"})
        return web.json_response({"ok": True, "brand": brand.resolve(await db.get_text_settings())})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    brand.clear_logo()
    return web.json_response({"ok": True, "brand": brand.resolve(await db.get_text_settings())})

async def _brand_logo_file(request: web.Request) -> web.StreamResponse:
    """Логотип наружу. Публичный: его показывает мини-апп любому клиенту мастерской."""
    p = brand.logo_file()
    if p is None:
        raise web.HTTPNotFound()
    # ?v=<mtime> в ссылке уже меняется при замене файла, поэтому кэшировать безопасно
    return web.FileResponse(p, headers={"Cache-Control": "max-age=86400"})

async def _api_admin_staff(request: web.Request) -> web.Response:
    """Список команды + справочник ролей. Только владелец (cap settings)."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    return web.json_response({
        "ok": True, "staff": await roles.staff_list(),
        "roles": [{"id": r, "label": roles.ROLE_LABELS[r], "hint": roles.ROLE_HINTS[r],
                   "caps": sorted(roles.ROLE_CAPS[r])} for r in (config.ROLE_OWNER,
                                                                 config.ROLE_OPERATOR)],
        "caps": [{"id": c, "label": roles.CAP_LABELS[c]} for c in roles.ALL_CAPS]})

async def _api_admin_staff_save(request: web.Request) -> web.Response:
    """Пригласить сотрудника / сменить роль / удалить. Только владелец (cap settings).

    Сотрудник задаётся Telegram ID: узнать его проще всего из карточки клиента или из ленты
    активности — человек должен хотя бы раз написать боту. Корневые владельцы из .env здесь
    неприкосновенны: их нельзя ни понизить, ни удалить (см. app/roles.py)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = _admin_body(body)
    if err:
        return err
    action = (body.get("action") or "save").strip()
    target = _toint(body.get("user_id"))
    if action == "del":
        ok, msg = await roles.remove(target)
        if ok:
            await roles.log_action(uid, "убрал сотрудника из команды", f"id {target}")
        return web.json_response({"ok": ok, "error": msg} if not ok else {"ok": True})
    if action == "logout":
        # Выкинуть человека со всех компьютеров, НЕ убирая его из команды: телефон забыт
        # разблокированным, сотрудник в отпуске, увольнение обсуждается. Отдельная кнопка
        # нужна именно потому, что снятие роли — действие другого веса.
        n = await desklogin.revoke_user(target)
        await roles.log_action(uid, "завершил сессии сотрудника", f"id {target}: {n}")
        return web.json_response({"ok": True, "closed": n, "staff": await roles.staff_list()})
    role = (body.get("role") or config.ROLE_OPERATOR).strip()
    ok, msg = await roles.set_role(target, role, name=(body.get("name") or "").strip()[:80],
                                   note=(body.get("note") or "").strip()[:200], added_by=uid)
    if not ok:
        return web.json_response({"ok": False, "error": msg})
    await roles.log_action(uid, "изменил роль сотрудника",
                           f"id {target} → {roles.role_label(role)}")
    return web.json_response({"ok": True, "staff": await roles.staff_list()})

async def _api_admin_errors(request: web.Request) -> web.Response:
    """Экран «Ошибки» обеих админок: собранные сбои с трейсбеками. ?new=1 — только новые."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    limit = _toint(request.query.get("limit")) or 200
    errors = await db.list_errors(limit=limit, only_new=bool(request.query.get("new")))
    return web.json_response({"ok": True, "errors": errors,
                              "new_count": await db.count_new_errors()})

async def _api_admin_errors_seen(request: web.Request) -> web.Response:
    """«Прочитано»: с id — одну запись, без id — все. Прочитанная больше не собирает повторы:
    вернувшийся после разбора сбой заведёт новую строку и новый алерт."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    n = await db.mark_errors_seen(_toint(body.get("id")) or None)
    return web.json_response({"ok": True, "marked": n})

async def _api_admin_shades(request: web.Request) -> web.Response:
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    return web.json_response({"ok": True,
        "shades": [{"name": n, "hex": h} for n, h in await db.list_color_shades()]})

async def _api_admin_shade(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    if body.get("action") == "delete":
        await db.delete_color_shade((body.get("name") or "").strip())
        return web.json_response({"ok": True})
    if body.get("action") == "add":
        name = (body.get("name") or "").strip()
        hexv = (body.get("hex") or "").strip()
        if not name or not hexv:
            return web.json_response({"ok": False, "error": "Нужны название и HEX"})
        if not hexv.startswith("#"):
            hexv = "#" + hexv
        await db.set_color_shade(name, hexv)
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "Неизвестное действие"})

async def _api_admin_mw_accounts(request: web.Request) -> web.Response:
    """Пул аккаунтов MakerWorld для админки. Сам токен наружу НЕ отдаём — только маска."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    if mwtoken is None:
        return web.json_response(_MW_ABSENT, status=404)
    return web.json_response({"ok": True, "accounts": mwtoken.status_all()})

async def _api_admin_mw_account(request: web.Request) -> web.Response:
    """Действия над аккаунтом пула MakerWorld. Body {action, id, label}:
      add     — завести пустой аккаунт (токен даёт `python get_jwt.py --account <id>`);
      toggle  — вкл/выкл участие в ротации;
      delete  — убрать из пула (кроме «main»: на нём сид из .env и прежний профиль);
      clear   — снять кулдаун 418 вручную;
      refresh — продлить токен из браузерной сессии этого аккаунта прямо сейчас.
    Завести аккаунт «под ключ» из веба нельзя: вход в MakerWorld требует живого окна браузера
    с капчей на самой машине — поэтому UI и подсказывает команды mw_login.py / get_jwt.py."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = _admin_body(body)
    if err:
        return err
    if mwtoken is None:
        return web.json_response(_MW_ABSENT, status=404)
    action = (body.get("action") or "").strip()
    aid = (body.get("id") or "").strip()
    if action == "add":
        label = (body.get("label") or "").strip()
        if not label and not aid:
            return web.json_response({"ok": False, "error": "Введите название аккаунта"})
        try:
            acc = mwtoken.add_account(label, aid=aid)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        return web.json_response({"ok": True, "id": acc["id"],
                                  "hint": f"python mw_login.py --account {acc['id']} → "
                                          f"python get_jwt.py --account {acc['id']}"})
    if not aid:
        return web.json_response({"ok": False, "error": "Не указан аккаунт"})
    if action == "toggle":
        cur = next((a for a in mwtoken.status_all() if a["id"] == aid), None)
        if not cur or not mwtoken.set_enabled(aid, not cur["enabled"]):
            return web.json_response({"ok": False, "error": "Аккаунт не найден"})
    elif action == "delete":
        if not mwtoken.remove_account(aid):
            return web.json_response({"ok": False, "error": "Основной аккаунт удалить нельзя"})
    elif action == "clear":
        if not mwtoken.clear_cooldown(aid):
            return web.json_response({"ok": False, "error": "Аккаунт не найден"})
    elif action == "refresh":
        ok = await mwtoken.ensure_fresh_account(aid, force=True)
        if not ok:
            return web.json_response({"ok": False,
                                      "error": "Не вышло — похоже, сессия разлогинилась. "
                                               f"Войдите заново: python mw_login.py --account {aid}"})
    else:
        return web.json_response({"ok": False, "error": "Неизвестное действие"})
    return web.json_response({"ok": True})
