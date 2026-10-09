// delivery.jsx — «Доставка»: типы и тарифы (A2).
//
// Прайс доставки: строка = тип (цена, порог бесплатной доставки, срок). Клиент выбирает
// тип в корзине или задаёт его в профиле, СТОИМОСТЬ СЧИТАЕТ СЕРВЕР и кладёт её в итог
// заказа и в онлайн-счёт. Типов нет — у клиента вообще нет выбора доставки (самовывоз).

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Field, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { money } from '../fmt.jsx';
import { Screen, Split, ABtn, DetHead, PickOne, useConfirm } from '../parts.jsx';

const { useState, useContext } = React;

const BLANK = { id: 0, name: '', price: '', free_from: '', eta: '', comment: '', is_active: true };

export function DeliveryScreen() {
  const { toast } = useContext(AppCtx);
  const [sel, setSel] = useState(null);
  const d = useApi('adminDelivery', [], []);
  const types = (d.data && d.data.types) || [];
  const cur = sel === 0 ? BLANK : types.find((t) => t.id === sel) || null;

  const listPane = <>
    <div className="ms-tools">
      <div className="hint" style={{ flex: 1 }}>
        Клиент выбирает тип при оформлении или задаёт его в профиле — стоимость входит в итог заказа и в счёт на оплату.
      </div>
      <button className="db" onClick={() => setSel(0)}><DIcon n="plus" size={15} />Новый тип</button>
    </div>
    <Screen state={d}>{() => (types.length ? <div className="twrap">
      <table className="tbl">
        <thead><tr>
          <th style={{ cursor: 'default' }}>Тип доставки</th>
          <th style={{ width: 120, textAlign: 'right', cursor: 'default' }}>Цена</th>
          <th style={{ width: 150, textAlign: 'right', cursor: 'default' }}>Бесплатно от</th>
          <th style={{ width: 130, cursor: 'default' }}>Срок</th>
          <th style={{ width: 110, cursor: 'default' }}>В корзине</th>
        </tr></thead>
        <tbody>
          {types.map((z) => <tr key={z.id} className={'row' + (z.id === sel ? ' sel' : '')}
            onClick={() => setSel(z.id)}>
            <td className="oname">{z.name}</td>
            <td className="mono" style={{ textAlign: 'right' }}>{money(z.price)}</td>
            <td className="mono faint" style={{ textAlign: 'right' }}>
              {z.free_from > 0 ? money(z.free_from) : '—'}
            </td>
            <td className="faint">{z.eta || '—'}</td>
            <td>{z.is_active
              ? <span className="stb green">видно</span>
              : <span className="stb gray">скрыт</span>}</td>
          </tr>)}
        </tbody>
      </table>
    </div> : <Empty ic="truck" t="Типов доставки нет"
      ds="Пока их нет, клиент видит только самовывоз — ни выбора доставки, ни адреса"
      cta="Новый тип" onCta={() => setSel(0)} />)}
    </Screen>
  </>;

  return <Split width={1180} list={listPane}
    detail={cur ? <TypeCard key={cur.id} z={cur} toast={toast}
      onSaved={() => d.reload()} onDeleted={() => { setSel(null); d.reload(); }} />
      : <PickOne what="тип доставки" />} />;
}

function TypeCard({ z, onSaved, onDeleted, toast }) {
  const [f, setF] = useState({ ...BLANK, ...z });
  const confirm = useConfirm();
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  const isNew = !f.id;

  const save = async () => {
    await call('adminDeliveryType', {
      action: isNew ? 'add' : 'update', id: f.id || 0, name: f.name,
      price: Number(f.price) || 0, free_from: Number(f.free_from) || 0,
      eta: f.eta || '', comment: f.comment || '', is_active: !!f.is_active,
    });
    toast(isNew ? 'Тип добавлен' : 'Тип сохранён', { kind: 'ok' });
    onSaved();
  };

  const del = () => confirm(
    'Удалить тип доставки?', `«${f.name}» исчезнет из выбора при оформлении. Уже оформленные заказы не изменятся — в них лежит снимок цены.`,
    async () => { await call('adminDeliveryType', { action: 'del', id: f.id }); toast('Тип удалён', { kind: 'ok' }); onDeleted(); },
  );

  return <div className="od">
    <DetHead title={isNew ? 'Новый тип доставки' : 'Тип: ' + f.name}
      sub="тариф для оформления заказа"
      right={isNew ? null : <button className="ib danger" title="Удалить" onClick={del}><DIcon n="trash" /></button>} />
    <div className="od-body">
      <div className="od-main">
        <Sec t="Тариф">
          <div className="fgap">
            <Field l="Название типа">
              <input className="inp" value={f.name} placeholder="Например, «Курьером по городу»"
                onChange={(e) => set('name', e.target.value)} />
            </Field>
            <div className="frow">
              <Field l="Стоимость доставки">
                <input className="inp mono" type="number" step="0.01" min="0" value={f.price}
                  onChange={(e) => set('price', e.target.value)} />
              </Field>
              <Field l="Бесплатно от суммы">
                <input className="inp mono" type="number" step="0.01" min="0" value={f.free_from}
                  onChange={(e) => set('free_from', e.target.value)} />
              </Field>
            </div>
            <div className="hint">
              «Бесплатно от» сравнивается с суммой ПОЗИЦИЙ заказа (без доставки). 0 — порога нет.
            </div>
            <Field l="Срок">
              <input className="inp" value={f.eta || ''} placeholder="1–2 дня"
                onChange={(e) => set('eta', e.target.value)} />
            </Field>
            <Field l="Примечание (видно клиенту)">
              <input className="inp" value={f.comment || ''} placeholder="Например, «доставка до двери»"
                onChange={(e) => set('comment', e.target.value)} />
            </Field>
            <label className="chk">
              <input type="checkbox" checked={!!f.is_active}
                onChange={(e) => set('is_active', e.target.checked)} />
              Предлагать этот тип клиенту
            </label>
            <ABtn disabled={!String(f.name).trim()} onClick={save}>{isNew ? 'Создать' : 'Сохранить'}</ABtn>
          </div>
        </Sec>
      </div>
    </div>
  </div>;
}
