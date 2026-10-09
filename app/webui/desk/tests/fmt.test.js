// Форматирование: деньги, вес, время, склонения. Ошибки здесь не видны в тестах бэкенда,
// но мастер читает эти цифры каждый день.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { money, grams, minutes, plural, cnt, num, CUR, curSym, rememberCurrency } from '../src/fmt.jsx';

const nb = (s) => s.replace(/ /g, ' ');   // неразрывные пробелы из toLocaleString

test('валюта берётся из ответа сервера, а не зашита в код', () => {
  rememberCurrency({ currency: 'Br' });
  assert.ok(nb(money(10)).endsWith('Br'));
  rememberCurrency({ currency: 'руб.' });
  assert.ok(nb(money(10)).endsWith('руб.'));
  assert.equal(CUR.v, 'руб.');
});

test('подписи полей и сумма рядом с ними берут валюту из ОДНОГО источника', () => {
  // В подписях («Цена, …», «₽/кг») символ валюты был зашит, и у студии на BYN заголовок
  // расходился со значением под ним. curSym() — тот же CUR.v, что печатает money().
  rememberCurrency({ currency: 'Br' });
  assert.equal(curSym(), 'Br');
  assert.ok(money(10).endsWith(curSym()), 'подпись и сумма разошлись');
  rememberCurrency({ currency: 'руб.' });
  assert.equal(curSym(), 'руб.');
  assert.ok(money(10).endsWith(curSym()));
});

test('деньги: разряды и знак плюса только у прихода', () => {
  assert.equal(nb(money(12480)), '12 480 руб.');
  assert.equal(nb(money(340, { sign: true })), '+340 руб.');
  assert.equal(nb(money(-340, { sign: true })), '-340 руб.', 'минус уже есть в числе');
  assert.equal(nb(money(0)), '0 руб.');
  assert.equal(nb(money(null)), '0 руб.', 'пусто от сервера не должно давать NaN');
});

test('вес: до 100 г с десятыми, дальше целые — грамм печати важнее долей', () => {
  // разделитель дробной части — запятая: локаль ru-RU, а не точка
  assert.equal(nb(grams(86.44)), '86,4 г');
  assert.equal(nb(grams(1314.7)), '1 315 г');
  assert.equal(nb(grams(0)), '0 г');
});

test('время: часы и минуты, без «0 ч»', () => {
  assert.equal(minutes(45), '45 м');
  assert.equal(minutes(60), '1 ч');
  assert.equal(minutes(320), '5 ч 20 м');
  assert.equal(minutes(0), '0 м');
  assert.equal(minutes(null), '0 м');
});

test('склонения по-русски, включая коварные 11-14', () => {
  const f = (n) => plural(n, 'заказ', 'заказа', 'заказов');
  assert.equal(f(1), 'заказ');
  assert.equal(f(2), 'заказа');
  assert.equal(f(5), 'заказов');
  assert.equal(f(11), 'заказов', '11 — не «заказ»');
  assert.equal(f(12), 'заказов');
  assert.equal(f(21), 'заказ');
  assert.equal(f(22), 'заказа');
  assert.equal(f(111), 'заказов');
  assert.equal(f(0), 'заказов');
});

test('cnt склеивает число со склонением', () => {
  assert.equal(nb(cnt(3, 'заказ', 'заказа', 'заказов')), '3 заказа');
  assert.equal(nb(cnt(1000, 'модель', 'модели', 'моделей')), '1 000 моделей');
});

test('num не падает на пустоте от сервера', () => {
  assert.equal(num(null), '0');
  assert.equal(num(undefined), '0');
  assert.equal(nb(num(3633)), '3 633');
});
