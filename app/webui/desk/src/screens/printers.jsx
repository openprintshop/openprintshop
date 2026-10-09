// printers.jsx — «Принтеры»: профили слайсера. Путей три и они локальные для ПК бота.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty, Field } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { Screen, ABtn, useConfirm } from '../parts.jsx';

const { useState, useContext } = React;

export function PrintersScreen() {
  const { toast } = useContext(AppCtx);
  const p = useApi('adminPrinters', [], []);
  const printers = (p.data && p.data.printers) || [];

  // Бэкенд знает только add/default/toggle/delete — правки полей существующего принтера нет,
  // поэтому пути показываем как справку, а не как форму.
  // Возвращает признак успеха: форма добавления чистит поля только когда сервер принял пути.
  const act = async (body, okText) => {
    try {
      await call('adminPrinter', body);
      toast(okText, { kind: 'ok' });
      p.reload();
      return true;
    } catch (e) {
      toast(e.message, { kind: 'warn2', ms: 9000 });
      return false;
    }
  };

  const kinds = (p.data && p.data.kinds) || [];
  return <Screen state={p}>{() =>
    <div className="onecol" style={{ maxWidth: 1500, width: '100%', margin: '0 auto', gap: 20, overflowY: 'auto' }}>
      {printers.length
        ? printers.map((x) => <PrinterCard key={x.id} p={x} act={act} kinds={kinds} />)
        : <Empty ic="box" t="Принтеров нет" ds="Добавьте профиль машины ниже" />}
      <AddPrinter act={act} />
    </div>}
  </Screen>;
}

// Способности принтера (OpenTodo 0613014e). Раньше кнопки пульта были у всех одинаковые, и
// «не умеет» выяснялось нажатием: у Klipper-принтера жали «Прогрев» и получали тишину.
// Теперь драйвер честно говорит, что умеет, — а карточка это показывает.
const CAP_LABELS = [
  ['status', 'телеметрия'], ['files', 'файлы'], ['upload', 'заливка'], ['start', 'старт'],
  ['control', 'пауза/стоп'], ['move', 'оси и прогрев'], ['filament', 'филамент'],
  ['camera', 'камера'], ['history', 'история'], ['timelapse', 'таймлапсы'],
  // delete завели отдельной способностью в волне 3: удаление файла гейтилось флагом «умею
  // показать список», и принтер, умеющий листать, но не удалять, показывал кнопку впустую.
  ['delete', 'удаление файлов'], ['exclude_object', 'снять деталь'],
];

function Caps({ caps }) {
  if (!caps) return null;
  return <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
    {CAP_LABELS.map(([k, l]) => <span key={k} className={'stb ' + (caps[k] ? 'green' : 'gray')}
      style={caps[k] ? undefined : { opacity: 0.5 }}>{caps[k] ? '' : '— '}{l}</span>)}
  </div>;
}

// Фото 300×230 слева, данные справа — как в дизайн-хендоффе (dd-catalog.jsx PrintersScreen).
// Реальное фото есть только у физического ECC2 (основного) — оно лежит в /app/desk/assets и
// раздаётся с админкой. У прочих профилей фото нет: вместо пустого слота показываем иконку.
const PHOTO = { width: 300, height: 230, flex: '0 0 auto', borderRadius: 12,
  border: '1px solid var(--line)', objectFit: 'cover', display: 'block' };

function PrinterCard({ p, act, kinds }) {
  const confirm = useConfirm();
  return <div className="card" style={{ flexDirection: 'row', gap: 22, alignItems: 'stretch' }}>
    {/* Фото — по МОДЕЛИ, а не по флагу «основной»: фотография ECC2 под карточкой Bambu
        подписывала бы чужую машину, а сделать основной можно любую. */}
    {(p.kind || 'elegoo') === 'elegoo'
      ? <img src="assets/printer-ecc2.jpg" alt="Elegoo Centauri Carbon 2" style={PHOTO} />
      : <div style={{ ...PHOTO, display: 'flex', flexDirection: 'column', gap: 8,
          alignItems: 'center', justifyContent: 'center', color: 'var(--faint)',
          background: 'var(--bg)', borderStyle: 'dashed' }}>
          <DIcon n="printer" size={30} style={{ opacity: 0.5 }} />
          <span style={{ fontSize: 12 }}>Профиль слайсера</span>
        </div>}
    <div style={{ flex: 1, minWidth: 0 }}>
      <div className="lab">
        <DIcon n="printer" size={13} />{p.name}
        {p.is_default ? <span className="stb amber" style={{ marginLeft: 8 }}>★ основной</span> : null}
        {p.is_active ? null : <span className="stb gray" style={{ marginLeft: 8 }}>выключен</span>}
        <span className="r">
          <button className="ib" style={{ width: 32, height: 32 }} title="Сделать основным"
            onClick={() => act({ action: 'default', id: p.id }, 'Основной принтер выбран')}>
            <DIcon n="star" size={14} />
          </button>
          <button className="ib" style={{ width: 32, height: 32 }} title={p.is_active ? 'Выключить' : 'Включить'}
            onClick={() => act({ action: 'toggle', id: p.id }, p.is_active ? 'Принтер выключен' : 'Принтер включён')}>
            <DIcon n="check" size={14} />
          </button>
          <button className="ib danger" style={{ width: 32, height: 32 }} title="Удалить"
            onClick={() => confirm('Удалить принтер?', `«${p.name}» пропадёт из списка профилей.`,
              () => act({ action: 'delete', id: p.id }, 'Принтер удалён'))}>
            <DIcon n="trash" size={14} />
          </button>
        </span>
      </div>
      {[['machine (.json)', p.machine], ['process (папка)', p.process],
        ['filament (папка)', p.filament]].map(([k, v]) => <div key={k} className="setrow">
        <span className="sl" style={{ width: 150, flex: '0 0 auto' }}>{k}</span>
        <span className="mono" style={{ flex: 1, color: 'var(--muted)', fontSize: 13 }}>{v || '—'}</span>
      </div>)}
      <Link p={p} act={act} kinds={kinds} />
      <TimeFactor p={p} act={act} />
      <Nozzles p={p} act={act} />
    </div>
  </div>;
}

// Связь с принтером: чем разговариваем и по какому адресу (OpenTodo 0613014e). Раньше адрес
// и код доступа жили только в .env — то есть второй принтер описать было нечем, а покупателю
// продукта пришлось бы править файл руками.
function Link({ p, act, kinds }) {
  const [f, setF] = useState(null);          // null — форма закрыта
  const drv = p.driver || {};

  return <div style={{ marginTop: 14, borderTop: '1px solid var(--line)', paddingTop: 12 }}>
    <div className="lab" style={{ marginBottom: 8 }}>
      <DIcon n="link" size={13} />Связь
      <span className="stb" style={{ marginLeft: 8 }}>{drv.title || '—'}</span>
      {drv.enabled
        ? <span className="stb green" style={{ marginLeft: 6 }}>настроен</span>
        : <span className="stb gray" style={{ marginLeft: 6 }}>не настроен</span>}
      <span className="r">
        <button className="db line sm" onClick={() => setF(f ? null : {
          kind: p.kind || '', address: p.address || '', serial: p.serial || '', access_code: '',
        })}><DIcon n="pencil" size={13} />{f ? 'Отмена' : 'Настроить'}</button>
      </span>
    </div>
    {f ? <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr auto', gap: 10,
      alignItems: 'end' }}>
      <Field l="Протокол">
        <select className="sel" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
          {(kinds || []).map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
        </select>
      </Field>
      <Field l="Адрес (IP или URL)">
        <input className="inp mono" placeholder="192.168.0.77" value={f.address}
          onChange={(e) => setF({ ...f, address: e.target.value })} />
      </Field>
      <Field l={p.has_code ? 'Код доступа (задан)' : 'Код доступа'}>
        <input className="inp mono" type="password" placeholder={p.has_code ? '••••••' : 'с экрана принтера'}
          value={f.access_code} onChange={(e) => setF({ ...f, access_code: e.target.value })} />
      </Field>
      <Field l="Серийный номер">
        <input className="inp mono" value={f.serial}
          onChange={(e) => setF({ ...f, serial: e.target.value })} />
      </Field>
      <ABtn onClick={async () => {
        if (await act({ action: 'link', id: p.id, ...f }, 'Связь сохранена')) setF(null);
      }}>Сохранить</ABtn>
    </div> : <>
      <div className="setrow">
        <span className="sl" style={{ width: 150, flex: '0 0 auto' }}>адрес</span>
        <span className="mono" style={{ flex: 1, color: 'var(--muted)', fontSize: 13 }}>
          {p.address || 'из .env'}
        </span>
      </div>
      <Caps caps={drv.caps} />
      {/* Пустой протокол = «как раньше»: определяем по .env. Так заведён боевой принтер. */}
      {!p.kind ? <div className="hint" style={{ marginTop: 6 }}>
        протокол определяется автоматически по настройкам в .env
      </div> : null}
      {/* Честность про второй принтер. Адрес, код и серийник карточки теперь едут параметром
          в каждый вызов протокола — но живой канал в процессе ОДИН: фоновый подписчик MQTT,
          ретранслятор камеры и наблюдатель за печатью знают ровно один принтер. Пока это так,
          у карточки с чужим адресом способности гасятся намеренно. Показать это словами важнее,
          чем оставить мастера гадать, почему кнопок нет. */}
      {p.kind && p.kind !== 'none' && !drv.enabled && (p.address || '').trim()
        ? <div className="warn" style={{ marginTop: 8 }}>
          Адрес задан, но пульта у этой карточки нет — и это не поломка.
          Живая связь с принтером в программе одна: телеметрия, камера и журнал печати
          сейчас принадлежат основному принтеру. Вторая карточка получила бы его температуры
          и его же таймлапсы, а её печать попала бы в чужой заказ — поэтому кнопки скрыты,
          пока второй принтер не поддержан целиком. Слайс и профили работают как обычно.
          Если это и есть основной принтер — проверьте, совпадает ли адрес с тем,
          что в .env, и задан ли код доступа.
        </div> : null}
      {/* Обратный случай: протокол выбран, а адрес пуст. Тогда поля падают на фолбэк в .env,
          и карточка становится пультом ОСНОВНОГО принтера, ничем этого не показывая.
          Из интерфейса это не эксплуатируется (управление идёт через принтер по умолчанию),
          но молча выдавать чужие кнопки за свои нельзя. */}
      {p.kind && p.kind !== 'none' && !(p.address || '').trim() && drv.enabled
        ? <div className="hint" style={{ marginTop: 8 }}>
          адрес не задан — карточка работает с основным принтером из .env, а не со своим
        </div> : null}
    </>}
  </div>;
}

// Коэффициент времени машины (B1, трек B roadmap). Слайсер и быстрый расчёт откалиброваны
// по одной паре «движок+машина» (CC2): у другой машины минуты — а с ними и цена — считались
// бы молча неверно. Множитель даёт мастеру ручку в день приезда, когда журнала по машине ещё
// нет; дальше подсказка по журналу ЭТОЙ машины (медиана факт/оценка) говорит, куда крутить.
function TimeFactor({ p, act }) {
  const [v, setV] = useState(null);          // null — форма закрыта
  const st = p.time_stats || {};
  const cur = Number(p.time_factor || 1);
  const fmt = (x) => '×' + Number(x).toFixed(2).replace(/\.?0+$/, '');
  return <div style={{ marginTop: 14, borderTop: '1px solid var(--line)', paddingTop: 12 }}>
    <div className="lab" style={{ marginBottom: 8 }}>
      <DIcon n="clock" size={13} />Время печати
      <span className={'stb ' + (cur === 1 ? 'gray' : 'green')} style={{ marginLeft: 8 }}>{fmt(cur)}</span>
      <span className="r">
        <button className="db line sm" onClick={() => setV(v === null ? String(cur) : null)}>
          <DIcon n="pencil" size={13} />{v === null ? 'Настроить' : 'Отмена'}</button>
      </span>
    </div>
    {v !== null ? <div style={{ display: 'grid', gridTemplateColumns: '180px auto 1fr', gap: 10,
      alignItems: 'end' }}>
      <Field l="Коэффициент (×0.2…×5)">
        <input className="inp mono" type="number" step="0.05" min="0.2" max="5" value={v}
          onChange={(e) => setV(e.target.value)} />
      </Field>
      <ABtn onClick={async () => {
        if (await act({ action: 'time_factor', id: p.id, time_factor: v }, 'Коэффициент сохранён')) setV(null);
      }}>Сохранить</ABtn>
      {st.suggested ? <button className="db line sm" style={{ justifySelf: 'start' }}
        onClick={() => setV(String(st.suggested))}>подставить {fmt(st.suggested)} из журнала</button> : null}
    </div> : null}
    <div className="hint" style={{ marginTop: 6 }}>
      {st.samples
        ? <>по журналу этой машины факт/оценка {fmt(st.median)} ({st.samples} печат.)
          {Math.abs(st.suggested - cur) >= 0.05 ? <> — стоит поставить <b>{fmt(st.suggested)}</b></> : ' — коэффициент в точку'}</>
        : 'журнала по этой машине ещё нет: выставьте по опыту, после первых печатей появится подсказка'}
    </div>
  </div>;
}

// Сопла принтера (OpenTodo f71f4973). У каждого сопла СВОЙ machine-профиль: в нём лежит
// nozzle_diameter, и слайсить процессом сопла 0.6 по профилю 0.4 нельзя — ширины линий и
// расход посчитались бы по чужому диаметру. Основное сопло — то, что физически стоит сейчас:
// им же подменяется machine-профиль принтера, который читает старый код (стол, selfcheck).
function Nozzles({ p, act }) {
  const confirm = useConfirm();
  const [add, setAdd] = useState(null);      // null — форма закрыта
  const list = p.nozzles || [];

  return <div style={{ marginTop: 14, borderTop: '1px solid var(--line)', paddingTop: 12 }}>
    <div className="lab" style={{ marginBottom: 8 }}>
      <DIcon n="wrench" size={13} />Сопла
      <span className="r">
        <button className="db line sm" onClick={() => setAdd({ nozzle: '', machine: '', note: '' })}>
          <DIcon n="plus" size={13} />Сопло
        </button>
      </span>
    </div>
    {list.length ? list.map((n) => <div key={n.id} className="setrow">
      <span className="sl" style={{ width: 150, flex: '0 0 auto' }}>
        {n.nozzle} мм
        {n.is_default ? <span className="stb amber" style={{ marginLeft: 6 }}>стоит</span> : null}
      </span>
      <span className="mono" style={{ flex: 1, color: n.exists ? 'var(--muted)' : 'var(--red)',
        fontSize: 13, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {n.machine}{n.exists ? '' : ' — файл не найден'}
      </span>
      {n.is_default ? null : <button className="ib" style={{ width: 30, height: 30 }}
        title="Это сопло стоит сейчас"
        onClick={() => act({ action: 'nozzle_default', id: p.id, nozzle_id: n.id },
          `Основное сопло: ${n.nozzle} мм`)}><DIcon n="star" size={13} /></button>}
      <button className="ib danger" style={{ width: 30, height: 30 }} title="Удалить"
        onClick={() => confirm('Удалить сопло?',
          `${n.nozzle} мм пропадёт из реестра. Профили качества с этим соплом станут считаться профилем основного сопла.`,
          () => act({ action: 'nozzle_delete', id: p.id, nozzle_id: n.id }, 'Сопло удалено'))}>
        <DIcon n="trash" size={13} />
      </button>
    </div>) : <div className="hint">сопла не заведены — слайс идёт machine-профилем принтера</div>}
    {add && <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr auto auto', gap: 10,
      marginTop: 10, alignItems: 'end' }}>
      <Field l="Диаметр">
        <input className="inp" placeholder="0.6" value={add.nozzle}
          onChange={(e) => setAdd({ ...add, nozzle: e.target.value })} />
      </Field>
      <Field l="machine .json под это сопло">
        <input className="inp mono" placeholder="путь к профилю" value={add.machine}
          onChange={(e) => setAdd({ ...add, machine: e.target.value })} />
      </Field>
      {/* Диаметр не обязателен: пустым его прочитает сервер из самого профиля. */}
      <ABtn disabled={!add.machine.trim()} onClick={async () => {
        if (await act({ action: 'nozzle_add', id: p.id, ...add }, 'Сопло добавлено')) setAdd(null);
      }}>Добавить</ABtn>
      <button className="db line" onClick={() => setAdd(null)}>Отмена</button>
    </div>}
  </div>;
}

function AddPrinter({ act }) {
  const [f, setF] = useState({ name: '', machine: '', process: '', filament: '' });
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  return <div className="card">
    <div className="lab"><DIcon n="plus" size={13} />Добавить принтер</div>
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr auto', gap: 12 }}>
      <Field l="Название">
        <input className="inp" placeholder="напр. K1 Max" value={f.name}
          onChange={(e) => set('name', e.target.value)} />
      </Field>
      <Field l="machine .json">
        <input className="inp mono" placeholder="путь" value={f.machine}
          onChange={(e) => set('machine', e.target.value)} />
      </Field>
      <Field l="process папка">
        <input className="inp mono" placeholder="путь" value={f.process}
          onChange={(e) => set('process', e.target.value)} />
      </Field>
      <Field l="filament папка">
        <input className="inp mono" placeholder="путь" value={f.filament}
          onChange={(e) => set('filament', e.target.value)} />
      </Field>
      {/* Пути проверяет сервер (файл/папка должны существовать) — здесь только «имя не пустое». */}
      <ABtn style={{ alignSelf: 'end' }} disabled={!f.name.trim()} onClick={async () => {
        if (await act({ action: 'add', ...f }, 'Принтер добавлен')) {
          setF({ name: '', machine: '', process: '', filament: '' });
        }
      }}>Добавить</ABtn>
    </div>
    <div className="hint" style={{ marginTop: 10 }}>
      профили используются ElegooSlicer при автослайсе и «Открыть в слайсере»
    </div>
  </div>;
}
