"""Stock-driven variant visibility: hide SKU variants that are out of stock so buyers can't buy
what we can't deliver.

ggsel can't toggle a variant's visibility cheaply (status=archived removes it one-way; no un-archive)
and can't move the default without a 422. But bulk variant creation makes an option REBUILD cheap
(~4 ggsel calls). So: per card, compute the in-stock set (store_items, with 2-cycle hysteresis); when
that set changes, rebuild the "Вариант" option with only the in-stock variants (default = cheapest
in-stock) and remap SkuVariant. If nothing is in stock -> pause the card; unpause when it returns.
Only changed cards touch ggsel; rate-limited.
"""
import asyncio

from sqlalchemy import select, update as sql_update, exists, and_

from app.db import AsyncSessionLocal
from app.db.models import SkuVariant, SkuProduct, Offer, StoreItem
from app.clients.ggsel import ggsel_office
from app.workers.sku_builder import _variant_sort_key

_HIDE_AFTER = 2                 # consecutive out-of-stock checks before hiding (hysteresis)
_oos_streak: dict = {}          # (gid, product_id) -> consecutive OOS count (in-memory)
_running = False                # guard: no concurrent writing runs (racing rebuilds -> 422)


class _P:
    __slots__ = ("age", "flyable", "rideable")

    def __init__(self, age, flyable, rideable):
        self.age, self.flyable, self.rideable = age, flyable, rideable


async def _rebuild_shown(gid, opt_hint, shown, default_pid):
    """Delete all 'Вариант' options, recreate ONE with `shown` variants (default=default_pid),
    bulk-create, and remap SkuVariant (shown -> new ids + hidden=False).

    Под общей блокировкой по gid: price_sync тоже пересобирает эту же опцию, и одновременная
    пересборка ловит промежуточное «нет дефолта» → 422. Блокировка сериализует их."""
    from app.workers.sku_lock import rebuild_lock
    async with rebuild_lock(gid):
        return await _rebuild_shown_locked(gid, opt_hint, shown, default_pid)


async def _rebuild_shown_locked(gid, opt_hint, shown, default_pid):
    opts = await ggsel_office.get_options(gid)
    odata = opts.get("data") if isinstance(opts, dict) else opts
    old_ids = [o.get("id") for o in (odata or [])
               if o.get("type") == "radio_button"
               and (o.get("title_ru") or "").strip() == "Вариант" and o.get("id") is not None]
    if old_ids:
        await ggsel_office.delete_options(gid, old_ids)
    new_opt = await ggsel_office.create_radio_option(gid, "Вариант", "Variant", position=3)

    base = min(float(v["live"]) for v in shown)
    ordered = sorted(shown, key=lambda v: _variant_sort_key(_P(v["age"], v["flyable"], v["rideable"])))
    creation = sorted(enumerate(ordered), key=lambda iv: 0 if iv[1]["pid"] == default_pid else 1)
    payload, meta = [], []
    for pos, v in creation:
        live = float(v["live"])
        delta = round(live - base, 2)
        payload.append({
            "title_ru": f"{v['label']} — {int(round(live))}₽", "title_en": v["label"],
            "price": abs(delta), "discount_type": "fixed",
            "impact_type": "increase" if delta >= 0 else "decrease",
            "is_default": (v["pid"] == default_pid), "status": "active", "position": pos,
        })
        meta.append((v["pid"], live))
    created = await ggsel_office.add_variants_bulk(gid, new_opt, payload)
    async with AsyncSessionLocal() as db:
        for (pid, live), cv in zip(meta, created):
            await db.execute(sql_update(SkuVariant)
                             .where(SkuVariant.starpets_product_id == pid,
                                    SkuVariant.ggsel_offer_id == gid)
                             .values(ggsel_option_id=new_opt, ggsel_variant_id=cv.get("id"),
                                     price_rub=live, hidden=False))
        await db.commit()
    await ggsel_office.update_price(gid, round(base, 2))
    return new_opt


async def sku_stock_sync(max_cards: int = 40, dry_run: bool = False) -> dict:
    """Guard against concurrent writing runs (parallel rebuilds of one card race into a
    transient no-default state -> 422). dry_run is read-only, so it never blocks."""
    global _running
    if not dry_run:
        if _running:
            print("[SkuStockSync] already running — skip", flush=True)
            return {"skipped": "already running"}
        _running = True
    try:
        return await _sku_stock_sync_impl(max_cards, dry_run)
    finally:
        if not dry_run:
            _running = False


async def _sku_stock_sync_impl(max_cards: int, dry_run: bool) -> dict:
    _in_stock = exists().where(and_(StoreItem.product_id == SkuVariant.starpets_product_id,
                                    StoreItem.reserve_level == 0))
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                SkuVariant.ggsel_offer_id, SkuVariant.ggsel_option_id, SkuVariant.starpets_product_id,
                SkuVariant.label, SkuVariant.hidden, Offer.price_rub.label("live"),
                SkuProduct.age, SkuProduct.flyable, SkuProduct.rideable,
                _in_stock.label("in_stock"),
            )
            .join(Offer, Offer.starpets_product_id == SkuVariant.starpets_product_id)
            .join(SkuProduct, SkuProduct.product_id == SkuVariant.starpets_product_id)
            .where(Offer.price_rub.isnot(None), Offer.price_rub > 0)
        )).all()

    cards: dict = {}
    for r in rows:
        cards.setdefault(r.ggsel_offer_id, []).append(r)

    checked = changed = rebuilt = paused = unpaused = errors = 0
    results = []
    for gid, variants in cards.items():
        checked += 1
        # hysteresis: desired-shown = in stock, or out for < _HIDE_AFTER cycles (grace)
        desired = set()
        for v in variants:
            key = (gid, v.starpets_product_id)
            if v.in_stock:
                _oos_streak[key] = 0
                desired.add(v.starpets_product_id)
            else:
                _oos_streak[key] = _oos_streak.get(key, 0) + 1
                if _oos_streak[key] < _HIDE_AFTER:
                    desired.add(v.starpets_product_id)
        current = {v.starpets_product_id for v in variants if not v.hidden}
        if desired == current:
            continue
        changed += 1
        if dry_run:
            if len(results) < 40:
                results.append({"gid": gid, "now_shown": len(current), "will_show": len(desired)})
            continue
        if (rebuilt + paused + unpaused) >= max_cards:
            break

        try:
            if not desired:
                # nothing in stock -> pause card, mark all hidden (option left as-is; card unsellable)
                await ggsel_office.pause_offer(gid)
                async with AsyncSessionLocal() as db:
                    await db.execute(sql_update(SkuVariant)
                                     .where(SkuVariant.ggsel_offer_id == gid).values(hidden=True))
                    await db.commit()
                paused += 1
                results.append({"gid": gid, "mode": "paused"})
            else:
                was_paused = (len(current) == 0)
                shown = [{"pid": v.starpets_product_id, "label": v.label, "live": v.live,
                          "age": v.age, "flyable": v.flyable, "rideable": v.rideable}
                         for v in variants if v.starpets_product_id in desired]
                default_pid = min(shown, key=lambda v: float(v["live"]))["pid"]
                await _rebuild_shown(gid, variants[0].ggsel_option_id, shown, default_pid)
                # mark out-of-stock ones hidden
                hidden_pids = [v.starpets_product_id for v in variants if v.starpets_product_id not in desired]
                if hidden_pids:
                    async with AsyncSessionLocal() as db:
                        await db.execute(sql_update(SkuVariant)
                                         .where(SkuVariant.ggsel_offer_id == gid,
                                                SkuVariant.starpets_product_id.in_(hidden_pids))
                                         .values(hidden=True))
                        await db.commit()
                if was_paused:
                    await ggsel_office.batch_activate([gid])
                    unpaused += 1
                rebuilt += 1
                results.append({"gid": gid, "mode": "rebuild", "shown": len(shown),
                                "was_paused": was_paused})
        except Exception as e:
            errors += 1
            print(f"[SkuStockSync] gid={gid} failed: {type(e).__name__}: {e}", flush=True)
            results.append({"gid": gid, "error": f"{type(e).__name__}: {e}"})
        await asyncio.sleep(0.3)

    summary = {"cards_checked": checked, "changed": changed, "rebuilt": rebuilt,
               "paused": paused, "unpaused": unpaused, "errors": errors,
               "dry_run": dry_run, "sample": results[:30]}
    print(f"[SkuStockSync] {summary}", flush=True)
    return summary
