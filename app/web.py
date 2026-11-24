"""Вкладка «Подсказки снабжения» в карточке Закупки (этап 2, только чтение).

Маршруты (за nginx: https://b-integration.ru/supply-ai/...):
  GET  /           — проверка, что сервис жив
  POST /install    — обработчик установки локального приложения: регистрирует вкладку
  POST /tab        — содержимое вкладки (Битрикс открывает в iframe)
  GET  /preview    — то же без Битрикса: /preview?purchase=ID&key=PREVIEW_KEY
"""
import html
import json
import os
import threading
import time
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import db
from .b24 import B24
from .discover import CONFIG
from .search import Index, supplier_summary, price_ok, _sort_date
from .sync import position_row

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://b-integration.ru/supply-ai/").rstrip("/") + "/"
PREVIEW_KEY = os.environ.get("PREVIEW_KEY", "")
TAB_TITLE = os.environ.get("TAB_TITLE", "Подсказки снабжения")
VERIFY = os.environ.get("B24_VERIFY_SSL", "1") != "0"

_lock = threading.Lock()
_state = {"index": None, "sync": None, "building": False, "cfg": None}
_tokens = {}  # AUTH_ID -> время проверки


# ---------- данные ----------
def cfg():
    if _state["cfg"] is None:
        with open(CONFIG, encoding="utf-8") as f:
            c = json.load(f)
        if "subrequest_entity_type_id" not in c:  # конфиг от этапа 1 — дополняем
            b = B24()
            types = b.call("crm.type.list", {}).get("result", {}).get("types", [])
            sub = next((t for t in types if "подзаяв" in (t.get("title") or "").lower()), None)
            c["subrequest_entity_type_id"] = int(os.environ.get("SUBREQUEST_ENTITY_TYPE_ID") or
                                                 (sub or {}).get("entityTypeId") or 164)
            fields = b.call("crm.item.fields", {"entityTypeId": c["subrequest_entity_type_id"]}) \
                .get("result", {}).get("fields", {})
            pt = c.get("purchase_entity_type_id")
            c["subrequest_purchase_fields"] = [k for k, f in fields.items()
                                               if k == f"parentId{pt}" or
                                               "закупк" in (f.get("title") or "").lower()]
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
        _state["cfg"] = c
    return _state["cfg"]


def index():
    """Индекс строится при старте и перестраивается в фоне после каждой синхронизации."""
    con = db.connect(threads=True)
    last = db.get_meta(con, "last_sync")
    if _state["index"] is None:
        with _lock:
            if _state["index"] is None:
                _state["index"], _state["sync"] = Index(con), last
    elif last != _state["sync"] and not _state["building"]:
        _state["building"] = True

        def rebuild():
            try:
                _state["index"], _state["sync"] = Index(db.connect(threads=True)), last
            finally:
                _state["building"] = False
        threading.Thread(target=rebuild, daemon=True).start()
    return _state["index"]


def purchase_positions(purchase_id):
    """Позиции закупки: Закупка → её подзаявки → позиции с этими подзаявками (живой запрос к порталу)
    + позиции, по которым уже есть запросы поставщику этой закупки (из локальной базы)."""
    c, b = cfg(), B24()
    st, pt = c["subrequest_entity_type_id"], c.get("purchase_entity_type_id")
    subs = set()
    for fld in c.get("subrequest_purchase_fields") or [f"parentId{pt}"]:
        try:
            items = b.list_all("crm.item.list", {"entityTypeId": st, "filter": {fld: purchase_id},
                                                 "select": ["id"]},
                               extract=lambda r: (r or {}).get("items", []))
            subs |= {int(i["id"]) for i in items}
        except Exception:
            continue
    out = {}
    sub_prop = c["position_fields"].get("subrequest")
    if subs and sub_prop:
        vals = []
        for s in subs:
            vals += [f"T{st:x}_{s}", f"DYNAMIC_{st}_{s}", str(s)]
        pm = c["position_meta"]
        els = b.list_all("lists.element.get", {
            "IBLOCK_TYPE_ID": c["iblock_type"], "IBLOCK_ID": c["positions_iblock_id"],
            "FILTER": {sub_prop: vals},
            "SELECT": ["ID", "NAME", "DATE_CREATE", "TIMESTAMP_X"] + [k for k in pm if k.startswith("PROPERTY_")]})
        for el in els:
            r = position_row(el, c["position_fields"], pm)
            out[r[0]] = {"id": r[0], "name": r[1], "qty": r[2], "unit": r[3]}
    con = db.connect(threads=True)
    for r in con.execute("""SELECT DISTINCT p.id, p.name, p.qty, p.unit FROM offers o
            JOIN requests q ON q.id = o.request_id JOIN positions p ON p.id = o.position_id
            WHERE q.purchase_id = ?""", (purchase_id,)):
        out.setdefault(r["id"], dict(r))
    return sorted(out.values(), key=lambda p: p["id"]), len(subs)


# ---------- проверка, что запрос пришёл из Битрикса ----------
def portal_base():
    u = urlsplit(os.environ["B24_WEBHOOK"])
    return f"{u.scheme}://{u.netloc}/"


def check_auth(auth_id):
    if not auth_id:
        return False
    now = time.time()
    if now - _tokens.get(auth_id, 0) < 600:
        return True
    try:
        r = requests.post(portal_base() + "rest/user.current.json", data={"auth": auth_id},
                          timeout=15, verify=VERIFY).json()
    except Exception:
        return False
    if r.get("result", {}).get("ID"):
        _tokens[auth_id] = now
        if len(_tokens) > 5000:
            _tokens.clear()
        return True
    return False


# ---------- страница ----------
def fmt_price(p):
    return f"{p:,.0f} ₽".replace(",", " ") if price_ok(p) else "—"


def e(s):
    return html.escape(str(s if s is not None else ""))


CSS = """
body{font:13px/1.45 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:#1f2d3d;margin:12px;background:#fff}
h2{font-size:16px;margin:18px 0 8px}
table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid #e5e9ee;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f5f7f9;font-weight:600;color:#525c69}
.muted{color:#80868e}.none{color:#a0662b}.win{color:#2b8a3e;font-weight:600}
details summary{cursor:pointer;color:#2067b0}
details table{margin:6px 0 4px;font-size:12px}
.note{background:#f5f7f9;padding:8px 10px;border-radius:4px;margin-bottom:10px}
.tag{display:inline-block;background:#eef2f4;border-radius:3px;padding:0 5px;margin:1px 2px 1px 0;font-size:12px}
"""


def render(purchase_id):
    idx = index()
    positions, nsubs = purchase_positions(purchase_id)
    pids = {p["id"] for p in positions}
    rows, by_supplier = [], {}
    for n, p in enumerate(positions, 1):
        hits = idx.search(p["name"], k=8, exclude_pids=pids)
        offers = [(o, h) for h in hits for o in h["offers"]]
        won = sorted([x for x in offers if x[0]["selected"] and price_ok(x[0]["price"])],
                     key=lambda x: _sort_date(x[0]["date_create"]), reverse=True)
        priced = sorted([x for x in offers if price_ok(x[0]["price"])], key=lambda x: x[0]["price"])
        for s in supplier_summary(hits)[:5]:
            d = by_supplier.setdefault(s["supplier"], {"pos": [], "wins": 0, "last": None})
            d["pos"].append(n)
            d["wins"] += s["wins"]
            if s["last"] and (d["last"] is None or _sort_date(s["last"][1]) > _sort_date(d["last"][1])):
                d["last"] = s["last"]
        last = (f'<span class="win">{e(won[0][0]["company"] or "—")}</span><br>{fmt_price(won[0][0]["price"])}'
                f' <span class="muted">· {e((won[0][0]["date_create"] or "")[:10])} · {e(won[0][1]["name"])}</span>'
                if won else '<span class="none">не закупали</span>' if not hits else
                '<span class="muted">похожее запрашивали, но без выбора</span>')
        best = (f'{e(priced[0][0]["company"] or "—")}<br>{fmt_price(priced[0][0]["price"])}'
                f' <span class="muted">· {e((priced[0][0]["date_create"] or "")[:10])}</span>' if priced else "—")
        detail = ""
        if hits:
            trs = "".join(
                f'<tr><td>{h["score"]:.2f}</td><td>{e(h["name"])} <span class="muted">({e(h["qty"])} {e(h["unit"])})</span></td>'
                f'<td>{e((h["date"] or "")[:10])}</td><td>' +
                "; ".join(f'{e(o["company"] or "запрос #%s" % o["request_id"])}: {fmt_price(o["price"])}'
                          f'{" ✓" if o["selected"] else ""}'
                          for o in sorted(h["offers"], key=lambda o: (-(o["selected"] or 0), o["price"] or 1e18))[:6]) +
                "</td></tr>" for h in hits)
            detail = (f'<details><summary>похожие закупки: {len(hits)}</summary><table><tr><th>Похож.</th>'
                      f'<th>Позиция</th><th>Дата</th><th>Предложения (✓ — выбран)</th></tr>{trs}</table></details>')
        rows.append(f'<tr><td>{n}</td><td>{e(p["name"])}{detail}</td><td>{e(p["qty"])} {e(p["unit"])}</td>'
                    f'<td>{last}</td><td>{best}</td></tr>')

    sup = sorted(by_supplier.items(), key=lambda kv: (-len(kv[1]["pos"]), -kv[1]["wins"]))[:15]
    sup_rows = "".join(
        f'<tr><td>{e(k)}</td><td>{"".join(f"<span class=tag>№{i}</span>" for i in v["pos"])}</td>'
        f'<td>{v["wins"]}</td><td>' +
        (f'{fmt_price(v["last"][0])} <span class="muted">· {e(v["last"][1][:10])} · {e(v["last"][2])}</span>'
         if v["last"] else "—") + "</td></tr>" for k, v in sup)

    if not positions:
        body = (f'<div class="note">Позиции этой закупки не найдены (подзаявок: {nsubs}). '
                f'Если позиции точно есть — сообщите ID закупки разработчику.</div>')
    else:
        body = (f'<h2>Позиции закупки ({len(positions)})</h2><table><tr><th>№</th><th>Позиция</th><th>Кол-во</th>'
                f'<th>Брали в последний раз</th><th>Лучшая цена в истории</th></tr>{"".join(rows)}</table>'
                f'<h2>Кому отправить запросы</h2>' +
                (f'<table><tr><th>Поставщик</th><th>Какие позиции закрывает</th><th>Выбирали раз</th>'
                 f'<th>Последняя цена</th></tr>{sup_rows}</table>' if sup_rows else
                 '<div class="note">По истории предложить некого — позиции новые.</div>'))
    upd = idx and _state["sync"]
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>'
            f'<div class="note">Подсказки по истории закупок. Данные обновлены: {e((upd or "")[:16].replace("T", " "))}. '
            f'Цены — за единицу из прошлой закупки, сверяйте единицы.</div>{body}</body></html>')


# ---------- маршруты ----------
@app.get("/", response_class=PlainTextResponse)
def health():
    return "RIAI: сервис подсказок снабжения работает"


@app.api_route("/tab", methods=["GET", "POST"], response_class=HTMLResponse)
async def tab(request: Request):
    form = dict(await request.form()) if request.method == "POST" else {}
    if not check_auth(form.get("AUTH_ID")):
        return HTMLResponse("Нет доступа: откройте вкладку из карточки закупки в Битрикс24.", status_code=403)
    try:
        pid = int(json.loads(form.get("PLACEMENT_OPTIONS") or "{}").get("ID"))
    except (TypeError, ValueError):
        return HTMLResponse("Не передан ID закупки", status_code=400)
    return render(pid)


@app.get("/preview", response_class=HTMLResponse)
def preview(purchase: int, key: str = ""):
    if not PREVIEW_KEY or key != PREVIEW_KEY:
        return HTMLResponse("Нет доступа", status_code=403)
    return render(purchase)


@app.api_route("/install", methods=["GET", "POST"], response_class=HTMLResponse)
async def install(request: Request):
    form = dict(await request.form()) if request.method == "POST" else {}
    auth = form.get("AUTH_ID") or request.query_params.get("AUTH_ID")
    if not check_auth(auth):
        return HTMLResponse("Установка возможна только из Битрикс24", status_code=403)
    placement = f'CRM_DYNAMIC_{cfg()["purchase_entity_type_id"]}_DETAIL_TAB'
    base = portal_base() + "rest/"
    requests.post(base + "placement.unbind.json",
                  data={"auth": auth, "PLACEMENT": placement, "HANDLER": PUBLIC_URL + "tab"},
                  timeout=15, verify=VERIFY)
    r = requests.post(base + "placement.bind.json",
                      data={"auth": auth, "PLACEMENT": placement, "HANDLER": PUBLIC_URL + "tab",
                            "TITLE": TAB_TITLE}, timeout=15, verify=VERIFY).json()
    ok = bool(r.get("result"))
    msg = (f"Вкладка «{e(TAB_TITLE)}» добавлена в карточку закупки ({placement})." if ok else
           f"Ошибка регистрации вкладки: {e(r.get('error'))} {e(r.get('error_description'))}")
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<script src="//api.bitrix24.com/api/v1/"></script></head><body style="font:14px sans-serif">'
            f'<p>{msg}</p><script>if(window.BX24){{BX24.init(function(){{BX24.installFinish();}});}}</script>'
            f'</body></html>')
