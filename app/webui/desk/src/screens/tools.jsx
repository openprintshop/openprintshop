// tools.jsx — «Инструменты»: рабочие инструменты мастера у станка. Первый — допечатка со слоя.
//
// «Допечатка со слоя» (Путь В, app/resumecut.py + app/web/tools.py, план W1–W9 в
// docs/Перспективные разработки/resume-tool-3d-plan-2026-09-05.md): одна из деталей на столе
// сорвалась на N-м слое, остальные стоят. Из g-code ТОЙ ЖЕ печати собирается файл, который
// начинается сразу с N-го слоя, а сорвавшиеся детали вырезаны на всех слоях.
//
// Четыре шага, между ними можно ходить назад: источник (архив заданий находит текущую печать сам)
// → детали и слой (3D-сцена: клик по детали, слой подставлен из телеметрии паузы) → файл
// (сборка со сверкой, ход по процентам, 3D того, что допечатается, стартовые переезды) → старт
// (гейты: принтер свободен, стол тёплый, файл на принтере, центр стола чист). Ни один шаг не
// делает следующий сам: между заливкой и стартом мастер снимает сорвавшиеся детали и мусор.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty, Field } from '../ui.jsx';
import { API, call, useApi } from '../api.jsx';
import { ABtn, Loading, useConfirm } from '../parts.jsx';
import { GcodeViewer3D } from '../gcode3d.jsx';

const { useState, useEffect, useContext, useMemo, useRef } = React;

const fmtMin = (m) => {
  const v = Math.max(0, Math.round(Number(m) || 0));
  return v >= 60 ? `${Math.floor(v / 60)} ч ${v % 60} мин` : `${v} мин`;
};
const fmtAge = (min) => (min < 60 ? `${min} мин назад` : min < 48 * 60 ? `${Math.round(min / 60)} ч назад` : `${Math.round(min / 1440)} дн назад`);
const fmtMB = (b) => `${(Number(b || 0) / 1e6).toFixed(1)} МБ`;
const fmtTs = (ts) => (ts ? new Date(ts * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '');
const KIND = { archive: 'архив', slicer: 'слайсер', upload: 'загружено', resume: 'допечатка', bot: 'бот' };

export function ToolsScreen() {
  return <div className="onecol" style={{ maxWidth: 1500, width: '100%', margin: '0 auto', gap: 20, overflowY: 'auto' }}>
    <ResumeTool />
    <HistoryCard />
  </div>;
}

/** История допечаток — из ленты активности: что собирали, заливали и запускали, кто и когда. */
function HistoryCard() {
  const h = useApi('adminToolsResumeHistory', [], [], { auto: 60000 });
  const items = (h.data && h.data.items) || [];
  return <div className="card" style={{ gap: 8 }}>
    <div className="nm"><DIcon n="clock" size={16} /> История допечаток</div>
    {h.loading ? <div className="hint">Загружаю…</div> : null}
    {!h.loading && !items.length ? <div className="hint">Пока пусто: здесь появятся сборки, заливки и старты.</div> : null}
    {items.map((e, i) => <div key={i} className="ds" style={{ display: 'flex', gap: 10 }}>
      <span className="mono" style={{ flex: '0 0 96px', opacity: 0.7 }}>{fmtTs(e.ts)}</span>
      <span style={{ flex: 1 }}>{e.detail}{e.user_name ? <span className="hint" style={{ marginLeft: 6 }}>· {e.user_name}</span> : null}</span>
    </div>)}
  </div>;
}

const STEPS = ['Источник', 'Детали и слой', 'Файл', 'Старт'];

function Stepper({ step, onGo }) {
  return <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
    {STEPS.map((t, i) => <React.Fragment key={t}>
      <button type="button" className={'chip' + (i === step ? ' on' : '')} disabled={i > step}
        onClick={() => (i < step ? onGo(i) : null)}
        style={{ opacity: i > step ? 0.45 : 1, cursor: i < step ? 'pointer' : 'default' }}>
        <span className="mono" style={{ marginRight: 6 }}>{i + 1}</span>{t}
      </button>
      {i < STEPS.length - 1 ? <DIcon n="chev" size={12} /> : null}
    </React.Fragment>)}
  </div>;
}

/** Состояние инструмента: source → an → job (сборка) → uploaded/started. Назад — по шагам. */
function ResumeTool() {
  const { params } = useContext(AppCtx);
  const [src, setSrc] = useState(null);        // {path, name}
  const [an, setAn] = useState(null);          // ответ analyze
  const [job, setJob] = useState(null);        // ответ job (done)
  const [step, setStep] = useState(0);
  const goBack = (i) => {
    setStep(i);
    if (i <= 0) { setSrc(null); setAn(null); setJob(null); }
    if (i <= 1) { setJob(null); }
  };
  return <div className="card" style={{ gap: 14 }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <DIcon n="layers" size={20} />
      <div style={{ flex: 1 }}>
        <div className="nm">Допечатка со слоя</div>
        <div className="ds">одна деталь сорвалась, остальные стоят: продолжить печать с N-го слоя без неё —
          из g-code той же печати, поверх уже напечатанного</div>
      </div>
    </div>
    <Stepper step={step} onGo={goBack} />
    {step === 0 ? <SourcePicker autoCurrent={params && params.src === 'current'}
      onPick={(s) => { setSrc(s); setAn(null); setJob(null); setStep(1); }} /> : null}
    {step === 1 && src && !an ? <Analyzing src={src} onDone={setAn} onFail={() => goBack(0)} /> : null}
    {step === 1 && src && an ? <ChooseStep src={src} an={an} onBuilt={(j) => { setJob(j); setStep(2); }} /> : null}
    {step >= 2 && job ? <ResultStep src={src} an={an} job={job} setJob={setJob} step={step} setStep={setStep} /> : null}
  </div>;
}

/** Источник: текущая печать (из архива по имени задания), архив, временные файлы слайсера, файл с диска. */
function SourcePicker({ onPick, autoCurrent }) {
  const { toast } = useContext(AppCtx);
  const s = useApi('adminToolsResumeSources', [0], []);
  const [busy, setBusy] = useState(false);
  const list = (s.data && s.data.sources) || [];
  const current = s.data && s.data.current;
  const picked = useRef(false);
  useEffect(() => {
    if (autoCurrent && current && current.path && !picked.current) {
      picked.current = true;
      onPick({ path: current.path, name: current.name || current.job_name });
    }
  }, [current, autoCurrent]);   // eslint-disable-line
  const upload = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    setBusy(true);
    try {
      const r = await call('adminToolsResumeUpload', f);
      onPick({ path: r.path, name: f.name });
    } catch (err) { toast(String(err.message || err), { kind: 'err' }); } finally { setBusy(false); }
  };
  return <>
    {current && current.path ? <div className="card" style={{ borderColor: 'var(--accent)', padding: 12, cursor: 'pointer' }}
      onClick={() => onPick({ path: current.path, name: current.name || current.job_name })}>
      <div className="nm"><DIcon n="printer" size={16} /> Сейчас на принтере: {current.name || current.job_name}</div>
      <div className="ds">файл найден в архиве заданий — нажмите, чтобы взять его</div>
    </div> : null}
    {current && !current.path ? <div className="hint">На принтере задание «{current.job_name}», но в архиве его нет:
      выберите файл ниже или приложите вручную.</div> : null}
    <div className="hint">Нужен g-code <b>той самой печати</b>, что стоит на столе. Всё, что слайсер и бот
      отправляли на принтер, лежит в архиве заданий; свежие файлы слайсера подхватываются сами.</div>
    {s.loading ? <Loading what="Ищу файлы заданий…" /> : null}
    {s.err ? <Empty ic="alert" t="Список не получить" ds={s.err} /> : null}
    {!s.loading && !s.err && !list.length
      ? <Empty ic="search" t="Заданий пока нет" ds="Приложите g-code вручную" /> : null}
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 10 }}>
      {list.map((f) => <div key={f.path} className="qrow" style={{ cursor: 'pointer', alignItems: 'center' }}
        onClick={() => onPick({ path: f.path, name: f.name })}>
        {f.thumb ? <img src={`data:image/png;base64,${f.thumb}`} width={56} height={56} alt=""
          style={{ borderRadius: 8, border: '1px solid var(--line)', flex: '0 0 auto' }} />
          : <DIcon n="doc" size={28} />}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="nm" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name}</div>
          <div className="ds">{KIND[f.kind] || f.kind} · {fmtAge(f.age_min)} · {f.layers ? `${f.layers} сл.` : ''}
            {f.minutes ? ` · ${fmtMin(f.minutes)}` : ''} · {fmtMB(f.size)}</div>
        </div>
      </div>)}
    </div>
    <label className="hint" style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer' }}>
      <DIcon n="upload" size={16} /> {busy ? 'Загружаю…' : 'Приложить .gcode с диска'}
      <input type="file" accept=".gcode" style={{ display: 'none' }} disabled={busy} onChange={upload} />
    </label>
  </>;
}

function Analyzing({ src, onDone, onFail }) {
  const { toast } = useContext(AppCtx);
  useEffect(() => {
    let alive = true;
    call('adminToolsResumeAnalyze', src.path, 0)
      .then((r) => { if (alive) onDone(r); })
      .catch((e) => { toast(String(e.message || e), { kind: 'err', ms: 9000 }); if (alive) onFail(); });
    return () => { alive = false; };
  }, [src.path]);   // eslint-disable-line
  return <Loading what={`Разбираю ${src.name}: детали, слои…`} />;
}

/** Плоская карта стола — запасной вид без WebGL и для маленьких экранов. */
export function PlateMap({ an, sel, onToggle, size = 420 }) {
  const [bx, by] = an.bed || [256, 256];
  const k = size / Math.max(bx, by);
  return <svg width={bx * k} height={by * k} viewBox={`0 0 ${bx} ${by}`}
    style={{ background: 'var(--bg2, #f4f4f4)', border: '1px solid var(--line)', borderRadius: 10, flex: '0 0 auto' }}>
    {[...Array(Math.floor(bx / 32) + 1)].map((_, i) => <line key={'x' + i} x1={i * 32} y1={0} x2={i * 32} y2={by} stroke="#0002" strokeWidth={0.5} />)}
    {[...Array(Math.floor(by / 32) + 1)].map((_, i) => <line key={'y' + i} x1={0} y1={by - i * 32} x2={bx} y2={by - i * 32} stroke="#0002" strokeWidth={0.5} />)}
    <circle cx={bx / 2} cy={by / 2} r={2.5} fill="none" stroke="#0006" strokeWidth={0.8} />
    {an.objects.map((o) => {
      const [x0, y0, x1, y1] = o.bbox_all && o.bbox_all[2] > o.bbox_all[0] ? o.bbox_all : o.bbox;
      const off = sel.includes(o.id);
      return <g key={o.id} style={{ cursor: 'pointer' }} onClick={() => onToggle(o.id)}>
        <rect x={x0} y={by - y1} width={x1 - x0} height={y1 - y0} rx={2}
          fill={off ? 'rgba(220,40,40,.28)' : 'rgba(40,120,220,.18)'}
          stroke={off ? 'var(--danger, #d33)' : 'var(--accent, #48c)'} strokeWidth={off ? 1.6 : 0.9} />
        <text x={(x0 + x1) / 2} y={by - (y0 + y1) / 2 + 3} textAnchor="middle" fontSize={7} fontWeight={700}
          fill={off ? 'var(--danger, #d33)' : '#222'}>{off ? '✕ ' : ''}{o.name}</text>
      </g>;
    })}
    <text x={4} y={by - 4} fontSize={6} fill="#0008">перед стола</text>
  </svg>;
}

/** Шаг 2: отметить сорвавшиеся детали (в 3D или списком) и слой, на котором остановлена печать. */
function ChooseStep({ src, an, onBuilt }) {
  const { toast, printer } = useContext(AppCtx);
  const pr = printer || {};
  const [sel, setSel] = useState([]);
  const sug = an.suggest && an.suggest.layer;
  const [layer, setLayer] = useState(() => (sug && sug >= 2 ? sug : (pr.layer && pr.layer > 1 ? pr.layer : '')));
  const [busy, setBusy] = useState(false);
  const toggle = (id) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : s.concat([id])));
  const n = Number(layer) || 0;
  const zAt = (i) => (an.layer_z && an.layer_z[i] != null ? an.layer_z[i] : null);
  const minL = (an.first_layer || 1) + 1;
  const layerOk = n >= minL && n <= an.layers;
  const es = an.objects.map((o) => o.e_mm).sort((a, b) => a - b);
  const med = es.length ? es[Math.floor(es.length / 2)] : 0;
  const kept = an.objects.length - sel.length;
  const pathsUrl = useMemo(() => (API.adminToolsResumePathsUrl ? API.adminToolsResumePathsUrl({ path: src.path }) : ''), [src.path]);
  const go = async () => {
    setBusy(true);
    try {
      const expect = {};
      sel.forEach((id) => { const o = an.objects.find((x) => x.id === id); if (o) expect[id] = [o.cx, o.cy]; });
      const r = await call('adminToolsResumeBuild', src.path, n, sel, expect, src.name);
      onBuilt({ ...r, layer: n, exclude: sel.slice() });
    } catch (e) { toast(String(e.message || e), { kind: 'err', ms: 12000 }); setBusy(false); }
  };
  return <>
    <div className="hint"><span className="mono">{src.name}</span> · {an.slicer || 'слайсер ?'} ·{' '}
      {an.printer_model || 'принтер ?'} · {an.layers} сл.{an.minutes_total ? ` · ${fmtMin(an.minutes_total)}` : ''} ·{' '}
      детали {an.labeled ? 'по меткам слайсера' : 'по первому слою (эвристика)'}</div>
    {!an.supported ? <Empty ic="alert" t="Этот файл допечатать нельзя" ds={an.reason} /> : null}
    {an.supported && !an.separable
      ? <Empty ic="alert" t="Детали не разделяются"
        ds={`${an.cross} ходов экструзии пересекают границы между деталями. Включите метки объектов в профиле слайсера (gcode_label_objects) — тогда вырез точный`} /> : null}
    {an.warnings.map((w, i) => <div key={i} className="card" style={{ borderColor: 'var(--warn, #d90)', padding: 10 }}>
      <div className="ds">⚠ {w}</div></div>)}
    <GcodeViewer3D url={pathsUrl} layer={layerOk ? n : 0} excluded={sel} onToggle={toggle} height={460}
      fallback={<PlateMap an={an} sel={sel} onToggle={toggle} />} />
    <div style={{ display: 'flex', gap: 18, alignItems: 'flex-start', flexWrap: 'wrap' }}>
      <div style={{ flex: 1, minWidth: 300, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div className="hint">Отметьте <b>сорвавшиеся</b> детали — кликом по 3D или в списке. Остальные
          допечатаются как есть.</div>
        {an.objects.map((o) => {
          const off = sel.includes(o.id);
          const ratio = med ? o.e_mm / med : 1;
          const bb = o.bbox_all || o.bbox;
          return <div key={o.id} className="qrow" onClick={() => toggle(o.id)}
            style={off ? { outline: '2px solid var(--danger)' } : null}>
            <input type="checkbox" checked={off} readOnly />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="nm">{o.name} <span className="mono" style={{ fontWeight: 400 }}>({o.cx}, {o.cy})</span>
                {ratio >= 1.7 ? <span className="stb red" style={{ marginLeft: 8 }}>×{ratio.toFixed(1)} пластика — две копии?</span> : null}</div>
              <div className="ds">{Math.round(bb[2] - bb[0])} × {Math.round(bb[3] - bb[1])} × {o.zmax} мм · {Math.round(o.e_mm / 1000 * 10) / 10} м прутка</div>
            </div>
          </div>;
        })}
      </div>
      <div style={{ flex: '0 0 340px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <Field l="С какого слоя продолжать">
          <input type="number" min={minL} max={an.layers} value={layer} placeholder={`${minL}…${an.layers}`}
            onChange={(e) => setLayer(e.target.value)} style={{ width: 120 }} />
        </Field>
        {an.suggest && an.suggest.layer ? <div className="hint">
          Принтер {an.suggest.phase === 'paused' ? 'встал на паузу' : `перешёл в «${an.suggest.phase}»`} на слое{' '}
          <b>{an.suggest.layer}</b>{an.suggest.ts ? ` (${fmtTs(an.suggest.ts)})` : ''}
          {an.suggest.job_name ? <>, задание <span className="mono">{an.suggest.job_name}</span></> : null} — подставлено.
        </div> : null}
        <div className="hint">
          Номер слоя, на котором нажали паузу (он же на экране принтера). Слои до него уже напечатаны.
          {layerOk ? <> Слой {n}: Z {zAt(n)} мм, детали на столе <b>{zAt(n - 1)} мм</b> — можно сверить штангенциркулем.</> : null}
          {pr.layer ? <> Принтер сейчас показывает слой {pr.layer}.</> : null}
        </div>
        <ABtn kind="good" disabled={busy || !an.supported || !an.separable || !sel.length || kept < 1 || !layerOk}
          onClick={go} style={{ width: '100%' }}>
          {busy ? 'Запускаю сборку…' : sel.length ? `Собрать файл: со слоя ${n || '?'}, без ${sel.length} дет., останется ${kept}` : 'Отметьте сорвавшиеся детали'}
        </ABtn>
      </div>
    </div>
  </>;
}

/** Поллинг задания сборки: фаза и проценты, по готовности — отчёт. */
function useJob(jobId, onDone) {
  const [st, setSt] = useState({ status: 'running', phase: 'queue', pct: 0 });
  useEffect(() => {
    if (!jobId) return undefined;
    let alive = true, timer = null;
    const tick = async () => {
      try {
        const r = await call('adminToolsResumeJob', jobId);
        if (!alive) return;
        setSt(r);
        if (r.status === 'done') { onDone(r); return; }
      } catch (e) {
        if (!alive) return;
        setSt({ status: 'failed', error: String(e.message || e) });
        return;
      }
      timer = setTimeout(tick, 900);
    };
    tick();
    return () => { alive = false; clearTimeout(timer); };
  }, [jobId]);   // eslint-disable-line
  return st;
}

const PHASES = { queue: 'в очереди', load: 'читаю файл', objects: 'ищу детали', analyze: 'разбираю ходы',
  build: 'собираю файл', check: 'самопроверка', verify: 'сверяю с исходником', previews: 'рисую превью', done: 'готово' };

/** Шаги 3–4: отчёт, 3D того, что допечатается, скачать/залить, гейты и старт. */
function ResultStep({ src, an, job, setJob, step, setStep }) {
  const { toast, printer } = useContext(AppCtx);
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [gates, setGates] = useState(null);
  const [force, setForce] = useState(false);
  const done = job.status === 'done';
  const prog = useJob(done ? '' : job.job, (r) => setJob({ ...job, ...r }));
  const rep = job.report || {};
  const ver = job.verify || {};
  const dl = useMemo(() => (done && API.adminToolsResumeDownloadUrl ? API.adminToolsResumeDownloadUrl(job.job) : ''), [job.job, done]);
  const pathsUrl = useMemo(() => (done && API.adminToolsResumePathsUrl ? API.adminToolsResumePathsUrl({ job: job.job }) : ''), [job.job, done]);
  const refreshGates = async () => {
    try { const g = await call('adminToolsResumeGates', job.job, 0, force); setGates(g); } catch (e) { setGates({ gates: [{ gate: 'net', hard: true, text: String(e.message || e) }], can_start: false }); }
  };
  useEffect(() => { if (done && step === 3) refreshGates(); }, [done, step, force, job.uploaded]);   // eslint-disable-line
  if (!done) {
    if (prog.status === 'failed') {
      return <Empty ic="alert" t="Сборка не удалась" ds={prog.error || job.error}
        cta="Назад к деталям" onCta={() => setStep(1)} />;
    }
    const pct = Math.round((prog.pct || 0) * 100);
    return <div className="card" style={{ padding: 12 }}>
      <div className="nm">Собираю файл…</div>
      <div className="ds" style={{ marginTop: 6 }}>{PHASES[prog.phase] || prog.phase} · {pct}%</div>
      <div style={{ height: 8, background: 'var(--line)', borderRadius: 4, marginTop: 8, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: 'var(--accent)', transition: 'width .4s' }} />
      </div>
    </div>;
  }
  const send = () => confirm(
    'Залить файл на принтер?',
    `«${job.filename}» уйдёт в память принтера. Печать НЕ начнётся: старт — отдельным шагом, когда `
    + 'сорвавшиеся детали и мусор сняты со стола. Принтер должен быть свободен (текущую печать — «Стоп»).',
    async () => {
      setBusy(true);
      try {
        const r = await call('adminToolsResumeSend', job.job, 0);
        setJob({ ...job, uploaded: r.uploaded });
        setGates({ gates: r.gates || [], can_start: !(r.gates || []).length });
        toast(`Залито «${r.uploaded}» — снимите сорвавшиеся детали и включите стол`, { kind: 'ok' });
        setStep(3);
      } catch (e) { toast(String(e.message || e), { kind: 'err', ms: 12000 }); } finally { setBusy(false); }
    },
    'Залить', false,
  );
  const heat = async (t) => {
    try { await call('adminPrinterHeat', 'bed', Math.round(t)); toast(`Стол греется до ${Math.round(t)}°`, { kind: 'ok' }); setTimeout(refreshGates, 1500); }
    catch (e) { toast(String(e.message || e), { kind: 'err' }); }
  };
  const start = () => confirm(
    'Запустить допечатку?',
    `«${job.uploaded}»: со слоя ${rep.layer} (Z ${rep.z_layer}), ${rep.kept_objects} дет., ≈${fmtMin(rep.minutes_remaining)}. `
    + 'Убедитесь: вырезанные детали и мусор сняты, остальные не сдвинуты. Калибровки стола не будет — сопло пойдёт по старой сетке.',
    async () => {
      setBusy(true);
      try {
        await call('adminToolsResumeStart', job.job, 0, { forceCentre: force, ackSoft: true });
        toast('Допечатка запущена — смотрите первые слои', { kind: 'ok' });
        setJob({ ...job, started: true });
      } catch (e) { toast(String(e.message || e), { kind: 'err', ms: 12000 }); refreshGates(); } finally { setBusy(false); }
    },
    'Стол готов, запустить', true,
  );
  const excludedNames = (rep.excluded_names || []).join(', ');
  return <>
    <div className="card" style={{ borderColor: 'var(--ok, #2a7)', padding: 12 }}>
      <div className="nm"><DIcon n="check" size={16} /> Файл собран и сверен: <span className="mono">{job.filename}</span></div>
      <div className="ds" style={{ marginTop: 6 }}>
        Со слоя {rep.layer} (Z {rep.z_layer} мм, детали на столе {rep.parts_height} мм) · вырезано {excludedNames} ·
        останется {rep.kept_objects} дет. · ≈{fmtMin(rep.minutes_remaining)} · {fmtMB(rep.size_bytes)}.
        Сверка с исходником: {ver.out_count} ходов экструзии совпали побуквенно, нарушений Z {ver.z_violations}.
        Сопло {rep.nozzle}°, стол {rep.bed}°{rep.first_object_name ? `, первой печатается «${rep.first_object_name}»` : ''}.
        Детали {rep.mode === 'labels' ? 'по меткам слайсера' : 'по эвристике'}, стартовый блок «{rep.style}».
      </div>
      {(rep.warnings || []).map((w, i) => <div key={i} className="ds" style={{ marginTop: 6, color: 'var(--warn, #d90)' }}>⚠ {w}</div>)}
    </div>
    <GcodeViewer3D url={pathsUrl} layer={0} excluded={[]} startMoves={rep.start_moves} mode="all" height={420}
      fallback={job.previews && job.previews.length ? <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        {job.previews.map((p) => <div key={p.layer} style={{ textAlign: 'center' }}>
          <img src={`data:image/png;base64,${p.png}`} alt="" width={258} height={258}
            style={{ border: '1px solid var(--line)', borderRadius: 8, background: '#fff' }} />
          <div className="hint">слой {p.layer} · Z {p.z}</div>
        </div>)}
      </div> : null} />
    {step === 2 ? <>
      <div className="card" style={{ padding: 12 }}>
        <div className="nm">У станка</div>
        <ol className="ds" style={{ margin: '6px 0 0 18px', padding: 0, lineHeight: 1.6 }}>
          <li>«Стоп» на принтере, если печать ещё на паузе. Стоп выключает нагрев — <b>сразу включите стол</b>{' '}
            на {rep.bed}°: остывший PLA отскакивает от текстурированной плиты.</li>
          <li>Снимите сорвавшиеся детали ({excludedNames}) и весь мусор с их места. Остальные не трогайте.</li>
          <li>Файл на флешку и старт с принтера, либо «Залить» и «Запустить» отсюда.</li>
          <li>Смотрите первые два-три слоя: пруток должен ложиться на детали, а не в воздух.</li>
        </ol>
      </div>
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        {dl ? <a className="db line" href={dl} download={job.filename}><DIcon n="doc" size={15} /> Скачать g-code</a> : null}
        {!job.uploaded
          ? <ABtn kind="good" disabled={busy} onClick={send} icon="upload">Залить на принтер</ABtn>
          : <ABtn kind="good" onClick={() => setStep(3)} icon="play">К старту</ABtn>}
        <ABtn disabled={busy} onClick={() => setStep(1)}>Другие детали или слой</ABtn>
      </div>
    </> : null}
    {step === 3 ? <div className="card" style={{ padding: 12 }}>
      <div className="nm">Перед стартом</div>
      {!gates ? <Loading what="Проверяю принтер…" /> : null}
      {gates && !gates.gates.length ? <div className="ds" style={{ marginTop: 6, color: 'var(--ok, #2a7)' }}>
        ✓ Принтер свободен, стол тёплый, файл «{job.uploaded}» на принтере, стартовые переезды чистые.</div> : null}
      {gates ? gates.gates.map((g) => <div key={g.gate} className="ds" style={{ marginTop: 6, color: g.hard ? 'var(--danger)' : 'var(--warn, #d90)', display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <span>{g.hard ? '✕' : '⚠'} {g.text}{g.hint ? ` — ${g.hint}` : ''}</span>
        {g.gate === 'bed_cold' ? <ABtn kind="soft" onClick={() => heat(g.bed_target)} icon="flame">Включить стол {Math.round(g.bed_target)}°</ABtn> : null}
        {g.gate === 'centre' ? <label className="hint" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> деталь в центре снята или беру риск на себя</label> : null}
      </div>) : null}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
        <ABtn kind="soft" onClick={refreshGates} icon="refresh">Проверить снова</ABtn>
        {job.started
          ? <span className="stb green">Запущено</span>
          : <ABtn kind="danger" disabled={busy || !gates || gates.gates.some((g) => g.hard) || (!force && gates.gates.some((g) => g.gate === 'centre'))}
            onClick={start} icon="play">Стол готов — запустить допечатку</ABtn>}
        <ABtn disabled={busy} onClick={() => setStep(2)}>Назад к файлу</ABtn>
      </div>
    </div> : null}
  </>;
}
