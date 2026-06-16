from datetime import datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, Offer, Task, TaskKind, DeliveryStatus
from app.clients.starpets import starpets


async def deliver_order(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        result2 = await db.execute(select(Offer).where(Offer.id == order.offer_id))
        offer = result2.scalar_one_or_none()

        max_price_usd = float(offer.price_usd or 10) * 1.10

        # 1. Buy item from StarPets
        buy_resp = await starpets.buy_items(
            item_id=order.item_name,
            max_price_usd=max_price_usd,
            custom_id=str(order.ggsel_order_id),
        )
        purchase_id = buy_resp.get("id") or buy_resp.get("purchase_id")

        order.starpets_purchase_id = str(purchase_id) if purchase_id else None
        order.starpets_status = buy_resp.get("status")
        order.max_price_usd = max_price_usd

        # 2. Create trade withdrawal with buyer's Roblox username
        trade_resp = await starpets.create_trade(
            purchase_id=str(purchase_id),
            roblox_username=order.roblox_username or "",
        )
        print(
            f"[Deliver] order_id={order_id} purchase_id={purchase_id} "
            f"trade_resp={trade_resp}",
            flush=True,
        )

        order.delivery_status = DeliveryStatus.dispatched

        db.add(Task(
            kind=TaskKind.MONITOR_DELIVERY,
            priority=20,
            payload={"order_id": order.id},
        ))

        await db.commit()
