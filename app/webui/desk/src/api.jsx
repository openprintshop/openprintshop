// api.jsx — доступ к бэкенду. Обёртка над window.API из app/webui/api.js.
//
// Свой fetch писать НЕЛЬЗЯ: авторизация зашита в форму запроса (GET — initData в query,
// POST — в теле), любой самодельный запрос получит 403 на каждом эндпоинте. Поэтому здесь
// только тонкий слой поверх готовых 143 методов: единая обработка ошибок, загрузка и поллинг.

import { AppCtx } from './ui.jsx';

const { useState, useEffect, useRef, useCallback, useContext } = React;

export const API = window.API || {};

// Признак MOCK-режима: api.js молча подменяет ВСЕ ответы выдуманными, если нет ни
// Telegram initData, ни токена ПК-админки. Снаружи это никак не видно — админка выглядит
// рабочей и показывает несуществующие заказы. Поэтому определяем сами и показываем баннер.
export function isMock() {
  try {
    if (localStorage.getItem('pcb_admin_token')) return false;
  } catch (e) { /* localStorage может быть недоступен */ }
  const tg = window.Telegram && window.Telegram.WebApp;
  return !(tg && tg.initData);
}

// Ответы бота имеют вид {ok: true, ...} либо {ok: false, error: '...'}.
// api.js статус HTTP не проверяет вообще — 500 прилетит сюда как объект без ok.
function unwrap(res, what) {
  if (!res || typeof res !== 'object') throw new Error(`${what}: пустой ответ сервера`);
  if (res.ok === false) {
    // Отказ с описанием ПРИНТЕРА — не ошибка связи, а честный ответ «этот принтер так не умеет»
    // (цикл 3, волна 3: телеметрию отдаёт не всякая машина). Бросить его значило бы выкинуть
    // тело вместе с `driver`, и экран снова рисовал бы «Нет связи с принтером» — то есть
    // серверная половина волны до интерфейса физически не доезжала.
    if (res.driver) return res;
    throw new Error(res.error || `${what}: сервер отказал`);
  }
  return res;
}

/** Разовый вызов метода API с разбором ошибки. */
export async function call(method, ...args) {
  const fn = API[method];
  if (typeof fn !== 'function') throw new Error(`Метод API «${method}» не найден`);
  let res;
  try {
    res = await fn(...args);
  } catch (e) {
    throw new Error(`${method}: нет связи с ботом (${e && e.message ? e.message : e})`);
  }
  return unwrap(res, method);
}

/**
 * Загрузка данных экрана.
 * Возвращает {data, err, loading, reload} — экран сам решает, что рисовать.
 * deps — как у useEffect: смена перезапускает загрузку.
 *
 * opts:
 *   auto — период тихого автообновления в мс (0 — только по требованию). «Тихо» значит
 *          без loading=true и без обнуления data: иначе экран мигал бы скелетом каждые
 *          несколько секунд, а короткий перезапуск бота стирал бы уже показанные данные.
 *   skip — не загружать вовсе. Нужно спискам, которые открываются по клику (вкладка
 *          «Для себя»): пока их не попросили, лишний запрос к боту не нужен.
 */
export function useApi(method, args = [], deps = [], opts = {}) {
  const { auto = 0, skip = false } = opts;
  // loading=true и у пропущенной (skip) загрузки: её состояние никто не рисует, пока она
  // пропущена, зато в момент включения экран сразу показывает спиннер, а не «нет данных».
  const [state, setState] = useState({ data: null, err: null, loading: true });
  const seq = useRef(0);
  const argsRef = useRef(args);
  argsRef.current = args;
  // Автообновление в фоновой вкладке — пустая трата: бот и вебапп в одном event loop.
  // tabVisible кладёт TabView; вне вкладок (палитра, оболочка) контекст пуст — считаем видимым.
  const ctx = useContext(AppCtx);
  const visible = ctx.tabVisible !== false;

  const load = useCallback(async (quiet) => {
    const my = ++seq.current;
    if (!quiet) setState((s) => ({ ...s, loading: true }));
    try {
      const data = await call(method, ...argsRef.current);
      if (my === seq.current) setState({ data, err: null, loading: false });
    } catch (e) {
      // Ответ устаревшего запроса игнорируем: иначе быстрый перебор строк списка
      // (j/k по клавиатуре) показал бы карточку не того заказа.
      if (my !== seq.current) return;
      setState((s) => (quiet
        ? { ...s, err: e.message, loading: false }   // фон: показанное не стираем
        : { data: null, err: e.message, loading: false }));
    }
  }, [method]);

  // reload из обработчиков часто уходит прямо в onClick — аргументом прилетел бы объект
  // события и молча включил тихий режим. Поэтому наружу отдаём обёртки без аргументов.
  const reload = useCallback(() => load(false), [load]);
  const refresh = useCallback(() => load(true), [load]);

  useEffect(() => { if (!skip) load(); }, deps.concat([load, skip]));   // eslint-disable-line

  useEffect(() => {
    if (!auto || skip) return undefined;
    let timer = null;
    const tick = () => {
      timer = setTimeout(async () => {
        if (!document.hidden && visible) await load(true);
        tick();
      }, auto);
    };
    tick();
    // Возврат в окно — повод обновиться немедленно: мастер уходил в слайсер и вернулся.
    const onVis = () => { if (!document.hidden && visible) load(true); };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', onVis); };
  }, deps.concat([auto, skip, visible, load]));   // eslint-disable-line

  return { ...state, reload, refresh };
}

/**
 * Поллинг. Реалтайма в проекте нет вообще — ни SSE, ни WebSocket.
 *
 * ВАЖНО: бот и вебапп живут в ОДНОМ event loop, поэтому поллинг заводим один на приложение
 * и раздаём через контекст, а не по одному на виджет. Иначе десктоп с несколькими панелями
 * умножит интервал на число виджетов и будет конкурировать с обработкой Telegram-апдейтов.
 *
 * Останавливается, когда окно не видно: незачем долбить бота, пока мастер в другой программе.
 */
export function usePoll(method, ms = 4000, args = []) {
  const [state, setState] = useState({ data: null, err: null, loading: true });
  const argsRef = useRef(args);
  argsRef.current = args;
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    let timer = null;

    const tick = async () => {
      if (!alive.current) return;
      if (document.hidden) { timer = setTimeout(tick, ms); return; }
      try {
        const data = await call(method, ...argsRef.current);
        if (alive.current) setState({ data, err: null, loading: false });
      } catch (e) {
        // Ошибку показываем, но НЕ гасим цикл: бот мог перезапускаться (~90 с),
        // после чего поллинг обязан сам ожить.
        if (alive.current) setState((s) => ({ ...s, err: e.message, loading: false }));
      }
      if (alive.current) timer = setTimeout(tick, ms);
    };

    tick();
    const onVis = () => { if (!document.hidden && alive.current) { clearTimeout(timer); tick(); } };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      alive.current = false;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [method, ms]);

  return state;
}

/** Мутация с оптимистичным тостом. Возвращает [run, busy]. */
export function useMutation(toast) {
  const [busy, setBusy] = useState(false);
  const run = useCallback(async (method, args, opts = {}) => {
    setBusy(true);
    try {
      const res = await call(method, ...(args || []));
      if (opts.ok) toast(opts.ok, { kind: 'ok', action: opts.undoLabel, onAction: opts.onUndo });
      if (opts.after) await opts.after(res);
      return res;
    } catch (e) {
      toast(e.message, { kind: 'warn2', ms: 9000 });
      return null;
    } finally {
      setBusy(false);
    }
  }, [toast]);
  return [run, busy];
}
