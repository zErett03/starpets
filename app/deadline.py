"""Shared delivery deadline: StarPets auto-refunds an undelivered item ~1 hour after purchase
(the item's lifetime once a trade has started). Past that the item is GONE and cannot be
delivered with the same purchase — retries are futile, so we stop and flag for manual handling.
Anchored to order.paid_at (stable: unlike dispatched_at it does NOT reset on trade recreate)."""
from datetime import datetime, timedelta

ITEM_LIFETIME = timedelta(minutes=60)


def item_expired(order, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    # Anchor to the CURRENT trade's start (dispatched_at), which RESETS on each rebuy/redeliver —
    # so a freshly re-bought item gets its own fresh 1h window, not the original payment time.
    anchor = (getattr(order, "dispatched_at", None)
              or getattr(order, "paid_at", None)
              or getattr(order, "created_at", None))
    if anchor is None:
        return False
    try:
        anchor = anchor.replace(tzinfo=None)
    except Exception:
        return False
    return (now - anchor) > ITEM_LIFETIME
