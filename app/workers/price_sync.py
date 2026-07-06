"""Event-driven price sync (StarPets /ex-buyers/updates feed).

Replaces the legacy 30-min top-per-product polling. A single worker reads the incremental
item event stream by cursor and mirrors OUR products' store items into `store_items`:
  event 0 CREATED  -> upsert items (only for our productIds)
  event 1 UPDATED  -> update price/reserveLevel of items we already track
  event 2 DELETED  -> remove items
After each batch we recompute the per-product floor (min price_usd among reserve_level==0
items) and push the new price to ggsel (or pause the card if nothing is available).

Seeding: `seed_store_items` does a one-time pull of current items per product (items/top)
so floors exist before the feed takes over.
"""
from datetime import datetime

import httpx
from sqlalchemy import select, func, update as sql_update, delete as sql_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus, StoreItem, KVState
from app.clients.starpets import starpets
from app.clients.ggsel import ggsel_office
from app.config import settings
from app.fx import get_usd_rub, calc_price_rub

_CURSOR_KEY = "items_cursor"
_PAGE = 50
_MAX_PAGES = 200          # safety cap per run: 200 * 50 = 10k events


async def _get_cursor(db) -> int | None:
    row = (await db.execute(select(KVState).where(KVState.key == _CURSOR_KEY))).scalar_one_or_none()
    if row and row.value:
        try:
            return int(row.value)
        except ValueError:
            return None
    return None


async def _set_cursor(db, value: int) -> None:
    row = (await db.execute(select(KVState).where(KVState.key == _CURSOR_KEY))).scalar_one_or_none()
    if row:
        row.value = str(value)
    else:
        db.add(KVState(key=_CURSOR_KEY, value=str(value)))


def _ints(seq) -> list[int]:
    out = []
    for x in seq or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


async def _apply_event(db, ev: dict, our_pids: set[int], affected: set[int]) -> None:
    etype = ev.get("event")
    data = ev.get("data")
    now = datetime.utcnow()

    if etype == 0:
        # created: data is a list of item dicts
        vals = []
        for it in (data if isinstance(data, list) else []):
            try:
                pid = int(it.get("productId"))
                iid = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            if pid not in our_pids:
                continue
            vals.append({
                "id": iid,
                "product_id": pid,
                "price_usd": float(it.get("price_usd") or 0),
                "reserve_level": int(it.get("reserveLevel") or 0),
                "updated_at": now,
            })
            affected.add(pid)
        if vals:
            stmt = pg_insert(StoreItem).values(vals)
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "product_id": stmt.excluded.product_id,
                    "price_usd": stmt.excluded.price_usd,
                    "reserve_level": stmt.excluded.reserve_level,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await db.execute(stmt)

    elif etype == 1:
        # updated: data = {"ids": [...], "data": {price_usd, reserveLevel}}
        d = data or {}
        ids = _ints(d.get("ids"))
        nd = d.get("data") or {}
        if not ids:
            return
        rows = (await db.execute(
            select(StoreItem.id, StoreItem.product_id).where(StoreItem.id.in_(ids))
        )).all()
        if not rows:
            return
        our_ids = [r[0] for r in rows]
        for r in rows:
            affected.add(r[1])
        upd = {"updated_at": now}
        if nd.get("price_usd") is not None:
            upd["price_usd"] = float(nd.get("price_usd") or 0)
        if nd.get("reserveLevel") is not None:
            upd["reserve_level"] = int(nd.get("reserveLevel") or 0)
        await db.execute(sql_update(StoreItem).where(StoreItem.id.in_(our_ids)).values(**upd))

    elif etype == 2:
        # deleted: data is a list of ids
        ids = _ints(data if isinstance(data, list) else [])
        if not ids:
            return
        rows = (await db.execute(
            select(StoreItem.id, StoreItem.product_id).where(StoreItem.id.in_(ids))
        )).all()
        if not rows:
            return
        our_ids = [r[0] for r in rows]
        for r in rows:
            affected.add(r[1])
        await db.execute(sql_delete(StoreItem).where(StoreItem.id.in_(our_ids)))


async def sync_item_updates() -> dict:
    """One incremental pass of the /ex-buyers/updates feed. Safe to call on a short interval."""
    # 1. Map our products -> offer
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Offer.id, Offer.starpets_product_id, Offer.ggsel_offer_id,
                   Offer.status, Offer.price_rub)
            .where(Offer.starpets_product_id.isnot(None), Offer.ggsel_offer_id.isnot(None))
        )).all()
    our = {
        int(pid): {"offer_id": oid, "ggsel_id": gid, "status": st, "price_rub": float(pr or 0)}
        for (oid, pid, gid, st, pr) in rows if pid is not None
    }
    our_pids = set(our.keys())
    if not our_pids:
        return {"events": 0, "affected": 0, "note": "no offers"}

    # 2. Consume the event stream, mirror our items, collect affected products
    affected: set[int] = set()
    events = 0
    async with AsyncSessionLocal() as db:
        cursor = await _get_cursor(db)
        new_cursor = cursor
        pages = 0
        while pages < _MAX_PAGES:
            try:
                batch = await starpets.get_item_updates(limit=_PAGE, cursor=new_cursor)
            except Exception as e:
                import traceback
                print(
                    f"[PriceSync] get_item_updates error: {type(e).__name__}: {e!r}\n"
                    f"{traceback.format_exc()}",
                    flush=True,
                )
                break
            if not batch:
                break
            for ev in batch:
                await _apply_event(db, ev, our_pids, affected)
                events += 1
                try:
                    eid = int(ev.get("id"))
                    new_cursor = eid if new_cursor is None else max(new_cursor, eid)
                except (TypeError, ValueError):
                    pass
            pages += 1
            if len(batch) < _PAGE:
                break
        if new_cursor is not None and new_cursor != cursor:
            await _set_cursor(db, new_cursor)
        await db.commit()

    if not affected:
        return {"events": events, "affected": 0}

    # 3. Recompute floor for affected products and push price / pause to ggsel
    fx = await get_usd_rub()
    updated = paused = 0
    async with AsyncSessionLocal() as db:
        for pid in affected:
            info = our.get(pid)
            if not info:
                continue
            floor = (await db.execute(
                select(func.min(StoreItem.price_usd)).where(
                    StoreItem.product_id == pid,
                    StoreItem.reserve_level == 0,
                )
            )).scalar()
            offer = (await db.execute(select(Offer).where(Offer.id == info["offer_id"]))).scalar_one_or_none()
            if not offer:
                continue

            if floor is None:
                # no available stock -> pause the card if it's live
                if offer.status == OfferStatus.active:
                    try:
                        await ggsel_office.pause_offer(offer.ggsel_offer_id)
                        offer.status = OfferStatus.paused
                        paused += 1
                    except Exception as e:
                        print(f"[PriceSync] pause error product={pid}: {e}", flush=True)
                continue

            floor = float(floor)
            new_rub = calc_price_rub(floor, settings.markup, fx)
            offer.price_usd = floor
            if abs(new_rub - float(offer.price_rub or 0)) >= 0.01:
                offer.price_rub = new_rub
                # Push to ggsel only for live cards; paused cards keep the fresh DB price and
                # get it pushed at activation time.
                if offer.status == OfferStatus.active:
                    try:
                        await ggsel_office.update_price(offer.ggsel_offer_id, new_rub)
                        updated += 1
                    except Exception as e:
                        print(f"[PriceSync] update_price error product={pid}: {e}", flush=True)
        await db.commit()

    print(
        f"[PriceSync] done — events={events} affected={len(affected)} "
        f"price_updated={updated} paused={paused}",
        flush=True,
    )
    return {"events": events, "affected": len(affected), "updated": updated, "paused": paused}


async def seed_store_items(limit: int = 0) -> dict:
    """One-time seed of store_items: pull current items (items/top) for each of our products
    so floors exist before the event feed takes over. Idempotent (upsert)."""
    import asyncio

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Offer.starpets_product_id).where(
                Offer.starpets_product_id.isnot(None),
                Offer.ggsel_offer_id.isnot(None),
            )
        )).all()
    pids = sorted({int(r[0]) for r in rows if r[0] is not None})
    if limit and limit > 0:
        pids = pids[:limit]

    total = len(pids)
    conc = settings.sync_concurrency
    print(f"[SeedStoreItems] starting — {total} products (concurrency={conc})", flush=True)
    counters = {"products": 0, "items": 0, "errors": 0}
    sem = asyncio.Semaphore(conc)

    async def _seed(http, pid):
        async with sem:
            try:
                params = starpets._base_params()
                resp = await http.get(
                    f"{starpets.base_url}/store/ex-buyers/items/top/{pid}",
                    headers=starpets._headers(starpets._sign(params)),
                    params=params,
                )
                if not resp.is_success:
                    counters["errors"] += 1
                    return
                items = resp.json().get("items") or []
                vals = []
                now = datetime.utcnow()
                for it in items:
                    try:
                        iid = int(it.get("id"))
                    except (TypeError, ValueError):
                        continue
                    vals.append({
                        "id": iid,
                        "product_id": pid,
                        "price_usd": float(it.get("price_usd") or 0),
                        "reserve_level": int(it.get("reserveLevel") or 0),
                        "updated_at": now,
                    })
                if vals:
                    async with AsyncSessionLocal() as db:
                        stmt = pg_insert(StoreItem).values(vals)
                        stmt = stmt.on_conflict_do_update(
                            index_elements=["id"],
                            set_={
                                "product_id": stmt.excluded.product_id,
                                "price_usd": stmt.excluded.price_usd,
                                "reserve_level": stmt.excluded.reserve_level,
                                "updated_at": stmt.excluded.updated_at,
                            },
                        )
                        await db.execute(stmt)
                        await db.commit()
                    counters["items"] += len(vals)
                counters["products"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"[SeedStoreItems] product={pid} error: {e}", flush=True)

    _limits = httpx.Limits(max_connections=conc + 5)
    async with httpx.AsyncClient(timeout=15, limits=_limits) as http:
        for i in range(0, total, 500):
            await asyncio.gather(*[_seed(http, pid) for pid in pids[i:i + 500]])
            print(
                f"[SeedStoreItems] progress {min(i + 500, total)}/{total} "
                f"products={counters['products']} items={counters['items']} errors={counters['errors']}",
                flush=True,
            )
    print(
        f"[SeedStoreItems] done — products={counters['products']} items={counters['items']} "
        f"errors={counters['errors']} total={total}",
        flush=True,
    )
    return counters
