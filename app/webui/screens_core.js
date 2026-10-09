// screens_core.js — Hub, ModelInput, Config, Slicing, Quote (no-Babel React)
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon;

  function Lab(props) {
    return h('div', { className: 'lab' }, props.children, props.right && h('span', { className: 'r' }, props.right));
  }

  function money(n, cur) { return (Number(n) || 0).toFixed(2).replace('.', ',') + ' ' + (cur || 'руб.'); }

  // ─────────────── Анимация ожидания «принтер печатает Benchy» (OpenTodo b690ef24) ───────────────
  // Чистый SVG+CSS (кейфреймы в app.css): каретка ездит по X, портал поднимается по Z дискретными
  // «слоями» (steps), модель проявляется снизу вверх синхронно через clip-path:inset на .plmodel
  // (работает и на полупрозрачном оверлее). Без JS-таймеров — не будит рендер.
  function PrinterLoader(props) {
    var cls = 'ploader' + (props.size === 'sm' ? ' sm' : '');
    return h('svg', { className: cls, viewBox: '0 0 132 112', 'aria-hidden': true },
      h('defs', null,
        // Упрощённый Benchy правым бортом: корпус, рубка с круглым окном (evenodd), труба.
        h('path', { id: 'plb', fillRule: 'evenodd', d:
          'M38 96 L42 79 L86 79 Q93 80 94 96 Z ' +
          'M46 79 L46 64 Q46 62 48 62 L66 62 Q68 62 68 64 L68 79 Z ' +
          'M72 79 L72 68 L78 68 L78 79 Z ' +
          'M57 66.5 a4 4 0 1 0 0.001 0 Z' }),
        h('clipPath', { id: 'plbc' }, h('use', { href: '#plb' }))),
      // рама: стойки, верхняя балка, стол
      h('rect', { x: 10, y: 10, width: 112, height: 6, rx: 3, fill: 'rgba(255,255,255,.10)' }),
      h('rect', { x: 10, y: 14, width: 5, height: 84, rx: 2, fill: 'rgba(255,255,255,.10)' }),
      h('rect', { x: 117, y: 14, width: 5, height: 84, rx: 2, fill: 'rgba(255,255,255,.10)' }),
      h('rect', { x: 20, y: 96, width: 92, height: 5, rx: 2.5, fill: 'rgba(255,255,255,.14)' }),
      h('rect', { x: 28, y: 101, width: 10, height: 4, rx: 1.5, fill: 'rgba(255,255,255,.08)' }),
      h('rect', { x: 94, y: 101, width: 10, height: 4, rx: 1.5, fill: 'rgba(255,255,255,.08)' }),
      // модель: заливка + «слои» (штриховка, обрезанная по силуэту); проявляется снизу вверх
      h('g', { className: 'plmodel' },
        h('use', { href: '#plb', fill: 'var(--accent)', opacity: .92 }),
        h('g', { clipPath: 'url(#plbc)', stroke: 'rgba(0,0,0,.30)', strokeWidth: 1 },
          [62, 65.5, 69, 72.5, 76, 79.5, 83, 86.5, 90, 93.5].map(function (y) {
            return h('line', { key: y, x1: 30, y1: y, x2: 102, y2: y });
          }))),
      // портал: поперечина + каретка с соплом (поднимается слоями, ездит по X)
      h('g', { className: 'plz' },
        h('rect', { x: 15, y: 40, width: 102, height: 5, rx: 2.5, fill: 'rgba(255,255,255,.16)' }),
        h('g', { className: 'plx' },
          h('rect', { x: 24, y: 36, width: 16, height: 13, rx: 3, fill: 'rgba(255,255,255,.22)' }),
          h('path', { d: 'M28 49 L36 49 L32 54 Z', fill: 'var(--accent)' }),
          h('circle', { className: 'plhot', cx: 32, cy: 53, r: 2.2, fill: 'var(--accent)' }))),
      // катушка филамента на левой стойке (вращается)
      h('g', { className: 'plspool', stroke: 'rgba(255,122,26,.55)', strokeWidth: 1.4, fill: 'none', strokeLinecap: 'round' },
        h('circle', { cx: 24, cy: 24, r: 8 }),
        h('circle', { cx: 24, cy: 24, r: 2.6 }),
        h('path', { d: 'M24 16v5.4M24 26.6V32M16 24h5.4M26.6 24H32' })));
  }

  // ── Семейство анимаций ожидания (OpenTodo 5f6de0a7): каждой ситуации — своя ──
  // Загрузка модели: «облако → устройство» (модель скачивается из интернета на ПК бота).
  function CloudLoader() {
    return h('svg', { className: 'cloudl', viewBox: '0 0 132 112', 'aria-hidden': true },
      // облако — органичный блоб (морфит масштабом в CSS)
      h('g', { className: 'cl-cloud' },
        h('path', { fill: 'rgba(255,255,255,.16)', d:
          'M38 44c-9 0-16-7-15-15 1-9 9-13 16-12 3-9 13-14 22-11 8 2 12 8 13 14 8-2 17 2 18 11 1 8-6 13-14 13z' }),
        h('circle', { className: 'cl-puff', cx: 36, cy: 30, r: 7, fill: 'rgba(255,255,255,.10)' }),
        h('circle', { className: 'cl-puff2', cx: 84, cy: 26, r: 6, fill: 'rgba(255,255,255,.10)' })),
      // устройство (телефон/ПК) внизу
      h('rect', { x: 48, y: 78, width: 36, height: 26, rx: 6, fill: 'none',
        stroke: 'rgba(255,255,255,.25)', strokeWidth: 2.4 }),
      h('rect', { className: 'cl-screen', x: 53, y: 83, width: 26, height: 16, rx: 3, fill: 'var(--accent)', opacity: .25 }),
      // капли-данные летят из облака в устройство
      h('circle', { className: 'cl-dot d1', cx: 58, cy: 52, r: 3.4, fill: 'var(--accent)' }),
      h('circle', { className: 'cl-dot d2', cx: 66, cy: 52, r: 3.4, fill: 'var(--accent)' }),
      h('circle', { className: 'cl-dot d3', cx: 74, cy: 52, r: 3.4, fill: 'var(--accent)' }));
  }

  // Отрисовка столов: модель «собирается из полигонов» (мини, на месте превью стола).
  function PolyLoader() {
    return h('svg', { className: 'polyl', viewBox: '0 0 48 48', 'aria-hidden': true },
      h('polygon', { points: '24,8 36,20 24,22', fill: 'var(--accent)', opacity: .85 }),
      h('polygon', { points: '24,8 12,20 24,22', fill: 'var(--accent)', opacity: .6 }),
      h('polygon', { points: '12,20 24,22 18,36', fill: 'var(--accent)', opacity: .45 }),
      h('polygon', { points: '36,20 24,22 30,36', fill: 'var(--accent)', opacity: .7 }),
      h('polygon', { points: '18,36 24,22 30,36', fill: 'var(--accent)', opacity: .55 }));
  }

  // Остальные ожидания: органичный «блоб» (all should have an organic, blobby feeling).
  function BlobLoader() {
    return h('div', { className: 'blobl', 'aria-hidden': true },
      h('div', { className: 'b1' }), h('div', { className: 'b2' }));
  }

  // Картинка одного стола в выборе столов (OpenTodo 314e175e): сервер отдаёт вшитую в .3mf
  // plate_N.png, а если её нет — рендерит превью стола сам (это секунды на большой модели),
  // поэтому пока грузится — анимация полигонов (5f6de0a7), при ошибке — кубик.
  function PlateImg(props) {
    var es = useState(false); var bad = es[0], setBad = es[1];
    var ls = useState(false); var ok = ls[0], setOk = ls[1];
    if (bad) return h('div', { className: 'pph' }, icon('box', { size: 20 }));
    return h(React.Fragment, null,
      !ok && h('div', { className: 'pph' }, h(PolyLoader, null)),
      h('img', { src: props.src, alt: '', loading: 'lazy',
        style: ok ? null : { position: 'absolute', width: 1, height: 1, opacity: 0 },
        onLoad: function () { setOk(true); }, onError: function () { setBad(true); } }));
  }

  // Превью модели с анимацией ожидания (OpenTodo ce2ff680): сервер рендерит mpreview/cpreview
  // по запросу — пока картинка не пришла, та же анимация «сборки из полигонов», что и у
  // столов (ed358d43: отрисовка одного стола и нескольких выглядит одинаково).
  function PreviewImg(props) {
    var ls = useState(false); var ok = ls[0], setOk = ls[1];
    React.useEffect(function () { setOk(false); }, [props.src]);   // смена src → снова ждём
    return h(React.Fragment, null,
      h('img', { src: props.src, alt: '', style: ok ? null : { opacity: 0 },
        onLoad: function () { setOk(true); }, onError: function () { setOk(true); } }),
      !ok && h('div', { className: 'pwait' },
        h(PolyLoader, null),
        h('span', null, 'Готовлю превью…')));
  }

  // ───────── Галерея картинок карточки товара (OpenTodo ae038968) ─────────
  // Фидбэк владельца 2026-07-11: в галерее — только НАСТОЯЩАЯ картинка/фото товара (картинка
  // каталога/фото печати). Технические рендеры столов сюда НЕ идут (дублировали рендер слайсера
  // ниже — «выглядит как полный бред»); нет нормальной картинки — галереи нет вовсе, и рендер
  // слайсера остаётся единственным визуалом (hero в RenderSlot). Листание — горизонтальный
  // скролл со scroll-snap (CSS, app.css); тап — во весь экран через PS_Lightbox.
  function galleryImages(product) {
    return (product && product.preview) ? [product.preview] : [];
  }
  function Gallery(props) {
    var images = props.images || [];
    var trackRef = React.useRef(null);
    var is = useState(0); var idx = is[0], setIdx = is[1];
    // индекс текущего слайда — по факту скролла (свайп пальцем тоже гоняет этот же track)
    function onScroll() {
      var el = trackRef.current; if (!el) return;
      var w = el.clientWidth || 1;
      var i = Math.round(el.scrollLeft / w);
      if (i !== idx) setIdx(i);
    }
    if (!images.length) return null;
    return h('div', { className: 'gallery' },
      h('div', { className: 'gallery-track', ref: trackRef, onScroll: onScroll },
        images.map(function (src, i) {
          return h('button', { key: i + ':' + src, type: 'button', className: 'gallery-slide',
            'aria-label': 'Открыть изображение во весь экран',
            onClick: function () { props.onOpen && props.onOpen(src); } },
            h(PreviewImg, { src: src, key: src }));
        })),
      // « мм» добавляем только если его ещё нет в строке (дубль «мм мм» — фидбэк 2026-07-11)
      props.dims && h('span', { className: 'dimtag' },
        String(props.dims).indexOf('мм') >= 0 ? props.dims : props.dims + ' мм'),
      images.length > 1 && h('div', { className: 'gallery-dots' }, images.map(function (_, i) {
        return h('span', { key: i, className: 'dot' + (i === idx ? ' on' : '') });
      })));
  }

  // Рендер слайсера (OpenTodo ae038968: «под этой картинкой мы рисуем рендер, пока рисуется —
  // анимация»). props: cpreview — честный рендер слайсера; fallback — превью модели, если
  // НАСТОЯЩЕЙ картинки товара нет и рендер ещё не готов (загрузка файла/ссылки — там рендер и
  // есть единственный визуал); hero — блок работает главным изображением карточки (галереи
  // нет), крупнее; refining — фоновый слайс идёт, без картинки показываем шиммер. Блока нет
  // вовсе, если показать нечего и уточнение не идёт (например, requote без слайса).
  function RenderSlot(props) {
    var src = props.cpreview || props.fallback || '';
    if (!src && !props.refining) return null;
    return h('div', { className: 'renderslot' + (props.hero ? ' hero' : '') },
      src
        ? h('button', { type: 'button', className: 'renderimg', 'aria-label': 'Открыть рендер во весь экран',
            onClick: function () { props.onOpen && props.onOpen(src); } },
            h('img', { src: src, alt: '', key: src }))
        : h('div', { className: 'rshimmer' }, h('span', null, 'Готовим рендер…')));
  }

  // Количество: шаговый edit-box (−/число/+) + ползунок. Заменяет фикс-кнопки 1/2/3/5/10 —
  // позволяет задать любое значение. Сервер /api/calc всё равно зажимает в [1, 1000].
  function QtyControl(props) {
    var qty = props.value, onSet = props.onSet;
    var sliderMax = Math.max(20, qty);          // ползунок всегда дотягивается до текущего значения
    function clamp(v) { v = Math.round(Number(v)); if (!v || v < 1) v = 1; if (v > 1000) v = 1000; return v; }
    return h('div', { className: 'qty' },
      h('div', { className: 'stepper' },
        h('button', { className: 'stepbtn', type: 'button', 'aria-label': 'Меньше',
          onClick: function () { onSet(clamp(qty - 1)); } }, icon('minus', { size: 18 })),
        h('input', { className: 'qfield mono', type: 'number', inputMode: 'numeric', min: 1, max: 1000, value: qty,
          onFocus: function (e) { try { e.target.select(); } catch (x) {} },
          onChange: function (e) { if (e.target.value === '') return; onSet(clamp(e.target.value)); } }),
        h('button', { className: 'stepbtn', type: 'button', 'aria-label': 'Больше',
          onClick: function () { onSet(clamp(qty + 1)); } }, icon('plus', { size: 18 })),
        h('span', { className: 'qunit' }, 'шт')),
      h('input', { className: 'qrange', type: 'range', min: 1, max: sliderMax, step: 1, value: Math.min(qty, sliderMax),
        onChange: function (e) { onSet(clamp(e.target.value)); } }));
  }

  // ─────────────────── СЕРДЕЧКО «ИЗБРАННОЕ» ───────────────────
  // Общая кнопка-сердечко для плиток каталога/ленты/поиска и карточки товара. id — модель
  // КАТАЛОГА (collection). span, не button: плитки сами <button>, вложенный button — не HTML.
  // props.inline — вариант в строке (карточка товара), иначе — оверлей в углу картинки.
  function FavHeart(props) {
    var app = props.app, id = props.id;
    if (!id || !app || !app.toggleFav) return null;
    var on = !!(app.favs && app.favs[id]);
    return h('span', {
      className: 'favheart' + (on ? ' on' : '') + (props.inline ? ' inline' : ''),
      role: 'button', tabIndex: 0,
      'aria-label': on ? 'Убрать из избранного' : 'В избранное',
      onClick: function (e) { e.stopPropagation(); e.preventDefault(); app.toggleFav(id); },
    }, icon(on ? 'heartf' : 'heart', { size: props.inline ? 18 : 16 }));
  }

  // Кэш ленты рекомендаций между заходами на Главную. Без него возврат из открытой
  // рекомендации перемонтирует Hub: лента сбрасывалась и тянулась заново с начала —
  // лишний запрос на бэк и потерянное место в ленте (жалоба клиента). Живёт на уровне
  // модуля, поэтому переживает перемонтирование компонента. Позицию прокрутки НЕ храним:
  // её возвращает штатный менеджер скролла App (scrollPos + лесенка, app.js) — кэш лишь
  // гарантирует, что карточки в DOM сразу и позиции есть куда дорасти.
  var HUB_FEED = { items: [], next: 0 };
  // Стопка превью «Готовых изделий» в акцентной строке (F4): витрину целиком Главная не
  // рисует — тянем /api/products один раз за сессию и держим первые 3 фото на модуле.
  var HUB_READY = { previews: null };
  // Состояние экрана поиска (запрос, выдача, фильтр) живёт на модуле, как COLL_STATE у каталога:
  // тап по найденной модели уводит на карточку/смету и РАЗМОНТИРУЕТ ModelInput, а «назад»
  // обязан вернуть ту же выдачу на том же месте, а не пустой бар с подсказкой (OpenTodo
  // cc642555 «стрелочка назад после выбора модельки через поиск»). Позицию скролла возвращает
  // штатный менеджер App (scrollPos) — здесь только данные, чтобы ему было куда возвращать.
  var SEARCH_STATE = { q: '', cat: null, ext: null, avail: [], err: false, flt: 'all' };

  // ───────────── Масонри в две колонки без «прыжков» (OpenTodo dfee9d2f) ─────────────
  // Раньше лента была CSS column-count:2: браузер сам балансирует колонки по высоте, и любое
  // изменение высоты — догрузилась ленивая картинка, приехала следующая страница ленты,
  // появились и исчезли скелетоны — перекладывает карточки МЕЖДУ колонками. При пролистывании
  // это и выглядело как «прыгает правый столбец». Здесь колонки явные: карточка один раз
  // получает колонку и больше её не меняет, новые карточки дописываются вниз в ту колонку, что
  // короче (по фактической высоте DOM плюс оценка ещё не отрисованных). Скелетоны — по одному
  // в конец каждой колонки, тоже без перекладки. Новая выдача (другая первая карточка) —
  // назначения с нуля.
  function Masonry(props) {
    var items = props.items || [], keyOf = props.keyOf, render = props.render;
    var refs = [useRef(null), useRef(null)];
    var st = useRef({ map: {}, first: null }); var s = st.current;
    var firstKey = items.length ? keyOf(items[0], 0) : null;
    var fresh = firstKey !== s.first;
    if (fresh) { s.map = {}; s.first = firstKey; }
    var hs = [0, 0];
    if (!fresh) refs.forEach(function (r, i) { if (r.current) hs[i] = r.current.offsetHeight; });
    // оценка высоты ещё не отрисованной карточки — средняя по уже стоящим (или типичная)
    var placed = Object.keys(s.map).length;
    var avg = placed ? (hs[0] + hs[1]) / placed : 210;
    var cols = [[], []];
    items.forEach(function (it, i) {
      var k = keyOf(it, i), c = s.map[k];
      if (c === undefined) { c = hs[0] <= hs[1] ? 0 : 1; s.map[k] = c; hs[c] += avg; }
      // data-sanchor — метка для менеджера скролла App (app.js): при возврате «назад» он
      // возвращает на место КАРТОЧКУ с этим ключом, а не запомненное число пикселей. Без метки
      // возврат на выдачу поиска показывал соседнюю карточку: фото грузятся лениво, и те, что
      // выше по списку, дорастали до своей высоты уже ПОСЛЕ восстановления позиции (фидбэк
      // владельца 03.09.2026). Метку вешаем клоном — обёртка сломала бы разметку колонки.
      var node = render(it, i);
      if (React.isValidElement(node)) node = React.cloneElement(node, { 'data-sanchor': String(k) });
      cols[c].push(h(React.Fragment, { key: k }, node));
    });
    return h('div', { className: 'feed', style: props.style },
      cols.map(function (col, i) {
        return h('div', { className: 'fcol', key: i, ref: refs[i] }, col,
          props.busy && skelCard(i ? 1.3 : 1.1, 'sk' + i));
      }));
  }

  // Печатающийся пример в пустом баре (F4 §3.1): печать по символу 55–100 мс → пауза
  // 1700 мс → стирание по 2 символа каждые 22 мс → пауза 350 мс → следующая фраза.
  // Компонент монтируется только пока input пуст и не в фокусе, поэтому «замирание» при
  // фокусе — обычное размонтирование; таймер чистится в cleanup-эффекте.
  var HBAR_PHRASES = ['подставка для наушников', 'makerworld.com/models/1042',
    'крючок для полотенец', 'кабель-органайзер'];
  var REDUCED_MOTION = false;
  try {
    REDUCED_MOTION = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (e) { /* нет matchMedia — анимируем как обычно */ }
  function TypeIn() {
    var s = useState({ i: 0, n: 0, del: false }); var st = s[0], setSt = s[1];
    useEffect(function () {
      var ph = HBAR_PHRASES[st.i]; var t;
      if (!st.del && st.n < ph.length) t = setTimeout(function () { setSt({ i: st.i, n: st.n + 1, del: false }); }, 55 + Math.random() * 45);
      else if (!st.del) t = setTimeout(function () { setSt({ i: st.i, n: st.n, del: true }); }, 1700);
      else if (st.n > 0) t = setTimeout(function () { setSt({ i: st.i, n: Math.max(0, st.n - 2), del: true }); }, 22);
      else t = setTimeout(function () { setSt({ i: (st.i + 1) % HBAR_PHRASES.length, n: 0, del: false }); }, 350);
      return function () { clearTimeout(t); };
    }, [st]);
    return h('span', { className: 'ghost' }, HBAR_PHRASES[st.i].slice(0, st.n), h('span', { className: 'fcur' }));
  }

  // «~N ч» на карточке ленты: до часа — в минутах, дальше — целые часы. 0 = не считалась.
  function feedTime(min) {
    min = Number(min) || 0;
    if (min <= 0) return '';
    return min < 60 ? ('~' + Math.round(min) + ' мин') : ('~' + Math.round(min / 60) + ' ч');
  }

  // Скелетон-карточка масонри (общая для ленты Главной и поиска): shimmer, пропорция фото.
  function skelCard(asp, key) {
    return h('div', { className: 'skel', key: key || undefined },
      h('div', { className: 'im', style: asp ? { aspectRatio: String(asp) } : null }),
      h('div', { className: 'l1' }), h('div', { className: 'l2' }));
  }

  // ───────────────────────── HUB ─────────────────────────
  // Главная по финалу F4 (design_handoff_home_f4): живой бар ввода вместо большой зоны,
  // hint-ряд (Файл / По фото / площадки), акцентные «Готовые изделия» со стопкой превью и
  // бесконечная масонри-лента «Рекомендуем напечатать». Логика приёма прежняя: URL →
  // loadUrl, несколько ссылок → пакетно, текст → поиск, файл → loadFile.
  function Hub(props) {
    var app = props.app, o = app.options || {};
    var hq = useState(''); var hqv = hq[0], setHqv = hq[1];
    var ds = useState(false); var over = ds[0], setOver = ds[1];    // drag-over подсветка бара
    var fs = useState(false); var fc = fs[0], setFc = fs[1];        // input в фокусе
    var ks = useState(false); var stuck = ks[0], setStuck = ks[1];  // бар прилип при скролле
    var ps = useState(''); var paste = ps[0], setPaste = ps[1];     // paste-чип (§3.2)
    var rs = useState(HUB_READY.previews || []); var ready = rs[0], setReady = rs[1];
    var inputRef = useRef(null);
    var watchRef = useRef(null);       // 1px-сторож над баром: ушёл за верх → бар прилип
    var blurT = useRef(null);
    // Бесконечная лента (df0c9674 → F4): курсорная пагинация /api/feed. Сентинел внизу
    // докачивает страницы через IntersectionObserver; стартовое состояние — из кэша.
    var fS = useState(function () {
      return HUB_FEED.items.length
        ? { items: HUB_FEED.items, next: HUB_FEED.next, busy: false }
        : { items: [], next: 0, busy: false };
    }); var feed = fS[0], setFeed = fS[1];
    var feedRef = React.useRef(feed); feedRef.current = feed;
    var sentinelRef = React.useRef(null);
    function loadFeed() {
      var f = feedRef.current;
      if (f.busy || f.next == null) return;
      setFeed(Object.assign({}, f, { busy: true }));
      API.feed({ cursor: f.next, limit: 10 }).then(function (j) {
        if (!j || !j.ok) { setFeed(Object.assign({}, feedRef.current, { busy: false, next: null })); return; }
        var merged = { items: feedRef.current.items.concat(j.items || []),
          next: (j.next == null ? null : j.next), busy: false };
        setFeed(merged);
        // держим кэш в актуальном состоянии, чтобы возврат на Главную не тянул заново
        HUB_FEED.items = merged.items; HUB_FEED.next = merged.next;
      }).catch(function () { setFeed(Object.assign({}, feedRef.current, { busy: false })); });
    }
    React.useEffect(function () {
      // первую страницу тянем только если кэш пуст — иначе показываем уже загруженное
      if (!feedRef.current.items.length) loadFeed();
      var ob = new IntersectionObserver(function (es) {
        if (es[0] && es[0].isIntersecting) loadFeed();
      }, { rootMargin: '400px' });   // §3.3: подгрузка за ~400px до конца
      if (sentinelRef.current) ob.observe(sentinelRef.current);
      // сторож липкости: 1px-элемент над баром ушёл из вью → включаем .hbar-stuck
      var so = new IntersectionObserver(function (es) {
        if (es[0]) setStuck(!es[0].isIntersecting);
      });
      if (watchRef.current) so.observe(watchRef.current);
      return function () {
        ob.disconnect(); so.disconnect();
        if (blurT.current) clearTimeout(blurT.current);
      };
    }, []);
    // стопка превью готовых изделий — один лёгкий запрос за сессию (кэш на модуле)
    React.useEffect(function () {
      if (HUB_READY.previews) return;
      API.products().then(function (j) {
        var ph = ((j && j.products) || []).map(function (p) { return p.photo; })
          .filter(Boolean).slice(0, 3);
        HUB_READY.previews = ph;
        setReady(ph);
      }).catch(function () { /* витрина недоступна — строка живёт без стопки */ });
    }, []);
    // §3.2: буфер обмена проверяем НА ФОКУС input. Сначала Telegram-API (в WebView
    // navigator.clipboard часто недоступен вовсе), затем стандартный — для превью в
    // браузере. Любой отказ — тихо без чипа: это прогрессивное улучшение.
    function checkClipboard() {
      try {
        var tg = window.Telegram && window.Telegram.WebApp;
        if (tg && tg.readTextFromClipboard) {
          tg.readTextFromClipboard(function (t) { if (looksLikeModelUrl(t)) setPaste(String(t).trim()); });
          return;
        }
      } catch (e) { /* WebAppMethodUnsupported и т.п. — пробуем обычный API */ }
      try {
        var nav = window.navigator;
        if (nav && nav.clipboard && nav.clipboard.readText) {
          nav.clipboard.readText().then(function (t) {
            if (looksLikeModelUrl(t)) setPaste(String(t).trim());
          }).catch(function () {});
        }
      } catch (e) { /* буфер недоступен — просто без чипа */ }
    }
    function onFocus() {
      if (blurT.current) { clearTimeout(blurT.current); blurT.current = null; }
      setFc(true); checkClipboard();
    }
    function onBlur() {
      // задержка: тап по paste-чипу сначала блюрит input — дать click-событию дойти
      blurT.current = setTimeout(function () { setFc(false); setPaste(''); }, 180);
    }
    function norm(u) { return /^https?:\/\//i.test(u) ? u : 'https://' + u; }
    function submit() {
      var raw = (hqv || '').trim();
      var urls = raw.split(/\s+/).filter(Boolean).filter(looksLikeUrl);
      if (urls.length) {
        setHqv('');
        if (urls.length === 1) app.loadUrl(norm(urls[0]));
        else app.loadUrls(urls.map(norm));          // несколько ссылок → пакетный расчёт в корзину
        return;
      }
      // не ссылка → это поисковый запрос: экран model теперь ЧИСТО поиск (IA-редизайн),
      // отдельного «шага 1» больше нет — вход в расчёт один, с Главной
      if (raw.length >= 2) { setHqv(''); app.searchModels(raw); return; }
      app.toast('Вставьте ссылку или название модели');
    }
    function pickFile(f) {
      if (!f) return;
      if (!/\.(stl|3mf|obj|zip|step|stp)$/i.test(f.name)) { app.toast('Нужен файл .stl / .3mf / .obj / .step / .zip'); return; }
      app.loadFile(f);
    }
    function onDrop(e) {
      e.preventDefault(); setOver(false);
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) pickFile(f);
    }
    var cur = (o.currency) || 'руб.';
    // тап по карточке ленты открывает карточку МГНОВЕННО из уже загруженных данных (m),
    // модель/цена подгружаются фоном — не app.useModel (тот блокирует оверлеем) (ae038968)
    function fcard(m, i) {
      // Карточка мастерской органайзеров (сервер подмешивает её в первую страницу ленты
      // через раз): не товар — тап открывает список проектов, сердечка и цены нет.
      // Рисуем свою обложку (иконка на градиенте), у неё нет preview-картинки.
      if (m.kind === 'gridfinity') {
        return h('button', { key: 'gf:' + i, className: 'fcard', onClick: function () { app.go('workshop'); } },
          h('div', { className: 'fimg', style: { aspectRatio: '1.25', display: 'flex', alignItems: 'center',
            justifyContent: 'center', background: 'linear-gradient(135deg, var(--accent-soft), var(--surface-2))' } },
            icon('layers', { size: 46, style: { color: 'var(--accent)' } })),
          h('div', { className: 'cap' },
            h('div', { className: 'n' }, m.title || 'Органайзер в ящик'),
            h('div', { className: 'm' },
              // у .tm в app.css white-space:nowrap под короткое «~2 ч» — длинную подпись
              // на узких телефонах резало посреди слова, разрешаем перенос точечно
              h('span', { className: 'tm', style: { whiteSpace: 'normal' } }, 'соберите под свой ящик — 3D'))));
      }
      var t = feedTime(m.time_min);
      var price = Number(m.price_from || m.price) || 0;
      return h('button', { key: m.id + ':' + i, className: 'fcard', onClick: function () { app.openProduct(m); } },
        h('div', { className: 'fimg' },
          h('img', { src: m.preview, alt: '', loading: 'lazy' }),
          m.printed ? h('span', { className: 'badge', style: { color: 'var(--amber)' } }, '📸') : null,
          h(FavHeart, { app: app, id: m.id })),
        h('div', { className: 'cap' },
          h('div', { className: 'n' }, m.title || m.label),
          (price > 0 || t) && h('div', { className: 'm' },
            price > 0 && h('span', { className: 'pr' },
              'от ' + String(Math.round(price * 100) / 100).replace('.', ',') + ' ' + cur),
            t && h('span', { className: 'tm' }, t))));
    }
    return h('div', { className: 'screen',
      // drag-and-drop файла работает на всём экране (большой пунктирной зоны больше нет)
      onDragOver: function (e) { e.preventDefault(); setOver(true); },
      onDragLeave: function () { setOver(false); }, onDrop: onDrop },

      h('div', { ref: watchRef, style: { height: 1 } }),

      // ── живой бар ввода (§1.2): «>» + настоящий input + кнопка-go; липнет при скролле ──
      h('div', { className: 's-wrap' + (stuck ? ' hbar-stuck' : '') },
        h('div', { className: 'hbar' + ((fc || over) ? ' focus' : ''),
          onClick: function () { if (inputRef.current) inputRef.current.focus(); } },
          h('span', { className: 'pfx' }, '>'),
          h('div', { className: 'fld' },
            h('input', { ref: inputRef, value: hqv,
              placeholder: REDUCED_MOTION ? 'Ссылка или название модели' : '',
              onChange: function (e) { setHqv(e.target.value); },
              onFocus: onFocus, onBlur: onBlur,
              onKeyDown: function (e) { if (e.key === 'Enter') submit(); } }),
            // печатающийся пример — только пока input пуст и не в фокусе (§3.1)
            !REDUCED_MOTION && !fc && !hqv && h(TypeIn, null)),
          h('button', { className: 'go', type: 'button', disabled: app.busy, onClick: submit,
            'aria-label': 'Загрузить' }, icon('chev', { size: 17 })))),

      // ── paste-чип (§3.2): в буфере ссылка на площадку → расчёт в 1 тап ──
      paste && h('button', { className: 'paste-chip', type: 'button',
        onClick: function () { var u = paste; setPaste(''); setHqv(''); app.loadUrl(norm(u)); } },
        h('span', { className: 'ic' }, icon('doc', { size: 16 })),
        h('span', { className: 't' }, 'Вставить из буфера', h('b', null, paste)),
        icon('plus', { size: 15, style: { color: 'var(--accent)' } })),

      // ── ряд подсказок: Файл / По фото + логотипы площадок (клик = открыть площадку) ──
      h('div', { className: 'hintrow' },
        h('label', { className: 'fchip' },
          icon('upload', { size: 14 }), ' Файл',
          h('input', { type: 'file', style: { display: 'none' },
            onChange: function (e) { var f = e.target.files[0]; e.target.value = ''; pickFile(f); } })),
        h('button', { className: 'fchip', type: 'button', onClick: function () { app.go('custom'); } },
          icon('search', { size: 14 }), ' По фото'),
        h('div', { className: 'favs' }, catalogs().map(function (s) {
          return h('button', { key: s.domain, type: 'button', 'aria-label': s.nm,
            onClick: function () { openExternal(s.url); } },
            h(CatalogLogo, { site: s }));
        }))),

      // контент под вводом приглушается, пока юзер печатает (§2, состояние «фокус»)
      h('div', { className: fc ? 'dimmed' : undefined },
        // ── «Готовые изделия» (§1.4): тёплая строка со стопкой превью витрины ──
        h('button', { className: 'ready', type: 'button', onClick: function () { app.go('products'); } },
          ready.length > 0 && h('div', { className: 'stack' }, ready.map(function (src, i) {
            return h('img', { key: i, src: src, alt: '' });
          })),
          h('div', { className: 'grow' },
            h('div', { className: 'tt' }, 'Готовые изделия'),
            h('div', { className: 'ss' }, 'Уже напечатано — купить и забрать без ожидания')),
          icon('chev', { size: 17, style: { color: 'var(--accent)', flex: '0 0 auto' } })),

        // ── лента «Рекомендуем напечатать» (§1.5): масонри 2 колонки, бесконечная ──
        feed.items.length > 0 && h('div', { className: 'feedlab' },
          icon('sparkle', { size: 14 }), ' Рекомендуем напечатать',
          h('span', { className: 'r' }, 'лента ∞')),
        // явные колонки (Masonry): карточка не меняет колонку при догрузке страниц и картинок
        (feed.items.length > 0 || feed.busy) && h(Masonry, { items: feed.items, busy: feed.busy,
          keyOf: function (m, i) { return m.kind === 'gridfinity' ? ('gf:' + i) : (m.id + ':' + i); },
          render: fcard }),
        h('div', { ref: sentinelRef, style: { height: 2 } })));
  }

  // catalog sites — external redirects (vendored logos in logos/, letter fallback)
  var CATALOGS = [
    { nm: 'MakerWorld',  short: 'MW', domain: 'makerworld.com',  url: 'https://makerworld.com',      color: '#39a6a3', file: 'makerworld.png' },
    { nm: 'Printables',  short: 'PR', domain: 'printables.com',  url: 'https://www.printables.com',  color: '#fa6831', file: 'printables.png' },
    { nm: 'Thingiverse', short: 'TH', domain: 'thingiverse.com', url: 'https://www.thingiverse.com', color: '#248bfb', file: 'thingiverse.png' }
  ];
  // MakerWorld — только если ссылки с него скачиваются в этой сборке (личный модуль; в открытой
  // версии его нет, и звать клиента туда за моделью незачем). Флаги известны лишь к отрисовке.
  function catalogs() {
    return CATALOGS.filter(function (c) { return c.nm !== 'MakerWorld' || window.PS_hasFeature('makerworld'); });
  }
  function openExternal(url) {
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
    else window.open(url, '_blank');
  }
  // юзер мог вставить ссылку в строку поиска — распознаём её и грузим как по ссылке, а не ищем текстом
  function looksLikeUrl(s) {
    if (!s || /\s/.test(s)) return false;                              // есть пробел → это поисковый запрос
    if (/^https?:\/\//i.test(s)) return true;                          // явная схема http(s)://
    return /^(www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}([/?#]|$)/i.test(s); // голый хост (+ путь): makerworld.com/...
  }
  // Чип из буфера обмена (идея 6): «правдоподобная» ссылка на модель — либо явный http(s),
  // либо голый хост известной площадки. Не срабатываем на длинном тексте/нескольких словах.
  var MODEL_HOSTS = /(makerworld\.com|printables\.com|thingiverse\.com|thangs\.com|cults3d\.com|myminifactory\.com)/i;
  function looksLikeModelUrl(s) {
    if (!s || typeof s !== 'string') return false;
    s = s.trim();
    if (!s || s.length > 300 || /\s/.test(s) || !looksLikeUrl(s)) return false;
    return /^https?:\/\//i.test(s) || MODEL_HOSTS.test(s);
  }
  function CatalogLogo(props) {
    var s = props.site;
    var es = useState(false); var err = es[0], setErr = es[1];
    if (err) return h('span', { className: 'fav-fb', style: { background: s.color } }, s.short);
    return h('img', { className: 'fav', alt: s.nm, src: 'logos/' + s.file,
      onError: function () { setErr(true); } });
  }
  // Метаданные источников поиска по ключу из контракта /api/model_search (source).
  // nm — подпись для бейджа/чипа, short/color — буквенный fallback (как у CatalogLogo).
  var SOURCE_META = {
    printables:  { nm: 'Printables',  short: 'PR', color: '#fa6831', file: 'printables.png' },
    thingiverse: { nm: 'Thingiverse', short: 'TH', color: '#248bfb', file: 'thingiverse.png' },
    makerworld:  { nm: 'MakerWorld',  short: 'MW', color: '#39a6a3', file: 'makerworld.png' },
  };
  function srcMeta(source) {
    return SOURCE_META[source] || { nm: source || '?', short: (source || '?').slice(0, 2).toUpperCase(), color: '#7c8b9c', file: '' };
  }
  // Логотип источника на карточке результата: картинка из logos/, буквенный fallback при ошибке.
  function SourceLogo(props) {
    var m = props.meta;
    var es = useState(false); var err = es[0], setErr = es[1];
    if (err || !m.file) return h('span', { className: 'fav-fb', style: { background: m.color } }, m.short);
    return h('img', { className: 'fav', alt: m.nm, src: 'logos/' + m.file, onError: function () { setErr(true); } });
  }

  // ───────────────────── ПОИСК (экран model, язык F4) ─────────────────────
  // Поиск — состояние главной, а не отдельная страница (README_SEARCH): «назад» + тот же
  // .hbar в фокус-виде с запросом, клиентские фильтры по источнику, две масонри-секции из
  // карточек ленты (.fcard) с бейджами источника; пустая выдача всегда даёт три выхода.
  // Табы «Поиск · Ссылка · Файл» упразднены: бар принимает всё (URL распознаётся и уводит
  // сразу в расчёт), файл — чип «Файл» на главной или выход из «пусто».
  function ModelInput(props) {
    var app = props.app;
    // Стартовые значения — из SEARCH_STATE: возврат «назад» с карточки/сметы монтирует экран
    // заново, и он обязан показать прежнюю выдачу, а не пустой бар.
    var qs = useState(SEARCH_STATE.q); var q = qs[0], setQ = qs[1];
    // Каталог и площадки ищутся НЕЗАВИСИМО (dc953458): каталожные модели уже готовы к
    // мгновенному расчёту — рисуются сразу, не дожидаясь площадок. null = ещё не спрашивали.
    var cS = useState(SEARCH_STATE.cat); var cat = cS[0], setCat = cS[1];
    var xS = useState(SEARCH_STATE.ext); var ext = xS[0], setExt = xS[1];
    var avs = useState(SEARCH_STATE.avail); var avail = avs[0], setAvail = avs[1];   // площадки, реально опрошенные
    var es = useState(SEARCH_STATE.err); var err = es[0], setErr = es[1];       // внешний поиск упал (не «пусто»)
    var fS = useState(SEARCH_STATE.flt); var flt = fS[0], setFlt = fS[1];       // all | catalog | ключ площадки
    var bc = useState(false); var busyCat = bc[0], setBusyCat = bc[1];
    var bx = useState(false); var busyExt = bx[0], setBusyExt = bx[1];
    React.useEffect(function () {
      SEARCH_STATE.q = q; SEARCH_STATE.cat = cat; SEARCH_STATE.ext = ext;
      SEARCH_STATE.avail = avail; SEARCH_STATE.err = err; SEARCH_STATE.flt = flt;
    }, [q, cat, ext, avail, err, flt]);
    // Поиск по ВНЕШНИМ площадкам — личный инструмент владельца (свои аккаунты, свои ключи,
    // локальная модель для семантики). В сборке студии он выключен (f98aa1ef) — ищется
    // только каталог мастерской, чипы площадок не показываются.
    var extOn = window.PS_hasFeature('model_search');
    var seq = useRef(0);   // номер запроса: поздний ответ старого поиска не перетирает новый

    // Префилл из бара главной (searchModels) и из оверлея «Найти на Printables».
    React.useEffect(function () {
      var pq = app.pendingSearch;
      if (pq) { app.clearPendingSearch(); setQ(pq); doSearch(pq); }
    }, [app.pendingSearch]);

    // §5: единая нормализация {id, title, preview, source, price_from?, time_min?, url}.
    // У каталожной карточки исходная плитка хранится в _cat — тап открывает её мгновенно
    // через openProduct (ae038968), как в ленте главной; у площадок тап = loadUrl.
    function normCat(m) {
      return { id: m.id, title: m.label, preview: m.preview, source: 'catalog',
        price_from: Number(m.price) || 0, time_min: Number(m.time_min) || 0, url: '', _cat: m };
    }
    function normHit(it) {
      return { id: it.source + ':' + it.ext_id, title: it.title, preview: it.thumb,
        source: it.source, price_from: 0, time_min: 0, url: it.page_url };
    }
    function doSearch(qOverride) {
      var query = (typeof qOverride === 'string' ? qOverride : q).trim();
      if (query.length < 2) return;
      if (looksLikeUrl(query)) { app.loadUrl(query); return; }   // ссылка в баре → сразу расчёт
      var my = ++seq.current;
      setErr(false); setFlt('all');
      setBusyCat(true); setCat(null);
      API.collectionSearch(query).then(function (j) {
        if (seq.current !== my) return;
        setCat(((j && j.ok && j.items) || []).map(normCat));
      }).catch(function () { if (seq.current === my) setCat([]); })
        .then(function () { if (seq.current === my) setBusyCat(false); });
      if (!extOn) { setExt([]); setAvail([]); return; }
      setBusyExt(true); setExt(null);
      API.modelSearch(query).then(function (j) {
        if (seq.current !== my) return;
        if (j && j.ok === false) { setErr(true); setExt([]); return; }
        setExt(((j && j.hits) || []).map(normHit));
        setAvail((j && j.sources) || []);     // какие площадки были доступны/опрошены
      }).catch(function () { if (seq.current === my) { setErr(true); setExt([]); } })
        .then(function () { if (seq.current === my) setBusyExt(false); });
    }
    function pickFile(f) {
      if (!f) return;
      if (!/\.(stl|3mf|obj|zip|step|stp)$/i.test(f.name)) { app.toast('Нужен файл .stl / .3mf / .obj / .step / .zip'); return; }
      app.loadFile(f);
    }
    function nOf(n, one, few, many) {
      var d = n % 10, dd = n % 100;
      return n + ' ' + ((d === 1 && dd !== 11) ? one
        : (d >= 2 && d <= 4 && (dd < 12 || dd > 14)) ? few : many);
    }
    var cur = (app.options && app.options.currency) || 'руб.';
    // §2: карточка — та же .fcard, что в ленте; поверх фото бейдж источника (.src-b).
    function sCard(it) {
      var meta = it.source === 'catalog' ? null : srcMeta(it.source);
      var t = feedTime(it.time_min);
      return h('button', { key: it.id, className: 'fcard sr-card', disabled: app.busy,
        onClick: function () {
          if (it.source === 'catalog') app.openProduct(it._cat);
          else app.loadUrl(it.url);
        } },
        h('div', { className: 'fimg' },
          h('img', { src: it.preview, alt: '', loading: 'lazy' }),
          meta ? h('span', { className: 'src-b' }, h(SourceLogo, { meta: meta }), meta.nm)
            : h('span', { className: 'src-b cat' }, 'каталог'),
          it.source === 'catalog' && h(FavHeart, { app: app, id: it.id })),
        h('div', { className: 'cap' },
          h('div', { className: 'n' }, it.title),
          (it.price_from > 0 || t) && h('div', { className: 'm' },
            it.price_from > 0 && h('span', { className: 'pr' },
              'от ' + String(Math.round(it.price_from * 100) / 100).replace('.', ',') + ' ' + cur),
            t && h('span', { className: 'tm' }, t))));
    }

    // §5: фильтры — клиентские, по source; «Все» — обе секции, «Наш каталог» — первая,
    // площадка — только она во второй. Новый запрос фильтр сбрасывает (doSearch).
    var catShown = (flt === 'all' || flt === 'catalog') ? (cat || []) : [];
    var extShown = flt === 'all' ? (ext || [])
      : (flt === 'catalog' ? [] : (ext || []).filter(function (it) { return it.source === flt; }));
    var busy = busyCat || busyExt;
    var started = busy || cat != null || ext != null;
    var total = (cat || []).length + (ext || []).length;
    var showEmpty = started && !busy && total === 0;
    // совет «покороче» в пустой выдаче — реально укороченный запрос юзера (как в прототипе)
    var words = q.trim().split(/\s+/).filter(Boolean);
    var shorter = words.length >= 3 ? words.slice(0, 2).join(' ') : '';
    var filters = [{ k: 'all', nm: 'Все' }, { k: 'catalog', nm: 'Наш каталог' }]
      .concat(extOn ? ['makerworld', 'printables', 'thingiverse'].map(function (s) {
        return { k: s, nm: srcMeta(s).nm, logo: true };
      }) : []);

    return h('div', { className: 'screen' },
      // ── назад + бар с запросом (§2.1): «назад» возвращает главную в исходном виде ──
      h('div', { className: 'sr-top' },
        h('button', { className: 'sr-back', type: 'button', 'aria-label': 'Назад',
          onClick: function () { app.goTab('hub'); } }, icon('back', { size: 18 })),
        h('div', { className: 'hbar focus' },
          h('span', { className: 'pfx' }, '>'),
          h('div', { className: 'fld' },
            h('input', { value: q, placeholder: 'Поиск моделей',
              onChange: function (e) { setQ(e.target.value); },
              onKeyDown: function (e) { if (e.key === 'Enter') doSearch(); } })),
          h('button', { className: 'go', type: 'button', disabled: app.busy,
            onClick: function () { doSearch(); }, 'aria-label': 'Найти' },
            icon('search', { size: 16 })))),

      // ── фильтры-источники (§2.2) ──
      h('div', { className: 'sr-filters' }, filters.map(function (f) {
        return h('button', { key: f.k, type: 'button', className: 'srf' + (flt === f.k ? ' on' : ''),
          onClick: function () { setFlt(f.k); } },
          f.logo && h(SourceLogo, { meta: srcMeta(f.k) }), f.nm);
      })),

      // ── статус-строка (§2.3) ──
      busy && h('div', { className: 'sr-status' }, h('span', { className: 'dot' }),
        'ищем: ' + ['каталог'].concat(extOn ? ['makerworld', 'printables', 'thingiverse'] : []).join(' · ')),
      !busy && started && total > 0 && h('div', { className: 'sr-status' },
        'нашли ' + nOf(total, 'модель', 'модели', 'моделей') + ' · каталог' +
        (extOn ? (err ? ' · площадки недоступны'
          : (avail.length ? ' + ' + nOf(avail.length, 'площадка', 'площадки', 'площадок') : '')) : '')),

      // ── «Из нашего каталога» — первыми и мгновенно, не дожидаясь площадок ──
      catShown.length > 0 && h('div', { className: 'feedlab', style: { marginTop: 13 } },
        icon('star', { size: 14 }), ' Из нашего каталога'),
      catShown.length > 0 && h(Masonry, { items: catShown, keyOf: function (it) { return it.id; }, render: sCard }),

      // ── «С площадок» — бейдж источника на каждом фото ──
      extShown.length > 0 && h('div', { className: 'feedlab' },
        icon('globe', { size: 14 }), ' С площадок',
        h('span', { className: 'r' }, nOf(extShown.length, 'модель', 'модели', 'моделей'))),
      extShown.length > 0 && h(Masonry, { items: extShown, keyOf: function (it) { return it.id; }, render: sCard }),

      // ── загрузка (§3): скелетоны в тех же колонках; каталог, если уже готов, виден выше
      // и НЕ сдвигается — скелетоны дозаполняют сетку снизу ──
      busy && h('div', { className: 'feed', style: { marginTop: catShown.length ? 10 : 13 } },
        h('div', { className: 'fcol' }, skelCard(1.1, 'sk1'), !catShown.length && skelCard(1.2, 'sk3')),
        h('div', { className: 'fcol' }, skelCard(1.35, 'sk2'), !catShown.length && skelCard(0.95, 'sk4'))),

      err && !busy && total > 0 && h('p', { className: 'hint', style: { marginTop: 10 } },
        'Площадки сейчас недоступны — показали, что нашлось в каталоге.'),

      // ── «ничего не нашли» (§ «Пусто»): не тупик — всегда три выхода ──
      showEmpty && h('div', { className: 'sr-empty' },
        h('div', { className: 'ic' }, icon('search', { size: 22 })),
        h('div', { className: 't' }, 'Ничего не нашли'),
        h('div', { className: 'd' }, shorter
          ? ['Попробуйте короче: «', h('b', { key: 'q' }, shorter), '» — или зайдите с другой стороны:']
          : 'Попробуйте другой запрос — или зайдите с другой стороны:')),
      showEmpty && h('div', { className: 'sr-acts' },
        h('label', { className: 'sr-act' },
          h('span', { className: 'aic' }, icon('upload', { size: 16 })),
          h('span', { className: 'tt' }, 'Загрузить свой файл',
            h('small', null, '.stl / .3mf / .obj · до 100 МБ')),
          icon('chev', { size: 15, style: { color: 'var(--faint)' } }),
          // БЕЗ accept: на Android Telegram-WebView accept мапится на неизвестные MIME →
          // файлы серые и не выбираются. Разрешаем любой файл, проверяем расширение сами.
          h('input', { type: 'file', style: { display: 'none' },
            onChange: function (e) { var f = e.target.files[0]; e.target.value = ''; pickFile(f); } })),
        h('button', { className: 'sr-act', type: 'button', onClick: function () { app.go('custom'); } },
          h('span', { className: 'aic' }, icon('search', { size: 16 })),
          h('span', { className: 'tt' }, 'Поиск по фото',
            h('small', null, 'пришлите снимок — найдём похожие')),
          icon('chev', { size: 15, style: { color: 'var(--faint)' } })),
        h('div', { className: 'sr-act', role: 'button', tabIndex: 0,
          onClick: function () { openExternal(catalogs()[0].url); } },
          h('span', { className: 'aic' }, icon('globe', { size: 16 })),
          h('span', { className: 'tt' }, 'Поискать на площадках',
            h('small', null, 'вручную, ссылку вставьте в бар')),
          h('span', { className: 'favs' }, catalogs().map(function (s) {
            return h('span', { key: s.domain, role: 'button', tabIndex: 0, 'aria-label': s.nm,
              onClick: function (e) { e.stopPropagation(); openExternal(s.url); } },
              h(CatalogLogo, { site: s }));
          })))),

      // экран открыт без запроса (deep-link) — подсказка вместо пустоты
      !started && h('p', { className: 'hint', style: { marginTop: 14 } },
        'Введите запрос — например, «подставка для наушников».'));
  }

  // ─────────────────── ВЫБОР СТОЛОВ (общий блок) ──────────────────
  // Выбор столов мультиплейт-модели (OpenTodo 77dc9b01). Вынесен из Config в общий компонент
  // по OpenTodo 71e15fe6 («вернуть возможность выбирать столы не из „Редактирования“, а с
  // основной страницы заказа») — теперь один и тот же блок рисуют и «Параметры», и СМЕТА,
  // расходиться им больше нечем. Единственная разница между экранами — что делает set():
  //   • «Параметры» — просто правит cfg, цену посчитает кнопка «Рассчитать» внизу экрана;
  //   • смета — правит cfg и ТИХО пересчитывает (app.tweak), не уходя со сметы.
  // Для одностольной модели рисует null (хуки при этом вызываются всегда — порядок стабилен).
  function PlatePicker(props) {
    var app = props.app, set = props.set, m = app.model || {}, s = app.cfg || {};
    var o = app.options || {}, plastics = o.plastics || [];
    var pl = plastics.filter(function (p) { return p.id === s.plastic_id; })[0] || plastics[0] || {};
    var cols = pl.colors || [];
    function dot(css) {
      return h('span', { className: 'dot-ring', style: { border: 0, background: css || '#888' } });
    }
    // Габариты и многоцвет каждого стола (OpenTodo eb4424d9 + d7518198): лениво с бэка.
    var piS = useState(null); var pinfo = piS[0], setPinfo = piS[1];
    // стол, которому выбираем цвет/пластик
    var pcS = useState(null); var pickFor = pcS[0], setPickFor = pcS[1];
    React.useEffect(function () {
      if (!(m.plates > 1) || !m.token) return;
      var stale = false;
      API.platesInfo(m.token).then(function (j) { if (!stale && j && j.ok) setPinfo(j.plates || []); });
      return function () { stale = true; };
    }, [m.token]);
    if (!(m.plates > 1)) return null;

    // по умолчанию выбраны все (s.plates == null); тап переключает стол; последний снять нельзя
    var selPlates = s.plates;   // null = все
    function plateOn(i) { return !selPlates || selPlates.indexOf(i) >= 0; }
    function togglePlate(i) {
      var all = []; for (var k = 0; k < m.plates; k++) all.push(k);
      var cur = selPlates ? selPlates.slice() : all;
      var at = cur.indexOf(i);
      if (at >= 0) { if (cur.length <= 1) return; cur.splice(at, 1); }
      else { cur.push(i); cur.sort(function (a, b) { return a - b; }); }
      set({ plates: cur.length === m.plates ? null : cur });
    }
    var nSel = selPlates ? selPlates.length : (m.plates || 1);
    function pin(i, k) { var p = (pinfo || [])[i]; return p ? p[k] : null; }
    // Цвет на каждый стол (OpenTodo 65aa1a35): plate_colors = {idx: имя}; пусто = базовый цвет.
    // Пластик на каждый стол (OpenTodo 70525c99): plate_plastics = {idx: plastic_id}; пусто =
    // общий пластик. Свой пластик стола задаёт и палитру его цветов, и цену (бэк считает честно).
    var plateColors = s.plate_colors || {};
    var platePlastics = s.plate_plastics || {};
    function platePl(i) {
      var pid = platePlastics[i];
      if (!pid || pid === s.plastic_id) return pl;
      return plastics.filter(function (p) { return p.id === pid; })[0] || pl;
    }
    function plateColor(i) {
      if (plateColors[i]) return plateColors[i];
      if (platePlastics[i]) return (platePl(i).colors || [])[0] || '';
      return s.color;
    }
    function setPlateColor(i, c) {
      var n = Object.assign({}, plateColors);
      if (!c || (c === s.color && !platePlastics[i])) delete n[i]; else n[i] = c;
      set({ plate_colors: n }); setPickFor(null);
    }
    function setPlatePlastic(i, p) {
      var n = Object.assign({}, platePlastics);
      var nc = Object.assign({}, plateColors);
      if (!p || p.id === s.plastic_id) { delete n[i]; delete nc[i]; }
      else { n[i] = p.id; nc[i] = (p.colors || [])[0] || ''; }
      set({ plate_plastics: n, plate_colors: nc });   // палитру не закрываем — дальше выбор цвета
    }
    function plateSrc(i) {
      var c = (platePlastics[i] || plateColors[i]) ? plateColor(i) : '';
      return '/api/plate/' + m.token + '/' + i + (c ? ('?c=' + encodeURIComponent(c)) : '');
    }
    function plateColCss(i) {
      var p = platePl(i); var k = (p.colors || []).indexOf(plateColor(i));
      return k >= 0 ? (p.color_css || [])[k] : '#888';
    }

    return h(React.Fragment, null,
      h(Lab, null, 'Какие столы печатать',
        nSel < m.plates ? ('выбрано ' + nSel + ' из ' + m.plates) : 'все'),
      h('div', { className: 'plates' },
        (function () {
          var tiles = [];
          for (var i = 0; i < m.plates; i++) {
            (function (i) {
              var on = plateOn(i);
              var mc = (m.plate_mc || [])[i] || pin(i, 'mc');
              var dims = pin(i, 'dims');
              var src = plateSrc(i);
              // Подпись «● Стол N ▾» — БОЛЬШАЯ кнопка цвета/пластика стола (фидбэк 492a9bc8:
              // точку 11px на телефоне не нажать). Тап по картинке = вкл/выкл стола, тап по
              // подписи = палитра (пластик + цвет). Один пластик с одним цветом — выбирать нечего.
              var colorable = (cols.length > 1 || plastics.length > 1) && on;
              tiles.push(h('button', { key: i, className: 'plate' + (on ? ' on' : ''), type: 'button',
                disabled: app.busy,
                onClick: function () { togglePlate(i); } },
                h(PlateImg, { src: src, key: src }),
                h('span', { className: 'pnum' + (colorable ? ' tappable' : ''),
                  role: colorable ? 'button' : undefined,
                  title: colorable ? 'Выбрать пластик и цвет этого стола' : undefined,
                  onClick: colorable ? function (e) { e.stopPropagation(); setPickFor(pickFor === i ? null : i); } : undefined },
                  cols.length > 0 && on && h('span', { className: 'pcol',
                    style: { background: plateColCss(i) } }),
                  'Стол ', i + 1,
                  platePlastics[i] && h('span', { style: { color: 'var(--accent)', fontWeight: 700 } },
                    ' · ', (platePl(i).name || '').split(' ')[0]),
                  colorable && h('span', { className: 'pcaret' }, '▾')),
                dims && h('span', { className: 'pdims' }, dims),
                mc && h('span', { className: 'pmc', title: 'Многоцветный стол' }, '🎨'),
                on && h('span', { className: 'pcheck' }, icon('check', { size: 12 }))));
            })(i);
          }
          return tiles;
        })()),
      // мини-палитра выбранного стола: свой ПЛАСТИК и свой ЦВЕТ на стол (70525c99 + 65aa1a35),
      // превью перерисовывается сразу; цена считается по пластику каждого стола
      pickFor != null && (cols.length > 0 || plastics.length > 1) && (function () {
        var ppk = platePl(pickFor); var pcols = ppk.colors || [], pcss = ppk.color_css || [];
        return h('div', { className: 'card', style: { marginTop: 9, padding: 11, position: 'relative' } },
          // явный крестик закрытия панели (раньше — только повторный тап по подписи стола)
          h('button', { type: 'button', 'aria-label': 'Закрыть',
            onClick: function () { setPickFor(null); },
            style: { position: 'absolute', top: 6, right: 6, width: 30, height: 30, border: 0,
              borderRadius: 9, background: 'transparent', color: 'var(--faint)', cursor: 'pointer' } },
            icon('close', { size: 15 })),
          plastics.length > 1 && h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginBottom: 8 } },
            'Пластик стола ', pickFor + 1, ':'),
          plastics.length > 1 && h('div', { className: 'chips', style: { marginBottom: pcols.length ? 10 : 0 } },
            plastics.map(function (p) {
              var onP = ppk.id === p.id;
              return h('button', { key: p.id, className: 'chip' + (onP ? ' on' : ''), type: 'button',
                disabled: app.busy,
                onClick: function () { setPlatePlastic(pickFor, p); } },
                icon('spool', { size: 14 }), ' ', p.name,
                p.price != null && h('small', { style: { opacity: 0.75 } }, ' · ', money(p.price, o.currency), '/кг'));
            })),
          pcols.length > 0 && h('div', { style: { fontSize: 12.5, color: 'var(--muted)', marginBottom: 8 } },
            'Цвет стола ', pickFor + 1, ':'),
          pcols.length > 0 && h('div', { className: 'chips' },
            pcols.map(function (c, k) {
              var onC = plateColor(pickFor) === c;
              return h('button', { key: c, className: 'chip' + (onC ? ' on' : ''), type: 'button',
                disabled: app.busy,
                onClick: function () { setPlateColor(pickFor, c); } }, dot(pcss[k]), ' ', c);
            }),
            (plateColors[pickFor] || platePlastics[pickFor]) && h('button', { className: 'chip', type: 'button',
              disabled: app.busy,
              onClick: function () { setPlatePlastic(pickFor, null); setPickFor(null); } }, '↩ как у всех')));
      })(),
      nSel < m.plates && h('p', { className: 'hint', style: { marginTop: 8 } },
        'Посчитаем и напечатаем только выбранные столы.'),
      (cols.length > 1 || plastics.length > 1) && h('p', { className: 'hint', style: { marginTop: 6 } },
        Object.keys(platePlastics).length > 0
          ? 'У столов свои пластики — цена, коллаж и заказ это учтут.'
          : Object.keys(plateColors).length > 0
            ? 'У столов свои цвета — коллаж и заказ это учтут.'
            : 'Тапните по подписи «Стол N», чтобы дать столу свой цвет или пластик.'));
  }

  // ─────────────── Опции постобработки (B4): шлифовка, покраска, вставки ───────────────
  // Общий блок для Config («Параметры») и Quote («Смета»). На Config set = app.setCfg (правит
  // только cfg, пересчёт делает кнопка расчёта); на Quote set = app.tweak (тихий пересчёт на
  // месте, как выбор столов/цвета). Секции нет вовсе, если владелец не завёл ни одной опции.
  function PostprocSection(props) {
    var app = props.app, set = props.set, disabled = props.disabled;
    var o = app.options || {}, opts = o.options || [], cur = app.cfg.options || [];
    if (!opts.length) return null;
    function toggle(id) {
      if (disabled) return;
      var next = cur.slice(), i = next.indexOf(id);
      if (i >= 0) next.splice(i, 1); else next.push(id);
      set({ options: next });
    }
    var sel = opts.filter(function (op) { return cur.indexOf(op.id) >= 0; }).length;
    // «За-детальные» услуги (accee7a9): ставка × детали модели × тираж — как apply_options на
    // сервере. Число деталей из последней сметы (result.parts); не мерили → 1.
    var rr = (app.result && app.result.result) || {};
    var parts = Math.max(1, Number(rr.parts) || 0);
    var qq = Math.max(1, Number(app.cfg.qty) || 1);
    return h('div', { style: { marginTop: 14 } },
      h(Lab, { right: sel ? ('выбрано ' + sel) : null }, 'Постобработка'),
      h('div', { className: 'card', style: { padding: '4px 13px 8px' } },
        opts.map(function (op, i) {
          var on = cur.indexOf(op.id) >= 0;
          var eff = op.per_part ? Math.round(op.price * parts * qq * 100) / 100 : op.price;
          var mult = op.per_part && (parts * qq) > 1
            ? (Number(op.price) || 0).toFixed(2).replace('.', ',') + ' × ' + (parts * qq) + ' дет.'
            : (op.per_part ? 'за деталь' : '');
          return h('button', { key: op.id, type: 'button', disabled: disabled,
            style: { display: 'flex', alignItems: 'center', gap: 11, width: '100%', textAlign: 'left',
              padding: '8px 6px', borderTop: i ? '1px solid var(--line)' : 'none', background: on ? 'var(--accent-soft)' : 'transparent',
              color: 'var(--text)', border: 0, borderRadius: 8, cursor: disabled ? 'default' : 'pointer' },
            onClick: function () { toggle(op.id); } },
            h('span', { className: 'dotbtn' + (on ? ' on' : ''), style: { flex: '0 0 auto', pointerEvents: 'none',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                background: on ? 'var(--accent)' : 'transparent', color: on ? '#1a1205' : 'inherit' } },
              on ? icon('check', { size: 22, sw: 3 }) : null),
            h('span', { style: { flex: 1, minWidth: 0, fontSize: 13.5, whiteSpace: 'normal' } }, op.name,
              mult ? h('span', { style: { display: 'block', fontSize: 11.5, color: 'var(--faint)' } }, mult) : null),
            eff > 0
              ? h('span', { className: 'mono', style: { flex: '0 0 auto', color: 'var(--price)', fontWeight: 700 } }, '+' + money(eff, o.currency))
              : h('span', { style: { flex: '0 0 auto', color: 'var(--faint)', fontSize: 12.5 } }, 'включено'));
        })),
      h('p', { className: 'hint', style: { marginTop: 4 } },
        'Ручные услуги: выполняются мастером после печати. Цена добавится к заказу.'));
  }

  // ──────────────────────── CONFIG ───────────────────────
  function Config(props) {
    var app = props.app, o = app.options || {}, s = app.cfg, set = app.setCfg, m = app.model || {};
    var plastics = o.plastics || [], quals = o.qualities || [], strs = o.strengths || [];
    var pl = plastics.filter(function (p) { return p.id === s.plastic_id; })[0] || plastics[0] || {};
    var cols = pl.colors || [], colCss = pl.color_css || [];

    function dot(css) {
      return h('span', { className: 'dot-ring', style: { border: 0, background: css || '#888' } });
    }
    // css выбранного цвета/слотов → проп colors инлайн-3D и модалки: пикер под моделью красит меш
    // в РЕАЛЬНОМ времени (одноцвет — все детали в один цвет; мультицвет — деталь i в colors_sel[i]).
    function _cssOf(name) { var i = cols.indexOf(name); return i >= 0 ? (colCss[i] || '#9aa3ad') : '#9aa3ad'; }
    var isMC = !!(m.multicolor && s.multicolor);
    var v3dColors = isMC ? (s.colors_sel || []).map(_cssOf) : [_cssOf(s.color)];
    var spS = useState(null); var slotPick = spS[0], setSlotPick = spS[1]; // слот мультицвета, которому меняем цвет (a197bb93)
    // 3D-просмотр модели (B1): локальный флаг открытия вьюера, как lbSrc в Quote.
    var v3S = useState(false); var view3d = v3S[0], setView3d = v3S[1];
    // Качество/прочность клиент больше не настраивает (8c8199d1): печатаем лучшим профилем,
    // который мастер закрепил за пластиком (94cc52c7); вместо них — поле «Особые пожелания».

    return h('div', { className: 'screen' },
      // Мультиплейт: общую картинку ЗАМЕЩАЕТ сетка столов (eb4424d9) — выбор и есть превью.
      // Одностольный: 3D — ПЕРВИЧНЫЙ превью (инлайн, в той же footprint), с PNG-фолбэком внутри;
      // если модель непросматриваема (STEP/тяжёлая/нет WebGL) — ровно сегодняшний PNG-путь.
      m.plates <= 1 && (window.PS_Inline3D && window.ps3dCanShow && ps3dCanShow(m)
        ? h('div', { className: 'preview is3d' },
            h(window.PS_Inline3D, { model: m, fmt: window.ps3dFmt ? window.ps3dFmt(m) : '',
              url: API.modelFile(m.token), dims: m.dims, png: m.preview,
              colors: v3dColors, multicolor: isMC,
              onExpand: function () { setView3d(true); }, onFallback: function () {} }))
        : h('div', { className: 'preview' },
            h(PreviewImg, { src: m.preview, key: m.preview }),
            m.dims && h('span', { className: 'dimtag' }, m.dims, ' мм'),
            // кнопка «3D» поверх превью (bottom-right) — сама решает, показываться ли (формат+WebGL)
            window.PS_View3DButton && h(window.PS_View3DButton, { model: m, onOpen: function () { setView3d(true); } }))),
      // Полноэкранный вьюер: PNG остаётся на экране Config под ним, поэтому при сбое 3D просто
      // закрываемся (onFallbackToPng не нужен — картинка уже видна).
      view3d && window.PS_Viewer3D && h(window.PS_Viewer3D, {
        token: m.token, url: API.modelFile(m.token), fmt: window.ps3dFmt ? window.ps3dFmt(m) : '',
        dims: m.dims || '', colors: v3dColors, multicolor: isMC, onClose: function () { setView3d(false); } }),

      // ПИКЕР ЦВЕТА — ПРЯМО ПОД 3D (редизайн владельца 2026-07-23): выбор цвета мгновенно
      // перекрашивает модель выше (проп colors → setColors, без переслайса). Пластик/количество/
      // срочность/пожелания — НИЖЕ. Опции цвета берутся из выбранного пластика (pl/cols/colCss).
      h('div', { className: 'color-under3d' },
        // одиночный «Цвет» прячем, когда включены слоты мультицвета — они его заменяют (a197bb93)
        !(m.multicolor && s.multicolor) && cols.length > 0 && h(Lab, null, m.plates > 1 ? 'Цвет (для всех столов)' : 'Цвет'),
        !(m.multicolor && s.multicolor) && cols.length > 0 && h('div', { className: 'chips' }, cols.map(function (c, i) {
          var isOut = (pl.out_of_stock || []).indexOf(c) >= 0;     // цвет на нуле склада — гасим
          var isLow = (pl.low_stock || []).indexOf(c) >= 0;
          return h('button', { key: c, disabled: isOut, className: 'chip' + (s.color === c ? ' on' : '') + (isOut ? ' oos' : ''),
            // смена общего цвета сбрасывает пер-стольные (предсказуемо: «для всех столов»)
            onClick: isOut ? undefined : function () { set({ color: c, plate_colors: {} }); } },
            dot(colCss[i]), ' ', c, isOut ? h('small', null, ' · нет') : (isLow ? h('small', { style: { color: 'var(--amber)' } }, ' · мало') : null));
        })),

        // Мультицвет (a197bb93): слоты «Цвет 1..N» с кружками; тап по кружку раскрывает палитру пластика.
        m.multicolor && h(Lab, null, 'Цвета печати'),
        m.multicolor && h('div', { className: 'chips' },
          h('button', { className: 'chip' + (s.multicolor ? ' on' : ''), onClick: function () {
            var n = Math.max(2, m.num_colors || 2);
            // приоритет слотов: цвета ДИЗАЙНА из .3mf (mc_colors → ближайший в палитре,
            // PS_NearestColor из app.js), затем первые цвета палитры — как в applyModel
            var init = (s.colors_sel && s.colors_sel.length === n) ? s.colors_sel
              : Array.from({ length: n }, function (_, i) {
                  var hx = (m.mc_colors || [])[i];
                  var nm = (hx && window.PS_NearestColor) ? window.PS_NearestColor(pl, hx) : null;
                  return nm || cols[i % Math.max(1, cols.length)] || '';
                });
            set({ multicolor: true, colors_sel: init });
          } }, icon('palette', { size: 15 }), ' Многоцветная · ' + m.num_colors + ' цв.'),
          h('button', { className: 'chip' + (!s.multicolor ? ' on' : ''), onClick: function () { set({ multicolor: false }); } },
            '⚪ Одноцветная ', h('small', null, '(дешевле)'))),
        m.multicolor && s.multicolor && h('div', { className: 'card', style: { padding: '4px 13px 8px' } },
          (s.colors_sel || []).map(function (cName, i) {
            var ci = cols.indexOf(cName);
            return h(React.Fragment, { key: i },
              h('div', { style: { display: 'flex', alignItems: 'center', gap: 11, padding: '8px 0', borderTop: i ? '1px solid var(--line)' : 'none' } },
                h('span', { style: { fontSize: 13, color: 'var(--muted)', flex: '0 0 auto', width: 56 } }, 'Цвет ' + (i + 1)),
                h('button', { type: 'button', className: 'dotbtn' + (slotPick === i ? ' on' : ''),
                  title: cName || 'выбрать цвет', 'aria-label': 'Цвет ' + (i + 1) + (cName ? ': ' + cName : ''),
                  style: { background: ci >= 0 ? colCss[ci] : '#8a97a5' },
                  onClick: function () { setSlotPick(slotPick === i ? null : i); } }),
                h('span', { style: { fontSize: 13.5, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, cName || '—'),
                slotPick !== i && h('span', { style: { marginLeft: 'auto', color: 'var(--faint)', fontSize: 11.5, flex: '0 0 auto' } }, 'тап по кружку — сменить')),
              slotPick === i && h('div', { className: 'chips', style: { padding: '2px 0 8px' } }, cols.map(function (c, k) {
                var isOut = (pl.out_of_stock || []).indexOf(c) >= 0;
                return h('button', { key: c, disabled: isOut, className: 'chip' + (cName === c ? ' on' : '') + (isOut ? ' oos' : ''),
                  onClick: isOut ? undefined : function () {
                    var n = (s.colors_sel || []).slice(); n[i] = c;
                    set({ colors_sel: n, color: n[0] || s.color, plate_colors: {} });
                    setSlotPick(null);
                  } }, dot(colCss[k]), ' ', c, isOut ? h('small', null, ' · нет') : null);
              })));
          }))),

      h('div', { style: { fontWeight: 700, fontSize: 17, margin: '12px 2px 0' } }, m.label || 'Модель'),

      // выбор столов — общий блок со сметой (71e15fe6); тут set просто правит cfg
      h(PlatePicker, { app: app, set: set }),

      h(Lab, null, 'Пластик'),
      h('div', { className: 'chips' },
        plastics.map(function (p) {
          // смена пластика: сброс пер-стольных + лучший профиль печати этого пластика (94cc52c7)
          var dq = quals.filter(function (q) { return q.is_default; })[0] || quals[0] || {};
          return h('button', { key: p.id, className: 'chip' + (s.plastic_id === p.id ? ' on' : ''),
            onClick: function () {
              var pal = p.colors || [];
              // лучший профиль мастера побеждает, иначе СОХРАНЯЕМ выбор ползунка высоты
              // слоя (03.08.2026) — раньше смена пластика молча сбрасывала на дефолт
              set({ plastic_id: p.id, color: pal[0] || '',
                quality_id: p.best_quality_id || s.quality_id || dq.id,
                plate_plastics: {}, plate_colors: {},
                // слоты мультицвета (a197bb93): палитра нового пластика с первых цветов
                colors_sel: m.multicolor ? Array.from({ length: Math.max(2, m.num_colors || 2) },
                  function (_, i) { return pal[i % Math.max(1, pal.length)] || ''; }) : [] });
            } },
            icon('spool', { size: 15 }), ' ', p.name);
        })),

      // Карточка выбранного пластика: имя, цена за кг (зелёным) и описание — над графой «Цвет».
      pl.name && h('div', { className: 'plcard' },
        h('div', { className: 'plcard-top' },
          h('div', { className: 'plcard-name' }, icon('spool', { size: 16, style: { color: 'var(--accent)' } }), ' ', pl.name),
          pl.price != null && h('div', { className: 'plcard-price' }, money(pl.price, o.currency), h('small', null, '/кг'))),
        pl.description && h('div', { className: 'plcard-desc' }, pl.description)),

      // (Пикер цвета ПЕРЕНЕСЁН ВЫШЕ — прямо под 3D-модель, редизайн владельца 2026-07-23.)

      // Ползунок высоты слоя (03.08.2026): стопы — активные пресеты качества из options
      // (0.12…0.36), 0.30/0.36 идут процессами сопла 0.6 (бэйдж). Явный выбор сервер
      // отличает от дефолта сам (fastcalc._quality_is_explicit) — на проект-3mf он
      // подменяет слой автора, дефолт оставляет прежнее поведение и прежние цены.
      quals.length >= 2 && (function () {
        var qi = -1;
        quals.forEach(function (q, i) { if (q.id === s.quality_id) qi = i; });
        if (qi < 0) { quals.forEach(function (q, i) { if (q.is_default) qi = i; }); }
        if (qi < 0) qi = 0;
        var q = quals[qi] || {};
        var noz = q.nozzle || '0.4';
        return h(React.Fragment, null,
          h(Lab, null, 'Высота слоя'),
          h('div', { className: 'card', style: { padding: '10px 13px 12px' } },
            h('div', { style: { display: 'flex', alignItems: 'baseline', gap: 8 } },
              h('span', { className: 'mono', style: { fontSize: 19, fontWeight: 700 } },
                (+q.layer_height || 0.2).toFixed(2), h('small', { style: { fontWeight: 500 } }, ' мм')),
              h('span', { style: { color: 'var(--muted)', fontSize: 12.5, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
                (q.label || '').split(' — ')[0]),
              h('span', { className: 'mono', style: { marginLeft: 'auto', flex: '0 0 auto', fontSize: 11.5, fontWeight: 700,
                  color: noz === '0.6' ? 'var(--accent)' : 'var(--faint)',
                  border: '1px solid ' + (noz === '0.6' ? 'var(--accent)' : 'var(--line)'),
                  borderRadius: 7, padding: '2px 7px' } }, 'сопло ' + noz)),
            h('input', { type: 'range', className: 'lh-range', min: 0, max: quals.length - 1, step: 1,
              value: qi, 'aria-label': 'Высота слоя',
              onChange: function (e) { var k = quals[+e.target.value]; if (k) set({ quality_id: k.id }); } }),
            h('div', { className: 'lh-ticks' }, quals.map(function (k, i) {
              return h('button', { key: k.id, type: 'button', className: 'lh-tick mono' + (i === qi ? ' on' : ''),
                onClick: function () { set({ quality_id: k.id }); } }, (+k.layer_height).toFixed(2));
            }))),
          h('p', { className: 'hint', style: { marginTop: 4 } },
            'Тоньше слой — аккуратнее поверхность, но дольше печать. 0.30 и 0.36 мм печатаем соплом 0.6.'));
      })(),

      h(Lab, null, 'Количество, шт'),
      h(QtyControl, { value: s.qty, onSet: function (v) { set({ qty: v }); } }),

      // Масштаб модели: пресеты + свой %. Габариты пересчитает слайс; если запрошенный масштаб
      // не влезает на стол — сервер уменьшит до максимума и фронт покажет тост (app.js doCalc).
      h(Lab, null, 'Масштаб модели'),
      h('div', { className: 'chips' },
        [50, 75, 100, 125, 150, 200].map(function (p) {
          return h('button', { key: p, className: 'chip' + ((s.scale_pct || 100) === p ? ' on' : ''),
            onClick: function () { set({ scale_pct: p }); } }, p + '%');
        }).concat([
          h('span', { key: 'custom', style: { display: 'inline-flex', alignItems: 'center', gap: 4, flex: '0 0 auto' } },
            h('input', { className: 'input mono', type: 'number', inputMode: 'numeric',
              min: 10, max: 1000, value: s.scale_pct || 100,
              style: { width: 84, marginTop: 0, textAlign: 'center', padding: '13px 10px' },
              onChange: function (e) {
                var v = Math.max(10, Math.min(1000, parseInt(e.target.value, 10) || 100));
                set({ scale_pct: v });
              } }),
            h('span', { className: 'mono', style: { color: 'var(--faint)', fontSize: 13 } }, '%'))
        ])),
      (function () {
        var pct = s.scale_pct || 100;
        if (pct === 100 || !m.dims) return null;
        var mm = /(\d+)\D+(\d+)\D+(\d+)/.exec(m.dims);
        if (!mm) return null;
        var d = [1, 2, 3].map(function (i) { return Math.round(+mm[i] * pct / 100); });
        return h('p', { className: 'hint', style: { marginTop: 6 } },
          '≈ ' + d.join('×') + ' мм при ' + pct + '%. Не влезет на стол — уменьшим до максимума.');
      })(),

      // Срок и срочность (B3): два взаимоисключающих чипа — тот же паттерн, что тумблер
      // «Многоцветная/Одноцветная». В Config set только правит cfg.urgent, цену посчитает
      // кнопка «Рассчитать» внизу. Конкретную дату «к DD.MM» здесь больше НЕ показываем — срок
      // теперь зависит от времени печати, а оно до расчёта неизвестно (точный срок появится в смете).
      h(Lab, null, 'Срок и срочность'),
      h('div', { className: 'chips' },
        h('button', { className: 'chip' + (!s.urgent ? ' on' : ''), onClick: function () { set({ urgent: false }); } },
          '🐢 Обычно'),
        h('button', { className: 'chip' + (s.urgent ? ' on' : ''), onClick: function () { set({ urgent: true }); } },
          icon('bolt', { size: 15 }), ' Срочно', o.urgent_k ? (' ×' + o.urgent_k) : '')),
      h('p', { className: 'hint', style: { marginTop: 4 } },
        'Срочные заказы идут в начало очереди' + (o.urgent_k ? ' — наценка ×' + o.urgent_k : '') + '.'),

      // «Особые пожелания» вместо выбора качества/прочности (8c8199d1): печатаем профилем
      // мастера, особые случаи клиент пишет словами — мастер учтёт при подтверждении.
      h(Lab, null, 'Особые пожелания'),
      h('textarea', { className: 'input', rows: 2, value: s.comment || '',
        placeholder: 'Необязательно: нужен к пятнице, попрочнее, печать потоньше…',
        style: { resize: 'none', fontFamily: 'inherit' },
        onChange: function (e) { set({ comment: e.target.value }); } }),
      h('p', { className: 'hint', style: { marginTop: 4 } },
        'Заполнение и прочность — профилем мастера; высота слоя — по ползунку выше.'),

      // Постобработка (B4): на «Параметрах» set только правит cfg — цену посчитает кнопка расчёта.
      h(PostprocSection, { app: app, set: set }),

      // скрыто, см. задачу f3332d8c — промокоды временно не нужны; раскомментировать когда понадобятся
      // h(Lab, null, 'Промокод (если есть)'),
      // h('input', { className: 'input mono', placeholder: 'КОД', value: s.promo,
      //   onChange: function (e) { set({ promo: e.target.value.toUpperCase() }); } }),
      h('div', { style: { height: 8 } }));
  }

  // ──────────────────────── SLICING ──────────────────────
  // Тот же оверлей-спиннер используется и для загрузки модели по ссылке/файлу
  // (props.loading) — чтобы юзер видел прогресс и не жал кнопку повторно.
  // Забавные факты о 3D-печати на экране ожидания: на нарезке — сразу, при загрузке
  // модели — после 10 c. Порядок каждый раз случайный (shuffled при монтировании).
  var FACTS = [
    'Первый слой — как первое впечатление: не прилип — дальше можно не стараться.',
    '3D-принтер не «глючит», он «экспериментирует с формой».',
    'Спагетти на столе — бонус-режим, который никто не просил.',
    'Идеальная печать существует. Её видели только в рекламе филамента.',
    'Запах PLA по утрам — это запах продуктивности.',
    '«Ещё 2 минуты» у принтера — как «уже выхожу» у людей.',
    'Поддержки убираются легко. В параллельной вселенной.',
    'Каждый мейкер уверен, что ЭТА модель точно не отклеится.',
    'Адгезия — это любовь детали к столу. Иногда несчастная.',
    'Коробка с обрезками поддержек — пенсионный фонд мейкера.',
    'Калибровка стола — медитация, доступная каждому.',
    'Benchy печатают не ради лодочки, а ради спокойствия в душе.',
    '0.2 мм — это и высота слоя, и толщина терпения мейкера.',
    'Филамент закончился на 98%. Классика жанра.',
    'Лучшая поддержка — та, которую не пришлось ставить.',
    'PLA делают из кукурузы — почти салат, только не едят.',
    'Слайсер режет модель на сотни слоёв, чтобы собрать её обратно.',
    'Сопло 0.4 мм — стандарт, к которому все привыкли и боятся менять.',
    'Первый 3D-принтер был размером с холодильник и стоил как машина.',
    'RepRap мечтал печатать сам себя. Гайки пока приходится докупать.',
    'Скорость печати измеряется в мм/с и в нервных клетках мейкера.',
    'ABS пахнет так, что соседи узнают о вашем хобби первыми.',
    'Ретракт придумали, чтобы между деталями не висели сопли филамента.',
    'PETG липнет ко всему. Кроме того, к чему должен прилипнуть.',
    'Brim — это юбочка детали, чтобы та крепче держалась за стол.',
    'Раскалённое сопло — 200 °C. Палец проверяет это только один раз.',
    'Каждый мейкер хоть раз печатал подставку для самого принтера.',
    'Засор сопла всегда случается на последнем проценте печати.',
    'Слой за слоем — так и пирамиды бы строили, будь у фараонов принтер.',
    'TPU гибкий, как обещания «напечатаю за пять минут».',
    'Сушилка для филамента — потому что влажный пластик стреляет и пузырится.',
    'Идеальный обдув — это когда деталь не плывёт, а сопло не остывает.',
    'Картинка в слайсере красивая. Реальность вносит коррективы.',
    'Запас филамента у мейкера всегда «ещё на одну катушку больше нужного».',
    'Warping — когда углы детали решили загнуться вверх и помахать вам.',
    'Любой принтер можно улучшить. Особенно деталями, напечатанными на нём.',
    'Стол с подогревом — чтобы пластик передумал отклеиваться.',
    '«Просто скачаю модель» — и вот ты уже три часа в каталоге.',
    'Заполнение 100% — для тех, кто не верит в пустоту внутри.',
    'Соты внутри детали — прочно, экономно и приятно глазу.',
    'G-code — это маршрутный лист сопла на пару тысяч строк.',
    'Хороший прайминг-лайн бережёт нервы на старте печати.',
    'Микрошаги моторов — вот почему всё это так плавно жужжит.',
    'Преднатяг ремней проверяют на слух, как струны гитары.',
    'Смола красиво, но перчатки и вытяжка — не обсуждается.',
    'Z-wobble — когда слои гуляют волной, а вы ищете кривой винт.',
    'Печать ночью — лотерея: проснёшься к детали или к спагетти.',
    'Octoprint позволяет следить за принтером, не вставая с дивана.',
    'Любой провал печати — это «бесплатный урок по настройке».',
    'Сменил филамент — будь готов снова искать идеальную температуру.',
    'Эх, ещё бы второй принтер — печатать запчасти для первого.',
    'Линии слоёв — это не дефект, это фирменная текстура 3D-печати.',
    'Слишком быстрый ретракт — и сопло начинает щёлкать, как кастаньеты.',
    'Поднял скорость вдвое — качество вспомнило про физику.',
    'Хороший первый слой выглядит как ровный асфальт, а не как поле.',
    'Катушка всегда запутывается именно в самый ответственный момент.',
    'Сопло из закалённой стали — для тех, кто печатает абразивами.',
    'Печатать ночник в виде Луны — почти обязательный ритуал мейкера.',
    'Сначала калибруешь принтер, потом он калибрует твоё терпение.',
    'Чем тоньше слой, тем дольше печать и тем глаже легенда о тебе.',
  ];

  // Task B: шуточные «статусы» во время ЗАГРУЗКИ модели с сайта — крутятся каждые ~3.5 c.
  // Это НЕ образовательные факты (FACTS) и НЕ про печать: тема — добыча файла модели из
  // интернета (Cloudflare, капча, подписанные ссылки, тяжёлые архивы). Нарочито абсурдные,
  // для настроения, пока качается модель.
  var JOKES = [
    'Доказываю сайту, что я не робот (а я вообще-то робот)…',
    'Перепрыгиваю через Cloudflare, стараясь не задеть турникет…',
    'Разгадываю капчу: тыкаю во все картинки со светофорами…',
    'Уговариваю сервер отдать файл по-хорошему…',
    'Притворяюсь настоящим браузером, чтобы меня впустили…',
    'Качаю модель по проводам из самого глубокого интернета…',
    'Договариваюсь с подписанной CDN-ссылкой о коротком свидании…',
    'Распаковываю архив, а в нём архив, а в нём ещё архив…',
    'Прошу файл весить чуть меньше — он гордо отказывается…',
    'Принимаю модель по байтику, как посылку на почте…',
    'Вылавливаю нужную уточку из океана одинаковых уточек…',
    'Жду, пока сервер проснётся и заварит себе кофе…',
    'Тяну файл через половину планеты по тонкому проводку…',
    'Тихонько конвертирую .3mf в STL, пока никто не видит…',
    'Сверяю хеш, чтобы вместо модели не подсунули кота…',
    'Объясняю Telegram, что 20 МБ — это вообще-то немного…',
    'Ищу .stl, который автор спрятал в десятой папке…',
    'Перебираю 47 расцветок, которые автор зачем-то залил…',
    'Качаю, пока интернет в хорошем настроении…',
    'Достаю модель из недр сайта, как археолог — черепок…',
    'Жму F5 на сервере силой мысли…',
    'Проверяю, что скачался файл, а не страница «ошибка 404»…',
    'Прорываюсь сквозь баннеры «купи подписку» к бесплатной модели…',
    'Ловлю последний байт — он всегда самый шустрый…',
    'Шепчу серверу волшебное слово «пожалуйста»…',
    'Убеждаю модель, что у нас ей будет хорошо…',
    'Сканирую ссылку на скрытые платные стены…',
    'Беру файл под расписку у капризного хостинга…',
    'Распутываю редирект, ведущий на ещё три редиректа…',
  ];

  // Перемешивание копии массива (Фишер–Йейтс). Math.random в браузере разрешён —
  // запрет на random действует только для скрипта-оркестратора, не для кода приложения.
  function shuffled(arr) {
    var a = arr.slice();
    for (var i = a.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var t = a[i]; a[i] = a[j]; a[j] = t;
    }
    return a;
  }

  var ROTATE_EVERY = 10;   // c — единый медленный темп ротации (раньше шутки мельтешили каждые 3.5 c)

  function Slicing(props) {
    // Свой тик: гарантирует ротацию подсказок и на загрузке модели (props.loading),
    // где родитель не гоняет props.secs. Видимый счётчик «N c» по-прежнему из props.secs.
    var ts = useState(0); var tick = ts[0], setTick = ts[1];
    // shuffled один раз на монтирование — порядок случайный при каждом показе оверлея,
    // но стабильный в течение этого показа.
    var fs = useState(function () { return shuffled(FACTS); }); var facts = fs[0];
    var js = useState(function () { return shuffled(JOKES); }); var jokes = js[0];
    // Один блок подсказок: режим 'fact' (💡 факт) или 'joke' (😂 шутка). По умолчанию на загрузке —
    // шутки (они как раз про саму загрузку), на нарезке — факты. Иконка-переключатель = пасхалка:
    // тап по иконке меняет режим (💡↔😂), тап по строке — следующая подсказка (если читаешь быстро).
    var ms = useState(props.loading ? 'joke' : 'fact'); var mode = ms[0], setMode = ms[1];
    var bs = useState(0); var bump = bs[0], setBump = bs[1];
    React.useEffect(function () {
      var t0 = Date.now();
      var id = setInterval(function () { setTick((Date.now() - t0) / 1000); }, 250);
      return function () { clearInterval(id); };
    }, []);

    var isJoke = mode === 'joke';
    var arr = isJoke ? jokes : facts;
    var idx = Math.floor(tick / ROTATE_EVERY) + bump;
    var line = arr[((idx % arr.length) + arr.length) % arr.length];
    function toggle(e) { if (e && e.stopPropagation) e.stopPropagation(); setMode(isJoke ? 'fact' : 'joke'); setBump(function (b) { return b + 1; }); }
    function advance() { setBump(function (b) { return b + 1; }); }

    return h('div', { className: 'slicing' },
      // Слайс — принтер печатает Benchy; загрузка модели — «облако → устройство» (5f6de0a7).
      props.loading ? h(CloudLoader, null) : h(PrinterLoader, null),
      props.loading
        ? h('div', { className: 'msg' }, h('b', null, 'Загружаю модель'), 'Получаю файл и готовлю превью — это займёт несколько секунд.')
        : h('div', { className: 'msg' }, h('b', null, 'Нарезаю модель и считаю цену'), 'Это займёт некоторое время — слайсер обсчитывает каждый слой.'),
      !props.loading && h('div', { className: 'nozzle' }, 'ElegooSlicer · ', props.secs, ' c'),
      // на загрузке модели тоже показываем счётчик (аудит UX: без него ожидание = «зависло»)
      props.loading && h('div', { className: 'nozzle' }, Math.round(tick), ' c'),
      !props.loading && props.est && h('div', { className: 'nozzle', style: { color: 'var(--price)' } }, 'Ориентир: от ', props.est),
      // Единый блок: иконка-переключатель 💡/😂 + текст; тап по строке — следующая подсказка.
      h('div', { className: 'tipline ' + (isJoke ? 'joke' : 'fact'), key: mode + idx, onClick: advance, title: 'Нажмите, чтобы обновить' },
        h('button', { className: 'tipicon', onClick: toggle, title: isJoke ? 'Показать факт' : 'Показать шутку' }, isJoke ? '😂' : '💡'),
        h('span', { className: 'tiptext' }, line)),
      // «Оставить заказ» прямо во время слайса (OpenTodo f1e80d67): заявка создаётся МГНОВЕННО,
      // ждать нарезку не нужно — цену проставит фоновый расчёт, а мастер уже видит заказ.
      !props.loading && props.onOrderNow && h('button', { className: 'btn', style: { marginTop: 16, width: 'auto', padding: '12px 20px' }, disabled: props.busy,
        onClick: function (e) { if (e && e.stopPropagation) e.stopPropagation(); if (props.busy) return; props.onOrderNow(); } },
        icon('receipt', { size: 18 }), props.busy ? ' Оформляю…' : ' Оставить заказ сейчас'),
      // «не ждать»: бот пришлёт цену сообщением, можно выйти или считать ещё (ae4c6c13)
      !props.loading && props.onNotify && h('button', { className: 'btn line', style: { marginTop: 10, width: 'auto', padding: '11px 18px' },
        onClick: function (e) { if (e && e.stopPropagation) e.stopPropagation(); props.onNotify(); } },
        '🔔 Уведомить, когда посчитается'));
  }

  // Блок «размер против банковской карты» (idea #6, ScaleRef) удалён по фидбэку владельца
  // 2026-07-10: на смете читался как непонятный мусор; габариты и так на бейдже превью.

  // ───────────────────────── QUOTE / карточка товара ───────────────────────
  // ЕДИНЫЙ экран сметы и карточки товара (фидбэк владельца 2026-07-11: «всё должно быть
  // доступно сразу» — цвета и кнопки заказа видны с первого кадра, а не после слайса).
  // Три состояния: !ready && !failed — цена «считаем…»/старая, рендер-шиммер, контролы уже
  // на месте; ready — обычная смета; failed — инлайн-блок «не получилось» + заказ без цены.
  // Один компонент вместо пары ProductQuote/Quote заодно лечит мигание фото: раньше на приходе
  // цены дерево перемонтировалось целиком, и картинка товара перезагружалась с анимацией.
  // Печатопригодность (B2, DFM): цвет статуса проверки. warn — янтарь (оценка/риск), bad — красный
  // (не влезет). Токены существующие (app.css:24-30), ничего нового не вводим.
  function dfmColor(status) { return status === 'bad' ? 'var(--red)' : 'var(--amber)'; }

  // Плашка-предупреждение DFM: заметная, но не пугающая карточка для risky/wont_fit над контролами.
  // OK/пусто — рендерит null (тихий стейт, только пункт в specrow). НИЧЕГО не блокирует — заказ
  // остаётся доступен при любом вердикте (B2 только предупреждает). Раскрытие проверок — тот же
  // toggle-паттерн, что «Из чего складывается цена». Рычаг «Уменьшить до N%» — scale-to-fit через
  // app.tweak (scale_max_pct из расчёта), как «Сделать дешевле».
  function DfmBanner(props) {
    var r = props.r, app = props.app;
    var es = useState(false); var open = es[0], setOpen = es[1];
    var v = r.verdict;
    if (v !== 'risky' && v !== 'wont_fit') return null;   // ok/нет вердикта — тихо
    var dfm = r.dfm || [];
    var wont = v === 'wont_fit';
    var col = wont ? 'var(--red)' : 'var(--amber)';
    var soft = wont ? 'var(--red-soft)' : 'var(--amber-soft)';
    var head = wont ? 'Не поместится на стол' : 'Возможны сложности с печатью';
    var lead = (dfm[0] && dfm[0].detail) || '';
    var canScale = wont && r.scale_max_pct && r.scale_max_pct < 100;
    return h('div', { className: 'card', style: { marginTop: 14, borderColor: soft } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
        icon(wont ? 'bed' : 'info', { size: 18, style: { color: col } }),
        h('span', { style: { fontWeight: 700, fontSize: 15, color: col } }, head),
        // при approx/refining вердикт предварительный — маленькая приписка, без отдельного элемента
        (r.approx || r.refining) ? h('span', { style: { marginLeft: 'auto', fontSize: 11.5, color: 'var(--faint)' } }, 'предварительно') : null),
      lead && h('div', { style: { marginTop: 6, fontSize: 13, color: 'var(--muted)', lineHeight: 1.4 } }, lead),
      // рычаг scale-to-fit — только для «не влезет» и если расчёт дал допустимый масштаб под стол
      canScale && h('button', { className: 'lever', type: 'button', style: { marginTop: 11 },
        disabled: app.busy || app.recalcPending,
        onClick: function () { if (app.busy || app.recalcPending) return; app.tweak({ scale_pct: r.scale_max_pct }); } },
        icon('sliders', { size: 15 }), h('span', null, 'Уменьшить до ', h('b', null, r.scale_max_pct + '%')),
        h('em', null, 'под стол')),
      // «Подробнее» — раскрытие построчных проверок (прозрачность вместо чёрного ящика)
      dfm.length > 0 && h('button', { onClick: function () { setOpen(!open); },
        style: { marginTop: 11, display: 'inline-flex', alignItems: 'center', gap: 6, background: 'transparent',
          border: 0, padding: '0 2px', cursor: 'pointer', color: 'var(--muted)', fontSize: 12.5, fontWeight: 600 } },
        open ? 'Скрыть проверки' : 'Подробнее',
        icon('chev', { size: 13, style: { transform: open ? 'rotate(-90deg)' : 'rotate(90deg)', transition: 'transform .15s' } })),
      open && h('div', { className: 'card', style: { marginTop: 9 } },
        h('div', { className: 'bd' }, dfm.map(function (row, i) {
          return h('div', { key: row.key || i, className: 'r' },
            h('span', { className: 'k', style: { display: 'flex', alignItems: 'center', gap: 8, flex: '0 0 auto' } },
              h('span', { style: { width: 9, height: 9, borderRadius: '50%', background: dfmColor(row.status), flex: '0 0 auto' } }),
              row.title),
            h('span', { className: 'v', style: { color: dfmColor(row.status), textAlign: 'right', maxWidth: '58%', fontFamily: 'inherit', fontWeight: 500 } }, row.detail));
        })),
        h('p', { className: 'hint', style: { marginTop: 4 } }, 'Оценка по геометрии — мастер проверит перед печатью')));
  }

  // Строка доставки на смете (A2): показывает, куда и почём уедет заказ, если клиент задал
  // адрес по умолчанию в профиле. Ничего не рисует, если доставка выключена мастерской или
  // умолчаний нет — тогда заказ, как и раньше, самовывоз.
  function QuoteDelivery(props) {
    var app = props.app;
    var cfg = (app.options && app.options.delivery) || {};
    if (cfg.enabled === false || !cfg.default_type_id || !cfg.default_address) return null;
    var t = (cfg.types || []).filter(function (x) { return x.id === cfg.default_type_id; })[0];
    if (!t) return null;
    // Порог «бесплатно от суммы» считаем ровно как сервер (delivery.type_cost): нестрогое >=.
    // Итог со сметы приходит форматированной строкой («12,30 руб.») — разбираем её локально:
    // parseMoney живёт в screens_more.js внутри своего IIFE и сюда не видна.
    var sum = parseFloat(String(props.total || '').replace(/[^\d,.-]/g, '').replace(',', '.')) || 0;
    var free = t.free_from > 0 && sum >= t.free_from;
    var cost = free ? 0 : (t.price || 0);
    return h('div', { className: 'card', style: { marginTop: 12, padding: 12 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
        h('span', { style: { flex: 1, minWidth: 0 } },
          h('b', { style: { fontSize: 13.5 } }, '🚚 ', t.name),
          h('span', { className: 'sub', style: { display: 'block', marginTop: 2 } }, cfg.default_address)),
        h('b', { className: 'mono', style: { color: cost > 0 ? 'var(--price)' : 'var(--ok, #3a9)', flex: '0 0 auto' } },
          cost > 0 ? API.fmt(cost) : 'бесплатно')),
      cost > 0 && h('div', { className: 'mono', style: { marginTop: 8, fontSize: 13 } },
        'Итого с доставкой: ', h('b', { style: { color: 'var(--price)' } }, API.fmt(sum + cost))),
      h('p', { className: 'hint', style: { marginTop: 6 } },
        'Адрес из профиля — изменить можно там же.'));
  }

  function Quote(props) {
    var app = props.app, product = app.product;
    var res = app.result;
    var failed = !!(res && res.ok === false);   // тихий фейл слайса карточки (55a1cf8f)
    var ready = !!(res && res.ok);              // смета (мгновенная или честная) уже есть
    var r = (ready && res.result) || {}, m = app.model || {};
    var cur = (app.options && app.options.currency) || 'руб.';
    var lines = r.lines || [];
    // css выбранного цвета/слотов → проп colors инлайн-3D и модалки: пикер под моделью красит меш
    // в РЕАЛЬНОМ времени (одноцвет — все детали в один цвет; мультицвет — деталь i в colors_sel[i]).
    var _qps = (app.options && app.options.plastics) || [];
    var _qpl = _qps.filter(function (p) { return p.id === app.cfg.plastic_id; })[0] || {};
    var _qcols = _qpl.colors || [], _qcss = _qpl.color_css || [];
    function _qCssOf(name) { var i = _qcols.indexOf(name); return i >= 0 ? (_qcss[i] || '#9aa3ad') : '#9aa3ad'; }
    var qIsMC = !!(m.multicolor && app.cfg.multicolor);
    var v3dColors = qIsMC ? (app.cfg.colors_sel || []).map(_qCssOf) : [_qCssOf(app.cfg.color)];
    // разбивка цены свёрнута по умолчанию (ux-simplification.md, идея 4): крупно — цена и срок
    var sb = useState(false); var showBd = sb[0], setShowBd = sb[1];
    // раскрытый слот мультицвета на карточке (фидбэк 2026-07-11: цвет выбирается заранее)
    var spS = useState(null); var slotPick = spS[0], setSlotPick = spS[1];
    // Лайтбокс на весь экран (OpenTodo ae038968): и картинки галереи, и рендер слайсера
    // открываются в один и тот же переиспользуемый PS_Lightbox (у него уже есть интеграция
    // с кнопкой/жестом «назад» — back.js).
    var lbS = useState(''); var lbSrc = lbS[0], setLbSrc = lbS[1];
    // 3D-просмотр модели (B1): флаг открытия вьюера. Сбой 3D → onFallbackToPng открывает PNG
    // в том же PS_Lightbox (единый полноэкранный просмотр картинок).
    var v3S = useState(false); var view3d = v3S[0], setView3d = v3S[1];
    // Галерея — только настоящая картинка товара (карточка каталога); у загрузки файла/ссылки
    // настоящего фото нет — галереи нет, и рендер работает главным визуалом (hero) с превью
    // модели как фолбэком, пока честный cpreview не готов (фидбэк владельца 2026-07-11:
    // два почти одинаковых рендера друг под другом выглядели как бред).
    var images = galleryImages(product);
    // до первого результата фон «считает» (шиммер/бейдж), после — только пока refining
    var refining = ready ? !!r.refining : !failed;
    return h('div', { className: 'screen' },
      // визуалы обёрнуты в relative-контейнер, чтобы кнопка «3D» легла в его правый-нижний угол
      h('div', { className: 'v3d-host' },
        // 3D — ОСНОВНОЕ превью (просьба владельца 2026-07-23): рендер ВСЕГДА заменяем встроенным
        // 3D-вьюером, когда модель просматриваема (не только при отсутствии фото). Реальные фото
        // товара (галерея) оставляем СВЕРХУ. PNG-рендер — фолбэк, если 3D не заводится (STEP/без
        // WebGL/сбой): тогда показываем прежний рендер + точку «3D».
        images.length ? h(Gallery, { images: images, dims: r.dims || m.dims || (product && product.dims), onOpen: setLbSrc }) : null,
        (window.PS_Inline3D && window.ps3dCanShow && ps3dCanShow(m))
          ? h(window.PS_Inline3D, { model: m, fmt: window.ps3dFmt ? window.ps3dFmt(m) : '',
              url: API.modelFile(m.token), dims: r.dims || m.dims, png: r.cpreview || m.preview,
              colors: v3dColors, multicolor: qIsMC,
              onExpand: function () { setView3d(true); },
              onFallback: function () {} })
          : h(React.Fragment, null,
              h(RenderSlot, { cpreview: r.cpreview, refining: refining, hero: !images.length,
                fallback: images.length ? null : (m.preview || null), onOpen: setLbSrc }),
              window.PS_View3DButton && h(window.PS_View3DButton, { model: m, onOpen: function () { setView3d(true); } }))),
      view3d && window.PS_Viewer3D && h(window.PS_Viewer3D, {
        token: m.token, url: API.modelFile(m.token), fmt: window.ps3dFmt ? window.ps3dFmt(m) : '',
        dims: r.dims || m.dims || '', colors: v3dColors, multicolor: qIsMC,
        onClose: function () { setView3d(false); },
        onFallbackToPng: function () { setLbSrc(m.preview || r.cpreview || ''); } }),
      h(window.PS_Lightbox, { src: lbSrc, name: lbSrc ? ('print-studio-' + (m.token || 'model') + '.png') : '',
        onClose: function () { setLbSrc(''); } }),

      // ВАРИАНТ НАБОРА (87eb9cf3): карточка несёт одну модель несколькими комплектами файлов
      // (полный 3mf + россыпь STL и т.п.) — считаем и показываем ВЫБРАННЫЙ. Смена комплекта
      // подменяет модель токеном нового комплекта и тихо пересчитывает смету.
      (m.file_variants && m.file_variants.length > 1) && h('div', { style: { marginTop: 14 } },
        h(Lab, null, 'Вариант набора'),
        h('div', { className: 'chips' }, m.file_variants.map(function (v) {
          var on = (m.variant_sel || '') === v.key;
          return h('button', { key: v.key, className: 'chip' + (on ? ' on' : ''),
            disabled: app.busy || app.recalcPending,
            onClick: function () { if (!on && !app.recalcPending) app.setFileVariant(v.key); } },
            v.label, v.files > 1 ? h('small', null, ' · ' + v.files + ' ф.') : null);
        })),
        h('p', { className: 'hint', style: { marginTop: 7 } },
          'В карточке несколько комплектов файлов одной модели — в расчёт идёт выбранный.')),

      // ПИКЕР ЦВЕТА — ПРЯМО ПОД 3D (редизайн владельца 2026-07-23): выбор цвета мгновенно
      // перекрашивает модель выше (проп colors → setColors, без переслайса). Срочность/постобработка —
      // НИЖЕ (уже перенесены под цвет). Оба блока — те же, что были: мультицвет-слоты + палитра.
      h('div', { className: 'color-under3d' },
        // Мультицвет: слоты «Цвет 1..N» ПРЯМО на карточке (фидбэк владельца 2026-07-11 — цвет
        // выбирается заранее и для мультицветных). Смена слота тихо пересчитывает (слайс в кэше).
        (function () {
          if (!(m.multicolor && app.cfg.multicolor)) return null;
          var pl = _qpl, cols = _qcols, colCss = _qcss;
          function dot(css) { return h('span', { className: 'dot-ring', style: { border: 0, width: 15, height: 15, background: css || '#8a97a5', flex: '0 0 auto' } }); }
          return h('div', { style: { marginTop: 14 } },
            h(Lab, null, 'Цвета печати'),
            h('div', { className: 'card', style: { padding: '4px 13px 8px' } },
              (app.cfg.colors_sel || []).map(function (cName, i) {
                var ci = cols.indexOf(cName);
                return h(React.Fragment, { key: i },
                  h('div', { style: { display: 'flex', alignItems: 'center', gap: 11, padding: '8px 0', borderTop: i ? '1px solid var(--line)' : 'none' } },
                    h('span', { style: { fontSize: 13, color: 'var(--muted)', flex: '0 0 auto', width: 56 } }, 'Цвет ' + (i + 1)),
                    h('button', { type: 'button', className: 'dotbtn' + (slotPick === i ? ' on' : ''), disabled: app.busy,
                      title: cName || 'выбрать цвет', 'aria-label': 'Цвет ' + (i + 1) + (cName ? ': ' + cName : ''),
                      style: { background: ci >= 0 ? colCss[ci] : '#8a97a5' },
                      onClick: function () { setSlotPick(slotPick === i ? null : i); } }),
                    h('span', { style: { fontSize: 13.5, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, cName || '—'),
                    slotPick !== i && h('span', { style: { marginLeft: 'auto', color: 'var(--faint)', fontSize: 11.5, flex: '0 0 auto' } }, 'тап по кружку — сменить')),
                  slotPick === i && h('div', { className: 'chips', style: { padding: '2px 0 8px' } }, cols.map(function (c, k) {
                    var isOut = (pl.out_of_stock || []).indexOf(c) >= 0;
                    return h('button', { key: c, disabled: isOut, className: 'chip' + (cName === c ? ' on' : '') + (isOut ? ' oos' : ''),
                      onClick: isOut ? undefined : function () { setSlotPick(null); app.setSlotColor(i, c); } },
                      dot(colCss[k]), ' ', c, isOut ? h('small', null, ' · нет') : null);
                  })));
              })));
        })(),

        // Один цвет на пластине → даём сменить и ПЛАСТИК, не только цвет (OpenTodo one-color-plastic).
        // Слоты выше меняют ЦВЕТ; для мультицвета сетка пластиков ниже спрятана, поэтому материал
        // менять негде. Когда distinct(colors_sel)===1 (все слоты одного цвета или один слот) —
        // пластик однозначен: показываем чипы пластиков, смена ремапит слоты и делает полный пересчёт.
        (function () {
          if (!(m.multicolor && app.cfg.multicolor)) return null;   // только мультицвет со слотами
          var sel = app.cfg.colors_sel || [];
          var uniq = {}; sel.forEach(function (x) { if (x) uniq[x] = 1; });
          if (Object.keys(uniq).length !== 1) return null;          // именно ОДИН цвет на пластине
          var ps = (app.options && app.options.plastics) || [];
          if (ps.length < 2) return null;                           // менять не на что
          return h('div', { style: { marginTop: 14 } },
            h(Lab, null, 'Пластик'),
            h('div', { className: 'chips' }, ps.map(function (p) {
              var on = app.cfg.plastic_id === p.id;
              return h('button', { key: p.id, type: 'button', className: 'chip' + (on ? ' on' : ''),
                disabled: app.busy || app.recalcPending,
                onClick: function () { if (!on) app.setPlastic(p.id); } },
                icon('spool', { size: 14 }), ' ', p.name,
                p.price != null && h('small', { style: { opacity: 0.75 } }, ' · ', money(p.price, cur), '/кг'));
            })));
        })(),

        // смена цвета прямо здесь — шарики без названий пластиков, секции по КАТЕГОРИИ пластика
        // (3af25fc2), мгновенный пересчёт без переслайса (050aff56 + fb552c3a)
        (function () {
          var ps = (app.options && app.options.plastics) || [];
          if (!ps.length) return null;
          // мультицвет со слотами (a197bb93): быстрая смена ОДНОГО цвета здесь сбила бы подбор по слотам
          if (app.model && app.model.multicolor && app.cfg.multicolor) return null;
          var CATS = ['Матовый декор', 'Обычный', 'Инженерный', 'Шелковый'];
          var groups = {};
          CATS.forEach(function (c) { groups[c] = []; });
          ps.forEach(function (p) {
            var cat = CATS.indexOf(p.category) >= 0 ? p.category : 'Обычный';
            var cols = (p.colors && p.colors.length) ? p.colors : [''];
            cols.forEach(function (c, ci) {
              groups[cat].push({ p: p, color: c, css: (p.color_css || [])[ci] || '#8a97a5' });
            });
          });
          var selP = app.cfg.plastic_id, selC = app.cfg.color;
          var curPl = ps.filter(function (p) { return p.id === selP; })[0] || {};
          function isOn(d) {
            if (d.p.id !== selP) return false;
            return selC ? d.color === selC : d.color === ((d.p.colors || [''])[0] || '');
          }
          function dots(list) {
            return h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 10 } }, list.map(function (d, i) {
              return h('button', { key: d.p.id + ':' + d.color + ':' + i, type: 'button',
                className: 'dotbtn' + (isOn(d) ? ' on' : ''), disabled: app.busy,
                title: d.p.name + (d.color ? ' · ' + d.color : ''),
                'aria-label': d.p.name + (d.color ? ' · ' + d.color : ''),
                style: { background: d.css },
                // до готовности сметы requote невозможен — выбор запоминается (app.pickColor)
                onClick: function () { if (!isOn(d)) (ready ? app.requote(d.p.id, d.color) : app.pickColor(d.p.id, d.color)); } });
            }));
          }
          function section(label, list, first) {
            if (!list.length) return null;
            return [h('div', { key: label + 'l', className: 'lab', style: { margin: (first ? '0' : '12px') + ' 2px 8px' } }, label),
                    h('div', { key: label + 'd' }, dots(list))];
          }
          var firstCat = CATS.filter(function (c) { return groups[c].length; })[0];
          return h('div', { style: { marginTop: 14 } },
            CATS.map(function (c) {
              return h(React.Fragment, { key: c }, section('Цвет · ' + c.toLowerCase(), groups[c], c === firstCat));
            }),
            h('p', { className: 'hint', style: { marginTop: 9 } },
              (curPl.name || '') + (selC ? ' · ' + selC : '') + ' · ' + (curPl.price || '—') + ' ' + cur + '/кг'));
        })()),

      h('div', { style: { margin: '14px 2px 0' } },
        h('div', { style: { color: 'var(--muted)', fontSize: 13.5, display: 'flex', alignItems: 'center', gap: 8 } },
          h('span', { style: { minWidth: 0, flex: 1 } },
            (m.label || (product && product.label) || 'Модель') + ' · ' + (app.cfg.qty) + ' шт'),
          // сердечко «Избранное» — только у карточек каталога (у загрузки файла/ссылки
          // модели каталога нет, отмечать нечего)
          product && product.id ? h(FavHeart, { app: app, id: product.id, inline: true }) : null),
        // тап по цифре раскрывает разбивку («подробности по нажатию на цифру», 050aff56);
        // «~» — быстрый пересчёт без переслайса, цена приблизительная (a94d2d3c).
        // скидка заметной: цена со скидкой крупно + зачёркнутая «до» + бейдж −X% (07451408).
        // До готовности сметы — старая цена «~N» из плитки либо «Считаем цену…» (ae038968).
        !failed && h('div', { style: { marginTop: 6, display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' } },
          ready
            // шеврон у цены убран (фидбэк владельца 2026-07-10: «лишняя стрелка»); цена
            // по-прежнему кликабельна, основной вход в разбивку — «Из чего складывается»
            ? h('div', { className: 'price', style: { cursor: 'pointer', color: r.discount_pct > 0 ? 'var(--price)' : undefined },
                onClick: function () { setShowBd(!showBd); } }, (r.approx ? '~' : '') + (r.total || ''))
            : (product && product.price > 0
                ? h('div', { className: 'price' }, '~', money(product.price, cur))
                : h('div', { className: 'price', style: { color: 'var(--muted)', fontSize: 24 } }, 'Считаем цену…')),
          ready && r.total_before && h('span', { style: { color: 'var(--faint)', textDecoration: 'line-through', fontSize: 17, fontWeight: 600 } }, r.total_before),
          ready && r.discount_pct > 0 && h('span', { style: { background: 'var(--accent)', color: '#1a0d02', fontWeight: 800, fontSize: 13, padding: '3px 10px', borderRadius: 999, alignSelf: 'center' } }, '−' + r.discount_pct + '%'),
          // Пульс у цены: до сметы — «считаем/обновляем», после — «уточняем по слайсу», пока
          // фон не снимет refining (тост «Цена уточнена» — app.js, поллинг quote_state).
          refining && !app.recalcPending && h('span', { className: 'refining-badge' },
            h('span', { className: 'rb-dot' }),
            ready ? 'уточняем по слайсу…' : (product && product.price > 0 ? 'обновляем цену…' : 'считаем…')),
          // правку столов/пластика стола сделали прямо здесь — цена рядом ещё СТАРАЯ (71e15fe6)
          app.recalcPending && h('span', { className: 'refining-badge' },
            h('span', { className: 'rb-dot' }), 'пересчитываем под новые столы…')),
        ready && r.discount_pct > 0 && h('div', { style: { color: 'var(--price)', fontSize: 12.5, fontWeight: 600, marginTop: 4 } }, '🎉 Ваша скидка ' + r.discount_pct + '% уже в цене')),
      // тихий фейл слайса (55a1cf8f): инлайн-блок вместо цены, заказ без цены остаётся доступен
      failed && h('div', { className: 'card', style: { marginTop: 14 } },
        h('div', { style: { fontWeight: 700, fontSize: 15 } }, 'Не получилось посчитать автоматически'),
        res.error && h('div', { style: { marginTop: 6, fontSize: 13, color: 'var(--muted)', lineHeight: 1.4 } }, res.error),
        res.can_order_unsliced && h('button', { className: 'btn', style: { marginTop: 14 },
          onClick: function () { app.orderUnslicedFor(res.token); } },
          icon('receipt', { size: 18 }), ' Отправить мастеру на оценку'),
        h('button', { className: 'btn line', style: { marginTop: 9 }, onClick: function () { app.back(); } }, 'Понятно')),
      ready && h('div', { className: 'specrow' },
        // габарит подсвечиваем красным, когда именно он нарушает стол (verdict=wont_fit) —
        // визуальная связь «размер → проблема» (B2, дизайн-спека)
        r.dims && h('span', { className: 's', style: r.verdict === 'wont_fit' ? { color: 'var(--red)' } : undefined },
          icon('ruler', { size: 15 }), h('b', null, r.dims), ' мм'),
        // масштаб ≠ 100% — показываем явно: габариты выше уже пересчитаны под него
        r.scale_pct && r.scale_pct !== 100 && h('span', { className: 's' }, '⤢ ', h('b', null, r.scale_pct + '%')),
        h('span', { className: 's' }, icon('scale', { size: 15 }), h('b', null, r.grams_total), ' г'),
        h('span', { className: 's' }, icon('clock', { size: 15 }), h('b', null, r.time_total)),
        // Набор деталей разложен по столам (defe5dc0): у модели нет родной сетки столов
        // (m.plates<=1 — грид не рисуется), но расчёт разложил детали на несколько печатей.
        !(m.plates > 1) && r.plates > 1 && h('span', { className: 's' },
          icon('bed', { size: 15 }), h('b', null, r.plates), r.plates < 5 ? ' стола' : ' столов'),
        // Пункт вердикта печатопригодности (B2): при 'ok' — тихая зелёная галочка-доверие; risky —
        // янтарь; wont_fit — красный. Только когда есть габариты (иначе замера нет — ничего не показываем).
        r.dims && r.verdict === 'ok' && h('span', { className: 's', style: { color: 'var(--price)' } },
          icon('shield', { size: 15 }), 'Печатается без проблем'),
        r.dims && r.verdict === 'risky' && h('span', { className: 's', style: { color: 'var(--amber)' } },
          icon('info', { size: 15 }), 'Проверьте печатопригодность'),
        r.dims && r.verdict === 'wont_fit' && h('span', { className: 's', style: { color: 'var(--red)' } },
          icon('bed', { size: 15 }), 'Не влезает на стол')),
      // Плашка-предупреждение DFM (B2) — заметная карточка для risky/wont_fit; ok/нет вердикта → null.
      ready && !failed && h(DfmBanner, { r: r, app: app }),
      // Лояльность на чекауте (idea #11): прогресс к следующему уровню/перку — заказ приближает к нему.
      (function () {
        var pr = app.headerProfile;
        if (!pr || !(pr.to_next_xp > 0)) return null;
        return h('div', { className: 'loyalty' }, icon('star', { size: 14, style: { color: 'var(--amber)' } }),
          h('span', null, 'Этот заказ приближает к «', (pr.next_title || ('уровню ' + ((pr.level || 0) + 1))), '» — осталось ', pr.to_next_xp, ' XP',
            pr.next_perk ? h('em', null, ' · откроется: ' + pr.next_perk) : null));
      })(),
      ((r.term || r.eta) || (r.queue && r.queue.count)) && h('div', { className: 'metaline' },
        // Срок (B3): обещанный клиенту term (сегодня + дни из настроек) в приоритете, иначе очередь-
        // осознанный eta. Для срочного заказа тег акцентный (.tag.warm) с иконкой bolt.
        (r.term || r.eta) && (r.urgency === 'rush'
          ? h('span', { className: 'tag warm' }, icon('bolt'), ' Срочно, готово к ', (r.term || r.eta))
          : h('span', { className: 'tag' }, icon('cal'), ' Готово к ', (r.term || r.eta))),
        r.queue && r.queue.count > 0 && h('span', { className: 'tag warm' }, icon('clock'), ' В очереди ', r.queue.count, ' зак.'),
        // живой статус принтера (idea #8): прямо сейчас печатается N% текущей работы
        r.queue && r.queue.printing && h('span', { className: 'tag', style: { color: 'var(--price)', borderColor: 'var(--price-soft)' } },
          icon('printer'), ' Печатается ', r.queue.printing.progress, '%')),
      (r.warnings && r.warnings.length > 0) && h('div', { className: 'metaline' },
        r.warnings.map(function (w, i) { return h('span', { key: i, className: 'tag', style: { color: 'var(--amber)', borderColor: 'var(--amber-soft)' } }, icon('info'), ' ', w); })),
      // Пока идёт фоновое уточнение слайсом — об этом уже сказано у самой цены (refining-badge
      // выше), здесь не дублируем; после того как refining снят, а approx почему-то остался
      // (например requote без printer/quality — бэк не запустил фон), возвращаем старый текст.
      r.approx && !refining && h('div', { className: 'metaline' },
        h('span', { className: 'tag', style: { color: 'var(--amber)', borderColor: 'var(--amber-soft)' } },
          icon('info'), ' Быстрый пересчёт — мастер уточнит цену при заказе')),

      // Выбор столов ПРЯМО на смете (OpenTodo 71e15fe6: «вернуть возможность выбирать столы
      // не из „Редактирования“, а с основной страницы заказа») — тот же блок, что в
      // «Параметрах», но set тихо пересчитывает цену на месте (app.tweak), не уводя со сметы.
      // Порядок как в «Параметрах»: сначала столы, затем цвета. Одностольная модель → null.
      !failed && h('div', { style: { marginTop: 14 } }, h(PlatePicker, { app: app, set: app.tweak })),

      // (Пикер цвета — мультицвет-слоты + палитра — ПЕРЕНЕСЁН ВЫШЕ, прямо под 3D-модель; редизайн
      // владельца 2026-07-23.) Срочность/постобработка остаются НИЖЕ выбора цвета.

      // Срочность (B3) — ПОД выбором цвета (фидбэк владельца 2026-07-23). Компактный тумблер; тап
      // вызывает app.tweak. Точную дату «Готово к DD.MM» показывает тег r.term в метастроке выше.
      !failed && ready && (function () {
        var oo = app.options || {};
        function chip(isUrgent, label) {
          return h('button', { className: 'chip' + ((!!app.cfg.urgent === isUrgent) ? ' on' : ''),
            disabled: app.busy || app.recalcPending,
            onClick: function () { if (!!app.cfg.urgent === isUrgent) return; app.tweak({ urgent: isUrgent }); } },
            label);
        }
        return h('div', { style: { marginTop: 14 } },
          h(Lab, null, 'Срочность'),
          h('div', { className: 'chips' },
            chip(false, '🐢 Обычно'),
            h('button', { className: 'chip' + (app.cfg.urgent ? ' on' : ''), disabled: app.busy || app.recalcPending,
              onClick: function () { if (app.cfg.urgent) return; app.tweak({ urgent: true }); } },
              icon('bolt', { size: 15 }), ' Срочно', oo.urgent_k ? (' ×' + oo.urgent_k) : '')));
      })(),

      // Постобработка (B4) — ПОД выбором цвета. Тап по услуге меняет выбор + цену МГНОВЕННО и
      // ЛОКАЛЬНО (B4-инстант): услуги плоские, геометрию не трогают → без переслайса/спиннера/
      // блокировки кнопки заказа (в отличие от срочности/столов, что идут через app.tweak). Выбор
      // уедет в /api/order и там переложится авторитетно. disabled не ставим — тап всегда мгновенный.
      !failed && ready && h(PostprocSection, { app: app,
        set: function (patch) { app.applyOptionsLocal(patch.options); } }),

      // скрыто, см. задачу f3332d8c — промокоды временно не нужны; раскомментировать когда понадобятся
      // r.promo && h('div', { className: 'metaline' }, h('span', { className: 'tag', style: { color: 'var(--price)', borderColor: 'var(--price-soft)' } }, icon('tag'), ' Промокод ', r.promo.code, ' · −', r.promo.pct, '%')),
      // r.promo_invalid && h('div', { className: 'metaline' }, h('span', { className: 'tag', style: { color: 'var(--red)', borderColor: 'var(--red-soft)' } }, icon('info'), ' Промокод не сработал')),

      // «Сделать дешевле» (idea #2): рычаг снижения цены в один тап — дешёвый пластик (быстрый
      // requote). Рычаг «эконом-заполнение» убран вместе с выбором прочности (8c8199d1).
      // Только при готовой смете: без last на сервере requote не сработает.
      ready && (function () {
        var o = app.options || {}, ps = o.plastics || [];
        var cur = ps.filter(function (p) { return p.id === app.cfg.plastic_id; })[0] || {};
        // не предлагаем «сэкономить» пластиком, которого нет на складе (все цвета out_of_stock)
        var avail = ps.filter(function (p) {
          return !(p.colors && p.colors.length && (p.out_of_stock || []).length >= p.colors.length);
        });
        var cheap = avail.slice().sort(function (a, b) { return (a.price || 0) - (b.price || 0); })[0];
        var levers = [];
        if (cheap && cheap.id !== cur.id && (cheap.price || 0) < (cur.price || 1e9)) {
          // первый ДОСТУПНЫЙ цвет дешёвого пластика (не первый попавшийся из палитры)
          var okCols = (cheap.colors || []).filter(function (c) { return (cheap.out_of_stock || []).indexOf(c) < 0; });
          levers.push(h('button', { key: 'pl', className: 'lever', type: 'button', disabled: app.busy,
            onClick: function () { app.requote(cheap.id, okCols[0] || (cheap.colors || [])[0] || ''); } },
            icon('spool', { size: 15 }), h('span', null, 'На ', h('b', null, cheap.name)),
            h('em', null, cheap.price + ' ' + (o.currency || 'руб.') + '/кг')));
        }
        if (!levers.length) return null;
        return h('div', { style: { marginTop: 14 } },
          h(Lab, null, '💡 Сделать дешевле'),
          h('div', { className: 'levers' }, levers));
      })(),
      lines.length > 0 && h('button', { onClick: function () { setShowBd(!showBd); },
        style: { marginTop: 12, display: 'inline-flex', alignItems: 'center', gap: 6, background: 'transparent',
          border: 0, padding: '0 2px', cursor: 'pointer', color: 'var(--muted)', fontSize: 12.5, fontWeight: 600 } },
        icon('receipt', { size: 14 }), showBd ? 'Скрыть расчётник' : 'Из чего складывается цена',
        icon('chev', { size: 13, style: { transform: showBd ? 'rotate(-90deg)' : 'rotate(90deg)', transition: 'transform .15s' } })),
      showBd && h('div', { className: 'card', style: { marginTop: 9 } },
        h('div', { className: 'bd' }, lines.map(function (ln, i) {
          var isDisc = /−/.test(ln[1]);
          return h('div', { key: i, className: 'r' + (isDisc ? ' disc' : '') },
            h('span', { className: 'k' }, ln[0]), h('span', { className: 'v' }, ln[1]));
        }), h('div', { className: 'r total' }, h('span', { className: 'k' }, 'Итого'), h('span', { className: 'v' }, r.total)))),

      // Доставка по умолчанию (A2): заказ в один тап уедет по адресу из профиля, поэтому
      // клиент должен увидеть доставку и итог с ней ЗДЕСЬ, до кнопки «Оставить заказ», —
      // иначе в «Моих заказах» сумма окажется больше той, что он видел на смете.
      // «В корзину» этой доставки не касается: там способ получения выбирается отдельно.
      !failed && h(QuoteDelivery, { app: app, total: r.total }),

      // Иерархия CTA (аудит UX): два главных действия крупно, «Параметры» — один вторичный
      // путь «поправить», сервисные (PDF/Поделиться/Заново) — компактный ряд третьего веса.
      // Кнопки РАБОТАЮТ сразу (фидбэк владельца 2026-07-11: «нельзя сразу оставить заказ»):
      // до готовой цены нажатие запоминает намерение (app.deferAction) — заказ/корзина
      // оформятся автоматически, как только расчёт завершится (эффект на [result] в app.js);
      // слайс упал → заказ автоматически уходит мастеру без цены. Повторный тап — отмена.
      // app.recalcPending (71e15fe6): параметры на экране новее посчитанной цены — правка столов
      // со сметы пересчитывается ТИХО и не трогает app.busy, поэтому без этого флага кнопка
      // оформляла заказ по серверному снапшоту прошлого расчёта (старые столы, старая цена).
      !failed && h('button', { className: 'btn lg', style: { marginTop: 14 }, disabled: app.busy || app.recalcPending,
        onClick: function () {
          if (app.recalcPending) return;
          if (ready) { app.placeOrder(); return; }
          if (app.pendingAction === 'order') { app.deferAction(null); app.toast('Отменил — заказ не оформится'); return; }
          app.deferAction('order'); app.toast('Оформлю заказ сразу, как досчитаю цену');
        } },
        icon('receipt', { size: 18 }),
        app.recalcPending ? ' Пересчитываем цену…'
          : (ready ? ' Оставить заказ'
            : (app.pendingAction === 'order' ? ' Оформится после расчёта — отменить' : ' Оставить заказ'))),
      !failed && h('button', { className: 'btn soft', disabled: app.busy || app.recalcPending,
        onClick: function () {
          if (app.recalcPending) return;
          if (ready) { app.addCart(); return; }
          if (app.pendingAction === 'cart') { app.deferAction(null); app.toast('Отменил'); return; }
          app.deferAction('cart'); app.toast('Добавлю в корзину сразу, как досчитаю цену');
        } },
        icon('cart', { size: 18 }),
        app.recalcPending ? ' Пересчитываем цену…'
          : (ready ? ' Добавить в корзину'
            : (app.pendingAction === 'cart' ? ' Добавится после расчёта — отменить' : ' Добавить в корзину'))),
      !failed && h('button', { className: 'btn line', disabled: !m.token, onClick: function () { app.go('config'); } }, icon('pencil', { size: 17 }), ' Изменить параметры'),
      h('div', { style: { display: 'flex', gap: 8, marginTop: 12 } },
        // recalcPending — смета на сервере ещё от прошлого набора столов, PDF/ссылка ушли бы со старой ценой
        [['doc', 'PDF', function () { if (!ready || app.recalcPending) { app.toast('Смета ещё считается'); return; } app.pdf(); }],
         ['share', 'Поделиться', function () { if (!ready || app.recalcPending) { app.toast('Смета ещё считается'); return; } app.share(); }],
         ['refresh', 'Заново', function () { app.reset(); }]].map(function (a) {
          return h('button', { key: a[1], type: 'button', onClick: a[2],
            style: { flex: 1, minHeight: 42, display: 'flex', alignItems: 'center', justifyContent: 'center',
              gap: 6, border: 0, borderRadius: 12, background: 'transparent', cursor: 'pointer',
              color: 'var(--faint)', fontSize: 12, fontWeight: 600, fontFamily: 'var(--ui)' } },
            icon(a[0], { size: 15 }), a[1]);
        })));
  }

  // Расчётник заказа: карточка-разбивка цены из объекта bd ({rows, total, …}), что приходит
  // в JSON заказа. Те же классы .bd/.r/.k/.v, что и на экране результата — единый вид.
  function Breakdown(props) {
    var bd = props.bd;
    if (!bd || !bd.rows || !bd.rows.length) return null;
    return h('div', { className: 'card', style: { marginTop: props.gap != null ? props.gap : 12 } },
      props.title !== false && h('div', { style: { fontSize: 12.5, color: 'var(--muted)', fontWeight: 600, marginBottom: 4 } }, props.title || 'Расчётник'),
      h('div', { className: 'bd' },
        bd.rows.map(function (r, i) {
          return h('div', { key: i, className: 'r' + (r.discount ? ' disc' : '') },
            h('span', { className: 'k' }, r.label, r.sub && h('em', null, '  ', r.sub)),
            h('span', { className: 'v' }, r.value));
        }),
        h('div', { className: 'r total' }, h('span', { className: 'k' }, 'Итого'), h('span', { className: 'v' }, bd.total))));
  }

  // ───────────────────── ERROR MEME (Task C) ─────────────────────
  // Само­достаточные inline-SVG «мемы» для экрана ошибки: без внешних URL,
  // без сетевых/копирайт-зависимостей. Тёмная тема, сдержанно. Выбирается случайно.
  // Каждый: { cap: подпись (RU), svg: function -> React-элемент }.
  var ME_ACC = '#ff7a1a', ME_LINE = '#3a4656', ME_FILL = '#222c39', ME_MUT = '#7c8b9c';
  function meSvg(children) {
    return h('svg', { viewBox: '0 0 120 120', width: 116, height: 116, fill: 'none',
      stroke: ME_LINE, strokeWidth: 3, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': 'true' }, children);
  }
  function eye(cx, cy) { return h('circle', { cx: cx, cy: cy, r: 2.6, fill: ME_MUT, stroke: 'none' }); }
  var MEMES = [
    { // 1 — спагетти: клубок филамента вместо детали
      cap: 'Принтер приготовил спагетти. Заказывали не это.',
      svg: function () {
        return meSvg([
          h('line', { key: 'b', x1: 18, y1: 96, x2: 102, y2: 96 }),
          h('path', { key: 's1', stroke: ME_ACC, d: 'M30 92c8-22 50-22 58 0M34 90c2-26 48-30 50-2M42 91c0-22 36-26 34 2M50 92c-6-20 26-30 24-4' }),
          h('path', { key: 's2', stroke: ME_ACC, d: 'M38 70c-10-8-6-26 8-26M82 70c10-8 6-26-8-26' }),
          h('g', { key: 'f' }, eye(50, 60), eye(70, 60),
            h('path', { d: 'M52 76c4-5 12-5 16 0', stroke: ME_MUT, strokeWidth: 2.6 }))
        ]);
      }
    },
    { // 2 — оплавленный Benchy: «потёкший» кораблик
      cap: 'Benchy поплыл. В прямом смысле.',
      svg: function () {
        return meSvg([
          h('line', { key: 'b', x1: 16, y1: 98, x2: 104, y2: 98 }),
          h('path', { key: 'h', stroke: ME_ACC, fill: ME_FILL, d: 'M28 86c2-14 62-14 64 0 2 6-66 6-64 0Z' }),
          h('path', { key: 'c', stroke: ME_ACC, fill: ME_FILL, d: 'M44 72c0-12 28-12 28 0 0 6-28 6-28 0Z' }),
          h('rect', { key: 'w', x: 50, y: 58, width: 12, height: 12, rx: 2, stroke: ME_ACC, fill: ME_FILL }),
          h('path', { key: 'd', stroke: ME_MUT, strokeWidth: 2.4, d: 'M34 92c0 7-1 12-3 12M86 92c0 6 1 10 3 10M60 96c0 8 0 13-2 14' }),
          h('g', { key: 'f' }, eye(52, 64), eye(60, 64))
        ]);
      }
    },
    { // 3 — пожимающий плечами робот
      cap: 'Робот пожимает манипуляторами. Бывает.',
      svg: function () {
        return meSvg([
          h('rect', { key: 'hd', x: 40, y: 26, width: 40, height: 32, rx: 8, fill: ME_FILL }),
          h('line', { key: 'an', x1: 60, y1: 26, x2: 60, y2: 16 }),
          h('circle', { key: 'ad', cx: 60, cy: 13, r: 3, fill: ME_ACC, stroke: 'none' }),
          h('g', { key: 'f' }, eye(51, 42), eye(69, 42),
            h('line', { x1: 53, y1: 51, x2: 67, y2: 51, stroke: ME_MUT, strokeWidth: 2.6 })),
          h('rect', { key: 'bd', x: 46, y: 64, width: 28, height: 26, rx: 6, fill: ME_FILL }),
          h('path', { key: 'ar', stroke: ME_ACC, d: 'M46 70c-12 2-16-6-14-14M74 70c12 2 16-6 14-14' }),
          h('path', { key: 'hn', stroke: ME_ACC, d: 'M30 58c-3-2-5 0-4 3M90 58c3-2 5 0 4 3' })
        ]);
      }
    },
    { // 4 — отклеившаяся / заворачивающаяся деталь (warping)
      cap: 'Деталь отклеилась от стола. Гравитация победила.',
      svg: function () {
        return meSvg([
          h('line', { key: 'b', x1: 14, y1: 90, x2: 106, y2: 90 }),
          h('path', { key: 'p', stroke: ME_ACC, fill: ME_FILL, d: 'M34 88c-6-2-8-10-2-14 18-10 38-10 56 0 6 4 4 12-2 14Z' }),
          h('path', { key: 'c1', stroke: ME_ACC, d: 'M34 88c-10-2-16-8-20-16' }),
          h('path', { key: 'c2', stroke: ME_ACC, d: 'M86 88c10-2 16-8 20-16' }),
          h('g', { key: 'f' }, eye(52, 76), eye(68, 76),
            h('path', { d: 'M54 84c3-4 9-4 12 0', stroke: ME_MUT, strokeWidth: 2.4 })),
          h('path', { key: 'sw', stroke: ME_MUT, strokeWidth: 2.2, d: 'M26 60c-3-3-3-7 0-10M94 60c3-3 3-7 0-10' })
        ]);
      }
    },
    { // 5 — пустая катушка: филамент кончился
      cap: 'Филамент кончился. Как обычно — на 98%.',
      svg: function () {
        return meSvg([
          h('circle', { key: 'o', cx: 60, cy: 58, r: 34, fill: ME_FILL }),
          h('circle', { key: 'i', cx: 60, cy: 58, r: 11 }),
          h('circle', { key: 'h1', cx: 60, cy: 30, r: 3.4, fill: ME_LINE, stroke: 'none' }),
          h('circle', { key: 'h2', cx: 60, cy: 86, r: 3.4, fill: ME_LINE, stroke: 'none' }),
          h('circle', { key: 'h3', cx: 88, cy: 58, r: 3.4, fill: ME_LINE, stroke: 'none' }),
          h('circle', { key: 'h4', cx: 32, cy: 58, r: 3.4, fill: ME_LINE, stroke: 'none' }),
          h('path', { key: 't', stroke: ME_ACC, d: 'M71 58c10 2 18 8 14 18-3 8-12 8-14 0' }),
          h('g', { key: 'f' }, eye(54, 54), eye(66, 54),
            h('path', { d: 'M55 66c3 3 9 3 12-2', stroke: ME_MUT, strokeWidth: 2.4 }))
        ]);
      }
    }
  ];

  // Оверлей ошибки: случайный мем + текст ошибки + кнопка закрытия. props.msg, props.onClose.
  // props.search (если задан) → кнопка «Найти на Printables» (props.onSearch(query)) — при блоке
  // MakerWorld предлагаем ту же модель на открытой площадке.
  function ErrorOverlay(props) {
    // Случайный мем фиксируется на монтирование (новый показ → новый мем).
    var ms = useState(function () { return MEMES[Math.floor(Math.random() * MEMES.length)]; }); var meme = ms[0];
    // Пожелание к заказу без цены (Э2, props.defer): цвет, количество — единственное, что клиент
    // может сказать о заказе, пока мастерская офлайн (выбор пластика без ПК недоступен).
    var cs = useState(''); var deferNote = cs[0], setDeferNote = cs[1];
    // Порядок кнопок сверху вниз: «оформить без цены» (55a1cf8f) → «на разбор» (26196ac1) →
    // «найти на Printables» → «понятно». Первая доступная — primary (сплошная, отступ 16),
    // остальные — line (отступ 9). Класс и отступ считаются ОДНИМ вызовом: раздельные хелперы
    // с общим флагом давали первой кнопке line-класс (флаг перещёлкивался между вызовами).
    var firstDone = false;
    function btnProps() {
      var first = !firstDone; firstDone = true;
      return { cls: first ? 'btn' : 'btn line', st: { marginTop: first ? 16 : 9 } };
    }
    return h('div', { className: 'errov', role: 'alertdialog', 'aria-modal': 'true',
      onClick: function () { props.onClose && props.onClose(); } },
      h('div', { className: 'errcard', onClick: function (e) { e.stopPropagation(); } },
        // Мастерская офлайн — не сбой: мем про «спагетти» тут звучал бы как поломка.
        props.defer
          ? h('div', { className: 'errart', 'aria-hidden': 'true', style: { fontSize: 56, lineHeight: 1.1 } }, '🌙')
          : h('div', { className: 'errart' }, meme.svg()),
        h('div', { className: 'errcap' }, props.defer ? 'Мастерская сейчас офлайн' : meme.cap),
        props.msg && h('div', { className: 'errmsg' }, props.msg),
        // Мастерская офлайн (Э2): посчитать нельзя, но заказ принять можно — модель уходит
        // приёмной на VPS, цену пришлют в чат, как только компьютер мастерской включится.
        props.defer && h('label', { className: 'wfield', style: { marginTop: 14 } },
          h('textarea', { rows: 2, maxLength: 500, value: deferNote,
            placeholder: 'Пожелания: цвет, количество… (необязательно)',
            style: { boxSizing: 'border-box', minHeight: 56 },
            onChange: function (e) { setDeferNote(e.target.value); } })),
        props.defer && (function () { var b = btnProps();
          return h('button', { className: b.cls, style: b.st, disabled: !!props.busy,
            onClick: function () { props.onDefer && props.onDefer(deferNote); } },
            icon('receipt', { size: 17 }), props.busy ? ' Отправляю…' : ' Оставить заказ — цену пришлём'); })(),
        props.defer && h('p', { className: 'hint', style: { marginTop: 7, marginBottom: 0 } },
          'Модель сохраним, а цену посчитаем и пришлём в чат с ботом.'),
        // Слайс не удался, но заказ всё равно можно оформить — мастер посмотрит модель и
        // назовёт цену вручную (OpenTodo 55a1cf8f): не тупик, а обходной путь.
        props.unslicedToken && (function () { var b = btnProps();
          return h('button', { className: b.cls, style: b.st,
            onClick: function () { props.onOrderUnsliced && props.onOrderUnsliced(); } },
            icon('receipt', { size: 17 }), ' Отправить мастеру на оценку'); })(),
        props.unslicedToken && h('p', { className: 'hint', style: { marginTop: 7, marginBottom: 0 } },
          'Оформим заявку без цены — мастер посмотрит модель и сам её назначит.'),
        // Очередь разбора (OpenTodo 26196ac1): ссылка не загрузилась → отправить мастеру.
        props.queueUrl && (function () { var b = btnProps();
          return h('button', { className: b.cls, style: b.st,
            onClick: function () { props.onQueue && props.onQueue(props.queueUrl, props.msg || ''); } },
            icon('chat', { size: 17 }), ' Отправить мастеру на разбор'); })(),
        props.queueUrl && h('p', { className: 'hint', style: { marginTop: 7, marginBottom: 0 } },
          'Мастер подготовит модель руками и напишет вам.'),
        props.search && (function () { var b = btnProps();
          return h('button', { className: b.cls, style: b.st,
            onClick: function () { props.onSearch && props.onSearch(props.search); } },
            icon('search', { size: 17 }), ' Найти на Printables'); })(),
        (function () { var b = btnProps();
          return h('button', { className: b.cls, style: b.st,
            onClick: function () { props.onClose && props.onClose(); } },
            icon('refresh', { size: 17 }), props.defer ? ' Не сейчас' : ' Понятно'); })()));
  }

  // ───────────────────── PARAMETRIC (настраиваемые модели, OpenTodo c1cc840a) ─────────────────────
  // Список .scad-моделей с Customizer-параметрами; пользователь задаёт текст/размеры → сервер
  // рендерит STL (OpenSCAD) и возвращает её как обычную модель → дальше тот же расчёт/заказ.
  function Parametric(props) {
    var app = props.app;
    var ds = useState(null); var data = ds[0], setData = ds[1];   // {available, models} | null
    var ss = useState(null); var sel = ss[0], setSel = ss[1];     // выбранная модель
    var vs = useState({}); var vals = vs[0], setVals = vs[1];     // значения параметров
    React.useEffect(function () {
      API.parametricModels().then(function (j) {
        if (j && j.ok) { setData(j); if ((j.models || []).length === 1) pick(j.models[0]); }
        else { setData({ available: false, models: [] }); app.toast((j && j.error) || 'Не удалось загрузить'); }
      }).catch(function () { setData({ available: false, models: [] }); app.toast('Сеть недоступна'); });
    }, []);
    function pick(m) {
      var v = {}; (m.params || []).forEach(function (p) { v[p.name] = p.default; });
      setVals(v); setSel(m);
    }
    function setVal(name, value) { setVals(function (x) { var n = Object.assign({}, x); n[name] = value; return n; }); }
    function flbl(t) { return h('span', { style: { fontSize: 12, color: 'var(--muted)', display: 'block', marginBottom: 4 } }, t); }
    function field(p) {
      var lab = p.label || p.name;
      if (p.type === 'enum') {
        return h('label', { key: p.name, style: { display: 'block', marginTop: 10 } }, flbl(lab),
          h('select', { className: 'input', value: vals[p.name], onChange: function (e) { setVal(p.name, e.target.value); } },
            (p.options || []).map(function (o) { return h('option', { key: o, value: o }, o); })));
      }
      if (p.type === 'number') {
        var rng = (p.min != null && p.max != null) ? ' (' + p.min + '–' + p.max + ')' : '';
        return h('label', { key: p.name, style: { display: 'block', marginTop: 10 } }, flbl(lab + rng),
          h('input', { className: 'input mono', inputMode: 'decimal', value: vals[p.name],
            onChange: function (e) { setVal(p.name, e.target.value); } }));
      }
      return h('label', { key: p.name, style: { display: 'block', marginTop: 10 } }, flbl(lab),
        h('input', { className: 'input', value: vals[p.name], maxLength: 40,
          onChange: function (e) { setVal(p.name, e.target.value); } }));
    }
    return h('div', { className: 'screen' },
      h('p', { className: 'hint', style: { marginTop: 0 } },
        'Настройте модель под себя — задайте текст и размеры, я сгенерирую её и сразу посчитаю цену.'),
      data === null && h('p', { className: 'hint' }, 'Загружаю…'),
      data && data.available === false && h('div', { className: 'card', style: { borderLeft: '3px solid var(--amber, #e0a800)' } },
        h('p', { className: 'hint', style: { margin: 0 } }, 'Настраиваемые модели сейчас недоступны. Загляните чуть позже 🙏')),
      data && data.available && !sel && (data.models || []).map(function (m) {
        return h('button', { key: m.id, className: 'mtile wide', style: { textAlign: 'left' }, onClick: function () { pick(m); } },
          h('div', { className: 'ic' }, icon('pencil')),
          h('div', { className: 'grow' }, h('div', { className: 'tt' }, m.name), h('div', { className: 'ss' }, m.desc)),
          icon('chev', { size: 18, style: { color: 'var(--faint)' } }));
      }),
      data && data.available && sel && h('div', null,
        h(Lab, { right: h('button', { className: 'chip', type: 'button', onClick: function () { setSel(null); } }, '← Другая') }, sel.name),
        h('div', { className: 'card' },
          (sel.params || []).map(field),
          h('button', { className: 'btn', style: { marginTop: 14 }, disabled: app.busy, onClick: function () { app.loadParametric(sel.id, vals); } },
            icon('box', { size: 18 }), ' Сгенерировать и посчитать')),
        h('p', { className: 'hint' }, 'После расчёта выберете пластик, количество и оформите заказ — как обычно.')));
  }

  // ── Лайтбокс: картинка на весь экран, тап по фону закрывает; name задан → кнопка
  // «Скачать» (share-sheet на телефоне / <a download> на десктопе). e99fe54b + 1a519e1c.
  // Подключён к центральному стеку «Назад» (window.PS_Back, back.js, OpenTodo 85d16a2a):
  // открытие кладёт слой на стек (показывает BackButton), а закрытие — ЛЮБЫМ путём (тап по
  // фону, аппаратная кнопка/жест) — идёт через PS_Back.back(), который снимает слой и сам
  // вызывает props.onClose. Так все вызовы PS_Lightbox (карточка заказа, аватар в профиле,
  // список заказов в админке) получают это бесплатно, без правок на местах использования.
  function Lightbox(props) {
    // useEffect обязателен ДО любого раннего return (иначе на разных рендерах разное число
    // хуков — React роняет всё дерево); внутри — обычная проверка условия. Cleanup снимает
    // слой при размонтировании/смене src — иначе слой повисает в стеке и первый свайп
    // «назад» уходит в пустоту (remove уже снятого back()'ом слоя — no-op).
    useEffect(function () {
      if (!props.src || !window.PS_Back) return;
      var layer = window.PS_Back.push(function () { if (props.onClose) props.onClose(); });
      return function () { window.PS_Back.remove(layer); };
    }, [props.src]);
    // ── Приближение (OpenTodo b470aac9): колесо/пинч/двойной тап зумят картинку, перетаскивание
    // возит её при зуме. Состояние (масштаб/сдвиг) держим в ref и применяем к <img> напрямую —
    // без ре-рендера на каждый кадр жеста (плавно); setZoomed только переключает курсор/логику
    // закрытия. Все жест-функции читают/пишут только ref-ы → без устаревших замыканий.
    var MAX_ZOOM = 8;
    var wrapRef = useRef(null), imgRef = useRef(null);
    var stRef = useRef({ scale: 1, tx: 0, ty: 0 });
    var tapRef = useRef({ t: 0, timer: 0 });
    var zs = useState(false); var zoomed = zs[0], setZoomed = zs[1];
    function apply(smooth) {
      var im = imgRef.current; if (!im) return;
      var st = stRef.current;
      im.style.transition = smooth ? 'transform .16s ease' : 'none';
      im.style.transform = 'translate(' + st.tx + 'px,' + st.ty + 'px) scale(' + st.scale + ')';
    }
    function clampPan() {
      var st = stRef.current, wrap = wrapRef.current, im = imgRef.current;
      if (!wrap || !im) return;
      var maxX = Math.max(0, (im.offsetWidth * st.scale - wrap.clientWidth) / 2);
      var maxY = Math.max(0, (im.offsetHeight * st.scale - wrap.clientHeight) / 2);
      st.tx = Math.max(-maxX, Math.min(maxX, st.tx));
      st.ty = Math.max(-maxY, Math.min(maxY, st.ty));
    }
    function setScale(ns, cx, cy, smooth) {
      var st = stRef.current, wrap = wrapRef.current;
      ns = Math.max(1, Math.min(MAX_ZOOM, ns));
      if (wrap && ns !== st.scale) {          // зум к точке (cx,cy): она остаётся на месте
        var r = wrap.getBoundingClientRect();
        var rx = r.left + r.width / 2, ry = r.top + r.height / 2;
        var dx = (cx == null ? rx : cx) - rx, dy = (cy == null ? ry : cy) - ry;
        var k = ns / st.scale;
        st.tx = dx - (dx - st.tx) * k;
        st.ty = dy - (dy - st.ty) * k;
      }
      st.scale = ns;
      if (ns <= 1.001) { st.scale = 1; st.tx = 0; st.ty = 0; }
      clampPan(); apply(smooth);
      setZoomed(st.scale > 1.01);
    }
    function toggleZoom(cx, cy) { setScale(stRef.current.scale > 1.01 ? 1 : 2.6, cx, cy, true); }
    // сброс при открытии/смене картинки
    useEffect(function () { stRef.current = { scale: 1, tx: 0, ty: 0 }; setZoomed(false); apply(false); }, [props.src]);
    // нативные слушатели (passive:false — иначе webview не даст preventDefault для пинча/колеса)
    useEffect(function () {
      var el = wrapRef.current;
      if (!el || !props.src) return undefined;
      function d2(t) { var dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY; return Math.hypot(dx, dy); }
      var pinch = null, pan = null;
      function onWheel(e) { e.preventDefault(); setScale(stRef.current.scale * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX, e.clientY, true); }
      function onTS(e) {
        if (e.touches.length === 2) { pan = null; pinch = { d: d2(e.touches), s: stRef.current.scale }; }
        else if (e.touches.length === 1 && stRef.current.scale > 1.01) {
          pan = { x: e.touches[0].clientX, y: e.touches[0].clientY, tx: stRef.current.tx, ty: stRef.current.ty };
        }
      }
      function onTM(e) {
        if (pinch && e.touches.length === 2) {
          e.preventDefault();
          var mx = (e.touches[0].clientX + e.touches[1].clientX) / 2, my = (e.touches[0].clientY + e.touches[1].clientY) / 2;
          setScale(pinch.s * d2(e.touches) / pinch.d, mx, my, false);
        } else if (pan && e.touches.length === 1) {
          e.preventDefault();
          stRef.current.tx = pan.tx + (e.touches[0].clientX - pan.x);
          stRef.current.ty = pan.ty + (e.touches[0].clientY - pan.y);
          clampPan(); apply(false);
        }
      }
      function onTE(e) { if (e.touches.length < 2) pinch = null; if (e.touches.length === 0) pan = null; }
      el.addEventListener('wheel', onWheel, { passive: false });
      el.addEventListener('touchstart', onTS, { passive: false });
      el.addEventListener('touchmove', onTM, { passive: false });
      el.addEventListener('touchend', onTE);
      el.addEventListener('touchcancel', onTE);
      return function () {
        el.removeEventListener('wheel', onWheel);
        el.removeEventListener('touchstart', onTS);
        el.removeEventListener('touchmove', onTM);
        el.removeEventListener('touchend', onTE);
        el.removeEventListener('touchcancel', onTE);
      };
    }, [props.src]);
    if (!props.src) return null;
    function download(e) {
      e.stopPropagation();
      fetch(props.src).then(function (r) { return r.blob(); }).then(function (b) {
        var name = props.name || 'image.png';
        var file = new File([b], name, { type: b.type || 'image/png' });
        if (navigator.canShare && navigator.canShare({ files: [file] })) {
          navigator.share({ files: [file] }).catch(function () {});
        } else {
          var url = URL.createObjectURL(b); var a = document.createElement('a');
          a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        }
      }).catch(function () {});
    }
    function closeNow() { if (window.PS_Back) window.PS_Back.back(); else if (props.onClose) props.onClose(); }
    function onImgTap(e) {                    // одиночный тап (без зума) — закрыть; двойной — зум
      e.stopPropagation();
      var now = Date.now(), tp = tapRef.current;
      if (now - tp.t < 300) { clearTimeout(tp.timer); tp.t = 0; toggleZoom(e.clientX, e.clientY); }
      else { tp.t = now; tp.timer = setTimeout(function () { tp.t = 0; if (stRef.current.scale <= 1.01) closeNow(); }, 300); }
    }
    function onImgMouseDown(e) {              // мышью возим картинку при зуме
      if (stRef.current.scale <= 1.01) return;
      e.preventDefault(); e.stopPropagation();
      var sx = e.clientX, sy = e.clientY, tx0 = stRef.current.tx, ty0 = stRef.current.ty;
      function mv(ev) { stRef.current.tx = tx0 + (ev.clientX - sx); stRef.current.ty = ty0 + (ev.clientY - sy); clampPan(); apply(false); }
      function up() { window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); }
      window.addEventListener('mousemove', mv); window.addEventListener('mouseup', up);
    }
    return h('div', { ref: wrapRef, onClick: function (e) { if (e.target === e.currentTarget) closeNow(); },
      style: { position: 'fixed', inset: 0, zIndex: 90, background: 'rgba(4,6,9,.92)',
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        gap: 14, overflow: 'hidden', touchAction: 'none', cursor: zoomed ? 'grab' : 'zoom-out' } },
      h('img', { ref: imgRef, src: props.src, alt: '', draggable: false, onClick: onImgTap, onMouseDown: onImgMouseDown,
        style: { maxWidth: '94vw', maxHeight: props.name ? '82vh' : '92vh', objectFit: 'contain',
          borderRadius: 14, willChange: 'transform', transformOrigin: 'center center',
          userSelect: 'none', cursor: zoomed ? 'grab' : 'zoom-in' } }),
      h('div', { style: { display: 'flex', gap: 10, alignItems: 'center' } },
        zoomed && h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '12px 18px' },
          onClick: function (e) { e.stopPropagation(); setScale(1, null, null, true); } }, '1:1'),
        props.name && h('button', { className: 'btn soft', style: { marginTop: 0, width: 'auto', padding: '12px 22px' }, onClick: download },
          icon('share', { size: 17 }), ' Скачать')),
      !zoomed && h('div', { style: { fontSize: 11.5, color: 'rgba(255,255,255,.5)', letterSpacing: '.3px' } },
        'Двойной тап или пинч — приблизить'));
  }

  // ───────────────────── Переписка: цитата и прочтение ─────────────────────
  // Клиентский чат (screens_more) и чат мастера (screens_admin) рисуют пузыри по-разному, а
  // вот эти две детали у них одинаковы — поэтому живут здесь, а не копией в каждом экране.

  // Сообщение из одних эмодзи (обычно это реакция, приехавшая из Telegram) показываем крупно:
  // сердечко кеглем текста выглядит опечаткой, а не реакцией.
  function isSoloEmoji(t) {
    var s = String(t || '').trim();
    return !!s && s.length <= 12 && !/[\wа-яё]/i.test(s);
  }

  // На что отвечают: короткая цитата над текстом. Цвет наследуется от пузыря (currentColor),
  // поэтому одинаково читается и на акцентной заливке своих, и на сером у чужих.
  function ChatQuote(props) {
    var q = props.q;
    if (!q) return null;
    return h('div', { style: { borderLeft: '2px solid currentColor', opacity: .62, paddingLeft: 7,
      marginBottom: 4, fontSize: 11.5, lineHeight: 1.35, maxHeight: 32, overflow: 'hidden' } },
      q.text || (q.image ? '📷 фото' : 'сообщение'));
  }

  // Галочка у СВОИХ сообщений. Отметка НАША, а не телеграмная: Bot API не сообщает боту, что
  // человек прочитал сообщение в чате. ✓✓ означает «вторая сторона открыла переписку или
  // ответила из Telegram», ✓ — «отправлено».
  function ChatRead(props) {
    if (!props.mine) return null;
    return h('span', { style: { marginLeft: 5, letterSpacing: '-2px' },
      title: props.read ? 'Прочитано' : 'Отправлено' }, props.read ? '✓✓' : '✓');
  }

  Object.assign(window, { PS_Lab: Lab, PS_Breakdown: Breakdown, Hub: Hub, ModelInput: ModelInput, Parametric: Parametric, Config: Config, Slicing: Slicing, Quote: Quote, PS_ErrorOverlay: ErrorOverlay, PrinterLoader: PrinterLoader, PS_PreviewImg: PreviewImg, PS_PolyLoader: PolyLoader, BlobLoader: BlobLoader, CloudLoader: CloudLoader, PS_Lightbox: Lightbox, PS_FavHeart: FavHeart, PS_ChatQuote: ChatQuote, PS_ChatRead: ChatRead, PS_SoloEmoji: isSoloEmoji });
})();
