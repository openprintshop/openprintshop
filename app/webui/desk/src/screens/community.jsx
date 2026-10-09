// community.jsx — «Сообщество»: база знаний студии + вход в чат владельцев (c356387a).
//
// Раздел адресован ВЛАДЕЛЬЦУ студии и его операторам, а не заказчикам мастерской: база
// знаний — инструкции для себя (как настроить, откалибровать, что делать при типовой
// проблеме), а «чат владельцев» — внешняя Telegram-группа, где общаются владельцы РАЗНЫХ
// студий. Своего сервера у продукта нет и установки друг о друге не знают, поэтому
// площадка внешняя, а её адрес лежит в настройке owners_chat_url.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Field, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { Screen, Split, ABtn, DetHead, PickOne, useConfirm } from '../parts.jsx';

const { useState, useContext } = React;

const WRAP = { maxWidth: 1500, width: '100%', margin: '0 auto' };
const BLANK = { id: 0, title: '', body: '', sort: 0, published: 0 };

export function CommunityScreen() {
  return <div className="onecol" style={WRAP}>
    <OwnersChatCard />
    <KbTab />
  </div>;
}

/** Вход в сообщество владельцев студий: внешняя Telegram-группа, адрес из настроек. */
function OwnersChatCard() {
  const s = useApi('adminSettings', [], []);
  const grp = (s.data && s.data.owners) || {};      // {order, labels, values} — как у pay/brand
  const url = String((grp.values || {}).owners_chat_url || '').trim();
  // ВАЖНО: .card в desktop.css — flex-КОЛОНКА; без явного row содержимое складывается
  // столбиком по центру (так и выглядел «кривой» первый вариант карточки).
  return <div className="card" style={{ padding: '14px 18px', marginBottom: 14, display: 'flex',
    flexDirection: 'row', alignItems: 'center', gap: 14, flexWrap: 'wrap', flex: '0 0 auto' }}>
    <div style={{ width: 40, height: 40, borderRadius: 12, flex: '0 0 auto', display: 'flex',
      alignItems: 'center', justifyContent: 'center', background: 'var(--surface-2)',
      color: 'var(--accent)' }}>
      <DIcon n="chat" size={19} />
    </div>
    <div style={{ flex: 1, minWidth: 260 }}>
      <b style={{ fontSize: 14 }}>Сообщество владельцев студий</b>
      <div className="hint" style={{ marginTop: 2 }}>
        {url
          ? 'Группа в Telegram, где владельцы студий разбирают печать, цены и оборудование.'
          : 'Ссылка на группу не задана. Впишите её в Настройках — раздел «Сообщество владельцев».'}
      </div>
    </div>
    {url
      ? <a className="db" style={{ textDecoration: 'none' }} href={url} target="_blank"
          rel="noreferrer noopener">
          {/* белый заливной самолётик, как у Telegram — по просьбе владельца */}
          <DIcon n="tgplane" size={15} style={{ color: '#fff' }} />Открыть чат
        </a>
      : null}
  </div>;
}

/** База знаний студии: слева список статей, справа редактор. Черновик виден только здесь. */
function KbTab() {
  const { toast } = useContext(AppCtx);
  const [sel, setSel] = useState(null);
  const k = useApi('adminKb', [], []);
  const arts = (k.data && k.data.articles) || [];
  const cur = sel === 0 ? BLANK : arts.find((a) => a.id === sel) || null;

  const listPane = <>
    <div className="ms-tools">
      <div className="hint" style={{ flex: 1 }}>
        Инструкции для себя и операторов: настройка, калибровка, типовые проблемы.
        Клиентам мастерской они не показываются. Порядок задаёт поле «Порядок».
      </div>
      <button className="db" onClick={() => setSel(0)}><DIcon n="plus" size={15} />Новая статья</button>
    </div>
    <Screen state={k}>{() => (arts.length ? <div className="twrap">
      <table className="tbl">
        <thead><tr>
          <th style={{ cursor: 'default' }}>Заголовок</th>
          <th style={{ width: 110, textAlign: 'right', cursor: 'default' }}>Порядок</th>
          <th style={{ width: 150, cursor: 'default' }}>Состояние</th>
          <th style={{ width: 180, cursor: 'default' }}>Изменена</th>
        </tr></thead>
        <tbody>
          {arts.map((a) => <tr key={a.id} className={'row' + (a.id === sel ? ' sel' : '')}
            onClick={() => setSel(a.id)}>
            <td className="oname">{a.title}</td>
            <td className="mono" style={{ textAlign: 'right' }}>{a.sort}</td>
            <td>{a.published
              ? <span className="stb green">опубликовано</span>
              : <span className="stb gray">черновик</span>}</td>
            <td className="mono faint">{a.updated_at || '—'}</td>
          </tr>)}
        </tbody>
      </table>
    </div> : <Empty ic="doc" t="Статей ещё нет"
      ds="Первая статья снимает самый частый вопрос — обычно это выбор пластика"
      cta="Новая статья" onCta={() => setSel(0)} />)}
    </Screen>
  </>;

  return <Split width={1120} list={listPane}
    detail={cur ? <ArticleCard key={cur.id} a={cur} toast={toast}
      onSaved={(id) => { setSel(id); k.reload(); }}
      onDeleted={() => { setSel(null); k.reload(); }} />
      : <PickOne what="статью" />} />;
}

function ArticleCard({ a, onSaved, onDeleted, toast }) {
  const [f, setF] = useState({ ...BLANK, ...a });
  const confirm = useConfirm();
  const set = (kk, v) => setF((s) => ({ ...s, [kk]: v }));
  const isNew = !f.id;

  const save = async () => {
    const res = await call('adminKbSave', {
      id: f.id || 0, title: String(f.title || '').trim(), body: f.body || '',
      sort: Number(f.sort) || 0, published: f.published ? 1 : 0,
    });
    toast(isNew ? 'Статья создана' : 'Статья сохранена', { kind: 'ok' });
    onSaved(res.id || f.id);
  };

  const del = () => confirm('Удалить статью?',
    `«${f.title}» пропадёт из базы знаний студии. Восстановить будет нечем.`,
    async () => { await call('adminKbDelete', f.id); toast('Статья удалена', { kind: 'ok' }); onDeleted(); });

  return <div className="od">
    <DetHead title={isNew ? 'Новая статья' : f.title} sub="база знаний студии"
      right={isNew ? null : <button className="ib danger" title="Удалить" onClick={del}><DIcon n="trash" /></button>} />
    <div className="od-body">
      <div className="od-main">
        {/* .od-main — сетка в две колонки: span2 растягивает редактор на обе */}
        <Sec t="Статья" span2 style={{ maxWidth: 860 }}>
          <div className="fgap">
            <Field l="Заголовок">
              <input className="inp" value={f.title} maxLength={200}
                placeholder="Например, «Чем PLA отличается от PETG»"
                onChange={(e) => set('title', e.target.value)} />
            </Field>
            <Field l="Текст">
              {/* Абзацы разделяются пустой строкой — так же их разбирает мини-апп. */}
              <textarea className="inp" style={{ minHeight: 320, resize: 'vertical', lineHeight: 1.55 }}
                value={f.body} placeholder="Абзацы разделяйте пустой строкой"
                onChange={(e) => set('body', e.target.value)} />
            </Field>
            <div className="frow">
              <Field l="Порядок в списке">
                <input className="inp mono" type="number" step="1" value={f.sort}
                  onChange={(e) => set('sort', e.target.value)} />
              </Field>
            </div>
            <label className="chk">
              <input type="checkbox" checked={!!f.published}
                onChange={(e) => set('published', e.target.checked ? 1 : 0)} />
              Готова (не черновик)
            </label>
            <div className="hint">
              Черновик — пометка для себя: статья остаётся в этом списке со значком
              «черновик», дописывайте её в несколько заходов.
            </div>
            <ABtn disabled={!String(f.title).trim()} onClick={save}>
              {isNew ? 'Создать' : 'Сохранить'}
            </ABtn>
          </div>
        </Sec>
      </div>
    </div>
  </div>;
}
