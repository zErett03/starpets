import httpx
from datetime import datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, Offer, DeliveryStatus
from app.clients.starpets import starpets


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
        print(
            f"[Deliver] start order_id={order_id} offer_id={offer.id} "
            f"product_id={product_id} roblox_username={roblox_username!r}",
            flush=True,
        )

        # 1. Get cheapest available item for this product
        async with httpx.AsyncClient(timeout=15) as http:
            top_item = await starpets.get_top_item(http, product_id)
        if not top_item:
            raise RuntimeError(f"No items available for product_id={product_id}")
        item_id = str(top_item["id"])
        price_usd = float(top_item.get("price_usd") or 0)
        print(f"[Deliver] top_item id={item_id} price_usd={price_usd}", flush=True)

        # 2. Buy the item at its current price
        buy_resp = await starpets.buy_by_items([{"id": item_id, "price": price_usd}])
        purchased = buy_resp.get("items") or []
        if not purchased:
            raise RuntimeError(f"Buy returned no items: {buy_resp}")
        purchased_item_id = str(purchased[0]["id"])
        exec_price = float(purchased[0].get("price_usd") or price_usd)
        print(f"[Deliver] bought purchased_item_id={purchased_item_id} exec_price={exec_price}", flush=True)

        # 3. Create withdrawal trade
        trade_resp = await starpets.create_trade(
            purchased_item_ids=[purchased_item_id],
            roblox_username=roblox_username,
        )
        trade_id = (
            trade_resp.get("tradeId")
            or trade_resp.get("trade_id")
            or trade_resp.get("id")
            or (trade_resp.get("data") or {}).get("id")
        )
        if not trade_id:
            raise RuntimeError(f"create_trade returned no trade_id: {trade_resp}")
        print(f"[Deliver] trade created trade_id={trade_id} resp={trade_resp}", flush=True)

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
        order.starpets_purchase_id = purchased_item_id
        order.starpets_custom_id = str(trade_id)
        order.exec_price_usd = exec_price
        order.max_price_usd = price_usd
        order.delivery_status = DeliveryStatus.dispatched
        order.updated_at = datetime.utcnow()
        await db.commit()

        print(
            f"[Deliver] done order_id={order_id} trade_id={trade_id} status=dispatched",
            flush=True,
        )
