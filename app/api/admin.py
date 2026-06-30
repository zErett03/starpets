"""Operator admin panel — list orders and manually manage delivery.

Built as a stop-gap while StarPets' wrapper doesn't emit terminal trade events
(status 6/7/8) yet: the monitor can't auto-close orders, so the operator closes
them here. Protected by HTTP Basic Auth (settings.admin_user / admin_password).

All mutations happen on OUR side; "close + mark delivered" additionally notifies
ggsel (MARK_DELIVERED) to release the buyer's payment.
"""

import secrets
from datetime import datetime
from html import escape as _esc

from fastapi import APIRouter, Depends, HTTPException, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.clients.starpets import starpets

router = APIRouter()
_security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    """Fail-closed Basic Auth. Denies everything if admin_password is unset."""
    user_ok = secrets.compare_digest(credentials.username, settings.admin_user)
    pass_ok = bool(settings.admin_password) and secrets.compare_digest(
        credentials.password, settings.admin_password
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="starpets-admin"'},
        )
    return credentials.username


# ---- delivery_status palette (dark theme) ----
_STATUS_COLORS = {
    "pending": "#d29922",
    "dispatched": "#58a6ff",
    "done": "#3fb950",
    "finalized": "#2ea043",
    "failed": "#f85149",
    "needs_attention": "#db61a2",
}


def _badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#8b949e")
    return (
        f'<span class="badge" style="background:{color}22;color:{color};'
        f'border:1px solid {color}55">{_esc(status)}</span>'
    )


def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(dt)


_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
a{color:#58a6ff;text-decoration:none}
header{padding:14px 20px;background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0;z-index:5}
h1{margin:0;font-size:16px;font-weight:600}
.sub{color:#8b949e;font-size:12px;margin-top:3px}
.filters{padding:10px 20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;border-bottom:1px solid #30363d}
.filters a{padding:4px 10px;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:12px}
.filters a.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
.filters .count{color:#8b949e}
.wrap{padding:14px 20px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:1100px}
th,td{padding:7px 9px;border-bottom:1px solid #21262d;text-align:left;vertical-align:top;white-space:nowrap}
th{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;background:#0d1117}
tr:hover td{background:#161b2233}
.badge{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.muted{color:#6e7681}
.err{color:#f85149;white-space:normal;max-width:240px;font-size:12px}
form.inline{display:inline-flex;gap:4px;align-items:center;margin:0}
select,input[type=text]{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 6px;font-size:12px}
input[type=text]{width:120px}
button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer}
button:hover{border-color:#8b949e}
button.go{background:#238636;border-color:#238636;color:#fff}
button.del{background:#21262d;border-color:#f8514955;color:#f85149}
button.warn{background:#21262d;border-color:#d2992255;color:#d29922}
.actions{display:flex;flex-direction:column;gap:5px}
.actions .row{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.note{padding:0 20px 18px;color:#6e7681;font-size:12px}
code{background:#161b22;padding:1px 5px;border-radius:4px;color:#c9d1d9}
"""


def _order_row(o) -> str:
    statuses = list(_STATUS_COLORS.keys())
    cur = o.delivery_status.value if o.delivery_status else ""
    opts = "".join(
        f'<option value="{s}"{" selected" if s == cur else ""}>{s}</option>'
        for s in statuses
    )
    uname = _esc(o.roblox_username or "")
    trade_id = _esc(o.starpets_custom_id or "—")
    purchase_id = _esc(o.starpets_purchase_id or "—")
    sp_status = _esc(o.starpets_status or "—")
    amount = f"{o.amount_rub}₽" if o.amount_rub is not None else "—"

    return f"""<tr>
  <td>{o.id}</td>
  <td>{_esc(str(o.ggsel_order_id))}</td>
  <td>{_esc(o.item_name or "—")}</td>
  <td>
    <form class="inline" method="post" action="/admin/edit-username">
      <input type="hidden" name="order_id" value="{o.id}">
      <input type="text" name="username" value="{uname}" placeholder="roblox ник">
      <button type="submit" title="Сохранить ник">✓</button>
    </form>
  </td>
  <td>{_badge(cur)}</td>
  <td class="muted">{sp_status}</td>
  <td class="muted">{trade_id}</td>
  <td class="muted">{purchase_id}</td>
  <td>{_esc(o.bot_name or "—")}</td>
  <td class="muted">{amount}</td>
  <td class="muted">{_fmt_dt(o.created_at)}</td>
  <td class="err">{_esc(o.error_reason or "")}</td>
  <td>
    <div class="actions">
      <div class="row">
        <form class="inline" method="post" action="/admin/set-status">
          <input type="hidden" name="order_id" value="{o.id}">
          <select name="status">{opts}</select>
          <button type="submit" class="go">Статус</button>
        </form>
      </div>
      <div class="row">
        <form class="inline" method="post" action="/admin/mark-delivered"
              onsubmit="return confirm('Закрыть заказ {o.id} и отметить доставку в ggsel (высвободит оплату)?')">
          <input type="hidden" name="order_id" value="{o.id}">
          <button type="submit" class="go">Закрыть + ggsel</button>
        </form>
        <form class="inline" method="post" action="/admin/redeliver"
              onsubmit="return confirm('Перезапустить доставку заказа {o.id}? Будет создан новый трейд (предмет переиспользуется).')">
          <input type="hidden" name="order_id" value="{o.id}">
          <button type="submit" class="warn">Доставить заново</button>
        </form>
      </div>
      <div class="row">
        <form class="inline" method="post" action="/admin/cancel"
              onsubmit="return confirm('Отменить заказ {o.id}? (статус → failed; возврат на ggsel пока вручную)')">
          <input type="hidden" name="order_id" value="{o.id}">
          <button type="submit" class="del">Отмена</button>
        </form>
        <a href="/admin/warehouse-check?order_id={o.id}" target="_blank" title="Проверить, ушёл ли предмет с нашего склада StarPets">🏬 Склад</a>
      </div>
    </div>
  </td>
</tr>"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_orders(
    _user: str = Depends(require_admin),
    status: str = Query(None),
):
    from sqlalchemy import select, func
    from app.db import AsyncSessionLocal
    from app.db.models import Order, DeliveryStatus

    async with AsyncSessionLocal() as db:
        counts_res = await db.execute(
            select(Order.delivery_status, func.count()).group_by(Order.delivery_status)
        )
        counts = {row[0].value: row[1] for row in counts_res}
        total = sum(counts.values())

        q = select(Order).order_by(Order.created_at.desc()).limit(500)
        if status and status in _STATUS_COLORS:
            q = (
                select(Order)
                .where(Order.delivery_status == DeliveryStatus(status))
                .order_by(Order.created_at.desc())
                .limit(500)
            )
        rows_res = await db.execute(q)
        orders = rows_res.scalars().all()

    # filter bar
    def _f(label, value, n):
        active = " active" if (value or None) == (status or None) else ""
        href = "/admin" if value is None else f"/admin?status={value}"
        cnt = f' <span class="count">{n}</span>' if n is not None else ""
        return f'<a class="filter{active}" href="{href}">{label}{cnt}</a>'

    filters = [_f("Все", None, total)]
    for s in _STATUS_COLORS:
        filters.append(_f(s, s, counts.get(s, 0)))
    filters_html = "".join(filters)

    rows_html = "".join(_order_row(o) for o in orders) or (
        '<tr><td colspan="13" class="muted" style="padding:24px;text-align:center">'
        "Заказов нет</td></tr>"
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StarPets — заказы</title><style>{_CSS}</style></head>
<body>
<header>
  <h1>Заказы StarPets</h1>
  <div class="sub">Операторская панель · показано {len(orders)} из {total} · обновлено {datetime.utcnow().strftime("%H:%M:%S")} UTC</div>
</header>
<div class="filters">{filters_html}</div>
<div class="wrap">
<table>
  <thead><tr>
    <th>ID</th><th>ggsel</th><th>Товар</th><th>Roblox ник</th><th>Статус</th>
    <th>SP&nbsp;статус</th><th>trade_id</th><th>purchase_id</th><th>Бот</th>
    <th>Сумма</th><th>Создан</th><th>Ошибка</th><th>Действия</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
</div>
<div class="note">
  «Закрыть + ggsel» ставит <code>done</code> и сообщает маркетплейсу о доставке (высвобождает оплату).
  «Отмена» ставит <code>failed</code> — автоматического возврата на ggsel пока нет.
  «🏬 Склад» (экспериментально) запрашивает у StarPets предмет заказа, чтобы проверить, ушёл ли он с нашего склада.
</div>
</body></html>""")


def _back(request: Request) -> RedirectResponse:
    ref = request.headers.get("referer") or "/admin"
    return RedirectResponse(ref, status_code=303)


@router.post("/admin/set-status")
async def admin_set_status(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
    status: str = Form(...),
):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, DeliveryStatus

    if status not in _STATUS_COLORS:
        raise HTTPException(400, f"unknown status {status}")
    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        order.delivery_status = DeliveryStatus(status)
        if status in ("done", "finalized") and order.delivered_at is None:
            order.delivered_at = datetime.utcnow()
        order.updated_at = datetime.utcnow()
        await db.commit()
    print(f"[admin] order_id={order_id} status → {status}", flush=True)
    return _back(request)


@router.post("/admin/mark-delivered")
async def admin_mark_delivered(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Task, TaskKind, DeliveryStatus

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        order.delivery_status = DeliveryStatus.done
        if order.delivered_at is None:
            order.delivered_at = datetime.utcnow()
        order.updated_at = datetime.utcnow()
        db.add(Task(kind=TaskKind.MARK_DELIVERED, priority=1, payload={"order_id": order_id}))
        await db.commit()
    print(f"[admin] order_id={order_id} closed → done + MARK_DELIVERED queued", flush=True)
    return _back(request)


@router.post("/admin/redeliver")
async def admin_redeliver(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, Task, TaskKind, DeliveryStatus

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        order.delivery_status = DeliveryStatus.pending
        order.error_reason = None
        order.updated_at = datetime.utcnow()
        db.add(Task(kind=TaskKind.DELIVER, priority=1, max_attempts=3, payload={"order_id": order_id}))
        await db.commit()
    print(f"[admin] order_id={order_id} re-deliver queued", flush=True)
    return _back(request)


@router.post("/admin/edit-username")
async def admin_edit_username(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
    username: str = Form(""),
):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        old = order.roblox_username
        order.roblox_username = username.strip() or None
        order.updated_at = datetime.utcnow()
        await db.commit()
    print(f"[admin] order_id={order_id} username {old!r} → {username!r}", flush=True)
    return _back(request)


@router.post("/admin/cancel")
async def admin_cancel(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, DeliveryStatus

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        order.delivery_status = DeliveryStatus.failed
        order.error_reason = "canceled by admin"
        order.updated_at = datetime.utcnow()
        await db.commit()
    print(f"[admin] order_id={order_id} canceled (→ failed)", flush=True)
    return _back(request)


@router.get("/admin/warehouse-check")
async def admin_warehouse_check(
    _user: str = Depends(require_admin),
    order_id: int = Query(...),
):
    """Experimental anti-scam check: query StarPets for the order's purchased item.

    There's no documented "my inventory" endpoint, so we probe GET /items by the
    purchased_item_id and return whatever StarPets says. Once we see real response
    shapes we can turn this into a clear "still in stock / left our warehouse" flag.
    """
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        purchase_id = order.starpets_purchase_id

    if not purchase_id:
        return JSONResponse({
            "order_id": order_id,
            "note": "у заказа нет starpets_purchase_id — покупка не зафиксирована",
        })

    try:
        result = await starpets.get_items([str(purchase_id)])
    except Exception as e:
        return JSONResponse({
            "order_id": order_id,
            "purchased_item_id": purchase_id,
            "error": str(e),
            "note": "StarPets вернул ошибку на /items — возможно, предмета уже нет у нас (ушёл покупателю)",
        })

    return JSONResponse({
        "order_id": order_id,
        "purchased_item_id": purchase_id,
        "starpets_status": order.starpets_status,
        "delivery_status": order.delivery_status.value if order.delivery_status else None,
        "items_response": result,
        "note": "experimental: интерпретация ответа уточняется. Если предмет в ответе отсутствует/принадлежит другому — он ушёл с нашего склада.",
    })
