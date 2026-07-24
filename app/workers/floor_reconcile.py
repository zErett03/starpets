"""Floor reconciliation — closes the store_items -> offers gap that froze SKU prices.

The event feed only recomputes the floor for products that had an event in the batch (`affected`),
and `seed_store_items` refreshes store_items but never re-prices offers. So a product whose cheap
item vanished without a feed event (or whose store rows were only re-seeded) keeps a frozen, often
underpriced `offers.price_rub` forever. That turns the ggsel SKU card into an underpriced trap.

Two complementary passes:
  * sweep_floors  — cheap, DB-only. Recompute the robust floor from store_items for EVERY SKU-combo
                    offer (not just `affected`) and rewrite offers.price_usd/price_rub when drifted.
                    Phase 3 then propagates the corrected price to the SKU card. Runs often.
  * relive_active — live items/top pull for the products behind SHOWN (non-hidden) variants, so the
                    store_items source itself is refreshed (and phantoms deleted) at the origin.
                    Bounded + per-product throttled; rotates through the set. Runs less often.
"""
import asyncio
import time
from datetime import datetime

import httpx
from sqlalchemy import select, exists, update as sql_update, delete as sql_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import AsyncSessionLocal
from app.db.models import Offer, StoreItem, SkuVariant
from app.clients.starpets import starpets
from app.config import settings
from app.fx import get_usd_rub, calc_price_rub
from app.pricing import robust_floors_for

_DRIFT_RUB = 0.5              # rewrite the offer only when the RUB price moves at least this much
_sweep_running = False
_relive_running = False
_relive_last: dict = {}       # product_id -> monotonic ts of last live pull
_RELIVE_THROTTLE = 7200       # re-pull each product at most every 2h (rotate through the set)


async def sweep_floors(max_offers: int = 20000, dry_run: bool = False) -> dict:
    """DB-only: recompute the robust floor from store_items for all SKU-combo offers and rewrite
    offers.price_usd/price_rub where the price drifted. None floor (no buyable stock) is left to
    stock-sync / reconcile-stuck. No ggsel calls — Phase 3 pushes the corrected price to the card."""
    global _sweep_running
    if not dry_run:
        if _sweep_running:
            return {"skipped": "already running"}
        _sweep_running = True
    try:
        _is_sku = exists().where(SkuVariant.starpets_product_id == Offer.starpets_product_id)
        async with AsyncSessionLocal() as db:
            offers = (await db.execute(
                select(Offer.id, Offer.starpets_product_id, Offer.price_usd, Offer.price_rub)
                .where(Offer.starpets_product_id.isnot(None), _is_sku)
                .limit(max_offers)
            )).all()
            pids = [int(p) for (_i, p, _u, _r) in offers if p is not None]
            floors = await robust_floors_for(db, pids)
            fx = await get_usd_rub()

            checked = drifted = nostock = 0
            samples = []
            now = datetime.utcnow()
            for oid, pid, old_usd, old_rub in offers:
                checked += 1
                floor = floors.get(int(pid)) if pid is not None else None
                if floor is None:
                    nostock += 1
                    continue
                new_rub = calc_price_rub(floor, settings.markup, fx)
                if (abs(new_rub - float(old_rub or 0)) < _DRIFT_RUB
                        and abs(floor - float(old_usd or 0)) < 0.005):
                    continue
                drifted += 1
                if len(samples) < 50:
                    samples.append({"offer_id": oid, "product_id": int(pid),
                                    "old_rub": float(old_rub or 0), "new_rub": new_rub,
                                    "old_usd": float(old_usd or 0), "floor_usd": round(float(floor), 3)})
                if not dry_run:
                    await db.execute(sql_update(Offer).where(Offer.id == oid)
                                     .values(price_usd=floor, price_rub=new_rub, last_synced_at=now))
            if not dry_run:
                await db.commit()

        summary = {"dry_run": dry_run, "offers_checked": checked, "drifted": drifted,
                   "no_stock": nostock, "sample": samples[:30]}
        print(f"[FloorSweep] {summary}", flush=True)
        return summary
    finally:
        if not dry_run:
            _sweep_running = False


async def relive_active(max_products: int = 150, dry_run: bool = False) -> dict:
    """Live items/top pull for the products behind SHOWN (non-hidden) SKU variants. Truthfully
    refreshes store_items (upsert present + delete rows no longer live -> kills phantoms), then
    recomputes the robust floor and re-prices the offer. Bounded + per-product throttled."""
    global _relive_running
    if not dry_run:
        if _relive_running:
            return {"skipped": "already running"}
        _relive_running = True
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SkuVariant.starpets_product_id)
                .where(SkuVariant.hidden.is_(False)).distinct()
            )).all()
            offer_rows = (await db.execute(
                select(Offer.id, Offer.starpets_product_id).where(Offer.starpets_product_id.isnot(None))
            )).all()
        offer_by_pid = {int(p): oid for (oid, p) in offer_rows if p is not None}
        pids = sorted({int(r[0]) for r in rows if r[0] is not None})
        total = len(pids)
        if dry_run:
            return {"dry_run": True, "candidate_products": total, "sample": pids[:30]}

        fx = await get_usd_rub()
        now_m = time.monotonic()
        checked = refreshed = repriced = nostock = errors = skipped = 0
        samples = []
        from app.config import settings as _s
        _throttle = int(getattr(_s, "floor_relive_throttle_sec", 0) or _RELIVE_THROTTLE)
        async with httpx.AsyncClient(timeout=15) as http:
            for pid in pids:
                if checked >= max_products:
                    break
                if now_m - _relive_last.get(pid, 0.0) < _throttle:
                    skipped += 1
                    continue
                _relive_last[pid] = now_m
                checked += 1
                try:
                    params = starpets._base_params()
                    resp = await http.get(
                        f"{starpets.base_url}/store/ex-buyers/items/top/{pid}",
                        headers=starpets._headers(starpets._sign(params)),
                        params=params,
                    )
                    if not resp.is_success:
                        errors += 1
                        continue
                    items = resp.json().get("items") or []
                    now = datetime.utcnow()
                    fresh_ids = set()
                    vals = []
                    for it in items:
                        try:
                            iid = int(it.get("id"))
                        except (TypeError, ValueError):
                            continue
                        fresh_ids.add(iid)
                        vals.append({"id": iid, "product_id": pid,
                                     "price_usd": float(it.get("price_usd") or 0),
                                     "reserve_level": int(it.get("reserveLevel") or 0),
                                     "updated_at": now})
                    async with AsyncSessionLocal() as db:
                        if vals:
                            stmt = pg_insert(StoreItem).values(vals)
                            stmt = stmt.on_conflict_do_update(
                                index_elements=["id"],
                                set_={"product_id": stmt.excluded.product_id,
                                      "price_usd": stmt.excluded.price_usd,
                                      "reserve_level": stmt.excluded.reserve_level,
                                      "updated_at": stmt.excluded.updated_at},
                            )
                            await db.execute(stmt)
                        # truthful: drop rows for this product that are no longer live (phantoms)
                        if fresh_ids:
                            await db.execute(sql_delete(StoreItem).where(
                                StoreItem.product_id == pid, StoreItem.id.notin_(fresh_ids)))
                        else:
                            await db.execute(sql_delete(StoreItem).where(StoreItem.product_id == pid))
                        refreshed += 1

                        floor = (await robust_floors_for(db, [pid])).get(pid)
                        oid = offer_by_pid.get(pid)
                        if floor is None:
                            nostock += 1
                        elif oid:
                            new_rub = calc_price_rub(floor, settings.markup, fx)
                            off = (await db.execute(select(Offer).where(Offer.id == oid))).scalar_one_or_none()
                            if off:
                                changed = (abs(new_rub - float(off.price_rub or 0)) >= _DRIFT_RUB
                                           or abs(floor - float(off.price_usd or 0)) >= 0.005)
                                off.price_usd = floor
                                if changed:
                                    off.price_rub = new_rub
                                    off.last_synced_at = now
                                    repriced += 1
                                    if len(samples) < 50:
                                        samples.append({"product_id": pid, "floor_usd": round(float(floor), 3),
                                                        "new_rub": new_rub, "old_rub": float(off.price_rub or 0)})
                        await db.commit()
                except Exception as e:
                    errors += 1
                    print(f"[FloorRelive] pid={pid} failed: {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(0.2)

        summary = {"candidate_products": total, "checked": checked, "skipped_throttled": skipped,
                   "refreshed": refreshed, "repriced": repriced, "no_stock": nostock,
                   "errors": errors, "sample": samples[:30]}
        print(f"[FloorRelive] {summary}", flush=True)
        return summary
    finally:
        if not dry_run:
            _relive_running = False
