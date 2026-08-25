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
            # Стадия прокачки: default / neon / mega_neon. Без неё прайс врёт на самом
            # дорогом: у неонового питомца возраст пустой, и в таблице он неотличим от
            # второго неона той же породы, хотя цены различаются вдвое.
            "pumping": p.get("pumping") or "default",
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


# Готовая выгрузка живёт в памяти процесса. Собирается она минуты — весь каталог плюс все
# страницы лотов, — и держать на это открытым HTTP-соединение бессмысленно: браузер отвалится
# по таймауту раньше, чем сервер закончит. Поэтому сборка идёт в фоне, а страница сразу
# говорит, готово или нет. Потеря при рестарте не страшна: пересобрать стоит одну кнопку.
_JOB: dict = {"state": "idle", "csv": "", "rows": 0, "fx": None,
              "started": None, "finished": None, "error": None}


async def _build_job(free_only: bool) -> None:
    _JOB.update(state="running", started=datetime.now(timezone.utc), error=None)
    try:
        rows, fx = await collect_prices(free_only=free_only)
        _JOB.update(state="done", csv=_to_csv(rows), rows=len(rows), fx=fx,
                    finished=datetime.now(timezone.utc))
        print(f"[PriceExport] готово: {len(rows)} строк, курс {fx}", flush=True)
    except Exception as e:  # noqa: BLE001
        _JOB.update(state="error", error=f"{type(e).__name__}: {e}",
                    finished=datetime.now(timezone.utc))
        print(f"[PriceExport] ошибка: {e}", flush=True)


def _to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["game"],
                       delimiter=";", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    # BOM — иначе Excel открывает кириллицу кракозябрами, а файл идёт людям, не в скрипт.
    return "﻿" + buf.getvalue()


def _age(dt) -> str:
    if not dt:
        return "—"
    sec = int((datetime.now(timezone.utc) - dt).total_seconds())
    return f"{sec // 60} мин {sec % 60} с назад" if sec >= 60 else f"{sec} с назад"


@router.get("/price-export/status")
async def price_export_status():
    """Состояние фоновой сборки: idle / running / done / error."""
    return {"state": _JOB["state"], "rows": _JOB["rows"], "fx": _JOB["fx"],
            "started": str(_JOB["started"] or ""), "finished": str(_JOB["finished"] or ""),
            "age": _age(_JOB["finished"]), "error": _JOB["error"]}


@router.get("/price-export", response_class=PlainTextResponse)
async def price_export(fmt: str = "csv", free_only: bool = True, refresh: bool = False):
    """Прайс по всему каталогу: себестоимость в USD и RUB, без наценки.

    Первый заход запускает сборку и отвечает сразу; когда она закончится, тот же адрес
    отдаст файл. ?refresh=true — пересобрать заново, ?fmt=json — JSON вместо CSV.
    """
    import asyncio

    if _JOB["state"] == "running":
        return PlainTextResponse(
            f"Выгрузка собирается, начата {_age(_JOB['started'])}.\n"
            f"Обнови страницу через минуту — файл скачается сам.",
            status_code=202, media_type="text/plain; charset=utf-8")

    if refresh or _JOB["state"] in ("idle", "error"):
        asyncio.create_task(_build_job(free_only))
        note = "Пересобираю выгрузку." if refresh else "Запустил сборку выгрузки."
        prev = (f"\nПрошлая версия ({_JOB['rows']} строк, {_age(_JOB['finished'])}) "
                f"останется доступна, пока не готова новая." if _JOB["csv"] and refresh else "")
        return PlainTextResponse(
            f"{note} Каталог большой, это занимает 1–3 минуты.\n"
            f"Обнови страницу — когда будет готово, начнётся скачивание.{prev}\n"
            f"Состояние: /price-export/status",
            status_code=202, media_type="text/plain; charset=utf-8")

    if fmt == "json":
        return PlainTextResponse(
            __import__("json").dumps({"game": GAME, "fx": _JOB["fx"], "rows": _JOB["rows"],
                                      "csv": _JOB["csv"]}, ensure_ascii=False),
            media_type="application/json")

    stamp = (_JOB["finished"] or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M")
    return PlainTextResponse(
        _JOB["csv"],
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="prices-am-{stamp}.csv"',
                 "X-FX-Rate": str(_JOB["fx"]), "X-Rows": str(_JOB["rows"])},
    )


