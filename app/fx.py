import httpx

from datetime import datetime, timedelta

_cache: dict = {"rate": None, "valid_until": None}
# Курсы прочих валют (EUR и т.п.): {код: (курс, действителен_до)}. Отдельно от _cache,
# чтобы не трогать проверенный путь USD, по которому считается вся закупка.
_rates: dict = {}


async def get_rate_to_rub(code: str) -> float:
    """Курс валюты к рублю по ЦБ. RUB -> 1.0.

    Нужен для сумм, приходящих в уведомлении ggsel: покупатель может заплатить в евро
    или долларах, а в заказ сумма попадала как рубли — заказ на 3.64 € выглядел как
    3.64 ₽, профит-гард видел «убыток 242 ₽» и блокировал выкуп выгодной сделки.
    """
    code = (code or "RUB").strip().upper()
    if code in ("RUB", "RUR", "₽", ""):
        return 1.0
    if code == "USD":
        return await get_usd_rub()

    now = datetime.utcnow()
    cached = _rates.get(code)
    if cached and cached[1] and now < cached[1]:
        return cached[0]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.cbr-xml-daily.ru/daily_json.js")
            resp.raise_for_status()
            data = resp.json()
            v = data["Valute"][code]
            # Курс ЦБ даётся за Nominal единиц (например, 100 JPY) — приводим к одной.
            rate = float(v["Value"]) / float(v.get("Nominal") or 1)
        _rates[code] = (rate, now + timedelta(hours=1))
        print(f"[FX] {code}/RUB = {rate}", flush=True)
        return rate
    except Exception as e:
        print(f"[FX] курс {code} недоступен: {e}", flush=True)
        if cached:
            return cached[0]
        raise RuntimeError(f"FX rate for {code} unavailable") from e


async def get_usd_rub() -> float:
    now = datetime.utcnow()

    if _cache["rate"] and _cache["valid_until"] and now < _cache["valid_until"]:
        return _cache["rate"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.cbr-xml-daily.ru/daily_json.js")
            resp.raise_for_status()
            data = resp.json()
            rate = data["Valute"]["USD"]["Value"]
            _cache["rate"] = rate
            _cache["valid_until"] = now + timedelta(hours=1)
            print(f"[FX] USD/RUB = {rate}")
            return rate
    except Exception as e:
        print(f"[FX] Failed to get rate: {e}")
        if _cache["rate"]:
            return _cache["rate"]
        raise RuntimeError("FX rate unavailable") from e


def item_cost_ok(price_usd: float, fx_rate: float, sale_price_rub: float, max_cost_ratio: float):
    """Profitability guard. Returns (ok, raw_cost_rub).

    raw_cost_rub = live item price in RUB WITHOUT markup (the money we actually spend).
    ok = raw_cost_rub <= sale_price_rub * max_cost_ratio. ok=False means the deal would
    be unprofitable (live cost too high vs the price the buyer paid)."""
    try:
        raw_cost_rub = float(price_usd) * float(fx_rate)
        sale = float(sale_price_rub or 0)
    except (TypeError, ValueError):
        return False, 0.0
    if sale <= 0:
        return False, raw_cost_rub  # unknown sale price -> refuse (safe)
    return raw_cost_rub <= sale * float(max_cost_ratio), raw_cost_rub


def calc_price_rub(price_usd: float, markup: float, fx_rate: float) -> float:
    from app.config import settings

    price = round(price_usd * markup * fx_rate, 2)
    return max(settings.min_price_rub, price)
