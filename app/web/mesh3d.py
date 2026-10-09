"""3D-артефакты модели: мета для вьюера, сборка GLB-сцены по деталям, меш заказа для деска.

Один предмет: как модель превращается в ТО, ЧТО РИСУЕТ 3D-вьюер (B1) — гейт «показывать ли
3D» (`_model3d_meta`), серверная сборка бинарного GLB со сценой из деталей и слотами цветов
в именах узлов (`_model3d_glb_bytes`) и отдача меша РАЗМЕЩЁННОГО заказа деск-карточке
(`_api_admin_orderfile`).

Выделено из app/webapp.py волной 9 endgame-плана. Константы бюджета (3D-гейт и кап граней)
переехали сюда же: их читают и эти функции, и оставшиеся в webapp клиентские пути
(`/api/modelfile`, `_register_full` — уедут волнами 14/15), поэтому webapp реэкспортирует
их фасадом. ПОДМЕНА В ТЕСТАХ — В ОБА модуля: setattr(webapp, "_MODEL3D_MAX_SRC_BYTES", …)
перепривязывает только копию webapp, а функции этого модуля читают свою.

`_model3d_stl_bytes` не переехал: был мёртвым кодом и УДАЛЁН из webapp 21.08 (вопрос 2
endgame-плана и ждёт санкции владельца; до неё лежит в webapp как лежал.

ЕДИНСТВЕННОЕ отличие от дословного переноса — ленивый импорт внутри `_model3d_glb_bytes`
поднялся с `from . import cache, geometry` на `from .. import …`: модуль теперь на уровень
глубже. Остальные тела перенесены ДОСЛОВНО.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from .. import config, db, inspect3mf, preview
from ..bedspec import DEFAULT_BED, BedSpec, bed_key_suffix
from ..webauth import _admin_forbidden, _verify_admin
from .common import _img_headers, _toint

log = logging.getLogger(__name__)

_MODEL3D_MAX_SRC_BYTES = 384 * 1024 * 1024
_MODEL3D_FACES = 300_000   # бюджет граней интерактивного 3D-GLB — щедрее растрового превью (телефон тянет)
_GLB_FACE_CAP = 350_000    # ЖЁСТКИЙ потолок граней всей сцены GLB: пол 25к/деталь не должен пухнуть
                           # на много-детальных наборах (иначе телефонный вьюер не грузит за таймаут)
_MODEL3D_EXTS = {".stl", ".obj", ".3mf"}

def _model3d_meta(paths) -> dict:
    """Мета для 3D-просмотра (B1): расширение и СУММАРНЫЙ размер файлов модели + флаг «показывать
    3D». Гейтим на бэке (а не на клиенте), чтобы кнопка «3D» решала показываться БЕЗ
    предварительного скачивания меша: неподъёмный набор и .step/.stp дают model_viewable=false →
    мини-апп остаётся на PNG. Размер меряем по ВСЕМ файлам: GLB собирается из всего набора
    (кейс «Флекси Дарт Вейдер» — 6 STL, гейт по первому файлу тут врал бы в обе стороны)."""
    ext, size = "", 0
    try:
        paths = inspect3mf.drop_redundant_plate_stls(list(paths))   # дубль-STL не считаем (24.07)
        ext = Path(paths[0]).suffix.lower()
        for p in paths:
            try:
                size += Path(p).stat().st_size
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    viewable = bool(ext in _MODEL3D_EXTS and 0 < size <= _MODEL3D_MAX_SRC_BYTES)
    return {"model_ext": ext, "model_bytes": size, "model_viewable": viewable}

def _model3d_glb_bytes(paths, bed: BedSpec = DEFAULT_BED) -> "bytes | None":
    """B1 (редизайн 2026-07-23): меш из ЛЮБОГО формата → БИНАРНЫЙ GLB со СЦЕНОЙ из отдельных
    ДЕТАЛЕЙ. В отличие от удалённого _model3d_stl_bytes (склеивал всё в один меш), детали сохраняются
    как узлы scene — это НУЖНО для мультицвета: клиент красит part[i] в свой цвет в реальном времени.

    Детали берём через geometry.load_print_meshes_with_ids (тот же разбор, что у цветного
    превью/слайса: дедуп копий, production-3mf, негативы) — part-id каждой детали ведёт к её
    экструдеру (inspect3mf.part_extruders) и дальше к СЛОТУ цвета: слоты нумеруем по занятым
    экструдерам по возрастанию — ровно так ModelInfo.used_colours собирает mc_colors, поэтому
    клиентский colors_sel[slot] ложится на правильную деталь сборки. Слот проносим ИМЕНЕМ узла
    f"part{i}_e{ext}_s{slot}" (без экструдера — просто f"part{i}", клиент откатится на раскраску
    по порядку). Бюджет граней делим на все детали, каждую дециируем preview._decimate — суммарно
    вьюер грузит не тяжелее STL-пути. None → меш не разобрался → мини-апп на PNG.

    bed — стол ПРИНТЕРА РАСЧЁТА (B1, хвосты; DEFAULT_BED = прежнее поведение). Он решает,
    дедупить ли наложенные копии, то есть и СКОЛЬКО узлов будет в сцене, и в каком ПОРЯДКЕ —
    а клиентский фолбэк красит colors[i] именно по порядку. Тот же стол обязан идти в
    preview.render_multicolor_preview, иначе вьюер и картинка сметы покрасят разные детали.
    Стол входит и в ключ кэша GLB (дефолтный → прежнее имя файла, см. bed_key_suffix)."""
    try:
        import trimesh

        # относительный импорт поднялся на уровень при переезде в app/web/ (единственное
        # отличие от дословного переноса)
        from .. import cache, geometry
        # дубль модели в карточке (полный 3mf + STL-россыпь «Plate N») рисовал модель ДВАЖДЫ (24.07)
        paths = inspect3mf.drop_redundant_plate_stls(list(paths))
        # КЭШ готового GLB по содержимому файлов: тяжёлый разбор .3mf + дециацию + экспорт платим ОДИН
        # раз, повторный показ вьюера (в т.ч. после смены цвета, которая НЕ трогает url) — мгновенно.
        cf = None
        try:
            key = cache.model_hash([Path(p) for p in paths])
            gdir = config.CACHE_DIR / "geo"
            gdir.mkdir(parents=True, exist_ok=True)
            # v4: крашеные детали режутся на подмеши по paint_color (Dyson 3b91dbaa), STL-наборы
            # с цветами в именах получают слот группы (Ам Ням 2f2a102c); v3: мост component→object;
            # v5: общий кап граней сцены — StlAggregator-сборки на десятки деталей больше не пухнут;
            # v6: детектор мис-ассембли production-3mf ловит дубли компонентов и при заявленном
            # face_count (заказ #136 — панель рисовалась дважды разными цветами) → пересобрать
            # стол в ключе: сцена зависит от него через дедуп копий (хвост пустой для
            # дефолтного стола — прогретые записи CC2 остаются верными, версию не бампаем)
            cf = gdir / f"glb_{key}_{_MODEL3D_FACES}_v6{bed_key_suffix(bed)}.glb"
            if cf.exists():
                cached = cf.read_bytes()
                if cached:
                    return cached
        except Exception:  # noqa: BLE001
            cf = None
        triples = geometry.load_print_meshes_with_ids([Path(p) for p in paths], bed)
        triples = [t for t in (triples or []) if hasattr(t[0], "faces") and len(t[0].faces)]
        if not triples:
            return None
        # деталь → экструдер → слот цвета; крашеная по граням деталь дополнительно режется на
        # подмеши по филаментам покраски; STL-набор без .3mf — слот по цветовой группе имени
        # файла. Всё из ЕДИНОГО geometry.part_color_context: превью красится теми же слотами,
        # а клиентский слот k гарантированно указывает на mc_colors[k].
        ctx = geometry.part_color_context(paths)
        slot_of, ext_maps = ctx["slot_of"], ctx["ext_maps"]
        # (mesh, ext|None, slot|None) на каждый БУДУЩИЙ узел сцены (крашеная деталь → несколько)
        nodes: list = []
        for mesh, pid, src in triples:
            emap = ext_maps.get(str(src)) or {}
            ext = emap.get(geometry._base_part_id(str(pid), set(emap))) if pid is not None else None
            pmap = ctx["paint"].get(str(src)) or {}
            states = pmap.get(geometry._base_part_id(str(pid), set(pmap))) if pid is not None else None
            subs = geometry.split_mesh_by_paint(mesh, states, ext) if states else None
            if subs:
                for sub, se in subs:
                    nodes.append((sub, se, slot_of.get(se) if se is not None else None))
            elif ext is not None:
                nodes.append((mesh, ext, slot_of.get(ext)))
            else:
                nodes.append((mesh, None, ctx["file_slots"].get(str(src))))
        n_parts = len(nodes)
        # Бюджет граней на деталь: не мельче общего бюджета поровну (_MODEL3D_FACES // n), но и не
        # мельче пола 25к, чтобы малодетальные модели не пикселило. ВАЖНО: пол сам по себе не
        # ограничен — у StlAggregator-сборок бывают десятки деталей («Сова Zippy»: 50 деталей →
        # 25к×50 = 1.25М граней, GLB 14.5 МБ, клиентский вьюер не укладывался в 30-сек таймаут и
        # показывал ПУСТОЙ 3D). Кап на всю сцену (_GLB_FACE_CAP) держит пол в узде: при большом
        # числе деталей делим общий кап, а не даём каждой по 25к.
        target_each = max(_MODEL3D_FACES // n_parts, min(25_000, _GLB_FACE_CAP // n_parts))
        scene = trimesh.Scene()
        for i, (mesh, ext, slot) in enumerate(nodes):
            try:
                d = preview._decimate(mesh, target_each)
            except Exception:  # noqa: BLE001
                d = mesh
            if d is None or not len(getattr(d, "faces", [])):
                continue
            name = f"part{i}"   # СТАБИЛЬНЫЙ порядок = порядок load_print_meshes (клиентский фолбэк colors[i])
            if ext is not None:
                name += f"_e{ext}"   # суффиксы читает клиент (screens_3d.normalizeGltf): _e — отладка
            if slot is not None:
                name += f"_s{slot}"  # _s — слот цвета (согласован с mc_colors)
            scene.add_geometry(d, geom_name=name, node_name=name)
        if not scene.geometry:
            return None
        data = scene.export(file_type="glb")   # бинарный glTF (bytes)
        if data and cf is not None:
            try:
                cf.write_bytes(data)   # идемпотентно (то же содержимое), гонка параллельных показов безвредна
            except Exception:  # noqa: BLE001
                pass
        return data or None
    except Exception:  # noqa: BLE001
        log.debug("3d glb export failed", exc_info=True)
        return None

async def _order_bed(r) -> BedSpec:
    """Стол принтера ЗАКАЗА — чтобы деск-вьюер судил о копиях так же, как судила смета.

    Принтер в заказе хранится ИМЕНЕМ (тот же разбор, что у calcservice._resolve_printer):
    переименовали/удалили принтер — падаем на принтер по умолчанию, и это стоит видеть в логе,
    потому что вьюер тогда покажет заказ по чужому столу. Ничего не нашлось — DEFAULT_BED,
    ровно прежнее поведение."""
    name = str(getattr(r, "printer", "") or "")
    try:
        printer = next((x for x in await db.list_printers(active_only=False)
                        if x.name == name), None)
        if printer is None:
            if name:
                log.info("orderfile: принтер %r не найден — стол беру у принтера по умолчанию", name)
            printer = await db.get_default_printer()
        if printer is not None:
            return BedSpec.from_machine_profile(getattr(printer, "machine_profile", None))
    except Exception:  # noqa: BLE001 — стол не восстановился: считаем как раньше, а не падаем
        log.debug("order bed lookup failed", exc_info=True)
    return DEFAULT_BED


async def _api_admin_orderfile(request: web.Request) -> web.StreamResponse:
    """3D-просмотр РАЗМЕЩЁННОГО заказа в карточке ПК-админки (B1 для мастера): отдаёт модель
    заказа бинарным GLB со СЦЕНОЙ из деталей (part{i}_e{ext}_s{slot}, как у клиента) — деск-
    вьюер красит каждую деталь в цвет её слота из цветов заказа (OpenTodo a3dff103).

    Отличие от /api/modelfile: там источник — transient-токен из _models (живёт только пока
    открыта смета), здесь — ПЕРСИСТЕНТНЫЙ requests.file_path заказа (тот же путь, что у «Открыть
    в слайсере»/«Открыть папку»). Авторизация — как у всех /api/admin/*: только админ (иначе 403).
      • 403 — подпись невалидна ИЛИ пользователь не админ;
      • 404 — заказ/файл не найден, формат не 3D (.step/.stp и пр.) или меш слишком тяжёлый.
    При 403/404 деск-карточка тихо остаётся на PNG-превью — сбой 3D заказ не блокирует."""
    if not _verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    r = await db.get_request(_toint(request.query.get("rid")))
    path = getattr(r, "file_path", "") if r else ""
    if not path:
        raise web.HTTPNotFound()                       # заказа нет / у него не сохранён файл модели
    p = Path(str(path))
    ext = p.suffix.lower()
    # .step/.stp three.js не парсит без OpenCASCADE → 3D не предлагаем (гейт _MODEL3D_EXTS = тот же,
    # что у model_viewable клиента); файла нет на диске — тоже нечего показывать.
    if ext not in _MODEL3D_EXTS or not p.is_file():
        raise web.HTTPNotFound()
    if p.stat().st_size > _MODEL3D_MAX_SRC_BYTES:      # страховка: неподъёмный исходник не разбираем
        raise web.HTTPNotFound()
    # Тот же серверный извлекатель, что у клиента: любой формат (в т.ч. Bambu/MakerWorld
    # production-.3mf) → бинарный GLB со сценой из деталей (слоты цветов в именах узлов);
    # тяжёлый разбор в отдельном потоке, результат дециирован/кэширован (glb_*_v4).
    data = await asyncio.to_thread(_model3d_glb_bytes, [str(p)], await _order_bed(r))
    if not data:
        raise web.HTTPNotFound()                       # меш не разобрался → деск остаётся на PNG
    headers = dict(_img_headers(request, private=True))
    headers["Content-Type"] = "model/gltf-binary"
    return web.Response(body=data, headers=headers)
