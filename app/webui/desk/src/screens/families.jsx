// families.jsx — «Семейные счета»: общий баланс на нескольких человек.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Field, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { money } from '../fmt.jsx';
import { Screen, ABtn, Chips, useConfirm } from '../parts.jsx';

const { useState, useContext } = React;

const STATUS_LABEL = { done: 'выполнено', rejected: 'отклонено' };

export function FamiliesScreen() {
  const { toast, nav } = useContext(AppCtx);
  // «Новые / Все»: сервер отдаёт обработанные и отклонённые заявки только по ?all=1
  // (_api_admin_families → list_admin_notifications(include_done=...)).
  const [all, setAll] = useState(false);
  const f = useApi('adminFamilies', [all], [all], { auto: 30000 });
  // Состав будущей семьи по каждой заявке: {notif_id: ['uid', ...]}. Ключ 0 — ручная форма.
  // Держим здесь, а не внутри карточки, чтобы правка состава пережила reload списка.
  const [memberSel, setMemberSel] = useState({});
  // выбранный кандидат в каждой карточке семьи: ключ — id семьи.
  // Добавление коммитит кнопка, а не onChange — иначе случайный выбор в списке
  // сразу дописывает человека в чужой счёт без шанса передумать.
  const [addSel, setAddSel] = useState({});
  const confirm = useConfirm();

  return <div className="col3">
    <Screen state={f}>{(d) => {
      const customers = d.customers || [];
      const requests = d.requests || [];
      const opts = customers.map((c) => <option key={c.uid} value={c.uid}>{c.name}</option>);
      const nameOf = (uid) => {
        const c = customers.find((x) => String(x.uid) === String(uid));
        return c ? c.name : `id ${uid}`;
      };

      // Партнёра в старых заявках указывали текстом («@username или имя») — подбираем
      // клиента по подстроке, чтобы мастеру не приходилось искать его в списке вручную.
      const matchCustomer = (text) => {
        const t = String(text || '').toLowerCase().replace(/^@/, '');
        if (!t) return '';
        const hit = customers.find((c) => (c.name || '').toLowerCase().replace(/^@/, '').includes(t));
        return hit ? String(hit.uid) : '';
      };
      // Состав по умолчанию: автор заявки + партнёр (id из нативного пикера либо подбор по имени).
      const membersOf = (r) => {
        if (memberSel[r.id]) return memberSel[r.id];
        const p = r.payload || {};
        return [String(p.from_id || ''), p.partner_id ? String(p.partner_id) : matchCustomer(p.partner)];
      };
      const setMembers = (nid, arr) => setMemberSel((m) => ({ ...m, [nid]: arr }));

      // Заявитель и партнёр сплошь и рядом без единого заказа (в том и смысл: зовут
      // близкого, который сам ничего не заказывал) — в списке клиентов их нет, и без
      // этой доп-опции предвыбор невидим, а заявку не одобрить. Имя берём из payload,
      // иначе в селекте светится голый id и мастер рискует перевыбрать однофамильца,
      // чей личный баланс уйдёт в чужую семью.
      // Опции строим от ЗАЯВКИ, а не от текущего значения слота: иначе, перещёлкнув слот
      // на другого клиента, вернуть заявителя обратно уже нечем — его строки в списке нет,
      // и заявку пришлось бы отклонять и просить клиента подать заново.
      const extraOpts = (r, uid) => {
        const p = r.payload || {};
        const known = (id) => !id || customers.some((c) => String(c.uid) === String(id));
        const out = [];
        if (!known(p.from_id)) {
          out.push(<option key={`req${p.from_id}`} value={p.from_id}>
            {(p.from_name || `id ${p.from_id}`) + ' · заявитель, заказов нет'}
          </option>);
        }
        if (!known(p.partner_id) && String(p.partner_id) !== String(p.from_id)) {
          out.push(<option key={`req${p.partner_id}`} value={p.partner_id}>
            {(p.partner || `id ${p.partner_id}`) + ' · партнёр, заказов нет'}
          </option>);
        }
        // выбранный вручную id, которого нет ни в клиентах, ни в заявке — чтобы не пропал
        if (uid && known(uid) === false && !out.some((o) => String(o.props.value) === String(uid))) {
          out.push(<option key={`sel${uid}`} value={uid}>{`id ${uid}`}</option>);
        }
        return out;
      };

      // Открытие счёта необратимо переносит личные балансы в общий — спрашиваем всегда,
      // перечислив поимённо, кого именно объединяем.
      const createFamily = (nid, ids) => {
        const clean = [...new Set(ids.filter(Boolean).map(Number))];
        if (clean.length < 2) { toast('Выберите минимум двух клиентов', { kind: 'warn2' }); return; }
        // Имя заявителя/партнёра без заказов в списке клиентов не найдётся — берём из payload.
        const r = requests.find((x) => x.id === nid) || {};
        const p = r.payload || {};
        const names = clean.map((uid) => {
          const c = customers.find((x) => x.uid === uid);
          if (c) return c.name;
          if (String(p.from_id) === String(uid) && p.from_name) return p.from_name;
          if (String(p.partner_id) === String(uid) && p.partner) return p.partner;
          return `id ${uid}`;
        });
        confirm('Открыть семейный счёт?',
          `Участники: ${names.join(' + ')}. Личные балансы будут перенесены в общий `
          + '(плюс бонус за открытие). Отменить это одной кнопкой нельзя.',
          async () => {
            try {
              await call('adminFamilyCreate', clean, nid || 0);
              toast('Семейный счёт открыт', { kind: 'ok' });
              setMembers(nid || 0, ['', '']);
              f.reload();
            } catch (e) {
              toast(e.message, { kind: 'warn2' });
            }
          }, 'Открыть счёт');
      };

      const manual = memberSel[0] || ['', ''];

      return <>
        <div className="colw">
          <Chips items={[{ k: 'new', l: 'Новые', n: requests.filter((x) => x.status === 'new').length },
            { k: 'all', l: 'Все' }]}
            value={all ? 'all' : 'new'} onChange={(k) => setAll(k === 'all')} />

          {requests.length
            ? requests.map((r) => {
              const isNew = r.status === 'new';
              const members = membersOf(r);
              return <div key={r.id} className="card notif"
                style={{ flex: '0 0 auto', borderLeftColor: 'var(--violet)', opacity: isNew ? 1 : 0.6 }}>
                <div className="lab"><DIcon n="users" size={13} />
                  {isNew ? 'Заявка · новая' : `Заявка · ${STATUS_LABEL[r.status] || r.status}`}
                </div>
                <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 9 }}>
                  <DIcon n="users" size={17} style={{ color: 'var(--violet)' }} />{r.title}
                </div>
                <div style={{ color: 'var(--muted)', fontSize: 14, marginBottom: 14 }}>{r.body}</div>
                {isNew ? <>
                  <div className="fgap" style={{ marginBottom: 12 }}>
                    {members.map((v, i) => <Field key={i} l={`Участник ${i + 1}`}>
                      <select className="sel" value={v || ''}
                        onChange={(e) => { const a = members.slice(); a[i] = e.target.value; setMembers(r.id, a); }}>
                        <option value="">выберите…</option>
                        {opts}
                        {extraOpts(r, v)}
                      </select>
                    </Field>)}
                    <span className="chip" onClick={() => setMembers(r.id, members.concat(['']))}>
                      + ещё участник
                    </span>
                  </div>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <ABtn style={{ flex: 1 }} icon="check"
                      onClick={() => createFamily(r.id, members)}>Создать счёт</ABtn>
                    <ABtn kind="line" onClick={async () => {
                      try {
                        await call('adminNotificationAction', r.id, 'reject');
                        toast('Заявка отклонена', { kind: 'ok' });
                        f.reload();
                      } catch (e) {
                        toast(e.message, { kind: 'warn2' });
                      }
                    }}>Отклонить</ABtn>
                  </div>
                </> : null}
              </div>;
            })
            : <Empty ic="users" t="Заявок нет" ds="Никто не просит объединить балансы" />}
        </div>

        <div className="colw">
          {(d.families || []).length ? (d.families || []).map((fam) => <Sec key={fam.id} card
            ic="users" t={fam.title || `Счёт #${fam.id}`} style={{ flex: '0 0 auto' }}
            right={<span className="mono" style={{ color: 'var(--price)', fontWeight: 700, fontSize: 15 }}>
              {money(fam.balance)}
            </span>}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
              {(fam.members || []).map((m) => <span key={m.user_id} className="chip"
                onClick={() => nav('clients', { sel: m.user_id })}>
                {m.user_name}
                <span style={{ color: 'var(--faint)' }} onClick={(e) => {
                  e.stopPropagation();
                  const last = (fam.members || []).length <= 2;
                  confirm('Исключить участника?',
                    last
                      // Семья из двух: убрать одного — значит распустить её целиком,
                      // и остаток общего счёта уедет на личный баланс оставшегося.
                      ? `${m.user_name} будет исключён. Останется один человек — семья `
                        + 'распустится, остаток общего счёта вернётся ему на личный баланс.'
                      : `${m.user_name} потеряет доступ к общему балансу. Общий счёт останется семье.`,
                    async () => {
                      try {
                        const res = await call('adminFamilyRemove', fam.id, m.user_id);
                        toast(res.dissolved ? 'Семья распущена' : 'Участник исключён', { kind: 'ok' });
                        f.reload();
                      } catch (e) {
                        toast(e.message, { kind: 'warn2' });
                      }
                    }, 'Исключить');
                }}>✕</span>
              </span>)}
            </div>
            <div style={{ display: 'flex', gap: 9 }}>
              <select className="sel" style={{ flex: 1 }} value={addSel[fam.id] || ''}
                onChange={(e) => setAddSel({ ...addSel, [fam.id]: e.target.value })}>
                <option value="">+ добавить участника…</option>
                {opts}
              </select>
              <ABtn kind="soft sm" icon="plus" title="Добавить участника"
                disabled={!addSel[fam.id]} onClick={async () => {
                  try {
                    await call('adminFamilyAdd', fam.id, Number(addSel[fam.id]));
                    toast('Участник добавлен', { kind: 'ok' });
                    setAddSel({ ...addSel, [fam.id]: '' });
                    f.reload();
                  } catch (e) {
                    toast(e.message, { kind: 'warn2' });
                  }
                }} />
            </div>
          </Sec>) : <Empty ic="users" t="Счетов нет" ds="Общий баланс ещё никто не открывал" />}
        </div>

        <div className="colw">
          <Sec card ic="plus" t="Открыть счёт вручную" style={{ flex: '0 0 auto' }}>
            <div className="fgap">
              {manual.map((v, i) => <Field key={i} l={`Участник ${i + 1}`}>
                <select className="sel" value={v || ''}
                  onChange={(e) => { const a = manual.slice(); a[i] = e.target.value; setMembers(0, a); }}>
                  <option value="">выберите…</option>
                  {opts}
                </select>
              </Field>)}
              <span className="chip" onClick={() => setMembers(0, manual.concat(['']))}>
                + ещё участник
              </span>
              <ABtn kind="soft" disabled={manual.filter(Boolean).length < 2}
                onClick={() => createFamily(0, manual)}>Создать счёт</ABtn>
            </div>
            <div className="hint" style={{ marginTop: 12 }}>
              семья делит баланс, реферальные бонусы и скидку за объём
            </div>
          </Sec>
        </div>
      </>;
    }}</Screen>
  </div>;
}
