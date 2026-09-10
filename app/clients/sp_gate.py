"""Общий шлюз ко всем запросам к StarPets: счётчик и ограничитель темпа.

Зачем. Запросы к StarPets идут из десятка мест — событийная лента, оживление полов,
сторож цен, выгрузка прайса, precheck на каждую продажу, монитор баланса. Каждое место
знает только про свою нагрузку, а площадка видит сумму. Мы узнали об этом не из графика,
а из жалобы на спам: своих цифр у нас не было вовсе.

Шлюз решает две задачи.

СЧЁТ. Каждый вызов помечается именем ручки, и `stats()` показывает, кто сколько сделал за
время жизни процесса и за последнюю минуту. Без этого разговор про «много запросов»
беспредметен: неизвестно ни сколько их, ни кто их шлёт.

ТЕМП. Общий семафор и минимальный интервал между запросами держат суммарный поток ниже
заданного потолка, независимо от того, сколько воркеров проснулись одновременно. Пики
опаснее среднего: раз в 10 минут оживление полов запускало 500 запросов подряд, и именно
такие всплески выглядят со стороны как атака.

Важно: ограничитель общий на процесс, но НЕ на аккаунт. Второй сервис (MM2) считает свой
поток отдельно, поэтому потолки задаются с запасом на двоих.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from app.config import settings

_sem: asyncio.Semaphore | None = None
_lock: asyncio.Lock | None = None
_last_at: float = 0.0

_total: dict[str, int] = defaultdict(int)     # ручка -> запросов за жизнь процесса
# Счёт по МИНУТНЫМ КОРЗИНАМ: {минута (epoch//60): {ручка: сколько}}.
#
# Раньше здесь стоял deque(maxlen=4000), и это была ошибка: при 4 запросах в секунду
# четыре тысячи записей — это семнадцать минут, а не час. «Часовой бюджет 300» на деле
# превращался в тысячу с лишним, то есть ломался ровно в том режиме, ради которого
# создавался. Корзины занимают шестьдесят словарей на любой нагрузке.
_buckets: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_started_at: float = time.time()
_penalty_until: float = 0.0                   # пауза после 429/5xx (см. penalize)


def _bucket(ts: float | None = None) -> int:
    return int((ts if ts is not None else time.time()) // 60)


def _prune(now_min: int) -> None:
    for m in [m for m in _buckets if now_min - m > 60]:
        _buckets.pop(m, None)


def _count_since(endpoint: str, minutes: int) -> int:
    now_min = _bucket()
    return sum(b.get(endpoint, 0) for m, b in _buckets.items() if now_min - m < minutes)


def _hit(endpoint: str) -> None:
    now_min = _bucket()
    _total[endpoint] += 1
    _buckets[now_min][endpoint] += 1
    _prune(now_min)


class SpBudgetExceeded(RuntimeError):
    """Фоновой задаче отказано: часовой бюджет поштучных запросов исчерпан."""


def _ensure() -> None:
    """Примитивы создаём лениво: на импорте модуля цикла событий ещё нет."""
    global _sem, _lock
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, int(settings.starpets_max_concurrency)))
    if _lock is None:
        _lock = asyncio.Lock()


def budget_left(endpoint: str = "items/top") -> int:
    """Сколько поштучных запросов ещё можно сделать в текущем часовом окне.

    Поставщик жаловался именно на `items/top` — опрос цены по одному товару. Событийная
    лента такого не требует: она сама приносит изменения. Поштучный опрос нужен ровно в
    двух местах, где нельзя ошибиться, — precheck перед оплатой и проверка перед выкупом.
    Всё остальное (оживление полов, сторож цен, выгрузка прайса) — фон, и фон обязан
    умещаться в бюджет, а не расти вместе с витриной.
    """
    limit = int(getattr(settings, "starpets_top_budget_hourly", 0) or 0)
    if limit <= 0:
        return 10 ** 9
    return max(0, limit - _count_since(endpoint, 60))


@asynccontextmanager
async def sp_gate(endpoint: str, background: bool = False):
    """Пропуск на один запрос к StarPets. Оборачивать КАЖДЫЙ вызов.

    endpoint — короткое имя ручки (`items/top`, `updates`, `info`, `buy`), по нему потом
    видно, кто именно создаёт поток. Имя должно быть без переменных частей: `items/top`,
    а не `items/top/12345`, иначе счётчик рассыплется на тысячи ключей.

    background=True — запрос ради обслуживания витрины, а не ради конкретной продажи. Такие
    расходуют часовой бюджет и получают отказ (SpBudgetExceeded), когда он исчерпан. Продажа
    при этом не страдает: precheck и выкуп идут как обычные, вне бюджета.
    """
    global _last_at
    _ensure()
    if background and budget_left(endpoint) <= 0:
        raise SpBudgetExceeded(
            f"часовой бюджет {endpoint} исчерпан "
            f"({settings.starpets_top_budget_hourly}/час) — фоновая задача пропущена")
    async with _sem:
        # Минимальный интервал держим под общим замком: без него десять корутин, ждавших
        # на семафоре, стартуют одновременно и дают залп вместо ровного темпа.
        async with _lock:
            # Поставщик недавно отбивался — выдерживаем паузу перед следующей попыткой.
            _pause = _penalty_until - time.monotonic()
            if _pause > 0:
                await asyncio.sleep(min(_pause, 60.0))
            min_interval = 1.0 / max(0.1, float(settings.starpets_max_rps))
            wait = min_interval - (time.monotonic() - _last_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_at = time.monotonic()
        _hit(endpoint)
        yield


def note(endpoint: str) -> None:
    """Учесть запрос, НЕ придерживая его.

    Для операций с деньгами и выдачей — покупка, трейд, заявка в друзья. Они редки (штуки
    в час против тысяч у чтения), зато срочны: у покупателя десять минут на приём трейда,
    и задерживать их ради ровного темпа неправильно. В статистике они видны наравне со
    всеми, чтобы картина запросов была полной.
    """
    _hit(endpoint)


def penalize(seconds: float = 60.0, reason: str = "") -> None:
    """Притормозить ВЕСЬ шлюз после отказа поставщика (429 или 5xx).

    Без этого мы продолжали давить прежним темпом в сервис, который уже отбивается, —
    и со стороны это выглядит хуже всего. Пауза общая: отказ по одной ручке означает,
    что нам не рады вообще, а не только на ней.
    """
    global _penalty_until
    until = time.monotonic() + max(1.0, seconds)
    if until > _penalty_until:
        _penalty_until = until
        print(f"[SpGate] пауза {seconds:.0f}с после отказа поставщика: {reason}", flush=True)


def stats() -> dict:
    """Сводка для диагностики: сколько запросов и кем сделано."""
    now = time.time()
    minute: dict[str, int] = defaultdict(int)
    for ep, n in _buckets.get(_bucket(now), {}).items():
        minute[ep] += n
    hour: dict[str, int] = defaultdict(int)
    now_min = _bucket(now)
    for m, b in _buckets.items():
        if now_min - m < 60:
            for ep, n in b.items():
                hour[ep] += n
    uptime = max(1.0, now - _started_at)
    total = sum(_total.values())
    return {
        "uptime_sec": int(uptime),
        "total": total,
        "avg_rps": round(total / uptime, 2),
        "last_minute": dict(sorted(minute.items(), key=lambda kv: -kv[1])),
        "last_minute_total": sum(minute.values()),
        "last_hour": dict(sorted(hour.items(), key=lambda kv: -kv[1])),
        "last_hour_total": sum(hour.values()),
        "penalty_sec_left": max(0, round(_penalty_until - time.monotonic(), 1)),
        "by_endpoint": dict(sorted(_total.items(), key=lambda kv: -kv[1])),
        "limits": {"max_rps": settings.starpets_max_rps,
                   "max_concurrency": settings.starpets_max_concurrency,
                   "top_budget_hourly": getattr(settings, "starpets_top_budget_hourly", 0)},
        "top_budget_left": budget_left("items/top"),
    }
