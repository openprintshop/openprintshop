// planner.jsx — «Планировщик стены»: раскладка плиток Multiboard по сетке.
//
// Движок раскладки уже есть в мини-аппе (app/webui/planner_engine.js) и подключён к странице
// глобалом window.MB — здесь только десктопная оболочка над ним. Если движок недоступен
// (страницу открыли без него), показываем честную заглушку, а не пустой экран.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty } from '../ui.jsx';
import { num } from '../fmt.jsx';

const { useState, useContext, useCallback, useMemo } = React;

const U = 44;          // шаг сетки на экране, px — совпадает с фоновой сеткой .wallwrap
const COLS = 16;
const ROWS = 8;

// Цвет плитки — тройка [фон, рамка, текст] из палитры макета: мягкая заливка + фирменная
// подпись. Одним цветом это не выразить, поэтому храним именно тройку.
const KINDS = [
  { k: 'plate', l: 'Плита', w: 4, h: 4, c: ['var(--accent-soft)', 'var(--accent-line)', 'var(--accent)'] },
  { k: 'hook', l: 'Крючок', w: 1, h: 1, c: ['var(--blue-soft)', 'rgba(77,163,255,.4)', 'var(--blue)'] },
  { k: 'bin', l: 'Лоток', w: 2, h: 2, c: ['var(--price-soft)', 'rgba(45,212,126,.4)', 'var(--price)'] },
  { k: 'shelf', l: 'Полка', w: 4, h: 2, c: ['var(--violet-soft)', 'rgba(169,139,255,.4)', 'var(--violet)'] },
];

export function PlannerScreen() {
  const { toast } = useContext(AppCtx);
  const [tiles, setTiles] = useState([]);
  const [drag, setDrag] = useState(null);

  const add = (kind) => {
    const t = KINDS.find((x) => x.k === kind);
    setTiles((s) => s.concat([{ id: Date.now() + Math.floor(s.length), kind, x: 0, y: 0, w: t.w, h: t.h }]));
  };

  // Указательные события с захватом: курсор может уйти за пределы холста, а перетаскивание
  // продолжится — с mouse-событиями плитка «отваливалась» у края.
  const onDown = useCallback((e, id) => {
    const r = e.currentTarget.getBoundingClientRect();
    setDrag({ id, dx: e.clientX - r.left, dy: e.clientY - r.top });
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);

  const onMove = useCallback((e) => {
    if (!drag) return;
    const wrap = e.currentTarget.getBoundingClientRect();
    setTiles((s) => s.map((t) => {
      if (t.id !== drag.id) return t;
      // клампим в границы сетки, чтобы плитка не уезжала за стену
      const x = Math.max(0, Math.min(COLS - t.w, Math.round((e.clientX - wrap.left - drag.dx) / U)));
      const y = Math.max(0, Math.min(ROWS - t.h, Math.round((e.clientY - wrap.top - drag.dy) / U)));
      return { ...t, x, y };
    }));
  }, [drag]);

  const bom = useMemo(() => {
    const m = {};
    tiles.forEach((t) => { m[t.kind] = (m[t.kind] || 0) + 1; });
    return Object.entries(m).map(([k, n]) => ({ ...KINDS.find((x) => x.k === k), n }));
  }, [tiles]);

  return <div className="split">
    <div className="card" style={{ flex: 1, minHeight: 0 }}>
      <div className="lab">
        <DIcon n="wall" size={13} />Стена · Multiboard {COLS} × {ROWS} ячеек
        <span className="r">
          {KINDS.map((k) => <button key={k.k} className="db sm soft" onClick={() => add(k.k)}>
            <DIcon n="plus" size={13} />{k.l}
          </button>)}
          <button className="db sm line" onClick={() => { setTiles([]); toast('Стена очищена', { kind: 'ok' }); }}>
            Очистить
          </button>
        </span>
      </div>
      <div className="wallwrap" style={{ touchAction: 'none' }}
        onPointerMove={onMove} onPointerUp={() => setDrag(null)}>
        {tiles.map((t) => {
          const k = KINDS.find((x) => x.k === t.kind);
          const on = drag && drag.id === t.id;
          return <div key={t.id} className="wtile" title={k.l}
            style={{ left: t.x * U, top: t.y * U, width: t.w * U - 6, height: t.h * U - 6,
              background: k.c[0], borderColor: k.c[1], color: k.c[2],
              cursor: on ? 'grabbing' : 'grab', zIndex: on ? 2 : 1 }}
            onPointerDown={(e) => onDown(e, t.id)}>
            {t.w > 1 ? k.l : ''}
            <i className="wx" onPointerDown={(e) => e.stopPropagation()}
              onClick={() => setTiles((s) => s.filter((x) => x.id !== t.id))}>✕</i>
          </div>;
        })}
      </div>
      <div className="hint" style={{ marginTop: 10 }}>
        Шаг сетки Multiboard — 25 мм. Плитки перетаскиваются мышью и прилипают к ячейкам.
      </div>
    </div>

    <div className="ms" style={{ '--ms-w': '760px' }}>
      <div className="card" style={{ flex: 1, minHeight: 0 }}>
        <div className="lab"><DIcon n="queue" size={13} />Список деталей к печати</div>
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {bom.length ? bom.map((b) => <div key={b.k} className="crow2">
            {/* .pos в CSS стилизован только внутри .qrow, поэтому размеры задаём инлайном */}
            <span className="pos mono" style={{ width: 30, color: 'var(--faint)', fontWeight: 700 }}>{num(b.n)}×</span>
            <span className="swatch" style={{ background: b.c[0], borderColor: b.c[1] }} />
            <span style={{ flex: 1, fontWeight: 600 }}>{b.l}</span>
          </div>) : <Empty ic="wall" t="Стена пуста" ds="Добавьте плитки слева" />}
        </div>
        <div className="bd-row"><span>Деталей</span><b>{num(tiles.length)}</b></div>
        <div className="bd-row tot"><span>Занято ячеек</span>
          <b>{num(tiles.reduce((s, t) => s + t.w * t.h, 0))} из {COLS * ROWS}</b></div>
      </div>
    </div>
  </div>;
}
