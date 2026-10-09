// staff.jsx — «Команда»: сотрудники, роли и права (A4).
//
// Владелец приглашает сотрудника по Telegram ID и выбирает роль. Оператор работает с
// заказами и принтером, но не видит цен, выручки, клиентской базы и настроек. Владельцы из
// .env (root) здесь только показаны: понизить или удалить их из приложения нельзя — иначе
// владелец запирает сам себя, и вернуть доступ выйдет только правкой .env с перезапуском.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Field, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { Screen, ABtn, useConfirm } from '../parts.jsx';

const { useState, useContext } = React;

export function StaffScreen() {
  const { toast } = useContext(AppCtx);
  const s = useApi('adminStaff', [], []);
  const [f, setF] = useState({ user_id: '', name: '', role: 'operator', note: '' });
  const confirm = useConfirm();
  const staff = (s.data && s.data.staff) || [];
  const roles = (s.data && s.data.roles) || [];
  const caps = (s.data && s.data.caps) || [];
  const set = (k, v) => setF((x) => ({ ...x, [k]: v }));

  const save = async (body) => {
    await call('adminStaffSave', body);
    toast('Готово', { kind: 'ok' });
    s.reload();
  };

  const add = async () => {
    if (!String(f.user_id).trim()) { toast('Нужен Telegram ID', { kind: 'err' }); return; }
    await save({ action: 'save', user_id: f.user_id, name: f.name, role: f.role, note: f.note });
    setF({ user_id: '', name: '', role: 'operator', note: '' });
  };

  const remove = (row) => confirm(
    'Убрать из команды?', `${row.name || 'id ' + row.user_id} потеряет доступ к админке.`,
    () => save({ action: 'del', user_id: row.user_id }),
  );

  return <Screen state={s}>{() => <div className="grid2">
    <Sec t="Роли и что они дают">
      {roles.map((r) => <div key={r.id} style={{ marginBottom: 12 }}>
        <b>{r.label}</b>
        <div className="hint" style={{ marginTop: 3 }}>{r.hint}</div>
        <div style={{ marginTop: 6, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {(r.caps || []).map((c) => <span key={c} className="stb gray">
            {(caps.find((x) => x.id === c) || {}).label || c}
          </span>)}
        </div>
      </div>)}
    </Sec>

    <Sec t="Пригласить сотрудника">
      <div className="fgap">
        <div className="frow">
          <Field l="Telegram ID">
            <input className="inp mono" value={f.user_id} placeholder="123456789"
              onChange={(e) => set('user_id', e.target.value)} />
          </Field>
          <Field l="Имя">
            <input className="inp" value={f.name} placeholder="Как показывать в списке"
              onChange={(e) => set('name', e.target.value)} />
          </Field>
        </div>
        <Field l="Роль">
          <select className="inp" value={f.role} onChange={(e) => set('role', e.target.value)}>
            {roles.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
          </select>
        </Field>
        <div className="hint">
          Telegram ID видно в карточке клиента и в ленте активности — человек должен хотя бы
          раз написать боту. Доступ включается сразу, перезапуск не нужен.
        </div>
        <ABtn onClick={add}><DIcon n="plus" size={15} />Добавить в команду</ABtn>
      </div>
    </Sec>

    <Sec t="Команда" span2>
      {staff.length ? <div className="twrap">
        <table className="tbl">
          <thead><tr>
            <th style={{ cursor: 'default' }}>Сотрудник</th>
            <th style={{ width: 140, cursor: 'default' }}>Telegram ID</th>
            <th style={{ width: 200, cursor: 'default' }}>Роль</th>
            <th style={{ width: 150, cursor: 'default' }}>Вход в админку</th>
            <th style={{ width: 60, cursor: 'default' }}></th>
          </tr></thead>
          <tbody>
            {staff.map((row) => <tr key={row.user_id} className="row">
              <td className="oname">{row.name || '—'}</td>
              <td className="mono faint">{row.user_id}</td>
              <td>
                {row.root
                  ? <span className="stb green" title="Задан в .env — здесь не меняется">
                      {row.role_label} · из настроек сервера
                    </span>
                  : <select className="inp" value={row.role}
                      onChange={(e) => save({ action: 'save', user_id: row.user_id, name: row.name, role: e.target.value })}>
                      {roles.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
                    </select>}
              </td>
              {/* Сколько живых входов в настольную админку. Кнопка «выйти везде» — отдельно
                  от «убрать»: телефон забыт разблокированным или человек в отпуске, а снятие
                  роли — действие другого веса. Снятие сессии тоже гасит, но не наоборот. */}
              <td>
                {row.sessions
                  ? <span className="stb green" style={{ marginRight: 6 }}>вошёл · {row.sessions}</span>
                  : <span className="faint">не входил</span>}
                {row.sessions
                  ? <button className="ib" title="Завершить сессии на всех компьютерах"
                      onClick={() => save({ action: 'logout', user_id: row.user_id })}>
                      <DIcon n="back" size={14} />
                    </button>
                  : null}
              </td>
              <td>{row.root ? null
                : <button className="ib danger" title="Убрать" onClick={() => remove(row)}><DIcon n="trash" /></button>}</td>
            </tr>)}
          </tbody>
        </table>
      </div> : <Empty ic="users" t="В команде только вы" ds="Добавьте оператора, чтобы разделить работу" />}
    </Sec>
  </div>}</Screen>;
}
