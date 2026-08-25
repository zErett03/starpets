"""Выгрузка себестоимости по всему каталогу, который мы парсим.

Цены БЕЗ наценки: это то, во что товар обходится нам на StarPets прямо сейчас, а не то,
за сколько он стоит на витрине. Берётся минимальная цена среди СВОБОДНЫХ лотов — по
зарезервированным купить нельзя, и включать их в прайс значит обещать цену, которой нет.

Данные тянутся живьём, а не из кэша store_items: кэш наполняется событийной лентой и
покрывает не весь каталог, а выгрузка обещает полноту. Один проход по каталогу плюс один
по страницам лотов — это дешевле, чем спрашивать цену по каждому товару отдельно.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.clients.starpets import starpets
from app.db import AsyncSessionLocal
from app.db.models import Offer, OfferStatus
from app.fx import get_usd_rub

router = APIRouter()

GAME = "Adopt Me"


async def collect_prices(free_only: bool = True) -> tuple[list[dict], float]:
    """[{product_id, name, ..., price_usd, price_rub, lots}] + курс. Живые данные."""
    products = await starpets.get_all_products()
    fx = await get_usd_rub()

    # Минимум по свободным лотам + счётчик предложений: одна цена без объёма мало
    # говорит — позиция с единственным лотом ведёт себя иначе, чем позиция с сотней.
    best: dict = {}
    lots: dict = {}
    async for page in starpets.iter_items():
        for item in page:
            pid = item.get("productId")
            if not pid:
                continue
            if free_only and int(item.get("reserveLevel") or 0) != 0:
                continue
            price = float(item.get("price_usd") or 0)
            if price <= 0:
                continue
            lots[pid] = lots.get(pid, 0) + 1
            if pid not in best or price < best[pid]:
                best[pid] = price

    # Статус карточки: выгрузку смотрят вместе с витриной, и «товар есть, а карточки нет»
    # — самая частая причина вопросов к прайсу.
    async with AsyncSessionLocal() as db:
        cards = {
            int(pid): (gid, st.value if hasattr(st, "value") else str(st))
            for pid, gid, st in (await db.execute(
                select(Offer.starpets_product_id, Offer.ggsel_offer_id, Offer.status)
                .where(Offer.starpets_product_id.isnot(None))
            )).all() if pid is not None
        }

    rows = []
    for p in products:
        pid = p.get("id")
        price_usd = best.get(pid)
        gid, status = cards.get(int(pid), (None, "нет карточки")) if pid is not None else (None, "нет карточки")
        rows.append({
            "game": GAME,
            "product_id": pid,
            "name": p.get("name") or "",
            "rare": p.get("rare") or p.get("rarity") or "",
            "type": p.get("type") or p.get("item_type") or "",
            "subtype": p.get("subtype") or "",
            "age": p.get("age") or "",
            "flyable": bool(p.get("flyable", False)),
            "rideable": bool(p.get("rideable", False)),
            "chroma": bool(p.get("chroma", False)),
            "price_usd": round(price_usd, 4) if price_usd else "",
            "price_rub": round(price_usd * fx, 2) if price_usd else "",
            "lots_free": lots.get(pid, 0),
            "ggsel_offer_id": gid or "",
            "card_status": status,
        })
    rows.sort(key=lambda r: (r["price_usd"] == "", -(r["price_usd"] or 0)))
    return rows, fx


@router.get("/price-export", response_class=PlainTextResponse)
async def price_export(fmt: str = "csv", free_only: bool = True):
    """Прайс по всему каталогу: себестоимость в USD и RUB, без наценки.
    ?fmt=json — тот же набор в JSON. ?free_only=false — считать и резерв (обычно не нужно)."""
    rows, fx = await collect_prices(free_only=free_only)
    if fmt == "json":
        return PlainTextResponse(
            __import__("json").dumps({"game": GAME, "fx": fx, "rows": rows}, ensure_ascii=False),
            media_type="application/json")

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["game"],
                       delimiter=";", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    # BOM — иначе Excel открывает кириллицу кракозябрами, а файл идёт людям, не в скрипт.
    return PlainTextResponse(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="prices-am-{stamp}.csv"',
                 "X-FX-Rate": str(fx)},
    )
