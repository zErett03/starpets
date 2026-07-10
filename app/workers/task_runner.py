import asyncio
import uuid

from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Task, TaskKind, TaskStatus

WORKER_ID = str(uuid.uuid4())


async def pop_task(db) -> Task | None:
    # Honour scheduled_at: a task rescheduled after a failure (scheduled_at = now + backoff)
    # must WAIT for its delay. Without this filter pop_task grabbed it immediately, retrying
    # 6× in one second (no backoff) — which raced cancel+create_trade and locked the item.
    result = await db.execute(
        select(Task)
        .where(
            Task.status == TaskStatus.pending,
            Task.scheduled_at <= datetime.utcnow(),
        )
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()


async def handle_task(task) -> None:
    payload = task.payload or {}

    match task.kind:
        case TaskKind.CREATE_OFFER:
            from app.workers.offer_creator import create_offer
            await create_offer(payload["offer_id"])
        case TaskKind.DELIVER:
            from app.workers.deliver import deliver_order
            await deliver_order(
                payload["order_id"],
                attempt=getattr(task, "attempts", 1),
                max_attempts=getattr(task, "max_attempts", 1),
            )
        case TaskKind.MONITOR_DELIVERY:
            from app.workers.monitor_delivery import monitor_delivery
            await monitor_delivery(payload["order_id"])
        case TaskKind.MARK_DELIVERED:
            from app.workers.mark_delivered import mark_delivered
            await mark_delivered(payload["order_id"])
        case TaskKind.UPDATE_PRICE_BATCH:
            pass
        case TaskKind.TOGGLE_STATUS_BATCH:
            pass
        case TaskKind.TRADE_WATCH:
            from app.workers.trade_watch import trade_watch
            await trade_watch(payload["order_id"])
        case _:
            raise ValueError(f"Unknown task kind: {task.kind}")


async def run_worker() -> None:
    print(f"[Worker] Started, id={WORKER_ID}")

    while True:
        try:
            task_id = task_kind = task_payload = task_attempts = task_max_attempts = None

            async with AsyncSessionLocal() as db:
                async with db.begin():
                    task = await pop_task(db)
                    if not task:
                        await asyncio.sleep(1)
                        continue

                    print(f"[Worker] Got task id={task.id} kind={task.kind}")
                    task.status = TaskStatus.processing
                    task.locked_by = WORKER_ID
                    task.locked_at = datetime.utcnow()
                    task.attempts += 1
                    task_id = task.id
                    task_kind = task.kind
                    task_payload = task.payload
                    task_attempts = task.attempts
                    task_max_attempts = task.max_attempts

            class TaskProxy:
                def __init__(self):
                    self.id = task_id
                    self.kind = task_kind
                    self.payload = task_payload
                    self.attempts = task_attempts
                    self.max_attempts = task_max_attempts

            try:
                await handle_task(TaskProxy())
                async with AsyncSessionLocal() as db2:
                    async with db2.begin():
                        result = await db2.execute(select(Task).where(Task.id == task_id))
                        t = result.scalar_one()
                        t.status = TaskStatus.done
                        t.updated_at = datetime.utcnow()
                        print(f"[Worker] Task {task_id} ({task_kind}) done")
            except Exception as e:
                print(f"[Worker] Task {task_id} ({task_kind}) failed: {e}")
                import traceback
                traceback.print_exc()
                async with AsyncSessionLocal() as db2:
                    async with db2.begin():
                        result = await db2.execute(select(Task).where(Task.id == task_id))
                        t = result.scalar_one()
                        t.last_error = str(e)
                        if task_attempts >= task_max_attempts:
                            t.status = TaskStatus.failed
                        else:
                            delays = [2, 5, 15, 30]
                            delay = delays[min(task_attempts - 1, len(delays) - 1)]
                            t.status = TaskStatus.pending
                            t.scheduled_at = datetime.utcnow() + timedelta(minutes=delay)
        except Exception as e:
            print(f"[Worker] Loop error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run_worker())
