import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.webhooks import router as webhooks_router
from app.clients.ggsel import SELLER_OFFICE_V2_URL, ggsel_office
from app.clients.starpets import starpets
from app.config import settings

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
                # Первый визит: ищем свежий order без uniquecode (notification webhook
                # срабатывает до редиректа, поэтому order уже есть в БД)
                cutoff = datetime.utcnow() - timedelta(minutes=10)
                result = await db.execute(
                    select(Order)
                    .where(
                        Order.uniquecode.is_(None),
                        Order.delivery_status.in_([DeliveryStatus.pending, DeliveryStatus.dispatched]),
                        Order.created_at >= cutoff,
                    )
                    .order_by(Order.created_at.desc())
                    .limit(1)
                )
                order = result.scalar_one_or_none()
                if order is not None:
                    order.uniquecode = uniquecode
                    await db.commit()
                    print(f"[delivery] linked uniquecode={uniquecode!r} → order id={order.id}", flush=True)

    bot_name = (order.bot_name or "").strip() if order else ""
    status = order.delivery_status if order else None

    if status == DeliveryStatus.done or status == DeliveryStatus.finalized:
        body_html = """
        <div class="card">
            <div class="icon">✅</div>
            <h1>Предмет доставлен!</h1>
            <p class="sub">Спасибо за покупку. Проверьте свой инвентарь в Adopt Me.</p>
        </div>"""
        extra_js = ""

    elif status == DeliveryStatus.failed:
        body_html = """
        <div class="card">
            <div class="icon">⏳</div>
            <h1>Товар временно закончился</h1>
            <p class="sub">Деньги вернутся автоматически.</p>
        </div>"""
        extra_js = ""

    elif bot_name:
        body_html = f"""
        <div class="card">
            <div class="icon">🤖</div>
            <p class="label">Имя бота для трейда</p>
            <div class="bot-name">{bot_name}</div>
            <div class="timer-box">
                <span class="timer-label">Осталось времени</span>
                <div class="timer" id="timer">05:00</div>
            </div>
            <div class="steps">
                <div class="step"><span class="num">1</span>Добавьте бота <strong>{bot_name}</strong> в друзья на Roblox</div>
                <div class="step"><span class="num">2</span>Зайдите в игру <strong>Adopt Me</strong></div>
                <div class="step"><span class="num">3</span>Найдите бота в списке друзей и телепортируйтесь к нему</div>
                <div class="step"><span class="num">4</span>Примите входящий трейд</div>
            </div>
            <p class="warn">⚠️ У вас <strong>5 минут</strong> — будьте готовы заранее!</p>
        </div>"""
        extra_js = """
        (function() {
            var total = 5 * 60;
            var el = document.getElementById('timer');
            var iv = setInterval(function() {
                total--;
                if (total <= 0) { clearInterval(iv); el.textContent = '00:00'; el.style.color='#ef4444'; return; }
                var m = Math.floor(total / 60);
                var s = total % 60;
                el.textContent = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
            }, 1000);
        })();"""

    else:
        body_html = """
        <div class="card">
            <div class="spinner"></div>
            <h1>Заказ обрабатывается</h1>
            <p class="sub">Пожалуйста, подождите — это займёт несколько секунд...</p>
            <p class="sub small">Страница обновится автоматически</p>
        </div>"""
        extra_js = ""

    refresh_meta = "" if (bot_name or status in (DeliveryStatus.done, DeliveryStatus.finalized, DeliveryStatus.failed)) else '<meta http-equiv="refresh" content="5">'

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
    margin-bottom: 24px;
    word-break: break-all;
  }}
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
    line-height: 1.4;
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

        updated = skipped = errors = 0
        paused_count = 0
        to_activate: list[tuple[int, int, str]] = []  # (offer_id, ggsel_id, name)

        async with httpx.AsyncClient(timeout=15) as http:
            for i, (offer_id, ggsel_id, product_id, name, current_rub, starpets_qty, offer_status) in enumerate(offer_snapshot, 1):
                try:
                    top_item = await starpets.get_top_item(http, str(product_id))

                    # Sync status from ggsel — authoritative source
                    try:
                        ggsel_data = await ggsel_office.get_offer(ggsel_id)
                        ggsel_status_raw = (ggsel_data.get("status") or "").lower()
                        _status_map = {
                            "active": OfferStatus.active,
                            "paused": OfferStatus.paused,
                            "draft": OfferStatus.draft,
                        }
                        synced_status = _status_map.get(ggsel_status_raw)
                        if synced_status and synced_status != offer_status:
                            async with AsyncSessionLocal() as db:
                                result = await db.execute(select(Offer).where(Offer.id == offer_id))
                                _o = result.scalar_one_or_none()
                                if _o:
                                    _o.status = synced_status
                                    await db.commit()
                            print(
                                f"[SyncPrices] status synced ggsel_id={ggsel_id} name={name!r}: "
                                f"db={offer_status.value} → ggsel={synced_status.value}",
                                flush=True,
                            )
                            offer_status = synced_status
                    except Exception as _se:
                        print(f"[SyncPrices] get_offer status sync failed ggsel_id={ggsel_id}: {_se}", flush=True)

                    if not top_item:
                        # No stock — pause if currently active
                        if offer_status == OfferStatus.active:
                            try:
                                await ggsel_office.pause_offer(ggsel_id)
                                async with AsyncSessionLocal() as db:
                                    result = await db.execute(select(Offer).where(Offer.id == offer_id))
                                    offer = result.scalar_one_or_none()
                                    if offer:
                                        offer.status = OfferStatus.paused
                                        await db.commit()
                                print(f"[SyncPrices] paused ggsel_id={ggsel_id} name={name!r}", flush=True)
                                paused_count += 1
                            except Exception as e:
                                print(f"[SyncPrices] pause error ggsel_id={ggsel_id} name={name!r}: {e}", flush=True)
                                errors += 1
                        skipped += 1
                        continue

                    # In stock — collect paused offers for batch activation
                    if offer_status == OfferStatus.paused:
                        to_activate.append((offer_id, ggsel_id, name))

                    price_usd = float(top_item.get("price_usd") or 0)
                    if not price_usd:
                        skipped += 1
                        continue

                    new_rub = round(price_usd * fx_rate * settings.markup, 2)
                    new_rub = max(new_rub, settings.min_price_rub)

                    if abs(new_rub - current_rub) < 0.01:
                        skipped += 1
                        continue

                    await ggsel_office.update_price(ggsel_id, new_rub)

                    async with AsyncSessionLocal() as db:
                        result = await db.execute(select(Offer).where(Offer.id == offer_id))
                        offer = result.scalar_one_or_none()
                        if offer:
                            offer.price_usd = price_usd
                            offer.price_rub = new_rub
                            await db.commit()

                    print(
                        f"[SyncPrices] updated ggsel_id={ggsel_id} name={name!r} "
                        f"old_rub={current_rub} → new_rub={new_rub} price_usd={price_usd}",
                        flush=True,
                    )
                    updated += 1

                    if i % 20 == 0:
                        print(f"[SyncPrices] progress {i}/{len(offer_snapshot)}", flush=True)

                    await asyncio.sleep(0.2)

                except Exception as e:
                    print(f"[SyncPrices] error ggsel_id={ggsel_id} name={name!r}: {e}", flush=True)
                    errors += 1

        # Batch activate paused offers that now have qty>0
        activated_count = 0
        if to_activate:
            ggsel_id_map = {gid: (oid, n) for oid, gid, n in to_activate}
            all_gids = list(ggsel_id_map.keys())
            for batch_start in range(0, len(all_gids), 100):
                batch = all_gids[batch_start:batch_start + 100]
                try:
                    await ggsel_office.batch_activate(batch)
                    batch_offer_ids = [ggsel_id_map[gid][0] for gid in batch]
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(select(Offer).where(Offer.id.in_(batch_offer_ids)))
                        for offer in result.scalars().all():
                            offer.status = OfferStatus.active
                        await db.commit()
                    for gid in batch:
                        _, n = ggsel_id_map[gid]
                        print(f"[SyncPrices] ACTIVATED ggsel_id={gid} name={n!r} starpets_qty>0", flush=True)
                    activated_count += len(batch)
                except Exception as e:
                    print(f"[SyncPrices] batch_activate error (batch={len(batch)}): {e}", flush=True)

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
    print(f"[FixPostPaymentUrl] starting — {total} offers, url={url}", flush=True)

    updated = errors = 0
    for i, gid in enumerate(offer_ids, 1):
        try:
            await ggsel_office.set_post_payment_url(gid, url)
            updated += 1
        except Exception as e:
            print(f"[FixPostPaymentUrl] ggsel_offer_id={gid} error: {e}", flush=True)
            errors += 1

        if i % 50 == 0:
            print(f"[FixPostPaymentUrl] progress {i}/{total} updated={updated} errors={errors}", flush=True)

        await asyncio.sleep(0.3)

    print(f"[FixPostPaymentUrl] done — updated={updated} errors={errors} total={total}", flush=True)


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
    print(f"[FixWebhooks] starting — {total} offers", flush=True)

    updated = skipped = errors = 0
    secret = settings.webhook_shared_secret
    for i, gid in enumerate(offer_ids, 1):
        try:
            current = await ggsel_office.get_offer(gid)
            precheck_url = (current.get("pre_payment_settings") or {}).get("url") or ""
            notif_url = (current.get("notification_settings") or {}).get("url") or ""
            if secret in precheck_url and secret in notif_url:
                skipped += 1
                continue
        except Exception:
            pass  # GET failed — proceed with PATCH anyway

        try:
            await ggsel_office.patch_offer(
                offer_id=gid,
                precheck_url=f"{settings.public_url}/hooks/ggsel/precheck/{gid}?secret={secret}",
                notification_url=f"{settings.public_url}/hooks/ggsel/notification/{gid}?secret={secret}",
            )
            updated += 1
        except Exception as e:
            print(f"[FixWebhooks] ggsel_offer_id={gid} error: {e}", flush=True)
            errors += 1

        if i % 100 == 0:
            print(f"[FixWebhooks] progress {i}/{total} updated={updated} skipped={skipped} errors={errors}", flush=True)

        await asyncio.sleep(0.3)

    print(f"[FixWebhooks] done — updated={updated} skipped={skipped} errors={errors} total={total}", flush=True)


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


@app.get("/pause-all-offers")
async def pause_all_offers():
    import asyncio
    asyncio.create_task(_run_pause_all_offers())
    return {"started": True}


async def _run_pause_all_offers():
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Offer, OfferStatus

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Offer).where(
                Offer.ggsel_offer_id.isnot(None),
                Offer.status == OfferStatus.active,
            )
        )
        offers = result.scalars().all()
        ggsel_ids = [o.ggsel_offer_id for o in offers]
        offer_ids = [o.id for o in offers]

    print(f"[PauseAllOffers] pausing {len(ggsel_ids)} active offers", flush=True)

    for batch_start in range(0, len(ggsel_ids), 100):
        batch = ggsel_ids[batch_start:batch_start + 100]
        try:
            await ggsel_office.pause_offers(batch)
        except Exception as e:
            print(f"[PauseAllOffers] batch error: {e}", flush=True)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Offer).where(Offer.id.in_(offer_ids)))
        for offer in result.scalars().all():
            offer.status = OfferStatus.paused
        await db.commit()

    print(f"[PauseAllOffers] done — paused {len(ggsel_ids)} offers", flush=True)


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
