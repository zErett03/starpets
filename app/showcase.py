"""Оформление карточки Adopt Me! под витрину Roblox на GGSel.

Источник правил — инструкция продавцу «Sellers | Roblox Витрина» (копия в проекте
gg: docs/GGSEL-ROBLOX-SHOWCASE.md, сверено 27.10.2026). Витрина сопоставляет
объявление с предметом своего каталога по трём вещам:

* название — `Точное имя предмета | Adopt Me!`, без рекламы, редкости в тегах,
  количества и второго товара;
* блок `[roblox]` в конце описания — свойства экземпляра `ключ=значение`.
  🔴 Блок с ошибкой СНИМАЕТ объявление с продажи даже при точном названии, поэтому
  всё, в чём нет уверенности, не пишется: такая позиция не идёт на витрину вовсе;
* категория — категория игры или её подкатегория (листы по редкости — те же,
  что у старых карточек, `offer_creator._PET_CATEGORY`).

Карточка с меню «Вариант» на витрину не годится: у каждого пункта свои возраст,
полёт и езда, а блок один на карточку. Поэтому витрина — это карточка на вариант.

Модуль чистый: ничего не отправляет и не читает из базы.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

GAME_TITLE = "Adopt Me!"
BLOCK_OPEN, BLOCK_CLOSE = "[roblox]", "[/roblox]"

# Вид каталога Starpets → `category` витрины. Позиция без вида — питомец.
_CATEGORY = {
    "": "pets", "pet": "pets", "pets": "pets", "egg": "eggs", "eggs": "eggs",
    "vehicle": "vehicles", "transport": "vehicles", "toy": "toys",
    "gift": "gifts", "food": "food", "potion": "potions",
    "pet_wear": "pet-wear", "petwear": "pet-wear", "stroller": "strollers",
}
# pumping сервиса → `type` витрины.
_TYPE = {"": "normal", "default": "normal", "normal": "normal",
         "neon": "neon", "mega_neon": "mega_neon"}
_AGES = {
    "normal": {"newborn", "junior", "pre_teen", "teen", "post_teen", "full_grown"},
    "neon": {"reborn", "twinkle", "sparkle", "flare", "sunshine", "luminous"},
    "mega_neon": set(),   # у mega neon возраста нет
}
_RARITY = {"common", "uncommon", "rare", "ultra_rare", "legendary"}
# Слова, которых в названии витрины быть не должно (раздел 2 инструкции). Целыми
# словами: «Many Mackerel» — это имя питомца, а не «any».
_BAD_TITLE_RE = re.compile(r"(?<!\w)(gift|любой|рандом|набор|random|any)(?!\w)", re.IGNORECASE)


def _norm(value) -> str:
    return (str(value) if value is not None else "").strip().lower() \
        .replace("-", "_").replace(" ", "_")


@dataclass(frozen=True)
class Showcase:
    """Карточка под витрину. `manual` — почему автоматом нельзя; при нём
    `title`/`block` пустые и карточка на витрину не заводится."""

    title: str = ""
    block: str = ""
    manual: str = ""

    @property
    def ready(self) -> bool:
        return not self.manual


def render_block(facts: list[tuple[str, str]]) -> str:
    """Блок ровно по грамматике: `schema=1` первой строкой, по строке на ключ,
    без повторов, пустых значений и разметки."""
    keys = [k for k, _ in facts]
    if len(keys) != len(set(keys)):
        raise ValueError(f"повтор ключа в блоке: {keys}")
    if any(not str(v).strip() for _, v in facts):
        raise ValueError(f"пустое значение в блоке: {facts}")
    return "\n".join([BLOCK_OPEN, "schema=1", *(f"{k}={v}" for k, v in facts), BLOCK_CLOSE])


def showcase_card(name: str, *, item_type: str | None, rare: str | None,
                  pumping: str | None, age: str | None, flyable: bool, rideable: bool,
                  delivery: str = "auto") -> Showcase:
    """Название и блок для ОДНОГО варианта (имя + вид + возраст + полёт + езда)."""
    item = (name or "").strip()
    if not item:
        return Showcase(manual="нет имени")
    if _BAD_TITLE_RE.search(item):
        return Showcase(manual="в имени слово, которое витрина не пропускает")
    if item.startswith(("Neon ", "Mega Neon ")):
        # Имя с видом внутри: витрине нужно точное имя каталога, а вид — в блоке.
        return Showcase(manual="вид вшит в имя — точного имени каталога нет")

    category = _CATEGORY.get(_norm(item_type))
    if category is None:
        return Showcase(manual=f"вид «{item_type}» не сводится к категории витрины")
    kind = _TYPE.get(_norm(pumping))
    if kind is None:
        return Showcase(manual=f"незнакомый вид прокачки «{pumping}»")
    facts: list[tuple[str, str]] = [("category", category)]
    rarity = _norm(rare)
    if rarity in _RARITY:   # незнакомую редкость не пишем, а не угадываем
        facts.append(("rarity", rarity))

    if category != "pets":
        if kind != "normal":
            return Showcase(manual="неон бывает только у питомца")
        facts.append(("delivery", delivery))
        return Showcase(title=f"{item} | {GAME_TITLE}"[:120], block=render_block(facts))

    facts.append(("type", kind))
    a = _norm(age)
    if kind == "mega_neon":
        if a:
            return Showcase(manual="у mega neon возраста нет, а в каталоге он есть")
    else:
        if not a:
            return Showcase(manual=f"у {kind} питомца нет возраста")
        if a not in _AGES[kind]:
            return Showcase(manual=f"возраста «{age}» нет у питомца типа {kind}")
        facts.append(("age", a))
    facts += [("flyable", str(bool(flyable)).lower()),
              ("rideable", str(bool(rideable)).lower()),
              ("delivery", delivery)]
    return Showcase(title=f"{item} | {GAME_TITLE}"[:120], block=render_block(facts))


def strip_block(description: str) -> str:
    start = description.find(BLOCK_OPEN)
    if start < 0:
        return description
    end = description.find(BLOCK_CLOSE, start)
    tail = description[end + len(BLOCK_CLOSE):] if end >= 0 else ""
    return (description[:start] + tail).strip()


def with_block(description: str, block: str) -> str:
    """Описание с блоком в конце отдельным абзацем. Блок ровно один (раздел 3)."""
    base = strip_block(description).rstrip()
    return f"{base}\n\n{block}" if block else base


_PUMP_RU = {"normal": "обычный", "neon": "Neon", "mega_neon": "Mega Neon"}
_PUMP_EN = {"normal": "Normal", "neon": "Neon", "mega_neon": "Mega Neon"}


def describe(name: str, *, rare: str | None, pumping: str | None, age: str | None,
             flyable: bool, rideable: bool) -> tuple[str, str]:
    """Человеческая часть описания одного варианта (до инструкции и блока)."""
    kind = _TYPE.get(_norm(pumping), "normal")
    age_txt = (age or "").replace("_", " ").title()
    ru = [f"Питомец: {name}", f"Вид: {_PUMP_RU[kind]}"]
    en = [f"Pet: {name}", f"Type: {_PUMP_EN[kind]}"]
    if rare:
        r = str(rare).replace("_", " ").title()
        ru.append(f"Редкость: {r}")
        en.append(f"Rarity: {r}")
    if age_txt:
        ru.append(f"Возраст: {age_txt}")
        en.append(f"Age: {age_txt}")
    ru.append(f"Летает: {'да' if flyable else 'нет'}")
    en.append(f"Flyable: {'yes' if flyable else 'no'}")
    ru.append(f"Ездовой: {'да' if rideable else 'нет'}")
    en.append(f"Rideable: {'yes' if rideable else 'no'}")
    return "\n".join(ru), "\n".join(en)
