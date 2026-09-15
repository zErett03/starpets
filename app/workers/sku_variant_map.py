"""Сопоставление созданных вариантов ggsel с нашими SkuVariant — ПО ЗАГОЛОВКУ, не по порядку.

Почему это отдельный модуль и почему это важно.

Радио-опция карточки пересобирается (price_sync и stock_sync удаляют опцию целиком и создают
новую), поэтому после каждой пересборки у всех вариантов НОВЫЕ id, и мы переписываем
`sku_variants.ggsel_variant_id`. Раньше ответ `add_variants_bulk` сопоставлялся с отправленным
массивом через `zip(meta, created)` — то есть на честном слове, что ggsel вернёт варианты в
порядке запроса. Такой гарантии в API нет (и быть не может: сервер вправе отдать их
отсортированными по `position` или по id), а в запросе дефолтный вариант ставится ПЕРВЫМ при
любом его `position` — значит порядок запроса заведомо не совпадает с порядком по position.

Цена ошибки — не косметическая: `_resolve_sku_product_id` переводит выбор покупателя
(`variant_id`) в `starpets_product_id` строго по этой таблице. Сдвиг на одну позицию — и
покупатель платит за «Летает · Ездовой», а выкупается соседний вариант. Ровно это случилось
с заказом #965.

Поэтому сопоставляем по `title_ru` — он уникален в пределах карточки («метка — цена₽») и
возвращается ggsel вместе с id. Если 1:1 не сложилось, перечитываем опцию живьём и пробуем
ещё раз. Если и это не помогло — поднимаем исключение: оставить карточку с неизвестным
маппингом нельзя, лучше упавшая пересборка (её починит следующий проход и /verify-sku-mapping),
чем тихая продажа не того товара.
"""
from app.clients.ggsel import ggsel_office


def _title(d: dict) -> str:
    return (d.get("title_ru") or "").strip()


def match_by_title(payload: list[dict], created) -> list | None:
    """Вернуть `created`, выстроенный в порядке `payload`, или None если соответствие не 1:1."""
    if not isinstance(created, list) or len(created) != len(payload):
        return None
    by_title: dict[str, dict] = {}
    for cv in created:
        if not isinstance(cv, dict) or cv.get("id") is None:
            return None
        t = _title(cv)
        if not t or t in by_title:      # пустой или неуникальный заголовок — сопоставлять нечем
            return None
        by_title[t] = cv
    out = []
    for p in payload:
        cv = by_title.get(_title(p))
        if cv is None:
            return None
        out.append(cv)
    return out


async def align_created(gid: int, option_id: int, payload: list[dict], created) -> list:
    """Сопоставить ответ ggsel с отправленным массивом. Бросает RuntimeError, если не вышло."""
    aligned = match_by_title(payload, created)
    if aligned is not None:
        return aligned
    print(f"[SkuVariantMap] gid={gid} opt={option_id}: ответ bulk не сопоставился по заголовкам "
          f"(sent={len(payload)} got={len(created) if isinstance(created, list) else created!r}) "
          f"— перечитываю опцию живьём", flush=True)
    live = await ggsel_office.get_option_variants(gid, option_id)
    aligned = match_by_title(payload, live)
    if aligned is not None:
        return aligned
    raise RuntimeError(
        f"variant mapping failed gid={gid} option_id={option_id}: "
        f"sent {len(payload)}, got {len(created) if isinstance(created, list) else 0} in response, "
        f"{len(live) if isinstance(live, list) else 0} live — маппинг не записан"
    )
