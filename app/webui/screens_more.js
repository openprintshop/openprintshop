// screens_more.js — Orders, Cart, Collection, MyModels, Catalog, Help, Invite, Chat
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, Lab = window.PS_Lab, API = window.API, Breakdown = window.PS_Breakdown;

  var ST_CLASS = { new: 'st-new', awaiting_payment: 'st-progress', in_progress: 'st-progress', printing: 'st-progress',
    price_changed: 'st-progress', invoiced: 'st-progress', printed: 'st-printed', done: 'st-printed',
    rejected: 'st-rejected', cancelled: 'st-rejected', handed: 'st-printed',
    // отложенный заказ жив — красить его как отказ нельзя, это разные новости для клиента
    deferred: 'st-new' };
  var ST_ICON = { new: 'sparkle', awaiting_payment: 'card', in_progress: 'flame', printing: 'flame', price_changed: 'coin',
    invoiced: 'receipt', printed: 'check', done: 'check', rejected: 'close', cancelled: 'close',
    handed: 'check', deferred: 'clock' };

  function refreshBtn(fn) {
    return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: fn }, icon('refresh', { size: 14 }), ' Обновить');
  }

  // ───────────────────────── ORDERS ─────────────────────────
  // Редактор параметров заказа до взятия в работу (OpenTodo 42ce4724): пластик/цвет/качество/
  // прочность/тираж берём из app.options (как на экране параметров), пересчёт — POST /api/order/edit.
  function OrderEditor(props) {
    var o = props.o, app = props.app, opt = app.options || {};
    var plastics = opt.plastics || [], quals = opt.qualities || [], strs = opt.strengths || [];
    function pick(list, id) {
      return (list.filter(function (x) { return x.id === id; })[0]
        || list.filter(function (x) { return x.is_default; })[0] || list[0] || {});
    }
    var initPl = (plastics.filter(function (p) { return o.plastic && o.plastic.indexOf(p.name) === 0; })[0]
      || plastics.filter(function (p) { return p.is_default; })[0] || plastics[0] || {});
    var fs = useState({ plastic_id: initPl.id || 0, quality_id: pick(quals, o.quality_id).id || 0,
      strength_id: pick(strs, o.strength_id).id || 0, qty: o.qty || 1, color: '' });
    var f = fs[0], setF = fs[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    function set(k, v) { var n = {}; n[k] = v; if (k === 'plastic_id') n.color = ''; setF(Object.assign({}, f, n)); }
    var curPl = plastics.filter(function (p) { return p.id === f.plastic_id; })[0] || {};
    var colors = curPl.colors || [];
    function save() { setBusy(true); app.editOrder(o.id, f).then(function () { setBusy(false); if (props.onDone) props.onDone(); }); }
    function sel(label, value, onChange, options) {
      return h('label', { style: { display: 'block', marginTop: 9 } },
        h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600, margin: '0 2px 4px' } }, label),
        h('select', { className: 'input', value: value, onChange: onChange }, options));
    }
    return h('div', { className: 'card', style: { marginTop: 10, padding: 13 } },
      sel('Пластик', f.plastic_id, function (e) { set('plastic_id', parseInt(e.target.value, 10)); },
        plastics.map(function (p) { return h('option', { key: p.id, value: p.id }, p.name); })),
      colors.length > 1 && sel('Цвет', f.color, function (e) { set('color', e.target.value); },
        [h('option', { key: '_', value: '' }, '— любой —')].concat(colors.map(function (c) { return h('option', { key: c, value: c }, c); }))),
      // Качество/прочность клиент больше не выбирает (8c8199d1): в пересчёт уходят прежние
      // id из заказа (fs выше), профиль печати остаётся на мастере.
      h('label', { style: { display: 'block', marginTop: 9 } },
        h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600, margin: '0 2px 4px' } }, 'Количество'),
        h('input', { className: 'input mono', inputMode: 'numeric', value: f.qty, onChange: function (e) { set('qty', parseInt(e.target.value, 10) || 1); } })),
      h('button', { className: 'btn', style: { marginTop: 12 }, disabled: busy, onClick: save },
        icon('refresh', { size: 17 }), busy ? ' Пересчитываю…' : ' Пересчитать и сохранить'),
      h('p', { className: 'hint', style: { marginTop: 7 } }, 'Цена пересчитается по новым параметрам. Менять можно, пока заказ не взят в работу.'));
  }

  // Статус-трекер «как у доставки еды» (ux-simplification.md, идея 5):
  // в очереди → печатается (живой флаг с принтера) → готово. Для отказов/отмен не показываем.
  function StageTracker(props) {
    var o = props.o;
    // «Отложен» тоже без трекера: заказ вне очереди, и «в очереди · 3-й» было бы враньём.
    if (o.status === 'rejected' || o.status === 'cancelled' || o.status === 'deferred' || o.custom) return null;
    var done = (o.status === 'printed' || o.status === 'done');
    var stage = done ? 2 : (o.printing ? 1 : 0);
    var labels = [o.qpos ? ('в очереди · ' + o.qpos + '-й') : 'в очереди',
                  o.printing ? 'печатается сейчас' : 'печать', 'готово'];
    return h('div', { className: 'strack' }, labels.map(function (lb, i) {
      var cls = i < stage || (done && i === 2) ? ' done' : (i === stage ? ' cur' : '');
      return h('span', { key: i, className: 'st' + cls }, h('span', { className: 'dot' }), lb);
    }));
  }

  // История статусов с метками времени (идея #2). Бэкенд шлёт o.timeline = [{label, at:"DD.MM HH:MM"}].
  // Сворачиваемо, чтобы не загромождать карточку; последний пункт подсвечен акцентом.
  function OrderTimeline(props) {
    var items = props.items || [];
    // короткий таймлайн (≤3) раскрыт сразу: карточка уже развёрнута тапом, второй тап лишний
    var op = useState(items.length <= 3); var open = op[0], setOpen = op[1];
    if (items.length < 2) return null;   // одна запись = только создание, таймлайн не нужен
    return h('div', { style: { marginTop: 8 } },
      h('button', { type: 'button', onClick: function () { setOpen(!open); },
        style: { display: 'inline-flex', alignItems: 'center', gap: 6, background: 'transparent',
          border: 0, padding: 0, cursor: 'pointer', color: 'var(--muted)', fontSize: 12.5, fontWeight: 600 } },
        icon('cal', { size: 14 }), open ? 'Скрыть историю' : 'История статусов',
        icon('chev', { size: 13, style: { transform: open ? 'rotate(-90deg)' : 'rotate(90deg)', transition: 'transform .15s' } })),
      open && h('div', { style: { marginTop: 8, paddingLeft: 3 } }, items.map(function (it, i) {
        var last = i === items.length - 1;
        return h('div', { key: i, style: { display: 'flex', gap: 9, alignItems: 'baseline', padding: '3px 0', fontSize: 12.5 } },
          h('span', { style: { flex: '0 0 auto', width: 7, height: 7, borderRadius: '50%', alignSelf: 'center',
            background: last ? 'var(--accent)' : 'var(--faint)' } }),
          h('span', { style: { color: 'var(--text)', fontWeight: last ? 700 : 500 } }, it.label),
          h('span', { style: { color: 'var(--faint)', marginLeft: 'auto', fontFamily: 'var(--mono, monospace)' } }, it.at));
      })));
  }

  function OrderCard(props) {
    var o = props.o, app = props.app;
    var sb = useState(false); var showBd = sb[0], setShowBd = sb[1];   // раскрытие расчётника
    var es = useState(false); var editing = es[0], setEditing = es[1];   // редактор параметров (42ce4724)
    var zs = useState(''); var zoom = zs[0], setZoom = zs[1];   // рендер/скрин на весь экран (1a519e1c)
    // Карточки свёрнуты по умолчанию (аудит, проблема #9): заголовок+статус+цена; детали по
    // тапу. Заказ, ждущий ответа по цене, — развёрнут сразу (там кнопки Принять/Отказаться).
    var xs = useState(!!o.needs_response); var expanded = xs[0], setExpanded = xs[1];
    var head = h('div', { className: 'top', style: { cursor: 'pointer' }, onClick: function () { setExpanded(!expanded); } },
      o.preview ? h('img', { className: 'th', src: o.preview, alt: '', style: { cursor: 'zoom-in' },
        onClick: function (e) { e.stopPropagation(); setZoom(o.preview); } }) : h('div', { className: 'th' }),
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('span', { className: 'id' }, '#', o.id),
        h('div', { className: 'nm' }, o.label),
        h('div', null, h('span', { className: 'stbadge ' + (ST_CLASS[o.status] || 'st-new') }, icon(ST_ICON[o.status] || 'sparkle', { size: 12 }), ' ', o.status_label),
          o.status === 'in_progress' && o.due && h('span', { className: 'stbadge st-progress', style: { marginLeft: 6 } }, icon('cal', { size: 12 }), ' к ', o.due),
          o.unread > 0 && h('span', { className: 'stbadge st-new', style: { marginLeft: 6 } }, '💬 ', o.unread)),
        h('div', { className: 'sub' }, o.plastic, ' · ', o.qty, ' шт'),
        expanded && h('div', { className: 'sub' }, o.dims && h('span', null, h('span', { className: 'mono' }, o.dims), ' мм · '), h('span', { className: 'mono' }, o.grams), ' · ', h('span', { className: 'mono' }, o.time))),
      h('span', { style: { alignSelf: 'center', color: 'var(--faint)', flex: '0 0 auto', display: 'flex' } },
        icon('chev', { size: 17, style: { transform: expanded ? 'rotate(-90deg)' : 'rotate(90deg)', transition: 'transform .15s' } })));
    var lightbox = h(window.PS_Lightbox, { src: zoom, name: zoom ? ('print-studio-order-' + o.id + '.png') : '',
      onClose: function () { setZoom(''); } });
    // печати заказа из журнала: таймлапсы и скрины стола per-печать (o.prints), одиночный
    // o.timelapse остаётся фолбэком для заказов без журнальных записей
    var prints = o.prints || [];
    var tls = prints.filter(function (p) { return p.timelapse; });
    if (!tls.length && o.timelapse) tls = [{ timelapse: o.timelapse }];
    var shots = prints.filter(function (p) { return p.photo; });
    // Онлайн-оплата (A1): бейдж «оплачено» виден и в свёрнутой карточке — клиенту важно
    // сразу понимать, рассчитался он уже или нет.
    var paidTag = o.paid ? h('span', { className: 'tag', style: { color: 'var(--price)', borderColor: 'var(--price-soft)' } }, icon('card'), ' оплачено') : null;
    if (!expanded) {
      return h('div', { className: 'order' }, head,
        h('div', { className: 'chiprow' },
          h('span', { className: 'tag' }, icon('coin'), ' ', o.price),
          paidTag,
          // самое ценное для взгляда «как там заказ» — очередь/этап, видно без разворота
          o.qpos ? h('span', { className: 'tag warm' }, icon('clock'), ' ', o.qpos + '-й из ' + o.qtotal + (o.qeta ? ' · к ' + o.qeta : ''))
            : o.printing ? h('span', { className: 'tag', style: { color: 'var(--price)', borderColor: 'var(--price-soft)' } }, icon('printer'), ' печатается') : null,
          // Отправление (A2) видно и в свёрнутой карточке: «уже выехало?» — первый вопрос клиента
          (o.track && (o.track.number || o.track.shipped_at))
            ? h('span', { className: 'tag' }, '📮 ', (o.track.carrier_name || 'отправлено')) : null),
        lightbox);
    }
    return h('div', { className: 'order' },
      head,
      h(StageTracker, { o: o }),
      h(OrderTimeline, { items: o.timeline }),
      // Доставка одиночного заказа (A2): адрес из умолчаний профиля, стоимость и трек-номер.
      // У заказа без доставки блок пустой и ничего не рисует.
      o.delivery_method ? h('div', { style: { marginTop: 8 } }, deliveryLines(o)) : null,
      o.needs_response && h('div', { className: 'card', style: { marginTop: 10, padding: 12, background: 'var(--amber-soft)', borderColor: 'var(--amber-soft)' } },
        h('div', { style: { fontSize: 13, color: 'var(--amber)', fontWeight: 600 } },
          // первая цена заказа (досчитал ПК / назвал мастер при «оценит мастер») — не «изменил»
          (o.status === 'invoiced' ? 'Мастер выставил счёт: ' : (o.first_price ? 'Цена посчитана: ' : 'Мастер изменил цену: ')), o.pending),
        o.comment && h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 4 } }, o.comment),
        o.status === 'invoiced' && h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 4 } }, 'Разверните «Из чего складывается цена» — там полный расчёт.'),
        h('div', { className: 'grid2', style: { gap: 8, marginTop: 10 } },
          // pending_minor — сумма, которую клиент видит: сервер не примет цену, если её успели поменять
          h('button', { className: 'btn', style: { marginTop: 0, padding: 11 }, onClick: function () { app.respondPrice(o.id, true, o.pending_minor); } }, icon('check', { size: 16 }), ' Принять'),
          h('button', { className: 'btn line', style: { marginTop: 0, padding: 11 }, onClick: function () { app.respondPrice(o.id, false); } }, icon('close', { size: 16 }), ' Отказаться'))),
      // цену считает ПК (очередь досчёта, Э0): клиенту честно, чего ждать и откуда придёт цена
      o.price_pending && o.price_note && h('div', { className: 'hint', style: { marginTop: 10 } }, '⏳ ', o.price_note, '.'),
      o.slicer_shot && h('div', { style: { marginTop: 10 } },
        h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600, margin: '0 2px 5px' } }, 'Скрин из слайсера'),
        h('img', { src: o.slicer_shot, alt: '', style: { width: '100%', borderRadius: 12, background: 'var(--bg-2)', cursor: 'zoom-in' },
          onClick: function () { setZoom(o.slicer_shot); } })),
      // Медиа всех печатей заказа (многостольный печатается в несколько заходов): скрины стола
      // галереей + таймлапс на каждую печать. Одиночный o.timelapse — фолбэк для старых заказов.
      shots.length > 0 && h('div', { style: { marginTop: 10 } },
        h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600, margin: '0 2px 5px' } },
          '📸 Готовая печать на столе' + (shots.length > 1 ? ' (' + shots.length + ')' : '')),
        h('div', { style: { display: 'grid', gridTemplateColumns: shots.length > 1 ? '1fr 1fr' : '1fr', gap: 6 } },
          shots.map(function (p, i) {
            return h('img', { key: i, src: p.photo, alt: '', loading: 'lazy',
              style: { width: '100%', borderRadius: 12, background: 'var(--bg-2)', cursor: 'zoom-in' },
              onClick: function () { setZoom(p.photo); } });
          }))),
      tls.map(function (p, i) {
        return h('div', { key: i, style: { marginTop: 10 } },
          h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600, margin: '0 2px 5px' } },
            '🎬 Таймлапс печати' + (tls.length > 1 ? ' ' + (i + 1) + ' из ' + tls.length : '')),
          h('video', { src: p.timelapse, controls: true, playsInline: true, preload: 'metadata', style: { width: '100%', borderRadius: 12, background: '#000' } }));
      }),
      (o.qpos ? h('div', { className: 'chiprow' },
        h('span', { className: 'tag warm', style: { gap: 8 } },
          h('span', { className: 'queue' }, Array.from({ length: o.qtotal || 0 }).map(function (_, i) {
            return h('span', { key: i, className: 'qd' + (i + 1 === o.qpos ? ' me' : (i + 1 < o.qpos ? ' done' : '')) });
          })),
          o.qpos + '-й из ' + o.qtotal + (o.qeta ? ' · к ' + o.qeta : '')),
        h('span', { className: 'tag' }, icon('coin'), ' ', o.price), paidTag)
        : h('div', { className: 'chiprow' }, h('span', { className: 'tag' }, icon('coin'), ' ', o.price), paidTag)),
      // Онлайн-оплата (A1): заметная кнопка «Оплатить» — счёт откроется нативным окном
      // Telegram Payments поверх приложения (app.payOrder → openInvoice)
      o.payable && h('button', { className: 'btn', style: { marginTop: 10, padding: 12 },
        onClick: function () { app.payOrder(o.id); } }, icon('card', { size: 16 }), ' Оплатить ', o.price),
      o.breakdown && o.breakdown.rows && h('button', { onClick: function () { setShowBd(!showBd); },
        style: { marginTop: 9, display: 'inline-flex', alignItems: 'center', gap: 6, background: 'transparent',
          border: 0, padding: 0, cursor: 'pointer', color: 'var(--muted)', fontSize: 12.5, fontWeight: 600 } },
        icon('receipt', { size: 14 }), showBd ? 'Скрыть расчётник' : 'Из чего складывается цена',
        icon('chev', { size: 13, style: { transform: showBd ? 'rotate(-90deg)' : 'rotate(90deg)', transition: 'transform .15s' } })),
      showBd && o.breakdown && h(Breakdown, { bd: o.breakdown, title: false, gap: 8 }),
      // Нижний ряд действий. «Повторить» — primary и доступна на каждом повторяемом заказе
      // (единственное её место теперь здесь: с Главной кнопку убрали). «Смета» → PDF заказа.
      h('div', { className: 'acts' },
        h('button', { onClick: function () { app.openChat(o.id, o.label); } }, icon('chat'), ' Чат', o.unread > 0 && h('span', { style: { marginLeft: 4, color: 'var(--accent)', fontWeight: 700 } }, '•', o.unread)),
        o.breakdown && h('button', { onClick: function () { app.orderPdf(o.id); } }, icon('doc'), ' Смета'),
        o.editable && h('button', { onClick: function () { setEditing(!editing); } }, icon('pencil'), editing ? ' Свернуть' : ' Изменить'),
        o.cancellable && h('button', { className: 'warn', onClick: function () { app.cancelOrder(o.id); } }, icon('trash'), ' Отменить'),
        o.repeatable && h('button', { className: 'primary', onClick: function () { app.useModel('order', o.id); } }, icon('repeat'), ' Повторить')),
      editing && h(OrderEditor, { o: o, app: app, onDone: function () { setEditing(false); } }),
      lightbox);
  }

  // Способ получения (B5) → человекочитаемая подпись в карточке заказа.
  var DELIVERY_LABELS = { pickup: '🏬 Самовывоз', delivery: '🚚 Доставка' };

  // Доставка и отправление в карточке заказа (A2) — ОДИН подкомпонент на два места: карточку
  // заказа-корзины и обычную карточку заказа (одиночный заказ тоже может ехать с доставкой,
  // если клиент задал адрес в профиле). Пусто, когда способ получения не выбирали.
  function deliveryLines(d) {
    var track = d.track || {};
    var dline = d.delivery_method ? (DELIVERY_LABELS[d.delivery_method] || '')
      + (d.delivery_type ? ' · ' + d.delivery_type : '')
      + (d.delivery_method === 'delivery' && d.delivery_address ? ' · ' + d.delivery_address : '') : '';
    return [
      dline && h('div', { key: 'd', className: 'sub' }, dline),
      // Платная доставка: показываем, из чего сложился итог, — иначе сумма на карточке
      // не сходится с ценой позиций и выглядит ошибкой.
      d.delivery_cost && h('div', { key: 'c', className: 'sub' },
        'Позиции ', d.items_total, ' + доставка ', d.delivery_cost),
      // Отправление: номер и ссылка отслеживания там же, где клиент смотрит заказ.
      (track.number || track.shipped_at) && h('div', { key: 't', className: 'sub' }, '📮 ',
        track.carrier_name || 'Отправлено',
        track.number ? h('b', { className: 'mono', style: { marginLeft: 4 } }, track.number) : null,
        track.url ? h('a', { href: track.url, target: '_blank', rel: 'noopener',
          style: { marginLeft: 8, color: 'var(--accent)' } }, 'отследить') : null),
    ];
  }

  // Карточка заказа-корзины (B5): шапка группы (общий итог, доставка, N позиций, смешанные
  // статусы rollup'ом) + вложенные карточки позиций. Позиции — обычные OrderCard: чат, смета,
  // статус, действия остаются ПЕР-ПОЗИЦИОННЫМИ (группа их не двигает скопом).
  function GroupOrderCard(props) {
    var g = props.g, app = props.app;
    var items = g.items || [];
    var statuses = g.statuses || [];
    return h('div', { style: { marginBottom: 6, borderLeft: '2px solid var(--accent-line)', paddingLeft: 10, borderRadius: 6 } },
      h('div', { className: 'order', style: { borderColor: 'var(--accent-line)', background: 'var(--accent-soft)' } },
        h('div', { className: 'top' },
          g.preview ? h('img', { className: 'th', src: g.preview, alt: '' }) : h('div', { className: 'th' }, icon('cart', { size: 20 })),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { className: 'nm' }, '🧺 Заказ · ', g.count, ' ', pluralRu(g.count, 'позиция', 'позиции', 'позиций')),
            statuses.length > 0 && h('div', { style: { marginTop: 3 } }, statuses.map(function (s, i) {
              return h('span', { key: i, className: 'stbadge ' + (ST_CLASS[s.status] || 'st-new'), style: { marginRight: 6, marginBottom: 3 } },
                icon(ST_ICON[s.status] || 'sparkle', { size: 12 }), ' ', s.label);
            })),
            deliveryLines(g),
            g.comment && h('div', { className: 'sub' }, '💬 ', g.comment)),
          h('span', { className: 'mono', style: { color: 'var(--price)', fontWeight: 700, alignSelf: 'center', flex: '0 0 auto' } }, g.total))),
      items.map(function (it) { return h(OrderCard, { key: it.id, o: it, app: app }); }));
  }

  function Orders(props) {
    var app = props.app, orders = app.orders || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(function () { app.reloadOrders(); }) }, 'Мои заказы'),
      // «Новый расчёт» всегда означает одно: Главная с зоной приёма (IA-редизайн, проблема #4)
      h('button', { className: 'btn soft', style: { marginTop: 0, marginBottom: 12 }, onClick: function () { app.goTab('hub'); } },
        icon('plus', { size: 17 }), ' Новый расчёт'),
      orders.length === 0 && h('div', { className: 'empty' }, icon('receipt', { size: 46 }),
        h('div', null, 'Заказов пока нет.'),
        h('button', { className: 'btn line', style: { marginTop: 14, width: 'auto', padding: '0 18px' },
          onClick: function () { app.go('custom'); } }, icon('chat', { size: 16 }), ' Вопрос мастеру')),
      // заказ-корзина (B5) приходит одной записью { group:true, items:[…] } — рендерим карточкой-группой
      orders.map(function (o) {
        // group === true — карточка-группа (заказ-корзина ≥2 позиций). У обычной позиции в
        // поле group лежит СТРОКА-идентификатор группы, поэтому сравнение строгое: иначе
        // одиночный заказ уехал бы в карточку-группу и отрисовался как «🧺 Заказ · undefined».
        return o.group === true
          ? h(GroupOrderCard, { key: 'g' + o.gid, g: o, app: app })
          : h(OrderCard, { key: o.id, o: o, app: app });
      }));
  }

  // ────────────────────── CUSTOM ORDER ─────────────────────
  function CustomOrder(props) {
    var app = props.app;
    // Префилл из планировщика стены Multiboard (window.__MBP_PREFILL): текст-спецификация + PNG раскладки.
    // Читаем один раз и сразу сбрасываем, чтобы обычный «Индивидуальный заказ» не подтянул прошлую раскладку.
    var pre = window.__MBP_PREFILL; if (pre) { try { window.__MBP_PREFILL = null; } catch (e) {} }
    var st = useState(function () { return (pre && pre.text) || ''; }); var text = st[0], setText = st[1];
    var sf = useState(function () {
      var a = [];
      if (pre && pre.files) { try { a = pre.files.map(function (f) { return { file: f, url: URL.createObjectURL(f) }; }); } catch (e) {} }
      return a;
    }); var files = sf[0], setFiles = sf[1];   // [{ file, url }]
    function addFiles(list) {
      var arr = Array.prototype.slice.call(list || []);
      setFiles(function (cur) {
        var next = cur.slice();
        arr.forEach(function (f) { if (next.length < 8) next.push({ file: f, url: URL.createObjectURL(f) }); });
        return next;
      });
    }
    function removeAt(i) { setFiles(function (cur) { return cur.filter(function (_, j) { return j !== i; }); }); }
    function submit() {
      if (!text.trim() && files.length === 0) { app.toast('Опишите заказ или добавьте фото'); return; }
      app.submitCustom(text.trim(), files.map(function (f) { return f.file; }));
    }
    return h('div', { className: 'screen' },
      h(Lab, null, 'Индивидуальный заказ'),
      h('p', { className: 'hint', style: { marginTop: 0 } },
        'Нет готовой модели? Опишите словами, что нужно напечатать, и приложите фото или эскизы — мастер оценит и предложит цену.'),
      h('textarea', { className: 'input', rows: 5,
        placeholder: 'Например: подставка под телефон как на фото, высота ~12 см, чёрный PETG…',
        value: text, onChange: function (e) { setText(e.target.value); },
        style: { resize: 'vertical', minHeight: 110, fontFamily: 'inherit', lineHeight: 1.45 } }),
      h('label', { className: 'btn soft', style: { cursor: 'pointer', marginTop: 12 } },
        icon('upload', { size: 18 }), ' Добавить фото / эскизы',
        h('input', { type: 'file', accept: 'image/*', multiple: true, style: { display: 'none' },
          onChange: function (e) { addFiles(e.target.files); e.target.value = ''; } })),
      files.length > 0 && h('div', { className: 'grid2', style: { marginTop: 12, gap: 9 } }, files.map(function (f, i) {
        return h('div', { key: i, className: 'imgwrap', style: { position: 'relative' } },
          h('img', { src: f.url, alt: '', style: { width: '100%', height: 120, objectFit: 'cover', borderRadius: 12 } }),
          h('button', { onClick: function () { removeAt(i); }, 'aria-label': 'Убрать',
            style: { position: 'absolute', top: 6, right: 6, width: 26, height: 26, padding: 0, borderRadius: 9,
              border: 0, background: 'rgba(0,0,0,.62)', color: '#fff', cursor: 'pointer' } }, icon('close', { size: 15 })));
      })),
      h('p', { className: 'hint', style: { marginTop: 12 } }, files.length, ' из 8 фото'),
      h('button', { className: 'btn lg', style: { marginTop: 8 }, disabled: app.busy, onClick: submit },
        icon('receipt', { size: 18 }), ' Отправить мастеру'));
  }

  // ───────────────────────── CART ─────────────────────────
  function parseMoney(s) { return parseFloat(String(s).replace(/[^\d,.-]/g, '').replace(',', '.')) || 0; }
  // Раскладка на стол (idea #14): первые два числа из «X×Y×Z мм».
  function parseXY(dims) {
    var m = String(dims || '').match(/(\d+(?:\.\d+)?)/g);
    return (m && m.length >= 2) ? [parseFloat(m[0]), parseFloat(m[1])] : null;
  }
  // Группы корзины «один материал + один цвет», которые выгодно собрать партией на меньшее число столов.
  function batchHints(cart) {
    var BED = 250, cap = BED * BED * 0.72;   // стол Centauri ~256мм, ~72% полезной площади при упаковке
    var groups = {}, order = [];
    (cart || []).forEach(function (c) {
      if (!c.pid || !c.color) return;
      var xy = parseXY(c.dims); if (!xy) return;
      if (Math.max(xy[0], xy[1]) > BED) return;   // одна модель не влезает на стол — не батчим
      var k = c.pid + '|' + c.color;
      if (!groups[k]) { groups[k] = { plastic: c.plastic || '', color: c.color, n: 0, area: 0 }; order.push(k); }
      groups[k].n += 1; groups[k].area += xy[0] * xy[1];
    });
    var out = [];
    order.forEach(function (k) {
      var g = groups[k], plates = Math.max(1, Math.ceil(g.area / cap));
      if (g.n >= 2 && plates < g.n) out.push({ plastic: g.plastic, color: g.color, n: g.n, plates: plates });
    });
    return out;
  }

  function Cart(props) {
    var app = props.app, cart = app.cart || [];
    var total = cart.reduce(function (a, c) { return a + parseMoney(c.totalStr); }, 0);
    var hints = batchHints(cart);
    var bs = useState(true); var batchOn = bs[0], setBatchOn = bs[1];   // согласие на печать партией
    var doBatch = hints.length > 0 && batchOn;
    // Доставка (B5 + A2): способ получения, адрес, ТИП ДОСТАВКИ и общий комментарий — уходят
    // одним заказом в checkout. Цену показываем справочно, считает её сервер по типу.
    // Стартовые значения — из умолчаний профиля: клиент задал адрес один раз и не повторяет
    // его на каждом заказе. Ни одного типа не заведено (dcfg.enabled=false) — блока получения
    // нет вовсе: мастерская работает на самовывоз, лишний вопрос там только мешает.
    var dcfg = (app.options && app.options.delivery) || {};
    var ds = useState(dcfg.default_address ? 'delivery' : 'pickup');
    var dmethod = ds[0], setDMethod = ds[1];
    var ads = useState(dcfg.default_address || ''); var addr = ads[0], setAddr = ads[1];
    var cs = useState(''); var gcomment = cs[0], setGComment = cs[1];
    var zs = useState([]); var types = zs[0], setTypes = zs[1];
    var zps = useState(dcfg.default_type_id || 0); var typeId = zps[0], setTypeId = zps[1];
    // Тарифы тянем один раз на вход в корзину и пересчитываем при смене суммы позиций:
    // порог «бесплатно от …» зависит именно от неё.
    useEffect(function () {
      if (!cart.length || dcfg.enabled === false) return;
      API.deliveryTypes(total).then(function (r) {
        var list = (r && r.ok && r.types) || [];
        setTypes(list);
        setTypeId(function (cur) {
          if (cur && list.filter(function (t) { return t.id === cur; }).length) return cur;
          return list.length ? list[0].id : 0;
        });
      }).catch(function () { setTypes([]); });
    }, [cart.length, Math.round(total * 100)]);
    var dtype = types.filter(function (t) { return t.id === typeId; })[0] || null;
    var dcost = (dmethod === 'delivery' && dtype) ? (dtype.cost || 0) : 0;
    // Доставку без адреса не пускаем; если тарифы заведены — тип тоже обязателен.
    var deliveryBad = dmethod === 'delivery' && (!addr.trim() || (types.length > 0 && !typeId));
    var deliveryOff = dcfg.enabled === false;      // типов нет → только самовывоз
    function submitCart() {
      app.checkout(doBatch, {
        delivery_method: (deliveryOff ? 'pickup' : dmethod), delivery_address: addr.trim(),
        delivery_type_id: (!deliveryOff && dmethod === 'delivery' ? typeId : 0),
        comment: gcomment.trim() });
    }
    return h('div', { className: 'screen' },
      h(Lab, null, 'Корзина'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Соберите несколько моделей и оформите одним заказом.'),
      cart.length === 0
        ? h('div', { className: 'empty' }, icon('cart', { size: 46 }),
            h('div', null, 'Корзина пуста.', h('br'), 'Посчитайте модель и нажмите «Добавить в корзину».'),
            // из пустой корзины должен быть выход к действию, а не тупик (аудит UX)
            h('div', { style: { display: 'flex', gap: 8, marginTop: 14, justifyContent: 'center' } },
              h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 16px' },
                onClick: function () { app.goTab('hub'); } }, icon('coin', { size: 16 }), ' Посчитать модель'),
              h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 16px' },
                onClick: function () { app.goTab('collection'); } }, icon('globe', { size: 16 }), ' Каталог')))
        : h('div', { style: { marginTop: 12 } },
          cart.map(function (c, i) {
            var qty = c.qty || 1;
            // степпер количества (аудит, проблема #3): пересчёт через requote, серверный
            // снимок токена обновляется — чекаут уйдёт с новым количеством
            function stepBtn(d, label) {
              return h('button', { type: 'button', disabled: app.busy || (qty + d < 1),
                onClick: function () { app.updateCartQty(i, qty + d); },
                style: { width: 28, height: 28, borderRadius: 9, border: '1px solid var(--line-2)',
                  background: 'var(--surface-2)', color: 'var(--text)', fontSize: 16, fontWeight: 700,
                  cursor: 'pointer', lineHeight: 1 } }, label);
            }
            return h('div', { key: i, className: 'cartrow' },
              c.preview ? h('img', { className: 'th', src: c.preview, alt: '' }) : h('div', { className: 'th' }),
              h('div', { style: { minWidth: 0, flex: 1 } },
                h('div', { style: { fontWeight: 600, fontSize: 14 } }, c.label),
                h('div', { className: 'mono', style: { fontSize: 12.5, color: 'var(--price)', marginTop: 2 } }, c.totalStr),
                h('div', { style: { display: 'flex', alignItems: 'center', gap: 9, marginTop: 7 } },
                  stepBtn(-1, '−'),
                  h('b', { className: 'mono', style: { fontSize: 13.5, minWidth: 34, textAlign: 'center' } }, qty, h('span', { style: { color: 'var(--faint)', fontWeight: 500 } }, ' шт')),
                  stepBtn(1, '+'))),
              h('button', { className: 'x', onClick: function () { app.removeCart(i); } }, icon('close', { size: 18 })));
          }),
          // Подсказка батчинга: несколько позиций одного материала+цвета → один стол, дешевле.
          hints.length > 0 && h('button', { className: 'batchsug' + (batchOn ? ' on' : ''), type: 'button',
            onClick: function () { setBatchOn(!batchOn); } },
            h('span', { className: 'bx' }, icon(batchOn ? 'check' : 'box', { size: 15 })),
            h('span', { className: 'bt' },
              h('b', null, 'Собрать партией на один стол'),
              hints.map(function (g, i) {
                return h('span', { key: i, className: 'bgrp' }, g.n + ' шт · ' + (g.plastic ? g.plastic + ' ' : '') + g.color
                  + ' → ' + (g.plates === 1 ? 'один стол' : g.plates + ' стол.') + ' вместо ' + g.n);
              }),
              h('span', { className: 'bh' }, 'Меньше подготовки — мастер пересчитает дешевле.'))),
          // Получение (B5 + A2): сегмент Самовывоз/Доставка + тип доставки + адрес.
          // Тариф выбирается ДО оформления: цена доставки входит в итог заказа. Типов не
          // заведено — блока нет совсем (мастерская только на самовывоз).
          !deliveryOff && h('div', { style: { marginTop: 18 } },
            h(Lab, null, 'Получение'),
            h('div', { className: 'seg', style: { marginTop: 8 } },
              h('button', { type: 'button', className: dmethod === 'pickup' ? 'on' : '',
                onClick: function () { setDMethod('pickup'); } }, 'Самовывоз'),
              h('button', { type: 'button', className: dmethod === 'delivery' ? 'on' : '',
                onClick: function () { setDMethod('delivery'); } }, 'Доставка')),
            dmethod === 'delivery' && types.length > 0 && h('div', { style: { marginTop: 10, display: 'grid', gap: 6 } },
              types.map(function (t) {
                var on = t.id === typeId;
                return h('button', { key: t.id, type: 'button', onClick: function () { setTypeId(t.id); },
                  style: { display: 'flex', alignItems: 'center', gap: 10, width: '100%', textAlign: 'left',
                    padding: '10px 12px', borderRadius: 12, cursor: 'pointer',
                    border: '1px solid ' + (on ? 'var(--accent)' : 'var(--line-2)'),
                    background: on ? 'var(--accent-soft)' : 'var(--surface-2)', color: 'var(--text)' } },
                  h('span', { style: { flex: 1, minWidth: 0 } },
                    h('b', { style: { fontSize: 14 } }, t.name),
                    t.eta && h('span', { className: 'sub', style: { display: 'block', marginTop: 2 } }, t.eta),
                    !t.free && t.free_from > 0 && h('span', { className: 'sub', style: { display: 'block', marginTop: 2 } },
                      'Бесплатно от ' + API.fmt(t.free_from))),
                  h('b', { className: 'mono', style: { color: t.free ? 'var(--ok, #3a9)' : 'var(--price)', flex: '0 0 auto' } },
                    t.free ? 'бесплатно' : API.fmt(t.cost || 0)));
              })),
            dmethod === 'delivery' && h('input', { className: 'input', style: { marginTop: 10 },
              placeholder: 'Адрес доставки', value: addr, onChange: function (e) { setAddr(e.target.value); } }),
            dmethod === 'delivery' && types.length === 0 && h('p', { className: 'hint', style: { marginTop: 6 } },
              'Стоимость доставки мастер уточнит отдельно.'),
            dmethod === 'delivery' && dcfg.default_address && h('p', { className: 'hint', style: { marginTop: 6 } },
              'Адрес подставлен из профиля — можно поправить для этого заказа.')),
          h('input', { className: 'input', style: { marginTop: 12 },
            placeholder: 'Комментарий к заказу (необязательно)', value: gcomment,
            onChange: function (e) { setGComment(e.target.value); } }),
          // Итог с доставкой: строкой «позиции + доставка», чтобы сумма на кнопке не удивляла.
          dcost > 0 && h('div', { style: { marginTop: 14, display: 'grid', gap: 4 } },
            h('div', { style: { display: 'flex', justifyContent: 'space-between' } },
              h('span', { className: 'sub' }, 'Позиции'), h('span', { className: 'mono' }, API.fmt(total))),
            h('div', { style: { display: 'flex', justifyContent: 'space-between' } },
              h('span', { className: 'sub' }, 'Доставка' + (dtype ? ' · ' + dtype.name : '')),
              h('span', { className: 'mono' }, API.fmt(dcost)))),
          h('button', { className: 'btn', style: { marginTop: 16 }, disabled: app.busy || deliveryBad, onClick: submitCart },
            icon('check', { size: 18 }), ' Оформить заказ · ', API.fmt(total + dcost),
            doBatch && h('span', { className: 'batchtag' }, '🧩 партией')),
          deliveryBad && h('p', { className: 'hint', style: { marginTop: 8, color: 'var(--accent)' } },
            'Укажите адрес доставки или выберите самовывоз.')));
  }

  // ──────────────────── COLLECTION / MODELS ────────────────────
  function Grid(props) {
    var app = props.app;
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(props.onRefresh) }, props.title),
      (props.items || []).length === 0
        ? h('div', { className: 'empty' }, icon(props.emptyIcon || 'folder', { size: 46 }), h('div', null, props.empty || 'Пусто.'))
        : h('div', { className: 'grid2' }, props.items.map(function (m) {
          return h('button', { key: m.id, className: 'tile', onClick: function () { app.useModel(props.kind, m.id); } },
            h('div', { className: 'imgwrap' }, h('img', { src: m.preview, alt: '' }),
              (m.plates > 1) && h('span', { className: 'badge', style: { left: 'auto', right: 7, color: 'var(--price)' } }, '×', m.plates)),
            h('div', { className: 'cap' }, h('div', { className: 'n' }, m.label),
              m.dims && h('div', { className: 'd' }, m.dims, ' мм')));
        })));
  }
  // русская плюрализация: pluralRu(4, 'модель', 'модели', 'моделей') → «модели»
  function pluralRu(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }
  // Каталог «Модели» (c9c42789): многоуровневая система — разделы («Напечатанное» + темы из
  // группы ВК + виртуальный «Создай своё») → грид раздела с пагинацией. Фильтры-чипы убраны.
  // Поиск — серверный по ВСЕМУ каталогу (подстрока label+теги+orig_label, семантика b56041c9
  // хвостом): фронт держит только топ-120, локальный фильтр каталог не покрывает. Пока ответ
  // в пути (и как фолбэк без бэка/в mock) — мгновенная подстрока по загруженному.
  //
  // Состояние каталога живёт ВНЕ компонента (COLL_STATE): уход на карточку товара размонтирует
  // Collection, а «назад» должен вернуть РОВНО на шаг — в открытый раздел на том же месте, а не
  // выкидывать в список разделов (фидбэк владельца 2026-07-11).
  var COLL_STATE = { tab: 'ready', cat: null, catPage: null };
  // Плитка «Создай своё» (параметрические конструкторы) снята с витрины 2026-09-02 решением
  // владельца: «на данном этапе сырое — выпиливаем из UI, код оставляем». Вернуть — true.
  var SHOW_CREATOR = false;
  function Collection(props) {
    var app = props.app;
    var tabS = useState(COLL_STATE.tab); var tab = tabS[0], setTab = tabS[1];   // 'ready' (Модели, дефолт) | 'materials' | 'mine'
    var qs = useState(''); var q = qs[0], setQ = qs[1];
    var catS = useState(COLL_STATE.cat); var cat = catS[0], setCat = catS[1];   // открытый раздел (null = список разделов)
    var ciS = useState(COLL_STATE.catPage || { items: [], next: null, busy: false }); var catPage = ciS[0], setCatPage = ciS[1];
    var semS = useState(null); var found = semS[0], setFound = semS[1];   // {q, items} | null — серверная выдача
    var semBusyS = useState(false); var semBusy = semBusyS[0], setSemBusy = semBusyS[1];
    var semT = useRef(null);
    // персистим снаружи компонента, чтобы возврат с карточки восстановил раздел и страницу
    useEffect(function () { COLL_STATE.tab = tab; COLL_STATE.cat = cat; COLL_STATE.catPage = catPage; }, [tab, cat, catPage]);
    // Открытый раздел — свой слой на центральном стеке «Назад» (window.PS_Back, back.js,
    // OpenTodo 85d16a2a): свайп/аппаратная кнопка «назад» выходят из раздела, а не закрывают
    // мини-апп. closeCat() — единственный путь закрытия (переключение вкладки каталога),
    // чтобы стек не оставался с висящим слоем. catLayer + cleanup на размонтировании: уход с
    // экрана каталога при открытом разделе (тап по модели → карточка) размонтирует Collection —
    // слой надо снять, иначе первый свайп «назад» уйдёт в пустоту.
    var catLayer = useRef(null);
    // Подвид для менеджера скролла (861eab19): у списка разделов и у открытого раздела своя
    // запомненная позиция. Зовём из ОБРАБОТЧИКОВ (не из эффекта), чтобы смена подвида и смена
    // контента попали в один коммит React.
    // ВАЖНО: состояние по умолчанию (вкладка «Модели», раздел не открыт) обязано давать
    // ПУСТУЮ строку — ровно её App держит для экрана, пока никто не звал setView. Иначе
    // позиция, записанная при входе на экран, лежала бы под одним ключом, а закрытие раздела
    // искало бы её под другим — и список каждый раз уезжал бы в начало.
    function viewKey(t, c) { return (t === 'ready' && !c) ? '' : (t + '/' + (c || '')); }
    function view(t, c) { if (app.setView) app.setView(viewKey(t, c)); }
    function pushCatLayer(name) {
      if (window.PS_Back) catLayer.current = window.PS_Back.push(function () { catLayer.current = null; setCat(null); view(tab, null); });
    }
    function closeCat() {
      if (cat && window.PS_Back && catLayer.current) window.PS_Back.back();
      else { setCat(null); view(tab, null); }
    }
    function switchTab(t) { closeCat(); setTab(t); view(t, null); }
    useEffect(function () {
      // монтирование с восстановленным разделом (вернулись с карточки товара) — слой раздела
      // надо положить заново, иначе первая же стрелка «назад» перескочит раздел
      if (cat) pushCatLayer();
      return function () { if (catLayer.current && window.PS_Back) window.PS_Back.remove(catLayer.current); };
    }, []);
    function openCat(name) {
      setCat(name); setQ(''); setFound(null);
      pushCatLayer(name); view(tab, name);
      if (name === 'Создай своё') return;   // виртуальный раздел — рендерит конструкторы
      setCatPage({ items: [], next: null, busy: true });
      API.collection({ category: name, offset: 0 }).then(function (j) {
        setCatPage({ items: (j && j.items) || [], next: (j && j.next_offset) || null, busy: false });
      }).catch(function () { setCatPage({ items: [], next: null, busy: false }); });
    }
    function moreCat() {
      if (!cat || catPage.busy || catPage.next == null) return;
      setCatPage(Object.assign({}, catPage, { busy: true }));
      API.collection({ category: cat, offset: catPage.next }).then(function (j) {
        setCatPage({ items: catPage.items.concat((j && j.items) || []),
          next: (j && j.next_offset) || null, busy: false });
      }).catch(function () { setCatPage(Object.assign({}, catPage, { busy: false })); });
    }
    function onQuery(v) {
      setQ(v); setFound(null);
      clearTimeout(semT.current);
      var query = v.trim();
      if (query.length < 2) { setSemBusy(false); return; }
      setSemBusy(true);   // индикатор «ищем по всему каталогу» у поля (аудит UX)
      semT.current = setTimeout(function () {
        API.collectionSearch(query).then(function (j) {
          setSemBusy(false);
          if (j && j.ok && j.items) setFound({ q: query, items: j.items });
        }).catch(function () { setSemBusy(false); });
      }, 350);
    }
    var all = app.collection || [];
    function textOf(m) { return ((m.label || '') + ' ' + (m.tags || '')).toLowerCase(); }
    var items;
    if (q && found && found.q === q.trim()) {
      // серверная выдача по всему каталогу: подстрочные совпадения первыми, семантика хвостом
      items = found.items;
    } else {
      // мгновенная подстрока по загруженному топ-120, пока серверный ответ в пути
      // (и фолбэк, если бэк недоступен / mock)
      items = all.filter(function (m) {
        return !(q && textOf(m).indexOf(q.toLowerCase()) < 0);
      });
    }
    var cur = (app.options && app.options.currency) || 'руб.';
    function tile(m) {
      // ae038968: карточка открывается мгновенно из данных плитки (разделы и поисковая выдача
      // каталога) — не app.useModel, чтобы не блокировать полноэкранным оверлеем загрузки
      return h('button', { key: m.id, className: 'tile', onClick: function () { app.openProduct(m); } },
        h('div', { className: 'imgwrap' }, h('img', { src: m.preview, alt: '', loading: 'lazy' }),
          m.printed ? h('span', { className: 'badge', style: { color: 'var(--amber)' } }, '📸') : null,
          (m.plates > 1) && h('span', { className: 'badge', style: { left: 'auto', right: 7, color: 'var(--price)' } }, '×', m.plates),
          window.PS_FavHeart && h(window.PS_FavHeart, { app: app, id: m.id })),
        h('div', { className: 'cap' }, h('div', { className: 'n' }, m.label),
          h('div', { className: 'd' },
            m.dims && h('span', null, m.dims, ' мм'),
            // «от N» — кэш последнего расчёта (ux-simplification.md, идея 3)
            m.price > 0 && h('span', { style: { color: 'var(--price)', fontWeight: 700 } },
              (m.dims ? ' · ' : ''), 'от ', String(Math.round(m.price * 100) / 100).replace('.', ','), ' ', cur))));
    }
    // разделы: «Создай своё» — виртуальный (конструкторы), остальные с сервера (c9c42789)
    var cats = (app.collectionCats || []).filter(function (c) { return c.count > 0; });
    // Чип «Назад» из этой строки убран (фидбэк владельца 2026-07-11): выход из раздела — общей
    // стрелкой в ШАПКЕ приложения (она видит слой раздела через PS_Back и снимает ровно его).
    return h('div', { className: 'screen' },
      h(Lab, { right: refreshBtn(function () { app.reloadCollection(); }) }, 'Каталог'),
      h('div', { className: 'ctabs' },
        // переключение на соседнюю вкладку каталога закрывает открытый раздел (иначе он
        // остаётся висеть на стеке «Назад», и первый свайп/тап «назад» окажется «пустым»).
        // switchTab сообщает подвид ПОСЛЕДНИМ — closeCat внутри тоже зовёт setView (со старой
        // вкладкой), и без этого порядка выиграла бы его запись.
        h('button', { className: tab === 'ready' ? 'on' : '', type: 'button', onClick: function () { switchTab('ready'); } },
          icon('globe', { size: 16 }), ' Модели'),
        h('button', { className: tab === 'materials' ? 'on' : '', type: 'button', onClick: function () { switchTab('materials'); } },
          icon('spool', { size: 16 }), ' Материалы'),
        h('button', { className: tab === 'mine' ? 'on' : '', type: 'button', onClick: function () { switchTab('mine'); } },
          icon('folder', { size: 16 }), ' Мои')),

      // ── таб «Материалы»: витрина пластиков с наличием и CTA (бывший экран catalog) ──
      tab === 'materials'
        ? h(MaterialsList, { app: app })

      // ── таб «Мои»: прошлые загрузки юзера для повтора (бывший экран «Мои модели») ──
      : tab === 'mine'
        ? h(MineTab, { app: app })

      // ── таб «Модели»: разделы → грид раздела; поиск ищет по всей коллекции ──
        : h('div', null,
            !cat && h('div', { className: 'field', style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 12, background: 'var(--surface)', border: '1px solid var(--line-2)', borderRadius: 12, padding: '0 12px' } },
              h('span', { style: { color: 'var(--faint)', display: 'flex' } }, icon('search', { size: 16 })),
              h('input', { placeholder: 'Поиск: «дракон», «кот», «подставка»…', value: q,
                style: { flex: 1, border: 0, background: 'transparent', color: 'var(--text)', padding: '11px 0', fontSize: 14, outline: 'none' },
                onChange: function (e) { onQuery(e.target.value); } }),
              semBusy && h('span', { className: 'mono', style: { color: 'var(--faint)', fontSize: 12, flex: '0 0 auto' } }, 'ищу…')),

            // поиск активен → плоская выдача по всей коллекции
            !cat && q
              ? (items.length === 0
                  ? h('p', { className: 'hint', style: { marginTop: 12 } },
                      semBusy ? 'Ищу по всему каталогу…' : 'Ничего не нашлось — измените запрос.')
                  : h('div', { className: 'grid2', style: { marginTop: 12 } }, items.map(tile)))

            // список разделов — крупная сетка обложек 2×N вместо узких строк (OpenTodo
            // 0f1869c8, фидбэк владельца 2026-07-10: «плохо видны картинки»)
            : !cat
              ? h('div', { className: 'catgrid' },
                  // «Избранное» — виртуальный раздел из сердечек юзера; появляется с первой
                  // отмеченной моделью (пустой раздел в сетке — мусор)
                  (function () {
                    var n = Object.keys(app.favs || {}).length;
                    if (!n) return null;
                    return h('button', { key: '_fav', className: 'cattile favcat', onClick: function () { openCat('__fav__'); } },
                      h('div', { className: 'cv-ph' }, icon('heartf', { size: 40 })),
                      h('div', { className: 'ov' }, h('div', { className: 'tt' }, 'Избранное'),
                        h('div', { className: 'ss' }, n + ' ' + pluralRu(n, 'модель', 'модели', 'моделей'))));
                  })(),
                  // «Создай своё» = параметрические модели, а они рендерятся внешним OpenSCAD.
                  // В сборке без него раздела нет вовсе (f98aa1ef): кнопка вела бы в ошибку.
                  // 2026-09-02: раздел СНЯТ С ВИТРИНЫ решением владельца («на данном этапе
                  // сырое») — плитка не рисуется, пока SHOW_CREATOR = false; код конструкторов
                  // (screens_custom.js, PS_CreatorSection, кастомайзер) оставлен целиком.
                  SHOW_CREATOR && window.PS_CreatorSection && window.PS_hasFeature('parametric')
                  && h('button', { key: '_creator', className: 'cattile creator', onClick: function () { openCat('Создай своё'); } },
                    h('div', { className: 'cv-ph' }, icon('sliders', { size: 40 })),
                    h('div', { className: 'ov' }, h('div', { className: 'tt' }, 'Создай своё'),
                      h('div', { className: 'ss' }, 'Настраиваемые модели'))),
                  // «Органайзер в ящик» переехал из каталога в таб «Приложения» (screens_apps.js,
                  // 2026-09-02): это инструмент с проектами, а не раздел коллекции.
                  cats.map(function (c) {
                    return h('button', { key: c.name, className: 'cattile', onClick: function () { openCat(c.name); } },
                      // картинка раздела = превью самой популярной модели (85e8d47b); нет — фолбэк-иконка
                      c.preview
                        ? h('img', { className: 'cv', src: c.preview, alt: '', loading: 'lazy' })
                        : h('div', { className: 'cv-ph' }, icon(c.name === 'Напечатанное' ? 'image' : 'box', { size: 40 })),
                      h('div', { className: 'ov' }, h('div', { className: 'tt' }, c.name),
                        h('div', { className: 'ss' }, c.count + ' ' + pluralRu(c.count, 'модель', 'модели', 'моделей'))));
                  }),
                  !cats.length && h('div', { className: 'empty', style: { gridColumn: '1 / -1' } }, icon('globe', { size: 46 }), h('div', null, 'Каталог пока пуст.')))

            // открытый раздел — кнопка «назад» теперь в заголовке «Каталог» выше (см. inCat/backBtn),
            // здесь только название раздела, без правой кнопки (не дублируем «назад»)
              : (function () {
                  // в «Избранном» снятое сердечко убирает плитку сразу (фильтр по живому
                  // app.favs), а не при следующей загрузке раздела
                  var secItems = cat === '__fav__'
                    ? catPage.items.filter(function (m) { return app.favs && app.favs[m.id]; })
                    : catPage.items;
                  return h('div', null,
                    h(Lab, null, cat === '__fav__' ? 'Избранное' : cat),
                    cat === 'Создай своё'
                      ? (window.PS_CreatorSection && window.PS_hasFeature('parametric')
                          ? h(window.PS_CreatorSection, { app: app })
                          : h('p', { className: 'hint' }, 'Конструкторы недоступны.'))
                      : h('div', null,
                          secItems.length === 0 && !catPage.busy && h('p', { className: 'hint', style: { marginTop: 10 } },
                            cat === '__fav__' ? 'Пока пусто. Отмечайте модели сердечком — они соберутся здесь.' : 'В разделе пока пусто.'),
                          h('div', { className: 'grid2', style: { marginTop: 10 } }, secItems.map(tile)),
                          catPage.busy && h('p', { className: 'hint', style: { marginTop: 10 } }, 'Загружаю…'),
                          catPage.next != null && !catPage.busy && h('button', { className: 'btn soft', style: { marginTop: 12 }, onClick: moreCat }, 'Показать ещё')));
                })()));
  }
  function MyModels(props) {
    return h(Grid, { app: props.app, title: 'Мои модели', kind: 'saved', items: props.app.mymodels,
      emptyIcon: 'folder', empty: 'Вы ещё не загружали модели.', onRefresh: function () { props.app.reloadMyModels(); } });
  }

  // Вкладка «Мои» Каталога: те же плитки, что и экран «Мои модели» (deep-link models жив),
  // но внутри единого Каталога; список подгружается лениво при первом открытии вкладки.
  function MineTab(props) {
    var app = props.app, all = app.mymodels || [];
    var qs = useState(''); var q = qs[0], setQ = qs[1];
    useEffect(function () { if (!all.length) app.reloadMyModels(); }, []);
    var items = q ? all.filter(function (m) { return (m.label || '').toLowerCase().indexOf(q.toLowerCase()) >= 0; }) : all;
    if (all.length === 0) {
      return h('div', { className: 'empty' }, icon('folder', { size: 46 }), h('div', null, 'Вы ещё не загружали модели.'));
    }
    return h('div', null,
      all.length > 4 && h('div', { className: 'field', style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 12, background: 'var(--surface)', border: '1px solid var(--line-2)', borderRadius: 12, padding: '0 12px' } },
        h('span', { style: { color: 'var(--faint)', display: 'flex' } }, icon('search', { size: 16 })),
        h('input', { placeholder: 'Поиск по своим моделям…', value: q,
          style: { flex: 1, border: 0, background: 'transparent', color: 'var(--text)', padding: '11px 0', fontSize: 14, outline: 'none' },
          onChange: function (e) { setQ(e.target.value); } })),
      items.length === 0
        ? h('p', { className: 'hint', style: { marginTop: 12 } }, 'Ничего не нашлось.')
        : h('div', { className: 'grid2', style: { marginTop: 12 } }, items.map(function (m) {
          return h('button', { key: m.id, className: 'tile', onClick: function () { app.useModel('saved', m.id); } },
            h('div', { className: 'imgwrap' }, h('img', { src: m.preview, alt: '' }),
              (m.plates > 1) && h('span', { className: 'badge', style: { left: 'auto', right: 7, color: 'var(--price)' } }, '×', m.plates)),
            h('div', { className: 'cap' }, h('div', { className: 'n' }, m.label),
              m.dims && h('div', { className: 'd' }, m.dims, ' мм')));
        })));
  }

  // ───────────────────────── МАТЕРИАЛЫ (бывш. CATALOG) ─────────────────────────
  // Витрина пластиков БЕЗ тупика (аудит, проблема #5): бейджи наличия (данные уже приходят в
  // /api/options: out_of_stock / low_stock) + CTA «Посчитать с этим пластиком» (пресет через
  // app.preselectPlastic — тот же механизм, что у кастомайзера).
  //
  // Карточка горизонтальная (OpenTodo c36cd4be, фидбэк владельца 2026-07-10: «картинка
  // обрезается» — было фото 16:10 на всю ширину с object-fit:cover, срезало катушку сверху и
  // снизу). Теперь слева фикс. квадрат ~100px с object-fit:contain на тёмной подложке — тот же
  // приём, что у .order .th / .cartrow .th (там уже решали ровно эту проблему для превью
  // моделей), фото целиком видно без обрезки. Справа — имя+бейджи наличия, тип/цена/плотность,
  // описание (line-clamp 3 строки) и кружки цветов (тот же dot-ring, что в слотах мультицвета
  // Config, screens_core.js).
  function MatPhoto(props) {
    // Фолбэк на битую/отсутствующую ссылку — аналог Thumb() в screens_core.js.
    var bs = useState(false); var bad = bs[0], setBad = bs[1];
    var ok = props.src && !bad;
    return h('div', { className: 'mat-th' },
      ok ? h('img', { src: props.src, alt: '', loading: 'lazy', onError: function () { setBad(true); } })
        : icon('spool', { size: 32 }));
  }
  function MaterialsList(props) {
    var app = props.app, plastics = (app.options && app.options.plastics) || [], cur = (app.options && app.options.currency) || 'руб.';
    // Лайтбокс фото катушки (OpenTodo eb9a9efe): тап по кружку цвета — фото катушки этого
    // цвета на весь экран (p.spools = {цвет: url} приходит из /api/options только для цветов
    // с реальным фото). PS_Lightbox уже интегрирован с «назад» (back.js). Кружки крупные —
    // как выбор цвета на смете (.dotbtn 34px, фидбэк владельца 2026-07-11).
    var lbS = useState(''); var lbSrc = lbS[0], setLbSrc = lbS[1];
    function dot(css, spoolUrl, colorName) {
      if (!spoolUrl) {
        return h('span', { className: 'dotbtn', style: { background: css || '#888', cursor: 'default', display: 'inline-block' } });
      }
      return h('button', { type: 'button', className: 'dotbtn',
        title: 'Фото катушки: ' + colorName, 'aria-label': 'Фото катушки: ' + colorName,
        style: { background: css || '#888', cursor: 'zoom-in' },
        onClick: function () { setLbSrc(spoolUrl); } });
    }
    if (plastics.length === 0) {
      return h('div', { className: 'empty' }, icon('spool', { size: 46 }), h('div', null, 'Мастер ещё не добавил материалы.'));
    }
    return h('div', null,
      h(window.PS_Lightbox, { src: lbSrc, name: lbSrc ? 'spool.jpg' : '', onClose: function () { setLbSrc(''); } }),
      plastics.map(function (p) {
      // out_of_stock/low_stock — СПИСКИ цветов со склада (/api/options): бейдж «нет в наличии»
      // только когда закончились ВСЕ цвета; частичные проблемы показываем по-цветно
      var out = p.out_of_stock || [], low = p.low_stock || [];
      var allOut = p.colors && p.colors.length > 0 && out.length >= p.colors.length;
      return h('div', { key: p.id, className: 'card matcard', style: { marginTop: 12 } },
        h(MatPhoto, { src: p.photoUrl }),
        h('div', { className: 'mat-body' },
          h('div', { style: { fontWeight: 600, fontSize: 14.5, display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' } },
            p.name,
            allOut ? h('span', { className: 'stbadge st-rejected', style: { marginTop: 0, padding: '2px 8px', fontSize: 10.5 } }, 'нет в наличии')
              : out.length > 0 ? h('span', { className: 'stbadge st-rejected', style: { marginTop: 0, padding: '2px 8px', fontSize: 10.5 } }, 'нет: ' + out.join(', '))
              : low.length > 0 ? h('span', { className: 'stbadge st-progress', style: { marginTop: 0, padding: '2px 8px', fontSize: 10.5 } }, 'заканчивается: ' + low.join(', ')) : null),
          // строка цифр короткая, но на узких экранах может не влезть в одну строку — пусть
          // переносится, а не режется на полуслове (обрезание значений — та же жалоба, что и с фото)
          h('div', { className: 'mono', style: { fontSize: 12, color: 'var(--muted)', marginTop: 4, lineHeight: 1.4 } },
            p.type + ' · ' + p.price + ' ' + cur + '/кг · ' + p.density + ' г/см³'),
          // описание — 1-3 строки, дальше многоточие (аудит + фидбэк 2026-07-10)
          p.description && h('div', { style: { marginTop: 5, fontSize: 12.5, lineHeight: 1.4, color: 'var(--muted)',
            display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 3, overflow: 'hidden' } }, p.description),
          // кружки цветов: с фото катушки — кликабельные (фуллскрин, OpenTodo eb9a9efe)
          (p.colors && p.colors.length > 0) && h('div', { style: { marginTop: 8, display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap', fontSize: 12.5, color: 'var(--muted)' } },
            (p.color_css || []).map(function (c, i) {
              var name = (p.colors || [])[i] || '';
              return h(React.Fragment, { key: i }, dot(c, (p.spools || {})[name], name));
            }), ' ', p.colors.join(', ')),
          !allOut && h('button', { className: 'btn soft', style: { marginTop: 10, padding: '9px 13px', fontSize: 13 },
            onClick: function () { app.preselectPlastic(p.id); } },
            icon('coin', { size: 15 }), ' Посчитать с этим пластиком')));
    }));
  }

  // Экран catalog оставлен ради живых deep-links; UI-путь теперь — Каталог → «Материалы».
  function Catalog(props) {
    return h('div', { className: 'screen' },
      h(Lab, null, 'Каталог пластиков'),
      h(MaterialsList, { app: props.app }));
  }

  // ───────────────────────── HELP ─────────────────────────
  function Help(props) {
    var app = props.app;
    var iis = useState(''); var idea = iis[0], setIdea = iis[1];
    var ibS = useState(false); var ideaBusy = ibS[0], setIdeaBusy = ibS[1];
    function sendIdea() {
      var t = (idea || '').trim();
      if (t.length < 5) { app.toast('Опишите идею чуть подробнее'); return; }
      if (ideaBusy) return;
      setIdeaBusy(true);
      API.sendIdea(t).then(function (j) {
        setIdeaBusy(false);
        if (j.ok) { setIdea(''); app.toast('Спасибо за идею!'); } else app.toast(j.error || 'Ошибка');
      }).catch(function () { setIdeaBusy(false); app.toast('Сеть недоступна'); });
    }
    var steps = [
      ['Пришлите модель', (window.PS_hasFeature('makerworld') ? 'Ссылка MakerWorld / Printables / Thingiverse' : 'Ссылка Printables / Thingiverse') + ', файл .stl / .3mf / .obj / .zip — или просто киньте ссылку в чат бота, посчитаем сами.'],
      ['Выберите параметры', 'Пластик, цвет(а), количество, масштаб — и особые пожелания словами. Несколько цветов → переливание.'],
      ['Получите смету', 'Бот нарежет модель, покажет превью, расход филамента, время и цену.'],
      ['Оформите заказ', 'Оставьте заказ или соберите несколько моделей в корзину — количество можно менять прямо там. PDF-смета — в чат.'],
    ];
    return h('div', { className: 'screen' },
      h(Lab, null, 'Как это работает'),
      h('div', { className: 'card' }, steps.map(function (s, i) {
        return h('div', { key: i, className: 'step' }, h('div', { className: 'num' }, i + 1),
          h('div', null, h('div', { className: 'tt' }, s[0]), h('div', { className: 'ss' }, s[1])));
      })),
      h('div', { className: 'card', style: { color: 'var(--muted)', fontSize: 13, display: 'flex', gap: 9 } },
        icon('sparkle', { size: 17, style: { color: 'var(--accent)', flex: '0 0 auto' } }),
        h('span', null, 'Готовые модели, конструкторы и материалы — в «Каталоге» (нижняя панель), заказы и корзина — рядом.')),
      h(Lab, null, 'Предложить идею'),
      h('div', { className: 'card' },
        h('div', { style: { fontSize: 14, color: 'var(--muted)', lineHeight: 1.5 } }, 'Есть идея, как улучшить бота? Напишите — отправим разработчику напрямую.'),
        h('textarea', { className: 'input', style: { marginTop: 11, minHeight: 78, resize: 'vertical', width: '100%' }, placeholder: 'Ваша идея… (хотя бы пара слов)', value: idea, onChange: function (e) { setIdea(e.target.value); } }),
        h('button', { className: 'btn', style: { marginTop: 11 }, disabled: ideaBusy, onClick: sendIdea }, icon('sparkle', { size: 18 }), ideaBusy ? ' Отправляю…' : ' Отправить идею')));
  }

  // Экран Invite удалён (IA-редизайн): рефералка живёт в Профиле, deep-link invite→profile.

  // ───────────────────────── CHAT ─────────────────────────
  function Chat(props) {
    var app = props.app;
    var st = useState({ loading: true, label: '', messages: [] }); var data = st[0], setData = st[1];
    var ist = useState(''); var text = ist[0], setText = ist[1];
    var sb = useState(false); var sending = sb[0], setSending = sb[1];
    var endRef = useRef(null);
    function load() {
      API.orderMessages(app.chatOrderId).then(function (j) {
        if (j.ok) setData({ loading: false, label: j.label, messages: j.messages || [] });
        else setData({ loading: false, label: '', messages: [] });
      }).catch(function () {});
    }
    // Поллинг раз в 4 c (аудит UX): без него ответ мастера появлялся только после
    // переоткрытия экрана. Интервал чистится при уходе с экрана.
    useEffect(function () {
      load();
      var iv = setInterval(load, 4000);
      return function () { clearInterval(iv); };
    }, []);
    useEffect(function () { if (endRef.current) endRef.current.scrollTop = endRef.current.scrollHeight; });
    function send() {
      var t = text.trim(); if (!t || sending) return;
      setSending(true);
      API.orderMessage(app.chatOrderId, t).then(function (j) {
        setSending(false);
        if (j.ok) { setText(''); setData(function (d) { return Object.assign({}, d, { messages: j.messages }); }); }
        else app.toast((j && j.error) || 'Не отправилось — попробуйте ещё раз');
      }).catch(function () { setSending(false); app.toast('Сеть недоступна'); });
    }
    return h('div', { className: 'screen', style: { display: 'flex', flexDirection: 'column', height: '100%' } },
      h('div', { ref: endRef, style: { flex: 1, overflowY: 'auto', padding: '4px 0' } },
        data.loading && h('p', { className: 'hint', style: { textAlign: 'center', marginTop: 20 } }, 'Загружаю переписку…'),
        !data.loading && data.messages.length === 0 && h('div', { className: 'empty' },
          icon('chat', { size: 46 }), h('div', null, 'Сообщений пока нет.', h('br'), 'Напишите мастеру — он ответит здесь.')),
        data.messages.map(function (m, i) {
          // Реакция приезжает в переписку отдельным сообщением из одного эмодзи, привязанным
          // к тому, на что её поставили: цитата сверху, сам эмодзи — крупно.
          var solo = window.PS_SoloEmoji(m.text) && !m.image;
          return h('div', { key: i, style: { display: 'flex', justifyContent: m.mine ? 'flex-end' : 'flex-start', margin: '7px 0' } },
            h('div', { style: { maxWidth: '82%', padding: '9px 13px', borderRadius: 15,
              background: m.mine ? 'var(--accent)' : 'var(--surface)', color: m.mine ? '#1a0d02' : 'var(--text)',
              border: m.mine ? 0 : '1px solid var(--line)',
              borderBottomRightRadius: m.mine ? 5 : 15, borderBottomLeftRadius: m.mine ? 15 : 5, fontSize: 14, lineHeight: 1.4 } },
              h(window.PS_ChatQuote, { q: m.reply }),
              solo ? h('div', { style: { fontSize: 30, lineHeight: 1.15 } }, m.text) : m.text,
              h('div', { style: { fontSize: 10, opacity: .6, textAlign: 'right', marginTop: 3 } },
                m.at, h(window.PS_ChatRead, { mine: m.mine, read: m.read }))));
        })),
      h('div', { style: { display: 'flex', gap: 8, paddingTop: 8 } },
        h('input', { className: 'input', placeholder: 'Сообщение…', value: text, disabled: sending,
          onChange: function (e) { setText(e.target.value); },
          onKeyDown: function (e) { if (e.key === 'Enter') send(); } }),
        h('button', { className: 'btn', style: { width: 'auto', padding: '0 16px', marginTop: 0 }, disabled: sending || !text.trim(), onClick: send },
          sending ? '…' : icon('share', { size: 18 }))));
  }

  // ───────────────────── PROFILE (OpenTodo 7a5be39c) ─────────────────────
  // Подтверждение цены (OpenTodo 0d11db78). Мастер иногда поднимает цену уже после заказа —
  // например, слайсер насчитал больше пластика. По умолчанию такое всегда идёт клиенту на
  // подтверждение, и заказ стоит, пока тот не откроет приложение. Кому эта пауза дороже
  // копеечной разницы, здесь выбирает: принимать всё или принимать мелкие подорожания.
  // Сохраняем сразу по выбору: экран настроек без кнопки «Сохранить» не обманывает ожиданий.
  function PriceConfirm(props) {
    var app = props.app, p0 = props.prefs || {};
    var ms = useState(p0.price_mode || 'ask'); var mode = ms[0], setMode = ms[1];
    var ps = useState(String(p0.price_pct || 10)); var pct = ps[0], setPct = ps[1];

    function save(nextMode, nextPct) {
      API.savePrefs(nextMode, nextPct).then(function (j) {
        if (!j || !j.ok) { if (app) app.toast((j && j.error) || 'Не сохранилось'); return; }
        // сервер нормализует значения (порог 0 → «всегда спрашивать») — берём его ответ
        var pr = j.prefs || {};
        setMode(pr.price_mode || nextMode);
        if (pr.price_pct != null) setPct(String(pr.price_pct));
        if (app) app.toast('Настройка сохранена');
      }).catch(function () { if (app) app.toast('Сеть недоступна'); });
    }

    function row(key, title, sub) {
      var on = mode === key;
      return h('button', { className: 'vrow', onClick: function () { setMode(key); save(key, Number(String(pct).replace(',', '.')) || 0); },
        style: on ? { background: 'var(--surface-2)' } : null },
        h('div', { className: 'ic', style: on ? { background: 'var(--accent)', color: '#1a0d02' } : null },
          icon(on ? 'check' : 'coin', { size: 18 })),
        h('div', { className: 'grow' }, h('div', { className: 'tt' }, title),
          h('div', { className: 'ss' }, sub)));
    }

    return h(React.Fragment, null,
      h(Lab, null, 'Подтверждение цены'),
      h('div', { className: 'vlist' },
        row('ask', 'Всегда подтверждать', 'Любое изменение цены жду от вас'),
        row('auto', 'Всегда принимать', 'Заказ сразу уходит в работу, вам придёт уведомление'),
        row('pct', 'Принимать небольшие', 'Без вопросов, если цена выросла меньше чем на…')),
      mode === 'pct' && h('div', { className: 'card', style: { padding: 14, marginTop: 8, display: 'flex', alignItems: 'center', gap: 10 } },
        h('input', { className: 'input mono', style: { flex: '0 0 84px', marginTop: 0 }, inputMode: 'decimal',
          value: pct, onChange: function (e) { setPct(e.target.value); },
          onBlur: function () { save('pct', Number(String(pct).replace(',', '.')) || 0); } }),
        h('div', { style: { fontSize: 12.5, color: 'var(--muted)' } },
          '% — до этого порога цена принимается сама. Дороже — спросим, как обычно.')));
  }

  // Доставка по умолчанию (A2): тип + адрес, заданные один раз. Подставляются в корзину и —
  // главное — применяются к заказу БЕЗ корзины, где спросить адрес негде. Секции нет вовсе,
  // если мастерская не завела ни одного типа доставки (работает только на самовывоз).
  function DeliveryDefault(props) {
    var app = props.app, p0 = props.prefs || {};
    var cfg = (app.options && app.options.delivery) || {};
    var types = cfg.types || [];
    var ts = useState(p0.delivery_type_id || 0); var typeId = ts[0], setTypeId = ts[1];
    var as = useState(p0.delivery_address || ''); var addr = as[0], setAddr = as[1];

    function save(nextType, nextAddr) {
      API.saveDeliveryPrefs(nextType, nextAddr).then(function (j) {
        if (!j || !j.ok) { if (app) app.toast((j && j.error) || 'Не сохранилось'); return; }
        var pr = j.prefs || {};                 // сервер мог отбросить выключенный тип
        setTypeId(pr.delivery_type_id != null ? pr.delivery_type_id : nextType);
        if (pr.delivery_address != null) setAddr(pr.delivery_address);
        if (app) app.toast(nextAddr ? 'Адрес сохранён' : 'Адрес очищен');
      }).catch(function () { if (app) app.toast('Сеть недоступна'); });
    }

    if (cfg.enabled === false) return null;
    return h(React.Fragment, null,
      h(Lab, null, 'Доставка по умолчанию'),
      h('p', { className: 'hint', style: { marginTop: 0 } },
        'Подставим в корзину и применим к заказу в один тап — не придётся диктовать адрес каждый раз.'),
      h('div', { className: 'card', style: { padding: 14, marginTop: 8 } },
        types.length > 0 && h('div', { style: { display: 'grid', gap: 6 } },
          types.map(function (t) {
            var on = t.id === typeId;
            return h('button', { key: t.id, type: 'button',
              onClick: function () { setTypeId(t.id); save(t.id, addr); },
              style: { display: 'flex', alignItems: 'center', gap: 10, width: '100%', textAlign: 'left',
                padding: '10px 12px', borderRadius: 12, cursor: 'pointer',
                border: '1px solid ' + (on ? 'var(--accent)' : 'var(--line-2)'),
                background: on ? 'var(--accent-soft)' : 'var(--surface-2)', color: 'var(--text)' } },
              h('span', { style: { flex: 1, minWidth: 0 } },
                h('b', { style: { fontSize: 14 } }, t.name),
                t.eta && h('span', { className: 'sub', style: { display: 'block', marginTop: 2 } }, t.eta)),
              h('b', { className: 'mono', style: { color: 'var(--price)', flex: '0 0 auto' } },
                API.fmt(t.price || 0)));
          })),
        h('input', { className: 'input', style: { marginTop: types.length ? 10 : 0 },
          placeholder: 'Адрес доставки', value: addr,
          onChange: function (e) { setAddr(e.target.value); },
          onBlur: function () { save(typeId, addr.trim()); } }),
        h('p', { className: 'hint', style: { marginTop: 8 } },
          typeId && addr.trim()
            ? 'Заказы будут уезжать сюда. Стоимость доставки добавится к сумме заказа.'
            : 'Выберите тип и укажите адрес — иначе заказ оформится как самовывоз.'),
        (typeId || addr) && h('button', { className: 'btn line', style: { marginTop: 10 },
          onClick: function () { setTypeId(0); setAddr(''); save(0, ''); } },
          icon('close', { size: 15 }), ' Убрать адрес — забираю сам')));
  }

  function Profile(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var gs = useState(false); var gen = gs[0], setGen = gs[1];   // идёт генерация аватарки
    var grS = useState(false); var grow = grS[0], setGrow = grS[1];   // анимация прогресс-баров при входе
    // оверлей «будим ИИ» при долгой генерации (a2e67f96). ВАЖНО: хук ДО раннего return
    // «Загружаю…» — иначе на втором рендере хуков больше, чем на первом, и React роняет
    // всё приложение (чёрный экран при открытии профиля).
    var aiS = useState(false); var aiWake = aiS[0], setAiWake = aiS[1];
    var azS = useState(false); var avZoom = azS[0], setAvZoom = azS[1];   // аватар на весь экран (e99fe54b)
    var celebrated = useRef(false);
    // inline-форма семейного счёта (вместо window.prompt)
    var foS = useState(false); var famOpen = foS[0], setFamOpen = foS[1];
    var fnS = useState(''); var famName = fnS[0], setFamName = fnS[1];
    var fbS = useState(false); var famBusy = fbS[0], setFamBusy = fbS[1];
    // архив личной истории у члена семьи (общий счёт заменяет личный баланс)
    var faS = useState(false); var famArch = faS[0], setFamArch = faS[1];

    useEffect(function () {
      API.profile({ seen: 1 }).then(function (j) { if (j.ok) setData(j); });
      // сбрасываем «непрочитанные» — точку на чипе в шапке
      if (app && app.setHeaderProfile) app.setHeaderProfile(function (p) { return p ? Object.assign({}, p, { unseen: 0 }) : p; });
    }, []);

    // запускаем рост полосок чуть после монтирования
    useEffect(function () { if (!data) return; var t = setTimeout(function () { setGrow(true); }, 80); return function () { clearTimeout(t); }; }, [!!data]);

    // празднуем только что открытые достижения (однократно)
    useEffect(function () {
      if (!data || celebrated.current) return;
      var fresh = (data.achievements || []).filter(function (a) { return a.is_new; });
      if (fresh.length) { celebrated.current = true; if (app) app.toast('🎉 Новое достижение: ' + fresh[0].title); }
    }, [!!data]);

    if (!data) return h('div', { className: 'screen' }, h('p', { className: 'hint', style: { marginTop: 4 } }, 'Загружаю…'));
    var st = data.stats || {};

    function statCell(label, val, target) {
      var tap = target && app && app.go;
      return h('div', { className: tap ? 'stat-tap' : null, onClick: tap ? function () { app.go(target); } : null },
        h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 4 } },
          label, tap && h('span', { className: 'stat-go', style: { display: 'flex' } }, icon('chev', { size: 12 }))),
        h('div', { style: { fontSize: 19, fontWeight: 700, marginTop: 2 } }, val));
    }
    function money2(v) { return (Number(v) || 0).toFixed(2).replace('.', ',') + ' ' + (data.currency || ''); }
    // строка истории операций: работает и для личных (balance_tx), и для семейных (family.tx —
    // с подписью «чьё действие»; номер заказа уже в reason «Оплата заказа #N»)
    function txRow(t, i) {
      var d = t.at ? new Date(t.at * 1000) : null;
      var dl = d ? ('0' + d.getDate()).slice(-2) + '.' + ('0' + (d.getMonth() + 1)).slice(-2) : '';
      return h('div', { key: t.id || i, style: { padding: '6px 0', borderTop: i ? '1px solid var(--line)' : 'none' } },
        h('div', { className: 'spread', style: { fontSize: 12.5 } },
          h('span', { style: { color: 'var(--muted)', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
            dl && h('span', { className: 'mono', style: { color: 'var(--faint)' } }, dl + ' · '),
            t.reason || (t.amount >= 0 ? 'Пополнение' : 'Списание')),
          h('b', { className: 'mono', style: { flex: '0 0 auto', marginLeft: 8, color: t.amount >= 0 ? 'var(--price)' : 'var(--red)' } }, (t.amount >= 0 ? '+' : '−') + money2(Math.abs(t.amount)))),
        // семейная операция: чьё это действие (пополнения мастером идут без имени)
        t.user_name && h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } }, '👤 ' + t.user_name),
        // заметка мастера к транзакции (ccb1e4b6)
        t.comment && h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontStyle: 'italic', marginTop: 2 } }, '💬 ' + t.comment));
    }
    // Долгая генерация = ИИ просыпается после выгрузки из VRAM (a2e67f96) — через 2.5 c
    // показываем забавный оверлей «будим мощнейший искусственный интеллект».
    function regen() {
      if (gen) return; setGen(true);
      var slowTimer = setTimeout(function () { setAiWake(true); }, 2500);
      API.avatarGenerate().then(function (j) {
        if (j && j.ok) {
          setData(function (d) { return Object.assign({}, d, { avatar_url: j.url, avatar_prompt: j.prompt }); });
          // чип в шапке обновляем сразу (OpenTodo b8c0d794) — иначе новая аватарка
          // появлялась там только после перезапуска приложения
          if (app && app.setHeaderProfile) app.setHeaderProfile(function (p) { return p ? Object.assign({}, p, { avatar_url: j.url }) : p; });
        } else if (app) app.toast((j && j.error) || 'Не получилось');
      }).catch(function () { if (app) app.toast('Сеть недоступна'); }).then(function () {
        clearTimeout(slowTimer); setAiWake(false); setGen(false);
      });
    }
    // Нейро-лоадер: три слоя «нейронов», импульсы бегут по связям (анимация в app.css)
    function NeuroLoader() {
      var L = [[20, 30], [20, 56], [20, 82], [66, 22], [66, 43], [66, 64], [66, 85], [112, 43], [112, 64]];
      var E = [[0, 3], [0, 4], [1, 4], [1, 5], [2, 5], [2, 6], [3, 7], [4, 7], [4, 8], [5, 8], [6, 8]];
      return h('svg', { className: 'neuro', viewBox: '0 0 132 108', 'aria-hidden': true },
        E.map(function (e, i) {
          var a = L[e[0]], b = L[e[1]];
          return h('line', { key: 'e' + i, className: 'nn-e', x1: a[0], y1: a[1], x2: b[0], y2: b[1],
            style: { animationDelay: (i * 0.18) + 's' } });
        }),
        L.map(function (p, i) {
          return h('circle', { key: 'n' + i, className: 'nn-n', cx: p[0], cy: p[1], r: 7,
            style: { animationDelay: (i * 0.22) + 's' } });
        }));
    }

    // ── «Похвастаться»: собрать картинку-карточку профиля на canvas ──
    function rr(c, x, y, w, hh, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r); c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
    function shareCard() {
      try {
        var W = 1080, HH = 1080;
        // Карточка уходит в чужие чаты — это витрина мастерской, а не наша. Имя и цвет
        // берём из бренда (A3, white-label); на canvas CSS-переменных нет, поэтому цвет
        // подставляем строкой.
        var BR = window.__BRAND__ || {};
        var BRN = BR.stock === false && BR.title ? BR.title : (BR.wordmark || ['OPEN', 'PRINT', 'SHOP']).join('·');
        var BRC = BR.accent || '#ff7a1a';
        var cv = document.createElement('canvas'); cv.width = W; cv.height = HH;
        var c = cv.getContext('2d');
        function paint(avImg) {
          c.fillStyle = '#0b0e13'; c.fillRect(0, 0, W, HH);
          c.fillStyle = '#141a22'; rr(c, 56, 56, W - 112, HH - 112, 44); c.fill();
          c.fillStyle = BRC; c.font = '700 34px ui-monospace, Menlo, monospace'; c.textAlign = 'left'; c.fillText(BRN, 112, 152);
          var cx = W / 2, cy = 390, r = 132;
          c.save(); c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2); c.closePath();
          if (avImg) { c.clip(); c.drawImage(avImg, cx - r, cy - r, r * 2, r * 2); c.restore(); }
          else { c.fillStyle = BRC; c.fill(); c.restore(); c.fillStyle = '#1a0d02'; c.font = '800 128px system-ui, sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText(String(data.level), cx, cy + 4); c.textBaseline = 'alphabetic'; }
          c.textAlign = 'center';
          c.fillStyle = '#ffffff'; c.font = '800 66px system-ui, sans-serif'; c.fillText(data.level_title, cx, 606);
          c.fillStyle = '#8a94a3'; c.font = '500 34px system-ui, sans-serif'; c.fillText('Уровень ' + data.level + ' · ' + data.xp + ' XP', cx, 660);
          var bx = 150, by = 724, bw = W - 300, bh = 26;
          c.fillStyle = '#1f2630'; rr(c, bx, by, bw, bh, 13); c.fill();
          var fillw = Math.max(bh, bw * (Math.max(2, data.progress_pct) / 100));
          c.fillStyle = BRC; rr(c, bx, by, fillw, bh, 13); c.fill();
          var stats = [['Заказов', st.orders || 0], ['Напечатано', st.printed || 0], ['Достижений', data.unlocked + '/' + data.total]];
          var colw = (W - 224) / stats.length;
          stats.forEach(function (sp, i) { var x = 112 + colw * i + colw / 2;
            c.fillStyle = '#ffffff'; c.font = '800 60px system-ui, sans-serif'; c.fillText(String(sp[1]), x, 884);
            c.fillStyle = '#8a94a3'; c.font = '500 30px system-ui, sans-serif'; c.fillText(sp[0], x, 932);
          });
          c.fillStyle = '#5a6472'; c.font = '500 28px system-ui, sans-serif';
          c.fillText('Считаю 3D-печать в ' + ((BR.stock === false && BR.title) || 'Print Studio'), cx, HH - 72);
          cv.toBlob(function (b) {
            if (!b) { if (app) app.toast('Не удалось собрать карточку'); return; }
            var file = new File([b], 'print-studio-profile.png', { type: 'image/png' });
            if (navigator.canShare && navigator.canShare({ files: [file] })) { navigator.share({ files: [file], title: 'Мой профиль Print Studio' }).then(function () { if (app) app.toast('Карточка отправлена'); }).catch(function () {}); }
            else { var url = URL.createObjectURL(b); var a = document.createElement('a'); a.href = url; a.download = 'print-studio-profile.png'; document.body.appendChild(a); a.click(); a.remove(); setTimeout(function () { URL.revokeObjectURL(url); }, 1000); if (app) app.toast('Карточка сохранена'); }
          }, 'image/png');
        }
        if (data.avatar_url) { var im = new Image(); im.crossOrigin = 'anonymous'; im.onload = function () { paint(im); }; im.onerror = function () { paint(null); }; im.src = data.avatar_url; }
        else { paint(null); }
      } catch (e) { if (app) app.toast('Не удалось собрать карточку'); }
    }

    // ── сортировка достижений: новые → близкие к разблокировке → остальные открытые ──
    var achs = (data.achievements || []).slice().sort(function (a, b) {
      function rank(x) { if (x.is_new) return 0; if (!x.unlocked) return 1; return 2; }
      var ra = rank(a), rb = rank(b);
      if (ra !== rb) return ra - rb;
      if (ra === 1) return (b.have / b.need) - (a.have / a.need);
      return 0;
    });

    return h('div', { className: 'screen' },
      // ── identity ──
      h('div', { className: 'card', style: { padding: 16, marginTop: 4 } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 13 } },
          h('div', { style: { position: 'relative', flex: '0 0 auto', width: 58, height: 58 } },
            (data.avatar_url
              ? h('img', { src: data.avatar_url, alt: '', style: { width: 58, height: 58, borderRadius: 16, objectFit: 'cover', display: 'block', cursor: 'zoom-in' },
                  onClick: function () { setAvZoom(true); } })   // тап → на весь экран + скачать (e99fe54b)
              : h('div', { style: { width: 58, height: 58, borderRadius: 16, background: 'var(--accent)', color: '#1a0d02', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 800, fontSize: 24 } }, data.level)),
            data.avatar_url && h('span', { className: 'lvl-badge' }, data.level)),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontWeight: 700, fontSize: 18 } }, data.level_title),
            h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 3 } }, 'Уровень ' + data.level + (data.next_title ? (' · до «' + data.next_title + '»') : ' · максимум')))),
        h('div', { className: 'pbar grow', style: { height: 8, marginTop: 13 } }, h('i', { style: { width: (grow ? data.progress_pct : 0) + '%' } })),
        h('div', { className: 'spread', style: { fontSize: 11, color: 'var(--faint)', marginTop: 5, fontFamily: 'var(--mono)' } },
          h('span', null, data.xp + ' XP'),
          data.xp_next && h('span', null, data.xp_next + ' XP')),
        data.next_perk && h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, marginTop: 11, padding: '9px 11px', borderRadius: 10, background: 'var(--surface-2)', fontSize: 12.5 } },
          h('span', { style: { color: 'var(--accent)', flex: '0 0 auto', display: 'flex' } }, icon('gift', { size: 16 })),
          h('span', { style: { color: 'var(--muted)' } }, 'Следующий уровень: ', h('b', { style: { color: 'var(--text)' } }, data.next_perk))),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 13 } },
          h('button', { className: 'btn soft', style: { marginTop: 0, flex: 1 }, disabled: gen, onClick: regen },
            icon(gen ? 'refresh' : 'sparkle', { size: 17 }), gen ? ' Рисую…' : (data.avatar_url ? ' Обновить аватар' : ' Аватар')),
          h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 16px' }, onClick: shareCard, title: 'Похвастаться' },
            icon('share', { size: 17 }), ' Похвастаться')),
        data.avatar_prompt && h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 9, textAlign: 'center', fontStyle: 'italic' } }, '«' + data.avatar_prompt + '»')),

      // оверлей «будим ИИ» (a2e67f96): модель выгрузилась по простою и грузится заново
      // тап по фону закрывает оверлей (аудит UX: при сетевом сбое юзер был заперт навсегда)
      aiWake && h('div', { onClick: function () { setAiWake(false); },
        style: { position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(6,9,12,.85)', backdropFilter: 'blur(6px)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 14, padding: 30 } },
        h(NeuroLoader, null),
        h('div', { style: { fontWeight: 700, fontSize: 15.5, textAlign: 'center' } }, 'Будим мощнейший искусственный интеллект…'),
        h('div', { style: { fontSize: 12.5, color: 'var(--muted)', textAlign: 'center', maxWidth: 260, lineHeight: 1.45 } }, 'Он спал, чтобы не занимать видеокарту. Загрузка занимает до полуминуты — зато рисует за секунду.')),

      // аватар на весь экран + скачать (e99fe54b)
      h(window.PS_Lightbox, { src: avZoom ? data.avatar_url : '', name: 'print-studio-avatar.png',
        onClose: function () { setAvZoom(false); } }),

      h(Lab, null, data.would_spend != null ? 'Печать для себя' : (data.family ? 'Семейный счёт' : 'Баланс')),
      data.would_spend != null
        // Мастеру вместо баланса — «было бы потрачено» (сумма самопечатей), синим (мастер-план).
        ? h('div', { className: 'card', style: { padding: 14 } },
            h('div', { style: { fontSize: 12, color: 'var(--muted)', marginBottom: 4 } }, 'Было бы потрачено — печатай ты не на своём принтере'),
            h('div', { className: 'price', style: { fontSize: 26, color: '#3a86ff' } }, money2(data.would_spend)))
        : data.family
        // Семейный счёт (2026-07-16): общий баланс, участники, история с подписью
        // «чьё действие» и номером заказа. Личная история — свернута как архив.
        ? h('div', { className: 'card', style: { padding: 14 } },
            h('div', { className: 'price', style: { fontSize: 26, color: (Number(data.family.balance) < 0 ? 'var(--red)' : 'var(--price)') } }, money2(data.family.balance)),
            h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 4 } },
              '👨‍👩‍👧 Общий счёт: ' + (data.family.members || []).map(function (m) { return m.user_name || ('id ' + m.user_id); }).join(', ')),
            (data.family.tx || []).length === 0
              ? h('div', { style: { display: 'flex', gap: 9, alignItems: 'flex-start', marginTop: 8, color: 'var(--faint)', fontSize: 12.5, lineHeight: 1.45 } },
                  icon('coin', { size: 17, style: { flex: '0 0 auto', marginTop: 1 } }),
                  h('span', null, 'Здесь появятся операции по общему счёту — с именем и номером заказа.'))
              : h('div', { style: { marginTop: 10 } }, data.family.tx.map(txRow)),
            (data.balance_tx || []).length > 0 && h('button', { className: 'btn line', style: { marginTop: 12 },
              onClick: function () { setFamArch(!famArch); } }, famArch ? 'Скрыть личную историю' : 'Личная история (архив)'),
            famArch && (data.balance_tx || []).length > 0 && h('div', { style: { marginTop: 6 } }, data.balance_tx.map(txRow)))
        : h('div', { className: 'card', style: { padding: 14 } },
        h('div', { className: 'price', style: { fontSize: 26, color: (Number(data.balance) < 0 ? 'var(--red)' : 'var(--price)') } }, money2(data.balance)),
        (data.balance_tx || []).length === 0
          ? h('div', { style: { display: 'flex', gap: 9, alignItems: 'flex-start', marginTop: 8, color: 'var(--faint)', fontSize: 12.5, lineHeight: 1.45 } },
              icon('coin', { size: 17, style: { flex: '0 0 auto', marginTop: 1 } }),
              h('span', null, 'Мастер пополняет баланс — например, когда вы вносите оплату заранее. Остаток хранится на будущие заказы.'))
          : h('div', { style: { marginTop: 10 } }, data.balance_tx.map(txRow))),

      h(Lab, null, 'Статистика'),
      h('div', { className: 'card', style: { padding: 16, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 } },
        statCell('Заказов', st.orders || 0, 'orders'), statCell('Напечатано', st.printed || 0, 'orders'),
        statCell('Материалов', st.materials || 0, 'catalog'),
        statCell('Потрачено', Math.round(st.spent || 0) + ' ' + (data.currency || '')),
        statCell('Отзывов', st.reviews || 0), statCell('Друзей', st.referrals || 0, 'invite')),

      // «Ещё» — практичные переходы сразу после статистики (аудит UX: гейм- и промо-блоки ниже)
      h(Lab, null, 'Ещё'),
      h('div', { className: 'vlist' },
        h('button', { className: 'vrow', onClick: function () { app.go('help'); } },
          h('div', { className: 'ic' }, icon('info', { size: 18 })),
          h('div', { className: 'grow' }, h('div', { className: 'tt' }, 'Как это работает'),
            h('div', { className: 'ss' }, '4 шага от модели до сметы + предложить идею')),
          icon('chev', { size: 18, style: { color: 'var(--faint)' } })),
        h('button', { className: 'vrow', onClick: function () { app.go('custom'); } },
          h('div', { className: 'ic' }, icon('chat', { size: 18 })),
          h('div', { className: 'grow' }, h('div', { className: 'tt' }, 'Вопрос мастеру'),
            h('div', { className: 'ss' }, 'Опишите вопрос — мастер ответит в чате заявки')),
          icon('chev', { size: 18, style: { color: 'var(--faint)' } })),
        (app.options && app.options.is_admin) && h('button', { className: 'vrow dashed', onClick: function () { app.go('admin'); } },
          h('div', { className: 'ic' }, icon('sliders', { size: 18 })),
          h('div', { className: 'grow' }, h('div', { className: 'tt' }, 'Админ-панель'),
            h('div', { className: 'ss' }, 'Только для мастера')),
          icon('chev', { size: 18, style: { color: 'var(--faint)' } }))),

      h(PriceConfirm, { app: app, prefs: data.prefs }),
      h(DeliveryDefault, { app: app, prefs: data.prefs }),

      data.referral_link && h(Lab, null, 'Приглашайте друзей'),
      data.referral_link && h('div', { className: 'card', style: { padding: 16 } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 11 } },
          h('div', { style: { width: 38, height: 38, borderRadius: 11, flex: '0 0 auto', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--accent)', color: '#1a0d02' } }, icon('gift', { size: 20 })),
          h('div', { style: { minWidth: 0 } },
            h('div', { style: { fontWeight: 700, fontSize: 14 } }, '+' + data.referral_reward + ' за каждого друга'),
            h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 2 } }, 'Когда его первый заказ напечатан и оплачен — бонус вам обоим'))),
        h('button', { className: 'btn', style: { marginTop: 13 }, onClick: function () { if (app && app.shareLink) app.shareLink(data.referral_link); } },
          icon('share', { size: 17 }), ' Поделиться ссылкой')),

      // Семейный счёт (редизайн 2026-07-16): общий ТОЛЬКО баланс, участники равноправны,
      // всё остальное личное. Заявка уходит в центр уведомлений админки, мастер подтверждает
      // кнопкой. У членов семьи секция не показывается (их счёт — выше, вместо баланса).
      !data.family && h(Lab, null, 'Семейный счёт'),
      !data.family && h('div', { className: 'card', style: { padding: 16 } },
        h('div', { style: { fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 } },
          'Общий счёт для своих: баланс один на всех, в истории видно, чей заказ и на сколько. Заказы, модели и скидки у каждого остаются свои.' +
          (data.family_bonus ? ' За открытие — +' + data.family_bonus + ' на общий счёт (если реферальный бонус друг за друга ещё не получали).' : '')),
        famOpen && h('input', { className: 'input', style: { marginTop: 11 }, autoFocus: true,
          placeholder: '@username или имя близкого', value: famName,
          onChange: function (e) { setFamName(e.target.value); } }),
        // Нативный выбор из контактов Telegram (0528ba47): бот пришлёт в чат кнопку-пикер,
        // выбранный контакт станет заявкой с точным user_id партнёра.
        famOpen && h('button', { className: 'btn line', style: { marginTop: 10 }, disabled: famBusy, onClick: function () {
          setFamBusy(true);
          API.familyPickPartner().then(function (j) {
            setFamBusy(false);
            if (!j.ok) { if (app) app.toast(j.error || 'Не удалось'); return; }
            if (app) app.toast('Продолжите в чате с ботом — выберите близкого кнопкой');
            setTimeout(function () { try { window.Telegram.WebApp.close(); } catch (e) { /* вне Telegram — ок */ } }, 700);
          }).catch(function () { setFamBusy(false); if (app) app.toast('Сеть недоступна'); });
        } }, '👤 Выбрать из контактов Telegram'),
        h('button', { className: 'btn soft', style: { marginTop: 12 }, disabled: famBusy, onClick: function () {
          if (!famOpen) { setFamOpen(true); return; }
          setFamBusy(true);
          API.familyRequest(famName.trim()).then(function (j) {
            setFamBusy(false);
            if (j.ok) { setFamOpen(false); setFamName(''); }
            if (app) app.toast(j.ok ? 'Заявка у мастера — он подтвердит открытие счёта' : (j.error || 'Не удалось'));
          }).catch(function () { setFamBusy(false); if (app) app.toast('Сеть недоступна'); });
        } }, icon('heart', { size: 17 }), famBusy ? ' Отправляю…' : (famOpen ? ' Отправить заявку' : ' Открыть семейный счёт'))),

      (data.xp_log && data.xp_log.length) && h(Lab, null, 'Как начисляется XP'),
      (data.xp_log && data.xp_log.length) && h('div', { className: 'card', style: { padding: '6px 14px' } },
        data.xp_log.map(function (e, i) {
          return h('div', { key: i, className: 'spread', style: { fontSize: 12.5, padding: '9px 0', borderTop: i ? '1px solid var(--line)' : 'none' } },
            h('div', { style: { minWidth: 0 } },
              h('div', { style: { color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, e.reason),
              e.at && h('div', { style: { fontSize: 11, color: 'var(--faint)', marginTop: 1 } }, e.at)),
            h('b', { className: 'mono', style: { flex: '0 0 auto', marginLeft: 10, color: 'var(--accent)' } }, '+' + e.amount + ' XP'));
        })),

      h(Lab, null, 'Достижения · ' + data.unlocked + ' / ' + data.total),
      // Все плашки ОДНОГО размера (OpenTodo 96bc3c56): описание фиксировано на 2 строки
      // (кламп + minHeight), а блок мини-прогресса зарезервирован у всех — у открытых/
      // одношаговых он просто невидим (visibility:hidden), поэтому высота не плавает.
      // minmax(0,1fr) + minWidth:0 (OpenTodo 08908ccf): nowrap-заголовки задавали колонкам
      // min-content ширину — на узких телефонах правая колонка уезжала за экран.
      h('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', gap: 10 } },
        achs.map(function (a) {
          var pct = a.need > 1 ? Math.min(100, Math.round((a.have / a.need) * 100)) : 0;
          var showBar = !a.unlocked && a.need > 1;
          // margin:0 гасит глобальный «.card + .card { margin-top:12px }»: внутри грида он
          // доставался всем плашкам кроме первой и съедал их stretch-высоту (+12px у первой)
          return h('div', { key: a.key, className: 'card' + (a.is_new ? ' ach-new' : ''),
            style: { padding: 12, margin: 0, minWidth: 0, opacity: a.unlocked ? 1 : 0.5, display: 'flex', flexDirection: 'column' } },
            h('div', { style: { display: 'flex', alignItems: 'center', gap: 9 } },
              h('div', { style: { width: 34, height: 34, borderRadius: 10, flex: '0 0 auto', display: 'flex', alignItems: 'center', justifyContent: 'center', background: a.unlocked ? 'var(--accent)' : 'var(--surface-2)', color: a.unlocked ? '#1a0d02' : 'var(--faint)' } }, icon(a.icon, { size: 18 })),
              h('div', { style: { minWidth: 0 } }, h('div', { style: { fontWeight: 700, fontSize: 13.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, a.title, a.is_new && h('span', { className: 'ach-tag' }, 'NEW')))),
            // описание: ровно 2 строки (кламп + minHeight) — длина текста не меняет высоту
            h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 6, lineHeight: '16px',
              minHeight: 32, maxHeight: 32, overflow: 'hidden',
              display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' } }, a.desc),
            h('div', { style: { marginTop: 'auto', paddingTop: 8, visibility: showBar ? 'visible' : 'hidden' } },
              h('div', { className: 'pbar grow', style: { height: 5 } }, h('i', { style: { width: (grow && showBar ? pct : 0) + '%' } })),
              h('div', { style: { fontSize: 10.5, color: 'var(--faint)', marginTop: 4, fontFamily: 'var(--mono)' } }, showBar ? (a.have + ' / ' + a.need) : '·')));
        })));
  }

  // ───────────────────── Готовые изделия / витрина (OpenTodo bd896aa6) ─────────────────────
  // Клиент покупает уже напечатанный товар СРАЗУ по фикс-цене — без загрузки модели, слайса и
  // расчёта. Наличие списывается на сервере атомарно; распроданное показываем неактивным.
  function Products(props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    var zs = useState(''); var zoom = zs[0], setZoom = zs[1];
    var bs = useState(0); var buyingId = bs[0], setBuyingId = bs[1];
    function load() { API.products().then(function (j) { if (j && j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function fmt(n) { return (Number(n) || 0).toFixed(2).replace('.', ',') + ' ' + ((data && data.currency) || 'руб.'); }
    function buy(p) {
      if (!p.in_stock || buyingId) return;
      setBuyingId(p.id);
      API.productOrder(p.id, 1, '').then(function (j) {
        setBuyingId(0);
        if (j && j.ok) { app.toast('Заказ на «' + p.title + '» принят!'); if (app.reloadOrders) app.reloadOrders(); app.go('orders'); }
        else { app.toast((j && j.error) || 'Не удалось оформить'); load(); }
      }).catch(function () { setBuyingId(0); app.toast('Сеть недоступна'); });
    }
    if (!data) return h('div', { className: 'screen' }, h('p', { className: 'hint', style: { textAlign: 'center', marginTop: 24 } }, 'Загружаю витрину…'));
    var items = data.products || [];
    return h('div', { className: 'screen' },
      h('h1', { style: { fontSize: 18, fontWeight: 700, margin: '4px 2px 0' } }, 'Готовые изделия'),
      h('p', { className: 'hint', style: { margin: '4px 2px 0' } }, 'Уже напечатано — купите и заберите без ожидания.'),
      items.length === 0 && h('div', { className: 'card', style: { marginTop: 14, textAlign: 'center', color: 'var(--muted)', padding: 20 } },
        'Пока пусто. Загляните позже — мастер добавит товары.'),
      items.map(function (p) {
        var busy = buyingId === p.id;
        return h('div', { key: p.id, className: 'card', style: { padding: 0, overflow: 'hidden', marginTop: 12, opacity: p.in_stock ? 1 : 0.62 } },
          p.photo && h('img', { src: p.photo, alt: p.title, loading: 'lazy',
            style: { width: '100%', maxHeight: 240, objectFit: 'cover', display: 'block', cursor: 'zoom-in', background: 'var(--bg-2)' },
            onClick: function () { setZoom(p.photo); } }),
          h('div', { style: { padding: '13px 15px' } },
            h('div', { style: { fontWeight: 700, fontSize: 16 } }, p.title),
            p.description && h('div', { style: { fontSize: 13.5, color: 'var(--muted)', marginTop: 5, lineHeight: 1.45, whiteSpace: 'pre-wrap' } }, p.description),
            h('div', { className: 'spread', style: { marginTop: 11, alignItems: 'center' } },
              // персональная скидка клиента действует и на витрину (OpenTodo 737b0a20)
              (data.discount_pct > 0)
                ? h('span', null,
                    h('span', { className: 'price', style: { fontSize: 20 } }, fmt(p.price * (1 - data.discount_pct / 100))),
                    h('span', { style: { fontSize: 13, color: 'var(--faint)', textDecoration: 'line-through', marginLeft: 7 } }, fmt(p.price)),
                    h('span', { style: { fontSize: 12, color: 'var(--accent)', marginLeft: 6 } }, '−' + data.discount_pct + '%'))
                : h('span', { className: 'price', style: { fontSize: 20 } }, fmt(p.price)),
              p.in_stock
                ? h('span', { style: { fontSize: 12.5, color: 'var(--accent)' } }, 'в наличии: ' + p.stock + ' шт')
                : h('span', { style: { fontSize: 12.5, color: 'var(--faint)' } }, 'нет в наличии')),
            h('button', { className: 'btn', style: { marginTop: 12 }, disabled: !p.in_stock || busy,
              onClick: function () { buy(p); } },
              icon('cart', { size: 18 }), ' ', p.in_stock ? (busy ? 'Оформляю…' : 'Купить') : 'Нет в наличии')));
      }),
      h(window.PS_Lightbox, { src: zoom, name: zoom ? 'product.jpg' : '', onClose: function () { setZoom(''); } }));
  }

  Object.assign(window, { Orders: Orders, Cart: Cart, Collection: Collection, MyModels: MyModels, Catalog: Catalog, Help: Help, Chat: Chat, CustomOrder: CustomOrder, Profile: Profile, Products: Products });
})();
