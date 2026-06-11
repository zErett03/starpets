from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus
from app.clients.ggsel import ggsel_office
from app.config import settings


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

        resp = await ggsel_office.create_offer(
            title_ru=offer.name,
            title_en=offer.name,
            description_ru=desc_ru,
            description_en=desc_en,
            category_id=settings.starpets_category_id,
            cover_base64="",
            price=float(offer.price_rub or 0),
        )

        offer.ggsel_offer_id = resp.get("id") or resp.get("offer_id")
        offer.status = OfferStatus.draft
        await db.commit()
