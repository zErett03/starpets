from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Order, Task, TaskKind, DeliveryStatus
from app.clients.starpets import starpets

_STATUS_FINISHED = 8
_STATUS_FAILED = {6, 7}
_STATUS_STARTED = 4
_STATUS_EXPIRED_STR = {"timeout", "expired", "cancelled", "cancel"}
_STATUS_EXPIRED_INT = {5, 9}
_FRIENDSHIP_RETRY_AFTER = timedelta(minutes=5)
_MAX_TRADE_RETRIES = 3


def _is_expired(status) -> bool:
    if isinstance(status, str):
        return status.lower() in _STATUS_EXPIRED_STR
    return status in _STATUS_EXPIRED_INT


async def monitor_all_deliveries() -> None:
    """APScheduler job: poll all dispatched orders and update statuses."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order).where(Order.delivery_status == DeliveryStatus.dispatched)
        )
        orders = result.scalars().all()

        print(f"[MonitorDelivery] dispatched orders: {len(orders)}", flush=True)
        for o in orders:
            print(
                f"[MonitorDelivery]   order_id={o.id} ggsel_order_id={o.ggsel_order_id} "
                f"roblox_username={o.roblox_username!r} trade_id={o.starpets_custom_id}",
                flush=True,
            )

        if not orders:
            return

        try:
            trades = await starpets.get_bulk_trade_updates(limit=50)
        except Exception as e:
            print(f"[MonitorDelivery] get_bulk_trade_updates error: {e}", flush=True)
            return

        trade_map: dict[str, dict] = {}
        for t in trades:
            for key in ("tradeId", "id", "customId", "custom_id"):
                tid = str(t.get(key) or "")
                if tid:
                    trade_map[tid] = t

        print(
            f"[MonitorDelivery] trades fetched={len(trades)} mapped={len(trade_map)}",
            flush=True,
        )

        now = datetime.utcnow()
        for order in orders:
            trade_key = str(order.starpets_custom_id or "")
            if not trade_key:
                continue

            trade = trade_map.get(trade_key)
            if trade is None:
                continue

            status = trade.get("status") or (trade.get("data") or {}).get("status")
            print(
                f"[MonitorDelivery] order_id={order.id} trade_id={trade_key} status={status}",
                flush=True,
            )

            if status == _STATUS_FINISHED:
                order.delivery_status = DeliveryStatus.done
                order.delivered_at = now
                db.add(Task(
                    kind=TaskKind.MARK_DELIVERED,
                    priority=1,
                    payload={"order_id": order.id},
                ))
                print(
                    f"[MonitorDelivery] order_id={order.id} → delivered, MARK_DELIVERED queued",
                    flush=True,
                )

            elif status in _STATUS_FAILED:
                order.delivery_status = DeliveryStatus.failed
                order.error_reason = f"trade status={status}"
                print(
                    f"[MonitorDelivery] order_id={order.id} → failed (status={status})",
                    flush=True,
                )

            elif _is_expired(status):
                print(
                    f"[MonitorDelivery] order_id={order.id} trade expired (status={status!r}) "
                    f"retry_count={order.trade_retry_count}",
                    flush=True,
                )
                if order.trade_retry_count >= _MAX_TRADE_RETRIES:
                    order.delivery_status = DeliveryStatus.needs_attention
                    order.error_reason = f"trade expired after {_MAX_TRADE_RETRIES} retries"
                    print(
                        f"[MonitorDelivery] order_id={order.id} → needs_attention (max retries reached)",
                        flush=True,
                    )
                else:
                    purchased_item_id = (order.starpets_purchase_id or "").split(",")[0]
                    roblox_username = order.roblox_username or ""
                    if not purchased_item_id or not roblox_username:
                        order.delivery_status = DeliveryStatus.needs_attention
                        order.error_reason = "trade expired but missing purchase_id or username"
                        print(
                            f"[MonitorDelivery] order_id={order.id} → needs_attention "
                            f"(missing purchase_id={purchased_item_id!r} or username={roblox_username!r})",
                            flush=True,
                        )
                    else:
                        try:
                            trade_resp = await starpets.create_trade(
                                purchased_item_ids=[purchased_item_id],
                                roblox_username=roblox_username,
                            )
                            first_trade = (trade_resp.get("trades") or [{}])[0]
                            new_trade_id = (
                                first_trade.get("id")
                                or first_trade.get("tradeId")
                                or trade_resp.get("tradeId")
                                or trade_resp.get("trade_id")
                                or trade_resp.get("id")
                                or (trade_resp.get("data") or {}).get("id")
                            )
                            if not new_trade_id:
                                raise RuntimeError(f"create_trade returned no trade_id: {trade_resp}")

                            linked = (
                                first_trade.get("linkedRobloxAccount")
                                or trade_resp.get("linkedRobloxAccount")
                                or (trade_resp.get("data") or {}).get("linkedRobloxAccount")
                                or {}
                            )
                            bot_name = linked.get("robloxAccountName") or linked.get("username") or linked.get("name")

                            order.starpets_custom_id = str(new_trade_id)
                            order.bot_name = bot_name or order.bot_name
                            order.trade_retry_count = (order.trade_retry_count or 0) + 1
                            order.updated_at = now
                            print(
                                f"[MonitorDelivery] order_id={order.id} → new trade_id={new_trade_id} "
                                f"retry_count={order.trade_retry_count}",
                                flush=True,
                            )

                            try:
                                await starpets.send_friendship(
                                    trade_id=int(new_trade_id),
                                    item_id=purchased_item_id,
                                    username=roblox_username,
                                )
                            except Exception as fe:
                                print(
                                    f"[MonitorDelivery] order_id={order.id} friendship failed (non-fatal): {fe}",
                                    flush=True,
                                )
                        except Exception as e:
                            print(
                                f"[MonitorDelivery] order_id={order.id} create_trade on retry failed: {e}",
                                flush=True,
                            )
                            order.delivery_status = DeliveryStatus.needs_attention
                            order.error_reason = f"trade expired, retry create_trade failed: {e}"

            elif status == _STATUS_STARTED:
                dispatched_at = order.updated_at
                if dispatched_at is None:
                    continue
                elapsed = now - dispatched_at.replace(tzinfo=None)
                if elapsed > _FRIENDSHIP_RETRY_AFTER:
                    item_id = (order.starpets_purchase_id or "").split(",")[0]
                    try:
                        await starpets.send_friendship(
                            trade_id=int(trade_key),
                            item_id=item_id,
                            username=order.roblox_username or "",
                        )
                        print(
                            f"[MonitorDelivery] order_id={order.id} friendship retried "
                            f"(elapsed={elapsed})",
                            flush=True,
                        )
                    except Exception as e:
                        print(
                            f"[MonitorDelivery] order_id={order.id} friendship retry failed: {e}",
                            flush=True,
                        )

        await db.commit()


async def monitor_delivery(order_id: int) -> None:
    """Legacy task-runner entry point — delegates to batch monitor."""
    await monitor_all_deliveries()
