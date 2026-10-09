// gcode3d.jsx — 3D-вьюер траекторий g-code для «Допечатки со слоя» (план W2, 05.09.2026).
//
// Сервер отдаёт бинарные отрезки (app/resumecut.py::toolpaths, ручка /api/admin/tools/resume/paths):
// 'PSTP', u16 версия, u32 длина JSON-шапки, JSON (стол, слои с Z, детали, типы), затем N записей
// по 24 байта: f32 x0,y0,x1,y1,z, u16 слой, u8 деталь, u8 тип. Отрезки идут слоями по возрастанию,
// поэтому «показать до слоя L» — это drawRange по префиксу массива, без пересборки геометрии.
//
// Что видно мастеру: стол с сеткой и передней кромкой, детали цветом (вырезанные — красным,
// напечатанные ниже слоя N — серым), слайдер слоёв, режимы «всё / напечатано / допечатается»,
// заполнение можно приглушить, клик по детали — отметить сорвавшейся (raycast по параллелепипеду
// детали, а не по миллиону отрезков), стартовые переезды — оранжевой линией.
//
// WebGL, не WebGPU: краши GUI на WebGPU уже ловили. Без WebGL — `fallback` (плоская карта).

import { ensureThree, webglOK } from './three3d.jsx';

const { useState, useEffect, useRef, useMemo, useCallback } = React;

const PALETTE = [0x3b82f6, 0x10b981, 0xf59e0b, 0x8b5cf6, 0x06b6d4, 0x84cc16, 0xec4899, 0x0ea5e9,
  0xf97316, 0x14b8a6, 0xa855f7, 0x22c55e];
const RED = 0xdc2626, GREY = 0x9a9a9a, GREY_DARK = 0x6b6b6b, ORANGE = 0xff7a1a;
const INFILL_TYPES = { 3: 1, 4: 1 };

/** Разобрать бинарный ответ сервера в типизированные массивы. */
export function parsePaths(buf) {
  const dv = new DataView(buf);
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
  if (magic !== 'PSTP') throw new Error('не траектории');
  const hlen = dv.getUint32(6, true);
  const header = JSON.parse(new TextDecoder('utf-8').decode(new Uint8Array(buf, 10, hlen)));
  const n = header.count | 0;
  const base = 10 + hlen;
  const pos = new Float32Array(n * 6);
  const layer = new Uint16Array(n);
  const obj = new Uint8Array(n);
  const type = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const o = base + i * 24;
    const x0 = dv.getFloat32(o, true), y0 = dv.getFloat32(o + 4, true);
    const x1 = dv.getFloat32(o + 8, true), y1 = dv.getFloat32(o + 12, true);
    const z = dv.getFloat32(o + 16, true);
    pos[i * 6] = x0; pos[i * 6 + 1] = y0; pos[i * 6 + 2] = z;
    pos[i * 6 + 3] = x1; pos[i * 6 + 4] = y1; pos[i * 6 + 5] = z;
    layer[i] = dv.getUint16(o + 20, true);
    obj[i] = dv.getUint8(o + 22);
    type[i] = dv.getUint8(o + 23);
  }
  // индекс «первый отрезок слоя L» — для drawRange по слайдеру
  const firstOf = new Int32Array((header.layers | 0) + 2).fill(-1);
  for (let i = 0; i < n; i++) if (firstOf[layer[i]] < 0) firstOf[layer[i]] = i;
  let last = n;
  for (let L = firstOf.length - 1; L >= 0; L--) { if (firstOf[L] < 0) firstOf[L] = last; else last = firstOf[L]; }
  return { header, n, pos, layer, obj, type, firstOf };
}

function hexToRgb(h) { return [((h >> 16) & 255) / 255, ((h >> 8) & 255) / 255, (h & 255) / 255]; }

/**
 * Вьюер. props:
 *   url — откуда брать траектории; layer — слой допечатки N (границa «напечатано/допечатается»);
 *   excluded — id вырезанных деталей; onToggle(id) — клик по детали; startMoves — [[x,y,z]…];
 *   mode — 'all' | 'printed' | 'remaining'; height — px; fallback — что рисовать без WebGL.
 */
export function GcodeViewer3D({ url, layer, excluded = [], onToggle, startMoves, mode = 'all',
  height = 460, fallback = null, onLoaded }) {
  const host = useRef(null);
  const st = useRef({});
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [ready, setReady] = useState(false);
  const [cur, setCur] = useState(0);           // верхний видимый слой (слайдер)
  const [dimInfill, setDimInfill] = useState(true);
  const [hover, setHover] = useState(null);
  const canGL = useMemo(() => webglOK(), []);

  // загрузка траекторий
  useEffect(() => {
    if (!url || !canGL) return undefined;
    let alive = true;
    setReady(false); setErr(''); setData(null);
    fetch(url).then((r) => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const ct = r.headers.get('content-type') || '';
      if (ct.indexOf('json') >= 0) return r.json().then((j) => { throw new Error(j.error || 'нет траекторий'); });
      return r.arrayBuffer();
    }).then((buf) => {
      if (!alive) return;
      const d = parsePaths(buf);
      setData(d);
      setCur(d.header.layers);
      if (onLoaded) onLoaded(d.header);
    }).catch((e) => { if (alive) setErr(String(e.message || e)); });
    return () => { alive = false; };
  }, [url, canGL]);   // eslint-disable-line

  // сцена
  useEffect(() => {
    if (!data || !host.current || !canGL) return undefined;
    let disposed = false;
    const s = st.current;
    ensureThree().then(() => {
      if (disposed) return;
      const THREE = window.THREE;
      const el = host.current;
      const w = el.clientWidth || 600, h = height;
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
      renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
      renderer.setSize(w, h);
      el.innerHTML = '';
      el.appendChild(renderer.domElement);
      const scene = new THREE.Scene();
      const [bx, by] = data.header.bed || [256, 256];
      const camera = new THREE.PerspectiveCamera(38, w / h, 1, 4000);
      camera.up.set(0, 0, 1);
      camera.position.set(bx / 2 + bx * 0.05, -by * 0.95, Math.max(bx, by) * 0.85);
      const controls = new THREE.OrbitControls(camera, renderer.domElement);
      controls.target.set(bx / 2, by / 2, 0);
      controls.enableDamping = true; controls.dampingFactor = 0.12;
      controls.maxDistance = Math.max(bx, by) * 4; controls.minDistance = 20;
      controls.update();
      // стол: пластина, сетка 16 мм, передняя кромка толще
      const plate = new THREE.Mesh(new THREE.PlaneGeometry(bx, by),
        new THREE.MeshBasicMaterial({ color: 0xf1f1f1, side: THREE.DoubleSide }));
      plate.position.set(bx / 2, by / 2, -0.05);
      scene.add(plate);
      const grid = new THREE.GridHelper(Math.max(bx, by), Math.round(Math.max(bx, by) / 16), 0xbdbdbd, 0xe3e3e3);
      grid.rotation.x = Math.PI / 2; grid.position.set(bx / 2, by / 2, 0);
      scene.add(grid);
      const front = new THREE.Line(new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(0, 0, 0.1), new THREE.Vector3(bx, 0, 0.1)]),
        new THREE.LineBasicMaterial({ color: 0x333333 }));
      scene.add(front);
      const centre = new THREE.Mesh(new THREE.RingGeometry(2, 3, 24),
        new THREE.MeshBasicMaterial({ color: 0x555555, side: THREE.DoubleSide }));
      centre.position.set(bx / 2, by / 2, 0.15);
      scene.add(centre);
      // траектории
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(data.pos, 3));
      const colors = new Float32Array(data.n * 6);
      geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
      const lines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ vertexColors: true }));
      lines.frustumCulled = false;
      scene.add(lines);
      // стартовые переезды
      const startGeo = new THREE.BufferGeometry();
      const startLine = new THREE.Line(startGeo, new THREE.LineBasicMaterial({ color: ORANGE, linewidth: 2 }));
      scene.add(startLine);
      // параллелепипеды деталей для клика
      const boxes = (data.header.objects || []).map((o) => {
        const b = o.bbox_all && o.bbox_all[2] > o.bbox_all[0] ? o.bbox_all : o.bbox;
        return { id: o.id, name: o.name, box: new THREE.Box3(new THREE.Vector3(b[0], b[1], 0),
          new THREE.Vector3(b[2], b[3], Math.max(0.5, o.zmax || 1))) };
      });
      const ray = new THREE.Raycaster();
      const ndc = new THREE.Vector2();
      let down = null;
      const pick = (ev) => {
        const r = renderer.domElement.getBoundingClientRect();
        ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
        ray.setFromCamera(ndc, camera);
        let best = null, bd = Infinity;
        const tmp = new THREE.Vector3();
        boxes.forEach((b) => {
          if (ray.ray.intersectBox(b.box, tmp)) {
            const d = tmp.distanceTo(camera.position);
            if (d < bd) { bd = d; best = b; }
          }
        });
        return best;
      };
      const onDown = (ev) => { down = [ev.clientX, ev.clientY]; };
      const onUp = (ev) => {
        if (!down || Math.abs(ev.clientX - down[0]) > 4 || Math.abs(ev.clientY - down[1]) > 4) { down = null; return; }
        down = null;
        const b = pick(ev);
        if (b && s.onToggle) s.onToggle(b.id);
      };
      const onMove = (ev) => { const b = pick(ev); setHover(b ? { id: b.id, name: b.name } : null); renderer.domElement.style.cursor = b ? 'pointer' : 'default'; };
      renderer.domElement.addEventListener('pointerdown', onDown);
      renderer.domElement.addEventListener('pointerup', onUp);
      renderer.domElement.addEventListener('pointermove', onMove);
      let raf = 0;
      const tick = () => { controls.update(); renderer.render(scene, camera); raf = requestAnimationFrame(tick); };
      tick();
      const ro = new ResizeObserver(() => {
        const nw = el.clientWidth || w;
        renderer.setSize(nw, height); camera.aspect = nw / height; camera.updateProjectionMatrix();
      });
      ro.observe(el);
      Object.assign(s, { renderer, scene, camera, controls, lines, geo, colors, startGeo, ro, raf, boxes,
        dispose: () => {
          cancelAnimationFrame(s.raf); ro.disconnect();
          renderer.domElement.removeEventListener('pointerdown', onDown);
          renderer.domElement.removeEventListener('pointerup', onUp);
          renderer.domElement.removeEventListener('pointermove', onMove);
          controls.dispose(); geo.dispose(); startGeo.dispose(); renderer.dispose();
          if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
        } });
      setReady(true);
    }).catch((e) => { if (!disposed) setErr('three.js: ' + String(e.message || e)); });
    return () => { disposed = true; if (s.dispose) { s.dispose(); s.dispose = null; } };
  }, [data, canGL, height]);

  st.current.onToggle = onToggle;

  // раскраска: деталь → цвет, вырезанные — красным, ниже слоя N — серым, заполнение приглушено
  const recolor = useCallback(() => {
    const s = st.current;
    if (!s.colors || !data) return;
    const ex = new Set(excluded);
    const N = layer || 0;
    const pal = PALETTE.map(hexToRgb), red = hexToRgb(RED), grey = hexToRgb(GREY), greyD = hexToRgb(GREY_DARK);
    const c = s.colors;
    for (let i = 0; i < data.n; i++) {
      const L = data.layer[i], o = data.obj[i], t = data.type[i];
      let rgb;
      if (ex.has(o)) rgb = red;
      else if (N && L < N) rgb = (dimInfill && INFILL_TYPES[t]) ? greyD : grey;
      else rgb = pal[o % pal.length];
      let k = 1;
      if (dimInfill && INFILL_TYPES[t] && !ex.has(o)) k = 0.55;
      if (hover && hover.id === o) k = Math.min(1.35, k + 0.35);
      const r = Math.min(1, rgb[0] * k), g = Math.min(1, rgb[1] * k), b = Math.min(1, rgb[2] * k);
      c[i * 6] = r; c[i * 6 + 1] = g; c[i * 6 + 2] = b; c[i * 6 + 3] = r; c[i * 6 + 4] = g; c[i * 6 + 5] = b;
    }
    s.geo.attributes.color.needsUpdate = true;
  }, [data, excluded, layer, dimInfill, hover]);

  // видимый диапазон: режим × слайдер
  const rerange = useCallback(() => {
    const s = st.current;
    if (!s.geo || !data) return;
    const N = layer || 0;
    let from = 0, to = data.n;
    if (mode === 'printed' && N) to = data.firstOf[N];
    if (mode === 'remaining' && N) from = data.firstOf[N];
    const top = Math.min(cur, data.header.layers);
    to = Math.min(to, data.firstOf[top + 1] != null ? data.firstOf[top + 1] : data.n);
    if (to < from) to = from;
    s.geo.setDrawRange(from * 2, (to - from) * 2);
  }, [data, layer, mode, cur]);

  useEffect(() => { if (ready) recolor(); }, [ready, recolor]);
  useEffect(() => { if (ready) rerange(); }, [ready, rerange]);
  useEffect(() => {
    const s = st.current;
    if (!ready || !s.startGeo || !window.THREE) return;
    const pts = (startMoves || []).map((p) => new window.THREE.Vector3(p[0], p[1], p[2]));
    s.startGeo.setFromPoints(pts.length ? pts : [new window.THREE.Vector3(0, 0, -10), new window.THREE.Vector3(0, 0, -10)]);
  }, [ready, startMoves]);

  if (!canGL) return fallback;
  const layers = data ? data.header.layers : 0;
  return <div style={{ position: 'relative', width: '100%' }}>
    <div ref={host} style={{ width: '100%', height, borderRadius: 10, overflow: 'hidden',
      border: '1px solid var(--line)', background: '#fafafa' }} />
    {!data && !err ? <div className="hint" style={{ position: 'absolute', top: 12, left: 14 }}>Загружаю траектории…</div> : null}
    {err ? <div className="hint" style={{ position: 'absolute', top: 12, left: 14, color: 'var(--danger)' }}>3D недоступно: {err}</div> : null}
    {hover ? <div className="hint" style={{ position: 'absolute', top: 10, right: 12, background: '#fff9', padding: '2px 8px', borderRadius: 6 }}>{hover.name}</div> : null}
    {data ? <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 8, flexWrap: 'wrap' }}>
      <label className="hint" style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 260 }}>
        слой <input type="range" min={1} max={layers} value={cur} style={{ flex: 1 }}
          onChange={(e) => setCur(Number(e.target.value))} />
        <span className="mono" style={{ width: 90 }}>{cur} / {layers}{data.header.layer_z && data.header.layer_z[cur] != null ? ` · Z ${data.header.layer_z[cur]}` : ''}</span>
      </label>
      <label className="hint" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <input type="checkbox" checked={dimInfill} onChange={(e) => setDimInfill(e.target.checked)} /> приглушить заполнение
      </label>
      <span className="hint">{layer ? <>серое — напечатано (ниже слоя {layer}), цветное — допечатается, </> : null}красное — вырезано, оранжевое — стартовые переезды</span>
    </div> : null}
  </div>;
}
