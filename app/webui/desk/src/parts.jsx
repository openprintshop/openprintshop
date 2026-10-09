// parts.jsx — части, общие для экранов. В прототипе они были скопированы между файлами
// (очередь печати и телеметрия — в Пульте и в Печати), здесь сведены в одно место.

import { DIcon } from './icons.jsx';
import { AppCtx, Empty, Bar, Spark } from './ui.jsx';
import { minutes, num } from './fmt.jsx';

const { useContext, useState, useEffect, useRef } = React;

export function Loading({ what = 'Загружаю…' }) {
  return <div className="ldr"><span className="spin" />{what}</div>;
}

/**
 * Квадратная миниатюра заказа с фолбэком на иконку принтера. Заказы мастера/индивидуальные
 * бывают без рендера — тогда вместо пустого квадрата показываем иконку (как в списке заказов).
 * При битой ссылке (превью удалено) тоже откатываемся на иконку, а не на «сломанную картинку».
 */
export function OrderThumb({ src, size = 52 }) {
  const [err, setErr] = useState(false);
  const box = { width: size, height: size, borderRadius: Math.round(size * 0.21), flex: '0 0 auto' };
  if (src && !err) {
    return <img src={src} alt="" style={{ ...box, objectFit: 'cover' }} onError={() => setErr(true)} />;
  }
  return <div style={{ ...box, background: 'var(--bg)', display: 'flex',
    alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
    <DIcon n="printer" size={Math.round(size * 0.4)} />
  </div>;
}

export function ErrBox({ err, onRetry }) {
  return <div className="errbox">
    <DIcon n="bell" size={22} style={{ color: 'var(--red)' }} />
    <div style={{ flex: 1 }}>
      <div style={{ fontWeight: 600, color: 'var(--text)' }}>Не получилось загрузить</div>
      <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 3 }}>{err}</div>
    </div>
    {onRetry && <button className="db soft sm" onClick={onRetry}>Ещё раз</button>}
  </div>;
}

/** Обёртка «загрузка / ошибка / данные» — чтобы каждый экран не писал это заново. */
export function Screen({ state, children, empty }) {
  if (state.loading && !state.data) return <Loading />;
  if (state.err && !state.data) return <ErrBox err={state.err} onRetry={state.reload} />;
  if (!state.data) return empty || <Empty t="Нет данных" />;
  return children(state.data);
}

/**
 * Чипы-фильтры со счётчиками над списком — разметка макета: .ms-chips + .chip + .chip .n.
 * Элемент: {k, l, n, ic} — ключ, подпись, счётчик, необязательная иконка.
 */
export function Chips({ items, value, onChange, style }) {
  // c.right — прижать чип к правому краю строки (первый такой забирает свободное место).
  // Так отделяют переключатель другого смысла от обычных фильтров: «Все | Новые … | Для себя».
  const firstRight = items.findIndex((c) => c.right);
  return <div className="ms-chips" style={style}>
    {items.map((c, i) => <button key={c.k}
      className={'chip' + (value === c.k ? ' on' : '')}
      style={i === firstRight ? { marginLeft: 'auto' } : undefined}
      onClick={() => onChange(c.k)}>
      {c.ic ? <DIcon n={c.ic} size={13} /> : null}
      {c.l}{typeof c.n === 'number' ? <span className="n">{c.n}</span> : null}
    </button>)}
  </div>;
}

// Поле внутри .tb-search в макете прозрачное и заданное инлайном — своего класса под него нет.
const SRCH_INPUT = { flex: 1, minWidth: 0, background: 'none', border: 0, outline: 'none', color: 'var(--text)', font: 'inherit' };

/** Поисковая строка списка (.tb-search из макета). Фокусируется по «/». */
export function Search({ value, onChange, placeholder = 'Поиск…' }) {
  return <div className="tb-search" style={{ flex: 1, margin: 0 }}>
    <DIcon n="search" size={16} />
    <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} style={SRCH_INPUT} />
    {value ? <button className="ib sm" onClick={() => onChange('')}><DIcon n="close" size={14} /></button> : null}
  </div>;
}

/** Раскладка «список слева, деталь справа». width — ширина списка на широком экране. */
export function Split({ width = 1130, list, detail }) {
  return <div className="split">
    <div className="ms" style={{ '--ms-w': width + 'px' }}>{list}</div>
    <div className="det">{detail}</div>
  </div>;
}

/**
 * Тумблер. С подписью — это целая строка .togrow из макета (иконка, заголовок, .ds, справа .sw);
 * без подписи остаётся голый .sw, чтобы его можно было вставить в чужую строку.
 */
export function Toggle({ on, onChange, label, ds, ic, children }) {
  const sw = <div className={'sw' + (on ? ' on' : '')} onClick={() => onChange(!on)} title={label}><i /></div>;
  if (!children && !ds && !ic) return sw;
  return <div className="togrow">
    {ic ? <DIcon n={ic} size={16} style={{ color: 'var(--accent)', flexShrink: 0, marginRight: 2 }} /> : null}
    <div style={{ flex: 1 }}>{children || label}{ds ? <div className="ds">{ds}</div> : null}</div>
    {sw}
  </div>;
}

/** Кнопка-действие с состоянием «выполняется». */
export function ABtn({ children, onClick, kind = '', icon, disabled, title, style }) {
  const [busy, setBusy] = useState(false);
  return <button className={'db ' + kind} title={title} style={style} disabled={busy || disabled}
    onClick={async () => {
      setBusy(true);
      try { await onClick(); } finally { setBusy(false); }
    }}>
    {busy ? <span className="spin sm" /> : (icon ? <DIcon n={icon} size={15} /> : null)}
    {children}
  </button>;
}

/** Очередь печати — разметка макета: .qrow с .pos, миниатюрой, .nm/.ds и стрелками .arr. */
export function QueueList({ items, onMove, onOpen }) {
  if (!items || !items.length) return <Empty ic="queue" t="Очередь пуста" ds="Все заказы напечатаны" />;
  return <>
    {items.map((q, i) => <div key={q.id} className="qrow"
      onClick={() => onOpen && onOpen(q.id)}>
      <div className="pos">{i + 1}</div>
      {/* слот под миниатюру занимаем всегда: без него строка без превью съезжает влево.
          Нет ни рендера, ни фото результата → иконка принтера, а не пустой тёмный квадрат. */}
      {q.preview
        ? <img src={q.preview} alt="" onError={(e) => { e.target.style.display = 'none'; }} />
        : <div style={{ width: 44, height: 44, borderRadius: 9, background: 'var(--bg)', flex: '0 0 auto',
            display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
            <DIcon n="printer" size={18} />
          </div>}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="nm">#{q.id} · {q.label || q.model_label || 'Без названия'}</div>
        <div className="ds">
          {q.user || ''}
          {q.minutes ? ' · ' + minutes(q.minutes) : ''}
          {q.eta ? <span style={{ color: 'var(--amber)' }}> · до {q.eta}</span> : null}
        </div>
      </div>
      {onMove && <div className="arr" onClick={(e) => e.stopPropagation()}>
        <button disabled={i === 0} onClick={() => onMove(q.id, 'up')} title="Выше в очереди">▲</button>
        <button disabled={i === items.length - 1} onClick={() => onMove(q.id, 'down')} title="Ниже">▼</button>
      </div>}
    </div>)}
  </>;
}

/**
 * Приведение ответа /api/admin/printer_status к плоскому виду.
 *
 * Форма ответа вложенная и «клипперовская»: прогресс лежит в state.virtual_sdcard.progress
 * долей 0..1, температуры — в summary, а человекочитаемые «осталось/ETA» досчитывает сервер.
 * Экраны не должны знать этих подробностей.
 */
export function normPrinter(res) {
  if (!res) return null;
  const st = res.state || {};
  const sum = res.summary || {};
  const exr = res.extras || {};
  const ps = st.print_stats || {};
  const vs = st.virtual_sdcard || {};
  const pst = st.print_status || {};
  const gm = st.gcode_move || {};
  const ms = st.machine_status || {};
  const info = st.info || {};
  // Способности принтера (цикл 3, волна 3). Сервер отдаёт их блоком `driver` — в том числе в
  // ОТКАЗЕ «телеметрию этот принтер не отдаёт» (ok:false + driver). Экраны обязаны гейтить
  // кнопки по ним, а не по online/printing: иначе кнопка есть всегда, а «не умею» выясняется
  // нажатием — ровно та скрытая связка, ради которой слой драйверов и писался.
  const drv = res.driver || {};
  const caps = drv.caps || {};
  return {
    online: !!res.online,
    serial: res.serial || '',
    // чем разговариваем и что эта машина умеет ПРЯМО СЕЙЧАС
    kind: drv.kind || '',
    title: drv.title || '',
    driverEnabled: drv.enabled !== false,
    note: drv.note || '',
    caps,
    // Способности через can(): экран не должен знать, что у ответа без `driver` (старый бот,
    // переходный период до рестарта) флагов нет вовсе — тогда работаем как раньше, «умею всё».
    can: (name) => (res.driver ? !!caps[name] : true),
    uploadExts: drv.upload_exts || [],
    state: ps.state || (res.online ? 'standby' : 'offline'),
    printing: ps.state === 'printing',
    progress: Math.max(0, Math.min(100,
      vs.progress != null ? Number(vs.progress) * 100 : (Number(ms.progress) || 0))),
    remaining: st.remaining || '',
    remainingSec: Number(pst.remaining_time_sec) || 0,
    elapsed: st.elapsed || '',
    eta: st.eta_at || '',
    startedAt: st.started_at || '',
    nozzle: sum.extruder_temp, nozzleTarget: sum.extruder_target,
    bed: sum.bed_temp, bedTarget: sum.bed_target,
    layer: Number(pst.current_layer ?? info.current_layer) || 0,
    speed: gm.speed ? Math.round(Number(gm.speed) / 60) : 0,   // мм/мин → мм/с
    // обдув приходит долей 0..1 и не у всех прошивок — без него ячейка телеметрии просто пустая
    fan: st.fan && st.fan.speed != null ? Math.round(Number(st.fan.speed) * 100) : null,
    z: gm.z,
    // extras с бэкенда (elegoo.extras, разведано захватами docs/captures/): поля появляются
    // по мере вещания принтера — null/пусто значит «принтер этого ещё не сообщал», UI молчит.
    chamber: exr.chamber_temp != null ? exr.chamber_temp : null,   // датчик камеры у CC2 ЕСТЬ
    light: exr.light != null ? !!exr.light : null,                 // реальное состояние подсветки
    filament: exr.filament_detected != null ? !!exr.filament_detected : null,
    filamentSensorOn: exr.filament_sensor_on != null ? !!exr.filament_sensor_on : null,
    fans: exr.fans || null,                       // {model, aux, box, controller, heater} в %
    pos: exr.position || null,                    // {x, y, z} мм
    homedAxes: exr.homed_axes || '',
    uDisk: exr.u_disk != null ? !!exr.u_disk : null,
    exceptions: exr.exceptions || [],             // ошибки принтера (machine_status)
    slots: exr.slots || [],                       // слоты CANVAS: {slot, brand, name, type, color, temp_min, temp_max}
    activeSlot: exr.active_slot != null ? exr.active_slot : null,  // 0 = ничего не подаётся
    // Состояние машины по таблице вендора (elegoo.machine_phase): «Автовыравнивание стола»,
    // «Печать · Прогрев стола»… null — принтер его ещё не прислал.
    machine: exr.machine || null,
    // Пауза ИЗ ТЕЛЕМЕТРИИ (sub_status 2501/2502/2505). null — не знаем, тогда экраны держат
    // своё локальное состояние кнопки, как раньше.
    paused: exr.machine ? !!exr.machine.paused : null,
    speedMode: exr.speed_mode != null ? exr.speed_mode : null,   // 0 тихий … 3 безумный
    autoRefill: exr.auto_refill != null ? !!exr.auto_refill : null,
    // Прошивка сообщества CC2, если драйвер её нашёл: {firmware, control, caps}.
    community: drv.community || null,
    // «богатая шапка»: заказ, к которому привязана печать (бэкенд отдаёт job/bound)
    filename: ps.filename || (res.job && res.job.label) || '',
    plastic: st.plastic || (res.job && res.job.plastic) || '',
    orderId: st.order_id || (res.job && res.job.order_id) || (res.bound && res.bound.rid) || 0,
    orderLabel: (res.bound && res.bound.label) || (res.job && res.job.label) || '',
    // миниатюра заказа для карточки принтера (bound.preview или thumb сетевой печати)
    orderPreview: (res.bound && res.bound.preview) || (res.job && res.job.thumb) || '',
    grams: res.job && res.job.grams,
    cameraUrl: res.camera_url || '',
    // connected — живость MQTT-соединения подписчика: пока оно есть, кэш актуален, даже если
    // последний кадр старый (в простое принтер вещает редко). Старый возрастной критерий —
    // фолбэк для ответов без поля (переходный период до рестарта бота).
    stale: res.connected != null ? res.connected === false : (!!res.cached && Number(res.age) > 30),
  };
}

/** Строка «ключ — значение» из макета: .kv это одна СТРОКА, а не контейнер. */
export function KV({ k, v, mono = true }) {
  return <div className="kv"><span className="k">{k}</span>
    <span className={'v' + (mono ? ' mono' : '')}>{v}</span></div>;
}

/**
 * Телеметрия принтера — сетка ячеек, как в макете (.tele .cell / .k / .v).
 *
 * Датчик температуры камеры у Centauri Carbon 2 ЕСТЬ (ztemperature_sensor, разведано захватом
 * docs/captures/broadcast-idle-2026-07-20.json). В широкой сетке Пульта (cols=4) камеру
 * показываем подписью под «Столом», чтобы не ломать сетку 4-в-ряд; в узкой колонке «Печати»
 * (cols=2) камера — своя ячейка со спарклайном, парой к ней — «Филамент» (датчик прутка).
 * Историю температур копим из самого опроса: своего эндпоинта под графики у бота нет.
 */
export function PrinterTele({ pr, history, cols = 4 }) {
  if (!pr || !pr.online) return <div className="tele-empty">Нет связи с принтером</div>;
  const h = history || {};
  const t = (v, u = '°') => (v == null ? '—' : Math.round(v) + u);
  const two = cols === 2;
  const sw = two ? 230 : 150, sh = two ? 40 : 34;
  return <div className="tele" style={two ? { gridTemplateColumns: '1fr 1fr' } : undefined}>
    <div className="cell">
      <div className="k">Сопло</div>
      <div className="v">{t(pr.nozzle)}{pr.nozzleTarget ? <span> / {Math.round(pr.nozzleTarget)}°</span> : null}</div>
      {h.noz && h.noz.length > 1 ? <Spark data={h.noz} w={sw} h={sh} color="var(--accent)" /> : null}
    </div>
    <div className="cell">
      <div className="k">Стол</div>
      <div className="v">{t(pr.bed)}{pr.bedTarget ? <span> / {Math.round(pr.bedTarget)}°</span> : null}</div>
      {h.bed && h.bed.length > 1 ? <Spark data={h.bed} w={sw} h={sh} color="var(--amber)" /> : null}
      {!two && pr.chamber != null ? <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 8 }}>
        камера {t(pr.chamber)}
      </div> : null}
    </div>
    {two ? <div className="cell">
      <div className="k">Камера</div>
      <div className="v">{t(pr.chamber)}</div>
      {h.cham && h.cham.length > 1 ? <Spark data={h.cham} w={sw} h={sh} color="var(--violet)" /> : null}
    </div> : null}
    {two ? <div className="cell">
      <div className="k">Филамент</div>
      <div className="v" style={pr.filament === false ? { color: 'var(--amber)' } : undefined}>
        {pr.filament == null ? '—' : (pr.filament ? 'есть' : 'нет')}
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 8 }}>
        {pr.filamentSensorOn == null ? 'датчик прутка' : ('датчик ' + (pr.filamentSensorOn ? 'включён' : 'выключен'))}
      </div>
    </div> : null}
    <div className="cell">
      <div className="k">Слой</div>
      <div className="v">{pr.layer || '—'}</div>
      <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 8 }}>
        {pr.z != null ? `высота ${Number(pr.z).toFixed(1)} мм` : ''}
      </div>
    </div>
    <div className="cell">
      <div className="k">Скорость</div>
      <div className="v">{pr.speed || '—'}<span> мм/с</span></div>
      <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 8 }}>
        {/* пластик показан в блоке филамента — здесь, как в макете, обдув */}
        {pr.fan != null ? `обдув ${pr.fan}%` : ''}
      </div>
    </div>
  </div>;
}

/**
 * Камера принтера с фолбэком, как в мобильной админке.
 *
 * MJPEG-поток в WebView через туннель регулярно «застывает» или обрывается — та самая жалоба
 * «спустя время видео пропадает». Раньше десктоп просто грузил поток в <img> и молча ловил
 * битый кадр. Теперь: сначала поток (mode=stream), при ошибке — переключаемся на покадровый
 * опрос (mode=snap, сервер отдаёт по одному JPEG), а если и он не отвечает — показываем причину
 * (cameraCheck) и кнопку «Повторить». Кадр тикаем принудительно: в snap это единственный способ
 * обновления, в stream — восстановление после зависания.
 *
 * fit='contain' (по умолчанию) не обрезает картинку: показываем кадр целиком в фиксированной
 * рамке, letterbox по краям. fit='cover' — заполнить рамку с обрезкой.
 */
export function Cam({ pr, fit = 'contain', className = 'cam', style, children }) {
  const [mode, setMode] = useState('stream');
  const [err, setErr] = useState(false);
  const [diag, setDiag] = useState(null);
  const [nonce, setNonce] = useState(0);
  const has = pr && pr.cameraUrl && window.API.cameraSrc;
  useEffect(() => {
    const iv = setInterval(() => setNonce((n) => n + 1), mode === 'snap' ? 3000 : 6000);
    return () => clearInterval(iv);
  }, [mode]);
  // фон под letterbox — сплошной тёмный, иначе сквозь contain просвечивает полосатая заглушка
  const boxStyle = fit === 'contain' ? { background: '#0a0e13', ...style } : style;

  if (!has) {
    return <div className={className} style={boxStyle}>
      <div className="ph"><DIcon n="camera" size={26} />Камера не настроена
        <span style={{ fontSize: 10.5, opacity: 0.7 }}>включите камеру на экране принтера · ELEGOO_PRINTER_IP в .env</span>
      </div>
    </div>;
  }
  if (err) {
    return <div className={className} style={boxStyle}>
      <div className="ph"><DIcon n="camera" size={26} />Камера недоступна
        {diag ? <span style={{ fontSize: 10, opacity: 0.7, maxWidth: 260, wordBreak: 'break-all', textAlign: 'center' }}>
          {(diag.url ? diag.url + ' — ' : '') + (diag.error || 'нет ответа')}
        </span> : null}
        <button className="db soft sm" style={{ marginTop: 8 }}
          onClick={() => { setErr(false); setDiag(null); setMode('stream'); setNonce((n) => n + 1); }}>
          Повторить
        </button>
      </div>
    </div>;
  }
  const src = window.API.cameraSrc(nonce) + (mode === 'snap' ? '&mode=snap&t=' + nonce : '');
  const onErr = () => {
    if (mode === 'stream') setMode('snap');   // поток не пошёл — пробуем покадровый режим
    else { setErr(true); Promise.resolve(window.API.cameraCheck && window.API.cameraCheck()).then(setDiag).catch(() => setDiag(null)); }
  };
  return <div className={className} style={boxStyle}>
    <img key={mode} src={src} alt="камера принтера" onError={onErr}
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: fit }} />
    {children}
  </div>;
}

/** Накопление истории температур для спарклайнов: бот их не хранит, копим из опроса. */
export function useTeleHistory(pr) {
  const ref = useRef({ noz: [], bed: [], cham: [] });
  const [, force] = useState(0);
  useEffect(() => {
    if (!pr || pr.nozzle == null) return;
    const h = ref.current;
    h.noz = h.noz.concat([Number(pr.nozzle) || 0]).slice(-40);
    h.bed = h.bed.concat([Number(pr.bed) || 0]).slice(-40);
    // температура камеры вещается не в каждом снимке — ряд копим только по реальным значениям,
    // иначе спарклайн проваливался бы в ноль на каждом пропуске
    if (pr.chamber != null) h.cham = h.cham.concat([Number(pr.chamber) || 0]).slice(-40);
    force((n) => n + 1);
  }, [pr && pr.nozzle, pr && pr.bed, pr && pr.chamber]);   // eslint-disable-line
  return ref.current;
}

/** Прогресс текущей печати. */
export function PrintProgress({ pr, onNav }) {
  if (!pr || !pr.printing) {
    return <div className="tele-empty">
      {pr && pr.online ? 'Принтер свободен' : 'Нет связи с принтером'}
    </div>;
  }
  const pct = Math.round(pr.progress || 0);
  return <div className="pprog" onClick={onNav} style={{ cursor: onNav ? 'pointer' : 'default' }}>
    <div className="pph">
      <span className="mono big">{pct}%</span>
      {pr.remaining ? <span className="left">осталось {pr.remaining}</span> : null}
    </div>
    <Bar pct={pct} />
    <div className="pfn">
      {pr.elapsed ? `идёт ${pr.elapsed}` : ''}
      {pr.eta ? ` · закончит ${pr.eta}` : ''}
    </div>
  </div>;
}

/** Шапка карточки детали — разметка макета (.od-head с .tt/.id/.sub/.price). */
export function DetHead({ id, title, sub, price, right }) {
  return <div className="od-head">
    <div style={{ minWidth: 0, flex: 1 }}>
      <div className="tt">{id ? <span className="id">{id}</span> : null}{title}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
    {price ? <div className="price">{price}</div> : null}
    {right}
  </div>;
}

/** Заглушка «выберите строку». */
export function PickOne({ what = 'запись' }) {
  return <div className="pickone">
    <DIcon n="chev" size={26} style={{ color: 'var(--faint)', transform: 'rotate(180deg)' }} />
    <div>Выберите {what} слева</div>
    <div className="kbdhint"><span className="kbd">j</span><span className="kbd">k</span> — по списку,
      <span className="kbd">/</span> — поиск</div>
  </div>;
}

/** Автопрокрутка вниз при появлении новых сообщений. */
export function useAutoScroll(dep) {
  const ref = useRef();
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [dep]);
  return ref;
}

// Больше 12 МБ Telegram всё равно не примет, а браузер до отправки успеет съесть память
// на base64 — режем на входе, одинаково для файла, вставки и перетаскивания.
const MAX_IMG = 12 * 1024 * 1024;

/** Файл-картинка из события вставки (Ctrl+V). null — в буфере был обычный текст. */
export function pastedImage(e) {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  for (let i = 0; i < items.length; i++) {
    if (items[i].kind === 'file' && String(items[i].type || '').startsWith('image/')) {
      const f = items[i].getAsFile();
      if (f) return f;
    }
  }
  return null;
}

/**
 * Прочитать картинку в data:-строку — в таком виде её принимают adminOrderMessage и
 * adminClientMessage. Слишком большой файл отбиваем сразу: понятная ошибка лучше, чем
 * молчаливый отказ Telegram после отправки.
 */
export function readImage(file) {
  return new Promise((res, rej) => {
    if (!file) { rej(new Error('это не картинка')); return; }
    if (file.size > MAX_IMG) { rej(new Error('картинка больше 12 МБ')); return; }
    const rd = new FileReader();
    rd.onload = () => res(rd.result);
    rd.onerror = () => rej(new Error('файл не прочитался'));
    rd.readAsDataURL(file);
  });
}

/**
 * Полоска с прикреплённой картинкой над полем ввода: пока она видна, отправка уйдёт
 * с картинкой. Без такого предпросмотра вставка Ctrl+V была бы отправкой вслепую —
 * промахнулся буфером, и клиент уже получил чужой скриншот.
 */
export function Attached({ src, onDrop }) {
  if (!src) return null;
  return <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px' }}>
    <img src={src} alt="" style={{ width: 54, height: 54, objectFit: 'cover', borderRadius: 9 }} />
    <div style={{ flex: 1, minWidth: 0, fontSize: 13, color: 'var(--muted)' }}>
      Картинка прикреплена — уйдёт вместе с сообщением
    </div>
    <button className="ib" title="Убрать картинку" onClick={onDrop}><DIcon n="close" size={15} /></button>
  </div>;
}

/** Сообщение из одних эмодзи — обычно реакция, приехавшая из Telegram: показываем крупно. */
export function soloEmoji(t) {
  const s = String(t || '').trim();
  return !!s && s.length <= 12 && !/[\wа-яё]/i.test(s);
}

/**
 * Пузырь переписки — общий для чата заказа и сплошного чата клиента (раньше эта разметка
 * жила в двух экранах копиями и успела разъехаться на мелочах).
 *
 * `m.reply` — на что отвечают: реакция клиента приходит отдельным сообщением-эмодзи,
 * привязанным к тому сообщению, на которое её поставили, и без цитаты она была бы
 * непонятным сердечком в пустоте.
 *
 * Галочка у СВОИХ сообщений — наша отметка, а не телеграмная: Bot API не сообщает боту,
 * что человек прочитал сообщение. ✓✓ значит «клиент открыл переписку или ответил из
 * Telegram», ✓ — «отправлено».
 */
export function Bubble({ m, onImage }) {
  const solo = soloEmoji(m.text) && !m.image;
  return <div className={'bub' + (m.mine ? ' me' : '')}>
    {m.reply ? <div className="bq">{m.reply.text || (m.reply.image ? '📷 фото' : 'сообщение')}</div> : null}
    {m.image ? <img className="bimg" src={m.image} alt="" onClick={() => onImage && onImage(m.image)} /> : null}
    {m.text ? <div className={solo ? 'bemoji' : ''}>{m.text}</div> : null}
    <div className="bt">
      {m.at}
      {m.mine ? <span className={'rd' + (m.read ? ' on' : '')}
        title={m.read ? 'Прочитано' : 'Отправлено, ещё не прочитано'}>{m.read ? '✓✓' : '✓'}</span> : null}
    </div>
  </div>;
}

/** Подтверждение опасного действия. На бэкенде подтверждений нет — UI единственный барьер. */
/**
 * Диалог подтверждения. danger=true (по умолчанию) — красная кнопка: действие необратимо
 * (удаление, остановка печати). Для обычных действий (запустить печать, выгрузить филамент)
 * передавайте danger=false — иначе красный перестаёт значить «осторожно» и мозолит глаза там,
 * где ничего страшного не происходит.
 */
export function useConfirm() {
  const { ask } = useContext(AppCtx);
  return (title, text, onYes, cta = 'Удалить', danger = true) =>
    ask({ title, text, cta, danger, onYes });
}

export const fmtCount = (n, w) => `${num(n)} ${w}`;
