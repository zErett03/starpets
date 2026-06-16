import httpx
from fastapi import FastAPI

from app.api.webhooks import router as webhooks_router
from app.clients.ggsel import SELLER_OFFICE_V2_URL, ggsel_office
from app.clients.starpets import starpets

app = FastAPI(title="starpets-layer")
app.include_router(webhooks_router)


@app.get("/")
async def status():
    return {"status": "ok"}


@app.get("/myip")
async def myip():
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get("https://api.ipify.org?format=json")
        resp.raise_for_status()
        return resp.json()


@app.get("/db-stats")
async def db_stats():
    from sqlalchemy import func, select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(func.count()).select_from(Offer))
        count = result.scalar()
    return {"offers": count}


@app.get("/test-sync")
async def test_sync():
    import asyncio
    from app.scheduler.jobs import starpets_sync_safe
    asyncio.create_task(starpets_sync_safe())
    return {"started": True}


@app.get("/test-products")
async def test_products():
    params = {
        **starpets._base_params(),
        "limit": 5,
    }
    async with httpx.AsyncClient(headers=starpets._headers(starpets._sign(params)), timeout=10) as client:
        resp = await client.get(
            f"{starpets.base_url}/products/ex-buyers/all-by-cursor",
            params=params,
        )
        return {
            "status_code": resp.status_code,
            "body": resp.json(),
        }


@app.get("/test-items")
async def test_items():
    params = {
        **starpets._base_params(),
        "limit": 50,
        "cursor": 0,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{starpets.base_url}/store/ex-buyers/items/all",
            headers=starpets._headers(starpets._sign(params)),
            params=params,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return {
            "status_code": resp.status_code,
            "body": body,
        }


@app.get("/test-sync-small")
async def test_sync_small():
    import asyncio
    from datetime import datetime
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.fx import get_usd_rub
    from app.config import settings

    fx_rate = await get_usd_rub()

    # Build price_map from first items page (guaranteed to have prices)
    price_map: dict = {}
    async for page in starpets.iter_items():
        for item in page:
            pid = item.get("productId")
            if not pid:
                continue
            price_usd = float(item.get("price_usd") or 0)
            if not price_usd:
                continue
            if pid not in price_map or price_usd < float(price_map[pid].get("price_usd") or 0):
                price_map[pid] = item
        break  # one page only

    # Take first 100 products that actually have a price
    all_products = await starpets.get_all_products()
    products = [p for p in all_products if p.get("id") in price_map][:100]

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
        priced_item = price_map.get(p.get("id"))
        if not priced_item:
            skipped_no_price += 1
            continue
        price_usd = float(priced_item.get("price_usd") or 0)
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
            "price_usd": price_usd,
            "price_rub": price_rub,
            "starpets_qty": p.get("qty") or p.get("quantity") or 0,
            "last_synced_at": now,
            "status": OfferStatus.pending_create,
        })

    db_error = None
    try:
        async with AsyncSessionLocal() as db:
            if rows:
                stmt = pg_insert(Offer).values(rows)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_offers_composite",
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
    except Exception as e:
        import traceback
        db_error = {"error": str(e), "traceback": traceback.format_exc()}

    return {
        "products_fetched": len(all_products),
        "products_with_price": len(products),
        "price_map_size": len(price_map),
        "rows_prepared": len(rows),
        "skipped_no_name": skipped_no_name,
        "skipped_no_price": skipped_no_price,
        "skipped_min_price": skipped_min_price,
        "fx_rate": fx_rate,
        "upserted": len(rows) if not db_error else 0,
        "db_error": db_error,
        "sample_row": rows[0] if rows else None,
    }


@app.get("/test-top-item")
async def test_top_item():
    # Find a productId that actually has items by peeking at the first items page
    product_id = None
    async for page in starpets.iter_items():
        for item in page:
            pid = item.get("productId")
            if pid:
                product_id = pid
                break
        break

    if not product_id:
        return {"error": "no items found in first page"}

    params = starpets._base_params()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{starpets.base_url}/store/ex-buyers/items/top/{product_id}",
            headers=starpets._headers(starpets._sign(params)),
            params=params,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return {"product_id": product_id, "status_code": resp.status_code, "body": body}


@app.get("/create-offers")
async def create_offers():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.workers.offer_creator import create_offer as _create_offer

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer)
            .where(Offer.status == OfferStatus.pending_create)
            .limit(5)
        )
        offers = result.scalars().all()
        offer_ids = [(o.id, o.name) for o in offers]

    results = []
    for oid, name in offer_ids:
        try:
            await _create_offer(oid)
            results.append({"offer_id": oid, "name": name, "status": "ok"})
        except Exception as e:
            results.append({"offer_id": oid, "name": name, "status": "error", "error": str(e)})

    return {
        "processed": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "results": results,
    }


@app.get("/test-categories")
async def test_categories():
    headers = {"Authorization": ggsel_office._headers()["Authorization"]}
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        resp = await client.get(
            f"{SELLER_OFFICE_V2_URL}/categories/search",
            params={"q": "Adopt Me"},
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return {"status_code": resp.status_code, "body": body, "raw": resp.text[:500]}


@app.get("/test-starpets")
async def test_starpets():
    params = starpets._base_params()
    async with httpx.AsyncClient(headers=starpets._headers(starpets._sign(params)), timeout=10) as client:
        resp = await client.get(
            f"{starpets.base_url}/ex-buyers/info/me",
            params=params,
        )
        return {
            "status_code": resp.status_code,
            "body": resp.json(),
        }
