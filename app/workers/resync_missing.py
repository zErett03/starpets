"""Rare (12h) re-sync of pets whose StarPets image is a 'NO IMAGE' placeholder.

Cards for art-less pets show the pet NAME as a clean interim fallback (sku_products.image_missing).
StarPets may add real art later; every 12h we refresh the catalog and re-generate the flagged
covers — if the art now exists, the real image replaces the name and the flag clears (regenerate
sets image_missing from the freshly-fetched image).
"""
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import SkuProduct, SkuVariant

_running = False


async def resync_missing_images(max_cards: int = 400, dry_run: bool = False) -> dict:
    global _running
    if not dry_run:
        if _running:
            return {"skipped": "already running"}
        _running = True
    try:
        async with AsyncSessionLocal() as db:
            gids = [int(g) for (g,) in (await db.execute(
                select(SkuVariant.ggsel_offer_id)
                .join(SkuProduct, SkuProduct.product_id == SkuVariant.starpets_product_id)
                .where(SkuProduct.image_missing.is_(True))
                .distinct()
            )).all()]
        gids = gids[:max_cards]

        if dry_run:
            return {"dry_run": True, "flagged_cards": len(gids), "sample": gids[:30]}
        if not gids:
            print("[ResyncMissing] no flagged cards", flush=True)
            return {"flagged_cards": 0}

        # lazy import (avoid api<->workers circular at module load)
        from app.api import _run_sync_sku_products, _run_regenerate_covers
        await _run_sync_sku_products()                 # refresh image_uris (StarPets may have added art)
        res = await _run_regenerate_covers(gids)       # re-fetch + composite real if now available; updates flag

        async with AsyncSessionLocal() as db:
            still = len((await db.execute(
                select(SkuProduct.product_id).where(SkuProduct.image_missing.is_(True))
            )).all())
        summary = {"flagged_cards": len(gids), "regenerated": res.get("count"),
                   "still_missing_products": still}
        print(f"[ResyncMissing] {summary}", flush=True)
        return summary
    finally:
        if not dry_run:
            _running = False
