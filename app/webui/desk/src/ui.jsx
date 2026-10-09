// ui.jsx — общие примитивы настольной админки. Порт dd-ui.jsx из хендоффа на ES-модули.
// React берётся глобалом из vendor/react.production.min.js (подключается в index.html до бандла).

import { DIcon } from './icons.jsx';
import { ST } from './statuses.jsx';

const { useEffect, useLayoutEffect, useRef, useState, useContext } = React;

export const AppCtx = React.createContext({});

export function StBadge({ st }) {
  const s = ST[st] || { l: st, c: 'gray' };
  return <span className={'stb ' + s.c}>{s.l}</span>;
}

export const fmt = (v) => Number(v || 0).toLocaleString('ru-RU');

// Здесь был компонент Money с ЗАШИТЫМ «₽». Его не звал никто (esbuild выбрасывал его из
// сборки), но он лежал заряженным ружьём: следующий, кто потянулся бы за готовым «выводом
// денег», получил бы рубль в мастерской, торгующей за белорусские рубли. Единый источник
// валюты — curSym() в fmt.jsx, туда и идти.

export function Spark({ data, w = 120, h = 34, color = 'var(--accent)' }) {
  if (!data || data.length < 2) return null;
  const mn = Math.min(...data), mx = Math.max(...data), sp = mx - mn || 1;
  const P = data.map((v, i) => ({ x: (i / (data.length - 1)) * w, y: h - 3 - ((v - mn) / sp) * (h - 6) }));
  // Гладкая волнистая линия, как в мобильной админке: Catmull-Rom сплайн через точки →
  // кубические безье. Ломаная polyline давала острые углы на каждом замере температуры.
  let d = `M${P[0].x.toFixed(1)},${P[0].y.toFixed(1)}`;
  for (let i = 0; i < P.length - 1; i++) {
    const p0 = P[i - 1] || P[i], p1 = P[i], p2 = P[i + 1], p3 = P[i + 2] || p2;
    const c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6;
    const c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
  }
  // w — координатное пространство, а не жёсткая ширина: фиксированный width вылезал из ячейки
  // телеметрии, когда колонка уже графика (жалоба «график вылазит из карточки»). viewBox +
  // width:100% вписывают линию в ячейку любой ширины; non-scaling-stroke держит толщину 2px.
  return <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
    style={{ display: 'block', width: '100%', maxWidth: '100%', height: h }}>
    <path d={d} fill="none" stroke={color} strokeWidth="2" vectorEffect="non-scaling-stroke"
      strokeLinejoin="round" strokeLinecap="round" />
  </svg>;
}

// id градиента уникален на экземпляр: несколько Area на одной странице иначе делят заливку
let areaSeq = 0;

/**
 * Фактическая высота элемента в пикселях: возвращает [ref, h], до первого замера h = 0.
 * Графику мало `height:100%`: у Area viewBox по вертикали совпадает с пиксельной высотой
 * один в один, и от тех же чисел считаются крестовина, точка и подсказка под курсором —
 * значит высоту надо знать числом, а не отдавать растяжение браузеру.
 */
export function useBoxHeight() {
  const ref = useRef(null);
  const [h, setH] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => setH(Math.round(el.getBoundingClientRect().height));
    read();
    // Сборка таргетит chrome110, где ResizeObserver есть всегда; проверка — на случай
    // вебвью постарше: там график просто останется с высотой первого замера.
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, h];
}

/**
 * Индекс точки под курсором. t — доля ширины графика (0..1), n — сколько точек.
 * Округляем к БЛИЖАЙШЕЙ точке, а не режем ширину на n равных корзин: линия идёт от края
 * до края, крайние точки стоят ровно на границах — корзинами их не поймать.
 */
export function hoverIndex(t, n) {
  if (!(n > 0)) return -1;
  if (n === 1) return 0;
  return Math.min(n - 1, Math.max(0, Math.round(Math.min(1, Math.max(0, t)) * (n - 1))));
}

/** Индекс столбика под курсором: столбики делят ширину поровну — здесь корзина, а не ближайший. */
export function barIndex(t, n) {
  if (!(n > 0)) return -1;
  return Math.min(n - 1, Math.max(0, Math.floor(Math.min(0.999999, Math.max(0, t)) * n)));
}

/**
 * Подсказка графика: абсолютно позиционируется внутри обёртки `.chwrap`.
 * x — доля ширины (0..1), y — координата якоря по вертикали (число = px, строка = как есть).
 * У краёв прижимаем подсказку к краю: посередине она вылезала бы за карточку, а у дашборда
 * контейнер со скроллом — вылет добавил бы горизонтальную полосу прокрутки.
 * below — рисовать под якорем (когда сверху уже нет места).
 */
export function ChartTip({ x, y, t, v, below }) {
  const pct = Math.min(1, Math.max(0, x)) * 100;
  const ax = pct < 16 ? 'translateX(-6px)'
    : pct > 84 ? 'translateX(calc(-100% + 6px))' : 'translateX(-50%)';
  const ay = below ? 'translateY(9px)' : 'translateY(calc(-100% - 9px))';
  return <div className="chtip" style={{ left: pct + '%', top: y, transform: ax + ' ' + ay }}>
    {t ? <span className="k">{t}</span> : null}<b>{v}</b>
  </div>;
}

/**
 * Площадная диаграмма. labels/fmtV включают подсказку под курсором: без неё по графику
 * читается только форма, а конкретные цифры дня взять негде.
 *
 * h — высота в пикселях; fill вместо неё берёт фактическую высоту обёртки. Константа
 * годится, когда график стоит в потоке (Пульт), но не когда карточка делит высоту между
 * заголовком, графиком, подписями оси и сноской: на низком окне (1920×1080) фиксированные
 * 330 px не влезали и график рисовался поверх подписей.
 */
export function Area({ data, h = 150, fill, color = 'var(--price)', labels, fmtV }) {
  const gid = React.useMemo(() => 'ag' + (++areaSeq), []);
  const [hi, setHi] = useState(-1);
  const [box, boxH] = useBoxHeight();
  const H = fill ? boxH : h;
  if (!data || data.length < 2) return null;
  const w = 100, mn = 0, mx = Math.max(...data), sp = mx - mn || 1;
  // Поля под линией и над ней — те же 6 и 24 px, что и всегда, пока карточка нормальной
  // высоты. На низкой они ужимаются пропорционально: при жёстких 30 px амплитуда (H-30)
  // на коротком графике уходит в минус и он рисуется вверх ногами.
  const pb = Math.min(6, H * 0.04), amp = Math.max(1, H - pb - Math.min(24, H * 0.16));
  const P = data.map((v, i) => [(i / (data.length - 1)) * w, H - pb - ((v - mn) / sp) * amp]);
  const line = P.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const at = hi >= 0 && hi < data.length ? hi : -1;
  // индекс считаем от ширины ЭЛЕМЕНТА: viewBox растянут (preserveAspectRatio=none),
  // поэтому координаты svg с экранными не совпадают
  const pick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHi(hoverIndex(r.width ? (e.clientX - r.left) / r.width : 0, data.length));
  };
  // H>0 — первый кадр в режиме fill идёт до замера; svg с нулевым viewBox не рисуется,
  // а крестовина с подсказкой встали бы не туда.
  return <div className="chwrap" ref={box} style={{ height: fill ? '100%' : h }}
    onMouseMove={pick} onMouseLeave={() => setHi(-1)}>
    {H > 0 ? <svg viewBox={`0 0 ${w} ${H}`} preserveAspectRatio="none"
      style={{ width: '100%', height: H, display: 'block' }}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity=".28" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={line + ` L ${w} ${H} L 0 ${H} Z`} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
    </svg> : null}
    {at >= 0 && H > 0 ? <>
      <span className="chcross" style={{ left: P[at][0] + '%' }} />
      <span className="chdot" style={{ left: P[at][0] + '%', top: P[at][1], background: color }} />
      <ChartTip x={P[at][0] / 100} y={P[at][1]} below={P[at][1] < 44}
        t={labels && labels[at]} v={fmtV ? fmtV(data[at]) : fmt(data[at])} />
    </> : null}
  </div>;
}

export function Bar({ pct, color = 'var(--accent)', h = 9 }) {
  const isGrad = String(color).startsWith('linear');
  return <div className="pbar" style={{ height: h }}>
    <i style={{
      width: Math.max(0, Math.min(100, pct)) + '%',
      background: isGrad ? color : undefined,
      backgroundColor: isGrad ? undefined : color,
    }} />
  </div>;
}

export function Field({ l, children }) {
  return <div style={{ minWidth: 0 }}><label className="f-lab">{l}</label>{children}</div>;
}

/**
 * Блок с заголовком. По умолчанию это .sec — вложенная панель карточки заказа.
 * card=true даёт .card из макета: самостоятельная карточка на своём экране, а не подпанель.
 * ic — иконка в .lab: в макете заголовки карточек почти всегда с иконкой.
 */
export function Sec({ t, ic, right, children, span2, card, style }) {
  return <div className={(card ? 'card' : 'sec') + (span2 ? ' span2' : '')} style={style}>
    <div className="lab">{ic ? <DIcon n={ic} size={13} /> : null}{t}{right && <span className="r">{right}</span>}</div>
    {children}
  </div>;
}

export function Drawer({ title, sub, onClose, children, footer }) {
  useEffect(() => {
    const f = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', f);
    return () => window.removeEventListener('keydown', f);
  }, [onClose]);
  return <>
    <div className="dim" onClick={onClose}></div>
    <div className="drawer">
      <div className="dw-h">
        <div style={{ flex: 1 }}>
          <div className="tt">{title}</div>
          {sub && <div className="ds">{sub}</div>}
        </div>
        <button className="ib" onClick={onClose}><DIcon n="close" /></button>
      </div>
      <div className="dw-b">{children}</div>
      {footer && <div className="dw-f">{footer}</div>}
    </div>
  </>;
}

export function ConfirmModal({ q, onClose }) {
  if (!q) return null;
  return <div className="modal" onClick={onClose}>
    <div className="mbox" onClick={(e) => e.stopPropagation()}>
      <div className="mt">{q.title}</div>
      <div className="mx">{q.text}</div>
      {/* Цвет кнопки = смысл действия: красная — необратимое (удалить, остановить),
          зелёная (tone:'ok') — позитивное подтверждение, оранжевая — обычное действие. */}
      <div className="mrow">
        <button className="db line" onClick={onClose}>Отмена</button>
        <button className={'db ' + (q.danger ? 'danger' : (q.tone === 'ok' ? 'good' : ''))}
          onClick={() => { q.onYes && q.onYes(); onClose(); }}>{q.cta || 'Подтвердить'}</button>
      </div>
    </div>
  </div>;
}

// Тон текста по типу тоста: раньше текст неудачи был того же цвета, что и текст успеха —
// ошибка читалась как обычное уведомление. Теперь текст окрашен по смыслу.
const TOAST_TONE = { ok: 'var(--price)', warn2: 'var(--amber)', err: 'var(--red)' };

export function ToastHost({ toasts, dismiss }) {
  return <div className="toasts">{toasts.map((t) =>
    <div key={t.id} className={'toast ' + (t.kind || '')}>
      {t.kind === 'ok' && <DIcon n="check" style={{ color: 'var(--price)', flexShrink: 0 }} />}
      {t.kind === 'warn2' && <DIcon n="bell" style={{ color: 'var(--amber)', flexShrink: 0 }} />}
      {t.kind === 'err' && <DIcon n="flame" style={{ color: 'var(--red)', flexShrink: 0 }} />}
      <div style={{ flex: 1, minWidth: 0, color: TOAST_TONE[t.kind] || undefined }}>
        {t.msg}
        {t.sub && <div style={{ fontSize: 12.5, color: 'var(--faint)', marginTop: 3 }}>{t.sub}</div>}
      </div>
      {t.action && <button className="act" onClick={() => { t.onAction && t.onAction(); dismiss(t.id); }}>{t.action}</button>}
      <button className="act" style={{ color: 'var(--faint)', marginLeft: t.action ? 4 : 'auto' }}
        onClick={() => dismiss(t.id)}>✕</button>
    </div>)}
  </div>;
}

/** Загрузить картинку в <img> — общий шаг для «копировать» и «скачать». */
function loadImage(src) {
  return new Promise((res, rej) => {
    const im = new Image();
    im.onload = () => res(im);
    im.onerror = () => rej(new Error('картинка не загрузилась'));
    im.src = src;
  });
}

/**
 * Положить картинку в буфер обмена именно КАРТИНКОЙ (чтобы вставилась в Paint, Telegram,
 * Word), а не ссылкой на неё.
 *
 * Буфер через браузер принимает только image/png, а у нас в ходу и jpeg (фото результата),
 * и data:-строки (вложения чата), поэтому что бы ни пришло — перерисовываем в PNG канвой.
 * Картинки все свои, с 127.0.0.1, так что канва не «протухает» (tainted) и toBlob работает.
 */
export async function copyImage(src) {
  if (!navigator.clipboard || !window.ClipboardItem) {
    throw new Error('буфер обмена недоступен в этом окне');
  }
  const im = await loadImage(src);
  const cv = document.createElement('canvas');
  cv.width = im.naturalWidth || im.width;
  cv.height = im.naturalHeight || im.height;
  cv.getContext('2d').drawImage(im, 0, 0);
  const blob = await new Promise((res) => cv.toBlob(res, 'image/png'));
  if (!blob) throw new Error('не удалось перекодировать картинку');
  await navigator.clipboard.write([new window.ClipboardItem({ 'image/png': blob })]);
}

/** Скачать картинку файлом. Имя берём из адреса — у data:-вложений его нет, зовём photo.png. */
function downloadImage(src) {
  const a = document.createElement('a');
  a.href = src;
  const name = /^data:/.test(src) ? 'photo.png' : (src.split('?')[0].split('/').pop() || 'photo');
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function Lightbox({ src, onClose }) {
  // Своё контекстное меню: окно админки — это WebView2, и родного меню «Копировать
  // изображение» в нём может не быть вовсе. Плюс мы кладём в буфер PNG, а не ссылку.
  const [menu, setMenu] = useState(null);
  const { toast } = useContext(AppCtx);

  useEffect(() => { setMenu(null); }, [src]);

  if (!src) return null;

  const act = async (fn, okMsg) => {
    setMenu(null);
    try {
      await fn();
      if (okMsg && toast) toast(okMsg, { kind: 'ok', ms: 2500 });
    } catch (e) {
      if (toast) toast('Не скопировалось: ' + e.message, { kind: 'warn2' });
    }
  };

  return <div className="modal" style={{ background: 'rgba(3,5,8,.85)', zIndex: 70 }}
    onClick={onClose}
    onContextMenu={(e) => { e.preventDefault(); setMenu(null); }}>
    <img src={src} alt="" style={{ maxWidth: '86%', maxHeight: '88%', borderRadius: 16, boxShadow: '0 40px 120px rgba(0,0,0,.7)' }}
      // Клик по картинке закрывает просмотр, как и раньше; но если открыто меню —
      // первый клик просто убирает его, иначе меню исчезало бы вместе с самой картинкой.
      onClick={(e) => { if (menu) { e.stopPropagation(); setMenu(null); } }}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
        // Меню держим в пределах окна: у правого края оно иначе уезжает за экран.
        setMenu({ x: Math.min(e.clientX, window.innerWidth - 270), y: Math.min(e.clientY, window.innerHeight - 170) });
      }} />
    <button className="ib" style={{ position: 'absolute', top: 26, right: 30 }} onClick={onClose}><DIcon n="close" /></button>
    {menu && <div className="menu" style={{ position: 'fixed', top: menu.y, left: menu.x, right: 'auto', zIndex: 80 }}
      onClick={(e) => e.stopPropagation()}>
      <button onClick={() => act(() => copyImage(src), 'Картинка скопирована')}>
        <DIcon n="copy" size={16} />Копировать
      </button>
      {!/^data:/.test(src) ? <button onClick={() => act(
        () => navigator.clipboard.writeText(new URL(src, window.location.href).href),
        'Ссылка скопирована')}>
        <DIcon n="link" size={16} />Копировать ссылку
      </button> : null}
      <button onClick={() => act(() => downloadImage(src))}>
        <DIcon n="doc" size={16} />Сохранить как…
      </button>
    </div>}
  </div>;
}

export function Empty({ ic = 'search', t = 'Ничего не найдено', ds, cta, onCta }) {
  return <div style={{ padding: '46px 20px', textAlign: 'center', color: 'var(--faint)', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
    <DIcon n={ic} size={30} style={{ opacity: 0.6, verticalAlign: 0 }} />
    <div style={{ fontSize: 14.5, fontWeight: 600, color: 'var(--muted)' }}>{t}</div>
    {ds && <div style={{ fontSize: 13 }}>{ds}</div>}
    {cta && <button className="db soft sm" onClick={onCta} style={{ marginTop: 4 }}>{cta}</button>}
  </div>;
}
