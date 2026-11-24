"""API подсказок снабжения для вкладки-компонента на портале.

Маршруты (за nginx: https://b-integration.ru/supply-ai/...):
  GET  /              — проверка, что сервис жив
  POST /api/advice    — подсказки по закупке; заголовок X-Api-Key: API_KEY
                        тело: {"purchase_id": 23504, "positions": [{"id":..,"name":..,"qty":..,"unit":..}]?}
                        если positions не переданы — сервер сам найдёт их через подзаявки закупки
  GET  /preview       — HTML для проверки в браузере: /preview?purchase=ID&key=API_KEY
"""
import html
import json
import os
import threading
import time
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import db
from .b24 import B24
from .discover import CONFIG
from .search import Index, supplier_summary, price_ok, _sort_date
from .sync import position_row

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://b-integration.ru/supply-ai/").rstrip("/") + "/"
API_KEY = os.environ.get("API_KEY", "")
CACHE_SEC = int(os.environ.get("ADVICE_CACHE_SEC", "600"))

_lock = threading.Lock()
_state = {"index": None, "sync": None, "building": False, "cfg": None}
_cache = {}  # (закупка, позиции, синхр.) -> (время, ответ)


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
    subs, errors = set(), []
    for fld in c.get("subrequest_purchase_fields") or [f"parentId{pt}"]:
        try:
            items = b.list_all("crm.item.list", {"entityTypeId": st, "filter": {fld: purchase_id},
                                                 "select": ["id"]},
                               extract=lambda r: (r or {}).get("items", []))
            subs |= {int(i["id"]) for i in items}
        except Exception as ex:
            errors.append(f"подзаявки по полю {fld}: {ex}")
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
    _state["last_errors"] = errors
    return sorted(out.values(), key=lambda p: p["id"]), len(subs)


# ---------- подсказки ----------
def _offer(o, h=None):
    return {"supplier": o["company"] or (f"запрос #{o['request_id']}" if o["request_id"] else None),
            "company_id": o["company_id"], "price": o["price"] if price_ok(o["price"]) else None,
            "selected": bool(o["selected"]), "date": (o["date_create"] or "")[:10],
            "request_id": o["request_id"], "position": h["name"] if h else None}


def advise(purchase_id, positions=None):
    """Чистые данные: по каждой позиции — у кого брали, лучшая цена, похожие закупки; сводка по поставщикам."""
    idx = index()
    nsubs = None
    _state["last_errors"] = []
    if not positions:
        positions, nsubs = purchase_positions(purchase_id)
    pids = {int(p["id"]) for p in positions if p.get("id") not in (None, "")}
    out, by_sup = [], {}
    for n, p in enumerate(positions, 1):
        hits = idx.search(p.get("name") or "", k=8, exclude_pids=pids)
        pairs = [(o, h) for h in hits for o in h["offers"]]
        won = sorted([x for x in pairs if x[0]["selected"] and price_ok(x[0]["price"])],
                     key=lambda x: _sort_date(x[0]["date_create"]), reverse=True)
        priced = sorted([x for x in pairs if price_ok(x[0]["price"])], key=lambda x: x[0]["price"])
        for s in supplier_summary(hits)[:5]:
            d = by_sup.setdefault(s["supplier"], {"supplier": s["supplier"], "company_id": s["company_id"],
                                                  "positions": [], "wins": 0, "last": None})
            d["positions"].append(n)
            d["wins"] += s["wins"]
            if s["last"] and (d["last"] is None or _sort_date(s["last"][1]) > _sort_date(d["last"]["date"])):
                d["last"] = {"price": s["last"][0], "date": s["last"][1][:10], "position": s["last"][2]}
        out.append({
            "n": n, "id": p.get("id"), "name": p.get("name"), "qty": p.get("qty"), "unit": p.get("unit"),
            "status": "found" if won else "asked" if hits else "new",
            "last": _offer(*won[0]) if won else None,
            "best": _offer(*priced[0]) if priced else None,
            "similar": [{"score": round(h["score"], 2), "name": h["name"], "qty": h["qty"], "unit": h["unit"],
                         "date": (h["date"] or "")[:10],
                         "offers": [_offer(o) for o in sorted(h["offers"], key=lambda o: (-(o["selected"] or 0),
                                                                                         o["price"] or 1e18))[:6]]}
                        for h in hits]})
    sups = sorted(by_sup.values(), key=lambda d: (-len(d["positions"]), -d["wins"]))[:15]
    return {"ok": True, "purchase_id": purchase_id, "updated": (_state["sync"] or "")[:16].replace("T", " "),
            "subrequests_found": nsubs, "warnings": list(_state.get("last_errors") or []),
            "positions": out, "suppliers": sups}


# ---------- HTML-фрагмент (стили с префиксом .riai, чтобы не задеть стили Битрикса) ----------
def fmt_price(p):
    return f"{p:,.0f}\u00a0₽".replace(",", "\u00a0") if p else "—"


def e(s):
    return html.escape(str(s if s is not None else ""))


CSS = """
.riai{font:13px/1.45 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:#1f2d3d}
.riai h3{font-size:15px;margin:18px 0 8px}
.riai table{border-collapse:collapse;width:100%;background:#fff}
.riai th,.riai td{border-bottom:1px solid #e5e9ee;padding:6px 8px;text-align:left;vertical-align:top}
.riai th{background:#f5f7f9;font-weight:600;color:#525c69}
.riai .muted{color:#80868e}.riai .none{color:#a0662b}.riai .win{color:#2b8a3e;font-weight:600}
.riai details summary{cursor:pointer;color:#2067b0}
.riai details table{margin:6px 0 4px;font-size:12px}
.riai .note{background:#f5f7f9;padding:8px 10px;border-radius:4px;margin-bottom:10px}
.riai .tag{display:inline-block;background:#eef2f4;border-radius:3px;padding:0 5px;margin:1px 2px 1px 0;font-size:12px}
"""


def to_html(d):
    def offer_cell(o, with_pos=True):
        if not o:
            return "—"
        return (f'<span class="win">{e(o["supplier"] or "—")}</span><br>{fmt_price(o["price"])} '
                f'<span class="muted">· {e(o["date"])}' + (f' · {e(o["position"])}' if with_pos and o["position"] else "") +
                "</span>")
    rows = []
    for p in d["positions"]:
        sim = ""
        if p["similar"]:
            trs = "".join(
                f'<tr><td>{h["score"]:.2f}</td><td>{e(h["name"])} <span class="muted">({e(h["qty"])} {e(h["unit"])})</span></td>'
                f'<td>{e(h["date"])}</td><td>' +
                "; ".join(f'{e(o["supplier"] or "—")}: {fmt_price(o["price"])}{" ✓" if o["selected"] else ""}'
                          for o in h["offers"]) + "</td></tr>" for h in p["similar"])
            sim = (f'<details><summary>похожие закупки: {len(p["similar"])}</summary><table><tr><th>Похож.</th>'
                   f'<th>Позиция</th><th>Дата</th><th>Предложения (✓ — выбран)</th></tr>{trs}</table></details>')
        last = (offer_cell(p["last"]) if p["last"] else
                '<span class="none">не закупали</span>' if p["status"] == "new" else
                '<span class="muted">похожее запрашивали, без выбора</span>')
        rows.append(f'<tr><td>{p["n"]}</td><td>{e(p["name"])}{sim}</td><td>{e(p["qty"])} {e(p["unit"])}</td>'
                    f'<td>{last}</td><td>{offer_cell(p["best"], False)}</td></tr>')
    sup = "".join(
        f'<tr><td>{e(s["supplier"])}</td><td>{"".join(f"<span class=tag>№{i}</span>" for i in s["positions"])}</td>'
        f'<td>{s["wins"]}</td><td>' +
        (f'{fmt_price(s["last"]["price"])} <span class="muted">· {e(s["last"]["date"])} · {e(s["last"]["position"])}</span>'
         if s["last"] else "—") + "</td></tr>" for s in d["suppliers"])
    if not d["positions"]:
        body = (f'<div class="note">Позиции закупки не найдены'
                f'{" (подзаявок: %s)" % d["subrequests_found"] if d["subrequests_found"] is not None else ""}.</div>')
    else:
        body = (f'<h3>Позиции ({len(d["positions"])})</h3><table><tr><th>№</th><th>Позиция</th><th>Кол-во</th>'
                f'<th>Брали в последний раз</th><th>Лучшая цена в истории</th></tr>{"".join(rows)}</table>'
                f'<h3>Кому отправить запросы</h3>' +
                (f'<table><tr><th>Поставщик</th><th>Какие позиции закрывает</th><th>Выбирали раз</th>'
                 f'<th>Последняя цена</th></tr>{sup}</table>' if sup else
                 '<div class="note">По истории предложить некого — позиции новые.</div>'))
    warn = "".join(f'<div class="note none">⚠ {e(w)}</div>' for w in d.get("warnings") or [])
    return (f'<style>{CSS}</style><div class="riai">{warn}<div class="note">Подсказки по истории закупок, '
            f'данные на {e(d["updated"])}. Цены — за единицу из прошлой закупки, сверяйте единицы.</div>{body}</div>')


# ---------- маршруты ----------
def key_ok(k):
    return bool(API_KEY) and hmac.compare_digest(k or "", API_KEY)


@app.get("/", response_class=PlainTextResponse)
def health():
    return "RIAI: сервис подсказок снабжения работает"


@app.post("/api/advice")
async def api_advice(request: Request):
    if not key_ok(request.headers.get("x-api-key")):
        return JSONResponse({"ok": False, "error": "bad api key"}, status_code=403)
    try:
        body = await request.json()
        pid = int(body.get("purchase_id"))
    except Exception:
        return JSONResponse({"ok": False, "error": "нужен JSON с purchase_id"}, status_code=400)
    positions = body.get("positions") or None
    fmt = body.get("format", "both")
    ck = (pid, json.dumps(positions, sort_keys=True, ensure_ascii=False), _state["sync"])
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_SEC and not body.get("refresh"):
        d = hit[1]
    else:
        try:
            d = advise(pid, positions)
        except Exception as ex:
            return JSONResponse({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, status_code=500)
        if len(_cache) > 500:
            _cache.clear()
        _cache[ck] = (time.time(), d)
    res = dict(d)
    if fmt in ("html", "both"):
        res["html"] = to_html(d)
    if fmt == "html":
        res = {"ok": True, "html": res["html"]}
    return JSONResponse(res)


@app.get("/preview", response_class=HTMLResponse)
def preview(purchase: int, key: str = ""):
    if not key_ok(key):
        return HTMLResponse("Нет доступа", status_code=403)
    return f'<!doctype html><meta charset="utf-8"><body style="margin:12px">{to_html(advise(purchase))}</body>'
