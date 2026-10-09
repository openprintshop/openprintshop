// normPrinter — нормализация ответа /api/admin/printer_status (включая extras с бэкенда).
// Ломается тихо: поля просто пропадают с «Печати»/«Пульта», поэтому фиксируем контракт.
import { test } from 'node:test';
import assert from 'node:assert/strict';

// parts.jsx на верхнем уровне деструктурирует глобальный React — заглушка нужна ДО импорта,
// поэтому импорт динамический (esbuild инициализирует модуль в момент await).
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
  memo: (c) => c,
  forwardRef: (c) => c,
};
const { normPrinter } = await import('../src/parts.jsx');

test('normPrinter разворачивает extras: свет, камера, филамент, слоты', () => {
  const pr = normPrinter({
    ok: true, online: true, serial: 'SN',
    state: {}, summary: { extruder_temp: 29, bed_temp: 26 },
    extras: {
      light: 1, chamber_temp: 29, filament_detected: 0, filament_sensor_on: 1,
      fans: { model: 100, box: 50 },
      position: { x: 52.5, y: 264, z: 80 },
      homed_axes: 'xyz', u_disk: false, exceptions: [],
      slots: [{ slot: 1, brand: 'ELEGOO', name: 'PLA Silk', type: 'PLA',
        color: '#A03BF7', temp_min: 190, temp_max: 230, status: 1 }],
      active_slot: 0,
    },
  });
  assert.equal(pr.light, true);
  assert.equal(pr.chamber, 29);
  assert.equal(pr.filament, false);          // 0 → прутка нет
  assert.equal(pr.filamentSensorOn, true);
  assert.deepEqual(pr.fans, { model: 100, box: 50 });
  assert.deepEqual(pr.pos, { x: 52.5, y: 264, z: 80 });
  assert.equal(pr.homedAxes, 'xyz');
  assert.equal(pr.uDisk, false);
  assert.equal(pr.slots.length, 1);
  assert.equal(pr.slots[0].color, '#A03BF7');
  assert.equal(pr.activeSlot, 0);            // 0 = ничего не подаётся (а не «нет данных»)
});

test('normPrinter: состояние машины, пауза, режим скорости, прошивка сообщества', () => {
  const pr = normPrinter({
    ok: true, online: true, state: {}, summary: {},
    extras: { speed_mode: 2, auto_refill: true,
      machine: { status: 2, sub_status: 2502, label: 'Печать', detail: 'Пауза', paused: true } },
    driver: { kind: 'elegoo', caps: { exclude_object: true }, community: { firmware: '4.2', control: '1.1.31' } },
  });
  assert.equal(pr.machine.detail, 'Пауза');
  assert.equal(pr.paused, true);
  assert.equal(pr.speedMode, 2);
  assert.equal(pr.autoRefill, true);
  assert.equal(pr.community.control, '1.1.31');
  assert.equal(pr.can('exclude_object'), true);
  assert.equal(pr.can('bed_mesh'), false);
  // старый ответ без machine — пауза «неизвестна», а не «нет»
  const old = normPrinter({ ok: true, online: true, state: {}, summary: {}, extras: {} });
  assert.equal(old.paused, null);
  assert.equal(old.community, null);
});

test('normPrinter: connected управляет флагом «данные устарели»', () => {
  const base = { ok: true, online: true, cached: true, state: {}, summary: {} };
  // соединение подписчика живо → кэш актуален, даже если принтер давно молчит (простой)
  assert.equal(normPrinter({ ...base, connected: true, age: 300 }).stale, false);
  // соединение упало → данные устарели, каким бы свежим ни был последний кадр
  assert.equal(normPrinter({ ...base, connected: false, age: 2 }).stale, true);
  // старый контракт без поля connected (до рестарта бота) — возрастной критерий
  assert.equal(normPrinter({ ...base, age: 40 }).stale, true);
  assert.equal(normPrinter({ ...base, age: 5 }).stale, false);
});

test('normPrinter без extras: новые поля нейтральны, ничего не выдумано', () => {
  const pr = normPrinter({ ok: true, online: true, state: {}, summary: {} });
  assert.equal(pr.light, null);
  assert.equal(pr.chamber, null);
  assert.equal(pr.filament, null);
  assert.equal(pr.fans, null);
  assert.equal(pr.pos, null);
  assert.deepEqual(pr.exceptions, []);
  assert.deepEqual(pr.slots, []);
  assert.equal(pr.activeSlot, null);
});

// ── способности принтера (цикл 3, волна 3) ──────────────────────────────────────────────────
test('normPrinter достаёт способности из блока driver', () => {
  const pr = normPrinter({
    ok: true, online: true, state: {}, summary: {},
    driver: { kind: 'moonraker', title: 'Klipper (Moonraker)', enabled: true,
              caps: { status: true, control: false, move: false, upload: true },
              upload_exts: ['.gcode'] },
  });
  assert.equal(pr.kind, 'moonraker');
  assert.equal(pr.title, 'Klipper (Moonraker)');
  assert.equal(pr.can('status'), true);
  assert.equal(pr.can('control'), false, 'кнопка «пауза» обещала бы то, чего принтер не умеет');
  assert.equal(pr.can('move'), false);
  assert.deepEqual(pr.uploadExts, ['.gcode']);
});

test('ответ без driver считается «умею всё» — старый бот до рестарта', () => {
  // Переходный период: бандл обновился, бот ещё нет. Считать всё запрещённым значило бы
  // отобрать у мастера пульт исправного принтера.
  const pr = normPrinter({ ok: true, online: true, state: {}, summary: {} });
  assert.equal(pr.can('control'), true);
  assert.equal(pr.can('чего-то-невиданного'), true);
});

test('честный отказ «телеметрию не отдаю» доносит способности, а не теряется', () => {
  // Сервер отвечает {ok:false, driver:…} — это не обрыв связи, а свойство принтера.
  const pr = normPrinter({
    ok: false, error: 'Klipper: телеметрию этот принтер не отдаёт',
    driver: { kind: 'none', title: 'Только профиль слайсера', enabled: false,
              caps: { status: false, control: false }, note: 'связь появится с приездом машины' },
  });
  assert.equal(pr.online, false);
  assert.equal(pr.can('status'), false);
  assert.equal(pr.note, 'связь появится с приездом машины');
});
