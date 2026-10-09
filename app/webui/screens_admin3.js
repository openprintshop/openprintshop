// screens_admin3.js — CRM: клиенты (список + карточка), задачи-напоминания, рассылки
// (crm-roadmap.md, OpenTodo a8b591cf). Паттерны — как в screens_admin2.js.
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, Lab = window.PS_Lab, API = window.API;

  function money(n, cur) { return (Number(n) || 0).toFixed(2).replace('.', ',') + ' ' + (cur || 'руб.'); }
  function field(label, node) { return h('label', { style: { display: 'block', marginBottom: 9 } }, h('div', { style: { fontSize: 11.5, color: 'var(--faint)', margin: '0 2px 5px', fontWeight: 600 } }, label), node); }
  function inp(props) { return h('input', Object.assign({ className: 'input' }, props)); }
  function loading(title) { return h('div', { className: 'screen' }, h(Lab, null, title), h('p', { className: 'hint', style: { marginTop: 0 } }, 'Загружаю…')); }
  function refresh(fn) { return h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: fn }, icon('refresh', { size: 14 }), ' Обновить'); }
  function iconBtn(ic, onClick, opts) {
    opts = opts || {};
    return h('button', { onClick: onClick, title: opts.title, style: { width: 32, height: 32, borderRadius: 9, border: '1px solid var(--line-2)', background: opts.on ? 'var(--accent)' : 'var(--surface-2)', color: opts.on ? '#1a0d02' : (opts.danger ? 'var(--red)' : 'var(--muted)'), cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto' } }, icon(ic, { size: 16 }));
  }

  // Цвета вычисляемых сегментов (этап 3): VIP — фирменный зелёный, спящий — янтарный.
  var SEG_COLOR = { new: '#7a8a99', repeat: '#3a86ff', vip: '#2dd47e', sleeping: '#ffb020' };

  // Аватарка клиента (4f0aa54f): сгенерированная ботом, иначе кружок с первой буквой имени.
  function avatar(c, size) {
    size = size || 38;
    var common = { width: size, height: size, borderRadius: '50%', flex: '0 0 auto' };
    if (c.avatar) return h('img', { src: c.avatar, alt: '', style: Object.assign({ objectFit: 'cover', background: 'var(--surface-2)' }, common) });
    var letter = ((c.name || '?').replace(/^@/, '')[0] || '?').toUpperCase();
    return h('span', { style: Object.assign({ display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--surface-2)', border: '1px solid var(--line-2)', color: 'var(--muted)',
      fontWeight: 700, fontSize: Math.round(size * 0.42) }, common) }, letter);
  }
  function segBadge(key, labels) {
    return h('span', { key: key, style: { fontSize: 10.5, fontWeight: 700, padding: '2px 7px', borderRadius: 999, flex: '0 0 auto', color: '#0c1016', background: SEG_COLOR[key] || '#7a8a99' } }, (labels || {})[key] || key);
  }

  // Текст win-back по умолчанию (этап 3): мастер правит перед отправкой.
  var WINBACK_TEXT = 'Давно вас не видели в Print Studio! 🖨 Если хочется напечатать что-то новое — присылайте модель, посчитаю сразу. Для вас могу сделать персональную скидку.';

  // ───────────────────── РАССЫЛКА ПО СЕГМЕНТУ (этап 5) ─────────────────────
  function BroadcastForm(props) {
    var app = props.app, segments = props.segments || {};
    var ss = useState(props.segment || ''); var seg = ss[0], setSeg = ss[1];
    var ts = useState(''); var text = ts[0], setText = ts[1];
    var ps = useState(null); var preview = ps[0], setPreview = ps[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    // конструктор контента (6fbed3ad): случайная модель из каталога + блок «что нового»
    var rmS = useState(false); var rndModel = rmS[0], setRndModel = rmS[1];
    var wnS = useState(false); var wnOn = wnS[0], setWnOn = wnS[1];
    var wtS = useState(''); var wnText = wtS[0], setWnText = wtS[1];
    function opts() {
      return { random_model: rndModel, whats_new: wnOn, whats_new_text: wnOn ? wnText : '' };
    }
    function hasContent() { return !!(text.trim() || rndModel || (wnOn && wnText.trim())); }
    function scope() {
      setBusy(true);
      API.adminBroadcast(seg, '', true, opts()).then(function (j) {
        setBusy(false);
        if (j.ok) setPreview(j); else app.toast(j.error || 'Ошибка');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function send() {
      if (!hasContent()) { app.toast('Напишите текст или включите контент-блоки'); return; }
      var n = preview ? preview.eligible : '?';
      if (!window.confirm('Отправить рассылку ' + n + ' клиентам сегмента «' + (segments[seg] || 'Все') + '»?')) return;
      setBusy(true);
      API.adminBroadcast(seg, text, false, opts()).then(function (j) {
        setBusy(false);
        if (j.ok) { app.toast('Доставлено: ' + j.sent + (j.failed ? (' · не дошло: ' + j.failed) : '')); setText(''); setWnText(''); setPreview(null); props.onDone && props.onDone(); }
        else app.toast(j.error || 'Ошибка');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function checkbox(on, setOn, label) {
      return h('label', { style: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, fontSize: 13.5, cursor: 'pointer' } },
        h('input', { type: 'checkbox', checked: on, onChange: function (e) { setOn(e.target.checked); setPreview(null); } }), label);
    }
    return h('div', { className: 'card', style: { marginTop: 10 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontWeight: 700, marginBottom: 4 } },
        icon('chat', { size: 17, style: { color: 'var(--accent)' } }), 'Рассылка по сегменту'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Сообщение уйдёт клиентам выбранного сегмента. Отписавшиеся и те, кому недавно слали, исключаются автоматически; в каждом сообщении есть кнопка «Отписаться».'),
      field('Сегмент', h('select', { className: 'input', value: seg, onChange: function (e) { setSeg(e.target.value); setPreview(null); } },
        [h('option', { key: '_', value: '' }, 'Все клиенты')].concat(Object.keys(segments).map(function (k) { return h('option', { key: k, value: k }, segments[k]); })))),
      h('textarea', { className: 'input', style: { minHeight: 84, resize: 'vertical' }, placeholder: 'Текст рассылки (например: новый материал, акция месяца…)', value: text, onChange: function (e) { setText(e.target.value); } }),
      // контент-блоки (6fbed3ad)
      checkbox(rndModel, setRndModel, '🎲 Приложить случайную модель из каталога (фото + «от N руб.» + кнопка в каталог)'),
      checkbox(wnOn, setWnOn, '✨ Блок «Что нового в приложении»'),
      wnOn && h('textarea', { className: 'input', style: { minHeight: 60, resize: 'vertical', marginTop: 6 },
        placeholder: 'Что нового: например «Каталог на 3500 моделей, мгновенный расчёт по ссылке…»',
        value: wnText, onChange: function (e) { setWnText(e.target.value); } }),
      preview && h('p', { className: 'hint', style: { marginTop: 8 } },
        'Получат: ', h('b', { style: { color: 'var(--text)' } }, preview.eligible), ' из ' + preview.total +
        ' (отписаны: ' + preview.skipped_optout + ', недавно получали: ' + preview.skipped_recent + ')' +
        (preview.sample && preview.sample.length ? ' — ' + preview.sample.join(', ') + '…' : '') +
        (preview.attach && preview.attach.model ? ' · 🎲 модель: «' + preview.attach.model + '»' : '')),
      h('div', { className: 'grid2', style: { gap: 9, marginTop: 10 } },
        h('button', { className: 'btn line', style: { marginTop: 0 }, disabled: busy, onClick: scope }, 'Показать охват'),
        h('button', { className: 'btn', style: { marginTop: 0 }, disabled: busy || !hasContent(), onClick: send }, icon('share', { size: 16 }), busy ? ' …' : ' Отправить')));
  }

  // ───────────────────── СПИСОК КЛИЕНТОВ (этапы 1–3) ─────────────────────
  // Настройка сегментов CRM (пороги VIP/«спящий»/пауза рассылок) — переехала сюда из «Цен»
  // (IA-редизайн, аудит #7): пороги описывают деление КЛИЕНТОВ, а не прайс-лист.
  function SegmentSettings(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var vs = useState({}); var vals = vs[0], setVals = vs[1];
    useEffect(function () {
      API.adminSettings().then(function (j) {
        if (j && j.ok && j.crm) { setData(j.crm); setVals(Object.assign({}, j.crm.values)); }
      });
    }, []);
    function save(k) {
      API.adminSetting({ key: k, kind: 'num', value: vals[k] }).then(function (j) {
        app.toast(j && j.ok ? 'Сохранено' : ((j && j.error) || 'Ошибка'));
      });
    }
    if (!data) return h('p', { className: 'hint', style: { marginTop: 8 } }, 'Загружаю…');
    return h('div', null, (data.order || []).map(function (k) {
      return h('div', { key: k, className: 'card', style: { marginTop: 8, padding: 12 } },
        h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginBottom: 7 } }, data.labels[k]),
        h('div', { style: { display: 'flex', gap: 8 } },
          inp({ className: 'input mono', inputMode: 'decimal', value: vals[k] != null ? vals[k] : '',
            onChange: function (e) { var n = {}; n[k] = e.target.value; setVals(Object.assign({}, vals, n)); } }),
          h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 16px' }, onClick: function () { save(k); } }, icon('check', { size: 17 }))));
    }));
  }

  function AdminClients(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var qs = useState(''); var q = qs[0], setQ = qs[1];
    var ss = useState(''); var seg = ss[0], setSeg = ss[1];
    var os = useState('last'); var sort = os[0], setSort = os[1];
    var bs = useState(false); var bcast = bs[0], setBcast = bs[1];
    var sgS = useState(false); var segCfg = sgS[0], setSegCfg = sgS[1];   // «Настройка сегментов»
    function load() {
      API.adminClients({ q: q, segment: seg, sort: sort }).then(function (j) { if (j.ok) setData(j); else app.toast(j.error || 'Ошибка'); });
    }
    useEffect(function () {                       // дебаунс поиска + мгновенно на фильтрах
      var t = setTimeout(load, q ? 320 : 0);
      return function () { clearTimeout(t); };
    }, [q, seg, sort]);
    function csv() {
      var url = API.adminClientsCsvUrl();
      if (!url) return;
      var tg = window.Telegram && window.Telegram.WebApp;
      if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false }); else window.open(url, '_blank');
    }
    var labels = (data && data.segments) || {};
    var counts = (data && data.segment_counts) || {};
    var items = (data && data.clients) || [];
    var SORTS = [['last', 'Свежие'], ['ltv', 'LTV'], ['orders', 'Заказы']];
    return h('div', { className: 'screen' },
      h(Lab, { right: h('span', { style: { display: 'inline-flex', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' } },
        h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: function () { app.go('adminTasks'); } }, '📌 Задачи'),
        h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5, color: segCfg ? 'var(--accent)' : '' }, onClick: function () { setSegCfg(!segCfg); } }, '⚙ Сегменты'),
        h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5, color: bcast ? 'var(--accent)' : '' }, onClick: function () { setBcast(!bcast); } }, icon('chat', { size: 14 }), ' Рассылка'),
        h('button', { className: 'chip', style: { padding: '6px 11px', fontSize: 12.5 }, onClick: csv }, icon('doc', { size: 14 }), ' CSV'),
        refresh(load)) }, 'Клиенты'),
      segCfg && h(SegmentSettings, { app: app }),
      bcast && h(BroadcastForm, { app: app, segments: labels, segment: seg, onDone: load }),
      h('input', { className: 'input', style: { marginTop: bcast ? 10 : 0 }, placeholder: '🔎 Имя, @ник или Telegram ID', value: q, onChange: function (e) { setQ(e.target.value); } }),
      h('div', { className: 'chips', style: { marginTop: 9 } },
        h('button', { className: 'chip' + (seg ? '' : ' on'), type: 'button', onClick: function () { setSeg(''); } }, 'Все · ' + (data ? data.total : '—')),
        Object.keys(labels).map(function (k) {
          return h('button', { key: k, className: 'chip' + (seg === k ? ' on' : ''), type: 'button', onClick: function () { setSeg(seg === k ? '' : k); } },
            labels[k] + ' · ' + (counts[k] || 0));
        })),
      h('div', { className: 'chips', style: { marginTop: 7 } }, SORTS.map(function (s) {
        return h('button', { key: s[0], className: 'chip' + (sort === s[0] ? ' on' : ''), type: 'button', style: { padding: '6px 11px', fontSize: 12 }, onClick: function () { setSort(s[0]); } }, s[1]);
      })),
      data === null && h('p', { className: 'hint' }, 'Загружаю…'),
      data !== null && items.length === 0 && h('p', { className: 'hint' }, q || seg ? 'Никого не нашлось.' : 'Клиентов пока нет — появятся с первым заказом.'),
      items.map(function (c) {
        return h('div', { key: c.uid, className: 'order', style: { cursor: 'pointer', display: 'flex', gap: 11, alignItems: 'center' }, onClick: function () { app.openAdminClient(c.uid); } },
          avatar(c, 40),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { className: 'spread' },
              h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, minWidth: 0, flexWrap: 'wrap' } },
                h('b', { style: { fontSize: 14.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, c.name),
                c.segments.map(function (s) { return segBadge(s, labels); }),
                c.no_broadcast ? h('span', { title: 'Отписан от рассылок', style: { fontSize: 11 } }, '🔕') : null),
              h('span', { className: 'mono', style: { fontWeight: 700, color: 'var(--price)', flex: '0 0 auto' } }, money(c.spent, data.currency))),
            h('div', { style: { marginTop: 6, display: 'flex', gap: 9, flexWrap: 'wrap', fontSize: 12.5, color: 'var(--muted)' } },
              h('span', null, c.orders + ' зак. · ' + c.printed + ' напеч.'),
              h('span', null, c.last_ago ? ('был ' + c.last_ago) : 'заказов не было'),
              c.discount_pct != null && h('span', { style: { color: 'var(--accent)' } }, '−' + c.discount_pct + '%'))));
      }));
  }

  // ───────────────────── КАРТОЧКА КЛИЕНТА (этап 1 + win-back этап 3) ─────────────────────
  function AdminClientCard(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var fs = useState(null); var f = fs[0], setF = fs[1];               // ручные поля
    var ms = useState(''); var msg = ms[0], setMsg = ms[1];             // сообщение клиенту
    var pcs = useState(''); var pct = pcs[0], setPct = pcs[1];          // скидка
    var nts = useState({ title: '', due: '' }); var nt = nts[0], setNt = nts[1];   // новая задача
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    var bas = useState(''); var bAmt = bas[0], setBAmt = bas[1];        // баланс: сумма (ccb1e4b6)
    var bcs = useState(''); var bCom = bcs[0], setBCom = bcs[1];        // баланс: комментарий
    var tes = useState(null); var txEdit = tes[0], setTxEdit = tes[1];  // {id, text} — правка заметки транзакции
    function load() {
      API.adminClient(app.adminClientId).then(function (j) {
        if (!j.ok) { app.toast(j.error || 'Ошибка'); return; }
        setData(j);
        setF({ name: j.client.name === ('id ' + j.client.uid) ? '' : j.client.name, phone: j.client.phone || '', note: j.client.note || '' });
        setPct(j.client.discount_pct != null ? String(j.client.discount_pct) : '');
      });
    }
    useEffect(function () { load(); }, [app.adminClientId]);
    if (!data || !f) return loading('Клиент');
    var c = data.client, cur = data.currency, labels = data.segments || {};
    function saveFields() {
      setBusy(true);
      API.adminClientSave(c.uid, f).then(function (j) { setBusy(false); app.toast(j.ok ? 'Сохранено' : (j.error || 'Ошибка')); if (j.ok) load(); })
        .catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function saveDiscount() {
      var v = parseFloat(String(pct).replace(',', '.')) || 0;
      API.adminClientDiscount(c.uid, v, f.name || c.tg_name).then(function (j) {
        app.toast(j.ok ? (j.pct != null ? ('Скидка −' + j.pct + '%') : 'Скидка снята') : (j.error || 'Ошибка'));
        if (j.ok) load();
      });
    }
    function sendMsg() {
      var t = msg.trim();
      if (!t) { app.toast('Напишите сообщение'); return; }
      setBusy(true);
      API.adminClientMessage(c.uid, t).then(function (j) {
        setBusy(false);
        if (j.ok) { app.toast('Отправлено клиенту'); setMsg(''); } else app.toast(j.error || 'Не доставлено');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function addTask() {
      if (!nt.title.trim()) { app.toast('Что напомнить?'); return; }
      API.adminTask({ action: 'add', title: nt.title, due: nt.due, uid: c.uid }).then(function (j) {
        if (j.ok) { app.toast('Задача добавлена'); setNt({ title: '', due: '' }); load(); } else app.toast(j.error || 'Ошибка');
      });
    }
    function taskDone(tid) { API.adminTask({ action: 'done', id: tid }).then(function (j) { if (j.ok) load(); }); }
    // Баланс: зачислить/списать с комментарием + аннотация прошлых транзакций (ccb1e4b6)
    function balAdjust(sign) {
      var v = Math.abs(parseFloat(String(bAmt).replace(',', '.')) || 0);
      if (!v) { app.toast('Введите сумму'); return; }
      setBusy(true);
      API.adminBalance(c.uid, sign * v, sign > 0 ? 'Пополнение мастером' : 'Списание мастером', bCom.trim())
        .then(function (j) {
          setBusy(false);
          if (j && j.ok) { app.toast('Баланс: ' + money(j.balance, cur)); setBAmt(''); setBCom(''); load(); }
          else app.toast((j && j.error) || 'Ошибка');
        }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function saveTxComment() {
      API.adminBalanceComment(txEdit.id, txEdit.text.trim()).then(function (j) {
        if (j && j.ok) { app.toast(txEdit.text.trim() ? 'Комментарий сохранён' : 'Комментарий удалён'); setTxEdit(null); load(); }
        else app.toast((j && j.error) || 'Ошибка');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function kv(label, val, accent) {
      return h('div', null,
        h('div', { style: { fontSize: 11, color: 'var(--faint)', fontWeight: 600 } }, label),
        h('div', { style: { fontSize: 17, fontWeight: 700, marginTop: 2, color: accent ? 'var(--price)' : 'var(--text)' } }, val));
    }
    var sleeping = (c.segments || []).indexOf('sleeping') >= 0;
    return h('div', { className: 'screen' },
      h('div', { className: 'card' },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 11 } },
          avatar(c, 48),
          h('div', { style: { minWidth: 0, flex: 1 } },
            h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' } },
              h('b', { style: { fontSize: 17, minWidth: 0 } }, c.name),
              (c.segments || []).map(function (s) { return segBadge(s, labels); }),
              c.no_broadcast ? h('span', { title: 'Отписан от рассылок', style: { fontSize: 12 } }, '🔕') : null),
            h('div', { className: 'mono', style: { fontSize: 12, color: 'var(--faint)', marginTop: 3 } }, 'id ' + c.uid + (c.tg_name && c.tg_name !== c.name ? ' · ' + c.tg_name : '')))),
        h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 11, marginTop: 13 } },
          kv('Заказов', c.orders), kv('Напечатано', c.printed), kv('LTV', money(c.spent, cur), true),
          kv('Средний чек', money(c.avg_check, cur)), kv('Баланс', money(c.balance, cur), Number(c.balance) > 0), kv('Рефералов', c.referrals)),
        // семейный счёт (2026-07-16): KPI «Баланс» выше — уже общий; здесь состав семьи
        c.family && h('div', { style: { marginTop: 10, padding: '8px 11px', borderRadius: 10, background: 'var(--surface-2)', fontSize: 12.5, color: 'var(--muted)' } },
          '👨‍👩‍👧 Семейный счёт #' + c.family.id + ': ',
          h('b', { style: { color: 'var(--text)' } }, (c.family.members || []).map(function (m) { return m.user_name || ('id ' + m.user_id); }).join(', '))),
        h('div', { style: { marginTop: 11, fontSize: 12.5, color: 'var(--muted)' } },
          'Первый заказ: ' + (c.first_ago || '—') + ' · Последний: ' + (c.last_ago || '—'),
          c.fav_material ? h('div', { style: { marginTop: 3 } }, 'Любимый материал: ', h('b', { style: { color: 'var(--text)' } }, c.fav_material)) : null)),

      // Живая корзина клиента (a327c3ec): что он собрал, но пока не заказал — мастер видит
      // интерес и может подтолкнуть сообщением ниже. Оформленная корзина отсюда исчезает.
      c.cart && c.cart.count > 0 && h(React.Fragment, null,
        h(Lab, null, '🛒 Корзина (не оформлена)'),
        h('div', { className: 'card', style: { padding: '11px 13px' } },
          (c.cart.items || []).map(function (it, i) {
            return h('div', { key: i, className: 'spread', style: { padding: '5px 0', borderTop: i ? '1px solid var(--line)' : 'none' } },
              h('span', { style: { fontSize: 13.5, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, it.label || '—'),
              h('span', { className: 'mono', style: { fontSize: 12.5, color: 'var(--price)', flex: '0 0 auto', marginLeft: 8 } }, it.totalStr || ''));
          }),
          h('div', { className: 'spread', style: { marginTop: 7, fontSize: 12.5, color: 'var(--muted)' } },
            h('span', null, 'обновлена ' + (c.cart.updated_label || '')),
            c.cart.total && h('b', { className: 'mono', style: { color: 'var(--text)' } }, c.cart.total)))),

      // Заказы клиента — сразу после шапки (аудит UX): мастер чаще всего открывает карточку,
      // чтобы вспомнить историю заказов, а не чтобы редактировать CRM-поля
      h(Lab, null, 'Последние заказы'),
      (c.recent || []).length === 0 && h('p', { className: 'hint' }, 'Заказов ещё нет.'),
      (c.recent || []).map(function (o) {
        return h('div', { key: o.id, className: 'order', style: { cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px' }, onClick: function () { app.openAdminOrder(o.id); } },
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, '#' + o.id + ' · ' + o.label),
            h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } }, o.status_label + ' · ' + (o.ago || ''))),
          h('span', { className: 'mono', style: { fontWeight: 600, color: 'var(--price)', flex: '0 0 auto' } }, money(o.price, cur)));
      }),

      h(Lab, null, 'Написать клиенту'),
      sleeping && h('button', { className: 'btn soft', style: { marginTop: 0, marginBottom: 9 }, onClick: function () { setMsg(WINBACK_TEXT); } },
        '🌙 Напомнить о себе (win-back)'),
      h('div', { className: 'card', style: { padding: 12 } },
        h('textarea', { className: 'input', style: { minHeight: 72, resize: 'vertical' }, placeholder: 'Сообщение уйдёт клиенту в чат с ботом…', value: msg, onChange: function (e) { setMsg(e.target.value); } }),
        h('button', { className: 'btn', style: { marginTop: 9 }, disabled: busy || !msg.trim(), onClick: sendMsg }, icon('share', { size: 16 }), ' Отправить')),

      h(Lab, null, 'Скидка'),
      h('div', { style: { display: 'flex', gap: 8, alignItems: 'center' } },
        h('input', { className: 'input mono', style: { flex: '0 0 110px' }, inputMode: 'decimal', placeholder: '%', value: pct, onChange: function (e) { setPct(e.target.value); } }),
        h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 16px' }, onClick: saveDiscount }, 'Сохранить'),
        h('span', { className: 'hint', style: { margin: 0 } }, '0 — снять скидку')),

      h(Lab, null, 'Баланс и зачисления'),
      h('div', { className: 'card' },
        h('div', { className: 'price', style: { fontSize: 22, color: Number(c.balance) < 0 ? 'var(--red)' : 'var(--price)' } }, money(c.balance, cur)),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
          inp({ className: 'input mono', style: { flex: '0 0 110px', marginTop: 0 }, inputMode: 'decimal', placeholder: 'Сумма', value: bAmt, onChange: function (e) { setBAmt(e.target.value); } }),
          inp({ style: { flex: 1, marginTop: 0 }, placeholder: 'Комментарий (клиент увидит)', value: bCom, onChange: function (e) { setBCom(e.target.value); } })),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 9 } },
          h('button', { className: 'btn soft', style: { marginTop: 0, flex: 1 }, disabled: busy, onClick: function () { balAdjust(1); } }, icon('coin', { size: 16 }), ' Зачислить'),
          h('button', { className: 'btn line', style: { marginTop: 0, flex: 1 }, disabled: busy, onClick: function () { balAdjust(-1); } }, '− Списать')),
        // член семьи: зачисление/списание выше уходит на ОБЩИЙ счёт — история семьи тут же
        c.family && (c.family.tx || []).length > 0 && h('div', { style: { marginTop: 10 } },
          h('div', { style: { fontSize: 11.5, color: 'var(--faint)', fontWeight: 600 } }, 'ОБЩИЙ СЧЁТ СЕМЬИ'),
          (c.family.tx || []).map(function (t) {
            return h('div', { key: 'f' + t.id, style: { padding: '7px 0', borderTop: '1px solid var(--line)' } },
              h('div', { className: 'spread', style: { fontSize: 12.5 } },
                h('span', { style: { color: 'var(--muted)', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
                  h('span', { className: 'mono', style: { color: 'var(--faint)' } }, t.at_label + ' · '),
                  t.reason || (t.amount >= 0 ? 'Пополнение' : 'Списание'),
                  t.user_name ? h('span', { style: { color: 'var(--faint)' } }, ' · ' + t.user_name) : null),
                h('b', { className: 'mono', style: { flex: '0 0 auto', marginLeft: 8, color: t.amount >= 0 ? 'var(--price)' : 'var(--red)' } }, (t.amount >= 0 ? '+' : '−') + money(Math.abs(t.amount), cur))),
              t.comment && h('div', { style: { fontSize: 12, color: 'var(--faint)', fontStyle: 'italic', marginTop: 3 } }, '💬 ' + t.comment));
          })),
        // история движений: дата · назначение · сумма; заметка мастера редактируется по 💬
        (c.balance_tx || []).length === 0
          ? (c.family ? null : h('p', { className: 'hint', style: { marginTop: 10 } }, 'Движений по балансу ещё не было.'))
          : h('div', { style: { marginTop: 10 } }, (c.balance_tx || []).map(function (t) {
              return h('div', { key: t.id, style: { padding: '7px 0', borderTop: '1px solid var(--line)' } },
                h('div', { className: 'spread', style: { fontSize: 12.5 } },
                  h('span', { style: { color: 'var(--muted)', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
                    h('span', { className: 'mono', style: { color: 'var(--faint)' } }, t.at_label + ' · '),
                    t.reason || (t.amount >= 0 ? 'Пополнение' : 'Списание')),
                  h('span', { style: { display: 'flex', alignItems: 'center', gap: 6, flex: '0 0 auto', marginLeft: 8 } },
                    h('b', { className: 'mono', style: { color: t.amount >= 0 ? 'var(--price)' : 'var(--red)' } }, (t.amount >= 0 ? '+' : '−') + money(Math.abs(t.amount), cur)),
                    iconBtn('pencil', function () { setTxEdit(txEdit && txEdit.id === t.id ? null : { id: t.id, text: t.comment || '' }); }, { title: 'Комментарий к транзакции' }))),
                t.comment && !(txEdit && txEdit.id === t.id)
                  && h('div', { style: { fontSize: 12, color: 'var(--faint)', fontStyle: 'italic', marginTop: 3 } }, '💬 ' + t.comment),
                txEdit && txEdit.id === t.id && h('div', { style: { display: 'flex', gap: 8, marginTop: 6 } },
                  inp({ style: { flex: 1, marginTop: 0 }, placeholder: 'Комментарий (пусто — удалить)', value: txEdit.text, onChange: function (e) { setTxEdit({ id: t.id, text: e.target.value }); } }),
                  h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 14px' }, onClick: saveTxComment }, 'Сохранить')));
            })),
        h('p', { className: 'hint', style: { marginTop: 8 } }, 'Клиент видит баланс, историю и комментарии у себя в профиле.')),

      h(Lab, null, 'Карточка'),
      h('div', { className: 'card' },
        field('Имя (как удобно мастеру; пусто — из Telegram)', inp({ value: f.name, onChange: function (e) { setF(Object.assign({}, f, { name: e.target.value })); } })),
        field('Телефон', inp({ className: 'input mono', value: f.phone, onChange: function (e) { setF(Object.assign({}, f, { phone: e.target.value })); } })),
        field('Заметка', h('textarea', { className: 'input', style: { minHeight: 56, resize: 'vertical' }, value: f.note, onChange: function (e) { setF(Object.assign({}, f, { note: e.target.value })); } })),
        h('button', { className: 'btn soft', style: { marginTop: 2 }, disabled: busy, onClick: saveFields }, icon('check', { size: 16 }), ' Сохранить карточку')),

      h(Lab, null, 'Задачи по клиенту'),
      (c.tasks || []).map(function (t) {
        return h('div', { key: t.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px' } },
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontSize: 13.5, fontWeight: 600 } }, t.title),
            t.due_label && h('div', { style: { fontSize: 11.5, marginTop: 2, color: t.overdue ? 'var(--red)' : 'var(--faint)' } }, (t.overdue ? '⏰ просрочено · ' : 'до ') + t.due_label)),
          iconBtn('check', function () { taskDone(t.id); }, { title: 'Сделано' }));
      }),
      h('div', { className: 'card', style: { padding: 12 } },
        h('div', { style: { display: 'flex', gap: 8 } },
          inp({ placeholder: 'Напомнить… (перезвонить, предложить докуп)', value: nt.title, onChange: function (e) { setNt(Object.assign({}, nt, { title: e.target.value })); } }),
          inp({ className: 'input mono', style: { flex: '0 0 92px' }, placeholder: 'ДД.ММ / +3', value: nt.due, onChange: function (e) { setNt(Object.assign({}, nt, { due: e.target.value })); } })),
        h('p', { className: 'hint', style: { marginTop: 6 } }, 'Срок: «15.08» — дата, «+3» — через 3 дня, пусто — без срока.'),
        h('button', { className: 'btn soft', style: { marginTop: 6 }, onClick: addTask }, icon('plus', { size: 16 }), ' Добавить задачу')));
  }

  // ───────────────────── ЗАДАЧИ-НАПОМИНАНИЯ (этап 4) ─────────────────────
  function AdminTasks(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var as = useState(false); var all = as[0], setAll = as[1];
    var cs = useState([]); var clients = cs[0], setClients = cs[1];
    var fs = useState({ title: '', due: '', uid: '' }); var f = fs[0], setF = fs[1];
    function load(allv) {
      API.adminTasks(allv !== undefined ? allv : all).then(function (j) { if (j.ok) setData(j); else app.toast(j.error || 'Ошибка'); });
    }
    useEffect(function () {
      load();
      API.adminClients({}).then(function (j) { if (j && j.ok) setClients(j.clients || []); });
    }, []);
    function add() {
      if (!f.title.trim()) { app.toast('Что напомнить?'); return; }
      API.adminTask({ action: 'add', title: f.title, due: f.due, uid: f.uid || 0 }).then(function (j) {
        if (j.ok) { app.toast('Добавлено'); setF({ title: '', due: '', uid: '' }); load(); } else app.toast(j.error || 'Ошибка');
      });
    }
    function act(action, id) { API.adminTask({ action: action, id: id }).then(function (j) { if (j.ok) load(); else app.toast(j.error || 'Ошибка'); }); }
    var items = (data && data.tasks) || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(function () { load(); }) }, 'Задачи мастера'),
      h('p', { className: 'hint', style: { marginTop: 0 } }, 'Напоминания по клиентам и заказам: «перезвонить», «спросить отзыв», «предложить докуп». В срок придёт сообщение от бота.'),
      h('div', { className: 'chips', style: { marginTop: 9 } },
        h('button', { className: 'chip' + (all ? '' : ' on'), type: 'button', onClick: function () { setAll(false); load(false); } }, 'Активные'),
        h('button', { className: 'chip' + (all ? ' on' : ''), type: 'button', onClick: function () { setAll(true); load(true); } }, 'Все')),
      h('div', { className: 'card', style: { marginTop: 10, padding: 12 } },
        inp({ placeholder: 'Напомнить…', value: f.title, onChange: function (e) { setF(Object.assign({}, f, { title: e.target.value })); } }),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 9 } },
          inp({ className: 'input mono', style: { flex: '0 0 100px' }, placeholder: 'ДД.ММ / +3', value: f.due, onChange: function (e) { setF(Object.assign({}, f, { due: e.target.value })); } }),
          h('select', { className: 'input', style: { flex: 1, minWidth: 0 }, value: f.uid, onChange: function (e) { setF(Object.assign({}, f, { uid: e.target.value })); } },
            [h('option', { key: '_', value: '' }, '— без клиента —')].concat(clients.map(function (cc) { return h('option', { key: cc.uid, value: String(cc.uid) }, cc.name); })))),
        h('p', { className: 'hint', style: { marginTop: 6 } }, 'Срок: «15.08» — дата, «+3» — через 3 дня, пусто — без срока.'),
        h('button', { className: 'btn soft', style: { marginTop: 6 }, onClick: add }, icon('plus', { size: 16 }), ' Добавить')),
      data === null && h('p', { className: 'hint' }, 'Загружаю…'),
      data !== null && items.length === 0 && h('p', { className: 'hint' }, 'Задач нет — чистый стол 🎉'),
      items.map(function (t) {
        return h('div', { key: t.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px', opacity: t.done ? 0.55 : 1 } },
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontSize: 13.5, fontWeight: 600, textDecoration: t.done ? 'line-through' : 'none' } }, t.title),
            h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } },
              (t.client ? t.client + ' · ' : '') + (t.due_label ? ((t.overdue && !t.done) ? '⏰ просрочено · ' + t.due_label : (t.today && !t.done ? '🔥 сегодня' : 'до ' + t.due_label)) : 'без срока'))),
          t.done
            ? iconBtn('refresh', function () { act('reopen', t.id); }, { title: 'Вернуть' })
            : iconBtn('check', function () { act('done', t.id); }, { title: 'Сделано' }),
          iconBtn('trash', function () { act('delete', t.id); }, { danger: true, title: 'Удалить' }));
      }));
  }

  // ───── Объединённые экраны с вкладками (admin-routes-plan.md, OpenTodo 4b4a7781) ─────
  // Старые экраны живут под своими ключами (deep-links не ломаются) — здесь только обёртки,
  // которые рендерят их как вкладки. Ребёнок монтируется лишь на активной вкладке (живой
  // опрос принтера не работает в фоне других вкладок).
  // Вкладка каждой записи: [подпись, геттер компонента, ключ для deep-link ?tab=…].
  // ?tab= разбирает BOOT (app.js) и отдаёт один раз через app.takeBootTab() — так
  // go=print&tab=service открывает сразу «Обслуживание», а не первую вкладку (IA-редизайн).
  function tabbed(tabs, extraRight) {
    return function TabScreen(props) {
      var ts = useState(function () {
        var hint = props.app && props.app.takeBootTab ? props.app.takeBootTab() : '';
        var idx = 0;
        if (hint) tabs.forEach(function (t, i) { if (t[2] === hint) idx = i; });
        return idx;
      });
      var tab = ts[0], setTab = ts[1];
      var Cur = tabs[tab][1]();
      return h(React.Fragment, null,
        h('div', { className: 'screen', style: { paddingBottom: 0 } },
          h('div', { className: 'chips' },
            tabs.map(function (t, i) {
              // setView — подвид для менеджера скролла (861eab19): у каждой вкладки своя
              // запомненная позиция, иначе переход на короткую вкладку оставлял бы её
              // проскролленной на позицию длинной. Первая вкладка = '' (состояние по
              // умолчанию, под этим ключом App хранит позицию до первого setView).
              return h('button', { key: i, className: 'chip' + (tab === i ? ' on' : ''), type: 'button',
                onClick: function () { setTab(i); if (props.app.setView) props.app.setView(i ? 't' + i : ''); } }, t[0]);
            }),
            extraRight ? extraRight(props.app) : null)),
        Cur ? h(Cur, { app: props.app }) : null);
    };
  }
  // «Печать»: очередь + принтер + обслуживание + печать для себя — один контекст (железо)
  var AdminPrintHub = tabbed([
    ['Очередь', function () { return window.AdminQueue; }, 'queue'],
    ['Принтер', function () { return window.AdminPrinter; }, 'printer'],
    ['Обслуживание', function () { return window.AdminService; }, 'service'],
    ['Для себя', function () { return window.AdminSelfPrint; }, 'self'],
  ]);
  // «Цены и скидки»: цены + скидки + промокоды; калькулятор — кнопкой (отдельный экран)
  var AdminPricing = tabbed([
    ['Цены', function () { return window.AdminSettings; }, 'prices'],
    ['Скидки', function () { return window.AdminDiscounts; }, 'discounts'],
    ['Промокоды', function () { return window.AdminPromos; }, 'promos'],
  ], function (app) {
    return h('button', { className: 'chip', type: 'button', style: { marginLeft: 'auto' }, onClick: function () { app.go('adminPriceCalc'); } }, '🧮 Калькулятор');
  });
  // «Пластики»: склад пластиков + справочник оттенков. На ПК отдельного раздела «Оттенки» нет —
  // цвета живут ВНУТРИ карточки пластика (ColorsSec в desk/src/screens/plastics.jsx), а новый
  // оттенок заводится прямо оттуда. На телефоне «Оттенки» стояли восьмым пунктом «Каталога»,
  // то есть один раздел ПК был разложен на две страницы (фидбэк владельца 03.09.2026 — тот же
  // разнобой, что у «печати на заказ» и «для себя»). Теперь это вкладка одного раздела.
  var AdminPlasticsHub = tabbed([
    ['Пластики', function () { return window.AdminPlastics; }, 'plastics'],
    ['Оттенки', function () { return window.AdminShades; }, 'shades'],
  ]);
  // «Журнал мастера»: разбор ссылок (бывш. «Очередь разбора») + журнал печатей
  var AdminMasterLog = tabbed([
    ['Разбор ссылок', function () { return window.AdminCalcQueue; }, 'links'],
    ['Журнал печатей', function () { return window.AdminJournal; }, 'journal'],
  ]);

  // ───────────────────── Готовые изделия / витрина (OpenTodo bd896aa6) ─────────────────────
  // CRUD витрины: мастер добавляет товар (название/описание/цена/наличие/фото), скрывает и удаляет.
  // Продажа идёт через /api/product_order (клиентская витрина Products).
  function AdminProducts(props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    var EMPTY = { id: null, title: '', description: '', price: '', stock: '', is_active: true };
    var fs = useState(EMPTY); var form = fs[0], setForm = fs[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    function load() { API.adminProducts().then(function (j) { if (j && j.ok) setData(j); }); }
    useEffect(function () { load(); }, []);
    function patch(p) { setForm(function (f) { return Object.assign({}, f, p); }); }
    function edit(p) { setForm({ id: p.id, title: p.title, description: p.description || '', price: p.price, stock: p.stock, is_active: !!p.is_active }); try { window.scrollTo(0, 0); } catch (e) {} }
    function resetForm() { setForm(EMPTY); }
    function save() {
      if (!String(form.title).trim()) { app.toast('Укажите название'); return; }
      setBusy(true);
      API.adminProductSave({ id: form.id || undefined, title: form.title, description: form.description, price: form.price, stock: form.stock, is_active: form.is_active }).then(function (j) {
        setBusy(false);
        if (!j || !j.ok) { app.toast((j && j.error) || 'Ошибка'); return; }
        app.toast('Сохранено');
        patch({ id: j.id });   // теперь появится кнопка загрузки фото
        load();
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    function del(p) {
      API.adminProductDelete(p.id).then(function (j) {
        if (j && j.ok) { app.toast('Удалено'); if (form.id === p.id) resetForm(); load(); }
        else app.toast((j && j.error) || 'Ошибка');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function toggleActive(p) {
      API.adminProductSave({ id: p.id, title: p.title, description: p.description, price: p.price, stock: p.stock, is_active: !p.is_active })
        .then(function (j) { if (j && j.ok) load(); else app.toast((j && j.error) || 'Ошибка'); });
    }
    function uploadPhoto(file) {
      if (!file || !form.id) return;
      if (file.size > 12 * 1024 * 1024) { app.toast('Фото больше 12 МБ — выберите поменьше'); return; }
      var rd = new FileReader();
      rd.onload = function () {
        app.toast('Загружаю фото…');
        API.adminPhoto('product', form.id, rd.result).then(function (j) {
          app.toast(j.ok ? 'Фото обновлено' : (j.error || 'Не удалось загрузить'));
          if (j.ok) load();
        }).catch(function () { app.toast('Сеть недоступна'); });
      };
      rd.readAsDataURL(file);
    }
    if (!data) return loading('Готовые изделия');
    var items = data.products || [];
    var cur = data.currency || 'руб.';
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Готовые изделия'),
      h('div', { className: 'card', style: { padding: 14 } },
        h('div', { style: { fontWeight: 700, marginBottom: 10 } }, form.id ? ('Редактирование #' + form.id) : 'Новый товар'),
        field('Название', inp({ value: form.title, maxLength: 120, placeholder: 'Ваза-спираль (готовая)', onChange: function (e) { patch({ title: e.target.value }); } })),
        field('Описание', h('textarea', { className: 'input', rows: 3, value: form.description, maxLength: 2000, placeholder: 'Материал, размер, что важно покупателю', onChange: function (e) { patch({ description: e.target.value }); }, style: { resize: 'vertical', fontFamily: 'inherit' } })),
        h('div', { className: 'grid2', style: { gap: 10 } },
          field('Цена (' + cur + ')', inp({ type: 'text', inputMode: 'decimal', value: form.price, placeholder: '0', onChange: function (e) { patch({ price: e.target.value }); } })),
          field('В наличии, шт', inp({ type: 'number', min: 0, value: form.stock, placeholder: '0', onChange: function (e) { patch({ stock: e.target.value }); } }))),
        h('label', { style: { display: 'flex', alignItems: 'center', gap: 8, margin: '2px 2px 10px', fontSize: 13.5, cursor: 'pointer' } },
          h('input', { type: 'checkbox', checked: form.is_active, onChange: function (e) { patch({ is_active: e.target.checked }); } }),
          'Показывать в витрине клиентам'),
        form.id
          ? h('label', { className: 'btn line', style: { marginTop: 0, cursor: 'pointer' } },
            icon('image', { size: 17 }), ' Фото товара',
            h('input', { type: 'file', accept: 'image/*', style: { display: 'none' }, onChange: function (e) { if (e.target.files[0]) uploadPhoto(e.target.files[0]); e.target.value = ''; } }))
          : h('p', { className: 'hint', style: { marginTop: 0 } }, 'Сохраните товар — после этого можно добавить фото.'),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 12 } },
          h('button', { className: 'btn', style: { marginTop: 0 }, disabled: busy, onClick: save }, icon('check', { size: 17 }), ' ', form.id ? 'Сохранить' : 'Создать'),
          form.id && h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: resetForm }, icon('plus', { size: 16 }), ' Новый'))),
      h(Lab, null, items.length + ' в каталоге'),
      items.length === 0 && h('p', { className: 'hint', style: { marginTop: 0 } }, 'Пока нет товаров — заполните форму выше.'),
      items.map(function (p) {
        return h('div', { key: p.id, className: 'card', style: { padding: 11, marginTop: 10, display: 'flex', gap: 11, alignItems: 'center', opacity: p.is_active ? 1 : 0.55 } },
          p.photo
            ? h('img', { src: p.photo, alt: '', style: { width: 56, height: 56, objectFit: 'cover', borderRadius: 10, flex: '0 0 auto' } })
            : h('div', { style: { width: 56, height: 56, borderRadius: 10, background: 'var(--surface-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto', color: 'var(--faint)' } }, icon('box', { size: 20 })),
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontWeight: 700, fontSize: 14.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, p.title,
              !p.is_active ? h('span', { style: { color: 'var(--faint)', fontWeight: 400 } }, ' · скрыт') : null),
            h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 3 } },
              money(p.price, cur), ' · ',
              p.stock > 0 ? ('в наличии ' + p.stock) : h('span', { style: { color: 'var(--red)' } }, 'нет в наличии'),
              ' · продано ' + (p.sold || 0))),
          h('div', { style: { display: 'flex', gap: 6, flex: '0 0 auto' } },
            iconBtn('pencil', function () { edit(p); }, { title: 'Изменить' }),
            iconBtn('check', function () { toggleActive(p); }, { title: p.is_active ? 'Скрыть из витрины' : 'Показать в витрине', on: p.is_active }),
            iconBtn('trash', function () { del(p); }, { title: 'Удалить', danger: true })));
      }));
  }

  // confirm в стиле приложения: tg.showConfirm с фолбэком на window.confirm
  function askConfirmish(msg, onYes) {
    try { var tg = window.Telegram && window.Telegram.WebApp; if (tg && tg.showConfirm) { tg.showConfirm(msg, function (ok) { if (ok) onYes(); }); return; } } catch (e) {}
    if (window.confirm(msg)) onYes();
  }

  // ── Генерация картинки release note по промту (редактируемый комбобокс с заготовками) ──
  // Тот же генератор, что у аватарок (image_regen на сервере): промт по-русски, стиль
  // «маскот-робот» добавится сам. Последний промт черновика живёт в payload.gen_prompt.
  var GEN_PROMPTS = [
    'робот-маскот радостно поднимает готовую напечатанную деталь возле 3D-принтера',
    'семья маленьких роботов смотрит, как 3D-принтер печатает деталь',
    'робот-маскот с гаечным ключом настраивает большой 3D-принтер',
    'робот-маскот везёт тележку с разноцветными напечатанными деталями',
    'робот-маскот в очках изучает чертёж рядом с 3D-принтером и готовыми деталями'];

  function GenImageSheet(props) {
    var n = props.n, app = props.app, onClose = props.onClose, onDone = props.onDone;
    var ps = useState(((n.payload || {}).gen_prompt) || GEN_PROMPTS[0]);
    var prompt = ps[0], setPrompt = ps[1];
    var os = useState(false); var open = os[0], setOpen = os[1];   // раскрыт ли список заготовок
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    function gen() {
      var p = prompt.trim();
      if (!p) { app.toast('Опишите, что нарисовать'); return; }
      setBusy(true);
      API.adminNotificationAction(n.id, 'image_regen', { prompt: p }).then(function (j) {
        setBusy(false);
        if (j && j.ok) {
          // честно про движок: процедурная заглушка ≠ ИИ-картинка, мастеру стоит перегенерить
          app.toast(j.engine === 'procedural'
            ? 'Видеокарта занята — вышел простой рисунок, попробуйте ещё раз'
            : j.engine === 'sd-turbo' ? 'Сгенерировано запасной моделью (быстрой)'
            : j.engine === 'z-image-turbo-lite' ? 'Сгенерировано Z-Image в облегчённом кванте (мало свободной видеопамяти)'
            : 'Картинка сгенерирована');
          onDone();
        } else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    return h('div', { className: 'csheet-ov', onClick: function () { if (!busy) onClose(); } },
      h('div', { className: 'csheet', onClick: function (e) { e.stopPropagation(); } },
        h('div', { className: 'csh-h' },
          h('div', { className: 'csh-t' }, '✨ Сгенерировать картинку'),
          h('button', { className: 'csh-x', type: 'button', onClick: function () { if (!busy) onClose(); } }, icon('close', { size: 16 }))),
        h('p', { className: 'hint', style: { margin: '10px 2px 6px' } },
          'Опишите сюжет по-русски — стиль маскота-робота добавится сам. Можно выбрать заготовку из списка и поправить её.'),
        h('div', { style: { display: 'flex', gap: 6, alignItems: 'stretch' } },
          h('textarea', { className: 'input', rows: 3, value: prompt, maxLength: 300, disabled: busy,
            onChange: function (ev) { setPrompt(ev.target.value); },
            style: { resize: 'vertical', fontFamily: 'inherit', lineHeight: 1.4, flex: 1 } }),
          h('button', { className: 'chip' + (open ? ' on' : ''), type: 'button', disabled: busy,
            style: { flex: '0 0 auto', alignSelf: 'flex-start' }, 'aria-label': 'Заготовки промтов',
            onClick: function () { setOpen(!open); } }, open ? '▴' : '▾')),
        open && h('div', { style: { marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 } },
          GEN_PROMPTS.map(function (p, i) {
            return h('button', { key: i, type: 'button', className: 'chip' + (p === prompt ? ' on' : ''),
              style: { width: '100%', textAlign: 'left', whiteSpace: 'normal', lineHeight: 1.35 },
              onClick: function () { setPrompt(p); setOpen(false); } }, p);
          })),
        h('button', { className: 'btn', style: { marginTop: 12 }, disabled: busy, onClick: gen },
          busy ? 'Генерирую… (до минуты)' : '✨ Сгенерировать'),
        busy && h('p', { className: 'hint', style: { margin: '8px 2px 0' } },
          'Горячая модель — ~20 с, холодная — до минуты. Если видеокарта занята, выйдет простой рисунок — можно перегенерить ещё раз.')));
  }

  // ───────────────────── Центр уведомлений (2026-07-16) ─────────────────────
  // Черновики release notes от Клода: текст + картинка, «Опубликовать» шлёт всем клиентам.
  // Семейные счета вынесены в отдельный раздел AdminFamilies (2026-07-17).
  function AdminNotifs(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var as_ = useState(false); var showAll = as_[0], setShowAll = as_[1];
    var bs = useState(0); var busyId = bs[0], setBusyId = bs[1];
    // правки release note: {id: {title, body}} — черновик до «Сохранить»
    var es = useState({}); var edits = es[0], setEdits = es[1];
    // уведомление, для которого открыт диалог генерации картинки (null — закрыт)
    var gs = useState(null); var genFor = gs[0], setGenFor = gs[1];

    function load(all) {
      API.adminNotifications(all === undefined ? showAll : all).then(function (j) { if (j && j.ok) setData(j); });
    }
    useEffect(function () { load(); }, []);

    if (!data) return loading('Уведомления');

    function act(n, action, extra, doneMsg) {
      setBusyId(n.id);
      API.adminNotificationAction(n.id, action, extra).then(function (j) {
        setBusyId(0);
        if (j && j.ok) { if (doneMsg) app.toast(typeof doneMsg === 'function' ? doneMsg(j) : doneMsg); load(); }
        else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { setBusyId(0); app.toast('Сеть недоступна'); });
    }
    function editOf(n) { return edits[n.id] || { title: n.title, body: n.body }; }
    function patchEdit(nid, p) { setEdits(function (e) { var o = Object.assign({}, e); o[nid] = Object.assign({}, o[nid] || {}, p); return o; }); }
    function uploadImage(n, file) {
      if (!file) return;
      if (file.size > 12 * 1024 * 1024) { app.toast('Картинка больше 12 МБ'); return; }
      var rd = new FileReader();
      rd.onload = function () {
        app.toast('Загружаю картинку…');
        API.adminPhoto('notif', n.id, rd.result).then(function (j) {
          app.toast(j.ok ? 'Картинка обновлена' : (j.error || 'Не удалось'));
          if (j.ok) load();
        }).catch(function () { app.toast('Сеть недоступна'); });
      };
      rd.readAsDataURL(file);
    }
    var STATUS_LABEL = { done: 'выполнено', rejected: 'отклонено', published: 'опубликовано' };

    // «Чья печать?» (4effbc1b): завершённая печать ждёт привязки к заказу — кнопка ведёт
    // на экран выбора со списком активных заказов.
    function printClassifyCard(n) {
      var isNew = n.status === 'new';
      var jid = (n.payload || {}).jid;
      return h('div', { key: n.id, className: 'card', style: { padding: 14, marginTop: 10, opacity: isNew ? 1 : 0.6 } },
        h('div', { className: 'spread' },
          h('div', { style: { fontWeight: 700 } }, '🖨 ' + (n.title || 'Печать завершена')),
          h('span', { style: { fontSize: 11, color: 'var(--faint)', flex: '0 0 auto', marginLeft: 8 } }, (STATUS_LABEL[n.status] ? STATUS_LABEL[n.status] + ' · ' : '') + (n.ago || ''))),
        n.body && h('div', { style: { fontSize: 13.5, color: 'var(--muted)', marginTop: 7, lineHeight: 1.45 } }, n.body),
        isNew && jid && h('button', { className: 'btn', style: { marginTop: 11 }, onClick: function () { app.openPrintClassify(jid); } }, icon('printer', { size: 17 }), ' Выбрать заказ'),
        isNew && h('button', { className: 'btn line', style: { marginTop: 8 }, disabled: busyId === n.id, onClick: function () { act(n, 'reject', null, 'Скрыто'); } }, 'Скрыть'));
    }

    function releaseNoteCard(n) {
      var e = editOf(n);
      var isNew = n.status === 'new';
      var dirty = e.title !== n.title || e.body !== n.body;
      return h('div', { key: n.id, className: 'card', style: { padding: 14, marginTop: 10, opacity: isNew ? 1 : 0.6 } },
        h('div', { className: 'spread' },
          h('div', { style: { fontWeight: 700 } }, '📣 Release notes'),
          h('span', { style: { fontSize: 11, color: 'var(--faint)', flex: '0 0 auto', marginLeft: 8 } }, (STATUS_LABEL[n.status] ? STATUS_LABEL[n.status] + ' · ' : '') + (n.ago || ''))),
        n.image_url
          ? h('div', { style: { position: 'relative', marginTop: 10 } },
              h('img', { src: n.image_url, alt: '', style: { width: '100%', borderRadius: 12, display: 'block' } }),
              isNew && h('div', { style: { position: 'absolute', top: 8, right: 8, display: 'flex', gap: 6 } },
                h('button', { className: 'chip', style: { background: 'rgba(10,13,18,.8)' },
                  onClick: function () { setGenFor(n); } }, '✨ Сгенерировать'),
                h('label', { style: { cursor: 'pointer' } },
                  h('span', { className: 'chip', style: { background: 'rgba(10,13,18,.8)' } }, '↻ Заменить'),
                  h('input', { type: 'file', accept: 'image/*', style: { display: 'none' }, onChange: function (ev) { if (ev.target.files[0]) uploadImage(n, ev.target.files[0]); ev.target.value = ''; } })),
                h('button', { className: 'chip', style: { background: 'rgba(10,13,18,.8)', color: 'var(--red)' },
                  onClick: function () { act(n, 'image_del', null, 'Картинка удалена'); } }, '✕')))
          : (isNew && h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
              h('label', { className: 'btn line', style: { marginTop: 0, flex: 1, cursor: 'pointer' } },
                icon('image', { size: 16 }), ' Добавить картинку',
                h('input', { type: 'file', accept: 'image/*', style: { display: 'none' }, onChange: function (ev) { if (ev.target.files[0]) uploadImage(n, ev.target.files[0]); ev.target.value = ''; } })),
              h('button', { className: 'btn line', style: { marginTop: 0, flex: 1 },
                onClick: function () { setGenFor(n); } }, '✨ Сгенерировать'))),
        isNew
          ? h('div', { style: { marginTop: 10 } },
              field('Заголовок', inp({ value: e.title, maxLength: 120, onChange: function (ev) { patchEdit(n.id, { title: ev.target.value }); } })),
              field('Текст для клиентов', h('textarea', { className: 'input', rows: 6, value: e.body, maxLength: 4000,
                onChange: function (ev) { patchEdit(n.id, { body: ev.target.value }); },
                style: { resize: 'vertical', fontFamily: 'inherit', lineHeight: 1.45 } })),
              // с картинкой Telegram ограничивает подпись ~1000 символами — предупреждаем заранее
              n.image_url && e.body.length > 900 && h('p', { className: 'hint', style: { marginTop: 0 } },
                '⚠️ С картинкой текст обрежется примерно до 1000 символов (сейчас ' + e.body.length + ').'),
              h('div', { style: { display: 'flex', gap: 8 } },
                h('button', { className: 'btn', style: { marginTop: 0, flex: 1 }, disabled: busyId === n.id, onClick: function () {
                  var doPublish = function () {
                    askConfirmish('Опубликовать? Уйдёт всем клиентам в бота' + (n.image_url ? ' с картинкой' : '') + '.', function () {
                      act(n, 'publish', null, function (j) { return 'Опубликовано: доставлено ' + j.sent + (j.failed ? (', недоставлено ' + j.failed) : ''); });
                    });
                  };
                  // несохранённые правки публикуем только после УСПЕШНОГО save —
                  // иначе при его ошибке клиентам ушёл бы старый текст
                  if (dirty) API.adminNotificationAction(n.id, 'save', { title: e.title, body: e.body }).then(function (j) {
                    if (j && j.ok) doPublish(); else app.toast((j && j.error) || 'Не удалось сохранить правки');
                  }).catch(function () { app.toast('Сеть недоступна'); });
                  else doPublish();
                } }, icon('share', { size: 16 }), ' Опубликовать'),
                dirty && h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 14px' }, disabled: busyId === n.id,
                  onClick: function () { act(n, 'save', { title: e.title, body: e.body }, 'Сохранено'); } }, 'Сохранить'),
                h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 12px' }, disabled: busyId === n.id,
                  onClick: function () { askConfirmish('Удалить черновик?', function () { act(n, 'delete', null, 'Удалено'); }); } }, icon('trash', { size: 16 }))))
          : h('div', { style: { marginTop: 8 } },
              h('div', { style: { fontWeight: 700, fontSize: 13.5 } }, n.title),
              h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 4, whiteSpace: 'pre-wrap' } }, n.body),
              h('button', { className: 'btn line', style: { marginTop: 10, width: 'auto', padding: '0 12px' },
                onClick: function () { askConfirmish('Удалить запись?', function () { act(n, 'delete', null, 'Удалено'); }); } }, icon('trash', { size: 16 }))));
    }

    var items = data.items || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(function () { load(); }) }, 'Уведомления'),
      h('div', { className: 'chips', style: { marginBottom: 4 } },
        h('button', { className: 'chip' + (!showAll ? ' on' : ''), type: 'button', onClick: function () { setShowAll(false); load(false); } }, 'Новые'),
        h('button', { className: 'chip' + (showAll ? ' on' : ''), type: 'button', onClick: function () { setShowAll(true); load(true); } }, 'Все')),
      items.length === 0 && h('p', { className: 'hint', style: { marginTop: 8 } }, 'Пока пусто: сюда падают черновики release notes от Клода после обновлений приложения.'),
      items.map(function (n) { return n.kind === 'print_classify' ? printClassifyCard(n) : releaseNoteCard(n); }),
      genFor && h(GenImageSheet, { n: genFor, app: app,
        onClose: function () { setGenFor(null); },
        onDone: function () { setGenFor(null); load(); } }));
  }

  // ───────────────────── Семейные счета (2026-07-17, вынесено из «Уведомлений») ─────────────────────
  // Заявки клиентов с кнопками «Создать счёт»/«Отклонить», существующие семьи
  // (добавить/исключить участника, роспуск при одном оставшемся), ручное создание.
  function AdminFamilies(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var as_ = useState(false); var showAll = as_[0], setShowAll = as_[1];
    var bs = useState(0); var busyId = bs[0], setBusyId = bs[1];
    // выбор участников: {notif_id: [uid, uid, ...]}; ключ 0 — форма «создать вручную»
    var ms = useState({}); var memberSel = ms[0], setMemberSel = ms[1];
    // выбор «добавить участника» — СВОЙ на каждую карточку семьи: {family_id: 'uid'}
    var fs = useState({}); var famAddSel = fs[0], setFamAddSel = fs[1];

    function load(all) {
      API.adminFamilies(all === undefined ? showAll : all).then(function (j) { if (j && j.ok) setData(j); });
    }
    useEffect(function () { load(); }, []);

    if (!data) return loading('Семейные счета');
    var customers = data.customers || [];

    function custOptions(extraEmpty) {
      return [h('option', { key: '_', value: '' }, extraEmpty || '— выбрать клиента —')].concat(customers.map(function (c) {
        return h('option', { key: c.uid, value: String(c.uid) }, (c.name || ('id ' + c.uid)) + ' · ' + c.orders + ' зак.');
      }));
    }
    // подбор клиента по «@username или имя» из заявки — предзаполняем select партнёра
    function matchCustomer(text) {
      var t = String(text || '').toLowerCase().replace(/^@/, '');
      if (!t) return '';
      var hit = customers.filter(function (c) { return (c.name || '').toLowerCase().replace(/^@/, '').indexOf(t) !== -1; })[0];
      return hit ? String(hit.uid) : '';
    }
    function membersOf(n) {
      var cur = memberSel[n.id];
      if (cur) return cur;
      // дефолт для заявки: автор + распознанный партнёр (+ пустой слот, если не распознан)
      if (n.kind === 'family_request') {
        var p = n.payload || {};
        // партнёр выбран нативным пикером (0528ba47) → точный id, без гаданий по имени
        return [String(p.from_id || ''), p.partner_id ? String(p.partner_id) : matchCustomer(p.partner)];
      }
      return ['', ''];
    }
    function setMembers(nid, arr) { setMemberSel(function (m) { var o = Object.assign({}, m); o[nid] = arr; return o; }); }
    function createFamily(n) {
      var ids = membersOf(n).filter(Boolean).map(Number);
      if (ids.length < 2) { app.toast('Выберите минимум двух клиентов'); return; }
      var names = ids.map(function (uid) { var c = customers.filter(function (x) { return x.uid === uid; })[0]; return c ? c.name : ('id ' + uid); });
      askConfirmish('Открыть семейный счёт: ' + names.join(' + ') + '? Личные балансы перейдут в общий + бонус за открытие.', function () {
        setBusyId(n.id || -1);
        API.adminFamilyCreate(ids, n.id || 0).then(function (j) {
          setBusyId(0);
          if (j && j.ok) { app.toast('Семейный счёт открыт 👨‍👩‍👧'); setMembers(n.id || 0, ['', '']); load(); }
          else app.toast((j && j.error) || 'Не удалось');
        }).catch(function () { setBusyId(0); app.toast('Сеть недоступна'); });
      });
    }
    function act(n, action, extra, doneMsg) {
      setBusyId(n.id);
      API.adminNotificationAction(n.id, action, extra).then(function (j) {
        setBusyId(0);
        if (j && j.ok) { if (doneMsg) app.toast(typeof doneMsg === 'function' ? doneMsg(j) : doneMsg); load(); }
        else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { setBusyId(0); app.toast('Сеть недоступна'); });
    }
    var STATUS_LABEL = { done: 'выполнено', rejected: 'отклонено' };

    function familyRequestCard(n) {
      var members = membersOf(n);
      var isNew = n.status === 'new';
      return h('div', { key: n.id, className: 'card', style: { padding: 14, marginTop: 10, opacity: isNew ? 1 : 0.6 } },
        h('div', { className: 'spread' },
          h('div', { style: { fontWeight: 700 } }, '👨‍👩‍👧 ' + (n.title || 'Заявка на семейный счёт')),
          h('span', { style: { fontSize: 11, color: 'var(--faint)', flex: '0 0 auto', marginLeft: 8 } }, n.ago || '')),
        n.body && h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginTop: 4 } }, n.body),
        !isNew && h('div', { style: { fontSize: 12, color: 'var(--faint)', marginTop: 6 } }, '· ' + (STATUS_LABEL[n.status] || n.status)),
        isNew && h('div', { style: { marginTop: 10 } },
          members.map(function (v, i) {
            var opts = custOptions();
            // заявитель без единого заказа: его нет в списке клиентов — добавляем
            // отдельной опцией, иначе предвыбор невидим и заявку не одобрить
            // Партнёр из нативного пикера — тоже сплошь и рядом без заказов (в том и смысл:
            // зовут близкого, который сам ничего не заказывал). Раньше он попадал сюда и
            // рисовался как «id 702 · заявитель, заказов нет» — две «заявителя» в одной
            // карточке и голый id вместо имени, хотя имя лежит в payload.partner. Мастер
            // решал, что предвыбор сломан, и перевыбирал вручную — с риском попасть в
            // однофамильца, чей баланс при подтверждении уходит в чужую семью.
            if (v && !customers.some(function (c) { return String(c.uid) === String(v); })) {
              var p = n.payload || {};
              var isFrom = String(p.from_id) === String(v);
              var nm = isFrom ? (p.from_name || ('id ' + v))
                : (String(p.partner_id) === String(v) && p.partner ? p.partner : ('id ' + v));
              opts = opts.concat([h('option', { key: 'req' + v, value: String(v) },
                nm + (isFrom ? ' · заявитель, заказов нет' : ' · партнёр, заказов нет'))]);
            }
            return field('Участник ' + (i + 1), h('select', { key: i, className: 'input', value: v || '', onChange: function (e) {
              var arr = members.slice(); arr[i] = e.target.value; setMembers(n.id, arr);
            } }, opts));
          }),
          h('button', { className: 'chip', type: 'button', onClick: function () { setMembers(n.id, members.concat([''])); } }, '+ ещё участник'),
          h('div', { style: { display: 'flex', gap: 8, marginTop: 12 } },
            h('button', { className: 'btn', style: { marginTop: 0, flex: 1 }, disabled: busyId === n.id, onClick: function () { createFamily(n); } },
              icon('check', { size: 16 }), ' Создать счёт'),
            h('button', { className: 'btn line', style: { marginTop: 0, width: 'auto', padding: '0 14px' }, disabled: busyId === n.id,
              onClick: function () { act(n, 'reject', null, 'Заявка отклонена'); } }, 'Отклонить'))));
    }

    var requests = data.requests || [];
    var families = data.families || [];
    var manualMembers = memberSel[0] || ['', ''];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(function () { load(); }) }, 'Заявки'),
      h('div', { className: 'chips', style: { marginBottom: 4 } },
        h('button', { className: 'chip' + (!showAll ? ' on' : ''), type: 'button', onClick: function () { setShowAll(false); load(false); } }, 'Новые'),
        h('button', { className: 'chip' + (showAll ? ' on' : ''), type: 'button', onClick: function () { setShowAll(true); load(true); } }, 'Все')),
      requests.length === 0 && h('p', { className: 'hint', style: { marginTop: 8 } }, 'Заявок нет. Клиенты отправляют их из профиля («Открыть семейный счёт»).'),
      requests.map(familyRequestCard),

      h(Lab, null, 'Семейные счета'),
      families.length === 0 && h('p', { className: 'hint', style: { marginTop: 0 } }, 'Пока нет ни одного. Создайте из заявки выше или вручную.'),
      families.map(function (f) {
        var sel = famAddSel[f.id] || '';
        function setSel(v) { setFamAddSel(function (s) { var o = Object.assign({}, s); o[f.id] = v; return o; }); }
        function removeMember(m) {
          var nm = m.user_name || ('id ' + m.user_id);
          var msg = (f.members || []).length <= 2
            ? 'Исключить ' + nm + '? Останется один человек — семья распустится, остаток общего счёта вернётся ему на личный баланс.'
            : 'Исключить ' + nm + ' из семейного счёта? Общий баланс останется семье.';
          askConfirmish(msg, function () {
            API.adminFamilyRemove(f.id, m.user_id).then(function (j) {
              if (j && j.ok) { app.toast(j.dissolved ? 'Семья распущена' : 'Участник исключён'); load(); }
              else app.toast((j && j.error) || 'Не удалось');
            }).catch(function () { app.toast('Сеть недоступна'); });
          });
        }
        return h('div', { key: f.id, className: 'card', style: { padding: 14, marginTop: 10 } },
          h('div', { className: 'spread' },
            h('div', { style: { fontWeight: 700 } }, '👨‍👩‍👧 Счёт #' + f.id),
            h('b', { className: 'mono', style: { color: Number(f.balance) < 0 ? 'var(--red)' : 'var(--price)' } }, money(f.balance, data.currency))),
          h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 } },
            (f.members || []).map(function (m) {
              return h('span', { key: m.user_id, className: 'chip', style: { display: 'inline-flex', alignItems: 'center', gap: 6 } },
                m.user_name || ('id ' + m.user_id),
                h('button', { title: 'Исключить', onClick: function () { removeMember(m); },
                  style: { border: 'none', background: 'none', color: 'var(--red)', cursor: 'pointer', padding: 0, display: 'flex' } },
                  icon('close', { size: 12 })));
            })),
          h('div', { style: { display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' } },
            h('select', { className: 'input', style: { flex: 1 }, value: sel, onChange: function (e) { setSel(e.target.value); } },
              custOptions('+ добавить участника')),
            h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '0 14px' }, disabled: !sel, onClick: function () {
              API.adminFamilyAdd(f.id, Number(sel)).then(function (j) {
                if (j && j.ok) { app.toast('Участник добавлен'); setSel(''); load(); }
                else app.toast((j && j.error) || 'Не удалось');
              }).catch(function () { app.toast('Сеть недоступна'); });
            } }, icon('plus', { size: 16 }))));
      }),
      h('div', { className: 'card', style: { padding: 14, marginTop: 10 } },
        h('div', { style: { fontWeight: 700, marginBottom: 8 } }, 'Открыть счёт вручную'),
        manualMembers.map(function (v, i) {
          return field('Участник ' + (i + 1), h('select', { key: i, className: 'input', value: v || '', onChange: function (e) {
            var arr = manualMembers.slice(); arr[i] = e.target.value; setMembers(0, arr);
          } }, custOptions()));
        }),
        h('button', { className: 'chip', type: 'button', onClick: function () { setMembers(0, manualMembers.concat([''])); } }, '+ ещё участник'),
        h('button', { className: 'btn soft', style: { marginTop: 12 }, disabled: busyId === -1, onClick: function () { createFamily({ id: 0, kind: 'manual' }); } },
          icon('heart', { size: 16 }), ' Открыть семейный счёт')));
  }

  // ───────────────── «ЧЬЯ ПЕЧАТЬ?» — выбор заказа для завершённой печати (4effbc1b) ─────────
  // Открывается из телеграм-пинга о финише (web_app-кнопка, go=printclassify&id=jid) и из
  // карточки в «Уведомлениях». Список активных клиентских заказов с картинками; тап —
  // прикрепить печать; «Для себя» — учесть самопечатью. Дальше бот шлёт обычную оценку.
  function AdminPrintClassify(props) {
    var app = props.app;
    var jid = app.printClassifyJid;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    function load() { API.adminPrintClassify(jid).then(function (j) { setData(j || { ok: false, error: 'Сеть недоступна' }); }); }
    useEffect(function () { if (jid) load(); }, [jid]);
    if (!jid) return h('div', { className: 'screen' }, h('p', { className: 'hint' }, 'Печать не выбрана — откройте из уведомления.'));
    if (!data) return loading('Чья печать?');
    if (!data.ok) return h('div', { className: 'screen' }, h('p', { className: 'hint' }, data.error || 'Не удалось загрузить'));
    var p = data.print || {};
    function pick(action, id) {
      if (busy) return;
      setBusy(true);
      API.adminPrintClassifyPick(jid, action, id).then(function (j) {
        setBusy(false);
        if (j && j.ok) { app.toast(j.message || 'Готово'); app.back(); }
        else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { setBusy(false); app.toast('Сеть недоступна'); });
    }
    return h('div', { className: 'screen' },
      h(Lab, null, 'Чья печать?'),
      h('div', { className: 'card', style: { padding: 14 } },
        h('div', { style: { fontWeight: 700, fontSize: 15 } }, '🖨 ', p.label),
        h('div', { style: { fontSize: 13, color: 'var(--muted)', marginTop: 6 } },
          (Math.round((p.grams || 0) * 10) / 10) + ' г · ' + Math.round(p.minutes || 0) + ' мин' + (p.plastic ? ' · ' + p.plastic : '')),
        p.request_id ? h('div', { style: { fontSize: 12.5, color: 'var(--accent)', marginTop: 5 } }, 'Печать шла с привязкой к заказу #' + p.request_id) : null),
      h('button', { className: 'btn line', style: { marginTop: 12 }, disabled: busy, onClick: function () { pick('self', 0); } }, '🏠 Для себя (самопечать)'),
      h(Lab, null, 'Или выберите заказ'),
      (data.orders || []).length === 0 && h('p', { className: 'hint', style: { marginTop: 0 } }, 'Активных заказов нет.'),
      (data.orders || []).map(function (o) {
        return h('div', { key: o.id, className: 'order', style: { cursor: 'pointer', opacity: busy ? 0.6 : 1, borderColor: p.request_id === o.id ? 'var(--accent)' : undefined },
            onClick: function () { pick('order', o.id); } },
          h('div', { style: { display: 'flex', gap: 10, alignItems: 'center' } },
            o.preview
              ? h('img', { src: o.preview, alt: '', loading: 'lazy', style: { width: 44, height: 44, borderRadius: 9, objectFit: 'cover', flex: '0 0 auto', background: 'var(--bg-2)' } })
              : h('div', { style: { width: 44, height: 44, borderRadius: 9, flex: '0 0 auto', background: 'var(--surface-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' } }, icon('box', { size: 18 })),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('div', null, h('span', { className: 'id' }, '#', o.id), ' ', h('b', { style: { fontSize: 14 } }, o.label)),
              h('div', { style: { marginTop: 5, fontSize: 12.5, color: 'var(--muted)', display: 'flex', gap: 8, flexWrap: 'wrap' } },
                h('span', null, o.status_label), h('span', null, o.user), h('span', null, o.qty + ' шт')))));
      }));
  }

  // ───────────────────── ДОСТАВКА: ТИПЫ И ТАРИФЫ (A2) ─────────────────────
  // Прайс доставки: строка = тип (цена, порог бесплатной доставки, срок). Клиент выбирает
  // тип на оформлении или задаёт его один раз в профиле; стоимость считает сервер и кладёт
  // её в итог заказа. Ни одного типа — у клиента вообще нет выбора доставки (самовывоз).
  function AdminDelivery(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var fs = useState({ name: '', price: '', free_from: '', eta: '' }); var f = fs[0], setF = fs[1];
    function load() { API.adminDelivery().then(function (j) { if (j.ok) setData(j); else app.toast(j.error || 'Ошибка'); }); }
    useEffect(load, []);
    function save(body) {
      API.adminDeliveryType(body).then(function (j) { if (j.ok) load(); else app.toast(j.error || 'Ошибка'); });
    }
    function add() {
      if (!f.name.trim()) { app.toast('Название типа'); return; }
      save({ action: 'add', name: f.name, price: f.price || 0, free_from: f.free_from || 0, eta: f.eta });
      setF({ name: '', price: '', free_from: '', eta: '' });
    }
    if (data === null) return loading('Доставка');
    var cur = data.currency || 'руб.';
    var types = data.types || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Доставка'),
      h('p', { className: 'hint', style: { marginTop: 0 } },
        'Типы доставки и тарифы: «курьером по городу», «почтой», «до пункта выдачи». Клиент выбирает тип в корзине или задаёт его в профиле — стоимость входит в итог заказа и в счёт на оплату. Ни одного типа — у клиента не будет ни выбора доставки, ни адреса: только самовывоз.'),
      h('div', { className: 'card', style: { marginTop: 10, padding: 12 } },
        inp({ placeholder: 'Название типа (например, «Курьером по городу»)', value: f.name, onChange: function (e) { setF(Object.assign({}, f, { name: e.target.value })); } }),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 9 } },
          inp({ className: 'input mono', style: { flex: 1, minWidth: 0 }, placeholder: 'Цена', value: f.price, onChange: function (e) { setF(Object.assign({}, f, { price: e.target.value })); } }),
          inp({ className: 'input mono', style: { flex: 1, minWidth: 0 }, placeholder: 'Бесплатно от', value: f.free_from, onChange: function (e) { setF(Object.assign({}, f, { free_from: e.target.value })); } }),
          inp({ style: { flex: 1, minWidth: 0 }, placeholder: 'Срок', value: f.eta, onChange: function (e) { setF(Object.assign({}, f, { eta: e.target.value })); } })),
        h('p', { className: 'hint', style: { marginTop: 6 } }, '«Бесплатно от» = сумма позиций, с которой доставка бесплатна. 0 — порога нет.'),
        h('button', { className: 'btn soft', style: { marginTop: 6 }, onClick: add }, icon('plus', { size: 16 }), ' Добавить тип')),
      types.length === 0 && h('p', { className: 'hint' }, 'Типов доставки пока нет — клиент видит только самовывоз.'),
      types.map(function (t) {
        return h('div', { key: t.id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px', opacity: t.is_active ? 1 : 0.5 } },
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontSize: 13.5, fontWeight: 600 } }, t.name),
            h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } },
              money(t.price, cur) + (t.free_from > 0 ? ' · бесплатно от ' + money(t.free_from, cur) : '') + (t.eta ? ' · ' + t.eta : ''))),
          iconBtn(t.is_active ? 'check' : 'close', function () { save({ action: 'update', id: t.id, is_active: !t.is_active }); },
            { on: !!t.is_active, title: t.is_active ? 'Включён' : 'Выключен' }),
          iconBtn('trash', function () { save({ action: 'del', id: t.id }); }, { danger: true, title: 'Удалить' }));
      }));
  }

  // ───────────────────── КОМАНДА: СОТРУДНИКИ И РОЛИ (A4) ─────────────────────
  // Владелец приглашает сотрудника по Telegram ID и выбирает роль. Оператор видит заказы и
  // принтер, но не цены, деньги, клиентскую базу и настройки. Владельцы из .env (root) здесь
  // только показаны — понизить или удалить их из приложения нельзя.
  function AdminStaff(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var fs = useState({ user_id: '', name: '', role: 'operator' }); var f = fs[0], setF = fs[1];
    function load() { API.adminStaff().then(function (j) { if (j.ok) setData(j); else app.toast(j.error || 'Ошибка'); }); }
    useEffect(load, []);
    function save(body) {
      API.adminStaffSave(body).then(function (j) { if (j.ok) { load(); app.toast('Готово'); } else app.toast(j.error || 'Ошибка'); });
    }
    function add() {
      if (!String(f.user_id).trim()) { app.toast('Нужен Telegram ID'); return; }
      save({ action: 'save', user_id: f.user_id, name: f.name, role: f.role });
      setF({ user_id: '', name: '', role: 'operator' });
    }
    if (data === null) return loading('Команда');
    var rolesList = data.roles || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(load) }, 'Команда'),
      h('p', { className: 'hint', style: { marginTop: 0 } },
        'Кто и что может в админке. Telegram ID сотрудника видно в карточке клиента — человек должен хотя бы раз написать боту.'),
      rolesList.map(function (r) {
        return h('div', { key: r.id, className: 'card', style: { marginTop: 8, padding: '9px 12px' } },
          h('b', { style: { fontSize: 13 } }, r.label),
          h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 3 } }, r.hint));
      }),
      h('div', { className: 'card', style: { marginTop: 12, padding: 12 } },
        h('div', { style: { display: 'flex', gap: 8 } },
          inp({ className: 'input mono', style: { flex: '0 0 120px' }, placeholder: 'Telegram ID', value: f.user_id, onChange: function (e) { setF(Object.assign({}, f, { user_id: e.target.value })); } }),
          inp({ style: { flex: 1, minWidth: 0 }, placeholder: 'Имя', value: f.name, onChange: function (e) { setF(Object.assign({}, f, { name: e.target.value })); } })),
        h('select', { className: 'input', style: { marginTop: 9 }, value: f.role, onChange: function (e) { setF(Object.assign({}, f, { role: e.target.value })); } },
          rolesList.map(function (r) { return h('option', { key: r.id, value: r.id }, r.label); })),
        h('button', { className: 'btn soft', style: { marginTop: 9 }, onClick: add }, icon('plus', { size: 16 }), ' Добавить в команду')),
      (data.staff || []).map(function (s) {
        return h('div', { key: s.user_id, className: 'order', style: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px' } },
          h('div', { style: { flex: 1, minWidth: 0 } },
            h('div', { style: { fontSize: 13.5, fontWeight: 600 } }, s.name || ('id ' + s.user_id)),
            h('div', { style: { fontSize: 11.5, color: 'var(--faint)', marginTop: 2 } },
              s.role_label + (s.root ? ' · из настроек сервера' : '') + ' · id ' + s.user_id)),
          !s.root && h('select', { className: 'input', style: { flex: '0 0 128px', height: 34, padding: '0 8px' }, value: s.role,
            onChange: function (e) { save({ action: 'save', user_id: s.user_id, name: s.name, role: e.target.value }); } },
            rolesList.map(function (r) { return h('option', { key: r.id, value: r.id }, r.label); })),
          !s.root && iconBtn('trash', function () { save({ action: 'del', user_id: s.user_id }); }, { danger: true, title: 'Убрать из команды' }));
      }));
  }

  // ───────────────────── ADMIN · ОШИБКИ (телеметрия, f4de8627) ─────────────────────
  // Всё, что упало: веб-API, хендлеры бота, фоновые циклы, фронт мини-аппа. Одинаковые сбои
  // схлопнуты по отпечатку (×N), «прочитано» убирает запись из новых — вернувшийся после
  // разбора сбой заведёт новую строку и новый алерт.
  var ERR_SRC = { web: 'веб-API', tg: 'бот', log: 'лог', js: 'мини-апп', task: 'фон', backup: 'бэкап' };
  var ERR_COLOR = { web: '#3a86ff', tg: '#2dd47e', log: '#ffb020', js: '#b06bff', task: '#7a8a99', backup: '#7a8a99' };

  function AdminErrors(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var os_ = useState(true); var onlyNew = os_[0], setOnlyNew = os_[1];
    var xs = useState(0); var openId = xs[0], setOpenId = xs[1];   // раскрытый трейсбек

    function load(nw) {
      API.adminErrors(nw === undefined ? onlyNew : nw).then(function (j) { setData(j || { ok: false }); });
    }
    useEffect(function () { load(); }, []);
    function seen(id) {
      API.adminErrorsSeen(id).then(function (j) {
        if (j && j.ok) { app.toast(id ? 'Отмечено прочитанным' : 'Все отмечены прочитанными'); load(); }
        else app.toast((j && j.error) || 'Не удалось');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function toggleNew(v) { setOnlyNew(v); setData(null); load(v); }

    // refresh() отдаёт onClick напрямую — без обёртки в load() прилетел бы объект события
    function reload() { load(); }

    if (!data) return loading('Ошибки');
    if (!data.ok) return h('div', { className: 'screen' }, h(Lab, { right: refresh(reload) }, 'Ошибки'),
      h('div', { className: 'empty' }, icon('info', { size: 46 }), h('div', null, data.error || 'Телеметрия недоступна')));

    var items = data.errors || [];
    var nnew = data.new_count || 0;
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(reload) }, 'Ошибки'),
      h('p', { className: 'hint', style: { marginTop: 0 } },
        nnew ? ('Непрочитанных сбоев: ' + nnew + '. Одинаковые схлопнуты — «×N» это сколько раз повторилось.')
          : 'Новых сбоев нет. Здесь копится всё, что упало: веб-API, бот, фоновые задачи и мини-апп.'),
      h('div', { style: { display: 'flex', gap: 8, marginBottom: 10 } },
        h('button', { className: 'chip' + (onlyNew ? ' on' : ''), onClick: function () { toggleNew(true); } }, 'Только новые'),
        h('button', { className: 'chip' + (onlyNew ? '' : ' on'), onClick: function () { toggleNew(false); } }, 'Все'),
        nnew > 0 && h('button', { className: 'chip', style: { marginLeft: 'auto' }, onClick: function () { seen(0); } },
          icon('check', { size: 14 }), ' Прочитать все')),
      items.length === 0 && h('div', { className: 'empty' }, icon('shield', { size: 46 }),
        h('div', null, onlyNew ? 'Новых ошибок нет' : 'Ошибок не записано')),
      items.map(function (e) {
        var open = openId === e.id;
        return h('div', { key: e.id, className: 'order', style: { padding: '11px 13px', opacity: e.seen ? 0.6 : 1 } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' } },
            h('span', { style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.3,
              padding: '2px 7px', borderRadius: 6, color: '#12100e',
              background: ERR_COLOR[e.source] || '#7a8a99' } }, ERR_SRC[e.source] || e.source),
            e.level && e.level !== 'error' && h('span', { style: { fontSize: 10.5, color: 'var(--faint)', textTransform: 'uppercase' } }, e.level),
            e.count > 1 && h('span', { style: { fontSize: 11.5, fontWeight: 700, color: 'var(--red)' } }, '×' + e.count),
            h('span', { style: { marginLeft: 'auto', fontSize: 11, color: 'var(--faint)' } }, e.last_at)),
          // тап по сообщению раскрывает трейсбек — целиком он в карточку не влезает
          h('div', { className: 'mono', style: { fontSize: 12.5, marginTop: 6, lineHeight: 1.4, cursor: e.traceback ? 'pointer' : 'default', wordBreak: 'break-word' },
            onClick: function () { if (e.traceback) setOpenId(open ? 0 : e.id); } }, e.message),
          e.context && h('div', { style: { fontSize: 11.5, color: 'var(--muted)', marginTop: 4 } }, e.context),
          open && h('pre', { className: 'mono', style: { marginTop: 8, padding: 10, borderRadius: 9, background: 'var(--surface-2)',
            border: '1px solid var(--line)', fontSize: 11, lineHeight: 1.45, overflowX: 'auto', whiteSpace: 'pre' } }, e.traceback),
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 9, marginTop: 8 } },
            e.count > 1 && h('span', { style: { fontSize: 11, color: 'var(--faint)' } }, 'впервые ' + e.first_at),
            e.traceback && h('button', { className: 'chip', style: { padding: '5px 10px', fontSize: 12, marginLeft: 'auto' },
              onClick: function () { setOpenId(open ? 0 : e.id); } }, open ? 'Скрыть трейсбек' : 'Трейсбек'),
            !e.seen && h('button', { className: 'chip', style: { padding: '5px 10px', fontSize: 12, marginLeft: e.traceback ? 0 : 'auto' },
              onClick: function () { seen(e.id); } }, icon('check', { size: 13 }), ' Прочитано')));
      }));
  }

  // ───────────────── ADMIN · БАЗА ЗНАНИЙ (c356387a) ─────────────────
  // Статьи ДЛЯ СЕБЯ и операторов: как настроить, откалибровать, что делать при типовой
  // проблеме. Клиенту мастерской они не показываются. Черновик (published=0) не виден
  // даже на самом экране базы знаний — можно спокойно писать в несколько заходов.
  var KB_BLANK = { id: 0, title: '', body: '', sort: 0, published: 0 };

  function AdminKb(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];
    var es = useState(null); var edit = es[0], setEdit = es[1];
    function load() { API.adminKb().then(function (j) { setData(j || { ok: false }); }); }
    useEffect(function () { load(); }, []);
    function reload() { load(); }

    function save() {
      var t = String(edit.title || '').trim();
      if (!t) { app.toast('Нужен заголовок'); return; }
      API.adminKbSave({ id: edit.id || 0, title: t, body: edit.body || '',
        sort: Number(edit.sort) || 0, published: edit.published ? 1 : 0 }).then(function (j) {
        if (j && j.ok) { app.toast('Сохранено'); setEdit(null); load(); }
        else app.toast((j && j.error) || 'Не сохранилось');
      }).catch(function () { app.toast('Сеть недоступна'); });
    }
    function del() {
      askConfirmish('Удалить статью «' + edit.title + '»?', function () {
        API.adminKbDelete(edit.id).then(function (j) {
          if (j && j.ok) { app.toast('Статья удалена'); setEdit(null); load(); }
          else app.toast((j && j.error) || 'Не удалось');
        }).catch(function () { app.toast('Сеть недоступна'); });
      });
    }
    function set(k, v) { setEdit(function (e) { var n = Object.assign({}, e); n[k] = v; return n; }); }

    if (!data) return loading('База знаний');
    if (!data.ok) return h('div', { className: 'screen' }, h(Lab, { right: refresh(reload) }, 'База знаний'),
      h('div', { className: 'empty' }, icon('info', { size: 46 }), h('div', null, data.error || 'Недоступно')));

    if (edit) {
      return h('div', { className: 'screen' },
        h(Lab, null, edit.id ? 'Правка статьи' : 'Новая статья'),
        h('div', { className: 'card' },
          field('Заголовок', inp({ value: edit.title, maxLength: 200, placeholder: 'Чем PLA отличается от PETG',
            onChange: function (e) { set('title', e.target.value); } })),
          field('Текст', h('textarea', { className: 'input', style: { minHeight: 200, resize: 'vertical', width: '100%' },
            placeholder: 'Абзацы разделяйте пустой строкой', value: edit.body,
            onChange: function (e) { set('body', e.target.value); } })),
          field('Порядок в списке', inp({ className: 'input mono', inputMode: 'numeric', value: String(edit.sort),
            onChange: function (e) { set('sort', e.target.value); } })),
          h('label', { style: { display: 'flex', alignItems: 'center', gap: 9, marginTop: 4, fontSize: 13.5 } },
            h('input', { type: 'checkbox', checked: !!edit.published,
              onChange: function (e) { set('published', e.target.checked ? 1 : 0); } }),
            'Готова (не черновик)'),
          h('div', { style: { display: 'flex', gap: 8, marginTop: 12 } },
            h('button', { className: 'btn', style: { marginTop: 0 }, onClick: save }, icon('check', { size: 17 }), ' Сохранить'),
            h('button', { className: 'btn line', style: { marginTop: 0 }, onClick: function () { setEdit(null); } }, 'Отмена')),
          edit.id ? h('button', { className: 'btn line', style: { marginTop: 9, color: 'var(--red)' }, onClick: del },
            icon('trash', { size: 16 }), ' Удалить статью') : null));
    }

    var arts = data.articles || [];
    return h('div', { className: 'screen' },
      h(Lab, { right: refresh(reload) }, 'База знаний'),
      h('p', { className: 'hint', style: { marginTop: 0 } },
        'Инструкции для себя и операторов: настройка, калибровка, типовые проблемы. Клиентам мастерской статьи не показываются.'),
      h('button', { className: 'btn', onClick: function () { setEdit(Object.assign({}, KB_BLANK)); } },
        icon('plus', { size: 17 }), ' Новая статья'),
      arts.length === 0 && h('div', { className: 'empty' }, icon('doc', { size: 46 }),
        h('div', null, 'Статей ещё нет')),
      h('div', { className: 'vlist', style: { marginTop: 11 } }, arts.map(function (a) {
        return h('button', { key: a.id, className: 'vrow', onClick: function () { setEdit(Object.assign({}, KB_BLANK, a)); } },
          h('div', { className: 'ic' }, icon('doc', { size: 18 })),
          h('div', { className: 'grow' }, h('div', { className: 'tt' }, a.title),
            h('div', { className: 'ss' }, 'порядок ' + a.sort + ' · изменена ' + (a.updated_at || '—'))),
          h('span', { className: 'stbadge ' + (a.published ? 'st-printed' : 'st-new'),
            style: { marginTop: 0, padding: '2px 8px', fontSize: 10.5 } }, a.published ? 'опубликовано' : 'черновик'));
      })));
  }

  Object.assign(window, { AdminClients: AdminClients, AdminClientCard: AdminClientCard, AdminTasks: AdminTasks,
    AdminErrors: AdminErrors,
    AdminKb: AdminKb,   // база знаний владельца студии (c356387a)
    AdminPrintHub: AdminPrintHub, AdminPricing: AdminPricing, AdminMasterLog: AdminMasterLog,
    AdminPlasticsHub: AdminPlasticsHub,
    AdminProducts: AdminProducts, AdminNotifs: AdminNotifs,
    AdminDelivery: AdminDelivery, AdminStaff: AdminStaff,
    AdminFamilies: AdminFamilies, AdminPrintClassify: AdminPrintClassify });
})();
