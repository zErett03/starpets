"""Live-reconcile 'stuck' SKU-variant offers.

The event feed keeps store_items broadly fresh, but occasionally misses re-listing events for a
specific product: its cheap items sold, the offer got paused (floor became None), price froze — and
the new (pricier) listings never landed in store_items. Such an offer is: SKU variant + paused +
ZERO store_items rows. This worker live-checks those via get_top_item; if StarPets actually has
stock, it seeds one StoreItem (so stock-sync shows the variant again) and refreshes offers.price_rub
from the live floor + unpauses. Phase 3 then pushes the corrected price to the SKU card.
"""
import asyncio
import time
from datetime import datetime

import httpx
from sqlalchemy import select, exists, not_

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus, StoreItem, SkuVariant
from app.clients.starpets import starpets
from app.fx import get_usd_rub
from app.config import settings

_running = False
_last_checked: dict = {}        # offer_id -> monotonic ts of last live-check
_THROTTLE_SEC = 3600           # re-check each candidate at most hourly (skip genuinely-OOS churn)


def _price_rub(price_usd: float, fx: float) -> float:
    return max(round(price_usd * fx * settings.markup, 2), settings.min_price_rub)


async def reconcile_stuck_offers(max_offers: int = 100, dry_run: bool = False) -> dict:
    global _running
    if not dry_run:
        if _running:
            return {"skipped": "already running"}
        _running = True
    try:
        _has_store = exists().where(StoreItem.product_id == Offer.starpets_product_id)
        _is_sku = exists().where(SkuVariant.starpets_product_id == Offer.starpets_product_id)
        async with AsyncSessionLocal() as db:
            stuck = (await db.execute(
                select(Offer.id, Offer.starpets_product_id, Offer.price_rub)
                .where(Offer.status == OfferStatus.paused,
                       Offer.starpets_product_id.isnot(None), _is_sku, not_(_has_store))
                .limit(5000)
            )).all()

        total = len(stuck)
        if dry_run:
            return {"dry_run": True, "stuck_candidates": total,
                    "sample": [{"offer_id": o, "product_id": p, "old_price_rub": float(pr or 0)}
                               for (o, p, pr) in stuck[:30]]}

        fx = await get_usd_rub()
        revived = still_oos = errors = checked = skipped = 0
        results = []
        now_m = time.monotonic()
        async with httpx.AsyncClient(timeout=10) as http:
            for off_id, pid, old_pr in stuck:
                if checked >= max_offers:
                    break
                if now_m - _last_checked.get(off_id, 0.0) < _THROTTLE_SEC:
                    skipped += 1
                    continue  # checked recently -> let the next run advance to unchecked ones
                _last_checked[off_id] = now_m
                checked += 1
                try:
                    top = await starpets.get_top_item(http, str(pid))
                    if not top:
                        still_oos += 1
                        continue
                    item_id = int(top.get("id"))
                    price_usd = float(top.get("price_usd") or 0)
                    new_pr = _price_rub(price_usd, fx)
                    now = datetime.utcnow()
                    async with AsyncSessionLocal() as db:
                        # seed store_items so stock-sync sees it in stock again
                        si = (await db.execute(select(StoreItem).where(StoreItem.id == item_id))).scalar_one_or_none()
                        if si:
                            si.product_id = pid; si.price_usd = price_usd; si.reserve_level = 0; si.updated_at = now
                        else:
                            db.add(StoreItem(id=item_id, product_id=pid, price_usd=price_usd,
                                             reserve_level=0, updated_at=now))
                        off = (await db.execute(select(Offer).where(Offer.id == off_id))).scalar_one()
                        off.price_rub = new_pr; off.price_usd = price_usd
                        off.status = OfferStatus.active; off.last_synced_at = now
                        await db.commit()
                    revived += 1
                    results.append({"offer_id": off_id, "product_id": pid,
                                    "old_price_rub": float(old_pr or 0), "new_price_rub": new_pr,
                                    "live_usd": price_usd})
                except Exception as e:
                    errors += 1
                    print(f"[ReconcileStuck] offer={off_id} pid={pid} failed: {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(0.2)

        summary = {"stuck_candidates": total, "checked": checked, "skipped_throttled": skipped,
                   "revived": revived, "still_oos": still_oos, "errors": errors, "sample": results[:30]}
        print(f"[ReconcileStuck] {summary}", flush=True)
        return summary
    finally:
        if not dry_run:
            _running = False
_last_checked: dict = {}        # offer_id -> monotonic ts of last live-check
_THROTTLE_SEC = 3600           # re-check each candidate at most hourly (skip genuinely-OOS churn)
