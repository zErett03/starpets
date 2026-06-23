import base64
import traceback

import httpx
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus
from app.clients.ggsel import ggsel_office
from app.config import settings

# Rarity → ggsel category id (for Pets)
_PET_CATEGORY: dict[str, int] = {
    "common":     122921,
    "uncommon":   122930,
    "rare":       122934,
    "ultra_rare": 122935,
    "legendary":  122939,
}

# item_type → ggsel category id (non-pet types)
_TYPE_CATEGORY: dict[str, int] = {
    "egg":       131181,
    "potion":    187200,
    "transport": 127105,
    "vehicle":   127105,
}


def _normalize(value: str | None) -> str:
    return (value or "").lower().replace("-", "_").replace(" ", "_").strip()


def _resolve_category(item_type: str | None, rare: str | None) -> int | None:
    t = _normalize(item_type)
    r = _normalize(rare)

    if t in ("pet", ""):
        return _PET_CATEGORY.get(r)

    return _TYPE_CATEGORY.get(t)


def _build_description(offer: Offer) -> tuple[str, str, str, str]:
    parts_ru = [f"Питомец: {offer.name}"]
    parts_en = [f"Pet: {offer.name}"]

    if offer.rare:
        parts_ru.append(f"Редкость: {offer.rare}")
        parts_en.append(f"Rarity: {offer.rare}")
    if offer.item_type:
        parts_ru.append(f"Тип: {offer.item_type}")
        parts_en.append(f"Type: {offer.item_type}")
    if offer.flyable:
        parts_ru.append("Летает: да")
        parts_en.append("Flyable: yes")
    if offer.rideable:
        parts_ru.append("Ездовой: да")
        parts_en.append("Rideable: yes")
    if offer.age:
        parts_ru.append(f"Возраст: {offer.age}")
        parts_en.append(f"Age: {offer.age}")

    desc_ru = "\n".join(parts_ru)
    desc_en = "\n".join(parts_en)
    instructions_ru = "ВАЖНО: После оплаты у вас будет 5 минут для получения предмета. Шаги: 1) Откройте страницу после оплаты — там появится имя бота. 2) Добавьте бота в друзья на Roblox. 3) Зайдите в Adopt Me. 4) Найдите бота в друзьях, телепортируйтесь к нему. 5) Примите трейд. Будьте готовы ПЕРЕД покупкой: откройте Roblox и Adopt Me заранее."
    instructions_en = "IMPORTANT: You have 5 minutes after payment to receive the item. Steps: 1) Open the page after payment — the bot name will appear. 2) Add the bot as a friend on Roblox. 3) Join Adopt Me. 4) Find the bot in friends, teleport to it. 5) Accept the trade. Be ready BEFORE purchase: open Roblox and Adopt Me in advance."
    return desc_ru, desc_en, instructions_ru, instructions_en


async def _download_image_b64(url: str) -> tuple[str, str]:
    """Returns (base64_data, mime_type)."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return base64.b64encode(resp.content).decode(), content_type


async def create_offer(offer_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Offer).where(Offer.id == offer_id))
        offer = result.scalar_one_or_none()
        if not offer:
            raise ValueError(f"Offer {offer_id} not found")

        category_id = _resolve_category(offer.item_type, offer.rare)
        if category_id is None:
            offer.status = OfferStatus.error
            offer.last_error = f"no_category: type={offer.item_type!r} rare={offer.rare!r}"
            await db.commit()
            print(f"[OfferCreator] offer_id={offer_id} skipped — {offer.last_error}", flush=True)
            return

        desc_ru, desc_en, instructions_ru, instructions_en = _build_description(offer)
        price = float(offer.price_rub or 0)

        cover_b64 = ""
        cover_mime = "image/jpeg"
        if offer.image_uri:
            try:
                cover_b64, cover_mime = await _download_image_b64(offer.image_uri)
                print(f"[OfferCreator] offer_id={offer_id} cover downloaded mime={cover_mime} size={len(cover_b64)}", flush=True)
            except Exception as e:
                print(f"[OfferCreator] offer_id={offer_id} cover download failed url={offer.image_uri!r}: {e}", flush=True)
        else:
            print(f"[OfferCreator] offer_id={offer_id} image_uri is empty", flush=True)

        try:
            # 1. Create offer on ggsel
            resp = await ggsel_office.create_offer(
                title_ru=offer.name,
                title_en=offer.name,
                description_ru=desc_ru,
                description_en=desc_en,
                instructions_ru=instructions_ru,
                instructions_en=instructions_en,
                category_id=category_id,
                cover_base64=cover_b64,
                cover_mime=cover_mime,
                price=price,
            )
            ggsel_offer_id = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
            if not ggsel_offer_id:
                raise ValueError(f"No offer id in response: {resp}")

            # 2. PATCH price
            await ggsel_office.update_price(ggsel_offer_id, price)

            # 3. Add Roblox Username option
            await ggsel_office.create_option(ggsel_offer_id)

            # 4. Set webhook URLs
            await ggsel_office.patch_offer(
                offer_id=ggsel_offer_id,
                precheck_url=f"{settings.public_url}/hooks/ggsel/precheck/{ggsel_offer_id}",
                notification_url=f"{settings.public_url}/hooks/ggsel/notification/{ggsel_offer_id}?secret={settings.webhook_shared_secret}",
            )

            offer.ggsel_offer_id = ggsel_offer_id
            offer.status = OfferStatus.draft
            offer.last_error = None
            print(
                f"[OfferCreator] offer_id={offer_id} ggsel_offer_id={ggsel_offer_id} "
                f"category={category_id} draft",
                flush=True,
            )

        except Exception as e:
            offer.status = OfferStatus.error
            offer.last_error = f"{e}\n{traceback.format_exc()}"
            await db.commit()
            raise

        await db.commit()
