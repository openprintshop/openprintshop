# Third-party notices

OpenPrintShop itself is licensed under the GNU AGPL v3.0 (see [LICENSE](LICENSE)). It relies on
the third-party components below. Their copyright notices and license texts ship inside the
vendored files and inside the installed Python packages.

## Python packages (installed by pip)

Core — `requirements.txt`:

| Package | License | Used for |
|---|---|---|
| aiogram | MIT | Telegram Bot API framework |
| aiosqlite | MIT | async SQLite |
| aiohttp | Apache-2.0 AND MIT | web server for the Mini App and the admin |
| httpx | BSD-3-Clause | HTTP client |
| python-dotenv | BSD-3-Clause | configuration from `.env` |
| numpy | BSD-3-Clause | numerics |
| trimesh | MIT | 3D model geometry |
| lxml | BSD-3-Clause | XML parsing (.3mf) |
| networkx | BSD-3-Clause | graphs (trimesh dependency) |
| pillow | MIT-CMU | images |
| fast-simplification | MIT | mesh simplification |
| reportlab | BSD-3-Clause | PDF quotes and calibration passports |
| qrcode | BSD-3-Clause | QR codes |
| psutil | BSD-3-Clause | system metrics |

Optional AI module — `requirements-ai.txt`: diffusers, transformers, accelerate, safetensors
(Apache-2.0), gguf (MIT), sentencepiece (Apache-2.0), torch (BSD-3-Clause).

Development only — `requirements-dev.txt`: pytest (MIT), pytest-asyncio (Apache-2.0), ruff (MIT),
mypy (MIT).

Transitive dependencies are permissive as well, except `certifi` (MPL-2.0, root certificates),
which is installed unmodified. Run `pip list` in `.venv` for the exact set.

## Files shipped in this repository

| Component | Version | License | Files |
|---|---|---|---|
| three.js (core and loaders) | 0.147.0 | MIT | `app/webui/vendor/three*.js` |
| React / ReactDOM | 18.3.1 | MIT | `app/webui/vendor/react*.production.min.js` |
| fflate | 0.8.2 | MIT | `app/webui/vendor/fflate.min.js` |
| JetBrains Mono, Onest (web fonts) | Google Fonts | SIL OFL 1.1 | `app/webui/vendor/fonts/` |
| Onest (variable font) | — | SIL OFL 1.1 | `assets/fonts/Onest-var.ttf` (bot menu card) |
| DejaVu Sans, Sans Bold, Sans Mono | 2.37 | DejaVu Fonts License (Bitstream Vera + Arev) | `assets/fonts/DejaVu*.ttf` (Cyrillic in PDFs) |

The desktop admin bundle `app/webui/desk/desk.js` is built with esbuild (MIT) from this
project's own sources only; React, three.js and fflate are loaded as separate files.

Desktop admin shell (`desktop_admin/requirements.txt`): pywebview (BSD-3-Clause),
windows-toasts (Apache-2.0), pystray (LGPL-3.0, used as an unmodified, dynamically loaded
package), Pillow (MIT-CMU).

## External programs (not included, installed separately)

| Program | License | How it is used |
|---|---|---|
| Python 3.13 | PSF License | runtime |
| Git | GPL-2.0 | code delivery and self-update, run as a separate program |
| OrcaSlicer / ElegooSlicer / Bambu Studio | AGPL-3.0 family | slicing, called as a separate CLI process |
| FreeCAD | LGPL-2.0+ | optional STEP→STL conversion, separate process |
| OpenSCAD | GPL-2.0+ | optional parametric models, separate process |
| cloudflared | Apache-2.0 (+ Cloudflare terms of service) | public HTTPS address for the Mini App; downloaded from Cloudflare's official releases on first use |

AI model weights for the optional avatar generator (Z-Image-Turbo, SDXL-Turbo, SD-Turbo) are
downloaded from Hugging Face on first use under their own terms. **Stability AI models restrict
commercial use** — check the current terms before enabling avatars in a commercial setting.
A procedural avatar fallback works without any model.

## Services and 3D models

Telegram (Bot API, Mini Apps, Payments), Cloudflare, Hugging Face, Printables and Thingiverse are
used under their own terms of service. 3D models that customers send come with their authors'
licenses (CC BY, CC BY-NC, …); respecting them is up to the studio.
