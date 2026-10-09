// isToggleKey — правило «настройка рисуется тумблером, а не полем». Ломается тихо: булев флаг
// молча превратился бы в числовое поле «впиши 1» (ровно та UX-боль, ради которой правило и есть),
// поэтому контракт фиксируем тестом.
import { test } from 'node:test';
import assert from 'node:assert/strict';

// settings.jsx на верхнем уровне деструктурирует глобальный React и тянет api.jsx (window.API) —
// заглушки нужны ДО импорта, поэтому импорт динамический (esbuild инициализирует модуль на await).
global.React = {
  useContext: () => ({}),
  useState: (v) => [v, () => {}],
  useEffect: () => {},
  useRef: (v) => ({ current: v }),
  useCallback: (f) => f,
  useMemo: (f) => f(),
  createContext: () => ({}),
  createElement: () => null,
  Fragment: 'Fragment',
};
global.window = { API: {} };
const { isToggleKey } = await import('../src/screens/settings.jsx');

test('булев ключ *_enabled — тумблер', () => {
  assert.equal(isToggleKey('nudge_abandoned_enabled', 0), true);
  assert.equal(isToggleKey('nudge_abandoned_enabled', 1), true);
});

test('исторические тумблеры распознаются по имени', () => {
  assert.equal(isToggleKey('slicing_enabled', 0), true);
  assert.equal(isToggleKey('sanitize_3mf', 1), true);
  assert.equal(isToggleKey('time_prediction', 0), true);
});

test('числовые пороги — не тумблеры, даже операционные', () => {
  assert.equal(isToggleKey('nudge_abandoned_hours', 24), false);
  assert.equal(isToggleKey('rush_surcharge_pct', 50), false);
  assert.equal(isToggleKey('term_split_hours', 12), false);
  assert.equal(isToggleKey('dfm_min_wall_mm', 0.8), false);
});

test('не-булево значение у *_enabled — НЕ тумблер (иначе поле схлопнулось бы в 0/1)', () => {
  // значение вне {0,1} значит «это не флаг» — рисуем полем, а не переключателем
  assert.equal(isToggleKey('some_enabled', 5), false);
  assert.equal(isToggleKey('sanitize_3mf', 7), false);
});
