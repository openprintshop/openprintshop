// calib.jsx — «Калибровка»: накопленный опыт по настройке филамента.
//
// Шесть вкладок под шесть разных вопросов:
//   Кампании   — что делали и что получили (шаги в обязательном порядке, печати с фото)
//   Паспорта   — восемь чисел на связку пластик × принтер × сопло, в БД, а не только в слайсере
//   Пресеты    — история правок пользовательских пресетов + восстановление после сброса
//   Образцы    — полка физических башен и кубов, этикетки с QR
//   Диагностика — правила «симптом → причина → что делать», выработанные по ходу калибровок
//   Дрейф      — уползают ли размеры и расходится ли оценка с фактом
//
// Данные экрана приходят одним запросом adminCalib (реестр тестов и правила — оттуда же:
// фронт не должен дублировать доменное знание из app/calibration.py). Кампания открывается
// вторым запросом — она тяжелее и нужна только на выбранной.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Empty, Field, Spark, Drawer } from '../ui.jsx';
import { call, API, useApi } from '../api.jsx';
import { num, dt, dateOnly } from '../fmt.jsx';
import { Screen, ABtn, Chips, Split, PickOne, readImage } from '../parts.jsx';

const { useState, useContext } = React;

const STEP_ST = {
  todo: ['gray', 'не начат'], printing: ['amber', 'печатается'],
  measured: ['violet', 'замерен'], done: ['green', 'готов'], skipped: ['gray', 'пропущен'],
};
const RUN_ST = { active: ['amber', 'идёт'], done: ['green', 'завершена'], archived: ['gray', 'архив'] };

const PLATE_KINDS = [
  ['textured', 'Текстурированная PEI'], ['smooth', 'Гладкая PEI'],
  ['cool', 'Холодная (Cool Plate)'], ['high', 'Высокотемпературная'], ['other', 'Другая'],
];

function Badge({ map, k }) {
  const [tone, label] = map[k] || ['gray', k || '—'];
  return <span className={'stb ' + tone}>{label}</span>;
}

// Z-offset пишется со знаком: −0.07 значит «сопло ближе к столу». Без явного плюса/минуса
// число читается неоднозначно, поэтому знак рисуем всегда.
function zfmt(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (!isFinite(n)) return '—';
  return (n > 0 ? '+' : '') + n.toFixed(3).replace(/0+$/, '').replace(/\.$/, '') + ' мм';
}

// Сопло выбирается из СВОЕГО реестра (f71f4973), но остаётся текстовым полем: у мастера
// может стоять сопло, которого в реестре ещё нет, и запретить его ввод было бы вредно.
function NozzleInput({ value, onChange, data }) {
  const list = data.nozzles || [];
  return <>
    <input className="inp" list="calib-nozzles" value={value} onChange={onChange}
      placeholder="0.4" />
    <datalist id="calib-nozzles">{list.map((n) => <option key={n} value={n} />)}</datalist>
  </>;
}

function openFile(url) {
  if (!url) return;
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
  else window.open(url, '_blank');
}

export function CalibScreen() {
  const [tab, setTab] = useState('runs');
  const d = useApi('adminCalib', [], []);
  const data = d.data || {};
  const st = data.stats || {};

  return <div className="onecol">
    <Chips value={tab} onChange={setTab} style={{ marginBottom: 16 }} items={[
      { k: 'runs', l: 'Кампании', ic: 'flame', n: st.runs || 0 },
      { k: 'passports', l: 'Паспорта', ic: 'shield', n: st.passports || 0 },
      { k: 'plates', l: 'Пластины', ic: 'box', n: (data.plates || []).length },
      { k: 'presets', l: 'Пресеты', ic: 'layers', n: st.presets || 0 },
      { k: 'samples', l: 'Образцы', ic: 'box', n: st.samples || 0 },
      { k: 'diag', l: 'Диагностика', ic: 'search' },
      { k: 'drift', l: 'Дрейф', ic: 'pulse' },
    ]} />
    <Screen state={d}>{() => {
      // .page у оболочки — overflow:hidden, прокрутку каждый экран заводит себе сам.
      // «Кампании» этого не требуют: Split держит свою высоту и скроллит список и деталь
      // по отдельности, а внешний скролл сломал бы ему растяжку на всю высоту.
      if (tab === 'runs') return <RunsTab d={d} data={data} />;
      return <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', paddingRight: 4 }}>
        {tab === 'passports' ? <PassportsTab d={d} data={data} />
          : tab === 'plates' ? <PlatesTab d={d} data={data} />
            : tab === 'presets' ? <PresetsTab data={data} />
              : tab === 'samples' ? <SamplesTab d={d} data={data} />
                : tab === 'diag' ? <DiagTab data={data} />
                  : <DriftTab />}
      </div>;
    }}</Screen>
  </div>;
}

/* ═══════════ Кампании ═══════════ */

function RunsTab({ d, data }) {
  const { toast, ask } = useContext(AppCtx);
  const [sel, setSel] = useState(0);
  const [form, setForm] = useState(null);
  const runs = data.runs || [];
  const cur = sel || (runs[0] && runs[0].id) || 0;

  const backfill = async () => {
    const r = await call('adminCalibBackfill', true);
    toast(`Разобрано печатей: ${r.calib}, привязано новых: ${r.changed}`, { kind: 'ok' });
    d.reload();
  };

  const list = <>
    <div className="ms-tools">
      <button className="db" onClick={() => setForm({ nozzle: '0.4', steps: null })}>
        <DIcon n="plus" size={15} />Кампания
      </button>
      <button className="db line" title="Пересканировать журнал печатей и разложить калибровочные печати по кампаниям"
        onClick={backfill}><DIcon n="refresh" size={15} />Разобрать журнал</button>
    </div>
    {runs.length ? runs.map((r) => {
      const pct = r.steps ? Math.round((r.steps_done / r.steps) * 100) : 0;
      return <div key={r.id} className={'qrow' + (r.id === cur ? ' sel' : '')}
        onClick={() => setSel(r.id)} style={{ cursor: 'pointer' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="nm">{r.title}</div>
          <div className="ds">{r.plastic_name || 'без пластика'} · сопло {r.nozzle} ·
            {' '}{dateOnly(r.started_at)}</div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <Badge map={RUN_ST} k={r.status} />
          <div className="mono" style={{ fontSize: 12, color: 'var(--faint)', marginTop: 5 }}>
            {r.steps_done}/{r.steps} · {pct}%
          </div>
        </div>
      </div>;
    }) : <Empty ic="flame" t="Кампаний ещё нет"
      ds="Кампания — это один повод: смена сопла, новая катушка, новый филамент" />}
  </>;

  return <>
    <Split width={420} list={list} detail={cur
      ? <RunDetail id={cur} data={data} onChanged={d.reload}
        onEdit={(r) => setForm(r)} onDelete={(r) => ask({
          title: 'Удалить кампанию?', text: `«${r.title}» со всеми шагами и замерами. Печати в журнале останутся.`,
          cta: 'Удалить', danger: true,
          onYes: async () => { await call('adminCalibRunDelete', r.id); setSel(0); d.reload(); },
        })} />
      : <PickOne what="кампанию" />} />
    {form && <RunForm x={form} data={data} onClose={() => setForm(null)}
      onSaved={() => { setForm(null); d.reload(); }} />}
  </>;
}

function RunForm({ x, data, onClose, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [f, setF] = useState({
    title: x.title || '', reason: x.reason || '', nozzle: x.nozzle || '0.4',
    plastic_id: x.plastic_id || '', printer_id: x.printer_id || '', note: x.note || '',
    status: x.status || 'active',
  });
  const [steps, setSteps] = useState(() => (x.id ? [] : ['temp', 'flow', 'pa', 'retract', 'mvs', 'cube', 'vfa']));
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const tests = data.tests || [];

  const save = async () => {
    const r = await call('adminCalibRunSave', { id: x.id || 0, ...f, steps, adopt: x.id ? 0 : 1 });
    toast(x.id ? 'Кампания сохранена'
      : `Кампания создана${r.adopted ? `, подтянуто печатей: ${r.adopted}` : ''}`, { kind: 'ok' });
    onSaved();
  };

  return <Drawer title={x.id ? 'Кампания' : 'Новая кампания'} sub={'Порядок шагов зафиксирован: каждый тест опирается на предыдущий'} onClose={onClose}
    footer={<>
      <button className="db line" onClick={onClose}>Отмена</button>
      <ABtn kind="good" icon="check" onClick={save}>Сохранить</ABtn>
    </>}>
    <div className="fgap">
      <Field l="Название"><input className="inp" value={f.title} onChange={set('title')}
        placeholder="Смена сопла 0.4" /></Field>
      <Field l="Повод"><input className="inp" value={f.reason} onChange={set('reason')}
        placeholder="новое незаводское сопло" /></Field>
      <div className="frow">
        <Field l="Пластик">
          <select className="sel" value={f.plastic_id} onChange={set('plastic_id')}>
            <option value="">— не выбран —</option>
            {(data.plastics || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field l="Сопло, мм">
          <NozzleInput value={f.nozzle} onChange={set('nozzle')} data={data} />
        </Field>
      </div>
      <Field l="Принтер">
        <select className="sel" value={f.printer_id} onChange={set('printer_id')}>
          <option value="">— не выбран —</option>
          {(data.printers || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      {x.id ? <Field l="Статус">
        <select className="sel" value={f.status} onChange={set('status')}>
          <option value="active">идёт</option>
          <option value="done">завершена</option>
          <option value="archived">архив</option>
        </select>
      </Field> : <Field l="Шаги">
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {tests.map((t) => {
            const on = steps.includes(t.key);
            return <button key={t.key} className={'chip' + (on ? ' on' : '')}
              onClick={() => setSteps(on ? steps.filter((k) => k !== t.key) : steps.concat([t.key]))}>
              {t.label}
            </button>;
          })}
        </div>
      </Field>}
      <Field l="Заметка"><textarea className="inp" rows={3} value={f.note}
        onChange={set('note')} /></Field>
    </div>
  </Drawer>;
}

function RunDetail({ id, data, onChanged, onEdit, onDelete }) {
  const r = useApi('adminCalibRun', [id], [id]);
  return <Screen state={r}>{(x) => {
    const run = x.run || {};
    return <div className="od">
      <div className="od-head">
        <div style={{ flex: 1 }}>
          <div className="tt" style={{ fontSize: 19, fontWeight: 600 }}>{run.title}</div>
          <div className="ds">{run.reason || '—'} · {run.plastic_name || 'пластик не выбран'} ·
            {' '}сопло {run.nozzle} · начата {dateOnly(run.started_at)}</div>
        </div>
        <Badge map={RUN_ST} k={run.status} />
        <button className="ib" title="Править" onClick={() => onEdit(run)}><DIcon n="pencil" /></button>
        <button className="ib" title="Удалить" onClick={() => onDelete(run)}><DIcon n="trash" /></button>
      </div>
      {/* .od-body — флекс-строка фиксированной высоты внутри .od{overflow:hidden}: класть
          карточки прямо в него нельзя, они сжимаются (flex-shrink) и содержимое обрезается.
          Прокручиваемый столбец — .colw; карточкам отдельно запрещаем сжиматься. */}
      <div className="od-body">
        <div className="colw" style={{ flex: 1, padding: '20px 24px' }}>
          {run.note ? <Sec t="Заметка" ic="doc" style={{ flex: '0 0 auto' }}>
            <div style={{ whiteSpace: 'pre-wrap', fontSize: 13.5 }}>{run.note}</div></Sec> : null}
          {/* ключ включает значение и статус: после замера бэкенд кладёт в шаг новое число,
              и карточка должна перемонтироваться — иначе в поле осталось бы старое (useState
              читает начальное значение ровно один раз). */}
          {(x.steps || []).map((s) => <StepCard key={`${s.id}:${s.value}:${s.status}`} s={s}
            run={run} data={data} onChanged={() => { r.reload(); onChanged(); }} />)}
        </div>
      </div>
    </div>;
  }}</Screen>;
}

function StepCard({ s, run, data, onChanged }) {
  const { toast, lightbox } = useContext(AppCtx);
  const [open, setOpen] = useState(false);
  const [meas, setMeas] = useState(false);
  // Тест может настраивать не один параметр, а пару (глажка: поток + скорость) — поэтому
  // поле на каждый параметр. Тесты без параметров (куб, VFA) оставляют одно поле «Результат»:
  // мастер записывает туда вывод, который в пресет не уезжает.
  const params = s.params && s.params.length ? s.params : null;
  const [vals, setVals] = useState(() => {
    const v = {};
    (params || []).forEach((pr) => {
      const x = (s.values || {})[pr.key];
      v[pr.key] = x === null || x === undefined ? '' : String(x);
    });
    return v;
  });
  const [val, setVal] = useState(s.value === null || s.value === undefined ? '' : String(s.value));
  const [note, setNote] = useState(s.note || '');
  const p = s.print;
  const primary = params ? (vals[params[0].key] || '') : val;

  const save = async (extra) => {
    await call('adminCalibStepSave', {
      id: s.id, note, value: (primary === '' ? null : primary),
      ...(params ? { values: vals } : {}), ...extra,
    });
    onChanged();
  };
  const apply = async () => {
    try {
      const res = await call('adminCalibStepApply', {
        id: s.id, value: primary, ...(params ? { values: vals } : {}),
      });
      const w = res.written || [];
      toast(w.length > 1
        ? `Записано в пресет: ${w.map((x) => `${x.param} = ${x.value}`).join(', ')}`
        : `${res.param || (w[0] || {}).param} = ${res.value ?? (w[0] || {}).value} записан в пресет`,
      { kind: 'ok' });
      onChanged();
    } catch (e) { toast(e.message, { kind: 'err' }); }
  };
  const analyze = async () => {
    try {
      const res = await call('adminCalibPhoto', { journal_id: p.id, bands: 20 });
      const w = res.window || {};
      toast(w.from_pct !== undefined
        ? `Ровная зона примерно ${w.from_pct}–${w.to_pct} % высоты. Где мерить — понятно, но чем является рябь, решает штангенциркуль`
        : 'Ровных зон не нашлось — фото слишком мелкое или засвеченное', { kind: 'warn2', ms: 12000 });
    } catch (e) { toast(e.message, { kind: 'err' }); }
  };

  return <div className="card" style={{ flex: '0 0 auto', borderLeftWidth: 3, borderLeftStyle: 'solid',
    borderLeftColor: s.status === 'done' ? 'var(--price)' : (s.status === 'todo' ? 'var(--line-2)' : 'var(--amber)') }}>
    <div className="lab">
      <DIcon n="bolt" size={13} />{s.label || s.test_key}
      <span className="r" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        {s.applied ? <span className="stb green">в пресете</span> : null}
        <Badge map={STEP_ST} k={s.status} />
      </span>
    </div>

    <div className="frow" style={{ alignItems: 'flex-end', gap: 12, marginTop: 4 }}>
      {params ? params.map((pr) => <Field key={pr.key} l={pr.label + (pr.unit ? ', ' + pr.unit : '')}>
        <input className="inp mono" value={vals[pr.key] || ''}
          onChange={(e) => setVals({ ...vals, [pr.key]: e.target.value })}
          placeholder={pr.key} />
      </Field>)
        : <Field l={`Результат${s.test_unit ? ', ' + s.test_unit : ''}`}>
          <input className="inp mono" value={val} onChange={(e) => setVal(e.target.value)}
            placeholder="вывод без числа" />
        </Field>}
      <div style={{ display: 'flex', gap: 8, flex: '0 0 auto' }}>
        <ABtn kind="line" icon="calc" onClick={() => setMeas(true)}>Замер</ABtn>
        <ABtn kind="line" icon="check" onClick={() => save({ status: 'done' })}>Готово</ABtn>
        {params ? <ABtn kind="good" icon="upload" onClick={apply}
          title={`Записать в пресет (${s.scope}): ${params.map((x) => x.key).join(', ')}`}>
          В пресет</ABtn> : null}
      </div>
    </div>

    {s.verdict ? <div className="hint" style={{ marginTop: 10 }}>
      <b>Вывод.</b> {s.verdict}</div> : null}

    {(s.measures || []).length ? <div style={{ marginTop: 10 }}>
      {s.measures.map((m) => <div key={m.id} className="aud">
        <span className="at">{dateOnly(m.at)}</span>
        <div style={{ flex: 1 }}>
          <b>{m.verdict}</b>{m.value !== null && m.value !== undefined
            ? <span className="mono"> → {m.value} {m.unit}</span> : null}
          <div style={{ color: 'var(--muted)', fontSize: 12.5 }}>{m.detail}</div>
        </div>
      </div>)}
    </div> : null}

    {p ? <div className="crow2" style={{ marginTop: 10, cursor: 'default' }}>
      {p.photo ? <img src={p.photo} alt="" onClick={() => lightbox(p.photo)}
        style={{ width: 74, height: 56, objectFit: 'cover', borderRadius: 9, cursor: 'zoom-in' }} /> : null}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="nm" style={{ fontSize: 13 }}>{p.label}</div>
        <div className="mt mono" style={{ fontSize: 11.5, color: 'var(--faint)' }}>
          {p.ago} · {num(Math.round(p.grams))} г · {Math.round(p.minutes)} мин
        </div>
      </div>
      {p.timelapse_url ? <button className="ib" title="Таймлапс"
        onClick={() => openFile(p.timelapse_url)}><DIcon n="film" size={16} /></button> : null}
      {p.photo ? <button className="ib" title="Найти рябь на фото" onClick={analyze}>
        <DIcon n="search" size={16} /></button> : null}
    </div> : <div className="hint" style={{ marginTop: 10, color: 'var(--faint)' }}>
      Печати нет. Нарежьте тест в слайсере как обычно — печать привяжется сама по имени файла.
    </div>}

    {/* .ib — квадратная кнопка-иконка фиксированных 26px: текст в ней схлопывался в
        невидимую полоску. Текстовой кнопке — текстовый класс; карточка — flex-колонка,
        без alignSelf кнопку растянуло бы на всю ширину. */}
    <button className="db line sm" style={{ marginTop: 8, alignSelf: 'flex-start' }}
      onClick={() => setOpen(!open)}>
      <DIcon n={open ? 'chevd' : 'chev'} size={14} />{open ? 'Свернуть' : 'Как делать и на что смотреть'}
    </button>
    {open ? <div style={{ fontSize: 13, lineHeight: 1.5, marginTop: 6 }}>
      <p><b>Метод.</b> {s.method}</p>
      <p><b>Смотреть.</b> {s.look}</p>
      <p><b>Правило.</b> {s.hint}</p>
      {s.formula ? <p className="mono" style={{ color: 'var(--accent)' }}>{s.formula}</p> : null}
      <Field l="Заметка">
        <textarea className="inp" rows={2} value={note} onChange={(e) => setNote(e.target.value)}
          onBlur={() => save({})} />
      </Field>
    </div> : null}

    {meas && <MeasureForm step={s} run={run} data={data} onClose={() => setMeas(false)}
      onDone={() => { setMeas(false); onChanged(); }} />}
  </div>;
}

function MeasureForm({ step, run, data, onClose, onDone }) {
  const { toast } = useContext(AppCtx);
  const kinds = (data.verdict_kinds || []).filter((k) => !k.test || k.test === step.test_key);
  const all = data.verdict_kinds || [];
  const [kind, setKind] = useState((kinds[0] || all[0] || {}).key || '');
  const [vals, setVals] = useState({});
  const [res, setRes] = useState(null);
  const spec = all.find((k) => k.key === kind);

  const run_ = async () => {
    const r = await call('adminCalibMeasure', {
      kind, data: vals, run_id: run.id, step_id: step.id, to_step: 1,
    });
    setRes(r.result);
    if (r.result && r.result.value !== null && r.result.value !== undefined) {
      toast(`${r.result.verdict} → ${r.result.value}`, { kind: 'ok' });
    }
  };

  return <Drawer title={'Замер · ' + step.label} sub={'Правило применяется к цифрам и сохраняется вместе с ними'} onClose={onClose}
    footer={<>
      <button className="db line" onClick={onClose}>Закрыть</button>
      {res ? <ABtn kind="good" icon="check" onClick={onDone}>Готово</ABtn>
        : <ABtn kind="good" icon="calc" onClick={run_}>Применить правило</ABtn>}
    </>}>
    <div className="fgap">
      <Field l="Что меряем">
        <select className="sel" value={kind} onChange={(e) => { setKind(e.target.value); setRes(null); }}>
          {all.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
        </select>
      </Field>
      {(spec ? spec.fields : []).map((f) => <Field key={f.k} l={f.l}>
        <input className="inp mono" value={vals[f.k] || ''}
          onChange={(e) => setVals({ ...vals, [f.k]: e.target.value })} />
      </Field>)}
      {res ? <div className="card" style={{ borderColor: 'var(--accent-line)' }}>
        <div className="lab"><DIcon n="bolt" size={13} />Вывод</div>
        <div style={{ fontSize: 15, fontWeight: 600 }}>{res.verdict}
          {res.value !== null && res.value !== undefined
            ? <span className="mono" style={{ color: 'var(--price)' }}> → {res.value} {res.unit}</span> : null}
        </div>
        <div style={{ color: 'var(--muted)', fontSize: 13, marginTop: 6 }}>{res.detail}</div>
      </div> : null}
    </div>
  </Drawer>;
}

/* ═══════════ Паспорта ═══════════ */

function PassportsTab({ d, data }) {
  const { toast, ask } = useContext(AppCtx);
  const [form, setForm] = useState(null);
  const fields = data.passport_fields || [];
  const items = data.passports || [];

  const applyAll = async (p) => {
    const r = await call('adminCalibPassportApply', { id: p.id });
    toast(`Записано параметров: ${r.applied.length}${r.skipped.length ? `, пропущено ${r.skipped.length}` : ''}`,
      { kind: r.applied.length ? 'ok' : 'warn2' });
  };

  return <>
    <div className="ms-tools" style={{ marginBottom: 14 }}>
      <button className="db" onClick={() => setForm({ nozzle: '0.4', presets: {} })}>
        <DIcon n="plus" size={15} />Паспорт филамента
      </button>
    </div>
    {items.length ? <div className="jgrid">{items.map((p) => <div key={p.id} className="card">
      <div className="lab">
        <DIcon n="shield" size={13} />{p.plastic || 'пластик #' + p.plastic_id}
        <span className="r"><span className="stb green">{p.nozzle} мм</span></span>
      </div>
      <div className="ds" style={{ marginBottom: 8 }}>
        {p.printer || 'принтер не указан'} · {p.ago}
      </div>
      {/* Z-offset — не из пресетов (в слайсере он всегда 0), поэтому стоит первой строкой
          и отдельно: свой у филамента либо унаследованный от пластины (1028ab67). */}
      {(p.z_offset !== null && p.z_offset !== undefined) || p.plate
        ? <div className="kv">
          <span className="k">Z-offset{p.plate ? ` · ${p.plate}` : ''}</span>
          <span className="v mono">
            {zfmt(p.z_offset === null || p.z_offset === undefined ? p.plate_z : p.z_offset)}
            {(p.z_offset === null || p.z_offset === undefined) && p.plate_z !== null
              && p.plate_z !== undefined
              ? <span style={{ color: 'var(--faint)', marginLeft: 6, fontSize: 11.5 }}>пластины</span>
              : null}
          </span>
        </div>
        : null}
      {fields.filter((f) => p.values && p.values[f.key] !== undefined).map((f) => <div key={f.key} className="kv">
        <span className="k">{f.label}</span>
        <span className="v mono">{p.values[f.key]}{f.unit ? ' ' + f.unit : ''}
          {p.source && p.source[f.key] === 'own'
            ? <DIcon n="star" size={12} style={{ color: 'var(--accent)', marginLeft: 6 }} />
            : null}</span>
      </div>)}
      <div className="act" style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
        <button className="db line sm" onClick={() => openFile(API.adminCalibExportUrl(p.id))}>
          <DIcon n="doc" size={14} />PDF
        </button>
        <ABtn kind="line sm" icon="upload" title="Записать значения обратно в пресеты слайсера"
          onClick={() => applyAll(p)}>В пресеты</ABtn>
        <button className="db line sm" onClick={() => setForm(p)}>
          <DIcon n="pencil" size={14} />Править
        </button>
        <button className="ib sm" title="Удалить" onClick={() => ask({
          title: 'Удалить паспорт?', text: 'Пресеты слайсера останутся на месте.',
          cta: 'Удалить', danger: true,
          onYes: async () => { await call('adminCalibPassportDelete', p.id); d.reload(); },
        })}><DIcon n="trash" size={15} /></button>
      </div>
      <div className="hint" style={{ marginTop: 8, fontSize: 11.5 }}>
        <DIcon n="star" size={11} style={{ color: 'var(--accent)' }} /> — своя калибровка,
        без звёздочки — значение из системного профиля
      </div>
    </div>)}</div> : <Empty ic="shield" t="Паспортов ещё нет"
      ds="Паспорт собирает восемь чисел из трёх пресетов слайсера в одну карточку и хранит их в БД" />}
    {form && <PassportForm x={form} data={data} onClose={() => setForm(null)}
      onSaved={() => { setForm(null); d.reload(); }} />}
  </>;
}

function PassportForm({ x, data, onClose, onSaved }) {
  const { toast } = useContext(AppCtx);
  const disk = data.presets_disk || [];
  const [f, setF] = useState({
    plastic_id: x.plastic_id || '', printer_id: x.printer_id || '', nozzle: x.nozzle || '0.4',
    status: x.status || 'calibrated', note: x.note || '',
    plate_id: x.plate_id || '',
    z_offset: (x.z_offset === null || x.z_offset === undefined) ? '' : String(x.z_offset),
    presets: { filament: '', machine: '', process: '', ...(x.presets || {}) },
  });
  // Пустое поле Z-offset = «беру вылет пластины»: подсказываем плейсхолдером, какой он.
  const plate = (data.plates || []).find((p) => String(p.id) === String(f.plate_id));
  const plateZ = plate && plate.z_offset !== null && plate.z_offset !== undefined
    ? `как у пластины: ${zfmt(plate.z_offset)}` : 'напр. -0.07';
  const byKind = (k) => disk.filter((p) => p.kind === k);
  const setPreset = (k) => (e) => setF({ ...f, presets: { ...f.presets, [k]: e.target.value } });

  const save = async () => {
    const r = await call('adminCalibPassportSave', { id: x.id || 0, ...f, run_id: x.run_id || 0 });
    const n = Object.keys(r.values || {}).length;
    toast(`Паспорт сохранён · собрано параметров: ${n}`, { kind: 'ok' });
    onSaved();
  };

  return <Drawer title={x.id ? 'Паспорт филамента' : 'Новый паспорт'} sub={'Значения читаются из выбранных пресетов, включая унаследованные'} onClose={onClose}
    footer={<>
      <button className="db line" onClick={onClose}>Отмена</button>
      <ABtn kind="good" icon="check" onClick={save}>Собрать и сохранить</ABtn>
    </>}>
    <div className="fgap">
      <Field l="Пластик">
        <select className="sel" value={f.plastic_id}
          onChange={(e) => setF({ ...f, plastic_id: e.target.value })}>
          <option value="">— выберите —</option>
          {(data.plastics || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <div className="frow">
        <Field l="Принтер">
          <select className="sel" value={f.printer_id}
            onChange={(e) => setF({ ...f, printer_id: e.target.value })}>
            <option value="">— не выбран —</option>
            {(data.printers || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field l="Сопло, мм">
          <NozzleInput value={f.nozzle} data={data}
            onChange={(e) => setF({ ...f, nozzle: e.target.value })} />
        </Field>
      </div>
      {/* Пластина и Z-offset (1028ab67). Z-offset живёт в ПРИНТЕРЕ, а не в пресетах слайсера
          (в слайсере он всегда 0), поэтому это единственное место, где он сохраняется. */}
      <div className="frow">
        <Field l="Пластина стола">
          <select className="sel" value={f.plate_id}
            onChange={(e) => setF({ ...f, plate_id: e.target.value })}>
            <option value="">— не выбрана —</option>
            {(data.plates || []).map((p) => <option key={p.id} value={p.id}>
              {p.name}{p.z_offset === null || p.z_offset === undefined ? '' : ` (${zfmt(p.z_offset)})`}
            </option>)}
          </select>
        </Field>
        <Field l="Z-offset этого филамента, мм">
          <input className="inp" value={f.z_offset} placeholder={plateZ}
            onChange={(e) => setF({ ...f, z_offset: e.target.value })} />
        </Field>
      </div>
      {['filament', 'machine', 'process'].map((k) => <Field key={k}
        l={{ filament: 'Пресет филамента', machine: 'Пресет принтера', process: 'Пресет процесса' }[k]}>
        <select className="sel" value={f.presets[k] || ''} onChange={setPreset(k)}>
          <option value="">— не выбран —</option>
          {byKind(k).map((p) => <option key={p.path} value={p.path}>{p.name}</option>)}
        </select>
      </Field>)}
      <Field l="Статус">
        <select className="sel" value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}>
          <option value="calibrated">откалиброван</option>
          <option value="recheck">требует перепроверки</option>
          <option value="stock">сток</option>
        </select>
      </Field>
      <Field l="Заметка">
        <textarea className="inp" rows={3} value={f.note}
          onChange={(e) => setF({ ...f, note: e.target.value })} />
      </Field>
      <div className="hint">Выбранный пресет филамента заодно пропишется в карточку пластика —
        бот начнёт резать вашим откалиброванным профилем, а не системным.</div>
    </div>
  </Drawer>;
}

/* ═══════════ Пластины стола ═══════════ */
// Z-offset хранится В ПРИНТЕРЕ (в слайсере он всегда 0, проверено 30.07 — сохранение в
// слайсере молча слетало), а у каждой пластины свой вылет первого слоя. До этой вкладки
// значение жило только в голове мастера и терялось при смене пластины (OpenTodo 1028ab67).

function PlatesTab({ d, data }) {
  const { ask, toast } = useContext(AppCtx);
  const [form, setForm] = useState(null);
  const items = data.plates || [];
  const cc2 = data.plates_cc2;            // библиотеку пластин ведёт CC2 Control (null — нет её)

  const act = async (body) => {
    try { await call('adminCalibPlateSave', body); } catch (e) { toast(e.message, { kind: 'err', ms: 8000 }); }
    d.reload();
  };

  return <>
    <div className="ms-tools" style={{ marginBottom: 14 }}>
      <button className="db" onClick={() => setForm({ kind: 'textured' })}>
        <DIcon n="plus" size={15} />Пластина
      </button>
    </div>
    {/* Пластины принтера с CC2 Control ведёт его библиотека (app/cc2plates.py): сетка и Z
        там действуют по-настоящему. Монтаж и удаление — в CC2 Control, имя и Z правятся тут. */}
    {cc2 ? <div className="hint" style={{ marginBottom: 12 }}>
      {cc2.error ? `${cc2.error}${cc2.stale ? ' — показаны последние данные' : ''}. `
        : 'Пластины принтера ведёт CC2 Control: имя и Z-offset отсюда уходят на принтер. '}
      Ставят на стол и удаляют их в CC2 Control, там же заводят новую со своей сеткой — сюда она придёт сама.
      {cc2.pending ? ' Смена пластины ждёт перезагрузки принтера.' : ''}
    </div> : null}
    {items.length ? <div className="jgrid">{items.map((p) => {
      const led = !!p.cc2_plate_id;
      const live = p.cc2 || {};
      return <div key={p.id} className="card">
        <div className="lab">
          {/* Имя — отдельный СЖИМАЕМЫЙ элемент строки. Голым текстом оно было анонимным
              flex-элементом: стили к такому не применяются, ужаться он не может, и длинное имя
              выдавливало бэйдж с Z-offset за правый край карточки (замер: +18 px наружу,
              переполнение строки 41 px). Теперь переносится имя, а число остаётся внутри. */}
          <DIcon n="box" size={13} />
          <span style={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>{p.name}</span>
          {p.is_default ? <span className="stb amber" style={{ marginLeft: 8 }}>на столе</span> : null}
          <span className="r"><span className="stb green">{zfmt(p.z_offset)}</span></span>
        </div>
        <div className="ds" style={{ marginBottom: 8 }}>
          {(PLATE_KINDS.find((k) => k[0] === p.kind) || [null, 'тип не указан'])[1]}
          {' · '}{p.printer || 'любой принтер'}
          {led ? <>{' · '}<b>CC2</b>{live.side ? `, сторона ${live.side}` : ''}
            {live.mounted && live.z_applied === false ? ', Z ещё не применён' : ''}</> : null}
        </div>
        {p.note ? <div className="kv"><span className="k">Заметка</span>
          <span className="v" style={{ whiteSpace: 'pre-line' }}>{p.note}</span></div> : null}
        <div className="act" style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
          {p.is_default || led ? null : <button className="db line sm"
            onClick={() => act({ action: 'default', id: p.id })}>
            <DIcon n="star" size={14} />Сейчас на столе
          </button>}
          <button className="db line sm" onClick={() => setForm(p)}>
            <DIcon n="pencil" size={14} />Править
          </button>
          {led ? null : <button className="ib sm" title="Удалить" onClick={() => ask({
            title: 'Удалить пластину?',
            text: `«${p.name}». Паспорта, где она указана, останутся — просто без пластины.`,
            cta: 'Удалить', danger: true,
            onYes: () => act({ action: 'delete', id: p.id }),
          })}><DIcon n="trash" size={15} /></button>}
        </div>
      </div>;
    })}</div> : <Empty ic="box" t="Пластин ещё нет"
      ds="Z-offset хранится в принтере, а не в слайсере: заведите пластину и запишите её вылет — он переживёт и смену пластины, и обновление слайсера" />}
    {form && <PlateForm x={form} data={data} onClose={() => setForm(null)}
      onSaved={() => { setForm(null); d.reload(); }} />}
  </>;
}

function PlateForm({ x, data, onClose, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [f, setF] = useState({
    name: x.name || '', kind: x.kind || 'textured', printer_id: x.printer_id || '',
    z_offset: (x.z_offset === null || x.z_offset === undefined) ? '' : String(x.z_offset),
    note: x.note || '',
  });
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const led = !!x.cc2_plate_id;            // ведёт CC2 Control: имя и Z уйдут на принтер

  const save = async () => {
    try {
      const r = await call('adminCalibPlateSave', { action: 'save', id: x.id || 0, ...f });
      toast(r && r.applied ? 'Пластина сохранена, принтер применил новый Z'
        : led ? 'Пластина сохранена на принтере' : 'Пластина сохранена', { kind: 'ok' });
      onSaved();
    } catch (e) { toast(e.message, { kind: 'err', ms: 8000 }); }
  };

  return <Drawer title={x.id ? 'Пластина стола' : 'Новая пластина'}
    sub={led ? 'Ведёт CC2 Control: имя и Z-offset уйдут на принтер, тип и заметка — только здесь'
      : 'Z-offset задаётся в самом принтере — здесь он записан, чтобы не потеряться'}
    onClose={onClose} footer={<>
      <button className="db line" onClick={onClose}>Отмена</button>
      <ABtn kind="good" icon="check" disabled={!f.name.trim() || (led && !String(f.z_offset).trim())}
        onClick={save}>Сохранить</ABtn>
    </>}>
    <div className="fgap">
      <Field l="Имя пластины">
        <input className="inp" value={f.name} onChange={set('name')}
          placeholder="напр. Текстурированная PEI (родная)" />
      </Field>
      <div className="frow">
        <Field l="Тип покрытия">
          <select className="sel" value={f.kind} onChange={set('kind')}>
            {PLATE_KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        </Field>
        <Field l="Z-offset, мм">
          <input className="inp" value={f.z_offset} onChange={set('z_offset')}
            placeholder="напр. -0.04" />
        </Field>
      </div>
      <Field l="Принтер">
        <select className="sel" value={f.printer_id} onChange={set('printer_id')} disabled={led}>
          <option value="">— любой —</option>
          {(data.printers || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <Field l="Заметка">
        <textarea className="inp" rows={3} value={f.note} onChange={set('note')}
          placeholder="чем клеили, как ведёт себя на PETG" />
      </Field>
      {led ? <div className="hint">Отрицательный Z-offset — сопло ближе к столу, предел ±1 мм.
        {(x.cc2 || {}).mounted ? ' Пластина на столе: новый Z принтер применит сразу, если простаивает.' : ''}
      </div> : <div className="hint">Отрицательный Z-offset — сопло ближе к столу. Значение живёт в
        самом принтере (в слайсере оно всегда 0): здесь оно записано, чтобы после смены
        пластины не подбирать его заново.</div>}
    </div>
  </Drawer>;
}

/* ═══════════ Пресеты ═══════════ */

function PresetsTab({ data }) {
  const { toast } = useContext(AppCtx);
  const p = useApi('adminCalibPresets', [], []);
  const [hist, setHist] = useState(null);

  const snap = async () => {
    const r = await call('adminCalibSnapshot');
    toast(`Снимков: новых ${r.new}, изменённых ${r.changed}${r.gone ? `, пропало ${r.gone}` : ''}`,
      { kind: r.gone ? 'warn2' : 'ok' });
    p.reload();
  };

  return <Screen state={p}>{(x) => <>
    <div className="ms-tools" style={{ marginBottom: 14 }}>
      <ABtn icon="camera" onClick={snap}>Снять снимок</ABtn>
      <span className="mono" style={{ color: 'var(--faint)', fontSize: 12, alignSelf: 'center' }}>
        {x.user_dir}
      </span>
    </div>
    <div className="hint" style={{ marginBottom: 12 }}>
      Пользовательские пресеты живут в папке слайсера, а не в проекте: обновление слайсера
      возвращает их к стоку. Снимок кладёт тело пресета в базу — оттуда его можно вернуть.
    </div>
    <table className="tbl">
      <thead><tr><th>Вид</th><th>Пресет</th><th>Версий</th><th>Снимок</th><th></th></tr></thead>
      <tbody>{(x.items || []).map((it) => <tr key={it.path}>
        <td><span className="stb gray">{it.kind}</span></td>
        <td>
          <div className="nm">{it.name}</div>
          <div className="mono" style={{ fontSize: 11, color: 'var(--faint)' }}>{it.path}</div>
        </td>
        <td className="mono">{it.versions || 0}</td>
        <td>
          {it.gone ? <span className="stb red">файл пропал</span>
            : it.snapped
              ? (it.changed ? <span className="stb amber">изменился</span>
                : <span className="stb green">актуален</span>)
              : <span className="stb gray">нет</span>}
        </td>
        <td style={{ textAlign: 'right' }}>
          <button className="db line sm" onClick={() => setHist(it)}>
            <DIcon n="clock" size={14} />История
          </button>
        </td>
      </tr>)}</tbody>
    </table>
    {hist && <PresetHistory it={hist} onClose={() => setHist(null)} onRestored={p.reload} />}
  </>}</Screen>;
}

function PresetHistory({ it, onClose, onRestored }) {
  const { toast, ask } = useContext(AppCtx);
  const h = useApi('adminCalibPresetHistory', [it.path], [it.path]);

  const restore = (s) => ask({
    title: 'Вернуть пресет из снимка?',
    text: `${it.name} будет перезаписан версией от ${dt(s.at)}.`,
    cta: 'Восстановить', danger: false,
    onYes: async () => {
      await call('adminCalibPresetRestore', s.id);
      toast('Пресет восстановлен', { kind: 'ok' });
      onRestored();
      onClose();
    },
  });

  return <Drawer title={it.name} sub="История правок пресета" onClose={onClose}>
    <div className="fgap">
      <Screen state={h}>{(x) => ((x.items || []).length
        ? x.items.map((s) => <div key={s.id} className="card">
          <div className="lab">
            <DIcon n="clock" size={13} />{dt(s.at)}
            <span className="r">{s.gone
              ? <span className="stb red">файл пропал</span>
              : <button className="db line sm" onClick={() => restore(s)}>
                <DIcon n="back" size={13} />Вернуть</button>}</span>
          </div>
          {(s.diff || []).length ? s.diff.map((df) => <div key={df.key} className="bd-row">
            <span>{df.key}</span>
            <b>{df.old === null ? '—' : df.old} → {df.new === null ? '—' : df.new}</b>
          </div>) : <div className="ds">Первый снимок — сравнивать не с чем</div>}
        </div>)
        : <Empty ic="clock" t="Снимков нет" ds="Нажмите «Снять снимок» на вкладке пресетов" />)}
      </Screen>
    </div>
  </Drawer>;
}

/* ═══════════ Образцы ═══════════ */

function SamplesTab({ d, data }) {
  const { ask, lightbox } = useContext(AppCtx);
  const [form, setForm] = useState(null);
  const items = data.samples || [];

  return <>
    <div className="ms-tools" style={{ marginBottom: 14 }}>
      <button className="db" onClick={() => setForm({})}><DIcon n="plus" size={15} />Образец</button>
      <button className="db line" onClick={() => openFile(API.adminCalibLabelsUrl(''))}>
        <DIcon n="doc" size={15} />Этикетки PDF
      </button>
    </div>
    {items.length ? <div className="jgrid">{items.map((s) => <div key={s.id} className="jcard">
      {s.photo_url ? <div className="med"><img src={s.photo_url} alt=""
        onClick={() => lightbox(s.photo_url)} /></div> : null}
      <div className="bd">
        <div className="nm">{s.title || 'Образец'} <span className="mono"
          style={{ color: 'var(--accent)' }}>{s.code}</span></div>
        {s.value ? <div className="mono" style={{ fontSize: 13 }}>{s.value}</div> : null}
        <div className="mt" style={{ whiteSpace: 'normal' }}>{s.proves}</div>
        <div className="mt">{s.place ? '📦 ' + s.place : 'место не указано'} · {dateOnly(s.created_at)}</div>
        <div className="act">
          <button className="db line sm" onClick={() => setForm(s)}>
            <DIcon n="pencil" size={14} />Править</button>
          <button className="ib sm" title="Удалить" onClick={() => ask({
            title: 'Удалить образец?', text: s.title || s.code, cta: 'Удалить', danger: true,
            onYes: async () => { await call('adminCalibSampleDelete', s.id); d.reload(); },
          })}><DIcon n="trash" size={15} /></button>
        </div>
      </div>
    </div>)}</div> : <Empty ic="box" t="Полка пуста"
      ds="Через полгода башня на полке — просто кусок пластика. Карточка с QR делает её доказательством" />}
    {form && <SampleForm x={form} data={data} onClose={() => setForm(null)}
      onSaved={() => { setForm(null); d.reload(); }} />}
  </>;
}

function SampleForm({ x, data, onClose, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [f, setF] = useState({
    code: x.code || '', title: x.title || '', proves: x.proves || '', value: x.value || '',
    place: x.place || '', note: x.note || '', run_id: x.run_id || '', journal_id: x.journal_id || '',
  });
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const [photo, setPhoto] = useState(null);

  // copy_photo=1: у калибровочной печати фото уже снято автоматически — переснимать образец
  // руками незачем, берём кадр из журнала. Своё фото перекрывает его.
  const save = async () => {
    const r = await call('adminCalibSampleSave', { id: x.id || 0, ...f, copy_photo: 1 });
    if (photo) await call('adminPhoto', 'calib', r.id, photo, false);
    toast('Образец сохранён', { kind: 'ok' });
    onSaved();
  };

  return <Drawer title={x.id ? 'Образец' : 'Новый образец'}
    sub="Шифр печатается на этикетке с QR" onClose={onClose}
    footer={<>
      <button className="db line" onClick={onClose}>Отмена</button>
      <ABtn kind="good" icon="check" onClick={save}>Сохранить</ABtn>
    </>}>
    <div className="fgap">
      <div className="frow">
        <Field l="Шифр"><input className="inp mono" value={f.code} onChange={set('code')}
          placeholder="C-1" /></Field>
        <Field l="Значение"><input className="inp mono" value={f.value} onChange={set('value')}
          placeholder="13 мм³/с" /></Field>
      </div>
      <Field l="Что это"><input className="inp" value={f.title} onChange={set('title')}
        placeholder="Клин расхода" /></Field>
      <Field l="Что доказывает"><textarea className="inp" rows={2} value={f.proves}
        onChange={set('proves')} placeholder="Чистое окно 10–13 мм³/с, выше — рябь" /></Field>
      <div className="frow">
        <Field l="Где лежит"><input className="inp" value={f.place} onChange={set('place')}
          placeholder="полка A, коробка 2" /></Field>
        <Field l="Кампания">
          <select className="sel" value={f.run_id} onChange={set('run_id')}>
            <option value="">— не привязан —</option>
            {(data.runs || []).map((r) => <option key={r.id} value={r.id}>{r.title}</option>)}
          </select>
        </Field>
      </div>
      <Field l="Печать из журнала (фото возьмётся оттуда)">
        <select className="sel" value={f.journal_id} onChange={set('journal_id')}>
          <option value="">— не привязана —</option>
          {(data.journal || []).map((j) => <option key={j.id} value={j.id}>
            {j.test_label ? j.test_label + ' · ' : ''}{j.label}</option>)}
        </select>
      </Field>
      <Field l="Своё фото">
        <input type="file" accept="image/*" className="inp"
          onChange={async (e) => {
            const file = e.target.files && e.target.files[0];
            if (file) setPhoto(await readImage(file));
          }} />
      </Field>
      <Field l="Заметка"><textarea className="inp" rows={2} value={f.note}
        onChange={set('note')} /></Field>
    </div>
  </Drawer>;
}

/* ═══════════ Диагностика ═══════════ */

function DiagTab({ data }) {
  const [f, setF] = useState('');
  const tests = data.tests || [];
  const items = (data.diagnostics || []).filter((x) => !f || (x.tests || []).includes(f));
  return <>
    <Chips value={f} onChange={setF} style={{ marginBottom: 14 }}
      items={[{ k: '', l: 'Все правила' }].concat(tests.map((t) => ({ k: t.key, l: t.label })))} />
    <div className="jgrid" style={{ gridTemplateColumns: 'repeat(auto-fill,minmax(420px,1fr))' }}>
      {items.map((x) => <div key={x.key} className="card">
        <div className="lab"><DIcon n="search" size={13} />{x.title}</div>
        <div style={{ fontSize: 13.5, lineHeight: 1.55 }}>
          <p><b>Симптом.</b> {x.symptom}</p>
          {x.distinguish ? <p><b>Отличать от.</b> {x.distinguish}</p> : null}
          {x.cause ? <p><b>Причина.</b> {x.cause}</p> : null}
          <p style={{ color: 'var(--price)' }}><b>Что делать.</b> {x.fix}</p>
        </div>
      </div>)}
    </div>
  </>;
}

/* ═══════════ Дрейф ═══════════ */

function DriftTab() {
  const d = useApi('adminCalibDrift', [], []);
  return <Screen state={d}>{(x) => {
    const sx = x.cube_x_stat || {}, sy = x.cube_y_stat || {}, td = x.time_dev_stat || {};
    // Два узких графика в сетке, широкая таблица — отдельной полосой во всю ширину:
    // в колонке 420 px имя задания принтера переносилось по три строки, а «%» уезжал
    // на свою. span2 не спасал — сетка auto-fill, колонок может быть и одна, и четыре.
    return <div className="fgap" style={{ gap: 20 }}>
      <div className="jgrid" style={{ gridTemplateColumns: 'repeat(auto-fill,minmax(360px,1fr))' }}>
      <Sec card t="Эталонный куб — X" ic="pulse">
        {(x.cube_x || []).length > 1
          ? <><Spark data={x.cube_x.map((p) => p.value)} h={54} />
            <div className="kv"><span className="k">первый → последний</span>
              <span className="v mono">{sx.first} → {sx.last} ({sx.delta > 0 ? '+' : ''}{sx.delta})</span></div>
            <div className="kv"><span className="k">разброс</span>
              <span className="v mono">{sx.min}…{sx.max} · σ {sx.sigma}</span></div></>
          : <div className="ds">Нужно минимум два замера куба: «Кампании» → шаг «Куб» → Замер</div>}
      </Sec>
      <Sec card t="Эталонный куб — Y" ic="pulse">
        {(x.cube_y || []).length > 1
          ? <><Spark data={x.cube_y.map((p) => p.value)} h={54} color="var(--violet)" />
            <div className="kv"><span className="k">первый → последний</span>
              <span className="v mono">{sy.first} → {sy.last} ({sy.delta > 0 ? '+' : ''}{sy.delta})</span></div>
            <div className="kv"><span className="k">разброс</span>
              <span className="v mono">{sy.min}…{sy.max} · σ {sy.sigma}</span></div></>
          : <div className="ds">Замеров пока нет</div>}
      </Sec>
      </div>
      <Sec card t="Оценка слайсера против факта" ic="clock">
        <div className="ds" style={{ marginBottom: 8 }}>
          Пара «оценка/факт» есть у каждой печати журнала — бесплатный сигнал: поползло
          расхождение, значит поехали профиль или механика.
        </div>
        {(x.time_dev || []).length ? <>
          <div className="kv"><span className="k">среднее расхождение</span>
            <span className="v mono">{td.mean} % · σ {td.sigma}</span></div>
          <div style={{ overflowX: 'auto' }}>
            <table className="tbl" style={{ minWidth: 560 }}>
              <thead><tr><th>Печать</th><th style={{ textAlign: 'right' }}>Оценка</th>
                <th style={{ textAlign: 'right' }}>Факт</th>
                <th style={{ textAlign: 'right' }}>Δ</th></tr></thead>
              <tbody>{x.time_dev.slice(0, 15).map((r, i) => <tr key={i}>
                {/* имя задания принтера длинное и без пробелов — режем многоточием,
                    полное показываем во всплывающей подсказке */}
                <td><div className="oname" title={r.label} style={{ maxWidth: 420 }}>{r.label}</div></td>
                <td className="mono" style={{ textAlign: 'right' }}>{Math.round(r.est)}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{Math.round(r.fact)}</td>
                <td className="mono" style={{ textAlign: 'right', whiteSpace: 'nowrap',
                  color: Math.abs(r.dev) > 20 ? 'var(--amber)' : 'var(--muted)' }}>
                  {r.dev > 0 ? '+' : ''}{r.dev} %</td>
              </tr>)}</tbody>
            </table>
          </div>
        </> : <div className="ds">Нет печатей с оценкой времени</div>}
      </Sec>
    </div>;
  }}</Screen>;
}
