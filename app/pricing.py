"""Robust floor computation (shared by the event feed, the floor-reconcile sweep and relive).

The "floor" of a product = the cheapest price we can actually buy it at. Naive `min(price_usd)`
is fragile: a single anomalously-cheap listing (a mispriced/bait item) or a stale row left behind
after its item sold both drag the floor far below the real market, which turns the ggsel card into
an underpriced trap (buyer pays, live cost is 100x, delivery blocks at a loss).

robust_floor fixes both:
  * freshness  — prefer rows updated within FLOOR_MAX_AGE_H; only fall back to older rows if there
                 are no fresh ones (kills stale phantoms left by missed sold/delete events).
  * outliers   — with enough candidates, drop anything priced below OUTLIER_FRAC × median before
                 taking the min (kills single cheap bait listings).
Only reserve_level == 0 (FREE / buyable) rows with a positive price are considered.
"""
from datetime import datetime, timezone, timedelta
from statistics import median

from sqlalchemy import select

from app.db.models import StoreItem

FLOOR_MAX_AGE_H = 24     # ignore store rows staler than this (unless none are fresh)
OUTLIER_FRAC = 0.5       # drop prices below this fraction of the median of the candidate pool
_MIN_POOL_FOR_OUTLIER = 3  # need at least this many candidates before outlier rejection kicks in


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def robust_floor(items, now=None, max_age_h: float = FLOOR_MAX_AGE_H,
                 outlier_frac: float = OUTLIER_FRAC) -> float | None:
    """items: iterable of (price_usd, reserve_level, updated_at). Returns robust floor USD or None.

    None means no buyable stock (all rows reserved / non-positive) -> caller should pause the card.
    """
    now = now or datetime.now(timezone.utc)
    avail = []  # (price, updated_at)
    for price, reserve, updated in items:
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        if p <= 0 or int(reserve or 0) != 0:
            continue
        avail.append((p, _utc(updated)))
    if not avail:
        return None

    cutoff = now - timedelta(hours=max_age_h)
    fresh = [p for (p, u) in avail if u is not None and u >= cutoff]
    pool = sorted(fresh if fresh else [p for (p, _) in avail])

    if len(pool) >= _MIN_POOL_FOR_OUTLIER:
        med = median(pool)
        filtered = [p for p in pool if p >= med * outlier_frac]
        if filtered:
            pool = filtered
    return pool[0]


async def robust_floors_for(db, pids) -> dict:
    """Batched: {product_id -> robust_floor USD or None} for the given product ids.

    One query loads every store row for the ids; missing ids (no rows at all) map to None.
    """
    pids = list({int(p) for p in pids})
    if not pids:
        return {}
    rows = (await db.execute(
        select(StoreItem.product_id, StoreItem.price_usd,
               StoreItem.reserve_level, StoreItem.updated_at)
        .where(StoreItem.product_id.in_(pids))
    )).all()
    grouped: dict = {}
    for pid, price, reserve, updated in rows:
        grouped.setdefault(int(pid), []).append((price, reserve, updated))
    now = datetime.now(timezone.utc)
    return {pid: robust_floor(grouped.get(pid, []), now) for pid in pids}
