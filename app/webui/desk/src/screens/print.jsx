// print.jsx — «Печать»: пульт принтера (задание + камера), правая колонка карточек и
// вкладки очередь / обслуживание / для себя.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty, Bar, Field, StBadge, Drawer } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { num, minutes as fmtMin, grams as fmtG } from '../fmt.jsx';
import {
  QueueList, PrinterTele, useTeleHistory, Chips, ABtn, Screen, useConfirm, Cam, KV, OrderThumb,
} from '../parts.jsx';
import {
  MachineBadge, machineText, SpeedChips, EStopButton, StartDrawer, ExcludeDrawer, BedDrawer,
  TuneCard, ConsoleDrawer, ExtrudeRow, RefillToggle, SlotEditDrawer, CommunityRows,
  useHeatPresets, PresetsDrawer, SPEED_MODES,
} from './printext.jsx';

const { useState, useContext, useEffect, useCallback, useRef } = React;

// Период опроса статуса принтера — тот же, что у usePoll('adminPrinterStatus') в main.jsx.
// Показываем его в бейдже LIVE, чтобы мастер понимал, насколько «живые» цифры.
const POLL_S = 3;

// Плитки «филамент/время» в макете заданы инлайном — своего класса под них нет.
const TILE = { background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 12, padding: '12px 14px' };
const TILE_K = { fontSize: 11, color: 'var(--faint)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5 };

export function PrintScreen() {
  const { printer, nav, can } = useContext(AppCtx);
  const [tab, setTab] = useState('queue');
  // «Для себя» ходит в /api/admin/self_orders и /api/admin/self_print — обе ручки не названы
  // ни в одной карте прав, то есть закрыты «Настройками». Та же вкладка в «Заказах» уже
  // спрятана; здесь была вторая её копия, которую сметание пропустило.
  const canSelf = can('settings');
  const shownTab = tab === 'self' && !canSelf ? 'queue' : tab;
  // Очередь меняется и без мастера: бот сам снимает напечатанное и добавляет новые заказы.
  const q = useApi('adminQueue', [], [], { auto: 15000 });
  const items = (q.data && q.data.queue) || [];
  const move = useCallback(async (id, dir) => { await call('adminQueueMove', id, dir); q.reload(); }, [q]);

  return <div className="prn">
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}><JobCard pr={printer} /></div>
    <TeleCol pr={printer} onBound={q.reload} canSelf={canSelf} />
    <div className="card" style={{ minHeight: 0 }}>
      <Chips value={shownTab} onChange={setTab} items={[
        { k: 'queue', l: 'Очередь', n: items.length },
        { k: 'service', l: 'Обслуживание' },
        ...(canSelf ? [{ k: 'self', l: 'Для себя' }] : []),
      ]} />
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        {shownTab === 'queue' && <QueueTab items={items} move={move} nav={nav} state={q} />}
        {shownTab === 'service' && <ServiceTab />}
        {shownTab === 'self' && <SelfTab nav={nav} />}
      </div>
    </div>
  </div>;
}

/**
 * Бейдж живости данных: «LIVE · N с назад» с реальным отсчётом от последнего успешного опроса.
 * Раньше тут была константа «LIVE · 3 с» (период опроса) — читалась как замерший счётчик
 * (OpenTodo ed02788a). pr — новый объект на каждый успешный опрос (usePoll в main.jsx),
 * поэтому «последнее обновление» ловится по смене ссылки.
 */
function LiveBadge({ pr }) {
  const at = useRef(Date.now());
  const [, force] = useState(0);
  useEffect(() => { at.current = Date.now(); force((n) => n + 1); }, [pr]);
  useEffect(() => {
    const iv = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(iv);
  }, []);
  const s = Math.max(0, Math.round((Date.now() - at.current) / 1000));
  return <span className="tag live" title={`опрос принтера раз в ${POLL_S} с`}>
    <i />LIVE · {s <= 1 ? 'только что' : `${s} с назад`}</span>;
}

function JobCard({ pr }) {
  const { toast, nav } = useContext(AppCtx);
  const confirm = useConfirm();
  // Реальное состояние подсветки принтер вещает дельтами (led.status → pr.light) — когда оно
  // есть, оно главнее. До первой дельты помним последнее наше переключение; до первого
  // нажатия (null) подпись нейтральная, чтобы не врать.
  const [lightLocal, setLightLocal] = useState(null);
  const [repOpen, setRepOpen] = useState(false);   // диалог «Допечатать без детали» (Путь A)
  const [excOpen, setExcOpen] = useState(false);   // «Снять деталь» на ходу (прошивка сообщества)
  const light = pr && pr.light != null ? pr.light : lightLocal;
  // print_stats.state у Centauri вычисляется из остатка времени (elegoo.py) и на паузе остаётся
  // 'printing'. Паузу теперь знает machine_status (sub_status 2501/2502/2505 → pr.paused); пока
  // принтер его не прислал (null), кнопка живёт от локального состояния, как раньше.
  const [pausedLocal, setPaused] = useState(false);
  const paused = pr && pr.paused != null ? pr.paused : pausedLocal;

  const pct = Math.round((pr && pr.progress) || 0);
  const printing = !!(pr && pr.printing);
  const job = (pr && pr.filename) || '';
  useEffect(() => { setPaused(false); }, [job]);
  const t = (v) => (v == null ? '—' : Math.round(v) + '°');

  return <div className="card" style={{ flex: 1, minHeight: 0 }}>
    <div className="lab"><DIcon n="printer" size={14} />Задание принтера
      <span className="r">
        {pr && pr.online && !pr.stale
          ? <LiveBadge pr={pr} />
          : <span className="tag">{pr && pr.stale ? 'данные устарели' : 'нет связи'}</span>}
      </span>
    </div>

    {printing ? <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 14 }}>
        {/* миниатюра привязанного заказа (фолбэк — иконка принтера) */}
        <OrderThumb src={pr.orderPreview} size={48} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 16 }}>
            {pr.orderId ? `#${pr.orderId} · ` : ''}{pr.orderLabel || pr.filename || 'Печать без заказа'}
          </div>
          {pr.filename ? <div className="mono" style={{
            fontSize: 12, color: 'var(--faint)', marginTop: 2,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>{pr.filename}</div> : null}
          <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 3 }}>
            {pr.plastic || 'пластик не указан'}{pr.grams ? ` · ≈ ${fmtG(pr.grams)} филамента` : ''}
          </div>
          <div style={{ marginTop: 3 }}><MachineBadge pr={pr} /></div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className="mono" style={{ fontSize: 38, fontWeight: 700, color: 'var(--accent)', lineHeight: 1 }}>{pct}%</div>
          {pr.layer ? <div className="mono" style={{ fontSize: 12, color: 'var(--faint)', marginTop: 4 }}>
            слой {pr.layer}
          </div> : null}
        </div>
      </div>
      <Bar pct={pct} h={10} />
      <SpeedChips pr={pr} />
    </> : <div className="tele-empty">
      {pr && pr.online ? (machineText(pr) || 'Принтер свободен') : 'Нет связи с принтером'}
    </div>}

    <Cam pr={pr} fit="contain" style={{ marginTop: 16, flex: '0 1 auto' }}>
      <div className="ovl">
        <span className="tag live"><i />LIVE</span>
      </div>
      {pr && pr.online ? <div className="ovr">
        <span className="tag">🔥 {t(pr.nozzle)} / {t(pr.bed)}</span>
      </div> : null}
      {printing && pr.layer ? <div className="ovb"><span className="tag">слой {pr.layer}</span></div> : null}
    </Cam>

    {/* Свет полезен и вне печати (заглянуть в камеру) — ряд управления показываем всегда,
        пока принтер на связи; пауза и стоп имеют смысл только во время печати. Гейт по
        СПОСОБНОСТИ, а не только по связи: «умею ли паузу» — свойство протокола (волна 3). */}
    {pr && pr.can('control') && pr.online ? <div className="pctl">
      {printing ? <ABtn kind="soft" style={{ flex: 1 }} icon={paused ? 'play' : 'pause'} onClick={async () => {
        await call('adminPrinterControl', paused ? 'resume' : 'pause', true);
        setPaused(!paused);
        toast(paused ? 'Печать продолжена' : 'Печать на паузе', { kind: 'ok' });
      }}>{paused ? 'Продолжить' : 'Пауза'}</ABtn> : null}
      <ABtn kind="soft" icon="sun" style={{ flex: 1, color: light ? 'var(--amber)' : undefined }}
        onClick={async () => {
          const next = !light;
          await call('adminPrinterControl', 'light', next);
          setLightLocal(next);
          toast(next ? 'Свет включён' : 'Свет выключен', { kind: 'ok' });
        }}>Свет{light === null ? '' : (light ? ' вкл' : ' выкл')}</ABtn>
      {/* Остановка необратима: на бэкенде подтверждения нет, UI — единственный барьер */}
      {printing ? <button className="db danger" style={{ flex: 1 }} onClick={() => confirm(
        'Остановить печать?',
        pr.layer ? `Печать прервётся на слое ${pr.layer}, деталь придётся начинать заново.`
          : 'Текущая печать прервётся, деталь придётся начинать заново.',
        async () => { await call('adminPrinterControl', 'stop', true); toast('Печать остановлена', { kind: 'ok' }); },
        'Остановить',
      )}><DIcon n="stop" size={14} />Стоп</button> : null}
      {/* Допечатать без сбойной детали (Путь A). Кнопка не красная: сама она ничего не ломает —
          ломает подтверждение внутри. Гейта «идёт печать» здесь НЕТ намеренно: после шага 1
          принтер уже остановлен, а залитое задание ждёт старта — спрятав кнопку, мы отрезали бы
          мастера от второй половины действия. Все причины недоступности (нет привязки к заказу,
          печать кончилась, нет снимка настроек) называет сам ящик, одной строкой. */}
      {pr.can('upload') && pr.can('start')
        ? <ABtn kind="soft" icon="layers" style={{ flex: 1 }} onClick={() => setRepOpen(true)}>
          Без детали
        </ABtn> : null}
      {/* Допечатка со слоя (Путь В): продолжить ТУ ЖЕ печать с N-го слоя без сорвавшейся детали —
          раздел «Инструменты» открывается сразу на текущем задании из архива. */}
      {pr.can('upload') && pr.can('start')
        ? <ABtn kind="soft" icon="wrench" style={{ flex: 1 }} title="Продолжить эту печать с N-го слоя без сорвавшейся детали"
          onClick={() => nav('tools', { src: 'current' })}>
          Со слоя
        </ABtn> : null}
      {/* Снять деталь НА ХОДУ — без остановки печати (EXCLUDE_OBJECT). Только с прошивкой
          сообщества: у стока команды нет, там остаются «Без детали» и «Со слоя». */}
      {printing && pr.can('exclude_object')
        ? <ABtn kind="soft" icon="close" style={{ flex: 1 }} title="Снять сорвавшуюся деталь, остальные допечатаются"
          onClick={() => setExcOpen(true)}>Снять деталь</ABtn> : null}
    </div> : null}
    {pr && pr.online ? <div className="pctl" style={{ marginTop: 8 }}>
      <EStopButton pr={pr} style={{ flex: 1 }} />
    </div> : null}
    {repOpen ? <ReprintDrawer pr={pr} onClose={() => setRepOpen(false)} /> : null}
    {excOpen ? <ExcludeDrawer onClose={() => setExcOpen(false)} /> : null}
  </div>;
}

/**
 * «Допечатать без сбойной детали» — Путь A (app/reprint.py, docs/…/exclude-object.md).
 *
 * Деталь отвалилась или ушла в спагетти: отмечаем её, печать останавливается, задание режется
 * заново БЕЗ неё и уезжает на принтер. Цена честная и названа вслух ДО подтверждения: остаток
 * печатается ЗАНОВО С ПЕРВОГО СЛОЯ (это не продолжение прерванной печати — время и пластик
 * потрачены зря), и всё, что сейчас на столе, придётся снять.
 *
 * ДВА ШАГА, И ЭТО ГЛАВНОЕ. Сначала «стоп + нарезка + заливка», и только потом, ОТДЕЛЬНОЙ
 * кнопкой у станка с пустым столом, — старт. Единый поток запускал бы печать сам через
 * несколько минут после нажатия, когда мастер уже отошёл, а на столе лежат недопечатанные
 * детали и отвалившаяся: сопло пошло бы прямо в них.
 *
 * Список деталей и все отказы приходят с сервера одним ответом: недоступность — это строка
 * «почему» (нет привязки к заказу, печать уже кончилась, нет снимка настроек, мультиплейт,
 * принтер так не умеет), а не пустой список. Обратно шлём и индекс, и part_id: сервер сверяет,
 * что по индексу лежит та же деталь — набор и порядок зависят от стола, и разъехавшись, мы
 * выкинули бы чужую.
 */
function ReprintDrawer({ pr, onClose }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [state, setState] = useState({ loading: true });
  const [sel, setSel] = useState([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    call('adminReprintParts', 0)
      .then((r) => { if (alive) setState({ loading: false, ...r }); })
      .catch((e) => { if (alive) setState({ loading: false, error: String(e.message || e) }); });
    return () => { alive = false; };
  }, []);
  const parts = state.parts || [];
  const toggle = (i) => setSel((s) => (s.includes(i) ? s.filter((x) => x !== i) : s.concat([i])));
  const kept = parts.length - sel.length;
  const go = () => {
    const expect = {};
    sel.forEach((i) => { const p = parts.find((x) => x.index === i); expect[i] = (p && p.part_id) || ''; });
    confirm(
      'Остановить печать и нарезать заново?',
      `Печать${pr.layer ? ` на слое ${pr.layer}` : ''} будет остановлена — напечатанное время и `
      + `пластик потеряны безвозвратно. Оставшиеся ${kept} дет. будут напечатаны ЗАНОВО С НУЛЯ и `
      + 'в новой раскладке: это не продолжение, а вторая полная печать остатка. Всё, что сейчас '
      + 'на столе, включая недопечатанное, придётся снять и выбросить. Печать НЕ начнётся сама — '
      + 'задание только зальётся, а запустите вы его отдельной кнопкой, когда стол будет пуст.',
      async () => {
        setBusy(true);
        try {
          const r = await call('adminReprint', sel, expect, 0);
          setState((st) => ({ ...st, available: false, pending: {
            filename: r.filename, label: st.label, kept: (r.kept || []).length || kept,
            minutes: r.minutes, grams: r.grams,
          } }));
          toast(`Залито «${r.filename}» — СНИМИТЕ ВСЁ СО СТОЛА`, { kind: 'ok' });
        } catch (e) {
          toast(String(e.message || e), { kind: 'err' });
        } finally { setBusy(false); }
      },
      'Остановить и нарезать',
      true,
    );
  };
  // Шаг 2: старт залитого задания. Отдельное подтверждение — единственное место, где мастер
  // отвечает за пустой стол; проверить это за него нечем.
  const pending = state.pending;
  const start = () => confirm(
    'Стол пуст — запустить допечатку?',
    `«${pending.filename}»: ${pending.kept} дет. с ПЕРВОГО СЛОЯ, ≈${fmtMin(pending.minutes)} · `
    + `${fmtG(pending.grams)}. Убедитесь, что со стола снято ВСЁ — и недопечатанные детали, и `
    + 'сбойная: раскладка новая, сопло пойдёт по чистому столу.',
    async () => {
      setBusy(true);
      try {
        await call('adminReprintStart', pending.filename, 0);
        toast('Допечатка запущена', { kind: 'ok' });
        onClose();
      } catch (e) {
        toast(String(e.message || e), { kind: 'err' });
      } finally { setBusy(false); }
    },
    'Стол пуст, запустить',
    true,
  );
  return <Drawer title="Допечатать без детали" sub="сбойная деталь выбрасывается, остальные режутся и печатаются заново" onClose={onClose}>
    {state.loading ? <div className="hint">Разбираю детали задания…</div> : null}
    {pending ? <div className="card" style={{ marginBottom: 10, borderColor: 'var(--danger)' }}>
      <div className="nm">Задание залито — СНИМИТЕ ВСЁ СО СТОЛА</div>
      <div className="ds">Печать остановлена. <span className="mono">{pending.filename}</span> ·{' '}
        {pending.kept} дет. · ≈{fmtMin(pending.minutes)} · {fmtG(pending.grams)}. Печатается с
        первого слоя в новой раскладке — на столе не должно остаться ничего.</div>
      <ABtn kind="danger" style={{ marginTop: 10, width: '100%' }} disabled={busy} onClick={start}>
        Стол пуст — запустить допечатку
      </ABtn>
    </div> : null}
    {!state.loading && !pending && (state.error || state.available === false)
      ? <Empty ic="alert" t="Допечатка недоступна" ds={state.error || state.reason} /> : null}
    {state.available ? <>
      <div className="hint" style={{ marginBottom: 10 }}>
        {state.label} · <span className="mono">{state.job_name}</span>
      </div>
      {parts.map((p) => <div key={p.index} className="qrow" onClick={() => toggle(p.index)}
        style={sel.includes(p.index) ? { outline: '2px solid var(--danger)' } : null}>
        <input type="checkbox" checked={sel.includes(p.index)} readOnly />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="nm mono">{p.name}{p.part_id ? ` · ${p.part_id}` : ''}</div>
          <div className="ds">{(p.dims_mm || []).join(' × ')} мм · {num(p.volume_cm3)} см³</div>
        </div>
      </div>)}
      <ABtn kind="danger" style={{ marginTop: 12, width: '100%' }}
        disabled={busy || !sel.length || kept < 1}
        onClick={go}>
        {sel.length ? `Выбросить ${sel.length}, нарезать ${kept} заново` : 'Отметьте сбойные детали'}
      </ABtn>
    </> : null}
  </Drawer>;
}

function TeleCol({ pr, onBound, canSelf }) {
  return <div className="prn-col">
    <ExcBanner pr={pr} />
    <TeleCard pr={pr} />
    <TuneCard pr={pr} />
    <BindCard pr={pr} onBound={onBound} canSelf={canSelf} />
    <FilesCard pr={pr} />
    <SlotsCard pr={pr} />
    <ControlCard pr={pr} />
    <CommunityCard pr={pr} />
    <StatusCard pr={pr} />
  </div>;
}

/**
 * Прошивка сообщества CC2: стол (карта и винты) и консоль. Карточки нет, пока драйвер её не
 * нашёл, — на стоке этих команд нет вовсе, и пустые кнопки только путали бы.
 */
function CommunityCard({ pr }) {
  const [open, setOpen] = useState('');
  if (!pr || !pr.online || !(pr.can('bed_mesh') || pr.can('gcode'))) return null;
  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="grid" size={14} />Стол и консоль
      <span className="r" style={{ color: 'var(--faint)', fontSize: 11.5 }}>прошивка сообщества</span></div>
    <div className="pctl wrap">
      {pr.can('bed_mesh') ? <ABtn kind="soft" icon="grid" onClick={() => setOpen('bed')}>Карта стола и винты</ABtn> : null}
      {pr.can('gcode') ? <ABtn kind="soft" icon="wrench" onClick={() => setOpen('console')}>Консоль G-code</ABtn> : null}
    </div>
    {open === 'bed' ? <BedDrawer pr={pr} onClose={() => setOpen('')} /> : null}
    {open === 'console' ? <ConsoleDrawer pr={pr} onClose={() => setOpen('')} /> : null}
  </div>;
}
// Пользовательские вентиляторы (совпадает с _FAN_UI на бэкенде) + подписи.
const FAN_UI = [['model', 'Модель'], ['aux', 'Боковой'], ['box', 'Корпус']];
const JOG_STEPS = [0.1, 1, 10, 50];

/**
 * Ручное управление принтером (коды разведаны 20.07): прогрев (1028), home (1026), jog (1027),
 * вентиляторы (1030). Всё блокируется во время печати — как вкладка Control в ElegooSlicer:
 * прогрев/оси/обдув на ходу сбивают задание. Команды идут привязанным путём и подтверждаются
 * принтером (error_code:0), поэтому тосты честные — «сделано», а не «отправлено вслепую».
 */
function ControlCard({ pr }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [step, setStep] = useState(10);
  const [busy, setBusy] = useState('');
  const online = !!(pr && pr.online);
  const printing = !!(pr && pr.printing);
  const locked = !online || printing;
  // Профили прогрева — с сервера (мастер правит их сам); до ответа — пустой ряд, не старые цифры.
  const presets = useHeatPresets();
  const [editPresets, setEditPresets] = useState(false);
  // Осей и прогрева у принтера может не быть вовсе (волна 3). Заблокированная карточка обещала
  // бы «сейчас нельзя, потом можно» — честнее не рисовать её совсем.
  if (pr && !pr.can('move')) return null;
  const plist = (presets.data && presets.data.presets) || [];

  // Единый вызов команды: показываем исход (успех/ошибка принтера), блокируем на время полёта.
  const run = async (tag, fn, okMsg) => {
    if (busy) return;
    setBusy(tag);
    try {
      const r = await fn();
      if (r && r.err_code != null) toast('Принтер ответил кодом ' + r.err_code, { kind: 'warn2' });
      else toast(okMsg, { kind: 'ok' });
    } catch (e) { toast(e.message, { kind: 'warn2' }); } finally { setBusy(''); }
  };

  const heat = (target, val, label) =>
    run('heat', () => call('adminPrinterHeat', target, val),
      val ? `${label}: цель ${val}°` : `${label}: нагрев выключен`);
  const fan = (which, pct, label) =>
    run('fan', () => call('adminPrinterFan', which, pct), `${label}: обдув ${pct}%`);
  const jog = (axis, dir) =>
    run('move', () => call('adminPrinterMove', axis, dir * step),
      `${axis.toUpperCase()} ${dir > 0 ? '+' : '−'}${step} мм`);

  const tgt = (v) => (v ? ` · цель ${Math.round(v)}°` : '');
  // Профиль ставит сопло и стол одним нажатием — так прогревают под материал на деле.
  const heatBoth = (p) => run('heat', async () => {
    const a = await call('adminPrinterHeat', 'extruder', p.nozzle);
    const b = await call('adminPrinterHeat', 'heater_bed', p.bed);
    return (a && a.err_code != null) ? a : b;
  }, `Прогрев ${p.name}: сопло ${p.nozzle}°, стол ${p.bed}°`);
  const off = (what, okMsg) => run('off', () => call('adminPrinterOff', what), okMsg);

  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="wrench" size={14} />Ручное управление
      <span className="r" style={{ color: 'var(--faint)', fontSize: 11.5 }}>
        {!online ? 'нет связи' : (printing ? 'недоступно во время печати' : '')}
      </span>
    </div>

    {/* Прогрев: профили материалов (сопло + стол одним нажатием) и выключатели. Текущая цель
        подсвечена у профиля, совпавшего с ней по обеим температурам. */}
    <div style={{ opacity: locked ? 0.5 : 1, pointerEvents: locked ? 'none' : 'auto' }}>
      <div className="ctl-stack">
        <span className="ctl-k"><DIcon n="flame" size={13} />Прогрев
          <span style={{ color: 'var(--faint)', fontWeight: 400 }}>{tgt(pr && pr.nozzleTarget)}{pr && pr.bedTarget ? ` / ${Math.round(pr.bedTarget)}°` : ''}</span></span>
        <div className="ctl-btns">
          {plist.map((p) => {
            const on = pr && Math.round(pr.nozzleTarget || 0) === p.nozzle && Math.round(pr.bedTarget || 0) === p.bed;
            return <button key={p.name} className="db sm line" disabled={locked || !!busy}
              title={`сопло ${p.nozzle}°, стол ${p.bed}°`}
              style={on ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
              onClick={() => heatBoth(p)}>{p.name}</button>;
          })}
          <button className="db sm line" title="Править профили прогрева" onClick={() => setEditPresets(true)}>
            <DIcon n="pencil" size={13} /></button>
        </div>
      </div>
      <div className="ctl-stack">
        <span className="ctl-k">Выключить</span>
        <div className="ctl-btns">
          <button className="db sm line" disabled={locked || !!busy} onClick={() => off('heaters', 'Нагрев выключен')}>нагрев</button>
          <button className="db sm line" disabled={locked || !!busy} onClick={() => off('fans', 'Обдув выключен')}>обдув</button>
          <button className="db sm line" disabled={locked || !!busy} onClick={() => off('all', 'Нагрев и обдув выключены')}>всё</button>
        </div>
      </div>

      {/* Оси: парковка + jog. Шаг общий на все оси, чтобы не промахнуться масштабом. */}
      <div className="ctl-row" style={{ marginTop: 6 }}>
        <span className="ctl-k">Оси · шаг</span>
        <div className="ctl-btns">
          {JOG_STEPS.map((s) => <ABtn key={s} kind="sm line" disabled={locked}
            style={step === s ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
            onClick={() => setStep(s)}>{s}</ABtn>)}
        </div>
      </div>
      <div className="ctl-jog">
        <ABtn kind="sm line" disabled={locked || !!busy} onClick={() => jog('y', 1)}>Y+</ABtn>
        <ABtn kind="sm line" disabled={locked || !!busy} onClick={() => jog('z', 1)}>Z+</ABtn>
        <ABtn kind="sm line" disabled={locked || !!busy} onClick={() => jog('x', -1)}>X−</ABtn>
        <ABtn kind="sm soft" disabled={locked || !!busy}
          onClick={() => run('home', () => call('adminPrinterHome', 'xyz'), 'Парковка осей')}>⌂ Домой</ABtn>
        <ABtn kind="sm line" disabled={locked || !!busy} onClick={() => jog('x', 1)}>X+</ABtn>
        <ABtn kind="sm line" disabled={locked || !!busy} onClick={() => jog('y', -1)}>Y−</ABtn>
        <ABtn kind="sm line" disabled={locked || !!busy} onClick={() => jog('z', -1)}>Z−</ABtn>
      </div>

      {/* Вентиляторы: быстрые 0/50/100 % с подсветкой текущего значения из телеметрии. */}
      {FAN_UI.map(([key, label]) => {
        const cur = pr && pr.fans ? pr.fans[key] : null;
        return <div className="ctl-row" key={key}>
          <span className="ctl-k"><DIcon n="pulse" size={13} />{label}{cur != null ? ` · ${cur}%` : ''}</span>
          <div className="ctl-btns">
            {[0, 50, 100].map((p) => <ABtn key={p} kind="sm line" disabled={locked || !!busy}
              style={cur === p ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
              onClick={() => fan(key, p, label)}>{p}%</ABtn>)}
          </div>
        </div>;
      })}
      {/* Ручная экструзия — только с прошивкой сообщества (у стока команды нет). */}
      <ExtrudeRow pr={pr} />
    </div>
    {editPresets ? <PresetsDrawer presets={plist} onSaved={() => presets.reload()}
      onClose={() => setEditPresets(false)} /> : null}
  </div>;
}

// Ошибки принтера (machine_status.exception_status) — вещаются дельтами; пустой список это
// «ошибок нет», отсутствие поля — «принтер пока не сообщал». Баннер рисуем только по факту.
function ExcBanner({ pr }) {
  const exc = (pr && pr.exceptions) || [];
  if (!exc.length) return null;
  return <div className="card" style={{ flex: '0 0 auto', border: '1px solid var(--red)' }}>
    <div className="lab" style={{ color: 'var(--red)' }}><DIcon n="flame" size={14} />Ошибки принтера</div>
    {exc.map((e, i) => <div key={i} className="mono" style={{ fontSize: 12.5, color: 'var(--red)' }}>
      {typeof e === 'object' ? JSON.stringify(e) : String(e)}
    </div>)}
  </div>;
}

function TeleCard({ pr }) {
  const hist = useTeleHistory(pr);
  const pct = Math.round((pr && pr.progress) || 0);
  const printing = !!(pr && pr.printing);
  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="pulse" size={14} />Телеметрия</div>
    <PrinterTele pr={pr} history={hist} cols={2} />
    {printing ? <div style={{
      display: 'grid', gridTemplateColumns: pr.grams ? '1fr 1fr' : '1fr', gap: 12, marginTop: 12,
    }}>
      {/* Сколько филамента уже уехало в модель, принтер не сообщает — оцениваем долей от
          общего расхода заказа по прогрессу, поэтому «≈». Без граммов плитку не рисуем. */}
      {pr.grams ? <div style={TILE}>
        <div style={TILE_K}>Филамент на модель</div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, margin: '6px 0 8px' }}>
          <span className="mono" style={{ fontSize: 17, fontWeight: 700 }}>≈ {fmtG(pr.grams * pct / 100)}</span>
          <span style={{ fontSize: 12, color: 'var(--faint)' }}>из {fmtG(pr.grams)}</span>
        </div>
        <Bar pct={pct} h={6} color="var(--violet)" />
      </div> : null}
      <div style={TILE}>
        <div style={TILE_K}>Время печати</div>
        <div style={{
          display: 'flex', justifyContent: 'space-between',
          fontFamily: 'var(--mono)', fontSize: 12.5, margin: '9px 0 8px',
        }}>
          <span>{pr.startedAt || '—'}</span>
          <span style={{ color: 'var(--faint)' }}>{pr.eta || '—'}</span>
        </div>
        <Bar pct={pct} h={6} color="var(--blue)" />
        <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 7 }}>
          {pr.elapsed ? `прошло ${pr.elapsed}` : ''}
          {pr.remaining ? ` · осталось ~${pr.remaining}` : ''}
        </div>
      </div>
    </div> : null}
  </div>;
}

function BindCard({ pr, onBound, canSelf }) {
  const { toast, ask } = useContext(AppCtx);
  const orders = useApi('adminOrders', [''], []);
  // Заказы самого мастера считаются «печатью для себя» (user_id ∈ ADMIN_IDS) и в клиентский
  // список не попадают — но привязать печать к своему заказу тоже нужно (иначе он не выбирается).
  // Поэтому в дропдаун привязки кладём оба списка: клиентские + самопечать, дедуп по id.
  // Самопечать закрыта «Настройками», а карточка мотается на КАЖДОМ открытии «Печати» — без
  // skip оператор ловил 403 просто за то, что зашёл на свой рабочий экран.
  const selfOrders = useApi('adminSelfOrders', [], [], { skip: !canSelf });
  const bindList = (() => {
    const seen = new Set();
    const out = [];
    for (const o of [...((orders.data && orders.data.orders) || []),
      ...((selfOrders.data && selfOrders.data.orders) || [])]) {
      if (seen.has(o.id)) continue;
      seen.add(o.id);
      out.push(o);
    }
    return out.sort((a, b) => b.id - a.id);
  })();
  const [open, setOpen] = useState(false);      // панель выбора заказа справа
  const bound = pr && pr.orderId;

  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="link" size={14} />Заказ печати</div>
    {bound ? <div style={{ display: 'flex', alignItems: 'center', gap: 13 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, fontSize: 14.5 }}>#{pr.orderId} · {pr.orderLabel || 'Без названия'}</div>
        <div className="hint" style={{ margin: 0 }}>таймлапс уйдёт клиенту по завершении</div>
      </div>
      <ABtn kind="sm line" onClick={async () => {
        await call('adminPrinterUnbind');
        toast('Привязка снята', { kind: 'ok' });
        onBound();
      }}>Отвязать</ABtn>
      {/* «Завершено» = adminPrintConfirm: заказ → «Напечатано», клиенту уходит уведомление
          и таймлапс, начисляется наработка ТО. Шаг заметный для клиента — спрашиваем. */}
      <ABtn kind="sm soft" onClick={() => ask({
        title: 'Завершить печать?',
        text: 'Заказ перейдёт в «Напечатано», клиент получит уведомление и таймлапс.',
        cta: '✓ Завершено', tone: 'ok',     // позитивное завершение → зелёная кнопка
        onYes: async () => {
          await call('adminPrintConfirm', pr.orderId, true);
          toast(`Заказ #${pr.orderId} → «Напечатано»`, { kind: 'ok', ms: 7000 });
          onBound();
        },
      })}>✓ Завершено</ABtn>
    </div> : <>
      {/* Раньше был комбобокс: заказ в нём — безликая строка «#id · название». Теперь кнопка
          открывает панель справа со списком заказов с миниатюрами (как выбор файла принтера) —
          мастер узнаёт заказ по картинке, а не вспоминает номер. */}
      <button className="db line full" onClick={() => { setOpen(true); orders.refresh(); selfOrders.refresh(); }}>
        <DIcon n="link" size={15} />Выбрать заказ…
      </button>
      <div className="hint" style={{ marginTop: 8 }}>
        чтобы списание пластика и таймлапс попали в нужный заказ
      </div>
      {open ? <BindDrawer list={bindList} loading={orders.loading || selfOrders.loading}
        onClose={() => setOpen(false)}
        onPick={async (o) => {
          try {
            await call('adminPrinterBind', o.id);
            toast(`Печать привязана к заказу #${o.id}`, { kind: 'ok' });
            setOpen(false);
            onBound();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }} /> : null}
    </>}
  </div>;
}

/**
 * Панель выбора заказа для привязки печати: строки с миниатюрами (превью модели или фото
 * результата), поиском и статусом — клик по строке сразу привязывает. Список тот же, что был
 * в комбобоксе: клиентские заказы + самопечать мастера (дедуп в BindCard).
 */
function BindDrawer({ list, loading, onPick, onClose }) {
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const needle = q.trim().toLowerCase();
  const rows = list.filter((o) => !needle
    || String(o.id).includes(needle)
    || (o.label || '').toLowerCase().includes(needle)
    || (o.user || '').toLowerCase().includes(needle));
  const pick = async (o) => {
    if (busy) return;
    setBusy(true);
    try { await onPick(o); } finally { setBusy(false); }
  };
  return <Drawer title="Заказ печати" sub="к какому заказу привязать текущую печать" onClose={onClose}>
    <input className="inp" placeholder="Поиск по номеру, названию, клиенту…" value={q} autoFocus
      onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 12 }} />
    {loading && !list.length ? <div className="hint">Загружаю заказы…</div> : null}
    {rows.length ? rows.map((o) => <div key={o.id} className="qrow"
      style={busy ? { opacity: .5, pointerEvents: 'none' } : null}
      onClick={() => pick(o)}>
      <OrderThumb src={o.preview} size={44} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="nm" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          #{o.id} · {o.label || 'Без названия'}
        </div>
        <div className="ds">{o.user || ''}</div>
      </div>
      <StBadge st={o.status} />
    </div>) : (!loading ? <Empty ic="box" t="Заказов не найдено"
      ds={list.length ? 'по этому запросу ничего' : 'активных заказов нет'} /> : null)}
  </Drawer>;
}

function FilesCard({ pr }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [storage, setStorage] = useState('local');      // 'local' | 'u-disk' (флешка)
  const files = useApi('adminPrinterFiles', [storage], [storage]);
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState(null);      // выбранный из панели файл (объект)
  const [starting, setStarting] = useState(null);  // файл в диалоге старта (слоты, сторона листа)
  const busy = !!(pr && pr.printing);

  // Старт печати двигает реальное железо и тратит пластик. Раньше — один вопрос «Запустить?»;
  // теперь диалог, где каждый филамент файла получает слот хаба, выбирается сторона листа и
  // калибровка. Он же и подтверждение: кнопка в нём — «Стол пуст — печатать».
  const doPrint = (f) => { setStarting(f); setOpen(false); };

  // Удаление из памяти принтера необратимо (файл придётся заливать заново) — спрашиваем.
  const doDelete = (list) => {
    const names = list.map((f) => f.name);
    const one = names.length === 1;
    confirm(one ? 'Удалить файл с принтера?' : `Удалить ${names.length} файлов с принтера?`,
      (one ? `«${names[0]}» будет стёрт` : 'Выбранные файлы будут стёрты')
      + (storage === 'u-disk' ? ' с флешки' : ' из памяти принтера') + '. Вернуть можно только повторной заливкой.',
      async () => {
        try {
          if (one && storage === 'local') await call('adminPrinterDelete', names[0]);
          else await call('adminPrinterDelete', names[0], { names, storage });
          toast(one ? `«${names[0]}» удалён` : `Удалено файлов: ${names.length}`, { kind: 'ok' });
          if (picked && names.includes(picked.name)) setPicked(null);   // выбранного файла больше нет
          files.reload();
        } catch (e) { toast(e.message, { kind: 'err', ms: 9000 }); }
      }, 'Удалить');
  };

  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="doc" size={14} />Старт печати файла</div>
    {/* Раньше был комбобокс на 100 файлов — теперь кнопка открывает панель-таблицу справа */}
    <button className="db line full" disabled={busy} onClick={() => { setOpen(true); files.reload(); }}>
      <DIcon n="folder" size={15} />
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {picked ? picked.name : 'Выбрать файл из принтера'}
      </span>
    </button>
    {picked ? <ABtn kind="full" icon="printer" disabled={busy} style={{ marginTop: 10 }}
      onClick={() => doPrint(picked)}>Печатать…</ABtn> : null}
    {busy ? <div className="hint" style={{ marginTop: 8 }}>недоступно, пока идёт текущая печать</div> : null}
    {open ? <FilesDrawer state={files} busy={busy} pr={pr} storage={storage} setStorage={setStorage}
      onClose={() => setOpen(false)}
      onPick={(f) => { setPicked({ ...f, storage }); setOpen(false); }}
      onPrint={(f) => doPrint({ ...f, storage })} onDelete={doDelete} /> : null}
    {starting ? <StartDrawer file={starting} pr={pr} storage={starting.storage || 'local'}
      onClose={() => setStarting(null)} /> : null}
  </div>;
}

const FILE_SORTS = [
  { k: 'new', l: 'Новые', f: (a, b) => (b.created || 0) - (a.created || 0) },
  { k: 'name', l: 'По имени', f: (a, b) => String(a.name).localeCompare(String(b.name), 'ru') },
  { k: 'long', l: 'Долгие', f: (a, b) => (b.print_time || 0) - (a.print_time || 0) },
];

// Панель выбора gcode-файла справа: файлы принтера (память или флешка), фильтр по имени,
// сортировка, выбор нескольких для удаления. Клик по строке — выбрать; кнопка — сразу в
// диалог старта. Сверху — заливка нового .gcode с компьютера (протокол разведан 20.07; заливка
// идёт только во внутреннюю память).
function FilesDrawer({ state, busy, pr, storage, setStorage, onClose, onPick, onPrint, onDelete }) {
  const { toast } = useContext(AppCtx);
  const [q, setQ] = useState('');
  const [sort, setSort] = useState('new');
  const [multi, setMulti] = useState(false);
  const [sel, setSel] = useState([]);
  const [up, setUp] = useState('');                  // имя файла, который сейчас заливается
  const fileRef = useRef(null);
  useEffect(() => { setSel([]); }, [storage]);
  const usb = pr && pr.can('usb') && pr.uDisk !== false;
  const thumbs = !!(pr && pr.can('thumbs'));

  const doUpload = async (file) => {
    if (!file) return;
    if (!/\.gcode$/i.test(file.name)) {
      toast('Принтер принимает только .gcode', { kind: 'warn2' });
      return;
    }
    setUp(file.name);
    try {
      const r = await call('adminPrinterUpload', file);
      toast(`«${r.name || file.name}» залит на принтер`, { kind: 'ok' });
      if (storage !== 'local') setStorage('local'); else state.reload();   // файл сразу в списке
    } catch (e) {
      toast(e.message, { kind: 'err', ms: 9000 });
    } finally {
      setUp('');
      if (fileRef.current) fileRef.current.value = '';   // тот же файл можно выбрать повторно
    }
  };
  const toggle = (f) => setSel((s) => (s.includes(f.name) ? s.filter((x) => x !== f.name) : s.concat([f.name])));

  return <Drawer title="Файлы на принтере" sub="выберите gcode для печати" onClose={onClose}
    footer={multi ? <ABtn kind="danger full" icon="trash" disabled={!sel.length || busy}
      onClick={() => onDelete(((state.data && state.data.files) || []).filter((f) => sel.includes(f.name)))}>
      {sel.length ? `Удалить выбранные (${sel.length})` : 'Отметьте файлы'}</ABtn> : null}>
    {/* Заливка: скрытый input + видимая кнопка (стандартный file-input не стилизуется) */}
    <input ref={fileRef} type="file" accept=".gcode" style={{ display: 'none' }}
      onChange={(e) => doUpload(e.target.files && e.target.files[0])} />
    <ABtn kind="full" icon="upload" disabled={!!up}
      onClick={() => fileRef.current && fileRef.current.click()}>
      {up ? `Заливаю «${up}»…` : 'Залить .gcode с компьютера'}
    </ABtn>
    <div className="hint" style={{ margin: '8px 0 12px' }}>
      файл уйдёт в память принтера — печатать можно сразу отсюда
    </div>
    {usb ? <Chips value={storage} onChange={setStorage} style={{ marginBottom: 10 }} items={[
      { k: 'local', l: 'Память принтера' }, { k: 'u-disk', l: 'Флешка' }]} /> : null}
    <input className="inp" placeholder="Поиск по имени…" value={q} autoFocus
      onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 10 }} />
    <Chips value={sort} style={{ marginBottom: 10 }}
      onChange={(k) => { if (k === '_m') { setMulti(!multi); setSel([]); } else setSort(k); }}
      items={FILE_SORTS.map(({ k, l }) => ({ k, l })).concat([
        { k: '_m', l: multi ? 'Готово' : 'Выбрать несколько', right: true }])} />
    <Screen state={state}>{(d) => {
      const needle = q.trim().toLowerCase();
      const cmp = (FILE_SORTS.find((x) => x.k === sort) || FILE_SORTS[0]).f;
      const list = (d.files || []).filter((f) => !needle || (f.name || '').toLowerCase().includes(needle)).sort(cmp);
      if (!list.length) return <Empty ic="doc" t="Файлов не найдено"
        ds={d.files && d.files.length ? 'по этому запросу ничего' : (storage === 'u-disk' ? 'на флешке нет gcode' : 'на принтере нет файлов')} />;
      return list.map((f) => <div key={f.name} className="qrow" onClick={() => (multi ? toggle(f) : onPick(f))}
        style={multi && sel.includes(f.name) ? { outline: '2px solid var(--red)' } : undefined}>
        {multi ? <input type="checkbox" checked={sel.includes(f.name)} readOnly /> : null}
        {/* Превью из самого G-code (прошивка сообщества): грузится лениво — к принтеру идут
            только видимые строки, по одной, и бот кэширует их на диске. Нет превью — пусто. */}
        {thumbs && f.path ? <img src={window.API.printerThumbSrc(storage, f.path, f.created)} alt=""
          loading="lazy" width="44" height="44"
          style={{ borderRadius: 8, objectFit: 'cover', background: 'var(--bg)', flex: '0 0 auto' }}
          onError={(e) => { e.currentTarget.style.visibility = 'hidden'; }} /> : null}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="nm" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</div>
          <div className="ds">
            {(f.colors || []).length ? <span style={{ marginRight: 6 }}>{(f.colors || []).map((c, i) => <span key={i}
              title={`T${c.extruder} · ${c.type || ''}`} style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%',
                background: c.hex || 'var(--bg)', border: '1px solid var(--line)', marginRight: 2, verticalAlign: -1 }} />)}</span> : null}
            {[f.print_time ? fmtMin(f.print_time / 60) : '', f.layer ? `${f.layer} слоёв` : '',
              f.grams ? fmtG(f.grams) : ''].filter(Boolean).join(' · ')}
          </div>
          {/* Чем резали (8e8d4873): снимок настроек снимается при заливке файла ботом.
              У файлов, залитых напрямую из слайсера, его нет — строки просто не будет. */}
          {f.settings ? <div className="ds mono" style={{ color: 'var(--faint)' }}>
            {f.settings}
          </div> : null}
        </div>
        {multi ? null : <>
          {/* прямой старт из строки: клик по строке — «выбрать», по кнопке — диалог старта */}
          <button className="db sm soft" disabled={busy}
            onClick={(e) => { e.stopPropagation(); onPrint(f); }}
            title="В печать"><DIcon n="printer" size={14} /></button>
          {/* удаление (подтверждение — в обработчике) */}
          <button className="db sm line" style={{ color: 'var(--red)' }} disabled={busy}
            onClick={(e) => { e.stopPropagation(); onDelete([f]); }}
            title="Удалить с принтера"><DIcon n="trash" size={14} /></button>
        </>}
      </div>);
    }}</Screen>
  </Drawer>;
}

/**
 * Слоты CANVAS: что заряжено в хаб (бренд/тип/цвет/температуры) + выгрузка по слоту.
 * Состав слотов принтер ВЕЩАЕТ (canvas_info в api_status — захват docs/captures/
 * broadcast-idle-2026-07-20.json), привязка клиента не нужна. active_slot приходит той же
 * телеметрией: во время печати подсвечиваем, из какого слота идёт пруток.
 */
function SlotsCard({ pr }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [busy, setBusy] = useState(0);
  const [editSlot, setEditSlot] = useState(null);   // правка слота (прошивка сообщества)
  const slots = (pr && pr.slots) || [];
  const printing = !!(pr && pr.printing);

  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="spool" size={14} />Слоты CANVAS
      <span className="r" style={{ color: 'var(--faint)', fontSize: 11.5 }}>
        {printing ? 'выгрузка недоступна во время печати' : ''}
      </span>
    </div>
    {slots.length ? slots.map((s) => {
      const active = pr.activeSlot === s.slot;
      return <div key={s.slot} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '7px 0' }}>
        {/* цвет филамента — прямо с катушки (RFID), рамка отделяет тёмные цвета от фона */}
        <span style={{ width: 16, height: 16, borderRadius: '50%', flex: '0 0 auto',
          background: s.color || 'var(--bg)', border: '1px solid var(--line)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {s.slot} · {[s.brand, s.name].filter(Boolean).join(' ') || s.type || 'пусто'}
            {active ? <span className="tag live" style={{ marginLeft: 8 }}><i />печатает</span> : null}
          </div>
          <div className="mono" style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 1 }}>
            {s.type || '—'}{s.temp_min && s.temp_max ? ` · ${s.temp_min}–${s.temp_max}°` : ''}
          </div>
        </div>
        <ABtn kind="sm line" disabled={!pr || !pr.online || printing || !!busy}
          title={`Выгрузить филамент из слота ${s.slot}`}
          onClick={() => confirm(
            `Выгрузить филамент из слота ${s.slot}?`,
            'Делайте это, когда принтер не печатает.',
            async () => {
              setBusy(s.slot);
              try {
                await call('adminPrinterUnload', s.slot);
                toast(`Выгрузка слота ${s.slot} запущена`, { kind: 'ok' });
              } catch (e) { toast(e.message, { kind: 'warn2' }); } finally { setBusy(0); }
            },
            'Выгрузить', false,        // выгрузка обратима — обычная кнопка, не «опасная»
          )}>{busy === s.slot ? '…' : 'Выгрузить'}</ABtn>
        {pr.can('slot_edit') ? <button className="db sm line" disabled={!pr.online || printing}
          title="Записать в хаб материал, цвет и температуры (для катушек без метки)"
          onClick={() => setEditSlot(s)}><DIcon n="pencil" size={13} /></button> : null}
      </div>;
    }) : <div className="hint">Принтер ещё не сообщил состав слотов — он вещает его сам, обычно в течение минуты.</div>}
    <RefillToggle pr={pr} />
    {editSlot ? <SlotEditDrawer slot={editSlot} onClose={() => setEditSlot(null)} /> : null}
  </div>;
}

// Подписи вентиляторов телеметрии (fans.* → человеческое имя).
const FAN_LABELS = [
  ['model', 'обдув модели'], ['aux', 'боковой'], ['box', 'корпуса'],
  ['controller', 'платы'], ['heater', 'хотэнда'],
];

/**
 * «Состояние» — остальная телеметрия, которой не нашлось места в основных ячейках:
 * вентиляторы, позиция головы, оси, U-диск. Строки появляются по мере того, как принтер
 * вещает соответствующие дельты (в простое их может не быть вовсе — тогда подсказка).
 */
function StatusCard({ pr }) {
  if (!pr || !pr.online) return null;
  const fans = pr.fans || {};
  const fanRows = FAN_LABELS.filter(([k]) => fans[k] != null);
  const pos = pr.pos || {};
  const hasPos = pos.x != null || pos.y != null || pos.z != null;
  const rows = [];
  if (hasPos) {
    rows.push(['Позиция головы', ['x', 'y', 'z'].filter((a) => pos[a] != null)
      .map((a) => `${a.toUpperCase()} ${Number(pos[a]).toFixed(1)}`).join(' · ')]);
  }
  if (pr.homedAxes) rows.push(['Оси захомлены', pr.homedAxes.toUpperCase()]);
  if (pr.uDisk != null) rows.push(['U-диск', pr.uDisk ? 'вставлен' : 'нет']);
  if (pr.machine) rows.unshift(['Машина', machineText(pr) || pr.machine.label]);
  if (pr.speedMode != null) {
    const sm = SPEED_MODES.find(([m]) => m === pr.speedMode);
    rows.push(['Режим скорости', sm ? sm[1] : String(pr.speedMode)]);
  }
  if (!fanRows.length && !rows.length && !pr.community) {
    return <div className="card" style={{ flex: '0 0 auto' }}>
      <div className="lab"><DIcon n="pulse" size={14} />Состояние</div>
      <div className="hint">Вентиляторы, позицию головы и прочее принтер сообщает по ходу печати.</div>
    </div>;
  }
  return <div className="card" style={{ flex: '0 0 auto' }}>
    <div className="lab"><DIcon n="pulse" size={14} />Состояние</div>
    {fanRows.map(([k, l]) => <KV key={k} k={l} v={fans[k] + '%'} />)}
    {rows.map(([k, v]) => <KV key={k} k={k} v={v} />)}
    <CommunityRows pr={pr} />
  </div>;
}

function QueueTab({ items, move, nav, state }) {
  const total = items.reduce((s, i) => s + (i.minutes || 0), 0);
  return <Screen state={state}>{() => <>
    <QueueList items={items} onMove={move} onOpen={(id) => nav('orders', { sel: id })} />
    {items.length ? <div className="hint" style={{ marginTop: 10 }}>
      клиент видит свою позицию в боте · стрелками меняем порядок · суммарно ≈ {fmtMin(total)}
    </div> : null}
  </>}</Screen>;
}

// Единицы триггера ТО. Ключи — ровно те значения trigger_type, что принимает сервер
// (_api_admin_service_action: hours / prints / days); опечатка здесь = тихий отказ.
const SVC_UNIT = { hours: 'часов', prints: 'печатей', days: 'дней' };
// Светофор приходит с сервера полем status, здесь только его оформление.
const SVC_LVL = {
  red: ['var(--red)', 'пора'],
  amber: ['var(--amber)', 'скоро'],
  green: ['var(--price)', 'норма'],
};

function ServiceTab() {
  const { toast, ask } = useContext(AppCtx);
  const confirm = useConfirm();
  const s = useApi('adminService', [], []);
  const [add, setAdd] = useState(null);
  // Правка порога — построчная: {id, val}. Разворачиваем прямо в строке пункта,
  // чтобы мастер видел текущую наработку рядом с новым числом.
  const [edit, setEdit] = useState(null);

  // Единая точка вызова: сервер на неизвестное действие отвечает {ok:false},
  // call() превращает это в исключение — показываем мастеру, а не молчим.
  const act = async (body, okMsg, kind = 'ok') => {
    try {
      await call('adminServiceAction', body);
      if (okMsg) toast(okMsg, { kind });
      s.reload();
      return true;
    } catch (e) {
      toast(e.message, { kind: 'warn2' });
      return false;
    }
  };

  return <Screen state={s}>{(d) => {
    const items = d.items || [];
    // warn_pct — порог «скоро» с бэкенда; показываем его в подсказке, сам светофор считает сервер.
    const warn = d.warn_pct || 85;
    return <>
      <div className="hint" style={{ marginBottom: 10 }}>
        🔴 пора · 🟡 скоро ({Math.round(warn)}%) · 🟢 норма — триггер по наработке
      </div>
      {items.length ? items.map((it) => {
        const pct = Math.min(100, (it.ratio || 0) * 100);
        const [color, word] = SVC_LVL[it.status] || SVC_LVL.green;
        const unit = SVC_UNIT[it.trigger] || '';
        const editing = edit && edit.id === it.id;
        // Выключенный пункт наработку не копит — гасим строку, как в мобильной админке.
        return <div key={it.id} className="svc"
          style={{ opacity: it.is_active ? 1 : 0.5, alignItems: editing ? 'flex-start' : undefined }}>
          <span className="sdot" style={{ background: it.is_active ? color : 'var(--faint)' }}></span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="snm">{it.label}{it.is_active ? '' : ' · выключен'}</div>
            <div className="sds">
              {Math.round(it.current || 0)} / {num(it.threshold)} {unit} · {Math.round(pct)}% · {word}
            </div>
            <div style={{ marginTop: 7 }}><Bar pct={pct} h={5} color={it.is_active ? color : 'var(--faint)'} /></div>
            {editing ? <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <input className="inp mono" style={{ flex: 1 }} type="number" autoFocus value={edit.val}
                onChange={(e) => setEdit({ id: it.id, val: e.target.value })} />
              <ABtn kind="sm soft" disabled={String(edit.val).trim() === ''} onClick={async () => {
                if (await act({ action: 'threshold', id: it.id, threshold: edit.val },
                  `Порог «${it.label}» изменён`)) setEdit(null);
              }}>OK</ABtn>
              <ABtn kind="sm line" onClick={() => setEdit(null)}>×</ABtn>
            </div> : null}
          </div>
          {/* Смена типа триггера — сразу выпадающим списком: сервер принимает только
              hours/prints/days, поэтому список закрытый и опечатка невозможна. */}
          <select className="sel" style={{ width: 110 }} value={it.trigger}
            title="Тип триггера наработки"
            onChange={(e) => act({ action: 'type', id: it.id, trigger_type: e.target.value },
              `«${it.label}» — триггер изменён`)}>
            <option value="hours">часы</option>
            <option value="prints">печати</option>
            <option value="days">дни</option>
          </select>
          <ABtn kind="sm line" title="Изменить порог наработки"
            onClick={() => setEdit(editing ? null : { id: it.id, val: String(it.threshold ?? '') })}>
            Порог
          </ABtn>
          <ABtn kind="sm line" title={it.is_active ? 'Не напоминать об этом узле' : 'Снова считать наработку'}
            onClick={() => act({ action: 'toggle', id: it.id },
              it.is_active ? `«${it.label}» выключен` : `«${it.label}» включён`)}>
            {it.is_active ? 'Выкл' : 'Вкл'}
          </ABtn>
          {/* Отметка «обслужено» обнуляет накопленную наработку — вернуть её нечем, спрашиваем. */}
          <ABtn kind="sm line" title="Отметить обслуженным" onClick={() => ask({
            title: 'Отметить обслуженным?',
            text: `Счётчики «${it.label}» обнулятся, отсчёт до следующего ТО начнётся заново.`,
            cta: '✓ Обслужено', tone: 'ok',        // тоже позитивное подтверждение
            onYes: () => act({ action: 'done', id: it.id }, `«${it.label}» — счётчик сброшен`),
          })}>✓</ABtn>
          <button className="db sm line" title="Удалить пункт" style={{ color: 'var(--red)' }}
            onClick={() => confirm(
              'Удалить пункт ТО?',
              `Пункт «${it.label}» и его наработка будут удалены безвозвратно.`,
              () => act({ action: 'del', id: it.id }, `«${it.label}» удалён`, 'warn2'),
            )}><DIcon n="trash" size={14} /></button>
        </div>;
      }) : <Empty ic="wrench" t="Пунктов ТО нет" ds="Добавьте узел, чтобы считать наработку" />}

      {add ? <div className="fgap" style={{ marginTop: 14 }}>
        <Field l="Название узла">
          <input className="inp" value={add.label} autoFocus
            onChange={(e) => setAdd({ ...add, label: e.target.value })} />
        </Field>
        <div className="frow">
          <Field l="Триггер">
            <select className="sel" value={add.trigger_type}
              onChange={(e) => setAdd({ ...add, trigger_type: e.target.value })}>
              <option value="hours">часы печати</option>
              <option value="prints">печати</option>
              <option value="days">дни</option>
            </select>
          </Field>
          <Field l="Порог">
            <input className="inp mono" type="number" value={add.threshold}
              onChange={(e) => setAdd({ ...add, threshold: e.target.value })} />
          </Field>
        </div>
        <div className="frow">
          <ABtn kind="line" onClick={() => setAdd(null)}>Отмена</ABtn>
          <ABtn disabled={!add.label.trim() || !Number(add.threshold)} onClick={async () => {
            const ok = await act({
              action: 'add', label: add.label.trim(),
              trigger_type: add.trigger_type, threshold: add.threshold,
            }, 'Узел добавлен');
            if (ok) setAdd(null);
          }}>Добавить</ABtn>
        </div>
      </div> : <button className="db line full" style={{ marginTop: 12 }}
        onClick={() => setAdd({ label: '', trigger_type: 'hours', threshold: '' })}>
        <DIcon n="plus" size={15} />Добавить узел</button>}
    </>;
  }}</Screen>;
}

function SelfTab({ nav }) {
  const { toast } = useContext(AppCtx);
  const plastics = useApi('adminPlastics', [], []);
  const mine = useApi('adminSelfOrders', [], []);
  const [f, setF] = useState({ label: '', plastic_id: '', grams: '', minutes: '' });
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  return <>
    <div className="fgap" style={{ marginBottom: 16 }}>
      <Field l="Пластик">
        <select className="sel" value={f.plastic_id} onChange={(e) => set('plastic_id', e.target.value)}>
          <option value="">—</option>
          {((plastics.data && plastics.data.plastics) || []).map((p) =>
            <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <div className="frow">
        <Field l="Граммы">
          <input className="inp mono" type="number" placeholder="18"
            value={f.grams} onChange={(e) => set('grams', e.target.value)} />
        </Field>
        <Field l="Минуты">
          <input className="inp mono" type="number" placeholder="необяз."
            value={f.minutes} onChange={(e) => set('minutes', e.target.value)} />
        </Field>
      </div>
      <Field l="Название">
        <input className="inp" placeholder="что печатали"
          value={f.label} onChange={(e) => set('label', e.target.value)} />
      </Field>
      <ABtn kind="full" disabled={!f.label.trim()} onClick={async () => {
        await call('adminSelfPrint', {
          label: f.label.trim(), plastic_id: Number(f.plastic_id) || 0,
          grams: Number(f.grams) || 0, minutes: Number(f.minutes) || 0,
        });
        toast('Записано в журнал', { kind: 'ok' });
        setF({ label: '', plastic_id: '', grams: '', minutes: '' });
        mine.reload();
      }}>Записать печать</ABtn>
      <div className="hint" style={{ marginTop: -4 }}>
        списывает склад и наработку ТО, не попадает в клиентскую статистику
      </div>
    </div>

    <div className="lab" style={{ marginBottom: 6 }}>Мои самопечати</div>
    <Screen state={mine}>{(d) => {
      const list = d.orders || [];
      if (!list.length) return <Empty ic="printer" t="Самопечатей пока нет" />;
      return list.map((o) => <div key={o.id} className="qrow" onClick={() => nav('orders', { sel: o.id })}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="nm">{o.label || 'Без названия'}</div>
          <div className="ds">
            {[o.plastic, o.grams ? fmtG(o.grams) : '', o.minutes ? fmtMin(o.minutes) : '']
              .filter(Boolean).join(' · ')}
          </div>
        </div>
        <StBadge st={o.status} />
      </div>);
    }}</Screen>
  </>;
}
