// notifs.jsx — «Уведомления»: инбокс мастера. Это НЕ поток системных событий, а черновики
// release notes и заявки — так устроена таблица admin_notifications.

import { DIcon } from '../icons.jsx';
import { AppCtx, Empty, Drawer, Field, StBadge } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { Screen, Chips, ABtn, useConfirm } from '../parts.jsx';

const { useState, useContext, useRef, useCallback, useEffect } = React;

// Ширина полосы карточек из макета: на 3440 без ограничения две колонки расползаются в километр текста.
const WRAP = { maxWidth: 1900, width: '100%', margin: '0 auto' };
// Сетка карточек — в макете ровно две колонки; minmax(0,…) не даёт длинному тексту распирать колонку.
const GRID = {
  display: 'grid', gridTemplateColumns: 'repeat(2,minmax(0,1fr))',
  gap: 20, alignItems: 'start', overflowY: 'auto', minHeight: 0,
};
// Высота карточки черновика release notes ≈ двум обычным уведомлениям (замер: обычная ~200px).
// Черновик правится прямо в карточке, и на одной высоте с обычным полю текста было тесно —
// по просьбе владельца растягиваем карточку, а лишнюю высоту отдаём textarea (flex ниже).
const DRAFT_MIN_H = 420;
// Слот картинки черновика: в макете он задан инлайном, своего класса под него нет.
const SLOT = {
  width: 150, aspectRatio: '1', border: '1px dashed var(--line-2)', borderRadius: 11,
  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
  gap: 6, color: 'var(--faint)', fontSize: 11.5, flexShrink: 0, cursor: 'pointer', overflow: 'hidden',
};

// Заголовок и иконка карточки — по типу уведомления; сам x.title идёт отдельной крупной строкой.
const KIND = {
  release_note: { ic: 'bell', t: 'Release notes · черновик от Клода' },
  print_classify: { ic: 'printer', t: 'Печать завершена' },
};

// Заготовки промтов генерации картинки (image_regen): мастер выбирает из списка и правит,
// а не сочиняет с нуля. Стиль «маскот-робот» генератор добавляет сам (цепочка аватарок).
const GEN_PROMPTS = [
  'робот-маскот радостно поднимает готовую напечатанную деталь возле 3D-принтера',
  'семья маленьких роботов смотрит, как 3D-принтер печатает деталь',
  'робот-маскот с гаечным ключом настраивает большой 3D-принтер',
  'робот-маскот везёт тележку с разноцветными напечатанными деталями',
  'робот-маскот в очках изучает чертёж рядом с 3D-принтером и готовыми деталями'];

/** Диалог генерации картинки release note: редактируемый комбобокс промта + «Сгенерировать». */
function GenImageModal({ x, onClose, onDone, toast }) {
  const [prompt, setPrompt] = useState((x.payload && x.payload.gen_prompt) || GEN_PROMPTS[0]);
  const [open, setOpen] = useState(false);      // раскрыт ли список заготовок
  const [busy, setBusy] = useState(false);
  const gen = async () => {
    const p = prompt.trim();
    if (!p) { toast('Опишите, что нарисовать', { kind: 'warn2' }); return; }
    setBusy(true);
    try {
      const r = await call('adminNotificationAction', x.id, 'image_regen', { prompt: p });
      // честно про движок: процедурная заглушка ≠ ИИ-картинка, мастеру стоит перегенерить
      if (r.engine === 'procedural') {
        toast('Видеокарта занята — вышел простой рисунок, попробуйте ещё раз', { kind: 'warn2' });
      } else if (r.engine === 'sd-turbo') {
        toast('Сгенерировано запасной моделью (быстрой)', { kind: 'ok' });
      } else {
        toast('Картинка сгенерирована', { kind: 'ok' });
      }
      // свежий url (с новым ?v=) — карточке: её локальный img иначе не узнает о новой
      // картинке (useState берёт x.image_url только при монтировании) — OpenTodo db51b5ba
      onDone(r.image_url || '');
    } catch (e) {
      toast(e.message, { kind: 'warn2' });
      setBusy(false);
    }
  };
  // .modal в макете absolute (рендерится в корне приложения); здесь диалог живёт внутри
  // карточки, поэтому fixed — иначе оверлей накрыл бы только карточку.
  return <div className="modal" style={{ position: 'fixed' }} onClick={() => { if (!busy) onClose(); }}>
    <div className="mbox" onClick={(e) => e.stopPropagation()}>
      <div className="mt">✨ Сгенерировать картинку</div>
      <div className="mx">Опишите сюжет по-русски — стиль маскота-робота добавится сам.
        Можно выбрать заготовку кнопкой ▾ и поправить её.</div>
      <div style={{ display: 'flex', gap: 8, marginTop: 4, alignItems: 'flex-start' }}>
        <textarea className="inp" rows={3} maxLength={300} value={prompt} disabled={busy}
          style={{ flex: 1, resize: 'vertical', lineHeight: 1.4 }}
          onChange={(e) => setPrompt(e.target.value)} />
        <button className="ib" title="Заготовки промтов" disabled={busy}
          onClick={() => setOpen(!open)}>
          <DIcon n="chevd" size={15} style={open ? { transform: 'rotate(180deg)' } : null} /></button>
      </div>
      {open ? <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
        {GEN_PROMPTS.map((p, i) => <button key={i} className="db sm line"
          style={{ justifyContent: 'flex-start', textAlign: 'left', whiteSpace: 'normal',
            lineHeight: 1.35, height: 'auto', padding: '8px 12px',
            ...(p === prompt ? { borderColor: 'var(--accent)' } : null) }}
          onClick={() => { setPrompt(p); setOpen(false); }}>{p}</button>)}
      </div> : null}
      {busy ? <div className="warn" style={{ marginTop: 10 }}>
        <DIcon n="bell" size={15} />Горячая модель — ~20 с, холодная — до минуты. Если видеокарта
        занята, выйдет простой рисунок — можно перегенерить ещё раз.
      </div> : null}
      <div className="mrow">
        <button className="db line" disabled={busy} onClick={onClose}>Отмена</button>
        <button className="db good" disabled={busy} onClick={gen}>
          {busy ? 'Генерирую…' : 'Сгенерировать'}
        </button>
      </div>
    </div>
  </div>;
}

export function NotifsScreen() {
  const { toast, params, nav, reloadHub } = useContext(AppCtx);
  const [all, setAll] = useState(false);
  // Уведомления приходят сами (бот дописывает черновики и завершённые печати), поэтому
  // список тихо обновляется, не дожидаясь, пока мастер переоткроет экран.
  const n = useApi('adminNotifications', [all], [all], { auto: 15000 });
  const items = (n.data && n.data.items) || [];

  // После любой кнопки перечитываем и список, и сводку: счётчик уведомлений в сайдбаре
  // живёт в adminHub, и без этого он ещё 20 секунд показывал бы обработанную карточку.
  const after = useCallback(() => { n.reload(); reloadHub(); }, [n, reloadHub]);

  return <div className="onecol" style={WRAP}>
    <Chips items={[{ k: 'new', l: 'Новые', n: items.filter((x) => x.status === 'new').length },
      { k: 'all', l: 'Все' }]}
      value={all ? 'all' : 'new'} onChange={(k) => setAll(k === 'all')} />

    {/* Закрытие шторки «Чья печать?» тоже обновляет список: привязка печати к заказу
        снимает карточку с «Новых», а закрывается шторка как раз после привязки. */}
    {params.sel ? <ClassifyDrawer jid={params.sel}
      onClose={() => { nav('notifs', {}); after(); }} /> : null}

    <Screen state={n}>{() => items.length ? <div style={GRID}>
      {items.map((x) => <NotifCard key={x.id} x={x} onDone={after} toast={toast} />)}
    </div> : <Empty ic="bell" t="Уведомлений нет" ds="Черновики и заявки появятся здесь" />}
    </Screen>
  </div>;
}

// Подписи статусов для режима «Все»: обработанную карточку иначе не отличить от живой.
const ST_LABEL = { rejected: 'обработано', published: 'опубликовано', done: 'выполнено' };

function NotifCard({ x, onDone, toast }) {
  const { nav } = useContext(AppCtx);
  const [title, setTitle] = useState(x.title || '');
  const [body, setBody] = useState(x.body || '');
  const [img, setImg] = useState(x.image_url || '');
  const [genOpen, setGenOpen] = useState(false);   // диалог генерации картинки по промту
  // Список перечитался (после генерации/сохранения) → синхронизируем слот картинки: карточка
  // не размонтируется (key=x.id постоянен), и без этого новая картинка не появлялась до F5
  // (OpenTodo db51b5ba). ?v= в url меняется вместе с файлом, поэтому сравнение честное.
  useEffect(() => { setImg(x.image_url || ''); }, [x.image_url]);
  const fileRef = useRef();
  const confirm = useConfirm();
  const isDraft = x.kind === 'release_note';
  // Редактировать и публиковать можно только НОВОЕ: у опубликованной записи сервер всё
  // равно откажет (try_mark_published), а форма с живой кнопкой «Опубликовать» вводит в
  // заблуждение. Обработанные карточки показываем только для чтения — и с удалением.
  const isNew = x.status === 'new';
  const k = KIND[x.kind] || { ic: 'bell', t: 'Уведомление' };

  // Общая обёртка действий: call() бросает на {ok:false}, и без catch мастер увидел бы
  // лишь «кнопка ничего не делает» — ровно та беда, ради которой этот экран переписан.
  const act = async (action, extra, msg) => {
    try {
      await call('adminNotificationAction', x.id, action, extra);
      toast(msg, { kind: 'ok' });
      onDone();
    } catch (e) {
      toast(e.message, { kind: 'warn2' });
    }
  };

  // Удаление записи целиком (сервер заодно подчищает файл картинки) — из любой карточки.
  const del = () => confirm(
    'Удалить уведомление?',
    isDraft ? 'Запись release notes и её картинка будут удалены безвозвратно. На уже отправленные клиентам сообщения это не влияет.'
      : 'Запись будет удалена безвозвратно.',
    () => act('delete', null, 'Удалено'),
  );

  // Черновик в макете помечен фиолетовой полосой — он единственный требует решения мастера.
  const st = {
    ...(isDraft ? { borderLeftColor: 'var(--violet)' } : null),
    ...(isNew ? null : { opacity: .6 }),
    // Редактируемый черновик релиза — высотой примерно в два обычных уведомления (см. DRAFT_MIN_H).
    ...(isDraft && isNew ? { minHeight: DRAFT_MIN_H } : null),
  };

  return <div className="card notif" style={st}>
    <div className="lab">
      <DIcon n={k.ic} size={13} />{k.t}
      <span className="r"><span style={{ color: 'var(--faint)' }}>
        {ST_LABEL[x.status] ? `${ST_LABEL[x.status]} · ` : ''}{x.ago}
      </span></span>
    </div>

    {isDraft && isNew ? <>
      {/* flex:1 — верхний ряд (картинка + поля) забирает добавленную карточке высоту, чтобы
          она ушла в поле текста, а не в пустоту под кнопками. */}
      <div style={{ display: 'flex', gap: 16, flex: 1, minHeight: 0 }}>
        <div style={{ position: 'relative', flexShrink: 0 }}>
          <div style={SLOT} onClick={() => fileRef.current.click()}>
            {img ? <img src={img} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
              : <><DIcon n="camera" size={20} />добавить картинку</>}
          </div>
          {/* Снять картинку, не трогая сам черновик: с ней Telegram режет текст до ~1000
              символов, и мастеру нужен способ отыграть назад, не удаляя запись. */}
          {img ? <button className="ib danger" title="Удалить картинку"
            style={{ position: 'absolute', top: 6, right: 6 }}
            onClick={(e) => {
              e.stopPropagation();   // иначе клик всплывёт в слот и откроет выбор файла
              confirm('Удалить картинку?', 'Черновик останется, рассылка уйдёт без картинки.',
                async () => {
                  try {
                    await call('adminNotificationAction', x.id, 'image_del');
                    setImg('');   // локально, чтобы слот сразу стал пустым
                    toast('Картинка удалена', { kind: 'ok' });
                    onDone();
                  } catch (err) { toast(err.message, { kind: 'warn2' }); }
                });
            }}><DIcon n="trash" /></button> : null}
          {/* Генерация по промту (image_regen) — под слотом, той же ширины: загрузка файла
              остаётся кликом по слоту, а «нарисуй заново» получает свою явную кнопку. */}
          <button className="db sm line" style={{ width: 150, marginTop: 8 }}
            onClick={() => setGenOpen(true)}>✨ Сгенерировать</button>
        </div>
        <div className="fgap" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <Field l="Заголовок для клиентов">
            <input className="inp" value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          {/* Поле текста растягивается на всю оставшуюся высоту карточки (flex:1), поэтому
              rows тут не задаём — высоту диктует карточка (DRAFT_MIN_H), а не число строк. */}
          <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, minHeight: 0 }}>
            <label className="f-lab">Текст</label>
            <textarea className="inp" style={{ flex: 1, minHeight: 96, resize: 'none' }}
              value={body} onChange={(e) => setBody(e.target.value)} />
          </div>
        </div>
      </div>
      <input type="file" accept="image/*" ref={fileRef} style={{ display: 'none' }}
        onChange={(e) => {
          const f = e.target.files && e.target.files[0];
          if (!f) return;
          if (f.size > 12 * 1024 * 1024) { toast('Картинка больше 12 МБ', { kind: 'warn2' }); return; }
          e.target.value = '';   // иначе повторный выбор того же файла не даст onChange
          const rd = new FileReader();
          rd.onload = async () => {
            try {
              await call('adminPhoto', 'notif', x.id, rd.result, false);
              setImg(rd.result);
              toast('Картинка сохранена', { kind: 'ok' });
              onDone();
            } catch (err) { toast(err.message, { kind: 'warn2' }); }
          };
          rd.readAsDataURL(f);
        }} />
      <div className="warn" style={{ marginTop: 14 }}>
        <DIcon n="bell" size={15} />С картинкой Telegram обрежет текст до ~1000 символов.
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
        {/* Публикация уходит ВСЕМ клиентам — отменить нельзя, поэтому спрашиваем */}
        <button className="db" onClick={() => confirm(
          'Опубликовать рассылку?',
          'Сообщение уйдёт всем, кто не отписан от рассылок. Отменить отправку нельзя.',
          async () => {
            try {
              // publish на сервере берёт текст ИЗ БАЗЫ, поля title/body в нём не читаются,
              // поэтому несохранённые правки сначала пишем — иначе клиентам уйдёт старая
              // редакция. Публикуем только после УСПЕШНОГО save.
              await call('adminNotificationAction', x.id, 'save', { title, body });
              const r = await call('adminNotificationAction', x.id, 'publish');
              toast(`Отправлено: ${r.sent || 0}, не дошло: ${r.failed || 0}`, { kind: 'ok' });
              onDone();
            } catch (e) { toast(e.message, { kind: 'warn2' }); }
          }, 'Опубликовать',
        )}>Опубликовать</button>
        <ABtn kind="soft" onClick={() => act('save', { title, body }, 'Черновик сохранён')}>Сохранить</ABtn>
        {/* «Отклонить» прячет черновик (status=rejected), запись остаётся в «Все» —
            безвозвратное удаление это соседняя кнопка «Удалить». */}
        <ABtn kind="soft" style={{ marginLeft: 'auto' }}
          onClick={() => confirm('Отклонить черновик?', 'Черновик уйдёт из «Новых», но останется в списке «Все».',
            () => act('reject', null, 'Черновик отклонён'), 'Отклонить')}>Отклонить</ABtn>
        <button className="ib danger" title="Удалить безвозвратно" onClick={del}><DIcon n="trash" /></button>
      </div>
      {genOpen ? <GenImageModal x={x} toast={toast}
        onClose={() => setGenOpen(false)}
        onDone={(url) => { setGenOpen(false); if (url) setImg(url); onDone(); }} /> : null}
    </> : <>
      <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 6 }}>{x.title}</div>
      <div style={{ color: 'var(--muted)', fontSize: 14, whiteSpace: 'pre-wrap' }}>{x.body}</div>
      {isDraft && img ? <img src={img} alt=""
        style={{ maxWidth: 320, width: '100%', borderRadius: 11, marginTop: 12, display: 'block' }} /> : null}
      <div style={{ display: 'flex', gap: 10, marginTop: 16 }}>
        {/* Завершённую печать надо не просто «увидеть», а привязать к заказу — открываем
            разбор «Чья печать?» по jid из payload (та же шторка, что по deep-link из бота).
            Без этой кнопки карточка «Печать завершена» действий не давала — только пряталась. */}
        {isNew && x.kind === 'print_classify' && x.payload && x.payload.jid
          ? <ABtn kind="soft" icon="printer"
              onClick={() => nav('notifs', { sel: x.payload.jid })}>Выбрать заказ</ABtn>
          : null}
        {/* «Обработано» = action reject: отдельного done на сервере нет, разбор
            _api_admin_notification_action знает reject/save/delete/image_del/image_regen/publish.
            reject ставит status=rejected и убирает карточку из «Новых» — это и есть
            «я это увидел и разобрался». */}
        {isNew ? <ABtn kind="soft"
          onClick={() => act('reject', null, 'Отмечено обработанным')}>Обработано</ABtn> : null}
        <button className="ib danger" style={{ marginLeft: 'auto' }} title="Удалить безвозвратно"
          onClick={del}><DIcon n="trash" /></button>
      </div>
    </>}
  </div>;
}

/** «Чья печать?» — выбор заказа для завершённой печати (открывается deep-link'ом из бота). */
function ClassifyDrawer({ jid, onClose }) {
  const { toast } = useContext(AppCtx);
  const c = useApi('adminPrintClassify', [jid], [jid]);
  const d = c.data || {};
  const pr = d.print || {};
  // Клик по строке сразу привязывает печать — блокируем повтор, пока запрос в полёте.
  const [busy, setBusy] = useState(false);

  // Параметры печати в макете живут в подзаголовке шторки, отдельного блока под них нет.
  const sub = [pr.label, pr.grams ? `${pr.grams} г` : '', pr.minutes ? `${pr.minutes} мин` : '',
    pr.plastic, pr.request_id ? `шла с привязкой к #${pr.request_id}` : '']
    .filter(Boolean).join(' · ');

  const pick = async (action, id, msg) => {
    if (busy) return;
    setBusy(true);
    try {
      await call('adminPrintClassifyPick', jid, action, id);
      toast(msg, { kind: 'ok' });
      onClose();
    } catch (e) {
      toast(e.message, { kind: 'warn2' });   // без этого отказ сервера выглядел как «клик не сработал»
    } finally { setBusy(false); }
  };

  return <Drawer title="Чья печать?" sub={sub} onClose={onClose}
    footer={<ABtn kind="soft full" icon="box" disabled={busy}
      onClick={() => pick('self', 0, 'Отмечено как печать для себя')}>
      Для себя (самопечать)
    </ABtn>}>
    <div className="f-lab" style={{ marginBottom: 10 }}>Или выберите заказ</div>
    <Screen state={c}>{() => (d.orders || []).length
      ? (d.orders || []).map((o) => <div key={o.id} className="qrow"
        style={busy ? { opacity: .5, pointerEvents: 'none' } : null}
        onClick={() => pick('order', o.id, `Печать привязана к заказу #${o.id}`)}>
        {o.preview ? <img src={o.preview} alt="" />
          : <div style={{ width: 44, height: 44, borderRadius: 9, background: 'var(--bg)', flexShrink: 0 }} />}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="nm">#{o.id} · {o.label}</div>
          <div className="ds">{o.user}</div>
        </div>
        <StBadge st={o.status} />
      </div>)
      : <Empty ic="box" t="Активных заказов нет" ds="Печать можно записать как самопечать" />}
    </Screen>
  </Drawer>;
}
