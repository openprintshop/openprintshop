# Скачивание таймлапсов с Centauri Carbon 2 (разведано 20.07.2026, дополнено 24.07.2026)

Долго «таймлапсы не качались». Причин оказалось три, и все неочевидные.

## ГЛАВНОЕ дополнение 24.07 (задача 4c6cd9e8): mp4 надо СНАЧАЛА СОБРАТЬ

У задания в истории (1036) есть поле `time_lapse_video_status` (enum из lan_service_web
ElegooSlicer): **0** = кадры не снимались, **1** = кадры есть, но mp4 ЕЩЁ НЕ СОБРАН,
**2** = mp4 готов и скачивается, **3** = сборка провалилась.

Пока статус 1, файла НЕ СУЩЕСТВУЕТ ни под каким именем — /download молчит до таймаута
(симптом «неверного пути» из граблей №2 ниже). Слайсер в этом случае шлёт **метод 1051**
(«GetTimeLapseVideoList» — имя обманчивое, это КОМАНДА СБОРКИ):

```json
{"id": N, "method": 1051, "params": {"url": "<time_lapse_video_url задания как есть>"}}
```

Принтер рендерит ролик (минуты; слайсер ждёт ответ до 180 с) и отвечает готовым путём для
/download (`result.url`); телеметрия при этом проходит subStatus 3020 «сборка» → 3021 «готово»
(3022 — сбой). Скачивать готовое имя — ВЕРБАТИМ, без переписывания префикса (слайсер качает
`TimeLapseVideoUrl` как есть только при статусе 2). Реализация: `elegoo.generate_timelapse` +
свип `printwatch._tick_timelapse_sweep` (по одной сборке за свип, только в простое принтера).

Именно из-за несобранных роликов журнал 21-23.07 оставался «без таймлапса, хотя они есть»:
бот качал `video/<имя>.mp4`, которого ещё нет, и получал вечное молчание.

## Рабочий запрос

```
GET http://{IP}/download?X-Token={ELEGOO_ACCESS_CODE}&file_name={urlencode("video/ИМЯ.mp4")}
```

Пример (проверено живьём, 200 OK, 4 839 487 байт — совпадает с ручной выгрузкой):

```
http://192.168.1.50/download?X-Token=4ijsD1&file_name=video%2FECC2_0.4_Body1_80%20Elegoo%20PETG%20_0.2_1h38m.gcode20260720215354.mp4
```

`X-Token` — это LAN access code принтера (у нас уже есть в `.env` как `ELEGOO_ACCESS_CODE`),
ничего дополнительно добывать не нужно.

## Правило: из истории (MQTT 1036) → в URL

```python
val  = task["time_lapse_video_url"]      # "picture/ИМЯ"  ЛИБО  "video/ИМЯ.mp4"
name = val.split("/", 1)[1]              # отбрасываем префикс до первого "/"
if not name.endswith(".mp4"):
    name += ".mp4"
file_name = "video/" + name              # префикс ВСЕГДА video/, .mp4 ВСЕГДА
```

**Главная ловушка:** префикс `picture/` в ответе 1036 не означает «видео не записано» —
тот же ролик прекрасно лежит в `video/` и скачивается. Пустое поле `time_lapse_video_url` —
вот это и есть «видео нет». Реализация: `webapp._timelapse_file_name/_timelapse_abs_url`.

## Три грабли реализации

1. **Ответ нарушает HTTP-спеку.** Принтер (сервер `libhv/1.3.4`) отдаёт ОДНОВРЕМЕННО
   `Content-Length` и `Transfer-Encoding: chunked`. aiohttp такой ответ отвергает
   (`400 Transfer-Encoding can't be present with Content-Length`) — именно поэтому скачивание
   не работало вообще. urllib и браузер читают нормально → качаем через
   `urllib.request` в `asyncio.to_thread` (`webapp._http_get_bytes`).
2. **Неверный токен или путь = вечное молчание.** Не 401 и не 404: соединение принимается,
   ответ не приходит никогда. Таймаут обязателен, иначе воркер повиснет.
3. **Прокси из окружения.** На машине заданы `HTTP(S)_PROXY` на VPN-прокси, через который
   принтер в локалке недостижим. Нужен `ProxyHandler({})` (urllib) / `trust_env=False` (aiohttp).

Прочее: `HEAD` не поддерживается (404), `Range` игнорируется — файл только целиком.
`Content-Disposition` приходит как `filename="video/ИМЯ.mp4"`, браузер санитизирует `/` в `_` —
отсюда имя `video_ИМЯ.mp4` у файлов, выгруженных вручную.

## Веб-интерфейса на принтере нет

Полный скан портов 1–65535: открыты только **53, 80, 1883, 8080, 9001**. Порт 80 (`libhv`)
отвечает 404 на всё, кроме `/download`; 8080 — MJPEG-камера (отдаёт поток на любой путь);
9001 — MQTT-over-WebSocket. «Веб-интерфейс», из которого файл выгружается вручную, — это
локальная страница внутри ElegooSlicer (`resources/plugins/elegoolink/web/lan_service_web/`),
открываемая в его WebView2, а не сервер на принтере.

Источники: [elegoo-homeassistant CC2_PROTOCOL.md](https://github.com/danielcherubini/elegoo-homeassistant/blob/main/docs/CC2_PROTOCOL.md)
(оттуда имя параметра `file_name` и роут `/download` с `X-Token`), [runnane/elegoo-web](https://github.com/runnane/elegoo-web);
точные значения подтверждены дебаг-логами ElegooSlicer (`%APPDATA%\ElegooSlicer\log\debug_*.log.0`).
