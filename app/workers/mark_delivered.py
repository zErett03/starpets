from datetime import datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, DeliveryStatus
from app.clients.ggsel import ggsel_office


async def mark_delivered(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        await ggsel_office.mark_delivered(order.ggsel_order_id)
        order.delivery_status = DeliveryStatus.finalized
        order.ggsel_marked_delivered_at = datetime.utcnow()
        await db.commit()
