// printext.jsx — «Печать», вторая очередь: что подсмотрено у прошивки сообщества CC2 и сделано
// своим (docs/Перспективные разработки/cc2-community-features-2026-10-03.md).
//
// Стоковая часть работает у любого CC2: диалог старта «филамент файла → слот хаба», режим
// скорости, аварийный стоп удержанием, профили прогрева, автодозаправка, состояние машины.
// Остальное — только если на принтере стоит прошивка сообщества (Neme77/centauri-carbon-2-
// community): драйвер находит её сам и включает способности, по которым здесь гейтится каждая
// кнопка (pr.can('exclude_object' | 'bed_mesh' | 'live_tune' | 'gcode' | 'slot_edit')).

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty, Drawer } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { minutes as fmtMin, grams as fmtG } from '../fmt.jsx';
import { ABtn, Chips, useConfirm, KV } from '../parts.jsx';
import { ensureThree, webglOK } from '../three3d.jsx';

const { useState, useContext, useEffect, useRef, useMemo, useCallback } = React;

// Стол CC2 — 256×256 мм. Схемы рисуем в его координатах: перед стола внизу, как смотрит мастер.
const BED = 256;

const safeLS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* приватное окно */ } },
};

/** Тост по ответу команды: подтверждённый успех, «без подтверждения» или отказ принтера. */
function useRun() {
  const { toast } = useContext(AppCtx);
  return useCallback(async (okText, fn) => {
    try {
      const r = await fn();
      if (r && r.unconfirmed) toast(okText + ' (принтер не подтвердил)', { kind: 'warn2', ms: 7000 });
      else toast(okText, { kind: 'ok' });
      return r || {};
    } catch (e) {
      toast(e.message, { kind: 'warn2', ms: 9000 });
      return null;
    }
  }, [toast]);
}

// ─── состояние машины ─────────────────────────────────────────────────────────────────────

/** «Печать · Прогрев стола», «Автовыравнивание стола»… — по таблице вендора. Простой не пишем. */
export function machineText(pr) {
  const m = pr && pr.machine;
  if (!m) return '';
  if ((m.status === 1 || m.status === 2) && !m.detail) return '';
  return m.detail ? `${m.label} · ${m.detail}` : m.label;
}

export function MachineBadge({ pr }) {
  const t = machineText(pr);
  if (!t) return null;
  return <span style={{ fontSize: 12, color: 'var(--amber)', fontWeight: 600 }}>{t}</span>;
}

// ─── режим скорости ───────────────────────────────────────────────────────────────────────

export const SPEED_MODES = [[0, 'Тихий'], [1, 'Баланс'], [2, 'Спорт'], [3, 'Безумный']];

export function SpeedChips({ pr }) {
  const run = useRun();
  const [busy, setBusy] = useState(false);
  if (!pr || !pr.can('speed_mode') || !pr.online) return null;
  return <div className="ctl-row" style={{ marginTop: 10, flexWrap: 'wrap' }}>
    <span className="ctl-k"><DIcon n="bolt" size={13} />Скорость</span>
    <div className="ctl-btns" style={{ flexWrap: 'wrap' }}>
      {SPEED_MODES.map(([m, l]) => <button key={m} className="db sm line" disabled={busy}
        style={pr.speedMode === m ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
        title={pr.speedMode === m ? 'текущий режим' : `переключить на «${l}»`}
        onClick={async () => {
          setBusy(true);
          try { await run(`Режим скорости: ${l}`, () => call('adminPrinterSpeed', m)); } finally { setBusy(false); }
        }}>{l}</button>)}
    </div>
  </div>;
}

// ─── аварийный стоп удержанием ────────────────────────────────────────────────────────────

/** Кнопка, которая срабатывает только после удержания: случайный клик ничего не делает. */
export function HoldButton({ ms = 1000, onHold, children, className = 'db danger', style, title, disabled }) {
  const [p, setP] = useState(0);
  const t0 = useRef(0);
  const raf = useRef(0);
  const fired = useRef(false);
  const stop = () => { cancelAnimationFrame(raf.current); raf.current = 0; setP(0); };
  const tick = () => {
    const k = Math.min(1, (performance.now() - t0.current) / ms);
    setP(k);
    if (k >= 1) {
      if (!fired.current) { fired.current = true; stop(); onHold(); }
      return;
    }
    raf.current = requestAnimationFrame(tick);
  };
  useEffect(() => () => cancelAnimationFrame(raf.current), []);
  return <button className={className} disabled={disabled} title={title}
    style={{ ...style, backgroundImage: p ? `linear-gradient(90deg, rgba(255,255,255,.28) ${p * 100}%, transparent ${p * 100}%)` : undefined }}
    onPointerDown={(e) => { if (disabled) return; e.preventDefault(); fired.current = false; t0.current = performance.now(); raf.current = requestAnimationFrame(tick); }}
    onPointerUp={stop} onPointerLeave={stop} onPointerCancel={stop}
    onClick={(e) => e.preventDefault()}>{children}</button>;
}

/**
 * Аварийный стоп. С прошивкой сообщества — настоящий M112 (Klipper встаёт мгновенно, дальше
 * только перезапуск принтера). На стоке такой команды нет, поэтому «мягкий»: прервать печать,
 * выключить нагрев и обдув. В обоих случаях — удержание секунду, без лишнего диалога: в аварии
 * некогда читать вопросы, а случайного нажатия удержание не допустит.
 */
export function EStopButton({ pr, style }) {
  const { toast } = useContext(AppCtx);
  if (!pr || !pr.online || !pr.can('move')) return null;
  const hard = pr.can('gcode');
  return <HoldButton style={style}
    title={hard ? 'Держите секунду: M112 — принтер встанет мгновенно, дальше только перезапуск'
      : 'Держите секунду: прервать печать и выключить нагрев и обдув'}
    onHold={async () => {
      try {
        if (hard) {
          await call('adminCommunityOp', 'estop');
          toast('M112 отправлен: принтер остановлен. Чтобы продолжить, выключите и включите его.', { kind: 'warn2', ms: 15000 });
        } else {
          const r = await call('adminPrinterOff', 'soft_stop');
          toast('Аварийный стоп: ' + ((r.done || []).join(', ') || 'выполнен'), { kind: 'warn2', ms: 10000 });
        }
      } catch (e) { toast(e.message, { kind: 'err', ms: 12000 }); }
    }}><DIcon n="stop" size={14} />Аварийный стоп</HoldButton>;
}

// ─── профили прогрева ─────────────────────────────────────────────────────────────────────

export function useHeatPresets() {
  return useApi('adminHeatPresets', [], []);
}

export function PresetsDrawer({ presets, onSaved, onClose }) {
  const { toast } = useContext(AppCtx);
  const [rows, setRows] = useState(() => (presets || []).map((p) => ({ ...p })));
  const set = (i, k, v) => setRows((r) => r.map((x, j) => (j === i ? { ...x, [k]: v } : x)));
  const save = async (reset) => {
    try {
      const r = await call('adminHeatPresetsSave', reset ? null : rows.map((x) => ({
        name: String(x.name || '').trim(), nozzle: Number(x.nozzle), bed: Number(x.bed) })));
      toast(reset ? 'Профили сброшены' : 'Профили прогрева сохранены', { kind: 'ok' });
      onSaved(r.presets);
      onClose();
    } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); }
  };
  return <Drawer title="Профили прогрева" sub="кнопки прогрева на пульте: сопло и стол одним нажатием" onClose={onClose}
    footer={<>
      <ABtn kind="line" onClick={() => save(true)}>По умолчанию</ABtn>
      <ABtn kind="full" icon="check" onClick={() => save(false)}>Сохранить</ABtn>
    </>}>
    {rows.map((p, i) => <div key={i} style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr 1fr auto', gap: 8, marginBottom: 8 }}>
      <input className="inp" value={p.name} maxLength={16} placeholder="Материал" onChange={(e) => set(i, 'name', e.target.value)} />
      <input className="inp" type="number" min="0" max="320" value={p.nozzle} title="сопло, °C" onChange={(e) => set(i, 'nozzle', e.target.value)} />
      <input className="inp" type="number" min="0" max="110" value={p.bed} title="стол, °C" onChange={(e) => set(i, 'bed', e.target.value)} />
      <button className="db sm line" style={{ color: 'var(--red)' }} title="Удалить профиль"
        onClick={() => setRows((r) => r.filter((_, j) => j !== i))}><DIcon n="trash" size={14} /></button>
    </div>)}
    <div className="hint" style={{ margin: '4px 0 12px' }}>сопло 0…320 °C, стол 0…110 °C · не больше 12 профилей</div>
    {rows.length < 12 ? <button className="db line" onClick={() => setRows((r) => r.concat([{ name: '', nozzle: 210, bed: 60 }]))}>
      <DIcon n="plus" size={14} />Добавить профиль</button> : null}
  </Drawer>;
}

// ─── диалог старта: филамент файла → слот хаба ────────────────────────────────────────────

function rgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function colorDist(a, b) {
  const x = rgb(a); const y = rgb(b);
  if (!x || !y) return 200;
  return Math.sqrt((x[0] - y[0]) ** 2 + (x[1] - y[1]) ** 2 + (x[2] - y[2]) ** 2);
}
const sameType = (a, b) => String(a || '').trim().toUpperCase() === String(b || '').trim().toUpperCase();

/** Подбор слота под каждый филамент файла: тот же материал, ближайший цвет, без повторов. */
export function autoSlotMap(colors, slots) {
  const used = new Set();
  const out = {};
  const loaded = (slots || []).filter((s) => s.status !== 0);
  [...(colors || [])].sort((a, b) => (a.extruder || 0) - (b.extruder || 0)).forEach((c) => {
    let best = null; let bestScore = Infinity;
    loaded.forEach((s) => {
      const score = (sameType(c.type, s.type) ? 0 : 1000) + colorDist(c.hex, s.color) + (used.has(s.slot) ? 500 : 0);
      if (score < bestScore) { bestScore = score; best = s.slot; }
    });
    if (best != null) { out[c.extruder || 0] = best; used.add(best); }
  });
  return out;
}

const Swatch = ({ c, size = 14 }) => <span style={{ width: size, height: size, borderRadius: '50%', flex: '0 0 auto',
  display: 'inline-block', background: c || 'var(--bg)', border: '1px solid var(--line)', verticalAlign: -2 }} />;

export function StartDrawer({ file, pr, storage = 'local', onClose, onStarted }) {
  const { toast } = useContext(AppCtx);
  const [colors, setColors] = useState(file.colors || []);
  const [loading, setLoading] = useState(!(file.colors && file.colors.length));
  const slots = (pr && pr.slots) || [];
  const [map, setMap] = useState(() => autoSlotMap(file.colors, slots));
  const [plate, setPlate] = useState(() => safeLS.get('pcb_plate_side', 'A'));
  const [bedlevel, setBedlevel] = useState(false);
  const [busy, setBusy] = useState(false);

  // Список файлов мог прийти без color_map (пассивный сбор старых версий) — спросим сам файл.
  useEffect(() => {
    if (file.colors && file.colors.length) return undefined;
    let alive = true;
    call('adminPrinterFileInfo', file.name, storage)
      .then((r) => {
        if (!alive) return;
        const cs = (r.file && r.file.colors) || [];
        setColors(cs);
        setMap(autoSlotMap(cs, slots));
      })
      .catch(() => {})
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [file.name]);   // eslint-disable-line

  const hub = slots.length > 0;
  const tools = [...colors].sort((a, b) => (a.extruder || 0) - (b.extruder || 0));
  const unmapped = hub ? tools.filter((c) => !map[c.extruder || 0]) : [];
  const warnings = [];
  if (hub) {
    tools.forEach((c) => {
      const s = slots.find((x) => x.slot === map[c.extruder || 0]);
      if (!s) return;
      if (c.type && s.type && !sameType(c.type, s.type)) {
        warnings.push(`T${c.extruder || 0}: файл ждёт ${c.type}, в слоте ${s.slot} — ${s.type}`);
      }
      if (s.status === 0) warnings.push(`Слот ${s.slot} пуст`);
    });
  }

  const go = async () => {
    setBusy(true);
    try {
      const opts = { plate, bedlevel, storage };
      if (hub && tools.length) {
        opts.slot_map = tools.map((c) => ({ t: c.extruder || 0, tray_id: map[c.extruder || 0] - 1, canvas_id: 0 }));
      }
      const r = await call('adminPrinterPrint', file.name, opts);
      safeLS.set('pcb_plate_side', plate);
      // С флешки CC2 Control печатает из копии в памяти принтера — копирование и было паузой.
      toast(r && r.imported_from_usb ? `«${file.name}» скопирован с флешки в память принтера и отправлен в печать`
        : `«${file.name}» отправлен в печать`, { kind: 'ok' });
      if (onStarted) onStarted();
      onClose();
    } catch (e) {
      toast(e.message, { kind: 'err', ms: 10000 });
    } finally { setBusy(false); }
  };

  return <Drawer title="Старт печати" sub={file.name} onClose={onClose}
    footer={<ABtn kind="full" icon="printer" disabled={busy || unmapped.length > 0 || loading} onClick={go}>
      {unmapped.length ? `Назначьте слот: ${unmapped.map((c) => 'T' + (c.extruder || 0)).join(', ')}` : 'Стол пуст — печатать'}
    </ABtn>}>
    <div className="hint" style={{ marginBottom: 12 }}>
      {[file.print_time ? fmtMin(file.print_time / 60) : '', file.layer ? `${file.layer} слоёв` : '',
        file.grams ? fmtG(file.grams) : '', storage === 'u-disk' ? 'с флешки' : ''].filter(Boolean).join(' · ')}
    </div>

    <div className="lab"><DIcon n="spool" size={14} />Филаменты → слоты хаба</div>
    {loading ? <div className="hint">Спрашиваю у принтера, какие филаменты в файле…</div> : null}
    {!loading && !tools.length ? <div className="hint">Принтер не сообщил филаменты файла — печать пойдёт по раскладке по умолчанию.</div> : null}
    {!loading && tools.length && !hub ? <div className="hint">Хаб CANVAS не сообщил слоты — печать пойдёт с внешней катушки.</div> : null}
    {hub ? tools.map((c) => {
      const t = c.extruder || 0;
      return <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0' }}>
        <span className="mono" style={{ width: 28, color: 'var(--faint)' }}>T{t}</span>
        <Swatch c={c.hex} size={16} />
        <span style={{ width: 70, fontWeight: 600 }}>{c.type || '—'}</span>
        <span style={{ color: 'var(--faint)' }}>→</span>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {slots.map((s) => <button key={s.slot} className="db sm line"
            title={`${[s.brand, s.name].filter(Boolean).join(' ')} · ${s.type || '—'}`}
            style={map[t] === s.slot ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
            onClick={() => setMap((m) => ({ ...m, [t]: s.slot }))}>
            <Swatch c={s.color} size={12} />{s.slot} · {s.type || 'пусто'}
          </button>)}
        </div>
      </div>;
    }) : null}
    {warnings.map((w) => <div key={w} className="hint" style={{ color: 'var(--amber)' }}>⚠ {w}</div>)}

    <div className="lab" style={{ marginTop: 16 }}><DIcon n="layers" size={14} />Сторона листа</div>
    <Chips value={plate} onChange={setPlate} items={[
      { k: 'A', l: 'A — гладкая' }, { k: 'B', l: 'B — текстурная' }]} />
    <div className="hint" style={{ marginTop: 6 }}>у каждой стороны своя сохранённая сетка стола</div>

    <label style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 14, cursor: 'pointer' }}>
      <input type="checkbox" checked={bedlevel} onChange={(e) => setBedlevel(e.target.checked)} />
      <span>Полная калибровка стола перед печатью</span>
    </label>
    <div className="hint" style={{ marginTop: 4 }}>
      обычно не нужна: сетку принтер снимает сам по g-code. Включайте после смены листа или удара по столу
    </div>
  </Drawer>;
}

// ─── прошивка сообщества: снять деталь на ходу ────────────────────────────────────────────

function objPoints(o) {
  if (o.polygon && o.polygon.length >= 3) return o.polygon;
  const c = o.center || [BED / 2, BED / 2];
  return [[c[0] - 6, c[1] - 6], [c[0] + 6, c[1] - 6], [c[0] + 6, c[1] + 6], [c[0] - 6, c[1] + 6]];
}

export function ExcludeDrawer({ onClose }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const st = useApi('adminCommunity', ['objects'], [], { auto: 3000 });
  const [sel, setSel] = useState('');
  const d = st.data || {};
  const objs = d.objects || [];
  const excluded = new Set(d.excluded || []);
  const pts = (o) => objPoints(o).map(([x, y]) => `${x},${BED - y}`).join(' ');
  const go = () => confirm(`Снять деталь «${sel}»?`,
    'Принтер перестанет её печатать до конца задания, остальные детали продолжатся. Вернуть деталь в это задание нельзя.',
    async () => {
      try {
        await call('adminCommunityOp', 'exclude', { name: sel });
        toast(`Деталь «${sel}» снята с печати`, { kind: 'ok' });
        setSel('');
        st.refresh();
      } catch (e) { toast(e.message, { kind: 'err', ms: 10000 }); }
    }, 'Снять деталь');
  return <Drawer title="Снять деталь" sub="сорвалась одна — остальные допечатаются без остановки" onClose={onClose}
    footer={<ABtn kind="danger full" disabled={!sel || excluded.has(sel)} onClick={go}>
      {sel ? `Снять «${sel}»` : 'Выберите деталь на схеме'}</ABtn>}>
    {st.err ? <Empty ic="alert" t="Детали недоступны" ds={st.err} /> : null}
    {!st.err && !objs.length ? <Empty ic="layers" t={st.loading ? 'Спрашиваю принтер…' : 'Деталей нет'}
      ds="Детали видны, только если слайсер подписал объекты (метки EXCLUDE_OBJECT в g-code)." /> : null}
    {objs.length ? <>
      <svg viewBox={`0 0 ${BED} ${BED}`} style={{ width: '100%', maxHeight: 420, background: 'var(--bg)', borderRadius: 12, border: '1px solid var(--line)' }}>
        {objs.map((o) => {
          const ex = excluded.has(o.name);
          const cur = d.current === o.name;
          const on = sel === o.name;
          return <polygon key={o.name} points={pts(o)} onClick={() => !ex && setSel(o.name)}
            style={{ cursor: ex ? 'default' : 'pointer' }}
            fill={ex ? 'rgba(128,128,128,.18)' : on ? 'rgba(255,91,77,.45)' : cur ? 'rgba(80,200,120,.35)' : 'rgba(120,160,255,.25)'}
            stroke={on ? 'var(--red)' : cur ? 'var(--accent)' : 'var(--line-2)'} strokeWidth={on || cur ? 2 : 1}
            strokeDasharray={ex ? '4 3' : undefined} />;
        })}
        <text x="6" y={BED - 6} fontSize="9" fill="var(--faint)">перед стола</text>
      </svg>
      <div style={{ marginTop: 10 }}>
        {objs.map((o) => {
          const ex = excluded.has(o.name);
          return <div key={o.name} className="qrow" onClick={() => !ex && setSel(o.name)}
            style={{ opacity: ex ? 0.5 : 1, outline: sel === o.name ? '2px solid var(--red)' : undefined }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="nm mono">{o.name}</div>
              <div className="ds">{ex ? 'снята' : d.current === o.name ? 'печатается сейчас' : 'печатается'}</div>
            </div>
          </div>;
        })}
      </div>
    </> : null}
  </Drawer>;
}

// ─── прошивка сообщества: карта стола и винты ─────────────────────────────────────────────

function heat(v, lim) {
  // синий — ниже нуля, белый — ноль, красный — выше; насыщенность по |v|/lim
  const k = Math.max(-1, Math.min(1, v / (lim || 0.1)));
  if (k >= 0) return `rgb(${255},${Math.round(255 - 170 * k)},${Math.round(255 - 200 * k)})`;
  return `rgb(${Math.round(255 + 200 * k)},${Math.round(255 + 120 * k)},255)`;
}

function MeshMap({ prof }) {
  const rows = prof.matrix || [];
  const ny = rows.length; const nx = ny ? rows[0].length : 0;
  const lim = Math.max(Math.abs(prof.lo || 0), Math.abs(prof.hi || 0), 0.05);
  const cell = 100 / Math.max(nx, 1);
  return <svg viewBox={`0 0 100 ${100 * ny / Math.max(nx, 1)}`} style={{ width: '100%', borderRadius: 10, border: '1px solid var(--line)' }}>
    {rows.map((r, j) => r.map((v, i) => {
      const y = (ny - 1 - j) * cell;   // первая строка сетки — перед стола, рисуем её внизу
      return <g key={`${i}-${j}`}>
        <rect x={i * cell} y={y} width={cell} height={cell} fill={v == null ? 'var(--bg)' : heat(v, lim)} />
        {nx <= 11 && v != null ? <text x={i * cell + cell / 2} y={y + cell / 2 + 1.2} fontSize={Math.min(3.2, cell / 3)}
          textAnchor="middle" fill="#111">{v.toFixed(2)}</text> : null}
      </g>;
    }))}
  </svg>;
}

function Mesh3D({ prof }) {
  const host = useRef(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    let alive = true; let cleanup = () => {};
    if (!webglOK()) { setErr('WebGL недоступен — смотрите 2D-карту'); return undefined; }
    ensureThree().then(() => {
      if (!alive || !host.current) return;
      const T = window.THREE;
      const el = host.current;
      const w = el.clientWidth || 480; const h = 340;
      const rows = prof.matrix; const ny = rows.length; const nx = rows[0].length;
      const lim = Math.max(Math.abs(prof.lo || 0), Math.abs(prof.hi || 0), 0.05);
      const scene = new T.Scene();
      const cam = new T.PerspectiveCamera(40, w / h, 0.1, 100);
      cam.position.set(0, -2.2, 1.6);
      const ren = new T.WebGLRenderer({ antialias: true, alpha: true });
      ren.setSize(w, h); ren.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
      el.appendChild(ren.domElement);
      const geo = new T.PlaneGeometry(2, 2, nx - 1, ny - 1);
      const pos = geo.attributes.position; const cols = [];
      const exaggerate = 0.5 / lim;   // сотые доли мм — показываем с преувеличением, иначе плоско
      for (let k = 0; k < pos.count; k++) {
        const i = k % nx; const j = ny - 1 - Math.floor(k / nx);
        const v = rows[j][i] == null ? 0 : rows[j][i];
        pos.setZ(k, v * exaggerate * 0.5);
        const c = new T.Color(heat(v, lim)); cols.push(c.r, c.g, c.b);
      }
      geo.setAttribute('color', new T.Float32BufferAttribute(cols, 3));
      geo.computeVertexNormals();
      const mesh = new T.Mesh(geo, new T.MeshStandardMaterial({ vertexColors: true, side: T.DoubleSide, roughness: 0.8 }));
      scene.add(mesh);
      scene.add(new T.AmbientLight(0xffffff, 0.75));
      const dl = new T.DirectionalLight(0xffffff, 0.6); dl.position.set(1, -1, 2); scene.add(dl);
      const ctl = new T.OrbitControls(cam, ren.domElement); ctl.enableDamping = true;
      let raf = 0;
      const loop = () => { ctl.update(); ren.render(scene, cam); raf = requestAnimationFrame(loop); };
      loop();
      cleanup = () => { cancelAnimationFrame(raf); ctl.dispose(); geo.dispose(); ren.dispose(); if (ren.domElement.parentNode) ren.domElement.parentNode.removeChild(ren.domElement); };
    }).catch((e) => setErr('3D не загрузился: ' + e.message));
    return () => { alive = false; cleanup(); };
  }, [prof]);
  return <div>
    <div ref={host} style={{ width: '100%', height: 340, borderRadius: 10, border: '1px solid var(--line)', overflow: 'hidden' }} />
    {err ? <div className="hint">{err}</div> : <div className="hint">высота преувеличена для наглядности · тяните мышью, колесо — масштаб</div>}
  </div>;
}

const mm = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(3)} мм`);

// Где рисовать винт на схеме стола (вид сверху, перед внизу): передний левый, передний правый,
// задний правый, задний левый — коды из app/cc2community.SCREWS.
const SCREW_XY = { 'ПЛ': [60, 150], 'ПП': [200, 150], 'ЗП': [200, 45], 'ЗЛ': [60, 45] };

function meshVerdict(range) {
  if (range == null) return '';
  if (range <= 0.1) return 'стол ровный';
  if (range <= 0.2) return 'в пределах нормы, сетка компенсирует';
  return 'перекос заметный — стоит выставить винты';
}

/** Ждать конца команды консоли их службы (калибровка, замер). */
async function waitConsole(maxMs = 15 * 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    await new Promise((r) => setTimeout(r, 2500));
    try {
      const c = await call('adminCommunity', 'console');
      if (!c.busy) return c;
    } catch (e) { /* служба на секунду пропала — ждём дальше */ }
  }
  return null;
}

export function BedDrawer({ pr, onClose }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [tab, setTab] = useState('mesh');
  const mesh = useApi('adminCommunity', ['mesh'], []);
  const [prof, setProf] = useState('active');
  const [view, setView] = useState('2d');
  const [busy, setBusy] = useState('');
  const [screws, setScrews] = useState(null);
  const [plan, setPlan] = useState('from_front_left');
  const printing = !!(pr && pr.printing);
  const d = mesh.data || {};
  const profiles = d.profiles || {};
  const cur = prof === 'active' ? d.active : (profiles[prof] || null);

  const calibrate = (side) => confirm(`Калибровать сетку стороны ${side}?`,
    'Принтер нагреет стол до 60 °C и снимет полную сетку 11×11 — это несколько минут. На столе ничего не должно быть.',
    async () => {
      setBusy('cal');
      try {
        await call('adminCommunityOp', 'mesh_calibrate', { side });
        toast(`Калибровка стороны ${side} запущена`, { kind: 'ok' });
        const c = await waitConsole();
        if (c && c.success) { toast('Калибровка завершена', { kind: 'ok' }); mesh.reload(); } else if (c) toast('Калибровка не удалась — смотрите консоль', { kind: 'err', ms: 10000 });
      } catch (e) { toast(e.message, { kind: 'err', ms: 10000 }); } finally { setBusy(''); }
    }, 'Калибровать', false);

  const measure = () => confirm('Замерить винты стола?',
    'Принтер запаркует оси и трижды коснётся стола соплом у каждого из четырёх винтов (новые версии '
    + 'службы перед этим греют стол до 60 °C и чистят сопло). На столе ничего не должно быть.',
    async () => {
      setBusy('screws'); setScrews(null);
      try {
        await call('adminCommunityOp', 'screws_measure');
        const t0 = Date.now();
        for (;;) {
          await new Promise((r) => setTimeout(r, 3000));
          const r = await call('adminCommunity', 'screws');
          if (!r.busy) {
            if (r.plan) setScrews(r.plan); else toast('Замер не дал четырёх точек — смотрите консоль', { kind: 'err', ms: 10000 });
            break;
          }
          if (Date.now() - t0 > 10 * 60000) { toast('Замер идёт слишком долго — смотрите консоль', { kind: 'warn2' }); break; }
        }
      } catch (e) { toast(e.message, { kind: 'err', ms: 10000 }); } finally { setBusy(''); }
    }, 'Замерить', false);

  const profItems = [{ k: 'active', l: 'Активная' }].concat(Object.keys(profiles).map((n) => ({
    k: n, l: profiles[n].side ? `Сторона ${profiles[n].side}` : n })));

  return <Drawer title="Стол" sub="карта стола и винты · прошивка сообщества" onClose={onClose}>
    <Chips value={tab} onChange={setTab} items={[{ k: 'mesh', l: 'Карта стола' }, { k: 'screws', l: 'Винты' }]} />
    {tab === 'mesh' ? <div style={{ marginTop: 12 }}>
      {mesh.err ? <Empty ic="alert" t="Карта недоступна" ds={mesh.err} /> : null}
      {!mesh.err && mesh.loading ? <div className="hint">Читаю сетку из памяти принтера…</div> : null}
      {d.active || Object.keys(profiles).length ? <>
        <Chips value={prof} onChange={setProf} items={profItems} style={{ marginBottom: 10 }} />
        {cur ? <>
          {view === '2d' ? <MeshMap prof={cur} /> : <Mesh3D prof={cur} />}
          <div style={{ display: 'flex', justifyContent: 'space-between', margin: '6px 0' }}>
            <span className="hint">перед стола внизу</span>
            <button className="db sm line" onClick={() => setView(view === '2d' ? '3d' : '2d')}>{view === '2d' ? '3D' : '2D'}</button>
          </div>
          <KV k="Минимум" v={mm(cur.lo)} />
          <KV k="Максимум" v={mm(cur.hi)} />
          <KV k="Перепад" v={`${(cur.range || 0).toFixed(3)} мм`} />
          <div className="hint" style={{ marginTop: 4 }}>{meshVerdict(cur.range)}</div>
        </> : <div className="hint">У этого профиля нет сетки.</div>}
      </> : null}
      <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
        <ABtn kind="line" icon="refresh" onClick={() => mesh.reload()}>Перечитать</ABtn>
        {pr && pr.can('gcode') ? <>
          <ABtn kind="soft" icon="bolt" disabled={printing || !!busy} onClick={() => calibrate('A')}>Калибровать A</ABtn>
          <ABtn kind="soft" icon="bolt" disabled={printing || !!busy} onClick={() => calibrate('B')}>Калибровать B</ABtn>
        </> : null}
      </div>
      {busy === 'cal' ? <div className="hint" style={{ marginTop: 8 }}>Идёт калибровка — окно можно закрыть, она продолжится.</div> : null}
    </div> : <div style={{ marginTop: 12 }}>
      <div className="hint" style={{ marginBottom: 10 }}>
        Тензодатчик сопла касается стола у каждого винта. Это не замена сетке: винты выставляют
        плоскость, сетка компенсирует остальное.
      </div>
      <ABtn kind="soft full" icon="wrench" disabled={printing || !!busy} onClick={measure}>
        {busy === 'screws' ? 'Замер идёт…' : 'Замерить винты'}</ABtn>
      {screws ? <>
        <Chips value={plan} onChange={setPlan} style={{ marginTop: 12 }} items={[
          { k: 'from_front_left', l: `От переднего левого · до ${screws.peak_um} мкм` },
          { k: 'minimal', l: `Минимум кручения · до ${screws.peak_minimal_um} мкм` }]} />
        <svg viewBox="0 0 260 200" style={{ width: '100%', maxWidth: 380, display: 'block', margin: '10px auto 0' }}>
          <rect x="20" y="10" width="220" height="170" rx="10" fill="var(--bg)" stroke="var(--line)" />
          {(screws[plan] || []).map((r) => {
            // угол — по коду винта: координаты замера у разных версий службы могут отличаться
            const [x, y] = SCREW_XY[r.code] || [130, 95];
            const col = r.action === 'ок' ? 'var(--accent)' : r.action === 'опустить' ? '#4aa3ff' : 'var(--red)';
            return <g key={r.code}>
              <circle cx={x} cy={y} r="28" fill="none" stroke={col} strokeWidth="2" />
              <text x={x} y={y - 2} fontSize="9.5" textAnchor="middle" fill="var(--text)">{r.um > 0 ? '+' : ''}{r.um} мкм</text>
              <text x={x} y={y + 11} fontSize="7.5" textAnchor="middle" fill={col}>{r.action === 'ок' ? 'в допуске' : r.action}</text>
            </g>;
          })}
          <text x="130" y="196" fontSize="9" textAnchor="middle" fill="var(--faint)">перед стола</text>
        </svg>
        {(screws[plan] || []).map((r) => <KV key={r.code} k={r.name} mono
          v={r.action === 'ок' ? 'в допуске (±20 мкм)' : `${r.action} на ${Math.abs(r.um)} мкм`} />)}
        <div className="hint" style={{ marginTop: 6 }}>
          перепад между углами {screws.spread_um} мкм · «опустить» — угол выше остальных, стол там ближе к соплу.
          После подкрутки замерьте ещё раз.
        </div>
      </> : null}
    </div>}
  </Drawer>;
}

// ─── прошивка сообщества: тонкая настройка на ходу ────────────────────────────────────────

export function TuneCard({ pr }) {
  const { toast } = useContext(AppCtx);
  const printing = !!(pr && pr.printing);
  const enabled = !!(pr && pr.online && pr.can('live_tune'));
  const t = useApi('adminCommunity', ['tuning'], [], { auto: printing ? 5000 : 0, skip: !enabled });
  const [speed, setSpeed] = useState('');
  const [flow, setFlow] = useState('');
  const [heat, setHeat] = useState(null);       // {nozzle, bed} — правка целей нагрева в печати
  const [busy, setBusy] = useState('');
  if (!enabled) return null;
  // Цели нагрева на ходу: поля заполняются текущими целями принтера, пока мастер их не тронул.
  const hv = heat || { nozzle: Math.round((pr && pr.nozzleTarget) || 0), bed: Math.round((pr && pr.bedTarget) || 0) };
  const setTarget = async () => {
    setBusy('h');
    try {
      await call('adminCommunityOp', 'heat', { nozzle: Number(hv.nozzle), bed: Number(hv.bed) });
      toast(`Цели нагрева: сопло ${hv.nozzle}°, стол ${hv.bed}°`, { kind: 'ok' });
      setHeat(null);
    } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); } finally { setBusy(''); }
  };
  const d = t.data || {};
  const z = Number(d.zoffset || 0);
  const op = async (tag, args, okText) => {
    setBusy(tag);
    try {
      const r = await call('adminCommunityOp', tag === 'z' || tag === 'zu' ? 'zoffset' : 'tune', args);
      toast(okText(r), { kind: 'ok' });
      t.refresh();
    } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); } finally { setBusy(''); }
  };
  const zbtn = (v) => <button key={v} className="db sm line" disabled={!!busy}
    onClick={() => op('z', { delta: v }, (r) => `Z-offset ${v > 0 ? '+' : ''}${v} мм · за сессию ${Number(r.session || 0).toFixed(2)}`)}>
    {v > 0 ? '+' : '−'}{Math.abs(v)}</button>;
  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="gear" size={14} />Тонкая настройка
      <span className="r" style={{ color: 'var(--faint)', fontSize: 11.5 }}>прошивка сообщества</span></div>
    <div className="ctl-stack">
      <span className="ctl-k">Z-offset · <b className="mono">{z >= 0 ? '+' : '−'}{Math.abs(z).toFixed(2)}</b> мм</span>
      <div className="ctl-btns">
        {[-0.05, -0.01, 0.01, 0.05].map(zbtn)}
        {Math.abs(z) > 0.0005 ? <button className="db sm soft" disabled={!!busy}
          onClick={() => op('zu', { undo: true }, () => 'Z-offset сессии откатан')}>Откатить</button> : null}
      </div>
    </div>
    <div className="hint">«−» — сопло ближе к столу. Сдвиг живёт до перезапуска принтера, сохранённые настройки не меняет; за сессию до ±0.5 мм.</div>
    {printing ? <>
      <div className="ctl-stack" style={{ marginTop: 8 }}>
        <span className="ctl-k">Скорость · <b className="mono">{d.speed_percent != null ? Math.round(d.speed_percent) + '%' : '—'}</b></span>
        <div className="ctl-btns">
          <input className="inp" type="number" min="25" max="200" placeholder="%" value={speed} onChange={(e) => setSpeed(e.target.value)} />
          <button className="db sm line" disabled={!!busy || !speed} onClick={() => op('s', { speed: Number(speed) }, () => `Скорость ${speed}%`)}>Задать</button>
          <button className="db sm line" disabled={!!busy} onClick={() => op('s', { speed: 100 }, () => 'Скорость 100%')}>100%</button>
        </div>
      </div>
      <div className="ctl-stack">
        <span className="ctl-k">Поток · <b className="mono">{d.flow_percent != null ? Math.round(d.flow_percent) + '%' : '—'}</b></span>
        <div className="ctl-btns">
          <input className="inp" type="number" min="50" max="150" placeholder="%" value={flow} onChange={(e) => setFlow(e.target.value)} />
          <button className="db sm line" disabled={!!busy || !flow} onClick={() => op('f', { flow: Number(flow) }, () => `Поток ${flow}%`)}>Задать</button>
          <button className="db sm line" disabled={!!busy} onClick={() => op('f', { flow: 100 }, () => 'Поток 100%')}>100%</button>
        </div>
      </div>
      {d.live_velocity != null ? <div className="hint">сейчас голова идёт {Math.round(d.live_velocity)} мм/с · скорость 25…200 %, поток 50…150 %</div> : null}
      <div className="ctl-stack" style={{ marginTop: 8 }}>
        <span className="ctl-k">Нагрев · цель <b className="mono">{Math.round((pr && pr.nozzleTarget) || 0)}° / {Math.round((pr && pr.bedTarget) || 0)}°</b></span>
        <div className="ctl-btns">
          <input className="inp" type="number" min="0" max="300" title="сопло, °C" value={hv.nozzle}
            onChange={(e) => setHeat({ ...hv, nozzle: e.target.value })} />
          <input className="inp" type="number" min="0" max="120" title="стол, °C" value={hv.bed}
            onChange={(e) => setHeat({ ...hv, bed: e.target.value })} />
          <button className="db sm line" disabled={!!busy || !heat} onClick={setTarget}>Задать</button>
        </div>
      </div>
      <div className="hint">сопло 0…300 °C, стол 0…120 °C · печать не останавливается и не ждёт нагрева; следующая команда нагрева из G-code может цель переписать</div>
    </> : null}
  </div>;
}

// ─── прошивка сообщества: консоль G-code ──────────────────────────────────────────────────

export function ConsoleDrawer({ pr, onClose }) {
  const { toast } = useContext(AppCtx);
  const [unlocked, setUnlocked] = useState(false);
  const [cmd, setCmd] = useState('');
  const [hist, setHist] = useState([]);
  const c = useApi('adminCommunity', ['console'], [], { auto: 1500 });
  const out = useRef(null);
  const d = c.data || {};
  useEffect(() => { if (out.current) out.current.scrollTop = out.current.scrollHeight; }, [d.output]);
  const printing = !!(pr && pr.printing);
  const send = async () => {
    const v = cmd.trim();
    if (!v) return;
    try {
      await call('adminCommunityOp', 'console', { command: v });
      setHist((h) => [v].concat(h.filter((x) => x !== v)).slice(0, 12));
      setCmd('');
      c.refresh();
    } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); }
  };
  return <Drawer title="Консоль G-code" sub="прошивка сообщества · только в простое" onClose={onClose}>
    {!unlocked ? <div className="card" style={{ borderColor: 'var(--amber)', marginBottom: 12 }}>
      <div className="nm">Осторожно</div>
      <div className="ds" style={{ margin: '6px 0 10px' }}>
        Ручной G-code двигает оси, греет и меняет калибровку. Принтер проверяет одну команду за раз,
        требует парковки для движения и закрывает консоль на время печати — но отвечать за смысл команды
        будете вы.
      </div>
      <button className="db soft" onClick={() => setUnlocked(true)}>Понимаю, открыть консоль</button>
    </div> : null}
    <pre ref={out} className="mono" style={{ background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 10,
      padding: 10, fontSize: 12, height: 300, overflow: 'auto', whiteSpace: 'pre-wrap', margin: 0 }}>
      {c.err ? c.err : (d.output || 'пусто')}
    </pre>
    <div className="hint" style={{ margin: '6px 0' }}>
      {d.busy ? `выполняется: ${d.command}` : d.command ? `последняя: ${d.command} · ${d.success ? 'успешно' : 'с ошибкой'}` : ''}
    </div>
    <div style={{ display: 'flex', gap: 8 }}>
      <input className="inp mono" disabled={!unlocked || printing} placeholder={printing ? 'идёт печать — консоль закрыта' : 'например: G28'}
        value={cmd} onChange={(e) => setCmd(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') send(); }} list="gcode-hist" />
      <datalist id="gcode-hist">{hist.map((h) => <option key={h} value={h} />)}</datalist>
      <ABtn kind="soft" icon="send" disabled={!unlocked || printing || !cmd.trim()} onClick={send}>Отправить</ABtn>
    </div>
    <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
      <ABtn kind="sm line" disabled={!unlocked || printing} onClick={async () => {
        try { await call('adminCommunityOp', 'motors_off'); toast('Моторы выключены', { kind: 'ok' }); } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Моторы выкл.</ABtn>
      <ABtn kind="sm line" onClick={async () => {
        try { await call('adminCommunityOp', 'console_clear'); c.refresh(); } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Очистить вывод</ABtn>
    </div>
  </Drawer>;
}

/** Ручная экструзия (прошивка сообщества): сопло не холоднее 170 °C, только в простое. */
export function ExtrudeRow({ pr }) {
  const run = useRun();
  const [len, setLen] = useState(10);
  if (!pr || !pr.can('gcode') || !pr.online) return null;
  const cold = !(pr.nozzle >= 170);
  const locked = pr.printing || cold;
  return <div className="ctl-stack" style={{ marginTop: 6 }}>
    <span className="ctl-k" title={cold ? 'нагрейте сопло до 170 °C и выше' : ''}>
      <DIcon n="spool" size={13} />Экструзия{cold ? ' · сопло холодное' : ''}</span>
    <div className="ctl-btns">
      {[5, 10, 25].map((v) => <button key={v} className="db sm line" disabled={locked}
        style={len === v ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined} onClick={() => setLen(v)}>{v}</button>)}
      <button className="db sm soft" disabled={locked} onClick={() => run(`Выдавлено ${len} мм`, () => call('adminCommunityOp', 'extrude', { mm: len }))}>Выдавить</button>
      <button className="db sm line" disabled={locked} onClick={() => run(`Втянуто ${len} мм`, () => call('adminCommunityOp', 'extrude', { mm: -len }))}>Втянуть</button>
    </div>
  </div>;
}

// ─── слоты хаба: автодозаправка и правка слота ────────────────────────────────────────────

export function RefillToggle({ pr }) {
  const run = useRun();
  const [busy, setBusy] = useState(false);
  if (!pr || !pr.can('auto_refill') || !pr.online) return null;
  const on = pr.autoRefill;
  return <label style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 8, cursor: busy ? 'wait' : 'pointer' }}
    title="Кончилась катушка — хаб сам переключится на слот с тем же материалом и цветом">
    <input type="checkbox" checked={!!on} disabled={busy} onChange={async (e) => {
      const next = e.target.checked;
      setBusy(true);
      try { await run(next ? 'Автодозаправка включена' : 'Автодозаправка выключена', () => call('adminPrinterRefill', next)); } finally { setBusy(false); }
    }} />
    <span style={{ fontSize: 13.5 }}>Автодозаправка{on == null ? ' · состояние неизвестно' : ''}</span>
  </label>;
}

const SLOT_TYPES = ['PLA', 'PLA+', 'PLA-CF', 'PETG', 'PETG-CF', 'ABS', 'ASA', 'TPU', 'PA', 'PA-CF', 'PC', 'PVA'];

export function SlotEditDrawer({ slot, onClose }) {
  const { toast } = useContext(AppCtx);
  const [type, setType] = useState((slot.type || 'PLA').toUpperCase());
  const [color, setColor] = useState(/^#[0-9a-f]{6}$/i.test(slot.color || '') ? slot.color : '#FFFFFF');
  const [tmin, setTmin] = useState(slot.temp_min || 190);
  const [tmax, setTmax] = useState(slot.temp_max || 230);
  const save = async () => {
    try {
      await call('adminCommunityOp', 'slot_edit', { slot: slot.slot, type, color, tmin: Number(tmin), tmax: Number(tmax) });
      toast(`Слот ${slot.slot}: ${type}`, { kind: 'ok' });
      onClose();
    } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); }
  };
  return <Drawer title={`Слот ${slot.slot}`} sub="что заправлено — для катушек без метки" onClose={onClose}
    footer={<ABtn kind="full" icon="check" onClick={save}>Записать в хаб</ABtn>}>
    <div className="lab">Материал</div>
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {SLOT_TYPES.map((t) => <button key={t} className="db sm line" onClick={() => setType(t)}
        style={type === t ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}>{t}</button>)}
    </div>
    <input className="inp" style={{ marginTop: 8 }} value={type} maxLength={16} onChange={(e) => setType(e.target.value.toUpperCase())} />
    <div className="lab" style={{ marginTop: 14 }}>Цвет</div>
    <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
      <input type="color" value={color} onChange={(e) => setColor(e.target.value)} style={{ width: 48, height: 36, border: 0, background: 'none' }} />
      <span className="mono">{color.toUpperCase()}</span>
    </div>
    <div className="lab" style={{ marginTop: 14 }}>Температура сопла, °C</div>
    <div style={{ display: 'flex', gap: 8 }}>
      <input className="inp" type="number" min="120" max="320" value={tmin} onChange={(e) => setTmin(e.target.value)} />
      <input className="inp" type="number" min="120" max="320" value={tmax} onChange={(e) => setTmax(e.target.value)} />
    </div>
  </Drawer>;
}

// ─── ресурсы принтера (прошивка сообщества) ───────────────────────────────────────────────

function fmtUptime(s) {
  if (s == null) return '—';
  const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600); const m = Math.floor((s % 3600) / 60);
  return (d ? `${d} д ` : '') + (h ? `${h} ч ` : '') + `${m} мин`;
}

export function CommunityRows({ pr }) {
  const has = !!(pr && pr.community && pr.online);
  const h = useApi('adminCommunity', ['health'], [], { auto: has ? 30000 : 0, skip: !has });
  if (!has) return null;
  const d = h.data || {};
  const mem = d.mem_total_kb && d.mem_available_kb != null
    ? `${Math.round((d.mem_total_kb - d.mem_available_kb) / 1024)} из ${Math.round(d.mem_total_kb / 1024)} МБ` : '—';
  return <>
    <KV k="Прошивка сообщества" v={`${pr.community.firmware || '?'} · CC2 Control ${pr.community.control || '?'}`} />
    {h.data ? <>
      <KV k="Принтер работает" v={fmtUptime(d.uptime_s)} />
      <KV k="Память принтера" v={mem} />
      {d.load1 != null ? <KV k="Загрузка CPU" v={d.load1.toFixed(2)} /> : null}
    </> : null}
  </>;
}

// ─── быстрые действия на «Пульте» ─────────────────────────────────────────────────────────

const QA_KEY = 'pcb_quick_actions';
const QA_DEFAULT = ['heat:0', 'off:heaters', 'home', 'light'];

/**
 * Четыре кнопки на выбор мастера: прогрев по профилю, выключить нагрев, парковка, свет.
 * Выбор хранится в браузере — это удобство рабочего места, а не настройка мастерской.
 */
export function QuickActions({ pr }) {
  const { toast } = useContext(AppCtx);
  const run = useRun();
  const presets = useHeatPresets();
  const list = (presets.data && presets.data.presets) || [];
  const [edit, setEdit] = useState(false);
  const [keys, setKeys] = useState(() => {
    try { const v = JSON.parse(safeLS.get(QA_KEY, '')); return Array.isArray(v) && v.length ? v.slice(0, 4) : QA_DEFAULT; } catch (e) { return QA_DEFAULT; }
  });
  const catalog = useMemo(() => {
    const out = list.map((p, i) => ({ k: `heat:${i}`, l: `Прогрев ${p.name}`, ic: 'flame',
      fn: async () => { await call('adminPrinterHeat', 'extruder', p.nozzle); await call('adminPrinterHeat', 'heater_bed', p.bed); return {}; },
      ok: `Прогрев ${p.name}: ${p.nozzle}/${p.bed}°`, idleOnly: true }));
    return out.concat([
      { k: 'off:heaters', l: 'Выкл. нагрев', ic: 'flame', fn: () => call('adminPrinterOff', 'heaters'), ok: 'Нагрев выключен' },
      { k: 'off:all', l: 'Всё выключить', ic: 'stop', fn: () => call('adminPrinterOff', 'all'), ok: 'Нагрев и обдув выключены', idleOnly: true },
      { k: 'home', l: 'Парковка', ic: 'grid', fn: () => call('adminPrinterHome', 'xyz'), ok: 'Парковка осей', idleOnly: true },
      { k: 'light', l: 'Свет', ic: 'sun', fn: () => call('adminPrinterControl', 'light', !(pr && pr.light)), ok: 'Свет переключён' },
    ]);
  }, [list, pr && pr.light]);   // eslint-disable-line
  if (!pr || !pr.online || !pr.can('move')) return null;
  const shown = keys.map((k) => catalog.find((c) => c.k === k)).filter(Boolean);
  const toggle = (k) => setKeys((ks) => {
    const next = ks.includes(k) ? ks.filter((x) => x !== k) : ks.concat([k]).slice(-4);
    safeLS.set(QA_KEY, JSON.stringify(next));
    return next;
  });
  return <>
    <div className="lab" style={{ marginTop: 16 }}><DIcon n="bolt" size={14} />Быстрые действия
      <span className="r"><button className="db sm line" onClick={() => setEdit(!edit)}>{edit ? 'Готово' : 'Настроить'}</button></span></div>
    {edit ? <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {catalog.map((c) => <button key={c.k} className="db sm line" onClick={() => toggle(c.k)}
        style={keys.includes(c.k) ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}>{c.l}</button>)}
      <div className="hint" style={{ width: '100%' }}>до четырёх кнопок · хранится в этом браузере</div>
    </div> : <div className="pctl wrap tight">
      {shown.map((c) => <button key={c.k} className="db sm soft"
        disabled={c.idleOnly && pr.printing} title={c.idleOnly && pr.printing ? 'недоступно во время печати' : ''}
        onClick={() => run(c.ok, c.fn)}><DIcon n={c.ic} size={14} />{c.l}</button>)}
      {!shown.length ? <div className="hint">Нажмите «Настроить» и выберите кнопки.</div> : null}
    </div>}
  </>;
}
