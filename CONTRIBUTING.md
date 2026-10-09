# Contributing to OpenPrintShop

Thank you for helping! Issues and pull requests are welcome in **English or Russian**.

## Good first contributions

- **Translations.** The UI is Russian-only for now; extracting strings and adding English (and
  other languages) is the most valuable help right now.
- **Printer support.** Drivers live in `app/printerdrv/`; `fake.py` shows the contract.
  Reports from owners of Bambu Lab and Klipper printers are especially useful.
- **Slicer profiles and price accuracy** for printers other than the Elegoo Centauri Carbon 2.
- Bug reports with steps to reproduce and the relevant part of `data/logs/bot.log`
  (remove tokens and personal data first).

## Development setup

Windows 10/11, Python 3.13:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check .
```

The desktop admin (`app/webui/desk/src`) is bundled with esbuild: `npm install`, then
`npm run build:desk`, and commit the rebuilt `app/webui/desk/desk.js` together with the sources
(a test checks that the bundle is up to date).

## Pull requests

- Keep each pull request focused on one change and add or update tests for it.
- `pytest` and `ruff check .` must pass.
- Match the surrounding code: naming, comment density, Russian comments are fine.
- Never commit secrets, `.env`, `data/` or personal data of real customers — use made-up
  names, IDs and example addresses (`203.0.113.x`, `example.com`) in tests and mocks.

## License of contributions

OpenPrintShop is licensed under the GNU AGPL v3.0. By submitting a contribution you agree that it
is licensed under the same license.

---

# Участие в разработке

Спасибо за помощь! Задачи и pull request'ы принимаются **на русском или английском**.

- **Переводы** — сейчас интерфейс только на русском; вынести строки и добавить английский —
  самая полезная помощь.
- **Принтеры** — драйверы в `app/printerdrv/`, контракт виден по `fake.py`. Особенно ценны
  отчёты владельцев Bambu Lab и Klipper.
- **Профили слайсеров и точность цены** для принтеров, кроме Elegoo Centauri Carbon 2.
- **Ошибки** — шаги воспроизведения и кусок `data/logs/bot.log` (без токенов и личных данных).

Окружение и проверки — как выше: `pytest` и `ruff check .` должны проходить, бандл настольной
админки пересобирается `npm run build:desk` и коммитится вместе с исходниками. В тестах и моках —
только выдуманные имена, ID и адреса. Присылая вклад, вы соглашаетесь, что он распространяется
по лицензии проекта (GNU AGPL v3.0).
