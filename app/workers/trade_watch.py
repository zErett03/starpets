from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, DeliveryStatus
from app.alerts import warn


async def trade_watch(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        if order.delivery_status == DeliveryStatus.done:
            order.delivery_status = DeliveryStatus.finalized
            await db.commit()
            print(f"[TradeWatch] Order {order_id} finalized")
        else:
            await warn(f"trade_watch: order {order_id} in unexpected status {order.delivery_status}")
