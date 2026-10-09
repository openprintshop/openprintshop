// main.jsx — корень настольной админки: провайдеры, вкладки, роутер, глобальные оверлеи.

import { AppCtx, ConfirmModal, ToastHost, Lightbox } from './ui.jsx';
import { Sidebar, Topbar, Palette, TITLES, BRAND, SCREEN_CAP } from './shell.jsx';
import { DIcon } from './icons.jsx';
import { call, usePoll, isMock } from './api.jsx';
import { normPrinter } from './parts.jsx';
import { rememberCurrency } from './fmt.jsx';
import { bootRoute, parseHash, toHash, HOME } from './router.jsx';
import { LoginScreen, deskWhoami } from './login.jsx';

import { PultScreen } from './screens/pult.jsx';
import { OrdersScreen } from './screens/orders.jsx';
import { ClientsScreen } from './screens/clients.jsx';
import { TasksScreen } from './screens/tasks.jsx';
import { PrintScreen } from './screens/print.jsx';
import { PlanScreen } from './screens/plan.jsx';
import { PlasticsScreen } from './screens/plastics.jsx';
import { PricingScreen } from './screens/pricing.jsx';
import { CollectionScreen } from './screens/collection.jsx';
import { ProductsScreen } from './screens/products.jsx';
import { PrintersScreen } from './screens/printers.jsx';
import { DashScreen } from './screens/dash.jsx';
import { ActivityScreen } from './screens/activity.jsx';
import { StatusScreen } from './screens/status.jsx';
import { ErrorsScreen } from './screens/errors.jsx';   // телеметрия ошибок (f4de8627)
import { CommunityScreen } from './screens/community.jsx';   // база знаний + чат владельцев (c356387a)
import { NotifsScreen } from './screens/notifs.jsx';
import { FamiliesScreen } from './screens/families.jsx';
import { JournalScreen } from './screens/journal.jsx';
import { CalibScreen } from './screens/calib.jsx';
import { PlannerScreen } from './screens/planner.jsx';
import { SettingsScreen } from './screens/settings.jsx';
import { DeliveryScreen } from './screens/delivery.jsx';   // зоны и тарифы доставки (A2)
import { StaffScreen } from './screens/staff.jsx';         // команда: роли и права (A4)
import { ToolsScreen } from './screens/tools.jsx';         // инструменты мастера: допечатка со слоя

const { useState, useEffect, useCallback, useRef, useMemo } = React;

const SCREENS = {
  pult: PultScreen, orders: OrdersScreen, clients: ClientsScreen, tasks: TasksScreen,
  print: PrintScreen, plan: PlanScreen, plastics: PlasticsScreen, pricing: PricingScreen,
  collection: CollectionScreen, products: ProductsScreen, printers: PrintersScreen,
  dash: DashScreen, activity: ActivityScreen, status: StatusScreen, errors: ErrorsScreen,
  notifs: NotifsScreen, community: CommunityScreen,
  families: FamiliesScreen, journal: JournalScreen, planner: PlannerScreen,
  settings: SettingsScreen, calib: CalibScreen,
  delivery: DeliveryScreen, staff: StaffScreen, tools: ToolsScreen,
};

/** Раздел закрыт ролью (A4): честно объясняем, а не показываем пустой экран с ошибками. */
function NoAccess() {
  return <div className="empty" style={{ padding: '60px 20px', textAlign: 'center' }}>
    <DIcon n="shield" size={34} />
    <div style={{ marginTop: 12, fontWeight: 700 }}>Раздел доступен только владельцу</div>
    <div className="hint" style={{ marginTop: 6 }}>
      У вашей роли нет прав на этот раздел. Открыть доступ может владелец мастерской
      в разделе «Команда».
    </div>
  </div>;
}

/** Баннер MOCK-режима. Без него админка молча показывает выдуманные данные как настоящие. */
function MockBanner() {
  return <div className="mockbar">
    <DIcon n="shield" size={16} />
    <b>Демо-данные.</b> Токен админки не найден, поэтому показаны выдуманные заказы и клиенты.
    Запусти админку через <span className="mono">admin_panel.bat</span>.
  </div>;
}

/**
 * Полоса вкладок. Показывается только когда вкладок больше одной: при обычной работе
 * с одним экраном лишняя полоса — визуальный шум.
 */
function TabBar({ tabs, active, onPick, onClose, onNew }) {
  if (tabs.length < 2) return null;
  return <div className="tabbar">
    {tabs.map((t, i) => {
      const [title] = TITLES[t.screen] || [t.screen];
      return <div key={t.id} className={'tab' + (t.id === active ? ' on' : '')}
        title={`${title}  ·  Ctrl+${i + 1}`}
        onClick={() => onPick(t.id)}
        onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); onClose(t.id); } }}>
        <span className="tt">{title}</span>
        <button className="x" title="Закрыть (Ctrl+W)"
          onClick={(e) => { e.stopPropagation(); onClose(t.id); }}>✕</button>
      </div>;
    })}
    <button className="tabnew" title="Новая вкладка (Ctrl+T)" onClick={onNew}>
      <DIcon n="plus" size={14} />
    </button>
  </div>;
}

/**
 * Одна вкладка со своим контекстом.
 *
 * Ключевое: `screen`, `params` и `drawer` берутся ИЗ ВКЛАДКИ, а не из активной. Иначе
 * все смонтированные экраны увидели бы параметры активной вкладки и показали чужой заказ.
 * Ради этого состояние и разъехалось из корневого App() по вкладкам.
 *
 * Неактивные вкладки остаются смонтированными и просто скрыты: в этом весь смысл вкладок —
 * вернувшись, мастер видит тот же список, ту же выбранную строку и незакрытую форму.
 * Опрос при этом не дублируется: экраны грузятся разово (useApi), а живой поллинг принтера
 * один на всё приложение и живёт в корне.
 */
function TabView({ tab, visible, shared, navTab, setTabDrawer }) {
  const nav = useCallback((s, p) => navTab(tab.id, s, p), [navTab, tab.id]);
  const setDrawer = useCallback((d) => setTabDrawer(tab.id, d), [setTabDrawer, tab.id]);
  const ctx = useMemo(() => ({
    ...shared,
    screen: tab.screen, params: tab.params, drawer: tab.drawer,
    nav, setDrawer,
    // Автообновление экранов (useApi({auto})) должно молчать в скрытых вкладках: они
    // остаются смонтированными, и без этого каждая вкладка добавляла бы свой поток запросов.
    tabVisible: visible,
  }), [shared, tab.screen, tab.params, tab.drawer, nav, setDrawer, visible]);

  // Права роли (A4) закрывают не только сайдбар, но и прямой заход по адресу #/settings:
  // экран не монтируем вовсе — иначе он ушёл бы в API и собрал горсть 403.
  const need = SCREEN_CAP[tab.screen];
  const denied = need && shared.can && !shared.can(need);
  const Cur = SCREENS[tab.screen] || PultScreen;
  return <AppCtx.Provider value={ctx}>
    <div className="content" style={visible ? undefined : { display: 'none' }}>
      <Topbar />
      {shared.mock ? <MockBanner /> : null}
      <div className="page">{denied ? <NoAccess /> : <Cur />}</div>
    </div>
  </AppCtx.Provider>;
}

let tabSeq = 1;

function App() {
  const boot = useRef(bootRoute()).current;
  const [tabs, setTabs] = useState(() => [
    { id: tabSeq, screen: boot.screen, params: boot.params, drawer: null },
  ]);
  const [active, setActive] = useState(tabSeq);
  const [toasts, setToasts] = useState([]);
  const [ask, setAsk] = useState(null);
  const [pal, setPal] = useState(false);
  const [lb, setLb] = useState(null);
  const [hub, setHub] = useState(null);
  const [env, setEnv] = useState(null);
  const idr = useRef(1);
  const mock = useRef(isMock()).current;

  const cur = tabs.find((t) => t.id === active) || tabs[0];
  // Состояние вкладок наружу — для отладки и автопроверок (смотреть в консоли window.__tabs).
  window.__tabs = { tabs, active };

  const dismiss = useCallback((id) => setToasts((ts) => ts.filter((t) => t.id !== id)), []);
  const toast = useCallback((msg, opts = {}) => {
    const id = idr.current++;
    setToasts((ts) => [...ts.slice(-3), { id, msg, ...opts }]);
    setTimeout(() => dismiss(id), opts.ms || 6000);
  }, [dismiss]);

  // Навигация внутри вкладки. Адрес отражает только активную вкладку: прямая ссылка и
  // «назад» продолжают работать, а фоновые вкладки в историю не лезут.
  const navTab = useCallback((tabId, s, p) => {
    setTabs((ts) => ts.map((t) => (t.id === tabId ? { ...t, screen: s, params: p || {}, drawer: null } : t)));
    setActive((a) => {
      if (a === tabId) window.history.pushState(null, '', toHash(s, p));
      return a;
    });
  }, []);

  const setTabDrawer = useCallback((tabId, d) => {
    setTabs((ts) => ts.map((t) => (t.id === tabId ? { ...t, drawer: d } : t)));
  }, []);

  const newTab = useCallback((screen = HOME, params = {}) => {
    const id = ++tabSeq;
    setTabs((ts) => ts.concat([{ id, screen, params, drawer: null }]));
    setActive(id);
    window.history.pushState(null, '', toHash(screen, params));
  }, []);

  const closeTab = useCallback((id) => {
    setTabs((ts) => {
      if (ts.length < 2) return ts;            // последнюю вкладку не закрываем: окно опустеет
      const i = ts.findIndex((t) => t.id === id);
      const next = ts.filter((t) => t.id !== id);
      setActive((a) => {
        if (a !== id) return a;
        const n = next[Math.min(i, next.length - 1)];
        window.history.pushState(null, '', toHash(n.screen, n.params));
        return n.id;
      });
      return next;
    });
  }, []);

  const pickTab = useCallback((id) => {
    setActive(id);
    setTabs((ts) => {
      const t = ts.find((x) => x.id === id);
      if (t) window.history.pushState(null, '', toHash(t.screen, t.params));
      return ts;
    });
  }, []);

  // «Назад» браузера меняет активную вкладку, а не плодит новые.
  useEffect(() => {
    const onPop = () => {
      const r = parseHash() || { screen: HOME, params: {} };
      setTabs((ts) => ts.map((t) => (t.id === active
        ? { ...t, screen: r.screen, params: r.params, drawer: null } : t)));
    };
    window.addEventListener('popstate', onPop);
    window.addEventListener('hashchange', onPop);
    return () => {
      window.removeEventListener('popstate', onPop);
      window.removeEventListener('hashchange', onPop);
    };
  }, [active]);

  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, '', toHash(cur.screen, cur.params));
  }, []);   // eslint-disable-line

  useEffect(() => {
    const t = TITLES[cur.screen];
    // Имя мастерской, а не наше: заголовок окна перетирается при каждой смене экрана,
    // и без BRAND он затирал бы подставленный сервером бренд (A3, white-label).
    document.title = (t ? t[0] + ' · ' : '') + BRAND.title;
  }, [cur.screen]);

  // Глобальные хоткеи. Ctrl+T/W/Tab и Ctrl+1..9 — вкладки, Ctrl+K — палитра.
  // В полях ввода не перехватываем ничего, кроме тех, что браузер и так забирает себе.
  useEffect(() => {
    const f = (e) => {
      const ctrl = e.ctrlKey || e.metaKey;
      if (ctrl && e.key.toLowerCase() === 'k') { e.preventDefault(); setPal((v) => !v); return; }
      if (e.key === 'Escape') { setPal(false); setLb(null); }
      if (!ctrl) return;
      if (e.key.toLowerCase() === 't') { e.preventDefault(); newTab(); return; }
      if (e.key.toLowerCase() === 'w') { e.preventDefault(); closeTab(active); return; }
      if (e.key === 'Tab') {
        e.preventDefault();
        setTabs((ts) => {
          const i = ts.findIndex((t) => t.id === active);
          const n = ts[(i + (e.shiftKey ? -1 : 1) + ts.length) % ts.length];
          if (n) pickTab(n.id);
          return ts;
        });
        return;
      }
      if (/^[1-9]$/.test(e.key)) {
        setTabs((ts) => {
          const n = ts[Number(e.key) - 1];
          if (n) { e.preventDefault(); pickTab(n.id); }
          return ts;
        });
      }
    };
    window.addEventListener('keydown', f);
    return () => window.removeEventListener('keydown', f);
  }, [active, newTab, closeTab, pickTab]);

  // Сводка по разделам: один запрос на приложение, раздаётся через контекст.
  const reloadHub = useCallback(async () => {
    try { setHub(rememberCurrency(await call('adminHub'))); } catch (e) { /* бот мог перезапускаться */ }
  }, []);
  useEffect(() => {
    reloadHub();
    const iv = setInterval(reloadHub, 20000);
    return () => clearInterval(iv);
  }, [reloadHub]);

  // Канал (test/stable) для метки в сайдбаре — это не админский эндпоинт, доступен всегда.
  useEffect(() => {
    fetch('/api/version').then((r) => r.json()).then(setEnv).catch(() => {});
  }, []);

  // Принтер — единственный по-настоящему живой источник. Один поллинг на всё приложение:
  // бот и вебапп в одном event loop, дублировать его по вкладкам нельзя.
  const printerState = usePoll('adminPrinterStatus', 3000);
  // useMemo по данным опроса: новый объект принтера — только на НОВЫЙ ответ. По смене ссылки
  // LiveBadge (экран «Печать») считает «N с назад», и пересоздание объекта на каждом рендере
  // App (тосты, hub каждые 20 с) сбрасывало бы отсчёт без свежих данных.
  const printer = useMemo(() => normPrinter(printerState.data), [printerState.data]);
  // Прогресс печати в заголовке окна: видно с панели задач, не разворачивая деск (как у CC2
  // Control сообщества). Эффект — после объявления printer: в списке зависимостей выше он ещё
  // не инициализирован. Тот же заголовок экрана ставит эффект выше; этот идёт следом и дописывает.
  const titlePct = printer && printer.printing ? Math.round(printer.progress || 0) : -1;
  useEffect(() => {
    const t = TITLES[cur.screen];
    document.title = (titlePct >= 0 ? `${titlePct}% · ` : '') + (t ? t[0] + ' · ' : '') + BRAND.title;
  }, [cur.screen, titlePct]);

  // Общее для всех вкладок. Экранное (screen/params/drawer/nav) живёт в самой вкладке.
  // Роль и права сотрудника (A4) приезжают вместе с хабом. can(cap) — единственный способ,
  // которым интерфейс решает, показывать ли раздел: сервер закрывает те же ручки сам, так
  // что скрытая кнопка — это забота о человеке, а не защита.
  const me = (hub && hub.me) || null;
  const can = useCallback((cap) => {
    if (!me) return true;      // хаб ещё не приехал — не мигаем скрытием разделов
    return (me.caps || []).indexOf(cap) >= 0;
  }, [me]);

  const shared = useMemo(() => ({
    toast, ask: setAsk, openPalette: () => setPal(true), lightbox: setLb,
    hub, reloadHub, printer, env, mock, me, can,
    newTab,      // «открыть в новой вкладке» доступно экранам
  }), [toast, hub, reloadHub, printer, env, mock, me, can, newTab]);

  // Сайдбар и палитра живут вне вкладок, но им нужен контекст активной вкладки.
  const shellCtx = useMemo(() => ({
    ...shared,
    screen: cur.screen, params: cur.params, drawer: cur.drawer,
    nav: (s, p) => navTab(active, s, p),
    setDrawer: (d) => setTabDrawer(active, d),
  }), [shared, cur, active, navTab, setTabDrawer]);

  return <AppCtx.Provider value={shellCtx}>
    <div className="viewport">
      <div className="app">
        <Sidebar />
        <div className="tabhost">
          <TabBar tabs={tabs} active={active} onPick={pickTab} onClose={closeTab}
            onNew={() => newTab()} />
          {tabs.map((t) => <TabView key={t.id} tab={t} visible={t.id === active}
            shared={shared} navTab={navTab} setTabDrawer={setTabDrawer} />)}
        </div>
      </div>
      {pal && <Palette onClose={() => setPal(false)} />}
      <Lightbox src={lb} onClose={() => setLb(null)} />
      <ConfirmModal q={ask} onClose={() => setAsk(null)} />
      <ToastHost toasts={toasts} dismiss={dismiss} />
    </div>
  </AppCtx.Provider>;
}

/**
 * Дверь перед приложением: пока сервер не признал в нас сотрудника, показываем вход.
 *
 * Почему не пускаем «как раньше, а там разберётся сервер»: без авторизации api.js уходит в
 * MOCK и рисует ВЫДУМАННЫЕ заказы и выручку — админка выглядит рабочей и врёт. Это хуже
 * пустого экрана: мастер принимает решения по несуществующим числам.
 *
 * `?mock=1` оставлен намеренно — им смотрят вёрстку без живого бота (и им же проверяю я).
 * Если сервер не ответил вовсе (бот не поднят), тоже пускаем внутрь: там баннер MOCK честно
 * скажет, что данные ненастоящие, а экран входа в этот момент бесполезен — обменивать код
 * всё равно не с кем.
 */
function Root() {
  const demo = new URLSearchParams(location.search).get('mock') === '1';
  const [state, setState] = useState(demo ? 'ok' : 'check');

  useEffect(() => {
    if (demo) return undefined;
    let dead = false;
    deskWhoami().then((r) => {
      if (dead) return;
      if (!r) { setState('ok'); return; }        // сервера нет — внутрь, с честным баннером
      setState(r.authed ? 'ok' : 'login');
    });
    return () => { dead = true; };
  }, [demo]);

  if (state === 'check') return null;            // мгновение до ответа: мигать нечем
  if (state === 'login') return <LoginScreen />;
  return <App />;
}

ReactDOM.createRoot(document.getElementById('root')).render(<Root />);
