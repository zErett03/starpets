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

        product_id = offer.starpets_product_id
        if not product_id:
            raise RuntimeError(f"Offer {offer.id} has no starpets_product_id")

        max_price_usd = float(offer.price_usd or 10) * 1.10

        # 1. Buy cheapest item for this product within price limit
        buy_resp = await starpets.buy_by_product(
            product_id=product_id,
            max_price_usd=round(max_price_usd, 4),
        )
        purchased_items = buy_resp.get("items") or []
        purchased_item_ids = [str(i["id"]) for i in purchased_items if i.get("id")]

        if not purchased_item_ids:
            raise RuntimeError(f"Buy response has no items: {buy_resp}")

        order.starpets_purchase_id = ",".join(purchased_item_ids)
        order.starpets_status = buy_resp.get("status")
        order.max_price_usd = max_price_usd
        if purchased_items:
            order.exec_price_usd = float(purchased_items[0].get("price_usd") or 0) or None

        # 2. Create trade withdrawal with buyer's Roblox username
        trade_resp = await starpets.create_trade(
            purchased_item_ids=purchased_item_ids,
            roblox_username=order.roblox_username or "",
        )
        print(
            f"[Deliver] order_id={order_id} product_id={product_id} "
            f"purchased_item_ids={purchased_item_ids} trade_resp={trade_resp}",
            flush=True,
        )

        order.delivery_status = DeliveryStatus.dispatched

        db.add(Task(
            kind=TaskKind.MONITOR_DELIVERY,
            priority=20,
            payload={"order_id": order.id},
        ))

        await db.commit()
