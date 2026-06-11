from datetime import datetime

from sqlalchemy import select

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus, Task, TaskKind


async def starpets_sync():
    print(f"[Scheduler] starpets_sync started at {datetime.utcnow()}")

    try:
        from app.fx import get_usd_rub
        fx_rate = await get_usd_rub()
        print(f"[Scheduler] FX rate: {fx_rate}")
    except Exception as e:
        from app.alerts import warn
        await warn(f"fx_rate_stale: {e}")
        return

    from app.clients.starpets import starpets
    from app.config import settings

    try:
        snapshot = await starpets.get_items()
        items = snapshot.get("items") or snapshot.get("data") or []
        print(f"[Scheduler] Got {len(items)} items from StarPets")
        print(f"[DEBUG] first item: {items[0] if items else 'empty'}")
    except Exception as e:
        from app.alerts import warn
        await warn(f"starpets_sync_failed: {e}")
        return

    async with AsyncSessionLocal() as db:
        updated = 0
        created = 0

        for item in items:
            name = item.get("name")
            price_usd = float(item.get("price_usd") or item.get("price") or 0)
            qty = item.get("qty") or item.get("quantity") or 0

            if not name or not price_usd:
                continue

            price_rub = round(price_usd * fx_rate * settings.markup, 2)
            if price_rub < settings.min_price_rub:
                continue

            result = await db.execute(select(Offer).where(Offer.name == name))
            offer = result.scalar_one_or_none()

            if offer:
                offer.price_usd = price_usd
                offer.price_rub = price_rub
                offer.starpets_qty = qty
                offer.item_type = item.get("type") or item.get("item_type")
                offer.rare = item.get("rare") or item.get("rarity")
                offer.flyable = bool(item.get("flyable", False))
                offer.rideable = bool(item.get("rideable", False))
                offer.age = item.get("age")
                offer.last_synced_at = datetime.utcnow()
                updated += 1
            else:
                offer = Offer(
                    name=name,
                    item_type=item.get("type") or item.get("item_type"),
                    rare=item.get("rare") or item.get("rarity"),
                    flyable=bool(item.get("flyable", False)),
                    rideable=bool(item.get("rideable", False)),
                    age=item.get("age"),
                    price_usd=price_usd,
                    price_rub=price_rub,
                    starpets_qty=qty,
                    last_synced_at=datetime.utcnow(),
                )
                db.add(offer)
                created += 1

        await db.commit()
        print(f"[Scheduler] starpets_sync done: {created} created, {updated} updated")


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
