// calcview.jsx — «Смета клиента»: что именно человек увидел на экране, когда считал цену.
//
// Открывается кликом по плитке ленты событий (пульт) и по строке «Активности». До этого клик
// вёл только в карточку клиента: сама цена нигде не хранилась — смета живёт в памяти бота и
// умирает с рестартом, а в ленте оставалась одна строка текста. Теперь каждое событие про цену
// несёт calc_id, а сервер отдаёт снимок (/api/admin/calc).
//
// Главное на экране — не итог, а ЧЕМ он получен: мгновенные ветки (≈оценка, кэш прошлого
// слайса, данные MakerWorld) показывают клиенту приблизительную цену и уточняются фоновым
// слайсом. Разницу между показанным и уточнённым рисуем отдельным блоком: именно она отвечает
// на вопрос «нормальную ли цену мы человеку назвали».

import { DIcon } from '../icons.jsx';
import { Drawer } from '../ui.jsx';
import { useApi } from '../api.jsx';
import { dt } from '../fmt.jsx';
import { Loading, ErrBox, ABtn, OrderThumb } from '../parts.jsx';

/** Есть ли у события сохранённая смета: только такие плитки открывают дровер. */
export const hasCalc = (e) => !!(e && e.calc_id);

const Row = ({ l, v }) => (v === '' || v === null || v === undefined
  ? null
  : <div className="bd-row"><span>{l}</span><b>{v}</b></div>);

export function CalcDrawer({ id, onClose, nav }) {
  const d = useApi('adminCalc', [id], [id]);
  const c = d.data && d.data.calc;
  const r = c && c.refined;

  const footer = <>
    {c && c.user_id
      ? <ABtn kind="full" style={{ flex: 1 }}
          onClick={() => { onClose(); nav('clients', { sel: c.user_id }); }}>
          Открыть клиента
        </ABtn>
      : null}
    {c && c.next_order
      ? <button className="db soft" onClick={() => { onClose(); nav('orders', { sel: c.next_order.id }); }}>
          Заказ #{c.next_order.id}
        </button>
      : null}
    <button className="db line" onClick={onClose}>Закрыть</button>
  </>;

  return <Drawer title="Смета клиента"
    sub={c ? `${c.action} · ${c.ago}` : 'что видел клиент на экране'}
    onClose={onClose} footer={footer}>
    {d.loading && !d.data ? <Loading what="Открываю смету…" /> : null}
    {d.err ? <ErrBox err={d.err} onRetry={d.reload} /> : null}
    {c ? <div className="fgap">

      {/* шапка: модель, кто считал, когда */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <OrderThumb src={c.preview} size={56} />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontWeight: 600 }}>{c.model_label || 'Без названия'}</div>
          <div className="hint" style={{ margin: 0 }} title={dt(c.at)}>
            {c.client ? c.client.name + ' · ' : ''}{c.printer}
          </div>
        </div>
      </div>

      {/* итог — ровно тот, что стоял у клиента на экране */}
      <div className="sec" style={{ background: 'var(--bg)' }}>
        <div className="lab">Клиент увидел</div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
          <b className="mono" style={{ fontSize: 26, color: 'var(--price)' }}>
            {c.approx ? '≈' : ''}{c.total_label}
          </b>
          {c.total_before_label
            ? <s className="mono faint">{c.total_before_label}</s> : null}
          {c.discount_pct ? <span className="stb amber">−{c.discount_pct}%</span> : null}
          {c.each_label ? <span className="hint" style={{ margin: 0 }}>{c.each_label} за шт</span> : null}
        </div>
        <div className="hint" style={{ marginBottom: 0 }}>
          {c.grams_total} г · {c.time_label} · {c.source_label}
          {c.estimated ? ' · слайса не было' : ''}
        </div>
      </div>

      {/* чем кончился фоновый слайс: показанное против уточнённого */}
      {r ? <div className="sec" style={{ background: 'var(--bg)' }}>
        <div className="lab">
          <DIcon n="calc" size={13} />Слайс уточнил цену
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
          <b className="mono" style={{ fontSize: 20 }}>{r.total_label}</b>
          {/* дороже после слайса — мы недосчитали клиенту; дешевле — назвали лишнего */}
          <span className={'stb ' + (r.delta_pct > 0 ? 'amber' : (r.delta_pct < 0 ? 'blue' : 'gray'))}>
            {r.delta_pct > 0 ? '+' : ''}{r.delta_pct}%
          </span>
          <span className="hint" style={{ margin: 0 }}>{r.delta_label} · {r.ago}</span>
        </div>
        <div className="hint" style={{ marginBottom: 0 }}>
          {r.grams} г · {r.time_label} — столько вышло по честному слайсу
        </div>
      </div> : null}

      {/* параметры расчёта: по ним видно, ЧТО именно считали */}
      <div className="sec">
        <div className="lab">Параметры</div>
        <Row l="Пластик" v={c.plastic_name + (c.color ? ' · ' + c.color : '')} />
        <Row l="Качество" v={c.quality} />
        <Row l="Прочность" v={c.strength} />
        <Row l="Тираж" v={c.qty > 1 ? c.qty + ' шт' : '1 шт'} />
        <Row l="Габариты" v={c.dims} />
        <Row l="Столов" v={c.plates > 1 ? c.plates : ''} />
        <Row l="Масштаб" v={c.scale_pct && c.scale_pct !== 100 ? c.scale_pct + '%' : ''} />
        <Row l="Доп. цвета" v={c.extra_colors || ''} />
        <Row l="Продувка" v={c.purge ? c.purge + ' г' : ''} />
        <Row l="Срочность" v={c.urgency === 'rush' ? 'срочно' : ''} />
        <Row l="Обещали к" v={c.term || c.eta} />
        <Row l="Промокод" v={c.promo ? `${c.promo.code} (−${c.promo.pct}%)` : ''} />
      </div>

      {/* разбивка — та самая, что клиент раскрывал кнопкой «Из чего складывается цена» */}
      {(c.lines || []).length ? <div className="sec">
        <div className="lab">Из чего сложилась цена</div>
        {c.lines.map((ln, i) => <div key={i} className="bd-row">
          <span>{ln[0]}</span><b>{ln[1]}</b>
        </div>)}
        <div className="bd-row tot">
          <span>Итого{c.qty > 1 ? ` за ${c.qty} шт` : ''}</span><b>{c.total_label}</b>
        </div>
      </div> : null}

      {/* печатопригодность: клиент видел эту же плашку */}
      {c.verdict && c.verdict !== 'ok' ? <div className="warn">
        {c.verdict === 'wont_fit' ? 'Не влезает на стол' : 'Печать рискованная'}
        {/* dfm — [{key,title,detail,status}] из texts.printability_verdict */}
        {(c.dfm || []).length ? ': ' + c.dfm.map((x) => x.title || x.key).join('; ') : ''}
      </div> : null}

      {/* согласился ли человек с ценой — без домыслов: первый заказ в сутки после расчёта */}
      <div className="hint" style={{ margin: 0 }}>
        {c.next_order
          ? `Ближайший заказ после расчёта — #${c.next_order.id} (${c.next_order.status_label}), `
            + `через ${c.next_order.minutes_after} мин`
            + (c.next_order.same_model ? ', та же модель' : ', другая модель')
          : 'Заказа в течение суток после этого расчёта не было.'}
      </div>

    </div> : null}
  </Drawer>;
}
