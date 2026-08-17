"""
Сторож цен: ловит карточки, которые продаются ДЕШЕВЛЕ, чем стоит сам предмет.

Зачем. Цена карточки живёт в двух местах — у нас в базе и на витрине ggsel. Если
проталкивание цены подвисает, витрина остаётся со старой ценой, а precheck и профит-гард
сверяются с нашей (уже обновлённой) — и пропускают заказ, который по факту убыточен.
Так «Ornament» простоял по 104 ₽ при себестоимости до 193 ₽ и собрал два десятка
убыточных продаж подряд, а «Evil Chickatrice» висел за 168 ₽ при цене предмета 705 ₽.

Заметить это глазами нельзя: карточек тысячи. Сторож считает разрыв по кэшу цен
(store_items, только СВОБОДНЫЕ позиции) и зовёт оператора, когда он становится опасным.

Шумит минимально: алерт уходит, только если состав проблемных позиций изменился —
иначе одна и та же карточка слала бы сообщение каждый час.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import KVState, Offer, OfferStatus, SkuVariant, StoreItem
from app.fx import get_usd_rub

_STATE_KEY = "price_watch:seen"


async def _load_seen(db) -> set[str]:
    row = (await db.execute(select(KVState).where(KVState.key == _STATE_KEY))).scalar_one_or_none()
    if not row or not row.value:
        return set()
    return {p for p in row.value.split(",") if p}


async def _save_seen(db, keys: set[str]) -> None:
    # Ограничиваем длину: список ключей может разрастись, а нам важен только факт
    # «об этой позиции уже сообщали».
    value = ",".join(sorted(keys))[:6000]
    row = (await db.execute(select(KVState).where(KVState.key == _STATE_KEY))).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(KVState(key=_STATE_KEY, value=value))


async def find_underpriced(min_gap_rub: float, min_ratio: float) -> list[dict]:
    """Позиции, где себестоимость выше цены продажи. Только активные карточки."""
    fx = await get_usd_rub()
    async with AsyncSessionLocal() as db:
        floors = dict((await db.execute(
            select(StoreItem.product_id, func.min(StoreItem.price_usd))
            .where(StoreItem.reserve_level == 0, StoreItem.price_usd > 0)
            .group_by(StoreItem.product_id)
        )).all())
        offers = (await db.execute(
            select(Offer).where(
                Offer.status == OfferStatus.active,
                Offer.ggsel_offer_id.isnot(None),
                Offer.starpets_product_id.isnot(None),
            )
        )).scalars().all()
        variants = (await db.execute(
            select(SkuVariant).where(SkuVariant.hidden.is_(False))
        )).scalars().all()
        sku_names = dict((await db.execute(
            select(Offer.ggsel_offer_id, Offer.name)
            .where(Offer.age == "__sku__", Offer.status == OfferStatus.active)
        )).all())

    found: list[dict] = []

    def _check(key: str, name: str, gid, price_rub: float, product_id: int) -> None:
        floor_usd = floors.get(product_id)
        if floor_usd is None or price_rub <= 0:
            return
        cost_rub = float(floor_usd) * fx
        gap = round(cost_rub - price_rub, 2)
        ratio = cost_rub / price_rub
        # Порог по РАЗРЫВУ и по КРАТНОСТИ: копеечное отставание в пару рублей —
        # обычный лаг между прогонами ценника, будить из-за него оператора незачем.
        if gap < min_gap_rub and ratio < min_ratio:
            return
        found.append({"key": key, "name": name, "ggsel_offer_id": gid,
                      "price_rub": round(price_rub, 2), "cost_rub": round(cost_rub, 2),
                      "gap_rub": gap, "ratio": round(ratio, 2)})

    for o in offers:
        _check(f"o{o.id}", o.name, o.ggsel_offer_id,
               float(o.price_rub or 0), o.starpets_product_id)
    for v in variants:
        if v.ggsel_offer_id not in sku_names:
            continue
        _check(f"v{v.id}", f"{sku_names.get(v.ggsel_offer_id, '?')} · {v.label or ''}".strip(),
               v.ggsel_offer_id, float(v.price_rub or 0), v.starpets_product_id)

    found.sort(key=lambda r: r["gap_rub"], reverse=True)
    return found


async def price_watch() -> dict:
    """Один проход сторожа. Возвращает сводку; при новых находках шлёт алерт в Telegram."""
    min_gap = settings.price_watch_min_gap_rub
    min_ratio = settings.price_watch_min_ratio
    found = await find_underpriced(min_gap, min_ratio)

    async with AsyncSessionLocal() as db:
        seen = await _load_seen(db)
        keys = {r["key"] for r in found}
        fresh = [r for r in found if r["key"] not in seen]
        await _save_seen(db, keys)      # ушедшие с радара забываем: вернутся — предупредим снова
        await db.commit()

    print(f"[PriceWatch] найдено {len(found)} (новых {len(fresh)}) "
          f"порог {min_gap}₽/{min_ratio}x", flush=True)

    if fresh:
        worst = fresh[0]
        lines = [f"⚠️ <b>Цена ниже себестоимости — {len(fresh)} шт</b>",
                 f"Худшая: {worst['name'][:60]}",
                 f"продаём {worst['price_rub']}₽ · стоит {worst['cost_rub']}₽ "
                 f"(×{worst['ratio']}, минус {worst['gap_rub']}₽)"]
        if len(fresh) > 1:
            for r in fresh[1:4]:
                lines.append(f"• {r['name'][:50]} — {r['price_rub']}₽ vs {r['cost_rub']}₽")
            if len(fresh) > 4:
                lines.append(f"…и ещё {len(fresh) - 4}")
        lines.append("Проверь /underpriced-offers, затем sync-prices / floor-sweep")
        try:
            from app.telegram.bot import _orders_chats, send_message
            for chat in _orders_chats():
                await send_message(chat, "\n".join(lines))
        except Exception as e:  # noqa: BLE001 — сбой алерта не должен ронять задание
            print(f"[PriceWatch] алерт не отправлен: {e}", flush=True)

    return {"found": len(found), "new": len(fresh), "items": found[:20]}
