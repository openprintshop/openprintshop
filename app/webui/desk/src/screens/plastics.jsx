// plastics.jsx — «Пластики»: склад по цветам, катушки, карточка материала.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Empty, Drawer, Bar, Field } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { money, num, grams, curSym } from '../fmt.jsx';
import { Screen, Search, Split, ABtn, DetHead, PickOne, useConfirm } from '../parts.jsx';

const { useState, useEffect, useContext, useMemo, useRef } = React;

// Ёмкость полосы остатка: катушка = 1 кг, поэтому «сколько кг помещается» = число цветов.
// Своей ёмкости бэкенд не отдаёт, а без знаменателя полоса не рисуется вообще.
const capOf = (x) => Math.max(1000 * ((x.colors || []).length || 1), Number(x.stock) || 0);

// Красная полоса = хотя бы один цвет ниже своего порога (порог задаётся по цвету, не по материалу).
const isLowPlastic = (x) => Object.values(x.color_stock || {})
  .some((s) => (s.stock || 0) <= (s.low || 0));

// Значение селекта «категория витрины». Пустая строка = «авто»: сервер сам подставит
// category_hint по типу и названию, поэтому её и отправляем как есть.
const AUTO_CAT = '';

export function PlasticsScreen() {
  const { drawer, setDrawer, toast } = useContext(AppCtx);
  const [sel, setSel] = useState(null);
  const [q, setQ] = useState('');
  const p = useApi('adminPlastics', [], []);
  const shades = useApi('adminShades', [], []);
  // Все катушки разом: полосе принтера (CC2 Control) нужны вопросы лотков и выбор из всего склада.
  const sp = useApi('adminSpools', [0], []);
  // Пресеты качества нужны только ради «лучшего профиля печати» материала — своего
  // эндпоинта под этот список нет, берём тот же, что и экран пресетов.
  const presets = useApi('adminPresets', [], []);
  const list = (p.data && p.data.plastics) || [];

  const shown = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return ql ? list.filter((x) => String(x.name || '').toLowerCase().includes(ql)) : list;
  }, [list, q]);

  const cur = list.find((x) => x.id === sel) || null;

  // «Докупить»: цвета, где остаток ниже порога — сводка по всем материалам.
  const low = useMemo(() => {
    const out = [];
    list.forEach((pl) => Object.entries(pl.color_stock || {}).forEach(([c, s]) => {
      if ((s.stock || 0) <= (s.low || 0)) out.push({ plastic: pl.name, color: c, ...s });
    }));
    return out;
  }, [list]);

  const buyText = low.slice(0, 6).map((l) => `${l.plastic} ${l.color} (${grams(l.stock)})`).join(' · ')
    + (low.length > 6 ? ` · и ещё ${low.length - 6}` : '');

  const listPane = <>
    <div className="ms-tools">
      <Search value={q} onChange={setQ} placeholder="Материал…" />
      <button className="ib" title="Обновить" onClick={p.reload}><DIcon n="refresh" size={16} /></button>
      <button className="db" onClick={() => setDrawer('new')}>
        <DIcon n="plus" size={15} />Новый пластик
      </button>
    </div>

    {low.length ? <div className="warn" style={{ marginBottom: 14 }}>
      <DIcon n="spool" size={15} />
      <span style={{ flex: 1, minWidth: 0 }}>Докупить: {buyText}</span>
      <ABtn kind="sm line" style={{ marginLeft: 'auto', color: 'var(--amber)', borderColor: 'rgba(255,176,32,.35)', flexShrink: 0 }}
        onClick={async () => {
          try {
            await call('adminTask', { action: 'add', title: 'Заказать пластик: ' + buyText });
            toast('Задача «Заказать пластик» создана', { kind: 'ok' });
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }}>В задачи</ABtn>
    </div> : null}

    <Cc2Bar sp={sp} plastics={list} onChanged={p.reload} />

    <Screen state={p}>{() => shown.length ? <>
      <div className="twrap">
        <table className="tbl">
          <thead><tr>
            <th>Материал</th>
            <th style={{ width: 80 }}>Тип</th>
            <th style={{ width: 110, textAlign: 'right' }}>{curSym()}/кг</th>
            <th style={{ width: 240 }}>Остаток</th>
            <th style={{ width: 80 }}>Брак</th>
            <th style={{ width: 80 }}>Коэф</th>
            <th style={{ width: 100 }}>Расчётов</th>
          </tr></thead>
          <tbody>
            {shown.map((x) => {
              const cols = x.colors || [];
              const css = x.color_css || {};
              const bad = isLowPlastic(x);
              return <tr key={x.id} className={'row' + (x.id === sel ? ' sel' : '')}
                onClick={() => setSel(x.id)}>
                <td><div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {cols.length ? <span style={{ display: 'flex' }}>
                    {cols.slice(0, 4).map((c, i) => <span key={c} className="swatch"
                      style={{ background: css[c] || 'var(--raise)', width: 20, height: 20, marginLeft: i ? -7 : 0 }} />)}
                  </span> : null}
                  <span className="oname">{x.name}</span>
                  {x.is_default ? <span className="stb amber">★ умолч.</span> : null}
                  {!x.is_active ? <span className="stb gray">выкл</span> : null}
                </div></td>
                <td className="mono faint">{x.material}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{money(x.price)}</td>
                <td><div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <div style={{ flex: 1 }}>
                    <Bar pct={(x.stock / capOf(x)) * 100} h={6} color={bad ? 'var(--red)' : 'var(--price)'} />
                  </div>
                  <span className="mono" style={{ fontSize: 12, color: bad ? 'var(--red)' : 'var(--faint)', width: 64, textAlign: 'right' }}>
                    {grams(x.stock)}
                  </span>
                </div></td>
                <td className="mono faint">{x.failure == null ? '—' : x.failure + '%'}</td>
                <td className="mono faint">×{x.factor == null ? 1 : x.factor}</td>
                <td className="mono faint">{num(x.uses)}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
      <div className="hint" style={{ marginTop: 10 }}>
        полоса остатка — сумма по цветам к ёмкости 1 кг на цвет · красная, если хотя бы один цвет
        ниже своего порога · порог алерта настраивается по цвету в карточке материала
      </div>
    </> : <Empty ic="spool" t="Материалов нет" ds="Добавьте первый пластик"
      cta="Новый пластик" onCta={() => setDrawer('new')} />}
    </Screen>
  </>;

  return <>
    <Split width={1240} list={listPane}
      detail={cur ? <PlasticCard key={cur.id} p={cur} shades={(shades.data && shades.data.shades) || []}
        types={(p.data && p.data.material_types) || []}
        cats={(p.data && p.data.plastic_categories) || []}
        qualities={(presets.data && presets.data.quality) || []}
        failureDefault={p.data && p.data.failure_default}
        onSaved={p.reload} /> : <PickOne what="материал" />} />
    {drawer === 'new' && <NewPlasticDrawer onClose={() => setDrawer(null)} onSaved={p.reload}
      types={(p.data && p.data.material_types) || []}
      cats={(p.data && p.data.plastic_categories) || []} />}
  </>;
}

function PlasticCard({ p, shades, types, cats, qualities, failureDefault, onSaved }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [f, setF] = useState({ ...p });
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  // Активность и «по умолчанию» переключаются ещё и кнопками в шапке. Без пересинхронизации
  // форма помнила бы прежнее состояние галочки, и следующее «Сохранить» щёлкнуло бы флаг
  // обратно — мастер выключил бы материал дважды и получил включённый. Флаги живут в шапке,
  // поэтому синхронизируем только их: набранный, но не сохранённый текст трогать нельзя.
  useEffect(() => {
    setF((s) => ({ ...s, is_active: p.is_active, is_default: p.is_default }));
  }, [p.is_active, p.is_default]);
  const spools = useApi('adminSpools', [p.id], [p.id]);
  const photoRef = useRef(null);

  const act = async (action, msg) => {
    try {
      await call('adminPlastic', { action, id: p.id });
      toast(msg, { kind: 'ok' });
      onSaved();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  // Единая точка сохранения карточки. Обработчик 'save' в webapp.py перезаписывает ВСЕ поля
  // разом: не передашь failure/factor/category/best_quality_id — сервер молча сбросит их
  // в NULL/1/авто-категорию. Поэтому базой всегда берём сохранённую запись p, а сверху
  // кладём то, что реально меняем.
  const savePlastic = async (patch) => {
    const b = {
      action: 'save', id: p.id,
      name: p.name, price: p.price, density: p.density, material: p.material,
      category: p.category || AUTO_CAT, description: p.description || '',
      failure: p.failure == null ? '' : p.failure,
      factor: p.factor == null ? '' : p.factor,
      best_quality_id: p.best_quality_id || null,
      colors: p.colors || [],
      spool_weight: p.spool_weight == null ? '' : p.spool_weight,
      spool_note: p.spool_note || '',
      ...patch,
    };
    await call('adminPlastic', b);
  };

  const sub = [
    p.material || '—',
    `${money(p.price)}/кг`,
    `плотность ${p.density}`,
    `расчётов ${num(p.uses)}`,
    p.failure == null ? null : `брак ${p.failure}%`,
    `коэф ×${p.factor == null ? 1 : p.factor}`,
    p.category ? `категория ${p.category}` : null,
    p.spool_weight ? `катушка ${num(p.spool_weight)} г` : null,
  ].filter(Boolean).join(' · ');

  return <div className="od">
    <DetHead
      title={<>{p.name}{p.is_default
        ? <span className="stb amber" style={{ marginLeft: 12, verticalAlign: 4 }}>★ по умолчанию</span> : null}</>}
      sub={sub}
      right={<>
        <button className="ib" title="Сделать по умолчанию"
          onClick={() => act('default', 'Материал назначен по умолчанию')}><DIcon n="star" /></button>
        <button className="ib" title={p.is_active ? 'Выключить' : 'Включить'}
          onClick={() => act('toggle', p.is_active ? 'Материал выключен' : 'Материал включён')}><DIcon n="check" /></button>
        <span style={{ width: 10 }} />
        <button className="ib danger" title="Удалить"
          onClick={() => confirm('Удалить материал?',
            `«${p.name}» исчезнет из каталога и калькулятора. История расчётов сохранится.`,
            () => act('delete', 'Материал удалён'))}><DIcon n="trash" /></button>
      </>} />
    <div className="od-body">
      <div className="od-main">
        <Sec t="Фото катушки" ic="camera">
          <div style={{ display: 'flex', gap: 14, alignItems: 'center' }}>
            <div onClick={() => photoRef.current.click()} title="Загрузить или заменить фото"
              style={{ width: 92, height: 92, borderRadius: 12, overflow: 'hidden', cursor: 'pointer',
                border: '1px solid var(--line)', background: 'var(--raise)', display: 'flex',
                alignItems: 'center', justifyContent: 'center', color: 'var(--faint)', flexShrink: 0 }}>
              {p.photo ? <img src={p.photo} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                : <DIcon n="spool" size={24} />}
            </div>
            <div className="hint" style={{ flex: 1 }}>
              фото видно клиенту в каталоге материалов · до 12 МБ, сервер сам ужмёт
            </div>
          </div>
          <input type="file" accept="image/*" ref={photoRef} style={{ display: 'none' }}
            onChange={(e) => {
              const file = e.target.files && e.target.files[0];
              e.target.value = '';    // иначе повторный выбор того же файла не даст события
              if (!file) return;
              // Лимит держит и сервер, но там он превращается в «Некорректное изображение» —
              // мастеру полезнее узнать настоящую причину до долгой загрузки.
              if (file.size > 12 * 1024 * 1024) {
                toast('Фото больше 12 МБ — выберите поменьше', { kind: 'warn2' });
                return;
              }
              const rd = new FileReader();
              rd.onload = async () => {
                try {
                  await call('adminPhoto', 'plastic', p.id, rd.result, false);
                  toast('Фото обновлено', { kind: 'ok' });
                  onSaved();
                } catch (err) { toast(err.message, { kind: 'warn2' }); }
              };
              rd.readAsDataURL(file);
            }} />
        </Sec>

        <SpoolPhotosSec p={p} onSaved={onSaved} />
        <StockSec p={p} onSaved={onSaved} />
        <ColorsSec p={p} shades={shades} savePlastic={savePlastic} onSaved={onSaved} />
        <SpoolsSec p={p} spools={spools} onSaved={onSaved} />

        <Sec t="Параметры" ic="pencil" span2>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr 1fr 1fr', gap: 12 }}>
            <Field l="Название">
              <input className="inp" value={f.name || ''} onChange={(e) => set('name', e.target.value)} /></Field>
            <Field l="Тип материала">
              <select className="sel" value={f.material || ''} onChange={(e) => set('material', e.target.value)}>
                {/* текущий тип может быть не из списка (старая запись) — добавляем его явно */}
                {(types.includes(f.material) ? types : [f.material, ...types].filter(Boolean))
                  .map((t) => <option key={t} value={t}>{t}</option>)}
              </select></Field>
            <Field l={`Цена, ${curSym()}/кг`}>
              <input className="inp mono" type="number" step="0.01" value={f.price}
                onChange={(e) => set('price', e.target.value)} /></Field>
            <Field l="Плотность">
              <input className="inp mono" type="number" step="0.01" value={f.density}
                onChange={(e) => set('density', e.target.value)} /></Field>
            <Field l={`% брака (пусто = ${failureDefault == null ? 'стандарт' : failureDefault + '%'})`}>
              {/* пусто = берётся общий процент из настроек, поэтому null не подменяем нулём */}
              <input className="inp mono" type="number" step="0.1" value={f.failure ?? ''}
                onChange={(e) => set('failure', e.target.value)} /></Field>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 2fr', gap: 12, marginTop: 14 }}>
            <Field l="Коэф. сложности">
              <input className="inp mono" type="number" step="0.01" value={f.factor ?? 1}
                onChange={(e) => set('factor', e.target.value)} /></Field>
            <Field l="Категория витрины">
              <select className="sel" value={f.category || ''} onChange={(e) => set('category', e.target.value)}>
                <option value={AUTO_CAT}>— авто (по типу/названию)</option>
                {cats.map((c) => <option key={c} value={c}>{c}</option>)}
              </select></Field>
            <Field l="Лучший профиль печати (подставится клиенту сам)">
              <select className="sel" value={f.best_quality_id || 0}
                onChange={(e) => set('best_quality_id', Number(e.target.value))}>
                <option value={0}>— стандартный (★ из пресетов)</option>
                {qualities.map((qq) => <option key={qq.id} value={qq.id}>{qq.label}</option>)}
              </select></Field>
          </div>
          {/* Тара катушки — не для клиента, а для сверки склада весами: остаток филамента =
              показание весов − вес пустой катушки. Оговорка нужна там, где катушка зависит
              от партии (картон/пластик), и одним числом положение не описать. */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 3fr', gap: 12, marginTop: 14 }}>
            <Field l="Вес пустой катушки, г">
              <input className="inp mono" type="number" step="1" value={f.spool_weight ?? ''}
                onChange={(e) => set('spool_weight', e.target.value)} /></Field>
            <Field l="Оговорка к катушке (партии, материал шпули)">
              <input className="inp" value={f.spool_note || ''}
                onChange={(e) => set('spool_note', e.target.value)} /></Field>
          </div>
          <div style={{ display: 'flex', gap: 12, marginTop: 14, alignItems: 'flex-end' }}>
            <Field l="Описание">
              <input className="inp" style={{ width: 420 }} value={f.description || ''}
                onChange={(e) => set('description', e.target.value)} /></Field>
            <label className="chk"><input type="checkbox" checked={!!f.is_active}
              onChange={(e) => set('is_active', e.target.checked)} />Активен</label>
            <label className="chk"><input type="checkbox" checked={!!f.is_default}
              onChange={(e) => set('is_default', e.target.checked)} />По умолчанию</label>
            <ABtn style={{ marginLeft: 'auto' }} onClick={async () => {
              try {
                await savePlastic({
                  name: f.name, price: f.price, density: f.density, material: f.material,
                  category: f.category || AUTO_CAT, description: f.description || '',
                  failure: f.failure ?? '', factor: f.factor ?? '',
                  best_quality_id: Number(f.best_quality_id) || null,
                  spool_weight: f.spool_weight ?? '', spool_note: f.spool_note || '',
                });
                // Активность и «по умолчанию» действие 'save' не читает вовсе — это отдельные
                // действия toggle/default. Дёргаем их только если галочка реально изменилась.
                if (!!f.is_active !== !!p.is_active) {
                  await call('adminPlastic', { action: 'toggle', id: p.id });
                }
                if (f.is_default && !p.is_default) {
                  await call('adminPlastic', { action: 'default', id: p.id });
                }
                toast('Материал сохранён', { kind: 'ok' });
                onSaved();
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            }}>Сохранить</ABtn>
          </div>
        </Sec>
      </div>
    </div>
  </div>;
}

// Общий склад материала: остаток и порог одной «виртуальной катушкой». Нужен там, где цвета
// не заведены — иначе учёт идёт по цветам, а это поле остаётся нулём.
/**
 * Фото катушек по цветам. Раньше их можно было только смотреть (файлы клали на диск руками),
 * а из админки — никак. Теперь каждый цвет = плитка: есть фото — показываем (клик — крупно),
 * кнопка-камера в углу грузит/заменяет. Файл живёт в data/spool_photos/{id}/{цвет}.jpg.
 */
function SpoolPhotosSec({ p, onSaved }) {
  const { toast, lightbox } = useContext(AppCtx);
  const refs = useRef({});
  const colors = p.colors || [];
  const spools = p.spools || {};
  const css = p.color_css || {};
  if (!colors.length) {
    return <Sec t="Фото катушек по цветам" ic="camera">
      <div className="hint">Сначала добавьте цвета материала — фото задаётся на каждый цвет.</div>
    </Sec>;
  }
  const upload = (color, file) => {
    if (!file) return;
    if (file.size > 12 * 1024 * 1024) { toast('Фото больше 12 МБ — выберите поменьше', { kind: 'warn2' }); return; }
    const rd = new FileReader();
    rd.onload = async () => {
      try {
        await call('adminSpoolPhoto', p.id, color, rd.result);
        toast(`Фото катушки «${color}» обновлено`, { kind: 'ok' });
        onSaved();
      } catch (e) { toast(e.message, { kind: 'warn2' }); }
    };
    rd.readAsDataURL(file);
  };
  return <Sec t="Фото катушек по цветам" ic="camera">
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14 }}>
      {colors.map((c) => {
        const url = spools[c];
        return <div key={c} style={{ width: 96 }}>
          <div title={url ? 'Клик — посмотреть крупно' : 'Загрузить фото катушки'}
            onClick={() => (url ? lightbox(url) : refs.current[c] && refs.current[c].click())}
            style={{ width: 96, height: 96, borderRadius: 12, overflow: 'hidden', cursor: 'pointer',
              border: '1px solid var(--line)', background: 'var(--raise)', display: 'flex',
              alignItems: 'center', justifyContent: 'center', color: 'var(--faint)', position: 'relative' }}>
            {url ? <img src={url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
              : <DIcon n="camera" size={22} />}
            <button className="ib" title="Загрузить или заменить фото"
              onClick={(e) => { e.stopPropagation(); if (refs.current[c]) refs.current[c].click(); }}
              style={{ position: 'absolute', right: 5, bottom: 5, width: 26, height: 26,
                background: 'rgba(6,9,12,.72)' }}><DIcon n="camera" size={13} /></button>
          </div>
          <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span className="swatch" style={{ background: css[c] || 'var(--raise)', width: 12, height: 12 }} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c}</span>
          </div>
          <input type="file" accept="image/*" ref={(el) => { refs.current[c] = el; }} style={{ display: 'none' }}
            onChange={(e) => { const f = e.target.files && e.target.files[0]; e.target.value = ''; upload(c, f); }} />
        </div>;
      })}
    </div>
    <div className="hint" style={{ marginTop: 10 }}>
      это фото видит клиент, когда выбирает цвет в каталоге · до 12 МБ, сервер сам ужмёт
    </div>
  </Sec>;
}

function StockSec({ p, onSaved }) {
  const { toast } = useContext(AppCtx);
  const [stock, setStock] = useState(p.stock ?? '');
  const [low, setLow] = useState(p.low ?? '');
  const many = (p.colors || []).length > 1;
  const res = Math.min(Number(p.reserved) || 0, Number(p.stock) || 0);

  return <Sec t="Склад материала" ic="layers">
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 12, alignItems: 'flex-end' }}>
      <Field l={p.cc2_stock ? 'Общий остаток, г — ведёт принтер' : 'Общий остаток, г'}>
        {/* катушки без цвета на принтере (CC2 Control): остаток — их сумма, руками не правится */}
        <input className="inp mono" type="number" step="1" value={p.cc2_stock ? (p.stock ?? '') : stock}
          disabled={!!p.cc2_stock} title={p.cc2_stock ? 'сумма катушек принтера — меняется весами и печатью' : ''}
          onChange={(e) => setStock(e.target.value)} /></Field>
      <Field l="Порог «мало», г">
        <input className="inp mono" type="number" step="1" value={low}
          onChange={(e) => setLow(e.target.value)} /></Field>
      <ABtn kind="soft" onClick={async () => {
        try {
          await call('adminPlastic', { action: 'stock', id: p.id, stock: stock || 0, low: low || 0 });
          toast('Склад материала сохранён', { kind: 'ok' });
          onSaved();
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Сохранить</ABtn>
    </div>
    {res > 0 ? <div className="hint" style={{ marginTop: 10 }}>
      зарезервировано под активные заказы: <span className="mono">{grams(res)}</span> —
      филамент списывается по факту, при отметке «напечатано»
    </div> : null}
    {many ? <div className="hint" style={{ marginTop: 8 }}>
      у материала несколько цветов — учёт идёт по цветам ниже, общий остаток обычно остаётся нулём
    </div> : null}
  </Sec>;
}

function ColorsSec({ p, shades, savePlastic, onSaved }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [name, setName] = useState('');
  const [hex, setHex] = useState('#2f6fd6');
  // Черновики правок по цветам: {цвет: {stock, low, rename}}. Правки применяются построчно,
  // чтобы случайная опечатка в одном цвете не мешала сохранить другой.
  const [ed, setEd] = useState({});
  const edit = (c, k, v) => setEd((s) => ({ ...s, [c]: { ...(s[c] || {}), [k]: v } }));
  const stock = p.color_stock || {};
  const css = p.color_css || {};
  const colors = p.colors || [];
  // цвета, чей остаток ведёт принтер (CC2 Control): число — сумма катушек, правится весами
  const owned = new Set(p.cc2_colors || []);

  // Список цветов материала лежит одной строкой в самом пластике, поэтому переименование
  // и удаление цвета — это пересохранение всего списка действием 'save'.
  const putColors = async (next, okMsg) => {
    await savePlastic({ colors: next });
    toast(okMsg, { kind: 'ok' });
    onSaved();
  };

  return <Sec t="Остатки по цветам" ic="spool">
    {colors.length ? colors.map((c) => {
      const s = stock[c] || {};
      // шкала считается от порога: своей ёмкости цвета бэкенд не отдаёт, а порог задан мастером
      const cap = s.low ? s.low * 4 : 1000;
      const isLow = (s.stock || 0) <= (s.low || 0);
      const color = isLow ? 'var(--red)' : ((s.stock || 0) < cap / 2 ? 'var(--amber)' : 'var(--price)');
      const d = ed[c] || {};
      const nm = d.rename == null ? c : d.rename;
      return <div key={c} className="crow2">
        <span className="swatch" style={{ background: css[c] || 'var(--raise)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13.5, marginBottom: 6 }}>
            <span style={{ fontWeight: 600 }}>{c}</span>
            <span className="mono" style={{ color: isLow ? 'var(--red)' : 'var(--faint)' }}>
              {grams(s.stock)} · порог {grams(s.low)}{isLow ? ' · ниже порога' : ''}
            </span>
          </div>
          <Bar pct={(s.stock / cap) * 100} h={6} color={color} />
        </div>
        <input className="inp" style={{ width: 150 }} value={nm} title="Переименовать цвет"
          onChange={(e) => edit(c, 'rename', e.target.value)} />
        <ABtn kind="sm line" disabled={!nm.trim() || nm.trim() === c} onClick={async () => {
          try {
            await putColors(colors.map((x) => (x === c ? nm.trim() : x)),
              `Цвет переименован в «${nm.trim()}»`);
            setEd((s2) => ({ ...s2, [c]: {} }));
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }}>Имя</ABtn>
        <input className="inp mono" style={{ width: 92 }} type="number" placeholder="остаток"
          value={owned.has(c) ? Math.round(s.stock || 0) : (d.stock == null ? (s.stock ?? '') : d.stock)}
          disabled={owned.has(c)}
          title={owned.has(c) ? 'остаток ведёт принтер: сумма его катушек этого цвета' : ''}
          onChange={(e) => edit(c, 'stock', e.target.value)} />
        <input className="inp mono" style={{ width: 84 }} type="number" placeholder="порог"
          value={d.low == null ? (s.low ?? '') : d.low}
          onChange={(e) => edit(c, 'low', e.target.value)} />
        <ABtn kind="sm line" onClick={async () => {
          try {
            await call('adminPlastic', {
              action: 'color_stock', id: p.id, color: c,
              stock: d.stock == null ? (s.stock || 0) : d.stock,
              low: d.low == null ? (s.low || 0) : d.low,
            });
            toast(`Остаток цвета «${c}» сохранён`, { kind: 'ok' });
            setEd((s2) => ({ ...s2, [c]: {} }));
            onSaved();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }}>ОК</ABtn>
        <button className="ib danger" title="Удалить цвет"
          onClick={() => confirm('Удалить цвет?',
            `«${c}» исчезнет из выбора у клиента. Остаток и порог, заданные для этого цвета, `
            + 'учитываться перестанут. Катушки этого цвета останутся.',
            async () => {
              try {
                await putColors(colors.filter((x) => x !== c), `Цвет «${c}» удалён`);
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            })}><DIcon n="trash" /></button>
      </div>;
    }) : <div className="hint">цвета не заведены — остаток учитывается общей суммой по материалу</div>}

    <div className="frow" style={{ marginTop: 12, alignItems: 'center' }}>
      <select className="sel" style={{ flex: 1 }} value={name} onChange={(e) => {
        setName(e.target.value);
        const sh = shades.find((s) => s.name === e.target.value);
        if (sh) setHex(sh.hex);
      }}>
        <option value="">— именованный оттенок —</option>
        {shades.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
      </select>
      <input className="inp" style={{ flex: 1 }} value={name} placeholder="или свой цвет"
        onChange={(e) => setName(e.target.value)} />
      <input type="color" className="cpick" value={hex} onChange={(e) => setHex(e.target.value)}
        title="точный оттенок этого пластика" />
      <ABtn kind="soft sm" disabled={!name.trim()} icon="plus" onClick={async () => {
        const n = name.trim();
        try {
          if (colors.includes(n)) { toast('Такой цвет уже есть', { kind: 'warn2' }); return; }
          await call('adminShade', { action: 'add', name: n, hex });
          // поля add_color обработчик не знает — цвет добавляется пересохранением списка
          await putColors(colors.concat([n]), `Цвет «${n}» добавлен`);
          setName('');
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Цвет</ABtn>
    </div>
    <div className="hint" style={{ marginTop: 8 }}>
      оттенок — как выглядит цвет именно этого пластика (кружок-образец в каталоге) ·
      при падении ниже порога цвет попадает в полосу «Докупить» ·
      переименование не переносит заданный остаток — задайте его заново
      {owned.size ? ' · серый остаток ведёт принтер (сумма катушек CC2 Control), у такого цвета правится только порог' : ''}
    </div>
  </Sec>;
}

// Где стоит катушка принтера: лоток Canvas 0..3, внешний держатель 4, иначе — на полке.
const slotName = (slot) => (slot == null ? 'на полке'
  : slot === 4 ? 'внешний держатель' : `лоток ${slot + 1}`);
const isHex = (c) => /^#[0-9a-f]{6}$/i.test(c || '');

/**
 * Катушки пластика. Есть принтер с учётом катушек CC2 Control (spools.data.cc2) — склад ведёт
 * он: остаток катушки списывает принтер по тому, что реально выдавил, а приход, весы, поправка
 * и архив уходят в его службу. Без него — прежний учёт Print Studio.
 */
function SpoolsSec({ p, spools, onSaved }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const cc2 = spools.data && spools.data.cc2;
  const css = p.color_css || {};
  const blank = { color: '', net: 1000, rem: '', tare: p.spool_weight || '', cost: '', supplier: '', low: '', hex: '' };
  const [f, setF] = useState(blank);
  const [edit, setEdit] = useState(null);       // {id, kind: 'weigh'|'actual', value, tare}
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  const items = (spools.data && spools.data.spools) || [];
  const ST = { active: 'green', empty: 'gray', archived: 'blue' };
  const done = () => { spools.reload(); if (onSaved) onSaved(); };
  const act = async (body, ok) => {
    try {
      await call('adminSpool', body);
      toast(ok, { kind: 'ok' });
      setEdit(null);
      done();
      return true;
    } catch (e) { toast(e.message, { kind: 'warn2' }); return false; }
  };
  // оттенок нового прихода: выбранный руками, иначе образец цвета палитры (если он сплошной)
  const pickHex = f.hex || (isHex(css[f.color]) ? css[f.color] : '#cccccc');
  // у принтера в строке прихода девять колонок — поля ужаты, иначе подсказки обрезаются
  const cmp = cc2 ? { padding: '10px 9px' } : undefined;

  return <Sec t="Катушки · учёт" ic="layers" span2
    right={<span className="mono">{num(items.length)}{cc2 ? ' · CC2 Control' : ''}</span>}>
    {cc2 ? <div className="hint" style={{ marginBottom: 10 }}>
      остаток катушек ведёт принтер «{cc2.printer}»: он списывает то, что реально выдавил из лотка
      (с продувками и упавшими печатями) · приход, весы и архив уходят на принтер
      {!cc2.online ? ` · принтер не отвечает, показан последний известный остаток${cc2.age_s != null ? ` (${Math.round(cc2.age_s / 60)} мин назад)` : ''}` : ''}
    </div> : null}

    <div style={{ display: 'grid', gap: 9, marginBottom: 12,
      gridTemplateColumns: cc2 ? '1.2fr 38px 88px 88px 72px 88px 1fr 76px auto' : '1fr 90px 90px 1fr 84px auto' }}>
      {cc2 ? <select className="sel" value={f.color} onChange={(e) => set('color', e.target.value)}>
        <option value="">— цвет —</option>
        {(p.colors || []).map((c) => <option key={c} value={c}>{c}</option>)}
      </select> : <input className="inp mono" placeholder="цвет (необязательно)" value={f.color}
        onChange={(e) => set('color', e.target.value)} />}
      {cc2 ? <input type="color" className="cpick" value={pickHex} title="цвет катушки на принтере"
        onChange={(e) => set('hex', e.target.value)} /> : null}
      <input className="inp mono" placeholder="нетто, г" style={cmp} type="number" value={f.net}
        onChange={(e) => set('net', e.target.value)} />
      {cc2 ? <input className="inp mono" placeholder="остаток" style={cmp} type="number" value={f.rem}
        title="у начатой катушки — сколько осталось; пусто = полная" onChange={(e) => set('rem', e.target.value)} /> : null}
      {cc2 ? <input className="inp mono" placeholder="тара" style={cmp} type="number" value={f.tare}
        title="вес пустой катушки — для взвешивания" onChange={(e) => set('tare', e.target.value)} /> : null}
      <input className="inp mono" placeholder={cc2 ? 'цена' : `цена, ${curSym()}`} title={`цена катушки, ${curSym()}`} style={cmp} type="number" step="0.01" value={f.cost}
        onChange={(e) => set('cost', e.target.value)} />
      <input className="inp" placeholder="поставщик" value={f.supplier}
        onChange={(e) => set('supplier', e.target.value)} />
      <input className="inp mono" placeholder={cc2 ? 'порог' : 'порог, г'} title="порог «мало», г" style={cmp} type="number" value={f.low}
        onChange={(e) => set('low', e.target.value)} />
      {/* Без принтера обязательно одно поле — вес нетто; принтеру нужен ещё и цвет */}
      <ABtn kind="soft sm" disabled={!(Number(f.net) > 0) || (cc2 && !f.color)} onClick={async () => {
        const ok = await act({
          action: 'add', plastic_id: p.id, color: f.color.trim(), net_weight: f.net, cost: f.cost,
          supplier: f.supplier.trim(), low: f.low,
          ...(cc2 ? { remaining: f.rem, tare: f.tare, hex: pickHex } : {}),
        }, cc2 ? 'Катушка заведена на принтере' : 'Катушка оприходована');
        if (ok) setF(blank);
      }}>Приход</ABtn>
    </div>

    {items.length ? items.map((s) => {
      const rem = s.live_g != null ? s.live_g : s.remaining_g;
      const ed = edit && edit.id === s.id ? edit : null;
      return <div key={s.id} className="crow2">
        <span className="swatch" style={{ background: s.hex || css[s.color] || 'var(--raise)', width: 20, height: 20 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13, flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600 }}>{s.color || 'без цвета'}</span>
            <span className={'stb ' + (ST[s.status] || 'gray')}>{s.status}</span>
            {cc2 && s.cc2_id ? <span className={'stb ' + (s.slot != null ? 'amber' : 'gray')}>{slotName(s.slot)}</span> : null}
            {cc2 && !s.cc2_id && s.status !== 'archived'
              ? <span className="stb gray" title="эту катушку принтер не видит и не списывает">только в Print Studio</span> : null}
            <span className="mono" style={{ marginLeft: 'auto', color: 'var(--faint)', fontSize: 12 }}>
              {grams(rem)} / {grams(s.net_weight_g)}{s.tare ? ' · тара ' + grams(s.tare) : ''}
              {s.cost ? ' · ' + money(s.cost) : ''}{s.supplier ? ' · ' + s.supplier : ''}
            </span>
          </div>
          <div style={{ marginTop: 6 }}>
            <Bar pct={s.pct || 0} h={5}
              color={(rem || 0) <= (s.low_threshold || 0) ? 'var(--red)' : 'var(--price)'} />
          </div>
        </div>
        {ed ? <>
          <input className="inp mono" style={{ width: 96 }} type="number" autoFocus
            placeholder={ed.kind === 'weigh' ? 'на весах, г' : 'остаток, г'} value={ed.value}
            onChange={(e) => setEdit({ ...ed, value: e.target.value })} />
          {ed.kind === 'weigh' && s.cc2_id ? <input className="inp mono" style={{ width: 74 }} type="number"
            placeholder="тара" title="вес пустой катушки" value={ed.tare}
            onChange={(e) => setEdit({ ...ed, tare: e.target.value })} /> : null}
          <ABtn kind="sm line" disabled={ed.value === ''} onClick={() => act(
            ed.kind === 'weigh'
              ? { action: 'weigh', spool_id: s.id, gross: ed.value, ...(ed.tare !== '' && Number(ed.tare) !== Number(s.tare) ? { tare: ed.tare } : {}) }
              : { action: 'correction', spool_id: s.id, actual: Number(ed.value) },
            ed.kind === 'weigh' ? 'Катушка взвешена' : 'Остаток поправлен')}>ОК</ABtn>
          <button className="ib" title="Отмена" onClick={() => setEdit(null)}><DIcon n="close" size={14} /></button>
        </> : <>
          <button className="db sm line" title="Поставить катушку на весы: остаток = вес минус тара"
            onClick={() => setEdit({ id: s.id, kind: 'weigh', value: '', tare: s.tare || p.spool_weight || '' })}>Весы</button>
          <button className="db sm line" title="Ввести остаток филамента"
            onClick={() => setEdit({ id: s.id, kind: 'actual', value: '' })}>Остаток</button>
        </>}
        {cc2 && !s.cc2_id && s.status !== 'archived' ? <button className="db sm line"
          title="Завести эту катушку на принтере — дальше её остаток ведёт он"
          onClick={() => act({ action: 'cc2_push', spool_id: s.id }, 'Катушка заведена на принтере')}>На принтер</button> : null}
        {s.status !== 'archived' && <button className="db sm line"
          onClick={() => confirm('Катушку в архив?',
            cc2 && s.cc2_id ? 'Катушка уйдёт в архив и на принтере: выйдет из лотка и пропадёт из выбора.'
              : 'Катушка исчезнет из выбора и из остатка.',
            () => act({ action: 'status', spool_id: s.id, status: 'archived' }, 'Катушка в архиве'),
            'В архив')}>Архив</button>}
      </div>;
    }) : <div className="hint">катушки не заведены — списание идёт по общему остатку</div>}

    {cc2 && (cc2.unlinked || []).length ? <Cc2Unlinked p={p} cc2={cc2} items={items} act={act} /> : null}
  </Sec>;
}

/**
 * Катушки, заведённые прямо на принтере (их «Какая это катушка?» → новая) или ещё не связанные:
 * их остаток принтер знает, а Print Studio — нет. Связь ставится только руками — по имени
 * ничего не угадываем.
 */
function Cc2Unlinked({ p, cc2, items, act }) {
  const [pick, setPick] = useState({});         // {cc2_id: {color, spool}}
  const set = (id, k, v) => setPick((s) => ({ ...s, [id]: { ...(s[id] || {}), [k]: v } }));
  const local = items.filter((s) => !s.cc2_id && s.status !== 'archived');
  return <div style={{ marginTop: 14, borderTop: '1px solid var(--line)', paddingTop: 12 }}>
    <div className="hint" style={{ marginBottom: 8 }}>
      на принтере есть катушки без пары в Print Studio — возьмите в учёт этого пластика нужные
    </div>
    {cc2.unlinked.map((u) => {
      const pk = pick[u.cc2_id] || {};
      return <div key={u.cc2_id} className="crow2">
        <span className="swatch" style={{ background: u.hex || 'var(--raise)', width: 20, height: 20 }} />
        <div style={{ flex: 1, minWidth: 0, fontSize: 13 }}>
          <span style={{ fontWeight: 600 }}>{u.name || 'без имени'}</span>
          <span className="mono faint" style={{ marginLeft: 8, fontSize: 12 }}>
            {u.material} · {grams(u.remaining_g)} / {grams(u.net_weight_g)} · {slotName(u.slot)}
          </span>
        </div>
        {local.length ? <select className="sel" style={{ width: 170 }} value={pk.spool || ''}
          onChange={(e) => set(u.cc2_id, 'spool', e.target.value)}>
          <option value="">новая катушка</option>
          {local.map((s) => <option key={s.id} value={s.id}>это #{s.id} {s.color || 'без цвета'} · {grams(s.remaining_g)}</option>)}
        </select> : null}
        {!pk.spool ? <select className="sel" style={{ width: 150 }} value={pk.color || ''}
          onChange={(e) => set(u.cc2_id, 'color', e.target.value)}>
          <option value="">— цвет —</option>
          {(p.colors || []).map((c) => <option key={c} value={c}>{c}</option>)}
        </select> : null}
        <ABtn kind="sm line" disabled={!pk.spool && !pk.color && (p.colors || []).length > 0}
          onClick={() => (pk.spool
            ? act({ action: 'cc2_link', spool_id: Number(pk.spool), cc2_id: u.cc2_id }, 'Катушки связаны')
            : act({ action: 'cc2_adopt', cc2_id: u.cc2_id, plastic_id: p.id, color: pk.color || '' }, 'Катушка взята в учёт'))}>
          {pk.spool ? 'Связать' : 'В учёт'}</ABtn>
      </div>;
    })}
  </div>;
}

/**
 * Полоса принтера над списком материалов: кто ведёт склад катушек, включён ли учёт и вопросы
 * лотков «Какая это катушка?». Вопрос можно не отвечать сразу: принтер копит выдавленное и
 * спишет его с той катушки, что выберут позже.
 */
function Cc2Bar({ sp, plastics, onChanged }) {
  const { toast } = useContext(AppCtx);
  const cc2 = sp.data && sp.data.cc2;
  const [pick, setPick] = useState({});         // {slot: cc2_id}
  if (!cc2) return null;
  const spools = ((sp.data && sp.data.spools) || []).filter((s) => s.cc2_id && s.status !== 'archived');
  const pname = (id) => (plastics.find((x) => x.id === id) || {}).name || '';
  const act = async (body, ok) => {
    try {
      await call('adminSpool', body);
      toast(ok, { kind: 'ok' });
      sp.reload();
      if (onChanged) onChanged();
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };
  const off = cc2.online && !cc2.enabled;
  return <>
    {(off || !cc2.online || !cc2.available) ? <div className="warn" style={{ marginBottom: 14 }}>
      <DIcon n="spool" size={15} />
      <span style={{ flex: 1, minWidth: 0 }}>
        {!cc2.online ? `Принтер «${cc2.printer}» не отвечает — остатки катушек на момент последней связи`
          : !cc2.available ? 'CC2 Control не загрузил склад катушек — остатки не обновляются'
            : 'Учёт катушек на принтере выключен: расход печати сейчас никто не считает'}
      </span>
      {off ? <ABtn kind="sm line" onClick={() => act({ action: 'cc2_enable', on: true }, 'Учёт катушек включён')}>
        Включить учёт</ABtn> : null}
    </div> : null}
    {(cc2.questions || []).map((q) => {
      const pr = q.printer || {};
      return <div key={q.slot} className="warn" style={{ marginBottom: 10, alignItems: 'center' }}>
        <span className="swatch" style={{ background: pr.color || 'var(--raise)', width: 16, height: 16 }} />
        <span style={{ flex: 1, minWidth: 0 }}>
          {q.name}: {q.question === 'changed' ? 'сменили филамент' : 'вставили катушку'}
          {pr.type ? ` ${pr.brand || ''} ${pr.name || pr.type}`.replace(/\s+/g, ' ') : ''} — какая это?
          {q.question_mm ? ` Уже выдавлено ${Math.round(q.question_mm)} мм — спишется с выбранной.` : ''}
        </span>
        <select className="sel" style={{ width: 260 }} value={pick[q.slot] || ''}
          onChange={(e) => setPick((s) => ({ ...s, [q.slot]: e.target.value }))}>
          <option value="">— катушка со склада —</option>
          {spools.map((s) => <option key={s.id} value={s.cc2_id}>
            {pname(s.plastic_id)} {s.color} · {grams(s.live_g != null ? s.live_g : s.remaining_g)}
            {s.slot != null ? ' · ' + slotName(s.slot) : ''}</option>)}
        </select>
        <ABtn kind="sm line" disabled={!pick[q.slot]}
          onClick={() => act({ action: 'cc2_assign', slot: q.slot, cc2_id: pick[q.slot] }, 'Катушка в лотке')}>Она</ABtn>
        <ABtn kind="sm line" title="В лотке не катушка со склада: его расход не учитываем"
          onClick={() => act({ action: 'cc2_dismiss', slot: q.slot }, 'Лоток без учёта')}>Не со склада</ABtn>
      </div>;
    })}
  </>;
}

function NewPlasticDrawer({ onClose, onSaved, types, cats }) {
  const { toast } = useContext(AppCtx);
  const [f, setF] = useState({ name: '', material: types[0] || 'PLA', category: AUTO_CAT,
    price: '', density: '1.24', description: '', spool_weight: '' });
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  return <Drawer title="Новый пластик" sub="появится в каталоге и калькуляторе" onClose={onClose}
    footer={<>
      <button className="db line" onClick={onClose}>Отмена</button>
      <ABtn disabled={!f.name.trim() || !f.price} onClick={async () => {
        try {
          // без action:'save' обработчик отвечает «Неизвестное действие» — пластик не создаётся
          await call('adminPlastic', {
            action: 'save', name: f.name.trim(), material: f.material, category: f.category,
            price: f.price, density: f.density || 1.24, description: f.description, colors: [],
            spool_weight: f.spool_weight || 0,
          });
          toast('Пластик добавлен', { kind: 'ok' });
          onClose(); onSaved();
        } catch (e) { toast(e.message, { kind: 'warn2' }); }
      }}>Добавить</ABtn>
    </>}>
    <div className="fgap">
      <Field l="Название">
        <input className="inp" placeholder="Elegoo PLA Matte" value={f.name}
          onChange={(e) => set('name', e.target.value)} /></Field>
      <div className="frow">
        <Field l="Тип">
          <select className="sel" value={f.material} onChange={(e) => set('material', e.target.value)}>
            {types.map((t) => <option key={t} value={t}>{t}</option>)}
          </select></Field>
        <Field l="Категория">
          <select className="sel" value={f.category} onChange={(e) => set('category', e.target.value)}>
            <option value={AUTO_CAT}>— авто (по типу/названию)</option>
            {cats.map((c) => <option key={c} value={c}>{c}</option>)}
          </select></Field>
      </div>
      <div className="frow">
        <Field l={`Цена, ${curSym()}/кг`}>
          <input className="inp mono" type="number" step="0.01" value={f.price}
            onChange={(e) => set('price', e.target.value)} /></Field>
        <Field l="Плотность">
          <input className="inp mono" type="number" step="0.01" value={f.density}
            onChange={(e) => set('density', e.target.value)} /></Field>
        <Field l="Вес пустой катушки, г">
          <input className="inp mono" type="number" step="1" value={f.spool_weight}
            onChange={(e) => set('spool_weight', e.target.value)} /></Field>
      </div>
      <Field l="Описание">
        <input className="inp" value={f.description} onChange={(e) => set('description', e.target.value)} /></Field>
      <div className="hint">цвета, склад и катушки добавите в карточке после создания</div>
    </div>
  </Drawer>;
}
