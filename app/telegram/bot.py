"""Telegram admin bot (v1).

Receives commands via a Telegram webhook (see /telegram/webhook/<secret> in the API) and pushes
new-order + problem notifications. Access is restricted to a whitelist of Telegram user ids
(TELEGRAM_ADMIN_IDS). Powerful actions (retry / force-deliver) go through inline buttons with a
confirmation step; force-deliver first shows the live cost + loss and only buys on a second tap.

Design: the bot is a thin Telegram front-end over the app's existing HTTP endpoints (it calls
them on 127.0.0.1) plus a couple of direct DB reads for the detailed /order debug report — so it
reuses all existing logic and stays in sync automatically.
"""
import html

import httpx
from sqlalchemy import select, func

from app.config import settings
from app.db import AsyncSessionLocal
from app.db.models import Order, SkuVariant, SkuProduct, Offer, DeliveryStatus

_API = lambda method: f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
_BASE = "http://127.0.0.1:8000"   # self-call the app's own endpoints


# ---------------------------------------------------------------- helpers ----
def _admin_ids() -> set[int]:
    out: set[int] = set()
    for x in (settings.telegram_admin_ids or "").replace(";", ",").split(","):
        x = x.strip()
        if x.isdigit():
            out.add(int(x))
    return out


def _orders_chats() -> list[str]:
    """Chats that receive new-order + problem alerts. TELEGRAM_CHAT_ID_ORDERS (comma-separated)
    if set, else EVERY whitelisted admin id — so notifications reach several people."""
    raw = settings.telegram_chat_id_orders or ""
    chats = [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]
    if chats:
        return chats
    return [str(i) for i in sorted(_admin_ids())]


def is_authorized(user_id) -> bool:
    try:
        return int(user_id) in _admin_ids()
    except (TypeError, ValueError):
        return False


async def send_message(chat_id, text: str, buttons: list | None = None) -> None:
    if not settings.telegram_bot_token or settings.telegram_bot_token == "dummy":
        print(f"[tg] (no token) → {chat_id}: {text[:120]}", flush=True)
        return
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(_API("sendMessage"), json=payload)
            if not r.is_success:
                print(f"[tg] sendMessage failed {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[tg] sendMessage error: {e}", flush=True)


async def _answer_callback(cb_id: str, text: str = "") -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(_API("answerCallbackQuery"),
                         json={"callback_query_id": cb_id, "text": text[:200]})
    except Exception as e:
        print(f"[tg] answerCallback error: {e}", flush=True)


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _order_buttons(order) -> list:
    oid = order.id
    gg = order.ggsel_order_id
    rows = [[_btn("🔁 Повторить", f"retry:{oid}"), _btn("💥 Force", f"force:{oid}")]]
    if order.uniquecode:
        rows.append([{"text": "🔗 Заказ на ggsel",
                      "url": f"https://payment.ggsel.com/order/{order.uniquecode}"}])
    return rows


async def _get_json(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{_BASE}{path}")
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:500], "_status": r.status_code}


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else "—"


# --------------------------------------------------------------- commands ----
async def cmd_help(chat_id, arg):
    await send_message(chat_id,
        "<b>Команды StarPets-бота</b>\n"
        "/balance — баланс StarPets\n"
        "/order &lt;id|ggsel_id&gt; — детальная карточка заказа + действия\n"
        "/attention — заказы в needs_attention/failed\n"
        "/stock &lt;ggsel_offer_id&gt; — наличие и цены вариантов\n"
        "/drift — сколько карточек с крупным ценовым дрейфом\n"
        "/floorsweep — пересчёт цен из robust floor (dry-run)\n"
        "/health — состояние системы\n"
        "/help — эта справка")


async def cmd_balance(chat_id, arg):
    from app.clients.starpets import starpets
    try:
        info = await starpets.get_info()
        bal = (info.get("buyer") or {}).get("balance")
        await send_message(chat_id, f"💰 Баланс StarPets: <b>${_esc(bal)}</b>")
    except Exception as e:
        await send_message(chat_id, f"⚠️ Не удалось получить баланс: {_esc(e)}")


async def cmd_order(chat_id, arg):
    if not arg or not arg.strip().isdigit():
        await send_message(chat_id, "Использование: <code>/order &lt;внутренний id или ggsel id&gt;</code>")
        return
    oid = int(arg.strip())
    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == oid))).scalar_one_or_none()
        if not order:
            order = (await db.execute(
                select(Order).where(Order.ggsel_order_id == oid))).scalar_one_or_none()
        if not order:
            await send_message(chat_id, f"Заказ <code>{oid}</code> не найден (искал по id и ggsel id).")
            return
        product = variant = card_gid = None
        if order.sku_product_id:
            product = (await db.execute(select(SkuProduct)
                       .where(SkuProduct.product_id == order.sku_product_id))).scalar_one_or_none()
            variant = (await db.execute(select(SkuVariant)
                       .where(SkuVariant.starpets_product_id == order.sku_product_id)
                       .limit(1))).scalar_one_or_none()
            if variant:
                card_gid = variant.ggsel_offer_id
        if card_gid is None and order.offer_id:
            card_gid = (await db.execute(select(Offer.ggsel_offer_id)
                        .where(Offer.id == order.offer_id))).scalar_one_or_none()

    # exact variant name from SkuProduct attributes
    exact = "—"
    if product:
        bits = [product.name]
        for extra in (product.rare, product.age, product.pumping):
            if extra:
                bits.append(str(extra))
        flags = []
        if product.flyable:
            flags.append("Летает")
        if product.rideable:
            flags.append("Ездовой")
        exact = " · ".join(bits + flags)

    st = order.delivery_status.value if order.delivery_status else "—"
    icon = {"done": "🟢", "finalized": "🟢", "dispatched": "🔵",
            "pending": "🟡", "needs_attention": "🟠", "failed": "🔴"}.get(st, "⚪")
    lines = [
        f"{icon} <b>Заказ #{order.id}</b> — {_esc(order.item_name)}",
        f"Статус: <b>{_esc(st)}</b>  ·  SP-статус: <code>{_esc(order.starpets_status)}</code>",
        "",
        "<b>Debug</b>",
        f"• ggsel order id: <code>{_esc(order.ggsel_order_id)}</code>",
        f"• внутренний id: <code>{order.id}</code>",
        f"• трейд id: <code>{_esc(order.starpets_custom_id)}</code>",
        f"• предмет StarPets (purchase): <code>{_esc(order.starpets_purchase_id)}</code>",
        f"• sku_product_id: <code>{_esc(order.sku_product_id)}</code>",
        f"• sku-вариант id: <code>{_esc(variant.id if variant else None)}</code>  "
        f"(ggsel_variant <code>{_esc(variant.ggsel_variant_id if variant else None)}</code>)",
        f"• карточка ggsel: <code>{_esc(card_gid)}</code>",
        f"• точный вариант: {_esc(exact)}",
        f"• label варианта: {_esc(variant.label if variant else None)}",
        "",
        "<b>Доставка</b>",
        f"• бот: <code>{_esc(order.bot_name)}</code>",
        f"• покупатель: <code>{_esc(order.roblox_username)}</code>  "
        f"({_esc(order.buyer_email)})",
        f"• сумма: <b>{_esc(order.amount_rub)}₽</b>  ·  выкуп: "
        f"${_esc(order.exec_price_usd)}",
        f"• ретраи трейда: {_esc(order.trade_retry_count)}",
        f"• ошибка: {_esc(order.error_reason)}",
    ]
    if order.last_redeliver_result:
        lines.append(f"• последнее пересоздание: {_esc(order.last_redeliver_result)}")
    lines += [
        "",
        f"создан {_esc(order.created_at)}  ·  оплачен {_esc(order.paid_at)}",
    ]
    await send_message(chat_id, "\n".join(lines), buttons=_order_buttons(order))


async def cmd_attention(chat_id, arg):
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Order.id, Order.ggsel_order_id, Order.item_name,
                   Order.delivery_status, Order.error_reason)
            .where(Order.delivery_status.in_([DeliveryStatus.needs_attention, DeliveryStatus.failed]))
            .order_by(Order.id.desc()).limit(15)
        )).all()
    if not rows:
        await send_message(chat_id, "✅ Нет заказов в needs_attention/failed.")
        return
    parts = ["<b>Заказы, требующие внимания</b>"]
    for (oid, gg, name, stt, err) in rows:
        stv = stt.value if stt else "—"
        parts.append(f"\n🟠 <b>#{oid}</b> ({_esc(gg)}) — {_esc(name)}\n"
                     f"   {stv}: {_esc((err or '')[:80])}\n"
                     f"   <code>/order {oid}</code>")
    await send_message(chat_id, "\n".join(parts))


async def cmd_stock(chat_id, arg):
    if not arg or not arg.strip().isdigit():
        await send_message(chat_id, "Использование: <code>/stock &lt;ggsel_offer_id&gt;</code>")
        return
    data = await _get_json(f"/sku-card-stock?ggsel_offer_id={arg.strip()}")
    variants = data.get("variants_detail") or []
    if not variants:
        await send_message(chat_id, f"Нет данных по карточке {_esc(arg)} ({_esc(data)[:200]}).")
        return
    head = (f"<b>Карточка {data.get('ggsel_offer_id')}</b> — "
            f"вариантов {data.get('variants')}, в наличии {data.get('in_stock_count')}")
    lines = [head]
    for v in variants[:25]:
        mark = "✅" if v.get("in_stock") else "⛔"
        floor = v.get("live_floor_usd")
        lines.append(f"{mark} {_esc(v.get('label'))} — {_esc(v.get('price_rub'))}₽  "
                     f"(floor ${_esc(floor)})")
    await send_message(chat_id, "\n".join(lines))


async def cmd_drift(chat_id, arg):
    data = await _get_json("/sku-price-sync?dry_run=true&threshold_rub=500&threshold_pct=0.30")
    checked = data.get("cards_checked")
    drifted = data.get("drifted")
    await send_message(chat_id,
        f"📊 Крупный ценовой дрейф (порог 500₽/30%):\n"
        f"• проверено карточек: <b>{_esc(checked)}</b>\n"
        f"• дрейфует: <b>{_esc(drifted)}</b>\n\n"
        f"0 — цены здоровы. Много — нужен пролив Phase 3.")


async def cmd_floorsweep(chat_id, arg):
    data = await _get_json("/floor-sweep?dry_run=true")
    await send_message(chat_id,
        f"🧮 Floor-sweep (dry-run):\n"
        f"• офферов проверено: <b>{_esc(data.get('offers_checked'))}</b>\n"
        f"• дрейфует: <b>{_esc(data.get('drifted'))}</b>\n"
        f"• без стока: {_esc(data.get('no_stock'))}\n\n"
        f"Запустить реальный пересчёт цен?",
        buttons=[[_btn("▶️ Запустить floor-sweep", "floorsweep_run")]])


async def cmd_health(chat_id, arg):
    async with AsyncSessionLocal() as db:
        pend = (await db.execute(select(func.count()).select_from(Order)
                .where(Order.delivery_status == DeliveryStatus.pending))).scalar()
        att = (await db.execute(select(func.count()).select_from(Order)
               .where(Order.delivery_status.in_(
                   [DeliveryStatus.needs_attention, DeliveryStatus.failed])))).scalar()
        disp = (await db.execute(select(func.count()).select_from(Order)
                .where(Order.delivery_status == DeliveryStatus.dispatched))).scalar()
    fx = "—"
    try:
        from app.fx import get_usd_rub
        fx = await get_usd_rub()
    except Exception:
        pass
    await send_message(chat_id,
        "<b>Состояние системы</b>\n"
        f"• pending: {_esc(pend)}\n"
        f"• dispatched: {_esc(disp)}\n"
        f"• needs_attention/failed: {_esc(att)}\n"
        f"• курс USD/RUB: {_esc(fx)}")


_COMMANDS = {
    "start": cmd_help, "help": cmd_help, "balance": cmd_balance, "order": cmd_order,
    "attention": cmd_attention, "stock": cmd_stock, "drift": cmd_drift,
    "floorsweep": cmd_floorsweep, "health": cmd_health,
}


# -------------------------------------------------------------- callbacks ----
async def _handle_callback(cb: dict) -> None:
    user_id = (cb.get("from") or {}).get("id")
    cb_id = cb.get("id")
    chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
    data = cb.get("data") or ""
    if not is_authorized(user_id):
        await _answer_callback(cb_id, "Нет доступа")
        return

    action, _, arg = data.partition(":")
    if action == "retry":
        res = await _get_json(f"/retry-delivery?order_id={arg}")
        await _answer_callback(cb_id, "Повтор поставлен в очередь")
        await send_message(chat_id, f"🔁 Повтор заказа {_esc(arg)}: <code>{_esc(res)[:250]}</code>")
    elif action == "force":
        # step 1: preview (no confirm) — shows live cost + loss
        res = await _get_json(f"/force-deliver?order_id={arg}")
        await _answer_callback(cb_id)
        live = res.get("live") or {}
        msg = (f"💥 <b>Force-deliver заказа {_esc(arg)}</b>\n"
               f"• себестоимость: {_esc(live.get('live_cost_rub'))}₽\n"
               f"• убыток: {_esc(live.get('est_loss_rub'))}₽\n"
               f"• прибыльно: {_esc(live.get('profitable'))}\n\n"
               f"Подтвердить выкуп?") if live else \
              f"💥 Force preview заказа {_esc(arg)}:\n<code>{_esc(res)[:300]}</code>\n\nПодтвердить?"
        await send_message(chat_id, msg,
                           buttons=[[_btn("✅ Подтвердить выкуп", f"forceok:{arg}")]])
    elif action == "forceok":
        res = await _get_json(f"/force-deliver?order_id={arg}&confirm=true")
        await _answer_callback(cb_id, "Выкуп запущен")
        await send_message(chat_id, f"💥 Force заказа {_esc(arg)}: <code>{_esc(res)[:250]}</code>")
    elif action == "floorsweep_run":
        res = await _get_json("/floor-sweep?dry_run=false")
        await _answer_callback(cb_id, "Floor-sweep запущен")
        await send_message(chat_id, f"🧮 Floor-sweep: <code>{_esc(res)[:200]}</code>")
    else:
        await _answer_callback(cb_id, "Неизвестное действие")


# ------------------------------------------------------------- dispatcher ----
async def handle_update(update: dict) -> None:
    """Entry point for a Telegram webhook update."""
    try:
        if "callback_query" in update:
            await _handle_callback(update["callback_query"])
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        user_id = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not is_authorized(user_id):
            # stay silent to strangers (don't reveal the bot's purpose)
            return
        if not text.startswith("/"):
            await send_message(chat_id, "Команда не распознана. /help — список команд.")
            return
        cmd, _, arg = text[1:].partition(" ")
        cmd = cmd.split("@")[0].lower()   # strip @botname in groups
        handler = _COMMANDS.get(cmd)
        if handler:
            await handler(chat_id, arg)
        else:
            await send_message(chat_id, f"Неизвестная команда /{_esc(cmd)}. /help — список.")
    except Exception as e:
        print(f"[tg] handle_update error: {e}", flush=True)


# ----------------------------------------------------------- notifications ---
async def notify_new_order(order) -> None:
    text = (f"🆕 <b>Новый заказ #{order.id}</b>\n"
            f"{_esc(order.item_name)}\n"
            f"покупатель: <code>{_esc(order.roblox_username)}</code>  ·  "
            f"{_esc(order.amount_rub)}₽\n"
            f"ggsel: <code>{_esc(order.ggsel_order_id)}</code>\n"
            f"<code>/order {order.id}</code>")
    for chat in _orders_chats():
        await send_message(chat, text)


async def notify_problem(order) -> None:
    st = order.delivery_status.value if order.delivery_status else "—"
    text = (f"🔴 <b>Заказ #{order.id} → {_esc(st)}</b>\n"
            f"{_esc(order.item_name)}\n"
            f"причина: {_esc((order.error_reason or '')[:150])}\n"
            f"покупатель: <code>{_esc(order.roblox_username)}</code>")
    btns = _order_buttons(order)
    for chat in _orders_chats():
        await send_message(chat, text, buttons=btns)


async def notify_new_order_by_id(order_id: int) -> None:
    """Background-safe: fetch the order in a fresh session and push a 'new order' message."""
    try:
        async with AsyncSessionLocal() as db:
            order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
            if order:
                await notify_new_order(order)   # attrs are loaded within this session
    except Exception as e:
        print(f"[tg] notify_new_order_by_id error: {e}", flush=True)
