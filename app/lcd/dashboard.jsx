/* ─────────────────────────────────────────────────────────────────────────
   LCD Dashboard 1920×480 · Print Studio
   ─────────────────────────────────────────────────────────────────────────
   КОНТРАКТ ДАННЫХ (для интеграции с ботом / системным агентом):
   вызывайте window.applyDashboardData(data) с любой периодичностью.
   Первый же вызов отключает встроенную демо-симуляцию.

   data = {
     printer: {
       state: 'printing' | 'done' | 'idle',   // done — печать завершена, модель ещё на столе
       file: 'ECC2_0.4_Assembly_Kingroon PETG fixed',
       progress: 19,            // 0..100
       layer: 21,
       layers_total: 110,       // опционально
       remain_min: 394,         // минут до конца
       nozzle: 245, nozzle_target: 245,
       bed: 79, bed_target: 80,
       // когда state==='idle' — показывается очередь ненапечатанных заказов.
       // ВАЖНО: порядок не важен, фронт сам сортирует по created_ts (старые сверху).
       queue: [
         { id: 1042, customer: 'Иван К.', model: 'Кронштейн GoPro',
           qty: 2, material: 'PETG', color: '#ff7a1a',
           est_min: 260,           // оценка времени печати заказа, мин
           created_ts: 1718200000000 }  // когда заказ создан (ms, Date.now())
       ]
     },
     alerts: {
       messages: 2,             // непрочитанные сообщения клиентов
       unseen_orders: 1         // заказы, которые мастер ещё не открывал
     },
     system: {
       cpu_load: 23, cpu_temp: 52,
       gpu_load: 41, gpu_temp: 58,
       ram_pct: 46, vram_pct: 38
     }
   }
   Системные метрики на Windows проще всего брать из LibreHardwareMonitor
   (встроенный web-сервер, JSON на http://localhost:8085/data.json).
   ───────────────────────────────────────────────────────────────────────── */

const { useState, useEffect, useRef } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "accent": "#ff7a1a",
  "progressStyle": "Слои",
  "glow": 100,
  "ambient": true,
  "quietDemo": false,
  "demoScene": "Печать"
}/*EDITMODE-END*/;

/* ── плавный подвод чисел ── */
function useSmooth(target, rate) {
  const [v, setV] = useState(target);
  const ref = useRef({ v: target, raf: 0 });
  useEffect(() => {
    cancelAnimationFrame(ref.current.raf);
    const step = () => {
      const cur = ref.current.v;
      const next = cur + (target - cur) * (rate || 0.09);
      ref.current.v = Math.abs(target - next) < 0.05 ? target : next;
      setV(ref.current.v);
      if (ref.current.v !== target) ref.current.raf = requestAnimationFrame(step);
    };
    ref.current.raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(ref.current.raf);
  }, [target]);
  return v;
}

const pad2 = (n) => String(n).padStart(2, '0');
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
// метрики нет (бэкенд её не прислал: нет LibreHardwareMonitor, нет NVIDIA) → «—», а не NaN
const fin = (v) => typeof v === 'number' && isFinite(v);

function fmtRemain(min) {
  if (min == null) return '—';
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h > 0 ? h + ' ч ' + pad2(m) + ' мин' : m + ' мин';
}
function fmtFinish(min) {
  if (min == null) return '—';
  const d = new Date(Date.now() + min * 60000);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ' · ' + pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1);
}
function fmtAge(ts) {
  if (ts == null) return '—';
  const min = (Date.now() - ts) / 60000;
  if (min < 60) return Math.max(1, Math.round(min)) + ' мин';
  const h = min / 60;
  if (h < 24) return Math.round(h) + ' ч';
  return Math.round(h / 24) + ' дн';
}

/* ── демо-симуляция (отключается первым applyDashboardData) ── */
const DEMO_START = {
  printer: {
    state: 'printing', file: 'ECC2_0.4_Assembly_Kingroon PETG fixed',
    progress: 19, layer: 21, layers_total: 110, remain_min: 394,
    nozzle: 245, nozzle_target: 245, bed: 79, bed_target: 80,
    queue: [
      { id: 1042, customer: 'Иван К.', model: 'Кронштейн GoPro для шлема', qty: 2, material: 'PETG', color: '#ff7a1a', est_min: 260, created_ts: Date.now() - 3 * 86400000 },
      { id: 1048, customer: 'Мастерская «Винт»', model: 'Шестерня Z24, модуль 1.5', qty: 6, material: 'PETG-CF', color: '#79828f', est_min: 95, created_ts: Date.now() - 30 * 3600000 },
      { id: 1051, customer: 'Ирина М.', model: 'Корпус для Raspberry Pi 5', qty: 1, material: 'PLA', color: '#2ea6ff', est_min: 175, created_ts: Date.now() - 26 * 3600000 },
      { id: 1053, customer: 'Двор Кузьмича', model: 'Ножка для стола (реплика)', qty: 4, material: 'ABS', color: '#1f8a5b', est_min: 410, created_ts: Date.now() - 9 * 3600000 },
      { id: 1057, customer: 'Павел Р.', model: 'Вазон «волна» спиральный', qty: 1, material: 'PLA Silk', color: '#b06bff', est_min: 205, created_ts: Date.now() - 3 * 3600000 },
      { id: 1060, customer: 'Ольга Д.', model: 'Заглушка профиля 40×40', qty: 12, material: 'PETG', color: '#ff7a1a', est_min: 70, created_ts: Date.now() - 35 * 60000 }
    ]
  },
  alerts: { messages: 2, unseen_orders: 1 },
  system: { cpu_load: 23, cpu_temp: 52, gpu_load: 41, gpu_temp: 58, ram_pct: 46, vram_pct: 38 }
};
function demoTick(d) {
  const wob = (v, amp, lo, hi) => clamp(v + (Math.random() - 0.5) * amp, lo, hi);
  const p = { ...d.printer };
  p.progress = clamp(p.progress + 0.03, 0, 100);
  p.layer = Math.max(1, Math.round(p.progress / 100 * p.layers_total));
  p.remain_min = Math.max(0, p.remain_min - 0.03);
  p.nozzle = wob(p.nozzle, 2, 242, 247);
  p.bed = wob(p.bed, 1.2, 78, 81);
  const s = { ...d.system };
  s.cpu_load = wob(s.cpu_load, 14, 6, 72);
  s.gpu_load = wob(s.gpu_load, 18, 4, 96);
  s.cpu_temp = clamp(40 + s.cpu_load * 0.45 + (Math.random() - 0.5) * 3, 38, 88);
  s.gpu_temp = clamp(42 + s.gpu_load * 0.38 + (Math.random() - 0.5) * 3, 40, 86);
  s.ram_pct = wob(s.ram_pct, 4, 32, 78);
  s.vram_pct = clamp(s.gpu_load * 0.7 + 12 + (Math.random() - 0.5) * 6, 8, 95);
  return { ...d, printer: p, system: s };
}

/* ── цвет по нагреву/загрузке ── */
const hx = (h) => { h = h.replace('#', ''); return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]; };
const mixc = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
function heatColor(h, accentHex) {
  const dim = [98, 106, 120], acc = hx(accentHex), hot = [255, 48, 32];
  h = clamp(h, 0, 1);
  const c = h < 0.55 ? mixc(dim, acc, h / 0.55) : mixc(acc, hot, (h - 0.55) / 0.45);
  return 'rgb(' + c.join(',') + ')';
}
const loadHeat = (l) => fin(l) ? clamp((l - 25) / 70, 0, 1) : 0;
const tempHeat = (tC) => fin(tC) ? clamp((tC - 40) / 45, 0, 1) : 0;
const memHeat = (m) => fin(m) ? clamp((m - 35) / 60, 0, 1) : 0;

/* ── вентилятор: лопасти-дуги, симметричны — ось ровно в центре ── */
function Fan(props) {
  const r = props.r;
  const pol = (rr, aDeg) => {
    const a = (aDeg - 90) * Math.PI / 180;
    return (rr * Math.cos(a)).toFixed(2) + ' ' + (rr * Math.sin(a)).toFixed(2);
  };
  const blades = [];
  for (let i = 0; i < 4; i++) {
    const a0 = i * 90, a1 = a0 + 52, rm = r * 0.6;
    blades.push(
      <path key={i} d={'M ' + pol(rm, a0) + ' A ' + rm.toFixed(2) + ' ' + rm.toFixed(2) + ' 0 0 1 ' + pol(rm, a1)}
        fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth={r * 0.36} strokeLinecap="round"></path>
    );
  }
  return (
    <g transform={'translate(' + props.cx + ' ' + props.cy + ')'}>
      <circle r={r} fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.16)" strokeWidth="2"></circle>
      <g className="fanblades" style={{ animationDuration: props.dur + 's', animationDirection: props.rev ? 'reverse' : 'normal' }}>
        <circle r={r * 0.92} fill="none" stroke="none"></circle>
        {blades}
        <circle r={r * 0.2} fill="rgba(255,255,255,0.5)"></circle>
      </g>
    </g>
  );
}

/* ── система в стиле печатной платы — на весь блок, без дублирования данных ── */
function PCB(props) {
  const s = props.s, accent = props.accent;
  const hCpu = clamp((s.cpu_temp - 40) / 45, 0, 1);
  const hGpu = clamp((s.gpu_temp - 40) / 45, 0, 1);
  const hRam = clamp((s.ram_pct - 30) / 60, 0, 1);
  const hVram = clamp((s.vram_pct - 40) / 55, 0, 1);
  const hCl = clamp((s.cpu_load - 35) / 55, 0, 1);
  const hGl = clamp((s.gpu_load - 35) / 60, 0, 1);
  const cCpu = heatColor(hCpu, accent), cGpu = heatColor(hGpu, accent);
  const cRam = heatColor(hRam, accent), cVram = heatColor(hVram, accent);
  const cCl = heatColor(hCl, accent), cGl = heatColor(hGl, accent);
  const sig = 'color-mix(in oklab, ' + accent + ' 65%, transparent)';
  const gl = (h, c) => ({ filter: 'drop-shadow(0 0 calc(' + Math.round(4 + h * 13) + 'px * var(--glowf)) ' + c + ')' });
  const fanDur = (load) => clamp(4.5 - load / 25, 0.55, 4.5);
  const sigDur = (load) => clamp(3.6 - load / 32, 0.7, 3.6);

  const sticks = [];
  const ramLit = Math.max(1, Math.round(s.ram_pct / 100 * 4));
  for (let i = 0; i < 4; i++) {
    const lit = i < ramLit;
    sticks.push(
      <rect key={i} x={320 + i * 22} y="40" width="13" height="130" rx="4"
        fill={lit ? cRam : 'rgba(255,255,255,0.05)'} fillOpacity={lit ? 0.85 : 1}
        stroke="rgba(255,255,255,0.14)" strokeWidth="1" style={lit ? gl(hRam, cRam) : null}></rect>
    );
  }
  const vias = [[300, 215], [335, 232], [368, 218], [402, 240], [300, 262], [350, 270],
    [560, 130], [590, 110], [870, 30], [872, 248], [405, 95], [255, 170]];
  const trace = (d, dur, k) => (
    <g key={k}>
      <path className="tracebase" d={d}></path>
      {dur ? <path className="sig" d={d} stroke={sig} style={{ animationDuration: dur + 's' }}></path> : null}
    </g>
  );

  return (
    <div className="pcb" data-screen-label="Система — плата">
      <svg viewBox="0 0 900 300" preserveAspectRatio="xMidYMid meet">
        {/* монтажные отверстия */}
        {[[16, 16], [884, 16], [16, 284], [884, 284]].map((p, i) => (
          <circle key={i} cx={p[0]} cy={p[1]} r="5.5" fill="none" stroke="rgba(255,255,255,0.14)" strokeWidth="1.5"></circle>
        ))}
        {/* дорожки */}
        {trace('M 270 80 L 320 80', 0, 't1')}
        {trace('M 270 105 L 320 105', sigDur(s.cpu_load), 't2')}
        {trace('M 270 130 L 320 130', 0, 't3')}
        {trace('M 255 170 L 255 230 L 273 248 L 440 248', sigDur(s.gpu_load), 't4')}
        {trace('M 405 95 L 470 95', sigDur(s.cpu_load * 0.6), 't5')}
        {trace('M 505 130 L 505 210', sigDur(s.gpu_load * 0.7), 't6')}
        {trace('M 540 95 L 760 95', 3.4, 't7')}
        {trace('M 560 30 L 750 30 L 770 50 L 868 50', 4.2, 't8')}
        {trace('M 840 248 L 870 248', 0, 't9')}
        {trace('M 655 150 L 655 130 L 540 130', 0, 't10')}
        {/* via-точки */}
        {vias.map((p, i) => (
          <circle key={i} cx={p[0]} cy={p[1]} r="3" fill="#0a0c10" stroke="rgba(255,255,255,0.22)" strokeWidth="1.5"></circle>
        ))}
        {/* фронтальные вентиляторы */}
        <Fan cx={60} cy={80} r={30} dur={fanDur(s.cpu_load)}></Fan>
        <Fan cx={60} cy={170} r={30} dur={fanDur((s.cpu_load + s.gpu_load) / 2)} rev={true}></Fan>
        {/* PSU */}
        <rect x="28" y="228" width="82" height="50" rx="9" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.12)" strokeWidth="1.5"></rect>
        <text className="slab" x="69" y="257" textAnchor="middle">PSU</text>
        {/* CPU + помпа */}
        <g style={gl(hCpu, cCpu)}>
          <rect x="140" y="40" width="130" height="130" rx="16" fill={cCpu} fillOpacity="0.12" stroke={cCpu} strokeWidth="2"></rect>
          <circle cx="205" cy="105" r="40" fill="#0a0c10" stroke={cCpu} strokeWidth="2.5"></circle>
          <circle className="pumpring" cx="205" cy="105" r="30" fill="none" stroke={cCpu} strokeWidth="1.5" opacity="0.55"></circle>
          <text x="205" y="116" textAnchor="middle" fontSize="30" fontWeight="800" fill={cCpu}>{Math.round(s.cpu_temp)}°</text>
        </g>
        <text className="slab" x="205" y="194" textAnchor="middle">CPU <tspan fontSize="20" fontWeight="800" fill={cCl}>{Math.round(s.cpu_load)}%</tspan></text>
        {/* RAM */}
        {sticks}
        <text className="slab" x="363" y="194" textAnchor="middle">RAM <tspan fontSize="20" fontWeight="800" fill={cRam}>{Math.round(s.ram_pct)}%</tspan></text>
        {/* чипсет + M.2 + I/O */}
        <rect x="470" y="60" width="70" height="70" rx="10" fill="rgba(255,255,255,0.025)" stroke="rgba(255,255,255,0.14)" strokeWidth="1.5"></rect>
        <text className="slab" x="505" y="99" textAnchor="middle">PCH</text>
        <rect x="600" y="150" width="110" height="17" rx="5" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.12)" strokeWidth="1.5"></rect>
        <text className="slab" x="655" y="163" textAnchor="middle" fontSize="12">M.2</text>
        <rect x="760" y="78" width="80" height="36" rx="8" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.12)" strokeWidth="1.5"></rect>
        <text className="slab" x="800" y="100" textAnchor="middle">I/O</text>
        {/* GPU */}
        <text className="slab" x="440" y="200" textAnchor="start">GPU</text>
        <g style={gl(hGpu, cGpu)}>
          <rect x="440" y="210" width="400" height="76" rx="12" fill={cGpu} fillOpacity="0.12" stroke={cGpu} strokeWidth="2"></rect>
          <text x="688" y="250" textAnchor="start" fontSize="36" fontWeight="800" fill={cGpu}>{Math.round(s.gpu_temp)}°</text>
        </g>
        <text x="768" y="250" textAnchor="start" fontSize="22" fontWeight="800" fill={cGl}>{Math.round(s.gpu_load)}%</text>
        <Fan cx={500} cy={248} r={27} dur={fanDur(s.gpu_load)}></Fan>
        <Fan cx={570} cy={248} r={27} dur={fanDur(s.gpu_load)} rev={true}></Fan>
        <Fan cx={640} cy={248} r={27} dur={fanDur(s.gpu_load)}></Fan>
        <text className="slab" x="688" y="266" textAnchor="start" fontSize="13.5">VRAM <tspan fontSize="18" fontWeight="800" fill={cVram}>{Math.round(s.vram_pct)}%</tspan></text>
        <rect x="688" y="272" width="142" height="5" rx="2.5" fill="rgba(255,255,255,0.08)"></rect>
        <rect x="688" y="272" width={142 * clamp(s.vram_pct, 0, 100) / 100} height="5" rx="2.5" fill={cVram} style={gl(hVram, cVram)}></rect>
      </svg>
    </div>
  );
}

/* ── иконки (минимальные) ── */

/* ── иконки ── */
function IconChat() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12a8 8 0 0 1-8 8H4l2.5-3A8 8 0 1 1 21 12z"></path>
      <circle cx="9" cy="12" r="0.8" fill="currentColor"></circle>
      <circle cx="13" cy="12" r="0.8" fill="currentColor"></circle>
      <circle cx="17" cy="12" r="0.8" fill="currentColor"></circle>
    </svg>
  );
}
function IconOrder() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5" y="3" width="14" height="18" rx="2.5"></rect>
      <line x1="9" y1="8" x2="15" y2="8"></line>
      <line x1="9" y1="12" x2="15" y2="12"></line>
      <line x1="9" y1="16" x2="12" y2="16"></line>
    </svg>
  );
}
function IconCheck() {
  return (
    <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="5 12.5 10 17.5 19 7"></polyline>
    </svg>
  );
}

/* ── часы ── */
function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: '18px' }}>
      <span className="date">{pad2(now.getDate())}.{pad2(now.getMonth() + 1)}</span>
      <span className="clock">{pad2(now.getHours())}<span className="colon">:</span>{pad2(now.getMinutes())}</span>
    </div>
  );
}

/* ── кольцевой прогресс ── */
function RingGauge(props) {
  const p = useSmooth(props.progress);
  const R = 132, C = 2 * Math.PI * R;
  const off = C * (1 - p / 100);
  const ticks = [];
  for (let i = 0; i < 64; i++) {
    const a = (-90 + i * (360 / 64)) * Math.PI / 180;
    const lit = (i / 64) * 100 <= p && p > 0.5;
    ticks.push(
      <line key={i} className={'tick' + (lit ? ' lit' : '')}
        x1={156 + Math.cos(a) * 110} y1={156 + Math.sin(a) * 110}
        x2={156 + Math.cos(a) * 118} y2={156 + Math.sin(a) * 118} strokeWidth="2"></line>
    );
  }
  const tipA = (-90 + p * 3.6) * Math.PI / 180;
  return (
    <div className="gaugebox">
      <svg className="ring-svg" width="312" height="312" viewBox="0 0 312 312">
        <g>{ticks}</g>
        <circle className="ring-track" cx="156" cy="156" r={R} fill="none" strokeWidth="13"></circle>
        <circle className="ring-fill" cx="156" cy="156" r={R} fill="none" strokeWidth="13"
          strokeDasharray={C} strokeDashoffset={off} transform="rotate(-90 156 156)"></circle>
        {p > 0.5 && (
          <g className="tipdot">
            <circle cx={156 + Math.cos(tipA) * R} cy={156 + Math.sin(tipA) * R} r="9.5" fill="#ffffff"></circle>
            <circle cx={156 + Math.cos(tipA) * R} cy={156 + Math.sin(tipA) * R} r="4" fill="var(--acc)"></circle>
          </g>
        )}
      </svg>
      <div className="ringcenter">
        <div className="pct">{Math.round(p)}<small>%</small></div>
        <div className="layerline">{props.state === 'done' ? 'модель готова' : props.state === 'idle' ? 'ожидание' : 'слой ' + (props.layer ?? '—') + (props.total ? ' / ' + props.total : '')}</div>
      </div>
    </div>
  );
}

/* ── «модель печатается» (альтернативный вид) ── */
function LayersGauge(props) {
  const p = useSmooth(props.progress);
  const N = 24;
  const filled = p / 100 * N;
  const bars = [];
  for (let i = 0; i < N; i++) {
    const t = i / (N - 1);
    const w = 168 * (0.52 + 0.44 * Math.sin(Math.PI * (0.12 + 0.82 * t)));
    const done = i < Math.floor(filled);
    const active = i === Math.floor(filled) && p < 99.9;
    bars.push(
      <div key={i} className={'layer' + (done ? ' done' : '') + (active ? ' active' : '')} style={{ width: w + 'px' }}>
        {active ? <span className="head"></span> : null}
      </div>
    );
  }
  return (
    <div className="layersbox">
      <div className={'layerstack' + (p >= 99.9 ? ' full' : '')}>
        {bars}
        <div className="plate"></div>
      </div>
      <div className="layerspct">
        <div className="pct">{Math.round(p)}<small>%</small></div>
        <div className="layerline">{props.state === 'done' ? 'модель готова' : props.state === 'idle' ? 'ожидание' : 'слой ' + (props.layer ?? '—') + (props.total ? ' / ' + props.total : '')}</div>
      </div>
    </div>
  );
}

/* ── температурная строка ── */
function TempBar(props) {
  const v = useSmooth(props.value, 0.12);
  return (
    <div className="temp">
      <div className="trow">
        <span className="tl">{props.label}</span>
        <span className="tv">{Math.round(v)}°</span>
        <span className="tt">/ {props.target ? Math.round(props.target) + '°' : 'выкл'}</span>
      </div>
      <div className="tbar"><i style={{ width: clamp(v / props.max * 100, 0, 100) + '%' }}></i></div>
    </div>
  );
}

/* ── система · телеметрия: история значений ── */
function useHist(s) {
  // история стартует ПУСТОЙ: засеянная первым кадром, она полторы минуты рисовала бы демо-числа
  const [hist, setHist] = useState(() => ({
    cpu_load: Array(46).fill(null), cpu_temp: Array(46).fill(null),
    gpu_load: Array(46).fill(null), gpu_temp: Array(46).fill(null)
  }));
  useEffect(() => {
    setHist((h) => {
      const push = (a, v) => { const n = a.slice(1); n.push(v); return n; };
      return {
        cpu_load: push(h.cpu_load, s.cpu_load), cpu_temp: push(h.cpu_temp, s.cpu_temp),
        gpu_load: push(h.gpu_load, s.gpu_load), gpu_temp: push(h.gpu_temp, s.gpu_temp)
      };
    });
  }, [s.cpu_load, s.cpu_temp, s.gpu_load, s.gpu_temp]);
  return hist;
}

/* график-осциллограф: сплошная линия — загрузка, пунктир — температура */
function GPanel(props) {
  const W = 100, H = 100;
  const y = (v) => (H - clamp(v, 0, 100) / 100 * H).toFixed(2);
  const x = (i, n) => ((i / (n - 1)) * W).toFixed(2);
  const seg = (arr) => arr.map((v, i) => fin(v) ? [x(i, arr.length), y(v)] : null).filter(Boolean);
  const pts = (p) => p.map((q) => q.join(',')).join(' ');
  const lp = seg(props.load);
  const area = lp.length ? 'M' + lp[0][0] + ',' + H + ' L' + lp.map((q) => q.join(',')).join(' L') +
    ' L' + lp[lp.length - 1][0] + ',' + H + ' Z' : '';
  const col = heatColor(loadHeat(props.cur), props.accent);
  const tcol = heatColor(tempHeat(props.curT), props.accent);
  return (
    <div className="gpanel">
      <div className="gtop">
        <span className="gtitle">{props.title}</span>
        <span className="gtemp" style={{ color: tcol }}>{fin(props.curT) ? Math.round(props.curT) + '°' : '—°'}</span>
        <span className="gbig" style={{ color: col }}>{fin(props.cur) ? Math.round(props.cur) : '—'}<em>%</em></span>
      </div>
      <svg className="gsvg" viewBox={'0 0 ' + W + ' ' + H} preserveAspectRatio="none">
        <defs>
          <linearGradient id={props.gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={col} stopOpacity="0.34"></stop>
            <stop offset="100%" stopColor={col} stopOpacity="0"></stop>
          </linearGradient>
        </defs>
        {[25, 50, 75].map((gy) => (
          <line key={gy} x1="0" y1={gy} x2={W} y2={gy} stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"></line>
        ))}
        {area ? <path d={area} fill={'url(#' + props.gid + ')'}></path> : null}
        <polyline points={pts(lp)} fill="none" stroke={col} strokeWidth="1.8" vectorEffect="non-scaling-stroke"
          style={{ filter: 'drop-shadow(0 0 calc(4px * var(--glowf)) ' + col + ')' }}></polyline>
        <polyline points={pts(seg(props.temp))} fill="none" stroke="rgba(255,255,255,0.42)" strokeWidth="1" strokeDasharray="3 2.5" vectorEffect="non-scaling-stroke"></polyline>
      </svg>
    </div>
  );
}

function Meter(props) {
  const known = fin(props.value);
  const v = useSmooth(known ? props.value : 0, 0.12);
  const col = heatColor(memHeat(v), props.accent);
  return (
    <div className="meter">
      <div className="mtop"><span className="mname">{props.name}</span><span className="mval" style={{ color: col }}>{known ? Math.round(v) : '—'}%</span></div>
      <div className="mtrack"><div className="mfill" style={{ width: clamp(v, 0, 100) + '%', background: col, boxShadow: '0 0 calc(8px * var(--glowf)) ' + col }}></div></div>
    </div>
  );
}

/* ── очередь ненапечатанных заказов (экран простоя) ── */
function QRow(props) {
  const o = props.o;
  return (
    <div className={'qrow' + (props.next ? ' next' : '')}>
      <span className="qidx">{props.pos}</span>
      <div className="qmain">
        <div className="qtitle">#{o.id} · {o.customer}{o.qty > 1 ? <span className="qqty">×{o.qty}</span> : null}</div>
        <div className="qsub">{o.model}</div>
      </div>
      {o.material ? (
        <span className="qchip"><i style={{ background: o.color || 'var(--muted)' }}></i>{o.material}</span>
      ) : null}
      <div className="qmeta">
        <span className="qwait">ждёт {fmtAge(o.created_ts)}</span>
        <span className="qest">печать ≈ {fmtRemain(o.est_min)}</span>
      </div>
    </div>
  );
}
function QueueZone(props) {
  const d = props.d;
  const q = [...(d.queue || [])].sort((a, b) => (a.created_ts || 0) - (b.created_ts || 0));
  const MAX = 4;
  const shown = q.slice(0, MAX);
  const totalMin = q.reduce((sum, o) => sum + (o.est_min || 0), 0);
  return (
    <section className="card queuecard" data-screen-label="Принтер · очередь">
      <div className="qhead">
        <span className="cardlab">Принтер</span>
        <span className="statechip idle">Свободен</span>
        {q.length > 0 ? (
          <span className="qcount"><b>{q.length}</b> в очереди · ≈ {fmtRemain(totalMin)} печати</span>
        ) : null}
      </div>
      {q.length === 0 ? (
        <div className="idlebox" style={{ margin: 'auto 0' }}>
          <div className="t">Очередь пуста — жду задание</div>
          <div className="s">стол свободен, можно запускать печать</div>
        </div>
      ) : (
        <div className="qlist">
          {shown.map((o, i) => <QRow key={o.id || i} o={o} pos={i + 1} next={i === 0}></QRow>)}
        </div>
      )}
    </section>
  );
}

/* ── зоны ── */
function PrinterZone(props) {
  const d = props.data.printer;
  const st = d.state === 'done' ? 'done' : (d.state === 'idle' ? 'idle' : 'printing');
  if (st === 'idle') return <QueueZone d={d}></QueueZone>;
  // прогресса прошивка может не знать (кадр без поля) — кольцо пустое, а не NaN
  const prog = st === 'done' ? 100 : (Number.isFinite(d.progress) ? d.progress : 0);
  const layer = st === 'done' ? (d.layers_total || d.layer) : d.layer;
  const ring = props.progressStyle !== 'Слои';
  return (
    <section className="card printcard" data-screen-label="Принтер">
      {ring
        ? <RingGauge progress={prog} layer={layer} total={d.layers_total} state={st}></RingGauge>
        : <LayersGauge progress={prog} layer={layer} total={d.layers_total} state={st}></LayersGauge>}
      <div className="printinfo">
        <div className="stateline">
          <span className="cardlab">Принтер</span>
          {st === 'done' ? <span className="statechip">Готово</span> : null}
        </div>
        <div className="filename">{d.file}</div>
        {st === 'printing' ? (
          <div className="metarow">
            <div className="meta">
              <span className="l">Осталось</span>
              <span className="v">{fmtRemain(d.remain_min)}</span>
            </div>
            <div className="meta">
              <span className="l">Финиш</span>
              <span className="v">{fmtFinish(d.remain_min)}</span>
            </div>
          </div>
        ) : (
          <div className="donebox">
            <span className="dcheck"><IconCheck></IconCheck></span>
            <div>
              <div className="t">Печать завершена</div>
              <div className="s">модель можно снимать со стола</div>
            </div>
          </div>
        )}
        <div className="temps">
          <TempBar label="Сопло" value={d.nozzle} target={d.nozzle_target} max={300}></TempBar>
          <TempBar label="Стол" value={d.bed} target={d.bed_target} max={110}></TempBar>
        </div>
      </div>
    </section>
  );
}

function AlertsZone(props) {
  const a = props.data.alerts;
  const total = (a.messages || 0) + (a.unseen_orders || 0);
  return (
    <section className={'card alertcard' + (total > 0 ? ' hasalerts' : '')} data-screen-label="Уведомления">
      <span className="cardlab">Внимание</span>
      {total > 0 ? (
        <div className="alertrows">
          <div className={'alertrow' + (a.messages > 0 ? ' on' : '')}>
            <span className="aicon"><IconChat></IconChat></span>
            <div className="atext">
              <div className="t">Сообщения клиентов</div>
              <div className="s">{a.messages > 0 ? 'ждут ответа' : 'все прочитаны'}</div>
            </div>
            <span className="acount">{a.messages || 0}</span>
          </div>
          <div className={'alertrow' + (a.unseen_orders > 0 ? ' on' : '')}>
            <span className="aicon"><IconOrder></IconOrder></span>
            <div className="atext">
              <div className="t">Непросмотренные заказы</div>
              <div className="s">{a.unseen_orders > 0 ? 'вы их ещё не открывали' : 'все просмотрены'}</div>
            </div>
            <span className="acount">{a.unseen_orders || 0}</span>
          </div>
        </div>
      ) : (
        <div className="allclear">
          <span className="ok"><IconCheck></IconCheck></span>
          <div className="m">Всё просмотрено</div>
          <div className="s">новых сообщений и заказов нет</div>
        </div>
      )}
    </section>
  );
}

function SystemZone(props) {
  const s = props.data.system;
  const hist = useHist(s);
  return (
    <section className="card syscard" data-screen-label="Система">
      <span className="cardlab">Система</span>
      <div className={'telem' + (props.wide ? ' wide' : '')}>
        <div className="graphs">
          <GPanel title="CPU" load={hist.cpu_load} temp={hist.cpu_temp} cur={s.cpu_load} curT={s.cpu_temp} accent={props.accent} gid="dcpu"></GPanel>
          <GPanel title="GPU" load={hist.gpu_load} temp={hist.gpu_temp} cur={s.gpu_load} curT={s.gpu_temp} accent={props.accent} gid="dgpu"></GPanel>
        </div>
        <div className="meters">
          <Meter name="RAM" value={s.ram_pct} accent={props.accent}></Meter>
          <Meter name="VRAM" value={s.vram_pct} accent={props.accent}></Meter>
        </div>
      </div>
    </section>
  );
}

/* ── приложение ── */
function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [data, setData] = useState(DEMO_START);
  const external = useRef(false);
  const [scale, setScale] = useState(1);

  // внешние данные отключают демо
  useEffect(() => {
    window.applyDashboardData = (d) => {
      // первые настоящие данные ВЫБРАСЫВАЮТ демо-метрики системы, а не мержатся поверх них:
      // бэкенд не шлёт то, чего не знает (cpu_temp без LibreHardwareMonitor), и на экране
      // навсегда застывали демо-52° — «неправильная температура» (24.09.2026)
      const first = !external.current;
      external.current = true;
      // Секция принтера ЗАМЕНЯЕТСЯ целиком: бэкенд шлёт её в каждом кадре, а то, чего не
      // знает, опускает. Мерж поверх прошлого держал на экране демо-файл «Assembly_Kingroon» и
      // «/ 110» слоёв из макета (06.10.2026), а между заданиями протащил бы слои прошлой печати.
      setData((prev) => ({
        printer: d.printer ? { ...d.printer } : prev.printer,
        alerts: { ...prev.alerts, ...(d.alerts || {}) },
        system: { ...(first ? {} : prev.system), ...(d.system || {}) }
      }));
    };
  }, []);

  // демо-симуляция
  useEffect(() => {
    const id = setInterval(() => {
      if (!external.current) setData((d) => demoTick(d));
    }, 2000);
    return () => clearInterval(id);
  }, []);

  // масштабирование под вьюпорт (устойчиво к нулевым размерам при загрузке)
  useEffect(() => {
    let raf = 0;
    const fit = () => {
      const w = window.innerWidth, h = window.innerHeight;
      if (!w || !h) { raf = requestAnimationFrame(fit); return; }
      setScale(Math.min(w / 1920, h / 480));
    };
    fit();
    window.addEventListener('resize', fit);
    const ro = new ResizeObserver(fit);
    ro.observe(document.documentElement);
    return () => { window.removeEventListener('resize', fit); ro.disconnect(); cancelAnimationFrame(raf); };
  }, []);

  // одноразовая boot-анимация карточек
  const [boot, setBoot] = useState(true);
  useEffect(() => {
    const id = setTimeout(() => setBoot(false), 1400);
    return () => clearTimeout(id);
  }, []);

  // твики → CSS-переменные
  useEffect(() => {
    const r = document.documentElement;
    r.style.setProperty('--acc', t.accent);
    r.style.setProperty('--glowf', String(t.glow / 100));
  }, [t.accent, t.glow]);

  const shownBase = t.quietDemo && !external.current
    ? { ...data, alerts: { messages: 0, unseen_orders: 0 } }
    : data;
  let shown = shownBase;
  if (!external.current) {
    if (t.demoScene === 'Готово') {
      shown = { ...shownBase, printer: { ...shownBase.printer, state: 'done', progress: 100, layer: shownBase.printer.layers_total, remain_min: 0, nozzle: 63, nozzle_target: 0, bed: 31, bed_target: 0 } };
    } else if (t.demoScene === 'Простой') {
      shown = { ...shownBase, printer: { ...shownBase.printer, state: 'idle', nozzle: 24, nozzle_target: 0, bed: 23, bed_target: 0 } };
    }
  }
  const quiet = ((shown.alerts.messages || 0) + (shown.alerts.unseen_orders || 0)) === 0;

  return (
    <div id="viewport" data-amb={t.ambient ? 'on' : 'off'}>
      <div id="stage" className={boot ? 'boot' : ''} style={{ transform: 'translate(-50%, -50%) scale(' + scale + ')' }} data-screen-label="LCD дашборд 1920×480">
        <div className="amb amb1"></div>
        <div className="amb amb2"></div>
        <div className="wrap">
          <header className="topbar">
            <span className="wordmark">PRINT<span className="dot">·</span>STUDIO</span>
            <span className="livechip"><i></i>LIVE</span>
            <span className="topspacer"></span>
            <Clock></Clock>
          </header>
          <main className={'grid' + (quiet ? ' wide' : '')}>
            <PrinterZone data={shown} progressStyle={t.progressStyle}></PrinterZone>
            {!quiet ? <AlertsZone data={shown}></AlertsZone> : null}
            <SystemZone data={shown} wide={quiet} accent={t.accent}></SystemZone>
          </main>
        </div>
      </div>
      <TweaksPanel>
        <TweakSection label="Стиль"></TweakSection>
        <TweakColor label="Акцент" value={t.accent}
          options={['#ff7a1a', '#ff3548', '#e8112d']}
          onChange={(v) => setTweak('accent', v)}></TweakColor>
        <TweakRadio label="Прогресс" value={t.progressStyle}
          options={['Кольцо', 'Слои']}
          onChange={(v) => setTweak('progressStyle', v)}></TweakRadio>
        <TweakSlider label="Свечение" value={t.glow} min={0} max={100} unit="%"
          onChange={(v) => setTweak('glow', v)}></TweakSlider>
        <TweakToggle label="Живой фон" value={t.ambient}
          onChange={(v) => setTweak('ambient', v)}></TweakToggle>
        <TweakSection label="Демо"></TweakSection>
        <TweakRadio label="Принтер" value={t.demoScene}
          options={['Печать', 'Готово', 'Простой']}
          onChange={(v) => setTweak('demoScene', v)}></TweakRadio>
        <TweakToggle label="Тихий режим (без уведомлений)" value={t.quietDemo}
          onChange={(v) => setTweak('quietDemo', v)}></TweakToggle>
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App></App>);
