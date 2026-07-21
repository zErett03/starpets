"""
Бухгалтерия по заказам.

Считает выручку, закуп, комиссию и прибыль за период, плюс показывает деньги, застрявшие
в проблемных заказах.

ВАЖНОЕ ОГРАНИЧЕНИЕ. У заказа хранится ОДНА цена выкупа (exec_price_usd). Если предмет
перекупали — а мы это делаем при протухании и застрявших трейдах — предыдущая цена
затирается, и фактические затраты занижены. Точную картину дал бы журнал покупок, как
сделано в mmo-api; здесь его нет.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import DeliveryStatus, Order

router = APIRouter()

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

# Заказ считается принёсшим деньги только в этих статусах.
_DELIVERED = {DeliveryStatus.done, DeliveryStatus.finalized}
# Здесь деньги покупателя получены, но выдача не закрыта — риск возврата.
_AT_RISK = {DeliveryStatus.needs_attention, DeliveryStatus.failed}


def _period(since: str, until: str, days: int) -> tuple[datetime, datetime, str]:
    """Границы периода. Явные даты важнее относительных дней.

    Отбор идёт по дате ОПЛАТЫ, а не создания: бухгалтерия про деньги, а заказ мог быть
    заведён precheck-ом заранее.
    """
    def _parse(s: str, default: datetime) -> datetime:
        s = (s or "").strip()
        if not s:
            return default
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return default

    end = _parse(until, datetime.utcnow() + timedelta(days=1))
    if since.strip():
        start = _parse(since, datetime(1970, 1, 1))
    elif days > 0:
        start = datetime.utcnow() - timedelta(days=days)
    else:
        start = datetime(1970, 1, 1)
    label = f"{start:%d.%m.%Y} — {min(end, datetime.utcnow()):%d.%m.%Y}"
    return start, end, label


async def _usd_rate() -> tuple[float, str]:
    """Курс доллара ЦБ. Закуп у StarPets идёт в долларах, выручка в рублях."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(CBR_URL)
            resp.raise_for_status()
            v = (resp.json().get("Valute") or {}).get("USD") or {}
        rate = float(v.get("Value")) / float(v.get("Nominal") or 1)
        return rate, "cbr"
    except Exception:  # noqa: BLE001
        return 0.0, "unavailable"


@router.get("/accounting")
async def accounting(since: str = "", until: str = "", days: int = 30,
                     usd_rub: float = 0.0, commission: float = 0.028):
    """
    Итоги за период по дате оплаты. usd_rub=0 — курс ЦБ на сегодня.

    Комиссия по умолчанию 2.8% — платёжная система 2.7% плюс ggsel 0.1%, как в ручной
    таблице. Если фактические удержания окажутся иными, передай параметром.
    """
    rate, rate_src = (usd_rub, "manual") if usd_rub > 0 else await _usd_rate()
    start, end, period_label = _period(since, until, days)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Order).where(Order.paid_at.isnot(None),
                                Order.paid_at >= start, Order.paid_at < end)
        )).scalars().all()

    delivered_n = at_risk_n = in_flight_n = unpaid_n = 0
    delivered_rev = delivered_cost_usd = 0.0
    at_risk_rev = at_risk_cost_usd = 0.0
    spent_usd_total = 0.0

    for o in rows:
        sale = float(o.amount_rub or 0)
        cost_usd = float(o.exec_price_usd or 0)
        spent_usd_total += cost_usd
        if sale <= 0:
            # precheck заводит заказ на каждую попытку покупки; оплаченные имеют сумму.
            unpaid_n += 1
            continue
        if o.delivery_status in _DELIVERED:
            delivered_n += 1
            delivered_rev += sale
            delivered_cost_usd += cost_usd
        elif o.delivery_status in _AT_RISK:
            at_risk_n += 1
            at_risk_rev += sale
            at_risk_cost_usd += cost_usd
        else:
            in_flight_n += 1

    delivered_cost_rub = delivered_cost_usd * rate
    fee_rub = delivered_rev * commission
    profit = delivered_rev - fee_rub - delivered_cost_rub
    margin = (profit / delivered_rev) if delivered_rev else 0.0

    return {
        "period": period_label,
        "usd_rub": round(rate, 4),
        "usd_rub_source": rate_src,
        "commission": commission,
        "delivered": {
            "orders": delivered_n,
            "revenue_rub": round(delivered_rev, 2),
            "ggsel_fee_rub": round(fee_rub, 2),
            "purchase_cost_usd": round(delivered_cost_usd, 3),
            "purchase_cost_rub": round(delivered_cost_rub, 2),
            "profit_rub": round(profit, 2),
            "margin": round(margin, 4),
            "avg_check_rub": round(delivered_rev / delivered_n, 2) if delivered_n else 0,
            "avg_profit_rub": round(profit / delivered_n, 2) if delivered_n else 0,
        },
        "at_risk": {
            "orders": at_risk_n,
            "revenue_held_rub": round(at_risk_rev, 2),
            "purchase_cost_rub": round(at_risk_cost_usd * rate, 2),
            "note": ("оплачено покупателем, но выдача не закрыта: деньги под возвратом, "
                     "а предмет, возможно, уже куплен"),
        },
        "in_flight_orders": in_flight_n,
        "unpaid_precheck_orders": unpaid_n,
        "total_spent_usd_incl_failed": round(spent_usd_total, 3),
        "caveats": [
            "у заказа хранится ОДНА цена выкупа: перекупы (rebuy-fresh) затирают "
            "предыдущую, поэтому реальные затраты выше показанных",
            "курс берётся текущий и применяется ко всем заказам периода, хотя покупались "
            "они по курсам своих дней",
            "комиссия считается по ставке из параметра, фактические удержания ggsel "
            "могут отличаться — сверь с вып