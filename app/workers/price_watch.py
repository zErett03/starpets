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


async def find_underpriced(min_gap_rub: float, min_ratio: float,
                           live_top: int = 0) -> tuple[list[dict], dict]:
    """Позиции, где себестоимость выше цены продажи. Только активные карточки.

    Возвращает (находки, охват). Охват важен не меньше находок: цены берутся из кэша
    store_items, и если предмета там нет, позиция ПРОПУСКАЕТСЯ. Пустой результат при
    большом `no_floor` означает не «всё хорошо», а «сравнивать было не с чем».

    live_top>0 — дополнительно проверить N самых дорогих карточек без цены в кэше
    напрямую через API StarPets: там, где кэш молчит, ошибка стоит дороже всего.
    """
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
    stats = {"checked": 0, "no_floor": 0, "live_checked": 0}
    no_floor_offers: list = []          # кандидаты на живую проверку через API

    def _check(key: str, name: str, gid, price_rub: float, product_id: int,
               floor_override: float | None = None) -> None:
        floor_usd = floor_override if floor_override is not None else floors.get(product_id)
        if price_rub <= 0:
            return
        if floor_usd is None:
            stats["no_floor"] += 1
            no_floor_offers.append((key, name, gid, price_rub, product_id))
            return
        stats["checked"] += 1
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

    # Живая проверка дорогих позиций, по которым кэш пуст. Берём самые дорогие: ошибка
    # в карточке за 3000 ₽ стоит на порядок больше, чем в карточке за 100 ₽, а запросов
    # к StarPets тратим ровно столько, сколько разрешено параметром.
    if live_top > 0 and no_floor_offers:
        import httpx
        from app.clients.starpets import starpets
        no_floor_offers.sort(key=lambda t: t[3], reverse=True)
        async with httpx.AsyncClient(timeout=10) as http:
            for key, name, gid, price_rub, product_id in no_floor_offers[:live_top]:
                try:
                    top = await starpets.get_top_item(http, str(product_id))
                except Exception:  # noqa: BLE001 — недоступность API не должна ронять проход
                    continue
                if not top:
                    continue
                stats["live_checked"] += 1
                _check(key, name, gid, price_rub, product_id,
                       floor_override=float(top.get("price_usd") or 0) or None)

    found.sort(key=lambda r: r["gap_rub"], reverse=True)
    return found, stats


async def price_watch() -> dict:
    """Один проход сторожа. Возвращает сводку; при новых находках шлёт алерт в Telegram."""
    min_gap = settings.price_watch_min_gap_rub
    min_ratio = settings.price_watch_min_ratio
    found, stats = await find_underpriced(min_gap, min_ratio,
                                          live_top=settings.price_watch_live_top)

    async with AsyncSessionLocal() as db:
        seen = await _load_seen(db)
        keys = {r["key"] for r in found}
        fresh = [r for r in found if r["key"] not in seen]
        await _save_seen(db, keys)      # ушедшие с радара забываем: вернутся — предупредим снова
        await db.commit()

    print(f"[PriceWatch] найдено {len(found)} (новых {len(fresh)}) "
          f"порог {min_gap}₽/{min_ratio}x · проверено {stats['checked']}, "
          f"без цены в кэше {stats['no_floor']} (из них живьём {stats['live_checked']})",
          flush=True)
    # Пустой кэш — не «всё хорошо», а слепая зона: предупреждаем отдельно.
    if stats["no_floor"] > stats["checked"]:
        print(f"[PriceWatch] ВНИМАНИЕ: у большинства карточек нет цены в store_items — "
              f"проверка почти не работает, посмотри ленту обновлений", flush=True)

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

    return {"found": len(found), "new": len(fresh), "coverage": stats, "items": found[:20]}
