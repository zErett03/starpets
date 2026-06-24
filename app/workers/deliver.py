import httpx
from datetime import datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, Offer, DeliveryStatus
from app.clients.starpets import starpets
from app.config import settings
from app.fx import get_usd_rub

_BUY_MAX_RETRIES = 3


async def _buy_with_retry(
    item_id: str,
    price_usd: float,
    offer_price_rub: float,
) -> tuple[str, float]:
    """Buy item, retrying up to 3x on code=330 (PRICES_HAVE_CHANGED).

    Returns (purchased_item_id, exec_price_usd).
    Raises RuntimeError('price_too_high') if the updated price would exceed
    the ggsel offer price after applying markup and FX conversion.
    """
    current_id = item_id
    current_price = price_usd

    for attempt in range(1, _BUY_MAX_RETRIES + 1):
        try:
            buy_resp = await starpets.buy_by_items([{"id": current_id, "price": current_price}])
            purchased = buy_resp.get("items") or []
            if not purchased:
                raise RuntimeError(f"Buy returned no items: {buy_resp}")
            purchased_item_id = str(purchased[0]["id"])
            exec_price = float(purchased[0].get("price_usd") or current_price)
            return purchased_item_id, exec_price

        except httpx.HTTPStatusError as exc:
            try:
                err_body = exc.response.json()
            except Exception:
                raise exc

            if err_body.get("code") != 330:
                raise exc

            resp_items = err_body.get("items") or []
            if not resp_items:
                raise exc

            new_price_usd = float(
                resp_items[0].get("price_usd") or resp_items[0].get("price") or 0
            )
            if not new_price_usd:
                raise exc

            fx_rate = await get_usd_rub()
            new_price_rub = new_price_usd * settings.markup * fx_rate

            print(
                f"[Buy] code=330 PRICES_HAVE_CHANGED attempt={attempt}/{_BUY_MAX_RETRIES} "
                f"item_id={current_id} old_usd={current_price} new_usd={new_price_usd} "
                f"new_rub={new_price_rub:.2f} offer_rub={offer_price_rub}",
                flush=True,
            )

            if new_price_rub > offer_price_rub:
                print(
                    f"[Buy] price_too_high: {new_price_rub:.2f} > {offer_price_rub} — aborting",
                    flush=True,
                )
                raise RuntimeError("price_too_high")

            if attempt == _BUY_MAX_RETRIES:
                raise RuntimeError(
                    f"code=330 after {_BUY_MAX_RETRIES} retries, last_price_usd={new_price_usd}"
                )

            current_price = new_price_usd
            new_id = str(resp_items[0].get("id") or "")
            if new_id:
                current_id = new_id


async def deliver_order(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        result2 = await db.execute(select(Offer).where(Offer.id == order.offer_id))
        offer = result2.scalar_one_or_none()
        if not offer:
            raise RuntimeError(f"Offer {order.offer_id} not found for order {order_id}")

        product_id = str(offer.starpets_product_id or "")
        if not product_id:
            raise RuntimeError(f"Offer {offer.id} has no starpets_product_id")

        roblox_username = order.roblox_username or ""
        offer_price_rub = float(offer.price_rub or 0)
        print(
            f"[Deliver] start order_id={order_id} offer_id={offer.id} "
            f"product_id={product_id} roblox_username={roblox_username!r}",
            flush=True,
        )

        # Already fully dispatched — nothing to do on retry
        if order.delivery_status in (DeliveryStatus.dispatched, DeliveryStatus.done, DeliveryStatus.finalized):
            print(f"[Deliver] order_id={order_id} already {order.delivery_status.value} — skip", flush=True)
            return

        # Guard: must have roblox_username before spending any money
        if not roblox_username:
            order.delivery_status = DeliveryStatus.failed
            order.error_reason = "no_roblox_username"
            order.updated_at = datetime.utcnow()
            await db.commit()
            print(f"[Deliver] order_id={order_id} failed: no_roblox_username", flush=True)
            return

        # Reuse existing purchased_item_id if item was already bought (retry after partial failure)
        if order.starpets_purchase_id:
            purchased_item_id = order.starpets_purchase_id
            exec_price = float(order.exec_price_usd or 0)
            price_usd = float(order.max_price_usd or 0)
            print(
                f"[Deliver] order_id={order_id} reusing purchased_item_id={purchased_item_id} — skip buy",
                flush=True,
            )
        else:
            # 1. Get cheapest available item for this product
            async with httpx.AsyncClient(timeout=15) as http:
                top_item = await starpets.get_top_item(http, product_id)
            if not top_item:
                raise RuntimeError(f"No items available for product_id={product_id}")
            item_id = str(top_item["id"])
            price_usd = float(top_item.get("price_usd") or 0)
            print(f"[Deliver] top_item id={item_id} price_usd={price_usd}", flush=True)

            # 2. Buy the item (with retry on code=330 PRICES_HAVE_CHANGED)
            try:
                purchased_item_id, exec_price = await _buy_with_retry(
                    item_id, price_usd, offer_price_rub
                )
            except RuntimeError as e:
                if str(e) == "price_too_high":
                    order.delivery_status = DeliveryStatus.failed
                    order.error_reason = "price_too_high"
                    order.updated_at = datetime.utcnow()
                    await db.commit()
                    print(
                        f"[Deliver] order_id={order_id} marked failed: price_too_high "
                        f"item_id={item_id} price_usd={price_usd} offer_rub={offer_price_rub}",
                        flush=True,
                    )
                    return
                raise

            print(f"[Deliver] bought purchased_item_id={purchased_item_id} exec_price={exec_price}", flush=True)

            # Persist purchase immediately so a retry won't double-buy
            order.starpets_purchase_id = purchased_item_id
            order.exec_price_usd = exec_price
            order.max_price_usd = price_usd
            order.updated_at = datetime.utcnow()
            await db.commit()

        # 3. Create withdrawal trade
        print(
            f"[Deliver] create_trade order_id={order_id} "
            f"purchased_item_id={purchased_item_id} roblox_username={roblox_username!r}",
            flush=True,
        )
        try:
            trade_resp = await starpets.create_trade(
                purchased_item_ids=[purchased_item_id],
                roblox_username=roblox_username,
            )
        except httpx.HTTPStatusError as exc:
            err_body = ""
            try:
                err_body = exc.response.text
            except Exception:
                pass
            print(
                f"[Deliver] create_trade FAILED order_id={order_id} "
                f"status={exc.response.status_code} body={err_body}",
                flush=True,
            )
            raise
        print(f"[Deliver] create_trade response order_id={order_id}: {trade_resp}", flush=True)

        trade_id = (
            trade_resp.get("tradeId")
            or trade_resp.get("trade_id")
            or trade_resp.get("id")
            or (trade_resp.get("data") or {}).get("id")
        )
        if not trade_id:
            raise RuntimeError(f"create_trade returned no trade_id: {trade_resp}")

        linked = trade_resp.get("linkedRobloxAccount") or (trade_resp.get("data") or {}).get("linkedRobloxAccount") or {}
        bot_name = linked.get("robloxAccountName") or linked.get("username") or linked.get("name")
        print(f"[Deliver] trade_id={trade_id} bot_name={bot_name!r}", flush=True)

        # 4. Send friendship request so buyer can add bot
        try:
            friendship_resp = await starpets.send_friendship(
                trade_id=int(trade_id),
                item_id=purchased_item_id,
                username=roblox_username,
            )
            print(f"[Deliver] friendship sent resp={friendship_resp}", flush=True)
        except Exception as e:
            # Non-fatal — trade exists, buyer can still add bot manually
            print(f"[Deliver] friendship request failed (non-fatal): {e}", flush=True)

        # 5. Persist results
        order.starpets_custom_id = str(trade_id)
        order.bot_name = bot_name
        order.delivery_status = DeliveryStatus.dispatched
        order.updated_at = datetime.utcnow()
        await db.commit()

        print(
            f"[Deliver] done order_id={order_id} trade_id={trade_id} status=dispatched",
            flush=True,
        )
