// collection.jsx — «Коллекция»: разделы каталога и модели в них.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { num } from '../fmt.jsx';
import { Screen, Search, DetHead, ABtn, useConfirm } from '../parts.jsx';

const { useState, useContext, useMemo, useRef, useEffect, useCallback } = React;

const NONE = '__none__';
// Бэкенд режет limit до 120 за ЗАПРОС, но offset не ограничен — поэтому листаем страницами
// и накапливаем, а не пытаемся вытянуть раздел одним запросом (в Коллекции моделей больше).
const PAGE = 40;
const MAXPHOTO = 12 * 1024 * 1024;   // столько же принимает /api/admin/photo

// Подсветка выбранной строки — из макета: полоса-акцент слева, а не отдельный класс.
const SELROW = { background: 'var(--surface-2)', boxShadow: 'inset 3px 0 0 var(--accent)' };
const IBTN = { width: 32, height: 32 };

export function CollectionScreen() {
  const { toast, lightbox } = useContext(AppCtx);
  const [cat, setCat] = useState(NONE);
  const [q, setQ] = useState('');
  const [rename, setRename] = useState(null);    // раздел: {old, value}
  const [mren, setMren] = useState(null);        // модель: {id, value}
  const confirm = useConfirm();

  const root = useApi('adminCollection', [{}], []);
  const cats = (root.data && root.data.categories) || [];

  // Модели раздела грузим САМИ, страницами: useApi умеет только «один запрос — один ответ»,
  // а нам нужно накапливать. Форма state повторяет useApi, чтобы работал <Screen>.
  const [items, setItems] = useState({ data: null, err: null, loading: true });
  const [busy, setBusy] = useState(false);
  const offRef = useRef(0);     // с какого места просить следующую страницу
  const seqRef = useRef(0);     // ответы прошлого раздела/перезагрузки игнорируем

  /** Догрузить одну страницу. Возвращает true, если у сервера ещё что-то осталось. */
  const loadPage = useCallback(async (replace) => {
    const my = seqRef.current;
    const offset = replace ? 0 : offRef.current;
    setBusy(true);
    try {
      const j = await call('adminCollection', { category: cat, offset, limit: PAGE });
      if (my !== seqRef.current) return false;
      const list = j.items || [];
      offRef.current = offset + list.length;
      setItems((s) => ({
        data: {
          items: (replace || !s.data) ? list : s.data.items.concat(list),
          total: j.total || 0,
        },
        err: null,
        loading: false,
      }));
      // Пустая страница при has_more означала бы вечный цикл в «Загрузить все» — обрываем.
      return list.length > 0 && !!j.has_more;
    } catch (e) {
      if (my === seqRef.current) {
        setItems((s) => ({ ...s, err: e.message, loading: false }));
        toast(e.message, { kind: 'warn2' });
      }
      return false;
    } finally {
      if (my === seqRef.current) setBusy(false);
    }
  }, [cat, toast]);

  const reloadItems = useCallback(() => {
    seqRef.current += 1;
    offRef.current = 0;
    setItems({ data: null, err: null, loading: true });
    loadPage(true);
  }, [loadPage]);

  useEffect(() => { reloadItems(); }, [cat]);   // eslint-disable-line

  const models = (items.data && items.data.items) || [];
  const total = (items.data && items.data.total) || 0;
  const more = models.length < total;

  // Точечные правки списка вместо полной перезагрузки: иначе после переименования модели
  // где-нибудь на 300-й позиции список схлопнулся бы обратно до первой страницы.
  const patch = (id, f) => setItems((s) => (s.data ? { ...s,
    data: { ...s.data, items: s.data.items.map((m) => (m.id === id ? { ...m, ...f } : m)) } } : s));
  const drop = (id) => setItems((s) => (s.data ? { ...s,
    data: { ...s.data,
      items: s.data.items.filter((m) => m.id !== id),
      total: Math.max(0, s.data.total - 1) } } : s));
  // Ушедшая из списка модель сдвигает окно на сервере — иначе следующая страница пропустит строку.
  const shiftOff = () => { offRef.current = Math.max(0, offRef.current - 1); };

  const shown = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return ql ? models.filter((m) => String(m.label || '').toLowerCase().includes(ql)) : models;
  }, [models, q]);

  const reload = () => { root.reload(); reloadItems(); };
  const pick = (name) => { setCat(name); setQ(''); };

  // Поиска на сервере у /api/admin/collection нет (только category/offset/limit), поэтому
  // фильтр клиентский. Чтобы он видел ВЕСЬ раздел, даём дотянуть остаток одной кнопкой.
  const loadAll = async () => {
    let rest = true;
    while (rest) rest = await loadPage(false);   // eslint-disable-line no-await-in-loop
  };

  // Фото позиции: одна скрытая форма на экран, цель запоминаем в ref — карточек может быть
  // сотни, и по input на каждую держать незачем.
  const fileRef = useRef(null);
  const photoFor = useRef(0);
  const pickPhoto = (id) => { photoFor.current = id; if (fileRef.current) fileRef.current.click(); };

  const onFile = (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';          // без сброса повторный выбор того же файла не даст onChange
    if (!f) return;
    if (f.size > MAXPHOTO) { toast('Фото больше 12 МБ — выберите поменьше', { kind: 'warn2' }); return; }
    const id = photoFor.current;
    const rd = new FileReader();
    rd.onload = async () => {
      try {
        const r = await call('adminPhoto', 'collection', id, rd.result, false);
        // Сервер возвращает подписанный URL уже сжатой картинки — подставляем его,
        // чтобы не перезагружать все накопленные страницы ради одной обложки.
        patch(id, { photo: r.url || null, has_photo: true });
        toast('Фото обновлено', { kind: 'ok' });
      } catch (err) {
        toast(err.message, { kind: 'warn2' });
      }
    };
    rd.readAsDataURL(f);
  };

  // Порядок разделов бэкенд принимает только целиком: частичная перестановка
  // прыгнула бы мимо разделов с дефолтным sort=100.
  const move = async (i, d) => {
    const names = cats.map((c) => c.name);
    const j = i + d;
    if (j < 0 || j >= names.length) return;
    [names[i], names[j]] = [names[j], names[i]];
    try {
      await call('adminCollectionEdit', { action: 'cats_order', names });
      root.reload();
    } catch (e) {
      toast(e.message, { kind: 'warn2' });
    }
  };

  const curCat = cats.find((c) => c.name === cat);
  const catTitle = cat === NONE ? 'Вне каталога' : cat;
  const catCount = cat === NONE ? ((root.data && root.data.uncat_count) || 0) : (curCat ? curCat.count : total);

  return <div className="split">
    <div className="ms" style={{ '--ms-w': '860px' }}>
      <div className="card" style={{ flex: 1, minHeight: 0 }}>
        <div className="lab">
          <DIcon n="grid" size={13} />Разделы каталога
          <span className="r"><span style={{ color: 'var(--faint)' }}>
            всего {num((root.data && root.data.total) || 0)} моделей
          </span></span>
        </div>
        <Screen state={root}>{() => <div style={{ flex: 1, overflowY: 'auto' }}>
          {cats.map((c, i) => <div key={c.name} className="qrow"
            style={cat === c.name ? SELROW : {}} onClick={() => pick(c.name)}>
            <div className="pos">{i + 1}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              {rename && rename.old === c.name
                ? <input className="inp" autoFocus value={rename.value}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setRename({ ...rename, value: e.target.value })}
                    onKeyDown={async (e) => {
                      if (e.key === 'Escape') setRename(null);
                      if (e.key === 'Enter' && rename.value.trim()) {
                        try {
                          await call('adminCollectionEdit', {
                            action: 'rename_category', old: c.name, new: rename.value.trim(),
                          });
                          if (cat === c.name) setCat(rename.value.trim());
                          setRename(null); toast('Раздел переименован', { kind: 'ok' }); reload();
                        } catch (err) { toast(err.message, { kind: 'warn2' }); }
                      }
                    }} />
                : <>
                    <div className="nm">{c.name}</div>
                    <div className="ds">{num(c.count)} моделей</div>
                  </>}
            </div>
            <div className="arr" onClick={(e) => e.stopPropagation()}>
              <button disabled={i === 0} title="Выше" onClick={() => move(i, -1)}>▲</button>
              <button disabled={i === cats.length - 1} title="Ниже" onClick={() => move(i, 1)}>▼</button>
            </div>
            <button className="ib" style={IBTN} title="Переименовать" onClick={(e) => {
              e.stopPropagation(); setRename({ old: c.name, value: c.name });
            }}><DIcon n="pencil" size={14} /></button>
            <button className="ib danger" style={IBTN} title="Удалить раздел" onClick={(e) => {
              e.stopPropagation();
              confirm('Удалить раздел?',
                `Модели раздела «${c.name}» станут «вне каталога» — не потеряются.`,
                async () => {
                  try {
                    await call('adminCollectionEdit', { action: 'delete_category', name: c.name });
                    if (cat === c.name) pick(NONE);
                    toast('Раздел удалён', { kind: 'ok' }); reload();
                  } catch (err) { toast(err.message, { kind: 'warn2' }); }
                });
            }}><DIcon n="trash" size={14} /></button>
          </div>)}
          {/* Псевдо-раздел: пунктиром, как в макете — это не строка каталога, а остаток. */}
          <div className="qrow" style={{
            border: '1px dashed var(--line-2)', borderRadius: 11, marginTop: 8,
            ...(cat === NONE ? SELROW : {}),
          }} onClick={() => pick(NONE)}>
            <div className="pos">·</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="nm" style={{ color: 'var(--muted)' }}>Вне каталога</div>
              <div className="ds">
                {num((root.data && root.data.uncat_count) || 0)} моделей — не видны клиентам
              </div>
            </div>
          </div>
        </div>}</Screen>
        {/* На месте формы создания из макета: у бэкенда нет действия create_category —
            раздел заводится переносом первой модели, поэтому тут только пояснение. */}
        <div className="hint" style={{ marginTop: 12 }}>
          Раздел появляется сам, когда в него переносят первую модель.
        </div>
      </div>
    </div>

    <div className="det"><div className="od">
      <DetHead title={catTitle}
        sub={`${num(catCount)} моделей · клик по фото — увеличить · селект — перенести в раздел`} />
      <div className="od-body">
        {/* display:block — иначе .od-main наследует грид 1fr 1fr и .mgrid схлопнется. */}
        <div className="od-main" style={{ display: 'block' }}>
          <div className="ms-tools">
            <Search value={q} onChange={setQ} placeholder="Название модели…" />
            {/* Фильтр клиентский: ищет по загруженным моделям — честно говорим, сколько их. */}
            <div className="hint" style={{ marginLeft: 'auto', whiteSpace: 'nowrap' }}>
              {num(shown.length)} из {num(models.length)} загруженных
              {more ? ` · всего ${num(total)}` : ''}
            </div>
            {q && more ? <ABtn kind="soft" icon="refresh" onClick={loadAll}>
              Искать по всем ({num(total - models.length)})
            </ABtn> : null}
          </div>
          {/* Одна форма выбора файла на весь экран — цель хранится в photoFor. */}
          <input type="file" accept="image/*" ref={fileRef} style={{ display: 'none' }} onChange={onFile} />
          <Screen state={{ ...items, reload: reloadItems }}>{() => shown.length ? <>
            <div className="mgrid">
              {shown.map((m) => <div key={m.id} className="mcard">
                {m.photo
                  ? <img src={m.photo} alt="" title="клик — увеличить"
                      style={{ cursor: 'zoom-in' }} onClick={() => lightbox(m.photo)} />
                  : <div className="mnoimg" title="клик — добавить фото"
                      style={{ cursor: 'pointer' }} onClick={() => pickPhoto(m.id)}>
                      <DIcon n="camera" size={22} />
                    </div>}
                {mren && mren.id === m.id
                  ? <input className="inp" autoFocus value={mren.value}
                      onChange={(e) => setMren({ ...mren, value: e.target.value })}
                      onKeyDown={async (e) => {
                        if (e.key === 'Escape') setMren(null);
                        if (e.key === 'Enter' && mren.value.trim()) {
                          const label = mren.value.trim();
                          try {
                            await call('adminCollectionEdit', { action: 'rename', id: m.id, label });
                            setMren(null); patch(m.id, { label });
                            toast('Модель переименована', { kind: 'ok' });
                          } catch (err) { toast(err.message, { kind: 'warn2' }); }
                        }
                      }} />
                  : <div className="nm" style={{ cursor: 'text' }} title="клик — переименовать"
                      onClick={() => setMren({ id: m.id, value: m.label || '' })}>
                      {m.label} <DIcon n="pencil" size={11} style={{ color: 'var(--faint)' }} />
                    </div>}
                <div className="ds">расчётов: {num(m.uses)}</div>
                <div style={{ display: 'flex', gap: 7 }}>
                  <select className="sel" style={{ flex: 1, padding: '7px 30px 7px 10px', fontSize: 12.5 }}
                    value={m.category || ''} onChange={async (e) => {
                      const category = e.target.value;
                      try {
                        await call('adminCollectionEdit', { action: 'set_category', id: m.id, category });
                        // Открыт конкретный раздел, а модель ушла в другой — убираем её из списка;
                        // счётчики разделов меняются на бэкенде, поэтому шапку перечитываем.
                        const gone = (cat === NONE ? category !== '' : category !== cat);
                        if (gone) { drop(m.id); shiftOff(); } else { patch(m.id, { category }); }
                        toast('Модель перенесена', { kind: 'ok' }); root.reload();
                      } catch (err) { toast(err.message, { kind: 'warn2' }); }
                    }}>
                    <option value="">— вне каталога —</option>
                    {cats.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                  </select>
                  <button className="ib" style={IBTN}
                    title={m.has_photo ? 'Заменить фото позиции' : 'Добавить фото позиции'}
                    onClick={() => pickPhoto(m.id)}><DIcon n="camera" size={14} /></button>
                  <button className="ib danger" style={IBTN} title="Удалить модель" onClick={() => {
                    confirm('Удалить модель?', `«${m.label}» исчезнет из коллекции.`, async () => {
                      try {
                        await call('adminCollectionEdit', { action: 'delete', id: m.id });
                        drop(m.id); shiftOff();
                        toast('Модель удалена', { kind: 'ok' }); root.reload();
                      } catch (err) { toast(err.message, { kind: 'warn2' }); }
                    });
                  }}><DIcon n="trash" size={14} /></button>
                </div>
              </div>)}
            </div>
            {/* Потолка больше нет: тянем страницами, пока сервер отдаёт has_more. */}
            {more ? <button className="db line full" style={{ marginTop: 18 }}
              disabled={busy} onClick={() => loadPage(false)}>
              {busy ? 'Загрузка…' : `Показать ещё (${num(total - models.length)})`}
            </button> : null}
          </> : <Empty ic="grid" t="Моделей нет"
            ds={q ? 'Попробуйте другой запрос' : 'В этом разделе пусто'} />}
          </Screen>
        </div>
      </div>
    </div></div>
  </div>;
}
