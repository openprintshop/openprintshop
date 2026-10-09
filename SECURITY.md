# Security policy

OpenPrintShop runs on a studio's own PC and handles customer orders, Telegram user IDs and
payment notifications, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Include the affected version (commit),
steps to reproduce and the impact you expect. You will get an answer within a few days.

## Supported versions

Only the latest `main` is supported; installations update themselves from it
(Admin → Status → Updates).

## Hardening tips for studios

- Keep `.env` and `data/` private: they contain the bot token, payment provider token and the
  customer database. Off-site backups copy them too — store them accordingly.
- The desktop admin is served to `127.0.0.1` only; do not expose port 8081 directly to the
  internet — use the built-in Cloudflare Tunnel or a reverse proxy that serves only the Mini App.

---

# Безопасность

Пожалуйста, **не открывайте публичный issue** про уязвимость — сообщите приватно через
**Security → Report a vulnerability** в этом репозитории: версия (коммит), шаги воспроизведения,
ожидаемые последствия. Ответим в течение нескольких дней. Поддерживается только актуальная
ветка `main`. Храните `.env` и `data/` в тайне (там токены и база клиентов) и не открывайте
порт 8081 в интернет напрямую — используйте встроенный туннель Cloudflare или обратный прокси.
