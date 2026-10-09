// pricing.jsx — «Цены и скидки»: ставки, тиражные пороги, персональные скидки, промокоды
// и калькулятор. Считает ВСЕГДА сервер: надбавка за объём, стол и округление прячутся
// внутрь машинного времени, поэтому на клиенте цифры не сойдутся с реальной сметой.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Field, Drawer } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { num, dateOnly } from '../fmt.jsx';
import { Screen, ABtn, Toggle, useConfirm } from '../parts.jsx';
import { OPERATIONAL_KEYS } from './settings.jsx';

const { useState, useContext, useEffect } = React;

// Отбрасываем из ценовой группы операционные ключи, уехавшие на экран «Настройки»: сервер
// отдаёт их одним списком pricing, а по смыслу это не деньги. values/labels оставляем как есть —
// фильтруем только порядок отрисовки (лишние подписи в объекте безвредны).
function pricingOnly(group) {
  if (!group) return group;
  return { ...group, order: (group.order || []).filter((k) => !OPERATIONAL_KEYS.has(k)) };
}

// Акцентный процент скидки. В макете он задан инлайном (класса под него нет), поэтому
// повторяем инлайн, а не заводим самодельный .price — иначе стиль потерялся бы совсем.
const PCT = { fontWeight: 700, color: 'var(--accent)' };

// Подсветка построчной кнопки «сохранить», когда значение изменено. Тоже инлайн из макета:
// класса .dirty в дизайн-системе нет.
const DIRTY = { color: 'var(--accent)', borderColor: 'var(--accent-line)', background: 'var(--accent-soft)' };

export function PricingScreen() {
  const { drawer, setDrawer, params } = useContext(AppCtx);
  // из карточки заказа: nav('pricing', {calc:1, grams, minutes, qty})
  useEffect(() => { if (params.calc) setDrawer('calc'); }, [params.calc, setDrawer]);
  const s = useApi('adminSettings', [], []);
  const d = useApi('adminDiscounts', [], []);
  const p = useApi('adminPromos', [], []);
  const opt = useApi('adminOptions', [], []);   // опции постобработки (B4)

  // Ручное обновление экрана: цены/скидки/промокоды меняются и из мобильной админки,
  // и из бота, поэтому мастеру нужен способ перечитать всё сразу, не перезагружая приложение.
  const reloadAll = () => { s.reload(); d.reload(); p.reload(); opt.reload(); };

  return <div className="col3">
    <div className="colw">
      <Screen state={s}>{(data) => <>
        {/* операционные тумблеры/пороги (догон, срочность, DFM, слайс/санация) переехали
            на экран «Настройки» — здесь оставляем только ставки и наценку, чтобы не дублировать */}
        <SetGroup title="Ставки и наценка" ic="coin" group={pricingOnly(data.pricing)} onSaved={s.reload}
          right={<button className="ib" title="Обновить" onClick={reloadAll}><DIcon n="refresh" size={16} /></button>} />
        {data.texts ? <SetGroup title="Реквизиты в смете" ic="doc" group={data.texts} text onSaved={s.reload} /> : null}
      </>}</Screen>
    </div>

    <div className="colw">
      <Screen state={d}>{(data) => <>
        <TiersSec tiers={data.tiers || []} onSaved={d.reload} />
        <PersonalSec users={data.users || []} customers={data.customers || []} onSaved={d.reload} />
      </>}</Screen>
      {/* горизонтальная карточка-«кнопка» из макета: иконка, подпись, действие справа */}
      <div className="card" style={{ flex: '0 0 auto', flexDirection: 'row', alignItems: 'center', gap: 14 }}>
        <DIcon n="calc" size={22} style={{ color: 'var(--accent)', flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <b>Калькулятор цены</b>
          <div className="hint" style={{ margin: 0 }}>смета без заказа — граммы, время, скидка</div>
        </div>
        <button className="db soft" onClick={() => setDrawer('calc')}>Открыть</button>
      </div>
    </div>

    <div className="colw">
      <Screen state={p}>{(data) => <PromosSec promos={data.promos || []} onSaved={p.reload} />}</Screen>
      <Screen state={opt}>{(data) => <OptionsSec options={data.options || []} onSaved={opt.reload} />}</Screen>
    </div>

    {drawer === 'calc' && <CalcDrawer onClose={() => setDrawer(null)} pre={params} />}
  </div>;
}

/**
 * Группа настроек. Сохранение построчное, как в макете: кнопка-галочка в строке и есть
 * индикатор «изменено» — пакетная кнопка в шапке этого не показывала.
 */
function SetGroup({ title, ic, group, onSaved, text, right }) {
  const { toast } = useContext(AppCtx);
  const [edit, setEdit] = useState({});
  const vals = group.values || {};

  const drop = (k) => setEdit((s) => { const n = { ...s }; delete n[k]; return n; });

  // kind обязателен: без него сервер (webapp.py::_api_admin_setting) отвечает
  // «Неизвестный параметр» и настройка молча не сохраняется.
  const save = async (k, label, value) => {
    await call('adminSetting', { key: k, kind: text ? 'text' : 'num', value });
    drop(k);
    toast('Сохранено: ' + label, { kind: 'ok', ms: 2500 });
    onSaved();
  };

  return <Sec card t={title} ic={ic} right={right} style={{ flex: '0 0 auto' }}>
    {(group.order || []).map((k) => {
      const cur = k in edit ? edit[k] : vals[k];
      const label = (group.labels || {})[k] || k;
      // Тумблеры 0/1 — переключателем .sw, а не числовым полем: смысл у них булев.
      const isToggle = !text && (vals[k] === 0 || vals[k] === 1) && /^(slicing_enabled|sanitize_3mf|time_prediction)$/.test(k);
      return <div key={k} className="setrow">
        <span className="sl">{label}</span>
        {isToggle
          ? <Toggle on={Number(cur) === 1} label={label} onChange={(on) => {
              // тумблер сохраняем сразу — отдельной кнопки подтверждения он не требует
              setEdit((s) => ({ ...s, [k]: on ? 1 : 0 }));
              save(k, label, on ? 1 : 0).catch((e) => { drop(k); toast(e.message, { kind: 'warn2' }); });
            }} />
          : <>
              {/* реквизиты — длинный текст, ему нужна широкая строка (в макете 300 против 150) */}
              <input className="inp" type={text ? 'text' : 'number'} step="0.01"
                style={{ width: text ? 300 : 150 }} value={cur ?? ''}
                onChange={(e) => setEdit({ ...edit, [k]: text ? e.target.value : Number(e.target.value) })} />
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

function TiersSec({ tiers, onSaved }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [qty, setQty] = useState('');
  const [pct, setPct] = useState('');
  return <Sec card t="Скидки за объём" ic="tag" style={{ flex: '0 0 auto' }}>
    {tiers.length
      ? tiers.map((t) => <div key={t.id} className="setrow">
          <span className="sl">от <b className="mono">{num(t.min_qty)}</b> шт</span>
          <span className="mono" style={PCT}>−{t.pct}%</span>
          <button className="ib danger" title="Удалить порог" onClick={() => confirm('Удалить порог?',
            `Скидка −${t.pct}% от ${t.min_qty} шт будет удалена.`,
            async () => {
              try {
                await call('adminDiscount', { action: 'del_tier', id: t.id });
                toast('Удалено', { kind: 'ok' }); onSaved();
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            })}>
            <DIcon n="trash" size={15} />
          </button>
        </div>)
      : <div className="hint">Порогов пока нет — скидка за тираж не начисляется.</div>}
    {/* кнопка вне .frow: там flex:1 растянул бы её наравне с полями */}
    <div style={{ display: 'flex', gap: 9, marginTop: 10 }}>
      <input className="inp mono" type="number" min="2" value={qty} placeholder="от шт"
        onChange={(e) => setQty(e.target.value)} />
      <input className="inp mono" type="number" min="1" max="90" value={pct} placeholder="−%"
        onChange={(e) => setPct(e.target.value)} />
      <ABtn kind="soft sm" icon="plus" title="Добавить порог" style={{ flexShrink: 0 }}
        disabled={!qty || !pct} onClick={async () => {
          try {
            await call('adminDiscount', { action: 'add_tier', min_qty: Number(qty), pct: Number(pct) });
            setQty(''); setPct(''); toast('Порог добавлен', { kind: 'ok' }); onSaved();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }} />
    </div>
  </Sec>;
}

// Опции постобработки (B4): каталог платных ручных услуг мастера (шлифовка, покраска, вставки),
// которые клиент добавляет к заказу. Цена — розничная, наценка/скидка на неё не начисляются.
// Зеркалит TiersSec: список с удалением + форма добавления снизу. Тумблер «за деталь» (accee7a9):
// цена трактуется как ставка за отдельный элемент модели — итог = ставка × детали × тираж
// (кейс «склейка 20 коп/деталь» вместо одной плоской цены на любую склейку).
function OptionsSec({ options, onSaved }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [name, setName] = useState('');
  const [price, setPrice] = useState('');
  const [perPart, setPerPart] = useState(false);
  return <Sec card t="Опции постобработки" ic="tag" style={{ flex: '0 0 auto' }}>
    {options.length
      ? options.map((o) => <div key={o.id} className="setrow">
          <span className="sl"><b>{o.name}</b>{o.per_part ? <span className="hint" style={{ marginLeft: 6 }}>за деталь</span> : null}</span>
          <span className="mono" style={PCT}>+{num(o.price)}{o.per_part ? '/дет.' : ''}</span>
          <button className="ib" title={o.per_part ? 'Сделать ценой за заказ' : 'Сделать ставкой за деталь'}
            onClick={async () => {
              try {
                await call('adminOption', { action: 'update', id: o.id, per_part: o.per_part ? 0 : 1 });
                toast(o.per_part ? 'Теперь цена за заказ' : 'Теперь ставка за деталь', { kind: 'ok' }); onSaved();
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            }}>
            <DIcon n="grid" size={15} />
          </button>
          <button className="ib danger" title="Удалить услугу" onClick={() => confirm('Удалить услугу?',
            `«${o.name}» (+${o.price}${o.per_part ? '/дет.' : ''}) больше не будет предлагаться клиентам. Прошлые заказы не изменятся.`,
            async () => {
              try {
                await call('adminOption', { action: 'del', id: o.id });
                toast('Удалено', { kind: 'ok' }); onSaved();
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            })}>
            <DIcon n="trash" size={15} />
          </button>
        </div>)
      : <div className="hint">Услуг пока нет — добавьте, чтобы клиенты могли выбрать их в заказе.</div>}
    <div style={{ display: 'flex', gap: 9, marginTop: 10, alignItems: 'center', flexWrap: 'wrap' }}>
      <input className="inp" value={name} placeholder="Название (Шлифовка)" style={{ flex: 1, minWidth: 140 }}
        onChange={(e) => setName(e.target.value)} />
      <input className="inp mono" type="number" min="0" step="0.01" value={price}
        placeholder={perPart ? 'цена/дет.' : 'цена'}
        style={{ width: 90, flex: '0 0 auto' }} onChange={(e) => setPrice(e.target.value)} />
      <label className="hint" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer', flex: '0 0 auto' }}>
        <input type="checkbox" checked={perPart} onChange={(e) => setPerPart(e.target.checked)} />
        за деталь
      </label>
      <ABtn kind="soft sm" icon="plus" title="Добавить услугу" style={{ flexShrink: 0 }}
        disabled={!name.trim() || price === ''} onClick={async () => {
          try {
            await call('adminOption', { action: 'add', name: name.trim(), price: Number(price), per_part: perPart ? 1 : 0 });
            setName(''); setPrice(''); setPerPart(false); toast('Услуга добавлена', { kind: 'ok' }); onSaved();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }} />
    </div>
    <div className="hint" style={{ marginTop: 6 }}>
      «За деталь» — итог услуги = ставка × число отдельных деталей модели × тираж (например, склейка 0,20/дет.).
    </div>
  </Sec>;
}

function PersonalSec({ users, customers, onSaved }) {
  const { toast, nav } = useContext(AppCtx);
  const confirm = useConfirm();
  const [uid, setUid] = useState('');
  const [pct, setPct] = useState('');
  // Имя нужно, когда скидку выдают по «голому» Telegram ID: такого человека ещё нет
  // среди заказчиков, и без имени в списке останется безликое «id 123456».
  const [name, setName] = useState('');

  const add = async () => {
    await call('adminDiscount', {
      action: 'add_user', uid: Number(uid), pct: Number(pct), name: name.trim(),
    });
    setUid(''); setPct(''); setName('');
    toast('Скидка выдана', { kind: 'ok' }); onSaved();
  };

  // Выбор из списка заказчиков лишь подставляет id и имя в те же поля — дальше
  // форма одна, чтобы не было двух путей выдачи скидки с расходящимся поведением.
  const pick = (v) => {
    setUid(v);
    const c = customers.find((x) => String(x.uid) === String(v));
    setName(c ? (c.name || '') : '');
  };

  return <Sec card t="Персональные скидки" ic="star" style={{ flex: '0 0 auto' }}>
    {users.length
      ? users.map((u) => <div key={u.uid} className="setrow">
          <span className="sl" style={{ cursor: 'pointer' }} onClick={() => nav('clients', { sel: u.uid })}>
            <b>{u.name || 'id ' + u.uid}</b> <span className="mono" style={{ fontSize: 11, color: 'var(--faint)' }}>id {u.uid}</span>
          </span>
          <span className="mono" style={PCT}>−{u.pct}%</span>
          <button className="ib danger" title="Снять скидку" onClick={() => confirm('Снять скидку?',
            `${u.name || 'id ' + u.uid} перестанет получать −${u.pct}% на заказы.`,
            // диалог подтверждения не ловит исключения сам, поэтому try/catch тут обязателен —
            // иначе отказ сервера остался бы невидимым для мастера
            async () => {
              try {
                await call('adminDiscount', { action: 'del_user', uid: u.uid });
                toast('Скидка снята', { kind: 'ok' }); onSaved();
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            }, 'Снять')}><DIcon n="trash" size={15} /></button>
        </div>)
      : <div className="hint">Персональных скидок нет.</div>}
    <div style={{ display: 'flex', gap: 9, marginTop: 10 }}>
      <select className="sel" style={{ flex: 1 }} value={uid} onChange={(e) => pick(e.target.value)}>
        <option value="">клиент из заказов…</option>
        {customers.map((c) => <option key={c.uid} value={c.uid}>{c.name || 'id ' + c.uid}</option>)}
      </select>
    </div>
    {/* Второй ряд — выдача по произвольному Telegram ID: клиенту, который ещё ничего
        не заказывал, скидку иначе не назначить (договорённость «приходи, тебе −10%»). */}
    <div style={{ display: 'flex', gap: 9, marginTop: 9 }}>
      <input className="inp mono" inputMode="numeric" value={uid} placeholder="Telegram ID"
        style={{ width: 130, flex: '0 0 auto' }} onChange={(e) => setUid(e.target.value.replace(/\D/g, ''))} />
      <input className="inp" value={name} placeholder="Имя (необязательно)" style={{ flex: 1 }}
        onChange={(e) => setName(e.target.value)} />
      <input className="inp mono" type="number" min="1" max="90" value={pct} placeholder="−%"
        style={{ width: 80, flex: '0 0 auto' }} onChange={(e) => setPct(e.target.value)} />
      <ABtn kind="soft sm" icon="plus" title="Выдать скидку" style={{ flexShrink: 0 }}
        disabled={!uid || !pct} onClick={() => add().catch((e) => toast(e.message, { kind: 'warn2' }))} />
    </div>
  </Sec>;
}

function PromosSec({ promos, onSaved }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [f, setF] = useState({ code: '', pct: '', days: '', max: '' });
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  return <Sec card t="Промокоды" ic="gift" style={{ flex: '0 0 auto' }}>
    {/* форма сверху: код создают чаще, чем правят список */}
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 76px 76px 76px auto', gap: 9, marginBottom: 14 }}>
      <input className="inp mono" value={f.code} placeholder="КОД" style={{ textTransform: 'uppercase' }}
        onChange={(e) => set('code', e.target.value.toUpperCase())} />
      <input className="inp mono" type="number" value={f.pct} placeholder="−%"
        onChange={(e) => set('pct', e.target.value)} />
      <input className="inp mono" type="number" value={f.days} placeholder="дней"
        onChange={(e) => set('days', e.target.value)} />
      <input className="inp mono" type="number" value={f.max} placeholder="лимит"
        onChange={(e) => set('max', e.target.value)} />
      <ABtn kind="soft sm" icon="plus" title="Создать промокод"
        disabled={!f.code.trim() || !f.pct} onClick={async () => {
          try {
            await call('adminPromo', {
              action: 'add', code: f.code.trim(), discount_pct: Number(f.pct),
              days: Number(f.days) || 0, max_uses: Number(f.max) || 0,
            });
            setF({ code: '', pct: '', days: '', max: '' });
            toast('Промокод создан', { kind: 'ok' }); onSaved();
          } catch (e) { toast(e.message, { kind: 'warn2' }); }
        }} />
    </div>
    {promos.length
      ? promos.map((p) => <div key={p.code} className="crow2">
          <span className="mono" style={{ fontSize: 22, fontWeight: 700, color: 'var(--accent)', width: 74, flexShrink: 0 }}>
            −{p.discount_pct}%
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontWeight: 700, letterSpacing: 1 }}>{p.code}</div>
            <div className="hint" style={{ margin: 0 }}>
              {p.expires_at ? 'до ' + dateOnly(p.expires_at) : 'бессрочно'}
              {' · использован '}{num(p.uses)} / {p.max_uses ? num(p.max_uses) : '∞'}
            </div>
          </div>
          <button className="ib danger" title="Удалить" onClick={() => confirm('Удалить промокод?',
            `Код ${p.code} перестанет работать.`,
            async () => {
              try {
                await call('adminPromo', { action: 'delete', code: p.code });
                toast('Удалён', { kind: 'ok' }); onSaved();
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            })}>
            <DIcon n="trash" size={15} />
          </button>
        </div>)
      : <div className="hint">Промокодов нет.</div>}
  </Sec>;
}

// ---------------- калькулятор ----------------

function CalcDrawer({ onClose, pre = {} }) {
  const { toast } = useContext(AppCtx);
  const plastics = useApi('adminPlastics', [], []);
  const [multi, setMulti] = useState(false);
  const [qty, setQty] = useState(Number(pre.qty) || 1);
  // Время вводим часами и минутами (печати идут часами — вводить 780 минут неудобно).
  // В расчёт уходит сумма в минутах.
  const [hrs, setHrs] = useState(pre.minutes ? Math.floor(Number(pre.minutes) / 60) : '');
  const [mins, setMins] = useState(pre.minutes ? Math.round(Number(pre.minutes) % 60) : '');
  const totalMin = (Number(hrs) || 0) * 60 + (Number(mins) || 0);
  const [discount, setDiscount] = useState('');
  const [ready, setReady] = useState(false);
  const [one, setOne] = useState({ grams: pre.grams ? Math.round(Number(pre.grams) * 10) / 10 : '', price_per_kg: '' });
  const [colors, setColors] = useState([{ grams: '', price_per_kg: '' }, { grams: '', price_per_kg: '' }]);
  const [res, setRes] = useState(null);

  const list = (plastics.data && plastics.data.plastics) || [];

  const calc = async () => {
    const body = {
      minutes: totalMin, qty: Number(qty) || 1,
      discount_pct: Number(discount) || 0, ready_product: ready,
    };
    if (multi) {
      body.colors = colors
        .filter((c) => c.grams && c.price_per_kg)
        .map((c) => ({ grams: Number(c.grams), price_per_kg: Number(c.price_per_kg) }));
    } else {
      body.grams = Number(one.grams) || 0;
      body.price_per_kg = Number(one.price_per_kg) || 0;
    }
    try {
      setRes(await call('adminPriceCalc', body));
    } catch (e) {
      toast(e.message, { kind: 'warn2' });
      setRes(null);
    }
  };

  const pickPlastic = (i, id) => {
    const p = list.find((x) => String(x.id) === String(id));
    if (!p) return;
    if (multi) {
      const c = colors.slice();
      c[i] = { ...c[i], price_per_kg: p.price };
      setColors(c);
    } else {
      setOne((s) => ({ ...s, price_per_kg: p.price }));
    }
  };

  const plasticOpts = <>
    <option value="">—</option>
    {list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
  </>;

  return <Drawer title="Калькулятор цены" sub="та же формула, что и в заказах" onClose={onClose}
    footer={<ABtn kind="full" onClick={calc}>Рассчитать</ABtn>}>
    {/* всё тело — один .fgap, группы полей — .frow: так свёрстан drawer макета */}
    <div className="fgap">
      <div className="stchips">
        <button className={'chip' + (multi ? '' : ' on')} onClick={() => { setMulti(false); setRes(null); }}>Один цвет</button>
        <button className={'chip' + (multi ? ' on' : '')} onClick={() => { setMulti(true); setRes(null); }}>Многоцвет</button>
      </div>

      {!multi
        ? <>
            <div className="frow">
              <Field l="Граммы">
                <input className="inp mono" type="number" value={one.grams}
                  onChange={(e) => setOne((s) => ({ ...s, grams: e.target.value }))} />
              </Field>
              <Field l="Цена за кг">
                <input className="inp mono" type="number" value={one.price_per_kg}
                  onChange={(e) => setOne((s) => ({ ...s, price_per_kg: e.target.value }))} />
              </Field>
            </div>
            <Field l="Из пластика">
              <select className="sel" onChange={(e) => pickPlastic(0, e.target.value)}>{plasticOpts}</select>
            </Field>
          </>
        : <>
            <div className="hint" style={{ margin: 0 }}>Граммы по каждому цвету со своей ценой. Надбавка за продувку
              не добавляется — числа берутся из реального многоцветного слайса.</div>
            {colors.map((c, i) => <div key={i} className="frow">
              <Field l={`Цвет ${i + 1}: граммы`}>
                <input className="inp mono" type="number" value={c.grams} onChange={(e) => {
                  const n = colors.slice(); n[i] = { ...n[i], grams: e.target.value }; setColors(n);
                }} />
              </Field>
              <Field l="Цена за кг">
                <input className="inp mono" type="number" value={c.price_per_kg} onChange={(e) => {
                  const n = colors.slice(); n[i] = { ...n[i], price_per_kg: e.target.value }; setColors(n);
                }} />
              </Field>
              <Field l="Из пластика">
                <select className="sel" onChange={(e) => pickPlastic(i, e.target.value)}>{plasticOpts}</select>
              </Field>
            </div>)}
            <button className="db soft sm" style={{ alignSelf: 'flex-start' }}
              onClick={() => setColors(colors.concat([{ grams: '', price_per_kg: '' }]))}>
              <DIcon n="plus" size={14} />Ещё цвет
            </button>
          </>}

      <div className="frow">
        <Field l="Время: часы">
          <input className="inp mono" type="number" min="0" value={hrs} placeholder="0"
            onChange={(e) => setHrs(e.target.value)} />
        </Field>
        <Field l="и минуты">
          <input className="inp mono" type="number" min="0" max="59" value={mins} placeholder="0"
            onChange={(e) => setMins(e.target.value)} />
        </Field>
        <Field l="Скидка, %">
          <input className="inp mono" type="number" min="0" max="90" value={discount}
            onChange={(e) => setDiscount(e.target.value)} />
        </Field>
        <Field l="Количество, шт">
          <div className="stepper">
            <button className="ib" onClick={() => setQty(Math.max(1, Number(qty) - 1))}>−</button>
            <input className="inp mono" type="number" min="1" value={qty} style={{ width: 64 }}
              onChange={(e) => setQty(Math.max(1, Number(e.target.value) || 1))} />
            <button className="ib" onClick={() => setQty(Number(qty) + 1)}>+</button>
          </div>
        </Field>
      </div>

      <label className="chk">
        <input type="checkbox" checked={ready} onChange={(e) => setReady(e.target.checked)} />
        Готовое изделие — печать на склад, с риском не продать
      </label>
      <div className="hint" style={{ margin: 0 }}>Процент наценки задаётся слева, в «Ставках и наценке».</div>

      {res && res.bd ? <div className="sec" style={{ background: 'var(--bg)' }}>
        <div className="lab">Смета</div>
        {(res.bd.rows || []).map((r, i) => <div key={i} className="bd-row">
          <span>{r.label}{r.sub ? <i> · {r.sub}</i> : null}</span>
          <b>{r.value}</b>
        </div>)}
        <div className="bd-row tot">
          <span>Итого{res.bd.qty > 1 ? ` за ${res.bd.qty} шт` : ''}</span>
          <b>{res.bd.total}</b>
        </div>
        <div className="hint" style={{ margin: '8px 0 0' }}>{res.bd.grams_total} г · {res.bd.time_total}</div>
      </div> : null}
    </div>
  </Drawer>;
}
