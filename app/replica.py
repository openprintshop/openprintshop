"""Копия базы на VPS (Э3 в варианте «копия», решение владельца 05.10.2026; контракт —
docs/Перспективные разработки/vps-relay-2026-10-04.md §7).

Главная база остаётся на ПК: цех (принтер, деск, склад, расчёты) от интернета не зависит и
не медленнее. Раз в минуту снимок базы уезжает приёмной на VPS:

* снимок — sqlite3 backup API (согласованный срез, писателей бота не держит), 8 МБ — 30–40 мс;
* из снимка вычищается то, чему не место вне ПК (SCRUB): токен платёжного провайдера, код
  доступа, серийник и адрес принтера в LAN, токены сессий настольной админки и ссылок, журнал
  ошибок (в трассировках бывает что угодно). Удалённое затирается нулями (secure_delete), а
  хвосты удалённого раньше вычистил один раз VACUUM при старте (миграция v11 в app/db.py);
* уезжают только изменившиеся страницы (PRAGMA page_size, обычно 4 КиБ) — обычно десятки КиБ.
  Целиком (gzip, ~1,5 МБ) — в первый раз после старта бота, если приёмная держит не то, что мы
  ей отдали в прошлый раз, и если изменилось больше половины файла. Приёмная сверяет sha256
  результата: не сошлось — следующий шаг целиком.

Тело запроса — gzip от «заголовок JSON + \\n + страницы подряд» (pack / unpack — общий
формат с relay/replica.py; тесты сверяют обе стороны).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

#: Версия формата тела /_r/replica.
FORMAT = 1
#: Изменилось больше этой доли страниц — дешевле целиком.
FULL_SHARE = 0.5

#: Что вычищается из копии. Каждая строка — отдельно: таблицы может не быть (старая или
#: урезанная установка), это не повод не отправить копию.
SCRUB = (
    "UPDATE text_settings SET value = '' WHERE key = 'pay_provider_token' AND value != ''",
    "UPDATE printers SET access_code = '', serial = '', address = '' "
    "WHERE access_code != '' OR serial != '' OR address != ''",
    "DELETE FROM desk_sessions",
    "DELETE FROM desk_login_codes",
    "DELETE FROM gridfinity_desk",
    "DELETE FROM gridfinity_shares",
    "DELETE FROM error_log",
)


def make_snapshot(src: Path, work: Path) -> tuple[bytes, int]:
    """Снимок базы без секретов → (байты файла, размер страницы). Повторный снимок неизменной
    базы даёт те же байты (разница — только в изменившихся страницах)."""
    work.parent.mkdir(parents=True, exist_ok=True)
    work.unlink(missing_ok=True)
    # Только чтение: нет файла — ошибка, а не пустая база, созданная на месте боевой.
    source = sqlite3.connect(f"file:{quote(Path(src).as_posix(), safe='/:')}?mode=ro", uri=True, timeout=10)
    try:
        dst = sqlite3.connect(str(work))
        try:
            source.backup(dst)
            # Копия — один файл без журнала: заголовок WAL от боевой базы иначе потащил бы
            # за собой -wal/-shm рядом и плавал бы от снимка к снимку.
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.execute("PRAGMA secure_delete=ON")
            for sql in SCRUB:
                try:
                    dst.execute(sql)
                except sqlite3.OperationalError:
                    pass
            dst.commit()
            page_size = int(dst.execute("PRAGMA page_size").fetchone()[0])
        finally:
            dst.close()
    finally:
        source.close()
    try:
        return work.read_bytes(), page_size
    finally:
        work.unlink(missing_ok=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def changed_pages(old: bytes, new: bytes, page_size: int) -> list[int] | None:
    """Номера страниц new, которые отличаются от old (или которых в old нет). None — сравнить
    нельзя (размер не кратен странице) — значит, целиком."""
    if page_size <= 0 or len(old) % page_size or len(new) % page_size:
        return None
    o, n = memoryview(old), memoryview(new)
    count_old = len(old) // page_size
    out = []
    for i in range(len(new) // page_size):
        a = i * page_size
        if i >= count_old or o[a:a + page_size] != n[a:a + page_size]:
            out.append(i)
    return out


def pack(header: dict, payload: bytes) -> bytes:
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + payload
    return gzip.compress(raw, 6)


def build_upload(confirmed: bytes | None, confirmed_sha: str | None, new: bytes,
                 page_size: int) -> tuple[bytes, dict]:
    """Тело /_r/replica: изменившиеся страницы относительно подтверждённой приёмной копии,
    а если её нет или изменилось слишком много — целиком. Второй элемент — заголовок
    (для журнала и тестов)."""
    new_sha = sha256(new)
    header = {"v": FORMAT, "sha256": new_sha, "size": len(new), "page_size": page_size}
    pages = changed_pages(confirmed, new, page_size) if confirmed is not None and confirmed_sha else None
    if pages is not None and len(pages) <= FULL_SHARE * max(1, len(new) // page_size):
        header.update(mode="delta", base=confirmed_sha, pages=pages)
        payload = b"".join(new[i * page_size:(i + 1) * page_size] for i in pages)
    else:
        header["mode"] = "full"
        payload = new
    return pack(header, payload), header


class Replicator:
    """Состояние отправителя: что приёмная подтвердила в последний раз (байты держим в памяти,
    8 МБ; на диск не пишем — запись копии раз в минуту стоила бы диску ПК ~11 ГБ в сутки,
    а после рестарта бота одна выгрузка целиком — 1,5 МБ)."""

    def __init__(self, db_path: Path, work_dir: Path) -> None:
        self.db_path = Path(db_path)
        self.work = Path(work_dir) / "replica.snapshot.db"
        self.confirmed: bytes | None = None
        self.confirmed_sha: str | None = None
        self.last_at: float | None = None
        self.last_mode: str | None = None
        self.last_bytes = 0
        self.error: str | None = None

    def prepare(self) -> tuple[bytes, dict, bytes] | None:
        """Снимок и тело выгрузки (в потоке: файловый ввод-вывод и gzip). None — база не
        менялась с прошлой подтверждённой выгрузки."""
        data, page_size = make_snapshot(self.db_path, self.work)
        if self.confirmed_sha is not None and sha256(data) == self.confirmed_sha:
            return None
        body, header = build_upload(self.confirmed, self.confirmed_sha, data, page_size)
        return body, header, data

    def accepted(self, data: bytes, header: dict, sent: int, now: float) -> None:
        self.confirmed, self.confirmed_sha = data, header["sha256"]
        self.last_at, self.last_mode, self.last_bytes, self.error = now, header["mode"], sent, None

    def reset(self) -> None:
        """Приёмная держит не то, что мы ей отдали: следующий шаг — целиком."""
        self.confirmed = self.confirmed_sha = None

    def status(self) -> dict:
        return {"sha256": (self.confirmed_sha or "")[:12] or None, "last_at": self.last_at,
                "last_mode": self.last_mode, "last_kb": round(self.last_bytes / 1024, 1),
                "error": self.error}
