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
    print(f"[precheck] body: {body}", flush=True)

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
            if opt.get("type") == "text":
                roblox_username = opt.get("value", "").strip()
                break

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

        precheck_ext_id = f"precheck-{offer_id}"
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
    print(f"[notification] body: {body}", flush=True)

    id_i = body.get("id_i")
    amount = body.get("amount")
    email = body.get("email")
    ip = body.get("ip")
    date_str = body.get("date")

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

        precheck_result = await db.execute(
            select(WebhookEvent).where(
                WebhookEvent.kind == WebhookKind.precheck,
                WebhookEvent.external_id == f"precheck-{offer_id}",
            )
        )
        precheck_event = precheck_result.scalar_one_or_none()
        precheck_payload = precheck_event.payload if precheck_event else {}
        roblox_username = precheck_payload.get("roblox_username", "")

        paid_at = datetime.fromisoformat(date_str) if date_str else datetime.utcnow()
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

        db.add(Task(kind=TaskKind.DELIVER, priority=1, payload={"order_id": order.id}))
        db.add(WebhookEvent(
            kind=WebhookKind.notification,
            external_id=str(id_i),
            payload=body,
            response_code=200,
        ))

        await db.commit()

    return {"status": "ok"}
