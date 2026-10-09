// errors.jsx — «Ошибки»: телеметрия сбоев (OpenTodo f4de8627).
//
// Единственное место, где видно всё упавшее: веб-API, хендлеры бота, фоновые циклы и фронт
// мини-аппа. Одинаковые сбои схлопнуты по отпечатку, поэтому «×N» это повторы одной строки,
// а не отдельные записи. «Прочитано» закрывает строку: вернувшийся после разбора сбой
// заведёт новую и пришлёт новый алерт — так видно рецидив, а не подросший счётчик.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { Screen, Chips, ABtn, useConfirm } from '../parts.jsx';

const { useState, useContext, useCallback } = React;

// Полоса карточек: на широком мониторе трейсбек без ограничения растянулся бы на километр.
const WRAP = { maxWidth: 1500, width: '100%', margin: '0 auto' };

// Источник сбоя: подпись и цвет метки. Ключи — source из app/errorlog.py.
const SRC = {
  web: { l: 'веб-API', c: 'var(--blue)' },
  tg: { l: 'бот', c: 'var(--price)' },
  log: { l: 'лог', c: 'var(--amber)' },
  js: { l: 'мини-апп', c: 'var(--violet)' },
  task: { l: 'фон', c: 'var(--faint)' },
  backup: { l: 'бэкап', c: 'var(--faint)' },
};

export function ErrorsScreen() {
  const { toast, reloadHub } = useContext(AppCtx);
  // Экран здоровья без автообновления показывал бы «всё тихо» уже после аварии.
  const e = useApi('adminErrors', [], [], { auto: 15000 });
  // Фильтр клиентский: сервер и так отдаёт последние записи, лишний round-trip ни к чему.
  const [onlyNew, setOnlyNew] = useState(true);
  const confirm = useConfirm();

  const all = (e.data && e.data.errors) || [];
  const items = onlyNew ? all.filter((x) => !x.seen) : all;
  const nnew = (e.data && e.data.new_count) || 0;

  // После отметки перечитываем и сводку: бейдж «Ошибки» в сайдбаре живёт в adminHub.
  const after = useCallback(() => { e.reload(); reloadHub(); }, [e, reloadHub]);

  const seen = async (id) => {
    try {
      await call('adminErrorsSeen', id);
      toast(id ? 'Отмечено прочитанным' : 'Все отмечены прочитанными', { kind: 'ok' });
      after();
    } catch (err) { toast(err.message, { kind: 'warn2' }); }
  };

  return <div className="onecol" style={WRAP}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
      <Chips items={[{ k: 'new', l: 'Только новые', n: nnew }, { k: 'all', l: 'Все' }]}
        value={onlyNew ? 'new' : 'all'} onChange={(k) => setOnlyNew(k === 'new')} />
      <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
        {nnew > 0 ? <ABtn kind="sm soft" icon="check"
          onClick={() => confirm('Отметить все прочитанными?',
            'Записи уйдут из «Новых». Если сбой повторится, он заведёт новую строку и пришлёт алерт.',
            () => seen(0), 'Прочитать все', false)}>Прочитать все</ABtn> : null}
        <button className="ib" title="Обновить" onClick={() => e.reload()}>
          <DIcon n="refresh" size={16} />
        </button>
      </span>
    </div>

    <Screen state={e}>{() => (items.length
      ? <div className="fgap" style={{ gap: 12, marginTop: 14, overflowY: 'auto', minHeight: 0 }}>
          {items.map((x) => <ErrRow key={x.id} x={x} onSeen={() => seen(x.id)} />)}
        </div>
      : <Empty ic="shield" t={onlyNew ? 'Новых ошибок нет' : 'Ошибок не записано'}
          ds="Сюда попадает всё, что упало: веб-API, бот, фоновые задачи и мини-апп" />)}
    </Screen>
  </div>;
}

function ErrRow({ x, onSeen }) {
  const [open, setOpen] = useState(false);
  const s = SRC[x.source] || { l: x.source, c: 'var(--faint)' };
  return <div className="card" style={{ padding: '14px 16px', flex: '0 0 auto',
    ...(x.seen ? { opacity: 0.6 } : null) }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
      <span style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.4,
        padding: '2px 8px', borderRadius: 6, border: `1px solid ${s.c}`, color: s.c }}>{s.l}</span>
      {x.level && x.level !== 'error'
        ? <span className="hint" style={{ textTransform: 'uppercase', fontSize: 11 }}>{x.level}</span> : null}
      {x.count > 1
        ? <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--red)' }}>×{x.count}</span> : null}
      <span className="mono" style={{ marginLeft: 'auto', fontSize: 11.5, color: 'var(--faint)' }}>{x.last_at}</span>
    </div>

    {/* Сообщение моноширинно и кликабельно: почти всегда это «Тип: текст» из питона. */}
    <div className="mono" style={{ fontSize: 13, marginTop: 8, lineHeight: 1.45, wordBreak: 'break-word',
      cursor: x.traceback ? 'pointer' : 'default' }}
      onClick={() => { if (x.traceback) setOpen(!open); }}>{x.message}</div>
    {x.context ? <div className="hint" style={{ marginTop: 4 }}>{x.context}</div> : null}

    {/* Трейсбек не переносим: кадры читаются только своей строкой — отдаём блоку
        горизонтальный скролл, иначе он распирал бы всю колонку. */}
    {open && x.traceback ? <pre className="mono" style={{ marginTop: 10, padding: 11, borderRadius: 10,
      background: 'var(--bg)', border: '1px solid var(--line)', fontSize: 11.5, lineHeight: 1.5,
      overflowX: 'auto', whiteSpace: 'pre', maxHeight: 340, overflowY: 'auto' }}>{x.traceback}</pre> : null}

    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
      {x.count > 1 ? <span className="hint">впервые {x.first_at}</span> : null}
      {x.traceback ? <button className="db sm line" style={{ marginLeft: 'auto' }}
        onClick={() => setOpen(!open)}>{open ? 'Скрыть трейсбек' : 'Трейсбек'}</button> : null}
      {!x.seen ? <ABtn kind="sm soft" icon="check"
        style={x.traceback ? null : { marginLeft: 'auto' }} onClick={onSeen}>Прочитано</ABtn> : null}
    </div>
  </div>;
}
