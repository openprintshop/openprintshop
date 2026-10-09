// screens_gridfinity.js — загрузчик 3D-движка Gridfinity (gridfinity_engine.js, порт
// design_handoff_gridfinity) и старый экран-одиночка конфигуратора.
//
// Движок (~150 КиБ) и three.js подгружаются ЛЕНИВО при первом заходе — по той же причине,
// что admin-скрипты в app.js: index.html отдаётся с no-store, и безусловный тег гонял бы
// движок каждому клиенту при каждом открытии мини-аппа.
//
// Сам конфигуратор теперь живёт внутри ПРОЕКТА («Мастерская органайзеров»,
// screens_workshop.js): раскладка — одна из четырёх вкладок проекта, рядом со
// спецификацией деталей и партиями. Этот файл остался загрузчиком (его зовут оба экрана)
// плюс перенаправлением со старого адреса `gridfinity`.
(function () {
  var h = React.createElement, useEffect = React.useEffect;

  // Дешёвая синхронная проверка WebGL (как webglOK в screens_3d.js): на урезанном WebView
  // без неё mount() падал бы на new WebGLRenderer и экран зависал на «Загружаю…».
  var _webgl = null;
  function webglOK() {
    if (_webgl !== null) return _webgl;
    try {
      var c = document.createElement('canvas');
      _webgl = !!(c.getContext('webgl') || c.getContext('experimental-webgl'));
    } catch (e) { _webgl = false; }
    return _webgl;
  }

  // ?v=… — общий ключ кеша фронта (как у ленивых admin-скриптов): без него залипшая копия
  // движка в WebView пережила бы деплой. В raw-режиме (плейсхолдер не вшит) — без ?v.
  var _ver = (window.__APPVER__ && window.__APPVER__.indexOf('%') < 0) ? window.__APPVER__ : '';
  // Каталог, из которого приехал ЭТОТ файл. Мини-апп живёт в /app/, и относительные пути
  // работали сами; но страница сессии ПК — /gfx/desk/{token}, и оттуда `vendor/three.min.js`
  // ушёл бы в /gfx/desk/vendor/… и вернул 404 («не удалось загрузить 3D-движок» при живом
  // проекте и целых файлах). Берём базу у собственного тега, а не зашиваем '/app/':
  // так же верно и в мини-аппе, и на любой будущей странице.
  var _base = (function () {
    try {
      var me = document.currentScript && document.currentScript.src;
      return me ? me.slice(0, me.lastIndexOf('/') + 1) : '';
    } catch (e) { return ''; }
  })();
  function vurl(p) { return _base + p + (_ver ? '?v=' + encodeURIComponent(_ver) : ''); }
  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      // маркер data-three3d общий со screens_3d.js: three.min.js, затянутый 3D-вьюером,
      // здесь не грузится второй раз (и наоборот)
      var el = document.querySelector('script[data-three3d="' + src + '"]');
      // тег, у которого load/error уже отстрелялись ошибкой, ждать бессмысленно — события
      // не повторятся, и «Повторить» вис бы на вечном промисе. Сносим и создаём заново.
      if (el && el.getAttribute('data-failed')) { el.parentNode.removeChild(el); el = null; }
      if (el) {
        if (el.getAttribute('data-loaded')) return resolve();
        el.addEventListener('load', function () { resolve(); });
        el.addEventListener('error', function () { reject(new Error('script ' + src)); });
        return;
      }
      el = document.createElement('script');
      el.src = vurl(src); el.async = false; el.setAttribute('data-three3d', src);
      el.addEventListener('load', function () { el.setAttribute('data-loaded', '1'); resolve(); });
      el.addEventListener('error', function () { el.setAttribute('data-failed', '1'); reject(new Error('script ' + src)); });
      document.head.appendChild(el);
    });
  }
  var _enginePromise = null;
  function ensureEngine() {
    if (!webglOK()) return Promise.reject(new Error('no webgl'));
    if (window.GFStudio && window.THREE && window.THREE.OrbitControls) return Promise.resolve();
    if (_enginePromise) return _enginePromise;
    _enginePromise = ['vendor/three.min.js', 'vendor/three-OrbitControls.js', 'gridfinity_engine.js']
      .reduce(function (chain, src) { return chain.then(function () { return loadScript(src); }); }, Promise.resolve())
      .then(function () {
        if (!(window.GFStudio && window.THREE && window.THREE.OrbitControls)) throw new Error('GFStudio not ready');
      })
      .catch(function (e) { _enginePromise = null; throw e; });   // дать шанс повторить
    return _enginePromise;
  }

  // Экспорт для экрана проекта (screens_workshop.js): он монтирует движок сам, внутри
  // вкладки «3D», и держит его смонтированным всё время жизни проекта.
  window.GFStudio_ensure = ensureEngine;
  window.GFStudio_webglOK = webglOK;

  // Старый адрес экрана: у кого-то он в истории навигации или в кнопке бота. Ведём в
  // мастерскую, а не показываем пустой конфигуратор без проекта.
  window.GridfinityScreen = function (props) {
    var app = props.app;
    useEffect(function () {
      var t = setTimeout(function () { app.goTab('workshop'); }, 0);
      return function () { clearTimeout(t); };
    }, []);
    return h('div', { className: 'screen' },
      h('div', { className: 'hint', style: { padding: '40px 0', textAlign: 'center' } },
        'Открываю мастерскую…'));
  };
})();
