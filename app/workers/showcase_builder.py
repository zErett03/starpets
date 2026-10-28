"""Витрина Roblox: карточка на каждый вариант питомца вместо карточки с меню.

Витрина не берёт карточку с меню «Вариант» (блок `[roblox]` один на карточку, а у
пунктов свои возраст, полёт и езда). Поэтому каждый вариант, который сейчас есть на
StarPets, получает свою карточку:

1. **Заведение.** Для каждого товара из SKU-групп (sku_variants), у которого есть
   устойчивый пол цены (`robust_floors_for`), берётся его строка offers — та самая
   поштучная карточка, которой сервис торговал до SKU. Есть карточка на площадке (их
   ~5.7 тыс. стоят на паузе) — ей переписываются название, описание с блоком и цена,
   она включается. Нет — заводится новая. Строка помечается `roblox_showcase`.
   Дальше её ведут обычные воркеры поштучных карточек: price_sync/reprice снимают
   её при пропаже товара и держат цену, precheck и выдача идут по
   `offers.starpets_product_id`, как до SKU.
2. **Возврат в продажу.** Поштучную карточку снимают при пропаже товара, а включать
   обратно до сих пор было некому. Здесь: карточка витрины на паузе, товар вернулся —
   цена по полу, включаем.
3. **Гашение SKU.** SKU-карточка гасится и помечается `showcase_retired`, когда ВСЕ её
   варианты в наличии стоят отдельными горящими карточками витрины. Иначе питомец на
   витрине дважды. Погашенную SKU-карточку синки SKU не пересобирают и не включают.

Всё ограничено бюджетом на прогон (`max_cards`) — темп GGSel режется по 429.
"""
from __future__ import annotations

import asyncio
import base64
import traceback
from collections import Counter

import httpx
from sqlalchemy import select, update as sql_update

from app.clients.ggsel import ggsel_office
from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus, SkuProduct, SkuVariant
from app.fx import calc_price_rub, get_usd_rub
from app.pricing import robust_floors_for
from app.showcase import describe, showcase_card, with_block
from app.workers.offer_creator import _INSTRUCTIONS_EN, _INSTRUCTIONS_RU, _resolve_category

_running = False
ACTIVATE_BATCH = 50


def _hooks(gid: int) -> tuple[str, str]:
    base, sec = settings.public_url, settings.webhook_shared_secret
    return (f"{base}/hooks/ggsel/precheck/{gid}?secret={sec}",
            f"{base}/hooks/ggsel/notification/{gid}?secret={sec}")


def _texts(offer: Offer, prod: SkuProduct, block: str) -> tuple[str, str]:
    head_ru, head_en = describe(offer.name, rare=offer.rare, pumping=prod.pumping,
                                age=offer.age, flyable=bool(offer.flyable),
                                rideable=bool(offer.rideable))
    desc_ru = with_block(f"{head_ru}\n\n{_INSTRUCTIONS_RU}", block)
    desc_en = with_block(f"{head_en}\n\n{_INSTRUCTIONS_EN}", block)
    return desc_ru, desc_en


async def _cover(prod: SkuProduct) -> str:
    """Обложка как у SKU-карточек (питомец на плашке редкости), base64 PNG."""
    from app.images.cover import make_cover
    pet = b""
    if prod.image_uri:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(prod.image_uri)
                if r.is_success:
                    pet = r.content
        except Exception as e:  # noqa: BLE001 — без картинки обложка всё равно соберётся
            print(f"[Showcase] обложка {prod.product_id}: {e}", flush=True)
    return base64.b64encode(make_cover(pet, prod.rare or "", prod.pumping or "default",
                                       name=prod.name)).decode()


async def _reuse(gid: int, title: str, desc_ru: str, desc_en: str, price: float) -> None:
    """Старая поштучная карточка → карточка витрины, без включения (оно пачкой).
    Два запроса: всё содержимое одним PATCH + проверка галочки согласия."""
    pre, notif = _hooks(gid)
    await ggsel_office.update_showcase(
        gid, title=title, description_ru=desc_ru, description_en=desc_en,
        instructions_ru=_INSTRUCTIONS_RU, instructions_en=_INSTRUCTIONS_EN,
        price=price, precheck_url=pre, notification_url=notif)
    if not await ggsel_office.has_consent_option(gid):
        await ggsel_office.create_consent_option(gid)


async def _create(offer: Offer, prod: SkuProduct, title: str, desc_ru: str, desc_en: str,
                  price: float) -> int:
    category_id = _resolve_category(offer.item_type, offer.rare)
    if category_id is None:
        raise ValueError(f"no_category type={offer.item_type!r} rare={offer.rare!r}")
    resp = await ggsel_office.create_offer(
        title_ru=title, title_en=title, description_ru=desc_ru, description_en=desc_en,
        instructions_ru=_INSTRUCTIONS_RU, instructions_en=_INSTRUCTIONS_EN,
        category_id=category_id, cover_base64=await _cover(prod), cover_mime="image/png",
        price=price,
    )
    gid = (resp.get("data") or {}).get("id") or resp.get("id") or resp.get("offer_id")
    if not gid:
        raise ValueError(f"нет id карточки в ответе: {resp}")
    gid = int(gid)
    try:
        await ggsel_office.create_option(gid)            # Roblox Username
        await ggsel_office.create_consent_option(gid)    # согласие
        pre, notif = _hooks(gid)
        await ggsel_office.patch_offer(offer_id=gid, precheck_url=pre, notification_url=notif)
    except Exception:
        # Полусобранную карточку не оставляем в продаже; id всё равно запишем —
        # повторный прогон дособерёт её через _reuse.
        try:
            await ggsel_office.pause_offer(gid)
        except Exception:  # noqa: BLE001
            pass
        async with AsyncSessionLocal() as db:
            await db.execute(sql_update(Offer).where(Offer.id == offer.id)
                             .values(ggsel_offer_id=gid, status=OfferStatus.paused))
            await db.commit()
        raise
    return gid


async def _plan(db):
    """Поштучные строки offers для товаров SKU-групп + их sku_products."""
    sku_pids = select(SkuVariant.starpets_product_id).distinct()
    rows = (await db.execute(
        select(Offer, SkuProduct)
        .join(SkuProduct, SkuProduct.product_id == Offer.starpets_product_id)
        .where(Offer.starpets_product_id.in_(sku_pids),
               Offer.age.is_distinct_from("__sku__"))
        .order_by(Offer.id)
    )).all()
    # На один товар бывает две строки offers (у ~20 товаров). Две карточки витрины на
    # один товар — дубль, поэтому берём одну: уже витринную, иначе с карточкой на
    # площадке, иначе первую.
    best: dict[int, tuple] = {}
    for offer, prod in rows:
        pid = int(offer.starpets_product_id)
        rank = (bool(offer.roblox_showcase), offer.ggsel_offer_id is not None)
        if pid not in best or rank > best[pid][0]:
            best[pid] = (rank, offer, prod)
    return [(o, p) for _r, o, p in best.values()]


async def showcase_build(max_cards: int = 60, dry_run: bool = True,
                         retire_sku: bool = True) -> dict:
    global _running
    if not dry_run:
        if _running:
            return {"skipped": "already running"}
        _running = True
    try:
        return await _build(max_cards, dry_run, retire_sku)
    finally:
        if not dry_run:
            _running = False


async def _build(max_cards: int, dry_run: bool, retire_sku: bool) -> dict:
    delivery = settings.showcase_delivery
    pause = settings.showcase_pause_sec
    fx = await get_usd_rub()
    async with AsyncSessionLocal() as db:
        rows = await _plan(db)
        floors = await robust_floors_for(db, [o.starpets_product_id for o, _ in rows])

    why: Counter = Counter()
    todo_new, todo_reuse, todo_relive = [], [], []
    for offer, prod in rows:
        floor = floors.get(int(offer.starpets_product_id))
        if offer.roblox_showcase:
            if floor is not None and offer.status != OfferStatus.active and offer.ggsel_offer_id:
                todo_relive.append((offer, float(floor)))
            continue
        if floor is None:
            why["нет в наличии — заведётся, когда появится"] += 1
            continue
        sc = showcase_card(offer.name, item_type=offer.item_type, rare=offer.rare,
                           pumping=prod.pumping, age=offer.age, flyable=bool(offer.flyable),
                           rideable=bool(offer.rideable), delivery=delivery)
        if not sc.ready:
            why[f"руками: {sc.manual}"] += 1
            continue
        item = (offer, prod, sc, float(floor))
        (todo_reuse if offer.ggsel_offer_id else todo_new).append(item)

    out = {"dry_run": dry_run, "to_reuse": len(todo_reuse), "to_create": len(todo_new),
           "to_relive": len(todo_relive), "skipped": dict(why.most_common()),
           "sample": [{"offer_id": o.id, "gid": o.ggsel_offer_id, "title": sc.title,
                       "block": sc.block, "price_rub": calc_price_rub(fl, settings.markup, fx)}
                      for o, _p, sc, fl in (todo_reuse + todo_new)[:5]]}
    if dry_run:
        out["retire"] = await _retire_sku(dry_run=True) if retire_sku else None
        return out

    done = Counter()
    budget = max_cards
    # Готовые карточки включаются пачкой (batch_activate), а в базу пишутся только после
    # включения: строка `active` = карточка правда горит.
    pending: list[tuple[int, int, dict]] = []   # (offer.id, gid, значения строки)

    async def flush() -> None:
        if not pending:
            return
        chunk = list(pending)
        pending.clear()
        try:
            await ggsel_office.batch_activate([g for _o, g, _v in chunk])
        except Exception as e:  # noqa: BLE001 — не включились: повтор следующим прогоном
            done["activate_failed"] += len(chunk)
            print(f"[Showcase] включение пачки {len(chunk)}: {type(e).__name__}: {e}", flush=True)
            async with AsyncSessionLocal() as db:
                for oid, _g, vals in chunk:
                    # Оформление уже на площадке; строку помечаем витринной, но на паузе —
                    # следующий прогон включит её веткой «вернуть в продажу».
                    await db.execute(sql_update(Offer).where(Offer.id == oid).values(
                        {**vals, "status": OfferStatus.paused}))
                await db.commit()
            return
        async with AsyncSessionLocal() as db:
            for oid, _g, vals in chunk:
                await db.execute(sql_update(Offer).where(Offer.id == oid).values(vals))
            await db.commit()
        done["activated"] += len(chunk)

    for offer, floor in todo_relive:
        if budget <= 0:
            break
        budget -= 1
        price = calc_price_rub(floor, settings.markup, fx)
        try:
            await ggsel_office.update_price(offer.ggsel_offer_id, price)
        except Exception as e:  # noqa: BLE001 — одна карточка не роняет прогон
            done["relive_failed"] += 1
            print(f"[Showcase] цена {offer.ggsel_offer_id}: {type(e).__name__}: {e}", flush=True)
            continue
        pending.append((offer.id, int(offer.ggsel_offer_id), dict(
            status=OfferStatus.active, price_usd=floor, price_rub=price)))
        done["relived"] += 1
        if len(pending) >= ACTIVATE_BATCH:
            await flush()
        await asyncio.sleep(pause)

    work = (todo_reuse + todo_new)[:max(0, budget)]
    gate = asyncio.Semaphore(max(1, settings.showcase_width))
    flush_lock = asyncio.Lock()

    async def flush_locked() -> None:
        async with flush_lock:
            await flush()

    async def one(offer, prod, sc, floor) -> None:
        async with gate:
            await _one(offer, prod, sc, floor)
        if len(pending) >= ACTIVATE_BATCH:
            await flush_locked()

    async def _one(offer, prod, sc, floor) -> None:
        price = calc_price_rub(floor, settings.markup, fx)
        desc_ru, desc_en = _texts(offer, prod, sc.block)
        gid = offer.ggsel_offer_id
        mode = "reused"
        try:
            if gid:
                try:
                    await _reuse(int(gid), sc.title, desc_ru, desc_en, price)
                except httpx.HTTPStatusError as e:
                    # Старой карточки на площадке больше нет (архив/удалена) — новая.
                    if e.response is None or e.response.status_code not in (404, 410, 422):
                        raise
                    print(f"[Showcase] {gid} не переиспользовать ({e.response.status_code}) — новая",
                          flush=True)
                    gid, mode = await _create(offer, prod, sc.title, desc_ru, desc_en, price), "created"
            else:
                gid, mode = await _create(offer, prod, sc.title, desc_ru, desc_en, price), "created"
        except Exception as e:  # noqa: BLE001
            done["failed"] += 1
            print(f"[Showcase] {offer.id} {sc.title!r}: {type(e).__name__}: {e}\n"
                  f"{traceback.format_exc()[-600:]}", flush=True)
            await asyncio.sleep(pause)
            return
        pending.append((offer.id, int(gid), dict(
            ggsel_offer_id=int(gid), status=OfferStatus.active, roblox_showcase=True,
            price_usd=floor, price_rub=price, last_error=None)))
        done[mode] += 1
        await asyncio.sleep(pause)

    # Несколько карточек сразу (SHOWCASE_WIDTH): ответ GGSel — около секунды, и по одной
    # адрес простаивает; 429 клиент повторяет сам.
    await asyncio.gather(*(one(*c) for c in work))
    await flush_locked()

    out["done"] = dict(done)
    if retire_sku:
        out["retire"] = await _retire_sku(dry_run=False)
    print(f"[Showcase] {out['done']} retire={out.get('retire')}", flush=True)
    return out


async def _retire_sku(dry_run: bool) -> dict:
    """Погасить SKU-карточки, у которых все варианты в наличии стоят отдельно."""
    async with AsyncSessionLocal() as db:
        cards = (await db.execute(
            select(Offer.id, Offer.ggsel_offer_id).where(
                Offer.age == "__sku__", Offer.ggsel_offer_id.isnot(None),
                Offer.showcase_retired.is_(False))
        )).all()
        variants = (await db.execute(
            select(SkuVariant.ggsel_offer_id, SkuVariant.starpets_product_id)
        )).all()
        shown = {int(p) for (p,) in (await db.execute(
            select(Offer.starpets_product_id).where(
                Offer.roblox_showcase.is_(True), Offer.status == OfferStatus.active,
                Offer.starpets_product_id.isnot(None))
        )).all()}
        pids_by_gid: dict[int, set[int]] = {}
        for gid, pid in variants:
            pids_by_gid.setdefault(int(gid), set()).add(int(pid))
        floors = await robust_floors_for(db, [p for s in pids_by_gid.values() for p in s])

    ready = []
    for oid, gid in cards:
        pids = pids_by_gid.get(int(gid), set())
        in_stock = {p for p in pids if floors.get(p) is not None}
        if not in_stock:
            # Сейчас ничего не продаёт: гасить рано — витринных карточек под неё ещё нет,
            # а когда товар вернётся, сначала встанут они (заведение идёт по наличию).
            continue
        if in_stock <= shown:
            ready.append((oid, int(gid)))
    if dry_run:
        return {"ready_to_retire": len(ready), "sku_cards": len(cards)}
    retired = 0
    for i in range(0, len(ready), 50):
        chunk = ready[i:i + 50]
        try:
            await ggsel_office.pause_offers([g for _o, g in chunk])
        except Exception as e:  # noqa: BLE001 — повтор следующим прогоном
            print(f"[Showcase] гашение SKU {len(chunk)} шт.: {type(e).__name__}: {e}", flush=True)
            continue
        async with AsyncSessionLocal() as db:
            await db.execute(sql_update(Offer).where(Offer.id.in_([o for o, _g in chunk])).values(
                status=OfferStatus.paused, showcase_retired=True))
            await db.commit()
        retired += len(chunk)
    return {"ready_to_retire": len(ready), "retired": retired, "sku_cards": len(cards)}
