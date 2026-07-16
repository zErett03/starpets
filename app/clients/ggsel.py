import asyncio

import httpx

from app.config import settings

SELLER_OFFICE_V2_URL = settings.ggsel_base_url
_GGSEL_RETRY_STATUS = frozenset({429, 502, 503, 504})


# Current (correct) consent-checkbox title — buyer must tick it before paying.
_CONSENT_TITLE_RU = "Я подтверждаю, что ознакомился с описанием товара перед его оплатой"
_CONSENT_TITLE_EN = "I confirm that I have read the product description before paying"
# Every consent-checkbox title we've ever created — used to find & clean up OUR options
# (including the earlier broken, variant-less version) before re-adding the correct one.
_CONSENT_TITLES_ALL = {
    _CONSENT_TITLE_RU,
    "Я понимаю, что должен принять трейд в течение 10 минут с момента оплаты заказа",
}

class GgselSellerOfficeClient:
    """Seller Office API v2 — создание офферов, управление, доставка."""

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": settings.ggsel_api_key,
        }

    async def _request_retry(self, client, method, url, retries=3, **kwargs):
        """Send request, retrying transient gateway errors (429/502/503/504) with backoff.
        ggsel's gateway occasionally 502s under concurrent load; a couple of retries with
        small backoff recovers almost all of them within the same run."""
        resp = None
        for attempt in range(retries + 1):
            try:
                resp = await client.request(method, url, **kwargs)
            except httpx.TransportError:
                # connection-level flap (disconnect/reset/timeout) under load — retry
                if attempt < retries:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                raise
            if resp.status_code in _GGSEL_RETRY_STATUS and attempt < retries:
                await asyncio.sleep(0.4 * (2 ** attempt))  # 0.4s, 0.8s, 1.6s
                continue
            return resp
        return resp

    async def create_offer(
        self,
        title_ru: str,
        title_en: str,
        description_ru: str,
        description_en: str,
        instructions_ru: str,
        instructions_en: str,
        category_id: int,
        cover_base64: str,
        price: float,
        cover_mime: str = "image/jpeg",
    ) -> dict:
        cover_data_uri = f"data:{cover_mime};base64,{cover_base64}" if cover_base64 else None
        body = {
            "title_ru": title_ru,
            "title_en": title_en,
            "description_ru": description_ru,
            "description_en": description_en,
            "instructions_ru": instructions_ru,
            "instructions_en": instructions_en,
            "cover_image_ru": cover_data_uri,
            "price": price,
            "currency": "RUB",
            "is_autoselling": False,
            "is_unlimited_quantity": True,   # manual delivery, unlimited StarPets supply -> never "sold out"
            "category_id": category_id,
            "delivery": "manual",
            "post_payment_url": f"{settings.public_url}/delivery",
        }

        import json as _json

        _log_body = {**body, "cover_image_ru": (f"<data-uri {len(cover_data_uri)} chars>"
                                              if cover_data_uri else None)}
        print(f"[create_offer] body: {_json.dumps(_log_body, ensure_ascii=False)}", flush=True)

        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(client, "POST", f"{SELLER_OFFICE_V2_URL}/offers", json=body)
            print(f"[create_offer] status={resp.status_code} response={resp.text[:500]}", flush=True)
            if not resp.is_success:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()

    async def patch_offer(self, offer_id: int, precheck_url: str, notification_url: str) -> dict:
        body = {
            "pre_payment_settings": {
                "is_enabled": True,
                "url": precheck_url,
                # False = ggsel HARD-blocks payment when our precheck returns an error (OOS /
                # price_too_high / bad username). True (ggsel default) silently let buyers pay
                # through every rejection — the root of OOS & underpriced purchases. Fail-closed:
                # if our precheck endpoint is down, sales are blocked until it recovers.
                "allow_payment": False,
            },
            "notification_settings": {
                "type": "url",
                "url": notification_url,
                "http_method": "POST",
                "is_disabled": False,
                "is_default": False,
            },
        }

        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}", json=body)
            if not resp.is_success:
                print(f"[patch_offer] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def update_content(self, offer_id: int, description_ru: str, description_en: str,
                             instructions_ru: str, instructions_en: str) -> dict:
        body = {
            "description_ru": description_ru,
            "description_en": description_en,
            "instructions_ru": instructions_ru,
            "instructions_en": instructions_en,
        }
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}", json=body
            )
            if not resp.is_success:
                print(f"[update_content] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def update_cover(self, offer_id: int, cover_base64: str, cover_mime: str = "image/png") -> dict:
        """PATCH just the cover image of an existing offer (same field as create_offer)."""
        data_uri = f"data:{cover_mime};base64,{cover_base64}" if cover_base64 else None
        body = {"cover_image_ru": data_uri}
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}", json=body
            )
            if not resp.is_success:
                print(f"[update_cover] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def create_option(self, offer_id: int) -> dict:
        body = {
            "options": [
                {
                    "type": "text",
                    "title_ru": "Ваш Roblox Username (без знака @)",
                    "title_en": "Your Roblox Username (without @)",
                    "comment_ru": "Имя пользователя в Roblox для отправки трейда",
                    "is_required": True,
                    "position": 1,
                }
            ]
        }

        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "POST", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options", json=body
            )
            if not resp.is_success:
                print(f"[create_option] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def _consent_option_id(self, offer_id: int) -> int | None:
        """Return the id of the consent checkbox option on this offer (matched by title),
        or None. Used to attach a variant right after creating the option."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=15) as client:
            resp = await self._request_retry(
                client, "GET", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options"
            )
            resp.raise_for_status()
            data = resp.json()
        opts = data if isinstance(data, list) else (data.get("options") or data.get("data") or [])
        for o in opts:
            if (o.get("title_ru") or "").strip() == _CONSENT_TITLE_RU and o.get("id") is not None:
                return o.get("id")
        return None

    async def add_option_variant(self, offer_id: int, option_id: int) -> dict:
        """Attach the single 'Подтверждаю' variant to a consent checkbox option
        (ggsel rejects `variants` on option creation — variants have their own endpoint)."""
        body = {
            "variants": [
                {
                    "title_ru": "Подтверждаю",
                    "title_en": "Confirm",
                    "price": 0,
                    "discount_type": "fixed",
                    "impact_type": "increase",
                    "is_default": False,
                    "status": "active",
                    "position": 0,
                }
            ]
        }
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "POST",
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options/{option_id}/variants",
                json=body,
            )
            if not resp.is_success:
                print(f"[add_option_variant] offer_id={offer_id} option_id={option_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return {"ok": True}

    async def create_consent_option(self, offer_id: int) -> dict:
        """Add the mandatory pre-purchase consent checkbox in TWO steps:
          1) create the check_box option (WITHOUT variants — ggsel 422s otherwise),
          2) attach its single 'Подтверждаю' variant via the variants endpoint.
        Without a variant the checkbox renders as a label with no tickable box."""
        body = {
            "options": [
                {
                    "type": "check_box",
                    "title_ru": _CONSENT_TITLE_RU,
                    "title_en": _CONSENT_TITLE_EN,
                    "is_required": True,
                    "is_price_modifier_hidden": True,
                    "position": 2,
                }
            ]
        }
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "POST", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options", json=body
            )
            if not resp.is_success:
                print(f"[create_consent_option] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            # Prefer the new option id from the create response (saves a GET); fall back to lookup.
            option_id = None
            try:
                rd = resp.json()
                _opts = rd if isinstance(rd, list) else (rd.get("options") or rd.get("data") or [])
                for _o in _opts:
                    if (_o.get("title_ru") or "").strip() == _CONSENT_TITLE_RU and _o.get("id") is not None:
                        option_id = _o.get("id")
                        break
                if option_id is None and isinstance(rd, dict) and rd.get("id") is not None:
                    option_id = rd.get("id")
            except Exception:
                option_id = None

        if option_id is None:
            option_id = await self._consent_option_id(offer_id)
        if option_id is None:
            raise RuntimeError(f"consent option created but id not found offer_id={offer_id}")
        return await self.add_option_variant(offer_id, option_id)

    async def has_consent_option(self, offer_id: int) -> bool:
        """True if the consent checkbox is already present (idempotency guard so a
        re-run doesn't add a duplicate). Uses the retrying request helper so a
        transient 503 on the check doesn't count as a hard error."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=15) as client:
            resp = await self._request_retry(
                client, "GET", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options"
            )
            resp.raise_for_status()
            data = resp.json()
        opts = data if isinstance(data, list) else (data.get("options") or data.get("data") or [])
        for o in opts:
            if (o.get("title_ru") or "").strip() == _CONSENT_TITLE_RU:
                return True
        return False

    async def consent_option_state(self, offer_id: int) -> tuple[bool, list[int]]:
        """Inspect an offer's options in one call. Returns (has_correct, stale_ids):
          has_correct — a proper consent check_box (our title + >=1 variant) is present;
          stale_ids   — ids of ANY of our consent options that are broken/old (variant-less
                        or the old title) and must be deleted before (re)creating the good one."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=15) as client:
            resp = await self._request_retry(
                client, "GET", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options"
            )
            resp.raise_for_status()
            data = resp.json()
        opts = data if isinstance(data, list) else (data.get("options") or data.get("data") or [])
        has_correct = False
        stale_ids: list[int] = []
        for o in opts:
            title = (o.get("title_ru") or "").strip()
            if title not in _CONSENT_TITLES_ALL:
                continue
            is_good = (
                title == _CONSENT_TITLE_RU
                and o.get("type") == "check_box"
                and bool(o.get("variants"))
            )
            if is_good and not has_correct:
                has_correct = True
            elif o.get("id") is not None:
                stale_ids.append(o.get("id"))
        return has_correct, stale_ids

    async def _options_list(self, offer_id: int) -> list:
        async with httpx.AsyncClient(headers=self._headers(), timeout=15) as client:
            resp = await self._request_retry(
                client, "GET", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options"
            )
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, list) else (data.get("options") or data.get("data") or [])

    async def create_radio_option(self, offer_id: int, title_ru: str, title_en: str = "Variant",
                                  position: int = 3) -> int:
        """Create a radio_button option on an offer; return its option id."""
        body = {"options": [{
            "type": "radio_button",
            "title_ru": title_ru,
            "title_en": title_en,
            "is_required": True,
            "is_price_modifier_hidden": False,
            "position": position,
        }]}
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "POST", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options", json=body
            )
            if not resp.is_success:
                print(f"[create_radio_option] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
        for o in await self._options_list(offer_id):
            if (o.get("title_ru") or "").strip() == title_ru and o.get("type") == "radio_button" and o.get("id") is not None:
                return o.get("id")
        raise RuntimeError(f"radio option created but id not found offer_id={offer_id}")

    async def add_variant(self, offer_id: int, option_id: int, title_ru: str, title_en: str,
                          price_delta: float, is_default: bool = False, position: int = 0) -> int:
        """Add one variant to an option. price_delta = +/- rub modifier from base. Returns variant id."""
        impact = "increase" if price_delta >= 0 else "decrease"
        body = {"variants": [{
            "title_ru": title_ru,
            "title_en": title_en,
            "price": round(abs(price_delta), 2),
            "discount_type": "fixed",
            "impact_type": impact,
            "is_default": is_default,
            "status": "active",
            "position": position,
        }]}
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "POST",
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options/{option_id}/variants",
                json=body,
            )
            if not resp.is_success:
                print(f"[add_variant] offer_id={offer_id} option_id={option_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:400]}", request=resp.request, response=resp
                )
        for o in await self._options_list(offer_id):
            if o.get("id") == option_id:
                for v in (o.get("variants") or []):
                    if (v.get("title_ru") or "").strip() == title_ru and v.get("id") is not None:
                        return v.get("id")
        raise RuntimeError(f"variant created but id not found offer_id={offer_id} option_id={option_id} title={title_ru!r}")

    async def update_variants(self, offer_id: int, option_id: int, variants: list[dict]) -> dict:
        """In-place upsert of variants on an option. POST /variants with each variant carrying its
        existing `id` UPDATES it (ids preserved) — the same endpoint that creates. Per ggsel, the
        default variant must have price 0. `variants` items:
          {id, title_ru, title_en, price, discount_type, impact_type, is_default, status, position}."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "POST",
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options/{option_id}/variants",
                json={"variants": variants},
            )
            if not resp.is_success:
                print(f"[update_variants] offer_id={offer_id} option_id={option_id} status={resp.status_code} body={resp.text[:400]}", flush=True)
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:400]}", request=resp.request, response=resp
                )
            return resp.json()

    async def add_variants_bulk(self, offer_id: int, option_id: int, variants: list[dict]) -> list:
        """Create MANY variants in ONE POST (items WITHOUT `id`). ~1 call instead of N×(POST+GET).
        Returns the created variants (each with its assigned `id`), in request order. Put the
        default variant FIRST in `variants` (a required radio must have a default during save)."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "POST",
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options/{option_id}/variants",
                json={"variants": variants},
            )
            if not resp.is_success:
                print(f"[add_variants_bulk] offer_id={offer_id} option_id={option_id} status={resp.status_code} body={resp.text[:400]}", flush=True)
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:400]}", request=resp.request, response=resp
                )
            data = resp.json()
            return data.get("data") if isinstance(data, dict) and "data" in data else data

    async def get_active_offer_ids(self) -> list[int]:
        """Fetch all active offer IDs from GGSel, filter by status in response."""
        ids = []
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.get(f"{SELLER_OFFICE_V2_URL}/offers")
            print(
                f"[get_active_offer_ids] status={resp.status_code} "
                f"response={resp.text[:2000]}",
                flush=True,
            )
            resp.raise_for_status()
            data = resp.json()
            page = data if isinstance(data, list) else data.get("offers") or data.get("data") or []
            active = [o["id"] for o in page if o.get("status") == "active"]
            print(f"[get_active_offer_ids] total={len(page)} active={len(active)}", flush=True)
            ids.extend(active)
        return ids

    async def get_offer(self, offer_id: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.get(f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}")
            resp.raise_for_status()
            return resp.json()

    async def get_options(self, offer_id: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=15) as client:
            resp = await self._request_retry(
                client, "GET", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options"
            )
            resp.raise_for_status()
            return resp.json()

    async def update_option(self, offer_id: int, option_id: int, title_ru: str, title_en: str) -> dict:
        """PATCH a single option's RU/EN title (e.g. add '(без знака @)' to the username field)."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options/{option_id}",
                json={"title_ru": title_ru, "title_en": title_en},
            )
            if not resp.is_success:
                print(f"[update_option] offer={offer_id} opt={option_id} status={resp.status_code} body={resp.text[:200]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def delete_options(self, offer_id: int, option_ids: list[int]) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=15) as client:
            resp = await self._request_retry(
                client, "DELETE",
                f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}/options",
                json={"option_ids": option_ids, "delete_all": "false"},
            )
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return {"ok": True}

    async def set_post_payment_url(self, offer_id: int, url: str) -> dict:
        # ggsel's v2 PATCH rejects a bare {"post_payment_url": ...} with 422 — it must be sent
        # together with the delivery mode (same as at create_offer). Include delivery="manual".
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}",
                json={"delivery": "manual", "post_payment_url": url},
            )
            if not resp.is_success:
                print(f"[set_post_payment_url] offer_id={offer_id} status={resp.status_code} body={resp.text[:200]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def set_quantity(self, offer_id: int, quantity: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}",
                json={"quantity": quantity},
            )
            resp.raise_for_status()
            return resp.json()

    async def update_price(self, offer_id: int, price: float) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}",
                json={"price": price},
            )
            resp.raise_for_status()
            return resp.json()

    async def update_title(self, offer_id: int, title_ru: str, title_en: str) -> dict:
        """PATCH an offer's title (RU/EN). Used to add the game-name suffix for store search."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}",
                json={"title_ru": title_ru, "title_en": title_en},
            )
            if not resp.is_success:
                print(f"[update_title] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def set_unlimited(self, offer_id: int) -> dict:
        """PATCH an offer to unlimited quantity (so a single sale doesn't mark it 'sold out')."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await self._request_retry(
                client, "PATCH", f"{SELLER_OFFICE_V2_URL}/offers/{offer_id}",
                json={"is_unlimited_quantity": True},
            )
            if not resp.is_success:
                print(f"[set_unlimited] offer_id={offer_id} status={resp.status_code} body={resp.text[:300]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def _pause_offer_legacy_patch(self, offer_id: int) -> dict:
        # deprecated: PATCH status is rejected by ggsel (422). Kept out of the pause path.
        return await self.pause_offers([offer_id])

    async def activate_offer(self, offer_id: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch_activate",
                json={"offer_ids": [offer_id]},
            )
            print(f"[activate_offer] status={resp.status_code} response={resp.text[:500]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    async def batch_activate(self, offer_ids: list[int]) -> dict:
        """POST /offers/batch_activate — activate up to 100 offers at once."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch_activate",
                json={"offer_ids": offer_ids},
            )
            print(
                f"[batch_activate] count={len(offer_ids)} status={resp.status_code} "
                f"response={resp.text[:200]}",
                flush=True,
            )
            resp.raise_for_status()
            return resp.json()

    async def pause_offer(self, offer_id: int) -> dict:
        """Pause a single offer via the batch endpoint. ggsel rejects a status change through
        PATCH /offers/{id} (422 'unpermitted parameter: status') — status transitions must use
        the dedicated batch_pause / batch_activate endpoints."""
        return await self.pause_offers([offer_id])

    async def pause_offers(self, offer_ids: list[int]) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            # Attempt 1: batch_pause (underscore, like batch_activate)
            url1 = f"{SELLER_OFFICE_V2_URL}/offers/batch_pause"
            body1 = {"offer_ids": offer_ids}
            resp = await client.post(url1, json=body1)
            print(
                f"[batch_pause] attempt=1 url={url1} body={body1} "
                f"status={resp.status_code} response={resp.text[:300]}",
                flush=True,
            )
            if resp.status_code != 404:
                resp.raise_for_status()
                return resp.json()

            # Attempt 2: batch/pause (slash) with {ids: [...]}
            url2 = f"{SELLER_OFFICE_V2_URL}/offers/batch/pause"
            body2 = {"ids": offer_ids}
            resp = await client.post(url2, json=body2)
            print(
                f"[batch_pause] attempt=2 url={url2} body={body2} "
                f"status={resp.status_code} response={resp.text[:300]}",
                flush=True,
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_offers(self, offer_ids: list[int]) -> dict:
        """POST /offers/batch_delete — delete up to 100 offers at once."""
        async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch_delete",
                json={"offer_ids": offer_ids},
            )
            print(
                f"[batch_delete] url={SELLER_OFFICE_V2_URL}/offers/batch_delete "
                f"count={len(offer_ids)} status={resp.status_code} "
                f"response={resp.text[:300]}",
                flush=True,
            )
            resp.raise_for_status()
            return resp.json()

    async def activate_offers(self, offer_ids: list[int]) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/offers/batch/activate",
                json={"offer_ids": offer_ids},
            )
            resp.raise_for_status()
            return resp.json()

    async def mark_delivered(self, order_id: int) -> dict:
        async with httpx.AsyncClient(headers=self._headers(), timeout=10) as client:
            resp = await client.post(
                f"{SELLER_OFFICE_V2_URL}/orders/{order_id}/deliveries/delivered"
            )
            resp.raise_for_status()
            return resp.json()

    async def get_order(self, order_id: int) -> dict:
        async with httpx.AsyncClient(
            headers={**self._headers(), "currency": "RUB"}, timeout=10
        ) as client:
            resp = await client.get(f"{SELLER_OFFICE_V2_URL}/orders/{order_id}")
            resp.raise_for_status()
            return resp.json()

    # ---- legacy purchase API (/api_sellers/api/purchases/*) --------------------
    # Auth here is ?token=... (a query param), NOT the Bearer Authorization header used by v2.
    # Base is .../api_sellers/api (v2 base is .../api_sellers/v2), so derive it from ggsel_base_url.
    def _purchases_base(self) -> str:
        return SELLER_OFFICE_V2_URL.rsplit("/", 1)[0] + "/api"

    def _purchase_token(self) -> str:
        return settings.ggsel_purchase_token or settings.ggsel_api_key

    _pt_token = None
    _pt_exp = 0.0

    async def _login_purchase_token(self):
        """POST /api_sellers/api/apilogin {seller_id, timestamp, sign=SHA256(key+ts)} -> token.
        Returns (token, expiry_epoch) or (None, 0.0) on failure."""
        import time as _t, hashlib as _hl
        from datetime import datetime as _dt
        ts = str(int(_t.time()))
        key = settings.ggsel_purchase_api_key or settings.ggsel_api_key
        sign = _hl.sha256((key + ts).encode()).hexdigest()
        url = f"{self._purchases_base()}/apilogin"
        body = {"seller_id": settings.ggsel_seller_id, "timestamp": ts, "sign": sign}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(url, json=body, headers={"Accept": "application/json"})
            data = r.json() if r.content else {}
        except Exception as e:
            print(f"[ggsel apilogin] error: {e}", flush=True)
            return None, 0.0
        tok = (data or {}).get("token") or (data or {}).get("access_token")
        if not tok:
            print(f"[ggsel apilogin] no token status={r.status_code} body={str(data)[:300]}", flush=True)
            return None, 0.0
        exp = _t.time() + 3600
        vt = (data or {}).get("valid_thru") or (data or {}).get("expires_at")
        if vt:
            try:
                exp = _dt.fromisoformat(str(vt).replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        return tok, exp

    async def purchase_token(self):
        """Cached token for the legacy purchase API. Manual override wins; else apilogin,
        cached in-process until ~2 min before expiry."""
        import time as _t
        if settings.ggsel_purchase_token:
            return settings.ggsel_purchase_token
        if self._pt_token and _t.time() < self._pt_exp - 120:
            return self._pt_token
        tok, exp = await self._login_purchase_token()
        if tok:
            self.__class__._pt_token = tok
            self.__class__._pt_exp = exp
        return tok

    async def resolve_unique_code(self, unique_code: str) -> dict | None:
        """Resolve a ggsel purchase uniquecode -> the purchase `content` object.
        GET /api_sellers/api/purchases/unique-code/{code}?token=...
        content.content_id is the ggsel order id (== our Order.ggsel_order_id). Returns the
        content dict, or None on any failure (caller falls back to the heuristic binder)."""
        url = f"{self._purchases_base()}/purchases/unique-code/{unique_code}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await self._request_retry(
                    client, "GET", url,
                    params={"token": await self.purchase_token()},
                    headers={"Accept": "application/json"},
                )
                if not resp.is_success:
                    print(f"[resolve_unique_code] code={unique_code!r} status={resp.status_code} "
                          f"body={resp.text[:300]}", flush=True)
                    return None
                data = resp.json()
        except Exception as e:
            print(f"[resolve_unique_code] code={unique_code!r} error: {e}", flush=True)
            return None
        if isinstance(data, dict):
            return data.get("content") if isinstance(data.get("content"), dict) else data
        return None

    async def get_purchase_info(self, invoice_id: int) -> dict | None:
        """GET /api_sellers/api/purchase/info/{invoice_id}?token=... -> purchase `content` object
        (used to VERIFY a heuristic candidate against a uniquecode). None on failure."""
        url = f"{self._purchases_base()}/purchase/info/{invoice_id}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await self._request_retry(
                    client, "GET", url,
                    params={"token": await self.purchase_token()},
                    headers={"Accept": "application/json"},
                )
                if not resp.is_success:
                    print(f"[get_purchase_info] invoice={invoice_id} status={resp.status_code} "
                          f"body={resp.text[:300]}", flush=True)
                    return None
                data = resp.json()
        except Exception as e:
            print(f"[get_purchase_info] invoice={invoice_id} error: {e}", flush=True)
            return None
        if isinstance(data, dict):
            return data.get("content") if isinstance(data.get("content"), dict) else data
        return None


ggsel_office = GgselSellerOfficeClient()
