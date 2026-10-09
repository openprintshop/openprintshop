// dash.jsx — «Дашборд»: метрики воронки и Big Data по накопленным печатям.

import { DIcon } from '../icons.jsx';
import { Empty, Area, Bar, ChartTip, barIndex } from '../ui.jsx';
import { ST } from '../statuses.jsx';
import { useApi } from '../api.jsx';
import { money, num, grams, cnt, minutes as fmtMin } from '../fmt.jsx';
import { Screen, Chips } from '../parts.jsx';

const { useState } = React;

const TABS = [{ k: 'metrics', l: 'Метрики' }, { k: 'big', l: '⚡ Big Data' }];

// Периоды воронки. Ключ совпадает с ключом блока crm из ответа сервера ('7d'/'30d'),
// пустой ключ — «всё время»: за него сервер периодов не считает, берём общие счётчики stats.
const PERIODS = [{ k: '', l: 'всё' }, { k: '30d', l: '30 дн' }, { k: '7d', l: '7 дн' }];

// Цвет сегмента доната берём из того же ST, что и бейджи .stb — иначе донат и таблица
// заказов красили бы один статус по-разному.
const STC = {
  gray: 'var(--muted)', amber: 'var(--amber)', violet: 'var(--violet)',
  green: 'var(--price)', red: 'var(--red)', blue: 'var(--blue)',
};

/** Горизонтальные полосы: удобнее вертикальных, когда подписи длинные. Цифры уже в строке —
 * подсказка под курсором тут не нужна, в отличие от Area/VBars. */
function HBars({ rows, color = 'var(--accent)', unit = '' }) {
  if (!rows || !rows.length) return <div className="hint">Нет данных.</div>;
  const max = Math.max(...rows.map((r) => r.v), 1);
  return <div>
    {rows.map((r, i) => <div key={i} className="brow">
      <span className="bl" title={r.l}>{r.l}</span>
      <span className="bb"><i style={{ width: (r.v / max) * 100 + '%', background: color }} /></span>
      <span className="bv">{r.d != null ? r.d : num(r.v) + unit}</span>
    </div>)}
  </div>;
}

/**
 * Столбики по дням: у ряда из 14 точек подписи под осью не нужны, важна форма — а точное
 * число дня показывает подсказка под курсором (нативный title её не заменяет: он появляется
 * с задержкой и не говорит, ЗА КАКОЙ день столбик).
 */
function VBars({ data, color = 'var(--accent)', labels, fmtV }) {
  const [hi, setHi] = useState(-1);
  if (!data || !data.length) return <div className="hint">Нет данных.</div>;
  const max = Math.max(...data, 1);
  const frac = (v) => Math.max(0.02, v / max);   // 2% — чтобы нулевой день был виден и наводился
  const pick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHi(barIndex(r.width ? (e.clientX - r.left) / r.width : 0, data.length));
  };
  return <div className="chwrap" onMouseMove={pick} onMouseLeave={() => setHi(-1)}
    style={{ display: 'flex', alignItems: 'flex-end', gap: 6, height: '100%', width: '100%' }}>
    {data.map((v, i) => <div key={i} style={{
      flex: 1, background: color, opacity: i === hi ? 1 : 0.32 + 0.68 * (v / max),
      borderRadius: '4px 4px 0 0', height: frac(v) * 100 + '%',
    }} />)}
    {hi >= 0 ? <ChartTip x={(hi + 0.5) / data.length} y={(1 - frac(data[hi])) * 100 + '%'}
      below={frac(data[hi]) > 0.8} t={labels && labels[hi]}
      v={fmtV ? fmtV(data[hi]) : num(data[hi])} /> : null}
  </div>;
}

/** Донат по статусам: parts — [{l, v, c}]. Легенда справа, общий счёт в центре. */
function Donut({ parts, size = 180 }) {
  const tot = parts.reduce((a, p) => a + p.v, 0);
  if (!tot) return <div className="hint">Нет данных.</div>;
  const R = 70, C = 2 * Math.PI * R;
  let off = 0;
  return <div style={{ display: 'flex', alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
    <svg width={size} height={size} viewBox="0 0 180 180" style={{ flexShrink: 0 }}>
      {parts.map((p, i) => {
        const len = (p.v / tot) * C;
        // зазор 3px между сегментами; при единственном сегменте он бы разорвал кольцо
        const gap = parts.length > 1 ? 3 : 0;
        const el = <circle key={i} cx="90" cy="90" r={R} fill="none" stroke={p.c} strokeWidth="26"
          strokeDasharray={`${Math.max(0, len - gap)} ${C - len + gap}`} strokeDashoffset={-off}
          transform="rotate(-90 90 90)" />;
        off += len;
        return el;
      })}
      <text x="90" y="86" textAnchor="middle" fill="var(--text)" fontSize="30" fontWeight="700"
        fontFamily="var(--mono)">{tot}</text>
      <text x="90" y="108" textAnchor="middle" fill="var(--faint)" fontSize="11"
        fontFamily="var(--ui)">заказов</text>
    </svg>
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7, minWidth: 0 }}>
      {parts.map((p, i) => <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 9, fontSize: 13 }}>
        <span style={{ width: 10, height: 10, borderRadius: 3, background: p.c, flexShrink: 0 }} />
        <span style={{ color: 'var(--muted)' }}>{p.l}</span>
        <b className="mono" style={{ marginLeft: 'auto', paddingLeft: 10 }}>{p.v}</b>
      </div>)}
    </div>
  </div>;
}

/** Подпись оси: дата «сегодня минус N дней». Ряд series14 всегда кончается сегодняшними сутками. */
function dayLabel(back) {
  const d = new Date();
  d.setDate(d.getDate() - back);
  return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0');
}

export function DashScreen() {
  const [tab, setTab] = useState('metrics');
  const d = useApi('adminDashboard', [], []);
  const b = useApi('adminBigData', [], []);

  // «Обновить» перечитывает только данные открытой вкладки: второй запрос мастеру не нужен,
  // а бот и вебапп живут в одном event loop — лишний вызов конкурирует с обработкой апдейтов.
  const active = tab === 'metrics' ? d : b;

  return <div className="onecol">
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '0 0 16px' }}>
      <Chips items={TABS} value={tab} onChange={setTab} style={{ margin: 0 }} />
      <button className="ib" title="Обновить" style={{ marginLeft: 'auto' }}
        onClick={active.reload}><DIcon n="refresh" size={16} /></button>
    </div>

    {tab === 'metrics'
      ? <Screen state={d}>{(data) => <Metrics s={data.stats || {}} crm={data.crm || {}} />}</Screen>
      : <Screen state={b}>{(data) => <BigData d={data} />}</Screen>}
  </div>;
}

function Metrics({ s, crm }) {
  const [per, setPer] = useState('');
  const series = (s.series14 || []).map((x) => Number(x.revenue) || 0);
  const dayOrders = (s.series14 || []).map((x) => Number(x.orders) || 0);
  // подписи для подсказок обоих графиков: ряд всегда кончается сегодняшними сутками
  const dayLabels = series.map((_, i) => dayLabel(series.length - 1 - i));

  const kpi = [
    ['coin', 'Выручка', money(s.revenue_printed), `+${money(s.orev_7d)} за 7 дней`, 'var(--price)'],
    ['receipt', 'Заказы', num(s.orders), `+${num(s.orders_7d)} за неделю`, null],
    ['tag', 'Средний чек', money(s.avg_order), 'по напечатанным', null],
    ['spool', 'Филамент', (Number(s.grams || 0) / 1000).toFixed(1) + ' кг', `${num(s.printed)} печатей всего`, null],
  ];

  // Периоды воронки сервер отдаёт уже посчитанными разом (crm['7d'] и crm['30d']), поэтому
  // переключатель работает без нового запроса. Пустой период = общие счётчики за всё время.
  const f = (per && crm[per]) || null;
  const base = f || s;
  const fSteps = [
    ['Расчёты', Number(base.calcs) || 0],
    ['Заказы', Number(base.orders) || 0],
    ['Напечатано', Number(base.printed) || 0],
  ];
  const fTop = fSteps[0][1] || 1;
  const convTotal = fSteps[0][1] ? (fSteps[2][1] / fSteps[0][1]) * 100 : 0;

  const mats = (s.top_materials || []).map(([l, v]) => ({ l: l || '—', v }));
  // reject_reasons живёт в crm-блоке ответа и приходит парами [причина, сколько]
  const rejects = (crm.reject_reasons || []).map(([l, v]) => ({ l: l || 'без причины', v }));
  const statuses = Object.entries(s.status_mix || {})
    .map(([k, v]) => ({ l: (ST[k] && ST[k].l) || k, v, c: STC[(ST[k] && ST[k].c) || 'gray'] }))
    .sort((a, b) => b.v - a.v);

  return <div className="dashrows">
    <div className="kpi4" style={{ flex: '0 0 auto' }}>
      {kpi.map(([ic, k, v, sub, c]) => <div key={k} className="card">
        <div className="lab" style={{ margin: 0 }}><DIcon n={ic} size={13} />{k}</div>
        <div className="v" style={{ color: c || undefined }}>{v}</div>
        <div className="s">{sub}</div>
      </div>)}
    </div>

    <div className="dash-r1">
      <div className="card">
        <div className="lab">Выручка · 14 дней
          <span className="r"><span className="mono" style={{ color: 'var(--faint)' }}>
            за 30 дней {money(s.orev_30d)} · {num(s.orders_30d)} заказов
          </span></span>
        </div>
        <div style={{ flex: 1, minHeight: 0 }}>
          {series.length > 1
            ? <Area data={series} fill labels={dayLabels} fmtV={(v) => money(v)} />
            : <div className="hint">Пока нет данных за период.</div>}
        </div>
        <div style={{
          display: 'flex', justifyContent: 'space-between',
          fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--faint)', marginTop: 8,
        }}>
          <span>{dayLabel(13)}</span><span>{dayLabel(7)}</span><span>{dayLabel(0)}</span>
        </div>
        <div className="hint" style={{ marginTop: 8 }}>
          по напечатанным заказам — единственный источник правды по деньгам
        </div>
      </div>

      <div className="card">
        <div className="lab">Заказы по дням</div>
        <div style={{ flex: 1, minHeight: 0, display: 'flex', alignItems: 'flex-end' }}>
          <VBars data={dayOrders} labels={dayLabels}
            fmtV={(v) => cnt(v, 'заказ', 'заказа', 'заказов')} />
        </div>
      </div>

      <div className="card">
        <div className="lab">Воронка
          <span className="r"><Chips items={PERIODS} value={per} onChange={setPer} style={{ margin: 0 }} /></span>
        </div>
        {fSteps.map(([l, v], i) => {
          const prev = i ? fSteps[i - 1][1] : 0;
          const p = i && prev ? Math.round((v / prev) * 100) : 0;
          return <div key={l} style={{ marginBottom: 14 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 6 }}>
              <span style={{ color: 'var(--muted)' }}>{l}</span>
              <span className="mono"><b>{num(v)}</b>
                {i ? <span style={{ color: 'var(--faint)' }}> · {p}%</span> : null}</span>
            </div>
            <Bar pct={(v / fTop) * 100} h={14}
              color={i === fSteps.length - 1 ? 'var(--price)' : 'var(--accent)'} />
          </div>;
        })}
        <div className="hint">конверсия расчёт → напечатано: {convTotal.toFixed(1)}%</div>
        {f ? <div className="hint" style={{ marginTop: 4 }}>
          за период: расчёт → заказ {f.calcs ? Math.round((f.orders / f.calcs) * 100) : 0}%
          {' · '}заказ → печать {f.orders ? Math.round((f.printed / f.orders) * 100) : 0}%
        </div> : null}
      </div>
    </div>

    <div className="dash-r2">
      <div className="card">
        <div className="lab">Причины отказов</div>
        <HBars rows={rejects} color="var(--red)" />
      </div>
      <div className="card">
        <div className="lab">Статусы заказов</div>
        <Donut parts={statuses} />
      </div>
      <div className="card">
        <div className="lab">Топ материалов, печатей</div>
        <HBars rows={mats} />
      </div>
    </div>

    {/* B7 (источник лида): откуда пришли заказы окна + новые vs вернувшиеся клиенты.
        Ключи source пишет сервер при оформлении (requests.source); «Не размечено» — старые
        заказы и пути без токена модели (индивидуальный, товар витрины). */}
    <div className="dash-r2">
      <div className="card">
        <div className="lab">Источники заказов · 30 дней</div>
        <HBars color="var(--violet)" rows={(s.sources_30d || []).map((r) => ({
          l: SRC[r.source] || r.source, v: r.orders,
        }))} />
      </div>
      <div className="card">
        <div className="lab">Клиенты · 30 дней</div>
        <div className="kpi2" style={{ flex: 1 }}>
          <div className="cell"><div className="k">Новые</div>
            <div className="v">{num((s.clients_30d || {}).new)}</div></div>
          <div className="cell"><div className="k">Вернувшиеся</div>
            <div className="v">{num((s.clients_30d || {}).returning)}</div></div>
        </div>
        <div className="hint">новый = первый заказ клиента внутри окна</div>
      </div>
      <div className="card">
        <div className="lab">Выручка по источникам · 30 дней</div>
        <HBars rows={(s.sources_30d || []).filter((r) => Number(r.revenue) > 0).map((r) => ({
          l: SRC[r.source] || r.source, v: r.revenue, d: money(r.revenue),
        }))} />
      </div>
    </div>
  </div>;
}

// Подписи источников заказа (requests.source, B7). Ключи пишет сервер — orders.place_from_token.
const SRC = {
  file: 'Файл', link: 'Ссылка', catalog: 'Каталог', mine: 'Свои модели',
  repeat: 'Повтор заказа', gridfinity: 'Конфигуратор', '—': 'Не размечено',
};

// Сетка Big Data задана инлайном, как в макете: 4 колонки равной высоты, свой скролл.
const BIG_GRID = {
  display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gridAutoRows: '1fr',
  gap: 20, flex: 1, minHeight: 0, overflowY: 'auto',
};
const BIG_V = { fontSize: 20 };

function BigData({ d }) {
  const s = d.samples || {};
  const inv = d.inventory || {};
  const cogs = d.cogs || {};
  const tot = d.totals || {};
  const col = d.collection || {};
  const jr = d.journal || {};   // журнал печатей мастера: брак по пластикам

  if (!s.count && !inv.active_spools && !jr.total) {
    return <Empty ic="bolt" t="Данных пока мало"
      ds="Big Data наполняется по мере накопления печатей" />;
  }

  return <div style={BIG_GRID}>
    <div className="card">
      <div className="lab">Точность оценки времени</div>
      <div className="kpi2" style={{ flex: 1 }}>
        <div className="cell"><div className="k">Выборка</div><div className="v" style={BIG_V}>{num(s.count)}</div></div>
        <div className="cell"><div className="k">Средняя ошибка</div><div className="v" style={BIG_V}>{(s.mae_min || 0).toFixed(1)} мин</div></div>
        <div className="cell"><div className="k">Оценка</div><div className="v" style={BIG_V}>{fmtMin(s.avg_est)}</div></div>
        <div className="cell"><div className="k">Факт</div><div className="v" style={BIG_V}>{fmtMin(s.avg_actual)}</div></div>
        <div className="cell"><div className="k">MAPE</div><div className="v" style={BIG_V}>{(s.mape_pct || 0).toFixed(1)}%</div></div>
        <div className="cell"><div className="k">В пределах 20%</div><div className="v" style={BIG_V}>{(s.within20_pct || 0).toFixed(1)}%</div></div>
      </div>
    </div>

    <div className="card">
      <div className="lab">Ошибка по материалам</div>
      <HBars color="var(--violet)" rows={(d.samples_by_material || []).map((m) => ({
        l: m.material, v: m.mae_min, d: (m.mae_min || 0).toFixed(1) + ' мин',
      }))} />
    </div>

    <div className="card">
      <div className="lab">Расход материалов</div>
      <HBars color="var(--violet)" rows={(d.materials || []).map((m) => ({
        l: m.material, v: m.grams, d: grams(m.grams),
      }))} />
    </div>

    <div className="card">
      <div className="lab">Склад</div>
      <div className="kpi2" style={{ flex: 1 }}>
        <div className="cell"><div className="k">Активных катушек</div><div className="v" style={BIG_V}>{num(inv.active_spools)}</div></div>
        <div className="cell"><div className="k">Остаток</div><div className="v" style={BIG_V}>{grams(inv.remaining_g)}</div></div>
        <div className="cell"><div className="k">С закупочной ценой</div><div className="v" style={BIG_V}>{num(inv.priced_spools)}</div></div>
        <div className="cell"><div className="k">Заканчивается / нет</div><div className="v" style={BIG_V}>{num(inv.low_colors)} / {num(inv.out_colors)}</div></div>
      </div>
    </div>

    <div className="card">
      <div className="lab">Себестоимость и маржа</div>
      <div className="kpi2" style={{ flex: 1 }}>
        <div className="cell"><div className="k">Выручка</div><div className="v" style={BIG_V}>{money(cogs.revenue)}</div></div>
        <div className="cell"><div className="k">Себестоимость</div><div className="v" style={BIG_V}>{money(cogs.cogs)}</div></div>
        <div className="cell"><div className="k">Маржа</div>
          <div className="v" style={{ ...BIG_V, color: 'var(--price)' }}>{money(cogs.margin)}</div></div>
        <div className="cell"><div className="k">Рентабельность</div><div className="v" style={BIG_V}>{(cogs.margin_pct || 0).toFixed(1)}%</div></div>
      </div>
      <div className="hint" style={{ marginTop: 10 }}>
        покрытие себестоимостью: {num(cogs.orders_with_cogs)} из {num(cogs.printed_orders)} ({(cogs.coverage_pct || 0).toFixed(0)}%)
      </div>
    </div>

    {jr.total > 0 ? <div className="card">
      <div className="lab">Журнал печатей · брак</div>
      <div className="kpi2">
        <div className="cell"><div className="k">Печатей в журнале</div><div className="v" style={BIG_V}>{num(jr.total)}</div></div>
        <div className="cell"><div className="k">Брак</div>
          <div className="v" style={{ ...BIG_V, color: 'var(--red)' }}>{num(jr.fail_pct)}% ({num(jr.fails)})</div></div>
        <div className="cell"><div className="k">Пластика учтено</div><div className="v" style={BIG_V}>{grams(jr.grams)}</div></div>
        <div className="cell"><div className="k">Времени печати</div><div className="v" style={BIG_V}>{fmtMin(jr.minutes)}</div></div>
      </div>
      {/* доля брака у сервера взвешена по граммам, поэтому колонка «n» — это число печатей, а не брака */}
      <div style={{ marginTop: 12 }}>
        <HBars color="var(--red)" rows={(jr.by_plastic || []).map((x) => ({
          l: x.plastic, v: x.n, d: num(x.n) + ' печ. · брак ' + num(x.fail_pct) + '%',
        }))} />
      </div>
      <div className="hint" style={{ marginTop: 10 }}>
        в брак ушло {grams(jr.waste)}; источник — «Журнал печатей», брак считается только
        по оценённым записям{jr.ungraded ? ` (без оценки: ${num(jr.ungraded)})` : ''}
      </div>
    </div> : null}

    <div className="card">
      <div className="lab">Каталог</div>
      <div className="kpi2">
        <div className="cell"><div className="k">Моделей</div><div className="v" style={BIG_V}>{num(col.models)}</div></div>
        <div className="cell"><div className="k">Расчётов по ним</div><div className="v" style={BIG_V}>{num(col.total_uses)}</div></div>
        <div className="cell"><div className="k">Сохранено клиентами</div><div className="v" style={BIG_V}>{num(tot.saved_models)}</div></div>
      </div>
      <div style={{ marginTop: 12 }}>
        <HBars color="var(--violet)" rows={(d.top_models || []).map((m) => ({ l: m.label, v: m.uses }))} />
      </div>
    </div>
  </div>;
}
