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

from app.clients.starpets import starpets
from app.db.models import DeliveryStatus, Task, TaskKind


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
            # Item is gone from our inventory -> it was delivered. Auto-close.
            order.delivery_status = DeliveryStatus.done
            if order.delivered_at is None:
                order.delivered_at = now
            order.error_reason = None
            order.updated_at = now
            db.add(Task(kind=TaskKind.MARK_DELIVERED, priority=1, payload={"order_id": order.id}))
            result = (
                "🟢 create_trade 130 NOT_FOUND: предмет ушёл — ДОСТАВЛЕНО; "
                "заказ закрыт, MARK_DELIVERED поставлен"
            )
        elif code == 210:
            order.delivery_status = DeliveryStatus.needs_attention
            order.error_reason = f"redeliver 210 NO_ACCESS: {body[:120]}"
            order.updated_at = now
            result = "🟡 create_trade 210 NO_ACCESS: залочен в активном трейде — НЕ доставлено, нужна проверка"
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
