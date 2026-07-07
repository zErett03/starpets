import hmac
import httpx
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import Offer, Order, Task, WebhookEvent, TaskKind, DeliveryStatus, WebhookKind, SkuVariant
from app.clients.starpets import starpets

router = APIRouter()


def check_secret(secret: str):
    if not hmac.compare_digest(secret, settings.webhook_shared_secret):
        raise HTTPException(status_code=403, detail="Invalid secret")


async def _resolve_sku_product_id(db, ggsel_offer_id: int, options: list) -> int | None:
    """SKU-master: if this card is a SKU base card, resolve the selected radio variant
    (its value == our ggsel_variant_id) to the target StarPets product_id."""
    rows = (await db.execute(
        select(SkuVariant.ggsel_variant_id, SkuVariant.starpets_product_id)
        .where(SkuVariant.ggsel_offer_id == ggsel_offer_id)
    )).all()
    if not rows:
        return None
    mapping = {int(vid): pid for vid, pid in rows}
    for opt in options or []:
        try:
            v = int(opt.get("value"))
        except (TypeError, ValueError):
            continue
        if v in mapping:
            return mapping[v]
    return None


@router.post("/hooks/ggsel/precheck/{ggsel_offer_id}")
async def precheck(ggsel_offer_id: int, request: Request, secret: str = ""):
    check_secret(secret)
    body = await request.json()
    print(f"[precheck] ggsel_offer_id={ggsel_offer_id} body: {body}", flush=True)

    product = body.get("product", {})
    options = body.get("options", [])
    id_i = body.get("id_i")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Offer).where(Offer.ggsel_offer_id == ggsel_offer_id))
        offer = result.scalar_one_or_none()

        if not offer:
            return {"error": "Оффер не найден"}

        # offer.id is the internal PK; ggsel_offer_id is the external ggsel ID
        internal_offer_id = offer.id
        print(
            f"[precheck] ggsel_offer_id={ggsel_offer_id} → internal offer.id={internal_offer_id}",
            flush=True,
        )

        if product.get("cnt") != 1:
            return {"error": "Количество должно быть 1"}

        roblox_username = None
        for opt in options:
            # Only the free-text field carries the Roblox username; skip checkbox/radio
            # consent options (they send an int variant-id as "value").
            if opt.get("type") != "text":
                continue
            val = str(opt.get("value") or "").strip()
            if val:
                roblox_username = val
                break

        print(f"[precheck] id_i={id_i} roblox_username={roblox_username!r}", flush=True)

        if not roblox_username:
            return {"error": "Укажите Roblox Username"}

        sku_product_id = await _resolve_sku_product_id(db, ggsel_offer_id, options)
        _sku_price_rub = None
        if sku_product_id is not None:
            print(f"[precheck] SKU variant → product_id={sku_product_id}", flush=True)
            _sv = (await db.execute(
                select(SkuVariant.price_rub).where(
                    SkuVariant.ggsel_offer_id == ggsel_offer_id,
                    SkuVariant.starpets_product_id == sku_product_id,
                )
            )).scalar_one_or_none()
            if _sv is not None:
                _sku_price_rub = float(_sv)

        if id_i is not None:
            result = await db.execute(select(Order).where(Order.ggsel_order_id == id_i))
            order = result.scalar_one_or_none()

            if order:
                order.roblox_username = roblox_username
                if order.offer_id != internal_offer_id:
                    print(
                        f"[precheck] fixing order.offer_id: {order.offer_id} → {internal_offer_id}",
                        flush=True,
                    )
                    order.offer_id = internal_offer_id
            else:
                order = Order(
                    ggsel_order_id=id_i,
                    offer_id=internal_offer_id,
                    item_name=offer.name,
                    roblox_username=roblox_username,
                    starpets_custom_id=str(id_i),
                )
                db.add(order)
            if sku_product_id is not None:
                order.sku_product_id = sku_product_id
            print(
                f"[precheck] order saved: ggsel_order_id={id_i} offer_id={internal_offer_id} "
                f"roblox_username={roblox_username!r}",
                flush=True,
            )

        # Save WebhookEvent BEFORE qty check so username is always persisted
        precheck_ext_id = f"precheck-{ggsel_offer_id}-{id_i}" if id_i is not None else f"precheck-{ggsel_offer_id}"
        result = await db.execute(
            select(WebhookEvent).where(WebhookEvent.external_id == precheck_ext_id)
        )
        existing_event = result.scalar_one_or_none()

        _pc_payload = {"roblox_username": roblox_username, "sku_product_id": sku_product_id}
        if existing_event:
            existing_event.payload = _pc_payload
            existing_event.processed_at = datetime.utcnow()
        else:
            db.add(WebhookEvent(
                kind=WebhookKind.precheck,
                external_id=precheck_ext_id,
                payload=_pc_payload,
                response_code=200,
            ))

        await db.commit()
        print(f"[precheck] WebhookEvent saved: {precheck_ext_id!r} username={roblox_username!r}", flush=True)

        # Capture before session closes. For SKU cards the product/price come from the
        # SELECTED variant (resolved above), not the multi-product backing offer row.
        _product_id = sku_product_id or offer.starpets_product_id
        _offer_name = offer.name
        _starpets_qty = offer.starpets_qty
        _offer_price_rub = _sku_price_rub if _sku_price_rub is not None else float(offer.price_rub or 0)

    # Live availability check — don't rely on stale starpets_qty from DB (updated every 30 min)
    if _product_id:
        async with httpx.AsyncClient(timeout=10) as _http:
            _top = await starpets.get_top_item(_http, str(_product_id))
        if _top is None:
            print(f"[precheck] live check: no items for product_id={_product_id} offer={_offer_name!r}", flush=True)
            return {"error": "Товар временно недоступен"}
        # Profitability guard — block the sale if the live floor spiked above our (stale)
        # offer price, so the buyer never pays for an order we'd fulfil at a loss.
        from app.fx import get_usd_rub, item_cost_ok
        _fx = await get_usd_rub()
        _ok, _cost = item_cost_ok(float(_top.get("price_usd") or 0), _fx, _offer_price_rub, settings.max_cost_ratio)
        if not _ok:
            print(
                f"[precheck] BLOCKED unprofitable id_i={id_i} product_id={_product_id} "
                f"cost_rub={_cost:.2f} > {settings.max_cost_ratio}×{_offer_price_rub} offer={_offer_name!r}",
                flush=True,
            )
            return {"error": "Товар временно недоступен — идёт обновление цены, попробуйте позже"}
    elif _starpets_qty == 0:
        return {"error": "Товар временно недоступен"}

    return {"error": None}


@router.post("/hooks/ggsel/notification/{offer_id}")
async def notification(offer_id: int, request: Request, secret: str = ""):
    check_secret(secret)
    body = await request.json()
    print(f"[notification] offer_id={offer_id} body: {body}", flush=True)

    id_i = body.get("id_i")
    amount = body.get("amount")
    email = body.get("email")
    ip = body.get("ip")
    date_str = body.get("date")
    options = body.get("options", [])

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(WebhookEvent).where(
                WebhookEvent.kind == WebhookKind.notification,
                WebhookEvent.external_id == str(id_i),
            )
        )
        if existing.scalar_one_or_none():
            return {"status": "already processed"}

        result = await db.execute(select(Offer).where(Offer.ggsel_offer_id == offer_id))
        offer = result.scalar_one_or_none()
        if not offer:
            raise HTTPException(status_code=422, detail="Offer not found")

        # 1. Look up order by ggsel_order_id (exact match)
        existing_order_result = await db.execute(
            select(Order).where(Order.ggsel_order_id == id_i)
        )
        existing_order = existing_order_result.scalar_one_or_none()
        print(
            f"[notification] lookup by ggsel_order_id={id_i}: "
            f"{'found id=' + str(existing_order.id) + ' username=' + repr(existing_order.roblox_username) if existing_order else 'not found'}",
            flush=True,
        )

        # 2. If not found by order_id, look for a precheck-created order by offer_id
        #    (ggsel may send precheck and notification with different id_i values)
        precheck_order = None
        if not existing_order:
            # Diagnostic: dump all orders for this offer_id so we can see what's there
            all_orders_result = await db.execute(
                select(Order).where(Order.offer_id == offer.id).order_by(Order.created_at.desc())
            )
            all_orders = all_orders_result.scalars().all()
            print(
                f"[notification] all orders for offer_id={offer.id} (ggsel_offer_id={offer_id}): "
                f"count={len(all_orders)}",
                flush=True,
            )
            for o in all_orders:
                print(
                    f"[notification]   order id={o.id} ggsel_order_id={o.ggsel_order_id} "
                    f"status={o.delivery_status.value if o.delivery_status else None} "
                    f"paid_at={o.paid_at} amount_rub={o.amount_rub} "
                    f"roblox_username={o.roblox_username!r}",
                    flush=True,
                )

            # Search: precheck orders have no amount_rub (payment info comes only from notification)
            precheck_order_result = await db.execute(
                select(Order).where(
                    Order.offer_id == offer.id,
                    Order.amount_rub.is_(None),
                ).order_by(Order.created_at.desc())
            )
            precheck_order = precheck_order_result.scalars().first()
            print(
                f"[notification] precheck order search (offer_id={offer.id}, amount_rub IS NULL): "
                f"{'found id=' + str(precheck_order.id) + ' ggsel_order_id=' + str(precheck_order.ggsel_order_id) + ' status=' + str(precheck_order.delivery_status.value if precheck_order.delivery_status else None) + ' username=' + repr(precheck_order.roblox_username) if precheck_order else 'not found'}",
                flush=True,
            )

        # Resolve roblox_username: order → notification options → precheck WebhookEvent
        order_for_username = existing_order or precheck_order
        roblox_username = (order_for_username.roblox_username if order_for_username else None) or ""
        print(f"[notification] roblox_username from order: {roblox_username!r}", flush=True)

        if not roblox_username:
            for opt in options:
                if opt.get("type") != "text":
                    continue  # skip checkbox/radio consent options
                val = str(opt.get("value") or "").strip()
                if val:
                    roblox_username = val
                    break
            print(f"[notification] roblox_username from notification options: {roblox_username!r}", flush=True)

        if not roblox_username:
            # Diagnostic: count all precheck WebhookEvents for this ggsel_offer_id
            from sqlalchemy import func as _func
            diag_result = await db.execute(
                select(WebhookEvent.external_id, WebhookEvent.processed_at, WebhookEvent.payload).where(
                    WebhookEvent.kind == WebhookKind.precheck,
                    WebhookEvent.external_id.like(f"precheck-{offer_id}%"),
                ).order_by(WebhookEvent.processed_at.desc())
            )
            diag_rows = diag_result.all()
            print(
                f"[notification] WebhookEvent precheck count for offer_id={offer_id}: {len(diag_rows)}",
                flush=True,
            )
            for row in diag_rows:
                print(
                    f"[notification]   event external_id={row.external_id!r} "
                    f"processed_at={row.processed_at} payload={row.payload}",
                    flush=True,
                )

            # Try exact keys first: precheck-{offer_id}-{id_i}, then precheck-{offer_id}
            for ext_id in (f"precheck-{offer_id}-{id_i}", f"precheck-{offer_id}"):
                precheck_result = await db.execute(
                    select(WebhookEvent).where(
                        WebhookEvent.kind == WebhookKind.precheck,
                        WebhookEvent.external_id == ext_id,
                    )
                )
                precheck_event = precheck_result.scalar_one_or_none()
                if precheck_event:
                    roblox_username = (precheck_event.payload or {}).get("roblox_username", "")
                    print(f"[notification] roblox_username from precheck event {ext_id!r}: {roblox_username!r}", flush=True)
                    break

            if not roblox_username and diag_rows:
                # Last resort: any precheck event for this offer_id (handles mismatched id_i)
                any_precheck_payload = diag_rows[0].payload or {}
                roblox_username = any_precheck_payload.get("roblox_username", "")
                print(
                    f"[notification] roblox_username from latest precheck event "
                    f"{diag_rows[0].external_id!r}: {roblox_username!r}",
                    flush=True,
                )

        print(f"[notification] final roblox_username={roblox_username!r} id_i={id_i}", flush=True)

        paid_at = datetime.fromisoformat(date_str) if date_str else datetime.utcnow()

        if existing_order:
            existing_order.amount_rub = amount
            existing_order.buyer_email = email
            existing_order.buyer_ip = ip
            existing_order.paid_at = paid_at
            existing_order.delivery_status = DeliveryStatus.pending
            if roblox_username:
                existing_order.roblox_username = roblox_username
            order = existing_order
        elif precheck_order:
            # Link precheck order to this notification (update to real ggsel_order_id)
            print(
                f"[notification] linking precheck order id={precheck_order.id} "
                f"old_ggsel_order_id={precheck_order.ggsel_order_id} → new={id_i} "
                f"roblox_username={roblox_username!r}",
                flush=True,
            )
            precheck_order.ggsel_order_id = id_i
            precheck_order.starpets_custom_id = str(id_i)
            precheck_order.amount_rub = amount
            precheck_order.buyer_email = email
            precheck_order.buyer_ip = ip
            precheck_order.paid_at = paid_at
            precheck_order.delivery_status = DeliveryStatus.pending
            if roblox_username:
                precheck_order.roblox_username = roblox_username
            order = precheck_order
        else:
            order = Order(
                ggsel_order_id=id_i,
                offer_id=offer.id,
                item_name=offer.name,
                amount_rub=amount,
                roblox_username=roblox_username,
                buyer_email=email,
                buyer_ip=ip,
                starpets_custom_id=str(id_i),
                delivery_status=DeliveryStatus.pending,
                paid_at=paid_at,
            )
            db.add(order)

        # SKU-master variant resolution. IMPORTANT: never overwrite the product that was
        # resolved for THIS order at precheck time — that is the buyer's actual selection.
        # Only when it is missing do we fall back, and ONLY to the strictly id_i-scoped
        # precheck event. The shared `precheck-{offer_id}` key is clobbered by every variant
        # attempt on the card, so using it can cross-wire a paid order to the wrong (often
        # out-of-stock) variant — exactly the "paid but no item" failure mode.
        if order.sku_product_id is None:
            _sku_pid = await _resolve_sku_product_id(db, offer_id, options)  # notif has no options
            if _sku_pid is None and id_i is not None:
                _ev = (await db.execute(select(WebhookEvent).where(
                    WebhookEvent.kind == WebhookKind.precheck,
                    WebhookEvent.external_id == f"precheck-{offer_id}-{id_i}",
                ))).scalar_one_or_none()
                if _ev and (_ev.payload or {}).get("sku_product_id") is not None:
                    _sku_pid = _ev.payload["sku_product_id"]
            if _sku_pid is not None:
                order.sku_product_id = _sku_pid
                print(f"[notification] SKU variant → product_id={_sku_pid}", flush=True)
        else:
            print(f"[notification] SKU variant kept from precheck → product_id={order.sku_product_id}", flush=True)

        await db.flush()

        db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=3, payload={"order_id": order.id}))
        db.add(WebhookEvent(
            kind=WebhookKind.notification,
            external_id=str(id_i),
            payload=body,
            response_code=200,
        ))

        await db.commit()

    return {"status": "ok"}
