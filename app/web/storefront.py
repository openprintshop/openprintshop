"""Клиентская витрина: страницы коллекции/каталога, картинки, thumb-прокси и API каталога.

Один предмет: то, что КЛИЕНТ мастерской видит и листает БЕЗ конвейера сметы — HTML-страницы
коллекции и каталога пластиков (старые web-view кнопки бота), их картинки, прокси внешних
превью (обход сетевых блокировок CDN у клиента), поиск моделей по площадкам и клиентские
API каталога: коллекция с разделами, избранное, рекомендации, лента F4, поиск по коллекции,
личные настройки.

Выделено из app/webapp.py волной 8 endgame-плана (после 1b: `_spool_photo_path` уже в
web/common). НЕ ВЗЯТЫ `_pick` и `_pick_plastic` — единственные во всём файле читатели
`_storage` (FSM-хранилища диспетчера), их дом решается волнами 12+; и весь блок «своих
моделей»/токенов (`_api_use`, `_api_mymodels`, `_make_token`) — он держится за
`_register_full` и `_models` (волны 14/15).

`_thumb_proxy`/`_api_thumb` — единственная пара на голом `_sign` (подписывается ЦЕЛЫЙ
внешний URL, у него нет ни префикса, ни ручки с {ident}); золотой тест
tests/test_web_common_facade.py знает их новый адрес поимённо.

Регистрация маршрутов осталась в make_app строками на фасадные имена. Тела перенесены
ДОСЛОВНО, без единой правки.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import web

from .. import config, db, downloader, modelsearch, notify, ratelimit, semsearch
from ..webauth import _verify_init_data, _verify_user
from ..websign import _file_ver, _sign
from .common import (
    _ago,
    _bad,
    _color_css,
    _img_headers,
    _maybe_webp,
    _served_img,
    _spool_photo_path,
    _toint,
)

log = logging.getLogger(__name__)

_bot = None   # ставит webapp.set_bot (веер _BOT_FAN, волна 12); отправка закрыта `if _bot is None`

_THUMB_MAX = 5 * 1024 * 1024   # потолок размера внешнего превью (байт)

# Чередование карточки Gridfinity-конфигуратора в ленте: uid → показана ли в прошлый раз.
# Живёт в памяти процесса — перезапуск бота просто начинает отсчёт заново. Сам
# конфигуратор уехал в app/web/gridfinity.py, но подмешивание карточки — логика ЛЕНТЫ.
_GF_FEED_SHOWN: dict[int, bool] = {}


def set_bot(bot):
    """Поставить бота (зовёт webapp.set_bot веером; возврат прежнего — для тестов)."""
    global _bot
    prev, _bot = _bot, bot
    return prev


def _thumb_proxy(url: str) -> str:
    """Внешний URL превью → наш подписанный прокси /api/thumb. У владельца сеть режет CDN
    площадок, поэтому внешние <img> в мини-аппе не грузятся (OpenTodo 27f25b91).

    Единственная пара ссылок, оставшаяся на голом `_sign` (вторая половина — `_api_thumb`):
    подписывается ЦЕЛЫЙ внешний URL, а не «префикс + id», поэтому ни sig(), ни signed_url()
    тут не про что — у этой ссылки нет ни префикса, ни своей ручки с {ident} в пути."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return url or ""
    return f"/api/thumb?u={urllib.parse.quote(url, safe='')}&s={_sign(url)}"


_PAGE_HEAD = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Коллекция моделей</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
/* Print Studio — workshop dark theme for standalone Collection/Catalog pages */
@import url('https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
:root{
  color-scheme: dark;
  --tg-theme-bg-color:#0e1217 !important;
  --tg-theme-secondary-bg-color:#161d26 !important;
  --tg-theme-text-color:#eef2f7 !important;
  --tg-theme-button-color:#ff7a1a !important;
  --tg-theme-button-text-color:#1a0d02 !important;

  --bg:#0e1217; --bg-2:#0a0e12; --surface:#161d26; --surface-2:#1e2733;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.12);
  --text:#eef2f7; --muted:#95a3b2; --faint:#5f6e7d; --accent:#ff7a1a;
  --accent-soft:color-mix(in srgb, var(--accent) 15%, transparent);
  --accent-line:color-mix(in srgb, var(--accent) 42%, transparent);
  --ui:'Onest',-apple-system,Roboto,"Segoe UI",sans-serif;
  --mono:'JetBrains Mono',ui-monospace,monospace;
}
*{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body{ margin:0; font-family:var(--ui); background:var(--bg); color:var(--text);
  -webkit-font-smoothing:antialiased; }

header{ position:sticky; top:0; z-index:5; background:var(--bg-2);
  border-bottom:1px solid var(--line); padding:13px 14px 11px; }
.title{ font-weight:700; font-size:16px; display:flex; align-items:center; gap:9px; }
.title::before{ content:""; width:4px; height:16px; border-radius:3px; background:var(--accent); }
.title .cnt{ font-family:var(--mono); font-weight:500; font-size:12px; color:var(--faint); margin-left:auto; }

.search{ margin-top:11px; }
.search input{ width:100%; border:1px solid var(--line-2); border-radius:13px; padding:11px 14px; font-size:15px;
  background:var(--surface); color:var(--text); outline:none; font-family:var(--ui); }
.search input:focus{ border-color:var(--accent-line); }
.search input::placeholder{ color:var(--faint); }

.sorts{ display:flex; gap:8px; margin-top:11px; overflow-x:auto; -webkit-overflow-scrolling:touch;
  padding-bottom:2px; scrollbar-width:none; }
.sorts::-webkit-scrollbar{ display:none; }
.sortbtn{ flex:0 0 auto; border:1px solid var(--line-2); cursor:pointer; font-family:var(--ui); font-size:13px;
  padding:7px 13px; border-radius:11px; background:var(--surface); color:var(--muted); }
.sortbtn.active{ background:var(--accent-soft); border-color:var(--accent-line); color:#ffd9b8; }

.grid{ display:grid; grid-template-columns:repeat(2,1fr); gap:11px; padding:12px; }
@media (min-width:560px){ .grid{ grid-template-columns:repeat(3,1fr);} }
@media (min-width:820px){ .grid{ grid-template-columns:repeat(4,1fr);} }

.card{ border:1px solid var(--line); padding:0; text-align:left; cursor:pointer; border-radius:15px; overflow:hidden;
  background:var(--surface); color:var(--text); font:inherit; box-shadow:none; transition:transform .08s ease; }
.card:active{ transform:scale(.97); }
.thumb{ aspect-ratio:1/1; display:block; position:relative;
  background:radial-gradient(120% 120% at 50% 0%, #20303f, #0c1016 75%); }
.tbg{ position:absolute; top:6px; left:6px; font-family:var(--mono); font-size:10px; font-weight:600;
  background:rgba(8,10,14,.7); border:1px solid var(--line-2); color:var(--muted); padding:3px 7px; border-radius:7px; backdrop-filter:blur(3px); }
.sw{ display:inline-block; width:13px; height:13px; border-radius:50%; vertical-align:-2px; margin-right:5px;
  border:1px solid rgba(255,255,255,.25); }
.thumb img{ width:100%; height:100%; object-fit:cover; display:block; }
.meta{ padding:9px 11px 12px; }
.name{ font-weight:600; font-size:13.5px; line-height:1.25; word-break:break-word; }
.dims{ font-family:var(--mono); font-size:11px; color:var(--faint); margin-top:4px; }
.sub{ font-size:11.5px; color:var(--faint); margin-top:3px; }
.props{ font-size:12px; color:var(--muted); margin-top:4px; }
.colors{ font-size:12px; color:var(--muted); margin-top:4px; }
.desc{ font-size:12px; color:var(--faint); margin-top:6px; line-height:1.4; }
.ph{ width:100%; height:100%; display:flex; align-items:center; justify-content:center; font-size:38px; opacity:.35; }
.empty,.noresult{ padding:46px 16px; text-align:center; color:var(--faint); }
.hint{ text-align:center; font-size:12px; color:var(--faint); padding:2px 0 22px; }
</style></head>
<body>
"""

_PAGE_TAIL = """
<div class="noresult" id="noresult" style="display:none">Ничего не найдено</div>
<div class="hint">Нажмите модель — посчитаем печать</div>
<script>
const tg = window.Telegram.WebApp;
try { tg.ready(); tg.expand(); tg.setHeaderColor && tg.setHeaderColor('bg_color'); } catch(e){}
function filterCards(){
  const q = (document.getElementById('q').value||'').toLowerCase().trim();
  let shown = 0;
  document.querySelectorAll('.card').forEach(c => {
    const ok = !q || (c.dataset.name||'').indexOf(q) !== -1;
    c.style.display = ok ? '' : 'none'; if (ok) shown++;
  });
  document.getElementById('noresult').style.display = shown ? 'none' : '';
}
function sortBy(key, btn){
  const grid = document.querySelector('.grid'); if(!grid) return;
  document.querySelectorAll('.sortbtn').forEach(b => b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  [...grid.children]
    .sort((a,b) => (parseFloat(b.dataset[key])||0) - (parseFloat(a.dataset[key])||0))
    .forEach(c => grid.appendChild(c));
}
let busy = false;
async function pick(id){
  if (busy) return; busy = true;
  try { tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch(e){}
  try {
    const r = await fetch('/pick', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({initData: tg.initData, collection_id: id})});
    const j = await r.json().catch(()=>({ok:false}));
    if (!j.ok){ busy=false; (tg.showAlert||alert)('Не удалось открыть модель' + (j.error?(': '+j.error):'')); return; }
  } catch(e){ busy=false; (tg.showAlert||alert)('Сеть недоступна, попробуйте ещё раз'); return; }
  tg.close();
}
</script>
</body></html>
"""


def _card(it) -> str:
    name = html.escape(it.label or "Модель")
    iver = _file_ver(_served_img(it))
    dims = f'<div class="dims">📐 {html.escape(it.dims)}</div>' if getattr(it, "dims", "") else ""
    mat = html.escape(it.last_material) if getattr(it, "last_material", "") else ""
    ago = _ago(getattr(it, "last_calc_at", 0))
    info = " · ".join(x for x in [(f"🧵 {mat}" if mat else ""), ago] if x)
    info_html = f'<div class="sub">{info}</div>' if info else ""
    size = getattr(it, "size", 0) or 0
    bd = ""
    if getattr(it, "multicolor", 0):
        bd += "🌈"
    if getattr(it, "plates", 1) and it.plates >= 2:
        bd += (" 🧩" + str(it.plates))
    badge = f'<span class="tbg">{bd.strip()}</span>' if bd else ""
    return (f'<button class="card" data-name="{html.escape((it.label or "").lower())}" '
            f'data-uses="{it.uses}" data-size="{size:.0f}" '
            f'data-time="{getattr(it, "last_calc_at", 0) or 0}" onclick="pick({it.id})">'
            f'<div class="thumb">{badge}<img loading="lazy" src="/img/{it.id}?v={iver}" alt=""></div>'
            f'<div class="meta"><div class="name">{name}</div>{dims}{info_html}'
            f'<div class="sub">Считали: {it.uses}</div></div></button>')


async def _collection_page(request: web.Request) -> web.Response:
    items = await db.list_collection(limit=300)
    sorts = ('<div class="sorts">'
             '<button class="sortbtn active" onclick="sortBy(\'uses\',this)">🔥 Популярные</button>'
             '<button class="sortbtn" onclick="sortBy(\'time\',this)">🆕 Новые</button>'
             '<button class="sortbtn" onclick="sortBy(\'size\',this)">📦 Большие</button>'
             '</div>')
    head = ('<header><div class="title">📦 Коллекция моделей'
            f'<span class="cnt">{len(items)}</span></div>'
            '<div class="search"><input id="q" inputmode="search" '
            'placeholder="🔎 Поиск по названию" oninput="filterCards()"></div>'
            + (sorts if items else "") + '</header>')
    if items:
        body = head + f'<div class="grid">{"".join(_card(it) for it in items)}</div>'
    else:
        body = (head + '<div class="empty">Пока пусто — наполняется из моделей, '
                'которые присылают пользователи.</div>')
    return web.Response(text=_PAGE_HEAD + body + _PAGE_TAIL, content_type="text/html")


async def _image(request: web.Request) -> web.StreamResponse:
    try:
        cid = int(request.match_info["cid"])
    except (ValueError, KeyError):
        raise web.HTTPNotFound()
    it = await db.get_collection_item(cid)
    if not it:
        raise web.HTTPNotFound()
    for p in (it.photo_path, it.preview_path):   # реальное фото печати приоритетнее рендера
        if p and Path(p).exists():
            served = await _maybe_webp(request, Path(p))
            return web.FileResponse(served, headers=_img_headers(request))
    raise web.HTTPNotFound()


# ---------- каталог пластиков ----------
_CATALOG_TAIL = """
<div class="noresult" id="noresult" style="display:none">Ничего не найдено</div>
<div class="hint">Во время расчёта нажмите пластик — он выберется</div>
<script>
const tg = window.Telegram.WebApp;
try { tg.ready(); tg.expand(); tg.setHeaderColor && tg.setHeaderColor('bg_color'); } catch(e){}
function filterCards(){
  const q = (document.getElementById('q').value||'').toLowerCase().trim();
  let shown = 0;
  document.querySelectorAll('.card').forEach(c => {
    const ok = !q || (c.dataset.name||'').indexOf(q) !== -1;
    c.style.display = ok ? '' : 'none'; if (ok) shown++;
  });
  document.getElementById('noresult').style.display = shown ? 'none' : '';
}
let busy = false;
async function pickPlastic(id){
  if (busy) return; busy = true;
  try { tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch(e){}
  try {
    const r = await fetch('/pickplastic', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({initData: tg.initData, plastic_id: id})});
    const j = await r.json().catch(()=>({ok:false}));
    if (j.ok){ tg.close(); return; }
    busy = false;
    if (j.status === 'not_calc')
      (tg.showAlert||alert)('Это каталог для просмотра. Чтобы выбрать материал — начните расчёт и откройте каталог на шаге «Пластик».');
    else (tg.showAlert||alert)('Не удалось выбрать' + (j.error?(': '+j.error):''));
  } catch(e){ busy = false; (tg.showAlert||alert)('Сеть недоступна, попробуйте ещё раз'); }
}
</script>
</body></html>
"""


def _plastic_card(p, overrides: dict | None = None) -> str:
    overrides = overrides or {}
    name = html.escape(p.name or "Пластик")
    props = html.escape(f"{p.material_type} · {p.eff_price_per_kg:g} {config.CURRENCY}/кг · {p.density:g} г/см³")
    cols_html = ""
    if getattr(p, "color_list", None):
        parts = []
        for nm in p.color_list:
            css = _color_css(nm, overrides)
            sw = f'<span class="sw" style="background:{css}"></span>' if css else ""
            parts.append(sw + html.escape(nm))
        cols_html = f'<div class="colors">{", ".join(parts)}</div>'
    desc = html.escape(p.description or "")
    desc_html = f'<div class="desc">{desc}</div>' if desc else ""
    has_photo = bool(p.photo_path and Path(p.photo_path).exists())
    thumb = (f'<img loading="lazy" src="/pimg/{p.id}?v={_file_ver(p.photo_path)}" alt="">'
             if has_photo else '<div class="ph">🧵</div>')
    return (f'<button class="card" data-name="{html.escape((p.name or "").lower())}" '
            f'onclick="pickPlastic({p.id})">'
            f'<div class="thumb">{thumb}</div>'
            f'<div class="meta"><div class="name">{name}</div>'
            f'<div class="props">{props}</div>{cols_html}{desc_html}</div></button>')


async def _catalog_page(request: web.Request) -> web.Response:
    items = await db.list_plastics(active_only=True)
    overrides = await db.get_color_overrides()
    head = ('<header><div class="title">🧵 Каталог пластиков'
            f'<span class="cnt">{len(items)}</span></div>'
            '<div class="search"><input id="q" inputmode="search" '
            'placeholder="🔎 Поиск по названию" oninput="filterCards()"></div></header>')
    if items:
        body = head + f'<div class="grid">{"".join(_plastic_card(p, overrides) for p in items)}</div>'
    else:
        body = head + '<div class="empty">Пока нет пластиков — добавит администратор.</div>'
    return web.Response(text=_PAGE_HEAD + body + _CATALOG_TAIL, content_type="text/html")


async def _plastic_image(request: web.Request) -> web.StreamResponse:
    try:
        pid = int(request.match_info["pid"])
    except (ValueError, KeyError):
        raise web.HTTPNotFound()
    p = await db.get_plastic(pid)
    if p and p.photo_path and Path(p.photo_path).exists():
        served = await _maybe_webp(request, Path(p.photo_path))
        return web.FileResponse(served, headers=_img_headers(request))
    raise web.HTTPNotFound()


# _spool_photo_path уехал в app/web/common.py (волна 1b endgame-плана) — реэкспорт в фасадном
# блоке common в шапке файла.


async def _spool_image(request: web.Request) -> web.StreamResponse:
    """GET /spool/{pid}?c=<цвет> — фото катушки цвета (клик по кружку цвета в «Материалах»)."""
    try:
        pid = int(request.match_info["pid"])
    except (ValueError, KeyError):
        raise web.HTTPNotFound()
    p = _spool_photo_path(pid, request.query.get("c", ""))
    if p is None:
        raise web.HTTPNotFound()
    served = await _maybe_webp(request, p)
    return web.FileResponse(served, headers=_img_headers(request))


async def _api_thumb(request: web.Request) -> web.StreamResponse:
    """Прокси внешних превью поиска моделей (OpenTodo 27f25b91): сервер достаёт картинку с CDN
    площадки (до него доступ есть) и отдаёт со своего origin, обходя сетевую блокировку у
    клиента. URL подписан (_sign) — не открытый прокси, только адреса, что выдал сам бот.

    Проверка НЕ через check_sig: подписан целый внешний URL, а не «префикс + id» (см.
    `_thumb_proxy`). Пустой `u` отсекается отдельно — иначе подпись пустой строки была бы
    предъявляемым «ключом» к ветке ниже."""
    url = request.query.get("u", "")
    if not url or request.query.get("s", "") != _sign(url):
        return web.Response(status=403)
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return web.Response(status=400)
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": downloader._PR_UA}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s, s.get(url) as r:
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
            if r.status != 200 or not ctype.startswith("image/"):
                return web.Response(status=404)
            # ВАЖНО: content.read(n) отдаёт только первый пришедший чанк (≤ n), а не n байт —
            # из-за этого клиенты получали обрезанный JPEG и превью «не грузились»
            # (OpenTodo 5ea29c10). Читаем тело целиком, потолок размера контролируем сами.
            data = bytearray()
            async for chunk in r.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > _THUMB_MAX:
                    return web.Response(status=404)
        return web.Response(body=bytes(data), content_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400"})
    except Exception:  # noqa: BLE001
        log.debug("thumb proxy failed: %s", url, exc_info=True)
        return web.Response(status=404)

async def _api_model_search(request: web.Request) -> web.Response:
    """Текстовый поиск моделей по площадкам (Printables/Thingiverse/MakerWorld) для мини-аппа.
    Контракт: { ok, hits:[SearchHit], sources:[...] } | { ok:false, error }. Запрос переводится
    RU→EN внутри агрегатора; недоступная площадка тихо выпадает (graceful degradation)."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not config.MODEL_SEARCH_ENABLED:
        return web.json_response({"ok": False, "error": "Поиск отключён"})
    if not ratelimit.allow(f"search:{uid}", config.RATE_SEARCH_PER_MIN, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return web.json_response({"ok": True, "hits": [], "sources": []})
    csv = (request.query.get("sources") or "").strip()
    want = [s for s in (x.strip() for x in csv.split(",")) if s] or None
    try:
        hits, sources = await modelsearch.search(q, sources=want)
    except Exception as exc:  # noqa: BLE001
        log.warning("model_search failed", exc_info=True)
        return web.json_response({"ok": False, "error": f"Поиск не удался: {exc}"})
    for hit in hits:   # внешние превью гоним через свой прокси (иначе у владельца не грузятся)
        hit["thumb"] = _thumb_proxy(hit.get("thumb", ""))
    await db.log_event(uid, "искал модели", f"«{q[:60]}» — {len(hits)} рез.")
    return web.json_response({"ok": True, "hits": hits, "sources": sources})

def _collection_item_json(it) -> dict:
    """Карточка модели каталога для фронта — единая форма для /api/collection и поиска."""
    return {"id": it.id, "label": it.label, "uses": it.uses, "dims": it.dims or "",
            "mc": int(getattr(it, "multicolor", 0) or 0), "plates": int(getattr(it, "plates", 1) or 1),
            # «напечатано вживую»: есть реальное фото печати (фильтр в коллекции, f0d7c423)
            "printed": int(bool(it.photo_path and Path(it.photo_path).exists())),
            # «от N руб.» — кэш последнего расчёта (ux-simplification.md, идея 3)
            "price": float(getattr(it, "last_price", 0.0) or 0.0),
            "category": getattr(it, "category", "") or "",
            "tags": getattr(it, "tags", "") or "",   # ИИ-теги (e0dedc27) — участвуют в поиске
            "preview": f"/img/{it.id}?v={_file_ver(_served_img(it))}"}

async def _api_collection(request: web.Request) -> web.Response:
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    # Каталог с категориями (c9c42789): ?category=X&offset=N — страница раздела (пагинация,
    # разделы из ВК содержат сотни моделей); без category — как раньше топ-120 (главная/поиск).
    # category=__fav__ — виртуальный раздел «Избранное» текущего юзера (сентинел, а не имя
    # «Избранное», чтобы не столкнуться с одноимённой темой из ВК-импорта).
    cat = (request.query.get("category") or "").strip()
    offset = max(0, _toint(request.query.get("offset"), 0))
    limit = max(1, min(120, _toint(request.query.get("limit"), 60)))
    if cat == "__fav__":
        items = await db.list_favorites(uid, limit=limit, offset=offset)
    elif cat:
        items = await db.list_collection_by_category(cat, limit=limit, offset=offset)
    else:
        items = await db.list_collection(limit=120)
    out = [_collection_item_json(it) for it in items]
    resp = {"ok": True, "items": out, "currency": config.CURRENCY}
    if cat:
        resp["next_offset"] = (offset + len(out)) if len(out) == limit else None
    else:
        resp["categories"] = await build_catalog_categories()
    return web.json_response(resp)


async def build_catalog_categories() -> list[dict]:
    """Разделы каталога с картинкой (превью самой популярной модели раздела, 85e8d47b) — для
    /api/collection и для пакета каталога приёмной (app/relaybundle.py): без ПК мини-апп
    показывает ровно то же."""
    cats = []
    for n, c, top_id, _srt in await db.list_collection_categories():
        entry = {"name": n, "count": c}
        if top_id:
            top = await db.get_collection_item(top_id)
            p = _served_img(top) if top else None
            if p and Path(p).exists():
                entry["preview"] = f"/img/{top_id}?v={_file_ver(p)}"
        cats.append(entry)
    return cats

async def _api_prefs(request: web.Request) -> web.Response:
    """Сохранить личные настройки клиента (OpenTodo 0d11db78): режим подтверждения цены и
    доставку по умолчанию (тип + адрес, доработка A2 31.07). Читаются они в /api/profile —
    отдельного GET не заводим, профиль и так открывается первым делом.

    Сохраняем ровно то, что прислали: тело с ключом price_mode правит цену, тело с ключом
    delivery_address/delivery_type_id — доставку. Так одна ручка обслуживает две независимые
    секции профиля и ни одна из них не затирает чужую настройку пустым значением.
    Нормализация — в db (set_user_price_prefs / set_user_delivery_prefs)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"prefs:{uid}", 30, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    prefs = None
    if "price_mode" in body or "price_pct" in body:
        prefs = await db.set_user_price_prefs(uid, str(body.get("price_mode") or "ask"),
                                              body.get("price_pct") or 0)
    if "delivery_address" in body or "delivery_type_id" in body:
        prefs = await db.set_user_delivery_prefs(uid, _toint(body.get("delivery_type_id")),
                                                 str(body.get("delivery_address") or ""))
    if prefs is None:
        prefs = await db.get_user_prefs(uid)
    return web.json_response({"ok": True, "prefs": prefs})

async def _api_favorite(request: web.Request) -> web.Response:
    """«Избранное» (сердечко на модельке каталога): body {id, on} — поставить/снять.
    Идемпотентно (фронт шлёт ЖЕЛАЕМОЕ состояние, не toggle) — двойной тап/повтор запроса
    не переворачивают сердечко обратно. Список избранного фронт получает в /api/options
    (fav_ids), раздел «Избранное» — /api/collection?category=__fav__."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    uid = _verify_init_data(body.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    if not ratelimit.allow(f"fav:{uid}", 60, 60):
        return web.json_response({"ok": False, "error": "Слишком часто — подождите минуту"})
    cid = _toint(body.get("id"))
    it = await db.get_collection_item(cid) if cid else None
    if not it:
        return web.json_response({"ok": False, "error": "Модель не найдена"}, status=404)
    on = bool(body.get("on"))
    await db.set_favorite(uid, cid, on)
    if on:
        await db.log_event(uid, "добавил в избранное", it.label)
    return web.json_response({"ok": True, "fav": on})

async def _api_recommend(request: web.Request) -> web.Response:
    """Бесконечная лента рекомендаций на главной (df0c9674): 70% — частые категории юзера
    (по его заказам/моделям через hash), 30% — остальные. Пагинация offset/next_offset."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    offset = max(0, _toint(request.query.get("offset"), 0))
    limit = max(1, min(60, _toint(request.query.get("limit"), 24)))
    items, nxt = await db.recommend_page(uid, limit=limit, offset=offset)
    out = [{"id": it.id, "label": it.label, "dims": it.dims or "",
            "plates": int(getattr(it, "plates", 1) or 1),
            "printed": int(bool(it.photo_path and Path(it.photo_path).exists())),
            "price": float(getattr(it, "last_price", 0.0) or 0.0),
            "category": getattr(it, "category", "") or "",
            "tags": getattr(it, "tags", "") or "",   # ИИ-теги (e0dedc27) — участвуют в поиске
            "preview": f"/img/{it.id}?v={_file_ver(_served_img(it))}"} for it in items]
    return web.json_response({"ok": True, "items": out, "next_offset": nxt,
                              "currency": config.CURRENCY})

def feed_item_json(it, minutes: dict) -> dict:
    """Карточка ленты главной (F4) — для /api/feed и для пакета каталога приёмной."""
    price = float(getattr(it, "last_price", 0.0) or 0.0)
    return {
        "id": it.id, "title": it.label, "label": it.label,
        "preview": f"/img/{it.id}?v={_file_ver(_served_img(it))}",
        "price_from": price, "price": price,
        "time_min": round(minutes.get(it.hash, 0.0), 1),
        "dims": it.dims or "",
        "plates": int(getattr(it, "plates", 1) or 1),
        "printed": int(bool(it.photo_path and Path(it.photo_path).exists())),
        "category": getattr(it, "category", "") or "",
    }


async def _api_feed(request: web.Request) -> web.Response:
    """Лента «Рекомендуем напечатать» на главной F4: курсорная пагинация cursor/limit →
    {ok, items, next}. Аддитивно к /api/recommend (тот остаётся как есть): источник тот же —
    коллекция мастера через recommend_page (70% частых категорий юзера / 30% прочее), но
    карточке ленты дополнительно нужно время печати — берём из последнего расчёта по hash.
    Поля старой плитки (label/dims/printed/price/category) сохранены: тап открывает ту же
    карточку товара (app.openProduct), что и раньше."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    cursor = max(0, _toint(request.query.get("cursor"), 0))
    limit = max(1, min(30, _toint(request.query.get("limit"), 10)))
    items, nxt = await db.recommend_page(uid, limit=limit, offset=cursor)
    minutes = await db.collection_last_minutes([it.hash for it in items])
    out = [feed_item_json(it, minutes) for it in items]
    # Карточка Gridfinity-конфигуратора в ленте (design_handoff_gridfinity): ЧЕРЕЗ РАЗ —
    # на каждом втором открытии первой страницы ленты карточки нет, чтобы не приедалась.
    # Позиция — среди первых 5 (индексы 1..4: самую верхнюю рекомендацию не заслоняем),
    # стабильна в пределах дня (seed uid+дата — как у самой ленты recommend_page).
    # Чередование в памяти процесса: перезапуск бота просто начинает отсчёт заново.
    if cursor == 0 and out:
        show = not _GF_FEED_SHOWN.get(uid, False)
        _GF_FEED_SHOWN[uid] = show
        if show:
            pos = random.Random(f"gf:{uid}:{time.strftime('%Y%m%d')}").randint(1, min(4, len(out)))
            # заглушки preview/price/time — для мини-аппа, ОТКРЫТОГО до перезапуска бота:
            # его старый fcard не знает kind и рисует обычную плитку — пусть пустую, но не битую
            out.insert(pos, {"kind": "gridfinity", "id": "gf",
                             "title": "Органайзер в ящик",
                             "label": "Органайзер в ящик",
                             "preview": "", "price": 0, "price_from": 0,
                             "time_min": 0, "printed": 0, "category": ""})
    return web.json_response({"ok": True, "items": out, "next": nxt,
                              "currency": config.CURRENCY})

async def _api_collection_search(request: web.Request) -> web.Response:
    """Поиск по коллекции: подстрока + фаззи-опечатки по ВСЕМУ каталогу (label + ИИ-теги +
    orig_label, ранжирование бакетами — db.search_collection, OpenTodo c52f32b4) + семантика
    (b56041c9) поверх топ-200 популярных как добавка («дракон» найдёт «Яйцо дракона»).
    Возвращает готовые карточки items — после перехода каталога на разделы (c9c42789)
    фронт держит только топ-120, и выдача по одним id была бы нерендерибельной.
    ok:False — только при ошибке; пустая выдача = ok:True."""
    uid = _verify_init_data(request.query.get("initData", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    q = (request.query.get("q") or "").strip()[:80]
    if len(q) < 2:
        return web.json_response({"ok": False, "error": "короткий запрос"})
    if not ratelimit.allow(f"semsearch:{uid}", 30, 60):
        return web.json_response({"ok": False, "error": "слишком часто"})
    found = await db.search_collection(q, limit=60)
    have = {it.id for it in found}
    top = await db.list_collection(limit=200)
    # ИИ-теги подмешиваются к названию (e0dedc27): «дракон» найдёт и по тегам
    payload = [{"id": it.id,
                "label": f'{it.label} {getattr(it, "tags", "") or ""}'.strip(),
                "material": getattr(it, "material", "") or ""}
               for it in top]
    try:
        # Семантика — добавка, подстрочную выдачу она блокировать не должна: первый поиск
        # после рестарта грузит модель и греет кэш векторов (~30 c живьём) — тред при
        # таймауте продолжает работу и догревает кэш, следующие запросы получат семантику.
        ids = await asyncio.wait_for(
            asyncio.to_thread(semsearch.search, q, payload), timeout=3.0)
    except TimeoutError:
        ids = None
    if ids:
        by_id = {it.id: it for it in top}
        found += [by_id[i] for i in ids if i in by_id and i not in have]
    return web.json_response({"ok": True,
                              "items": [_collection_item_json(it) for it in found[:60]],
                              "currency": config.CURRENCY})


async def _api_idea(request: web.Request) -> web.Response:
    """Идея по улучшению из мини-аппа (экран «Как это работает»): пишем в БД + шлём админам."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _bad()
    u = _verify_user(body.get("initData", ""))
    if not u:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    text = (body.get("text") or "").strip()[:1000]
    if len(text) < 5:
        return web.json_response({"ok": False, "error": "Слишком короткая идея"})
    uid = int(u["id"])
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or str(uid)
    await db.add_idea(uid, name, text)
    if _bot is not None:
        try:
            uname = f"@{u['username']}" if u.get("username") else "—"
            await notify.notify_admins(
                _bot, "💡 <b>Новая идея от пользователя</b>\n\n"
                      f"👤 {html.escape(name)} ({uname}, id <code>{uid}</code>)\n\n{html.escape(text)}")
        except Exception:  # noqa: BLE001
            log.debug("idea notify failed", exc_info=True)
    return web.json_response({"ok": True})
