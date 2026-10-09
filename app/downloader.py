"""Получение файла модели: приём загруженного пользователем файла (.stl/.3mf/.obj/.zip/.step)
либо загрузка по ссылке — Printables, Thingiverse, прямая ссылка на файл.

MakerWorld закрывает и страницу, и API за авторизацией + анти-бот (Cloudflare): анонимно
скачать нельзя (API отдаёт 403). Ссылку MakerWorld мы всегда распознаём и вытаскиваем из неё
человекочитаемое имя, а само автоскачивание живёт в отдельном модуле app/makerworld.py, которого
нет в открытой версии проекта: тогда ссылка честно отказывает просьбой прислать файл.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import config, netguard, optional, stepconv

log = logging.getLogger(__name__)

MESH_EXTS = {".stl", ".3mf", ".obj"}

# Инженерные (CAD) форматы: файл валиден, но это НЕ меш — trimesh их не читает (нужен
# OpenCASCADE/cascadio, его в окружении нет), конвертера STEP→STL пока нет. До 29.07.2026
# .step/.stp лежали в MESH_EXTS — загрузчик их принимал, а дальше всё молча деградировало:
# geometry.load_print_meshes → [] (лимит габаритов inf, geometry.py:652), превью None, 3D
# недоступно, и только в САМОМ конце мастера (после выбора пластика/цвета/тиража) расчёт падал
# «Не удалось оценить модель (нечитаемая геометрия)» с предложением оформить заказ БЕЗ сметы.
# Держим их отдельным списком, чтобы отказывать сразу и по делу, а не общим «неподдерживаемый
# формат»: STEP — законный 3D-формат, клиент вправе не понимать, почему его не берут.
# Появится конвертер (B6 в docs/Перспективные разработки/competitor-feature-gap-2026-07.md) —
# формат переедет обратно в MESH_EXTS вместе с обработкой в geometry.
# B6 СДЕЛАН 21.08.2026: конвертер STEP→STL (app/stepconv.py, FreeCAD headless). CAD_EXTS
# остаются ОТДЕЛЬНЫМ множеством: .step живёт в конвейере только до конвертации, дальше
# везде ходит полученный .stl. Без конвертера (нет freecadcmd) поведение прежнее — честный
# отказ CAD_MSG, а не молчаливая деградация.
CAD_EXTS = {".step", ".stp"}
CAD_MSG = ("Инженерные форматы (.step/.stp) мы пока не считаем — их геометрию нечем прочитать. "
           "Пришлите .stl, .3mf или .obj: в любом CAD это «Экспорт → STL».")
_MW_RE = re.compile(r"^https?://(www\.)?makerworld\.com/", re.IGNORECASE)
_ID_RE = re.compile(r"makerworld\.com/(?:[a-z]{2}/)?models/(\d+)", re.IGNORECASE)
_SLUG_RE = re.compile(r"makerworld\.com/(?:[a-z]{2}/)?models/\d+[-/]([^/?#]+)", re.IGNORECASE)
_FILE_LINK_RE = re.compile(r'https?://[^\s"\'<>]+?\.(?:3mf|stl)', re.IGNORECASE)


class DownloadError(Exception):
    pass


class ModelFileError(DownloadError):
    """Файл модели непригоден сам по себе: формат не тот, архив битый или пустой, файл больше
    лимита Telegram. В отличие от сетевых DownloadError повтор тут не поможет — очередь
    досчёта (app/deferred.py) по такой ошибке сразу отдаёт заказ мастеру, а не мучает ПК
    шестью попытками. Подкласс DownloadError: все прежние `except DownloadError` его ловят."""


class UnsafeLinkError(ModelFileError):
    """Ссылка (или редирект по ней) ведёт во внутреннюю сеть — защита от SSRF, app/netguard.py.
    Подкласс ModelFileError ради семантики «повтор не поможет»: очередь досчёта не будет шесть
    раз стучаться на тот же внутренний адрес, а клиент получит честный текст причины."""


# Telegram отдаёт ботам через getFile файлы только до 20 МБ (лимит Bot API; свой сервер Bot API
# не используем). Больше — скачивание падает, причём уже после того, как клиент дождался
# «принято». Проверяем document.file_size ДО скачивания и говорим честно, что делать.
TG_FILE_LIMIT = 20 * 1024 * 1024
TG_TOO_BIG_MSG = ("Telegram не отдаёт ботам файлы больше 20 МБ — пришлите ссылку на модель "
                  "или загрузите файл через приложение.")


class MakerWorldBlocked(DownloadError):
    """MakerWorld отдал анти-бот «капчу» (HTTP 418) на эндпоинте скачивания. Это НЕ проблема
    токена — блок временный и бьёт и по JWT, и по браузеру (один и тот же эндпоинт), поэтому
    запасной браузерный путь дёргать бесполезно."""




def is_makerworld(url: str) -> bool:
    return bool(_MW_RE.match(url.strip()))


def parse_design_id(url: str) -> str | None:
    m = _ID_RE.search(url)
    return m.group(1) if m else None


# «#profileId-N» (якорь страницы, клиент шлёт строку ссылки целиком) или «?profileId=N».
_PROFILE_ID_RE = re.compile(r"profileId[-=](\d+)", re.IGNORECASE)


def parse_profile_id(url: str) -> int | None:
    """Явно выбранный ВАРИАНТ (профиль печати) из самой ссылки MakerWorld. Пользователь уже
    выбрал профиль на сайте — копия ссылки несёт его в якоре. None — в ссылке варианта нет.
    Зачем: с явным iid модель берётся из кэша (design id, instance id) ВООБЩЕ без похода в
    api.bambulab.com — «модель из кэша грузилась минуту», потому что путь загрузки сперва
    ждал мёртвый variants-API (522), хотя выбор варианта уже был сделан в ссылке (24.07)."""
    if not is_makerworld(url):
        return None
    m = _PROFILE_ID_RE.search(url)
    return int(m.group(1)) if m else None


def label_from_url(url: str) -> str:
    """Человекочитаемое имя из slug ссылки."""
    m = _SLUG_RE.search(url)
    if m:
        slug = m.group(1).replace("-", " ").replace("_", " ").strip()
        if slug:
            return slug[:80].title()
    did = parse_design_id(url)
    return f"MakerWorld {did}" if did else "Модель MakerWorld"


def _job_dir() -> Path:
    d = config.DOWNLOAD_DIR / f"dl_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reject_cad(ext: str) -> None:
    """Инженерный формат БЕЗ конвертера → честный отказ ПРЯМО НА ПРИЁМЕ (см. CAD_EXTS).
    С конвертером (B6, app/stepconv.py) CAD проходит дальше и мешится в STL по месту.
    Не-CAD не трогаем: общий «неподдерживаемый формат» вызывающий отдаёт сам."""
    if ext in CAD_EXTS and not stepconv.available():
        raise ModelFileError(CAD_MSG)


# Имена устройств Windows: файл «CON.stl» не создать, «NUL.3mf» молча уходит в никуда.
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


def safe_upload_name(name: str | None, fallback: str = "model") -> str:
    """Имя файла от клиента → безопасное имя для записи в каталог задания.

    Имя документа Telegram (и имя из события приёмной) — чужие данные: в нём бывают
    разделители путей («../../x.stl», «C:» с обратными косыми), управляющие символы и имена устройств
    Windows. Берём только последний компонент (оба вида разделителей), выкидываем
    управляющие и запрещённые Windows символы, точки и пробелы по краям, режем длину,
    СОХРАНЯЯ расширение — по нему дальше решается, что это за файл."""
    raw = str(name or "")
    base = re.split(r"[/\x5c]", raw)[-1]          # \x5c — обратная косая (Windows-разделитель)
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f")
    base = re.sub(r'[/\x5c:*?"<>|]+', "_", base).strip()
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = re.sub(r"[^0-9A-Za-z]", "", ext)[:10]
    stem = stem.strip().strip(".")[:60] or (fallback or "model")
    if stem.split(".")[0].casefold() in _WIN_RESERVED:
        stem = "_" + stem
    return f"{stem}.{ext}" if ext else stem


def _convert_cad_file(path: Path) -> Path:
    """STEP→STL по месту загрузки. Блокирует поток — звать из to_thread. Отказ конвертера
    переводим на язык клиента: он прислал валидный файл, виноват не он."""
    try:
        return stepconv.convert_file(path)
    except stepconv.ConvertError as e:
        log.warning("STEP не сконвертировался (%s): %s", path.name, e)
        raise DownloadError("Не получилось разобрать STEP-файл. Пришлите, пожалуйста, "
                            ".stl или .3mf — в любом CAD это «Экспорт → STL».") from e


def _collect_meshes(root: Path) -> list[Path]:
    # ._* и __MACOSX — AppleDouble-мусор из архивов, созданных на macOS: расширение как у
    # модели, внутри — метаданные Finder; слайсер на них падает (фидбэк владельца 2026-07-11)
    found = [p for p in sorted(root.rglob("*"))
             if p.suffix.lower() in MESH_EXTS
             and not p.name.startswith("._") and "__MACOSX" not in p.parts]
    return found[: config.MAX_MESH_FILES]


def _extract_zip(zip_path: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            # защита от zip-бомбы: суммарный распакованный объём ограничиваем
            total = sum(max(0, i.file_size) for i in z.infolist())
            if total > config.MAX_DOWNLOAD_BYTES * 3:
                raise ModelFileError("Архив распаковывается в слишком большой объём.")
            z.extractall(dest)   # Python санирует пути (.. и абсолютные) сам
    except zipfile.BadZipFile:
        raise ModelFileError("Не удалось распаковать архив (повреждён или не .zip).")
    meshes = _collect_meshes(dest)
    # CAD в архиве: с конвертером мешим в STL и кладём в общий набор (тот же фильтр
    # AppleDouble-мусора, что у _collect_meshes); без конвертера поведение прежнее
    cads = [p for p in sorted(dest.rglob("*"))
            if p.suffix.lower() in CAD_EXTS
            and not p.name.startswith("._") and "__MACOSX" not in p.parts]
    if cads and stepconv.available():
        meshes.extend(_convert_cad_file(c) for c in cads[: config.MAX_MESH_FILES])
        meshes = meshes[: config.MAX_MESH_FILES]
    if not meshes:
        # Архив из одних CAD-файлов — не «моделей нет», а «формат не тот»: говорим это прямо,
        # иначе клиент ищет несуществующую ошибку в своём архиве.
        if cads:
            raise ModelFileError(CAD_MSG)
        raise ModelFileError("В архиве не найдено моделей (.stl/.3mf/.obj).")
    return meshes


async def save_web_upload(filename: str, data: bytes) -> tuple[list[Path], str]:
    """Приём файла, загруженного из мини-апки (multipart). Возвращает (меши, имя)."""
    if len(data) > config.MAX_DOWNLOAD_BYTES:
        raise DownloadError("Файл слишком большой.")
    fn = Path(filename or "model")
    ext = fn.suffix.lower()
    _reject_cad(ext)          # до записи на диск: CAD видно по имени
    job = _job_dir()
    safe = (re.sub(r'[\\/:*?"<>|]+', "_", fn.stem).strip() or "model")[:60]
    name = safe + ext
    target = job / name
    target.write_bytes(data)
    if ext == ".zip":
        return await asyncio.to_thread(_extract_zip, target, job / "unzipped"), name
    if ext in CAD_EXTS:      # сюда попадаем только с конвертером: _reject_cad выше
        return [await asyncio.to_thread(_convert_cad_file, target)], name
    if ext in MESH_EXTS:
        return [target], name
    raise DownloadError("Неподдерживаемый формат. Пришлите .stl, .3mf, .obj или .zip.")


async def download_tg_file(bot, file, *, file_name: str | None, file_size: int | None,
                           unique_id: str | None = None) -> tuple[list[Path], str]:
    """Скачать файл модели из Telegram (объект документа ИЛИ его file_id) и разобрать.

    Общий путь для документа, присланного в чат прямо сейчас (handle_upload), и для файла,
    принятого приёмной, пока ПК был выключен (очередь досчёта знает только file_id, имя и
    размер). Все отказы, которые можно дать без скачивания, — ДО скачивания: формат и лимит
    Telegram в 20 МБ."""
    name = safe_upload_name(file_name, fallback=f"model_{unique_id or uuid.uuid4().hex[:8]}")
    # Бот принимает ЛЮБОЙ документ (клиентского фильтра, как у мини-апки, тут нет) — гейт формата
    # ставим до скачивания из Telegram: незачем тянуть файл, который всё равно нечем прочитать.
    _reject_cad(Path(name).suffix.lower())
    if file_size is not None and int(file_size or 0) > TG_FILE_LIMIT:
        raise ModelFileError(TG_TOO_BIG_MSG)
    job = _job_dir()
    target = job / name
    await bot.download(file, destination=str(target))

    ext = target.suffix.lower()
    if ext == ".zip":
        return await asyncio.to_thread(_extract_zip, target, job / "unzipped"), name
    if ext in CAD_EXTS:      # сюда попадаем только с конвертером: _reject_cad выше
        return [await asyncio.to_thread(_convert_cad_file, target)], name
    if ext in MESH_EXTS:
        return [target], name
    raise ModelFileError("Неподдерживаемый формат. Пришлите .stl, .3mf, .obj или .zip.")


async def handle_upload(bot, document) -> tuple[list[Path], str]:
    """Скачивает загруженный в Telegram файл и возвращает (список mesh, подпись)."""
    return await download_tg_file(bot, document, file_name=document.file_name,
                                  file_size=getattr(document, "file_size", None),
                                  unique_id=getattr(document, "file_unique_id", None))


# ===== дружелюбные источники: прямые ссылки и Thingiverse =====
_THING_RE = re.compile(r"thingiverse\.com/thing:(\d+)", re.IGNORECASE)
# step|stp здесь ОСТАЮТСЯ намеренно, хотя мы их не считаем: ссылка распознаётся как файловая и
# уходит в _download_direct, а тот отвечает понятным CAD_MSG. Убрать их — и клиент получил бы
# «ссылка не поддерживается» на совершенно валидную прямую ссылку.
_FILE_URL_RE = re.compile(r"\.(stl|3mf|obj|step|stp|zip)(?:[?#]|$)", re.IGNORECASE)


def is_http_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def is_thingiverse(url: str) -> bool:
    return bool(_THING_RE.search(url))


def _looks_like_file_url(url: str) -> bool:
    return bool(_FILE_URL_RE.search(url))


@contextlib.asynccontextmanager
async def _public_client(**kwargs):
    """httpx-клиент для ссылок от клиента и для файлов, найденных по ним: только публичные
    адреса, проверка на каждом шаге редиректа (app/netguard.py). Отказы защиты переводим в
    наши DownloadError — их тексты вызывающие показывают клиенту как есть."""
    try:
        async with netguard.public_client(**kwargs) as client:
            yield client
    except netguard.BlockedAddress as exc:
        log.warning("SSRF: ссылка клиента ведёт во внутреннюю сеть — отказ (%s)", exc.detail)
        raise UnsafeLinkError(str(exc)) from exc
    except netguard.ResolveError as exc:
        raise DownloadError(str(exc)) from exc
    except httpx.TooManyRedirects as exc:
        raise DownloadError("Ссылка перенаправляет слишком много раз — пришлите прямую ссылку "
                            "на файл или сам файл.") from exc


async def _stream_to_file(client, url: str, target: Path) -> None:
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        total = 0
        with open(target, "wb") as f:
            async for chunk in resp.aiter_bytes(1 << 16):
                total += len(chunk)
                if total > config.MAX_DOWNLOAD_BYTES:
                    raise DownloadError("Файл слишком большой.")
                f.write(chunk)


async def _download_direct(url: str) -> tuple[list[Path], str]:
    fname = url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1] or "model"
    # CAD виден прямо по ссылке — отказываем ДО закачки. Это ещё и обязательная страховка:
    # ниже незнакомому суффиксу дописывается «.stl», и «part.step» уехал бы дальше как
    # «part.step.stl» — то есть ровно та ловушка, которую мы закрываем.
    _reject_cad(Path(fname).suffix.lower())
    job = _job_dir()
    if Path(fname).suffix.lower() not in (MESH_EXTS | CAD_EXTS | {".zip"}):
        fname += ".stl"
    target = job / fname
    try:
        # адрес целиком от клиента: без _public_client это был SSRF (принтер в LAN, свой :8081)
        async with _public_client(headers={"User-Agent": "Mozilla/5.0"}, timeout=120) as client:
            await _stream_to_file(client, url, target)
    except httpx.HTTPError as exc:
        raise DownloadError(f"Не удалось скачать по ссылке: {exc}") from exc
    ext = target.suffix.lower()
    if ext == ".zip":
        return await asyncio.to_thread(_extract_zip, target, job / "unzipped"), fname
    if ext in CAD_EXTS:      # сюда попадаем только с конвертером: _reject_cad выше
        return [await asyncio.to_thread(_convert_cad_file, target)], fname
    if ext in MESH_EXTS:
        return [target], fname
    raise DownloadError("По ссылке не похоже на модель (.stl/.3mf/.obj/.zip).")


async def _download_thingiverse(url: str) -> tuple[list[Path], str]:
    tid = _THING_RE.search(url).group(1)
    if not config.THINGIVERSE_TOKEN:
        raise DownloadError("Для Thingiverse нужен THINGIVERSE_TOKEN в .env "
                            "(бесплатно: thingiverse.com/developers).")
    headers = {"Authorization": f"Bearer {config.THINGIVERSE_TOKEN}", "User-Agent": "Mozilla/5.0"}
    job = _job_dir()
    try:
        # API у Thingiverse свой, но download_url и редиректы по нему — уже куда угодно
        async with _public_client(headers=headers, timeout=120) as client:
            tr = await client.get(f"https://api.thingiverse.com/things/{tid}")
            tr.raise_for_status()
            label = tr.json().get("name") or f"Thingiverse {tid}"
            fr = await client.get(f"https://api.thingiverse.com/things/{tid}/files")
            fr.raise_for_status()
            meshes: list[Path] = []
            saw_cad = False
            for fobj in fr.json()[: config.MAX_MESH_FILES]:
                fn = fobj.get("name", "file")
                ext = Path(fn).suffix.lower()
                if ext in CAD_EXTS and stepconv.available():
                    pass                              # скачаем и сконвертируем ниже
                elif ext not in (MESH_EXTS | {".zip"}):
                    saw_cad = saw_cad or ext in CAD_EXTS
                    continue
                dl = fobj.get("download_url") or fobj.get("public_url")
                if not dl:
                    continue
                target = job / fn
                await _stream_to_file(client, dl, target)
                if ext == ".zip":
                    meshes.extend(await asyncio.to_thread(
                        _extract_zip, target, job / f"z_{target.stem}"))
                elif ext in CAD_EXTS:
                    meshes.append(await asyncio.to_thread(_convert_cad_file, target))
                else:
                    meshes.append(target)
            if not meshes:
                # у автора выложен только CAD — причина именно в формате, а не в пустой модели
                raise DownloadError(CAD_MSG if saw_cad else "У модели нет файлов .stl/.3mf/.obj.")
            return meshes, label
    except httpx.HTTPError as exc:
        raise DownloadError(f"Ошибка Thingiverse: {exc}") from exc


# --- Printables: открытый GraphQL API (без логина и токена для бесплатных моделей) ---
_PR_API = "https://api.printables.com/graphql/"
_PR_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_PRINTABLES_RE = re.compile(r"printables\.com/(?:[a-z]{2}/)?model/(\d+)", re.IGNORECASE)
_PR_SLUG_RE = re.compile(r"printables\.com/(?:[a-z]{2}/)?model/\d+-([^/?#]+)", re.IGNORECASE)
_PR_FILES_Q = "query ModelFiles($id: ID!){ model: print(id:$id){ id stls{ id name fileSize } } }"
_PR_DL_Q = ("mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!, "
            "$source: DownloadSourceEnum!){ getDownloadLink(id:$id, printId:$modelId, "
            "fileType:$fileType, source:$source){ ok output { link } } }")


def is_printables(url: str) -> bool:
    return bool(_PRINTABLES_RE.search(url))


def _printables_label(url: str, mid: str) -> str:
    m = _PR_SLUG_RE.search(url)
    if m:
        s = m.group(1).replace("-", " ").replace("_", " ").strip()
        if s:
            return s[:80].title()
    return f"Printables {mid}"


async def _download_printables(url: str) -> tuple[list[Path], str]:
    mid = _PRINTABLES_RE.search(url).group(1)
    job = _job_dir()
    try:
        # как и у Thingiverse: ссылка на файл приходит из ответа API, а за ней редиректы
        async with _public_client(headers={"User-Agent": _PR_UA}, timeout=60) as client:
            r = await client.post(_PR_API, json={"operationName": "ModelFiles",
                                                 "query": _PR_FILES_Q, "variables": {"id": mid}})
            r.raise_for_status()
            model = (r.json().get("data") or {}).get("model")
            if not model:
                raise DownloadError("Модель Printables не найдена.")
            stls = model.get("stls") or []
            if not stls:
                raise DownloadError("У модели Printables нет STL-файлов (возможно, только .3mf).")
            meshes: list[Path] = []
            for f in stls[: config.MAX_MESH_FILES]:
                fid = f.get("id")
                fname = f.get("name") or f"{fid}.stl"
                if not fname.lower().endswith(".stl"):
                    fname += ".stl"
                mut = await client.post(_PR_API, json={
                    "operationName": "GetDownloadLink", "query": _PR_DL_Q,
                    "variables": {"id": fid, "modelId": mid, "fileType": "stl", "source": "model_detail"}})
                mut.raise_for_status()
                out = (((mut.json().get("data") or {}).get("getDownloadLink") or {}).get("output") or {})
                link = out.get("link")
                if not link:
                    continue
                target = job / fname
                await _stream_to_file(client, link, target)
                meshes.append(target)
            if not meshes:
                raise DownloadError("Не удалось получить ссылки на файлы Printables.")
            return meshes, _printables_label(url, mid)
    except httpx.HTTPError as exc:
        raise DownloadError(f"Ошибка Printables: {exc}") from exc


# ===== кэш загрузок по ссылке: один и тот же ФАЙЛ повторно не качаем =====
# Ключ обязан совпадать у всех путей, которые качают физически одно и то же. Раньше ключом была
# сырая строка ссылки, и fetch_from_url(url) + download_makerworld_instance(url, iid) для
# дефолтного инстанса давали ДВЕ записи на один файл (замер на проде 20.07.2026: 11 дублей,
# ~202 МБ) — то есть лишнюю закачку, а лишние закачки и приводят к анти-боту 418. Поэтому ключ
# строим из смысла ссылки: MakerWorld — (design id, instance id), остальные — нормализованный URL.

# Трекинговые параметры ленты/поиска MakerWorld (?from=recommend, ?from=search) и обычная UTM-метка:
# на сам файл не влияют, но раньше плодили отдельные записи кэша для одной и той же модели.
_TRACK_PARAM_RE = re.compile(r"^(from|ref|referrer|spm|utm_[a-z_]+)$", re.IGNORECASE)


def _url_key(s: str) -> str:
    return hashlib.sha1(s.strip().encode("utf-8")).hexdigest()


def _norm_url(url: str) -> str:
    """Ссылка без фрагмента и трекинговых параметров — ТОЛЬКО для ключа кэша (качаем всегда по
    исходной ссылке). Фрагмент режем всегда: HTTP его не передаёт, а MakerWorld-овский
    «#profileId-N» — якорь страницы, выбор варианта идёт отдельным аргументом iid."""
    try:
        sp = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = urlencode([(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
                   if not _TRACK_PARAM_RE.match(k)])
    return urlunsplit((sp.scheme.lower(), sp.netloc.lower(), sp.path.rstrip("/"), q, ""))


def _cache_key(url: str, iid=None) -> str:
    """Ключ записи кэша. MakerWorld — по (design id, instance id): один и тот же вариант, пришедший
    и из fetch_from_url, и из download_makerworld_instance, попадает в ОДНУ запись, а разные
    варианты модели остаются разными записями. iid неизвестен (нет токена/сбой) → ключ по design id."""
    did = parse_design_id(url) if is_makerworld(url) else None
    if did:
        return _url_key(f"mw:{did}:{iid}" if iid else f"mw:{did}")
    return _url_key(_norm_url(url))


def _cache_get(key: str, _alias_hop: bool = False) -> tuple[list[Path], str] | None:
    """Запись кэша (файлы на месте) → (меши, подпись). DOWNLOAD_CACHE_AGE_DAYS=0 — бессрочно
    («кэшировать всё, что хоть раз скачал», OpenTodo bdcfcda4); >0 — прежний срок в днях."""
    d = config.DOWNLOAD_CACHE_DIR / key
    meta = d / "meta.json"
    if not meta.exists():
        return None
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
        if m.get("alias"):                    # указатель на настоящую запись (см. _cache_alias)
            return None if _alias_hop else _cache_get(str(m["alias"]), True)
        if (config.DOWNLOAD_CACHE_AGE_DAYS > 0
                and time.time() - float(m.get("created_at", 0)) > config.DOWNLOAD_CACHE_AGE_DAYS * 86400):
            return None
        paths = [d / n for n in m.get("files", [])]
        if not paths or not all(p.exists() for p in paths):
            return None
        return paths, m.get("label", "")
    except Exception:  # noqa: BLE001
        return None


def _cache_alias(key: str, target: str) -> None:
    """Пометка «по этому ключу лежит запись target» — крошечный meta.json без файлов. Нужна, когда
    инстанс определить не удалось (кулдаун 418, нет токена): ключ деградирует до design id, и без
    пометки уже скачанная модель не нашлась бы в кэше именно тогда, когда качать нельзя."""
    if key == target or _cache_get(key):      # там уже настоящая запись — не трогаем
        return
    try:
        d = config.DOWNLOAD_CACHE_DIR / key
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"alias": target}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("download cache alias failed", exc_info=True)


def _cache_get_any(key: str, legacy_key: str) -> tuple[list[Path], str] | None:
    """Сперва нынешний ключ, затем ключ по сырой строке ссылки — так остаются доступны записи,
    сделанные до перехода на ключ по смыслу ссылки (20.07.2026). Переносить старые записи не
    нужно: лишних закачек они уже не дают, а разбор дублей — дело dedupe_download_cache.py."""
    return _cache_get(key) or (_cache_get(legacy_key) if legacy_key != key else None)


def _cache_put(key: str, url: str, paths: list[Path], label: str, iid=None) -> tuple[list[Path], str]:
    """Копирует скачанные файлы в стабильную папку cache/<key>/ и пишет meta.json (url, подпись,
    исходные имена). Кэш — best-effort: при любом сбое отдаём исходные файлы как есть."""
    try:
        d = config.DOWNLOAD_CACHE_DIR / key
        d.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        cached: list[Path] = []
        for p in paths:
            name = p.name
            if name in names:                     # коллизия имён из разных подпапок
                name = f"{p.stem}_{len(names)}{p.suffix}"
            shutil.copy2(p, d / name)
            names.append(name)
            cached.append(d / name)
        meta = {"url": url, "label": label, "files": names,
                "orig_names": [p.name for p in paths], "created_at": time.time()}
        if iid:                               # для разбора дублей: какой именно вариант MW лежит
            meta["iid"] = iid
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return cached, label
    except Exception:  # noqa: BLE001
        log.debug("download cache store failed", exc_info=True)
        return paths, label


async def fetch_from_url(url: str) -> tuple[list[Path], str]:
    """Единая точка входа + кэш (срок DOWNLOAD_CACHE_AGE_DAYS дней). Для MakerWorld заранее
    выясняем, какой инстанс тут скачается, — иначе тот же файл, запрошенный через
    download_makerworld_instance, лёг бы во вторую запись кэша и скачался повторно."""
    url = url.strip()
    pid = parse_profile_id(url)
    if pid is not None:
        # вариант выбран прямо в ссылке — качаем/берём из кэша ИМЕННО его; при попадании в кэш
        # (design id, instance id) это ноль запросов к API (лежащий MW ничего не тормозит)
        return await download_makerworld_instance(url, pid)
    iid = await _mw_default_iid(url)
    key = _cache_key(url, iid)
    hit = _cache_get_any(key, _url_key(url))
    if hit:
        log.info("download cache hit: %s", url)
        return hit
    paths, label = await _fetch_from_url_uncached(url)
    res = _cache_put(key, url, paths, label, iid=iid)
    if iid:                     # скачали дефолтный вариант: ключ «без инстанса» ведёт сюда же
        _cache_alias(_cache_key(url), key)
    return res


async def _fetch_from_url_uncached(url: str) -> tuple[list[Path], str]:
    """MakerWorld (браузер), Printables/Thingiverse (API), прямые ссылки."""
    url = url.strip()
    if is_makerworld(url):
        return await try_download_makerworld(url)
    if is_printables(url):
        return await _download_printables(url)
    if is_thingiverse(url):
        return await _download_thingiverse(url)
    if _looks_like_file_url(url):
        return await _download_direct(url)
    raise DownloadError("Ссылка не поддерживается. Пришлите файл, либо ссылку на MakerWorld / "
                        "Printables / Thingiverse / прямую ссылку на .stl/.3mf/.obj/.zip.")


# ============================== MakerWorld ==============================
# Ссылку MakerWorld распознаём здесь (выше), а скачивание, варианты, оценка и поиск живут в
# app/makerworld.py. В открытой версии проекта этого модуля нет: тогда обёртки ниже честно
# отказывают (ModelFileError — повтор не поможет, очередь досчёта сразу отдаёт заказ мастеру).
MW_UNAVAILABLE_MSG = ("Ссылки MakerWorld не скачиваются автоматически: сайт закрыт авторизацией и "
                      "защитой от ботов. Скачайте модель (.3mf или .stl) на MakerWorld и пришлите "
                      "файлом. Многие модели есть и на Printables — попробуйте поиск по названию.")


def makerworld_available() -> bool:
    """Есть ли в этой сборке автоскачивание с MakerWorld (модуль app/makerworld.py)."""
    return _mw is not None


async def try_download_makerworld(url: str) -> tuple[list[Path], str]:
    if _mw is None:
        raise ModelFileError(MW_UNAVAILABLE_MSG)
    return await _mw.try_download_makerworld(url)


async def fetch_makerworld_variants(url: str) -> dict | None:
    """Варианты модели MakerWorld для пикера; None — пикер не нужен (или модуля нет)."""
    if _mw is None:
        return None
    return await _mw.fetch_makerworld_variants(url)


async def fetch_makerworld_estimate(url: str) -> dict | None:
    """Вес и время из профиля MakerWorld для мгновенной цены; None — считаем обычным путём."""
    if _mw is None:
        return None
    return await _mw.fetch_makerworld_estimate(url)


async def download_makerworld_instance(url: str, iid) -> tuple[list[Path], str]:
    if _mw is None:
        raise ModelFileError(MW_UNAVAILABLE_MSG)
    return await _mw.download_makerworld_instance(url, iid)


async def _mw_default_iid(url: str) -> object | None:
    """Инстанс, который скачается по ссылке без явного варианта (для ключа кэша fetch_from_url)."""
    if _mw is None:
        return None
    return await _mw._mw_default_iid(url)


# В самом низу: app/makerworld.py импортирует этот модуль и берёт из него помощников.
_mw = optional.load("makerworld")
