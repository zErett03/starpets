from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import AsyncSessionLocal
from app.db.models import Order, Task, TaskKind, DeliveryStatus, KVState, TradeEvent
from app.clients.starpets import starpets


def _parse_event_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


async def _persist_trade_events(db, orders, events) -> None:
    """Idempotently store each status-bearing trade event for tracked orders.

    The audit trail (table trade_events) is our dispute-defense record since StarPets
    never emits a terminal event. order_id is stamped now so it survives re-deliver.
    """
    by_trade = {str(o.starpets_custom_id): o for o in orders if o.starpets_custom_id}
    if not by_trade:
        return
    for e in events:
        order = by_trade.get(str(e.get("tradeId") or ""))
        if not order:
            continue
        etype = e.get("event")
        data = e.get("data") or {}
        st = data.get("status")
        if etype == 1 and st is None:
            continue  # skip heartbeats (e.g. sessionTime) — only meaningful transitions
        try:
            sp_event_id = int(e["id"]) if e.get("id") is not None else None
        except (ValueError, TypeError):
            sp_event_id = None
        stmt = pg_insert(TradeEvent.__table__).values(
            order_id=order.id,
            trade_id=str(e.get("tradeId") or ""),
            sp_event_id=sp_event_id,
            event_type=etype,
            status=st,
            bot_name=order.bot_name,
            data_json=data,
            occurred_at=_parse_event_dt(data.get("updatedAt")),
            recorded_at=datetime.utcnow(),
        ).on_conflict_do_nothing(constraint="uq_trade_events_trade_event")
        await db.execute(stmt)

# Official trade status codes (per StarPets Business API docs):
#   0 CREATED · 1 DELAYED_START · 2 PENDING_FRIEND · 3 PENDING_START
#   4 STARTED · 5 IN_PROGRESS · 6 FAILED · 7 CANCELED · 8 FINISHED
_STATUS_FINISHED = 8
_STATUS_FAILED = {6, 7}

_CURSOR_KEY = "trades_cursor"
_MAX_PAGES = 40                       # safety cap: 40 * 50 = 2000 events per cycle
_FRIENDSHIP_WINDOW = timedelta(minutes=10)
# starpets_status values meaning "bot already accepted / trade moving" → stop re-pinging
_FRIENDSHIP_OPEN_STATES = (None, "", "0")

# Server-side delivery timer: how long a single trade's bot-online window lasts before
# we auto-recreate the trade (new bot). Anchored to order.dispatched_at (the moment the
# current trade's friendship was fired). Capped to avoid an infinite recreate loop.
_DELIVERY_TIMEOUT = timedelta(minutes=10)
_MAX_AUTO_RETRIES = 2


async def _get_cursor(db) -> int | None:
    row = (await db.execute(select(KVState).where(KVState.key == _CURSOR_KEY))).scalar_one_or_none()
    if row and row.value:
        try:
            return int(row.value)
        except ValueError:
            return None
    return None


async def _set_cursor(db, value: int) -> None:
    row = (await db.execute(select(KVState).where(KVState.key == _CURSOR_KEY))).scalar_one_or_none()
    if row:
        row.value = str(value)
    else:
        db.add(KVState(key=_CURSOR_KEY, value=str(value)))


async def monitor_all_deliveries() -> None:
    """APScheduler job (~30s): incrementally poll trade events by cursor and settle orders."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order).where(Order.delivery_status == DeliveryStatus.dispatched)
        )
        orders = result.scalars().all()

        print(f"[MonitorDelivery] dispatched orders: {len(orders)}", flush=True)
        for o in orders:
            print(
                f"[MonitorDelivery]   order_id={o.id} ggsel_order_id={o.ggsel_order_id} "
                f"roblox_username={o.roblox_username!r} trade_id={o.starpets_custom_id} "
                f"status={o.starpets_status}",
                flush=True,
            )

        if not orders:
            return

        # ---- 1. Incremental event poll by cursor (durable, survives redeploys) ----
        cursor = await _get_cursor(db)
        new_cursor = cursor
        events: list[dict] = []
        try:
            c = cursor
            for _ in range(_MAX_PAGES):
                batch = await starpets.get_bulk_trade_updates(limit=50, cursor=c)
                if not batch:
                    break
                events.extend(batch)
                ids = [int(e["id"]) for e in batch if e.get("id") is not None]
                if ids:
                    bmax = max(ids)
                    new_cursor = bmax if new_cursor is None else max(new_cursor, bmax)
                    c = bmax
                if len(batch) < 50:
                    break
        except Exception as e:
            print(f"[MonitorDelivery] get_bulk_trade_updates error: {e}", flush=True)
            return  # don't advance cursor — re-fetch next cycle

        # Latest status per trade from this batch (event:1 carries data.status;
        # event:2 "finished/canceled" has no data, so we keep the prior status event).
        status_by_trade: dict[str, int] = {}
        for e in events:
            if e.get("event") == 1:
                st = (e.get("data") or {}).get("status")
                if st is not None:
                    status_by_trade[str(e.get("tradeId"))] = st

        print(
            f"[MonitorDelivery] events fetched={len(events)} trades_with_status={len(status_by_trade)} "
            f"cursor={cursor}→{new_cursor}",
            flush=True,
        )

        # ---- 1b. Persist trade-event audit trail (dispute defense) ----
        await _persist_trade_events(db, orders, events)

        # ---- 2. Settle orders against new statuses ----
        now = datetime.utcnow()
        for order in orders:
            tkey = str(order.starpets_custom_id or "")
            if not tkey or tkey not in status_by_trade:
                continue
            status = status_by_trade[tkey]
            # persist last-known status only when it changed (avoids needless updated_at bumps)
            if order.starpets_status != str(status):
                order.starpets_status = str(status)
            print(
                f"[MonitorDelivery] order_id={order.id} trade_id={tkey} status={status}",
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
            # statuses 0–5 are healthy in-progress states → leave dispatched, no action.

        # ---- 3. Device-independent friendship re-send ----
        # deliver_order fires friendship once at T=0 (before the buyer added the bot);
        # the /delivery page re-send only works while the tab is foregrounded (unreliable
        # on mobile). Re-ping here every cycle while the bot hasn't accepted yet
        # (starpets_status still CREATED/unknown) and we're within the bot's online window.
        for order in orders:
            if order.delivery_status != DeliveryStatus.dispatched:
                continue  # just settled this cycle — skip
            if order.starpets_status not in _FRIENDSHIP_OPEN_STATES:
                continue  # bot already accepted / trade moving — re-send would 400
            tid = str(order.starpets_custom_id or "")
            if not tid:
                continue
            anchor = order.dispatched_at or order.updated_at
            anchor = anchor.replace(tzinfo=None) if anchor else None
            if anchor is None or (now - anchor) > _FRIENDSHIP_WINDOW:
                continue  # past the bot's online window
            try:
                await starpets.send_friendship(trade_id=int(tid))
                print(
                    f"[MonitorDelivery] order_id={order.id} friendship re-sent (periodic) "
                    f"trade_id={tid}",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"[MonitorDelivery] order_id={order.id} periodic friendship failed: {e}",
                    flush=True,
                )

        # ---- 3b. Server-side 10-min delivery timer: auto-recreate expired trades ----
        # The bot's online window (~10 min) has passed without delivery. Recreate the
        # trade (new bot, same purchased item) up to _MAX_AUTO_RETRIES times; the buyer's
        # /delivery page (20s auto-refresh) then shows the new bot + a fresh timer.
        # A create_trade code=130 means the item already left us -> auto-close as done.
        from app.workers.redeliver import redeliver_same_item
        for order in orders:
            if order.delivery_status != DeliveryStatus.dispatched:
                continue  # settled or already handled this cycle
            anchor = order.dispatched_at or order.updated_at
            anchor = anchor.replace(tzinfo=None) if anchor else None
            if anchor is None or (now - anchor) <= _DELIVERY_TIMEOUT:
                continue  # timer not expired yet
            retries = order.trade_retry_count or 0
            if retries < _MAX_AUTO_RETRIES:
                order.trade_retry_count = retries + 1
                result = await redeliver_same_item(db, order)
                print(
                    f"[MonitorDelivery] order_id={order.id} timer expired "
                    f"(retry {order.trade_retry_count}/{_MAX_AUTO_RETRIES}) → {result}",
                    flush=True,
                )
            else:
                order.delivery_status = DeliveryStatus.needs_attention
                if not order.error_reason:
                    order.error_reason = "delivery timeout: max auto-retries reached"
                order.updated_at = now
                print(
                    f"[MonitorDelivery] order_id={order.id} timer expired, "
                    f"max auto-retries reached → needs_attention",
                    flush=True,
                )

        # ---- 4. Persist cursor + order changes atomically ----
        if new_cursor is not None and new_cursor != cursor:
            await _set_cursor(db, new_cursor)
        await db.commit()


async def monitor_delivery(order_id: int) -> None:
    """Legacy task-runner entry point — delegates to batch monitor."""
    await monitor_all_deliveries()
