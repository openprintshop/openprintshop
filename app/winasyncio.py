"""Заплатка на баг asyncio под Windows: закрытие сокета, брошенное на полпути.

Что происходит без неё. У проактор-цикла (единственного под Windows, где работает подпроцесс)
закрытие соединения устроено так — CPython 3.13, Lib/asyncio/proactor_events.py, строки 154-171:

    def _call_connection_lost(self, exc):
        ...
        finally:
            if hasattr(self._sock, 'shutdown') and self._sock.fileno() != -1:
                self._sock.shutdown(socket.SHUT_RDWR)   # ← кидает WinError 10022
            self._sock.close()                           # ← сюда уже не доходит
            self._sock = None
            server = self._server
            if server is not None:
                server._detach(self)                     # ← и сюда
                self._server = None
            self._called_connection_lost = True          # ← и сюда

Если пир закрыл соединение раньше нас, `shutdown()` на уже отключённом сокете отвечает
WinError 10022 «получен недопустимый аргумент». Исключение НЕ ловится, летит наружу из finally —
и три последние операции не выполняются НИКОГДА: сокет не закрыт, транспорт не отцеплен от
сервера, флаг «уже прибрались» не взведён. В логе остаётся «Exception in callback
_ProactorBasePipeTransport._call_connection_lost()».

Чем это обходится боту (замер 10.09.2026): 2217 таких исключений за сутки — 99.7% ВСЕХ строк
ERROR в bot.log, из-за них лог ротировался каждые два часа и настоящие ошибки в нём тонули.
Плюс каждое такое событие оставляет незакрытый сокет и не отцепленный транспорт.

Заплатка повторяет оригинал слово в слово и оборачивает единственную строку с shutdown() в
try/except OSError: не смогли попрощаться вежливо — и ладно, закрыть сокет всё равно надо.
Это же и есть общепринятый обход этого бага.

Ставить ДО создания цикла (то есть до asyncio.run) — заменяется метод класса, а не объекта,
так что порядок важен только относительно первого соединения.
"""
from __future__ import annotations

import logging
import socket
import sys


def install() -> None:
    """Заменить _call_connection_lost на терпимую к WinError 10022 версию. Не под Windows — ничего."""
    if sys.platform != "win32":
        return
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport as T
    except Exception:  # noqa: BLE001 — нет проактора, значит и чинить нечего
        return
    if getattr(T, "_pcb_patched", False):
        return

    def _call_connection_lost(self, exc):  # noqa: ANN001, ANN202 — сигнатура стандартной библиотеки
        if self._called_connection_lost:
            return
        try:
            self._protocol.connection_lost(exc)
        finally:
            try:
                if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
                    self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass          # ← ВСЯ правка: пир уже ушёл, прощаться не с кем
            self._sock.close()
            self._sock = None
            server = self._server
            if server is not None:
                server._detach(self)
                self._server = None
            self._called_connection_lost = True

    T._call_connection_lost = _call_connection_lost
    T._pcb_patched = True
    logging.getLogger(__name__).info(
        "asyncio: заплатка на WinError 10022 в _call_connection_lost поставлена")
