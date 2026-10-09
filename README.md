# OpenPrintShop

**An open-source order desk for 3D printing studios.** Customers send a model to your Telegram
bot and get an instant, slicer-accurate price; you run orders, customers, filament spools and
printers from a desktop admin on the studio PC.

[Русская версия → README.ru.md](README.ru.md)

> **Language:** the user interface is in Russian for now; most code comments and docs are in
> Russian too. Translations are very welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## What it does

**For customers, inside Telegram (bot + Mini App):**

- send a model file (`.stl`, `.3mf`, `.obj`, `.step`, `.zip`) or a link to Printables,
  Thingiverse or a direct file;
- see a 3D preview and get a price in seconds, then refine it: plastic, colour, quality,
  strength, quantity, urgency;
- cart and orders with statuses, chat with the studio, online payment through Telegram Payments
  (with the studio's own payment provider), delivery options and tracking, a PDF quote;
- a catalogue of ready-made products and a gridfinity organizer configurator.

**For the studio (desktop admin + admin screens in the Mini App):**

- order pipeline and print queue, revenue dashboard, CRM with customer history, segments,
  broadcasts and tasks;
- price formula you control: material, machine time, setup, failure buffer, markup,
  quantity discounts, minimum price;
- spool inventory with tare weights and a stock ledger, calibration notes with QR passports;
- printer control and print journal: Elegoo Centauri Carbon 2 (deepest integration —
  status, timelapses, resume from a layer), Klipper/Moonraker, Bambu Lab LAN (experimental);
- owner/operator roles, white-label branding (your name, logo and colour), backups including
  an off-site copy, self-update from git, error log.

**Optional modules:** STEP→STL conversion (FreeCAD), parametric models (OpenSCAD), a status
dashboard for a TURZX 8.8" USB display, AI avatars (NVIDIA GPU), semantic catalogue search,
model search across Printables and Thingiverse.

## How it works

```
 Telegram ──► bot + web server (run.py, aiogram + aiohttp) ──► SQLite (data/)
   ▲              │                │
   │              │                └─► slicer CLI (OrcaSlicer / ElegooSlicer / Bambu Studio)
   │              │                    → exact grams and print time; a built-in estimator
   │              │                      gives the instant first price
   │              └─► printers on your LAN (Elegoo MQTT/HTTP, Moonraker, Bambu)
   │
 Mini App ◄── public HTTPS address: Cloudflare Tunnel (quick or named) or your own proxy
```

Everything runs on one always-on Windows PC in the studio; `watchdog.py` keeps the bot alive.

## Requirements

- Windows 10/11 x64 PC that stays on; 8 GB RAM or more is comfortable.
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your Telegram user ID
  ([@userinfobot](https://t.me/userinfobot)).
- A slicer on the same PC: OrcaSlicer (recommended), ElegooSlicer or Bambu Studio.
- Internet access. The Mini App needs a public HTTPS address; by default a free Cloudflare
  Quick Tunnel is started for you (no account needed, but the address changes on restart).

## Install

**One command** (PowerShell): the script installs Python and Git with winget if needed,
clones this repository to `C:\OpenPrintShop\bot`, creates the virtual environment, asks for
the bot token and admin IDs, and registers the watchdog to start at logon.

```powershell
irm https://raw.githubusercontent.com/openprintshop/openprintshop/main/tools/installer/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**MSI:** `tools/installer/build_msi.ps1` builds a wizard-style installer (WiX 5) that runs the
same script.

**Manual / development:**

```powershell
git clone https://github.com/openprintshop/openprintshop.git
cd openprintshop
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env     # set BOT_TOKEN and ADMIN_IDS, see comments inside
.\.venv\Scripts\python run.py
```

Then send `/start` to your bot. On the studio PC the desktop admin is at
`http://127.0.0.1:8081/app/desk/`. Every setting in `.env.example` is documented inline;
most day-to-day settings (prices, plastics, printers, branding, payments, delivery) live in
the admin UI.

**Updates:** Admin → Status → Updates (manual, notify or automatic). The bot fetches `main`
from its git remote and reinstalls changed dependencies.

**Optional modules** after install: `tools\installer\modules.ps1 -Enable ai` or
`-Enable smartsearch`.

## Development

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest        # about 3,800 tests, no network needed
.\.venv\Scripts\python -m ruff check .
npm install && npm run build:desk         # only if you change app/webui/desk/src
```

Project layout: `run.py` (entry point), `app/` (bot handlers, web server, pricing, slicing,
printer drivers in `app/printerdrv/`, storage in `app/store/`), `app/webui/` (Mini App and the
desktop admin), `tools/` (installer and utilities), `tests/`.

## Contributing, security, license

- Contributions are welcome — please read [CONTRIBUTING.md](CONTRIBUTING.md).
- Found a vulnerability? Please report it privately — see [SECURITY.md](SECURITY.md).
- OpenPrintShop is licensed under the **GNU Affero General Public License v3.0**
  ([LICENSE](LICENSE)). If you run a modified version for others, you must offer them its
  source. Third-party components and their licenses: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Built for a real small print studio and opened for everyone.
