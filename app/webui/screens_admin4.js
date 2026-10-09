// screens_admin4.js — экраны мобильной админки, добавленные ради паритета с настольной
// (2026-09-02, задача владельца «привести телефонную админку к ПК виду»): План недели,
// Настройки студии, Сообщество, Калибровка. Иерархия разделов — в AdminMore (screens_admin.js),
// она повторяет NAV настольной админки (desk/src/shell.jsx). Экраны здесь — мобильные версии
// desk/src/screens/{plan,settings,community,calib}.jsx: те же данные и ручки, компактная вёрстка.
// Файл едет с остальными screens_admin*.js лениво (app.js ADMIN_SCRIPTS), сторожевой глобал — AdminPlan.
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, Lab = window.PS_Lab, API = window.API;

  function loading(title) { return h('div', { className: 'screen' }, h(Lab, null, title), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…')); }
  function refresh(fn) { return h('button', { className: 'chip', type: 'button', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: fn }, icon('refresh', { size: 14 }), ' Обновить'); }
  function failed(title, err, fn) {
    return h('div', { className: 'screen' }, h(Lab, { right: refresh(fn) }, title),
      h('div', { className: 'empty' }, icon('info', { size: 46 }), h('div', null, err || 'Недоступно')));
  }
  function plu(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    return b > 1 && b < 5 ? few : (b === 1 ? one : many);
  }
  var MB = function (b) { return (Number(b || 0) / 1048576).toFixed(1) + ' МБ'; };
  function dateShort(ts) {
    if (!ts) return '';
    var d = new Date(Number(ts) * 1000);
    return ('0' + d.getDate()).slice(-2) + '.' + ('0' + (d.getMonth() + 1)).slice(-2);
  }
  // Ссылка наружу из мини-аппа: t.me — через клиент Telegram, прочее — openLink, в браузере — новая вкладка
  function openLink(url) {
    var tg = window.Telegram && window.Telegram.WebApp;
    var abs = url;
    try { abs = new URL(url, window.location.href).href; } catch (e) {}
    try {
      if (tg && /^https?:\/\/t\.me\//.test(abs) && tg.openTelegramLink) { tg.openTelegramLink(abs); return; }
      if (tg && tg.openLink) { tg.openLink(abs); return; }
    } catch (e) {}
    window.open(abs, '_blank');
  }
  // Сохранение одной настройки (webapp: kind обязателен — без него сервер молча отвечает
  // «Неизвестный параметр»). Возвращает промис с булевым «получилось».
  function saveSetting(app, key, kind, value) {
    var v = value;
    if (kind === 'num') {
      v = Number(String(value).replace(',', '.'));
      if (isNaN(v)) { app.toast('Нужно число'); return Promise.resolve(false); }
    }
    return API.adminSetting({ key: key, kind: kind, value: v }).then(function (j) {
      app.toast(j && j.ok ? 'Сохранено' : ((j && j.error) || 'Не сохранилось'));
      return !!(j && j.ok);
    }).catch(function () { app.toast('Сеть недоступна'); return false; });
  }

  // ═══════════════════════ ПЛАН НЕДЕЛИ (desk plan.jsx) ═══════════════════════
  // Дедлайны заказов против мощности принтера: ~CAP_HOURS часов печати в сутки (ночная печать
  // без присмотра — норма, но с запасом на смену катушки и снятие детали). Минуты берём из
  // очереди — там уже посчитано время с учётом количества.
  var CAP_HOURS = 14, DOW = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  function parseDue(due) {   // «ДД.ММ» → Date в текущем (или следующем) году
    var m = /^(\d{1,2})\.(\d{1,2})$/.exec(String(due || '').trim());
    if (!m) return null;
    var now = new Date();
    var d = new Date(now.getFullYear(), Number(m[2]) - 1, Number(m[1]));
    if (d < now && (now - d) > 182 * 86400000) d = new Date(now.getFullYear() + 1, Number(m[2]) - 1, Number(m[1]));
    return d;
  }
  function dkey(d) { return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(); }
  function dm(d) { return ('0' + d.getDate()).slice(-2) + '.' + ('0' + (d.getMonth() + 1)).slice(-2); }
  function hrs(m) { return (m / 60).toFixed(1).replace('.0', ''); }

  function AdminPlan(props) {
    var app = props.app;
    var qs = useState(null); var queue = qs[0], setQueue = qs[1];
    var os = useState(null); var orders = os[0], setOrders = os[1];
    function load() {
      API.adminQueue().then(function (j) { setQueue((j && j.ok && j.queue) || []); }).catch(function () { setQueue([]); });
      API.adminOrders().then(function (j) { setOrders((j && j.orders) || []); }).catch(function () { setOrders([]); });
    }
    useEffect(function () { load(); var iv = setInterval(load, 20000); return function () { clearInterval(iv); }; }, []);
    if (!orders) return loading('План недели');
    var days = [], t = new Date();
    for (var i = 0; i < 7; i++) days.push(new Date(t.getFullYear(), t.getMonth(), t.getDate() + i));
    var mins = {};
    (queue || []).forEach(function (x) { mins[x.id] = Number(x.minutes) || 0; });
    var active = orders.filter(function (o) { return ['printed', 'done', 'handed', 'rejected', 'cancelled'].indexOf(o.status) < 0; });
    var byDay = {}, none = [];
    days.forEach(function (d) { byDay[dkey(d)] = []; });
    active.forEach(function (o) {
      var d = parseDue(o.due), k = d ? dkey(d) : null;
      if (k && byDay[k]) byDay[k].push(o); else none.push(o);
    });
    function setDue(o, d) {
      var due = d ? dm(d) : '';
      API.adminOrderStatus(o.id, o.status, due, '', '').then(function (j) {
        if (j && j.ok) { app.toast(due ? ('Срок заказа #' + o.id + ': ' + due) : ('Срок заказа #' + o.id + ' снят')); load(); }
        else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function orow(o, inDay) {
      return h('div', { key: o.id, className: 'plan-o', onClick: function () { app.openAdminOrder(o.id); } },
        o.preview ? h('img', { src: o.preview, alt: '' }) : h('span', { className: 'ph' }),
        h('span', { style: { minWidth: 0, flex: 1 } },
          h('div', { className: 'nm' }, '#' + o.id + ' · ' + (o.label || '—')),
          h('div', { className: 'ds' }, (mins[o.id] ? '≈ ' + hrs(mins[o.id]) + ' ч · ' : '') + (o.user || '—'))),
        inDay
          ? h('button', { className: 'x', type: 'button', 'aria-label': 'Снять срок',
            onClick: function (e) { e.stopPropagation(); setDue(o, null); } }, icon('close', { size: 14 }))
          // срок ставится и в карточке заказа, но список уже отсортирован «без срока» —
          // выпадашка экономит переход туда-обратно на каждый заказ (как на ПК)
          : h('select', { className: 'input', value: '', style: { width: 'auto', marginTop: 0, padding: '6px 8px', fontSize: 12 },
            onClick: function (e) { e.stopPropagation(); },
            onChange: function (e) { var i = e.target.value; if (i !== '') setDue(o, days[Number(i)]); } },
            h('option', { value: '' }, 'на день…'),
            days.map(function (d, i) { return h('option', { key: i, value: i }, DOW[d.getDay()] + ' ' + dm(d)); })));
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'План недели'),
      h('p', { className: 'hint', style: { marginTop: 0 } },
        'Дедлайны заказов против мощности принтера: ≈ ' + CAP_HOURS + ' ч печати в день. Цвет полосы — единственный сигнал перегруза.'),
      days.map(function (d, di) {
        var list = byDay[dkey(d)];
        var load_h = list.reduce(function (s, o) { return s + (mins[o.id] || 0); }, 0) / 60;
        var over = load_h > CAP_HOURS;
        return h('div', { key: dkey(d), className: 'plan-day' + (di === 0 ? ' today' : '') },
          h('div', { className: 'hd' }, h('b', null, DOW[d.getDay()]), h('span', { className: 'dt' }, dm(d)),
            di === 0 && h('span', { className: 'pill amber' }, 'сегодня'),
            h('span', { className: 'ld' + (over ? ' over' : '') }, (load_h ? load_h.toFixed(1).replace('.0', '') : 0) + ' ч из ' + CAP_HOURS + (over ? ' · перегруз' : ''))),
          h('div', { className: 'plan-bar' }, h('i', { style: { width: Math.min(100, load_h / CAP_HOURS * 100) + '%',
            background: over ? 'var(--red)' : (load_h > CAP_HOURS * 0.7 ? 'var(--amber)' : 'var(--price)') } })),
          list.length ? list.map(function (o) { return orow(o, true); }) : h('div', { className: 'plan-empty' }, 'свободно'));
      }),
      h(Lab, null, 'Без срока · ' + none.length),
      none.length ? none.map(function (o) { return orow(o, false); })
        : h('p', { className: 'hint' }, 'Все активные заказы со сроком.'));
  }

  // ═══════════════════════ НАСТРОЙКИ СТУДИИ (desk settings.jsx) ═══════════════════════
  // «Настройки» = поведение бота (слайс/санация, догон, срочность, DFM), бренд, оплата, доступ,
  // возможности сборки, бэкапы — отдельно от денег: ставки и наценка остаются в «Ценах».

  // строка «подпись · поле · галочка»: сохраняется по кнопке, подсветка — есть несохранённое
  function SetLine(props) {
    var vs = useState(null); var draft = vs[0], setDraft = vs[1];   // null = не правили
    var base = props.value == null ? '' : String(props.value);
    var cur = draft === null ? base : draft;
    var dirty = draft !== null && draft !== base;
    return h('div', { className: 'setline' },
      h('span', { className: 'sl' }, props.label),
      h('input', { className: 'input' + (props.wide ? ' wide' : '') + (props.kind === 'num' ? ' mono' : ''), value: cur,
        placeholder: props.placeholder || '', inputMode: props.kind === 'num' ? 'decimal' : undefined,
        onChange: function (e) { setDraft(e.target.value); } }),
      h('button', { className: 'ibtn' + (dirty ? ' dirty' : ''), type: 'button', disabled: !dirty, 'aria-label': 'Сохранить',
        onClick: function () { props.onSave(cur).then(function (ok) { if (ok) setDraft(null); }); } }, icon('check', { size: 15 })));
  }
  // тумблер 0/1 — сохраняет сразу, отдельная кнопка была бы лишним шагом
  function Tgl(props) {
    return h('div', { className: 'tgl' },
      h('span', { className: 'ic' }, icon(props.ic || 'sliders', { size: 17 })),
      h('div', { className: 'meta' }, h('div', { className: 't' }, props.label), props.sub && h('div', { className: 's' }, props.sub)),
      h('div', { className: 'sw' + (props.on ? ' on' : ''), role: 'switch', 'aria-checked': !!props.on,
        onClick: function () { props.onChange(!props.on); } }, h('i', null)));
  }
  // группа text-настроек (оплата, сообщество владельцев): порядок и подписи — серверные
  function TextGroup(props) {
    var g = props.group;
    if (!g || !(g.order || []).length) return null;
    var vals = g.values || {}, labels = g.labels || {};
    return h(React.Fragment, null,
      h(Lab, null, props.title),
      h('div', { className: 'card', style: { padding: '2px 13px' } },
        g.order.map(function (k) {
          return h(SetLine, { key: k, label: labels[k] || k, value: vals[k], kind: 'text', wide: true, placeholder: props.placeholder,
            onSave: function (v) { return saveSetting(props.app, k, 'text', v).then(function (ok) { if (ok) props.onSaved(); return ok; }); } });
        })),
      props.hint && h('p', { className: 'hint' }, props.hint));
  }
  // Бренд мастерской (A3, white-label): предпросмотр шапки + поля + логотип файлом.
  // Изменения видны после перезагрузки мини-аппа — бренд вшивается сервером в разметку.
  function BrandGroup(props) {
    var app = props.app, g = props.group, cur = props.current || {};
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    var fileRef = useRef(null);
    if (!g) return null;
    function done(j, okMsg) {
      setBusy(false);
      app.toast(j && j.ok ? okMsg : ((j && j.error) || 'Не удалось'));
      if (j && j.ok) props.onSaved();
    }
    function upload(f) {
      if (!f) return;
      setBusy(true);
      API.adminBrandLogo(f).then(function (j) { done(j, 'Логотип загружен'); }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function clear() {
      setBusy(true);
      API.adminBrandLogoClear().then(function (j) { done(j, 'Логотип снят'); }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    return h(React.Fragment, null,
      h(Lab, null, 'Бренд мастерской'),
      // предпросмотр: ровно то, что увидит клиент в шапке
      h('div', { className: 'card', style: { padding: '11px 13px', display: 'flex', alignItems: 'center', gap: 10 } },
        cur.logo
          ? h('img', { src: cur.logo, alt: '', style: { width: 26, height: 26, objectFit: 'contain', borderRadius: 6, flex: '0 0 auto' } })
          : h('svg', { width: 26, height: 26, viewBox: '0 0 24 24', fill: 'none', stroke: cur.accent || 'var(--accent)', strokeWidth: 1.6, strokeLinecap: 'round', style: { flex: '0 0 auto' } },
            h('path', { d: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6M12 3v6M12 15v6M3 12h6M15 12h6' })),
        h('span', { style: { flex: 1, minWidth: 0 } },
          h('div', { style: { fontWeight: 700, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, cur.title || 'Print Studio'),
          h('div', { className: 'hint', style: { margin: 0 } }, cur.tagline || '')),
        h('span', { title: cur.accent || '', style: { width: 20, height: 20, borderRadius: 6, background: cur.accent || 'var(--accent)', border: '1px solid var(--line-2)', flex: '0 0 auto' } })),
      h('div', { className: 'card', style: { padding: '2px 13px', marginTop: 10 } },
        (g.order || []).map(function (k) {
          return h(SetLine, { key: k, label: (g.labels || {})[k] || k, value: (g.values || {})[k], kind: 'text', wide: true,
            placeholder: k === 'brand_accent' ? '#ff7a1a' : 'как у нас',
            onSave: function (v) { return saveSetting(app, k, 'text', v).then(function (ok) { if (ok) props.onSaved(); return ok; }); } });
        }),
        h('div', { className: 'setline' },
          h('span', { className: 'sl' }, 'Логотип (SVG, PNG, WEBP, JPG · до 512 КБ)'),
          h('input', { ref: fileRef, type: 'file', accept: '.svg,.png,.webp,.jpg,.jpeg', style: { display: 'none' },
            onChange: function (e) { upload(e.target.files && e.target.files[0]); e.target.value = ''; } }),
          h('button', { className: 'btn soft', type: 'button', disabled: busy, style: { width: 'auto', marginTop: 0, padding: '8px 12px' },
            onClick: function () { if (fileRef.current) fileRef.current.click(); } }, cur.logo ? 'Заменить' : 'Загрузить'),
          cur.logo && h('button', { className: 'ibtn', type: 'button', disabled: busy, 'aria-label': 'Снять логотип', onClick: clear }, icon('trash', { size: 15 })))),
      h('p', { className: 'hint' }, 'Пустое поле = наше значение по умолчанию. Клиент увидит новый бренд после перезагрузки мини-аппа: он вшивается в страницу на сервере, чтобы не мелькал старый.'));
  }
  // Вход в настольную админку: два состояния, а не текстовое поле — опечатка «Personal»
  // тихо оставила бы общую дверь открытой (app/desklogin.py).
  function AccessGroup(props) {
    var g = props.group;
    if (!g) return null;
    var mode = (g.values || {}).desk_login_mode || 'token';
    function set(v) { if (v !== mode) saveSetting(props.app, 'desk_login_mode', 'text', v).then(function (ok) { if (ok) props.onSaved(); }); }
    return h(React.Fragment, null,
      h(Lab, null, 'Вход в настольную админку'),
      h('div', { className: 'card', style: { padding: '11px 13px' } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 10 } },
          h('span', { style: { flex: 1, fontSize: 12.5, color: 'var(--muted)' } }, 'Как входят сотрудники'),
          h('span', { className: 'seg' },
            h('button', { type: 'button', className: mode === 'token' ? 'on' : '', onClick: function () { set('token'); } }, 'Общий'),
            h('button', { type: 'button', className: mode === 'personal' ? 'on' : '', onClick: function () { set('personal'); } }, 'Личный'))),
        h('p', { className: 'hint' }, mode === 'personal'
          ? 'Каждый входит под собой: пишет боту /desk, получает код и вводит его в админке. Роли работают по-настоящему — оператор не видит выручку и цены.'
          : 'Один вход на компьютер: кто сел за него, тот работает правами владельца, и роли в настольной админке ничего не значат. Для мастерской с наёмным сотрудником включите личный вход.')));
  }
  // Возможности сборки (f98aa1ef): личные инструменты владельца vs продуктовые. Тумблер сильнее
  // редакции в обе стороны, «как в сборке» возвращает решение редакции.
  function EditionGroup(props) {
    var d = props.data;
    if (!d || !(d.features || []).length) return null;
    function set(f, body) {
      API.adminFeature(Object.assign({ key: f.key }, body)).then(function (j) {
        props.app.toast(j && j.ok
          ? ('«' + f.title + '» — ' + (body.reset ? 'как в сборке' : (body.on ? 'включена' : 'выключена')))
          : ((j && j.error) || 'Не удалось'));
        if (j && j.ok) props.onSaved();
      }).catch(function () { props.app.toast('Сеть недоступна'); });
    }
    return h(React.Fragment, null,
      h(Lab, null, 'Возможности сборки'),
      h('div', { className: 'card', style: { padding: '4px 13px' } },
        h('p', { className: 'hint', style: { margin: '8px 0 2px' } }, 'Сборка: ' + (d.edition === 'dev'
          ? 'разработчика — личные инструменты включены, это рабочая машина владельца.'
          : 'студии — инструменты, завязанные на машину разработчика, по умолчанию выключены.')),
        d.features.map(function (f) {
          return h('div', { key: f.key, className: 'feat' },
            h('div', { className: 'hd' },
              h('span', { className: 'tt' }, f.title,
                f.overridden && h('span', { className: 'pill violet', style: { marginLeft: 6 } }, 'вручную'),
                !f.available && h('span', { className: 'pill red', style: { marginLeft: 6 } }, 'нет на этой машине')),
              h('button', { className: 'chip' + (f.enabled ? ' on' : ''), type: 'button', style: { padding: '5px 10px', fontSize: 12, flex: '0 0 auto' },
                onClick: function () { set(f, { on: !f.enabled }); } }, f.enabled ? 'Включена' : 'Выключена'),
              f.overridden && h('button', { className: 'ibtn', type: 'button', 'aria-label': 'Как в сборке', style: { width: 30, height: 30 },
                onClick: function () { set(f, { reset: 1 }); } }, icon('refresh', { size: 13 }))),
            h('div', { className: 'why' }, f.why, f.needs ? ' Нужно: ' + f.needs + '.' : ''));
        })));
  }
  // Операционные настройки: тумблер, если значение 0/1 и ключ похож на флаг; иначе число.
  // Список групп — общий с «Ценами» (screens_admin2.js PS_OperationalGroups), подписи — серверные.
  var OPERATIONAL_FALLBACK = [
    { t: 'Расчёт и файлы моделей', ic: 'printer', keys: ['slicing_enabled', 'sanitize_3mf', 'time_prediction'] },
    { t: 'Догон брошенных расчётов', ic: 'bell', keys: ['nudge_abandoned_enabled', 'nudge_abandoned_hours', 'nudge_abandoned_max_days'] },
    { t: 'Срочность и сроки', ic: 'clock', keys: ['rush_surcharge_pct', 'term_split_hours'] },
    { t: 'Печатопригодность', ic: 'shield', keys: ['dfm_min_wall_mm'] },
  ];
  function isToggleKey(k, v) {
    var boolean = v === 0 || v === 1 || v === '0' || v === '1';
    return boolean && (/_enabled$/.test(k) || /^(slicing_enabled|sanitize_3mf|time_prediction)$/.test(k));
  }
  function NumGroup(props) {
    var g = props.group || {}, vals = g.values || {}, labels = g.labels || {};
    var present = props.keys.filter(function (k) { return k in vals; });   // старый бот без ключа — строки нет
    if (!present.length) return null;
    var BOOL = window.PS_BoolSettings || {};
    return h(React.Fragment, null,
      h(Lab, null, props.title),
      h('div', { className: 'card', style: { padding: '2px 13px' } }, present.map(function (k) {
        if (isToggleKey(k, vals[k])) {
          var meta = BOOL[k] || [];
          return h(Tgl, { key: k, ic: meta[0] || props.ic, label: labels[k] || k, sub: meta[1], on: Number(vals[k]) === 1,
            onChange: function (on) { saveSetting(props.app, k, 'num', on ? 1 : 0).then(function (ok) { if (ok) props.onSaved(); }); } });
        }
        return h(SetLine, { key: k, label: labels[k] || k, value: vals[k], kind: 'num',
          onSave: function (v) { return saveSetting(props.app, k, 'num', v).then(function (ok) { if (ok) props.onSaved(); return ok; }); } });
      })));
  }
  // Бэкапы БД (b557e341): локальные снимки бота + офсайт-копии задачи Планировщика. Показываем
  // ВОЗРАСТ свежего снимка: молча протухший бэкап опаснее явной ошибки.
  function BackupCard(props) {
    var app = props.app;
    var ds = useState(null); var d = ds[0], setD = ds[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    function load() { API.adminBackupStatus().then(function (j) { setD(j || { ok: false }); }).catch(function () { setD({ ok: false }); }); }
    useEffect(function () { load(); }, []);
    function runNow() {
      setBusy(true);
      API.adminBackupNow().then(function (j) {
        setBusy(false);
        app.toast(j && j.ok ? ('Бэкап готов · ' + MB(j.run && j.run.size)) : ((j && j.error) || 'Не удалось'));
        if (j && j.ok) load();
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    var loc = (d && d.local) || {}, off = (d && d.offsite) || {}, snaps = loc.snapshots || [], fresh = snaps[0], last = d && d.last;
    var ageH = fresh ? (Date.now() / 1000 - fresh.mtime) / 3600 : null;
    var warns = [];
    if (d && d.ok) {
      if (last && !last.ok) warns.push('Последний бэкап не удался: ' + (last.error || 'причина неизвестна'));
      if (ageH === null || ageH > 13) warns.push(fresh
        ? ('Свежему снимку уже ' + Math.round(ageH) + ' ч при интервале ' + (loc.interval_hours || 6) + ' ч — проверьте, жив ли бот')
        : 'Локальных снимков нет вообще — бэкап не работает');
      if (d.same_volume) warns.push('Копии лежат на том же физическом диске, что и база. Вынесите BOT_DB_BACKUP_DIR на другой носитель или в облако.');
    }
    return h(React.Fragment, null,
      h(Lab, { right: d && d.ok ? refresh(load) : null }, 'Бэкапы'),
      h('div', { className: 'card', style: { padding: '11px 13px' } },
        !d ? h('p', { className: 'hint', style: { margin: 0 } }, 'Загружаю…')
          : !d.ok ? h('p', { className: 'hint', style: { margin: 0 } }, d.error || 'Недоступно')
            : h(React.Fragment, null,
              h('div', { style: { fontSize: 13, fontWeight: 600, color: last && !last.ok ? 'var(--red)' : 'var(--price)' } },
                !last ? 'Бэкапов ещё не было — первый снимок бот делает при старте'
                  : last.ok ? ('Последний · ' + MB(last.size) + (last.ts ? ' · ' + last.ts : ''))
                    : ('Последняя попытка ' + (last.ts || '') + ' — неудачно')),
              h('p', { className: 'hint' }, 'Локально: ' + snaps.length + ' ' + plu(snaps.length, 'снимок', 'снимка', 'снимков') + ', храним последние ' + (loc.keep || 0) + '. '
                + (off.exists ? ('Второй контур: ' + (off.count || 0) + ' ' + plu(off.count || 0, 'копия', 'копии', 'копий') + '.') : 'Второй контур: каталог не найден — задача pcb_db_backup не отработала ни разу.')),
              off.dir && h('div', { className: 'mono', style: { fontSize: 11, color: 'var(--faint)', marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, off.dir),
              warns.map(function (w, i) { return h('p', { key: i, className: 'hint', style: { color: 'var(--amber)' } }, icon('bell', { size: 13 }), ' ', w); }),
              h('button', { className: 'btn line', type: 'button', disabled: busy, style: { marginTop: 10 }, onClick: runNow }, busy ? 'Делаю бэкап…' : 'Сделать бэкап сейчас'),
              h('p', { className: 'hint' }, 'Копия снимается на живой базе и публикуется только после проверки целостности.'))));
  }

  function AdminStudioSettings(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    function load() { API.adminSettings().then(function (j) { setData(j || { ok: false }); }).catch(function () { setData({ ok: false }); }); }
    useEffect(function () { load(); }, []);
    if (!data) return loading('Настройки');
    if (!data.ok) return failed('Настройки', data.error, load);
    var groups = window.PS_OperationalGroups || OPERATIONAL_FALLBACK;
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Настройки'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Поведение бота, бренд и доступ — как в настольной админке. Ставки, наценка и реквизиты в смете — в «Ценах и скидках», пороги сегментов клиентов — в «Клиентах».'),
      h(BrandGroup, { app: app, group: data.brand, current: data.brand_current, onSaved: load }),
      h(TextGroup, { app: app, title: 'Онлайн-оплата', group: data.pay, onSaved: load, placeholder: 'из BotFather',
        hint: (((data.pay || {}).values || {}).pay_provider_token || '').trim()
          ? 'Клиенты видят кнопку «Оплатить» в заказах: счёт открывается прямо в Telegram, деньги идут вашему провайдеру.'
          : 'Подключите провайдера в BotFather (Bot Settings → Payments), токен вставьте сюда — у клиентов появится кнопка «Оплатить». Валюта должна совпадать с валютой ваших цен.' }),
      h(TextGroup, { app: app, title: 'Сообщество владельцев', group: data.owners, onSaved: load, placeholder: 'https://t.me/…',
        hint: 'Группа для владельцев студий, а не для заказчиков: ссылка видна только в админке, в разделе «Сообщество».' }),
      h(AccessGroup, { app: app, group: data.access, onSaved: load }),
      h(EditionGroup, { app: app, data: data.edition, onSaved: load }),
      groups.map(function (g) { return h(NumGroup, { key: g.t, app: app, title: g.t, ic: g.ic, keys: g.keys, group: data.pricing, onSaved: load }); }),
      h(BackupCard, { app: app }));
  }

  // ═══════════════════════ СООБЩЕСТВО (desk community.jsx) ═══════════════════════
  // База знаний студии + вход в чат владельцев студий (внешняя Telegram-группа, адрес — из
  // настройки owners_chat_url). Своего сервера у продукта нет, установки друг о друге не знают.
  function AdminCommunity(props) {
    var app = props.app;
    var ss = useState(null); var st = ss[0], setSt = ss[1];
    useEffect(function () {
      API.adminSettings().then(function (j) { setSt(j && j.ok ? j : { ok: false }); }).catch(function () { setSt({ ok: false }); });
    }, []);
    var url = String((((st || {}).owners || {}).values || {}).owners_chat_url || '').trim();
    return h('div', null,
      h('div', { className: 'screen', style: { paddingBottom: 0 } },
        h('div', { className: 'card', style: { display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px' } },
          h('span', { style: { width: 42, height: 42, borderRadius: 12, flex: '0 0 auto', display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: 'var(--surface-2)', color: 'var(--accent)' } }, icon('chat', { size: 19 })),
          h('span', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontWeight: 700, fontSize: 13.5 } }, 'Сообщество владельцев студий'),
            h('div', { className: 'hint', style: { margin: '2px 0 0' } }, st === null ? 'Проверяю ссылку…'
              : (url ? 'Группа в Telegram: печать, цены, оборудование — между владельцами разных студий.'
                : 'Ссылка на группу не задана — впишите её в Настройках → «Сообщество владельцев».'))),
          url && h('button', { className: 'btn', type: 'button', style: { width: 'auto', marginTop: 0, padding: '9px 12px', flex: '0 0 auto' },
            onClick: function () { openLink(url); } }, 'Открыть'))),
      // статьи базы знаний — прежний экран целиком (инструкции для себя и операторов)
      window.AdminKb ? h(window.AdminKb, { app: app }) : null);
  }

  // ═══════════════════════ КАЛИБРОВКА (desk calib.jsx, обзор) ═══════════════════════
  // Телефон здесь — справочник у станка: сводка, кампании с шагами и вердиктами замеров,
  // паспорта пластиков, PDF-паспорт и этикетки. Заведение кампаний, замеры, фото образцов и
  // снимки пресетов — в настольной админке: на телефоне их некуда ввести.
  function kpi(ic, label, value, note) {
    return h('div', { className: 'kpi' },
      h('div', { className: 'kl' }, icon(ic, { size: 14 }), ' ', label),
      h('div', { className: 'kv' }, value == null ? '—' : value),
      note && h('div', { className: 'kd', style: { color: 'var(--muted)' } }, note));
  }
  var RUN_ST = { active: ['amber', 'идёт'], done: ['green', 'завершена'], paused: ['gray', 'пауза'] };
  var PP_ST = { calibrated: ['green', 'откалиброван'], partial: ['amber', 'частично'], stale: ['gray', 'устарел'], draft: ['gray', 'черновик'] };
  var PP_FIELDS = [['temp', 'Темп', '°'], ['flow', 'Поток', ''], ['pa', 'PA', ''], ['mvs', 'MVS', ''], ['retract', 'Ретракт', '']];

  function AdminCalib(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var os = useState(null); var open = os[0], setOpen = os[1];      // раскрытая кампания {id, run, steps, …}
    var ob = useState(0); var openBusy = ob[0], setOpenBusy = ob[1];
    function load() { API.adminCalib().then(function (j) { setData(j || { ok: false }); }).catch(function () { setData({ ok: false }); }); }
    useEffect(function () { load(); }, []);
    function toggleRun(id) {
      if (open && open.id === id) { setOpen(null); return; }
      setOpenBusy(id);
      API.adminCalibRun(id).then(function (j) {
        setOpenBusy(0);
        if (j && j.ok) setOpen(Object.assign({ id: id }, j)); else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { setOpenBusy(0); app.toast('Сеть недоступна'); });
    }
    if (!data) return loading('Калибровка');
    if (!data.ok) return failed('Калибровка', data.error, load);
    var st = data.stats || {}, runs = data.runs || [], pps = data.passports || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Калибровка'),
      h('div', { className: 'kpis' },
        kpi('flame', 'Кампаний', st.runs, st.active ? (st.active + ' ' + plu(st.active, 'активная', 'активные', 'активных')) : ''),
        kpi('doc', 'Паспортов', st.passports, ''),
        kpi('image', 'Образцов', st.samples, ''),
        kpi('layers', 'Пресетов', st.presets, st.prints ? (st.prints + ' ' + plu(st.prints, 'печать', 'печати', 'печатей')) : '')),
      h(Lab, null, 'Кампании'),
      !runs.length && h('p', { className: 'hint' }, 'Кампаний ещё нет. Заводятся в настольной админке — раздел «Калибровка».'),
      runs.map(function (r) {
        var s = RUN_ST[r.status] || ['gray', r.status || '—'];
        var isOpen = !!(open && open.id === r.id);
        var steps = isOpen ? (open.steps || []) : [];
        return h('div', { key: r.id, className: 'cal-run', onClick: function () { toggleRun(r.id); } },
          h('div', { className: 'hd' }, h('b', null, r.title || ('Кампания #' + r.id)), h('span', { className: 'pill ' + s[0] }, s[1])),
          h('div', { className: 'ds' }, [r.plastic_name, r.nozzle ? ('сопло ' + r.nozzle) : '', r.reason].filter(Boolean).join(' · ')),
          h('div', { className: 'plan-bar', style: { marginTop: 8 } },
            h('i', { style: { width: (r.steps ? Math.round((r.steps_done || 0) / r.steps * 100) : 0) + '%' } })),
          h('div', { className: 'ds', style: { marginTop: 4 } },
            (r.steps_done || 0) + ' из ' + (r.steps || 0) + ' шагов' + (r.started_at ? ' · с ' + dateShort(r.started_at) : '')),
          openBusy === r.id && h('p', { className: 'hint' }, 'Загружаю…'),
          isOpen && h('div', { style: { marginTop: 8 } },
            steps.map(function (sp, i) {
              var vd = (sp.measures || []).map(function (m) { return m.verdict; }).filter(Boolean).join(', ');
              return h('div', { key: sp.id || i, className: 'cal-step' + (sp.status === 'done' ? ' done' : '') },
                h('span', { className: 'n' }, i + 1),
                h('span', { className: 'tt' }, sp.title || sp.kind || ('шаг ' + (i + 1)),
                  sp.note && h('div', { className: 'hint', style: { margin: '2px 0 0' } }, sp.note),
                  vd && h('div', { className: 'vd' }, vd)));
            }),
            !steps.length && h('p', { className: 'hint' }, 'Шагов нет.'),
            h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
              API.adminCalibExportUrl && h('button', { className: 'btn soft', type: 'button', style: { marginTop: 0 },
                onClick: function (e) { e.stopPropagation(); openLink(API.adminCalibExportUrl(r.id)); } }, icon('doc', { size: 16 }), ' PDF-паспорт'),
              API.adminCalibLabelsUrl && h('button', { className: 'btn line', type: 'button', style: { marginTop: 0 },
                onClick: function (e) { e.stopPropagation(); openLink(API.adminCalibLabelsUrl(r.id)); } }, icon('tag', { size: 16 }), ' Этикетки'))));
      }),
      h(Lab, null, 'Паспорта пластиков'),
      !pps.length && h('p', { className: 'hint' }, 'Паспортов ещё нет — они рождаются из завершённых кампаний.'),
      pps.map(function (p) {
        var v = p.values || {}, s = PP_ST[p.status] || ['gray', p.status || '—'];
        return h('div', { key: p.id, className: 'card', style: { marginTop: 10, padding: '12px 13px' } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
            h('b', { style: { flex: 1, minWidth: 0, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, p.plastic || ('Пластик #' + p.plastic_id)),
            h('span', { className: 'pill ' + s[0] }, s[1])),
          h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 3 } }, [p.printer, p.nozzle ? 'сопло ' + p.nozzle : '', p.ago].filter(Boolean).join(' · ')),
          h('div', { className: 'cal-pp' }, PP_FIELDS.map(function (f) {
            var val = v[f[0]];
            return h('div', { key: f[0], className: 'c' }, h('div', { className: 'k' }, f[1]),
              h('div', { className: 'v' }, val != null && val !== '' ? (val + f[2]) : '—'));
          })));
      }),
      h('p', { className: 'hint', style: { marginTop: 14 } }, 'Замеры, фото образцов, снимки пресетов и правка кампаний — в настольной админке; телефон здесь — справочник у станка.'));
  }

  // ═══════════════════════ ИНСТРУМЕНТЫ · допечатка со слоя (desk tools.jsx, без 3D) ═══════════════════════
  // Телефон у станка: выбрать задание (текущая печать находится сама), отметить сорвавшиеся
  // детали, слой (подставлен из паузы), собрать, посмотреть превью, залить, пройти гейты, запустить.
  // 3D-сцена есть только на ПК; здесь — плоские превью слоёв с сервера.
  function fmtMinR(m) { m = Math.max(0, Math.round(Number(m) || 0)); return m >= 60 ? (Math.floor(m / 60) + ' ч ' + (m % 60) + ' мин') : (m + ' мин'); }
  var PHASES_R = { queue: 'в очереди', load: 'читаю файл', objects: 'ищу детали', analyze: 'разбираю ходы', build: 'собираю файл',
    check: 'самопроверка', verify: 'сверяю с исходником', previews: 'рисую превью', done: 'готово' };
  function AdminTools(props) {
    var app = props.app;
    var sS = useState(null); var sources = sS[0], setSources = sS[1];
    var srcS = useState(null); var src = srcS[0], setSrc = srcS[1];
    var anS = useState(null); var an = anS[0], setAn = anS[1];
    var selS = useState([]); var sel = selS[0], setSel = selS[1];
    var lS = useState(''); var layer = lS[0], setLayer = lS[1];
    var jS = useState(null); var job = jS[0], setJob = jS[1];
    var gS = useState(null); var gates = gS[0], setGates = gS[1];
    var eS = useState(''); var err = eS[0], setErr = eS[1];
    var bS = useState(false); var busy = bS[0], setBusy = bS[1];
    useEffect(function () {
      API.adminToolsResumeSources(0).then(function (j) { setSources(j && j.ok ? j : { ok: false, error: (j && j.error) || 'нет связи' }); })
        .catch(function (e) { setSources({ ok: false, error: String(e && e.message || e) }); });
    }, []);
    // поллинг сборки
    useEffect(function () {
      if (!job || job.status === 'done' || job.status === 'failed') return undefined;
      var dead = false;
      var t = setTimeout(function () {
        API.adminToolsResumeJob(job.job).then(function (j) { if (!dead) setJob(j && j.ok ? j : { status: 'failed', error: (j && j.error) || 'сборка не удалась' }); })
          .catch(function (e) { if (!dead) setJob({ status: 'failed', error: String(e && e.message || e) }); });
      }, 900);
      return function () { dead = true; clearTimeout(t); };
    }, [job]);
    function pick(s) {
      setSrc(s); setAn(null); setSel([]); setJob(null); setGates(null); setErr('');
      API.adminToolsResumeAnalyze(s.path, 0).then(function (j) {
        if (!j || !j.ok) { setErr((j && j.error) || 'файл не разобрать'); return; }
        setAn(j);
        if (j.suggest && j.suggest.layer) setLayer(String(j.suggest.layer));
      }).catch(function (e) { setErr(String(e && e.message || e)); });
    }
    function toggle(id) { setSel(function (s) { return s.indexOf(id) >= 0 ? s.filter(function (x) { return x !== id; }) : s.concat([id]); }); }
    function build() {
      var n = Number(layer) || 0, expect = {};
      sel.forEach(function (id) { var o = an.objects.filter(function (x) { return x.id === id; })[0]; if (o) expect[id] = [o.cx, o.cy]; });
      setBusy(true); setErr('');
      API.adminToolsResumeBuild(src.path, n, sel, expect, src.name).then(function (j) {
        if (!j || !j.ok) { setErr((j && j.error) || 'сборка не запустилась'); return; }
        setJob(j);
      }).catch(function (e) { setErr(String(e && e.message || e)); }).finally(function () { setBusy(false); });
    }
    function refreshGates(force) {
      API.adminToolsResumeGates(job.job, 0, !!force).then(function (g) { setGates(g && g.ok ? g : { gates: [{ gate: 'net', hard: true, text: (g && g.error) || 'нет связи' }], can_start: false }); });
    }
    function send() {
      if (!window.confirm('Залить «' + job.filename + '» на принтер? Печать НЕ начнётся — старт отдельно, когда сорвавшиеся детали сняты.')) return;
      setBusy(true);
      API.adminToolsResumeSend(job.job, 0).then(function (j) {
        if (!j || !j.ok) { setErr((j && j.error) || 'заливка не удалась'); return; }
        setJob(Object.assign({}, job, { uploaded: j.uploaded })); setGates({ gates: j.gates || [], can_start: !(j.gates || []).length });
      }).catch(function (e) { setErr(String(e && e.message || e)); }).finally(function () { setBusy(false); });
    }
    function start(force) {
      if (!window.confirm('Запустить допечатку «' + job.uploaded + '» со слоя ' + (job.report || {}).layer + '? Убедитесь, что вырезанные детали и мусор сняты, стол тёплый.')) return;
      setBusy(true);
      API.adminToolsResumeStart(job.job, 0, { forceCentre: !!force, ackSoft: true }).then(function (j) {
        if (!j || !j.ok) { setErr((j && j.error) || 'старт не удался'); refreshGates(force); return; }
        setJob(Object.assign({}, job, { started: true }));
      }).catch(function (e) { setErr(String(e && e.message || e)); }).finally(function () { setBusy(false); });
    }
    var rep = (job && job.report) || {};
    var list = (sources && sources.sources) || [];
    var cur = sources && sources.current;
    var minL = (an && (an.first_layer || 1) + 1) || 2, n = Number(layer) || 0;
    var layerOk = an && n >= minL && n <= an.layers;
    return h('div', { className: 'screen' },
      h(Lab, null, 'Допечатка со слоя'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Одна деталь сорвалась, остальные стоят: продолжить ту же печать с N-го слоя без неё. На ПК есть 3D-сцена, здесь — превью слоёв.'),
      err ? h('div', { className: 'card', style: { borderColor: 'var(--danger)', padding: 10 } }, h('div', { className: 'hint', style: { margin: 0, color: 'var(--danger)' } }, err)) : null,
      // шаг 1: источник
      !src ? h(React.Fragment, null,
        sources === null ? loading('Задания') : null,
        sources && !sources.ok ? failed('Задания', sources.error) : null,
        cur && cur.path ? h('button', { className: 'card', type: 'button', style: { textAlign: 'left', borderColor: 'var(--accent)' }, onClick: function () { pick({ path: cur.path, name: cur.name || cur.job_name }); } },
          h('div', { style: { fontWeight: 700 } }, 'Сейчас на принтере: ' + (cur.name || cur.job_name)), h('div', { className: 'hint', style: { margin: 0 } }, 'файл найден в архиве — нажмите, чтобы взять')) : null,
        list.map(function (f) {
          return h('button', { key: f.path, className: 'card', type: 'button', style: { textAlign: 'left', display: 'flex', gap: 10, alignItems: 'center' }, onClick: function () { pick(f); } },
            f.thumb ? h('img', { src: 'data:image/png;base64,' + f.thumb, width: 48, height: 48, alt: '', style: { borderRadius: 8 } }) : icon('doc', { size: 22 }),
            h('span', { style: { flex: 1, minWidth: 0 } }, h('div', { style: { fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, f.name),
              h('div', { className: 'hint', style: { margin: 0 } }, (f.layers ? f.layers + ' сл. · ' : '') + (f.minutes ? fmtMinR(f.minutes) + ' · ' : '') + Math.round(f.size / 1e6) + ' МБ')));
        }),
        sources && sources.ok && !list.length && !(cur && cur.path) ? h('p', { className: 'hint' }, 'Заданий пока нет. Файл можно приложить в настольной админке.') : null)
        : null,
      // шаг 2: детали и слой
      src && !job ? h(React.Fragment, null,
        h('p', { className: 'hint' }, src.name, ' · ', h('a', { href: '#', onClick: function (e) { e.preventDefault(); setSrc(null); setAn(null); } }, 'другой файл')),
        !an && !err ? loading('Разбор') : null,
        an && !an.supported ? h('p', { className: 'hint', style: { color: 'var(--danger)' } }, an.reason) : null,
        an && an.warnings.map(function (w, i) { return h('p', { key: i, className: 'hint' }, '⚠ ' + w); }),
        an ? an.objects.map(function (o) {
          var off = sel.indexOf(o.id) >= 0;
          return h('label', { key: o.id, className: 'card', style: { display: 'flex', gap: 10, alignItems: 'center', outline: off ? '2px solid var(--danger)' : 'none' } },
            h('input', { type: 'checkbox', checked: off, onChange: function () { toggle(o.id); } }),
            h('span', { style: { flex: 1 } }, h('div', { style: { fontWeight: 600 } }, o.name), h('div', { className: 'hint', style: { margin: 0 } }, '(' + o.cx + ', ' + o.cy + ') · ' + o.zmax + ' мм')));
        }) : null,
        an ? h('div', { className: 'card' },
          h('div', { style: { fontWeight: 600 } }, 'С какого слоя продолжать'),
          h('input', { type: 'number', min: minL, max: an.layers, value: layer, placeholder: minL + '…' + an.layers, onChange: function (e) { setLayer(e.target.value); }, style: { width: 110, marginTop: 6 } }),
          an.suggest && an.suggest.layer ? h('div', { className: 'hint', style: { margin: '6px 0 0' } }, 'Пауза была на слое ' + an.suggest.layer + ' — подставлено.') : null,
          layerOk ? h('div', { className: 'hint', style: { margin: '6px 0 0' } }, 'Слой ' + n + ': Z ' + an.layer_z[n] + ' мм, детали на столе ' + an.layer_z[n - 1] + ' мм.') : null) : null,
        an ? h('button', { className: 'btn', type: 'button', disabled: busy || !an.supported || !an.separable || !sel.length || sel.length >= an.objects.length || !layerOk, onClick: build },
          sel.length ? ('Собрать: со слоя ' + (n || '?') + ', без ' + sel.length + ' дет.') : 'Отметьте сорвавшиеся детали') : null)
        : null,
      // шаг 3–4: сборка, файл, старт
      job && job.status !== 'done' ? (job.status === 'failed'
        ? h(React.Fragment, null, failed('Сборка', job.error, function () { setJob(null); }))
        : h('div', { className: 'card' }, h('div', { style: { fontWeight: 600 } }, 'Собираю файл…'),
          h('div', { className: 'hint', style: { margin: '4px 0 0' } }, (PHASES_R[job.phase] || job.phase) + ' · ' + Math.round((job.pct || 0) * 100) + '%')))
        : null,
      job && job.status === 'done' ? h(React.Fragment, null,
        h('div', { className: 'card' },
          h('div', { style: { fontWeight: 700 } }, 'Файл собран и сверен'),
          h('div', { className: 'hint', style: { margin: '4px 0 0' } }, job.filename + ' · со слоя ' + rep.layer + ' (Z ' + rep.z_layer + ') · останется ' + rep.kept_objects + ' дет. · ≈' + fmtMinR(rep.minutes_remaining)),
          (rep.warnings || []).map(function (w, i) { return h('div', { key: i, className: 'hint', style: { margin: '4px 0 0', color: 'var(--warn, #d90)' } }, '⚠ ' + w); })),
        (job.previews || []).map(function (p) {
          return h('div', { key: p.layer, style: { textAlign: 'center', marginBottom: 8 } },
            h('img', { src: 'data:image/png;base64,' + p.png, alt: '', style: { width: '100%', maxWidth: 320, border: '1px solid var(--line)', borderRadius: 8, background: '#fff' } }),
            h('div', { className: 'hint', style: { margin: '2px 0 0' } }, 'слой ' + p.layer + ' · Z ' + p.z + ' · чёрное печатается, красное вырезано'));
        }),
        h('p', { className: 'hint' }, '«Стоп» на принтере, сразу включить стол на ' + rep.bed + '°, снять сорвавшиеся детали и мусор, остальные не трогать.'),
        !job.uploaded ? h('button', { className: 'btn', type: 'button', disabled: busy, onClick: send }, 'Залить на принтер') : null,
        job.uploaded ? h('div', { className: 'card' },
          h('div', { style: { fontWeight: 600 } }, 'Перед стартом'),
          gates === null ? h('button', { className: 'btn line', type: 'button', onClick: function () { refreshGates(false); } }, 'Проверить принтер') : null,
          gates && !gates.gates.length ? h('div', { className: 'hint', style: { margin: '4px 0 0', color: 'var(--ok, #2a7)' } }, '✓ Принтер свободен, стол тёплый, файл на принтере.') : null,
          gates ? gates.gates.map(function (g) {
            return h('div', { key: g.gate, className: 'hint', style: { margin: '4px 0 0', color: g.hard ? 'var(--danger)' : 'var(--warn, #d90)' } },
              (g.hard ? '✕ ' : '⚠ ') + g.text + (g.hint ? ' — ' + g.hint : ''),
              g.gate === 'bed_cold' ? h('button', { className: 'chip', type: 'button', style: { marginLeft: 8 }, onClick: function () { API.adminPrinterHeat('bed', Math.round(g.bed_target)).then(function () { setTimeout(function () { refreshGates(false); }, 1500); }); } }, 'Включить стол ' + Math.round(g.bed_target) + '°') : null,
              g.gate === 'centre' ? h('button', { className: 'chip', type: 'button', style: { marginLeft: 8 }, onClick: function () { start(true); } }, 'Деталь снята, старт') : null);
          }) : null,
          gates ? h('button', { className: 'btn line', type: 'button', style: { marginTop: 8 }, onClick: function () { refreshGates(false); } }, 'Проверить снова') : null,
          job.started ? h('p', { className: 'hint', style: { color: 'var(--ok, #2a7)' } }, 'Запущено — смотрите первые слои.')
            : h('button', { className: 'btn', type: 'button', style: { marginTop: 8 }, disabled: busy || !gates || gates.gates.length > 0, onClick: function () { start(false); } }, 'Стол готов — запустить')) : null,
        h('button', { className: 'btn line', type: 'button', style: { marginTop: 12 }, onClick: function () { setJob(null); setGates(null); } }, 'Другие детали или слой'))
        : null,
      h('button', { className: 'btn line', style: { marginTop: 18 }, onClick: function () { app.go('adminMore'); } }, icon('back', { size: 16 }), ' К разделам'));
  }

  Object.assign(window, { AdminPlan: AdminPlan, AdminStudioSettings: AdminStudioSettings,
    AdminCommunity: AdminCommunity, AdminCalib: AdminCalib, AdminTools: AdminTools });
})();
