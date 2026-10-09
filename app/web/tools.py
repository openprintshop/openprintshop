"""«Инструменты» настольной админки. Первый инструмент — допечатка с N-го слоя без сорвавшихся
деталей («Путь В», ядро — app/resumecut.py, история — docs/Уже в продукте/resume-from-layer.md,
план — docs/Перспективные разработки/resume-tool-3d-plan-2026-09-05.md).

Поток мастера:
  1. источник — g-code ТОЙ ЖЕ печати: архив заданий (app/jobarchive.py — туда сторож складывает
     всё, что слайсер и бот отправили на принтер), временные папки слайсера, файл из браузера.
     Текущая печать находится сама по имени задания на принтере;
  2. разбор — детали (по меткам слайсера или эвристикой), слои, время, подсказка «пауза была на
     слое N» из print_phases;
  3. сборка — задание с фазами и процентами (интерфейс поллит `job`): файл + сверка с исходником +
     превью слоёв; бинарные траектории для 3D — ручка `paths`;
  4. заливка и старт — ОТДЕЛЬНЫЕ подтверждения; перед стартом гейты (`gates`): принтер свободен,
     стол тёплый, файл виден на принтере, центр стола чист, стартовые переезды не задевают детали.

Ручки ничего не решают за ядро: недоступность приходит из `ResumeUnavailable` строкой «почему»
и уходит в интерфейс как `{ok: false, error}`. Путь к исходнику принимается ТОЛЬКО внутри
разрешённых корней (`_roots`): ручка с произвольным путём читала бы любой файл машины.

Аутентификация зовётся через модуль (`webauth._verify_admin`), а не связыванием по имени: так
подмена в тестах (`stub_auth`) достаёт сюда без правки списка модулей conftest.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from .. import config, db, elegoo, jobarchive, reprint, resumecut, webauth
from ..webauth import _admin_forbidden
from .common import _bad, _toint
from .printerhub import _no_printer, _printer_driver
from .printerlive import _remember_gcode_settings

log = logging.getLogger(__name__)
routes = web.RouteTableDef()

_UPLOAD_MAX_BYTES = 300 * 1024 * 1024
MAX_SOURCES = 12
MIN_SOURCE_BYTES = 10 * 1024   # мельче — не задание, а обрывок или тест слайсера
JOB_TTL_SEC = 12 * 3600
KEEP_DAYS = 7                  # загрузки и собранные файлы старше — удаляются
ANALYSIS_TTL_SEC = 3600


def tool_dir() -> Path:
    return config.DATA_DIR / "tools" / "resume"


def home_clearance_mm() -> float:
    """Зазор концевика Z над столом: выше него деталь в центре стола встретит сопло при G28.
    У CC1 4.5 мм по исходникам, у CC2 не измерен (4.16 мм не помешали) — дефолт консервативный."""
    try:
        return float(os.getenv("RESUME_HOME_CLEARANCE_MM") or 4.0)
    except ValueError:
        return 4.0


def _roots() -> list[Path]:
    return [tool_dir() / "uploads", jobarchive.archive_dir(), *jobarchive.slicer_temp_dirs()]


def _inside_roots(path: Path) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in _roots():
        try:
            rr = root.resolve()
        except OSError:
            continue
        if rp == rr or rr in rp.parents:
            return True
    return False


def _cleanup_old() -> None:
    """Загрузки, собранные файлы и кэш траекторий старше KEEP_DAYS — долой."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for sub in ("uploads", "out", "cache"):
        d = tool_dir() / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


def _display_name(p: Path, archived: dict | None) -> str:
    if archived and archived.get("name"):
        return str(archived["name"])
    if p.parent.name == "Metadata":
        return p.parent.parent.name
    return p.name


def list_sources() -> list[dict]:
    """Источники: архив заданий (новые первыми), потом сырые временные файлы слайсера, которых в
    архиве ещё нет, потом загрузки из браузера. Каждая запись — путь внутри разрешённых корней."""
    _cleanup_old()
    seen: set[tuple[int, int]] = set()
    out: list[dict] = []
    for it in jobarchive.entries(MAX_SOURCES):
        p = Path(it.get("path", ""))
        try:
            st = p.stat()
        except OSError:
            continue
        seen.add((st.st_size, int(it.get("src_mtime") or st.st_mtime)))
        seen.add((st.st_size, int(st.st_mtime)))
        try:
            info = jobarchive.head_info(p)
        except OSError:
            info = {}
        out.append({"path": str(p), "name": it.get("name") or p.name, "dir": "архив", "kind": "archive",
                    "size": st.st_size, "mtime": int(it.get("src_mtime") or st.st_mtime),
                    "age_min": int((time.time() - int(it.get("src_mtime") or st.st_mtime)) / 60),
                    "source": it.get("source", ""), **info})
    raw: list[tuple[float, Path, str]] = []
    for root, kind in [(tool_dir() / "uploads", "upload"), *[(d, "slicer") for d in jobarchive.slicer_temp_dirs()]]:
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*.gcode"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size < MIN_SOURCE_BYTES or (st.st_size, int(st.st_mtime)) in seen:
                    continue
                raw.append((st.st_mtime, p, kind))
        except OSError:
            log.debug("источники допечатки: каталог %s не прочитать", root, exc_info=True)
    raw.sort(key=lambda t: t[0], reverse=True)
    for mtime, p, kind in raw[:MAX_SOURCES]:
        try:
            info = jobarchive.head_info(p)
        except OSError:
            continue
        out.append({"path": str(p), "name": _display_name(p, None), "dir": p.parent.name, "kind": kind,
                    "size": p.stat().st_size, "mtime": int(mtime),
                    "age_min": int((time.time() - mtime) / 60), "source": kind, **info})
    return out[: MAX_SOURCES * 2]


async def _current_job(pid: int | None, drv) -> dict | None:
    """Что печатается/печаталось: имя задания из привязки или из живого статуса → запись архива."""
    name = ""
    try:
        b = await db.get_print_binding(pid)
        if b and b.get("job_name"):
            name = str(b["job_name"])
    except Exception:  # noqa: BLE001
        pass
    if not name and drv is not None and getattr(drv, "caps", None) and drv.caps.status:
        try:
            st = await asyncio.to_thread(drv.status, 30.0)
            if st and st.get("ok"):
                name = str((drv.job_state(st.get("state") or {}) or {}).get("name") or "")
        except Exception:  # noqa: BLE001
            name = ""
    if not name:
        return None
    norm = elegoo.normalize_job_name(name)
    it = jobarchive.find_by_job_name(norm, elegoo.normalize_job_name)
    if not it:
        return {"job_name": name, "path": None}
    return {"job_name": name, "path": it.get("path"), "name": it.get("name")}


@dataclass
class ResumeJob:
    id: str
    src: Path
    layer: int
    exclude: list[int]
    at: float
    status: str = "running"        # running / done / failed
    phase: str = "queue"
    pct: float = 0.0
    error: str = ""
    out: Path | None = None
    report: dict = field(default_factory=dict)
    verify: dict = field(default_factory=dict)
    previews: list = field(default_factory=list)
    uploaded: str = ""
    source_name: str = ""

    def as_dict(self, *, with_previews: bool = False) -> dict:
        d = {"job": self.id, "status": self.status, "phase": self.phase, "pct": round(self.pct, 3),
             "error": self.error, "filename": self.out.name if self.out else "",
             "uploaded": self.uploaded, "report": self.report, "verify": self.verify,
             "layer": self.layer, "exclude": self.exclude, "source_name": self.source_name}
        if with_previews:
            d["previews"] = self.previews
        return d


_JOBS: dict[str, ResumeJob] = {}
_ANALYSIS: dict[tuple[str, int, int], tuple[float, dict]] = {}


def _job(jid: str) -> ResumeJob | None:
    job = _JOBS.get(str(jid or ""))
    if job and time.time() - job.at > JOB_TTL_SEC:
        _JOBS.pop(job.id, None)
        return None
    return job


def _source_path(raw: object) -> Path | None:
    p = Path(str(raw or "")).expanduser()
    if not str(raw or "").strip() or not p.is_file() or not _inside_roots(p):
        return None
    return p


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_.")
    return s[:40] or "job"


# ------------------------------------------------------------------ ручки

@routes.get("/api/admin/tools/resume/sources")
async def _api_tools_resume_sources(request: web.Request) -> web.Response:
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    try:
        await asyncio.to_thread(jobarchive.scan_once)     # свежая отправка слайсера — сразу в архив
    except Exception:  # noqa: BLE001
        log.debug("архив заданий: проход по требованию не удался", exc_info=True)
    pid = _toint(request.query.get("printer_id")) or None
    drv = await _printer_driver(pid)
    sources = await asyncio.to_thread(list_sources)
    current = await _current_job(pid, drv)
    return web.json_response({"ok": True, "sources": sources, "current": current,
                              "roots": [str(r) for r in _roots() if r.is_dir()],
                              "home_clearance_mm": home_clearance_mm()})


@routes.post("/api/admin/tools/resume/upload")
async def _api_tools_resume_upload(request: web.Request) -> web.Response:
    """multipart: initData, file — g-code с машины мастера, если слайсер стоит не на ПК бота."""
    if not (request.content_type or "").startswith("multipart/"):
        return _bad()
    init_data, fname, data = "", "", b""
    reader = await request.multipart()
    async for part in reader:
        if part.name == "initData":
            init_data = (await part.read()).decode("utf-8", "ignore")
        elif part.name == "file":
            fname = part.filename or ""
            data = await part.read()
    if not webauth._verify_admin(init_data):
        return _admin_forbidden()
    if not data or not fname:
        return web.json_response({"ok": False, "error": "Файл не приложен"})
    if len(data) > _UPLOAD_MAX_BYTES:
        return web.json_response({"ok": False, "error": "Файл слишком большой"})
    if not fname.lower().endswith(".gcode"):
        return web.json_response({"ok": False, "error": "Нужен .gcode того же задания"})
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(fname).name).strip("_") or "job.gcode"
    dst = tool_dir() / "uploads" / f"{time.strftime('%m%d_%H%M%S')}_{safe}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(dst.write_bytes, data)
    return web.json_response({"ok": True, "path": str(dst), "name": dst.name, "size": len(data)})


def _analysis_cached(src: Path) -> dict:
    st = src.stat()
    key = (str(src), int(st.st_mtime), st.st_size)
    hit = _ANALYSIS.get(key)
    if hit and time.time() - hit[0] < ANALYSIS_TTL_SEC:
        return hit[1]
    an = resumecut.analyze(src).as_dict()
    _ANALYSIS[key] = (time.time(), an)
    if len(_ANALYSIS) > 20:
        oldest = min(_ANALYSIS, key=lambda k: _ANALYSIS[k][0])
        _ANALYSIS.pop(oldest, None)
    return an


@routes.post("/api/admin/tools/resume/analyze")
async def _api_tools_resume_analyze(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    _uid, err = webauth._admin_body(body)
    if err:
        return err
    src = _source_path(body.get("path"))
    if src is None:
        return web.json_response({"ok": False, "error": "Файл не найден или лежит вне разрешённых папок"})
    try:
        an = await asyncio.to_thread(_analysis_cached, src)
    except resumecut.ResumeUnavailable as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.warning("допечатка со слоя: разбор %s не удался", src, exc_info=True)
        return web.json_response({"ok": False, "error": f"Файл не разобрать: {exc}"})
    pid = _toint(body.get("printer_id")) or None
    suggest = None
    try:
        ph = await db.last_print_phase(pid, phases=("paused", "pausing", "stopped", "cancelled", "idle"))
        if ph and ph.get("layer"):
            suggest = {"layer": int(ph["layer"]), "phase": ph.get("phase"), "ts": ph.get("ts"),
                       "job_name": ph.get("job_name") or "", "z": ph.get("z")}
    except Exception:  # noqa: BLE001
        log.debug("допечатка со слоя: фазу печати не прочитать", exc_info=True)
    return web.json_response({**an, "suggest": suggest, "home_clearance_mm": home_clearance_mm()})


def _run_build(job: ResumeJob, expect: dict) -> None:
    """Сборка + сверка + превью в рабочем потоке; прогресс — в самом задании."""
    def prog(phase: str, pct: float) -> None:
        job.phase = phase
        job.pct = max(0.0, min(1.0, pct))

    try:
        out = tool_dir() / "out" / (f"resume_{_slug(job.source_name)}_L{job.layer}_minus{len(job.exclude)}"
                                    f"_{time.strftime('%m%d_%H%M')}_{job.id[:4]}.gcode")
        rep = resumecut.build(job.src, job.layer, job.exclude, out, expect=expect, progress=prog)
        ver = resumecut.verify(job.src, out, job.layer, job.exclude, progress=prog)
        if not ver.get("ok"):
            try:
                out.unlink()
            except OSError:
                pass
            raise resumecut.ResumeUnavailable(
                "Сверка собранного файла с исходником НЕ прошла — файл не отдаю "
                f"(экструзий {ver.get('out_count')} против {ver.get('ref_count')}, "
                f"расхождений {ver.get('differ')}, нарушений Z {ver.get('z_violations')})")
        prog("previews", 0.0)
        pv = resumecut.previews(job.src, out, resumecut.preview_layers(job.layer, rep.last_layer))
        job.out = out
        job.report = rep.as_dict()
        job.verify = ver
        job.previews = pv
        job.status = "done"
        prog("done", 1.0)
    except resumecut.ResumeUnavailable as exc:
        job.status, job.error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("допечатка со слоя: сборка не удалась", exc_info=True)
        job.status, job.error = "failed", f"Сборка не удалась: {exc}"


@routes.post("/api/admin/tools/resume/build")
async def _api_tools_resume_build(request: web.Request) -> web.Response:
    """Запуск сборки. Отвечает сразу; ход и результат — в `job`. Принтер не трогается."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid, err = webauth._admin_body(body)
    if err:
        return err
    src = _source_path(body.get("path"))
    if src is None:
        return web.json_response({"ok": False, "error": "Файл не найден или лежит вне разрешённых папок"})
    layer = _toint(body.get("layer")) or 0
    exclude = sorted({int(v) for v in (body.get("exclude") or []) if _toint(v) is not None})
    if not exclude or layer < 2:
        return web.json_response({"ok": False, "error": "Отметьте детали и укажите слой"})
    expect: dict[int, tuple[float, float]] = {}
    for k, v in (body.get("expect") or {}).items():
        i = _toint(k)
        if i is not None and isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                expect[int(i)] = (float(v[0]), float(v[1]))
            except (TypeError, ValueError):
                pass
    job = ResumeJob(id=uuid.uuid4().hex[:10], src=src, layer=int(layer), exclude=exclude,
                    at=time.time(), source_name=str(body.get("source_name") or src.stem.lstrip(".")))
    _JOBS[job.id] = job
    threading.Thread(target=_run_build, args=(job, expect), name=f"resume-{job.id}", daemon=True).start()
    await db.log_event(uid, "допечатка со слоя",
                       f"сборка «{job.source_name}» со слоя {job.layer}, вырезано "
                       f"{len(exclude)} дет. ({', '.join('#' + str(i) for i in exclude)})")
    return web.json_response({"ok": True, **job.as_dict()})


@routes.get("/api/admin/tools/resume/job")
async def _api_tools_resume_job(request: web.Request) -> web.Response:
    """Состояние задания сборки: фаза, проценты, а по готовности — отчёт, сверка и превью."""
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    job = _job(request.query.get("job", ""))
    if not job:
        return web.json_response({"ok": False, "error": "Задание не найдено — соберите заново"})
    if job.status == "failed":
        return web.json_response({"ok": False, "error": job.error, **job.as_dict()})
    return web.json_response({"ok": True, **job.as_dict(with_previews=job.status == "done")})


@routes.get("/api/admin/tools/resume/download")
async def _api_tools_resume_download(request: web.Request) -> web.StreamResponse:
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    job = _job(request.query.get("job", ""))
    if not job or not job.out or not job.out.is_file():
        return web.json_response({"ok": False, "error": "Файл допечатки не найден — соберите заново"},
                                 status=404)
    return web.FileResponse(job.out, headers={
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{job.out.name}"'})


def _paths_cached(src: Path) -> bytes:
    """Бинарные траектории с кэшем на диске: ключ — путь+mtime+размер файла."""
    st = src.stat()
    key = hashlib.sha1(f"{src}|{int(st.st_mtime)}|{st.st_size}|{resumecut.TOOLPATH_VERSION}".encode()).hexdigest()
    cache = tool_dir() / "cache" / f"{key}.bin"
    if cache.is_file():
        return cache.read_bytes()
    data = resumecut.toolpaths(src)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    except OSError:
        pass
    return data


@routes.get("/api/admin/tools/resume/paths")
async def _api_tools_resume_paths(request: web.Request) -> web.StreamResponse:
    """Траектории для 3D: `path=` — исходник, `job=` — собранный файл (оба внутри корней)."""
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    src: Path | None
    if request.query.get("job"):
        job = _job(request.query.get("job", ""))
        src = job.out if job and job.out and job.out.is_file() else None
    else:
        src = _source_path(request.query.get("path"))
    if src is None:
        return web.json_response({"ok": False, "error": "Файл не найден"}, status=404)
    try:
        data = await asyncio.to_thread(_paths_cached, src)
    except resumecut.ResumeUnavailable as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.warning("допечатка со слоя: траектории %s не собрать", src, exc_info=True)
        return web.json_response({"ok": False, "error": f"Траектории не собрать: {exc}"})
    return web.Response(body=data, content_type="application/octet-stream",
                        headers={"Cache-Control": "private, max-age=600"})


async def _job_from_body(request: web.Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None, None, None, None, _bad()
    uid, err = webauth._admin_body(body)
    if err:
        return None, None, None, None, err
    job = _job(body.get("job", ""))
    if not job or job.status != "done" or not job.out or not job.out.is_file():
        return None, None, None, None, web.json_response(
            {"ok": False, "error": "Файл допечатки не найден — соберите заново"})
    pid = _toint(body.get("printer_id")) or None
    return uid, job, pid, body, None


async def _gates(job: ResumeJob, drv, pid: int | None, *, force_centre: bool = False,
                 after_upload: bool = False) -> list[dict]:
    """Список препятствий старту. Пустой список = можно. Каждое: {gate, text, hard, hint}."""
    gates: list[dict] = []
    rep = job.report or {}
    chk = rep.get("start_check") or {}
    if chk and not chk.get("ok", True):
        c = (chk.get("collisions") or [{}])[0]
        gates.append({"gate": "start_path", "hard": True,
                      "text": f"Стартовый переезд проходит над деталью «{c.get('name', '?')}» на высоте "
                              f"{c.get('z')} мм — ниже её макушки"})
    if rep.get("centre_object") and float(rep.get("parts_height") or 0) > home_clearance_mm() and not force_centre:
        gates.append({"gate": "centre", "hard": False,
                      "text": f"Деталь «{rep['centre_object']}» стоит на точке хоминга Z, а детали выше "
                              f"зазора концевика ({home_clearance_mm():g} мм)",
                      "hint": "Снимите её (и вырежьте из файла) или подтвердите старт на свой риск"})
    if drv is None or not drv.enabled():
        gates.append({"gate": "printer", "hard": True, "text": "Принтер не подключён"})
        return gates
    try:
        await reprint.ensure_idle(drv)
    except reprint.ReprintUnavailable as exc:
        gates.append({"gate": "busy", "hard": True, "text": str(exc)})
    if drv.caps.status:
        try:
            st = await asyncio.to_thread(drv.status, 30.0)
        except Exception:  # noqa: BLE001
            st = None
        if st and st.get("ok") and st.get("state"):
            try:
                summ = elegoo.summarize(st["state"]) if getattr(drv, "kind", "") in ("elegoo", "", "fake") else {}
            except Exception:  # noqa: BLE001
                summ = {}
            bed = summ.get("bed_temp")
            want = float(rep.get("bed") or 0)
            if isinstance(bed, (int, float)) and want > 0 and float(bed) < want - 8:
                gates.append({"gate": "bed_cold", "hard": False,
                              "text": f"Стол {float(bed):.0f}° при нужных {want:.0f}° — остывший PLA "
                                      "отскакивает от текстурированной плиты",
                              "hint": f"Включите стол на {want:.0f}° и дождитесь прогрева",
                              "bed_target": want})
    if after_upload and job.uploaded and drv.caps.files:
        try:
            r = await asyncio.to_thread(drv.files)
            names = {elegoo.normalize_job_name(str(f.get("name") or f.get("filename") or ""))
                     for f in (r or {}).get("files") or [] if isinstance(f, dict)}
            if names and elegoo.normalize_job_name(job.uploaded) not in names:
                gates.append({"gate": "file", "hard": True,
                              "text": f"Файл «{job.uploaded}» не виден в списке принтера — залейте заново"})
        except Exception:  # noqa: BLE001
            log.debug("допечатка со слоя: список файлов принтера не прочитать", exc_info=True)
    return gates


@routes.get("/api/admin/tools/resume/gates")
async def _api_tools_resume_gates(request: web.Request) -> web.Response:
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    job = _job(request.query.get("job", ""))
    if not job or job.status != "done":
        return web.json_response({"ok": False, "error": "Файл допечатки не найден — соберите заново"})
    pid = _toint(request.query.get("printer_id")) or None
    drv = await _printer_driver(pid)
    gates = await _gates(job, drv, pid, force_centre=request.query.get("force_centre") == "1",
                         after_upload=bool(job.uploaded))
    return web.json_response({"ok": True, "gates": gates, "can_start": not any(g["hard"] for g in gates)
                              and not gates, "uploaded": job.uploaded})


@routes.post("/api/admin/tools/resume/send")
async def _api_tools_resume_send(request: web.Request) -> web.Response:
    """Залить собранный файл на принтер. БЕЗ старта — стол сначала чистит мастер."""
    uid, job, pid, _body, err = await _job_from_body(request)
    if err:
        return err
    drv = await _printer_driver(pid)
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.upload:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) не принимает задания"})
    exts = {str(e).lower() for e in (getattr(drv, "upload_exts", ()) or ())}
    if exts and ".gcode" not in exts:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) принимает {', '.join(sorted(exts))}, а не .gcode"})
    try:
        await reprint.ensure_idle(drv)
    except reprint.ReprintUnavailable as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    name = job.out.name
    try:
        up = await drv.upload(job.out, name=name)
    except Exception as exc:  # noqa: BLE001
        log.warning("допечатка со слоя: заливка %s не удалась", name, exc_info=True)
        return web.json_response({"ok": False, "error": f"Заливка не удалась: {exc}"})
    if not (up or {}).get("ok"):
        return web.json_response({"ok": False, "error": (up or {}).get("error") or "Принтер не принял файл"})
    try:
        await _remember_gcode_settings(name, await asyncio.to_thread(job.out.read_bytes))
    except Exception:  # noqa: BLE001
        log.debug("допечатка со слоя: не запомнить настройки %s", name, exc_info=True)
    try:
        await asyncio.to_thread(jobarchive.register, job.out, f"{job.source_name} · допечатка L{job.layer}",
                                source="resume")
    except Exception:  # noqa: BLE001
        log.debug("допечатка со слоя: не положить в архив", exc_info=True)
    job.uploaded = name
    await db.log_event(uid, "допечатка со слоя", f"«{name}» залит на принтер (ждёт старта у станка)")
    gates = await _gates(job, drv, pid, after_upload=True)
    return web.json_response({"ok": True, **job.as_dict(), "needs_start": True, "gates": gates})


@routes.post("/api/admin/tools/resume/start")
async def _api_tools_resume_start(request: web.Request) -> web.Response:
    """Старт залитого файла — отдельное подтверждение мастера у станка, после гейтов."""
    uid, job, pid, body, err = await _job_from_body(request)
    if err:
        return err
    if not job.uploaded:
        return web.json_response({"ok": False, "error": "Файл ещё не залит на принтер"})
    drv = await _printer_driver(pid)
    if not drv.enabled():
        return _no_printer(drv)
    if not drv.caps.start:
        return web.json_response({"ok": False, "error": f"Принтер ({drv.title}) не умеет запускать печать по команде"})
    force = bool(body.get("force_centre"))
    gates = await _gates(job, drv, pid, force_centre=force, after_upload=True)
    blocking = [g for g in gates if g["hard"] or not bool(body.get("ack_soft"))]
    if blocking:
        return web.json_response({"ok": False, "error": "; ".join(g["text"] for g in blocking),
                                  "gates": gates})
    name = job.uploaded
    res = await asyncio.to_thread(lambda: drv.start(name))
    if not (res or {}).get("ok"):
        return web.json_response({"ok": False, "error": (res or {}).get("error") or "Не удалось запустить"})
    try:
        await db.save_print_settings(name, started=True, norm_name=elegoo.normalize_job_name(name),
                                     printer_id=pid)
    except Exception:  # noqa: BLE001
        log.debug("допечатка со слоя: не отметить старт %s", name, exc_info=True)
    rebound = None
    try:
        # Печать продолжает ТУ ЖЕ работу: если исходная печать была привязана к заказу — переставляем
        # привязку на новый файл, чтобы printwatch дописал журнал и уведомил клиента о финише.
        b = await db.get_print_binding(pid)
        if b and b.get("request_id"):
            await db.set_print_binding(int(b["request_id"]), name, pid)
            rebound = int(b["request_id"])
    except Exception:  # noqa: BLE001
        log.debug("допечатка со слоя: привязку не переставить", exc_info=True)
    await db.log_event(uid, "допечатка со слоя",
                       f"старт «{name}»: со слоя {job.layer}, {job.report.get('kept_objects')} дет., "
                       f"~{job.report.get('minutes_remaining')} мин"
                       + (f", заказ #{rebound}" if rebound else ""))
    return web.json_response({"ok": True, **job.as_dict(), "started": True, "request_id": rebound})


HISTORY_ACTION = "допечатка со слоя"


@routes.get("/api/admin/tools/resume/history")
async def _api_tools_resume_history(request: web.Request) -> web.Response:
    """История допечаток — из ленты активности (сборка, заливка, старт), новые сверху."""
    if not webauth._verify_admin(request.query.get("initData", "")):
        return _admin_forbidden()
    try:
        rows = await db.list_activity(400)
    except Exception:  # noqa: BLE001
        log.debug("допечатка со слоя: ленту не прочитать", exc_info=True)
        rows = []
    items = [{"ts": int(r.get("at") or 0), "user_name": r.get("name") or "",
              "detail": r.get("detail") or ""} for r in rows if r.get("action") == HISTORY_ACTION][:50]
    return web.json_response({"ok": True, "items": items})
