// journal.jsx — «Журнал мастера»: разбор неудавшихся ссылок и журнал печатей.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Empty, Field } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { num, grams, minutes as fmtMin } from '../fmt.jsx';
import { Screen, ABtn, Chips, PickOne, useConfirm } from '../parts.jsx';

const { useState, useContext, useRef, useEffect } = React;

export function JournalScreen() {
  const [tab, setTab] = useState('journal');
  // Разбор ссылок ходит в /api/admin/calc_queue, а та не названа ни в одной карте прав —
  // значит закрыта по умолчанию правом «Настройки». Оператору вкладка открывалась красной
  // плашкой отказа; журнал печатей (право «Принтер») — его работа и остаётся на месте.
  const { can } = useContext(AppCtx);
  const canLinks = can('settings');
  // Оба запроса живут здесь, а не во вкладках: счётчики на чипах макета показываются
  // сразу для обеих вкладок, а не только для открытой.
  // Очередь разбора ссылок разгребается фоновыми воркерами — без автообновления мастер
  // смотрел бы на давно посчитанные позиции как на «в работе».
  const q = useApi('adminCalcQueue', [], [], { auto: 10000, skip: !canLinks });
  const j = useApi('adminJournal', [], [], { auto: 30000 });
  const links = (q.data && (q.data.items || q.data.queue)) || [];
  const stats = (j.data && j.data.stats) || {};
  const shownTab = canLinks ? tab : 'journal';

  return <div className="onecol">
    <Chips value={shownTab} onChange={setTab} style={{ marginBottom: 16 }} items={[
      ...(canLinks ? [{ k: 'links', l: 'Разбор ссылок', ic: 'link', n: links.length }] : []),
      { k: 'journal', l: 'Журнал печатей', ic: 'film', n: stats.ungraded || 0 },
    ]} />
    {shownTab === 'links' ? <LinksTab q={q} items={links} /> : <PrintsTab j={j} stats={stats} />}
  </div>;
}

/* ---------- вкладка «Разбор ссылок» ---------- */

function LinksTab({ q, items }) {
  return <Screen state={q}>{() => items.length
    ? <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'start', overflowY: 'auto' }}>
      {items.map((x) => <LinkCard key={x.id} x={x} onDone={q.reload} />)}
    </div>
    : <Empty ic="link" t="Разбирать нечего" ds="Все ссылки клиентов посчитались автоматически" />}
  </Screen>;
}

function LinkCard({ x, onDone }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [note, setNote] = useState('');

  const resolve = async (action, msg) => {
    await call('adminCalcQueueResolve', x.id, action, note);
    toast(msg, { kind: 'ok' });
    onDone();
  };
  const copy = () => {
    // Ссылку чаще всего нужно вставить в слайсер, а не открыть в браузере — поэтому копирование.
    if (navigator.clipboard) navigator.clipboard.writeText(x.url);
    toast('Ссылка скопирована', { kind: 'ok', ms: 2000 });
  };

  return <div className="card notif" style={{ borderLeftColor: 'var(--amber)' }}>
    <div className="lab">#{x.id} · <b style={{ color: 'var(--text)' }}>{x.user || 'Клиент'}</b>
      <span className="r"><span style={{ color: 'var(--faint)' }}>{x.ago}</span></span>
    </div>
    <div className="mono" style={{ fontSize: 13, color: 'var(--blue)', cursor: 'copy', marginBottom: 10, wordBreak: 'break-all' }}
      onClick={copy}>{x.url} ⧉</div>
    {x.error ? <div className="warn" style={{ marginBottom: 12 }}>
      <DIcon n="bell" size={15} />{x.error}
    </div> : null}
    <Field l="Комментарий клиенту">
      <input className="inp" value={note} onChange={(e) => setNote(e.target.value)}
        placeholder="что ему ответить при закрытии" />
    </Field>
    <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
      <ABtn kind="soft" style={{ flex: 1 }} icon="check"
        onClick={() => resolve('done', 'Разобрано — клиент получил уведомление')}>Разобрано</ABtn>
      <ABtn kind="line" icon="trash" title="Убрать из очереди без уведомления"
        onClick={() => confirm('Убрать из очереди?',
          'Позиция пропадёт, клиент ничего не получит.',
          () => resolve('delete', 'Убрано из очереди'), 'Убрать')} />
    </div>
  </div>;
}

/* ---------- вкладка «Журнал печатей» ---------- */

function PrintsTab({ j, stats }) {
  // sel: 0 — ничего не выбрано, 'new' — ручная запись печати, число — правка/оценка записи.
  const [sel, setSel] = useState(0);
  const items = (j.data && (j.data.items || j.data.journal)) || [];
  const cur = items.find((x) => x.id === sel) || null;
  // В макете форма оценки видна всегда — мастер заходит сюда, чтобы оценить свежую печать,
  // а не чтобы сначала искать её в списке. Подставляем первую неоценённую (иначе просто первую).
  useEffect(() => {
    if (sel || !items.length) return;
    setSel((items.find((x) => x.graded === 0) || items[0]).id);
  }, [items.length]);   // eslint-disable-line
  const byPlastic = stats.by_plastic || [];

  return <Screen state={j}>{() => <div style={{ display: 'flex', flexDirection: 'column', gap: 20, flex: 1, minHeight: 0 }}>
    {stats.total ? <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 20, flex: '0 0 auto' }}>
      <StatCard ic="printer" k="Печатей" v={num(stats.total || 0)} />
      {/* fail_pct считается сервером по граммам, а не по числу печатей: полный брак мелочи
          и брак 10% большой детали — разный ущерб. Поэтому число печатей уносим в подпись. */}
      <StatCard ic="flame" k="Брак" v={(stats.fail_pct || 0) + '%'} amber
        sub={num(stats.fails || 0) + ' печат. с браком'} />
      <StatCard ic="trash" k="В брак" v={grams(stats.waste || 0)} amber sub="потрачено впустую" />
      <StatCard ic="spool" k="Пластика" v={grams(stats.grams || 0)} />
    </div> : null}
    {byPlastic.length ? <Sec card ic="spool" t="Расход и брак по пластикам" style={{ flex: '0 0 auto' }}>
      {byPlastic.map((bp) => <div key={bp.plastic}
        style={{ display: 'flex', alignItems: 'baseline', gap: 12, padding: '5px 0', fontSize: 13 }}>
        <span style={{ color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{bp.plastic}</span>
        <span className="mono" style={{ marginLeft: 'auto', flexShrink: 0, color: 'var(--faint)' }}>
          {num(bp.n)} печ. · <span style={{ color: bp.fail_pct > 0 ? 'var(--amber)' : 'var(--faint)' }}>брак {bp.fail_pct}%</span> · {grams(bp.grams)}
        </span>
      </div>)}
    </Sec> : null}
    {stats.ungraded ? <div className="warn" style={{ flex: '0 0 auto' }}>
      <DIcon n="clock" size={15} />
      {stats.ungraded} печат{stats.ungraded === 1 ? 'ь ждёт' : 'и ждут'} оценки — фото результата
      снимается автоматом на финише.
    </div> : null}

    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 20, flex: 1, minHeight: 0 }}>
      <Sec card ic="pencil" style={{ overflowY: 'auto' }}
        t={sel === 'new' ? 'Ручная запись печати'
          : 'Оценка печати' + (cur ? ' · ' + (cur.label || '#' + cur.id) : '')}>
        {sel === 'new'
          ? <JournalForm key="new" isNew onSaved={() => { setSel(0); j.reload(); }} />
          : cur
            ? <JournalForm key={cur.id} x={cur} onSaved={() => { setSel(0); j.reload(); }} />
            : <PickOne what="печать" />}
      </Sec>

      <Sec card ic="doc" t="Печати · скрин стола + таймлапс" style={{ minHeight: 0 }}
        right={<button className="db sm soft" onClick={() => setSel('new')}>
          <DIcon n="plus" size={13} />Записать печать
        </button>}>
        {items.length ? <div className="jgrid" style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          {items.map((x) => <PrintCard key={x.id} x={x} onGrade={() => setSel(x.id)} onDeleted={j.reload} />)}
        </div> : <Empty ic="doc" t="Журнал пуст"
          ds="Печати попадают сюда сами — либо занесите вручную ту, что шла мимо системы"
          cta="Записать печать" onCta={() => setSel('new')} />}
      </Sec>
    </div>
  </div>}
  </Screen>;
}

function StatCard({ ic, k, v, sub, amber }) {
  const c = amber ? 'var(--amber)' : 'var(--accent)';
  return <div className="card" style={{ flexDirection: 'row', alignItems: 'baseline', gap: 14 }}>
    <DIcon n={ic} size={17} style={{ color: c, alignSelf: 'center', flexShrink: 0 }} />
    <span style={{ minWidth: 0 }}>
      <span className="lab" style={{ margin: 0 }}>{k}</span>
      {sub ? <span style={{ display: 'block', fontSize: 11, color: 'var(--faint)', marginTop: 3 }}>{sub}</span> : null}
    </span>
    <span className="mono" style={{ fontSize: 26, fontWeight: 700, marginLeft: 'auto', color: amber ? 'var(--amber)' : 'var(--text)' }}>{v}</span>
  </div>;
}

/**
 * «С какими настройками отправляли» (OpenTodo 8e8d4873). Слайсер пишет полный слепок настроек
 * в конец gcode, бот снимает его при заливке файла на принтер — здесь он показывается свёрнутым:
 * в списке нужна одна строка («сопло 0.4 · слой 0.2 · 15% · PLA»), подробности — по клику.
 *
 * Печати, залитые в принтер мимо бота (напрямую из слайсера), снимка не имеют — тогда блока
 * нет вовсе, и это честно: выдумывать настройки задним числом нельзя.
 */
function PrintSettings({ s }) {
  const [open, setOpen] = useState(false);
  if (!s || (!(s.rows || []).length && !(s.start || []).length)) return null;

  return <div className="mt" style={{ marginTop: 6 }}>
    <span onClick={() => setOpen(!open)} style={{ cursor: 'pointer', color: 'var(--muted)' }}>
      <DIcon n={open ? 'chevd' : 'chev'} size={12} style={{ verticalAlign: -1 }} />
      {' '}настройки: <span className="mono">{s.summary || 'см. подробности'}</span>
    </span>
    {open ? <div style={{ marginTop: 8, borderLeft: '2px solid var(--line-2)', paddingLeft: 10 }}>
      {(s.rows || []).map((r) => <div key={r.key} className="setrow">
        <span className="sl" style={{ width: 190, flex: '0 0 auto' }}>{r.label}</span>
        <span className="mono" style={{ flex: 1 }}>{r.value}{r.unit ? ' ' + r.unit : ''}</span>
      </div>)}
      {(s.start || []).length ? <>
        <div className="sl" style={{ marginTop: 8, marginBottom: 2 }}>Как отправляли</div>
        {s.start.map((r) => <div key={r.label} className="setrow">
          <span className="sl" style={{ width: 190, flex: '0 0 auto' }}>{r.label}</span>
          <span className="mono" style={{ flex: 1 }}>{r.value}</span>
        </div>)}
      </> : null}
      <div className="hint" style={{ marginTop: 8 }}>
        {s.slicer ? s.slicer + ' · ' : ''}
        {s.layers ? s.layers + ' слоёв · ' : ''}
        файл {s.name}
      </div>
    </div> : null}
  </div>;
}

/** Карточка печати: медиа во всю ширину (.med) + описание (.bd) — разметка макета. */
function PrintCard({ x, onGrade, onDeleted }) {
  const { toast, lightbox } = useContext(AppCtx);
  const confirm = useConfirm();
  const wait = x.graded === 0;                       // старые записи приходят без graded — они оценены
  const fail = Number(x.fail_pct || 0);

  return <div className={'jcard' + (wait ? ' wait' : '')}>
    <div className="med">
      {x.photo
        ? <img src={x.photo} alt="" onClick={() => lightbox(x.photo)}
            onError={(e) => { e.target.style.display = 'none'; }} />
        : <div className="med-ph"><DIcon n="camera" size={26} />нет фото</div>}
      <span className="tag shot" style={{ fontSize: 9.5, padding: '3px 8px' }}>📷 скрин стола · финиш</span>
      <span className="res">{wait
        ? <span className="stb amber">🕒 оценить</span>
        : fail > 0
          ? <span className="stb red">⚠️ брак {fail}%</span>
          : <span className="stb green">✅ удача</span>}</span>
      {x.timelapse_url
        ? <span className="tlb" onClick={() => window.open(x.timelapse_url, '_blank')}>▶ таймлапс</span>
        : <span className="tlb" style={{ opacity: 0.45, cursor: 'default', borderColor: 'var(--line-2)', color: 'var(--faint)' }}>без таймлапса</span>}
    </div>
    <div className="bd">
      <div className="nm">{x.label || 'Без названия'}</div>
      <div className="mt">
        {x.plastic_name || '—'}
        {x.grams ? ' · ' + grams(x.grams) : ''}
        {x.minutes ? ' · ' + fmtMin(x.minutes) : ''}
        {x.request_id ? ' · заказ #' + x.request_id : ''}
      </div>
      {x.fail_reason ? <div className="mt" style={{ color: 'var(--amber)' }}>причина: {x.fail_reason}</div> : null}
      {x.note ? <div className="mt">заметка: {x.note}</div> : null}
      <PrintSettings s={x.settings} />
      <div className="act">
        <span className="mt">{x.ago}</span>
        <span style={{ flex: 1 }} />
        {wait ? <button className="db sm soft" onClick={onGrade}>Оценить</button> : null}
        <button className="ib" style={{ width: 30, height: 30 }} title="Править" onClick={onGrade}>
          <DIcon n="pencil" size={13} />
        </button>
        <button className="ib danger" style={{ width: 30, height: 30 }} title="Удалить"
          onClick={() => confirm('Удалить запись журнала?',
            'Расход пластика по этой печати останется на складе.',
            async () => {
              // ui.jsx::ask зовёт onYes без catch — ошибку ловим здесь, иначе отказ сервера
              // превратится в unhandled rejection и мастер увидит лишь закрывшийся диалог
              try {
                await call('adminJournalDelete', x.id);
                toast('Запись удалена', { kind: 'ok' });
                onDeleted();
              } catch (e) { toast(e.message, { kind: 'warn2' }); }
            })}>
          <DIcon n="trash" size={13} />
        </button>
      </div>
    </div>
  </div>;
}

/**
 * Выбор катушки для ручной записи печати. Без него списание идёт «по пластику» — общим складом,
 * и остатки конкретных катушек расходятся с реальностью. Архивные не предлагаем: с них не печатают.
 * Отдельный компонент, чтобы запрос катушек уходил только там, где выбор действительно нужен.
 */
function SpoolPick({ plasticId, value, onChange }) {
  const sp = useApi('adminSpools', [plasticId || 0], [plasticId]);
  if (!plasticId) return null;
  const spools = ((sp.data && sp.data.spools) || []).filter((s) => s.status !== 'archived');
  if (!spools.length) return null;
  return <Field l="Катушка">
    <select className="sel" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Катушка — по пластику (без точного учёта)</option>
      {spools.map((s) => <option key={s.id} value={s.id}>
        {(s.color || 'без цвета') + ' · ' + Math.round(s.live_g != null ? s.live_g : s.remaining_g) + ' г'
          + (s.status === 'empty' ? ' · пустая' : '')
          // катушку принтера (CC2 Control) списывает он сам — запись журнала её не тронет
          + (s.cc2_id ? ' · списывает принтер' : '')}
      </option>)}
    </select>
  </Field>;
}

/**
 * Форма записи/оценки печати: сервер требует непустой label, брак — ползунком 0..100%.
 * isNew — ручная запись печати, которая шла мимо системы (другой принтер, допечатка, чужой файл):
 * такие в журнал сами не попадают. Отличий от правки два, и оба — ограничения сервера:
 * journal_add не читает request_id, а journal_update не читает spool_id.
 */
function JournalForm({ x = {}, isNew, onSaved }) {
  const { toast, lightbox } = useContext(AppCtx);
  const confirm = useConfirm();
  const plastics = useApi('adminPlastics', [], []);
  const fileRef = useRef(null);
  const [photo, setPhoto] = useState(x.photo || '');
  const [tl, setTl] = useState(x.timelapse_url || '');

  // Удаление медиа печати (1faab6cb): скрин стола и таймлапс убираются и из карточки заказа
  // у клиента; таймлапс сервер помечает удалённым, чтобы свип не докачал его заново.
  const delMedia = (kind) => confirm(kind === 'photo' ? 'Удалить фото печати?' : 'Удалить таймлапс?',
    kind === 'photo'
      ? 'Скрин стола пропадёт и из фоток результата привязанного заказа.'
      : 'Файл видео будет удалён и заново с принтера не скачается.',
    async () => {
      try {
        await call('adminJournalMediaDelete', x.id, kind);
        if (kind === 'photo') setPhoto(''); else setTl('');
        toast(kind === 'photo' ? 'Фото удалено' : 'Таймлапс удалён', { kind: 'ok' });
      } catch (e) { toast(e.message, { kind: 'warn2' }); }
    });
  const [f, setF] = useState({
    label: x.label || '', request_id: x.request_id || '', plastic_id: x.plastic_id || '',
    grams: x.grams || '', minutes: x.minutes || '',
    fail_pct: Number(x.fail_pct || 0), fail_reason: x.fail_reason || '',
    note: x.note || '', spool_id: '',
  });
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  // Фото результата печати → /api/admin/photo kind=journal. Нужен уже существующий id,
  // поэтому у ручной записи фото добавляется вторым шагом, после сохранения.
  const upload = async (file) => {
    if (!file) return;
    if (file.size > 12 * 1024 * 1024) { toast('Фото больше 12 МБ', { kind: 'warn2' }); return; }
    const rd = new FileReader();
    rd.onload = async () => {
      try {
        const r = await call('adminPhoto', 'journal', x.id, rd.result, false);
        setPhoto(r.url || rd.result);
        toast('Фото результата сохранено', { kind: 'ok' });
      } catch (e) {
        toast(e.message, { kind: 'warn2' });
      }
    };
    rd.readAsDataURL(file);
  };

  return <div className="fgap">
    {isNew
      ? <Field l="Что печатали">
        <input className="inp" value={f.label} placeholder="модель или деталь"
          onChange={(e) => set('label', e.target.value)} />
      </Field>
      : <div className="frow">
        <Field l="Что печатали">
          <input className="inp" value={f.label} onChange={(e) => set('label', e.target.value)} />
        </Field>
        <Field l="Заказ №">
          <input className="inp mono" value={f.request_id} placeholder="вне заказа"
            onChange={(e) => set('request_id', e.target.value.replace(/[^0-9]/g, ''))} />
        </Field>
      </div>}
    <div className="frow">
      <Field l="Пластик">
        <select className="sel" value={f.plastic_id}
          onChange={(e) => setF((s) => ({ ...s, plastic_id: e.target.value, spool_id: '' }))}>
          <option value="">{x.plastic_name || 'не указан'}</option>
          {((plastics.data && plastics.data.plastics) || []).map((p) =>
            <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <Field l="Граммы">
        <input className="inp mono" value={f.grams} onChange={(e) => set('grams', e.target.value)} />
      </Field>
    </div>
    {/* Катушку принимает только journal_add: при правке записи сервер поле spool_id не читает,
        поэтому там его и не показываем, чтобы мастер не думал, что списание переехало. */}
    {isNew ? <SpoolPick plasticId={f.plastic_id} value={f.spool_id}
      onChange={(v) => set('spool_id', v)} /> : null}
    <Field l="Минуты">
      <input className="inp mono" value={f.minutes} onChange={(e) => set('minutes', e.target.value)} />
    </Field>
    <div>
      <label className="f-lab">Результат: {f.fail_pct === 0 ? '✅ удача' : `⚠️ брак ${f.fail_pct}%`}</label>
      <input type="range" className="rng" min="0" max="100" value={f.fail_pct}
        onChange={(e) => set('fail_pct', Number(e.target.value))}
        style={{ accentColor: f.fail_pct > 0 ? 'var(--amber)' : 'var(--price)' }} />
    </div>
    {f.fail_pct > 0 ? <Field l="Причина брака">
      <input className="inp" value={f.fail_reason} placeholder="отлип, засор, слои…"
        onChange={(e) => set('fail_reason', e.target.value)} />
    </Field> : null}
    <Field l="Заметка">
      <input className="inp" value={f.note} placeholder="что запомнить об этой печати"
        onChange={(e) => set('note', e.target.value)} />
    </Field>
    {isNew ? <div className="warn">
      <DIcon n="camera" size={15} />
      Фото результата и заказ добавляются после сохранения — откройте запись на правку.
    </div> : <div>
      <label className="f-lab">Медиа печати · скрин стола снимается автоматом на финише</label>
      <div style={{ display: 'flex', gap: 10, alignItems: 'stretch' }}>
        <div style={{ position: 'relative', width: 150, aspectRatio: '16/10', borderRadius: 10, overflow: 'hidden', border: '1px solid var(--line-2)', flexShrink: 0, background: 'var(--surface-2)' }}>
          {photo ? <img src={photo} alt="" onClick={() => lightbox(photo)}
            style={{ width: '100%', height: '100%', objectFit: 'cover', cursor: 'zoom-in' }} /> : null}
          <span className="tag" style={{ position: 'absolute', left: 6, bottom: 5, fontSize: 9.5, padding: '2px 7px' }}>📷 скрин стола</span>
        </div>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8, justifyContent: 'center' }}>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="db line sm" style={{ flex: 1 }} onClick={() => fileRef.current.click()}>
              <DIcon n="camera" size={14} />{photo ? 'Заменить фото' : 'Фото результата'}
            </button>
            {photo ? <button className="ib danger" style={{ width: 32, flex: '0 0 auto' }}
              title="Удалить фото" onClick={() => delMedia('photo')}>
              <DIcon n="trash" size={13} />
            </button> : null}
          </div>
          <input type="file" accept="image/*" ref={fileRef} style={{ display: 'none' }}
            onChange={(e) => { const g = e.target.files && e.target.files[0]; e.target.value = ''; upload(g); }} />
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="db line sm" style={{ flex: 1 }} disabled={!tl}
              onClick={() => window.open(tl, '_blank')}>
              <DIcon n="film" size={14} />{tl ? 'Таймлапс' : 'Таймлапса нет'}
            </button>
            {tl ? <button className="ib danger" style={{ width: 32, flex: '0 0 auto' }}
              title="Удалить таймлапс" onClick={() => delMedia('timelapse')}>
              <DIcon n="trash" size={13} />
            </button> : null}
          </div>
        </div>
      </div>
    </div>}
    {/* Оценка печати без фото результата бесполезна для разбора брака — требуем его так же,
        как мобильная админка (только для печатей, которые ещё ни разу не оценивали). */}
    {!isNew && x.graded === 0 && !photo ? <div className="warn">
      <DIcon n="camera" size={15} />Добавьте фото результата — без него оценку не сохранить.
    </div> : null}
    <ABtn onClick={async () => {
      if (!f.label.trim()) { toast('Заполните «Что печатали»', { kind: 'warn2' }); return; }
      if (!isNew && x.graded === 0 && !photo) {
        toast('Добавьте фото результата — оно обязательно для оценки', { kind: 'warn2' });
        return;
      }
      // note шлём всегда: сервер пишет body.get('note') без проверки на отсутствие,
      // и запрос без этого поля молча затёр бы уже сохранённую заметку.
      const base = {
        label: f.label.trim(), plastic_id: f.plastic_id || 0,
        plastic_name: f.plastic_id ? '' : (x.plastic_name || ''),
        grams: Number(f.grams) || 0, minutes: Number(f.minutes) || 0,
        fail_pct: f.fail_pct, fail_reason: f.fail_reason, note: f.note,
      };
      try {
        if (isNew) {
          await call('adminJournalAdd', { ...base, spool_id: Number(f.spool_id) || 0 });
          toast(f.spool_id ? 'Записано: списано с выбранной катушки'
            : 'Записано: пластик списан по складу', { kind: 'ok', ms: 6000 });
        } else {
          await call('adminJournalUpdate', x.id, { ...base, request_id: Number(f.request_id) || 0 });
          toast('Печать оценена: склад списан по катушке', { kind: 'ok', ms: 6000 });
        }
        onSaved();
      } catch (e) {
        toast(e.message, { kind: 'warn2' });
      }
    }}>{isNew ? 'Записать печать' : 'Сохранить оценку'}</ABtn>
  </div>;
}
