"""Phase 3: keep SKU card prices fresh from live `offers.price_rub`.

ggsel's variant upsert can update prices in place (ids preserved) but CANNOT move the default
flag (422 "exactly 1 default"). So:
  * default still ~cheapest (within STALE_FACTOR of the live min) -> cheap in-place upsert:
    keep the default, base = its price, every other variant gets a +/- modifier.
  * default drifted well above the min -> REBUILD the option (delete + re-add) so the default
    becomes the cheapest again; remap SkuVariant ids. Heavier, so bounded per run.
Only cards whose prices drifted beyond a threshold are touched; rate-limited.
"""
import asyncio

from sqlalchemy import select, update as sql_update

from app.db import AsyncSessionLocal
from app.db.models import SkuVariant, SkuProduct, Offer
from app.clients.ggsel import ggsel_office
from app.workers.sku_builder import _variant_sort_key

_STALE_FACTOR = 1.15   # default price may sit up to 15% above the live min before we rebuild


class _P:
    __slots__ = ("age", "flyable", "rideable")

    def __init__(self, age, flyable, rideable):
        self.age, self.flyable, self.rideable = age, flyable, rideable


def _drifted(live: float, snap: float, thr_rub: float, thr_pct: float) -> bool:
    return abs(live - snap) >= max(thr_rub, snap * thr_pct)


async def _cheap_upsert(gid, opt, ordered, default_vid, base):
    payload = []
    for pos, v in enumerate(ordered):
        live = float(v.live)
        delta = round(live - base, 2)
        payload.append({
            "id": v.ggsel_variant_id,
            "title_ru": f"{v.label} — {int(round(live))}₽",
            "title_en": v.label,
            "price": abs(delta),
            "discount_type": "fixed",
            "impact_type": "increase" if delta >= 0 else "decrease",
            "is_default": (v.ggsel_variant_id == default_vid),
            "status": "active", "position": pos,
        })
    await ggsel_office.update_price(gid, round(base, 2))
    await ggsel_office.update_variants(gid, opt, payload)
    async with AsyncSessionLocal() as db:
        for v in ordered:
            await db.execute(sql_update(SkuVariant)
                             .where(SkuVariant.ggsel_variant_id == v.ggsel_variant_id)
                             .values(price_rub=float(v.live)))
        await db.commit()


async def _rebuild_option(gid, ordered, base, default_pid):
    # Delete EVERY "Вариант" radio option first (including orphan empties left by earlier failed
    # rebuilds), then create exactly one fresh option -> no leftover required-empty option that
    # would block checkout. Then remap SkuVariant ids.
    opts = await ggsel_office.get_options(gid)
    odata = opts.get("data") if isinstance(opts, dict) else opts
    old_ids = [o.get("id") for o in (odata or [])
               if o.get("type") == "radio_button"
               and (o.get("title_ru") or "").strip() == "Вариант" and o.get("id") is not None]
    if old_ids:
        await ggsel_office.delete_options(gid, old_ids)
    new_opt = await ggsel_office.create_radio_option(gid, "Вариант", "Variant", position=3)
    # Guarantee EXACTLY one default regardless of card state: dedupe by product, pick the default
    # variant by identity (default_pid, else cheapest), recompute base from it so its modifier is 0,
    # and create it FIRST at position 0. Prevents the "должен быть указан 1 вариант по умолчанию" 422.
    seen, uniq = set(), []
    for v in ordered:
        if v.starpets_product_id in seen:
            continue
        seen.add(v.starpets_product_id)
        uniq.append(v)
    ordered = uniq
    if not ordered:
        print(f"[SkuPriceSync] rebuild gid={gid}: no variants — skip", flush=True)
        return
    default_v = next((v for v in ordered if v.starpets_product_id == default_pid), None)
    if default_v is None:
        print(f"[SkuPriceSync] rebuild gid={gid}: default_pid={default_pid} not among "
              f"{len(ordered)} variants -> using cheapest-by-live", flush=True)
        default_v = min(ordered, key=lambda v: float(v.live))
    base = float(default_v.live)
    creation = [default_v] + [v for v in ordered if v is not default_v]
    payload, meta = [], []
    for pos, v in enumerate(creation):
        live = float(v.live)
        delta = round(live - base, 2)
        payload.append({
            "title_ru": f"{v.label} — {int(round(live))}₽", "title_en": v.label,
            "price": abs(delta), "discount_type": "fixed",
            "impact_type": "increase" if delta >= 0 else "decrease",
            "is_default": (v is default_v), "status": "active", "position": pos,
        })
        meta.append((v, live))
    created = await ggsel_office.add_variants_bulk(gid, new_opt, payload)
    async with AsyncSessionLocal() as db:
        for (v, live), cv in zip(meta, created):
            await db.execute(sql_update(SkuVariant)
                             .where(SkuVariant.ggsel_variant_id == v.ggsel_variant_id)
                             .values(ggsel_option_id=new_opt, ggsel_variant_id=cv.get("id"),
                                     price_rub=live))
        await db.commit()
    await ggsel_office.update_price(gid, round(base, 2))


async def sku_price_sync(threshold_rub: float = 5.0, threshold_pct: float = 0.05,
                         max_cards: int = 100, max_rebuilds: int = 20,
                         dry_run: bool = False) -> dict:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                SkuVariant.ggsel_offer_id, SkuVariant.ggsel_option_id, SkuVariant.ggsel_variant_id,
                SkuVariant.starpets_product_id, SkuVariant.label, SkuVariant.price_rub,
                Offer.price_rub.label("live"),
                SkuProduct.age, SkuProduct.flyable, SkuProduct.rideable,
            )
            .join(Offer, Offer.starpets_product_id == SkuVariant.starpets_product_id)
            .join(SkuProduct, SkuProduct.product_id == SkuVariant.starpets_product_id)
            .where(Offer.price_rub.isnot(None), Offer.price_rub > 0,
                   SkuVariant.hidden.is_(False))   # skip stock-hidden variants (archived on ggsel)
        )).all()

    cards: dict = {}
    for r in rows:
        cards.setdefault((r.ggsel_offer_id, r.ggsel_option_id), []).append(r)

    checked = drifted = upserted = rebuilt = errors = 0
    results = []
    for (gid, opt), variants in cards.items():
        checked += 1
        if not any(_drifted(float(v.live), float(v.price_rub or 0), threshold_rub, threshold_pct)
                   for v in variants):
            continue
        drifted += 1
        if dry_run:
            if len(results) < 50:
                results.append({"gid": gid, "variants": len(variants)})
            continue
        if (upserted + rebuilt) >= max_cards:
            break

        try:
            opts = await ggsel_office.get_options(gid)
            odata = opts.get("data") if isinstance(opts, dict) else opts
            default_vid = None
            for o in (odata or []):
                if o.get("id") == opt:
                    for vv in (o.get("variants") or []):
                        if vv.get("is_default"):
                            default_vid = vv.get("id")
                            break
                    break
            by_vid = {v.ggsel_variant_id: v for v in variants}
            min_v = min(variants, key=lambda v: float(v.live))
            min_live = float(min_v.live)
            ordered = sorted(variants, key=lambda v: _variant_sort_key(_P(v.age, v.flyable, v.rideable)))
            default_live = float(by_vid[default_vid].live) if default_vid in by_vid else None

            if default_live is not None and default_live <= min_live * _STALE_FACTOR:
                await _cheap_upsert(gid, opt, ordered, default_vid, default_live)
                upserted += 1
                results.append({"gid": gid, "mode": "upsert", "base": round(default_live, 2)})
            else:
                if rebuilt >= max_rebuilds:
                    continue  # bound the heavy path per run; picked up next cycle
                await _rebuild_option(gid, ordered, min_live, min_v.starpets_product_id)
                rebuilt += 1
                results.append({"gid": gid, "mode": "rebuild", "base": round(min_live, 2)})
        except Exception as e:
            errors += 1
            print(f"[SkuPriceSync] gid={gid} failed: {type(e).__name__}: {e}", flush=True)
            results.append({"gid": gid, "error": f"{type(e).__name__}: {e}"})
        await asyncio.sleep(0.3)

    summary = {"cards_checked": checked, "drifted": drifted, "upserted": upserted,
               "rebuilt": rebuilt, "errors": errors, "dry_run": dry_run, "sample": results[:30]}
    print(f"[SkuPriceSync] {summary}", flush=True)
    return summary
