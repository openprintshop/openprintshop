// pult.jsx — «Пульт»: живая сводка по мастерской. Ежедневный экран мастера.
// Структура и классы — из макета (dd-pult.jsx), данные — живые.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty, Bar } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { money, num, ago, minutes as fmtMin, grams } from '../fmt.jsx';
import { QueueList, PrinterTele, useTeleHistory, Loading, useConfirm, Cam, OrderThumb } from '../parts.jsx';
import { CalcDrawer, hasCalc } from './calcview.jsx';
import { QuickActions, machineText } from './printext.jsx';

const { useContext, useState, useEffect, useCallback } = React;

// Как называется машина. Раньше здесь стояла константа 'Elegoo Centauri Carbon 2' с
// пометкой «модель бот нигде не отдаёт» — с двумя принтерами она стала бы подписью чужой
// машины под её же телеметрией. Сервер отдаёт название драйвера в блоке driver (волна 3).
// Пусто, пока бот не назвал машину: подпись «Принтер · принтер» — не информация, а шум.
const printerModel = (pr) => (pr && pr.title) || '';

// Свежее событие подсвечиваем (.ev.hot). Порог — чуть больше периода поллинга ленты,
// чтобы новинка успела «мигнуть» хотя бы один цикл обновления.
const HOT_SEC = 90;

const SIG = [
  { k: 'new_orders', flt: 'new', ic: 'receipt', c: 'var(--accent)', tt: 'новых заказов', ds: 'ждут ответа мастера' },
  { k: 'unread', flt: 'msg', ic: 'chat', c: 'var(--blue)', tt: 'сообщений от клиентов', ds: 'непрочитанные в чатах' },
  { k: 'price_wait', flt: 'price', ic: 'coin', c: 'var(--violet)', tt: 'ждут по цене', ds: 'клиент не подтвердил' },
];

// Слоты хаба для выгрузки филамента: сервер сам считает tray_id = слот−1 (elegoo.unload_filament).
const HUB_SLOTS = [1, 2, 3, 4];

/**
 * Честный текст об исходе команды принтеру.
 *
 * Centauri Carbon 2 шлёт unicast-ответы только «привязанному» клиенту-слайсеру, поэтому наши
 * команды он выполняет, но подтверждения мы не видим: elegoo.request(command=True) отдаёт
 * {ok:true, unconfirmed:true}. Раньше пульт в этом случае писал просто «Готово» — то есть врал
 * мастеру об исходе. Отказ ({ok:false}) сюда не доходит: call() на нём бросает исключение.
 */
function cmdText(res, ok) {
  if (res && res.unconfirmed) return ok + ' — отправлено (принтер не подтверждает выполнение)';
  // err_code принтер кладёт рядом с ok=true крайне редко (нештатная прошивка), но если он есть —
  // это отказ, и молчать о нём нельзя.
  if (res && res.err_code != null) return 'Принтер ответил кодом ' + res.err_code;
  return ok;
}

// Цвет точки события выводим из текста: отдельного поля типа у события нет.
function evColor(a) {
  const s = String(a || '');
  if (/заказ/i.test(s)) return 'var(--accent)';
  if (/сообщ|чат/i.test(s)) return 'var(--blue)';
  if (/оплат|баланс|пополн/i.test(s)) return 'var(--price)';
  if (/печат/i.test(s)) return 'var(--amber)';
  return 'var(--faint)';
}

export function PultScreen() {
  const { hub, printer, nav, can } = useContext(AppCtx);
  if (!hub) return <Loading what="Собираю сводку…" />;
  // Две карточки «Пульта» держатся на правах, которых у ОПЕРАТОРА нет, и обе ломались молча:
  // выручка (adminDashboard → «Деньги») рисовала пустой график и нули, а лента событий
  // (adminActivity → «Клиенты») показывала «Пока тихо» — то есть ВРАЛА, будто клиенты
  // ничего не делают. Прятать пустые места нельзя: сетка пульта фиксированная, дырка в ней
  // выглядит поломкой. Поэтому оператор получает в тех же слотах то, что ему по правам
  // положено и по работе нужно, — счётчики работы и ленту последних печатей.
  return <div className="pult">
    <PrinterCard printer={printer} nav={nav} />
    <AttCard hub={hub} nav={nav} />
    {can('finance') ? <RevCard hub={hub} nav={nav} /> : <WorkCard hub={hub} nav={nav} />}
    <QueueCard nav={nav} />
    <TasksCard nav={nav} />
    {can('clients') ? <EventsCard nav={nav} /> : <PrintsCard nav={nav} />}
  </div>;
}

// Камера пульта: общий компонент с фолбэком stream→snap (parts.Cam) + оверлеи пульта.
// fit='contain' — картинка целиком в фиксированной рамке, ничего не обрезается.
function PultCam({ pr }) {
  return <Cam pr={pr} fit="contain">
    <div className="ovl">
      {pr && pr.printing ? <span className="tag live"><i></i>LIVE</span> : null}
      {pr && pr.elapsed ? <span className="tag">{pr.elapsed}</span> : null}
    </div>
    {pr && pr.online ? <div className="ovr">
      <span className="tag">🔥 {Math.round(pr.nozzle || 0)}° / {Math.round(pr.bed || 0)}°</span>
    </div> : null}
    {pr && pr.layer ? <div className="ovb"><span className="tag">слой {pr.layer}</span></div> : null}
  </Cam>;
}

function PrinterCard({ printer, nav }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const pr = printer;
  const hist = useTeleHistory(pr);
  const pct = Math.round((pr && pr.progress) || 0);
  // Пауза — состояние UI: принтер её не сообщает (elegoo.enrich_status выводит только
  // «печатает»), поэтому флаг переворачиваем лишь после успешной команды. Свет принтер
  // ВЕЩАЕТ (led.status → pr.light) — локальный флаг лишь мостик до первой дельты телеметрии.
  // Пауза теперь приходит и из телеметрии (machine_status → pr.paused); локальный флаг — мостик,
  // пока принтер её не прислал.
  const [pausedLocal, setPaused] = useState(false);
  const paused = pr && pr.paused != null ? pr.paused : pausedLocal;
  const [light, setLight] = useState(false);
  // Какая команда сейчас в полёте — чтобы не долбить принтер повторным кликом по слоту.
  const [busy, setBusy] = useState('');

  /**
   * Отправка команды принтеру: результат мастеру показываем всегда — и успех (с оговоркой
   * про неподтверждённое выполнение), и отказ. Молчаливого «ничего не произошло» быть не должно.
   * Возвращает ответ сервера либо null, если команда не прошла.
   */
  const run = async (okText, fn) => {
    try {
      const res = await fn();
      const t = cmdText(res, okText);
      toast(t, { kind: res && res.err_code != null ? 'warn2' : 'ok', ms: t === okText ? 4000 : 8000 });
      return res && res.err_code != null ? null : res;
    } catch (e) {
      toast(e.message, { kind: 'warn2', ms: 9000 });
      return null;
    }
  };

  const cs = { background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 12, padding: '12px 14px' };
  const ks = { fontSize: 11, color: 'var(--faint)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5 };
  // Реальное состояние подсветки из телеметрии (led.status), когда принтер его вещал;
  // иначе — последнее наше переключение (см. комментарий у useState выше).
  const lightOn = pr && pr.light != null ? pr.light : light;

  return <div className="card pu-printer">
    <div className="lab">
      <DIcon n="printer" size={14} />Принтер{printerModel(pr) ? ` · ${printerModel(pr)}` : ''}
      <span className="r">
        <button className="db sm line" onClick={() => nav('print')}>Пульт печати<DIcon n="chev" size={14} /></button>
      </span>
    </div>

    <PultCam pr={pr} />

    {pr && pr.printing ? <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, margin: '18px 0 8px' }}>
        {/* миниатюра привязанного заказа 52×52 (фолбэк — иконка принтера, если превью нет) */}
        <OrderThumb src={pr.orderPreview} size={52} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 15.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {pr.orderId
              ? <a onClick={() => nav('orders', { sel: pr.orderId })} style={{ cursor: 'pointer' }}>
                  #{pr.orderId} · {pr.orderLabel || 'заказ'}
                </a>
              : 'Печать не привязана к заказу'}
          </div>
          <div className="mono" style={{ fontSize: 12, color: 'var(--faint)', marginTop: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {pr.filename || 'имя файла принтер не сообщает'}
          </div>
        </div>
        <div className="mono" style={{ fontSize: 34, fontWeight: 700, color: 'var(--accent)' }}>{pct}%</div>
      </div>
      <Bar pct={pct} h={10} />
      <div className="meta-line" style={{ marginTop: 10 }}>
        {pr.remaining ? <span><DIcon n="clock" size={13} style={{ verticalAlign: -2 }} /> осталось <b>~{pr.remaining}</b></span> : null}
        {pr.elapsed ? <span>прошло <b>{pr.elapsed}</b></span> : null}
        {pr.eta ? <span>финиш <b>{pr.eta}</b></span> : null}
      </div>
    </> : <div style={{ margin: '18px 0 8px', color: 'var(--muted)', fontSize: 14 }}>
      {pr && pr.online ? (machineText(pr) || 'Принтер свободен — печать не идёт')
        : (pr && pr.note) || (pr && !pr.can('status')
          ? `${printerModel(pr) || 'Этот принтер'}: телеметрию не отдаёт`
          : 'Нет связи с принтером')}
    </div>}

    <div style={{ marginTop: 16 }}><PrinterTele pr={pr} history={hist} /></div>

    {pr && pr.printing ? <>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 14 }}>
        <div style={cs}>
          <div style={ks}>Филамент на модель</div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, margin: '6px 0 8px' }}>
            {/* Бэкенд отдаёт только ОБЩИЙ вес задания (job.grams из заказа), израсходованное
                принтер не сообщает — поэтому расход оцениваем по прогрессу (и бар, конечно,
                совпадает с прогрессом печати). Оценочность помечена знаком «≈». */}
            <span className="mono" style={{ fontSize: 18, fontWeight: 700 }}>
              {pr.grams ? '≈ ' + grams(pr.grams * pct / 100) : '—'}
            </span>
            <span style={{ fontSize: 12, color: 'var(--faint)' }}>
              {pr.grams ? 'из ' + grams(pr.grams) : (pr.plastic || 'заказ не привязан')}
            </span>
          </div>
          <Bar pct={pct} h={6} color="var(--violet)" />
        </div>
        <div style={cs}>
          <div style={ks}>Время печати</div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'var(--mono)', fontSize: 12.5, margin: '9px 0 8px' }}>
            <span>{pr.startedAt || '—'}</span>
            <span style={{ color: 'var(--faint)' }}>{pr.eta || '—'}</span>
          </div>
          <Bar pct={pct} h={6} color="var(--blue)" />
        </div>
      </div>

    </> : null}

    {/* Управление: свет полезен и вне печати (посмотреть в камеру на пустой стол), поэтому ряд
        живёт вне ветки printing; пауза и стоп появляются только когда есть что ставить на паузу. */}
    {/* Ряд управления гейтится СПОСОБНОСТЬЮ, а не связью: «умею ли я паузу» — свойство
        протокола, а не сети. По online кнопка была всегда, и «не умею» выяснялось нажатием. */}
    {pr && pr.can('control') && pr.online ? <div className="pctl">
      {pr.printing ? <button className="db soft" style={{ flex: 1 }} onClick={async () => {
        const next = !paused;
        const res = await run(next ? 'Печать на паузе' : 'Печать продолжена',
          () => call('adminPrinterControl', next ? 'pause' : 'resume', true));
        if (res) setPaused(next);   // при отказе call() бросает — подпись кнопки не соврёт
      }}><DIcon n={paused ? 'play' : 'pause'} size={16} />{paused ? 'Продолжить' : 'Пауза'}</button> : null}
      <button className="db soft" style={{ flex: 1, color: lightOn ? 'var(--amber)' : undefined }}
        onClick={async () => {
          const next = !lightOn;
          // второй аргумент — штатный `on` метода (power 1|0): без него команда всегда
          // включала бы свет, и амбер-подсветка кнопки показывала бы неправду
          const res = await run(next ? 'Свет включён' : 'Свет выключен',
            () => call('adminPrinterControl', 'light', next));
          if (res) setLight(next);
        }}><DIcon n="sun" size={16} />Свет{pr.light != null ? (pr.light ? ' вкл' : ' выкл') : ''}</button>
      {pr.printing ? <button className="db danger" style={{ flex: 1 }} onClick={() => confirm(
        'Остановить печать?',
        `Печать будет прервана на слое ${pr.layer || '?'}. Деталь придётся начинать заново.`,
        () => run('Печать остановлена', () => call('adminPrinterControl', 'stop', true)),
        'Остановить',
      )}><DIcon n="stop" size={14} />Стоп</button> : null}
    </div> : null}

    {/* Быстрые действия: до четырёх кнопок на выбор мастера (прогрев по профилю, выключить
        нагрев, парковка, свет) — как «Quick Actions» у прошивки сообщества CC2. */}
    <QuickActions pr={pr} />

    {/* Выгрузка филамента из слотов хаба. В настольной админке её не было вовсе — с ПК филамент
        было не выгрузить. Во время печати команду не даём: сервер её не блокирует, а выдёргивание
        прутка на ходу гарантированно портит деталь. Что заряжено в слот, принтер вещает сам
        (canvas_info → pr.slots) — кнопки показывают цвет и материал катушки. */}
    <div className="lab" style={{ marginTop: 16 }}>
      <DIcon n="spool" size={14} />Филамент
      <span className="r" style={{ color: 'var(--faint)', fontSize: 11.5 }}>
        {pr && pr.printing ? 'недоступно во время печати' : 'слоты хаба 1–4 · клик = выгрузить'}
      </span>
    </div>
    <div className="pctl">
      {HUB_SLOTS.map((n) => {
        const s = ((pr && pr.slots) || []).find((x) => x.slot === n);
        const active = pr && pr.activeSlot === n;
        return <button key={n} className="db sm line" style={{ flex: 1,
          borderColor: active ? 'var(--accent)' : undefined }}
          title={s ? `${[s.brand, s.name].filter(Boolean).join(' ')} · ${s.type}`
            + (s.temp_min && s.temp_max ? ` · ${s.temp_min}–${s.temp_max}°` : '')
            + (active ? ' · сейчас печатает' : '') : `Слот ${n}`}
          // блокируем ВСЕ слоты, пока команда в полёте: тракт подачи у хаба один,
          // две встречные выгрузки способны его заклинить (мобильная делает так же)
          disabled={!pr || !pr.online || pr.printing || !!busy}
          onClick={() => confirm(
            `Выгрузить филамент из слота ${n}?`,
            (s ? `В слоте ${[s.brand, s.name].filter(Boolean).join(' ') || s.type}. ` : '')
              + 'Делайте это, когда принтер не печатает.',
            async () => {
              setBusy('unload' + n);
              try {
                await run(`Выгрузка слота ${n} запущена`, () => call('adminPrinterUnload', n));
              } finally {
                setBusy('');
              }
            },
            'Выгрузить', false,        // выгрузка обратима — не красная кнопка
          )}>
          {s ? <span style={{ width: 12, height: 12, borderRadius: '50%', flex: '0 0 auto',
            background: s.color || 'var(--bg)', border: '1px solid var(--line)' }} />
            : <DIcon n="spool" size={14} />}
          {busy === 'unload' + n ? '…' : (s ? `${n} · ${s.type || '—'}` : n)}
        </button>;
      })}
    </div>
  </div>;
}

function AttCard({ hub, nav }) {
  const a = hub.attention || {};
  const total = SIG.reduce((s, x) => s + (a[x.k] || 0), 0);
  return <div className="card pu-att">
    <div className="lab">
      <span style={{ width: 8, height: 8, borderRadius: '50%',
        background: total ? 'var(--accent)' : 'var(--price)', display: 'inline-block',
        animation: total ? 'pulse 1.6s infinite' : 'none' }} />
      Требует внимания
      <span className="r">
        <span className="mono" style={{ color: total ? 'var(--accent)' : 'var(--price)', fontSize: 12, fontWeight: 700 }}>
          {total ? total + ' сигн.' : 'тихо'}
        </span>
      </span>
    </div>
    {total === 0
      ? <div className="okay" style={{ marginTop: 6 }}>✅ Всё спокойно — новых заказов и сообщений нет</div>
      : <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 12, justifyContent: 'center' }}>
          {SIG.map((s) => {
            const n = a[s.k] || 0;
            return <div key={s.k} className="sig" style={{ '--sc': s.c, opacity: n ? 1 : 0.45 }}
              onClick={() => nav('orders', n ? { flt: s.flt } : {})}>
              <div className="sic"><DIcon n={s.ic} size={19} /></div>
              <div className="num">{n}</div>
              <div style={{ minWidth: 0 }}>
                <div className="tt">{s.tt}</div>
                <div className="ds">{s.ds}</div>
              </div>
              <div className="go">разобрать<DIcon n="chev" size={14} /></div>
            </div>;
          })}
        </div>}
  </div>;
}

/**
 * График выручки Пульта — своя версия Area: с пунктирной сеткой и точкой-маркером на
 * последнем дне (в макете это отдельный Area2). Общий Area из ui.jsx намеренно не трогаем:
 * его рисуют другие экраны, а сетка нужна только здесь.
 */
function Area2({ data, h = 150 }) {
  if (!data || data.length < 2) return null;
  const w = 100, mx = Math.max(...data) || 1;
  const Y = (v) => h - 8 - (v / mx) * (h - 34);
  const P = data.map((v, i) => [(i / (data.length - 1)) * w, Y(v)]);
  const line = P.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const last = P[P.length - 1];
  return <div style={{ position: 'relative' }}>
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: '100%', height: h, display: 'block' }}>
      <defs>
        <linearGradient id="pult-ag2" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--price)" stopOpacity=".3" />
          <stop offset="1" stopColor="var(--price)" stopOpacity="0" />
        </linearGradient>
      </defs>
      {[0.25, 0.5, 0.75].map((f) => <line key={f} x1="0" x2={w} y1={8 + (h - 34) * f} y2={8 + (h - 34) * f}
        stroke="rgba(255,255,255,.06)" strokeWidth="1" vectorEffect="non-scaling-stroke" strokeDasharray="3 4" />)}
      <path d={line + ` L ${w} ${h} L 0 ${h} Z`} fill="url(#pult-ag2)" />
      <path d={line} fill="none" stroke="var(--price)" strokeWidth="1.8" vectorEffect="non-scaling-stroke" />
    </svg>
    <span style={{ position: 'absolute', right: -4, top: last[1] / h * 100 + '%', transform: 'translateY(-50%)',
      width: 9, height: 9, borderRadius: '50%', background: 'var(--price)',
      boxShadow: '0 0 0 4px rgba(45,212,126,.2)' }} />
  </div>;
}

// Даты ряда бэкенд не отдаёт (series14 — это [{orders, revenue}]), но нарезка жёсткая:
// ровно 14 суток, последний элемент — сегодня. Значит подпись оси считается от «сегодня».
function seriesDay(i, n) {
  const d = new Date(Date.now() - (n - 1 - i) * 86400000);
  return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0');
}

function RevCard({ hub, nav }) {
  const dash = useApi('adminDashboard', [], []);
  const r = hub.revenue || {};
  const stats = (dash.data && dash.data.stats) || {};
  const days = stats.series14 || [];
  const series = days.map((d) => Number(d.revenue) || 0);
  // Дельта заказов к прошлой неделе — из того же ряда: считать её больше не из чего,
  // отдельного «прошлая неделя» в hub.revenue нет.
  const sum7 = (from) => days.slice(from, from + 7).reduce((s, d) => s + (Number(d.orders) || 0), 0);
  const ordDelta = days.length >= 14 ? sum7(7) - sum7(0) : null;
  const avg = series.length > 1
    ? series.slice(0, -1).reduce((s, v) => s + v, 0) / (series.length - 1)
    : 0;
  // Сравнение со средним днём осмысленно, только если средний день не копеечный:
  // иначе первый же заказ даёт «+18000%» и цифра перестаёт что-либо значить.
  const delta = avg >= 1 && r.today > 0 ? Math.round((r.today / avg - 1) * 100) : null;

  return <div className="card pu-rev" style={{ cursor: 'pointer' }} onClick={() => nav('dash')}>
    <div className="lab">
      <DIcon n="coin" size={14} />Выручка
      <span className="r" style={{ color: 'var(--faint)' }}>14 дней · дашборд<DIcon n="chev" size={13} /></span>
    </div>
    <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
      <span className="mono" style={{ fontSize: 44, fontWeight: 700, color: 'var(--price)', lineHeight: 1 }}>
        {money(r.today)}
      </span>
      {delta !== null ? <div>
        <span className="delta">{delta >= 0 ? '↑ +' : '↓ '}{delta}%</span>
        <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 4 }}>к среднему дню</div>
      </div> : null}
      <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
        <div className="mono" style={{ fontSize: 15, fontWeight: 700, color: 'var(--price)' }}>{money(r.week)}</div>
        <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 2 }}>за 7 дней</div>
      </div>
    </div>
    {series.length > 1 ? <>
      <div style={{ margin: '14px 0 4px', flex: '0 0 auto' }}><Area2 data={series} h={150} /></div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'var(--mono)',
        fontSize: 10.5, color: 'var(--faint)', marginBottom: 14 }}>
        <span>{seriesDay(0, series.length)}</span>
        <span>{seriesDay(Math.floor(series.length / 2), series.length)}</span>
        <span>сегодня</span>
      </div>
    </> : null}
    <div className="rstats">
      <div><div className="k">Заказов / нед</div><div className="v">{num(r.orders_week)}
        {ordDelta > 0 ? <span className="up"> +{ordDelta}</span> : null}</div></div>
      <div><div className="k">Средний чек</div><div className="v">{money(r.avg_order)}</div></div>
      {/* «Напечатано» из макета: суммарный вес по напечатанным заказам приходит тем же
          adminDashboard, что и ряд выручки, — отдельный запрос не нужен */}
      <div><div className="k">Напечатано</div><div className="v">
        {stats.grams ? (Number(stats.grams) / 1000).toFixed(1) + ' кг' : '—'}</div></div>
    </div>
  </div>;
}

// Карточка выручки для того, кому деньги не показывают. Данные — из ХАБА, который оператору
// открыт (adminHub), поэтому лишнего запроса нет: сервер уже вырезал из него денежные поля
// (money=false), а счётчики заказов оставил. Клик ведёт в «Заказы», а не в «Дашборд»:
// дашборд оператору закрыт целиком, и переход туда упирался бы в «Нет доступа».
function WorkCard({ hub, nav }) {
  const r = hub.revenue || {};
  const c = hub.counts || {};
  return <div className="card pu-rev" style={{ cursor: 'pointer' }} onClick={() => nav('orders')}>
    <div className="lab">
      <DIcon n="receipt" size={14} />Работа
      <span className="r" style={{ color: 'var(--faint)' }}>заказы<DIcon n="chev" size={13} /></span>
    </div>
    <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
      <span className="mono" style={{ fontSize: 44, fontWeight: 700, lineHeight: 1 }}>
        {num(r.orders_week)}
      </span>
      <div style={{ fontSize: 11.5, color: 'var(--faint)' }}>заказов<br />за 7 дней</div>
      <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
        <div className="mono" style={{ fontSize: 15, fontWeight: 700 }}>{num(r.orders_total)}</div>
        <div style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 2 }}>всего</div>
      </div>
    </div>
    <div className="rstats" style={{ marginTop: 'auto' }}>
      <div><div className="k">В очереди</div><div className="v">{num(c.queue)}</div></div>
      <div><div className="k">Задач</div><div className="v">{num(c.tasks)}</div></div>
      <div><div className="k">Пластиков</div><div className="v">{num(c.plastics)}</div></div>
    </div>
  </div>;
}

// Лента для того, кому закрыты клиенты: последние печати из журнала (право «Принтер» —
// оператору открыто). Ровно тот же слот сетки, что и лента событий.
function PrintsCard({ nav }) {
  const j = useApi('adminJournal', [], [], { auto: 30000 });
  const items = (j.data && (j.data.items || j.data.journal)) || [];
  return <div className="card pu-events">
    <div className="lab">
      <DIcon n="film" size={14} />Последние печати
      <span className="r">
        <button className="db sm line" onClick={() => nav('journal')}>Журнал<DIcon n="chev" size={13} /></button>
      </span>
    </div>
    <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
      {items.length ? items.slice(0, 40).map((x) => <div key={x.id} className="ev"
        onClick={() => nav('journal')}>
        <div className="edot" style={{ background: 'var(--amber)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="et">{x.label || 'Без названия'}</div>
          <div className="es">
            {x.plastic_name || '—'}
            {x.grams ? ' · ' + grams(x.grams) : ''}
            {x.minutes ? ' · ' + fmtMin(x.minutes) : ''}
          </div>
        </div>
        <div className="tm">{x.ago}</div>
      </div>) : <Empty ic="film" t="Печатей нет" ds="Журнал пока пуст" />}
    </div>
  </div>;
}

function QueueCard({ nav }) {
  // Пульт — экран, на который мастер смотрит фоном: очередь обязана обновляться сама.
  const q = useApi('adminQueue', [], [], { auto: 15000 });
  const items = (q.data && q.data.queue) || [];
  const move = useCallback(async (id, dir) => {
    await call('adminQueueMove', id, dir);
    q.reload();
  }, [q]);
  const totalMin = items.reduce((s, i) => s + (i.minutes || 0), 0);
  return <div className="card pu-queue">
    <div className="lab">
      <DIcon n="queue" size={14} />Очередь печати · {items.length}
      <span className="r"><span style={{ color: 'var(--faint)' }}>
        {totalMin ? '≈ ' + fmtMin(totalMin) : ''}
      </span></span>
    </div>
    <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
      <QueueList items={items} onMove={move} onOpen={(id) => nav('orders', { sel: id })} />
    </div>
  </div>;
}

function TasksCard({ nav }) {
  const { toast } = useContext(AppCtx);
  const t = useApi('adminTasks', [], [], { auto: 30000 });
  const tasks = (t.data && t.data.tasks) || [];
  const toggle = async (x) => {
    await call('adminTask', { id: x.id, action: x.done ? 'reopen' : 'done' });
    if (!x.done) {
      toast('Задача выполнена', {
        kind: 'ok', action: 'Отменить',
        onAction: async () => { await call('adminTask', { id: x.id, action: 'reopen' }); t.reload(); },
      });
    }
    t.reload();
  };
  return <div className="card pu-tasks">
    <div className="lab">
      <DIcon n="task" size={14} />Задачи на сегодня
      <span className="r">
        <button className="db sm line" onClick={() => nav('tasks')}>Все<DIcon n="chev" size={13} /></button>
      </span>
    </div>
    <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
      {tasks.length ? tasks.map((x) => <div key={x.id} className={'task' + (x.done ? ' done' : '')}>
        <button className="cb" onClick={() => toggle(x)}>
          {x.done ? <DIcon n="check" size={13} sw={3} /> : null}
        </button>
        <div style={{ minWidth: 0 }}>
          <div className="tt">{x.title}</div>
          <div className={'dd' + (x.overdue ? ' red' : (x.today ? ' hot' : ''))}>
            {x.overdue ? '⏰ ' : ''}{x.due_label || (x.client ? x.client : '')}
          </div>
        </div>
      </div>) : <Empty ic="check" t="Задач нет" ds="Ничего не висит" />}
    </div>
  </div>;
}

function EventsCard({ nav }) {
  const a = useApi('adminActivity', [], [], { auto: 15000 });
  const events = (a.data && a.data.events) || [];
  const now = Date.now() / 1000;
  // Открытый снимок сметы держим ЛОКАЛЬНО: nav() сбрасывает drawer вкладки, а сюда нужно
  // открыть панель, никуда не уходя с пульта.
  const [calc, setCalc] = useState(null);
  return <div className="card pu-events">
    <div className="lab">
      <DIcon n="pulse" size={14} />Лента событий
      <span className="r">
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--price)', fontSize: 11 }}>
          <span className="pdot" style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--price)' }} />live
        </span>
      </span>
    </div>
    <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
      {/* событие про цену открывает САМУ смету (что видел клиент), остальные — карточку клиента */}
      {events.length ? events.slice(0, 40).map((e) => <div key={e.id}
        className={'ev' + (e.at && now - Number(e.at) < HOT_SEC ? ' hot' : '')}
        title={hasCalc(e) ? 'Показать смету, которую увидел клиент' : 'Открыть карточку клиента'}
        onClick={() => (hasCalc(e) ? setCalc(e.calc_id) : (e.user_id && nav('clients', { sel: e.user_id })))}>
        <div className="edot" style={{ background: evColor(e.action) }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="et">{e.name || 'Гость'} — {e.action}</div>
          <div className="es">{e.detail}</div>
        </div>
        <div className="tm" style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          {hasCalc(e) ? <DIcon n="calc" size={12} /> : null}{ago(e.at)}
        </div>
      </div>) : <Empty ic="pulse" t="Пока тихо" ds="Клиенты ничего не делали" />}
    </div>
    {calc ? <CalcDrawer id={calc} onClose={() => setCalc(null)} nav={nav} /> : null}
  </div>;
}
