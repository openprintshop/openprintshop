// activity.jsx — «Активность»: лента действий клиентов в боте. Только просмотр.

import { AppCtx, Empty } from '../ui.jsx';
import { DIcon } from '../icons.jsx';
import { useApi } from '../api.jsx';
import { ago, dt } from '../fmt.jsx';
import { Screen, Chips, Search } from '../parts.jsx';
import { CalcDrawer, hasCalc } from './calcview.jsx';

const { useState, useContext, useMemo } = React;

// Тип действия выводим из текста: отдельного поля у события нет.
const KIND = {
  all: { l: 'Все', f: () => true },
  order: { l: 'Заказы', f: (e) => /заказ/i.test(e.action) },
  calc: { l: 'Расчёты', f: (e) => /расч|цен/i.test(e.action) },
  cart: { l: 'Корзина', f: (e) => /корзин/i.test(e.action) },
  chat: { l: 'Сообщения', f: (e) => /сообщ/i.test(e.action) },
};

// Лента живёт колонкой по центру: на широком мониторе таблица иначе расползается на всю ширину.
const COL = { maxWidth: 1700, width: '100%', margin: '0 auto' };
// Сортировки на экране нет, поэтому гасим общий .tbl th{cursor:pointer}.
const TH = { cursor: 'default' };

export function ActivityScreen() {
  const { nav } = useContext(AppCtx);
  const [kind, setKind] = useState('all');
  const [q, setQ] = useState('');
  const [calc, setCalc] = useState(null);   // открытый снимок сметы (клик по строке про цену)
  // Лента событий клиентов — тот случай, где устаревший экран бесполезен по определению.
  const a = useApi('adminActivity', [], [], { auto: 15000 });
  const events = (a.data && a.data.events) || [];

  const shown = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return events.filter(KIND[kind].f).filter((e) => !ql ||
      String(e.name || '').toLowerCase().includes(ql) ||
      String(e.action || '').toLowerCase().includes(ql) ||
      String(e.detail || '').toLowerCase().includes(ql));
  }, [events, kind, q]);

  const counts = useMemo(() => {
    const c = {};
    Object.entries(KIND).forEach(([k, v]) => { c[k] = events.filter(v.f).length; });
    return c;
  }, [events]);

  return <div className="onecol" style={COL}>
    <div className="ms-tools">
      <Search value={q} onChange={setQ} placeholder="Клиент или действие…" />
    </div>
    <Chips items={Object.entries(KIND).map(([k, v]) => ({ k, l: v.l, n: counts[k] }))}
      value={kind} onChange={setKind} />
    {/* .twrap даёт подложку и собственный скролл — без скроллящего предка sticky-шапка не липнет. */}
    <Screen state={a}>{() => <div className="twrap" style={{ flex: 1 }}>
      {shown.length ? <table className="tbl">
        <thead><tr>
          <th style={TH}>Клиент</th>
          <th style={TH}>Действие</th>
          <th style={TH}>Деталь</th>
          <th style={{ ...TH, width: 150 }}>Когда</th>
        </tr></thead>
        <tbody>
          {/* строка про цену открывает саму смету, остальные — карточку клиента */}
          {shown.map((e) => <tr key={e.id} className="row"
            title={hasCalc(e) ? 'Показать смету, которую увидел клиент' : 'Открыть карточку клиента'}
            onClick={() => (hasCalc(e) ? setCalc(e.calc_id) : (e.user_id && nav('clients', { sel: e.user_id })))}>
            <td style={{ fontWeight: 600 }}>{e.name || 'Гость'}</td>
            <td style={{ color: 'var(--muted)' }}>
              {hasCalc(e) ? <DIcon n="calc" size={12} /> : null} {e.action}
            </td>
            <td className="mono" style={{ color: 'var(--faint)', fontSize: 12.5 }}>{e.detail}</td>
            <td className="mono faint" title={dt(e.at)}>{ago(e.at)}</td>
          </tr>)}
        </tbody>
      </table> : <Empty ic="pulse" t="Пока тихо" ds="Клиенты ничего не делали"
        cta={q || kind !== 'all' ? 'Сбросить фильтр' : null}
        onCta={() => { setQ(''); setKind('all'); }} />}
    </div>}
    </Screen>
    {calc ? <CalcDrawer id={calc} onClose={() => setCalc(null)} nav={nav} /> : null}
    <div className="hint" style={{ marginTop: 10 }}>только просмотр — сигналы интереса клиентов из бота</div>
  </div>;
}
