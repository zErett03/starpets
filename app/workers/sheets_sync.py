"""
Выгрузка заказов в Google-таблицу через Apps Script.

Почему отправляем ОКНО последних дней целиком, а не только новые заказы: статус меняется
задним числом. Заказ уходит «в работе», через час становится доставленным, а иногда через
сутки — возвратом. Если слать каждый заказ однократно, таблица навсегда застынет на том
статусе, который был в момент отправки.

Скрипт на стороне таблицы обновляет строку по номеру заказа, поэтому повторные отправки
безопасны и дублей не создают.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import DeliveryStatus, Order

_DELIVERED = {DeliveryStatus.done, DeliveryStatus.finalized}
# closed = оператор вернул деньги покупателю вручную; failed = выдача сорвалась.
# Для таблицы это одно и то же: продажи не случилось. Совпадает с _REFUND в CSV-экспорте.
_REFUNDED = {DeliveryStatus.failed, DeliveryStatus.closed}


def _status(o: Order) -> str:
    """Три состояния, как в таблице: суммы считаются только по первым двум.

    needs_attention остаётся «в работе» осознанно: деньги ещё у нас, заказ может
    закрыться выдачей. Ярлыка «Проблема» в таблице нет — формула прибыли знает только
    «Возврат», и новый статус посчитался бы как обычная продажа.
    """
    if o.delivery_status in _DELIVERED:
        return "Доставлен"
    if o.delivery_status in _REFUNDED:
        return "Возврат"
    return "В работе"


async def _usd_rate() -> float:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://www.cbr-xml-daily.ru/daily_json.js")
            resp.raise_for_status()
            v = (resp.json().get("Valute") or {}).get("USD") or {}
        return float(v.get("Value")) / float(v.get("Nominal") or 1)
    except Exception:  # noqa: BLE001
        return 0.0


async def push_orders(days: int = 14) -> dict:
    """Отправляет заказы, оплаченные за последние `days` дней."""
    url = (settings.sheets_webhook_url or "").strip()
    token = (settings.sheets_webhook_token or "").strip()
    if not url or not token:
        return {"ok": False, "error": "SHEETS_WEBHOOK_URL или SHEETS_WEBHOOK_TOKEN не заданы"}

    since = datetime.utcnow() - timedelta(days=days)
    rate = await _usd_rate()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Order)
            .where(Order.paid_at.isnot(None), Order.paid_at >= since)
            .order_by(Order.paid_at.asc())
        )).scalars().all()

    orders = []
    for o in rows:
        sale = float(o.amount_rub or 0)
        if sale <= 0:
            continue      # precheck-заготовки без оплаты в бухгалтерию не идут
        orders.append({
            "ggsel_order_id": o.ggsel_order_id,
            "item_name": o.item_name,
            "sale_rub": sale,
            "usd_rub": round(rate, 4),
            "cost_usd": float(o.exec_price_usd or 0),
            "status": _status(o),
            "paid_at": o.paid_at.strftime("%d.%m.%Y %H:%M") if o.paid_at else "",
        })

    if not orders:
        return {"ok": True, "sent": 0, "note": "нет оплаченных заказов за период"}

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.post(url, json={
                "token": token,
                "sheet": settings.sheets_tab,
                "orders": orders,
            })
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"[SheetsSync] отправка не удалась: {type(e).__name__}: {e}", flush=True)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "sent": 0}

    print(f"[SheetsSync] отправлено {len(orders)} заказов → {result}", flush=True)
    return {"ok": True, "sent": len(orders), "sheet_response": result}
