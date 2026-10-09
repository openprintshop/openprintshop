"""Слой доступа к данным (SQLite через aiosqlite): жизненный цикл БД + фасад пакета app.store.

Что осталось здесь навсегда (волна 7 разреза довела состав до целевого):
- `_SCHEMA` и версионные миграции (`SCHEMA_VERSION`, PRAGMA user_version) — ДОГОВОР с боевым
  файлом БД: побайтовая дословность и порядок шагов неприкосновенны;
- `init()` / `connect_existing()` / `close()` / `_open()` / `_ensure_column()` и сиды-бэкфиллы;
- ФАСАД слоя данных — реэкспорт имён доменных модулей (блок в конце файла).

Карта пакета app/store (номер — волна разреза; отчёт программы — docs/Уже в продукте/
refactoring-db-plan-2026-08-21.md):
- core         (0) — соединение `_db`/`_conn()`/`set_db()` и инфраструктурный `_jload`
- models       (1) — dataclass-модели строк и статусы заказов
- settings     (2) — числовые и текстовые настройки мастерской + дефолты и подписи
- plastics     (2) — склад филамента целиком: пластики, остатки по цветам, катушки, леджер
- catalog      (2) — сопла, пресеты качества/прочности, скидки за тираж, постобработка
- printers     (3) — карточки принтеров, ТО, пластины стола
- printjournal (3) — журнал печатей, телеметрия, мост в калибровку
- printjob     (3) — привязка текущей печати и снимок настроек задания
- printqueue   (3) — очередь печати с позициями
- orders       (4) — заказ как сущность: requests, таймлайн, корзина-группа, доставка
- money        (4) — личный баланс, онлайн-платежи A1, семейный счёт
- collection   (5) — каталог моделей, поиск, избранное, рекомендации, кэши расчёта
- profile      (5) — профиль клиента, XP, брошенная корзина, сохранённые модели, идеи
- chat         (5) — переписка мастер<->клиент и карта сообщений Telegram
- crm          (6) — клиент как объект, задачи, лента активности, промокоды, рефералка
- ops          (6) — служебные журналы админки: usage, бэкапы, ошибки, уведомления
- team         (6) — роли сотрудников и личный вход в деск
- products     (6) — витрина готовых изделий
- calib        (6) — калибровка: кампании, шаги, замеры, образцы, паспорта
- kb           (6) — база знаний владельца студии
- timepred     (7) — предсказание времени печати: замеры, коррекция оценки, сводка данных
- analytics    (7) — производные цифры без своей таблицы: сроки и админ-дашборд
- deferred     (Э0) — очередь досчёта цены (заказ без цены переживает рестарт) и дедуп приёмной

Ядро расчёта в `_SCHEMA` (8 таблиц из примерно семидесяти): plastics, settings, text_settings,
printers, quality_presets, strength_presets, discount_tiers, orders.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiosqlite

from . import calibration, config, materials, nozzles, presets, profilepaths, slicers
from .store import core

log = logging.getLogger(__name__)

# Версия схемы (PRAGMA user_version): поднимается КАЖДОЙ одноразовой миграцией данных —
# пересозданием таблицы, бэкфиллом, сносом. Обычные CREATE TABLE IF NOT EXISTS и
# _ensure_column версии не требуют: они идемпотентны сами по себе.
SCHEMA_VERSION = 11


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plastics(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  price_per_kg REAL NOT NULL,
  density REAL NOT NULL DEFAULT 1.24,
  is_active INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS text_settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS printers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  machine_profile TEXT NOT NULL,
  process_dir TEXT NOT NULL,
  filament_dir TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0
);
-- Сопла принтера (OpenTodo f71f4973). У принтера столько машинных профилей, сколько сопел:
-- профиль несёт nozzle_diameter, и слайсить процессом сопла 0.6 по профилю 0.4 нельзя —
-- ширины линий и расход считаются по чужому диаметру. printers.machine_profile остаётся
-- профилем ПО УМОЛЧАНИЮ (обратная совместимость), здесь — полный набор.
CREATE TABLE IF NOT EXISTS printer_nozzles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id INTEGER NOT NULL,
  nozzle TEXT NOT NULL,
  machine_profile TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_printer_nozzle_key ON printer_nozzles(printer_id, nozzle);
CREATE TABLE IF NOT EXISTS quality_presets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  layer_height REAL NOT NULL,
  process TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS strength_presets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  infill INTEGER NOT NULL,
  walls INTEGER NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  is_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discount_tiers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  min_qty INTEGER NOT NULL,
  discount_pct REAL NOT NULL
);
-- Опции постобработки (B4): платные ручные услуги мастера ПОВЕРХ печати — шлифовка, покраска,
-- латунные вставки и т.п. Каталог редактирует мастер в десктоп-админке; цена задаётся розничная,
-- маржа/скидка на неё НЕ накидываются (это готовая цена услуги). Каталог по умолчанию ПУСТ.
-- per_part=1 → price трактуется как ставка ЗА ДЕТАЛЬ (отдельный элемент модели, не пластину):
-- итог услуги = ставка × число деталей × тираж (кейс «склейка 20 коп/деталь», OpenTodo accee7a9).
CREATE TABLE IF NOT EXISTS postproc_options(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  price REAL NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0,
  per_part INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0
);
-- Снимок выбранных опций на КОНКРЕТНЫЙ заказ (данные-сейф): имя+цена фиксируются в момент
-- оформления, поэтому поздняя правка каталога НЕ переписывает историю заказа. option_id может
-- стать NULL, если строку каталога позже удалили — имя/цена в снимке остаются.
CREATE TABLE IF NOT EXISTS request_options(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL,
  option_id INTEGER,
  name TEXT NOT NULL,
  price REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reqopt_req ON request_options(request_id);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  model_label TEXT,
  quantity INTEGER,
  quality TEXT,
  strength TEXT,
  plastic_id INTEGER,
  printer TEXT,
  grams_total REAL,
  minutes_total REAL,
  total_price REAL,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS print_samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id   INTEGER,
  model_hash   TEXT,
  est_minutes  REAL NOT NULL,
  actual_minutes REAL NOT NULL,
  volume_cm3   REAL,
  grams        REAL,
  bbox_x REAL, bbox_y REAL, bbox_z REAL,
  bbox_volume  REAL,
  parts        INTEGER,
  layer_height REAL,
  infill_pct   INTEGER,
  walls        INTEGER,
  material_type TEXT,
  extra_colors INTEGER NOT NULL DEFAULT 0,
  process_name TEXT,
  printer_id   INTEGER,
  source       TEXT NOT NULL DEFAULT 'manual',
  created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_psamp_hash ON print_samples(model_hash);
CREATE TABLE IF NOT EXISTS plastic_color_stock(
  plastic_id INTEGER NOT NULL,
  color TEXT NOT NULL,
  stock_grams REAL NOT NULL DEFAULT 0,
  low_threshold REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(plastic_id, color)
);
CREATE TABLE IF NOT EXISTS self_prints(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plastic_name TEXT NOT NULL DEFAULT '',
  grams REAL NOT NULL DEFAULT 0,
  minutes REAL NOT NULL DEFAULT 0,
  label TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
-- Катушки (мастер-план учёта, docs/В процессе реализации/inventory_master_plan.md, Фаза A):
-- физическая единица пластика = катушка. Остаток ведём НЕ ручной перезаписью числа, а
-- неизменяемым журналом движений stock_ledger — по образцу пары user_balance/balance_tx.
-- remaining_g — КЭШ = SUM(stock_ledger.delta_g по этой катушке) (пересчитывается, не источник
-- истины). net_weight_g — номинал (для «сколько % осталось»). color — нормализованный ключ
-- (см. _canon_color). status: active | empty | archived.
CREATE TABLE IF NOT EXISTS spools(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plastic_id INTEGER NOT NULL,
  color TEXT NOT NULL DEFAULT '',
  net_weight_g REAL NOT NULL DEFAULT 0,
  remaining_g REAL NOT NULL DEFAULT 0,
  low_threshold REAL NOT NULL DEFAULT 0,
  cost REAL,
  supplier TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  note TEXT,
  purchased_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spools_pcs ON spools(plastic_id, color, status);
-- Журнал движений остатка (неизменяемый, как balance_tx). delta_g: + приход/коррекция,
-- − расход. reason: purchase | backfill_initial | print | purge | correction | writeoff.
-- ref_type/ref_id — на что ссылается движение (request | journal | print_event | ...).
CREATE TABLE IF NOT EXISTS stock_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spool_id INTEGER NOT NULL,
  delta_g REAL NOT NULL,
  reason TEXT NOT NULL,
  ref_type TEXT,
  ref_id INTEGER,
  note TEXT,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_spool ON stock_ledger(spool_id, id);
CREATE TABLE IF NOT EXISTS user_cart(
  user_id INTEGER PRIMARY KEY,
  items TEXT NOT NULL DEFAULT '[]',
  total TEXT NOT NULL DEFAULT '',
  count INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  nudged_at INTEGER NOT NULL DEFAULT 0
);
-- Мультифайловый заказ-корзина (B5): шапка группы над позициями. Сами позиции остаются
-- строками requests с общим requests.order_group = order_groups.id — requests не нормализуем
-- в order/order_items (downstream: статусы/журнал/очередь/desk/CSV читают requests по строке).
-- Итог группы ДЕРИВИРУЕТСЯ из суммы цен позиций; total_snapshot — лишь снимок на момент
-- оформления, не авторитет. Доставка в MVP информативна (способ+адрес+комментарий, без
-- автосписания). Строка может ОТСУТСТВОВАТЬ у старых cart-заказов (до бэкфилла v6) —
-- свёртка обязана работать при её отсутствии (fallback: total = сумма позиций, доставка пуста).
CREATE TABLE IF NOT EXISTS order_groups(
  id TEXT PRIMARY KEY,            -- uuid.hex[:12], тот же, что в requests.order_group
  user_id INTEGER NOT NULL,
  comment TEXT,                  -- общий комментарий-пожелание к заказу
  delivery_method TEXT,          -- способ получения (информативно, без автосписания)
  delivery_address TEXT,
  total_snapshot REAL,           -- снимок суммарного итога на момент оформления
  batch INTEGER NOT NULL DEFAULT 0,   -- клиент попросил печать партией (idea #14)
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_models(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  hash TEXT NOT NULL,
  label TEXT NOT NULL,
  paths TEXT NOT NULL,
  preview_path TEXT,
  printer_id INTEGER,
  plastic_id INTEGER,
  quality_id INTEGER,
  strength_id INTEGER,
  quantity INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_models(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS user_discounts(
  user_id INTEGER PRIMARY KEY,
  discount_pct REAL NOT NULL,
  name TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS collection(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hash TEXT UNIQUE,
  label TEXT NOT NULL,
  paths TEXT NOT NULL,
  preview_path TEXT,
  photo_path TEXT,
  added_by INTEGER,
  uses INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites(
  user_id INTEGER NOT NULL,
  col_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, col_id)
);
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  user_name TEXT,
  model_label TEXT,
  printer TEXT,
  plastic_name TEXT,
  quantity INTEGER,
  quality TEXT,
  strength TEXT,
  grams_total REAL,
  minutes_total REAL,
  price REAL,
  status TEXT NOT NULL DEFAULT 'new',
  pending_price REAL,
  preview_path TEXT,
  price_comment TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_req_user ON requests(user_id, id DESC);
CREATE TABLE IF NOT EXISTS color_shades(
  name TEXT PRIMARY KEY,
  hex TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS req_status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rsh_req ON req_status_history(request_id, id);
-- Привязка ТЕКУЩЕЙ печати к заказу (OpenTodo 70b5a590): единственная строка (id=1).
-- was_active — видели ли печать активной (для детекта финиша), finished — финиш зафиксирован
-- и мастеру отправлено подтверждение, confirmed — мастер подтвердил «всё ок».
-- Ключ — МАШИНА, а не константа 1 (цикл 3, волна 6). При одновременной печати на двух
-- принтерах единственная строка означала бы, что вторая привязка вытесняет первую: заказ
-- «потерял» бы свою печать, а её финиш закрыл бы чужой заказ. printer_id = 0 — «машина
-- неизвестна» (установка без карточек, всё из .env).
CREATE TABLE IF NOT EXISTS print_binding(
  printer_id INTEGER PRIMARY KEY,
  request_id INTEGER,
  job_name TEXT,
  started_at INTEGER,
  was_active INTEGER NOT NULL DEFAULT 0,
  finished INTEGER NOT NULL DEFAULT 0,
  confirmed INTEGER NOT NULL DEFAULT 0
);
-- Аватарки профиля (OpenTodo fca4246e): сам PNG лежит в data/avatars/<user_id>.png,
-- здесь — промт и сид (чтобы показать подпись и пережить перезапуск).
CREATE TABLE IF NOT EXISTS user_avatars(
  user_id INTEGER PRIMARY KEY,
  prompt TEXT,
  seed TEXT,
  updated_at INTEGER NOT NULL
);
-- Онлайн-платежи Telegram Payments (A1, app/payments.py): леджер успешных оплат.
-- tg_charge_id UNIQUE — идемпотентность: повторная доставка successful_payment не задвоит
-- запись. Сумма в валюте счёта (мажорные единицы), provider_charge_id — для сверки
-- с кабинетом провайдера (ЮKassa/bePaid/…) и возвратов.
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  currency TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'telegram',
  tg_charge_id TEXT UNIQUE,
  provider_charge_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_req ON payments(request_id);
-- Баланс клиента в личном кабинете (OpenTodo 8c428c43): мастер зачисляет принесённые деньги,
-- остаток хранится на будущее. balance_tx — журнал движений (прозрачность для клиента и админа).
CREATE TABLE IF NOT EXISTS user_balance(
  user_id INTEGER PRIMARY KEY,
  balance REAL NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS balance_tx(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  reason TEXT,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_baltx_user ON balance_tx(user_id, id DESC);
-- Личные настройки клиента (OpenTodo 0d11db78). Пока одна: что делать, когда мастер поднял
-- цену по заказу. Строка создаётся лениво — у большинства настройки по умолчанию, и заводить
-- её каждому, кто просто открыл приложение, незачем.
--   price_mode: 'ask'  — всегда подтверждать вручную (как было всегда, значение по умолчанию);
--               'auto' — всегда принимать новую цену;
--               'pct'  — принимать, если цена выросла не больше чем на price_pct процентов.
CREATE TABLE IF NOT EXISTS user_prefs(
  user_id INTEGER PRIMARY KEY,
  price_mode TEXT NOT NULL DEFAULT 'ask',
  price_pct REAL NOT NULL DEFAULT 0,
  -- Доставка по умолчанию (A2, доработка 31.07): адрес и тип, которые клиент задал один раз
  -- в профиле. Подставляются в корзину и — главное — применяются к заказу БЕЗ корзины, где
  -- спросить адрес негде. 0/'' = не задано (тогда заказ идёт как самовывоз, как и раньше).
  delivery_type_id INTEGER NOT NULL DEFAULT 0,
  delivery_address TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_codes(
  code TEXT PRIMARY KEY,
  discount_pct REAL NOT NULL,
  expires_at INTEGER NOT NULL DEFAULT 0,
  max_uses INTEGER NOT NULL DEFAULT 0,
  used_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS referrals(
  referee_id INTEGER PRIMARY KEY,
  referrer_id INTEGER NOT NULL,
  rewarded INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
-- Просмотренные достижения (профиль v2): красная точка на чипе и тег NEW гаснут после
-- открытия экрана профиля (?seen=1). Достижение «просмотрено» = есть строка здесь.
CREATE TABLE IF NOT EXISTS ach_seen(
  user_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  seen_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, key)
);
-- Журнал печатей мастера (OpenTodo ed1518ed): ВСЕ печати (удачи и брак) для анализа пластика
-- и брака; result: ok | fail. Источник данных для Big Data и будущих ML-алгоритмов.
CREATE TABLE IF NOT EXISTS print_journal(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  plastic_id INTEGER,
  plastic_name TEXT NOT NULL DEFAULT '',
  grams REAL NOT NULL DEFAULT 0,
  minutes REAL NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT 'ok',
  fail_reason TEXT,
  note TEXT,
  created_at INTEGER NOT NULL
);
-- Настройки, с которыми задание уехало на принтер (OpenTodo 8e8d4873). Ключ — имя файла на
-- принтере: по нему же printwatch заводит запись журнала, поэтому снимок находится к печати
-- сам. values_json — выжимка конфиг-блока gcode (app/gcodemeta), start_json — параметры
-- САМОГО старта (пластина, слоты хаба, автокалибровка стола): «чем резал» и «как отправил» —
-- разные вопросы, и мастер задаёт оба.
CREATE TABLE IF NOT EXISTS print_settings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,                        -- имя gcode-файла на принтере
  norm_name TEXT NOT NULL DEFAULT '',        -- то же имя после elegoo.normalize_job_name
  values_json TEXT NOT NULL DEFAULT '{}',
  start_json TEXT NOT NULL DEFAULT '{}',
  slicer TEXT NOT NULL DEFAULT '',
  grams REAL, minutes REAL, layers INTEGER,
  uploaded_at INTEGER,                       -- когда файл залит через админку
  started_at INTEGER,                        -- когда с него запускали печать
  created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_print_settings_name ON print_settings(name);
CREATE INDEX IF NOT EXISTS idx_print_settings_norm ON print_settings(norm_name);
-- История телеметрии принтера (фидбэк 20.07: «тащить данные всегда — для мгновенных страниц
-- и будущей аналитики»). Пишет printwatch по тику: каждый тик во время печати, разреженно в
-- простое. Ретенция — prune_printer_telemetry (дни), чтобы БД не пухла бесконечно.
CREATE TABLE IF NOT EXISTS printer_telemetry(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,                       -- unix-время снимка
  printing INTEGER NOT NULL DEFAULT 0,
  nozzle REAL, nozzle_target REAL,
  bed REAL, bed_target REAL,
  chamber REAL,                              -- ztemperature_sensor (датчик в камере)
  progress REAL,                             -- 0..1, только во время печати
  layer INTEGER,
  filename TEXT NOT NULL DEFAULT '',
  light INTEGER,                             -- 1/0, если принтер вещал led.status
  active_slot INTEGER,                       -- слот CANVAS, который подаёт (0 = никакой)
  fan_model INTEGER                          -- обдув модели, %
);
CREATE INDEX IF NOT EXISTS idx_ptele_ts ON printer_telemetry(ts);
-- Фазы печати (05.09.2026, план допечатки со слоя W4): переходы печать → пауза → стоп с
-- последним слоем и временем. Пишет printwatch на смене фазы; читает «Допечатка со слоя»,
-- чтобы подставить слой, на котором мастер нажал паузу, вместо ввода руками.
CREATE TABLE IF NOT EXISTS print_phases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id INTEGER,
  ts INTEGER NOT NULL,
  phase TEXT NOT NULL,                       -- printing / paused / stopped / idle / …
  layer INTEGER,
  z REAL,
  job_name TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_print_phases_ts ON print_phases(ts);
-- ── Калибровка филамента ───────────────────────────────────────────────────────────────────
-- Правила и реестр тестов живут в app/calibration.py (без БД), здесь — накопленный опыт.
-- Кампания (calib_runs) = один повод: смена сопла, новая катушка, новый филамент. Внутри —
-- шаги в обязательном порядке (температура → поток → PA → откат → расход → VFA): каждый
-- следующий тест опирается на результат предыдущего, поэтому шаги нумерованы и не переставляются.
CREATE TABLE IF NOT EXISTS calib_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  printer_id INTEGER,
  nozzle TEXT NOT NULL DEFAULT '0.4',
  plastic_id INTEGER,
  plastic_name TEXT NOT NULL DEFAULT '',
  spool_id INTEGER,
  status TEXT NOT NULL DEFAULT 'active',       -- active | done | archived
  note TEXT,
  started_at INTEGER NOT NULL,
  finished_at INTEGER
);
-- Шаг кампании. journal_id — печать теста из журнала (привязывается АВТОМАТИЧЕСКИ по имени
-- задания принтера, см. calibration.detect_test), поэтому фото и таймлапс шага бесплатны.
-- applied=1 — значение записано в пресет слайсера (кнопкой из админки).
CREATE TABLE IF NOT EXISTS calib_steps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  test_key TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'todo',         -- todo | printing | measured | done | skipped
  -- value — ОСНОВНОЙ параметр теста (по нему считается дрейф и строится PDF). Тесты с парой
  -- параметров (глажка: поток + скорость) держат все значения в values_json {ключ: число},
  -- а value остаётся зеркалом первого — чтобы старые запросы и графики не переписывать.
  value REAL,
  values_json TEXT NOT NULL DEFAULT '{}',
  unit TEXT NOT NULL DEFAULT '',
  range_from REAL,
  range_to REAL,
  verdict TEXT NOT NULL DEFAULT '',
  note TEXT,
  journal_id INTEGER,
  applied INTEGER NOT NULL DEFAULT 0,
  applied_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calib_steps_run ON calib_steps(run_id, sort);
-- Замер штангенциркулем + вердикт правила (calibration.verdict). data — входные числа замера,
-- verdict/detail/rule — вывод правила: почему выбрано именно это значение, а не «помню, что так».
CREATE TABLE IF NOT EXISTS calib_measure(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  step_id INTEGER,
  kind TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}',
  value REAL,
  unit TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  rule TEXT NOT NULL DEFAULT '',
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calib_measure_step ON calib_measure(step_id, id);
-- Полка образцов: физическая башня/куб на полке ↔ карточка с фото и тем, что она доказывает.
-- code печатается на этикетке с QR (см. /api/admin/calib/labels).
CREATE TABLE IF NOT EXISTS calib_samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  step_id INTEGER,
  journal_id INTEGER,
  code TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  proves TEXT NOT NULL DEFAULT '',
  value TEXT NOT NULL DEFAULT '',
  place TEXT NOT NULL DEFAULT '',
  photo TEXT,
  note TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calib_samples_run ON calib_samples(run_id, id);
-- Паспорт филамента = проекция трёх пресетов слайсера в карточку (пластик × принтер × сопло).
-- Пресеты остаются рабочим форматом слайсера, паспорт — тем, что лежит в БД, попадает в бэкап
-- и переживает обновление слайсера (которое возвращает пользовательские пресеты к стоку).
CREATE TABLE IF NOT EXISTS calib_passport(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plastic_id INTEGER NOT NULL,
  printer_id INTEGER,
  nozzle TEXT NOT NULL DEFAULT '0.4',
  values_json TEXT NOT NULL DEFAULT '{}',
  source_json TEXT NOT NULL DEFAULT '{}',
  presets_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'calibrated',   -- calibrated | stock | recheck
  run_id INTEGER,
  spool_id INTEGER,
  note TEXT,
  calibrated_at INTEGER,
  updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calib_passport_key
  ON calib_passport(plastic_id, printer_id, nozzle);
-- Пластины (столы) принтера и Z-offset под каждую (OpenTodo 1028ab67). Z-offset НЕ живёт в
-- пресетах слайсера: он хранится В ПРИНТЕРЕ (в слайсере всегда 0), поэтому единственное место,
-- где его вообще можно записать и не потерять, — эта таблица. У каждой пластины своё
-- «прилипание»: текстурированная PEI, гладкая, холодная — у всех разный вылет первого слоя.
CREATE TABLE IF NOT EXISTS calib_plates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id INTEGER,
  name TEXT NOT NULL,                          -- имя пластины словами мастера («Текстурированная PEI»)
  kind TEXT NOT NULL DEFAULT '',               -- textured | smooth | cool | high | other
  z_offset REAL,                               -- мм, отрицательный = ближе к столу
  note TEXT NOT NULL DEFAULT '',
  is_default INTEGER NOT NULL DEFAULT 0,       -- пластина, которая лежит на столе сейчас
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calib_plate_key ON calib_plates(printer_id, name);
-- Снимки пользовательских пресетов слайсера. Пишем ТОЛЬКО при смене sha, поэтому таблица —
-- это история правок: когда MVS стал 13, когда юбка стала 1 петля. gone=1 — файл исчез
-- (обновление слайсера сбросило пресет): снимок остаётся, восстановить можно одной кнопкой.
CREATE TABLE IF NOT EXISTS calib_presets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL,
  sha TEXT NOT NULL,
  body TEXT NOT NULL,
  mtime INTEGER NOT NULL DEFAULT 0,
  gone INTEGER NOT NULL DEFAULT 0,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calib_presets_path ON calib_presets(path, id);
-- Очередь разбора (OpenTodo 26196ac1): модель не загрузилась/не посчиталась — клиент отправляет
-- ссылку мастеру, тот разбирает руками (готовит модель или чинит код) и отвечает клиенту.
CREATE TABLE IF NOT EXISTS calc_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  user_name TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',
  note TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
-- Переписка мастер↔клиент. request_id NULL = сообщение вне заказа (общий чат клиента):
-- настольная админка показывает сплошную ленту по клиенту, а не только треды заказов.
-- user_id заполнен всегда — по нему собирается общий чат и считается непрочитанное.
CREATE TABLE IF NOT EXISTS order_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER,
  user_id INTEGER,
  from_admin INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  image_path TEXT,
  created_at INTEGER NOT NULL,
  read_user INTEGER NOT NULL DEFAULT 0,
  read_admin INTEGER NOT NULL DEFAULT 0,
  reply_to INTEGER          -- id сообщения этой же переписки, на которое отвечают
);
CREATE INDEX IF NOT EXISTS idx_omsg_req ON order_messages(request_id, id);
-- Какое сообщение переписки уехало каким сообщением в Telegram (2026-08-13).
-- Нужна ДВУМ вещам, которым разбора текста не хватает:
--   • реакция (❤️ на сообщение бота) приходит апдейтом message_reaction, где есть ТОЛЬКО
--     chat_id и message_id — ни текста, ни номера заказа в нём нет;
--   • ответ реплаем становится настоящим ответом (order_messages.reply_to), а не просто
--     сообщением в тред.
-- Строк — по одной на получателя: одно сообщение клиента уходит каждому сотруднику своим
-- message_id, и реакция любого из них должна находить ту же строку переписки.
CREATE TABLE IF NOT EXISTS tg_chat_msgs(
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  msg_id INTEGER NOT NULL,          -- order_messages.id
  created_at INTEGER NOT NULL,
  PRIMARY KEY(chat_id, message_id)
);
-- idx_omsg_user создаётся в миграции, а НЕ здесь: на старой базе таблица уже существует
-- (CREATE TABLE IF NOT EXISTS ничего не делает), колонки user_id ещё нет, и весь
-- executescript со схемой упал бы — бот не поднялся бы вообще.
CREATE TABLE IF NOT EXISTS ideas(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  user_name TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE,
  label TEXT NOT NULL,
  trigger_type TEXT NOT NULL DEFAULT 'hours',
  threshold REAL NOT NULL,
  hours_since REAL NOT NULL DEFAULT 0,
  prints_since INTEGER NOT NULL DEFAULT 0,
  last_service_at INTEGER NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  notified INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  item_label TEXT NOT NULL,
  at INTEGER NOT NULL,
  hours_at REAL NOT NULL,
  prints_at INTEGER NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_servlog_item ON service_log(item_id, id DESC);
CREATE TABLE IF NOT EXISTS activity_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  user_name TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log(id DESC);
-- Объединение счетов «семьи» (OpenTodo): журнал слияний для аудита. УСТАРЕЛО (инцидент
-- 2026-07-16: слияние необратимо уничтожало историю второго клиента) — заменено семейным
-- счётом ниже. Таблица остаётся только как исторический след, код её больше не пишет.
CREATE TABLE IF NOT EXISTS user_merges(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  primary_user_id INTEGER NOT NULL,
  merged_user_id INTEGER NOT NULL,
  merged_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merges_primary ON user_merges(primary_user_id);
CREATE INDEX IF NOT EXISTS idx_merges_merged ON user_merges(merged_user_id);

-- Семейный счёт (редизайн 2026-07-16): ОБЩИЙ только баланс. Участники равноправны (без
-- «основного/второстепенного»), количество не ограничено двумя. Заказы, модели, скидки,
-- аватарки и достижения у каждого остаются СВОИ. get_balance/adjust_balance прозрачно
-- работают с общим счётом, если пользователь состоит в семье.
CREATE TABLE IF NOT EXISTS family_accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '',
  balance REAL NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
-- user_id PRIMARY KEY: один человек состоит максимум в одной семье.
CREATE TABLE IF NOT EXISTS family_members(
  user_id INTEGER PRIMARY KEY,
  family_id INTEGER NOT NULL,
  user_name TEXT NOT NULL DEFAULT '',
  joined_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_family_members_fam ON family_members(family_id);
-- Журнал общего счёта: user_id/user_name — чьё действие (NULL = мастер/система),
-- request_id — номер заказа при оплате заказа (клиенты видят «чей заказ и какой номер»).
CREATE TABLE IF NOT EXISTS family_tx(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family_id INTEGER NOT NULL,
  user_id INTEGER,
  user_name TEXT NOT NULL DEFAULT '',
  request_id INTEGER,
  amount REAL NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  comment TEXT,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_family_tx_fam ON family_tx(family_id, id DESC);

-- Центр уведомлений админа (2026-07-16): заявки клиентов с кнопками действий (сейчас —
-- семейный счёт) и черновики release notes от Клода (текст + картинка; опубликовать/
-- отредактировать/удалить). status: new | done | rejected | published.
-- payload — JSON под конкретный kind (family_request: {from_id, from_name, partner}).
CREATE TABLE IF NOT EXISTS admin_notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  image_path TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'new',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_notif ON admin_notifications(status, id DESC);

-- CRM (crm-roadmap.md, OpenTodo a8b591cf), вариант C: тонкая карточка клиента — ТОЛЬКО ручные
-- поля (имя-override, телефон, заметка, отписка от рассылок). LTV и счётчики всегда считаются
-- из requests на лету (customer_card/list_customers_crm) — рассинхрон с дашбордом невозможен.
-- Строка создаётся лениво при первом ручном поле/рассылке.
CREATE TABLE IF NOT EXISTS customers(
  user_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  no_broadcast INTEGER NOT NULL DEFAULT 0,
  last_broadcast_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

-- CRM этап 4: задачи-напоминания мастера («перезвонить», «спросить отзыв», «предложить докуп»).
-- Привязка к клиенту и/или заказу опциональна. notified — антидубль напоминания (crmtasks.loop).
CREATE TABLE IF NOT EXISTS crm_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  request_id INTEGER,
  title TEXT NOT NULL,
  due_at INTEGER NOT NULL DEFAULT 0,
  done INTEGER NOT NULL DEFAULT 0,
  notified INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crmtasks_open ON crm_tasks(done, due_at);

-- Кэш результата слайса каталожной модели (OpenTodo ae038968): грамм/время НЕ зависят от
-- пластика — только от геометрии модели + принтера + профиля печати (качество/прочность) +
-- масштаба. Следующий расчёт ТОЙ ЖЕ модели теми же принтером/качеством/прочностью/масштабом
-- получает цену МГНОВЕННО под ЛЮБОЙ пластик/тираж/скидку (см. _api_calc_inner, _cache_snapshot
-- в webapp.py), а честный слайс досчитывает точные цифры в фоне (_refine_calc). Мультиплейт с
-- выбором столов/своими пластиками на столах не кэшируется (см. _remember_calc_cache) — там
-- объём частичный/смешанный и формула «пластик не важен» не работает.
CREATE TABLE IF NOT EXISTS calc_cache(
  model_hash TEXT NOT NULL,
  printer_id INTEGER NOT NULL,
  quality_id INTEGER NOT NULL,
  strength_id INTEGER NOT NULL,
  scale_pct INTEGER NOT NULL DEFAULT 100,
  volume_cm3 REAL NOT NULL,
  minutes REAL NOT NULL,
  plates INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(model_hash, printer_id, quality_id, strength_id, scale_pct)
);
-- Готовые изделия на продажу (OpenTodo bd896aa6): витрина товаров с фикс-ценой и наличием.
-- Покупка не требует загрузки модели/слайса/расчёта — заказ создаётся сразу по цене товара.
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  price REAL NOT NULL DEFAULT 0,
  stock INTEGER NOT NULL DEFAULT 0,        -- кол-во в наличии; 0 — распродано (карточка «нет в наличии»)
  photo_path TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1,    -- 0 — скрыт из витрины (черновик/снят с продажи)
  sold INTEGER NOT NULL DEFAULT 0,         -- всего продано (аналитика)
  sort INTEGER NOT NULL DEFAULT 0,         -- ручной порядок в витрине (меньше — выше)
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
-- Команда мастерской (A4, роли и права). До этого доступ был плоским: ADMIN_IDS из .env,
-- все админы всемогущи. Здесь появляется вторая роль — оператор (заказы + принтер, БЕЗ цен,
-- денег, клиентской базы и настроек), чтобы наёмного сотрудника можно было пустить в админку,
-- не открывая ему ценообразование и выручку.
-- ВАЖНО: ADMIN_IDS остаются КОРНЕВЫМИ владельцами и в этой таблице не живут — их роль нельзя
-- понизить и нельзя удалить из приложения (иначе владелец запирает сам себя, и вернуть доступ
-- можно будет только правкой .env). Строки здесь — только приглашённые сотрудники.
CREATE TABLE IF NOT EXISTS staff(
  user_id INTEGER PRIMARY KEY,
  role TEXT NOT NULL DEFAULT 'operator',   -- owner | operator (см. app/roles.py)
  name TEXT NOT NULL DEFAULT '',           -- как показывать в списке команды
  note TEXT NOT NULL DEFAULT '',
  added_by INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL DEFAULT 0
);
-- Личный вход в настольную админку (app/desklogin.py). До него вход был один на машину:
-- .env-токен резолвился в первого владельца, и роли выше в деске ничего не значили.
-- Хранится sha256 токена, а не сам токен: утечка файла базы не должна открывать админку.
CREATE TABLE IF NOT EXISTS desk_sessions(
  token TEXT PRIMARY KEY,                  -- sha256(токен), сам токен живёт только у клиента
  user_id INTEGER NOT NULL,
  device TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_desk_sessions_user ON desk_sessions(user_id);
-- Одноразовые коды входа: бот выдаёт их сотруднику в личку, обменять на сессию можно только
-- запросом с этой машины. Живут минуты и удаляются в момент обмена.
CREATE TABLE IF NOT EXISTS desk_login_codes(
  code TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
-- Типы доставки (A2): строка прайса, из которой клиент выбирает на оформлении. Изначально
-- назывались «зонами» (31.07), но мастерские заводят сюда не только географию («по городу»,
-- «по области»), а любой способ получения — «курьер», «до пункта выдачи», «почтой», —
-- поэтому переименовано в тип доставки; таблица переехала миграцией ниже.
-- price — стоимость доставки, free_from — сумма заказа, с которой доставка бесплатна
-- (0 = порога нет), eta — человеческий срок («1–2 дня»). Стоимость считает СЕРВЕР по
-- выбранному типу (клиенту верить нельзя), снимок цены и названия ложится в шапку группы.
CREATE TABLE IF NOT EXISTS delivery_types(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  price REAL NOT NULL DEFAULT 0,
  free_from REAL NOT NULL DEFAULT 0,
  eta TEXT NOT NULL DEFAULT '',
  comment TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
-- Gridfinity-конфигуратор (design_handoff_gridfinity): черновик раскладки клиента —
-- один на юзера, JSON {v, base, modules, seq} в формате движка. Заменяет localStorage
-- прототипа: черновик доступен с любого устройства клиента.
CREATE TABLE IF NOT EXISTS gridfinity_configs(
  user_id INTEGER PRIMARY KEY,
  config TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
-- Снимок конфигурации на момент оформления заявки: черновик клиент продолжит менять,
-- а мастеру нужна ИМЕННО заказанная раскладка (request_id = requests.id заявки).
CREATE TABLE IF NOT EXISTS gridfinity_order_configs(
  request_id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  config TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
-- «Мастерская органайзеров»: ПРОЕКТ = физический ящик клиента, который живёт неделями и
-- переделывается. Раскладка — тот же JSON движка, что и в черновике выше (потолок 256 КБ
-- держит ручка). archived_at > 0 — в архиве: проект, из которого что-то заказывали,
-- удалять нельзя, иначе заявка ссылается в пустоту.
CREATE TABLE IF NOT EXISTS gridfinity_projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  config TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  archived_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gfproj_user ON gridfinity_projects(user_id, updated_at DESC);
-- ДЕТАЛЬ спецификации: проекция модуля раскладки (mod_id — id модуля в JSON) плюс то,
-- чего в геометрии нет. Правда по геометрии — ТОЛЬКО раскладка проекта; spec_json —
-- замороженный снимок на момент заказа (печатали именно его). mod_id = 0 — деталь снята
-- с раскладки (клиент удалил уже заказанный модуль): в сцене её нет, в истории есть.
-- sig — подпись геометрии: расходится с раскладкой => модуль правили после заказа.
-- color_idx В ПОДПИСЬ НЕ ВХОДИТ: перекраска не форкает напечатанную деталь в v2.
CREATE TABLE IF NOT EXISTS gridfinity_parts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  mod_id INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'draft',
  sel INTEGER NOT NULL DEFAULT 0,
  request_id INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  parent_id INTEGER NOT NULL DEFAULT 0,
  color_idx INTEGER NOT NULL DEFAULT 0,
  sig TEXT NOT NULL DEFAULT '',
  vol_cm3 REAL NOT NULL DEFAULT 0,
  spec_json TEXT NOT NULL DEFAULT '',
  changed_at INTEGER NOT NULL DEFAULT 0,
  printed_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfpart_proj ON gridfinity_parts(project_id, id);
CREATE INDEX IF NOT EXISTS idx_gfpart_req ON gridfinity_parts(request_id);
-- Сохранённые замеры мебели: новый проект стартует без рулетки.
CREATE TABLE IF NOT EXISTS gridfinity_measures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  btype TEXT NOT NULL DEFAULT 'drawer',
  w REAL NOT NULL DEFAULT 0,
  d REAL NOT NULL DEFAULT 0,
  h REAL NOT NULL DEFAULT 0,
  series TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfmeas_user ON gridfinity_measures(user_id, id DESC);
-- Снимки раскладки для отката (храним последние 10 на проект).
CREATE TABLE IF NOT EXISTS gridfinity_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  config TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfver_proj ON gridfinity_versions(project_id, id DESC);
-- Свои заготовки модулей: перенос удачной детали в другой проект.
CREATE TABLE IF NOT EXISTS gridfinity_presets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  mod_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfpreset_user ON gridfinity_presets(user_id, id DESC);
-- Ссылка «показать» — чтение проекта по токену без авторизации (жене, мастеру, соседу).
CREATE TABLE IF NOT EXISTS gridfinity_shares(
  token TEXT PRIMARY KEY,
  project_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfshare_proj ON gridfinity_shares(project_id);
-- Сессия «продолжить на компьютере»: ссылка даёт ПРАВКУ одного проекта в обычном браузере
-- без Telegram. Таблица отдельная от gridfinity_shares намеренно: там ссылка «показать»,
-- то есть чтение, и подмешать к ней право писать значило бы однажды раздать правку тем,
-- кому дали посмотреть. Живёт по сроку (expires_at) и снимается кнопкой.
CREATE TABLE IF NOT EXISTS gridfinity_desk(
  token TEXT PRIMARY KEY,
  project_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gfdesk_proj ON gridfinity_desk(project_id);
-- Очередь досчёта цены (Э0, docs/Перспективные разработки/vps-relay-2026-10-04.md §1):
-- заказ, цену которого ещё никто не посчитал, переживает рестарт. До неё связь «заказ ↔
-- досчёт» жила в памяти токена мини-аппы, и рестарт оставлял заявку с ценой 0 навсегда.
-- source/params — JSON (источник модели и выбор клиента ИДЕНТИФИКАТОРАМИ, не именами);
-- client_req_id — ключ идемпотентности приёма ('now:<rid>', 'rly:<event_id>'); claim_token —
-- кто именно держит задание в работе. Логика — app/store/deferred.py, сборщик — app/deferred.py.
CREATE TABLE IF NOT EXISTS deferred_calc(
  id TEXT PRIMARY KEY,
  rid INTEGER NOT NULL,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  params TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  claim_token TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try_at REAL NOT NULL DEFAULT 0,
  error TEXT,
  client_req_id TEXT UNIQUE,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deferred_due ON deferred_calc(status, next_try_at);
CREATE INDEX IF NOT EXISTS idx_deferred_rid ON deferred_calc(rid);
-- Дедуп входящих приёмной (Э1): апдейт Telegram и событие приёмной могут прийти повторно
-- (ack потерялся), а обработать их надо ровно раз — «дубль хуже пропуска».
CREATE TABLE IF NOT EXISTS tg_seen(
  update_id INTEGER PRIMARY KEY,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_events_seen(
  event_id TEXT PRIMARY KEY,
  at REAL NOT NULL
);
-- Катушки CC2 Control (app/cc2spools.py): события их журнала, уже перенесённые в stock_ledger.
-- Номеров у событий нет, поэтому ключ — отпечаток полей события. Строка 'baseline|<адрес>' —
-- метка «прошлое этого принтера уже сидит в остатках» (первая сверка ничего не переносит).
CREATE TABLE IF NOT EXISTS cc2_spool_seen(
  key TEXT PRIMARY KEY,
  at INTEGER NOT NULL
);
"""


async def _ensure_column(table: str, column: str, decl: str) -> None:
    cur = await _conn().execute(f"PRAGMA table_info({table})")
    cols = {r["name"] for r in await cur.fetchall()}
    if column not in cols:
        await _conn().execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


async def _open(path: str | Path) -> aiosqlite.Connection:
    """Соединение с файлом БД: фабрика строк + PRAGMA. Общая часть init() и connect_existing(),
    чтобы два способа подключиться к базе не разъехались настройками."""
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    # WAL: читатели (Mini App) не блокируются писателем; synchronous=NORMAL — быстрее запись
    # при сохранной долговечности в режиме WAL. journal_mode хранится в файле БД (идемпотентно).
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    # Удалённое — нулями: страницы базы раз в минуту уезжают копией на VPS (app/replica.py),
    # и стёртые строки (коды входа, токены) не должны жить в них «свободным местом».
    await conn.execute("PRAGMA secure_delete=ON")
    return conn


async def connect_existing(path: str | Path) -> None:
    """Подключиться к УЖЕ готовой базе: то же соединение, что у init(), но без схемы, миграций
    и сидов. Нужен там, где файл базы получен копией уже проинициализированного (тестовые
    фикстуры): init() на каждый тест — это весь _SCHEMA, все миграции и все сиды заново."""
    core.set_db(await _open(path))


async def init() -> None:
    # `_db` дальше по телу — ЛОКАЛЬНАЯ ссылка на то же соединение (волна 0: сотня прямых
    # обращений init-а осталась дословной); всему остальному слою его раздаёт core.
    _db = await _open(config.DB_PATH)
    core.set_db(_db)
    await _db.executescript(_SCHEMA)

    # миграции старых БД
    await _ensure_column("plastics", "material_type", "TEXT NOT NULL DEFAULT 'PLA'")
    await _ensure_column("plastics", "filament_profile", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("plastics", "photo_path", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("plastics", "description", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("plastics", "uses", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("plastics", "colors", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("orders", "printer", "TEXT")
    await _ensure_column("collection", "palette", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("collection", "dims", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("collection", "size", "REAL NOT NULL DEFAULT 0")
    await _ensure_column("collection", "last_material", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("collection", "last_calc_at", "INTEGER NOT NULL DEFAULT 0")
    # цена «от N» на плитках коллекции — кэш последнего расчёта (ux-simplification.md, идея 3)
    await _ensure_column("collection", "last_price", "REAL NOT NULL DEFAULT 0")
    # Каталог «Модели» с категориями (c9c42789): category='' — вне каталога (авто-записи из
    # расчётов юзеров); «Напечатанное» — старые записи и печати, отмеченные админом; остальные
    # категории — темы из группы ВК (создаёт импорт tools/vk_board_import.py). Бэкфилл старых
    # записей — ниже, в блоке версионных миграций (после миграции рейтинга).
    await _ensure_column("collection", "category", "TEXT NOT NULL DEFAULT ''")
    # ИИ-теггер каталога (e0dedc27): красивое название пишется в label, исходное (ВК-мусор)
    # сохраняется в orig_label, теги — через запятую (ищутся поиском вместе с названием).
    await _ensure_column("collection", "tags", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("collection", "orig_label", "TEXT NOT NULL DEFAULT ''")
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS collection_categories("
        "name TEXT PRIMARY KEY, sort INTEGER NOT NULL DEFAULT 100, created_at INTEGER NOT NULL)")
    await _db.execute(
        "INSERT INTO collection_categories(name, sort, created_at) VALUES('Напечатанное', 0, ?) "
        "ON CONFLICT(name) DO NOTHING", (int(time.time()),))
    # usage-агрегат обращений к эндпоинтам (аудит 07.2026, реком. 6): суточный счётчик по
    # (день, нормализованный путь, метод, статус) — устойчивая замена текстовым access-логам,
    # которые ротируются за ~40 ч и теряют историю. Пишет _usage_mw через bump_endpoint_hit.
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS endpoint_hits("
        "day TEXT NOT NULL, endpoint TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'GET', "
        "status INTEGER NOT NULL DEFAULT 200, hits INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY(day, endpoint, method, status))")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_endpoint_hits_day ON endpoint_hits(day)")
    # Телеметрия ошибок (OpenTodo f4de8627): одинаковые сбои схлопываются по fingerprint,
    # пока владелец не пометил их прочитанными (seen=0). После «прочитано» тот же сбой
    # заведёт НОВУЮ строку — иначе повтор после разбора выглядел бы как старая запись.
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS error_log("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, source TEXT NOT NULL, "
        "level TEXT NOT NULL, message TEXT NOT NULL, traceback TEXT, context TEXT, "
        "count INTEGER NOT NULL DEFAULT 1, first_at TEXT NOT NULL, last_at TEXT NOT NULL, "
        "seen INTEGER NOT NULL DEFAULT 0)")
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_error_log_fp ON error_log(fingerprint, seen)")
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_error_log_seen ON error_log(seen, id DESC)")
    # Журнал резервных копий (OpenTodo b557e341): по строке на КАЖДУЮ попытку снимка, включая
    # неудачные — без него провал бэкапа виден только в bot.log, который ротируется за сутки.
    # Пишет app/backup.py, читает админка (/api/admin/backup_status). Прунится до 200 строк.
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS backup_runs("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 0, "
        "path TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0, "
        "duration_ms INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')")
    # База знаний (OpenTodo c356387a): статьи-инструкции ДЛЯ ВЛАДЕЛЬЦА СТУДИИ и его операторов
    # — как настроить, откалибровать, что делать при типовых проблемах. Живёт только в админке;
    # клиенту мастерской не показывается. Черновик (published=0) виден лишь автору статьи.
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS kb_articles("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
        "body TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0, "
        "published INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT)")
    # Снимки смет, которые ВИДЕЛ КЛИЕНТ (26.08): лента событий хранила только строку текста,
    # и «рассчитал цену» открыть было нечем — сама смета живёт лишь в памяти процесса (_models).
    # Здесь лежит то, что ушло клиенту на экран: цифры, разбивка (payload JSON) и пометка, каким
    # путём цена получена. Строки чистятся своей обрезкой (store/crm.add_calc_snapshot) — ссылка
    # идёт от activity_log.calc_id, поэтому лента может пережить свой снимок и наоборот.
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS calc_log("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, created_at INTEGER NOT NULL, "
        "action TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
        "model_label TEXT NOT NULL DEFAULT '', printer TEXT NOT NULL DEFAULT '', "
        "plastic_name TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '', "
        "quality TEXT NOT NULL DEFAULT '', strength TEXT NOT NULL DEFAULT '', "
        "qty INTEGER NOT NULL DEFAULT 1, grams_total REAL NOT NULL DEFAULT 0, "
        "minutes_total REAL NOT NULL DEFAULT 0, total REAL NOT NULL DEFAULT 0, "
        "total_before REAL NOT NULL DEFAULT 0, discount_pct REAL NOT NULL DEFAULT 0, "
        "approx INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0, "
        "refined_total REAL, refined_grams REAL, refined_minutes REAL, refined_at INTEGER, "
        "preview_path TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '')")
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_calc_log_user ON calc_log(user_id, id DESC)")
    # Ссылка события ленты на снимок сметы. NULL — снимка нет (событие не про цену, либо строка
    # старше самой фичи, либо снимок уже вытеснен обрезкой): фронт в этом случае ведёт себя
    # по-старому и открывает карточку клиента.
    await _ensure_column("activity_log", "calc_id", "INTEGER")
    await _ensure_column("requests", "price_comment", "TEXT")
    await _ensure_column("requests", "dims", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("collection", "multicolor", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("collection", "plates", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column("saved_models", "multicolor", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("saved_models", "plates", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column("requests", "multicolor", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("requests", "plates", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column("plastics", "is_default", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("quality_presets", "is_default", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("strength_presets", "is_default", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("requests", "hash", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("plastics", "stock_grams", "REAL NOT NULL DEFAULT 0")
    await _ensure_column("plastics", "low_threshold", "REAL NOT NULL DEFAULT 0")
    await _ensure_column("plastics", "failure_rate_pct", "REAL")  # NULL = общий % брака из settings
    await _ensure_column("plastics", "price_factor", "REAL NOT NULL DEFAULT 1")
    # Категории каталога пластиков (3af25fc2): «Матовый декор / Обычный / Инженерный / Шелковый».
    # Бэкфилл по типу/названию для строк без категории (и для добавленных мимо админки).
    await _ensure_column("plastics", "category", "TEXT NOT NULL DEFAULT ''")
    cur = await _db.execute("SELECT id, name, material_type FROM plastics WHERE category = ''")
    _no_cat = await cur.fetchall()
    for r in _no_cat:
        await _db.execute("UPDATE plastics SET category = ? WHERE id = ?",
                          (materials.category_hint(r["material_type"], r["name"]), r["id"]))
    if _no_cat:
        await _db.commit()
    await _ensure_column("balance_tx", "comment", "TEXT")  # заметка мастера к транзакции (ccb1e4b6)
    # Любимый цвет клиента (ux-paths-next.md, идея 4): последний выбранный цвет — подставляем
    # первым в палитре, чтобы повтор был в один тап.
    await _ensure_column("customers", "last_color", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("requests", "order_group", "TEXT")
    # Индекс по группе корзины (B5): свёртка позиций одного заказа в клиентском и мастерском
    # списках ходит по order_group. Аддитивно, каждый boot.
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_reqgroup ON requests(order_group)")
    await _ensure_column("requests", "rating", "INTEGER")
    await _ensure_column("requests", "review", "TEXT")
    await _ensure_column("requests", "done_photo", "TEXT")
    await _ensure_column("requests", "src_url", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("requests", "file_path", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("requests", "promo", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("requests", "promo_ok", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("requests", "quality_id", "INTEGER")
    await _ensure_column("requests", "strength_id", "INTEGER")
    await _ensure_column("requests", "queue_pos", "INTEGER")
    await _ensure_column("requests", "is_custom", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("requests", "description", "TEXT")
    await _ensure_column("requests", "photos", "TEXT")
    await _ensure_column("requests", "breakdown", "TEXT")   # JSON-снимок расчётника цены
    await _ensure_column("requests", "due_date", "TEXT NOT NULL DEFAULT ''")  # срок исполнения «DD.MM»
    # Срочность заказа (B3): выбранная срочность ('' = обычная, 'rush' = срочная) + обещанный
    # клиенту срок «DD.MM», зафиксированный на момент оформления. urgency_factor/urgency_surcharge
    # едут внутри requests.breakdown JSON автоматически — эти колонки для отображения/выборок.
    await _ensure_column("requests", "urgency", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("requests", "promised_date", "TEXT NOT NULL DEFAULT ''")
    # Индивидуальный заказ: счёт-расчёт от мастера (OpenTodo 6aaa6a19)
    await _ensure_column("requests", "dev_fee", "REAL NOT NULL DEFAULT 0")     # плата за разработку модели
    await _ensure_column("requests", "slicer_shot", "TEXT")                    # скрин из слайсера к счёту
    await _ensure_column("requests", "timelapse", "TEXT")                      # таймлапс печати (OpenTodo 70b5a590)
    # Заказ за клиента руками админа: бот шлёт его клиенту на подтверждение (OpenTodo cd3258d6)
    await _ensure_column("requests", "created_by_admin_id", "INTEGER")         # id админа-оформителя
    # Заказ без сметы, если автослайс не удался (OpenTodo 55a1cf8f): клиент не упирается в тупик,
    # мастер видит модель и назначает цену вручную инструментом «Изменить цену».
    await _ensure_column("requests", "unsliced", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("requests", "product_id", "INTEGER")   # заказ готового изделия (bd896aa6)
    await _ensure_column("requests", "paid", "INTEGER NOT NULL DEFAULT 0")     # оплачен (7f257a33)
    # Онлайн-оплата (A1): когда и каким способом оплачен — старые «галочки» мастера остаются
    # с paid_at=0 и pay_method='' (не выдумываем им дату задним числом).
    await _ensure_column("requests", "paid_at", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("requests", "pay_method", "TEXT NOT NULL DEFAULT ''")
    # Рефералка: bonus_paid=1 — бонус реально ВЫПЛАЧЕН (в отличие от rewarded, который ставится
    # и при глушении внутри семьи). Такой паре бонус за открытие семейного счёта не начисляется
    # (2026-07-16). Существующие строки → 0: единственная rewarded-пара на момент миграции
    # выплату получила ошибочно, и она откачена — ей бонус за открытие положен.
    await _ensure_column("referrals", "bonus_paid", "INTEGER NOT NULL DEFAULT 0")
    # Лучший профиль печати у пластика (OpenTodo 94cc52c7): открытие модели сразу ставит его.
    await _ensure_column("plastics", "best_quality_id", "INTEGER")
    # CRM этап 6: причина отказа по заказу — для аналитики воронки (crm-roadmap.md G6)
    await _ensure_column("requests", "reject_reason", "TEXT")
    # Резерв филамента (OpenTodo a89586a2): заказ резервирует пластик (stock_plan), списание —
    # при «напечатано» (stock_deducted). СУЩЕСТВУЮЩИЕ заказы уже списали склад при оформлении —
    # помечаем их stock_deducted=1, чтобы не списать повторно.
    cur = await _db.execute("PRAGMA table_info(requests)")
    if "stock_deducted" not in {r["name"] for r in await cur.fetchall()}:
        await _db.execute("ALTER TABLE requests ADD COLUMN stock_deducted INTEGER NOT NULL DEFAULT 0")
        await _db.execute("UPDATE requests SET stock_deducted = 1")   # старые — уже списаны
    await _ensure_column("requests", "stock_plan", "TEXT")
    # LCD-дашборд (design_handoff_lcd): «непросмотренные» заказы — мастер ни разу не открывал
    # карточку. При добавлении колонки помечаем ВСЕ существующие заказы просмотренными, чтобы
    # на экране не вспыхнул ложный вал из всей истории.
    cur = await _db.execute("PRAGMA table_info(requests)")
    if "admin_seen_at" not in {r["name"] for r in await cur.fetchall()}:
        await _db.execute("ALTER TABLE requests ADD COLUMN admin_seen_at INTEGER")
        await _db.execute("UPDATE requests SET admin_seen_at = ?", (int(time.time()),))
    # ---- Версионные одноразовые миграции (PRAGMA user_version, шаги строго по порядку) ----
    cur = await _db.execute("PRAGMA user_version")
    _uv = (await cur.fetchone())[0]
    # v1 (bdcc18e4): оценки теперь 1–10 — старые 1–5 разово ×2, чтобы «5/5» осталась максимумом.
    if _uv < 1:
        await _db.execute("UPDATE requests SET rating = MIN(rating * 2, 10) "
                          "WHERE rating IS NOT NULL AND rating <= 5")
    # v2 (c9c42789): существующие записи коллекции — в категорию «Напечатанное».
    if _uv < 2:
        await _db.execute("UPDATE collection SET category = 'Напечатанное' WHERE category = ''")
        await _db.execute("PRAGMA user_version = 2")
    # Журнал печатей: брак ползунком 0–100% вместо бинарного ok/fail (OpenTodo 60e0b2a8).
    # Бэкфилл старых записей: result='fail' → 100% брака, 'ok' → 0%.
    await _ensure_column("print_journal", "fail_pct", "REAL")
    await _db.execute("UPDATE print_journal SET fail_pct = CASE WHEN result='fail' THEN 100 "
                      "ELSE 0 END WHERE fail_pct IS NULL")
    # Журнал: каждая печать заносится СРАЗУ (OpenTodo 5ac0ae9f), оценку мастер ставит позже —
    # graded=0 «не оценено». Старые записи считаем оценёнными. + привязка к заказу/фото/таймлапс
    # для полного пост-печатного опроса (OpenTodo 0f848906).
    await _ensure_column("print_journal", "graded", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column("print_journal", "request_id", "INTEGER")
    await _ensure_column("print_journal", "done_photo", "TEXT")
    await _ensure_column("print_journal", "timelapse", "TEXT")
    # Мастер удалил таймлапс печати (OpenTodo 1faab6cb): tl_deleted=1 запрещает «самолечению»
    # (_heal_journal_timelapse) и свипу printwatch заново привязать/скачать это видео. Явная
    # привязка нового таймлапса флаг снимает (см. set_print_journal_fields).
    await _ensure_column("print_journal", "tl_deleted", "INTEGER NOT NULL DEFAULT 0")
    # Печать вне заказа списывает свой пластик со склада ровно один раз (OpenTodo 05806f15):
    # stock_deducted — анти-повтор. Существующие записи считаем уже учтёнными (не списываем задним
    # числом). Печати, привязанные к заказу (request_id), склад НЕ трогают здесь — за них списывает
    # сам заказ при «напечатано» (apply_order_stock), иначе двойное списание. Бэкфилл — ТОЛЬКО при
    # первом добавлении столбца (иначе рестарт пометил бы свежие, ещё не списанные печати).
    cur = await _db.execute("PRAGMA table_info(print_journal)")
    if "stock_deducted" not in {r["name"] for r in await cur.fetchall()}:
        await _db.execute("ALTER TABLE print_journal ADD COLUMN stock_deducted INTEGER NOT NULL DEFAULT 0")
        await _db.execute("UPDATE print_journal SET stock_deducted = 1")   # старые — уже учтены

    # v3 (мастер-план учёта, Фаза A): катушки как надстройка. Разово (a) слить дубли цвета по
    # регистру/пробелам в plastic_color_stock (Черный/черный → один, Решение 5) и (b/c) обернуть
    # ТЕКУЩИЕ остатки в стартовые катушки (reason='backfill_initial'). Идемпотентно по user_version.
    if _uv < 3:
        now3 = int(time.time())
        cur = await _db.execute(
            "SELECT plastic_id, color, stock_grams, low_threshold FROM plastic_color_stock")
        merged: dict[tuple[int, str], dict] = {}
        for r in await cur.fetchall():
            disp = " ".join((r["color"] or "").split())      # вид как ввели, без двойных пробелов
            key = (r["plastic_id"], _canon_color(disp))
            m = merged.setdefault(key, {"display": disp, "stock": 0.0, "low": 0.0})
            m["stock"] += float(r["stock_grams"] or 0)
            m["low"] = max(m["low"], float(r["low_threshold"] or 0))
        await _db.execute("DELETE FROM plastic_color_stock")   # (a) слить дубли, сохранив первый вид цвета
        for (pid_, canon), m in merged.items():
            await _db.execute(
                "INSERT INTO plastic_color_stock(plastic_id, color, stock_grams, low_threshold) "
                "VALUES(?,?,?,?)", (pid_, m["display"], m["stock"], m["low"]))
        for (pid_, canon), m in merged.items():                          # (b) катушка на каждый цвет (canon)
            if m["stock"] <= 0:
                continue
            cur2 = await _db.execute(
                "INSERT INTO spools(plastic_id, color, net_weight_g, remaining_g, low_threshold, "
                "status, note, purchased_at, created_at) VALUES(?,?,?,?,?, 'active', ?, ?, ?)",
                (pid_, canon, m["stock"], m["stock"], m["low"],
                 "бэкфилл из plastic_color_stock", now3, now3))
            await _db.execute("INSERT INTO stock_ledger(spool_id, delta_g, reason, at) VALUES(?,?,?,?)",
                              (cur2.lastrowid, m["stock"], "backfill_initial", now3))
        cur = await _db.execute(                                         # (c) общий остаток без цветов
            "SELECT id, stock_grams, low_threshold FROM plastics WHERE stock_grams > 0 "
            "AND id NOT IN (SELECT DISTINCT plastic_id FROM plastic_color_stock WHERE stock_grams > 0)")
        for r in await cur.fetchall():
            g = float(r["stock_grams"] or 0)
            cur2 = await _db.execute(
                "INSERT INTO spools(plastic_id, color, net_weight_g, remaining_g, low_threshold, "
                "status, note, purchased_at, created_at) VALUES(?,'',?,?,?, 'active', ?, ?, ?)",
                (r["id"], g, g, float(r["low_threshold"] or 0),
                 "бэкфилл из plastics.stock_grams", now3, now3))
            await _db.execute("INSERT INTO stock_ledger(spool_id, delta_g, reason, at) VALUES(?,?,?,?)",
                              (cur2.lastrowid, g, "backfill_initial", now3))
        await _db.execute("PRAGMA user_version = 3")

    # v4 (семейный счёт, 2026-07-16): разовая правка данных одной конкретной базы — заявку
    # на семейный счёт пересоздали в центре уведомлений после откаченного слияния. Та база
    # давно на версии ≥ 4, а на любой другой правке не на что было опереться, поэтому от
    # шага остался только номер версии: порядок шагов — договор с боевыми базами.
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 4:
        await _db.execute("PRAGMA user_version = 4")

    # Чат мастер↔клиент вне заказа + вложения (настольная админка, 2026-07-19).
    # Колонки добавляем идемпотентно; снять NOT NULL с request_id в SQLite можно только
    # пересозданием таблицы — делаем это версионным шагом, ровно один раз.
    # День рождения клиента («ДД.ММ») — единственное поле карточки из макета настольной
    # админки, которого не было в БД. По нему заводится задача-напоминание за 3 дня.
    await _ensure_column("customers", "birthday", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("order_messages", "user_id", "INTEGER")
    await _ensure_column("order_messages", "image_path", "TEXT")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_omsg_user ON order_messages(user_id, id)")
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 5:
        cur = await _db.execute("PRAGMA table_info(order_messages)")
        cols = {r["name"]: r for r in await cur.fetchall()}
        if cols and cols.get("request_id") and cols["request_id"]["notnull"]:
            await _db.execute("""
                CREATE TABLE order_messages_new(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  request_id INTEGER,
                  user_id INTEGER,
                  from_admin INTEGER NOT NULL DEFAULT 0,
                  text TEXT NOT NULL,
                  image_path TEXT,
                  created_at INTEGER NOT NULL,
                  read_user INTEGER NOT NULL DEFAULT 0,
                  read_admin INTEGER NOT NULL DEFAULT 0
                )""")
            await _db.execute(
                "INSERT INTO order_messages_new(id, request_id, user_id, from_admin, text, "
                "image_path, created_at, read_user, read_admin) "
                "SELECT id, request_id, user_id, from_admin, text, image_path, created_at, "
                "read_user, read_admin FROM order_messages")
            await _db.execute("DROP TABLE order_messages")
            await _db.execute("ALTER TABLE order_messages_new RENAME TO order_messages")
            await _db.execute("CREATE INDEX IF NOT EXISTS idx_omsg_req ON order_messages(request_id, id)")
            await _db.execute("CREATE INDEX IF NOT EXISTS idx_omsg_user ON order_messages(user_id, id)")
        # Бэкфилл автора: у старых сообщений user_id пуст, а общий чат клиента строится по нему.
        await _db.execute(
            "UPDATE order_messages SET user_id = "
            "(SELECT r.user_id FROM requests r WHERE r.id = order_messages.request_id) "
            "WHERE user_id IS NULL AND request_id IS NOT NULL")
        await _db.execute("PRAGMA user_version = 5")

    # v6 (B5, мультифайловый заказ-корзина): у живых cart-заказов позиции уже несут общий
    # requests.order_group, но строки-сущности order_groups под них нет (появилась только сейчас).
    # Разовый бэкфилл заводит шапку по существующим группам. Идемпотентно (INSERT OR IGNORE +
    # PRAGMA-гейт): повторный прогон не дублирует. Итог — сумма цен позиций, доставка пуста
    # (в старых заказах её не было). Фильтр order_group IS NOT NULL AND <> '' обязателен:
    # NULL-группа (одиночные заказы) не должна схлопнуться в один фейковый мега-заказ.
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 6:
        await backfill_order_groups()
        await _db.execute("PRAGMA user_version = 6")

    # v7: снос общего чата КЛИЕНТОВ (c356387a). Фича прожила один день и была свёрнута:
    # сообщество делается для владельцев студий (Telegram-группа + база знаний в админке),
    # а не для заказчиков мастерской. Таблицы существовали меньше суток и были пусты;
    # оставлять мёртвую схему в продукте, который уезжает лицензиатам, ни к чему.
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 7:
        await _db.execute("DROP TABLE IF EXISTS community_messages")
        await _db.execute("DROP TABLE IF EXISTS community_state")
        # Версия шага прибита ЛИТЕРАЛОМ, а не SCHEMA_VERSION: иначе при появлении v8 этот шаг
        # проставит на старой базе сразу 8 и миграция v8 у лицензиата молча не выполнится.
        await _db.execute("PRAGMA user_version = 7")

    # A2 (доставка: денежная и трекинговая часть). До этого шапка группы знала только способ
    # получения и адрес — «информативно, без автосписания». Теперь:
    #   delivery_type_id/_name — выбранный тариф (имя — СНИМОК: переименование типа задним
    #     числом не должно менять уже оформленный заказ);
    #   delivery_cost — стоимость доставки, посчитанная СЕРВЕРОМ на момент оформления и
    #     входящая в итог заказа (итог группы = сумма позиций + доставка);
    #   delivery_paid — доставка уже оплачена онлайн (счёт якорной позиции, см. app/payments.py);
    #   track_* / shipped_at / delivered_at — отправление: перевозчик, номер, ссылка отслеживания.
    await _ensure_column("order_groups", "delivery_type_id", "INTEGER")
    await _ensure_column("order_groups", "delivery_type_name", "TEXT")
    await _ensure_column("order_groups", "delivery_cost", "REAL NOT NULL DEFAULT 0")
    await _ensure_column("order_groups", "delivery_paid", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("order_groups", "track_carrier", "TEXT")
    await _ensure_column("order_groups", "track_number", "TEXT")
    await _ensure_column("order_groups", "track_url", "TEXT")
    await _ensure_column("order_groups", "shipped_at", "INTEGER")
    await _ensure_column("order_groups", "delivered_at", "INTEGER")
    # ОТКУДА взялась стоимость доставки (11.08). Само число двусмысленно: ноль означает либо
    # «сумма перекрыла порог free_from», либо «мастер подарил доставку руками», и постфактум
    # это не различить ничем. Из-за этого любое изменение цены заказа молча отменяло подарок.
    # 'manual' — мастер задал цену явно (её автоматика трогать не смеет), '' / 'auto' — считал
    # сервер. Старые строки остаются с '' и ведут себя как раньше — по догадке (см.
    # delivery.threshold_recalcable). Место под будущее 'promo' здесь же.
    await _ensure_column("order_groups", "delivery_cost_source", "TEXT")
    # Доставка по умолчанию в профиле клиента (доработка 31.07)
    await _ensure_column("user_prefs", "delivery_type_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column("user_prefs", "delivery_address", "TEXT NOT NULL DEFAULT ''")

    # «Зона доставки» → «тип доставки» (переименование 31.07, в тот же день, что и сама A2).
    # Мастерские заводят сюда не только географию, но и способ получения («курьер», «до пункта
    # выдачи»), поэтому слово «зона» вводило в заблуждение. Переносим ДАННЫЕ, а не только имена:
    # миграция обязана быть корректной и на БД, где типы уже успели завести.
    # Идемпотентно: после переноса старая таблица/колонки исчезают, повторный прогон — no-op.
    cur = await _db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = 'delivery_zones'")
    if await cur.fetchone():
        await _db.execute(
            "INSERT OR IGNORE INTO delivery_types(id, name, price, free_from, eta, comment, "
            "is_active, sort, created_at) SELECT id, name, price, free_from, eta, comment, "
            "is_active, sort, created_at FROM delivery_zones")
        await _db.execute("DROP TABLE delivery_zones")
    cur = await _db.execute("PRAGMA table_info(order_groups)")
    _og_cols = {r["name"] for r in await cur.fetchall()}
    if "delivery_zone_id" in _og_cols:
        await _db.execute("UPDATE order_groups SET delivery_type_id = delivery_zone_id, "
                          "delivery_type_name = delivery_zone_name "
                          "WHERE delivery_type_id IS NULL AND delivery_zone_id IS NOT NULL")
        # DROP COLUMN есть с SQLite 3.35; если сборка старее — колонки просто останутся
        # мёртвым грузом, данные уже перенесены и код их не читает.
        for _old in ("delivery_zone_id", "delivery_zone_name"):
            try:
                await _db.execute(f"ALTER TABLE order_groups DROP COLUMN {_old}")
            except Exception:  # noqa: BLE001
                log.info("не удалось убрать старую колонку %s (не критично)", _old)

    # Фаза C мастер-плана: реальная себестоимость заказа по факту (из stock_ledger × цена катушки)
    await _ensure_column("requests", "cogs_actual", "REAL")
    # Самопечать мастера как «заказ админа» (status='self', ВНЕ клиентской выручки) — для суммы
    # «было бы потрачено» в профиле мастера (сколько стоила бы эта печать заказом).
    await _ensure_column("requests", "self_print", "INTEGER NOT NULL DEFAULT 0")
    # Догон брошенных расчётов (A5, abandonednudge): антидубль — расчёт (строка orders-лога),
    # по которому уже слали ОДНО напоминание. DEFAULT 0 → все легаси-строки считаются
    # не-нуджёнными, но окно max_days не даёт «догнать» их задним числом. Аналог user_cart.nudged_at.
    await _ensure_column("orders", "nudged_at", "INTEGER NOT NULL DEFAULT 0")
    # Опция постобработки «за деталь» (OpenTodo accee7a9): price = ставка за отдельный элемент
    # модели, итог = ставка × детали × тираж (кейс «склейка 20 коп/деталь»). Старые опции = 0 (за заказ).
    await _ensure_column("postproc_options", "per_part", "INTEGER NOT NULL DEFAULT 0")

    # B7 (источник лида, competitor-gap шаг 5): откуда пришла МОДЕЛЬ заказа — file (загрузка),
    # link (ссылка на площадку), catalog (коллекция/лента), mine (своя модель), repeat (повтор
    # заказа); '' — старые заказы и непомеченные пути (индивидуальный, товар витрины). Источник
    # ставится при регистрации токена модели и доезжает до заказа в orders.place_from_token;
    # дашборд строит по нему разбивку «откуда берутся заказы». НЕ путать с print_journal.source
    # (там source печати: order/self/test).
    await _ensure_column("requests", "source", "TEXT NOT NULL DEFAULT ''")

    # Фаза B (print_events lite, мастер-план): источник печати в журнале — order/self/test (вместо
    # отдельной таблицы print_events; полное объединение отложено планом до обкатки Пути A). Бэкфилл
    # при первом добавлении столбца: привязанные к заказу → 'order', остальные → 'self'.
    cur = await _db.execute("PRAGMA table_info(print_journal)")
    if "source" not in {r["name"] for r in await cur.fetchall()}:
        await _db.execute("ALTER TABLE print_journal ADD COLUMN source TEXT NOT NULL DEFAULT 'self'")
        await _db.execute("UPDATE print_journal SET source = "
                          "CASE WHEN request_id IS NOT NULL THEN 'order' ELSE 'self' END")
    # print_events (Фаза B): print_journal = единый журнал событий печати. Катушка печати + оценка
    # времени слайсера (пара к факту minutes, как в print_samples). self_prints вливаем сюда разово.
    await _ensure_column("print_journal", "spool_id", "INTEGER")
    await _ensure_column("print_journal", "est_minutes", "REAL")
    # print_events (Фаза B): происхождение цифры граммов — насколько ей верить (мастер-план §2.1):
    # telemetry | scale | slicer_estimate | estimate_missing. Старые записи → NULL (неизвестно).
    await _ensure_column("print_journal", "source_of_grams", "TEXT")
    _mrg = await (await _db.execute(
        "SELECT value FROM settings WHERE key='self_prints_merged'")).fetchone()
    if not _mrg:
        await _db.execute(
            "INSERT INTO print_journal(label, plastic_id, plastic_name, grams, minutes, result, "
            "fail_pct, graded, source, created_at) "
            "SELECT COALESCE(label,''), NULL, COALESCE(plastic_name,''), COALESCE(grams,0), "
            "COALESCE(minutes,0), 'ok', 0, 1, 'self', created_at FROM self_prints")
        await _db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('self_prints_merged','1')")

    # print_events (Фаза B, ПОЛНОЕ слияние — по решению владельца): print_journal становится ЕДИНОЙ
    # таблицей событий печати, в неё вливаем print_samples (замеры оценка/факт + признаки модели).
    # Богатые поля замера — столбцы журнала; predict_minutes/bigdata_stats читают отсюда. Дедуп по
    # request_id (заказная печать уже есть в журнале → ОБОГАЩАЕМ её, не задваиваем); замеры без
    # привязки → отдельные события source='test'. print_samples остаётся как бэкап (не читается).
    for _c, _d in (("material_type", "TEXT"), ("model_hash", "TEXT"), ("volume_cm3", "REAL"),
                   ("bbox_x", "REAL"), ("bbox_y", "REAL"), ("bbox_z", "REAL"),
                   ("bbox_volume", "REAL"), ("parts", "INTEGER"), ("layer_height", "REAL"),
                   ("infill_pct", "INTEGER"), ("walls", "INTEGER"), ("process_name", "TEXT"),
                   ("printer_id", "INTEGER"), ("extra_colors", "INTEGER")):
        await _ensure_column("print_journal", _c, _d)
    _sm = await (await _db.execute(
        "SELECT value FROM settings WHERE key='print_samples_merged'")).fetchone()
    if not _sm:
        _rich = ("material_type", "model_hash", "volume_cm3", "bbox_x", "bbox_y", "bbox_z",
                 "bbox_volume", "parts", "layer_height", "infill_pct", "walls", "process_name",
                 "printer_id", "extra_colors")
        for s in await (await _db.execute("SELECT * FROM print_samples")).fetchall():
            rid = s["request_id"]
            jrow = None
            if rid:
                jrow = await (await _db.execute(
                    "SELECT id, est_minutes, minutes FROM print_journal WHERE request_id=? "
                    "ORDER BY id LIMIT 1", (rid,))).fetchone()
            if jrow:                                  # обогащаем существующее событие (без дубля)
                sets = {k: (s[k] if k != "extra_colors" else (s["extra_colors"] or 0)) for k in _rich}
                if not (jrow["est_minutes"] or 0):
                    sets["est_minutes"] = s["est_minutes"]
                if not (jrow["minutes"] or 0):
                    sets["minutes"] = s["actual_minutes"]
                await _db.execute(
                    f"UPDATE print_journal SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                    (*sets.values(), jrow["id"]))
            else:                                     # замер без привязки → отдельное событие 'test'
                await _db.execute(
                    "INSERT INTO print_journal(label, plastic_id, plastic_name, grams, minutes, "
                    "result, fail_pct, graded, source, est_minutes, request_id, material_type, "
                    "model_hash, volume_cm3, bbox_x, bbox_y, bbox_z, bbox_volume, parts, "
                    "layer_height, infill_pct, walls, process_name, printer_id, extra_colors, "
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ((s["model_hash"] or "замер")[:120], None, "", float(s["grams"] or 0),
                     float(s["actual_minutes"] or 0), "ok", 0, 1, "test",
                     float(s["est_minutes"] or 0), rid, s["material_type"], s["model_hash"],
                     s["volume_cm3"], s["bbox_x"], s["bbox_y"], s["bbox_z"], s["bbox_volume"],
                     s["parts"], s["layer_height"], s["infill_pct"], s["walls"],
                     s["process_name"], s["printer_id"], s["extra_colors"] or 0, int(time.time())))
        await _db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('print_samples_merged','1')")

    # Калибровка: тип теста прямо в журнале печатей. Журнал ведётся автоматически именем задания
    # принтера, поэтому весь накопленный опыт («temperature_tower», «pa_pattern», «VFA»)
    # распознаётся ЗАДНИМ ЧИСЛОМ — бэкфилл один раз при появлении колонки, дальше проставляет
    # add_print_journal. Нераспознанные печати остаются с пустым calib_type.
    _had_calib = True
    cur = await _db.execute("PRAGMA table_info(print_journal)")
    if "calib_type" not in {r["name"] for r in await cur.fetchall()}:
        _had_calib = False
    # Тесты с парой параметров (глажка) появились позже — колонка добавляется миграцией.
    await _ensure_column("calib_steps", "values_json", "TEXT NOT NULL DEFAULT '{}'")
    await _ensure_column("print_journal", "calib_type", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("print_journal", "calib_run_id", "INTEGER")
    await _ensure_column("print_journal", "calib_step_id", "INTEGER")
    if not _had_calib:
        rows = await (await _db.execute("SELECT id, label FROM print_journal")).fetchall()
        for r in rows:
            k = calibration.detect_test(r["label"] or "")
            if k:
                await _db.execute("UPDATE print_journal SET calib_type=? WHERE id=?", (k, r["id"]))

    # Сопла (OpenTodo f71f4973): у пресета качества сопло становится ЯВНЫМ полем вместо
    # угадывания по имени процесс-файла. Пустое значение = «как раньше» (разберём именем),
    # поэтому бэкфилл ниже безопасен и для пресетов, заведённых руками в проде.
    await _ensure_column("quality_presets", "nozzle", "TEXT NOT NULL DEFAULT ''")
    # Пластина в паспорте (OpenTodo 1028ab67): на какой пластине калибровался этот филамент и
    # какой Z-offset у него получился. z_offset паспорта — уточнение поверх значения пластины
    # (у PETG на той же плите вылет обычно другой), пусто → берём значение пластины.
    await _ensure_column("calib_passport", "plate_id", "INTEGER")
    await _ensure_column("calib_passport", "z_offset", "REAL")
    # Настройки печати (OpenTodo 8e8d4873): ссылка записи журнала на снимок настроек задания.
    await _ensure_column("print_journal", "settings_id", "INTEGER")
    # Ключ наблюдения printwatch (04.10.2026): «эта печать этой машины». Запись финиша теперь
    # повторяется, пока не удастся, и повтор обязан узнать строку, которую уже вставила
    # прошлая попытка (INSERT прошёл, а commit упал — строка висит в общей транзакции и уедет
    # в БД с чужим commit-ом). Ручные записи журнала ключа не имеют — индекс частичный.
    await _ensure_column("print_journal", "watch_key", "TEXT")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_print_journal_watch_key "
                      "ON print_journal(watch_key) WHERE watch_key IS NOT NULL")
    # Как разговаривать с принтером (OpenTodo 0613014e): до этого таблица знала только профили
    # слайсера, а адрес и код доступа жили в .env — то есть второй принтер описать было нечем.
    # Пустой kind = «как было»: драйвер определяется по .env (printerdrv.autodetect_kind).
    await _ensure_column("printers", "kind", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("printers", "address", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("printers", "access_code", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("printers", "serial", "TEXT NOT NULL DEFAULT ''")
    # Чем резать задания ЭТОЙ машины (цикл 3): у Bambu свои профили, и резать их
    # ElegooSlicer'ом значит считать клиенту цену по чужой машине. Пусто = движок по
    # умолчанию, то есть прежнее поведение установки с одним принтером.
    await _ensure_column("printers", "slicer", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column("printers", "slicer_path", "TEXT NOT NULL DEFAULT ''")
    # Коэффициент времени машины (трек B, п.1 roadmap-2026-08-20; B1): fastcalc и константы
    # времени откалиброваны по одной паре «движок+машина» (ElegooSlicer + CC2). Для второго
    # принтера цифры считались бы молча неверно — множитель на карточке даёт мастеру ручку
    # в день приезда машины, пока журнал печатей по ней пуст; дальше predict_minutes
    # учится по журналу ЭТОЙ машины (оценка в журнале уже с множителем — без двойного счёта).
    await _ensure_column("printers", "time_factor", "REAL NOT NULL DEFAULT 1.0")
    # ЧЬЯ ЭТО ПЕЧАТЬ (цикл 3, волна 4). Аддитивно и заранее: разметить историю «всё это CC2»
    # можно ровно до первой печати на второй машине, после — нечем. print_journal.printer_id
    # завёлся ещё миграцией объединения с print_samples, но не писался НИ ОДНОЙ строкой: ни
    # INSERT, ни белый список правки его не знали, а бэкфилл тянул значение из таблицы, где оно
    # тоже никогда не заполнялось. Колонка выглядела готовой и была пуста.
    # requests.printer TEXT НЕ трогаем: это снимок имени на момент заказа, и переименование
    # карточки не должно переписывать историю. printer_id живёт рядом как ссылка.
    await _ensure_column("printer_telemetry", "printer_id", "INTEGER")
    await _ensure_column("requests", "printer_id", "INTEGER")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_tele_printer_ts "
                      "ON printer_telemetry(printer_id, ts)")
    # Разметка прошлой истории. ВНИМАНИЕ на порядок: строго ПОСЛЕ появления колонок и ДО сидов —
    # сид может завести вторую карточку, и тогда «вся история принадлежит первому» перестанет
    # быть правдой ещё до того, как мы это запишем.
    # Снимок «чем резали» уникален по имени файла НА ВСЮ СТУДИЮ (idx_print_settings_name).
    # С двумя машинами одинаковые имена (`plate_1.gcode` у обеих — обычное дело) молча
    # перезаписывали бы ответ на вопрос «какими настройками это напечатано», а он единственный
    # источник для калибровки и разбора брака. Уникальность становится ПАРОЙ (принтер, имя).
    # Индекс отдельный, поэтому меняется без пересоздания таблицы — в отличие от service_items,
    # где UNIQUE висит на колонке (см. цикл 3, волна 5: тот кусок ждёт отдельного захода).
    # Тара катушки (вес пустой шпули, г) у материала. Мастер сверяет склад ВЕСАМИ: кладёт
    # катушку целиком, и остаток филамента = показание − тара. Без этого числа взвешивание
    # ничего не даёт, а держать его в описании нельзя — описание видит клиент.
    await _ensure_column("plastics", "spool_weight_g", "REAL NOT NULL DEFAULT 0")
    # Оговорки к таре: у части материалов катушка зависит от ПАРТИИ (у Elegoo PLA Matte
    # приходят и картонная 150 г, и пластиковая многоразовая 195 г) — одним числом не сказать.
    await _ensure_column("plastics", "spool_note", "TEXT NOT NULL DEFAULT ''")
    # Ответ на сообщение внутри переписки (13.08.2026): им становится и реакция клиента
    # (эмодзи приходит отдельным сообщением, привязанным к тому, на что его поставили), и
    # ответ реплаем из Telegram. Колонка добавляется ЗДЕСЬ, а не рядом с user_id/image_path
    # выше: те строки стоят ДО версионного шага v5, который на старой базе пересоздаёт
    # order_messages по явному списку колонок, — добавленный до него reply_to тем же запуском
    # и потерялся бы, а вернулся бы только следующим стартом бота.
    await _ensure_column("order_messages", "reply_to", "INTEGER")
    await _ensure_column("print_settings", "printer_id", "INTEGER")
    await _db.execute("DROP INDEX IF EXISTS idx_print_settings_name")
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_print_settings_printer_name "
                      "ON print_settings(printer_id, name)")
    await _backfill_printer_ownership(_db)
    await _split_service_items_per_printer(_db)
    await _split_print_binding_per_printer(_db)
    for r in await (await _db.execute(
            "SELECT id, process FROM quality_presets WHERE nozzle = ''")).fetchall():
        n = nozzles.from_process_name(r["process"] or "")
        if n:
            await _db.execute("UPDATE quality_presets SET nozzle=? WHERE id=?", (n, r["id"]))

    # v8 (C2, отвязка data/ от захардкоженного пути): пути под DATA_DIR переезжают в БД
    # в ОТНОСИТЕЛЬНЫЙ вид. Шаг стоит здесь, ПОСЛЕ всех _ensure_column и пересозданий таблиц
    # (v5 пересоздаёт order_messages по явному списку колонок) — иначе правку данных смыло бы
    # следующим шагом. Версию ставим литералом, как и предыдущие шаги.
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 8:
        await _migrate_paths_to_relative(_db)
        await _db.execute("PRAGMA user_version = 8")

    # v9 (хвост C2): профили слайсера — в относительный вид с маркером корня установки.
    # Версию ставим ВСЕГДА, даже если корней на машине не нашлось и переписывать было нечего:
    # чтение таких баз держится на перекоренении в profilepaths.abs_profile, а запись любой
    # карточки и так уйдёт маркером. Иначе шаг крутился бы при каждом старте.
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 9:
        await _migrate_profiles_to_relative(_db)
        await _db.execute("PRAGMA user_version = 9")
    else:
        # ДОГОН. Шаг v9 смотрит на диск, а не на строки: если при первом прогоне слайсер ещё
        # не был установлен (или нашёлся лишь один корень), часть путей осталась абсолютной, и
        # одноразовый гейт больше не вернулся бы к ним никогда. Прогон дешёвый (шесть коротких
        # SELECT-ов) и идемпотентный — UPDATE только при реальном изменении строки.
        try:
            await _migrate_profiles_to_relative(_db)
        except Exception:  # noqa: BLE001 — догон не имеет права мешать старту бота
            log.debug("догон профилей слайсера не удался", exc_info=True)

    # v10 (Э0, очередь досчёта): requests.price_pending=1 — «цену посчитает ПК сам» (не «оценит
    # мастер»). Таблицы
    # очереди и дедупа приёмной — в _SCHEMA (CREATE IF NOT EXISTS), колонка — здесь, ПОСЛЕ
    # всех пересозданий таблиц выше (тот же довод, что у v8: правку схемы не смоет следующий
    # шаг). Шаг ДАННЫХ сознательно пуст: старые заказы «без сметы» в очередь НЕ ставим — по ним
    # мастер мог уже договориться с клиентом, и внезапное «заказ посчитан, подтвердите» по
    # заявке недельной давности хуже ручной оценки. Версия — маркер договора схемы: на неё
    # смотрит клиент приёмной (Э1), и шаг v11 встанет строго после. Литералом, как и прочие.
    await _ensure_column("requests", "price_pending", "INTEGER NOT NULL DEFAULT 0")
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 10:
        await _db.execute("PRAGMA user_version = 10")

    # Катушки CC2 Control (app/cc2spools.py, 08.10.2026). cc2_spool_id — id катушки в их службе:
    # такая катушка принадлежит принтеру, её остаток он меряет сам, а строка здесь — кэш.
    # job_name — файл печати у движения, перенесённого из их журнала (себестоимость заказа).
    # Данных не трогаем, поэтому версия схемы не растёт.
    await _ensure_column("spools", "cc2_spool_id", "TEXT")
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_spools_cc2 ON spools(cc2_spool_id) "
                      "WHERE cc2_spool_id IS NOT NULL")
    await _ensure_column("stock_ledger", "job_name", "TEXT")
    # Пластины стола CC2 Control (app/cc2plates.py): связанную пластину ведёт принтер.
    await _ensure_column("calib_plates", "cc2_plate_id", "TEXT")
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_calib_plates_cc2 "
                      "ON calib_plates(cc2_plate_id) WHERE cc2_plate_id IS NOT NULL")

    # сиды
    for key, value in DEFAULT_SETTINGS.items():
        await _db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    for key, value in DEFAULT_TEXT_SETTINGS.items():
        await _db.execute("INSERT OR IGNORE INTO text_settings(key, value) VALUES(?, ?)", (key, value))

    await _seed_printers()
    await _backfill_printer_nozzles()   # после сида: у нового принтера сразу есть его сопло
    await _seed_presets()
    await _seed_default_plastic()
    await _seed_service_items()

    await _db.commit()

    # v11 (копия базы на VPS, app/replica.py): дальше удалённое затирается нулями (secure_delete
    # в _open), а хвосты всего, что удаляли и перезаписывали ДО этого, вычищает один VACUUM:
    # страницы уезжают в копию как есть, и старые коды и токены уехали бы вместе с ними.
    # Последним шагом и вне транзакции (VACUUM внутри неё нельзя). Не вышло (базу держит
    # другой процесс) — не беда: старт важнее, шаг повторится при следующем.
    cur = await _db.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] < 11:
        try:
            await _db.execute("VACUUM")
            await _db.execute("PRAGMA user_version = 11")
            await _db.commit()
        except Exception:  # noqa: BLE001
            log.warning("v11: VACUUM не прошёл — повторю при следующем старте", exc_info=True)


# ---- C2: колонки, хранящие путь под DATA_DIR ----
# БЕЛЫЙ СПИСОК, а не поиск «любой текст с префиксом»: путь под data/ встречается и внутри
# свободного текста (activity_log.detail, error_log.traceback, kb_articles.body) — такие
# значения переписывать нельзя. Пути ВНЕ data/ (профили слайсера в plastics.filament_profile,
# printers.*, calib_presets.path, офсайтные бэкапы) сюда не входят: rel_data их не трогает по
# определению. Профили слайсера отвязаны ОТДЕЛЬНЫМ шагом v9 (_PROFILE_COLUMNS ниже) — у них
# своя система координат (корни установки движка), а не DATA_DIR.
_PATH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("collection", "preview_path"), ("collection", "photo_path"),
    ("saved_models", "preview_path"),
    ("requests", "preview_path"), ("requests", "done_photo"), ("requests", "file_path"),
    ("requests", "slicer_shot"), ("requests", "timelapse"),
    ("print_journal", "done_photo"), ("print_journal", "timelapse"),
    ("plastics", "photo_path"), ("products", "photo_path"),
    ("admin_notifications", "image_path"), ("order_messages", "image_path"),
    ("calib_samples", "photo"), ("backup_runs", "path"),
)
# Колонки со СПИСКОМ путей в JSON — переписываем поэлементно.
_PATH_JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("collection", "paths"), ("saved_models", "paths"), ("requests", "photos"),
)


async def _migrate_paths_to_relative(_db) -> None:
    """Переписать пути под DATA_DIR в относительный вид (шаг схемы v8, задача C2).

    Правила: работаем ТОЛЬКО по белому списку колонок; значение прогоняем через тот же
    config.rel_data, что и рантайм (один источник правды, иначе миграция и запись разъедутся);
    UPDATE делаем, только если значение изменилось (иначе каждый прогон переписывал бы ~4600
    строк и раздувал WAL); битый JSON пропускаем со строкой в лог, не роняя старт бота.
    Шаг ЧИСТО СТРОКОВЫЙ — к диску не обращается: тесты гоняют его при боевом DATA_DIR.
    """
    changed: dict[str, int] = {}
    for table, col in _PATH_COLUMNS:
        try:
            cur = await _db.execute(
                f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
            rows = await cur.fetchall()
        except Exception:  # noqa: BLE001 — нет таблицы/колонки на старой базе: пропускаем
            log.debug("C2: пропущено %s.%s", table, col, exc_info=True)
            continue
        n = 0
        for r in rows:
            old = r[1]
            if not isinstance(old, str):
                continue
            new = config.rel_data(old)
            if new is not None and new != old:
                await _db.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (new, r[0]))
                n += 1
        if n:
            changed[f"{table}.{col}"] = n
    for table, col in _PATH_JSON_COLUMNS:
        try:
            cur = await _db.execute(
                f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
            rows = await cur.fetchall()
        except Exception:  # noqa: BLE001
            log.debug("C2: пропущено %s.%s", table, col, exc_info=True)
            continue
        n = 0
        for r in rows:
            old = r[1]
            if not isinstance(old, str):
                continue
            try:
                items = json.loads(old)
            except (TypeError, ValueError):
                log.warning("C2: битый JSON в %s.%s rowid=%s — пропущено", table, col, r[0])
                continue
            if not isinstance(items, list):
                continue
            fixed = [config.rel_data(x) if isinstance(x, str) else x for x in items]
            if fixed != items:
                await _db.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                                  (json.dumps(fixed), r[0]))
                n += 1
        if n:
            changed[f"{table}.{col}"] = n
    if changed:
        log.info("C2: пути в БД переведены в относительные — %s",
                 ", ".join(f"{k}: {v}" for k, v in sorted(changed.items())))


# ---- C2 (хвост): колонки, хранящие путь к профилю СЛАЙСЕРА ----
# Вторая привязка к машине владельца после data/: профили лежали абсолютными путями в
# «C:/Program Files/<Слайсер>/resources/profiles/…» и «%APPDATA%/<Слайсер>/user/default/…».
# Белый список — по той же причине, что и у v8: путь встречается и в свободном тексте.
_PROFILE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("printers", "machine_profile"), ("printers", "process_dir"), ("printers", "filament_dir"),
    ("printer_nozzles", "machine_profile"), ("plastics", "filament_profile"),
    ("calib_presets", "path"),
)
# Колонка со СЛОВАРЁМ путей в JSON ({filament|machine|process: путь} паспорта калибровки) —
# переписываем поэлементно. Без неё «применить паспорт» на чужой машине молча ничего не пишет.
_PROFILE_JSON_DICT_COLUMNS: tuple[tuple[str, str], ...] = (("calib_passport", "presets_json"),)


async def _migrate_profiles_to_relative(_db) -> bool:
    """Профили слайсера в БД → маркер корня (шаг схемы v9, хвост задачи C2).

    Переписываем ТОЛЬКО значения, лежащие под текущими корнями установки (profilepaths.rel_profile
    их и находит); путь вне корней, пустое и уже относительное значение остаются как есть.
    Идемпотентно: повторный прогон ничего не меняет, UPDATE делаем только при изменении.

    Отличие от v8, и оно принципиальное: тот шаг был ЧИСТО СТРОКОВЫМ, а этот СМОТРИТ НА ДИСК —
    «под корнем ли путь» иначе не узнать. Следствие: на машине без слайсера переписывать
    нечего, и такая база останется с абсолютными путями; читать её это не мешает —
    profilepaths.abs_profile перекоренит опознаваемый хвост на корень той машины, где база
    в итоге откроется. Возвращаемое значение (нашлись ли корни) — для лога и тестов.

    calib_presets.path мигрируем ЦЕЛИКОМ или никак: по этой колонке группируются версии
    снимков (list_calib_preset_latest) — половина переписанных строк расщепила бы историю.
    """
    roots = {k: slicers.profile_roots(k) for k in slicers.BACKENDS}
    if not any(roots.values()):
        log.info("C2/профили: корней слайсера на этой машине нет — миграция путей отложена")
        return False
    changed: dict[str, int] = {}
    for table, col in _PROFILE_COLUMNS:
        try:
            cur = await _db.execute(
                f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
            rows = await cur.fetchall()
        except Exception:  # noqa: BLE001 — нет таблицы/колонки на старой базе: пропускаем
            log.debug("C2/профили: пропущено %s.%s", table, col, exc_info=True)
            continue
        n = 0
        for r in rows:
            old = r[1]
            if not isinstance(old, str):
                continue
            new = profilepaths.rel_profile(old)
            if new is not None and new != old:
                await _db.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (new, r[0]))
                n += 1
        if n:
            changed[f"{table}.{col}"] = n
    for table, col in _PROFILE_JSON_DICT_COLUMNS:
        try:
            cur = await _db.execute(
                f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
            rows = await cur.fetchall()
        except Exception:  # noqa: BLE001
            log.debug("C2/профили: пропущено %s.%s", table, col, exc_info=True)
            continue
        n = 0
        for r in rows:
            old = r[1]
            if not isinstance(old, str):
                continue
            try:
                items = json.loads(old)
            except (TypeError, ValueError):
                log.warning("C2/профили: битый JSON в %s.%s rowid=%s — пропущено", table, col, r[0])
                continue
            if not isinstance(items, dict):
                continue
            fixed = {k: (profilepaths.rel_profile(v) if isinstance(v, str) else v)
                     for k, v in items.items()}
            if fixed != items:
                await _db.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                                  (json.dumps(fixed, ensure_ascii=False), r[0]))
                n += 1
        if n:
            changed[f"{table}.{col}"] = n
    if changed:
        log.info("C2/профили: пути профилей слайсера переведены в относительные — %s",
                 ", ".join(f"{k}: {v}" for k, v in sorted(changed.items())))
    return True


async def _backfill_printer_ownership(_db) -> None:
    """Разово проставить «чья это печать» всей прошлой истории (цикл 3, волна 4).

    Пока принтер один, вся история заведомо его — и это единственный момент, когда так можно
    сказать. После первой печати на второй машине разделить будет нечем: ни журнал, ни
    телеметрия, ни заказы машину не записывали.

    Основной принтер вычисляем ОДИН РАЗ и им размечаем все три таблицы. Брать его на каждую
    строку нельзя: переключи владелец основную карточку посреди прогона — и история окажется
    разрезанной пополам по случайному признаку.

    Флаг в settings, а не «где NULL»: повторный прогон не должен приписывать первому принтеру
    записи, которые к тому времени уже честно принадлежат второму."""
    done = await (await _db.execute(
        "SELECT value FROM settings WHERE key='printer_ownership_backfilled'")).fetchone()
    if done:
        return
    row = await (await _db.execute(
        "SELECT id FROM printers WHERE is_active = 1 ORDER BY is_default DESC, id LIMIT 1"
    )).fetchone()
    pid = int(row["id"]) if row else None
    if pid:
        for table in ("print_journal", "printer_telemetry", "requests", "print_settings"):
            await _db.execute(f"UPDATE {table} SET printer_id = ? WHERE printer_id IS NULL",
                              (pid,))
        log.info("История печатей размечена принтером #%s (разово, цикл 3 волна 4)", pid)
    await _db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES('printer_ownership_backfilled','1')")


async def _split_service_items_per_printer(_db) -> None:
    """Наработка узлов ТО — своя у каждой машины (цикл 3, волна 5).

    Была уникальность `key TEXT UNIQUE` на всю студию: у двух принтеров не может быть двух
    записей «смазка направляющих», а значит обслуживание одной машины обнуляло бы счётчик
    другой — и вторая тихо ездила бы на просроченном ТО.

    В SQLite `UNIQUE` НА КОЛОНКЕ снимается только пересозданием таблицы. Делаем это аккуратно:
    список колонок читаем из схемы (а не зашиваем — миграции могли добавить свои), переносим
    ВСЕ строки как есть, наработку не трогаем. `service_log` не трогаем вовсе: он ссылается на
    item_id, а id мы сохраняем.

    Признак «уже сделано» — наличие колонки printer_id: она появляется только здесь."""
    cols = [r["name"] for r in await (await _db.execute("PRAGMA table_info(service_items)")).fetchall()]
    if not cols or "printer_id" in cols:
        return
    row = await (await _db.execute(
        "SELECT id FROM printers WHERE is_active = 1 ORDER BY is_default DESC, id LIMIT 1"
    )).fetchone()
    pid = int(row["id"]) if row else None
    keep = ", ".join(cols)
    await _db.execute("""
        CREATE TABLE service_items_v2(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          key TEXT,
          label TEXT NOT NULL,
          trigger_type TEXT NOT NULL DEFAULT 'hours',
          threshold REAL NOT NULL,
          hours_since REAL NOT NULL DEFAULT 0,
          prints_since INTEGER NOT NULL DEFAULT 0,
          last_service_at INTEGER NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          notified INTEGER NOT NULL DEFAULT 0,
          sort INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          printer_id INTEGER,
          UNIQUE(printer_id, key)
        )""")
    await _db.execute(f"INSERT INTO service_items_v2({keep}, printer_id) "
                      f"SELECT {keep}, ? FROM service_items", (pid,))
    await _db.execute("DROP TABLE service_items")
    await _db.execute("ALTER TABLE service_items_v2 RENAME TO service_items")
    log.info("Пункты ТО разделены по машинам: существующие закреплены за принтером #%s", pid)


async def _split_print_binding_per_printer(_db) -> None:
    """Привязка «текущая печать → заказ» — своя у каждой машины (цикл 3, волна 6).

    Была ровно одна строка: `id INTEGER PRIMARY KEY CHECK (id = 1)`. При одновременной печати
    на двух принтерах вторая привязка вытеснила бы первую — заказ остался бы без своей печати,
    а её финиш закрыл бы чужой заказ и отправил клиенту чужое фото результата.

    `CHECK` в SQLite снимается только пересозданием таблицы. Строка тут максимум одна и живёт
    минуты (текущая печать), так что переносим её как есть, закрепив за принтером по умолчанию:
    это ровно та машина, к которой она и относилась.

    Признак «уже сделано» — наличие колонки printer_id."""
    cols = [r["name"] for r in await (await _db.execute(
        "PRAGMA table_info(print_binding)")).fetchall()]
    if not cols or "printer_id" in cols:
        return
    row = await (await _db.execute(
        "SELECT id FROM printers WHERE is_active = 1 ORDER BY is_default DESC, id LIMIT 1"
    )).fetchone()
    pid = int(row["id"]) if row else 0
    await _db.execute("""
        CREATE TABLE print_binding_v2(
          printer_id INTEGER PRIMARY KEY,
          request_id INTEGER,
          job_name TEXT,
          started_at INTEGER,
          was_active INTEGER NOT NULL DEFAULT 0,
          finished INTEGER NOT NULL DEFAULT 0,
          confirmed INTEGER NOT NULL DEFAULT 0
        )""")
    await _db.execute(
        "INSERT INTO print_binding_v2(printer_id, request_id, job_name, started_at, "
        "was_active, finished, confirmed) "
        "SELECT ?, request_id, job_name, started_at, was_active, finished, confirmed "
        "FROM print_binding", (pid,))
    await _db.execute("DROP TABLE print_binding")
    await _db.execute("ALTER TABLE print_binding_v2 RENAME TO print_binding")
    log.info("Привязка печати разделена по машинам: текущая закреплена за принтером #%s", pid)


async def _seed_printers() -> None:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM printers")
    if (await cur.fetchone())["c"] == 0:
        await _conn().execute(
            "INSERT INTO printers(name, machine_profile, process_dir, filament_dir, is_active, is_default) "
            "VALUES(?,?,?,?,1,1)",
            # Сид пишет ОТНОСИТЕЛЬНО корня слайсера: у лицензиата свежая база иначе получила
            # бы абсолютные пути машины, на которой собирался установщик.
            ("Elegoo Centauri Carbon 2 (0.4 мм)",
             profilepaths.rel_profile(config.MACHINE_PROFILE),
             profilepaths.rel_profile(config.PROCESS_DIR),
             profilepaths.rel_profile(config.FILAMENT_DIR)),
        )


async def _backfill_printer_nozzles() -> None:
    """У каждого принтера должно быть хотя бы одно сопло — его текущий machine_profile.

    Диаметр читаем из самого профиля (`nozzle_diameter`, с учётом `inherits`), иначе — из имени,
    иначе исторический 0.4. Идемпотентно: принтеру, у которого запись уже есть, ничего не делаем,
    поэтому миграция безопасно переживает и рестарт, и переключение канала выпуска."""
    rows = await (await _conn().execute(
        "SELECT p.id, p.machine_profile FROM printers p WHERE NOT EXISTS("
        "  SELECT 1 FROM printer_nozzles n WHERE n.printer_id = p.id)")).fetchall()
    for r in rows:
        # Профиль в БД с v9 лежит маркером корня (@elegoo/resources/…) — на диск ходить надо
        # развёрнутым путём, иначе диаметр сопла тихо падал бы на исторический 0.4.
        n = nozzles.from_machine_profile(profilepaths.abs_profile_str(r["machine_profile"]))             or nozzles.DEFAULT
        await _conn().execute(
            "INSERT OR IGNORE INTO printer_nozzles(printer_id, nozzle, machine_profile, "
            "is_default, created_at) VALUES(?,?,?,1,?)",
            (r["id"], n, r["machine_profile"], int(time.time())))


async def _seed_presets() -> None:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM quality_presets")
    if (await cur.fetchone())["c"] == 0:
        for i, key in enumerate(presets.QUALITY_ORDER):
            p = presets.QUALITY_PRESETS[key]
            await _conn().execute(
                "INSERT INTO quality_presets(label, layer_height, process, sort, is_active) VALUES(?,?,?,?,1)",
                (p["label"], p["layer_height"], p["process"], i),
            )
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM strength_presets")
    if (await cur.fetchone())["c"] == 0:
        for i, key in enumerate(presets.STRENGTH_ORDER):
            p = presets.STRENGTH_PRESETS[key]
            await _conn().execute(
                "INSERT INTO strength_presets(label, infill, walls, sort, is_active) VALUES(?,?,?,?,1)",
                (p["label"], p["infill"], p["walls"], i),
            )


async def _seed_default_plastic() -> None:
    cur = await _conn().execute("SELECT COUNT(*) AS c FROM plastics")
    if (await cur.fetchone())["c"] == 0:
        await _conn().execute(
            "INSERT INTO plastics(name, price_per_kg, density, is_active, created_at, material_type, filament_profile) "
            "VALUES(?,?,?,1,?,?,?)",
            ("Elegoo PLA", 45.0, 1.24, int(time.time()), "PLA",
             profilepaths.rel_profile(config.DEFAULT_FILAMENT_PROFILE)),
        )


async def close() -> None:
    conn = core.db_or_none()
    if conn is not None:
        await conn.close()


# ==========================================================================================
# ФАСАД СЛОЯ ДАННЫХ — постоянный публичный интерфейс app.db, а не переходный костыль.
# Собран в ОДИН блок волной 7 (раньше 22 куска были разбросаны по местам вырезов волн 0-6 и
# перемежались осиротевшими маркерами «продолжение выреза волны N»).
# Правила endgame, действующие для всего блока: явное перечисление по строке на имя, noqa
# построчно, группы отсортированы, `ruff check --fix` / `ruff format` по блоку НИКОГДА не
# запускать (они схлопывают строки и рвут фасад). Потребители зовут `db.<имя>` и
# `from .db import <имя>` — это ТОТ ЖЕ объект, что в доменном модуле.
# Блок стоит ПОСЛЕ кода намеренно: выше по файлу его имена нужны только внутри тел (init и
# миграции зовут DEFAULT_SETTINGS, DEFAULT_TEXT_SETTINGS, _canon_color, _conn,
# _seed_service_items, backfill_order_groups голыми именами), а модульных операторов, которые
# бы их трогали, в db.py ровно три — log, SCHEMA_VERSION и _SCHEMA. К первому вызову init()
# модуль импортирован целиком, и все голые имена резолвятся из globals.
# ==========================================================================================


# Модели строк и статусы заказов уехали в app/store/models.py (волна 1 разреза db).
from .store import models  # noqa: E402,F401,I001
from .store.models import CollectionItem  # noqa: E402,F401,I001
from .store.models import DiscountTier  # noqa: E402,F401,I001
from .store.models import Plastic  # noqa: E402,F401,I001
from .store.models import PostprocOption  # noqa: E402,F401,I001
from .store.models import Printer  # noqa: E402,F401,I001
from .store.models import PrinterNozzle  # noqa: E402,F401,I001
from .store.models import Product  # noqa: E402,F401,I001
from .store.models import QualityPreset  # noqa: E402,F401,I001
from .store.models import REQ_CANCELLABLE  # noqa: E402,F401,I001
from .store.models import REQ_STATUS_LABELS  # noqa: E402,F401,I001
from .store.models import Request  # noqa: E402,F401,I001
from .store.models import SavedModel  # noqa: E402,F401,I001
from .store.models import ServiceItem  # noqa: E402,F401,I001
from .store.models import StrengthPreset  # noqa: E402,F401,I001


# ИНФРАСТРУКТУРА пакета store, вынесенная в фасад двумя разными волнами.
# _conn — соединение уехало в app/store/core.py (волна 0 разреза db.py, 21.08.2026): оно
# пере-присваивается (init/connect_existing), поэтому реэкспортировать САМ ОБЪЕКТ нельзя —
# та же механика, что у webapp._bot. Фасад _conn зовут и тела этого файла (_ensure_column),
# и тесты как db._conn(). Жизненным циклом владеет по-прежнему этот модуль —
# init/connect_existing/close ставят соединение через core.set_db(). Тестам, эмулирующим
# «БД упала», подменять app.store.core._db, не db._db.
# _jload — инфраструктурный хелпер (волна 2): его зовут и склад (store/plastics), и
# уведомления с калибровкой (store/ops, store/calib). ВНУТРИ САМОГО db.py вызовов _jload
# больше не осталось — последние три уехали волной 6, — но фасадная строка остаётся: это
# публичный интерфейс слоя данных, и tests/test_store_wave2_facade.py требует
# `db._jload is core._jload`.
# Сам ХЭНДЛ модуля (`from .store import core`) стоит выше, в блоке настоящих импортов:
# init/connect_existing/close зовут core.set_db()/core.db_or_none() по-настоящему.
from .store.core import _conn  # noqa: E402,F401,I001
from .store.core import _jload  # noqa: E402,F401,I001


# Настройки мастерской — числовые и текстовые, вместе со всем семейством дефолтов,
# подписей и порядков групп — уехали в app/store/settings.py (волна 2 разреза db);
# get_text_setting дописана туда же волной 6 (её домен — настройки, а не деск-вход,
# в хвосте секции которого она физически лежала).
# Хэндл модуля — под ПСЕВДОНИМОМ store_settings. Исходная причина (голое `settings`
# столкнулось бы с параметром promised_date(..., settings: dict) ниже по файлу, ruff F811)
# волной 7 исчезла вместе с самой promised_date, но псевдоним ОСТАЁТСЯ: он давно часть
# публичного интерфейса и закреплён tests/test_store_wave2_facade.py и wave6.
from .store import settings as store_settings  # noqa: E402,F401,I001
from .store.settings import ACCESS_SETTINGS_ORDER  # noqa: E402,F401,I001
from .store.settings import BRAND_SETTINGS_ORDER  # noqa: E402,F401,I001
from .store.settings import DEFAULT_SETTINGS  # noqa: E402,F401,I001
from .store.settings import DEFAULT_TEXT_SETTINGS  # noqa: E402,F401,I001
from .store.settings import OWNERS_SETTINGS_ORDER  # noqa: E402,F401,I001
from .store.settings import PAY_SETTINGS_ORDER  # noqa: E402,F401,I001
from .store.settings import SETTINGS_ORDER  # noqa: E402,F401,I001
from .store.settings import SETTING_LABELS  # noqa: E402,F401,I001
from .store.settings import TEXT_SETTINGS_ORDER  # noqa: E402,F401,I001
from .store.settings import TEXT_SETTING_LABELS  # noqa: E402,F401,I001
from .store.settings import delete_setting  # noqa: E402,F401,I001
from .store.settings import get_settings  # noqa: E402,F401,I001
from .store.settings import get_text_setting  # noqa: E402,F401,I001
from .store.settings import get_text_settings  # noqa: E402,F401,I001
from .store.settings import set_setting  # noqa: E402,F401,I001
from .store.settings import set_text_setting  # noqa: E402,F401,I001


# Склад филамента целиком (пластики, остатки по цветам, катушки + журнал движений,
# резерв под заказ, себестоимость по факту, оттенки цветов) уехал в app/store/plastics.py
# (волна 2 разреза db). Резать связку dual-write по границе commit нельзя — отсюда один модуль.
from .store import plastics  # noqa: E402,F401,I001
from .store.plastics import _RESERVING_STATUSES  # noqa: E402,F401,I001
from .store.plastics import _RES_PH  # noqa: E402,F401,I001
from .store.plastics import _canon_color  # noqa: E402,F401,I001
from .store.plastics import _deduct_stock_legacy  # noqa: E402,F401,I001
from .store.plastics import _dual_write_spool  # noqa: E402,F401,I001
from .store.plastics import _hex_to_rgb  # noqa: E402,F401,I001
from .store.plastics import _spool_recalc  # noqa: E402,F401,I001
from .store.plastics import _to_plastic  # noqa: E402,F401,I001
from .store.plastics import _to_spool  # noqa: E402,F401,I001
from .store.plastics import add_plastic  # noqa: E402,F401,I001
from .store.plastics import add_spool  # noqa: E402,F401,I001
from .store.plastics import add_stock_ledger  # noqa: E402,F401,I001
from .store.plastics import apply_order_stock  # noqa: E402,F401,I001
from .store.plastics import cc2_add_spool  # noqa: E402,F401,I001
from .store.plastics import cc2_apply_snapshot  # noqa: E402,F401,I001
from .store.plastics import cc2_has_links  # noqa: E402,F401,I001
from .store.plastics import cc2_id_ok  # noqa: E402,F401,I001
from .store.plastics import cc2_job_same  # noqa: E402,F401,I001
from .store.plastics import cc2_job_usage  # noqa: E402,F401,I001
from .store.plastics import cc2_link_spool  # noqa: E402,F401,I001
from .store.plastics import cc2_linked_ids  # noqa: E402,F401,I001
from .store.plastics import cc2_owned_colors  # noqa: E402,F401,I001
from .store.plastics import cc2_printed_requests_for_jobs  # noqa: E402,F401,I001
from .store.plastics import cc2_recent_jobs  # noqa: E402,F401,I001
from .store.plastics import cc2_request_usage  # noqa: E402,F401,I001
from .store.plastics import cogs_summary  # noqa: E402,F401,I001
from .store.plastics import color_availability  # noqa: E402,F401,I001
from .store.plastics import compute_request_cogs  # noqa: E402,F401,I001
from .store.plastics import deduct_color_stock  # noqa: E402,F401,I001
from .store.plastics import deduct_color_stock_split  # noqa: E402,F401,I001
from .store.plastics import deduct_from_spools  # noqa: E402,F401,I001
from .store.plastics import deduct_stock  # noqa: E402,F401,I001
from .store.plastics import delete_color_shade  # noqa: E402,F401,I001
from .store.plastics import delete_plastic  # noqa: E402,F401,I001
from .store.plastics import get_color_overrides  # noqa: E402,F401,I001
from .store.plastics import get_color_stock  # noqa: E402,F401,I001
from .store.plastics import get_default_plastic  # noqa: E402,F401,I001
from .store.plastics import get_order_stock_plan  # noqa: E402,F401,I001
from .store.plastics import get_plastic  # noqa: E402,F401,I001
from .store.plastics import get_plastic_by_name  # noqa: E402,F401,I001
from .store.plastics import get_spool  # noqa: E402,F401,I001
from .store.plastics import increment_plastic_uses  # noqa: E402,F401,I001
from .store.plastics import inventory_summary  # noqa: E402,F401,I001
from .store.plastics import list_color_shades  # noqa: E402,F401,I001
from .store.plastics import list_plastics  # noqa: E402,F401,I001
from .store.plastics import list_spools  # noqa: E402,F401,I001
from .store.plastics import popular_plastics  # noqa: E402,F401,I001
from .store.plastics import recent_plastics_for_user  # noqa: E402,F401,I001
from .store.plastics import reserved_grams_by_plastic  # noqa: E402,F401,I001
from .store.plastics import set_color_shade  # noqa: E402,F401,I001
from .store.plastics import set_color_low  # noqa: E402,F401,I001
from .store.plastics import set_color_stock  # noqa: E402,F401,I001
from .store.plastics import set_default_plastic  # noqa: E402,F401,I001
from .store.plastics import set_order_stock_plan  # noqa: E402,F401,I001
from .store.plastics import set_spool_status  # noqa: E402,F401,I001
from .store.plastics import spool_is_cc2  # noqa: E402,F401,I001
from .store.plastics import spool_ledger  # noqa: E402,F401,I001
from .store.plastics import spool_stock_by_color  # noqa: E402,F401,I001
from .store.plastics import spool_stock_total  # noqa: E402,F401,I001
from .store.plastics import update_plastic  # noqa: E402,F401,I001


# Справочники печати без склада (сопла, пресеты качества и прочности, скидки за тираж,
# платные опции постобработки) уехали в app/store/catalog.py (волна 2 разреза db).
# Модуль назван catalog, а не presets: имя `presets` в db.py занято модулем app/presets.py.
from .store import catalog  # noqa: E402,F401,I001
from .store.catalog import _to_nozzle  # noqa: E402,F401,I001
from .store.catalog import _to_postproc  # noqa: E402,F401,I001
from .store.catalog import _to_quality  # noqa: E402,F401,I001
from .store.catalog import _to_strength  # noqa: E402,F401,I001
from .store.catalog import add_discount  # noqa: E402,F401,I001
from .store.catalog import add_postproc_option  # noqa: E402,F401,I001
from .store.catalog import add_printer_nozzle  # noqa: E402,F401,I001
from .store.catalog import add_quality  # noqa: E402,F401,I001
from .store.catalog import add_strength  # noqa: E402,F401,I001
from .store.catalog import best_discount  # noqa: E402,F401,I001
from .store.catalog import delete_discount  # noqa: E402,F401,I001
from .store.catalog import delete_postproc_option  # noqa: E402,F401,I001
from .store.catalog import delete_printer_nozzle  # noqa: E402,F401,I001
from .store.catalog import delete_quality  # noqa: E402,F401,I001
from .store.catalog import delete_strength  # noqa: E402,F401,I001
from .store.catalog import get_quality  # noqa: E402,F401,I001
from .store.catalog import get_request_options  # noqa: E402,F401,I001
from .store.catalog import get_strength  # noqa: E402,F401,I001
from .store.catalog import known_nozzles  # noqa: E402,F401,I001
from .store.catalog import list_discounts  # noqa: E402,F401,I001
from .store.catalog import list_postproc_options  # noqa: E402,F401,I001
from .store.catalog import list_printer_nozzles  # noqa: E402,F401,I001
from .store.catalog import list_quality  # noqa: E402,F401,I001
from .store.catalog import list_strength  # noqa: E402,F401,I001
from .store.catalog import machine_profile_for  # noqa: E402,F401,I001
from .store.catalog import nozzle_of  # noqa: E402,F401,I001
from .store.catalog import set_default_printer_nozzle  # noqa: E402,F401,I001
from .store.catalog import set_default_quality  # noqa: E402,F401,I001
from .store.catalog import set_default_strength  # noqa: E402,F401,I001
from .store.catalog import set_request_options  # noqa: E402,F401,I001
from .store.catalog import update_postproc_option  # noqa: E402,F401,I001
from .store.catalog import update_printer_nozzle  # noqa: E402,F401,I001
from .store.catalog import update_quality  # noqa: E402,F401,I001
from .store.catalog import update_strength  # noqa: E402,F401,I001


# Железо мастерской (карточки принтеров + кэш ёмкости и фоновые подписчики,
# обслуживание по узлам с журналом ТО, пластины стола с Z-offset) уехало в
# app/store/printers.py (волна 3 разреза db). Плиты внутри него, а не в модуле
# `plates`: это имя в db.py занято параметром add_request/save_model (ruff F811).
from .store import printers  # noqa: E402,F401,I001
from .store.printers import PLATE_KINDS  # noqa: E402,F401,I001
from .store.printers import TIME_FACTOR_MAX  # noqa: E402,F401,I001
from .store.printers import TIME_FACTOR_MIN  # noqa: E402,F401,I001
from .store.printers import _DEFAULT_SERVICE_ITEMS  # noqa: E402,F401,I001
from .store.printers import _seed_service_items  # noqa: E402,F401,I001
from .store.printers import _to_printer  # noqa: E402,F401,I001
from .store.printers import _to_service_item  # noqa: E402,F401,I001
from .store.printers import accrue_service  # noqa: E402,F401,I001
from .store.printers import add_printer  # noqa: E402,F401,I001
from .store.printers import add_service_item  # noqa: E402,F401,I001
from .store.printers import calib_plate_cc2  # noqa: E402,F401,I001
from .store.printers import cc2_plate_link  # noqa: E402,F401,I001
from .store.printers import cc2_plates_apply  # noqa: E402,F401,I001
from .store.printers import delete_calib_plate  # noqa: E402,F401,I001
from .store.printers import delete_printer  # noqa: E402,F401,I001
from .store.printers import delete_service_item  # noqa: E402,F401,I001
from .store.printers import get_calib_plate  # noqa: E402,F401,I001
from .store.printers import get_default_printer  # noqa: E402,F401,I001
from .store.printers import get_printer  # noqa: E402,F401,I001
from .store.printers import get_service_item  # noqa: E402,F401,I001
from .store.printers import list_calib_plates  # noqa: E402,F401,I001
from .store.printers import list_printers  # noqa: E402,F401,I001
from .store.printers import list_service_items  # noqa: E402,F401,I001
from .store.printers import list_service_log  # noqa: E402,F401,I001
from .store.printers import mark_service_notified  # noqa: E402,F401,I001
from .store.printers import mark_serviced  # noqa: E402,F401,I001
from .store.printers import refresh_active_printers  # noqa: E402,F401,I001
from .store.printers import refresh_printer_sessions  # noqa: E402,F401,I001
from .store.printers import refresh_printers  # noqa: E402,F401,I001
from .store.printers import save_calib_plate  # noqa: E402,F401,I001
from .store.printers import set_default_calib_plate  # noqa: E402,F401,I001
from .store.printers import set_default_printer  # noqa: E402,F401,I001
from .store.printers import update_printer  # noqa: E402,F401,I001
from .store.printers import update_service_item  # noqa: E402,F401,I001
from .store.printers import valid_time_factor  # noqa: E402,F401,I001


# Журнал печатей мастера целиком (print_journal, телеметрия принтера, самопечать-
# обёртка и мост «журнал ↔ калибровка») уехал в app/store/printjournal.py (волна 3
# разреза db). add_print_journal пишет пять записей под одной логикой идемпотентности
# и на хвосте зовёт attach_journal_to_calib — связка режется только целиком, поэтому
# сюда же втянуты четыре имени из секции «Калибровка» (домен волны 6).
from .store import printjournal  # noqa: E402,F401,I001
from .store.printjournal import _calib_step_row  # noqa: E402,F401,I001
from .store.printjournal import _fail_pct_norm  # noqa: E402,F401,I001
from .store.printjournal import active_calib_run  # noqa: E402,F401,I001
from .store.printjournal import add_print_journal  # noqa: E402,F401,I001
from .store.printjournal import add_printer_telemetry  # noqa: E402,F401,I001
from .store.printjournal import add_print_phase  # noqa: E402,F401,I001
from .store.printjournal import last_print_phase  # noqa: E402,F401,I001
from .store.printjournal import add_self_print  # noqa: E402,F401,I001
from .store.printjournal import apply_journal_stock  # noqa: E402,F401,I001
from .store.printjournal import attach_journal_to_calib  # noqa: E402,F401,I001
from .store.printjournal import backfill_calib_types  # noqa: E402,F401,I001
from .store.printjournal import cc2_fill_journal  # noqa: E402,F401,I001
from .store.printjournal import delete_print_journal  # noqa: E402,F401,I001
from .store.printjournal import get_calib_step  # noqa: E402,F401,I001
from .store.printjournal import get_print_event  # noqa: E402,F401,I001
from .store.printjournal import get_print_journal  # noqa: E402,F401,I001
from .store.printjournal import grade_print_journal  # noqa: E402,F401,I001
from .store.printjournal import list_calib_journal  # noqa: E402,F401,I001
from .store.printjournal import list_print_journal  # noqa: E402,F401,I001
from .store.printjournal import list_print_journal_for_request  # noqa: E402,F401,I001
from .store.printjournal import list_printer_telemetry  # noqa: E402,F401,I001
from .store.printjournal import print_journal_stats  # noqa: E402,F401,I001
from .store.printjournal import prune_printer_telemetry  # noqa: E402,F401,I001
from .store.printjournal import set_journal_calib  # noqa: E402,F401,I001
from .store.printjournal import set_print_journal_fields  # noqa: E402,F401,I001
from .store.printjournal import update_calib_step  # noqa: E402,F401,I001
from .store.printjournal import update_print_journal  # noqa: E402,F401,I001


# Задание, уехавшее на принтер (привязка текущей печати к заказу по машине + снимок
# настроек, с которыми оно уехало), уехало в app/store/printjob.py (волна 3 разреза db).
from .store import printjob  # noqa: E402,F401,I001
from .store.printjob import _settings_row  # noqa: E402,F401,I001
from .store.printjob import binding_key  # noqa: E402,F401,I001
from .store.printjob import clear_print_binding  # noqa: E402,F401,I001
from .store.printjob import clear_print_binding_for_request  # noqa: E402,F401,I001
from .store.printjob import find_print_settings  # noqa: E402,F401,I001
from .store.printjob import get_print_binding  # noqa: E402,F401,I001
from .store.printjob import get_print_settings  # noqa: E402,F401,I001
from .store.printjob import list_print_bindings  # noqa: E402,F401,I001
from .store.printjob import list_print_settings  # noqa: E402,F401,I001
from .store.printjob import prune_print_settings  # noqa: E402,F401,I001
from .store.printjob import save_print_settings  # noqa: E402,F401,I001
from .store.printjob import set_print_binding  # noqa: E402,F401,I001
from .store.printjob import update_print_binding  # noqa: E402,F401,I001


# Очередь печати с позициями (активные заказы, нормализация queue_pos, перестановка,
# срез для LCD-дашборда) уехала в app/store/printqueue.py (волна 3 разреза db).
from .store import printqueue  # noqa: E402,F401,I001
from .store.printqueue import _queue_active_rows  # noqa: E402,F401,I001
from .store.printqueue import lcd_print_queue  # noqa: E402,F401,I001
from .store.printqueue import queue_items  # noqa: E402,F401,I001
from .store.printqueue import queue_move  # noqa: E402,F401,I001
from .store.printqueue import queue_normalize  # noqa: E402,F401,I001
from .store.printqueue import queue_position  # noqa: E402,F401,I001


# Заказ как сущность целиком (строки requests и таймлайн req_status_history, корзина-
# группа order_groups с доставкой, самопечать мастера, персональные скидки клиентам,
# «непросмотренные» для LCD, легаси-таблица orders и два агрегата по requests) уехал
# в app/store/orders.py (волна 4 разреза db). Доставка живёт там же: белый список полей
# шапки группы — это поля доставки, а group_anchor_request_id держит её единственный
# носитель. backfill_order_groups зовёт init() на шаге user_version<6 — через этот фасад.
from .store import orders  # noqa: E402,F401,I001
from .store.orders import _GROUP_FIELDS  # noqa: E402,F401,I001
from .store.orders import _self_print_predicate  # noqa: E402,F401,I001
from .store.orders import _to_request  # noqa: E402,F401,I001
from .store.orders import add_delivery_type  # noqa: E402,F401,I001
from .store.orders import add_order  # noqa: E402,F401,I001
from .store.orders import add_request  # noqa: E402,F401,I001
from .store.orders import admin_would_spend  # noqa: E402,F401,I001
from .store.orders import apply_client_edit  # noqa: E402,F401,I001
from .store.orders import backfill_order_groups  # noqa: E402,F401,I001
from .store.orders import count_unseen_requests  # noqa: E402,F401,I001
from .store.orders import count_user_requests  # noqa: E402,F401,I001
from .store.orders import create_order_group  # noqa: E402,F401,I001
from .store.orders import create_self_print_order  # noqa: E402,F401,I001
from .store.orders import daily_revenue  # noqa: E402,F401,I001
from .store.orders import delete_delivery_type  # noqa: E402,F401,I001
from .store.orders import delete_request  # noqa: E402,F401,I001
from .store.orders import delete_test_requests  # noqa: E402,F401,I001
from .store.orders import delete_user_discount  # noqa: E402,F401,I001
from .store.orders import get_delivery_type  # noqa: E402,F401,I001
from .store.orders import get_order_group  # noqa: E402,F401,I001
from .store.orders import get_request  # noqa: E402,F401,I001
from .store.orders import get_status_history  # noqa: E402,F401,I001
from .store.orders import get_user_discount  # noqa: E402,F401,I001
from .store.orders import group_anchor_request_id  # noqa: E402,F401,I001
from .store.orders import group_id_of_request  # noqa: E402,F401,I001
from .store.orders import is_self_order  # noqa: E402,F401,I001
from .store.orders import is_self_print_order  # noqa: E402,F401,I001
from .store.orders import list_customers  # noqa: E402,F401,I001
from .store.orders import list_delivery_types  # noqa: E402,F401,I001
from .store.orders import list_requests  # noqa: E402,F401,I001
from .store.orders import list_requests_by_group  # noqa: E402,F401,I001
from .store.orders import list_user_discounts  # noqa: E402,F401,I001
from .store.orders import list_user_requests  # noqa: E402,F401,I001
from .store.orders import mark_all_requests_seen  # noqa: E402,F401,I001
from .store.orders import mark_request_seen  # noqa: E402,F401,I001
from .store.orders import pending_price_count  # noqa: E402,F401,I001
from .store.orders import queue_load  # noqa: E402,F401,I001
from .store.orders import set_user_discount  # noqa: E402,F401,I001
from .store.orders import update_delivery_type  # noqa: E402,F401,I001
from .store.orders import update_order_group  # noqa: E402,F401,I001
from .store.orders import update_request  # noqa: E402,F401,I001


# «Мастерская органайзеров» (Gridfinity): проекты-ящики, спецификация деталей, мерки,
# версии раскладки, заготовки и ссылка «показать» — app/store/gridfinity.py. Сюда же
# переехали из store/profile.py черновик и снимок заявки: домен собран в одном месте.
from .store import gridfinity  # noqa: E402,F401,I001
from .store.gridfinity import gridfinity_config_get  # noqa: E402,F401,I001
from .store.gridfinity import gridfinity_config_set  # noqa: E402,F401,I001
from .store.gridfinity import gridfinity_order_config_add  # noqa: E402,F401,I001
from .store.gridfinity import gridfinity_order_config_get  # noqa: E402,F401,I001
from .store.gridfinity import gf_measure_add  # noqa: E402,F401,I001
from .store.gridfinity import gf_measure_delete  # noqa: E402,F401,I001
from .store.gridfinity import gf_measures  # noqa: E402,F401,I001
from .store.gridfinity import gf_part  # noqa: E402,F401,I001
from .store.gridfinity import gf_part_add  # noqa: E402,F401,I001
from .store.gridfinity import gf_part_delete  # noqa: E402,F401,I001
from .store.gridfinity import gf_part_set_printed  # noqa: E402,F401,I001
from .store.gridfinity import gf_part_update  # noqa: E402,F401,I001
from .store.gridfinity import gf_parts  # noqa: E402,F401,I001
from .store.gridfinity import gf_parts_by_request  # noqa: E402,F401,I001
from .store.gridfinity import gf_parts_order  # noqa: E402,F401,I001
from .store.gridfinity import gf_parts_release_request  # noqa: E402,F401,I001
from .store.gridfinity import gf_parts_select  # noqa: E402,F401,I001
from .store.gridfinity import gf_request_statuses  # noqa: E402,F401,I001
from .store.gridfinity import gf_drop_legacy_draft  # noqa: E402,F401,I001
from .store.gridfinity import gf_preset_add  # noqa: E402,F401,I001
from .store.gridfinity import gf_preset_delete  # noqa: E402,F401,I001
from .store.gridfinity import gf_presets  # noqa: E402,F401,I001
from .store.gridfinity import gf_project  # noqa: E402,F401,I001
from .store.gridfinity import gf_project_add  # noqa: E402,F401,I001
from .store.gridfinity import gf_project_archive  # noqa: E402,F401,I001
from .store.gridfinity import gf_project_count  # noqa: E402,F401,I001
from .store.gridfinity import gf_project_delete  # noqa: E402,F401,I001
from .store.gridfinity import gf_project_rename  # noqa: E402,F401,I001
from .store.gridfinity import gf_project_set_config  # noqa: E402,F401,I001
from .store.gridfinity import gf_projects  # noqa: E402,F401,I001
from .store.gridfinity import gf_desk_drop  # noqa: E402,F401,I001
from .store.gridfinity import gf_desk_get  # noqa: E402,F401,I001
from .store.gridfinity import gf_desk_of  # noqa: E402,F401,I001
from .store.gridfinity import gf_desk_set  # noqa: E402,F401,I001
from .store.gridfinity import gf_desk_touch  # noqa: E402,F401,I001
from .store.gridfinity import gf_share_drop  # noqa: E402,F401,I001
from .store.gridfinity import gf_user_name  # noqa: E402,F401,I001
from .store.gridfinity import gf_share_get  # noqa: E402,F401,I001
from .store.gridfinity import gf_share_of  # noqa: E402,F401,I001
from .store.gridfinity import gf_share_set  # noqa: E402,F401,I001
from .store.gridfinity import gf_version_add  # noqa: E402,F401,I001
from .store.gridfinity import gf_version_get  # noqa: E402,F401,I001
from .store.gridfinity import gf_versions  # noqa: E402,F401,I001


# Деньги клиента ОДНОЙ транзакционной связкой (личный баланс user_balance + журнал
# balance_tx, онлайн-платежи A1 с идемпотентностью по tg_charge_id, семейный счёт
# целиком) уехали в app/store/money.py (волна 4 разреза db). Резать связку нельзя:
# adjust_balance при членстве в семье делегирует на общий счёт, а роспуск семьи
# возвращает остаток на личный баланс тем же commit-ом — это инцидентная зона 16.07.
from .store import money  # noqa: E402,F401,I001
from .store.money import _family_member_name  # noqa: E402,F401,I001
from .store.money import add_family_member  # noqa: E402,F401,I001
from .store.money import add_payment  # noqa: E402,F401,I001
from .store.money import adjust_balance  # noqa: E402,F401,I001
from .store.money import adjust_family_balance  # noqa: E402,F401,I001
from .store.money import create_family  # noqa: E402,F401,I001
from .store.money import family_of  # noqa: E402,F401,I001
from .store.money import get_balance  # noqa: E402,F401,I001
from .store.money import get_family  # noqa: E402,F401,I001
from .store.money import get_personal_balance  # noqa: E402,F401,I001
from .store.money import list_balance_tx  # noqa: E402,F401,I001
from .store.money import list_families  # noqa: E402,F401,I001
from .store.money import list_family_tx  # noqa: E402,F401,I001
from .store.money import payments_for_request  # noqa: E402,F401,I001
from .store.money import remove_family_member  # noqa: E402,F401,I001
from .store.money import set_balance_tx_comment  # noqa: E402,F401,I001


# Общая (кросс-пользовательская) коллекция моделей целиком — каталог collection,
# категории с перепривязкой строк одним commit-ом, фаззи-поиск без внешних зависимостей,
# избранное и рекомендации, плюс два примыкающих кэша каталога (очередь разбора
# незагрузившихся моделей calc_queue и кэш результата слайса calc_cache) — уехала
# в app/store/collection.py (волна 5 разреза db). Вместе с телами туда уехали
# functools/random/re: в db.py они были нужны только этим функциям.
from .store import collection  # noqa: E402,F401,I001
from .store.collection import _WORD_RE  # noqa: E402,F401,I001
from .store.collection import _fuzzy_threshold  # noqa: E402,F401,I001
from .store.collection import _fuzzy_vocab_matches  # noqa: E402,F401,I001
from .store.collection import _levenshtein_le  # noqa: E402,F401,I001
from .store.collection import _row_fuzzy_hit  # noqa: E402,F401,I001
from .store.collection import _to_collection  # noqa: E402,F401,I001
from .store.collection import _token_fuzzy_match  # noqa: E402,F401,I001
from .store.collection import _tokenize  # noqa: E402,F401,I001
from .store.collection import add_calc_queue  # noqa: E402,F401,I001
from .store.collection import add_to_collection  # noqa: E402,F401,I001
from .store.collection import collection_last_minutes  # noqa: E402,F401,I001
from .store.collection import count_calc_queue  # noqa: E402,F401,I001
from .store.collection import count_collection  # noqa: E402,F401,I001
from .store.collection import count_collection_by_category  # noqa: E402,F401,I001
from .store.collection import delete_collection_category  # noqa: E402,F401,I001
from .store.collection import delete_collection_item  # noqa: E402,F401,I001
from .store.collection import get_calc_cache  # noqa: E402,F401,I001
from .store.collection import get_calc_queue  # noqa: E402,F401,I001
from .store.collection import get_collection_id_by_hash  # noqa: E402,F401,I001
from .store.collection import get_collection_item  # noqa: E402,F401,I001
from .store.collection import list_calc_queue  # noqa: E402,F401,I001
from .store.collection import list_collection  # noqa: E402,F401,I001
from .store.collection import list_collection_by_category  # noqa: E402,F401,I001
from .store.collection import list_collection_categories  # noqa: E402,F401,I001
from .store.collection import list_favorite_ids  # noqa: E402,F401,I001
from .store.collection import list_favorites  # noqa: E402,F401,I001
from .store.collection import put_calc_cache  # noqa: E402,F401,I001
from .store.collection import random_collection_items  # noqa: E402,F401,I001
from .store.collection import recommend_page  # noqa: E402,F401,I001
from .store.collection import rename_collection_category  # noqa: E402,F401,I001
from .store.collection import resolve_calc_queue  # noqa: E402,F401,I001
from .store.collection import search_collection  # noqa: E402,F401,I001
from .store.collection import set_collection_category  # noqa: E402,F401,I001
from .store.collection import set_collection_category_sort  # noqa: E402,F401,I001
from .store.collection import set_collection_label  # noqa: E402,F401,I001
from .store.collection import set_collection_photo  # noqa: E402,F401,I001
from .store.collection import set_favorite  # noqa: E402,F401,I001
from .store.collection import upsert_collection_category  # noqa: E402,F401,I001
from .store.collection import user_fav_categories  # noqa: E402,F401,I001


# Клиент как личность (аватарка профиля, личные настройки — режим подтверждения цены
# и доставка по умолчанию, профиль v2 с просмотренными достижениями и лентой XP, сырьё
# геймификации, серверная копия корзины и нуджи по брошенной корзине/расчёту,
# сохранённые модели и идеи) уехал в app/store/profile.py (волна 5 разреза db).
# Туда же втянуто count_referrals: его зовёт голым именем user_profile_stats.
# ЭТА ФАСАДНАЯ СТРОКА count_referrals ОБЯЗАНА ДОЖИТЬ ДО ВОЛНЫ 6 — остающаяся здесь
# customer_card зовёт его голым именем, как и остальной блок рефералки.
from .store import profile  # noqa: E402,F401,I001
from .store.profile import PRICE_MODES  # noqa: E402,F401,I001
from .store.profile import PRICE_PCT_MAX  # noqa: E402,F401,I001
from .store.profile import _PREFS_DEFAULT  # noqa: E402,F401,I001
from .store.profile import _to_saved  # noqa: E402,F401,I001
from .store.profile import abandoned_calcs  # noqa: E402,F401,I001
from .store.profile import add_idea  # noqa: E402,F401,I001
from .store.profile import clear_user_cart  # noqa: E402,F401,I001
from .store.profile import count_referrals  # noqa: E402,F401,I001
from .store.profile import count_saved_models  # noqa: E402,F401,I001
from .store.profile import delete_saved_model  # noqa: E402,F401,I001
from .store.profile import get_saved_model  # noqa: E402,F401,I001
from .store.profile import get_saved_model_by_hash  # noqa: E402,F401,I001
from .store.profile import get_user_avatar  # noqa: E402,F401,I001
from .store.profile import get_user_cart  # noqa: E402,F401,I001
from .store.profile import get_user_prefs  # noqa: E402,F401,I001
from .store.profile import list_saved_models  # noqa: E402,F401,I001
from .store.profile import mark_achievements_seen  # noqa: E402,F401,I001
from .store.profile import mark_calc_nudged  # noqa: E402,F401,I001
from .store.profile import mark_cart_nudged  # noqa: E402,F401,I001
from .store.profile import price_auto_accept  # noqa: E402,F401,I001
from .store.profile import save_user_cart  # noqa: E402,F401,I001
from .store.profile import seen_achievements  # noqa: E402,F401,I001
from .store.profile import set_user_avatar  # noqa: E402,F401,I001
from .store.profile import set_user_delivery_prefs  # noqa: E402,F401,I001
from .store.profile import set_user_price_prefs  # noqa: E402,F401,I001
from .store.profile import stale_carts  # noqa: E402,F401,I001
from .store.profile import upsert_saved_model  # noqa: E402,F401,I001
from .store.profile import user_profile_stats  # noqa: E402,F401,I001
from .store.profile import xp_events  # noqa: E402,F401,I001


# Переписка мастер<->клиент (order_messages: треды заказов и вне заказа, цитаты-реплаи,
# отметки прочтения обеих сторон, поиск, счётчики непрочитанного) вместе с картой
# «сообщение переписки <-> сообщение в Telegram» (tg_chat_msgs) уехала в
# app/store/chat.py (волна 5 разреза db). Две секции режутся только вместе:
# tg_message_link читает обе таблицы одним JOIN-ом, prune_tg_links чистит карту
# по возрасту строк переписки.
from .store import chat  # noqa: E402,F401,I001
from .store.chat import _MSG_COLS  # noqa: E402,F401,I001
from .store.chat import _msg_row  # noqa: E402,F401,I001
from .store.chat import _msg_user_id  # noqa: E402,F401,I001
from .store.chat import add_client_message  # noqa: E402,F401,I001
from .store.chat import add_order_message  # noqa: E402,F401,I001
from .store.chat import clients_unread_admin  # noqa: E402,F401,I001
from .store.chat import get_message  # noqa: E402,F401,I001
from .store.chat import link_tg_message  # noqa: E402,F401,I001
from .store.chat import list_client_messages  # noqa: E402,F401,I001
from .store.chat import list_order_messages  # noqa: E402,F401,I001
from .store.chat import mark_client_read  # noqa: E402,F401,I001
from .store.chat import mark_order_read  # noqa: E402,F401,I001
from .store.chat import orders_unread_admin  # noqa: E402,F401,I001
from .store.chat import orders_unread_user  # noqa: E402,F401,I001
from .store.chat import prune_tg_links  # noqa: E402,F401,I001
from .store.chat import search_messages  # noqa: E402,F401,I001
from .store.chat import set_message_image  # noqa: E402,F401,I001
from .store.chat import tg_message_link  # noqa: E402,F401,I001


# CRM мастера — клиент как объект (агрегаты LTV/дат из requests + ручные поля customers,
# вычисляемые сегменты, карточка клиента), задачи-напоминания мастера, лента активности
# пользователей и клиентский маркетинг (промокоды + рефералка с атомарным захватом права
# на выплату) — уехал в app/store/crm.py (волна 6 разреза db). Блок промо/рефералки лежал
# ниже по файлу под чужой шапкой «Центр уведомлений админа» — доменно он здесь.
from .store import crm  # noqa: E402,F401,I001
from .store.crm import CALC_SOURCE_LABELS  # noqa: E402,F401,I001
from .store.crm import CRM_SEGMENT_LABELS  # noqa: E402,F401,I001
from .store.crm import _CRM_AGG_SQL  # noqa: E402,F401,I001
from .store.crm import _crm_manual_maps  # noqa: E402,F401,I001
from .store.crm import _crm_row  # noqa: E402,F401,I001
from .store.crm import add_calc_snapshot  # noqa: E402,F401,I001
from .store.crm import add_crm_task  # noqa: E402,F401,I001
from .store.crm import add_promo  # noqa: E402,F401,I001
from .store.crm import add_referral  # noqa: E402,F401,I001
from .store.crm import award_referral_if_due  # noqa: E402,F401,I001
from .store.crm import calc_next_request  # noqa: E402,F401,I001
from .store.crm import crm_broadcast_targets  # noqa: E402,F401,I001
from .store.crm import crm_dashboard  # noqa: E402,F401,I001
from .store.crm import crm_segments  # noqa: E402,F401,I001
from .store.crm import crm_tasks_due  # noqa: E402,F401,I001
from .store.crm import customer_card  # noqa: E402,F401,I001
from .store.crm import delete_crm_task  # noqa: E402,F401,I001
from .store.crm import delete_promo  # noqa: E402,F401,I001
from .store.crm import ensure_birthday_tasks  # noqa: E402,F401,I001
from .store.crm import get_calc_snapshot  # noqa: E402,F401,I001
from .store.crm import get_customer  # noqa: E402,F401,I001
from .store.crm import get_promo  # noqa: E402,F401,I001
from .store.crm import get_referral  # noqa: E402,F401,I001
from .store.crm import get_user_last_color  # noqa: E402,F401,I001
from .store.crm import list_activity  # noqa: E402,F401,I001
from .store.crm import list_crm_tasks  # noqa: E402,F401,I001
from .store.crm import list_customers_crm  # noqa: E402,F401,I001
from .store.crm import list_promos  # noqa: E402,F401,I001
from .store.crm import log_event  # noqa: E402,F401,I001
from .store.crm import mark_calc_refined  # noqa: E402,F401,I001
from .store.crm import mark_crm_task_notified  # noqa: E402,F401,I001
from .store.crm import redeem_promo  # noqa: E402,F401,I001
from .store.crm import set_crm_task_done  # noqa: E402,F401,I001
from .store.crm import set_user_last_color  # noqa: E402,F401,I001
from .store.crm import upsert_customer  # noqa: E402,F401,I001
from .store.crm import validate_promo  # noqa: E402,F401,I001


# Служебные журналы студии для админки — usage-агрегат обращений к эндпоинтам (замена
# ротируемым access-логам), журнал резервных копий, телеметрия ошибок и центр уведомлений
# админа — уехали в app/store/ops.py (волна 6 разреза db). Все четыре пишутся служебным
# кодом (app/backup.py, app/errorlog.py, middleware веб-слоя) и никогда не роняют вызвавшего.
from .store import ops  # noqa: E402,F401,I001
from .store.ops import _BACKUP_RUNS_KEEP  # noqa: E402,F401,I001
from .store.ops import add_admin_notification  # noqa: E402,F401,I001
from .store.ops import add_backup_run  # noqa: E402,F401,I001
from .store.ops import bump_endpoint_hit  # noqa: E402,F401,I001
from .store.ops import count_new_admin_notifications  # noqa: E402,F401,I001
from .store.ops import count_new_errors  # noqa: E402,F401,I001
from .store.ops import delete_admin_notification  # noqa: E402,F401,I001
from .store.ops import get_admin_notification  # noqa: E402,F401,I001
from .store.ops import last_backup_run  # noqa: E402,F401,I001
from .store.ops import list_admin_notifications  # noqa: E402,F401,I001
from .store.ops import list_backup_runs  # noqa: E402,F401,I001
from .store.ops import list_errors  # noqa: E402,F401,I001
from .store.ops import mark_errors_seen  # noqa: E402,F401,I001
from .store.ops import purge_old_errors  # noqa: E402,F401,I001
from .store.ops import try_mark_published  # noqa: E402,F401,I001
from .store.ops import update_admin_notification  # noqa: E402,F401,I001
from .store.ops import upsert_error  # noqa: E402,F401,I001
from .store.ops import usage_stats  # noqa: E402,F401,I001


# Команда мастерской — роли сотрудников (таблица staff, A4) и личный вход в настольную
# админку (desk_sessions + одноразовые desk_login_codes; хранится только sha256, логика
# в app/desklogin.py) — уехала в app/store/team.py (волна 6 разреза db).
from .store import team  # noqa: E402,F401,I001
from .store.team import add_desk_code  # noqa: E402,F401,I001
from .store.team import add_desk_session  # noqa: E402,F401,I001
from .store.team import drop_desk_codes  # noqa: E402,F401,I001
from .store.team import drop_desk_session  # noqa: E402,F401,I001
from .store.team import drop_desk_sessions_of  # noqa: E402,F401,I001
from .store.team import get_staff  # noqa: E402,F401,I001
from .store.team import list_desk_sessions  # noqa: E402,F401,I001
from .store.team import list_staff  # noqa: E402,F401,I001
from .store.team import prune_desk_auth  # noqa: E402,F401,I001
from .store.team import remove_staff  # noqa: E402,F401,I001
from .store.team import set_staff  # noqa: E402,F401,I001
from .store.team import take_desk_code  # noqa: E402,F401,I001
from .store.team import touch_desk_session  # noqa: E402,F401,I001


# Готовые изделия / витрина (bd896aa6) — карточки products и покупка со списанием наличия
# условием `stock >= qty` прямо в UPDATE — уехали в app/store/products.py
# (волна 6 разреза db).
from .store import products  # noqa: E402,F401,I001
from .store.products import _to_product  # noqa: E402,F401,I001
from .store.products import add_product  # noqa: E402,F401,I001
from .store.products import buy_product  # noqa: E402,F401,I001
from .store.products import count_products  # noqa: E402,F401,I001
from .store.products import delete_product  # noqa: E402,F401,I001
from .store.products import get_product  # noqa: E402,F401,I001
from .store.products import list_products  # noqa: E402,F401,I001
from .store.products import update_product  # noqa: E402,F401,I001


# Калибровка целиком — кампании и их шаги, замеры, образцы с фото, паспорта и снимки
# пресетов слайсера — уехала в app/store/calib.py (волна 6 разреза db). Имя модуля
# `calib`, а не `calibration`: последнее занято top-level импортом app/calibration.py.
# Четыре имени моста журнал<->калибровка уехали ещё волной 3 в app/store/printjournal.py,
# оттуда же store/calib.py импортирует _calib_step_row — долг волны 3 закрыт.
from .store import calib  # noqa: E402,F401,I001
from .store.calib import _passport_row  # noqa: E402,F401,I001
from .store.calib import add_calib_measure  # noqa: E402,F401,I001
from .store.calib import add_calib_run  # noqa: E402,F401,I001
from .store.calib import calib_overview  # noqa: E402,F401,I001
from .store.calib import delete_calib_measure  # noqa: E402,F401,I001
from .store.calib import delete_calib_passport  # noqa: E402,F401,I001
from .store.calib import delete_calib_run  # noqa: E402,F401,I001
from .store.calib import delete_calib_sample  # noqa: E402,F401,I001
from .store.calib import get_calib_passport  # noqa: E402,F401,I001
from .store.calib import get_calib_passport_by_id  # noqa: E402,F401,I001
from .store.calib import get_calib_preset_snapshot  # noqa: E402,F401,I001
from .store.calib import get_calib_run  # noqa: E402,F401,I001
from .store.calib import get_calib_sample  # noqa: E402,F401,I001
from .store.calib import list_calib_measures  # noqa: E402,F401,I001
from .store.calib import list_calib_passports  # noqa: E402,F401,I001
from .store.calib import list_calib_preset_history  # noqa: E402,F401,I001
from .store.calib import list_calib_preset_latest  # noqa: E402,F401,I001
from .store.calib import list_calib_runs  # noqa: E402,F401,I001
from .store.calib import list_calib_samples  # noqa: E402,F401,I001
from .store.calib import list_calib_steps  # noqa: E402,F401,I001
from .store.calib import save_calib_passport  # noqa: E402,F401,I001
from .store.calib import save_calib_sample  # noqa: E402,F401,I001
from .store.calib import seed_calib_steps  # noqa: E402,F401,I001
from .store.calib import set_calib_sample_photo  # noqa: E402,F401,I001
from .store.calib import snapshot_calib_presets  # noqa: E402,F401,I001
from .store.calib import update_calib_run  # noqa: E402,F401,I001


# База знаний владельца студии (OpenTodo c356387a) — статьи kb_articles с черновиками и
# ручной сортировкой — уехала в app/store/kb.py (волна 6 разреза db).
from .store import kb  # noqa: E402,F401,I001
from .store.kb import _now_txt  # noqa: E402,F401,I001
from .store.kb import kb_delete  # noqa: E402,F401,I001
from .store.kb import kb_get  # noqa: E402,F401,I001
from .store.kb import kb_list  # noqa: E402,F401,I001
from .store.kb import kb_save  # noqa: E402,F401,I001


# Предсказание времени печати (копилка замеров print_samples, коррекция оценки по медиане
# факт/оценка из print_journal, сводка собранных данных) уехало в app/store/timepred.py
# (волна 7 разреза db). Это ПОТРЕБИТЕЛЬ журнала печатей, а не его часть: record_time_sample
# зовёт add_print_journal, обратных вызовов нет (ответ на открытый вопрос 2 плана).
# ЛОВУШКА: predict_minutes зовёт сама себя, bigdata_stats зовёт predict_minutes, а
# record_time_sample — add_print_journal, все ГОЛЫМИ именами; подмена db.<имя> до этих
# внутренних вызовов не достаёт — патчить app.store.timepred.
from .store import timepred  # noqa: E402,F401,I001
from .store.timepred import add_print_sample  # noqa: E402,F401,I001
from .store.timepred import bigdata_stats  # noqa: E402,F401,I001
from .store.timepred import list_print_samples  # noqa: E402,F401,I001
from .store.timepred import predict_minutes  # noqa: E402,F401,I001
from .store.timepred import print_sample_count  # noqa: E402,F401,I001
from .store.timepred import printer_time_stats  # noqa: E402,F401,I001
from .store.timepred import record_time_sample  # noqa: E402,F401,I001


# Производные цифры, у которых НЕТ своей таблицы (сроки исполнения — parse_due_date/
# eta_date/promised_date — и сводка админ-дашборда dashboard_stats), уехали в
# app/store/analytics.py (волна 7 разреза db). Объединяющий признак — ни одна из четырёх
# функций не владеет таблицей, а три из них вообще не трогают соединение.
from .store import analytics  # noqa: E402,F401,I001
from .store.analytics import dashboard_stats  # noqa: E402,F401,I001
from .store.analytics import eta_date  # noqa: E402,F401,I001
from .store.analytics import parse_due_date  # noqa: E402,F401,I001
from .store.analytics import promised_date  # noqa: E402,F401,I001


# Очередь досчёта цены (Э0, docs/Перспективные разработки/vps-relay-2026-10-04.md §1): заказ
# без цены переживает рестарт — задание deferred_calc и флаг requests.price_pending; захват,
# применение и отказ — условными UPDATE одним commit-ом. Здесь же дедуп входящих приёмной
# (Э1): tg_seen и relay_events_seen. Модуль новый, не вырез волны: группа стоит в хвосте.
from .store import deferred  # noqa: E402,F401,I001
from .store.deferred import apply_deferred_result  # noqa: E402,F401,I001
from .store.deferred import apply_live_price  # noqa: E402,F401,I001
from .store.deferred import cancel_deferred_for_request  # noqa: E402,F401,I001
from .store.deferred import claim_deferred  # noqa: E402,F401,I001
from .store.deferred import claim_next_deferred  # noqa: E402,F401,I001
from .store.deferred import deferred_by_client_req  # noqa: E402,F401,I001
from .store.deferred import deferred_counts  # noqa: E402,F401,I001
from .store.deferred import deferred_for_request  # noqa: E402,F401,I001
from .store.deferred import enqueue_deferred  # noqa: E402,F401,I001
from .store.deferred import fail_deferred  # noqa: E402,F401,I001
from .store.deferred import get_deferred  # noqa: E402,F401,I001
from .store.deferred import next_due_deferred  # noqa: E402,F401,I001
from .store.deferred import prune_tg_seen  # noqa: E402,F401,I001
from .store.deferred import relay_event_seen_mark  # noqa: E402,F401,I001
from .store.deferred import relay_event_was_seen  # noqa: E402,F401,I001
from .store.deferred import release_claim  # noqa: E402,F401,I001
from .store.deferred import release_deferred_hold  # noqa: E402,F401,I001
from .store.deferred import requeue_running  # noqa: E402,F401,I001
from .store.deferred import retry_deferred  # noqa: E402,F401,I001
from .store.deferred import tg_seen_mark  # noqa: E402,F401,I001
