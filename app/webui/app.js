// app.js — Print Studio mini-app shell: routing, state, Telegram bridge.
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, API = window.API;
  var tg = window.Telegram && window.Telegram.WebApp;
  // Бренд мастерской (A3, white-label): сервер вшил его в index.html до первого кадра.
  // Дефолты продублированы здесь на случай статичного превью без сервера — тогда шапка
  // рисуется нашей, а не пустой.
  var BRAND = window.__BRAND__ || {};
  if (!BRAND.title) { BRAND = { title: 'OpenPrintShop', stock: true, logo: '', wordmark: ['OPEN', 'PRINT', 'SHOP'] }; }
  // Вордмарк нашего имени: части через точки-акценты (OPEN·PRINT·SHOP, PRINT·STUDIO) — brand.wordmark.
  function wordmark(parts) {
    var out = [];
    (parts && parts.length ? parts : ['OPEN', 'PRINT', 'SHOP']).forEach(function (p, i) {
      if (i) out.push(h('em', { key: 'd' + i }, '·'));
      out.push(p);
    });
    return out;
  }
  window.__BRAND__ = BRAND;   // нормализованный — им же пользуются остальные экраны

  // Возможности сборки (f98aa1ef): часть инструментов делалась под машину разработчика
  // (рендер OpenSCAD, поиск по внешним каталогам) и в билде студии выключена. Флаги приходят
  // в /api/options; пока их нет — считаем включённым, иначе разделы моргали бы при загрузке.
  window.PS_hasFeature = function (key) {
    var f = window.__FEATURES__;
    return !f || f[key] !== false;
  };

  // Глубокая ссылка из уведомления: /app/?v=2&go=<экран>&id=<заказ>. Telegram сохраняет
  // query-строку (initData он кладёт в #hash), поэтому экран берём из location.search.
  var BOOT = (function () {
    try {
      var q = new URLSearchParams(window.location.search);
      var go = (q.get('go') || '').toLowerCase();
      var id = parseInt(q.get('id') || '0', 10) || 0;
      var t = q.get('t') || '';   // токен модели для go=quote (уведомление «расчёт готов», ae4c6c13)
      var tab = (q.get('tab') || '').toLowerCase();   // адресуемая вкладка tabbed()-обёрток (IA-редизайн)
      var MAP = { orders: 'orders', adminorder: 'adminOrder', adminorders: 'adminOrders', admin: 'admin',
        printer: 'adminPrinter', dash: 'adminDash', chat: 'chat', collection: 'collection', models: 'models',
        profile: 'profile', adminqueuefix: 'adminCalcQueue',
        clients: 'adminClients', client: 'adminClient', tasks: 'adminTasks',
        print: 'adminPrint', pricing: 'adminPricing', masterlog: 'adminMasterLog',
        products: 'products', adminproducts: 'adminProducts',   // готовые изделия (bd896aa6)
        adminnotifs: 'adminNotifs',   // центр уведомлений (2026-07-16)
        adminfamilies: 'adminFamilies',   // семейные счета (кнопка из телеграм-пинга о заявке)
        printclassify: 'adminPrintClassify',   // «чья печать?» из пинга о финише (4effbc1b)
        workshop: 'workshop', gfx: 'gfxProject',   // мастерская органайзеров: список и проект
        apps: 'apps',   // «Приложения» — корневой таб клиента (2026-09-02)
        // Мобильная админка повторяет иерархию настольной (2026-09-02): алиасы те же, что в
        // desk/router.jsx, чтобы одна и та же ссылка из уведомления открывала раздел на обоих.
        plan: 'adminPlan', calib: 'adminCalib', calibration: 'adminCalib',
        plastics: 'adminPlastics', shades: 'adminPlastics',
        settings: 'adminStudio', adminsettings: 'adminStudio',
        community: 'adminCommunity', kb: 'adminCommunity', adminkb: 'adminCommunity',
        catalog: 'collection', invite: 'profile' };   // старые экраны слиты (IA-редизайн 2026-07)
      return { go: go, id: id, t: t, tab: tab, screen: MAP[go] || 'hub' };
    } catch (e) { return { go: '', id: 0, t: '', tab: '', screen: 'hub' }; }
  })();

  // ── Телеметрия JS-ошибок (f4de8627) ──
  // Фронт падает молча: у клиента белый экран, а мастер узнаёт об этом в лучшем случае из
  // жалобы. Ловим необработанные исключения и отклонённые промисы и шлём на /api/jserr.
  // Троттлинг локальный (раз в 30 с): один сломанный рендер иначе шлёт по ошибке на кадр.
  (function () {
    var lastSent = 0;
    function report(what, e) {
      var now = Date.now();
      if (now - lastSent < 30000) return;
      lastSent = now;
      var msg = '';
      try {
        var err = (e && (e.error || e.reason)) || null;
        msg = what + ': ' + ((err && (err.stack || err.message)) || (e && e.message) || String(err || e));
        if (e && e.filename) msg += ' @ ' + e.filename + ':' + (e.lineno || 0);
        msg += ' · экран ' + (window.__PS_SCREEN__ || '?');
      } catch (x) { msg = what + ': (не удалось прочитать ошибку)'; }
      try { API.clientError(msg.slice(0, 500)); } catch (x) { /* сеть недоступна — молчим */ }
    }
    window.addEventListener('error', function (e) { report('JS', e); });
    window.addEventListener('unhandledrejection', function (e) { report('Promise', e); });
  })();

  // ── B4-инстант: локальная цена постобработки (без переслайса) ──
  // Услуги постобработки — ПЛОСКИЕ (фикс-цена, известна клиенту), а срочность — чистый множитель
  // печати. Итог считаем ровно как сервер (pricing.apply_options): ceil до 10 копеек от
  // (непокруглённая база печати + сумма выбранных услуг). Округление копеек делаем через целые
  // (Math.round × 100), затем ceil к десятку копеек — 1-в-1 с pricing._ROUND_KOPECKS.
  function ceil10(rub) {
    var k = Math.round((Number(rub) || 0) * 100);   // до копеек (как round() на сервере)
    return (Math.ceil(k / 10) * 10) / 100;          // вверх до 10 копеек
  }
  // Формат суммы 1-в-1 с texts.fmt_money: пробел-разряды, запятая-десятичные, " руб." в конце.
  function fmtMoney(n, cur) {
    n = Number(n) || 0;
    var neg = n < 0; if (neg) n = -n;
    var p = n.toFixed(2).split('.');
    p[0] = p[0].replace(/\B(?=(\d{3})+(?!\d))/g, ' ');   // разрядность обычным пробелом
    return (neg ? '-' : '') + p[0] + ',' + p[1] + ' ' + (cur || 'руб.');
  }

  // ── Подбор ближайшего цвета палитры к hex-цветам дизайна из .3mf (model.mc_colors) ──
  // Модель открывается сразу «в своих» цветах: сервер вытащил hex филаментов из .3mf, здесь
  // подбираем к каждому ближайший ДОСТУПНЫЙ цвет выбранного пластика (обычно Elegoo PLA Matte).
  function _hexesOf(css) { return String(css || '').match(/#[0-9a-fA-F]{3,8}/g) || []; }
  function _rgbOf(hex) {
    var s = String(hex || '').replace('#', '');
    if (s.length === 3) s = s.replace(/./g, function (ch) { return ch + ch; });
    if (s.length > 6) s = s.slice(0, 6);                  // '#rrggbbaa' → без альфы
    if (s.length !== 6) return null;
    var n = parseInt(s, 16);
    if (isNaN(n)) return null;
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  // redmean-метрика: перцептивно заметно лучше сырого евклида, а считается так же дёшево
  function _colorDist(a, b) {
    var rm = (a[0] + b[0]) / 2, dr = a[0] - b[0], dg = a[1] - b[1], db = a[2] - b[2];
    return (2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db;
  }
  // Имя ближайшего цвета пластика pl ({colors, color_css, out_of_stock}) к hex; цвета «на нуле»
  // пропускаем (предвыбрать недоступный — тупик). Переливающийся цвет палитры (color_css =
  // linear-gradient c ≥2 стопами) сравниваем по каждому стопу. null — палитры/hex нет.
  function nearestPaletteColor(pl, hex) {
    var rgb = _rgbOf(hex);
    if (!rgb || !pl) return null;
    var names = pl.colors || [], css = pl.color_css || [], oos = pl.out_of_stock || [];
    var best = null, bestD = Infinity;
    names.forEach(function (name, i) {
      if (oos.indexOf(name) >= 0) return;
      _hexesOf(css[i]).forEach(function (stop) {
        var r = _rgbOf(stop);
        if (!r) return;
        var d = _colorDist(rgb, r);
        if (d < bestD) { bestD = d; best = name; }
      });
    });
    return best;
  }
  // screens_core (ручное включение мультицвета) подбирает слоты тем же матчером
  window.PS_NearestColor = nearestPaletteColor;

  var TITLES = {
    // model без заголовка (редизайн поиска F4): экран открывается баром с запросом,
    // строка «Поиск моделей» дублировала бы его
    hub: [null, null], model: [null, null], parametric: ['Настраиваемые модели', 'свой текст'], config: ['Параметры', 'шаг 2/3'],
    slicing: ['Смета', 'считаю…'], quote: ['Смета', 'готово'], orders: ['Мои заказы', null],
    cart: ['Корзина', null], collection: ['Каталог', null], models: ['Мои модели', null],
    custom: ['Индивидуальный заказ', null], customizer: [null, null], profile: ['Профиль', null],
    products: ['Готовые изделия', null],
    apps: ['Приложения', null],   // витрина инструментов клиента (2026-09-02)
    planner: ['Планировщик стены', 'Multiboard'],
    // без заголовка: конфигуратору дорог каждый пиксель высоты (3D-сцена + панели)
    gridfinity: [null, null],
    workshop: ['Мастерская', 'органайзеры'], gfxProject: [null, null],
    catalog: ['Каталог пластиков', null], help: ['Как это работает', null],
    admin: ['Пульт', 'мастер'], chat: ['Чат с мастером', null],
    adminDash: ['Дашборд', 'мастер'], adminOrders: ['Заказы', 'мастер'], adminOrder: ['Заказ', 'мастер'],
    adminQueue: ['Очередь печати', 'мастер'], adminPrinter: ['Принтер', 'LIVE'], adminStatus: ['Статус системы', 'мастер'],
    adminService: ['Обслуживание', 'ТО'],
    adminSelf: ['Печать для себя', 'мастер'],
    adminPlastics: ['Пластики', 'мастер'], adminPromos: ['Промокоды', 'мастер'], adminSettings: ['Цены', 'мастер'],
    adminPriceCalc: ['Калькулятор', 'мастер'],
    adminCalcQueue: ['Разбор ссылок', 'мастер'],
    adminJournal: ['Журнал печатей', 'мастер'],
    adminPrint: ['Печать', 'мастер'], adminPricing: ['Цены и скидки', 'мастер'],
    adminMasterLog: ['Журнал мастера', 'мастер'],
    adminDelivery: ['Доставка', 'мастер'], adminStaff: ['Команда', 'мастер'],
    adminDiscounts: ['Скидки', 'мастер'], adminShades: ['Оттенки', 'мастер'], adminCollectionAdmin: ['Коллекция', 'мастер'],
    adminProducts: ['Готовые изделия', 'мастер'],
    adminPrinters: ['Принтеры', 'мастер'],
    adminActivity: ['Активность', 'мастер'], adminBigData: ['Big Data', 'мастер'],
    adminClients: ['Клиенты', 'мастер'], adminClient: ['Клиент', 'мастер'], adminTasks: ['Задачи', 'мастер'],
    adminNotifs: ['Уведомления', 'мастер'],
    adminErrors: ['Ошибки', 'телеметрия сбоев'],
    adminKb: ['База знаний', 'инструкции студии'],
    adminFamilies: ['Семейные счета', 'мастер'],
    adminPrintClassify: ['Чья печать?', 'мастер'],
    adminMore: ['Ещё', 'мастер'],
    // экраны, добавленные ради паритета с настольной админкой (2026-09-02)
    adminPlan: ['План недели', 'дедлайны × мощность'],
    adminStudio: ['Настройки', 'бренд · доступ · поведение бота'],
    adminCommunity: ['Сообщество', 'база знаний · чат владельцев'],
    adminCalib: ['Калибровка', 'кампании · паспорта'],
  };

  function haptic(kind) { try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(kind || 'light'); } catch (e) {} }
  function alertMsg(m) { try { if (tg && tg.showAlert) { tg.showAlert(m); return; } } catch (e) {} window.alert(m); }
  // Подтверждение необратимых действий (отмена заказа и т.п.): нативный Telegram-диалог,
  // в браузере — обычный confirm.
  function askConfirm(msg, onYes) {
    try { if (tg && tg.showConfirm) { tg.showConfirm(msg, function (ok) { if (ok) onYes(); }); return; } } catch (e) {}
    if (window.confirm(msg)) onYes();
  }

  // Нижняя таб-панель (IA-редизайн 2026-07): 4 корневых раздела клиента. Переключение таба
  // НЕ пишется в стек «Назад» (goTab сбрасывает стек — табы и есть корни навигации).
  // 2026-09-02 (решение владельца): «Профиль» уехал в шапку — кружок с аватаркой справа от
  // корзины (ProfileChip), а его место между «Расчётом» и «Каталогом» заняли «Приложения» —
  // витрина инструментов (органайзер в ящик и другие, screens_apps.js).
  var TABS = [
    { key: 'hub', label: 'Расчёт', ic: 'coin' },
    { key: 'apps', label: 'Приложения', ic: 'apps' },
    { key: 'collection', label: 'Каталог', ic: 'globe' },
    { key: 'orders', label: 'Заказы', ic: 'receipt' },
  ];
  // Подсветка таба для дочерних клиентских экранов: мастерская органайзеров и планировщик
  // открываются из «Приложений», и таб должен гореть, пока клиент внутри них.
  var TAB_OF = { workshop: 'apps', gfxProject: 'apps', gridfinity: 'apps', planner: 'apps' };

  // Кружок профиля в шапке. Картинка по убыванию приоритета: аватар, нарисованный в профиле
  // (/api/profile.avatar_url) → фото Telegram (initDataUnsafe.user.photo_url — приходит не во
  // всех клиентах) → первая буква имени на подложке → силуэт. Точка — непросмотренные
  // достижения (раньше жила на табе «Профиль»). Битая картинка откатывается к букве, а не
  // оставляет пустой кружок.
  function ProfileChip(props) {
    var p = props.profile || {};
    var u = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || {};
    var src = p.avatar_url || u.photo_url || '';
    var bs = useState(''); var broken = bs[0], setBroken = bs[1];
    var letter = String(u.first_name || u.username || '').trim().charAt(0).toUpperCase();
    var showImg = !!src && broken !== src;
    return h('button', { className: 'profchip' + (props.on ? ' on' : ''), type: 'button', onClick: props.onClick, 'aria-label': 'Профиль' },
      h('span', { className: 'pc-wrap' },
        showImg
          ? h('img', { className: 'pc-av', src: src, alt: '', onError: function () { setBroken(src); } })
          : h('span', { className: 'pc-av pc-ph' }, letter || icon('user', { size: 17 })),
        p.unseen > 0 && h('span', { className: 'pc-dot' })));
  }
  // Где таб-панель скрыта: поток расчёта (чекаут-паттерн — тап по табу не должен терять
  // конфигурацию), полноэкранные слайсинг/чат/кастомайзер/планировщик и вся админка.
  var TABBAR_HIDDEN = { config: 1, quote: 1, slicing: 1, chat: 1, customizer: 1, planner: 1, gridfinity: 1, gfxProject: 1 };

  // Админский таб-бар (этап B IA-редизайна): мастер живёт в 4 зонах, редкое — в «Ещё».
  var ADMIN_TABS = [
    { key: 'admin', label: 'Пульт', ic: 'bolt' },
    { key: 'adminOrders', label: 'Заказы', ic: 'receipt' },
    { key: 'adminPrint', label: 'Печать', ic: 'printer' },
    { key: 'adminClients', label: 'Клиенты', ic: 'star' },
    { key: 'adminMore', label: 'Ещё', ic: 'sliders' },
  ];
  // Подсветка таба для дочерних админ-экранов (детали заказа → «Заказы» и т.п.);
  // всё, что не перечислено, относится к «Ещё».
  var ADMIN_TAB_OF = {
    admin: 'admin',
    adminOrders: 'adminOrders', adminOrder: 'adminOrders', adminCalcQueue: 'adminOrders',
    adminPrint: 'adminPrint', adminPrinter: 'adminPrint', adminQueue: 'adminPrint',
    adminService: 'adminPrint', adminSelf: 'adminPrint',
    adminClients: 'adminClients', adminClient: 'adminClients', adminTasks: 'adminClients',
  };

  // ── Ленивая подгрузка админских экранов ───────────────────────────────────
  // screens_admin*.js — 406 КиБ на троих. Нужны они мастеру, а подключены были тремя
  // безусловными тегами в index.html, то есть ехали КАЖДОМУ клиенту при КАЖДОМ открытии
  // мини-аппа (страница отдаётся с no-store и без сжатия — то есть заново и целиком).
  // Теперь тянем их при первом заходе на любой admin*-экран; механика — как у three.js
  // в screens_3d.js (ensureThree).
  //
  // Почему это вообще безопасно: карта SCREENS собирается заново на каждый рендер и читает
  // window в этот момент — приехавшие экраны подхватываются сами, без реестра и регистрации.
  //
  // И почему тут нельзя ошибиться молча: `SCREENS[screen] || window.Hub` для ещё не
  // приехавшего админского экрана даёт undefined и уводит мастера на ГЛАВНУЮ — без
  // исключения и без строчки в консоли. Поэтому пока экраны не готовы, мы не рисуем Cur
  // вообще: на его месте либо «Загружаю», либо явная ошибка с кнопкой «Повторить».
  var ADMIN_SCRIPTS = ['screens_admin.js', 'screens_admin2.js', 'screens_admin3.js', 'screens_admin4.js'];
  // По одному сторожевому глобалу на файл — из его же Object.assign(window, …) в самом конце.
  // Проверяем именно их, а не факт события load: файл мог приехать обрезанным (обрыв связи,
  // заглушка прокси) — тогда load будет, а экранов не будет.
  var ADMIN_SENTINELS = ['Admin', 'AdminPlastics', 'AdminClients', 'AdminPlan'];
  function adminReady() {
    for (var i = 0; i < ADMIN_SENTINELS.length; i++) if (!window[ADMIN_SENTINELS[i]]) return false;
    return true;
  }
  // ?v=… — тот же ключ кеша, что у остального фронта: без него залипшая копия в WebView
  // переживёт деплой. В raw-режиме (плейсхолдер не подставлен) грузим без ?v.
  var _ver = (window.__APPVER__ && window.__APPVER__.indexOf('%') < 0) ? window.__APPVER__ : '';
  function loadAdminScript(src) {
    return new Promise(function (resolve, reject) {
      var el = document.querySelector('script[data-psadmin="' + src + '"]');
      if (el) {
        if (el.getAttribute('data-loaded')) { resolve(); return; }
        el.addEventListener('load', function () { resolve(); });
        el.addEventListener('error', function () { reject(new Error(src)); });
        return;
      }
      el = document.createElement('script');
      el.src = _ver ? (src + '?v=' + encodeURIComponent(_ver)) : src;
      el.async = false;
      el.setAttribute('data-psadmin', src);
      el.addEventListener('load', function () { el.setAttribute('data-loaded', '1'); resolve(); });
      el.addEventListener('error', function () {
        // Тег обязательно снимаем: иначе повтор найдёт его через querySelector и будет вечно
        // ждать load, которого у сломанного тега уже не будет.
        try { if (el.parentNode) el.parentNode.removeChild(el); } catch (e) {}
        reject(new Error(src));
      });
      document.head.appendChild(el);
    });
  }
  // Планировщик Multiboard — личный инструмент владельца: движок и экран грузятся только при
  // первом заходе. В открытой версии проекта этих файлов нет, и страница их зря не просит.
  function PlannerLazy(props) {
    var st = React.useState(window.MultiboardPlanner ? 'ready' : 'loading');
    var state = st[0], setState = st[1];
    React.useEffect(function () {
      if (state !== 'loading') return;
      loadAdminScript('planner_engine.js')
        .then(function () { return loadAdminScript('screens_planner.js'); })
        .then(function () { setState(window.MultiboardPlanner ? 'ready' : 'missing'); },
              function () { setState('missing'); });
    }, []);
    if (state === 'ready') return h(window.MultiboardPlanner, props);
    return h('div', { className: 'screen' }, h('div', { className: 'empty' },
      state === 'missing' ? 'Планировщика нет в этой сборке' : 'Загружаю планировщик…'));
  }
  var _adminPromise = null;
  function ensureAdmin() {
    if (adminReady()) return Promise.resolve();
    if (_adminPromise) return _adminPromise;
    _adminPromise = ADMIN_SCRIPTS.reduce(function (chain, src) {
      return chain.then(function () { return loadAdminScript(src); });
    }, Promise.resolve()).then(function () {
      if (!adminReady()) throw new Error('admin screens not registered');
    }).catch(function (e) { _adminPromise = null; throw e; });   // дать шанс повторить
    return _adminPromise;
  }

  // Заглушка вместо админского экрана, пока его код едет. Отдельная ошибка с кнопкой — не
  // украшательство: без неё оборвавшаяся загрузка выглядит как «приложение выкинуло на
  // главную», и мастер не понимает, что произошло и что делать.
  function AdminGate(props) {
    if (props.state === 'error') {
      return h('div', { className: 'screen' },
        h('div', { className: 'empty' }, icon('sliders', { size: 46 }),
          h('div', null, 'Админка не загрузилась'),
          h('p', { className: 'hint', style: { marginTop: 6 } },
            'Экраны мастера подгружаются отдельным файлом, и он не доехал — похоже, пропала связь.')),
        h('button', { className: 'btn', onClick: props.onRetry }, icon('refresh', { size: 19 }), ' Повторить'));
    }
    return h('div', { className: 'screen' },
      h('div', { className: 'empty' }, icon('sliders', { size: 46 }), h('div', null, 'Загружаю админку…')));
  }

  function App() {
    var s0 = useState(BOOT.screen); var screen = s0[0], setScreen = s0[1];
    var s1 = useState(BOOT.screen === 'hub' ? [] : ['hub']); var stack = s1[0], setStack = s1[1];
    // Состояние ленивой админки: 'idle' | 'loading' | 'ready' | 'error'. Рисовать экран или
    // заглушку решаем по самим глобалам (adminReady), а состояние нужно, чтобы отличить
    // «ещё едет» от «не доехало» и чтобы React перерисовался, когда файлы приехали.
    var s1a = useState('idle'); var adminLoad = s1a[0], setAdminLoad = s1a[1];
    var s1b = useState(0); var adminTry = s1b[0], setAdminTry = s1b[1];   // счётчик «Повторить»
    var s2 = useState(null); var options = s2[0], setOptions = s2[1];
    var s3 = useState(null); var model = s3[0], setModel = s3[1];
    var s4 = useState({ plastic_id: 0, color: '', colors_sel: [], qty: 1, quality_id: 0, strength_id: 0, promo: '', multicolor: false, plates: null, plate_colors: {}, plate_plastics: {}, comment: '', scale_pct: 100, urgent: false, options: [] }); var cfg = s4[0], setCfgRaw = s4[1];
    var s5 = useState(null); var result = s5[0], setResult = s5[1];
    var s6 = useState([]); var cart = s6[0], setCart = s6[1];
    var s7 = useState(null); var toastMsg = s7[0], setToastMsg = s7[1];
    var s8 = useState(0); var secs = s8[0], setSecs = s8[1];
    var s9 = useState(false); var busy = s9[0], setBusy = s9[1];
    var s9b = useState(false); var loadingModel = s9b[0], setLoadingModel = s9b[1];
    var sA = useState([]); var orders = sA[0], setOrders = sA[1];
    var sB = useState([]); var collection = sB[0], setCollection = sB[1];
    var sBC = useState([]); var collectionCats = sBC[0], setCollectionCats = sBC[1];   // разделы каталога (c9c42789)
    var sC = useState([]); var mymodels = sC[0], setMymodels = sC[1];
    var sD = useState(BOOT.go === 'chat' ? { id: BOOT.id, label: '' } : { id: null, label: '' }); var chat = sD[0], setChat = sD[1];
    var sE = useState(BOOT.go === 'adminorder' ? BOOT.id : null); var adminOrderId = sE[0], setAdminOrderId = sE[1];
    var sE2 = useState(BOOT.go === 'client' ? BOOT.id : null); var adminClientId = sE2[0], setAdminClientId = sE2[1];   // CRM (a8b591cf)
    // Открытый проект мастерской (Gridfinity): экран проекта читает id отсюда, как
    // карточка заказа — adminOrderId. Deep-link ?go=gfx&id=N ведёт прямо в проект.
    var sGF = useState(BOOT.go === 'gfx' ? BOOT.id : null); var gfxProjectId = sGF[0], setGfxProjectId = sGF[1];
    var sE3 = useState(BOOT.go === 'printclassify' ? BOOT.id : null); var printClassifyJid = sE3[0], setPrintClassifyJid = sE3[1];   // «чья печать?» (4effbc1b)
    var sE3 = useState(''); var adminOrdersFilter = sE3[0], setAdminOrdersFilter = sE3[1];   // предфильтр админ-заказов
    var bootTabRef = useRef(BOOT.tab || '');   // ?tab= из deep-link, потребляется один раз (takeBootTab)
    var sF = useState(null); var errMsg = sF[0], setErrMsg = sF[1];   // Task C: ошибка с мемом-оверлеем
    var sF2 = useState(''); var errSearch = sF2[0], setErrSearch = sF2[1];   // запрос для кнопки «Найти на Printables»
    var sF3 = useState(''); var errUrl = sF3[0], setErrUrl = sF3[1];   // ссылка для «Отправить мастеру на разбор» (26196ac1)
    var sF4 = useState(''); var errUnslicedToken = sF4[0], setErrUnslicedToken = sF4[1];   // токен для «Отправить мастеру на оценку» (55a1cf8f)
    // Мастерская офлайн (Э2): источник модели ({url}|{file}|{kind,id,label}), который можно
    // оставить заказом без цены, и ключ идемпотентности этого предложения (двойное нажатие и
    // повтор на плохой связи дают одну заявку). Пока не null — оверлей ошибки предлагает заказ.
    var sF5 = useState(null); var deferSrc = sF5[0], setDeferSrc = sF5[1];
    var deferKey = useRef('');
    var sG = useState(''); var pendingSearch = sG[0], setPendingSearch = sG[1];   // префилл поиска в ModelInput
    var sH = useState(null); var headerProfile = sH[0], setHeaderProfile = sH[1];   // мини-сводка профиля для чипа в шапке
    var sI = useState(null); var customModel = sI[0], setCustomModel = sI[1];   // выбранный параметрический конструктор (кастомайзер)
    // Карточка товара (OpenTodo ae038968, фидбэк владельца 2026-07-10): данные плитки каталога,
    // на которых Quote рендерится МГНОВЕННО, пока модель/цена ещё грузятся фоном. null — обычный
    // поток (ссылка/файл/повтор заказа), объект — открыта карточка из каталога/ленты/поиска.
    var sJ = useState(null); var product = sJ[0], setProduct = sJ[1];
    // Глубина центрального стека «Назад» (back.js) — стрелка в шапке живёт на ВСЕХ экранах
    // (фидбэк владельца 2026-07-11) и должна реагировать на слои вне React-состояния App
    // (открытый раздел каталога, лайтбоксы). Подписка — в useEffect ниже.
    var sK = useState(0); var backDepth = sK[0], setBackDepth = sK[1];
    // Избранное (сердечки на модельках каталога): {id: 1}. Единственный источник для
    // сердечек на всех плитках; грузится из /api/options.fav_ids, меняется оптимистично.
    var sM = useState({}); var favs = sM[0], setFavs = sM[1];
    // «Параметры на экране новее посчитанной цены» (71e15fe6): выставляется синхронно правкой на
    // смете (tweak), снимается, когда тихий пересчёт долетел. Пока true — заказ/корзина
    // заблокированы, иначе можно оформить заказ по устаревшему серверному снапшоту.
    var sN = useState(false); var recalcPending = sN[0], setRecalcPending = sN[1];

    var scrollRef = useRef(null), toastTimer = useRef(null), slTimer = useRef(null);
    // Позиция скролла по экранам (фидбэк владельца 2026-07-12: «при возврате на страницу
    // скролл сбрасывается на 0»). scrollPos хранит scrollTop каждого ВИДА, обновляется на
    // onScroll контейнера. Направление берём из back.js (PS_Back.dir): 'back' → эффект ниже
    // ВОССТАНАВЛИВАЕТ сохранённую позицию (вернулись на ту же страницу), 'forward' (новый
    // контент) — как раньше, scrollTop=0.
    var scrollPos = useRef({}), navDir = useRef('forward');
    // Якорь прокрутки: КАКАЯ карточка стояла у верхнего края, а не сколько пикселей промотано
    // (фидбэк владельца 03.09.2026 к OpenTodo cc642555: «скачет скролбар — не виден тот же
    // элемент»). Одной пиксельной позиции мало: карточки ленты и выдачи поиска грузят фото
    // лениво, до загрузки картинка занимает min-height, после — свою настоящую высоту. Возврат
    // ставил scrollTop и на этом заканчивал, а дозагрузка фото ВЫШЕ по ленте раздвигала контент,
    // и под тем же scrollTop оказывалась уже другая карточка. Здесь запоминается ключ элемента
    // [data-sanchor] и его отступ от верха окна прокрутки, а восстановление держит именно этот
    // элемент на месте, пока высоты вокруг устаканиваются. Элементы помечает тот, кто рисует
    // список (Masonry в screens_core.js); экраны без пометок работают как раньше, по пикселям.
    var scrollAnchor = useRef({}), anchorTimer = useRef(null), scrollKeyRef = useRef('');
    // Топовый видимый якорь: первый по вертикали элемент, чей низ ещё не ушёл за верхний край.
    function noteAnchor(el, key) {
      if (!el || !el.querySelectorAll) return;
      var top = el.getBoundingClientRect().top;
      var list = el.querySelectorAll('[data-sanchor]');
      var best = null, bestTop = 0;
      for (var i = 0; i < list.length; i++) {
        var r = list[i].getBoundingClientRect();
        if (r.bottom - top < 1) continue;                       // уже ушёл вверх целиком
        if (!best || r.top < bestTop) { best = list[i]; bestTop = r.top; }
      }
      if (best) scrollAnchor.current[key] = { k: best.getAttribute('data-sanchor'), d: bestTop - top };
      else delete scrollAnchor.current[key];
    }
    // Подвид внутри экрана, по одному на экран (OpenTodo 861eab19: «при возврате из раздела
    // скролл перескакивает на непонятную позицию, и не только тут»). Контейнер прокрутки один
    // на всё приложение, а часть экранов подменяет контент БЕЗ смены screen: раздел каталога,
    // вкладки tabbed()-обёрток админки, вкладки поиска моделей. Раньше все они писали позицию
    // в один ключ screen и читали её же — открытый раздел затирал позицию списка разделов, а
    // при закрытии эффект даже не перезапускался (screen-то не менялся), и список оставался
    // проскроллен на позицию раздела. Ключ = screen + подвид → у каждого вида своя позиция.
    // Храним по экранам, чтобы подвид каталога не протекал на посторонние экраны и переживал
    // уход на карточку товара с возвратом обратно (Collection восстанавливает раздел сам).
    var sV = useState({}); var subViews = sV[0], setSubViews = sV[1];
    // Экран сообщает свой подвид ИЗ ОБРАБОТЧИКА (не из эффекта) — тогда смена подвида и смена
    // контента попадают в один коммит React, и менеджер скролла не успевает отработать по
    // промежуточному ключу.
    function setView(name) {
      setSubViews(function (mp) {
        var v = name || '';
        if ((mp[screen] || '') === v) return mp;
        var n = Object.assign({}, mp); n[screen] = v; return n;
      });
    }
    var scrollKey = screen + '\u0000' + (subViews[screen] || '');
    var calcGuard = useRef(null);   // токен активного слайса; null → ответ calc игнорируется (ae4c6c13)
    var calcSeq = useRef(0);        // номер последнего запущенного calc: поздний ответ старого
                                    // пересчёта не должен перетирать свежий (ранний выбор цвета)
    var calcPending = useRef(0);    // seq НЕтихого calc в полёте (0 — нет): выход с экрана
                                    // slicing при живом расчёте = авто-«Уведомить, когда посчитается»
    var orderNowInflight = useRef(false);   // синхронный гард order_now: два тапа в один кадр (busy
                                            // ещё не закоммичен) не отправят два запроса (f1e80d67)
    var productGuard = useRef(null);   // id карточки товара, чей API.use сейчас ждём (ae038968)
    // Свежая копия cfg для асинхронных цепочек (openProduct → API.use → applyModel): замыкания
    // держат cfg того рендера, где создавались, а юзер мог успеть выбрать цвет на карточке.
    var cfgRef = useRef(cfg);
    useEffect(function () { cfgRef.current = cfg; }, [cfg]);
    // То же для модели — нужен отложенному пересчёту со сметы (tweak): к моменту, когда
    // сработает его таймер, замыкание того рендера может держать уже неактуальную модель.
    var modelRef = useRef(model);
    useEffect(function () { modelRef.current = model; }, [model]);
    // Юзер потрогал пластик/цвет на карточке ДО загрузки модели — applyModel не должен
    // перетирать его выбор дефолтами (фидбэк владельца 2026-07-11: контролы доступны сразу).
    var productTouched = useRef(false);
    // Отложенное действие карточки (фидбэк 2026-07-11: «нельзя сразу оставить заказ»):
    // «Оставить заказ»/«В корзину» работают ДО готовой цены — намерение запоминается и
    // выполняется автоматически, как только расчёт завершится (эффект на [result] ниже).
    var sL = useState(null); var pendingAct = sL[0], setPendingActRaw = sL[1];   // 'order'|'cart'|null
    var pendingActRef = useRef(null);
    function setPendingAct(v) { pendingActRef.current = v; setPendingActRaw(v); }
    var paramPreselRef = useRef(null);   // пресет пластика/цвета из кастомайзера → применяется в applyModel
    var userId = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) || '';

    function setCfg(patch) { setCfgRaw(function (c) { return Object.assign({}, c, patch); }); }

    // Task C: показываем ошибку in-app оверлеем с мемом (PS_ErrorOverlay). Этот alertMsg
    // перекрывает модульный (тот служит фолбэком, если компонент не подгрузился). Все
    // существующие вызовы alertMsg(...) внутри App автоматически идут через мем-оверлей.
    // failedUrl — ссылка, которую не удалось обработать: оверлей предложит «Отправить мастеру
    // на разбор» (очередь разбора, OpenTodo 26196ac1).
    // unslicedToken (55a1cf8f): слайс не удался, но токен модели живой — оверлей предложит
    // «Отправить мастеру на оценку» вместо тупикового «Понятно».
    function alertMsg(m, search, failedUrl, unslicedToken) {
      if (window.PS_ErrorOverlay) { haptic('heavy'); setErrSearch(search || ''); setErrUrl(failedUrl || ''); setErrUnslicedToken(unslicedToken || ''); setErrMsg(m || 'Что-то пошло не так'); return; }
      try { if (tg && tg.showAlert) { tg.showAlert(m); return; } } catch (e) {}
      window.alert(m);
    }
    // Заказ без цены, пока мастерская офлайн (Э2): оверлей ошибки с полем пожеланий и кнопкой.
    function offerDefer(src, msg) {
      deferKey.current = 'w' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
      setDeferSrc(src);
      setErrSearch(''); setErrUrl(''); setErrUnslicedToken('');
      setErrMsg(msg || 'Мастерская сейчас офлайн — оставьте заказ, цену пришлём в чат.');
    }
    function placeDeferred(comment) {
      var src = deferSrc;
      if (!src || busy) return;
      setBusy(true);
      API.orderDeferred(src, comment, deferKey.current).then(function (j) {
        setBusy(false);
        if (j && j.ok) {
          setErrMsg(null); setDeferSrc(null);
          var done = j.text || 'Заявка принята — цену пришлём в чат.';
          try { if (tg && tg.showAlert) { tg.showAlert(done); return; } } catch (e) {}
          toast(done);
          return;
        }
        toast((j && j.error) || 'Не удалось оставить заявку');
      }).catch(function () { setBusy(false); toast('Сеть недоступна'); });
    }
    // Отправить нерабочую ссылку мастеру на разбор (очередь разбора, OpenTodo 26196ac1).
    function queueForMaster(url, errText) {
      API.calcQueue(url, errText).then(function (j) {
        setErrMsg(null); setErrUrl('');
        toast(j.ok ? 'Отправлено мастеру — он разберётся и ответит' : (j.error || 'Не удалось'));
      }).catch(function () { toast('Сеть недоступна'); });
    }
    // Префилл поиска моделей (кнопка «Найти на Printables» из оверлея блока MakerWorld).
    function searchModels(query) { setErrMsg(null); setErrSearch(''); setPendingSearch(query || ''); if (screen !== 'model') go('model'); }
    function clearPendingSearch() { setPendingSearch(''); }

    // ---- load options once ----
    useEffect(function () {
      API.options().then(function (o) {
        if (!o || !o.ok) { alertMsg('Не удалось загрузить данные'); return; }
        setOptions(o);
        // Возможности сборки (f98aa1ef): разделы, которых в этой сборке нет, не рисуем.
        // Флага нет (старый сервер) — считаем включённым: скрыть по незнанию хуже.
        window.__FEATURES__ = o.features || {};
        if (o.fav_ids && o.fav_ids.length) {
          var fv = {};
          o.fav_ids.forEach(function (i) { fv[i] = 1; });
          setFavs(fv);
        }
        var dp = (o.plastics || []).filter(function (p) { return p.is_default; })[0] || (o.plastics || [])[0] || {};
        var dq = (o.qualities || []).filter(function (q) { return q.is_default; })[0] || (o.qualities || [])[0] || {};
        var ds = (o.strengths || []).filter(function (x) { return x.is_default; })[0] || (o.strengths || [])[0] || {};
        // лучший профиль печати пластика по умолчанию (94cc52c7), иначе дефолтное качество
        setCfgRaw({ plastic_id: dp.id || 0, color: (dp.colors || [])[0] || '', qty: 1,
          quality_id: dp.best_quality_id || dq.id || 0, strength_id: ds.id || 0, promo: '',
          multicolor: false, comment: '', urgent: false, options: [] });
      });
    }, []);

    // ---- профиль для чипа в шапке (грузим один раз; экран профиля обновит его сам) ----
    useEffect(function () { API.profile().then(function (j) { if (j && j.ok) setHeaderProfile(j); }); }, []);

    // Серверная копия корзины (idea #10): синхронизируем при изменении (debounced), чтобы нудж о
    // брошенной корзине знал о ней. Пустая корзина → бэк удалит запись.
    useEffect(function () {
      var t = setTimeout(function () {
        var items = cart.map(function (c) { return { token: c.token, label: c.label, totalStr: c.totalStr }; });
        var sum = cart.reduce(function (a, c) { return a + (parseFloat(String(c.totalStr || '').replace(/[^\d.,]/g, '').replace(',', '.')) || 0); }, 0);
        var total = sum > 0 ? (sum.toFixed(2).replace('.', ',') + ' ' + ((options && options.currency) || 'руб.')) : '';
        API.cartSync(items, total).catch(function () {});
      }, 1500);
      return function () { clearTimeout(t); };
    }, [cart]);

    // ---- deep-link «расчёт готов» (ae4c6c13): go=quote&t=<токен> → грузим смету с сервера ----
    useEffect(function () {
      if (BOOT.go !== 'quote' || !BOOT.t) return;
      API.quoteState(BOOT.t).then(function (j) {
        if (j && j.ok) { setModel(j.model); setResult({ ok: true, result: j.result }); syncBackStack(['hub']); setStack(['hub']); setScreen('quote'); }
        else alertMsg((j && j.error) || 'Расчёт устарел — посчитайте заново');
      }).catch(function () {});
    }, []);

    // Мгновенная MW-смета (97a91eee): цена показана из данных MakerWorld, а честный слайс
    // бежит на сервере фоном — поллим quote_state, пока payload.refining, и молча
    // подменяем цифры, когда слайс дорешал.
    useEffect(function () {
      if (screen !== 'quote' || !model || !model.token) return;
      if (!(result && result.result && result.result.refining)) return;
      var stop = false; var n = 0;
      var t = setInterval(function () {
        if (stop || ++n > 60) { clearInterval(t); return; }
        API.quoteState(model.token).then(function (j) {
          if (stop || !j || !j.ok || !j.result || j.result.refining) return;
          stop = true; clearInterval(t);
          setResult({ ok: true, result: j.result });
          if (!j.result.approx) toast('Цена уточнена по слайсу');
        }).catch(function () {});
      }, 4000);
      return function () { stop = true; clearInterval(t); };
    }, [screen, result, model]);

    // Юзер ВЫШЕЛ из потока во время слайса (шеврон/жест «назад», корзина/админка из шапки) —
    // авто-«Уведомить, когда посчитается» (2026-07-11): молча подписываемся, бот дошлёт цену
    // сообщением, как если бы юзер нажал кнопку сам. Программные уходы slicing→quote (успех)
    // и slicing→config (ошибка расчёта) — не выход, их пропускаем. Закрытие мини-аппа целиком
    // ловит сервер (разрыв соединения /api/calc), тут только внутренняя навигация.
    var prevScreenRef = useRef(screen);
    useEffect(function () {
      var was = prevScreenRef.current; prevScreenRef.current = screen;
      if (was !== 'slicing' || screen === 'quote' || screen === 'config') return;
      if (!calcPending.current || !model || !model.token) return;
      calcPending.current = 0;
      calcGuard.current = null;               // поздний ответ calc не должен дёргать экран
      clearInterval(slTimer.current);
      API.calcNotify(model.token).then(function (j) {
        if (j && j.ok) toast('Пришлю цену сообщением в чат');
      }).catch(function () {});
    }, [screen]);

    // ---- Telegram chrome ----
    // BackButton теперь целиком на центральном стеке window.PS_Back (back.js, OpenTodo
    // 85d16a2a) — он сам показывает/прячет кнопку и слушает 'backButtonClicked'; здесь только
    // разовая настройка вида окна. disableVerticalSwipes (Bot API 7.7+) не даёт вертикальному
    // свайпу сворачивать/закрывать мини-апп поверх вложенных экранов; старые клиенты — no-op.
    useEffect(function () {
      if (!tg) return;
      try {
        tg.ready(); tg.expand();
        tg.setHeaderColor && tg.setHeaderColor('#0a0e12'); tg.setBackgroundColor && tg.setBackgroundColor('#0e1217');
        if (typeof tg.disableVerticalSwipes === 'function') tg.disableVerticalSwipes();
      } catch (e) {}
    }, []);
    // Начальный экран из deep-link (?go=...) кладёт в стек ['hub'] ещё до первого рендера
    // (см. useState стека выше) — синхронизируем это с PS_Back один раз на монтировании,
    // иначе кнопка «Назад»/жест не покажутся сразу на первом же нехабовом экране.
    useEffect(function () {
      if (stack.length && window.PS_Back) stack.forEach(function () { window.PS_Back.push(doBack); });
    }, []);
    // Стрелка «назад» в шапке — на всех экранах (фидбэк владельца 2026-07-11): чтобы она знала
    // о слоях вне React-состояния App (раздел каталога, лайтбоксы), подписываемся на back.js.
    useEffect(function () {
      if (!window.PS_Back || !window.PS_Back.onChange) return;
      setBackDepth(window.PS_Back.size());
      return window.PS_Back.onChange(setBackDepth);
    }, []);

    // Направление фиксируем в useLayoutEffect — он отрабатывает ДО пассивных эффектов детей,
    // а те могут положить свой слой на стек «Назад» и тем самым пометить направление 'forward'
    // раньше, чем мы успеем его прочитать (Collection на монтировании возвращает слой ранее
    // открытого раздела — возврат с карточки товара иначе бы уезжал в начало списка).
    // Помечаем 'forward' сразу: один «назад» восстанавливает позицию ровно один раз.
    var navBack = useRef(false);
    React.useLayoutEffect(function () {
      var PB = window.PS_Back;
      navBack.current = (PB && PB.dir) ? PB.dir() === 'back' : navDir.current === 'back';
      scrollKeyRef.current = scrollKey;   // по нему отложенный расчёт якоря узнаёт, что экран сменился
      navDir.current = 'forward';
      if (PB && PB.markForward) PB.markForward();
    }, [scrollKey]);

    useEffect(function () {
      var el = scrollRef.current; if (!el) return;
      var saved = scrollPos.current[scrollKey];
      var anc = scrollAnchor.current[scrollKey];
      var back = navBack.current && saved != null;
      // Вперёд (новый контент) → в начало.
      if (!back) { el.scrollTop = 0; return; }
      // Назад на ту же страницу → вернуть сохранённую позицию. НО контент части экранов —
      // локальный стейт, перезагружаемый при возврате (лента «Рекомендуем» на hub грузится
      // с сервера при каждом входе): в момент эффекта высота = высоте вьюпорта, и scrollTop=saved
      // зажимается в 0. Поэтому переприменяем позицию, пока контент дорастает до неё — по событию
      // роста (ResizeObserver) и лесенкой setTimeout (rAF не годится: он ставится на паузу, если
      // вебвью скрыт). Прерываемся, когда позиция достигнута, юзер сам скроллит, или по таймауту.
      var target = saved, done = false, start = Date.now(), ro = null, timers = [];
      function finish() {
        done = true;
        timers.forEach(clearTimeout);
        if (ro) { ro.disconnect(); ro = null; }
        el.removeEventListener('wheel', finish); el.removeEventListener('touchstart', finish);
        el.removeEventListener('mousedown', finish);
      }
      // Элемент-якорь ищем перебором, а не селектором: ключ карточки — это её id
      // ('makerworld:1042', '#136'), в CSS-селекторе его пришлось бы экранировать.
      function anchorNode() {
        if (!anc) return null;
        var list = el.querySelectorAll('[data-sanchor]');
        for (var i = 0; i < list.length; i++) {
          if (list[i].getAttribute('data-sanchor') === anc.k) return list[i];
        }
        return null;
      }
      function apply() {
        if (done || scrollRef.current !== el) return;
        var node = anchorNode();
        if (node) {
          // Держим ту же карточку на том же месте: считаем не от запомненных пикселей, а от её
          // ФАКТИЧЕСКОГО положения сейчас — тогда любой рост высот выше по списку (дозагрузка
          // фото, приехавшая страница ленты) сдвигает нас ровно на столько же, и глазу карточка
          // остаётся неподвижной. Лесенка и ResizeObserver повторяют это, пока список растёт.
          var d = node.getBoundingClientRect().top - el.getBoundingClientRect().top;
          var next = el.scrollTop + d - anc.d;
          el.scrollTop = next < 0 ? 0 : next;
          if (Date.now() - start > 2500) finish();
          return;
        }
        el.scrollTop = target;
        if ((el.scrollHeight - el.clientHeight) >= target - 1 || Date.now() - start > 2500) finish();
      }
      el.addEventListener('wheel', finish, { passive: true });     // пользователь сам скроллит → не мешаем
      el.addEventListener('touchstart', finish, { passive: true });
      el.addEventListener('mousedown', finish, { passive: true }); // потянул полосу прокрутки мышью
      if (window.ResizeObserver) {
        ro = new ResizeObserver(apply);
        ro.observe(el);
        if (el.firstElementChild) ro.observe(el.firstElementChild);   // рост контента экрана
      }
      timers = [0, 60, 150, 300, 500, 800, 1200, 1800, 2500].map(function (ms) { return setTimeout(apply, ms); });
      // Жёсткий предел: последняя ступень лесенки может прийти ровно в 2500 мс и не пройти
      // проверку «> 2500», а ResizeObserver без finish() держал бы якорь бесконечно.
      timers.push(setTimeout(finish, 3000));
      apply();   // сразу — если контент уже достаточной высоты
      return finish;
    }, [scrollKey]);

    // Экранная навигация подключена к центральному стеку «Назад» (window.PS_Back, back.js,
    // OpenTodo 85d16a2a): каждый go() кладёт свой уровень, свайп/аппаратная кнопка «назад»
    // снимают уровни по одному в обратном порядке. syncBackStack() держит PS_Back в
    // соответствии с прыжками сразу в корень (goTab/reset/afterOrder и подобные setStack(...)).
    function doBack() { navDir.current = 'back'; setStack(function (st) { if (!st.length) return st; setScreen(st[st.length - 1]); return st.slice(0, -1); }); }
    function syncBackStack(newStack) {
      if (!window.PS_Back) return;
      window.PS_Back.clear();
      newStack.forEach(function () { window.PS_Back.push(doBack); });
    }
    function go(sc) { setStack(function (st) { return st.concat([screen]); }); setScreen(sc); haptic(); if (window.PS_Back) window.PS_Back.push(doBack); }
    // back() — «сработай, будто нажали системную кнопку/жест назад»: и шеврон в шапке, и
    // BackButton/попстейт-фолбэк идут через один PS_Back.back(), поэтому какой бы путь
    // закрытия ни сработал первым, экранный стек не рассинхронизируется.
    function back() { if (window.PS_Back) window.PS_Back.back(); else doBack(); }
    // product сбрасывается здесь (ae038968): «Заново» не должно оставлять карточку товара
    // просочившейся в обычный поток (loadUrl/loadFile/useModel/loadParametric уже сами её сбрасывают).
    function reset() { setProduct(null); syncBackStack([]); setStack([]); setScreen('hub'); }
    // Переключение корневого таба. Фидбэк владельца 2026-07-11: «назад» ВСЕГДА возвращает на
    // шаг и никогда не закрывает приложение — поэтому не-hub табы держат под собой один уровень
    // «hub» (стрелка/жест с корня таба ведут на главную, а не выкидывают из мини-аппа). Табы —
    // сиблинги, а не цепочка: переключение Каталог→Заказы не наслаивает уровни, под низом всё
    // так же один hub. Сам hub — корень: стек пустой.
    function goTab(sc) {
      if (screen === sc) return;
      var base = sc === 'hub' ? [] : ['hub'];
      syncBackStack(base); setStack(base); setScreen(sc); haptic();
    }
    // После оформления заказа ведём в «Мои заказы», но оставляем hub в стеке — иначе кнопка
    // «Назад» прячется (показывается только при непустом стеке) и с заказов не выбраться (fb4e46b7).
    function afterOrder() { syncBackStack(['hub']); setStack(['hub']); setScreen('orders'); haptic(); }

    function toast(m) { setToastMsg(m); clearTimeout(toastTimer.current); toastTimer.current = setTimeout(function () { setToastMsg(null); }, 2400); }

    // Самый дешёвый активный пластик — стартовая цена «от» (OpenTodo 050aff56)
    function cheapestPlastic() {
      var ps = (options && options.plastics) || [], best = null;
      ps.forEach(function (p) { if (!best || Number(p.price) < Number(best.price)) best = p; });
      return best;
    }
    // Дефолтный пластик карточки товара (OpenTodo ae038968, решение владельца): «каждая модель
    // открывается сначала с Elegoo Matte Тёмно-синий». options.default_plastic_id/default_color
    // приходят с бэка (_default_mw_plastic_color) — ищем эту катушку в актуальном списке пластиков;
    // не нашли (переименовали/удалили катушку) → null, вызывающий код откатится на cheapestPlastic.
    function defaultPlastic() {
      var did = options && options.default_plastic_id;
      if (!did) return null;
      return ((options && options.plastics) || []).filter(function (p) { return p.id === did; })[0] || null;
    }
    function applyModel(j, quiet) {
      setModel(j);
      // новая модель → сброс столов/цветов/комментария; параметры: пресет повтора, иначе
      // самый дешёвый пластик — и СРАЗУ считаем (быстрый путь «ссылка → цена», 050aff56)
      var patch = { plates: null, plate_colors: {}, plate_plastics: {}, comment: '', scale_pct: 100, urgent: false, options: [] };
      var cp = paramPreselRef.current; paramPreselRef.current = null;   // пресет из кастомайзера (пластик/цвет)
      if (j.presel) {
        patch.plastic_id = j.presel.plastic_id || cfg.plastic_id;
        patch.quality_id = j.presel.quality_id || cfg.quality_id;
        patch.strength_id = j.presel.strength_id || cfg.strength_id;
        patch.qty = j.presel.qty || 1;
      } else if (cp) {
        if (cp.plastic_id) patch.plastic_id = cp.plastic_id;
        if (cp.color != null) patch.color = cp.color;
        patch.multicolor = !!cp.multicolor;
        patch.qty = 1;
        if (cp.comment) patch.comment = cp.comment;
        var cpl = ((options && options.plastics) || []).filter(function (pp) { return pp.id === cp.plastic_id; })[0];
        if (cpl && cpl.best_quality_id) patch.quality_id = cpl.best_quality_id;
      } else if (quiet && productTouched.current) {
        // Юзер уже выбрал пластик/цвет на карточке, пока модель грузилась (фидбэк 2026-07-11:
        // контролы доступны сразу) — его выбор в cfgRef, дефолтами не перетираем.
      } else {
        // Без пресета/пресета кастомайзера: открываем дефолтным пластиком карточки товара
        // (OpenTodo ae038968 — Elegoo Matte Тёмно-синий), а не самым дешёвым. Нет дефолта на
        // бэке (катушку переименовали/удалили) → фолбэк как раньше, cheapestPlastic.
        var dp = defaultPlastic();
        var ch = dp || cheapestPlastic();
        if (ch) {
          patch.plastic_id = ch.id;
          var dcol = options && options.default_color;
          // цвет ДИЗАЙНА из .3mf (mc_colors[0]) → ближайший доступный в палитре пластика:
          // модель открывается «в своём» цвете, а не дефолтным тёмно-синим (2026-07-23)
          var mcol = (j.mc_colors && j.mc_colors.length) ? nearestPaletteColor(ch, j.mc_colors[0]) : null;
          patch.color = mcol
            || ((dp && dcol && (ch.colors || []).indexOf(dcol) >= 0) ? dcol : ((ch.colors || [])[0] || ''));
          if (ch.best_quality_id) patch.quality_id = ch.best_quality_id;
          patch.qty = 1;
        }
      }
      // База — СВЕЖИЙ cfg (cfgRef): applyModel зовётся из промиса, замыкание держит cfg рендера
      // openProduct-времени, а юзер мог успеть выбрать цвет на карточке (фидбэк 2026-07-11).
      var base = cfgRef.current;
      // Мультицвет (a197bb93): цвета подбираются сразу — слоты «Цвет 1..N». Приоритет —
      // цвета ДИЗАЙНА из .3mf (mc_colors, слот i → ближайший доступный цвет палитры);
      // где дизайн-цвета нет — первые цвета палитры, как раньше. Кастомайзер (cp) решает сам.
      if (j.multicolor) {
        if (!cp) patch.multicolor = true;
        var mpl = ((options && options.plastics) || []).filter(function (pp) { return pp.id === (patch.plastic_id || base.plastic_id); })[0] || {};
        var mpal = mpl.colors || [];
        var design = (j.mc_colors || []).map(function (hx) { return nearestPaletteColor(mpl, hx); });
        patch.colors_sel = Array.from({ length: Math.max(2, j.num_colors || 2) }, function (_, i) {
          return design[i] || mpal[i % Math.max(1, mpal.length)] || '';
        });
      } else { patch.multicolor = false; patch.colors_sel = []; }
      var next = Object.assign({}, base, patch);
      cfgRef.current = next;
      setCfgRaw(next);
      setResult(null);
      doCalc(j, next, quiet);
    }
    // quiet=true (ae038968): фоновая загрузка модели карточки товара — БЕЗ busy/loadingModel-
    // оверлея; на неудаче не мем-оверлей и не alertMsg, а тихая ошибка в app.result (Quote сам
    // покажет инлайн-блок, см. screens_core.js ProductQuote).
    function handleModelResp(j, srcUrl, quiet, src) {
      if (!quiet) setBusy(false);
      // Компьютер мастерской выключен (Э2): посчитать нельзя, но модель уже в руках —
      // предлагаем оставить заказ, цену пришлют в чат. Тихий путь (карточка товара) тоже:
      // клиент сам нажал на модель, иначе карточка висела бы «считаем» без конца.
      if (j && j.pc_offline && j.can_defer && src) {
        paramPreselRef.current = null;
        if (quiet) setResult({ ok: false, error: j.error || 'Мастерская сейчас офлайн' });
        else setLoadingModel(false);
        offerDefer(src, j.error);
        return;
      }
      if (!j || !j.ok) {
        paramPreselRef.current = null;
        if (quiet) { setResult({ ok: false, error: (j && j.error) || 'Не удалось загрузить модель' }); return; }
        setLoadingModel(false); alertMsg((j && j.error) || 'Не удалось загрузить модель', j && j.blocked ? j.search : null, srcUrl || ''); return;
      }
      if (j.variants && j.variants.length) {   // MakerWorld multi-variant → take first for now
        if (!quiet) setBusy(true);              // оверлей загрузки держим до финального ответа (loadingModel)
        var v0 = j.variants[0];                 // сервер отдаёт вариант как {id,title,thumb} — ключ id, не iid
        API.variant(srcUrl, v0.id || v0.iid || v0.instanceId).then(function (jj) { handleModelResp(jj, srcUrl, quiet, src); });
        return;
      }
      if (!quiet) setLoadingModel(false);
      applyModel(j, quiet);
    }
    function loadUrl(url) { setProduct(null); setBusy(true); setLoadingModel(true); API.uploadUrl(url).then(function (j) { handleModelResp(j, url, false, { url: url }); }).catch(function () { setBusy(false); setLoadingModel(false); alertMsg('Сеть недоступна'); }); }
    function loadFile(file) { setProduct(null); setBusy(true); setLoadingModel(true); API.uploadFile(file).then(function (j) { handleModelResp(j, '', false, { file: file }); }).catch(function () { setBusy(false); setLoadingModel(false); alertMsg('Сеть недоступна'); }); }
    function useModel(kind, id) { setProduct(null); setBusy(true); setLoadingModel(true); API.use(kind, id).then(function (j) { handleModelResp(j, '', false, { kind: kind, id: id }); }).catch(function () { setBusy(false); setLoadingModel(false); alertMsg('Сеть недоступна'); }); }
    // Параметрическая модель (OpenTodo c1cc840a): генерим STL на сервере (OpenSCAD) и дальше — как обычная модель.
    // presel (опц., из кастомайзера): {plastic_id, color, multicolor, comment} — применится в applyModel.
    function loadParametric(modelId, params, presel) {
      paramPreselRef.current = presel || null;
      setProduct(null);
      setBusy(true); setLoadingModel(true);
      API.parametricModel(modelId, params).then(function (j) { handleModelResp(j, ''); })
        .catch(function () { paramPreselRef.current = null; setBusy(false); setLoadingModel(false); alertMsg('Сеть недоступна'); });
    }
    // Кастомайзер коллекции: кладём выбранный конструктор в стейт и открываем экран настройки.
    function openCustomizer(item) { setCustomModel(item); go('customizer'); }
    // ── Карточка товара: мгновенное открытие (OpenTodo ae038968, фидбэк владельца 2026-07-10) ──
    // «При нажатии на товар мы ОТКРЫВАЕМ СТРАНИЧКУ с картинкой, слайсинг идёт НА ФОНЕ». item —
    // объект плитки каталога/ленты/поиска ({id,label,preview,dims,price,plates,printed,...}),
    // уже есть на фронте — экран quote рендерится ИЗ НЕГО сразу, без ожидания сети. Модель и
    // честная цена подгружаются молча следом (handleModelResp/applyModel/doCalc с quiet=true).
    function openProduct(item) {
      setProduct(item);
      setModel(null);
      setResult(null);
      productTouched.current = false;
      setPendingAct(null);
      // структурный сброс cfg + СРАЗУ дефолтный пластик/цвет (Elegoo Matte Тёмно-синий):
      // кружки цветов видны с первого кадра (фидбэк 2026-07-11) — выбранный должен быть
      // подсвечен, не дожидаясь загрузки модели
      var dp = defaultPlastic(), ch = dp || cheapestPlastic();
      var dcol = options && options.default_color;
      var patch = { plates: null, plate_colors: {}, plate_plastics: {}, comment: '', scale_pct: 100, qty: 1, urgent: false, options: [] };
      if (ch) {
        patch.plastic_id = ch.id;
        patch.color = (dp && dcol && (ch.colors || []).indexOf(dcol) >= 0) ? dcol : ((ch.colors || [])[0] || '');
      }
      var next = Object.assign({}, cfgRef.current, patch);
      cfgRef.current = next;
      setCfgRaw(next);
      go('quote');
      // guard от гонки двух быстрых тапов: поздний ответ ПЕРВОЙ модели не должен перезаписать
      // карточку второй (иначе картинка одна, а цена/модель — другие)
      productGuard.current = item.id;
      API.use('collection', item.id).then(function (j) {
        if (productGuard.current !== item.id) return;
        handleModelResp(j, '', true, { kind: 'collection', id: item.id, label: item.label || '' });
      }).catch(function () {
        if (productGuard.current !== item.id) return;
        handleModelResp({ ok: false, error: 'Сеть недоступна' }, '', true);
      });
    }
    // Отложенное действие карточки (фидбэк 2026-07-11): нажали «Оставить заказ»/«В корзину»,
    // пока цена считалась — выполняем сразу после прихода результата. Эффект (а не вызов из
    // doCalc): после коммита placeOrder/addCart видят СВЕЖИЕ model/result/cfg. Слайс тихо
    // упал, а юзер хотел заказ → автоматически уходим в заявку без цены (мастер оценит) —
    // намерение «закажи это» выполняется в любом случае.
    useEffect(function () {
      var pa = pendingActRef.current;
      if (!pa || !result) return;
      if (result.ok) {
        setPendingAct(null);
        if (pa === 'order') placeOrder();
        else if (pa === 'cart') addCart();
      } else if (result.ok === false) {
        setPendingAct(null);
        if (pa === 'order' && result.can_order_unsliced && result.token) orderUnslicedFor(result.token);
      }
    }, [result]);
    // Ранний выбор пластика/цвета на карточке (фидбэк 2026-07-11: кружки доступны сразу).
    // Смета ещё не готова → requote невозможен (на сервере нет last): запоминаем выбор в cfg;
    // модель уже загрузилась — тихо пересчитываем с ним, ещё грузится — applyModel возьмёт
    // выбор из cfgRef (см. productTouched) и посчитает сразу правильным пластиком.
    function pickColor(pid, color) {
      productTouched.current = true;
      var next = Object.assign({}, cfgRef.current, { plastic_id: pid, color: color });
      cfgRef.current = next;
      setCfgRaw(next);
      if (model && model.token) doCalc(model, next, true);
    }
    // Смена ПЛАСТИКА одноцветной модели прямо на смете (OpenTodo: «если на пластине один цвет —
    // менять не только цвет, но и пластик»). У мультицвета со слотами смета даёт лишь выбор ЦВЕТА;
    // но когда цвет один, смена пластика однозначна — ремапим ВСЕ слоты на палитру нового пластика
    // (первый цвет, distinct остаётся 1), берём его лучший профиль печати и делаем ПОЛНЫЙ пересчёт:
    // плотность/профиль материала меняют цену и время (approx-requote для мультицвета вообще
    // заблокирован на бэке как mixed). Для одноцветных не-мультицвет цвет уже меняется шариками.
    function setPlastic(pid) {
      var mdl = modelRef.current;
      if (!(mdl && mdl.token) || busy) return;
      var ps = (options && options.plastics) || [];
      var p = ps.filter(function (x) { return x.id === pid; })[0];
      var c = cfgRef.current;
      if (!p || pid === c.plastic_id) return;
      productTouched.current = true;
      var pal = p.colors || [];
      var isMC = mdl.multicolor && c.multicolor;
      var nslots = (c.colors_sel || []).length || Math.max(2, mdl.num_colors || 2);
      var next = Object.assign({}, c, {
        plastic_id: pid, color: pal[0] || '',
        quality_id: p.best_quality_id || c.quality_id,
        colors_sel: isMC ? Array.from({ length: nslots }, function () { return pal[0] || ''; }) : [],
        plate_plastics: {}, plate_colors: {} });
      cfgRef.current = next;
      setCfgRaw(next);
      // Денежный контракт (как в setSlotColor/tweak): пока смета не пересчитана под новый пластик —
      // гасим «Оставить заказ»/«В корзину». doCalc снимет флаг, когда цена снова совпадёт с экраном.
      setRecalcPending(true);
      doCalc(mdl, next, true);
    }
    // Слот мультицвета прямо на карточке (фидбэк 2026-07-11: цвет выбирается заранее и для
    // мультицветных): обновляем слот и тихо пересчитываем — слайс уже в кэше, сервер лишь
    // обновит смету (colors_sel из last уходит в заказ).
    // Правка параметров ПРЯМО на смете (OpenTodo 71e15fe6 — выбор столов с основной страницы
    // заказа, а не только из «Параметров»): патчим cfg и ТИХО пересчитываем, не уходя со сметы.
    // Переключение столов — интерфейс многотаповый (отметить 3 стола из 5), поэтому пересчёт
    // отложенный: летит один запрос по последнему состоянию, а не по каждому тапу. Гонки
    // ответов уже отсекает calcSeq в doCalc, дебаунс тут ради лишних слайсов/трафика.
    var tweakTimer = useRef(null);
    function tweak(patch) {
      productTouched.current = true;
      var next = Object.assign({}, cfgRef.current, patch || {});
      cfgRef.current = next;
      setCfgRaw(next);
      // Флаг «на экране параметры новее, чем цена» выставляем СИНХРОННО, в том же кадре, что и
      // сам патч. Он гасит «Оставить заказ»/«В корзину» и рисует «Пересчитываем цену…».
      // Без него был денежный баг: /api/order строит заказ из СЕРВЕРНОГО снапшота (m["last"],
      // m["plates_sel"]), который переписывается только в конце /api/calc, а тихий пересчёт не
      // трогает busy — значит всё окно (400 мс дебаунса + сам расчёт) кнопка «Оставить заказ»
      // оставалась живой и оформляла заказ по СТАРОМУ набору столов и старой цене, хотя плитки
      // на экране уже показывали новый выбор.
      setRecalcPending(true);
      clearTimeout(tweakTimer.current);
      tweakTimer.current = setTimeout(function () {
        var mdl = modelRef.current;
        if (mdl && mdl.token) doCalc(mdl, cfgRef.current, true);
        else setRecalcPending(false);       // считать нечем — не держим кнопки выключенными
      }, 400);
    }
    var colorsSelSeq = useRef(0);
    var colorsSelTimer = useRef(null);
    // Вариант НАБОРА файлов карточки (87eb9cf3): «Железный человек» = полный 3mf + та же модель
    // россыпью STL — считать надо ОДИН комплект. Сервер пересобирает токен под выбранный key и
    // отвечает той же формой, что /api/model → прогоняем через handleModelResp (тихо): модель
    // подменяется, applyModel пересчитывает смету, 3D/превью дальше живут файлами комплекта.
    function setFileVariant(key) {
      var mdl = modelRef.current;
      if (!mdl || !mdl.token || busy) return;
      setRecalcPending(true);   // денежный контракт: пока смета не от нового комплекта — заказ гасим
      API.fileVariant(mdl.token, key).then(function (j) {
        if ((modelRef.current || {}).token !== mdl.token) return;   // успели открыть другую модель
        if (!j || !j.ok) { setRecalcPending(false); alertMsg((j && j.error) || 'Не удалось сменить вариант'); return; }
        handleModelResp(j, '', true);
      }).catch(function () { setRecalcPending(false); alertMsg('Сеть недоступна'); });
    }
    function setSlotColor(i, c) {
      productTouched.current = true;
      var prev = (cfgRef.current.colors_sel || []).slice();
      var sel = prev.slice();
      sel[i] = c;
      var next = Object.assign({}, cfgRef.current, { colors_sel: sel, color: sel[0] || cfgRef.current.color, plate_colors: {} });
      cfgRef.current = next;
      setCfgRaw(next);   // МГНОВЕННО: v3dColors обновились → 3D перекрасился без сервера
      var mdl = modelRef.current;
      if (!(mdl && mdl.token)) return;
      // Денежный контракт (OpenTodo 2f7b597f): заказ строится из СЕРВЕРНОГО снимка (m["last"]),
      // а слоты меняются локально — раньше выбор цвета «жил» только на экране, пока клиент не
      // пересчитает через «изменить параметры», и заказ уходил со стандартными цветами. Теперь
      // до синхронизации снимка recalcPending гасит «Оставить заказ»/«В корзину».
      setRecalcPending(true);
      // ЧИСЛО разных цветов влияет на продувку многоцвета → полный тихий пересчёт (он же
      // синхронизирует снимок и снимет флаг); иначе — лёгкий /api/colors_sel без переслайса.
      var distinct = function (a) { var s = {}; (a || []).forEach(function (x) { if (x) s[x] = 1; }); return Object.keys(s).length; };
      if (distinct(prev) !== distinct(sel)) { doCalc(mdl, next, true); return; }
      clearTimeout(colorsSelTimer.current);
      colorsSelTimer.current = setTimeout(function () {
        var seq = ++colorsSelSeq.current;
        var cs = calcSeq.current;      // полный пересчёт, начатый ПОСЛЕ нас, главнее (как в requote)
        var tok = (modelRef.current || {}).token;
        if (!tok) { setRecalcPending(false); return; }
        API.colorsSel(tok, (cfgRef.current.colors_sel || []).slice()).then(function (j) {
          if (seq !== colorsSelSeq.current || cs !== calcSeq.current) return;   // обогнали — молчим
          if ((modelRef.current || {}).token !== tok) return;                   // уже другая модель
          if (!j || !j.ok) { alertMsg((j && j.error) || 'Не удалось применить цвета'); return; }
          setRecalcPending(false);
        }).catch(function () {
          if (seq !== colorsSelSeq.current || cs !== calcSeq.current) return;
          alertMsg('Сеть недоступна — цвета применятся при следующей смене');
        });
      }, 300);
    }
    // Постобработка на смете — МГНОВЕННО и ЛОКАЛЬНО (B4-инстант): услуги плоские (фикс-цена,
    // известна клиенту), геометрию не трогают → переслайс/спиннер/блокировка кнопки НЕ нужны, в
    // отличие от tweak. Обновляем cfg.options и пересобираем показанный итог + строки услуг из
    // серверной «голой» базы печати (result.total_unrounded, срочность в ней уже учтена):
    // total = ceil10(база + Σ выбранных услуг) — тем же округлением, что pricing.apply_options.
    // Выбор уедет в /api/order (сервер переприложит его авторитетно, без переслайса).
    function applyOptionsLocal(nextOptions) {
      var next = Object.assign({}, cfgRef.current, { options: (nextOptions || []).slice() });
      cfgRef.current = next;
      setCfgRaw(next);
      productTouched.current = true;
      var res = result;
      if (!res || !res.ok || !res.result) return;           // сметы ещё нет — просто запомнили выбор
      var r = res.result;
      var base = Number(r.total_unrounded);
      if (!isFinite(base)) return;                          // старый снимок без поля — цену не трогаем
      var cur = (options && options.currency) || 'руб.';
      var cat = (options && options.options) || [];
      var chosen = cat.filter(function (op) { return next.options.indexOf(op.id) >= 0; });
      // «За-детальные» услуги (accee7a9): ставка каталога × детали модели (result.parts) × тираж —
      // ровно как pricing.apply_options на сервере. parts не мерили → считаем как 1 деталь.
      var parts = Math.max(1, Number(r.parts) || 0);
      var qty = Math.max(1, Number(cfgRef.current.qty) || 1);
      function effPrice(op) {
        var p = Number(op.price) || 0;
        return op.per_part ? Math.round(p * parts * qty * 100) / 100 : p;
      }
      var addCost = chosen.reduce(function (s, op) { var p = effPrice(op); return s + (p > 0 ? p : 0); }, 0);
      var newTotal = ceil10(base + addCost);
      // строки расчётника: несервисные оставляем, услуги пересобираем из текущего выбора (>0 руб.)
      // без закрывающей скобки: у «за-детальных» услуг метка «(постобработка · 0,20 × N дет.)»
      var baseLines = (r.lines || []).filter(function (ln) { return String(ln[0]).indexOf('(постобработка') < 0; });
      var optLines = chosen.filter(function (op) { return effPrice(op) > 0; })
        .map(function (op) {
          var rate = (Number(op.price) || 0).toFixed(2).replace('.', ',');
          var lbl = op.name + ' (постобработка' +
            (op.per_part ? ' · ' + rate + ' × ' + (parts * qty) + ' дет.' : '') + ')';
          return [lbl, '+' + fmtMoney(effPrice(op), cur)];
        });
      var nr = Object.assign({}, r, { total: fmtMoney(newTotal, cur), lines: baseLines.concat(optLines) });
      setResult(Object.assign({}, res, { result: nr }));
    }

    // ── Кастомайзер как полноценный калькулятор: реальная генерация STL + слайс + цена + рендер ──
    // НЕ навигируем — возвращаем результат на сам экран кастомайзера (real-time, debounced на фронте).
    function paramDefaults(plasticId) {
      var o = options || {};
      var pl = (o.plastics || []).filter(function (p) { return p.id === plasticId; })[0] || {};
      var dq = (o.qualities || []).filter(function (q) { return q.is_default; })[0] || {};
      var ds = (o.strengths || []).filter(function (s) { return s.is_default; })[0] || {};
      return { printer_id: ((o.printers || [])[0] || {}).id,
        quality_id: pl.best_quality_id || cfg.quality_id || dq.id || 0,
        strength_id: cfg.strength_id || ds.id || 0 };
    }
    // Генерация (OpenSCAD) + слайс: реальные граммы/время/цена + серверный рендер превью.
    function parametricQuote(modelId, params, opts) {
      opts = opts || {};
      var d = paramDefaults(opts.plastic_id);
      return API.parametricModel(modelId, params).then(function (j) {
        if (!j || !j.ok || !j.token) return { ok: false, error: (j && j.error) || 'Не удалось сгенерировать модель' };
        return API.calc({ token: j.token, printer_id: d.printer_id, plastic_id: opts.plastic_id || cfg.plastic_id,
          color: opts.color || '', qty: opts.qty || 1, quality_id: d.quality_id, strength_id: d.strength_id,
          multicolor: !!opts.multicolor, colors: opts.colors || 1, quiet: 1 }).then(function (cj) {
          if (!cj || !cj.ok) return { ok: false, token: j.token, preview: j.preview, dims: j.dims, error: (cj && cj.error) || 'Не удалось посчитать' };
          var r = cj.result || {};
          return { ok: true, token: j.token, preview: r.cpreview || j.preview, dims: r.dims || j.dims,
            grams: r.grams_total, time: r.time_total, priceStr: r.total, totalBefore: r.total_before, discountPct: r.discount_pct };
        });
      }).catch(function () { return { ok: false, error: 'Сеть недоступна' }; });
    }
    // Многоцветный серверный 3D-рендер модели (корпус=color1, текст+рамка=color2, полоса=color3) → data-URL.
    function parametricRender(modelId, params, color1, color2, color3) {
      return API.parametricPreview2c(modelId, params, color1, color2, color3).then(function (j) {
        return (j && j.ok && j.image) ? { ok: true, image: j.image } : { ok: false, error: j && j.error };
      }).catch(function () { return { ok: false, error: 'Сеть недоступна' }; });
    }
    // Быстрый пересчёт цены под другой пластик/цвет БЕЗ переслайса (та же геометрия → тот же токен).
    function parametricRequote(token, plasticId, color, qty) {
      return API.requote(token, plasticId, qty || 1, color).then(function (j) {
        if (!j || !j.ok) return { ok: false, error: (j && j.error) || 'Не удалось пересчитать' };
        var r = j.result || {};
        return { ok: true, token: token, priceStr: r.total, grams: r.grams_total, time: r.time_total,
          totalBefore: r.total_before, discountPct: r.discount_pct, preview: r.cpreview };
      }).catch(function () { return { ok: false, error: 'Сеть недоступна' }; });
    }
    // Оформить заказ по готовому токену (из кастомайзера) — без отдельного экрана сметы.
    function orderToken(token, comment) {
      setBusy(true);
      return API.order(token, comment || '').then(function (j) {
        setBusy(false);
        if (j && j.ok) { toast('Заказ оформлен!'); reloadOrders(); afterOrder(); }
        else alertMsg((j && j.error) || 'Не удалось оформить');
        return j;
      }).catch(function () { setBusy(false); alertMsg('Сеть недоступна'); return { ok: false }; });
    }
    function addCartItem(item) { setCart(function (c) { return c.concat([item]); }); toast('Добавлено в корзину'); }

    // Пакетная обработка нескольких ссылок с Главной (несколько моделей → общая корзина).
    // Каждую ссылку грузим и считаем по очереди дешёвым пластиком (как быстрый путь 050aff56),
    // складываем в корзину, в конце уводим в «Корзину» — дальше существующий checkout.
    function loadUrls(urls) {
      if (!urls || !urls.length) return;
      setProduct(null);
      setBusy(true); setLoadingModel(true);
      var ch = cheapestPlastic() || {};
      var added = 0, failed = 0;
      var chain = Promise.resolve();
      urls.forEach(function (url) {
        chain = chain.then(function () {
          return API.uploadUrl(url).then(function (j) {
            if (!j || !j.ok || !j.token) { failed++; return; }   // варианты/ошибки в пакете пропускаем
            return API.calc({ token: j.token, printer_id: ((options.printers || [])[0] || {}).id,
              plastic_id: ch.id || cfg.plastic_id, color: (ch.colors || [])[0] || cfg.color, qty: 1,
              quality_id: ch.best_quality_id || cfg.quality_id, strength_id: cfg.strength_id,
              quiet: 1 }).then(function (cj) {
              if (cj && cj.ok) {
                var r = cj.result;
                setCart(function (c) {
                  return c.concat([{ token: j.token, label: j.label, preview: r.cpreview || j.preview, totalStr: r.total,
                    pid: ch.id || cfg.plastic_id, plastic: ch.name || plasticName(cfg.plastic_id),
                    color: (ch.colors || [])[0] || cfg.color, dims: r.dims || '' }]);
                });
                added++;
              } else failed++;
            });
          }).catch(function () { failed++; });
        });
      });
      chain.then(function () {
        setBusy(false); setLoadingModel(false);
        toast(added ? ('В корзину: ' + added + (failed ? (' · не вышло: ' + failed) : '')) : 'Не удалось обработать ссылки');
        if (added) { syncBackStack(['hub']); setStack(['hub']); setScreen('cart'); haptic(); }
      });
    }

    // quiet=true (ae038968): пересчёт для тихо открытой карточки товара — НЕ переключает экран
    // на 'slicing' (карточка уже на 'quote' и остаётся там), не крутит секундомер/оверлей. Успех
    // просто кладёт setResult (Quote сам увидит цену); неудача — тоже setResult, но с ok:false и
    // без alertMsg/мем-оверлея — Quote отрисует инлайн-блок «не получилось» вместо конфига.
    function doCalc(mdl, c, quiet) {
      if (!mdl) return;
      // Любой новый расчёт отменяет отложенный тихий пересчёт со сметы: иначе опоздавший
      // таймер выстрелит уже поверх более свежего расчёта, поднимет calcSeq и обесценит его
      // ответ — а тихая ветка не делает clearInterval(slTimer)/setScreen('quote'), и юзер
      // остаётся на экране «слайсим» без выхода. Отложенный requote гасим по той же причине:
      // его approx-ответ не должен перетереть честный расчёт.
      clearTimeout(tweakTimer.current);
      clearTimeout(requoteTimer.current);
      calcGuard.current = mdl.token;
      // номер этого запуска: юзер мог сменить цвет во время расчёта — поздний ответ СТАРОГО
      // запуска не должен перетереть свежий (2026-07-11, ранний выбор цвета на карточке)
      var mySeq = ++calcSeq.current;
      if (!quiet) {
        // Вход в поток расчёта ИЗВНЕ (hub/каталог/корзина/…) кладёт уровень «назад» (OpenTodo
        // 85d16a2a): раньше стек тут не пополнялся, BackButton не показывался и свайп закрывал
        // мини-апп — это и была жалоба «на некоторых вкладках». Внутри потока (config/slicing/
        // quote сменяют друг друга, пересчёты) новых уровней не добавляем.
        if (['config', 'slicing', 'quote'].indexOf(screen) < 0) {
          setStack(function (st) { return st.concat([screen]); });
          if (window.PS_Back) window.PS_Back.push(doBack);
        }
        setScreen('slicing'); setSecs(0);
        setRecalcPending(false);       // блокирующий расчёт сам себе индикатор — флаг сметы снимаем
        calcPending.current = mySeq;   // живой НЕтихий расчёт: выход из потока → авто-уведомление
        var t0 = Date.now();
        slTimer.current = setInterval(function () { setSecs(Math.round((Date.now() - t0) / 1000)); }, 250);
      }
      API.calc({ token: mdl.token, printer_id: (options.printers[0] || {}).id, plastic_id: c.plastic_id,
        color: c.color, qty: c.qty, quality_id: c.quality_id, strength_id: c.strength_id,
        multicolor: c.multicolor, promo: c.promo, scale: c.scale_pct || 100, quiet: quiet ? 1 : undefined,
        urgency: c.urgent ? 'rush' : 'normal',   // срочность (B3)
        options: (c.options && c.options.length) ? c.options : undefined,   // опции постобработки (B4)
        colors_sel: (mdl.multicolor && c.multicolor && c.colors_sel && c.colors_sel.length) ? c.colors_sel : undefined,
        plates: (c.plates && c.plates.length) ? c.plates : undefined,
        plate_colors: (c.plate_colors && Object.keys(c.plate_colors).length) ? c.plate_colors : undefined,
        plate_plastics: (c.plate_plastics && Object.keys(c.plate_plastics).length) ? c.plate_plastics : undefined }).then(function (j) {
        if (calcPending.current === mySeq) calcPending.current = 0;   // расчёт долетел — выход уже не «бросил слайс»
        // юзер ушёл ждать уведомление (ae4c6c13) / открыл другую карточку / успел пересчитать
        if (calcGuard.current !== mdl.token || calcSeq.current !== mySeq) return;
        // цена снова соответствует тому, что на экране (ранний выход выше — значит этот ответ
        // уже неактуален, и флаг снимет тот расчёт, который его обогнал)
        setRecalcPending(false);
        if (!quiet) clearInterval(slTimer.current);
        if (!j || !j.ok) {
          if (quiet) {
            setResult({ ok: false, error: (j && j.error) || 'Не удалось посчитать', can_order_unsliced: !!(j && j.can_order_unsliced), token: mdl.token });
            return;
          }
          setScreen('config');
          // Слайс не удался (55a1cf8f): бэк говорит can_order_unsliced — не тупик, предлагаем
          // отправить мастеру заявку без цены (он посмотрит модель и назовёт её сам).
          alertMsg((j && j.error) || 'Не удалось посчитать', null, null, j && j.can_order_unsliced ? mdl.token : '');
          return;
        }
        var r = j.result || {};
        // сервер уменьшил масштаб до максимума под стол → синхронизируем cfg и говорим об этом
        if (r.scale_pct && r.scale_pct !== (c.scale_pct || 100)) {
          setCfgRaw(Object.assign({}, c, { scale_pct: r.scale_pct }));
          if (r.scale_clamped && !quiet) toast('Не влезает на стол — масштаб уменьшен до ' + r.scale_pct + '%');
        }
        setResult(j);
        if (!quiet) setScreen('quote');
      }).catch(function () {
        if (calcPending.current === mySeq) calcPending.current = 0;
        if (calcGuard.current !== mdl.token || calcSeq.current !== mySeq) return;
        setRecalcPending(false);       // сеть отвалилась — не держим кнопки заблокированными навсегда
        if (quiet) { setResult({ ok: false, error: 'Сеть недоступна' }); return; }
        clearInterval(slTimer.current); setScreen('config'); alertMsg('Сеть недоступна');
      });
    }
    function calc() { doCalc(model, cfg); }
    // Пересчёт с патчем cfg (idea #2 «сделать дешевле»: сменить заполнение → нужен переслайс).
    function recalc(patch) {
      if (!model) return;
      var next = Object.assign({}, cfg, patch || {});
      setCfgRaw(next);
      doCalc(model, next);
    }
    // Мгновенный ОРИЕНТИР цены пока идёт нарезка (idea #1): грубо из габаритов (bbox) и выбранного
    // пластика, нарочно ЗАНИЖЕН (фактор 0.18) — реальная цена обычно ≥, поэтому «от ≈». Точную даёт слайс.
    function priceEstimate() {
      if (!model || !model.dims || !options) return null;
      var mm = /(\d+)\D+(\d+)\D+(\d+)/.exec(model.dims);
      if (!mm) return null;
      // dims у многостольной модели перечисляет столы («83×158×29 · 73×148×25 мм») — regexp
      // берёт ПЕРВЫЙ стол, поэтому ориентир множим на число столов, иначе он занижен вчетверо
      var bbox = (+mm[1] / 10) * (+mm[2] / 10) * (+mm[3] / 10) * Math.max(1, +model.plates || 1);
      var pl = (options.plastics || []).filter(function (p) { return p.id === cfg.plastic_id; })[0] || {};
      var grams = bbox * (Number(pl.density) || 1.24) * 0.18;
      var price = grams * ((Number(pl.price) || 50) / 1000) * 1.4 + 1.0;
      return price > 0 ? API.fmt(price) : null;
    }
    // «Уведомить, когда посчитается» (ae4c6c13): бот дошлёт цену сообщением с кнопкой на смету
    function notifyCalc() {
      if (!model) return;
      API.calcNotify(model.token).then(function (j) {
        if (j && j.ok) {
          calcPending.current = 0;   // подписка оформлена — уход с экрана не должен подписывать второй раз
          calcGuard.current = null;
          clearInterval(slTimer.current);
          reset();
          toast('Ок! Пришлю цену сообщением в чат');
        } else alertMsg((j && j.error) || 'Не получилось — дождитесь расчёта');
      }).catch(function () { alertMsg('Сеть недоступна'); });
    }
    // «Оставить заказ» ПРЯМО во время слайса (OpenTodo f1e80d67): заявка создаётся мгновенно из
    // текущего выбора, цену проставит идущий фоном расчёт (сервер, calc-хвост). Гасим авто-подписку
    // на уведомление (calcPending) и гвард позднего ответа — уход с экрана не должен ни подписать
    // повторно, ни выкинуть на смету, когда слайс долетит: заказ уже оформлен.
    function orderNow() {
      // busy-гард ловит тапы через кадр; ref-гард (синхронный) — два тапа в ОДИН кадр, пока
      // setBusy/disabled ещё не применились. Иначе оба запроса летят, серверный лок отсекает
      // второй, но юзер видит лишний «уже оформляется». Ref делает клиент таким же герметичным.
      if (!model || !model.token || busy || orderNowInflight.current) return;
      orderNowInflight.current = true;
      calcPending.current = 0;
      calcGuard.current = null;
      clearInterval(slTimer.current);
      setBusy(true);
      var pr = (options && options.printers && options.printers[0]) || {};
      // ВЕСЬ выбор, с которым ушёл расчёт (тот же, что в API.calc выше): если живой слайс до
      // заказа не доживёт (рестарт ПК), цену досчитает очередь — и считать она должна то же самое
      // (столы, свой пластик и цвет стола, масштаб, опции, промо). Пустое шлём явно (null/{}),
      // а не пропуском: пропуск сервер понимает как «старый фронт» и берёт выбор идущего расчёта.
      var sel = { printer_id: pr.id, plastic_id: cfg.plastic_id, quality_id: cfg.quality_id,
        strength_id: cfg.strength_id, qty: cfg.qty, color: cfg.color, multicolor: cfg.multicolor,
        urgency: cfg.urgent ? 'rush' : 'normal',   // срочность (B3)
        colors_sel: (model.multicolor && cfg.multicolor && cfg.colors_sel && cfg.colors_sel.length) ? cfg.colors_sel : null,
        scale: cfg.scale_pct || 100, promo: cfg.promo || '',
        options: (cfg.options && cfg.options.length) ? cfg.options : [],
        plates: (cfg.plates && cfg.plates.length) ? cfg.plates : null,
        plate_colors: cfg.plate_colors || {}, plate_plastics: cfg.plate_plastics || {} };
      API.orderNow(model.token, sel, cfg.comment).then(function (j) {
        orderNowInflight.current = false; setBusy(false);
        if (j && j.ok) {
          toast(j.priced ? 'Заказ оформлен!' : 'Заказ принят! Цену пришлю, как досчитаю');
          reloadOrders(); afterOrder();
        } else {
          // не удалось (напр. пресет удалён во время сессии) — не бросаем юзера на «вечном» слайсе:
          // расчётный контекст уже свёрнут выше, поэтому уводим на экран параметров (можно пересчитать)
          alertMsg((j && j.error) || 'Не удалось оформить');
          setScreen('config');
        }
      }).catch(function () { orderNowInflight.current = false; setBusy(false); alertMsg('Сеть недоступна'); setScreen('config'); });
    }
    // Смена пластика/цвета на экране цены — ОПТИМИСТИЧНО (жалоба «медленная смена цвета в 3D»,
    // 2026-07-23). Раньше cfg (а с ним и перекраска 3D через v3dColors) ждал ответа сервера, и
    // весь пикер дизейблился busy на время запроса. Теперь: cfg меняем СРАЗУ (3D красится в тот
    // же кадр, шарики не блокируются, можно перебирать цвета подряд), а цена и серверный снимок
    // заказа (m["last"]) догоняют фоновым /api/requote — дебаунс + last-wins. Пока снимок не
    // догнал, recalcPending гасит «Оставить заказ»/«В корзину» (иначе заказ ушёл бы со старым
    // цветом — тот же денежный контракт, что у tweak). Ошибка сети/сервера НЕ снимает флаг:
    // заказ по устаревшему снимку хуже, чем видимый бейдж «пересчитываем» до следующего тапа.
    var requoteSeq = useRef(0);
    var requoteTimer = useRef(null);
    function requote(plasticId, color) {
      var mdl = modelRef.current;
      if (!mdl || !mdl.token) return;
      // тап в уже выбранный цвет НЕ отсекаем: после сбоя сети это легальный путь освежить
      // серверный снимок (recalcPending остался висеть) — дебаунс всё равно схлопнет лишнее
      var next = Object.assign({}, cfgRef.current, { plastic_id: plasticId, color: color || '' });
      productTouched.current = true;
      cfgRef.current = next;
      setCfgRaw(next);                 // мгновенно: v3dColors обновились → 3D перекрасился
      setRecalcPending(true);
      clearTimeout(requoteTimer.current);
      requoteTimer.current = setTimeout(function () {
        var seq = ++requoteSeq.current;
        var cs = calcSeq.current;      // полный пересчёт (tweak/calc), начатый ПОСЛЕ нас, главнее
        var tok = (modelRef.current || {}).token;
        if (!tok) { setRecalcPending(false); return; }
        var cc = cfgRef.current;
        API.requote(tok, cc.plastic_id, cc.qty, cc.color).then(function (j) {
          if (seq !== requoteSeq.current || cs !== calcSeq.current) return;   // обогнали — молчим
          if ((modelRef.current || {}).token !== tok) return;                 // уже другая модель
          if (!j || !j.ok) { alertMsg((j && j.error) || 'Не удалось пересчитать'); return; }
          setRecalcPending(false);
          setResult(j);
        }).catch(function () {
          if (seq !== requoteSeq.current || cs !== calcSeq.current) return;
          alertMsg('Сеть недоступна — цена обновится при следующей смене цвета');
        });
      }, 250);
    }

    function reloadOrders() { API.orders().then(function (j) { if (j.ok) setOrders(j.orders || []); }); }
    function reloadCollection() { API.collection().then(function (j) { if (j.ok) { setCollection(j.items || []); if (j.categories) setCollectionCats(j.categories); } }); }
    function reloadMyModels() { API.mymodels().then(function (j) { if (j.ok) setMymodels(j.models || []); }); }

    // Опции постобработки шлём в теле заказа (B4-инстант): их могли переключить локально на смете
    // без переслайса, поэтому сервер переприложит актуальный выбор к цене при оформлении.
    function placeOrder() { setBusy(true); API.order(model.token, cfg.comment, cfg.options).then(function (j) { setBusy(false); if (j.ok) { toast('Заказ оформлен!'); reloadOrders(); afterOrder(); } else alertMsg(j.error || 'Не удалось'); }); }
    // Заказ без сметы (OpenTodo 55a1cf8f): токен передаётся явно, поэтому один код работает и
    // из мем-оверлея (orderUnsliced() → errUnslicedToken), и из тихого инлайн-блока карточки
    // товара (ae038968: Quote зовёт orderUnslicedFor(result.token) напрямую, без оверлея).
    // Пожелание берём из того же поля, что и обычный заказ (cfg.comment).
    function orderUnslicedFor(token) {
      if (!token) return;
      setBusy(true);
      API.orderUnsliced(token, cfg.comment).then(function (j) {
        setBusy(false);
        if (j && j.ok) {
          setErrMsg(null); setErrUnslicedToken('');
          toast('Заявка отправлена мастеру — назовёт цену после осмотра модели');
          reloadOrders(); afterOrder();
        } else alertMsg((j && j.error) || 'Не удалось отправить');
      }).catch(function () { setBusy(false); alertMsg('Сеть недоступна'); });
    }
    function orderUnsliced() { orderUnslicedFor(errUnslicedToken); }
    function submitCustom(text, files) {
      setBusy(true);
      API.customOrder(text, files).then(function (j) {
        setBusy(false);
        if (j.ok) { toast('Заявка отправлена мастеру!'); reloadOrders(); afterOrder(); }
        else alertMsg(j.error || 'Не удалось отправить');
      }).catch(function () { setBusy(false); alertMsg('Сеть недоступна'); });
    }
    // Имя пластика по id (для метаданных батчинга в корзине, idea #14).
    function plasticName(id) { return (((options || {}).plastics || []).filter(function (p) { return p.id === id; })[0] || {}).name || ''; }
    function addCart() {
      var r = (result && result.result) || {};
      setCart(function (c) {
        return c.concat([{ token: model.token, label: model.label, preview: r.cpreview || model.preview, totalStr: r.total,
          pid: cfg.plastic_id, plastic: plasticName(cfg.plastic_id), color: cfg.color, dims: r.dims || model.dims || '',
          qty: cfg.qty || 1, urgent: !!cfg.urgent,   // срочность (B3): бейдж ⚡ на плитке корзины
          opts: (cfg.options || []).slice() }]);   // B4-инстант: локальный выбор услуг уедет в /api/order_cart
      });
      toast('Добавлено в корзину');
    }
    function removeCart(i) { setCart(function (c) { return c.filter(function (_, j) { return j !== i; }); }); }
    // Количество позиции корзины (аудит, проблема #3): пересчёт через requote — он же обновляет
    // серверный снимок токена, так что «Оформить корзину» уйдёт уже с новым количеством/ценой.
    function updateCartQty(i, qty) {
      var it = cart[i];
      qty = Math.max(1, Math.min(1000, qty || 1));
      if (!it || busy || qty === (it.qty || 1)) return;
      setBusy(true);
      API.requote(it.token, it.pid, qty, it.color).then(function (j) {
        setBusy(false);
        if (!j || !j.ok) { toast((j && j.error) || 'Не удалось пересчитать'); return; }
        var r = j.result || {};
        setCart(function (c) { return c.map(function (x, k) { return k === i ? Object.assign({}, x, { qty: qty, totalStr: r.total || x.totalStr }) : x; }); });
      }).catch(function () { setBusy(false); toast('Сеть недоступна'); });
    }
    // Сердечко «Избранное» на модельках каталога: оптимистичное переключение (сердечко
    // заливается сразу), сервер догоняет; ошибка сети — откат и тост. Шлём ЖЕЛАЕМОЕ
    // состояние, не toggle — двойной тап не перевернёт сердечко обратно.
    function toggleFav(id) {
      if (!id) return;
      var on = !favs[id];
      setFavs(function (f) {
        var n = Object.assign({}, f);
        if (on) n[id] = 1; else delete n[id];
        return n;
      });
      haptic();
      API.favorite(id, on).then(function (j) {
        if (!j || !j.ok) throw new Error((j && j.error) || 'fail');
        if (on) toast('Добавлено в избранное');
      }).catch(function () {
        setFavs(function (f) {   // откат оптимистичного переключения
          var n = Object.assign({}, f);
          if (on) delete n[id]; else n[id] = 1;
          return n;
        });
        toast('Не удалось обновить избранное');
      });
    }
    // «Посчитать с этим пластиком» из витрины материалов: тот же механизм пресета, что у
    // кастомайзера (paramPreselRef применится в applyModel к следующей загруженной модели).
    function preselectPlastic(pid) {
      paramPreselRef.current = { plastic_id: pid };
      toast('Пластик выбран — пришлите модель или выберите из каталога');
      goTab('hub');
    }
    // opts (B5 + A2): { delivery_method, delivery_address, delivery_type_id, comment }. Итог
    // деривируется на сервере: сумма позиций + стоимость доставки ПО ТИПУ (клиент присылает
    // только id тарифа, цену не диктует).
    function checkout(batch, opts) {
      if (!cart.length) return; setBusy(true);
      // Карта token→[id услуг] (B4-инстант): позиции могли получить услуги локально на смете —
      // отдаём их серверу, чтобы он переприложил к цене каждой позиции без переслайса.
      var optsMap = {};
      cart.forEach(function (c) { if (c.token && c.opts && c.opts.length) optsMap[c.token] = c.opts; });
      API.orderCart(cart.map(function (c) { return c.token; }), !!batch, opts || {}, optsMap).then(function (j) {
        setBusy(false);
        if (j.ok) { toast('Заказ оформлен (' + (j.count || cart.length) + ' поз.)'); setCart([]); reloadOrders(); afterOrder(); }
        else alertMsg(j.error || 'Не удалось');
      });
    }
    function pdf() { API.pdf(model.token).then(function (j) { toast(j.ok ? 'PDF-смета отправлена в чат' : (j.error || 'Ошибка')); }); }
    function orderPdf(id) { API.orderPdf(id).then(function (j) { toast(j.ok ? 'PDF-смета отправлена в чат' : (j.error || 'Ошибка')); }); }
    // Шеринг-карточка «я напечатал» (idea #12): модель + цена + РЕФЕРАЛЬНАЯ ссылка (если есть).
    function share() {
      var r = (result && result.result) || {};
      var ref = (headerProfile && headerProfile.referral_link) || ('https://t.me/' + ((options && options.bot_username) || 'bot'));
      var label = (model && model.label) || 'модель';
      var text = 'Напечатал «' + label + '»' + (r.total ? (' за ' + r.total) : '') + ' 🖨️ Посчитай свою 3D-печать тут:';
      if (tg && tg.openTelegramLink) tg.openTelegramLink('https://t.me/share/url?url=' + encodeURIComponent(ref) + '&text=' + encodeURIComponent(text));
      else { try { navigator.clipboard.writeText(ref); } catch (e) {} toast('Ссылка скопирована'); }
    }
    function shareLink(link) {
      if (tg && tg.openTelegramLink) tg.openTelegramLink('https://t.me/share/url?url=' + encodeURIComponent(link) + '&text=' + encodeURIComponent('Считаю 3D-печать тут'));
      else { try { navigator.clipboard.writeText(link); } catch (e) {} toast('Ссылка скопирована'); }
    }
    // stale — цену успели поменять, пока карточка была открыта: не принимаем, а показываем новую
    function respondPrice(id, accept, expected) { API.orderRespond(id, accept, expected).then(function (j) { if (j.ok) { toast(accept ? 'Цена принята' : 'Вы отказались'); reloadOrders(); } else if (j.stale) { toast(j.error || 'Цена изменилась'); reloadOrders(); } else alertMsg(j.error || 'Ошибка'); }); }
    // Онлайн-оплата (A1): счёт-ссылка с бэка → нативное окно оплаты Telegram поверх мини-аппа.
    // status: 'paid' — деньги списаны (бот получит successful_payment и пометит заказ);
    // 'cancelled' — клиент закрыл окно; 'failed' — платёж не прошёл. Вне Telegram (старый
    // клиент без openInvoice) открываем счёт ссылкой — оплата пройдёт в обычном чате.
    function payOrder(id) {
      API.orderPayLink(id).then(function (j) {
        if (!j.ok) { alertMsg(j.error || 'Не удалось создать счёт'); return; }
        if (tg && tg.openInvoice) {
          tg.openInvoice(j.link, function (status) {
            if (status === 'paid') { toast('Оплачено ✅'); setTimeout(reloadOrders, 1200); }
            else if (status === 'failed') alertMsg('Платёж не прошёл. Попробуйте ещё раз.');
          });
        } else if (tg && tg.openTelegramLink) tg.openTelegramLink(j.link);
        else window.open(j.link, '_blank');
      });
    }
    function editOrder(id, p) { return API.orderEdit(id, p).then(function (j) { if (j.ok) { toast('Параметры обновлены' + (j.price ? ' · ' + j.price : '')); reloadOrders(); } else alertMsg(j.error || 'Не удалось пересчитать'); return j; }); }
    function cancelOrder(id) {
      // Необратимое действие — раньше срабатывало с одного тапа (аудит, проблема #1)
      askConfirm('Отменить заказ #' + id + '?', function () {
        API.orderCancel(id).then(function (j) { if (j.ok) { toast('Заказ #' + id + ' отменён'); reloadOrders(); } else alertMsg(j.error || 'Ошибка'); });
      });
    }
    function openChat(id, label) { setChat({ id: id, label: label }); go('chat'); }
    function openAdminOrder(id) { setAdminOrderId(id); go('adminOrder'); }
    function openPrintClassify(jid) { setPrintClassifyJid(jid); go('adminPrintClassify'); }
    function openAdminClient(uid) { setAdminClientId(uid); go('adminClient'); }
    function openGfxProject(id) { setGfxProjectId(id); go('gfxProject'); }
    // Чипы «Требует внимания» → список заказов с УЖЕ включённым фильтром (аудит, проблема #2)
    function openAdminOrders(filter) { setAdminOrdersFilter(filter || ''); go('adminOrders'); }
    // Адресуемая вкладка из deep-link (?tab=…): отдаётся один раз первой смонтированной
    // tabbed()-обёртке — так go=print&tab=service открывает «Обслуживание», а не первую вкладку.
    function takeBootTab() { var t = bootTabRef.current; bootTabRef.current = ''; return t; }

    // Ушли со сметы — снимаем отложенные тихие пересчёты (tweak и requote) и их флаг: таймер
    // иначе выстрелит уже на другом экране (обесценив более свежий расчёт), а флаг останется
    // висеть на кнопках.
    useEffect(function () {
      if (screen === 'quote') return;
      clearTimeout(tweakTimer.current);
      clearTimeout(requoteTimer.current);
      setRecalcPending(false);
    }, [screen]);

    // Текущий экран — в глобалку для телеметрии JS-ошибок: «TypeError» без экрана,
    // на котором он случился, разбирать почти невозможно.
    useEffect(function () { window.__PS_SCREEN__ = screen; }, [screen]);

    // Админские экраны едут отдельными файлами (ADMIN_SCRIPTS) — тянем их при первом заходе.
    // Экран может быть админским с самого старта (глубокая ссылка ?go=adminorder из пинга),
    // поэтому эффект объявлен ВЫШЕ раннего возврата «ещё нет options» — иначе загрузка
    // началась бы только после первой отрисовки шелла.
    var wantAdmin = String(screen).slice(0, 5) === 'admin';
    useEffect(function () {
      if (!wantAdmin || adminReady()) return undefined;
      var alive = true;
      setAdminLoad('loading');
      ensureAdmin().then(
        function () { if (alive) setAdminLoad('ready'); },     // перерисовка → SCREENS увидит экраны
        function () { if (alive) setAdminLoad('error'); });
      return function () { alive = false; };
    }, [wantAdmin, adminTry]);

    // Мастеру админка нужна почти наверняка, поэтому греем её фоном, когда он просто открыл
    // приложение: первый заход тогда открывается мгновенно. Клиенту эти файлы не поедут вовсе.
    // Ошибку глушим: подгрузка по заходу всё равно повторится и покажет её как надо.
    useEffect(function () {
      if (!options || !options.is_admin || wantAdmin || adminReady()) return undefined;
      var t = setTimeout(function () { ensureAdmin().catch(function () {}); }, 2000);
      return function () { clearTimeout(t); };
    }, [!!(options && options.is_admin), wantAdmin]);

    // lazy-load lists on entry
    useEffect(function () {
      if (screen === 'orders' && orders.length === 0) reloadOrders();
      // model больше не префетчит коллекцию: блок «Из коллекции» ушёл с экрана поиска
      // (редизайн F4) — каталожные результаты приходят из collection_search
      if (screen === 'collection' && collection.length === 0) reloadCollection();
      if (screen === 'models' && mymodels.length === 0) reloadMyModels();
    }, [screen]);

    var app = {
      options: options, model: model, cfg: cfg, setCfg: setCfg, result: result, cart: cart, busy: busy,
      orders: orders, collection: collection, collectionCats: collectionCats, mymodels: mymodels, chatOrderId: chat.id, userId: userId,
      adminOrderId: adminOrderId, openAdminOrder: openAdminOrder,
      printClassifyJid: printClassifyJid, openPrintClassify: openPrintClassify,
      adminClientId: adminClientId, openAdminClient: openAdminClient,
      gfxProjectId: gfxProjectId, openGfxProject: openGfxProject,   // мастерская органайзеров
      adminOrdersFilter: adminOrdersFilter, openAdminOrders: openAdminOrders,
      takeBootTab: takeBootTab, updateCartQty: updateCartQty, preselectPlastic: preselectPlastic,
      setView: setView,   // подвид экрана для менеджера скролла (861eab19) — см. scrollKey выше
      favs: favs, toggleFav: toggleFav,   // избранное (сердечки на модельках)
      go: go, back: back, reset: reset, goTab: goTab, toast: toast,
      loadUrl: loadUrl, loadUrls: loadUrls, loadFile: loadFile, useModel: useModel, loadParametric: loadParametric, calc: calc, requote: requote,
      customModel: customModel, openCustomizer: openCustomizer, recalc: recalc,
      product: product, openProduct: openProduct, orderUnslicedFor: orderUnslicedFor, pickColor: pickColor,   // карточка товара (ae038968)
      setPlastic: setPlastic,   // смена пластика одноцветной модели прямо на смете (OpenTodo one-color-plastic)
      setSlotColor: setSlotColor, pendingAction: pendingAct, deferAction: setPendingAct,   // «сразу доступно» (2026-07-11)
      setFileVariant: setFileVariant,   // вариант набора файлов карточки (87eb9cf3)
      tweak: tweak, recalcPending: recalcPending,   // правка параметров прямо на смете (71e15fe6)
      applyOptionsLocal: applyOptionsLocal,   // B4-инстант: услуги постобработки считаются локально, без переслайса
      parametricQuote: parametricQuote, parametricRequote: parametricRequote, parametricRender: parametricRender, orderToken: orderToken, addCartItem: addCartItem,
      placeOrder: placeOrder, submitCustom: submitCustom, addCart: addCart, removeCart: removeCart, checkout: checkout, pdf: pdf, orderPdf: orderPdf, share: share, shareLink: shareLink,
      reloadOrders: reloadOrders, reloadCollection: reloadCollection, reloadMyModels: reloadMyModels,
      respondPrice: respondPrice, payOrder: payOrder, cancelOrder: cancelOrder, editOrder: editOrder, openChat: openChat,
      pendingSearch: pendingSearch, clearPendingSearch: clearPendingSearch, searchModels: searchModels,
      setHeaderProfile: setHeaderProfile, headerProfile: headerProfile,
      // Права сотрудника (A4): app.can('finance') — показывать ли деньги, app.can('settings') —
      // пускать ли в настройки. Разделы, которых нет в правах, просто не рисуем; отказ всё
      // равно придёт с сервера — интерфейс лишь не водит оператора в закрытую дверь.
      can: function (cap) {
        var me = options && options.me;
        // Данные о правах ещё не приехали (первый кадр) — ничего не прячем: сервер всё равно
        // закрывает те же ручки, а мигать исчезающими разделами у владельца нельзя.
        if (!me || !me.caps) return true;
        return me.caps.indexOf(cap) >= 0;
      },
      role: ((options && options.me) || {}).role || '',
      roleLabel: ((options && options.me) || {}).role_label || '',
    };

    var SCREENS = { hub: window.Hub, model: window.ModelInput, parametric: window.Parametric, customizer: window.Customizer, config: window.Config, quote: window.Quote, planner: PlannerLazy,
      gridfinity: window.GridfinityScreen,   // старый адрес конфигуратора — ведёт в мастерскую
      workshop: window.WorkshopScreen, gfxProject: window.GfxProjectScreen,   // мастерская органайзеров
      orders: window.Orders, cart: window.Cart, collection: window.Collection, models: window.MyModels, custom: window.CustomOrder, profile: window.Profile,
      products: window.Products,   // витрина готовых изделий (bd896aa6)
      apps: window.AppsScreen,   // «Приложения»: витрина инструментов клиента (2026-09-02)
      catalog: window.Catalog, help: window.Help, admin: window.Admin, chat: window.Chat,
      adminDash: window.AdminDash, adminOrders: window.AdminOrders, adminOrder: window.AdminOrderDetail,
      adminTools: window.AdminTools,   // инструменты у станка: допечатка со слоя (screens_admin4.js)
      adminQueue: window.AdminQueue, adminPrinter: window.AdminPrinter, adminStatus: window.AdminStatus, adminService: window.AdminService, adminSelf: window.AdminSelfPrint,
      adminPlastics: window.AdminPlasticsHub, adminPromos: window.AdminPromos, adminSettings: window.AdminSettings,
      adminPriceCalc: window.AdminPriceCalc,
      adminDiscounts: window.AdminDiscounts, adminShades: window.AdminShades,
      adminCollectionAdmin: window.AdminCollectionAdmin, adminPrinters: window.AdminPrinters,
      adminProducts: window.AdminProducts,   // готовые изделия / витрина (bd896aa6)
      adminActivity: window.AdminActivity, adminBigData: window.AdminBigData,
      adminCalcQueue: window.AdminCalcQueue, adminJournal: window.AdminJournal,
      adminClients: window.AdminClients, adminClient: window.AdminClientCard, adminTasks: window.AdminTasks,
      adminNotifs: window.AdminNotifs,   // центр уведомлений: release notes (2026-07-16)
      adminErrors: window.AdminErrors,   // телеметрия ошибок (f4de8627)
      adminKb: window.AdminKb,   // база знаний владельца студии (c356387a)
      adminFamilies: window.AdminFamilies,   // семейные счета: заявки + семьи (2026-07-17)
      adminPrintClassify: window.AdminPrintClassify,   // «чья печать?» (4effbc1b)
      adminPrint: window.AdminPrintHub, adminPricing: window.AdminPricing,
      adminMasterLog: window.AdminMasterLog,
      adminDelivery: window.AdminDelivery,   // зоны и тарифы доставки (A2)
      adminStaff: window.AdminStaff,         // команда: сотрудники и роли (A4)
      adminMore: window.AdminMore,
      // паритет с настольной админкой (2026-09-02): план недели, настройки, сообщество, калибровка
      adminPlan: window.AdminPlan, adminStudio: window.AdminStudioSettings,
      adminCommunity: window.AdminCommunity, adminCalib: window.AdminCalib };
    var Cur = SCREENS[screen] || window.Hub;
    var tt = TITLES[screen] || ['', null];
    var isAdminScreen = wantAdmin;
    // Админский экран запрошен, а его файл ещё (или уже) не приехал. Именно этот случай и
    // давал бы молчаливый провал: SCREENS[screen] === undefined, `|| window.Hub` — и мастер
    // на главной без единого следа в консоли. Рисуем заглушку вместо Cur.
    var adminPending = isAdminScreen && !adminReady();
    var showTabbar = !isAdminScreen && !TABBAR_HIDDEN[screen];
    var showAdminTabbar = isAdminScreen;   // у мастера свой таб-бар на всех админ-экранах
    // На hub заголовок дублировал бы вордмарк «PRINT·STUDIO» в брендбаре (и нативный
    // заголовок Telegram) — три одинаковых «Print Studio» подряд. Поэтому пустой tt[0]
    // прячет строку заголовка целиком.
    var titleText = screen === 'chat' ? ('Заказ #' + chat.id) : tt[0];
    var hasCta = screen === 'config';

    if (!options) {
      // Органичный блоб на буте (OpenTodo 5f6de0a7 «organic, blobby feeling»); ring — фолбэк.
      return h('div', { className: 'tg' }, h('div', { className: 'slicing', style: { background: 'var(--bg)' } },
        window.BlobLoader ? h(window.BlobLoader, null)
          : h('svg', { className: 'ring', viewBox: '0 0 50 50' },
            h('circle', { cx: 25, cy: 25, r: 20, fill: 'none', stroke: 'rgba(255,255,255,.1)', strokeWidth: 4 }),
            h('circle', { cx: 25, cy: 25, r: 20, fill: 'none', stroke: 'var(--accent)', strokeWidth: 4, strokeLinecap: 'round', strokeDasharray: '90 200' },
              h('animateTransform', { attributeName: 'transform', type: 'rotate', from: '0 25 25', to: '360 25 25', dur: '0.9s', repeatCount: 'indefinite' }))),
        h('div', { className: 'msg' }, 'Загружаю…')));
    }

    // Экран проекта мастерской отдаёт шапку сцене. Строка с логотипом и «PRINT·STUDIO»
    // занимает почти полсотни пикселей, а на этом экране она не несёт ничего: назад ведёт
    // собственная стрелка у вкладок проекта, корзина к органайзеру отношения не имеет.
    // Класс уезжает и на корень: по нему движок добирает высоту сцены (.tg.bare .gfx).
    var bare = screen === 'gfxProject';

    return h('div', { className: 'tg' + (bare ? ' bare' : '') },
      h('div', { className: 'chrome' + (bare ? ' bare' : '') },
        !bare && h('div', { className: 'brandbar' },
          // Стрелка «назад» в шапке — НА ВСЕХ экранах кроме корня главной (фидбэк владельца
          // 2026-07-11): снимает ровно один шаг (лайтбокс → раздел → список → главная), никогда
          // не закрывает приложение. Слои вне React (раздел каталога, лайтбоксы) видны через
          // backDepth (подписка на back.js); пустой стек вне hub = корень таба → шаг на главную.
          (backDepth > 0 || screen !== 'hub') && h('button', { className: 'backbtn', 'aria-label': 'Назад',
            onClick: function () { if (backDepth > 0) back(); else goTab('hub'); } },
            icon('back', { size: 22 })),
          BRAND.logo
            ? h('img', { className: 'mark', src: BRAND.logo, alt: '' })
            : h('svg', { className: 'mark', viewBox: '0 0 24 24', fill: 'none', stroke: 'var(--accent)', strokeWidth: '1.6', strokeLinecap: 'round' },
              h('path', { d: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6M12 3v6M12 15v6M3 12h6M15 12h6' })),
          // Наш вордмарк с точками-акцентами — только пока имя наше (brand.stock). Чужое имя
          // рисуем как есть: разрезать его на «ПЕРВОЕ·ВТОРОЕ» вслепую нельзя.
          h('div', { className: 'wm' }, BRAND.stock ? wordmark(BRAND.wordmark) : BRAND.title),
          // корзина в шапке (магазинный паттерн) — на клиентских экранах, не в админке;
          // справа от неё — кружок профиля (вернулся в шапку 2026-09-02 решением владельца:
          // на его месте внизу теперь «Приложения»). Группа прижата вправо целиком: кнопка
          // админки монтируется ПОЗЖЕ (после загрузки options) и растёт влево, не сдвигая
          // корзину и профиль (70cfa89a)
          !isAdminScreen && h('span', { className: 'rgroup' },
            // мастеру — вход в админку всегда под рукой (1 тап из любого клиентского экрана)
            options && options.is_admin && h('button', { className: 'cart', onClick: function () { go('admin'); }, 'aria-label': 'Админ-панель' },
              icon('sliders', { size: 19 })),
            h('button', { className: 'cart', onClick: function () { if (screen !== 'cart') go('cart'); }, 'aria-label': 'Корзина' },
              icon('cart', { size: 19 }),
              cart.length > 0 && h('span', { className: 'b' }, cart.length)),
            // Профиль — корень навигации, как и табы: goTab, чтобы «назад» с него вёл на главную
            h(ProfileChip, { profile: headerProfile, on: screen === 'profile',
              onClick: function () { if (screen !== 'profile') goTab('profile'); } }))),
        titleText && h('div', { className: 'titleline' }, h('b', null, titleText), tt[1] && h('span', null, tt[1])),
        // Э2: опции пришли от приёмной на VPS — компьютер мастерской выключен. Честно говорим,
        // что работает (заказы), а что нет (мгновенная цена), — до первого же нажатия.
        options && options.offline && h('div', { className: 'offbanner', role: 'status',
          style: { margin: '6px 14px 0', padding: '8px 12px', borderRadius: 12, fontSize: 13, lineHeight: 1.35,
                   background: 'rgba(255,176,32,.14)', color: 'var(--text, #e8edf2)',
                   border: '1px solid rgba(255,176,32,.35)' } },
          '🌙 ', options.offline_note || 'Мастерская сейчас офлайн — заказы принимаем, цену пришлём в чат.')),

      h('div', { className: 'scroll' + (hasCta ? ' has-cta' : '') + (showTabbar || showAdminTabbar ? ' has-tabbar' : ''), ref: scrollRef,
        onScroll: function (e) {
          var el = e.target, key = scrollKey;
          scrollPos.current[key] = el.scrollTop;
          // Якорь пересчитываем не чаще раза в 120 мс: querySelectorAll на каждый кадр прокрутки —
          // лишняя работа в вебвью, а нужен он только к моменту ухода с экрана, то есть после
          // остановки пальца. Последнее событие прокрутки всегда доводит таймер до конца.
          if (anchorTimer.current) return;
          // Считаем сразу (передний фронт), а потом ещё раз в конце пачки: тап по карточке может
          // прийти в те же 120 мс, и уход с экрана не должен застать нас без якоря.
          noteAnchor(el, key);
          anchorTimer.current = setTimeout(function () {
            anchorTimer.current = null;
            // Экран мог смениться: контейнер прокрутки один на всё приложение, и поздний расчёт
            // по СТАРОМУ ключу увидел бы уже чужую разметку — и стёр бы только что взятый якорь.
            if (key === scrollKeyRef.current) noteAnchor(el, key);
          }, 120);
        } },
        adminPending
          ? h(AdminGate, { state: adminLoad,
            onRetry: function () { setAdminLoad('loading'); setAdminTry(adminTry + 1); } })
          : h(Cur, { app: app, secs: secs })),

      screen === 'slicing' && h(window.Slicing, { secs: secs, onNotify: notifyCalc, onOrderNow: orderNow, busy: busy, est: priceEstimate() }),

      loadingModel && h(window.Slicing, { loading: true }),

      errMsg && window.PS_ErrorOverlay && h(window.PS_ErrorOverlay, { msg: errMsg, search: errSearch, onSearch: searchModels,
        queueUrl: errUrl, onQueue: queueForMaster,
        unslicedToken: errUnslicedToken, onOrderUnsliced: orderUnsliced,
        defer: !!deferSrc, onDefer: placeDeferred, busy: busy,
        onClose: function () { setErrMsg(null); setErrUrl(''); setErrUnslicedToken(''); setDeferSrc(null); } }),

      hasCta && h('div', { className: 'cta' },
        h('button', { className: 'btn lg', disabled: busy, onClick: calc }, icon('coin', { size: 19 }), ' Рассчитать стоимость')),

      // нижняя таб-панель: 4 корневых раздела; бейдж «Заказы» = заказы, ждущие ответа по цене.
      // Точка непросмотренных достижений переехала на кружок профиля в шапке (ProfileChip).
      // Дочерние экраны «Приложений» (мастерская, планировщик) подсвечивают свой таб (TAB_OF).
      showTabbar && h('div', { className: 'tabbar' }, TABS.map(function (t) {
        var badge = t.key === 'orders' ? Number((options && options.pending_orders) || 0) : 0;
        var curTab = TAB_OF[screen] || screen;
        return h('button', { key: t.key, className: 'tab' + (curTab === t.key ? ' on' : ''),
          onClick: function () { goTab(t.key); }, 'aria-label': t.label },
          h('span', { className: 'tabic' }, icon(t.ic, { size: 20 }),
            badge > 0 && h('span', { className: 'b' }, badge)),
          h('span', { className: 'lb' }, t.label));
      })),

      // админский таб-бар (этап B IA-редизайна): дочерние экраны подсвечивают родительский таб.
      // Оператору (A4) вкладка «Клиенты» не показывается — клиентская база закрыта правами.
      showAdminTabbar && h('div', { className: 'tabbar' }, ADMIN_TABS.filter(function (t) {
        return t.key !== 'adminClients' || app.can('clients');
      }).map(function (t) {
        var cur = ADMIN_TAB_OF[screen] || 'adminMore';
        return h('button', { key: t.key, className: 'tab' + (cur === t.key ? ' on' : ''),
          onClick: function () { if (t.key === 'adminOrders') { setAdminOrdersFilter(''); } goTab(t.key); }, 'aria-label': t.label },
          h('span', { className: 'tabic' }, icon(t.ic, { size: 20 })),
          h('span', { className: 'lb' }, t.label));
      })),

      toastMsg && h('div', { className: 'toast' }, h('span', { className: 'ok' }, icon('check', { size: 20 })), toastMsg));
  }

  ReactDOM.createRoot(document.getElementById('app-root')).render(h(App));
})();
