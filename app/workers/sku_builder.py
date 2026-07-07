"""SKU-master card builder (Phase 2).

Groups sku_products by (name, pumping) into ONE ggsel card with a 'Вариант' radio option —
one variant per age × flyable × rideable combo (rarity is constant within a pet). Pricing is
additive: base = cheapest combo, each variant carries a +/- delta. Live per-combo prices come
from the existing `offers.price_rub` (kept fresh by the event-driven price sync), joined on
starpets_product_id. The composited rarity cover comes from app.images.cover.make_cover.

This is ADDITIVE — it does not touch the per-combo cards. Retiring those is Phase 5.
"""
import base64
import traceback

import httpx
from sqlalchemy import select, delete as sql_delete

from app.db import AsyncSessionLocal
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.db.models import Offer, OfferStatus, SkuProduct, SkuVariant
from app.clients.ggsel import ggsel_office
from app.config import settings
from app.images.cover import make_cover
from app.workers.offer_creator import _resolve_category, _INSTRUCTIONS_RU, _INSTRUCTIONS_EN

# pumping value -> label used in the title tag "Name | Rarity, Pumping".
_PUMPING_TITLE = {"default": "Default", "neon": "Neon", "mega_neon": "Mega Neon"}

# Age display order (youngest -> oldest). Default and neon stage names don't collide, so one
# map covers both; a card is single-pumping, so only one family's ranks ever apply.
_AGE_ORDER = {
    # default pet ages
    "newborn": 0, "junior": 1, "pre_teen": 2, "teen": 3, "post_teen": 4, "full_grown": 5,
    # neon stages
    "reborn": 0, "twinkle": 1, "sparkle": 2, "flare": 3, "sunshine": 4, "luminous": 5,
}


def _norm_age(age) -> str:
    return (age or "").strip().lower().replace(" ", "_").replace("-", "_")


def _variant_sort_key(p):
    """Group by age (youngest->oldest), then by fly/ride combo in the order:
    base -> ездовой(ride) -> летает(fly) -> ездовой·летает."""
    age_rank = _AGE_ORDER.get(_norm_age(p.age), 99)
    fly_ride = (2 if p.flyable else 0) + (1 if p.rideable else 0)
    return (age_rank, fly_ride)


def _variant_label(p: SkuProduct) -> str:
    """Age + fly/ride label for one combo (rarity is constant across a pet's variants)."""
    parts = []
    if p.age:
        parts.append(str(p.age).replace("_", " ").title())
    if p.flyable:
        parts.append("Летает")
    if p.rideable:
        parts.append("Ездовой")
    return " · ".join(parts) or "Стандарт"


def _card_title(name: str, rare: str, pumping: str) -> str:
    rarity = (rare or "").replace("_", " ").strip().title()          # legendary -> Legendary
    pump = _PUMPING_TITLE.get((pumping or "").lower(), "Default")
    tags = ", ".join(t for t in (rarity, pump) if t)
    return f"{name} | {tags}" if tags else name


def _card_description(name: str, rare: str, pumping: str) -> tuple[str, str]:
    suffix = _PUMPING_TITLE.get((pumping or "").lower())
    ru = [f"Питомец: {name}"]
    en = [f"Pet: {name}"]
    if rare:
        ru.append(f"Редкость: {rare}")
        en.append(f"Rarity: {rare}")
    if suffix:
        ru.append(f"Тип: {suffix}")
        en.append(f"Type: {suffix}")
    ru.append("Выберите нужный вариант (возраст / летает / ездовой) в поле «Вариант».")
    en.append("Choose the variant you need (age / flyable / rideable) in the \"Variant\" field.")
    desc_ru = "\n".join(ru) + "\n\n" + _INSTRUCTIONS_RU
    desc_en = "\n".join(en) + "\n\n" + _INSTRUCTIONS_EN
    return desc_ru, desc_en


async def build_sku_card(name: str, pumping: str, force: bool = False) -> dict:
    """Create ONE SKU card for (name, pumping). Returns a summary dict (never raises).
    Idempotent: if a card already exists for this group it is skipped unless force=True."""
    pumping = (pumping or "default").lower()

    # 1. Collect combos in this group and their live prices (from offers.price_rub).
    async with AsyncSessionLocal() as db:
        prods = (await db.execute(
            select(SkuProduct).where(
                SkuProduct.name == name,
                SkuProduct.pumping == pumping,
            )
        )).scalars().all()
        if not prods:
            return {"error": f"no sku_products for name={name!r} pumping={pumping!r}"}

        pids = [p.product_id for p in prods]

        # Idempotency: skip if this group already has a SKU card (variant rows point to it).
        if not force:
            existing = (await db.execute(
                select(SkuVariant.ggsel_offer_id)
                .where(SkuVariant.starpets_product_id.in_(pids)).limit(1)
            )).scalar_one_or_none()
            if existing is not None:
                return {"skipped": True, "reason": "already built",
                        "ggsel_offer_id": int(existing), "name": name, "pumping": pumping}

        price_rows = (await db.execute(
            select(Offer.starpets_product_id, Offer.price_rub).where(
                Offer.starpets_product_id.in_(pids),
                Offer.price_rub.isnot(None),
                Offer.price_rub > 0,
            )
        )).all()
    price_by_pid = {int(pid): float(pr) for (pid, pr) in price_rows}

    combos = []
    for p in prods:
        price = price_by_pid.get(p.product_id)
        if price is None:
            continue
        combos.append((p, price))
    if not combos:
        return {"error": f"no priced combos for name={name!r} pumping={pumping!r} "
                         f"(products={len(prods)}, none have a live offer price)"}
    # Pricing base = cheapest combo (card headline + additive deltas).
    base_p, base_price = min(combos, key=lambda cp: cp[1])
    # Display order = by age (youngest->oldest), then fly/ride combo.
    combos.sort(key=lambda cp: _variant_sort_key(cp[0]))

    rare = base_p.rare or ""
    category_id = _resolve_category(base_p.item_type, rare)
    if category_id is None:
        return {"error": f"no_category type={base_p.item_type!r} rare={rare!r}"}

    # 2. Composited rarity cover from the group's pet image.
    pet_bytes = b""
    img_uri = base_p.image_uri
    if img_uri:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(img_uri)
                if r.is_success:
                    pet_bytes = r.content
                else:
                    print(f"[SkuBuilder] image fetch {r.status_code} {img_uri}", flush=True)
        except Exception as e:
            print(f"[SkuBuilder] image fetch error: {e}", flush=True)
    cover_png = make_cover(pet_bytes, rare, pumping)
    cover_b64 = base64.b64encode(cover_png).decode()

    desc_ru, desc_en = _card_description(name, rare, pumping)
    title = _card_title(name, rare, pumping)

    # 3. Create the card, then username + consent + Вариант radio, then webhooks.
    try:
        resp = await ggsel_office.create_offer(
            title_ru=title, title_en=title,
            description_ru=desc_ru, description_en=desc_en,
            instructions_ru=_INSTRUCTIONS_RU, instructions_en=_INSTRUCTIONS_EN,
            category_id=category_id,
            cover_base64=cover_b64, cover_mime="image/png",
            price=round(base_price, 2),
        )
        gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
        if not gid:
            return {"error": f"no offer id in create_offer response: {resp}"}

        # Backing Offer row so the ggsel webhook (which looks up Offer by ggsel_offer_id and
        # uses offer.name for the order) resolves. It is multi-product, so starpets_product_id
        # stays NULL; the age="__sku__" sentinel keeps it off the per-combo composite key.
        async with AsyncSessionLocal() as db:
            stmt = pg_insert(Offer).values(
                name=title, item_type=base_p.item_type, rare=rare,
                flyable=False, rideable=False, age="__sku__",
                ggsel_offer_id=gid, status=OfferStatus.active,
                price_rub=round(base_price, 2), starpets_product_id=None,
                image_uri=base_p.image_uri,
            ).on_conflict_do_update(
                constraint="uq_offers_composite",
                set_={"ggsel_offer_id": gid, "status": OfferStatus.active,
                      "price_rub": round(base_price, 2), "image_uri": base_p.image_uri},
            )
            await db.execute(stmt)
            await db.commit()

        await ggsel_office.create_option(gid)          # Roblox Username (text, pos 1)
        await ggsel_office.create_consent_option(gid)   # consent checkbox (pos 2)
        option_id = await ggsel_office.create_radio_option(
            gid, title_ru="Вариант", title_en="Variant", position=3
        )

        # `combos` is in DISPLAY order (age/fly-ride); position=display index. But ggsel needs
        # the option's DEFAULT variant created FIRST (a required radio must always have a
        # default), so we create the cheapest one first, then the rest in display order —
        # display order is preserved by the explicit `position`.
        creation = sorted(
            enumerate(combos),
            key=lambda ip: 0 if ip[1][0].product_id == base_p.product_id else 1,
        )
        variants = []
        for pos, (p, price) in creation:
            delta = round(price - base_price, 2)
            label = _variant_label(p)
            vtitle = f"{label} — {int(round(price))}₽"
            is_def = (p.product_id == base_p.product_id)
            vid = await ggsel_office.add_variant(
                gid, option_id, title_ru=vtitle, title_en=label,
                price_delta=delta, is_default=is_def, position=pos,
            )
            async with AsyncSessionLocal() as db:
                db.add(SkuVariant(
                    ggsel_offer_id=gid, ggsel_option_id=option_id, ggsel_variant_id=vid,
                    starpets_product_id=p.product_id, label=label, price_rub=price,
                ))
                await db.commit()
            variants.append({"label": label, "variant_id": vid, "position": pos,
                             "product_id": p.product_id, "price_rub": price,
                             "delta": delta, "default": is_def})
        variants.sort(key=lambda v: v["position"])

        await ggsel_office.patch_offer(
            offer_id=gid,
            precheck_url=f"{settings.public_url}/hooks/ggsel/precheck/{gid}?secret={settings.webhook_shared_secret}",
            notification_url=f"{settings.public_url}/hooks/ggsel/notification/{gid}?secret={settings.webhook_shared_secret}",
        )
    except Exception as e:
        # Roll back partial state so a re-run rebuilds cleanly instead of skip-guarding a
        # half-built card: drop any SkuVariant rows created for this gid and pause the orphan.
        _gid = locals().get("gid")
        if _gid:
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(sql_delete(SkuVariant).where(SkuVariant.ggsel_offer_id == _gid))
                    await db.commit()
            except Exception as ce:
                print(f"[SkuBuilder] rollback variant rows failed gid={_gid}: {ce}", flush=True)
            try:
                await ggsel_office.pause_offer(_gid)
            except Exception as pe:
                print(f"[SkuBuilder] pause orphan card failed gid={_gid}: {pe}", flush=True)
        return {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}

    return {
        "ggsel_offer_id": gid, "title": title, "category_id": category_id,
        "option_id": option_id, "base_price": round(base_price, 2),
        "count": len(variants), "variants": variants,
    }
