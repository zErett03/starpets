"""Баланс закупа: показ в админке и предупреждение, пока он ещё не кончился.

Зачем. Баланс — единственный ресурс, исчерпание которого останавливает бизнес целиком:
выкуп падает, заказы копятся в очереди, покупатели ждут. Узнавать об этом по жалобам
поздно. Один раз AM уже дошёл до нуля незаметно — деньги просто разошлись на выкупы за
пять дней, и обнаружилось это по отказам.

Значение кэшируется: админка перерисовывается часто, а баланс меняется медленно, и
дёргать StarPets на каждый показ страницы незачем.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.clients.starpets import starpets
from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import KVState

_CACHE: dict = {"value": None, "at": 0.0}
_CACHE_TTL = 60          # сек; баланс не скачет так быстро, чтобы спрашивать чаще
_ALERT_KEY = "balance_watch:alerted"


def _dig_balance(info: dict) -> float | None:
    """Баланс из ответа /ex-buyers/info/me. Ключ плавает между версиями API."""
    if not isinstance(info, dict):
        return None
    for src in (info.get("buyer") or {}, info.get("data") or {}, info):
        if not isinstance(src, dict):
            continue
        for k in ("balance", "balanceUsd", "balance_usd"):
            v = src.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


async def get_balance_usd(force: bool = False) -> float | None:
    """Баланс закупа в долларах. None — если API не ответил (это НЕ ноль)."""
    now = time.monotonic()
    if not force and _CACHE["value"] is not None and (now - _CACHE["at"]) < _CACHE_TTL:
        return _CACHE["value"]
    try:
        info = await starpets.get_info()
    except Exception as e:  # noqa: BLE001 — недоступность API не должна ронять страницу
        print(f"[Balance] запрос не удался: {e}", flush=True)
        return _CACHE["value"]
    val = _dig_balance(info)
    if val is not None:
        _CACHE["value"], _CACHE["at"] = val, now
    return val


async def balance_watch() -> dict:
    """Предупреждение о низком балансе. Один раз на пересечение порога, не каждый проход.

    Повторяем, только когда баланс успел подняться выше порога и снова упал: иначе
    сообщение приходило бы каждые четверть часа всё время, пока деньги не пополнены,
    и перестало бы читаться ровно тогда, когда важно.
    """
    bal = await get_balance_usd(force=True)
    if bal is None:
        return {"balance": None, "alerted": False}

    low = bal < settings.low_balance_usd
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(KVState).where(KVState.key == _ALERT_KEY))).scalar_one_or_none()
        was_low = bool(row and row.value == "1")
        if low != was_low:
            if row:
                row.value = "1" if low else "0"
            else:
                db.add(KVState(key=_ALERT_KEY, value="1" if low else "0"))
            await db.commit()

    if not low or was_low:
        return {"balance": bal, "alerted": False, "low": low}

    msg = (f"💰 <b>Баланс закупа низкий</b>\n"
           f"осталось ${bal:.2f} · порог ${settings.low_balance_usd:.0f}\n"
           f"Выкуп остановится, когда деньги кончатся — заказы уйдут в возврат.\n"
           f"{datetime.now(timezone.utc).strftime('%d.%m %H:%M')} UTC")
    try:
        from app.telegram.bot import _price_chats, send_message
        for chat in _price_chats():
            await send_message(chat, msg)
    except Exception as e:  # noqa: BLE001
        print(f"[Balance] алерт не отправлен: {e}", flush=True)
    print(f"[Balance] низкий баланс ${bal:.2f} (порог ${settings.low_balance_usd})", flush=True)
    return {"balance": bal, "alerted": True, "low": True}
