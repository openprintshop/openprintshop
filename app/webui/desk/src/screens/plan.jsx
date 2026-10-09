// plan.jsx — «План недели»: дедлайны заказов против мощности принтера.

import { DIcon } from '../icons.jsx';
import { AppCtx, StBadge, Empty, Bar } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { Screen } from '../parts.jsx';

const { useContext, useMemo } = React;

// Сколько часов печати реально влезает в сутки. Ночная печать без присмотра — норма,
// но с запасом на смену катушки и снятие детали.
const CAP_HOURS = 14;
const DOW = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];

/** «ДД.ММ» → Date в текущем (или следующем) году. */
function parseDue(due) {
  const m = /^(\d{1,2})\.(\d{1,2})$/.exec(String(due || '').trim());
  if (!m) return null;
  const now = new Date();
  let d = new Date(now.getFullYear(), Number(m[2]) - 1, Number(m[1]));
  // срок в прошлом больше чем на полгода — значит имелся в виду следующий год
  if (d < now && (now - d) > 182 * 86400000) d = new Date(now.getFullYear() + 1, Number(m[2]) - 1, Number(m[1]));
  return d;
}

const key = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
const dm = (d) => `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}`;
// Часы одной шкалой со столбцом загрузки: «2.5 ч», «3 ч» — целое без хвоста .0.
const hrs = (m) => (m / 60).toFixed(1).replace('.0', '');

/** Миниатюра заказа. Превью есть не у всех — вместо битой картинки держим пустой слот, чтобы строка не съезжала. */
function Pv({ src, size, r }) {
  return src
    ? <img src={src} alt="" style={{ width: size, height: size, borderRadius: r, objectFit: 'cover', flex: '0 0 auto' }} />
    : <div style={{ width: size, height: size, borderRadius: r, background: 'var(--bg)', flex: '0 0 auto' }} />;
}

export function PlanScreen() {
  const { nav, toast } = useContext(AppCtx);
  const q = useApi('adminQueue', [], [], { auto: 20000 });
  const orders = useApi('adminOrders', [''], [], { auto: 20000 });

  const days = useMemo(() => {
    const out = [];
    const t = new Date();
    for (let i = 0; i < 7; i++) {
      const d = new Date(t.getFullYear(), t.getMonth(), t.getDate() + i);
      out.push(d);
    }
    return out;
  }, []);

  const all = (orders.data && orders.data.orders) || [];
  // минуты берём из очереди: там уже посчитано время печати с учётом количества
  const mins = useMemo(() => {
    const m = {};
    ((q.data && q.data.queue) || []).forEach((x) => { m[x.id] = x.minutes || 0; });
    return m;
  }, [q.data]);

  const active = all.filter((o) => !['printed', 'done', 'handed', 'rejected', 'cancelled'].includes(o.status));
  const byDay = useMemo(() => {
    const m = {};
    days.forEach((d) => { m[key(d)] = []; });
    const none = [];
    active.forEach((o) => {
      const d = parseDue(o.due);
      const k = d ? key(d) : null;
      if (k && k in m) m[k].push(o); else none.push(o);
    });
    return { m, none };
  }, [active, days]);

  const setDue = async (id, d) => {
    const due = d ? dm(d) : '';
    const o = all.find((x) => x.id === id);
    await call('adminOrderStatus', id, o ? o.status : 'in_progress', due, '', '');
    toast(due ? `Срок заказа #${id}: ${due}` : `Срок заказа #${id} снят`, { kind: 'ok' });
    orders.reload();
  };

  return <Screen state={orders}>{() => <div className="split">
    <div className="ms" style={{ '--ms-w': '560px' }}>
      <div className="card" style={{ flex: 1, minHeight: 0 }}>
        <div className="lab"><DIcon n="queue" size={13} />Без срока · {byDay.none.length}</div>
        <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          {byDay.none.length ? byDay.none.map((o) => <div key={o.id} className="qrow"
            onClick={() => nav('orders', { sel: o.id })}>
            <Pv src={o.preview} size={44} r={9} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="nm">#{o.id} · {o.label || '—'}</div>
              <div className="ds">{o.user || '—'}{mins[o.id] ? ` · ≈ ${hrs(mins[o.id])} ч` : ''}</div>
            </div>
            <StBadge st={o.status} />
            {/* Срок ставится и в карточке заказа, но здесь список уже отсортирован по «нет срока» —
                выпадашка экономит переход туда-обратно на каждый заказ. */}
            <select className="sel" value="" style={{ flex: '0 0 auto', width: 104, fontSize: 12 }}
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => { const i = e.target.value; if (i !== '') setDue(o.id, days[Number(i)]); }}>
              <option value="">на день…</option>
              {days.map((d, i) => <option key={i} value={i}>{DOW[d.getDay()]} {dm(d)}</option>)}
            </select>
          </div>) : <Empty ic="check" t="Все заказы со сроком" />}
        </div>
        <div className="hint" style={{ marginTop: 10 }}>
          срок можно назначить здесь или в карточке заказа; печать ≈ {CAP_HOURS} ч/день
        </div>
      </div>
    </div>

    <div style={{ flex: 1, minWidth: 0, display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 14, minHeight: 0 }}>
      {days.map((d, di) => {
        const list = byDay.m[key(d)] || [];
        const load = list.reduce((s, o) => s + (mins[o.id] || 0), 0) / 60;
        const over = load > CAP_HOURS;
        const today = di === 0;
        return <div key={key(d)} className="card"
          style={{ padding: 16, minHeight: 0, border: today ? '1px solid var(--accent-line)' : undefined }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
            <span style={{ fontWeight: 700, fontSize: 15, color: today ? 'var(--accent)' : 'var(--text)' }}>{DOW[d.getDay()]}</span>
            <span className="mono" style={{ fontSize: 12, color: 'var(--faint)' }}>{dm(d)}</span>
            {today ? <span className="stb amber" style={{ marginLeft: 'auto' }}>сегодня</span> : null}
          </div>
          {/* Цвет полосы — единственный сигнал перегруза: красный за 100%, янтарный с 70% мощности. */}
          <div style={{ marginBottom: 6 }}>
            <Bar pct={load / CAP_HOURS * 100} h={7}
              color={over ? 'var(--red)' : load > CAP_HOURS * 0.7 ? 'var(--amber)' : 'var(--price)'} />
          </div>
          <div className="mono" style={{ fontSize: 11.5, color: over ? 'var(--red)' : 'var(--faint)', marginBottom: 12 }}>
            {load ? load.toFixed(1).replace('.0', '') : 0} ч из {CAP_HOURS}{over ? ' · перегруз' : ''}
          </div>
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {list.map((o) => <div key={o.id}
              style={{ background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 11, padding: '10px 12px', cursor: 'pointer' }}
              onClick={() => nav('orders', { sel: o.id })}>
              <div style={{ display: 'flex', gap: 9, alignItems: 'center' }}>
                <Pv src={o.preview} size={34} r={7} />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    #{o.id} {o.label || '—'}
                  </div>
                  <div className="mono" style={{ fontSize: 11, color: 'var(--faint)' }}>
                    {mins[o.id] ? `≈ ${hrs(mins[o.id])} ч · ` : ''}{o.user || '—'}
                  </div>
                </div>
                <button className="ib sm" title="Снять срок" style={{ marginLeft: 'auto', flex: '0 0 auto' }}
                  onClick={(e) => { e.stopPropagation(); setDue(o.id, null); }}>
                  <DIcon n="close" size={12} />
                </button>
              </div>
              <div style={{ marginTop: 8 }}><StBadge st={o.status} /></div>
            </div>)}
            {!list.length
              ? <div style={{ color: 'var(--faint)', fontSize: 12, textAlign: 'center', marginTop: 20 }}>свободно</div>
              : null}
          </div>
        </div>;
      })}
    </div>
  </div>}</Screen>;
}
