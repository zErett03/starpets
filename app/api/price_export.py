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
from app.db.models import KVState, Offer, OfferStatus
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
    pages = seen = 0
    async for page in starpets.iter_items():
        pages += 1
        seen += len(page)
        # Прогресс в лог: проход идёт минутами, и без него снаружи не отличить «медленно
        # листает» от «повис на запросе». Раз в 50 страниц — не засоряя логи.
        if pages % 50 == 0:
            print(f"[PriceExport] страниц {pages}, лотов {seen}, товаров с ценой {len(best)}",
                  flush=True)
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


_CSV_KEY = "price_export:csv"
_META_KEY = "price_export:meta"
_DEADLINE_SEC = 1200        # 20 мин; дольше — значит проход повис, а не идёт медленно


async def _save_result(csv_text: str, rows: int, fx: float) -> None:
    """Готовую выгрузку кладём в базу. Память процесса её не переживает: Railway
    перезапускает контейнер когда захочет, и на 14-й минуте сборки это особенно обидно."""
    meta = f"{rows}|{fx}|{datetime.now(timezone.utc).isoformat()}"
    async with AsyncSessionLocal() as db:
        for key, val in ((_CSV_KEY, csv_text), (_META_KEY, meta)):
            row = (await db.execute(select(KVState).where(KVState.key == key))).scalar_one_or_none()
            if row:
                row.value = val
            else:
                db.add(KVState(key=key, value=val))
        await db.commit()


async def _load_result() -> bool:
    """Поднять последнюю выгрузку из базы в память. True — если что-то нашлось."""
    async with AsyncSessionLocal() as db:
        csv_row = (await db.execute(select(KVState).where(KVState.key == _CSV_KEY))).scalar_one_or_none()
        meta_row = (await db.execute(select(KVState).where(KVState.key == _META_KEY))).scalar_one_or_none()
    if not (csv_row and csv_row.value):
        return False
    rows, fx, finished = 0, None, None
    if meta_row and meta_row.value:
        parts = meta_row.value.split("|")
        try:
            rows, fx = int(parts[0]), float(parts[1])
            finished = datetime.fromisoformat(parts[2])
        except (ValueError, IndexError):
            pass
    _JOB.update(state="done", csv=csv_row.value, rows=rows, fx=fx, finished=finished)
    return True


async def _build_job(free_only: bool) -> None:
    import asyncio

    _JOB.update(state="running", started=datetime.now(timezone.utc), error=None)
    try:
        rows, fx = await asyncio.wait_for(collect_prices(free_only=free_only),
                                          timeout=_DEADLINE_SEC)
        csv_text = _to_csv(rows)
        _JOB.update(state="done", csv=csv_text, rows=len(rows), fx=fx,
                    finished=datetime.now(timezone.utc))
        await _save_result(csv_text, len(rows), fx)
        print(f"[PriceExport] готово: {len(rows)} строк, курс {fx}", flush=True)
    except asyncio.TimeoutError:
        _JOB.update(state="error", error=f"проход не уложился в {_DEADLINE_SEC // 60} мин",
                    finished=datetime.now(timezone.utc))
        print(f"[PriceExport] таймаут {_DEADLINE_SEC}с", flush=True)
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
    if _JOB["state"] == "idle" and not _JOB["csv"]:
        await _load_result()
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

    # После рестарта контейнера память пуста, но выгрузка могла остаться в базе —
    # поднимаем её, вместо того чтобы гонять сборку заново.
    if _JOB["state"] == "idle" and not _JOB["csv"]:
        await _load_result()

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


