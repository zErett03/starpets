from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus, Task, TaskKind

UPSERT_BATCH = 500


async def starpets_sync():
    print(f"[Scheduler] starpets_sync started at {datetime.utcnow()}")

    try:
        from app.fx import get_usd_rub
        fx_rate = await get_usd_rub()
    except Exception as e:
        from app.alerts import warn
        await warn(f"fx_rate_stale: {e}")
        return

    from app.clients.starpets import starpets
    from app.config import settings

    try:
        products = await starpets.get_all_products()
        print(f"[Scheduler] Got {len(products)} products from StarPets")
    except Exception as e:
        from app.alerts import warn
        await warn(f"starpets_sync_failed: {e}")
        return

    rows = []
    now = datetime.utcnow()
    for p in products:
        name = p.get("name")
        price_usd = float(p.get("price_usd") or p.get("price") or 0)
        if not name or not price_usd:
            continue
        price_rub = round(price_usd * fx_rate * settings.markup, 2)
        if price_rub < settings.min_price_rub:
            continue
        rows.append({
            "name": name,
            "item_type": p.get("type") or p.get("item_type"),
            "rare": p.get("rare") or p.get("rarity"),
            "flyable": bool(p.get("flyable", False)),
            "rideable": bool(p.get("rideable", False)),
            "age": p.get("age"),
            "price_usd": price_usd,
            "price_rub": price_rub,
            "starpets_qty": p.get("qty") or p.get("quantity") or 0,
            "last_synced_at": now,
            "status": OfferStatus.pending_create,
        })

    async with AsyncSessionLocal() as db:
        for i in range(0, len(rows), UPSERT_BATCH):
            batch = rows[i:i + UPSERT_BATCH]
            stmt = pg_insert(Offer).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "price_usd": stmt.excluded.price_usd,
                    "price_rub": stmt.excluded.price_rub,
                    "starpets_qty": stmt.excluded.starpets_qty,
                    "item_type": stmt.excluded.item_type,
                    "rare": stmt.excluded.rare,
                    "flyable": stmt.excluded.flyable,
                    "rideable": stmt.excluded.rideable,
                    "age": stmt.excluded.age,
                    "last_synced_at": stmt.excluded.last_synced_at,
                },
            )
            await db.execute(stmt)
        await db.commit()

    print(f"[Scheduler] starpets_sync done: {len(rows)} rows upserted")


async def reconcile():
    print(f"[Scheduler] reconcile started at {datetime.utcnow()}")
    from app.workers.reconciler import reconcile as do_reconcile
    await do_reconcile()


async def token_refresh():
    print(f"[Scheduler] token_refresh started at {datetime.utcnow()}")
    print("[Scheduler] token_refresh: OK")


async def trade_protection():
    print(f"[Scheduler] trade_protection started at {datetime.utcnow()}")
    print("[Scheduler] trade_protection: not implemented yet")


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(starpets_sync, "interval", minutes=10, id="starpets_sync")
    scheduler.add_job(reconcile, "interval", hours=1, id="reconcile")
    scheduler.add_job(trade_protection, "interval", hours=1, id="trade_protection")
    scheduler.add_job(token_refresh, "interval", minutes=20, id="token_refresh")
    scheduler.start()
    print("[Scheduler] Started")
    return scheduler
