// fmt.jsx — форматирование. Валюта приходит из ответов бэкенда (поле currency), в разных
// эндпоинтах это 'руб.' или 'Br' — поэтому она параметр, а не константа.

export const CUR = { v: 'руб.' };

/** Запомнить валюту из ответа API, чтобы экраны не тащили её через все пропсы. */
export function rememberCurrency(res) {
  if (res && res.currency) CUR.v = res.currency;
  return res;
}

/**
 * Единственный источник символа валюты для подписей деска (заголовки полей, плейсхолдеры,
 * шапки таблиц). Раньше в них был зашит '₽', и у студии на BYN подпись расходилась со
 * значением рядом, которое money() уже печатал с CUR.v. Вызывать в теле рендера: CUR.v
 * меняется, когда приезжает adminHub, и функция отдаёт свежее значение на каждый рендер.
 *
 * Имя намеренно НЕ 'cur': в экранах деска `const cur = list.find(...)` — расхожая локальная
 * переменная (plastics.jsx, products.jsx, orders.jsx), и импорт с таким именем молча
 * перекрывался бы ею внутри компонента.
 */
export const curSym = () => CUR.v;

export const num = (v) => Number(v || 0).toLocaleString('ru-RU');

export function money(v, opts = {}) {
  const n = Number(v || 0);
  const s = n.toLocaleString('ru-RU', { minimumFractionDigits: opts.frac ?? 0, maximumFractionDigits: 2 });
  return (opts.sign && n > 0 ? '+' : '') + s + ' ' + (opts.cur || CUR.v);
}

export function grams(v) {
  const n = Number(v || 0);
  return (n >= 100 ? Math.round(n) : Math.round(n * 10) / 10).toLocaleString('ru-RU') + ' г';
}

/** Минуты → «5 ч 20 м». */
export function minutes(m) {
  const t = Math.max(0, Math.round(Number(m) || 0));
  const h = Math.floor(t / 60), mm = t % 60;
  if (!h) return mm + ' м';
  return mm ? `${h} ч ${mm} м` : `${h} ч`;
}

const MON = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

/** Unix-секунды → «08.07 14:22». Бэкенд отдаёт время в секундах. */
export function dt(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getDate())}.${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function dateOnly(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  return `${d.getDate()} ${MON[d.getMonth()]}`;
}

/** «5 мин назад» из unix-секунд. Часть эндпоинтов отдаёт готовый ago — он в приоритете. */
export function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (s < 60) return 'только что';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} ${plural(m, 'минуту', 'минуты', 'минут')} назад`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ${plural(h, 'час', 'часа', 'часов')} назад`;
  const d = Math.floor(h / 24);
  if (d < 31) return `${d} ${plural(d, 'день', 'дня', 'дней')} назад`;
  const mo = Math.floor(d / 30);
  return `${mo} ${plural(mo, 'месяц', 'месяца', 'месяцев')} назад`;
}

export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

/** «12 заказов» с правильным окончанием. */
export const cnt = (n, one, few, many) => `${num(n)} ${plural(n, one, few, many)}`;
