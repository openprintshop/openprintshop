/* Поллер реальных данных для LCD-дашборда (продакшен).
   Каждые 2 с дёргает GET /api/lcd/data и передаёт ответ в window.applyDashboardData.
   Первый успешный ответ автоматически отключает встроенную демо-симуляцию.

   Токен: если бэкенд требует ключ, открывайте страницу как /lcd/?k=СЕКРЕТ —
   параметр k прозрачно пробрасывается в запросы к API. */
(function () {
  var POLL_MS = 2000;
  var qs = new URLSearchParams(location.search);
  var key = qs.get('k') || '';
  var url = '/api/lcd/data' + (key ? '?k=' + encodeURIComponent(key) : '');

  function tick() {
    fetch(url, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (j && window.applyDashboardData) window.applyDashboardData(j);
      })
      .catch(function () { /* сеть моргнула — просто ждём следующий тик */ })
      .then(function () { setTimeout(tick, POLL_MS); });
  }

  // даём React смонтироваться, затем стартуем
  setTimeout(tick, 800);
})();
