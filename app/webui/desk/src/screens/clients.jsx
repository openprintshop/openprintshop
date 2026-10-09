// clients.jsx — «Клиенты» (CRM): список с сегментами, карточка клиента, сплошной чат.

import { DIcon } from '../icons.jsx';
import { AppCtx, StBadge, Sec, Empty, Drawer, Field } from '../ui.jsx';
import { API, call, useApi } from '../api.jsx';
import { money, num, ago, dt, plural, curSym } from '../fmt.jsx';
import { Screen, Chips, Search, Split, ABtn, PickOne, useAutoScroll, Loading, ErrBox, useConfirm, Attached, Bubble, pastedImage, readImage } from '../parts.jsx';

const { useState, useEffect, useContext, useCallback, useMemo, useRef } = React;

// .seg.vip/.new2/.rep/.sleep/.vis — классы макета; ключи сегментов приходят с бэкенда
const SEG_CLS = { vip: 'vip', new: 'new2', repeat: 'rep', sleeping: 'sleep', visitor: 'vis' };
const Seg = ({ s, label }) => <span className={'seg ' + (SEG_CLS[s] || '')}>{label || s}</span>;

// порядок и подписи как в макете; ключи — те, что понимает adminClients
const SORTS = [['last', 'Свежие'], ['ltv', 'LTV'], ['orders', 'Заказы']];

/** Аватарка клиента: бот генерирует картинку в профиле, сервер отдаёт её в поле avatar
 *  (webapp.py::_api_admin_clients / _api_admin_client). Если картинки нет — буква имени,
 *  как было раньше. Класс .avatar один и тот же, чтобы кружок совпал по размеру. */
const Ava = ({ c, size, cls }) => {
  // Аватарка может не загрузиться (файл удалён, подпись протухла, нет сети) — тогда <img>
  // показывал бы сломанную иконку. Ловим onError и падаем на букву имени, как без картинки.
  const [bad, setBad] = useState(false);
  const st = size ? { width: size, height: size, fontSize: Math.round(size * 0.37) } : null;
  const k = 'avatar' + (cls ? ' ' + cls : '');
  return c.avatar && !bad
    ? <img className={k} src={c.avatar} alt="" onError={() => setBad(true)}
        style={{ ...(st || {}), objectFit: 'cover' }} />
    : <div className={k} style={st}>{(c.name || '?').trim()[0].toUpperCase()}</div>;
};

export function ClientsScreen() {
  const { params, nav, drawer, setDrawer } = useContext(AppCtx);
  const [q, setQ] = useState('');
  const [seg, setSeg] = useState('');
  const [sort, setSort] = useState('last');
  const [qSent, setQSent] = useState('');

  useEffect(() => {
    const t = setTimeout(() => setQSent(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  const list = useApi('adminClients', [{ q: qSent, segment: seg, sort }], [qSent, seg, sort],
    { auto: 30000 });
  const clients = (list.data && list.data.clients) || [];
  const segLabels = (list.data && list.data.segments) || {};
  const segCounts = (list.data && list.data.segment_counts) || {};
  const total = (list.data && list.data.total) || 0;
  const sel = params.sel ? Number(params.sel) : null;

  const pick = useCallback((uid) => nav('clients', { sel: uid }), [nav]);

  // пустые сегменты не показываем: фильтр, который заведомо ничего не даст, только мешает
  const chips = useMemo(() => [{ k: '', l: 'Все', n: total }].concat(
    Object.entries(segLabels)
      .filter(([k]) => segCounts[k])
      .map(([k, l]) => ({ k, l, n: segCounts[k] }))), [segLabels, segCounts, total]);

  // CSV отдаётся обычным GET-файлом, а не методом API — отсюда открытие ссылки, а не call()
  const csv = () => {
    const url = API.adminClientsCsvUrl && API.adminClientsCsvUrl();
    if (!url) return;
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
    else window.open(url, '_blank');
  };

  const listPane = <>
    <div className="ms-tools">
      <Search value={q} onChange={setQ} placeholder="Имя, @ник или Telegram ID" />
      <button className="db soft" onClick={() => setDrawer('broadcast')}>
        <DIcon n="send" size={15} />Рассылка
      </button>
      <button className="db line" onClick={() => setDrawer('segments')}>
        <DIcon n="wrench" size={15} />Сегменты
      </button>
      <button className="ib" title="Выгрузить CSV" onClick={csv}><DIcon n="doc" /></button>
      <button className="ib" title="Обновить" onClick={list.reload}><DIcon n="refresh" size={16} /></button>
    </div>

    {/* один ряд чипов макета: слева сегменты, справа сортировка */}
    <div className="ms-tools">
      <Chips items={chips} value={seg} onChange={setSeg} style={{ margin: 0, flex: 1 }} />
      <Chips items={SORTS.map(([k, l]) => ({ k, l }))} value={sort} onChange={setSort} style={{ margin: 0 }} />
    </div>

    <div className="twrap">
      <Screen state={list}>{() => <>
        {clients.length ? <table className="tbl">
          <thead><tr>
            <th style={{ cursor: 'default' }}>Клиент</th>
            <th style={{ width: 110, cursor: 'default' }}>Сегмент</th>
            <th style={{ width: 100, textAlign: 'right', cursor: 'default' }}>LTV</th>
            <th style={{ width: 200, cursor: 'default' }}>Заказы</th>
            <th style={{ width: 100, cursor: 'default' }}>Последний</th>
          </tr></thead>
          <tbody>
            {clients.map((c) => <tr key={c.uid} className={'row' + (c.uid === sel ? ' sel' : '')}
              onClick={() => pick(c.uid)}>
              <td>
                <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}>
                  <Ava c={c} size={38} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 7 }}>
                      {c.name}
                      {c.no_broadcast ? <span title="Отписан от рассылок">🔕</span> : null}
                      {c.unread > 0 ? <span className="msg-pill" title="непрочитанные сообщения">{c.unread}</span> : null}
                    </div>
                    <div className="mono" style={{ fontSize: 11.5, color: 'var(--faint)' }}>
                      {c.tg_name ? c.tg_name + ' · ' : ''}id {c.uid}
                    </div>
                  </div>
                </div>
              </td>
              <td>
                <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                  {(c.segments || []).map((s) => <Seg key={s} s={s} label={segLabels[s]} />)}
                </div>
              </td>
              <td className="mono" style={{ textAlign: 'right', color: 'var(--price)', fontWeight: 700 }}>
                {money(c.spent)}
              </td>
              {/* действующая скидка видна прямо в списке: мастер не должен выдать её дважды */}
              <td className="mono faint">
                {num(c.orders)} зак. · {num(c.printed)} нап.
                {c.discount_pct
                  ? <span className="stb amber" style={{ marginLeft: 6 }} title="персональная скидка уже действует">
                      уже −{c.discount_pct}%
                    </span>
                  : null}
              </td>
              {/* у посетителя заказов нет — вместо вечного «—» показываем последний визит */}
              <td className="mono faint" title={c.last_at ? 'последний заказ' : 'последний визит в бота'}>
                {c.last_at ? ago(c.last_at) : (c.last_seen_at ? 'был ' + ago(c.last_seen_at) : '—')}
              </td>
            </tr>)}
          </tbody>
        </table> : <Empty ic="users" t="Клиентов не найдено"
          ds={q || seg ? 'Поменяйте запрос или сегмент' : 'Пока бота никто не открывал'}
          cta={q || seg ? 'Сбросить фильтры' : null} onCta={() => { setQ(''); setSeg(''); }} />}
      </>}</Screen>
    </div>
    <div className="hint" style={{ marginTop: 10 }}>
      сегменты: авто по порогам («Сегменты» — настроить) · 🔕 — отписан от рассылок
    </div>
  </>;

  // key={sel} обязателен: без него поля карточки (имя, телефон, день рождения) остались бы
  // от прошлого клиента — состояние формы живёт в компоненте, а не в данных.
  const detail = sel
    ? <ClientCard key={sel} uid={sel} onChanged={list.reload} segLabels={segLabels} />
    : <PickOne what="клиента" />;

  return <>
    <Split width={1000} list={listPane} detail={detail} />
    {drawer === 'broadcast' && <BroadcastDrawer onClose={() => setDrawer(null)} segLabels={segLabels} />}
    {drawer === 'segments' && <SegmentsDrawer onClose={() => setDrawer(null)} onSaved={list.reload} />}
  </>;
}

// ---------------- карточка клиента ----------------

function ClientCard({ uid, onChanged, segLabels }) {
  const { nav } = useContext(AppCtx);
  const d = useApi('adminClient', [uid], [uid], { auto: 20000 });

  if (d.loading && !d.data) return <Loading what="Открываю карточку…" />;
  if (d.err && !d.data) return <ErrBox err={d.err} onRetry={d.reload} />;
  const c = d.data && d.data.client;
  if (!c) return <Empty t="Клиент не найден" />;

  const after = () => { d.reload(); onChanged(); };

  return <div className="od">
    <div className="od-head">
      <Ava c={c} cls="lg" />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="tt" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {c.name}
          {c.no_broadcast ? <span title="отписан от рассылок">🔕</span> : null}
          <span style={{ display: 'flex', gap: 6 }}>
            {(c.segments || []).map((s) => <Seg key={s} s={s} label={segLabels[s]} />)}
          </span>
        </div>
        <div className="sub">
          {c.tg_name ? c.tg_name + ' · ' : ''}id {c.uid}
          {c.first_ago ? ` · первый заказ ${c.first_ago}` : ''}
          {c.last_ago ? ` · последний ${c.last_ago}` : ''}
          {/* заказов нет — единственное, что мы про человека знаем, это его визиты */}
          {!c.orders && c.seen_ago ? ` · заходил ${c.seen_ago}` : ''}
          {c.fav_material ? ` · любимый материал: ${c.fav_material}` : ''}
        </div>
      </div>
      {c.discount_pct ? <span className="stb amber">скидка −{c.discount_pct}%</span> : null}
      <button className="db soft sm" onClick={() => nav('orders', { q: c.name })}>
        Заказы клиента<DIcon n="chev" size={14} />
      </button>
      <button className="ib" title="Обновить" onClick={d.reload}><DIcon n="refresh" size={16} /></button>
    </div>

    <ClientKpi c={c} />

    <div className="od-body">
      <div className="od-main">
        {c.family ? <FamilySec c={c} nav={nav} /> : null}
        {c.cart && c.cart.count ? <CartSec c={c} /> : null}
        <RecentSec c={c} nav={nav} />
        <TasksSec c={c} after={after} />
        <BalanceSec c={c} after={after} />
        <CardSec c={c} after={after} />
        <TimelineSec c={c} nav={nav} />
      </div>
      <ClientChat uid={uid} name={c.name} avatar={c.avatar} onSent={after} nav={nav}
        sleeping={(c.segments || []).includes('sleeping')} />
    </div>
  </div>;
}

function ClientKpi({ c }) {
  const cells = [
    ['Заказов', num(c.orders), 'receipt'],
    ['Напечатано', num(c.printed), 'printer'],
    ['LTV', money(c.spent), 'coin', 'var(--price)'],
    ['Средний чек', money(c.avg_check), 'tag'],
    ['Баланс', money(c.balance), 'coin'],
    ['Рефералов', num(c.referrals), 'users'],
  ];
  return <div className="ckpi">
    {cells.map(([k, v, ic, col]) => <div key={k} className="cell">
      <div className="k" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <DIcon n={ic} size={12} style={{ color: 'var(--accent)', flexShrink: 0 }} />{k}
      </div>
      <div className="v" style={{ color: col }}>{v}</div>
    </div>)}
  </div>;
}

function FamilySec({ c, nav }) {
  const f = c.family;
  const who = (f.members || []).map((m) => m.user_name).join(' + ');
  return <div className="fambox span2" style={{ gridColumn: '1/3' }}>
    <DIcon n="users" size={17} />
    Семейный счёт #{f.id}: {who} · общий баланс {money(f.balance)}
    <button className="db sm line" style={{ marginLeft: 'auto', color: 'var(--violet)', borderColor: 'rgba(169,139,255,.35)' }}
      onClick={() => nav('families')}>Открыть</button>
  </div>;
}

function CartSec({ c }) {
  const cart = c.cart;
  return <Sec ic="grid" t="🛒 Корзина — не оформлена"
    right={<span style={{ color: 'var(--faint)' }}>обновлена {cart.updated_label}</span>} span2>
    {(cart.items || []).map((i) => <div key={i.token} className="bd-row">
      <span>{i.label}</span><b>{i.totalStr || money(i.total)}</b>
    </div>)}
    <div className="bd-row tot">
      <span>Итого · сигнал интереса — можно подтолкнуть сообщением</span>
      <b>{cart.total || money(0)}</b>
    </div>
  </Sec>;
}

function RecentSec({ c, nav }) {
  const orders = c.recent || [];
  // секцию рендерим всегда: иначе сетка карточки «прыгает» на клиентах без заказов
  return <Sec ic="receipt" t="Последние заказы">
    {orders.length ? orders.map((o) => <div key={o.id} className="orow" onClick={() => nav('orders', { sel: o.id })}>
      {o.preview ? <img src={o.preview} alt="" /> : null}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="nm">#{o.id} · {o.label}</div>
        <div className="ds">{money(o.price)} · {o.ago}</div>
      </div>
      <StBadge st={o.status} />
    </div>) : <div className="hint">заказов пока нет</div>}
  </Sec>;
}

function BalanceSec({ c, after }) {
  const { toast } = useContext(AppCtx);
  const [amt, setAmt] = useState('');
  const [comment, setComment] = useState('');
  // какая транзакция сейчас редактируется: {id, text}. null — никакая
  const [txEdit, setTxEdit] = useState(null);
  const done = (msg) => { toast(msg, { kind: 'ok' }); setAmt(''); setComment(''); after(); };

  // комментарий пишется к уже проведённой транзакции (adminBalanceComment → /api/admin/balance_comment,
  // поля tx_id + comment). Пустой текст сервер трактует как «стереть заметку».
  const saveTxComment = async () => {
    try {
      await call('adminBalanceComment', txEdit.id, txEdit.text.trim());
      toast(txEdit.text.trim() ? 'Комментарий сохранён' : 'Комментарий удалён', { kind: 'ok' });
      setTxEdit(null);
      after();
    } catch (e) {
      toast(e.message, { kind: 'warn2' });
    }
  };

  const fam = c.family;
  const famTx = (fam && fam.tx) || [];

  return <Sec ic="coin" t="Баланс и зачисления"
    right={<span className="mono" style={{ fontSize: 17, fontWeight: 700, color: 'var(--price)' }}>{money(c.balance)}</span>}>
    <div className="frow">
      <Field l={`Сумма, ${curSym()}`}>
        <input className="inp mono" type="number" step="0.01" value={amt} placeholder="500"
          onChange={(e) => setAmt(e.target.value)} />
      </Field>
      <Field l="Комментарий">
        <input className="inp" value={comment} placeholder="назначение — виден клиенту"
          onChange={(e) => setComment(e.target.value)} />
      </Field>
    </div>
    <div style={{ display: 'flex', gap: 10, margin: '12px 0 16px' }}>
      <ABtn kind="soft" style={{ flex: 1 }} disabled={!amt} onClick={async () => {
        try {
          await call('adminBalance', c.uid, Math.abs(Number(amt)), 'Пополнение мастером', comment, 0);
          done(fam ? 'Зачислено на общий счёт семьи' : 'Зачислено, клиент уведомлён');
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Зачислить</ABtn>
      <ABtn kind="line" style={{ flex: 1 }} disabled={!amt} onClick={async () => {
        try {
          await call('adminBalance', c.uid, -Math.abs(Number(amt)), 'Списание мастером', comment, 0);
          done('Списано с баланса');
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>− Списать</ABtn>
    </div>

    {/* Клиент в семье: зачисление выше уходит на ОБЩИЙ счёт, поэтому и движения по нему
        показываем здесь — иначе сальдо в шапке выглядит взявшимся из ниоткуда.
        Столбец «кто» отвечает на главный вопрос мастера: чьей покупкой ушли деньги. */}
    {famTx.length ? <>
      <div className="f-lab">Общий счёт семьи #{fam.id} · {money(fam.balance)}</div>
      {famTx.map((t) => <div key={'f' + t.id} className="hrow">
        <span className="hd">{t.at_label}</span>
        <span style={{ flex: 1, minWidth: 0 }}>
          {t.reason || (t.amount >= 0 ? 'Пополнение' : 'Списание')}
          {t.user_name ? <span style={{ color: 'var(--faint)' }}> · {t.user_name}</span> : null}
          {t.comment ? <span style={{ color: 'var(--faint)' }}> · 💬 {t.comment}</span> : null}
        </span>
        <span className="hv" style={{ color: t.amount >= 0 ? 'var(--price)' : 'var(--muted)' }}>
          {t.amount >= 0 ? '+' : '−'}{money(Math.abs(t.amount))}
        </span>
      </div>)}
    </> : null}

    <div className="f-lab">История движений</div>
    {/* Fragment, а не div-обёртка: .hrow:last-child в макете убирает нижнюю границу,
        и лишний контейнер сломал бы разделители между строками. */}
    {c.balance_tx && c.balance_tx.length ? c.balance_tx.map((t) => <React.Fragment key={t.id}>
      <div className="hrow">
        <span className="hd">{t.at_label}</span>
        <span style={{ flex: 1, minWidth: 0 }}>
          {t.reason}
          {t.comment && !(txEdit && txEdit.id === t.id)
            ? <span style={{ color: 'var(--faint)' }}> · 💬 {t.comment}</span> : null}
        </span>
        <span className="hv" style={{ color: t.amount > 0 ? 'var(--price)' : 'var(--muted)' }}>
          {t.amount > 0 ? '+' : ''}{money(t.amount)}
        </span>
        <button className="ib sm" title="Комментарий к операции"
          onClick={() => setTxEdit(txEdit && txEdit.id === t.id ? null : { id: t.id, text: t.comment || '' })}>
          <DIcon n="pencil" size={14} />
        </button>
      </div>
      {txEdit && txEdit.id === t.id ? <div style={{ display: 'flex', gap: 10, margin: '2px 0 10px' }}>
        <input className="inp" autoFocus value={txEdit.text} style={{ flex: 1 }}
          placeholder="Комментарий — виден клиенту (пусто — удалить)"
          onChange={(e) => setTxEdit({ id: t.id, text: e.target.value })}
          onKeyDown={(e) => { if (e.key === 'Enter') saveTxComment(); }} />
        <ABtn kind="soft" onClick={saveTxComment}>Сохранить</ABtn>
        <button className="db line" onClick={() => setTxEdit(null)}>Отмена</button>
      </div> : null}
    </React.Fragment>) : <div className="hint">движений пока не было</div>}
    <div className="hint">Клиент видит баланс, историю и комментарии у себя в профиле.</div>
  </Sec>;
}

function CardSec({ c, after }) {
  const { toast } = useContext(AppCtx);
  const [f, setF] = useState({
    name: c.name || '', phone: c.phone || '', note: c.note || '',
    birthday: c.birthday || '', discount: c.discount_pct == null ? '' : String(c.discount_pct),
  });
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  const dirty = f.name !== (c.name || '') || f.phone !== (c.phone || '') ||
    f.note !== (c.note || '') || f.birthday !== (c.birthday || '');

  return <Sec ic="doc" t="Карточка и скидка">
    <div className="fgap">
      <div className="frow">
        <Field l="Имя (как удобно вам)">
          <input className="inp" value={f.name} onChange={(e) => set('name', e.target.value)} />
        </Field>
        <div className="frow">
          <Field l="Телефон">
            <input className="inp mono" value={f.phone} placeholder="+7 …"
              onChange={(e) => set('phone', e.target.value)} />
          </Field>
          <Field l="День рождения">
            <input className="inp mono" value={f.birthday} placeholder="ДД.ММ"
              title="за 3 дня появится задача «поздравить + промокод»"
              onChange={(e) => set('birthday', e.target.value)} />
          </Field>
        </div>
      </div>
      <Field l="Заметка">
        <textarea className="inp" rows="2" value={f.note} placeholder="приватная, клиент не видит"
          onChange={(e) => set('note', e.target.value)} />
      </Field>
      {/* скидка сохраняется своим эндпоинтом (adminClientDiscount), поэтому и кнопка своя */}
      <Field l="Персональная скидка, %">
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <input className="inp mono" type="number" min="0" max="90" value={f.discount}
            style={{ width: 110, flex: '0 0 auto' }}
            onChange={(e) => set('discount', e.target.value)} />
          <span className="hint" style={{ margin: 0, flex: 1 }}>0 — снять скидку</span>
          <ABtn kind="soft sm" onClick={async () => {
            try {
              await call('adminClientDiscount', c.uid, f.discount === '' ? null : Number(f.discount), c.name);
              toast('Скидка обновлена', { kind: 'ok' }); after();
            } catch (e) { toast(e.message, { kind: 'warn2' }); }
          }}>Применить</ABtn>
        </div>
      </Field>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <ABtn kind="soft" disabled={!dirty} onClick={async () => {
          try {
            await call('adminClientSave', c.uid, { name: f.name, phone: f.phone, note: f.note, birthday: f.birthday });
            toast('Карточка сохранена', { kind: 'ok' }); after();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }}>Сохранить</ABtn>
        <label className="chk">
          <input type="checkbox" checked={!!c.no_broadcast} onChange={async (e) => {
            const off = e.target.checked;
            try {
              await call('adminClientSave', c.uid, { no_broadcast: off ? 1 : 0 });
              toast(off ? 'Отписан от рассылок' : 'Подписан на рассылки', { kind: 'ok' });
              after();
            } catch (err) { toast(err.message, { kind: 'warn2' }); after(); }
          }} />
          Не слать рассылки
        </label>
      </div>
    </div>
  </Sec>;
}

function TasksSec({ c, after }) {
  const { toast } = useContext(AppCtx);
  const [title, setTitle] = useState('');
  const [due, setDue] = useState('');
  const tasks = c.tasks || [];
  const add = async () => {
    if (!title.trim()) return;
    try {
      await call('adminTask', { action: 'add', title: title.trim(), due, uid: c.uid });
      setTitle(''); setDue(''); after();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };
  // порядок макета: сперва список, форма добавления под ним
  return <Sec ic="task" t="Задачи по клиенту">
    {tasks.map((t) => <div key={t.id} className={'task' + (t.done ? ' done' : '')}>
      <button className="cb" onClick={async () => {
        try {
          await call('adminTask', { id: t.id, action: t.done ? 'reopen' : 'done' });
          after();
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>{t.done ? <DIcon n="check" size={13} sw={3} /> : null}</button>
      <div style={{ minWidth: 0 }}>
        <div className="tt">{t.title}</div>
        {t.due_label ? <div className={'dd' + (t.overdue ? ' red' : '')}>{t.due_label}</div> : null}
      </div>
    </div>)}
    <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
      <input className="inp" value={title} placeholder="Напомнить…"
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
      <input className="inp mono" value={due} placeholder="ДД.ММ / +3"
        style={{ width: 110, flex: '0 0 auto' }}
        onChange={(e) => setDue(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
      <ABtn kind="soft" disabled={!title.trim()} onClick={add} icon="plus" />
    </div>
  </Sec>;
}

/** Таймлайн: заказы и движения баланса на одной линии. */
function TimelineSec({ c, nav }) {
  const items = useMemo(() => {
    const out = [];
    (c.recent || []).forEach((o) => out.push({
      at: o.at, ic: 'receipt', c: 'var(--accent)',
      tt: `Заказ #${o.id} · ${o.label}`, ds: o.status_label, go: () => nav('orders', { sel: o.id }),
    }));
    (c.balance_tx || []).forEach((t) => out.push({
      at: t.at, ic: 'coin', c: t.amount > 0 ? 'var(--price)' : 'var(--muted)',
      tt: t.reason, ds: (t.amount > 0 ? '+' : '') + money(t.amount),
    }));
    return out.sort((a, b) => (b.at || 0) - (a.at || 0)).slice(0, 25);
  }, [c, nav]);

  // в макете сюда шли и сообщения, но adminClient их не отдаёт — подпись честная
  return <Sec ic="pulse" t="История"
    right={<span style={{ color: 'var(--faint)' }}>заказы · платежи</span>} span2>
    <div style={{ position: 'relative', paddingLeft: 30 }}>
      <span style={{ position: 'absolute', left: 9, top: 8, bottom: 8, width: 2, background: 'var(--line)' }} />
      {items.map((i, k) => <div key={k} onClick={i.go}
        style={{ position: 'relative', display: 'flex', gap: 10, alignItems: 'baseline',
          padding: '6px 0', fontSize: 13.5, cursor: i.go ? 'pointer' : 'default' }}>
        <span style={{ position: 'absolute', left: -30, top: 5, width: 20, height: 20, borderRadius: '50%',
          background: 'var(--bg-2)', border: '1px solid var(--line)', display: 'flex',
          alignItems: 'center', justifyContent: 'center' }}>
          <DIcon n={i.ic} size={11} style={{ color: i.c, verticalAlign: 0 }} />
        </span>
        <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {i.tt}{i.ds ? <span style={{ color: 'var(--faint)' }}> · {i.ds}</span> : null}
        </span>
        <span className="mono" style={{ fontSize: 11.5, color: 'var(--faint)', flexShrink: 0, marginLeft: 'auto' }}>
          {dt(i.at)}
        </span>
      </div>)}
      {!items.length ? <div className="hint" style={{ margin: 0 }}>событий пока нет</div> : null}
    </div>
  </Sec>;
}

// ---------------- сплошной чат клиента ----------------

const WINBACK = 'Здравствуйте! Давно не виделись — если что-то нужно напечатать, я на связи. ' +
  'В каталоге появились новые модели.';

function ClientChat({ uid, name, avatar, onSent, nav, sleeping }) {
  const { toast, lightbox } = useContext(AppCtx);
  // Ответ клиента приходит в Telegram-бота, а не в админку: без автообновления мастер видел
  // бы свою же реплику последней, пока не нажмёт «Обновить». Пять секунд — темп живой беседы.
  const chat = useApi('adminClientChat', [uid], [uid], { auto: 5000 });
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  // Прикреплённая, но ещё не отправленная картинка (файлом, вставкой или перетаскиванием).
  const [pic, setPic] = useState(null);
  const fileRef = useRef();
  const msgs = (chat.data && chat.data.messages) || [];
  const scRef = useAutoScroll(msgs.length);

  const send = async () => {
    const t = text.trim();
    if (!t && !pic) return;
    setBusy(true);
    try {
      await call('adminClientMessage', uid, t, pic);
      setText('');
      setPic(null);
      chat.reload();
      if (onSent) onSent();
    } catch (e) {
      // «Не доставлено» — не потеря: сообщение всё равно сохранено в переписке.
      toast(e.message, { kind: 'warn2', ms: 9000 });
      chat.reload();
    } finally { setBusy(false); }
  };

  // Картинка сначала становится вложением, а не улетает клиенту сама: так к скриншоту
  // можно дописать подпись — и так промах буфером не превращается в отправку.
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

  // Разделители «— по заказу #N —» появляются там, где тред меняется.
  const rows = [];
  let prevRid;
  msgs.forEach((m, i) => {
    const rid = m.request_id || null;
    if (rid !== prevRid) {
      rows.push({ sep: true, rid, label: m.order_label, key: 's' + i });
      prevRid = rid;
    }
    rows.push({ m, key: m.id || 'm' + i });
  });

  return <div className="od-chat cl-chat"
    onDragOver={(e) => e.preventDefault()}
    onDrop={(e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      e.preventDefault();
      take(f);
    }}>
    <div className="chat-h">
      <Ava c={{ name, avatar }} size={34} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="nm">Чат с клиентом</div>
        <div className="st">общий по всем заказам · уходит в Telegram</div>
      </div>
      {/* текст только подставляем в поле: отправку мастер подтверждает сам */}
      {sleeping ? <button className="db sm soft" title="Подставить текст для «спящего»"
        onClick={() => setText(WINBACK)}>🌙 win-back</button> : null}
      <button className="ib" title="Обновить" onClick={chat.reload}><DIcon n="refresh" size={15} /></button>
    </div>
    <div className="chat-msgs" ref={scRef}>
      {chat.loading && !msgs.length ? <Loading what="Загружаю переписку…" /> : null}
      {rows.map((r) => r.sep
        ? (r.rid
            ? <button key={r.key} className="chat-sep" onClick={() => nav('orders', { sel: r.rid })}>
                — по заказу #{r.rid}{r.label ? ' · ' + r.label : ''} —
              </button>
            : <div key={r.key} className="chat-sep">— вне заказа —</div>)
        : <Bubble key={r.key} m={r.m} onImage={lightbox} />)}
      {!chat.loading && !msgs.length
        ? <Empty ic="chat" t="Переписки нет" ds="Сообщение уйдёт клиенту в Telegram" /> : null}
    </div>
    <Attached src={pic} onDrop={() => setPic(null)} />
    <div className="chat-in">
      <input type="file" accept="image/*" ref={fileRef} style={{ display: 'none' }} onChange={attach} />
      <button className="ib" title="Прикрепить фото" onClick={() => fileRef.current.click()} disabled={busy}>
        <DIcon n="camera" size={17} />
      </button>
      <input className="inp" value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
        onPaste={paste}
        placeholder="Написать клиенту… (Enter — отправить, Ctrl+V — картинка)" disabled={busy} style={{ flex: 1 }} />
      {/* одной картинкой, без текста, отправить тоже можно — раньше кнопка требовала текст */}
      <button className="db" onClick={() => send()} disabled={busy || (!text.trim() && !pic)}>
        <DIcon n="send" size={15} />
      </button>
    </div>
  </div>;
}

// ---------------- панели ----------------

function BroadcastDrawer({ onClose, segLabels }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [seg, setSeg] = useState('');
  const [text, setText] = useState('');
  const [prev, setPrev] = useState(null);
  const [opts, setOpts] = useState({ random_model: false, whats_new: false });

  // Рассылка необратима: сообщения уходят живым людям в Telegram, отозвать их нельзя.
  // Поэтому спрашиваем — как перед удалением, — и называем охват из уже посчитанного превью.
  const send = () => confirm('Отправить рассылку?',
    `Сообщение получат ${prev.eligible} ${plural(prev.eligible, 'клиент', 'клиента', 'клиентов')}`
    + ` сегмента «${segLabels[seg] || 'Все, кроме посетителей'}». Отменить отправку будет нельзя.`,
    async () => {
      try {
        const r = await call('adminBroadcast', seg, text, false, opts);
        toast(`Отправлено: ${r.sent}, не дошло: ${r.failed}`, { kind: 'ok' });
        onClose();
      } catch (e) { toast(e.message, { kind: 'warn2' }); }
    }, 'Отправить');

  return <Drawer title="Рассылка по сегменту" sub="уйдёт от имени бота всем в сегменте"
    onClose={onClose}
    footer={<>
      <ABtn kind="full" style={{ flex: 1 }} disabled={!text.trim() || !prev}
        onClick={send}>Отправить</ABtn>
      <button className="db line" onClick={onClose}>Отмена</button>
    </>}>
    <div className="fgap">
      <Field l="Сегмент">
        <select className="sel" value={seg} onChange={(e) => { setSeg(e.target.value); setPrev(null); }}>
          {/* «Все» = заказывавшие и заведённые вручную; посетителям — только выбрав их
              сегмент явно (store/crm.crm_broadcast_targets) */}
          <option value="">Все, кроме посетителей</option>
          {Object.entries(segLabels).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
      </Field>
      <Field l="Текст">
        <textarea className="inp" rows="5" value={text} placeholder="Что расскажем?"
          onChange={(e) => { setText(e.target.value); setPrev(null); }} />
      </Field>
      <label className="chk">
        <input type="checkbox" checked={opts.random_model}
          onChange={(e) => setOpts((s) => ({ ...s, random_model: e.target.checked }))} />
        🎲 Приложить случайную модель из каталога
      </label>
      <label className="chk">
        <input type="checkbox" checked={opts.whats_new}
          onChange={(e) => setOpts((s) => ({ ...s, whats_new: e.target.checked }))} />
        ✨ Блок «Что нового»
      </label>
      {/* охват считается тем же эндпоинтом в режиме dry — и он же разблокирует «Отправить» */}
      <ABtn kind="soft" onClick={async () => {
        try { setPrev(await call('adminBroadcast', seg, text, true, opts)); }
        catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>
        Показать охват
      </ABtn>
      {prev ? <div className="okay">
        Получат <b>{prev.eligible} из {prev.total}</b> · отписаны: {prev.skipped_optout} ·
        недавно получали: {prev.skipped_recent}
        {prev.sample && prev.sample.length
          ? <><br /><span style={{ opacity: 0.8 }}>{prev.sample.join(', ')}</span></> : null}
      </div> : null}
    </div>
  </Drawer>;
}

function SegmentsDrawer({ onClose, onSaved }) {
  const { toast } = useContext(AppCtx);
  const s = useApi('adminSettings', [], []);
  const crm = (s.data && s.data.crm) || { order: [], labels: {}, values: {} };
  const [vals, setVals] = useState(null);
  const v = vals || crm.values || {};

  return <Drawer title="Настройки сегментов" sub="пороги авто-сегментации CRM" onClose={onClose}
    footer={<>
      <ABtn kind="full" style={{ flex: 1 }} disabled={!vals} onClick={async () => {
        try {
          // kind:'num' обязателен, иначе сервер отвечает «Неизвестный параметр» (webapp.py::_api_admin_setting)
          for (const [k, val] of Object.entries(vals)) await call('adminSetting', { key: k, kind: 'num', value: val });
        } catch (e) {
          toast(e.message, { kind: 'warn2' });
          return;
        }
        toast('Пороги сегментов сохранены', { kind: 'ok' });
        onClose(); onSaved();
      }}>Сохранить</ABtn>
      <button className="db line" onClick={onClose}>Закрыть</button>
    </>}>
    <div className="fgap">
      {(crm.order || []).map((k) => <Field key={k} l={crm.labels[k] || k}>
        <input className="inp mono" type="number" value={v[k] ?? ''}
          onChange={(e) => setVals({ ...(vals || crm.values), [k]: Number(e.target.value) })} />
      </Field>)}
      <div className="hint">
        Сегменты не хранятся в базе — они вычисляются на лету из этих порогов, поэтому протухнуть не могут.
      </div>
    </div>
  </Drawer>;
}
