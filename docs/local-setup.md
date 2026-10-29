# Локальная установка, запуск и тесты

Документ для разработчика: как поднять OpenPrintShop из исходников, запустить бота и прогнать
проверки. Боевая установка на ПК студии (Windows, `install.ps1`, MSI) описана в
[README.ru.md](../README.ru.md#установка).

## Что проверено, а что нет

Все команды в разделах «Linux / macOS» ниже реально выполнены в чистом контейнере
Linux x86_64 с Python 3.13.17, Node 22.22.0 и npm 10, на коммите `b806fc2`. Результаты
приведены как есть.

Не проверялось:

- **Windows** (основная платформа проекта). Команды для PowerShell взяты из README и
  CONTRIBUTING и здесь не запускались.
- **macOS**. Ожидается то же, что на Linux, но не проверялось.
- **Работа бота с настоящим токеном.** В контейнере не было доступа к `api.telegram.org`,
  поэтому бот запускался только до первого запроса к Telegram (подробности ниже). Настольную
  админку в браузере открыть не удалось по той же причине.
- **Docker.** В репозитории нет `Dockerfile` и `docker-compose.yml`, поэтому способа запуска
  через Docker нет.

## Что нужно

- Python 3.13 (так указано в CONTRIBUTING; проверено на 3.13.17).
- Git.
- Node.js и npm — только для сборки настольной админки (`app/webui/desk/src`). Для работы бота
  Node не нужен: готовый `app/webui/desk/desk.js` лежит в репозитории.
- Для запуска бота: токен от [@BotFather](https://t.me/BotFather) и ваш Telegram ID
  ([@userinfobot](https://t.me/userinfobot)).
- Для расчёта цены по слайсеру: OrcaSlicer, ElegooSlicer или Bambu Studio. Без слайсера бот
  запускается и пишет в лог предупреждение «Слайсер не найден … расчёт работать не будет».

## Установка

### Linux / macOS

```bash
git clone https://github.com/openprintshop/openprintshop.git
cd openprintshop
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

`requirements-dev.txt` нужен только для тестов и линтера (pytest, pytest-asyncio, ruff, mypy).

### Windows (PowerShell, не проверялось в этом документе)

```powershell
git clone https://github.com/openprintshop/openprintshop.git
cd openprintshop
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
```

## Настройка

```bash
cp .env.example .env
```

(на Windows — `copy .env.example .env`). Каждая настройка описана комментарием в
`.env.example`. Минимум для запуска:

| Переменная | Что это |
|---|---|
| `BOT_TOKEN` | токен бота от @BotFather |
| `ADMIN_IDS` | Telegram ID владельцев через запятую |

Что ещё стоит поправить при запуске не на Windows:

- `SLICER_PATH`, `PROFILES_DIR` — в примере стоят пути `C:\Program Files\ElegooSlicer\…`.
  Укажите путь к слайсеру на своей машине или оставьте как есть, если расчёт по слайсеру не
  нужен.
- `TUNNEL_MODE` — по умолчанию `quick`. По коду (`app/tunnel.py`) в этом режиме скачивается
  `cloudflared-windows-amd64.exe`, то есть на Linux и macOS он работать не будет (не
  проверялось: в контейнере не было выхода в интернет). Для локальной разработки ставьте
  `TUNNEL_MODE=none`; мини-приложение в Telegram тогда не откроется, пока не задан
  `WEBAPP_PUBLIC_URL` с публичным HTTPS-адресом.
- `DATA_DIR` — куда бот складывает базу, кэши и логи. По умолчанию `data/` в папке проекта
  (она в `.gitignore`). Чтобы не смешивать данные разработки с проектом, раскомментируйте
  строку и задайте абсолютный путь.

## Запуск

```bash
.venv/bin/python run.py
```

(на Windows — `.\.venv\Scripts\python run.py`). После запуска отправьте боту `/start`.
Настольная админка на этой же машине: `http://127.0.0.1:8081/app/desk/` (порт меняется
переменной `WEBAPP_PORT`).

Что было в контейнере с токеном-заглушкой из `.env.example`, `TUNNEL_MODE=none` и `DATA_DIR`
во временной папке: бот прочитал настройки, создал базу и поднял веб-сервер:

```
INFO root: Канал выпуска: STABLE · b806fc2
WARNING root: Слайсер не найден по пути C:\Program Files\ElegooSlicer\elegoo-slicer.exe — расчёт работать не будет.
INFO app.webapp: Мини-апка слушает 127.0.0.1:8081
ERROR app.tunnel: Вход снаружи не настроен: TUNNEL_MODE=none, но WEBAPP_PUBLIC_URL не задан — адреса снаружи нет вовсе
```

Затем процесс завершился с `aiogram.exceptions.TelegramNetworkError` на запросе к
`api.telegram.org`: в контейнере этот адрес недоступен. То есть на старте бот сразу обращается
к Telegram, и без связи с ним (или с неверным токеном) работать не будет. Дальше бота в этом
окружении проверить не удалось.

На ПК студии бота держит запущенным `watchdog.py`; для разработки он не нужен.

## Тесты и проверки

### Python-тесты

```bash
.venv/bin/python -m pytest
```

Сеть тестам не нужна. Тест `tests/test_network_slice.py` (маркер `network`) по умолчанию
пропускается; он включается переменной `RUN_NETWORK_TESTS=1` и требует сети и слайсера (не
запускался).

**Тестам нужен `.env`.** Часть тестов берёт `BOT_TOKEN` и `ADMIN_IDS` из настроек. Без `.env`
(и без этих переменных в окружении) в контейнере упало 30 тестов: 3605 прошли, 21 пропущен.
С `.env`, скопированным из `.env.example` без правок, результат такой:

```
8 failed, 3627 passed, 21 skipped in 130.87s
```

Прогон был после `npm ci`. Эти 8 тестов падают на Linux (на macOS, скорее всего, тоже), и причины известны:

| Тест | Причина |
|---|---|
| `test_bambu3mf.py::test_sanitize_uses_db_resolver_profiles` | путь Windows (`C:\x\…`): на Linux не разбирается как путь |
| `test_datapaths_c2.py::test_abs_data_reroots_foreign_absolute_path_when_file_exists` | пути Windows (`D:\…`) |
| `test_datapaths_c2.py::test_vision_tagger_reads_relative_paths` | пути Windows (`C:\…`) |
| `test_profilepaths.py::test_reroots_absolute_path_from_another_machine` | пути Windows (`D:\…`) |
| `test_selfcheck_engines.py::test_existing_engine_is_quiet` | слайсер ищется по `C:\Program Files\…` |
| `test_selfcheck_engines.py::test_default_engine_not_duplicated` | то же |
| `test_desk_js_suite.py::test_desk_js_suite_passes` | запускает `app/webui/desk/tests/run.mjs`, см. ниже. Пропускается, если нет `node` или `node_modules/@esbuild` (то есть до `npm ci`) |
| `test_geometry.py::test_subtract_distinct_cutter` | trimesh не находит движок булевых операций |

Последний тест проходит, если доустановить `manifold3d` (его нет в `requirements.txt`):

```bash
.venv/bin/pip install manifold3d
.venv/bin/python -m pytest tests/test_geometry.py   # 10 passed, 1 skipped
```

### Линтер

```bash
.venv/bin/python -m ruff check .
```

Результат в контейнере: `All checks passed!`.

### Настольная админка (Node)

```bash
npm ci
npm run build:desk
```

Сборка прошла, и пересобранный `app/webui/desk/desk.js` совпал с закоммиченным (`git status`
чистый). Если меняете `app/webui/desk/src`, коммитьте пересобранный `desk.js` вместе с
исходниками: тест проверяет, что бандл актуален.

`npm run test:desk` на Linux **не работает**: выходит с кодом 1 и ничего не выводит.
`app/webui/desk/tests/run.mjs` вызывает esbuild по жёстко прописанному пути
`node_modules/@esbuild/win32-x64/esbuild.exe`, а на Linux npm ставит другой пакет esbuild.
На Windows не проверялось.
