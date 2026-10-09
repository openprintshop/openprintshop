# Захваты MQTT-обмена с принтером (Centauri Carbon 2)

Сырые логи сниффинга брокера принтера (`<ip>:1883`, подписка `elegoo/#`). Раньше жили во
временных папках сессий Claude и пропадали при чистке — теперь здесь. Методика сниффинга
описана в `docs/Уже в продукте/printer-integration.md`, разбор ключей — в
`inventory_master_plan.md` §5.1.1 и `printer-actions-audit.md`.

## Файлы

| Файл | Что это |
|---|---|
| `mqtt-capture-2026-07-12-full.log` | **Главный захват 12.07** (100 с): PING/PONG heartbeat, полные ответы 1036 (история), 2005 (слоты), 1044 (файлы, local + u-disk err 1017), 1002 (полный статус), 1001 (инфо/прошивка), 1042 (URL камеры) + сводка кадров |
| `mqtt-capture-2026-07-12-partial.log` | Первый прогон того же сниффера: пачка запросов ElegooSlicer при входе на экран Device; упал на cp1251-юникоде (иероглифы в имени файла) — потому и «partial» |
| `mqtt-harvest-check-2026-07-12.log` | Проверка пассивного сбора: 18 api_response за 90 с → harvest файлов=20, истории=44 |
| `broadcast-idle-2026-07-20.json` | Снимок кэша фонового подписчика бота в ПРОСТОЕ: вещание api_status несёт temps + **canvas_info (слоты)** + **ztemperature_sensor (камера)** без всякой привязки |
| `mqtt-register-binding-2026-07-20.log` | **РАЗВЕДКА ПРИВЯЗКИ (751 строка):** полный payload `api_register`→`register_response` + все команды ElegooSlicer с ответами `error_code:0` — свет 1029, вентиляторы 1030, home 1026, jog 1027. На основе этого лога бот сам стал «привязанным» клиентом |

## ⭐ Привязка РАЗВЕДАНА (20.07.2026) — бот теперь полноценный клиент

Из `mqtt-register-binding-2026-07-20.log`. Механизм «привязки», из-за которого принтер
отвечал unicast только слайсеру:

```
→  elegoo/{SN}/api_register              {"request_id":"0cliaa2497","client_id":"0cliaa2497"}
←  elegoo/{SN}/0cliaa2497/register_response   {"client_id":"0cliaa2497","error":"ok"}
```

После этого клиент шлёт запросы в `elegoo/{SN}/{client_id}/api_request`, принтер отвечает
unicast в `.../api_response`. Держится heartbeat-PING `{"type":"PING"}` на своём канале ~раз в
8–10 с. Повторный `api_register` тем же `client_id` → `"error":"already registered"` (тоже успех).

**Проверено живьём (probe + бот):** после регистрации принтер ответил боту на 1044/1002/2005/
1036, а команда 1029 (свет) вернула `error_code:0` — подтверждённый успех. Реализовано в
`app/elegoo.py` (CHANNEL_ID, `_register`, `_bound`, привязанный `request()`).

**Коды команд из этого захвата (ответ `error_code:0` на каждый):**

| Действие | method | params |
|---|---|---|
| Свет вкл/выкл | 1029 | `{"power":1\|0}` (метод НЕ менялся с 04.06) |
| Вентилятор | 1030 | `{"fan"\|"aux_fan"\|"box_fan": 0..255}` (шкала 0–255, НЕ доля!) |
| Home осей | 1026 | `{"homed_axes":"xyz"}` |
| Перемещение оси (jog) | 1027 | `{"axes":"z"\|"x"\|"y","distance":±N}` |

> Замечание по вентиляторам: команда 1030 — шкала 0–255, а в телеметрии `fans.*.speed`
> пользовательские (model/aux/box) приходят долей 0..1, а системные (controller/heater) — в
> 0..255. `elegoo._fan_pct` нормализует обе (порог 1.5).

## Ключевые факты из захватов

- Формат: JSON-RPC поверх MQTT, топики `elegoo/{SN}/{клиент}/api_request|api_response`,
  вещание `.../api_status` (method 6000, ~раз в 7 с).
- ElegooSlicer держит постоянный канал `1_PC_2769` (PING/PONG каждые ~10 с) и на каждый вход
  в экран принтера создаёт одноразовый канал `0cli<hex>` для пачки запросов.
- **`api_register` → `register_response`** — «привязка» клиента. 12.07 был виден только в
  сводке кадров (payload утерян); 20.07 payload пойман целиком (см. раздел ⭐ выше) и встроен
  в бота — теперь бот полноценный клиент, не зависит от открытого слайсера.
- Вещание в простое (20.07): `extruder.temperature`, `heater_bed.temperature`,
  `ztemperature_sensor.temperature`, `canvas_info.canvas_list[].tray_list[]` (бренд/тип/цвет/
  температуры по слотам). Остальное (`print_status`, `fans`, `gcode_move`, `led`,
  `machine_status`) прилетает дельтами по ходу печати/изменений — см. полную структуру в
  ответе 1002 внутри full-лога.
