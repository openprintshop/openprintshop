"""Реестр сессий расчёта мини-апки: токен модели -> файлы, варианты, происхождение.

Волна 14 endgame-плана (12.08). Дом словаря `_models` и единой регистрации модели:
загрузка файла/ссылки, каталог, «повторить заказ» и смена варианта проходят через
_register_full/_make_token и получают одинаковую карточку (варианты набора 87eb9cf3,
цвета из групп имён 2f2a102c, пер-стольные картинки, origin для B7).

Правила дома:
* модуль НЕ имеет права импортировать app.webapp (разрез односторонний; проверяется
  тестом в отдельном процессе). Оставшиеся в webapp читатели (_api_use, конвейер сметы)
  работают через фасад.
* `_models` мутируется ПО МЕСТУ и никогда не пере-присваивается — фасад webapp
  реэкспортирует ТОТ ЖЕ объект, автоuse-фикстура conftest.clean_models чистит его через
  фасад. Вторая копия словаря — это две разные сметы у одного клиента.
* `_MODELS_MAX` — плоская константа: подменять в тестах здесь, а не через webapp.
* Тела перенесены ДОСЛОВНО (сверка против git show HEAD:app/webapp.py).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from aiohttp import web

from .. import cpupool, db, inspect3mf, preview
from .mesh3d import _model3d_meta

log = logging.getLogger(__name__)

# Сессии загруженных моделей мини-апки: token -> {paths,label,plates,multicolor,num_colors,uid,...}
_models: dict = {}
_MODELS_MAX = 200


def _remember_model(rec: dict) -> str:
    if len(_models) > _MODELS_MAX:                      # простая защита от роста памяти
        for k in list(_models)[: len(_models) - _MODELS_MAX // 2]:
            _models.pop(k, None)
    token = uuid.uuid4().hex
    _models[token] = rec
    return token

async def _register_model(paths, label, uid, src_url, info, log: bool = True,
                          origin: str = "") -> str:
    """Зарегистрировать модель в _models (токен для мини-апки/deep-link «смета»). Для моделей
    на несколько столов вытаскиваем отдельную картинку на каждый стол (#03a5727e) — встроенные
    в .3mf plate_N.png. Общая точка: загрузка из мини-апки и быстрый расчёт по ссылке из чата.
    log=False — вызывающий пишет своё событие сам (каталог/повтор: «взял из коллекции»)."""
    plate_thumbs: list = []
    plate_mc: list = []
    if info.plates >= 2:
        try:
            plate_thumbs = [str(x) for x in
                            await asyncio.to_thread(preview.extract_plate_thumbnails, paths)]
        except Exception:  # noqa: BLE001
            plate_thumbs = []
        try:   # бейдж многоцвета на каждом столе (OpenTodo d7518198) — дёшево, по XML
            plate_mc = await asyncio.to_thread(inspect3mf.plate_multicolor, paths[0])
        except Exception:  # noqa: BLE001
            plate_mc = []
    # STL-набор с цветами в именах файлов (Ам Ням, OpenTodo 2f2a102c): .3mf-инспекции у него
    # нет, но группы имён дают и число цветов, и дизайн-цвета слотов. Карточка получает слоты
    # «Цвета печати» (multicolor=true, mc_colors по группам), GLB/превью красят по тем же
    # группам (geometry.part_color_context). Надбавки за продувку НЕТ: mixed_plates=False —
    # столы одноцветные, а число столов расчёт поднимает сам (_filename_color_groups, fm5).
    multicolor, num_colors = info.is_multicolor, info.num_colors
    mc_colors, mixed = info.used_colours, bool(info.mixed_plates)
    if not multicolor and not any(Path(p).suffix.lower() == ".3mf" for p in paths):
        _slots, ghex = inspect3mf.filename_group_slots([str(p) for p in paths])
        if len(ghex) >= 2:
            multicolor, num_colors, mc_colors, mixed = True, len(ghex), ghex, False
    token = _remember_model({"paths": paths, "label": label, "uid": uid, "plates": info.plates,
                             "multicolor": multicolor, "num_colors": num_colors,
                             "mixed_plates": mixed,
                             "mc_colors": mc_colors,
                             "src_url": src_url, "plate_thumbs": plate_thumbs,
                             "plate_mc": plate_mc,
                             # B7 (источник лида): не назвали — выводим из пути регистрации
                             "origin": origin or ("link" if src_url else "file")})
    if log:
        await db.log_event(uid, "загрузил модель", str(label or ""))
    return token

async def _register_full(paths, label, uid, src_url, info=None, *, sel_key: str | None = None,
                         presel: dict | None = None, log: bool = True,
                         origin: str = "") -> dict:
    """ЕДИНАЯ регистрация модели для мини-апки (загрузка файла/ссылки, каталог, «повторить»,
    смена варианта): варианты набора (87eb9cf3) + запись токена + JSON карточки.

    paths — ПОЛНЫЙ набор файлов карточки. Если в нём распознаны ВАРИАНТЫ (полный .3mf + та же
    модель россыпью STL — inspect3mf.variant_groups), регистрируем ТОЛЬКО выбранный комплект
    (sel_key; по умолчанию первый — полный .3mf): расчёт/3D/превью/заказ дальше живут его
    файлами, ничего фильтровать по месту не нужно. Полный набор остаётся в m["all_paths"] —
    /api/file_variant пересобирает токен под другой комплект."""
    all_paths = [str(p) for p in paths]
    variants = inspect3mf.variant_groups(all_paths)
    v = None
    reg_paths = all_paths
    if variants:
        v = next((x for x in variants if x["key"] == sel_key), variants[0])
        vset = set(v["paths"])
        reg_paths = [p for p in all_paths if p in vset] or all_paths
        info = None                      # инспекция ниже — по файлам ВЫБРАННОГО комплекта
    if info is None:
        info = await cpupool.run(inspect3mf.inspect_model, reg_paths)
    token = await _register_model(reg_paths, label, uid, src_url, info, log=log, origin=origin)
    m = _models[token]
    if variants:
        m["all_paths"] = all_paths
        m["file_variants"] = variants
        m["variant_sel"] = v["key"]
        m["variant_label"] = v["label"]
    meta = _model3d_meta(reg_paths)
    # мультицвет/цвета — из ЗАПИСИ токена, не из info: для STL-наборов с цветами в именах
    # _register_model поднимает multicolor/mc_colors из групп имён (Ам Ням, 2f2a102c)
    res = {"ok": True, "token": token, "label": label,
           "plates": info.plates, "multicolor": bool(m.get("multicolor")),
           "num_colors": m.get("num_colors") or 1,
           "preview": f"/api/mpreview/{token}",
           "plate_count": len(m.get("plate_thumbs") or []),
           "plate_mc": m.get("plate_mc") or [],
           # цвета дизайна из .3mf/групп имён → мини-апп предвыберет ближайшие
           "mc_colors": m.get("mc_colors") or [],
           # B1 (3D-просмотр): URL сырого файла + гейт видимости кнопки «3D»
           "model_url": f"/api/modelfile/{token}", **meta}
    if variants:
        res["file_variants"] = [{"key": x["key"], "label": x["label"], "files": len(x["paths"])}
                                for x in variants]
        res["variant_sel"] = v["key"]
    if presel:
        res["presel"] = presel
    return res

async def _remember_and_respond(paths, label, uid, src_url, info) -> web.Response:
    """Запомнить модель + ответ для мини-апки."""
    return web.json_response(await _register_full(paths, label, uid, src_url, info))

async def _make_token(uid: int, paths: list, label: str, presel: dict | None = None,
                      origin: str = "") -> dict | None:
    """Токен для каталога/«повторить заказ» — та же единая регистрация, что у загрузки файла:
    каталожные карточки тоже получают варианты набора (87eb9cf3), цвета из групп имён (Ам Ням,
    2f2a102c), mixed_plates и пер-стольные картинки. Своё событие пишет вызывающий (log=False)."""
    existing = [str(p) for p in paths if Path(p).exists()]
    if not existing:
        return None
    return await _register_full(existing, label, uid, "", presel=presel, log=False, origin=origin)
