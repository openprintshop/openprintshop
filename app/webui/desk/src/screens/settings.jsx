// settings.jsx — «Настройки»: операционные тумблеры и пороги мастерской. До этого экрана
// эти настройки жили только частью в «Ценах и скидках» (SetGroup group=pricing) вперемешку
// со ставками и в мобильной админке (screens_admin2). Здесь они собраны отдельно от денег:
// «Настройки» = поведение бота (догон, срочность, DFM, слайс/санация), а не прайс.
//
// Источник тот же, что у «Цен» — /api/admin/settings (adminSettings) отдаёт ВСЕ ценовые ключи
// одной группой pricing; мы берём из неё только операционные ключи (ставки/наценка остаются в
// «Ценах и скидках», их тут НЕ дублируем — решение владельца). Подписи — серверные (SETTING_LABELS
// через data.pricing.labels), чтобы формулировки не расходились между экранами.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { ago } from '../fmt.jsx';
import { ABtn, Screen, Toggle } from '../parts.jsx';

const { useState, useContext } = React;

// Подсветка построчной кнопки «сохранить», когда значение изменено (как в pricing.jsx —
// класса .dirty в дизайн-системе нет, повторяем инлайн из макета).
const DIRTY = { color: 'var(--accent)', borderColor: 'var(--accent-line)', background: 'var(--accent-soft)' };

// Явно известные тумблеры 0/1 (историческое множество из pricing.jsx / screens_admin2).
// Смысл у них булев, поэтому переключатель, а не поле ввода.
const KNOWN_TOGGLES = /^(slicing_enabled|sanitize_3mf|time_prediction)$/;

/**
 * Настройка — тумблер, если её значение булево (0/1) И ключ либо кончается на `_enabled`
 * (общее правило для новых флагов вроде nudge_abandoned_enabled), либо входит в исторический
 * набор известных тумблеров. Всё остальное — числовое поле. Экспортируется отдельно ради теста:
 * ошибка тут тихая — флаг молча превратился бы в поле «впиши 1», ровно ту UX-боль, что и чинили.
 */
export function isToggleKey(key, value) {
  const boolean = value === 0 || value === 1;
  return boolean && (/_enabled$/.test(key) || KNOWN_TOGGLES.test(key));
}

// Группы операционных настроек с иконками. Порядок и состав — смысловой (A5 догон, B3 срочность,
// B2 печатопригодность, расчёт/файлы). Ключей, которых нет в ответе сервера, просто не покажем.
const GROUPS = [
  { t: 'Расчёт и файлы моделей', ic: 'printer',
    keys: ['slicing_enabled', 'sanitize_3mf', 'time_prediction'] },
  { t: 'Догон брошенных расчётов', ic: 'bell',
    keys: ['nudge_abandoned_enabled', 'nudge_abandoned_hours', 'nudge_abandoned_max_days'] },
  { t: 'Срочность и сроки', ic: 'clock',
    keys: ['rush_surcharge_pct', 'term_split_hours'] },
  { t: 'Печатопригодность', ic: 'shield',
    keys: ['dfm_min_wall_mm'] },
];

// Единый список операционных ключей, «переехавших» из «Цен и скидок» на этот экран. Экспорт —
// чтобы pricing.jsx не показывал их второй раз (единый источник правды: правим состав здесь).
export const OPERATIONAL_KEYS = new Set(GROUPS.flatMap((g) => g.keys));

export function SettingsScreen() {
  const s = useApi('adminSettings', [], []);
  // одна колонка ограниченной ширины: настроек немного, растягивать на всю ширину незачем
  return <div className="colw" style={{ maxWidth: 640 }}>
    <Screen state={s}>{(data) => {
      const pricing = data.pricing || {};
      return <>
        <BrandGroup group={data.brand} current={data.brand_current} onSaved={s.reload} />
        <PayGroup group={data.pay} onSaved={s.reload} />
        <OwnersGroup group={data.owners} onSaved={s.reload} />
        <AccessGroup group={data.access} onSaved={s.reload} />
        <EditionGroup data={data.edition} onSaved={s.reload} />
        {GROUPS.map((g) => <SettingsGroup key={g.t} title={g.t} ic={g.ic}
          keys={g.keys} group={pricing} onSaved={s.reload} />)}
        <div className="hint" style={{ marginTop: 2 }}>
          Ставки, наценка и реквизиты в смете — на экране «Цены и скидки». Пороги сегментов
          клиентов (VIP / «спящий») — на экране «Клиенты».
        </div>
      </>;
    }}</Screen>
    {/* своя загрузка (adminBackupStatus), поэтому вне Screen настроек: состояние бэкапов
        должно быть видно даже когда /api/admin/settings не отвечает */}
    <BackupCard />
  </div>;
}

const MB = (b) => (Number(b || 0) / 1048576).toFixed(1) + ' МБ';

// Порог «бэкап протух»: снимки идут раз в 6 ч, значит 13 ч — это два пропуска подряд,
// то есть уже не случайная осечка, а сломанный контур.
const STALE_HOURS = 13;

/**
 * Бэкапы БД (OpenTodo b557e341). Два контура: локальные снимки бота в data/backups и копии
 * задачи Планировщика в офсайт-каталоге. Показываем не только «успешно», но и ВОЗРАСТ свежего
 * снимка: молча протухший бэкап опаснее явной ошибки — про ошибку хотя бы приходит алерт.
 */
function BackupCard() {
  const { toast } = useContext(AppCtx);
  const s = useApi('adminBackupStatus', [], []);
  const [busy, setBusy] = useState(false);

  const runNow = async () => {
    setBusy(true);
    // call() разворачивает {ok:false,error} в исключение — покажем настоящую причину отказа
    try {
      const r = await call('adminBackupNow');
      toast('Бэкап готов · ' + MB(r.run && r.run.size), { kind: 'ok' });
      s.reload();
    } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); } finally { setBusy(false); }
  };

  return <Sec card ic="folder" t="Бэкапы" style={{ flex: '0 0 auto' }}>
    <Screen state={s}>{(d) => {
      const loc = d.local || {};
      const off = d.offsite || {};
      const snaps = loc.snapshots || [];
      const manual = loc.manual || [];
      const fresh = snaps[0];
      const last = d.last;
      const ageH = fresh ? (Date.now() / 1000 - fresh.mtime) / 3600 : null;
      const stale = ageH === null || ageH > STALE_HOURS;

      const warns = [];
      if (last && !last.ok) {
        warns.push('Последний бэкап не удался: ' + (last.error || 'причина неизвестна'));
      }
      if (stale) {
        warns.push(fresh
          ? `Свежему снимку уже ${Math.round(ageH)} ч, а он должен обновляться раз в ${loc.interval_hours || 6} ч — проверьте, жив ли бот`
          : 'Локальных снимков нет вообще — бэкап не работает');
      }
      if (d.same_volume) {
        warns.push('Резервные копии лежат на том же физическом диске, что и база — при отказе диска пропадёт всё. Вынесите BOT_DB_BACKUP_DIR на другой носитель или в облако.');
      }

      const line = !last ? 'Бэкапов ещё не было — первый снимок бот делает при старте'
        : last.ok ? `Последний ${ago(fresh ? fresh.mtime : 0) || last.ts} · ${MB(last.size)} · за ${(Number(last.duration_ms || 0) / 1000).toFixed(1)} с`
        : `Последняя попытка ${last.ts} — неудачно`;

      return <>
        <div className={last && !last.ok ? 'warn' : 'okay'} style={{ fontSize: 13 }}>{line}</div>
        <div className="hint" style={{ marginTop: 9 }}>
          Локально: {snaps.length} снимк(ов), храним последние {loc.keep || 0}
          {manual.length ? ` · ручных копий ${manual.length} (ротация их не трогает)` : ''}
        </div>
        <div className="hint" style={{ marginTop: 3 }}>
          {off.exists
            ? `Второй контур: ${off.count} копи(й)${off.latest_mtime ? ', свежая ' + ago(off.latest_mtime) : ''}`
            : 'Второй контур: каталог не найден — задача pcb_db_backup не отработала ни разу'}
        </div>
        <div className="mono" style={{ fontSize: 11.5, color: 'var(--faint)', marginTop: 3,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{off.dir || ''}</div>

        {warns.map((w, i) => <div key={i} className="warn" style={{ fontSize: 13, marginTop: 9 }}>
          <DIcon n="bell" size={15} style={{ flexShrink: 0 }} />{w}
        </div>)}

        <div style={{ marginTop: 12 }}>
          <ABtn kind="line" style={{ width: '100%' }} disabled={busy} onClick={runNow}>
            {busy ? 'Делаю бэкап…' : 'Сделать бэкап сейчас'}
          </ABtn>
        </div>
        <div className="hint" style={{ marginTop: 8 }}>
          Копия снимается на живой базе и публикуется только после проверки целостности.
          Порядок восстановления — в docs/Уже в продукте/backups.md.
        </div>
      </>;
    }}</Screen>
  </Sec>;
}

/**
 * Бренд мастерской (A3, white-label): имя, подпись, акцент и логотип.
 *
 * Три поля — обычные text-настройки (kind:'text'), логотип — файл, поэтому у него свой
 * эндпоинт. Живой предпросмотр рисуем шапкой мини-аппа: цифры в полях ничего не говорят,
 * а мастеру важно увидеть, КАК это будет выглядеть у клиента.
 *
 * Изменения применяются после перезагрузки страницы: бренд вшивается сервером в разметку
 * до первого кадра (иначе клиент видел бы моргание чужого бренда), значит на лету его
 * подменить нельзя — об этом честно пишем подсказкой, а не делаем вид, что всё мгновенно.
 */
function BrandGroup({ group, current, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [edit, setEdit] = useState({});
  const [busy, setBusy] = useState(false);
  const file = React.useRef(null);
  if (!group) return null;                         // старая версия бота — группы в ответе нет
  const vals = group.values || {};
  const labels = group.labels || {};
  const cur = current || {};

  const drop = (k) => setEdit((st) => { const n = { ...st }; delete n[k]; return n; });
  const save = async (k) => {
    try {
      await call('adminSetting', { key: k, kind: 'text', value: edit[k] ?? '' });
      drop(k);
      toast('Сохранено — обновите страницу, чтобы увидеть', { kind: 'ok', ms: 3000 });
      onSaved();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };
  const upload = async (f) => {
    if (!f) return;
    setBusy(true);
    // call() сам разворачивает {ok:false,error} в исключение — отдельной проверки не нужно
    try { await call('adminBrandLogo', f); toast('Логотип загружен', { kind: 'ok' }); onSaved(); }
    catch (e) { toast(e.message, { kind: 'warn2' }); } finally { setBusy(false); }
  };
  const clear = async () => {
    setBusy(true);
    try { await call('adminBrandLogoClear'); toast('Логотип снят', { kind: 'ok' }); onSaved(); }
    catch (e) { toast(e.message, { kind: 'warn2' }); } finally { setBusy(false); }
  };

  return <Sec card t="Бренд мастерской" ic="star" style={{ flex: '0 0 auto' }}>
    {/* предпросмотр шапки: ровно то, что увидит клиент в мини-аппе */}
    <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '11px 14px',
      background: 'var(--bg)', border: '1px solid var(--line)', borderRadius: 12, marginBottom: 14 }}>
      {cur.logo
        ? <img src={cur.logo} alt="" width="25" height="25" className="sb-logo" />
        : <svg width="25" height="25" viewBox="0 0 24 24" fill="none" stroke={cur.accent || 'var(--accent)'}
            strokeWidth="1.6" strokeLinecap="round">
            <path d="M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6M12 3v6M12 15v6M3 12h6M15 12h6" />
          </svg>}
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 700, letterSpacing: '.4px', fontSize: 14 }}>{cur.title}</div>
        <div className="hint" style={{ margin: 0 }}>{cur.tagline}</div>
      </div>
      <span style={{ marginLeft: 'auto', width: 22, height: 22, borderRadius: 7, flex: '0 0 auto',
        background: cur.accent, border: '1px solid var(--line-2)' }} title={cur.accent} />
    </div>

    {(group.order || []).map((k) => {
      const v = k in edit ? edit[k] : (vals[k] || '');
      return <div key={k} className="setrow">
        <span className="sl">{labels[k] || k}</span>
        <input className="inp" style={{ width: 190 }} value={v}
          placeholder={k === 'brand_accent' ? '#ff7a1a' : 'как у нас'}
          onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
        <button className="ib" title="Сохранить" style={k in edit ? DIRTY : undefined}
          disabled={!(k in edit)} onClick={() => save(k)}>
          <DIcon n="check" size={15} />
        </button>
      </div>;
    })}

    <div className="setrow">
      <span className="sl">Логотип (SVG, PNG, WEBP, JPG · до 512 КБ)</span>
      <input ref={file} type="file" accept=".svg,.png,.webp,.jpg,.jpeg" style={{ display: 'none' }}
        onChange={(e) => { upload(e.target.files && e.target.files[0]); e.target.value = ''; }} />
      <button className="db soft" disabled={busy} onClick={() => file.current && file.current.click()}>
        {cur.logo ? 'Заменить' : 'Загрузить'}
      </button>
      {cur.logo
        ? <button className="ib" title="Снять логотип" disabled={busy} onClick={clear}>
            <DIcon n="trash" size={15} />
          </button>
        : null}
    </div>

    <div className="hint" style={{ marginTop: 10 }}>
      Пустое поле = наше значение по умолчанию. Клиент увидит новый бренд после перезагрузки
      мини-аппа: он вшивается в страницу на сервере, чтобы не мелькал старый.
    </div>
  </Sec>;
}

/**
 * Сообщество ВЛАДЕЛЬЦЕВ студий (c356387a): ссылка на внешнюю Telegram-группу. Своего
 * сервера у продукта нет и установки друг о друге не знают, поэтому площадка внешняя,
 * а здесь хранится только её адрес. Пусто — раздел «Сообщество» не предлагает вход.
 */
// Вход в настольную админку (app/desklogin.py). Не текстовое поле, а два состояния: значение
// сравнивается со строкой, и опечатка «Personal» тихо оставила бы общую дверь открытой.
function AccessGroup({ group, onSaved }) {
  const { toast } = useContext(AppCtx);
  if (!group) return null;                         // старая версия бота — группы в ответе нет
  const mode = (group.values || {}).desk_login_mode || 'token';

  const set = async (v) => {
    if (v === mode) return;
    try {
      await call('adminSetting', { key: 'desk_login_mode', kind: 'text', value: v });
      toast(v === 'personal' ? 'Теперь вход только личный' : 'Вернули общий вход',
        { kind: 'ok', ms: 3000 });
      onSaved();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  return <Sec card t="Вход в настольную админку" ic="users" style={{ flex: '0 0 auto' }}>
    <div className="setrow">
      <span className="sl">Как входят сотрудники</span>
      <div style={{ display: 'flex', gap: 8 }}>
        <button className={'db sm' + (mode === 'token' ? '' : ' line')} onClick={() => set('token')}>
          Общий вход
        </button>
        <button className={'db sm' + (mode === 'personal' ? '' : ' line')} onClick={() => set('personal')}>
          Личный вход
        </button>
      </div>
    </div>
    <div className="hint" style={{ marginTop: 8 }}>
      {mode === 'personal'
        ? <>Каждый входит под собой: пишет боту <b>/desk</b>, получает код и вводит его здесь. Роли
            работают по-настоящему — оператор не видит выручку и цены.</>
        : <>Один вход на компьютер: кто сел за него, тот работает правами владельца, и роли
            в этой админке ничего не значат. Для мастерской с наёмным сотрудником включите
            личный вход.</>}
    </div>
  </Sec>;
}

function OwnersGroup({ group, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [edit, setEdit] = useState({});
  if (!group) return null;                         // старая версия бота — группы в ответе нет
  const vals = group.values || {};
  const labels = group.labels || {};

  const save = async (k) => {
    try {
      await call('adminSetting', { key: k, kind: 'text', value: edit[k] ?? '' });
      setEdit((st) => { const n = { ...st }; delete n[k]; return n; });
      toast('Сохранено', { kind: 'ok', ms: 2500 });
      onSaved();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  return <Sec card t="Сообщество владельцев" ic="chat" style={{ flex: '0 0 auto' }}>
    {(group.order || []).map((k) => {
      const v = k in edit ? edit[k] : (vals[k] || '');
      return <div key={k} className="setrow">
        <span className="sl">{labels[k] || k}</span>
        <input className="inp" style={{ width: 260 }} value={v} placeholder="https://t.me/…"
          onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
        <button className="ib" title="Сохранить" style={k in edit ? DIRTY : undefined}
          disabled={!(k in edit)} onClick={() => save(k)}>
          <DIcon n="check" size={15} />
        </button>
      </div>;
    })}
    <div className="hint" style={{ marginTop: 10 }}>
      Группа для владельцев студий, а не для ваших заказчиков: ссылка видна только в админке,
      на экране «Сообщество». Клиенты мастерской её не видят.
    </div>
  </Sec>;
}

/**
 * Возможности сборки (OpenTodo f98aa1ef). Часть инструментов делалась «под себя» и завязана
 * на конкретную машину: пул аккаунтов MakerWorld, генерация аватарок на видеокарте, рендер
 * OpenSCAD, дисплей TURZX, самообновление из git. В билде студии они по умолчанию скрыты —
 * иначе продукт обещает то, чего у покупателя нет.
 *
 * Переключатель владельца сильнее дефолта в ОБЕ стороны: свою фичу можно выключить, а чужую
 * (при подходящей машине) включить. «Сброс» возвращает решение редакции.
 */
function EditionGroup({ data, onSaved }) {
  const { toast } = useContext(AppCtx);
  if (!data || !(data.features || []).length) return null;   // старая версия бота
  const dev = data.edition === 'dev';

  const set = async (f, body) => {
    try {
      await call('adminFeature', { key: f.key, ...body });
      toast(`«${f.title}» — ${body.reset ? 'как в этой сборке' : (body.on ? 'включена' : 'выключена')}`,
        { kind: 'ok', ms: 2500 });
      onSaved();
    } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); }
  };

  return <Sec card t="Возможности сборки" ic="layers" style={{ flex: '0 0 auto' }}>
    <div className="hint" style={{ marginBottom: 10 }}>
      Сборка: <b>{dev ? 'разработчика' : 'студии'}</b>. {dev
        ? 'Личные инструменты включены — это ваша рабочая машина.'
        : 'Инструменты, завязанные на машину разработчика, по умолчанию выключены.'}
    </div>
    {(data.features || []).map((f) => <div key={f.key} style={{ padding: '10px 0',
      borderTop: '1px solid var(--line)' }}>
      <div className="setrow" style={{ alignItems: 'flex-start' }}>
        <span className="sl" style={{ flex: 1, whiteSpace: 'normal' }}>
          {f.title}
          {f.overridden ? <span className="stb violet" style={{ marginLeft: 8 }}>вручную</span> : null}
          {!f.available ? <span className="stb red" style={{ marginLeft: 8 }}>нет на этой машине</span> : null}
        </span>
        <button className={'db sm ' + (f.enabled ? '' : 'line')}
          onClick={() => set(f, { on: !f.enabled })}>
          {f.enabled ? 'Включена' : 'Выключена'}
        </button>
        {f.overridden ? <button className="ib" title="Вернуть как в этой сборке"
          onClick={() => set(f, { reset: 1 })}><DIcon n="refresh" size={14} /></button> : null}
      </div>
      <div className="hint" style={{ marginTop: 4 }}>{f.why}</div>
      {f.needs ? <div className="hint" style={{ marginTop: 2, color: 'var(--faint)' }}>
        нужно: {f.needs}
      </div> : null}
    </div>)}
  </Sec>;
}

/**
 * Онлайн-оплата (A1): токен провайдера Telegram Payments + валюта счетов. Обычные
 * text-настройки (kind:'text'), но со своей секцией и подсказкой по подключению —
 * без неё поле «токен» выглядит загадкой. Пустой токен = оплата выключена.
 */
function PayGroup({ group, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [edit, setEdit] = useState({});
  if (!group) return null;                         // старая версия бота — группы в ответе нет
  const vals = group.values || {};
  const labels = group.labels || {};
  const on = !!(vals.pay_provider_token || '').trim() || 'pay_provider_token' in edit;

  const drop = (k) => setEdit((st) => { const n = { ...st }; delete n[k]; return n; });
  const save = async (k) => {
    try {
      await call('adminSetting', { key: k, kind: 'text', value: edit[k] ?? '' });
      drop(k);
      toast('Сохранено', { kind: 'ok', ms: 2500 });
      onSaved();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  return <Sec card t="Онлайн-оплата" ic="coin" style={{ flex: '0 0 auto' }}>
    {(group.order || []).map((k) => {
      const v = k in edit ? edit[k] : (vals[k] || '');
      return <div key={k} className="setrow">
        <span className="sl">{labels[k] || k}</span>
        <input className="inp" style={{ width: 190 }} value={v}
          placeholder={k === 'pay_currency' ? 'RUB' : 'из BotFather'}
          onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
        <button className="ib" title="Сохранить" style={k in edit ? DIRTY : undefined}
          disabled={!(k in edit)} onClick={() => save(k)}>
          <DIcon n="check" size={15} />
        </button>
      </div>;
    })}
    <div className="hint" style={{ marginTop: 10 }}>
      {on
        ? 'Клиенты видят кнопку «Оплатить» в заказах: счёт открывается прямо в Telegram, деньги идут вашему провайдеру. Возвраты — в кабинете провайдера.'
        : 'Подключите провайдера в BotFather (Bot Settings → Payments): ЮKassa для РФ, bePaid для Беларуси, Stripe для карт мира. Токен вставьте сюда — у клиентов появится кнопка «Оплатить». Валюта должна совпадать с валютой ваших цен.'}
    </div>
  </Sec>;
}

/**
 * Одна смысловая группа настроек. Сохранение построчное, как в «Ценах»: тумблер уходит сразу,
 * числовое поле — по кнопке-галочке (индикатор «изменено» подсвечивает несохранённое).
 * kind:'num' обязателен — без него сервер (webapp.py::_api_admin_setting) молча отвечает
 * «Неизвестный параметр» и настройка не пишется.
 */
function SettingsGroup({ title, ic, keys, group, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [edit, setEdit] = useState({});
  const vals = group.values || {};
  const labels = group.labels || {};
  // показываем только те ключи группы, что реально пришли с сервера — иначе на старой версии
  // бота (до появления ключа) нарисовали бы пустую строку без значения
  const present = keys.filter((k) => k in vals);
  if (!present.length) return null;

  const drop = (k) => setEdit((st) => { const n = { ...st }; delete n[k]; return n; });
  const save = async (k, label, value) => {
    await call('adminSetting', { key: k, kind: 'num', value });
    drop(k);
    toast('Сохранено: ' + label, { kind: 'ok', ms: 2500 });
    onSaved();
  };

  return <Sec card t={title} ic={ic} style={{ flex: '0 0 auto' }}>
    {present.map((k) => {
      const cur = k in edit ? edit[k] : vals[k];
      const label = labels[k] || k;
      return <div key={k} className="setrow">
        <span className="sl">{label}</span>
        {isToggleKey(k, vals[k])
          ? <Toggle on={Number(cur) === 1} label={label} onChange={(on) => {
              // тумблер сохраняем сразу — отдельной кнопки подтверждения он не требует
              setEdit((st) => ({ ...st, [k]: on ? 1 : 0 }));
              save(k, label, on ? 1 : 0).catch((e) => { drop(k); toast(e.message, { kind: 'warn2' }); });
            }} />
          : <>
              <input className="inp" type="number" step="0.01" style={{ width: 150 }} value={cur ?? ''}
                onChange={(e) => setEdit({ ...edit, [k]: Number(e.target.value) })} />
              <button className="ib" title="Сохранить" style={k in edit ? DIRTY : undefined}
                disabled={!(k in edit)}
                onClick={() => save(k, label, edit[k]).catch((e) => toast(e.message, { kind: 'warn2' }))}>
                <DIcon n="check" size={15} />
              </button>
            </>}
      </div>;
    })}
  </Sec>;
}
