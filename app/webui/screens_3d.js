// screens_3d.js — Интерактивный 3D-просмотр модели (B1): three.js-вьюер.
//
// Две площадки, ОДИН движок. mountViewer() строит сцену/загрузку/RAF/dispose без React;
// его хостят и полноэкранная модалка (Viewer3DModal), и инлайн-превью (Inline3D). Инлайн —
// ПЕРВИЧНЫЙ визуал на Config/Quote там, где рендер и так герой (загрузка файла/ссылки);
// PNG остаётся полноправным фолбэком (тяжёлый файл, STEP, сбой парсинга, нет WebGL, сотовая сеть).
//
// Почему PNG-фолбэк — ЧАСТЫЙ путь, а не край: реальные .3mf от клиентов (MakerWorld/Bambu, и даже
// выхлоп слайсера) идут в production-extension — геометрия в отдельных 3D/Objects/*.model через
// p:path (тысячи мешей, десятки-сотни МБ). three.js r147 их не собирает (падает на .mesh), а даже
// если бы собирал — телефон не потянет. Поэтому крупные .3mf НЕ автозагружаются (гейт по размеру),
// PNG виден сразу, а тап приводит к быстрому фолбэку с внятной причиной. 3D никогда не блокирует заказ.
//
// three.js вендорится в app/webui/vendor/ (пин r147 — последний с classic examples/js-лоадерами
// и UMD-глобалом THREE; см. комментарии в vendor-файлах). Грузится ЛЕНИВО, только при первом
// реальном показе вьюера, чтобы не тащить ~1 МБ в каждую Telegram-сессию.
(function () {
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useRef = React.useRef;
  var icon = window.icon;

  // Версия фронта для ?v=… у vendor-скриптов — тот же ключ кеша, что у остального webui
  // (иначе залипшая копия three.js в WebView переживёт деплой). '%%VER%%' в raw-режиме → без ?v.
  var VER = (window.__APPVER__ && window.__APPVER__.indexOf('%') < 0) ? window.__APPVER__ : '';
  function vurl(p) { return VER ? (p + '?v=' + encodeURIComponent(VER)) : p; }

  // Порядок важен: three.min.js регистрирует глобал THREE, лоадеры дописывают в него классы при
  // ЗАГРУЗКЕ (THREE.GLTFLoader = …), поэтому идут после ядра. Нужен ТОЛЬКО GLTFLoader: сервер отдаёт
  // ЛЮБУЮ модель (.stl/.obj/.3mf, включая Bambu/MakerWorld) уже бинарным GLB со сценой из ДЕТАЛЕЙ
  // (webapp._model3d_glb_bytes) — детали нужны для мультицвета (красим part[i] в реальном времени),
  // поэтому теперь GLB/GLTFLoader вместо плоского STL; капризные 3MFLoader/OBJLoader/fflate не нужны.
  var THREE_SCRIPTS = [
    'vendor/three.min.js',
    'vendor/three-OrbitControls.js',
    'vendor/three-GLTFLoader.js',
  ];

  var _threePromise = null;
  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector('script[data-three3d="' + src + '"]');
      if (existing) {
        if (existing.getAttribute('data-loaded')) return resolve();
        existing.addEventListener('load', function () { resolve(); });
        existing.addEventListener('error', function () { reject(new Error('script ' + src)); });
        return;
      }
      var el = document.createElement('script');
      el.src = vurl(src); el.async = false; el.setAttribute('data-three3d', src);
      el.addEventListener('load', function () { el.setAttribute('data-loaded', '1'); resolve(); });
      el.addEventListener('error', function () { reject(new Error('script ' + src)); });
      document.head.appendChild(el);
    });
  }
  // Ленивая подгрузка three.js + лоадеров одним промисом (кешируется на модуль). Скрипты грузим
  // строго последовательно — глобальные лоадеры зависят от уже загруженного ядра THREE.
  function ensureThree() {
    if (window.THREE && window.THREE.OrbitControls && window.THREE.GLTFLoader) return Promise.resolve();
    if (_threePromise) return _threePromise;
    _threePromise = THREE_SCRIPTS.reduce(function (chain, src) {
      return chain.then(function () { return loadScript(src); });
    }, Promise.resolve()).then(function () {
      if (!(window.THREE && window.THREE.GLTFLoader)) throw new Error('THREE not ready');
    }).catch(function (e) { _threePromise = null; throw e; });   // дать шанс повторить при новом открытии
    return _threePromise;
  }

  // Дешёвая синхронная проверка WebGL — аффорданс «3D» не должен появляться там, где вьюер не
  // заведётся (старый/урезанный WebView, low-end Android). Кешируем: контекст создаём один раз.
  var _webgl = null;
  function webglOK() {
    if (_webgl !== null) return _webgl;
    try {
      var c = document.createElement('canvas');
      _webgl = !!(window.WebGLRenderingContext
        && (c.getContext('webgl2') || c.getContext('webgl') || c.getContext('experimental-webgl')));
    } catch (e) { _webgl = false; }
    return _webgl;
  }

  function fmtOf(model) {
    return String((model && (model.model_ext || model.model_fmt)) || '').toLowerCase();
  }
  // Показывать ли аффорданс «3D»: бэк уже отгейтил формат (.stl/.obj/.3mf) и размер
  // (model_viewable), нам остаётся проверить WebGL. THREE тут ещё НЕ загружен (ленивая загрузка
  // на клик/показ) — намеренно: требовать window.THREE значило бы никогда не показать кнопку.
  function canShow3D(model) {
    return !!(model && model.model_viewable && webglOK());
  }

  // ─────────────── Гейт автозагрузки инлайн-3D (крайне важно на сотовой) ───────────────
  // 3D первичен ТАМ, ГДЕ ДЁШЕВО: маленький меш + не metered-сеть. Иначе — armed-but-idle: показываем
  // PNG и точку «Смотреть в 3D», тап всегда апгрейдит. Гейт по байтам сильно ниже 30-МБ хард-порога.
  var AUTOLOAD_MAX_BYTES = 8 * 1024 * 1024;
  function meteredConn() {
    try {
      var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
      if (c && (c.saveData || /(^|-)2g$|^3g$/.test(String(c.effectiveType || '')))) return true;
    } catch (e) {}
    return false;   // navigator.connection нет на iOS WebView → считаем сеть неизвестной (не metered)
  }
  function shouldAutoload(model) {
    // 3D — ОСНОВНОЕ превью (просьба владельца): встраиваем и грузим СРАЗУ, как только модель
    // просматриваема и есть WebGL. Ни размер исходника, ни тип сети больше не гейтят: сервер отдаёт
    // ДЕЦИИРОВАННЫЙ GLB (≤ preview._MAX_FACES) — он лёгкий даже для тяжёлого .3mf, поэтому и на
    // телефоне/сотовой встраиваем сразу. PNG остаётся фолбэком при сбое/STEP/без WebGL.
    return canShow3D(model);
  }

  // Причина PNG-фолбэка, читаемая клиентом. Меш извлекает СЕРВЕР (в т.ч. из Bambu/MakerWorld .3mf),
  // поэтому до фолбэка доходит редко — только если сеть/разбор совсем не задались.
  function reasonFor(fmt, err) {
    var msg = (err && err.message) || '';
    if (msg === 'timeout') return 'Загрузка 3D заняла слишком долго';
    return 'Не удалось загрузить 3D-модель';
  }

  // ─────────────── Движок (без React): сцена + загрузка + RAF + dispose ───────────────
  // Возвращает { promise, dispose, api }. promise резолвится на 'ready' и реджектится с причиной.
  // Один и тот же движок хостят модалка и инлайн — разница лишь в opts (autoRotate, pixelCap,
  // pauseWhenHidden, onProgress). Полный dispose (forceContextLoss) идентичен старому — иначе
  // WebGL-контексты копятся в Telegram WebView.
  var NEUTRAL = 0x9aa3ad;   // дефолтный серый, когда цвет клиента не задан (как было у одного материала)

  // ─────────────── Переливающиеся цвета (silk PLA «синий-пурпурный») ───────────────
  // Такие цвета приходят из палитры НЕ одним hex, а строкой linear-gradient(135deg,#a,#b) —
  // сервер (_color_css) собирает её из имени «через тире». THREE.Color.set такую строку не ест
  // (раньше падало в catch → деталь серела). Достаём hex-стопы; ≥2 стопов = двухцветный режим
  // материала: базовый color + uColorB, смешение по френелю (угол грани к камере) в шейдере —
  // перелив «играет» при вращении модели, как у настоящего шёлкового филамента.
  function cssStops(css) {
    var s = String(css || '');
    if (s.indexOf('gradient') < 0) return s ? [s] : [];
    return s.match(/#[0-9a-fA-F]{3,8}/g) || [];
  }

  function mountViewer(wrap, opts) {
    opts = opts || {};
    var st = {
      disposed: false, raf: 0, running: false, ready: false, io: null,
      renderer: null, scene: null, camera: null, controls: null, canvas: null,
      // Материал НА КАЖДУЮ деталь (part0..N из GLB-сцены) — красим их независимо для мультицвета.
      // partSlots[i] — слот цвета детали i из имени узла (part…_sN, сервер знает экструдер детали);
      // -1 = слота нет → раскраска по порядковому индексу, как раньше.
      partMats: [], partSlots: [], parts: [], geoms: [], start: { pos: null, target: null }, onResize: null, onDown: null,
      // Отложенный цвет: детали могут приехать позже загрузкой GLB — запоминаем последний colors/multicolor
      // и применяем, как только материалы созданы (и при каждом setColors из React).
      pendingColors: opts.colors || null, pendingMulti: !!opts.multicolor,
    };
    var settled = false, _res = null, _rej = null;
    var promise = new Promise(function (a, b) { _res = a; _rej = b; });
    function done() { if (!settled && !st.disposed) { settled = true; _res(); } }
    function fail(reason) { if (!settled && !st.disposed) { settled = true; _rej(new Error(reason || 'error')); } }

    // Материал одной детали. НЕЙТРАЛЬНЫЙ по умолчанию; цвет клиента накатывает applyColors.
    // Всегда несёт «спящий» двухцветный режим (uIriOn=0): шейдер один на все детали, перелив
    // включается uniform'ом без перекомпиляции — смена solid↔silk остаётся мгновенной.
    function makePartMat(THREE) {
      var mat = new THREE.MeshStandardMaterial({ color: NEUTRAL, roughness: 0.72, metalness: 0.04 });
      mat.userData.uColorB = { value: new THREE.Color(NEUTRAL) };
      mat.userData.uIriOn = { value: 0 };
      mat.onBeforeCompile = function (sh) {
        sh.uniforms.uColorB = mat.userData.uColorB;
        sh.uniforms.uIriOn = mat.userData.uIriOn;
        // vIriF — френель вершины (0 — грань смотрит в камеру, 1 — скользящий угол).
        // transformedNormal/mvPosition уже в области видимости после project_vertex (r147).
        sh.vertexShader = sh.vertexShader
          .replace('#include <common>', '#include <common>\nvarying float vIriF;')
          .replace('#include <project_vertex>',
            '#include <project_vertex>\nvIriF = 1.0 - abs(dot(normalize(transformedNormal), normalize(-mvPosition.xyz)));');
        sh.fragmentShader = sh.fragmentShader
          .replace('#include <common>', '#include <common>\nvarying float vIriF;\nuniform vec3 uColorB;\nuniform float uIriOn;')
          .replace('vec4 diffuseColor = vec4( diffuse, opacity );',
            'vec4 diffuseColor = vec4( mix( diffuse, uColorB, uIriOn * smoothstep( 0.1, 0.9, vIriF ) ), opacity );');
      };
      return mat;
    }
    // Цвет одной детали: solid ('#hex'/'rgb()'/имя) или перелив (linear-gradient → 2 стопа).
    // Мусорная строка → нейтральный серый (как раньше, без падения).
    function setPartColor(mat, css) {
      var stops = cssStops(css);
      var iri = stops.length >= 2;
      try {
        mat.color.set(stops.length ? stops[0] : NEUTRAL);
        if (iri) mat.userData.uColorB.value.set(stops[1]);
      } catch (e) { mat.color.set(NEUTRAL); iri = false; }
      mat.userData.uIriOn.value = iri ? 1 : 0;
      // шёлк заметно глянцевее матового пластика — иначе перелив «глохнет» в шершавости
      mat.roughness = iri ? 0.38 : 0.72;
      mat.metalness = iri ? 0.18 : 0.04;
    }
    // Реалтайм-перекраска: НЕ перезагружаем меш, только цвет/uniform'ы материала — мгновенно
    // (без mat.needsUpdate: он форсил бы перекомпиляцию шейдера, а для цвета она не нужна).
    //   одноцвет → ВСЕ детали = colors[0];  мультицвет → part по его СЛОТУ из имени узла GLB
    // (colors[slot % n] — слот согласован с порядком mc_colors, см. webapp._model3d_glb_bytes);
    // деталь без слота (старый кэш GLB, STL/OBJ, экструдер не размечен) → фолбэк по индексу
    // colors[i % n], как раньше. colors пуст → нейтральный серый.
    function applyColors(colors, multicolor) {
      st.pendingColors = colors || null; st.pendingMulti = !!multicolor;   // запомнить для деталей, что подгрузятся позже
      var THREE = window.THREE;
      if (!THREE || !st.partMats.length) return;
      var list = (colors && colors.length) ? colors : null;
      st.partMats.forEach(function (mat, i) {
        var slot = (st.partSlots[i] >= 0) ? st.partSlots[i] : i;
        setPartColor(mat, !list ? null : (multicolor ? list[slot % list.length] : list[0]));
      });
      if (st.renderer && st.scene && st.camera && !st.running) st.renderer.render(st.scene, st.camera);   // idle-фаза: показать смену сразу
    }

    var apiObj = {
      reset: function () {
        if (!st.controls || !st.start.pos) return;
        st.camera.position.copy(st.start.pos); st.controls.target.copy(st.start.target); st.controls.update();
      },
      wire: function (on) { st.partMats.forEach(function (m) { m.wireframe = !!on; }); },
      spin: function (on) { if (st.controls) st.controls.autoRotate = !!on; },
      setColors: function (colors, multicolor) { applyColors(colors, multicolor); },   // React дергает при смене цвета
    };

    function loop() {
      if (st.disposed || !st.running) return;
      st.raf = requestAnimationFrame(loop);
      if (st.controls) st.controls.update();
      if (st.renderer && st.scene && st.camera) st.renderer.render(st.scene, st.camera);
    }
    function startLoop() { if (st.running || st.disposed || !st.ready) return; st.running = true; loop(); }
    function stopLoop() { st.running = false; if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; } }

    ensureThree().then(function () {
      if (st.disposed || !wrap) return;
      var THREE = window.THREE;
      var W = Math.max(1, wrap.clientWidth), H = Math.max(1, wrap.clientHeight);
      st.canvas = document.createElement('canvas');
      try {
        st.renderer = new THREE.WebGLRenderer({ canvas: st.canvas, antialias: true, alpha: true });
      } catch (e) { fail('В этом браузере недоступен WebGL'); return; }
      st.renderer.setPixelRatio(Math.min(opts.pixelCap || 2, window.devicePixelRatio || 1));   // инлайн — 1.5, модалка — 2
      st.renderer.setSize(W, H, false);
      wrap.appendChild(st.canvas);

      st.scene = new THREE.Scene();
      st.camera = new THREE.PerspectiveCamera(45, W / H, 0.01, 100);
      // Освещение: hemisphere (мягкая заливка) + тёплый key-light. Материалы деталей создаём при
      // разборе GLB (по одному на part), цвет накатывает applyColors — реалтайм-перекраска из пикера.
      st.scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2f36, 0.95));
      var key = new THREE.DirectionalLight(0xfff0e0, 0.85); key.position.set(1, 1.4, 1); st.scene.add(key);
      var fill = new THREE.DirectionalLight(0xbcd0ff, 0.25); fill.position.set(-1, -0.4, -0.8); st.scene.add(fill);

      st.controls = new THREE.OrbitControls(st.camera, st.renderer.domElement);
      st.controls.enableDamping = true; st.controls.dampingFactor = 0.08;
      st.controls.autoRotate = opts.autoRotate !== false; st.controls.autoRotateSpeed = opts.autoRotateSpeed || 1.0;
      st.controls.minDistance = 0.05; st.controls.maxDistance = 60;   // временно; frame() перекрывает под габариты
      // первый жест по канвасу останавливает idle-автовращение
      st.onDown = function () { if (st.controls) st.controls.autoRotate = false; if (opts.onUserInput) opts.onUserInput(); };
      st.renderer.domElement.addEventListener('pointerdown', st.onDown);

      loadMesh(THREE, opts.fmt, opts.url).then(function (obj) {
        if (st.disposed) return;
        st.scene.add(obj);
        // .stl/.3mf приходят из печатного мира (Z-up) — доворачиваем к Y-up three.js, чтобы
        // авто-вращение крутило модель «стоя», а не лёжа на боку.
        if (opts.fmt === '.stl' || opts.fmt === '.3mf') obj.rotation.x = -Math.PI / 2;
        frame(THREE, obj);
        st.ready = true;
        done();
        startLoop();
      }).catch(function (e) { fail(reasonFor(opts.fmt, e)); });

      // авто-кадрирование: центрируем объект и отводим камеру по габаритам bounding box
      function frame(TH, obj) {
        st.scene.updateMatrixWorld(true);
        var box = new TH.Box3().setFromObject(obj);
        if (box.isEmpty()) { throw new Error('empty'); }
        var size = box.getSize(new TH.Vector3());
        var center = box.getCenter(new TH.Vector3());
        obj.position.x -= center.x; obj.position.y -= center.y; obj.position.z -= center.z;
        var maxDim = Math.max(size.x, size.y, size.z) || 1;
        var dist = (maxDim / 2) / Math.tan((st.camera.fov * Math.PI / 180) / 2) * 1.35;  // модель крупнее в кадре
        st.camera.near = maxDim / 200; st.camera.far = maxDim * 200; st.camera.updateProjectionMatrix();
        // КЛЮЧЕВОЙ ФИКС: модели в МИЛЛИМЕТРАХ (десятки-сотни единиц). Прежние жёсткие 0.05/60 были в
        // МИРОВЫХ единицах — для 88-мм модели камере нужно отойти на ~232, а OrbitControls зажимал
        // на 60 при первом update() → модель улетала за кадр (клиент видел серый фрагмент/пусто).
        // Пределы теперь ОТНОСИТЕЛЬНО габарита — деталь любого масштаба кадрируется корректно.
        st.controls.minDistance = maxDim * 0.1; st.controls.maxDistance = maxDim * 8;
        st.camera.position.set(dist * 0.75, dist * 0.55, dist);
        st.controls.target.set(0, 0, 0); st.controls.update();
        st.start.pos = st.camera.position.clone(); st.start.target = st.controls.target.clone();
      }

      st.onResize = function () {
        if (st.disposed || !wrap || !st.renderer || !st.camera) return;
        var w = Math.max(1, wrap.clientWidth), hh = Math.max(1, wrap.clientHeight);
        st.camera.aspect = w / hh; st.camera.updateProjectionMatrix();
        st.renderer.setSize(w, hh, false);
      };
      window.addEventListener('resize', st.onResize);

      // Пауза RAF, когда стейдж уходит за экран (инлайн): держим лишь ОДИН активный WebGL-контекст
      // и не жжём батарею, пока клиент читает цену. Модалка всегда на виду — ей IO не нужен.
      if (opts.pauseWhenHidden && window.IntersectionObserver) {
        st.io = new IntersectionObserver(function (ents) {
          var vis = ents[0] && ents[0].isIntersecting;
          if (vis) startLoop(); else stopLoop();
        }, { threshold: 0.01 });
        st.io.observe(wrap);
      }
    }).catch(function () { fail('Не удалось загрузить 3D-просмотрщик'); });

    // Загрузка меша по формату. Пустой url (MOCK) → демо-узел, чтобы вьюер верстался без сети.
    // onProgress (3-й арг loader.load) кормит определённый прогресс-бар на медленной сотовой —
    // прямое лекарство от «нет прогресса → тихий таймаут».
    function loadMesh(THREE, fmt, url) {
      return new Promise(function (resolve, reject) {
        if (!url) {   // MOCK / статик-превью: демо-меш + свой материал, чтобы реалтайм-цвет работал и тут
          var g = new THREE.TorusKnotGeometry(0.6, 0.2, 160, 28);
          var mat = makePartMat(THREE);
          st.geoms.push(g); st.partMats.push(mat); st.partSlots.push(-1);
          var mesh = new THREE.Mesh(g, mat); st.parts.push(mesh);
          applyColors(st.pendingColors, st.pendingMulti);
          resolve(mesh); return;
        }
        // Сервер отдаёт ЛЮБОЙ формат уже бинарным GLB со сценой из деталей (webapp._model3d_glb_bytes) —
        // поэтому здесь всегда GLTFLoader, надёжно и без капризов 3MFLoader на Bambu/MakerWorld.
        var loader;
        try { loader = new THREE.GLTFLoader(); } catch (e) { reject(e); return; }
        var to = setTimeout(function () { reject(new Error('timeout')); }, 30000);   // холодная конвертация 3mf на сервере не мгновенна
        loader.load(url, function (gltf) {
          clearTimeout(to);
          try { resolve(normalizeGltf(THREE, gltf)); } catch (e) { reject(e); }
        }, function (ev) {
          if (opts.onProgress && ev) opts.onProgress(ev.loaded || 0, ev.lengthComputable ? ev.total : 0);
        }, function () { clearTimeout(to); reject(new Error('load')); });
      });
    }
    // GLB → gltf.scene: собираем меши в СТАБИЛЬНОМ порядке (part0..N), у КАЖДОГО подменяем материал
    // на свой MeshStandardMaterial (геометрию оставляем) — так part[i] красится независимо. Копим
    // материалы/геометрии для полного dispose. applyColors накатывает текущий цвет клиента сразу.
    // Из имени узла берём СЛОТ цвета детали: сервер пишет part{i}_e{экструдер}_s{слот}
    // (webapp._model3d_glb_bytes), слот согласован с порядком model.mc_colors. Суффикса нет
    // (деталь без экструдера, STL/OBJ) → -1, applyColors красит её по индексу, как раньше.
    function partSlot(o) {
      var m = /_s(\d+)$/.exec(String(o.name || ''));
      if (!m && o.parent) m = /_s(\d+)$/.exec(String(o.parent.name || ''));   // мультипримитив: имя на группе
      return m ? parseInt(m[1], 10) : -1;
    }
    function normalizeGltf(THREE, gltf) {
      var root = gltf && (gltf.scene || (gltf.scenes && gltf.scenes[0]));
      if (!root) throw new Error('no scene');
      var any = false;
      root.traverse(function (o) {
        if (o.isMesh && o.geometry) {
          any = true;
          // trimesh экспортирует GLB БЕЗ нормалей (только POSITION) → под MeshStandardMaterial меш
          // рисуется тёмным/плоским. Считаем нормали на клиенте, как раньше делал STL-путь.
          if (!o.geometry.getAttribute || !o.geometry.getAttribute('normal')) {
            try { o.geometry.computeVertexNormals(); } catch (e) {}
          }
          if (o.material && o.material.dispose) { try { o.material.dispose(); } catch (e) {} }
          var mat = makePartMat(THREE);
          o.material = mat;
          st.partMats.push(mat); st.partSlots.push(partSlot(o)); st.parts.push(o); st.geoms.push(o.geometry);
        }
      });
      if (!any) throw new Error('no mesh');
      applyColors(st.pendingColors, st.pendingMulti);
      return root;
    }

    function dispose() {
      st.disposed = true;
      stopLoop();
      if (st.io) { try { st.io.disconnect(); } catch (e) {} st.io = null; }
      if (st.onResize) window.removeEventListener('resize', st.onResize);
      if (st.onDown && st.renderer) { try { st.renderer.domElement.removeEventListener('pointerdown', st.onDown); } catch (e) {} }
      if (st.controls && st.controls.dispose) { try { st.controls.dispose(); } catch (e) {} }
      st.geoms.forEach(function (g) { try { g.dispose(); } catch (e) {} });
      st.geoms = [];
      st.partMats.forEach(function (m) { try { m.dispose(); } catch (e) {} });   // по материалу на деталь
      st.partMats = []; st.partSlots = []; st.parts = [];
      if (st.renderer) {
        try { st.renderer.dispose(); } catch (e) {}
        try { st.renderer.forceContextLoss(); } catch (e) {}   // отпускаем WebGL-контекст явно
        if (st.renderer.domElement && st.renderer.domElement.parentNode) {
          try { st.renderer.domElement.parentNode.removeChild(st.renderer.domElement); } catch (e) {}
        }
      }
      st.renderer = st.scene = st.camera = st.controls = null;
    }

    return { promise: promise, dispose: dispose, api: apiObj };
  }

  // ─────────────── Кнопка-аффорданс «3D» (оверлей на превью) ───────────────
  // Визуальный язык .dimtag (app.css), но кликабельная (button). Угол bottom-right свободен от
  // .dimtag (top-left) и сердечка. Сама решает, показываться ли — иначе возвращает null.
  function View3DButton(props) {
    if (!canShow3D(props.model)) return null;
    return h('button', { type: 'button', className: 'view3d-btn',
      'aria-label': 'Посмотреть модель в 3D',
      onClick: function (e) { e.stopPropagation(); e.preventDefault(); props.onOpen && props.onOpen(); } },
      icon('box', { size: 14 }), h('span', null, '3D'));
  }

  // ─────────────── Инлайн-состояние отказа во вьюере (модалка) ───────────────
  function Viewer3DFallback(props) {
    return h('div', { className: 'v3d-fallback' },
      h('div', { className: 'card' },
        h('div', { style: { fontWeight: 700, fontSize: 15 } }, 'Не удалось показать 3D'),
        h('div', { style: { color: 'var(--muted)', fontSize: 13, marginTop: 6 } }, props.reason || ''),
        h('button', { type: 'button', className: 'btn line', style: { marginTop: 14 },
          onClick: props.onPng }, 'Показать картинку')));
  }

  // Маленький индикатор загрузки: PolyLoader + подпись, + тонкий определённый бар, если известен
  // размер (медленная сотовая — клиент видит жизнь, а не тёмный прямоугольник).
  function LoadWait(props) {
    return h('div', { className: 'v3d-wait' },
      h(window.PS_PolyLoader ? window.PS_PolyLoader : 'div', null),
      h('span', null, props.label || 'Загружаю 3D…'),
      props.prog > 0 && props.prog < 1 && h('div', { className: 'v3d-prog' },
        h('div', { className: 'v3d-prog-fill', style: { width: Math.round(props.prog * 100) + '%' } })));
  }

  // ─────────────── Полноэкранный вьюер (модалка) ───────────────
  // Оверлей-контракт скопирован с PS_Lightbox: zIndex 90, тёмный фон, интеграция с window.PS_Back
  // (аппаратная кнопка/жест «назад» и тап по фону закрывают через один путь). Движок — mountViewer.
  function Viewer3DModal(props) {
    var ps = useState('loading'); var phase = ps[0], setPhase = ps[1];   // loading | ready | error
    var rs = useState(''); var reason = rs[0], setReason = rs[1];
    var ws = useState(false); var wire = ws[0], setWire = ws[1];
    var as = useState(true); var spin = as[0], setSpin = as[1];
    var pgs = useState(0); var prog = pgs[0], setProg = pgs[1];
    var wrapRef = useRef(null);
    var handleRef = useRef(null);   // { api, dispose } из mountViewer

    function closeNow() { if (window.PS_Back) window.PS_Back.back(); else if (props.onClose) props.onClose(); }
    function toPng() {
      closeNow();
      if (props.onFallbackToPng) props.onFallbackToPng();   // Quote/Config откроют PNG в PS_Lightbox
    }

    // Слой «назад» (как в Lightbox): push при открытии, remove при размонтировании.
    useEffect(function () {
      if (!window.PS_Back) return undefined;
      var layer = window.PS_Back.push(function () { if (props.onClose) props.onClose(); });
      return function () { window.PS_Back.remove(layer); };
    }, []);

    // Монтируем движок в стейдж; при размонтировании — полный dispose.
    useEffect(function () {
      var wrap = wrapRef.current;
      if (!wrap) return undefined;
      var cancelled = false;
      var handle = mountViewer(wrap, {
        fmt: props.fmt, url: props.url, autoRotate: true, autoRotateSpeed: 1.0, pixelCap: 2,
        colors: props.colors, multicolor: props.multicolor,
        onUserInput: function () { setSpin(false); },
        onProgress: function (l, t) { if (t) setProg(l / t); },
      });
      handleRef.current = handle;
      handle.promise.then(function () { if (!cancelled) setPhase('ready'); })
        .catch(function (e) { if (!cancelled) { setReason((e && e.message) || 'Не удалось показать 3D'); setPhase('error'); } });
      return function () { cancelled = true; try { handle.dispose(); } catch (e) {} handleRef.current = null; };
    }, []);

    // Реалтайм-перекраска: смена цвета в пикере → setColors без перезагрузки меша. Ключ по строке
    // цветов (React сравнивает по ссылке — массив каждый рендер новый), поэтому сериализуем.
    var mColorKey = (props.colors || []).join(',') + '|' + (props.multicolor ? 1 : 0);
    useEffect(function () {
      var hp = handleRef.current;
      if (hp && hp.api && hp.api.setColors) hp.api.setColors(props.colors, props.multicolor);
    }, [mColorKey]);   // eslint-disable-line

    function callApi(fn) { var hp = handleRef.current; if (hp && hp.api && hp.api[fn]) return hp.api[fn]; return function () {}; }

    var dims = props.dims || '';
    return h('div', { className: 'v3d-overlay',
      onClick: function (e) { if (e.target === e.currentTarget) closeNow(); } },
      phase === 'error'
        ? h(Viewer3DFallback, { reason: reason, onPng: toPng })
        : h(React.Fragment, null,
            h('div', { className: 'v3d-stage', ref: wrapRef },
              dims && h('span', { className: 'v3d-dim' }, String(dims).replace(/\s*мм\s*$/i, ''), ' мм'),
              phase === 'loading' && h(LoadWait, { label: 'Загружаю 3D-модель…', prog: prog })),
            h('div', { className: 'v3d-bar' },
              h('button', { type: 'button', className: 'btn soft',
                onClick: function (e) { e.stopPropagation(); callApi('reset')(); } }, 'Сброс вида'),
              h('button', { type: 'button', className: 'btn soft' + (wire ? ' on' : ''),
                onClick: function (e) { e.stopPropagation(); var n = !wire; setWire(n); callApi('wire')(n); } }, 'Каркас'),
              h('button', { type: 'button', className: 'btn soft' + (spin ? ' on' : ''),
                onClick: function (e) { e.stopPropagation(); var n = !spin; setSpin(n); callApi('spin')(n); } }, '⟳ Вращение')),
            h('div', { className: 'v3d-hint' }, 'Проведите пальцем — повернуть · пинч — приблизить')));
  }

  // ─────────────── Инлайн-3D: первичный превью на Config/Quote ───────────────
  // Живёт в потоке страницы, в той же footprint, что PNG (нулевой сдвиг вёрстки). Фазы:
  //   idle    — armed, но не грузим (сотовая/тяжёлый файл): виден PNG + точка «Смотреть в 3D»
  //   loading — грузим меш; PNG всё ещё под канвасом, модель проявляется поверх своей же миниатюры
  //   ready   — интерактивный меш; правый-низ — «развернуть» (полноэкранная модалка)
  //   error   — тихий откат на PNG под канвасом + чип «Картинка/повторить» (3D никогда не блокирует)
  function Inline3D(props) {
    var m = props.model || {};
    var ps = useState(shouldAutoload(m) ? 'loading' : 'idle'); var phase = ps[0], setPhase = ps[1];
    var pgs = useState(0); var prog = pgs[0], setProg = pgs[1];
    var wrapRef = useRef(null);
    var handleRef = useRef(null);

    // «Активные» фазы держат канвас смонтированным. loading→ready НЕ перемонтирует движок:
    // active не меняется, эффект не перезапускается. ready/idle→error → active=false → dispose.
    var active = phase === 'loading' || phase === 'ready';
    useEffect(function () {
      if (!active) return undefined;
      var wrap = wrapRef.current;
      if (!wrap) return undefined;
      var cancelled = false;
      setProg(0);
      var handle = mountViewer(wrap, {
        fmt: props.fmt, url: props.url, autoRotate: true, autoRotateSpeed: 0.55, pixelCap: 1.5,
        pauseWhenHidden: true, colors: props.colors, multicolor: props.multicolor,
        onProgress: function (l, t) { if (t) setProg(l / t); },
      });
      handleRef.current = handle;
      handle.promise.then(function () { if (!cancelled) setPhase('ready'); })
        .catch(function () { if (!cancelled) { setPhase('error'); if (props.onFallback) props.onFallback(); } });
      return function () { cancelled = true; try { handle.dispose(); } catch (e) {} handleRef.current = null; };
    }, [active]);   // eslint-disable-line — намеренно: перемонтируем ТОЛЬКО при входе/выходе из активной фазы

    // Реалтайм-перекраска из пикера цвета под 3D: setColors без перезагрузки (мгновенно). Ключ по
    // строке цветов — массив каждый рендер новый, иначе effect не сработал бы на смену содержимого.
    var colorKey = (props.colors || []).join(',') + '|' + (props.multicolor ? 1 : 0);
    useEffect(function () {
      var hp = handleRef.current;
      if (hp && hp.api && hp.api.setColors) hp.api.setColors(props.colors, props.multicolor);
    }, [colorKey]);   // eslint-disable-line — реагируем на смену цвета, не на перемонтирование движка

    var dims = props.dims || m.dims || '';
    // PNG всегда лежит ПОД канвасом → в любой фазе нет пустого/тёмного бокса, а сбой 3D = ровно
    // сегодняшнее PNG-превью без сдвига вёрстки.
    return h('div', { className: 'v3d-inline' + (phase === 'ready' ? ' is-ready' : '') },
      props.png && h('img', { className: 'v3d-inline-png', src: props.png, alt: '', draggable: false }),
      h('div', { className: 'v3d-inline-stage', ref: wrapRef }),
      dims && h('span', { className: 'v3d-dim' }, String(dims).replace(/\s*мм\s*$/i, ''), ' мм'),
      phase === 'loading' && h(LoadWait, { label: 'Загружаю 3D…', prog: prog }),
      // idle: PNG первичен, одна точка апгрейда в 3D
      phase === 'idle' && h('button', { type: 'button', className: 'v3d-idle-cta',
        onClick: function (e) { e.stopPropagation(); setPhase('loading'); } },
        icon('box', { size: 15 }), h('span', null, 'Смотреть в 3D')),
      // ready: развернуть в полноэкранную модалку
      phase === 'ready' && props.onExpand && h('button', { type: 'button', className: 'view3d-btn',
        'aria-label': 'Развернуть 3D', onClick: function (e) { e.stopPropagation(); props.onExpand(); } },
        icon('box', { size: 14 }), h('span', null, 'Развернуть')),
      // error: тихо откатились на PNG, даём чип «повторить 3D»
      phase === 'error' && h('button', { type: 'button', className: 'view3d-btn',
        'aria-label': 'Повторить 3D', onClick: function (e) { e.stopPropagation(); setProg(0); setPhase('loading'); } },
        icon('box', { size: 14 }), h('span', null, 'Картинка · 3D')));
  }

  Object.assign(window, { PS_View3DButton: View3DButton, PS_Viewer3D: Viewer3DModal,
    PS_Inline3D: Inline3D, ps3dCanShow: canShow3D, ps3dFmt: fmtOf, ps3dShouldAutoload: shouldAutoload });
})();
