// screens_admin.js — admin: dashboard, orders (+detail), queue, printer telemetry
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, Lab = window.PS_Lab, API = window.API, Breakdown = window.PS_Breakdown;

  var ST_LABEL = { new: 'Новый', awaiting_payment: 'Ждёт оплаты', in_progress: 'В работе', printing: 'В работе', price_changed: 'Цена изменена',
    invoiced: 'Счёт выставлен', printed: 'Напечатано', done: 'Готово', rejected: 'Отказ', cancelled: 'Отменён', self: 'Самопечать', handed: 'Вручено',
    deferred: 'Отложен' };
  var ST_COLOR = { new: '#7a8a99', awaiting_payment: '#a98bff', in_progress: '#ffb020', printing: '#ffb020', price_changed: '#a98bff',
    invoiced: '#a98bff', printed: '#2dd47e', done: '#2dd47e', rejected: '#ff5b4d', cancelled: '#ff5b4d', self: '#3a86ff', handed: '#2dd47e',
    deferred: '#4da3ff' };
  var ST_CLASS = { new: 'st-new', awaiting_payment: 'st-progress', in_progress: 'st-progress', printing: 'st-progress', price_changed: 'st-progress',
    invoiced: 'st-progress', printed: 'st-printed', done: 'st-printed', rejected: 'st-rejected', cancelled: 'st-rejected', self: 'st-printed', handed: 'st-printed',
    deferred: 'st-new' };
  var MAT_COLORS = ['#ff7a1a', '#3a86ff', '#1fb573', '#a98bff', '#7a8a99'];

  function refreshBtn(fn) { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: fn }, icon('refresh', { size: 14 }), ' Обновить'); }
  function money(n, cur) { return (Number(n) || 0).toFixed(2).replace('.', ',') + '\u00A0' + (cur || 'руб.'); }
  // Предупреждение о пересчёте доставки по порогу «бесплатно от суммы» (волна 5, 09.08.2026).
  //
  // Стоимость доставки зависит от суммы заказа, а цену мастер назначает уже ПОСЛЕ оформления.
  // Значит, сохранение цены меняет сумму заказа целиком — в том числе сумму УЖЕ ВЫСТАВЛЕННОГО
  // счёта. Telegram сверяет её на pre_checkout и такой счёт отклоняет: клиент увидит отказ
  // платежа, а не «стало дешевле». Мастер обязан узнать это ДО нажатия кнопки.
  //
  // Сами ничего не считаем: сервер прислал в o.group.delivery_recalc готовый порог free_price
  // (цена ЭТОЙ позиции, начиная с которой доставка станет бесплатной) — здесь только сравнение.
  // Блока нет вовсе, когда пересчёта не будет: нет порога/типа, доставка оплачена или её
  // стоимость мастер назначил руками.
  //
  // reissue — действие само отправит новый счёт («Выставить счёт»), звать выставлять заново не надо.
  function deliveryWarn(o, price, cur, reissue) {
    var dr = (o.group || {}).delivery_recalc;
    var p = parseFloat(String(price == null ? '' : price).replace(',', '.'));
    if (!dr || !(p > 0)) return null;
    var next = dr.free_price != null && p >= dr.free_price ? 0 : dr.tariff;
    if (Math.abs(next - dr.cost) < 0.005) return null;      // порог не пересекается — молчим
    var ids = dr.invoice_ids || [];
    var txt = 'Доставка пересчитается по порогу «бесплатно от ' + money(dr.free_from, cur) + '»: '
      + money(dr.cost, cur) + ' → ' + money(next, cur)
      + (next > dr.cost ? ' — с этой ценой заказ до порога не дотягивает, доставка снова станет платной.' : '.');
    if (ids.length) {
      txt += ' Сумма заказа изменится, поэтому счёт, который уже висит у клиента ('
        + (ids.length > 1 ? 'позиции' : 'позиция') + ' #' + ids.join(', #')
        + '), перестанет действовать: Telegram отклонит оплату по нему.'
        + (reissue ? ' Новый счёт уйдёт клиенту сразу — оплатит по нему.'
          : ' Выставьте счёт заново после сохранения цены.');
    }
    return h('p', { className: 'hint', style: { marginTop: 7, color: 'var(--amber)' } },
      icon('bell', { size: 14 }), ' ', txt);
  }

  function plu(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    return b > 1 && b < 5 ? few : (b === 1 ? one : many);
  }

  // ───────── charts ─────────
  // Индекс точки/столбика под курсором. Считаем от ширины ЭЛЕМЕНТА: viewBox растянут по
  // карточке (preserveAspectRatio=none), экранные координаты с координатами svg не совпадают.
  // Для линии берём ближайшую точку (крайние стоят ровно на границах), для столбиков — корзину.
  function pickIdx(e, n, bars) {
    if (!(n > 0)) return -1;
    var r = e.currentTarget.getBoundingClientRect();
    var t = r.width ? (e.clientX - r.left) / r.width : 0;
    t = Math.min(1, Math.max(0, t));
    if (bars) return Math.min(n - 1, Math.floor(Math.min(0.999999, t) * n));
    return n === 1 ? 0 : Math.round(t * (n - 1));
  }
  // Подсказка с цифрами. x — доля ширины (0..1), y — пиксели от верха обёртки. У краёв
  // прижимаем к краю: по центру подсказка вылезала бы за карточку.
  function chartTip(x, y, label, value, below) {
    var pct = Math.min(1, Math.max(0, x)) * 100;
    var ax = pct < 16 ? 'translateX(-6px)' : pct > 84 ? 'translateX(calc(-100% + 6px))' : 'translateX(-50%)';
    return h('div', { className: 'chtip', style: { left: pct + '%', top: y,
      transform: ax + ' ' + (below ? 'translateY(9px)' : 'translateY(calc(-100% - 9px))') } },
    label ? h('span', { className: 'k' }, label) : null, h('b', null, value));
  }
  function AreaChart(props) {
    var data = props.data, color = props.color || 'var(--accent)', hh = props.h || 86, w = 300, pad = 4;
    var hs = useState(-1); var hi = hs[0], setHi = hs[1];
    var n = data.length, max = Math.max.apply(null, data.concat([1]));
    var x = function (i) { return pad + i * (w - pad * 2) / (n - 1); };
    var y = function (v) { return hh - pad - v / max * (hh - pad * 2); };
    var line = data.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1); }).join(' ');
    var area = line + ' L' + x(n - 1).toFixed(1) + ',' + hh + ' L' + x(0).toFixed(1) + ',' + hh + ' Z';
    var gid = 'g' + Math.random().toString(36).slice(2, 7);
    var at = hi >= 0 && hi < n ? hi : -1;
    var xp = at >= 0 ? (x(at) / w * 100) + '%' : null;
    return h('div', { className: 'chwrap',
      onMouseMove: function (e) { setHi(pickIdx(e, n, false)); },
      onMouseLeave: function () { setHi(-1); } },
    h('svg', { viewBox: '0 0 ' + w + ' ' + hh, width: '100%', height: hh, preserveAspectRatio: 'none', style: { display: 'block', overflow: 'visible' } },
      h('defs', null, h('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 },
        h('stop', { offset: 0, stopColor: color, stopOpacity: 0.34 }), h('stop', { offset: 1, stopColor: color, stopOpacity: 0 }))),
      h('path', { d: area, fill: 'url(#' + gid + ')' }),
      h('path', { d: line, fill: 'none', stroke: color, strokeWidth: 2.4, strokeLinejoin: 'round', strokeLinecap: 'round', vectorEffect: 'non-scaling-stroke' }),
      h('circle', { cx: x(n - 1), cy: y(data[n - 1]), r: 3.4, fill: color })),
    at >= 0 ? h('span', { key: 'c', className: 'chcross', style: { left: xp } }) : null,
    at >= 0 ? h('span', { key: 'd', className: 'chdot', style: { left: xp, top: y(data[at]), background: color } }) : null,
    at >= 0 ? chartTip(x(at) / w, y(data[at]), props.labels && props.labels[at],
      props.fmtV ? props.fmtV(data[at]) : data[at], y(data[at]) < 34) : null);
  }
  function Bars(props) {
    var data = props.data, color = props.color || 'var(--accent)', hh = props.h || 72;
    var hs = useState(-1); var hi = hs[0], setHi = hs[1];
    var n = data.length, max = Math.max.apply(null, data.concat([1]));
    var bh = function (v) { return Math.max(3, v / max * hh); };
    return h('div', { className: 'chwrap', style: { display: 'flex', alignItems: 'flex-end', gap: 4, height: hh },
      onMouseMove: function (e) { setHi(pickIdx(e, n, true)); },
      onMouseLeave: function () { setHi(-1); } },
    data.map(function (v, i) {
      return h('div', { key: i, style: { flex: 1, height: bh(v), borderRadius: 3, background: v ? color : 'var(--surface-2)', opacity: i === hi ? 1 : (v ? (0.45 + 0.55 * v / max) : 1) } });
    }),
    hi >= 0 && hi < n ? chartTip((hi + 0.5) / n, hh - bh(data[hi]), props.labels && props.labels[hi],
      props.fmtV ? props.fmtV(data[hi]) : data[hi], hh - bh(data[hi]) < 34) : null);
  }
  function Donut(props) {
    var segs = props.segs, size = props.size || 116, sw = props.sw || 16;
    var r = (size - sw) / 2, C = 2 * Math.PI * r, total = segs.reduce(function (a, s) { return a + s[1]; }, 0) || 1, off = 0;
    return h('svg', { width: size, height: size, viewBox: '0 0 ' + size + ' ' + size },
      h('circle', { cx: size / 2, cy: size / 2, r: r, fill: 'none', stroke: 'var(--surface-2)', strokeWidth: sw }),
      segs.map(function (s, i) {
        var frac = s[1] / total, dash = frac * C;
        var el = h('circle', { key: i, cx: size / 2, cy: size / 2, r: r, fill: 'none', stroke: s[2], strokeWidth: sw,
          strokeDasharray: dash + ' ' + (C - dash), strokeDashoffset: -off * C, transform: 'rotate(-90 ' + (size / 2) + ' ' + (size / 2) + ')', strokeLinecap: 'butt' });
        off += frac; return el;
      }),
      h('text', { x: '50%', y: '48%', textAnchor: 'middle', fill: 'var(--text)', fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 22 }, total),
      h('text', { x: '50%', y: '63%', textAnchor: 'middle', fill: 'var(--faint)', fontSize: 10 }, 'всего'));
  }

  // ─────────────────────── ADMIN HUB (Вариант A — умные плитки) ───────────────────────
  // Карточка принтера: живой прогресс через ту же телеметрию, что AdminPrinter (парсер gf()).
  // printer === null → нейтральный скелетон; не печатает / нет связи → «спящая» карточка; печатает → LIVE.
  function PrinterCard(props) {
    var app = props.app, p = props.printer;
    function open() { app.go('adminPrinter'); }
    if (!p) {   // ещё опрашиваем — скелетон, без полноэкранного «Загружаю…»
      return h('button', { className: 'pcard', onClick: open },
        h('div', { className: 'ph' },
          h('div', { className: 'ic', style: { background: 'var(--surface-2)', color: 'var(--faint)' } }, icon('printer', { size: 22 })),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { className: 'nm', style: { color: 'var(--muted)' } }, 'Принтер'),
            h('div', { className: 'id', style: { color: 'var(--faint)' } }, 'опрашиваю…'))),
        h('div', { className: 'prow' }, h('span', { className: 'pct', style: { color: 'var(--faint)' } }, '—'), h('span', { className: 'ly' })),
        h('div', { className: 'pbar' }, h('i', { style: { width: '0%' } })));
    }
    var stt = p.state || {}, sum = p.summary || {};
    var printing = gf(stt, 'print_stats.state') === 'printing';
    if (!printing) {   // простой / нет связи — «спящая» карточка без LIVE, бар 0%
      return h('button', { className: 'pcard', onClick: open },
        h('div', { className: 'ph' },
          h('div', { className: 'ic', style: { background: 'var(--surface-2)', color: 'var(--muted)' } }, icon('printer', { size: 22 })),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { className: 'nm' }, 'Принтер не печатает'),
            h('div', { className: 'id', style: { color: 'var(--faint)' } }, p.ok ? 'в простое' : (p.error || 'нет связи')))),
        h('div', { className: 'prow' }, h('span', { className: 'pct', style: { color: 'var(--faint)' } }, '0%'), h('span', { className: 'ly' })),
        h('div', { className: 'pbar' }, h('i', { style: { width: '0%' } })));
    }
    var prog = gf(stt, 'virtual_sdcard.progress', 'progress') || 0;
    var pct = Math.round(prog * 100);
    var layer = gf(stt, 'info.current_layer', 'current_layer', 'layer');
    var layers = gf(stt, 'info.total_layer', 'total_layer', 'layers');
    var jobName = gf(stt, 'print_stats.filename', 'filename') || gf(stt, 'plastic') || 'Печать';
    var oid = gf(stt, 'order_id');
    var remaining = gf(stt, 'remaining');
    var noz = sum.extruder_temp, bed = sum.bed_temp;
    return h('button', { className: 'pcard', onClick: open },
      h('div', { className: 'ph' },
        h('div', { className: 'ic' }, icon('printer', { size: 22 })),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { className: 'nm' }, 'Печатает сейчас'),
          h('div', { className: 'id' }, (oid ? '#' + oid + ' · ' : '') + jobName)),
        h('span', { className: 'live' }, h('span', { className: 'blip' }), 'LIVE')),
      h('div', { className: 'prow' },
        h('span', { className: 'pct' }, pct + '%'),
        (layer != null && layers != null) && h('span', { className: 'ly' }, 'слой ' + layer + ' / ' + layers)),
      h('div', { className: 'pbar' }, h('i', { style: { width: pct + '%' } })),
      h('div', { className: 'meta' },
        remaining != null && h('span', { className: 's' }, icon('clock', { size: 14 }), 'осталось ', h('b', null, '~' + remaining)),
        noz != null && h('span', { className: 's' }, icon('flame', { size: 14 }), h('b', null, Math.round(noz) + '°')),
        bed != null && h('span', { className: 's' }, icon('bed', { size: 14 }), h('b', null, Math.round(bed) + '°'))));
  }

  // «N ч M мин» для очереди и журнала на Пульте (как fmtMin в настольной админке)
  function fmtMin(m) {
    m = Math.round(Number(m) || 0);
    if (m < 60) return m + ' мин';
    return Math.floor(m / 60) + ' ч' + (m % 60 ? ' ' + (m % 60) + ' мин' : '');
  }
  // «5 мин назад / 3 ч / 2 дн» для ленты событий (unix-секунды)
  function agoShort(ts) {
    var s = Math.max(0, Date.now() / 1000 - (Number(ts) || 0));
    if (!ts) return '';
    if (s < 60) return 'сейчас';
    if (s < 3600) return Math.round(s / 60) + ' мин';
    if (s < 86400) return Math.round(s / 3600) + ' ч';
    return Math.round(s / 86400) + ' дн';
  }

  function Admin(props) {
    var app = props.app;
    var hs = useState(null); var hub = hs[0], setHub = hs[1];
    var ps = useState(null); var printer = ps[0], setPrinter = ps[1];
    var qs = useState(null); var queue = qs[0], setQueue = qs[1];     // очередь печати (null = ещё не спрашивали)
    var ts = useState(null); var tasks = ts[0], setTasks = ts[1];     // задачи мастера
    var evs = useState(null); var events = evs[0], setEvents = evs[1];   // лента событий (право «Клиенты»)
    var js = useState(null); var prints = js[0], setPrints = js[1];   // последние печати — оператору вместо ленты
    var canClients = app.can('clients');
    // Принтер опрашиваем отдельно (как AdminPrinter): при разовом сбое держим последний живой снимок.
    function loadPrinter() {
      API.adminPrinterStatus().then(function (j) {
        setPrinter(function (prev) { return ((!j || !j.ok) && prev && prev.ok) ? prev : (j || { ok: false }); });
      }).catch(function () {});
    }
    function loadQueue() { API.adminQueue().then(function (j) { setQueue((j && j.ok && j.queue) || []); }).catch(function () { setQueue([]); }); }
    function loadTasks() { API.adminTasks().then(function (j) { setTasks((j && j.ok && j.tasks) || []); }).catch(function () { setTasks([]); }); }
    // Лента событий — клиентская база, оператору закрыта правом (A4). Ему в тот же слот идут
    // последние печати из журнала (право «Принтер» у него есть) — как WorkCard/PrintsCard на ПК.
    // Гейт стоит на ВЫЗОВЕ, а не только на отрисовке: 403 на каждый вход Пульта — не дело.
    function loadFeed() {
      if (canClients) API.adminActivity().then(function (j) { setEvents((j && j.ok && j.events) || []); }).catch(function () { setEvents([]); });
      else API.adminJournal().then(function (j) { setPrints((j && j.ok && (j.items || j.journal)) || []); }).catch(function () { setPrints([]); });
    }
    useEffect(function () {
      API.adminHub().then(function (j) { if (j && j.ok) setHub(j); });   // счётчики — один раз на вход
      loadQueue(); loadTasks(); loadFeed(); loadPrinter();
      var iv = setInterval(loadPrinter, 2500);                           // живой принтер ~раз в 2.5 c
      // очередь и лента обновляются сами — на Пульт смотрят фоном, как на ПК (15 с)
      var iv2 = setInterval(function () { loadQueue(); loadFeed(); }, 15000);
      return function () { clearInterval(iv); clearInterval(iv2); };
    }, []);
    function toggleTask(x) {
      API.adminTask({ id: x.id, action: x.done ? 'reopen' : 'done' }).then(function (j) {
        if (j && j.ok) { if (!x.done) app.toast('Задача выполнена'); loadTasks(); }
        else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }

    var at = (hub && hub.attention) || {};
    var rev = (hub && hub.revenue) || {};
    var cur = (hub && hub.currency) || 'руб.';
    var attnSum = (at.new_orders || 0) + (at.unread || 0) + (at.price_wait || 0);
    function dash(v) { return hub ? v : '—'; }            // скелетон: до загрузки — нейтральный прочерк
    var totalMin = (queue || []).reduce(function (s, x) { return s + (Number(x.minutes) || 0); }, 0);
    var openTasks = (tasks || []).filter(function (x) { return !x.done; }).slice(0, 5);
    // одна строка компактного списка Пульта (очередь / задачи / события / печати)
    function emptyRow(text) {
      return h('div', { className: 'pu-item', style: { color: 'var(--faint)', fontSize: 12.5 } }, text);
    }
    function moreBtn(label, onClick) {
      return h('button', { className: 'btn line', type: 'button', style: { margin: '6px 0 10px', padding: '8px' }, onClick: onClick }, label);
    }

    // Пульт повторяет обстановку настольного «Пульта» (desk pult.jsx), сжатую под телефон:
    // внимание → принтер → очередь → выручка → задачи на сегодня → лента событий (2026-09-02).
    // Заказы/Печать/Клиенты — в админском таб-баре, полная карта разделов — на экране «Ещё».

    return h('div', { className: 'screen' },
      // «всё спокойно» вместо пустоты (аудит UX): утренний взгляд получает подтверждение
      (hub && attnSum === 0) && h('div', { className: 'card', style: { padding: '11px 14px', display: 'flex', alignItems: 'center', gap: 9, color: 'var(--price)', fontSize: 13, fontWeight: 600 } },
        icon('check', { size: 17 }), 'Всё спокойно — новых заказов и сообщений нет'),
      // Требует внимания — скрыт при нулевой сумме (и пока хаб не загружен)
      (hub && attnSum > 0) && h('div', { className: 'attn' },
        h('div', { className: 'h' }, h('span', { className: 'dot' }), 'Требует внимания'),
        h('div', { className: 'attn-row' },
          h('button', { className: 'attn-chip acc', onClick: function () { app.openAdminOrders('new'); } },
            h('div', { className: 'n' }, at.new_orders || 0), h('div', { className: 'l' }, 'новых заказа')),
          h('button', { className: 'attn-chip', onClick: function () { app.openAdminOrders('msg'); } },
            h('div', { className: 'n' }, at.unread || 0), h('div', { className: 'l' }, 'сообщения от клиентов')),
          h('button', { className: 'attn-chip violet', onClick: function () { app.openAdminOrders('price'); } },
            h('div', { className: 'n' }, at.price_wait || 0), h('div', { className: 'l' }, 'ждёт ответа по цене')))),

      // ссылка «Принтер →» убрана (аудит, проблема #8): вход один — тап по самой карточке
      h('div', { className: 'lab' }, 'Печать'),
      h(PrinterCard, { app: app, printer: printer }),
      // ── Очередь печати (QueueCard на ПК): первые три позиции и суммарное время, тап → заказ ──
      queue && queue.length > 0 && h('div', { className: 'lab' }, 'Очередь печати · ' + queue.length,
        totalMin > 0 && h('span', { className: 'r mono', style: { textTransform: 'none', letterSpacing: 0, fontWeight: 500 } }, '≈ ' + fmtMin(totalMin))),
      queue && queue.length > 0 && h('div', { className: 'card', style: { padding: '2px 13px' } },
        queue.slice(0, 3).map(function (x, i) {
          return h('div', { key: x.id, className: 'pu-item', style: { cursor: 'pointer' }, onClick: function () { app.openAdminOrder(x.id); } },
            h('span', { className: 'mono', style: { color: 'var(--faint)', fontSize: 11.5, flex: '0 0 14px' } }, i + 1),
            h('span', { className: 'tt' }, '#' + x.id + ' · ' + (x.label || '—')),
            Number(x.minutes) > 0 && h('span', { className: 'tm' }, fmtMin(x.minutes)));
        }),
        queue.length > 3 && moreBtn('Вся очередь · ' + queue.length, function () { app.go('adminPrint'); })),

      // Выручка — только тем, у кого есть право на деньги (A4). У оператора этой карточки нет,
      // и сервер всё равно вернул бы по ней пустые суммы.
      app.can('finance') && h('div', { className: 'dashpeek', style: { marginTop: 10 }, onClick: function () { app.go('adminDash'); } },
        h('div', { className: 'k' },
          h('div', { className: 'l' }, icon('coin', { size: 14 }), 'Выручка сегодня'),
          h('div', { className: 'v green' }, hub ? money(rev.today, cur) : '—'),
          h('div', { className: 'd' }, hub ? ('+' + money(rev.week, cur) + ' за неделю') : '')),
        h('div', { className: 'k' },
          h('div', { className: 'l' }, icon('receipt', { size: 14 }), 'Заказов / неделя'),
          h('div', { className: 'v' }, dash(rev.orders_week)),
          h('div', { className: 'd', style: { color: 'var(--muted)' } }, hub ? ('средний чек ' + money(rev.avg_order, cur)) : '')),
        h('span', { className: 'go' }, icon('chev', { size: 18 }))),

      // ── Задачи на сегодня (TasksCard на ПК): галочка закрывает задачу, «Все» → экран задач ──
      tasks && h('div', { className: 'lab' }, 'Задачи на сегодня',
        h('span', { className: 'r' }, h('button', { className: 'chip', type: 'button', style: { padding: '4px 9px', fontSize: 11.5 },
          onClick: function () { app.go('adminTasks'); } }, 'Все'))),
      tasks && h('div', { className: 'card', style: { padding: '2px 13px' } },
        openTasks.length ? openTasks.map(function (x) {
          return h('div', { key: x.id, className: 'pu-item' + (x.done ? ' done' : '') },
            h('button', { className: 'cb', type: 'button', 'aria-label': x.done ? 'Вернуть' : 'Выполнено', onClick: function () { toggleTask(x); } },
              x.done && icon('check', { size: 13, sw: 3 })),
            h('span', { style: { flex: 1, minWidth: 0 } },
              h('div', { className: 'tt' }, x.title),
              (x.due_label || x.client) && h('div', { className: 'ds' + (x.overdue ? ' red' : (x.today ? ' hot' : '')) },
                (x.overdue ? '⏰ ' : '') + (x.due_label || x.client || ''))));
        }) : emptyRow('Задач нет — ничего не висит')),

      // ── Лента событий (EventsCard на ПК) — тем, кому открыта клиентская база ──
      canClients && events && h('div', { className: 'lab' }, 'Лента событий',
        h('span', { className: 'r', style: { color: 'var(--price)', textTransform: 'none', letterSpacing: 0, fontWeight: 600 } }, 'live')),
      canClients && events && h('div', { className: 'card', style: { padding: '2px 13px' } },
        events.length ? events.slice(0, 6).map(function (e) {
          return h('div', { key: e.id, className: 'pu-item', style: { cursor: e.user_id ? 'pointer' : 'default' },
            onClick: function () { if (e.user_id) app.openAdminClient(e.user_id); } },
            h('span', { className: 'dot', style: { background: /заказ|оплат/i.test(e.action || '') ? 'var(--accent)' : 'var(--muted)' } }),
            h('span', { style: { flex: 1, minWidth: 0 } },
              h('div', { className: 'tt' }, (e.name || 'Гость') + ' — ' + (e.action || '')),
              e.detail && h('div', { className: 'ds' }, e.detail)),
            h('span', { className: 'tm' }, agoShort(e.at)));
        }) : emptyRow('Пока тихо — клиенты ничего не делали'),
        events.length > 0 && moreBtn('Вся активность', function () { app.go('adminActivity'); })),

      // ── Последние печати (PrintsCard на ПК) — оператору, которому клиенты закрыты ──
      !canClients && prints && h('div', { className: 'lab' }, 'Последние печати'),
      !canClients && prints && h('div', { className: 'card', style: { padding: '2px 13px' } },
        prints.length ? prints.slice(0, 6).map(function (x) {
          return h('div', { key: x.id, className: 'pu-item', style: { cursor: 'pointer' }, onClick: function () { app.go('adminMasterLog'); } },
            h('span', { className: 'dot', style: { background: 'var(--amber)' } }),
            h('span', { style: { flex: 1, minWidth: 0 } },
              h('div', { className: 'tt' }, x.label || 'Без названия'),
              h('div', { className: 'ds' }, [x.plastic_name, x.grams ? Math.round(x.grams) + ' г' : '', x.minutes ? fmtMin(x.minutes) : ''].filter(Boolean).join(' · '))),
            h('span', { className: 'tm' }, x.ago || ''));
        }) : emptyRow('Журнал пока пуст')),

      hub && h('p', { className: 'hint', style: { marginTop: 12 } },
        'Заказы, Печать и Клиенты — в панели снизу; полная карта разделов — во вкладке «Ещё».'));
  }

  // ───────────────────── ADMIN · ЕЩЁ (карта разделов) ─────────────────────
  // 2026-09-02 (задача владельца): мобильная админка ПОВТОРЯЕТ ИЕРАРХИЮ настольной
  // (desk/src/shell.jsx NAV) — те же четыре группы «Операции · Каталог · Аналитика ·
  // Инструменты», в том же порядке, с теми же названиями и правами, чтобы мастер, привыкший к
  // ПК, находил раздел на телефоне не думая. Оптимизация под телефон: четыре самых частых
  // раздела вынесены в таб-бар (здесь они тоже есть — полная карта, — и открываются как табы),
  // а поиск сверху заменяет палитру Ctrl+K: заказы, клиенты и разделы одной строкой.
  // Формат плитки — [иконка, заголовок, подпись, экран, право?, бейдж?, приглушённый?]: его
  // разбирает tests/test_front_contracts.py (право = пятое поле, '' — открыт всем).
  function AdminMore(props) {
    var app = props.app;
    var hs = useState(null); var hub = hs[0], setHub = hs[1];
    var qS = useState(''); var q = qS[0], setQ = qS[1];
    var rS = useState(null); var hits = rS[0], setHits = rS[1];   // {orders, clients} | null
    var tRef = useRef(null);
    useEffect(function () { API.adminHub().then(function (j) { if (j && j.ok) setHub(j); }); }, []);
    // Поиск идёт на сервер, как в палитре деска: заказы — всем, клиенты — только с правом
    // (иначе у оператора 403 на каждый набранный символ). Дебаунс, чтобы не долбить бота.
    useEffect(function () {
      clearTimeout(tRef.current);
      var ql = q.trim();
      if (ql.length < 2) { setHits(null); return undefined; }
      var dead = false;
      tRef.current = setTimeout(function () {
        Promise.all([
          API.adminOrders(ql).catch(function () { return null; }),
          app.can('clients') ? API.adminClients({ q: ql }).catch(function () { return null; }) : Promise.resolve(null),
        ]).then(function (r) {
          if (dead) return;
          setHits({ orders: ((r[0] && r[0].orders) || []).slice(0, 5), clients: ((r[1] && r[1].clients) || []).slice(0, 4) });
        });
      }, 250);
      return function () { dead = true; };
    }, [q]);
    var cnt = (hub && hub.counts) || {}, at = (hub && hub.attention) || {};
    function dash(v) { return hub ? v : '—'; }
    var ql = q.trim().toLowerCase();
    var TAB_KEYS = { admin: 1, adminOrders: 1, adminPrint: 1, adminClients: 1 };   // корни таб-бара
    function tiles(arr) {
      var vis = arr.filter(Boolean)   // выключенная возможность сборки оставляет false вместо строки
        .filter(function (c) { return !c[4] || app.can(c[4]); })
        .filter(function (c) { return !ql || c[1].toLowerCase().indexOf(ql) >= 0 || String(c[2] || '').toLowerCase().indexOf(ql) >= 0; });
      if (!vis.length) return null;
      return h('div', { className: 'cfg' }, vis.map(function (c, i) {
        var bdg = Number(c[5]) || 0;
        return h('button', { className: 'cfgtile' + (TAB_KEYS[c[3]] ? ' tabbed' : ''), key: i,
          onClick: function () { if (TAB_KEYS[c[3]]) app.goTab(c[3]); else app.go(c[3]); } },
          h('div', { className: 'ic' }, icon(c[0], { size: 19 })),
          h('div', null, h('div', { className: 'tt' }, c[1]), h('div', { className: 'ct' }, c[2])),
          bdg > 0 && h('span', { className: 'bdg' + (c[6] ? ' mut' : '') }, bdg));
      }));
    }
    function group(label, node) { return node ? h(React.Fragment, { key: label }, h('div', { className: 'lab' }, label), node) : null; }
    var ordersBdg = (at.new_orders || 0) + (at.unread || 0);
    var hitN = hits ? hits.orders.length + hits.clients.length : 0;
    return h('div', { className: 'screen' },
      h('div', { className: 'more-search' }, icon('search', { size: 16, style: { color: 'var(--faint)', flex: '0 0 auto' } }),
        h('input', { value: q, placeholder: 'Заказ, клиент или раздел…', onChange: function (e) { setQ(e.target.value); } }),
        q && h('button', { className: 'clr', type: 'button', 'aria-label': 'Очистить', onClick: function () { setQ(''); } }, icon('close', { size: 15 }))),
      hitN > 0 && h('div', { style: { marginBottom: 8 } },
        hits.orders.map(function (o) {
          return h('button', { key: 'o' + o.id, className: 'more-hit', type: 'button', onClick: function () { app.openAdminOrder(o.id); } },
            o.preview ? h('img', { src: o.preview, alt: '' }) : h('span', { className: 'ic' }, icon('receipt', { size: 16 })),
            h('span', { className: 'tt' }, '#' + o.id + ' · ' + (o.label || '—')), h('span', { className: 'pk' }, 'заказ'));
        }),
        hits.clients.map(function (c) {
          return h('button', { key: 'c' + c.uid, className: 'more-hit', type: 'button', onClick: function () { app.openAdminClient(c.uid); } },
            h('span', { className: 'ic' }, icon('star', { size: 16 })),
            h('span', { className: 'tt' }, c.name || ('id ' + c.uid)), h('span', { className: 'pk' }, 'клиент'));
        })),
      hits && !hitN && h('p', { className: 'hint', style: { margin: '0 0 8px' } }, 'Заказов и клиентов по «' + q.trim() + '» нет — ниже подходящие разделы.'),
      group('Операции', tiles([
        ['bolt', 'Пульт', 'обстановка в мастерской', 'admin'],
        ['receipt', 'Заказы', 'клиентские заявки', 'adminOrders', '', ordersBdg],
        ['printer', 'Печать', 'очередь · принтер · ТО · для себя', 'adminPrint'],
        ['cal', 'План недели', 'дедлайны × мощность принтера', 'adminPlan'],
        ['star', 'Клиенты', 'CRM', 'adminClients', 'clients'],
        ['task', 'Задачи', hub && cnt.tasks ? (cnt.tasks + ' открытых') : 'напоминания мастера', 'adminTasks', '', cnt.tasks || 0, 1],
      ])),
      group('Каталог', tiles([
        // «Оттенки» — вкладка внутри «Пластиков», как на ПК (там цвета живут в карточке пластика):
        // отдельной плитки в «Каталоге» больше нет (фидбэк владельца 03.09.2026)
        ['spool', 'Пластики', dash(cnt.plastics) + ' видов · ' + dash(cnt.shades) + ' оттенков', 'adminPlastics', 'pricing'],
        ['coin', 'Цены и скидки', 'ставки · тиры · промокоды', 'adminPricing', 'pricing'],
        ['truck', 'Доставка', 'типы · тарифы · сроки', 'adminDelivery', 'pricing'],
        ['globe', 'Коллекция', dash(cnt.collection) + ' моделей', 'adminCollectionAdmin', 'pricing'],
        ['gift', 'Готовые изделия', dash(cnt.products) + ' в продаже', 'adminProducts', 'pricing'],
        // как на ПК — целиком владельческий: каждая правка профиля идёт под «Настройками»
        ['box', 'Принтеры', 'профили слайсера', 'adminPrinters', 'settings'],
      ])),
      group('Аналитика', tiles([
        ['chart', 'Дашборд', 'метрики · Big Data', 'adminDash', 'finance'],
        ['pulse', 'Активность', 'действия клиентов', 'adminActivity', 'clients'],
        ['shield', 'Статус', 'система · ИИ · GPU', 'adminStatus'],
        // телеметрия ошибок (f4de8627): бейдж — сколько сбоев ждёт разбора
        ['flame', 'Ошибки', hub && cnt.errors ? (cnt.errors + ' новых') : 'телеметрия сбоев', 'adminErrors', 'settings', cnt.errors || 0],
      ])),
      group('Инструменты', tiles([
        // рабочие инструменты у станка (допечатка со слоя, Путь В) — как на ПК, право «Принтер»
        ['wrench', 'Инструменты', 'допечатка со слоя', 'adminTools', 'printer'],
        ['bell', 'Уведомления', hub && cnt.notifications ? (cnt.notifications + ' новых') : 'черновики и события', 'adminNotifs', 'settings', cnt.notifications || 0, 1],
        // база знаний + вход в чат владельцев студий — один раздел, как на ПК (c356387a)
        ['chat', 'Сообщество', 'база знаний · чат владельцев', 'adminCommunity', 'settings'],
        ['users', 'Семейные счета', hub && cnt.family_requests ? (cnt.family_requests + ' заявок') : 'общий баланс', 'adminFamilies', 'clients', cnt.family_requests || 0, 1],
        ['doc', 'Журнал мастера', 'разбор ссылок · печати', 'adminMasterLog'],
        ['flame', 'Калибровка', 'кампании · паспорта · пресеты', 'adminCalib'],
        // планировщик — возможность сборки (f98aa1ef): в студийной сборке пункта нет, как и на ПК
        window.PS_hasFeature('planner') && ['wall', 'Планировщик стены', 'Multiboard', 'planner'],
        ['users', 'Команда', 'сотрудники · роли и права', 'adminStaff', 'settings'],
        ['gear', 'Настройки', 'бренд · доступ · поведение бота', 'adminStudio', 'settings'],
      ])),
      // выход в клиентскую часть: у мастера те же клиентские табы, что у юзеров
      h('button', { className: 'btn line', style: { marginTop: 18 }, onClick: function () { app.goTab('hub'); } },
        icon('back', { size: 16 }), ' Клиентский режим'));
  }

  // ───────────────────── ADMIN · DASHBOARD ─────────────────────
  function AdminDash(props) {
    var app = props.app;
    var st = useState(null); var d = st[0], setD = st[1];
    var cs = useState(null); var crm = cs[0], setCrm = cs[1];   // воронка периодов + отказы (crm этап 6)
    var ps = useState(''); var per = ps[0], setPer = ps[1];     // '' = всё время | '7d' | '30d'
    function load() { API.adminDashboard().then(function (j) { if (j.ok) { setD(j.stats); setCrm(j.crm || null); } }); }
    useEffect(function () { load(); }, []);
    // Big Data — вкладка дашборда (admin-routes-plan.md шаг 5); старый ключ adminBigData жив.
    var tbs = useState('dash'); var tab = tbs[0], setTab = tbs[1];
    var tabChips = h('div', { className: 'chips', style: { marginBottom: 11 } },
      // setView — своя запомненная позиция скролла на вкладку (861eab19); вкладка по
      // умолчанию («Метрики») = '', под этим ключом App хранит позицию до первого setView
      h('button', { className: 'chip' + (tab === 'dash' ? ' on' : ''), type: 'button', onClick: function () { setTab('dash'); if (app.setView) app.setView(''); } }, 'Метрики'),
      h('button', { className: 'chip' + (tab === 'big' ? ' on' : ''), type: 'button', onClick: function () { setTab('big'); if (app.setView) app.setView('big'); } }, '⚡ Big Data'));
    if (tab === 'big') {
      return h(React.Fragment, null,
        h('div', { className: 'screen', style: { paddingBottom: 0 } }, tabChips),
        window.AdminBigData ? h(window.AdminBigData, { app: app }) : null);
    }
    if (!d) return h('div', { className: 'screen' }, tabChips, h(Lab, null, 'Дашборд'), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…'));
    var rev14 = (d.series14 || []).map(function (s) { return s.revenue; });
    var ord14 = (d.series14 || []).map(function (s) { return s.orders; });
    // подписи дней для подсказок под курсором: ряд series14 всегда кончается сегодняшними сутками
    var day14 = rev14.map(function (_, i) {
      var dd = new Date(); dd.setDate(dd.getDate() - (rev14.length - 1 - i));
      return ('0' + dd.getDate()).slice(-2) + '.' + ('0' + (dd.getMonth() + 1)).slice(-2);
    });
    var sm = d.status_mix || {};
    var segs = Object.keys(sm).map(function (k) { return [ST_LABEL[k] || k, sm[k], ST_COLOR[k] || '#7a8a99']; });
    var pd = (per && crm && crm[per]) || null;
    var funnel = pd
      ? [['Расчёты', pd.calcs || 0], ['Заказы', pd.orders || 0], ['Напечатано', pd.printed || 0]]
      : [['Расчёты', d.calcs || 0], ['Заказы', d.orders || 0], ['В печати', sm.in_progress || 0], ['Напечатано', d.printed || 0]];
    var mats = d.top_materials || [];
    var matMax = Math.max.apply(null, mats.map(function (m) { return m[1]; }).concat([1]));
    function kpi(ic, label, value, unit, delta) {
      return h('div', { className: 'kpi' },
        h('div', { className: 'kl' }, icon(ic, { size: 14 }), ' ', label),
        h('div', { className: 'kv' }, value, unit && h('small', null, ' ' + unit)),
        delta && h('div', { className: 'kd' }, delta));
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(load) }, 'Дашборд'),
      tabChips,
      h('div', { className: 'kpis' },
        kpi('coin', 'Выручка', money(d.revenue_all).replace('\u00A0руб.', ''), 'руб.', '+' + money(d.orev_7d).replace('\u00A0руб.', '') + ' за 7 дн'),
        kpi('receipt', 'Заказы', d.orders, '', '+' + (d.orders_7d || 0) + ' за неделю'),
        kpi('box', 'Средний чек', money(d.avg_order).replace('\u00A0руб.', ''), 'руб.'),
        kpi('spool', 'Филамент', ((d.grams || 0) / 1000).toFixed(1), 'кг', (d.printed || 0) + ' напечатано')),
      h('div', { className: 'chartcard' },
        h('div', { className: 'ct' }, h('b', null, 'Выручка · 14 дней'), h('span', null, money(d.orev_30d))),
        h(AreaChart, { data: rev14, color: 'var(--accent)', labels: day14,
          fmtV: function (v) { return money(v); } })),
      h('div', { className: 'chartcard' },
        h('div', { className: 'ct' }, h('b', null, 'Заказы по дням'), h('span', null, (d.orders_30d || 0) + ' за месяц')),
        h(Bars, { data: ord14, color: 'var(--accent)', labels: day14,
          fmtV: function (v) { return v + ' ' + plu(v, 'заказ', 'заказа', 'заказов'); } })),
      h('div', { className: 'chartcard' },
        h('div', { className: 'ct' }, h('b', null, 'Воронка'),
          crm ? h('span', { style: { display: 'inline-flex', gap: 5 } }, [['', 'всё'], ['30d', '30 дн'], ['7d', '7 дн']].map(function (p) {
            return h('button', { key: p[0] || 'all', className: 'chip' + (per === p[0] ? ' on' : ''), style: { padding: '4px 9px', fontSize: 11.5 }, onClick: function () { setPer(p[0]); } }, p[1]);
          })) : h('span', null, 'конверсия ' + Math.round((d.conv || 0)) + '%')),
        h('div', { className: 'funnel' }, funnel.map(function (f, i) {
          return h('div', { key: i, className: 'fr' },
            h('div', { className: 'fl' }, h('span', null, f[0]), h('span', { className: 'v' }, f[1])),
            h('div', { className: 'ft' }, h('i', { style: { width: (f[1] / (funnel[0][1] || 1) * 100) + '%' } })));
        })),
        pd && h('p', { className: 'hint', style: { marginTop: 7, marginBottom: 0 } },
          'Конверсия за период: расчёт→заказ ' + (pd.calcs ? Math.round(pd.orders / pd.calcs * 100) : 0) + '% · заказ→печать ' + (pd.orders ? Math.round(pd.printed / pd.orders * 100) : 0) + '%')),
      // причины отказов (crm-roadmap этап 6): заполняются при переводе заказа в «Отказ»
      crm && (crm.reject_reasons || []).length > 0 && h('div', { className: 'chartcard' },
        h('div', { className: 'ct' }, h('b', null, 'Причины отказов'), h('span', null, (crm.rejected_total || 0) + ' отказов')),
        h('div', { className: 'legend' }, (function () {
          var rmax = Math.max.apply(null, crm.reject_reasons.map(function (x) { return x[1]; }).concat([1]));
          return crm.reject_reasons.map(function (rr, i) {
            return h('div', { key: i, className: 'lr' },
              h('span', { style: { flex: '0 0 auto', maxWidth: 150, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } }, rr[0]),
              h('span', { className: 'bar' }, h('i', { style: { width: (rr[1] / rmax * 100) + '%', background: 'var(--red)' } })),
              h('span', { className: 'v' }, rr[1]));
          });
        })())),
      segs.length > 0 && h('div', { className: 'chartcard' },
        h('div', { className: 'ct' }, h('b', null, 'Статусы заказов')),
        h('div', { style: { display: 'flex', gap: 16, alignItems: 'center' } },
          h(Donut, { segs: segs }),
          h('div', { className: 'legend', style: { flex: 1, marginTop: 0 } }, segs.map(function (s, i) {
            return h('div', { key: i, className: 'lr' }, h('span', { className: 'sw', style: { background: s[2] } }), s[0], h('span', { className: 'v' }, s[1]));
          })))),
      mats.length > 0 && h('div', { className: 'chartcard' },
        h('div', { className: 'ct' }, h('b', null, 'Топ материалов')),
        h('div', { className: 'legend' }, mats.map(function (m, i) {
          var c = MAT_COLORS[i % MAT_COLORS.length];
          return h('div', { key: i, className: 'lr' }, h('span', { className: 'sw', style: { background: c } }),
            h('span', { style: { flex: '0 0 auto', maxWidth: 130, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } }, m[0]),
            h('span', { className: 'bar' }, h('i', { style: { width: (m[1] / matMax * 100) + '%', background: c } })),
            h('span', { className: 'v' }, m[1]));
        }))));
  }

  // ───────────────────── ADMIN · ORDERS ─────────────────────
  // Кнопка «Экспорт в CSV»: ведём на /api/admin/orders/export.csv (отдаётся как attachment).
  // В Telegram открываем во внешнем браузере (tg.openLink), иначе — обычный window.open.
  function exportCsv() {
    var url = API.adminOrdersCsvUrl();
    if (!url) return;   // mock-режим: эндпоинта нет
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
    else window.open(url, '_blank');
  }
  function csvBtn() { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: exportCsv }, icon('doc', { size: 14 }), ' CSV'); }
  // открыть внешнюю ссылку (оригинал модели и т.п.) — в Telegram через openLink, иначе window.open
  function exportLink(url) {
    if (!url) return;
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
    else window.open(url, '_blank');
  }

  // Оформить заказ за клиента (OpenTodo cd3258d6): клиент написал в личку мимо системы — мастер
  // создаёт заказ сам (принтер/пластик/качество/прочность/тираж + готовые граммы-время), бот шлёт
  // его клиенту на подтверждение (статус 'invoiced', тот же поток принять/отклонить, что у счёта).
  function CreateOrderForm(props) {
    var app = props.app, cur = props.cur, onDone = props.onDone;
    var os = useState(null); var opts = os[0], setOpts = os[1];           // {printers,plastics,qualities,strengths}
    var cs = useState([]); var customers = cs[0], setCustomers = cs[1];   // известные клиенты для пикера
    var fs = useState({ user_id: '', printer_id: 0, plastic_id: 0, quality_id: 0, strength_id: 0,
      qty: '1', grams: '', minutes: '', price: '', label: '', comment: '' });
    var f = fs[0], setF = fs[1];
    var calcS = useState(null); var calc = calcS[0], setCalc = calcS[1];  // {price, calc_price}
    var bS = useState(false); var busy = bS[0], setBusy = bS[1];
    useEffect(function () {
      API.options().then(function (o) {
        if (!o || !o.ok) return;
        var def = function (arr) { var d = (arr || []).filter(function (x) { return x.is_default; })[0]; return (d || (arr || [])[0] || {}).id || 0; };
        setOpts(o);
        setF(function (x) { return Object.assign({}, x, { printer_id: ((o.printers || [])[0] || {}).id || 0,
          plastic_id: def(o.plastics), quality_id: def(o.qualities), strength_id: def(o.strengths) }); });
      });
      API.adminDiscounts().then(function (j) { if (j && j.ok) setCustomers(j.customers || []); });
    }, []);
    function patch(p) { setF(function (x) { return Object.assign({}, x, p); }); }
    function patchKey(key, v) { setF(function (x) { var n = Object.assign({}, x); n[key] = v; return n; }); }
    var ready = Number(f.user_id) > 0 && Number(f.grams) > 0 && Number(f.minutes) > 0 && f.plastic_id;
    useEffect(function () {              // живой предпросмотр суммы (dry), когда чисел достаточно
      if (!ready) { setCalc(null); return; }
      var stale = false;
      API.adminOrderCreate({ user_id: f.user_id, printer_id: f.printer_id, plastic_id: f.plastic_id,
        quality_id: f.quality_id, strength_id: f.strength_id, qty: f.qty, grams: f.grams,
        minutes: f.minutes, price: f.price }, true)
        .then(function (j) { if (!stale) setCalc(j && j.ok ? j : null); })
        .catch(function () { if (!stale) setCalc(null); });
      return function () { stale = true; };
    }, [f.user_id, f.printer_id, f.plastic_id, f.quality_id, f.strength_id, f.qty, f.grams, f.minutes, f.price]);
    function submit() {
      if (!ready) { app.toast('Заполните клиента, пластик, граммы и время'); return; }
      setBusy(true);
      API.adminOrderCreate({ user_id: f.user_id, printer_id: f.printer_id, plastic_id: f.plastic_id,
        quality_id: f.quality_id, strength_id: f.strength_id, qty: f.qty, grams: f.grams,
        minutes: f.minutes, price: f.price, label: f.label, comment: f.comment }, false).then(function (j) {
        setBusy(false);
        if (j.ok) { app.toast('Заказ создан и отправлен клиенту на подтверждение'); onDone(); }
        else app.toast(j.error || 'Ошибка');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function num(label, key, ph) {
      return h('label', { style: { display: 'block' } },
        h('span', { style: { fontSize: 12, color: 'var(--muted)' } }, label),
        h('input', { className: 'input mono', style: { marginTop: 4 }, inputMode: 'decimal', placeholder: ph || '',
          value: f[key], onChange: function (e) { patchKey(key, e.target.value); } }));
    }
    function sel(label, key, items, fmt) {
      return h('label', { style: { display: 'block' } },
        h('span', { style: { fontSize: 12, color: 'var(--muted)' } }, label),
        h('select', { className: 'input', style: { marginTop: 4 }, value: f[key], onChange: function (e) { patchKey(key, Number(e.target.value)); } },
          (items || []).map(function (it) { return h('option', { key: it.id, value: it.id }, fmt(it)); })));
    }
    if (!opts) return h('div', { className: 'card', style: { marginTop: 12 } }, h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю параметры…'));
    return h('div', { className: 'card', style: { marginTop: 12 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 4 } },
        icon('plus', { size: 17, style: { color: 'var(--accent)' } }), 'Оформить заказ за клиента'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Клиент написал в личку мимо системы? Создайте заказ сами — бот отправит его клиенту на подтверждение. Слайсните модель и впишите итоговые граммы и время; цену посчитаю по обычной формуле (можно переопределить договорной).'),
      // клиент: выбор из известных + ручной ID
      customers.length ? h('label', { style: { display: 'block', marginBottom: 9 } },
        h('span', { style: { fontSize: 12, color: 'var(--muted)' } }, 'Клиент'),
        h('select', { className: 'input', style: { marginTop: 4 }, value: f.user_id, onChange: function (e) { patch({ user_id: e.target.value }); } },
          h('option', { value: '' }, '— выберите клиента —'),
          customers.map(function (c) { return h('option', { key: c.uid, value: String(c.uid) }, (c.name || ('id ' + c.uid)) + ' · заказов: ' + c.orders); }))) : null,
      h('label', { style: { display: 'block' } },
        h('span', { style: { fontSize: 12, color: 'var(--muted)' } }, 'или Telegram ID клиента'),
        h('input', { className: 'input mono', style: { marginTop: 4 }, inputMode: 'numeric', placeholder: 'напр. 333333333',
          value: f.user_id, onChange: function (e) { patch({ user_id: e.target.value.replace(/[^0-9]/g, '') }); } })),
      (opts.printers || []).length > 1 ? h('div', { style: { marginTop: 9 } }, sel('Принтер', 'printer_id', opts.printers, function (p) { return p.name; })) : null,
      h('div', { style: { marginTop: 9 } }, sel('Пластик', 'plastic_id', opts.plastics, function (p) { return p.name + ' · ' + p.price + ' ' + cur + '/кг'; })),
      // Качество и прочность не спрашиваем (8c8199d1): печатаем профилем мастера, сервер
      // сам подставляет пресеты по умолчанию.
      h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } }, num('Граммы (итого)', 'grams', 'напр. 42'), num('Время (мин, итого)', 'minutes', 'напр. 180')),
      h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } }, num('Количество', 'qty'), num('Цена (договорная), ' + cur, 'price', 'авто')),
      h('input', { className: 'input', style: { marginTop: 9 }, placeholder: 'Название заказа (необязательно)', value: f.label, onChange: function (e) { patch({ label: e.target.value }); } }),
      h('input', { className: 'input', style: { marginTop: 9 }, placeholder: 'Комментарий клиенту (необязательно)', value: f.comment, onChange: function (e) { patch({ comment: e.target.value }); } }),
      calc && h('div', { className: 'price', style: { marginTop: 12, fontSize: 24 } }, money(calc.price, cur)),
      calc && Number(f.price) > 0 && Math.abs(Number(calc.calc_price) - Number(calc.price)) > 0.005
        && h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 2 } }, 'расчётная: ' + money(calc.calc_price, cur)),
      // не блокируем кнопку по calc: предпросмотр — только подсказка, а валидацию с
      // понятным тостом делает submit(); disabled глотал клик без фидбэка (6dc2fb66)
      h('button', { className: 'btn', style: { marginTop: 12 }, onClick: submit, disabled: busy },
        icon('plus', { size: 17 }), busy ? ' Создаю…' : ' Создать и отправить клиенту'));
  }

  // Пакетный расчёт ссылок (OpenTodo 89251db6): мастер выбирает клиента, кидает пачку ссылок
  // одним текстом — бот считает каждую (дефолтные параметры, 1 шт), мастер просматривает и
  // отправляет клиенту расчёт каждого товара (модели заодно попадают клиенту в «Мои модели»).
  function BatchCalcForm(props) {
    var app = props.app, cur = props.cur;
    var cs = useState([]); var customers = cs[0], setCustomers = cs[1];
    var us = useState(''); var userId = us[0], setUserId = us[1];
    var ls = useState(''); var links = ls[0], setLinks = ls[1];
    var pls = useState(null); var plastics = pls[0], setPlastics = pls[1];   // выбор пластика (fcf355eb)
    var pidS = useState(0); var plasticId = pidS[0], setPlasticId = pidS[1];
    var bs = useState(null); var bid = bs[0], setBid = bs[1];
    var ds = useState(null); var data = ds[0], setData = ds[1];        // batch_status payload
    var es = useState({}); var excl = es[0], setExcl = es[1];          // {idx:true} — исключённые
    var bzS = useState(false); var busy = bzS[0], setBusy = bzS[1];
    var zS = useState(''); var zoom = zS[0], setZoom = zS[1];          // фуллскрин-просмотр превью
    var pollRef = useRef(null);
    useEffect(function () {
      API.adminDiscounts().then(function (j) { if (j && j.ok) setCustomers(j.customers || []); });
      API.adminPlastics().then(function (j) {
        if (j && j.ok) { setPlastics(j.plastics || []); var d = (j.plastics || []).filter(function (p) { return p.is_default; })[0] || (j.plastics || [])[0]; if (d) setPlasticId(d.id); }
      });
      // восстановление после выхода из приложения (fcf355eb): пакет живёт на сервере
      API.adminBatchLast().then(function (j) {
        if (j && j.ok && j.bid) {
          setBid(j.bid); setData(j);
          setUserId(String(j.user_id || ''));
          if (j.links_raw) setLinks(j.links_raw);
          if (j.plastic_id) setPlasticId(j.plastic_id);
          if (!j.done) poll(j.bid);
        }
      });
      return function () { clearInterval(pollRef.current); };
    }, []);
    function poll(b) {
      clearInterval(pollRef.current);
      function tick() {
        API.adminBatchStatus(b).then(function (j) {
          if (!j.ok) { clearInterval(pollRef.current); app.toast(j.error || 'Пакет не найден'); return; }
          setData(j);
          if (j.done) clearInterval(pollRef.current);
        });
      }
      tick();
      pollRef.current = setInterval(tick, 2500);
    }
    function start() {
      if (!Number(userId)) { app.toast('Выберите клиента'); return; }
      if (!/https?:\/\//.test(links)) { app.toast('Вставьте хотя бы одну ссылку'); return; }
      setBusy(true);
      var nm = (customers.filter(function (c) { return String(c.uid) === String(userId); })[0] || {}).name || '';
      API.adminBatchCalc(userId, links, nm, plasticId).then(function (j) {
        setBusy(false);
        if (!j.ok) { app.toast(j.error || 'Не удалось запустить'); return; }
        setBid(j.bid); setData(null); setExcl({});
        app.toast('Считаю ' + j.count + ' ссылок — это займёт время');
        poll(j.bid);
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function cancel() {
      if (!bid) return;
      API.adminBatchCancel(bid).then(function (j) {
        if (j.ok) { app.toast('Отменяю — текущая ссылка дорабатывает'); }
        else app.toast(j.error || 'Не удалось');
      });
    }
    function send() {
      var inc = (data.items || []).filter(function (it) { return it.status === 'ok' && !excl[it.idx]; })
        .map(function (it) { return it.idx; });
      if (!inc.length) { app.toast('Нечего отправлять'); return; }
      setBusy(true);
      API.adminBatchSend(bid, inc).then(function (j) {
        setBusy(false);
        if (j.ok) { app.toast('Отправлено клиенту: ' + j.sent + ' поз.'); setBid(null); setData(null); setLinks(''); }
        else app.toast(j.error || 'Не удалось отправить');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    var stIcon = { wait: '⏳', run: '⚙️', ok: '✅', err: '⚠️' };
    var okCount = data ? data.items.filter(function (it) { return it.status === 'ok' && !excl[it.idx]; }).length : 0;
    return h('div', { className: 'card', style: { marginTop: 12 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 4 } },
        icon('link', { size: 17, style: { color: 'var(--accent)' } }), 'Пакетный расчёт ссылок'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Клиент прислал в личку кучу ссылок? Выберите клиента, вставьте всё сообщение целиком — посчитаю каждую ссылку (параметры по умолчанию, 1 шт). Потом просмотрите результат и отправьте клиенту расчёт каждого товара.'),
      customers.length ? h('select', { className: 'input', value: userId, onChange: function (e) { setUserId(e.target.value); } },
        h('option', { value: '' }, '— выберите клиента —'),
        customers.map(function (c) { return h('option', { key: c.uid, value: String(c.uid) }, (c.name || ('id ' + c.uid)) + ' · заказов: ' + c.orders); })) : null,
      h('input', { className: 'input mono', style: { marginTop: 9 }, inputMode: 'numeric', placeholder: 'или Telegram ID клиента',
        value: userId, onChange: function (e) { setUserId(e.target.value.replace(/[^0-9]/g, '')); } }),
      plastics && plastics.length ? h('select', { className: 'input', style: { marginTop: 9 }, value: plasticId,
        onChange: function (e) { setPlasticId(Number(e.target.value)); } },
        plastics.map(function (p) { return h('option', { key: p.id, value: p.id }, p.name + ' · ' + p.price + ' ' + cur + '/кг'); })) : null,
      h('textarea', { className: 'input', style: { marginTop: 9, minHeight: 92, resize: 'vertical' },
        placeholder: 'Вставьте сообщение со ссылками — найду их сам', value: links,
        onChange: function (e) { setLinks(e.target.value); } }),
      h('div', { className: 'grid2', style: { gap: 9, marginTop: 10 } },
        h('button', { className: 'btn', style: { marginTop: 0 }, disabled: busy || !!(bid && data && !data.done), onClick: start },
          icon('coin', { size: 17 }), busy ? ' Запускаю…' : ' Посчитать все ссылки'),
        bid && data && !data.done ? h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: cancel },
          icon('close', { size: 16 }), ' Отменить') : null),
      bid && !data && h('p', { className: 'hint', style: { marginTop: 9 } }, 'Запустил, жду первые результаты…'),
      data && h('div', { style: { marginTop: 11 } },
        data.items.map(function (it) {
          var off = !!excl[it.idx];
          return h('div', { key: it.idx, style: { display: 'flex', alignItems: 'center', gap: 9, padding: '7px 0', borderTop: it.idx ? '1px solid var(--line)' : 'none', opacity: off ? .45 : 1 } },
            it.status === 'ok' && it.preview ? h('img', { src: it.preview, alt: '',
              style: { width: 40, height: 40, objectFit: 'cover', borderRadius: 8, flex: '0 0 auto', cursor: 'zoom-in' },
              // тап → на весь экран (fcf355eb); слой на PS_Back — свайп/BackButton закрывают
              // превью, а не всю мини-апп (OpenTodo 85d16a2a)
              onClick: function (e) { e.stopPropagation(); setZoom(it.preview); if (window.PS_Back) window.PS_Back.push(function () { setZoom(''); }); } })
              : h('span', { style: { width: 40, textAlign: 'center', flex: '0 0 auto', fontSize: 18 } }, stIcon[it.status] || '•'),
            h('div', { style: { minWidth: 0, flex: '1 1 auto' } },
              h('div', { style: { fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
                it.label || it.url.replace(/^https?:\/\//, '').slice(0, 48)),
              // время — через API.human (как везде на этом экране): инлайн-формат округлял часы
              // ВВЕРХ, а минуты брал остатком, и 110 мин показывались как «2 ч 50 м»
              it.status === 'ok' && h('div', { style: { fontSize: 12, color: 'var(--muted)' } }, it.grams + ' г · ' + API.human(it.minutes) + ' · ' + (it.plastic || '')),
              it.status === 'err' && h('div', { style: { fontSize: 12, color: 'var(--amber)' } }, it.error || 'не посчиталась'),
              (it.status === 'wait' || it.status === 'run') && h('div', { style: { fontSize: 12, color: 'var(--muted)' } }, it.status === 'run' ? 'считаю…' : 'в очереди')),
            it.status === 'ok' && h('div', { style: { flex: '0 0 auto', textAlign: 'right' } },
              h('div', { className: 'mono', style: { fontWeight: 700, color: 'var(--price)' } }, money(it.price, data.currency || cur)),
              h('label', { style: { fontSize: 11, color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer', justifyContent: 'flex-end' } },
                h('input', { type: 'checkbox', checked: !off, onChange: function () { setExcl(function (x) { var n = Object.assign({}, x); n[it.idx] = !n[it.idx]; return n; }); } }), 'слать')));
        }),
        !data.done && h('p', { className: 'hint', style: { marginTop: 8 } }, 'Ещё считаю — список обновляется сам…'),
        data.done && h('button', { className: 'btn', style: { marginTop: 11 }, disabled: busy || !okCount, onClick: send },
          icon('chat', { size: 17 }), busy ? ' Отправляю…' : (' Отправить клиенту (' + okCount + ')'))),
      // фуллскрин-просмотр превью (fcf355eb): тап по картинке → на весь экран, тап — закрыть
      // (через PS_Back.back(), чтобы свайп/аппаратная кнопка «назад» тоже просто закрывали
      // превью, а не мини-апп целиком — OpenTodo 85d16a2a)
      zoom && h('div', { onClick: function () { if (window.PS_Back) window.PS_Back.back(); else setZoom(''); },
        style: { position: 'fixed', inset: 0, zIndex: 90, background: 'rgba(4,6,9,.92)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'zoom-out' } },
        h('img', { src: zoom, alt: '', style: { maxWidth: '94vw', maxHeight: '92vh', objectFit: 'contain', borderRadius: 14 } })));
  }

  // Фильтры «Заказов» — тот же набор, в том же порядке и с той же логикой, что в настольной
  // админке (desk/src/screens/orders.jsx, FLT): владелец ходит по обеим и ищет заказ одинаково.
  // Раньше на телефоне жили только три чипа («Новые», «Сообщения», «Ждёт цены»), а «В работе»,
  // «Готовые» и «Отложенные» были доступны лишь глазами — фидбэк владельца 03.09.2026.
  var ORD_FLT = [
    ['', 'Все', function () { return true; }],
    ['new', '🆕 Новые', function (o) { return o.status === 'new'; }],
    ['msg', '💬 Сообщения', function (o) { return (o.unread || 0) > 0; }],
    ['price', '💱 Ждёт цены', function (o) { return o.status === 'price_changed' || o.pending != null; }],
    ['work', '🔥 В работе', function (o) { return o.status === 'in_progress' || o.status === 'printing'; }],
    ['done', '✅ Готовые', function (o) { return ['printed', 'done', 'handed'].indexOf(o.status) >= 0; }],
    ['deferred', '⏸ Отложенные', function (o) { return o.status === 'deferred'; }],
  ];
  // «Для себя» — не фильтр по загруженному списку, а другой источник: самопечать мастера
  // исключена из /api/admin/orders и живёт в /api/admin/self_orders. Как на ПК — чип у правого
  // края, переключающий список целиком. До 03.09.2026 самопечать на телефоне лежала ОТДЕЛЬНОЙ
  // страницей (вкладка «Для себя» в «Печати»), и «печать на заказ» с «печатью для себя»
  // оказывались в разных местах — на ПК это одна страница с фильтром (фидбэк владельца).
  var ORD_SELF = 'self';

  function AdminOrders(props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    var cre = useState(false); var creating = cre[0], setCreating = cre[1];
    var bat = useState(false); var batching = bat[0], setBatching = bat[1];
    var sq = useState(''); var q = sq[0], setQ = sq[1];   // сквозной поиск (crm-roadmap этап 2)
    // Фильтр-чипы (аудит, проблема #2): стартовое значение приходит из чипов «Требует
    // внимания» на хабе (app.openAdminOrders), дальше мастер щёлкает чипы прямо здесь.
    var sf = useState(app.adminOrdersFilter || ''); var flt = sf[0], setFlt = sf[1];
    // Самопечать — второй список, не фильтр. null = ещё ни разу не спрашивали; после первого
    // захода держим загруженной, как на ПК: мастер ходит туда-сюда, лишний стук в бота ни к чему.
    var ss = useState(null); var selfData = ss[0], setSelfData = ss[1];
    var isSelf = flt === ORD_SELF;
    var canSelf = !!(app.can && app.can('settings'));   // /api/admin/self_orders — владельческая ручка
    var zs = useState(''); var zoom = zs[0], setZoom = zs[1];   // рендер на весь экран (1a519e1c)
    function load() { API.adminOrders(q).then(function (j) { if (j.ok) setData(j); }); }
    function loadSelf() { API.adminSelfOrders(q).then(function (j) { if (j && j.ok) setSelfData(j); }); }
    useEffect(function () {                                // дебаунс при наборе, мгновенно на входе
      var t = setTimeout(load, q ? 320 : 0);
      return function () { clearTimeout(t); };
    }, [q]);
    useEffect(function () {
      if (!isSelf || !canSelf) return undefined;
      var t = setTimeout(loadSelf, q ? 320 : 0);
      return function () { clearTimeout(t); };
    }, [isSelf, q]);
    var allOrders = (data && data.orders) || [];
    function fltOf(key) {
      for (var i = 0; i < ORD_FLT.length; i++) if (ORD_FLT[i][0] === key) return ORD_FLT[i][2];
      return null;
    }
    var selfOrders = (selfData && selfData.orders) || [];
    var orders = isSelf ? selfOrders : allOrders.filter(fltOf(flt) || ORD_FLT[0][2]);
    var cur = ((isSelf ? selfData : data) || {}).currency || 'руб.';
    var loading = isSelf ? (selfData === null) : !data;
    function fltChip(key, label, n) {
      return h('button', { key: key || 'all', className: 'chip' + (flt === key ? ' on' : ''), type: 'button',
        onClick: function () { setFlt(key); } }, label + (n ? ' · ' + n : ''));
    }
    // Счётчики чипов ВСЕГДА по клиентскому списку (как на ПК): стоя в «Для себя», мастер иначе
    // видел бы «Новые 3» про самопечать и, нажав, попал бы в другой список с другой тройкой.
    var counts = {};
    ORD_FLT.forEach(function (f) { if (f[0]) counts[f[0]] = allOrders.filter(f[2]).length; });
    function cleanTest() {
      if (!window.confirm('Удалить все тестовые заказы (без цены и с 0 г / 0 мин)?\n\nДействие необратимо.')) return;
      API.adminOrdersCleanTest().then(function (j) {
        if (j.ok) { app.toast(j.deleted > 0 ? ('Удалено: ' + j.deleted) : 'Тестовых заказов нет'); load(); }
        else app.toast(j.error || 'Ошибка');
      });
    }
    function cleanBtn() { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: cleanTest }, icon('trash', { size: 14 }), ' Тестовые'); }
    function markSeen() {
      if (!window.confirm('Пометить все непросмотренные заказы как «просмотренные»?\n\nСбросит счётчик непросмотренных на дашборде.')) return;
      API.adminOrdersMarkAllSeen().then(function (j) {
        if (j.ok) { app.toast(j.marked > 0 ? ('Просмотрено: ' + j.marked) : 'Непросмотренных не было'); load(); }
        else app.toast(j.error || 'Ошибка');
      });
    }
    function seenBtn() { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: markSeen }, icon('check', { size: 14 }), ' Всё просмотрено'); }
    function createBtn() { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5, color: creating ? 'var(--accent)' : '' }, onClick: function () { setCreating(!creating); } }, icon('plus', { size: 14 }), ' За клиента'); }
    function batchBtn() { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5, color: batching ? 'var(--accent)' : '' }, onClick: function () { setBatching(!batching); } }, icon('link', { size: 14 }), ' Пакет ссылок'); }
    return h('div', { className: 'screen' },
      h(Lab, { right: h('span', { style: { display: 'inline-flex', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' } }, createBtn(), batchBtn(), seenBtn(), cleanBtn(), csvBtn(), refreshBtn(load)) }, 'Заказы'),
      creating && h(CreateOrderForm, { app: app, cur: (data && data.currency) || 'руб.', onDone: function () { setCreating(false); load(); } }),
      batching && h(BatchCalcForm, { app: app, cur: (data && data.currency) || 'руб.' }),
      h('input', { className: 'input', style: { marginBottom: 10 }, placeholder: '🔎 Поиск: #id, клиент, модель',
        value: q, onChange: function (e) { setQ(e.target.value); } }),
      // Чип без единого заказа только мешает — кроме «Все», который держит сброс (как на ПК).
      // «Для себя» показываем всегда, даже пустым: это вход в другой список, спрятать его —
      // спрятать сам раздел; закрыт он только правом «Настройки», под которым лежит ручка.
      h('div', { className: 'chips', style: { marginBottom: 10 } },
        ORD_FLT.filter(function (f) { return !f[0] || counts[f[0]]; })
          .map(function (f) { return fltChip(f[0], f[1], counts[f[0]]); }),
        canSelf && h('button', { key: 'self', className: 'chip' + (isSelf ? ' on' : ''), type: 'button',
          style: { marginLeft: 'auto' }, onClick: function () { setFlt(ORD_SELF); } },
          '🏠 Для себя' + (selfData ? ' · ' + selfOrders.length : ''))),
      loading && h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…'),
      !loading && orders.length === 0 && isSelf && h('p', { className: 'hint', style: { marginTop: 0 } },
        q ? 'Среди самопечатей ничего не нашлось.' : 'Пока пусто — завершённые печати «для себя» появятся здесь.'),
      !loading && orders.length === 0 && !isSelf && (q || flt) && h('p', { className: 'hint', style: { marginTop: 0 } }, 'Ничего не нашлось' + (flt ? ' по фильтру.' : '.')),
      orders.map(function (o) {
        return h('div', { key: o.id, className: 'order', style: { cursor: 'pointer' }, onClick: function () { app.openAdminOrder(o.id); } },
          h('div', { style: { display: 'flex', gap: 10, alignItems: 'flex-start' } },
            // миниатюра модели: мастер узнаёт заказ по картинке, не открывая его (аудит UX);
            // тап по миниатюре — рендер на весь экран, не открывая заказ (1a519e1c)
            o.preview
              ? h('img', { src: o.preview, alt: '', loading: 'lazy',
                  style: { width: 40, height: 40, borderRadius: 9, objectFit: 'cover', flex: '0 0 auto', background: 'var(--bg-2)', cursor: 'zoom-in' },
                  onClick: function (e) { e.stopPropagation(); setZoom(o.preview); } })
              : h('div', { style: { width: 40, height: 40, borderRadius: 9, flex: '0 0 auto', background: 'var(--surface-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' } }, icon('box', { size: 17 })),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', { className: 'spread' },
                h('div', null, h('span', { className: 'id' }, '#', o.id), ' ', h('b', { style: { fontSize: 14.5 } }, o.label)),
                o.unread > 0 ? h('span', { style: { color: 'var(--accent)', fontWeight: 700, fontSize: 12 } }, '💬 ', o.unread) : icon('chev', { size: 16, style: { color: 'var(--faint)' } })),
              h('div', { style: { marginTop: 7, display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' } },
                h('span', { className: 'stbadge ' + (ST_CLASS[o.status] || 'st-new'), style: { marginTop: 0 } }, o.status_label),
                h('span', { style: { fontSize: 12.5, color: 'var(--muted)' } },
                  // у самопечати граммы и время — главная информация (на ПК они колонками)
                  [o.user, o.qty ? (o.qty + ' шт') : '', o.grams ? (Math.round(o.grams * 10) / 10 + ' г') : '',
                    o.minutes ? fmtMin(o.minutes) : ''].filter(Boolean).join(' · ')),
                h('span', { className: 'mono', style: { marginLeft: 'auto', fontWeight: 600, color: 'var(--price)' } }, money(o.pending != null ? o.pending : o.price, cur))))));
      }),
      h(window.PS_Lightbox, { src: zoom, onClose: function () { setZoom(''); } }));
  }

  // ───────────────── ADMIN · ORDER DETAIL ─────────────────
  // Счёт-расчёт по индивидуальному заказу (OpenTodo 6aaa6a19): мастер слайснул найденную модель,
  // вводит итоговые граммы/время/пластик → полный расчёт + плата за разработку + скрин из слайсера.
  function InvoiceForm(props) {
    var app = props.app, o = props.order, cur = props.cur, onDone = props.onDone;
    var pls = useState(null); var plastics = pls[0], setPlastics = pls[1];
    var fs = useState({ plastic_id: 0, qty: String(o.qty || 1), grams: '', minutes: '', dev_fee: '0', comment: '' });
    var f = fs[0], setF = fs[1];
    // Многоцветная печать (OpenTodo efca76c3): граммы вводятся ПО ЦВЕТАМ (каждый своим пластиком
    // и своей ценой), время общее. Продувка/башня уже в граммах слайсера — ничего не добавляем.
    var mcS = useState(false); var mc = mcS[0], setMc = mcS[1];
    var rowsS = useState([{ plastic_id: 0, grams: '' }, { plastic_id: 0, grams: '' }]);
    var rows = rowsS[0], setRows = rowsS[1];
    var shS = useState(''); var shot = shS[0], setShot = shS[1];                 // новый скрин (dataURL)
    var spS = useState(o.slicer_shot || ''); var shotPrev = spS[0], setShotPrev = spS[1];
    var cS = useState(null); var calc = cS[0], setCalc = cS[1];                   // {price, print_price, dev_fee}
    var bS = useState(false); var busy = bS[0], setBusy = bS[1];
    // Мастер «закидывает» модель по ссылке (OpenTodo e78bf8f2) — сам скачаю и слайсну, подставлю граммы/время
    var lkS = useState(''); var link = lkS[0], setLink = lkS[1];
    var slS = useState(false); var slicing = slS[0], setSlicing = slS[1];
    useEffect(function () {
      API.adminPlastics().then(function (j) {
        if (j.ok) {
          setPlastics(j.plastics);
          if (j.plastics[0]) {
            var pid = j.plastics[0].id;
            setF(function (x) { return Object.assign({}, x, { plastic_id: pid }); });
            setRows(function (rs) { return rs.map(function (r) { return { plastic_id: r.plastic_id || pid, grams: r.grams }; }); });
          }
        }
      });
    }, []);
    function patch(p) { setF(function (x) { return Object.assign({}, x, p); }); }
    function patchKey(key, v) { setF(function (x) { var n = Object.assign({}, x); n[key] = v; return n; }); }
    function patchRow(i, p) { setRows(function (rs) { return rs.map(function (r, k) { return k === i ? Object.assign({}, r, p) : r; }); }); }
    function addRow() { setRows(function (rs) { return rs.concat([{ plastic_id: (plastics && plastics[0] && plastics[0].id) || 0, grams: '' }]); }); }
    function delRow(i) { setRows(function (rs) { return rs.length > 2 ? rs.filter(function (_, k) { return k !== i; }) : rs; }); }
    // colors для API — только валидные строки; null если мультицвет не готов к расчёту
    function colorsBody() {
      var v = rows.filter(function (r) { return r.plastic_id && Number(r.grams) > 0; });
      return (v.length >= 2 && v.length === rows.length)
        ? v.map(function (r) { return { plastic_id: r.plastic_id, grams: r.grams }; }) : null;
    }
    var rowsKey = rows.map(function (r) { return r.plastic_id + ':' + r.grams; }).join('|');
    useEffect(function () {            // живой предпросмотр суммы (dry), когда чисел достаточно
      var ready = mc ? !!colorsBody() : (Number(f.grams) > 0 && f.plastic_id);
      if (!(ready && Number(f.minutes) > 0)) { setCalc(null); return; }
      var stale = false;
      var body = mc
        ? { colors: colorsBody(), qty: f.qty, minutes: f.minutes, dev_fee: f.dev_fee }
        : { plastic_id: f.plastic_id, qty: f.qty, grams: f.grams, minutes: f.minutes, dev_fee: f.dev_fee };
      API.adminOrderInvoice(o.id, body, true)
        .then(function (j) { if (!stale && j.ok) setCalc(j); });
      return function () { stale = true; };
    }, [f.plastic_id, f.qty, f.grams, f.minutes, f.dev_fee, mc, rowsKey]);
    function sliceByLink() {
      var url = (link || '').trim();
      if (!url) { app.toast('Вставьте ссылку на модель'); return; }
      setSlicing(true);
      API.adminOrderSlice(o.id, url, { plastic_id: f.plastic_id, qty: f.qty }).then(function (j) {
        setSlicing(false);
        if (j.ok) {
          patch({ grams: String(j.grams), minutes: String(j.minutes) });
          app.toast('Посчитал: ' + j.grams + ' г · ' + j.minutes + ' мин' + (j.dims ? ' · ' + j.dims : ''));
        } else app.toast(j.error || 'Не удалось посчитать');
      }).catch(function () { setSlicing(false); app.toast('Сеть недоступна'); });
    }
    function pickShot(file) {
      if (!file) return;
      if (file.size > 12 * 1024 * 1024) { app.toast('Скрин больше 12 МБ — выберите поменьше'); return; }
      var rd = new FileReader(); rd.onload = function () { setShot(rd.result); setShotPrev(rd.result); }; rd.readAsDataURL(file);
    }
    function submit() {
      if (!(Number(f.minutes) > 0)) { app.toast('Введите время печати'); return; }
      var body;
      if (mc) {
        var cols = colorsBody();
        if (!cols) { app.toast('Заполните пластик и граммы в каждом цвете (минимум два)'); return; }
        body = { colors: cols, qty: f.qty, minutes: f.minutes, dev_fee: f.dev_fee, comment: f.comment };
      } else {
        if (!(Number(f.grams) > 0)) { app.toast('Введите граммы'); return; }
        if (!f.plastic_id) { app.toast('Выберите пластик'); return; }
        body = { plastic_id: f.plastic_id, qty: f.qty, grams: f.grams, minutes: f.minutes, dev_fee: f.dev_fee, comment: f.comment };
      }
      setBusy(true);
      if (shot) body.shot = shot;
      API.adminOrderInvoice(o.id, body, false).then(function (j) {
        setBusy(false);
        if (j.ok) { app.toast('Счёт отправлен клиенту'); onDone(); } else app.toast(j.error || 'Ошибка');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function num(label, key, ph) {
      return h('label', { style: { display: 'block' } },
        h('span', { style: { fontSize: 12, color: 'var(--muted)' } }, label),
        h('input', { className: 'input mono', style: { marginTop: 4 }, inputMode: 'decimal', placeholder: ph || '',
          value: f[key], onChange: function (e) { patchKey(key, e.target.value); } }));
    }
    return h('div', { className: 'card', style: { marginTop: 12 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 4 } },
        icon('receipt', { size: 17, style: { color: 'var(--accent)' } }), 'Выставить счёт (расчёт)'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Слайсните найденную модель, введите итоговые граммы и время — посчитаю по обычной формуле. Можно добавить плату за разработку модели (может быть 0) и приложить скрин из слайсера. Клиент увидит расчёт в приложении и подтвердит.'),
      // Режим: одноцвет (как раньше) / многоцвет — граммы по каждому цвету (OpenTodo efca76c3)
      h('div', { className: 'chips', style: { marginTop: 2 } },
        h('button', { className: 'chip' + (mc ? '' : ' on'), type: 'button', onClick: function () { setMc(false); } }, 'Один цвет'),
        h('button', { className: 'chip' + (mc ? ' on' : ''), type: 'button', onClick: function () { setMc(true); } }, icon('palette', { size: 15 }), ' Многоцвет')),
      plastics == null ? h('p', { className: 'hint', style: { marginTop: 9 } }, 'Загружаю пластики…')
        : (mc
          ? h('div', { style: { marginTop: 9 } },
              rows.map(function (r, i) {
                return h('div', { key: i, style: { display: 'flex', gap: 7, marginTop: i ? 7 : 0, alignItems: 'center' } },
                  h('select', { className: 'input', style: { flex: '1 1 auto', minWidth: 0 }, value: r.plastic_id,
                    onChange: function (e) { patchRow(i, { plastic_id: Number(e.target.value) }); } },
                    plastics.map(function (p) { return h('option', { key: p.id, value: p.id }, p.name + ' · ' + p.price + ' ' + cur + '/кг'); })),
                  h('input', { className: 'input mono', style: { flex: '0 0 86px' }, inputMode: 'decimal', placeholder: 'г',
                    value: r.grams, onChange: function (e) { patchRow(i, { grams: e.target.value }); } }),
                  rows.length > 2 && h('button', { className: 'btn line', type: 'button', 'aria-label': 'Убрать цвет',
                    style: { flex: '0 0 auto', width: 38, padding: 0 },
                    onClick: function () { delRow(i); } }, icon('close', { size: 15 })));
              }),
              h('button', { className: 'btn line', type: 'button', style: { marginTop: 8 }, onClick: addRow },
                icon('plus', { size: 15 }), ' Добавить цвет'),
              h('p', { className: 'hint', style: { marginTop: 7, marginBottom: 0 } },
                'Граммы по каждому цвету — из слайсера (продувка уже в них), время — общее.'))
          : h('select', { className: 'input', value: f.plastic_id, onChange: function (e) { patch({ plastic_id: Number(e.target.value) }); } },
              plastics.map(function (p) { return h('option', { key: p.id, value: p.id }, p.name + ' · ' + p.price + ' ' + cur + '/кг'); }))),
      !mc && h('div', { style: { marginTop: 9 } },
        h('div', { style: { display: 'flex', gap: 7 } },
          h('input', { className: 'input', style: { flex: '1 1 auto', minWidth: 0 }, placeholder: '🔗 Ссылка на модель (.stl/.3mf, Thingiverse…)',
            value: link, onChange: function (e) { setLink(e.target.value); } }),
          h('button', { className: 'btn line', type: 'button', style: { flex: '0 0 auto', width: 'auto', whiteSpace: 'nowrap', padding: '0 16px' }, onClick: sliceByLink, disabled: slicing },
            icon('box', { size: 15 }), slicing ? ' Слайсю…' : ' Посчитать')),
        h('p', { className: 'hint', style: { marginTop: 6, marginBottom: 0 } },
          'Закиньте ссылку — сам скачаю, слайсну выбранным выше пластиком и подставлю граммы и время. Потом можно поправить руками.')),
      h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } },
        !mc && num('Граммы (итого)', 'grams', 'напр. 42'),
        num('Время (мин, итого)', 'minutes', 'напр. 180')),
      h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } }, num('Количество', 'qty'), num('Разработка модели, ' + cur, 'dev_fee', '0')),
      h('input', { className: 'input', style: { marginTop: 9 }, placeholder: 'Комментарий к счёту (необязательно)', value: f.comment, onChange: function (e) { patch({ comment: e.target.value }); } }),
      shotPrev && h('img', { src: shotPrev, alt: '', style: { width: '100%', maxHeight: 200, objectFit: 'contain', borderRadius: 12, marginTop: 10, background: 'var(--bg-2)' } }),
      h('label', { className: 'btn line', style: { marginTop: 9, cursor: 'pointer' } },
        icon('image', { size: 17 }), ' ', shotPrev ? 'Заменить скрин из слайсера' : 'Приложить скрин из слайсера',
        h('input', { type: 'file', accept: 'image/*', style: { display: 'none' }, onChange: function (e) { if (e.target.files[0]) pickShot(e.target.files[0]); e.target.value = ''; } })),
      calc && h('div', { className: 'price', style: { marginTop: 12, fontSize: 24 } }, money(calc.price, cur)),
      calc && Number(calc.dev_fee) > 0 && h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 2 } }, 'печать ' + money(calc.print_price, cur) + ' + разработка ' + money(calc.dev_fee, cur)),
      h('button', { className: 'btn', style: { marginTop: 12 }, onClick: submit, disabled: busy || !calc },
        icon('receipt', { size: 17 }), busy ? ' Отправляю…' : ' Выставить счёт клиенту'));
  }

  // Печать и таймлапс заказа (OpenTodo c83a2a9e + 6b2eb433): мастер выбирает, какая из
  // последних печатей принтера относится к заказу — её таймлапс прикрепляется к заказу
  // (виден в карточке у клиента и админа) и по желанию сразу улетает клиенту.
  function AttachPrintBox(props) {
    var app = props.app, o = props.order, onDone = props.onDone;
    var hS = useState(null); var hist = hS[0], setHist = hS[1];       // null = не загружали
    var eS = useState(false); var open = eS[0], setOpen = eS[1];
    var sS = useState(''); var sel = sS[0], setSel = sS[1];           // имя выбранной печати
    var sendS = useState(true); var send = sendS[0], setSend = sendS[1];
    var bS = useState(false); var busy = bS[0], setBusy = bS[1];
    var heS = useState(''); var histErr = heS[0], setHistErr = heS[1];   // честная причина (edde64e4)
    function loadHist() {
      setOpen(true);
      if (hist !== null) return;
      API.adminPrinterHistory().then(function (j) {
        if (j.ok) { setHist(j.jobs || []); var f = (j.jobs || []).filter(function (x) { return x.has_timelapse; })[0]; if (f) setSel(f.name); }
        else { setHist([]); setHistErr(j.error || 'История недоступна'); }
      }).catch(function () { setHist([]); setHistErr('Сеть недоступна'); });
    }
    function attach() {
      if (!sel) { app.toast('Выберите печать'); return; }
      setBusy(true);
      API.adminAttachPrint(o.id, sel, send).then(function (j) {
        setBusy(false);
        if (j.ok) {
          // видео у заказа есть всегда, а вот саму печать (запись журнала со скрином стола)
          // сматчить по имени задания удаётся не всегда — честно предупреждаем (71523f3f)
          app.toast(j.journal_linked === false
            ? 'Видео привязано, но саму печать в журнале сопоставить не удалось — скрин стола к заказу не добавлен'
            : (send ? 'Таймлапс прикреплён и отправлен клиенту' : 'Таймлапс прикреплён'));
          setOpen(false); onDone();
        } else app.toast(j.error || 'Не удалось');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    return h(React.Fragment, null,
      h(Lab, null, 'Печать и таймлапс'),
      // все печати заказа из журнала (многостольный заказ печатается в несколько заходов):
      // клиент видит их агрегатом в карточке — тут мастер проверяет, что собралось
      (o.prints || []).length > 0 && h('div', { style: { marginBottom: 9 } },
        (o.prints || []).map(function (p, i) {
          return h('div', { key: p.jid, className: 'card', style: { marginTop: i ? 6 : 0, padding: 8 } },
            h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
              p.photo ? h('img', { src: p.photo, alt: '', style: { width: 44, height: 44, objectFit: 'cover', borderRadius: 8, flex: '0 0 auto' } }) : null,
              h('span', { style: { flex: 1, minWidth: 0, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
                (i + 1) + '. ' + (p.label || 'печать') + (p.minutes ? ' · ' + p.minutes + ' мин' : '')),
              p.timelapse ? h('a', { href: p.timelapse, target: '_blank', rel: 'noreferrer', style: { fontSize: 12.5, flex: '0 0 auto' } }, '▶ таймлапс') : null,
              // удаление медиа печати (1faab6cb): таймлапс помечается удалённым — свип не докачает
              p.timelapse ? h('button', { className: 'btn line', type: 'button', title: 'Удалить таймлапс',
                style: { marginTop: 0, width: 34, minHeight: 34, padding: 0, flex: '0 0 auto' },
                onClick: function () {
                  if (!window.confirm('Удалить таймлапс печати «' + (p.label || 'печать') + '»?\n\nФайл видео будет удалён, клиент перестанет его видеть.')) return;
                  API.adminJournalMediaDelete(p.jid, 'timelapse').then(function (j) {
                    if (j.ok) { app.toast('Таймлапс удалён'); onDone(); } else app.toast(j.error || 'Не удалось');
                  });
                } }, '🗑') : null,
              p.photo ? h('button', { className: 'btn line', type: 'button', title: 'Удалить скрин стола',
                style: { marginTop: 0, width: 34, minHeight: 34, padding: 0, flex: '0 0 auto' },
                onClick: function () {
                  if (!window.confirm('Удалить скрин стола печати «' + (p.label || 'печать') + '»?\n\nОн пропадёт из фоток результата и у клиента.')) return;
                  API.adminJournalMediaDelete(p.jid, 'photo').then(function (j) {
                    if (j.ok) { app.toast('Скрин стола удалён'); onDone(); } else app.toast(j.error || 'Не удалось');
                  });
                } }, '🗑') : null));
        }),
        h('p', { className: 'hint', style: { margin: '6px 0 0' } },
          'Печати этого заказа из журнала — клиент видит все их таймлапсы и скрины стола.')),
      o.timelapse && h('video', { src: o.timelapse, controls: true, playsInline: true,
        style: { width: '100%', maxHeight: 220, borderRadius: 12, background: '#000', marginBottom: 9 } }),
      o.timelapse && h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 9 } },
        h('p', { className: 'hint', style: { margin: 0, flex: 1 } }, 'Таймлапс прикреплён — клиент видит его в карточке заказа.'),
        h('button', { className: 'btn line', type: 'button', title: 'Удалить таймлапс заказа',
          style: { marginTop: 0, width: 34, minHeight: 34, padding: 0, flex: '0 0 auto' },
          onClick: function () {
            if (!window.confirm('Удалить таймлапс, привязанный к заказу #' + o.id + '?\n\nКлиент перестанет его видеть.')) return;
            API.adminOrderMediaDelete(o.id, 'timelapse').then(function (j) {
              if (j.ok) { app.toast('Таймлапс удалён'); onDone(); } else app.toast(j.error || 'Не удалось');
            });
          } }, '🗑')),
      !open && h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: loadHist },
        icon('printer', { size: 17 }), o.timelapse ? ' Заменить (выбрать печать из истории)' : ' Привязать печать из истории'),
      open && h('div', { className: 'card', style: { marginTop: 0, padding: 12 } },
        hist === null && h('p', { className: 'hint', style: { margin: 0 } }, 'Загружаю историю печатей…'),
        hist !== null && hist.length === 0 && h('p', { className: 'hint', style: { margin: 0 } },
          histErr ? ('Не удалось получить историю: ' + histErr) : 'История печатей на принтере пуста.'),
        (hist || []).map(function (jb, i) {
          var on = sel === jb.name, dis = !jb.has_timelapse;
          return h('button', { key: i, type: 'button', disabled: dis,
            className: 'chip' + (on ? ' on' : ''),
            style: { display: 'flex', width: '100%', justifyContent: 'space-between', marginTop: i ? 6 : 0, opacity: dis ? .45 : 1 },
            onClick: function () { setSel(jb.name); } },
            h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 } }, jb.name || '(без имени)'),
            h('span', { style: { flex: '0 0 auto', marginLeft: 8, fontSize: 11.5, color: 'var(--muted)' } },
              dis ? 'без таймлапса' : (jb.ago || '')));
        }),
        hist !== null && hist.length > 0 && h('label', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, fontSize: 13, color: 'var(--muted)', cursor: 'pointer' } },
          h('input', { type: 'checkbox', checked: send, onChange: function (e) { setSend(e.target.checked); } }),
          'Сразу отправить таймлапс клиенту'),
        h('div', { className: 'grid2', style: { gap: 9, marginTop: 10 } },
          h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { setOpen(false); } }, 'Отмена'),
          h('button', { className: 'btn', style: { marginTop: 0 }, disabled: busy || !sel, onClick: attach },
            busy ? 'Прикрепляю…' : 'Прикрепить'))),
      h('p', { className: 'hint', style: { marginTop: 6 } }, 'Выберите, какая из последних печатей принтера относится к этому заказу — её таймлапс прикрепится к заказу.'));
  }

  // Журнал печатей мастера (OpenTodo ed1518ed): ВСЕ печати (удачи и брак) — сырьё для анализа
  // пластика/брака (Big Data) и будущих алгоритмов.
  function AdminJournal(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var pls = useState(null); var plastics = pls[0], setPlastics = pls[1];
    // id != null — правим существующую запись (OpenTodo 60e0b2a8); брак — ползунок 0–100%
    var fs = useState({ id: null, label: '', plastic_id: 0, grams: '', minutes: '', fail_pct: 0, fail_reason: '', note: '', was_ungraded: false });
    var f = fs[0], setF = fs[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    var sps = useState([]); var spools = sps[0], setSpools = sps[1];   // катушки выбранного пластика (Фаза B)
    function load() { API.adminJournal().then(function (j) { if (j.ok) setData(j); else app.toast(j.error || 'Ошибка'); }); }
    useEffect(function () {
      load();
      API.adminPlastics().then(function (j) { if (j && j.ok) { setPlastics(j.plastics || []); var d = (j.plastics || [])[0]; if (d) setF(function (x) { return Object.assign({}, x, { plastic_id: x.plastic_id || d.id }); }); } });
    }, []);
    // катушки выбранного пластика — для точного списания печати с конкретной катушки (Фаза B)
    useEffect(function () {
      if (!f.plastic_id) { setSpools([]); return; }
      API.adminSpools(f.plastic_id).then(function (j) {
        if (j && j.ok) setSpools((j.spools || []).filter(function (s) { return s.status !== 'archived'; }));
      });
    }, [f.plastic_id]);
    function patch(p) { setF(function (x) { return Object.assign({}, x, p); }); }
    function resetForm() { patch({ id: null, label: '', grams: '', minutes: '', fail_pct: 0, fail_reason: '', note: '', request_id: '', photo: '', timelapse_url: '', was_ungraded: false }); }
    function startEdit(it) {
      setF({ id: it.id, label: it.label || '', plastic_id: it.plastic_id || 0,
        grams: it.grams || '', minutes: it.minutes || '',
        fail_pct: Math.round(it.fail_pct != null ? it.fail_pct : (it.result === 'fail' ? 100 : 0)),
        fail_reason: it.fail_reason || '', note: it.note || '',
        // оценка только что завершённой печати требует фото (OpenTodo 7bb76b35)
        was_ungraded: (it.graded === 0 || it.graded === false),
        request_id: it.request_id || '', photo: it.photo || '', timelapse_url: it.timelapse_url || '' });
      try { document.querySelector('.scr').scrollTo({ top: 0, behavior: 'smooth' }); } catch (e) {}
    }
    // фото результата печати → POST /api/admin/photo kind=journal (полный опрос, 0f848906)
    function uploadPhoto(file) {
      if (!file || !f.id) return;
      if (file.size > 12 * 1024 * 1024) { app.toast('Фото больше 12 МБ'); return; }
      var rd = new FileReader();
      rd.onload = function () {
        app.toast('Загружаю фото…');
        API.adminPhoto('journal', f.id, rd.result).then(function (j) {
          if (j.ok) { patch({ photo: j.url }); app.toast('Фото добавлено'); load(); }
          else app.toast(j.error || 'Не удалось');
        }).catch(function () { app.toast('Сеть недоступна'); });
      };
      rd.readAsDataURL(file);
    }
    function add() {
      if (!f.label.trim()) { app.toast('Напишите, что печатали'); return; }
      // фото обязательно при оценке только что завершённой печати (OpenTodo 7bb76b35)
      if (f.was_ungraded && !f.photo) { app.toast('📸 Добавьте фото результата — оно обязательно для оценки'); return; }
      setBusy(true);
      var call = f.id ? API.adminJournalUpdate(f.id, f) : API.adminJournalAdd(f);
      call.then(function (j) {
        setBusy(false);
        if (j.ok) { app.toast(f.id ? 'Сохранено' : 'Записано'); resetForm(); load(); }
        else app.toast(j.error || 'Ошибка');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    var st = (data && data.stats) || {};
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(load) }, 'Журнал печатей'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Каждая печать заносится сюда автоматически. Неоценённые (🕒) — нажмите, чтобы поставить результат. Это сырьё для анализа пластика и брака.'),
      data && st.ungraded > 0 && h('div', { className: 'card', style: { padding: '10px 13px', marginBottom: 10, borderLeft: '3px solid var(--accent)', display: 'flex', alignItems: 'center', gap: 8 } },
        h('span', { style: { fontSize: 16 } }, '🕒'),
        h('span', { style: { fontSize: 13, color: 'var(--text)' } }, st.ungraded + (st.ungraded === 1 ? ' печать ждёт оценки' : ' печатей ждут оценки') + ' — отметьте результат ниже')),
      data && st.total > 0 && h('div', { className: 'card', style: { padding: 13 } },
        h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10, textAlign: 'center' } },
          h('div', null, h('div', { style: { fontSize: 20, fontWeight: 700 } }, st.total), h('div', { style: { fontSize: 11, color: 'var(--faint)' } }, 'печатей')),
          h('div', null, h('div', { style: { fontSize: 20, fontWeight: 700, color: st.fail_pct > 15 ? 'var(--red)' : 'var(--text)' } }, st.fail_pct + '%'), h('div', { style: { fontSize: 11, color: 'var(--faint)' } }, 'брак · ' + (st.waste || 0) + ' г')),
          h('div', null, h('div', { style: { fontSize: 20, fontWeight: 700 } }, st.grams >= 1000 ? (st.grams / 1000).toFixed(1) + ' кг' : st.grams + ' г'), h('div', { style: { fontSize: 11, color: 'var(--faint)' } }, 'пластика'))),
        (st.by_plastic || []).length > 0 && h('div', { style: { marginTop: 11 } },
          (st.by_plastic || []).map(function (bp) {
            return h('div', { key: bp.plastic, className: 'spread', style: { fontSize: 12.5, padding: '4px 0' } },
              h('span', { style: { color: 'var(--muted)' } }, bp.plastic),
              h('span', { className: 'mono' }, bp.n + ' печ. · брак ' + bp.fail_pct + '% · ' + bp.grams + ' г'));
          }))),
      h(Lab, null, f.id ? ('Правка записи #' + f.id) : 'Записать печать'),
      h('div', { className: 'card' },
        h('input', { className: 'input', placeholder: 'Что печатали (модель/деталь)', value: f.label, onChange: function (e) { patch({ label: e.target.value }); } }),
        plastics && plastics.length ? h('select', { className: 'input', style: { marginTop: 9 }, value: f.plastic_id, onChange: function (e) { patch({ plastic_id: Number(e.target.value), spool_id: null }); } },
          plastics.map(function (p) { return h('option', { key: p.id, value: p.id }, p.name); })) : null,
        // Катушка печати (Фаза B мастер-плана): точное списание с конкретной катушки; «по пластику» =
        // прежнее поведение (общий склад/цвет).
        spools.length ? h('select', { className: 'input', style: { marginTop: 9 }, value: f.spool_id || 0, onChange: function (e) { patch({ spool_id: Number(e.target.value) || null }); } },
          h('option', { value: 0 }, 'Катушка — по пластику (без точного учёта)'),
          spools.map(function (s) { return h('option', { key: s.id, value: s.id }, (s.color || 'без цвета') + ' · ' + Math.round(s.remaining_g) + ' г' + (s.status === 'empty' ? ' · пустая' : '')); })) : null,
        h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } },
          h('input', { className: 'input mono', inputMode: 'decimal', placeholder: 'граммы', value: f.grams, onChange: function (e) { patch({ grams: e.target.value }); } }),
          h('input', { className: 'input mono', inputMode: 'numeric', placeholder: 'минуты', value: f.minutes, onChange: function (e) { patch({ minutes: e.target.value }); } })),
        // удача↔брак — ползунок 0–100% (60e0b2a8): 0 = всё ок, 30 = треть в брак, 100 = полный брак
        h('div', { style: { marginTop: 11 } },
          h('div', { className: 'spread', style: { fontSize: 12.5, marginBottom: 5 } },
            h('span', { style: { color: 'var(--muted)' } }, 'Результат'),
            h('b', { style: { color: f.fail_pct > 0 ? (f.fail_pct >= 50 ? 'var(--red)' : 'var(--amber)') : 'var(--green, #6dd17e)' } },
              f.fail_pct > 0 ? ('⚠️ брак ' + f.fail_pct + '%') : '✅ удача')),
          h('input', { type: 'range', min: 0, max: 100, step: 5, value: f.fail_pct,
            style: { width: '100%', accentColor: f.fail_pct >= 50 ? 'var(--red)' : (f.fail_pct > 0 ? 'var(--amber)' : 'var(--accent)') },
            onChange: function (e) { patch({ fail_pct: Number(e.target.value) }); } }),
          h('div', { className: 'spread', style: { fontSize: 10.5, color: 'var(--faint)' } },
            h('span', null, 'удача'), h('span', null, 'частичный брак'), h('span', null, 'всё в брак'))),
        f.fail_pct > 0 && h('input', { className: 'input', style: { marginTop: 9 }, placeholder: 'Причина брака (отлип, спагетти, засор…)', value: f.fail_reason, onChange: function (e) { patch({ fail_reason: e.target.value }); } }),
        h('input', { className: 'input', style: { marginTop: 9 }, placeholder: 'Заметка (необязательно)', value: f.note, onChange: function (e) { patch({ note: e.target.value }); } }),
        // полный опрос (0f848906): какой это был заказ + фото результата + таймлапс
        f.id ? h('div', { style: { marginTop: 9 } },
          h('div', { style: { fontSize: 11.5, color: 'var(--faint)', margin: '0 2px 5px', fontWeight: 600 } }, 'Заказ № (пусто = печать вне заказа / для себя — спишу её пластик со склада)'),
          h('input', { className: 'input mono', inputMode: 'numeric', placeholder: 'напр. 31', value: f.request_id || '', onChange: function (e) { patch({ request_id: e.target.value.replace(/[^0-9]/g, '') }); } })) : null,
        f.id && f.was_ungraded && !f.photo ? h('div', { style: { fontSize: 11.5, color: 'var(--accent)', margin: '9px 2px 0', fontWeight: 600 } }, '📸 Фото результата обязательно для оценки этой печати') : null,
        f.id ? h('div', { style: { display: 'flex', gap: 8, marginTop: 9, alignItems: 'center', flexWrap: 'wrap' } },
          f.photo ? h('img', { src: f.photo, alt: '', style: { width: 54, height: 54, objectFit: 'cover', borderRadius: 10 } }) : null,
          h('label', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 14px', cursor: 'pointer' } },
            icon('image', { size: 16 }), ' ', f.photo ? 'Заменить фото' : 'Фото результата',
            h('input', { type: 'file', accept: 'image/*', style: { display: 'none' }, onChange: function (e) { if (e.target.files[0]) uploadPhoto(e.target.files[0]); e.target.value = ''; } })),
          // удаление медиа печати (1faab6cb): фото — и из фоток результата заказа; таймлапс
          // помечается удалённым на сервере, чтобы свип не докачал его заново
          f.photo ? h('button', { className: 'btn line', type: 'button', title: 'Удалить фото',
            style: { marginTop: 0, width: 42, padding: 0 },
            onClick: function () {
              if (!window.confirm('Удалить скрин стола этой печати?\n\nОн пропадёт и из фоток результата привязанного заказа.')) return;
              API.adminJournalMediaDelete(f.id, 'photo').then(function (j) {
                if (j.ok) { patch({ photo: '' }); app.toast('Фото удалено'); load(); } else app.toast(j.error || 'Не удалось');
              });
            } }, '🗑') : null,
          f.timelapse_url ? h('button', { className: 'btn line', type: 'button', style: { marginTop: 0, width: 'auto', padding: '0 14px' }, onClick: function () { exportLink(f.timelapse_url); } }, '🎬 Таймлапс') : null,
          f.timelapse_url ? h('button', { className: 'btn line', type: 'button', title: 'Удалить таймлапс',
            style: { marginTop: 0, width: 42, padding: 0 },
            onClick: function () {
              if (!window.confirm('Удалить таймлапс этой печати?\n\nФайл видео будет удалён и заново с принтера не скачается.')) return;
              API.adminJournalMediaDelete(f.id, 'timelapse').then(function (j) {
                if (j.ok) { patch({ timelapse_url: '' }); app.toast('Таймлапс удалён'); load(); } else app.toast(j.error || 'Не удалось');
              });
            } }, '🗑') : null) : null,
        f.id
          ? h('div', { className: 'grid2', style: { gap: 9, marginTop: 10 } },
              h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: resetForm }, 'Отмена'),
              h('button', { className: 'btn', style: { marginTop: 0 }, disabled: busy, onClick: add }, busy ? 'Сохраняю…' : 'Сохранить'))
          : h('button', { className: 'btn', style: { marginTop: 10 }, disabled: busy, onClick: add }, icon('plus', { size: 17 }), busy ? ' Пишу…' : ' Записать')),
      h(Lab, null, 'Последние печати'),
      data === null && h('p', { className: 'hint' }, 'Загружаю…'),
      data && (data.items || []).length === 0 && h('p', { className: 'hint' }, 'Журнал пуст — запишите первую печать.'),
      ((data && data.items) || []).map(function (it) {
        var pct = Math.round(it.fail_pct != null ? it.fail_pct : (it.result === 'fail' ? 100 : 0));
        // graded=0 — печать занесена автоматически, оценки ещё нет (5ac0ae9f)
        var ungraded = it.graded === 0 || it.graded === false;
        // рамку убрали (аудит UX): «не оценено» и так сигналит текстом + 🕒 + баннером сверху
        return h('div', { key: it.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px', cursor: ungraded ? 'pointer' : 'default' },
          onClick: ungraded ? function () { startEdit(it); } : undefined },
          ungraded
            ? h('span', { style: { fontSize: 13, fontWeight: 700, flex: '0 0 auto', color: 'var(--muted)' } }, '🕒')
            : h('span', { style: { fontSize: pct > 0 ? 12 : 16, fontWeight: 700, flex: '0 0 auto', color: pct >= 50 ? 'var(--red)' : 'var(--amber)' } },
                pct > 0 ? '⚠️' + pct + '%' : '✅'),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, it.label,
              it.request_id ? h('span', { style: { color: 'var(--muted)', fontWeight: 500 } }, ' · #' + it.request_id) : null),
            ungraded
              ? h('div', { style: { fontSize: 11.5, color: 'var(--accent)', marginTop: 1, fontWeight: 600 } }, 'не оценено — нажмите, чтобы поставить результат')
              : h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 1 } },
                  (it.plastic_name || '—') + ' · ' + (it.grams || 0) + ' г · ' + (it.minutes || 0) + ' мин'
                  + (it.fail_reason ? ' · ' + it.fail_reason : ''))),
          it.timelapse ? h('span', { title: 'Есть таймлапс', style: { fontSize: 13, flex: '0 0 auto' } }, '🎬') : null,
          h('span', { style: { fontSize: 11, color: 'var(--faint)', flex: '0 0 auto' } }, it.ago),
          h('button', { className: 'btn line', style: { marginTop: 0, width: 40, minHeight: 40, padding: 0, flex: '0 0 auto' },
            'aria-label': 'Править', onClick: function (e) { e.stopPropagation(); startEdit(it); } },
            icon('pencil', { size: 14 })),
          h('button', { className: 'btn line', style: { marginTop: 0, width: 40, minHeight: 40, padding: 0, flex: '0 0 auto' },
            'aria-label': 'Удалить', onClick: function (e) { e.stopPropagation(); API.adminJournalDelete(it.id).then(function (j) { if (j.ok) load(); }); } },
            icon('trash', { size: 14 })));
      }));
  }

  // Очередь разбора (OpenTodo 26196ac1): клиентские ссылки, которые не загрузились/не посчитались.
  // Мастер разбирает руками (готовит модель или чинит код) и закрывает позицию — клиент получает
  // уведомление (с комментарием, если задан).
  function AdminCalcQueue(props) {
    var app = props.app;
    var ds = useState(null); var items = ds[0], setItems = ds[1];
    var ns = useState({}); var notes = ns[0], setNotes = ns[1];     // {id: текст клиенту}
    function load() { API.adminCalcQueue().then(function (j) { if (j.ok) setItems(j.items || []); else app.toast(j.error || 'Ошибка'); }); }
    useEffect(function () { load(); }, []);
    function resolve(it, action) {
      API.adminCalcQueueResolve(it.id, action, notes[it.id] || '').then(function (j) {
        if (j.ok) { app.toast(action === 'delete' ? 'Удалено' : 'Закрыто — клиент уведомлён'); load(); }
        else app.toast(j.error || 'Не удалось');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function copy(text) {
      try { navigator.clipboard.writeText(text).then(function () { app.toast('Ссылка скопирована'); }); }
      catch (e) { app.toast(text); }
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(load) }, 'Разбор ссылок'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Ссылки клиентов, которые бот не смог посчитать. Разберите руками (подготовьте модель или почините причину) и закройте позицию — клиент получит уведомление.'),
      items === null && h('p', { className: 'hint' }, 'Загружаю…'),
      items !== null && items.length === 0 && h('p', { className: 'hint' }, 'Пусто — все ссылки разобраны 🎉'),
      (items || []).map(function (it) {
        return h('div', { key: it.id, className: 'card', style: { padding: 13 } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' } },
            h('span', { className: 'id' }, '#', it.id),
            h('b', { style: { fontSize: 13.5 } }, it.user),
            h('span', { style: { fontSize: 11.5, color: 'var(--faint)', marginLeft: 'auto' } }, it.ago)),
          h('div', { className: 'mono', style: { fontSize: 12, marginTop: 7, wordBreak: 'break-all', color: 'var(--text)', cursor: 'pointer' },
            onClick: function () { copy(it.url); }, title: 'Нажмите, чтобы скопировать' },
            icon('doc', { size: 13, style: { flex: '0 0 auto', marginRight: 5, verticalAlign: '-2px' } }), it.url),
          it.error && h('div', { style: { fontSize: 12, color: 'var(--amber)', marginTop: 5 } }, it.error),
          h('input', { className: 'input', style: { marginTop: 9 }, placeholder: 'Комментарий клиенту (необязательно)',
            value: notes[it.id] || '', onChange: function (e) { var v = e.target.value; setNotes(function (n) { var x = Object.assign({}, n); x[it.id] = v; return x; }); } }),
          h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } },
            h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { resolve(it, 'delete'); } },
              icon('trash', { size: 15 }), ' Убрать'),
            h('button', { className: 'btn', style: { marginTop: 0 }, onClick: function () { resolve(it, ''); } },
              icon('check', { size: 16 }), ' Разобрано')));
      }));
  }

  // Смена пластика/цвета заказа (OpenTodo e132724b): раньше материал правился только через БД.
  // Пластики и палитры — из adminPlastics (color_css как в клиентском пикере); слотов цвета
  // столько, сколько цветов у заказа сейчас. Цену НЕ трогает — для этого «Изменить цену»/«Пересчёт».
  function MaterialBox(props) {
    var app = props.app, o = props.order, onDone = props.onDone;
    var os = useState(false); var open = os[0], setOpen = os[1];
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var ps = useState(0); var pid = ps[0], setPid = ps[1];
    var cs = useState([]); var cols = cs[0], setCols = cs[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    // текущие цвета заказа — хвост plastic_name после « · », по слотам через « - »
    var tail = String(o.plastic || '').split(' · ');
    var curCols = tail.length === 2 ? tail[1].split(' - ').map(function (s) { return s.trim(); }).filter(Boolean) : [];
    var nSlots = Math.max(1, curCols.length);
    function openEd() {
      setOpen(true);
      if (data) return;
      API.adminPlastics().then(function (j) {
        if (!j || !j.ok) { app.toast((j && j.error) || 'Не удалось загрузить пластики'); setOpen(false); return; }
        setData(j);
        var act = (j.plastics || []).filter(function (p) { return p.is_active; });
        var curP = act.filter(function (p) { return String(o.plastic || '').indexOf(p.name) === 0; })[0] || act[0];
        if (curP) {
          setPid(curP.id);
          setCols(curCols.length ? curCols.slice() : [((curP.colors || [])[0] || '')]);
        }
      }).catch(function () { app.toast('Сеть недоступна'); setOpen(false); });
    }
    function save() {
      if (busy || !pid) return; setBusy(true);
      API.adminOrderMaterial(o.id, pid, cols.slice(0, nSlots).filter(Boolean)).then(function (j) {
        app.toast(j && j.ok ? 'Материал заказа обновлён' : ((j && j.error) || 'Ошибка'));
        if (j && j.ok) { setOpen(false); onDone(); }
      }).catch(function () { app.toast('Сеть недоступна'); }).then(function () { setBusy(false); });
    }
    if (!open) {
      return h('button', { className: 'btn line', style: { marginTop: 12 }, onClick: openEd },
        icon('spool', { size: 17 }), ' Сменить пластик или цвет');
    }
    var act = ((data && data.plastics) || []).filter(function (p) { return p.is_active; });
    var cur = act.filter(function (p) { return p.id === pid; })[0] || null;
    var pal = (cur && cur.colors) || [];
    var css = (cur && cur.color_css) || {};
    return h('div', { className: 'card', style: { marginTop: 12 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 8 } },
        icon('spool', { size: 17, style: { color: 'var(--accent)' } }), 'Пластик и цвет'),
      !data && h('p', { className: 'hint' }, 'Загружаю пластики…'),
      data && h('div', { className: 'chips' }, act.map(function (p) {
        return h('button', { key: p.id, className: 'chip' + (p.id === pid ? ' on' : ''),
          onClick: function () {
            setPid(p.id);
            setCols(Array.from({ length: nSlots }, function (_, i) {
              return (p.colors || [])[i % Math.max(1, (p.colors || []).length)] || '';
            }));
          } }, p.name);
      })),
      data && cur && Array.from({ length: nSlots }, function (_, i) {
        return h('div', { key: i, style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, flexWrap: 'wrap' } },
          nSlots > 1 && h('span', { style: { fontSize: 12.5, color: 'var(--muted)', flex: '0 0 52px' } }, 'Цвет ' + (i + 1)),
          pal.map(function (c) {
            return h('button', { key: c, type: 'button', title: c,
              onClick: function () { var n = cols.slice(); n[i] = c; setCols(n); },
              style: { width: 22, height: 22, borderRadius: '50%', cursor: 'pointer', padding: 0,
                background: css[c] || '#8a97a5',
                border: cols[i] === c ? '2px solid var(--accent)' : '1px solid var(--line)' } });
          }),
          h('span', { style: { fontSize: 12, color: 'var(--faint)' } }, cols[i] || '—'));
      }),
      data && h('div', { className: 'grid2', style: { gap: 9, marginTop: 11 } },
        h('button', { className: 'btn line', style: { marginTop: 0 }, disabled: busy, onClick: function () { setOpen(false); } }, 'Отмена'),
        h('button', { className: 'btn', style: { marginTop: 0 }, disabled: busy || !cur, onClick: save }, busy ? 'Сохраняю…' : 'Сохранить')),
      h('p', { className: 'hint', style: { marginTop: 6 } }, 'Цена не пересчитывается — для этого есть «Изменить цену» и «Пересчёт заказа».'));
  }

  // Баланс клиента (OpenTodo 8c428c43): мастер зачисляет принесённые деньги, может списать за заказ.
  function BalanceBox(props) {
    var app = props.app, o = props.order, cur = props.cur, onDone = props.onDone;
    var as = useState(''); var amt = as[0], setAmt = as[1];
    var cs = useState(''); var com = cs[0], setCom = cs[1];   // заметка мастера (ccb1e4b6)
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    function adjust(amount, reason, requestId) {
      if (busy) return; setBusy(true);
      // requestId — для семейного счёта: история покажет, за чей заказ и какой номер
      API.adminBalance(o.user_id, amount, reason, com.trim(), requestId).then(function (j) {
        app.toast(j && j.ok ? ('Баланс: ' + money(j.balance, cur)) : ((j && j.error) || 'Ошибка'));
        if (j && j.ok) { setCom(''); onDone(); }
      }).catch(function () { app.toast('Сеть недоступна'); }).then(function () { setBusy(false); });
    }
    function credit() {
      var v = parseFloat(String(amt).replace(',', '.'));
      if (!v) { app.toast('Введите сумму'); return; }
      setAmt(''); adjust(v, 'Пополнение мастером');
    }
    return h('div', { className: 'card', style: { marginTop: 12 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 8 } },
        icon('coin', { size: 17, style: { color: 'var(--price)' } }), 'Баланс клиента'),
      h('div', { className: 'price', style: { fontSize: 22, color: (Number(o.balance) < 0 ? 'var(--red)' : 'var(--price)') } }, money(o.balance, cur)),
      h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
        h('input', { className: 'input mono', style: { flex: '0 0 130px', marginTop: 0 }, inputMode: 'decimal', placeholder: 'Сумма', value: amt, onChange: function (e) { setAmt(e.target.value); } }),
        h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 16px' }, disabled: busy, onClick: credit }, 'Зачислить')),
      h('input', { className: 'input', style: { marginTop: 8 }, placeholder: 'Комментарий к операции (клиент увидит)', value: com, onChange: function (e) { setCom(e.target.value); } }),
      o.price > 0 && h('button', { className: 'btn line', style: { marginTop: 9 }, disabled: busy, onClick: function () { adjust(-o.price, 'Оплата заказа #' + o.id, o.id); } },
        icon('receipt', { size: 16 }), ' Списать за заказ (' + money(o.price, cur) + ')'),
      h('p', { className: 'hint', style: { marginTop: 6 } }, 'Зачислите принесённые клиентом деньги — остаток сохранится на будущее. Клиент видит баланс и историю в профиле; вся история по клиенту — в его карточке (Клиенты).'));
  }

  // ───────── Доставка и отправление заказа (A2) ─────────
  // Живёт на ГРУППЕ (заказ-корзина), а не на позиции: адрес, стоимость и трек-номер одни на
  // весь заказ. Стоимость правит только владелец (деньги), трек-номер — вся команда: отправка
  // посылки это работа склада, а не кассы.
  function DeliveryBlock(props) {
    var app = props.app, g = props.g, cur = props.cur, canMoney = props.canMoney;
    var ds = useState(null); var dict = ds[0], setDict = ds[1];   // перевозчики + зоны
    var track = g.track || {};
    var cs = useState(track.carrier || ''); var carrier = cs[0], setCarrier = cs[1];
    var ns = useState(track.number || ''); var num = ns[0], setNum = ns[1];
    var us = useState(track.url || ''); var url = us[0], setUrl = us[1];
    var ps = useState(''); var cost = ps[0], setCost = ps[1];
    useEffect(function () { API.adminDelivery().then(function (j) { if (j && j.ok) setDict(j); }); }, []);
    function ship(action) {
      API.adminOrderShipment({ gid: g.gid, action: action, carrier: carrier, number: num, url: url })
        .then(function (j) {
          if (!j.ok) { app.toast(j.error || 'Ошибка'); return; }
          app.toast(action === 'ship' ? 'Клиенту ушло уведомление с трек-номером'
            : action === 'delivered' ? 'Отмечено доставленным'
            : action === 'clear' ? 'Отправление снято' : 'Сохранено');
          if (props.onDone) props.onDone();
        }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function saveCost() {
      var v = String(cost).replace(',', '.').trim();
      if (v === '') { app.toast('Введите стоимость'); return; }
      API.adminOrderDelivery({ gid: g.gid, cost: parseFloat(v) }).then(function (j) {
        if (j.ok) { app.toast('Стоимость доставки обновлена'); setCost(''); if (props.onDone) props.onDone(); }
        else app.toast(j.error || 'Ошибка');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    var carriers = (dict && dict.carriers) || [];
    var isPickup = g.delivery_method === 'pickup';
    return h(React.Fragment, null,
      h(Lab, null, isPickup ? 'Получение' : 'Доставка'),
      h('div', { className: 'card', style: { marginTop: 0, padding: 13 } },
        h('div', { style: { fontSize: 13.5 } },
          isPickup ? '🏬 Самовывоз' : '🚚 Доставка',
          g.delivery_type ? h('span', { style: { color: 'var(--muted)' } }, ' · ' + g.delivery_type) : null),
        g.delivery_address && h('div', { style: { fontSize: 13, color: 'var(--muted)', marginTop: 4 } }, g.delivery_address),
        canMoney && h('div', { className: 'mono', style: { marginTop: 7, fontSize: 13 } },
          'Позиции ', money(g.items_total, cur), ' + доставка ', money(g.delivery_cost, cur),
          ' = ', h('b', { style: { color: 'var(--price)' } }, money(g.total_with_delivery, cur)),
          g.delivery_paid ? h('span', { className: 'stbadge st-printed', style: { marginLeft: 8 } }, 'доставка оплачена') : null),
        // Стоимость доставки — деньги: правит только владелец и только пока она не оплачена.
        canMoney && !g.delivery_paid && h('div', { style: { display: 'flex', gap: 8, marginTop: 9 } },
          h('input', { className: 'input mono', style: { flex: '0 0 110px', marginTop: 0 }, inputMode: 'decimal',
            placeholder: String(g.delivery_cost || 0), value: cost, onChange: function (e) { setCost(e.target.value); } }),
          h('button', { className: 'btn soft', style: { width: 'auto', padding: '0 14px', marginTop: 0 }, onClick: saveCost },
            icon('coin', { size: 15 }), ' Изменить')),
        // Отправление: перевозчик + номер. Ссылка собирается по шаблону перевозчика сама.
        !isPickup && h('div', { style: { marginTop: 12 } },
          h('div', { style: { display: 'flex', gap: 8 } },
            h('select', { className: 'input', style: { flex: '1 1 120px', marginTop: 0 }, value: carrier,
              onChange: function (e) { setCarrier(e.target.value); } },
              [h('option', { key: '_', value: '' }, '— перевозчик —')].concat(carriers.map(function (c) {
                return h('option', { key: c.id, value: c.id }, c.name);
              }))),
            h('input', { className: 'input mono', style: { flex: '1 1 120px', marginTop: 0 }, placeholder: 'Трек-номер',
              value: num, onChange: function (e) { setNum(e.target.value); } })),
          h('input', { className: 'input', style: { marginTop: 8 }, placeholder: 'Ссылка отслеживания (если своя)',
            value: url, onChange: function (e) { setUrl(e.target.value); } }),
          track.url && h('p', { className: 'hint', style: { marginTop: 6 } },
            h('a', { href: track.url, target: '_blank', rel: 'noopener', style: { color: 'var(--accent)' } }, 'Проверить ссылку отслеживания')),
          h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } },
            h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { ship('save'); } }, 'Сохранить'),
            h('button', { className: 'btn', style: { marginTop: 0 }, onClick: function () { ship('ship'); } },
              icon('share', { size: 16 }), track.shipped_at ? ' Переотправить' : ' Отправлено')),
          h('p', { className: 'hint', style: { marginTop: 6 } },
            '«Отправлено» пошлёт клиенту уведомление с перевозчиком, номером и ссылкой отслеживания.'),
          track.shipped_at ? h('div', { className: 'grid2', style: { gap: 9, marginTop: 6 } },
            h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { ship('clear'); } }, 'Снять отправку'),
            h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { ship('delivered'); } },
              icon('check', { size: 16 }), track.delivered_at ? ' Доставлено ✓' : ' Доставлено')) : null)));
  }

  function AdminOrderDetail(props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    var ps = useState({ price: '', comment: '' }); var pf = ps[0], setPf = ps[1];
    var ms = useState(''); var msg = ms[0], setMsg = ms[1];
    var ds = useState(''); var due = ds[0], setDue = ds[1];   // срок исполнения «ДД.ММ» для «в работу»
    var acs = useState(''); var actual = acs[0], setActual = acs[1];   // факт. время печати, мин (Этап 0 сбора данных)
    var rjs = useState(''); var rejReason = rjs[0], setRejReason = rjs[1];   // причина отказа (crm этап 6)
    var rs = useState(null); var recalc = rs[0], setRecalc = rs[1];   // {old, price, grams, minutes} пересчёта
    // OpenTodo 8c1cab08: режим формы «Изменить цену» — 'price' (вписать цену) | 'gt' (по граммам/времени)
    var pms = useState('price'); var priceMode = pms[0], setPriceMode = pms[1];
    var gts = useState({ grams: '', hours: '', minutes: '' }); var gtf = gts[0], setGtf = gts[1];
    var rls = useState(false); var rLoad = rls[0], setRLoad = rls[1];
    var tcs = useState(false); var toCol = tcs[0], setToCol = tcs[1];   // фото результата → в общую коллекцию (3fe0b3fa)
    var zs = useState(''); var zoom = zs[0], setZoom = zs[1];   // лайтбокс картинки модели (OpenTodo aff57787)
    var bls = useState(''); var bill = bls[0], setBill = bls[1];   // цена для «Выставить счёт» (пусто = текущая)
    function load() { API.adminOrder(app.adminOrderId).then(function (j) { if (j.ok) { setData(j); setPf({ price: j.order.pending_price != null ? j.order.pending_price : j.order.price, comment: j.order.price_comment || '' }); setDue(j.order.due || ''); } }); }
    useEffect(function () { load(); }, []);
    if (!data) return h('div', { className: 'screen' }, h(Lab, null, 'Заказ'), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…'));
    var o = data.order, cur = data.currency;
    // Права сотрудника (A4): у оператора карточка приезжает без цен, счетов и баланса —
    // рисовать эти блоки нечем и незачем (сервер их вырезал).
    var canMoney = data.money !== false;
    // для «в работу» передаём срок (его можно ввести в поле ниже), остальные статусы — без срока
    function setStatus(s) { API.adminOrderStatus(o.id, s, s === 'in_progress' ? due : '', s === 'printed' ? actual : '', s === 'rejected' ? rejReason : '').then(function (j) { if (j.ok) { app.toast('Статус: ' + (ST_LABEL[s] || s)); load(); } else app.toast(j.error || 'Ошибка'); }); }
    function savePrice() { API.adminOrderPrice(o.id, pf.price, pf.comment).then(function (j) { if (j.ok) { app.toast('Новая цена отправлена клиенту'); load(); } else app.toast(j.error || 'Ошибка'); }); }
    // OpenTodo 8c1cab08: пересчёт по граммам+времени (тот же эндпоинт, ответ несёт посчитанную цену)
    function savePriceGt() {
      var g = parseFloat(String(gtf.grams || '').replace(',', '.'));
      if (!g || g <= 0) { app.toast('Введите граммы'); return; }
      var mins = (parseInt(gtf.hours, 10) || 0) * 60 + (parseInt(gtf.minutes, 10) || 0);
      API.adminOrderPriceByGt(o.id, g, mins, pf.comment).then(function (j) {
        if (!j.ok) { app.toast(j.error || 'Ошибка'); return; }
        app.toast('Новая цена ' + money(j.price, cur) + (j.applied === 'direct' ? ' — отправлена клиенту' : ' — на подтверждение клиенту'));
        setGtf({ grams: '', hours: '', minutes: '' }); load();
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    // Выставить счёт (этап «Ждёт оплаты», фидбэк 31.07): одобрение заказа + опциональное
    // уточнение цены + инвойс клиенту — одним действием и одним сообщением.
    function sendBill() {
      var p = String(bill || '').replace(',', '.').trim();
      API.adminOrderBill(o.id, p ? parseFloat(p) : null).then(function (j) {
        if (j.ok) { app.toast('Счёт отправлен клиенту — заказ ждёт оплаты'); setBill(''); load(); }
        else app.toast(j.error || 'Ошибка');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function printNet(start) { API.adminOrderPrintNet(o.id, start).then(function (j) { app.toast(j.ok ? (start ? 'Отправлено на печать' : 'Файл загружен на принтер') : (j.error || 'Ошибка')); }); }
    function openSlicer() { API.adminOrderOpenSlicer(o.id).then(function (j) { app.toast(j.ok ? (j.note || 'Открываю в ElegooSlicer') : (j.error || 'Ошибка')); }); }
    function openFolder() { API.adminOrderOpenFolder(o.id).then(function (j) { app.toast(j.ok ? (j.note || 'Открываю папку на ПК бота') : (j.error || 'Ошибка')); }); }
    function send() { var t = msg.trim(); if (!t) return; setMsg(''); API.adminOrderMessage(o.id, t).then(function (j) { if (j.ok) setData(function (dd) { return Object.assign({}, dd, { order: Object.assign({}, dd.order, { messages: j.messages }) }); }); }); }
    function delOrder() {
      if (!window.confirm('Удалить заказ #' + o.id + ' «' + o.label + '» из памяти?\n\nДействие необратимо: заказ, его история и переписка будут удалены.')) return;
      API.adminOrderDelete(o.id).then(function (j) { if (j.ok) { app.toast('Удалено'); app.go('adminOrders'); } else app.toast(j.error || 'Ошибка'); });
    }
    // Фото готовой печати (done_photo) → POST /api/admin/photo kind=order, затем перечитываем заказ.
    function uploadDone(file) {
      if (!file) return;
      if (file.size > 12 * 1024 * 1024) { app.toast('Фото больше 12 МБ — выберите поменьше'); return; }
      var rd = new FileReader();
      rd.onload = function () {
        app.toast('Загружаю фото…');
        API.adminPhoto('order', o.id, rd.result, toCol).then(function (j) {
          if (j.ok) {
            app.toast(toCol ? (j.collection ? 'Фото сохранено и добавлено в коллекцию' : 'Фото сохранено (карточки модели в коллекции нет)') : 'Фото результата сохранено');
            load();
          } else app.toast(j.error || 'Не удалось загрузить');
        }).catch(function () { app.toast('Сеть недоступна'); });
      };
      rd.readAsDataURL(file);
    }
    function hm(m) { m = Math.round(Number(m) || 0); return Math.floor(m / 60) + 'ч ' + (m % 60) + 'м'; }
    function cmpRow(label, val) {
      return h('div', { className: 'spread', style: { marginTop: 6, fontSize: 14 } },
        h('span', { style: { color: 'var(--muted)' } }, label), h('b', { className: 'mono' }, val));
    }
    function runRecalc() {
      setRLoad(true);
      API.adminOrderRecalc(o.id).then(function (j) {
        setRLoad(false);
        if (!j.ok) { app.toast(j.error || 'Ошибка пересчёта'); return; }
        setRecalc({ old: j.old, price: j.new.price, grams: (Number(j.new.grams) || 0).toFixed(1), minutes: String(Math.round(j.new.minutes)) });
      }).catch(function () { setRLoad(false); app.toast('Сеть недоступна'); });
    }
    function recalcEdit(patch) {   // правка граммов/времени → авто-пересчёт цены (dry, без сохранения)
      var rc = Object.assign({}, recalc, patch); setRecalc(rc);
      API.adminOrderRecalcApply(o.id, rc.grams, rc.minutes, true).then(function (j) {
        if (j.ok) setRecalc(function (cur) { return cur ? Object.assign({}, cur, { price: j.price }) : cur; });
      });
    }
    function applyRecalc() {
      if (!recalc) return;
      API.adminOrderRecalcApply(o.id, recalc.grams, recalc.minutes, false).then(function (j) {
        if (!j.ok) { app.toast(j.error || 'Ошибка'); return; }
        app.toast(j.applied === 'pending' ? 'Дороже прежней — отправлено клиенту на подтверждение'
          : j.applied === 'accepted' ? 'Клиент разрешил такое подорожание — цена принята, заказ в работе'
          : 'Цена применена, клиент уведомлён');
        setRecalc(null); load();
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    // Готовое изделие (product_id): товар уже напечатан — только «Новый» и «Вручено» (26a44d79)
    // «Отложен» — заказ, который сейчас не берём, но клиент оставляет его на будущее:
    // из очереди уходит, филамент не резервирует, но живёт (db.REQ_STATUS_LABELS.deferred).
    var STATUSES = o.product_id ? [['new', 'Новый'], ['deferred', 'Отложен'], ['handed', 'Вручено']]
      : [['new', 'Новый'], ['awaiting_payment', 'Ждёт оплаты'], ['in_progress', 'В работе'], ['deferred', 'Отложен'], ['printed', 'Напечатано'], ['rejected', 'Отказ']];
    return h('div', { className: 'screen' },
      h('div', { className: 'card' },
        h('div', { className: 'spread' }, h('div', null, h('span', { className: 'id' }, '#', o.id), ' ', h('b', { style: { fontSize: 16 } }, o.label)),
          h('span', { className: 'stbadge ' + (ST_CLASS[o.status] || 'st-new'), style: { marginTop: 0 } }, o.status_label)),
        h('div', { className: 'sub', style: { marginTop: 7, color: 'var(--muted)', fontSize: 13 } }, o.user, ' · ', o.qty, ' шт',
          o.dims ? h('span', null, ' · ', h('span', { className: 'mono' }, o.dims), ' мм') : null,
          // масштаб заказа (как в карточке деска): не 100% — подсветка, нет значения — не рисуем
          o.scale_pct ? h('span', null, ' · масштаб ', h('span', { className: 'mono',
            style: o.scale_pct !== 100 ? { color: 'var(--accent)', fontWeight: 700 } : undefined }, o.scale_pct + '%')) : null),
        !o.is_custom && h('div', { className: 'specrow', style: { marginTop: 9 } },
          h('span', { className: 's' }, icon('spool', { size: 15 }), ' ', o.plastic),
          h('span', { className: 's' }, icon('scale', { size: 15 }), h('b', null, (Number(o.grams) || 0).toFixed(1)), ' г')),
        !o.is_custom && h('div', { className: 'specrow', style: { marginTop: 4 } },
          h('span', { className: 's' }, icon('layers', { size: 15 }), ' ', o.quality),
          h('span', { className: 's' }, icon('shield', { size: 15 }), ' ', o.strength)),
        // оригинал ссылки на модель — для анализа мастером (OpenTodo 05e215c0)
        o.src_url && h('div', { style: { marginTop: 9, display: 'flex', alignItems: 'center', gap: 8 } },
          h('span', { style: { color: 'var(--faint)', display: 'flex' } }, icon('link', { size: 15 })),
          h('a', { style: { flex: 1, minWidth: 0, fontSize: 12.5, color: 'var(--accent)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' },
            onClick: function () { exportLink(o.src_url); } }, o.src_url.replace(/^https?:\/\//, '')),
          h('button', { className: 'btn line', style: { marginTop: 0, width: 40, minHeight: 40, padding: 0, flex: '0 0 auto' }, 'aria-label': 'Копировать ссылку',
            onClick: function () { try { navigator.clipboard.writeText(o.src_url); app.toast('Ссылка скопирована'); } catch (e) { app.toast(o.src_url); } } }, icon('doc', { size: 14 }))),
        canMoney && h('div', { className: 'price', style: { marginTop: 12, fontSize: 26 } }, money(o.price, cur))),

      // Картинка/фото модели (OpenTodo aff57787): мастер видит, ЧТО заказали, прямо в карточке —
      // тап открывает во весь экран (тот же PS_Lightbox, что в списке заказов и профиле).
      o.preview && h('div', { className: 'card', style: { marginTop: 12, padding: 8 } },
        h('img', { src: o.preview, alt: o.label || '', loading: 'lazy',
          style: { width: '100%', maxHeight: 300, objectFit: 'contain', borderRadius: 10, cursor: 'zoom-in', background: 'var(--bg-2)' },
          onClick: function () { setZoom(o.preview); } })),

      // Себестоимость по факту + маржа (Фаза C мастер-плана): показываем, только когда cogs
      // посчитан (катушки с закупочной ценой + печать привязана к заказу).
      (o.cogs_actual != null) && h('div', { className: 'card mono', style: { marginTop: 12, fontSize: 13, color: 'var(--muted)' } },
        'Себестоимость по факту: ', money(o.cogs_actual, cur), ' · маржа: ',
        h('b', { style: { color: (o.margin || 0) >= 0 ? 'var(--accent)' : 'var(--red)' } }, money(o.margin, cur))),

      o.breakdown && h(Breakdown, { bd: o.breakdown, title: 'Расчётник цены' }),
      !o.is_custom && o.breakdown && h('button', { className: 'btn line', style: { marginTop: 12 }, onClick: function () { app.orderPdf(o.id); } }, icon('doc', { size: 17 }), ' PDF-смета'),

      // пожелание клиента из конфигуратора (d929c054) — для обычных заказов; если автослайс не
      // удался (OpenTodo 55a1cf8f), тут же лежит причина — подкрашиваем карточку заметнее и
      // подсказываем инструмент («Изменить цену» → «По граммам и времени» ниже на этом экране).
      // price_pending (очередь досчёта, Э0) — НЕ провал: цену посчитает ПК сам и пришлёт клиенту,
      // оценивать руками нечего. Поэтому отдельная, спокойная плашка, а не янтарная тревога.
      !o.is_custom && o.description && h('div', { className: 'card', style: { marginTop: 12,
          background: (o.unsliced && !o.price_pending) ? 'var(--amber-soft)' : undefined, borderColor: (o.unsliced && !o.price_pending) ? 'var(--amber-soft)' : undefined } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 8,
            color: (o.unsliced && !o.price_pending) ? 'var(--amber)' : undefined } },
          icon(o.price_pending ? 'clock' : (o.unsliced ? 'flame' : 'chat'), { size: 17, style: { color: (o.unsliced && !o.price_pending) ? undefined : 'var(--accent)' } }),
          o.price_pending ? 'Ждёт расчёта ПК — цена придёт клиенту автоматически'
            : (o.unsliced ? 'Автослайс не удался — оцените вручную' : 'Комментарий клиента')),
        h('div', { style: { whiteSpace: 'pre-wrap', fontSize: 14, lineHeight: 1.5, color: 'var(--text)' } }, o.description)),

      o.is_custom && h('div', { className: 'card', style: { marginTop: 12 } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 8 } },
          icon('palette', { size: 17, style: { color: 'var(--accent)' } }), 'Что хочет клиент'),
        h('div', { style: { whiteSpace: 'pre-wrap', fontSize: 14, lineHeight: 1.5, color: 'var(--text)' } }, o.description || '—'),
        (o.photos || []).length > 0 && h('div', { className: 'grid2', style: { marginTop: 11, gap: 9 } }, o.photos.map(function (src, i) {
          return h('img', { key: i, src: src, alt: '', style: { width: '100%', height: 130, objectFit: 'cover', borderRadius: 12 } });
        }))),

      o.is_custom && canMoney && h(InvoiceForm, { app: app, order: o, cur: cur, onDone: load }),

      // Доставка и отправление (A2): цена доставки — только владельцу, трек-номер — всей команде.
      // Секции нет у заказов, где способ получения вообще не выбирали (старые корзины до A2):
      // рисовать «🚚 Доставка» там, где её не было, значит врать мастеру.
      o.group && (o.group.delivery_method || o.group.delivery_cost
        || (o.group.track && (o.group.track.number || o.group.track.shipped_at)))
        && h(DeliveryBlock, { app: app, g: o.group, cur: cur, canMoney: canMoney, onDone: load }),

      h(Lab, null, 'Статус'),
      h('div', { className: 'chips' }, STATUSES.map(function (s) {
        return h('button', { key: s[0], className: 'chip' + (o.status === s[0] ? ' on' : ''), onClick: function () { setStatus(s[0]); } }, s[1]);
      })),
      // Выставить счёт (этап «Ждёт оплаты»): для обычного неоплаченного заказа. Поле цены
      // пустое = выставить по текущей; вписанная цена уточняется тем же действием и
      // отмечается в описании счёта («было → стало»).
      canMoney && !o.is_custom && !o.paid && (o.status === 'new' || o.status === 'awaiting_payment') &&
        h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 9, flexWrap: 'wrap' } },
          h('span', { style: { fontSize: 12.5, color: 'var(--muted)' } }, 'Счёт на оплату:'),
          h('input', { className: 'input mono', style: { flex: '0 0 96px', marginTop: 0 }, placeholder: String(o.price || ''), value: bill, onChange: function (e) { setBill(e.target.value); } }),
          h('button', { className: 'btn soft', style: { width: 'auto', padding: '0 16px', marginTop: 0 }, onClick: sendBill },
            icon('card', { size: 15 }), o.status === 'awaiting_payment' ? ' Перевыставить' : ' Выставить счёт')),
      // Пустое поле = счёт по ТЕКУЩЕЙ цене, а доставка пересчитается всё равно — поэтому
      // предупреждение смотрит на введённую ИЛИ текущую цену.
      canMoney && !o.is_custom && !o.paid && (o.status === 'new' || o.status === 'awaiting_payment') &&
        deliveryWarn(o, bill || o.price, cur, true),
      // Галочка «оплачен» (OpenTodo 7f257a33): включает реферальный бонус, когда заказ ещё и напечатан.
      // Заказ, оплаченный ОНЛАЙН (A1), галочкой не разоплачивается — деньги реально получены,
      // сервер такой запрос отклонит; показываем способ и сумму из леджера платежей.
      !canMoney ? null : o.pay_method === 'online'
        ? h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 9, fontSize: 13.5 } },
            h('span', { className: 'stbadge st-printed' }, icon('check', { size: 12 }), ' Оплачен онлайн'),
            (o.payments || []).length > 0 && h('span', { style: { fontSize: 11.5, color: 'var(--faint)' } },
              (o.payments || []).map(function (p) { return p.amount + ' ' + p.currency; }).join(' + '),
              ' через Telegram Payments'))
        : h('label', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 9, fontSize: 13.5, cursor: 'pointer' } },
            h('input', { type: 'checkbox', checked: !!o.paid, onChange: function (e) {
              var v = e.target.checked;
              API.adminOrderPaid(o.id, v).then(function (j) { if (j.ok) { app.toast(v ? 'Отмечен оплаченным' : 'Оплата снята'); load(); } else app.toast(j.error || 'Ошибка'); });
            } }),
            h('b', null, 'Оплачен'),
            h('span', { style: { fontSize: 11.5, color: 'var(--faint)' } }, '— вместе с «напечатано» включает реферальный бонус')),
      // Поля переходов показываем ПО СТАТУСУ (аудит UX): у «нового» заказа не нужны
      // «факт. печать», у «напечатанного» — срок и отказ. Меньше шума на длинном экране.
      (function () {
        if (o.product_id) return null;   // у готового изделия печатных переходов нет
        var stActive = ['new', 'price_changed', 'invoiced', 'in_progress', 'printing', 'deferred'].indexOf(o.status) >= 0;
        var stInWork = o.status === 'in_progress' || o.status === 'printing';
        return h(React.Fragment, null,
          stActive && !stInWork && h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 9, flexWrap: 'wrap' } },
            h('span', { style: { fontSize: 12.5, color: 'var(--muted)' } }, 'Срок (для «В работе»):'),
            h('input', { className: 'input mono', style: { flex: '0 0 96px', marginTop: 0 }, placeholder: 'ДД.ММ', value: due, onChange: function (e) { setDue(e.target.value); } }),
            h('button', { className: 'btn soft', style: { width: 'auto', padding: '0 16px', marginTop: 0 }, onClick: function () { setStatus('in_progress'); } }, 'В работу')),
          !o.is_custom && stInWork && h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 7, flexWrap: 'wrap' } },
            h('span', { style: { fontSize: 12.5, color: 'var(--muted)' } }, 'Факт. печать (мин, для «Напечатано»):'),
            h('input', { className: 'input mono', style: { flex: '0 0 96px', marginTop: 0 }, placeholder: 'мин', value: actual, onChange: function (e) { setActual(e.target.value); } }),
            h('button', { className: 'btn soft', style: { width: 'auto', padding: '0 16px', marginTop: 0 }, onClick: function () { setStatus('printed'); } }, 'Напечатано')),
          // причина отказа — для аналитики воронки (crm-roadmap этап 6)
          stActive && h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 7, flexWrap: 'wrap' } },
            h('span', { style: { fontSize: 12.5, color: 'var(--muted)' } }, 'Причина (для «Отказ»):'),
            h('input', { className: 'input', style: { flex: '1 1 140px', marginTop: 0 }, placeholder: 'дорого / долго / передумал…', value: rejReason, onChange: function (e) { setRejReason(e.target.value); } }),
            h('button', { className: 'btn soft', style: { width: 'auto', padding: '0 16px', marginTop: 0 }, onClick: function () { setStatus('rejected'); } }, 'Отказ')));
      })(),
      o.status === 'rejected' && o.reject_reason && h('p', { className: 'hint', style: { marginTop: 6 } }, 'Причина отказа: ' + o.reject_reason),

      canMoney && h(Lab, null, 'Изменить цену'),
      // OpenTodo 8c1cab08: два режима — «по-старинке» вписать цену, или вписать граммы+часы/минуты
      // и дать серверу пересчитать по формуле (пластик и тираж — из заказа, без повторного слайса).
      canMoney && h('div', { className: 'chips', style: { marginBottom: 8 } },
        h('button', { className: 'chip' + (priceMode === 'price' ? ' on' : ''), onClick: function () { setPriceMode('price'); } }, 'Вписать цену'),
        h('button', { className: 'chip' + (priceMode === 'gt' ? ' on' : ''), onClick: function () { setPriceMode('gt'); } }, 'По граммам и времени')),
      canMoney && priceMode === 'price' && h('div', { style: { display: 'flex', gap: 8 } },
        h('input', { className: 'input mono', style: { flex: '0 0 120px' }, inputMode: 'decimal', value: pf.price, onChange: function (e) { setPf(Object.assign({}, pf, { price: e.target.value })); } }),
        h('input', { className: 'input', placeholder: 'Комментарий', value: pf.comment, onChange: function (e) { setPf(Object.assign({}, pf, { comment: e.target.value })); } })),
      canMoney && priceMode === 'gt' && h(React.Fragment, null,
        h('div', { style: { display: 'flex', gap: 8 } },
          h('input', { className: 'input mono', style: { flex: '1 1 90px' }, inputMode: 'decimal', placeholder: 'Граммы', value: gtf.grams, onChange: function (e) { setGtf(Object.assign({}, gtf, { grams: e.target.value })); } }),
          h('input', { className: 'input mono', style: { flex: '0 0 60px' }, inputMode: 'numeric', placeholder: 'ч', value: gtf.hours, onChange: function (e) { setGtf(Object.assign({}, gtf, { hours: e.target.value })); } }),
          h('input', { className: 'input mono', style: { flex: '0 0 60px' }, inputMode: 'numeric', placeholder: 'мин', value: gtf.minutes, onChange: function (e) { setGtf(Object.assign({}, gtf, { minutes: e.target.value })); } })),
        h('input', { className: 'input', style: { marginTop: 8 }, placeholder: 'Комментарий', value: pf.comment, onChange: function (e) { setPf(Object.assign({}, pf, { comment: e.target.value })); } }),
        h('p', { className: 'hint', style: { marginTop: 6 } }, 'Цена посчитается по цене пластика заказа и текущим настройкам расчёта — без повторного слайса.')),
      // «было X» + предупреждение при большой разнице (аудит UX: защита от 5000 вместо 500) — только для ручной цены
      (function () {
        if (!canMoney) return null;
        var np = priceMode === 'price' ? parseFloat(String(pf.price || '').replace(',', '.')) : 0;
        var big = np > 0 && o.price > 0 && Math.abs(np - o.price) / o.price > 0.2 && Math.round(np * 100) !== Math.round(o.price * 100);
        return h(React.Fragment, null,
          o.price > 0 && h('p', { className: 'hint', style: { marginTop: 6 } }, 'Сейчас: ' + money(o.price, cur)
            + (big ? ' — новая цена отличается больше чем на 20%, проверьте сумму' : '')),
          priceMode === 'price' && deliveryWarn(o, pf.price, cur),
          h('button', { className: 'btn soft', style: { marginTop: 9, color: big ? 'var(--amber)' : undefined },
            onClick: priceMode === 'price' ? savePrice : savePriceGt },
            icon('coin', { size: 17 }), priceMode === 'price' ? ' Отправить новую цену' : ' Посчитать и отправить'));
      })(),

      !o.is_custom && h(MaterialBox, { app: app, order: o, onDone: load }),

      canMoney && h(BalanceBox, { app: app, order: o, cur: cur, onDone: load }),

      canMoney && !o.is_custom && h(Lab, null, 'Пересчёт заказа'),
      canMoney && !o.is_custom && !recalc && h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: runRecalc, disabled: rLoad },
        icon('coin', { size: 17 }), rLoad ? ' Считаю… (повторный слайс)' : ' Пересчитать по слайсеру'),
      canMoney && !o.is_custom && !recalc && h('p', { className: 'hint', style: { marginTop: 6 } }, 'Повторно нарежет модель текущим слайсером и покажет, как изменятся время, граммы и цена. Применение: дешевле — сразу с уведомлением клиента, дороже — на подтверждение.'),
      canMoney && !o.is_custom && recalc && h('div', { className: 'card', style: { marginTop: 0 } },
        cmpRow('Цена', money(recalc.old.price, cur) + ' → ' + money(recalc.price, cur)),
        cmpRow('Время', hm(recalc.old.minutes) + ' → ' + hm(recalc.minutes)),
        cmpRow('Граммы', (Number(recalc.old.grams) || 0).toFixed(1) + ' → ' + recalc.grams + ' г'),
        h('div', { style: { marginTop: 11, display: 'flex', gap: 8, alignItems: 'center' } },
          h('span', { style: { fontSize: 12.5, color: 'var(--muted)', flex: '0 0 92px' } }, 'Время (мин):'),
          h('input', { className: 'input mono', style: { marginTop: 0 }, inputMode: 'decimal', value: recalc.minutes, onChange: function (e) { recalcEdit({ minutes: e.target.value }); } })),
        h('div', { style: { marginTop: 7, display: 'flex', gap: 8, alignItems: 'center' } },
          h('span', { style: { fontSize: 12.5, color: 'var(--muted)', flex: '0 0 92px' } }, 'Граммы:'),
          h('input', { className: 'input mono', style: { marginTop: 0 }, inputMode: 'decimal', value: recalc.grams, onChange: function (e) { recalcEdit({ grams: e.target.value }); } })),
        h('p', { className: 'hint', style: { marginTop: 6 } }, 'Можно поправить вручную, если расчёт промахнулся — цена пересчитается сама.'),
        h('div', { className: 'price', style: { marginTop: 10, fontSize: 24 } }, money(recalc.price, cur)),
        deliveryWarn(o, recalc.price, cur),
        h('div', { className: 'grid2', style: { gap: 9, marginTop: 6 } },
          h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { setRecalc(null); } }, 'Отмена'),
          h('button', { className: 'btn', style: { marginTop: 0 }, onClick: applyRecalc }, ' Применить'))),

      o.can_print_net && h(Lab, null, 'Сетевая печать'),
      o.can_print_net && h('div', { className: 'grid2', style: { gap: 9 } },
        h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { printNet(false); } }, icon('upload', { size: 17 }), ' Загрузить файл'),
        h('button', { className: 'btn', style: { marginTop: 0 }, onClick: function () { printNet(true); } }, icon('printer', { size: 17 }), ' Печатать')),

      (o.can_open_slicer || o.can_open_folder) && h(Lab, null, 'Действия с ПК бота'),
      o.can_open_slicer && h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: openSlicer }, icon('box', { size: 17 }), ' Открыть в слайсере'),
      o.can_open_slicer && h('p', { className: 'hint', style: { marginTop: 6 } }, 'Откроет .3mf в ElegooSlicer на компьютере, где запущен бот, уже с вашим пресетом и профилями принтера. Печать отправляете оттуда в один клик.'),
      o.can_open_folder && h('button', { className: 'btn line', style: { marginTop: o.can_open_slicer ? 6 : 0 }, onClick: openFolder }, icon('folder', { size: 17 }), ' Открыть папку'),
      o.can_open_folder && h('p', { className: 'hint', style: { marginTop: 6 } }, 'Откроет папку с файлом модели в проводнике на компьютере, где запущен бот.'),

      h(Lab, null, 'Фото результата'),
      o.done_url && h('img', { src: o.done_url, alt: '', style: { width: '100%', maxHeight: 220, objectFit: 'cover', borderRadius: 12, marginBottom: 9 } }),
      h('div', { style: { display: 'flex', gap: 9 } },
        h('label', { className: 'btn line', style: { marginTop: 0, cursor: 'pointer', flex: 1 } },
          icon('image', { size: 17 }), ' ', o.done_url ? 'Заменить фото' : 'Прикрепить фото',
          h('input', { type: 'file', accept: 'image/*', style: { display: 'none' },
            onChange: function (e) { if (e.target.files[0]) uploadDone(e.target.files[0]); e.target.value = ''; } })),
        // удаление фото результата (1faab6cb): скрины стола печатей остаются — у них своя кнопка
        o.done_url ? h('button', { className: 'btn line', type: 'button', title: 'Удалить фото результата',
          style: { marginTop: 0, width: 46, padding: 0, flex: '0 0 auto' },
          onClick: function () {
            if (!window.confirm('Удалить фото результата заказа #' + o.id + '?\n\nОно пропадёт и у клиента.')) return;
            API.adminOrderMediaDelete(o.id, 'photo').then(function (j) {
              if (j.ok) { app.toast('Фото удалено'); load(); } else app.toast(j.error || 'Не удалось');
            });
          } }, '🗑') : null),
      h('p', { className: 'hint', style: { marginTop: 6 } }, 'Фото готовой печати — попадёт в карточку заказа и PDF-смету.'),
      !o.is_custom && h('label', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, fontSize: 13, color: 'var(--muted)', cursor: 'pointer' } },
        h('input', { type: 'checkbox', checked: toCol, onChange: function (e) { setToCol(e.target.checked); } }),
        'Добавить фото в общую коллекцию (заменит рендер модели)'),

      h(AttachPrintBox, { app: app, order: o, onDone: load }),

      (o.rating || o.review) && h(Lab, null, 'Отзыв клиента'),
      (o.rating || o.review) && h('div', { className: 'card', style: { padding: '12px 14px' } },
        // шкала 1–10 (bdcc18e4): звёзды мельче, чтобы ряд из 10 влезал на телефоне
        o.rating > 0 && h('div', { style: { fontSize: 17, letterSpacing: 1, marginBottom: o.review ? 7 : 0 } },
          Array.from({ length: 10 }, function (_, i) {
            return h('span', { key: i, style: { color: i < Math.round(o.rating) ? '#ffb020' : 'var(--surface-2)' } }, '★');
          }),
          h('span', { style: { fontSize: 13, color: 'var(--muted)', marginLeft: 8, verticalAlign: 'middle' } }, o.rating + ' / 10')),
        o.review && h('div', { style: { fontSize: 14, lineHeight: 1.55, color: 'var(--text)', whiteSpace: 'pre-wrap' } }, o.review)),

      h(Lab, null, 'Переписка с клиентом'),
      h('div', { className: 'card', style: { padding: 12 } },
        (o.messages || []).length === 0 && h('div', { style: { color: 'var(--faint)', fontSize: 13 } }, 'Сообщений нет.'),
        (o.messages || []).map(function (m, i) {
          // Реакция клиента приезжает сюда отдельным сообщением-эмодзи с цитатой того, на что
          // её поставили; галочка у своих показывает, открыл ли клиент переписку.
          var solo = window.PS_SoloEmoji(m.text) && !m.image;
          return h('div', { key: i, style: { display: 'flex', justifyContent: m.mine ? 'flex-end' : 'flex-start', margin: '5px 0' } },
            h('div', { style: { maxWidth: '82%', padding: '8px 12px', borderRadius: 13, fontSize: 13.5,
              background: m.mine ? 'var(--accent)' : 'var(--surface-2)', color: m.mine ? '#1a0d02' : 'var(--text)' } },
              h(window.PS_ChatQuote, { q: m.reply }),
              solo ? h('div', { style: { fontSize: 27, lineHeight: 1.15 } }, m.text) : m.text,
              h('div', { style: { fontSize: 10, opacity: .6, textAlign: 'right', marginTop: 2 } },
                m.at, h(window.PS_ChatRead, { mine: m.mine, read: m.read }))));
        }),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
          h('input', { className: 'input', placeholder: 'Ответить клиенту…', value: msg, onChange: function (e) { setMsg(e.target.value); }, onKeyDown: function (e) { if (e.key === 'Enter') send(); } }),
          h('button', { className: 'btn', style: { width: 'auto', padding: '0 16px', marginTop: 0 }, onClick: send }, icon('share', { size: 18 })))),

      // Удаление заказа — действие владельца (A4): оператору ручка закрыта и на сервере.
      app.can('settings') && h(Lab, null, 'Опасная зона'),
      // выделенная красная зона (аудит UX): кнопка удаления не должна сливаться с обычными
      app.can('settings') && h('div', { className: 'card', style: { marginTop: 0, background: 'rgba(226,59,59,.06)', borderColor: 'var(--red-soft)', padding: 13 } },
        h('p', { className: 'hint', style: { marginTop: 0, marginBottom: 9 } }, 'Удаление сотрёт заказ, его историю и переписку — без возврата.'),
        h('button', { className: 'btn line', style: { marginTop: 0, color: 'var(--red)', borderColor: 'var(--red-soft)' }, onClick: delOrder }, icon('trash', { size: 17 }), ' Удалить заказ')),
      app.can('settings') && h('p', { className: 'hint', style: { marginTop: 6 } }, 'Заказ, его история и переписка будут удалены безвозвратно.'),
      h(window.PS_Lightbox, { src: zoom, name: zoom ? ('print-studio-order-' + o.id + '.png') : '', onClose: function () { setZoom(''); } }));
  }

  // ───────────────────── ADMIN · QUEUE ─────────────────────
  function AdminQueue(props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    function load() { API.adminQueue().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function move(id, dir) { API.adminQueueMove(id, dir).then(function (j) { if (j.ok) load(); else app.toast(j.error || 'Нельзя'); }); }
    var q = (data && data.queue) || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(load) }, 'Очередь печати'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Порядок печати. Стрелками меняйте очередь — клиент видит свою позицию.'),
      !data && h('p', { className: 'hint' }, 'Загружаю…'),
      data && q.length === 0 && h('div', { className: 'empty' }, icon('queue', { size: 46 }), h('div', null, 'Очередь пуста.')),
      h('div', { style: { marginTop: 10 } }, q.map(function (o, i) {
        return h('div', { key: o.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 11 } },
          h('div', { className: 'mono', style: { fontSize: 19, fontWeight: 700, color: 'var(--accent)', width: 22, textAlign: 'center' } }, i + 1),
          // тап по строке открывает заказ (OpenTodo 4b3970fa); стрелки очереди — со stopPropagation
          h('div', { style: { flex: 1, minWidth: 0, cursor: 'pointer' },
            onClick: function () { app.openAdminOrder(o.id); } },
            h('div', { style: { fontWeight: 600, fontSize: 14, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } }, '#' + o.id + ' ' + o.label),
            h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 3 } },
              h('span', { className: 'stbadge ' + (ST_CLASS[o.status] || 'st-new'), style: { marginTop: 0, padding: '2px 7px', fontSize: 10.5 } }, o.status_label || ST_LABEL[o.status] || o.status),
              ' ', o.user, ' · ', Math.round((o.minutes || 0) / 60), ' ч', o.eta ? ' · к ' + o.eta : '')),
          h('div', { style: { display: 'flex', flexDirection: 'column', gap: 5 } },
            h('button', { className: 'qmove', onClick: function () { move(o.id, 'up'); } }, icon('chev', { size: 16, style: { transform: 'rotate(-90deg)' } })),
            h('button', { className: 'qmove', onClick: function () { move(o.id, 'down'); } }, icon('chev', { size: 16, style: { transform: 'rotate(90deg)' } }))));
      })));
  }

  // ───────────────────── ADMIN · СТАТУС / САМОДИАГНОСТИКА ─────────────────────
  // Read-only зеркало чат-команды /status (a:status). Бэкенд: GET /api/admin/status →
  // uptime, свободно ГБ, загрузка слайсера, очередь, активные принтеры/пластики, issues.
  function AdminStatus(props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    function load() { API.adminStatus().then(function (j) { setData(j || { ok: false }); }); }
    useEffect(function () { load(); }, []);
    // питание TURZX-экрана (тумблер): tz=null пока неизвестно, иначе bool; засеваем из /api/admin/status
    var tzS = useState(null); var tz = tzS[0], setTz = tzS[1];
    var tbS = useState(false); var tbusy = tbS[0], setTbusy = tbS[1];
    // автообновления (644367fb): результат ручной проверки поверх фоновой из /api/admin/status
    var upS = useState(null); var upd = upS[0], setUpd = upS[1];
    var ubS = useState(''); var ubusy = ubS[0], setUbusy = ubS[1];
    var umS = useState(''); var umode = umS[0], setUmode = umS[1];
    useEffect(function () { if (data && data.ok && data.status && data.status.update && !umode) setUmode(data.status.update.mode || 'notify'); }, [data]);
    function updCheck() {
      setUbusy('check');
      API.adminUpdateCheck().then(function (j) {
        setUbusy('');
        if (j.ok) { setUpd(j.update); if (j.update && j.update.mode) setUmode(j.update.mode); }
        else app.toast(j.error || 'Не удалось проверить');
      }).catch(function () { setUbusy(''); app.toast('Сеть недоступна'); });
    }
    function updApply() {
      if (!window.confirm('Обновить бота и перезапустить?\n\nБот будет недоступен одну-две минуты, пока сторож поднимает его с новым кодом.')) return;
      setUbusy('apply');
      API.adminUpdateApply().then(function (j) {
        setUbusy('');
        if (j.ok) app.toast('Обновляюсь — бот перезапустится через минуту-другую');
        else app.toast(j.error || 'Не удалось обновиться');
      }).catch(function () { setUbusy(''); app.toast('Сеть недоступна'); });
    }
    function updModeSet(m) {
      API.adminUpdateMode(m).then(function (j) {
        if (j.ok) setUmode(m); else app.toast(j.error || 'Не удалось');
      });
    }
    // бэкапы БД (b557e341): отдельный запрос — в /api/admin/status этих данных нет
    var bkS = useState(null); var bk = bkS[0], setBk = bkS[1];
    var bbS = useState(false); var bbusy = bbS[0], setBbusy = bbS[1];
    function loadBackups() { API.adminBackupStatus().then(function (j) { if (j && j.ok) setBk(j); }); }
    // экран «Статус» открыт и оператору (CAP_ORDERS), а ручки бэкапа — владельческие:
    // без этой проверки оператор смотрел бы на вечное «Загружаю…» после 403
    useEffect(function () { if (app.can('settings')) loadBackups(); }, []);
    function backupNow() {
      setBbusy(true);
      API.adminBackupNow().then(function (j) {
        setBbusy(false);
        if (j && j.ok) { app.toast('Бэкап готов'); loadBackups(); }
        else app.toast((j && j.error) || 'Бэкап не удался');
      }).catch(function () { setBbusy(false); app.toast('Сеть недоступна'); });
    }
    useEffect(function () { if (data && data.ok && data.status && data.status.turzx) setTz(!!data.status.turzx.enabled); }, [data]);
    function setScreen(on) { setTbusy(true); API.adminScreenPower(on).then(function (j) { if (j && j.ok && j.turzx) setTz(!!j.turzx.enabled); setTbusy(false); }); }
    function toggleRow(ic, on, offColor, title, desc, label, onClick, top, disabled) {
      return h('div', { style: { display: 'flex', alignItems: 'center', gap: 11, padding: '12px 13px', borderTop: top ? '1px solid var(--line)' : 'none' } },
        h('div', { style: { color: on ? 'var(--price)' : offColor, display: 'flex' } }, icon(ic, { size: 21 })),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { style: { fontWeight: 700, fontSize: 14 } }, title),
          h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 2, lineHeight: 1.4 } }, desc)),
        h('button', { className: 'btn line', disabled: disabled, style: { width: 'auto', padding: '0 14px', marginTop: 0 }, onClick: onClick }, label));
    }
    if (!data) return h('div', { className: 'screen' }, h(Lab, null, 'Статус'), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…'));
    if (!data.ok) return h('div', { className: 'screen' }, h(Lab, { right: refreshBtn(load) }, 'Статус'),
      h('div', { className: 'empty' }, icon('info', { size: 46 }), h('div', null, data.error || 'Статус недоступен')));
    var s = data.status || {};
    var issues = s.issues || [];
    var ok = issues.length === 0;
    var lowDisk = s.disk_gb != null && s.disk_gb >= 0 && s.disk_gb < 1;
    var mw = s.mw_token || {};
    var mwDays = (mw.configured && mw.days_left != null) ? mw.days_left : null;
    var mwExpired = mwDays != null && mwDays <= 0;
    var mwLow = mwDays != null && mwDays <= 7;            // ≤7 дней (включая истёкший) → подсветка
    function mwDate(ts) {
      if (!ts) return null;
      try {
        var d = new Date(ts * 1000);
        var p = function (n) { return ('0' + n).slice(-2); };
        return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear();
      } catch (e) { return null; }
    }
    var mwExp = mwDate(mw.expires_at);
    // Пул аккаунтов: usable — сколько сейчас в строю, ready — сколько всего с токеном. Все в
    // кулдауне после анти-бота 418 → качать нечем, это тоже повод для амбера.
    var mwAccs = s.mw_accounts || [];
    var mwReady = mw.ready != null ? mw.ready : (mw.configured ? 1 : 0);
    var mwUsable = mw.usable != null ? mw.usable : (mw.configured ? 1 : 0);
    var mwStuck = mw.configured && mwUsable === 0;
    var mwWarn = mwLow || mwStuck;                        // амбер: скоро истечёт / все в кулдауне
    var mwLine = !mw.configured ? 'Аккаунтов нет — загрузка идёт через браузер'
      : mwExpired ? 'Токен истёк — обнови: python mw_login.py → python get_jwt.py'
      : mwStuck ? 'Все аккаунты в кулдауне (анти-бот 418) — повторите позже'
      : mwDays != null ? ('Запас закачек ×' + mwReady + ' · действует ещё ' + mwDays + ' дн'
          + (mwExp ? ' (до ' + mwExp + ')' : '')
          + (mwLow ? ' — скоро обновится' : ' — обновляется автоматически'))
      : 'Задан, срок неизвестен';
    var mwTitle = !mw.configured ? 'Аккаунтов нет'
      : ('В строю ' + mwUsable + ' из ' + mwReady);
    // Строка на аккаунт — чтобы с телефона было видно, кто именно остывает.
    var mwRows = mwAccs.map(function (a) {
      var st = !a.configured ? 'нет токена'
        : !a.enabled ? 'выключен'
        : a.expired ? 'токен истёк'
        : a.cooldown_sec > 0 ? ('кулдаун ' + Math.max(1, Math.round(a.cooldown_sec / 60)) + ' мин')
        : (a.days_left != null ? Math.round(a.days_left) + ' дн' : 'активен');
      return h('div', { key: a.id, style: { display: 'flex', justifyContent: 'space-between',
          gap: 8, fontSize: 12, color: 'var(--muted)', marginTop: 3 } },
        h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, a.label),
        h('span', null, st));
    });
    function cell(cls, ic, label, val, unit, sub) {
      return h('div', { className: 'tcell' + (cls ? ' ' + cls : '') },
        h('div', { className: 'tl' }, icon(ic, { size: 14 }), ' ', label),
        h('div', { className: 'tv' }, val == null ? '—' : val, unit && h('small', null, unit)),
        sub && h('div', { className: 'tgt' }, sub));
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(load) }, 'Статус системы'),
      h('div', { className: 'card', style: { display: 'flex', alignItems: 'center', gap: 11,
        borderColor: 'color-mix(in srgb, ' + (ok ? 'var(--price)' : 'var(--amber)') + ' 38%, transparent)',
        background: ok ? 'var(--price-soft)' : 'var(--amber-soft)' } },
        h('div', { style: { color: ok ? 'var(--price)' : 'var(--amber)', display: 'flex' } }, icon(ok ? 'check' : 'info', { size: 26 })),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { style: { fontWeight: 700, fontSize: 14.5 } }, ok ? 'Всё в порядке' : ('Замечаний: ' + issues.length)),
          h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 2 } }, 'Самодиагностика окружения'))),
      h(Lab, null, 'Показатели'),
      h('div', { className: 'telem' },
        cell('', 'clock', 'Аптайм', s.uptime || '—'),
        cell(lowDisk ? 'hot' : '', 'box', 'Диск свободно', s.disk_gb != null ? s.disk_gb : null, ' ГБ', lowDisk ? 'мало места' : null),
        cell('', 'layers', 'Слайсер', (s.slices_running || 0) + ' / ' + (s.slices_cap || 0), '', 'слотов занято'),
        cell('', 'queue', 'Очередь', s.queue_count != null ? s.queue_count : null, '', (s.queue_minutes ? ('~' + API.human(s.queue_minutes)) : 'пусто')),
        cell('', 'printer', 'Принтеры', s.printers != null ? s.printers : null, '', 'активны'),
        cell('', 'spool', 'Пластики', s.plastics != null ? s.plastics : null, '', 'активны')),
      // ИИ и видеокарта (OpenTodo 689d72bb)
      (s.ai || s.gpu) && h(Lab, null, 'ИИ и видеокарта'),
      (s.ai || s.gpu) && h('div', { className: 'telem' },
        s.ai && cell(s.ai.off_by_flag ? 'hot' : '', 'sparkle', 'ИИ-аватарки',
          s.ai.off_by_flag ? 'выкл' : (s.ai.loaded ? 'в VRAM' : 'спит'), '',
          s.ai.off_by_flag ? 'выключены ярлыком' : (s.ai.loaded
            ? (s.ai.idle_min != null ? ('простой ' + s.ai.idle_min + ' мин') : 'готов')
            : 'загрузится при клике')),
        cell('', 'bolt', 'GPU-рендер', s.preview_gpu ? 'вкл' : 'выкл', '', 'растеризация превью'),
        s.gpu && s.gpu.vram_total_mb && cell('', 'layers', 'VRAM',
          (s.gpu.vram_used_mb / 1024).toFixed(1) + ' / ' + (s.gpu.vram_total_mb / 1024).toFixed(1), ' ГБ',
          s.gpu.name || null),
        s.gpu && s.gpu.util_pct != null && cell(s.gpu.temp_c > 80 ? 'hot' : '', 'flame', 'Загрузка GPU',
          s.gpu.util_pct, ' %', (s.gpu.temp_c != null ? s.gpu.temp_c + ' °C' : null)),
        s.calc_queue != null && cell(s.calc_queue > 0 ? 'hot' : '', 'lifebuoy', 'Очередь разбора',
          s.calc_queue, '', s.calc_queue > 0 ? 'ждут мастера' : 'пусто')),
      // Пул MakerWorld — личный модуль: в открытой версии его нет, и блок не рисуем.
      window.PS_hasFeature('makerworld') && h(Lab, null, 'Автозагрузка MakerWorld'),
      window.PS_hasFeature('makerworld') && h('div', { className: 'card', style: { display: 'flex', alignItems: 'center', gap: 11,
          borderColor: mwWarn ? 'color-mix(in srgb, var(--amber) 38%, transparent)' : 'var(--line)',
          background: mwWarn ? 'var(--amber-soft)' : undefined } },
        h('div', { style: { color: mwWarn ? 'var(--amber)' : 'var(--muted)', display: 'flex' } }, icon('globe', { size: 22 })),
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('div', { style: { fontWeight: 700, fontSize: 14 } }, mwTitle),
          h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 2, lineHeight: 1.4 } }, mwLine),
          mwRows.length > 1 ? h('div', { style: { marginTop: 6 } }, mwRows) : null)),
      h(Lab, null, 'Самодиагностика'),
      ok
        ? h('div', { className: 'card', style: { display: 'flex', alignItems: 'center', gap: 9, color: 'var(--price)' } },
            icon('check', { size: 18 }), h('span', { style: { fontSize: 13.5, fontWeight: 600 } }, 'Проблем не обнаружено'))
        : h('div', { className: 'card', style: { padding: 0, overflow: 'hidden' } }, issues.map(function (it, i) {
            return h('div', { key: i, style: { display: 'flex', alignItems: 'flex-start', gap: 9, padding: '11px 13px',
              borderTop: i ? '1px solid var(--line)' : 'none' } },
              h('span', { style: { color: 'var(--amber)', display: 'flex', flex: '0 0 auto', marginTop: 1 } }, icon('info', { size: 16 })),
              h('span', { style: { fontSize: 13, lineHeight: 1.4 } }, it));
          })),
      // Обновления кода (644367fb): версия, ручная проверка, применение и режим авто
      h(Lab, null, 'Обновления'),
      (function () {
        var v = s.version || {};
        var u = upd || ((s.update && s.update.checked_at) ? s.update : null);
        var behind = u && u.ok ? (u.behind || 0) : 0;
        var line = !u ? 'Ещё не проверялось — фоновая проверка раз в несколько часов'
          : !u.ok ? ('Проверка не удалась: ' + (u.error || 'ошибка'))
          : behind > 0 ? ('Доступно обновление: ' + behind + ' коммит(ов)')
          : 'Актуальная версия — новых коммитов нет';
        return h('div', { className: 'card', style: {
            borderColor: behind > 0 ? 'color-mix(in srgb, var(--amber) 38%, transparent)' : 'var(--line)',
            background: behind > 0 ? 'var(--amber-soft)' : undefined } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 11 } },
            h('div', { style: { color: behind > 0 ? 'var(--amber)' : 'var(--muted)', display: 'flex' } }, icon('bolt', { size: 22 })),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', { style: { fontWeight: 700, fontSize: 14 } },
                'Версия: ' + (v.channel || '?') + (v.sha ? ' · ' + v.sha : '')),
              h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 2, lineHeight: 1.4 } }, line)),
            behind > 0
              ? h('button', { className: 'btn', disabled: !!ubusy, style: { width: 'auto', padding: '0 14px', marginTop: 0 }, onClick: updApply }, ubusy === 'apply' ? '…' : 'Обновить')
              : h('button', { className: 'btn line', disabled: !!ubusy, style: { width: 'auto', padding: '0 14px', marginTop: 0 }, onClick: updCheck }, ubusy === 'check' ? '…' : 'Проверить')),
          behind > 0 && (u.commits || []).length ? h('div', { style: { marginTop: 8 } },
            (u.commits || []).slice(0, 8).map(function (c, i) {
              return h('div', { key: i, className: 'mono', style: { fontSize: 11, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: 2 } }, c);
            })) : null,
          h('div', { style: { display: 'flex', gap: 6, marginTop: 10, alignItems: 'center' } },
            h('span', { style: { fontSize: 12, color: 'var(--muted)', flex: '0 0 auto' } }, 'Режим:'),
            [['off', 'выкл'], ['notify', 'уведомлять'], ['auto', 'сам']].map(function (m) {
              return h('button', { key: m[0], className: 'chip' + (umode === m[0] ? ' on' : ''),
                style: { padding: '5px 11px', fontSize: 12 },
                onClick: function () { updModeSet(m[0]); } }, m[1]);
            })),
          h('p', { className: 'hint', style: { margin: '7px 0 0' } },
            umode === 'auto' ? 'Бот сам подтянет обновления с GitHub и перезапустится.'
              : umode === 'off' ? 'Обновления не проверяются — только вручную этой кнопкой.'
              : 'Придёт уведомление в Telegram, обновление — по кнопке.'));
      })(),
      // Бэкапы БД (b557e341): когда снимали, сколько копий, предупреждения и кнопка «сделать»
      app.can('settings') && h(Lab, null, 'Бэкапы'),
      app.can('settings') && (function () {
        if (!bk) return h('div', { className: 'card' }, h('p', { className: 'hint', style: { margin: 0 } }, 'Загружаю…'));
        var loc = bk.local || {}, off = bk.offsite || {};
        var snaps = loc.snapshots || [];
        var fresh = snaps[0];
        var lastRun = bk.last;
        // Возраст считаем по свежему СНИМКУ, а не по записи журнала: неудачная попытка
        // не должна выглядеть как «бэкап только что был».
        var ageH = fresh ? (Date.now() / 1000 - fresh.mtime) / 3600 : null;
        var stale = ageH === null || ageH > 13;      // 13 ч = два пропуска подряд при шаге 6 ч
        var warns = [];
        if (lastRun && !lastRun.ok) warns.push('Последний бэкап не удался: ' + (lastRun.error || 'причина неизвестна'));
        if (stale) warns.push(fresh
          ? ('Свежему снимку уже ' + Math.round(ageH) + ' ч, а он должен обновляться раз в ' + (loc.interval_hours || 6) + ' ч')
          : 'Локальных снимков нет — бэкап не работает');
        if (bk.same_volume) warns.push('Копии лежат на том же физическом диске, что и база — при отказе диска пропадёт всё.');
        var warn = warns.length > 0;
        var mb = function (b) { return (Number(b || 0) / 1048576).toFixed(1) + ' МБ'; };
        var age = ageH === null ? '' : (ageH < 1 ? 'меньше часа назад' : (Math.round(ageH) + ' ч назад'));
        var line = !lastRun ? 'Бэкапов ещё не было — первый снимок бот делает при старте'
          : lastRun.ok ? (age + ' · ' + mb(lastRun.size))
          : ('Попытка ' + lastRun.ts + ' — неудачно');
        return h('div', { className: 'card', style: {
            borderColor: warn ? 'color-mix(in srgb, var(--amber) 38%, transparent)' : 'var(--line)',
            background: warn ? 'var(--amber-soft)' : undefined } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 11 } },
            h('div', { style: { color: warn ? 'var(--amber)' : 'var(--muted)', display: 'flex' } }, icon('box', { size: 22 })),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', { style: { fontWeight: 700, fontSize: 14 } }, line),
              h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 2, lineHeight: 1.4 } },
                'Локально ' + snaps.length + ' из ' + (loc.keep || 0)
                  + ' · второй контур ' + (off.exists ? off.count : '—'))),
            h('button', { className: 'btn line', disabled: bbusy, style: { width: 'auto', padding: '0 14px', marginTop: 0 }, onClick: backupNow }, bbusy ? '…' : 'Сделать')),
          warns.map(function (w, i) {
            return h('p', { key: i, style: { margin: '8px 0 0', fontSize: 12, color: 'var(--amber)', lineHeight: 1.4 } }, w);
          }));
      })(),
      h(Lab, null, 'TURZX-экран'),
      h('div', { className: 'card', style: { padding: 0, overflow: 'hidden' } },
        toggleRow('monitor', !!tz, 'var(--muted)', 'Дашборд на TURZX',
          tz == null ? 'загрузка…' : (tz ? 'включён — показывает заказы, принтер и нагрузку' : 'выключен — подсветка панели погашена'),
          tbusy ? '…' : (tz ? 'Выкл' : 'Вкл'),
          function () { setScreen(!tz); }, false, tz == null || tbusy)));
  }

  // ───────────────────── ADMIN · PRINTER ─────────────────────
  function gf(obj) { // gf(obj, 'a.b', 'c') → first defined path
    for (var i = 1; i < arguments.length; i++) {
      var v = obj, ok = true, parts = arguments[i].split('.');
      for (var j = 0; j < parts.length; j++) { if (v == null) { ok = false; break; } v = v[parts[j]]; }
      if (ok && v != null) return v;
    }
    return null;
  }
  function AdminPrinter(props) {
    var app = props.app;
    var st = useState({ loading: true }); var data = st[0], setData = st[1];
    var tk = useState(0); var tick = tk[0], setTick = tk[1];
    var ce = useState(false); var camErr = ce[0], setCamErr = ce[1];
    var cn = useState(0); var camNonce = cn[0], setCamNonce = cn[1];
    var cdg = useState(null); var camDiag = cdg[0], setCamDiag = cdg[1];
    var cmd = useState('stream'); var camMode = cmd[0], setCamMode = cmd[1];
    var bs = useState(''); var busy = bs[0], setBusy = bs[1];           // действие, что сейчас выполняется
    var lt = useState(true); var lightOn = lt[0], setLightOn = lt[1];   // предполагаемое состояние света (toggle)
    var fl = useState(null); var files = fl[0], setFiles = fl[1];       // список файлов на принтере (по запросу)
    var fp = useState(''); var pickFile = fp[0], setPickFile = fp[1];   // выбранный файл для старта
    var hs = useState({ noz: [], bed: [] }); var hist = hs[0], setHist = hs[1];   // история температур для спарклайнов
    var os_ = useState(null); var orders = os_[0], setOrders = os_[1];   // заказы для привязки (по запросу, 70b5a590)
    var po = useState(''); var pickOrder = po[0], setPickOrder = po[1];
    function camFailed() { setCamErr(true); API.cameraCheck().then(setCamDiag); }
    // поток (multipart) не отрисовался → пробуем опрос кадрами; и он не сработал → заглушка
    function camImgError() { if (camMode === 'stream') setCamMode('snap'); else camFailed(); }
    function camRetry() { setCamErr(false); setCamDiag(null); setCamMode('stream'); setCamNonce(function (n) { return n + 1; }); }
    // загрузка телеметрии: quiet=true не мигает экраном «Опрашиваю…» и сохраняет последний рабочий
    // снимок при разовом сбое опроса (одиночный таймаут MQTT не должен гасить живой экран).
    function load(quiet) {
      if (!quiet) setData({ loading: true });
      API.adminPrinterStatus().then(function (j) {
        setData(function (prev) {
          if (quiet && (!j || !j.ok) && prev && prev.ok) return prev;
          return j || { ok: false };
        });
      });
    }
    useEffect(function () {
      load();
      // тик: тихо обновляем телеметрию (опрос ускорен на сервере) и двигаем кадр snap-фолбэка.
      // Плавный multipart-поток теперь держит сам прокси (авто-реконнект апстрима), без перетыка <img>.
      var iv = setInterval(function () { setTick(function (t) { return t + 1; }); load(true); }, 2500);
      // список заказов для привязки печати — из БД, дёшево: грузим сразу, без лишнего тапа
      // (файлы принтера НЕ автогрузим: это MQTT-запрос, CC2 отвечает не всегда — оставлен ручной)
      loadOrders();
      return function () { clearInterval(iv); };
    }, []);
    // Кольцевой буфер последних температур сопла/стола (по тому же опросу 2.5 c) → мини-графики.
    useEffect(function () {
      if (!data.ok) return;
      var s = data.summary || {};
      if (s.extruder_temp == null && s.bed_temp == null) return;
      setHist(function (prev) {
        return { noz: prev.noz.concat([Math.round(Number(s.extruder_temp) || 0)]).slice(-16),
                 bed: prev.bed.concat([Math.round(Number(s.bed_temp) || 0)]).slice(-16) };
      });
    }, [data]);

    // Команда управления печатью (pause/resume/stop/light). Блокируем кнопки на время запроса,
    // показываем ответ принтера тостом и тихо обновляем телеметрию (пауза/стоп меняют состояние).
    function control(action, opts) {
      opts = opts || {};
      if (busy) return;
      setBusy(action);
      API.adminPrinterControl(action, opts.on).then(function (j) {
        // честная диагностика (6785be2f + edde64e4): принтер выполняет команды, но unicast-
        // подтверждение шлёт только привязанному клиенту-слайсеру → unconfirmed = «отправлено»
        var err = j && (j.error || (j.err_code != null && 'принтер ответил кодом ' + j.err_code)
          || (j.method && !j.ok && 'метод ' + j.method + ' отклонён'));
        var okText = j && j.unconfirmed
          ? (opts.okText || 'Готово') + ' — отправлено (принтер не подтверждает выполнение)'
          : (opts.okText || 'Готово');
        app.toast(j && j.ok ? okText : (err || 'Не удалось (нет ответа принтера)'));
        if (j && j.ok && action === 'light') setLightOn(opts.on !== false);
        load(true);
      }).catch(function () { app.toast('Сеть недоступна'); })
        .then(function () { setBusy(''); });
    }
    function onStop() {
      if (!window.confirm('Остановить печать на принтере?\n\nТекущая модель будет отменена — это необратимо.')) return;
      control('stop', { okText: 'Печать остановлена' });
    }
    // Список файлов на принтере — подгружаем по требованию (отдельный MQTT-запрос, не в общий опрос).
    function loadFiles() {
      if (busy) return;
      setBusy('files');
      API.adminPrinterFiles().then(function (j) {
        if (j && j.ok) { setFiles(j.files || []); if (!pickFile && (j.files || []).length) setPickFile(j.files[0].name); }
        else app.toast((j && j.error) || 'Не удалось получить список');
      }).catch(function () { app.toast('Сеть недоступна'); })
        .then(function () { setBusy(''); });
    }
    function startFile() {
      if (busy || !pickFile) return;
      if (!window.confirm('Отправить на печать файл «' + pickFile + '»?')) return;
      setBusy('print');
      API.adminPrinterPrint(pickFile).then(function (j) {
        app.toast(j && j.ok ? 'Отправлено на печать' : ((j && j.error) || 'Не удалось'));
        load(true);
      }).catch(function () { app.toast('Сеть недоступна'); })
        .then(function () { setBusy(''); });
    }

    // Привязка текущей печати к заказу + подтверждение завершения (OpenTodo 70b5a590).
    function loadOrders() {
      if (busy) return;
      setBusy('orders');
      API.adminOrders().then(function (j) {
        if (j && j.ok) {
          var act = (j.orders || []).filter(function (o) { return ['printed', 'rejected', 'cancelled'].indexOf(o.status) < 0; });
          setOrders(act); if (!pickOrder && act.length) setPickOrder(String(act[0].id));
        } else app.toast((j && j.error) || 'Не удалось получить заказы');
      }).catch(function () { app.toast('Сеть недоступна'); }).then(function () { setBusy(''); });
    }
    function bindOrder() {
      if (busy || !pickOrder) return;
      setBusy('bind');
      API.adminPrinterBind(Number(pickOrder)).then(function (j) {
        app.toast(j && j.ok ? 'Печать привязана к заказу' : ((j && j.error) || 'Не удалось'));
        load(true);
      }).catch(function () { app.toast('Сеть недоступна'); }).then(function () { setBusy(''); });
    }
    function unbindOrder() {
      if (busy) return;
      setBusy('unbind');
      API.adminPrinterUnbind().then(function () { app.toast('Привязка снята'); load(true); })
        .catch(function () { app.toast('Сеть недоступна'); }).then(function () { setBusy(''); });
    }
    function confirmPrint(rid) {
      if (busy) return;
      if (!window.confirm('Печать завершена и всё ок?\n\nЗаказ перейдёт в «Напечатано», клиенту уйдёт таймлапс печати.')) return;
      setBusy('confirm');
      API.adminPrintConfirm(rid, true).then(function (j) {
        app.toast(j && j.ok ? 'Заказ напечатан, клиент уведомлён' : ((j && j.error) || 'Не удалось'));
        load(true);
      }).catch(function () { app.toast('Сеть недоступна'); }).then(function () { setBusy(''); });
    }

    // Выгрузка филамента из слота хаба. tray_id считает сервер (слот−1); подтверждение обязательно.
    function unload(slot) {
      if (busy) return;
      if (!window.confirm('Выгрузить филамент из слота ' + slot + '? Делайте это, когда принтер не печатает.')) return;
      setBusy('unload' + slot);
      API.adminPrinterUnload(slot).then(function (j) {
        app.toast(j && j.ok ? ('Выгрузка слота ' + slot + ' запущена') : ((j && j.error) || 'Не удалось'));
        load(true);
      }).catch(function () { app.toast('Сеть недоступна'); })
        .then(function () { setBusy(''); });
    }

    if (data.loading) return h('div', { className: 'screen' }, h(Lab, null, 'Принтер'), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Опрашиваю принтер…'));
    if (!data.ok) return h('div', { className: 'screen' }, h(Lab, { right: refreshBtn(load) }, 'Принтер'),
      h('div', { className: 'empty' }, icon('printer', { size: 46 }), h('div', null, data.error || 'Принтер недоступен')));

    var stt = data.state || {}, sum = data.summary || {};
    var job = data.job || {};   // «богатая шапка»: данные заказа активной печати (OpenTodo 09e9ee22)
    var prog = gf(stt, 'virtual_sdcard.progress', 'progress') || 0;
    var layer = gf(stt, 'info.current_layer', 'current_layer', 'layer');
    var layers = gf(stt, 'info.total_layer', 'total_layer', 'layers');
    var jobName = gf(stt, 'print_stats.filename', 'filename') || gf(stt, 'plastic') || 'Печать';
    var noz = sum.extruder_temp, nozT = sum.extruder_target, bed = sum.bed_temp, bedT = sum.bed_target;
    var chamber = gf(stt, 'chamber'), speed = gf(stt, 'speed'), fan = gf(stt, 'fan');
    var fUsed = gf(stt, 'filament_used_g'), fTotal = gf(stt, 'filament_total_g');
    var na = '—';
    // Идёт ли печать сейчас — по той же телеметрии, что уже на экране (точного флага паузы у CC2 в
    // снимке нет): есть остаток времени, либо прогресс начат, но не завершён. От этого зависит,
    // показывать «Пауза» или нет, и активна ли кнопка старта файла (нельзя стартовать поверх печати).
    var printing = gf(stt, 'remaining') != null || gf(stt, 'eta_at') != null || (prog > 0 && prog < 1);
    function spark(vals, color) {
      if (!vals || vals.length < 2) return null;
      var max = Math.max.apply(null, vals), min = Math.min.apply(null, vals), rng = (max - min) || 1;
      var n = vals.length, W = 100, H = 22, pad = 2;
      var P = vals.map(function (v, i) {
        return { x: (i / (n - 1)) * W, y: (H - pad) - ((v - min) / rng) * (H - pad * 2) };
      });
      // Гладкая волнистая линия: Catmull-Rom сплайн через точки → кубические безье (без острых углов).
      var d = 'M' + P[0].x.toFixed(1) + ',' + P[0].y.toFixed(1);
      for (var i = 0; i < P.length - 1; i++) {
        var p0 = P[i - 1] || P[i], p1 = P[i], p2 = P[i + 1], p3 = P[i + 2] || p2;
        var c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6;
        var c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6;
        d += ' C' + c1x.toFixed(1) + ',' + c1y.toFixed(1) + ' ' + c2x.toFixed(1) + ',' + c2y.toFixed(1) +
             ' ' + p2.x.toFixed(1) + ',' + p2.y.toFixed(1);
      }
      return h('svg', { className: 'spark', viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none' },
        h('path', { d: d, fill: 'none', stroke: color, strokeWidth: 1.5,
          strokeLinejoin: 'round', strokeLinecap: 'round', vectorEffect: 'non-scaling-stroke' }));
    }
    function tcell(cls, ic, label, val, unit, sub, extra) {
      return h('div', { className: 'tcell' + (cls ? ' ' + cls : '') },
        h('div', { className: 'tl' }, icon(ic, { size: 14 }), ' ', label),
        h('div', { className: 'tv' }, val == null ? na : val, unit && h('small', null, unit)),
        sub && h('div', { className: 'tgt' }, sub),
        extra || null);
    }
    return h('div', { className: 'screen' },
      h('div', { className: 'card' },
        h('div', { className: 'pjob' },
          (job.thumb
            ? h('img', { className: 'th', src: job.thumb, alt: '' })
            : h('div', { className: 'th', style: { display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--accent)' } }, icon('printer', { size: 26 }))),
          h('div', { style: { flex: 1, minWidth: 0 } },
            (gf(stt, 'order_id') || data.serial || gf(stt, 'serial')) && h('div', { className: 'id' },
              gf(stt, 'order_id') ? ('#' + gf(stt, 'order_id')) : null,
              (data.serial || gf(stt, 'serial')) ? h('span', { style: { color: 'var(--faint)', fontWeight: 400, marginLeft: gf(stt, 'order_id') ? 7 : 0 } }, (data.serial || gf(stt, 'serial'))) : null),
            h('div', { className: 'nm' }, jobName),
            gf(stt, 'plastic') && h('div', { style: { marginTop: 4, fontSize: 12, color: 'var(--muted)' } }, gf(stt, 'plastic')),
            (job.grams ? h('div', { style: { marginTop: 3, fontSize: 12, color: 'var(--muted)' } }, '≈ ' + job.grams + ' г филамента') : null)),
          h('span', { className: 'live' }, h('span', { className: 'blip' }), 'LIVE')),
        h('div', { style: { marginTop: 15 } },
          h('div', { className: 'pbar-row' }, h('span', { className: 'pct' }, Math.round(prog * 100) + '%'),
            (layer != null && layers != null) && h('span', { className: 'ly' }, 'слой ' + layer + ' / ' + layers)),
          h('div', { className: 'pbar' }, h('i', { style: { width: (prog * 100) + '%' } })))),

      h(Lab, null, 'Камера'),
      (data.camera_url && !camErr)
        ? h('div', { className: 'cam' },
            h('img', { src: API.cameraSrc(camNonce) + (camMode === 'snap' ? '&mode=snap&t=' + tick : ''),
              alt: '', style: { width: '100%', height: '100%', objectFit: 'cover' }, onError: camImgError }),
            h('span', { className: 'ts' }, new Date().toLocaleTimeString('ru-RU')),
            h('span', { className: 'live' }, h('span', { className: 'blip' }), camMode === 'snap' ? 'LIVE · кадры' : 'LIVE'))
        : h('div', { className: 'cam' }, h('div', { className: 'grain' }), h('span', { className: 'live' }, h('span', { className: 'blip' }), 'LIVE · CAM'),
          h('div', { className: 'reticle' }, icon('printer', { size: 40 }),
            h('div', null, data.camera_url ? 'Камера выключена или недоступна' : 'Видео-поток принтера'),
            camDiag && h('div', { style: { fontSize: 10.5, color: 'var(--faint)', marginTop: 6, maxWidth: 260, wordBreak: 'break-all' } },
              (camDiag.url ? camDiag.url + (camDiag.error ? ' — ' + camDiag.error : ' — OK?') : (camDiag.error || ''))),
            data.camera_url
              ? h('button', { className: 'btn soft', style: { marginTop: 10, width: 'auto', padding: '6px 16px' }, onClick: camRetry }, 'Повторить')
              : h('div', { style: { fontSize: 11, color: 'var(--faint)', marginTop: 6 } }, 'Включите камеру на экране принтера'))),

      h(Lab, null, 'Телеметрия'),
      h('div', { className: 'telem' },
        tcell('hot', 'flame', 'Сопло', noz != null ? Math.round(noz) : null, '°C', nozT != null ? ('цель ' + Math.round(nozT) + '°C') : null, spark(hist.noz, '#ff8d4a')),
        tcell('hot', 'layers', 'Стол', bed != null ? Math.round(bed) : null, '°C', bedT != null ? ('цель ' + Math.round(bedT) + '°C') : null, spark(hist.bed, '#5abef0')),
        tcell('', 'box', 'Камера', chamber != null ? Math.round(chamber) : null, '°C', 'в камере'),
        tcell('', 'bolt', 'Скорость', speed != null ? speed : null, ' мм/с', fan != null ? ('обдув ' + fan + '%') : null)),

      (fUsed != null && fTotal != null) && h('div', null,
        h(Lab, null, 'Филамент на модель'),
        h('div', { className: 'card' },
          h('div', { className: 'spread', style: { marginBottom: 9 } }, h('span', { style: { fontSize: 13, color: 'var(--muted)' } }, 'Израсходовано'), h('span', { className: 'mono', style: { fontWeight: 600 } }, fUsed + ' / ' + fTotal + ' г')),
          h('div', { className: 'pbar' }, h('i', { style: { width: (fUsed / fTotal * 100) + '%' } })))),

      (gf(stt, 'started_at') || gf(stt, 'eta_at')) && h('div', null,
        h(Lab, null, 'Время печати'),
        h('div', { className: 'card' },
          h('div', { className: 'sched' },
            h('div', { className: 'pt' }, h('div', { className: 'lb' }, icon('clock', { size: 12 }), ' Начало'), h('div', { className: 'vl' }, gf(stt, 'started_at') || na)),
            h('div', { className: 'line' }),
            h('div', { className: 'pt end' }, h('div', { className: 'lb' }, 'Окончание (прогноз)'), h('div', { className: 'vl' }, gf(stt, 'eta_at') || na))),
          (gf(stt, 'elapsed') || gf(stt, 'remaining')) && h('div', null, h('div', { className: 'div' }),
            h('div', { className: 'spread', style: { fontSize: 12.5 } },
              h('span', { style: { color: 'var(--muted)' } }, 'Прошло ', h('b', { className: 'mono', style: { color: 'var(--text)' } }, gf(stt, 'elapsed') || na)),
              h('span', { style: { color: 'var(--muted)' } }, 'Осталось ', h('b', { className: 'mono', style: { color: 'var(--accent)' } }, '~' + (gf(stt, 'remaining') || na))))))),

      h('div', { className: 'pctrl' },
        printing
          ? h('button', { disabled: !!busy, onClick: function () { control('pause', { okText: 'Печать на паузе' }); } }, icon('clock'), busy === 'pause' ? '…' : ' Пауза')
          : h('button', { disabled: !!busy, onClick: function () { control('resume', { okText: 'Печать продолжена' }); } }, icon('clock'), busy === 'resume' ? '…' : ' Продолжить'),
        h('button', { disabled: !!busy, onClick: function () { control('light', { on: !lightOn, okText: lightOn ? 'Свет выключен' : 'Свет включён' }); } }, icon('bolt'), busy === 'light' ? '…' : (lightOn ? ' Свет выкл' : ' Свет вкл')),
        h('button', { className: 'danger', disabled: !!busy, onClick: onStop }, icon('close'), busy === 'stop' ? '…' : ' Стоп')),

      h(Lab, null, 'Заказ печати'),
      h('div', { className: 'card' },
        data.bound
          ? h('div', null,
              h('div', { className: 'spread' },
                h('div', null, h('span', { className: 'id' }, '#', data.bound.rid), ' ', h('b', null, data.bound.label)),
                h('span', { className: 'stbadge st-progress', style: { marginTop: 0 } }, data.bound.status_label)),
              h('div', { className: 'grid2', style: { gap: 9, marginTop: 11 } },
                h('button', { className: 'btn line', style: { marginTop: 0 }, disabled: !!busy, onClick: unbindOrder }, busy === 'unbind' ? '…' : ' Отвязать'),
                h('button', { className: 'btn', style: { marginTop: 0 }, disabled: !!busy, onClick: function () { confirmPrint(data.bound.rid); } }, icon('check', { size: 16 }), busy === 'confirm' ? ' …' : ' Завершено')),
              h('p', { className: 'hint', style: { marginTop: 8, marginBottom: 0 } }, 'Когда печать закончится, бот сам пришлёт подтверждение. Можно подтвердить и здесь — заказ станет «Напечатано», клиенту уйдёт таймлапс.'))
          : h('div', null,
              h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginBottom: 9 } }, 'Свяжите текущую печать с заказом — по завершении бот предложит отметить «Напечатано» и отправит клиенту таймлапс печати.'),
              orders == null
                ? h('button', { className: 'btn line', style: { marginTop: 0 }, disabled: busy === 'orders', onClick: loadOrders }, icon('receipt', { size: 16 }), busy === 'orders' ? ' Загружаю…' : ' Выбрать заказ')
                : orders.length === 0
                  ? h('div', { style: { color: 'var(--faint)', fontSize: 13 } }, 'Активных заказов нет.')
                  : h('div', null,
                      h('select', { className: 'input', style: { marginTop: 0 }, value: pickOrder, onChange: function (e) { setPickOrder(e.target.value); } },
                        orders.map(function (o) { return h('option', { key: o.id, value: o.id }, '#' + o.id + ' · ' + o.label); })),
                      h('button', { className: 'btn', style: { marginTop: 9 }, disabled: !!busy || !pickOrder, onClick: bindOrder }, icon('check', { size: 16 }), busy === 'bind' ? ' Привязываю…' : ' Привязать к печати')))),

      h(Lab, null, 'Филамент'),
      h('div', { className: 'card' },
        h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginBottom: 9 } },
          printing ? 'Выгрузка недоступна во время печати.' : 'Выгрузка филамента из слота хаба (1–4).'),
        h('div', { className: 'grid2', style: { gap: 9 } },
          [1, 2, 3, 4].map(function (n) {
            return h('button', { key: n, className: 'btn line', style: { marginTop: 0 },
              disabled: !!busy || printing, onClick: function () { unload(n); } },
              icon('spool', { size: 16 }), busy === ('unload' + n) ? ' …' : (' Выгрузить ' + n));
          }))),

      h(Lab, null, 'Старт печати файла'),
      h('div', { className: 'card' },
        files == null
          ? h('button', { className: 'btn line', style: { marginTop: 0 }, disabled: busy === 'files', onClick: loadFiles },
              icon('folder', { size: 17 }), busy === 'files' ? ' Загружаю…' : ' Файлы на принтере')
          : files.length === 0
            ? h('div', { style: { color: 'var(--faint)', fontSize: 13, padding: '4px 2px' } }, 'На принтере нет файлов для печати.')
            : h('div', null,
                h('select', { className: 'input', style: { marginTop: 0 }, value: pickFile, onChange: function (e) { setPickFile(e.target.value); } },
                  files.map(function (f) {
                    var t = f.print_time ? ' · ' + API.human(f.print_time / 60) : '';
                    return h('option', { key: f.name, value: f.name }, f.name + t);
                  })),
                h('div', { className: 'grid2', style: { gap: 9, marginTop: 9 } },
                  h('button', { className: 'btn line', style: { marginTop: 0 }, disabled: busy === 'files', onClick: loadFiles }, icon('refresh', { size: 16 }), ' Обновить'),
                  h('button', { className: 'btn', style: { marginTop: 0 }, disabled: !!busy || !pickFile, onClick: startFile }, icon('printer', { size: 17 }), busy === 'print' ? ' Отправляю…' : ' Печатать'))),
        h('p', { className: 'hint', style: { marginTop: 9, marginBottom: 0 } }, 'Запустит печать файла из памяти принтера. Убедитесь, что заправлен нужный филамент и стол свободен.')));
  }

  // ─────────────────────── ADMIN · обслуживание (ТО) ───────────────────────
  function AdminService(props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    var ed = useState(null); var edit = ed[0], setEdit = ed[1];
    var nf = useState(null); var nform = nf[0], setNForm = nf[1];
    function load() { API.adminService().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    if (!data) return h('div', { className: 'screen' }, h(Lab, null, 'Обслуживание'), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…'));
    var DOT = { green: '#2dd47e', amber: '#ffb020', red: '#ff5b4d' };
    var UNIT = { hours: 'ч', prints: 'печатей', days: 'дн' };
    function act(b) { return API.adminServiceAction(b).then(function (j) { if (j.ok) load(); else app.toast(j.error || 'Ошибка'); return j; }); }
    function done(it) { if (window.confirm('Отметить «' + it.label + '» обслуженным? Счётчики обнулятся.')) act({ action: 'done', id: it.id }); }
    function cycleType(it) { var o = ['hours', 'prints', 'days']; act({ action: 'type', id: it.id, trigger_type: o[(o.indexOf(it.trigger) + 1) % 3] }); }
    function saveThr() { act({ action: 'threshold', id: edit.id, threshold: edit.val }).then(function () { setEdit(null); }); }
    function addItem() { if (!nform.label.trim()) { app.toast('Введите название'); return; } act({ action: 'add', label: nform.label, trigger_type: nform.trigger_type, threshold: nform.threshold }).then(function () { setNForm(null); }); }
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(load) }, 'Обслуживание'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Наработка считается по выполненным печатям. 🔴 пора · 🟡 скоро (' + Math.round(data.warn_pct) + '%) · 🟢 норма.'),
      (data.items || []).map(function (it) {
        return h('div', { key: it.id, className: 'card', style: { marginTop: 10, opacity: it.is_active ? 1 : 0.5 } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 9 } },
            h('span', { style: { width: 11, height: 11, borderRadius: '50%', background: DOT[it.status] || DOT.green, flex: '0 0 auto' } }),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('b', { style: { fontSize: 14 } }, it.label),
              h('div', { className: 'mono', style: { fontSize: 12, color: 'var(--muted)', marginTop: 2 } },
                Math.round(it.current) + ' / ' + (it.threshold || 0) + ' ' + (UNIT[it.trigger] || '') + ' · ' + Math.round(it.ratio * 100) + '%'))),
          h('div', { className: 'pbar', style: { marginTop: 8 } }, h('i', { style: { width: Math.min(100, it.ratio * 100) + '%', background: DOT[it.status] } })),
          edit && edit.id === it.id
            ? h('div', { style: { display: 'flex', gap: 8, marginTop: 9 } },
                h('input', { className: 'input mono', style: { flex: 1, marginTop: 0 }, inputMode: 'decimal', value: edit.val, onChange: function (e) { setEdit({ id: it.id, val: e.target.value }); } }),
                h('button', { className: 'btn', style: { marginTop: 0, width: 'auto', padding: '0 14px' }, onClick: saveThr }, 'OK'),
                h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 12px' }, onClick: function () { setEdit(null); } }, '×'))
            : h('div', { style: { display: 'flex', gap: 8, marginTop: 9, flexWrap: 'wrap' } },
                h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 14px' }, onClick: function () { done(it); } }, icon('check', { size: 16 }), ' Обслужено'),
                h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 12px' }, onClick: function () { setEdit({ id: it.id, val: it.threshold }); } }, 'Порог'),
                h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 12px' }, onClick: function () { cycleType(it); } }, UNIT[it.trigger] || it.trigger),
                h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 12px' }, onClick: function () { act({ action: 'toggle', id: it.id }); } }, it.is_active ? 'Выкл' : 'Вкл'),
                h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 12px', color: 'var(--red)' }, onClick: function () { if (window.confirm('Удалить пункт «' + it.label + '»?')) act({ action: 'del', id: it.id }); } }, icon('trash', { size: 16 }))));
      }),
      nform
        ? h('div', { className: 'card', style: { marginTop: 12 } },
            h('input', { className: 'input', placeholder: 'Название узла', value: nform.label, onChange: function (e) { setNForm(Object.assign({}, nform, { label: e.target.value })); } }),
            h('div', { style: { display: 'flex', gap: 8, marginTop: 8 } },
              h('select', { className: 'input', style: { flex: 1, marginTop: 0 }, value: nform.trigger_type, onChange: function (e) { setNForm(Object.assign({}, nform, { trigger_type: e.target.value })); } },
                h('option', { value: 'hours' }, 'часы'), h('option', { value: 'prints' }, 'печати'), h('option', { value: 'days' }, 'дни')),
              h('input', { className: 'input mono', style: { flex: 1, marginTop: 0 }, inputMode: 'decimal', placeholder: 'порог', value: nform.threshold, onChange: function (e) { setNForm(Object.assign({}, nform, { threshold: e.target.value })); } })),
            h('div', { style: { display: 'flex', gap: 8, marginTop: 8 } },
              h('button', { className: 'btn', style: { marginTop: 0 }, onClick: addItem }, 'Добавить'),
              h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { setNForm(null); } }, 'Отмена')))
        : h('button', { className: 'btn line', style: { marginTop: 12 }, onClick: function () { setNForm({ label: '', trigger_type: 'hours', threshold: '' }); } }, '+ Добавить узел'));
  }

  // ─────────────────────── ADMIN · печать для себя ───────────────────────
  function AdminSelfPrint(props) {
    var app = props.app;
    var ps = useState(null); var plastics = ps[0], setPlastics = ps[1];
    var fs = useState({ plastic_id: '', grams: '', minutes: '', label: '' }); var form = fs[0], setForm = fs[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    // Отдельный список «печать для себя» — заказы-самопечати мастера, исключённые из клиентских
    // заказов. Заводятся авто-классификацией завершённой печати (printwatch → create_self_print_order).
    var os = useState(null); var selfOrders = os[0], setSelfOrders = os[1];
    function loadSelfOrders() { API.adminSelfOrders().then(function (j) { if (j && j.ok) setSelfOrders(j); }); }
    useEffect(function () {
      loadSelfOrders();
      API.adminPlastics().then(function (j) {
        if (!j.ok) return;
        var act = (j.plastics || []).filter(function (p) { return p.is_active; });
        setPlastics(act);
        if (act[0]) setForm(function (ff) { return Object.assign({}, ff, { plastic_id: act[0].id }); });
      });
    }, []);
    function submit() {
      if (!form.plastic_id) { app.toast('Выберите пластик'); return; }
      if (!(Number(form.grams) > 0)) { app.toast('Укажите граммы'); return; }
      setBusy(true);
      API.adminSelfPrint(form).then(function (j) {
        app.toast(j.ok ? ('Записано: −' + form.grams + ' г со склада') : (j.error || 'Ошибка'));
        if (j.ok) setForm(Object.assign({}, form, { grams: '', minutes: '', label: '' }));
      }).catch(function () { app.toast('Сеть недоступна'); }).then(function () { setBusy(false); });
    }
    return h('div', { className: 'screen' },
      h(Lab, null, 'Печать для себя'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Внутренняя печать (не заказ клиента): спишет филамент со склада и засчитает наработку в ТО, но не попадёт в клиентскую статистику.'),
      h('div', { className: 'card' },
        h('label', { style: { fontSize: 12.5, color: 'var(--muted)' } }, 'Пластик'),
        h('select', { className: 'input', style: { marginTop: 4 }, value: form.plastic_id, onChange: function (e) { setForm(Object.assign({}, form, { plastic_id: e.target.value })); } },
          (plastics || []).map(function (p) { return h('option', { key: p.id, value: p.id }, p.name + ' · ' + Math.round(p.stock || 0) + ' г'); })),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 9 } },
          h('div', { style: { flex: 1 } },
            h('label', { style: { fontSize: 12.5, color: 'var(--muted)' } }, 'Граммы'),
            h('input', { className: 'input mono', style: { marginTop: 4 }, inputMode: 'decimal', value: form.grams, onChange: function (e) { setForm(Object.assign({}, form, { grams: e.target.value })); } })),
          h('div', { style: { flex: 1 } },
            h('label', { style: { fontSize: 12.5, color: 'var(--muted)' } }, 'Минуты (необяз.)'),
            h('input', { className: 'input mono', style: { marginTop: 4 }, inputMode: 'decimal', value: form.minutes, onChange: function (e) { setForm(Object.assign({}, form, { minutes: e.target.value })); } }))),
        h('label', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 9, display: 'block' } }, 'Название (необяз.)'),
        h('input', { className: 'input', style: { marginTop: 4 }, placeholder: 'Печать для себя', value: form.label, onChange: function (e) { setForm(Object.assign({}, form, { label: e.target.value })); } }),
        h('button', { className: 'btn', style: { marginTop: 12 }, disabled: busy, onClick: submit }, icon('printer', { size: 17 }), busy ? ' Записываю…' : ' Записать печать')),
      // Список самопечатей мастера (заказы status='self' / заказчик-админ) — отдельно от клиентских
      // заказов. Тап открывает ту же карточку заказа, что и клиентские (app.openAdminOrder).
      h(Lab, { right: refreshBtn(loadSelfOrders) }, 'Мои самопечати'),
      selfOrders === null && h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…'),
      selfOrders && (selfOrders.orders || []).length === 0 && h('p', { className: 'hint', style: { marginTop: 0 } }, 'Пока пусто — завершённые печати «для себя» появятся здесь.'),
      ((selfOrders && selfOrders.orders) || []).map(function (o) {
        var when = o.created_at ? new Date(o.created_at * 1000).toLocaleDateString('ru') : '';
        return h('div', { key: o.id, className: 'order', style: { cursor: 'pointer' }, onClick: function () { app.openAdminOrder(o.id); } },
          h('div', { className: 'spread' },
            h('div', { style: { minWidth: 0 } }, h('span', { className: 'id' }, '#', o.id), ' ', h('b', { style: { fontSize: 14.5 } }, o.label)),
            icon('chev', { size: 16, style: { color: 'var(--faint)' } })),
          h('div', { style: { marginTop: 7, display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' } },
            h('span', { className: 'stbadge ' + (ST_CLASS[o.status] || 'st-printed'), style: { marginTop: 0 } }, o.status_label),
            h('span', { style: { fontSize: 12.5, color: 'var(--muted)' } },
              (o.grams ? (Math.round(o.grams * 10) / 10) + ' г' : '—')
              + (o.minutes ? ' · ' + Math.round(o.minutes) + ' мин' : '')
              + (when ? ' · ' + when : '')),
            h('span', { className: 'mono', style: { marginLeft: 'auto', fontWeight: 600, color: 'var(--price)' } }, money(o.price, (selfOrders && selfOrders.currency) || 'руб.'))));
      }));
  }

  Object.assign(window, { Admin: Admin, AdminMore: AdminMore, AdminDash: AdminDash, AdminOrders: AdminOrders, AdminOrderDetail: AdminOrderDetail, AdminQueue: AdminQueue, AdminStatus: AdminStatus, AdminPrinter: AdminPrinter, AdminService: AdminService, AdminSelfPrint: AdminSelfPrint, AdminCalcQueue: AdminCalcQueue, AdminJournal: AdminJournal });
})();
