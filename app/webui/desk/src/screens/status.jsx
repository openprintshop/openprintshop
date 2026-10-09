// status.jsx — «Статус»: показатели, самодиагностика, ИИ/GPU, токен, экран, камера.
// Раскладка макета: сводная плашка сверху + три колонки .col3/.colw по карточкам .card.

import { DIcon } from '../icons.jsx';
import { AppCtx, Sec, Bar } from '../ui.jsx';
import { call, useApi } from '../api.jsx';
import { num, minutes as fmtMin, plural } from '../fmt.jsx';
import { Screen, ABtn, Toggle, useConfirm } from '../parts.jsx';
import { hasFeature } from '../shell.jsx';

const { useState, useContext, useEffect } = React;

// Человеческие подписи вместо сырых ключей ответа.
const LABEL = {
  uptime: 'Бот работает', disk_gb: 'Свободно на диске', queue_count: 'В очереди печати',
  queue_minutes: 'Времени в очереди', slices_running: 'Слайсов сейчас', slices_cap: 'Лимит слайсов',
  printers: 'Принтеров', plastics: 'Материалов', calc_queue: 'Ссылок на разбор',
  preview_gpu: 'Рендер превью на GPU', data_dir: 'Каталог данных',
  // C3: чем и по какому адресу студия видна клиентам снаружи — вход клиентов в мини-апку.
  tunnel_mode: 'Вход клиентов снаружи', public_url: 'Адрес мини-приложения',
  tunnel_token_set: 'Токен туннеля задан',
  tunnel_running: 'Процесс cloudflared', tunnel_error: 'Сбой входа снаружи',
};

// Режим входа наружу человеческими словами (сырые quick/named/none владельцу ничего не говорят).
const TUNNEL_RU = {
  quick: 'временный Cloudflare (адрес меняется)',
  named: 'постоянный Cloudflare (домен студии)',
  none: 'свой адрес владельца',
};

// В макете у каждой ячейки .kpi2 своя иконка. Карта по ключу, а не по порядку: состав
// ответа сервера меняется (gpu/ai могут не прийти), позиционный список разъехался бы.
const KPI_IC = {
  uptime: 'clock', disk_gb: 'folder', queue_count: 'queue', queue_minutes: 'clock',
  slices_running: 'layers', slices_cap: 'layers', printers: 'printer', plastics: 'spool',
  calc_queue: 'link', preview_gpu: 'bolt', data_dir: 'folder',
  tunnel_mode: 'link', public_url: 'link', tunnel_token_set: 'shield',
  tunnel_running: 'bolt', tunnel_error: 'bell',
};

function fmtVal(k, v) {
  if (k === 'queue_minutes') return fmtMin(v);
  if (k === 'disk_gb') return `${Number(v).toFixed(1)} ГБ`;
  if (typeof v === 'boolean') return v ? 'да' : 'нет';
  if (k === 'data_dir') return String(v);   // путь: не число и не да/нет
  if (k === 'tunnel_mode') return TUNNEL_RU[v] || String(v);
  if (k === 'public_url') return String(v || 'не задан');
  // Для cloudflared «да/нет» ничего не объясняет: в режимах none и «свой адрес в .env»
  // процесса нет и быть не должно, поэтому пишем словами.
  if (k === 'tunnel_running') return v ? 'работает' : 'не запущен';
  if (k === 'tunnel_error') return String(v);
  if (typeof v === 'number') return num(Math.round(v * 100) / 100);
  return String(v);
}

// selfcheck отдаёт список строк, но исторически встречались и объекты — не падаем.
const issueText = (i) => (typeof i === 'string' ? i : (i && (i.text || i.name)) || '');

const gb = (mb) => (Number(mb || 0) / 1024).toFixed(1);

export function StatusScreen() {
  // Экран здоровья системы без автообновления показывал бы «всё хорошо» уже после аварии.
  const s = useApi('adminStatus', [], [], { auto: 20000 });
  const { can } = useContext(AppCtx);
  const canSettings = can('settings');

  return <div className="onecol" style={{ gap: 20, maxWidth: 2600, width: '100%', margin: '0 auto' }}>
    {/* весь экран под одним Screen: сводка и тумблер TURZX берут своё состояние из adminStatus,
        а без этих данных остальные карточки всё равно нечем наполнить */}
    <Screen state={s}>{(d) => {
      const st = d.status || {};
      const issues = (st.issues || []).map(issueText).filter(Boolean);
      const simple = Object.entries(st).filter(([k, v]) =>
        v !== null && typeof v !== 'object' && !Array.isArray(v)
        // пустой tunnel_error = сбоев нет: пустая ячейка «Сбой входа снаружи» только пугает
        && !(k === 'tunnel_error' && !v));
      return <>
        {issues.length ? <div className="warn" style={{ flex: '0 0 auto', fontSize: 14 }}>
          <DIcon n="bell" size={16} style={{ flexShrink: 0 }} />
          Замечаний: {issues.length} — {issues[0]}
        </div> : null}

        <div className="col3" style={{ flex: 1 }}>
          <div className="colw">
            {/* Обновление руками: цифры статуса живые (диск, очередь, VRAM), а сам экран
                не поллится — без кнопки мастеру пришлось бы перезагружать окно. */}
            <Sec card ic="shield" t="Показатели" style={{ flex: '0 0 auto' }}
              right={<button className="ib" title="Обновить" onClick={() => s.reload()}>
                <DIcon n="refresh" size={16} />
              </button>}>
              <div className="kpi2">
                {simple.map(([k, v]) => <div key={k} className="cell">
                  <div className="k" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <DIcon n={KPI_IC[k] || 'grid'} size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
                    {LABEL[k] || k}
                  </div>
                  <div className="v" style={{ fontSize: 19 }}>{fmtVal(k, v)}</div>
                </div>)}
              </div>
            </Sec>

            <Sec card ic="pulse" t="Самодиагностика" style={{ flex: '0 0 auto' }}>
              {issues.length
                ? <div className="fgap" style={{ gap: 10 }}>
                    {issues.map((t, i) => <div key={i} className="warn">
                      <DIcon n="bell" size={15} style={{ flexShrink: 0 }} />{t}
                    </div>)}
                  </div>
                : <div className="okay">Критичных проблем не обнаружено</div>}
            </Sec>

            {/* Обновления кода, ИИ и аккаунты MakerWorld — всё это ручки под «Настройками».
                Показатели и самодиагностика оператору открыты и остаются, а эти четыре
                карточки у него были набором кнопок, падающих по нажатию. */}
            {canSettings ? <UpdateCard version={st.version} update={st.update} /> : null}
            {canSettings ? <LicenseCard lic={st.license} /> : null}
            {canSettings ? <TuningCard t={st.tuning} /> : null}
          </div>

          {canSettings ? <div className="colw">
            <AiCard ai={st.ai} gpu={st.gpu} onChanged={() => s.reload()} />
            <AiTestCard ai={st.ai} />
            {hasFeature('makerworld')
              ? <TokenCard tok={st.mw_token} accounts={st.mw_accounts} onChanged={() => s.reload()} />
              : null}
          </div> : null}

          <div className="colw">
            <ScreenPowerCard initial={st.turzx && st.turzx.enabled} />
            <CameraCard />
          </div>
        </div>
      </>;
    }}</Screen>
  </div>;
}

/** Обновления кода (OpenTodo 644367fb): версия/канал, ручная проверка против origin,
 * применение (ff-only pull + pip + перезапуск сторожем) и режим off/notify/auto. */
function UpdateCard({ version: v = {}, update: u0 }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [upd, setUpd] = useState(null);       // результат ручной проверки поверх фоновой
  const [busy, setBusy] = useState('');
  const [mode, setMode] = useState('');
  const u = upd || ((u0 && u0.checked_at) ? u0 : null);
  const curMode = mode || (u0 && u0.mode) || 'notify';
  const behind = u && u.ok ? (u.behind || 0) : 0;
  const line = !u ? 'Ещё не проверялось — фоновая проверка раз в несколько часов'
    : !u.ok ? 'Проверка не удалась: ' + (u.error || 'ошибка')
    : behind > 0 ? 'Доступно обновление: ' + behind + ' коммит(ов)'
    : 'Актуальная версия — новых коммитов нет';

  const check = async () => {
    setBusy('check');
    try {
      const r = await call('adminUpdateCheck');
      setUpd(r.update);
      if (r.update && r.update.mode) setMode(r.update.mode);
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
    setBusy('');
  };
  // cta «Обновить» + danger=false: без них useConfirm рисует красную «Удалить» —
  // пугающе для НЕразрушающего действия (фидбэк 01.08)
  const apply = () => confirm('Обновить бота и перезапустить?',
    'Бот будет недоступен одну-две минуты, пока сторож поднимает его с новым кодом.',
    async () => {
      setBusy('apply');
      try {
        await call('adminUpdateApply');
        toast('Обновляюсь — бот перезапустится через минуту-другую', { kind: 'ok', ms: 9000 });
      } catch (e) { toast(e.message, { kind: 'warn2', ms: 9000 }); }
      setBusy('');
    }, 'Обновить', false);
  const setModeSrv = async (m) => {
    try {
      await call('adminUpdateMode', m);
      setMode(m);
    } catch (e) { toast(e.message, { kind: 'warn2' }); }
  };

  return <Sec card ic="bolt" t="Обновления" style={{ flex: '0 0 auto' }}
    right={<span className="mono" style={{ fontSize: 12.5, color: 'var(--faint)' }}>
      {(v.channel || '?') + (v.sha ? ' · ' + v.sha : '')}</span>}>
    <div className={behind > 0 ? 'warn' : 'hint'} style={{ fontSize: 13 }}>{line}</div>
    {behind > 0 && (u.commits || []).length ? <div style={{ marginTop: 8 }}>
      {(u.commits || []).slice(0, 8).map((c, i) => <div key={i} className="mono" style={{
        fontSize: 11.5, color: 'var(--faint)', overflow: 'hidden', textOverflow: 'ellipsis',
        whiteSpace: 'nowrap', marginTop: 2 }}>{c}</div>)}
    </div> : null}
    <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
      <ABtn kind="line" style={{ flex: 1 }} disabled={!!busy} onClick={check}>
        {busy === 'check' ? 'Проверяю…' : 'Проверить'}
      </ABtn>
      <ABtn kind="full" style={{ flex: 1 }} disabled={!!busy || behind === 0} onClick={apply}>
        {busy === 'apply' ? 'Обновляю…' : 'Обновить сейчас'}
      </ABtn>
    </div>
    <div style={{ display: 'flex', gap: 6, marginTop: 12, alignItems: 'center' }}>
      <span className="hint" style={{ flex: '0 0 auto' }}>Режим:</span>
      {[['off', 'выкл'], ['notify', 'уведомлять'], ['auto', 'обновляться самому']].map(([m, l]) =>
        <button key={m} type="button" className={'chip' + (curMode === m ? ' on' : '')}
          onClick={() => setModeSrv(m)}>{l}</button>)}
    </div>
    <div className="hint" style={{ marginTop: 7 }}>
      {curMode === 'auto' ? 'Бот сам подтянет обновления с GitHub и перезапустится.'
        : curMode === 'off' ? 'Обновления не проверяются — только вручную кнопкой.'
        : 'Придёт уведомление в Telegram, обновление — по кнопке.'}
    </div>
  </Sec>;
}

/** Лицензия студии (трек C, п.4). Стоит рядом с «Обновлениями» намеренно: лицензия
 * управляет ровно ими — без действующей лицензии код не обновляется, а приём заказов,
 * расчёты и данные студии работают как обычно. Ключ и подпись сюда не приезжают вовсе:
 * сервер отдаёт только расшифрованные поля (кому выдана, план, срок). */
function LicenseCard({ lic }) {
  if (!lic) return null;
  const st = lic.state || 'missing';
  const left = lic.days_left;
  const dt = (iso) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    return m ? m[3] + '.' + m[2] + '.' + m[1] : (iso || '');
  };
  const head = st === 'ok' ? (lic.expires
        ? <div className="okay">Лицензия действует до {dt(lic.expires)}</div>
        : <div className="okay">Лицензия бессрочная</div>)
    : st === 'expiring'
      ? <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />
          Лицензия истекает: осталось {left == null ? '?' : Math.max(0, Math.round(left))} дн (до {dt(lic.expires)})</div>
      : st === 'expired'
        ? <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />
            Лицензия истекла {dt(lic.expires)} — обновления кода не применяются</div>
        : st === 'trial'
          ? <div className="okay">Пробный период: осталось {left == null ? '?' : Math.max(0, Math.ceil(left))} дн (до {dt(lic.expires)})</div>
        : st === 'trial_over'
          ? <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />
              Пробный период закончился {dt(lic.expires)} — обновления кода не применяются</div>
        : st === 'missing'
          ? <div className={lic.enforcement === 'off' ? 'hint' : 'warn'}>
              {lic.enforcement === 'off' ? null : <DIcon n="bell" size={15} style={{ flexShrink: 0 }} />}
              Ключ не задан{lic.enforcement === 'off' ? ' (в этой сборке не требуется)' : ' — обновления кода применяться не будут'}</div>
        : <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />
            {lic.reason || 'Ключ не проходит проверку'}</div>;

  return <Sec card ic="doc" t="Лицензия" style={{ flex: '0 0 auto' }}
    right={lic.plan ? <span className="mono" style={{ fontSize: 12.5, color: 'var(--faint)' }}>{lic.plan}</span> : null}>
    {head}
    {lic.studio ? <div className="hint" style={{ marginTop: 7 }}>
      Выдана: {lic.studio}{lic.id ? ' · №' + lic.id : ''}{lic.issued ? ' · от ' + dt(lic.issued) : ''}
    </div> : null}
    {lic.edition_mismatch ? <div className="hint" style={{ marginTop: 6 }}>
      Ключ выдан на редакцию «{lic.edition}», сборка опознана как «{lic.build_edition}» — ничего не отключено.
    </div> : null}
    {st === 'missing' || st === 'invalid' || st === 'expired' || st === 'trial_over' ? <div className="hint" style={{ marginTop: 6 }}>
      Ключ вставляется строкой LICENSE_KEY= в файл .env рядом с ботом либо файлом license.key в каталоге данных.
      После правки бот нужно перезапустить.
    </div> : null}
  </Sec>;
}

/** Копилка исходов печати (этап 0 плана автотюнинга). Показывает РОВНО одно: сколько
 * выполненных заказов уже описаны применёнными параметрами и чем кончились. Ничего не
 * рекомендует и ничего не обещает — накопленное пригодится, когда выборки хватит на
 * проверку правил. Владельческая цифра, поэтому под canSettings: оператору у стола она
 * только мешает. */
function TuningCard({ t }) {
  if (!t) return null;
  const n = Number(t.count || 0);
  const rated = Number(t.rated || 0);
  // Три РАЗНЫХ числа, и путать их нельзя: заказов в выборке всего, из них с замороженными
  // на момент печати ручками, из них с оценкой клиента. У заказа, чья печать в журнал не
  // заносилась, параметры лишь восстановлены по сегодняшним пресетам — это не «накоплено».
  const withParams = Number(t.with_params || 0);
  const mats = Array.isArray(t.by_material) ? t.by_material : [];
  return <Sec card ic="layers" t="Копилка исходов печати" style={{ flex: '0 0 auto' }}>
    {n
      ? <div className="okay">Накоплено {n} {plural(n, 'выполненный заказ', 'выполненных заказа',
          'выполненных заказов')}, из них с замороженными параметрами печати — {withParams},
          с оценкой клиента — {rated}</div>
      : <div className="hint">Пока ничего не накоплено: исходы копятся с первого заказа,
          переведённого в «напечатано»</div>}
    {mats.length ? <div className="hint" style={{ marginTop: 7 }}>
      {mats.map((m) => `${m.material} — ${m.count} (с оценкой ${m.rated})`).join(' · ')}
    </div> : null}
    <div className="hint" style={{ marginTop: 6 }}>
      Записывается при переводе заказа в «напечатано»: слой, заполнение, стенки, материал,
      объём и габариты — и что из этого вышло. У заказов, чья печать в журнал не заносилась,
      параметры лишь восстановлены по нынешним пресетам. Ничего не пересчитывается и не
      подставляется в новые заказы.
    </div>
  </Sec>;
}

// Пресеты порога автовыгрузки ИИ по простою (мин); 0 — держать в VRAM, пока не выгрузят руками.
const AI_IDLE_PRESETS = [0, 2, 5, 15, 60];

/** ИИ и видеокарта — карточка со строками .setrow, полосой VRAM и управлением генератором
 * (OpenTodo ee064f93): вкл/выкл, порог автовыгрузки, ручная выгрузка из VRAM. */
function AiCard({ ai, gpu, onChanged }) {
  const { toast } = useContext(AppCtx);
  if (!ai && !(gpu && gpu.name)) return null;
  // Единая точка действий: сервер на отказ отвечает {ok:false} → call() бросает — показываем
  // причину, а не молчим; после успеха перечитываем статус (положение тумблера/подсветку).
  const act = async (body, okMsg) => {
    try {
      await call('adminAiAction', body);
      if (okMsg) toast(okMsg, { kind: 'ok' });
    } catch (e) {
      toast(String(e.message || e), { kind: 'warn2' });
    }
    onChanged && onChanged();
  };
  // [иконка, подпись, значение]; иконки взяты из макета (AI_IC), цвет — фиолетовый
  const rows = [];
  if (ai) {
    // off_by_flag — файл-флаг (ярлык на рабочем столе ИЛИ тумблер ниже — источник один):
    // генерация выключена руками, а не сломалась. «Выключена» из-за настройки .env — другое.
    rows.push(['bolt', 'Генерация аватарок', ai.off_by_flag
      ? <span className="stb red">выключена руками</span>
      : (ai.enabled ? 'включена' : 'выключена')]);
    if (ai.model) rows.push(['grid', 'Модель', ai.model]);
    rows.push(['layers', 'Загружена в память', ai.loaded ? 'да' : 'нет']);
    // Простой считается от последней генерации: по нему видно, скоро ли модель сама уйдёт из VRAM.
    if (ai.loaded && ai.idle_min != null) rows.push(['clock', 'Простой модели', fmtMin(ai.idle_min)]);
    if (ai.load_failed) rows.push(['pulse', 'Загрузка модели', <span className="stb red">сбой</span>]);
  }
  if (gpu && gpu.name) {
    rows.push(['queue', 'Видеокарта', gpu.name]);
    rows.push(['pulse', 'Загрузка · температура', `${gpu.util_pct}% · ${gpu.temp_c}°`]);
  }
  const total = gpu && gpu.vram_total_mb;
  const idleCur = ai && ai.idle_unload_min != null ? Number(ai.idle_unload_min) : null;
  return <Sec card ic="bolt" t="ИИ и видеокарта" style={{ flex: '0 0 auto' }}>
    {rows.map(([ic, k, v], i) => <div key={i} className="setrow">
      <DIcon n={ic} size={15} style={{ color: 'var(--violet)', flexShrink: 0 }} />
      <span className="sl">{k}</span>
      {/* длинное имя модели/карты не должно ломать строку — режем многоточием */}
      <span className="mono" title={typeof v === 'string' ? v : undefined}
        style={{ fontSize: 13, fontWeight: 600, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</span>
    </div>)}
    {total ? <div style={{ marginTop: 8 }}>
      <Bar pct={(gpu.vram_used_mb / total) * 100} h={7} color="var(--violet)" />
      <div className="hint" style={{ marginTop: 6 }}>VRAM {gb(gpu.vram_used_mb)} / {gb(total)} ГБ</div>
    </div> : null}
    {ai ? <div style={{ marginTop: 12 }}>
      {/* тот же файл-флаг, что у ярлыка на рабочем столе: выключение сразу освобождает VRAM */}
      <Toggle on={!!ai.enabled} ic="bolt"
        ds={ai.enabled ? 'модель грузится по первому клику «Аватар»' : 'аватарки рисуются процедурно, VRAM свободна'}
        onChange={(v) => act({ action: 'toggle', on: v },
          v ? 'ИИ-генерация включена' : 'ИИ-генерация выключена — VRAM освобождена')}>
        ИИ-генерация</Toggle>
      {/* выбор модели генерации: применяется сразу (старый пайплайн выгружается из VRAM),
          хранится в настройках БД поверх .env — переживает рестарт. Подробности — в title. */}
      {ai.models && ai.models.length ? <div className="ctl-row" style={{ marginTop: 6 }}>
        <span className="ctl-k"><DIcon n="grid" size={13} />Модель</span>
        <div className="ctl-btns">
          {ai.models.map((m) => <ABtn key={m.id} kind="sm line" title={m.desc}
            style={m.current ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
            onClick={() => !m.current && act({ action: 'model', model: m.id },
              `Модель: ${m.label} — применится со следующей генерации`)}>
            {m.label}</ABtn>)}
        </div>
      </div> : null}
      {/* порог автовыгрузки по простою: живёт в настройках БД поверх .env, без перезапуска */}
      <div className="ctl-row" style={{ marginTop: 6 }}>
        <span className="ctl-k"><DIcon n="clock" size={13} />Автовыгрузка{
          idleCur == null ? '' : (idleCur ? ` · ${num(idleCur)} мин` : ' · выкл')}</span>
        <div className="ctl-btns">
          {AI_IDLE_PRESETS.map((m) => <ABtn key={m} kind="sm line"
            title={m ? `Выгружать из VRAM после ${m} мин простоя` : 'Не выгружать по простою'}
            style={idleCur === m ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}
            onClick={() => act({ action: 'idle', minutes: m },
              m ? `Автовыгрузка: через ${m} мин простоя` : 'Автовыгрузка по простою выключена')}>
            {m ? `${m} мин` : 'выкл'}</ABtn>)}
        </div>
      </div>
      <ABtn kind="line full" icon="trash" disabled={!ai.loaded} style={{ marginTop: 10 }}
        title={ai.loaded ? 'Освободить VRAM/RAM сейчас — следующая генерация загрузит модель заново' : 'Модель не загружена'}
        onClick={() => act({ action: 'unload' }, 'Модель выгружена из VRAM')}>
        Выгрузить из VRAM сейчас</ABtn>
    </div> : null}
  </Sec>;
}

/** Проба генерации картинок (22.07): промт → картинка той моделью, что выбрана в карточке
 * выше. Панель для сравнения моделей: переключил → сгенерировал → посмотрел. Без сохранения
 * на диск (base64 в ответе) и без процедурного фолбэка — честный отказ полезнее заглушки.
 * Каждый клик — новый случайный seed, работает и как ручной прогрев модели. */
function AiTestCard({ ai }) {
  const { lightbox } = useContext(AppCtx);
  const [prompt, setPrompt] = useState('');
  const [styled, setStyled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [sec, setSec] = useState(0);          // живой счётчик: холодная загрузка — до минуты
  const [res, setRes] = useState(null);       // {image, engine, seconds, seed}
  const [err, setErr] = useState('');
  useEffect(() => {
    if (!busy) return undefined;
    setSec(0);
    const id = setInterval(() => setSec((v) => v + 1), 1000);
    return () => clearInterval(id);
  }, [busy]);
  if (!ai) return null;
  const go = async () => {
    const p = prompt.trim();
    if (!p || busy) return;
    setBusy(true); setErr('');
    try {
      setRes(await call('adminAiTest', { prompt: p, styled }));
    } catch (e) {
      setErr(String(e.message || e));
    }
    setBusy(false);
  };
  return <Sec card ic="palette" t="Проба генерации" style={{ flex: '0 0 auto' }}>
    <div className="hint">Промт по-русски или по-английски — нарисует модель, выбранная выше.
      Каждый запуск — новая случайная картинка.</div>
    <textarea className="inp" rows={2} maxLength={300} value={prompt} disabled={busy}
      placeholder="космический ёжик с кружкой какао"
      style={{ marginTop: 8, resize: 'vertical', lineHeight: 1.4 }}
      onChange={(e) => setPrompt(e.target.value)}
      onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) go(); }} />
    <Toggle on={styled} ic="star"
      ds={styled ? 'как у аватарок: милый маскот-фигурка, оранжевая подсветка' : 'промт уходит в модель как есть (только перевод на английский)'}
      onChange={setStyled}>Фирменный стиль</Toggle>
    <ABtn kind="line full" icon="palette" disabled={busy || !prompt.trim()} style={{ marginTop: 8 }}
      title="Ctrl+Enter в поле промта тоже запускает"
      onClick={go}>{busy ? `Генерирую… ${sec} с` : 'Сгенерировать'}</ABtn>
    {busy ? <div className="hint" style={{ marginTop: 6 }}>Горячая модель ~20 с, холодная — до минуты.</div> : null}
    {err ? <div className="warn" style={{ marginTop: 8 }}>
      <DIcon n="bell" size={15} style={{ flexShrink: 0 }} />{err}</div> : null}
    {res ? <div style={{ marginTop: 10 }}>
      <img src={res.image} alt="проба генерации" title="Открыть крупно"
        style={{ width: '100%', borderRadius: 10, cursor: 'zoom-in', display: 'block' }}
        onClick={() => lightbox(res.image)} />
      <div className="hint" style={{ marginTop: 6 }}>{res.engine} · {res.seconds} с · seed {res.seed}</div>
    </div> : null}
  </Sec>;
}

/** Пул аккаунтов MakerWorld: сводка + строка на аккаунт (кулдаун 418, срок, действия).
 *  Зачем пул: MakerWorld банит автоскачивание примерно с девятой закачки подряд (418, ~90 мин
 *  на аккаунт). Бот сам переключается на следующий аккаунт — карточка показывает, кто в строю. */
function TokenCard({ tok, accounts, onChanged }) {
  const { toast } = useContext(AppCtx);
  const confirm = useConfirm();
  const [busy, setBusy] = useState('');
  if (!tok) return null;
  const list = accounts || [];
  const left = tok.days_left;
  const soon = tok.configured && left != null && left < 7;
  const usable = tok.usable == null ? (tok.configured ? 1 : 0) : tok.usable;
  const ready = tok.ready == null ? (tok.configured ? 1 : 0) : tok.ready;

  const act = async (action, body) => {
    setBusy((body.id || '') + action);
    try {
      const r = await call('adminMwAccount', { action, ...body });
      toast(r && r.hint ? 'Аккаунт заведён' : 'Готово',
        r && r.hint ? { kind: 'ok', sub: r.hint, ms: 15000 } : { kind: 'ok', ms: 2500 });
      if (onChanged) onChanged();
    } catch (e) {
      toast('Не вышло', { kind: 'err', sub: String((e && e.message) || e), ms: 12000 });
    } finally { setBusy(''); }
  };

  return <Sec card ic="external" t="Автозагрузка MakerWorld" style={{ flex: '0 0 auto' }}>
    {!tok.configured
      ? <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />Ни одного аккаунта — автозагрузка моделей не работает</div>
      : usable === 0
        ? <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />Все аккаунты сейчас в кулдауне — качать нечем</div>
        : soon
          ? <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />Токен истекает: осталось {Math.round(left)} дн</div>
          : <div className="okay">В строю {usable} из {ready} · запас закачек ×{ready}</div>}

    {list.map((a) => {
      const cool = a.cooldown_sec > 0;
      const state = !a.configured ? 'нет токена'
        : !a.enabled ? 'выключен'
          : a.expired ? 'токен истёк'
            : cool ? `кулдаун ${Math.max(1, Math.round(a.cooldown_sec / 60))} мин`
              : a.days_left != null ? `${Math.round(a.days_left)} дн` : 'активен';
      return <div key={a.id} style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600 }}>{a.label}</div>
          {/* id виден всегда: он нужен для команд mw_login.py --account <id> */}
          <div className="hint" title={a.profile}>
            {a.id} · {state}{a.uses ? ` · ${a.uses} запросов` : ''}
            {a.last_error ? ` · ${a.last_error}` : ''}
          </div>
        </div>
        <button className="db line" disabled={!!busy} onClick={() => act('refresh', { id: a.id })}
          title="Продлить токен из браузерной сессии этого аккаунта">↻</button>
        {cool ? <button className="db line" disabled={!!busy} onClick={() => act('clear', { id: a.id })}
          title="Снять кулдаун вручную">снять</button> : null}
        <button className="db line" disabled={!!busy} onClick={() => act('toggle', { id: a.id })}
          title={a.enabled ? 'Убрать из ротации' : 'Вернуть в ротацию'}>{a.enabled ? 'вкл' : 'выкл'}</button>
        {a.id !== 'main' ? <button className="db line" disabled={!!busy}
          onClick={() => confirm('Убрать аккаунт из пула?',
            `«${a.label}» исчезнет из ротации вместе с токеном. Профиль браузера останется — `
            + `вернуть аккаунт можно будет через python get_jwt.py --account ${a.id}.`,
            () => act('delete', { id: a.id }))}
          title="Убрать аккаунт из пула">×</button> : null}
      </div>;
    })}

    <button className="db line full" style={{ marginTop: 12 }} disabled={!!busy}
      onClick={() => {
        const label = window.prompt('Название нового аккаунта MakerWorld (для админки)');
        if (label) act('add', { label });
      }}>Добавить аккаунт</button>
    <div className="hint" style={{ marginTop: 6 }}>
      После добавления войдите на этой машине: python mw_login.py --account &lt;id&gt; → python get_jwt.py --account &lt;id&gt;
    </div>
  </Sec>;
}

/** TURZX-экран: в макете это тумблер, а не пара кнопок. Начальное положение — из adminStatus. */
function ScreenPowerCard({ initial }) {
  const { toast } = useContext(AppCtx);
  const [on, setOn] = useState(!!initial);
  return <Sec card ic="grid" t="TURZX-экран" style={{ flex: '0 0 auto' }}>
    <Toggle on={on} ic="chart" ds="физический экран рядом с принтером"
      onChange={async (v) => {
        try {
          await call('adminScreenPower', v);
          setOn(v);   // положение меняем только после успеха, иначе тумблер соврёт
          toast(v ? 'Экран включён' : 'Экран выключен', { kind: 'ok' });
        } catch (e) {
          toast(String(e.message || e), { kind: 'warn2' });
        }
      }}>Дашборд на экране</Toggle>
  </Sec>;
}

/** Камера принтера — секции в макете нет, но функция рабочая; вид приведён к макету. */
function CameraCard() {
  const { toast } = useContext(AppCtx);
  const [res, setRes] = useState(null);
  return <Sec card ic="camera" t="Камера принтера" style={{ flex: '0 0 auto' }}>
    {res
      ? (res.ok
        ? <div className="okay">Камера отвечает{res.url ? ` · ${res.url}` : ''}</div>
        : <div className="warn"><DIcon n="bell" size={15} style={{ flexShrink: 0 }} />{res.error || 'Камера недоступна'}</div>)
      : <div className="hint">Проверка достаёт кадр из потока и показывает причину сбоя.</div>}
    <ABtn kind="line full" style={{ marginTop: 12 }} onClick={async () => {
      // недоступная камера — это ok:false, то есть call() бросает: ответ нужен из текста ошибки
      try {
        const r = await call('cameraCheck');
        setRes(r);
        toast('Камера отвечает', { kind: 'ok' });
      } catch (e) {
        setRes({ ok: false, error: String(e.message || e) });
        toast('Камера недоступна', { kind: 'warn2' });
      }
    }}>Проверить</ABtn>
  </Sec>;
}
