from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, DeliveryStatus, Task, TaskKind


async def reconcile() -> None:
    async with AsyncSessionLocal() as db:
        cutoff = datetime.utcnow() - timedelta(hours=2)
        result = await db.execute(
            select(Order).where(
                Order.delivery_status == DeliveryStatus.dispatched,
                Order.updated_at < cutoff,
            )
        )
        stuck_orders = result.scalars().all()

        for order in stuck_orders:
            print(f"[Reconciler] Re-queuing MONITOR_DELIVERY for order {order.id}")
            db.add(Task(
                kind=TaskKind.MONITOR_DELIVERY,
                priority=20,
                payload={"order_id": order.id},
            ))

        await db.commit()
        print(f"[Reconciler] Done, {len(stuck_orders)} orders re-queued")
