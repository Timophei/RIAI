"""API подсказок снабжения для вкладки-компонента на портале.

Маршруты (за nginx: https://b-integration.ru/supply-ai/...):
  GET  /              — проверка, что сервис жив
  POST /api/advice    — подсказки; заголовок X-Api-Key: API_KEY
                        тело: {"purchase_id": 23504, "positions": [{"id":..,"name":..,"qty":..,"unit":..}],
                               "matches": 8?, "refresh": false?}
                        ответ: JSON с ID позиций, предложений, поставщиков, запросов и закупок — ссылки строит портал
  GET  /preview       — HTML для проверки в браузере: /preview?key=API_KEY&q=Бумага А4&q=Швеллер 20
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
from .discover import CONFIG
from .search import Index, price_ok, _sort_date

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://b-integration.ru/supply-ai/").rstrip("/") + "/"
API_KEY = os.environ.get("API_KEY", "")
CACHE_SEC = int(os.environ.get("ADVICE_CACHE_SEC", "600"))

_lock = threading.Lock()
_state = {"index": None, "sync": None, "building": False}
_cache = {}  # (закупка, позиции, синхр.) -> (время, ответ)


# ---------- данные ----------
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


# ---------- подсказки ----------
def _offer(o):
    """Предложение из истории со всеми ID, по которым портал строит ссылки."""
    return {"offer_id": o["id"], "position_id": o["position_id"], "position": o["name"],
            "company_id": o["company_id"], "company": o["company"],
            "request_id": o["request_id"], "purchase_id": o["purchase_id"],
            "price": o["price"] if price_ok(o["price"]) else None, "selected": bool(o["selected"]),
            "date": (o["date_create"] or "")[:10]}


def advise(purchase_id, positions, k=8):
    """positions: [{"id", "name", "qty"?, "unit"?}] — позиции текущей закупки (передаёт портал).
    Для каждой: похожие позиции из истории и все их предложения; последняя выбранная; лучшая цена.
    Предложения текущей закупки и сами переданные позиции из истории исключаются."""
    idx = index()
    pids = set()
    for p in positions:
        try:
            pids.add(int(p.get("id")))
        except (TypeError, ValueError):
            pass
    out, by_sup = [], {}
    for p in positions:
        hits = idx.search(p.get("name") or "", k=k, exclude_purchase=purchase_id, exclude_pids=pids)
        offers = [o for h in hits for o in h["offers"]]
        won = sorted([o for o in offers if o["selected"] and price_ok(o["price"])],
                     key=lambda o: _sort_date(o["date_create"]), reverse=True)
        priced = sorted([o for o in offers if price_ok(o["price"])], key=lambda o: o["price"])
        for o in offers:
            if not o["company_id"]:
                continue
            d = by_sup.setdefault(o["company_id"], {"company_id": o["company_id"], "company": o["company"],
                                                   "position_ids": [], "offers": 0, "wins": 0, "last": None})
            if p.get("id") not in d["position_ids"]:
                d["position_ids"].append(p.get("id"))
            d["offers"] += 1
            d["wins"] += 1 if o["selected"] else 0
            if price_ok(o["price"]) and (d["last"] is None or
                                         _sort_date(o["date_create"]) > _sort_date(d["last"]["date"])):
                d["last"] = _offer(o)
        out.append({
            "id": p.get("id"), "name": p.get("name"), "qty": p.get("qty"), "unit": p.get("unit"),
            "status": "found" if won else "asked" if hits else "new",   # брали / только запрашивали / не было
            "last_selected": _offer(won[0]) if won else None,
            "best_price": _offer(priced[0]) if priced else None,
            "matches": [{"position_id": h["position_id"], "name": h["name"], "qty": h["qty"], "unit": h["unit"],
                         "score": round(h["score"], 2), "date": (h["date"] or "")[:10],
                         "purchase_ids": h["purchase_ids"],
                         "offers": [_offer(o) for o in sorted(h["offers"], key=lambda o: (-(o["selected"] or 0),
                                                                                           o["price"] or 1e18))]}
                        for h in hits]})
    sups = sorted(by_sup.values(), key=lambda d: (-len(d["position_ids"]), -d["wins"], -d["offers"]))[:20]
    return {"ok": True, "purchase_id": purchase_id,
            "updated": (_state["sync"] or "")[:16].replace("T", " "),
            "positions": out, "suppliers": sups}


# ---------- HTML для проверки (/preview); на портале HTML строит ваш модуль из JSON ----------
def portal():
    u = os.environ.get("B24_WEBHOOK", "")
    return u.split("/rest/")[0].rstrip("/") if "/rest/" in u else ""


def links():
    c = {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c = json.load(f)
    except OSError:
        pass
    base = portal()
    req_t, pur_t = c.get("request_entity_type_id", 183), c.get("purchase_entity_type_id", 132)
    return {
        "company": lambda i: f"{base}/crm/company/details/{i}/",
        "request": lambda i: f"{base}/crm/type/{req_t}/details/{i}/",
        "purchase": lambda i: f"{base}/crm/type/{pur_t}/details/{i}/",
    }


def fmt_price(p):
    return f"{p:,.0f}\u00a0₽".replace(",", "\u00a0") if p else "—"


def e(s):
    return html.escape(str(s if s is not None else ""))


CSS = """
body{font:13px/1.45 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:#1f2d3d;margin:12px}
h3{font-size:15px;margin:18px 0 8px}
table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid #e5e9ee;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f5f7f9;font-weight:600;color:#525c69}
a{color:#2067b0;text-decoration:none}.muted{color:#80868e}.none{color:#a0662b}.win{color:#2b8a3e;font-weight:600}
details summary{cursor:pointer;color:#2067b0} details table{margin:6px 0 4px;font-size:12px}
.note{background:#f5f7f9;padding:8px 10px;border-radius:4px;margin-bottom:10px}
"""


def to_html(d):
    L = links()

    def off(o, cls=""):
        if not o:
            return "—"
        comp = f'<a class="{cls}" href="{L["company"](o["company_id"])}" target="_blank">{e(o["company"])}</a>' \
            if o["company_id"] else e(o["company"] or "—")
        refs = []
        if o["purchase_id"]:
            refs.append(f'<a href="{L["purchase"](o["purchase_id"])}" target="_blank">закупка {o["purchase_id"]}</a>')
        if o["request_id"]:
            refs.append(f'<a href="{L["request"](o["request_id"])}" target="_blank">запрос {o["request_id"]}</a>')
        return (f'{comp} {fmt_price(o["price"])}{" ✓" if o["selected"] else ""} '
                f'<span class="muted">· {e(o["date"])} · {" · ".join(refs)}</span>')

    rows = []
    for n, p in enumerate(d["positions"], 1):
        m = ""
        if p["matches"]:
            trs = "".join(
                f'<tr><td>{h["score"]:.2f}</td><td>{e(h["name"])} <span class="muted">({e(h["qty"])} {e(h["unit"])})</span></td>'
                f'<td>{"<br>".join(off(o) for o in h["offers"])}</td></tr>' for h in p["matches"])
            m = (f'<details><summary>похожие позиции в истории: {len(p["matches"])}</summary><table>'
                 f'<tr><th>Похож.</th><th>Позиция</th><th>Предложения (✓ — выбрано)</th></tr>{trs}</table></details>')
        last = off(p["last_selected"], "win") if p["last_selected"] else (
            '<span class="none">не закупали</span>' if p["status"] == "new" else
            '<span class="muted">запрашивали, без выбора</span>')
        rows.append(f'<tr><td>{n}</td><td>{e(p["name"])}{m}</td><td>{e(p["qty"])} {e(p["unit"])}</td>'
                    f'<td>{last}</td><td>{off(p["best_price"])}</td></tr>')
    nums = {p["id"]: n for n, p in enumerate(d["positions"], 1)}
    sup = "".join(
        f'<tr><td><a href="{L["company"](s["company_id"])}" target="_blank">{e(s["company"])}</a></td>'
        f'<td>{", ".join("№%s" % nums.get(i, i) for i in s["position_ids"])}</td><td>{s["offers"]}</td><td>{s["wins"]}</td>'
        f'<td>{off(s["last"])}</td></tr>' for s in d["suppliers"])
    return (f'<style>{CSS}</style><div class="note">Данные на {e(d["updated"])}. Предложения закупки {d["purchase_id"]} '
            f'исключены.</div><h3>Позиции</h3><table><tr><th>№</th><th>Позиция</th><th>Кол-во</th>'
            f'<th>Последний раз выбрали</th><th>Лучшая цена в истории</th></tr>{"".join(rows)}</table>'
            f'<h3>Кому отправить запросы</h3><table><tr><th>Поставщик</th><th>По позициям</th><th>Предложений</th>'
            f'<th>Выбирали</th><th>Последнее предложение</th></tr>{sup}</table>')


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
        pid = int(body.get("purchase_id") or 0)
        positions = body["positions"]
        assert isinstance(positions, list) and positions and all(isinstance(p, dict) for p in positions)
    except Exception:
        return JSONResponse({"ok": False, "error": "нужен JSON: {purchase_id, positions:[{id,name,qty,unit}]}"},
                            status_code=400)
    k = max(1, min(int(body.get("matches") or 8), 20))
    ck = (pid, k, json.dumps(positions, sort_keys=True, ensure_ascii=False), _state["sync"])
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_SEC and not body.get("refresh"):
        return JSONResponse(hit[1])
    try:
        d = advise(pid, positions, k)
    except Exception as ex:
        return JSONResponse({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, status_code=500)
    if len(_cache) > 500:
        _cache.clear()
    _cache[ck] = (time.time(), d)
    return JSONResponse(d)


@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request, key: str = "", purchase: int = 0):
    """Проверка в браузере: /preview?key=API_KEY&q=Бумага А4&q=Швеллер 20[&purchase=ID]"""
    if not key_ok(key):
        return HTMLResponse("Нет доступа", status_code=403)
    qs = request.query_params.getlist("q") or ["Бумага А4", "Швеллер 40", "Кирпич печной", "Бетон М300"]
    d = advise(purchase, [{"id": f"q{i}", "name": q} for i, q in enumerate(qs, 1)])
    return f'<!doctype html><meta charset="utf-8">{to_html(d)}'
