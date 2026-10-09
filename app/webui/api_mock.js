// api_mock.js — мок-домен мини-аппа: выдуманные ответы, повторяющие форму реального API.
//
// Зачем отдельным файлом (волна 7): мок лежал внутри api.js и ехал КАЖДОМУ клиенту Telegram,
// хотя нужен ровно в обратном случае — когда initData нет и приложение открыли в обычном
// браузере для предпросмотра. Теперь api.js подключает этот файл только в таком режиме
// (см. __PS_MOCK_INSTALL__ там же).
//
// Контракт с api.js: он объявляет у себя в замыкании переменные M / mockPriceCalc / mockCalc /
// mockBrand / mockBrandValues и заполняет их тем, что вернёт наша build(). Благодаря этому тела
// всех методов API не изменились ни на символ — как обращались к M, так и обращаются.
// fmt/human приезжают параметром: они остались в api.js, потому что вызываются на БОЕВЫХ путях
// отрисовки (корзина, доставка, смета, кастомайзер) и к моку отношения не имеют.
//
// Отступы блока сохранены от api.js — код перенесён дословно, чтобы правки в нём читались
// построчно, а не тонули в сдвиге всего файла.
(function () {
  var install = window.__PS_MOCK_INSTALL__;
  if (!install) {
    // Файл подключили без api.js или api.js не отработал. Молчать нельзя (мок «есть», а данных
    // нет), но и падать здесь незачем — настоящая причина выше по стеку загрузки.
    try { console.warn('api_mock.js: api.js не загружен — мок не подключён'); } catch (e) {}
    return;
  }
  install(function (helpers) {
    var fmt = helpers.fmt, human = helpers.human;

    var M = {
      options: {
        ok: true, currency: 'руб.', pending_orders: 0, orders_count: 3,
        bot_username: 'my_print_studio_bot', referrals: 0, is_admin: true,
        // Роль и права сотрудника (A4): в предпросмотре — владелец со всеми правами
        me: { role: 'owner', role_label: 'Владелец', root: true,
          caps: ['orders', 'printer', 'finance', 'pricing', 'clients', 'settings'] },
        // Доставка (A2): типов нет → у клиента исчезает и выбор получения, и адрес в профиле
        delivery: { enabled: true, default_type_id: 1, default_address: 'ул. Ленина, 5',
          types: [{ id: 1, name: 'Курьером по городу', price: 5, free_from: 50, eta: '1–2 дня' },
                  { id: 2, name: 'Почтой по области', price: 12, free_from: 0, eta: '2–4 дня' }] },
        models_count: 4, collection_count: 6,
        last_order: { id: 13, label: 'Устройство по перетасовке карт' },
        // Дефолтный пластик карточки товара (OpenTodo ae038968): в проде — Elegoo Matte
        // Тёмно-синий. В моке нарочно берём id 2 (Elegoo PETG, «Чёрный») — НЕ самый дешёвый
        // пластик мока (id 3, Elegoo PLA), иначе applyModel() было бы не на чем отличить выбор
        // «дефолт с бэка» от старого фолбэка cheapestPlastic (оба привели бы к id 3).
        default_plastic_id: 2, default_color: 'Чёрный',
        // Срочность (B3): множитель для подписи «×K». Конкретная дата больше не приходит с CONFIG-
        // экрана — срок зависит от времени печати и появляется в смете (result.term).
        urgent_k: 1.5, rush_pct: 50,
        // избранное (сердечки): «Яйцо дракона» уже в избранном — виден залитый heart и раздел
        fav_ids: [4],
        printers: [{ id: 1, name: 'Elegoo Centauri Carbon 2' }],
        plastics: [
          { id: 1, name: 'Crown PLA шёлковый трёхцветный', type: 'PLA', price: 50, density: 1.24,
            category: 'Шелковый',
            is_default: true, colors: ['Зелёный-синий-красный'],
            description: 'Шелковистый трёхцветный PLA с плавным переливом. Лёгок в печати, идеален для декора.',
            color_css: ['linear-gradient(135deg,#2fae57,#2f6fd6,#d23b3b)'],
            photoUrl: '../assets/filament/f1.jpg', _multicolor: true, _density: 1.24, _price: 50 },
          { id: 2, name: 'Elegoo PETG', type: 'PETG', price: 45, density: 1.27,
            category: 'Инженерный',
            is_default: false, colors: ['Белый', 'Синий', 'Чёрный'], color_css: ['#e8edf2', '#2f6fd6', '#222a33'],
            description: 'Прочный и термостойкий PETG. Подходит для функциональных деталей.',
            // фото катушек по цветам (eb9a9efe): в проде — /spool/{id}?c=..., в моке — data-URI
            spools: { 'Синий': 'data:image/gif;base64,R0lGODlhAQABAIAAAC9v1gAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==' },
            photoUrl: '../assets/filament/f3.jpg', _multicolor: false, _density: 1.27, _price: 45 },
          { id: 3, name: 'Elegoo PLA', type: 'PLA', price: 38, density: 1.24,
            category: 'Обычный',
            is_default: false, colors: ['Чёрный', 'Белый', 'Красный', 'Синий', 'Зелёный', 'Жёлтый'],
            color_css: ['#222a33', '#e8edf2', '#e23b3b', '#2f6fd6', '#2dd47e', '#ffcf33'],
            out_of_stock: ['Жёлтый'], low_stock: ['Зелёный'],
            description: 'Базовый PLA — большая палитра матовых цветов.',
            photoUrl: '../assets/filament/f2.jpg', _multicolor: false, _density: 1.24, _price: 38 },
        ],
        qualities: [
          // стопы ползунка высоты слоя (03.08.2026); nozzle — бэйдж «сопло 0.6» на 0.30/0.36
          { id: 1, label: 'Высокое — 0.12 мм', is_default: false, layer_height: 0.12, nozzle: '0.4', _tmult: 1.62 },
          { id: 2, label: 'Тонкое — 0.16 мм', is_default: false, layer_height: 0.16, nozzle: '0.4', _tmult: 1.28 },
          { id: 3, label: 'Стандарт — 0.20 мм', is_default: true, layer_height: 0.20, nozzle: '0.4', _tmult: 1.00 },
          { id: 5, label: 'Быстрое — 0.24 мм', is_default: false, layer_height: 0.24, nozzle: '0.4', _tmult: 0.86 },
          { id: 6, label: 'Крупное — 0.30 мм (сопло 0.6)', is_default: false, layer_height: 0.30, nozzle: '0.6', _tmult: 0.62 },
          { id: 7, label: 'Черновое — 0.36 мм (сопло 0.6)', is_default: false, layer_height: 0.36, nozzle: '0.6', _tmult: 0.52 },
        ],
        strengths: [
          { id: 1, label: 'Низкая (декор)', infill: 10, walls: 2, is_default: false },
          { id: 2, label: 'Средняя', infill: 20, walls: 3, is_default: false },
          { id: 3, label: 'Высокая', infill: 40, walls: 3, is_default: false },
          { id: 4, label: 'Максимальная', infill: 60, walls: 4, is_default: false },
          { id: 5, label: 'Золотая середина', infill: 15, walls: 3, is_default: true },
        ],
        // Опции постобработки (B4): платные ручные услуги мастера поверх печати. В проде каталог
        // пуст, пока владелец не заведёт свои; в моке — три штуки для статик-превью
        // (третья — «за деталь», accee7a9: ставка × детали × тираж).
        options: [
          { id: 1, name: 'Шлифовка', price: 5, per_part: 0 },
          { id: 2, name: 'Покраска', price: 12, per_part: 0 },
          { id: 3, name: 'Склейка деталей', price: 0.2, per_part: 1 },
        ],
      },
      model: { ok: true, token: 'mocktoken', label: 'Бэйбизавр', plates: 1, multicolor: true,
        num_colors: 3, preview: '../assets/models/m2.png', plate_count: 0, dims: '60×88×23',
        plate_mc: [],
        // цвета дизайна из .3mf (новый mc_colors бэка): applyModel предвыберет ближайшие из палитры
        mc_colors: ['#e8302a', '#1a72e8', '#f4f4f4'],
        // 3D-просмотр (B1): в проде бэк отдаёт model_url + гейт. В моке файла нет — model_url
        // пустой, а model_viewable=true, чтобы кнопка «3D» и демо-меш вьюера были видны в
        // статик-превью (viewer при пустом url рисует демо TorusKnot вместо fetch).
        model_url: '', model_ext: '.3mf', model_bytes: 1234567, model_viewable: true },
      collection: { ok: true, currency: 'руб.',
        // разделы каталога «Модели» (c9c42789): Напечатанное + темы из ВК
        categories: [
          { name: 'Напечатанное', count: 4, preview: '../assets/models/photo_dino.jpg' },
          { name: '3D модели FLEXI Toys', count: 2, preview: '../assets/models/m3.png' },
          // раздел без preview (OpenTodo 0f1869c8) — проверяем фолбэк-иконку в сетке каталога
          { name: '3D модели ламп', count: 3 },
        ],
        items: [
        { id: 1, label: 'Бэйбизавр', uses: 12, dims: '60×88×23', mc: 1, plates: 1, printed: 1, price: 3.1, category: 'Напечатанное', preview: '../assets/models/photo_dino.jpg' },
        { id: 2, label: 'Устройство по перетасовке карт', uses: 5, dims: '771×464×117', mc: 0, plates: 5, price: 15.5, category: 'Напечатанное', preview: '../assets/models/m1.png' },
        { id: 3, label: 'Фруктовая ловушка для мошек', uses: 3, dims: '119×58×35', mc: 0, plates: 1, price: 4.6, category: 'Напечатанное', preview: '../assets/models/m8.png' },
        { id: 4, label: 'Яйцо дракона', uses: 7, dims: '61×122×47', mc: 1, plates: 1, price: 6.8, category: 'Напечатанное', preview: '../assets/models/m4.png' },
        { id: 5, label: 'Octopus v6', uses: 4, dims: '128×128×49', mc: 0, plates: 1, price: 0, category: '3D модели FLEXI Toys', preview: '../assets/models/m3.png' },
        { id: 6, label: 'Бэйбизавр (синий)', uses: 2, dims: '60×88×23', mc: 0, plates: 1, price: 2.9, category: '3D модели FLEXI Toys', preview: '../assets/models/m7.png' },
      ] },
      mymodels: { ok: true, models: [
        { id: 1, label: 'Бэйбизавр', mc: 1, plates: 1, preview: '../assets/models/m5.png' },
        { id: 2, label: 'Устройство по перетасовке карт', mc: 0, plates: 5, preview: '../assets/models/m1.png' },
        { id: 3, label: 'Яйцо дракона', mc: 1, plates: 1, preview: '../assets/models/m4.png' },
        { id: 4, label: 'Octopus_v6_2C.3mf', mc: 0, plates: 1, preview: '../assets/models/m3.png' },
      ] },
      products: { ok: true, currency: 'руб.', products: [
        { id: 1, title: 'Ваза-спираль (готовая)', description: 'Напечатана шёлковым PLA, высота 18 см. В наличии, забрать сегодня.', price: 25, stock: 3, in_stock: true, sold: 7, photo: '../assets/models/m5.png' },
        { id: 2, title: 'Подставка под телефон', description: 'PETG, регулируемый угол. Прочная, матовая.', price: 12.5, stock: 1, in_stock: true, sold: 21, photo: '../assets/models/m1.png' },
        { id: 3, title: 'Брелок-дракон', description: 'Мультицвет, 4 см.', price: 6, stock: 0, in_stock: false, sold: 40, photo: '../assets/models/m2.png' } ] },
      orders: { ok: true, orders: [
        { id: 15, label: 'Бэйбизавр', plastic: 'Crown PLA шёлковый трёхцветный', qty: 1, status: 'awaiting_payment',
          status_label: 'Ждёт оплаты', price: '3,10\u00A0руб.', pending: null, comment: '', dims: '60×88×23',
          mc: 1, plates: 1, grams: '17 г', time: '1 ч 40 мин', preview: '../assets/models/m5.png',
          needs_response: false, cancellable: true, repeatable: true,
          editable: false, quality_id: 2, strength_id: 1,   // «ждёт оплаты» уже не редактируется
          paid: false, payable: true,   // мастер выставил счёт — видна кнопка «Оплатить» (A1)
          qpos: 3, qtotal: 3, qeta: '04.06', unread: 0,
          breakdown: { total: '3,10 руб.', grams_total: '17.0', time_total: '1 ч 40 мин', qty: 1, extra_colors: 2, purge: '2.4', rows: [
            { label: 'Филамент', sub: '17.0 г', value: '0,85 руб.' },
            { label: 'Машинное время', sub: '1 ч 40 мин', value: '0,90 руб.' },
            { label: 'Буфер на брак', sub: '8%', value: '0,14 руб.' },
            { label: 'Подготовка', sub: 'фикс. на заказ', value: '0,50 руб.' },
            { label: 'Наценка', sub: '40%', value: '0,71 руб.' } ] },
          timeline: [['Новый', '02.06 11:30']] },
        { id: 13, label: 'Устройство по перетасовке карт', plastic: 'Elegoo PETG', qty: 1, status: 'in_progress',
          status_label: 'В работе', price: '15,50\u00A0руб.', pending: null, comment: '', dims: '771×464×117',
          mc: 0, plates: 5, grams: '127 г', time: '12 ч 15 мин', preview: '../assets/models/m1.png',
          needs_response: false, cancellable: true, repeatable: true, due: '10.06', printing: true,
          qpos: 1, qtotal: 3, qeta: '04.06', unread: 1,
          prints: [
            { jid: 41, timelapse: '/x1.mp4', photo: '../assets/models/photo_dino.jpg', label: 'plate_1.gcode', minutes: 148 },
            { jid: 42, timelapse: '/x2.mp4', photo: '../assets/models/m1.png', label: 'plate_2.gcode', minutes: 132 },
            { jid: 43, timelapse: '', photo: '../assets/models/m2.png', label: 'plate_3.gcode', minutes: 95 } ],
          timeline: [['Новый', '01.06 09:10'], ['В работе', '02.06 08:12']] },
        { id: 2, label: 'бэйбизавр.3mf', plastic: 'Crown PLA шёлковый трёхцветный', qty: 1, status: 'printed',
          status_label: 'Напечатано', price: '4,00\u00A0руб.', pending: null, comment: '', dims: '60×88×23',
          mc: 1, plates: 1, grams: '16 г', time: '1 ч 40 мин', preview: '../assets/models/m6.png',
          needs_response: false, cancellable: false, repeatable: true, unread: 0,
          paid: true, payable: false,   // оплачен онлайн — бейдж «Оплачено» (A1)
          timeline: [['Новый', '28.05 14:00'], ['В работе', '29.05 10:00'], ['Напечатано', '30.05 16:30']] },
        // заказ-корзина (B5): группа приходит одной записью { group:true, items:[…] } — рендерится GroupOrderCard
        // A2: доставка стала платной — итог группы = позиции + доставка, плюс трек-номер
        { group: true, gid: 'demo00000cart', count: 2, total: '14,20 руб.',
          items_total: '9,20 руб.', delivery_cost: '5,00 руб.', delivery_type: 'Курьером по городу',
          track: { carrier: 'cdek', carrier_name: 'СДЭК', number: '1234567890',
            url: 'https://www.cdek.ru/ru/tracking?order_id=1234567890', shipped_at: 1, delivered_at: 0 },
          preview: '../assets/models/m2.png', delivery_method: 'delivery', delivery_address: 'ул. Ленина, 5',
          comment: 'по возможности к пятнице',
          statuses: [{ status: 'new', label: 'Новый' }, { status: 'in_progress', label: 'В работе' }],
          items: [
            { id: 21, label: 'Брелок-дракон', plastic: 'Elegoo PLA', qty: 1, group: 'demo00000cart', status: 'new',
              status_label: 'Новый', price: '3,20 руб.', pending: null, comment: '', mc: 0, plates: 1,
              grams: '9 г', time: '48 мин', preview: '../assets/models/m2.png', needs_response: false,
              cancellable: true, repeatable: true, unread: 0, timeline: [['Новый', '03.06 10:00']] },
            { id: 22, label: 'Подставка', plastic: 'Elegoo PLA', qty: 1, group: 'demo00000cart', status: 'in_progress',
              status_label: 'В работе', price: '6,00 руб.', pending: null, comment: '', mc: 0, plates: 1,
              grams: '18 г', time: '1 ч 20 мин', preview: '../assets/models/m1.png', needs_response: false,
              cancellable: true, repeatable: true, due: '07.06', unread: 0, timeline: [['Новый', '03.06 10:00'], ['В работе', '04.06 09:00']] } ] },
      ] },
      messages: { ok: true, label: 'Бэйбизавр', messages: [
        { text: 'Здравствуйте! Принял ваш заказ #15 на печать Бэйбизавра 🦖', mine: false, at: '02.06 11:31' },
        { text: 'Печать шёлковым трёхцветным — переливы будут особенно красивыми.', mine: false, at: '02.06 11:31' },
        { text: 'Супер, спасибо! Когда примерно будет готово?', mine: true, at: '02.06 11:34' },
        { text: 'Вы 3-й в очереди, ориентир — к 4 июня. Напишу, как поставлю на стол.', mine: false, at: '02.06 11:36' },
      ] },
      // Поиск модели: мок-выдача в форме контракта /api/model_search (см. docs/model-search.md §3).
      // SearchHit = { source, ext_id, title, author, thumb, page_url, license, free }.
      // Превью переиспользуют локальные ассеты, чтобы статический preview работал без сети.
      search: { ok: true, sources: ['printables', 'thingiverse', 'makerworld'], hits: [
        { source: 'printables', ext_id: '1234567', title: 'Groot planter (self-watering)', author: 'PrintMaster',
          thumb: '../assets/models/m2.png', page_url: 'https://www.printables.com/model/1234567-groot-planter', license: 'CC-BY', free: true },
        { source: 'thingiverse', ext_id: '4503599', title: 'Articulated Dragon', author: 'cults_dan',
          thumb: '../assets/models/m4.png', page_url: 'https://www.thingiverse.com/thing:4503599', license: 'CC-BY-NC', free: true },
        { source: 'makerworld', ext_id: '88421', title: 'Phone Stand — adjustable', author: 'deskcraft',
          thumb: '../assets/models/m8.png', page_url: 'https://makerworld.com/en/models/88421', license: null, free: true },
        { source: 'printables', ext_id: '7654321', title: 'Octopus flexi v6', author: 'oct0lab',
          thumb: '../assets/models/m3.png', page_url: 'https://www.printables.com/model/7654321-octopus-flexi-v6', license: 'CC-BY-SA', free: true },
        { source: 'thingiverse', ext_id: '763622', title: 'Fidget Cube', author: 'maker_io',
          thumb: '../assets/models/m1.png', page_url: 'https://www.thingiverse.com/thing:763622', license: 'CC-BY', free: true },
        { source: 'printables', ext_id: '2468013', title: "Dragon's Egg lamp", author: 'lumen3d',
          thumb: '../assets/models/m7.png', page_url: 'https://www.printables.com/model/2468013-dragons-egg-lamp', license: 'CC-BY-ND', free: true },
        { source: 'makerworld', ext_id: '55012', title: 'Cable clip (pack of 6)', author: 'tidydesk',
          thumb: '../assets/models/m5.png', page_url: 'https://makerworld.com/en/models/55012', license: null, free: true },
        { source: 'thingiverse', ext_id: '3495390', title: 'Benchy — the classic boat', author: 'cre3d',
          thumb: '../assets/models/photo_dino.jpg', page_url: 'https://www.thingiverse.com/thing:3495390', license: 'CC-BY', free: true },
      ] },
    };

    // Мок калькулятора цены админки (тот же расклад строк, что у бэкенда; для статик-превью без бота).
    function mockPriceCalc(b) {
      function n(v) { return parseFloat(String(v).replace(',', '.')) || 0; }
      var grams = n(b.grams), mins = n(b.minutes), ppk = n(b.price_per_kg);
      if (b.colors && b.colors.length) {   // многоцвет: Σ грамм, средневзвешенная цена (как на бэке)
        var g = 0, cost = 0;
        b.colors.forEach(function (c) { var cg = n(c.grams); g += cg; cost += cg * n(c.price_per_kg); });
        grams = g; ppk = g ? cost / g : 0;
      }
      var disc = Math.min(100, Math.max(0, n(b.discount_pct))), qty = Math.max(1, parseInt(b.qty, 10) || 1);
      var RATE = 0.516, BUFFER = 8, MARKUP = 30, PREP = 1.0, MIN = 5.0;
      var filU = grams * (ppk / 1000), machU = (mins / 60) * RATE;
      var fil = filU * qty, mach = machU * qty, cons = fil + mach;
      var waste = cons * BUFFER / 100, setup = PREP;
      var sub = cons + waste + setup, marg = sub * MARKUP / 100, total = sub + marg;
      // Скидка — от ВСЕГО итога, включая филамент (как pricing.compute_price на бэке).
      var dAmt = total * disc / 100; total -= dAmt;
      if (total < MIN) total = MIN;
      var tk = Math.round(total * 100), rk = Math.ceil(tk / 10) * 10; mach += (rk / 100 - total); total = rk / 100;
      var rows = [
        { label: 'Филамент', sub: (grams * qty).toFixed(1) + ' г', value: fmt(fil) },
        { label: 'Машинное время', sub: human(mins * qty), value: fmt(mach) },
        { label: 'Буфер на брак', sub: BUFFER + '%', value: fmt(waste) },
        { label: 'Подготовка', sub: 'фикс. на заказ', value: fmt(setup) },
        { label: 'Наценка', sub: MARKUP + '%', value: fmt(marg) },
      ];
      if (disc) rows.push({ label: 'Скидка', sub: disc + '% на всё', value: '−' + fmt(dAmt), discount: true });
      return { ok: true, bd: { rows: rows, total: fmt(total), grams_total: (grams * qty).toFixed(1), time_total: human(mins * qty), qty: qty } };
    }

    function mockCalc(body) {
      var o = M.options;
      var pl = o.plastics.filter(function (p) { return p.id === body.plastic_id; })[0] || o.plastics[0];
      // пер-стольные пластики (70525c99): мок-цена = среднее цены/плотности эффективных пластиков столов
      if (body.plate_plastics && Object.keys(body.plate_plastics).length) {
        var nPl = (M.model && M.model.plates) || 1, selP = (body.plates && body.plates.length) ? body.plates : null, acc = [];
        for (var pi = 0; pi < nPl; pi++) {
          if (selP && selP.indexOf(pi) < 0) continue;
          var ppid = body.plate_plastics[pi] != null ? body.plate_plastics[pi] : body.plate_plastics[String(pi)];
          acc.push(ppid ? (o.plastics.filter(function (x) { return x.id === ppid; })[0] || pl) : pl);
        }
        if (acc.length) pl = Object.assign({}, pl, {
          _price: acc.reduce(function (s, x) { return s + x._price; }, 0) / acc.length,
          _density: acc.reduce(function (s, x) { return s + x._density; }, 0) / acc.length,
          name: acc.map(function (x) { return x.name; }).filter(function (v, k, a) { return a.indexOf(v) === k; }).join(' + ')
        });
      }
      var ql = o.qualities.filter(function (q) { return q.id === body.quality_id; })[0] || o.qualities[2];
      var st = o.strengths.filter(function (s) { return s.id === body.strength_id; })[0] || o.strengths[4];
      var qty = Math.max(1, body.qty || 1);
      var dens = pl._density / 1.24;
      var gramsU = 16.6 * dens * (0.9 + st.infill / 100 * 0.7);
      var minU = 100 * ql._tmult * (0.95 + st.infill / 100 * 0.5);
      // масштаб модели: мок-лимит стола 290% (256 мм / длинная сторона 88); расход ~кубом масштаба
      var scaleReq = Math.max(10, Math.min(1000, parseInt(body.scale, 10) || 100));
      var SCALE_MAX = 290;
      var scalePct = Math.min(scaleReq, SCALE_MAX);
      var sc = scalePct / 100;
      gramsU *= sc * sc * sc; minU *= sc * sc * sc;
      var RATE = 0.516, PREP = 1.0, BUFFER = 0.08, MARKUP = 0.30, DISC = 20;
      var filament = gramsU * (pl._price / 1000);
      var machine = (minU / 60) * RATE;
      var materials = (filament + machine) * qty;
      var buffer = materials * BUFFER;
      var sub = materials + buffer + PREP;
      var markup = sub * MARKUP;
      var before = sub + markup;
      var filTotal = filament * qty;
      var discount = before * (DISC / 100);          // скидка на ВСЁ, включая филамент (07451408)
      var total = before - discount;
      var lines = [
        ['Филамент', fmt(filTotal)],
        ['Машинное время', fmt(machine * qty)],
        ['Буфер на брак (8%)', fmt(buffer)],
        ['Подготовка', fmt(PREP)],
        ['Наценка (30%)', fmt(markup)],
        ['Скидка (20% на всё)', '−' + fmt(discount)],
      ];
      var promo = null, promoInvalid = false;
      if (body.promo) { if (body.promo === 'PRINT10') promo = { code: 'PRINT10', pct: 10 }; else promoInvalid = true; }
      // Срочность (B3): rush домножает итог (наценка после скидки) и добавляет строку разбивки + срок.
      var isRush = body.urgency === 'rush', RUSH_K = 1.5;
      if (isRush) { var surch = total * (RUSH_K - 1); total += surch; lines.push(['Срочность ×' + RUSH_K, '+' + fmt(surch)]); }
      // Голая база печати ДО услуг (B4-инстант): срочность уже в total, опции — ещё нет. Клиент
      // считает по ней ceil10(база + Σ услуг) при локальном тапе (паритет с payload.total_unrounded).
      var baseNoOpts = total;
      // Опции постобработки (B4): сумма выбранных услуг поверх цены + строки разбивки.
      // «За-детальные» (accee7a9) — ставка × детали × тираж, как pricing.apply_options.
      var MOCK_PARTS = 6;                              // деталей в мок-модели (result.parts)
      if (body.options && body.options.length) {
        (o.options || []).forEach(function (opt) {
          if (body.options.indexOf(opt.id) < 0) return;
          var eff = opt.per_part ? Math.round(opt.price * MOCK_PARTS * qty * 100) / 100 : opt.price;
          if (eff > 0) {
            var lbl = opt.name + ' (постобработка' +
              (opt.per_part ? ' · ' + opt.price.toFixed(2).replace('.', ',') + ' × ' + (MOCK_PARTS * qty) + ' дет.' : '') + ')';
            total += eff; lines.push([lbl, '+' + fmt(eff)]);
          }
        });
      }
      var res = {
        total: fmt(total), total_unrounded: baseNoOpts, parts: MOCK_PARTS,
        discount_pct: DISC, total_before: fmt(before),
        grams_unit: gramsU.toFixed(1), grams_total: (gramsU * qty).toFixed(1),
        time_unit: human(minU), time_total: human(minU * qty), purge: pl._multicolor ? '2.4' : '',
        extra_colors: (body.multicolor && pl._multicolor) ? 2 : 0, plates: 1, cached: false,
        lines: lines, cpreview: '../assets/models/m5.png',
        dims: Math.round(60 * sc) + '×' + Math.round(88 * sc) + '×' + Math.round(23 * sc),
        queue: { count: 2, minutes: 1320 }, eta: '04.06', warnings: [],
        scale_pct: scalePct,
        // B3: срок по времени печати заказа — «срочно» всегда завтра (+1), «обычно» короче 12 ч → +1,
        // иначе послезавтра (+2). Держим MOCK в паритете с db.promised_date.
        urgency: isRush ? 'rush' : 'normal',
        term: (function () {
          var days = isRush ? 1 : ((minU * qty) < 12 * 60 ? 1 : 2);
          var d = new Date(); d.setDate(d.getDate() + days);
          return ('0' + d.getDate()).slice(-2) + '.' + ('0' + (d.getMonth() + 1)).slice(-2);
        })(),
      };
      if (scalePct < scaleReq) { res.scale_clamped = true; res.scale_max_pct = SCALE_MAX; }
      // Печатопригодность (B2, DFM): держим MOCK в паритете с реальным API. По умолчанию 'ok'
      // (тихо), но если модель не влезла и масштаб зажали — показываем 'wont_fit' с проверкой bed,
      // чтобы плашка-предупреждение и рычаг «уменьшить до N%» были видны в браузерном превью.
      res.verdict = 'ok'; res.dfm = [];
      if (res.scale_clamped) {
        var longest = Math.max(60, 88, 23) * scaleReq / 100;
        res.verdict = 'wont_fit';
        res.dfm = [{ key: 'bed', title: 'Размещение на столе', status: 'bad',
          detail: 'Самая длинная сторона детали ' + Math.round(longest) + ' мм больше стола ~256 мм — деталь придётся уменьшить или разрезать.' }];
      }
      if (promo) res.promo = promo; if (promoInvalid) res.promo_invalid = true;
      return { ok: true, result: res };
    }

    // Бренд для MOCK-ответов (A3, white-label). Сервер уже вшил реальный бренд в разметку —
    // берём его оттуда, чтобы предпросмотр в настройках совпадал с тем, во что перекрашена
    // сама админка. Без сервера (статичный превью файлом) падаем на наш дефолт.
    function mockBrand() {
      var b = window.__BRAND__ || {};
      return { title: b.title || 'OpenPrintShop', tagline: b.tagline || 'расчёт 3D-печати',
        accent: b.accent || '#ff7a1a', logo: b.logo || '', stock: b.stock !== false,
        wordmark: b.wordmark || ['OPEN', 'PRINT', 'SHOP'] };
    }
    // Поля редактирования — это то, что мастер ВВЁЛ. Для стокового бренда они пусты.
    function mockBrandValues() {
      var b = mockBrand();
      return b.stock
        ? { brand_title: '', brand_tagline: '', brand_accent: '' }
        : { brand_title: b.title, brand_tagline: b.tagline, brand_accent: b.accent };
    }
    return { M: M, mockPriceCalc: mockPriceCalc, mockCalc: mockCalc,
      mockBrand: mockBrand, mockBrandValues: mockBrandValues };
  });
})();
