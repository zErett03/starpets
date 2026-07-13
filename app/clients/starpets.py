import hashlib
import hmac
import json as _json
import time
import httpx

from app.config import settings


class StarPetsClient:
    def __init__(self):
        self.base_url = settings.starpets_base_url

    def _sign(self, params: dict) -> str:
        params = self._normalize(params)
        parts = []
        for k, v in params.items():
            if isinstance(v, (list, dict)):
                parts.append(f"{k}:{_json.dumps(v, separators=(',', ':'))}")
            else:
                parts.append(f"{k}:{v}")
        qs = ";".join(parts) + ";"
        return hmac.new(
            settings.starpets_secret.encode(),
            qs.encode(),
            hashlib.sha512,
        ).hexdigest()

    def _normalize(self, obj):
        """Match JavaScript JSON.stringify number formatting for the signature. StarPets'
        backend is Node.js: JSON.stringify(6.0) === "6" (JS has no int/float split). Python json
        keeps the ".0" (6.0 -> "6.0"), so a ROUND-dollar price signed our way never matches the
        server's recomputed signature -> code 120 INVALID_SIGNATURE. Coerce integer-valued floats
        to int (6.0 -> 6); fractional prices (6.67) are left untouched."""
        if isinstance(obj, float):
            return int(obj) if obj.is_integer() else obj
        if isinstance(obj, dict):
            return {k: self._normalize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._normalize(v) for v in obj]
        return obj

    def _headers(self, signature: str) -> dict:
        return {
            "Api_Key": settings.starpets_shared_key,
            "X-Api-Key": settings.starpets_account_id,
            "Signature": signature,
            "Content-Type": "application/json",
        }

    def _base_params(self) -> dict:
        return {
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }

    async def get_info(self) -> dict:
        params = self._base_params()
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=10) as client:
            resp = await client.get(f"{self.base_url}/ex-buyers/info/me", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_all_products(self) -> list:
        all_items = []
        cursor = None
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {**self._base_params(), "limit": 500}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(
                    f"{self.base_url}/products/ex-buyers/all-by-cursor",
                    headers=self._headers(self._sign(params)),
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("products") or []
                all_items.extend(items)
                if len(items) < 500:
                    break
                cursor = items[-1]["id"]
        return all_items

    async def get_products(self) -> dict:
        params = self._base_params()
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=30) as client:
            resp = await client.get(f"{self.base_url}/products", params=params)
            resp.raise_for_status()
            return resp.json()

    async def iter_items(self):
        """Async generator yielding pages of 1000 items without accumulating all in memory."""
        cursor = 0
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {**self._base_params(), "limit": 1000, "cursor": cursor}
                resp = await client.get(
                    f"{self.base_url}/store/ex-buyers/items/all",
                    headers=self._headers(self._sign(params)),
                    params=params,
                )
                if not resp.is_success:
                    raise RuntimeError(
                        f"iter_items {resp.status_code}: {resp.text}"
                    )
                data = resp.json()
                items = data.get("items") or []
                if items:
                    yield items
                if len(items) < 500:
                    break
                cursor = items[-1]["id"]

    async def get_all_items(self) -> list:
        all_items = []
        async for page in self.iter_items():
            all_items.extend(page)
        return all_items

    async def get_top_item(self, client: httpx.AsyncClient, product_id: str) -> dict | None:
        params = self._base_params()
        resp = await client.get(
            f"{self.base_url}/store/ex-buyers/items/top/{product_id}",
            headers=self._headers(self._sign(params)),
            params=params,
        )
        if not resp.is_success:
            print(f"[get_top_item] product_id={product_id} HTTP {resp.status_code} body={resp.text[:200]}", flush=True)
            return None
        items = resp.json().get("items") or []
        # Only FREE items are buyable (reserveLevel: 0 FREE, 1 CART, 2 SOLD, 3 FREEZE).
        # Missing field -> treated as FREE (int(None or 0)==0), so this never over-filters.
        avail = [it for it in items if int(it.get("reserveLevel") or 0) == 0]
        if not avail:
            if items:
                print(
                    f"[get_top_item] product_id={product_id} no FREE items "
                    f"total={len(items)} reserve_levels={[it.get('reserveLevel') for it in items[:5]]}",
                    flush=True,
                )
            return None
        return min(avail, key=lambda it: float(it.get("price_usd") or 1e9))

    async def get_item_updates(
        self, limit: int = 50, cursor: int | None = None, date_ms: int | None = None
    ) -> list:
        """GET /ex-buyers/updates — incremental item price/stock event feed.

        `cursor` (event id) and `date` are mutually exclusive: pass `cursor` to get events
        AFTER an event id (incremental), or `date` to bootstrap from a timestamp. Returns the
        raw `updates` list; each entry is:
          {"id": <event id>, "event": 0, "data": [ {id, productId, price_usd, reserveLevel}, ... ]}  # created
          {"id": <event id>, "event": 1, "data": {"ids": [...], "data": {price_usd, reserveLevel}}}   # updated
          {"id": <event id>, "event": 2, "data": [id, id, ...]}                                        # deleted
        """
        from datetime import datetime, timezone, timedelta
        params = {**self._base_params(), "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        elif date_ms is not None:
            params["date"] = date_ms   # explicit date — diagnostics only (StarPets' date path 500s)
        else:
            # StarPets' date-based bootstrap returns 500 for the items feed, but cursor
            # pagination works. Bootstrap from the oldest retained event and page forward.
            params["cursor"] = 0
        print(f"[starpets] get_item_updates params={params}", flush=True)
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=30) as client:
            resp = await client.get(f"{self.base_url}/ex-buyers/updates", params=params)
            if not resp.is_success:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text
                print(
                    f"[starpets] get_item_updates FAILED status={resp.status_code} "
                    f"url={resp.request.url} body={err_body}",
                    flush=True,
                )
            resp.raise_for_status()
            data = resp.json()
        print(f"[starpets] get_item_updates raw response: {str(data)[:400]}", flush=True)
        if isinstance(data, list):
            return data
        return (
            data.get("updates") or data.get("data") or data.get("items")
            or data.get("events") or []
        )

    async def get_items(self, item_ids: list[str] = None) -> dict:
        params = self._base_params()
        if item_ids:
            params["item_ids"] = ",".join(item_ids)
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=30) as client:
            resp = await client.get(f"{self.base_url}/items", params=params)
            resp.raise_for_status()
            return resp.json()

    async def buy_by_items(self, items: list[dict]) -> dict:
        """POST /store/ex-buyers/items/buy — buy specific items by ID at known price.

        items: [{"id": <item_id>, "price": <price_usd>}, ...]
        Response items[].id are the purchased item IDs to use in create_trade.
        """
        base = self._base_params()
        payload = self._normalize({**base, "items": items})
        async with httpx.AsyncClient(
            headers=self._headers(self._sign(payload)), timeout=15
        ) as client:
            resp = await client.post(
                f"{self.base_url}/store/ex-buyers/items/buy", json=payload
            )
            if not resp.is_success:
                print(
                    f"[buy_by_items] FAILED status={resp.status_code} body={resp.text}",
                    flush=True,
                )
            resp.raise_for_status()
            return resp.json()

    async def buy_by_product(self, product_id: int, max_price_usd: float) -> dict:
        """POST /store/ex-buyers/products/buy — buy cheapest item for a product up to max price.

        Response items[].id are the purchased item IDs to use in create_trade.
        """
        base = self._base_params()
        payload = self._normalize({**base, "products": [{"id": product_id, "maxPrice": max_price_usd}]})
        async with httpx.AsyncClient(
            headers=self._headers(self._sign(payload)), timeout=15
        ) as client:
            resp = await client.post(
                f"{self.base_url}/store/ex-buyers/products/buy", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def create_trade(
        self,
        purchased_item_ids: list[str],
        roblox_username: str,
    ) -> dict:
        """POST /trades/ex-buyers/withdrawal — send purchased items to Roblox user.

        purchased_item_ids: list of string IDs from buy response items[].id
        """
        base = self._base_params()
        payload = {
            **base,
            "username": roblox_username,
            "items": purchased_item_ids,
        }
        async with httpx.AsyncClient(
            headers=self._headers(self._sign(payload)), timeout=15
        ) as client:
            resp = await client.post(
                f"{self.base_url}/trades/ex-buyers/withdrawal", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def cancel_trade(
        self, trade_id: int | str, reason_type: str, reason: str | None = None
    ) -> dict:
        """DELETE /trades/ex-buyers/withdrawal — cancel a created trade.

        Per the Business API docs the payload is a JSON body:
            {tradeId, reasonType, [reason], timestamp, recvWindow}
        `reason` (free text 1..144) is required only when reasonType == "other".
        reasonType enum: other, change_mind_about_picking_up, seller_not_friending,
        seller_left_game, seller_cancel_trade, roblox_join_error_279,
        roblox_join_error_773, roblox_join_infinite_connecting, seller_no_join_button,
        cannot_start_trade_with_seller, seller_not_offering_item, wrong_account_specified.
        Response: {"status": true}.
        """
        payload = {
            **self._base_params(),
            "tradeId": str(trade_id),
            "reasonType": reason_type,
        }
        if reason_type == "other" and reason:
            payload["reason"] = reason
        async with httpx.AsyncClient(
            headers=self._headers(self._sign(payload)), timeout=15
        ) as client:
            resp = await client.request(
                "DELETE", f"{self.base_url}/trades/ex-buyers/withdrawal", json=payload
            )
            if not resp.is_success:
                print(
                    f"[cancel_trade] FAILED status={resp.status_code} body={resp.text}",
                    flush=True,
                )
            resp.raise_for_status()
            return resp.json()

    async def get_trade_updates(self, custom_id: str) -> dict:
        params = {
            **self._base_params(),
            "custom_id": custom_id,
        }
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=10) as client:
            resp = await client.get(f"{self.base_url}/trade/status", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_bulk_trade_updates(
        self, limit: int = 50, cursor: int | None = None, date_ms: int | None = None
    ) -> list:
        """GET /ex-buyers/trades/updates — incremental status poll.

        Per the API docs, `cursor` (event id) and `date` are mutually exclusive:
        pass `cursor` to get events AFTER a given event id (incremental), or `date`
        to bootstrap from a timestamp. Returns the raw `updates` list; each item is
        {"id": <event id>, "tradeId": ..., "event": 1|2, "data": {"status": ...}}.
        """
        from datetime import datetime, timezone, timedelta
        params = {**self._base_params(), "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        else:
            # bootstrap: events since `date_ms` (default last 6h)
            params["date"] = date_ms if date_ms is not None else int(
                (datetime.now(timezone.utc) - timedelta(hours=6)).timestamp() * 1000
            )
        print(
            f"[starpets] get_bulk_trade_updates params={params}",
            flush=True,
        )
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=15) as client:
            resp = await client.get(f"{self.base_url}/ex-buyers/trades/updates", params=params)
            if not resp.is_success:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text
                print(
                    f"[starpets] get_bulk_trade_updates FAILED status={resp.status_code} "
                    f"url={resp.request.url} body={err_body}",
                    flush=True,
                )
            resp.raise_for_status()
            data = resp.json()
        print(f"[starpets] get_bulk_trade_updates raw response: {str(data)[:300]}", flush=True)
        if isinstance(data, list):
            return data
        return data.get("trades") or data.get("updates") or data.get("items") or data.get("data") or []

    async def send_friendship(self, trade_id: int) -> dict:
        """PUT /trades/ex-buyers/friendship — ask buyer to add bot as friend."""
        params = {
            **self._base_params(),
            "tradeId": trade_id,
        }
        async with httpx.AsyncClient(headers=self._headers(self._sign(params)), timeout=15) as client:
            resp = await client.put(
                f"{self.base_url}/trades/ex-buyers/friendship", params=params
            )
            resp.raise_for_status()
            return resp.json()


starpets = StarPetsClient()
