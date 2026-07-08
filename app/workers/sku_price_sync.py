"""Phase 3: keep SKU card prices fresh from live `offers.price_rub` via in-place variant upsert.

For each SKU card, the base = cheapest live combo (its variant is the default, modifier 0); every
other variant's modifier = its live price - base. When any variant drifts beyond a threshold we push
`update_price(base)` + one `update_variants(all)` (variant ids preserved), regenerating the
"— NNN₽" title and the age/fly-ride display order. Only drifted cards touch ggsel; rate-limited.
"""
import asyncio
from datetime import datetime

from sqlalchemy import select, update as sql_update

from app.db import AsyncSessionLocal
from app.db.models import SkuVariant, SkuProduct, Offer
from app.clients.ggsel import ggsel_office
from app.workers.sku_builder import _variant_sort_key


class _P:
    __slots__ = ("age", "flyable", "rideable")

    def __init__(self, age, flyable, rideable):
        self.age, self.flyable, self.rideable = age, flyable, rideable


def _drifted(live: float, snap: float, thr_rub: float, thr_pct: float) -> bool:
    return abs(live - snap) >= max(thr_rub, snap * thr_pct)


async def sku_price_sync(threshold_rub: float = 5.0, threshold_pct: float = 0.05,
                         max_cards: int = 100, dry_run: bool = False) -> dict:
    # 1. Every SKU variant with its live price + display attrs, grouped by card.
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
            .where(Offer.price_rub.isnot(None), Offer.price_rub > 0)
        )).all()

    cards: dict = {}
    for r in rows:
        cards.setdefault((r.ggsel_offer_id, r.ggsel_option_id), []).append(r)

    checked = drifted = updated = errors = 0
    results = []
    for (gid, opt), variants in cards.items():
        checked += 1
        need = any(_drifted(float(v.live), float(v.price_rub or 0), threshold_rub, threshold_pct)
                   for v in variants)
        if not need:
            continue
        drifted += 1
        if dry_run:
            if len(results) < 50:
                results.append({"gid": gid, "variants": len(variants), "would_update": True})
            continue
        if updated >= max_cards:
            break

        try:
            # Keep the CURRENT default fixed. Changing which variant is default makes the
            # per-variant upsert transiently violate "exactly 1 default" -> 422. So we read the
            # current default from ggsel, set base = its live price (its modifier stays 0), and
            # give every other variant a +/- modifier around it (ggsel supports decrease).
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
            if default_vid not in by_vid:
                default_vid = min(variants, key=lambda v: float(v.live)).ggsel_variant_id
            base = float(by_vid[default_vid].live)

            ordered = sorted(variants, key=lambda v: _variant_sort_key(_P(v.age, v.flyable, v.rideable)))
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
                for v in variants:
                    await db.execute(
                        sql_update(SkuVariant)
                        .where(SkuVariant.ggsel_variant_id == v.ggsel_variant_id)
                        .values(price_rub=float(v.live))
                    )
                await db.commit()
            updated += 1
            results.append({"gid": gid, "base": round(base, 2), "variants": len(payload)})
        except Exception as e:
            errors += 1
            print(f"[SkuPriceSync] gid={gid} update failed: {type(e).__name__}: {e}", flush=True)
            results.append({"gid": gid, "error": f"{type(e).__name__}: {e}"})
        await asyncio.sleep(0.3)

    summary = {"cards_checked": checked, "drifted": drifted, "updated": updated,
               "errors": errors, "dry_run": dry_run, "sample": results[:30]}
    print(f"[SkuPriceSync] {summary}", flush=True)
    return summary
