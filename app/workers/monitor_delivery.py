from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, Task, TaskKind, DeliveryStatus
from app.clients.starpets import starpets
from app.clients.ggsel import ggsel_office

_TERMINAL_FAILED = {"FAILED", "EXPIRED", "CANCELLED"}


async def monitor_delivery(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        resp = await starpets.get_trade_updates(custom_id=order.starpets_custom_id)
        status = resp.get("status", "")
        order.starpets_status = status

        if status == "DONE":
            await ggsel_office.mark_delivered(order.ggsel_order_id)
            order.delivery_status = DeliveryStatus.done
            order.delivered_at = datetime.utcnow()
            db.add(Task(
                kind=TaskKind.TRADE_WATCH,
                priority=20,
                payload={"order_id": order.id},
                scheduled_at=datetime.utcnow() + timedelta(days=7, hours=12),
            ))
        elif status in _TERMINAL_FAILED:
            order.delivery_status = DeliveryStatus.failed
        else:
            db.add(Task(
                kind=TaskKind.MONITOR_DELIVERY,
                priority=20,
                payload={"order_id": order.id},
                scheduled_at=datetime.utcnow() + timedelta(minutes=5),
            ))

        await db.commit()
