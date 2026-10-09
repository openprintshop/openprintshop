# -*- coding: utf-8 -*-
"""Разбор дублей в кэше загрузок (data/downloads/cache).

До 20.07.2026 ключом записи кэша была сырая строка ссылки, поэтому один и тот же файл
MakerWorld ложился в две папки: fetch_from_url(url) → sha1(url), а
download_makerworld_instance(url, iid) → sha1(url#iid). Ключ починен (теперь MakerWorld
ключуется по design id + instance id), но уже накопленные дубли лежат на диске.

Скрипт ищет записи с ПОБАЙТОВО одинаковым набором файлов и схлопывает их: одна остаётся
как есть, остальные превращаются в «указатель» (meta.json c полем alias) — тогда кэш
по-прежнему находится по обоим ключам, а место освобождается.

Осторожность по умолчанию:
  * без --apply ничего не меняется, только отчёт;
  * запись, на файлы которой ссылается коллекция/сохранённые модели (таблицы collection,
    saved_models), выбирается «хранителем» и не трогается — иначе у модели отвалятся файлы;
  * если ссылки ведут на ВСЕ записи группы, группа пропускается целиком.

Запуск (из корня репо):  .venv/Scripts/python dedupe_download_cache.py
  [--apply]   действительно схлопнуть дубли (без него — сухой прогон)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from app import config


def _referenced_paths() -> set[str]:
    """Файлы, на которые ссылаются коллекция и сохранённые модели, — их удалять нельзя."""
    refs: set[str] = set()
    if not config.DB_PATH.exists():
        return refs
    con = sqlite3.connect(str(config.DB_PATH))
    try:
        for sql in ("SELECT paths FROM collection WHERE paths IS NOT NULL",
                    "SELECT paths FROM saved_models WHERE paths IS NOT NULL"):
            try:
                for (raw,) in con.execute(sql):
                    try:
                        for p in json.loads(raw or "[]"):
                            refs.add(str(Path(p)).lower())
                    except (ValueError, TypeError):
                        pass
            except sqlite3.Error:
                pass          # старая БД без таблицы/колонки — просто ничего не защищаем
    finally:
        con.close()
    return refs


def _entry(d: Path) -> dict | None:
    """Запись кэша → сведения о ней. None — не запись (нет meta.json) или уже указатель."""
    try:
        m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if m.get("alias"):
        return None
    files = [d / n for n in m.get("files", [])]
    if not files or not all(f.exists() for f in files):
        return None
    h = hashlib.sha1()
    size = 0
    for f in sorted(files, key=lambda p: p.name.lower()):
        data = f.read_bytes()
        size += len(data)
        h.update(f.name.lower().encode("utf-8"))
        h.update(data)
    return {"dir": d, "key": d.name, "label": m.get("label") or "", "url": m.get("url") or "",
            "iid": m.get("iid"), "created": float(m.get("created_at") or 0),
            "files": files, "size": size, "digest": h.hexdigest()}


def _keeper(group: list[dict], refs: set[str]) -> dict:
    """Кого оставить: сперва запись, на файлы которой уже кто-то ссылается, иначе самую старую."""
    linked = [e for e in group if any(str(f).lower() in refs for f in e["files"])]
    return sorted(linked or group, key=lambda e: e["created"])[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Схлопнуть дубли кэша загрузок")
    ap.add_argument("--apply", action="store_true", help="действительно схлопнуть (иначе сухой прогон)")
    args = ap.parse_args()

    root = config.DOWNLOAD_CACHE_DIR
    if not root.exists():
        print(f"нет папки кэша: {root}")
        return 1
    refs = _referenced_paths()
    entries = [e for e in (_entry(d) for d in sorted(root.iterdir()) if d.is_dir()) if e]
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e["digest"], []).append(e)
    dups = [g for g in groups.values() if len(g) > 1]

    freed = skipped = 0
    for g in sorted(dups, key=lambda g: -sum(e["size"] for e in g[1:])):
        keep = _keeper(g, refs)
        drop = [e for e in g if e is not keep]
        print(f"\n{keep['label'] or keep['url'] or keep['key']}")
        print(f"  оставляем  {keep['key'][:12]}  {keep['size'] / 2**20:6.1f} МБ  {keep['url']}")
        for e in drop:
            locked = [f for f in e["files"] if str(f).lower() in refs]
            # без символов вне cp1251: консоль Windows по умолчанию именно в ней
            mark = "ССЫЛКИ — пропуск" if locked else "станет указателем"
            print(f"  дубль      {e['key'][:12]}  {e['size'] / 2**20:6.1f} МБ  {mark}")
            if locked:
                skipped += 1
                continue
            freed += e["size"]
            if not args.apply:
                continue
            for f in e["files"]:
                f.unlink(missing_ok=True)
            (e["dir"] / "meta.json").write_text(
                json.dumps({"alias": keep["key"], "url": e["url"], "label": e["label"]},
                           ensure_ascii=False), encoding="utf-8")

    verb = "освобождено" if args.apply else "освободится"
    print(f"\nзаписей: {len(entries)}, групп-дублей: {len(dups)}, "
          f"{verb}: {freed / 2**20:.1f} МБ" + (f", пропущено по ссылкам: {skipped}" if skipped else ""))
    if dups and not args.apply:
        print("сухой прогон — повторите с --apply, чтобы схлопнуть")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
