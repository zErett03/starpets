from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus, Task, TaskKind

UPSERT_BATCH = 500


async def starpets_sync() -> dict:
    print(f"[Scheduler] starpets_sync started at {datetime.utcnow()}")

    from app.fx import get_usd_rub
    fx_rate = await get_usd_rub()

    from app.clients.starpets import starpets
    from app.config import settings

    products = await starpets.get_all_products()
    print(f"[Scheduler] Got {len(products)} products from StarPets")

    # Build price lookup by productId page-by-page to avoid loading all items into memory
    price_map: dict[str, dict] = {}
    items_fetched = 0
    sample_item = None
    async for page in starpets.iter_items():
        for item in page:
            if sample_item is None:
                sample_item = item
            items_fetched += 1
            pid = item.get("productId")
            if not pid:
                continue
            price_usd = float(item.get("price_usd") or 0)
            if not price_usd:
                continue
            if pid not in price_map or price_usd < float(price_map[pid].get("price_usd") or 0):
                price_map[pid] = item
    print(f"[Scheduler] Got {items_fetched} items (with prices) from StarPets")

    rows = []
    skipped_no_name = 0
    skipped_no_price = 0
    skipped_min_price = 0
    now = datetime.utcnow()
    for p in products:
        name = p.get("name")
        if not name:
            skipped_no_name += 1
            continue
        product_id = p.get("id")
        priced_item = price_map.get(product_id)
        if not priced_item:
            skipped_no_price += 1
            continue
        price_usd = float(priced_item.get("price_usd") or 0)
        price_rub_api = float(priced_item.get("price_rub") or 0)
        price_rub = round(price_usd * fx_rate * settings.markup, 2)
        if price_rub < settings.min_price_rub:
            skipped_min_price += 1
            continue
        rows.append({
            "name": name,
            "item_type": p.get("type") or p.get("item_type"),
            "rare": p.get("rare") or p.get("rarity"),
            "flyable": bool(p.get("flyable", False)),
            "rideable": bool(p.get("rideable", False)),
            "age": p.get("age"),
            "image_uri": p.get("imageUri") or p.get("image_uri") or p.get("image"),
            "starpets_product_id": product_id,
            "price_usd": price_usd,
            "price_rub": price_rub,
            "starpets_qty": p.get("qty") or p.get("quantity") or 0,
            "last_synced_at": now,
            "status": OfferStatus.pending_create,
        })

    diag = {
        "products_fetched": len(products),
        "items_fetched": items_fetched,
        "items_with_price": len(price_map),
        "rows_prepared": len(rows),
        "skipped_no_name": skipped_no_name,
        "skipped_no_price": skipped_no_price,
        "skipped_min_price": skipped_min_price,
        "fx_rate": fx_rate,
        "markup": settings.markup,
        "min_price_rub": settings.min_price_rub,
        "sample_product": products[0] if products else None,
        "sample_item": sample_item,
    }
    print(f"[Scheduler] Prepared {len(rows)} rows for upsert, diag={diag}")

    try:
        async with AsyncSessionLocal() as db:
            for i in range(0, len(rows), UPSERT_BATCH):
                batch = rows[i:i + UPSERT_BATCH]
                stmt = pg_insert(Offer).values(batch)
                # When event_price_sync is on, prices are owned by the event feed + the robust
                # floor sweep. starpets_sync must NOT clobber them: its per-product min (incl.
                # reserved items, no outlier rejection) re-introduces bait/phantom prices and
                # fights the sweep. Catalog fields are always refreshed.
                _set = {
                    "starpets_qty": stmt.excluded.starpets_qty,
                    "item_type": stmt.excluded.item_type,
                    "rare": stmt.excluded.rare,
                    "flyable": stmt.excluded.flyable,
                    "rideable": stmt.excluded.rideable,
                    "age": stmt.excluded.age,
                    "image_uri": stmt.excluded.image_uri,
                    "starpets_product_id": stmt.excluded.starpets_product_id,
                    "last_synced_at": stmt.excluded.last_synced_at,
                }
                if not settings.event_price_sync:
                    _set["price_usd"] = stmt.excluded.price_usd
                    _set["price_rub"] = stmt.excluded.price_rub
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_offers_composite",
                    set_=_set,
                )
                await db.execute(stmt)
            await db.commit()
        print(
            f"[Scheduler] starpets_sync done: products={len(products)} "
            f"items_fetched={items_fetched} price_map={len(price_map)} upserted={len(rows)}"
        )
    except Exception as e:
        import traceback
        diag["db_error"] = str(e)
        diag["db_traceback"] = traceback.format_exc()
        print(f"[Scheduler] starpets_sync DB error: {e}\n{diag['db_traceback']}")
        return diag

    return diag


async def starpets_sync_safe():
    try:
        await starpets_sync()
    except Exception as e:
        print(f"[Scheduler] starpets_sync error: {e}", flush=True)
        from app.alerts import warn
        await warn(f"starpets_sync_failed: {e}")


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


async def monitor_delivery_safe():
    try:
        from app.workers.monitor_delivery import monitor_all_deliveries
        await monitor_all_deliveries()
    except Exception as e:
        print(f"[Scheduler] monitor_delivery error: {e}", flush=True)


async def sync_prices_safe():
    from app.config import settings
    if settings.event_price_sync:
        return  # legacy top-per-product polling disabled — event feed keeps prices live
    try:
        from app.api import _run_sync_prices
        await _run_sync_prices()
    except Exception as e:
        print(f"[Scheduler] sync_prices error: {e}", flush=True)


async def price_sync_safe():
    from app.config import settings
    if not settings.event_price_sync:
        return  # event-driven price sync not enabled yet (seed store_items, then set EVENT_PRICE_SYNC)
    try:
        from app.workers.price_sync import sync_item_updates
        await sync_item_updates()
    except Exception as e:
        print(f"[Scheduler] price_sync error: {e}", flush=True)


async def sku_price_sync_safe():
    from app.config import settings
    if not settings.sku_price_sync:
        return  # Phase 3 disabled until validated (set SKU_PRICE_SYNC=true)
    try:
        from app.workers.sku_price_sync import sku_price_sync
        # scheduled pass ignores copeck-churn (FX/floor micro-shifts); manual endpoint keeps
        # its sensitive defaults for diagnostics. Big traps still caught well under 50₽/10%.
        await sku_price_sync(threshold_rub=50.0, threshold_pct=0.10)
    except Exception as e:
        print(f"[Scheduler] sku_price_sync error: {e}", flush=True)


async def sku_stock_sync_safe():
    from app.config import settings
    if not settings.sku_stock_sync:
        return  # variant hiding disabled until validated (set SKU_STOCK_SYNC=true)
    try:
        from app.workers.sku_stock_sync import sku_stock_sync
        await sku_stock_sync()
    except Exception as e:
        print(f"[Scheduler] sku_stock_sync error: {e}", flush=True)


async def reconcile_stuck_safe():
    from app.config import settings
    if not settings.sku_price_sync:
        return  # tied to Phase 3 (keeps SKU prices correct); runs when SKU_PRICE_SYNC=true
    try:
        from app.workers.reconcile_stuck import reconcile_stuck_offers
        await reconcile_stuck_offers()
    except Exception as e:
        print(f"[Scheduler] reconcile_stuck error: {e}", flush=True)


async def floor_sweep_safe():
    from app.config import settings
    if not settings.floor_reconcile:
        return  # DB-only floor sweep (fixes frozen offers.price_rub); set FLOOR_RECONCILE=true
    try:
        from app.workers.floor_reconcile import sweep_floors
        await sweep_floors()
    except Exception as e:
        print(f"[Scheduler] floor_sweep error: {e}", flush=True)


async def floor_relive_safe():
    from app.config import settings
    if not settings.floor_reconcile:
        return  # live items/top relive of shown variants (refreshes store_items at the source)
    try:
        from app.workers.floor_reconcile import relive_active
        await relive_active()
    except Exception as e:
        print(f"[Scheduler] floor_relive error: {e}", flush=True)


async def resync_missing_safe():
    from app.config import settings
    if not settings.sku_price_sync:
        return  # part of keeping SKU cards correct; runs when SKU_PRICE_SYNC=true
    try:
        from app.workers.resync_missing import resync_missing_images
        await resync_missing_images()
    except Exception as e:
        print(f"[Scheduler] resync_missing error: {e}", flush=True)


_tg_alerted_ids: set = set()
_tg_seeded = False


async def tg_alerts_safe():
    """Push a Telegram alert when an order NEWLY enters needs_attention/failed. Seeds silently on
    first run (so existing problems don't spam after a redeploy), then alerts only new ones."""
    from app.config import settings
    if not (settings.telegram_admin_ids and settings.telegram_bot_token
            and settings.telegram_bot_token != "dummy"):
        return
    global _tg_seeded
    try:
        from sqlalchemy import select
        from app.db import AsyncSessionLocal
        from app.db.models import Order, DeliveryStatus
        from app.telegram.bot import notify_problem
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Order).where(Order.delivery_status.in_(
                    [DeliveryStatus.needs_attention, DeliveryStatus.failed]))
                .order_by(Order.id.desc()).limit(100)
            )).scalars().all()
            current = {o.id for o in rows}
            if not _tg_seeded:
                _tg_alerted_ids.update(current)
                _tg_seeded = True
                return
            for o in rows:
                if o.id not in _tg_alerted_ids:
                    await notify_problem(o)
        _tg_alerted_ids.clear()
        _tg_alerted_ids.update(current)
    except Exception as e:
        print(f"[Scheduler] tg_alerts error: {e}", flush=True)


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(starpets_sync_safe, "interval", minutes=10, id="starpets_sync")
    scheduler.add_job(reconcile, "interval", hours=1, id="reconcile")
    scheduler.add_job(trade_protection, "interval", hours=1, id="trade_protection")
    scheduler.add_job(token_refresh, "interval", minutes=20, id="token_refresh")
    scheduler.add_job(monitor_delivery_safe, "interval", seconds=30, id="monitor_delivery")
    scheduler.add_job(sync_prices_safe, "interval", minutes=30, id="sync_prices")
    scheduler.add_job(price_sync_safe, "interval", seconds=15, id="price_sync")
    scheduler.add_job(sku_price_sync_safe, "interval", minutes=5, id="sku_price_sync")
    scheduler.add_job(sku_stock_sync_safe, "interval", minutes=10, id="sku_stock_sync")
    scheduler.add_job(reconcile_stuck_safe, "interval", minutes=30, id="reconcile_stuck")
    scheduler.add_job(floor_sweep_safe, "interval", minutes=10, id="floor_sweep")
    scheduler.add_job(floor_relive_safe, "interval", minutes=15, id="floor_relive")
    scheduler.add_job(resync_missing_safe, "interval", hours=12, id="resync_missing")
    scheduler.add_job(tg_alerts_safe, "interval", minutes=2, id="tg_alerts")
    scheduler.start()
    print("[Scheduler] Started")
    return scheduler
