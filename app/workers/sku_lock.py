"""Общая блокировка пересборки SKU-опции по gid.

stock_sync и price_sync оба умеют пересобирать опцию карточки. Если они делают это по
одной карточке одновременно, ggsel ловит промежуточное состояние «нет дефолта» и отбивает
422 (FAILED_TO_SAVE «должен быть указан 1 вариант по умолчанию»), и карточка застревает.
Один процесс планировщика → достаточно in-process asyncio.Lock на gid.
"""
import asyncio

_locks: dict[int, asyncio.Lock] = {}


def rebuild_lock(gid: int) -> asyncio.Lock:
    lock = _locks.get(gid)
    if lock is None:
        lock = _locks[gid] = asyncio.Lock()
    return lock
