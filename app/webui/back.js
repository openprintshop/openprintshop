// back.js — центральный стек «куда возвращаться» для аппаратной кнопки/жеста «назад»
// (OpenTodo 85d16a2a: свайп назад иногда закрывал мини-апп вместо возврата на прошлый экран).
//
// Раньше Telegram.WebApp.BackButton показывал/прятал только app.js — по своему экранному
// стеку. Вложенные состояния (лайтбоксы, открытый раздел каталога, полноэкранные превью в
// админке) его не трогали: BackButton оставался скрыт, и системный жест на Android/iOS
// закрывал приложение целиком, а не закрывал модалку/раздел. window.PS_Back — общий
// LIFO-стек «слоёв», которым может пользоваться любой экран/компонент:
//
//   window.PS_Back.push(onBack)  — открыли слой (экран/лайтбокс/раздел): показывает
//                                  BackButton, кладёт один history-стейт про запас.
//   window.PS_Back.back()        — «сработай, будто нажали назад»: снимает верхний слой и
//                                  вызывает его onBack(). Дёргается нативным событием
//                                  backButtonClicked, попстейтом (фолбэк без Telegram —
//                                  превью в браузере) — и ЛЮБОЙ своей кнопкой «Назад»/
//                                  крестиком/тапом по фону модалки должен дёргать ЭТУ же
//                                  функцию, а не менять своё состояние напрямую. Так все
//                                  пути закрытия слоя идут через один код и стек не
//                                  рассинхронизируется с тем, что реально на экране.
//   window.PS_Back.clear()       — сбросить весь стек без вызова колбэков (прыжок в корень:
//                                  смена нижнего таба, reset()).
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  var stack = [];   // элементы — функции onBack; вершина = последний элемент массива

  var listeners = [];   // подписчики на изменение глубины стека (шапка приложения, app.js)

  // Направление последней навигации — ЕДИНСТВЕННЫЙ его источник на всё приложение (OpenTodo
  // 861eab19: «скролл перескакивает на непонятную позицию»). Раньше «назад» помечал только
  // doBack() в app.js, поэтому закрытие СЛОЯ (раздел каталога, лайтбокс) направлением не
  // считалось — восстановление скролла не срабатывало, а флаг мог залипнуть в 'back' и
  // сработать на следующем переходе ВПЕРЁД. Теперь push/clear = 'forward', back = 'back',
  // и любой путь навигации (экран, слой, свайп, попстейт) помечается одинаково.
  var dir = 'forward';

  function updateChrome() {
    // сообщаем подписчикам новую глубину — стрелка в шапке рисуется на ВСЕХ экранах
    // (фидбэк владельца 2026-07-11) и должна знать о слоях вне React-состояния App
    // (открытый раздел каталога, лайтбоксы)
    for (var i = 0; i < listeners.length; i++) { try { listeners[i](stack.length); } catch (e) {} }
    if (!tg) return;
    try {
      if (stack.length > 0) {
        if (tg.BackButton) tg.BackButton.show();
        // Страховка (Bot API 6.2+): если BackButton/жест почему-то не перехватят закрытие —
        // спросит подтверждение вместо мгновенного выхода. Есть куда возвращаться → лучше
        // лишний тап, чем потерянный расчёт/корзина.
        if (typeof tg.enableClosingConfirmation === 'function') tg.enableClosingConfirmation();
      } else {
        if (tg.BackButton) tg.BackButton.hide();
        if (typeof tg.disableClosingConfirmation === 'function') tg.disableClosingConfirmation();
      }
    } catch (e) {}
  }

  function back() {
    var fn = stack.pop();
    // Направление помечаем ТОЛЬКО когда слой реально сняли: back() по пустому стеку (шеврон в
    // корне таба, лишний попстейт) — не «назад», иначе флаг залипнет и следующий переход
    // вперёд восстановит чужую позицию скролла.
    if (fn) dir = 'back';
    updateChrome();
    if (fn) { try { fn(); } catch (e) {} }
  }

  function push(onBack) {
    stack.push(onBack);
    dir = 'forward';
    updateChrome();
    // Синтетическая history-запись про запас — фолбэк для окружений без Telegram (превью в
    // браузере) и подстраховка на случай, если клиент Telegram не перехватит жест сам:
    // history.back()/свайп сработают как «назад» по нашему стеку, а не уйдут со страницы.
    try { history.pushState({ psBack: stack.length }, ''); } catch (e) {}
    return onBack;
  }

  function clear() {
    dir = 'forward';   // прыжок в корень (смена таба, reset) — всегда «новый контент»
    if (!stack.length) return;
    stack = [];
    updateChrome();
  }

  // Снять КОНКРЕТНЫЙ слой без вызова колбэка — для cleanup при размонтировании компонента
  // с открытым слоем (ушли с экрана каталога при открытом разделе, сменился src лайтбокса):
  // иначе слой повисает и первый свайп «назад» уходит в пустоту. Уже снятый back()'ом слой —
  // тихий no-op.
  function remove(onBack) {
    var i = stack.lastIndexOf(onBack);
    if (i < 0) return;
    stack.splice(i, 1);
    updateChrome();
  }

  if (tg && tg.BackButton && typeof tg.onEvent === 'function') {
    try { tg.onEvent('backButtonClicked', back); } catch (e) {}
  }
  // Фолбэк на history: реальный свайп/кнопка «назад» браузера (превью-режим, либо клиент,
  // где BackButton не перехватил жест) — попстейт есть, стек непуст → закрываем верхний
  // слой. Пустой стек — мы ничего не открывали, не мешаем обычной навигации/уходу со страницы.
  window.addEventListener('popstate', function () {
    if (stack.length) back();
  });

  window.PS_Back = { push: push, back: back, clear: clear, remove: remove,
    size: function () { return stack.length; },   // size — для отладки/превью-проверок
    // Направление последней навигации для менеджера скролла (app.js): 'back' → вернуть
    // сохранённую позицию, 'forward' → в начало. Читатель сам помечает 'forward' после того,
    // как отработал, — чтобы один «назад» восстановил позицию ровно один раз.
    dir: function () { return dir; },
    markForward: function () { dir = 'forward'; },
    // подписка на изменение глубины (fn(depth)); возвращает функцию отписки
    onChange: function (fn) {
      listeners.push(fn);
      return function () { var i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
    } };
})();
