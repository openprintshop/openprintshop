"""Рендер превью модели в PNG — собственный софт-растеризатор с z-буфером (CPU, без GPU).

Почему не matplotlib 3D: он рисует грани по алгоритму художника без настоящего z-буфера,
поэтому на плотных мешах задние грани просвечивают сквозь передние — «крошка»/артефакты.
Здесь: проекция → отсечение задних граней → попиксельный z-буфер → корректная видимость.

Поддержка переливающихся цветов: на вход можно дать СПИСОК цветов — тогда красим градиентом
по высоте (Z) через цвета вершин. Один цвет — равномерная заливка. GPU/OpenGL не трогаем.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
import zipfile
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

from . import cache, config, geometry
from .bedspec import DEFAULT_BED, BedSpec, bed_key_suffix

log = logging.getLogger(__name__)

# Сериализация тяжёлой загрузки одной и той же модели: если фоновый рендер модели ещё
# грузит её 13-млн-граней меш, параллельный рендер результата подождёт и возьмёт из кэша,
# а не будет грузить второй раз.
_geo_locks: dict = {}
_geo_locks_guard = threading.Lock()


def _geo_lock(name: str) -> threading.Lock:
    with _geo_locks_guard:
        lk = _geo_locks.get(name)
        if lk is None:
            lk = threading.Lock()
            _geo_locks[name] = lk
        return lk

_DEF_COLOR = (0.23, 0.55, 0.95)
_MAX_FACES = 55000   # предел граней для растеризации (баланс скорость/качество)
_SS = 2              # суперсэмплинг: рендерим ×2 и уменьшаем — мягкое сглаживание краёв
_OUT = 512           # итоговый размер PNG
_FONT = None


def _font():
    global _FONT
    if _FONT is None:
        try:
            ttf = config.BASE_DIR / "assets" / "fonts" / "DejaVuSans.ttf"
            _FONT = ImageFont.truetype(str(ttf), 16)
        except Exception:  # noqa: BLE001
            _FONT = ImageFont.load_default()
    return _FONT


def _load_dedup(model_paths: list[Path], bed: BedSpec = DEFAULT_BED) -> "trimesh.Trimesh | None":
    """Геометрия из файлов с дедупликацией наложенных копий (см. app.geometry) — чтобы
    дубли-копии всей модели не рисовались поверх друг друга, а структура (флекси) сохранялась.

    Для модели-ТИРАЖА (один тяжёлый меш ×N копий, напр. «100 mini ducks») рисуем ПО ОДНОМУ
    экземпляру каждого уникального меша: склейка 100 копий тяжёлого меша даёт мульти-компонентный
    меш, который дециируется МИНУТАМИ (и не доходит до цели) и жрёт память. Один экземпляр —
    полноценное превью того же объекта, а дециация одного компонента быстра.

    bed — стол ПРИНТЕРА РАСЧЁТА, приходит параметром от вызывающего (calcservice/clientflow
    знают принтер; DEFAULT_BED = прежнее поведение). Он тут не украшение: по fit_xy_mm
    geometry решает, мультиплейт ли раскладка, а значит — дедупить ли наложенные копии. Пока
    стол сюда не доходил, картинка судила о копиях по CC2, а смета — по реальному принтеру,
    и на широком столе смета копии схлопывала, а вьюер показывал все (привязка цветов к
    деталям по индексу уезжала следом). Тиражная ветка (unit_groups) от стола не зависит —
    её гейты про число инстансов и бюджет граней."""
    paths = [Path(p) for p in model_paths]
    try:
        groups = geometry.unit_groups(paths, bed)
    except Exception:  # noqa: BLE001
        groups = None
    if groups:                                # тираж: один экземпляр на каждый уникальный меш
        meshes = [m for m, _ in groups]
    else:
        meshes = geometry.load_print_meshes(paths, bed)
    if not meshes:
        return None
    return meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)


def _decimate(mesh: "trimesh.Trimesh", target: int) -> "trimesh.Trimesh":
    if len(mesh.faces) <= target:
        return mesh
    try:
        d = mesh.simplify_quadric_decimation(face_count=target)
        if d is not None and len(d.faces):
            return d
    except Exception:  # noqa: BLE001
        log.debug("decimation failed", exc_info=True)
    return mesh  # БЕЗ рандом-сэмплинга (он рвёт меш в решето) — рисуем как есть


def _atomic_save(mesh: "trimesh.Trimesh", cf: Path) -> None:
    """Записать меш в файл кэша атомарно: временное имя рядом с целью + os.replace.

    Тонкость, из-за которой дисковый кэш геометрии не работал НИ РАЗУ (гигиена 22.08.2026):
    trimesh выбирает экспортёр ПО РАСШИРЕНИЮ имени, и на прежнем `<stem>.<hex>.tmp` падал
    `ValueError: ('%s exporter not available!', 'tmp')` — исключение глотал `except` вызывающего,
    и кэш молча не писался (каждое превью дециировало модель заново). Поэтому здесь имя вида
    `<полное имя цели>.<hex>.part` (расширение цели остаётся внутри имени, но готовым кэшем
    такой файл ни один glob не сочтёт) и ЯВНЫЙ `file_type`. Уникальный hex в имени — защита от
    гонки параллельных рендеров, недописанный файл за собой убираем.
    """
    tmp = cf.with_name(f"{cf.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        mesh.export(str(tmp), file_type=cf.suffix.lstrip("."))
        os.replace(str(tmp), str(cf))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _geo_cache_path(model_paths, target: int,
                    bed: BedSpec = DEFAULT_BED) -> "Path | None":
    """Имя файла дискового кэша геометрии превью (None — хэш не посчитался, кэш пропускаем).

    Вынесено из _load_decimated отдельной функцией, чтобы состав ключа можно было проверить
    тестом, не гоняя разбор модели и не завися от того, дошла ли запись на диск."""
    try:
        key = cache.model_hash(model_paths)
        d = config.CACHE_DIR / "geo"
        d.mkdir(parents=True, exist_ok=True)
        # v11: детектор мис-ассембли production-3mf больше не слепнет на повторах размещений
        #      (заказ #136, CATAN card holder box: верхняя панель лежала в кэше нарисованной
        #      дважды) — геометрия, собранная старым путём, могла нести призрачные копии.
        # v10: production-сборка учитывает столы (объекты вне стола не печатаются), матрицы
        #      деталей и assemble-позиции из model_settings.config (OpenTodo 90b32407,
        #      «пивные открывашка»: лишний объект + детали кучей) — старый кэш геометрии стух.
        # v9: production-3mf собираются вручную при кратном расхождении граней (041be6d4,
        #     springy kitty ×8) — кэш геометрии, собранный старым путём, мог быть раздут.
        # v8: превью тиражей рисует ОДИН экземпляр (unit_groups) — старый кэш геометрии стух
        #
        # СТОЛ В КЛЮЧЕ (B1, хвосты): геометрия зависит от стола через дедуп наложенных копий,
        # поэтому имя несёт отпечаток стола. Хвост ПУСТОЙ для дефолтного стола (bed_key_suffix),
        # то есть прогретые записи CC2 остаются верными и версию бампать не за что — принтер
        # с другим столом просто получает собственное имя файла.
        return d / f"{key}_{target}_v11{bed_key_suffix(bed)}.stl"
    except Exception:  # noqa: BLE001
        return None


def _load_decimated(model_paths: list[Path], target: int,
                    bed: BedSpec = DEFAULT_BED) -> "trimesh.Trimesh | None":
    """Загрузка + дециация с кэшем по содержимому файлов: тяжёлый разбор .3mf (у плотных
    моделей это десятки секунд) платим ОДИН раз, цветной рендер результата берёт готовое."""
    cf = _geo_cache_path(model_paths, target, bed)
    lock = _geo_lock(cf.name) if cf is not None else None
    if lock is not None:
        lock.acquire()
    try:
        if cf is not None and cf.exists():
            try:
                m = trimesh.load(str(cf), force="mesh")
                if hasattr(m, "faces") and len(m.faces):
                    return m
            except Exception:  # noqa: BLE001
                log.debug("geo cache load failed", exc_info=True)
        mesh = _load_dedup([Path(p) for p in model_paths], bed)
        if mesh is None or not len(mesh.faces):
            return None
        mesh = _decimate(mesh, target)
        if cf is not None:
            try:
                _atomic_save(mesh, cf)
            except Exception:  # noqa: BLE001
                log.debug("geo cache save failed", exc_info=True)
        return mesh
    finally:
        if lock is not None:
            lock.release()


def _gradient_vertex_colors(verts: np.ndarray, colors: np.ndarray) -> np.ndarray:
    """Цвет каждой вершины по её высоте (Z) через цвета-стопы — эффект «переливания»."""
    if len(colors) == 1:
        return np.tile(colors[0], (len(verts), 1))
    z = verts[:, 2].astype(np.float64)
    zmin, zmax = float(z.min()), float(z.max())
    t = (z - zmin) / (zmax - zmin) if zmax > zmin else np.zeros_like(z)
    K = len(colors)
    pos = t * (K - 1)
    i0 = np.clip(np.floor(pos).astype(int), 0, K - 2)
    frac = (pos - i0)[:, None]
    return colors[i0] * (1 - frac) + colors[i0 + 1] * frac


_GPU_RASTER_BROKEN = False     # фатальный сбой GPU-пути в этом процессе → numpy до перезапуска


def _rasterize_gpu(mesh: "trimesh.Trimesh", colors: np.ndarray, W: int, H: int, vertex_colors=None):
    """GPU-растеризация (OpenTodo 9b62b4f8): та же математика, что в numpy-пути, но все грани
    параллельно на CUDA. Z-буфер в два прохода: (1) scatter_reduce(amax) глубины по пикселям,
    (2) пересчёт кандидатов и запись цвета там, где глубина победила (fp32 → сравнение точное,
    т.к. оба прохода считают одинаково). На RTX 5070 кадр 1024² на 55К граней ≈ 30–80 мс
    вместо секунд. None — GPU недоступен/занят/ошибка: вызывающий уходит в numpy-фолбэк."""
    global _GPU_RASTER_BROKEN
    if _GPU_RASTER_BROKEN or not config.PREVIEW_GPU:
        return None
    # torch импортирует ТОЛЬКО выделенный рендер-воркер (OpenTodo 745aa243): иначе каждый из
    # воркеров общего пула тащил бы свой torch (~1 ГБ RAM) и CUDA-контекст, а после ресайклинга
    # воркера — заново («кушается много ОЗУ»). Прочие процессы честно считают numpy.
    if os.environ.get("PCB_RENDER_WORKER") != "1":
        return None
    try:
        import torch
        if not torch.cuda.is_available():
            _GPU_RASTER_BROKEN = True
            return None
        free, _total = torch.cuda.mem_get_info()
        if free < 1200 * 1024 * 1024:           # карта занята (SDXL/игра) — не лезем, numpy
            return None
        dev = torch.device("cuda")
        # np.array(...) — копия: trimesh отдаёт read-only массивы, torch на них ругается
        V = torch.as_tensor(np.array(mesh.vertices), dtype=torch.float32, device=dev)
        F = torch.as_tensor(np.array(mesh.faces), dtype=torch.long, device=dev)
        fn = torch.as_tensor(np.array(mesh.face_normals), dtype=torch.float32, device=dev)
        vcol_np = vertex_colors if vertex_colors is not None \
            else _gradient_vertex_colors(mesh.vertices.astype(np.float64), colors)
        vcol = torch.as_tensor(np.asarray(vcol_np, dtype=np.float64), dtype=torch.float32, device=dev)

        el, az = np.radians(22), np.radians(-60)
        cam = torch.tensor([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)],
                           dtype=torch.float32, device=dev)
        up0 = torch.tensor([0.0, 0.0, 1.0], dtype=torch.float32, device=dev)
        right = torch.linalg.cross(up0, cam); right = right / torch.linalg.norm(right)
        up = torch.linalg.cross(cam, right)

        c = (V.amin(0) + V.amax(0)) / 2.0
        P = V - c
        sx = P @ right; sy = P @ up; dep = P @ cam
        span = float(max((sx.max() - sx.min()).item(), (sy.max() - sy.min()).item())) * 0.55 or 1.0
        px = (sx / span * 0.5 + 0.5) * (W - 1)
        py = (1 - (sy / span * 0.5 + 0.5)) * (H - 1)

        light = torch.tensor([0.4, 0.5, 0.85], dtype=torch.float32, device=dev)
        light = light / torch.linalg.norm(light)
        inten = (fn @ light).clamp(0.0, 1.0)
        shade = 0.30 + 0.70 * inten
        spec = inten ** 12 * 0.5
        facing = (fn @ cam) > 0

        tx = px[F]; ty = py[F]; td = dep[F]            # (N,3)
        fc = vcol[F]                                    # (N,3,3)
        ax, ay = tx[:, 0], ty[:, 0]
        bx, by = tx[:, 1], ty[:, 1]
        cx, cy = tx[:, 2], ty[:, 2]
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        keep = facing & (det.abs() > 1e-9)
        xmin = tx.amin(1).floor().clamp(0, W - 1).long()
        xmax = tx.amax(1).ceil().clamp(0, W - 1).long()
        ymin = ty.amin(1).floor().clamp(0, H - 1).long()
        ymax = ty.amax(1).ceil().clamp(0, H - 1).long()
        keep &= (xmax >= xmin) & (ymax >= ymin)
        idxs = torch.nonzero(keep, as_tuple=False).squeeze(1)
        if idxs.numel() == 0:
            return np.ones((H, W, 3), dtype=np.float64)

        bw = (xmax - xmin + 1)[idxs]
        bh = (ymax - ymin + 1)[idxs]
        order = torch.argsort(bw * bh)
        idxs = idxs[order]

        zbuf = torch.full((H * W,), -1e18, dtype=torch.float32, device=dev)
        img = torch.ones((H * W, 3), dtype=torch.float32, device=dev)
        BUDGET = 2_000_000                           # сэмплов на батч (~100 МБ временных тензоров)

        def _batches():
            n = idxs.numel()
            s = 0
            while s < n:
                # размер патча в батче диктует самый крупный треугольник его хвоста
                e = min(n, s + 4096)
                while True:
                    mw = int(bw[order[s:e]].max().item())
                    mh = int(bh[order[s:e]].max().item())
                    if (e - s) * mw * mh <= BUDGET or e - s <= 1:
                        break
                    e = s + max(1, (e - s) // 2)
                yield idxs[s:e], mw, mh
                s = e

        def _candidates(fi, mw, mh):
            n = fi.numel()
            ox = torch.arange(mw, device=dev)
            oy = torch.arange(mh, device=dev)
            gx = (xmin[fi].view(n, 1, 1) + ox.view(1, 1, mw)).float()
            gy = (ymin[fi].view(n, 1, 1) + oy.view(1, mh, 1)).float()
            valid = (gx <= xmax[fi].view(n, 1, 1).float()) & (gy <= ymax[fi].view(n, 1, 1).float())
            d = det[fi].view(n, 1, 1)
            w0 = ((by[fi].view(n, 1, 1) - cy[fi].view(n, 1, 1)) * (gx - cx[fi].view(n, 1, 1))
                  + (cx[fi].view(n, 1, 1) - bx[fi].view(n, 1, 1)) * (gy - cy[fi].view(n, 1, 1))) / d
            w1 = ((cy[fi].view(n, 1, 1) - ay[fi].view(n, 1, 1)) * (gx - cx[fi].view(n, 1, 1))
                  + (ax[fi].view(n, 1, 1) - cx[fi].view(n, 1, 1)) * (gy - cy[fi].view(n, 1, 1))) / d
            w2 = 1 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & valid
            zz = (w0 * td[fi, 0].view(n, 1, 1) + w1 * td[fi, 1].view(n, 1, 1)
                  + w2 * td[fi, 2].view(n, 1, 1))
            pix = (gy.long() * W + gx.long())
            return inside, zz, pix, (w0, w1, w2)

        # проход 1: глубина
        for fi, mw, mh in _batches():
            inside, zz, pix, _w = _candidates(fi, mw, mh)
            sel = inside.reshape(-1)
            zbuf.scatter_reduce_(0, pix.reshape(-1)[sel], zz.reshape(-1)[sel], reduce="amax")
        # проход 2: цвет там, где глубина победила
        for fi, mw, mh in _batches():
            inside, zz, pix, (w0, w1, w2) = _candidates(fi, mw, mh)
            n = fi.numel()
            col = (w0.unsqueeze(-1) * fc[fi, 0].view(n, 1, 1, 3)
                   + w1.unsqueeze(-1) * fc[fi, 1].view(n, 1, 1, 3)
                   + w2.unsqueeze(-1) * fc[fi, 2].view(n, 1, 1, 3))
            col = (col * shade[fi].view(n, 1, 1, 1) + spec[fi].view(n, 1, 1, 1)).clamp(0.0, 1.0)
            sel = inside.reshape(-1)
            pixf = pix.reshape(-1)[sel]
            zzf = zz.reshape(-1)[sel]
            win = zzf >= zbuf[pixf]
            img[pixf[win]] = col.reshape(-1, 3)[sel][win]
        out = img.view(H, W, 3).cpu().numpy().astype(np.float64)
        return out
    except Exception:  # noqa: BLE001
        log.warning("GPU-растеризация не удалась — numpy-фолбэк до перезапуска", exc_info=True)
        _GPU_RASTER_BROKEN = True
        return None


def gpu_selfcheck() -> tuple:
    """Проверка GPU-растеризации в рендер-воркере (для лога при старте): (ok, описание)."""
    try:
        tri = trimesh.Trimesh(vertices=[[0, 0, 0], [10, 0, 0], [0, 10, 5]],
                              faces=[[0, 1, 2]], process=False)
        out = _rasterize_gpu(tri, _norm_colors(None), 64, 64)
        if out is None:
            return False, ("PREVIEW_GPU=0" if not config.PREVIEW_GPU else
                           "CUDA недоступна/занята — numpy-фолбэк")
        import torch
        return True, torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001
        return False, f"ошибка: {exc}"


def _rasterize(mesh: "trimesh.Trimesh", colors: np.ndarray, W: int, H: int, vertex_colors=None) -> np.ndarray:
    gpu = _rasterize_gpu(mesh, colors, W, H, vertex_colors)
    if gpu is not None:
        return gpu
    V = mesh.vertices.astype(np.float64)
    F = mesh.faces
    fn = mesh.face_normals.astype(np.float64)
    vcol = np.asarray(vertex_colors, dtype=np.float64) if vertex_colors is not None \
        else _gradient_vertex_colors(V, colors)

    # камера (орто-проекция) под углом, как было: elev=22, azim=-60
    el, az = np.radians(22), np.radians(-60)
    cam = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    up0 = np.array([0.0, 0.0, 1.0])
    right = np.cross(up0, cam); right /= np.linalg.norm(right)
    up = np.cross(cam, right)

    c = (V.min(0) + V.max(0)) / 2.0
    P = V - c
    sx = P @ right; sy = P @ up; dep = P @ cam
    span = max(sx.max() - sx.min(), sy.max() - sy.min()) * 0.55 or 1.0
    px = (sx / span * 0.5 + 0.5) * (W - 1)
    py = (1 - (sy / span * 0.5 + 0.5)) * (H - 1)

    light = np.array([0.4, 0.5, 0.85]); light /= np.linalg.norm(light)
    inten = np.clip(fn @ light, 0.0, 1.0)
    shade = 0.30 + 0.70 * inten           # ambient + диффуз
    spec = inten ** 12 * 0.5              # блик
    facing = (fn @ cam) > 0              # только грани «к камере»

    img = np.ones((H, W, 3), dtype=np.float64)
    zbuf = np.full((H, W), -1e18)

    tx = px[F]; ty = py[F]; td = dep[F]
    fc = vcol[F]
    xmin = np.clip(np.floor(tx.min(1)).astype(int), 0, W - 1)
    xmax = np.clip(np.ceil(tx.max(1)).astype(int), 0, W - 1)
    ymin = np.clip(np.floor(ty.min(1)).astype(int), 0, H - 1)
    ymax = np.clip(np.ceil(ty.max(1)).astype(int), 0, H - 1)

    for i in range(len(F)):
        if not facing[i]:
            continue
        x0, x1, y0, y1 = xmin[i], xmax[i], ymin[i], ymax[i]
        if x1 < x0 or y1 < y0:
            continue
        ax, ay = tx[i, 0], ty[i, 0]
        bx, by = tx[i, 1], ty[i, 1]
        cx, cy = tx[i, 2], ty[i, 2]
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-9:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
        w0 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / det
        w1 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / det
        w2 = 1 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue
        zz = w0 * td[i, 0] + w1 * td[i, 1] + w2 * td[i, 2]
        sub = zbuf[y0:y1 + 1, x0:x1 + 1]
        upd = inside & (zz > sub)
        if not upd.any():
            continue
        col = (w0[..., None] * fc[i, 0] + w1[..., None] * fc[i, 1] + w2[..., None] * fc[i, 2])
        col = np.clip(col * shade[i] + spec[i], 0.0, 1.0)
        sub[upd] = zz[upd]
        block = img[y0:y1 + 1, x0:x1 + 1]
        block[upd] = col[upd]
    return img


def _norm_colors(color) -> np.ndarray:
    if color is None:
        return np.array([_DEF_COLOR], dtype=np.float64)
    if isinstance(color, tuple):
        return np.array([color], dtype=np.float64)
    arr = [c for c in color if c is not None]
    if not arr:
        return np.array([_DEF_COLOR], dtype=np.float64)
    return np.array(arr, dtype=np.float64)


_dims_memo: dict = {}


def _fmt_ext(ext, scale: float = 1.0) -> str:
    s = float(scale or 1.0)
    return f"{ext[0] * s:.0f}×{ext[1] * s:.0f}×{ext[2] * s:.0f}"


def plate_extents(model_paths) -> list:
    """[(dx, dy, dz) мм | None] — СЫРЫЕ габариты содержимого КАЖДОГО стола (None = стол пуст,
    место в списке держим: подписи плиток выбора столов адресуются индексом). Дисковый кэш по
    содержимому модели: тяжёлый разбор .3mf платим один раз на модель.

    Заведён вместо кэша plates_dims_*_v2: после фикса детектора мис-ассембли production-3mf
    (заказ #136) старые значения могли мерить призрачные копии деталей.

    СТОЛА ЗДЕСЬ НЕТ И НЕ НАДО (B1, хвосты): весь мультиплейт-путь идёт через
    geometry.load_print_meshes_by_plate — столы там известны ТОЧНО из .3mf, дедуп наложенных
    копий сознательно выключен, порог fit_xy_mm не участвует. Значит результат от принтера
    не зависит, и стол в этом ключе давал бы промахи кэша без единого изменения картинки."""
    cf = None
    try:
        key = cache.model_hash(model_paths)
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cf = config.CACHE_DIR / f"plate_ext_{key}_v1.json"
        if cf.exists():
            return [tuple(x) if x else None for x in json.loads(cf.read_text(encoding="utf-8"))]
    except Exception:  # noqa: BLE001
        cf = None
    out: list = []
    try:
        for group in geometry.load_print_meshes_by_plate(model_paths) or []:
            if not group:
                out.append(None)
                continue
            mins = np.array([m.bounds[0] for m in group]).min(0)
            maxs = np.array([m.bounds[1] for m in group]).max(0)
            e = maxs - mins
            out.append((float(e[0]), float(e[1]), float(e[2])))
        if cf is not None:
            cf.write_text(json.dumps(out), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.warning("plate extents failed", exc_info=True)
    return out


def dims_by_plates(model_paths, scale: float = 1.0) -> str:
    """«X×Y×Z · X×Y×Z мм» — габарит КАЖДОГО стола раскладки. Пусто, если стол один (тогда
    габарит модели и есть габарит стола — его считает model_dims) или посчитать не вышло.

    Зачем: общий bbox многостольного проекта — это габарит ВСЕЙ раскладки, он неизбежно больше
    стола («711×464×117 мм» у набора мелких деталей) и клиента только пугает. Реально печатается
    стол за столом, поэтому и показывать надо по столам. Длинные списки подрезаем: четыре стола
    в подпись влезают, дальше — счётчик."""
    exts = [e for e in (plate_extents(model_paths) or []) if e]
    if len(exts) < 2:
        return ""
    parts = [_fmt_ext(e, scale) for e in exts]
    if len(parts) <= 4:
        return " · ".join(parts) + " мм"
    from .texts import plural_ru
    rest = len(parts) - 4
    return (" · ".join(parts[:4]) + " мм · ещё "
            + f"{rest} {plural_ru(rest, 'стол', 'стола', 'столов')}")


def model_dims(model_paths, scale: float = 1.0,
               bed: BedSpec = DEFAULT_BED) -> tuple[str, float]:
    """(«X×Y×Z мм», объём габ. коробки в мм³). scale — множитель масштаба расчёта: габариты
    линейны по нему, поэтому мемоизируем по (model_hash, СТОЛ) СЫРЫЕ размеры (геометрию не
    перегружаем ни на повторных расчётах, ни при смене масштаба), а множим при форматировании.

    Стол в ключе мемо обязателен: габариты берутся из _load_decimated, а тот дедупит копии
    по столу — на одном мемо по хэшу второй принтер получил бы строку габаритов, посчитанную
    по чужому столу, молча и без единой ошибки в логе (а строка уходит в смету, PDF и каталог).

    Многостольная раскладка → строка идёт ПО СТОЛАМ (dims_by_plates). Объём габаритной коробки
    остаётся общим: по нему каталог сортирует карточки «крупные → мелкие», и проект на четыре
    стола там честно крупный."""
    key = None
    ext = None
    try:
        key = (cache.model_hash(model_paths), bed)
        ext = _dims_memo.get(key)
    except Exception:  # noqa: BLE001
        key = None
    if ext is None:
        try:
            mesh = _load_decimated([Path(p) for p in model_paths], _MAX_FACES, bed)
            if mesh is None or not len(mesh.faces):
                return "", 0.0
            b = mesh.bounds[1] - mesh.bounds[0]
            ext = (float(b[0]), float(b[1]), float(b[2]))
        except Exception:  # noqa: BLE001
            log.debug("model_dims failed", exc_info=True)
            return "", 0.0
        if key is not None:
            if len(_dims_memo) >= 256:
                _dims_memo.clear()
            _dims_memo[key] = ext
    s = float(scale or 1.0)
    e = (ext[0] * s, ext[1] * s, ext[2] * s)
    gsize = float(e[0] * e[1] * e[2])
    by_plates = ""
    try:
        by_plates = dims_by_plates(model_paths, s)
    except Exception:  # noqa: BLE001
        log.debug("dims by plates failed", exc_info=True)
    return (by_plates or f"{e[0]:.0f}×{e[1]:.0f}×{e[2]:.0f} мм", gsize)


def extract_palette(image_path, k: int = 4) -> list:
    """Доминирующие цвета с фото печати → список RGB 0..1 (авто-«текстура» по фото).
    Отсекаем почти-белый фон и почти-чёрные тени, чтобы взять реальные цвета модели."""
    try:
        im = Image.open(str(image_path)).convert("RGB")
        im.thumbnail((220, 220))
        q = im.quantize(colors=max(4, k + 3), method=Image.FASTOCTREE)
        pal = q.getpalette()
        out = []
        for cnt, idx in sorted(q.getcolors() or [], reverse=True):
            r, g, b = pal[idx * 3:idx * 3 + 3]
            mx, mn = max(r, g, b), min(r, g, b)
            if mx > 240 and (mx - mn) < 14:   # почти-белый фон
                continue
            if mx < 28:                       # почти-чёрные тени
                continue
            out.append((r / 255.0, g / 255.0, b / 255.0))
            if len(out) >= k:
                break
        return out
    except Exception:  # noqa: BLE001
        log.debug("palette extract failed", exc_info=True)
        return []


# Приоритет встроенных в 3mf превью: авторский «модельный» рендер (thumbnail_middle) крупнее
# и красивее наших плоских, поэтому берём его первым; дальше — плейт-рендеры слайсера.
_3MF_THUMB_NAMES = (
    "Auxiliaries/.thumbnails/thumbnail_middle.png",
    "Auxiliaries/.thumbnails/thumbnail_3mf.png",
    "Metadata/plate_1.png",
    "Metadata/thumbnail.png",
    "Auxiliaries/.thumbnails/thumbnail_small.png",
)


def extract_3mf_thumbnail(model_paths) -> "Path | None":
    """Достать готовое превью, встроенное в .3mf (это zip: слайсеры кладут туда PNG-рендеры).
    Фолбэк для тяжёлых моделей, где render_preview падает по памяти (напр. 107 МБ / огромный
    face-count → пик десятки ГБ RAM, воркер умирает). Дёшево — распаковка одного PNG, без
    загрузки меша. Кэш по хэшу модели в PREVIEW_DIR. Возвращает Path или None; не бросает."""
    try:
        src = next((Path(p) for p in (model_paths or [])
                    if str(p).lower().endswith(".3mf") and Path(p).exists()), None)
        if src is None:
            return None
        out = config.PREVIEW_DIR / f"t3mf_{cache.model_hash([src])}.png"
        if out.exists():
            return out
        with zipfile.ZipFile(src) as z:
            names = set(z.namelist())
            pick = next((n for n in _3MF_THUMB_NAMES if n in names), None)
            if pick is None:
                # общий фолбэк: самый крупный «превьюшный» PNG (thumbnails/Metadata), кроме
                # служебных no-light-рендеров и pick-масок — они не годятся как обложка
                cands = [(z.getinfo(n).file_size, n) for n in names
                         if n.lower().endswith(".png")
                         and ("thumbnail" in n.lower() or n.startswith("Metadata/"))
                         and "_no_light" not in n and "/pick_" not in n and n.count("pick_") == 0]
                pick = max(cands)[1] if cands else None
            if pick is None:
                return None
            data = z.read(pick)
        if not data:
            return None
        config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return out
    except Exception:  # noqa: BLE001
        log.debug("extract_3mf_thumbnail failed", exc_info=True)
        return None


def render_preview(model_paths, color=None, max_faces: int = _MAX_FACES,
                   bed: BedSpec = DEFAULT_BED) -> Path | None:
    """JPEG-превью. color: None | (r,g,b) | [(r,g,b), ...] (градиент по высоте).

    bed — стол принтера расчёта: он решает, дедупить ли наложенные копии, поэтому входит и в
    геометрию, и в ключ кэша картинки (дефолтный стол → прежнее имя файла, см. bed_key_suffix)."""
    try:
        # кэш готового превью по (содержимое модели + цвет + предел граней): один и тот же
        # материал в одном цвете не растеризуется повторно. model_hash дёшев (мемо хэша файла).
        cached = None
        try:
            csig = hashlib.sha1(repr(color).encode()).hexdigest()[:10]
            # pc7_: production-сборка со столами/матрицами из model_settings (90b32407) —
            # превью с лишними объектами и деталями «кучей» пересоберутся.
            # pc6_: ручная сборка production-3mf (041be6d4) — превью с раздутой геометрией.
            cached = (config.PREVIEW_DIR /
                      f"pc7_{cache.model_hash(model_paths)}_{csig}_{max_faces}{bed_key_suffix(bed)}.jpg")
            if cached.exists():
                return cached
        except Exception:  # noqa: BLE001
            cached = None
        mesh = _load_decimated([Path(p) for p in model_paths], max_faces, bed)
        if mesh is None or not len(mesh.faces):
            return None
        colors = _norm_colors(color)

        W = H = _OUT * _SS
        img = _rasterize(mesh, colors, W, H)

        arr = (np.clip(img, 0, 1) * 255).astype(np.uint8)
        im = Image.fromarray(arr, "RGB").resize((_OUT, _OUT), Image.LANCZOS)
        try:
            ext = mesh.bounds[1] - mesh.bounds[0]
            # многостольная раскладка: подписываем габарит КАЖДОГО стола, а не всей раскладки
            label = (dims_by_plates(model_paths)
                     or f"{ext[0]:.0f} × {ext[1]:.0f} × {ext[2]:.0f} мм")
            ImageDraw.Draw(im).text((12, 10), label, fill=(70, 70, 70), font=_font())
        except Exception:  # noqa: BLE001
            pass

        # JPEG (а не PNG): затенённый 3D-рендер — фотоподобная картинка, JPEG сжимает её
        # в ~3 раза лучше PNG (≈100–140 КБ → ≈30–45 КБ) при незаметной потере. JPEG к тому же
        # родной формат фото в Telegram (превью уходит и через send_photo) — webp бы это сломал.
        out = cached or (config.PREVIEW_DIR / f"prev_{uuid.uuid4().hex[:8]}.jpg")
        im.save(out, "JPEG", quality=85, optimize=True)
        return out
    except Exception:  # noqa: BLE001
        log.warning("preview render failed", exc_info=True)
        return None


def render_multicolor_preview(model_paths, slot_rgbs=None, max_faces: int = _MAX_FACES,
                              bed: BedSpec = DEFAULT_BED) -> "Path | None":
    """Превью МНОГОЦВЕТНОЙ модели: каждая деталь красится цветом своего СЛОТА (экструдер из
    .3mf / цветовая группа имени файла; крашеные по граням детали режутся по покраске) — тем же
    geometry.part_color_context, что 3D-GLB, поэтому превью и вьюер совпадают цвет-в-цвет.
    bed — стол принтера расчёта: он решает, дедупить ли наложенные копии, а значит и СКОЛЬКО
    деталей и в каком ПОРЯДКЕ вернёт load_print_meshes_with_ids — то есть на какие детали лягут
    слоты цветов. Тот же стол обязан идти в 3D-GLB (web.mesh3d), иначе картинка и вьюер
    красят разное. Входит и в ключ кэша (дефолтный стол → прежнее имя файла).
    slot_rgbs — [(r,g,b) 0..1 по слотам] из ВЫБРАННЫХ клиентом цветов; None → дизайн-цвета
    модели (палитра .3mf / цвета групп имён). None-результат — слотов не нашлось/не разобралось:
    вызывающий откатывается на render_preview (прежний градиент останется только там, где
    цветов действительно не знаем). Тяжёлый CPU — звать через cpupool."""
    try:
        from . import geometry
        cached = None
        try:
            csig = hashlib.sha1(repr(slot_rgbs).encode()).hexdigest()[:10]
            cached = (config.PREVIEW_DIR /
                      f"pmc_{cache.model_hash(model_paths)}_{csig}_{max_faces}{bed_key_suffix(bed)}.jpg")
            if cached.exists():
                return cached
        except Exception:  # noqa: BLE001
            cached = None
        ctx = geometry.part_color_context(model_paths)
        rgbs = list(slot_rgbs or [])
        if not rgbs:
            for hx in ctx.get("mc_colors") or []:
                try:
                    rgbs.append(tuple(int(hx[i:i + 2], 16) / 255.0 for i in (1, 3, 5)))
                except Exception:  # noqa: BLE001
                    rgbs.append((0.6, 0.64, 0.68))
        if len(rgbs) < 2 or not (ctx["slot_of"] or ctx["file_slots"] or ctx["paint"]):
            return None                          # это не многоцвет — нечем/некого красить
        triples = geometry.load_print_meshes_with_ids([Path(p) for p in model_paths], bed)
        triples = [t for t in (triples or []) if hasattr(t[0], "faces") and len(t[0].faces)]
        if not triples:
            return None
        slot_of, ext_maps = ctx["slot_of"], ctx["ext_maps"]
        nodes: list = []                         # (mesh, слот|None) — как узлы GLB
        for mesh, pid, src in triples:
            emap = ext_maps.get(str(src)) or {}
            ext = emap.get(geometry._base_part_id(str(pid), set(emap))) if pid is not None else None
            pmap = ctx["paint"].get(str(src)) or {}
            states = pmap.get(geometry._base_part_id(str(pid), set(pmap))) if pid is not None else None
            subs = geometry.split_mesh_by_paint(mesh, states, ext) if states else None
            if subs:
                nodes.extend((sub, slot_of.get(se) if se is not None else None) for sub, se in subs)
            elif ext is not None:
                nodes.append((mesh, slot_of.get(ext)))
            else:
                nodes.append((mesh, ctx["file_slots"].get(str(src))))
        if not any(s is not None for _m, s in nodes):
            return None                          # ни одна деталь не привязана к слоту
        neutral = np.array((0.6, 0.64, 0.68))
        target_each = max(8_000, max_faces // max(1, len(nodes)))
        loaded: list = []
        for mesh, slot in nodes:
            try:
                d = _decimate(mesh, target_each)
            except Exception:  # noqa: BLE001
                d = mesh
            if d is None or not len(getattr(d, "faces", [])):
                continue
            col = np.array(rgbs[slot % len(rgbs)]) if slot is not None else neutral
            loaded.append((d, col))
        if not loaded:
            return None
        combined = loaded[0][0] if len(loaded) == 1 else trimesh.util.concatenate([m for m, _ in loaded])
        vcol = np.vstack([np.tile(col, (len(m.vertices), 1)) for m, col in loaded])
        W = H = _OUT * _SS
        img = _rasterize(combined, None, W, H, vertex_colors=vcol)
        arr = (np.clip(img, 0, 1) * 255).astype(np.uint8)
        im = Image.fromarray(arr, "RGB").resize((_OUT, _OUT), Image.LANCZOS)
        try:
            ext3 = combined.bounds[1] - combined.bounds[0]
            label = (dims_by_plates(model_paths)          # по столам, см. dims_by_plates
                     or f"{ext3[0]:.0f} × {ext3[1]:.0f} × {ext3[2]:.0f} мм")
            ImageDraw.Draw(im).text((12, 10), label, fill=(70, 70, 70), font=_font())
        except Exception:  # noqa: BLE001
            pass
        config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        out = cached or (config.PREVIEW_DIR / f"pmc_{uuid.uuid4().hex[:8]}.jpg")
        im.save(out, "JPEG", quality=85, optimize=True)
        return out
    except Exception:  # noqa: BLE001
        log.warning("multicolor preview render failed", exc_info=True)
        return None


def render_colored_parts(parts, cache_key=None, max_faces: int = _MAX_FACES) -> "Path | None":
    """Многоцветный рендер параметрической модели. parts = [(paths_list, (r,g,b)), …] — каждая
    часть (отдельный STL из OpenSCAD, напр. корпус/текст+рамка/полоса) красится своим цветом.
    Все части кладём в ОДИН меш (общая камера → идеально совпадают) с явными цветами вершин по
    принадлежности к части. color — (r,g,b) 0..1. Тяжёлый CPU/GPU — звать через cpupool.

    Стола здесь нет и не надо (B1, хвосты): части грузятся напрямую trimesh.load, мимо
    app.geometry — ни дедупа копий, ни порогов стола на этом пути не существует."""
    try:
        cached = (config.PREVIEW_DIR / f"p2c_{cache_key}.jpg") if cache_key else None
        if cached is not None and cached.exists():
            return cached
        loaded = []
        for paths, col in (parts or []):
            m = None
            for p in (paths or []):
                try:
                    mm = trimesh.load(str(p), force="mesh")
                    if hasattr(mm, "faces") and len(mm.faces):
                        m = mm
                        break
                except Exception:  # noqa: BLE001
                    continue
            if m is not None:
                loaded.append((m, np.array(col, dtype=np.float64)))
        if not loaded:
            return None
        meshes = [m for m, _ in loaded]
        combined = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        vcol = np.vstack([np.tile(col, (len(m.vertices), 1)) for m, col in loaded])
        W = H = _OUT * _SS
        img = _rasterize(combined, None, W, H, vertex_colors=vcol)
        arr = (np.clip(img, 0, 1) * 255).astype(np.uint8)
        im = Image.fromarray(arr, "RGB").resize((_OUT, _OUT), Image.LANCZOS)
        config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        out = cached or (config.PREVIEW_DIR / f"p2c_{uuid.uuid4().hex[:8]}.jpg")
        im.save(out, "JPEG", quality=85, optimize=True)
        return out
    except Exception:  # noqa: BLE001
        log.warning("multi-color preview render failed", exc_info=True)
        return None


def _plate_mesh_cached(model_paths, key: str, idx: int, group) -> "trimesh.Trimesh | None":
    """Дециированный меш ОДНОГО стола с дисковым кэшем (OpenTodo 65aa1a35 «ускорить отрисовку»):
    перерисовка стола в другом цвете пропускает загрузку .3mf и дециацию (самое дорогое) —
    остаётся только растеризация (~0.2–0.4 с)."""
    cf = config.CACHE_DIR / "geo" / f"plate_{key}_{idx}_v2.stl"
    try:
        if cf.exists():
            m = trimesh.load(str(cf), force="mesh")
            if hasattr(m, "faces") and len(m.faces):
                return m
    except Exception:  # noqa: BLE001
        pass
    if group is None:
        return None
    mesh = trimesh.util.concatenate(group) if len(group) > 1 else group[0]
    if mesh is None or not len(mesh.faces):
        return None
    mesh = _decimate(mesh, _MAX_FACES)
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        _atomic_save(mesh, cf)
    except Exception:  # noqa: BLE001
        log.debug("plate geo cache save failed", exc_info=True)
    return mesh


def render_plate_previews(model_paths, color=None) -> list:
    """JPEG-превью КАЖДОГО стола мультиплейт-модели (OpenTodo 314e175e) — для выбора столов,
    когда автор .3mf не вшил картинки plate_N.png, и для коллажа/перекраски столов (eb4424d9,
    65aa1a35: цвет на стол). Геометрия грузится один раз И дециированные меши столов кэшируются
    на диск — повторная перерисовка в новом цвете почти мгновенна. Возвращает список путей по
    индексам столов ('' где не вышло). Синхронно и тяжело — звать через cpupool.

    Стол принтера сюда НЕ передаётся осознанно (B1, хвосты): столы раскладки известны точно
    из .3mf (load_print_meshes_by_plate), дедуп копий там сознательно выключен — результат от
    принтера не зависит, а стол в ключе plate_*/plate_r2_* дал бы пустые промахи кэша."""
    try:
        key = cache.model_hash(model_paths)
    except Exception:  # noqa: BLE001
        key = uuid.uuid4().hex[:12]
    csig = hashlib.sha1(repr(color).encode()).hexdigest()[:8] if color else "mono"
    out: list = []
    try:
        from . import geometry
        plates = None                                 # лениво: если все меши в кэше — не грузим
        n = None
        try:                                          # точное число столов — из кэша замеров
            ext_cf = config.CACHE_DIR / f"plate_ext_{key}_v1.json"
            if ext_cf.exists():
                n = len(json.loads(ext_cf.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            n = None
        if n is None:
            plates = geometry.load_print_meshes_by_plate(model_paths) or []
            n = len(plates)
        for idx in range(n):
            cached = config.PREVIEW_DIR / f"plate_r2_{key}_{idx}_{csig}.jpg"
            if cached.exists():
                out.append(str(cached))
                continue
            try:
                mesh = _plate_mesh_cached(model_paths, key, idx,
                                          plates[idx] if plates is not None else None)
                if mesh is None and plates is None:   # меш-кэш оборвался — догружаем геометрию
                    plates = geometry.load_print_meshes_by_plate(model_paths) or []
                    if idx >= len(plates):
                        break
                    mesh = _plate_mesh_cached(model_paths, key, idx, plates[idx])
                if mesh is None:
                    out.append("")
                    continue
                W = H = _OUT * _SS
                img = _rasterize(mesh, _norm_colors(color), W, H)
                arr = (np.clip(img, 0, 1) * 255).astype(np.uint8)
                im = Image.fromarray(arr, "RGB").resize((_OUT, _OUT), Image.LANCZOS)
                im.save(cached, "JPEG", quality=85, optimize=True)
                out.append(str(cached))
            except Exception:  # noqa: BLE001
                log.warning("plate preview render failed (стол %s)", idx, exc_info=True)
                out.append("")
    except Exception:  # noqa: BLE001
        log.warning("plate previews failed", exc_info=True)
    return out


def plates_dims(model_paths) -> list:
    """Габариты каждого стола («X×Y×Z», по содержимому стола) — для подписи на плитках выбора
    столов (OpenTodo eb4424d9). Замер и его кэш — общие с dims_by_plates (plate_extents),
    поэтому плитка стола и строка габаритов в смете не могут разъехаться.

    Стола нет по той же причине, что и у plate_extents: мультиплейт-путь bed-независим."""
    return ["" if e is None else _fmt_ext(e) for e in (plate_extents(model_paths) or [])]


def compose_plate_collage(image_paths: list, dest: "Path | None" = None) -> "Path | None":
    """Коллаж из превью столов (OpenTodo eb4424d9) — единая картинка для заказа/сметы
    мультиплейт-модели: сетка до 2 колонок, подписи «Стол N» в углу каждой ячейки."""
    paths = [p for p in image_paths if p and Path(p).exists()]
    if not paths:
        return None
    if len(paths) == 1:
        return Path(paths[0])
    try:
        cols = 2
        rows = (len(paths) + cols - 1) // cols
        cell = 384
        pad = 6
        W = cols * cell + pad * (cols + 1)
        H = rows * cell + pad * (rows + 1)
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        for i, p in enumerate(paths):
            im = Image.open(p).convert("RGB")
            im.thumbnail((cell, cell), Image.LANCZOS)
            x = pad + (i % cols) * (cell + pad) + (cell - im.width) // 2
            y = pad + (i // cols) * (cell + pad) + (cell - im.height) // 2
            canvas.paste(im, (x, y))
            cx = pad + (i % cols) * (cell + pad)
            cy = pad + (i // cols) * (cell + pad)
            draw.text((cx + 10, cy + 8), f"Стол {i + 1}", fill=(90, 90, 90), font=_font())
        if dest is None:
            dest = config.PREVIEW_DIR / f"collage_{uuid.uuid4().hex[:10]}.jpg"
        canvas.save(dest, "JPEG", quality=85, optimize=True)
        return Path(dest)
    except Exception:  # noqa: BLE001
        log.warning("plate collage failed", exc_info=True)
        return None


_PLATE_RE = re.compile(r"(?i)/plate_(\d+)\.png$")


def extract_plate_thumbnails(model_paths) -> list:
    """Встроенные в .3mf картинки СТОЛОВ (Metadata/plate_N.png — их рисует слайсер автора):
    по одной на стол. Возвращает отсортированные пути к сохранённым PNG (или [] если их нет).
    Нужно для моделей на несколько столов — показываем отдельную картинку на каждый стол (#03a5727e)."""
    out: list = []
    for p in model_paths:
        p = Path(p)
        if p.suffix.lower() != ".3mf" or not p.exists():
            continue
        try:
            key = cache.model_hash([p])
        except Exception:  # noqa: BLE001
            key = p.stem
        try:
            with zipfile.ZipFile(str(p)) as z:
                found = []
                for n in z.namelist():
                    m = _PLATE_RE.search(n)
                    if m:
                        found.append((int(m.group(1)), n))
                for idx, n in sorted(found):
                    dest = config.PREVIEW_DIR / f"plate_{key}_{idx}.png"
                    if not dest.exists():
                        with z.open(n) as src, open(dest, "wb") as f:
                            shutil.copyfileobj(src, f)
                    out.append(dest)
        except Exception:  # noqa: BLE001
            log.debug("plate thumbnails extract failed for %s", p, exc_info=True)
    return out


def optimize_photo(path, max_dim: int = 1024, quality: int = 85) -> None:
    """Ужимает загруженное фото НА МЕСТЕ: ресайз до max_dim по большей стороне + JPEG q85.
    Фото из Telegram бывает до 1280px / ~200 КБ — после ≈60–100 КБ. При любой ошибке оставляет
    файл как есть (он уже скачан — лучше тяжёлый, чем никакой)."""
    try:
        p = Path(path)
        im = Image.open(str(p)).convert("RGB")
        w, h = im.size
        scale = min(1.0, max_dim / max(w, h)) if max(w, h) else 1.0
        if scale < 1.0:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        im.save(str(p), "JPEG", quality=quality, optimize=True)
    except Exception:  # noqa: BLE001
        log.debug("optimize_photo failed for %s", path, exc_info=True)
