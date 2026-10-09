# OpenPrintShop

**Открытая система приёма заказов для студий 3D-печати.** Клиент присылает модель вашему
Telegram-боту и за секунды получает цену, посчитанную по настоящему слайсеру. Вы ведёте заказы,
клиентов, катушки и принтеры в настольной админке на ПК студии.

[English version → README.md](README.md)

## Что умеет

**Клиенту — в Telegram (бот и Mini App):**

- прислать файл модели (`.stl`, `.3mf`, `.obj`, `.step`, `.zip`) или ссылку на Printables,
  Thingiverse или прямой файл;
- увидеть 3D-превью и получить цену за секунды, а потом уточнить: пластик, цвет, качество,
  прочность, тираж, срочность;
- корзина и заказы со статусами, чат со студией, онлайн-оплата через Telegram Payments
  (провайдер подключает сама студия), доставка с трекингом, PDF-смета;
- витрина готовых изделий и конфигуратор органайзеров gridfinity.

**Студии — настольная админка и админ-экраны в Mini App:**

- очередь заказов и печати, дашборд выручки, CRM: история клиента, сегменты, рассылки, задачи;
- своя формула цены: пластик, машинное время, подготовка, буфер на брак, наценка, скидки за
  тираж, минимальная цена;
- учёт катушек с тарой и журналом движения, база калибровок с QR-паспортами;
- управление принтерами и журнал печати: Elegoo Centauri Carbon 2 (глубже всего — статус,
  таймлапсы, допечатка со слоя), Klipper/Moonraker, Bambu Lab по локальной сети (бета);
- роли владелец/оператор, white-label (своё имя, логотип, цвет), бэкапы с копией на другой
  носитель, самообновление из git, журнал ошибок.

**Дополнительно (по желанию):** конвертация STEP→STL (FreeCAD), параметрические модели
(OpenSCAD), дашборд на дисплей TURZX 8.8" по USB, аватарки ИИ (видеокарта NVIDIA), умный поиск
по каталогу, поиск моделей на Printables и Thingiverse.

## Как устроено

```
 Telegram ──► бот + веб-сервер (run.py, aiogram + aiohttp) ──► SQLite (data/)
   ▲              │                │
   │              │                └─► слайсер CLI (OrcaSlicer / ElegooSlicer / Bambu Studio)
   │              │                    → точные граммы и время; встроенная оценка даёт
   │              │                      мгновенную первую цену
   │              └─► принтеры в локальной сети (Elegoo MQTT/HTTP, Moonraker, Bambu)
   │
 Mini App ◄── публичный HTTPS-адрес: Cloudflare Tunnel (временный или постоянный) или свой прокси
```

Всё работает на одном постоянно включённом Windows-ПК студии; `watchdog.py` держит бота живым.

## Что нужно

- Windows 10/11 x64, ПК, который не выключается; от 8 ГБ памяти.
- Токен бота от [@BotFather](https://t.me/BotFather) и ваш Telegram ID
  ([@userinfobot](https://t.me/userinfobot)).
- Слайсер на том же ПК: OrcaSlicer (рекомендуется), ElegooSlicer или Bambu Studio.
- Интернет. Mini App нужен публичный HTTPS-адрес; по умолчанию бот сам поднимает бесплатный
  туннель Cloudflare (без аккаунта, но адрес меняется при перезапуске).

## Установка

**Одной командой** (PowerShell): скрипт при необходимости поставит Python и Git через winget,
склонирует репозиторий в `C:\OpenPrintShop\bot`, создаст окружение, спросит токен бота и ID
администраторов и поставит сторожа в автозапуск.

```powershell
irm https://raw.githubusercontent.com/openprintshop/openprintshop/main/tools/installer/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**MSI:** `tools/installer/build_msi.ps1` собирает установщик с мастером (WiX 5); внутри — тот же
скрипт.

**Вручную / для разработки:**

```powershell
git clone https://github.com/openprintshop/openprintshop.git
cd openprintshop
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env     # BOT_TOKEN и ADMIN_IDS; остальное описано в комментариях файла
.\.venv\Scripts\python run.py
```

Дальше напишите боту `/start`. На ПК студии настольная админка открывается по адресу
`http://127.0.0.1:8081/app/desk/`. Каждая настройка в `.env.example` прокомментирована, а
повседневные (цены, пластики, принтеры, бренд, оплата, доставка) меняются в админке.

**Обновления:** Админка → Статус → Обновления (вручную, с уведомлением или автоматически). Бот
подтягивает ветку `main` из своего git-репозитория и доустанавливает новые зависимости.

**Дополнительные модули** после установки: `tools\installer\modules.ps1 -Enable ai` или
`-Enable smartsearch`.

## Разработка

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest        # около 3 800 тестов, сеть не нужна
.\.venv\Scripts\python -m ruff check .
npm install && npm run build:desk         # только если меняли app/webui/desk/src
```

Структура: `run.py` (запуск), `app/` (обработчики бота, веб-сервер, расчёт цены, слайсинг,
драйверы принтеров в `app/printerdrv/`, хранилище в `app/store/`), `app/webui/` (Mini App и
настольная админка), `tools/` (установщик и утилиты), `tests/`.

## Участие, безопасность, лицензия

- Будем рады помощи — см. [CONTRIBUTING.md](CONTRIBUTING.md).
- Нашли уязвимость? Сообщите приватно — см. [SECURITY.md](SECURITY.md).
- OpenPrintShop распространяется по лицензии **GNU Affero General Public License v3.0**
  ([LICENSE](LICENSE)): если вы запускаете изменённую версию для других людей, вы обязаны дать
  им её исходный код. Сторонние компоненты и их лицензии —
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Сделано для настоящей небольшой студии печати и открыто для всех.
