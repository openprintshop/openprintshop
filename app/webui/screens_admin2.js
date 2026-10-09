// screens_admin2.js — admin CRUD: Plastics, Promos, Settings, Discounts, Shades, Presets, Collection
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, Lab = window.PS_Lab, API = window.API, Breakdown = window.PS_Breakdown;

  function field(label, node) { return h('label', { style: { display: 'block', marginBottom: 9 } }, h('div', { style: { fontSize: 11.5, color: 'var(--faint)', margin: '0 2px 5px', fontWeight: 600 } }, label), node); }
  function inp(props) { return h('input', Object.assign({ className: 'input' }, props)); }
  function iconBtn(ic, onClick, opts) {
    opts = opts || {};
    return h('button', { onClick: onClick, title: opts.title, style: { width: 32, height: 32, borderRadius: 9, border: '1px solid var(--line-2)', background: opts.on ? 'var(--accent)' : 'var(--surface-2)', color: opts.on ? '#1a0d02' : (opts.danger ? 'var(--red)' : 'var(--muted)'), cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto' } }, icon(ic, { size: 16 }));
  }
  function actionsRow(children) { return h('div', { style: { display: 'flex', gap: 6, marginTop: 10, justifyContent: 'flex-end' } }, children); }
  function addHeader(open, setOpen, label) {
    return h('button', { className: 'btn ' + (open ? 'line' : 'soft'), onClick: function () { setOpen(!open); } },
      icon(open ? 'close' : 'plus', { size: 17 }), ' ', open ? 'Свернуть' : label);
  }

  // ───────────────────── PLASTICS ─────────────────────
  // ── Катушки материала (Фаза A мастер-плана учёта): приход, остаток, весы, инвентаризация, статус ──
  // Единица физического учёта = катушка; остаток ведётся неизменяемым журналом (бэкенд), здесь
  // только приход/просмотр/инвентаризация. Разворачивается по кнопке-катушке на карточке пластика.
  // Есть принтер с учётом катушек CC2 Control (data.cc2) — склад ведёт он: остаток катушки он
  // списывает по реально выдавленному, а приход, весы и архив уходят в его службу.
  function slotName(slot) { return slot == null ? 'на полке' : (slot === 4 ? 'внешний держатель' : 'лоток ' + (slot + 1)); }
  function SpoolPanel(props) {
    var app = props.app, pid = props.plasticId, colorCss = props.colorCss || {};
    var colors = props.colors || [], tare0 = props.tare || '';
    var blank = { color: '', net: '', rem: '', tare: tare0, cost: '', supplier: '', low: '' };
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var fsp = useState(blank); var f = fsp[0], setF = fsp[1];
    var ups = useState({}); var upick = ups[0], setUpick = ups[1];   // {cc2_id: цвет} — катушки принтера без пары
    function load() { API.adminSpools(pid).then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function act(body, okMsg) { return API.adminSpool(body).then(function (j) { if (!j.ok) app.toast(j.error || 'Ошибка'); else { if (okMsg) app.toast(okMsg); load(); if (props.onChanged) props.onChanged(); } return j; }); }
    var cc2 = data && data.cc2;
    function add() {
      if (!f.net || Number(String(f.net).replace(',', '.')) <= 0) { app.toast('Вес нетто — число больше нуля'); return; }
      if (cc2 && !f.color) { app.toast('Выберите цвет — без него принтер катушку не заведёт'); return; }
      var body = { action: 'add', plastic_id: pid, color: f.color, net_weight: f.net, cost: f.cost, supplier: f.supplier, low: f.low };
      if (cc2) { body.remaining = f.rem; body.tare = f.tare; }
      act(body).then(function (j) { if (j.ok) { setF(blank); app.toast(cc2 ? 'Катушка заведена на принтере' : 'Катушка добавлена'); } });
    }
    function weigh(s) {
      var tare = s.tare || tare0;
      var v = prompt('Вес катушки «' + (s.color || 'без цвета') + '» на весах, г' + (tare ? ' (тара ' + Math.round(tare) + ' г вычтется):' : ':'), '');
      if (v == null || v === '') return;
      act({ action: 'weigh', spool_id: s.id, gross: v }, 'Взвешено');
    }
    function inventory(s) {
      var rem = s.live_g != null ? s.live_g : s.remaining_g;
      var v = prompt('Сколько филамента осталось на катушке «' + (s.color || 'без цвета') + '», г:', String(Math.round(rem)));
      if (v == null) return;
      act({ action: 'correction', spool_id: s.id, actual: v }, 'Учтено');
    }
    function archive(s) { if (confirm(cc2 && s.cc2_id ? 'Катушку в архив? Она уйдёт в архив и на принтере.' : 'Списать катушку в архив? (перестанет учитываться в остатке)')) act({ action: 'status', spool_id: s.id, status: 'archived' }); }
    if (!data) return h('div', { style: { fontSize: 12, color: 'var(--muted)', padding: '8px 2px' } }, 'Загрузка катушек…');
    var stmap = { active: ['st-printed', 'активна'], empty: ['st-rejected', 'пустая'], archived: ['st-rejected', 'архив'] };
    function fld(key, ph, flex, extra) { return h('input', Object.assign({ className: 'input mono', style: { flex: flex, marginTop: 0, padding: '13px 8px' }, inputMode: 'decimal', placeholder: ph, value: f[key], onChange: function (e) { var o = {}; o[key] = e.target.value; setF(Object.assign({}, f, o)); } }, extra || {})); }
    // .btn в мини-аппе — во всю ширину; здесь кнопки делят строку и переносятся, только если тесно
    var smallBtn = { marginTop: 0, padding: '0 10px', fontSize: 12, width: 'auto', flex: '1 1 auto' };
    return h('div', { style: { marginTop: 10, borderTop: '1px solid var(--surface-2)', paddingTop: 10 } },
      h('div', { style: { fontSize: 11, fontWeight: 700, letterSpacing: '.7px', textTransform: 'uppercase', color: 'var(--faint)', margin: '0 2px 6px' } }, 'Катушки' + (cc2 ? ' · ведёт принтер' : '')),
      cc2 && h('div', { style: { fontSize: 11.5, color: 'var(--muted)', margin: '0 2px 8px' } },
        'Остаток списывает принтер «' + cc2.printer + '» по реально выдавленному. Приход, весы и архив уходят на принтер.'
        + (!cc2.online ? ' Принтер не отвечает — показан последний известный остаток.' : '')),
      h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 } },
        cc2
          ? h('select', { className: 'input', style: { flex: '1 1 108px', marginTop: 0 }, value: f.color, onChange: function (e) { setF(Object.assign({}, f, { color: e.target.value })); } },
              h('option', { value: '' }, '— цвет —'),
              colors.map(function (c) { return h('option', { key: c, value: c }, c); }))
          : h('input', { className: 'input', style: { flex: '1 1 108px', marginTop: 0 }, placeholder: 'цвет', value: f.color, onChange: function (e) { setF(Object.assign({}, f, { color: e.target.value })); } }),
        fld('net', 'нетто', '0 0 74px', { title: 'вес нетто, г' }),
        cc2 && fld('rem', 'остаток', '0 0 76px', { title: 'у начатой катушки; пусто = полная' }),
        cc2 && fld('tare', 'тара', '0 0 64px', { title: 'вес пустой катушки' }),
        fld('cost', 'цена', '0 0 72px'),
        fld('low', 'порог', '0 0 66px', { title: 'порог «мало», г' }),
        h('button', { className: 'btn', style: { marginTop: 0, flex: '0 0 auto', padding: '0 12px' }, onClick: add }, 'Приход')),
      (data.spools || []).length === 0 && h('div', { style: { fontSize: 12, color: 'var(--muted)', padding: '2px' } }, 'Катушек пока нет — добавь приход выше.'),
      (data.spools || []).map(function (s) {
        var rem = s.live_g != null ? s.live_g : s.remaining_g;
        var pct = s.net_weight_g > 0 ? Math.min(100, Math.max(0, rem / s.net_weight_g * 100)) : 0;
        var lowOn = s.low_threshold > 0 && rem <= s.low_threshold;
        var badge = stmap[s.status] || stmap.active;
        return h('div', { key: s.id, style: { marginTop: 8, opacity: s.status === 'archived' ? 0.5 : 1 } },
          h('div', { className: 'spread', style: { fontSize: 12, marginBottom: 4 } },
            h('span', { style: { display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 } },
              h('span', { className: 'dot-ring', style: { border: 0, width: 12, height: 12, background: s.hex || colorCss[s.color] || '#888' } }),
              h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, s.color || 'без цвета'),
              h('span', { className: 'stbadge ' + badge[0], style: { marginTop: 0, padding: '1px 6px', fontSize: 10 } }, badge[1]),
              cc2 && s.cc2_id && h('span', { className: 'stbadge ' + (s.slot != null ? 'st-progress' : 'st-new'), style: { marginTop: 0, padding: '1px 6px', fontSize: 10 } }, slotName(s.slot)),
              cc2 && !s.cc2_id && s.status !== 'archived' && h('span', { className: 'stbadge st-rejected', style: { marginTop: 0, padding: '1px 6px', fontSize: 10 } }, 'не на принтере')),
            h('span', { className: 'mono', style: { color: lowOn ? 'var(--red)' : 'var(--text)', fontWeight: 600, flex: '0 0 auto', marginLeft: 8 } },
              Math.round(rem) + ' / ' + Math.round(s.net_weight_g) + ' г' + (s.cost != null ? ' · ' + s.cost : ''))),
          h('div', { className: 'pbar' }, h('i', { style: { width: pct + '%', background: lowOn ? 'var(--red)' : undefined } })),
          h('div', { style: { display: 'flex', gap: 6, marginTop: 5, flexWrap: 'wrap' } },
            s.status !== 'archived' && h('button', { className: 'btn line', style: smallBtn, onClick: function () { weigh(s); } }, 'Весы'),
            h('button', { className: 'btn line', style: smallBtn, onClick: function () { inventory(s); } }, 'Остаток'),
            cc2 && !s.cc2_id && s.status !== 'archived' && h('button', { className: 'btn line', style: smallBtn, onClick: function () { act({ action: 'cc2_push', spool_id: s.id }, 'Катушка заведена на принтере'); } }, 'На принтер'),
            s.status !== 'archived' && h('button', { className: 'btn line', style: smallBtn, onClick: function () { archive(s); } }, 'В архив')));
      }),
      // катушки, заведённые прямо на принтере: связь только руками, по имени ничего не угадываем
      cc2 && (cc2.unlinked || []).length > 0 && h('div', { style: { marginTop: 12, borderTop: '1px solid var(--surface-2)', paddingTop: 8 } },
        h('div', { style: { fontSize: 11.5, color: 'var(--muted)', margin: '0 2px 6px' } }, 'На принтере есть катушки без пары — возьмите нужные в учёт этого пластика:'),
        cc2.unlinked.map(function (u) {
          return h('div', { key: u.cc2_id, style: { display: 'flex', gap: 6, alignItems: 'center', marginTop: 6, flexWrap: 'wrap' } },
            h('span', { className: 'dot-ring', style: { border: 0, width: 12, height: 12, background: u.hex || '#888' } }),
            h('span', { style: { flex: '1 1 140px', fontSize: 12, minWidth: 0 } }, (u.name || 'без имени') + ' · ' + Math.round(u.remaining_g) + ' г · ' + slotName(u.slot)),
            h('select', { className: 'input', style: { flex: '0 0 120px', marginTop: 0 }, value: upick[u.cc2_id] || '', onChange: function (e) { var o = {}; o[u.cc2_id] = e.target.value; setUpick(Object.assign({}, upick, o)); } },
              h('option', { value: '' }, '— цвет —'),
              colors.map(function (c) { return h('option', { key: c, value: c }, c); })),
            h('button', { className: 'btn line', style: smallBtn, onClick: function () {
              if (colors.length && !upick[u.cc2_id]) { app.toast('Выберите цвет'); return; }
              act({ action: 'cc2_adopt', cc2_id: u.cc2_id, plastic_id: pid, color: upick[u.cc2_id] || '' }, 'Катушка взята в учёт');
            } }, 'В учёт'));
        })));
  }

  // ── Полоса принтера (CC2 Control) над списком пластиков: учёт выключен / принтер молчит /
  // «Какая это катушка?» по лоткам. Ответ может подождать: принтер копит выдавленное и спишет
  // его с катушки, которую выберут позже.
  function Cc2SpoolBar(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var ps = useState({}); var pick = ps[0], setPick = ps[1];
    function load() { API.adminSpools(0).then(function (j) { if (j && j.ok) setData(j); }); }
    useEffect(function () { load(); }, [props.tick]);
    var cc2 = data && data.cc2;
    if (!cc2) return null;
    function act(body, okMsg) { API.adminSpool(body).then(function (j) { if (!j.ok) app.toast(j.error || 'Ошибка'); else { app.toast(okMsg); load(); if (props.onChanged) props.onChanged(); } }); }
    var spools = (data.spools || []).filter(function (s) { return s.cc2_id && s.status !== 'archived'; });
    var pname = {}; (props.plastics || []).forEach(function (p) { pname[p.id] = p.name; });
    var warn = !cc2.online ? 'Принтер «' + cc2.printer + '» не отвечает — остатки катушек на момент последней связи'
      : (!cc2.available ? 'CC2 Control не загрузил склад катушек — остатки не обновляются'
        : (!cc2.enabled ? 'Учёт катушек на принтере выключен: расход печати никто не считает' : ''));
    return h('div', null,
      warn && h('div', { className: 'card', style: { marginTop: 10, borderColor: 'var(--amber)', fontSize: 12.5 } },
        warn,
        cc2.online && !cc2.enabled && h('button', { className: 'btn soft', style: { marginTop: 8 }, onClick: function () { act({ action: 'cc2_enable', on: true }, 'Учёт катушек включён'); } }, 'Включить учёт')),
      (cc2.questions || []).map(function (q) {
        var pr = q.printer || {};
        return h('div', { key: q.slot, className: 'card', style: { marginTop: 10, borderColor: 'var(--accent)' } },
          h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 } },
            h('span', { className: 'dot-ring', style: { border: 0, width: 14, height: 14, background: pr.color || '#888' } }),
            h('b', null, q.name + ': ' + (q.question === 'changed' ? 'сменили филамент' : 'вставили катушку') + ' — какая это?')),
          (pr.type || q.question_mm) && h('div', { style: { fontSize: 11.5, color: 'var(--muted)', marginTop: 4 } },
            [pr.brand, pr.name || pr.type].filter(Boolean).join(' ') + (q.question_mm ? ' · уже выдавлено ' + Math.round(q.question_mm) + ' мм — спишется с выбранной' : '')),
          h('select', { className: 'input', value: pick[q.slot] || '', onChange: function (e) { var o = {}; o[q.slot] = e.target.value; setPick(Object.assign({}, pick, o)); } },
            h('option', { value: '' }, '— катушка со склада —'),
            spools.map(function (s) { return h('option', { key: s.id, value: s.cc2_id }, (pname[s.plastic_id] || '') + ' ' + (s.color || '') + ' · ' + Math.round(s.live_g != null ? s.live_g : s.remaining_g) + ' г'); })),
          h('div', { style: { display: 'flex', gap: 6, marginTop: 8 } },
            h('button', { className: 'btn', style: { marginTop: 0 }, onClick: function () { if (!pick[q.slot]) { app.toast('Выберите катушку'); return; } act({ action: 'cc2_assign', slot: q.slot, cc2_id: pick[q.slot] }, 'Катушка в лотке'); } }, 'Это она'),
            h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { act({ action: 'cc2_dismiss', slot: q.slot }, 'Лоток без учёта'); } }, 'Не со склада')));
      }));
  }

  function AdminPlastics(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var fs = useState(null); var form = fs[0], setForm = fs[1]; // null | {id?, ...}
    var sps = useState(null); var spOpen = sps[0], setSpOpen = sps[1]; // id пластика с раскрытыми катушками
    function load() { API.adminPlastics().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function act(body) { return API.adminPlastic(body).then(function (j) { if (j.ok) { load(); } else app.toast(j.error || 'Ошибка'); return j; }); }
    // Загрузка/замена фото катушки (восстановленный функционал) → POST /api/admin/photo, затем перечитываем список.
    function uploadPhoto(pid, file) {
      if (!file) return;
      if (file.size > 12 * 1024 * 1024) { app.toast('Фото больше 12 МБ — выберите поменьше'); return; }
      var rd = new FileReader();
      rd.onload = function () {
        app.toast('Загружаю фото…');
        API.adminPhoto('plastic', pid, rd.result).then(function (j) {
          app.toast(j.ok ? 'Фото обновлено' : (j.error || 'Не удалось загрузить'));
          if (j.ok) load();
        }).catch(function () { app.toast('Сеть недоступна'); });
      };
      rd.readAsDataURL(file);
    }
    function photoInput(pid) {
      return h('input', { type: 'file', accept: 'image/*', style: { display: 'none' },
        onChange: function (e) { if (e.target.files[0]) uploadPhoto(pid, e.target.files[0]); e.target.value = ''; } });
    }
    function openNew() { setForm({ name: '', material: (data.material_types || ['PLA'])[0], category: '', price: '', density: '1.24', colors: '', description: '', stock: '', low: '', failure: '', factor: '', cstock: {}, best_quality_id: 0, spool_weight: '', spool_note: '' }); }
    function openEdit(p) { setForm({ id: p.id, name: p.name, material: p.material, category: p.category || '', price: p.price, density: p.density, colors: (p.colors || []).join(', '), description: p.description || '', stock: p.stock, low: p.low, failure: (p.failure == null ? '' : p.failure), factor: (p.factor == null || Number(p.factor) === 1 ? '' : p.factor), cstock: p.color_stock || {}, best_quality_id: p.best_quality_id || 0, spool_weight: (p.spool_weight ? p.spool_weight : ''), spool_note: p.spool_note || '', cc2Colors: p.cc2_colors || [], cc2Stock: !!p.cc2_stock }); }
    function save() {
      if (!form.name.trim()) { app.toast('Введите название'); return; }
      var cstock = form.cstock || {};
      act({ action: 'save', id: form.id, name: form.name, price: form.price, density: form.density, material: form.material, category: form.category, description: form.description, failure: form.failure, factor: form.factor, best_quality_id: form.best_quality_id || null, spool_weight: form.spool_weight || 0, spool_note: form.spool_note || '', colors: form.colors.split(',').map(function (s) { return s.trim(); }).filter(Boolean) })
        .then(function (j) {
          var pid = form.id || j.id;
          var chain = Promise.resolve();
          if (form.stock !== '' || form.low !== '') chain = chain.then(function () { return act({ action: 'stock', id: pid, stock: form.stock || 0, low: form.low || 0 }); });
          Object.keys(cstock).forEach(function (c) {
            var cs = cstock[c] || {};
            if (cs.stock !== '' && cs.stock != null) chain = chain.then(function () { return act({ action: 'color_stock', id: pid, color: c, stock: cs.stock || 0, low: cs.low || 0 }); });
          });
          return chain;
        })
        .then(function () { setForm(null); });
    }
    function cstockSet(c, key, v) {
      var cur = Object.assign({}, (form.cstock || {})[c]); cur[key] = v;
      var cs = Object.assign({}, form.cstock); cs[c] = cur;
      setForm(Object.assign({}, form, { cstock: cs }));
    }
    function colorStockRows() {
      var cols = form.colors.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      if (!cols.length) return null;
      return h('div', { style: { marginTop: 6 } },
        h('div', { style: { fontSize: 12, color: 'var(--muted)', marginBottom: 4 } }, 'Остаток по цветам (необязательно — иначе берётся общий склад материала)'),
        cols.map(function (c) {
          var cs = (form.cstock && form.cstock[c]) || {};
          // остаток цвета, который ведёт принтер (катушки CC2 Control), — сумма катушек: только порог
          var owned = (form.cc2Colors || []).indexOf(c) >= 0;
          return h('div', { key: c, style: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 } },
            h('span', { style: { flex: 1, fontSize: 12.5, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' } }, c + (owned ? ' · принтер' : '')),
            h('input', { className: 'input mono', style: { flex: '0 0 92px', marginTop: 0 }, inputMode: 'decimal', placeholder: 'остаток', disabled: owned, title: owned ? 'остаток ведёт принтер — сумма его катушек' : '', value: cs.stock != null ? (owned ? Math.round(cs.stock) : cs.stock) : '', onChange: function (e) { cstockSet(c, 'stock', e.target.value); } }),
            h('input', { className: 'input mono', style: { flex: '0 0 78px', marginTop: 0 }, inputMode: 'decimal', placeholder: 'порог', value: cs.low != null ? cs.low : '', onChange: function (e) { cstockSet(c, 'low', e.target.value); } }));
        }));
    }
    // Полоса остатка под каждым цветом материала (визуализация color_stock[c].stock).
    // Масштаб общий для всех цветов карточки, чтобы полосы были сравнимы между собой.
    function colorStockBars(p) {
      var cs = p.color_stock || {};
      var names = Object.keys(cs);
      if (!names.length) return null;
      var maxBasis = 1000;
      names.forEach(function (c) {
        var e = cs[c] || {};
        maxBasis = Math.max(maxBasis, Number(e.stock) || 0, (Number(e.low) || 0) * 2);
      });
      return h('div', { style: { marginTop: 8, display: 'flex', flexDirection: 'column', gap: 8 } },
        names.map(function (c) {
          var e = cs[c] || {};
          var g = Number(e.stock) || 0;
          var low = Number(e.low) || 0;
          var lowOn = low > 0 && g <= low;
          var fill = lowOn ? 'var(--red)' : (low > 0 && g <= low * 2 ? 'var(--amber)' : undefined);
          return h('div', { key: c },
            h('div', { className: 'spread', style: { fontSize: 11.5, marginBottom: 4 } },
              h('span', { style: { display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 } },
                h('span', { className: 'dot-ring', style: { border: 0, width: 12, height: 12, background: (p.color_css && p.color_css[c]) || '#888' } }),
                h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--muted)' } }, c)),
              h('span', { className: 'mono', style: { color: lowOn ? 'var(--red)' : 'var(--text)', fontWeight: 600, flex: '0 0 auto', marginLeft: 8 } }, Math.round(g) + ' г')),
            h('div', { className: 'pbar' }, h('i', { style: { width: Math.min(100, g / maxBasis * 100) + '%', background: fill } })));
        }));
    }
    if (!data) return loading('Пластики');
    var mt = data.material_types || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Пластики'),
      !form && h(Cc2SpoolBar, { app: app, plastics: data.plastics || [], onChanged: load }),
      form
        ? h('div', { className: 'card' },
          field('Название', inp({ value: form.name, onChange: function (e) { setForm(Object.assign({}, form, { name: e.target.value })); } })),
          h('div', { style: { display: 'flex', gap: 8 } },
            h('div', { style: { flex: 1 } }, field('Материал', h('select', { className: 'input', value: form.material, onChange: function (e) { setForm(Object.assign({}, form, { material: e.target.value })); } }, mt.map(function (m) { return h('option', { key: m, value: m }, m); })))),
            h('div', { style: { flex: 1 } }, field('Цена, руб./кг', inp({ className: 'input mono', inputMode: 'decimal', value: form.price, onChange: function (e) { setForm(Object.assign({}, form, { price: e.target.value })); } }))),
            h('div', { style: { flex: 1 } }, field('Плотность', inp({ className: 'input mono', inputMode: 'decimal', value: form.density, onChange: function (e) { setForm(Object.assign({}, form, { density: e.target.value })); } })))),
          // категория витрины (3af25fc2): группирует палитру цветов на экране параметров клиента
          field('Категория витрины', h('select', { className: 'input', value: form.category || '',
            onChange: function (e) { setForm(Object.assign({}, form, { category: e.target.value })); } },
            h('option', { value: '' }, '— авто (по типу/названию)'),
            (data.plastic_categories || []).map(function (c) { return h('option', { key: c, value: c }, c); }))),
          field('Цвета (через запятую)', inp({ value: form.colors, onChange: function (e) { setForm(Object.assign({}, form, { colors: e.target.value })); } })),
          field('Описание', inp({ value: form.description, onChange: function (e) { setForm(Object.assign({}, form, { description: e.target.value })); } })),
          // капшены-разделители (аудит UX): форма из 9 полей без групп читалась простынёй
          h('div', { style: { fontSize: 11, fontWeight: 700, letterSpacing: '.7px', textTransform: 'uppercase', color: 'var(--faint)', margin: '14px 2px 2px' } }, 'Склад'),
          // Несколько цветов → склад ведётся ПО ЦВЕТАМ, общая «катушка» только путает
          // (всегда 0) — прячем её поля (OpenTodo b2dda53f)
          (form.colors || '').split(',').filter(function (s) { return s.trim(); }).length <= 1
            && h('div', { style: { display: 'flex', gap: 8 } },
              h('div', { style: { flex: 1 } }, field(form.cc2Stock ? 'Остаток, г — ведёт принтер' : 'Остаток, г', inp({ className: 'input mono', inputMode: 'decimal', disabled: !!form.cc2Stock, value: form.stock, onChange: function (e) { setForm(Object.assign({}, form, { stock: e.target.value })); } }))),
              h('div', { style: { flex: 1 } }, field('Порог низкого, г', inp({ className: 'input mono', inputMode: 'decimal', value: form.low, onChange: function (e) { setForm(Object.assign({}, form, { low: e.target.value })); } })))),
          colorStockRows(),
          // Тара катушки: остаток филамента = показание весов − вес пустой шпули. Клиенту не
          // идёт; оговорка нужна там, где шпуля зависит от партии (картон 150 / пластик 195).
          h('div', { style: { display: 'flex', gap: 8 } },
            h('div', { style: { flex: '0 0 40%' } }, field('Вес пустой катушки, г', inp({ className: 'input mono', inputMode: 'decimal', value: form.spool_weight, onChange: function (e) { setForm(Object.assign({}, form, { spool_weight: e.target.value })); } }))),
            h('div', { style: { flex: 1 } }, field('Оговорка к катушке', inp({ placeholder: 'например: у части партий пластиковая 195 г', value: form.spool_note, onChange: function (e) { setForm(Object.assign({}, form, { spool_note: e.target.value })); } })))),
          h('div', { style: { fontSize: 11, fontWeight: 700, letterSpacing: '.7px', textTransform: 'uppercase', color: 'var(--faint)', margin: '14px 2px 2px' } }, 'Параметры расчёта'),
          field('% брака (пусто = стандарт ' + (data.failure_default != null ? data.failure_default : '') + '%)', inp({ className: 'input mono', inputMode: 'decimal', value: form.failure, onChange: function (e) { setForm(Object.assign({}, form, { failure: e.target.value })); } })),
          // Коэффициент сложности: клиент видит цену уже ×коэф, сам коэффициент — только здесь
          field('Коэффициент сложности, ×цена (пусто = 1)', inp({ className: 'input mono', inputMode: 'decimal', placeholder: '1', value: form.factor, onChange: function (e) { setForm(Object.assign({}, form, { factor: e.target.value })); } })),
          // Лучший профиль печати этого пластика (94cc52c7): открытие модели сразу ставит его
          field('Лучший профиль печати (юзеру подставится сам)', h('select', { className: 'input', value: form.best_quality_id || 0,
            onChange: function (e) { setForm(Object.assign({}, form, { best_quality_id: Number(e.target.value) })); } },
            h('option', { value: 0 }, '— стандартный (★ из пресетов)'),
            ((app.options && app.options.qualities) || []).map(function (q) { return h('option', { key: q.id, value: q.id }, q.label); }))),
          h('div', { style: { display: 'flex', gap: 8, marginTop: 4 } },
            h('button', { className: 'btn', style: { marginTop: 0 }, onClick: save }, icon('check', { size: 17 }), ' Сохранить'),
            h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { setForm(null); } }, 'Отмена')))
        : addHeader(false, openNew, 'Добавить пластик'),
      !form && (data.plastics || []).map(function (p) {
        var lowOn = p.low > 0 && p.stock < p.low;
        return h('div', { key: p.id, className: 'card', style: { marginTop: 12, opacity: p.is_active ? 1 : 0.55 } },
          h('div', { style: { display: 'flex', gap: 12 } },
            h('label', { className: 'pthumb', title: p.photo ? 'Заменить фото катушки' : 'Добавить фото катушки' },
              p.photo ? h('img', { src: p.photo, alt: '' }) : h('div', { className: 'ph' }, icon('spool', { size: 22 })),
              h('span', { className: 'edit' }, icon('pencil', { size: 11 })),
              photoInput(p.id)),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' } },
                h('b', { style: { fontSize: 14.5 } }, p.name),
                p.is_default && h('span', { className: 'stbadge st-printed', style: { marginTop: 0, padding: '2px 7px', fontSize: 10.5 } }, '★ по умолч.'),
                !p.is_active && h('span', { className: 'stbadge st-rejected', style: { marginTop: 0, padding: '2px 7px', fontSize: 10.5 } }, 'выкл')),
              h('div', { className: 'mono', style: { fontSize: 12, color: 'var(--muted)', marginTop: 4 } }, p.material + ' · ' + p.price + ' руб./кг · ' + p.density + ' г/см³ · расчётов ' + p.uses
                + (p.failure != null ? ' · брак ' + p.failure + '%' : '')
                + (p.factor != null && Number(p.factor) !== 1 ? ' · коэф ×' + p.factor : '')
                + (p.spool_weight ? ' · катушка ' + p.spool_weight + ' г' : '')),
              p.spool_note && h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } }, p.spool_note))),
          // у многоцветного пластика общий «Остаток» (вечный 0) прячем — есть полосы по цветам.
          // Резерв под активные заказы (a89586a2) — синий сегмент справа: заказы РЕЗЕРВИРУЮТ
          // филамент, реально списывается при «напечатано».
          !((p.colors || []).length > 1 && !(p.stock > 0)) && (function () {
            var stock = p.stock || 0, res = Math.min(p.reserved || 0, stock);
            var availPct = Math.min(100, Math.max(0, (stock - res)) / 1000 * 100);
            var resPct = Math.min(100 - availPct, res / 1000 * 100);
            return h('div', { style: { marginTop: 10 } },
              h('div', { className: 'spread', style: { fontSize: 12, marginBottom: 5 } },
                h('span', { style: { color: 'var(--muted)' } }, 'Остаток' + (res > 0 ? ' · резерв ' + Math.round(res) + ' г' : '')),
                h('span', { className: 'mono', style: { color: lowOn ? 'var(--red)' : 'var(--text)', fontWeight: 600 } }, Math.round(stock) + ' г из 1000')),
              h('div', { style: { display: 'flex', height: 8, borderRadius: 6, overflow: 'hidden', background: 'var(--surface-2)' } },
                h('div', { style: { width: availPct + '%', background: lowOn ? 'var(--red)' : 'var(--accent)' } }),
                res > 0 && h('div', { title: 'Зарезервировано под заказы', style: { width: resPct + '%', background: '#3a86ff' } })));
          })(),
          colorStockBars(p),
          actionsRow([
            h('label', { key: 'photo', className: 'iconlabel', title: 'Фото катушки' }, icon('image', { size: 16 }), photoInput(p.id)),
            iconBtn('pencil', function () { openEdit(p); }, { title: 'Изменить' }),
            iconBtn('spool', function () { setSpOpen(spOpen === p.id ? null : p.id); }, { title: 'Катушки', on: spOpen === p.id }),
            iconBtn('star', function () { act({ action: 'default', id: p.id }); }, { title: 'По умолчанию', on: p.is_default }),
            iconBtn(p.is_active ? 'check' : 'close', function () { act({ action: 'toggle', id: p.id }); }, { title: 'Вкл/выкл' }),
            // зазор перед удалением (аудит UX): опасное действие не должно стоять вплотную
            h('span', { key: 'gap', style: { width: 14, flex: '0 0 auto' } }),
            iconBtn('trash', function () { if (confirm('Удалить «' + p.name + '»?')) act({ action: 'delete', id: p.id }); }, { title: 'Удалить', danger: true }),
          ]),
          spOpen === p.id && h(SpoolPanel, { app: app, plasticId: p.id, colorCss: p.color_css || {},
            colors: p.colors || [], tare: p.spool_weight || '', onChanged: load }));
      }));
  }

  // ───────────────────── PROMOS ─────────────────────
  function AdminPromos(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var fs = useState(false); var open = fs[0], setOpen = fs[1];
    var nf = useState({ code: '', discount_pct: '', days: '', max_uses: '' }); var nv = nf[0], setNv = nf[1];
    function load() { API.adminPromos().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function add() { if (!nv.code.trim()) { app.toast('Введите код'); return; } API.adminPromo({ action: 'add', code: nv.code, discount_pct: nv.discount_pct, days: nv.days, max_uses: nv.max_uses }).then(function (j) { if (j.ok) { setNv({ code: '', discount_pct: '', days: '', max_uses: '' }); setOpen(false); load(); } else app.toast(j.error || 'Ошибка'); }); }
    function del(code) { if (confirm('Удалить промокод ' + code + '?')) API.adminPromo({ action: 'delete', code: code }).then(function (j) { if (j.ok) load(); }); }
    if (!data) return loading('Промокоды');
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Промокоды'),
      addHeader(open, setOpen, 'Новый промокод'),
      open && h('div', { className: 'card', style: { marginTop: 10 } },
        h('div', { style: { display: 'flex', gap: 8 } },
          h('div', { style: { flex: 1 } }, field('Код', inp({ className: 'input mono', value: nv.code, onChange: function (e) { setNv(Object.assign({}, nv, { code: e.target.value.toUpperCase() })); } }))),
          h('div', { style: { flex: 1 } }, field('Скидка, %', inp({ className: 'input mono', inputMode: 'decimal', value: nv.discount_pct, onChange: function (e) { setNv(Object.assign({}, nv, { discount_pct: e.target.value })); } })))),
        h('div', { style: { display: 'flex', gap: 8 } },
          h('div', { style: { flex: 1 } }, field('Срок, дней (0 = бессрочно)', inp({ className: 'input mono', inputMode: 'numeric', value: nv.days, onChange: function (e) { setNv(Object.assign({}, nv, { days: e.target.value })); } }))),
          h('div', { style: { flex: 1 } }, field('Лимит исп. (0 = ∞)', inp({ className: 'input mono', inputMode: 'numeric', value: nv.max_uses, onChange: function (e) { setNv(Object.assign({}, nv, { max_uses: e.target.value })); } })))),
        h('button', { className: 'btn', style: { marginTop: 4 }, onClick: add }, icon('check', { size: 17 }), ' Создать')),
      (data.promos || []).length === 0 && !open && h('div', { className: 'empty' }, icon('tag', { size: 46 }), h('div', null, 'Промокодов нет.')),
      (data.promos || []).map(function (p) {
        var exp = p.expires_at ? new Date(p.expires_at * 1000).toLocaleDateString('ru') : 'бессрочно';
        return h('div', { key: p.code, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 11 } },
          h('div', { className: 'mono', style: { fontSize: 20, fontWeight: 700, color: 'var(--accent)' } }, '−' + p.discount_pct + '%'),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { className: 'mono', style: { fontWeight: 700, fontSize: 15 } }, p.code),
            h('div', { style: { fontSize: 12, color: 'var(--muted)', marginTop: 3 } }, 'до ' + exp + ' · использован ' + (p.uses || 0) + (p.max_uses ? ' / ' + p.max_uses : ''))),
          iconBtn('trash', function () { del(p.code); }, { danger: true, title: 'Удалить' }));
      }));
  }

  // ───────────────────── SETTINGS (pricing + texts) ─────────────────────
  function SettingRow(props) {
    var s = props.s, save = props.save;
    var vs = useState(props.value); var val = vs[0], setVal = vs[1];
    // подсветка несохранённого изменения (аудит UX): видно, где галочку ещё не нажали
    var dirty = String(val) !== String(props.value);
    return h('div', { className: 'card', style: { marginTop: 10, padding: 12 } },
      h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginBottom: 7 } }, props.label),
      h('div', { style: { display: 'flex', gap: 8 } },
        inp({ className: 'input' + (props.kind === 'num' ? ' mono' : ''), inputMode: props.kind === 'num' ? 'decimal' : undefined, value: val, onChange: function (e) { setVal(e.target.value); } }),
        h('button', { className: 'btn' + (dirty ? '' : ' soft'), style: { marginTop: 0, width: 'auto', padding: '0 16px' },
          title: dirty ? 'Есть несохранённое изменение' : undefined,
          onClick: function () { save(props.skey, props.kind, val); } }, icon('check', { size: 17 }))));
  }
  // Настройки-тумблеры (OpenTodo 67dfaeaf: «сделать в админке чекбокс»). В базе это те же
  // числовые настройки 0/1, что и раньше, — меняется только их вид в админке: вместо поля,
  // куда надо вписать «1», и отдельной кнопки-галочки — переключатель, сохраняющий сразу.
  // {ключ: [иконка, короткая подпись под названием]}
  var BOOL_SETTINGS = {
    slicing_enabled: ['printer', 'Выкл. — цена только быстрым расчётом, мастер уточнит по факту'],
    sanitize_3mf: ['doc', 'Выкл. — .3mf остаётся в исходном виде, файл автора не переписывается'],
    time_prediction: ['clock', 'Коррекция времени печати по накопленным парам оценка/факт'],
    nudge_abandoned_enabled: ['bell', 'Вкл. — клиенту с брошенным расчётом придёт одно напоминание'],
  };
  // Любой ключ 0/1: явно перечисленные в BOOL_SETTINGS ИЛИ по общему правилу `*_enabled`
  // (чтобы новые флаги-переключатели не приходилось каждый раз дописывать сюда руками).
  function isBoolSetting(k) { return !!BOOL_SETTINGS[k] || /_enabled$/.test(k); }
  function SettingToggle(props) {
    var on = String(props.value) === '1' || props.value === 1 || props.value === true;
    var meta = BOOL_SETTINGS[props.skey] || [];
    // тумблер сохраняет сразу (кнопка-галочка тут была бы лишним шагом); значение уходит тем же
    // kind:'num', что и раньше, — бэкенд и формат хранения не меняются
    function flip() { props.save(props.skey, 'num', on ? '0' : '1'); props.onSaved(props.skey, on ? '0' : '1'); }
    return h('div', { className: 'card', style: { marginTop: 10, padding: '2px 12px' } },
      h('div', { className: 'tgl' },
        h('span', { className: 'ic' }, icon(meta[0] || 'sliders', { size: 17 })),
        h('div', { className: 'meta' },
          h('div', { className: 't' }, props.label),
          meta[1] && h('div', { className: 's' }, meta[1])),
        h('div', { className: 'sw' + (on ? ' on' : ''), role: 'switch', 'aria-checked': on,
          onClick: flip }, h('i', null))));
  }
  // Группировка настроек по смыслу (аудит UX: плоский список из 8 одинаковых карточек)
  var SETTING_GROUPS = [
    ['Ставки и наценка', ['machine_hour_rate', 'setup_fee', 'plate_fee', 'failure_rate_pct', 'profit_margin_pct', 'min_price']],
    ['Многоцветная печать', ['multicolor_waste_pct', 'multicolor_time_pct']],
    ['Большая модель', ['volume_threshold_cm3', 'volume_surcharge_pct']],
  ];
  // Операционные настройки — поведение бота, а не прайс: слайс/санация/коррекция времени,
  // догон брошенных расчётов, срочность и сроки, печатопригодность. 2026-09-02 они переехали
  // на экран «Настройки» (screens_admin4.js), как в настольной админке (settings.jsx GROUPS), и
  // здесь, в «Ценах», больше не показываются — двух мест правды быть не должно. Список один:
  // его же читает экран «Настройки» (window.PS_OperationalGroups).
  var OPERATIONAL_GROUPS = [
    { t: 'Расчёт и файлы моделей', ic: 'printer', keys: ['slicing_enabled', 'sanitize_3mf', 'time_prediction'] },
    { t: 'Догон брошенных расчётов', ic: 'bell', keys: ['nudge_abandoned_enabled', 'nudge_abandoned_hours', 'nudge_abandoned_max_days'] },
    { t: 'Срочность и сроки', ic: 'clock', keys: ['rush_surcharge_pct', 'term_split_hours'] },
    { t: 'Печатопригодность', ic: 'shield', keys: ['dfm_min_wall_mm'] },
  ];
  var OPERATIONAL_KEYS = {};
  OPERATIONAL_GROUPS.forEach(function (g) { g.keys.forEach(function (k) { OPERATIONAL_KEYS[k] = 1; }); });
  function AdminSettings(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    function load() { API.adminSettings().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function save(key, kind, value) { API.adminSetting({ key: key, kind: kind, value: value }).then(function (j) { app.toast(j.ok ? 'Сохранено' : (j.error || 'Ошибка')); }); }
    // Тумблер рисуется от значения из data — правим его оптимистично, иначе переключатель
    // отскочит в прежнее положение и «не переключится» до перезагрузки экрана.
    function onSaved(key, value) {
      setData(function (d) {
        if (!d) return d;
        var pr2 = Object.assign({}, d.pricing);
        pr2.values = Object.assign({}, pr2.values); pr2.values[key] = value;
        return Object.assign({}, d, { pricing: pr2 });
      });
    }
    if (!data) return loading('Ценообразование');
    var pr = data.pricing, tx = data.texts;
    var known = {};
    SETTING_GROUPS.forEach(function (g) { g[1].forEach(function (k) { known[k] = 1; }); });
    // новые ключи не потеряются; операционные (поведение бота) живут на экране «Настройки»
    var rest = pr.order.filter(function (k) { return !known[k] && !OPERATIONAL_KEYS[k]; });
    // настройки 0/1 — тумблером, остальные — прежним полем с кнопкой-галочкой (67dfaeaf).
    // Признак тумблера — isBoolSetting: список BOOL_SETTINGS ИЛИ общее правило `*_enabled`.
    function row(k) {
      return isBoolSetting(k)
        ? h(SettingToggle, { key: k, skey: k, label: pr.labels[k], value: pr.values[k], save: save, onSaved: onSaved })
        : h(SettingRow, { key: k, skey: k, kind: 'num', label: pr.labels[k], value: pr.values[k], save: save });
    }
    function rows(keys) {
      return keys.filter(function (k) { return pr.order.indexOf(k) >= 0; }).map(row);
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Ценообразование'),
      SETTING_GROUPS.map(function (g, i) {
        var rr = rows(g[1]);
        if (!rr.length) return null;
        return h(React.Fragment, { key: g[0] }, i > 0 && h(Lab, null, g[0]), rr);
      }),
      rest.length > 0 && h(Lab, null, 'Прочее'),
      rest.map(row),
      // подсказка-редирект на переходный период (IA-редизайн, аудит #7)
      h('p', { className: 'hint', style: { marginTop: 10 } }, 'Пороги сегментов клиентов (VIP / «спящий» / пауза рассылок) переехали: Клиенты → «Настройка сегментов». Поведение бота (слайс, догон, срочность, DFM) — в «Ещё → Настройки».'),
      h(Lab, null, 'Реквизиты в смете'),
      tx.order.map(function (k) { return h(SettingRow, { key: k, skey: k, kind: 'text', label: tx.labels[k], value: tx.values[k], save: save }); }),
      // Онлайн-оплата (A1): токен провайдера из BotFather + валюта счетов. Пустой токен =
      // оплата выключена (кнопок «Оплатить» ни у кого не появляется).
      data.pay && h(React.Fragment, null,
        h(Lab, null, 'Онлайн-оплата'),
        h('p', { className: 'hint', style: { margin: '6px 2px 0' } },
          'Подключите провайдера в BotFather (Bot Settings → Payments): ЮKassa для РФ, bePaid для Беларуси, Stripe для карт мира. Полученный токен вставьте сюда — у клиентов появится кнопка «Оплатить». Валюта должна совпадать с валютой ваших цен и поддерживаться провайдером.'),
        data.pay.order.map(function (k) { return h(SettingRow, { key: k, skey: k, kind: 'text', label: data.pay.labels[k], value: data.pay.values[k], save: save }); })),
      // Сообщество владельцев студий (c356387a): ссылка на внешнюю Telegram-группу.
      // Это площадка ВЛАДЕЛЬЦЕВ, а не заказчиков — клиентам мастерской она не видна.
      data.owners && h(React.Fragment, null,
        h(Lab, null, 'Сообщество владельцев'),
        h('p', { className: 'hint', style: { margin: '6px 2px 0' } },
          'Группа в Telegram, где общаются владельцы студий. Ссылка видна только в админке — на экране «База знаний». Пусто — вход не показываем.'),
        data.owners.order.map(function (k) { return h(SettingRow, { key: k, skey: k, kind: 'text', label: data.owners.labels[k], value: data.owners.values[k], save: save }); })),
      // Бренд мастерской (A3, white-label). Группа приходит отдельно от реквизитов: это
      // разные вещи — одно печатается в PDF, другое видит клиент в приложении.
      data.brand ? h(BrandSection, { app: app, group: data.brand, current: data.brand_current, reload: load }) : null);
  }

  /**
   * Бренд мастерской: имя, подпись, акцентный цвет и логотип. Поля — обычные text-настройки,
   * логотип — файл со своим эндпоинтом (бинарь в text_settings не положишь).
   *
   * Предпросмотр рисуем шапкой мини-аппа: по значениям в полях не видно, как это будет
   * выглядеть у клиента, а именно это мастеру и важно.
   */
  function BrandSection(props) {
    var app = props.app, group = props.group, cur = props.current || {};
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    var fileRef = useRef(null);
    function save(key, kind, value) {
      API.adminSetting({ key: key, kind: kind, value: value }).then(function (j) {
        app.toast(j.ok ? 'Сохранено — обновите приложение' : (j.error || 'Ошибка'));
        if (j.ok) props.reload();
      });
    }
    function upload(f) {
      if (!f) return;
      setBusy(true);
      API.adminBrandLogo(f).then(function (j) {
        app.toast(j && j.ok ? 'Логотип загружен' : ((j && j.error) || 'Ошибка'));
        if (j && j.ok) props.reload();
      }).catch(function () { app.toast('Не удалось загрузить'); }).then(function () { setBusy(false); });
    }
    function clearLogo() {
      setBusy(true);
      API.adminBrandLogoClear().then(function (j) {
        app.toast(j && j.ok ? 'Логотип снят' : 'Ошибка');
        if (j && j.ok) props.reload();
      }).then(function () { setBusy(false); });
    }
    return h(React.Fragment, null,
      h(Lab, null, 'Бренд мастерской'),
      h('div', { className: 'card', style: { display: 'flex', alignItems: 'center', gap: 11, padding: 12 } },
        cur.logo
          ? h('img', { className: 'mark', src: cur.logo, alt: '', width: 25, height: 25 })
          : h('svg', { className: 'mark', width: 25, height: 25, viewBox: '0 0 24 24', fill: 'none', stroke: cur.accent || 'var(--accent)', strokeWidth: 1.6, strokeLinecap: 'round' },
            h('path', { d: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6M12 3v6M12 15v6M3 12h6M15 12h6' })),
        h('div', { style: { minWidth: 0 } },
          h('div', { style: { fontWeight: 700, fontSize: 13.5, letterSpacing: '.4px' } }, cur.title || 'Print Studio'),
          h('div', { className: 'hint', style: { margin: 0 } }, cur.tagline || '')),
        h('span', { title: cur.accent, style: { marginLeft: 'auto', width: 22, height: 22, borderRadius: 7, flex: '0 0 auto', background: cur.accent || 'var(--accent)', border: '1px solid var(--line-2)' } })),
      group.order.map(function (k) {
        return h(SettingRow, { key: k, skey: k, kind: 'text', label: group.labels[k], value: group.values[k], save: save });
      }),
      h('div', { className: 'card', style: { marginTop: 10, padding: 12 } },
        h('div', { style: { fontSize: 13.5, marginBottom: 9 } }, 'Логотип · SVG, PNG, WEBP, JPG до 512 КБ'),
        h('input', { ref: fileRef, type: 'file', accept: '.svg,.png,.webp,.jpg,.jpeg', style: { display: 'none' },
          onChange: function (e) { upload(e.target.files && e.target.files[0]); e.target.value = ''; } }),
        h('div', { style: { display: 'flex', gap: 8 } },
          h('button', { className: 'btn line', disabled: busy, onClick: function () { if (fileRef.current) fileRef.current.click(); } },
            cur.logo ? 'Заменить' : 'Загрузить'),
          cur.logo ? h('button', { className: 'btn line', disabled: busy, onClick: clearLogo }, 'Снять') : null)),
      h('p', { className: 'hint', style: { marginTop: 10 } },
        'Пустое поле = наше значение по умолчанию. Клиент увидит новый бренд после перезагрузки приложения: он вшивается в страницу на сервере, чтобы не мелькал старый.'));
  }

  // ───────────────────── PRICE CALCULATOR ─────────────────────
  // Ручной калькулятор цены: вбиваешь граммы/время/цену/скидку → тот же расчёт, что при
  // слайсинге модели (бэкенд зовёт pricing.compute_price), результат — в общей карточке Breakdown.
  function AdminPriceCalc(props) {
    var app = props.app;
    var fs = useState({ grams: '', hours: '', minutes: '', price_per_kg: '', discount_pct: '', qty: '1' });
    var f = fs[0], setF = fs[1];
    // Многоцвет (OpenTodo 82ebeeef): граммы по каждому цвету своей ценой за кг; время общее.
    var mcS = useState(false); var mc = mcS[0], setMc = mcS[1];
    var rowsS = useState([{ grams: '', price_per_kg: '' }, { grams: '', price_per_kg: '' }]);
    var rows = rowsS[0], setRows = rowsS[1];
    var rs = useState(null); var res = rs[0], setRes = rs[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    function upd(k) { return function (e) { var v = e.target.value; setF(function (p) { var o = Object.assign({}, p); o[k] = v; return o; }); }; }
    function num(v) { return parseFloat(String(v).replace(',', '.')) || 0; }
    function patchRow(i, k, v) { setRows(function (rs2) { return rs2.map(function (r, x) { if (x !== i) return r; var n = Object.assign({}, r); n[k] = v; return n; }); }); }
    function calc() {
      var minutes = num(f.hours) * 60 + num(f.minutes);
      var body = { minutes: minutes, discount_pct: f.discount_pct, qty: f.qty };
      if (mc) {
        var valid = rows.filter(function (r) { return num(r.grams) > 0 && num(r.price_per_kg) >= 0; });
        if (valid.length < 2 || valid.length !== rows.length) { app.toast('Заполните граммы и цену в каждом цвете (минимум два)'); return; }
        body.colors = rows.map(function (r) { return { grams: r.grams, price_per_kg: r.price_per_kg }; });
      } else {
        if (num(f.grams) <= 0) { app.toast('Введите граммы пластика'); return; }
        body.grams = f.grams; body.price_per_kg = f.price_per_kg;
      }
      setBusy(true); setRes(null);
      API.adminPriceCalc(body)
        .then(function (j) { setBusy(false); if (j.ok) setRes(j.bd); else app.toast(j.error || 'Ошибка'); })
        .catch(function () { setBusy(false); app.toast('Ошибка расчёта'); });
    }
    return h('div', { className: 'screen' },
      h(Lab, null, 'Калькулятор цены'),
      h('div', { className: 'card' },
        h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 } },
          'Тот же расчёт, что и при слайсинге модели — по текущим настройкам цен (ставка времени, брак, наценка, мин. цена).'),
        h('div', { className: 'chips', style: { marginBottom: 10 } },
          h('button', { className: 'chip' + (mc ? '' : ' on'), type: 'button', onClick: function () { setMc(false); } }, 'Один цвет'),
          h('button', { className: 'chip' + (mc ? ' on' : ''), type: 'button', onClick: function () { setMc(true); } }, icon('palette', { size: 15 }), ' Многоцвет')),
        mc
          ? h('div', { style: { marginBottom: 4 } },
              rows.map(function (r, i) {
                return h('div', { key: i, style: { display: 'flex', gap: 8, marginTop: i ? 7 : 0, alignItems: 'center' } },
                  inp({ className: 'input mono', inputMode: 'decimal', placeholder: 'цвет ' + (i + 1) + ', г',
                    value: r.grams, onChange: function (e) { patchRow(i, 'grams', e.target.value); } }),
                  inp({ className: 'input mono', inputMode: 'decimal', placeholder: 'цена/кг',
                    value: r.price_per_kg, onChange: function (e) { patchRow(i, 'price_per_kg', e.target.value); } }),
                  rows.length > 2 && h('button', { className: 'btn line', type: 'button', 'aria-label': 'Убрать цвет',
                    style: { flex: '0 0 auto', width: 38, padding: 0, marginTop: 0 },
                    onClick: function () { setRows(rows.filter(function (_, x) { return x !== i; })); } }, icon('close', { size: 15 })));
              }),
              h('button', { className: 'btn line', type: 'button', style: { marginTop: 8 }, onClick: function () { setRows(rows.concat([{ grams: '', price_per_kg: '' }])); } },
                icon('plus', { size: 15 }), ' Добавить цвет'),
              h('p', { className: 'hint', style: { marginTop: 7 } }, 'Граммы по каждому цвету — из слайсера (продувка уже в них), время — общее.'))
          : h('div', { style: { display: 'flex', gap: 8 } },
              h('div', { style: { flex: 1 } }, field('Пластик, г', inp({ className: 'input mono', inputMode: 'decimal', placeholder: '0', value: f.grams, onChange: upd('grams') }))),
              h('div', { style: { flex: 1 } }, field('Цена пластика, за кг', inp({ className: 'input mono', inputMode: 'decimal', placeholder: '0', value: f.price_per_kg, onChange: upd('price_per_kg') })))),
        field('Время печати',
          h('div', { style: { display: 'flex', gap: 8 } },
            inp({ className: 'input mono', inputMode: 'numeric', placeholder: 'часы', value: f.hours, onChange: upd('hours') }),
            inp({ className: 'input mono', inputMode: 'numeric', placeholder: 'минуты', value: f.minutes, onChange: upd('minutes') }))),
        h('div', { style: { display: 'flex', gap: 8 } },
          h('div', { style: { flex: 1 } }, field('Скидка, %', inp({ className: 'input mono', inputMode: 'decimal', placeholder: '0', value: f.discount_pct, onChange: upd('discount_pct') }))),
          h('div', { style: { flex: 1 } }, field('Количество, шт', inp({ className: 'input mono', inputMode: 'numeric', value: f.qty, onChange: upd('qty') })))),
        h('button', { className: 'btn', disabled: busy, onClick: calc }, busy ? 'Считаю…' : 'Рассчитать')),
      res && h('div', null,
        h(Breakdown, { bd: res, title: 'Расчёт' }),
        (res.qty > 1) && h('div', { style: { textAlign: 'center', fontSize: 12, color: 'var(--faint)', marginTop: 6 } },
          'итог за ' + res.qty + ' шт · ' + res.grams_total + ' г · ' + res.time_total)));
  }

  // ───────────────────── DISCOUNTS ─────────────────────
  function AdminDiscounts(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var tf = useState({ min_qty: '', pct: '' }); var tier = tf[0], setTier = tf[1];
    var uf = useState({ uid: '', pct: '', name: '' }); var usr = uf[0], setUsr = uf[1];
    function load() { API.adminDiscounts().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function act(body) { API.adminDiscount(body).then(function (j) { if (j.ok) load(); else app.toast(j.error || 'Ошибка'); }); }
    if (!data) return loading('Скидки');
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Скидки за количество'),
      (data.tiers || []).map(function (t) {
        return h('div', { key: t.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 11 } },
          h('div', { style: { flex: 1 } }, h('b', null, 'от ' + t.min_qty + ' шт'), h('span', { className: 'mono', style: { color: 'var(--price)', marginLeft: 8, fontWeight: 700 } }, '−' + t.pct + '%')),
          iconBtn('trash', function () { act({ action: 'del_tier', id: t.id }); }, { danger: true }));
      }),
      h('div', { className: 'card', style: { marginTop: 10, display: 'flex', gap: 8, alignItems: 'flex-end' } },
        h('div', { style: { flex: 1 } }, field('Кол-во, от', inp({ className: 'input mono', inputMode: 'numeric', value: tier.min_qty, onChange: function (e) { setTier(Object.assign({}, tier, { min_qty: e.target.value })); } }))),
        h('div', { style: { flex: 1 } }, field('Скидка, %', inp({ className: 'input mono', inputMode: 'decimal', value: tier.pct, onChange: function (e) { setTier(Object.assign({}, tier, { pct: e.target.value })); } }))),
        h('button', { className: 'btn', style: { marginTop: 0, width: 'auto', padding: '0 16px', flex: '0 0 auto', height: 46 }, onClick: function () { act({ action: 'add_tier', min_qty: tier.min_qty, pct: tier.pct }); setTier({ min_qty: '', pct: '' }); } }, icon('plus', { size: 17 }))),

      h(Lab, null, 'Персональные скидки'),
      (data.users || []).map(function (u) {
        return h('div', { key: u.uid, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 11 } },
          h('div', { style: { flex: 1, minWidth: 0 } }, h('b', null, u.name || ('id ' + u.uid)), h('span', { className: 'mono', style: { color: 'var(--price)', marginLeft: 8, fontWeight: 700 } }, '−' + u.pct + '%'), h('div', { className: 'mono', style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } }, 'id ' + u.uid)),
          iconBtn('trash', function () { act({ action: 'del_user', uid: u.uid }); }, { danger: true }));
      }),
      h('div', { className: 'card', style: { marginTop: 10 } },
        (data.customers && data.customers.length > 0) && field('Выбрать клиента из заказов', h('select', {
          className: 'input', value: usr.uid || '',
          onChange: function (e) {
            var c = (data.customers || []).filter(function (x) { return String(x.uid) === e.target.value; })[0];
            if (c) setUsr({ uid: String(c.uid), name: c.name || '', pct: (c.pct != null ? String(c.pct) : usr.pct) });
            else setUsr(Object.assign({}, usr, { uid: '' }));
          }
        }, [h('option', { key: '_', value: '' }, '— выбрать из списка —')].concat((data.customers || []).map(function (c) {
          return h('option', { key: c.uid, value: String(c.uid) }, (c.name || ('id ' + c.uid)) + ' · ' + c.orders + ' зак.' + (c.pct != null ? ' · уже −' + c.pct + '%' : ''));
        })))),
        h('div', { style: { display: 'flex', gap: 8 } },
          h('div', { style: { flex: 2 } }, field('Telegram ID', inp({ className: 'input mono', inputMode: 'numeric', value: usr.uid, onChange: function (e) { setUsr(Object.assign({}, usr, { uid: e.target.value })); } }))),
          h('div', { style: { flex: 1 } }, field('Скидка, %', inp({ className: 'input mono', inputMode: 'decimal', value: usr.pct, onChange: function (e) { setUsr(Object.assign({}, usr, { pct: e.target.value })); } })))),
        field('Имя (необязательно)', inp({ value: usr.name, onChange: function (e) { setUsr(Object.assign({}, usr, { name: e.target.value })); } })),
        h('button', { className: 'btn soft', style: { marginTop: 4 }, onClick: function () { act({ action: 'add_user', uid: usr.uid, pct: usr.pct, name: usr.name }); setUsr({ uid: '', pct: '', name: '' }); } }, icon('plus', { size: 17 }), ' Добавить')));
      // «Объединить счета» удалено (2026-07-16): слияние уничтожало историю второго клиента.
      // Семейные счета живут в «Уведомлениях» админки (AdminNotifs, screens_admin3.js).
  }

  // ───────────────────── SHADES ─────────────────────
  function AdminShades(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var nf = useState({ name: '', hex: '#2f6fd6' }); var nv = nf[0], setNv = nf[1];
    function load() { API.adminShades().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function act(body) { API.adminShade(body).then(function (j) { if (j.ok) load(); else app.toast(j.error || 'Ошибка'); }); }
    if (!data) return loading('Оттенки цветов');
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Оттенки цветов'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Соответствие названия цвета и HEX — для кружков-образцов в каталоге и заказах.'),
      h('div', { className: 'card', style: { marginTop: 10, display: 'flex', gap: 8, alignItems: 'flex-end' } },
        h('input', { type: 'color', value: nv.hex, onChange: function (e) { setNv(Object.assign({}, nv, { hex: e.target.value })); }, style: { width: 46, height: 46, border: 0, background: 'none', padding: 0, flex: '0 0 auto', borderRadius: 10 } }),
        h('div', { style: { flex: 1 } }, field('Название цвета', inp({ value: nv.name, onChange: function (e) { setNv(Object.assign({}, nv, { name: e.target.value })); } }))),
        h('button', { className: 'btn', style: { marginTop: 0, width: 'auto', padding: '0 16px', flex: '0 0 auto', height: 46 }, onClick: function () { if (!nv.name.trim()) { app.toast('Введите название'); return; } act({ action: 'add', name: nv.name, hex: nv.hex }); setNv({ name: '', hex: '#2f6fd6' }); } }, icon('plus', { size: 17 }))),
      h('div', { style: { marginTop: 6 } }, (data.shades || []).map(function (s) {
        return h('div', { key: s.name, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 12, padding: 11 } },
          h('span', { style: { width: 26, height: 26, borderRadius: '50%', background: s.hex, border: '1px solid var(--line-2)', flex: '0 0 auto' } }),
          h('div', { style: { flex: 1, minWidth: 0 } }, h('b', null, s.name), h('span', { className: 'mono', style: { color: 'var(--faint)', marginLeft: 8, fontSize: 12.5 } }, s.hex)),
          iconBtn('trash', function () { act({ action: 'delete', name: s.name }); }, { danger: true }));
      })));
  }

  // ───────────────────── COLLECTION (admin) ─────────────────────
  // Коллекция (OpenTodo b38e9569): было — плоская простыня из топ-60 моделей, где не видно, что
  // лежит в разделе. Стало — два уровня: список разделов (порядок + переименование + счётчики) →
  // заход внутрь → постраничное редактирование моделей ИМЕННО этого раздела.
  function AdminCollection(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];   // уровень 1: разделы
    var ss = useState(null); var sec = ss[0], setSec = ss[1];     // выбранный раздел (drill-in) | null
    var re = useState({ name: null, val: '' }); var ren = re[0], setRen = re[1];   // переименование раздела
    function loadCats() { API.adminCollection().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { loadCats(); }, []);
    function act(body) { API.adminCollectionEdit(body).then(function (j) { if (j.ok) loadCats(); else app.toast(j.error || 'Ошибка'); }); }
    // Порядок разделов каталога (475b413e): стрелка меняет соседей местами и шлёт ВЕСЬ порядок
    // одним запросом (sort = индекс*10 на сервере) — частичный sort у пары строк прыгал бы
    // мимо разделов с дефолтным sort=100.
    function moveCat(i, dir) {
      var names = (data.categories || []).map(function (c) { return c.name; });
      var j = i + (dir === 'up' ? -1 : 1);
      if (j < 0 || j >= names.length) return;
      var t = names[i]; names[i] = names[j]; names[j] = t;
      act({ action: 'cats_order', names: names });
    }
    if (!data) return loading('Коллекция');
    // Уровень 2 — заход внутрь раздела: модели ЭТОГО раздела, постранично.
    if (sec !== null) {
      return h(CollectionSection, { app: app, section: sec, cats: data.categories || [],
        onBack: function () { setSec(null); loadCats(); } });
    }
    // Уровень 1 — список разделов.
    var cats = data.categories || [];
    function catRow(c, i) {
      var renaming = ren.name === c.name;
      return h('div', { key: c.name, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 11px', marginTop: 7 } },
        renaming
          ? h('div', { style: { flex: 1, display: 'flex', gap: 6 } },
            inp({ value: ren.val, autoFocus: true, onChange: function (e) { setRen({ name: c.name, val: e.target.value }); } }),
            iconBtn('check', function () { var nn = (ren.val || '').trim(); setRen({ name: null, val: '' }); if (nn && nn !== c.name) act({ action: 'rename_category', old: c.name, new: nn }); }, { on: true }),
            iconBtn('close', function () { setRen({ name: null, val: '' }); }))
          : h('button', { style: { flex: 1, minWidth: 0, textAlign: 'left', background: 'none', border: 0, padding: 0, cursor: 'pointer', color: 'inherit' },
              onClick: function () { setSec(c.name); } },
            h('b', { style: { fontSize: 14 } }, c.name),
            h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } }, c.count + ' моделей · открыть →')),
        !renaming && h('div', { style: { display: 'flex', flexDirection: 'column', gap: 5 } },
          h('button', { className: 'qmove', disabled: i === 0, onClick: function () { moveCat(i, 'up'); } }, icon('chev', { size: 16, style: { transform: 'rotate(-90deg)' } })),
          h('button', { className: 'qmove', disabled: i === cats.length - 1, onClick: function () { moveCat(i, 'down'); } }, icon('chev', { size: 16, style: { transform: 'rotate(90deg)' } }))),
        !renaming && iconBtn('pencil', function () { setRen({ name: c.name, val: c.name }); }, { title: 'Переименовать раздел' }),
        !renaming && iconBtn('trash', function () { if (confirm('Удалить раздел «' + c.name + '»?\n\nМодели не удалятся — станут «вне каталога».')) act({ action: 'delete_category', name: c.name }); }, { danger: true, title: 'Удалить раздел' }));
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(loadCats) }, 'Коллекция'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Разделы каталога «Модели». Стрелки — порядок, ✎ — переименовать, тап по разделу — открыть модели внутри.'),
      cats.length === 0 && (data.uncat_count || 0) === 0 && h('div', { className: 'empty' }, icon('globe', { size: 46 }), h('div', null, 'Коллекция пуста.')),
      cats.map(catRow),
      // Псевдо-раздел «вне каталога»: модели без раздела (видны только в поиске/«Мои»).
      (data.uncat_count || 0) > 0 && h('button', { key: '__none__', className: 'order', style: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 11px', marginTop: 7, width: '100%', textAlign: 'left', border: '1px dashed var(--line-2)', background: 'none', cursor: 'pointer', color: 'inherit' },
        onClick: function () { setSec('__none__'); } },
        h('div', { style: { flex: 1, minWidth: 0 } },
          h('b', { style: { fontSize: 14 } }, 'Вне каталога'),
          h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } }, data.uncat_count + ' моделей · открыть →'))),
      h('p', { className: 'hint', style: { marginTop: 12 } }, 'Всего моделей в коллекции: ' + (data.total || 0) + '.'));
  }

  // Уровень 2 коллекции — модели одного раздела, постранично (разделы из ВК бывают на сотни).
  function CollectionSection(props) {
    var app = props.app, section = props.section, cats = props.cats;
    var is = useState([]); var items = is[0], setItems = is[1];
    var ms = useState(false); var more = ms[0], setMore = ms[1];
    var ts = useState(0); var total = ts[0], setTotal = ts[1];
    var ls = useState(true); var busy = ls[0], setBusy = ls[1];
    var es = useState({ id: null, label: '' }); var edit = es[0], setEdit = es[1];
    var offRef = useRef(0);
    var title = section === '__none__' ? 'Вне каталога' : section;
    function loadPage(replace) {
      var offset = replace ? 0 : offRef.current;
      setBusy(true);
      API.adminCollection({ category: section, offset: offset }).then(function (j) {
        setBusy(false);
        if (!j.ok) { app.toast(j.error || 'Ошибка'); return; }
        var list = j.items || [];
        offRef.current = offset + list.length;
        setItems(function (prev) { return replace ? list : prev.concat(list); });
        setMore(!!j.has_more); setTotal(j.total || 0);
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    useEffect(function () { offRef.current = 0; setItems([]); loadPage(true); }, [section]);
    function act(body) { API.adminCollectionEdit(body).then(function (j) { if (j.ok) loadPage(true); else app.toast(j.error || 'Ошибка'); }); }
    function uploadPhoto(cid, file) {
      if (!file) return;
      if (file.size > 12 * 1024 * 1024) { app.toast('Фото больше 12 МБ — выберите поменьше'); return; }
      var rd = new FileReader();
      rd.onload = function () {
        app.toast('Загружаю фото…');
        API.adminPhoto('collection', cid, rd.result).then(function (j) {
          app.toast(j.ok ? 'Фото обновлено' : (j.error || 'Не удалось загрузить'));
          if (j.ok) loadPage(true);
        }).catch(function () { app.toast('Сеть недоступна'); });
      };
      rd.readAsDataURL(file);
    }
    function photoInput(cid) {
      return h('input', { type: 'file', accept: 'image/*', style: { display: 'none' },
        onChange: function (e) { if (e.target.files[0]) uploadPhoto(cid, e.target.files[0]); e.target.value = ''; } });
    }
    var backBtn = h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: props.onBack }, icon('chev', { size: 14, style: { transform: 'rotate(180deg)' } }), ' Разделы');
    return h('div', { className: 'screen' },
      h(Lab, { right: h('span', { style: { display: 'inline-flex', gap: 6 } }, backBtn, refresh(function () { loadPage(true); })) }, title),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Моделей в разделе: ' + total + '. Меняйте раздел у модели, чтобы перенести её в другой.'),
      items.length === 0 && !busy && h('div', { className: 'empty' }, icon('box', { size: 46 }), h('div', null, 'В разделе пусто.')),
      items.map(function (it) {
        return h('div', { key: it.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 12, padding: 11 } },
          h('label', { className: 'pthumb', title: it.has_photo ? 'Заменить фото печати' : 'Добавить фото печати' },
            it.photo ? h('img', { src: it.photo, alt: '' }) : h('div', { className: 'ph' }, icon('box', { size: 20 })),
            h('span', { className: 'edit' }, icon('pencil', { size: 11 })),
            photoInput(it.id)),
          edit.id === it.id
            ? h('div', { style: { flex: 1, display: 'flex', gap: 6 } },
              inp({ value: edit.label, onChange: function (e) { setEdit({ id: it.id, label: e.target.value }); } }),
              iconBtn('check', function () { act({ action: 'rename', id: it.id, label: edit.label }); setEdit({ id: null, label: '' }); }, { on: true }))
            : h('div', { style: { flex: 1, minWidth: 0 } }, h('b', { style: { fontSize: 14 } }, it.label),
              h('div', { style: { fontSize: 12, color: 'var(--faint)', marginTop: 2 } }, 'расчётов: ' + it.uses),
              h('select', { className: 'input', value: it.category || '', style: { marginTop: 5, padding: '6px 9px', fontSize: 12.5, width: 'auto', maxWidth: '100%' },
                onClick: function (e) { e.stopPropagation(); },
                onChange: function (e) { act({ action: 'set_category', id: it.id, category: e.target.value }); } },
                h('option', { value: '' }, '— вне каталога'),
                cats.map(function (c) { return h('option', { key: c.name, value: c.name }, c.name); }))),
          edit.id !== it.id && h('label', { key: 'photo', className: 'iconlabel', title: 'Фото печати' }, icon('image', { size: 16 }), photoInput(it.id)),
          edit.id !== it.id && iconBtn('pencil', function () { setEdit({ id: it.id, label: it.label }); }),
          edit.id !== it.id && iconBtn('trash', function () { if (confirm('Удалить «' + it.label + '» из коллекции?')) act({ action: 'delete', id: it.id }); }, { danger: true }));
      }),
      more && h('button', { className: 'btn soft', style: { marginTop: 12 }, disabled: busy, onClick: function () { loadPage(false); } },
        busy ? 'Загрузка…' : ('Показать ещё (' + Math.max(0, total - items.length) + ')')));
  }

  // ───────────────────── PRINTERS (admin) ─────────────────────
  function AdminPrinters(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var fs = useState({ name: '', machine: '', process: '', filament: '' }); var nf = fs[0], setNf = fs[1];
    var os = useState(false); var open = os[0], setOpen = os[1];
    function load() { API.adminPrinters().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function act(body) { return API.adminPrinter(body).then(function (j) { if (j.ok) load(); else app.toast(j.error || 'Ошибка'); return j; }); }
    if (!data) return loading('Принтеры');
    function row(label, value) {
      return h('div', { className: 'mono', style: { fontSize: 11.5, color: 'var(--muted)', marginTop: 4, wordBreak: 'break-all' } },
        h('span', { style: { color: 'var(--faint)' } }, label + ': '), value || '—');
    }
    function add() {
      act({ action: 'add', name: nf.name, machine: nf.machine, process: nf.process, filament: nf.filament }).then(function (j) {
        if (j && j.ok) { setNf({ name: '', machine: '', process: '', filament: '' }); setOpen(false); }
      });
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Принтеры'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, '⭐ — основной (по умолчанию для расчётов), 🚫 — выключен.'),
      (data.printers || []).length === 0 && h('div', { className: 'empty' }, icon('printer', { size: 46 }), h('div', null, 'Принтеров пока нет.')),
      (data.printers || []).map(function (p) {
        return h('div', { key: p.id, className: 'card', style: { marginTop: 10, padding: 13, opacity: p.is_active ? 1 : 0.55 } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
            h('b', { style: { fontSize: 14.5, flex: 1, minWidth: 0 } }, p.name,
              p.is_default && h('span', { className: 'stbadge st-printed', style: { marginTop: 0, padding: '2px 7px', fontSize: 10.5, marginLeft: 7 } }, '★')),
            iconBtn('star', function () { act({ action: 'default', id: p.id }); }, { on: p.is_default, title: 'Сделать основным' }),
            iconBtn(p.is_active ? 'check' : 'close', function () { act({ action: 'toggle', id: p.id }); }, { title: 'Вкл/выкл' }),
            iconBtn('trash', function () { if (confirm('Удалить принтер «' + p.name + '»?')) act({ action: 'delete', id: p.id }); }, { danger: true })),
          row('machine', p.machine), row('process', p.process), row('filament', p.filament));
      }),
      h('div', { style: { marginTop: 12 } }, addHeader(open, setOpen, 'Добавить принтер')),
      open && h('div', { className: 'card', style: { marginTop: 10 } },
        field('Название', inp({ value: nf.name, placeholder: 'Elegoo Centauri Carbon 2', onChange: function (e) { setNf(Object.assign({}, nf, { name: e.target.value })); } })),
        field('Профиль принтера (machine .json)', inp({ className: 'input mono', value: nf.machine, placeholder: 'C:\\…\\machine.json', onChange: function (e) { setNf(Object.assign({}, nf, { machine: e.target.value })); } })),
        field('Папка профилей процесса', inp({ className: 'input mono', value: nf.process, placeholder: 'C:\\…\\process', onChange: function (e) { setNf(Object.assign({}, nf, { process: e.target.value })); } })),
        field('Папка профилей филамента', inp({ className: 'input mono', value: nf.filament, placeholder: 'C:\\…\\filament', onChange: function (e) { setNf(Object.assign({}, nf, { filament: e.target.value })); } })),
        h('button', { className: 'btn soft', style: { marginTop: 4 }, onClick: add }, icon('plus', { size: 17 }), ' Добавить')));
  }

  function ago(ts) {
    var s = Math.max(0, Math.floor(Date.now() / 1000) - (ts || 0));
    if (s < 60) return 'только что';
    if (s < 3600) return Math.floor(s / 60) + ' мин назад';
    if (s < 86400) return Math.floor(s / 3600) + ' ч назад';
    return Math.floor(s / 86400) + ' дн назад';
  }

  // ───────────────────── ACTIVITY LOG (OpenTodo f0602c66) ─────────────────────
  function AdminActivity() {
    var ds = useState(null); var data = ds[0], setData = ds[1];
    // фильтр по типу действия (OpenTodo 4c6edb76): чипы из реально встретившихся действий
    var fs = useState(''); var flt = fs[0], setFlt = fs[1];
    function load() { API.adminActivity().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    if (!data) return loading('Активность');
    var all = data.events || [];
    var actions = [];
    all.forEach(function (e) { if (e.action && actions.indexOf(e.action) < 0) actions.push(e.action); });
    var evs = flt ? all.filter(function (e) { return e.action === flt; }) : all;
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Активность'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Последние действия пользователей — только просмотр, без уведомлений.'),
      actions.length > 1 && h('div', { className: 'chips', style: { marginBottom: 11 } },
        h('button', { className: 'chip' + (flt ? '' : ' on'), type: 'button', onClick: function () { setFlt(''); } }, 'Все · ' + all.length),
        actions.map(function (a) {
          var n = all.filter(function (e) { return e.action === a; }).length;
          return h('button', { key: a, className: 'chip' + (flt === a ? ' on' : ''), type: 'button',
            onClick: function () { setFlt(flt === a ? '' : a); } }, a + ' · ' + n);
        })),
      evs.length === 0 && h('div', { className: 'empty' }, icon('clock', { size: 44 }), h('div', null, 'Пока пусто.')),
      evs.map(function (e) {
        return h('div', { key: e.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 11, padding: '11px 13px' } },
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontSize: 14 } }, h('b', null, e.name || ('id ' + e.user_id)), e.admin ? ' 👑' : null, h('span', { style: { color: 'var(--muted)', marginLeft: 6 } }, e.action)),
            e.detail && h('div', { className: 'mono', style: { fontSize: 12, color: 'var(--faint)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, e.detail)),
          h('span', { style: { fontSize: 11.5, color: 'var(--faint)', whiteSpace: 'nowrap', flex: '0 0 auto' } }, ago(e.at)));
      }));
  }

  // ───────────────────── BIG DATA (OpenTodo 7507fba0) ─────────────────────
  function AdminBigData() {
    var ds = useState(null); var data = ds[0], setData = ds[1];
    function load() { API.adminBigData().then(function (j) { if (j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    if (!data) return loading('Big Data');
    var s = data.samples || {}, t = data.totals || {}, col = data.collection || {};
    function kv(label, val, accent) {
      return h('div', null,
        h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600 } }, label),
        h('div', { style: { fontSize: 20, fontWeight: 700, marginTop: 2, color: accent ? 'var(--price)' : 'var(--text)' } }, val));
    }
    function grid(children) { return h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } }, children); }
    function bars(items, key, fmt) {
      var max = (items || []).reduce(function (a, x) { return Math.max(a, x[key] || 0); }, 0) || 1;
      return (items || []).map(function (x, i) {
        return h('div', { key: i, style: { margin: '7px 0' } },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 3, gap: 8 } },
            h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, x.material || x.label),
            h('b', { className: 'mono', style: { flex: '0 0 auto' } }, fmt(x))),
          h('div', { className: 'pbar', style: { height: 6 } }, h('i', { style: { width: Math.round((x[key] || 0) / max * 100) + '%' } })));
      });
    }
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Big Data'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Данные, которые бот копит для своих алгоритмов: обучающая выборка модели предсказания времени, расход материалов и переиспользование моделей.'),

      h(Lab, null, 'Модель предсказания времени'),
      h('div', { className: 'card', style: { padding: 14 } },
        grid([kv('Образцов (оценка→факт)', s.count || 0, false),
          kv('Попадание ±20%', (s.within20_pct || 0) + '%', true),
          kv('Средняя ошибка', (s.mae_min || 0) + ' мин', false),
          kv('Отн. ошибка (MAPE)', (s.mape_pct || 0) + '%', false)]),
        s.count > 0
          ? h('p', { className: 'hint', style: { marginTop: 10 } }, 'Средняя оценка слайсера ' + s.avg_est + ' мин против факта ' + s.avg_actual + ' мин.')
          : h('p', { className: 'hint', style: { marginTop: 10 } }, 'Пока нет замеров. Отмечайте «Факт. печать (мин)» в заказах — образцы копятся сюда.')),

      (data.samples_by_material || []).length > 0 && h(Lab, null, 'Точность по материалам'),
      (data.samples_by_material || []).length > 0 && h('div', { className: 'card', style: { padding: 14 } },
        bars(data.samples_by_material, 'count', function (x) { return x.count + ' шт · ±' + x.mae_min + ' мин'; })),

      (data.materials || []).length > 0 && h(Lab, null, 'Расход материалов (напечатано)'),
      (data.materials || []).length > 0 && h('div', { className: 'card', style: { padding: 14 } },
        bars(data.materials, 'grams', function (x) { return Math.round(x.grams) + ' г · ' + x.orders + ' зак.'; })),

      // Журнал печатей мастера (cacfae14): брак по пластикам — данные для анализа качества
      data.journal && data.journal.total > 0 && h(Lab, null, 'Журнал печатей · брак'),
      data.journal && data.journal.total > 0 && h('div', { className: 'card', style: { padding: 14 } },
        grid([kv('Печатей в журнале', data.journal.total, false),
          kv('Брак', data.journal.fail_pct + '% (' + data.journal.fails + ')', false),
          kv('Пластика учтено', data.journal.grams >= 1000 ? (data.journal.grams / 1000).toFixed(1) + ' кг' : data.journal.grams + ' г', true),
          kv('Времени печати', Math.round((data.journal.minutes || 0) / 60) + ' ч', false)]),
        (data.journal.by_plastic || []).length > 0 && h('div', { style: { marginTop: 8 } },
          bars(data.journal.by_plastic.map(function (x) { return { label: x.plastic, n: x.n, fail_pct: x.fail_pct, grams: x.grams }; }), 'n',
            function (x) { return x.n + ' печ. · брак ' + x.fail_pct + '%'; })),
        h('p', { className: 'hint', style: { marginTop: 8 } }, 'Источник — «Журнал печатей» в админке: вносите все печати, и здесь появится честная статистика брака по пластикам.')),

      data.inventory && h(Lab, null, 'Склад по катушкам'),
      data.inventory && h('div', { className: 'card', style: { padding: 14 } },
        grid([kv('Активных катушек', data.inventory.active_spools || 0, false),
          kv('Остаток филамента', data.inventory.remaining_g >= 1000 ? (data.inventory.remaining_g / 1000).toFixed(1) + ' кг' : Math.round(data.inventory.remaining_g || 0) + ' г', true),
          kv('С закупочной ценой', data.inventory.priced_spools || 0, false),
          kv('Цветов на исходе / нет', (data.inventory.low_colors || 0) + ' / ' + (data.inventory.out_colors || 0), false)])),

      data.cogs && h(Lab, null, 'Себестоимость по факту'),
      data.cogs && h('div', { className: 'card', style: { padding: 14 } },
        grid([kv('Заказов с себест.', (data.cogs.orders_with_cogs || 0) + ' / ' + (data.cogs.printed_orders || 0), false),
          kv('Покрытие', (data.cogs.coverage_pct || 0) + '%', true),
          kv('Выручка', Math.round(data.cogs.revenue || 0), false),
          kv('Маржа', Math.round(data.cogs.margin || 0) + ' (' + (data.cogs.margin_pct || 0) + '%)', true)]),
        h('p', { className: 'hint', style: { marginTop: 8 } }, 'Считается по катушкам с закупочной ценой при «напечатано». Вносите цену при приходе катушки — покрытие вырастет.')),

      h(Lab, null, 'Коллекция моделей'),
      h('div', { className: 'card', style: { padding: 14 } },
        grid([kv('Моделей в коллекции', col.models || 0, false), kv('Расчётов по ним', col.total_uses || 0, false)]),
        (data.top_models || []).length > 0 && h('div', { style: { marginTop: 10 } }, bars(data.top_models, 'uses', function (x) { return x.uses + '×'; }))),

      h(Lab, null, 'Покрытие данными'),
      h('div', { className: 'card', style: { padding: 14 } },
        grid([kv('Напечатано заказов', t.printed_orders || 0, false),
          kv('С замером времени', (t.coverage_pct || 0) + '%', true),
          kv('Сохранённых моделей', t.saved_models || 0, false),
          kv('Образцов времени', s.count || 0, false)])));
  }

  function loading(title) { return h('div', { className: 'screen' }, h(Lab, null, title), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…')); }
  function refresh(fn) { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: fn }, icon('refresh', { size: 14 }), ' Обновить'); }

  Object.assign(window, { AdminPlastics: AdminPlastics, AdminPromos: AdminPromos, AdminSettings: AdminSettings,
    AdminPriceCalc: AdminPriceCalc,
    AdminDiscounts: AdminDiscounts, AdminShades: AdminShades, AdminCollectionAdmin: AdminCollection,
    AdminPrinters: AdminPrinters, AdminActivity: AdminActivity, AdminBigData: AdminBigData,
    // единый список операционных настроек и подписи тумблеров — для экрана «Настройки» (screens_admin4.js)
    PS_OperationalGroups: OPERATIONAL_GROUPS, PS_BoolSettings: BOOL_SETTINGS });
})();
