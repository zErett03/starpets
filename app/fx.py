import httpx

from datetime import datetime, timedelta

_cache: dict = {"rate": None, "valid_until": None}


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
