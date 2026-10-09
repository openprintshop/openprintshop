// screens_apps.js — «Приложения»: таб между «Расчётом» и «Каталогом» (2026-09-02, решение
// владельца). Заведён ради ОДНОГО инструмента — «Мастерской органайзеров», переехавшей сюда
// из каталога: это не раздел коллекции, а самостоятельное приложение с проектами. Плитки
// планировщика стены и «Заказа по фото» здесь были один день и сняты тем же решением
// (02.09): вкладка — про мастерскую, ничего не должно отвлекать. Заказ по фото по-прежнему
// доступен из поиска и из ленты; планировщик — личный инструмент владельца (edition dev_only),
// его вход остался в админке.
//
// Превью органайзера — НАСТОЯЩЕЕ 3D (three.js, вендорный r147 — тот же файл, что у вьюера
// моделей и движка мастерской): маленькая сцена «лоток со вставками», медленно вращается,
// пока плитка на экране. Ядро three.js подгружается лениво по тому же маркеру data-three3d,
// что у screens_3d.js/screens_gridfinity.js, поэтому второй раз не качается. Без WebGL, до
// загрузки и при ошибке — изометрическая SVG-картинка того же лотка (та же раскладка).
(function () {
  var h = React.createElement, useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon, API = window.API;

  // ── ленивое ядро three.js ──
  var _ver = (window.__APPVER__ && window.__APPVER__.indexOf('%') < 0) ? window.__APPVER__ : '';
  var _base = (function () {
    try { var me = document.currentScript && document.currentScript.src; return me ? me.slice(0, me.lastIndexOf('/') + 1) : ''; }
    catch (e) { return ''; }
  })();
  function vurl(p) { return _base + p + (_ver ? '?v=' + encodeURIComponent(_ver) : ''); }
  var _threeP = null;
  function ensureThreeCore() {
    if (window.THREE) return Promise.resolve();
    if (_threeP) return _threeP;
    _threeP = new Promise(function (resolve, reject) {
      var src = 'vendor/three.min.js';
      var el = document.querySelector('script[data-three3d="' + src + '"]');
      if (el && el.getAttribute('data-failed')) { el.parentNode.removeChild(el); el = null; }
      if (el) {
        if (el.getAttribute('data-loaded') || window.THREE) { resolve(); return; }
        el.addEventListener('load', function () { resolve(); });
        el.addEventListener('error', function () { reject(new Error('three')); });
        return;
      }
      el = document.createElement('script');
      el.src = vurl(src); el.async = false; el.setAttribute('data-three3d', src);
      el.addEventListener('load', function () { el.setAttribute('data-loaded', '1'); resolve(); });
      el.addEventListener('error', function () { el.setAttribute('data-failed', '1'); reject(new Error('three')); });
      document.head.appendChild(el);
    }).catch(function (e) { _threeP = null; throw e; });   // дать шанс повторить при следующем заходе
    return _threeP;
  }
  function webglOK() {
    if (window.GFStudio_webglOK) return window.GFStudio_webglOK();
    try { var c = document.createElement('canvas'); return !!(c.getContext('webgl') || c.getContext('experimental-webgl')); }
    catch (e) { return false; }
  }
  var REDUCED = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  // Раскладка превью — одна и для 3D, и для SVG: лоток 6×4 клеток по 42 мм (шаг Gridfinity),
  // вставки [колонка, ряд, ширина, глубина, высота мм, цвет].
  var GRID = 42, COLS = 6, ROWS = 4;
  var BINS = [
    [0, 0, 2, 2, 42, 0xff7a1a], [2, 0, 1, 2, 35, 0x39a6a3], [3, 0, 3, 1, 28, 0x3a86ff],
    [3, 1, 2, 1, 28, 0xa98bff], [5, 1, 1, 1, 21, 0xffb020], [0, 2, 3, 2, 35, 0x2dd47e],
    [3, 2, 3, 2, 42, 0x8595a5],
  ];

  // ── 3D-сцена ──
  // r147 ≠ современному three по цвету: без sRGB-выхода и физически корректного света фон
  // читается серым, а пластик кислотным (урок порта движка мастерской) — поэтому каждый цвет
  // через convertSRGBToLinear, а на рендерере выставлены outputEncoding и physicallyCorrectLights.
  function col(T, hex) { return new T.Color(hex).convertSRGBToLinear(); }
  function rrect(T, w, d, r) {   // скруглённый прямоугольник в плане, центр в нуле
    var s = new T.Shape(), x = -w / 2, y = -d / 2;
    r = Math.min(r, w / 2, d / 2);
    s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r);
    s.lineTo(x + w, y + d - r); s.quadraticCurveTo(x + w, y + d, x + w - r, y + d);
    s.lineTo(x + r, y + d); s.quadraticCurveTo(x, y + d, x, y + d - r);
    s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
    return s;
  }
  // Extrude тянет вдоль +Z; поворот на −90° вокруг X переводит выдавливание в +Y (вверх).
  function slab(T, w, d, hgt, r) {
    var g = new T.ExtrudeGeometry(rrect(T, w, d, r), { depth: hgt, bevelEnabled: false, curveSegments: 6 });
    g.rotateX(-Math.PI / 2);
    return g;
  }
  // Вставка — стенки с настоящим отверстием (Shape.holes): сквозь неё видна плита лотка, то
  // есть лоток читается полым, а не набором кубиков.
  function binGeo(T, w, d, hgt, wall, r) {
    var s = rrect(T, w, d, r);
    s.holes.push(rrect(T, w - 2 * wall, d - 2 * wall, Math.max(0.6, r - wall)));
    var g = new T.ExtrudeGeometry(s, { depth: hgt, bevelEnabled: false, curveSegments: 6 });
    g.rotateX(-Math.PI / 2);
    return g;
  }
  function mountScene(host) {
    var T = window.THREE;
    var renderer = new T.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.setClearColor(0x000000, 0);
    if (T.sRGBEncoding !== undefined) renderer.outputEncoding = T.sRGBEncoding;
    renderer.physicallyCorrectLights = true;
    host.appendChild(renderer.domElement);

    var scene = new T.Scene();
    var camera = new T.PerspectiveCamera(28, 1, 10, 3000);
    camera.position.set(0, 250, 360); camera.lookAt(0, 12, 0);
    scene.add(new T.HemisphereLight(col(T, 0xdde7f5), col(T, 0x131a22), 1.5));
    var sun = new T.DirectionalLight(col(T, 0xffffff), 2.8); sun.position.set(140, 260, 120); scene.add(sun);
    var fill = new T.DirectionalLight(col(T, 0xff9a4a), 0.9); fill.position.set(-180, 80, -60); scene.add(fill);

    var group = new T.Group(); scene.add(group);
    var geos = [], mats = [];
    function mesh(g, m) { geos.push(g); mats.push(m); var o = new T.Mesh(g, m); group.add(o); return o; }
    var W = COLS * GRID, D = ROWS * GRID;
    mesh(slab(T, W + 10, D + 10, 5, 5), new T.MeshStandardMaterial({ color: col(T, 0x2b3441), roughness: .8 }));
    // сетка 42 мм тонкими линиями по плите — «это Gridfinity», а не просто поднос
    var lineMat = new T.LineBasicMaterial({ color: col(T, 0x3c4756), transparent: true, opacity: .7 }); mats.push(lineMat);
    var pts = [];
    for (var i = 0; i <= COLS; i++) pts.push(new T.Vector3(-W / 2 + i * GRID, 5.2, -D / 2), new T.Vector3(-W / 2 + i * GRID, 5.2, D / 2));
    for (var j = 0; j <= ROWS; j++) pts.push(new T.Vector3(-W / 2, 5.2, -D / 2 + j * GRID), new T.Vector3(W / 2, 5.2, -D / 2 + j * GRID));
    var lg = new T.BufferGeometry().setFromPoints(pts); geos.push(lg);
    group.add(new T.LineSegments(lg, lineMat));
    BINS.forEach(function (b) {
      var w = b[2] * GRID - 0.8, d = b[3] * GRID - 0.8;
      var o = mesh(binGeo(T, w, d, b[4], 1.8, 4), new T.MeshStandardMaterial({ color: col(T, b[5]), roughness: .5 }));
      o.position.set(-W / 2 + (b[0] + b[2] / 2) * GRID, 5, -D / 2 + (b[1] + b[3] / 2) * GRID);
    });
    group.rotation.y = -0.55;

    var raf = 0, visible = true, last = 0;
    function size() {
      var w = host.clientWidth || 300, hh = host.clientHeight || 220;
      renderer.setSize(w, hh, false); camera.aspect = w / hh; camera.updateProjectionMatrix();
    }
    size();
    // Кадры только пока плитка видна и вкладка активна; при prefers-reduced-motion — один кадр
    // (без вращения), иначе медленный оборот за ~25 с.
    function frame(t) {
      raf = 0;
      if (!visible || document.hidden) return;
      var dt = last ? Math.min(0.05, (t - last) / 1000) : 0; last = t;
      if (!REDUCED) group.rotation.y += dt * 0.25;
      renderer.render(scene, camera);
      if (!REDUCED) raf = requestAnimationFrame(frame);
    }
    function kick() { if (!raf) { last = 0; raf = requestAnimationFrame(frame); } }
    kick();
    var io = null, ro = null;
    if (window.IntersectionObserver) {
      io = new IntersectionObserver(function (es) { visible = !!(es[0] && es[0].isIntersecting); if (visible) kick(); });
      io.observe(host);
    }
    if (window.ResizeObserver) { ro = new ResizeObserver(function () { size(); kick(); }); ro.observe(host); }
    function onVis() { if (!document.hidden) kick(); }
    document.addEventListener('visibilitychange', onVis);
    return function destroy() {
      if (raf) cancelAnimationFrame(raf);
      document.removeEventListener('visibilitychange', onVis);
      if (io) io.disconnect();
      if (ro) ro.disconnect();
      geos.forEach(function (g) { g.dispose(); }); mats.forEach(function (m) { m.dispose(); });
      renderer.dispose();
      try { renderer.forceContextLoss(); } catch (e) {}   // контекстов WebGL у WebView мало — отдаём сразу
      if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
    };
  }

  // ── SVG-фолбэк: тот же лоток в изометрии (без WebGL, до загрузки three, при ошибке) ──
  function shade(hex, f) {   // затемнить цвет: f = 0.82 → на 18 % темнее
    var r = (hex >> 16) & 255, g = (hex >> 8) & 255, b = hex & 255;
    return 'rgb(' + Math.round(r * f) + ',' + Math.round(g * f) + ',' + Math.round(b * f) + ')';
  }
  function IsoTray(props) {
    var C = Math.cos(Math.PI / 6), S = 0.5, K = 0.78, W = COLS * GRID, D = ROWS * GRID;
    function P(x, y, z) { return [((x - y) * C) * K + 180, ((x + y) * S - z) * K + 128]; }
    function pts(arr) { return arr.map(function (p) { return p.join(','); }).join(' '); }
    // призма с основанием (x0..x0+w, y0..y0+d) и высотой hgt; видны верх, передняя (y0+d) и правая (x0+w) грани
    function box(x0, y0, w, d, hgt, hex, key, hollow) {
      var top = [P(x0, y0, hgt), P(x0 + w, y0, hgt), P(x0 + w, y0 + d, hgt), P(x0, y0 + d, hgt)];
      var front = [P(x0, y0 + d, hgt), P(x0 + w, y0 + d, hgt), P(x0 + w, y0 + d, 0), P(x0, y0 + d, 0)];
      var right = [P(x0 + w, y0, hgt), P(x0 + w, y0 + d, hgt), P(x0 + w, y0 + d, 0), P(x0 + w, y0, 0)];
      var wall = 3, inner = hollow ? [P(x0 + wall, y0 + wall, hgt), P(x0 + w - wall, y0 + wall, hgt), P(x0 + w - wall, y0 + d - wall, hgt), P(x0 + wall, y0 + d - wall, hgt)] : null;
      return h('g', { key: key },
        h('polygon', { points: pts(front), fill: shade(hex, 0.78) }),
        h('polygon', { points: pts(right), fill: shade(hex, 0.62) }),
        h('polygon', { points: pts(top), fill: shade(hex, 1) }),
        inner && h('polygon', { points: pts(inner), fill: shade(hex, 0.42) }));
    }
    // порядок отрисовки — «сзади вперёд»: по сумме x+y основания
    var bins = BINS.slice().sort(function (a, b) { return (a[0] + a[1]) - (b[0] + b[1]); });
    return h('svg', { className: 'ap-iso', viewBox: '0 0 360 224', 'aria-hidden': true,
      style: { opacity: props.hidden ? 0 : 1, transition: 'opacity .5s' } },
      box(-W / 2 - 5, -D / 2 - 5, W + 10, D + 10, 5, 0x2b3441, 'base', false),
      bins.map(function (b, i) {
        return box(-W / 2 + b[0] * GRID + 0.6, -D / 2 + b[1] * GRID + 0.6, b[2] * GRID - 1.2, b[3] * GRID - 1.2, 5 + b[4], b[5], 'b' + i, true);
      }));
  }

  function GfxPreview3D() {
    var host = useRef(null);
    var ms = useState('svg'); var mode = ms[0], setMode = ms[1];   // svg | 3d
    useEffect(function () {
      if (!webglOK()) return undefined;
      var dead = false, destroy = null;
      ensureThreeCore().then(function () {
        if (dead || !host.current) return;
        try { destroy = mountScene(host.current); setMode('3d'); }
        catch (e) { destroy = null; }   // упал рендерер — остаёмся на SVG, ошибку не показываем
      }).catch(function () {});
      return function () { dead = true; if (destroy) destroy(); };
    }, []);
    return h('div', { className: 'ap-vis' },
      h(IsoTray, { hidden: mode === '3d' }),
      h('div', { ref: host, className: 'ap-canvas' + (mode === '3d' ? ' on' : '') }),
      h('span', { className: 'ap-tag' }, '3D-конфигуратор'),
      mode === '3d' && h('span', { className: 'ap-3d' }, REDUCED ? '3D' : '3D · вращается'));
  }

  function plural(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    return b > 1 && b < 5 ? few : (b === 1 ? one : many);
  }

  function AppsScreen(props) {
    var app = props.app;
    var ps = useState(null); var proj = ps[0], setProj = ps[1];   // число проектов мастерской у клиента
    useEffect(function () {
      if (!API.gfxProjects) return;
      API.gfxProjects().then(function (j) { if (j && j.ok) setProj((j.projects || []).length); }).catch(function () {});
    }, []);
    return h('div', { className: 'screen apps' },
      // ── широкая плитка-герой: органайзер в ящик с живым 3D ──
      h('button', { className: 'apptile hero', type: 'button', onClick: function () { app.go('workshop'); } },
        h(GfxPreview3D, null),
        h('div', { className: 'ap-body' },
          h('div', { className: 'ap-kicker' }, 'Мастерская органайзеров'),
          h('div', { className: 'ap-tt' }, 'Органайзер в ящик'),
          h('div', { className: 'ap-ss' }, 'Соберите лотки и вставки Gridfinity под размеры своего ящика прямо в 3D — а мы напечатаем, партиями и по мере надобности.'),
          h('div', { className: 'ap-row' },
            h('span', { className: 'ap-cta' }, proj > 0 ? 'Мои проекты' : 'Начать', icon('chev', { size: 15 })),
            proj != null && h('span', { className: 'ap-meta' },
              proj > 0 ? (proj + ' ' + plural(proj, 'проект', 'проекта', 'проектов')) : 'первый проект — за минуту')))),

      h('p', { className: 'hint ap-foot' }, 'Расчёт по ссылке или файлу и готовые изделия — на вкладке «Расчёт».'));
  }

  Object.assign(window, { AppsScreen: AppsScreen });
})();
