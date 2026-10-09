// screens_custom.js — параметрический кастомайзер (вариант C2) + секция «Создай своё» в Коллекции.
// Модель «Брелок номер авто»: номерной знак с синей полосой-евробандом слева. ТРИ цвета из реальных
// пластиков системы (основа / текст+рамка / полоса), все одного ТИПА пластика. Превью кастомайзера —
// настоящий 3D-рендер с сервера (3 цвета), пока считается — SVG-плейсхолдер. Табличка авто-длиной
// подгоняется под текст. Цена/вес/время — реальный серверный слайс прямо на экране.
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, API = window.API;

  var PXMM = 2.6;   // px на мм в превью

  var FONTS = {
    round:   { fam: "'Onest', system-ui, sans-serif", label: 'Округлый', gl: 'Aa' },
    classic: { fam: "Georgia, 'Times New Roman', serif", label: 'Класс.', gl: 'Aa' },
    mono:    { fam: "'JetBrains Mono', monospace", label: 'Моно', gl: 'Aa' },
  };
  var SHAPES = [['rect', 'Прямо'], ['rounded', 'Скругл'], ['pill', 'Овал']];

  function solidColor(css) {
    if (!css) return '#8a97a5';
    var m = String(css).match(/#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}\b/);
    if (!m) return '#8a97a5';
    var hex = m[0];
    if (hex.length === 4) hex = '#' + hex[1] + hex[1] + hex[2] + hex[2] + hex[3] + hex[3];
    return hex;
  }
  function rgbOf(hex) { var s = solidColor(hex).replace('#', ''); return [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)]; }
  function shade(hex, f) {
    var c = rgbOf(hex);
    function adj(v) { return f < 0 ? Math.round(v * (1 + f)) : Math.round(v + (255 - v) * f); }
    function to(v) { return Math.max(0, Math.min(255, v)).toString(16).padStart(2, '0'); }
    return '#' + to(adj(c[0])) + to(adj(c[1])) + to(adj(c[2]));
  }
  function lum(hex) { var c = rgbOf(hex); return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255; }
  function colorDist(h1, h2) { var a = rgbOf(h1), b = rgbOf(h2); var dr = a[0] - b[0], dg = a[1] - b[1], db = a[2] - b[2]; return dr * dr + dg * dg + db * db; }
  function nearest(list, targetHex) {
    var avail = (list || []).filter(function (e) { return !e.oos; });   // не предлагать дефолтом цвет «нет в наличии»
    var pool = avail.length ? avail : (list || []);
    var best = null, bd = Infinity;
    pool.forEach(function (e) { var d = colorDist(e.css, targetHex); if (d < bd) { bd = d; best = e; } });
    return best;
  }

  // Авто-длина таблички (мм) под текст — ДОЛЖНА совпадать с layout .scad (euro_w + текст + поля).
  function euroWidthMm(p) { var heightMm = Math.round((p.fontSize * 1.7 + 16) / PXMM); return Math.max(10, heightMm * 0.5); }
  // Грубая оценка длины ТОЛЬКО для SVG-плейсхолдера/оценки: точную длину считает сервер по реальной
  // ширине текста (autosize_keychain). Коэффициенты ≈ средняя ширина символа Liberation.
  function autoWidthMm(p) {
    var textSizeMm = p.fontSize / PXMM;
    var factor = p.font === 'mono' ? 0.62 : (p.font === 'classic' ? 0.78 : 0.80);
    var textW = Math.max(1, (p.text || '').length) * textSizeMm * factor;
    var len = euroWidthMm(p) + textW + 2 * 9;
    return Math.max(40, Math.min(230, Math.round(len)));
  }

  // ── Живой SVG-брелок: корпус p.color, текст+рамка p.color2, евробанд-полоса p.color3. ──
  function Keychain(props) {
    var p = props.p, max = props.max || 320;
    var body = solidColor(p.color);
    var ink = p.color2 ? solidColor(p.color2) : (lum(body) > 0.62 ? '#1b1410' : '#ffffff');
    var euro = (p.euro !== false && p.color3) ? solidColor(p.color3) : null;
    var pw = p.widthMm * PXMM;
    var ph = p.fontSize * 1.7 + 16;
    var depth = p.thickMm * PXMM;
    var rx = p.shape === 'rect' ? 6 : (p.shape === 'rounded' ? 22 : ph / 2);
    var pad = 18;
    var vbW = pw + pad * 2, vbH = ph + depth + pad * 2;
    var x0 = pad, y0 = pad;
    var dark = shade(body, -0.42);
    var edge = shade(body, -0.18);
    var fontFam = FONTS[p.font].fam;
    var euroW = euro ? Math.max(26, ph * 0.5) : 0;
    var holeR = ph * 0.17;
    var holeCx = euro ? (x0 + euroW * 0.5) : (x0 + ph * 0.6);
    var holeCy = y0 + ph * 0.5;
    var textStart = x0 + euroW + 11;
    var textRight = x0 + pw - 12;
    var textCx = (textStart + textRight) / 2;
    var availW = textRight - textStart;
    var natW = (p.text || '').length * p.fontSize * 0.62;
    var useLen = natW > availW;
    var uid = body.replace('#', '') + (euro ? euro.replace('#', '') : '');
    var gid = 'kc-top-' + uid, clipId = 'kc-clip-' + uid;
    return h('svg', { viewBox: '0 0 ' + vbW + ' ' + vbH, fontFamily: fontFam,
      style: { width: '100%', maxWidth: max, height: 'auto', display: 'block' } },
      h('defs', null,
        h('linearGradient', { id: gid, x1: '0', y1: '0', x2: '0', y2: '1' },
          h('stop', { offset: '0', stopColor: shade(body, 0.14) }),
          h('stop', { offset: '1', stopColor: body })),
        h('clipPath', { id: clipId }, h('rect', { x: x0, y: y0, width: pw, height: ph, rx: rx }))),
      // боковина (толщина)
      h('rect', { x: x0, y: y0 + depth, width: pw, height: ph, rx: rx, fill: dark }),
      // верхняя грань (корпус)
      h('rect', { x: x0, y: y0, width: pw, height: ph, rx: rx, fill: 'url(#' + gid + ')', stroke: edge, strokeWidth: '1' }),
      // евробанд — синяя полоса слева, обрезана по контуру таблички
      euro && h('rect', { x: x0, y: y0, width: euroW, height: ph, fill: euro, clipPath: 'url(#' + clipId + ')' }),
      // рамка-окантовка (краска)
      p.border && h('rect', { x: x0 + 7, y: y0 + 7, width: pw - 14, height: ph - 14, rx: Math.max(2, rx - 7),
        fill: 'none', stroke: ink, strokeOpacity: '0.82', strokeWidth: '2.4' }),
      // отверстие под кольцо
      p.ring && h('g', null,
        h('circle', { cx: holeCx, cy: holeCy, r: holeR + 3, fill: dark }),
        h('circle', { cx: holeCx, cy: holeCy, r: holeR, fill: '#0b0f14' })),
      // надпись (краска)
      h('text', { x: textCx, y: y0 + ph / 2, fill: ink, fontSize: p.fontSize, fontWeight: '700',
        textAnchor: 'middle', dominantBaseline: 'central',
        textLength: useLen ? availW : undefined, lengthAdjust: 'spacingAndGlyphs',
        style: { letterSpacing: p.font === 'mono' ? '1px' : '.3px' } }, p.text || ' '));
  }

  // Черновая оценка веса/времени/цены без слайса (точная — после серверного расчёта).
  function spec(p, ppk, dens) {
    ppk = ppk || 50; dens = dens || 1.24;
    var ph = p.fontSize * 1.7 + 16;
    var heightMm = Math.round(ph / PXMM);
    var vol = (p.widthMm / 10) * (heightMm / 10) * (p.thickMm / 10);   // см³
    var grams = vol * 0.6 * dens + (p.border ? 0.3 : 0);
    var minutes = grams * 3.4 + 7;
    var filament = grams * (ppk / 1000);
    var machine = (minutes / 60) * 0.516;
    var mat = filament + machine;
    var sub = mat + mat * 0.08 + 1.0;
    var tot = sub + sub * 0.30;
    var total = tot - (tot - filament) * 0.2;
    return { heightMm: heightMm, grams: grams, minutes: minutes, total: total };
  }

  // DEF: дефолты «номера авто» — рамка ВКЛ, синяя полоса слева, цвета бело-сине-чёрные.
  var DEF = { text: 'А123ВС', font: 'round', shape: 'rounded', widthMm: 80, fontSize: 30, thickMm: 3,
    color: '#e8edf2', color2: '#222a33', color3: '#2f6fd6', ring: true, border: true, euro: true };

  // Конструкторы. euro:false — без синей полосы (обычная бирка/метка, 2 цвета: корпус + текст).
  var CREATORS = [
    { id: 'plate', modelId: 'keychain_plate', label: 'Брелок номер авто', desc: 'Номерной знак с текстом', icon: 'tag',
      p: Object.assign({}, DEF) },
    { id: 'cable', modelId: 'keychain_plate', label: 'Метка на кабель', desc: 'Подпись для проводов', icon: 'tag',
      p: Object.assign({}, DEF, { text: 'HDMI', shape: 'pill', fontSize: 24, border: false, euro: false, color: '#222a33', color2: '#e8edf2', color3: '#e8edf2' }) },
    { id: 'pet', modelId: 'keychain_plate', label: 'Бирка для питомца', desc: 'Кличка и телефон', icon: 'tag',
      p: Object.assign({}, DEF, { text: 'РЕКС', shape: 'rounded', border: false, euro: false, color: '#ff7a1a', color2: '#1b1410', color3: '#1b1410' }) },
    { id: 'cloak', modelId: 'keychain_plate', label: 'Номерок в гардероб', desc: 'Номер для вещей', icon: 'doc',
      p: Object.assign({}, DEF, { text: '№ 17', font: 'classic', shape: 'rect', border: true, euro: false, color: '#e8edf2', color2: '#222a33', color3: '#222a33' }) },
  ];

  function creatorsFrom(app) {
    var fromApi = (app.collection || []).filter(function (m) { return m && m.kind === 'parametric'; })
      .map(function (m) {
        var d = (m.params && m.params.defaults) || {};
        return { id: m.id, modelId: m.parametric_id || m.model_id || m.id, label: m.label,
          desc: m.subtitle || 'Настраиваемая', icon: 'tag', p: Object.assign({}, DEF, d) };
      });
    return fromApi.length ? fromApi : CREATORS;
  }

  // ── каталог реальных цветов: плоский список {pid, pname, ptype, name, css, price, density, key} ──
  function colorCatalog(plastics) {
    var out = [];
    (plastics || []).forEach(function (pl) {
      var cols = pl.colors || [], css = pl.color_css || [];
      var oos = pl.out_of_stock || [], low = pl.low_stock || [];
      cols.forEach(function (nm, i) {
        out.push({ pid: pl.id, pname: pl.name, ptype: pl.type || pl.material || '', name: nm,
          css: css[i] || '#8a97a5', price: pl.price, density: pl.density, key: pl.id + '|' + nm,
          oos: oos.indexOf(nm) >= 0, low: low.indexOf(nm) >= 0 });
      });
    });
    return out;
  }
  // Дефолтные цвета номера: основа БЕЛАЯ (предпочт. PETG), текст ЧЁРНЫЙ, полоса СИНЯЯ — все одного типа.
  function plateColors(catalog, hasEuro) {
    if (!catalog.length) return { c1: null, c2: null, c3: null };
    var petg = catalog.filter(function (c) { return /petg/i.test(c.ptype); });
    var c1 = nearest(petg.length ? petg : catalog, '#e8edf2');
    var same = catalog.filter(function (c) { return c.ptype === c1.ptype; });
    return { c1: c1, c2: nearest(same, '#15181d') || c1,
      c3: hasEuro === false ? null : (nearest(same, '#2f6fd6') || c1) };
  }
  function grouped(opts) {
    var by = {}, order = [];
    opts.forEach(function (c) {
      if (!by[c.pid]) { by[c.pid] = { pid: c.pid, pname: c.pname, ptype: c.ptype, items: [] }; order.push(c.pid); }
      by[c.pid].items.push(c);
    });
    return order.map(function (id) { return by[id]; });
  }

  // ── Сохранённые таблички пользователя (idea #7): localStorage, до 12 шт ──
  var PRESETS_KEY = 'pcb_presets_v1';
  function loadPresets() { try { return JSON.parse(localStorage.getItem(PRESETS_KEY) || '[]') || []; } catch (e) { return []; } }
  function savePresets(list) { try { localStorage.setItem(PRESETS_KEY, JSON.stringify(list.slice(0, 12))); } catch (e) {} }
  function resolveSaved(pr, catalog) {
    var byKey = {}; (catalog || []).forEach(function (c) { byKey[c.key] = c; });
    var d = plateColors(catalog), sc = (pr && pr.colors) || {};
    return { c1: byKey[sc.c1] || d.c1, c2: byKey[sc.c2] || d.c2, c3: byKey[sc.c3] || d.c3 };
  }

  // ── модальный лист выбора цвета ──
  function ColorSheet(props) {
    return h('div', { className: 'csheet-ov', onClick: props.onClose },
      h('div', { className: 'csheet', onClick: function (e) { e.stopPropagation(); } },
        h('div', { className: 'csh-h' },
          h('div', { className: 'csh-t' }, props.title),
          h('button', { className: 'csh-x', type: 'button', onClick: props.onClose, 'aria-label': 'Закрыть' }, icon('close', { size: 18 }))),
        grouped(props.options).map(function (g) {
          return h('div', { key: g.pid, className: 'cgrp' },
            h('div', { className: 'cgrp-h' }, g.pname, g.ptype && h('span', { className: 'tp' }, g.ptype)),
            h('div', { className: 'cgrid' }, g.items.map(function (c) {
              return h('button', { key: c.key, type: 'button', disabled: c.oos,
                className: 'copt' + (props.selectedKey === c.key ? ' on' : '') + (c.oos ? ' oos' : ''),
                onClick: c.oos ? undefined : function () { props.onPick(c); },
                title: c.name + (c.oos ? ' — нет в наличии' : (c.low ? ' — мало' : '')) },
                h('span', { className: 'sw3', style: { background: c.css } }),
                h('span', { className: 'nm' }, c.name),
                c.oos ? h('span', { className: 'oostag' }, 'нет') : (c.low ? h('span', { className: 'oostag low' }, 'мало') : null));
            })));
        })));
  }

  // Декоративное мини-превью QR для карточки конструктора (не настоящий код — иллюстрация).
  function qrMotif() {
    var N = 7, s = 11, gap = 2, sz = N * s;
    var on = [[0,0],[0,1],[0,2],[1,0],[1,2],[2,0],[2,1],[2,2],
      [0,4],[0,5],[0,6],[1,4],[1,6],[2,4],[2,5],[2,6],
      [4,0],[4,1],[4,2],[5,0],[5,2],[6,0],[6,1],[6,2],
      [4,4],[4,6],[5,5],[6,4],[6,6],[3,3],[3,5],[5,3],[4,5]];
    return h('svg', { viewBox: '0 0 ' + sz + ' ' + sz, width: 92, height: 92, 'aria-hidden': 'true' },
      h('rect', { x: 0, y: 0, width: sz, height: sz, rx: 6, fill: 'var(--card2, #f2f5f8)' }),
      on.map(function (rc, i) {
        return h('rect', { key: i, x: rc[1] * s + 1, y: rc[0] * s + 1, width: s - gap, height: s - gap,
          rx: 1.5, fill: 'var(--ink, #1a2129)' });
      }));
  }

  // ───────────────── Секция «Создай своё» (вкладка Коллекции) ─────────────────
  function CreatorSection(props) {
    var app = props.app;
    var list = creatorsFrom(app);
    if (!list.length) return null;
    var feat = list[0];
    var fp = Object.assign({}, feat.p, { widthMm: autoWidthMm(feat.p) });   // карточка авто-подгоняется
    var catalog = colorCatalog((app.options && app.options.plastics) || []);
    var presets = loadPresets();
    return h(React.Fragment, null,
      h('div', { className: 'csec' },
        h('div', { className: 'clab' },
          h('span', { className: 'pill' }, 'Параметрические'),
          h('span', { className: 'r' }, 'настрой под себя')),
        h('div', { className: 'pgrid' },
          h('button', { className: 'pcard pfeat', type: 'button', onClick: function () { app.openCustomizer(feat); } },
            h('div', { className: 'pv' }, h(Keychain, { p: fp, max: 130 })),
            h('div', { className: 'cap' },
              h('div', { className: 'ptag' }, icon('sliders', { size: 12 }), ' Настраиваемая'),
              h('div', { className: 'n', style: { marginTop: 8 } }, feat.label),
              h('div', { className: 'sub' }, 'Свой текст, размер и цвета — табличка соберётся под вас.'),
              h('span', { className: 'go' }, 'Настроить ', icon('chev', { size: 14 })))),
          list.slice(1).map(function (c) {
            var cp = Object.assign({}, c.p, { widthMm: autoWidthMm(c.p) });
            return h('button', { key: c.id, className: 'pcard', type: 'button', onClick: function () { app.openCustomizer(c); } },
              h('div', { className: 'pv' }, h(Keychain, { p: cp, max: 150 })),
              h('div', { className: 'cap' },
                h('div', { className: 'n' }, c.label),
                h('div', { className: 'd' }, icon(c.icon, { size: 13, style: { color: 'var(--faint)' } }), ' ', c.desc)));
          }),
          // QR-брелок (idea #5): отдельный конструктор — открывает форму параметрической модели.
          h('button', { key: 'qr', className: 'pcard', type: 'button', onClick: function () { app.go('parametric'); } },
            h('div', { className: 'pv' }, qrMotif()),
            h('div', { className: 'cap' },
              h('div', { className: 'n' }, 'QR-брелок'),
              h('div', { className: 'd' }, icon('link', { size: 13, style: { color: 'var(--faint)' } }), ' Ссылка, Telegram, контакт'))))),
      // «Мои таблички» (idea #7): сохранённые конфиги — открыть/переоформить в один тап
      presets.length > 0 && h('div', { className: 'csec' },
        h('div', { className: 'clab' }, h('span', { className: 'pill' }, 'Мои таблички'),
          h('span', { className: 'r' }, presets.length + ' шт')),
        h('div', { className: 'pgrid' }, presets.map(function (pr) {
          var rc = resolveSaved(pr, catalog);
          var pp = Object.assign({}, DEF, pr.p, { color: rc.c1 && rc.c1.css, color2: rc.c2 && rc.c2.css, color3: rc.c3 && rc.c3.css });
          pp.widthMm = autoWidthMm(pp);
          return h('button', { key: pr.id, className: 'pcard', type: 'button',
            onClick: function () { app.openCustomizer({ id: 'saved:' + pr.id, modelId: 'keychain_plate', label: pr.name, savedColors: pr.colors, p: pr.p }); } },
            h('div', { className: 'pv' }, h(Keychain, { p: pp, max: 150 })),
            h('div', { className: 'cap' }, h('div', { className: 'n' }, pr.name)));
        }))));
  }

  // ───────────────────────── Кастомайзер ─────────────────────────
  function Customizer(props) {
    var app = props.app;
    var item = app.customModel || CREATORS[0];
    var hasEuro = !item.p || item.p.euro !== false;   // euro:false (метка/бирка) — без полосы и слота её цвета
    var plastics = (app.options && app.options.plastics) || [];
    var catalog = colorCatalog(plastics);
    function defs() { return Object.assign({}, DEF, item.p || {}); }
    function initColors() { return item.savedColors ? resolveSaved(item, catalog) : plateColors(catalog, hasEuro); }

    var ps = useState(defs); var p = ps[0], setP = ps[1];
    var cs = useState(initColors);
    var col = cs[0], setCol = cs[1];
    var pickS = useState(null); var pick = pickS[0], setPick = pickS[1];   // null | 'c1' | 'c2' | 'c3'
    var qS = useState({ loading: false }); var q = qS[0], setQ = qS[1];     // серверный расчёт цены (слайс)
    var reqRef = useRef(0);
    var lastRef = useRef({ token: null, geomSig: null });
    var rqS = useState({ loading: false }); var rq = rqS[0], setRq = rqS[1];   // серверный 3-цветный 3D-рендер
    var renderReqRef = useRef(0);

    var c1 = col.c1, c2 = col.c2, c3 = col.c3;
    var autoW = autoWidthMm(p);   // оценка длины для SVG-плейсхолдера; точную считает сервер
    // Геометрия (в .scad, без цвета и БЕЗ длины — Length сервер подгоняет под реальный текст).
    var geom = { text: p.text, fontSize: p.fontSize, thickMm: p.thickMm,
      font: p.font, shape: p.shape, ring: p.ring, border: p.border, euro: !!p.euro };
    var geomSig = JSON.stringify(geom);
    var fullSig = geomSig + '|' + (c1 ? c1.pid + ':' + c1.name : '-');
    var renderSig = geomSig + '|' + [c1, c2, c3].map(function (c) { return c ? c.css : ''; }).join('|');

    React.useEffect(function () {
      setP(defs()); setCol(initColors()); setPick(null);
      lastRef.current = { token: null, geomSig: null };
      reqRef.current++; renderReqRef.current++; setQ({ loading: false }); setRq({ loading: false });
    }, [item.id]);

    function multicolor() { return !!(c1 && ((c2 && c2.key !== c1.key) || (c3 && c3.key !== c1.key))); }
    // число РАЗНЫХ цветов (пластик|цвет) среди задействованных слотов → сервер берёт его в extra_colors
    function colorCount() { return new Set([c1, c2, c3].filter(Boolean).map(function (c) { return c.key; })).size || 1; }

    function runQuote() {
      var reqId = ++reqRef.current;
      var thisGeom = geomSig, thisFull = fullSig;
      setQ(function (prev) { return Object.assign({}, prev, { loading: true, error: null }); });
      var needGen = thisGeom !== lastRef.current.geomSig || !lastRef.current.token;
      var pr = needGen
        ? app.parametricQuote(item.modelId || item.id, geom, { plastic_id: c1.pid, color: c1.name, multicolor: multicolor(), colors: colorCount(), qty: 1 })
        : app.parametricRequote(lastRef.current.token, c1.pid, c1.name, 1);
      pr.then(function (r) {
        if (reqRef.current !== reqId) return;
        if (!r || !r.ok) { setQ(function (prev) { return Object.assign({}, prev, { loading: false, error: (r && r.error) || 'Ошибка расчёта' }); }); return; }
        var token = needGen ? r.token : lastRef.current.token;
        lastRef.current = { token: token, geomSig: thisGeom };
        setQ(function (prev) {
          var base = needGen
            ? { token: r.token, preview: r.preview, dims: r.dims, grams: r.grams, time: r.time }
            : { token: prev.token, preview: r.preview || prev.preview, dims: prev.dims,
                grams: r.grams || prev.grams, time: r.time || prev.time };
          return Object.assign({}, base, { loading: false, error: null,
            priceStr: r.priceStr, totalBefore: r.totalBefore, discountPct: r.discountPct,
            sig: thisFull, geomSig: thisGeom });
        });
      });
    }
    React.useEffect(function () {
      if (!c1) return;
      var t = setTimeout(runQuote, 550);
      return function () { clearTimeout(t); };
    }, [fullSig, item.id]);

    function runRender() {
      var reqId = ++renderReqRef.current;
      var thisSig = renderSig;
      setRq(function (prev) { return Object.assign({}, prev, { loading: true, error: null }); });
      app.parametricRender(item.modelId || item.id, geom,
        c1 ? solidColor(c1.css) : null, c2 ? solidColor(c2.css) : null, c3 ? solidColor(c3.css) : null).then(function (r) {
        if (renderReqRef.current !== reqId) return;
        if (!r || !r.ok || !r.image) { setRq(function (prev) { return Object.assign({}, prev, { loading: false, error: r && r.error }); }); return; }
        setRq({ loading: false, error: null, image: r.image, sig: thisSig });
      });
    }
    React.useEffect(function () {
      if (!c1) return;
      var t = setTimeout(runRender, 450);
      return function () { clearTimeout(t); };
    }, [renderSig, item.id]);

    function set(patch) { setP(function (prev) { return Object.assign({}, prev, patch); }); }
    function setK(k, v) { var o = {}; o[k] = v; set(o); }
    function reset() { setP(defs()); setCol(initColors()); }
    function savePreset() {
      var list = loadPresets();
      var preset = { id: String(Date.now()), name: (p.text || 'Табличка'),
        p: { text: p.text, font: p.font, shape: p.shape, fontSize: p.fontSize, thickMm: p.thickMm, ring: p.ring, border: p.border },
        colors: { c1: c1 && c1.key, c2: c2 && c2.key, c3: c3 && c3.key } };
      savePresets([preset].concat(list.filter(function (x) { return x.name !== preset.name; })));
      app.toast('Сохранено в «Мои таблички»');
    }

    function pickColor(slot, c) {
      if (slot === 'c1') {
        setCol(function (prev) {
          if (prev.c1 && c.ptype === prev.c1.ptype) return { c1: c, c2: prev.c2, c3: prev.c3 };   // тип тот же
          var same = catalog.filter(function (x) { return x.ptype === c.ptype; });                 // тип сменился — пересобрать
          // цвета текста/полосы пересобираются молча — говорим об этом (аудит UX)
          if (app && app.toast) app.toast('Цвета текста и полосы подобраны под новый пластик');
          return { c1: c, c2: nearest(same, '#15181d') || c,
            c3: hasEuro === false ? null : (nearest(same, '#2f6fd6') || c) };
        });
      } else {
        setCol(function (prev) { var n = Object.assign({}, prev); n[slot] = c; return n; });
      }
      setPick(null);
    }

    // c2/c3 — только из пластиков того же ТИПА, что основа
    var sameType = c1 ? catalog.filter(function (c) { return c.ptype === c1.ptype; }) : [];
    var ppk = c1 && c1.price != null ? Number(c1.price) : 50;
    var dens = c1 && c1.density != null ? Number(c1.density) : 1.24;
    var ep = Object.assign({}, p, { widthMm: autoW });
    var s = spec(ep, ppk, dens);
    var kp = Object.assign({}, ep, { color: c1 ? c1.css : p.color, color2: c2 ? c2.css : p.color2, color3: c3 ? c3.css : p.color3 });
    var realReady = !!(q.priceStr && q.sig === fullSig && !q.error);
    var ready = realReady && !!q.token && !q.loading;
    var showRender = !!(rq.image && rq.sig === renderSig && !rq.error);
    var orderLabel = item.label + ' «' + (p.text || '') + '»';
    var comment = c1 ? (item.label + ' «' + p.text + '»: основа — ' + c1.name
      + (c2 ? (', текст/рамка — ' + c2.name) : '') + (p.euro && c3 ? (', полоса — ' + c3.name) : '')) : '';

    function sliderRow(label, k, min, mx, step) {
      return h('div', { className: 'crow' }, h('div', { className: 'ck' }, label),
        h('div', { className: 'cc' },
          h('input', { className: 'rng', type: 'range', min: min, max: mx, step: step, value: p[k],
            onChange: function (e) { setK(k, +e.target.value); } }),
          h('span', { className: 'mini' }, p[k])));
    }
    function segRow(label, k, opts) {
      return h('div', { className: 'crow' }, h('div', { className: 'ck' }, label),
        h('div', { className: 'cc' },
          h('div', { className: 'seg' }, opts.map(function (o) {
            var val = o[0], lab = o[1], gl = o[2], fam = o[3];
            return h('button', { key: val, type: 'button', className: p[k] === val ? 'on' : '',
              onClick: function () { setK(k, val); } },
              gl && h('span', { className: 'gl', style: { fontFamily: fam } }, gl), lab);
          }))));
    }
    function colorRow(label, sel, slot, disabled) {
      return h('div', { className: 'crow' }, h('div', { className: 'ck' }, label),
        h('div', { className: 'cc' },
          h('button', { className: 'colorbtn' + (disabled ? ' off' : ''), type: 'button', disabled: disabled,
            onClick: disabled ? undefined : function () { setPick(slot); } },
            h('span', { className: 'sw2', style: sel ? { background: sel.css } : { borderStyle: 'dashed', background: 'transparent' } }),
            h('span', { className: 'lbl' }, sel ? sel.name : 'Нет цвета', sel && h('em', null, ' · ', sel.pname)),
            !disabled && icon('chev', { size: 16, style: { color: 'var(--faint)', marginLeft: 'auto', flex: '0 0 auto' } }))));
    }
    function toggleRow(k, ic, t, sub) {
      return h('div', { className: 'tgl' },
        h('span', { className: 'ic' }, icon(ic, { size: 17 })),
        h('div', { className: 'meta' }, h('div', { className: 't' }, t), h('div', { className: 's' }, sub)),
        h('div', { className: 'sw' + (p[k] ? ' on' : ''), role: 'switch', 'aria-checked': !!p[k],
          onClick: function () { setK(k, !p[k]); } }, h('i', null)));
    }
    function specCell(label, val, money) {
      return h('div', { className: 'sx' }, h('div', { className: 'k' }, label),
        h('div', { className: 'v' + (money ? ' money' : '') }, val));
    }

    var fontOpts = Object.keys(FONTS).map(function (k) { return [k, FONTS[k].label, FONTS[k].gl, FONTS[k].fam]; });
    var noColors = !catalog.length;

    return h('div', { className: 'screen czscreen' },
      h('div', { className: 'cz-head' },
        h('div', { className: 'ttl' },
          h('div', { className: 't' }, item.label),
          h('div', { className: 's' }, icon('sliders', { size: 12 }), ' Параметрическая модель')),
        h('div', { className: 'sp' }),
        h('button', { className: 'ib', type: 'button', onClick: reset, 'aria-label': 'Сбросить' }, icon('refresh', { size: 18 }))),

      h('div', { className: 'czstage' },
        (q.loading || rq.loading)
          ? h('span', { className: 'live calc' }, h('span', { className: 'spin' }), ' Считаю…')
          : (realReady ? h('span', { className: 'live' }, h('span', { className: 'dot' }), ' Live')
            : h('span', { className: 'live est' }, '≈ оценка')),
        h('div', { className: 'canvaswrap' + ((q.loading || rq.loading) ? ' busy' : '') },
          showRender ? h('img', { className: 'czimg', src: rq.image, alt: '' }) : h(Keychain, { p: kp, max: 270 })),
        h('div', { className: 'czspec' },
          specCell('Размер', realReady ? q.dims : (autoW + '×' + s.heightMm + '×' + p.thickMm)),
          specCell('Вес', realReady ? (q.grams + ' г') : ('≈ ' + s.grams.toFixed(1) + ' г')),
          specCell('Время', realReady ? q.time : ('≈ ' + API.human(Math.round(s.minutes)))),
          specCell('Цена', realReady ? q.priceStr : ('≈ ' + API.fmt(s.total)), true))),

      h('div', { className: 'cz-body' },
        h('div', { className: 'fld' },
          // лейбл + живой счётчик лимита (аудит UX: лимит 14 был невидим пользователю)
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', margin: '0 2px 5px' } },
            h('span', { style: { fontSize: 11.5, fontWeight: 700, letterSpacing: '.6px', textTransform: 'uppercase', color: 'var(--faint)' } }, 'Текст на табличке'),
            h('span', { className: 'mono', style: { fontSize: 11, color: (p.text || '').length >= 14 ? 'var(--amber)' : 'var(--faint)' } }, (p.text || '').length + '/14')),
          h('input', { className: 'txtin', value: p.text, maxLength: 14, placeholder: 'А123ВС',
            onChange: function (e) { set({ text: (e.target.value || '').toUpperCase() }); } })),
        h('div', { style: { marginTop: 6 } },
          sliderRow('Размер', 'fontSize', 18, 46, 1),
          sliderRow('Толщина', 'thickMm', 2, 6, 0.5),
          segRow('Шрифт', 'font', fontOpts),
          segRow('Форма', 'shape', SHAPES),
          colorRow('Основа', c1, 'c1', noColors),
          colorRow('Текст и рамка', c2, 'c2', noColors || sameType.length < 1),
          p.euro && colorRow('Полоса слева', c3, 'c3', noColors || sameType.length < 1)),
        h('p', { className: 'hint', style: { marginTop: 8 } },
          'Длина подгоняется под текст. ' + (p.euro ? 'Три цвета' : 'Два цвета') + ' печатаются одним типом пластика'
          + (c1 ? (' (' + c1.ptype + ')') : '') + '.'),
        h('div', { style: { marginTop: 8 } },
          toggleRow('ring', 'spool', 'Кольцо для ключей', 'Отверстие под карабин'),
          toggleRow('border', 'box', 'Рамка по контуру', 'Чёрная окантовка как у номера')),
        h('button', { className: 'btn line', type: 'button', style: { marginTop: 12 }, onClick: savePreset },
          icon('check', { size: 17 }), ' Сохранить эту табличку')),

      h('div', { className: 'czcta' },
        q.error && h('div', { className: 'cz-err' }, icon('info', { size: 14 }), ' ', q.error, ' · ',
          h('button', { className: 'cz-retry', type: 'button', onClick: runQuote }, 'повторить')),
        h('div', { className: 'czrow' },
          h('button', { className: 'cz-cart', type: 'button', disabled: !ready || app.busy, 'aria-label': 'В корзину',
            onClick: function () { app.addCartItem({ token: q.token, label: orderLabel, preview: q.preview, totalStr: q.priceStr,
              pid: col.c1 && col.c1.pid, plastic: (col.c1 && col.c1.pname) || '', color: (col.c1 && col.c1.name) || '', dims: q.dims || '' }); } },
            icon('cart', { size: 19 })),
          h('button', { className: 'cz-order', type: 'button', disabled: !ready || app.busy,
            onClick: function () { app.orderToken(q.token, comment); } },
            q.loading ? 'Рассчитываю…' : 'Оставить заказ',
            h('span', { className: 'right' }, (ready ? q.priceStr : ('≈ ' + API.fmt(s.total))), ' ', icon('chev', { size: 18 }))))),

      pick === 'c1' && h(ColorSheet, { title: 'Цвет основы', options: catalog,
        selectedKey: c1 && c1.key, onPick: function (c) { pickColor('c1', c); }, onClose: function () { setPick(null); } }),
      pick === 'c2' && h(ColorSheet, { title: 'Цвет текста и рамки' + (c1 ? ' (' + c1.ptype + ')' : ''), options: sameType,
        selectedKey: c2 && c2.key, onPick: function (c) { pickColor('c2', c); }, onClose: function () { setPick(null); } }),
      pick === 'c3' && h(ColorSheet, { title: 'Цвет полосы слева' + (c1 ? ' (' + c1.ptype + ')' : ''), options: sameType,
        selectedKey: c3 && c3.key, onPick: function (c) { pickColor('c3', c); }, onClose: function () { setPick(null); } }));
  }

  Object.assign(window, { Customizer: Customizer, PS_CreatorSection: CreatorSection, PS_Keychain: Keychain });
})();
