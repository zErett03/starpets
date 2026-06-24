from datetime import datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, Offer, DeliveryStatus
from app.clients.ggsel import ggsel_office


async def mark_delivered(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        result2 = await db.execute(select(Offer).where(Offer.id == order.offer_id))
        offer = result2.scalar_one_or_none()

        await ggsel_office.mark_delivered(order.ggsel_order_id)
        order.delivery_status = DeliveryStatus.finalized
        order.ggsel_marked_delivered_at = datetime.utcnow()
        await db.commit()
        print(f"[MarkDelivered] order_id={order_id} finalized", flush=True)

        if offer and offer.ggsel_offer_id:
            try:
                await ggsel_office.set_quantity(offer.ggsel_offer_id, 1)
                print(
                    f"[MarkDelivered] quantity reset to 1 for ggsel_offer_id={offer.ggsel_offer_id}",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"[MarkDelivered] set_quantity failed ggsel_offer_id={offer.ggsel_offer_id}: {e}",
                    flush=True,
                )
