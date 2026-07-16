"""Shared re-delivery helper: create a NEW trade for an order that already owns a
purchased item (order.starpets_purchase_id), without re-buying.

Used by:
  - the operator "Новый трейд" / "новый логин" actions in /admin
  - the server-side 10-min delivery timer (auto-retrigger in monitor_delivery)

Because StarPets never emits a terminal trade event, a failed re-creation is our
strongest delivery signal:
  * code 130 NOT_FOUND  -> the item already left our inventory => DELIVERED
  * code 210 NO_ACCESS  -> item locked in an active trade   => NOT delivered (in flight)
  * success             -> item was still ours              => NOT delivered, restarted
"""
from datetime import datetime

import httpx
from sqlalchemy import select

from app.clients.starpets import starpets
from app.db.models import DeliveryStatus, Task, TaskKind, TradeEvent


async def _order_reached_in_progress(db, order_id) -> bool:
    """True if any trade for this order ever reached IN_PROGRESS(5) or FINISHED(8),
    per the persisted audit trail — our proof the in-game exchange actually executed.
    Used to make the 130 -> 'delivered' inference safe (a 130 without reaching 5 usually
    means the item is locked in a concurrent/active trade, NOT delivered)."""
    row = (await db.execute(
        select(TradeEvent.id).where(
            TradeEvent.order_id == order_id,
            TradeEvent.status.in_([5, 8]),
        ).limit(1)
    )).first()
    return row is not None


def _extract_trade(trade_resp: dict):
    first = (trade_resp.get("trades") or [{}])[0]
    trade_id = (
        first.get("id") or first.get("tradeId") or trade_resp.get("tradeId")
        or trade_resp.get("trade_id") or trade_resp.get("id")
        or (trade_resp.get("data") or {}).get("id")
    )
    linked = (
        first.get("linkedRobloxAccount") or trade_resp.get("linkedRobloxAccount")
        or (trade_resp.get("data") or {}).get("linkedRobloxAccount") or {}
    )
    bot_name = linked.get("robloxAccountName") or linked.get("username") or linked.get("name")
    return trade_id, bot_name


async def redeliver_same_item(db, order) -> str:
    """Attempt to (re)create a trade for `order` reusing its purchased item.

    Mutates `order` (and may enqueue a MARK_DELIVERED task) on the given session but
    does NOT commit — the caller owns the transaction. Returns a human-readable
    result string (also stored on order.last_redeliver_result).
    """
    purchase_id = order.starpets_purchase_id
    username = (order.roblox_username or "").strip()

    if not purchase_id:
        result = "⚪ нет purchase_id — предмет ещё не куплен; используйте обычный DELIVER (/trigger-deliver)"
        order.last_redeliver_result = result
        order.updated_at = datetime.utcnow()
        return result
    if not username:
        result = "⚪ не указан Roblox-ник — заполните ник и повторите"
        order.last_redeliver_result = result
        order.updated_at = datetime.utcnow()
        return result

    # 1-hour item lifetime: past it StarPets has refunded the item — recreating a trade is futile.
    from app.deadline import item_expired
    if item_expired(order):
        result = ("🔴 предмет протух >1ч — StarPets вернул за него деньги. Доставка этим предметом "
                  "невозможна: перекупить свежий (Force) или вернуть деньги покупателю.")
        order.delivery_status = DeliveryStatus.needs_attention
        order.error_reason = result
        order.last_redeliver_result = result
        order.updated_at = datetime.utcnow()
        return result

    # Cancel the previous (hung) trade FIRST so the item is released — otherwise create_trade
    # returns 130/210 because the item is still locked in the active trade. Skip the cancel if
    # the exchange already reached IN_PROGRESS(5), where the item may be mid-delivery.
    prev_trade_id = (order.starpets_custom_id or "").strip()
    if prev_trade_id:
        if await _order_reached_in_progress(db, order.id):
            print(f"[redeliver] order={order.id} reached in_progress(5) — NOT cancelling "
                  f"prev trade {prev_trade_id}", flush=True)
        else:
            try:
                await starpets.cancel_trade(prev_trade_id, "seller_cancel_trade")
                print(f"[redeliver] cancelled prev trade {prev_trade_id} to free item "
                      f"order={order.id}", flush=True)
            except Exception as e:
                print(f"[redeliver] cancel prev trade {prev_trade_id} failed (continuing): {e}",
                      flush=True)

    try:
        trade_resp = await starpets.create_trade(
            purchased_item_ids=[purchase_id], roblox_username=username
        )
        trade_id, bot_name = _extract_trade(trade_resp)
        if not trade_id:
            result = f"⚠️ create_trade без trade_id: {str(trade_resp)[:200]}"
            order.last_redeliver_result = result
            order.updated_at = datetime.utcnow()
            return result

        now = datetime.utcnow()
        order.starpets_custom_id = str(trade_id)
        order.bot_name = bot_name
        order.starpets_status = None
        order.delivery_status = DeliveryStatus.dispatched
        order.error_reason = None
        order.dispatched_at = now          # (re)start the 10-min delivery timer
        order.updated_at = now
        try:
            await starpets.send_friendship(trade_id=int(trade_id))
        except Exception as e:
            print(f"[redeliver] friendship failed order={order.id}: {e}", flush=True)
        result = (
            f"✅ Создан новый трейд {trade_id}, бот {bot_name or '?'} — "
            f"предмет был у нас (НЕ доставлено), выдача перезапущена"
        )
        order.last_redeliver_result = result
        return result

    except httpx.HTTPStatusError as exc:
        code = None
        body = ""
        try:
            j = exc.response.json()
            code = j.get("code")
            body = str(j)
        except Exception:
            body = (exc.response.text or "")[:200]
        try:
            code = int(code)
        except (ValueError, TypeError):
            code = None

        now = datetime.utcnow()
        if code == 130:
            # 130 NOT_FOUND = item not in our purchasable inventory. That means DELIVERED
            # ONLY if the trade actually executed (reached IN_PROGRESS=5). If the exchange
            # never started, a 130 more likely means the item is locked in a concurrent /
            # active trade — NOT delivered — so we must NOT auto-close (that would release
            # the buyer's payment for nothing). Flag for the operator instead.
            delivered = await _order_reached_in_progress(db, order.id)
            if delivered:
                order.delivery_status = DeliveryStatus.done
                if order.delivered_at is None:
                    order.delivered_at = now
                order.error_reason = None
                order.updated_at = now
                db.add(Task(kind=TaskKind.MARK_DELIVERED, priority=1, payload={"order_id": order.id}))
                result = (
                    "🟢 create_trade 130 NOT_FOUND + обмен исполнялся (статус 5) — "
                    "ДОСТАВЛЕНО; заказ закрыт, MARK_DELIVERED поставлен"
                )
            else:
                order.delivery_status = DeliveryStatus.needs_attention
                order.error_reason = (
                    "130 — предмет не выводится после протухшего трейда (StarPets держит). Обычно "
                    "освобождается сам: жми 🔁 Повторить (/retry-delivery) — ретраит с бэкоффом в пределах 1ч (после StarPets вернёт деньги) "
                    "и дожмётся, когда отпустит. Если и после — предмет застрял у StarPets (редко) → "
                    "возврат покупателю + их поддержка. Разовый «Новый трейд» окно не покрывает."
                )
                order.updated_at = now
                result = (
                    "🟡 create_trade 130 — предмет пока не выводится (StarPets держит после протухшего "
                    "трейда). Жми 🔁 Повторить (retry-delivery) — в пределах 1ч от оплаты. Если не дожмётся — "
                    "застрял у StarPets, нужен возврат/их поддержка."
                )
        elif code == 210:
            order.delivery_status = DeliveryStatus.needs_attention
            order.error_reason = (
                "210 NO_ACCESS — предмет залочен/недоступен для вывода (держит активный трейд или "
                "блок StarPets). Освободится при истечении (~15 мин) → 🔁 Повторить. Держится долго "
                "— завис у StarPets: их поддержка или возврат покупателю."
            )
            order.updated_at = now
            result = "🟡 210 NO_ACCESS: предмет залочен — освободится ~15 мин, затем 🔁 Повторить (долго держится → StarPets support / возврат)"
        else:
            order.delivery_status = DeliveryStatus.needs_attention
            order.error_reason = f"redeliver err {exc.response.status_code} code={code}: {body[:120]}"
            order.updated_at = now
            result = f"⚪ create_trade {exc.response.status_code} code={code}: {body[:180]}"
        order.last_redeliver_result = result
        return result

    except Exception as e:
        order.delivery_status = DeliveryStatus.needs_attention
        order.error_reason = f"redeliver exception: {str(e)[:120]}"
        order.updated_at = datetime.utcnow()
        result = f"⚪ ошибка create_trade: {e}"
        order.last_redeliver_result = result
        return result
