import hmac
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import Offer, Order, Task, WebhookEvent, TaskKind, DeliveryStatus, WebhookKind

router = APIRouter()


def check_secret(secret: str):
    if not hmac.compare_digest(secret, settings.webhook_shared_secret):
        raise HTTPException(status_code=403, detail="Invalid secret")


@router.post("/hooks/ggsel/precheck/{offer_id}")
async def precheck(offer_id: int, request: Request, secret: str = ""):
    check_secret(secret)
    body = await request.json()
    print(f"[precheck] offer_id={offer_id} body: {body}", flush=True)

    product = body.get("product", {})
    options = body.get("options", [])
    id_i = body.get("id_i")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Offer).where(Offer.ggsel_offer_id == offer_id))
        offer = result.scalar_one_or_none()

        if not offer:
            return {"error": "Оффер не найден"}

        if product.get("cnt") != 1:
            return {"error": "Количество должно быть 1"}

        roblox_username = None
        for opt in options:
            val = (opt.get("value") or "").strip()
            if val:
                roblox_username = val
                break

        print(f"[precheck] offer_id={offer_id} id_i={id_i} roblox_username={roblox_username!r}", flush=True)

        if not roblox_username:
            return {"error": "Укажите Roblox Username"}

        if offer.starpets_qty == 0:
            return {"error": "Товар временно недоступен"}

        if id_i is not None:
            result = await db.execute(select(Order).where(Order.ggsel_order_id == id_i))
            order = result.scalar_one_or_none()

            if order:
                order.roblox_username = roblox_username
            else:
                order = Order(
                    ggsel_order_id=id_i,
                    offer_id=offer.id,
                    item_name=offer.name,
                    roblox_username=roblox_username,
                    starpets_custom_id=str(id_i),
                )
                db.add(order)

        precheck_ext_id = f"precheck-{offer_id}-{id_i}" if id_i is not None else f"precheck-{offer_id}"
        result = await db.execute(
            select(WebhookEvent).where(WebhookEvent.external_id == precheck_ext_id)
        )
        existing_event = result.scalar_one_or_none()

        if existing_event:
            existing_event.payload = {"roblox_username": roblox_username}
            existing_event.processed_at = datetime.utcnow()
        else:
            db.add(WebhookEvent(
                kind=WebhookKind.precheck,
                external_id=precheck_ext_id,
                payload={"roblox_username": roblox_username},
                response_code=200,
            ))

        await db.commit()

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
            precheck_order_result = await db.execute(
                select(Order).where(
                    Order.offer_id == offer.id,
                    Order.paid_at.is_(None),
                    Order.delivery_status == DeliveryStatus.pending,
                ).order_by(Order.created_at.desc())
            )
            precheck_order = precheck_order_result.scalars().first()
            print(
                f"[notification] lookup precheck order by offer_id={offer.id}: "
                f"{'found id=' + str(precheck_order.id) + ' ggsel_order_id=' + str(precheck_order.ggsel_order_id) + ' username=' + repr(precheck_order.roblox_username) if precheck_order else 'not found'}",
                flush=True,
            )

        # Resolve roblox_username: order → notification options → precheck WebhookEvent
        order_for_username = existing_order or precheck_order
        roblox_username = (order_for_username.roblox_username if order_for_username else None) or ""
        print(f"[notification] roblox_username from order: {roblox_username!r}", flush=True)

        if not roblox_username:
            for opt in options:
                val = (opt.get("value") or "").strip()
                if val:
                    roblox_username = val
                    break
            print(f"[notification] roblox_username from notification options: {roblox_username!r}", flush=True)

        if not roblox_username:
            # Try exact precheck event keys first, then any precheck event for this offer
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

        if not roblox_username:
            # Last resort: any precheck event for this offer_id (handles mismatched id_i)
            any_precheck_result = await db.execute(
                select(WebhookEvent).where(
                    WebhookEvent.kind == WebhookKind.precheck,
                    WebhookEvent.external_id.like(f"precheck-{offer_id}%"),
                ).order_by(WebhookEvent.processed_at.desc())
            )
            any_precheck = any_precheck_result.scalars().first()
            if any_precheck:
                roblox_username = (any_precheck.payload or {}).get("roblox_username", "")
                print(f"[notification] roblox_username from any precheck event {any_precheck.external_id!r}: {roblox_username!r}", flush=True)

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
