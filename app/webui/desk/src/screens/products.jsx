// products.jsx — «Готовые изделия»: витрина товаров. Цена фиксированная, мимо расчёта.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Field, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { money, num, curSym } from '../fmt.jsx';
import { Screen, Search, Split, ABtn, DetHead, PickOne, useConfirm } from '../parts.jsx';

const { useState, useContext, useMemo, useRef } = React;

const BLANK = { id: 0, title: '', description: '', price: '', stock: 1, is_active: true };

export function ProductsScreen() {
  const { toast } = useContext(AppCtx);
  const [sel, setSel] = useState(null);
  const [q, setQ] = useState('');
  const p = useApi('adminProducts', [], []);
  const products = (p.data && p.data.products) || [];

  const shown = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return ql ? products.filter((x) => String(x.title || '').toLowerCase().includes(ql)) : products;
  }, [products, q]);

  const cur = sel === 0 ? BLANK : products.find((x) => x.id === sel) || null;

  const listPane = <>
    <div className="ms-tools">
      <Search value={q} onChange={setQ} placeholder="Товар…" />
      <button className="db" onClick={() => setSel(0)}>
        <DIcon n="plus" size={15} />Новый товар
      </button>
    </div>
    {/* .twrap обязателен: он держит рамку списка и его собственную прокрутку —
        без него таблица растягивает страницу и скроллится всё окно целиком */}
    <Screen state={p}>{() => shown.length ? <div className="twrap">
      <table className="tbl">
        <thead><tr>
          {/* сортировки на экране нет — гасим th{cursor:pointer} из макета */}
          <th style={{ width: 58, cursor: 'default' }}></th>
          <th style={{ cursor: 'default' }}>Название</th>
          <th style={{ width: 100, textAlign: 'right', cursor: 'default' }}>Цена</th>
          <th style={{ width: 130, cursor: 'default' }}>В наличии</th>
          <th style={{ width: 100, cursor: 'default' }}>Продано</th>
          <th style={{ width: 110, cursor: 'default' }}>Витрина</th>
        </tr></thead>
        <tbody>
          {shown.map((x) => <tr key={x.id} className={'row' + (x.id === sel ? ' sel' : '')}
            onClick={() => setSel(x.id)}>
            <td>{x.photo ? <img className="pv" src={x.photo} alt="" /> : null}</td>
            <td className="oname">{x.title}</td>
            <td className="mono" style={{ textAlign: 'right' }}>{money(x.price)}</td>
            <td className="mono" style={{ color: x.stock ? 'var(--text)' : 'var(--red)' }}>
              {x.stock ? num(x.stock) + ' шт' : 'нет в наличии'}
            </td>
            <td className="mono faint">{num(x.sold)}</td>
            <td>{x.is_active ? <span className="stb green">видна</span> : <span className="stb gray">скрыт</span>}</td>
          </tr>)}
        </tbody>
      </table>
    </div> : <Empty ic="gift" t="Товаров нет" ds="Добавьте готовое изделие в витрину"
      cta="Новый товар" onCta={() => setSel(0)} />}
    </Screen>
  </>;

  return <Split width={1240} list={listPane}
    detail={cur ? <ProductCard key={cur.id} p={cur} onSaved={() => { p.reload(); }}
      onDeleted={() => { setSel(null); p.reload(); }} toast={toast} />
      : <PickOne what="товар" />} />;
}

function ProductCard({ p, onSaved, onDeleted, toast }) {
  const [f, setF] = useState({ ...BLANK, ...p });
  const [photo, setPhoto] = useState(p.photo || '');
  const fileRef = useRef();
  const confirm = useConfirm();
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));
  const isNew = !p.id;

  // одна и та же ручка сохраняет и форму, и быстрый тумблер видимости из шапки
  const put = (patch) => call('adminProductSave', {
    id: f.id || 0, title: f.title, description: f.description,
    price: Number(f.price) || 0, stock: Number(f.stock) || 0, is_active: !!f.is_active,
    ...patch,
  });

  const save = async () => {
    const r = await put({});
    // фото грузится вторым шагом: до сохранения у нового товара ещё нет id
    if (photo && photo.startsWith('data:')) {
      await call('adminPhoto', 'product', (r.product && r.product.id) || f.id, photo, false);
    }
    toast(isNew ? 'Товар добавлен' : 'Товар сохранён', { kind: 'ok' });
    onSaved();
  };

  const toggleVis = async () => {
    const next = !f.is_active;
    await put({ is_active: next });
    set('is_active', next);
    toast(next ? 'Показан на витрине' : 'Скрыт с витрины', { kind: 'ok' });
    onSaved();
  };

  const del = () => confirm(
    'Удалить товар?', `«${f.title}» исчезнет из витрины.`,
    async () => { await call('adminProductDelete', f.id); toast('Товар удалён', { kind: 'ok' }); onDeleted(); },
  );

  const pickPhoto = (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const rd = new FileReader();
    rd.onload = () => setPhoto(rd.result);
    rd.readAsDataURL(file);
  };

  return <div className="od">
    <DetHead title={isNew ? 'Новый товар' : 'Редактирование: ' + f.title}
      sub="витрина «Готовые изделия» в боте"
      right={isNew ? null : <>
        <button className="db soft sm" onClick={toggleVis}>{f.is_active ? 'Скрыть' : 'Показать'}</button>
        <button className="ib danger" title="Удалить" onClick={del}><DIcon n="trash" /></button>
      </>} />
    <div className="od-body">
      <div className="od-main">
        <Sec t="Карточка товара">
          <div className="fgap">
            <Field l="Название">
              <input className="inp" value={f.title} placeholder="Как увидит клиент"
                onChange={(e) => set('title', e.target.value)} />
            </Field>
            <Field l="Описание">
              <textarea className="inp" rows={4} value={f.description || ''}
                onChange={(e) => set('description', e.target.value)} />
            </Field>
            <div className="frow">
              <Field l={`Цена, ${curSym()}`}>
                <input className="inp mono" type="number" step="0.01" value={f.price}
                  onChange={(e) => set('price', e.target.value)} />
              </Field>
              <Field l="В наличии, шт">
                <input className="inp mono" type="number" min="0" value={f.stock}
                  onChange={(e) => set('stock', e.target.value)} />
              </Field>
            </div>
            <label className="chk">
              <input type="checkbox" checked={!!f.is_active}
                onChange={(e) => set('is_active', e.target.checked)} />
              Показывать в витрине клиентам
            </label>
            <ABtn disabled={!f.title.trim()} onClick={save}>{isNew ? 'Создать' : 'Сохранить'}</ABtn>
          </div>
        </Sec>

        <Sec t="Фото товара">
          {photo
            ? <img src={photo} alt="" style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', borderRadius: 11, display: 'block' }} />
            : <div style={{
              aspectRatio: '1', border: '1px dashed var(--line-2)', borderRadius: 11,
              display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
              gap: 8, color: 'var(--faint)', fontSize: 13,
            }}><DIcon n="camera" size={26} />фото пока нет</div>}
          <input type="file" accept="image/*" ref={fileRef} style={{ display: 'none' }} onChange={pickPhoto} />
          <button className="db line full" style={{ marginTop: 12 }} onClick={() => fileRef.current.click()}>
            <DIcon n="upload" size={15} />{photo ? 'Заменить фото' : 'Загрузить фото'}
          </button>
          {/* у нового товара фото уходит на сервер только вторым шагом, после сохранения */}
          {isNew ? <div className="hint" style={{ marginTop: 8 }}>фото загрузится после создания товара</div> : null}
        </Sec>
      </div>
    </div>
  </div>;
}
