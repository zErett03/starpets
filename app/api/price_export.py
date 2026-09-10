"""Выгрузка себестоимости по всему каталогу, который мы парсим.

Цены БЕЗ наценки: это то, во что товар обходится нам на StarPets прямо сейчас, а не то,
за сколько он стоит на витрине. Берётся минимальная цена среди СВОБОДНЫХ лотов — по
зарезервированным купить нельзя, и включать их в прайс значит обещать цену, которой нет.

Цены берутся из кэша store_items, который наполняет событийная лента: это ноль обращений
к StarPets. Поштучный опрос всего каталога (`?live=true`) оставлен как исключение — это 42
тысячи запросов к `items/top`, и именно на них поставщик пожаловался как на спам.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.clients.starpets import starpets
from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import KVState, Offer, OfferStatus, SkuVariant
from app.fx import get_usd_rub

router = APIRouter()

GAME = "Adopt Me"


async def card_index() -> dict:
    """{product_id: (ggsel_offer_id, статус)} — где каждый товар представлен на витрине.

    Источников привязки ДВА, и это не дублирование. Одиночная карточка хранит товар в
    offers.starpets_product_id. SKU-карточка так не может: за ней стоят десятки товаров
    (возраст × прокачка × fly/ride), поэтому её собственный product_id пуст, а связь живёт
    в sku_variants. Если смотреть только в offers, вся витрина Adopt Me выглядит мёртвой:
    40 «активных» против 13 546 «на паузе» — это старые одиночные карточки, выключенные
    при переходе на SKU, а реальные продажи идут мимо той таблицы.
    """
    async with AsyncSessionLocal() as db:
        cards = {
            int(pid): (gid, st.value if hasattr(st, "value") else str(st))
            for pid, gid, st in (await db.execute(
                select(Offer.starpets_product_id, Offer.ggsel_offer_id, Offer.status)
                .where(Offer.starpets_product_id.isnot(None))
            )).all() if pid is not None
        }
        # Статус берём у карточки-владельца варианта; скрытый вариант отмечаем отдельно —
        # карточка активна, но конкретно этот товар с витрины убран.
        sku_rows = (await db.execute(
            select(SkuVariant.starpets_product_id, SkuVariant.ggsel_offer_id,
                   SkuVariant.hidden, Offer.status)
            .join(Offer, Offer.ggsel_offer_id == SkuVariant.ggsel_offer_id)
        )).all()
    for pid, gid, hidden, st in sku_rows:
        if pid is None:
            continue
        status = "вариант скрыт" if hidden else (st.value if hasattr(st, "value") else str(st))
        prev = cards.get(int(pid))
        # SKU-привязка приоритетнее: одиночная карточка того же товара, если она есть,
        # почти всегда — выключенный предшественник.
        if prev is None or prev[1] in ("paused", "draft", "pending_create"):
            cards[int(pid)] = (gid, status)
    return cards


async def collect_prices(free_only: bool = True, live: bool = False) -> tuple[list[dict], float]:
    """[{product_id, name, ..., price_usd, price_rub, lots}] + курс. Живые данные.

    Порядок простой: берём каталог игры, по каждому товару спрашиваем его лоты, берём
    минимальную цену среди свободных — она и есть себестоимость на сейчас.

    Цены спрашиваем ПОТОВАРНО (items/top/{product_id}), а не листая общую ленту items/all.
    Лента отдаёт страницы по 1000 и просит передавать курсором id последнего лота, но
    курсор на ней не сдвигается: проход 1150 раз получил одну и ту же первую тысячу и
    завис бы навсегда, если бы его не оборвала 500-я ошибка (счётчик показал «1,15 млн
    лотов» при реальных двадцати тысячах — это были повторы). Потоварный опрос дороже по
    числу запросов, зато ограничен нашим каталогом, идёт параллельно и переживает
    единичные сбои: упавший товар останется без цены, а не обнулит весь проход.
    """
    import asyncio

    import httpx

    products = await starpets.get_all_products()
    fx = await get_usd_rub()
    pids = [p.get("id") for p in products if p.get("id") is not None]

    # Минимум по свободным лотам + счётчик предложений: одна цена без объёма мало
    # говорит — позиция с единственным лотом ведёт себя иначе, чем позиция с сотней.
    # Счётчик упирается в потолок ответа API: items/top отдаёт максимум 100 экземпляров,
    # поэтому «100» в колонке означает «сто и больше». На минимальную цену это не влияет —
    # список приходит от самых дешёвых.
    best: dict = {}
    lots: dict = {}
    done = failed = 0
    sem = asyncio.Semaphore(max(4, settings.sync_concurrency))

    # ОСНОВНОЙ ИСТОЧНИК — кэш store_items, который наполняет событийная лента. Поштучный
    # опрос `items/top` по всему каталогу — это 42 тысячи запросов за прогон, и именно на
    # него пожаловался поставщик. Событийная модель ровно для того и нужна, чтобы цены
    # лежали у нас заранее, а не спрашивались по одной.
    #
    # live=True возвращает прежнее поведение (опросить каждый товар) — на случай, когда
    # выгрузка нужна максимально точной и есть разрешение на нагрузку.
    from sqlalchemy import func as _func
    from app.db.models import StoreItem
    async with AsyncSessionLocal() as db:
        cache_rows = (await db.execute(
            select(StoreItem.product_id, _func.min(StoreItem.price_usd), _func.count())
            .where(StoreItem.reserve_level == 0, StoreItem.price_usd > 0)
            .group_by(StoreItem.product_id)
        )).all()
    for pid, price, cnt in cache_rows:
        if pid is None or not price:
            continue
        best[int(pid)] = float(price)
        lots[int(pid)] = int(cnt)
    print(f"[PriceExport] из кэша store_items: {len(best)} товаров с ценой", flush=True)

    if not live:
        rows = _rows_from(products, best, lots, fx, await card_index(), cached=True)
        return rows, fx

    async def _one(http: httpx.AsyncClient, pid) -> None:
        nonlocal done, failed
        async with sem:
            for attempt in (1, 2, 3):
                try:
                    params = starpets._base_params()
                    from app.clients.sp_gate import sp_gate
                    async with sp_gate("items/top"):
                        resp = await http.get(
                            f"{starpets.base_url}/store/ex-buyers/items/top/{pid}",
                            headers=starpets._headers(starpets._sign(params)), params=params)
                    if resp.status_code >= 500 and attempt < 3:
                        await asyncio.sleep(0.5 * attempt)   # временная просадка — подождём
                        continue
                    if not resp.is_success:
                        failed += 1
                        return
                    items = resp.json().get("items") or []
                except Exception:  # noqa: BLE001
                    if attempt < 3:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    failed += 1
                    return
                for it in items:
                    if free_only and int(it.get("reserveLevel") or 0) != 0:
                        continue
                    price = float(it.get("price_usd") or 0)
                    if price <= 0:
                        continue
                    lots[pid] = lots.get(pid, 0) + 1
                    if pid not in best or price < best[pid]:
                        best[pid] = price
                done += 1
                return

    print(f"[PriceExport] старт: {len(pids)} товаров каталога", flush=True)
    limits = httpx.Limits(max_connections=settings.sync_concurrency + 5)
    async with httpx.AsyncClient(timeout=15, limits=limits) as http:
        for i in range(0, len(pids), 500):
            await asyncio.gather(*[_one(http, pid) for pid in pids[i:i + 500]])
            print(f"[PriceExport] {min(i + 500, len(pids))}/{len(pids)} · "
                  f"с ценой {len(best)} · без ответа {failed}", flush=True)

    # Статус карточки: выгрузку смотрят вместе с витриной, и «товар есть, а карточки нет»
    # — самая частая причина вопросов к прайсу.
    #
    # Источников привязки ДВА, и это не дублирование. Одиночная карточка хранит товар в
    # offers.starpets_product_id. SKU-карточка так не может: за ней стоят десятки товаров
    # (возраст × прокачка × fly/ride), поэтому её собственный product_id пуст, а связь
    # живёт в sku_variants. Если смотреть только в offers, вся витрина Adopt Me выглядит
    # мёртвой: 40 «активных» против 13 546 «на паузе» — это старые одиночные карточки,
    # выключенные при переходе на SKU, а реальные продажи идут мимо этой таблицы.
    cards = await card_index()
    return _rows_from(products, best, lots, fx, cards, cached=False), fx


def _rows_from(products, best: dict, lots: dict, fx: float, cards: dict,
               cached: bool) -> list[dict]:
    """Сборка строк прайса. Общая для обоих источников цены — кэша и живого опроса."""
    rows = []
    for p in products:
        pid = p.get("id")
        price_usd = best.get(int(pid)) if pid is not None else None
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
            # Из кэша число лотов честное (столько строк в store_items), при живом опросе
            # упирается в сотню — потолок ответа API.
            "lots_free": lots.get(int(pid), 0) if pid is not None else 0,
            "source": "кэш" if cached else "живой опрос",
            "ggsel_offer_id": gid or "",
            "card_status": status,
        })
    rows.sort(key=lambda r: (r["price_usd"] == "", -(r["price_usd"] or 0)))
    return rows


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


async def _build_job(free_only: bool, live: bool = False) -> None:
    import asyncio

    _JOB.update(state="running", started=datetime.now(timezone.utc), error=None)
    try:
        rows, fx = await asyncio.wait_for(collect_prices(free_only=free_only, live=live),
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


@router.get("/showcase-audit")
async def showcase_audit(fmt: str = "json", top: int = 20, min_price_rub: float = 0.0,
                         max_sale_rub: float = 10000.0):
    """Всё ли, что можно продать, лежит на витрине — и наоборот.

    Считается по последней выгрузке цен (она даёт наличие лотов) и текущим статусам
    карточек из базы. Пересобирать цены ради аудита не нужно: наличие товара меняется
    медленнее, чем статусы, а полный проход стоит четверти часа.

    Четыре разряда, по убыванию денежной боли:
      • upside      — товар есть, карточки нет вообще. Продажи, которых мы не делаем.
      • sleeping    — карточка есть, но спит (пауза/черновик) при живом товаре.
      • hidden      — SKU-вариант скрыт, хотя товар вернулся в продажу.
      • overhang    — карточка активна, а товара нет. Заказ придёт, выкупить будет нечего.
      • premium     — спит или не заведена, но цена продажи выше max_sale_rub.

    Разряд premium существует, чтобы не звать чинить то, что выключено намеренно: дорогие
    позиции держат вне витрины сознательно (активация шла партиями с потолком цены —
    см. activate-batch?max_price_rub). Без такого разделения аудит каждый раз показывал бы
    сотни тысяч рублей «упущенного» и приучал бы себя игнорировать. Порог считается по
    ЦЕНЕ ПРОДАЖИ (себестоимость × наценка), а не по себестоимости: ограничение возникло
    из-за того, во сколько карточка встаёт покупателю.
    """
    if _JOB["state"] == "idle" and not _JOB["csv"]:
        await _load_result()
    if not _JOB["csv"]:
        return {"error": "нет выгрузки цен — сначала /price-export"}

    reader = csv.DictReader(io.StringIO(_JOB["csv"].lstrip("﻿")), delimiter=";")
    cards = await card_index()
    buckets: dict = {"upside": [], "sleeping": [], "hidden": [], "overhang": [], "premium": []}

    for r in reader:
        try:
            pid = int(r["product_id"])
        except (ValueError, TypeError, KeyError):
            continue
        price_rub = float(r["price_rub"]) if r.get("price_rub") else 0.0
        lots = int(r.get("lots_free") or r.get("lots_free_max100") or 0)
        gid, status = cards.get(pid, (None, "нет карточки"))
        in_stock = lots > 0 and price_rub > 0
        sale_rub = round(price_rub * settings.markup, 2)
        item = {"product_id": pid, "name": r["name"], "rare": r.get("rare", ""),
                "age": r.get("age", ""), "pumping": r.get("pumping", ""),
                "price_rub": round(price_rub, 2), "sale_rub": sale_rub, "lots": lots,
                "ggsel_offer_id": gid, "status": status}

        if in_stock and price_rub >= min_price_rub:
            idle = status in ("нет карточки", "pending_create", "paused", "draft")
            if idle and max_sale_rub and sale_rub > max_sale_rub:
                buckets["premium"].append(item)      # выключено намеренно, не чиним
            elif status in ("нет карточки", "pending_create"):
                buckets["upside"].append(item)
            elif status in ("paused", "draft"):
                buckets["sleeping"].append(item)
            elif status == "вариант скрыт":
                buckets["hidden"].append(item)
        elif not in_stock and status == "active":
            buckets["overhang"].append(item)

    # Сортируем по цене: сотня дешёвых позиций без карточки стоит меньше, чем одна дорогая.
    for k in buckets:
        buckets[k].sort(key=lambda x: -x["price_rub"])

    summary = {k: len(v) for k, v in buckets.items()}
    summary["upside_rub"] = round(sum(x["price_rub"] for x in buckets["upside"]), 2)
    summary["sleeping_rub"] = round(sum(x["price_rub"] for x in buckets["sleeping"]), 2)
    summary["max_sale_rub"] = max_sale_rub

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        w.writerow(["разряд", "product_id", "название", "редкость", "возраст", "прокачка",
                    "себестоимость_руб", "цена_продажи_руб", "лотов", "ggsel_offer_id", "статус"])
        for k, items in buckets.items():
            for x in items:
                w.writerow([k, x["product_id"], x["name"], x["rare"], x["age"], x["pumping"],
                            x["price_rub"], x["sale_rub"], x["lots"], x["ggsel_offer_id"],
                            x["status"]])
        return PlainTextResponse(
            "﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="showcase-audit.csv"'})

    return {"game": GAME, "prices_from": str(_JOB["finished"] or ""), "summary": summary,
            "upside": buckets["upside"][:top], "sleeping": buckets["sleeping"][:top],
            "hidden": buckets["hidden"][:top], "overhang": buckets["overhang"][:top],
            "premium": buckets["premium"][:top]}


@router.get("/price-export/status")
async def price_export_status():
    """Состояние фоновой сборки: idle / running / done / error."""
    if _JOB["state"] == "idle" and not _JOB["csv"]:
        await _load_result()
    return {"state": _JOB["state"], "rows": _JOB["rows"], "fx": _JOB["fx"],
            "started": str(_JOB["started"] or ""), "finished": str(_JOB["finished"] or ""),
            "age": _age(_JOB["finished"]), "error": _JOB["error"]}


@router.get("/price-export", response_class=PlainTextResponse)
async def price_export(fmt: str = "csv", free_only: bool = True, refresh: bool = False,
                       live: bool = False):
    """Прайс по всему каталогу: себестоимость в USD и RUB, без наценки.

    Первый заход запускает сборку и отвечает сразу; когда она закончится, тот же адрес
    отдаст файл. ?refresh=true — пересобрать заново, ?fmt=json — JSON вместо CSV.

    По умолчанию цены берутся из кэша store_items, который наполняет событийная лента:
    это ноль запросов к StarPets и секунды вместо четверти часа. ?live=true возвращает
    поштучный опрос всего каталога — 42 тысячи обращений к `items/top`, на которые
    поставщик уже жаловался. Включать только по согласованию с ним.
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
        asyncio.create_task(_build_job(free_only, live=live))
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


