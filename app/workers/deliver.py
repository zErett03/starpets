import httpx
from datetime import datetime

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, Offer, DeliveryStatus
from app.clients.starpets import starpets
from app.config import settings
from app.fx import get_usd_rub, item_cost_ok

_BUY_MAX_RETRIES = 3


class TransientDeliveryError(Exception):
    """A recoverable delivery failure (no stock / price spike). Raised so the task runner
    retries with backoff; StarPets stock and prices flicker, so a paid order should not be
    failed permanently on the first miss."""


async def _transient_fail(db, order, order_id: int, reason: str, msg: str,
                          attempt: int, max_attempts: int) -> None:
    """Escalate to needs_attention on the LAST attempt, otherwise raise to retry.
    Keeps the order recoverable (nothing bought yet) across the retry window."""
    order.updated_at = datetime.utcnow()
    if attempt >= max_attempts:
        order.delivery_status = DeliveryStatus.needs_attention
        order.error_reason = reason
        await db.commit()
        print(f"[Deliver] order_id={order_id} needs_attention after {attempt} attempts: "
              f"{reason} ({msg})", flush=True)
        return
    order.delivery_status = DeliveryStatus.pending
    order.error_reason = f"retrying:{reason}"
    await db.commit()
    print(f"[Deliver] order_id={order_id} transient {reason} attempt {attempt}/{max_attempts} "
          f"— will retry ({msg})", flush=True)
    raise TransientDeliveryError(f"{reason}: {msg}")


async def _buy_with_retry(
    item_id: str,
    price_usd: float,
    sale_price_rub: float,
    force: bool = False,
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

            # code=330 returns items as a dict {item_id: new_price_usd} (per API docs),
            # NOT a list of objects. Parse both shapes defensively.
            items_map = err_body.get("items")
            new_price_usd = None
            new_id = current_id
            if isinstance(items_map, dict) and items_map:
                key = str(current_id) if str(current_id) in items_map else next(iter(items_map))
                new_id = str(key)
                try:
                    new_price_usd = float(items_map[key] or 0)
                except (TypeError, ValueError):
                    new_price_usd = None
            elif isinstance(items_map, list) and items_map:
                first = items_map[0]
                new_id = str(first.get("id") or current_id)
                new_price_usd = float(first.get("price_usd") or first.get("price") or 0)
            if not new_price_usd:
                raise exc

            fx_rate = await get_usd_rub()
            ok, new_cost_rub = item_cost_ok(new_price_usd, fx_rate, sale_price_rub, settings.max_cost_ratio)

            print(
                f"[Buy] code=330 PRICES_HAVE_CHANGED attempt={attempt}/{_BUY_MAX_RETRIES} "
                f"item_id={current_id} old_usd={current_price} new_usd={new_price_usd} "
                f"new_cost_rub={new_cost_rub:.2f} sale_rub={sale_price_rub} ratio={settings.max_cost_ratio}",
                flush=True,
            )

            if not ok and not force:
                print(
                    f"[Buy] price_too_high: cost {new_cost_rub:.2f} > "
                    f"{settings.max_cost_ratio}×{sale_price_rub} — aborting",
                    flush=True,
                )
                raise RuntimeError("price_too_high")

            if attempt == _BUY_MAX_RETRIES:
                raise RuntimeError(
                    f"code=330 after {_BUY_MAX_RETRIES} retries, last_price_usd={new_price_usd}"
                )

            current_price = new_price_usd
            current_id = new_id


async def deliver_order(order_id: int, attempt: int = 1, max_attempts: int = 1) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            raise ValueError(f"Order {order_id} not found")

        result2 = await db.execute(select(Offer).where(Offer.id == order.offer_id))
        offer = result2.scalar_one_or_none()
        if not offer:
            raise RuntimeError(f"Offer {order.offer_id} not found for order {order_id}")

        # SKU-master: a resolved variant product takes precedence over the base offer product
        product_id = str(order.sku_product_id or offer.starpets_product_id or "")
        if not product_id and order.roblox_username and offer.ggsel_offer_id:
            # Self-heal: a SKU order whose notification didn't persist the variant. Recover it
            # from the precheck event (username-scoped first, then the shared offer key with a
            # username check) and persist, so we deliver the buyer's actual selection.
            from app.db.models import WebhookEvent, WebhookKind
            _u = order.roblox_username.strip().lower()
            _shared = f"precheck-{offer.ggsel_offer_id}"
            for _ext in (f"{_shared}-sku-{_u}", _shared):
                _ev = (await db.execute(select(WebhookEvent).where(
                    WebhookEvent.kind == WebhookKind.precheck,
                    WebhookEvent.external_id == _ext,
                ))).scalar_one_or_none()
                if not _ev or (_ev.payload or {}).get("sku_product_id") is None:
                    continue
                _ev_u = (_ev.payload.get("roblox_username") or "").strip().lower()
                if _ext == _shared and _ev_u and _ev_u != _u:
                    continue
                order.sku_product_id = _ev.payload["sku_product_id"]
                product_id = str(order.sku_product_id)
                await db.commit()
                print(f"[Deliver] order_id={order_id} self-healed sku_product_id={product_id} "
                      f"from {_ext}", flush=True)
                break
        if not product_id:
            raise RuntimeError(f"Offer {offer.id} has no starpets_product_id")

        roblox_username = order.roblox_username or ""
        offer_price_rub = float(offer.price_rub or 0)
        # what we actually receive for this order (buyer's payment; fallback to listed price)
        sale_rub = float(order.amount_rub or offer.price_rub or 0)
        print(
            f"[Deliver] start order_id={order_id} offer_id={offer.id} "
            f"product_id={product_id} roblox_username={roblox_username!r}",
            flush=True,
        )

        # Already fully dispatched — nothing to do on retry
        if order.delivery_status in (DeliveryStatus.dispatched, DeliveryStatus.done, DeliveryStatus.finalized):
            print(f"[Deliver] order_id={order_id} already {order.delivery_status.value} — skip", flush=True)
            return

        # 1-hour item lifetime: if we already bought the item and >1h has passed, StarPets has
        # refunded it — the purchase is dead. Don't keep retrying; flag for manual handling.
        from app.deadline import item_expired
        if order.starpets_purchase_id and item_expired(order):
            order.delivery_status = DeliveryStatus.needs_attention
            order.error_reason = ("предмет протух >1ч: StarPets вернул деньги — доставка невозможна. "
                                  "Перекупить свежий или вернуть покупателю.")
            order.updated_at = datetime.utcnow()
            await db.commit()
            print(f"[Deliver] order_id={order_id} item >1h expired — needs_attention (no retry)", flush=True)
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
                await _transient_fail(
                    db, order, order_id, "no_items_available",
                    f"product_id={product_id}", attempt, max_attempts,
                )
                return
            item_id = str(top_item["id"])
            price_usd = float(top_item.get("price_usd") or 0)
            print(f"[Deliver] top_item id={item_id} price_usd={price_usd}", flush=True)

            # 1b. Profitability guard — never buy at a loss. The offer price is only
            # refreshed every ~30 min, so between syncs the live floor can spike above it.
            fx_rate = await get_usd_rub()
            ok, cost_rub = item_cost_ok(price_usd, fx_rate, sale_rub, settings.max_cost_ratio)
            if not ok and not order.force_deliver:
                await _transient_fail(
                    db, order, order_id, "price_too_high",
                    f"cost_rub={cost_rub:.2f} > {settings.max_cost_ratio}×{sale_rub} "
                    f"price_usd={price_usd} fx={fx_rate}", attempt, max_attempts,
                )
                return

            # 2. Buy the item (with retry on code=330 PRICES_HAVE_CHANGED)
            try:
                purchased_item_id, exec_price = await _buy_with_retry(
                    item_id, price_usd, sale_rub, force=order.force_deliver
                )
            except RuntimeError as e:
                if str(e) == "price_too_high":
                    await _transient_fail(
                        db, order, order_id, "price_too_high",
                        f"item_id={item_id} price_usd={price_usd} offer_rub={offer_price_rub}",
                        attempt, max_attempts,
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
            _code = None
            try:
                err_body = exc.response.text
                _code = (exc.response.json() or {}).get("code")
            except Exception:
                pass
            try:
                _code = int(_code)
            except (ValueError, TypeError):
                _code = None
            print(
                f"[Deliver] create_trade FAILED order_id={order_id} "
                f"status={exc.response.status_code} code={_code} body={err_body}",
                flush=True,
            )
            if _code == 210:
                # 210 NO_ACCESS on the FIRST create_trade (the item was just bought, so it CANNOT
                # be "locked in a trade"): StarPets now validates the RECIPIENT up front and rejects
                # when the buyer's Roblox account can't receive a trade (closed trade privacy /
                # under-13 / trading disabled). Retrying or re-buying does NOT help — flag for the
                # operator to fix the buyer login/privacy or refund. (The item-locked flavour of 210
                # is handled in redeliver, where a prior active trade actually holds the item.)
                order.delivery_status = DeliveryStatus.needs_attention
                order.error_reason = (
                    "210 NO_ACCESS — StarPets не создал трейд: получатель не может принять "
                    "(закрытая приватность / возраст <13 / трейды отключены у покупателя). Проверь "
                    "ник/приватность покупателя; не помогает → возврат. Ретрай и rebuy тут НЕ помогут."
                )
                order.updated_at = datetime.utcnow()
                await db.commit()
                print(f"[Deliver] order_id={order_id} create_trade 210 NO_ACCESS (recipient cannot receive) → needs_attention", flush=True)
                try:
                    from app.telegram.bot import notify_problem
                    await notify_problem(order)
                except Exception as _e:
                    print(f"[Deliver] 210 notify failed order={order_id}: {_e}", flush=True)
                return
            raise
        print(f"[Deliver] create_trade response order_id={order_id}: {trade_resp}", flush=True)

        first_trade = (trade_resp.get("trades") or [{}])[0]
        trade_id = (
            first_trade.get("id")
            or first_trade.get("tradeId")
            or trade_resp.get("tradeId")
            or trade_resp.get("trade_id")
            or trade_resp.get("id")
            or (trade_resp.get("data") or {}).get("id")
        )
        if not trade_id:
            raise RuntimeError(f"create_trade returned no trade_id: {trade_resp}")

        linked = (
            first_trade.get("linkedRobloxAccount")
            or trade_resp.get("linkedRobloxAccount")
            or (trade_resp.get("data") or {}).get("linkedRobloxAccount")
            or {}
        )
        bot_name = linked.get("robloxAccountName") or linked.get("username") or linked.get("name")
        print(f"[Deliver] trade_id={trade_id} bot_name={bot_name!r}", flush=True)

        # 4. Send friendship request so buyer can add bot
        try:
            friendship_resp = await starpets.send_friendship(
                trade_id=int(trade_id),
            )
            print(f"[Deliver] friendship sent resp={friendship_resp}", flush=True)
        except Exception as e:
            # Non-fatal — trade exists, buyer can still add bot manually
            print(f"[Deliver] friendship request failed (non-fatal): {e}", flush=True)

        # 5. Persist results
        now = datetime.utcnow()
        order.starpets_custom_id = str(trade_id)
        order.bot_name = bot_name
        order.delivery_status = DeliveryStatus.dispatched
        order.dispatched_at = now          # start the 10-min delivery timer
        order.updated_at = now
        await db.commit()

        print(
            f"[Deliver] done order_id={order_id} trade_id={trade_id} status=dispatched",
            flush=True,
        )
