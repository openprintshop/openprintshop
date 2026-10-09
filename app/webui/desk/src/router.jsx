// router.jsx — адресация экранов.
//
// У прототипа роутера нет вообще: ни URL, ни history, F5 всегда возвращает на Пульт.
// Здесь настоящий: адрес вида #/orders?sel=147 переживает перезагрузку, работает «назад»,
// и на него можно повесить клик по Windows-тосту (этап 3 плана оболочки).
//
// Плюс поддержаны deep-links из телеграм-уведомлений в том же виде, что у мини-аппа
// (?go=<алиас>&id=<n>) — иначе ссылки мастера открывали бы мобильную админку.

export const HOME = 'pult';

// Алиасы deep-links мини-аппа (app/webui/app.js: BOOT.MAP) → экраны настольной админки.
const GO = {
  admin: 'pult', adminorders: 'orders', orders: 'orders', adminorder: 'orders',
  clients: 'clients', client: 'clients', tasks: 'tasks', adminqueuefix: 'journal',
  print: 'print', printer: 'print', dash: 'dash', pricing: 'pricing',
  masterlog: 'journal', products: 'products', adminproducts: 'products',
  adminnotifs: 'notifs', notifs: 'notifs', adminfamilies: 'families', families: 'families',
  printclassify: 'notifs', collection: 'collection', plastics: 'plastics',
  activity: 'activity', status: 'status', planner: 'planner', plan: 'plan',
  errors: 'errors', adminerrors: 'errors',
  // база знаний и чат владельцев — один раздел настольной админки (c356387a)
  community: 'community', kb: 'community', adminkb: 'community',
  settings: 'settings', adminsettings: 'settings',
  calib: 'calib', calibration: 'calib',
  tools: 'tools', resume: 'tools',
};

function parseQuery(qs) {
  const out = {};
  new URLSearchParams(qs || '').forEach((v, k) => { out[k] = v; });
  if (out.sel) out.sel = Number(out.sel) || out.sel;
  return out;
}

/** Текущий адрес из хэша: #/orders?sel=147 → {screen:'orders', params:{sel:147}}. */
export function parseHash() {
  const h = String(window.location.hash || '').replace(/^#\/?/, '');
  if (!h) return null;
  const [path, qs] = h.split('?');
  const screen = (path || '').split('/')[0];
  if (!screen) return null;
  return { screen, params: parseQuery(qs) };
}

/**
 * Что открыть при старте окна. Приоритет: ?go= (пришли из уведомления) → хэш → Пульт.
 * Разобранный ?go= из адреса убираем, иначе он «залипнет» при каждом F5.
 */
export function bootRoute() {
  const q = new URLSearchParams(window.location.search);
  const go = (q.get('go') || '').toLowerCase();
  if (go && GO[go]) {
    const id = Number(q.get('id') || 0) || 0;
    const params = {};
    if (id) params.sel = id;
    const tab = q.get('tab');
    if (tab) params.tab = tab;
    // токен админки лаунчер тоже кладёт в адрес — чистим всё, кроме него самого:
    // его уже забрал api.js и вычистил, здесь остаётся только наша навигация.
    q.delete('go'); q.delete('id'); q.delete('tab');
    const rest = q.toString();
    const url = window.location.pathname + (rest ? '?' + rest : '') + toHash(GO[go], params);
    window.history.replaceState(null, '', url);
    return { screen: GO[go], params };
  }
  return parseHash() || { screen: HOME, params: {} };
}

export function toHash(screen, params) {
  const p = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') p.set(k, String(v));
  });
  const qs = p.toString();
  return '#/' + screen + (qs ? '?' + qs : '');
}
