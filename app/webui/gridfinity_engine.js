// gridfinity_engine.js — Gridfinity Studio: 3D-конфигуратор органайзера в ящик/полку/Blum.
// Порт дизайн-пакета design_handoff_gridfinity (state.js/geometry.js/app.js/panels.js/
// editor3d.js) на вендорный глобальный THREE (r147 + OrbitControls, как в screens_3d.js)
// с жизненным циклом mount/destroy для мини-аппа.
// window.GFStudio.mount(hostEl, {app, config, save, order, onSpec, parts, lsKey}) ->
//   {destroy, setParts, setFilter, setConfig, snapshot, flush, addFromTemplate, selected}
// parts — спецификация проекта с сервера: сцена помечает уже заказанные детали и умеет
// прятать лишнее фильтром. Сама спецификация живёт в React-экране, движок её не редактирует.
//   config — стартовый черновик с сервера (null → localStorage → демо-раскладка seed);
//   save(data) — дебаунс-сохранение черновика; order(data) — заявка мастеру (Promise).
// Формулы геометрии выверены с реальными замерами (README пакета) — НЕ «упрощать»:
// шаг 42, бокс = 42N−0.62, высота 7u+4.85, spacerless-растяжение краёв, профиль царги Blum.
(function () {
  'use strict';
  var STYLE_ID = 'gfx-style';

  // ── доменные константы (state.js, дословно) ──
  var PITCH = 42, PLATE_H = 4.5, SEAT = 1.35, FOOT_H = 4.75, FLOOR_TOP = 5.96,
      WALL_T = .8, DIV_T = .8, CLR = .62, RAD_OUT = 3.75, RAD_IN = 2.95;
  var HMM = function (u) { return +(7 * u + 4.85).toFixed(2); };
  var SERIES = { M: { h: 83, rails: [], top: 83 }, K: { h: 115, rails: [], top: 115 },
    C: { h: 83, rails: [192], top: 192 }, D: { h: 83, rails: [152, 224], top: 224 } };
  var PROF = { out: 36, foot: 0, slopeY: 32, inset: 23 };
  var COLORS = [['Оранж', '#ff7a1a'], ['Графит', '#3d4a5e'], ['Бирюза', '#3fb6c9'],
    ['Лайм', '#2dd47e'], ['Лаванда', '#a98bff'], ['Песок', '#d4b183']];
  var TYPE_LABEL = { shelf: 'Полка', drawer: 'Шуфлядка', tandem: 'Tandembox' };
  var GEN_LABEL = { tray: 'Лоток', comp: 'Отсеки', grid: 'Сетка', comb: 'Гребёнка', free: 'Свои' };
  var ITEM_LABEL = { scoop: 'Скос дна', platform: 'Подиум', pins: 'Штырьки',
    label: 'Полочка', cups: 'Гнёзда', vslot: 'V-ложемент' };
  var LS = 'gf-studio-v4';   // тот же ключ, что у прототипа — черновики переживают апгрейд
  // ...но с появлением ПРОЕКТОВ один ключ на всех — это порча данных: локальная копия
  // проекта А свежее серверной копии проекта Б, и при открытии Б движок показал бы (а
  // потом и сохранил) раскладку А. Поэтому у каждого проекта свой ключ, а голый LS
  // остаётся только у экрана без проекта.

  // Стили — адаптированный studio.css пакета: токены переведены на переменные app.css
  // (--bg-2/--surface-2/--price вместо --bg2/--surface2/--green), все классы с префиксом
  // g-/gfx, чтобы не столкнуться с .btn/.chip/.card мини-аппа. < 920px — панели становятся
  // bottom-sheets с таб-баром «База/Модули» (как в прототипе; таб-бар аппа на экране скрыт).
  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var css = [
      '.gfx{--gfx-mono:"JetBrains Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;display:flex;flex-direction:column;height:calc(100dvh - 104px - env(safe-area-inset-top));min-height:430px;color:var(--text);font-size:14px;line-height:1.45}',
      '.gfx .g-mono{font-family:var(--gfx-mono)}',
      '.g-top{flex:0 0 auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:2px 0 8px}',
      '.g-filt{padding:0 0 8px}',
      '.g-filt .g-tbtn.on{border-color:var(--accent);color:var(--accent)}',
      '.g-chip{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12px;color:var(--muted)}',
      '.g-tbtn{background:var(--surface);border:1px solid var(--line-2);color:var(--text);font:600 12.5px inherit;font-family:inherit;padding:7px 13px;border-radius:11px;cursor:pointer}',
      '.g-tbtn:active{transform:scale(.96)}',
      '.g-tbtn.warn{color:var(--red)}',
      '.g-tbtn.acc{background:var(--accent);border-color:var(--accent);color:#1a0d00}',
      '.g-tbtn[disabled]{opacity:.55;pointer-events:none}',
      '.g-sp{flex:1}',
      '.g-wrap{flex:1;display:grid;grid-template-columns:288px 1fr 324px;min-height:0;border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--bg-2)}',
      '.g-panel{background:var(--bg);display:flex;flex-direction:column;min-height:0}',
      '.g-left{border-right:1px solid var(--line)}',
      '.g-right{border-left:1px solid var(--line)}',
      // Правая колонка держит ДВЕ шторки: палитру модулей и параметры выбранной детали.
      // На широком экране это одна колонка сетки (иначе .g-wrap получил бы четвёртую),
      // на узком обёртка растворяется (display:contents ниже) и шторки становятся
      // самостоятельными — у каждой своя вкладка внизу.
      ".g-rcol{display:flex;flex-direction:column;min-height:0;border-left:1px solid var(--line)}",
      ".g-rcol .g-right{border-left:0}",
      ".g-rcol .g-panel + .g-panel{border-top:1px solid var(--line)}",
      '.g-phead{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;padding:13px 16px 9px;font-weight:700;font-size:13px;letter-spacing:.4px;text-transform:uppercase;color:var(--faint)}',
      '.g-pclose{display:none;background:none;border:0;color:var(--muted);font-size:14px;cursor:pointer;padding:4px 8px}',
      '.g-pbody{flex:1;overflow-y:auto;padding:2px 16px 24px;scrollbar-width:thin;scrollbar-color:var(--raise) transparent}',
      '.g-sec{margin-top:16px}',
      '.g-lbl{font-size:11.5px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--faint);margin:0 0 8px}',
      '.g-seg{display:flex;gap:4px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:4px}',
      '.g-seg button{flex:1;background:none;border:0;border-radius:9px;color:var(--muted);font:600 12.5px inherit;font-family:inherit;padding:8px 4px;cursor:pointer}',
      '.g-seg button.on{background:var(--raise);color:var(--text)}',
      '.g-genseg button{font-size:11px;padding:8px 1px}',
      '.g-stp{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:6px 8px}',
      '.g-stp .g-nm{flex:1;font-size:13px;color:var(--muted)}',
      '.g-stp button{width:28px;height:28px;border:1px solid var(--line-2);background:var(--surface-2);color:var(--text);border-radius:9px;font-size:15px;cursor:pointer;line-height:1}',
      '.g-stp input{width:56px;background:none;border:0;color:var(--text);font:600 14px var(--gfx-mono);text-align:center;outline:none}',
      '.g-stp input::-webkit-outer-spin-button,.g-stp input::-webkit-inner-spin-button{-webkit-appearance:none}',
      '.g-stack{display:flex;flex-direction:column;gap:8px}',
      '.g-info{font:12px var(--gfx-mono);color:var(--faint);background:var(--surface);border:1px dashed var(--line-2);border-radius:10px;padding:8px 11px;line-height:1.6}',
      '.g-info b{color:var(--muted);font-weight:600}',
      '.g-series{display:grid;grid-template-columns:1fr 1fr;gap:8px}',
      '.g-series button{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:9px 11px;cursor:pointer;text-align:left;color:var(--text);font-family:inherit}',
      '.g-series button.on{border-color:var(--accent-line);background:var(--accent-soft)}',
      '.g-series b{font:700 15px var(--gfx-mono)}',
      '.g-series span{display:block;font-size:11px;color:var(--faint);margin-top:2px}',
      '.g-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}',
      '.g-card{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:9px 6px 7px;cursor:grab;text-align:center;user-select:none;-webkit-user-select:none;touch-action:none;color:var(--text);font-family:inherit}',
      '.g-card.on{border-color:var(--accent-line);background:var(--accent-soft)}',
      '.g-card svg{display:block;margin:0 auto 5px}',
      '.g-card i{font-style:normal;font:600 11.5px var(--gfx-mono);color:var(--muted)}',
      '.g-card.g-wing{grid-column:span 3;display:flex;align-items:center;gap:10px;text-align:left;padding:9px 12px}',
      '.g-card.g-wing i{color:var(--text)}',
      '.g-card.g-wing small{display:block;font:11px var(--gfx-mono);color:var(--faint)}',
      '.g-btn{background:var(--surface-2);border:1px solid var(--line-2);color:var(--text);font:600 13px inherit;font-family:inherit;padding:9px 12px;border-radius:11px;cursor:pointer}',
      '.g-btn.acc{background:var(--accent);border-color:var(--accent);color:#1a0d00}',
      '.g-btn.warn{color:var(--red)}',
      '.g-btn.sm{padding:6px 10px;font-size:12px;border-radius:9px}',
      '.g-row{display:flex;gap:8px;flex-wrap:wrap}',
      '.g-row .g-btn{flex:1}',
      '.g-swatches{display:flex;gap:8px}',
      '.g-sw{width:26px;height:26px;border-radius:9px;border:2px solid transparent;cursor:pointer;padding:0}',
      '.g-sw.on{border-color:#fff;box-shadow:0 0 0 2px var(--bg)}',
      '.gfx input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:26px;background:none}',
      '.gfx input[type=range]::-webkit-slider-runnable-track{height:5px;border-radius:3px;background:var(--raise)}',
      '.gfx input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--accent);margin-top:-6.5px;border:3px solid var(--bg);box-shadow:0 1px 4px rgba(0,0,0,.5)}',
      '.gfx input[type=range]::-moz-range-track{height:5px;border-radius:3px;background:var(--raise)}',
      '.gfx input[type=range]::-moz-range-thumb{width:13px;height:13px;border-radius:50%;background:var(--accent);border:2px solid var(--bg)}',
      '.g-sldrow{display:flex;align-items:center;gap:10px}',
      '.g-sldrow output{font:600 12px var(--gfx-mono);color:var(--muted);min-width:74px;text-align:right}',
      '.g-shead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:20px;padding-top:14px;border-top:1px solid var(--line)}',
      '.g-shead b{font-size:14.5px}',
      '.g-shead .g-chip{margin-left:auto}',
      '.g-divhint{font:11px var(--gfx-mono);color:var(--faint);margin-top:7px;line-height:1.5}',
      '.g-viewport{position:relative;min-width:0;min-height:0;background:var(--bg-2)}',
      // Виды камеры живут ПОВЕРХ сцены, а не отдельной строкой над ней: на телефоне
      // каждая строка тулбара — это минус полсотни пикселей у самой сцены, ради которой
      // экран и открыли. Полупрозрачная подложка, чтобы кнопки читались на любом ящике.
      ".g-vtools{position:absolute;top:10px;right:10px;z-index:4;display:flex;gap:6px}",
      ".g-vtools button{background:rgba(11,15,20,.72);border:1px solid var(--line-2);color:var(--text);"
      + "font:600 12px inherit;font-family:inherit;padding:6px 11px;border-radius:10px;cursor:pointer}",
      ".g-vtools button:active{transform:scale(.95)}",
      ".g-legend{position:absolute;left:14px;bottom:38px;z-index:3;pointer-events:none;font-family:var(--gfx-mono)}",
      ".g-legend:empty{display:none}",
      '.g-viewport canvas{position:absolute;inset:0;display:block}',
      '.g-summary{position:absolute;left:14px;bottom:14px;z-index:3;pointer-events:none;font-family:var(--gfx-mono)}',
      '.g-hint{position:absolute;top:14px;left:50%;transform:translateX(-50%);z-index:3;border-color:var(--accent-line);color:var(--accent);background:rgba(20,14,8,.85);pointer-events:none;max-width:92%;text-align:center}',
      '.g-hint[hidden]{display:none}',
      '.g-mtabs{display:none}',
      '.g-empty{color:var(--faint);font-size:13px;padding:10px 2px;line-height:1.5}',
      // диалог заявки: пожелание мастеру (цену назначает он — уточнение здесь ценнее всего)
      '.g-dlg{position:fixed;inset:0;z-index:60;background:rgba(5,8,12,.62);display:flex;align-items:center;justify-content:center}',
      '.g-dlg-card{box-sizing:border-box;background:var(--surface);border:1px solid var(--line-2);border-radius:16px;padding:16px;width:min(440px,calc(100vw - 24px));margin-bottom:env(safe-area-inset-bottom);box-shadow:0 18px 50px rgba(0,0,0,.5)}',
      '.g-dlg textarea{box-sizing:border-box;width:100%;min-height:64px;background:var(--bg-2);border:1px solid var(--line-2);border-radius:10px;color:var(--text);font-size:13px;font-family:inherit;padding:8px 10px;outline:none;resize:vertical}',
      '.g-dlg textarea:focus{border-color:var(--accent-line)}',
      // ── полноэкранный редактор модуля ──
      // height:100dvh обязателен: класс .gfx на этом же элементе задаёт height, а при
      // top+height правило bottom из inset:0 игнорируется — без явной высоты внизу
      // оставалась полоса, сквозь которую просвечивал экран под редактором
      '.gfx-ed{position:fixed;inset:0;height:100dvh;z-index:70;background:#0b0f14;display:flex;flex-direction:column;color:var(--text)}',
      '.gfx-ed[hidden]{display:none}',
      '.g-ed-top{flex:0 0 auto;display:flex;flex-wrap:wrap;align-items:center;min-height:52px;padding:6px 12px;padding-top:calc(6px + env(safe-area-inset-top));gap:8px;row-gap:6px;border-bottom:1px solid var(--line);background:var(--bg)}',
      '.g-ed-top b{font-size:13.5px;white-space:nowrap}',
      '.g-ed-tools{display:flex;gap:4px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:4px}',
      '.g-ed-tools button{background:none;border:0;border-radius:9px;color:var(--muted);font:600 12.5px inherit;font-family:inherit;padding:7px 13px;cursor:pointer}',
      '.g-ed-tools button.on{background:var(--raise);color:var(--text)}',
      '#gfxE3Done{margin-left:auto}',
      '#gfxE3Info{min-width:0;flex:0 1 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}',
      '@media (max-width:990px){.g-ed-top b{display:none}#gfxE3Info{display:none}.g-ed-tools button{padding:7px 9px;font-size:11.5px}}',
      '.g-ed-body{flex:1;display:grid;grid-template-columns:1fr 300px;min-height:0;overflow:hidden}',
      '.g-ed-stage{position:relative;min-width:0;min-height:0;overflow:hidden}',
      '.g-ed-stage canvas{position:absolute;inset:0;width:100%;height:100%}',
      '#gfxE3Dims{pointer-events:none;z-index:2}',
      '#gfxE3Hint{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);z-index:3;pointer-events:none;max-width:86%;text-align:center}',
      '.g-ed-side{border-left:1px solid var(--line);background:var(--bg);padding:2px 16px 22px;padding-bottom:calc(22px + env(safe-area-inset-bottom));overflow-y:auto}',
      '.g-ed-side .g-sec{margin-top:14px}',
      '.g-ed-items{display:flex;flex-direction:column;gap:8px}',
      '.g-ed-item{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:9px 12px;cursor:grab;touch-action:none}',
      '.g-ed-item.on{border-color:var(--accent-line);background:var(--accent-soft)}',
      '.g-ed-item i{font-style:normal;font:600 13px inherit;font-family:inherit}',
      '.g-ed-item small{display:block;font:11px var(--gfx-mono);color:var(--faint);margin-top:2px}',
      // Полоса-ручка панели. На ПК её нет вовсе (панель — колонка справа, сцене не мешает),
      // поэтому по умолчанию display:none, а включает её только узкий макет ниже.
      '.g-ed-fold{display:none;align-items:center;justify-content:space-between;gap:12px;width:100%;box-sizing:border-box;background:var(--bg);border:0;border-top:1px solid var(--line);color:var(--muted);font:600 10.5px inherit;font-family:inherit;letter-spacing:.08em;text-transform:uppercase;padding:11px 16px;cursor:pointer;text-align:left}',
      '.g-ed-fold i{font-style:normal;font-size:15px;line-height:1;color:var(--faint);transition:transform .18s}',
      '.g-ed-body.fold .g-ed-fold i{transform:rotate(180deg)}',
      // На узком экране сцена и панель встают строками — и здесь был баг «модуля не видно»:
      // `max-height:44%` НЕ участвует в измерении auto-строки (процент считать не от чего,
      // высота строки в этот момент и вычисляется), поэтому строка панели бралась по её
      // содержимому — 688px против 667px тела — и `1fr` сцены схлопывался в 0. Панель при
      // этом всё же обрезалась до 44% вторым проходом, так что под ней оставалась пустота,
      // а 3D не было вовсе. Потолок панели обязан быть в АБСОЛЮТНЫХ единицах, а у сцены —
      // свой пол, иначе баг вернётся, как только в панель добавят ещё одну секцию.
      // Строк три: сцена, полоса-ручка, панель. Свёрнутая панель убирается из потока
      // (display:none) — её строка `auto` схлопывается сама, и всё, что она занимала,
      // достаётся сцене; отдельного правила на высоту строки не нужно.
      '@media (max-width:820px){.g-ed-body{grid-template-columns:1fr;grid-template-rows:minmax(140px,1fr) auto minmax(0,auto)}',
      '.g-ed-fold{display:flex}',
      '.g-ed-side{border-left:0;max-height:40dvh}',
      '.g-ed-body.fold .g-ed-side{display:none}',
      // свёрнута — полоса стала последней на экране и обязана сама отступить от «челюсти»
      '.g-ed-body.fold .g-ed-fold{padding-bottom:calc(11px + env(safe-area-inset-bottom))}}',
      // ── отклик на курсор: при порте studio.css hover-состояния потерялись целиком,
      // и панели на ПК ощущались «мёртвыми». На тач-экранах блок не применяется вовсе.
      '.gfx .g-card,.gfx .g-btn,.gfx .g-tbtn,.gfx .g-sw,.gfx .g-seg button,.gfx-ed .g-ed-item,.gfx-ed .g-btn{transition:background .14s,border-color .14s,color .14s,transform .12s}',
      '@media (hover:hover) and (pointer:fine){',
      '.gfx .g-tbtn:hover,.gfx .g-btn:hover,.gfx-ed .g-btn:hover{background:var(--raise);border-color:var(--line-2)}',
      '.gfx .g-card:hover{border-color:var(--accent-line);background:var(--surface-2);transform:translateY(-1px)}',
      '.gfx .g-stp button:hover,.gfx-ed .g-stp button:hover{background:var(--raise)}',
      '.gfx-ed .g-ed-item:hover{background:var(--surface-2);border-color:var(--line-2)}',
      '.gfx .g-seg button:hover:not(.on),.gfx-ed .g-ed-tools button:hover:not(.on){color:var(--text)}',
      '.gfx .g-sw:hover{transform:scale(1.09)}',
      '.gfx .g-series button:hover{border-color:var(--line-2)}',
      '}',
      '.gfx .g-btn:active,.gfx .g-card:active,.gfx-ed .g-ed-item:active,.gfx .g-sw:active{transform:scale(.955)}',
      // ── узкий десктоп (ноутбук, окно в половину экрана): панели УЖИМАЮТСЯ ──
      // Прототип рисовался под широкое окно и ниже 920px просто прятал панели в шторки.
      // На ПК это читается как «кнопки пропали»: половина экрана — обычный размер окна.
      // Поэтому шторки теперь только для телефонов (≤820px), а между — плотная вёрстка.
      '@media (max-width:1360px){.g-wrap{grid-template-columns:252px 1fr 284px}}',
      // Колонки здесь ужимаются до 224px, а компактный шаговый ряд включался только
      // с 1020px — в промежутке строка «Ширина внутри − 480 + мм» не влезала и
      // подпись единиц вылезала за панель на сцену. Ужимаем ряд там же, где колонку.
      '@media (max-width:1180px){.g-wrap{grid-template-columns:224px 1fr 254px}',
      '.g-stp{padding:5px 6px;gap:5px}',
      '.g-stp input{width:42px}',
      '.g-stp button{width:24px;height:24px;font-size:14px}}',
      '@media (max-width:1020px){',
      '.g-wrap{grid-template-columns:194px 1fr 226px}',
      // overflow-x:hidden обязателен: моноширинная сводка («Расширения: ←9.0 →9.0…»)
      // распирала панель и вешала горизонтальный скроллбар поверх контента
      '.g-pbody{padding:2px 10px 20px;overflow-x:hidden}',
      '.g-phead{padding:11px 10px 7px}',
      '.g-lbl{font-size:10.5px;margin-bottom:6px}',
      '.g-seg{padding:3px;gap:3px}',
      '.g-seg button{font-size:11px;padding:7px 2px}',   // иначе «Tandembox» обрезался
      '.g-cards{gap:5px}',
      '.g-card{padding:7px 3px 5px;border-radius:11px}',
      '.g-card i{font-size:10.5px}',
      '.g-card.g-wing{padding:8px 9px;gap:8px}',
      '.g-card.g-wing small{font-size:10px}',
      '.g-stp{padding:5px 6px;gap:5px}',
      '.g-stp .g-nm{font-size:11.5px}',
      '.g-stp input{width:42px;font-size:13px}',
      '.g-stp button{width:24px;height:24px;font-size:14px}',
      '.g-series{gap:6px}',
      '.g-series button{padding:7px 7px}',
      '.g-series b{font-size:13.5px}',
      '.g-info{padding:7px 8px;font-size:10.5px;line-height:1.5;word-break:break-word}',
      '.g-divhint{font-size:10.5px}',
      '.g-btn{padding:8px 9px;font-size:12.5px}',
      '.g-sw{width:23px;height:23px}',
      '.g-swatches{gap:6px}',
      '.g-sec{margin-top:12px}',
      '.g-shead b{font-size:13px}',
      '}',
      // ── мобильная раскладка: панели → bottom-sheets, свой таб-бар внизу ──
      // Порог единый с редактором (820): при разъезде на пиксель редактор и шторки
      // оказывались в двух разных мирах.
      '@media (max-width:820px){',
      // env(safe-area-inset-top): в Telegram под вырез (viewport-fit=cover) шапка растёт
      // на его величину — без вычета низ студии уезжал под таб-бар и за кромку экрана
      '.gfx{height:calc(100dvh - 100px - env(safe-area-inset-top))}',
      // Экран проекта прячет шапку приложения (app.js, класс bare). Заодно чиним старую
      // арифметику: 100px не учитывали ФИКСИРОВАННЫЙ таб-бар шторок, и нижние 74px сцены
      // уезжали под него — рендерились и не были видны. Считаем честно: вырез сверху,
      // 72px своего ряда вкладок и тулбара, 54px таб-бара и вырез снизу.
      '.tg.bare .gfx{height:calc(100dvh - 126px - env(safe-area-inset-top) - env(safe-area-inset-bottom))}',
      // сцена больше не заходит под таб-бар — чипам не от чего прятаться
      '.tg.bare .g-summary{bottom:14px}',
      '.tg.bare .g-legend{bottom:40px}',
      '.g-wrap{grid-template-columns:1fr}',
      '#gfxChip{display:none}',
      '.g-panel{position:fixed;left:0;right:0;bottom:calc(54px + env(safe-area-inset-bottom));top:auto;max-height:62%;z-index:20;border:0;border-top:1px solid var(--line-2);border-radius:18px 18px 0 0;transform:translateY(120%);transition:transform .25s;box-shadow:0 -12px 40px rgba(0,0,0,.5)}',
      '.g-rcol{display:contents}',
      '.g-panel.open{transform:none}',
      '.g-pclose{display:block}',
      '.g-mtabs{display:flex;position:fixed;left:0;right:0;bottom:0;height:calc(54px + env(safe-area-inset-bottom));padding-bottom:env(safe-area-inset-bottom);z-index:21;background:var(--bg);border-top:1px solid var(--line)}',
      '.g-mtabs button{flex:1;background:none;border:0;color:var(--muted);font:600 13px inherit;font-family:inherit;cursor:pointer}',
      '.g-mtabs button.on{color:var(--accent)}',
      '.g-mtabs button.has::after{content:" •";color:var(--accent)}',
      // чип сводки поднят НАД таб-баром «База/Модули» (как #summary{bottom:68px} в
      // прототипе): на bottom:14px его накрывал fixed-бар + safe-area айфонов
      '.g-summary{bottom:calc(48px + env(safe-area-inset-bottom))}',
      '.g-legend{bottom:calc(74px + env(safe-area-inset-bottom))}',
      '}',
      // ПОСЛЕ мобильного блока и двумя классами: правило .gfx{height:...-100px} стоит ниже
      // .gfx-ed{height:100dvh} в этом же файле и при равной специфичности побеждало — на
      // телефоне редактор оказывался на 96px короче экрана, снизу просвечивала студия
      // (в прототипе #ed3 был ID и такого не знал).
      // ── фильтр вида в тулбаре: та же таблетка, что чип ящика рядом ──
      // Стоит В КОНЦЕ таблицы намеренно. `.g-segf button` и `.g-seg button` равны по силе
      // (класс + тег), поэтому решает порядок: панельный вариант сегмента — крупные кнопки
      // во всю ширину — и его мобильные переопределения обязаны остаться ВЫШЕ, иначе они
      // перебивают строчный. Первая редакция этого не учла, и компактные правила молча не
      // работали: сегмент был 42px и почти квадратный рядом с 29-пиксельной таблеткой чипа.
      '.g-segf{flex:0 0 auto;padding:3px;gap:2px;border-radius:999px}',
      '.g-segf button{flex:0 0 auto;padding:3px 11px;font-size:12px;font-weight:600;border-radius:999px}',
      // Активный фильтр — акцентом, а не серой заливкой: это состояние показа сцены,
      // и его видно боковым зрением, не вчитываясь в подписи.
      '.g-segf button.on{background:var(--accent-soft);color:var(--accent)}',
      // «Сброс» остался единственным прямоугольником в строке из таблеток (виды камеры
      // уехали в сцену) и выбивался формой. Правим адресно по id: класс .g-tbtn носят
      // кнопки по всей студии, и общая правка сменила бы им облик заодно.
      '#gfxClear{border-radius:999px;padding:5px 12px;font-size:12px}',
      '.gfx.gfx-ed{height:100dvh;min-height:100dvh}',
    ].join('\n');
    var st = document.createElement('style');
    st.id = STYLE_ID; st.textContent = css;
    document.head.appendChild(st);
  }

  function mount(host, opts) {
    opts = opts || {};
    var app = opts.app || {};
    var THREE = window.THREE;
    if (!THREE || !THREE.OrbitControls) {
      host.innerHTML = '<div class="g-empty">Не удалось загрузить 3D-движок — попробуйте ещё раз.</div>';
      return { destroy: function () {} };
    }
    // Ключ локальной копии — на ПРОЕКТ (см. комментарий у LS выше).
    var lsKey = opts.lsKey || LS;
    injectStyle();
    var tg = window.Telegram && window.Telegram.WebApp;
    // Тактильная отдача Telegram: модуль встал / не поместился / удалён. В браузере — no-op.
    function hap(kind) {
      try {
        var h = tg && tg.HapticFeedback;
        if (!h) return;
        if (kind === 'error') h.notificationOccurred('error');
        else h.impactOccurred(kind || 'light');
      } catch (e) {}
    }
    function confirmDlg(msg, onYes) {
      try { if (tg && tg.showConfirm) { tg.showConfirm(msg, function (ok) { if (ok) onYes(); }); return; } } catch (e) {}
      if (window.confirm(msg)) onYes();
    }

    // ── жизненный цикл: все глобальные слушатели/таймеры под учётом, destroy снимает всё ──
    var disposed = false;
    var offs = [], timers = [], ros = [];
    function on(t, ev, fn, o) { t.addEventListener(ev, fn, o); offs.push([t, ev, fn, o]); }
    function every(fn, ms) { timers.push(setInterval(fn, ms)); }

    // ════════════════ state (порт state.js) ════════════════
    var state = { base: { type: 'drawer', W: 480, D: 270, H: 120, series: 'M', fitX: 'sym', fitY: 'sym' },
      modules: [], sel: null, placing: null, editing: null, placingItem: null };
    var subs = {};
    var bus = { on: function (e, f) { (subs[e] = subs[e] || []).push(f); },
      emit: function (e, x) { (subs[e] || []).forEach(function (f) { f(x); }); } };
    var seq = 1;
    var gridInfo = function () {
      var b = state.base, W = b.W, D = b.D, fitX = b.fitX || 'sym', fitY = b.fitY || 'sym';
      var cols = Math.max(1, Math.floor(W / PITCH)), rows = Math.max(1, Math.floor(D / PITCH));
      var lx = W - cols * PITCH, ly = D - rows * PITCH;
      var mL = fitX === 'left' ? lx : fitX === 'sym' ? lx / 2 : 0, mR = lx - mL;
      var mB = fitY === 'back' ? ly : fitY === 'sym' ? ly / 2 : 0, mF = ly - mB;
      return { cols: cols, rows: rows, mL: mL, mR: mR, mB: mB, mF: mF, lx: lx, ly: ly };
    };
    var seriesOf = function () { return SERIES[state.base.series]; };
    var innerH = function () { var b = state.base; return b.type === 'tandem' ? Math.max(b.H || 0, seriesOf().top) : (b.H || 250); };
    var capMM = function (kind) { return Math.max(14, (kind === 'wing' ? seriesOf().h : innerH()) - PLATE_H); };
    var uMax = function (kind) { return Math.max(1, Math.floor((capMM(kind) - 4.85) / 7)); };
    var wingGap = function (side) { var g = gridInfo(); return side === 'L' ? g.mL : g.mR; };
    var wingOk = function (side) {
      if (state.base.type !== 'tandem') return false;
      if (!side) return wingGap('L') >= 6 || wingGap('R') >= 6;
      return wingGap(side) >= 6;
    };
    var byId = function (id) { return state.modules.find(function (m) { return m.id === id; }); };
    var selected = function () { return byId(state.sel); };
    var cellsUsed = function () { return state.modules.filter(function (m) { return m.kind === 'bin'; }).reduce(function (a, m) { return a + m.w * m.d; }, 0); };
    function canPlaceBin(cx, cy, w, d, ignore) {
      var g = gridInfo();
      if (cx < 0 || cy < 0 || cx + w > g.cols || cy + d > g.rows) return false;
      return !state.modules.some(function (m) { return m.kind === 'bin' && m.id !== ignore && cx < m.cx + m.w && m.cx < cx + w && cy < m.cy + m.d && m.cy < cy + d; });
    }
    function canPlaceWing(side, cy, d, ignore) {
      if (!wingOk(side)) return false;
      var g = gridInfo();
      if (cy < 0 || cy + d > g.rows) return false;
      return !state.modules.some(function (m) { return m.kind === 'wing' && m.side === side && m.id !== ignore && cy < m.cy + m.d && m.cy < cy + d; });
    }
    var defBin = function () { return { gen: 'tray', compN: 3, gridX: 2, gridY: 2, combN: 6, combW: 16, combSide: 'W', walls: [], islands: [], items: [], ext: { L: 0, R: 0, B: 0, F: 0 } }; };
    function extOf(m) {
      var r = { L: 0, R: 0, B: 0, F: 0 };
      if (!m.ext) return r;
      var g = gridInfo();
      if (m.ext.L && m.cx === 0 && g.mL > 2.3) r.L = g.mL - .3;
      if (m.ext.R && m.cx + m.w === g.cols && g.mR > 2.3) r.R = g.mR - .3;
      if (m.ext.B && m.cy === 0 && g.mB > 2.3) r.B = g.mB - .3;
      if (m.ext.F && m.cy + m.d === g.rows && g.mF > 2.3) r.F = g.mF - .3;
      return r;
    }
    function binDims(m) {
      var e = extOf(m);
      var ow = m.w * PITCH - CLR + e.L + e.R, od = m.d * PITCH - CLR + e.B + e.F;
      return { e: e, ow: ow, od: od, ox: (e.R - e.L) / 2, oz: (e.F - e.B) / 2 };
    }
    function bakeWalls(m) {
      var bd = binDims(m), ow = bd.ow, od = bd.od;
      var W = WALL_T, iw = ow - 2 * W, idd = od - 2 * W, out = [];
      var V = function (p, a0, a1) { out.push({ x0: p, y0: a0, x1: p, y1: a1, t: WALL_T }); };
      var H = function (p, a0, a1) { out.push({ x0: a0, y0: p, x1: a1, y1: p, t: WALL_T }); };
      var gen = m.gen || 'tray', k;
      if (gen === 'comp') {
        var n = Math.max(1, m.compN || 3);
        if (ow >= od) for (k = 1; k < n; k++) V(W + k * iw / n, W, W + idd);
        else for (k = 1; k < n; k++) H(W + k * idd / n, W, W + iw);
      }
      if (gen === 'grid') {
        var gx = Math.max(1, m.gridX || 2), gy = Math.max(1, m.gridY || 2);
        for (k = 1; k < gx; k++) V(W + k * iw / gx, W, W + idd);
        for (k = 1; k < gy; k++) H(W + k * idd / gy, W, W + iw);
      }
      if (gen === 'comb') {
        var s = m.combSide || 'W', cn = Math.max(2, m.combN || 6), along = s === 'W' || s === 'E';
        var cw = Math.min(m.combW || 16, (along ? iw : idd) - 12);
        if (s === 'W') { V(W + cw, W, W + idd); for (k = 1; k < cn; k++) H(W + k * idd / cn, W, W + cw); }
        if (s === 'E') { V(W + iw - cw, W, W + idd); for (k = 1; k < cn; k++) H(W + k * idd / cn, W + iw - cw, W + iw); }
        if (s === 'N') { H(W + cw, W, W + iw); for (k = 1; k < cn; k++) V(W + k * iw / cn, W, W + cw); }
        if (s === 'S') { H(W + idd - cw, W, W + iw); for (k = 1; k < cn; k++) V(W + k * iw / cn, W + idd - cw, W + idd); }
      }
      if (gen === 'free') return (m.walls || []).map(function (w) { return Object.assign({}, w); });
      return out;
    }
    function openEditor(id) {
      var m = byId(id);
      if (!m || m.kind !== 'bin') return;
      if (m.gen !== 'free') { m.walls = bakeWalls(m); m.gen = 'free'; }
      m.items = m.items || []; m.islands = m.islands || [];
      state.sel = id; state.editing = id;
      save(); bus.emit('mods'); bus.emit('ui'); bus.emit('edit');
    }
    function closeEditor() { state.editing = null; save(); bus.emit('mods'); bus.emit('ui'); bus.emit('edit'); }
    function setCustom(id, patch, quiet) {
      var m = byId(id);
      if (!m) return;
      if (patch.walls) m.walls = patch.walls.map(function (w) { return Object.assign({}, w); });
      if (patch.islands) m.islands = patch.islands.map(function (s) { return { pts: s.pts.map(function (p) { return p.slice(); }), hh: s.hh }; });
      if (patch.items) m.items = patch.items.map(function (i) { return Object.assign({}, i); });
      ['hmm', 'wallH', 'colorIdx'].forEach(function (k) { if (patch[k] !== undefined) m[k] = patch[k]; });
      m.gen = 'free'; save(); bus.emit('mods', { quiet: quiet !== false });
    }
    function addModule(tpl, pos) {
      var m;
      if (tpl.kind === 'wing') {
        if (!canPlaceWing(pos.side, pos.cy, tpl.d)) return null;
        m = { id: seq++, kind: 'wing', side: pos.side, cy: pos.cy, d: tpl.d,
          hmm: Math.min(tpl.hmm || capMM('wing'), capMM('wing')), comp: tpl.comp || 3,
          colorIdx: tpl.colorIdx != null ? tpl.colorIdx : (seq % COLORS.length) };
      } else {
        if (!canPlaceBin(pos.cx, pos.cy, tpl.w, tpl.d)) return null;
        m = Object.assign(defBin(), tpl, { id: seq++, kind: 'bin', cx: pos.cx, cy: pos.cy,
          hmm: Math.min(tpl.hmm || HMM(6), capMM('bin')),
          colorIdx: tpl.colorIdx != null ? tpl.colorIdx : (seq % COLORS.length) });
        m.walls = (tpl.walls || []).map(function (w) { return Object.assign({}, w); });
        m.items = (tpl.items || []).map(function (i) { return Object.assign({}, i); });
        m.islands = (tpl.islands || []).map(function (s) { return { pts: s.pts.map(function (p) { return p.slice(); }), hh: s.hh }; });
        m.ext = Object.assign({ L: 0, R: 0, B: 0, F: 0 }, tpl.ext || {});
      }
      m._new = 1;   // снимется в rebuildMods, когда модуль впервые появится в сцене
      state.modules.push(m); state.sel = m.id; state.placing = null;
      // Фильтр вида показывает только заказанное/только черновик — новый модуль под него
      // может не попасть и «исчезнуть» на глазах. Снимаем фильтр, а не молчим.
      if (filterMode !== 'all' && !visibleMod(m)) setFilter('all');
      hap('light');
      save(); bus.emit('mods'); bus.emit('ui');
      return m;
    }
    // Вставка СВОЕЙ ЗАГОТОВКИ (сохранённого модуля из другого проекта): места клиент не
    // выбирает — кладём в первую свободную ячейку слева-сверху, дальше он сам перетащит.
    // Возвращает false, если ящик занят целиком: молча проглотить тап нельзя.
    function addFromTemplate(mod) {
      if (!mod || typeof mod !== 'object') return false;
      var tpl = JSON.parse(JSON.stringify(mod));
      delete tpl.id; delete tpl.vol; delete tpl._new;
      if (tpl.kind === 'wing') {
        var g0 = gridInfo();
        for (var cy0 = 0; cy0 <= g0.rows - (tpl.d || 1); cy0++) {
          for (var si = 0; si < 2; si++) {
            var side = si ? 'R' : 'L';
            if (wingOk(side) && addModule(tpl, { side: side, cy: cy0 })) return true;
          }
        }
        return false;
      }
      var g = gridInfo(), w = Math.max(1, tpl.w || 1), d = Math.max(1, tpl.d || 1);
      for (var cy = 0; cy <= g.rows - d; cy++) {
        for (var cx = 0; cx <= g.cols - w; cx++) {
          if (canPlaceBin(cx, cy, w, d) && addModule(tpl, { cx: cx, cy: cy })) {
            if (filterMode !== 'all') setFilter('all');   // иначе вставленное сразу невидимо
            return true;
          }
        }
      }
      return false;
    }
    function upd(id, patch, quiet) {
      var m = byId(id);
      if (!m) return false;
      if (m.kind === 'bin' && (patch.w || patch.d)) {
        var w = patch.w || m.w, d = patch.d || m.d, g = gridInfo();
        var cx = Math.min(m.cx, g.cols - w), cy = Math.min(m.cy, g.rows - d);
        if (!canPlaceBin(cx, cy, w, d, id)) return false;
        patch.cx = cx; patch.cy = cy;
      }
      if (m.kind === 'wing' && patch.d) {
        var g2 = gridInfo(), cy2 = Math.min(m.cy, g2.rows - patch.d);
        if (!canPlaceWing(m.side, cy2, patch.d, id)) return false;
        patch.cy = cy2;
      }
      Object.assign(m, patch); save(); bus.emit('mods', { quiet: quiet });
      if (!quiet) bus.emit('ui');
      return true;
    }
    function removeModule(id) {
      var i = state.modules.findIndex(function (m) { return m.id === id; });
      if (i < 0) return;
      state.modules.splice(i, 1);
      hap('medium');
      if (state.sel === id) state.sel = null;
      save(); bus.emit('mods'); bus.emit('ui');
    }
    var rotSide = { W: 'N', N: 'E', E: 'S', S: 'W' }, rotExt = { L: 'B', B: 'R', R: 'F', F: 'L' };
    function rotateModule(id) {
      var m = byId(id);
      if (!m || m.kind !== 'bin') return;
      var w = m.d, d = m.w, g = gridInfo();
      var cx = Math.min(m.cx, g.cols - w), cy = Math.min(m.cy, g.rows - d);
      if (cx < 0 || cy < 0 || !canPlaceBin(cx, cy, w, d, id)) { flash(); return; }
      var od = binDims(m).od;
      var nw = (m.walls || []).map(function (wl) { return { x0: od - wl.y0, y0: wl.x0, x1: od - wl.y1, y1: wl.x1, t: wl.t }; });
      var ns = (m.islands || []).map(function (s) { return { hh: s.hh, pts: s.pts.map(function (p) { return [od - p[1], p[0]]; }) }; });
      var ni = (m.items || []).map(function (it) { return Object.assign({}, it, { x: od - (it.y + it.h), y: it.x, w: it.h, h: it.w }); });
      var ne = { L: 0, R: 0, B: 0, F: 0 };
      ['L', 'R', 'B', 'F'].forEach(function (k) { if (m.ext && m.ext[k]) ne[rotExt[k]] = 1; });
      Object.assign(m, { w: w, d: d, cx: cx, cy: cy, walls: nw, islands: ns, items: ni, ext: ne,
        gridX: m.gridY, gridY: m.gridX, combSide: rotSide[m.combSide] || 'W' });
      save(); bus.emit('mods'); bus.emit('ui');
    }
    function duplicateModule(id) {
      var m = byId(id);
      if (!m) return;
      var g = gridInfo(), cy, cx;
      if (m.kind === 'wing') {
        for (cy = 0; cy <= g.rows - m.d; cy++) if (canPlaceWing(m.side, cy, m.d)) { addModule(Object.assign({}, m), { side: m.side, cy: cy }); return; }
        var other = m.side === 'L' ? 'R' : 'L';
        for (cy = 0; cy <= g.rows - m.d; cy++) if (canPlaceWing(other, cy, m.d)) { addModule(Object.assign({}, m), { side: other, cy: cy }); return; }
        flash(); return;
      }
      for (cy = 0; cy <= g.rows - m.d; cy++) for (cx = 0; cx <= g.cols - m.w; cx++)
        if (canPlaceBin(cx, cy, m.w, m.d)) { addModule(Object.assign({}, m, { kind: 'bin' }), { cx: cx, cy: cy }); return; }
      flash();
    }
    function setBase(patch) {
      Object.assign(state.base, patch);
      var r = prune();
      save(); bus.emit('scene'); bus.emit('ui');
      // Шаг степпера ширины — 10 мм, и один клик «−» может стереть целый столбец модулей.
      // Молчать об этом нельзя: черновик уезжает на сервер, а работа пропадает без следа.
      if (r.dropped) { hap('error'); warn('Не поместились и убраны: ' + r.dropped + ' ' + plural(r.dropped, 'модуль', 'модуля', 'модулей')); }
      else if (r.clamped) warn('Высота обрезана у ' + r.clamped + ' ' + plural(r.clamped, 'модуля', 'модулей', 'модулей'));
    }
    function prune() {
      var g = gridInfo(), keep = [], dropped = 0, clamped = 0;
      state.modules.forEach(function (m) {
        var was = m.hmm;
        m.hmm = Math.min(m.hmm, capMM(m.kind));
        if (m.hmm < was - .01) clamped++;
        if (m.kind === 'wing') {
          if (!wingOk(m.side)) { dropped++; return; }
          m.cy = Math.max(0, Math.min(m.cy, g.rows - m.d));
          if (m.d > g.rows) { dropped++; return; }
          if (!keep.some(function (o) { return o.kind === 'wing' && o.side === m.side && m.cy < o.cy + o.d && o.cy < m.cy + m.d; })) keep.push(m);
          else dropped++;
          return;
        }
        if (m.w > g.cols || m.d > g.rows) { dropped++; return; }
        m.cx = Math.max(0, Math.min(m.cx, g.cols - m.w));
        m.cy = Math.max(0, Math.min(m.cy, g.rows - m.d));
        if (!keep.some(function (o) { return o.kind === 'bin' && m.cx < o.cx + o.w && o.cx < m.cx + m.w && m.cy < o.cy + o.d && o.cy < m.cy + m.d; })) keep.push(m);
        else dropped++;
      });
      state.modules = keep;
      if (state.sel && !byId(state.sel)) state.sel = null;
      return { dropped: dropped, clamped: clamped };
    }
    function select(id) { state.sel = id; bus.emit('ui'); bus.emit('sel'); }
    function clearAll() { state.modules = []; state.sel = null; save(); bus.emit('mods'); bus.emit('ui'); }
    function flash() { hap('error'); bus.emit('flash'); }
    // Предупреждение (модули убраны / высота обрезана) держим 3.4 с и защищаем от
    // затирания обычными подсказками — flash() гасит свою через 1.4 с.
    var warnUntil = 0;
    function warn(text) {
      warnUntil = Date.now() + 3400;
      bus.emit('warn', text);
      setTimeout(function () { if (Date.now() >= warnUntil - 50) { warnUntil = 0; bus.emit('warn', ''); } }, 3400);
    }

    // Персист: localStorage сразу (мгновенный кэш и офлайн-фолбэк превью), на сервер — с
    // дебаунсом 1.5 с через opts.save (иначе каждый драг слал бы запрос на pointermove).
    var saveT = null;
    // ts в снимке — арбитр «кто свежее» между серверной и локальной копией: сохранения на
    // сервер могут отбиваться (сеть/ратлимит), и без метки старый серверный черновик молча
    // затирал более новую локальную работу при следующем входе.
    function snapshot() { return JSON.parse(JSON.stringify({ v: 4, ts: Date.now(), base: state.base, modules: state.modules, seq: seq })); }
    function flushSave() {
      saveT = null;
      if (disposed) return;
      var snap = snapshot();
      try { localStorage.setItem(lsKey, JSON.stringify(snap)); } catch (e) {}
      if (opts.save) opts.save(snap);
    }
    function save() {
      // Никакой работы на самом вызове: save() прилетает на каждое движение указателя
      // при перетаскивании модуля, а сериализация раскладки — не бесплатная.
      clearTimeout(saveT);
      saveT = setTimeout(flushSave, 1500);
    }
    function load() {
      var tries = [opts.config];   // [сервер, локаль]; при равной свежести побеждает сервер
      try { tries.push(JSON.parse(localStorage.getItem(lsKey))); } catch (e) {}
      var best = null;
      for (var i = 0; i < tries.length; i++) {
        var d = tries[i];
        if (d && d.v === 4 && d.base && d.modules && (!best || (+d.ts || 0) > (+best.ts || 0))) best = d;
      }
      if (!best) return false;
      state.base = best.base; state.modules = best.modules; seq = best.seq || 200;
      return true;
    }
    function seed() {
      state.modules = [
        Object.assign(defBin(), { id: seq++, kind: 'bin', gen: 'tray', cx: 0, cy: 0, w: 4, d: 2, hmm: HMM(7), colorIdx: 0 }),
        Object.assign(defBin(), { id: seq++, kind: 'bin', gen: 'grid', gridX: 1, gridY: 2, cx: 4, cy: 0, w: 4, d: 4, hmm: HMM(6), colorIdx: 3 }),
        Object.assign(defBin(), { id: seq++, kind: 'bin', gen: 'comb', combSide: 'W', combN: 6, combW: 16, cx: 8, cy: 0, w: 2, d: 3, hmm: HMM(6), colorIdx: 4 }),
        Object.assign(defBin(), { id: seq++, kind: 'bin', gen: 'comp', compN: 3, cx: 10, cy: 0, w: 1, d: 2, hmm: 67.5, colorIdx: 5 }),
        Object.assign(defBin(), { id: seq++, kind: 'bin', gen: 'tray', cx: 0, cy: 2, w: 2, d: 4, hmm: 16.4, colorIdx: 2 }),
        Object.assign(defBin(), { id: seq++, kind: 'bin', gen: 'tray', cx: 8, cy: 3, w: 2, d: 3, hmm: 67.5, colorIdx: 1 })];
    }

    // ════════════════ геометрия (порт geometry.js, THREE-глобал) ════════════════
    // ЦВЕТОУПРАВЛЕНИЕ. Прототип живёт на three r184, где вывод рендерера в sRGB и цвета
    // материалов конвертируются движком сами. Вендорный r147 мини-аппа так НЕ умеет: он
    // пишет линейный цвет прямо в sRGB-буфер, отчего сцена выходит вымытой и засвеченной
    // (фон #0a0e12 читается светло-серым, пластик — кислотным). Приводим к прототипу
    // ЛОКАЛЬНО: выход рендерера в sRGB (свойство рендерера, чужих сцен не касается) +
    // каждый цвет палитры конвертируем sRGB→linear сами. Глобальный THREE.ColorManagement
    // НЕ трогаем: на том же вендорном THREE сидит вьюер моделей (screens_3d.js).
    var C = function (hex) { return new THREE.Color(hex).convertSRGBToLinear(); };
    var std = function (c, o) { return new THREE.MeshStandardMaterial(Object.assign({ color: C(c), roughness: .6 }, o || {})); };
    var mats = {
      plate: std('#2c3847', { roughness: .85 }), socket: std('#1f2936', { roughness: .9 }),
      wood: std('#7d5f43', { roughness: .72 }), wood2: std('#6a4f38', { roughness: .75 }), ply: std('#c0a179', { roughness: .68 }),
      steel: std('#c3ccd6', { metalness: .5, roughness: .35 }), steel2: std('#8894a2', { metalness: .4, roughness: .5 }),
      glass: new THREE.MeshStandardMaterial({ color: C('#22303f'), transparent: true, opacity: .32, roughness: .15, metalness: .4, depthWrite: false }),
      ground: std('#0d1218', { roughness: .95 }),
      ghostOk: new THREE.MeshBasicMaterial({ color: C('#2dd47e'), transparent: true, opacity: .35, depthWrite: false }),
      ghostBad: new THREE.MeshBasicMaterial({ color: C('#ff5b4d'), transparent: true, opacity: .4, depthWrite: false }) };
    var binMats = COLORS.map(function (c) { return std(c[1], { roughness: .5 }); });
    var binMat = function (i) { return binMats[(i || 0) % binMats.length]; };
    function rrect(w, d, r, cx, cy) {
      cx = cx || 0; cy = cy || 0;
      var s = new THREE.Shape(), x = cx - w / 2, y = cy - d / 2;
      r = Math.min(r, w / 2 - .1, d / 2 - .1);
      s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.absarc(x + w - r, y + r, r, -Math.PI / 2, 0);
      s.lineTo(x + w, y + d - r); s.absarc(x + w - r, y + d - r, r, 0, Math.PI / 2);
      s.lineTo(x + r, y + d); s.absarc(x + r, y + d - r, r, Math.PI / 2, Math.PI);
      s.lineTo(x, y + r); s.absarc(x + r, y + r, r, Math.PI, Math.PI * 1.5);
      return s;
    }
    var SEG = 8, SEG_SMALL = 6;   // сегментов на четверть дуги: корпус и ножка ОДИНАКОВО
    // Сварка нормалей по совпадающим позициям: внутри порога 30° нормали усредняются
    // (дуга скругления становится гладкой), а фаски 45° и излом царги 35.7° остаются
    // острыми. Порог именно 30°, чтобы у профиля царги оставался запас.
    function weldNormals(geo, deg) {
      var pos = geo.getAttribute('position'), nor = geo.getAttribute('normal');
      if (!pos || !nor) return geo;
      var cosT = Math.cos((deg || 30) * Math.PI / 180), map = {}, i, key;
      for (i = 0; i < pos.count; i++) {
        // округление, а не побитовое усечение: у отрицательных координат |0 режет к нулю
        key = Math.round(pos.getX(i) * 1e3) + '_' + Math.round(pos.getY(i) * 1e3) + '_' + Math.round(pos.getZ(i) * 1e3);
        (map[key] || (map[key] = [])).push(i);
      }
      var v = new THREE.Vector3(), out = new THREE.Vector3();
      Object.keys(map).forEach(function (k) {
        var idx = map[k];
        if (idx.length < 2) return;
        var groups = [];
        idx.forEach(function (i0) {
          v.set(nor.getX(i0), nor.getY(i0), nor.getZ(i0));
          var g = null, b;
          for (b = 0; b < groups.length; b++) if (groups[b].dir.dot(v) >= cosT) { g = groups[b]; break; }
          if (!g) { g = { dir: v.clone(), sum: new THREE.Vector3(), items: [] }; groups.push(g); }
          g.sum.add(v); g.items.push(i0); g.dir.copy(g.sum).normalize();
        });
        groups.forEach(function (g) {
          if (g.items.length < 2) return;
          out.copy(g.sum).normalize();
          g.items.forEach(function (i1) { nor.setXYZ(i1, out.x, out.y, out.z); });
        });
      });
      nor.needsUpdate = true;
      return geo;
    }
    function ex(shape, depth, mat, flat) {
      var g = new THREE.ExtrudeGeometry(shape, { depth: depth, bevelEnabled: false, curveSegments: SEG });
      weldNormals(g, 30);
      if (flat) g.rotateX(-Math.PI / 2);
      var m = new THREE.Mesh(g, mat);
      m.castShadow = m.receiveShadow = true;
      return m;
    }
    function box(w, h, d, mat) {
      var m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
      m.castShadow = m.receiveShadow = true;
      return m;
    }
    function poly(pts) {
      var s = new THREE.Shape();
      s.moveTo(pts[0][0], pts[0][1]);
      for (var i = 1; i < pts.length; i++) s.lineTo(pts[i][0], pts[i][1]);
      return s;
    }
    // ── НОЖКА GRIDFINITY: профиль СНЯТ С ОБРАЗЦА владельца (box-copy.stl — тот самый, по
    // которому печаталась шуфлядка в ванной, см. _geo_work/build5.py): 35.5 → фаска 0.8 →
    // 37.1 → вертикаль до 2.6 → фаска → 41.4 у верха; радиусы углов 0.8 / 1.6 / 3.75.
    // В дизайне на её месте стоял столбик 36.6×36.6 постоянного сечения — он читался как
    // кубик, не давал характерного «гриба» и не совпадал с тем, что реально печатается.
    var FOOT_PROFILE = [[35.5, 0.8, 0], [37.1, 1.6, 0.8], [37.1, 1.6, 2.6], [41.4, RAD_OUT, FOOT_H]];
    // Точки скруглённого квадрата. Число точек ОДИНАКОВО на всех уровнях профиля — только так
    // соседние кольца сшиваются один-к-одному без вееров и дыр.
    function rrectPts(w, r, n) {
      var k = w / 2 - r, pts = [], c, i, a;
      var cs = [[k, -k, -Math.PI / 2], [k, k, 0], [-k, k, Math.PI / 2], [-k, -k, Math.PI]];
      for (c = 0; c < 4; c++) for (i = 0; i <= n; i++) {
        a = cs[c][2] + Math.PI / 2 * i / n;
        pts.push([cs[c][0] + r * Math.cos(a), cs[c][1] + r * Math.sin(a)]);
      }
      return pts;
    }
    // Геометрия ножки строится ОДИН раз на монтирование и переиспользуется всеми клетками
    // (у сетки 11×6 их 66) — раньше на каждую клетку создавалась своя ExtrudeGeometry.
    var _footGeo = null;
    function footGeometry() {
      if (_footGeo) return _footGeo;
      var N = SEG, rings = FOOT_PROFILE.map(function (q) { return rrectPts(q[0], q[1], N); });
      var ys = FOOT_PROFILE.map(function (q) { return q[2]; });
      var M = rings[0].length, pos = [], L, i, j, a, b, c, d;
      var t = function (p1, y1, p2, y2, p3, y3) {
        pos.push(p1[0], y1, p1[1], p2[0], y2, p2[1], p3[0], y3, p3[1]);
      };
      for (L = 0; L < rings.length - 1; L++) for (i = 0; i < M; i++) {
        j = (i + 1) % M;
        a = rings[L][i]; b = rings[L][j]; c = rings[L + 1][i]; d = rings[L + 1][j];
        t(a, ys[L], b, ys[L], c, ys[L + 1]);
        t(b, ys[L], d, ys[L + 1], c, ys[L + 1]);
      }
      var lo = rings[0], hi = rings[rings.length - 1], yt = ys[ys.length - 1], O = [0, 0];
      for (i = 0; i < M; i++) {           // донышко и площадка под корпусом
        j = (i + 1) % M;
        t(O, 0, lo[j], 0, lo[i], 0);
        t(O, yt, hi[i], yt, hi[j], yt);
      }
      var geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
      geo.computeVertexNormals();
      weldNormals(geo, 30);   // дуги углов гладкие, фаски 45° остаются острыми гранями
      geo.userData.shared = true;   // одна на монтирование: disposeDeep её не трогает
      _footGeo = geo;
      return geo;
    }
    function buildBase(b, G) {
      var g = new THREE.Group(), W = b.W, D = b.D;
      var slab = box(W, 1.2, D, mats.socket); slab.position.y = .6; g.add(slab);
      var x0 = -W / 2 + G.mL, z0 = -D / 2 + G.mB;
      var rh = PLATE_H - 1.2;
      var ridgeGeo = function (len) { return new THREE.ExtrudeGeometry(poly([[-2.3, 0], [2.3, 0], [.95, rh], [-.95, rh]]), { depth: len, bevelEnabled: false }); };
      var xLines = [], i, j;
      for (i = 0; i <= G.cols; i++) { if (i === 0 && G.mL < 3) continue; if (i === G.cols && G.mR < 3) continue; xLines.push(x0 + i * PITCH); }
      xLines.push(-W / 2 + 2.3, W / 2 - 2.3);
      var zLines = [];
      for (j = 0; j <= G.rows; j++) { if (j === 0 && G.mB < 3) continue; if (j === G.rows && G.mF < 3) continue; zLines.push(z0 + j * PITCH); }
      zLines.push(-D / 2 + 2.3, D / 2 - 2.3);
      // Рёбра плиты — двумя InstancedMesh (вдоль X и вдоль Z) вместо ~20 отдельных мешей:
      // столько же пикселей, вчетверо меньше draw call'ов на каждый кадр.
      var mtxP = new THREE.Matrix4();
      var ridX = new THREE.InstancedMesh(ridgeGeo(D), mats.plate, xLines.length);
      ridX.castShadow = ridX.receiveShadow = true;
      xLines.forEach(function (x, k) { mtxP.makeTranslation(x, 1.2, -D / 2); ridX.setMatrixAt(k, mtxP); });
      ridX.instanceMatrix.needsUpdate = true; g.add(ridX);
      var ridZ = new THREE.InstancedMesh(ridgeGeo(W), mats.plate, zLines.length);
      ridZ.castShadow = ridZ.receiveShadow = true;
      var rotY = new THREE.Matrix4().makeRotationY(Math.PI / 2);
      zLines.forEach(function (z, k) {
        mtxP.makeTranslation(-W / 2, 1.2, z); mtxP.multiply(rotY); ridZ.setMatrixAt(k, mtxP);
      });
      ridZ.instanceMatrix.needsUpdate = true; g.add(ridZ);
      if (b.type === 'shelf') {
        var brd = box(W + 36, 18, D + 10, mats.wood); brd.position.y = -9; g.add(brd);
        var lip = box(W + 36, 30, 14, mats.wood2); lip.position.set(0, -3, -D / 2 - 2); g.add(lip);
      }
      if (b.type === 'drawer') {
        var H = b.H;
        var fl = box(W + 32, 16, D + 32, mats.wood2); fl.position.y = -8; g.add(fl);
        var mk = function (w, h, d, x, z, mat) { var m = box(w, h, d, mat || mats.wood); m.position.set(x, h / 2, z); g.add(m); };
        mk(16, H, D + 32, -(W / 2 + 8), 0); mk(16, H, D + 32, W / 2 + 8, 0);
        mk(W, H, 16, 0, -(D / 2 + 8)); mk(W, H + 16, 16, 0, D / 2 + 8, mats.wood2);
      }
      if (b.type === 'tandem') {
        var s = seriesOf(), h = s.h, P = PROF;
        var fl2 = box(W + 8, 16, D, mats.ply); fl2.position.y = -8; g.add(fl2);
        var side = function (mir) {
          var sh = poly([[W / 2, -14], [W / 2 + P.out, -14], [W / 2 + P.out, h], [W / 2 + P.inset, h], [W / 2 + P.inset, P.slopeY], [W / 2, P.foot]].map(function (p) { return [p[0] * mir, p[1]]; }));
          var m = ex(sh, D, mats.steel); m.position.z = -D / 2; g.add(m);
        };
        side(1); side(-1);
        var back = box(W + 2 * P.inset - 4, s.top, 12, mats.steel2); back.position.set(0, s.top / 2, -(D / 2 + 6)); g.add(back);
        var front = box(W + 2 * P.out + 40, s.top + 28, 16, mats.glass); front.castShadow = false; front.position.set(0, (s.top + 28) / 2 - 14, D / 2 + 10); g.add(front);
        s.rails.forEach(function (ry) {
          [-1, 1].forEach(function (sx) {
            var rail = new THREE.Mesh(new THREE.CylinderGeometry(4.5, 4.5, D - 16, 20), mats.steel);
            rail.rotation.x = Math.PI / 2; rail.position.set(sx * (W / 2 + P.inset + 6), ry - 5, 0); rail.castShadow = true; g.add(rail);
            [-1, 1].forEach(function (sz) {
              var post = new THREE.Mesh(new THREE.CylinderGeometry(2.6, 2.6, ry - 5 - h, 12), mats.steel2);
              post.position.set(sx * (W / 2 + P.inset + 6), h + (ry - 5 - h) / 2, sz * (D / 2 - 22)); post.castShadow = true; g.add(post);
            });
          });
        });
      }
      return g;
    }
    function buildBin(m, ctx) {
      var g = new THREE.Group(), mat = binMat(m.colorIdx), b = ctx.base;
      var H = m.hmm, i, j;
      // Все ножки модуля — ОДИН InstancedMesh: на плите 11×6 у десятка модулей это
      // десятки draw call'ов вместо сотен, а на мобильном GPU это и есть плавность.
      var feet = new THREE.InstancedMesh(footGeometry(), mat, m.w * m.d);
      feet.castShadow = feet.receiveShadow = true;
      var mtx = new THREE.Matrix4(), fi = 0;
      for (i = 0; i < m.w; i++) for (j = 0; j < m.d; j++) {
        mtx.makeTranslation((i - (m.w - 1) / 2) * PITCH, 0, (j - (m.d - 1) / 2) * PITCH);
        feet.setMatrixAt(fi++, mtx);
      }
      feet.instanceMatrix.needsUpdate = true;
      // Ножки и плита основания — СПЛОШНЫЕ тела: в слайсере это инфилл с парой сплошных
      // слоёв, а не литой пластик. Считаем их объём отдельно (userData.bulk), иначе
      // оценка расхода завышалась почти вдвое: у бокса 2×2 плита дна — три четверти меша.
      feet.userData.bulk = 1;
      g.add(feet);
      var bd = binDims(m), e = bd.e, ow = bd.ow, od = bd.od, ox = bd.ox, oz = bd.oz;
      var anyExt = e.L || e.R || e.B || e.F;
      var tanL = b.type === 'tandem' && e.L > 0, tanR = b.type === 'tandem' && e.R > 0;
      // ОСНОВАНИЕ (4.75…5.96) — сплошная скруглённая призма, ровно как разность призм в
      // нашем генераторе (_geo_work/build5.py). В дизайне вместо неё стоял ПРЯМОУГОЛЬНЫЙ
      // box-пол: в скруглённых углах он не доставал до стенки (щель, которую видно насквозь),
      // свисал на 0.65 мм ниже низа корпуса и совпадал со стенкой гранью в 0.1 мм — отсюда
      // полосы z-fighting по низу борта.
      var R_O = anyExt ? 2.4 : RAD_OUT;
      var base = ex(rrect(ow, od, R_O, ox, -oz), FLOOR_TOP - FOOT_H, mat, true);
      base.position.y = FOOT_H; base.userData.bulk = 1; g.add(base);
      if (!tanL && !tanR) {
        var outer = rrect(ow, od, R_O, ox, -oz);
        outer.holes.push(rrect(ow - 2 * WALL_T, od - 2 * WALL_T, RAD_IN, ox, -oz));
        var bev = .25, wgeo = new THREE.ExtrudeGeometry(outer, { depth: (H - FLOOR_TOP + .4) - 2 * bev,
          bevelEnabled: true, bevelThickness: bev, bevelSize: bev, bevelSegments: 1, bevelOffset: 0,
          curveSegments: anyExt ? SEG_SMALL : SEG });
        weldNormals(wgeo, 30); wgeo.rotateX(-Math.PI / 2);
        var wall = new THREE.Mesh(wgeo, mat);
        wall.castShadow = wall.receiveShadow = true;
        wall.position.y = FLOOR_TOP - .4;   // полная высота = depth + 2·bev → верх ровно на H
        g.add(wall);
      } else {
        var P = PROF, t = 1.8, wh0 = H - FOOT_H;
        var owr = ow - (tanL ? e.L : 0) - (tanR ? e.R : 0), oxr = ox + (tanL ? e.L : 0) / 2 - (tanR ? e.R : 0) / 2;
        [-1, 1].forEach(function (sz) {
          var bw = box(owr, wh0, WALL_T, mat);
          bw.position.set(oxr, FOOT_H + wh0 / 2, oz + sz * (od / 2 - WALL_T / 2)); g.add(bw);
        });
        var ko = function (y) { return P.inset * Math.min(1, Math.max(0, y) / P.slopeY); };
        var band = function (sign, gp) {
          var pts = [[-3, FOOT_H], [gp + ko(FOOT_H), FOOT_H]];
          if (H > P.slopeY) pts.push([gp + P.inset, P.slopeY], [gp + P.inset, H], [gp + P.inset - t, H], [gp + P.inset - t, P.slopeY]);
          else pts.push([gp + ko(H), H], [gp + ko(H) - t, H]);
          pts.push([gp + ko(FLOOR_TOP) - t, FLOOR_TOP], [-3, FLOOR_TOP]);
          var mm = ex(poly(pts.map(function (q) { return [q[0] * sign, q[1]]; })), od, mat);
          mm.position.set(oxr + sign * owr / 2, 0, oz - od / 2); g.add(mm);
          var cp = [[0, FOOT_H], [gp + ko(FOOT_H), FOOT_H]];
          if (H > P.slopeY) cp.push([gp + P.inset, P.slopeY], [gp + P.inset, H]); else cp.push([gp + ko(H), H]);
          cp.push([0, H]);
          [0, od - WALL_T].forEach(function (z) {
            var cap = ex(poly(cp.map(function (q) { return [q[0] * sign, q[1]]; })), WALL_T, mat);
            cap.position.set(oxr + sign * owr / 2, 0, oz - od / 2 + z); g.add(cap);
          });
        };
        var sideW = function (sign) {
          var bx = box(WALL_T, wh0, od, mat);
          bx.position.set(oxr + sign * (owr / 2 - WALL_T / 2), FOOT_H + wh0 / 2, oz); g.add(bx);
        };
        if (tanL) band(-1, e.L); else sideW(-1);
        if (tanR) band(1, e.R); else sideW(1);
      }
      var y0 = FLOOR_TOP, yT = Math.max(y0 + 2.5, H - 4.85), dh = yT - y0;
      var wh = Math.max(3, Math.min(m.wallH || dh, dh));
      var X = function (p) { return ox - ow / 2 + p; }, Z = function (p) { return oz - od / 2 + p; };
      bakeWalls(m).forEach(function (w, wi) {
        var dx = w.x1 - w.x0, dz = w.y1 - w.y0, len = Math.hypot(dx, dz);
        if (len < 1) return;
        var t2 = w.t || DIV_T, bx = box(t2, wh, len + t2, mat);
        bx.position.set(X((w.x0 + w.x1) / 2), y0 + wh / 2, Z((w.y0 + w.y1) / 2));
        bx.rotation.y = Math.atan2(dx, dz); bx.userData.wall = wi; g.add(bx);
      });
      (m.islands || []).forEach(function (s, si) {
        if (!s.pts || s.pts.length < 3) return;
        var sh = poly(s.pts.map(function (p) { return [X(p[0]), -Z(p[1])]; }));
        var hh = Math.max(2, Math.min(s.hh || 10, dh));
        var mm = ex(sh, hh, mat, true); mm.position.y = y0; mm.userData.island = si; g.add(mm);
      });
      (m.items || []).forEach(function (it, ii) {
        var ig = new THREE.Group(); ig.userData.item = ii; g.add(ig);
        var cx = X(it.x + it.w / 2), cz = Z(it.y + it.h / 2);
        if (it.t === 'platform') {
          var hh1 = Math.min(it.hh || 10, dh - 2);
          var bx1 = box(it.w, hh1, it.h, mat); bx1.position.set(cx, y0 + hh1 / 2, cz); ig.add(bx1);
        }
        if (it.t === 'scoop') {
          var r1 = Math.min(it.hh || 14, dh - 2);
          var sh1 = poly([[-it.h / 2, 0], [it.h / 2, 0], [-it.h / 2, r1]]);
          var wd = ex(sh1, it.w, mat); wd.rotation.y = Math.PI / 2; wd.position.set(cx - it.w / 2, y0, cz); ig.add(wd);
        }
        if (it.t === 'pins') {
          var r2 = 2.6, hh2 = Math.min(it.hh || 20, dh - 1);
          var nx = Math.max(1, Math.round(it.w / (it.step || 12))), ny = Math.max(1, Math.round(it.h / (it.step || 12)));
          for (var a = 0; a < nx; a++) for (var b2 = 0; b2 < ny; b2++) {
            var p = new THREE.Mesh(new THREE.CylinderGeometry(r2, r2 + .5, hh2, 14), mat);
            p.castShadow = p.receiveShadow = true;
            p.position.set(X(it.x + (a + .5) * it.w / nx), y0 + hh2 / 2, Z(it.y + (b2 + .5) * it.h / ny)); ig.add(p);
          }
        }
        if (it.t === 'label') {
          var tl = 12, sh2 = poly([[0, 0], [tl, 0], [tl, -3.2], [0, -3.2]]);
          var lip = ex(sh2, it.w, mat); lip.rotation.y = -Math.PI / 2; lip.position.set(cx + it.w / 2, yT, cz - it.h / 2 + 0.1); ig.add(lip);
        }
        if (it.t === 'cups') {
          // Стакан с НАСТОЯЩЕЙ стенкой и фаской по кромке (LatheGeometry — ядро three)
          // вместо трубы нулевой толщины с DoubleSide: она сама себе бросала тень муаром,
          // а на каждое гнездо уходило по две геометрии и по два меша. Теперь одна
          // геометрия на вставку и один InstancedMesh на все гнёзда.
          var step = Math.max(8, it.step || 15), rr = Math.max(2.5, (it.dia || 9) / 2);
          var wr = Math.max(rr + .5, Math.min(rr + 1.3, step / 2 - .3));
          var hh3 = Math.max(3, Math.min(it.hh || 22, dh));
          var nx2 = Math.max(1, Math.floor((it.w + 3) / step)), ny2 = Math.max(1, Math.floor((it.h + 3) / step));
          var prof = [new THREE.Vector2(0, 1.4), new THREE.Vector2(rr, 1.4),
            new THREE.Vector2(rr, hh3 - .45), new THREE.Vector2(rr + .35, hh3),
            new THREE.Vector2(wr, hh3), new THREE.Vector2(wr, 0)];
          var cupGeo = new THREE.LatheGeometry(prof, 16);
          cupGeo.computeVertexNormals(); weldNormals(cupGeo, 30);
          var cups = new THREE.InstancedMesh(cupGeo, mat, nx2 * ny2);
          cups.castShadow = cups.receiveShadow = true;
          var mtxC = new THREE.Matrix4(), ci = 0;
          for (var a2 = 0; a2 < nx2; a2++) for (var b3 = 0; b3 < ny2; b3++) {
            mtxC.makeTranslation(X(it.x + (a2 + .5) * it.w / nx2), y0, Z(it.y + (b3 + .5) * it.h / ny2));
            cups.setMatrixAt(ci++, mtxC);
          }
          cups.instanceMatrix.needsUpdate = true;
          ig.add(cups);
        }
        if (it.t === 'vslot') {
          var hh4 = Math.min(it.hh || 24, dh), vd = Math.min(it.vd || 14, hh4 - 3);
          var alongX = it.w >= it.h, a3 = (alongX ? it.h : it.w) / 2, L = alongX ? it.w : it.h;
          var sh3 = poly([[-a3, 0], [a3, 0], [a3, hh4], [0, hh4 - vd], [-a3, hh4]]);
          var pr = ex(sh3, L, mat);
          if (alongX) { pr.rotation.y = Math.PI / 2; pr.position.set(X(it.x), y0, Z(it.y + it.h / 2)); }
          else pr.position.set(cx, y0, Z(it.y));
          ig.add(pr);
        }
        ig.traverse(function (o) { if (o.isMesh) { o.castShadow = o.receiveShadow = true; } });
      });
      g.traverse(function (o) { o.userData.mid = m.id; });
      g.userData = { mid: m.id };
      return g;
    }
    function buildWing(m, G) {
      var g = new THREE.Group(), mir = m.side === 'R' ? -1 : 1, P = PROF;
      var L = m.d * PITCH - .5, h = m.hmm;
      var c = .7, gap = m.side === 'L' ? G.mL : G.mR, bOut = Math.max(c + 8, gap - .6);
      var t = 1.8, ft = 3;
      var xw = function (y) { return c - P.inset * Math.min(1, Math.max(0, y / P.slopeY)); };
      var outerUp = [];
      if (h <= P.slopeY) outerUp.push([xw(h), h]);
      else { outerUp.push([xw(P.slopeY), P.slopeY], [xw(h), h]); }
      var capPts = [[c, 0], [bOut, 0], [bOut, h]].concat(outerUp.slice().reverse());
      var innerUp = [];
      if (h <= P.slopeY) innerUp.push([xw(h) + t, h]);
      else innerUp.push([xw(P.slopeY) + t, P.slopeY], [xw(h) + t, h]);
      var uPts = [[c, 0], [bOut, 0], [bOut, h], [bOut - t, h], [bOut - t, ft], [xw(ft) + t, ft]].concat(innerUp, outerUp.slice().reverse());
      var mk = function (pts) { return poly(pts.map(function (q) { return [q[0] * mir, q[1]]; })); };
      var mat = binMat(m.colorIdx);
      var body = ex(mk(uPts), L, mat); g.add(body);
      var capF = ex(mk(capPts), 2, mat); g.add(capF);
      var capB = ex(mk(capPts), 2, mat); capB.position.z = L - 2; g.add(capB);
      for (var k = 1; k < m.comp; k++) { var cc = ex(mk(capPts), DIV_T + .8, mat); cc.position.z = k * L / m.comp; g.add(cc); }
      g.traverse(function (o) { o.userData.mid = m.id; if (o.isMesh) { o.castShadow = o.receiveShadow = true; } });
      g.userData = { mid: m.id };
      return g;
    }

    // ════════════════ DOM ════════════════
    host.innerHTML = [
      '<div class="gfx">',
      // ОДНА строка управления. Было две: виды камеры с «Сбросом» и фильтр вида — на
      // телефоне они съедали сотню пикселей у сцены и обе выглядели служебными. Виды уехали
      // поверх сцены (.g-vtools), «К спецификации» убрана как дубль вкладки над ней, фильтр
      // сжат до сегмента с короткими подписями.
      '<div class="g-top">',
      '<span id="gfxChip" class="g-chip g-mono"></span>',
      '<div id="gfxFilt" class="g-seg g-segf" hidden>',
      '<button class="on" data-f="all" type="button">Всё</button>',
      '<button data-f="draft" type="button">Черновик</button>',
      '<button data-f="ordered" type="button">Заказ</button>',
      '</div>',
      '<span class="g-sp"></span>',
      '<button id="gfxClear" class="g-tbtn warn" type="button">Сброс</button>',
      '<button id="gfxOrder" class="g-tbtn acc" type="button">Оформить заявку</button>',
      '</div>',
      '<div class="g-wrap">',
      '<aside id="gfxLeft" class="g-panel g-left"><div class="g-phead">База<button class="g-pclose" data-close="gfxLeft" type="button">✕</button></div><div id="gfxBase" class="g-pbody"></div></aside>',
      '<main id="gfxViewport" class="g-viewport">',
      '<div class="g-vtools"><button id="gfxIso" type="button">3/4</button>',
      '<button id="gfxTopV" type="button">Сверху</button></div>',
      '<div id="gfxHint" class="g-chip g-hint" hidden></div>',
      '<div id="gfxSummary" class="g-chip g-summary"></div>',
      '<span id="gfxLegend" class="g-chip g-mono g-legend"></span>',
      '</main>',
      // Палитра и параметры выбранной детали — РАЗНЫЕ шторки: раньше они лежали в одной
      // «Модули», и чтобы поправить высоту лотка, надо было проматывать мимо всей палитры.
      '<div class="g-rcol">',
      '<aside id="gfxRight" class="g-panel g-right"><div class="g-phead">Модули<button class="g-pclose" data-close="gfxRight" type="button">✕</button></div><div class="g-pbody"><div id="gfxPalette"></div></div></aside>',
      '<aside id="gfxPart" class="g-panel g-right"><div class="g-phead">Деталь<button class="g-pclose" data-close="gfxPart" type="button">✕</button></div><div class="g-pbody"><div id="gfxSel"></div></div></aside>',
      '</div>',
      '</div>',
      '<nav id="gfxMtabs" class="g-mtabs"><button data-p="gfxLeft" type="button">⚙ База</button>',
      '<button data-p="gfxRight" type="button">▦ Модули</button>',
      '<button data-p="gfxPart" type="button">✎ Деталь</button></nav>',
      '</div>'].join('');
    var $ = function (s) { return host.querySelector(s); };

    // ════════════════ главная сцена (порт app.js) ════════════════
    var vp = $('#gfxViewport');
    // Телефон рисует ту же сцену на слабом GPU: режем плотность пикселей и карту теней.
    // 1.6 DPR на экране 3x визуально неотличим, а кадров даёт вдвое больше.
    var LOWEND = false;
    try { LOWEND = window.matchMedia('(max-width:820px)').matches; } catch (e) {}
    var renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, LOWEND ? 1.6 : 2));
    renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    // Сцена статична между действиями: depth-pass 2048² каждые 25 мс — чистая трата
    // батареи. Взводим карту только там, где геометрия/свет реально изменились.
    renderer.shadowMap.autoUpdate = false; renderer.shadowMap.needsUpdate = true;
    renderer.outputEncoding = THREE.sRGBEncoding;   // см. «ЦВЕТОУПРАВЛЕНИЕ» ниже
    // r147 в легаси-режиме домножает свет на π (в r184 прототипа этого нет) — без флага
    // сцена ярче эталона примерно втрое, дерево ящика уходит в рыжий
    renderer.physicallyCorrectLights = true;
    vp.appendChild(renderer.domElement);
    var scene = new THREE.Scene();
    scene.background = C('#0a0e12');
    scene.fog = new THREE.Fog(C('#0a0e12'), 1400, 3200);
    var camera = new THREE.PerspectiveCamera(40, 1, 1, 6000);
    var controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = .08; controls.maxPolarAngle = 1.5;
    controls.minDistance = 120; controls.maxDistance = 2600;
    scene.add(new THREE.HemisphereLight(C('#a9bdd4'), C('#1c242e'), .62));
    var sun = new THREE.DirectionalLight(C('#fff8ef'), 1.0);
    sun.position.set(260, 460, 200); sun.castShadow = true;
    sun.shadow.mapSize.set(LOWEND ? 1024 : 2048, LOWEND ? 1024 : 2048);
    sun.shadow.bias = -.00025; sun.shadow.normalBias = 0.4;
    // Направление света НЕ трогаем (target остаётся в начале координат) — меняем только
    // охват и дальность, иначе поедет характер кадра.
    function fitSun() {
      var b = state.base;
      var R = .5 * Math.hypot(b.W, b.D) + innerH() * .35 + 40;
      sun.position.set(260, 460, 200).setLength(R * 1.9);
      var c = sun.shadow.camera;
      c.left = -R; c.right = R; c.top = R; c.bottom = -R; c.near = 1; c.far = R * 3.2;
      c.updateProjectionMatrix();
    }
    fitSun();
    scene.add(sun);
    var fill = new THREE.DirectionalLight(C('#6a86a8'), .4); fill.position.set(-320, 220, -260); scene.add(fill);
    var fill2 = new THREE.DirectionalLight(C('#8fa5bd'), .35); fill2.position.set(60, 260, 460); scene.add(fill2);
    var ground = new THREE.Mesh(new THREE.CircleGeometry(2200, 48), mats.ground);
    ground.rotation.x = -Math.PI / 2; ground.position.y = -18.05; ground.receiveShadow = true; scene.add(ground);
    // Контактная тень: мягкое пятно под ящиком. Тёмный центр прячется под корпусом
    // (пятно ниже дна), наружу выходит только растекание — сцена перестаёт «висеть».
    var contactTex = (function () {
      var cv = document.createElement('canvas'); cv.width = cv.height = 128;
      var g2 = cv.getContext('2d');
      var grd = g2.createRadialGradient(64, 64, 0, 64, 64, 64);
      for (var i = 0; i <= 10; i++) {
        var t = i / 10;
        grd.addColorStop(t, 'rgba(0,0,0,' + (0.45 * Math.pow(1 - t, 1.8)).toFixed(3) + ')');
      }
      g2.fillStyle = grd; g2.fillRect(0, 0, 128, 128);
      return new THREE.CanvasTexture(cv);
    })();
    mats.contact = new THREE.MeshBasicMaterial({ map: contactTex, transparent: true, opacity: .85, depthWrite: false });
    var contact = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), mats.contact);
    contact.rotation.x = -Math.PI / 2; contact.position.y = -17.99; scene.add(contact);
    function fitContact() {
      var b = state.base;
      contact.scale.set((b.W + 40) * 1.55, (b.D + 40) * 1.55, 1);
    }
    fitContact();

    // СТУДИЙНОЕ ОКРУЖЕНИЕ. У MeshStandardMaterial без environment нет ни одного отражения —
    // пластик выглядит «нарисованным». Собираем крошечную сцену-софтбокс и прогоняем её через
    // PMREMGenerator (он в ЯДРЕ three, никаких внешних файлов): пластик получает мягкие блики
    // и глубину, а стоит это одну генерацию 256px при монтировании.
    function studioEnv(rnd) {
      var pm = new THREE.PMREMGenerator(rnd);
      var es = new THREE.Scene();
      es.background = C('#161d26');
      var add = function (w, h, pos, rot, col) {
        var q = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color: C(col) }));
        q.position.set(pos[0], pos[1], pos[2]); q.rotation.set(rot[0], rot[1], 0); es.add(q);
      };
      add(900, 900, [0, 400, 0], [Math.PI / 2, 0], '#dce8fa');        // потолочный свет
      add(500, 380, [-420, 60, 0], [0, Math.PI / 2], '#8fa7c8');      // левый софтбокс
      add(500, 380, [420, 60, 0], [0, -Math.PI / 2], '#4d5a70');      // правый добор
      add(700, 400, [0, 40, -420], [0, 0], '#2b3646');                // задник
      var tex = pm.fromScene(es, 0.03).texture;
      pm.dispose();
      es.traverse(function (o) { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
      return tex;
    }
    var envTex = studioEnv(renderer);   // для главного контекста
    scene.environment = envTex;
    var envTexEd = null;                // для контекста редактора — СВОЯ, см. initEditor3D

    var baseG = null, modsG = new THREE.Group();
    scene.add(modsG);
    var ghost = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), mats.ghostOk);
    ghost.visible = false; scene.add(ghost);
    // Ghost был просто полупрозрачным боксом, прыгающим по 42 мм: высокий модуль
    // закрывал собой те самые ячейки, куда целишься. Добавляем чёткие рёбра, след на
    // плите (видно, что именно будет занято) и плавное довождение до снапнутой позиции.
    mats.ghostEdgeOk = new THREE.LineBasicMaterial({ color: C('#2dd47e') });
    mats.ghostEdgeBad = new THREE.LineBasicMaterial({ color: C('#ff5b4d') });
    mats.ghostFootOk = new THREE.MeshBasicMaterial({ color: C('#2dd47e'), transparent: true, opacity: .22, depthWrite: false });
    mats.ghostFootBad = new THREE.MeshBasicMaterial({ color: C('#ff5b4d'), transparent: true, opacity: .22, depthWrite: false });
    var ghostEdges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1)), mats.ghostEdgeOk);
    ghostEdges.visible = false; scene.add(ghostEdges);
    var ghostFoot = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), mats.ghostFootOk);
    ghostFoot.rotation.x = -Math.PI / 2; ghostFoot.visible = false; scene.add(ghostFoot);
    var ghostGoal = new THREE.Vector3(), ghostFresh = true;
    function showGhost(ok, sx, sy, sz, x, y, z) {
      ghost.scale.set(sx, sy, sz);
      ghost.material = ok ? mats.ghostOk : mats.ghostBad;
      ghostEdges.scale.set(sx, sy, sz);
      ghostEdges.material = ok ? mats.ghostEdgeOk : mats.ghostEdgeBad;
      ghostFoot.scale.set(sx, sz, 1);
      ghostFoot.material = ok ? mats.ghostFootOk : mats.ghostFootBad;
      ghostGoal.set(x, y, z);
      if (ghostFresh || !ghost.visible) { ghost.position.copy(ghostGoal); ghostFresh = false; }
      ghostEdges.position.copy(ghost.position);
      ghostFoot.position.set(ghost.position.x, PLATE_H + .1, ghost.position.z);
      ghost.visible = ghostEdges.visible = ghostFoot.visible = true;
    }
    function hideGhost() { ghost.visible = ghostEdges.visible = ghostFoot.visible = false; ghostFresh = true; }
    var selPad = new THREE.Mesh(new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ color: C('#ff7a1a'), transparent: true, opacity: .2, depthWrite: false }));
    selPad.rotation.x = -Math.PI / 2; selPad.visible = false; scene.add(selPad);
    var selRing = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.PlaneGeometry(1, 1)),
      new THREE.LineBasicMaterial({ color: C('#ffa15c'), transparent: true, opacity: .9 }));
    selRing.rotation.x = -Math.PI / 2; selRing.visible = false; scene.add(selRing);
    var G = gridInfo();
    var x0 = function () { return -state.base.W / 2 + G.mL; };
    var z0 = function () { return -state.base.D / 2 + G.mB; };

    // shared-геометрия (ножка) переживает пересборку сцены — освобождается только в destroy()
    function disposeDeep(o) {
      o.traverse(function (c) { if (c.geometry && !(c.geometry.userData && c.geometry.userData.shared)) c.geometry.dispose(); });
    }

    // ── объём пластика модуля: единственная честная цифра, из которой сервер считает «≈» ──
    // Слайсинг в клиентских расчётах выключен намеренно, а меш у нас уже построен — берём
    // объём прямо с него (сумма знаковых объёмов тетраэдров по треугольникам). У бокса
    // стенка 0.8 мм — это два периметра, инфилла нет, поэтому объём меша и есть расход.
    var _va = new THREE.Vector3(), _vb = new THREE.Vector3(), _vc = new THREE.Vector3();
    function geoVolume(geo) {
      var pos = geo.getAttribute && geo.getAttribute('position');
      if (!pos) return 0;
      var idx = geo.getIndex(), n = idx ? idx.count : pos.count, v = 0;
      for (var i = 0; i + 2 < n; i += 3) {
        var i0 = idx ? idx.getX(i) : i, i1 = idx ? idx.getX(i + 1) : i + 1, i2 = idx ? idx.getX(i + 2) : i + 2;
        _va.fromBufferAttribute(pos, i0); _vb.fromBufferAttribute(pos, i1); _vc.fromBufferAttribute(pos, i2);
        v += _va.dot(_vb.cross(_vc)) / 6;
      }
      return Math.abs(v);
    }
    function objVolume(root) {
      var total = 0, bulk = 0;
      root.traverse(function (o) {
        if (!o.isMesh || !o.geometry || (o.userData && o.userData.noVolume)) return;
        var s2 = o.scale, k = Math.abs(s2.x * s2.y * s2.z);
        var v = geoVolume(o.geometry) * k * (o.isInstancedMesh ? o.count : 1);
        total += v;
        if (o.userData && o.userData.bulk) bulk += v;
      });
      return { v: total, b: bulk };   // мм³: весь меш и его сплошная часть
    }
    // Ключ кэша объёма: геометрия модуля ПЛЮС база (доборы крайних модулей зависят от
    // остатков сетки). Цвет и сам объём в ключ не входят — перекраска пересчёта не требует.
    function modKey(m) {
      var out = {};
      Object.keys(m).sort().forEach(function (k) {
        if (k !== 'id' && k !== 'colorIdx' && k !== 'vol' && k !== '_new') out[k] = m[k];
      });
      return JSON.stringify(out) + '|' + JSON.stringify(state.base);
    }
    // Кэш объёма по подписи модуля. Ключей у долгой сессии копится много (каждое
    // движение ползунка высоты — новый), поэтому чистим по достижении потолка целиком:
    // пересчёт стоит миллисекунды, а утечка памяти на телефоне — сессию.
    var volCache = {}, volKeys = 0;
    function rebuildScene() {
      G = gridInfo();
      if (baseG) { scene.remove(baseG); disposeDeep(baseG); }
      baseG = buildBase(state.base, G); scene.add(baseG);
      fitSun(); fitContact();
      rebuildMods(); frame();
      renderer.shadowMap.needsUpdate = true;
    }
    var pops = [];   // твины «модуль вырастает»: {g, t0}
    function rebuildMods() {
      if (state.editing) return;
      while (modsG.children.length) { var c = modsG.children.pop(); disposeDeep(c); }
      pops.length = 0;
      hovRing.visible = false; hovId = null;
      var volDirty = false;
      state.modules.forEach(function (m) {
        var g = m.kind === 'wing' ? buildWing(m, G) : buildBin(m, { G: G, base: state.base });
        // объём считаем ДО pop-анимации (она масштабирует группу) и кэшируем по подписи
        var key = modKey(m), vol = volCache[key];
        if (vol == null) {
          if (volKeys > 400) { volCache = {}; volKeys = 0; }
          vol = objVolume(g); volCache[key] = vol; volKeys++;
        }
        var vr = Math.round(vol.v / 10) / 100, br = Math.round(vol.b / 10) / 100;
        if (m.vol !== vr) { m.vol = vr; volDirty = true; }
        if (m.volb !== br) { m.volb = br; volDirty = true; }
        // Служебный флаг «только что поставлен» снимаем ВСЕГДА, даже если модуль сейчас
        // скрыт фильтром: иначе он уезжал в раскладку, попадал в подпись геометрии — и
        // деталь форкалась в v2 сама по себе, без единой правки клиента.
        var fresh = !!m._new;
        if (fresh) delete m._new;
        if (!visibleMod(m)) { disposeDeep(g); return; }   // фильтр вида: «что мне ещё осталось»
        if (m.kind === 'wing') g.position.set(m.side === 'L' ? -state.base.W / 2 : state.base.W / 2, PLATE_H, z0() + m.cy * PITCH);
        else g.position.set(x0() + (m.cx + m.w / 2) * PITCH, SEAT, z0() + (m.cy + m.d / 2) * PITCH);
        if (fresh) { g.scale.set(.86, .5, .86); pops.push({ g: g, t0: performance.now() }); }
        modsG.add(g);
      });
      rebuildBadges();
      updateSelBox();
      renderer.shadowMap.needsUpdate = true;
      // объём — производная геометрии: сервер берёт его из раскладки, значит раскладку надо
      // сохранить. save() дебаунсит, поэтому лишнего трафика это не даёт.
      if (volDirty) save();
    }
    // 200 мс с лёгким перелётом (back-out): модуль «садится» в ячейку, а не возникает рывком
    function stepPops(now) {
      for (var i = pops.length - 1; i >= 0; i--) {
        var t = Math.min(1, (now - pops[i].t0) / 200);
        var e = 1 + 1.9 * Math.pow(t - 1, 3) + 0.9 * Math.pow(t - 1, 2);
        pops[i].g.scale.set(.86 + .14 * e, .5 + .5 * e, .86 + .14 * e);
        if (t >= 1) { pops[i].g.scale.set(1, 1, 1); pops.splice(i, 1); }
      }
      renderer.shadowMap.needsUpdate = true;   // масштаб меняется — тень обязана поспевать
    }
    // Габариты берём ИЗ ДАННЫХ модуля, а не из Box3.setFromObject: ножки теперь
    // InstancedMesh, и подсчёт по сцене давал неверную коробку (подсветка не появлялась).
    function modFootprint(m) {
      if (m.kind === 'wing') {
        var gap = Math.max(6, wingGap(m.side) - 1);
        return { cx: (m.side === 'L' ? -state.base.W / 2 + gap / 2 : state.base.W / 2 - gap / 2),
          cz: z0() + (m.cy + m.d / 2) * PITCH, w: gap, d: m.d * PITCH, top: PLATE_H + m.hmm };
      }
      var bd2 = binDims(m);
      return { cx: x0() + (m.cx + m.w / 2) * PITCH + bd2.ox,
        cz: z0() + (m.cy + m.d / 2) * PITCH + bd2.oz,
        w: bd2.ow, d: bd2.od, top: SEAT + m.hmm };
    }
    // ── метки состояния деталей: заказанное видно прямо в сцене ──
    // Кольцо по верхней кромке (цветом состояния) плюс бейдж «#134». Цвет модуля НЕ
    // приглушаем: клиенту важен реальный цвет пластика, а приглушение читается как
    // «выключено». Материалы и текстуры кэшируются — состояний всего три.
    var badgeG = new THREE.Group(); scene.add(badgeG);
    var partsMap = {}, filterMode = 'all';
    var _ringGeo = new THREE.EdgesGeometry(new THREE.PlaneGeometry(1, 1));
    var _ringMat = {}, _labTex = {}, _labMat = {};
    var STATE_COLOR = { ordered: '#3fb6c9', in_progress: '#3fb6c9', awaiting_payment: '#3fb6c9',
      printed: '#2dd47e', sel: '#ff7a1a' };
    function ringMat(hex) {
      if (!_ringMat[hex]) _ringMat[hex] = new THREE.LineBasicMaterial({ color: C(hex), transparent: true, opacity: .95 });
      return _ringMat[hex];
    }
    function labelSprite(text, hex) {
      var key = text + '|' + hex;
      if (!_labTex[key]) {
        var cv = document.createElement('canvas'); cv.width = 128; cv.height = 64;
        var cx = cv.getContext('2d');
        cx.fillStyle = 'rgba(13,18,20,.82)';
        cx.beginPath(); cx.roundRect ? cx.roundRect(4, 12, 120, 40, 10) : cx.rect(4, 12, 120, 40); cx.fill();
        cx.fillStyle = hex; cx.font = 'bold 26px system-ui,sans-serif';
        cx.textAlign = 'center'; cx.textBaseline = 'middle';
        cx.fillText(text, 64, 33);
        var t = new THREE.CanvasTexture(cv);
        t.anisotropy = 2;
        // Рендерер пишет в sRGB (см. блок «ЦВЕТОУПРАВЛЕНИЕ»): без явной кодировки
        // canvas-текстура выцветает, и цифра бейджа перестаёт совпадать по цвету с
        // кольцом рядом, которое построено через C(hex) и потому корректно.
        t.encoding = THREE.sRGBEncoding;
        _labTex[key] = t;
        _labMat[key] = new THREE.SpriteMaterial({ map: t, transparent: true, depthTest: false });
      }
      var sp = new THREE.Sprite(_labMat[key]);
      sp.scale.set(34, 17, 1);
      return sp;
    }
    function partOf(m) { return partsMap[m.id] || null; }
    function visibleMod(m) {
      if (filterMode === 'all') return true;
      var p = partOf(m);
      var live = p ? (p.live_state || p.state) : 'draft';
      var busy = live === 'ordered' || live === 'in_progress' || live === 'awaiting_payment' || live === 'printed';
      return filterMode === 'ordered' ? busy : !busy;
    }
    function rebuildBadges() {
      // Материалы спрайтов и колец КЭШИРОВАНЫ — здесь только отцепляем, иначе каждая
      // перестройка меток компилировала бы шейдер заново.
      while (badgeG.children.length) badgeG.children.pop();
      state.modules.forEach(function (m) {
        var p = partOf(m);
        if (!p || !visibleMod(m)) return;
        var live = p.live_state || p.state;
        var hex = STATE_COLOR[live] || (p.sel ? STATE_COLOR.sel : null);
        if (!hex) return;
        var f = modFootprint(m);
        var ring = new THREE.LineSegments(_ringGeo, ringMat(hex));
        ring.rotation.x = -Math.PI / 2;
        ring.scale.set(f.w + 1.6, f.d + 1.6, 1);
        ring.position.set(f.cx, f.top + .35, f.cz);
        ring.userData.noVolume = 1;
        badgeG.add(ring);
        if (live !== 'draft' && live !== 'hold' && p.request_id) {
          var sp = labelSprite('#' + p.request_id, hex);
          sp.position.set(f.cx, f.top + 9, f.cz);
          badgeG.add(sp);
        }
      });
    }
    function updateSelBox() {
      var m = selected();
      if (!m) { selPad.visible = selRing.visible = false; return; }
      var f = modFootprint(m);
      // подложка ВЫСТУПАЕТ за корпус — иначе её целиком закрывает сам модуль
      selPad.scale.set(f.w + 11, f.d + 11, 1); selPad.position.set(f.cx, PLATE_H + .14, f.cz); selPad.visible = true;
      // рамка — по ВЕРХНЕЙ кромке: обводит модуль, но не перечёркивает его, как каркас
      selRing.scale.set(f.w + 2.4, f.d + 2.4, 1); selRing.position.set(f.cx, f.top + .6, f.cz); selRing.visible = true;
      if (hovRing.visible && m.id === hovId) hovRing.visible = false;
    }
    // Наведение мышью: приглушённая рамка + курсор-палец, чтобы было видно, что модуль
    // кликабелен. Только для мыши и с троттлингом — на тач-экранах наведения нет.
    var hovRing = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.PlaneGeometry(1, 1)),
      new THREE.LineBasicMaterial({ color: C('#95a3b2'), transparent: true, opacity: .55 }));
    hovRing.rotation.x = -Math.PI / 2; hovRing.visible = false; scene.add(hovRing);
    var hovId = null, hovT = 0;
    function updateHover(e) {
      if (performance.now() - hovT < 60) return;
      hovT = performance.now();
      var m = pickModule(e);
      var id = m && m.id !== state.sel ? m.id : null;
      renderer.domElement.style.cursor = m ? 'pointer' : '';
      if (id === hovId) return;
      hovId = id;
      if (!id) { hovRing.visible = false; return; }
      var f = modFootprint(m);
      hovRing.scale.set(f.w + 2.4, f.d + 2.4, 1);
      hovRing.position.set(f.cx, f.top + .6, f.cz);
      hovRing.visible = true;
    }

    var camGoal = null, userMoved = false, lastTop = false;
    function frame(top) {
      var b = state.base;
      if (top !== undefined) lastTop = !!top;
      var R = .5 * Math.hypot(b.W, b.D) + Math.max(20, innerH() * .5);
      var vHalf = THREE.MathUtils.degToRad(camera.fov) / 2;
      var hHalf = Math.atan(Math.tan(vHalf) * (camera.aspect || 1));
      var dist = R / Math.sin(Math.min(vHalf, hHalf));
      var tgt = new THREE.Vector3(0, innerH() * .22, 0);
      var dir = lastTop ? new THREE.Vector3(0, 1.6, .001) : new THREE.Vector3(.72, .85, 1.02);
      var pos = tgt.clone().add(dir.normalize().multiplyScalar(dist));
      camGoal = { pos: pos, tgt: tgt };
    }
    camera.position.set(500, 520, 620); controls.target.set(0, 30, 0);

    // указатель: драг с палитры, перенос модулей, ghost-валидация
    var caster = new THREE.Raycaster();
    var plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -PLATE_H);
    function ndc(e) {
      var r = renderer.domElement.getBoundingClientRect();
      return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    }
    function planePt(e) {
      caster.setFromCamera(ndc(e), camera);
      var p = new THREE.Vector3();
      return caster.ray.intersectPlane(plane, p) ? p : null;
    }
    function pickModule(e) {
      caster.setFromCamera(ndc(e), camera);
      var hits = caster.intersectObjects(modsG.children, true);
      return hits.length ? byId(hits[0].object.userData.mid) : null;
    }
    var drag = null, down = null;
    var hintEl = $('#gfxHint');
    function setHint(t) { hintEl.hidden = !t; hintEl.textContent = t || ''; }

    function ghostFor(e) {
      var p = planePt(e);
      if (!p) { hideGhost(); return null; }
      var tpl = (drag && drag.tpl) || (drag && drag.m) || null;
      if (!tpl) return null;
      var b = state.base;
      if (tpl.kind === 'wing') {
        var side = p.x < 0 ? 'L' : 'R';
        var cyw = Math.max(0, Math.min(G.rows - tpl.d, Math.round((p.z - z0()) / PITCH - tpl.d / 2)));
        var okw = canPlaceWing(side, cyw, tpl.d, drag && drag.m && drag.m.id);
        var gap = side === 'L' ? G.mL : G.mR;
        var gw = Math.max(6, gap - 1), hw = tpl.hmm || 60;
        showGhost(okw, gw, hw, tpl.d * PITCH - 1,
          side === 'L' ? -b.W / 2 + gw / 2 + .8 : b.W / 2 - gw / 2 - .8, SEAT + hw / 2, z0() + (cyw + tpl.d / 2) * PITCH);
        return { ok: okw, side: side, cy: cyw };
      }
      var off = (drag && drag.off) || { x: tpl.w / 2, y: tpl.d / 2 };
      var cx = Math.round((p.x - x0()) / PITCH - off.x), cy = Math.round((p.z - z0()) / PITCH - off.y);
      var ok = canPlaceBin(cx, cy, tpl.w, tpl.d, drag && drag.m && drag.m.id);
      var gh = tpl.hmm || 46.85;
      showGhost(ok, tpl.w * PITCH - 2, gh, tpl.d * PITCH - 2,
        x0() + (cx + tpl.w / 2) * PITCH, SEAT + gh / 2, z0() + (cy + tpl.d / 2) * PITCH);
      return { ok: ok, cx: cx, cy: cy };
    }

    on(renderer.domElement, 'dblclick', function (e) {
      var m = pickModule(e);
      if (m && m.kind === 'bin') openEditor(m.id);
    });
    on(renderer.domElement, 'pointerdown', function (e) {
      down = { x: e.clientX, y: e.clientY, moved: false, touch: e.pointerType === 'touch' };
      if (state.placing) return;
      var m = pickModule(e);
      if (m) {
        select(m.id); controls.enabled = false;
        var p = planePt(e), off;
        if (m.kind === 'wing') off = { x: 0, y: m.d / 2 };
        else off = { x: (p.x - x0()) / PITCH - m.cx, y: (p.z - z0()) / PITCH - m.cy };
        drag = { m: m, tpl: m, off: off, started: false };
        // На телефоне двойной клик съедает жест зума WebView — вход в редактор даёт
        // ДОЛГИЙ ТАП (450 мс без движения), как в привычных мобильных редакторах.
        if (down.touch && m.kind === 'bin') {
          down.lp = setTimeout(function () {
            if (!down || down.moved || !drag) return;
            hap('medium'); drag = null; controls.enabled = true; openEditor(m.id);
          }, 450);
          timers.push(down.lp);
        }
      }
    });
    on(window, 'pointermove', function (e) {
      if (disposed || state.editing) return;
      // палец «плывёт» сильнее мыши: с порогом 5 px обычный тап то и дело превращался в перенос
      if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > (down.touch ? 9 : 5)) {
        down.moved = true;
        if (down.lp) { clearTimeout(down.lp); down.lp = null; }
      }
      if (state.placing) {
        if (over(e)) { drag = { tpl: state.placing.tpl }; ghostFor(e); drag = null; }
        else hideGhost();
        return;
      }
      if (!drag) {
        if (e.pointerType === 'mouse' && over(e)) updateHover(e);
        else if (hovRing.visible) { hovRing.visible = false; hovId = null; }
        return;
      }
      if (!drag.started && down && down.moved) drag.started = true;
      if (drag.started) ghostFor(e);
    });
    function over(e) {
      var r = renderer.domElement.getBoundingClientRect();
      return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    }
    on(window, 'pointerup', function (e) {
      if (disposed) return;
      if (down && down.lp) { clearTimeout(down.lp); down.lp = null; }
      if (state.editing) { down = null; return; }
      var pl = state.placing;
      if (pl && over(e) && (pl.dragging || down)) {   // поставить новый модуль
        drag = { tpl: pl.tpl };
        var res = ghostFor(e);
        drag = null; hideGhost();
        if (res && res.ok) { addModule(pl.tpl, res); setHint(''); }
        else if (pl.dragging) { state.placing = null; bus.emit('ui'); setHint(''); }
        down = null; return;
      }
      if (drag) {
        controls.enabled = true;
        if (drag.started) {
          var res2 = ghostFor(e);
          hideGhost();
          if (res2 && res2.ok) {
            var m = drag.m;
            if (m.kind === 'wing') upd(m.id, { side: res2.side, cy: res2.cy });
            else upd(m.id, { cx: res2.cx, cy: res2.cy });
          } else bus.emit('mods');
        }
        drag = null; down = null; return;
      }
      if (down && !down.moved && over(e) && !pl) select(null);
      down = null; hideGhost();
    });
    on(window, 'pointercancel', function () {
      if (down && down.lp) { clearTimeout(down.lp); down.lp = null; }
      if (drag) { controls.enabled = true; drag = null; }
      down = null; hideGhost();
    });
    on(window, 'keydown', function (e) {
      if (disposed || state.editing) return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      var m = selected();
      if (e.key === 'Escape') { state.placing = null; setHint(''); hideGhost(); select(null); }
      if (!m) return;
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); removeModule(m.id); }
      if (e.key === 'r' || e.key === 'R' || e.key === 'к' || e.key === 'К') rotateModule(m.id);
      if (e.key === 'e' || e.key === 'E' || e.key === 'у' || e.key === 'У') { if (m.kind === 'bin') openEditor(m.id); }
      if (e.key === 'd' || e.key === 'D' || e.key === 'в' || e.key === 'В') duplicateModule(m.id);
    });

    // ── верхняя панель: камеры, сброс, заявка мастеру ──
    $('#gfxTopV').onclick = function () { userMoved = false; lastTop = true; frame(true); };
    $('#gfxIso').onclick = function () { userMoved = false; lastTop = false; frame(false); };
    $('#gfxClear').onclick = function () { confirmDlg('Убрать все модули?', clearAll); };
    var orderBtn = $('#gfxOrder');
    var orderDlg = null;   // открытый диалог заявки — destroy снимает его с body
    function closeOrderDlg() { if (orderDlg && orderDlg.parentNode) orderDlg.parentNode.removeChild(orderDlg); orderDlg = null; }
    // В мастерской заказ оформляется ПАРТИЯМИ из спецификации, а не «всей раскладкой»:
    // кнопка ведёт туда, где клиент отмечает, что печатаем сейчас. Старый путь (диалог с
    // пожеланием и заявка целиком) остаётся для экрана без проекта.
    var filtEl = $('#gfxFilt'), legendEl = $('#gfxLegend');
    if (opts.onSpec) {
      // Кнопку прячем, а не переименовываем: путь к спецификации уже есть вкладкой сверху,
      // и вторая дверь в ту же комнату занимала место, которого на телефоне нет.
      orderBtn.hidden = true;
      filtEl.hidden = false;
      on(filtEl, 'click', function (e) {
        var b = e.target.closest('[data-f]');
        if (!b) return;
        setFilter(b.dataset.f);
      });
    }
    // Раскладку СНАРУЖИ подменяет только откат к снимку версии. Локальную копию
    // перезаписываем тут же со свежей меткой времени: иначе арбитр свежести в load()
    // при следующем входе снова выбрал бы её и молча вернул откаченное состояние.
    function setConfig(cfg) {
      if (!cfg || !cfg.base || !cfg.modules) return false;
      state.editing = null; state.sel = null; state.placing = null;
      state.base = JSON.parse(JSON.stringify(cfg.base));
      state.modules = JSON.parse(JSON.stringify(cfg.modules));
      seq = Math.max(cfg.seq || 0, state.modules.reduce(function (a, m) {
        return Math.max(a, (m.id || 0) + 1);
      }, 1));
      volCache = {}; volKeys = 0;
      try { localStorage.setItem(lsKey, JSON.stringify(snapshot())); } catch (e) {}
      bus.emit('scene'); bus.emit('ui'); bus.emit('edit');
      return true;
    }
    function setFilter(mode) {
      filterMode = mode || 'all';
      filtEl.querySelectorAll('[data-f]').forEach(function (b) {
        b.classList.toggle('on', b.dataset.f === filterMode);
      });
      rebuildMods();
    }
    function setParts(list) {
      partsMap = {};
      (list || []).forEach(function (p) { if (p && p.mod_id) partsMap[p.mod_id] = p; });
      var n = 0, sel = 0;
      (list || []).forEach(function (p) {
        var live = p.live_state || p.state;
        if (live === 'ordered' || live === 'in_progress' || live === 'awaiting_payment' || live === 'printed') n++;
        if (p.sel) sel++;
      });
      if (legendEl) {
        legendEl.textContent = n ? ('▢ заказано: ' + n + (sel ? ' · в партии: ' + sel : '')) : '';
      }
      // Метки — это кольца и спрайты, геометрия модулей от них не зависит. Полная
      // пересборка нужна ТОЛЬКО когда фильтр вида решает, кого рисовать: иначе каждая
      // галочка в спецификации перестраивала бы всю сцену, которую никто не видит.
      if (filterMode === 'all') {
        rebuildBadges();
        renderer.shadowMap.needsUpdate = true;
      } else {
        rebuildMods();
      }
    }
    if (opts.parts) setParts(opts.parts);
    orderBtn.onclick = function () {
      if (opts.onSpec) { flushSave(); opts.onSpec(); return; }
      if (!state.modules.length) { setHint('Сначала добавьте хотя бы один модуль'); setTimeout(function () { setHint(''); }, 1800); return; }
      if (!opts.order || orderDlg) return;
      var dlg = document.createElement('div');
      dlg.className = 'gfx g-dlg';
      dlg.innerHTML = '<div class="g-dlg-card"><p class="g-lbl">Заявка мастеру</p>' +
        '<div class="g-divhint" style="margin:0 0 8px">Мастер посмотрит раскладку и назовёт цену. Можно добавить пожелание:</div>' +
        '<textarea maxlength="500" placeholder="Например: пластик матовый, нужно к пятнице"></textarea>' +
        '<div class="g-row" style="margin-top:10px"><button class="g-btn" data-a="no" type="button">Отмена</button>' +
        '<button class="g-btn acc" data-a="yes" type="button">Отправить</button></div></div>';
      document.body.appendChild(dlg);
      orderDlg = dlg;
      dlg.addEventListener('click', function (e) {
        if (e.target === dlg) { closeOrderDlg(); return; }
        var b = e.target.closest('[data-a]');
        if (!b) return;
        if (b.dataset.a === 'no') { closeOrderDlg(); return; }
        var note = (dlg.querySelector('textarea').value || '').trim().slice(0, 500);
        closeOrderDlg();
        orderBtn.disabled = true;
        Promise.resolve(opts.order(snapshot(), note)).then(function () { if (!disposed) orderBtn.disabled = false; },
          function () { if (!disposed) orderBtn.disabled = false; });
      });
    };

    // ── события шины ──
    bus.on('scene', rebuildScene);
    bus.on('mods', function () { rebuildMods(); });
    bus.on('edit', function () { if (!state.editing) rebuildMods(); });
    bus.on('sel', updateSelBox);
    bus.on('warn', function (t) { setHint(t); });
    bus.on('ui', function () {
      if (Date.now() < warnUntil) return;   // не затираем предупреждение подсказкой
      var pl = state.placing;
      setHint(pl && !pl.dragging ? 'Кликните по сетке, чтобы поставить модуль · Esc — отмена' : '');
    });
    bus.on('flash', function () {
      if (Date.now() < warnUntil) return;
      setHint('Здесь не помещается');
      setTimeout(function () { if (!state.placing) setHint(''); }, 1400);
    });
    // «Назад» (жест/шеврон) при открытом редакторе закрывает РЕДАКТОР, а не экран: свой
    // слой на центральном стеке PS_Back (как у лайтбоксов), снимается при выходе из редактора.
    var edLayer = null;
    bus.on('edit', function () {
      if (state.editing) {
        if (window.PS_Back && !edLayer) edLayer = window.PS_Back.push(function () { edLayer = null; closeEditor(); });
      } else if (edLayer && window.PS_Back) {
        var l = edLayer; edLayer = null; window.PS_Back.remove(l);
      }
    });

    // ── цикл: троттлинг 25 мс + страховочный setInterval 300 мс (rAF душится в фоне) ──
    function resize() {
      var w = vp.clientWidth, h = vp.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
      if (!userMoved) frame(lastTop);
    }
    controls.addEventListener('start', function () { userMoved = true; renderer.shadowMap.needsUpdate = true; });
    var ro = new ResizeObserver(resize); ro.observe(vp); ros.push(ro);
    var lastTick = 0;
    function tick() {
      // Вкладка «Спецификация» скрывает сцену через display:none — размер хоста нулевой.
      // Рисовать в этот момент нечего: на телефоне это греет корпус и садит батарею.
      if (!vp.clientWidth || !vp.clientHeight) return;
      if (disposed || state.editing) return;
      var now = performance.now();
      if (now - lastTick < 25) return;
      var dt = Math.min(1200, now - lastTick);
      lastTick = now;
      if (pops.length) stepPops(now);
      if (ghost.visible && !ghost.position.equals(ghostGoal)) {
        ghost.position.lerp(ghostGoal, 1 - Math.pow(0.0008, dt / 1000));
        if (ghost.position.distanceTo(ghostGoal) < .05) ghost.position.copy(ghostGoal);
        ghostEdges.position.copy(ghost.position);
        ghostFoot.position.set(ghost.position.x, PLATE_H + .1, ghost.position.z);
      }
      if (camGoal) {
        var f = Math.min(1, dt * .006);
        camera.position.lerp(camGoal.pos, f); controls.target.lerp(camGoal.tgt, f);
        if (camera.position.distanceTo(camGoal.pos) < 1) camGoal = null;
      }
      controls.update(); renderer.render(scene, camera);
    }
    function loop() { if (disposed) return; requestAnimationFrame(loop); tick(); }
    every(tick, 300);

    // ════════════════ панели (порт panels.js; легаси-редактор перегородок divSvg не взят —
    // его в UI прототипа уже заменил genParamsHTML + 3D-редактор) ════════════════
    var customW = 2, customD = 1;
    var stp = function (k, nm, v, min, max, step, unit) {
      return '<div class="g-stp" data-stp="' + k + '" data-min="' + min + '" data-max="' + max + '" data-step="' + (step || 1) + '">' +
        '<span class="g-nm">' + nm + '</span><button data-d="-1" type="button">−</button>' +
        '<input type="number" value="' + v + '" min="' + min + '" max="' + max + '" step="' + (step || 1) + '">' +
        '<button data-d="1" type="button">+</button>' +
        // flex:0 (не 0 0 auto!) даёт basis 0 при живом shrink — подпись единиц схлопывалась
        // в нулевую ширину и вылезала за панель. На телефоне это пряталось шириной шторки,
        // а на ПК «мм» встало поперёк сцены.
        (unit ? '<span class="g-nm" style="flex:0 0 auto">' + unit + '</span>' : '') + '</div>';
    };
    function miniSvg(w, d) {
      var s = 14, W = w * s, D = d * s, cells = '';
      for (var i = 0; i < w; i++) for (var j = 0; j < d; j++)
        cells += '<rect x="' + (i * s + 1.5) + '" y="' + (j * s + 1.5) + '" width="' + (s - 3) + '" height="' + (s - 3) + '" rx="3" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".8"></rect>';
      return '<svg width="' + W + '" height="' + D + '" viewBox="0 0 ' + W + ' ' + D + '" style="color:var(--muted);max-width:100%">' + cells + '</svg>';
    }
    var genSvg = {
      comp: '<svg width="34" height="26" viewBox="0 0 34 26"><rect x="1" y="1" width="32" height="24" rx="4" fill="none" stroke="currentColor" stroke-width="1.6"></rect><line x1="12" y1="1" x2="12" y2="25" stroke="currentColor" stroke-width="1.4"></line><line x1="23" y1="1" x2="23" y2="25" stroke="currentColor" stroke-width="1.4"></line></svg>',
      grid: '<svg width="34" height="26" viewBox="0 0 34 26"><rect x="1" y="1" width="32" height="24" rx="4" fill="none" stroke="currentColor" stroke-width="1.6"></rect><line x1="17" y1="1" x2="17" y2="25" stroke="currentColor" stroke-width="1.4"></line><line x1="1" y1="13" x2="33" y2="13" stroke="currentColor" stroke-width="1.4"></line></svg>',
      comb: '<svg width="34" height="26" viewBox="0 0 34 26"><rect x="1" y="1" width="32" height="24" rx="4" fill="none" stroke="currentColor" stroke-width="1.6"></rect><line x1="11" y1="1" x2="11" y2="25" stroke="currentColor" stroke-width="1.4"></line><line x1="1" y1="7" x2="11" y2="7" stroke="currentColor" stroke-width="1.2"></line><line x1="1" y1="13" x2="11" y2="13" stroke="currentColor" stroke-width="1.2"></line><line x1="1" y1="19" x2="11" y2="19" stroke="currentColor" stroke-width="1.2"></line></svg>',
      low: '<svg width="34" height="26" viewBox="0 0 34 26"><rect x="1" y="15" width="32" height="10" rx="3" fill="none" stroke="currentColor" stroke-width="1.6"></rect></svg>' };

    function baseHTML() {
      var b = state.base, g = gridInfo();
      var segBtn = function (v, l) { return '<button data-type="' + v + '" type="button" class="' + (b.type === v ? 'on' : '') + '">' + l + '</button>'; };
      var dims = '<div class="g-stack">' + stp('W', 'Ширина внутри', b.W, 120, 1200, 10, 'мм') + stp('D', 'Глубина внутри', b.D, 120, 1200, 10, 'мм');
      var hLbl = b.type === 'shelf' ? 'Просвет по высоте' : b.type === 'drawer' ? 'Высота борта' : 'Полезная высота';
      dims += stp('H', hLbl, b.H, 40, 600, 10, 'мм');
      if (b.type === 'tandem') dims += '<div class="g-divhint">Лимит высоты модулей = максимум из этого значения и верха серии (' + seriesOf().top + ' мм).</div>';
      dims += '</div>';
      var series = '';
      if (b.type === 'tandem') {
        series = '<div class="g-sec"><p class="g-lbl">Серия Blum Tandembox</p><div class="g-series">' +
          Object.keys(SERIES).map(function (k) {
            var s = SERIES[k];
            return '<button data-series="' + k + '" type="button" class="' + (b.series === k ? 'on' : '') + '"><b>' + k + '</b><span>царга ' + s.h + ' мм' + (s.rails.length ? ' · рейлинг ' + s.rails[s.rails.length - 1] : '') + '</span></button>';
          }).join('') + '</div>' +
          '<div class="g-info" style="margin-top:8px">Дно между царгами ' + b.W + ' мм, по верху стенок ' + (b.W + 2 * PROF.inset) + ' мм — внизу уже, кверху +' + PROF.inset + ' мм на сторону. Пристенные модули повторяют профиль.</div></div>';
      }
      var fit = '';
      if (g.lx > .5 || g.ly > .5) {
        var fx = b.fitX || 'sym', fy = b.fitY || 'sym';
        fit = '<div class="g-sec"><p class="g-lbl">Куда отдать остаток (spacerless)</p>' +
          '<div class="g-seg">' + [['left', 'Влево'], ['sym', 'Поровну'], ['right', 'Вправо']].map(function (p) { return '<button data-fitx="' + p[0] + '" type="button" class="' + (fx === p[0] ? 'on' : '') + '">' + p[1] + '</button>'; }).join('') + '</div>' +
          '<div class="g-seg" style="margin-top:6px">' + [['back', 'Назад'], ['sym', 'Поровну'], ['front', 'Вперёд']].map(function (p) { return '<button data-fity="' + p[0] + '" type="button" class="' + (fy === p[0] ? 'on' : '') + '">' + p[1] + '</button>'; }).join('') + '</div></div>';
      }
      return '<div class="g-sec"><p class="g-lbl">Тип базы</p><div class="g-seg">' + segBtn('shelf', 'Полка') + segBtn('drawer', 'Шуфлядка') + segBtn('tandem', 'Tandembox') + '</div></div>' +
        '<div class="g-sec"><p class="g-lbl">Размеры</p>' + dims + '</div>' + series + fit +
        '<div class="g-sec"><div class="g-info">Сетка <b>' + g.cols + ' × ' + g.rows + '</b> · шаг 42 мм<br>Плита spacerless: крайние ячейки растянуты в остаток<br>Расширения: ←' + g.mL.toFixed(1) + ' →' + g.mR.toFixed(1) + ' · ↑' + g.mB.toFixed(1) + ' ↓' + g.mF.toFixed(1) + ' мм<br>Бокс N×M = 42N−0.62 · стенка 0.8</div></div>';
    }

    var TPLS = [{ w: 1, d: 1 }, { w: 2, d: 1 }, { w: 1, d: 2 }, { w: 2, d: 2 }, { w: 3, d: 1 }, { w: 3, d: 2 }];
    var ARCH = [
      { gen: 'comp', w: 1, d: 2, compN: 3, label: 'Отсеки', hint: 'стенки поперёк' },
      { gen: 'grid', w: 2, d: 2, gridX: 2, gridY: 2, label: 'Сетка ячеек', hint: 'k × m поровну' },
      { gen: 'comb', w: 2, d: 3, combN: 6, combW: 16, combSide: 'W', label: 'Гребёнка', hint: 'кармашки + отсек' },
      { gen: 'tray', w: 2, d: 4, hmm: 16.4, label: 'Низкий лоток', hint: 'h 16.4 мм' }];
    function paletteHTML() {
      var g = gridInfo();
      var cards = TPLS.map(function (t, i) { return '<div class="g-card" data-tpl="' + i + '">' + miniSvg(t.w, t.d) + '<i>' + t.w + '×' + t.d + '</i></div>'; }).join('');
      var arch = ARCH.map(function (a, i) { return '<div class="g-card g-wing" data-arch="' + i + '"><span style="color:var(--muted)">' + genSvg[a.gen === 'tray' ? 'low' : a.gen] + '</span><div><i>' + a.label + ' · ' + a.w + '×' + a.d + '</i><small>' + a.hint + '</small></div></div>'; }).join('');
      if (wingOk('L')) arch += '<div class="g-card g-wing" data-wing="L"><svg width="34" height="30" viewBox="0 0 34 30" style="color:var(--muted)"><path d="M4 2 L10 2 L10 28 L4 28 Z M13 6 L28 6 L28 28 L13 28 Z" fill="none" stroke="currentColor" stroke-width="1.6"></path></svg><div><i>Пристенный · левый</i><small>профиль под царгу · ' + wingGap('L').toFixed(1) + '→' + (wingGap('L') + PROF.inset).toFixed(1) + ' мм</small></div></div>';
      if (wingOk('R')) arch += '<div class="g-card g-wing" data-wing="R"><svg width="34" height="30" viewBox="0 0 34 30" style="color:var(--muted)"><path d="M30 2 L24 2 L24 28 L30 28 Z M21 6 L6 6 L6 28 L21 28 Z" fill="none" stroke="currentColor" stroke-width="1.6"></path></svg><div><i>Пристенный · правый</i><small>профиль под царгу · ' + wingGap('R').toFixed(1) + '→' + (wingGap('R') + PROF.inset).toFixed(1) + ' мм</small></div></div>';
      return '<div class="g-sec"><p class="g-lbl">Размеры — тяните на сетку</p><div class="g-cards">' + cards + '</div></div>' +
        '<div class="g-sec"><p class="g-lbl">Типовые модули</p><div class="g-cards">' + arch + '</div></div>' +
        '<div class="g-sec"><p class="g-lbl">Свой размер</p><div class="g-stack">' + stp('cw', 'Ширина, ячеек', customW, 1, Math.max(1, g.cols), 1) + stp('cd', 'Глубина, ячеек', customD, 1, Math.max(1, g.rows), 1) + '<button class="g-btn acc" id="gfxAddCustom" type="button">＋ Добавить ' + customW + '×' + customD + '</button></div></div>';
    }

    function heightHTML(m) {
      var cap = capMM(m.kind), um = uMax(m.kind), u = Math.round((m.hmm - 4.85) / 7);
      return '<div class="g-sec"><p class="g-lbl">Высота</p>' +
        '<div class="g-sldrow"><input type="range" id="gfxHuSld" min="1" max="' + um + '" value="' + Math.max(1, Math.min(um, u)) + '"><output id="gfxHuOut">' + (+m.hmm).toFixed(1) + ' мм</output></div>' +
        '<div style="margin-top:8px">' + stp('hmm', 'Точно, мм', (+m.hmm).toFixed(1), 12, cap, .5) + '</div>' +
        '<div class="g-divhint">Слайдер — юниты (H = 7u + 4.85). Поле — любая высота, макс ' + cap.toFixed(0) + ' мм.</div></div>';
    }
    function extHTML(m) {
      var g = gridInfo(), b = state.base;
      var can = { L: m.cx === 0 && g.mL > 2.3, R: m.cx + m.w === g.cols && g.mR > 2.3, B: m.cy === 0 && g.mB > 2.3, F: m.cy + m.d === g.rows && g.mF > 2.3 };
      if (!can.L && !can.R && !can.B && !can.F) return '';
      var lb = { L: '← ' + g.mL.toFixed(0) + 'мм', R: '→ ' + g.mR.toFixed(0) + 'мм', B: '↑ ' + g.mB.toFixed(0) + 'мм', F: '↓ ' + g.mF.toFixed(0) + 'мм' };
      var btns = ['L', 'R', 'B', 'F'].filter(function (k) { return can[k]; }).map(function (k) { return '<button class="g-btn sm ' + (m.ext && m.ext[k] ? 'acc' : '') + '" data-ext="' + k + '" type="button">' + lb[k] + '</button>'; }).join('');
      var bde = binDims(m), sum = bde.e.L + bde.e.R + bde.e.B + bde.e.F;
      return '<div class="g-sec"><p class="g-lbl">Добор до стенки</p><div class="g-row">' + btns + '</div>' +
        '<div class="g-divhint">Корпус расширяется на остаток этой стороны' + (b.type === 'tandem' ? ' (до «ноги» царги)' : '') + '.'
        + (sum > 0 ? '<br>С добором ' + bde.ow.toFixed(1) + '×' + bde.od.toFixed(1) + ' мм (без него ' + (m.w * 42 - .62).toFixed(1) + '×' + (m.d * 42 - .62).toFixed(1) + ')' : '')
        + '</div></div>';
    }
    function genParamsHTML(m) {
      var gen = m.gen || 'tray';
      var segs = Object.keys(GEN_LABEL).map(function (k) { return '<button data-gen="' + k + '" type="button" class="' + (gen === k ? 'on' : '') + '">' + GEN_LABEL[k] + '</button>'; }).join('');
      var p = '';
      if (gen === 'comp') p = '<div class="g-stack" style="margin-top:8px">' + stp('compN', 'Отсеков', m.compN || 3, 2, 12, 1) + '</div>';
      if (gen === 'grid') p = '<div class="g-stack" style="margin-top:8px">' + stp('gridX', 'Колонок', m.gridX || 2, 1, 10, 1) + stp('gridY', 'Рядов', m.gridY || 2, 1, 10, 1) + '</div>';
      if (gen === 'comb') {
        var sd = m.combSide || 'W';
        var sseg = [['W', 'Слева'], ['E', 'Справа'], ['N', 'Сзади'], ['S', 'Спереди']].map(function (q) { return '<button data-combside="' + q[0] + '" type="button" class="' + (sd === q[0] ? 'on' : '') + '" style="font-size:11px;padding:8px 1px">' + q[1] + '</button>'; }).join('');
        p = '<div class="g-seg" style="margin-top:8px">' + sseg + '</div><div class="g-stack" style="margin-top:8px">' + stp('combN', 'Кармашков', m.combN || 6, 2, 16, 1) + stp('combW', 'Полоса, мм', m.combW || 16, 12, 34, .5) + '</div>';
      }
      if (gen === 'free') p = '<div class="g-divhint" style="margin-top:8px">Свои стены: <b>' + (m.walls || []).length + '</b> · вставок: <b>' + (m.items || []).length + '</b></div>';
      return '<div class="g-sec"><p class="g-lbl">Наполнение</p><div class="g-seg g-genseg">' + segs + '</div>' + p +
        '<button class="g-btn acc" data-act="edit" type="button" style="width:100%;margin-top:8px">✐ Редактор модуля</button>' +
        '<div class="g-divhint">' + (LOWEND ? 'Долгий тап по модулю в 3D — тоже открывает редактор.' : 'Двойной клик по модулю в 3D — тоже открывает редактор.') + '</div></div>';
    }
    function selHTML() {
      var m = selected();
      if (!m) return '<div class="g-shead"><b>Модуль</b></div><div class="g-empty">Выберите модуль на сетке — или перетащите новый из палитры выше.</div>';
      var colors = COLORS.map(function (c, i) { return '<button class="g-sw ' + (i === m.colorIdx ? 'on' : '') + '" data-color="' + i + '" type="button" style="background:' + c[1] + '" title="' + c[0] + '"></button>'; }).join('');
      if (m.kind === 'wing') {
        var g = gridInfo(), wg = wingGap(m.side);
        return '<div class="g-shead"><b>Пристенный · ' + (m.side === 'L' ? 'левый' : 'правый') + '</b><span class="g-chip g-mono">' + wg.toFixed(1) + '–' + (wg + PROF.inset).toFixed(1) + '×' + (m.d * 42).toFixed(0) + '×' + (+m.hmm).toFixed(0) + ' мм</span></div>' +
          '<div class="g-sec"><div class="g-stack">' + stp('md', 'Длина, ячеек', m.d, 1, g.rows, 1) + stp('comp', 'Отсеков', m.comp, 1, 8, 1) + '</div></div>' +
          heightHTML(m) +
          '<div class="g-sec"><div class="g-info">Профиль под царгу: внизу ниша упирается в «ногу» направляющей, выше ' + PROF.slopeY + ' мм — консоль +' + PROF.inset + ' мм до тонкой стенки.</div></div>' +
          '<div class="g-sec"><p class="g-lbl">Цвет</p><div class="g-swatches">' + colors + '</div></div>' +
          '<div class="g-sec"><div class="g-row"><button class="g-btn" data-act="dup" type="button">Дублировать</button><button class="g-btn warn" data-act="del" type="button">Удалить</button></div></div>';
      }
      var bdc = binDims(m);   // с добором корпус БОЛЬШЕ: печатаем то, что реально строится
      return '<div class="g-shead"><b>' + GEN_LABEL[m.gen || 'tray'] + ' ' + m.w + '×' + m.d + '</b><span class="g-chip g-mono">' + bdc.ow.toFixed(1) + '×' + bdc.od.toFixed(1) + '×' + (+m.hmm).toFixed(1) + ' мм</span></div>' +
        '<div class="g-sec"><div class="g-stack">' + stp('mw', 'Ширина, ячеек', m.w, 1, gridInfo().cols, 1) + stp('md', 'Глубина, ячеек', m.d, 1, gridInfo().rows, 1) + '</div></div>' +
        genParamsHTML(m) + heightHTML(m) + extHTML(m) +
        '<div class="g-sec"><p class="g-lbl">Цвет</p><div class="g-swatches">' + colors + '</div></div>' +
        '<div class="g-sec"><div class="g-row"><button class="g-btn" data-act="rot" type="button">Повернуть R</button><button class="g-btn" data-act="dup" type="button">Дублировать D</button><button class="g-btn warn" data-act="del" type="button">Удалить ⌫</button></div></div>';
    }

    var plural = function (n, a, b, c) {
      var n10 = n % 10, n100 = n % 100;
      return n10 === 1 && n100 !== 11 ? a : n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20) ? b : c;
    };
    function render() {
      $('#gfxBase').innerHTML = baseHTML();
      $('#gfxPalette').innerHTML = paletteHTML();
      $('#gfxSel').innerHTML = selHTML();
      // Точка у вкладки «Деталь»: параметры выбранного модуля переехали в свою шторку, и без
      // метки человек не понимает, что тап по модулю вообще что-то куда-то положил.
      var mpart = host.querySelector('.g-mtabs [data-p="gfxPart"]');
      if (mpart) mpart.classList.toggle('has', !!selected());
      var b = state.base, g = gridInfo();
      $('#gfxChip').textContent = TYPE_LABEL[b.type] + (b.type === 'tandem' ? ' ' + b.series : '') + ' · ' + b.W + '×' + b.D + ' мм · ' + g.cols + '×' + g.rows;
      var wings = state.modules.filter(function (m) { return m.kind === 'wing'; }).length, tot = state.modules.length;
      $('#gfxSummary').textContent = tot + ' ' + plural(tot, 'модуль', 'модуля', 'модулей') + ' · ' + cellsUsed() + '/' + (g.cols * g.rows) + ' ' + plural(cellsUsed(), 'ячейка', 'ячейки', 'ячеек') + (wings ? ' · ' + wings + ' ' + plural(wings, 'пристенный', 'пристенных', 'пристенных') : '');
    }

    function stpHandler(root, fn) {
      root.addEventListener('click', function (e) {
        var b = e.target.closest('.g-stp button');
        if (!b) return;
        var bx = b.closest('.g-stp'), inp = bx.querySelector('input');
        var v = Math.max(+bx.dataset.min, Math.min(+bx.dataset.max, (+inp.value || 0) + (+b.dataset.d) * (+bx.dataset.step)));
        inp.value = Math.round(v * 100) / 100;
        fn(bx.dataset.stp, +inp.value);
      });
      root.addEventListener('change', function (e) {
        var inp = e.target.closest('.g-stp input');
        if (!inp) return;
        var bx = inp.closest('.g-stp');
        var v = Math.max(+bx.dataset.min, Math.min(+bx.dataset.max, +inp.value || +bx.dataset.min));
        inp.value = Math.round(v * 100) / 100;
        fn(bx.dataset.stp, +inp.value);
      });
    }

    function initPanels() {
      var left = $('#gfxLeft'), right = $('#gfxRight');
      render();
      bus.on('ui', render);
      bus.on('scene', render);
      bus.on('mods', function (x) { if (!x || !x.quiet) render(); });

      left.addEventListener('click', function (e) {
        var t = e.target.closest('[data-type]'); if (t) { setBase({ type: t.dataset.type }); return; }
        var fx = e.target.closest('[data-fitx]'); if (fx) { setBase({ fitX: fx.dataset.fitx }); return; }
        var fy = e.target.closest('[data-fity]'); if (fy) { setBase({ fitY: fy.dataset.fity }); return; }
        var s = e.target.closest('[data-series]'); if (s) { setBase({ series: s.dataset.series }); }
      });
      stpHandler(left, function (k, v) { var p = {}; p[k] = v; setBase(p); });

      right.addEventListener('pointerdown', function (e) {
        var c = e.target.closest('.g-card');
        if (!c) return;
        e.preventDefault();
        var tpl = null;
        if (c.dataset.wing) tpl = { kind: 'wing', side: c.dataset.wing, d: 2, hmm: capMM('wing'), comp: 3 };
        else if (c.dataset.arch !== undefined) tpl = Object.assign({}, ARCH[+c.dataset.arch]);
        else if (c.dataset.tpl !== undefined) tpl = Object.assign({}, TPLS[+c.dataset.tpl]);
        if (!tpl) return;
        delete tpl.label; delete tpl.hint;
        var pl = { tpl: tpl, dragging: true, sx: e.clientX, sy: e.clientY };
        state.placing = pl;
        // capture-фаза ОБЯЗАТЕЛЬНА: bubble-обработчик сцены зарегистрирован раньше и на
        // мобильной раскладке (шторка ЛЕЖИТ ПОВЕРХ canvas, тап проходит rect-проверку over())
        // успевал принять тап по карточке за попытку установки и гасил state.placing. В
        // capture тап помечает dragging=false ДО сцены — она видит «режим клика» и молчит;
        // драг ≥6px оставляет dragging=true, и сцена штатно ставит модуль под пальцем.
        var up = function (ev) {
          window.removeEventListener('pointerup', up, true);
          // и из учёта offs — иначе записи копятся по одной на каждый драг карточки
          for (var i = offs.length - 1; i >= 0; i--) { if (offs[i][2] === up) { offs.splice(i, 1); break; } }
          if (disposed) return;
          if (Math.hypot(ev.clientX - pl.sx, ev.clientY - pl.sy) < 6) { pl.dragging = false; bus.emit('ui'); }
          else setTimeout(function () { if (state.placing === pl && pl.dragging) { state.placing = null; bus.emit('ui'); } }, 0);
        };
        // через on(): размонтирование ПОСРЕДИ перетаскивания (жест «назад») иначе оставляло
        // слушатель на window с замыканием на всю сцену до чужого следующего pointerup
        on(window, 'pointerup', up, true);
        c.classList.add('on');
        // мобильный сценарий «тап по карточке → тап по сетке»: шторка панели закрывается,
        // иначе сетки не видно (в прототипе панель тоже уходила — там она была отдельным слоем)
        if (right.classList.contains('open')) closeSheets();
      });

      right.addEventListener('click', function (e) {
        if (e.target.closest('#gfxAddCustom')) { state.placing = { tpl: { w: customW, d: customD }, dragging: false }; bus.emit('ui'); closeSheets(); return; }
        var m = selected();
        if (!m) return;
        var sw = e.target.closest('[data-color]'); if (sw) { upd(m.id, { colorIdx: +sw.dataset.color }); return; }
        var gb = e.target.closest('[data-gen]'); if (gb) { upd(m.id, { gen: gb.dataset.gen }); return; }
        var cs = e.target.closest('[data-combside]'); if (cs) { upd(m.id, { combSide: cs.dataset.combside }); return; }
        var ex2 = e.target.closest('[data-ext]');
        if (ex2) { var k = ex2.dataset.ext, ne = Object.assign({}, m.ext); ne[k] = ne[k] ? 0 : 1; upd(m.id, { ext: ne }); return; }
        var act = e.target.closest('[data-act]');
        if (act) {
          var a = act.dataset.act;
          if (a === 'del') removeModule(m.id);
          if (a === 'rot') rotateModule(m.id);
          if (a === 'dup') duplicateModule(m.id);
          if (a === 'edit') { closeSheets(); openEditor(m.id); }
        }
      });

      stpHandler(right, function (k, v) {
        if (k === 'cw') { customW = v; render(); return; }
        if (k === 'cd') { customD = v; render(); return; }
        var m = selected();
        if (!m) return;
        var map = { mw: 'w', md: 'd', comp: 'comp', compN: 'compN', gridX: 'gridX', gridY: 'gridY', combN: 'combN', combW: 'combW', hmm: 'hmm' };
        var key = map[k];
        if (!key) return;
        var p = {}; p[key] = v;
        if (!upd(m.id, p)) { flash(); render(); }
      });

      right.addEventListener('input', function (e) {
        if (e.target.id !== 'gfxHuSld') return;
        var m = selected();
        if (!m) return;
        var v = HMM(+e.target.value);
        upd(m.id, { hmm: v }, true);
        var o = $('#gfxHuOut');
        if (o) o.textContent = v.toFixed(1) + ' мм';
        var inp = right.querySelector('[data-stp="hmm"] input');
        if (inp) inp.value = v.toFixed(1);
      });
      right.addEventListener('change', function (e) { if (e.target.id === 'gfxHuSld') render(); });

      // мобильные шторки: таб-бар «База/Модули» + крестики
      host.querySelectorAll('.g-mtabs button').forEach(function (b) {
        b.onclick = function () {
          var p = $('#' + b.dataset.p), was = p.classList.contains('open');
          closeSheets();
          if (!was) { p.classList.add('open'); b.classList.add('on'); }
        };
      });
      host.querySelectorAll('.g-pclose').forEach(function (b) {
        b.onclick = function () { closeSheets(); };
      });
    }
    function closeSheets() {
      host.querySelectorAll('.g-panel').forEach(function (x) { x.classList.remove('open'); });
      host.querySelectorAll('.g-mtabs button').forEach(function (x) { x.classList.remove('on'); });
    }

    // ════════════════ 3D-редактор модуля (порт editor3d.js) ════════════════
    var edHost, stage, cvs2, ctx2, eRenderer, eScene, camP, camO, cam, ctlP, ctlO, modG = null, gridG = null;
    var tool = 'wall', sub = 'wall', shape = 'circle', eSel = null, eDrag = null, polyPts = null;
    var hist = [], hi = -1;
    // Свёрнута ли панель под сценой. Это предпочтение ИНТЕРФЕЙСА, а не данные ящика:
    // ключ один на все проекты и в снимок раскладки (и на сервер) не попадает.
    var FOLD_LS = 'gf-ed-fold', edFold = false;
    var $e = function (s) { return edHost.querySelector(s); };
    var cur = function () { return byId(state.editing); };
    var clamp = function (v, a, b) { return Math.max(a, Math.min(b, v)); };
    var snapV = function (v, alt) { return alt ? Math.round(v) : Math.round(v / 10.5) * 10.5; };
    var ITEMS = [['cups', 'Гнёзда под биты', 'стаканы, шаг/Ø'], ['vslot', 'V-ложемент', 'для валов, отвёрток'], ['scoop', 'Скос дна', 'наклон-выгреб'], ['platform', 'Подиум', 'поднятое дно'], ['pins', 'Штырьки', 'ряды пеньков'], ['label', 'Полочка', 'этикетка']];
    var TOOLS = [['select', 'Выбор'], ['wall', 'Стена'], ['poly', 'Полигон'], ['brush', 'Кисть'], ['shape', 'Фигуры'], ['erase', 'Ластик']];

    function initEditor3D() {
      edHost = document.createElement('div');
      edHost.className = 'gfx gfx-ed';   // .gfx — чтобы g-* классы получили свои токены/шрифты
      edHost.hidden = true;
      edHost.innerHTML = '<div class="g-ed-top"><b>Редактор модуля</b>' +
        '<div class="g-ed-tools" id="gfxE3Tools">' + TOOLS.map(function (t) { return '<button data-tool="' + t[0] + '" type="button" class="' + (t[0] === tool ? 'on' : '') + '">' + t[1] + '</button>'; }).join('') + '</div>' +
        '<div class="g-ed-tools"><button id="gfxE3Undo" title="Ctrl+Z" type="button">↶</button><button id="gfxE3Redo" title="Ctrl+Shift+Z" type="button">↷</button></div>' +
        '<div class="g-ed-tools"><button id="gfxE3Orbit" class="on" type="button">Орбита</button><button id="gfxE3Top" type="button">Сверху</button></div>' +
        '<span class="g-chip g-mono" id="gfxE3Info"></span><button class="g-btn acc sm" id="gfxE3Done" type="button">Готово</button></div>' +
        '<div class="g-ed-body"><div class="g-ed-stage" id="gfxE3Stage"><canvas id="gfxE3Dims"></canvas><div id="gfxE3Hint" class="g-chip"></div></div>' +
        // Полоса-ручка стоит МЕЖДУ сценой и панелью и живёт вне #gfxE3Side намеренно:
        // edPanel() перерисовывает панель целиком (innerHTML), и кнопка внутри неё
        // теряла бы обработчик на каждом выборе инструмента.
        '<button class="g-ed-fold" id="gfxE3Fold" type="button" aria-expanded="true">' +
        '<span>Вставки и настройки</span><i>⌄</i></button>' +
        '<aside class="g-ed-side" id="gfxE3Side"></aside></div>';
      document.body.appendChild(edHost);
      stage = $e('#gfxE3Stage');
      eRenderer = new THREE.WebGLRenderer({ antialias: true });
      eRenderer.setPixelRatio(Math.min(devicePixelRatio, LOWEND ? 1.6 : 2));
      eRenderer.shadowMap.enabled = true;
      eRenderer.outputEncoding = THREE.sRGBEncoding;
      eRenderer.physicallyCorrectLights = true;
      stage.insertBefore(eRenderer.domElement, stage.firstChild);
      cvs2 = $e('#gfxE3Dims'); ctx2 = cvs2.getContext('2d');
      eScene = new THREE.Scene(); eScene.background = C('#0b0f14');
      envTexEd = studioEnv(eRenderer);   // тот же рецепт, но в СВОЁМ контексте
      eScene.environment = envTexEd;
      eScene.add(new THREE.HemisphereLight(C('#b6c8dd'), C('#1c242e'), .7));
      var sunE = new THREE.DirectionalLight(C('#fff6ea'), 1.15);
      sunE.position.set(160, 300, 150); sunE.castShadow = true;
      sunE.shadow.mapSize.set(1024, 1024);
      sunE.shadow.camera.left = -260; sunE.shadow.camera.right = 260; sunE.shadow.camera.top = 260; sunE.shadow.camera.bottom = -260;
      // без bias карта теней «сыпалась» по стенкам муаром (shadow acne) — в главной сцене
      // такой же bias стоит с самого начала, в редакторе его забыли
      sunE.shadow.bias = -0.0006; sunE.shadow.normalBias = 0.4;
      eScene.add(sunE);
      var flE = new THREE.DirectionalLight(C('#7d95b3'), .4); flE.position.set(-200, 180, -160); eScene.add(flE);
      var gnd = new THREE.Mesh(new THREE.CircleGeometry(1500, 40), mats.ground);
      gnd.rotation.x = -Math.PI / 2; gnd.position.y = -.02; gnd.receiveShadow = true; eScene.add(gnd);
      camP = new THREE.PerspectiveCamera(42, 1, 1, 4000);
      camO = new THREE.OrthographicCamera(-100, 100, 100, -100, 1, 3000);
      ctlP = new THREE.OrbitControls(camP, eRenderer.domElement);
      ctlO = new THREE.OrbitControls(camO, eRenderer.domElement);
      ctlP.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
      ctlO.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
      ctlP.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };
      ctlO.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_PAN };
      [ctlP, ctlO].forEach(function (c) { c.enableDamping = true; c.dampingFactor = .1; });
      ctlO.enableRotate = false;
      cam = camP; ctlO.enabled = false;
      var roE = new ResizeObserver(resizeE); roE.observe(stage); ros.push(roE);
      on(eRenderer.domElement, 'pointerdown', onDown);
      on(window, 'pointermove', onMove);
      on(window, 'pointerup', onUp);
      on(eRenderer.domElement, 'dblclick', function () { if (tool === 'poly' && polyPts && polyPts.length > 2) closePoly(false); });
      on(window, 'keydown', onKey);
      $e('#gfxE3Done').onclick = function () { closeEditor(); };
      $e('#gfxE3Tools').onclick = function (e) { var b = e.target.closest('[data-tool]'); if (b) setTool(b.dataset.tool); };
      $e('#gfxE3Undo').onclick = undo; $e('#gfxE3Redo').onclick = redo;
      $e('#gfxE3Orbit').onclick = function () { setCam(false); };
      $e('#gfxE3Top').onclick = function () { setCam(true); };
      try { edFold = localStorage.getItem(FOLD_LS) === '1'; } catch (e) {}
      $e('#gfxE3Fold').onclick = function () {
        edFold = !edFold;
        try { localStorage.setItem(FOLD_LS, edFold ? '1' : '0'); } catch (e) {}
        applyFold();
      };
      on(edHost, 'click', function (e) { if (edHost.hidden) return; if (e.target.closest('#gfxE3Side')) { panelClick(e); var b = e.target.closest('.g-stp button'); if (b) panelStp(e); } });
      on(edHost, 'change', function (e) { if (edHost.hidden) return; if (e.target.closest('#gfxE3Side')) panelStp(e); });
      bus.on('edit', syncEd);
    }
    function applyFold() {
      var b = $e('.g-ed-body'), f = $e('#gfxE3Fold');
      if (!b) return;
      b.classList.toggle('fold', edFold);
      f.setAttribute('aria-expanded', edFold ? 'false' : 'true');
      // Свёрнутая полоса — единственный намёк на то, что внутри, поэтому называет содержимое.
      // Развёрнутая называет ДЕЙСТВИЕ: иначе её подпись дублирует заголовок первой же секции
      // панели («Вставки — тяните на дно»), и две одинаковые строки читаются как сбой вёрстки.
      f.querySelector('span').textContent = edFold ? 'Вставки и настройки' : 'Свернуть панель';
      f.title = edFold ? 'Развернуть панель' : 'Свернуть панель — сцена на весь экран';
      // Сцена сию секунду изменила высоту. ResizeObserver придёт только следующим кадром,
      // и до него картинка растянулась бы из старого буфера — зовём пересчёт сами.
      resizeE();
    }
    function setTool(t) {
      tool = t; polyPts = null; eSel = null; eDrag = null; cancelPlacing();
      edHost.querySelectorAll('#gfxE3Tools button').forEach(function (x) { x.classList.toggle('on', x.dataset.tool === t); });
      if (stage) stage.style.cursor = t === 'select' ? 'default' : 'crosshair';
      edHint(); edPanel();
    }
    function setCam(top) {
      cam = top ? camO : camP; ctlP.enabled = !top; ctlO.enabled = top;
      $e('#gfxE3Orbit').classList.toggle('on', !top);
      $e('#gfxE3Top').classList.toggle('on', top);
      if (top) frameTop();
    }
    function edHint() {
      if (state.placingItem) { $e('#gfxE3Hint').textContent = 'Кликните по дну — поставить «' + (ITEM_LABEL[state.placingItem.t] || '') + '» · Esc — отмена'; return; }
      var h = { select: 'Клик — выбрать · тяните — двигать · Alt-драг — копия вставки · ⌫ — удалить · пустое место — вращение',
        wall: 'Тяните по дну — стена (гориз/верт/45°) · Alt — шаг 1 мм · вне модуля — вращение камеры',
        poly: 'Кликайте вершины · клик у старта или двойной — замкнуть (' + (sub === 'wall' ? 'контур-стенка' : 'остров') + ') · Esc — отмена',
        brush: 'Ведите линию по дну — превратится в ломаную стенку',
        shape: 'Тяните по дну — ' + (shape === 'circle' ? 'круг' : shape === 'rrect' ? 'скруглённый прямоугольник' : 'дуга') + ' (' + (sub === 'wall' ? 'стенкой' : 'островом') + ')',
        erase: 'Клик по стене / острову / вставке — удалить' }[tool];
      $e('#gfxE3Hint').textContent = h || '';
    }
    function syncEd() {
      var m = cur();
      edHost.hidden = !m;
      if (!m) {
        // ВАЖНО: снять предыдущий модуль со сцены, а не просто обнулить ссылку. В прототипе
        // здесь стояло `modG=null`, и меш оставался в сцене навсегда: открыв второй модуль,
        // владелец видел его вместе с первым — «слились в один».
        if (modG) { eScene.remove(modG); modG.traverse(function (c) { if (c.geometry) c.geometry.dispose(); }); modG = null; }
        if (gridG) { eScene.remove(gridG); gridG.geometry.dispose(); gridG = null; }
        return;
      }
      tool = 'wall'; sub = 'wall'; eSel = null; polyPts = null; hist = []; hi = -1; pushH();
      setTool('wall'); applyFold(); rebuildEd(); resizeE(); frameIso(); setCam(false); edPanel();
    }
    function dims() { return binDims(cur()); }
    function rebuildEd() {
      var m = cur();
      if (!m) return;
      if (modG) { eScene.remove(modG); modG.traverse(function (c) { if (c.geometry) c.geometry.dispose(); }); }
      var G2 = gridInfo();
      modG = buildBin(m, { G: G2, base: state.base });
      var d0 = dims();
      modG.position.set(-d0.ox, 0, -d0.oz); eScene.add(modG);
      if (gridG) { eScene.remove(gridG); gridG.geometry.dispose(); }
      var ow = d0.ow, od = d0.od, pts = [], x, y;
      for (x = 0; x <= ow + .1; x += 10.5) { pts.push(-ow / 2 + x, 0, -od / 2, -ow / 2 + x, 0, od / 2); }
      for (y = 0; y <= od + .1; y += 10.5) { pts.push(-ow / 2, 0, -od / 2 + y, ow / 2, 0, -od / 2 + y); }
      var gg = new THREE.BufferGeometry();
      gg.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
      gridG = new THREE.LineSegments(gg, gridMat);
      gridG.position.y = FLOOR_TOP + .15; eScene.add(gridG);
      $e('#gfxE3Info').textContent = ow.toFixed(1) + '×' + od.toFixed(1) + '×' + (+m.hmm).toFixed(1) + ' мм · ' + m.w + '×' + m.d + ' ячеек';
    }
    function frameIso() {
      var d0 = dims(), m = cur();
      var r = Math.max(d0.ow, d0.od, m.hmm * 1.4) * 1.15;
      var k = 1 / Math.min(1, camP.aspect || 1);
      camP.position.set(r * .8 * k, r * .9 * k, r * 1.05 * k);
      ctlP.target.set(0, m.hmm * .25, 0); ctlP.update();
    }
    function frameTop() {
      var d0 = dims(), a = stage.clientWidth / Math.max(1, stage.clientHeight);
      var hw = Math.max(d0.ow / 2 + 30, (d0.od / 2 + 30) * a), hh = hw / a;
      camO.left = -hw; camO.right = hw; camO.top = hh; camO.bottom = -hh; camO.near = 1; camO.far = 2000;
      camO.position.set(0, 500, 0); camO.up.set(0, 0, -1); camO.lookAt(0, 0, 0);
      camO.updateProjectionMatrix(); ctlO.target.set(0, 0, 0); ctlO.update();
    }
    function resizeE() {
      if (!edHost || edHost.hidden) return;
      var w = stage.clientWidth, h = stage.clientHeight;
      if (!w || !h) return;
      eRenderer.setSize(w, h, false);
      camP.aspect = w / h; camP.updateProjectionMatrix();
      cvs2.width = w * devicePixelRatio; cvs2.height = h * devicePixelRatio;
      cvs2.style.width = w + 'px'; cvs2.style.height = h + 'px';
      if (cam === camO) frameTop();
    }
    var gridMat = new THREE.LineBasicMaterial({ color: C('#3a4a5e'), transparent: true, opacity: .4 });
    var ray = new THREE.Raycaster();
    var floorPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -FLOOR_TOP);
    function ndcE(e) {
      var r = eRenderer.domElement.getBoundingClientRect();
      return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    }
    function planPt(e) {
      ray.setFromCamera(ndcE(e), cam);
      var p = new THREE.Vector3();
      if (!ray.ray.intersectPlane(floorPlane, p)) return null;
      var d0 = dims();
      return { x: p.x + d0.ow / 2, y: p.z + d0.od / 2 };
    }
    function pick(e) {
      ray.setFromCamera(ndcE(e), cam);
      var hits = ray.intersectObjects(modG ? modG.children : [], true);
      for (var i = 0; i < hits.length; i++) {
        var o = hits[i].object;
        while (o && o !== modG) {
          if (o.userData.wall !== undefined) return { k: 'w', i: o.userData.wall };
          if (o.userData.island !== undefined) return { k: 's', i: o.userData.island };
          if (o.userData.item !== undefined) return { k: 'i', i: o.userData.item };
          o = o.parent;
        }
      }
      return null;
    }
    function snapEd() { var m = cur(); return JSON.stringify({ walls: m.walls, islands: m.islands, items: m.items, hmm: m.hmm, wallH: m.wallH || null, colorIdx: m.colorIdx }); }
    function pushH() { hist = hist.slice(0, hi + 1); hist.push(snapEd()); if (hist.length > 60) hist.shift(); hi = hist.length - 1; }
    function restore(s) {
      var m = cur(), d = JSON.parse(s);
      setCustom(m.id, { walls: d.walls, islands: d.islands, items: d.items, hmm: d.hmm, colorIdx: d.colorIdx });
      m.wallH = d.wallH || undefined;
      eSel = null; rebuildEd(); edPanel();
    }
    function undo() { if (hi > 0) { hi--; restore(hist[hi]); } }
    function redo() { if (hi < hist.length - 1) { hi++; restore(hist[hi]); } }
    function commit() { var m = cur(); setCustom(m.id, { walls: m.walls, islands: m.islands, items: m.items }); pushH(); rebuildEd(); edPanel(); }
    function suppress() { ctlP.enabled = false; ctlO.enabled = false; }
    function unsuppress() { ctlP.enabled = cam === camP; ctlO.enabled = cam === camO; }
    function cancelPlacing() {
      if (!state.placingItem) return;
      state.placingItem = null;
      if (edHost) edHost.querySelectorAll('.g-ed-item').forEach(function (x) { x.classList.remove('on'); });
      if (edHost && !edHost.hidden) edHint();
    }
    function onDown(e) {
      if (edHost.hidden || e.button !== 0) return;
      var m = cur();
      if (!m) return;
      var p = planPt(e), d0 = dims(), ow = d0.ow, od = d0.od;
      var inPlan = p && p.x > -8 && p.y > -8 && p.x < ow + 8 && p.y < od + 8;
      var pl = state.placingItem;
      if (pl && !pl.dragging) { suppress(); dropItem(e); return; }
      if (tool === 'erase') { var h0 = pick(e); if (h0) { suppress(); delEd(h0); commit(); } return; }
      if (tool === 'select') {
        var h1 = pick(e);
        if (h1) {
          eSel = h1; suppress();
          if (h1.k === 'i' && e.altKey) { var cc = Object.assign({}, m.items[h1.i]); m.items.push(cc); eSel = { k: 'i', i: m.items.length - 1 }; }
          var o = eSel.k === 'w' ? Object.assign({}, m.walls[eSel.i]) : eSel.k === 's' ? JSON.parse(JSON.stringify(m.islands[eSel.i])) : Object.assign({}, m.items[eSel.i]);
          if (p) eDrag = { kind: 'move', p0: p, o: o };
          edPanel(); e.preventDefault();
        } else if (eSel) { eSel = null; edPanel(); }
        return;
      }
      if (!inPlan) return;
      suppress();
      if (tool === 'wall') { eDrag = { kind: 'wall', p0: { x: snapV(p.x, e.altKey), y: snapV(p.y, e.altKey) }, cur: null }; e.preventDefault(); return; }
      if (tool === 'brush') { eDrag = { kind: 'brush', pts: [[p.x, p.y]] }; e.preventDefault(); return; }
      if (tool === 'shape') { eDrag = { kind: 'shape', p0: { x: snapV(p.x, e.altKey), y: snapV(p.y, e.altKey) }, cur: null }; e.preventDefault(); return; }
      if (tool === 'poly') {
        var sp = { x: snapV(p.x, e.altKey), y: snapV(p.y, e.altKey) };
        if (!polyPts) polyPts = [];
        if (polyPts.length > 2 && Math.hypot(sp.x - polyPts[0][0], sp.y - polyPts[0][1]) < 7) { closePoly(true); return; }
        polyPts.push([clamp(sp.x, .8, ow - .8), clamp(sp.y, .8, od - .8)]);
      }
    }
    function onMove(e) {
      if (disposed || !edHost || edHost.hidden) return;
      var m = cur();
      if (!m || !eDrag) return;
      var p = planPt(e);
      if (!p) return;
      var d0 = dims(), ow = d0.ow, od = d0.od, alt = e.altKey;
      if (eDrag.kind === 'move') {
        var dx = p.x - eDrag.p0.x, dy = p.y - eDrag.p0.y;
        if (eSel.k === 'w') {
          var w = m.walls[eSel.i], o = eDrag.o;
          var sx = snapV(o.x0 + dx, alt) - o.x0, sy = snapV(o.y0 + dy, alt) - o.y0;
          w.x0 = clamp(o.x0 + sx, 0, ow); w.x1 = clamp(o.x1 + sx, 0, ow);
          w.y0 = clamp(o.y0 + sy, 0, od); w.y1 = clamp(o.y1 + sy, 0, od);
        } else if (eSel.k === 's') {
          var s = m.islands[eSel.i], o2 = eDrag.o;
          var sx2 = snapV(dx, alt), sy2 = snapV(dy, alt);
          s.pts = o2.pts.map(function (q) { return [clamp(q[0] + sx2, 0, ow), clamp(q[1] + sy2, 0, od)]; });
        } else {
          var it = m.items[eSel.i], o3 = eDrag.o;
          it.x = clamp(snapV(o3.x + dx, alt), .8, ow - .8 - it.w);
          it.y = clamp(snapV(o3.y + dy, alt), .8, od - .8 - it.h);
        }
        setCustom(m.id, { walls: m.walls, islands: m.islands, items: m.items });
        rebuildEd(); return;
      }
      if (eDrag.kind === 'wall') {
        var dx2 = p.x - eDrag.p0.x, dy2 = p.y - eDrag.p0.y, len = Math.hypot(dx2, dy2);
        if (len < 3) { eDrag.cur = null; return; }
        var q = Math.round(Math.atan2(dy2, dx2) / (Math.PI / 4)) * (Math.PI / 4);
        var L = Math.max(0, snapV(len, alt));
        var ex2 = clamp(eDrag.p0.x + Math.cos(q) * L, .8, ow - .8), ey = clamp(eDrag.p0.y + Math.sin(q) * L, .8, od - .8);
        eDrag.cur = { x0: eDrag.p0.x, y0: eDrag.p0.y, x1: ex2, y1: ey, t: WALL_T };
        return;
      }
      if (eDrag.kind === 'brush') {
        var last = eDrag.pts[eDrag.pts.length - 1];
        if (Math.hypot(p.x - last[0], p.y - last[1]) > 3) eDrag.pts.push([clamp(p.x, .8, ow - .8), clamp(p.y, .8, od - .8)]);
        return;
      }
      if (eDrag.kind === 'shape') {
        eDrag.cur = { x0: eDrag.p0.x, y0: eDrag.p0.y, x1: clamp(snapV(p.x, alt), .8, ow - .8), y1: clamp(snapV(p.y, alt), .8, od - .8) };
      }
    }
    function onUp(e) {
      if (disposed || !edHost || edHost.hidden) return;
      var m = cur();
      if (!m) return;
      var d = eDrag;
      eDrag = null;
      unsuppress();
      var pl = state.placingItem;
      if (!d) {
        if (pl && pl.dragging) {
          if (Math.hypot(e.clientX - pl.sx, e.clientY - pl.sy) < 6) { pl.dragging = false; edHint(); }
          else {
            var r = eRenderer.domElement.getBoundingClientRect();
            var overE = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
            if (overE) dropItem(e); else cancelPlacing();
          }
        }
        return;
      }
      if (d.kind === 'move') { commit(); return; }
      if (d.kind === 'wall' && d.cur) { var c = d.cur; if (Math.hypot(c.x1 - c.x0, c.y1 - c.y0) > 6) { m.walls.push(c); commit(); } return; }
      if (d.kind === 'brush' && d.pts.length >= 2) {
        var simp = rdp(d.pts, 3.5).map(function (q) { return [snapV(q[0]), snapV(q[1])]; });
        var closed = Math.hypot(simp[0][0] - simp[simp.length - 1][0], simp[0][1] - simp[simp.length - 1][1]) < 14;
        if (closed && sub === 'island' && simp.length > 3) { m.islands.push({ pts: simp.slice(0, -1), hh: 10 }); }
        else for (var i = 0; i < simp.length - 1; i++) {
          if (Math.hypot(simp[i + 1][0] - simp[i][0], simp[i + 1][1] - simp[i][1]) > 4)
            m.walls.push({ x0: simp[i][0], y0: simp[i][1], x1: simp[i + 1][0], y1: simp[i + 1][1], t: WALL_T });
        }
        commit(); return;
      }
      if (d.kind === 'shape' && d.cur) { var pts = shapePts(d.cur); if (pts && pts.length > 2) applyPts(pts, shape !== 'arc'); commit(); }
    }
    function dropItem(e) {
      var m = cur(), pl = state.placingItem;
      state.placingItem = null;
      edHost.querySelectorAll('.g-ed-item').forEach(function (x) { x.classList.remove('on'); });
      var p = planPt(e);
      if (!p) { edHint(); return; }
      var d0 = dims(), ow = d0.ow, od = d0.od;
      if (p.x < -5 || p.y < -5 || p.x > ow + 5 || p.y > od + 5) { edHint(); return; }
      var w = Math.min(pl.t === 'cups' ? 60 : 42, ow - 4), h = Math.min(42, od - 4);
      var it = { t: pl.t, x: clamp(snapV(p.x - w / 2), .8, ow - .8 - w), y: clamp(snapV(p.y - h / 2), .8, od - .8 - h), w: w, h: h,
        hh: pl.t === 'pins' ? 20 : pl.t === 'scoop' ? 14 : pl.t === 'cups' ? 22 : pl.t === 'vslot' ? 24 : 10, step: 15, dia: 9, vd: 14 };
      m.items.push(it);
      eSel = { k: 'i', i: m.items.length - 1 };
      commit(); edHint();
    }
    function closePoly() {
      var pts = polyPts;
      polyPts = null;
      if (!pts || pts.length < 3) return;
      applyPts(pts, true); commit();
    }
    function applyPts(pts, closed) {
      var m = cur();
      if (sub === 'island' && closed) { m.islands.push({ pts: pts, hh: 10 }); return; }
      var n = pts.length, end = closed ? n : n - 1;
      for (var i = 0; i < end; i++) {
        var a = pts[i], b = pts[(i + 1) % n];
        if (Math.hypot(b[0] - a[0], b[1] - a[1]) > 4) m.walls.push({ x0: a[0], y0: a[1], x1: b[0], y1: b[1], t: WALL_T });
      }
    }
    function shapePts(c) {
      var x0 = Math.min(c.x0, c.x1), x1 = Math.max(c.x0, c.x1), y0 = Math.min(c.y0, c.y1), y1 = Math.max(c.y0, c.y1);
      var w = x1 - x0, h = y1 - y0;
      if (w < 6 || h < 6) return null;
      var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, pts = [], i, a;
      if (shape === 'circle') { for (i = 0; i < 28; i++) { a = i / 28 * Math.PI * 2; pts.push([cx + w / 2 * Math.cos(a), cy + h / 2 * Math.sin(a)]); } return pts; }
      if (shape === 'arc') { for (i = 0; i <= 14; i++) { a = Math.PI + i / 14 * Math.PI; pts.push([cx + w / 2 * Math.cos(a), cy + h / 2 * Math.sin(a)]); } return pts; }
      var r = Math.min(8, w / 2 - 1, h / 2 - 1);
      var cs = [[x1 - r, y0 + r, -Math.PI / 2, 0], [x1 - r, y1 - r, 0, Math.PI / 2], [x0 + r, y1 - r, Math.PI / 2, Math.PI], [x0 + r, y0 + r, Math.PI, Math.PI * 1.5]];
      cs.forEach(function (q) { for (var k = 0; k <= 4; k++) { var aa = q[2] + (q[3] - q[2]) * k / 4; pts.push([q[0] + r * Math.cos(aa), q[1] + r * Math.sin(aa)]); } });
      return pts;
    }
    function rdp(pts, eps) {
      if (pts.length < 3) return pts;
      var dmax = 0, idx = 0, a = pts[0], b = pts[pts.length - 1];
      for (var i = 1; i < pts.length - 1; i++) { var d = pDist(pts[i], a, b); if (d > dmax) { dmax = d; idx = i; } }
      if (dmax > eps) { var l = rdp(pts.slice(0, idx + 1), eps), r = rdp(pts.slice(idx), eps); return l.slice(0, -1).concat(r); }
      return [a, b];
    }
    function pDist(p, a, b) {
      var dx = b[0] - a[0], dy = b[1] - a[1], L = dx * dx + dy * dy;
      if (!L) return Math.hypot(p[0] - a[0], p[1] - a[1]);
      var t = clamp(((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L, 0, 1);
      return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
    }
    function delEd(h) {
      var m = cur();
      if (h.k === 'w') m.walls.splice(h.i, 1);
      else if (h.k === 's') m.islands.splice(h.i, 1);
      else m.items.splice(h.i, 1);
      if (eSel && eSel.k === h.k && eSel.i === h.i) eSel = null;
    }
    function onKey(e) {
      if (disposed || !edHost || edHost.hidden) return;
      if (e.key === 'Escape') { if (state.placingItem) { cancelPlacing(); return; } if (polyPts) { polyPts = null; return; } closeEditor(); return; }
      if (e.key === 'Enter' && polyPts && polyPts.length > 2) { closePoly(); return; }
      if ((e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
      if (e.target.tagName === 'INPUT') return;
      if ((e.key === 'Delete' || e.key === 'Backspace') && eSel) { e.preventDefault(); delEd(eSel); commit(); }
    }

    // ── боковая панель редактора ──
    var stpE = function (k, nm, v, min, max, step) {
      return '<div class="g-stp" data-e3="' + k + '" data-min="' + min + '" data-max="' + max + '" data-step="' + (step || 1) + '">' +
        '<span class="g-nm">' + nm + '</span><button data-d="-1" type="button">−</button><input type="number" value="' + v + '" step="' + (step || 1) + '"><button data-d="1" type="button">+</button></div>';
    };
    function edPanel() {
      var m = cur();
      if (!m) return;
      var side = $e('#gfxE3Side');
      var selHtml = '';
      if (eSel) {
        if (eSel.k === 'w') {
          var w = m.walls[eSel.i], L = Math.hypot(w.x1 - w.x0, w.y1 - w.y0);
          selHtml = '<p class="g-lbl">Стена · ' + L.toFixed(1) + ' мм</p><div class="g-seg">' + [.8, 1.2, 1.6].map(function (t) { return '<button data-wt="' + t + '" type="button" class="' + (Math.abs((w.t || .8) - t) < .01 ? 'on' : '') + '">' + t + '</button>'; }).join('') + '</div>' +
            '<div class="g-row" style="margin-top:8px"><button class="g-btn sm warn" data-e3act="del" type="button">Удалить ⌫</button></div>';
        } else if (eSel.k === 's') {
          var s = m.islands[eSel.i];
          selHtml = '<p class="g-lbl">Остров</p><div class="g-stack">' + stpE('ihh', 'Высота, мм', s.hh || 10, 2, 80, 1) + '</div>' +
            '<div class="g-row" style="margin-top:8px"><button class="g-btn sm warn" data-e3act="del" type="button">Удалить ⌫</button></div>';
        } else {
          var it = m.items[eSel.i], extra = '';
          if (it.t === 'cups') extra = stpE('dia', 'Ø гнезда, мм', it.dia || 9, 4, 26, .5) + stpE('step', 'Шаг, мм', it.step || 15, 8, 40, .5);
          if (it.t === 'pins') extra = stpE('step', 'Шаг, мм', it.step || 12, 8, 30, 1);
          if (it.t === 'vslot') extra = stpE('vd', 'Глубина V, мм', it.vd || 14, 4, 40, 1);
          selHtml = '<p class="g-lbl">' + ITEM_LABEL[it.t] + '</p><div class="g-stack">' + stpE('iw', 'Ширина, мм', it.w.toFixed(0), 10, 400, 10.5) + stpE('ih', 'Глубина, мм', it.h.toFixed(0), 10, 400, 10.5) + stpE('hh', 'Высота, мм', it.hh || 10, 2, 80, 1) + extra + '</div>' +
            '<div class="g-row" style="margin-top:8px"><button class="g-btn sm" data-e3act="dupI" type="button">Дублировать</button><button class="g-btn sm warn" data-e3act="del" type="button">Удалить</button></div>';
        }
      }
      var subSeg = (tool === 'poly' || tool === 'brush' || tool === 'shape')
        ? '<div class="g-sec"><p class="g-lbl">Что рисуем</p><div class="g-seg"><button data-sub="wall" type="button" class="' + (sub === 'wall' ? 'on' : '') + '">Стенку</button><button data-sub="island" type="button" class="' + (sub === 'island' ? 'on' : '') + '">Остров</button></div>' +
          (tool === 'shape' ? '<div class="g-seg" style="margin-top:6px"><button data-shape="circle" type="button" class="' + (shape === 'circle' ? 'on' : '') + '">Круг</button><button data-shape="rrect" type="button" class="' + (shape === 'rrect' ? 'on' : '') + '">Прямоуг.</button><button data-shape="arc" type="button" class="' + (shape === 'arc' ? 'on' : '') + '">Дуга</button></div>' : '') + '</div>'
        : '';
      var dh = Math.max(3, (m.hmm - 4.85) - FLOOR_TOP);
      side.innerHTML = (selHtml ? '<div class="g-sec">' + selHtml + '</div>' : '') + subSeg +
        '<div class="g-sec"><p class="g-lbl">Вставки — тяните на дно</p><div class="g-ed-items">' + ITEMS.map(function (t) { return '<div class="g-ed-item" data-item="' + t[0] + '"><i>' + t[1] + '</i><small>' + t[2] + '</small></div>'; }).join('') + '</div></div>' +
        '<div class="g-sec"><p class="g-lbl">Модуль</p><div class="g-stack">' + stpE('hmm', 'Высота, мм', (+m.hmm).toFixed(1), 12, capMM('bin'), .5) + stpE('wallH', 'Перегородки, мм', (m.wallH || dh).toFixed(1), 3, dh, .5) + '</div>' +
        '<div class="g-swatches" style="margin-top:8px">' + COLORS.map(function (c, i) { return '<button class="g-sw ' + (i === m.colorIdx ? 'on' : '') + '" data-e3c="' + i + '" type="button" style="background:' + c[1] + '"></button>'; }).join('') + '</div></div>' +
        '<div class="g-sec"><p class="g-lbl">Управление</p><div class="g-divhint">ЛКМ — инструмент · ПКМ — вращение · колесо — зум · средняя — панорама.<br>Снап 10.5 мм, Alt — 1 мм. Ctrl+Z — отмена.</div>' +
        '<div class="g-row" style="margin-top:8px"><button class="g-btn sm warn" data-e3act="clear" type="button">Очистить всё</button></div></div>';
      side.querySelectorAll('.g-ed-item').forEach(function (el) {
        el.addEventListener('pointerdown', function (e) {
          e.preventDefault();
          cancelPlacing();
          state.placingItem = { t: el.dataset.item, dragging: true, sx: e.clientX, sy: e.clientY };
          el.classList.add('on'); edHint();
        });
      });
      var plc = state.placingItem;
      if (plc) { var el2 = side.querySelector('.g-ed-item[data-item="' + plc.t + '"]'); if (el2) el2.classList.add('on'); }
      edHint();
    }
    function panelClick(e) {
      var m = cur();
      if (!m) return;
      var sb = e.target.closest('[data-sub]'); if (sb) { sub = sb.dataset.sub; edPanel(); return; }
      var sh = e.target.closest('[data-shape]'); if (sh) { shape = sh.dataset.shape; edPanel(); return; }
      var c = e.target.closest('[data-e3c]'); if (c) { setCustom(m.id, { colorIdx: +c.dataset.e3c }); pushH(); rebuildEd(); edPanel(); return; }
      var wt = e.target.closest('[data-wt]'); if (wt && eSel && eSel.k === 'w') { m.walls[eSel.i].t = +wt.dataset.wt; commit(); return; }
      var act = e.target.closest('[data-e3act]');
      if (!act) return;
      var a = act.dataset.e3act;
      if (a === 'del' && eSel) { delEd(eSel); commit(); }
      if (a === 'dupI' && eSel && eSel.k === 'i') { var c2 = Object.assign({}, m.items[eSel.i], { x: m.items[eSel.i].x + 8, y: m.items[eSel.i].y + 8 }); m.items.push(c2); eSel = { k: 'i', i: m.items.length - 1 }; commit(); }
      if (a === 'clear') { m.walls = []; m.islands = []; m.items = []; eSel = null; commit(); }
    }
    function panelStp(e) {
      var m = cur();
      if (!m) return;
      var b = e.target.closest('.g-stp button'), inp = e.target.closest('.g-stp input');
      var bx, val;
      if (b) {
        bx = b.closest('.g-stp');
        var i2 = bx.querySelector('input');
        val = clamp((+i2.value || 0) + (+b.dataset.d) * (+bx.dataset.step), +bx.dataset.min, +bx.dataset.max);
        i2.value = Math.round(val * 100) / 100;
      } else if (inp && e.type === 'change') {
        bx = inp.closest('.g-stp');
        val = clamp(+inp.value || +bx.dataset.min, +bx.dataset.min, +bx.dataset.max);
        inp.value = Math.round(val * 100) / 100;
      } else return;
      var k = bx.dataset.e3;
      if (!k) return;
      if (k === 'hmm') { setCustom(m.id, { hmm: val }); pushH(); rebuildEd(); return; }
      if (k === 'wallH') { m.wallH = val; setCustom(m.id, {}); pushH(); rebuildEd(); return; }
      if (k === 'ihh' && eSel && eSel.k === 's') { m.islands[eSel.i].hh = val; commit(); return; }
      if (eSel && eSel.k === 'i') {
        var it = m.items[eSel.i], d0 = dims();
        if (k === 'iw') it.w = Math.min(val, d0.ow - 1.6 - it.x);
        if (k === 'ih') it.h = Math.min(val, d0.od - 1.6 - it.y);
        if (k === 'hh') it.hh = val;
        if (k === 'dia') it.dia = val;
        if (k === 'step') it.step = val;
        if (k === 'vd') it.vd = val;
        commit();
      }
    }
    // ── оверлей живых размеров ──
    function proj(x, y, z) {
      var v = new THREE.Vector3(x, y, z).project(cam);
      return [(v.x + 1) / 2 * cvs2.width / devicePixelRatio, (1 - v.y) / 2 * cvs2.height / devicePixelRatio];
    }
    function drawDims() {
      if (!edHost || edHost.hidden) return;
      var m = cur();
      if (!m) return;
      var w = cvs2.width / devicePixelRatio, h = cvs2.height / devicePixelRatio;
      ctx2.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
      ctx2.clearRect(0, 0, w, h);
      ctx2.font = '600 11px "JetBrains Mono",monospace';
      var d0 = dims(), ow = d0.ow, od = d0.od, y = FLOOR_TOP + .3;
      var lbl = function (x0, y0, x1, y1, txt, col) {
        var p1 = proj(x0 - ow / 2, y, y0 - od / 2), p2 = proj(x1 - ow / 2, y, y1 - od / 2);
        ctx2.strokeStyle = col; ctx2.lineWidth = 2;
        ctx2.beginPath(); ctx2.moveTo(p1[0], p1[1]); ctx2.lineTo(p2[0], p2[1]); ctx2.stroke();
        var mx = (p1[0] + p2[0]) / 2, my = (p1[1] + p2[1]) / 2, tw = ctx2.measureText(txt).width;
        ctx2.fillStyle = 'rgba(10,14,18,.85)'; ctx2.fillRect(mx - tw / 2 - 4, my - 18, tw + 8, 15);
        ctx2.fillStyle = col; ctx2.fillText(txt, mx - tw / 2, my - 7);
      };
      if (eDrag && eDrag.kind === 'wall' && eDrag.cur) { var c = eDrag.cur; lbl(c.x0, c.y0, c.x1, c.y1, Math.hypot(c.x1 - c.x0, c.y1 - c.y0).toFixed(1) + ' мм', '#2dd47e'); }
      if (eDrag && eDrag.kind === 'shape' && eDrag.cur) { var c2 = eDrag.cur; lbl(c2.x0, c2.y0, c2.x1, c2.y0, Math.abs(c2.x1 - c2.x0).toFixed(0), '#2dd47e'); lbl(c2.x1, c2.y0, c2.x1, c2.y1, Math.abs(c2.y1 - c2.y0).toFixed(0), '#2dd47e'); }
      if (polyPts && polyPts.length) {
        ctx2.strokeStyle = '#2dd47e'; ctx2.lineWidth = 2; ctx2.beginPath();
        polyPts.forEach(function (q, i) { var p = proj(q[0] - ow / 2, y, q[1] - od / 2); if (i) ctx2.lineTo(p[0], p[1]); else ctx2.moveTo(p[0], p[1]); });
        ctx2.stroke();
        polyPts.forEach(function (q) { var p = proj(q[0] - ow / 2, y, q[1] - od / 2); ctx2.fillStyle = '#2dd47e'; ctx2.beginPath(); ctx2.arc(p[0], p[1], 3.5, 0, 7); ctx2.fill(); });
      }
      if (eDrag && eDrag.kind === 'brush' && eDrag.pts) {
        ctx2.strokeStyle = '#2dd47e'; ctx2.lineWidth = 2; ctx2.beginPath();
        eDrag.pts.forEach(function (q, i) { var p = proj(q[0] - ow / 2, y, q[1] - od / 2); if (i) ctx2.lineTo(p[0], p[1]); else ctx2.moveTo(p[0], p[1]); });
        ctx2.stroke();
      }
      if (eSel && eSel.k === 'w' && m.walls[eSel.i]) { var wl = m.walls[eSel.i]; lbl(wl.x0, wl.y0, wl.x1, wl.y1, Math.hypot(wl.x1 - wl.x0, wl.y1 - wl.y0).toFixed(1) + ' мм', '#ff7a1a'); }
      if (eSel && eSel.k === 'i' && m.items[eSel.i]) { var it = m.items[eSel.i]; lbl(it.x, it.y, it.x + it.w, it.y, it.w.toFixed(0), '#ff7a1a'); lbl(it.x + it.w, it.y, it.x + it.w, it.y + it.h, it.h.toFixed(0), '#ff7a1a'); }
    }
    // ── ghost-превью стен/фигур ──
    var ghostG = null;
    // один материал на весь редактор: раньше на каждый кадр протяжки стены создавался новый
    var ghostMat = new THREE.MeshBasicMaterial({ color: C('#2dd47e'), transparent: true, opacity: .5, depthWrite: false });
    function drawGhost() {
      if (ghostG) { eScene.remove(ghostG); ghostG.traverse(function (c) { if (c.geometry) c.geometry.dispose(); }); ghostG = null; }
      var m = cur();
      if (!m || !eDrag) return;
      var d0 = dims(), ow = d0.ow, od = d0.od;
      var mkBox = function (x0, y0, x1, y1) {
        var dx = x1 - x0, dz = y1 - y0, L = Math.hypot(dx, dz);
        if (L < 2) return null;
        var dhv = Math.max(3, (m.hmm - 4.85) - FLOOR_TOP);
        var bx = new THREE.Mesh(new THREE.BoxGeometry(1.2, dhv, L), ghostMat);
        bx.position.set((x0 + x1) / 2 - ow / 2, FLOOR_TOP + dhv / 2, (y0 + y1) / 2 - od / 2);
        bx.rotation.y = Math.atan2(dx, dz);
        return bx;
      };
      ghostG = new THREE.Group();
      if (eDrag.kind === 'wall' && eDrag.cur) { var c = eDrag.cur, b = mkBox(c.x0, c.y0, c.x1, c.y1); if (b) ghostG.add(b); }
      if (eDrag.kind === 'shape' && eDrag.cur) {
        var pts = shapePts(eDrag.cur);
        if (pts) {
          var n = pts.length, end = shape === 'arc' ? n - 1 : n;
          for (var i = 0; i < end; i++) { var b2 = mkBox(pts[i][0], pts[i][1], pts[(i + 1) % n][0], pts[(i + 1) % n][1]); if (b2) ghostG.add(b2); }
        }
      }
      eScene.add(ghostG);
    }
    var lastT = 0;
    function tickE() {
      if (disposed || !edHost || edHost.hidden) return;
      var now = performance.now();
      if (now - lastT < 25) return;
      lastT = now;
      if (ctlP.enabled) ctlP.update();
      if (ctlO.enabled) ctlO.update();
      drawGhost(); eRenderer.render(eScene, cam); drawDims();
    }
    function loopE() { if (disposed) return; requestAnimationFrame(loopE); tickE(); }
    every(tickE, 300);

    // ════════════════ запуск и жизненный цикл ════════════════
    // Отладочный хук (в прототипе был window.__gf): даёт заглянуть в сцену и состояние из
    // консоли при разборе визуальных дефектов. Ничего не меняет в работе.
    if (typeof window !== 'undefined') window.__gfx = { state: state, scene: scene, camera: camera,
      selPad: selPad, selRing: selRing, modsG: modsG, renderer: renderer, sel: function () { return selected(); },
      ghost: ghost, ghostEdges: ghostEdges, ghostFoot: ghostFoot };
    // Редактор поднимает ВТОРОЙ WebGL-контекст. Браузер держит их по счёту (≈16 на
    // страницу), а открывают редактор далеко не всегда — создаём по первому входу.
    var edReady = false;
    function ensureEditor() {
      if (edReady) return;
      edReady = true;
      initEditor3D();
      syncEd();   // подписка внутри initEditor3D не успевает на ТЕКУЩЕЕ событие шины
    }
    bus.on('edit', function () { if (state.editing) ensureEditor(); });

    if (!load()) seed();
    initPanels();
    resize(); rebuildScene(); loop(); loopE();

    function destroy() {
      if (disposed) return;
      // незаписанный дебаунс — дослать на сервер ДО взведения флага: flushSave выходит по
      // `disposed`, и в прежнем порядке последние полторы секунды работы (самый частый
      // жест на телефоне — подвинул модуль и сразу «назад») терялись молча, вместе с
      // локальной копией.
      if (saveT) { clearTimeout(saveT); try { flushSave(); } catch (e) {} }
      disposed = true;
      closeOrderDlg();
      if (state.editing) { state.editing = null; }
      if (edLayer && window.PS_Back) { var l = edLayer; edLayer = null; try { window.PS_Back.remove(l); } catch (e) {} }
      offs.forEach(function (o) { try { o[0].removeEventListener(o[1], o[2], o[3]); } catch (e) {} });
      timers.forEach(clearTimeout);
      ros.forEach(function (r) { try { r.disconnect(); } catch (e) {} });
      // forceContextLoss ОБЯЗАТЕЛЕН (конвенция screens_3d.js): dispose() освобождает
      // программы/буферы, но WebGL-контекст живёт до GC. mount создаёт ДВА контекста
      // (сцена + редактор), экран ремонтируется на каждый заход — без явного released
      // за ~8 заходов упираемся в лимит браузера (~16) и чернеет чужой 3D-вьюер.
      try { disposeDeep(scene); controls.dispose(); renderer.dispose(); renderer.forceContextLoss(); } catch (e) {}
      try {
        if (eScene) disposeDeep(eScene);
        if (ctlP) ctlP.dispose();
        if (ctlO) ctlO.dispose();
        if (eRenderer) { eRenderer.dispose(); eRenderer.forceContextLoss(); }
      } catch (e) {}
      // материалы создаются на каждый mount — разложить явно (disposeDeep трогает только геометрии)
      try {
        if (_footGeo) { _footGeo.userData.shared = false; _footGeo.dispose(); _footGeo = null; }
        if (envTex) { envTex.dispose(); envTex = null; }
        if (envTexEd) { envTexEd.dispose(); envTexEd = null; }
        ghostMat.dispose(); gridMat.dispose();
        _ringGeo.dispose();
        Object.keys(_ringMat).forEach(function (k) { _ringMat[k].dispose(); });
        Object.keys(_labTex).forEach(function (k) { _labTex[k].dispose(); });
        Object.keys(_labMat).forEach(function (k) { _labMat[k].dispose(); });
        if (contactTex) contactTex.dispose();
        Object.keys(mats).forEach(function (k) { mats[k].dispose(); });
        binMats.forEach(function (m) { m.dispose(); });
      } catch (e) {}
      try { if (edHost && edHost.parentNode) edHost.parentNode.removeChild(edHost); } catch (e) {}
      host.innerHTML = '';
    }
    // Экран проекта правит спецификацию рядом со сценой — движку нужно уметь принять
    // её обновление, не пересоздавая контекст WebGL (mount стоит дорого).
    return { destroy: destroy, setParts: setParts, setFilter: setFilter,
      setConfig: setConfig, snapshot: snapshot, flush: flushSave, addFromTemplate: addFromTemplate,
      selected: function () { var m = selected(); return m ? JSON.parse(JSON.stringify(m)) : null; } };
  }

  window.GFStudio = { mount: mount };
})();
