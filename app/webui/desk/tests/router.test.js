// Адресация настольной админки: deep-links из телеграм-уведомлений и хэш-роутинг.
// Это чистая логика без React — ломается тихо, а последствия видны только в проде
// (мастер жмёт кнопку в уведомлении и попадает не туда).
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { toHash, parseHash, bootRoute, HOME } from '../src/router.jsx';

/** Подменяем окно: роутер читает location и пишет в history. */
function fakeWindow(search = '', hash = '') {
  const calls = [];
  global.window = {
    location: { search, hash, pathname: '/app/desk/' },
    history: { replaceState: (_s, _t, url) => calls.push(url) },
  };
  return calls;
}

test('toHash собирает адрес и выкидывает пустые параметры', () => {
  assert.equal(toHash('orders', { sel: 147 }), '#/orders?sel=147');
  assert.equal(toHash('pult', {}), '#/pult');
  assert.equal(toHash('clients', { sel: null, tab: '' }), '#/clients');
});

test('parseHash разбирает экран и параметры, sel приводит к числу', () => {
  fakeWindow('', '#/orders?sel=147');
  const r = parseHash();
  assert.equal(r.screen, 'orders');
  assert.equal(r.sel, undefined);
  assert.equal(r.params.sel, 147, 'sel должен быть числом: экран сравнивает его с o.id');
});

test('parseHash без хэша возвращает null, а не выдуманный экран', () => {
  fakeWindow('', '');
  assert.equal(parseHash(), null);
});

test('deep-link ?go=adminorder&id= разворачивается в экран заказов', () => {
  const urls = fakeWindow('?go=adminorder&id=42', '');
  const r = bootRoute();
  assert.equal(r.screen, 'orders');
  assert.equal(r.params.sel, 42);
  // ?go= обязан вычищаться из адреса, иначе он «залипнет» при каждом F5
  assert.ok(urls.length === 1 && !urls[0].includes('go='), `адрес не очищен: ${urls[0]}`);
  assert.ok(urls[0].includes('#/orders?sel=42'));
});

test('deep-link сохраняет чужие параметры адреса (например токен лаунчера)', () => {
  const urls = fakeWindow('?admintoken=abc&go=clients&id=7', '');
  bootRoute();
  assert.ok(urls[0].includes('admintoken=abc'), `токен потерян: ${urls[0]}`);
});

test('неизвестный ?go= не роняет загрузку, а открывает Пульт', () => {
  fakeWindow('?go=НЕТ_ТАКОГО', '');
  assert.equal(bootRoute().screen, HOME);
});

test('алиасы уведомлений ведут на экраны настольной админки', () => {
  const cases = [['adminorder', 'orders'], ['adminfamilies', 'families'],
    ['printclassify', 'notifs'], ['adminqueuefix', 'journal'], ['client', 'clients']];
  for (const [go, screen] of cases) {
    fakeWindow(`?go=${go}`, '');
    assert.equal(bootRoute().screen, screen, `алиас ${go}`);
  }
});

test('без ?go= берётся хэш, а без хэша — Пульт', () => {
  fakeWindow('', '#/dash');
  assert.equal(bootRoute().screen, 'dash');
  fakeWindow('', '');
  assert.equal(bootRoute().screen, HOME);
});
