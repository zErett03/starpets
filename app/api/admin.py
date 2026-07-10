"""Operator admin panel — list orders and manually manage delivery.

Stop-gap while StarPets' wrapper doesn't emit terminal trade events (6/7/8): the
monitor can't auto-close orders, so the operator closes them here. Basic Auth.
"""

import math
import secrets
from datetime import datetime
from html import escape as _esc
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.clients.starpets import starpets

router = APIRouter()
_security = HTTPBasic()

PAGE_SIZES = [25, 50, 100, 200]
DEFAULT_PAGE_SIZE = 50


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


_STATUS_COLORS = {
    "pending": "#d29922",
    "dispatched": "#58a6ff",
    "done": "#3fb950",
    "finalized": "#2ea043",
    "failed": "#f85149",
    "needs_attention": "#db61a2",
    "closed": "#ffffff",
}

_TRADE_STATUS_LABEL = {
    0: "CREATED", 1: "DELAYED_START", 2: "PENDING_FRIEND", 3: "PENDING_START",
    4: "STARTED", 5: "IN_PROGRESS", 6: "FAILED", 7: "CANCELED", 8: "FINISHED",
}

_ERR_SHORT = {
    "no_items_available": "нет товара",
    "no_items": "нет товара",
    "no_roblox_username": "нет ника",
    "price_too_high": "цена выросла",
    "canceled by admin": "отменён",
    "Заказ отменён вручную": "🗑 отменён вручную",
}


# Cancellation reasons for DELETE /trades/ex-buyers/withdrawal (value → operator label).
# "other" requires a free-text reason (1..144 chars). Default is wrong_account_specified
# (buyer gave the wrong login) — the main driver of the "new login" re-issue flow.
REASON_TYPES = [
    ("wrong_account_specified", "Неверно указан логин"),
    ("change_mind_about_picking_up", "Передумал покупать"),
    ("seller_not_friending", "Не добавляет в друзья"),
    ("seller_left_game", "Бот вышел из игры"),
    ("roblox_join_error_773", "Ошибка входа 773"),
    ("roblox_join_error_279", "Ошибка входа 279"),
    ("other", "Другое (укажите текст)"),
]
_REASON_VALUES = {v for v, _ in REASON_TYPES}


def _reason_options() -> str:
    return "".join(f'<option value="{v}">{_esc(label)}</option>' for v, label in REASON_TYPES)


def _err_short(reason: str) -> str:
    if reason in _ERR_SHORT:
        return _ERR_SHORT[reason]
    if reason.startswith("trade status="):
        st = reason.split("=")[-1].strip()
        return {"6": "трейд провален", "7": "трейд отменён"}.get(st, f"статус {st}")
    return (reason[:16] + "…") if len(reason) > 17 else reason


def _badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#8b949e")
    return (
        f'<span class="badge" style="background:{color}22;color:{color};'
        f'border:1px solid {color}55">{_esc(status)}</span>'
    )


def _err_badge(reason: str) -> str:
    if not reason:
        return ""
    short = _err_short(reason)
    color = "#f85149"
    return (
        f'<span class="badge" title="{_esc(reason)}" '
        f'style="background:{color}22;color:{color};border:1px solid {color}55">{_esc(short)}</span>'
    )


def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.strftime("%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def classify_create_trade_error(code, body_text: str):
    """Map a create_trade failure to (icon, verdict) for the operator."""
    try:
        code = int(code)
    except (ValueError, TypeError):
        code = None
    if code == 130:
        return "🟢", "предмет ушёл — вероятно ДОСТАВЛЕНО"
    if code == 210:
        return "🟡", "залочен в активном трейде — НЕ доставлено"
    return "⚪", "ошибка — повторить/проверить"


_CSS = """
:root{color-scheme:dark}
html{scrollbar-gutter:stable}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
a{color:#58a6ff;text-decoration:none}
header{padding:14px 40px;background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0;z-index:5}
h1{margin:0;font-size:16px;font-weight:600}
.sub{color:#8b949e;font-size:12px;margin-top:3px}
.flash{margin:12px 40px 0;padding:10px 14px;border-radius:8px;background:#1f6feb22;border:1px solid #1f6feb66;color:#c9d1d9;font-size:13px}
.flash .x{float:right;color:#8b949e;cursor:pointer}
.toolbar{padding:12px 40px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;border-bottom:1px solid #30363d}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.filters a{padding:4px 10px;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:12px}
.filters a.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
.filters .count{color:#8b949e}
.pager{margin-left:auto;display:flex;gap:10px;align-items:center;font-size:12px;color:#c9d1d9}
.pager a{padding:3px 9px;border:1px solid #30363d;border-radius:6px;color:#c9d1d9}
.pager a.disabled{opacity:.35;pointer-events:none}
.pager select{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:3px 6px;font-size:12px}
.wrap{padding:14px 40px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:1180px}
th,td{padding:8px 9px;border-bottom:1px solid #21262d;text-align:left;vertical-align:middle;white-space:nowrap}
th:last-child,td:last-child{padding-right:0;text-align:right}
th{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;background:#0d1117}
tr:hover td{background:#161b2233}
td{color:#c9d1d9}
.badge{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap;cursor:default}
.col-err{white-space:normal;max-width:160px}
.col-bot{max-width:120px;overflow:hidden;text-overflow:ellipsis}
.actions{display:flex;flex-direction:column;gap:6px;width:168px;margin-left:auto}
.actions .reissue{display:flex;flex-direction:column;gap:4px;margin-top:2px;padding-top:6px;border-top:1px dashed #30363d}
.actions .row1{display:flex;gap:6px;margin:0}
.actions .row1 select{flex:1;min-width:0;height:25px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:0 6px;font-size:12px}
.actform{margin:0}
.act-btn{height:25px;width:100%;border-radius:6px;font-size:12px;font-weight:400;cursor:pointer;border:1px solid #30363d;background:#21262d;color:#c9d1d9;display:flex;align-items:center;justify-content:center}
.act-btn:hover{filter:brightness(1.12)}
.b-green{background:#238636;border-color:#238636;color:#fff;flex:0 0 25px;width:25px;padding:0}
.b-amber{background:#e28015;border-color:#bb8009;color:#fff}
.b-ggsel{background:#238636;border-color:#238636;color:#fff}
.b-blue{background:#1f6feb;border-color:#1f6feb;color:#fff}
.b-red{background:#da3633;border-color:#da3633;color:#fff;flex:0 0 25px;width:25px;padding:0}
.row-pair{display:flex;gap:6px}
.overlay{display:none;position:fixed;inset:0;z-index:50;background:rgba(1,4,9,0.55);backdrop-filter:blur(6px);align-items:center;justify-content:center;padding:20px}
.modal{position:relative;background:#161b22;border:1px solid #30363d;border-radius:14px;max-width:760px;width:100%;max-height:86vh;overflow:auto;padding:22px 24px;box-shadow:0 12px 48px rgba(0,0,0,.6)}
.modal h2{margin:0 30px 14px 0;font-size:16px}
.modal-close{position:absolute;top:12px;right:14px;background:transparent;border:none;color:#8b949e;font-size:18px;cursor:pointer}
.modal-close:hover{color:#fff}
table.hist{min-width:0;width:100%;margin-bottom:16px}
table.hist th,table.hist td{font-size:12px;padding:6px 8px}
.summary-box{position:relative;background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:14px 16px}
pre.summary{margin:0;white-space:pre-wrap;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;color:#c9d1d9}
.copy-btn{position:absolute;right:10px;bottom:10px;background:#21262d;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:13px;padding:4px 8px;cursor:pointer}
.copy-btn:hover{border-color:#8b949e}
.reissue-btn{margin-top:6px;width:145px}
.nick-err{display:none;color:#f85149;font-size:11px;margin-top:4px;line-height:1.3}
.fld-label{display:block;color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin:12px 0 4px}
.nick-view{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 10px;font-size:14px;font-weight:600;color:#c9d1d9;word-break:break-all}
.modal-select,.modal-input{width:100%;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:8px 10px;font-size:13px}
.modal-err{color:#f85149;font-size:12px;margin-top:10px}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.modal-actions .act-btn{width:auto;min-width:96px;height:32px;padding:0 14px}
"""

_JS = """
function changePageSize(sel){
  var u=new URL(window.location.href);
  u.searchParams.set('page_size', sel.value);
  u.searchParams.set('page','1');
  window.location.href=u.toString();
}
async function openHistory(orderId){
  try{
    var res=await fetch('/admin/history?order_id='+orderId,{credentials:'same-origin'});
    if(!res.ok){ alert('Ошибка загрузки истории: '+res.status); return; }
    var d=await res.json();
    document.getElementById('hist-title').textContent='История доставки — заказ #'+d.order_id;
    var rows='';
    for(var i=0;i<d.timeline.length;i++){
      var ev=d.timeline[i];
      rows+='<tr><td>'+ev.time+'</td><td>'+ev.trade_id+'</td><td>'+ev.status_label+'</td><td>'+ev.event_type+'</td></tr>';
    }
    document.getElementById('hist-timeline').innerHTML=rows||'<tr><td colspan="4" style="color:#8b949e">событий ещё нет</td></tr>';
    document.getElementById('hist-summary').textContent=d.summary;
    window._histSummary=d.summary;
    document.getElementById('hist-overlay').style.display='flex';
  }catch(e){ alert('Ошибка: '+e); }
}
function closeHistory(){ document.getElementById('hist-overlay').style.display='none'; }
function copyHistory(){
  navigator.clipboard.writeText(window._histSummary||'').then(function(){
    var b=document.getElementById('hist-copy'); var t=b.textContent;
    b.textContent='✓'; setTimeout(function(){ b.textContent=t; },1200);
  });
}
document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeHistory(); });
document.addEventListener('click',function(e){ if(e.target&&e.target.id==='hist-overlay') closeHistory(); });
function openReissue(orderId){
  var inp=document.getElementById('nick-'+orderId);
  var nick=(inp&&inp.value||'').trim();
  var err=document.getElementById('nickerr-'+orderId);
  if(!nick){
    if(err){ err.textContent='Укажите новый логин в поле ника выше'; err.style.display='block';
             setTimeout(function(){err.style.display='none';},4000); }
    if(inp) inp.focus();
    return;
  }
  if(err) err.style.display='none';
  document.getElementById('reissue-order-id').value=orderId;
  document.getElementById('reissue-username').value=nick;
  document.getElementById('reissue-nick-view').textContent=nick;
  document.getElementById('reissue-title').textContent='Новый логин — заказ #'+orderId;
  var sel=document.getElementById('reissue-reason'); sel.selectedIndex=0;
  document.getElementById('reissue-custom').value='';
  document.getElementById('reissue-err').style.display='none';
  toggleReissueCustom();
  document.getElementById('reissue-overlay').style.display='flex';
}
function closeReissue(){ document.getElementById('reissue-overlay').style.display='none'; }
function toggleReissueCustom(){
  var v=document.getElementById('reissue-reason').value;
  document.getElementById('reissue-custom-wrap').style.display=(v==='other')?'block':'none';
}
function validateReissue(){
  var e=document.getElementById('reissue-err');
  var u=(document.getElementById('reissue-username').value||'').trim();
  if(!u){ e.textContent='Новый логин пуст — закройте окно и заполните поле ника'; e.style.display='block'; return false; }
  if(document.getElementById('reissue-reason').value==='other'){
    var c=(document.getElementById('reissue-custom').value||'').trim();
    if(!c){ e.textContent='Введите текст причины для «Другое»'; e.style.display='block'; return false; }
  }
  return true;
}
document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeReissue(); });
document.addEventListener('click',function(e){ if(e.target&&e.target.id==='reissue-overlay') closeReissue(); });
// Strip flash_order from the URL on load: the flash is server-rendered once from the
// post-action redirect; removing the param means a plain refresh won't re-show it,
// while a new action re-adds the param and shows a fresh flash.
(function(){
  try {
    var u=new URL(window.location.href);
    if(u.searchParams.has('flash_order')){
      u.searchParams.delete('flash_order');
      window.history.replaceState({}, '', u.pathname + u.search + u.hash);
    }
  } catch(e){}
})();
"""


def _order_row(o) -> str:
    statuses = list(_STATUS_COLORS.keys())
    cur = o.delivery_status.value if o.delivery_status else ""
    opts = "".join(
        f'<option value="{s}"{" selected" if s == cur else ""}>{s}</option>'
        for s in statuses
    )
    uname = _esc(o.roblox_username or "")
    amount = f"{o.amount_rub}₽" if o.amount_rub is not None else "—"

    return f"""<tr>
  <td>{o.id}</td>
  <td>{_esc(str(o.ggsel_order_id))}</td>
  <td>{_esc(o.item_name or "—")}</td>
  <td>{amount}</td>
  <td>{_fmt_dt(o.created_at)}</td>
  <td>
    <form class="actform" method="post" action="/admin/edit-username" style="display:flex;gap:5px">
      <input type="hidden" name="order_id" value="{o.id}">
      <input type="text" name="username" id="nick-{o.id}" value="{uname}" placeholder="ник"
             style="width:110px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 6px;font-size:12px">
      <button type="submit" class="act-btn" style="width:30px" title="Сохранить ник">✓</button>
    </form>
    <button type="button" class="act-btn b-amber reissue-btn" onclick="openReissue({o.id})" title="Отменить текущий трейд и пересоздать на новый логин">Новый логин</button>
    <div id="nickerr-{o.id}" class="nick-err"></div>
  </td>
  <td>{_badge(cur)}</td>
  <td>{_esc(o.starpets_status or "—")}</td>
  <td>{_esc(o.starpets_custom_id or "—")}</td>
  <td>{_esc(o.starpets_purchase_id or "—")}</td>
  <td class="col-bot" title="{_esc(o.bot_name or '')}">{_esc(o.bot_name or "—")}</td>
  <td class="col-err">{_err_badge(o.error_reason or "")}</td>
  <td>
    <div class="actions">
      <form class="actform row1" method="post" action="/admin/set-status">
        <input type="hidden" name="order_id" value="{o.id}">
        <select name="status">{opts}</select>
        <button type="submit" class="act-btn b-green" title="Обновить статус">✓</button>
      </form>
      <form class="actform" method="post" action="/admin/redeliver"
            onsubmit="return confirm('Создать новый трейд по заказу {o.id}? Если предмет ещё у нас — запустится новая выдача; если ушёл — покажет ошибку.')">
        <input type="hidden" name="order_id" value="{o.id}">
        <button type="submit" class="act-btn b-amber">Новый трейд</button>
      </form>
      <div class="row-pair">
        <form class="actform" method="post" action="/admin/mark-delivered" style="flex:1"
              onsubmit="return confirm('Закрыть заказ {o.id} и отметить доставку в ggsel (высвободит оплату)?')">
          <input type="hidden" name="order_id" value="{o.id}">
          <button type="submit" class="act-btn b-ggsel">Отправить на ggsel</button>
        </form>
        <form class="actform" method="post" action="/admin/cancel-order"
              onsubmit="return confirm('Отменить заказ {o.id}? Трейд будет закрыт, выдача остановлена. Возврат/отказ денег оформите на ggsel вручную.')">
          <input type="hidden" name="order_id" value="{o.id}">
          <button type="submit" class="act-btn b-red" title="Отменить заказ (закрыть трейд)">🗑</button>
        </form>
      </div>
      <button type="button" class="act-btn b-blue" onclick="openHistory({o.id})">История доставки</button>
    </div>
  </td>
</tr>"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_orders(
    _user: str = Depends(require_admin),
    status: str = Query(None),
    page: int = Query(1),
    page_size: int = Query(DEFAULT_PAGE_SIZE),
    flash_order: int = Query(None),
):
    from sqlalchemy import select, func
    from app.db import AsyncSessionLocal
    from app.db.models import Order, DeliveryStatus

    if page_size not in PAGE_SIZES:
        page_size = DEFAULT_PAGE_SIZE
    if page < 1:
        page = 1

    flash_html = ""
    async with AsyncSessionLocal() as db:
        counts_res = await db.execute(
            select(Order.delivery_status, func.count()).group_by(Order.delivery_status)
        )
        counts = {row[0].value: row[1] for row in counts_res}
        total_all = sum(counts.values())

        base_where = []
        if status and status in _STATUS_COLORS:
            base_where.append(Order.delivery_status == DeliveryStatus(status))

        cnt_q = select(func.count()).select_from(Order)
        for w in base_where:
            cnt_q = cnt_q.where(w)
        total = (await db.execute(cnt_q)).scalar() or 0
        pages = max(1, math.ceil(total / page_size))
        if page > pages:
            page = pages
        offset = (page - 1) * page_size

        q = select(Order).order_by(Order.created_at.desc())
        for w in base_where:
            q = q.where(w)
        q = q.limit(page_size).offset(offset)
        orders = (await db.execute(q)).scalars().all()

        if flash_order is not None:
            fo = (await db.execute(select(Order).where(Order.id == flash_order))).scalar_one_or_none()
            if fo and fo.last_redeliver_result:
                flash_html = (
                    f'<div class="flash"><span class="x" onclick="this.parentNode.remove()">✕</span>'
                    f'Заказ #{fo.id}: {_esc(fo.last_redeliver_result)}</div>'
                )

    def _f(label, value, n):
        active = " active" if (value or None) == (status or None) else ""
        href = f"/admin?page_size={page_size}" if value is None else f"/admin?status={value}&page_size={page_size}"
        cnt = f' <span class="count">{n}</span>' if n is not None else ""
        return f'<a class="filter{active}" href="{href}">{label}{cnt}</a>'

    filters = [_f("Все", None, total_all)]
    for s in _STATUS_COLORS:
        filters.append(_f(s, s, counts.get(s, 0)))
    filters_html = "".join(filters)

    def _page_link(p, label, disabled):
        qs = f"page={p}&page_size={page_size}" + (f"&status={status}" if status else "")
        cls = "disabled" if disabled else ""
        return f'<a class="{cls}" href="/admin?{qs}">{label}</a>'

    size_opts = "".join(
        f'<option value="{n}"{" selected" if n == page_size else ""}>{n}</option>' for n in PAGE_SIZES
    )
    pager_html = (
        f'<div class="pager">'
        f'<span>показано {len(orders)} из {total}</span>'
        f'<span>стр {page}/{pages}</span>'
        f'{_page_link(page-1, "← Назад", page <= 1)}'
        f'{_page_link(page+1, "Вперёд →", page >= pages)}'
        f'<span>на странице:</span>'
        f'<select onchange="changePageSize(this)">{size_opts}</select>'
        f'</div>'
    )

    rows_html = "".join(_order_row(o) for o in orders) or (
        '<tr><td colspan="13" style="padding:24px;text-align:center;color:#8b949e">Заказов нет</td></tr>'
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StarPets — заказы</title><style>{_CSS}</style></head>
<body>
<header>
  <h1>Заказы StarPets</h1>
  <div class="sub">Операторская панель · всего {total_all} · обновлено {datetime.utcnow().strftime("%H:%M:%S")} UTC</div>
</header>
{flash_html}
<div class="toolbar">
  <div class="filters">{filters_html}</div>
  {pager_html}
</div>
<div class="wrap">
<table>
  <thead><tr>
    <th>ID</th><th>ggsel</th><th>Товар</th><th>Сумма</th><th>Создан</th>
    <th>Roblox ник</th><th>Статус</th><th>SP статус</th><th>Trade ID</th>
    <th>Purchase ID</th><th>Бот</th><th>Ошибка</th><th>Действия</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
</div>

<div id="hist-overlay" class="overlay">
  <div class="modal">
    <button class="modal-close" onclick="closeHistory()" title="Закрыть">✕</button>
    <h2 id="hist-title">История доставки</h2>
    <table class="hist">
      <thead><tr><th>Время (UTC)</th><th>Trade ID</th><th>Статус</th><th>Тип</th></tr></thead>
      <tbody id="hist-timeline"></tbody>
    </table>
    <div class="summary-box">
      <pre id="hist-summary" class="summary"></pre>
      <button id="hist-copy" class="copy-btn" title="Скопировать сводку" onclick="copyHistory()">📋</button>
    </div>
  </div>
</div>

<div id="reissue-overlay" class="overlay">
  <div class="modal" style="max-width:460px">
    <button class="modal-close" onclick="closeReissue()" title="Закрыть">✕</button>
    <h2 id="reissue-title">Новый логин</h2>
    <p class="sub" style="margin-bottom:6px">Текущий трейд будет <b>отменён</b>, предмет пересоздан на указанный логин. Убедитесь, что предмет ещё <b>НЕ доставлен</b>.</p>
    <form id="reissue-form" method="post" action="/admin/reissue-new-login" onsubmit="return validateReissue()">
      <input type="hidden" name="order_id" id="reissue-order-id">
      <input type="hidden" name="username" id="reissue-username">
      <span class="fld-label">Новый логин</span>
      <div id="reissue-nick-view" class="nick-view"></div>
      <span class="fld-label">Причина отмены</span>
      <select name="reason_type" id="reissue-reason" class="modal-select" onchange="toggleReissueCustom()">{_reason_options()}</select>
      <div id="reissue-custom-wrap" style="display:none">
        <span class="fld-label">Свой текст (для «Другое»)</span>
        <input type="text" name="reason_custom" id="reissue-custom" class="modal-input" maxlength="144" placeholder="1–144 символа">
      </div>
      <div id="reissue-err" class="modal-err" style="display:none"></div>
      <div class="modal-actions">
        <button type="button" class="act-btn" onclick="closeReissue()">Отмена</button>
        <button type="submit" class="act-btn b-amber">Подтвердить</button>
      </div>
    </form>
  </div>
</div>

<script>{_JS}</script>
</body></html>""")


def _flash_redirect(request: Request, order_id: int) -> RedirectResponse:
    ref = request.headers.get("referer") or "/admin"
    u = urlparse(ref)
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    q["flash_order"] = str(order_id)
    return RedirectResponse(urlunparse(u._replace(query=urlencode(q))), status_code=303)


def _back(request: Request) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or "/admin", status_code=303)


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
    print(f"[admin] order_id={order_id} status -> {status}", flush=True)
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
    print(f"[admin] order_id={order_id} -> done + MARK_DELIVERED queued", flush=True)
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
        order.roblox_username = username.strip() or None
        order.updated_at = datetime.utcnow()
        await db.commit()
    return _back(request)


@router.post("/admin/redeliver")
async def admin_redeliver(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order
    from app.workers.redeliver import redeliver_same_item

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")

        result = await redeliver_same_item(db, order)
        await db.commit()

    print(f"[admin] order_id={order_id} redeliver: {result}", flush=True)
    return _flash_redirect(request, order_id)


@router.post("/admin/cancel-order")
async def admin_cancel_order(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
):
    """Cancel an order at the buyer's request (changed mind / wrong item).

    Cancels the StarPets trade (stops delivery, frees the item) and marks the order
    failed with a distinct reason ("Заказ отменён вручную"), so the auto-retry timer
    won't recreate it and the badge is distinguishable from real delivery failures.
    The money refund/decline is handled MANUALLY in the ggsel cabinet — there is no
    ggsel refund API.
    """
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, DeliveryStatus

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")

        parts = []
        trade_id = order.starpets_custom_id
        if trade_id:
            try:
                await starpets.cancel_trade(trade_id, "change_mind_about_picking_up")
                parts.append(f"трейд {trade_id} закрыт")
            except httpx.HTTPStatusError as exc:
                code = None
                try:
                    code = exc.response.json().get("code")
                except Exception:
                    pass
                parts.append(
                    f"⚠️ закрыть трейд {trade_id} не удалось "
                    f"({exc.response.status_code} code={code})"
                )
            except Exception as e:
                parts.append(f"⚠️ закрыть трейд {trade_id} ошибка: {str(e)[:80]}")

        order.delivery_status = DeliveryStatus.failed
        order.error_reason = "Заказ отменён вручную"
        order.updated_at = datetime.utcnow()
        order.last_redeliver_result = (
            "🗑 " + "; ".join(parts + ["заказ отменён — оформите возврат/отказ на ggsel вручную"])
        )
        await db.commit()

    print(f"[admin] order_id={order_id} cancelled: {order.last_redeliver_result}", flush=True)
    return _flash_redirect(request, order_id)


@router.post("/admin/reissue-new-login")
async def admin_reissue_new_login(
    request: Request,
    _user: str = Depends(require_admin),
    order_id: int = Form(...),
    username: str = Form(""),
    reason_type: str = Form("wrong_account_specified"),
    reason_custom: str = Form(""),
):
    """Cancel the current trade and recreate it on a NEW login (buyer error).

    Chain: DELETE /withdrawal (cancel) -> set new username -> redeliver_same_item.
    Reset trade_retry_count so the operator's manual attempt gets a fresh budget.
    """
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order
    from app.workers.redeliver import redeliver_same_item

    username = username.strip()
    reason_custom = (reason_custom or "").strip()
    if reason_type not in _REASON_VALUES:
        reason_type = "other"
    reason = None
    if reason_type == "other":
        reason = reason_custom[:144] or "reissue to correct account"

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")

        if not username:
            order.last_redeliver_result = "⚪ новый логин не указан — операция отменена"
            order.updated_at = datetime.utcnow()
            await db.commit()
            return _flash_redirect(request, order_id)

        parts = []
        old_trade = order.starpets_custom_id
        if old_trade:
            try:
                await starpets.cancel_trade(old_trade, reason_type, reason)
                parts.append(f"трейд {old_trade} отменён ({reason_type})")
            except httpx.HTTPStatusError as exc:
                code = None
                try:
                    code = exc.response.json().get("code")
                except Exception:
                    pass
                parts.append(
                    f"⚠️ отмена трейда {old_trade} не удалась "
                    f"({exc.response.status_code} code={code}) — продолжаю"
                )
            except Exception as e:
                parts.append(f"⚠️ отмена трейда {old_trade} ошибка: {str(e)[:80]} — продолжаю")

        old_username = order.roblox_username
        order.roblox_username = username
        order.trade_retry_count = 0
        result = await redeliver_same_item(db, order)

        order.last_redeliver_result = (
            f"[новый логин {old_username!r}->{username!r}] " + "; ".join(parts + [result])
        )
        order.updated_at = datetime.utcnow()
        await db.commit()

    print(f"[admin] order_id={order_id} reissue-new-login -> {order.last_redeliver_result}", flush=True)
    return _flash_redirect(request, order_id)


@router.get("/admin/history")
async def admin_history(
    _user: str = Depends(require_admin),
    order_id: int = Query(...),
):
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.db.models import Order, TradeEvent

    async with AsyncSessionLocal() as db:
        order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        ev_rows = (await db.execute(
            select(TradeEvent).where(TradeEvent.order_id == order_id)
            .order_by(TradeEvent.sp_event_id.asc(), TradeEvent.recorded_at.asc())
        )).scalars().all()

    timeline = []
    statuses_seen = []
    reached_in_progress_at = None
    trade_ids = []
    for e in ev_rows:
        occ = e.occurred_at or e.recorded_at
        tstr = occ.strftime("%Y-%m-%d %H:%M:%S") if occ else "—"
        label = "—" if e.status is None else f"{e.status} {_TRADE_STATUS_LABEL.get(e.status, '?')}"
        timeline.append({
            "time": tstr,
            "trade_id": e.trade_id or "—",
            "status_label": label,
            "event_type": e.event_type if e.event_type is not None else "—",
        })
        if e.trade_id and e.trade_id not in trade_ids:
            trade_ids.append(e.trade_id)
        if e.status is not None:
            statuses_seen.append(e.status)
            if e.status == 5 and reached_in_progress_at is None:
                reached_in_progress_at = tstr

    progress = "→".join(str(s) for s in statuses_seen) if statuses_seen else "нет событий"
    exec_price = f"${order.exec_price_usd}" if order.exec_price_usd is not None else "—"
    paid = order.paid_at.strftime("%Y-%m-%d %H:%M") if order.paid_at else "—"
    in_prog = (
        f"{reached_in_progress_at} UTC (внутриигровой обмен исполнен)"
        if reached_in_progress_at else "НЕ достигнут (обмен не начинался)"
    )

    summary = (
        f"Заказ #{order.id} · ggsel {order.ggsel_order_id}\n"
        f"Товар: {order.item_name}\n"
        f"Покупатель Roblox: {order.roblox_username or '—'}\n"
        f"Бот: {order.bot_name or '—'}\n"
        f"Трейд(ы): {', '.join(trade_ids) or '—'}\n"
        f"Прогресс статусов: {progress}\n"
        f"Достиг IN_PROGRESS(5): {in_prog}\n"
        f"Куплен предмет: {order.starpets_purchase_id or '—'} за {exec_price} · оплачен {paid}\n"
        f"Текущий статус заказа: {order.delivery_status.value if order.delivery_status else '—'}\n"
        f"Последняя проверка «Новый трейд»: {order.last_redeliver_result or '—'}"
    )

    return JSONResponse({
        "order_id": order.id,
        "summary": summary,
        "timeline": timeline,
        "reached_in_progress_at": reached_in_progress_at,
    })
