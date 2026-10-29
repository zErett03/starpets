"""
Бухгалтерия по заказам.

Считает выручку, закуп, комиссию и прибыль за период, плюс показывает деньги, застрявшие
в проблемных заказах.

ВАЖНОЕ ОГРАНИЧЕНИЕ. У заказа хранится ОДНА цена выкупа (exec_price_usd). Если предмет
перекупали — а мы это делаем при протухании и застрявших трейдах — предыдущая цена
затирается, и фактические затраты занижены. Точную картину дал бы журнал покупок, как
сделано в mmo-api; здесь его нет.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.db.models import DeliveryStatus, Order

router = APIRouter()

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

# Заказ считается принёсшим деньги только в этих статусах.
_DELIVERED = {DeliveryStatus.done, DeliveryStatus.finalized}
# Деньги покупателю уже вернули: closed — вручную оператором, failed — сорванная выдача.
# Выручки нет. Закуп при этом обычно НЕ теряется: если предмет не выдан и протух,
# StarPets возвращает его стоимость на баланс API. Реальный убыток — только комиссии,
# поэтому формула в таблице (-(H+I), без себестоимости) верна.
_REFUNDED = {DeliveryStatus.failed, DeliveryStatus.closed}
# Здесь деньги покупателя получены, но выдача не закрыта — риск возврата.
_AT_RISK = {DeliveryStatus.needs_attention}


def _period(since: str, until: str, days: int) -> tuple[datetime, datetime, str]:
    """Границы периода. Явные даты важнее относительных дней.

    Отбор идёт по дате ОПЛАТЫ, а не создания: бухгалтерия про деньги, а заказ мог быть
    заведён precheck-ом заранее.
    """
    def _parse(s: str, default: datetime) -> datetime:
        s = (s or "").strip()
        if not s:
            return default
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return default

    end = _parse(until, datetime.utcnow() + timedelta(days=1))
    if since.strip():
        start = _parse(since, datetime(1970, 1, 1))
    elif days > 0:
        start = datetime.utcnow() - timedelta(days=days)
    else:
        start = datetime(1970, 1, 1)
    label = f"{start:%d.%m.%Y} — {min(end, datetime.utcnow()):%d.%m.%Y}"
    return start, end, label


async def _usd_rate() -> tuple[float, str]:
    """Курс доллара ЦБ. Закуп у StarPets идёт в долларах, выручка в рублях."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(CBR_URL)
            resp.raise_for_status()
            v = (resp.json().get("Valute") or {}).get("USD") or {}
        rate = float(v.get("Value")) / float(v.get("Nominal") or 1)
        return rate, "cbr"
    except Exception:  # noqa: BLE001
        return 0.0, "unavailable"


@router.get("/accounting")
async def accounting(since: str = "", until: str = "", days: int = 30,
                     usd_rub: float = 0.0, commission: float = 0.028):
    """
    Итоги за период по дате оплаты. usd_rub=0 — курс ЦБ на сегодня.

    Комиссия по умолчанию 2.8% — платёжная система 2.7% плюс ggsel 0.1%, как в ручной
    таблице. Если фактические удержания окажутся иными, передай параметром.
    """
    rate, rate_src = (usd_rub, "manual") if usd_rub > 0 else await _usd_rate()
    start, end, period_label = _period(since, until, days)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Order).where(Order.paid_at.isnot(None),
                                Order.paid_at >= start, Order.paid_at < end)
        )).scalars().all()

    delivered_n = at_risk_n = in_flight_n = unpaid_n = refunded_n = 0
    delivered_rev = delivered_cost_usd = 0.0
    at_risk_rev = at_risk_cost_usd = 0.0
    refunded_rev = refunded_cost_usd = 0.0
    spent_usd_total = 0.0

    for o in rows:
        sale = float(o.amount_rub or 0)
        cost_usd = float(o.exec_price_usd or 0)
        spent_usd_total += cost_usd
        if sale <= 0:
            # precheck заводит заказ на каждую попытку покупки; оплаченные имеют сумму.
            unpaid_n += 1
            continue
        if o.delivery_status in _DELIVERED:
            delivered_n += 1
            delivered_rev += sale
            delivered_cost_usd += cost_usd
        elif o.delivery_status in _REFUNDED:
            refunded_n += 1
            refunded_rev += sale
            refunded_cost_usd += cost_usd
        elif o.delivery_status in _AT_RISK:
            at_risk_n += 1
            at_risk_rev += sale
            at_risk_cost_usd += cost_usd
        else:
            in_flight_n += 1

    delivered_cost_rub = delivered_cost_usd * rate
    fee_rub = delivered_rev * commission
    profit = delivered_rev - fee_rub - delivered_cost_rub
    margin = (profit / delivered_rev) if delivered_rev else 0.0

    return {
        "period": period_label,
        "usd_rub": round(rate, 4),
        "usd_rub_source": rate_src,
        "commission": commission,
        "delivered": {
            "orders": delivered_n,
            "revenue_rub": round(delivered_rev, 2),
            "ggsel_fee_rub": round(fee_rub, 2),
            "purchase_cost_usd": round(delivered_cost_usd, 3),
            "purchase_cost_rub": round(delivered_cost_rub, 2),
            "profit_rub": round(profit, 2),
            "margin": round(margin, 4),
            "avg_check_rub": round(delivered_rev / delivered_n, 2) if delivered_n else 0,
            "avg_profit_rub": round(profit / delivered_n, 2) if delivered_n else 0,
        },
        "at_risk": {
            "orders": at_risk_n,
            "revenue_held_rub": round(at_risk_rev, 2),
            "purchase_cost_rub": round(at_risk_cost_usd * rate, 2),
            "note": ("оплачено покупателем, но выдача не закрыта: деньги под возвратом, "
                     "а предмет, возможно, уже куплен"),
        },
        "refunded": {
            "orders": refunded_n,
            "returned_rub": round(refunded_rev, 2),
            "purchase_cost_rub": round(refunded_cost_usd * rate, 2),
            "note": ("деньги возвращены покупателю, выручки нет. Стоимость закупа обычно "
                     "возвращается StarPets на баланс API, поэтому purchase_cost_rub — "
                     "не убыток, а справка. Убыток реален лишь если предмет успели выдать"),
        },
        "in_flight_orders": in_flight_n,
        "unpaid_precheck_orders": unpaid_n,
        "total_spent_usd_incl_failed": round(spent_usd_total, 3),
        "caveats": [
            "у заказа хранится ОДНА цена выкупа: перекупы (rebuy-fresh) затирают "
            "предыдущую, поэтому реальные затраты выше показанных",
            "курс берётся текущий и применяется ко всем заказам периода, хотя покупались "
            "они по курсам своих дней",
            "комиссия считается по ставке из параметра, фактические удержания ggsel "
            "могут отличаться — сверь с выпиской кабинета",
        ],
    }


@router.get("/accounting/xlsx")
async def accounting_xlsx(since: str = "", until: str = "", days: int = 0,
                          usd_rub: float = 0.0,
                          fee_ps: float = 0.027, fee_ggsel: float = 0.001):
    """
    Выгрузка в тот же формат, что и ручная таблица: три листа, формулы, параметры.

    Формулы сохранены живыми, а не посчитанными — чтобы можно было поменять комиссию или
    курс в «Параметрах» и увидеть пересчёт, как в исходном файле.

    days=0 — все заказы.
    """
    import io
    from fastapi.responses import StreamingResponse
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        return {"error": "openpyxl не установлен — добавь его в requirements.txt"}

    rate, rate_src = (usd_rub, "manual") if usd_rub > 0 else await _usd_rate()
    start, end, period_label = _period(since, until, days)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Order)
            .where(Order.paid_at.isnot(None), Order.paid_at >= start, Order.paid_at < end)
            .order_by(Order.paid_at.asc())
        )).scalars().all()

    # В таблице всего два учитываемых статуса. Остальные заказы попадают в файл, но в
    # суммы не входят: они ещё не закрыты, и записывать их в прибыль или убыток рано.
    def _status(o) -> str:
        if o.delivery_status in _DELIVERED:
            return "Доставлен"
        if o.delivery_status in _REFUNDED:      # failed + closed (возврат оператором)
            return "Возврат"
        return "В работе"

    wb = Workbook()
    ws = wb.active
    ws.title = "Заказы"
    headers = ["№ заказа ggsel", "Товар", "Цена продажи P, ₽", "Курс USD/RUB",
               "Себест. выкупа, $", "Статус", "Себест., ₽", "Комиссия ПС, ₽",
               "Комиссия ggsel, ₽", "Net-выручка, ₽", "Прибыль, ₽", "Маржа %",
               "Оплачен"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    n = 0
    for o in rows:
        sale = float(o.amount_rub or 0)
        if sale <= 0:
            continue          # precheck-заготовки без оплаты в бухгалтерию не идут
        n += 1
        r = n + 1
        ws.append([
            o.ggsel_order_id, o.item_name, sale, round(rate, 4),
            float(o.exec_price_usd or 0), _status(o),
            f"=E{r}*D{r}",
            f"=C{r}*'Параметры'!$B$4",
            f"=C{r}*'Параметры'!$B$5",
            f"=C{r}-H{r}-I{r}",
            f'=IF($F{r}="Возврат",-(H{r}+I{r}),J{r}-G{r})',
            f"=IF(C{r}=0,0,K{r}/C{r})",
            o.paid_at.strftime("%d.%m.%Y %H:%M") if o.paid_at else "",
        ])

    last = n + 1
    s = wb.create_sheet("Сводка")
    s["A1"] = "Сводка (P&L)"
    s["A1"].font = Font(bold=True, size=13)
    s["A2"] = f"Период по дате оплаты: {period_label}"
    pairs = [
        (3, "Заказов всего", f"=COUNTA('Заказы'!$A$2:$A${last})"),
        (4, "  из них доставлено", f"=COUNTIF('Заказы'!$F$2:$F${last},\"Доставлен\")"),
        (5, "  из них возвраты", f"=COUNTIF('Заказы'!$F$2:$F${last},\"Возврат\")"),
        (6, "  из них в работе", f"=COUNTIF('Заказы'!$F$2:$F${last},\"В работе\")"),
        (8, "Валовая выручка (доставленные)",
            f"=SUMIFS('Заказы'!$C$2:$C${last},'Заказы'!$F$2:$F${last},\"Доставлен\")"),
        (9, "Комиссии всего (доставленные)",
            f"=SUMIFS('Заказы'!$H$2:$H${last},'Заказы'!$F$2:$F${last},\"Доставлен\")"
            f"+SUMIFS('Заказы'!$I$2:$I${last},'Заказы'!$F$2:$F${last},\"Доставлен\")"),
        (10, "Net-выручка (доставленные)",
             f"=SUMIFS('Заказы'!$J$2:$J${last},'Заказы'!$F$2:$F${last},\"Доставлен\")"),
        (11, "Себестоимость (доставленные)",
             f"=SUMIFS('Заказы'!$G$2:$G${last},'Заказы'!$F$2:$F${last},\"Доставлен\")"),
        (12, "Убыток от возвратов",
             f"=-SUMIFS('Заказы'!$K$2:$K${last},'Заказы'!$F$2:$F${last},\"Возврат\")"),
        (14, "ЧИСТАЯ ПРИБЫЛЬ",
             f"=SUMIFS('Заказы'!$K$2:$K${last},'Заказы'!$F$2:$F${last},\"Доставлен\")"
             f"+SUMIFS('Заказы'!$K$2:$K${last},'Заказы'!$F$2:$F${last},\"Возврат\")"),
        (15, "Средняя маржа (доставленные)",
             f"=IFERROR((SUMIFS('Заказы'!$J$2:$J${last},'Заказы'!$F$2:$F${last},\"Доставлен\")"
             f"-SUMIFS('Заказы'!$G$2:$G${last},'Заказы'!$F$2:$F${last},\"Доставлен\"))"
             f"/SUMIFS('Заказы'!$C$2:$C${last},'Заказы'!$F$2:$F${last},\"Доставлен\"),0)"),
    ]
    for row, label, formula in pairs:
        s.cell(row=row, column=1, value=label)
        s.cell(row=row, column=2, value=formula)
    s["A14"].font = Font(bold=True)
    s["B14"].font = Font(bold=True)
    s.column_dimensions["A"].width = 34
    s.column_dimensions["B"].width = 18

    p = wb.create_sheet("Параметры")
    yellow = PatternFill("solid", fgColor="FFF2CC")
    p["A1"] = "Параметры расчёта"
    p["A1"].font = Font(bold=True, size=13)
    p["A2"] = "Жёлтые ячейки — редактируемые. Проценты хранятся как доли (0.027 = 2.7%)."
    params = [
        (4, "Комиссия платёжной системы", fee_ps, "Удерживается платёжкой с суммы заказа"),
        (5, "Комиссия ggsel", fee_ggsel, "Комиссия площадки ggsel"),
        (6, "Итого комиссия с продажи", "=B4+B5", "Сумма двух комиссий выше"),
        (7, "Курс USD/RUB", round(rate, 4), f"источник: {rate_src}"),
    ]
    for row, label, val, note in params:
        p.cell(row=row, column=1, value=label)
        c = p.cell(row=row, column=2, value=val)
        if isinstance(val, float):
            c.fill = yellow
        p.cell(row=row, column=3, value=note)
    notes = [
        (10, "Как считается прибыль"),
        (11, "Net-выручка = Цена продажи × (1 − итого комиссия). Комиссии при возврате НЕ возвращаются."),
        (12, "Себестоимость = цена выкупа StarPets ($) × курс USD/RUB."),
        (13, "Прибыль (доставлен) = Net-выручка − Себестоимость."),
        (14, "Прибыль (возврат) = −(комиссия ПС + комиссия ggsel)."),
        (16, "ОГРАНИЧЕНИЕ: у заказа хранится ОДНА цена выкупа. Перекупы (rebuy-fresh)"),
        (17, "затирают предыдущую, поэтому себестоимость таких заказов занижена."),
        (18, "Курс один на весь период, хотя заказы покупались по курсам своих дней."),
    ]
    for row, text in notes:
        p.cell(row=row, column=1, value=text)
    p["A10"].font = Font(bold=True)
    p["A16"].font = Font(bold=True)
    p.column_dimensions["A"].width = 78

    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 34
    for col in "CDEFGHIJKLM":
        ws.column_dimensions[col].width = 15
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"accounting_{datetime.utcnow():%Y%m%d}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/accounting/push-sheets")
async def accounting_push_sheets(days: int = 14):
    """Отправить заказы в Google-таблицу вручную. По расписанию это делает воркер."""
    from app.workers.sheets_sync import push_orders
    return await push_orders(days=days)


@router.get("/accounting/orders")
async def accounting_orders(days: int = 30, only_problems: bool = False, limit: int = 200):
    """Построчная выгрузка — чтобы сверить итоги и найти, где деньги застряли."""
    since = datetime.utcnow() - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Order).where(Order.created_at >= since)
            .order_by(Order.id.desc()).limit(limit)
        )).scalars().all()

    out = []
    for o in rows:
        if only_problems and o.delivery_status not in _AT_RISK:
            continue
        if float(o.amount_rub or 0) <= 0 and not only_problems:
            continue
        out.append({
            "id": o.id,
            "ggsel_order_id": o.ggsel_order_id,
            "item": o.item_name,
            "status": o.delivery_status.value if o.delivery_status else None,
            "sale_rub": float(o.amount_rub) if o.amount_rub else None,
            "cost_usd": float(o.exec_price_usd) if o.exec_price_usd else None,
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
            "delivered_at": o.delivered_at.isoformat() if o.delivered_at else None,
            "error": (o.error_reason or "")[:120] or None,
        })
    return {"count": len(out), "orders": out}


@router.get("/accounting/starpets-reconcile")
async def accounting_starpets_reconcile(sp_delivered: float = 0.0, sp_canceled: float = 0.0,
                                        limit: int = 100):
    """
    Сверка закупа с цифрами StarPets за всё время, в долларах: «стоимость успешных выдач»
    и «стоимость отменённых покупок». sp_delivered / sp_canceled — их цифры; если переданы,
    в ответе будет разница.

    Считаем ПОКУПКИ, а не заказы: StarPets видит каждый купленный предмет, а наш заказ мог
    купить два-три (перевыкупы). Покупки собираются из журнала (purchase_log) и из текущей
    покупки заказа (orders.starpets_purchase_id) по id покупки StarPets — так одна и та же
    покупка не считается дважды.

    Куда относится покупка:
      выдана   — текущая покупка заказа «Доставлен» (done/finalized);
      отменена — по журналу брошена или возвращена; вытеснена перевыкупом без записи о
                 возврате; текущая покупка сорванного или закрытого заказа; невыданная,
                 у которой истёк час жизни (такие StarPets возвращает сам);
      открыта  — исход ещё неизвестен.

    Доказательство выдачи — только событие трейда FINISHED(8) в trade_events. Заказы, где
    наш статус и это событие расходятся, перечислены в suspects: в них и надо искать
    разницу с цифрами StarPets.
    """
    from collections import Counter

    from app.config import settings
    from app.db.models import PurchaseLog, TradeEvent
    from app.deadline import item_expired

    async with AsyncSessionLocal() as db:
        orders = (await db.execute(select(Order))).scalars().all()
        logs = (await db.execute(select(PurchaseLog).order_by(PurchaseLog.id))).scalars().all()
        with_events = set((await db.execute(
            select(TradeEvent.order_id).where(TradeEvent.order_id.isnot(None)).distinct()
        )).scalars().all())
        finished = set((await db.execute(
            select(TradeEvent.order_id).where(TradeEvent.status == 8).distinct()
        )).scalars().all())

    def _key(pid, order_id) -> str:
        # Покупка без id бывает только в старых записях — считаем её одной на заказ.
        return str(pid) if pid else f"order{order_id}:noid"

    def _f(v):
        return float(v) if v is not None else None

    purchases: dict[str, dict] = {}

    def _slot(key: str, order_id: int) -> dict:
        return purchases.setdefault(key, {"order_id": order_id, "buy": None, "exec": None,
                                          "back": None, "returned": False})

    for r in logs:
        p = _slot(_key(r.starpets_purchase_id, r.order_id), r.order_id)
        if r.kind == "buy":
            p["buy"] = _f(r.price_usd)
        elif r.kind in ("abandon", "refund"):
            # abandon и refund по одному предмету — один и тот же возврат, не два.
            p["returned"] = True
            if r.price_usd is not None:
                p["back"] = max(p["back"] or 0.0, float(r.price_usd))

    current: dict[str, Order] = {}
    for o in orders:
        if not (o.starpets_purchase_id or o.exec_price_usd):
            continue
        key = _key(o.starpets_purchase_id, o.id)
        current[key] = o
        _slot(key, o.id)["exec"] = _f(o.exec_price_usd)

    def _bucket() -> dict:
        return {"purchases": 0, "usd": 0.0}

    def _add(b: dict, usd: float) -> None:
        b["purchases"] += 1
        b["usd"] += usd

    delivered = {"finished_8": _bucket(), "events_without_8": _bucket(), "no_events": _bucket()}
    canceled = {"returned_logged": _bucket(), "superseded_unlogged": _bucket(),
                "order_failed_or_closed": _bucket(), "expired_unlogged": _bucket()}
    open_: dict[str, dict] = {}
    not_in_accounting = _bucket()
    no_price = 0
    unconfirmed, delivered_but_returned = [], []
    now = datetime.utcnow()

    def _row(o: Order, usd: float) -> dict:
        return {
            "id": o.id,
            "ggsel_order_id": o.ggsel_order_id,
            "item": o.item_name,
            "status": o.delivery_status.value if o.delivery_status else None,
            "cost_usd": round(usd, 3),
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
            "delivered_at": o.delivered_at.isoformat() if o.delivered_at else None,
            "starpets_purchase_id": o.starpets_purchase_id,
            "trade_id": o.starpets_custom_id,
            "last_trade_status": o.starpets_status,
            "has_trade_events": o.id in with_events,
        }

    for key, p in purchases.items():
        price = next((v for v in (p["buy"], p["exec"], p["back"]) if v is not None), None)
        if price is None:
            no_price += 1
            price = 0.0
        cur = current.get(key)
        if cur is not None and cur.delivery_status in _DELIVERED:
            if cur.id in finished:
                _add(delivered["finished_8"], price)
            else:
                _add(delivered["events_without_8" if cur.id in with_events else "no_events"],
                     price)
                unconfirmed.append(_row(cur, price))
            if cur.paid_at is None or float(cur.amount_rub or 0) <= 0:
                _add(not_in_accounting, price)
            if p["returned"]:
                # Мы считаем предмет выданным, а журнал говорит, что StarPets его вернул.
                delivered_but_returned.append(_row(cur, price))
        elif p["returned"]:
            _add(canceled["returned_logged"], price)
        elif cur is None:
            _add(canceled["superseded_unlogged"], price)
        elif cur.delivery_status in _REFUNDED:
            _add(canceled["order_failed_or_closed"], price)
        elif item_expired(cur, now):
            _add(canceled["expired_unlogged"], price)
        else:
            st = cur.delivery_status.value if cur.delivery_status else "unknown"
            _add(open_.setdefault(st, _bucket()), price)

    # Обратный случай: трейд дошёл до FINISHED(8), а заказ у нас не «Доставлен». StarPets
    # такую покупку, скорее всего, считает выдачей — а если покупателю ещё и вернули
    # деньги, это прямой убыток на всю стоимость предмета.
    finished_not_delivered = [
        _row(o, float(o.exec_price_usd or 0)) for o in orders
        if o.id in finished and o.delivery_status not in _DELIVERED
    ]

    def _total(buckets: dict) -> dict:
        return {"purchases": sum(b["purchases"] for b in buckets.values()),
                "usd": round(sum(b["usd"] for b in buckets.values()), 2)}

    def _rounded(buckets: dict) -> dict:
        return {k: {"purchases": b["purchases"], "usd": round(b["usd"], 2)}
                for k, b in buckets.items()}

    delivered_total, canceled_total = _total(delivered), _total(canceled)

    # Покрытие журнала: он ведётся с 04.08.2026. Покупки до этого есть в нём, только если
    # делали импорт из логов Railway (/admin/import-purchase-log), иначе перевыкупы того
    # времени не видны вовсе, и отменённых у нас выйдет меньше, чем у StarPets.
    journal_start = min((r.created_at for r in logs if r.source == "worker"), default=None)
    logged_orders = {r.order_id for r in logs}
    before = [o for o in orders
              if journal_start and o.created_at and o.created_at < journal_start
              and (o.starpets_purchase_id or o.exec_price_usd)]

    unconfirmed.sort(key=lambda r: r["delivered_at"] or "", reverse=True)
    return {
        "service": settings.public_url,
        "delivered": {
            **delivered_total,
            "diff_vs_starpets": (round(delivered_total["usd"] - sp_delivered, 2)
                                 if sp_delivered else None),
            "by_evidence": _rounded(delivered),
            "not_in_accounting_report": {"purchases": not_in_accounting["purchases"],
                                         "usd": round(not_in_accounting["usd"], 2)},
        },
        "canceled": {
            **canceled_total,
            "diff_vs_starpets": (round(canceled_total["usd"] - sp_canceled, 2)
                                 if sp_canceled else None),
            "by_reason": _rounded(canceled),
        },
        "open": {**_total(open_), "by_status": _rounded(open_)},
        "journal": {
            "rows": len(logs),
            "by_source": dict(Counter(r.source or "unknown" for r in logs)),
            "by_kind": dict(Counter(r.kind for r in logs)),
            "worker_since": journal_start.isoformat() if journal_start else None,
            "orders_before_journal": len(before),
            "of_them_with_log_rows": sum(1 for o in before if o.id in logged_orders),
        },
        "purchases_without_price": no_price,
        "suspects": {
            "delivered_without_finish_8": {"count": len(unconfirmed),
                                           "orders": unconfirmed[:limit]},
            "finish_8_but_not_delivered": {"count": len(finished_not_delivered),
                                           "orders": finished_not_delivered[:limit]},
            "delivered_but_returned_in_log": {"count": len(delivered_but_returned),
                                              "orders": delivered_but_returned[:limit]},
        },
        "notes": [
            "by_evidence.no_events — заказы без единого события трейда: до 01.07.2026 "
            "события не записывались, позже так выглядят заказы, закрытые вручную",
            "not_in_accounting_report — выдачи, которых нет в /accounting: там пропускаются "
            "заказы без даты оплаты или суммы",
            "superseded_unlogged — покупка заказа, вытесненная следующей, без записи о "
            "возврате. Почти наверняка StarPets её отменил, но по журналу это не видно",
        ],
    }
