import traceback

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus
from app.clients.ggsel import ggsel_office

GGSEL_CATEGORY_ID = 122916


def _build_description(offer: Offer) -> tuple[str, str]:
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

    return "\n".join(parts_ru), "\n".join(parts_en)


async def create_offer(offer_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Offer).where(Offer.id == offer_id))
        offer = result.scalar_one_or_none()
        if not offer:
            raise ValueError(f"Offer {offer_id} not found")

        desc_ru, desc_en = _build_description(offer)
        price = float(offer.price_rub or 0)

        try:
            # 1. Create offer on ggsel
            resp = await ggsel_office.create_offer(
                title_ru=offer.name,
                title_en=offer.name,
                description_ru=desc_ru,
                description_en=desc_en,
                category_id=GGSEL_CATEGORY_ID,
                cover_base64="",
                price=price,
            )
            ggsel_offer_id = resp.get("id") or resp.get("offer_id")
            if not ggsel_offer_id:
                raise ValueError(f"No offer id in response: {resp}")

            # 2. PATCH price
            await ggsel_office.update_price(ggsel_offer_id, price)

            # 3. Add Roblox Username option
            await ggsel_office.create_option(ggsel_offer_id)

            # 4. Activate
            await ggsel_office.activate_offer(ggsel_offer_id)

            offer.ggsel_offer_id = ggsel_offer_id
            offer.status = OfferStatus.active
            offer.last_error = None
            print(f"[OfferCreator] offer_id={offer_id} ggsel_offer_id={ggsel_offer_id} activated", flush=True)

        except Exception as e:
            offer.status = OfferStatus.error
            offer.last_error = f"{e}\n{traceback.format_exc()}"
            await db.commit()
            raise

        await db.commit()
