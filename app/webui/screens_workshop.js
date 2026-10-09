// screens_workshop.js — «Мастерская органайзеров»: проекты Gridfinity, спецификация
// деталей и частичный заказ партиями.
//
// Конфигуратор перестал быть одноразовым черновиком: ящик собирают неделями — померил,
// напечатал три лотка, поносил, переделал. Поэтому здесь два экрана:
//
//   WorkshopScreen   — список проектов (проект = физический ящик клиента);
//   GfxProjectScreen — сам проект: 3D-раскладка, спецификация, партии, ящик.
//
// 3D-движок (gridfinity_engine.js) живёт во вкладке «3D» и НЕ размонтируется при переходе
// на соседние вкладки: mount создаёт два контекста WebGL, и дёргать его туда-сюда — верный
// способ упереться в лимит браузера. Прячем через display:none, движок сам переразмерится.
(function () {
  var h = React.createElement, useState = React.useState, useEffect = React.useEffect,
    useRef = React.useRef;
  var API = window.API;

  // Палитра модулей — та же, что в движке (COLORS) и на сервере (_GF_COLOR_HEX).
  var COLORS = ['#ff7a1a', '#3d4a5e', '#3fb6c9', '#2dd47e', '#a98bff', '#d4b183'];
  var TYPES = [['drawer', 'Шуфлядка'], ['shelf', 'Полка'], ['tandem', 'Tandembox']];
  var SERIES = ['M', 'K', 'C', 'D'];
  var BUSY = { ordered: 1, in_progress: 1, awaiting_payment: 1, printed: 1 };

  function pillClass(p) {
    var s = p.live_state;
    if (s === 'printed') return 'done';
    if (BUSY[s]) return 'ord';
    if (s === 'hold') return 'hold';
    if (p.sel) return 'sel';
    return 'draft';
  }

  function money(v) { return API.fmt ? API.fmt(v) : (Math.round(v) + ' ₽'); }

  function hours(min) {
    min = Math.round(min || 0);
    if (min < 60) return min + ' мин';
    var hh = Math.floor(min / 60), mm = min % 60;
    return hh + ' ч' + (mm ? ' ' + mm + ' мин' : '');
  }

  function pct(v) { return String(Math.round((v || 0) * 100) / 100); }

  /** Плашка «печатаем стандартом — за это скидка». Одна на спецификацию и подтверждение.
   *
   * Текст стандарта и его процент приходят С СЕРВЕРА (`standard`), а не зашиты здесь:
   * пластик можно переименовать или снять с производства, и обещание обязано назвать то,
   * по чему реально посчитана цена. Личная скидка клиента показывается отдельным слагаемым —
   * она СКЛАДЫВАЕТСЯ со стандартной, и человек должен видеть, из чего вышел итог. */
  function stdNote(std, tot) {
    if (!std || !std.text) return null;
    var own = tot && tot.discount_pct ? Math.max(0, tot.discount_pct - (tot.std_pct || 0)) : 0;
    return h('div', { className: 'wstd' },
      h('b', null, 'Печатаем стандартом: ' + std.text),
      std.pct ? h('span', { className: 'p' },
        own > 0 ? ('−' + pct(std.pct) + '% за стандарт + ' + pct(own) + '% ваша скидка = −'
          + pct(std.pct + own) + '%')
          : ('−' + pct(std.pct) + '% к цене')) : null,
      // ПРО СКОРОСТЬ ЗДЕСЬ НИЧЕГО НЕ ОБЕЩАЕМ. В первой редакции стояло «сопло 0.6 печатает
      // вдвое быстрее»: цифра пришла из модели времени fastcalc, а настоящий слайс той же
      // геометрии показал обратное — лоток 2×2 это 64 мин при 0.4/0.2 против 67 мин при
      // 0.6/0.3. Выигрыш стандарта не в скорости, а в предсказуемости и одном потоке.
      h('span', { className: 'w' },
        'Пластик обкатан, настройки известны, детали идут общим потоком — поэтому дешевле. '
        + 'Нужен другой пластик или цвет — напишите мастеру в комментарии к заказу, '
        + 'он посчитает отдельно.'));
  }

  /** Монитор — векторный, а не эмодзи: эмодзи каждая платформа рисует по-своему, и
   *  на части телефонов он приезжает цветной картинкой поперёк остального интерфейса. */
  function MonitorIcon() {
    return h('svg', {
      width: 19, height: 19, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      strokeWidth: 1.7, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': 'true',
    },
      h('rect', { x: 2.5, y: 3.5, width: 19, height: 13, rx: 2 }),
      h('path', { d: 'M8.5 20.5h7' }), h('path', { d: 'M12 16.5v4' }));
  }

  /** Telegram Desktop и веб-версия умеют открыть внешнюю ссылку в браузере машины —
   *  ровно то, что нужно: нажал на компьютере, браузер открылся сам. С телефона так не
   *  выйдет физически (откроется браузер телефона), поэтому там остаётся ссылка в чате. */
  var DESKTOP_TG = { tdesktop: 1, macos: 1, web: 1, weba: 1, webk: 1 };

  function tgPlatform() {
    try { return String((window.Telegram.WebApp.platform || '')).toLowerCase(); }
    catch (e) { return ''; }
  }

  function openInBrowser(url) {
    try {
      var tg = window.Telegram && window.Telegram.WebApp;
      // openLink уводит НАРУЖУ мини-аппа: на десктопе это системный браузер, а не вкладка
      // внутри Telegram, — иначе редактор открылся бы в том же тесном окне
      if (tg && tg.openLink) { tg.openLink(url); return true; }
    } catch (e) {}
    try { return !!window.open(url, '_blank', 'noopener'); } catch (e) { return false; }
  }

  function hap(kind) {
    try {
      var f = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.HapticFeedback;
      if (!f) return;
      if (kind === 'error') f.notificationOccurred('error');
      else if (kind === 'ok') f.notificationOccurred('success');
      else f.impactOccurred('light');
    } catch (e) {}
  }

  // ── план ящика: тот же компактный формат, что рисует карточка проекта и карточка партии ──
  function WPlan(props) {
    var pv = props.preview || { cols: 1, rows: 1, mods: [] };
    var only = props.only;   // подсветить ТОЛЬКО эти mod_id (карточка партии), остальные — контуром
    return h('div', {
      className: 'wplan',
      style: { '--cols': pv.cols, '--rows': pv.rows, aspectRatio: pv.cols + '/' + pv.rows },
    }, (pv.mods || []).map(function (m, i) {
      if (m.wing) return null;   // пристенный модуль стоит вне сетки — в мини-плане его не рисуем
      var faded = only && only.indexOf(m.id) < 0;
      return h('i', {
        key: i,
        className: (m.o ? 'ord' : '') + (faded ? ' dim' : ''),
        style: {
          gridColumn: (m.x + 1) + '/span ' + m.w,
          gridRow: (m.y + 1) + '/span ' + m.d,
          background: COLORS[m.c % COLORS.length],
        },
      });
    }));
  }

  function Sheet(props) {
    return h('div', {
      className: 'wsh-ov',
      onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); },
    }, h('div', { className: 'wsh' },
      h('div', { className: 'wsh-h' },
        h('div', { className: 't' }, props.title),
        props.right,
        h('button', { className: 'wsh-x', type: 'button', onClick: props.onClose }, '✕')),
      props.children));
  }

  // ════════════════════ экран 1: список проектов ════════════════════

  window.WorkshopScreen = function (props) {
    var app = props.app;
    var st = useState(null); var data = st[0], setData = st[1];
    var ar = useState(false); var arch = ar[0], setArch = ar[1];
    var nw = useState(false); var creating = nw[0], setCreating = nw[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];

    var er = useState(''); var err = er[0], setErr = er[1];
    function load(a) {
      setErr('');
      API.gfxProjects(a).then(function (j) {
        if (j && j.ok) { setData(j); return; }
        // «Тут пока пусто» вместо оборвавшейся сети — это «бот потерял мои проекты»
        setData(null); setErr((j && j.error) || 'Не удалось загрузить проекты');
      }, function () { setData(null); setErr('Нет сети — проекты не загрузились'); });
    }
    useEffect(function () { load(arch); }, [arch]);

    function create(body) {
      if (busy) return;
      setBusy(true);
      API.gfxProjectAdd(body).then(function (j) {
        setBusy(false);
        if (!j || !j.ok) { app.toast((j && j.error) || 'Не удалось создать проект'); return; }
        setCreating(false);
        hap('ok');
        app.openGfxProject(j.id);
      }, function () { setBusy(false); app.toast('Сеть недоступна'); });
    }

    var list = (data && data.projects) || [];
    return h('div', { className: 'screen wlim' },
      h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 } },
        h('div', { className: 'seg', style: { flex: 1 } },
          h('button', { className: arch ? '' : 'on', type: 'button', onClick: function () { setArch(false); } },
            'Активные' + (data && data.active != null ? ' · ' + data.active : '')),
          h('button', { className: arch ? 'on' : '', type: 'button', onClick: function () { setArch(true); } },
            'Архив' + (data && data.archived_n != null ? ' · ' + data.archived_n : ''))),
        h('button', {
          className: 'btn', style: { width: 'auto', margin: 0, padding: '11px 14px', flex: '0 0 auto' },
          type: 'button', onClick: function () { setCreating(true); },
        }, '+ Проект')),

      !data && !err && h('div', { className: 'hint', style: { textAlign: 'center', padding: '32px 0' } },
        'Загружаю проекты…'),

      err && h('div', { className: 'empty' },
        h('div', { style: { fontSize: 15, color: 'var(--muted)', marginBottom: 6 } }, err),
        h('div', null, 'Проекты никуда не делись — не доехал ответ сервера.'),
        h('button', {
          className: 'btn', style: { marginTop: 16 }, type: 'button',
          onClick: function () { load(arch); },
        }, 'Повторить')),

      data && !list.length && h('div', { className: 'empty' },
        h('div', { style: { fontSize: 15, color: 'var(--muted)', marginBottom: 6 } },
          arch ? 'Архив пуст' : 'Тут пока пусто'),
        h('div', null, arch ? 'Сюда попадают проекты, из которых уже что-то заказывали.'
          : 'Проект — это ваш ящик. Померьте его изнутри, соберите раскладку и заказывайте по частям: сначала пару лотков на пробу, остальное — когда убедитесь, что село как надо.'),
        !arch && h('button', {
          className: 'btn', style: { marginTop: 16 }, type: 'button',
          onClick: function () { setCreating(true); },
        }, 'Создать первый проект')),

      list.map(function (p) {
        return h('button', {
          key: p.id, className: 'wproj', type: 'button',
          onClick: function () { app.openGfxProject(p.id); },
        },
          h(WPlan, { preview: p.preview }),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'wt' }, p.title),
            h('div', { className: 'ws' },
              p.preview.W + ' × ' + p.preview.D + ' × ' + p.preview.H + ' мм · '
              + p.counts.total + ' дет.'),
            h('div', { className: 'wbars' },
              p.counts.printed ? h('i', { style: { flex: p.counts.printed, background: 'var(--price)' } }) : null,
              p.counts.ordered ? h('i', { style: { flex: p.counts.ordered, background: '#3fb6c9' } }) : null,
              (p.counts.draft + p.counts.hold) ? h('i', {
                style: { flex: p.counts.draft + p.counts.hold, background: 'var(--surface-2)' },
              }) : null,
              !p.counts.total ? h('i', { style: { flex: 1, background: 'var(--surface-2)' } }) : null),
            h('div', { className: 'ws', style: { marginTop: 5 } },
              [p.counts.printed ? p.counts.printed + ' напечатано' : '',
                p.counts.ordered ? p.counts.ordered + ' в работе' : '',
                p.counts.draft ? p.counts.draft + ' черновик' : '',
                p.counts.hold ? p.counts.hold + ' отложено' : ''].filter(Boolean).join(' · ')
              || 'пусто')));
      }),

      creating && h(NewProject, {
        app: app, busy: busy, measures: (data && data.measures) || [],
        onClose: function () { setCreating(false); }, onCreate: create,
      }));
  };

  // ── шторка «новый проект»: тип → замеры → имя ──
  function NewProject(props) {
    var t = useState('drawer'); var type = t[0], setType = t[1];
    var w = useState(480); var W = w[0], setW = w[1];
    var d = useState(270); var D = d[0], setD = d[1];
    var hh = useState(120); var H = hh[0], setH = hh[1];
    var sr = useState('M'); var series = sr[0], setSeries = sr[1];
    var nm = useState(''); var title = nm[0], setTitle = nm[1];
    var sv = useState(false); var saveMeas = sv[0], setSaveMeas = sv[1];

    function num(v, d0) { var x = parseFloat(String(v).replace(',', '.')); return isFinite(x) ? x : d0; }
    function apply(m) {
      setType(m.btype || 'drawer'); setW(m.w); setD(m.d); setH(m.h);
      if (m.series) setSeries(m.series);
      if (!title) setTitle(m.title);
    }
    function submit() {
      if (saveMeas) {
        API.gfxMeasures({ title: title || 'Ящик', type: type, W: W, D: D, H: H, series: series })
          .catch(function () {});
      }
      props.onCreate({
        title: title,
        config: { v: 4, seq: 1, modules: [],
          base: { type: type, W: num(W, 480), D: num(D, 270), H: num(H, 120),
            series: series, fitX: 'sym', fitY: 'sym' } },
      });
    }
    var cols = Math.max(1, Math.floor(num(W, 0) / 42)), rows = Math.max(1, Math.floor(num(D, 0) / 42));

    return h(Sheet, { title: 'Новый проект', onClose: props.onClose },
      h('div', { className: 'wchips' }, TYPES.map(function (x) {
        return h('button', {
          key: x[0], type: 'button', className: 'chip' + (type === x[0] ? ' on' : ''),
          onClick: function () { setType(x[0]); },
        }, x[1]);
      })),
      h('div', { className: 'hint', style: { marginTop: 2 } },
        type === 'tandem'
          ? 'Мерьте по дну ящика между царгами. Серия задаёт высоту боковины.'
          : 'Мерьте ВНУТРИ ящика по дну, а не по фасаду: фасад шире, и раскладка не сойдётся.'),

      type === 'tandem' && h('div', { className: 'wchips' }, SERIES.map(function (x) {
        return h('button', {
          key: x, type: 'button', className: 'chip' + (series === x ? ' on' : ''),
          onClick: function () { setSeries(x); },
        }, 'Серия ' + x);
      })),

      h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 } },
        [['Ширина, мм', W, setW], ['Глубина, мм', D, setD], ['Высота, мм', H, setH]].map(function (f, i) {
          return h('label', { key: i, className: 'wfield' }, h('span', null, f[0]),
            h('input', {
              type: 'number', inputMode: 'decimal', value: f[1],
              onChange: function (e) { f[2](e.target.value); },
            }));
        })),
      h('div', { className: 'hint' }, 'Сетка получится ' + cols + '×' + rows
        + ' (шаг 42 мм), остаток по краям уйдёт в доборы крайних модулей.'),

      h('label', { className: 'wfield' }, h('span', null, 'Название'),
        h('input', {
          type: 'text', maxLength: 80, placeholder: 'Кухня, нижний ящик', value: title,
          onChange: function (e) { setTitle(e.target.value); },
        })),

      !!props.measures.length && h('div', null,
        h('div', { className: 'hint', style: { marginTop: 12 } }, 'Мои мерки:'),
        h('div', { className: 'wchips' }, props.measures.map(function (m) {
          return h('button', {
            key: m.id, type: 'button', className: 'chip',
            onClick: function () { apply(m); },
          }, m.title + ' · ' + Math.round(m.w) + '×' + Math.round(m.d));
        }))),

      h('label', {
        className: 'wchk', style: { marginTop: 12, cursor: 'pointer' },
        onClick: function () { setSaveMeas(!saveMeas); },
      },
        h('span', { className: 'wcbx' + (saveMeas ? ' on' : '') }, '✓'),
        h('span', null, 'Запомнить эти замеры — следующий такой же ящик заведётся в два тапа')),

      h('button', {
        className: 'btn lg', style: { marginTop: 14 }, type: 'button',
        disabled: props.busy, onClick: submit,
      }, props.busy ? 'Создаю…' : 'Создать проект'));
  }

  // ════════════════════ экран 2: проект ════════════════════

  window.GfxProjectScreen = function (props) {
    var app = props.app;
    var pid = app.gfxProjectId;
    var st = useState(null); var data = st[0], setData = st[1];
    var tb = useState('3d'); var tab = tb[0], setTab = tb[1];
    var sp = useState(null); var sheet = sp[0], setSheet = sp[1];
    var od = useState(false); var ordering = od[0], setOrdering = od[1];
    var er = useState(''); var err = er[0], setErr = er[1];
    var dk = useState(false); var deskOpen = dk[0], setDeskOpen = dk[1];
    var db_ = useState(false); var deskBusy = db_[0], setDeskBusy = db_[1];
    var host = useRef(null), inst = useRef(null), latest = useRef(null), saveToasted = useRef(false);

    latest.current = data;

    function load(quiet) {
      return API.gfxProject(pid).then(function (j) {
        if (!j || !j.ok) { setErr((j && j.error) || 'Проект не найден'); return null; }
        setData(j);
        if (inst.current) inst.current.setParts(j.parts);
        return j;
      }, function () { if (!quiet) setErr('Сеть недоступна'); return null; });
    }
    useEffect(function () { load(); }, [pid]);

    // ── движок: монтируем ОДИН раз на проект, дальше только подкладываем спецификацию ──
    useEffect(function () {
      if (!data || inst.current || !host.current) return undefined;
      var alive = true;
      window.GFStudio_ensure().then(function () {
        if (!alive || !host.current || inst.current) return;
        inst.current = window.GFStudio.mount(host.current, {
          app: app,
          config: data.config || null,
          parts: data.parts,
          // локальная копия — СВОЯ на каждый проект: общий ключ подсунул бы в новый
          // проект раскладку соседнего (она свежее серверной) и затем сохранил её туда
          lsKey: 'gf-studio-v4:p' + pid,
          save: function (cfg) {
            API.gfxProjectSave(pid, cfg).then(function (j) {
              if (!j || !j.ok) {
                if (!saveToasted.current) {
                  saveToasted.current = true;
                  app.toast((j && j.error) || 'Раскладка не сохранилась на сервере');
                }
                return;
              }
              var d = j.diff || {};
              // сверка спецификации что-то изменила — перечитываем: клиент обязан увидеть,
              // что деталь форкнулась в v2, а не узнать об этом от мастера
              if (d.added || d.dropped || d.forked || d.changed) {
                load(true).then(function () {
                  if (d.forked) app.toast('Деталь уже печаталась — сделали новую версию (v2)');
                });
              }
            }, function () {
              if (!saveToasted.current) {
                saveToasted.current = true;
                app.toast('Нет сети — правки живут на этом устройстве');
              }
            });
          },
          onSpec: function () { setTab('spec'); },
        });
      }).catch(function () { setErr('Не удалось загрузить 3D-движок'); });
      return function () { alive = false; };
    }, [!!data]);

    // размонтирование движка — только при уходе с экрана проекта
    useEffect(function () {
      return function () {
        try { if (inst.current) { inst.current.destroy(); inst.current = null; } } catch (e) {}
      };
    }, []);

    // База и раскладка живут в движке, а серверная копия догоняет их дебаунсом сохранения.
    // Вкладки «Ящик» и «Партии» обязаны показывать ЖИВОЕ состояние: иначе клиент поменял
    // глубину ящика, ушёл на соседнюю вкладку — и читает прежние цифры.
    function cfgNow() {
      try { if (inst.current) return inst.current.snapshot(); } catch (e) {}
      return (data && data.config) || {};
    }

    // Заготовки: удачная деталь переносится в другой проект. Модуль берём из ЖИВОЙ
    // раскладки по mod_id — в детали спецификации геометрии нет, она там и не нужна.
    function duplicate() {
      API.gfxProjectAdd({ from_id: pid }).then(function (j) {
        if (j && j.ok) { hap('ok'); app.openGfxProject(j.id); }
        else app.toast((j && j.error) || 'Не удалось скопировать');
      }, function () { app.toast('Сеть недоступна'); });
    }

    function savePreset(part) {
      var mods = (cfgNow().modules || []).filter(function (m) { return m.id === part.mod_id; });
      if (!mods.length) { app.toast('Эта деталь снята с раскладки — сохранять нечего'); return; }
      API.gfxPresetSave(part.name, mods[0]).then(function (j) {
        if (j && j.ok) { hap('ok'); app.toast('Заготовка сохранена — вставится в любой проект'); }
        else app.toast((j && j.error) || 'Не удалось сохранить');
      }, function () { app.toast('Сеть недоступна'); });
    }
    function insertPreset(preset) {
      var mod;
      try { mod = JSON.parse(preset.mod_json); } catch (e) { mod = null; }
      if (!mod || !inst.current) { app.toast('Заготовка не читается'); return; }
      if (!inst.current.addFromTemplate(mod)) { hap('error'); app.toast('В ящике нет свободного места'); return; }
      hap('ok');
      setTab('3d');
      app.toast('Вставили — перетащите на своё место');
    }

    /** Выпустить или погасить ссылку «продолжить на компьютере».
     *
     * Саму ссылку сервер отправляет клиенту В ЧАТ — так она сразу на всех его устройствах,
     * и на ПК открывается одним кликом из Telegram Desktop. Здесь её тоже показываем: без
     * этого пришлось бы уходить в чат ради адреса, который только что запросили. */
    function deskLink(on) {
      if (deskBusy) return;
      setDeskBusy(true);
      API.gfxProjectPatch(pid, { desk: on ? 1 : 0 }).then(function (j) {
        setDeskBusy(false);
        if (!j || !j.ok) { app.toast((j && j.error) || 'Не получилось'); return; }
        setData(Object.assign({}, latest.current, { desk: j.desk || {} }));
        if (!on) { setDeskOpen(false); app.toast('Доступ с компьютера закрыт'); return; }
        hap('ok');
        // На компьютере не заставляем идти в чат за ссылкой, которую только что попросили:
        // открываем редактор сразу. В чат она всё равно уходит — чтобы вернуться к ней
        // завтра с другого устройства.
        var url = (j.desk || {}).url;
        if (url && DESKTOP_TG[tgPlatform()] && openInBrowser(url)) {
          app.toast('Открываю в браузере — ссылка продублирована в чат');
        } else {
          app.toast('Ссылка отправлена вам в чат');
        }
      }, function () { setDeskBusy(false); app.toast('Сеть недоступна'); });
    }

    function openDesk() {
      setDeskOpen(true);
      if (!(latest.current && latest.current.desk && latest.current.desk.url)) deskLink(true);
    }

    function patchPart(partId, body) {
      return API.gfxPartPatch(partId, body).then(function (j) {
        if (!j || !j.ok) { app.toast((j && j.error) || 'Не удалось изменить деталь'); return null; }
        var next = Object.assign({}, latest.current, { parts: j.parts, counts: j.counts });
        setData(next);
        if (inst.current) inst.current.setParts(j.parts);
        return j;
      }, function () { app.toast('Сеть недоступна'); return null; });
    }

    function selectMany(ids, sel) {
      if (!ids.length) return;
      hap();
      API.gfxSelect(pid, ids, sel).then(function (j) {
        if (!j || !j.ok) return;
        var next = Object.assign({}, latest.current, { parts: j.parts, counts: j.counts });
        setData(next);
        if (inst.current) inst.current.setParts(j.parts);
      });
    }

    var parts = (data && data.parts) || [];
    var selIds = parts.filter(function (p) { return p.sel; }).map(function (p) { return p.id; });
    var TABS = [['3d', '3D'], ['spec', 'Спецификация'], ['batches', 'Партии'], ['box', 'Ящик']];

    return h('div', { className: 'screen' },
      // Ошибка рисуется БАННЕРОМ, а не вместо экрана: ранний return выносил из DOM хост
      // движка, не уничтожая его, и после «Повторить» вкладка 3D оставалась пустой
      // навсегда — вместе с двумя живыми контекстами WebGL при лимите браузера ~16.
      err && h('div', { className: 'wchk block', style: { marginBottom: 10 } },
        h('span', null, err),
        h('button', {
          className: 'chip', style: { marginLeft: 'auto' }, type: 'button',
          onClick: function () { setErr(''); load(); },
        }, 'Повторить')),
      // Шапка приложения на этом экране скрыта ради высоты сцены (app.js, класс bare),
      // поэтому выход из проекта живёт здесь. Без него из мастерской не выйти вовсе:
      // нижний таб-бар приложения закрыт собственным баром студии («База/Модули/Деталь»).
      h('div', { className: 'wtabrow' },
        !app.deskMode && h('button', {
          className: 'wback', type: 'button', 'aria-label': 'К проектам',
          onClick: function () { app.goTab('workshop'); },
        }, '‹'),
        h('div', { className: 'seg', style: { flex: 1, minWidth: 0 } }, TABS.map(function (t) {
        var n = t[0] === 'spec' ? parts.length : t[0] === 'batches' ? ((data && data.batches) || []).length : 0;
        return h('button', {
          key: t[0], type: 'button', className: tab === t[0] ? 'on' : '',
          onClick: function () { setTab(t[0]); },
        }, t[1], n ? h('small', { style: { opacity: .6, marginLeft: 4 } }, n) : null);
      })),
        // Кнопка «продолжить на компьютере». В самой ПК-сессии её нет: мы уже там, а вторая
        // ссылка из первой означала бы, что утёкшая продлевает себя сама.
        !app.deskMode && h('button', {
          className: 'wback wmon' + (data && data.desk && data.desk.url ? ' on' : ''),
          type: 'button', 'aria-label': 'Открыть на компьютере',
          title: 'Открыть на компьютере', onClick: openDesk,
        }, h(MonitorIcon, null))),

      // сцена живёт всегда — снимать её при переключении вкладки значит пересоздавать WebGL
      h('div', { style: { display: tab === '3d' ? 'block' : 'none' } },
        !data && h('div', { className: 'hint', style: { textAlign: 'center', padding: '32px 0' } },
          'Загружаю раскладку…'),
        h('div', { ref: host })),

      tab === 'spec' && h('div', { className: 'wlim' }, h(SpecTab, {
        app: app, data: data, onOpen: setSheet, onSelect: selectMany,
        onOrder: function () { setOrdering(true); },
      })),
      tab === 'batches' && h('div', { className: 'wlim' },
        h(BatchesTab, { app: app, data: data, cfg: cfgNow() })),
      tab === 'box' && h('div', { className: 'wlim' }, h(BoxTab, {
        app: app, pid: pid, data: data, cfg: cfgNow(),
        onChanged: function () { load(); }, onInsert: insertPreset,
        onConfig: function (cfg) { if (inst.current) inst.current.setConfig(cfg); },
        onDuplicate: duplicate,
      })),

      sheet && h(PartSheet, {
        app: app, part: (parts.filter(function (p) { return p.id === sheet; })[0] || null),
        parts: parts, onClose: function () { setSheet(null); },
        onPatch: patchPart, onPreset: savePreset,
      }),

      deskOpen && h(Sheet, {
        title: 'Открыть на компьютере', onClose: function () { setDeskOpen(false); },
      },
        h('div', { className: 'hint', style: { marginTop: 4, lineHeight: 1.6 } },
          'Ссылка открывает ЭТОТ проект в обычном браузере — без Telegram и без входа. '
          + 'Мы отправили её вам в чат: на компьютере проще открыть её оттуда, чем '
          + 'переписывать руками.'),
        (data && data.desk && data.desk.url)
          ? h('div', null,
            h('div', {
              className: 'hint',
              style: { wordBreak: 'break-all', userSelect: 'all', marginTop: 10,
                padding: '10px 12px', border: '1px solid var(--line)', borderRadius: 12,
                background: 'var(--surface)', color: 'var(--text)', fontFamily: 'var(--mono)' },
            }, data.desk.url),
            h('button', {
              className: 'btn', style: { marginTop: 12 }, type: 'button', disabled: deskBusy,
              onClick: function () {
                if (!openInBrowser(data.desk.url)) app.toast('Скопируйте адрес вручную');
              },
            }, 'Открыть в браузере'),
            h('button', {
              className: 'btn ghost', style: { marginTop: 8 }, type: 'button', disabled: deskBusy,
              onClick: function () {
                // navigator.clipboard в Telegram WebView есть не всегда — тогда просто
                // оставляем адрес на экране: он выделяется одним тапом (user-select:all).
                try {
                  navigator.clipboard.writeText(data.desk.url).then(function () {
                    hap('ok'); app.toast('Ссылка скопирована');
                  }, function () { app.toast('Скопируйте адрес вручную'); });
                } catch (e) { app.toast('Скопируйте адрес вручную'); }
              },
            }, 'Скопировать ссылку'),
            h('button', {
              className: 'btn ghost', style: { marginTop: 8 }, type: 'button', disabled: deskBusy,
              onClick: function () { deskLink(true); },
            }, 'Отправить в чат ещё раз'),
            h('button', {
              className: 'btn ghost warn', style: { marginTop: 8 }, type: 'button',
              disabled: deskBusy,
              onClick: function () { deskLink(false); },
            }, 'Погасить доступ'),
            h('div', { className: 'hint', style: { marginTop: 10, lineHeight: 1.6 } },
              'Ссылка и есть ключ: у кого она есть, тот правит раскладку и может оформить '
              + 'заявку. Не публикуйте её. Срок — '
              + (data.desk.expires_at
                ? Math.max(1, Math.ceil((data.desk.expires_at * 1000 - Date.now()) / 86400000)) + ' дн.'
                : 'неделя')
              + '; удалить проект или раздать другие ссылки с компьютера нельзя.'))
          : h('div', { className: 'hint', style: { marginTop: 10 } },
            deskBusy ? 'Готовлю ссылку…' : 'Ссылка не выпущена.')),
      ordering && h(OrderSheet, {
        app: app, pid: pid, ids: selIds,
        onClose: function () { setOrdering(false); },
        onDone: function (rid, n) {
          setOrdering(false);
          hap('ok');
          app.toast('Заявка #' + rid + ' на ' + n + ' дет. отправлена мастеру');
          if (app.reloadOrders) app.reloadOrders();
          load();
        },
      }));
  };

  // ── вкладка «Спецификация» ──
  function SpecTab(props) {
    var data = props.data;
    var fl = useState('all'); var filt = fl[0], setFilt = fl[1];
    if (!data) return h('div', { className: 'hint' }, 'Загружаю…');
    var parts = data.parts || [];
    if (!parts.length) {
      return h('div', { className: 'empty' },
        h('div', { style: { fontSize: 15, color: 'var(--muted)', marginBottom: 6 } }, 'Деталей пока нет'),
        h('div', null, 'Соберите раскладку на вкладке «3D» — каждый модуль станет строкой спецификации, и только отсюда детали уезжают в заказ.'));
    }
    var FILTERS = [['all', 'Все'], ['draft', 'Черновик'], ['busy', 'В работе'], ['printed', 'Напечатано']];
    function match(p) {
      if (filt === 'all') return true;
      if (filt === 'printed') return p.live_state === 'printed';
      if (filt === 'busy') return BUSY[p.live_state] && p.live_state !== 'printed';
      return !BUSY[p.live_state];
    }
    var shown = parts.filter(match);
    var free = parts.filter(function (p) { return !BUSY[p.live_state] && p.live_state !== 'hold'; });
    var sel = parts.filter(function (p) { return p.sel; });
    // Итог считает СЕРВЕР одним расчётом. Позаказных статей у gridfinity больше нет
    // (01.09.2026), поэтому итог сходится с суммой строк — но считать его всё равно серверу:
    // скидка зависит от тиража партии, а тираж меняется на каждый чек-бокс.
    var tot = data.money || { price: 0, minutes: 0, qty: 0, n: 0 };

    return h('div', null,
      h('div', { className: 'wchips' }, FILTERS.map(function (f) {
        var n = parts.filter(function (p) {
          return f[0] === 'all' ? true : f[0] === 'printed' ? p.live_state === 'printed'
            : f[0] === 'busy' ? (BUSY[p.live_state] && p.live_state !== 'printed')
              : !BUSY[p.live_state];
        }).length;
        return h('button', {
          key: f[0], type: 'button', className: 'chip' + (filt === f[0] ? ' on' : ''),
          onClick: function () { setFilt(f[0]); },
        }, f[1], h('small', null, ' ' + n));
      })),
      !!free.length && h('div', { className: 'wchips', style: { marginTop: 2 } },
        h('button', {
          type: 'button', className: 'chip assist',
          onClick: function () {
            props.onSelect(free.map(function (p) { return p.id; }), sel.length !== free.length);
          },
        }, sel.length === free.length ? 'Снять всё' : 'Выбрать всё незаказанное'),
        h('button', {
          type: 'button', className: 'chip assist',
          onClick: function () {
            var c = free.length ? free[0].color_idx : 0;
            props.onSelect(free.filter(function (p) { return p.color_idx === c; })
              .map(function (p) { return p.id; }), true);
          },
        }, 'По цвету первой')),

      shown.map(function (p) {
        var busy = BUSY[p.live_state];
        return h('div', {
          key: p.id, className: 'wrow' + (p.sel ? ' sel' : '') + (busy ? ' mut' : ''),
        },
          h('div', {
            className: 'wcbx' + (p.sel ? ' on' : '') + (busy ? ' lock' : ''),
            role: 'button', 'aria-label': 'В партию',
            onClick: function () { if (!busy) props.onSelect([p.id], !p.sel); },
          }, '✓'),
          h('div', {
            className: 'wthumb' + (busy ? ' ord' : ''),
            style: { background: p.color_hex, cursor: 'pointer' },
            onClick: function () { props.onOpen(p.id); },
          }),
          h('div', { style: { minWidth: 0, cursor: 'pointer' }, onClick: function () { props.onOpen(p.id); } },
            h('div', { className: 'wn' }, p.name,
              p.qty > 1 ? h('span', { style: { color: 'var(--muted)', fontWeight: 400 } }, ' ×' + p.qty) : null,
              p.version > 1 ? h('span', { style: { color: 'var(--accent)', fontWeight: 400 } }, ' v' + p.version) : null),
            h('div', { className: 'wd' },
              (p.cells ? p.cells + ' яч · ' : '')
              + Math.round(p.w_mm) + '×' + Math.round(p.d_mm) + '×' + Math.round(p.h_mm) + ' мм · '
              + p.units + 'u · ' + p.gen_label
              + (p.request_id ? ' · заявка #' + p.request_id : '')
              + (p.on_plan ? '' : ' · снята с раскладки')),
            p.note ? h('div', { className: 'wd', style: { color: 'var(--muted)' } }, 'кладут: ' + p.note) : null),
          h('div', { className: 'wr' },
            h('span', { className: 'wpill ' + pillClass(p) },
              p.sel && !busy ? 'в партии' : p.state_label),
            h('span', { className: 'wmoney' }, '≈ ' + money(p.price))));
      }),

      stdNote(data.standard, tot),

      h('div', { className: 'wtot' },
        h('div', { className: 'l' },
          sel.length ? ('В партии ' + sel.length + ' дет. · ' + tot.qty + ' шт') : 'Отметьте, что печатаем сейчас',
          h('b', null, sel.length ? ('≈ ' + money(tot.price) + ' · ' + hours(tot.minutes)) : '—')),
        h('button', {
          className: 'btn', type: 'button', disabled: !sel.length, onClick: props.onOrder,
        }, 'Оформить')),
      h('div', { className: 'hint' },
        'Цену подтвердит мастер — оценка считается по объёму модели, без слайсинга.'));
  }

  // ── вкладка «Партии» ──
  function BatchesTab(props) {
    var data = props.data;
    if (!data) return h('div', { className: 'hint' }, 'Загружаю…');
    var bs = data.batches || [];
    if (!bs.length) {
      return h('div', { className: 'empty' },
        h('div', { style: { fontSize: 15, color: 'var(--muted)', marginBottom: 6 } }, 'Заказов по проекту ещё не было'),
        h('div', null, 'Отметьте детали в спецификации и оформите первую партию — сюда попадёт её история.'));
    }
    return h('div', null, bs.map(function (b, i) {
      return h('div', { key: b.request_id, className: 'wproj', style: { cursor: 'default' } },
        h(WPlan, { preview: dataPreview(data, props.cfg), only: b.mod_ids }),
        h('div', { style: { minWidth: 0 } },
          h('div', { className: 'wt' }, 'Партия ' + (i + 1) + ' · заявка #' + b.request_id),
          h('div', { className: 'ws' },
            b.n + ' дет. · ' + b.qty + ' шт · '
            + (b.price ? money(b.price) : '≈ ' + money(b.est))),
          h('div', { style: { marginTop: 7, display: 'flex', gap: 6, flexWrap: 'wrap' } },
            h('span', { className: 'wpill ' + (b.printed === b.n ? 'done' : 'ord') },
              b.status_label || b.status || 'в работе'),
            b.printed ? h('span', { className: 'wpill done' }, b.printed + ' из ' + b.n + ' готово') : null)));
    }));
  }

  // план проекта для карточек партий строим из спецификации: сервер отдаёт превью только
  // в списке проектов, а здесь у нас уже есть и раскладка, и детали
  function dataPreview(data, live) {
    var cfg = live && live.base ? live : (data.config || {}), base = cfg.base || {};
    var cols = Math.max(1, Math.floor((base.W || 0) / 42)), rows = Math.max(1, Math.floor((base.D || 0) / 42));
    var ordered = {};
    (data.parts || []).forEach(function (p) { if (BUSY[p.live_state] && p.mod_id) ordered[p.mod_id] = 1; });
    return {
      cols: cols, rows: rows,
      mods: (cfg.modules || []).filter(function (m) { return m.kind !== 'wing'; }).map(function (m) {
        return { id: m.id, x: m.cx || 0, y: m.cy || 0, w: m.w || 1, d: m.d || 1,
          c: m.colorIdx || 0, o: ordered[m.id] ? 1 : 0 };
      }),
    };
  }

  // ── вкладка «Ящик»: замеры, шаблон, ссылка, версии, архив ──
  function BoxTab(props) {
    var app = props.app, data = props.data, pid = props.pid;
    var nm = useState(null); var title = nm[0], setTitle = nm[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];
    if (!data) return h('div', { className: 'hint' }, 'Загружаю…');
    var base = (props.cfg && props.cfg.base) || (data.config || {}).base || {};
    var g = { cols: Math.max(1, Math.floor((base.W || 0) / 42)), rows: Math.max(1, Math.floor((base.D || 0) / 42)) };

    function patch(body, msg) {
      if (busy) return;
      setBusy(true);
      API.gfxProjectPatch(pid, body).then(function (j) {
        setBusy(false);
        if (!j || !j.ok) { app.toast((j && j.error) || 'Не получилось'); return; }
        // откат к снимку обязан доехать до 3D: иначе тост зелёный, сцена прежняя, а через
        // полторы секунды автосохранение вернёт всё как было
        if (j.config && props.onConfig) props.onConfig(j.config);
        if (j.note) app.toast(j.note);
        else if (msg) app.toast(msg);
        props.onChanged();
      }, function () { setBusy(false); app.toast('Сеть недоступна'); });
    }

    return h('div', null,
      h('div', { className: 'card' },
        h('div', { style: { fontSize: 12, color: 'var(--muted)' } }, 'Ящик'),
        h('div', { style: { fontFamily: 'var(--mono)', fontSize: 13, marginTop: 6, lineHeight: 1.8 } },
          (TYPES.filter(function (t) { return t[0] === base.type; })[0] || TYPES[0])[1]
          + ' · внутри ' + Math.round(base.W || 0) + '×' + Math.round(base.D || 0) + ' мм',
          h('br'), 'высота ' + Math.round(base.H || 0) + ' мм'
          + (base.type === 'tandem' ? ' · серия ' + (base.series || 'M') : ''),
          h('br'), 'сетка ' + g.cols + '×' + g.rows + ' (шаг 42, spacerless)'),
        h('div', { className: 'hint' },
          'Размеры ящика меняются на вкладке «3D» — там сразу видно, что помещается.')),

      h('label', { className: 'wfield' }, h('span', null, 'Название проекта'),
        h('input', {
          type: 'text', maxLength: 80, value: title === null ? data.project.title : title,
          onChange: function (e) { setTitle(e.target.value); },
          onBlur: function () {
            if (title !== null && title !== data.project.title) patch({ title: title }, 'Переименовано');
          },
        })),

      h('a', {
        className: 'btn ghost', style: { marginTop: 12, display: 'flex', justifyContent: 'center', textDecoration: 'none' },
        href: API.gfxTemplateUrl(pid), target: '_blank', rel: 'noopener',
      }, 'Шаблон дна 1:1 (PDF)'),
      h('div', { className: 'hint' },
        'Распечатайте БЕЗ «подгонки под страницу», склейте и положите в ящик — так замеры '
        + 'проверяются листом бумаги, а не заказом.'),

      h('button', {
        className: 'btn ghost', style: { marginTop: 12 }, type: 'button', disabled: busy,
        onClick: function () { patch({ share: data.share ? 0 : 1 }, data.share ? 'Ссылка отозвана' : 'Ссылка готова'); },
      }, data.share ? 'Отозвать ссылку «показать»' : 'Ссылка «показать» (только чтение)'),
      data.share ? h('div', {
        className: 'hint',
        style: { wordBreak: 'break-all', userSelect: 'all' },
      }, location.origin + '/gfx/' + data.share) : null,

      h('button', {
        className: 'btn ghost', style: { marginTop: 12 }, type: 'button', disabled: busy,
        onClick: function () { patch({ snapshot: 1 }, 'Снимок раскладки сохранён'); },
      }, 'Сохранить снимок раскладки'),
      !!(data.versions || []).length && h('div', null,
        h('div', { className: 'hint' }, 'Снимки (последние ' + data.versions.length + '):'),
        h('div', { className: 'wchips' }, data.versions.map(function (v) {
          return h('button', {
            key: v.id, type: 'button', className: 'chip', disabled: busy,
            onClick: function () { patch({ restore: v.id }, 'Раскладка откачена'); },
          }, new Date(v.created_at * 1000).toLocaleDateString('ru-RU')
            + (v.note ? ' · ' + v.note : ''));
        }))),

      h('button', {
        className: 'btn ghost', style: { marginTop: 12 }, type: 'button', disabled: busy,
        onClick: props.onDuplicate,
      }, 'Дублировать проект'),
      h('div', { className: 'hint' },
        'Соседний ящик тумбы или «то же самое, но глубже»: раскладка копируется, '
        + 'а заказы и состояния деталей — нет, копия начинается с чистого листа.'),

      h(Presets, { app: app, onInsert: props.onInsert }),

      h('button', {
        className: 'btn line', style: { marginTop: 18 }, type: 'button', disabled: busy,
        onClick: function () { patch({ archived: data.project.archived ? 0 : 1 }, 'Готово'); },
      }, data.project.archived ? 'Вернуть из архива' : 'В архив'),
      h('button', {
        className: 'btn line', style: { marginTop: 9, color: 'var(--red)' }, type: 'button',
        disabled: busy,
        onClick: function () {
          if (!window.confirm('Удалить проект целиком? Заказанные детали останутся в заявках.')) return;
          API.gfxProjectPatch(pid, { delete: 1 }).then(function (j) {
            if (j && j.deleted) { app.toast('Проект удалён'); app.back(); }
            else if (j && j.note) { app.toast(j.note); props.onChanged(); }
          });
        },
      }, 'Удалить проект'));
  }

  // ── свои заготовки: удачная деталь переносится между проектами ──
  function Presets(props) {
    var st = useState(null); var list = st[0], setList = st[1];
    useEffect(function () {
      API.gfxPresets().then(function (j) { setList((j && j.presets) || []); },
        function () { setList([]); });
    }, []);
    if (!list || !list.length) {
      return h('div', { className: 'hint', style: { marginTop: 16 } },
        'Заготовок пока нет. Сохраните удачную деталь из спецификации — и вставите её '
        + 'в любой другой проект одним тапом.');
    }
    function drop(id) {
      API.gfxPresetDelete(id).then(function (j) { setList((j && j.presets) || []); });
    }
    return h('div', { style: { marginTop: 16 } },
      h('div', { className: 'hint' }, 'Мои заготовки — вставляются в текущую раскладку:'),
      h('div', { className: 'wchips' }, list.map(function (pr) {
        return h('span', { key: pr.id, style: { display: 'flex', gap: 4 } },
          h('button', {
            type: 'button', className: 'chip',
            onClick: function () { props.onInsert(pr); },
          }, pr.title || 'Заготовка'),
          h('button', {
            type: 'button', className: 'chip', style: { color: 'var(--red)' },
            'aria-label': 'Удалить заготовку',
            onClick: function () { drop(pr.id); },
          }, '✕'));
      })));
  }

  // ── шторка детали ──
  function PartSheet(props) {
    var p = props.part;
    var nm = useState(null); var name = nm[0], setName = nm[1];
    var nt = useState(null); var note = nt[0], setNote = nt[1];
    if (!p) return null;
    var busy = BUSY[p.live_state];
    var others = props.parts.filter(function (x) { return x.parent_id === p.id || x.id === p.parent_id; });

    function save(body) { props.onPatch(p.id, body); }

    return h(Sheet, {
      title: p.name, onClose: props.onClose,
      right: h('span', { className: 'wpill ' + pillClass(p) }, p.state_label),
    },
      h('div', { style: { display: 'grid', gridTemplateColumns: '84px 1fr', gap: 12, alignItems: 'center' } },
        h('div', {
          style: { height: 54, borderRadius: 10, background: p.color_hex },
          className: busy ? 'wthumb ord' : 'wthumb',
        }),
        h('div', { style: { fontFamily: 'var(--mono)', fontSize: 11, lineHeight: 1.7, color: 'var(--muted)' } },
          p.auto_name || p.gen_label, h('br'),
          Math.round(p.w_mm) + ' × ' + Math.round(p.d_mm) + ' × ' + Math.round(p.h_mm) + ' мм ('
          + p.units + 'u)', h('br'),
          p.color + (p.vol_cm3 ? ' · ' + p.vol_cm3 + ' см³' : ''), h('br'),
          '≈ ' + Math.round(p.grams) + ' г · ' + hours(p.minutes) + ' · ' + money(p.price))),

      h('label', { className: 'wfield' }, h('span', null, 'Как называем'),
        h('input', {
          type: 'text', maxLength: 60, placeholder: p.auto_name,
          value: name === null ? (p.custom_name ? p.name : '') : name,
          onChange: function (e) { setName(e.target.value); },
          onBlur: function () { if (name !== null) save({ name: name }); },
        })),
      h('label', { className: 'wfield' }, h('span', null, 'Что кладём'),
        h('textarea', {
          maxLength: 200, placeholder: 'ключи 8–19, зарядки, лопатки…',
          value: note === null ? p.note : note,
          onChange: function (e) { setNote(e.target.value); },
          onBlur: function () { if (note !== null) save({ note: note }); },
        })),
      h('div', { className: 'hint' },
        'Заметка уезжает мастеру вместе с деталью — по ней он поймёт нагрузку и посадку.'),

      !busy && h('div', { className: 'wchips', style: { marginTop: 12 } },
        h('span', { style: { fontSize: 12, color: 'var(--muted)', alignSelf: 'center' } }, 'Количество:'),
        [1, 2, 3, 4].map(function (n) {
          return h('button', {
            key: n, type: 'button', className: 'chip' + (p.qty === n ? ' on' : ''),
            onClick: function () { save({ qty: n }); },
          }, '×' + n);
        })),

      !busy && h('button', {
        className: 'btn ghost', style: { marginTop: 12 }, type: 'button',
        onClick: function () { save({ state: p.state === 'hold' ? 'draft' : 'hold' }); },
      }, p.state === 'hold' ? 'Вернуть в работу' : 'Отложить на потом'),

      p.on_plan ? h('button', {
        className: 'btn line', style: { marginTop: 9 }, type: 'button',
        onClick: function () { props.onPreset(p); props.onClose(); },
      }, 'Сохранить как заготовку') : null,

      busy && h('div', { className: 'wchk', style: { marginTop: 12 } },
        'Деталь уже в заявке #' + p.request_id + '. Менять её здесь нельзя — поправьте модуль '
        + 'в раскладке: пока заявку не взяли в работу, правка уедет мастеру, а после — '
        + 'мы сделаем новую версию, и напечатанная останется в истории.'),

      p.changed ? h('div', { className: 'wchk warn', style: { marginTop: 9 } },
        'Вы изменили эту деталь после заказа — мастер предупреждён.') : null,

      !!others.length && h('div', { className: 'wlog' },
        h('div', { className: 'li' }, h('span', null, 'версии'),
          h('div', null, others.map(function (o) {
            return o.id === p.parent_id ? ('v' + o.version + ' — ' + o.state_label
              + (o.request_id ? ' (#' + o.request_id + ')' : ''))
              : ('v' + o.version + ' — ' + o.state_label);
          }).join(', ')))),

      h('div', { className: 'wlog' },
        h('div', { className: 'li' }, h('span', null, 'состояние'), h('div', null, p.state_label)),
        p.request_id ? h('div', { className: 'li' }, h('span', null, 'заявка'),
          h('div', null, '#' + p.request_id + (p.req_status ? ' · ' + p.req_status : ''))) : null,
        p.printed_at ? h('div', { className: 'li' }, h('span', null, 'готова'),
          h('div', null, new Date(p.printed_at * 1000).toLocaleDateString('ru-RU'))) : null));
  }

  // ── шторка оформления партии ──
  function OrderSheet(props) {
    var app = props.app;
    var pf = useState(null); var pre = pf[0], setPre = pf[1];
    var cm = useState(''); var comment = cm[0], setComment = cm[1];
    var bs = useState(false); var busy = bs[0], setBusy = bs[1];

    var er = useState(''); var err = er[0], setErr = er[1];
    function loadPre() {
      setErr('');
      API.gfxPreflight(props.pid, props.ids).then(function (j) {
        if (j && j.ok) { setPre(j); return; }
        setErr((j && j.error) || 'Не удалось посчитать смету');
      }, function () { setErr('Нет сети — смета не посчиталась'); });
    }
    useEffect(loadPre, []);

    function send() {
      if (busy) return;
      setBusy(true);
      API.gfxOrder(props.pid, props.ids, comment).then(function (j) {
        setBusy(false);
        if (!j || !j.ok) { hap('error'); app.toast((j && j.error) || 'Не удалось отправить'); return; }
        props.onDone(j.order_id, j.n);
      }, function () { setBusy(false); app.toast('Сеть недоступна'); });
    }

    var blocked = pre && pre.blocked;
    return h(Sheet, { title: 'Оформление партии', onClose: props.onClose },
      !pre && !err && h('div', { className: 'hint' }, 'Считаю смету…'),
      err && h('div', null,
        h('div', { className: 'wchk block' }, h('span', null, err)),
        h('button', {
          className: 'btn ghost', style: { marginTop: 10 }, type: 'button', onClick: loadPre,
        }, 'Повторить')),
      pre && h('div', null,
        h('div', { style: { fontSize: 12, color: 'var(--muted)', marginBottom: 8 } },
          'Отправляем · ' + pre.total.n + ' дет., ' + pre.total.qty + ' шт'),
        pre.parts.map(function (p) {
          return h('div', {
            key: p.id,
            style: { display: 'flex', gap: 9, alignItems: 'center', fontSize: 13, marginTop: 6 },
          },
            h('span', { style: { width: 16, height: 16, borderRadius: 5, background: p.color_hex, flex: '0 0 auto' } }),
            h('span', { style: { minWidth: 0, flex: 1 } }, p.name + (p.qty > 1 ? ' ×' + p.qty : '')),
            h('span', { className: 'wmoney' }, '≈ ' + money(p.price)));
        }),

        !!(pre.colors || []).length && h('div', { style: { marginTop: 12 } },
          h('div', { className: 'hint' }, 'Пластика по цветам:'),
          h('div', { className: 'wchips' }, pre.colors.map(function (c, i) {
            return h('span', {
              key: i, className: 'chip',
              style: c.short ? { borderColor: 'var(--red)', color: 'var(--red)' } : null,
            },
              h('span', {
                style: { display: 'inline-block', width: 9, height: 9, borderRadius: 3,
                  background: c.color_hex, marginRight: 6 },
              }),
              c.color + ' ≈ ' + Math.round(c.need) + ' г'
              + (c.have ? ' · на складе ' + Math.round(c.have) : ''));
          }))),

        (pre.checks || []).map(function (c, i) {
          return h('div', { key: i, className: 'wchk ' + (c.level === 'block' ? 'block' : 'warn') },
            h('span', null, c.level === 'block' ? '⛔' : '⚠'), h('span', null, c.text));
        }),

        stdNote(pre.standard, pre.total),

        h('label', { className: 'wfield' }, h('span', null, 'Комментарий мастеру'),
          h('textarea', {
            maxLength: 500, placeholder: 'Например: пластик матовый, нужно к пятнице',
            value: comment, onChange: function (e) { setComment(e.target.value); },
          })),

        h('div', { className: 'wtot', style: { position: 'static', marginTop: 12 } },
          h('div', { className: 'l' }, 'Итого ориентировочно',
            h('b', null, '≈ ' + money(pre.total.price) + ' · ' + hours(pre.total.minutes))),
          h('button', {
            className: 'btn', type: 'button', disabled: busy || blocked, onClick: send,
          }, busy ? 'Отправляю…' : 'Отправить')),
        h('div', { className: 'hint', style: { textAlign: 'center' } },
          blocked ? 'Сначала поправьте то, что отмечено красным.'
            : 'Цену подтвердит мастер — оценка считается по объёму модели.')));
  }
})();
