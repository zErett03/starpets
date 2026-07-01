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


_INSTRUCTIONS_RU = '⚠️ После оплаты у тебя будет ~10 минут на принятие трейда у бота. Запускай игру до оплаты заказа ⚠️\n\n🌸Инструкция по получению:\n\n1. После оплаты откроется страница с заказом и инструкцией\n2. Добавь бота в друзья на сайте Roblox (запрос примется через ~1 минуту)\n3. Обнови страницу профиля. Нажми на кнопку "Join"\n4. После подключения к серверу с ботом, найди его в списке друзей и телепортируйся к нему\n5. Нажми на бота → кнопка Trade (если занят — дождись своей очереди)\n6. Бот примет запрос на трейд и добавит предмет в течение ~1 минуты\n7. Прими трейд\n8. Готово! Проверь свой инвентарь ;)\n_____________________________________________\n\n⚠️ Не пытайтесь телепортироваться к боту до нажатия кнопки Join на странице профиля бота\n⚠️ Если бот не принял запрос в друзья в течение ~5-ти минут — дождитесь окончания и перезапуска таймера, после чего повторите попытку\n⚠️ Если указали неверный логин при оформлении — обратитесь в чат поддержки на странице заказа, мы поможем :)\n⚠️ Если таймер на странице заказа истёк — дождитесь его обновления и повторите весь процесс сначала\n\n🌸Если у тебя возникли вопросы или столкнулись с проблемами — обращайся в чат поддержки на странице с заказом, мы всегда готовы Вам помочь! 🤗'

_INSTRUCTIONS_EN = '⚠️ After payment you\'ll have ~10 minutes to accept the trade from the bot. Launch the game BEFORE paying for the order ⚠️\n\n🌸 How to receive your item:\n\n1. After payment, the order page with instructions will open\n2. Add the bot as a friend on the Roblox website (the request is accepted in ~1 minute)\n3. Refresh the profile page. Click the "Join" button\n4. Once you\'ve connected to the bot\'s server, find it in your friends list and teleport to it\n5. Click on the bot → "Trade" button (if it\'s busy, wait for your turn)\n6. The bot will accept the trade request and add the item within ~1 minute\n7. Accept the trade\n8. Done! Check your inventory ;)\n_____________________________________________\n\n⚠️ Do not try to teleport to the bot before clicking the "Join" button on the bot\'s profile page\n⚠️ If the bot hasn\'t accepted your friend request within ~5 minutes, wait for the timer to finish and restart, then try again\n⚠️ If you entered the wrong login at checkout, contact the support chat on your order page and we\'ll help :)\n⚠️ If the timer on the order page has expired, wait for it to refresh and repeat the whole process from the start\n\n🌸 If you have any questions or run into any problems, reach out to the support chat on your order page — we\'re always glad to help! 🤗'


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

    # Put the receiving instructions into the DESCRIPTION too (shown BEFORE purchase),
    # not only in instructions (shown after payment), so buyers see the process upfront.
    desc_ru = "\n".join(parts_ru) + "\n\n" + _INSTRUCTIONS_RU
    desc_en = "\n".join(parts_en) + "\n\n" + _INSTRUCTIONS_EN
    instructions_ru = _INSTRUCTIONS_RU
    instructions_en = _INSTRUCTIONS_EN
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
                precheck_url=f"{settings.public_url}/hooks/ggsel/precheck/{ggsel_offer_id}?secret={settings.webhook_shared_secret}",
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
