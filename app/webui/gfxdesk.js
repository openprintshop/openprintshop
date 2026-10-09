// gfxdesk.js — точка входа страницы «продолжить на компьютере» (/gfx/desk/{token}).
//
// Экран проекта мастерской в обычном браузере, без Telegram. Приложение целиком сюда не
// грузится намеренно (см. `_gf_desk_html` на сервере): корзина, заказы, профиль и админка
// живут на ручках, которые ключ сессии не принимают, — клиент получил бы экран из отказов.
// Поэтому здесь ровно один экран и минимальный `app`-объект вместо всего каркаса app.js.
//
// Что из `app` реально нужно GfxProjectScreen: `gfxProjectId`, `toast` и переходы (их тут
// некуда делать). `deskMode` говорит экрану спрятать то, чему на ПК нет смысла: стрелку «к
// проектам» (списка проектов в этой сессии нет) и саму кнопку «открыть на компьютере».
(function () {
  var h = React.createElement;

  function toast(msg) {
    var el = document.createElement('div');
    el.className = 'gfxd-toast';
    el.textContent = String(msg || '');
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add('out'); }, 2600);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 3000);
  }

  var app = {
    gfxProjectId: window.__GFX_PID__ || 0,
    deskMode: true,
    toast: toast,
    // Переходов из этой сессии нет: она открыта на ОДИН проект и дальше него не пускает.
    // Пустые заглушки, а не отсутствие методов: экран зовёт их без проверок, и падение
    // на `undefined is not a function` стоило бы клиенту всей страницы.
    goTab: function () {},
    openGfxProject: function () {},
    can: function () { return false; },
  };

  var style = document.createElement('style');
  style.textContent = '.gfxd-toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);'
    + 'z-index:999;background:var(--surface,#161b22);border:1px solid var(--line,#2a3138);'
    + 'color:var(--text,#e8edf2);border-radius:12px;padding:10px 16px;font-size:13.5px;'
    + 'box-shadow:0 8px 28px rgba(0,0,0,.5);transition:opacity .3s}'
    + '.gfxd-toast.out{opacity:0}';
  document.head.appendChild(style);

  if (!window.GfxProjectScreen) {
    document.getElementById('app-root').textContent =
      'Не удалось загрузить экран проекта — обновите страницу.';
    return;
  }
  ReactDOM.createRoot(document.getElementById('app-root'))
    .render(h(window.GfxProjectScreen, { app: app }));
})();
