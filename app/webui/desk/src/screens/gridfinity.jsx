// gridfinity.jsx — блок «Раскладка органайзера» в карточке заявки мастера.
//
// Клиент собирает ящик неделями и заказывает ПАРТИЯМИ, поэтому заявка из конфигуратора —
// это не «вся раскладка», а несколько деталей из проекта. Мастеру нужны две вещи, которых
// в тексте описания нет:
//
//   * ПЛАН ящика: где эти детали стоят и что вокруг них уже напечатано (бледные клетки —
//     из прошлых партий). По нему сразу видно нагрузку и посадку;
//   * ПЕЧАТЬ-ЛИСТ: строка на деталь с габаритами, цветом, расходом и отметкой «готово».
//     Отметка — единственное новое действие мастера: она шлёт клиенту уведомление
//     «Лоток «Под лопатки» готов · 1 из 3».
//
// Снимок конфигурации лежит в БД с первого дня (gridfinity_order_configs) — здесь он
// наконец читается. Заявка без снимка (обычный заказ) отдаёт 404, и блок не рисуется.
import { DIcon } from '../icons.jsx';
import { Sec } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { grams, minutes } from '../fmt.jsx';

const { useState } = React;

const COLORS = ['#ff7a1a', '#3d4a5e', '#3fb6c9', '#2dd47e', '#a98bff', '#d4b183'];

/** План ящика CSS-сеткой: детали ЭТОЙ партии в цвете, остальные — бледные. */
function Plan({ preview, only }) {
  if (!preview || !preview.cols) return null;
  return <div style={{
    display: 'grid', gap: 2, padding: 3, borderRadius: 9,
    background: 'var(--bg-2, #0d1214)', border: '1px solid var(--line)',
    gridTemplateColumns: `repeat(${preview.cols}, minmax(0,1fr))`,
    gridTemplateRows: `repeat(${preview.rows}, minmax(0,1fr))`,
    aspectRatio: `${preview.cols}/${preview.rows}`,
  }}>
    {(preview.mods || []).filter((m) => !m.wing).map((m, i) => {
      // Пустой список подсветки означает «вся раскладка наша» (заявка старого образца),
      // а не «ничего не наше»: иначе мастер видел бы полностью выцветший план.
      const mine = !only || !only.length || only.includes(m.id);
      return <i key={i} style={{
        gridColumn: `${(m.x || 0) + 1}/span ${m.w || 1}`,
        gridRow: `${(m.y || 0) + 1}/span ${m.d || 1}`,
        background: COLORS[(m.c || 0) % COLORS.length],
        opacity: mine ? 1 : 0.28, borderRadius: 3, display: 'block',
      }} />;
    })}
  </div>;
}

/** Строка печать-листа с отметкой «напечатано». */
function PartRow({ p, n, onMark }) {
  const [busy, setBusy] = useState(false);
  const done = p.live_state === 'printed';
  return <div style={{
    display: 'grid', gridTemplateColumns: '22px 34px 1fr auto', gap: 10,
    alignItems: 'center', padding: '8px 10px', borderRadius: 11,
    border: '1px solid var(--line)', background: 'var(--surface)', opacity: busy ? 0.6 : 1,
  }}>
    <input type="checkbox" checked={done} disabled={busy}
      style={{ width: 16, height: 16, accentColor: 'var(--accent)', cursor: 'pointer' }}
      onChange={async (e) => {
        setBusy(true);
        try { await onMark(p.id, e.target.checked); } finally { setBusy(false); }
      }} />
    <span style={{ height: 28, borderRadius: 6, background: p.color_hex, display: 'block' }} />
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 13.5, fontWeight: 600 }}>
        {n}. {p.name}{p.qty > 1 ? <span style={{ color: 'var(--muted)', fontWeight: 400 }}> ×{p.qty}</span> : null}
        {p.version > 1 ? <span style={{ color: 'var(--accent)', fontWeight: 400 }}> v{p.version}</span> : null}
        {/* Клиент правил деталь ПОСЛЕ заказа: размеры в листе — заказанные, но знать об
            этом мастер обязан до того, как встанет к принтеру. */}
        {p.changed ? <span style={{ color: 'var(--amber)', fontWeight: 400, fontSize: 11.5 }}>
          {' '}· клиент правил после заказа
        </span> : null}
      </div>
      <div className="mono hint" style={{ fontSize: 11, marginTop: 2 }}>
        {Math.round(p.w_mm)}×{Math.round(p.d_mm)}×{Math.round(p.h_mm)} мм · {p.color} · {grams(p.grams)} · {minutes(p.minutes)}
        {p.cells ? ` · ${p.cells} яч` : ''}{p.gen_label ? ` · ${p.gen_label}` : ''}
        {p.note ? ` · кладут: ${p.note}` : ''}
      </div>
    </div>
    <span className="mono hint" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>
      {done ? 'готово' : p.state_label}
    </span>
  </div>;
}

export function GfxSec({ o, toast }) {
  const d = useApi('adminGridfinity', [o.id], [o.id]);
  const [tick, setTick] = useState(0);
  // 404 «снимка нет» — обычная заявка: блок просто не рисуется, ошибку не показываем.
  if (!d.data || !d.data.ok) return null;
  const g = d.data;
  const parts = g.parts || [];
  const mine = parts.map((p) => p.mod_id).filter(Boolean);
  const others = Math.max(0, ((g.preview && g.preview.mods) || []).length - mine.length);
  const totG = parts.reduce((a, p) => a + (p.grams || 0), 0);
  const totM = parts.reduce((a, p) => a + (p.minutes || 0), 0);
  const done = parts.filter((p) => p.live_state === 'printed').length;

  async function mark(partId, printed) {
    try {
      await call('adminGfxPartPrinted', partId, printed);
      d.reload();
      setTick(tick + 1);
      if (printed) toast('Клиенту ушло уведомление о готовой детали');
    } catch (e) {
      toast(e.message || 'Не получилось отметить');
    }
  }

  return <Sec t={<span><DIcon n="layers" size={13} /> Раскладка органайзера</span>} span2>
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,260px) minmax(0,1fr)', gap: 18, alignItems: 'start' }}>
      <div style={{ display: 'grid', gap: 8 }}>
        <Plan preview={g.preview} only={mine} />
        <div className="mono hint" style={{ fontSize: 11, lineHeight: 1.7, whiteSpace: 'pre-line' }}>
          {(g.summary || '').split('\n').slice(0, 2).join('\n')}
          {others > 0 ? `\nбледные (${others}) — детали проекта из других партий` : ''}
        </div>
      </div>
      <div style={{ display: 'grid', gap: 7, minWidth: 0 }}>
        {parts.length
          ? <>
            <div className="hint" style={{ textTransform: 'uppercase', letterSpacing: '.08em', fontSize: 10 }}>
              Печать-лист · {done} из {parts.length} готово
            </div>
            {parts.map((p, i) => <PartRow key={p.id} p={p} n={i + 1} onMark={mark} />)}
            <div className="mono hint" style={{ fontSize: 11, borderTop: '1px dashed var(--line)', paddingTop: 8 }}>
              Итого {grams(totG)} · {minutes(totM)} · деталей {parts.length}
              {g.money && g.money.price ? ` · оценка ≈ ${g.money.price}` : ''}
            </div>
          </>
          : <div className="hint" style={{ whiteSpace: 'pre-wrap', fontSize: 12.5, lineHeight: 1.6 }}>
            {g.summary || 'Снимок раскладки без спецификации (заявка старого образца).'}
          </div>}
      </div>
    </div>
  </Sec>;
}
