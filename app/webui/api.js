// api.js — single data layer for the Telegram Mini App.
// In Telegram: reads WebApp.initData, calls the bot's real /api/* endpoints.
// In a plain browser (no initData): serves mock data so the UI can be previewed.
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  var INIT = (tg && tg.initData) || '';
  // ПК-режим админки (e075a6f3): ?admintoken=… → токен вместо initData (сервер принимает
  // его как аккаунт мастера). Токен прячем из адресной строки и держим в localStorage,
  // чтобы работали перезагрузка и внутренние переходы.
  if (!INIT) {
    try {
      var _q = new URLSearchParams(location.search);
      var _tok = _q.get('admintoken');
      if (_tok) {
        localStorage.setItem('pcb_admin_token', _tok);
        _q.delete('admintoken');
        history.replaceState(null, '', location.pathname + (String(_q) ? '?' + String(_q) : '') + location.hash);
      }
      INIT = _tok || localStorage.getItem('pcb_admin_token') || '';
    } catch (e) { /* приватный режим браузера и т.п. — остаёмся в MOCK */ }
  }
  // Сессия «продолжить на компьютере»: страница /gfx/desk/{token} кладёт ключ в
  // window.__GFX_KEY__ ДО этого файла. Ключ НЕ становится общим удостоверением (INIT) —
  // сервер принимает его только на /api/gridfinity/ и только для одного проекта, поэтому
  // и подписываем им ровно эти запросы (см. gfxKey ниже). Мок-домен при этом обязан
  // молчать: без initData он иначе решил бы, что нас открыли для предпросмотра, и вместо
  // настоящего проекта клиент увидел бы выдуманный.
  var GFXKEY = (typeof window !== 'undefined' && window.__GFX_KEY__) || '';
  var MOCK = !INIT && !GFXKEY;

  /** Ключ сессии ПК — ТОЛЬКО для ручек мастерской. Любой другой путь его не увидит. */
  function gfxKey(path) {
    return (GFXKEY && String(path).indexOf('/api/gridfinity/') === 0) ? GFXKEY : '';
  }

  // ---- low-level fetch helpers (real mode) --------------------------------
  function qs(obj) {
    return Object.keys(obj).map(function (k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(obj[k]);
    }).join('&');
  }
  // Разбор ответа с проверкой HTTP-статуса. Раньше её не было вообще: на 500 сервер отдаёт
  // HTML/пустое тело, r.json() падал с невнятным SyntaxError, и в UI это выглядело как
  // «ничего не произошло». Теперь возвращаем нормальный {ok:false, error}.
  async function parseJSON(r, path) {
    var txt = await r.text();
    var data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
    if (data && typeof data === 'object') return data;
    if (!r.ok) {
      return { ok: false, status: r.status,
        error: r.status === 403 ? 'Нет доступа: токен админки не принят'
          : (r.status === 404 ? 'Метод не найден — возможно, бот на старой версии'
            : 'Сервер ответил ошибкой ' + r.status) };
    }
    return { ok: false, error: 'Непонятный ответ сервера' + (path ? ' (' + path + ')' : '') };
  }
  async function getJSON(path, params) {
    var p = Object.assign({ initData: INIT }, params || {});
    var k = gfxKey(path); if (k) p.gfxkey = k;
    // страховка от '?' уже в path: иначе initData клеится вторым '?' и сервер его не видит
    // (403 auth — так молча умирал семантический поиск, жалоба «Беззубик — пусто» 2026-07-10)
    var r = await fetch(path + (path.indexOf('?') >= 0 ? '&' : '?') + qs(p), { headers: { 'Accept': 'application/json' } });
    return parseJSON(r, path);
  }
  async function postJSON(path, body) {
    var r = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ initData: INIT }, gfxKey(path) ? { gfxkey: GFXKEY } : null, body || {})),
    });
    return parseJSON(r, path);
  }

  // ---- форматирование для UI (БОЕВЫЕ пути, не мок) -------------------------
  // Исторически эти две функции лежали внутри мок-блока ниже, хотя экспортируются как
  // API.fmt / API.human и вызываются в проде при ОТРИСОВКЕ: корзина и доставка
  // (screens_more.js, screens_core.js), кастомайзер (screens_custom.js), оценка «от ≈»
  // на карточке модели (app.js), время в админских экранах (screens_admin.js).
  // Держим их здесь, рядом с низкоуровневыми помощниками, чтобы вынос мок-домена
  // в отдельный файл их не унёс вместе с моком и не уронил корзину при отрисовке.
  // Валюта тут захардкожена сознательно — это отдельный открытый вопрос, не трогаем.
  function fmt(n) { return n.toFixed(2).replace('.', ',') + '\u00A0руб.'; }
  function human(min) {
    var hh = Math.floor(min / 60), mm = Math.round(min % 60);
    return (hh ? hh + ' ч ' : '') + mm + ' мин';
  }

  // =========================================================================
  //  MOCK domain — вынесен в api_mock.js и подключается ТОЛЬКО в мок-режиме
  // =========================================================================
  // Мок подменяет ВСЕ ответы выдуманными и нужен ровно тогда, когда авторизоваться нечем:
  // ни initData Telegram, ни токена ПК-админки. Раньше он лежал прямо здесь и ехал каждому
  // клиенту при каждом открытии мини-аппа (страница отдаётся с no-store, то есть заново).
  //
  // Переменные ниже — те же самые ссылки замыкания, что и были: api_mock.js заполняет их через
  // __PS_MOCK_INSTALL__, поэтому тела методов API не изменились ни на символ. К моменту первого
  // вызова (React уже смонтирован, экран запросил данные) файл давно выполнен.
  var M, mockPriceCalc, mockCalc, mockBrand, mockBrandValues;
  window.__PS_MOCK_INSTALL__ = function (build) {
    var mod = build({ fmt: fmt, human: human });
    M = mod.M;
    mockPriceCalc = mod.mockPriceCalc; mockCalc = mod.mockCalc;
    mockBrand = mod.mockBrand; mockBrandValues = mod.mockBrandValues;
  };
  if (MOCK) {
    // document.write, а не createElement+appendChild: мок обязан быть на месте ДО первого
    // вызова API, а асинхронно вставленный тег этого не гарантирует. api.js подключён обычным
    // парсер-блокирующим <script> и в мини-аппе, и в настольной админке — значит запись
    // попадает в поток разбора сразу за нами и выполнится раньше следующего скрипта.
    // Путь считаем от СВОЕГО src: документ деска лежит в /app/desk/, и относительный
    // 'api_mock.js' указал бы на несуществующий /app/desk/api_mock.js.
    // ?v=… — тот же ключ кеша, что у ленивых скриптов вьюера и админки: файл подключается
    // не тегом из разметки, и залипшая копия в WebView иначе переживёт деплой. В деске
    // __APPVER__ не вшивается — там просто без ?v.
    var _self = document.currentScript;
    var _ver = (window.__APPVER__ && window.__APPVER__.indexOf('%') < 0) ? window.__APPVER__ : '';
    var _mockSrc = ((_self && _self.src) ? _self.src.replace(/[^/]*$/, '') : '') + 'api_mock.js'
      + (_ver ? '?v=' + encodeURIComponent(_ver) : '');
    if (document.readyState === 'loading') {
      document.write('<script src="' + _mockSrc + '"><\/script>');
    } else {
      // Страница уже разобрана (api.js подключили не тегом в разметке) — write затёр бы
      // документ целиком. Грузим обычным способом: предпросмотр поднимется, мок приедет чуть
      // позже. В мини-аппе и в деске эта ветка не используется.
      var _s = document.createElement('script');
      _s.src = _mockSrc; _s.async = false;
      (document.head || document.documentElement).appendChild(_s);
    }
  }

  // =========================================================================
  //  Public API — same surface in mock and real mode
  // =========================================================================
  function normOptions(o) {
    // real API gives plastics[].photo(bool)+pver — turn into a usable photoUrl
    (o.plastics || []).forEach(function (p) {
      if (p.photoUrl == null) p.photoUrl = p.photo ? ('/pimg/' + p.id + '?v=' + (p.pver || 0)) : '';
    });
    return o;
  }

  // Демо-данные мастерской для превью без бэка (MOCK): один проект с двумя деталями,
  // одна из которых уже заказана — чтобы в превью были видны и метки, и состояния.
  var MGFX = {
    projects: { ok: true, active: 1, archived_n: 0, limit: 20, measures: [], projects: [{
      id: 1, title: 'Кухня, нижний ящик', updated_at: 0, archived: 0,
      counts: { total: 2, draft: 1, hold: 0, ordered: 1, printed: 0, sel: 0 },
      preview: { cols: 11, rows: 6, type: 'drawer', W: 480, D: 270, H: 120, series: '', mods: [
        { id: 1, x: 0, y: 0, w: 4, d: 2, c: 0, o: 1 }, { id: 2, x: 4, y: 0, w: 4, d: 4, c: 3, o: 0 }] } }] },
    project: { ok: true, config: null, versions: [], share: '', batches: [],
      // стандарт мастерской: форма ровно как у боевой ручки — плашка «печатаем стандартом»
      // рисуется по нему, и без него предпросмотр в браузере молча терял бы целый блок
      standard: { text: 'Elegoo PETG · слой 0.3 мм (сопло 0.6)', pct: 25 },
      project: { id: 1, title: 'Кухня, нижний ящик', archived: 0, updated_at: 0 },
      counts: { total: 2, draft: 1, hold: 0, ordered: 1, printed: 0, sel: 0 },
      colors: [], parts: [
        { id: 11, mod_id: 1, name: 'Под лопатки', auto_name: 'Бокс 4×2 · лоток · 67 мм',
          custom_name: true, note: 'лопатки, венчик', qty: 1, state: 'ordered',
          live_state: 'printed', state_label: 'напечатано', sel: 0, request_id: 134,
          req_status: 'printed', version: 1, parent_id: 0, frozen: 1, changed: 0, on_plan: 1,
          color_idx: 0, color: 'оранж', color_hex: '#ff7a1a', gen: 'tray', gen_label: 'лоток',
          cells: '4×2', w_mm: 167.4, d_mm: 83.4, h_mm: 67, units: 9, vol_cm3: 78,
          grams: 96, minutes: 250, price: 380, printed_at: 0 },
        { id: 12, mod_id: 2, name: 'Сетка под соусники', auto_name: 'Бокс 4×4 · сетка · 54 мм',
          custom_name: true, note: '', qty: 1, state: 'draft', live_state: 'draft',
          state_label: 'черновик', sel: 0, request_id: 0, req_status: '', version: 1,
          parent_id: 0, frozen: 0, changed: 0, on_plan: 1, color_idx: 3, color: 'лайм',
          color_hex: '#2dd47e', gen: 'grid', gen_label: 'сетка', cells: '4×4',
          w_mm: 167.4, d_mm: 167.4, h_mm: 54, units: 7, vol_cm3: 124, grams: 152,
          minutes: 380, price: 640, printed_at: 0 }] },
  };

  // Смета партии в превью считается так же, как на сервере: подготовка одна на заявку,
  // а не на каждую деталь (иначе мок показывал бы цену, которой не бывает).
  function mgfxMoney() {
    var sel = MGFX.project.parts.filter(function (p) { return p.sel; });
    var sum = function (k) { return sel.reduce(function (a, p) { return a + p[k]; }, 0); };
    MGFX.project.money = { n: sel.length, qty: sum('qty'), grams: Math.round(sum('grams')),
      minutes: Math.round(sum('minutes')), price: sel.length ? Math.round(sum('price')) : 0,
      discount_pct: sel.length ? 25 : 0, std_pct: 25 };
    MGFX.project.counts.sel = sel.length;
    return MGFX.project;
  }

  var API = {
    MOCK: MOCK,
    fmt: fmt, human: human,

    options: function () { return MOCK ? Promise.resolve(M.options) : getJSON('/api/options').then(normOptions); },

    uploadUrl: function (url) { return MOCK ? Promise.resolve(M.model) : postJSON('/api/model', { url: url }); },
    // Поиск модели по запросу. Контракт: { ok, hits:[SearchHit], sources:[...] } | { ok:false, error }.
    // В mock-режиме отдаём фиктивную выдачу (M.search), чтобы статический preview работал без бота.
    modelSearch: function (q) { return MOCK ? Promise.resolve(M.search) : getJSON('/api/model_search', { q: q }); },
    uploadFile: function (file) {
      if (MOCK) return Promise.resolve(M.model);
      var fd = new FormData(); fd.append('initData', INIT); fd.append('file', file);
      // parseJSON, а не r.json(): HTML-ответ прокси (413 «слишком большой», 502) иначе ронял
      // разбор, и клиент видел «Сеть недоступна» вместо настоящей причины.
      return fetch('/api/model', { method: 'POST', body: fd }).then(function (r) { return parseJSON(r, '/api/model'); });
    },
    variant: function (url, iid) { return MOCK ? Promise.resolve(M.model) : postJSON('/api/variant', { url: url, iid: iid }); },
    // Вариант НАБОРА файлов карточки (87eb9cf3): пересобирает токен под выбранный комплект
    fileVariant: function (token, key) { return MOCK ? Promise.resolve(M.model) : postJSON('/api/file_variant', { token: token, key: key }); },
    // URL сырого файла модели для three.js-вьюера (B1). Возвращает СТРОКУ-URL (не Promise):
    // three.js-лоадеры сами делают XHR по нему, поэтому кладём initData в query (сервер
    // сверит uid с владельцем токена). MOCK → '' → вьюер рисует демо-меш, PNG-фолбэк цел.
    modelFile: function (token) {
      if (MOCK || !token) return '';
      return '/api/modelfile/' + encodeURIComponent(token) + '?initData=' + encodeURIComponent(INIT);
    },
    use: function (kind, id) {
      if (MOCK) return Promise.resolve(Object.assign({}, M.model, { label: 'Из ' + kind }));
      return postJSON('/api/use', { kind: kind, id: id });
    },
    // Заказ без цены, пока мастерская офлайн (Э2): ссылка, файл или модель мастерской уходят
    // приёмной на VPS — nginx домена отдаёт эту ручку ей всегда. src: {url} | {file} |
    // {kind, id, label}. reqId — ключ идемпотентности: двойное нажатие и повтор на плохой связи
    // дают одну заявку. initData — ПЕРВЫМ полем формы: приёмная проверяет подпись до файла.
    orderDeferred: function (src, comment, reqId) {
      if (MOCK) return Promise.resolve({ ok: true, deferred: true, text: 'Заявка принята (демо)' });
      if (src && src.file) {
        var fd = new FormData();
        fd.append('initData', INIT); fd.append('client_req_id', reqId); fd.append('comment', comment || '');
        fd.append('file', src.file);
        return fetch('/api/order_deferred', { method: 'POST', body: fd })
          .then(function (r) { return parseJSON(r, '/api/order_deferred'); });
      }
      var body = { client_req_id: reqId, comment: comment || '' };
      if (src && src.url) body.url = src.url;
      else if (src) { body.kind = src.kind; body.id = src.id; if (src.label) body.label = src.label; }
      return postJSON('/api/order_deferred', body);
    },
    // Параметрические модели на OpenSCAD (OpenTodo c1cc840a): список моделей + их параметры,
    // и генерация по введённым значениям (вернёт токен обычной модели → дальше расчёт/заказ).
    parametricModels: function () { return MOCK ? Promise.resolve({ ok: true, available: true, models: [{ id: 'keychain_qr', name: 'QR-брелок', desc: 'QR-код на вашу ссылку, телеграм или контакт', params: [
      { name: 'Text', type: 'string', default: 'https://t.me/', label: 'Ссылка или текст' },
      { name: 'Size', type: 'number', default: 40, min: 26, max: 60, label: 'Размер, мм' }] }] }) : getJSON('/api/parametric/models'); },
    parametricModel: function (modelId, params) { return MOCK ? Promise.resolve(M.model) : postJSON('/api/parametric/model', { model_id: modelId, params: params }); },
    // Двухцветный 3D-рендер параметрической модели (корпус=color1, текст+рамка=color2) → data-URL.
    // В мок-режиме рендера нет → {ok:false}, фронт остаётся на SVG-превью.
    parametricPreview2c: function (modelId, params, color1, color2, color3) {
      if (MOCK) return Promise.resolve({ ok: false });
      return postJSON('/api/parametric/preview2c', { model_id: modelId, params: params,
        color1: color1 || '', color2: color2 || '', color3: color3 || '' });
    },

    calc: function (body) { return MOCK ? Promise.resolve(mockCalc(body)) : postJSON('/api/calc', body); },
    // Быстрый пересчёт под другой пластик/цвет без переслайса (OpenTodo 050aff56 + fb552c3a).
    requote: function (token, plasticId, qty, color) {
      if (MOCK) { var j = mockCalc({ plastic_id: plasticId, qty: qty || 1 }); j.result.approx = true; return Promise.resolve(j); }
      return postJSON('/api/requote', { token: token, plastic_id: plasticId, qty: qty || 1, color: color || '' });
    },
    // «Уведомить, когда посчитается» + смета по токену из уведомления (OpenTodo ae4c6c13).
    // Цвета слотов мультицвета → серверный снимок заказа (2f7b597f): лёгкая синхронизация
    // без переслайса, чтобы «Оставить заказ» ушёл с теми цветами, что на экране.
    colorsSel: function (token, sel) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/colors_sel', { token: token, colors_sel: sel || [] }); },
    calcNotify: function (token) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/calc_notify', { token: token }); },
    quoteState: function (token) { return MOCK ? Promise.resolve({ ok: true, model: M.model, result: mockCalc({ qty: 1 }).result }) : getJSON('/api/quote_state', { token: token }); },
    // Журнал печатей мастера (ed1518ed) + заявка на семейный счёт (d5689a4b).
    adminJournal: function () { return MOCK ? Promise.resolve({ ok: true, items: [
      { id: 4, label: 'Bag Clip V2', plastic_name: 'Kingroon PETG', grams: 23, minutes: 27, graded: 0, request_id: 31, timelapse: '/x.mp4', timelapse_url: '/jtl/4?s=x', photo: '../assets/models/photo_dino.jpg', ago: 'только что' },
      { id: 3, label: 'Шестерёнки х6', plastic_name: 'Elegoo PLA', grams: 88, minutes: 210, result: 'fail', fail_pct: 35, fail_reason: 'две отлипли', graded: 1, ago: 'час назад' },
      { id: 2, label: 'Бэйбизавр х2', plastic_name: 'Elegoo PLA', grams: 64, minutes: 130, result: 'ok', fail_pct: 0, graded: 1, ago: '2 ч назад' },
      { id: 1, label: 'Кронштейн полки', plastic_name: 'Elegoo PETG', grams: 41, minutes: 95, result: 'fail', fail_pct: 100, fail_reason: 'отлип от стола', graded: 1, ago: 'вчера' },
    ], stats: { total: 15, ungraded: 1, fails: 3, fail_pct: 12, grams: 1263, minutes: 2237, waste: 152, by_plastic: [
      { plastic: 'Elegoo PLA', n: 9, fails: 2, fail_pct: 9, grams: 820, waste: 72 },
      { plastic: 'Elegoo PETG', n: 5, fails: 1, fail_pct: 19, grams: 420, waste: 80 } ] } }) : getJSON('/api/admin/journal'); },
    adminJournalAdd: function (body) { return MOCK ? Promise.resolve({ ok: true, id: 3 }) : postJSON('/api/admin/journal_add', body); },
    // правка прошлой записи журнала (OpenTodo 60e0b2a8)
    adminJournalUpdate: function (id, body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/journal_update', Object.assign({ id: id }, body)); },
    adminJournalDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/journal_delete', { id: id }); },
    // удаление медиа (1faab6cb): kind = 'photo' (скрин стола / фото результата) | 'timelapse'
    adminJournalMediaDelete: function (id, kind) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/journal_media_delete', { id: id, kind: kind }); },
    adminOrderMediaDelete: function (id, kind) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/order_media_delete', { id: id, kind: kind }); },
    // ---- Калибровка филамента (кампании, паспорта, пресеты, образцы) --------------------
    // MOCK-данные нарочно повторяют реальную калибровку Elegoo PLA Matte 27.07: экран
    // отлаживается без бота ровно на тех числах, что и в проде.
    adminCalib: function () { return MOCK ? Promise.resolve({ ok: true,
      stats: { runs: 1, active: 1, passports: 1, samples: 3, presets: 3, prints: 7, loose: 0,
        by_type: [{ key: 'temp', n: 1 }, { key: 'flow', n: 2 }, { key: 'pa', n: 1 }] },
      runs: [{ id: 1, title: 'Смена сопла 0.4', reason: 'новое незаводское сопло', nozzle: '0.4',
        plastic_name: 'Elegoo PLA Matte', status: 'active', steps: 7, steps_done: 5,
        started_at: Math.floor(Date.now() / 1000) - 86400 }],
      passports: [{ id: 1, plastic: 'Elegoo PLA Matte', plastic_id: 11, printer: 'Elegoo CC2',
        nozzle: '0.4', status: 'calibrated', ago: 'сегодня',
        values: { temp: 215, flow: 1.01, pa: 0.03, mvs: 13, retract: 0.5 },
        source: { temp: 'own', flow: 'own', pa: 'own', mvs: 'own', retract: 'own' },
        presets: { filament: 'Elegoo PLA Matte - Откалиброван.json' } }],
      presets: [], presets_disk: [], samples: [], journal: [], tests: [], diagnostics: [],
      verdict_kinds: [], passport_fields: [], plastics: [], printers: [],
      nozzles: ['0.4', '0.6'], plates: [] })
      : getJSON('/api/admin/calib'); },
    adminCalibRun: function (id) { return MOCK ? Promise.resolve({ ok: true, run: {}, steps: [], journal: [], measures: [], samples: [] }) : getJSON('/api/admin/calib/run', { id: id }); },
    adminCalibRunSave: function (body) { return MOCK ? Promise.resolve({ ok: true, id: 1 }) : postJSON('/api/admin/calib/run_save', body); },
    adminCalibRunDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calib/run_delete', { id: id }); },
    adminCalibStepSave: function (body) { return MOCK ? Promise.resolve({ ok: true, step: {} }) : postJSON('/api/admin/calib/step_save', body); },
    adminCalibStepApply: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calib/step_apply', body); },
    adminCalibMeasure: function (body) { return MOCK ? Promise.resolve({ ok: true, result: { verdict: 'недоэкструзия', value: 11.7, detail: 'демо' } }) : postJSON('/api/admin/calib/measure_add', body); },
    adminCalibMeasureDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calib/measure_delete', { id: id }); },
    adminCalibSampleSave: function (body) { return MOCK ? Promise.resolve({ ok: true, id: 1 }) : postJSON('/api/admin/calib/sample_save', body); },
    adminCalibSampleDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calib/sample_delete', { id: id }); },
    adminCalibPresets: function () { return MOCK ? Promise.resolve({ ok: true, items: [], user_dir: '' }) : getJSON('/api/admin/calib/presets'); },
    adminCalibSnapshot: function () { return MOCK ? Promise.resolve({ ok: true, new: 0, changed: 0, gone: 0, files: 0 }) : postJSON('/api/admin/calib/presets_snapshot', {}); },
    adminCalibPresetHistory: function (path) { return MOCK ? Promise.resolve({ ok: true, items: [] }) : getJSON('/api/admin/calib/preset_history', { path: path }); },
    adminCalibPresetRestore: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calib/preset_restore', { id: id }); },
    adminCalibPassportSave: function (body) { return MOCK ? Promise.resolve({ ok: true, id: 1 }) : postJSON('/api/admin/calib/passport_save', body); },
    adminCalibPassportApply: function (body) { return MOCK ? Promise.resolve({ ok: true, applied: [], skipped: [] }) : postJSON('/api/admin/calib/passport_apply', body); },
    adminCalibPassportDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calib/passport_delete', { id: id }); },
    // Пластины стола и их Z-offset (1028ab67): action = save | delete | default
    adminCalibPlateSave: function (body) { return MOCK ? Promise.resolve({ ok: true, id: 1 }) : postJSON('/api/admin/calib/plate_save', body); },
    adminCalibPhoto: function (body) { return MOCK ? Promise.resolve({ ok: true, bands: [], window: {} }) : postJSON('/api/admin/calib/photo', body); },
    adminCalibDrift: function () { return MOCK ? Promise.resolve({ ok: true, cube_x: [], cube_y: [], time_dev: [] }) : getJSON('/api/admin/calib/drift'); },
    adminCalibBackfill: function (attach) { return MOCK ? Promise.resolve({ ok: true, scanned: 0, changed: 0, calib: 0, by_type: {} }) : postJSON('/api/admin/calib/backfill', { attach: attach ? 1 : 0 }); },
    adminCalibLink: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calib/link', body); },
    // PDF отдаются обычным GET-файлом (как CSV клиентов) — не методом API, а ссылкой
    adminCalibExportUrl: function (id) { return MOCK ? '' : ('/api/admin/calib/export?id=' + id + '&initData=' + encodeURIComponent(INIT)); },
    adminCalibLabelsUrl: function (runId) { return MOCK ? '' : ('/api/admin/calib/labels?run=' + (runId || '') + '&initData=' + encodeURIComponent(INIT)); },
    familyRequest: function (partner) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/family_request', { partner: partner || '' }); },
    familyPickPartner: function () { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/family_pick_partner', {}); },   // нативный пикер (0528ba47)
    // Очередь разбора (OpenTodo 26196ac1): нерабочая ссылка → мастеру; админ список/закрытие.
    calcQueue: function (url, errText) { return MOCK ? Promise.resolve({ ok: true, id: 1 }) : postJSON('/api/calc_queue', { url: url, error: errText || '' }); },
    adminCalcQueue: function (all) { return MOCK ? Promise.resolve({ ok: true, items: [
      { id: 2, user: '@demo_client', user_id: 42, url: 'https://makerworld.com/x/broken', error: 'MakerWorld требует капчу', status: 'new', ago: '2 ч назад' },
      { id: 1, user: 'Аня', user_id: 43, url: 'https://example.com/model.stl', error: 'Не удалось скачать', status: 'new', ago: 'вчера' },
    ] }) : getJSON('/api/admin/calc_queue', all ? { all: 1 } : undefined); },
    adminCalcQueueResolve: function (id, action, note) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/calc_queue_resolve', { id: id, action: action || '', note: note || '' }); },
    // Сводка по столам мультиплейта (eb4424d9 + d7518198): габариты и признак многоцвета.
    platesInfo: function (token) { return MOCK ? Promise.resolve({ ok: true, plates: [
      { idx: 0, dims: '93×51×28', mc: true }, { idx: 1, dims: '93×51×28', mc: false }, { idx: 2, dims: '120×80×40', mc: false },
    ] }) : getJSON('/api/plates/' + token); },

    // options (B4-инстант): актуальный выбор услуг постобработки — их переключают локально на
    // смете без переслайса, сервер переприложит присланный набор к цене при оформлении.
    order: function (token, comment, options) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/order', { token: token, comment: comment || '', options: options || [] }); },
    // Заказ без сметы (OpenTodo 55a1cf8f): слайс не удался — оформляем заявку, цену назначит мастер.
    orderUnsliced: function (token, comment) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/order_unsliced', { token: token, comment: comment || '' }); },
    // Заказ ВО ВРЕМЯ слайса (OpenTodo f1e80d67): заявка создаётся мгновенно из живого выбора (sel),
    // цену проставит идущий фоном расчёт (или очередь досчёта, если он не доживёт). sel — ВЕСЬ выбор
    // расчёта: {printer_id, plastic_id, quality_id, strength_id, qty, color, multicolor, colors_sel,
    // urgency, scale, promo, options, plates, plate_colors, plate_plastics}.
    orderNow: function (token, sel, comment) { return MOCK ? Promise.resolve({ ok: true, order_id: 777 }) : postJSON('/api/order_now', Object.assign({ token: token, comment: comment || '' }, sel || {})); },
    // поиск по всему каталогу (подстрока+семантика, отдаёт готовые items); в мок-режиме
    // ok:false → фронт остаётся на локальной подстроке по мок-коллекции
    collectionSearch: function (q) { return MOCK ? Promise.resolve({ ok: false }) : getJSON('/api/collection_search', { q: q }); },
    // Заказ-корзина (B5): + доставка (способ/адрес/тип) и общий комментарий к заказу.
    // Стоимость доставки СЧИТАЕТ СЕРВЕР по типу (A2) — сюда уходит только его id.
    orderCart: function (tokens, batch, extra, optsMap) {
      extra = extra || {};
      if (MOCK) return Promise.resolve({ ok: true, count: tokens.length, group: 'mockgroup01' });
      return postJSON('/api/order_cart', { tokens: tokens, batch: !!batch,
        delivery_method: extra.delivery_method || '', delivery_address: extra.delivery_address || '',
        delivery_type_id: extra.delivery_type_id || 0,
        comment: extra.comment || '',
        opts: optsMap || {} });   // B4-инстант: карта token→[id услуг], локальный выбор на смете
    },
    // Тарифы доставки для оформления (A2): total — сумма позиций, по ней считается порог
    // «бесплатно от …», поэтому клиент видит цену доставки именно для своего заказа.
    deliveryTypes: function (total) {
      if (MOCK) return Promise.resolve({ ok: true, enabled: true, types: [
        { id: 1, name: 'Курьером по городу', price: 5, free_from: 50, eta: '1–2 дня', cost: (total >= 50 ? 0 : 5), free: total >= 50 },
        { id: 2, name: 'Почтой по области', price: 12, free_from: 0, eta: '2–4 дня', cost: 12, free: false }] });
      return getJSON('/api/delivery_types', { total: total || 0 });
    },
    // Серверная копия корзины для нуджа о брошенной корзине (idea #10).
    cartSync: function (items, total) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/cart_sync', { items: items, total: total || '' }); },
    customOrder: function (text, files) {
      if (MOCK) return Promise.resolve({ ok: true, id: 99 });
      var fd = new FormData(); fd.append('initData', INIT); fd.append('text', text || '');
      (files || []).forEach(function (f) { fd.append('file', f); });
      return fetch('/api/custom_order', { method: 'POST', body: fd }).then(function (r) { return r.json(); });
    },
    pdf: function (token) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/pdf', { token: token }); },
    orderPdf: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/order/pdf', { id: id }); },

    orders: function () { return MOCK ? Promise.resolve(M.orders) : getJSON('/api/orders'); },
    // expected — сумма (копейки), которую клиент видел: сервер отвечает stale, если цену успели поменять
    orderRespond: function (id, accept, expected) { var b = { order_id: id, accept: accept }; if (expected != null) b.expected = expected; return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/order/respond', b); },
    // Онлайн-оплата (A1): ссылка-счёт Telegram Payments — открывается через WebApp.openInvoice
    orderPayLink: function (id) { return MOCK ? Promise.resolve({ ok: false, error: 'Оплата недоступна в предпросмотре' }) : postJSON('/api/order/pay_link', { order_id: id }); },
    // Выставить счёт по обычному заказу — этап «Ждёт оплаты» (фидбэк 31.07). price=null — по текущей цене
    adminOrderBill: function (id, price) { return MOCK ? Promise.resolve({ ok: true, status: 'awaiting_payment' }) : postJSON('/api/admin/order/bill', { id: id, price: price }); },
    orderCancel: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/order/cancel', { order_id: id }); },
    orderEdit: function (id, p) { return MOCK ? Promise.resolve({ ok: true, price: '12,30 Br' }) : postJSON('/api/order/edit', { order_id: id, plastic_id: p.plastic_id, quality_id: p.quality_id, strength_id: p.strength_id, qty: p.qty, color: p.color || '' }); },
    profile: function (opts) { return MOCK ? Promise.resolve({ ok: true, currency: 'Br', avatar_url: '', avatar_prompt: '', balance: 3.4, would_spend: null, prefs: { price_mode: 'ask', price_pct: 0 }, balance_tx: [{ id: 3, amount: -6.6, reason: 'Оплата заказа #13', at: 1751400000, comment: '' }, { id: 2, amount: 10, reason: 'Пополнение мастером', at: 1750800000, comment: 'принёс наличными' }], family: { id: 1, title: '', balance: 45.6, members: [{ user_id: 111111111, user_name: '@maria_demo', joined_at: 1751400000 }, { user_id: 222222222, user_name: '@alex_demo', joined_at: 1751400000 }], tx: [{ id: 3, user_id: 111111111, user_name: '@maria_demo', request_id: 62, amount: -7.2, reason: 'Оплата заказа #62', at: 1751400000, comment: '' }, { id: 2, user_id: null, user_name: '', request_id: null, amount: 5, reason: 'Бонус за открытие семейного счёта', at: 1751300000, comment: '' }, { id: 1, user_id: 222222222, user_name: '@alex_demo', request_id: null, amount: 40.6, reason: 'Перенос личного баланса', at: 1751300000, comment: '' }] }, xp: 410, xp_floor: 360, xp_next: 750, level: 3, level_title: 'Энтузиаст', next_title: 'Мастер печати', to_next_xp: 340, progress_pct: 15, next_perk: 'Скидка 5% на печать', referral_link: 'https://t.me/printstudio_bot?start=ref_482931', referral_reward: '300 Br', family_bonus: '5 Br', unseen: 1, xp_log: [{ reason: 'Заказ #146 напечатан', amount: 50, at: 'вчера' }, { reason: 'Отзыв о заказе', amount: 20, at: '3 дня назад' }, { reason: 'Новый материал: PETG', amount: 15, at: 'неделю назад' }], unlocked: 5, total: 9, stats: { orders: 7, printed: 4, spent: 92, reviews: 1, materials: 3, referrals: 0 }, achievements: [
      { key: 'first_order', title: 'Первый заказ', desc: 'Оформить первый заказ', icon: 'sparkle', unlocked: true, have: 7, need: 1 },
      { key: 'orders_5', title: 'Постоянный клиент', desc: '5 заказов', icon: 'receipt', unlocked: true, have: 7, need: 5 },
      { key: 'orders_20', title: 'Завсегдатай', desc: '20 заказов', icon: 'star', unlocked: false, have: 7, need: 20 },
      { key: 'printed_10', title: 'Десятка в пластике', desc: '10 напечатанных заказов', icon: 'check', unlocked: false, have: 4, need: 10 },
      { key: 'materials_3', title: 'Материаловед', desc: 'Попробовать 3 пластика', icon: 'spool', unlocked: true, is_new: true, have: 3, need: 3 },
      { key: 'reviewer', title: 'Критик', desc: 'Оставить отзыв', icon: 'chat', unlocked: true, have: 1, need: 1 },
      { key: 'referral', title: 'Амбассадор', desc: 'Пригласить друга', icon: 'gift', unlocked: false, have: 0, need: 1 },
      { key: 'big_spender', title: 'Меценат', desc: 'Заказов на 100', icon: 'coin', unlocked: false, have: 92, need: 100 },
      { key: 'family', title: 'Семейный счёт', desc: 'Открыть общий счёт с близкими', icon: 'heart', unlocked: true, have: 1, need: 1 }] })
      : getJSON('/api/profile', opts && opts.seen ? { seen: 1 } : undefined); },
    // Сгенерировать аватарку профиля локально (OpenTodo fca4246e).
    avatarGenerate: function () { return MOCK ? Promise.resolve({ ok: true, url: '', prompt: 'бешеный кальмар в шапочке из фольги' }) : postJSON('/api/avatar/generate', {}); },
    orderMessages: function (id) { return MOCK ? Promise.resolve(M.messages) : getJSON('/api/order/messages', { id: id }); },
    // image — dataURL вложения (2026-07-19): фото ходят в обе стороны, как и у мастера.
    orderMessage: function (id, text, image) {
      if (MOCK) { M.messages.messages.push({ text: text, mine: true, at: 'сейчас' }); return Promise.resolve(M.messages); }
      return postJSON('/api/order/message', { order_id: id, text: text, image: image || '' });
    },
    sendIdea: function (text) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/idea', { text: text }); },

    // Ошибка JS мини-аппа → телеметрия (f4de8627). В превью — no-op: жаловаться некому.
    clientError: function (text) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/jserr', { text: String(text || '').slice(0, 500) }); },

    // Сердечко «Избранное» (id модели каталога, on — желаемое состояние). Мок хранит набор
    // в M.options.fav_ids — раздел __fav__ и залитые сердечки живут и в статик-превью.
    favorite: function (id, on) {
      if (MOCK) {
        var f = M.options.fav_ids = (M.options.fav_ids || []).filter(function (x) { return x !== id; });
        if (on) f.unshift(id);
        return Promise.resolve({ ok: true, fav: !!on });
      }
      return postJSON('/api/favorite', { id: id, on: on ? 1 : 0 });
    },
    // Личные настройки клиента (0d11db78): режим подтверждения цены. Сервер нормализует
    // значения и возвращает применённые — фронт показывает именно их, а не свои хотелки.
    savePrefs: function (mode, pct) {
      if (MOCK) return Promise.resolve({ ok: true, prefs: { price_mode: mode, price_pct: Number(pct) || 0 } });
      return postJSON('/api/prefs', { price_mode: mode, price_pct: Number(pct) || 0 });
    },
    // params: {category, offset, limit} — страница раздела каталога (c9c42789); без — топ + разделы;
    // category='__fav__' — виртуальный раздел «Избранное» текущего юзера
    collection: function (params) {
      if (MOCK) {
        var p = params || {};
        if (!p.category) return Promise.resolve(M.collection);
        var its = p.category === '__fav__'
          ? (M.options.fav_ids || []).map(function (id) {
              return M.collection.items.filter(function (m) { return m.id === id; })[0];
            }).filter(Boolean)
          : M.collection.items.filter(function (m) { return m.category === p.category; });
        return Promise.resolve({ ok: true, currency: 'руб.', items: its, next_offset: null });
      }
      return getJSON('/api/collection', params || {});
    },
    mymodels: function () { return MOCK ? Promise.resolve(M.mymodels) : getJSON('/api/mymodels'); },
    // лента рекомендаций на главной (df0c9674): {offset} → {items, next_offset}
    recommend: function (params) {
      if (MOCK) {
        var off = (params && params.offset) || 0;
        return Promise.resolve({ ok: true, currency: 'руб.',
          items: off ? [] : M.collection.items, next_offset: off ? null : 6 });
      }
      return getJSON('/api/recommend', params || {});
    },
    // масонри-лента главной F4: {cursor, limit} → {items:[{id,title,preview,price_from,
    // time_min,…}], next}. В моке — карточки каталога с выдуманным временем печати.
    feed: function (params) {
      if (MOCK) {
        var cur = (params && params.cursor) || 0;
        var mins = [130, 250, 170, 45, 700, 95];
        var its = cur ? [] : M.collection.items.map(function (m, i) {
          return Object.assign({}, m, { title: m.label,
            price_from: m.price || 0, time_min: mins[i % mins.length] });
        });
        // в превью карточка конфигуратора видна всегда (в проде — через раз, решает сервер)
        if (!cur && its.length) its.splice(2, 0, { kind: 'gridfinity', id: 'gf', title: 'Органайзер в ящик', label: 'Органайзер в ящик' });
        return Promise.resolve({ ok: true, currency: 'руб.', next: cur ? null : 6, items: its });
      }
      return getJSON('/api/feed', params || {});
    },
    // ── Gridfinity-конфигуратор (design_handoff_gridfinity) ──
    // Черновик раскладки живёт на сервере (замена localStorage прототипа); в превью
    // без бэка движок сам откатывается на localStorage — мок отвечает «черновика нет».
    gridfinityConfig: function () {
      return MOCK ? Promise.resolve({ ok: true, config: null }) : getJSON('/api/gridfinity/config');
    },
    gridfinitySave: function (config) {
      return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/gridfinity/config', { config: config });
    },
    // Заявка мастеру ЦЕЛИКОМ — старый путь. Новый клиент заказывает партиями (ниже).
    gridfinityOrder: function (config, comment) {
      return MOCK ? Promise.resolve({ ok: true, order_id: 1 })
        : postJSON('/api/gridfinity/order', { config: config, comment: comment || '' });
    },

    // ── Мастерская органайзеров: проекты, спецификация, партии ──
    // Проект = физический ящик клиента. Список отдаёт КОМПАКТНЫЙ план (preview), а не
    // раскладку: 20 проектов по 256 КБ в ленту карточек не влезут.
    gfxProjects: function (archived) {
      return MOCK ? Promise.resolve(MGFX.projects)
        : getJSON('/api/gridfinity/projects', archived ? { archived: 1 } : {});
    },
    gfxProjectAdd: function (body) {
      return MOCK ? Promise.resolve({ ok: true, id: 1 })
        : postJSON('/api/gridfinity/projects', body || {});
    },
    gfxProject: function (id) {
      return MOCK ? Promise.resolve(mgfxMoney()) : getJSON('/api/gridfinity/project/' + id);
    },
    gfxProjectSave: function (id, config) {
      return MOCK ? Promise.resolve({ ok: true, diff: {} })
        : postJSON('/api/gridfinity/project/' + id, { config: config });
    },
    gfxProjectPatch: function (id, body) {
      return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/gridfinity/project/' + id + '/patch', body || {});
    },
    gfxPartPatch: function (partId, body) {
      if (!MOCK) return postJSON('/api/gridfinity/part/' + partId, body || {});
      MGFX.project.parts.forEach(function (p) {
        if (p.id !== partId || p.live_state !== 'draft') return;
        if (body.name != null) { p.name = body.name || p.auto_name; p.custom_name = !!body.name; }
        if (body.note != null) p.note = body.note;
        if (body.qty) p.qty = body.qty;
        if (body.state) { p.state = p.live_state = body.state; p.state_label = body.state === 'hold' ? 'отложено' : 'черновик'; }
        if (body.sel != null) p.sel = body.sel ? 1 : 0;
      });
      return Promise.resolve(mgfxMoney());
    },
    // В превью выбор и смета считаются ЛОКАЛЬНО: без этого мок-режим показывал бы
    // мёртвые галочки и пустую партию, и пройти сценарий заказа глазами было бы нечем.
    gfxSelect: function (id, ids, sel) {
      if (!MOCK) return postJSON('/api/gridfinity/project/' + id + '/select', { ids: ids, sel: sel ? 1 : 0 });
      MGFX.project.parts.forEach(function (p) {
        if (ids.indexOf(p.id) >= 0 && p.live_state === 'draft') p.sel = sel ? 1 : 0;
      });
      return Promise.resolve(mgfxMoney());
    },
    gfxPreflight: function (id, ids) {
      if (!MOCK) return getJSON('/api/gridfinity/project/' + id + '/preflight', ids && ids.length ? { ids: ids.join(',') } : {});
      var sel = MGFX.project.parts.filter(function (p) { return ids.indexOf(p.id) >= 0; });
      return Promise.resolve({
        ok: true, parts: sel, blocked: 0,
        checks: sel.length ? [{ level: 'warn', part_id: 0,
          text: 'Бирюзы на складе 300 г из нужных 480 г — мастер предложит замену' }] : [],
        colors: [], total: {
          n: sel.length, qty: sel.reduce(function (a, p) { return a + p.qty; }, 0),
          price: sel.reduce(function (a, p) { return a + p.price; }, 0),
          grams: sel.reduce(function (a, p) { return a + p.grams; }, 0),
          minutes: sel.reduce(function (a, p) { return a + p.minutes; }, 0) },
      });
    },
    gfxOrder: function (id, ids, comment) {
      return MOCK ? Promise.resolve({ ok: true, order_id: 1, n: (ids || []).length })
        : postJSON('/api/gridfinity/project/' + id + '/order', { ids: ids, comment: comment || '' });
    },
    gfxMeasures: function (body) {
      return MOCK ? Promise.resolve({ ok: true, measures: [] })
        : postJSON('/api/gridfinity/measures', body || {});
    },
    gfxPresets: function () {
      return MOCK ? Promise.resolve({ ok: true, presets: [] }) : getJSON('/api/gridfinity/presets');
    },
    gfxPresetSave: function (title, mod) {
      return MOCK ? Promise.resolve({ ok: true, presets: [] })
        : postJSON('/api/gridfinity/presets', { title: title, mod: mod });
    },
    gfxPresetDelete: function (id) {
      return MOCK ? Promise.resolve({ ok: true, presets: [] })
        : postJSON('/api/gridfinity/presets', { delete: id });
    },
    gfxTemplateUrl: function (id) {
      return '/api/gridfinity/project/' + id + '/template?initData=' + encodeURIComponent(INIT)
        + (GFXKEY ? '&gfxkey=' + encodeURIComponent(GFXKEY) : '');
    },
  };

  // ---- admin mock ----
  var MA = {
    products: { ok: true, currency: 'руб.', products: [
      { id: 1, title: 'Ваза-спираль (готовая)', description: 'Напечатана шёлковым PLA, высота 18 см.', price: 25, stock: 3, in_stock: true, sold: 7, is_active: 1, photo: '../assets/models/m5.png' },
      { id: 2, title: 'Подставка под телефон', description: 'PETG, регулируемый угол.', price: 12.5, stock: 1, in_stock: true, sold: 21, is_active: 1, photo: '../assets/models/m1.png' },
      { id: 3, title: 'Брелок-дракон', description: 'Мультицвет, 4 см.', price: 6, stock: 0, in_stock: false, sold: 40, is_active: 1, photo: '../assets/models/m2.png' },
      { id: 4, title: 'Органайзер (черновик)', description: 'Ещё не в продаже.', price: 15, stock: 5, in_stock: true, sold: 0, is_active: 0, photo: '' } ] },
    dashboard: { ok: true, currency: 'руб.', stats: {
      calcs: 128, orders: 41, printed: 27, conv: 32.0, conv_print: 65.8,
      revenue_all: 612.40, revenue_printed: 408.20, avg_order: 14.94, grams: 3800,
      calcs_7d: 22, revenue_7d: 132.5, orders_7d: 9, orev_7d: 132.5,
      calcs_30d: 96, revenue_30d: 612.4, orders_30d: 41, orev_30d: 612.4,
      top_materials: [['Crown PLA шёлковый', 25], ['Elegoo PETG', 9], ['Elegoo PLA+', 5], ['Прочие', 2]],
      top_models: [['Бэйбизавр', 12], ['Card Shuffler', 7], ['Яйцо дракона', 5]],
      series14: [12, 28, 9, 0, 34, 41, 18, 22, 7, 0, 38, 52, 31, 44].map(function (rev, i) {
        return { revenue: rev, orders: [1, 2, 1, 0, 3, 3, 2, 2, 1, 0, 3, 4, 2, 3][i] }; }),
      status_mix: { new: 6, in_progress: 3, printed: 27, rejected: 5 },
    } },
    orders: { ok: true, currency: 'руб.', orders: [
      { id: 15, label: 'Бэйбизавр', user: '@demo_client', status: 'new', status_label: 'Новый', price: 3.10, qty: 1, created_at: 0, pending: null, unread: 0 },
      { id: 13, label: 'Устройство по перетасовке карт', user: '@demo_client', status: 'in_progress', status_label: 'В работе', price: 15.50, qty: 1, created_at: 0, pending: null, unread: 1 },
      { id: 12, label: 'Card Shuffler V2', user: '@anna_prints', status: 'new', status_label: 'Новый', price: 12.70, qty: 1, created_at: 0, pending: null, unread: 0 },
      { id: 11, label: 'Base à tiroir', user: '@anna_prints', status: 'rejected', status_label: 'Отменён', price: 10.20, qty: 1, created_at: 0, pending: null, unread: 0 },
      { id: 2, label: 'бэйбизавр.3mf', user: '@demo_client', status: 'printed', status_label: 'Напечатано', price: 4.00, qty: 1, created_at: 0, pending: null, unread: 0 },
    ] },
    selfOrders: { ok: true, currency: 'руб.', orders: [
      { id: 61, label: 'Koala AMS', user: 'мастер', status: 'self', status_label: '🏠 Печать для себя', price: 3.0, qty: 1, created_at: 0, grams: 3.69, minutes: 16, plastic: 'Elegoo PLA Matte', preview: '' },
      { id: 58, label: 'Ваза для себя', user: 'мастер', status: 'self', status_label: '🏠 Печать для себя', price: 0.0, qty: 1, created_at: 0, grams: 0, minutes: 0, plastic: '', preview: '' },
      { id: 54, label: 'Kingroon PETG тест', user: 'мастер', status: 'self', status_label: '🏠 Печать для себя', price: 3.0, qty: 1, created_at: 0, grams: 4.2, minutes: 53, plastic: 'Kingroon PETG', preview: '' },
    ] },
    queue: { ok: true, currency: 'руб.', queue: [
      { id: 13, pos: 1, label: 'Устройство по перетасовке карт', user: '@demo_client', status: 'in_progress', status_label: 'В работе', qty: 1, minutes: 720, done_minutes: 720, eta: '04.06' },
      { id: 12, pos: 2, label: 'Card Shuffler V2', user: '@anna_prints', status: 'new', status_label: 'Новый', qty: 1, minutes: 600, done_minutes: 1320, eta: '04.06' },
      { id: 15, pos: 3, label: 'Бэйбизавр', user: '@demo_client', status: 'new', status_label: 'Новый', qty: 1, minutes: 100, done_minutes: 1420, eta: '04.06' },
    ] },
    status: { ok: true, status: { uptime: '2д 4ч 17м', disk_gb: 214.3, queue_count: 3, queue_minutes: 1420,
      slices_running: 0, slices_cap: 2, printers: 1, plastics: 2, turzx: { enabled: true },
      ai: { enabled: true, loaded: true, model: 'Tongyi-MAI/Z-Image-Turbo', model_label: 'Z-Image-Turbo',
        model_from_env: false, idle_min: 2.5, idle_unload_min: 5, off_by_flag: false, load_failed: false,
        models: [
          { id: 'Tongyi-MAI/Z-Image-Turbo', label: 'Z-Image-Turbo', desc: 'лучшее качество и следование промту · 1024² · ~19 с · ~7 ГБ VRAM', current: true },
          { id: 'stabilityai/sdxl-turbo', label: 'SDXL-Turbo', desc: 'лучше слушается промта, чем SD-Turbo · 512² · ~2 с · ~7 ГБ VRAM', current: false },
          { id: 'stabilityai/sd-turbo', label: 'SD-Turbo', desc: 'самая быстрая и лёгкая · 512² · ~0.3 с · ~3 ГБ VRAM', current: false },
        ] },
      gpu: { name: 'RTX 5070', vram_used_mb: 6400, vram_total_mb: 12288, util_pct: 12, temp_c: 47 },
      // лицензия в MOCK — правдоподобная действующая, иначе карточка «Лицензия» на демо
      // выглядит красной и вводит в заблуждение
      license: { state: 'ok', studio: 'Студия «Пример»', id: '001', edition: 'studio',
        plan: 'subscription', issued: '2026-08-01', expires: '2027-08-01', days_left: 344.0,
        expired: false, reason: '', source: 'env', key_present: true, enforcement: 'update',
        build_edition: 'studio', edition_mismatch: false },
      issues: [] } },
    printer: { ok: true, online: true, camera_url: '', summary: { extruder_temp: 248, extruder_target: 250, bed_temp: 70, bed_target: 70 },
      state: { print_stats: { filename: 'Устройство по перетасовке карт', state: 'printing' },
        virtual_sdcard: { progress: 0.40 }, info: { current_layer: 247, total_layer: 612 },
        chamber: 41, speed: 164, fan: 100, filament_used_g: 51, filament_total_g: 127,
        started_at: '02.06 · 08:12', eta_at: '02.06 · 20:27', elapsed: '4 ч 58 мин', remaining: '7 ч 17 мин',
        order_id: 13, plastic: 'Elegoo PETG' },
      // extras — как отдаёт elegoo.extras() (свет/камера/слоты CANVAS/вентиляторы/позиция)
      extras: { light: 1, chamber_temp: 41, filament_detected: 1, filament_sensor_on: 1,
        fans: { model: 100, aux: 30, box: 0 }, position: { x: 128.4, y: 120.1, z: 49.4 },
        homed_axes: 'xyz', u_disk: false, exceptions: [],
        slots: [
          { slot: 1, brand: 'ELEGOO', name: 'PLA Silk', type: 'PLA', color: '#A03BF7', temp_min: 190, temp_max: 230, status: 1 },
          { slot: 2, brand: 'ELEGOO', name: 'PLA Matte', type: 'PLA', color: '#09CC3A', temp_min: 190, temp_max: 230, status: 1 },
          { slot: 3, brand: 'ELEGOO', name: 'PETG', type: 'PETG', color: '#F88D36', temp_min: 230, temp_max: 260, status: 1 },
          { slot: 4, brand: 'ELEGOO', name: 'PLA Matte', type: 'PLA', color: '#D2C5A3', temp_min: 190, temp_max: 230, status: 1 },
        ], active_slot: 3, speed_mode: 1, auto_refill: false,
        machine: { status: 2, sub_status: 2075, label: 'Печать', detail: '', paused: false } },
      job: { order_id: 13, label: 'Устройство по перетасовке карт', plastic: 'Elegoo PETG', grams: 127.5, thumb: '' } },
    // Ответы прошивки сообщества CC2 (adminCommunity) — для демо и просмотра без принтера.
    community: {
      objects: { ok: true, current: 'Card_box_id_0_copy_1', excluded: ['Card_box_id_0_copy_3'], objects: [
        { name: 'Card_box_id_0_copy_0', center: [70, 70], polygon: [[40, 40], [100, 40], [100, 100], [40, 100]] },
        { name: 'Card_box_id_0_copy_1', center: [150, 70], polygon: [[120, 40], [180, 40], [180, 100], [120, 100]] },
        { name: 'Card_box_id_0_copy_2', center: [70, 160], polygon: [[40, 130], [100, 130], [100, 190], [40, 190]] },
        { name: 'Card_box_id_0_copy_3', center: [150, 160], polygon: [[120, 130], [180, 130], [180, 190], [120, 190]] },
      ] },
      mesh: { ok: true, active_name: 'default', active: { min: [10, 10], max: [246, 246], lo: -0.11, hi: 0.14, range: 0.25, mean: 0.01,
        matrix: [[0.14, 0.09, 0.05, 0.02, 0.0], [0.08, 0.04, 0.01, -0.02, -0.04], [0.03, 0.0, -0.02, -0.05, -0.07],
          [0.01, -0.02, -0.04, -0.07, -0.09], [0.0, -0.03, -0.06, -0.09, -0.11]] },
        profiles: { default: { side: 'A', min: [10, 10], max: [246, 246], lo: -0.11, hi: 0.14, range: 0.25, mean: 0.01,
          matrix: [[0.14, 0.09, 0.05, 0.02, 0.0], [0.08, 0.04, 0.01, -0.02, -0.04], [0.03, 0.0, -0.02, -0.05, -0.07],
            [0.01, -0.02, -0.04, -0.07, -0.09], [0.0, -0.03, -0.06, -0.09, -0.11]] } } },
      tuning: { ok: true, speed_percent: 100, flow_percent: 100, live_velocity: 142.5, zoffset: 0 },
      screws: { ok: true, busy: false, completed: true, success: true, plan: { spread_um: 70, peak_um: 49, peak_minimal_um: 35,
        from_front_left: [{ code: 'ПЛ', name: 'передний левый', um: 0, action: 'ок' }, { code: 'ПП', name: 'передний правый', um: 49, action: 'опустить' },
          { code: 'ЗП', name: 'задний правый', um: -21, action: 'поднять' }, { code: 'ЗЛ', name: 'задний левый', um: 9, action: 'ок' }],
        minimal: [{ code: 'ПЛ', name: 'передний левый', um: -14, action: 'ок' }, { code: 'ПП', name: 'передний правый', um: 35, action: 'опустить' },
          { code: 'ЗП', name: 'задний правый', um: -35, action: 'поднять' }, { code: 'ЗЛ', name: 'задний левый', um: -5, action: 'ок' }] } },
      console: { ok: true, busy: false, completed: true, success: true, command: 'G28', output: '// G28\nok\n' },
      health: { ok: true, uptime_s: 86400 * 2 + 3600 * 5, mem_total_kb: 120000, mem_available_kb: 52000, load1: 0.42, mqtt: true },
    },
    hub: {
      ok: true, currency: 'руб.',
      attention: { new_orders: 3, unread: 2, price_wait: 1 },
      revenue: { today: 4250, week: 18600, orders_week: 9, avg_order: 2180, orders_total: 47 },
      // набор ключей держим тем же, что отдаёт /api/admin/hub: недостающий счётчик
      // рисуется в плитке как «undefined», а не как прочерк
      counts: { queue: 5, plastics: 6, discounts: 2, presets: 8, shades: 24, collection: 14,
        products: 4, notifications: 2, family_requests: 1, tasks: 3, errors: 2 },
    },
  };
  function adminOrderDetail(id) {
    var o = MA.orders.orders.filter(function (x) { return x.id === id; })[0] || MA.orders.orders[0];
    return { ok: true, currency: 'руб.', order: {
      id: o.id, label: o.label, user: o.user, user_id: 1, status: o.status, status_label: o.status_label,
      price: o.price, pending_price: o.pending, price_comment: '', qty: o.qty, printer: 'Elegoo Centauri Carbon 2',
      plastic: 'Crown PLA шёлковый трёхцветный', quality: 'Стандарт', strength: 'Золотая середина',
      grams: 16.6, minutes: 100, dims: '60×88×23', due: o.status === 'in_progress' ? '10.06' : '', src_url: 'https://makerworld.com/en/models/88421-phone-stand-adjustable', file_path: 'job.3mf', promo: '', promo_ok: 0,
      rating: o.status === 'printed' ? 9 : 0,
      review: o.status === 'printed' ? 'Отличное качество! Всё чётко, вовремя — спасибо.' : '',
      done_url: o.status === 'printed' ? '../assets/models/photo_dino.jpg' : '',
      preview: '../assets/models/photo_dino.jpg',                 // картинка модели в карточке (aff57787)
      cogs_actual: o.status === 'printed' ? 1.85 : null,          // Фаза C: себестоимость по факту
      margin: o.status === 'printed' ? Math.round((o.price - 1.85) * 100) / 100 : null,
      created_at: 0, can_print_net: true, can_open_slicer: true,
      // печати заказа из журнала (per-печать медиа) — как в /api/admin/order/{id}
      prints: o.status === 'in_progress' ? [
        { jid: 41, timelapse: '/x1.mp4', photo: '../assets/models/photo_dino.jpg', label: 'plate_1.gcode', minutes: 148 },
        { jid: 42, timelapse: '/x2.mp4', photo: '../assets/models/m1.png', label: 'plate_2.gcode', minutes: 132 } ] : [],
      breakdown: { total: '3,10 руб.', grams_total: '16.6', time_total: '1 ч 40 мин', qty: o.qty, extra_colors: 2, purge: '2.3', rows: [
        { label: 'Филамент', sub: '16.6 г', value: '0,83 руб.' },
        { label: 'Машинное время', sub: '1 ч 40 мин', value: '0,90 руб.' },
        { label: 'Буфер на брак', sub: '8%', value: '0,14 руб.' },
        { label: 'Подготовка', sub: 'фикс. на заказ', value: '0,50 руб.' },
        { label: 'Наценка', sub: '40%', value: '0,73 руб.' } ] },
      messages: [{ text: 'Здравствуйте! Принял заказ.', mine: true, at: '02.06 11:31' }],
      history: [{ status: 'new', label: 'Новый', at: '02.06 11:30' }] } };
  }

  window.API = API;
  Object.assign(API, {
    adminDashboard: function () { return MOCK ? Promise.resolve(MA.dashboard) : getJSON('/api/admin/dashboard'); },
    adminHub: function () { return MOCK ? Promise.resolve(MA.hub) : getJSON('/api/admin/hub'); },
    adminOrders: function (q) { return MOCK ? Promise.resolve(MA.orders) : getJSON('/api/admin/orders', q ? { q: q } : undefined); },
    adminOrdersCsvUrl: function () { return MOCK ? '' : ('/api/admin/orders/export.csv?initData=' + encodeURIComponent(INIT)); },
    adminOrder: function (id) { return MOCK ? Promise.resolve(adminOrderDetail(id)) : getJSON('/api/admin/order/' + id); },
    // Раскладка Gridfinity в карточке заявки: снимок конфигурации + печать-лист деталей.
    // 404 — обычная заявка (снимка нет), карточка просто не рисует блок.
    adminGridfinity: function (rid) {
      return MOCK ? Promise.resolve({ ok: false }) : getJSON('/api/admin/gridfinity/' + rid);
    },
    adminGfxPartPrinted: function (partId, printed) {
      return MOCK ? Promise.resolve({ ok: true })
        : postJSON('/api/admin/gridfinity/part/' + partId, { printed: !!printed });
    },
    adminGfxRelease: function (rid) {
      return MOCK ? Promise.resolve({ ok: true, released: 0 })
        : postJSON('/api/admin/gridfinity/release/' + rid, {});
    },
    // URL меша заказа (STL) для 3D-вьюера в карточке мастера (B1). Возвращает СТРОКУ-URL (не Promise):
    // three.js-STLLoader сам делает XHR по нему, поэтому кладём initData/admintoken в query (сервер
    // сверит права админа). MOCK → '' → вьюер молча остаётся на PNG-превью заказа.
    adminOrderFileUrl: function (id) {
      if (MOCK || !id) return '';
      return '/api/admin/orderfile?rid=' + encodeURIComponent(id) + '&initData=' + encodeURIComponent(INIT);
    },
    adminOrderStatus: function (id, status, due, actual, reason) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/order/status', { id: id, status: status, due: due || '', actual_minutes: actual || '', reason: reason || '' }); },
    // Галочка «оплачен» (рефералка 7f257a33): первый напечатанный+оплаченный заказ друга → бонус обоим.
    adminOrderPaid: function (id, paid) { return MOCK ? Promise.resolve({ ok: true, paid: paid ? 1 : 0 }) : postJSON('/api/admin/order/paid', { id: id, paid: !!paid }); },
    // Смена пластика/цвета заказа мастером (e132724b): colors — массив имён (мультицвет — по слоту)
    adminOrderMaterial: function (id, plasticId, colors) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/order/material', { id: id, plastic_id: plasticId, colors: colors || [] }); },
    adminOrderPrice: function (id, price, comment) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/order/price', { id: id, price: price, comment: comment }); },
    // OpenTodo 8c1cab08: тот же эндпоинт цены, но пересчёт по граммам/минутам вместо ручной цены.
    adminOrderPriceByGt: function (id, grams, minutes, comment) { return MOCK ? Promise.resolve({ ok: true, applied: 'direct', price: 14.1 }) : postJSON('/api/admin/order/price', { id: id, grams: grams, minutes: minutes, comment: comment }); },
    adminOrderRecalc: function (id) { return MOCK ? Promise.resolve({ ok: true, old: { price: 18.9, grams: 136, minutes: 938 }, new: { price: 14.1, grams: 133.4, minutes: 453 } }) : postJSON('/api/admin/order/recalc', { id: id }); },
    adminOrderRecalcApply: function (id, grams, minutes, dry) { return MOCK ? Promise.resolve({ ok: true, price: 14.1, applied: 'direct' }) : postJSON('/api/admin/order/recalc_apply', { id: id, grams: grams, minutes: minutes, dry: !!dry }); },
    // Счёт-расчёт по индивидуальному заказу (OpenTodo 6aaa6a19): body = {plastic_id, qty, grams, minutes, dev_fee, shot?, comment?}.
    adminOrderInvoice: function (id, body, dry) { return MOCK ? Promise.resolve({ ok: true, price: 21.5, print_price: 18.5, dev_fee: Number(body && body.dev_fee) || 0 }) : postJSON('/api/admin/order/invoice', Object.assign({ id: id, dry: !!dry }, body || {})); },
    // Мастер закидывает модель по ссылке (OpenTodo e78bf8f2): скачиваю + слайсю, возвращаю готовые итог. граммы/время.
    adminOrderSlice: function (id, url, body) { return MOCK ? Promise.resolve({ ok: true, grams: 42.5, minutes: 185, dims: '60×88×23 мм', label: 'Из ссылки' }) : postJSON('/api/admin/order/slice', Object.assign({ id: id, url: url }, body || {})); },
    // Зачислить/списать баланс клиента (OpenTodo 8c428c43): amount +/-, reason — назначение,
    // comment — заметка мастера к транзакции (ccb1e4b6, клиент видит её в истории).
    // requestId — номер заказа (кнопка «Оплата заказа #N»): у семейного счёта попадает в историю
    adminBalance: function (userId, amount, reason, comment, requestId) { return MOCK ? Promise.resolve({ ok: true, balance: 3.4 }) : postJSON('/api/admin/balance', { user_id: userId, amount: amount, reason: reason || '', comment: comment || '', request_id: requestId || 0 }); },
    // Комментарий к уже совершённой транзакции (ccb1e4b6); пустой текст стирает заметку.
    adminBalanceComment: function (txId, comment) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/balance_comment', { tx_id: txId, comment: comment || '' }); },
    // Оформить заказ за клиента (OpenTodo cd3258d6): body = {user_id, printer_id?, plastic_id, quality_id, strength_id, qty, grams, minutes, price?, label?, comment?}. dry=true — предпросмотр суммы.
    adminOrderCreate: function (body, dry) { return MOCK ? Promise.resolve({ ok: true, order_id: 901, price: Number(body && body.price) || 12.5, calc_price: 12.5 }) : postJSON('/api/admin/order/create', Object.assign({ dry: !!dry }, body || {})); },
    // image — dataURL вложения (2026-07-19); текст при этом может быть пустым.
    adminOrderMessage: function (id, text, image) {
      if (MOCK) { return Promise.resolve({ ok: true, messages: [{ text: text, mine: true, at: 'сейчас' }] }); }
      return postJSON('/api/admin/order/message', { id: id, text: text, image: image || '' });
    },
    adminOrderPrintNet: function (id, start) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/order/print_net', { id: id, start: start }); },
    adminOrderOpenSlicer: function (id) { return MOCK ? Promise.resolve({ ok: true, note: 'Открываю в ElegooSlicer на ПК бота' }) : postJSON('/api/admin/order/open_slicer', { id: id }); },
    adminOrderOpenFolder: function (id) { return MOCK ? Promise.resolve({ ok: true, note: 'Открываю папку на ПК бота' }) : postJSON('/api/admin/order/open_folder', { id: id }); },
    adminOrderDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/order/delete', { id: id }); },
    adminOrdersCleanTest: function () { return MOCK ? Promise.resolve({ ok: true, deleted: 3 }) : postJSON('/api/admin/orders/clean_test', {}); },
    adminOrdersMarkAllSeen: function () { return MOCK ? Promise.resolve({ ok: true, marked: 2 }) : postJSON('/api/admin/orders/mark_all_seen', {}); },
    adminQueue: function () { return MOCK ? Promise.resolve(MA.queue) : getJSON('/api/admin/queue'); },
    adminQueueMove: function (id, dir) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/queue/move', { id: id, dir: dir }); },
    adminPrinterStatus: function () { return MOCK ? Promise.resolve(MA.printer) : getJSON('/api/admin/printer_status'); },
    // storage: 'local' (по умолчанию) | 'u-disk' — флешка принтера.
    adminPrinterFiles: function (storage) { return MOCK ? Promise.resolve({ ok: true, files: storage === 'u-disk' ? [
      { name: 'С флешки.gcode', size: 800000, print_time: 3600, created: 1784400000,
        colors: [{ hex: '#FFFFFF', type: 'PLA', extruder: 0 }] },
    ] : [
      { name: 'Устройство по перетасовке карт.gcode', size: 4823910, print_time: 44100, created: 1784503283,
        colors: [{ hex: '#E4BD68', type: 'PLA', extruder: 0 }, { hex: '#3F8E43', type: 'PLA', extruder: 1 }, { hex: '#000000', type: 'PETG', extruder: 2 }] },
      { name: 'Бэйбизавр.gcode', size: 1180044, print_time: 6000, created: 1784488726,
        colors: [{ hex: '#A03BF7', type: 'PLA', extruder: 0 }] },
      { name: 'Яйцо дракона.gcode', size: 2204110, print_time: 13800, created: 1784300000 },
    ] }) : getJSON('/api/admin/printer_files', storage && storage !== 'local' ? { storage: storage } : null); },
    // opts: {slot_map:[{t,tray_id,canvas_id}], plate:'A'|'B', bedlevel, storage} — диалог старта.
    adminPrinterPrint: function (filename, opts) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_print', Object.assign({ filename: filename }, opts || {})); },
    adminPrinterFileInfo: function (name, storage) { return MOCK ? Promise.resolve({ ok: true, file: { name: name, colors: [] } }) : getJSON('/api/admin/printer_file_info', { name: name, storage: storage || 'local' }); },
    // Стоковые команды CC2 из SDK вендора: режим скорости 0..3 (1031), автодозаправка (2004).
    adminPrinterSpeed: function (mode) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_speed', { mode: mode }); },
    adminPrinterRefill: function (on) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_refill', { on: !!on }); },
    // what: 'heaters' | 'fans' | 'all' | 'soft_stop' (прервать печать и всё выключить).
    adminPrinterOff: function (what) { return MOCK ? Promise.resolve({ ok: true, done: ['нагрев выключен'] }) : postJSON('/api/admin/printer_off', { what: what }); },
    adminHeatPresets: function () { return MOCK ? Promise.resolve({ ok: true, presets: [
      { name: 'PLA', nozzle: 210, bed: 60 }, { name: 'PETG', nozzle: 240, bed: 80 }, { name: 'ABS', nozzle: 250, bed: 100 },
    ] }) : getJSON('/api/admin/heat_presets'); },
    // presets = null → сбросить к значениям по умолчанию.
    adminHeatPresetsSave: function (presets) { return MOCK ? Promise.resolve({ ok: true, presets: presets || [] }) : postJSON('/api/admin/heat_presets', presets ? { presets: presets } : { reset: true }); },
    // Прошивка сообщества CC2 (app/cc2community.py): what = objects|mesh|console|health|tuning|screws|info.
    adminCommunity: function (what) { return MOCK ? Promise.resolve(MA.community && MA.community[what] || { ok: false, error: 'нет в демо' }) : getJSON('/api/admin/printer_community', { what: what }); },
    adminCommunityOp: function (op, args) { return MOCK ? Promise.resolve({ ok: true, session: 0 }) : postJSON('/api/admin/printer_community', Object.assign({ op: op }, args || {})); },
    // Управление текущей печатью: action 'pause'|'resume'|'stop'|'light'; on — для света (вкл/выкл).
    adminPrinterControl: function (action, on) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_control', { action: action, on: on !== false }); },
    adminPrinterUnload: function (slot) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_unload', { slot: slot }); },
    // Ручное управление (коды 1028/1030/1026/1027 разведаны 20.07): прогрев, вентилятор, home, jog.
    // Заливка .gcode в память принтера (multipart, как uploadFile). Бэкенд льёт файл на
    // принтер чанками по разведанному протоколу — см. docs/captures/upload-protocol.md.
    adminPrinterUpload: function (file) {
      if (MOCK) return Promise.resolve({ ok: true, name: file && file.name, size: file && file.size });
      var fd = new FormData(); fd.append('initData', INIT); fd.append('file', file);
      return fetch('/api/admin/printer_upload', { method: 'POST', body: fd }).then(function (r) { return r.json(); });
    },
    // opts: {names: [...], storage} — удалить несколько файлов одной командой (1047 берёт массив).
    adminPrinterDelete: function (name, opts) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_delete', Object.assign({ name: name }, opts || {})); },
    adminPrinterLoad: function (slot) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_load', { slot: slot }); },
    adminPrinterHeat: function (target, value) { return MOCK ? Promise.resolve({ ok: true, result: { error_code: 0 } }) : postJSON('/api/admin/printer_heat', { target: target, value: value }); },
    adminPrinterFan: function (which, pct) { return MOCK ? Promise.resolve({ ok: true, result: { error_code: 0 } }) : postJSON('/api/admin/printer_fan', { which: which, pct: pct }); },
    adminPrinterHome: function (axes) { return MOCK ? Promise.resolve({ ok: true, result: { error_code: 0 } }) : postJSON('/api/admin/printer_home', { axes: axes || 'xyz' }); },
    adminPrinterMove: function (axis, distance) { return MOCK ? Promise.resolve({ ok: true, result: { error_code: 0 } }) : postJSON('/api/admin/printer_move', { axis: axis, distance: distance }); },
    // Привязка текущей печати к заказу + подтверждение завершения (OpenTodo 70b5a590).
    adminPrinterBind: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_bind', { id: id }); },
    adminPrinterUnbind: function () { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer_unbind', {}); },
    // Допечатать без сбойной детали (Путь A, app/reprint.py): сначала спрашиваем детали текущего
    // задания, потом — по подтверждению мастера — стоп + переслайс + заливка + старт. printer_id
    // тащим ЯВНО: стоп и заливка обязаны уйти на ту машину, где идёт печать, а не на дефолтную.
    adminReprintParts: function (printerId) { return MOCK ? Promise.resolve({ ok: true, available: true, request_id: 136, label: '#136', job_name: 'plate_1.gcode', parts: [
      { index: 0, name: 'part0', part_id: 'keychain_a', volume_cm3: 3.1, dims_mm: [40, 18, 4] },
      { index: 1, name: 'part1', part_id: 'keychain_b', volume_cm3: 3.4, dims_mm: [42, 18, 4] },
      { index: 2, name: 'part2', part_id: 'keychain_c', volume_cm3: 3.2, dims_mm: [41, 18, 4] },
    ] }) : getJSON('/api/admin/reprint_parts', { printer_id: printerId || 0 }); },
    adminReprint: function (drop, expect, printerId) { return MOCK ? Promise.resolve({ ok: true, filename: 'o136_reprint.gcode', stopped: true, needs_start: true, kept: [], dropped: [], minutes: 90, grams: 24 }) : postJSON('/api/admin/reprint', { drop: drop || [], expect: expect || {}, printer_id: printerId || 0 }); },
    // Старт залитой допечатки — ОТДЕЛЬНОЕ действие: задание печатается с первого слоя и в новой
    // раскладке, поэтому между заливкой и стартом мастер обязан очистить стол. Автоматический
    // старт означал бы заход соплом в лежащий на столе пластик.
    adminReprintStart: function (filename, printerId) { return MOCK ? Promise.resolve({ ok: true, started: true, filename: filename }) : postJSON('/api/admin/reprint_start', { filename: filename || '', printer_id: printerId || 0 }); },
    // «Инструменты» → допечатка со слоя без сорвавшихся деталей (Путь В, app/web/tools.py):
    // источники (свежий g-code слайсера на ПК бота) → разбор деталей → сборка со сверкой →
    // скачать / залить → старт ОТДЕЛЬНОЙ кнопкой, когда стол готов.
    adminToolsResumeSources: function (printerId) { return MOCK ? Promise.resolve({ ok: true, sources: [], roots: [], current: null, home_clearance_mm: 4 }) : getJSON('/api/admin/tools/resume/sources', { printer_id: printerId || 0 }); },
    adminToolsResumeUpload: function (file) {
      if (MOCK) return Promise.resolve({ ok: true, path: 'mock.gcode', name: file && file.name });
      var fd = new FormData(); fd.append('initData', INIT); fd.append('file', file);
      return fetch('/api/admin/tools/resume/upload', { method: 'POST', body: fd }).then(function (r) { return r.json(); });
    },
    adminToolsResumeAnalyze: function (path, printerId) { return MOCK ? Promise.resolve({ ok: true, objects: [], layers: 0, layer_z: [], bed: [256, 256], separable: true, supported: true, warnings: [], suggest: null }) : postJSON('/api/admin/tools/resume/analyze', { path: path, printer_id: printerId || 0 }); },
    // сборка — задание: ответ сразу, ход и результат — adminToolsResumeJob (фаза, проценты, отчёт, превью)
    adminToolsResumeBuild: function (path, layer, exclude, expect, sourceName) { return MOCK ? Promise.resolve({ ok: true, job: 'mock', status: 'running' }) : postJSON('/api/admin/tools/resume/build', { path: path, layer: layer, exclude: exclude || [], expect: expect || {}, source_name: sourceName || '' }); },
    adminToolsResumeJob: function (job) { return MOCK ? Promise.resolve({ ok: true, job: job, status: 'done', report: {}, verify: { ok: true }, previews: [] }) : getJSON('/api/admin/tools/resume/job', { job: job }); },
    adminToolsResumeGates: function (job, printerId, forceCentre) { return MOCK ? Promise.resolve({ ok: true, gates: [], can_start: true }) : getJSON('/api/admin/tools/resume/gates', { job: job, printer_id: printerId || 0, force_centre: forceCentre ? 1 : 0 }); },
    adminToolsResumeSend: function (job, printerId) { return MOCK ? Promise.resolve({ ok: true, uploaded: 'resume.gcode', needs_start: true, gates: [] }) : postJSON('/api/admin/tools/resume/send', { job: job, printer_id: printerId || 0 }); },
    adminToolsResumeStart: function (job, printerId, opts) { opts = opts || {}; return MOCK ? Promise.resolve({ ok: true, started: true }) : postJSON('/api/admin/tools/resume/start', { job: job, printer_id: printerId || 0, force_centre: !!opts.forceCentre, ack_soft: !!opts.ackSoft }); },
    adminToolsResumeDownloadUrl: function (job) { return MOCK ? '' : ('/api/admin/tools/resume/download?job=' + encodeURIComponent(job) + '&initData=' + encodeURIComponent(INIT)); },
    // бинарные траектории для 3D-вьюера: исходник (path) или собранный файл (job)
    adminToolsResumeHistory: function () { return MOCK ? Promise.resolve({ ok: true, items: [] }) : getJSON('/api/admin/tools/resume/history', {}); },
    adminToolsResumePathsUrl: function (q) { if (MOCK) return ''; var p = q && q.job ? ('job=' + encodeURIComponent(q.job)) : ('path=' + encodeURIComponent((q && q.path) || '')); return '/api/admin/tools/resume/paths?' + p + '&initData=' + encodeURIComponent(INIT); },
    // Пакетный расчёт ссылок для клиента (OpenTodo 89251db6 + UX fcf355eb).
    adminBatchCalc: function (userId, links, userName, plasticId) { return MOCK ? Promise.resolve({ ok: true, bid: 'mockbid', count: 2 }) : postJSON('/api/admin/batch_calc', { user_id: userId, links: links, user_name: userName || '', plastic_id: plasticId || 0 }); },
    adminBatchStatus: function (bid) { return MOCK ? Promise.resolve({ ok: true, bid: bid, done: true, currency: 'Br', user_id: 42, links_raw: 'https://www.printables.com/model/1 https://makerworld.com/x/2', items: [
      { idx: 0, url: 'https://www.printables.com/model/1', status: 'ok', label: 'Подставка для телефона', price: 12.4, grams: 38, minutes: 150, plastic: 'Elegoo PLA', plates: 1, preview: '../assets/models/m8.png' },
      { idx: 1, url: 'https://makerworld.com/x/2', status: 'err', label: '', error: 'MakerWorld требует капчу' },
    ] }) : getJSON('/api/admin/batch_status', { bid: bid }); },
    adminBatchLast: function () { return MOCK ? Promise.resolve({ ok: true, bid: null }) : getJSON('/api/admin/batch_last'); },
    adminBatchCancel: function (bid) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/batch_cancel', { bid: bid }); },
    adminBatchSend: function (bid, include) { return MOCK ? Promise.resolve({ ok: true, sent: 1 }) : postJSON('/api/admin/batch_send', { bid: bid, include: include }); },
    // История печатей принтера + прикрепление выбранной печати (таймлапса) к заказу (c83a2a9e + 6b2eb433).
    adminPrinterHistory: function () { return MOCK ? Promise.resolve({ ok: true, jobs: [
      { name: 'Бэйбизавр.gcode', end_time: 1760000000, ago: '2 ч назад', has_timelapse: true },
      { name: 'Яйцо дракона.gcode', end_time: 1759990000, ago: '5 ч назад', has_timelapse: true },
      { name: 'Калибровочный куб.gcode', end_time: 1759900000, ago: 'вчера', has_timelapse: false },
    ] }) : getJSON('/api/admin/printer_history'); },
    adminAttachPrint: function (id, jobName, send) { return MOCK ? Promise.resolve({ ok: true, timelapse: '', journal_linked: true }) : postJSON('/api/admin/order/attach_print', { id: id, job_name: jobName, send: !!send }); },
    adminPrintConfirm: function (id, ok) { return MOCK ? Promise.resolve({ ok: true, confirmed: !!ok }) : postJSON('/api/admin/print_confirm', { id: id, ok: !!ok }); },
    adminStatus: function () { return MOCK ? Promise.resolve(MA.status) : getJSON('/api/admin/status'); },
    // Телеметрия ошибок (f4de8627): список сбоев + отметка «прочитано» (без id — все)
    adminErrors: function (onlyNew) { return MOCK ? Promise.resolve({ ok: true, new_count: 2, errors: [
      { id: 3, source: 'web', level: 'error', message: 'KeyError: \'plastic_id\'', context: '/api/calc',
        count: 4, first_at: '2026-08-02 09:12:40', last_at: '2026-08-02 11:03:07', seen: 0,
        traceback: 'Traceback (most recent call last):\n  File "app/webapp.py", line 4210, in _api_calc\n    pl = plastics[body["plastic_id"]]\nKeyError: \'plastic_id\'' },
      { id: 2, source: 'tg', level: 'error', message: 'TelegramBadRequest: message is not modified', context: 'callback_query uid 4471',
        count: 1, first_at: '2026-08-02 08:41:15', last_at: '2026-08-02 08:41:15', seen: 0,
        traceback: 'Traceback (most recent call last):\n  File "app/handlers/user_flow.py", line 902, in on_pick\n    await cb.message.edit_text(txt)\naiogram.exceptions.TelegramBadRequest: message is not modified' },
      { id: 1, source: 'log', level: 'error', message: 'Слайсер не ответил за 120 с', context: 'app.slicer',
        count: 12, first_at: '2026-08-01 22:10:03', last_at: '2026-08-02 07:55:31', seen: 1, traceback: '' },
    ] }) : getJSON('/api/admin/errors', onlyNew ? { new: 1 } : undefined); },
    adminErrorsSeen: function (id) { return MOCK ? Promise.resolve({ ok: true, marked: id ? 1 : 2 }) : postJSON('/api/admin/errors/seen', id ? { id: id } : {}); },
    // База знаний владельца студии (c356387a): статьи-инструкции для себя и операторов,
    // клиенту мастерской они не показываются.
    adminKb: function () { return MOCK ? Promise.resolve({ ok: true, articles: [
      { id: 1, title: 'Чем PLA отличается от PETG', body: 'PLA жёстче и печатается проще.\n\nPETG вязче и лучше держит удар.', sort: 1, published: 1, created_at: '2026-07-20 12:00:00', updated_at: '2026-08-01 10:20:00' },
      { id: 2, title: 'Почему деталь «слоится»', body: 'Чаще всего дело в температуре сопла.', sort: 2, published: 1, created_at: '2026-07-22 09:00:00', updated_at: '2026-07-22 09:00:00' },
      { id: 3, title: 'Черновик про сушку пластика', body: '', sort: 3, published: 0, created_at: '2026-08-02 08:00:00', updated_at: '2026-08-02 08:00:00' },
    ] }) : getJSON('/api/admin/kb'); },
    adminKbSave: function (body) { return MOCK ? Promise.resolve({ ok: true, id: body && body.id ? body.id : 4 }) : postJSON('/api/admin/kb/save', body || {}); },
    adminKbDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/kb/delete', { id: id }); },
    // автообновления кода (644367fb): проверить / применить / режим (off|notify|auto)
    adminUpdateCheck: function () { return MOCK ? Promise.resolve({ ok: true, update: { ok: true, branch: 'master', behind: 0, ahead: 0, commits: [], mode: 'notify' } }) : postJSON('/api/admin/update_check', {}); },
    adminUpdateApply: function () { return MOCK ? Promise.resolve({ ok: true, updated_to: 'abc1234', count: 1 }) : postJSON('/api/admin/update_apply', {}); },
    adminUpdateMode: function (mode) { return MOCK ? Promise.resolve({ ok: true, mode: mode }) : postJSON('/api/admin/update_mode', { mode: mode }); },
    // резервные копии БД (b557e341): состояние обоих контуров + снимок по кнопке
    adminBackupStatus: function () {
      if (!MOCK) return getJSON('/api/admin/backup_status');
      var now = Math.floor(Date.now() / 1000);
      var snaps = [0, 6, 12, 18].map(function (h, i) {
        return { name: 'bot_2026080' + (2 - Math.floor(i / 3)) + '_0' + (6 + i) + '0000.db',
          size: 41 * 1024 * 1024 + i * 9000, mtime: now - h * 3600 - 900 };
      });
      return Promise.resolve({ ok: true,
        last: { id: 42, ts: '2026-08-02 12:00:11', ok: 1, path: 'data\\backups\\' + snaps[0].name,
          size: snaps[0].size, duration_ms: 640, error: '' },
        runs: [{ id: 42, ts: '2026-08-02 12:00:11', ok: 1, path: snaps[0].name, size: snaps[0].size, duration_ms: 640, error: '' },
          { id: 41, ts: '2026-08-02 06:00:09', ok: 1, path: snaps[1].name, size: snaps[1].size, duration_ms: 590, error: '' }],
        local: { dir: 'C:\\...\\data\\backups', snapshots: snaps, manual: [
          { name: 'bot_pre_merge.db', size: 39 * 1024 * 1024, mtime: now - 9 * 86400 }],
          keep: 14, interval_hours: 6 },
        offsite: { dir: 'C:\\Bot backups', exists: true, count: 118, latest_mtime: now - 3600 },
        same_volume: false });
    },
    adminBackupNow: function () {
      return MOCK ? Promise.resolve({ ok: true, run: { ok: true, path: 'bot_20260802_143000.db',
        size: 41 * 1024 * 1024, duration_ms: 700, error: '' }, error: '' })
        : postJSON('/api/admin/backup_now', {});
    },
    adminScreenPower: function (on) { return MOCK ? Promise.resolve({ ok: true, turzx: { enabled: !!on } }) : postJSON('/api/admin/screen_power', { on: !!on }); },
    adminAiAction: function (b) { return MOCK ? Promise.resolve({ ok: true, ai: Object.assign({}, MA.status.status.ai, { loaded: false, model: (b && b.model) || MA.status.status.ai.model, idle_unload_min: (b && b.minutes != null) ? b.minutes : 5 }) }) : postJSON('/api/admin/ai', b); },
    // проба генерации по промту (22.07): в моке — оранжевый смайлик-заглушка с задержкой,
    // чтобы в превью было видно состояние «Генерирую…»
    adminAiTest: function (b) {
      if (!MOCK) return postJSON('/api/admin/ai_test', b);
      return new Promise(function (resolve) {
        setTimeout(function () {
          resolve({ ok: true, engine: 'z-image-turbo', seconds: 18.7, seed: 123456789,
            image: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAIAAABt+uBvAAAB20lEQVR42u2c223DMAxFFaL/XaAomv0yTvbrDNmgHwHSIohdS+JDJA7/Yijm1RHlByX69PX50bBtExAACEAAAhCAAAQgAGEAAhCAAAQgAAEIQNivvYUr+L687zc4X2+B8k4hKdd/oawDyxvQMJooTE6AVLiEkJK8dEzP7BRBDh2wDiUpQMfUlxSgY+pRatCx8ytl6Bh5l0p0LDRIMTrqSqQeHV09UpKOoirSHfaA1gwfLW1SmI6KQqYYgAIBrT+/5nUSQWaAsoTPpNqYVY1Hfuug7t72Le+yz1Pq7/5zp9u97bmLJbkGjY3hVuZY67iFZiIIQIkAbQW51nEiqNXf3XEf/OPPNb3ti2x/6e1n4FM7UwxAAAJQQkCx+wY9NRNBAALQooByXYaG1RJBloB6h+V8vc3H3cAZZpzK+kFuBHo5QI8Xzpm++b/WS0hcrP+vpruR/Ph4PsntXRebdNdSJMz+it6fMi+755wbUitF6NU9MLYOLgwv0r1qrJOKWrc55WIW3QVFixPGV/uE7/rQfUSSjEVunt4laSGgm19JV+Hm7FEyVgF6+vIo6jW9bFsPg6QeXocgpW6eLy9kBMS3Oxo5aQBhAAIQgAAEIAABCEAAwgAEIAABCEAp7Qd9IcUTlQzWvAAAAABJRU5ErkJggg==' });
        }, 1500);
      });
    },
    adminService: function () { return MOCK ? Promise.resolve({ ok: true, items: (MA.service || []), warn_pct: 85 }) : getJSON('/api/admin/service'); },
    adminServiceAction: function (b) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/service', b); },
    adminSelfPrint: function (b) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/self_print', b); },
    // Отдельный список «печать для себя»: заказы-самопечати мастера, исключённые из клиентских заказов.
    adminSelfOrders: function (q) { return MOCK ? Promise.resolve(MA.selfOrders) : getJSON('/api/admin/self_orders', q ? { q: q } : undefined); },
    cameraSrc: function (nonce) { return MOCK ? '' : ('/api/admin/camera?initData=' + encodeURIComponent(INIT) + (nonce ? '&r=' + nonce : '')); },
    // Превью файла на принтере из самого G-code (прошивка сообщества); m — время изменения файла,
    // ключ кэша: тот же файл бот к принтеру второй раз не спрашивает.
    printerThumbSrc: function (storage, path, m) { return MOCK ? '' : ('/api/admin/printer_thumb?initData=' + encodeURIComponent(INIT) + '&storage=' + encodeURIComponent(storage || 'local') + '&path=' + encodeURIComponent(path) + '&m=' + encodeURIComponent(m || '')); },
    cameraCheck: function () { return MOCK ? Promise.resolve({ ok: false, url: '(mock)', error: 'mock' }) : getJSON('/api/admin/camera_check').catch(function () { return { ok: false, error: 'эндпоинт недоступен — перезапустите бота' }; }); },

    adminPlastics: function () { return MOCK ? Promise.resolve({ ok: true, currency: 'руб.',
      material_types: ['PLA', 'PLA+', 'PETG', 'ABS', 'ASA', 'TPU', 'PC'],
      plastic_categories: ['Матовый декор', 'Обычный', 'Инженерный', 'Шелковый'],
      plastics: [
        { id: 1, name: 'Crown PLA шёлковый трёхцветный', price: 50, density: 1.24, material: 'PLA', description: 'Переливы', colors: ['Зелёный', 'Синий', 'Красный'], is_active: true, is_default: true, stock: 820, low: 200, uses: 25, photo: '../assets/filament/f1.jpg', color_css: { 'Зелёный': '#2fae57', 'Синий': '#2f6fd6', 'Красный': '#d23b3b' }, color_stock: { 'Зелёный': { stock: 740, low: 200 }, 'Синий': { stock: 280, low: 200 }, 'Красный': { stock: 90, low: 200 } } },
        { id: 2, name: 'Elegoo PETG', price: 45, density: 1.27, material: 'PETG', description: '', colors: ['Синий'], is_active: true, is_default: false, stock: 600, low: 200, reserved: 230, uses: 9, factor: 1.3, photo: '../assets/filament/f3.jpg', color_css: { 'Синий': '#2f6fd6' }, color_stock: { 'Синий': { stock: 600, low: 200 } } },
      ] }) : getJSON('/api/admin/plastics'); },
    adminPlastic: function (body) { return MOCK ? Promise.resolve({ ok: true, id: body.id || 99 }) : postJSON('/api/admin/plastic', body); },
    // Катушки материала (Фаза A мастер-плана учёта): список/приход/инвентаризация/статус.
    adminSpools: function (plasticId) { return MOCK ? Promise.resolve({ ok: true, currency: 'руб.',
      spools: [
        { id: 1, plastic_id: plasticId || 1, color: 'зелёный', net_weight_g: 1000, remaining_g: 740, low_threshold: 200, cost: 950, supplier: 'Магазин', status: 'active', note: '', pct: 74, ledger: [{ delta_g: 1000, reason: 'purchase', at: 0 }, { delta_g: -260, reason: 'print', ref_type: 'request', ref_id: 12, at: 0 }] },
        { id: 2, plastic_id: plasticId || 1, color: 'синий', net_weight_g: 1000, remaining_g: 0, low_threshold: 200, cost: null, supplier: '', status: 'empty', note: '', pct: 0, ledger: [] },
      ], by_color: { 'зелёный': { stock: 740, low: 200, spools: 1 }, 'синий': { stock: 0, low: 200, spools: 1 } } })
      : getJSON('/api/admin/spools', { plastic_id: plasticId }); },
    adminSpool: function (body) { return MOCK ? Promise.resolve({ ok: true, id: body.spool_id || 5, remaining_g: 500 }) : postJSON('/api/admin/spool', body); },
    // Загрузка фото катушки/печати: kind 'plastic'|'collection', data — dataURL (base64). Бэкенд (/api/admin/photo) ресайзит и сохраняет.
    adminPhoto: function (kind, id, dataUrl, toCollection) { return MOCK ? Promise.resolve({ ok: true, url: dataUrl, collection: !!toCollection }) : postJSON('/api/admin/photo', { kind: kind, id: id, data: dataUrl, to_collection: !!toCollection }); },
    // фото катушки конкретного цвета (kind=spool + color): путь на диске зависит от цвета
    adminSpoolPhoto: function (id, color, dataUrl) { return MOCK ? Promise.resolve({ ok: true, url: dataUrl }) : postJSON('/api/admin/photo', { kind: 'spool', id: id, color: color, data: dataUrl }); },

    // Готовые изделия / витрина (OpenTodo bd896aa6)
    products: function () { return MOCK ? Promise.resolve(M.products) : getJSON('/api/products'); },
    productOrder: function (id, qty, comment) { return MOCK ? Promise.resolve({ ok: true, order_id: 555 }) : postJSON('/api/product_order', { product_id: id, qty: qty || 1, comment: comment || '' }); },
    adminProducts: function () { return MOCK ? Promise.resolve(MA.products) : getJSON('/api/admin/products'); },
    adminProductSave: function (p) { return MOCK ? Promise.resolve({ ok: true, id: p.id || 99, product: Object.assign({ id: p.id || 99, in_stock: (p.stock || 0) > 0, sold: 0, photo: '' }, p) }) : postJSON('/api/admin/product/save', p); },
    adminProductDelete: function (id) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/product/delete', { id: id }); },

    adminPromos: function () { return MOCK ? Promise.resolve({ ok: true, promos: [
      { code: 'PRINT10', discount_pct: 10, expires_at: 0, max_uses: 0, uses: 3 },
      { code: 'SPRING', discount_pct: 15, expires_at: Math.floor(Date.now() / 1000) + 14 * 86400, max_uses: 50, uses: 12 },
    ] }) : getJSON('/api/admin/promos'); },
    adminPromo: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/promo', body); },

    adminSettings: function () { return MOCK ? Promise.resolve({ ok: true, currency: 'руб.',
      pricing: { order: ['machine_rate', 'setup_fee', 'failure_rate_pct', 'profit_margin_pct', 'volume_threshold_cm3', 'volume_surcharge_pct',
        // настройки-тумблеры 0/1 (67dfaeaf) — в моке тоже, иначе их не увидеть в превью
        'slicing_enabled', 'sanitize_3mf', 'time_prediction',
        // операционные настройки (A5 догон / B3 срочность / B2 DFM) — для экрана «Настройки»
        'nudge_abandoned_enabled', 'nudge_abandoned_hours', 'nudge_abandoned_max_days',
        'rush_surcharge_pct', 'term_split_hours', 'dfm_min_wall_mm'],
        labels: { machine_rate: 'Ставка машинного времени, руб./ч', setup_fee: 'Подготовка, руб.', failure_rate_pct: 'Буфер на брак, %', profit_margin_pct: 'Наценка, %', volume_threshold_cm3: 'Большая модель: порог объёма, см³ (0 — выкл.)', volume_surcharge_pct: 'Большая модель: надбавка, % за каждый порог сверх',
          slicing_enabled: 'Честный слайс в расчётах (1 — вкл.; 0 — только быстрый расчёт)',
          sanitize_3mf: 'Чинить .3mf из BambuStudio под слайсер (0 — не трогать файл, оставить как есть)',
          time_prediction: 'Коррекция времени печати по накопленным данным (1 — вкл., Этап 5)',
          nudge_abandoned_enabled: 'Догон брошенных расчётов (1 — вкл., 0 — выкл.)',
          nudge_abandoned_hours: 'Догон: через сколько часов напомнить о расчёте',
          nudge_abandoned_max_days: 'Догон: не трогать расчёты старше стольких дней',
          rush_surcharge_pct: 'Срочность: наценка, %',
          term_split_hours: 'Срок: порог «быстро/долго» по времени печати, ч (короче — завтра, дольше — послезавтра)',
          dfm_min_wall_mm: 'Печатопригодность: мин. толщина стенки, мм (только предупреждение)' },
        values: { machine_rate: 31, setup_fee: 1, failure_rate_pct: 8, profit_margin_pct: 30, volume_threshold_cm3: 0, volume_surcharge_pct: 0,
          slicing_enabled: 0, sanitize_3mf: 0, time_prediction: 0,
          nudge_abandoned_enabled: 0, nudge_abandoned_hours: 24, nudge_abandoned_max_days: 7,
          rush_surcharge_pct: 50, term_split_hours: 12, dfm_min_wall_mm: 0.8 } },
      crm: { order: ['crm_vip_spent', 'crm_sleep_days', 'crm_broadcast_gap_days'],
        labels: { crm_vip_spent: 'Клиенты: порог VIP по сумме заказов, руб.', crm_sleep_days: 'Клиенты: «спящий» после стольких дней без заказов', crm_broadcast_gap_days: 'Клиенты: пауза между рассылками одному клиенту, дней' },
        values: { crm_vip_spent: 100, crm_sleep_days: 45, crm_broadcast_gap_days: 7 } },
      texts: { order: ['business_name', 'business_contact'],
        labels: { business_name: 'Название мастерской', business_contact: 'Контакт для связи' },
        values: { business_name: 'Print Studio', business_contact: '@my_print_studio' } },
      // Брендинг (A3, white-label) — MOCK держим в паритете с реальным ответом. Бренд берём
      // из вшитого сервером window.__BRAND__, а не из констант: иначе предпросмотр в
      // настройках показывал бы наш дефолт поверх уже перекрашенной чужой админки.
      brand: { order: ['brand_title', 'brand_tagline', 'brand_accent'],
        labels: { brand_title: 'Название в приложении (пусто — Print Studio)',
          brand_tagline: 'Подпись под названием (пусто — «расчёт 3D-печати»)',
          brand_accent: 'Акцентный цвет, #RRGGBB (пусто — #ff7a1a)' },
        values: mockBrandValues() },
      brand_current: mockBrand(),
      // Онлайн-оплата (A1) — паритет с реальным ответом, чтобы секция была видна в превью
      pay: { order: ['pay_provider_token', 'pay_currency'],
        labels: { pay_provider_token: 'Токен провайдера из BotFather (пусто — оплата выключена)',
          pay_currency: 'Валюта счетов, ISO-код (RUB, BYN…) — как у цен мастерской' },
        values: { pay_provider_token: '', pay_currency: 'RUB' } },
      // сообщество ВЛАДЕЛЬЦЕВ студий (c356387a): ссылка на внешнюю Telegram-группу
      owners: { order: ['owners_chat_url'],
        labels: { owners_chat_url: 'Чат владельцев студий: ссылка на Telegram-группу (пусто — скрыть)' },
        values: { owners_chat_url: 'https://t.me/print_studio_owners' } },
      // возможности сборки (f98aa1ef): что личное, а что продуктовое
      edition: { edition: 'dev', features: [
        { key: 'makerworld', title: 'MakerWorld: скачивание по ссылке и пул аккаунтов',
          why: 'Завязано на личные аккаунты и обход анти-бота.', needs: 'аккаунты MakerWorld',
          dev_only: true, enabled: true, default: true, overridden: false, available: true }] } })
      : getJSON('/api/admin/settings'); },
    adminSetting: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/setting', body); },
    // Возможности сборки (f98aa1ef): {key, on} — включить/выключить, {key, reset:1} — как в сборке
    adminFeature: function (body) { return MOCK ? Promise.resolve({ ok: true, features: [] }) : postJSON('/api/admin/feature', body); },
    // Логотип мастерской: заливка — multipart (как uploadFile), снятие — обычный POST.
    adminBrandLogo: function (file) {
      if (MOCK) return Promise.resolve({ ok: true });
      var fd = new FormData(); fd.append('initData', INIT); fd.append('file', file);
      return fetch('/api/admin/brand_logo', { method: 'POST', body: fd }).then(function (r) { return r.json(); });
    },
    adminBrandLogoClear: function () { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/brand_logo', {}); },
    adminPriceCalc: function (body) { return MOCK ? Promise.resolve(mockPriceCalc(body)) : postJSON('/api/admin/price_calc', body); },

    adminDiscounts: function () { return MOCK ? Promise.resolve({ ok: true,
      tiers: [{ id: 1, min_qty: 3, pct: 5 }, { id: 2, min_qty: 10, pct: 12 }],
      users: [{ uid: 333333333, pct: 20, name: '@demo_client' }],
      customers: [{ uid: 333333333, name: '@demo_client', orders: 7, pct: 20 },
        { uid: 42, name: 'Аня', orders: 3, pct: null }, { uid: 77, name: 'Боря', orders: 1, pct: null }] }) : getJSON('/api/admin/discounts'); },
    adminDiscount: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/discount', body); },
    // Опции постобработки (B4): каталог платных услуг мастера, редактируется в десктоп-админке.
    adminOptions: function () { return MOCK ? Promise.resolve({ ok: true, options: [
      { id: 1, name: 'Шлифовка', price: 5, is_active: 1, sort: 0, per_part: 0 },
      { id: 2, name: 'Покраска', price: 12, is_active: 1, sort: 1, per_part: 0 },
      { id: 3, name: 'Склейка деталей', price: 0.2, is_active: 1, sort: 2, per_part: 1 }] }) : getJSON('/api/admin/options'); },
    adminOption: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/option', body); },
    // Центр уведомлений админа (2026-07-16): release notes; семейные счета — отдельный раздел.
    adminNotifications: function (all) { return MOCK ? Promise.resolve({ ok: true, items: [
      { id: 3, kind: 'release_note', title: 'Таймлапсы каждой печати', body: 'Теперь к заказу приходят таймлапсы всех печатей одним альбомом.', image_url: '../assets/models/photo_dino.jpg', payload: { gen_prompt: 'робот-маскот с кинохлопушкой снимает 3D-принтер' }, status: 'new', ago: 'только что' },
      { id: 2, kind: 'release_note', title: 'Обновление приложения', body: 'Появился семейный счёт: общий баланс на несколько человек.', image_url: '', payload: {}, status: 'new', ago: '5 мин назад' }] })
      : getJSON('/api/admin/notifications', all ? { all: 1 } : undefined); },
    // Семейные счета (2026-07-17, вынесено из уведомлений; пришло на смену слиянию аккаунтов).
    adminFamilies: function (all) { return MOCK ? Promise.resolve({ ok: true, currency: 'Br', requests: [
      { id: 1, kind: 'family_request', title: 'Заявка на семейный счёт', body: 'От @maria_demo, партнёр: @alex_demo', payload: { from_id: 111111111, from_name: '@maria_demo', partner: '@alex_demo' }, status: 'new', ago: 'час назад' }],
      families: [{ id: 1, title: '', balance: 45.6, members: [{ user_id: 111111111, user_name: '@maria_demo', joined_at: 0 }, { user_id: 222222222, user_name: '@alex_demo', joined_at: 0 }] }],
      customers: [{ uid: 111111111, name: '@maria_demo', orders: 4 }, { uid: 222222222, name: '@alex_demo', orders: 11 }] })
      : getJSON('/api/admin/families', all ? { all: 1 } : undefined); },
    // «Чья печать?» — выбор заказа для завершённой печати (OpenTodo 4effbc1b)
    adminPrintClassify: function (jid) { return MOCK ? Promise.resolve({ ok: true, currency: 'руб.',
      print: { jid: jid, label: 'ECC2_0.4_Koala.gcode', grams: 12.4, minutes: 96, plastic: 'Elegoo PLA', request_id: null },
      orders: [{ id: 45, label: 'Willys Jeep', user: '@demo_client', status: 'in_progress', status_label: 'В работе', qty: 1, price: 15.5, preview: '' },
        { id: 51, label: 'Овечка Тимми', user: '@anna', status: 'new', status_label: 'Новый', qty: 1, price: 6.9, preview: '' }] })
      : getJSON('/api/admin/print_classify', { jid: jid }); },
    adminPrintClassifyPick: function (jid, action, id) { return MOCK ? Promise.resolve({ ok: true, message: 'Печать прикреплена' })
      : postJSON('/api/admin/print_classify', { jid: jid, action: action, id: id || 0 }); },
    adminNotificationAction: function (id, action, extra) { return MOCK ? Promise.resolve(action === 'image_regen' ? { ok: true, image_url: '../assets/models/m2.png' } : { ok: true, sent: 2, failed: 0 }) : postJSON('/api/admin/notification_action', Object.assign({ id: id, action: action }, extra || {})); },
    adminFamilyCreate: function (memberIds, notifId) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/family_create', { member_ids: memberIds, notif_id: notifId || 0 }); },
    adminFamilyAdd: function (familyId, userId) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/family_add', { family_id: familyId, user_id: userId }); },
    adminFamilyRemove: function (familyId, userId) { return MOCK ? Promise.resolve({ ok: true, dissolved: false }) : postJSON('/api/admin/family_remove', { family_id: familyId, user_id: userId }); },
    adminActivity: function (userId) { return MOCK ? Promise.resolve({ ok: true, events: [
      { id: 3, user_id: 42, name: 'Аня', action: 'оставил заказ', detail: '#17 «Подставка»', at: Math.floor(Date.now() / 1000) - 120 },
      { id: 2, user_id: 42, name: 'Аня', action: 'рассчитал цену', detail: 'Подставка · PETG ×1', calc_id: 5, at: Math.floor(Date.now() / 1000) - 200 },
      { id: 1, user_id: 77, name: 'Боря', action: 'загрузил модель', detail: 'Benchy', at: Math.floor(Date.now() / 1000) - 4000 }] })
      : getJSON('/api/admin/activity', userId ? { user_id: userId } : null); },
    // Смета, которую видел клиент (26.08): открывается кликом по плитке ленты событий.
    adminCalc: function (id) { return MOCK ? Promise.resolve({ ok: true, calc: {
      id: id, at: Math.floor(Date.now() / 1000) - 200, ago: '3 минуты назад', action: 'рассчитал цену',
      source: 'estimate', source_label: '≈оценка по геометрии', user_id: 42,
      model_label: 'Подставка', printer: 'Elegoo Centauri Carbon 2', plastic_name: 'PETG',
      color: 'чёрный', quality: 'Стандарт — 0.20 мм', strength: 'Золотая середина', qty: 1,
      grams_total: 213.1, time_label: '9 ч 36 мин', total: 21.7, total_label: '21,70 BYN',
      each_label: '', total_before_label: '', discount_pct: 0, approx: true, estimated: false,
      lines: [['Филамент (213.1 г)', '9,60 BYN'], ['Машинное время', '8,20 BYN']],
      dims: '120×80×45 мм', plates: 1, scale_pct: 100, urgency: 'normal', term: 'завтра',
      eta: '27.08', verdict: 'ok', dfm: [], promo: null, purge: '', extra_colors: 0,
      currency: 'BYN', preview: '', client: { name: 'Аня', orders: 3, spent: 120, avatar: '' },
      refined: { total_label: '32,50 BYN', at: 0, ago: '2 минуты назад', grams: 240.4,
        time_label: '11 ч 02 мин', delta_label: '10,80 BYN', delta_pct: 50 },
      next_order: null } })
      : getJSON('/api/admin/calc', { id: id }); },
    adminBigData: function () { return MOCK ? Promise.resolve({ ok: true,
      samples: { count: 24, avg_est: 213, avg_actual: 228, mae_min: 19.4, mape_pct: 9.1, within20_pct: 79.2 },
      samples_by_material: [{ material: 'PLA', count: 17, mae_min: 16.2 }, { material: 'PETG', count: 7, mae_min: 27.1 }],
      materials: [{ material: 'Elegoo PLA', orders: 31, grams: 4120.5, minutes: 9800 }, { material: 'PETG чёрный', orders: 9, grams: 1340.0, minutes: 3120 }],
      collection: { models: 48, total_uses: 173 },
      top_models: [{ label: '#3DBenchy', uses: 22, material: 'PLA' }, { label: 'Cute Dragon', uses: 14, material: 'PLA' }],
      totals: { printed_orders: 30, saved_models: 61, coverage_pct: 80.0 },
      inventory: { active_spools: 5, remaining_g: 3240, priced_spools: 3, low_colors: 1, out_colors: 1 },
      cogs: { orders_with_cogs: 12, printed_orders: 30, coverage_pct: 40.0, revenue: 24800, cogs: 6120, margin: 18680, margin_pct: 75.3 },
      // блок брака: без него карточка «Журнал печатей · брак» в превью не рисуется вовсе
      journal: { total: 42, ungraded: 3, fails: 5, fail_pct: 11.9, grams: 5300, minutes: 9100, waste: 640,
        by_plastic: [{ plastic: 'Elegoo PLA', n: 28, fails: 3, fail_pct: 10.7, grams: 3400, waste: 310 },
          { plastic: 'PETG чёрный', n: 14, fails: 2, fail_pct: 14.3, grams: 1900, waste: 330 }] } })
      : getJSON('/api/admin/bigdata'); },

    adminShades: function () { return MOCK ? Promise.resolve({ ok: true, shades: [
      { name: 'Зелёный', hex: '#2fae57' }, { name: 'Синий', hex: '#2f6fd6' }, { name: 'Красный', hex: '#d23b3b' }, { name: 'Жёлтый', hex: '#e8b920' },
    ] }) : getJSON('/api/admin/shades'); },
    adminShade: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/shade', body); },

    adminPresets: function () { return MOCK ? Promise.resolve({ ok: true,
      processes: ['0.20mm Standard.json', '0.12mm Fine.json', '0.16mm Optimal.json', '0.28mm Draft.json'],
      quality: [{ id: 1, label: 'Высокое', layer: 0.12, process: '0.12mm Fine.json', is_active: true, is_default: false },
        { id: 3, label: 'Стандарт', layer: 0.20, process: '0.20mm Standard.json', is_active: true, is_default: true }],
      strength: [{ id: 5, label: 'Золотая середина', infill: 15, walls: 3, is_active: true, is_default: true },
        { id: 3, label: 'Высокая', infill: 40, walls: 3, is_active: true, is_default: false }] }) : getJSON('/api/admin/presets'); },
    adminPreset: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/preset', body); },

    adminCollection: function (params) {
      if (MOCK) {
        // sort: порядок разделов каталога (475b413e); при равном sort — по count DESC
        var CATS = [
          { name: 'Напечатанное', count: 2, sort: 0 },
          { name: '3D модели FLEXI Toys', count: 1, sort: 100 },
          { name: '3D модели ламп', count: 0, sort: 100 },
        ];
        var ITEMS = [
          { id: 1, label: 'Бэйбизавр', uses: 12, has_photo: true, category: 'Напечатанное', photo: '../assets/models/photo_dino.jpg' },
          // has_photo=false, но photo есть: бэкенд отдаёт рендер, когда нет фото печати (b785c67f)
          { id: 2, label: 'Устройство по перетасовке карт', uses: 5, has_photo: false, category: '', photo: '../assets/models/m2.png' },
          { id: 4, label: 'Яйцо дракона', uses: 7, has_photo: true, category: 'Напечатанное', photo: '../assets/models/m4.png' },
          { id: 5, label: 'Гибкий кот', uses: 3, has_photo: false, category: '3D модели FLEXI Toys', photo: '../assets/models/m2.png' },
        ];
        if (params && typeof params.category !== 'undefined') {
          var dbcat = params.category === '__none__' ? '' : params.category;
          var list = ITEMS.filter(function (it) { return it.category === dbcat; });
          return Promise.resolve({ ok: true, category: params.category, categories: CATS, items: list, offset: 0, total: list.length, has_more: false });
        }
        return Promise.resolve({ ok: true, categories: CATS, uncat_count: 1, total: 4 });
      }
      return getJSON('/api/admin/collection', params || undefined);
    },
    adminCollectionEdit: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/collection', body); },

    adminPrinters: function () { return MOCK ? Promise.resolve({ ok: true, printers: [
      { id: 1, name: 'Elegoo Centauri Carbon 2', is_active: true, is_default: true, machine: 'C:/profiles/centauri.json', process: 'C:/profiles/process', filament: 'C:/profiles/filament' },
      { id: 2, name: 'Bambu A1', is_active: false, is_default: false, machine: 'C:/profiles/a1.json', process: 'C:/profiles/a1_process', filament: 'C:/profiles/a1_filament' },
    ] }) : getJSON('/api/admin/printers'); },
    adminPrinter: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/printer', body); },

    // Пул аккаунтов MakerWorld: ротация обходит анти-бот 418 (~9 закачек на аккаунт).
    adminMwAccounts: function () { return MOCK ? Promise.resolve({ ok: true, accounts: [
      { id: 'main', label: 'Основной', configured: true, token_mask: 'ey12ab…9f0c', enabled: true,
        days_left: 63.4, expired: false, cooldown_sec: 0, usable: true, uses: 128,
        last_error: '', profile: 'C:/data/mw_profile', profile_ready: true },
      { id: 'second', label: 'Второй', configured: true, token_mask: 'ey77cd…41ab', enabled: true,
        days_left: 88.1, expired: false, cooldown_sec: 2700, usable: false, uses: 41,
        last_error: 'анти-бот MakerWorld (418)', profile: 'C:/data/mw_profiles/second', profile_ready: true },
    ] }) : getJSON('/api/admin/mw_accounts'); },
    adminMwAccount: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/mw_account', body); },
  });

  // ---- CRM: клиенты, сегменты, рассылки, задачи (crm-roadmap.md, OpenTodo a8b591cf) ----
  var now0 = Math.floor(Date.now() / 1000);
  var MC = {
    clients: { ok: true, currency: 'руб.', total: 3,
      segments: { new: 'Новый', repeat: 'Повторный', vip: 'VIP', sleeping: 'Спящий' },
      segment_counts: { new: 1, repeat: 2, vip: 1, sleeping: 1 },
      clients: [
        { uid: 333333333, name: '@demo_client', tg_name: '@demo_client', orders: 7, printed: 5, spent: 412.6, avg_check: 82.52, first_at: now0 - 90 * 86400, last_at: now0 - 2 * 86400, last_ago: '2 дн назад', discount_pct: 20, phone: '', note: 'постоянник', no_broadcast: 0, segments: ['repeat', 'vip'] },
        { uid: 42, name: 'Аня', tg_name: 'Аня', orders: 3, printed: 2, spent: 56.4, avg_check: 28.2, first_at: now0 - 40 * 86400, last_at: now0 - 6 * 86400, last_ago: '6 дн назад', discount_pct: null, phone: '+375 29 …', note: '', no_broadcast: 0, segments: ['repeat'] },
        { uid: 77, name: 'Боря', tg_name: 'Боря', orders: 1, printed: 1, spent: 12.0, avg_check: 12.0, first_at: now0 - 80 * 86400, last_at: now0 - 80 * 86400, last_ago: '2 мес назад', discount_pct: null, phone: '', note: '', no_broadcast: 1, segments: ['new', 'sleeping'] },
      ] },
    tasks: { ok: true, tasks: [
      { id: 1, title: 'Перезвонить Ане про PETG', user_id: 42, client: 'Аня', due_at: now0 + 3600, due_label: 'сегодня', overdue: false, today: true, done: 0 },
      { id: 2, title: 'Спросить отзыв у Бори', user_id: 77, client: 'Боря', due_at: now0 - 86400, due_label: 'вчера', overdue: true, today: false, done: 0 },
      { id: 3, title: 'Заказать катушку чёрного PLA', user_id: null, client: '', due_at: 0, due_label: '', overdue: false, today: false, done: 0 },
    ] },
  };
  function mockClientCard(uid) {
    var c = MC.clients.clients.filter(function (x) { return String(x.uid) === String(uid); })[0] || MC.clients.clients[0];
    return { ok: true, currency: 'руб.', segments: MC.clients.segments,
      client: Object.assign({}, c, { fav_material: 'Crown PLA шёлковый', balance: 3.4, referrals: 1,
        first_ago: '3 мес назад',
        recent: [
          { id: 15, label: 'Бэйбизавр', status: 'new', status_label: '🆕 Новый', price: 3.1, ago: '2 дн назад' },
          { id: 13, label: 'Card Shuffler', status: 'printed', status_label: '✅ Напечатано', price: 15.5, ago: '8 дн назад' },
        ],
        balance_tx: [
          { id: 3, amount: -6.6, reason: 'Оплата заказа #13', at: now0 - 86400 * 2, at_label: '02.07.2026', comment: '' },
          { id: 2, amount: 10, reason: 'Пополнение мастером', at: now0 - 86400 * 9, at_label: '25.06.2026', comment: 'принёс наличными' },
        ],
        // живая корзина (a327c3ec): мастер видит, что клиент собрал, но не заказал
        cart: { count: 2, total: '7,80 руб.', updated_label: '08.07 14:22', items: [
          { token: 't1', label: 'Яйцо дракона', totalStr: '4,60 руб.' },
          { token: 't2', label: 'Держатель наушников', totalStr: '3,20 руб.' },
        ] },
        tasks: MC.tasks.tasks.filter(function (t) { return t.user_id === c.uid; }) }) };
  }
  Object.assign(API, {
    adminClients: function (params) { return MOCK ? Promise.resolve(MC.clients) : getJSON('/api/admin/clients', params || {}); },
    adminClientsCsvUrl: function () { return MOCK ? '' : ('/api/admin/clients/export.csv?initData=' + encodeURIComponent(INIT)); },
    adminClient: function (uid) { return MOCK ? Promise.resolve(mockClientCard(uid)) : getJSON('/api/admin/client', { uid: uid }); },
    adminClientSave: function (uid, fields) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/client_save', Object.assign({ uid: uid }, fields || {})); },
    adminClientDiscount: function (uid, pct, name) { return MOCK ? Promise.resolve({ ok: true, pct: pct }) : postJSON('/api/admin/client_discount', { uid: uid, pct: pct, name: name || '' }); },
    adminClientMessage: function (uid, text, image) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/client_message', { uid: uid, text: text, image: image || '' }); },
    // Сплошная переписка с клиентом: треды всех заказов + сообщения вне заказа (2026-07-19).
    // Открытие сбрасывает непрочитанное, как и тред заказа.
    adminClientChat: function (uid) { return MOCK ? Promise.resolve({ ok: true, messages: [
      { id: 1, request_id: 13, text: 'Здравствуйте! Когда будет готово?', mine: false, at: '02.07 11:31', image: '', order_label: 'Card Shuffler' },
      { id: 2, request_id: 13, text: 'Добрый день! Завтра к обеду.', mine: true, at: '02.07 11:40', image: '', order_label: 'Card Shuffler' },
      { id: 3, request_id: null, text: 'Спасибо, всё отлично!', mine: false, at: '05.07 09:02', image: '', order_label: '' },
    ] }) : getJSON('/api/admin/client_chat', { uid: uid }); },
    // Поиск по тексту переписки — командная палитра Ctrl+K.
    adminChatSearch: function (q) { return MOCK ? Promise.resolve({ ok: true, hits: [] }) : getJSON('/api/admin/chat_search', { q: q }); },
    // Рассылка по сегменту: dry=true — предпросмотр охвата без отправки.
    adminBroadcast: function (segment, text, dry, opts) { return MOCK ? Promise.resolve({ ok: true, dry: !!dry, total: 3, eligible: 2, skipped_optout: 1, skipped_recent: 0, sent: dry ? 0 : 2, failed: 0, sample: ['@demo_client', 'Аня'], attach: (opts && opts.random_model) ? { model: 'Яйцо дракона', whats_new: !!(opts && opts.whats_new) } : undefined }) : postJSON('/api/admin/broadcast', { segment: segment || '', text: text || '', dry: !!dry, options: opts || {} }); },
    adminTasks: function (all) { return MOCK ? Promise.resolve(MC.tasks) : getJSON('/api/admin/tasks', all ? { all: 1 } : undefined); },
    adminTask: function (body) { return MOCK ? Promise.resolve({ ok: true, id: 9 }) : postJSON('/api/admin/task', body); },
    // Доставка (A2): прайс зон + реестр перевозчиков; CRUD зоны; правка доставки и отправления в заказе
    adminDelivery: function () {
      return MOCK ? Promise.resolve({ ok: true, currency: 'руб.', carriers: [{ id: 'cdek', name: 'СДЭК', auto_url: true }, { id: 'other', name: 'Другое', auto_url: false }],
        types: [{ id: 1, name: 'Курьером по городу', price: 5, free_from: 50, eta: '1–2 дня', comment: '', is_active: 1, sort: 0 }] })
        : getJSON('/api/admin/delivery');
    },
    adminDeliveryType: function (body) { return MOCK ? Promise.resolve({ ok: true, id: 2 }) : postJSON('/api/admin/delivery_type', body); },
    adminOrderDelivery: function (body) { return MOCK ? Promise.resolve({ ok: true, delivery_cost: body.cost || 0 }) : postJSON('/api/admin/order/delivery', body); },
    // Доставка по умолчанию в профиле (A2): тип + адрес. Та же ручка /api/prefs, что и у
    // подтверждения цены — сервер правит только те настройки, ключи которых пришли.
    saveDeliveryPrefs: function (typeId, address) {
      if (MOCK) return Promise.resolve({ ok: true, prefs: { delivery_type_id: typeId || 0, delivery_address: address || '' } });
      return postJSON('/api/prefs', { delivery_type_id: typeId || 0, delivery_address: address || '' });
    },
    adminOrderShipment: function (body) { return MOCK ? Promise.resolve({ ok: true, track: { carrier: body.carrier || '', carrier_name: 'СДЭК', number: body.number || '', url: '', shipped_at: 1 } }) : postJSON('/api/admin/order/shipment', body); },
    // Команда (A4): сотрудники и роли
    adminStaff: function () {
      return MOCK ? Promise.resolve({ ok: true, staff: [{ user_id: 1, role: 'owner', role_label: 'Владелец', name: '', root: true }],
        roles: [{ id: 'owner', label: 'Владелец', hint: '' }, { id: 'operator', label: 'Оператор', hint: '' }], caps: [] })
        : getJSON('/api/admin/staff');
    },
    adminStaffSave: function (body) { return MOCK ? Promise.resolve({ ok: true }) : postJSON('/api/admin/staff', body); },
  });
})();
