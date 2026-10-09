// tasks.jsx — «Задачи»: напоминания мастера. Часть заводится сама (дни рождения клиентов).

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { Screen, Chips, ABtn } from '../parts.jsx';

const { useState, useContext } = React;

export function TasksScreen() {
  const { toast, nav } = useContext(AppCtx);
  const [all, setAll] = useState(false);
  const [title, setTitle] = useState('');
  const [due, setDue] = useState('');
  const t = useApi('adminTasks', [all], [all], { auto: 30000 });
  const tasks = (t.data && t.data.tasks) || [];

  const add = async () => {
    if (!title.trim()) return;
    await call('adminTask', { action: 'add', title: title.trim(), due });
    setTitle(''); setDue('');
    t.reload();
  };

  // Один обработчик на чекбокс .cb и на кнопку «↻ Вернуть» — действие одно и то же.
  const tog = async (x) => {
    await call('adminTask', { id: x.id, action: x.done ? 'reopen' : 'done' });
    toast(x.done ? 'Возвращена в работу' : 'Готово', { kind: 'ok' });
    t.reload();
  };

  return <div className="onecol narrow">
    <div className="ms-tools">
      {/* При all=false сервер отдаёт только активные, поэтому общий счётчик известен
          лишь когда включён фильтр «Все» — иначе цифру не показываем, чтобы не врать. */}
      <Chips items={[{ k: 'open', l: 'Активные', n: tasks.filter((x) => !x.done).length },
        { k: 'all', l: 'Все', n: all ? tasks.length : undefined }]}
        value={all ? 'all' : 'open'} onChange={(k) => setAll(k === 'all')} style={{ margin: 0 }} />
      <span style={{ flex: 1 }} />
    </div>

    <div className="card" style={{ marginBottom: 16, flexDirection: 'row', gap: 10, alignItems: 'center' }}>
      <input className="inp" value={title} placeholder="Новая задача…" style={{ flex: 1 }}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
      {/* Подпись ровно по разбору на бэкенде (_parse_task_due): дата или «через N дней». */}
      <input className="inp mono" value={due} placeholder="ДД.ММ / +3"
        onChange={(e) => setDue(e.target.value)} style={{ width: 130, flex: '0 0 auto' }} />
      <ABtn disabled={!title.trim()} onClick={add} icon="plus">Добавить</ABtn>
    </div>

    <Screen state={t}>{() => tasks.length ? <div className="twrap" style={{ flex: 1 }}>
      {tasks.map((x) => <div key={x.id} className={'task' + (x.done ? ' done' : '')}
        style={{ padding: '14px 18px', borderBottom: '1px solid var(--line)', borderRadius: 0 }}>
        <button className="cb" onClick={() => tog(x)}>
          {x.done ? <DIcon n="check" size={13} sw={3} /> : null}
        </button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="tt" style={{ fontSize: 15 }}>{x.title}</div>
          {x.client || x.due_label
            ? <div className={'dd' + (x.overdue ? ' red' : (x.today ? ' hot' : ''))}>
              {x.client ? <span style={{ cursor: 'pointer' }}
                onClick={() => nav('clients', { sel: x.user_id })}>{x.client}</span> : null}
              {x.client && x.due_label ? ' · ' : ''}
              {x.overdue ? '⏰ ' : ''}{x.due_label}
            </div>
            : null}
        </div>
        {x.done ? <button className="db sm line" onClick={() => tog(x)}>↻ Вернуть</button> : null}
        <button className="ib danger" title="Удалить" style={{ width: 32, height: 32 }}
          onClick={async () => {
            await call('adminTask', { id: x.id, action: 'delete' });
            toast('Задача удалена', { kind: 'ok' });
            t.reload();
          }}><DIcon n="trash" size={15} /></button>
      </div>)}
    </div> : <Empty ic="check" t={all ? 'Задач нет' : 'Все задачи сделаны'}
      ds="Дни рождения клиентов попадают сюда сами, за 3 дня" />}
    </Screen>
  </div>;
}
