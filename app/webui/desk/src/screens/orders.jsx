// orders.jsx — «Заказы»: список с фильтрами и bulk-действиями, карточка заказа, чат заказа.

import { DIcon } from '../icons.jsx';
import { AppCtx, StBadge, Sec, Empty, Drawer, Field } from '../ui.jsx';
import { ST } from '../statuses.jsx';
import { API, call, useApi, usePoll } from '../api.jsx';
import { money, grams, minutes, dt, num, curSym } from '../fmt.jsx';
import { Screen, Chips, Search, Split, ABtn, PickOne, KV, useAutoScroll, useConfirm, Loading, ErrBox, Attached, Bubble, pastedImage, readImage } from '../parts.jsx';
import { OrderViewer3D, canShow3D } from '../three3d.jsx';
import { GfxSec } from './gridfinity.jsx';

const { useState, useEffect, useContext, useCallback, useMemo, useRef } = React;

// Фильтры-чипы над списком. Считаются по загруженному списку — счётчики честные.
const FLT = {
  all: { l: 'Все', f: () => true },
  new: { l: 'Новые', ic: 'bolt', f: (o) => o.status === 'new' },
  msg: { l: 'Сообщения', ic: 'chat', f: (o) => (o.unread || 0) > 0 },
  price: { l: 'Ждёт цены', ic: 'coin', f: (o) => o.status === 'price_changed' || o.pending != null },
  work: { l: 'В работе', ic: 'flame', f: (o) => o.status === 'in_progress' || o.status === 'printing' },
  done: { l: 'Готовые', ic: 'check', f: (o) => ['printed', 'done', 'handed'].includes(o.status) },
  deferred: { l: 'Отложенные', ic: 'pause', f: (o) => o.status === 'deferred' },
};

// «Для себя» — не фильтр по загруженному списку, а другой источник: самопечать мастера
// исключена из /api/admin/orders и живёт в /api/admin/self_orders. Поэтому чип стоит
// отдельно, у правого края, и переключает список целиком.
const SELF = 'self';

// Статусы для массовой смены: только те, куда мастер осмысленно двигает пачку заказов.
// «Вручено» тут не место: это статус заказа ГОТОВОГО ИЗДЕЛИЯ, обычному (печатному) заказу
// сервер его не поставит (webapp._api_admin_order_status), и вся пачка ушла бы в отказ.
const BULK_ST = ['new', 'in_progress', 'deferred', 'printed', 'rejected'];

// Числовые ключи сортировки — их сравниваем вычитанием, остальные локалью.
const NUM_KEYS = ['id', 'price', 'qty', 'created_at'];

// Системный чекбокс на оранжевой теме синий — красим акцентом (как в макете).
const CB = { width: 15, height: 15, accentColor: 'var(--accent)', pointerEvents: 'none' };

// В колонку 96px «5 часов назад» не влезает: в таблице нужен короткий вид, как в макете.
function agoShort(ts) {
  if (!ts) return '—';
  const m = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)) / 60);
  if (m < 60) return Math.round(m) + ' мин';
  if (m < 1440) return Math.round(m / 60) + ' ч';
  return Math.round(m / 1440) + ' дн';
}

/** Кликабельный заголовок колонки со стрелкой .sa — .tbl th в макете кликабельны по смыслу. */
function Th({ k, l, sort, setSort, style }) {
  return <th style={style} onClick={() => setSort((s) => ({ k, d: s.k === k ? -s.d : 1 }))}>
    {l}{sort.k === k ? <span className="sa">{sort.d > 0 ? '↑' : '↓'}</span> : null}
  </th>;
}

export function OrdersScreen() {
  const { params, nav, toast, ask, lightbox, drawer, setDrawer, reloadHub, can } = useContext(AppCtx);
  const [q, setQ] = useState('');
  const [flt, setFlt] = useState('all');
  const [sort, setSort] = useState({ k: 'created_at', d: -1 });
  const [marked, setMarked] = useState(() => new Set());
  const [qSent, setQSent] = useState('');

  // Поиск уходит на сервер (db.list_requests(search=)), но с задержкой.
  useEffect(() => {
    const t = setTimeout(() => setQSent(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  const isSelf = flt === SELF;
  // Самопечать подгружаем только после первого захода в неё и дальше держим загруженной:
  // возвращаться туда-сюда мастер будет часто, а перезапрашивать при каждом переключении —
  // лишний стук в бота.
  const [selfSeen, setSelfSeen] = useState(false);
  useEffect(() => { if (isSelf) setSelfSeen(true); }, [isSelf]);

  const list = useApi('adminOrders', [qSent], [qSent], { auto: 12000 });
  const selfList = useApi('adminSelfOrders', [qSent], [qSent],
    { skip: !selfSeen, auto: isSelf ? 12000 : 0 });
  const src = isSelf ? selfList : list;
  const orders = (src.data && src.data.orders) || [];
  // Права роли (A4): оператор видит очередь работ, но не суммы — колонку цены не показываем.
  const canMoney = !src.data || src.data.money !== false;
  // Счётчики чипов всегда считаем по клиентскому списку: иначе, стоя в «Для себя», мастер
  // видел бы «Новые 3» про самопечать и, кликнув, попал бы в другой список с другой тройкой.
  const clientOrders = (list.data && list.data.orders) || [];
  const sel = params.sel ? Number(params.sel) : null;

  const shown = useMemo(() => {
    const f = isSelf ? { f: () => true } : (FLT[flt] || FLT.all);
    const k = sort.k, d = sort.d;
    return orders.filter(f.f).slice().sort((a, b) => (NUM_KEYS.includes(k)
      ? ((Number(a[k]) || 0) - (Number(b[k]) || 0)) * d
      : String(a[k] == null ? '' : a[k]).localeCompare(String(b[k] == null ? '' : b[k]), 'ru') * d));
  }, [orders, flt, isSelf, sort]);

  const pick = useCallback((id) => nav('orders', { sel: id }), [nav]);

  // Стрелки по списку (j/k живут в топбаре — там они общие для всех таблиц).
  useEffect(() => {
    const h = (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const tg = (e.target.tagName || '').toLowerCase();
      if (tg === 'input' || tg === 'textarea' || e.target.isContentEditable) return;
      if (!shown.length) return;
      e.preventDefault();
      const i = shown.findIndex((o) => o.id === sel);
      const n = shown[Math.max(0, Math.min(shown.length - 1, i < 0 ? 0 : i + (e.key === 'ArrowDown' ? 1 : -1)))];
      if (n) pick(n.id);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [shown, sel, pick]);

  const counts = useMemo(() => {
    const c = {};
    Object.entries(FLT).forEach(([k, v]) => { c[k] = clientOrders.filter(v.f).length; });
    return c;
  }, [clientOrders]);

  const refresh = useCallback(() => {
    list.reload();
    if (selfSeen) selfList.reload();
    reloadHub();
  }, [list, selfList, selfSeen, reloadHub]);

  // Прогон действия по выборке. Сервер отвечает по КАЖДОМУ заказу отдельно (недоступный
  // статус, права роли, уже удалённый заказ), поэтому первая ошибка не обрывает пачку:
  // иначе половина выборки осталась бы необработанной, да ещё и молча. Отказы копим и
  // показываем сводкой, наверх отдаём то, что реально прошло.
  const bulkRun = async (items, fn) => {
    const done = [], bad = [];
    for (const it of items) {
      try {
        await fn(it);
        done.push(it);
      } catch (e) {
        bad.push(`#${it && it.id != null ? it.id : it}: ${e.message}`);
      }
    }
    if (bad.length) {
      toast(`Не прошло ${num(bad.length)} из ${num(items.length)} · ${bad[0]}`,
        { kind: 'warn2', ms: 9000 });
    }
    return done;
  };

  // Массовая смена статуса тоже откатывается: каждому заказу возвращается ЕГО прежний
  // статус, а не общий — иначе откат сам стал бы новой ошибкой.
  const bulkStatus = async (status) => {
    const ids = [...marked];
    if (!ids.length) return;
    const prev = ids.map((id) => {
      const o = orders.find((x) => x.id === id);
      return { id, status: o ? o.status : null };
    }).filter((x) => x.status);
    const okIds = await bulkRun(ids, (id) => call('adminOrderStatus', id, status));
    setMarked(new Set());
    // Считаем по фактически применённым: «Статус — 8 зак.» при трёх отказах было бы враньём.
    if (okIds.length) {
      const back = prev.filter((p) => okIds.indexOf(p.id) >= 0);
      toast(`Статус «${(ST[status] || {}).l || status}» — ${num(okIds.length)} зак.`, {
        kind: 'ok', ms: 9000, action: 'Отменить',
        onAction: async () => {
          const ret = await bulkRun(back, (p) => call('adminOrderStatus', p.id, p.status));
          if (ret.length) toast(`Статусы возвращены: ${num(ret.length)}`, { kind: 'ok' });
          refresh();
        },
      });
    }
    refresh();
  };

  const bulk = async (fn, okMsg) => {
    const ids = [...marked];
    if (!ids.length) return;
    const okIds = await bulkRun(ids, fn);
    setMarked(new Set());
    if (okIds.length) toast(`${okMsg}: ${num(okIds.length)}`, { kind: 'ok' });
    refresh();
  };

  const allSel = shown.length > 0 && shown.every((o) => marked.has(o.id));
  const togAll = () => setMarked(allSel ? new Set() : new Set(shown.map((o) => o.id)));
  const togOne = (id) => setMarked((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  const listPane = <>
    <div className="ms-tools">
      <Search value={q} onChange={setQ} placeholder="Номер, модель, клиент…" />
      {/* Обе кнопки ведут в ручки под правом «Деньги» (order/create и batch_calc): у оператора
          они открывали форму, которая на «Создать» отвечала бы отказом сервера. Прячем — как
          это уже сделано для плитки дашборда. */}
      {can('finance') ? <button className="db" onClick={() => setDrawer('create')}>
        <DIcon n="plus" size={15} />За клиента
      </button> : null}
      {can('finance') ? <button className="db line" onClick={() => setDrawer('batch')}>
        <DIcon n="link" size={15} />Пакет ссылок
      </button> : null}
      <button className="ib" title="Обновить" onClick={refresh}><DIcon n="refresh" size={16} /></button>
      <button className="ib" title="Отметить всё просмотренным" onClick={() => ask({
        title: 'Отметить всё просмотренным?',
        text: 'Счётчики новых заказов и непрочитанных сообщений будут сброшены.',
        cta: 'Просмотрено',
        onYes: async () => {
          const r = await call('adminOrdersMarkAllSeen');
          toast(`Отмечено: ${num(r.marked || 0)}`, { kind: 'ok' });
          refresh();
        },
      })}><DIcon n="check" size={16} /></button>
      {/* Чистка тестовых: сервер сам выбирает заказы без цены и с 0 г / 0 мин — критерий
          повторять на клиенте нельзя, иначе цифра в подтверждении разойдётся с делом.
          Ручка не названа ни в одной карте прав, то есть закрыта «Настройками»: оператору
          кнопка показывала грозное подтверждение и упиралась в отказ. */}
      {can('settings') ? <button className="ib" title="Удалить тестовые заказы" onClick={() => ask({
        title: 'Удалить тестовые заказы?',
        text: 'Будут удалены заказы без цены и с нулевыми граммами и временем — вместе с их файлами. Отменить нельзя.',
        cta: 'Удалить', danger: true,
        onYes: async () => {
          try {
            const r = await call('adminOrdersCleanTest');
            toast(`Удалено тестовых: ${num(r.deleted || 0)}`, { kind: 'ok' });
            refresh();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        },
      })}><DIcon n="trash" size={16} /></button> : null}
      {/* Выгрузка всех заказов в файл — /api/admin/orders/export.csv, право «Настройки».
          Кнопка у оператора открывала вкладку с отказом сервера. */}
      {can('settings') ? <button className="ib" title="Экспорт CSV" onClick={() => {
        const url = API.adminOrdersCsvUrl && API.adminOrdersCsvUrl();
        if (url) window.open(url, '_blank');
        else toast('Экспорт недоступен без входа в админку', { kind: 'warn2' });
      }}><DIcon n="doc" size={16} /></button> : null}
    </div>

    {marked.size > 0 ? <div className="bulkbar">
      <b>{num(marked.size)}</b> выбрано
      <select className="sel" style={{ width: 210 }} value="" onChange={(e) => {
        const v = e.target.value;
        if (v) bulkStatus(v);
      }}>
        <option value="">Сменить статус…</option>
        {BULK_ST.map((k) => <option key={k} value={k}>{ST[k].l}</option>)}
      </select>
      {/* Удаление заказа — /api/admin/order/delete, право «Настройки». Оператору кнопка
          показывала подтверждение «отменить нельзя» и падала уже ПОСЛЕ его согласия. */}
      {can('settings') ? <button className="db sm danger" onClick={() => ask({
        title: 'Удалить заказы?',
        text: `Будет удалено ${marked.size} шт. Файлы заказов тоже удаляются. Отменить нельзя.`,
        cta: 'Удалить', danger: true,
        onYes: () => bulk((id) => call('adminOrderDelete', id), 'Удалено'),
      })}><DIcon n="trash" size={14} />Удалить</button> : null}
      <button className="db sm line" style={{ marginLeft: 'auto' }}
        onClick={() => setMarked(new Set())}>✕ Снять</button>
    </div> : <Chips
      items={Object.entries(FLT)
        // фильтр без единого заказа только мешает — кроме «Все», который держит сброс
        .filter(([k]) => k === 'all' || counts[k])
        .map(([k, v]) => ({ k, l: v.l, ic: v.ic, n: counts[k] }))
        // «Для себя» показываем всегда, даже пустым: это не фильтр, а вход в другой список,
        // и спрятать его значило бы спрятать сам раздел. Кроме одного случая: /api/admin/
        // self_orders не назван ни в одной карте прав, то есть закрыт по умолчанию правом
        // «Настройки». Оператору эта вкладка открывалась в пустой список с ошибкой.
        .concat(can('settings') ? [{ k: SELF, l: 'Для себя', ic: 'flame', right: true,
          n: selfSeen && selfList.data ? (selfList.data.orders || []).length : undefined }] : [])}
      value={flt} onChange={setFlt} />}

    <div className="twrap">
      <Screen state={src}>{() => <>
        <table className="tbl">
          <thead><tr>
            <th style={{ width: 42 }} onClick={togAll}>
              <input type="checkbox" readOnly checked={allSel} style={CB} />
            </th>
            <th style={{ width: 58, cursor: 'default' }}></th>
            <Th k="id" l="№" sort={sort} setSort={setSort} style={{ width: 70 }} />
            <Th k="label" l="Модель" sort={sort} setSort={setSort} />
            <Th k="user" l="Клиент" sort={sort} setSort={setSort} style={{ width: 180 }} />
            <Th k="status_label" l="Статус" sort={sort} setSort={setSort} style={{ width: 150 }} />
            <Th k="qty" l="Шт" sort={sort} setSort={setSort} style={{ width: 52 }} />
            {canMoney ? <Th k="price" l="Цена" sort={sort} setSort={setSort} style={{ width: 100 }} /> : null}
            <Th k="created_at" l="Создан" sort={sort} setSort={setSort} style={{ width: 96 }} />
          </tr></thead>
          <tbody>
            {shown.map((o) => <tr key={o.id} className={'row' + (o.id === sel ? ' sel' : '')}
              onClick={() => pick(o.id)}>
              <td onClick={(e) => { e.stopPropagation(); togOne(o.id); }}>
                <input type="checkbox" readOnly checked={marked.has(o.id)} style={CB} />
              </td>
              <td>{o.preview ? <img className="pv" src={o.preview} alt="" style={{ cursor: 'zoom-in' }}
                onClick={(e) => { e.stopPropagation(); lightbox(o.preview); }} /> : null}</td>
              <td className="mono faint">#{o.id}</td>
              <td>
                <div style={{ display: 'flex', alignItems: 'center', gap: 9, minWidth: 0 }}>
                  <span className="oname">{o.label || '—'}</span>
                  {o.unread > 0 ? <span className="msg-pill" title="непрочитанные">{o.unread}</span> : null}
                  {/* Заказ-корзина (B5): позиция входит в группу ≥2 — пометка «N поз · итог» */}
                  {o.group ? <span className="tag" title="Заказ из нескольких моделей"
                    style={{ color: 'var(--accent)', borderColor: 'var(--accent-line)', background: 'var(--accent-soft)', flex: '0 0 auto' }}>
                    🧺 {o.group_count}{o.group_total != null ? ' · ' + money(o.group_total) : ' поз.'}
                  </span> : null}
                </div>
              </td>
              <td style={{ color: 'var(--muted)' }}>{o.user || '—'}</td>
              {/* 💲 у оплаченных заказов — рядом со статусом, как просил владелец */}
              <td><StBadge st={o.status} />{o.paid
                ? <span title="оплачен" style={{ marginLeft: 6 }}>💲</span> : null}</td>
              <td className="mono faint">{o.qty || 1}</td>
              {canMoney ? <td className="mono" style={{ fontWeight: 600 }}>
                {o.price ? money(o.price) : (o.pending != null ? <span className="stb amber">ждёт</span> : '—')}
              </td> : null}
              <td className="mono faint">{agoShort(o.created_at)}</td>
            </tr>)}
          </tbody>
        </table>
        {!shown.length ? <Empty ic="receipt"
          t={isSelf && !q ? 'Печати для себя пока нет' : 'Заказов не найдено'}
          ds={isSelf && !q ? 'Сюда попадают заказы мастера самому себе'
            : (q || flt !== 'all' ? 'Попробуйте сбросить фильтр' : 'Пока ни одной заявки')}
          cta={q || flt !== 'all' ? 'Сбросить фильтр' : null}
          onCta={() => { setQ(''); setFlt('all'); }} /> : null}
      </>}</Screen>
    </div>

    <div className="hint" style={{ marginTop: 10, display: 'flex', gap: 14, alignItems: 'center' }}>
      <span><span className="kbd">↑</span> <span className="kbd">↓</span> — по списку</span>
      <span><span className="kbd">/</span> — поиск</span>
      <span style={{ marginLeft: 'auto' }}>{num(shown.length)} из {num(orders.length)}</span>
    </div>
  </>;

  // key={sel} обязателен: иначе поля «изменить цену» и «срок» остались бы от прошлого заказа.
  const detail = sel
    ? <OrderDetail key={sel} id={sel} onChanged={refresh} />
    : <PickOne what="заказ" />;

  return <>
    <Split width={1130} list={listPane} detail={detail} />
    {drawer === 'create' && <CreateOrderDrawer onClose={() => setDrawer(null)} onDone={refresh} />}
    {drawer === 'batch' && <BatchCalcDrawer onClose={() => setDrawer(null)} />}
  </>;
}

// ---------------- карточка заказа ----------------

function OrderDetail({ id, onChanged }) {
  const { toast, nav, lightbox } = useContext(AppCtx);
  // Карточка живая: статус меняет и сам бот (печать, оплата), а сообщения клиента приходят
  // в чат заказа. Обновляем тихо — набранная цена, срок и текст сообщения не сбрасываются.
  const d = useApi('adminOrder', [id], [id], { auto: 7000 });

  if (d.loading && !d.data) return <Loading what="Открываю заказ…" />;
  // Ошибка фонового обновления не должна стирать открытую карточку: бот мог перезапускаться.
  if (d.err && !d.data) return <ErrBox err={d.err} onRetry={d.reload} />;
  const o = d.data && d.data.order;
  if (!o) return <Empty t="Заказ не найден" />;

  const after = () => { d.reload(); onChanged(); };
  // Права роли (A4): у оператора карточка приезжает без цен, счетов, баланса и себестоимости —
  // денежные секции не рисуем вовсе (сервер эти поля уже вырезал).
  const canMoney = d.data.money !== false;

  return <div className="od">
    <OrderHead o={o} onReload={d.reload} canMoney={canMoney}
      onDeleted={() => { onChanged(); nav('orders', {}); }} nav={nav} toast={toast} />

    <div className="od-body">
      <div className="od-main">
        {o.group && o.group.count > 1 ? <GroupSec o={o} nav={nav} canMoney={canMoney} /> : null}
        {/* Доставка живёт на группе: цена — владельцу, трек-номер — всей команде (A2).
            Секции нет у заказов, где способ получения не выбирали (старые корзины до A2). */}
        {o.group && (o.group.delivery_method || o.group.delivery_cost
          || (o.group.track && (o.group.track.number || o.group.track.shipped_at)))
          ? <DeliverySec o={o} canMoney={canMoney} after={after} toast={toast} /> : null}
        <ModelSec o={o} lightbox={lightbox} after={after} toast={toast} />
        {/* Правая колонка макета: расчёт, себестоимость и аудит-трейл идут стопкой */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20, minWidth: 0 }}>
          {canMoney ? <BreakdownSec o={o} nav={nav} /> : null}
          {canMoney ? <CogsSec o={o} /> : null}
          <HistorySec o={o} />
        </div>
        {o.description ? <WishSec o={o} lightbox={lightbox} /> : null}
        {/* Раскладка органайзера: план ящика + печать-лист с отметкой «готово» по детали.
            Блок сам решает, показываться ли — заявка без снимка отдаёт 404. */}
        {o.is_custom ? <GfxSec o={o} toast={toast} /> : null}
        {o.is_custom && canMoney ? <InvoiceSec o={o} after={after} toast={toast} /> : null}
        <StatusSec o={o} after={after} toast={toast} canMoney={canMoney} />
        {canMoney ? <PriceSec o={o} after={after} toast={toast} /> : null}
        {canMoney ? <RecalcSec o={o} after={after} toast={toast} /> : null}
        <FilesSec o={o} toast={toast} />
        <TimelapseSec o={o} after={after} toast={toast} />
        {canMoney ? <BalanceSec o={o} after={after} toast={toast} /> : null}
        <PhotoSec o={o} after={after} lightbox={lightbox} toast={toast} />
        <ReviewSec o={o} />
      </div>
      <OrderChat o={o} after={after} />
    </div>
  </div>;
}

// ---------------- доставка и отправление заказа (A2) ----------------
// Живёт на ГРУППЕ, а не на позиции: адрес, стоимость и трек-номер одни на весь заказ.
// Стоимость правит только владелец (это деньги), трек-номер вводит вся команда — отправка
// посылки относится к производству, а не к кассе.
function DeliverySec({ o, canMoney, after, toast }) {
  const g = o.group, track = g.track || {};
  const dict = useApi('adminDelivery', [], []);
  const [carrier, setCarrier] = useState(track.carrier || '');
  const [num, setNum] = useState(track.number || '');
  const [url, setUrl] = useState(track.url || '');
  const [cost, setCost] = useState('');
  const carriers = (dict.data && dict.data.carriers) || [];
  const isPickup = g.delivery_method === 'pickup';

  const ship = async (action) => {
    const r = await call('adminOrderShipment', { gid: g.gid, action, carrier, number: num, url });
    toast(action === 'ship' ? 'Клиенту ушло уведомление с трек-номером'
      : action === 'delivered' ? 'Отмечено доставленным'
      : action === 'clear' ? 'Отправление снято' : 'Сохранено', { kind: 'ok' });
    if (r && r.track) { setCarrier(r.track.carrier || ''); setNum(r.track.number || ''); setUrl(r.track.url || ''); }
    after();
  };
  const saveCost = async () => {
    const v = String(cost).replace(',', '.').trim();
    if (v === '') { toast('Введите стоимость', { kind: 'err' }); return; }
    await call('adminOrderDelivery', { gid: g.gid, cost: Number(v) });
    toast('Стоимость доставки обновлена', { kind: 'ok' });
    setCost('');
    after();
  };

  return <Sec t={<span><DIcon n="truck" size={13} /> {isPickup ? 'Получение' : 'Доставка'}</span>} span2>
    <div style={{ fontSize: 13.5 }}>
      {isPickup ? '🏬 Самовывоз' : '🚚 Доставка'}
      {g.delivery_type ? <span className="faint"> · {g.delivery_type}</span> : null}
      {g.delivery_address ? <span className="faint"> · {g.delivery_address}</span> : null}
    </div>
    {canMoney ? <div className="mono" style={{ marginTop: 8 }}>
      позиции {money(g.items_total)} + доставка {money(g.delivery_cost)} ={' '}
      <b style={{ color: 'var(--price)' }}>{money(g.total_with_delivery)}</b>
      {g.delivery_paid ? <span className="stb green" style={{ marginLeft: 8 }}>доставка оплачена</span> : null}
    </div> : null}
    {canMoney && !g.delivery_paid ? <div className="frow" style={{ marginTop: 10 }}>
      <Field l="Стоимость доставки">
        <input className="inp mono" type="number" step="0.01" min="0" value={cost}
          placeholder={String(g.delivery_cost || 0)} onChange={(e) => setCost(e.target.value)} />
      </Field>
      <div style={{ alignSelf: 'end' }}><ABtn kind="soft" onClick={saveCost}>Изменить</ABtn></div>
    </div> : null}

    {!isPickup ? <div style={{ marginTop: 14 }}>
      <div className="frow">
        <Field l="Перевозчик">
          <select className="inp" value={carrier} onChange={(e) => setCarrier(e.target.value)}>
            <option value="">— выберите —</option>
            {carriers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </Field>
        <Field l="Трек-номер">
          <input className="inp mono" value={num} onChange={(e) => setNum(e.target.value)} />
        </Field>
      </div>
      {/* Справочник перевозчиков лежит под правом «Прайс и каталог» (в нём цены тарифов), а
          отправка посылки — работа оператора. Пустой выпадающий список выглядел поломкой;
          говорим прямо и показываем рабочий обход: ссылку можно вписать руками. */}
      {dict.err && !carriers.length ? <div className="hint" style={{ marginBottom: 8 }}>
        Справочник перевозчиков доступен владельцу — впишите ссылку отслеживания вручную.
      </div> : null}
      <Field l="Ссылка отслеживания (если своя)">
        <input className="inp" value={url} onChange={(e) => setUrl(e.target.value)}
          placeholder="У СДЭК, Почты и других соберётся сама по номеру" />
      </Field>
      <div style={{ display: 'flex', gap: 9, marginTop: 10, flexWrap: 'wrap' }}>
        <ABtn kind="line" onClick={() => ship('save')}>Сохранить</ABtn>
        <ABtn icon="send" onClick={() => ship('ship')}>
          {track.shipped_at ? 'Переотправить' : 'Отправлено — уведомить клиента'}
        </ABtn>
        {track.shipped_at ? <ABtn kind="line" icon="check" onClick={() => ship('delivered')}>
          {track.delivered_at ? 'Доставлено ✓' : 'Доставлено'}
        </ABtn> : null}
        {track.shipped_at ? <ABtn kind="line" onClick={() => ship('clear')}>Снять отправку</ABtn> : null}
      </div>
      {track.url ? <div className="hint" style={{ marginTop: 8 }}>
        <a href={track.url} target="_blank" rel="noopener">Проверить ссылку отслеживания</a>
      </div> : null}
    </div> : null}
  </Sec>;
}

function OrderHead({ o, onReload, onDeleted, nav, toast, canMoney }) {
  const { can } = useContext(AppCtx);
  const [menu, setMenu] = useState(false);
  const confirm = useConfirm();
  useEffect(() => setMenu(false), [o.id]);
  return <div className="od-head" style={{ position: 'relative' }}>
    <div style={{ flex: 1, minWidth: 0 }}>
      <div className="tt"><span className="id">#{o.id}</span>{o.label || 'Без названия'}</div>
      <div className="sub">
        <a onClick={() => nav('clients', { sel: o.user_id })} style={{ cursor: 'pointer' }}>{o.user}</a>
        {o.qty > 1 ? ` · ${o.qty} шт` : ''}
        {o.dims ? ` · ${o.dims} мм` : ''}
        {o.due ? ` · срок ${o.due}` : ''}
        {canMoney && o.paid ? ' · оплачен ✓' : ''}
      </div>
      {/* Постобработка (B4): плашка-тег — мастеру сразу видно, что нужна ручная работа после печати. */}
      {o.options && o.options.length ? <div style={{ marginTop: 7 }}>
        <span className="tag" style={{ color: 'var(--accent)', borderColor: 'var(--accent-line)', background: 'var(--accent-soft)' }}>
          <DIcon n="palette" size={12} /> {o.options.map((x) => x.name).join(' · ')}
        </span>
      </div> : null}
    </div>
    <StBadge st={o.status} />
    {canMoney ? <div className="price">{o.price ? money(o.price) : '—'}</div> : null}
    {canMoney ? <ABtn kind="soft sm" icon="doc" onClick={async () => {
      await call('orderPdf', o.id);
      toast('PDF-смета отправлена вам в Telegram', { kind: 'ok' });
    }}>PDF-смета</ABtn> : null}
    <button className="ib" onClick={() => setMenu((m) => !m)}><DIcon n="dots" /></button>
    {menu && <div className="menu">
      <button onClick={() => { setMenu(false); onReload(); }}>
        <DIcon n="refresh" size={16} />Обновить заказ
      </button>
      {o.src_url ? <button onClick={() => { setMenu(false); window.open(o.src_url, '_blank'); }}>
        <DIcon n="external" size={16} />Открыть оригинал модели
      </button> : null}
      {o.src_url ? <button onClick={() => {
        setMenu(false);
        navigator.clipboard.writeText(o.src_url);
        toast('Ссылка скопирована', { kind: 'ok', ms: 2500 });
      }}><DIcon n="copy" size={16} />Копировать ссылку на модель</button> : null}
      {/* Удаление — право «Настройки» (см. кнопку массового удаления выше). */}
      {can('settings') ? <hr /> : null}
      {can('settings') ? <button className="danger" onClick={() => {
        setMenu(false);
        confirm('Удалить заказ #' + o.id + '?',
          'Заказ, переписка и файлы будут удалены безвозвратно. Клиент уведомлён не будет.',
          async () => { await call('adminOrderDelete', o.id); toast('Заказ удалён', { kind: 'warn2' }); onDeleted(); },
          'Удалить навсегда');
      }}><DIcon n="trash" size={16} />Удалить заказ…</button> : null}
    </div>}
  </div>;
}

function ModelSec({ o, lightbox, after, toast }) {
  // Габариты, количество и срок уже стоят в шапке карточки — здесь, как в макете, только
  // параметры печати. mono — для чисел, чтобы столбец значений не «плясал».
  const sc = Number(o.scale_pct) || 0;
  const rows = [
    // Масштаб, в котором клиент заказал модель: печатать в нём, а не в 100% файла. Не 100% —
    // подсвечиваем, это легко проглядеть. Нет значения — масштаб неизвестен или неприменим
    // (индивидуальный заказ, старый заказ без сметы), строку не рисуем (orderdesk._order_scale_pct).
    ['Масштаб', !sc ? '' : sc === 100 ? '100%'
      : <span style={{ color: 'var(--accent)', fontWeight: 700 }}>{sc}%</span>, true],
    ['Вес модели', o.grams ? grams(o.grams) + (o.qty > 1 ? ` × ${o.qty}` : '') : '', true],
    // Качество и прочность не показываем: клиент их не выбирает (8c8199d1), печатаем
    // профилем мастера — в карточке это мёртвые строки.
    ['Время печати', o.minutes ? minutes(o.minutes) : '', true],
  ].filter((r) => r[1]);
  // 3D-просмотр заказанной модели (B1 для мастера): кнопка-оверлей на превью открывает вьюер,
  // который тянет GLB заказа с /api/admin/orderfile и красит детали цветами заказа (a3dff103).
  // show3d прячем на сбое/недоступности, чтобы не оставлять мёртвую кнопку — карточка тихо
  // остаётся на PNG.
  const [open3d, setOpen3d] = useState(false);
  const [show3d, setShow3d] = useState(canShow3D(o));
  const [editMat, setEditMat] = useState(false);
  useEffect(() => { setShow3d(canShow3D(o)); setOpen3d(false); setEditMat(false); }, [o.id]);
  const url3d = show3d ? API.adminOrderFileUrl(o.id) : '';
  const ccss = o.colors_css || [];
  return <Sec t={<span><DIcon n="grid" size={13} /> Модель</span>}>
    {o.preview ? <div className="od-preview-wrap">
      <img className="od-preview" src={o.preview} alt="" onClick={() => lightbox(o.preview)} />
      {show3d && url3d ? <button type="button" className="view3d-btn"
        title="Покрутить модель в 3D" onClick={(e) => { e.stopPropagation(); setOpen3d(true); }}>
        <DIcon n="box" size={14} /> 3D
      </button> : null}
    </div> : null}
    {/* Нет превью, но файл модели есть — даём отдельную кнопку «Смотреть в 3D». */}
    {!o.preview && show3d && url3d ? <button className="db line sm" style={{ marginBottom: 12 }}
      onClick={() => setOpen3d(true)}><DIcon n="box" size={14} /> Смотреть в 3D</button> : null}
    <div style={{ marginTop: 14 }}>
      {/* Пластик заказа: кружки цветов + карандаш смены пластика/цвета (e132724b) */}
      <KV k="Пластик" mono={false} v={<span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
        {ccss.slice(0, 8).map((c, i) => <span key={i} style={{
          width: 12, height: 12, borderRadius: '50%', background: c,
          display: 'inline-block', flex: '0 0 auto', border: '1px solid var(--line)' }} />)}
        <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{o.plastic || '—'}</span>
        <button className="ib" title="Сменить пластик или цвет"
          onClick={() => setEditMat((v) => !v)}><DIcon n="pencil" size={13} /></button>
      </span>} />
      {editMat ? <MaterialEdit o={o} toast={toast}
        onDone={() => { setEditMat(false); if (after) after(); }}
        onCancel={() => setEditMat(false)} /> : null}
      {rows.map(([k, v, m]) => <KV key={k} k={k} v={v} mono={m} />)}
      {o.src_url ? <KV k="Оригинал" mono={false} v={
        <a href={o.src_url} target="_blank" rel="noreferrer">
          {String(o.src_url).replace(/^https?:\/\//, '').slice(0, 34)}… <DIcon n="external" size={12} />
        </a>} /> : null}
    </div>
    {open3d && url3d ? <OrderViewer3D url={url3d} dims={o.dims || ''}
      colors={ccss} multicolor={!!o.multicolor || ccss.length > 1}
      onClose={() => setOpen3d(false)}
      onFallback={() => setShow3d(false)} /> : null}
  </Sec>;
}

// Редактор пластика/цвета заказа (OpenTodo e132724b): раньше «нельзя/неудобно» — материал
// правился только через БД. Пластики и палитры берём из adminPlastics (те же color_css, что
// в мини-аппе); слотов цвета столько, сколько цветов сейчас у заказа (мультицвет — по слоту
// на цвет печати). Цену НЕ трогаем — для этого есть «Изменить цену»/«Пересчитать».
function MaterialEdit({ o, toast, onDone, onCancel }) {
  const pl = useApi('adminPlastics', [], []);
  const plastics = ((pl.data && pl.data.plastics) || []).filter((p) => p.is_active);
  // текущие имена цветов заказа — хвост plastic_name после « · », по слотам через « - »
  const curColors = (() => {
    const t = String(o.plastic || '').split(' · ');
    return t.length === 2 ? t[1].split(' - ').map((s) => s.trim()).filter(Boolean) : [];
  })();
  const nSlots = Math.max(1, curColors.length);
  const [pid, setPid] = useState(0);
  const [cols, setCols] = useState(curColors.length ? curColors : ['']);
  const [busy, setBusy] = useState(false);
  const cur = plastics.find((p) => p.id === pid)
    || plastics.find((p) => String(o.plastic || '').indexOf(p.name) === 0) || null;
  useEffect(() => {   // первый приход списка: подсветить пластик заказа, если нашёлся по имени
    if (!pid && cur) setPid(cur.id);
  }, [plastics.length]);   // eslint-disable-line
  const palette = (cur && cur.colors) || [];
  const css = (cur && cur.color_css) || {};
  function setSlot(i, v) { setCols((c) => { const n = c.slice(); n[i] = v; return n; }); }
  async function save() {
    if (!cur) return;
    setBusy(true);
    try {
      const chosen = cols.slice(0, nSlots).map((c) => (palette.indexOf(c) >= 0 ? c : palette[0] || ''))
        .filter(Boolean);
      await call('adminOrderMaterial', o.id, cur.id, chosen);
      toast('Материал заказа обновлён', { kind: 'ok' });
      onDone();
    } catch (e) {
      toast(e.message, { kind: 'warn2', ms: 8000 });
    } finally { setBusy(false); }
  }
  if (pl.loading && !pl.data) return <div className="hint" style={{ margin: '6px 0 10px' }}>Загружаю пластики…</div>;
  if (!plastics.length) return <div className="hint" style={{ margin: '6px 0 10px' }}>Активных пластиков нет</div>;
  return <div style={{ margin: '6px 0 12px', padding: '10px 12px', borderRadius: 11, background: 'var(--surface-2)' }}>
    <select className="sel" value={(cur && cur.id) || ''} onChange={(e) => {
      const p = plastics.find((x) => x.id === Number(e.target.value));
      setPid(p ? p.id : 0);
      // палитра сменилась — слоты пере-дефолтим первым цветом новой палитры
      if (p) setCols(Array.from({ length: nSlots }, (_, i) => (p.colors || [])[i % Math.max(1, (p.colors || []).length)] || ''));
    }}>
      {plastics.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
    </select>
    {Array.from({ length: nSlots }, (_, i) => <div key={i}
      style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
      {nSlots > 1 ? <span className="hint" style={{ flex: '0 0 auto', width: 52 }}>Цвет {i + 1}</span> : null}
      {palette.map((c) => <button key={c} type="button" title={c}
        onClick={() => setSlot(i, c)}
        style={{
          width: 20, height: 20, borderRadius: '50%', cursor: 'pointer',
          background: css[c] || '#8a97a5', padding: 0,
          border: cols[i] === c ? '2px solid var(--accent)' : '1px solid var(--line)' }} />)}
      <span className="hint" style={{ flex: '0 0 auto' }}>{cols[i] || '—'}</span>
    </div>)}
    <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
      <button className="db sm" disabled={busy || !cur} onClick={save}>{busy ? 'Сохраняю…' : 'Сохранить'}</button>
      <button className="db sm line" disabled={busy} onClick={onCancel}>Отмена</button>
    </div>
    <div className="hint" style={{ marginTop: 8 }}>Цена не пересчитывается — для этого есть «Изменить цену» и «Пересчитать».</div>
  </div>;
}

// Шапка заказа-корзины (B5): сёстры-позиции, общий итог, доставка, комментарий. Клик по позиции
// открывает ЕЁ карточку — действия (статус/цена/пересчёт) остаются пер-позиционными, группу
// скопом не двигаем (смешанные статусы — норма).
function GroupSec({ o, nav, canMoney }) {
  const g = o.group;
  // Доставка ушла в отдельную секцию (A2) — здесь остались позиции и комментарий.
  return <Sec t={<span><DIcon n="grid" size={13} /> Заказ-корзина · {g.count} поз.</span>}
    right={canMoney
      ? <span className="mono" style={{ color: 'var(--price)', fontWeight: 700 }}>{money(g.total_with_delivery != null ? g.total_with_delivery : g.total)}</span>
      : null} span2>
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {(g.items || []).map((it) => <div key={it.id}
        onClick={() => { if (!it.current) nav('orders', { sel: it.id }); }}
        style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderRadius: 11,
          background: it.current ? 'var(--accent-soft)' : 'var(--surface-2)',
          cursor: it.current ? 'default' : 'pointer',
        }}>
        <span className="mono faint" style={{ flex: '0 0 auto' }}>#{it.id}</span>
        <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {it.label || '—'}{it.qty > 1 ? ` ×${it.qty}` : ''}
        </span>
        <StBadge st={it.status} />
        {canMoney ? <span className="mono" style={{ flex: '0 0 auto' }}>{it.price ? money(it.price) : '—'}</span> : null}
      </div>)}
    </div>
    {g.comment ? <div className="hint" style={{ marginTop: 6 }}>💬 {g.comment}</div> : null}
    <div className="hint" style={{ marginTop: 8 }}>
      Позиции печатаются и оцениваются по отдельности — статус, цена и пересчёт ниже относятся к #{o.id}.
    </div>
  </Sec>;
}

function BreakdownSec({ o, nav }) {
  const b = o.breakdown || {};
  // 🧮 из макета: открыть калькулятор, подставив граммы и время этого заказа
  const calcBtn = <button className="db sm line" title="Открыть калькулятор с параметрами заказа"
    onClick={() => nav('pricing', { calc: 1, grams: o.grams || '', minutes: o.minutes || '', qty: o.qty || 1 })}>
    <DIcon n="calc" size={14} />
  </button>;
  if (!b.rows || !b.rows.length) {
    return <Sec t={<span><DIcon n="calc" size={13} /> Расчёт цены</span>} right={calcBtn}>
      <div className="hint">Снимка расчёта нет — цена выставлена вручную.</div>
    </Sec>;
  }
  return <Sec t={<span><DIcon n="calc" size={13} /> Расчёт цены</span>} right={calcBtn}>
    {b.rows.map((r, i) => <div key={i} className="bd-row">
      <span>{r.label}{r.sub ? <i style={{ fontStyle: 'normal', color: 'var(--faint)' }}> · {r.sub}</i> : null}</span>
      <b>{r.value}</b>
    </div>)}
    <div className="bd-row tot"><span>Итого для клиента</span><b>{b.total}</b></div>
  </Sec>;
}

function CogsSec({ o }) {
  // Считается по списанию катушек при «напечатано»; у катушек без закупочной цены — пусто.
  if (o.cogs_actual == null) return null;
  const marginPct = o.price ? Math.round(((o.price - o.cogs_actual) / o.price) * 100) : null;
  return <Sec t={<span><DIcon n="coin" size={13} /> Себестоимость по факту</span>}>
    <div style={{ display: 'flex', gap: 26, alignItems: 'baseline' }}>
      <div>
        <div className="hint">материал по списанию</div>
        <div className="mono" style={{ fontSize: 22, fontWeight: 700 }}>{money(o.cogs_actual)}</div>
      </div>
      <div>
        <div className="hint">маржа</div>
        <div className="mono" style={{ fontSize: 22, fontWeight: 700, color: 'var(--price)' }}>
          {marginPct != null ? marginPct + '%' : '—'}
        </div>
      </div>
      {o.margin != null ? <div className="hint" style={{ marginLeft: 'auto', textAlign: 'right' }}>
        в деньгах<br />{money(o.margin)}
      </div> : null}
    </div>
  </Sec>;
}

function WishSec({ o, lightbox }) {
  return <Sec t={<span><DIcon n="chat" size={13} /> Что хочет клиент</span>} span2>
    <div style={{ fontSize: 14.5, lineHeight: 1.55, color: 'var(--text)', whiteSpace: 'pre-wrap' }}>
      {o.description || 'Без описания'}
    </div>
    {o.photos && o.photos.length ? <>
      <div className="photos">
        {o.photos.map((ph, i) => <img key={i} src={ph} alt="" onClick={() => lightbox(ph)} />)}
      </div>
      <div className="hint" style={{ marginTop: 8 }}>
        фото от клиента — клик для просмотра
      </div>
    </> : null}
  </Sec>;
}

/**
 * Счёт по индивидуальному заказу (POST /api/admin/order/invoice).
 *
 * Мастер сам находит и слайсит модель — либо даёт ссылку, и бот скачивает и считает сам
 * (POST /api/admin/order/slice). Сумма считается той же формулой, что у обычных заказов,
 * плюс плата за разработку; клиент подтверждает её тем же потоком, что и смену цены.
 */
function InvoiceSec({ o, after, toast }) {
  const plastics = useApi('adminPlastics', [], []);
  const [f, setF] = useState({
    plastic_id: '', grams: o.grams || '', minutes: o.minutes || '',
    qty: o.qty || 1, dev_fee: o.dev_fee || '',
  });
  // Многоцвет: граммы вводятся ПО КАЖДОМУ цвету своим пластиком (у каждого своя цена за кг),
  // время печати — общее на всю модель. Сервер ждёт colors=[{plastic_id, grams}] ВМЕСТО
  // plastic_id+grams и требует минимум два валидных цвета.
  const [mc, setMc] = useState(false);
  const [rows, setRows] = useState([{ plastic_id: '', grams: '' }, { plastic_id: '', grams: '' }]);
  const [link, setLink] = useState('');
  const [shot, setShot] = useState('');
  const [calc, setCalc] = useState(null);
  const [calcErr, setCalcErr] = useState('');   // почему сервер не посчитал — иначе кнопка мертва молча
  const shotRef = useRef();
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  const pl = (plastics.data && plastics.data.plastics) || [];

  // Первый пластик подставляем сам: без него сервер не посчитает, а выбор чаще всего один.
  useEffect(() => {
    if (!pl.length) return;
    if (!f.plastic_id) set('plastic_id', pl[0].id);
    setRows((rs) => rs.map((r) => (r.plastic_id ? r : { ...r, plastic_id: pl[0].id })));
  }, [pl.length]);   // eslint-disable-line

  const setRow = (i, p) => setRows((rs) => rs.map((r, k) => (k === i ? { ...r, ...p } : r)));
  const addRow = () => setRows((rs) => rs.concat([{ plastic_id: (pl[0] && pl[0].id) || '', grams: '' }]));
  // Меньше двух цветов сервер не примет — последние две строки удалять нечего.
  const delRow = (i) => setRows((rs) => (rs.length > 2 ? rs.filter((_, k) => k !== i) : rs));

  // colors для запроса — только если ВСЕ строки заполнены: полупустую раскладку сервер
  // отклонит целиком, и мастер увидит отказ вместо предпросчёта.
  const colorsBody = () => {
    const v = rows.filter((r) => r.plastic_id && Number(r.grams) > 0);
    return (v.length >= 2 && v.length === rows.length)
      ? v.map((r) => ({ plastic_id: Number(r.plastic_id), grams: r.grams }))
      : null;
  };

  const body = () => {
    const common = { qty: Number(f.qty) || 1, minutes: f.minutes, dev_fee: f.dev_fee || 0 };
    return mc
      ? { ...common, colors: colorsBody() }
      : { ...common, plastic_id: Number(f.plastic_id), grams: f.grams };
  };

  // Живой предпросчёт суммы: dry=true считает, но ничего не сохраняет и клиенту не шлёт.
  const rowsKey = rows.map((r) => r.plastic_id + ':' + r.grams).join('|');
  useEffect(() => {
    const ready = mc ? !!colorsBody() : (Number(f.grams) > 0 && f.plastic_id);
    if (!(ready && Number(f.minutes) > 0)) { setCalc(null); return; }
    let stale = false;
    call('adminOrderInvoice', o.id, body(), true)
      .then((r) => { if (!stale) { setCalc(r); setCalcErr(''); } })
      // Причину отказа показываем рядом со сметой. Проглотить её нельзя: кнопка «Выставить
      // счёт» завязана на предпросчёт, и мастер получал бы мёртвую кнопку без объяснений —
      // а в многоцвете сервер отвечает содержательно («минимум два цвета», цена пластика…).
      .catch((e) => { if (!stale) { setCalc(null); setCalcErr(e.message); } });
    return () => { stale = true; };
  }, [f.plastic_id, f.grams, f.minutes, f.qty, f.dev_fee, mc, rowsKey]);   // eslint-disable-line

  const pickShot = (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (file.size > 12 * 1024 * 1024) { toast('Скрин больше 12 МБ — выберите поменьше', { kind: 'warn2' }); return; }
    const rd = new FileReader();
    rd.onload = () => { setShot(rd.result); toast('Скрин прикреплён', { kind: 'ok' }); };
    rd.readAsDataURL(file);
  };

  return <Sec t={<span><DIcon n="doc" size={13} /> Выставить счёт (индивидуальный)</span>} span2>
    {o.price_pending ? <div className="warn" style={{ marginBottom: 14 }}>
      <DIcon n="clock" size={15} />
      Ждёт расчёта ПК — цену посчитает очередь досчёта и пришлёт клиенту на подтверждение.
    </div> : o.unsliced ? <div className="warn" style={{ marginBottom: 14 }}>
      <DIcon n="bell" size={15} />
      Автослайс не удался — оцените вручную или дайте ссылку на модель, бот скачает и посчитает сам.
    </div> : null}
    <div className="stchips" style={{ marginBottom: 14 }}>
      <button className={'chip' + (mc ? '' : ' on')} onClick={() => setMc(false)}>Один цвет</button>
      <button className={'chip' + (mc ? ' on' : '')} onClick={() => setMc(true)}>
        <DIcon n="palette" size={14} />Многоцвет
      </button>
    </div>
    <div style={{
      display: 'grid', gap: 12,
      // в многоцвете граммы и ссылка на слайс уходят: граммы живут в строках цветов
      gridTemplateColumns: mc ? '1fr 1fr 1fr' : '2fr 1fr 1fr 1fr 1fr',
    }}>
      {!mc ? <Field l="Ссылка на модель">
        <input className="inp" value={link} onChange={(e) => setLink(e.target.value)}
          placeholder="makerworld.com/…" />
      </Field> : null}
      {!mc ? <Field l="Граммы">
        <input className="inp mono" value={f.grams} onChange={(e) => set('grams', e.target.value)} />
      </Field> : null}
      <Field l="Время, мин">
        <input className="inp mono" value={f.minutes} onChange={(e) => set('minutes', e.target.value)} />
      </Field>
      <Field l="Кол-во">
        <input className="inp mono" value={f.qty} onChange={(e) => set('qty', e.target.value)} />
      </Field>
      <Field l={`Разработка, ${curSym()}`}>
        <input className="inp mono" value={f.dev_fee} onChange={(e) => set('dev_fee', e.target.value)}
          placeholder="0" />
      </Field>
    </div>
    <div style={{ marginTop: 12 }}>
      {!mc ? <Field l="Пластик">
        <select className="sel" value={f.plastic_id} onChange={(e) => set('plastic_id', e.target.value)}>
          <option value="">— выберите —</option>
          {pl.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field> : <>
        {rows.map((r, i) => <div key={i} className="frow" style={{ marginTop: i ? 10 : 0 }}>
          <Field l={'Цвет ' + (i + 1)}>
            <select className="sel" value={r.plastic_id}
              onChange={(e) => setRow(i, { plastic_id: e.target.value })}>
              <option value="">— выберите —</option>
              {pl.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </Field>
          <Field l="Граммы">
            <input className="inp mono" value={r.grams}
              onChange={(e) => setRow(i, { grams: e.target.value })} />
          </Field>
          {rows.length > 2 ? <button className="ib" title="Убрать цвет"
            style={{ alignSelf: 'flex-end' }} onClick={() => delRow(i)}>
            <DIcon n="close" size={15} />
          </button> : null}
        </div>)}
        <button className="db line sm" style={{ marginTop: 12 }} onClick={addRow}>
          <DIcon n="plus" size={14} />Добавить цвет
        </button>
        <div className="hint" style={{ marginTop: 8 }}>
          Граммы по каждому цвету — из слайсера (продувка уже в них), время — общее на всю печать.
        </div>
      </>}
    </div>
    <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 14, flexWrap: 'wrap' }}>
      {!mc ? <ABtn kind="line sm" icon="calc" disabled={!link.trim()} onClick={async () => {
        try {
          const r = await call('adminOrderSlice', o.id, link.trim(),
            { plastic_id: Number(f.plastic_id), qty: Number(f.qty) || 1 });
          setF((s) => ({ ...s, grams: r.grams, minutes: r.minutes }));
          toast(`Посчитал: ${grams(r.grams)} · ${minutes(r.minutes)}${r.dims ? ' · ' + r.dims : ''}`, { kind: 'ok' });
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Посчитать по ссылке</ABtn> : null}
      <button className="db line sm" onClick={() => shotRef.current.click()}>
        <DIcon n="upload" size={14} />{shot ? 'Скрин заменить' : 'Скрин из слайсера'}
      </button>
      <input type="file" accept="image/*" ref={shotRef} style={{ display: 'none' }} onChange={pickShot} />
      <span style={{ marginLeft: 'auto', fontSize: 13.5, color: 'var(--muted)' }}>
        {calcErr ? <b style={{ color: 'var(--amber)', fontWeight: 600 }}>{calcErr}</b> : null}
        {calcErr ? ' · ' : ''}
        предпросчёт: <b className="mono" style={{ color: 'var(--price)' }}>
          {calc && calc.price != null ? money(calc.price) : '—'}</b>
        {calc && Number(calc.dev_fee) > 0 ? <i style={{ fontStyle: 'normal', color: 'var(--faint)' }}>
          {' '}· печать {money(calc.print_price)} + разработка {money(calc.dev_fee)}
        </i> : null}
      </span>
      <ABtn disabled={!calc} onClick={async () => {
        try {
          const b = body();
          // предпросчёт мог остаться от прошлой раскладки — перепроверяем перед отправкой,
          // иначе клиент получит счёт, посчитанный не по тем цветам
          if (mc && !b.colors) {
            toast('Заполните пластик и граммы в каждом цвете (минимум два)', { kind: 'warn2' });
            return;
          }
          if (shot) b.shot = shot;
          await call('adminOrderInvoice', o.id, b, false);
          toast('Счёт выставлен — клиент получил расчёт в чат', { kind: 'ok', ms: 7000 });
          setShot(''); after();
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Выставить счёт клиенту</ABtn>
    </div>
  </Sec>;
}

// Статусы, после которых заказ закрыт: срок готовности в них уже не правят.
const FINAL_ST = ['printed', 'done', 'handed', 'rejected', 'cancelled'];

function StatusSec({ o, after, toast, canMoney = true }) {
  const [due, setDue] = useState(o.due || '');
  const [actual, setActual] = useState('');
  const [reason, setReason] = useState(o.reject_reason || '');
  // Причину показываем и до отказа: в макете поле привязано к статусу, но вписать её нужно
  // ДО перевода — иначе клиент получит уведомление без объяснения.
  const [askReason, setAskReason] = useState(o.status === 'rejected');
  const [bill, setBill] = useState('');   // цена для «Выставить счёт» (пусто = текущая)
  const confirm = useConfirm();
  // Набор переходов зависит от того, ЧТО заказали (как в мобильной админке, screens_admin.js).
  // Готовое изделие с витрины уже напечатано: печатных статусов у него нет, только «Новый»
  // и «Вручено». Обычному (печатному) заказу, наоборот, «Вручено» сервер не поставит —
  // до этой правки кнопка висела, мастер жал, а сервер молча отвечал отказом.
  const FLOW = o.product_id
    ? ['new', 'handed']
    : ['new', 'awaiting_payment', 'in_progress', 'printed'];

  const set = async (st) => {
    if (st === o.status) return;
    const prev = o.status;
    try {
      await call('adminOrderStatus', o.id, st, due, actual, reason);
    } catch (e) {
      // Отказ сервера (недоступный статус, права роли, пропавший заказ) обязан быть виден:
      // молчащий интерфейс выглядит как «нажал и ничего не произошло».
      toast(e.message, { kind: 'warn2', ms: 8000 });
      return;
    }
    // Смена статуса шлёт клиенту уведомление — ошибиться легко, поэтому даём откатить.
    toast(`Статус: ${(ST[st] || {}).l || st}`, {
      kind: 'ok', ms: 9000, action: 'Отменить',
      onAction: async () => {
        try {
          await call('adminOrderStatus', o.id, prev, due, '', '');
          toast(`Вернул статус «${(ST[prev] || {}).l || prev}»`, { kind: 'ok' });
        } catch (e) { toast(e.message, { kind: 'warn2', ms: 8000 }); }
        after();
      },
    });
    after();
  };

  // «Отложить» стоит рядом с «Отказом», но смысл противоположный: заказ остаётся жив,
  // просто выходит из очереди. Поэтому кнопка не красная и подтверждение не «опасное».
  const defer = () => confirm(
    `Отложить заказ #${o.id}?`,
    'Заказ уйдёт из очереди и перестанет резервировать филамент, но останется у клиента. '
    + 'Клиент получит уведомление, что к заказу вернутся позже'
    + (due.trim() ? ` — ориентировочно ${due.trim()}.` : '.'),
    () => set('deferred'), 'Отложить', false);

  const reject = () => {
    if (!reason.trim()) {
      setAskReason(true);
      toast('Укажите причину отказа — она уйдёт клиенту', { kind: 'warn2' });
      return;
    }
    confirm(`Отказ по заказу #${o.id}?`,
      'Заказ будет помечен как отказ, клиент получит уведомление с причиной.',
      () => set('rejected'), 'Отказ');
  };

  const showDue = !FINAL_ST.includes(o.status);
  const showActual = ['in_progress', 'printing', 'printed'].includes(o.status);
  // Выставить счёт (этап «Ждёт оплаты», фидбэк 31.07): одобрение обычного заказа + опциональное
  // уточнение цены + инвойс клиенту — одним действием и ОДНИМ сообщением («было → стало»
  // сервер впишет в описание счёта сам).
  // Счёт — деньги: оператору (A4) ни кнопки, ни отметки об оплате.
  const canBill = canMoney && !o.is_custom && !o.paid && (o.status === 'new' || o.status === 'awaiting_payment');
  const sendBill = async () => {
    const p = String(bill || '').replace(',', '.').trim();
    try {
      await call('adminOrderBill', o.id, p ? Number(p) : null);
      toast('Счёт отправлен клиенту — заказ ждёт оплаты', { kind: 'ok' });
      setBill('');
      after();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  return <Sec t={<span><DIcon n="check" size={13} /> {canMoney ? 'Статус и оплата' : 'Статус'}</span>}>
    <div className="stchips">
      {FLOW.map((k) => <button key={k} className={'chip' + (o.status === k ? ' on' : '')}
        onClick={() => set(k)}>{ST[k].l}</button>)}
      <span style={{ flex: 1 }} />
      <button className="chip" onClick={() => (o.status === 'deferred' ? set('new') : defer())}
        title={o.status === 'deferred' ? 'Вернуть заказ в очередь' : 'Отложить на будущее'}
        style={o.status === 'deferred'
          ? { background: 'var(--blue-soft)', borderColor: 'rgba(77,163,255,.4)', color: 'var(--blue)' }
          : undefined}>
        {o.status === 'deferred' ? 'Вернуть в очередь' : 'Отложить'}
      </button>
      {/* «Отказ» — печатный переход: заказу готового изделия сервер его не даст, кнопку прячем */}
      {o.product_id ? null : <button className="chip" onClick={reject} style={o.status === 'rejected'
        ? { background: 'var(--red-soft)', borderColor: 'rgba(255,91,77,.4)', color: 'var(--red)' }
        : { color: 'var(--red)' }}>Отказ</button>}
    </div>

    {showDue || showActual ? <div className="frow" style={{ marginTop: 14 }}>
      {/* у отложенного заказа то же поле значит «когда ориентировочно вернёмся» */}
      {showDue ? <Field l={o.status === 'deferred' ? 'Вернёмся ориентировочно' : 'Срок готовности'}>
        <input className="inp" value={due} onChange={(e) => setDue(e.target.value)} placeholder="ДД.ММ" />
      </Field> : null}
      {showActual ? <Field l="Факт. печать, мин">
        <input className="inp mono" value={actual} onChange={(e) => setActual(e.target.value)}
          placeholder="запишется в статистику" />
      </Field> : null}
    </div> : null}

    {askReason ? <div style={{ marginTop: 14 }}>
      <Field l="Причина отказа">
        <input className="inp" value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
    </div> : null}

    {canBill ? <div className="frow" style={{ marginTop: 14, alignItems: 'flex-end' }}>
      <Field l="Счёт на оплату (пусто — по текущей цене)">
        <input className="inp mono" value={bill} onChange={(e) => setBill(e.target.value)}
          placeholder={String(o.price || '')} />
      </Field>
      <button className="db" style={{ flex: '0 0 auto' }} onClick={sendBill}>
        <DIcon n="coin" size={14} /> {o.status === 'awaiting_payment' ? 'Перевыставить счёт' : 'Выставить счёт'}
      </button>
    </div> : null}
    {/* Пустое поле = счёт по ТЕКУЩЕЙ цене, и доставка пересчитается всё равно — поэтому
        предупреждение смотрит на bill || o.price, а не только на введённое. */}
    {canBill ? <DeliveryWarn o={o} price={bill || o.price} reissue /> : null}

    {!canMoney ? null : o.pay_method === 'online'
      // Оплачен через Telegram Payments (A1): деньги реально получены — галочкой это не
      // снимается (сервер откажет), показываем бейдж и сумму из леджера платежей.
      ? <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="stb green">Оплачен онлайн</span>
          {(o.payments || []).length > 0
            ? <span className="hint">{(o.payments || []).map((p) => `${p.amount} ${p.currency}`).join(' + ')} через Telegram Payments · возврат — в кабинете провайдера</span>
            : null}
        </div>
      : <label className="chk" style={{ marginTop: 16 }}>
          <input type="checkbox" checked={!!o.paid} style={{ width: 17, height: 17, accentColor: 'var(--accent)' }}
            onChange={async (e) => {
              await call('adminOrderPaid', o.id, e.target.checked);
              toast(e.target.checked ? 'Отмечен оплаченным' : 'Отметка оплаты снята', { kind: 'ok' });
              after();
            }} />
          Оплачен <span className="hint">· вместе с «напечатано» включает реферальный бонус</span>
        </label>}
  </Sec>;
}

/**
 * Предупреждение о пересчёте доставки по порогу «бесплатно от суммы» (волна 5, 09.08.2026).
 *
 * Стоимость доставки зависит от суммы заказа, а цену мастер назначает уже ПОСЛЕ оформления.
 * Значит, сохранение цены меняет сумму заказа целиком — в том числе сумму УЖЕ ВЫСТАВЛЕННОГО
 * счёта. Telegram сверяет её на pre_checkout и такой счёт отклоняет: клиент увидит отказ
 * платежа, а не «стало дешевле». Мастер обязан узнать это ДО нажатия кнопки.
 *
 * Ничего не считаем сами: сервер прислал в o.group.delivery_recalc готовый порог free_price
 * (цена ЭТОЙ позиции, с которой доставка станет бесплатной) — здесь только сравнение.
 * dr отсутствует, когда пересчёта не будет вовсе (нет порога, доставка оплачена или её
 * стоимость мастер назначил руками).
 *
 * price — цена, которую мастер СЕЙЧАС ввёл ('' → блок молчит: менять нечего).
 * reissue — действие само отправит клиенту новый счёт («Выставить счёт»), поэтому
 *           звать выставлять его заново не нужно.
 */
function DeliveryWarn({ o, price, style, reissue }) {
  const dr = (o.group || {}).delivery_recalc;
  const p = Number(String(price == null ? '' : price).replace(',', '.'));
  if (!dr || !(p > 0)) return null;
  const next = dr.free_price != null && p >= dr.free_price ? 0 : dr.tariff;
  if (Math.abs(next - dr.cost) < 0.005) return null;      // порог не пересекается — молчим
  const ids = dr.invoice_ids || [];
  return <div className="warn" style={style || { marginTop: 12 }}>
    <DIcon n="bell" size={15} />
    <span>
      Доставка пересчитается по порогу «бесплатно от <b className="mono">{money(dr.free_from)}</b>»:{' '}
      <b className="mono">{money(dr.cost)}</b> → <b className="mono">{money(next)}</b>
      {next > dr.cost ? ' — с этой ценой заказ до порога не дотягивает, доставка снова станет платной.' : '.'}
      {ids.length ? <> {' '}Сумма заказа изменится, поэтому счёт, который уже висит у клиента
        (позици{ids.length > 1 ? 'и' : 'я'} <b className="mono">#{ids.join(', #')}</b>), перестанет
        действовать: Telegram отклонит оплату по нему.
        {reissue ? ' Новый счёт уйдёт клиенту сразу — оплатит по нему.'
          : ' Выставьте счёт заново после сохранения цены.'}</> : null}
    </span>
  </div>;
}

function PriceSec({ o, after, toast }) {
  const [mode, setMode] = useState('direct');
  const [price, setPrice] = useState(o.price || '');
  const [g, setG] = useState(o.grams || '');
  const [m, setM] = useState(o.minutes || '');
  const [comment, setComment] = useState('');
  // Отклонение больше 20% почти всегда означает опечатку — предупреждаем ДО отправки.
  const dev = price && o.price ? Math.abs(Number(price) - o.price) / o.price : 0;
  const ready = mode === 'direct' ? Number(price) > 0 : (Number(g) > 0 && Number(m) > 0);

  return <Sec t={<span><DIcon n="coin" size={13} /> Изменить цену</span>}
    right={o.price ? <span className="mono" style={{ color: 'var(--faint)' }}>сейчас {money(o.price)}</span> : null}>
    {o.pending_price != null ? <div className="warn" style={{ marginBottom: 14 }}>
      <DIcon n="bell" size={15} />
      <span>Клиенту отправлена цена <b className="mono">{money(o.pending_price)}</b> — ждём подтверждения.</span>
    </div> : null}
    <div className="stchips" style={{ marginBottom: 14 }}>
      <button className={'chip' + (mode === 'direct' ? ' on' : '')}
        onClick={() => setMode('direct')}>Вписать цену</button>
      <button className={'chip' + (mode === 'calc' ? ' on' : '')}
        onClick={() => setMode('calc')}>По граммам и времени</button>
    </div>
    {mode === 'direct' ? <div className="frow">
      <Field l={`Новая цена, ${curSym()}`}>
        <input className="inp mono" type="number" step="0.01" value={price}
          onChange={(e) => setPrice(e.target.value)} placeholder={String(o.price || '')} />
      </Field>
      <Field l="Комментарий клиенту">
        <input className="inp" value={comment} onChange={(e) => setComment(e.target.value)}
          placeholder="почему изменилась" />
      </Field>
    </div> : <div className="frow">
      <Field l="Граммы"><input className="inp mono" value={g} onChange={(e) => setG(e.target.value)} /></Field>
      <Field l="Минуты"><input className="inp mono" value={m} onChange={(e) => setM(e.target.value)} /></Field>
      <Field l="Комментарий клиенту">
        <input className="inp" value={comment} onChange={(e) => setComment(e.target.value)} />
      </Field>
    </div>}
    {dev > 0.2 && mode === 'direct' ? <div className="warn" style={{ marginTop: 12 }}>
      <DIcon n="bell" size={15} />
      <span>Новая цена отличается от текущей на {Math.round(dev * 100)}% — клиент получит запрос на подтверждение.</span>
    </div> : null}
    {mode === 'direct' ? <DeliveryWarn o={o} price={price} /> : null}
    <ABtn kind="soft full" style={{ marginTop: 14 }} disabled={!ready} onClick={async () => {
      if (mode === 'direct') await call('adminOrderPrice', o.id, Number(price), comment);
      else await call('adminOrderPriceByGt', o.id, g, m, comment);
      toast('Цена отправлена клиенту', { kind: 'ok' });
      setComment(''); after();
    }}>Сохранить цену</ABtn>
  </Sec>;
}

function RecalcSec({ o, after, toast }) {
  const [res, setRes] = useState(null);
  const [g, setG] = useState('');
  const [m, setM] = useState('');
  const [dry, setDry] = useState(null);   // цена из dry-пересчёта, пока правим поля

  // Правка граммов/минут меняет цену нелинейно (надбавка за объём, стол, округление живут на
  // сервере) — поэтому итог спрашиваем у recalc_apply с dry=true, ничего не сохраняя.
  useEffect(() => {
    if (!res) { setDry(null); return; }
    if (!(Number(g) > 0 && Number(m) > 0)) { setDry(null); return; }
    let stale = false;
    const t = setTimeout(() => {
      call('adminOrderRecalcApply', o.id, g, m, true)
        .then((r) => { if (!stale && r.price != null) setDry(r.price); })
        .catch(() => { if (!stale) setDry(null); });   // молча: это лишь предпросмотр
    }, 350);
    return () => { stale = true; clearTimeout(t); };
  }, [g, m, res, o.id]);

  if (!o.can_open_slicer) return null;
  const has = res && res.new;
  return <Sec t={<span><DIcon n="refresh" size={13} /> Пересчёт заказа</span>}>
    {!has ? <ABtn kind="soft full" icon="calc" onClick={async () => {
      try {
        const r = await call('adminOrderRecalc', o.id);
        setRes(r);
        setG(String((r.new || {}).grams == null ? '' : r.new.grams));
        setM(String((r.new || {}).minutes == null ? '' : r.new.minutes));
      } catch (e) { toast(e.message, { kind: 'warn2' }); }
    }}>Пересчитать по слайсеру</ABtn> : <>
      <div className="cmp">
        <div>
          <div style={{ color: 'var(--faint)', fontSize: 11 }}>БЫЛО</div>
          {money(res.old.price)} · {grams(res.old.grams)} · {minutes(res.old.minutes)}
        </div>
        <div className="to">→</div>
        <div>
          <div style={{ color: 'var(--faint)', fontSize: 11 }}>СТАЛО</div>
          {/* показываем цену правки (dry), а не первичного слайса — иначе мастер применяет вслепую */}
          <span style={{ color: 'var(--price)' }}>{money(dry != null ? dry : res.new.price)}</span>
          {' · '}{grams(Number(g) || res.new.grams)} · {minutes(Number(m) || res.new.minutes)}
        </div>
      </div>
      {/* Граммы и время можно поправить руками: слайсер иногда мажет по опорам */}
      <div className="frow" style={{ marginTop: 12 }}>
        <Field l="Граммы (правка)">
          <input className="inp mono" value={g} onChange={(e) => setG(e.target.value)} />
        </Field>
        <Field l="Минуты">
          <input className="inp mono" value={m} onChange={(e) => setM(e.target.value)} />
        </Field>
      </div>
      <DeliveryWarn o={o} price={dry != null ? dry : res.new.price} />
      <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
        <ABtn kind="full" style={{ flex: 1 }} onClick={async () => {
          try {
            const r = await call('adminOrderRecalcApply', o.id, g, m, false);
            // дороже прежней цены сервер не применяет молча — уходит клиенту на подтверждение.
            // Исключение: клиент сам разрешил принимать подорожание (профиль → подтверждение цены).
            toast(r.applied === 'pending'
              ? 'Дороже прежней — отправлено клиенту на подтверждение'
              : (r.applied === 'accepted'
                ? 'Клиент разрешил такое подорожание — цена принята, заказ в работе'
                : 'Цена применена, клиент уведомлён'), { kind: 'ok' });
            setRes(null); setDry(null); after();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }}>Применить</ABtn>
        <button className="db line" onClick={() => setRes(null)}>Отмена</button>
      </div>
    </>}
    <div className="hint" style={{ marginTop: 10 }}>
      Цену считает сервер по текущим ставкам из «Цен»: надбавка за объём, стол и округление
      прячутся внутрь машинного времени, на клиенте цифры не сойдутся.
    </div>
  </Sec>;
}

function FilesSec({ o, toast }) {
  if (!o.can_print_net && !o.can_open_slicer && !o.can_open_folder && !o.unsliced) return null;
  return <Sec t={<span><DIcon n="printer" size={13} /> Печать и файлы</span>}>
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
      {o.can_print_net ? <ABtn kind="soft" icon="printer" onClick={async () => {
        try {
          await call('adminOrderPrintNet', o.id, true);
          toast('Отправлено на принтер', { kind: 'ok' });
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Печатать</ABtn> : null}
      {/* start=false — файл только заливается в принтер, печать мастер стартует сам с экрана
          принтера. Отличие от «Печатать» ровно в этом флаге (print_net: start). */}
      {o.can_print_net ? <ABtn kind="line" icon="upload" onClick={async () => {
        try {
          await call('adminOrderPrintNet', o.id, false);
          toast('Файл загружен на принтер — печать не запущена', { kind: 'ok' });
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Загрузить файл</ABtn> : null}
      {o.can_open_slicer ? <ABtn kind="line" icon="external" onClick={async () => {
        try {
          const r = await call('adminOrderOpenSlicer', o.id);
          toast(r.note || 'Открываю слайсер', { kind: 'ok' });
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Открыть в слайсере</ABtn> : null}
      {o.can_open_folder ? <ABtn kind="line" icon="folder" onClick={async () => {
        try {
          const r = await call('adminOrderOpenFolder', o.id);
          toast(r.note || 'Открываю папку', { kind: 'ok' });
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Открыть папку</ABtn> : null}
    </div>
    {o.price_pending ? <div className="warn" style={{ marginTop: 12 }}>
      <DIcon n="clock" size={15} />Ждёт расчёта ПК — цена придёт клиенту автоматически, оценивать вручную не нужно.
    </div> : o.unsliced ? <div className="warn" style={{ marginTop: 12 }}>
      <DIcon n="bell" size={15} />Автослайс не удался — цену оценивает мастер вручную.
    </div> : null}
  </Sec>;
}

/**
 * Таймлапс заказа. Принтер снимает таймлапс на КАЖДУЮ печать и не знает про заказы — связать
 * печать с заказом может только мастер. Поэтому: список последних печатей принтера
 * (adminPrinterHistory), выбор нужной и attach_print, который скачивает её таймлапс к заказу
 * и по галочке сразу отправляет клиенту в Telegram.
 */
function TimelapseSec({ o, after, toast }) {
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const [hist, setHist] = useState(null);       // null — историю ещё не загружали
  const [histErr, setHistErr] = useState('');   // честная причина вместо пустого списка
  const [sel, setSel] = useState('');
  const [send, setSend] = useState(true);

  // Удаление таймлапса (1faab6cb): у печати журнала — с «follow» копии заказа на сервере,
  // у самого заказа — только его копия. Клиент перестаёт видеть видео сразу.
  const delTl = (kind, id, what) => confirm('Удалить таймлапс?',
    what + ' Файл видео будет удалён, клиент перестанет его видеть.',
    async () => {
      try {
        await call(kind === 'journal' ? 'adminJournalMediaDelete' : 'adminOrderMediaDelete', id, 'timelapse');
        toast('Таймлапс удалён', { kind: 'ok' });
        after();
      } catch (e) { toast(e.message, { kind: 'warn2' }); }
    });

  // Историю тянем по требованию: запрос идёт на живой принтер и занимает секунды.
  const load = async () => {
    setOpen(true);
    // после неудачи пробуем снова: принтер мог быть занят или временно недоступен,
    // иначе первая же ошибка залипала бы навсегда и кнопка показывала старый текст
    if (hist !== null && !histErr) return;
    setHistErr('');
    try {
      const r = await call('adminPrinterHistory');
      const jobs = r.jobs || [];
      setHist(jobs);
      // предвыбираем свежайшую печать С таймлапсом — почти всегда именно она и нужна
      const f = jobs.find((x) => x.has_timelapse);
      if (f) setSel(f.name);
    } catch (e) {
      setHist([]);
      setHistErr(e.message);
    }
  };

  return <Sec t={<span><DIcon n="film" size={13} /> Таймлапс печати</span>}>
    {/* все печати заказа из журнала (многостольный печатается в несколько заходов):
        клиент видит их таймлапсы и скрины стола агрегатом — тут видно, что собралось */}
    {(o.prints || []).length ? <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
      {(o.prints || []).map((p, i) => <div key={p.jid} style={{
        display: 'flex', alignItems: 'center', gap: 10,
        background: 'var(--surface-2)', borderRadius: 11, padding: '8px 12px',
      }}>
        {p.photo ? <img src={p.photo} alt="" style={{ width: 40, height: 40, objectFit: 'cover', borderRadius: 8, flex: '0 0 auto' }} /> : null}
        <span style={{ flex: 1, minWidth: 0, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {(i + 1) + '. ' + (p.label || 'печать') + (p.minutes ? ' · ' + p.minutes + ' мин' : '')}
        </span>
        {p.timelapse ? <a className="db sm line" href={p.timelapse} target="_blank" rel="noreferrer">
          <DIcon n="film" size={13} /> Таймлапс
        </a> : null}
        {p.timelapse ? <button className="ib danger" style={{ width: 30, height: 30, flex: '0 0 auto' }}
          title="Удалить таймлапс печати"
          onClick={() => delTl('journal', p.jid, 'Таймлапс печати «' + (p.label || 'печать') + '».')}>
          <DIcon n="trash" size={13} />
        </button> : null}
      </div>)}
      <div className="hint">Печати этого заказа из журнала — клиент видит все их таймлапсы и скрины стола.</div>
    </div> : null}
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12,
      background: 'var(--surface-2)', borderRadius: 11, padding: '12px 14px',
    }}>
      <DIcon n="film" size={19} style={{ color: 'var(--violet)' }} />
      <div style={{ flex: 1, fontSize: 13.5 }}>
        {o.timelapse ? 'Таймлапс печати готов' : 'Таймлапс не привязан'}
        <div className="hint">{o.timelapse
          ? 'клиент видит его в карточке заказа'
          : 'снимается сам, если печать шла через бота'}</div>
      </div>
      {o.timelapse ? <a className="db sm line" href={o.timelapse} target="_blank" rel="noreferrer">
        Смотреть
      </a> : null}
      {o.timelapse ? <button className="ib danger" style={{ width: 30, height: 30, flex: '0 0 auto' }}
        title="Удалить таймлапс заказа"
        onClick={() => delTl('order', o.id, 'Таймлапс, привязанный к заказу #' + o.id + '.')}>
        <DIcon n="trash" size={13} />
      </button> : null}
    </div>

    {!open ? <ABtn kind="line full" icon="printer" style={{ marginTop: 12 }} onClick={load}>
      {o.timelapse ? 'Заменить — выбрать печать из истории' : 'Привязать печать из истории'}
    </ABtn> : <div style={{ marginTop: 12 }}>
      {hist === null ? <div className="hint">Загружаю историю печатей принтера…</div> : null}
      {hist !== null && !hist.length ? <div className="hint">
        {histErr ? 'Не удалось получить историю: ' + histErr : 'История печатей на принтере пуста.'}
      </div> : null}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {(hist || []).map((jb, i) => <button key={i} type="button"
          className={'chip' + (sel === jb.name ? ' on' : '')}
          disabled={!jb.has_timelapse}
          style={{
            display: 'flex', justifyContent: 'space-between', width: '100%',
            opacity: jb.has_timelapse ? 1 : 0.45,
          }}
          onClick={() => setSel(jb.name)}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
            {jb.name || '(без имени)'}
          </span>
          <span style={{ flex: '0 0 auto', marginLeft: 8, color: 'var(--faint)' }}>
            {jb.has_timelapse ? (jb.ago || '') : 'без таймлапса'}
          </span>
        </button>)}
      </div>
      {hist && hist.length ? <label className="chk" style={{ marginTop: 12 }}>
        <input type="checkbox" checked={send} style={{ width: 17, height: 17, accentColor: 'var(--accent)' }}
          onChange={(e) => setSend(e.target.checked)} />
        Сразу отправить таймлапс клиенту
      </label> : null}
      <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
        <ABtn kind="full" style={{ flex: 1 }} disabled={!sel} onClick={async () => {
          try {
            const j = await call('adminAttachPrint', o.id, sel, send);
            // видео у заказа есть всегда, а вот саму печать (запись журнала со скрином стола)
            // сматчить по имени задания удаётся не всегда — честно предупреждаем (71523f3f)
            if (j.journal_linked === false) {
              toast('Видео привязано, но саму печать в журнале сопоставить не удалось — '
                + 'скрин стола к заказу не добавлен', { kind: 'warn2', ms: 9000 });
            } else {
              toast(send ? 'Таймлапс прикреплён и отправлен клиенту' : 'Таймлапс прикреплён', { kind: 'ok' });
            }
            setOpen(false); after();
            // неудача скачивания — это именно ошибка (красный тост), а не рядовое уведомление
          } catch (e) { toast(e.message, { kind: 'err', ms: 9000 }); }
        }}>Прикрепить</ABtn>
        <button className="db line" onClick={() => setOpen(false)}>Отмена</button>
      </div>
    </div>}
    <div className="hint" style={{ marginTop: 10 }}>
      Выберите, какая из последних печатей принтера относится к этому заказу — её таймлапс
      прикрепится к заказу.
    </div>
  </Sec>;
}

function BalanceSec({ o, after, toast }) {
  const [amt, setAmt] = useState('');
  const [comment, setComment] = useState('');
  // Сумма заказа по умолчанию: списать можно, не вводя её вручную (задача владельца) —
  // берётся посчитанная при заказе цена; поле остаётся для пополнения и правки суммы.
  const orderPrice = Number(o.price) || 0;
  const chargeAmt = amt !== '' ? Math.abs(Number(amt)) : orderPrice;
  return <Sec t={<span><DIcon n="coin" size={13} /> Баланс клиента</span>}
    right={<span className="mono" style={{ fontSize: 16, color: 'var(--price)', fontWeight: 700 }}>
      {money(o.balance)}</span>}>
    <div className="frow">
      <Field l={`Сумма, ${curSym()}`}>
        <input className="inp mono" type="number" step="0.01" value={amt}
          onChange={(e) => setAmt(e.target.value)}
          placeholder={orderPrice ? String(orderPrice) : '500'} />
      </Field>
      <Field l="Комментарий">
        <input className="inp" value={comment} onChange={(e) => setComment(e.target.value)}
          placeholder="за что" />
      </Field>
    </div>
    <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
      <ABtn kind="soft" style={{ flex: 1 }} disabled={!amt} onClick={async () => {
        try {
          await call('adminBalance', o.user_id, Math.abs(Number(amt)), 'Пополнение мастером', comment, 0);
          toast('Баланс пополнен', { kind: 'ok' }); setAmt(''); setComment(''); after();
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Зачислить</ABtn>
      {/* Списать за заказ: без ввода суммы берёт цену заказа и заодно ставит «оплачен» —
          мастеру не надо потом отдельно щёлкать галочку. */}
      <ABtn kind="line" style={{ flex: 1 }} disabled={!(chargeAmt > 0)} onClick={async () => {
        try {
          await call('adminBalance', o.user_id, -Math.abs(chargeAmt), `Оплата заказа #${o.id}`, comment, o.id);
          if (!o.paid) await call('adminOrderPaid', o.id, true);
          toast(`Списано ${money(chargeAmt)} · заказ отмечен оплаченным`, { kind: 'ok' });
          setAmt(''); setComment(''); after();
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Списать за заказ</ABtn>
    </div>
  </Sec>;
}

function PhotoSec({ o, after, lightbox, toast }) {
  const confirm = useConfirm();
  const fileRef = useRef();
  const [busy, setBusy] = useState(false);

  // Удаление из фоток результата (1faab6cb): скрин стола печати или фото самого заказа.
  const delPhoto = (kind, id, what) => confirm('Удалить фото?', what,
    async () => {
      try {
        await call(kind === 'journal' ? 'adminJournalMediaDelete' : 'adminOrderMediaDelete', id, 'photo');
        toast('Фото удалено', { kind: 'ok' });
        after();
      } catch (e) { toast(e.message, { kind: 'warn2' }); }
    });
  // Фото готового изделия можно заодно опубликовать в Коллекции — там оно заменит рендер
  // модели живым снимком. Для индивидуальных заказов карточки в коллекции нет.
  const [toCol, setToCol] = useState(false);

  const upload = (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    // сервер режет всё тяжелее 12 МБ — предупреждаем до чтения файла, а не после отказа
    if (f.size > 12 * 1024 * 1024) { toast('Фото больше 12 МБ — выберите поменьше', { kind: 'warn2' }); return; }
    const rd = new FileReader();
    rd.onload = async () => {
      setBusy(true);
      try {
        const r = await call('adminPhoto', 'order', o.id, rd.result, toCol);
        toast(toCol
          ? (r.collection ? 'Фото сохранено и добавлено в коллекцию' : 'Фото сохранено (карточки модели в коллекции нет)')
          : 'Фото результата сохранено', { kind: 'ok' });
        after();
      } catch (err) {
        toast(err.message, { kind: 'warn2' });
      } finally { setBusy(false); }
    };
    rd.readAsDataURL(f);
  };

  // Скрины стола всех печатей заказа (по одному на стол, из журнала): раньше мастер видел
  // здесь только одно «фото результата», а клиент — всю галерею (кейс «Овечка Тимми»).
  const shots = (o.prints || []).filter((p) => p.photo);
  return <Sec t={<span><DIcon n="camera" size={13} /> Фото результата</span>}>
    {o.done_url ? <div style={{ position: 'relative' }}>
      <img src={o.done_url} alt="" onClick={() => lightbox(o.done_url)}
        style={{ width: '100%', borderRadius: 11, aspectRatio: '16/10', objectFit: 'cover', cursor: 'zoom-in' }} />
      <button className="ib danger" title="Удалить фото результата"
        style={{ position: 'absolute', top: 8, right: 8, width: 30, height: 30 }}
        onClick={() => delPhoto('order', o.id,
          'Фото результата заказа пропадёт и у клиента. Скрины стола печатей останутся.')}>
        <DIcon n="trash" size={13} />
      </button>
    </div> : null}
    {shots.length ? <>
      <label className="f-lab" style={{ margin: '12px 0 6px', display: 'block' }}>
        Скрины стола печатей · {shots.length}
      </label>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 8 }}>
        {shots.map((p) => <div key={p.jid} style={{ position: 'relative' }}>
          <img src={p.photo} alt="" title={p.label}
            onClick={() => lightbox(p.photo)}
            style={{ width: '100%', aspectRatio: '16/10', objectFit: 'cover', borderRadius: 9, cursor: 'zoom-in' }} />
          <button className="ib danger" title="Удалить скрин стола"
            style={{ position: 'absolute', top: 4, right: 4, width: 26, height: 26 }}
            onClick={() => delPhoto('journal', p.jid,
              'Скрин стола печати «' + (p.label || 'печать') + '» пропадёт из фоток результата и у клиента.')}>
            <DIcon n="trash" size={12} />
          </button>
        </div>)}
      </div>
    </> : null}
    {o.slicer_shot ? <img src={o.slicer_shot} alt="" onClick={() => lightbox(o.slicer_shot)}
      style={{ width: '100%', borderRadius: 11, marginTop: 10, cursor: 'zoom-in' }} /> : null}
    <input type="file" accept="image/*" ref={fileRef} style={{ display: 'none' }} onChange={upload} />
    <button className="db line" style={{ marginTop: o.done_url || o.slicer_shot ? 12 : 0 }}
      disabled={busy} onClick={() => fileRef.current.click()}>
      <DIcon n="upload" size={14} />{o.done_url ? 'Заменить фото' : 'Прикрепить фото'}
    </button>
    {!o.is_custom ? <label className="chk" style={{ marginTop: 12 }}>
      <input type="checkbox" checked={toCol} style={{ width: 17, height: 17, accentColor: 'var(--accent)' }}
        onChange={(e) => setToCol(e.target.checked)} />
      Добавить в общую коллекцию <span className="hint">· заменит рендер модели</span>
    </label> : null}
    <div className="hint" style={{ marginTop: 8 }}>
      Фото готовой печати попадёт в карточку заказа у клиента и в PDF-смету.
    </div>
  </Sec>;
}

function ReviewSec({ o }) {
  if (!o.rating && !o.review) return null;
  return <Sec t={<span><DIcon n="star" size={13} /> Отзыв клиента</span>}>
    {o.rating ? <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <div className="stars">
        {Array.from({ length: 10 }, (_, i) =>
          <DIcon key={i} n="star" size={17} style={{ opacity: i < o.rating ? 1 : 0.22 }} />)}
      </div>
      <span className="mono" style={{ fontWeight: 700 }}>{o.rating} / 10</span>
    </div> : null}
    {o.review ? <div style={{ marginTop: 10, fontSize: 14.5, color: 'var(--muted)' }}>«{o.review}»</div> : null}
  </Sec>;
}

function HistorySec({ o }) {
  const hist = o.history && o.history.length ? o.history : null;
  return <Sec t={<span><DIcon n="clock" size={13} /> История изменений</span>}>
    {hist ? hist.map((h, i) => <div key={i} className="aud">
      <span className="at">{dt(h.at).split(' ')[0]}</span>
      <span style={{ flex: 1, minWidth: 0 }}>{h.label || h.status}</span>
      <span className="ab">{dt(h.at).split(' ')[1] || ''}</span>
    </div>) : <div className="hint">Изменений пока не было.</div>}
    <div className="hint" style={{ marginTop: 8 }}>
      аудит-трейл: смена статуса пишется сама
    </div>
  </Sec>;
}

// ---------------- чат заказа ----------------

function OrderChat({ o, after }) {
  const { toast, lightbox } = useContext(AppCtx);
  const [text, setText] = useState('');
  const [msgs, setMsgs] = useState(o.messages || []);
  const [busy, setBusy] = useState(false);
  // Прикреплённая, но ещё не отправленная картинка (файлом, вставкой или перетаскиванием).
  const [pic, setPic] = useState(null);
  const fileRef = useRef();
  const scRef = useAutoScroll(msgs.length);

  useEffect(() => { setMsgs(o.messages || []); }, [o.id, o.messages]);

  const send = async () => {
    const t = text.trim();
    if (!t && !pic) return;
    setBusy(true);
    try {
      const r = await call('adminOrderMessage', o.id, t, pic);
      setMsgs(r.messages || msgs);
      setText('');
      setPic(null);
      if (after) after();
    } catch (e) {
      toast(e.message, { kind: 'warn2' });
    } finally { setBusy(false); }
  };

  // Картинка сначала становится вложением, а не улетает клиенту сама: так к скриншоту
  // можно дописать «вот тут подпилим» — и так промах буфером не превращается в отправку.
  const take = (file) => readImage(file).then(setPic).catch((e) => toast(e.message, { kind: 'warn2' }));

  const attach = (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (f) take(f);
  };

  const paste = (e) => {
    const f = pastedImage(e);
    if (!f) return;          // обычный текст вставляется как обычно
    e.preventDefault();
    take(f);
  };

  return <div className="od-chat"
    onDragOver={(e) => e.preventDefault()}
    onDrop={(e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      e.preventDefault();
      take(f);
    }}>
    <div className="chat-h">
      <div className="avatar">{String(o.user || '?').trim().charAt(0).toUpperCase()}</div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="nm">{o.user || 'Клиент'}</div>
        <div className="st">чат заказа #{o.id} · уходит клиенту в Telegram</div>
      </div>
    </div>
    <div className="chat-msgs" ref={scRef}>
      {msgs.length
        ? msgs.map((m, i) => <Bubble key={m.id || i} m={m} onImage={lightbox} />)
        : <Empty ic="chat" t="Сообщений пока нет" ds="Напишите клиенту первым" />}
    </div>
    <Attached src={pic} onDrop={() => setPic(null)} />
    <div className="chat-in">
      <input className="inp" value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
        onPaste={paste}
        placeholder="Ответить клиенту… (Enter — отправить, Ctrl+V — картинка)" disabled={busy} />
      <input type="file" accept="image/*" ref={fileRef} style={{ display: 'none' }} onChange={attach} />
      <button className="ib" title="Прикрепить фото" onClick={() => fileRef.current.click()} disabled={busy}>
        <DIcon n="camera" size={18} />
      </button>
      <button className="db" onClick={() => send()} disabled={busy}>
        <DIcon n="send" size={15} />
      </button>
    </div>
  </div>;
}

// ---------------- «Заказ за клиента» ----------------

function CreateOrderDrawer({ onClose, onDone }) {
  const { toast } = useContext(AppCtx);
  const clients = useApi('adminClients', [{}], []);
  const plastics = useApi('adminPlastics', [], []);
  const [f, setF] = useState({ user_id: '', plastic_id: '', qty: 1, grams: '', minutes: '', label: '', comment: '' });
  const [prev, setPrev] = useState(null);
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  const body = () => ({
    user_id: Number(f.user_id), plastic_id: Number(f.plastic_id), qty: Number(f.qty) || 1,
    grams: Number(f.grams) || 0, minutes: Number(f.minutes) || 0,
    label: f.label, comment: f.comment,
  });

  return <Drawer title="Оформить заказ за клиента" sub="для тех, кто пишет в личку мимо бота" onClose={onClose}
    footer={<>
      <ABtn kind="full" style={{ flex: 1 }} disabled={!f.user_id || !f.plastic_id} onClick={async () => {
        try {
          const r = await call('adminOrderCreate', body(), false);
          toast(`Заказ #${r.order_id} создан`, { kind: 'ok' });
          onClose(); onDone();
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Создать и отправить клиенту</ABtn>
      {/* «Посчитать» — вместо живого предпросчёта макета: считает тот же dry-запрос, но по кнопке */}
      <ABtn kind="soft" onClick={async () => {
        try { setPrev(await call('adminOrderCreate', body(), true)); }
        catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Посчитать</ABtn>
      <button className="db line" onClick={onClose}>Отмена</button>
    </>}>
    <div className="fgap">
      <Field l="Клиент">
        <select className="sel" value={f.user_id} onChange={(e) => set('user_id', e.target.value)}>
          <option value="">— выберите —</option>
          {((clients.data && clients.data.clients) || []).map((c) =>
            <option key={c.uid} value={c.uid}>{c.name}</option>)}
        </select>
      </Field>
      <Field l="Пластик">
        <select className="sel" value={f.plastic_id} onChange={(e) => set('plastic_id', e.target.value)}>
          <option value="">— выберите —</option>
          {((plastics.data && plastics.data.plastics) || []).map((p) =>
            <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <div className="frow">
        <Field l="Граммы">
          <input className="inp mono" type="number" value={f.grams} onChange={(e) => set('grams', e.target.value)} />
        </Field>
        <Field l="Минуты">
          <input className="inp mono" type="number" value={f.minutes} onChange={(e) => set('minutes', e.target.value)} />
        </Field>
        <Field l="Количество">
          <input className="inp mono" type="number" min="1" value={f.qty} onChange={(e) => set('qty', e.target.value)} />
        </Field>
      </div>
      <Field l="Название">
        <input className="inp" value={f.label} onChange={(e) => set('label', e.target.value)}
          placeholder="как заказ увидит клиент" />
      </Field>
      <Field l="Комментарий клиенту">
        <input className="inp" value={f.comment} onChange={(e) => set('comment', e.target.value)}
          placeholder="придёт вместе с заказом" />
      </Field>
      {prev ? <div className="okay">
        Предпросчёт: <b className="mono">{money(prev.calc_price || prev.price)}</b>
      </div> : null}
    </div>
  </Drawer>;
}

// ---------------- «Пакет ссылок» ----------------

/**
 * Опрос статуса пакета вынесен в отдельный компонент СПЕЦИАЛЬНО: usePoll нельзя остановить
 * флагом — цикл живёт, пока компонент смонтирован. Значит единственный честный способ
 * прекратить долбить бота, когда пакет досчитан или отменён, — размонтировать опрашивающего.
 * Родитель просто перестаёт его рисовать.
 */
function BatchPoll({ bid, onData, onErr }) {
  const st = usePoll('adminBatchStatus', 2500, [bid]);
  useEffect(() => { if (st.data) onData(st.data); }, [st.data, onData]);
  // «Пакет не найден (возможно, бот перезапускался)» прилетает как ok:false ⇒ call бросает.
  // Молча крутить опрос дальше нельзя: мастер будет ждать список, которого уже не существует.
  useEffect(() => { if (st.err) onErr(st.err); }, [st.err, onErr]);
  return null;
}

/** Пакет пропал с сервера (перезапуск бота) — это конец, продолжать опрос бессмысленно.
 *  Любая другая ошибка (моргнула связь, бот поднимается ~90 с) временная: пакет жив и
 *  считается дальше, опрос обязан пережить её сам. */
const batchGone = (msg) => /не найден/i.test(String(msg || ''));

/**
 * Пакетный расчёт ссылок: клиент прислал в личку сообщение с кучей ссылок на Printables /
 * MakerWorld — мастер вставляет текст целиком, выбирает клиента и пластик, бот скачивает и
 * слайсит каждую модель. Дальше мастер снимает галочки с ненужных позиций и отправляет
 * клиенту расчёт (модели заодно ложатся клиенту в «Мои модели»).
 *
 * Расчёт идёт минутами, поэтому пакет живёт на СЕРВЕРЕ, а не в этом компоненте: при входе
 * спрашиваем batch_last и подхватываем незавершённый пакет — мастер успевает уйти на другой
 * экран и вернуться.
 */
function BatchCalcDrawer({ onClose }) {
  const { toast, lightbox } = useContext(AppCtx);
  const clients = useApi('adminClients', [{}], []);
  const plastics = useApi('adminPlastics', [], []);
  const [userId, setUserId] = useState('');
  const [plasticId, setPlasticId] = useState('');
  const [links, setLinks] = useState('');
  const [bid, setBid] = useState('');
  const [data, setData] = useState(null);        // тело batch_status
  const [excl, setExcl] = useState(() => new Set());   // idx, которые НЕ шлём клиенту
  const [pollErr, setPollErr] = useState('');
  const confirm = useConfirm();

  const pl = (plastics.data && plastics.data.plastics) || [];
  const cl = (clients.data && clients.data.clients) || [];

  // Пластик по умолчанию — помеченный is_default, иначе первый: без пластика сервер не считает.
  useEffect(() => {
    if (plasticId || !pl.length) return;
    const d = pl.filter((p) => p.is_default)[0] || pl[0];
    if (d) setPlasticId(String(d.id));
  }, [pl.length]);   // eslint-disable-line

  // Восстановление незавершённого пакета. Ошибку здесь не показываем тостом: отсутствие
  // пакета — обычное дело, а не сбой (сервер отвечает ok:true, bid:null).
  useEffect(() => {
    let stale = false;
    (async () => {
      try {
        const r = await call('adminBatchLast');
        // Панель пересоздаётся при каждом возврате на экран (drawer живёт во вкладке), поэтому
        // тостим только когда пакет действительно новый — иначе сообщение повторялось бы.
        if (stale || !r.bid || r.bid === bid) return;
        setBid(r.bid);
        setData(r);
        if (r.user_id) setUserId(String(r.user_id));
        if (r.plastic_id) setPlasticId(String(r.plastic_id));
        if (r.links_raw) setLinks(r.links_raw);
        toast('Восстановил незавершённый пакет', { kind: 'ok' });
      } catch (e) { /* нет связи — форма просто откроется пустой */ }
    })();
    return () => { stale = true; };
  }, []);   // eslint-disable-line

  const items = (data && data.items) || [];
  const finished = !!(data && (data.done || data.cancelled));
  // Опрос гасим ТОЛЬКО когда пакет досчитан, отменён или потерян сервером. На сетевом сбое
  // он продолжается: иначе мастер увидел бы «запустите заново», не смог отменить живой пакет
  // и запустил второй — два тяжёлых слайса в том же event loop, где крутится телеграм.
  const running = !!bid && !batchGone(pollErr) && !finished;
  // Отмена доступна, пока пакет вообще существует, — даже если связь моргает.
  const cancellable = !!bid && !batchGone(pollErr) && !finished;
  const okItems = items.filter((it) => it.status === 'ok' && !excl.has(it.idx));

  // Успешный ответ снимает предупреждение о связи: иначе «жду связи» висело бы вечно.
  const onPoll = useCallback((d) => { setData(d); setPollErr(''); }, []);

  const togItem = (idx) => setExcl((s) => {
    const n = new Set(s);
    if (n.has(idx)) n.delete(idx); else n.add(idx);
    return n;
  });

  const start = async () => {
    try {
      const name = (cl.filter((c) => String(c.uid) === String(userId))[0] || {}).name || '';
      const r = await call('adminBatchCalc', Number(userId), links, name, Number(plasticId) || 0);
      setBid(r.bid); setData(null); setExcl(new Set()); setPollErr('');
      toast(`Считаю ${num(r.count)} ссылок — это займёт время`, { kind: 'ok', ms: 7000 });
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  const send = async () => {
    try {
      const inc = okItems.map((it) => it.idx);
      const r = await call('adminBatchSend', bid, inc);
      toast(`Отправлено клиенту: ${num(r.sent || 0)} поз.`, { kind: 'ok', ms: 7000 });
      setBid(''); setData(null); setExcl(new Set()); setLinks('');
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  const cancel = () => confirm('Отменить пакетный расчёт?',
    'Оставшиеся ссылки считаться не будут, уже посчитанные позиции потеряются. Текущая ссылка досчитается — слайсер не прерываем.',
    async () => {
      try {
        await call('adminBatchCancel', bid);
        toast('Отменяю — текущая ссылка дорабатывает', { kind: 'ok' });
        setBid(''); setData(null); setExcl(new Set()); setPollErr('');
      } catch (e) { toast(e.message, { kind: 'warn2' }); }
    }, 'Отменить пакет');

  return <Drawer title="Пакетный расчёт ссылок" sub="клиент прислал кучу ссылок одним сообщением"
    onClose={onClose}
    footer={<>
      <ABtn kind="full" style={{ flex: 1 }} icon="coin"
        disabled={running || !Number(userId) || !/https?:\/\//.test(links)}
        onClick={start}>Посчитать все ссылки</ABtn>
      {cancellable ? <button className="db line" onClick={cancel}>
        <DIcon n="close" size={15} />Отменить пакет
      </button> : null}
      <button className="db line" onClick={onClose}>Закрыть</button>
    </>}>
    {/* Опрос жив ровно пока пакет считается — размонтирование глушит цикл */}
    {running ? <BatchPoll key={bid} bid={bid} onData={onPoll} onErr={setPollErr} /> : null}
    <div className="fgap">
      <div className="hint">
        Вставьте сообщение клиента целиком — ссылки найдутся сами. Каждая модель считается
        параметрами по умолчанию, 1 шт. Потом снимите галочки с лишнего и отправьте расчёт.
      </div>
      <Field l="Клиент">
        <select className="sel" value={userId} onChange={(e) => setUserId(e.target.value)}>
          <option value="">— выберите —</option>
          {cl.map((c) => <option key={c.uid} value={c.uid}>{c.name}</option>)}
        </select>
      </Field>
      <Field l="Пластик">
        <select className="sel" value={plasticId} onChange={(e) => setPlasticId(e.target.value)}>
          <option value="">— выберите —</option>
          {pl.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <Field l="Сообщение со ссылками">
        <textarea className="inp" rows={5} value={links} onChange={(e) => setLinks(e.target.value)}
          placeholder="Вставьте текст — ссылки найду сам" />
      </Field>

      {pollErr ? <div className="warn">
        <DIcon n="bell" size={15} />
        <span>{pollErr}{batchGone(pollErr)
          ? ' — запустите расчёт заново.'
          : ' — пакет считается дальше, жду связи.'}</span>
      </div> : null}

      {bid && !data && !batchGone(pollErr) ? <div className="hint">Запустил, жду первые результаты…</div> : null}

      {items.length ? <div>
        {items.map((it) => {
          const off = excl.has(it.idx);
          return <div key={it.idx} className="bd-row" style={{ opacity: off ? 0.45 : 1, gap: 10, alignItems: 'center' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flex: 1 }}>
              {/* .pv и .oname в макете живут только внутри .tbl — здесь тот же вид инлайном */}
              {it.preview ? <img src={it.preview} alt="" onClick={() => lightbox(it.preview)}
                style={{
                  width: 40, height: 40, borderRadius: 9, objectFit: 'cover',
                  flex: '0 0 auto', background: 'var(--bg)', cursor: 'zoom-in',
                }} /> : null}
              <span style={{ minWidth: 0 }}>
                <span style={{
                  display: 'block', fontWeight: 600, color: 'var(--text)',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{it.label || String(it.url).replace(/^https?:\/\//, '').slice(0, 46)}</span>
                {it.status === 'ok' ? <span className="hint" style={{ display: 'block' }}>
                  {grams(it.grams)} · {minutes(it.minutes)}{it.plastic ? ' · ' + it.plastic : ''}
                </span> : null}
                {it.status === 'err' ? <span className="hint" style={{ display: 'block', color: 'var(--amber)' }}>
                  {it.error || 'не посчиталась'}
                </span> : null}
                {it.status === 'wait' || it.status === 'run'
                  ? <span className="hint" style={{ display: 'block' }}>
                    {it.status === 'run' ? 'считаю…' : 'в очереди'}</span> : null}
              </span>
            </span>
            {/* span, а не b: правило .bd-row b задаёт моношрифт и утащило бы за собой подпись
                чекбокса — моноширинной должна быть только цена */}
            {it.status === 'ok' ? <span style={{ display: 'flex', alignItems: 'center', gap: 10, flex: '0 0 auto' }}>
              <span className="mono" style={{ color: 'var(--price)' }}>{money(it.price)}</span>
              <label className="chk">
                <input type="checkbox" checked={!off} onChange={() => togItem(it.idx)}
                  style={{ width: 15, height: 15, accentColor: 'var(--accent)' }} />
                слать
              </label>
            </span> : null}
          </div>;
        })}
      </div> : null}

      {running && items.length ? <div className="hint">Ещё считаю — список обновляется сам…</div> : null}

      {data && data.done && !data.cancelled ? <ABtn kind="soft full" icon="chat"
        disabled={!okItems.length} onClick={send}>
        Отправить клиенту ({num(okItems.length)})
      </ABtn> : null}
      {data && data.cancelled ? <div className="hint">Пакет отменён.</div> : null}
    </div>
  </Drawer>;
}
