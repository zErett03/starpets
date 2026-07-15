import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from app.api.webhooks import router as webhooks_router
from app.api.admin import router as admin_router
from app.clients.ggsel import SELLER_OFFICE_V2_URL, ggsel_office
from app.clients.starpets import starpets
from app.config import settings

app = FastAPI(title="starpets-layer")
app.include_router(webhooks_router)
app.include_router(admin_router)


import base64 as _b64
import secrets as _sec

# Public paths (no operator auth): buyer delivery page, ggsel webhooks (authed by ?secret),
# and health root. EVERYTHING ELSE (test-*, fix-*, sync-*, trigger-*, /admin, …) requires
# the admin Basic Auth — closes the previously-public ops/test endpoints.
_PUBLIC_EXACT = {"/", "/delivery"}
_PUBLIC_PREFIXES = ("/hooks/", "/telegram/webhook/")  # webhook guarded by its own secret path


@app.middleware("http")
async def _require_operator_auth(request, call_next):
    path = request.url.path
    if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    ok = False
    if auth.startswith("Basic "):
        try:
            _user, _, _pw = _b64.b64decode(auth[6:]).decode("utf-8").partition(":")
            ok = (
                _sec.compare_digest(_user, settings.admin_user)
                and bool(settings.admin_password)
                and _sec.compare_digest(_pw, settings.admin_password)
            )
        except Exception:
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="starpets-ops"'})
    return await call_next(request)


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
    from app.db.models import Offer, OfferStatus
    async with AsyncSessionLocal() as db:
        total_result = await db.execute(select(func.count()).select_from(Offer))
        count = total_result.scalar()

        status_result = await db.execute(
            select(Offer.status, func.count().label("n"))
            .group_by(Offer.status)
        )
        by_status = {row.status.value: row.n for row in status_result}

    return {
        "offers": count,
        "offers_by_status": {
            "pending_create": by_status.get(OfferStatus.pending_create.value, 0),
            "active": by_status.get(OfferStatus.active.value, 0),
            "error": by_status.get(OfferStatus.error.value, 0),
        },
    }


@app.get("/offer-errors")
async def offer_errors():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.name, Offer.last_error)
            .where(Offer.status == OfferStatus.error)
            .limit(10)
        )
        rows = result.all()
    return [{"name": r.name, "last_error": r.last_error} for r in rows]


@app.get("/cheapest-offers")
async def cheapest_offers():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.name, Offer.price_rub, Offer.ggsel_offer_id)
            .where(
                Offer.status == OfferStatus.draft,
                Offer.ggsel_offer_id.isnot(None),
            )
            .order_by(Offer.price_rub.asc())
            .limit(5)
        )
        rows = result.all()
    return [{"name": r.name, "price_rub": float(r.price_rub), "ggsel_offer_id": r.ggsel_offer_id} for r in rows]


@app.get("/check-offers-health")
async def check_offers_health():
    from sqlalchemy import func, select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus

    async with AsyncSessionLocal() as db:
        sample_result = await db.execute(
            select(Offer.name, Offer.ggsel_offer_id)
            .where(Offer.ggsel_offer_id.isnot(None))
            .order_by(func.random())
            .limit(10)
        )
        sample = sample_result.all()

        status_result = await db.execute(
            select(Offer.status, func.count().label("n")).group_by(Offer.status)
        )
        by_status = {row.status.value: row.n for row in status_result}

    stats = {
        "pending_create": by_status.get(OfferStatus.pending_create.value, 0),
        "active": by_status.get(OfferStatus.active.value, 0),
        "error": by_status.get(OfferStatus.error.value, 0),
    }

    offers = []
    async with httpx.AsyncClient(headers=ggsel_office._headers(), timeout=10) as client:
        for row in sample:
            gid = row.ggsel_offer_id
            try:
                resp = await client.get(f"{SELLER_OFFICE_V2_URL}/offers/{gid}")
                if resp.is_success:
                    data = resp.json()
                    offers.append({
                        "name": row.name,
                        "ggsel_offer_id": gid,
                        "has_options": data.get("has_options"),
                        "notification_settings": data.get("notification_settings"),
                        "pre_payment_settings": data.get("pre_payment_settings"),
                    })
                else:
                    offers.append({
                        "name": row.name,
                        "ggsel_offer_id": gid,
                        "error": f"HTTP {resp.status_code}",
                        "body": resp.text[:200],
                    })
            except Exception as e:
                offers.append({"name": row.name, "ggsel_offer_id": gid, "error": str(e)})

    return {"stats": stats, "sample_size": len(sample), "offers": offers}


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


@app.get("/test-deliver-dryrun")
async def test_deliver_dryrun(ggsel_offer_id: int, username: str = "testuser"):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.fx import get_usd_rub

    steps = []

    # 1. Look up offer in DB
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Offer).where(Offer.ggsel_offer_id == ggsel_offer_id))
        offer = result.scalar_one_or_none()

    if not offer:
        return {"ok": False, "error": f"Offer with ggsel_offer_id={ggsel_offer_id} not found in DB", "steps": steps}
    steps.append({
        "step": "offer_lookup",
        "ok": True,
        "offer_id": offer.id,
        "name": offer.name,
        "status": offer.status.value,
        "ggsel_offer_id": offer.ggsel_offer_id,
        "starpets_product_id": offer.starpets_product_id,
        "price_usd": float(offer.price_usd or 0),
        "price_rub": float(offer.price_rub or 0),
    })

    if not offer.starpets_product_id:
        return {"ok": False, "error": "Offer has no starpets_product_id", "steps": steps}

    if offer.status != OfferStatus.active:
        steps.append({"step": "status_check", "ok": False, "warning": f"Offer status is {offer.status.value}, not active"})
    else:
        steps.append({"step": "status_check", "ok": True})

    # 2. Get top item from StarPets
    product_id = str(offer.starpets_product_id)
    top_item = None
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            top_item = await starpets.get_top_item(http, product_id)
        if top_item:
            steps.append({
                "step": "get_top_item",
                "ok": True,
                "item_id": top_item.get("id"),
                "price_usd": float(top_item.get("price_usd") or 0),
            })
        else:
            steps.append({"step": "get_top_item", "ok": False, "error": "No items available for this product"})
    except Exception as e:
        steps.append({"step": "get_top_item", "ok": False, "error": str(e)})
        return {"ok": False, "error": "get_top_item failed", "steps": steps}

    if not top_item:
        return {"ok": False, "error": "No items available", "steps": steps}

    # 3. Price profitability check
    item_price_usd = float(top_item.get("price_usd") or 0)
    offer_price_rub = float(offer.price_rub or 0)
    fx_rate = await get_usd_rub()
    cost_rub = item_price_usd * fx_rate * 1.0  # raw cost without markup
    cost_rub_with_markup = item_price_usd * fx_rate * settings.markup
    profitable = cost_rub_with_markup <= offer_price_rub
    steps.append({
        "step": "price_check",
        "ok": profitable,
        "item_price_usd": item_price_usd,
        "fx_rate": fx_rate,
        "markup": settings.markup,
        "cost_rub_with_markup": round(cost_rub_with_markup, 2),
        "offer_price_rub": offer_price_rub,
        "margin_rub": round(offer_price_rub - cost_rub_with_markup, 2),
        "would_buy": profitable,
    })

    # 4. Dry-run buy (no real purchase)
    steps.append({
        "step": "buy_dryrun",
        "ok": True,
        "skipped": True,
        "would_call": "POST /store/ex-buyers/items/buy",
        "would_send": {"id": top_item.get("id"), "price": item_price_usd},
    })

    # 5. Dry-run trade
    steps.append({
        "step": "trade_dryrun",
        "ok": True,
        "skipped": True,
        "would_call": "POST /trades/ex-buyers/withdrawal",
        "would_send": {"username": username, "items": [str(top_item.get("id"))]},
    })

    print(
        f"[DryRun] ggsel_offer_id={ggsel_offer_id} name={offer.name!r} "
        f"item_id={top_item.get('id')} price_usd={item_price_usd} "
        f"cost_rub={cost_rub_with_markup:.2f} offer_rub={offer_price_rub} "
        f"profitable={profitable} username={username!r}",
        flush=True,
    )

    return {
        "ok": profitable,
        "dryrun": True,
        "warning": None if profitable else "price_too_high — would NOT buy",
        "steps": steps,
    }


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


# In-process throttle for friendship re-sends on /delivery load (order_id -> last datetime).
# Not shared across instances, but send_friendship is idempotent so duplicates are harmless.
_friendship_resend_at: dict = {}


@app.get("/delivery", response_class=HTMLResponse)
async def delivery_page(uniquecode: str = None, id_i: int = None, id: int = None):
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, DeliveryStatus

    order = None
    if id_i is not None or id is not None or uniquecode is not None:
        async with AsyncSessionLocal() as db:
            if id_i is not None:
                result = await db.execute(select(Order).where(Order.ggsel_order_id == id_i))
                order = result.scalar_one_or_none()
            if order is None and id is not None:
                result = await db.execute(select(Order).where(Order.ggsel_order_id == id))
                order = result.scalar_one_or_none()
            if order is None and uniquecode is not None:
                # Повторный визит: order уже привязан к uniquecode
                result = await db.execute(select(Order).where(Order.uniquecode == uniquecode))
                order = result.scalar_one_or_none()
            if order is None and uniquecode is not None:
                # ДЕТЕРМИНИРОВАННО: спрашиваем у ggsel, чей это uniquecode.
                # GET /api_sellers/api/purchases/unique-code/{code} -> content.content_id = ggsel
                # order id (== наш Order.ggsel_order_id). Если резолвится — привязка точная, эвристика
                # ниже не нужна. Любая ошибка/None -> тихий откат на эвристику (fail-safe).
                try:
                    from app.clients.ggsel import ggsel_office
                    _content = await ggsel_office.resolve_unique_code(uniquecode)
                    # реальный ответ ggsel — плоский Digiseller-формат: id заказа = inv
                    # (id_goods = карточка, amount = сумма). content_id — на случай иной схемы.
                    _gg_id = (_content or {}).get("inv") or (_content or {}).get("content_id")
                    if _gg_id is not None:
                        order = (await db.execute(
                            select(Order).where(Order.ggsel_order_id == int(_gg_id))
                        )).scalar_one_or_none()
                        if order is not None and not order.uniquecode:
                            order.uniquecode = uniquecode
                            await db.commit()
                            print(f"[delivery] resolved uniquecode={uniquecode!r} -> "
                                  f"ggsel_order_id={_gg_id} order id={order.id}", flush=True)
                except Exception as _e:
                    print(f"[delivery] resolve_unique_code failed: {_e}", flush=True)

            if order is None and uniquecode is not None:
                # Первый визит: привязываем uniquecode к свежему order без него (notification
                # webhook срабатывает до редиректа, order уже в БД). Окно считаем по САМОМУ
                # позднему из created_at / dispatched_at — чтобы восстановленный заказ
                # (retry-delivery, пересоздание трейда спустя время) снова стал привязываемым,
                # а не завис на "обрабатывается". Берём и needs_attention (recovered orders).
                from sqlalchemy import func as _func
                # Buyers often open the order link long AFTER purchase (an hour+). A short window
                # left them stuck on the spinner forever, so it is generous now. But ggsel passes
                # only the uniquecode (not our order id), so if MULTIPLE unbound orders are in range
                # (same buyer bought several at once) we can't tell them apart — binding "the newest"
                # would show the WRONG bot. So bind only when there is exactly ONE candidate; the
                # ambiguous multi-order case needs the order id in the URL (see &id_i handling).
                # Bind to the MOST RECENT unbound order in range: the buyer who just paid and got
                # redirected is the freshest order. Window 24h so a late opener (an hour+ later) still
                # binds. Older still-unbound orders (dead/abandoned) are simply older, so newest-first
                # skips past them. ggsel passes only the uniquecode (not our order id), so newest-first
                # is the best signal available — don't refuse on multiple candidates (that stalls
                # EVERY new order while any old unbound order lingers).
                cutoff = datetime.utcnow() - timedelta(hours=24)
                _recent = _func.coalesce(Order.dispatched_at, Order.created_at)
                order = (await db.execute(
                    select(Order)
                    .where(
                        Order.uniquecode.is_(None),
                        Order.delivery_status.in_([
                            DeliveryStatus.pending, DeliveryStatus.dispatched,
                            DeliveryStatus.needs_attention,
                        ]),
                        # ONLY paid orders. precheck creates an Order for EVERY purchase attempt
                        # (incl. balance/maintenance-blocked ones) with amount_rub NULL — those flood
                        # the pool and steal the binding from the real paid order -> buyer gets the
                        # "processing" spinner. amount_rub is set only by the notification (payment).
                        Order.amount_rub.isnot(None),
                        _recent >= cutoff,
                    )
                    .order_by(_recent.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if order is not None:
                    order.uniquecode = uniquecode
                    await db.commit()
                    print(f"[delivery] linked uniquecode={uniquecode!r} → order id={order.id}", flush=True)

    bot_name = (order.bot_name or "").strip() if order else ""
    status = order.delivery_status if order else None

    # Re-send friendship when the buyer opens this page for a dispatched order.
    # deliver_order fires friendship once at T=0 — before the buyer has added the bot
    # as a friend — so that first call is a no-op. The buyer adds the bot HERE, on this
    # page, so this is the right moment to (re)trigger the bot to accept the pending
    # request. Throttled in-process (~20s) to avoid spam on rapid reloads.
    if (
        status == DeliveryStatus.dispatched
        and order
        and order.starpets_custom_id
        # Re-poke friendship through the whole pre-acceptance phase (statuses 0/1/2 oscillate
        # before the bot accepts), not just "0" — otherwise a buyer stuck at 2 never gets the
        # bot to accept. Stop at 3+ (buyer in-session / in-progress) to avoid 400 spam.
        and (order.starpets_status or "").strip() in ("", "0", "1", "2")
    ):
        from datetime import datetime as _dt
        _now = _dt.utcnow()
        _last = _friendship_resend_at.get(order.id)
        if _last is None or (_now - _last).total_seconds() > 20:
            _friendship_resend_at[order.id] = _now
            try:
                await starpets.send_friendship(trade_id=int(order.starpets_custom_id))
                print(
                    f"[delivery] friendship re-sent order_id={order.id} "
                    f"trade_id={order.starpets_custom_id}",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"[delivery] friendship re-send failed order_id={order.id}: {e}",
                    flush=True,
                )

    if status == DeliveryStatus.done or status == DeliveryStatus.finalized:
        body_html = """
        <div class="card">
            <div class="icon">✅</div>
            <h1>Предмет доставлен!</h1>
            <p class="sub">Спасибо за покупку.<br>Проверьте свой инвентарь в Adopt Me.</p>
        </div>"""
        extra_js = ""

    elif status == DeliveryStatus.failed:
        body_html = """
        <div class="card">
            <div class="icon">⏳</div>
            <h1>Не удалось доставить предмет</h1>
            <p class="sub">Свяжитесь с продавцом на странице заказа GGSEL — мы поможем с повторной доставкой или возвратом.</p>
        </div>"""
        extra_js = ""

    elif bot_name:
        # Server-side timer: seconds remaining in the current trade's 10-min bot window,
        # anchored to order.dispatched_at (survives tab close / device switch). The page
        # auto-refreshes every 20s, so this re-syncs; when the server auto-recreates an
        # expired trade, the refreshed page shows the new bot + a fresh countdown.
        _TOTAL = 10 * 60
        remaining = _TOTAL
        if order and order.dispatched_at:
            elapsed = (datetime.utcnow() - order.dispatched_at.replace(tzinfo=None)).total_seconds()
            remaining = max(0, int(_TOTAL - elapsed))
        _mm, _ss = remaining // 60, remaining % 60
        timer_init = f"{_mm:02d}:{_ss:02d}"
        from app.clients.roblox import bot_profile_url
        profile_url = await bot_profile_url(bot_name)
        # Support link → the buyer's GGSEL order page (has the chat with us). The
        # uniquecode is the order UUID GGSEL appends when redirecting to /delivery.
        support_url = (
            f"https://payment.ggsel.com/order/{order.uniquecode}"
            if (order and order.uniquecode) else "https://ggsel.net"
        )
        body_html = f"""
        <div class="card">
            <div class="icon">🤖</div>
            <p class="label">Имя бота для трейда</p>
            <a class="bot-name" href="{profile_url}" target="_blank" rel="noopener">{bot_name}</a>
            <p class="profile-hint">👆 нажмите на имя, чтобы открыть профиль бота</p>
            <p class="warn warn-top">⚠️ У Вас <strong>10 минут</strong> — успейте совершить трейд!</p>
            <div class="timer-box">
                <span class="timer-label">Осталось времени</span>
                <div class="timer" id="timer">{timer_init}</div>
            </div>
            <div class="steps">
                <div class="step"><span class="num">1</span><span class="step-text">Добавьте <a href="{profile_url}" target="_blank" rel="noopener" class="steplink">{bot_name}</a> в друзья на Roblox и дождитесь, пока он примет заявку (~1 минута)</span></div>
                <div class="step"><span class="num">2</span><span class="step-text">После добавления в друзья обновите страницу и нажмите появившуюся кнопку <strong>«Join»</strong>, чтобы подключиться к сессии бота</span></div>
                <div class="step"><span class="num">3</span><span class="step-text">После загрузки игры найдите бота в списке друзей и телепортируйтесь к нему</span></div>
                <div class="step"><span class="num">4</span><span class="step-text">Нажмите на кнопку взаимодействия с ботом и выберите <strong>«Trade»</strong> (нижняя кнопка из меню)</span></div>
                <div class="step"><span class="num">5</span><span class="step-text">Бот примет запрос на трейд и в течение ~1 минуты добавит питомца в обмен — примите трейд</span></div>
                <div class="step"><span class="num">6</span><span class="step-text">Готово! Проверьте инвентарь — питомец был передан вам 🤗</span></div>
            </div>
            <p class="support">Если в течение 5 минут бот не принял заявку в друзья — дождитесь завершения таймера с его перезапуском и проверьте статус запроса.</p>
            <p class="support support-bottom">Остались вопросы? Обратитесь в <a href="{support_url}" target="_blank" rel="noopener" class="steplink">Чат поддержки</a></p>
        </div>"""
        extra_js = f"""
        (function() {{
            var total = {remaining};
            var el = document.getElementById('timer');
            if (total <= 0) {{ el.textContent = '00:00'; el.style.color='#ef4444'; return; }}
            var iv = setInterval(function() {{
                total--;
                if (total <= 0) {{ clearInterval(iv); el.textContent = '00:00'; el.style.color='#ef4444'; return; }}
                var m = Math.floor(total / 60);
                var s = total % 60;
                el.textContent = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
            }}, 1000);
        }})();"""

    else:
        body_html = """
        <div class="card">
            <div class="spinner"></div>
            <h1>Заказ обрабатывается</h1>
            <p class="sub">Пожалуйста, подождите — это займёт несколько секунд...</p>
            <p class="sub small">Страница обновится автоматически</p>
        </div>"""
        extra_js = ""

    if status in (DeliveryStatus.done, DeliveryStatus.finalized, DeliveryStatus.failed):
        # Keep polling even on terminal screens so that if the operator re-issues the
        # order ("Новый логин"/"Новый трейд" → back to dispatched), the buyer's already
        # open page auto-updates to the new bot + fresh timer without a manual refresh.
        refresh_meta = '<meta http-equiv="refresh" content="10">'
    elif bot_name:
        # Dispatched: refresh every 20s so a foregrounded tab re-triggers the friendship
        # re-send above. Desktop-reliable; on mobile the server-side monitor loop is the
        # real safety net (page timers pause when the tab is backgrounded for Roblox).
        refresh_meta = '<meta http-equiv="refresh" content="20">'
    else:
        refresh_meta = '<meta http-equiv="refresh" content="5">'

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Доставка предмета — StarPets</title>
{refresh_meta}
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    padding: 16px;
    color: #fff;
  }}
  .card {{
    background: rgba(255,255,255,0.07);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 20px;
    padding: 40px 32px;
    max-width: 480px;
    width: 100%;
    text-align: center;
    backdrop-filter: blur(12px);
    box-shadow: 0 8px 40px rgba(0,0,0,0.4);
  }}
  .icon {{ font-size: 56px; margin-bottom: 16px; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 10px; }}
  .sub {{ color: rgba(255,255,255,0.65); font-size: 0.95rem; line-height: 1.5; margin-top: 8px; }}
  .sub.small {{ font-size: 0.8rem; margin-top: 4px; }}
  .label {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1.5px; color: rgba(255,255,255,0.5); margin-bottom: 8px; }}
  .bot-name {{
    font-size: 2.2rem;
    font-weight: 800;
    background: linear-gradient(90deg, #a78bfa, #60a5fa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 6px;
    word-break: break-all;
    display: inline-block;
    text-decoration: none;
    cursor: pointer;
  }}
  .bot-name:hover {{ filter: brightness(1.15); text-decoration: underline; }}
  .profile-hint {{ font-size: 0.8rem; color: rgba(255,255,255,0.55); margin-bottom: 22px; }}
  .steplink {{ color: #a78bfa; font-weight: 700; text-decoration: underline; }}
  .steplink:hover {{ filter: brightness(1.15); }}
  .step-text {{ flex: 1; min-width: 0; }}
  .warn-top {{ margin-bottom: 20px; }}
  .support {{ font-size: 0.85rem; color: rgba(255,255,255,0.6); line-height: 1.5; margin-top: 2px; }}
  .support-bottom {{ margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.08); }}
  .timer-box {{
    background: rgba(0,0,0,0.3);
    border-radius: 12px;
    padding: 14px 20px;
    margin-bottom: 28px;
  }}
  .timer-label {{ font-size: 0.75rem; color: rgba(255,255,255,0.5); display: block; margin-bottom: 4px; }}
  .timer {{
    font-size: 2.8rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    color: #34d399;
    letter-spacing: 2px;
  }}
  .steps {{ text-align: left; margin-bottom: 24px; }}
  .step {{
    display: flex;
    align-items: flex-start;
    gap: 14px;
    padding: 12px 0;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    font-size: 0.95rem;
    line-height: 1.5;
    color: rgba(255,255,255,0.88);
  }}
  .step:last-child {{ border-bottom: none; }}
  .num {{
    min-width: 28px;
    height: 28px;
    border-radius: 50%;
    background: linear-gradient(135deg, #7c3aed, #2563eb);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.8rem;
    font-weight: 700;
    flex-shrink: 0;
    margin-top: 1px;
  }}
  .warn {{
    background: rgba(239,68,68,0.15);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: 10px;
    padding: 10px 16px;
    font-size: 0.9rem;
    color: #fca5a5;
  }}
  .spinner {{
    width: 52px;
    height: 52px;
    border: 4px solid rgba(255,255,255,0.15);
    border-top-color: #a78bfa;
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
    margin: 0 auto 24px;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
{body_html}
<script>{extra_js}</script>
</body>
</html>
""")


@app.get("/system-status")
async def system_status():
    from sqlalchemy import func, select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, Order, OfferStatus, DeliveryStatus

    async with AsyncSessionLocal() as db:
        offer_result = await db.execute(
            select(Offer.status, func.count().label("n")).group_by(Offer.status)
        )
        offers_by_status = {row.status.value: row.n for row in offer_result}

        order_result = await db.execute(
            select(Order.delivery_status, func.count().label("n")).group_by(Order.delivery_status)
        )
        orders_by_status = {row.delivery_status.value: row.n for row in order_result}

    starpets_balance = None
    starpets_balance_raw = None
    balance_endpoints = [
        "/ex-buyers/balance",
        "/account/balance",
        "/ex-buyers/info/me",
        "/info",
    ]
    async with httpx.AsyncClient(timeout=10) as _hc:
        for ep in balance_endpoints:
            try:
                params = starpets._base_params()
                resp = await _hc.get(
                    f"{starpets.base_url}{ep}",
                    headers=starpets._headers(starpets._sign(params)),
                    params=params,
                )
                starpets_balance_raw = {"endpoint": ep, "status": resp.status_code, "body": resp.text[:300]}
                if resp.is_success:
                    data = resp.json()
                    starpets_balance = (
                        (data.get("buyer") or {}).get("balance")
                        or data.get("balance")
                        or data.get("balanceUsd")
                        or data.get("balance_usd")
                        or (data.get("data") or {}).get("balance")
                        or (data.get("data") or {}).get("balanceUsd")
                    )
                    if starpets_balance is not None:
                        break
            except Exception as e:
                starpets_balance_raw = {"endpoint": ep, "error": str(e)}

    return {
        "offers": {s.value: offers_by_status.get(s.value, 0) for s in OfferStatus},
        "orders": {s.value: orders_by_status.get(s.value, 0) for s in DeliveryStatus},
        "starpets_balance_usd": starpets_balance,
        "starpets_balance_debug": starpets_balance_raw,
    }


@app.get("/trigger-deliver")
async def trigger_deliver(order_id: int):
    from datetime import datetime
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Task, TaskKind, DeliveryStatus

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            return {"error": f"Order {order_id} not found"}

        order.delivery_status = DeliveryStatus.pending
        order.error_reason = None
        order.trade_retry_count = 0          # fresh manual attempt → reset auto-retry budget
        order.dispatched_at = None           # new timer starts when the new trade dispatches
        order.updated_at = datetime.utcnow()
        db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=3, payload={"order_id": order_id}))
        await db.commit()

    print(f"[trigger-deliver] order_id={order_id} reset to pending, DELIVER task queued", flush=True)
    return {
        "order_id": order_id,
        "delivery_status": "pending",
        "queued_deliver": True,
        "roblox_username": order.roblox_username,
        "starpets_purchase_id": order.starpets_purchase_id,
    }


@app.get("/fix-order-username")
async def fix_order_username(order_id: int, username: str):
    from datetime import datetime
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Task, TaskKind, DeliveryStatus

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            return {"error": f"Order {order_id} not found"}

        old_username = order.roblox_username
        order.roblox_username = username
        order.updated_at = datetime.utcnow()

        queued_deliver = False
        # If failed due to missing username or still pending with item already bought → retry deliver
        if order.delivery_status in (DeliveryStatus.failed,) and order.error_reason == "no_roblox_username":
            order.delivery_status = DeliveryStatus.pending
            order.error_reason = None
            db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=3, payload={"order_id": order.id}))
            queued_deliver = True
        elif order.delivery_status == DeliveryStatus.pending and order.starpets_purchase_id:
            db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=3, payload={"order_id": order.id}))
            queued_deliver = True

        await db.commit()
        print(
            f"[fix-order-username] order_id={order_id} username: {old_username!r} → {username!r} "
            f"status={order.delivery_status.value} queued_deliver={queued_deliver}",
            flush=True,
        )

    return {
        "order_id": order_id,
        "old_username": old_username,
        "new_username": username,
        "delivery_status": order.delivery_status.value,
        "starpets_purchase_id": order.starpets_purchase_id,
        "queued_deliver": queued_deliver,
    }


@app.get("/order-info")
async def order_info(order_id: int):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.ggsel_order_id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            result = await db.execute(select(Order).where(Order.id == order_id))
            order = result.scalar_one_or_none()

    if not order:
        return {"error": f"Order {order_id} not found"}

    return {
        "order_id": order.id,
        "ggsel_order_id": order.ggsel_order_id,
        "item_name": order.item_name,
        "roblox_username": order.roblox_username,
        "bot_name": order.bot_name,
        "delivery_status": order.delivery_status.value if order.delivery_status else None,
        "starpets_trade_id": order.starpets_custom_id,
    }


@app.get("/sync-prices")
async def sync_prices():
    import asyncio
    asyncio.create_task(_run_sync_prices())
    return {"started": True}


async def _run_sync_prices():
    import asyncio
    import traceback
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.fx import get_usd_rub

    print("[SyncPrices] started", flush=True)
    try:
        # Balance guard: pause all active offers and abort if funds are too low
        try:
            info = await starpets.get_info()
            balance = float(
                (info.get("buyer") or {}).get("balance")
                or info.get("balanceUsd")
                or info.get("balance_usd")
                or info.get("balance")
                or (info.get("data") or {}).get("balance")
                or 0
            )
        except Exception as _be:
            print(f"[SyncPrices] balance check failed (non-fatal): {_be}", flush=True)
            balance = None

        if balance is not None and balance < 1.0:
            print(f"[SyncPrices] LOW BALANCE: ${balance:.2f} — pausing all active offers", flush=True)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Offer).where(
                        Offer.ggsel_offer_id.isnot(None),
                        Offer.status == OfferStatus.active,
                    )
                )
                active_offers = result.scalars().all()
                ggsel_ids = [o.ggsel_offer_id for o in active_offers]
                offer_ids = [o.id for o in active_offers]

            if ggsel_ids:
                for batch_start in range(0, len(ggsel_ids), 100):
                    batch = ggsel_ids[batch_start:batch_start + 100]
                    try:
                        await ggsel_office.pause_offers(batch)
                    except Exception as _pe:
                        print(f"[SyncPrices] pause_offers batch error: {_pe}", flush=True)
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(Offer).where(Offer.id.in_(offer_ids)))
                    for offer in result.scalars().all():
                        offer.status = OfferStatus.paused
                    await db.commit()
                print(f"[SyncPrices] paused {len(ggsel_ids)} offers", flush=True)
            return

        fx_rate = await get_usd_rub()

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Offer).where(
                    Offer.ggsel_offer_id.isnot(None),
                    Offer.starpets_product_id.isnot(None),
                )
            )
            offers = result.scalars().all()
            offer_snapshot = [
                (o.id, o.ggsel_offer_id, o.starpets_product_id, o.name, float(o.price_rub or 0), int(o.starpets_qty or 0), o.status)
                for o in offers
            ]

        print(f"[SyncPrices] {len(offer_snapshot)} offers to check, fx_rate={fx_rate}", flush=True)

        counters = {"updated": 0, "skipped": 0, "errors": 0, "paused": 0}
        to_activate: list[tuple[int, int, str]] = []          # (offer_id, ggsel_id, name)
        price_updates: list[tuple[int, float, float]] = []    # (offer_id, price_usd, new_rub)
        pause_ids: list[int] = []                             # offer_ids to mark paused in DB
        sem = asyncio.Semaphore(settings.sync_concurrency)

        async def _process_offer(http, rec):
            offer_id, ggsel_id, product_id, name, current_rub, starpets_qty, offer_status = rec
            async with sem:
                try:
                    top_item = await starpets.get_top_item(http, str(product_id))

                    if not top_item:
                        # No stock — pause if currently active on our side
                        if offer_status == OfferStatus.active:
                            try:
                                await ggsel_office.pause_offer(ggsel_id)
                                pause_ids.append(offer_id)
                                counters["paused"] += 1
                                if settings.sync_log_verbose:
                                    print(f"[SyncPrices] paused ggsel_id={ggsel_id} name={name!r}", flush=True)
                            except Exception as e:
                                counters["errors"] += 1
                                print(f"[SyncPrices] pause error ggsel_id={ggsel_id} name={name!r}: {e}", flush=True)
                        counters["skipped"] += 1
                        return

                    if offer_status == OfferStatus.paused:
                        to_activate.append((offer_id, ggsel_id, name))

                    price_usd = float(top_item.get("price_usd") or 0)
                    if not price_usd:
                        counters["skipped"] += 1
                        return

                    new_rub = max(round(price_usd * fx_rate * settings.markup, 2), settings.min_price_rub)
                    if abs(new_rub - current_rub) < 0.01:
                        counters["skipped"] += 1
                        return

                    await ggsel_office.update_price(ggsel_id, new_rub)
                    price_updates.append((offer_id, price_usd, new_rub))
                    counters["updated"] += 1
                    if settings.sync_log_verbose:
                        print(
                            f"[SyncPrices] updated ggsel_id={ggsel_id} name={name!r} "
                            f"old_rub={current_rub} → new_rub={new_rub} price_usd={price_usd}",
                            flush=True,
                        )
                except Exception as e:
                    counters["errors"] += 1
                    print(f"[SyncPrices] error ggsel_id={ggsel_id} name={name!r}: {e}", flush=True)

        # Process offers CONCURRENTLY (bounded by SYNC_CONCURRENCY). Network-bound, so this
        # is ~Nx faster than the old one-at-a-time loop. The per-offer get_offer status-sync
        # was removed from this hot path (it doubled the API calls) — status is kept in sync
        # by the activate/pause flow and the hourly reconcile job.
        _limits = httpx.Limits(max_connections=settings.sync_concurrency + 5)
        async with httpx.AsyncClient(timeout=15, limits=_limits) as http:
            total = len(offer_snapshot)
            for chunk_start in range(0, total, 500):
                chunk = offer_snapshot[chunk_start:chunk_start + 500]
                await asyncio.gather(*[_process_offer(http, rec) for rec in chunk])
                print(f"[SyncPrices] progress {min(chunk_start + 500, total)}/{total}", flush=True)

        # Batch-persist price changes and pauses in a single DB session
        if price_updates or pause_ids:
            async with AsyncSessionLocal() as db:
                for offer_id, price_usd, new_rub in price_updates:
                    _o = (await db.execute(select(Offer).where(Offer.id == offer_id))).scalar_one_or_none()
                    if _o:
                        _o.price_usd = price_usd
                        _o.price_rub = new_rub
                for offer_id in pause_ids:
                    _o = (await db.execute(select(Offer).where(Offer.id == offer_id))).scalar_one_or_none()
                    if _o:
                        _o.status = OfferStatus.paused
                await db.commit()

        updated = counters["updated"]
        skipped = counters["skipped"]
        errors = counters["errors"]
        paused_count = counters["paused"]

        # Activation disabled — maintenance mode (pause-all-offers in progress)
        activated_count = 0
        if to_activate:
            print(f"[SyncPrices] skipping activation of {len(to_activate)} offers (maintenance mode)", flush=True)

        print(
            f"[SyncPrices] done — updated={updated} paused={paused_count} activated={activated_count} "
            f"skipped={skipped} errors={errors} total={len(offer_snapshot)}",
            flush=True,
        )

    except Exception as e:
        print(f"[SyncPrices] fatal error: {e}\n{traceback.format_exc()}", flush=True)


@app.get("/fix-paused-to-draft")
async def fix_paused_to_draft():
    from sqlalchemy import text
    from app.db import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        # Diagnose actual stored values
        diag = (await db.execute(
            text("SELECT status::text, COUNT(*) FROM offers GROUP BY status::text")
        )).all()
        diag_map = {row[0]: row[1] for row in diag}

        count_before = (await db.execute(
            text("SELECT COUNT(*) FROM offers WHERE status::text = 'paused' AND ggsel_offer_id IS NOT NULL")
        )).scalar()
        upd = await db.execute(
            text("UPDATE offers SET status = 'draft'::offerstatus WHERE status::text = 'paused' AND ggsel_offer_id IS NOT NULL")
        )
        affected = upd.rowcount
        await db.commit()
    print(f"[FixPausedToDraft] diag={diag_map} count_before={count_before} affected={affected}", flush=True)
    return {"diag": diag_map, "count_before": count_before, "affected": affected}


@app.get("/retry-errors")
async def retry_errors():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer).where(Offer.status == OfferStatus.error)
        )
        offers = result.scalars().all()
        count = len(offers)
        for offer in offers:
            offer.status = OfferStatus.pending_create
            offer.last_error = None
            offer.error_count = 0
        await db.commit()

    print(f"[RetryErrors] reset {count} error offers to pending_create", flush=True)
    return {"reset": count}


@app.get("/fix-post-payment-url")
async def fix_post_payment_url():
    import asyncio
    asyncio.create_task(_run_fix_post_payment_url())
    return {"started": True}


async def _run_fix_post_payment_url():
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer

    url = f"{settings.public_url}/delivery"

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.ggsel_offer_id).where(Offer.ggsel_offer_id.isnot(None))
        )
        offer_ids = [r[0] for r in result.all()]

    total = len(offer_ids)
    print(f"[FixPostPaymentUrl] starting — {total} offers url={url} (concurrency={settings.sync_concurrency})", flush=True)
    counters = {"updated": 0, "errors": 0}
    sem = asyncio.Semaphore(settings.sync_concurrency)

    async def _fix(gid):
        async with sem:
            try:
                await ggsel_office.set_post_payment_url(gid, url)
                counters["updated"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"[FixPostPaymentUrl] ggsel_offer_id={gid} error: {e}", flush=True)

    for i in range(0, total, 500):
        await asyncio.gather(*[_fix(g) for g in offer_ids[i:i + 500]])
        print(
            f"[FixPostPaymentUrl] progress {min(i + 500, total)}/{total} "
            f"updated={counters['updated']} errors={counters['errors']}",
            flush=True,
        )
    print(f"[FixPostPaymentUrl] done — updated={counters['updated']} errors={counters['errors']} total={total}", flush=True)


@app.get("/fix-webhooks")
async def fix_webhooks():
    import asyncio
    asyncio.create_task(_run_fix_webhooks())
    return {"started": True}


async def _run_fix_webhooks():
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer
    from app.config import settings

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.ggsel_offer_id).where(Offer.ggsel_offer_id.isnot(None))
        )
        offer_ids = [r[0] for r in result.all()]

    total = len(offer_ids)
    print(f"[FixWebhooks] starting — {total} offers (concurrency={settings.sync_concurrency})", flush=True)

    secret = settings.webhook_shared_secret
    counters = {"updated": 0, "errors": 0}
    sem = asyncio.Semaphore(settings.sync_concurrency)

    async def _fix(gid):
        async with sem:
            try:
                await ggsel_office.patch_offer(
                    offer_id=gid,
                    precheck_url=f"{settings.public_url}/hooks/ggsel/precheck/{gid}?secret={secret}",
                    notification_url=f"{settings.public_url}/hooks/ggsel/notification/{gid}?secret={secret}",
                )
                counters["updated"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"[FixWebhooks] ggsel_offer_id={gid} error: {e}", flush=True)

    # Concurrent PATCH (bounded by SYNC_CONCURRENCY). We patch every offer directly — for a
    # secret rotation all URLs must change anyway — dropping the per-offer get_offer check
    # (it was also broken: ggsel wraps the offer in "data", so the skip never triggered).
    for chunk_start in range(0, total, 500):
        chunk = offer_ids[chunk_start:chunk_start + 500]
        await asyncio.gather(*[_fix(gid) for gid in chunk])
        print(
            f"[FixWebhooks] progress {min(chunk_start + 500, total)}/{total} "
            f"updated={counters['updated']} errors={counters['errors']}",
            flush=True,
        )

    print(f"[FixWebhooks] done — updated={counters['updated']} errors={counters['errors']} total={total}", flush=True)


@app.get("/fix-descriptions")
async def fix_descriptions():
    import asyncio
    asyncio.create_task(_run_fix_descriptions())
    return {"started": True}


async def _run_fix_descriptions():
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer
    from app.workers.offer_creator import _build_description

    async with AsyncSessionLocal() as db:
        offers = (await db.execute(
            select(Offer).where(Offer.ggsel_offer_id.isnot(None))
        )).scalars().all()

    total = len(offers)
    print(f"[FixDescriptions] starting — {total} offers (concurrency={settings.sync_concurrency})", flush=True)
    counters = {"updated": 0, "errors": 0}
    sem = asyncio.Semaphore(settings.sync_concurrency)

    async def _fix(offer):
        async with sem:
            try:
                desc_ru, desc_en, instr_ru, instr_en = _build_description(offer)
                await ggsel_office.update_content(
                    offer.ggsel_offer_id, desc_ru, desc_en, instr_ru, instr_en
                )
                counters["updated"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"[FixDescriptions] ggsel_offer_id={offer.ggsel_offer_id} error: {e}", flush=True)

    for i in range(0, total, 500):
        await asyncio.gather(*[_fix(o) for o in offers[i:i + 500]])
        print(
            f"[FixDescriptions] progress {min(i + 500, total)}/{total} "
            f"updated={counters['updated']} errors={counters['errors']}",
            flush=True,
        )

    print(f"[FixDescriptions] done — updated={counters['updated']} errors={counters['errors']} total={total}", flush=True)


@app.get("/add-consent-option")
async def add_consent_option(limit: int = 0, ggsel_offer_id: int = 0):
    """Add the mandatory pre-purchase consent checkbox to offers.
    ?ggsel_offer_id=N  -> single card (smoke test)
    ?limit=N           -> first N offers
    (no params)        -> all offers. Idempotent: skips offers that already have it."""
    import asyncio
    asyncio.create_task(_run_add_consent_option(limit, ggsel_offer_id))
    return {"started": True, "limit": limit, "ggsel_offer_id": ggsel_offer_id}


async def _run_add_consent_option(limit: int = 0, ggsel_offer_id: int = 0):
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer

    async with AsyncSessionLocal() as db:
        offers = (await db.execute(
            select(Offer).where(Offer.ggsel_offer_id.isnot(None))
        )).scalars().all()

    if ggsel_offer_id:
        offers = [o for o in offers if o.ggsel_offer_id == ggsel_offer_id]
    elif limit and limit > 0:
        offers = offers[:limit]

    total = len(offers)
    conc = min(settings.sync_concurrency, 4)  # 2 requests/offer (GET+POST) — keep gentle on ggsel gateway
    print(f"[AddConsent] starting — {total} offers (concurrency={conc})", flush=True)
    counters = {"added": 0, "skipped": 0, "errors": 0}
    sem = asyncio.Semaphore(conc)

    async def _add(offer):
        async with sem:
            try:
                if await ggsel_office.has_consent_option(offer.ggsel_offer_id):
                    counters["skipped"] += 1
                    return
                await ggsel_office.create_consent_option(offer.ggsel_offer_id)
                counters["added"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"[AddConsent] ggsel_offer_id={offer.ggsel_offer_id} error: {e}", flush=True)

    for i in range(0, total, 500):
        await asyncio.gather(*[_add(o) for o in offers[i:i + 500]])
        print(
            f"[AddConsent] progress {min(i + 500, total)}/{total} "
            f"added={counters['added']} skipped={counters['skipped']} errors={counters['errors']}",
            flush=True,
        )

    print(
        f"[AddConsent] done — added={counters['added']} skipped={counters['skipped']} "
        f"errors={counters['errors']} total={total}",
        flush=True,
    )


@app.get("/seed-store-items")
async def seed_store_items_ep(limit: int = 0):
    """One-time seed of store_items (floors) for our products before the event feed takes over."""
    import asyncio
    from app.workers.price_sync import seed_store_items
    asyncio.create_task(seed_store_items(limit))
    return {"started": True, "limit": limit}


@app.get("/debug-item-updates")
async def debug_item_updates(cursor: int = None, date_ms: int = None):
    """Probe StarPets /ex-buyers/updates directly. ?cursor=0 tests the cursor path;
    ?date_ms=<ms> tests a specific date; no params = default (last 10s date)."""
    from app.clients.starpets import starpets
    try:
        batch = await starpets.get_item_updates(limit=50, cursor=cursor, date_ms=date_ms)
        return {"ok": True, "count": len(batch), "sample": batch[:3]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e!r}"}


@app.get("/fast-forward-cursor")
async def fast_forward_cursor_ep():
    """Jump the items-feed cursor near the current tip (skip replaying history)."""
    from app.workers.price_sync import fast_forward_cursor
    return await fast_forward_cursor()


@app.get("/probe-image-sizes")
async def probe_image_sizes(product_id: int):
    """Probe which StarPets image sizes are available for a product (swap the WxH path folder)."""
    import re
    from io import BytesIO
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct
    from PIL import Image

    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(SkuProduct).where(SkuProduct.product_id == product_id))).scalar_one_or_none()
    if not p or not p.image_uri:
        return {"error": "no image_uri for this product"}

    base = p.image_uri
    cands = {"as_is": base, "original_nosize": re.sub(r"/\d+x\d+/", "/", base)}
    for sz in ("220x220", "256x256", "300x300", "512x512", "1024x1024"):
        cands[sz] = re.sub(r"/\d+x\d+/", f"/{sz}/", base)

    results = {}
    async with httpx.AsyncClient(timeout=15) as c:
        for name, url in cands.items():
            try:
                r = await c.get(url)
                info = {"status": r.status_code, "bytes": len(r.content)}
                if r.is_success:
                    try:
                        im = Image.open(BytesIO(r.content))
                        info["dim"] = f"{im.width}x{im.height}"
                    except Exception:
                        info["dim"] = "not-an-image"
                results[name] = info
            except Exception as e:
                results[name] = {"error": f"{type(e).__name__}: {e}"}
    return {"base": base, "candidates": results}


@app.get("/proto-cover")
async def proto_cover(product_id: int):
    """Preview the generated SKU cover for a product (rarity bg + pet + pumping badge)."""
    from fastapi.responses import Response
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct
    from app.images.cover import make_cover

    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(SkuProduct).where(SkuProduct.product_id == product_id))).scalar_one_or_none()
    if not p:
        return {"error": f"product {product_id} not found in sku_products"}

    pet_bytes = b""
    if p.image_uri:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(p.image_uri)
                if r.is_success:
                    pet_bytes = r.content
                else:
                    print(f"[proto-cover] image fetch {r.status_code} {p.image_uri}", flush=True)
        except Exception as e:
            print(f"[proto-cover] image fetch error: {e}", flush=True)
    png = make_cover(pet_bytes, p.rare, p.pumping, name=p.name)
    return Response(content=png, media_type="image/png")


@app.get("/sync-sku-products")
async def sync_sku_products():
    """Populate sku_products from StarPets get_all_products (full catalog WITH pumping)."""
    import asyncio
    asyncio.create_task(_run_sync_sku_products())
    return {"started": True}


async def _run_sync_sku_products():
    from datetime import datetime
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct

    products = await starpets.get_all_products()
    total = len(products)
    print(f"[SyncSkuProducts] starting — {total} products from StarPets", flush=True)

    now = datetime.utcnow()
    rows = []
    for p in products:
        pid = p.get("id")
        if not pid:
            continue
        rows.append({
            "product_id": int(pid),
            "name": p.get("name") or "",
            "rare": p.get("rare") or p.get("rarity"),
            "item_type": p.get("type") or p.get("item_type"),
            "age": p.get("age"),
            "pumping": p.get("pumping"),
            "flyable": bool(p.get("flyable", False)),
            "rideable": bool(p.get("rideable", False)),
            "image_uri": p.get("imageUri") or p.get("image_uri") or p.get("image"),
            "updated_at": now,
        })

    upserted = 0
    async with AsyncSessionLocal() as db:
        for i in range(0, len(rows), 1000):
            batch = rows[i:i + 1000]
            stmt = pg_insert(SkuProduct).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["product_id"],
                set_={
                    "name": stmt.excluded.name,
                    "rare": stmt.excluded.rare,
                    "item_type": stmt.excluded.item_type,
                    "age": stmt.excluded.age,
                    "pumping": stmt.excluded.pumping,
                    "flyable": stmt.excluded.flyable,
                    "rideable": stmt.excluded.rideable,
                    "image_uri": stmt.excluded.image_uri,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await db.execute(stmt)
            upserted += len(batch)
        await db.commit()
    print(f"[SyncSkuProducts] done — upserted={upserted} total={total}", flush=True)


@app.get("/proto-sku-card")
async def proto_sku_card(name: str, limit: int = 8):
    """SKU-master prototype (Phase 0): turn several combos of one pet into a single card
    with a 'Вариант' radio option (one variant per combo), mapping each variant to its product."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, SkuVariant

    async with AsyncSessionLocal() as db:
        offers = (await db.execute(
            select(Offer).where(
                Offer.name == name,
                Offer.ggsel_offer_id.isnot(None),
                Offer.starpets_product_id.isnot(None),
                Offer.price_rub > 0,
            ).order_by(Offer.price_rub.asc()).limit(limit)
        )).scalars().all()
    if not offers:
        return {"error": f"no offers for name={name!r}"}

    base = offers[0]
    base_gid = base.ggsel_offer_id
    base_price = float(base.price_rub or 0)

    def _label(o):
        parts = []
        if o.rare:
            parts.append(str(o.rare).replace("_", " ").title())
        if o.flyable:
            parts.append("Летает")
        if o.rideable:
            parts.append("Ездовой")
        if o.age:
            parts.append(str(o.age).replace("_", " ").title())
        return " · ".join(parts) or "Стандарт"

    try:
        option_id = await ggsel_office.create_radio_option(base_gid, title_ru="Вариант", title_en="Variant")
    except Exception as e:
        return {"error": f"create_radio_option failed: {e}"}

    created = []
    for i, o in enumerate(offers):
        price = float(o.price_rub or 0)
        delta = round(price - base_price, 2)
        label = _label(o)
        title = f"{label} — {int(round(price))}\u20bd"
        try:
            vid = await ggsel_office.add_variant(
                base_gid, option_id, title_ru=title, title_en=label,
                price_delta=delta, is_default=(i == 0), position=i,
            )
        except Exception as e:
            created.append({"label": label, "error": str(e)})
            continue
        async with AsyncSessionLocal() as db:
            db.add(SkuVariant(
                ggsel_offer_id=base_gid, ggsel_option_id=option_id, ggsel_variant_id=vid,
                starpets_product_id=o.starpets_product_id, label=label, price_rub=price,
            ))
            await db.commit()
        created.append({
            "label": label, "variant_id": vid, "product_id": o.starpets_product_id,
            "price_rub": price, "delta": delta, "default": i == 0,
        })

    return {
        "base_ggsel_offer_id": base_gid, "option_id": option_id,
        "base_price": base_price, "count": len(created), "variants": created,
    }


@app.get("/price-sync-once")
async def price_sync_once():
    """Run one pass of the event-driven price sync immediately (manual test)."""
    from app.workers.price_sync import sync_item_updates
    return await sync_item_updates()


@app.get("/fix-consent-option")
async def fix_consent_option(limit: int = 0, ggsel_offer_id: int = 0, active_only: bool = False):
    """Put the CORRECT consent checkbox (with variant) on offers, cleaning up any old/broken one.
    ?active_only=true  -> only currently-active offers first
    ?ggsel_offer_id=N  -> single card (smoke test)
    ?limit=N           -> first N. Idempotent: offers already correct are skipped."""
    import asyncio
    asyncio.create_task(_run_fix_consent_option(limit, ggsel_offer_id, active_only))
    return {"started": True, "limit": limit, "ggsel_offer_id": ggsel_offer_id, "active_only": active_only}


async def _run_fix_consent_option(limit: int = 0, ggsel_offer_id: int = 0, active_only: bool = False):
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus

    async with AsyncSessionLocal() as db:
        q = select(Offer).where(Offer.ggsel_offer_id.isnot(None))
        if active_only:
            q = q.where(Offer.status == OfferStatus.active)
        offers = (await db.execute(q)).scalars().all()

    if ggsel_offer_id:
        offers = [o for o in offers if o.ggsel_offer_id == ggsel_offer_id]
    elif limit and limit > 0:
        offers = offers[:limit]

    total = len(offers)
    conc = min(settings.sync_concurrency, 8)  # up to 4-5 requests/offer; retries absorb 503s
    print(f"[FixConsent] starting — {total} offers active_only={active_only} concurrency={conc}", flush=True)
    counters = {"fixed": 0, "skipped": 0, "errors": 0}
    sem = asyncio.Semaphore(conc)

    async def _fix(offer):
        gid = offer.ggsel_offer_id
        async with sem:
            try:
                has_correct, stale_ids = await ggsel_office.consent_option_state(gid)
                if stale_ids:
                    await ggsel_office.delete_options(gid, stale_ids)
                if has_correct:
                    counters["skipped"] += 1
                    return
                await ggsel_office.create_consent_option(gid)
                counters["fixed"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"[FixConsent] ggsel_offer_id={gid} error: {type(e).__name__}: {e!r}", flush=True)

    for i in range(0, total, 500):
        await asyncio.gather(*[_fix(o) for o in offers[i:i + 500]])
        print(
            f"[FixConsent] progress {min(i + 500, total)}/{total} "
            f"fixed={counters['fixed']} skipped={counters['skipped']} errors={counters['errors']}",
            flush=True,
        )
    print(
        f"[FixConsent] done — fixed={counters['fixed']} skipped={counters['skipped']} "
        f"errors={counters['errors']} total={total}",
        flush=True,
    )


@app.get("/debug-options")
async def debug_options(ggsel_offer_id: int):
    """Dump the raw options JSON for one offer (to inspect the checkbox/variant structure)."""
    try:
        data = await ggsel_office.get_options(ggsel_offer_id)
        return {"ggsel_offer_id": ggsel_offer_id, "options": data}
    except Exception as e:
        return {"ggsel_offer_id": ggsel_offer_id, "error": str(e)}


@app.get("/remove-consent-option")
async def remove_consent_option(limit: int = 0, ggsel_offer_id: int = 0, active_only: bool = False):
    """Remove the (broken) consent option from offers so checkout works again.
    ?active_only=true  -> only currently-active offers (the live, blocked ones) first
    ?ggsel_offer_id=N  -> single card
    ?limit=N           -> first N. Idempotent: offers without it are skipped."""
    import asyncio
    asyncio.create_task(_run_remove_consent_option(limit, ggsel_offer_id, active_only))
    return {"started": True, "limit": limit, "ggsel_offer_id": ggsel_offer_id, "active_only": active_only}


async def _run_remove_consent_option(limit: int = 0, ggsel_offer_id: int = 0, active_only: bool = False):
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.clients.ggsel import _CONSENT_TITLE_RU

    async with AsyncSessionLocal() as db:
        q = select(Offer).where(Offer.ggsel_offer_id.isnot(None))
        if active_only:
            q = q.where(Offer.status == OfferStatus.active)
        offers = (await db.execute(q)).scalars().all()

    if ggsel_offer_id:
        offers = [o for o in offers if o.ggsel_offer_id == ggsel_offer_id]
    elif limit and limit > 0:
        offers = offers[:limit]

    total = len(offers)
    conc = min(settings.sync_concurrency, 4)
    print(f"[RemoveConsent] starting — {total} offers active_only={active_only} concurrency={conc}", flush=True)
    counters = {"removed": 0, "skipped": 0, "errors": 0}
    sem = asyncio.Semaphore(conc)

    async def _rm(offer):
        async with sem:
            try:
                data = await ggsel_office.get_options(offer.ggsel_offer_id)
                opts = data if isinstance(data, list) else (data.get("options") or data.get("data") or [])
                ids = [
                    o.get("id") for o in opts
                    if (o.get("title_ru") or "").strip() == _CONSENT_TITLE_RU and o.get("id") is not None
                ]
                if not ids:
                    counters["skipped"] += 1
                    return
                await ggsel_office.delete_options(offer.ggsel_offer_id, ids)
                counters["removed"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"[RemoveConsent] ggsel_offer_id={offer.ggsel_offer_id} error: {type(e).__name__}: {e!r}", flush=True)

    for i in range(0, total, 500):
        await asyncio.gather(*[_rm(o) for o in offers[i:i + 500]])
        print(
            f"[RemoveConsent] progress {min(i + 500, total)}/{total} "
            f"removed={counters['removed']} skipped={counters['skipped']} errors={counters['errors']}",
            flush=True,
        )
    print(
        f"[RemoveConsent] done — removed={counters['removed']} skipped={counters['skipped']} "
        f"errors={counters['errors']} total={total}",
        flush=True,
    )


@app.get("/activate-batch")
async def activate_batch(limit: int = 0, max_price_rub: float = 0, dry_run: bool = False):
    """Activate a controlled batch of PAUSED offers with a live StarPets stock check.
    ?limit=N            -> at most N offers (cheapest first)
    ?max_price_rub=X    -> only offers listed at <= X rub
    ?dry_run=true       -> only report what WOULD activate, change nothing
    Only activates offers that are (a) in stock on StarPets right now and (b) still
    profitable at the listed price. Works regardless of maintenance mode."""
    import asyncio
    asyncio.create_task(_run_activate_batch(limit, max_price_rub, dry_run))
    return {"started": True, "limit": limit, "max_price_rub": max_price_rub, "dry_run": dry_run}


async def _run_activate_batch(limit: int = 0, max_price_rub: float = 0.0, dry_run: bool = False):
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.fx import get_usd_rub, item_cost_ok

    async with AsyncSessionLocal() as db:
        q = select(Offer).where(
            Offer.ggsel_offer_id.isnot(None),
            Offer.status == OfferStatus.paused,
            Offer.starpets_product_id.isnot(None),
            Offer.price_rub > 0,
        )
        if max_price_rub and max_price_rub > 0:
            q = q.where(Offer.price_rub <= max_price_rub)
        q = q.order_by(Offer.price_rub.asc())
        if limit and limit > 0:
            q = q.limit(limit)
        offers = (await db.execute(q)).scalars().all()
        snap = [
            (o.id, o.ggsel_offer_id, str(o.starpets_product_id), o.name, float(o.price_rub or 0))
            for o in offers
        ]

    total = len(snap)
    fx_rate = await get_usd_rub()
    print(
        f"[ActivateBatch] candidates={total} max_price_rub={max_price_rub} "
        f"dry_run={dry_run} fx={fx_rate} concurrency={settings.sync_concurrency}",
        flush=True,
    )

    counters = {"in_stock": 0, "no_stock": 0, "unprofitable": 0, "errors": 0}
    to_activate: list[tuple[int, int, str, float]] = []   # (offer_id, ggsel_id, name, price_usd)
    sem = asyncio.Semaphore(settings.sync_concurrency)

    async def _check(http, rec):
        offer_id, ggsel_id, product_id, name, price_rub = rec
        async with sem:
            try:
                top = await starpets.get_top_item(http, product_id)
                if not top:
                    counters["no_stock"] += 1
                    return
                price_usd = float(top.get("price_usd") or 0)
                ok, cost_rub = item_cost_ok(price_usd, fx_rate, price_rub, settings.max_cost_ratio)
                if not ok:
                    counters["unprofitable"] += 1
                    return
                counters["in_stock"] += 1
                to_activate.append((offer_id, ggsel_id, name, price_usd))
            except Exception as e:
                counters["errors"] += 1
                print(f"[ActivateBatch] check error ggsel_id={ggsel_id} name={name!r}: {e}", flush=True)

    _limits = httpx.Limits(max_connections=settings.sync_concurrency + 5)
    async with httpx.AsyncClient(timeout=15, limits=_limits) as http:
        for i in range(0, total, 500):
            await asyncio.gather(*[_check(http, r) for r in snap[i:i + 500]])
            print(
                f"[ActivateBatch] checked {min(i + 500, total)}/{total} "
                f"in_stock={counters['in_stock']} no_stock={counters['no_stock']} "
                f"unprofitable={counters['unprofitable']} errors={counters['errors']}",
                flush=True,
            )

    if dry_run:
        print(
            f"[ActivateBatch] DRY RUN — would activate {len(to_activate)} offers "
            f"(in_stock={counters['in_stock']} no_stock={counters['no_stock']} "
            f"unprofitable={counters['unprofitable']} errors={counters['errors']}); no changes made",
            flush=True,
        )
        return

    activated = 0
    act_errors = 0
    for i in range(0, len(to_activate), 100):
        batch = to_activate[i:i + 100]
        gids = [t[1] for t in batch]
        oids = [t[0] for t in batch]
        try:
            await ggsel_office.batch_activate(gids)
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(Offer).where(Offer.id.in_(oids)))).scalars().all()
                for o in rows:
                    o.status = OfferStatus.active
                await db.commit()
            activated += len(batch)
            print(f"[ActivateBatch] activated {activated}/{len(to_activate)}", flush=True)
        except Exception as e:
            act_errors += len(batch)
            print(f"[ActivateBatch] activate batch error: {e}", flush=True)
        await asyncio.sleep(0.5)

    print(
        f"[ActivateBatch] done — activated={activated} act_errors={act_errors} "
        f"in_stock={counters['in_stock']} no_stock={counters['no_stock']} "
        f"unprofitable={counters['unprofitable']} check_errors={counters['errors']} candidates={total}",
        flush=True,
    )


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
    from app.workers.deliver import _buy_with_retry

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

    item_id = str(top_item["id"])
    price_usd = float(top_item.get("price_usd") or 0)
    offer_price_rub = float(offer.price_rub or 0)

    try:
        purchased_item_id, exec_price = await _buy_with_retry(
            item_id, price_usd, offer_price_rub
        )
        return {
            "offer": {"id": offer.id, "name": offer.name, "product_id": product_id, "price_rub": offer_price_rub},
            "item": {"id": item_id, "price_usd": price_usd},
            "result": "ok",
            "purchased_item_id": purchased_item_id,
            "exec_price_usd": exec_price,
        }
    except RuntimeError as e:
        return {
            "offer": {"id": offer.id, "name": offer.name, "product_id": product_id, "price_rub": offer_price_rub},
            "item": {"id": item_id, "price_usd": price_usd},
            "result": "failed",
            "error": str(e),
        }


@app.get("/test-friendship")
async def test_friendship(trade_id: int = 57393365):
    params = {**starpets._base_params(), "tradeId": trade_id}
    headers = starpets._headers(starpets._sign(params))
    url = f"{starpets.base_url}/trades/ex-buyers/friendship"

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        resp = await client.put(url, params=params)
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    return {
        "request_url": str(resp.request.url),
        "request_headers": dict(headers),
        "request_body": None,
        "response_status": resp.status_code,
        "response_body": body,
    }


@app.get("/test-trade-status")
async def test_trade_status():
    from datetime import datetime, timezone
    today_ms = int(datetime(2026, 6, 17, tzinfo=timezone.utc).timestamp() * 1000)
    params = {**starpets._base_params(), "date": today_ms, "limit": 50}
    headers = starpets._headers(starpets._sign(params))
    url = f"{starpets.base_url}/ex-buyers/trades/updates"

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        resp = await client.get(url, params=params)
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    return {
        "request_url": str(resp.request.url),
        "request_headers": dict(headers),
        "request_body": None,
        "response_status": resp.status_code,
        "response_body": body,
    }


@app.get("/test-trade")
async def test_trade(item_id: str = "72488406", username: str = "klvcdy"):
    base = starpets._base_params()
    payload = {**base, "username": username, "items": [item_id]}
    headers = starpets._headers(starpets._sign(payload))
    url = f"{starpets.base_url}/trades/ex-buyers/withdrawal"

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        resp = await client.post(url, json=payload)
        try:
            body = resp.json()
        except Exception:
            body = resp.text

    return {
        "request_url": url,
        "request_headers": dict(headers),
        "request_body": payload,
        "response_status": resp.status_code,
        "response_body": body,
    }


@app.post("/create-all-offers")
async def create_all_offers():
    import asyncio
    asyncio.create_task(_run_create_all_offers())
    return {"started": True}


async def _run_create_all_offers():
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.workers.offer_creator import create_offer

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.id).where(Offer.status == OfferStatus.pending_create)
        )
        offer_ids = [r[0] for r in result.all()]

    print(f"[CreateAllOffers] starting — {len(offer_ids)} offers pending_create", flush=True)

    for i, offer_id in enumerate(offer_ids, 1):
        try:
            await create_offer(offer_id)
        except Exception as e:
            print(f"[CreateAllOffers] offer_id={offer_id} error: {e}", flush=True)

        if i % 50 == 0:
            print(f"[CreateAllOffers] progress {i}/{len(offer_ids)}", flush=True)

        await asyncio.sleep(3)

    print(f"[CreateAllOffers] done — {len(offer_ids)} processed", flush=True)


@app.get("/test-offers-format")
async def test_offers_format():
    import httpx as _httpx
    from app.clients.ggsel import SELLER_OFFICE_V2_URL

    headers = ggsel_office._headers()
    result = {}

    async with _httpx.AsyncClient(headers=headers, timeout=30) as client:
        # 1. Full response page 1 — including all metadata/pagination keys
        r1 = await client.get(f"{SELLER_OFFICE_V2_URL}/offers")
        data1 = r1.json() if r1.status_code == 200 else r1.text
        result["page1"] = {
            "status": r1.status_code,
            "keys": list(data1.keys()) if isinstance(data1, dict) else "array",
            "full_response": data1 if isinstance(data1, dict) else data1[:3],  # first 3 if list
        }

        # 2. Try page=2 to confirm pagination format
        r2 = await client.get(f"{SELLER_OFFICE_V2_URL}/offers", params={"page": 2})
        data2 = r2.json() if r2.status_code == 200 else r2.text
        result["page2_attempt"] = {
            "status": r2.status_code,
            "response": data2 if not isinstance(data2, list) else f"{len(data2)} items, first={data2[0].get('id') if data2 else None}",
        }

        # 3. GET individual status for the 3 offers we paused via test-batch-pause
        individual = {}
        for oid in [102438921, 102444611, 102438650]:
            r = await client.get(f"{SELLER_OFFICE_V2_URL}/offers/{oid}")
            if r.status_code == 200:
                d = r.json()
                individual[oid] = {"status": d.get("status"), "keys": list(d.keys())}
            else:
                individual[oid] = {"http": r.status_code, "response": r.text[:200]}
        result["individual_offers"] = individual

    return result


@app.get("/test-batch-pause")
async def test_batch_pause():
    import httpx as _httpx
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer
    from app.clients.ggsel import SELLER_OFFICE_V2_URL

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.ggsel_offer_id).where(Offer.ggsel_offer_id.isnot(None)).limit(3)
        )
        offer_ids = [r[0] for r in result.all()]

    if not offer_ids:
        return {"error": "no offers with ggsel_offer_id found"}

    attempts = []
    headers = ggsel_office._headers()

    async with _httpx.AsyncClient(headers=headers, timeout=30) as client:
        for url, body in [
            (f"{SELLER_OFFICE_V2_URL}/offers/batch_pause", {"offer_ids": offer_ids}),
            (f"{SELLER_OFFICE_V2_URL}/offers/batch/pause", {"ids": offer_ids}),
            (f"{SELLER_OFFICE_V2_URL}/offers/batch/pause", {"offer_ids": offer_ids}),
        ]:
            try:
                resp = await client.post(url, json=body)
                attempts.append({
                    "url": url,
                    "body": body,
                    "status": resp.status_code,
                    "response": resp.text[:500],
                })
                if resp.status_code == 200:
                    break
            except Exception as e:
                attempts.append({"url": url, "body": body, "error": str(e)})

    return {"offer_ids_tested": offer_ids, "attempts": attempts}


@app.get("/pause-all-offers")
async def pause_all_offers():
    import asyncio
    asyncio.create_task(_run_pause_all_offers())
    return {"started": True}


async def _run_pause_all_offers():
    import asyncio
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.id, Offer.ggsel_offer_id).where(Offer.ggsel_offer_id.isnot(None))
        )
        rows = result.all()

    total = len(rows)
    print(f"[PauseAllOffers] pausing {total} offers from DB in batches of 100", flush=True)

    paused_db_ids = []
    errors = 0
    batch_size = 100
    for batch_start in range(0, total, batch_size):
        batch = rows[batch_start:batch_start + batch_size]
        db_ids = [r[0] for r in batch]
        gids = [r[1] for r in batch]
        try:
            await ggsel_office.pause_offers(gids)
            paused_db_ids.extend(db_ids)
        except Exception as e:
            print(f"[PauseAllOffers] batch {batch_start}–{batch_start+len(batch)} error: {e}", flush=True)
            errors += len(batch)
        await asyncio.sleep(0.5)

    if paused_db_ids:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Offer).where(Offer.id.in_(paused_db_ids)))
            for offer in result.scalars().all():
                offer.status = OfferStatus.paused
            await db.commit()

    print(f"[PauseAllOffers] done — paused={len(paused_db_ids)} errors={errors} total={total}", flush=True)


@app.post("/activate-all-offers")
async def activate_all_offers():
    import asyncio
    asyncio.create_task(_run_activate_all_offers())
    return {"started": True}


async def _run_activate_all_offers():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus
    from app.clients.ggsel import ggsel_office

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer.ggsel_offer_id).where(
                Offer.ggsel_offer_id.isnot(None),
                Offer.status == OfferStatus.draft,
                Offer.starpets_qty > 0,
            )
        )
        offer_ids = [r[0] for r in result.all()]

    print(f"[ActivateAllOffers] starting — {len(offer_ids)} draft offers with starpets_qty>0", flush=True)

    batch_size = 100
    for batch_num, start in enumerate(range(0, len(offer_ids), batch_size), 1):
        batch = offer_ids[start:start + batch_size]
        try:
            await ggsel_office.batch_activate(batch)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Offer).where(Offer.ggsel_offer_id.in_(batch))
                )
                for offer in result.scalars().all():
                    offer.status = OfferStatus.active
                await db.commit()
            print(
                f"[ActivateAllOffers] batch {batch_num}: activated {len(batch)} offers",
                flush=True,
            )
        except Exception as e:
            print(f"[ActivateAllOffers] batch {batch_num} error: {e}", flush=True)

    print(f"[ActivateAllOffers] done — {len(offer_ids)} total", flush=True)


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


@app.get("/sku-groups")
async def sku_groups(name: str = "", min_combos: int = 2, limit: int = 40):
    """List candidate (name, pumping) groups for SKU cards, with priced-combo counts.
    ?name=Frostclaw  -> only that pet. Helps pick a prototype target."""
    from sqlalchemy import select, func
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct, Offer

    async with AsyncSessionLocal() as db:
        q = select(
            SkuProduct.name, SkuProduct.pumping, SkuProduct.rare,
            func.count(Offer.id).label("priced"),
            func.count(SkuProduct.product_id).label("combos"),
        ).join(
            Offer,
            (Offer.starpets_product_id == SkuProduct.product_id)
            & (Offer.price_rub.isnot(None)) & (Offer.price_rub > 0),
            isouter=True,
        )
        if name:
            q = q.where(SkuProduct.name == name)
        q = q.group_by(SkuProduct.name, SkuProduct.pumping, SkuProduct.rare) \
             .having(func.count(Offer.id) >= min_combos) \
             .order_by(func.count(Offer.id).desc()).limit(limit)
        rows = (await db.execute(q)).all()
    return {"groups": [
        {"name": n, "pumping": pm, "rare": rr, "priced_combos": int(pc), "total_combos": int(tc)}
        for (n, pm, rr, pc, tc) in rows
    ]}


@app.get("/build-sku-card")
async def build_sku_card_ep(name: str, pumping: str = "default"):
    """Build ONE real SKU card for (name, pumping): composited cover, username+consent+Вариант
    radio (age×fly×ride variants), webhooks, SkuVariant rows. Additive — per-combo cards untouched."""
    from app.workers.sku_builder import build_sku_card
    return await build_sku_card(name, pumping)


@app.get("/regenerate-cover")
async def regenerate_cover(ggsel_offer_id: int = 0, all_sku: bool = False):
    """Re-composite and PATCH the cover for SKU card(s) in place (no rebuild).
    ?ggsel_offer_id=N -> one card. ?all_sku=true -> every distinct SKU card."""
    import base64
    from sqlalchemy import select, func
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant, SkuProduct
    from app.images.cover import make_cover

    async with AsyncSessionLocal() as db:
        if all_sku:
            gids = [int(g) for (g,) in (await db.execute(
                select(SkuVariant.ggsel_offer_id).distinct()
            )).all()]
        elif ggsel_offer_id:
            gids = [ggsel_offer_id]
        else:
            return {"error": "pass ggsel_offer_id=N or all_sku=true"}

    if len(gids) <= 30:
        return await _run_regenerate_covers(gids)
    import asyncio
    asyncio.create_task(_run_regenerate_covers(gids))
    return {"started": True, "count": len(gids), "note": "background; see [RegenCovers] in logs"}


async def _run_regenerate_covers(gids):
    import base64, asyncio
    from sqlalchemy import select, update as sql_update
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant, SkuProduct
    from app.images.cover import make_cover, is_placeholder_bytes
    results = []
    for gid in gids:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(SkuProduct.product_id, SkuProduct.rare, SkuProduct.pumping,
                       SkuProduct.image_uri, SkuProduct.name)
                .join(SkuVariant, SkuVariant.starpets_product_id == SkuProduct.product_id)
                .where(SkuVariant.ggsel_offer_id == gid).limit(1)
            )).first()
        if not row:
            results.append({"ggsel_offer_id": gid, "error": "no variant/product"}); continue
        pid, rare, pumping, image_uri, pet_name = row
        pet_bytes = b""
        if image_uri:
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                    r = await c.get(image_uri)
                    if r.is_success:
                        pet_bytes = r.content
            except Exception as e:
                print(f"[RegenCovers] image fetch error gid={gid}: {e}", flush=True)
        missing = is_placeholder_bytes(pet_bytes)
        async with AsyncSessionLocal() as db:
            _pids = [int(x) for (x,) in (await db.execute(
                select(SkuVariant.starpets_product_id).where(SkuVariant.ggsel_offer_id == gid)
            )).all()]
            await db.execute(sql_update(SkuProduct).where(SkuProduct.product_id.in_(_pids))
                             .values(image_missing=missing))
            await db.commit()
        png = make_cover(pet_bytes, rare, pumping, name=pet_name)
        try:
            await ggsel_office.update_cover(gid, base64.b64encode(png).decode(), "image/png")
            results.append({"ggsel_offer_id": gid, "ok": True})
        except Exception as e:
            results.append({"ggsel_offer_id": gid, "error": f"{type(e).__name__}: {e}"})
        await asyncio.sleep(0.2)
    ok = sum(1 for r in results if r.get("ok"))
    print(f"[RegenCovers] done total={len(results)} ok={ok}", flush=True)
    return {"count": len(results), "ok": ok, "results": results[:50]}


async def _sku_group_list(min_combos: int, pumping: str = ""):
    """All (name, pumping) groups with >= min_combos priced combos, richest first."""
    from sqlalchemy import select, func
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct, Offer

    async with AsyncSessionLocal() as db:
        q = select(
            SkuProduct.name, SkuProduct.pumping,
            func.count(Offer.id).label("priced"),
        ).join(
            Offer,
            (Offer.starpets_product_id == SkuProduct.product_id)
            & (Offer.price_rub.isnot(None)) & (Offer.price_rub > 0),
            isouter=True,
        )
        if pumping:
            q = q.where(SkuProduct.pumping == pumping)
        q = q.group_by(SkuProduct.name, SkuProduct.pumping) \
             .having(func.count(Offer.id) >= min_combos) \
             .order_by(func.count(Offer.id).desc())
        rows = (await db.execute(q)).all()
    return [(n, pm, int(pc)) for (n, pm, pc) in rows]


@app.get("/build-all-sku-cards")
async def build_all_sku_cards(min_combos: int = 2, pumping: str = "", limit: int = 0,
                              dry_run: bool = True):
    """Mass-build SKU cards for every group with >= min_combos priced combos.
    ?dry_run=true (default) -> just list what WOULD be built (no writes).
    ?dry_run=false          -> build in background. ?limit=N caps it. ?pumping=neon filters.
    Each card self-skips if already built, so re-running only fills gaps."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct, SkuVariant

    groups = await _sku_group_list(min_combos, pumping)

    # Exclude groups already built (have SkuVariant rows) BEFORE applying limit — otherwise
    # limit=N always re-picks the same first N (fixed order) and skips them forever, never
    # reaching the unbuilt tail.
    async with AsyncSessionLocal() as db:
        built = {(n, pm) for (n, pm) in (await db.execute(
            select(SkuProduct.name, SkuProduct.pumping).join(
                SkuVariant, SkuVariant.starpets_product_id == SkuProduct.product_id
            ).distinct()
        )).all()}
    remaining = [g for g in groups if (g[0], g[1]) not in built]
    total_remaining = len(remaining)
    if limit:
        remaining = remaining[:limit]

    if dry_run:
        return {"dry_run": True, "min_combos": min_combos, "pumping": pumping or "all",
                "already_built": len(built), "remaining_unbuilt": total_remaining,
                "this_batch": len(remaining),
                "sample": [{"name": n, "pumping": pm, "priced_combos": pc}
                           for (n, pm, pc) in remaining[:30]]}
    import asyncio
    asyncio.create_task(_run_build_all(remaining))
    return {"started": True, "group_count": len(groups)}


async def _run_build_all(groups):
    import asyncio
    from app.workers.sku_builder import build_sku_card

    total = len(groups)
    built = skipped = errors = 0
    print(f"[BuildAllSku] starting — {total} groups", flush=True)
    for i, (name, pumping, priced) in enumerate(groups, 1):
        try:
            res = await build_sku_card(name, pumping)
        except Exception as e:
            errors += 1
            print(f"[BuildAllSku] {i}/{total} {name!r}/{pumping} EXC {type(e).__name__}: {e}", flush=True)
            continue
        if res.get("skipped"):
            skipped += 1
        elif res.get("error"):
            errors += 1
            print(f"[BuildAllSku] {i}/{total} {name!r}/{pumping} ERROR {res['error']}", flush=True)
        else:
            built += 1
            print(f"[BuildAllSku] {i}/{total} {name!r}/{pumping} -> gid={res.get('ggsel_offer_id')} "
                  f"variants={res.get('count')}", flush=True)
        if i % 25 == 0:
            print(f"[BuildAllSku] progress {i}/{total} built={built} skipped={skipped} errors={errors}", flush=True)
        await asyncio.sleep(0.3)   # gentle pacing for ggsel
    print(f"[BuildAllSku] DONE — total={total} built={built} skipped={skipped} errors={errors}", flush=True)


@app.get("/sku-card-stock")
async def sku_card_stock(ggsel_offer_id: int):
    """Live stock per variant of a SKU card — pick an in-stock variant to test end-to-end.
    For each variant: its label, snapshot price, and whether StarPets has a free item now."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant

    async with AsyncSessionLocal() as db:
        variants = (await db.execute(
            select(SkuVariant).where(SkuVariant.ggsel_offer_id == ggsel_offer_id)
            .order_by(SkuVariant.price_rub.asc())
        )).scalars().all()
    if not variants:
        return {"error": f"no SkuVariant rows for ggsel_offer_id={ggsel_offer_id}"}

    out = []
    async with httpx.AsyncClient(timeout=10) as http:
        for v in variants:
            top = None
            try:
                top = await starpets.get_top_item(http, str(v.starpets_product_id))
            except Exception as e:
                out.append({"product_id": v.starpets_product_id, "label": v.label,
                            "error": f"{type(e).__name__}: {e}"})
                continue
            out.append({
                "product_id": v.starpets_product_id, "label": v.label,
                "price_rub": float(v.price_rub or 0),
                "in_stock": top is not None,
                "live_floor_usd": float(top.get("price_usd")) if top else None,
            })
    in_stock = [o for o in out if o.get("in_stock")]
    return {"ggsel_offer_id": ggsel_offer_id, "variants": len(out),
            "in_stock_count": len(in_stock), "variants_detail": out}


@app.get("/debug-sku-order")
async def debug_sku_order(ggsel_order_id: int = 0, order_id: int = 0):
    """Full forensic dump of a (SKU) order: resolved product, precheck/notification events,
    and live stock — to see why a paid order hit 'no item'."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Offer, WebhookEvent, WebhookKind

    async with AsyncSessionLocal() as db:
        if order_id:
            order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        elif ggsel_order_id:
            order = (await db.execute(select(Order).where(Order.ggsel_order_id == ggsel_order_id))).scalar_one_or_none()
        else:
            return {"error": "pass ggsel_order_id or order_id"}
        if not order:
            return {"error": "order not found"}

        offer = (await db.execute(select(Offer).where(Offer.id == order.offer_id))).scalar_one_or_none()
        gid = offer.ggsel_offer_id if offer else None

        prechecks = (await db.execute(
            select(WebhookEvent).where(
                WebhookEvent.kind == WebhookKind.precheck,
                WebhookEvent.external_id.like(f"precheck-{gid}%"),
            ).order_by(WebhookEvent.processed_at.desc())
        )).scalars().all() if gid else []
        notif = (await db.execute(
            select(WebhookEvent).where(
                WebhookEvent.kind == WebhookKind.notification,
                WebhookEvent.external_id == str(order.ggsel_order_id),
            )
        )).scalar_one_or_none()

    resolved_pid = order.sku_product_id or (offer.starpets_product_id if offer else None)
    live = None
    if resolved_pid:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                top = await starpets.get_top_item(http, str(resolved_pid))
            live = {"in_stock": top is not None,
                    "floor_usd": float(top.get("price_usd")) if top else None}
        except Exception as e:
            live = {"error": f"{type(e).__name__}: {e}"}

    return {
        "order": {
            "id": order.id, "ggsel_order_id": order.ggsel_order_id,
            "offer_id": order.offer_id, "item_name": order.item_name,
            "sku_product_id": order.sku_product_id,
            "amount_rub": float(order.amount_rub) if order.amount_rub is not None else None,
            "roblox_username": order.roblox_username,
            "delivery_status": order.delivery_status.value if order.delivery_status else None,
            "error_reason": order.error_reason,
            "uniquecode": order.uniquecode,
            "bot_name": order.bot_name,
            "starpets_custom_id": order.starpets_custom_id,
            "starpets_status": order.starpets_status,
            "dispatched_at": str(order.dispatched_at),
            "last_redeliver_result": order.last_redeliver_result,
            "starpets_purchase_id": order.starpets_purchase_id,
            "paid_at": str(order.paid_at), "created_at": str(order.created_at),
        },
        "offer": {"ggsel_offer_id": gid, "name": offer.name if offer else None,
                  "starpets_product_id": offer.starpets_product_id if offer else None},
        "resolved_product_id": resolved_pid,
        "resolved_product_live_stock": live,
        "notification_seen": notif is not None,
        "precheck_events": [
            {"external_id": e.external_id, "processed_at": str(e.processed_at), "payload": e.payload}
            for e in prechecks
        ],
    }


@app.get("/retry-delivery")
async def retry_delivery(order_id: int):
    """Re-enqueue delivery for an order that failed on a transient issue (e.g. no_items_available
    when stock has since returned). Accepts the internal Order.id OR the ggsel order id.
    Safe: skips orders already dispatched/done/finalized."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Task, TaskKind, DeliveryStatus

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            order = (await db.execute(
                select(Order).where(Order.ggsel_order_id == order_id)
            )).scalar_one_or_none()
        if not order:
            return {"error": f"order {order_id} not found (tried internal id and ggsel_order_id)"}
        if order.delivery_status in (DeliveryStatus.dispatched, DeliveryStatus.done, DeliveryStatus.finalized):
            return {"error": f"order {order.id} is {order.delivery_status.value} — not retrying"}
        order.delivery_status = DeliveryStatus.pending
        order.error_reason = None
        db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=6, payload={"order_id": order.id}))
        await db.commit()
        return {"ok": True, "order_id": order.id, "ggsel_order_id": order.ggsel_order_id,
                "requeued": True, "sku_product_id": order.sku_product_id,
                "roblox_username": order.roblox_username}


@app.get("/cleanup-sku-card")
async def cleanup_sku_card(ggsel_offer_id: int, remove_option: bool = True, pause: bool = False):
    """Retire an old/prototype SKU card: delete its SkuVariant rows (so the group is no longer
    skip-guarded) and, by default, remove the bolted-on 'Вариант' radio option from the ggsel
    card. ?pause=true also pauses the card."""
    from sqlalchemy import select, delete as sql_delete
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant

    async with AsyncSessionLocal() as db:
        option_ids = [int(o) for (o,) in (await db.execute(
            select(SkuVariant.ggsel_option_id).where(
                SkuVariant.ggsel_offer_id == ggsel_offer_id,
                SkuVariant.ggsel_option_id.isnot(None),
            ).distinct()
        )).all()]
        n_rows = (await db.execute(
            select(SkuVariant.id).where(SkuVariant.ggsel_offer_id == ggsel_offer_id)
        )).all()

    result = {"ggsel_offer_id": ggsel_offer_id, "variant_rows": len(n_rows),
              "option_ids": option_ids}

    if remove_option and option_ids:
        try:
            await ggsel_office.delete_options(ggsel_offer_id, option_ids)
            result["option_removed"] = True
        except Exception as e:
            result["option_remove_error"] = f"{type(e).__name__}: {e}"

    if pause:
        try:
            await ggsel_office.pause_offer(ggsel_offer_id)
            result["paused"] = True
        except Exception as e:
            result["pause_error"] = f"{type(e).__name__}: {e}"

    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(SkuVariant).where(SkuVariant.ggsel_offer_id == ggsel_offer_id))
        await db.commit()
    result["variant_rows_deleted"] = True
    return result


@app.get("/close-order")
async def close_order(order_id: int):
    """Operator override: mark a confirmed-delivered order as done. For false-positive
    needs_attention (e.g. timer expired at status 4 while the trade actually completed).
    Accepts internal Order.id or the ggsel order id."""
    from datetime import datetime
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, DeliveryStatus

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            order = (await db.execute(
                select(Order).where(Order.ggsel_order_id == order_id)
            )).scalar_one_or_none()
        if not order:
            return {"error": f"order {order_id} not found (tried internal id and ggsel_order_id)"}
        prev = order.delivery_status.value if order.delivery_status else None
        order.delivery_status = DeliveryStatus.done
        if order.delivered_at is None:
            order.delivered_at = datetime.utcnow()
        order.error_reason = None
        await db.commit()
        return {"ok": True, "order_id": order.id, "ggsel_order_id": order.ggsel_order_id,
                "previous_status": prev, "delivery_status": "done"}


@app.get("/activate-sku-cards")
async def activate_sku_cards(limit: int = 0, dry_run: bool = True, chunk_size: int = 1):
    """Publish SKU-card drafts (they're created as drafts, like normal offers). Activates every
    distinct ggsel card that has SkuVariant rows, in batches of 100 via ggsel batch_activate.
    ?dry_run=true (default) just lists what would be activated. Per-variant availability is
    enforced at precheck, so no pre-activation stock gate is needed."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant

    async with AsyncSessionLocal() as db:
        gids = [int(g) for (g,) in (await db.execute(
            select(SkuVariant.ggsel_offer_id).distinct()
        )).all()]
    gids.sort()
    if limit and limit > 0:
        gids = gids[:limit]

    if dry_run:
        return {"dry_run": True, "sku_card_count": len(gids), "sample": gids[:30]}

    cs = max(1, chunk_size)
    # Small runs inline (immediate result); large ones in background to avoid HTTP timeout.
    if len(gids) <= 60:
        return await _run_activate_sku(gids, cs, background=False)
    import asyncio
    asyncio.create_task(_run_activate_sku(gids, cs, background=True))
    return {"started": True, "total": len(gids), "chunk_size": cs,
            "note": "running in background (one batch_activate per chunk); verify with /sku-card-statuses"}


async def _run_activate_sku(gids, cs, background):
    import asyncio
    activated = 0
    errors = []
    for i in range(0, len(gids), cs):
        chunk = gids[i:i + cs]
        try:
            await ggsel_office.batch_activate(chunk)
            activated += len(chunk)
        except Exception as e:
            errors.append({"chunk_start": i, "gids": chunk, "error": f"{type(e).__name__}: {e}"})
        await asyncio.sleep(0.25)
    summary = {"activated_submitted": activated, "total": len(gids), "chunk_size": cs,
               "errors": errors[:20], "error_count": len(errors)}
    print(f"[ActivateSku] {summary}", flush=True)
    if background:
        return summary
    summary["note"] = "batch_activate is async; verify with /sku-card-statuses after ~1-2 min"
    return summary


@app.get("/cleanup-sku-by-name")
async def cleanup_sku_by_name(name: str, dry_run: bool = True):
    """Clear the DB traces of a pet's SKU cards (SkuVariant rows + backing __sku__ offers) when
    the ggsel cards were deleted manually and the ids weren't kept. Un-skip-guards the pet so it
    rebuilds fresh. Does NOT touch ggsel (cards already gone) or per-combo offers."""
    from sqlalchemy import select, delete as sql_delete
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct, SkuVariant, Offer, Order

    async with AsyncSessionLocal() as db:
        pids = [int(p) for (p,) in (await db.execute(
            select(SkuProduct.product_id).where(SkuProduct.name == name)
        )).all()]
        if not pids:
            return {"error": f"no sku_products for name={name!r}"}

        gids = [int(g) for (g,) in (await db.execute(
            select(SkuVariant.ggsel_offer_id).where(
                SkuVariant.starpets_product_id.in_(pids)
            ).distinct()
        )).all()]
        n_variants = len((await db.execute(
            select(SkuVariant.id).where(SkuVariant.starpets_product_id.in_(pids))
        )).all())

        if dry_run:
            return {"dry_run": True, "name": name, "product_ids": len(pids),
                    "sku_variant_rows": n_variants, "backing_offer_gids": gids}

        # 1. Drop variant rows — this alone un-skip-guards the group so it rebuilds.
        await db.execute(sql_delete(SkuVariant).where(SkuVariant.starpets_product_id.in_(pids)))

        # 2. Delete backing __sku__ offers, but FK-safe: skip any still referenced by an order
        #    (test orders point at them). Those are harmless orphans; the neon one is repointed
        #    on rebuild (same composite key), a stale default one just lingers unused.
        deleted_offers = 0
        kept_with_orders = []
        if gids:
            backing = (await db.execute(
                select(Offer.id, Offer.ggsel_offer_id).where(
                    Offer.age == "__sku__", Offer.ggsel_offer_id.in_(gids)
                )
            )).all()
            for off_id, gid in backing:
                has_order = (await db.execute(
                    select(Order.id).where(Order.offer_id == off_id).limit(1)
                )).first()
                if has_order:
                    kept_with_orders.append(gid)
                    continue
                await db.execute(sql_delete(Offer).where(Offer.id == off_id))
                deleted_offers += 1
        await db.commit()
    return {"name": name, "deleted_sku_variant_rows": n_variants,
            "deleted_backing_offers": deleted_offers,
            "kept_backing_offers_with_orders": kept_with_orders, "gids": gids}


@app.get("/sku-card-statuses")
async def sku_card_statuses(limit: int = 20):
    """Diagnostic: fetch the real ggsel status of a sample of SKU cards, so we can see whether
    they're still 'draft' (batch_activate was a no-op → wrong endpoint) or in moderation."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant
    from collections import Counter

    async with AsyncSessionLocal() as db:
        gids = [int(g) for (g,) in (await db.execute(
            select(SkuVariant.ggsel_offer_id).distinct()
        )).all()]
    gids.sort()
    sample = gids[:limit]

    dist = Counter()
    rows = []
    for gid in sample:
        try:
            data = await ggsel_office.get_offer(gid)
            off = data.get("data") if isinstance(data, dict) and "data" in data else data
            status = (off or {}).get("status")
        except Exception as e:
            status = f"ERR {type(e).__name__}"
        dist[str(status)] += 1
        rows.append({"ggsel_offer_id": gid, "status": status})
    return {"sku_cards_total": len(gids), "sampled": len(sample),
            "status_distribution": dict(dist), "rows": rows}


@app.get("/reset-sku-cards")
async def reset_sku_cards(dry_run: bool = True):
    """Wipe ALL SKU cards (delete on ggsel + clear DB traces) so the whole set rebuilds fresh
    — used when the card layout/labels change. Backing __sku__ offers still referenced by an
    order are kept (FK-safe); those ggsel cards are still deleted."""
    from sqlalchemy import select, delete as sql_delete
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant, Offer, Order

    async with AsyncSessionLocal() as db:
        gids = sorted({int(g) for (g,) in (await db.execute(
            select(SkuVariant.ggsel_offer_id).distinct()
        )).all()})

    if dry_run:
        return {"dry_run": True, "sku_cards": len(gids), "sample": gids[:30]}

    # 1. Delete the cards on ggsel (chunks of 100).
    deleted_ggsel = 0
    ggsel_errors = []
    for i in range(0, len(gids), 100):
        chunk = gids[i:i + 100]
        try:
            await ggsel_office.delete_offers(chunk)
            deleted_ggsel += len(chunk)
        except Exception as e:
            ggsel_errors.append({"chunk_start": i, "error": f"{type(e).__name__}: {e}"})
        import asyncio
        await asyncio.sleep(0.3)

    # 2. Clear DB: all SkuVariant rows, and orderless __sku__ backing offers.
    async with AsyncSessionLocal() as db:
        n_variants = len((await db.execute(select(SkuVariant.id))).all())
        await db.execute(sql_delete(SkuVariant))
        backing = (await db.execute(
            select(Offer.id, Offer.ggsel_offer_id).where(Offer.age == "__sku__")
        )).all()
        deleted_offers = kept = 0
        for off_id, _gid in backing:
            has_order = (await db.execute(
                select(Order.id).where(Order.offer_id == off_id).limit(1)
            )).first()
            if has_order:
                kept += 1
                continue
            await db.execute(sql_delete(Offer).where(Offer.id == off_id))
            deleted_offers += 1
        await db.commit()

    return {"sku_cards": len(gids), "deleted_on_ggsel": deleted_ggsel,
            "ggsel_errors": ggsel_errors, "deleted_sku_variant_rows": n_variants,
            "deleted_backing_offers": deleted_offers,
            "kept_backing_offers_with_orders": kept}


@app.get("/probe-activate")
async def probe_activate(ggsel_offer_id: int):
    """One-shot: try both activate endpoints on one draft and report which flips it out of
    'draft'. batch_activate = POST /offers/batch_activate; activate_offers = POST /offers/batch/activate."""
    async def _status():
        try:
            data = await ggsel_office.get_offer(ggsel_offer_id)
            off = data.get("data") if isinstance(data, dict) and "data" in data else data
            return (off or {}).get("status")
        except Exception as e:
            return f"ERR {type(e).__name__}: {e}"

    out = {"ggsel_offer_id": ggsel_offer_id, "initial_status": await _status()}

    try:
        out["batch_activate_resp"] = await ggsel_office.batch_activate([ggsel_offer_id])
    except Exception as e:
        out["batch_activate_resp"] = f"ERR {type(e).__name__}: {e}"
    out["status_after_batch_activate"] = await _status()

    try:
        out["activate_offers_resp"] = await ggsel_office.activate_offers([ggsel_offer_id])
    except Exception as e:
        out["activate_offers_resp"] = f"ERR {type(e).__name__}: {e}"
    out["status_after_activate_offers"] = await _status()

    return out


@app.get("/probe-matrix")
async def probe_matrix():
    """Create a throwaway offer with TWO radio params (Age + Modifier) and dump the raw offer +
    options structure — to see whether ggsel v2 supports per-combination (matrix) pricing or
    only additive per-variant modifiers that sum. Returns gid; delete it manually after."""
    resp = await ggsel_office.create_offer(
        title_ru="__probe_matrix__", title_en="__probe_matrix__",
        description_ru="probe", description_en="probe",
        instructions_ru="probe", instructions_en="probe",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid in create_offer response: {resp}"}

    steps = {"gid": gid}
    try:
        opt_age = await ggsel_office.create_radio_option(gid, "Возраст", "Age", position=1)
        await ggsel_office.add_variant(gid, opt_age, "Teen", "Teen", 0, is_default=True, position=0)
        await ggsel_office.add_variant(gid, opt_age, "Full Grown", "Full Grown", 50, position=1)

        opt_mod = await ggsel_office.create_radio_option(gid, "Модификатор", "Modifier", position=2)
        await ggsel_office.add_variant(gid, opt_mod, "Обычный", "Base", 0, is_default=True, position=0)
        await ggsel_office.add_variant(gid, opt_mod, "Летает", "Fly", 100, position=1)
        steps["option_ids"] = {"age": opt_age, "modifier": opt_mod}
    except Exception as e:
        steps["build_error"] = f"{type(e).__name__}: {e}"

    try:
        steps["offer"] = await ggsel_office.get_offer(gid)
    except Exception as e:
        steps["offer_error"] = f"{type(e).__name__}: {e}"
    try:
        steps["options"] = await ggsel_office.get_options(gid)
    except Exception as e:
        steps["options_error"] = f"{type(e).__name__}: {e}"

    steps["note"] = "Inspect for combinations/matrix/price_table fields; delete this test offer manually."
    return steps


@app.get("/probe-variant-patch")
async def probe_variant_patch():
    """Phase 3 feasibility: can we PATCH an existing variant's price/default in place?
    Creates a throwaway offer+option+variant, tries two PATCH body formats, verifies. Delete gid after."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_vpatch__", title_en="__probe_vpatch__",
        description_ru="probe", description_en="probe",
        instructions_ru="probe", instructions_en="probe",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}

    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Возраст", "Age", position=1)
        vid = await ggsel_office.add_variant(gid, opt, "Teen", "Teen", 0, is_default=True, position=0)
        await ggsel_office.add_variant(gid, opt, "Full Grown", "Full Grown", 10, position=1)
        out["option_id"], out["variant_id"] = opt, vid
    except Exception as e:
        out["build_error"] = f"{type(e).__name__}: {e}"
        return out

    url = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}/variants/{vid}"
    async def _patch(body, label):
        async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
            try:
                r = await c.patch(url, json=body)
                out[label] = {"status": r.status_code, "body": r.text[:400]}
            except Exception as e:
                out[label] = {"error": f"{type(e).__name__}: {e}"}

    # Format A: direct fields
    await _patch({"price": 77.0, "discount_type": "fixed", "impact_type": "increase",
                  "is_default": True, "status": "active"}, "patch_direct")
    # Format B: wrapped like POST
    await _patch({"variants": [{"id": vid, "price": 88.0, "discount_type": "fixed",
                                "impact_type": "increase", "is_default": True, "status": "active"}]},
                 "patch_wrapped")

    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["final_variants"] = [{"id": v.get("id"), "price": v.get("price"),
                                          "is_default": v.get("is_default")} for v in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "If a variant price became 77 or 88 -> PATCH works (Phase 3 in-place). Delete gid after."
    return out


@app.get("/probe-variant-update2")
async def probe_variant_update2():
    """Phase 3: find HOW ggsel updates an existing variant. Tries several endpoint/method combos
    with DISTINCT target prices so the final variant price reveals which one worked. Delete gid after."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_vu2__", title_en="__probe_vu2__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Возраст", "Age", position=1)
        vid = await ggsel_office.add_variant(gid, opt, "Teen", "Teen", 0, is_default=True, position=0)
        vid2 = await ggsel_office.add_variant(gid, opt, "Full Grown", "Full Grown", 10, position=1)
        out["option_id"], out["variant_id"], out["variant_id2"] = opt, vid, vid2
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    B = f"{SELLER_OFFICE_V2_URL}/offers"
    def _var(price):
        return {"id": vid, "title_ru": "Teen", "title_en": "Teen", "price": price,
                "discount_type": "fixed", "impact_type": "increase", "is_default": True,
                "status": "active", "position": 0}
    candidates = [
        ("A_patch_option",   "PATCH", f"{B}/{gid}/options/{opt}",               {"variants": [_var(71)]}),
        ("B_put_variant",    "PUT",   f"{B}/{gid}/options/{opt}/variants/{vid}", _var(72)),
        ("C_patch_offer",    "PATCH", f"{B}/{gid}",                             {"options": [{"id": opt, "variants": [_var(73)]}]}),
        ("D_post_with_id",   "POST",  f"{B}/{gid}/options/{opt}/variants",       {"variants": [_var(74)]}),
    ]
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
        for label, method, url, body in candidates:
            try:
                r = await c.request(method, url, json=body)
                out[label] = {"method": method, "status": r.status_code, "body": r.text[:200]}
            except Exception as e:
                out[label] = {"method": method, "error": f"{type(e).__name__}: {e}"}

    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["final_variants"] = [{"id": v.get("id"), "title": v.get("title_ru"),
                                          "price": v.get("price")} for v in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "Teen price 71/72/73/74 reveals which worked (A/B/C/D). Check variant ids stayed stable. Delete gid."
    return out


@app.get("/probe-v2-put-option")
async def probe_v2_put_option():
    """Phase 3 linchpin: does our v2 seller API (api-key auth) accept the UI's PUT-option-with-
    variants_attributes format? If yes -> in-place variant update (ids preserved). Delete gid after."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_v2put__", title_en="__probe_v2put__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Возраст", "Age", position=3)
        vid = await ggsel_office.add_variant(gid, opt, "Teen", "Teen", 0, is_default=True, position=0)
        vid2 = await ggsel_office.add_variant(gid, opt, "Full Grown", "Full Grown", 10, position=1)
        out["option_id"], out["variant_id"], out["variant_id2"] = opt, vid, vid2
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    url = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}"
    def _body(teen_price):
        variants = [
            {"id": vid, "position": 0, "title_ru": "Teen", "title_en": "Teen",
             "discount_kind": "fixed", "impact_variant": "increase", "default": True, "price": teen_price},
            {"id": vid2, "position": 1, "title_ru": "Full Grown", "title_en": "Full Grown",
             "discount_kind": "fixed", "impact_variant": "increase", "default": False, "price": 10},
        ]
        inner = {"kind": "radio_button", "position": 3, "title_ru": "Возраст", "title_en": "Age",
                 "required": True, "hide_price_modifier": False, "splitted_products": False,
                 "variants_attributes": variants}
        return inner

    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
        # A: wrapped in {"option": {...}} (exactly like the UI)
        try:
            r = await c.put(url, json={"option": _body(61)})
            out["A_put_wrapped"] = {"status": r.status_code, "body": r.text[:250]}
        except Exception as e:
            out["A_put_wrapped"] = {"error": f"{type(e).__name__}: {e}"}
        # B: no wrapper (inner object directly)
        try:
            r = await c.put(url, json=_body(62))
            out["B_put_plain"] = {"status": r.status_code, "body": r.text[:250]}
        except Exception as e:
            out["B_put_plain"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["final_variants"] = [{"id": v.get("id"), "title": v.get("title_ru"),
                                          "price": v.get("price"), "is_default": v.get("is_default")}
                                         for v in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "Teen price 61 -> A works, 62 -> B works, still 0 -> v2 rejects it (use delete+re-add). Check ids stable. Delete gid."
    return out


@app.get("/probe-options-bulk")
async def probe_options_bulk():
    """Try PUT/PATCH on the PLURAL /options endpoint with the v2 option_object schema (variants
    with ids) — the doc schema implies an in-place update path. Distinct prices reveal the winner."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_optbulk__", title_en="__probe_optbulk__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Возраст", "Age", position=3)
        vid = await ggsel_office.add_variant(gid, opt, "Teen", "Teen", 0, is_default=True, position=0)
        vid2 = await ggsel_office.add_variant(gid, opt, "Full Grown", "Full Grown", 10, position=1)
        out["option_id"], out["variant_id"], out["variant_id2"] = opt, vid, vid2
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    def _opt_obj(teen_price):
        return {"id": opt, "type": "radio_button", "status": "active",
                "title_ru": "Возраст", "title_en": "Age", "is_required": True,
                "is_price_modifier_hidden": False, "position": 3,
                "variants": [
                    {"id": vid, "title_ru": "Teen", "title_en": "Teen", "price": teen_price,
                     "discount_type": "fixed", "impact_type": "increase", "is_default": True,
                     "status": "active", "position": 0},
                    {"id": vid2, "title_ru": "Full Grown", "title_en": "Full Grown", "price": 10,
                     "discount_type": "fixed", "impact_type": "increase", "is_default": False,
                     "status": "active", "position": 1},
                ]}

    url_plural = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options"
    url_single = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}"
    attempts = [
        ("A_put_plural",    "PUT",   url_plural, {"options": [_opt_obj(51)]}),
        ("B_patch_plural",  "PATCH", url_plural, {"options": [_opt_obj(52)]}),
        ("C_put_single_v2", "PUT",   url_single, _opt_obj(53)),
    ]
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
        for label, method, url, body in attempts:
            try:
                r = await c.request(method, url, json=body)
                out[label] = {"method": method, "status": r.status_code, "body": r.text[:220]}
            except Exception as e:
                out[label] = {"method": method, "error": f"{type(e).__name__}: {e}"}

    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["final_variants"] = [{"id": v.get("id"), "price": v.get("price")}
                                         for v in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "Teen 51->A, 52->B, 53->C worked. Still 0 -> no v2 update path, use delete+re-add."
    return out


@app.get("/probe-upsert-correct")
async def probe_upsert_correct():
    """Confirm: POST /variants with ids UPDATES in place (upsert). Update a NON-default variant's
    price (default stays price 0). Expect Full Grown -> 55, ids stable, still 2 variants."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_upsert__", title_en="__probe_upsert__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Возраст", "Age", position=3)
        vid = await ggsel_office.add_variant(gid, opt, "Teen", "Teen", 0, is_default=True, position=0)
        vid2 = await ggsel_office.add_variant(gid, opt, "Full Grown", "Full Grown", 10, position=1)
        out["variant_id_default"], out["variant_id_nondefault"] = vid, vid2
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    body = {"variants": [
        {"id": vid, "title_ru": "Teen", "title_en": "Teen", "price": 0,
         "discount_type": "fixed", "impact_type": "increase", "is_default": True,
         "status": "active", "position": 0},
        {"id": vid2, "title_ru": "Full Grown NEW", "title_en": "Full Grown NEW", "price": 55,
         "discount_type": "fixed", "impact_type": "increase", "is_default": False,
         "status": "active", "position": 1},
    ]}
    url = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}/variants"
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
        try:
            r = await c.post(url, json=body)
            out["upsert_resp"] = {"status": r.status_code, "body": r.text[:300]}
        except Exception as e:
            out["upsert_resp"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["final_variants"] = [{"id": v.get("id"), "title": v.get("title_ru"),
                                          "price": v.get("price"), "is_default": v.get("is_default")}
                                         for v in o.get("variants", [])]
                out["variant_count"] = len(o.get("variants", []))
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "Full Grown price 55 + same ids + count 2 => in-place upsert WORKS (Phase 3 easy). Delete gid."
    return out


@app.get("/sku-price-sync")
async def sku_price_sync_ep(dry_run: bool = True, threshold_rub: float = 5.0,
                            threshold_pct: float = 0.05, max_cards: int = 100,
                            max_rebuilds: int = 20):
    """Phase 3: refresh SKU card prices from live offers.price_rub. Cheap in-place upsert when the
    default is still ~cheapest; option rebuild (bounded by max_rebuilds) when the default drifted.
    ?dry_run=true (default) reports how many cards drifted without writing."""
    from app.workers.sku_price_sync import sku_price_sync
    if dry_run:
        return await sku_price_sync(threshold_rub=threshold_rub, threshold_pct=threshold_pct,
                                    max_cards=max_cards, max_rebuilds=max_rebuilds, dry_run=True)
    # writing pass can take minutes (rebuilds ~26 ggsel calls each) -> run in background to avoid
    # the HTTP/proxy timeout; final summary is logged as [SkuPriceSync] {...}.
    import asyncio
    asyncio.create_task(sku_price_sync(threshold_rub=threshold_rub, threshold_pct=threshold_pct,
                                       max_cards=max_cards, max_rebuilds=max_rebuilds, dry_run=False))
    return {"started": True, "max_cards": max_cards, "max_rebuilds": max_rebuilds,
            "note": "running in background; see [SkuPriceSync] summary in logs"}


@app.get("/probe-default-swap")
async def probe_default_swap():
    """Can upsert MOVE the default if the new default is sent FIRST in the array? Create A(default),
    B, C; then upsert to make C default (C first). Success => cheap path keeps default=cheapest."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_dswap__", title_en="__probe_dswap__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "V", "V", position=3)
        a = await ggsel_office.add_variant(gid, opt, "A", "A", 0, is_default=True, position=0)
        b = await ggsel_office.add_variant(gid, opt, "B", "B", 20, position=1)
        c = await ggsel_office.add_variant(gid, opt, "C", "C", 50, position=2)
        out["ids"] = {"A": a, "B": b, "C": c}
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    # Make C the default, C FIRST in the array. base becomes C's price -> A/B get modifiers.
    payload = {"variants": [
        {"id": c, "title_ru": "C", "title_en": "C", "price": 0, "discount_type": "fixed",
         "impact_type": "increase", "is_default": True, "status": "active", "position": 2},
        {"id": a, "title_ru": "A", "title_en": "A", "price": 30, "discount_type": "fixed",
         "impact_type": "increase", "is_default": False, "status": "active", "position": 0},
        {"id": b, "title_ru": "B", "title_en": "B", "price": 10, "discount_type": "fixed",
         "impact_type": "increase", "is_default": False, "status": "active", "position": 1},
    ]}
    url = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}/variants"
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as cl:
        try:
            r = await cl.post(url, json=payload)
            out["swap_resp"] = {"status": r.status_code, "body": r.text[:300]}
        except Exception as e:
            out["swap_resp"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["final"] = [{"id": v.get("id"), "title": v.get("title_ru"),
                                 "price": v.get("price"), "is_default": v.get("is_default")}
                                for v in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "If C is_default=true (200) => default-first ordering fixes the swap. Delete gid."
    return out


@app.get("/fix-duplicate-options")
async def fix_duplicate_options(ggsel_offer_id: int = 0, all_sku: bool = False, dry_run: bool = True):
    """Repair cards left with duplicate/orphan 'Вариант' options by failed rebuilds. Keeps the one
    referenced by SkuVariant.ggsel_option_id, deletes the rest. ?all_sku=true scans every SKU card."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant

    async with AsyncSessionLocal() as db:
        if all_sku:
            gids = [int(g) for (g,) in (await db.execute(
                select(SkuVariant.ggsel_offer_id).distinct()
            )).all()]
        elif ggsel_offer_id:
            gids = [ggsel_offer_id]
        else:
            return {"error": "pass ggsel_offer_id or all_sku=true"}

    results = []
    for gid in gids:
        async with AsyncSessionLocal() as db:
            keep = {int(o) for (o,) in (await db.execute(
                select(SkuVariant.ggsel_option_id).where(
                    SkuVariant.ggsel_offer_id == gid, SkuVariant.ggsel_option_id.isnot(None)
                ).distinct()
            )).all()}
        try:
            opts = await ggsel_office.get_options(gid)
            data = opts.get("data") if isinstance(opts, dict) else opts
            variant_opts = [o.get("id") for o in (data or [])
                            if o.get("type") == "radio_button"
                            and (o.get("title_ru") or "").strip() == "Вариант" and o.get("id") is not None]
            orphans = [oid for oid in variant_opts if oid not in keep]
            if not orphans:
                continue
            entry = {"ggsel_offer_id": gid, "variant_options": variant_opts,
                     "keep": list(keep), "orphans": orphans}
            if not dry_run:
                await ggsel_office.delete_options(gid, orphans)
                entry["deleted"] = True
            results.append(entry)
        except Exception as e:
            results.append({"ggsel_offer_id": gid, "error": f"{type(e).__name__}: {e}"})
    return {"dry_run": dry_run, "cards_with_orphans": len(results), "results": results[:50]}


@app.get("/set-order-product")
async def set_order_product(order_id: int, product_id: int):
    """Operator: deliver a DIFFERENT variant of the same SKU card for a stuck order (buyer agreed
    to a substitute). Validates product_id belongs to the order's card, sets it, re-enqueues delivery.
    Same Roblox login; profitability guard at buy-time still applies."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Offer, SkuVariant, Task, TaskKind, DeliveryStatus

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            order = (await db.execute(select(Order).where(Order.ggsel_order_id == order_id))).scalar_one_or_none()
        if not order:
            return {"error": f"order {order_id} not found"}
        if order.delivery_status in (DeliveryStatus.dispatched, DeliveryStatus.done, DeliveryStatus.finalized):
            return {"error": f"order {order.id} is {order.delivery_status.value} — not overriding"}
        if order.starpets_purchase_id:
            return {"error": f"order {order.id} already bought item {order.starpets_purchase_id} — can't switch"}

        offer = (await db.execute(select(Offer).where(Offer.id == order.offer_id))).scalar_one_or_none()
        gid = offer.ggsel_offer_id if offer else None
        # substitute must be a variant of the SAME card
        sv = (await db.execute(select(SkuVariant).where(
            SkuVariant.ggsel_offer_id == gid, SkuVariant.starpets_product_id == product_id
        ).limit(1))).scalar_one_or_none()
        if not sv:
            return {"error": f"product {product_id} is not a variant of this card (ggsel_offer_id={gid})"}

        order.sku_product_id = product_id
        order.delivery_status = DeliveryStatus.pending
        order.error_reason = None
        db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=6, payload={"order_id": order.id}))
        await db.commit()
        return {"ok": True, "order_id": order.id, "roblox_username": order.roblox_username,
                "new_sku_product_id": product_id, "label": sv.label, "requeued": True}


@app.get("/fix-sku-unlimited")
async def fix_sku_unlimited(dry_run: bool = True):
    """Set every SKU card to unlimited quantity (they were created quantity=1 -> 'sold out' after
    one purchase). ?dry_run=true just counts; false applies (background if many)."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuVariant

    async with AsyncSessionLocal() as db:
        gids = sorted({int(g) for (g,) in (await db.execute(
            select(SkuVariant.ggsel_offer_id).distinct()
        )).all()})
    if dry_run:
        return {"dry_run": True, "sku_cards": len(gids), "sample": gids[:20]}
    if len(gids) <= 60:
        return await _run_fix_unlimited(gids, background=False)
    import asyncio
    asyncio.create_task(_run_fix_unlimited(gids, background=True))
    return {"started": True, "sku_cards": len(gids), "note": "background; see [FixUnlimited] in logs"}


async def _run_fix_unlimited(gids, background):
    import asyncio
    print(f"[FixUnlimited] START total={len(gids)}", flush=True)
    ok = 0
    errors = []
    for i, gid in enumerate(gids, 1):
        try:
            await ggsel_office.set_unlimited(gid)
            ok += 1
        except Exception as e:
            errors.append({"gid": gid, "error": f"{type(e).__name__}: {e}"})
        if i % 25 == 0:
            print(f"[FixUnlimited] progress {i}/{len(gids)} ok={ok} errors={len(errors)}", flush=True)
        await asyncio.sleep(0.2)
    summary = {"updated": ok, "total": len(gids), "errors": errors[:20], "error_count": len(errors)}
    print(f"[FixUnlimited] DONE {summary}", flush=True)
    return summary



@app.get("/probe-unlimited")
async def probe_unlimited(ggsel_offer_id: int):
    """Sync test: PATCH is_unlimited_quantity on ONE card, return ggsel's raw status+body, then
    read back the offer's quantity fields — to confirm the field is accepted."""
    import httpx as _httpx
    url = f"{SELLER_OFFICE_V2_URL}/offers/{ggsel_offer_id}"
    out = {"ggsel_offer_id": ggsel_offer_id}
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
        try:
            r = await c.patch(url, json={"is_unlimited_quantity": True})
            out["patch"] = {"status": r.status_code, "body": r.text[:300]}
        except Exception as e:
            out["patch"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        data = await ggsel_office.get_offer(ggsel_offer_id)
        off = data.get("data") if isinstance(data, dict) and "data" in data else data
        out["after"] = {"is_unlimited_quantity": (off or {}).get("is_unlimited_quantity"),
                        "quantity": (off or {}).get("quantity"),
                        "status": (off or {}).get("status")}
    except Exception as e:
        out["after_error"] = f"{type(e).__name__}: {e}"
    return out


@app.get("/probe-hide-variant")
async def probe_hide_variant():
    """Does status='inactive' hide a variant from the buyer's radio? Create A(default)+B, set B
    inactive via upsert, report B's status. Then open the card on ggsel and check if B is gone."""
    resp = await ggsel_office.create_offer(
        title_ru="__probe_hide__", title_en="__probe_hide__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Вариант", "Variant", position=3)
        a = await ggsel_office.add_variant(gid, opt, "A (in stock)", "A", 0, is_default=True, position=0)
        b = await ggsel_office.add_variant(gid, opt, "B (out of stock)", "B", 20, position=1)
        out["ids"] = {"A": a, "B": b}
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    payload = [
        {"id": a, "title_ru": "A (in stock)", "title_en": "A", "price": 0, "discount_type": "fixed",
         "impact_type": "increase", "is_default": True, "status": "active", "position": 0},
        {"id": b, "title_ru": "B (out of stock)", "title_en": "B", "price": 20, "discount_type": "fixed",
         "impact_type": "increase", "is_default": False, "status": "inactive", "position": 1},
    ]
    try:
        out["upsert"] = await ggsel_office.update_variants(gid, opt, payload)
    except Exception as e:
        out["upsert_error"] = f"{type(e).__name__}: {e}"
    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["variants_after"] = [{"id": v.get("id"), "title": v.get("title_ru"),
                                          "status": v.get("status")} for v in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "Open this card (gid) on ggsel: if B is NOT in the Вариант list -> inactive hides it. Delete gid."
    return out


@app.get("/probe-variant-statuses")
async def probe_variant_statuses():
    """Find a valid 'hidden' status for a variant. Create A(default)+B, try setting B to each
    candidate status, report which ggsel accepts (200) vs '422 invalid'. Delete gid after."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_vstat__", title_en="__probe_vstat__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Вариант", "Variant", position=3)
        a = await ggsel_office.add_variant(gid, opt, "A", "A", 0, is_default=True, position=0)
        b = await ggsel_office.add_variant(gid, opt, "B", "B", 20, position=1)
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    url = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}/variants"
    candidates = ["hidden", "disabled", "paused", "archived", "not_active", "deleted", "draft", "off"]
    out["results"] = {}
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
        for st in candidates:
            body = {"variants": [{"id": b, "title_ru": "B", "title_en": "B", "price": 20,
                                  "discount_type": "fixed", "impact_type": "increase",
                                  "is_default": False, "status": st, "position": 1}]}
            try:
                r = await c.post(url, json=body)
                out["results"][st] = {"status": r.status_code, "body": r.text[:120]}
            except Exception as e:
                out["results"][st] = {"error": f"{type(e).__name__}: {e}"}
    out["note"] = "Any status with 200 = accepted. Then check if B disappears from the buyer's radio. Delete gid."
    return out


@app.get("/probe-unarchive")
async def probe_unarchive():
    """Can an archived variant be brought back to active (clean toggle) or not (must recreate)?
    Create A+B, archive B, then try B->active, report. Delete gid after."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_unarch__", title_en="__probe_unarch__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Вариант", "Variant", position=3)
        a = await ggsel_office.add_variant(gid, opt, "A", "A", 0, is_default=True, position=0)
        b = await ggsel_office.add_variant(gid, opt, "B", "B", 20, position=1)
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    url = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}/variants"
    def _b(status):
        return {"variants": [{"id": b, "title_ru": "B", "title_en": "B", "price": 20,
                              "discount_type": "fixed", "impact_type": "increase",
                              "is_default": False, "status": status, "position": 1}]}
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as c:
        try:
            r = await c.post(url, json=_b("archived"))
            out["archive"] = {"status": r.status_code, "body": r.text[:150]}
        except Exception as e:
            out["archive"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            r = await c.post(url, json=_b("active"))
            out["unarchive_active"] = {"status": r.status_code, "body": r.text[:150]}
        except Exception as e:
            out["unarchive_active"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["variants_after"] = [{"id": v.get("id"), "title": v.get("title_ru"),
                                          "status": v.get("status")} for v in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    out["note"] = "unarchive_active 200 + B active => clean toggle. 404 => must recreate. Delete gid."
    return out


@app.get("/probe-unarchive2")
async def probe_unarchive2():
    """Can an archived variant be resurrected by sending the FULL variant set (with it = active)?
    Create A(default)+B+C, archive B alone, then POST all three active -> is B back? Delete gid."""
    import httpx as _httpx
    resp = await ggsel_office.create_offer(
        title_ru="__probe_unarch2__", title_en="__probe_unarch2__",
        description_ru="p", description_en="p", instructions_ru="p", instructions_en="p",
        category_id=122921, cover_base64="", price=100.0,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        return {"error": f"no gid: {resp}"}
    out = {"gid": gid}
    try:
        opt = await ggsel_office.create_radio_option(gid, "Вариант", "Variant", position=3)
        a = await ggsel_office.add_variant(gid, opt, "A", "A", 0, is_default=True, position=0)
        b = await ggsel_office.add_variant(gid, opt, "B", "B", 20, position=1)
        c = await ggsel_office.add_variant(gid, opt, "C", "C", 30, position=2)
        out["ids"] = {"A": a, "B": b, "C": c}
    except Exception as e:
        return {**out, "build_error": f"{type(e).__name__}: {e}"}

    url = f"{SELLER_OFFICE_V2_URL}/offers/{gid}/options/{opt}/variants"
    def v(vid, title, price, st, is_def, pos):
        return {"id": vid, "title_ru": title, "title_en": title, "price": price,
                "discount_type": "fixed", "impact_type": "increase",
                "is_default": is_def, "status": st, "position": pos}
    async with _httpx.AsyncClient(headers=ggsel_office._headers(), timeout=30) as cl:
        # archive B alone
        try:
            r = await cl.post(url, json={"variants": [v(b, "B", 20, "archived", False, 1)]})
            out["archive_B"] = {"status": r.status_code}
        except Exception as e:
            out["archive_B"] = {"error": str(e)}
        # resurrect via FULL set (all active)
        try:
            r = await cl.post(url, json={"variants": [
                v(a, "A", 0, "active", True, 0), v(b, "B", 20, "active", False, 1),
                v(c, "C", 30, "active", False, 2)]})
            out["resurrect_fullset"] = {"status": r.status_code, "body": r.text[:200]}
        except Exception as e:
            out["resurrect_fullset"] = {"error": str(e)}
    try:
        opts = await ggsel_office.get_options(gid)
        data = opts.get("data") if isinstance(opts, dict) else opts
        for o in (data or []):
            if o.get("id") == opt:
                out["variants_after"] = [{"id": vv.get("id"), "title": vv.get("title_ru"),
                                          "status": vv.get("status")} for vv in o.get("variants", [])]
    except Exception as e:
        out["verify_error"] = str(e)
    out["note"] = "If B is back (active) in variants_after -> full-set resurrect works = clean toggle. Delete gid."
    return out


@app.get("/sku-stock-sync")
async def sku_stock_sync_ep(dry_run: bool = True, max_cards: int = 40):
    """Stock-driven variant visibility: hide (archive) out-of-stock variants, rebuild the option
    when the in-stock set changes, pause a card with nothing in stock. ?dry_run=true just counts."""
    from app.workers.sku_stock_sync import sku_stock_sync
    if dry_run:
        return await sku_stock_sync(max_cards=max_cards, dry_run=True)
    import asyncio
    asyncio.create_task(sku_stock_sync(max_cards=max_cards, dry_run=False))
    return {"started": True, "max_cards": max_cards, "note": "background; see [SkuStockSync] in logs"}


@app.get("/force-deliver")
async def force_deliver(order_id: int, confirm: bool = False):
    """Operator override: deliver an order EVEN AT A LOSS. Two-step: without confirm it shows the
    LIVE cost + estimated loss (no write); with confirm=true it actually forces the buy. Prevents
    blindly force-buying a stale-underpriced card. Accepts internal id or ggsel id."""
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Offer, Task, TaskKind, DeliveryStatus
    from app.fx import get_usd_rub, item_cost_ok

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            order = (await db.execute(select(Order).where(Order.ggsel_order_id == order_id))).scalar_one_or_none()
        if not order:
            return {"error": f"order {order_id} not found"}
        if order.delivery_status in (DeliveryStatus.dispatched, DeliveryStatus.done, DeliveryStatus.finalized):
            return {"error": f"order {order.id} is {order.delivery_status.value} — not forcing"}
        offer = (await db.execute(select(Offer).where(Offer.id == order.offer_id))).scalar_one_or_none()
        product_id = order.sku_product_id or (offer.starpets_product_id if offer else None)
        sale_rub = float(order.amount_rub or 0)
        order_pk = order.id
        username = order.roblox_username

    # LIVE cost check — the whole point: show what it ACTUALLY costs right now.
    live = {"product_id": product_id}
    if product_id:
        async with httpx.AsyncClient(timeout=10) as http:
            top = await starpets.get_top_item(http, str(product_id))
        if not top:
            live["in_stock"] = False
        else:
            price_usd = float(top.get("price_usd") or 0)
            fx = await get_usd_rub()
            _ok, cost_rub = item_cost_ok(price_usd, fx, sale_rub, settings.max_cost_ratio)
            live.update({"in_stock": True, "live_price_usd": price_usd, "fx": fx,
                         "live_cost_rub": round(cost_rub, 2), "sale_rub": sale_rub,
                         "est_loss_rub": round(cost_rub - sale_rub, 2), "profitable": _ok})

    if not confirm:
        return {"preview": True, "order_id": order_pk, "roblox_username": username,
                "live": live,
                "message": "Проверь live_cost_rub и est_loss_rub. Чтобы всё равно выкупить — повтори с &confirm=true"}

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_pk))).scalar_one()
        order.force_deliver = True
        order.delivery_status = DeliveryStatus.pending
        order.error_reason = None
        db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=6, payload={"order_id": order.id}))
        await db.commit()
    return {"ok": True, "order_id": order_pk, "confirmed": True, "live": live, "requeued": True,
            "warning": "profitability guard bypassed — buying even at a loss"}


@app.get("/reconcile-stuck-offers")
async def reconcile_stuck_offers_ep(dry_run: bool = True, max_offers: int = 100):
    """Revive 'stuck' SKU-variant offers (paused + empty store_items but live stock exists): seed
    store_items + refresh offers.price_rub from the live floor + unpause. ?dry_run=true just counts."""
    from app.workers.reconcile_stuck import reconcile_stuck_offers
    if dry_run:
        return await reconcile_stuck_offers(max_offers=max_offers, dry_run=True)
    import asyncio
    asyncio.create_task(reconcile_stuck_offers(max_offers=max_offers, dry_run=False))
    return {"started": True, "max_offers": max_offers, "note": "background; see [ReconcileStuck] in logs"}


@app.get("/probe-image-hash")
async def probe_image_hash(product_ids: str = "1739700,19416,23991"):
    """Fetch each product's image_uri and hash the CONTENT — to see if 'NO IMAGE' placeholders
    share one hash (detect by hash) or differ. Pass comma-separated product_ids."""
    import hashlib
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import SkuProduct

    ids = [int(x) for x in product_ids.split(",") if x.strip().isdigit()]
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SkuProduct.product_id, SkuProduct.name, SkuProduct.image_uri)
            .where(SkuProduct.product_id.in_(ids))
        )).all()

    out = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        for pid, name, uri in rows:
            e = {"product_id": pid, "name": name, "uri": uri}
            if not uri:
                e["error"] = "no image_uri"
                out.append(e); continue
            try:
                r = await c.get(uri)
                e["status"] = r.status_code
                if r.is_success:
                    e["bytes"] = len(r.content)
                    e["sha256"] = hashlib.sha256(r.content).hexdigest()
                    # perceptual aHash (composite on white -> L -> 8x8 -> bit per pixel vs mean).
                    # Visually-identical placeholders share a near-identical aHash even if bytes differ.
                    try:
                        from PIL import Image as _Img
                        from io import BytesIO as _BIO
                        im = _Img.open(_BIO(r.content)).convert("RGBA")
                        bg = _Img.new("RGBA", im.size, (255, 255, 255, 255))
                        bg.alpha_composite(im)
                        g = bg.convert("L").resize((8, 8), _Img.LANCZOS)
                        px = list(g.getdata())
                        avg = sum(px) / len(px)
                        bits = 0
                        for i, p in enumerate(px):
                            if p >= avg:
                                bits |= (1 << i)
                        e["ahash"] = format(bits, "016x")
                    except Exception as ie:
                        e["ahash_error"] = f"{type(ie).__name__}: {ie}"
                else:
                    e["body"] = r.text[:120]
            except Exception as ex:
                e["error"] = f"{type(ex).__name__}: {ex}"
            out.append(e)
    hashes = [e.get("sha256") for e in out if e.get("sha256")]
    return {"results": out, "unique_hashes": len(set(hashes)),
            "all_same": len(set(hashes)) == 1 and len(hashes) > 1,
            "note": "all_same=true -> one placeholder hash to detect. Different -> hash per-pet or real images."}


@app.get("/resync-missing-images")
async def resync_missing_images_ep(dry_run: bool = True, max_cards: int = 400):
    """Re-sync catalog + regenerate covers for pets flagged image_missing (StarPets 'NO IMAGE').
    If art now exists, the real image replaces the name fallback. ?dry_run=true just counts."""
    from app.workers.resync_missing import resync_missing_images
    if dry_run:
        return await resync_missing_images(max_cards=max_cards, dry_run=True)
    import asyncio
    asyncio.create_task(resync_missing_images(max_cards=max_cards, dry_run=False))
    return {"started": True, "note": "background; see [ResyncMissing] in logs"}


@app.get("/floor-sweep")
async def floor_sweep_ep(dry_run: bool = True, max_offers: int = 20000):
    """DB-only: recompute the robust floor from store_items for every SKU-combo offer and rewrite
    offers.price_usd/price_rub where the price drifted (fixes frozen/underpriced offers). Phase 3
    then pushes the corrected price to the SKU card. ?dry_run=true just reports what would change."""
    from app.workers.floor_reconcile import sweep_floors
    if dry_run:
        return await sweep_floors(max_offers=max_offers, dry_run=True)
    import asyncio
    asyncio.create_task(sweep_floors(max_offers=max_offers, dry_run=False))
    return {"started": True, "note": "background; see [FloorSweep] in logs"}


@app.get("/floor-relive")
async def floor_relive_ep(dry_run: bool = True, max_products: int = 150):
    """Live items/top pull for products behind SHOWN variants: truthfully refresh store_items
    (delete phantoms), then re-price the offer from the robust floor. ?dry_run=true just counts."""
    from app.workers.floor_reconcile import relive_active
    if dry_run:
        return await relive_active(max_products=max_products, dry_run=True)
    import asyncio
    asyncio.create_task(relive_active(max_products=max_products, dry_run=False))
    return {"started": True, "max_products": max_products, "note": "background; see [FloorRelive] in logs"}


@app.get("/debug-item")
async def debug_item(item_ids: str = ""):
    """Forensic: dump StarPets' live view of specific purchased item ids (comma-separated) +
    account info. Reveals whether a 'stuck' item still exists / is ours / its reserveLevel —
    to tell 'locked in trade' from 'gone' from 'valid but request-side 130'."""
    from app.clients.starpets import starpets
    out = {"item_ids": item_ids}
    ids = [x.strip() for x in item_ids.split(",") if x.strip()]
    # 1. account identity we're acting as
    try:
        info = await starpets.get_info()
        out["account_info"] = info
    except Exception as e:
        out["account_info_error"] = f"{type(e).__name__}: {e}"
    # 2. raw item lookup
    if ids:
        try:
            out["get_items"] = await starpets.get_items(ids)
        except Exception as e:
            import httpx as _hx
            body = None
            if isinstance(e, _hx.HTTPStatusError):
                try:
                    body = e.response.json()
                except Exception:
                    body = e.response.text[:300]
            out["get_items_error"] = f"{type(e).__name__}: {e}"
            out["get_items_error_body"] = body
    return out


@app.get("/ggsel-login")
async def ggsel_login_ep():
    # Diagnostic: force an apilogin and report the result (token masked). Verifies
    # GGSEL_SELLER_ID + the sign key without exposing the token. Clears the cache first.
    import datetime as _dt
    from app.clients.ggsel import ggsel_office
    ggsel_office.__class__._pt_token = None
    ggsel_office.__class__._pt_exp = 0.0
    tok, exp = await ggsel_office._login_purchase_token()
    return {
        "seller_id": settings.ggsel_seller_id,
        "sign_key_source": "GGSEL_PURCHASE_API_KEY" if settings.ggsel_purchase_api_key else "ggsel_api_key",
        "got_token": bool(tok),
        "token_preview": (tok[:6] + "\u2026" + tok[-4:]) if tok else None,
        "valid_thru": _dt.datetime.fromtimestamp(exp).isoformat() if exp else None,
    }

@app.get("/resolve-code")
async def resolve_code_ep(code: str = "", invoice: int = 0, token: str = ""):
    """Diagnostic: probe the ggsel purchase API so we can find the working ?token=.
    /resolve-code?code=<uniquecode>         -> unique-code resolver (target endpoint)
    /resolve-code?invoice=<ggsel_order_id>  -> purchase/info (verifier)
    ?token=<override> tries a specific token; default = GGSEL_PURCHASE_TOKEN or ggsel_api_key.
    Returns raw status + body so the response tells us whether the token is accepted."""
    import httpx as _hx
    from app.clients.ggsel import ggsel_office
    base = ggsel_office._purchases_base()
    tok = token or await ggsel_office.purchase_token()
    if code:
        url = f"{base}/purchases/unique-code/{code}"
    elif invoice:
        url = f"{base}/purchase/info/{invoice}"
    else:
        return {"error": "pass ?code=<uniquecode> or ?invoice=<ggsel_order_id>"}
    out = {"url": url, "token_source": "override" if token else
           ("GGSEL_PURCHASE_TOKEN" if settings.ggsel_purchase_token else "ggsel_api_key")}
    try:
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.get(url, params={"token": tok}, headers={"Accept": "application/json"})
        out["status"] = r.status_code
        try:
            out["body"] = r.json()
        except Exception:
            out["body"] = r.text[:800]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out

@app.get("/orders-export")
async def orders_export(since: str = "", until: str = "", status: str = "all", format: str = "csv"):
    # Экспорт заказов строками для калькулятора Бухгалтерия_StarPets.xlsx (лист «Заказы»).
    # Колонки: № заказа, Товар, Цена продажи P ₽, Курс USD/RUB, Себест. выкупа $, Статус.
    # ?since=YYYY-MM-DD&until=YYYY-MM-DD — фильтр по дате оплаты; ?status=delivered|refund|all;
    # ?format=csv|json. Курс = ТЕКУЩИЙ USD/RUB (историю курса не храним) — для свежих заказов ок.
    from datetime import datetime as _dt
    from sqlalchemy import select as _select
    from app.db import AsyncSessionLocal as _S
    from app.db.models import Order as _O, DeliveryStatus as _DS
    from app.fx import get_usd_rub as _fx
    import csv as _csv, io as _io
    from fastapi.responses import PlainTextResponse as _PTR
    _DELIVERED = {_DS.dispatched, _DS.done, _DS.finalized}
    _REFUND = {_DS.failed, _DS.closed}
    def _ru(ds):
        if ds in _DELIVERED: return "Доставлен"
        if ds in _REFUND: return "Возврат"
        return ""  # pending / needs_attention — исход ещё не ясен, пропускаем
    _since = _dt.fromisoformat(since) if since else None
    _until = _dt.fromisoformat(until) if until else None
    async with _S() as db:
        rows = (await db.execute(_select(_O).order_by(_O.ggsel_order_id.asc()))).scalars().all()
    fx = await _fx()
    out = []
    for o in rows:
        ru = _ru(o.delivery_status)
        if not ru:
            continue
        if status == "delivered" and ru != "Доставлен":
            continue
        if status == "refund" and ru != "Возврат":
            continue
        d = o.paid_at or o.created_at
        dd = d.replace(tzinfo=None) if d else None
        if _since and dd and dd < _since:
            continue
        if _until and dd and dd > _until:
            continue
        out.append({
            "order_id": o.ggsel_order_id,
            "item": o.item_name,
            "sale_rub": float(o.amount_rub or 0),
            "fx": round(fx, 4),
            "cost_usd": float(o.exec_price_usd or 0),
            "status": ru,
            "raw_status": o.delivery_status.value if o.delivery_status else None,
            "date": dd.date().isoformat() if dd else "",
        })
    if format == "json":
        return {"count": len(out), "fx": round(fx, 4), "rows": out}
    buf = _io.StringIO()
    buf.write("\ufeff")  # BOM — чтобы Excel корректно открыл кириллицу
    w = _csv.writer(buf)
    w.writerow(["№ заказа", "Товар", "Цена продажи P, ₽", "Курс USD/RUB", "Себест. выкупа, $", "Статус"])
    for r in out:
        w.writerow([r["order_id"], r["item"], r["sale_rub"], r["fx"], r["cost_usd"], r["status"]])
    return _PTR(buf.getvalue(), media_type="text/csv")

from fastapi import Request as _TgRequest  # Telegram bot webhook


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: _TgRequest):
    """Telegram sends updates here. Secret path segment gates it. Processing is backgrounded
    so we return 200 immediately (Telegram retries on slow/non-200)."""
    if not settings.telegram_webhook_secret or secret != settings.telegram_webhook_secret:
        return {"ok": False}
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    import asyncio
    from app.telegram.bot import handle_update
    asyncio.create_task(handle_update(update))
    return {"ok": True}


@app.get("/set-telegram-webhook")
async def set_telegram_webhook(base_url: str = ""):
    """Register the Telegram webhook. Pass ?base_url=https://<your-railway-domain> (defaults to
    settings.public_url, which may be stale after a domain change)."""
    import httpx
    if not settings.telegram_bot_token or settings.telegram_bot_token == "dummy":
        return {"error": "TELEGRAM_BOT_TOKEN not set"}
    if not settings.telegram_webhook_secret:
        return {"error": "TELEGRAM_WEBHOOK_SECRET not set"}
    base = (base_url or settings.public_url).rstrip("/")
    url = f"{base}/telegram/webhook/{settings.telegram_webhook_secret}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
            json={"url": url, "allowed_updates": ["message", "callback_query"],
                  "drop_pending_updates": True},
        )
        try:
            tg = r.json()
        except Exception:
            tg = r.text[:300]
    return {"set_webhook_url": url, "telegram_response": tg}


@app.get("/retitle-adopt-me")
async def retitle_adopt_me(dry_run: bool = True, limit: int = 0):
    """Add ' | Adopt Me!' to every live SKU base card's ggsel title. Runs in the BACKGROUND
    (many cards -> would exceed the gateway timeout if synchronous). Watch logs for
    '[retitle-adopt-me] DONE'. Idempotent; Offer.name stays clean."""
    from sqlalchemy import select as _sel
    from app.db import AsyncSessionLocal as _S
    from app.db.models import Offer as _O
    from app.clients.ggsel import ggsel_office as _g
    import asyncio as _asyncio
    _SUFFIX = " | Adopt Me!"
    async with _S() as db:
        rows = (await db.execute(
            _sel(_O.ggsel_offer_id, _O.name).where(
                _O.age == "__sku__", _O.ggsel_offer_id.isnot(None)))).all()
    targets = [(g, n) for (g, n) in rows]
    if limit:
        targets = targets[:limit]

    def _title_for(name):
        base = (name or "").rstrip()
        return base if base.endswith(_SUFFIX) else base + _SUFFIX

    if dry_run:
        return {"dry_run": True, "count": len(targets),
                "sample": [{"gid": g, "title": _title_for(n)} for g, n in targets[:30]]}

    async def _run():
        updated = errors = 0
        total = len(targets)
        print(f"[retitle-adopt-me] START total={total}", flush=True)
        for i, (gid, name) in enumerate(targets, 1):
            title = _title_for(name)
            try:
                await _g.update_title(gid, title, title)
                updated += 1
            except Exception as e:
                errors += 1
                print(f"[retitle-adopt-me] gid={gid} ERROR {str(e)[:200]}", flush=True)
            if i % 25 == 0:
                print(f"[retitle-adopt-me] progress {i}/{total} updated={updated} errors={errors}", flush=True)
        print(f"[retitle-adopt-me] DONE updated={updated} errors={errors} total={total}", flush=True)

    _asyncio.create_task(_run())
    return {"status": "started", "total": len(targets),
            "note": "watch logs for [retitle-adopt-me] DONE"}


@app.get("/fix-username-label")
async def fix_username_label(dry_run: bool = True, limit: int = 0):
    """Add '(без знака @)' to the Roblox-username field on every live SKU base card. Background.
    Clean PATCH of the option title, fallback delete+recreate. Watch logs for '[fix-username] DONE'.
    Idempotent (skips already-updated). Base cards are Offer rows with age=='__sku__'."""
    from sqlalchemy import select as _sel
    from app.db import AsyncSessionLocal as _S
    from app.db.models import Offer as _O
    from app.clients.ggsel import ggsel_office as _g
    import asyncio as _asyncio
    _NEW_RU = "Ваш Roblox Username (без знака @)"
    _NEW_EN = "Your Roblox Username (without @)"
    async with _S() as db:
        gids = [r[0] for r in (await db.execute(
            _sel(_O.ggsel_offer_id).where(
                _O.age == "__sku__", _O.ggsel_offer_id.isnot(None)))).all()]
    if limit:
        gids = gids[:limit]
    if dry_run:
        return {"dry_run": True, "count": len(gids), "new_label": _NEW_RU}

    def _find_text_opt(data):
        opts = data if isinstance(data, list) else (data.get("options") or data.get("data") or [])
        for o in opts:
            if o.get("type") == "text" or "Username" in (o.get("title_ru") or ""):
                return o
        return None

    async def _run():
        updated = skipped = errors = 0
        total = len(gids)
        print(f"[fix-username] START total={total}", flush=True)
        for i, gid in enumerate(gids, 1):
            try:
                data = await _g.get_options(gid)
                opt = _find_text_opt(data)
                if not opt:
                    errors += 1
                    print(f"[fix-username] gid={gid} no text option", flush=True)
                elif (opt.get("title_ru") or "").strip() == _NEW_RU:
                    skipped += 1
                else:
                    try:
                        await _g.update_option(gid, opt.get("id"), _NEW_RU, _NEW_EN)
                    except Exception:
                        await _g.delete_options(gid, [opt.get("id")])
                        await _g.create_option(gid)
                    updated += 1
            except Exception as e:
                errors += 1
                print(f"[fix-username] gid={gid} ERROR {str(e)[:200]}", flush=True)
            if i % 25 == 0:
                print(f"[fix-username] progress {i}/{total} updated={updated} skipped={skipped} errors={errors}", flush=True)
        print(f"[fix-username] DONE updated={updated} skipped={skipped} errors={errors} total={total}", flush=True)

    _asyncio.create_task(_run())
    return {"status": "started", "total": len(gids), "note": "watch logs for [fix-username] DONE"}
