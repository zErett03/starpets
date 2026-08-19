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
    # Сырые счётчики строк нужны, чтобы отличить «всё в порядке» от «выборка пустая»:
    # coverage 0/0/0 сам по себе не говорит, отвалились карточки на статусе, на NULL
    # product_id или на нулевой цене — а это три разные поломки.
    stats = {"checked": 0, "no_floor": 0, "live_checked": 0, "zero_price": 0,
             "rows_offers": len(offers), "rows_variants": len(variants),
             "rows_sku_cards": len(sku_names), "rows_floors": len(floors)}
    no_floor_offers: list = []          # кандидаты на живую проверку через API

    def _check(key: str, name: str, gid, price_rub: float, product_id: int,
               floor_override: float | None = None) -> None:
        floor_usd = floor_override if floor_override is not None else floors.get(product_id)
        if price_rub <= 0:
            stats["zero_price"] += 1
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
            stats["orphan_variants"] = stats.get("orphan_variants", 0) + 1
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


_PRICE_KEYS = ("price_rub", "price", "cost", "amount", "value")

# Сколько карточек опросила последняя сверка с витриной — отдаём наружу в отчёте,
# чтобы «расхождений нет» нельзя было спутать с «проверять было нечего».
_last_showcase_checked = 0


def _dig_price(data) -> float | None:
    """Цена карточки из ответа ggsel: структура вложенная и не документирована."""
    if isinstance(data, dict):
        for k in _PRICE_KEYS:
            v = data.get(k)
            if isinstance(v, (int, float, str)):
                try:
                    f = float(v)
                    if f > 0:
                        return f
                except (TypeError, ValueError):
                    pass
        for v in data.values():
            got = _dig_price(v)
            if got is not None:
                return got
    elif isinstance(data, list):
        for v in data:
            got = _dig_price(v)
            if got is not None:
                return got
    return None


async def check_showcase_drift(max_price_rub: float, top_n: int,
                               hot_orders: int, tolerance_rub: float,
                               fix: bool = False) -> list[dict]:
    """Сверяет НАШУ цену с ценой на ВИТРИНЕ ggsel.

    Это та самая слепая зона, из-за которой ушли деньги: у нас в базе значилось 228 ₽,
    покупатели платили 104 ₽ по витрине, а precheck и профит-гард сверялись с нашей ценой
    и пропускали заказ. Ни один внутренний отчёт расхождения не видел — цену на витрине
    никто не спрашивал.

    Проверяем не всё (карточек тысячи), а то, где ошибка дороже всего:
      • самые дорогие карточки до max_price_rub;
      • «горячие» — по которым за сутки прошло hot_orders и больше заказов.
    """
    from datetime import datetime, timedelta, timezone

    from app.clients.ggsel import ggsel_office
    from app.db.models import Order

    since = datetime.now(timezone.utc) - timedelta(days=1)
    async with AsyncSessionLocal() as db:
        expensive = (await db.execute(
            select(Offer)
            .where(Offer.status == OfferStatus.active,
                   Offer.ggsel_offer_id.isnot(None),
                   Offer.price_rub.isnot(None),
                   Offer.price_rub <= max_price_rub)
            .order_by(Offer.price_rub.desc())
            .limit(top_n)
        )).scalars().all()
        # «Горячие» карточки: продажи идут потоком — если цена там разъехалась, счёт
        # убыточных заказов растёт быстрее всего.
        hot_ids = [r[0] for r in (await db.execute(
            select(Order.offer_id, func.count())
            .where(Order.created_at >= since)
            .group_by(Order.offer_id)
            .having(func.count() >= hot_orders)
        )).all()]
        hot = (await db.execute(
            select(Offer).where(Offer.id.in_(hot_ids),
                                Offer.ggsel_offer_id.isnot(None))
        )).scalars().all() if hot_ids else []

    # Пол по каждому продукту — не для расчёта цены, а для ПРАВА её проталкивать. Наша
    # цена считается правильной только пока она подтверждается себестоимостью; если пола
    # нет или цена скатилась к минимальной планке, значит она не посчитана, а осталась
    # заглушкой, и толкать её на витрину нельзя.
    from app.pricing import robust_floors_for
    fx = await get_usd_rub()
    async with AsyncSessionLocal() as db:
        floors = await robust_floors_for(
            db, [o.starpets_product_id for o in list(expensive) + list(hot)
                 if o.starpets_product_id is not None])

    global _last_showcase_checked
    seen_gids, targets = set(), []
    for o in list(expensive) + list(hot):
        if o.ggsel_offer_id in seen_gids:
            continue
        seen_gids.add(o.ggsel_offer_id)
        targets.append(o)
    _last_showcase_checked = len(targets)

    drift = []
    for o in targets:
        try:
            data = await ggsel_office.get_offer(o.ggsel_offer_id)
        except Exception as e:  # noqa: BLE001 — одна недоступная карточка не рушит проход
            print(f"[PriceWatch] витрина {o.ggsel_offer_id}: {type(e).__name__}: {e}", flush=True)
            continue
        shown = _dig_price(data)
        if shown is None:
            continue
        ours = float(o.price_rub or 0)
        if ours <= 0 or abs(shown - ours) <= tolerance_rub:
            continue
        row = {"ggsel_offer_id": o.ggsel_offer_id, "name": o.name,
               "our_price_rub": round(ours, 2), "showcase_price_rub": round(shown, 2),
               "diff_rub": round(shown - ours, 2)}

        # ПРАВО ПРОТАЛКИВАТЬ. Наша цена верна только пока подтверждена себестоимостью:
        # есть свежий пол и цена его покрывает. Всё остальное — повод звать человека, а не
        # чинить, потому что неизвестно, какая из двух сторон врёт.
        #
        # Чего здесь СПЕЦИАЛЬНО НЕТ: проверки «цена равна минимальной планке — значит
        # заглушка». Она кажется разумной и ошибочна: для дешёвого товара планка в 100 ₽
        # может быть в десять раз выше себестоимости, то есть законной ценой, а не
        # незаполненным полем.
        floor_usd = floors.get(int(o.starpets_product_id)) if o.starpets_product_id else None
        cost_rub = float(floor_usd) * fx if floor_usd else None
        blocked = None
        if cost_rub is None:
            blocked = "нет пола в store_items — цена не подтверждена"
        elif ours < cost_rub:
            blocked = f"наша цена ниже себестоимости {round(cost_rub, 2)}₽"
        if blocked:
            row["fixed"] = False
            row["blocked"] = blocked
            row["cost_rub"] = round(cost_rub, 2) if cost_rub else None
            drift.append(row)
            continue

        # Дальше чинить безопасно: цена посчитана от живого пола с наценкой, витрина просто
        # отстала. Ждать ценника бессмысленно — он сравнивает новую цену с базой, а там она
        # уже правильная, и пуш не повторится.
        if fix:
            try:
                await ggsel_office.update_price(o.ggsel_offer_id, round(ours, 2))
                row["fixed"] = True
            except Exception as e:  # noqa: BLE001
                row["fixed"] = False
                row["fix_error"] = f"{type(e).__name__}: {e}"
        drift.append(row)
    drift.sort(key=lambda r: r["diff_rub"])      # сначала те, где витрина ДЕШЕВЛЕ нашей
    # Сколько карточек реально опросили. Без этого «дрейфа нет» и «проверять было нечего»
    # выглядят одинаково — ровно на этом мы уже один раз обожглись с охватом.
    print(f"[PriceWatch] витрина: опрошено {len(targets)} карточек "
          f"(дорогих {len(expensive)}, горячих {len(hot)}), расхождений {len(drift)}", flush=True)
    return drift


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

    # ПОЧИНКА, а не жалоба. Раньше сторож сообщал о занижённых ценах и ждал, что человек
    # руками запустит нужный синк. Это неправильное разделение труда: нужное действие
    # известно заранее и одно и то же, а пока оно ждёт человека, карточка продолжает
    # продаваться в убыток. Теперь сторож чинит сам и лишь потом рассказывает.
    repaired = {"cards": 0, "variants": 0}
    if found and settings.price_watch_autofix:
        if any(r["key"].startswith("o") for r in found):
            try:
                from app.workers.floor_reconcile import reprice_cards
                res = await reprice_cards(dry_run=False)
                repaired["cards"] = res.get("pushed", 0)
            except Exception as e:  # noqa: BLE001
                print(f"[PriceWatch] переоценка карточек не удалась: {e}", flush=True)
        if any(r["key"].startswith("v") for r in found):
            # Варианты SKU живут внутри опции карточки, отдельным PATCH цену им не поднять —
            # это умеет только штатный SKU-синк. Порог занижаем: обычный прогон пропускает
            # мелкие расхождения, а здесь мы уже знаем, что цена ниже себестоимости.
            try:
                from app.workers.sku_price_sync import sku_price_sync
                res = await sku_price_sync(threshold_rub=1.0, threshold_pct=0.01,
                                           max_cards=settings.sku_price_sync_max_cards,
                                           max_rebuilds=settings.sku_price_sync_max_rebuilds,
                                           dry_run=False)
                repaired["variants"] = res.get("updated", res.get("cards", 0)) if isinstance(res, dict) else 0
            except Exception as e:  # noqa: BLE001
                print(f"[PriceWatch] SKU-синк не удался: {e}", flush=True)
        print(f"[PriceWatch] автопочинка: карточек {repaired['cards']}, "
              f"вариантов {repaired['variants']}", flush=True)

    # Сверка с витриной: расхождение нашей цены и цены на ggsel — первопричина убытков,
    # и внутренними отчётами оно не ловится в принципе.
    drift = []
    try:
        drift = await check_showcase_drift(
            max_price_rub=settings.price_watch_showcase_max_price_rub,
            top_n=settings.price_watch_showcase_top,
            hot_orders=settings.price_watch_hot_orders,
            tolerance_rub=settings.price_watch_showcase_tolerance_rub,
            fix=settings.price_watch_showcase_fix,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[PriceWatch] сверка витрины не выполнена: {e}", flush=True)

    cheaper = [d for d in drift if d["diff_rub"] < 0]      # витрина ДЕШЕВЛЕ — опасно
    blocked = [d for d in drift if d.get("blocked")]        # цена не подтверждена — нужны руки
    if drift:
        print(f"[PriceWatch] расхождение с витриной: {len(drift)} "
              f"(из них дешевле у нас на витрине: {len(cheaper)}, "
              f"не тронуто из-за неподтверждённой цены: {len(blocked)})", flush=True)
    await _send_digest(found, repaired, cheaper, blocked, stats)

    return {"found": len(found), "new": len(fresh), "coverage": stats, "repaired": repaired,
            "showcase_checked": _last_showcase_checked, "showcase_drift": drift[:20],
            "items": found[:20]}


_DIGEST_KEY = "price_watch:digest_sent"


async def _send_digest(found, repaired, cheaper, blocked, stats) -> None:
    """Одна сводка раз в price_watch_digest_hours вместо потока алертов.

    Прежний сторож слал по сообщению на каждую находку каждый час, причём в чат заказов —
    туда, где сидит оператор поддержки, который с ценами всё равно ничего не делает. Такой
    поток не информирует, а обучает не читать бота. Теперь: одно сообщение, в отдельный
    чат, и только если есть что сказать — либо что-то починено, либо что-то требует рук.

    Из графика выбиваются только случаи, требующие человека (blocked): если их состав
    изменился, сводка уходит сразу, не дожидаясь окна.
    """
    needs_hands = len(blocked)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(KVState).where(KVState.key == _DIGEST_KEY))).scalar_one_or_none()
        prev_blocked, ts = ((row.value or "|0").split("|", 1) + ["0"])[:2] if row else ("", "0")
        now = datetime.now(timezone.utc).timestamp()
        due = (now - float(ts or 0)) >= settings.price_watch_digest_hours * 3600
        blocked_key = ",".join(sorted(str(b["ggsel_offer_id"]) for b in blocked))
        urgent = bool(blocked) and blocked_key != prev_blocked
        if not (due or urgent):
            return
        nothing_to_say = not (found or cheaper or blocked or repaired["cards"] or repaired["variants"])
        if nothing_to_say:
            return
        val = f"{blocked_key}|{now}"
        if row:
            row.value = val
        else:
            db.add(KVState(key=_DIGEST_KEY, value=val))
        await db.commit()

    lines = [f"📊 <b>Цены за {settings.price_watch_digest_hours} ч</b>"]
    if repaired["cards"] or repaired["variants"]:
        lines.append(f"✅ переоценено: карточек {repaired['cards']}, вариантов {repaired['variants']}")
    if cheaper:
        fixed = sum(1 for d in cheaper if d.get("fixed"))
        lines.append(f"✅ витрина выровнена: {fixed} из {len(cheaper)}")
    if found:
        worst = found[0]
        lines.append(f"⚠️ ниже себестоимости: {len(found)} — худшая {worst['name'][:40]} "
                     f"({worst['price_rub']}₽ при {worst['cost_rub']}₽)")
    if stats.get("no_floor"):
        lines.append(f"ℹ️ без цены в кэше: {stats['no_floor']} из "
                     f"{stats['no_floor'] + stats['checked']}")
    if needs_hands:
        b = blocked[0]
        lines.append("")
        lines.append(f"❗️ <b>Нужны руки — {needs_hands} шт</b>")
        lines.append(f"{b['name'][:40]}: у нас {b['our_price_rub']}₽, "
                     f"на витрине {b['showcase_price_rub']}₽ — {b['blocked']}")
        lines.append("Проверь /card-diag?ggsel_offer_id=" + str(b["ggsel_offer_id"]))
    else:
        lines.append("Действий не требуется.")

    try:
        from app.telegram.bot import _price_chats, send_message
        for chat in _price_chats():
            await send_message(chat, "\n".join(lines))
    except Exception as e:  # noqa: BLE001 — сбой алерта не должен ронять задание
        print(f"[PriceWatch] сводка не отправлена: {e}", flush=True)
