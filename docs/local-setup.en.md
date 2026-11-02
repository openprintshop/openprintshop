[Русский](local-setup.md) | **English**

# Local setup, running and tests

A guide for developers: how to set up OpenPrintShop from source, run the bot and run the
checks. The production install on the studio PC (Windows, `install.ps1`, MSI) is described in
[README.md](../README.md#install).

## What has been verified and what hasn't

All commands in the "Linux / macOS" sections below were actually run in a clean Linux x86_64
container with Python 3.13.17, Node 22.22.0 and npm 10, at commit `b806fc2`. The results are
shown as they came out.

Not verified:

- **Windows** (the project's main platform). The PowerShell commands are taken from README and
  CONTRIBUTING and were not run for this document.
- **macOS**. Expected to behave the same as Linux, but not verified.
- **The bot with a real token.** The container had no access to `api.telegram.org`, so the bot
  was only run up to its first request to Telegram (details below). The desktop admin could not
  be opened in a browser for the same reason.
- **Docker.** The repository has no `Dockerfile` or `docker-compose.yml`, so there is no way to
  run it with Docker.

## Requirements

- Python 3.13 (as stated in CONTRIBUTING; verified on 3.13.17).
- Git.
- Node.js and npm — only for building the desktop admin (`app/webui/desk/src`). The bot itself
  doesn't need Node: a prebuilt `app/webui/desk/desk.js` is committed to the repository.
- To run the bot: a token from [@BotFather](https://t.me/BotFather) and your Telegram ID
  ([@userinfobot](https://t.me/userinfobot)).
- For slicer-based pricing: OrcaSlicer, ElegooSlicer or Bambu Studio. Without a slicer the bot
  still starts and logs a warning "Slicer not found … pricing will not work" («Слайсер не найден … расчёт работать не будет»).

## Installation

### Linux / macOS

```bash
git clone https://github.com/openprintshop/openprintshop.git
cd openprintshop
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

`requirements-dev.txt` is only needed for tests and the linter (pytest, pytest-asyncio, ruff, mypy).

### Windows (PowerShell, not verified in this document)

```powershell
git clone https://github.com/openprintshop/openprintshop.git
cd openprintshop
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
```

## Configuration

```bash
cp .env.example .env
```

(on Windows: `copy .env.example .env`). Every setting is documented by a comment in
`.env.example`. The minimum needed to start:

| Variable | What it is |
|---|---|
| `BOT_TOKEN` | bot token from @BotFather |
| `ADMIN_IDS` | Telegram IDs of the owners, comma-separated |

What else is worth changing when not running on Windows:

- `SLICER_PATH`, `PROFILES_DIR` — the example has `C:\Program Files\ElegooSlicer\…` paths.
  Set the path to the slicer on your machine, or leave them as is if you don't need
  slicer-based pricing.
- `TUNNEL_MODE` — `quick` by default. According to the code (`app/tunnel.py`), this mode
  downloads `cloudflared-windows-amd64.exe`, so it won't work on Linux or macOS (not verified:
  the container had no internet access). For local development set `TUNNEL_MODE=none`; the
  Telegram Mini App then won't open until `WEBAPP_PUBLIC_URL` is set to a public HTTPS address.
- `DATA_DIR` — where the bot keeps its database, caches and logs. Defaults to `data/` in the
  project folder (it is in `.gitignore`). To keep development data separate from the project,
  uncomment the line and set an absolute path.

## Running

```bash
.venv/bin/python run.py
```

(on Windows: `.\.venv\Scripts\python run.py`). Once it is running, send `/start` to the bot.
The desktop admin on the same machine: `http://127.0.0.1:8081/app/desk/` (the port is set by the
`WEBAPP_PORT` variable).

What happened in the container with the placeholder token from `.env.example`,
`TUNNEL_MODE=none` and `DATA_DIR` in a temporary folder: the bot read the settings, created the
database and started the web server:

```
INFO root: Канал выпуска: STABLE · b806fc2
WARNING root: Слайсер не найден по пути C:\Program Files\ElegooSlicer\elegoo-slicer.exe — расчёт работать не будет.
INFO app.webapp: Мини-апка слушает 127.0.0.1:8081
ERROR app.tunnel: Вход снаружи не настроен: TUNNEL_MODE=none, но WEBAPP_PUBLIC_URL не задан — адреса снаружи нет вовсе
```

The process then exited with `aiogram.exceptions.TelegramNetworkError` on a request to
`api.telegram.org`: that host is unreachable from the container. In other words, the bot
contacts Telegram right at startup and won't work without a connection to it (or with an invalid
token). The bot could not be tested any further in this environment.

On the studio PC `watchdog.py` keeps the bot running; it isn't needed for development.

## Tests and checks

### Python tests

```bash
.venv/bin/python -m pytest
```

The tests don't need network access. The test `tests/test_network_slice.py` (marker `network`)
is skipped by default; it is enabled with `RUN_NETWORK_TESTS=1` and needs network access and a
slicer (not run).

**The tests need a `.env`.** Some tests read `BOT_TOKEN` and `ADMIN_IDS` from the settings.
Without `.env` (and without these variables in the environment), 30 tests failed in the
container: 3605 passed, 21 skipped. With `.env` copied from `.env.example` unchanged, the result
is:

```
8 failed, 3627 passed, 21 skipped in 130.87s
```

This run was after `npm ci`. These 8 tests fail on Linux (most likely on macOS too), and the
reasons are known:

| Test | Reason |
|---|---|
| `test_bambu3mf.py::test_sanitize_uses_db_resolver_profiles` | Windows path (`C:\x\…`): not parsed as a path on Linux |
| `test_datapaths_c2.py::test_abs_data_reroots_foreign_absolute_path_when_file_exists` | Windows paths (`D:\…`) |
| `test_datapaths_c2.py::test_vision_tagger_reads_relative_paths` | Windows paths (`C:\…`) |
| `test_profilepaths.py::test_reroots_absolute_path_from_another_machine` | Windows paths (`D:\…`) |
| `test_selfcheck_engines.py::test_existing_engine_is_quiet` | the slicer is looked up under `C:\Program Files\…` |
| `test_selfcheck_engines.py::test_default_engine_not_duplicated` | same as above |
| `test_desk_js_suite.py::test_desk_js_suite_passes` | runs `app/webui/desk/tests/run.mjs`, see below. Skipped if `node` or `node_modules/@esbuild` is missing (i.e. before `npm ci`) |
| `test_geometry.py::test_subtract_distinct_cutter` | trimesh can't find a boolean operations engine |

The last test passes if you additionally install `manifold3d` (it isn't in `requirements.txt`):

```bash
.venv/bin/pip install manifold3d
.venv/bin/python -m pytest tests/test_geometry.py   # 10 passed, 1 skipped
```

### Linter

```bash
.venv/bin/python -m ruff check .
```

Result in the container: `All checks passed!`.

### Desktop admin (Node)

```bash
npm ci
npm run build:desk
```

The build succeeded, and the rebuilt `app/webui/desk/desk.js` matched the committed one
(`git status` was clean). If you change `app/webui/desk/src`, commit the rebuilt `desk.js`
together with the sources: a test checks that the bundle is up to date.

`npm run test:desk` **does not work** on Linux: it exits with code 1 and prints nothing.
`app/webui/desk/tests/run.mjs` calls esbuild via the hard-coded path
`node_modules/@esbuild/win32-x64/esbuild.exe`, while on Linux npm installs a different esbuild
package. Not verified on Windows.
