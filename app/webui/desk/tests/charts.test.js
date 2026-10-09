// hoverIndex / barIndex — попадание курсора в точку графика на дашборде.
// Ломается тихо: подсказка просто показывает соседний день, и цифры «не сходятся» с графиком.
import { test } from 'node:test';
import assert from 'node:assert/strict';

// ui.jsx на верхнем уровне деструктурирует глобальный React — заглушка нужна ДО импорта.
global.React = {
  useState: (v) => [v, () => {}],
  useEffect: () => {},
  useLayoutEffect: () => {},
  useRef: (v) => ({ current: v }),
  useContext: () => ({}),
  useMemo: (f) => f(),
  createContext: () => ({}),
  createElement: () => null,
};
const { hoverIndex, barIndex } = await import('../src/ui.jsx');

test('hoverIndex берёт БЛИЖАЙШУЮ точку: крайние стоят ровно на границах графика', () => {
  assert.equal(hoverIndex(0, 14), 0);
  assert.equal(hoverIndex(1, 14), 13);            // правый край — сегодняшний день, он обязан ловиться
  assert.equal(hoverIndex(0.5, 14), 7);           // 0.5*13 = 6.5 → округление вверх
  assert.equal(hoverIndex(0.03, 14), 0);          // чуть правее нуля — всё ещё первая точка
  assert.equal(hoverIndex(0.05, 14), 1);
});

test('hoverIndex не вылезает за ряд при курсоре вне графика', () => {
  assert.equal(hoverIndex(-0.4, 14), 0);
  assert.equal(hoverIndex(1.7, 14), 13);
  assert.equal(hoverIndex(0.5, 1), 0);            // единственная точка
  assert.equal(hoverIndex(0.5, 0), -1);           // пустой ряд — подсказки нет
});

test('barIndex делит ширину на равные корзины: столбик, а не ближайшая граница', () => {
  assert.equal(barIndex(0, 14), 0);
  assert.equal(barIndex(0.99, 14), 13);
  assert.equal(barIndex(1, 14), 13);              // ровно правый край не должен давать 14
  assert.equal(barIndex(0.5, 4), 2);
  assert.equal(barIndex(0.24, 4), 0);
  assert.equal(barIndex(0.26, 4), 1);
});

test('barIndex переживает курсор вне графика и пустой ряд', () => {
  assert.equal(barIndex(-1, 14), 0);
  assert.equal(barIndex(9, 14), 13);
  assert.equal(barIndex(0.5, 0), -1);
});
