// shell.jsx — сайдбар, топбар и командная палитра.

import { DIcon } from './icons.jsx';
import { AppCtx } from './ui.jsx';
import { call } from './api.jsx';
import { deskLogout } from './login.jsx';

const { useState, useEffect, useRef, useContext, useCallback } = React;

// Бренд мастерской (A3, white-label): сервер вшивает его в desk/index.html до первого кадра.
// Дефолт на случай, если бандл открыли без серверной подстановки.
export const BRAND = (function () {
  const b = window.__BRAND__ || {};
  return b.title ? b : { title: 'OpenPrintShop', stock: true, logo: '', accent: '#ff7a1a',
                         wordmark: ['OPEN', 'PRINT', 'SHOP'] };
})();

// Разделы. Ключ совпадает с ключом роутера (main.jsx) и с адресом #/<ключ>.
// bdg — имя счётчика из /api/admin/hub, mut — приглушённый (не срочный) бейдж.
// cap — право из матрицы ролей (A4): раздела нет в сайдбаре и в палитре у того, у кого нет
// права. Ручки на сервере закрыты той же матрицей — здесь мы лишь не водим человека
// в дверь, которая для него заперта.
// Возможности сборки (f98aa1ef): сервер вшил их в desk/index.html до первого кадра, как и
// бренд. Ключ отсутствует (старый сервер / статичное превью) — считаем возможность включённой:
// прятать раздел из-за незнания хуже, чем показать лишний.
export const FEATURES = window.__FEATURES__ || {};
export const hasFeature = (key) => FEATURES[key] !== false;

// feat — раздел живёт только пока включена эта возможность сборки.
export const NAV = [
  { lab: 'Операции', items: [
    ['pult', 'Пульт', 'bolt'],
    ['orders', 'Заказы', 'receipt', { bdg: 'orders' }],
    ['print', 'Печать', 'printer', { live: 1 }],
    ['plan', 'План недели', 'cal'],
    ['clients', 'Клиенты', 'star', { cap: 'clients' }],
    ['tasks', 'Задачи', 'task', { bdg: 'tasks', mut: 1 }],
  ] },
  { lab: 'Каталог', items: [
    ['plastics', 'Пластики', 'spool', { cap: 'pricing' }],
    ['pricing', 'Цены и скидки', 'coin', { cap: 'pricing' }],
    ['delivery', 'Доставка', 'truck', { cap: 'pricing' }],
    ['collection', 'Коллекция', 'grid', { cap: 'pricing' }],
    ['products', 'Готовые изделия', 'gift', { cap: 'pricing' }],
    // Экран настроечный: список читается правом «Принтер», но КАЖДАЯ правка (добавить,
    // включить, сделать основным, удалить, привязать, сопла) идёт в /api/admin/printer под
    // «Настройками». Экран, где не работает ни одна кнопка, хуже отсутствующего экрана —
    // поэтому он целиком владельческий, а живая машина оператору доступна на «Печати».
    ['printers', 'Принтеры', 'box', { cap: 'settings' }],
  ] },
  { lab: 'Аналитика', items: [
    ['dash', 'Дашборд', 'chart', { cap: 'finance' }],
    ['activity', 'Активность', 'pulse', { cap: 'clients' }],
    ['status', 'Статус', 'shield'],
    // телеметрия ошибок (f4de8627): бейдж — непрочитанные сбои из /api/admin/hub
    ['errors', 'Ошибки', 'flame', { bdg: 'errors', cap: 'settings' }],
  ] },
  { lab: 'Инструменты', items: [
    // рабочие инструменты у станка (допечатка со слоя, Путь В): то же право, что у печати —
    // за ними заливка файла на принтер и старт
    ['tools', 'Инструменты', 'wrench', { cap: 'printer' }],
    ['notifs', 'Уведомления', 'bell', { bdg: 'notifications', mut: 1, cap: 'settings' }],
    // база знаний студии + вход в чат владельцев студий (c356387a)
    ['community', 'Сообщество', 'chat', { cap: 'settings' }],
    ['families', 'Семейные счета', 'users', { bdg: 'family_requests', mut: 1, cap: 'clients' }],
    ['journal', 'Журнал мастера', 'doc'],
    ['calib', 'Калибровка', 'flame'],
    ['planner', 'Планировщик стены', 'wall', { feat: 'planner' }],
    ['staff', 'Команда', 'users', { cap: 'settings' }],
    ['settings', 'Настройки', 'gear', { cap: 'settings' }],
  ] },
];

// Экран → требуемое право (A4). Собирается из NAV, чтобы не держать второй список: по нему
// фильтруется командная палитра и закрывается прямой переход по адресу #/<экран>.
export const SCREEN_CAP = (function () {
  const m = {};
  NAV.forEach((g) => g.items.forEach(([k, , , x]) => { if (x && x.cap) m[k] = x.cap; }));
  return m;
})();

// Экран → возможность сборки (f98aa1ef). Тот же приём: список один, собирается из NAV.
export const SCREEN_FEAT = (function () {
  const m = {};
  NAV.forEach((g) => g.items.forEach(([k, , , x]) => { if (x && x.feat) m[k] = x.feat; }));
  return m;
})();

export const TITLES = {
  pult: ['Пульт', 'обстановка в мастерской'],
  orders: ['Заказы', 'клиентские заявки'],
  print: ['Печать', 'очередь · принтер · ТО · для себя'],
  plan: ['План недели', 'дедлайны × мощность принтера'],
  clients: ['Клиенты', 'CRM'],
  tasks: ['Задачи', 'напоминания мастера'],
  plastics: ['Пластики', 'склад и катушки'],
  pricing: ['Цены и скидки', 'ставки · тиры · промокоды'],
  collection: ['Коллекция', 'витрина моделей'],
  products: ['Готовые изделия', 'в продаже'],
  printers: ['Принтеры', 'профили слайсера'],
  dash: ['Дашборд', 'метрики · Big Data'],
  activity: ['Активность', 'действия клиентов'],
  status: ['Статус', 'система · ИИ · GPU'],
  errors: ['Ошибки', 'телеметрия сбоев'],
  notifs: ['Уведомления', 'черновики и события'],
  community: ['Сообщество', 'база знаний · чат владельцев'],
  families: ['Семейные счета', 'общий баланс'],
  journal: ['Журнал мастера', 'разбор ссылок · печати'],
  calib: ['Калибровка', 'кампании · паспорта · пресеты · образцы'],
  planner: ['Планировщик стены', 'Multiboard'],
  delivery: ['Доставка', 'типы · тарифы · сроки'],
  staff: ['Команда', 'сотрудники · роли и права'],
  settings: ['Настройки', 'догон · срочность · DFM · расчёт'],
  tools: ['Инструменты', 'допечатка со слоя без сорвавшихся деталей'],
};

// Счётчик берём из hub по имени; «заказы» показывают новые + непрочитанные сообщения.
function badgeValue(hub, name) {
  if (!hub) return 0;
  const a = hub.attention || {}, c = hub.counts || {};
  if (name === 'orders') return (a.new_orders || 0) + (a.unread || 0);
  if (name === 'tasks') return c.tasks || 0;
  return c[name] || 0;
}

export function Sidebar() {
  const { screen, nav, hub, printer, env, can, me } = useContext(AppCtx);
  const pr = printer || {};
  const printing = !!pr.printing;
  // Раздел показываем, если хватает прав (A4) И возможность включена в этой сборке (f98aa1ef).
  const allowed = (x) => (!x || !x.cap || !can || can(x.cap)) && (!x || !x.feat || hasFeature(x.feat));
  return <div className="sb">
    <div className="sb-brand">
      {/* Логотип мастерской, если залит (A3, white-label); иначе — наш знак, тот же, что
          в шапке мини-аппа (app.js brandbar): владельцу он нравится больше куба. */}
      {BRAND.logo
        ? <img className="sb-logo" src={BRAND.logo} alt="" width="27" height="27" />
        : <svg width="27" height="27" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.6" strokeLinecap="round">
            <path d="M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6M12 3v6M12 15v6M3 12h6M15 12h6" />
          </svg>}
      {/* Наш вордмарк: части имени через точки-акценты (OPEN·PRINT·SHOP, PRINT·STUDIO). */}
      <div className="wm">{BRAND.stock
        ? (BRAND.wordmark || ['OPEN', 'PRINT', 'SHOP']).map((p, i) => <React.Fragment key={i}>{i ? <em>·</em> : null}{p}</React.Fragment>)
        : BRAND.title}</div>
      {env && env.is_test ? <div className="env">{env.channel} · {env.rev}</div> : null}
    </div>
    <nav className="sb-nav">
      {NAV.map((g) => (g.items.filter(([, , , x]) => allowed(x)).length === 0 ? null : <div key={g.lab}>
        <div className="sb-lab">{g.lab}</div>
        {g.items.filter(([, , , x]) => allowed(x)).map(([k, l, ic, x]) => {
          const n = x && x.bdg ? badgeValue(hub, x.bdg) : 0;
          return <button key={k} className={'sb-item' + (screen === k ? ' on' : '')} onClick={() => nav(k)}>
            <DIcon n={ic} size={18} />{l}
            {n > 0 ? <span className={'bdg' + (x.mut ? ' mut' : '')}>{n}</span> : null}
            {x && x.live && printing
              ? <span className="live">
                  <i className="pdot" style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--price)', display: 'inline-block' }} />
                  {Math.round(pr.progress || 0)}%
                </span>
              : null}
          </button>;
        })}
      </div>))}
    </nav>
    <div className="sb-foot">
      {/* Кто работает. Пока вход был один на машину, показывать было нечего — за компьютером
          всегда «владелец». С личным входом это первое, что надо видеть: иначе человек не
          заметит, что сидит под чужой сессией, и удивится, куда делись разделы. */}
      {me && me.role ? <div className="sb-me">
        <div className="sb-me-n">{me.name || me.role_label}</div>
        <div className="sb-me-r">
          {/* Имя есть — во второй строке роль; имени нет (корневой владелец из .env) —
              повторять «Владелец» дважды незачем, показываем id. */}
          <span>{me.name ? me.role_label : (me.uid ? 'id ' + me.uid : '')}</span>
          {me.uid ? <button className="sb-out" title="Выйти" onClick={deskLogout}>выйти</button> : null}
        </div>
      </div> : null}
      <div className="ver">{BRAND.title.toLowerCase().replace(/\s+/g, '-')} desktop{env && env.rev ? ' · ' + env.rev : ''}</div>
    </div>
  </div>;
}

function Clock() {
  const [t, setT] = useState('');
  useEffect(() => {
    const f = () => setT(new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }));
    f();
    const iv = setInterval(f, 10000);
    return () => clearInterval(iv);
  }, []);
  return <span className="tb-clock">{t}</span>;
}

export function Topbar() {
  const { screen, nav, openPalette, setDrawer, hub, printer } = useContext(AppCtx);

  // Хоткеи экрана: «/» — в поиск, Esc — закрыть панель, j/k — по строкам таблицы.
  useEffect(() => {
    const h = (e) => {
      const tg = (e.target.tagName || '').toLowerCase();
      if (tg === 'input' || tg === 'textarea' || tg === 'select' || e.target.isContentEditable) {
        if (e.key === 'Escape') e.target.blur();
        return;
      }
      if (e.key === '/') {
        e.preventDefault();
        // поиск списка — тоже .tb-search (класс макета); в топбаре поля нет, там палитра
        const f = document.querySelector('.page .tb-search input');
        if (f) f.focus(); else openPalette();
      } else if (e.key === 'Escape') {
        setDrawer(null);
      } else if (e.key === 'j' || e.key === 'k') {
        const rows = [...document.querySelectorAll('tr.row')];
        if (!rows.length) return;
        const i = rows.findIndex((r) => r.className.includes('sel'));
        const n = rows[Math.max(0, Math.min(rows.length - 1, i < 0 ? 0 : i + (e.key === 'j' ? 1 : -1)))];
        if (n) { n.click(); n.scrollIntoView({ block: 'nearest' }); }
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [openPalette, setDrawer]);

  const [t, s] = TITLES[screen] || ['', ''];
  const pr = printer || {};
  const printing = !!pr.printing;
  const notif = ((hub && hub.counts && hub.counts.notifications) || 0) +
                ((hub && hub.counts && hub.counts.family_requests) || 0);

  return <div className="tb">
    <div className="tb-title">{t}<span>{s}</span></div>
    <div className="tb-search" onClick={openPalette}>
      <DIcon n="search" size={17} />
      <span className="grow">Поиск: заказ, клиент, модель, экран…</span>
      <span className="kbd">Ctrl</span><span className="kbd">K</span>
    </div>
    <div className="tb-right">
      {printing
        ? <div className="tb-chip" onClick={() => nav('print')} title="Принтер — печатает">
            <span className="pdot"></span>
            <span className="mono" style={{ color: 'var(--text)', fontWeight: 600 }}>{Math.round(pr.progress || 0)}%</span>
            {pr.remaining ? <span>осталось {pr.remaining}</span> : null}
          </div>
        : <div className="tb-chip" onClick={() => nav('print')} title="Принтер">
            <span>принтер · {pr.online ? 'свободен' : 'нет связи'}</span>
          </div>}
      <div className="tb-bell" onClick={() => nav('notifs')}>
        <DIcon n="bell" size={19} />
        {notif > 0 ? <span className="n">{notif}</span> : null}
      </div>
      <Clock />
    </div>
  </div>;
}

// ---------- командная палитра (Ctrl+K) ----------

export function Palette({ onClose }) {
  const { nav, can } = useContext(AppCtx);
  const [q, setQ] = useState('');
  const [ix, setIx] = useState(0);
  const [hits, setHits] = useState({ orders: [], clients: [], chats: [] });
  const ref = useRef();

  useEffect(() => { if (ref.current) ref.current.focus(); }, []);
  useEffect(() => setIx(0), [q]);

  // Поиск идёт на сервер: заказы и клиенты ищутся по своим эндпоинтам, чаты — отдельным
  // (в списке заказов текста сообщений нет). Дебаунс, чтобы не долбить бота на каждый символ.
  useEffect(() => {
    const ql = q.trim();
    if (!ql) { setHits({ orders: [], clients: [], chats: [] }); return; }
    let dead = false;
    const t = setTimeout(async () => {
      // Клиенты и поиск по чатам закрыты правом «Клиенты» (chat_search — вовсе «Настройками»
      // по умолчанию). У оператора эти два запроса на каждый набор символов уходили в отказ:
      // результат тот же пустой, но по 403 на каждый ввод. Спрашиваем только то, что можно.
      const [o, c, m] = await Promise.all([
        call('adminOrders', ql).catch(() => null),
        can('clients') ? call('adminClients', { q: ql }).catch(() => null) : null,
        can('settings') ? call('adminChatSearch', ql).catch(() => null) : null,
      ]);
      if (dead) return;
      setHits({
        orders: (o && o.orders ? o.orders : []).slice(0, 5),
        clients: (c && c.clients ? c.clients : []).slice(0, 4),
        chats: (m && m.hits ? m.hits : []).slice(0, 4),
      });
    }, 220);
    return () => { dead = true; clearTimeout(t); };
  }, [q, can]);

  const items = [];
  const ql = q.trim().toLowerCase();
  hits.orders.forEach((o) => items.push({
    k: 'o' + o.id, img: o.preview, tt: `#${o.id} · ${o.label}`, pk: 'заказ',
    go: () => nav('orders', { sel: o.id }),
  }));
  hits.clients.forEach((c) => items.push({
    k: 'c' + c.uid, ic: 'star', tt: c.name, pk: 'клиент',
    go: () => nav('clients', { sel: c.uid }),
  }));
  hits.chats.forEach((m) => items.push({
    k: 'm' + m.id, ic: 'chat',
    tt: '«' + String(m.text || '').slice(0, 52) + (String(m.text || '').length > 52 ? '…' : '') + '»',
    pk: 'чат #' + m.request_id,
    go: () => nav('orders', { sel: m.request_id }),
  }));
  Object.entries(TITLES)
    // разделы, закрытые ролью (A4) или выключенные в сборке (f98aa1ef), в палитру не
    // попадают — иначе она вела бы в отказ или в пустой экран
    .filter(([k]) => !SCREEN_CAP[k] || !can || can(SCREEN_CAP[k]))
    .filter(([k]) => !SCREEN_FEAT[k] || hasFeature(SCREEN_FEAT[k]))
    .filter(([, [t]]) => !ql || t.toLowerCase().includes(ql))
    .slice(0, ql ? 4 : 9)
    .forEach(([k, [t]]) => items.push({ k: 's' + k, ic: 'chev', tt: 'Перейти: ' + t, pk: 'экран', go: () => nav(k) }));

  const onKey = useCallback((e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setIx((i) => Math.min(items.length - 1, i + 1)); }
    if (e.key === 'ArrowUp') { e.preventDefault(); setIx((i) => Math.max(0, i - 1)); }
    if (e.key === 'Enter' && items[ix]) { items[ix].go(); onClose(); }
    if (e.key === 'Escape') onClose();
  }, [items, ix, onClose]);

  return <div className="pal" onClick={onClose}>
    <div className="pbox" onClick={(e) => e.stopPropagation()}>
      <input ref={ref} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey}
        placeholder="Номер заказа, клиент, модель, текст сообщения или экран…" />
      <div className="plist">
        {items.map((it, i) =>
          <div key={it.k} className={'pitem' + (i === ix ? ' on' : '')}
            onMouseEnter={() => setIx(i)} onClick={() => { it.go(); onClose(); }}>
            {it.img ? <img src={it.img} alt="" /> : <DIcon n={it.ic} size={18} style={{ color: 'var(--faint)' }} />}
            <span>{it.tt}</span><span className="pk">{it.pk}</span>
          </div>)}
        {!items.length && <div style={{ padding: '22px 16px', color: 'var(--faint)', fontSize: 14 }}>
          {q ? `Ничего не найдено по «${q}»` : 'Начните вводить запрос'}
        </div>}
      </div>
    </div>
  </div>;
}
