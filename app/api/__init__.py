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
            "image_uri": p.get("imageUri") or p.get("image_uri") or p.get("image"),
            "starpets_product_id": p.get("id"),
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
                        "image_uri": stmt.excluded.image_uri,
                        "starpets_product_id": stmt.excluded.starpets_product_id,
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


@app.post("/test-webhook")
async def test_webhook(body: dict):
    from datetime import datetime
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, Order, DeliveryStatus

    ggsel_offer_id = body.get("ggsel_offer_id")
    roblox_username = body.get("roblox_username", "")

    if not ggsel_offer_id:
        return {"error": "ggsel_offer_id is required"}

    fake_order_id = int(datetime.utcnow().timestamp())

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Offer).where(Offer.ggsel_offer_id == ggsel_offer_id))
        offer = result.scalar_one_or_none()
        if not offer:
            return {"error": f"Offer with ggsel_offer_id={ggsel_offer_id} not found"}

        order = Order(
            ggsel_order_id=fake_order_id,
            offer_id=offer.id,
            item_name=offer.name,
            amount_rub=offer.price_rub,
            roblox_username=roblox_username,
            buyer_email="test@test.com",
            buyer_ip="127.0.0.1",
            starpets_custom_id=str(fake_order_id),
            delivery_status=DeliveryStatus.pending,
            paid_at=datetime.utcnow(),
        )
        db.add(order)
        await db.flush()
        order_id = order.id
        await db.commit()

    print(
        f"[test-webhook] order created: order_id={order_id} ggsel_order_id={fake_order_id} "
        f"offer={offer.name} roblox={roblox_username} — DELIVER task NOT queued (test mode)",
        flush=True,
    )

    return {
        "order_id": order_id,
        "ggsel_order_id": fake_order_id,
        "offer_id": offer.id,
        "offer_name": offer.name,
        "roblox_username": roblox_username,
        "delivery_status": "pending",
        "would_deliver": {
            "buy": f"POST /store/ex-buyers/products/buy item={offer.name}",
            "trade": f"POST /trades/ex-buyers/withdrawal roblox_username={roblox_username}",
        },
        "note": "DELIVER task NOT queued — test mode only",
    }


@app.get("/fix-webhooks")
async def fix_webhooks():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.config import settings

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer).where(
                Offer.status == OfferStatus.active,
                Offer.ggsel_offer_id.isnot(None),
            )
        )
        offers = result.scalars().all()

    updated, errors = [], []
    for offer in offers:
        gid = offer.ggsel_offer_id
        try:
            await ggsel_office.patch_offer(
                offer_id=gid,
                precheck_url=f"{settings.public_url}/hooks/ggsel/precheck/{gid}",
                notification_url=f"{settings.public_url}/hooks/ggsel/notification/{gid}",
            )
            updated.append(gid)
        except Exception as e:
            errors.append({"ggsel_offer_id": gid, "error": str(e)})

    return {"updated": len(updated), "offer_ids": updated, "errors": errors}


@app.get("/create-offers")
async def create_offers():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.workers.offer_creator import create_offer as _create_offer

    # Capture actual headers httpx sends on a real POST to /offers
    captured_request_headers: dict = {}

    async def _capture(request: httpx.Request) -> None:
        captured_request_headers.update(dict(request.headers))

    async with httpx.AsyncClient(
        headers=ggsel_office._headers(),
        event_hooks={"request": [_capture]},
        timeout=5,
    ) as probe:
        try:
            await probe.post(f"{SELLER_OFFICE_V2_URL}/offers", json={"_probe": True})
        except Exception:
            pass

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
        except httpx.HTTPStatusError as e:
            results.append({
                "offer_id": oid, "name": name, "status": "error",
                "http_status": e.response.status_code,
                "ggsel_response": e.response.text[:1000],
            })
            break
        except Exception as e:
            results.append({"offer_id": oid, "name": name, "status": "error", "error": str(e)})
            break

    return {
        "processed": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "request_headers": captured_request_headers,
        "results": results,
    }


@app.get("/ggsel-auth-probe")
async def ggsel_auth_probe():
    from app.config import settings
    key = settings.ggsel_api_key
    probe_url = f"{SELLER_OFFICE_V2_URL}/offers"
    schemes = [
        ("raw", {"Authorization": key, "Content-Type": "application/json"}),
        ("bearer", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}),
        ("x-api-key", {"X-Api-Key": key, "Content-Type": "application/json"}),
        ("api-key-header", {"Api-Key": key, "Content-Type": "application/json"}),
    ]
    results = []
    async with httpx.AsyncClient(timeout=10) as client:
        for name, headers in schemes:
            try:
                resp = await client.get(probe_url, headers=headers, params={"limit": 1})
                results.append({"scheme": name, "status": resp.status_code, "body": resp.text[:200]})
            except Exception as e:
                results.append({"scheme": name, "error": str(e)})
    return results


@app.get("/test-categories")
async def test_categories():
    headers = {"Authorization": ggsel_office._headers()["Authorization"]}
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        resp = await client.get(
            f"{SELLER_OFFICE_V2_URL}/categories",
            params={"parent_id": 122916},
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return {"status_code": resp.status_code, "body": body, "raw": resp.text[:500]}


@app.get("/test-categories-tree")
async def test_categories_tree():
    headers = {"Authorization": ggsel_office._headers()["Authorization"]}
    leaves = []

    async def fetch_children(parent_id: int, tree: list[str]) -> None:
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            resp = await client.get(
                f"{SELLER_OFFICE_V2_URL}/categories",
                params={"parent_id": parent_id},
            )
            resp.raise_for_status()
            data = resp.json()

        cats = data if isinstance(data, list) else (data.get("data") or data.get("categories") or [])
        for cat in cats:
            cat_id = cat.get("id")
            title = cat.get("title") or cat.get("title_ru") or cat.get("name") or str(cat_id)
            current_tree = tree + [title]
            if cat.get("has_children"):
                await fetch_children(cat_id, current_tree)
            else:
                leaves.append({"id": cat_id, "title": title, "tree": current_tree})

    await fetch_children(122916, ["Adopt Me"])
    return {"total": len(leaves), "leaves": leaves}


@app.get("/test-buy")
async def test_buy():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer

    # Find a product from DB with price < $3 that has starpets_product_id
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer)
            .where(
                Offer.starpets_product_id.isnot(None),
                Offer.price_usd < 3.0,
                Offer.price_usd > 0,
            )
            .order_by(Offer.price_usd.asc())
            .limit(10)
        )
        candidates = result.scalars().all()

    if not candidates:
        return {"error": "no offer with price_usd < $3 found in DB"}

    # Try candidates in price order until we find one with a live top item
    async with httpx.AsyncClient(timeout=15) as client:
        for offer in candidates:
            product_id = offer.starpets_product_id
            top_item = await starpets.get_top_item(client, str(product_id))
            if top_item:
                break
        else:
            return {"error": "no live top item found for any candidate offer"}

    item_id = top_item.get("id")
    price_usd = float(top_item.get("price_usd") or 0)

    # POST /store/ex-buyers/items/buy — buy the live item at its current price
    base = starpets._base_params()
    payload = {**base, "items": [{"id": item_id, "price": price_usd}]}

    async with httpx.AsyncClient(
        headers=starpets._headers(starpets._sign(payload)), timeout=15
    ) as client:
        resp = await client.post(
            f"{starpets.base_url}/store/ex-buyers/items/buy",
            json=payload,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    return {
        "offer": {"id": offer.id, "name": offer.name, "product_id": product_id},
        "item": {"id": item_id, "price_usd": price_usd},
        "payload_sent": payload,
        "status_code": resp.status_code,
        "response": body,
    }


@app.get("/test-friendship")
async def test_friendship(trade_id: int):
    params = {**starpets._base_params(), "tradeId": trade_id}

    async with httpx.AsyncClient(
        headers=starpets._headers(starpets._sign(params)), timeout=15
    ) as client:
        resp = await client.put(
            f"{starpets.base_url}/trades/ex-buyers/friendship",
            params=params,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    return {
        "trade_id": trade_id,
        "status_code": resp.status_code,
        "response": body,
    }


@app.get("/test-trade-status")
async def test_trade_status():
    from datetime import datetime, timezone
    today_ms = int(datetime(2026, 6, 17, tzinfo=timezone.utc).timestamp() * 1000)
    params = {**starpets._base_params(), "date": today_ms, "limit": 50}

    async with httpx.AsyncClient(
        headers=starpets._headers(starpets._sign(params)), timeout=15
    ) as client:
        resp = await client.get(
            f"{starpets.base_url}/ex-buyers/trades/updates",
            params=params,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    return {
        "params_sent": params,
        "status_code": resp.status_code,
        "response": body,
    }


@app.get("/test-trade")
async def test_trade():
    base = starpets._base_params()
    payload = {**base, "username": "withouq", "items": ["72341315"]}

    async with httpx.AsyncClient(
        headers=starpets._headers(starpets._sign(payload)), timeout=15
    ) as client:
        resp = await client.post(
            f"{starpets.base_url}/trades/ex-buyers/withdrawal",
            json=payload,
        )
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    return {
        "payload_sent": payload,
        "status_code": resp.status_code,
        "response": body,
    }


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
