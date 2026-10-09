// statuses.jsx — статусы заказа. Набор совпадает с бэкендом (app/db.py requests.status)
// и с мобильной админкой (app/webui/screens_admin.js ST_LABEL/ST_COLOR).
// Цветовые классы — из desktop.css (.stb.gray/.amber/.violet/.green/.red/.blue).

export const ST = {
  new:           { l: 'Новый',          c: 'gray' },
  // мастер одобрил и выставил счёт (этап A1, фидбэк 31.07); оплата сама переведёт «В работе»
  awaiting_payment: { l: 'Ждёт оплаты', c: 'violet' },
  in_progress:   { l: 'В работе',       c: 'amber' },
  printing:      { l: 'Печатается',     c: 'amber' },
  price_changed: { l: 'Цена изменена',  c: 'violet' },
  invoiced:      { l: 'Счёт выставлен', c: 'violet' },
  printed:       { l: 'Напечатано',     c: 'green' },
  done:          { l: 'Готово',         c: 'green' },
  handed:        { l: 'Вручено',        c: 'green' },
  // отложен — не отказ: заказ жив, просто вне очереди (сине-серый, а не красный)
  deferred:      { l: 'Отложен',        c: 'blue' },
  rejected:      { l: 'Отказ',          c: 'red' },
  cancelled:     { l: 'Отменён',        c: 'red' },
  self:          { l: 'Самопечать',     c: 'blue' },
};
