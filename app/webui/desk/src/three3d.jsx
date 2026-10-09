// three3d.jsx — 3D-просмотр модели заказа в карточке мастера (B1 для ПК-админки).
//
// Мастеру нужно покрутить ЗАКАЗАННУЮ деталь прямо в карточке заказа. Движок — тот же, что у
// клиентского вьюера (app/webui/screens_3d.js): three.js r147, сцена + GLTFLoader + OrbitControls.
// Но десктоп собирается ОТДЕЛЬНЫМ esbuild-бандлом и не тащит IIFE клиента, поэтому здесь —
// компактная самодостаточная копия логики сцены. Сервер отдаёт ЛЮБОЙ формат уже бинарным GLB
// со СЦЕНОЙ из деталей (webapp._model3d_glb_bytes, в т.ч. Bambu/MakerWorld .3mf) — имена узлов
// несут СЛОТ цвета (part{i}_e{ext}_s{slot}), и вьюер красит каждую деталь цветом заказа
// (OpenTodo a3dff103: раньше деск получал слитый серый STL — цвета не подтягивались вообще).
//
// three.js НЕ бандлим в desk.js: r147 UMD-файлы уже завендорены (app/webui/vendor/*.js) и
// раздаются по /app/vendor/…. Грузим их ЛЕНИВО инъекцией <script> — окно админки открывается
// без ~1 МБ three.js, пока мастер не нажал «3D».

const { useEffect, useRef, useState } = React;

// Абсолютные пути: десктоп живёт под /app/desk/, а вендор — под /app/vendor/. Относительный
// 'vendor/…' резолвился бы в /app/desk/vendor/… и давал 404.
const VENDOR = '/app/vendor/';
const THREE_SCRIPTS = ['three.min.js', 'three-OrbitControls.js', 'three-GLTFLoader.js'];

// Кэш-версия скриптов — тот же ключ, что у остального фронта, если оболочка его выставила.
const VER = (typeof window !== 'undefined' && window.__APPVER__ && String(window.__APPVER__).indexOf('%') < 0)
  ? window.__APPVER__ : '';
function vurl(name) {
  const p = VENDOR + name;
  return VER ? (p + '?v=' + encodeURIComponent(VER)) : p;
}

let _threePromise = null;
function loadScript(name) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-desk3d="' + name + '"]');
    if (existing) {
      if (existing.getAttribute('data-loaded')) return resolve();
      existing.addEventListener('load', () => resolve());
      existing.addEventListener('error', () => reject(new Error('script ' + name)));
      return;
    }
    const el = document.createElement('script');
    el.src = vurl(name); el.async = false; el.setAttribute('data-desk3d', name);
    el.addEventListener('load', () => { el.setAttribute('data-loaded', '1'); resolve(); });
    el.addEventListener('error', () => reject(new Error('script ' + name)));
    document.head.appendChild(el);
  });
}
// Скрипты грузим СТРОГО последовательно: лоадеры дописывают классы в уже загруженный глобал THREE.
// Экспорт — для вьюера траекторий g-code (gcode3d.jsx): тот же THREE, тот же кэш загрузки.
export function ensureThree() {
  if (window.THREE && window.THREE.OrbitControls && window.THREE.GLTFLoader) return Promise.resolve();
  if (_threePromise) return _threePromise;
  _threePromise = THREE_SCRIPTS.reduce((chain, name) => chain.then(() => loadScript(name)), Promise.resolve())
    .then(() => { if (!(window.THREE && window.THREE.GLTFLoader)) throw new Error('THREE not ready'); })
    .catch((e) => { _threePromise = null; throw e; });   // дать шанс повторить при новом открытии
  return _threePromise;
}

export function webglOK() {
  try {
    const c = document.createElement('canvas');
    return !!(window.WebGLRenderingContext
      && (c.getContext('webgl2') || c.getContext('webgl') || c.getContext('experimental-webgl')));
  } catch (e) { return false; }
}

const NEUTRAL = 0x9aa3ad;   // цвет заказа не разобрался → нейтральный серый, как раньше

// Переливающиеся цвета приходят из палитры строкой linear-gradient(...,#a,#b) — THREE.Color
// такую не ест. Достаём hex-стопы; деск красит ПЕРВЫМ стопом (перелив-шейдер клиента тут не
// тащим — мастеру важен сам цвет, а не игра шёлка).
function cssStops(css) {
  const s = String(css || '');
  if (s.indexOf('gradient') < 0) return s ? [s] : [];
  return s.match(/#[0-9a-fA-F]{3,8}/g) || [];
}

// Слот цвета из имени узла GLB: сервер пишет part{i}_e{ext}_s{slot} (см. _model3d_glb_bytes);
// суффикса нет → -1, деталь красится по порядковому индексу.
function partSlot(o) {
  let m = /_s(\d+)$/.exec(String(o.name || ''));
  if (!m && o.parent) m = /_s(\d+)$/.exec(String(o.parent.name || ''));
  return m ? parseInt(m[1], 10) : -1;
}

// ─────────────── Движок (без React): сцена + загрузка GLB + RAF + dispose ───────────────
// Возвращает { promise, dispose, api }. promise резолвится готовым, реджектится с причиной.
function mountViewer(wrap, opts) {
  opts = opts || {};
  const st = {
    disposed: false, raf: 0, running: false, ready: false,
    renderer: null, scene: null, camera: null, controls: null, canvas: null,
    partMats: [], partSlots: [], geoms: [], start: { pos: null, target: null }, onResize: null, onDown: null,
  };
  let settled = false, _res = null, _rej = null;
  const promise = new Promise((a, b) => { _res = a; _rej = b; });
  function done() { if (!settled && !st.disposed) { settled = true; _res(); } }
  function fail(reason) { if (!settled && !st.disposed) { settled = true; _rej(new Error(reason || 'error')); } }

  // Покраска деталей цветами заказа: одноцвет — все детали colors[0]; мультицвет — деталь по
  // её СЛОТУ из имени узла (согласован с порядком цветов заказа), без слота — по индексу.
  function applyColors() {
    const colors = opts.colors && opts.colors.length ? opts.colors : null;
    st.partMats.forEach((mat, i) => {
      let css = null;
      if (colors) {
        const slot = (st.partSlots[i] >= 0) ? st.partSlots[i] : i;
        css = opts.multicolor ? colors[slot % colors.length] : colors[0];
      }
      const stops = cssStops(css);
      try { mat.color.set(stops.length ? stops[0] : NEUTRAL); } catch (e) { mat.color.set(NEUTRAL); }
    });
  }

  const apiObj = {
    reset() {
      if (!st.controls || !st.start.pos) return;
      st.camera.position.copy(st.start.pos); st.controls.target.copy(st.start.target); st.controls.update();
    },
    wire(on) { st.partMats.forEach((m) => { m.wireframe = !!on; }); },
    spin(on) { if (st.controls) st.controls.autoRotate = !!on; },
  };

  function loop() {
    if (st.disposed || !st.running) return;
    st.raf = requestAnimationFrame(loop);
    if (st.controls) st.controls.update();
    if (st.renderer && st.scene && st.camera) st.renderer.render(st.scene, st.camera);
  }
  function startLoop() { if (st.running || st.disposed || !st.ready) return; st.running = true; loop(); }
  function stopLoop() { st.running = false; if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; } }

  ensureThree().then(() => {
    if (st.disposed || !wrap) return;
    const THREE = window.THREE;
    const W = Math.max(1, wrap.clientWidth), H = Math.max(1, wrap.clientHeight);
    st.canvas = document.createElement('canvas');
    try {
      st.renderer = new THREE.WebGLRenderer({ canvas: st.canvas, antialias: true, alpha: true });
    } catch (e) { fail('В этом браузере недоступен WebGL'); return; }
    st.renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    st.renderer.setSize(W, H, false);
    wrap.appendChild(st.canvas);

    st.scene = new THREE.Scene();
    st.camera = new THREE.PerspectiveCamera(45, W / H, 0.01, 100);
    // Освещение как у клиента: мягкая hemisphere-заливка + тёплый key + холодный fill.
    st.scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2f36, 0.95));
    const key = new THREE.DirectionalLight(0xfff0e0, 0.85); key.position.set(1, 1.4, 1); st.scene.add(key);
    const fill = new THREE.DirectionalLight(0xbcd0ff, 0.25); fill.position.set(-1, -0.4, -0.8); st.scene.add(fill);

    st.controls = new THREE.OrbitControls(st.camera, st.renderer.domElement);
    st.controls.enableDamping = true; st.controls.dampingFactor = 0.08;
    st.controls.autoRotate = opts.autoRotate !== false; st.controls.autoRotateSpeed = 0.9;
    st.controls.minDistance = 0.05; st.controls.maxDistance = 60;   // временно; frame() перекроет под габариты
    st.onDown = () => { if (st.controls) st.controls.autoRotate = false; if (opts.onUserInput) opts.onUserInput(); };
    st.renderer.domElement.addEventListener('pointerdown', st.onDown);

    loadMesh(THREE, opts.url).then((obj) => {
      if (st.disposed) return;
      st.scene.add(obj);
      // GLB собран из печатного мира (Z-up) — доворачиваем к Y-up three.js, чтобы модель крутилась «стоя».
      obj.rotation.x = -Math.PI / 2;
      frame(THREE, obj);
      st.ready = true;
      done();
      startLoop();
    }).catch((e) => fail((e && e.message === 'timeout') ? 'Загрузка 3D заняла слишком долго' : 'Не удалось загрузить 3D-модель'));

    // Авто-кадрирование: центрируем объект и отводим камеру по габаритам bounding box.
    function frame(TH, obj) {
      st.scene.updateMatrixWorld(true);
      const box = new TH.Box3().setFromObject(obj);
      if (box.isEmpty()) throw new Error('empty');
      const size = box.getSize(new TH.Vector3());
      const center = box.getCenter(new TH.Vector3());
      obj.position.x -= center.x; obj.position.y -= center.y; obj.position.z -= center.z;
      const maxDim = Math.max(size.x, size.y, size.z) || 1;
      const dist = (maxDim / 2) / Math.tan((st.camera.fov * Math.PI / 180) / 2) * 1.6;
      st.camera.near = maxDim / 200; st.camera.far = maxDim * 200; st.camera.updateProjectionMatrix();
      // КРИТИЧНО (тот же баг, что ломал клиентский вьюер): модели в МИЛЛИМЕТРАХ (десятки-сотни
      // единиц). Жёсткие 0.05/60 были в МИРОВЫХ единицах — для 88-мм детали камере надо отойти на
      // ~232, а OrbitControls зажимал на 60 → модель улетала за кадр. Пределы — ОТНОСИТЕЛЬНО габарита.
      st.controls.minDistance = maxDim * 0.1; st.controls.maxDistance = maxDim * 8;
      st.camera.position.set(dist * 0.75, dist * 0.55, dist);
      st.controls.target.set(0, 0, 0); st.controls.update();
      st.start.pos = st.camera.position.clone(); st.start.target = st.controls.target.clone();
    }

    st.onResize = () => {
      if (st.disposed || !wrap || !st.renderer || !st.camera) return;
      const w = Math.max(1, wrap.clientWidth), hh = Math.max(1, wrap.clientHeight);
      st.camera.aspect = w / hh; st.camera.updateProjectionMatrix();
      st.renderer.setSize(w, hh, false);
    };
    window.addEventListener('resize', st.onResize);
  }).catch(() => fail('Не удалось загрузить 3D-просмотрщик'));

  // Загрузка меша: сервер отдаёт бинарный GLB со сценой из деталей — только GLTFLoader.
  // У каждой детали СВОЙ материал (part[i] красится независимо), слот цвета — из имени узла.
  function loadMesh(THREE, url) {
    return new Promise((resolve, reject) => {
      let loader;
      try { loader = new THREE.GLTFLoader(); } catch (e) { reject(e); return; }
      const to = setTimeout(() => reject(new Error('timeout')), 30000);   // холодная конвертация .3mf на сервере не мгновенна
      loader.load(url, (gltf) => {
        clearTimeout(to);
        try {
          const root = gltf && (gltf.scene || (gltf.scenes && gltf.scenes[0]));
          if (!root) throw new Error('no scene');
          let any = false;
          root.traverse((o) => {
            if (o.isMesh && o.geometry) {
              any = true;
              // trimesh экспортирует GLB без нормалей (только POSITION) → под
              // MeshStandardMaterial меш рисуется тёмным. Считаем нормали на клиенте.
              if (!o.geometry.getAttribute || !o.geometry.getAttribute('normal')) {
                try { o.geometry.computeVertexNormals(); } catch (e) {}
              }
              if (o.material && o.material.dispose) { try { o.material.dispose(); } catch (e) {} }
              const mat = new THREE.MeshStandardMaterial({ color: NEUTRAL, roughness: 0.72, metalness: 0.04 });
              o.material = mat;
              st.partMats.push(mat); st.partSlots.push(partSlot(o)); st.geoms.push(o.geometry);
            }
          });
          if (!any) throw new Error('no mesh');
          applyColors();   // цвета заказа известны при открытии — красим сразу
          resolve(root);
        } catch (e) { reject(e); }
      }, null, () => { clearTimeout(to); reject(new Error('load')); });
    });
  }

  function dispose() {
    st.disposed = true;
    stopLoop();
    if (st.onResize) window.removeEventListener('resize', st.onResize);
    if (st.onDown && st.renderer) { try { st.renderer.domElement.removeEventListener('pointerdown', st.onDown); } catch (e) {} }
    if (st.controls && st.controls.dispose) { try { st.controls.dispose(); } catch (e) {} }
    st.geoms.forEach((g) => { try { g.dispose(); } catch (e) {} });
    st.geoms = [];
    st.partMats.forEach((m) => { try { m.dispose(); } catch (e) {} });   // по материалу на деталь
    st.partMats = []; st.partSlots = [];
    if (st.renderer) {
      try { st.renderer.dispose(); } catch (e) {}
      try { st.renderer.forceContextLoss(); } catch (e) {}   // отпускаем WebGL-контекст явно
      if (st.renderer.domElement && st.renderer.domElement.parentNode) {
        try { st.renderer.domElement.parentNode.removeChild(st.renderer.domElement); } catch (e) {}
      }
    }
    st.renderer = st.scene = st.camera = st.controls = null;
  }

  return { promise, dispose, api: apiObj };
}

// ─────────────── Полноэкранный вьюер (React-оверлей) ───────────────
// Клик по фону / крестик / Esc — закрывают. Любой сбой → onFallback (карточка остаётся на PNG).
// colors — css-цвета заказа по слотам (order.colors_css), multicolor — режим раскраски.
export function OrderViewer3D({ url, dims, colors, multicolor, onClose, onFallback }) {
  const [phase, setPhase] = useState('loading');   // loading | ready | error
  const [reason, setReason] = useState('');
  const [wire, setWire] = useState(false);
  const [spin, setSpin] = useState(true);
  const wrapRef = useRef(null);
  const handleRef = useRef(null);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return undefined;
    let cancelled = false;
    const handle = mountViewer(wrap, {
      url, colors, multicolor, autoRotate: true, onUserInput: () => setSpin(false),
    });
    handleRef.current = handle;
    handle.promise.then(() => { if (!cancelled) setPhase('ready'); })
      .catch((e) => { if (!cancelled) { setReason((e && e.message) || 'Не удалось показать 3D'); setPhase('error'); } });
    return () => { cancelled = true; try { handle.dispose(); } catch (e) {} handleRef.current = null; };
  }, [url]);

  // Esc закрывает — привычно для десктопа.
  useEffect(() => {
    const h = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  function callApi(fn, arg) { const hp = handleRef.current; if (hp && hp.api && hp.api[fn]) hp.api[fn](arg); }

  return <div className="v3d-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="v3d-box" onClick={(e) => e.stopPropagation()}>
      {phase === 'error'
        ? <div className="v3d-fallback">
            <div style={{ fontWeight: 700, fontSize: 15 }}>Не удалось показать 3D</div>
            <div style={{ color: 'var(--muted)', fontSize: 13, marginTop: 6 }}>{reason}</div>
            <button className="db line" style={{ marginTop: 14 }}
              onClick={() => { if (onFallback) onFallback(); onClose(); }}>Показать картинку</button>
          </div>
        : <>
            <div className="v3d-stage" ref={wrapRef}>
              {dims ? <span className="v3d-dim">{dims} мм</span> : null}
              {phase === 'loading' ? <div className="v3d-wait">Загружаю 3D-модель…</div> : null}
            </div>
            <div className="v3d-bar">
              <button className="db soft sm" onClick={() => callApi('reset')}>Сброс вида</button>
              <button className={'db soft sm' + (wire ? ' on' : '')}
                onClick={() => { const n = !wire; setWire(n); callApi('wire', n); }}>Каркас</button>
              <button className={'db soft sm' + (spin ? ' on' : '')}
                onClick={() => { const n = !spin; setSpin(n); callApi('spin', n); }}>⟳ Вращение</button>
              <span style={{ flex: 1 }} />
              <button className="ib" title="Закрыть" onClick={onClose}>✕</button>
            </div>
          </>}
    </div>
  </div>;
}

// Показывать ли аффорданс «3D»: у заказа должен быть файл модели (can_open_slicer = есть file_path)
// И браузер должен уметь WebGL. Формат/размер добьёт сервер — при 404 карточка тихо падает на PNG.
export function canShow3D(o) {
  return !!(o && o.can_open_slicer && webglOK());
}
