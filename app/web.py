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
from . import llm
from .search import Index, price_ok, _sort_date, norm, head
from .units import basis, unit_price, label, to_float

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
# ---------- типы номенклатуры (дерево из taxonomy.txt, разметка в name_types) ----------
def resolve_types(names, allow_llm=True):
    """[как написано] → {нормализованное: путь типа}. Неизвестные — раскладывает моделью (если она настроена
    и allow_llm): только для позиций самой закупки; похожие из истории без типа доразметит ночная задача."""
    from .taxonomy import OTHER, classify_names, TREE
    con = db.connect(threads=True)
    keys = {norm(n): n for n in names if norm(n)}
    if not keys:
        return {}
    q = ",".join("?" * len(keys))
    got = dict(con.execute(f"SELECT name, type_id FROM name_types WHERE name IN ({q})", list(keys)).fetchall())
    missing = [(k, v) for k, v in keys.items() if k not in got]
    if missing and allow_llm and llm.configured() and os.path.exists(TREE):
        try:
            got.update(classify_names(missing, con))
        except Exception as ex:   # модель недоступна — работаем без типов
            _state.setdefault("last_errors", []).append(f"типы: {ex}")
    return {k: v for k, v in got.items() if v and v != OTHER}


def shares_word(a, b):
    """Совпадает ли главное слово (первое существительное) по началу: «Термопаста …»≈«Термопасты …», «Лампа»≈«Лампочки»,
    но «Патрон …» ≠ «лампочки … цоколь Е27»."""
    ha, hb = head(a), head(b)
    return bool(ha and hb and ha[:4] == hb[:4])


def type_offers(idx, type_id, exclude_norms, purchase_id, exclude_pids, cap=300):
    """Предложения по всем позициям истории того же типа (аналоги), свежие сначала."""
    con = db.connect(threads=True)
    norms = [r[0] for r in con.execute("SELECT name FROM name_types WHERE type_id=?", (type_id,))]
    pids = []
    for n in norms:
        if n in exclude_norms or n not in idx.pos_of:
            continue
        pids += [x for x in idx.pids[idx.pos_of[n]] if x not in exclude_pids]
    pids = sorted(pids, reverse=True)[:cap]   # самые новые позиции
    if not pids:
        return []
    rows = [o for p in idx._load(pids).values() for o in p["offers"] if o["purchase_id"] != purchase_id]
    return sorted(rows, key=lambda o: _sort_date(o["date_create"]), reverse=True)


def _offer(o, want=None):
    """Предложение из истории со всеми ID, по которым портал строит ссылки.
    unit — единица той позиции; unit_price — цена за базовую единицу (кг, л, м…), если её можно вычислить;
    comparable — та же базовая единица, что у запрошенной позиции (цены можно сравнивать)."""
    price = o["price"] if price_ok(o["price"]) else None
    up, base = unit_price(price, o["name"], o.get("unit"), o.get("weight"))
    return {"offer_id": o["id"], "position_id": o["position_id"], "position": o["name"],
            "company_id": o["company_id"], "company": o["company"],
            "request_id": o["request_id"], "request_title": o.get("request_title"),
            "purchase_id": o["purchase_id"],
            "price": price, "unit": o.get("unit"),
            "unit_price": round(up, 2) if up is not None else None, "base_unit": label(base),
            "comparable": bool(want and base == want), "analog": False,
            "selected": bool(o["selected"]), "date": (o["date_create"] or "")[:10]}


def advise(purchase_id, positions, k=8, max_columns=8, per_position=3):
    """positions: [{"id", "name", "qty"?, "unit"?}] — позиции текущей закупки (передаёт портал).
    Для каждой: похожие позиции из истории и все их предложения; последняя выбранная; лучшая цена.
    Предложения текущей закупки и сами переданные позиции из истории исключаются."""
    t_start = time.time()
    idx = index()
    pids = set()
    for p in positions:
        try:
            pids.add(int(p.get("id")))
        except (TypeError, ValueError):
            pass
    out, by_sup, by_pur = [], {}, {}
    best_up = {}  # позиция -> лучшая сопоставимая цена за базовую единицу (для индекса цены поставщика)
    t0 = time.time()
    timing = {"index": round(t0 - t_start, 2)}
    qtypes = resolve_types([p.get("name") or "" for p in positions])   # тип каждой позиции закупки
    timing["types"] = round(time.time() - t0, 2)
    t_search = t_analog = 0.0
    for p in positions:
        pid_cur = p.get("id")
        want, _ = basis(p.get("name"), p.get("unit"), p.get("weight"))
        qtype = qtypes.get(norm(p.get("name") or ""))
        t1 = time.time()
        hits = idx.search(p.get("name") or "", k=k * 2 if qtype else k,
                          exclude_purchase=purchase_id, exclude_pids=pids)
        if qtype and hits:
            # по смыслу: находка другого (известного) типа — не то же самое, даже если слова похожи
            htypes = resolve_types([h["name"] for h in hits], allow_llm=False)
            hits = [h for h in hits if htypes.get(norm(h["name"]), qtype) == qtype][:k]
        t_search += time.time() - t1
        offers = [o for h in hits for o in h["offers"]]
        won = sorted([o for o in offers if o["selected"] and price_ok(o["price"])],
                     key=lambda o: _sort_date(o["date_create"]), reverse=True)
        # лучшая цена — только среди сопоставимых единиц, по цене за базовую единицу
        priced = sorted([x for x in ((_offer(o, want), o) for o in offers if price_ok(o["price"]))
                         if x[0]["comparable"]], key=lambda x: x[0]["unit_price"])
        if priced:
            best_up[pid_cur] = priced[0][0]["unit_price"]
        for h in hits:
            # «давал лучшую цену»: в каждой прошлой позиции (одном конкурсе) — у кого минимальная цена
            ph = [o for o in h["offers"] if price_ok(o["price"]) and o["company_id"]]
            if len({o["company_id"] for o in ph}) > 1:   # конкурс — только если цен было хотя бы две
                low = min(o["price"] for o in ph)
                for o in ph:
                    d = by_sup.get(o["company_id"])
                    if d is None:
                        d = by_sup.setdefault(o["company_id"], {
                            "company_id": o["company_id"], "company": o["company"], "position_ids": [],
                            "offers": 0, "wins": 0, "relevance": 0.0, "last": None, "purchase_ids": [],
                            "best_count": 0, "priced_count": 0, "cells": {}, "_best": {}, "_rel": {}})
                    d["priced_count"] += 1
                    d["best_count"] += 1 if o["price"] == low else 0
            for o in h["offers"]:
                of = _offer(o, want)
                # --- поставщики ---
                if o["company_id"]:
                    d = by_sup.setdefault(o["company_id"], {
                        "company_id": o["company_id"], "company": o["company"], "position_ids": [],
                        "offers": 0, "wins": 0, "relevance": 0.0, "last": None, "purchase_ids": [],
                        "best_count": 0, "priced_count": 0, "cells": {}, "_best": {}, "_rel": {}})
                    if pid_cur not in d["position_ids"]:
                        d["position_ids"].append(pid_cur)
                    d["offers"] += 1
                    d["wins"] += 1 if o["selected"] else 0
                    d["_rel"][pid_cur] = max(d["_rel"].get(pid_cur, 0), h["score"])
                    if o["purchase_id"] and o["purchase_id"] not in d["purchase_ids"]:
                        d["purchase_ids"].append(o["purchase_id"])
                    # последнее предложение — даже без цены, но при равной дате предпочитаем с ценой
                    if d["last"] is None or (_sort_date(of["date"]), of["price"] is not None) > \
                            (_sort_date(d["last"]["date"]), d["last"]["price"] is not None):
                        d["last"] = of
                    # ячейка матрицы: самое свежее предложение с ценой, сопоставимое — в приоритете
                    cell = d["cells"].get(str(pid_cur))
                    rank = (of["price"] is not None, of["comparable"], _sort_date(of["date"]))
                    if cell is None or rank > (cell["price"] is not None, cell["comparable"], _sort_date(cell["date"])):
                        d["cells"][str(pid_cur)] = of
                    if of["comparable"] and of["unit_price"] is not None:
                        cur = d["_best"].get(pid_cur)
                        if cur is None or of["unit_price"] < cur:
                            d["_best"][pid_cur] = of["unit_price"]
                # --- прошлые закупки ---
                if o["purchase_id"]:
                    g = by_pur.setdefault(o["purchase_id"], {"purchase_id": o["purchase_id"], "date": of["date"],
                                                             "position_ids": [], "matched": {}, "_sup": {}})
                    if _sort_date(of["date"]) > _sort_date(g["date"]):
                        g["date"] = of["date"]
                    if pid_cur not in g["position_ids"]:
                        g["position_ids"].append(pid_cur)
                    g["matched"].setdefault(pid_cur, [])
                    if h["name"] not in g["matched"][pid_cur]:
                        g["matched"][pid_cur].append(h["name"])
                    key = o["company_id"] or f"r{o['request_id']}"
                    gs = g["_sup"].setdefault(key, {"company_id": o["company_id"], "company": o["company"],
                                                    "wins": 0, "offers": []})
                    gs["wins"] += 1 if o["selected"] else 0
                    gs["offers"].append(dict(of, for_position_id=pid_cur))
        # --- аналоги того же типа: другие марки/модели, которые покупали у тех же или других поставщиков ---
        type_sup = {}
        t1 = time.time()
        if qtype:
            seen = {norm(h["name"]) for h in hits} | {norm(p.get("name") or "")}
            for o in type_offers(idx, qtype, seen, purchase_id, pids):
                if not o["company_id"]:
                    continue
                of = dict(_offer(o, want), analog=True)
                t = type_sup.setdefault(o["company_id"], {"company_id": o["company_id"], "company": o["company"],
                                                          "offers": 0, "wins": 0, "last": None, "items": []})
                t["offers"] += 1
                t["wins"] += 1 if o["selected"] else 0
                if o["name"] not in t["items"] and len(t["items"]) < 5:
                    t["items"].append(o["name"])
                if t["last"] is None or (of["price"] is not None, _sort_date(of["date"])) > \
                        (t["last"]["price"] is not None, _sort_date(t["last"]["date"])):
                    t["last"] = of
                # в матрицу — только если по самой позиции у этого поставщика ничего нет и аналог
                # похож по словам (защита от ошибок разметки: «лампочка … цоколь Е27» в типе «патроны»)
                if not shares_word(p.get("name"), o["name"]):
                    continue
                d = by_sup.setdefault(o["company_id"], {
                    "company_id": o["company_id"], "company": o["company"], "position_ids": [],
                    "offers": 0, "wins": 0, "relevance": 0.0, "last": None, "purchase_ids": [],
                    "best_count": 0, "priced_count": 0, "cells": {}, "_best": {}, "_rel": {}})
                cell = d["cells"].get(str(pid_cur))
                if cell is None or (cell["analog"] and (of["price"] is not None, _sort_date(of["date"])) >
                                    (cell["price"] is not None, _sort_date(cell["date"]))):
                    d["cells"][str(pid_cur)] = of
                    if pid_cur not in d["position_ids"]:
                        d["position_ids"].append(pid_cur)
                    if d["last"] is None:
                        d["last"] = of
        t_analog += time.time() - t1
        out.append({
            "id": pid_cur, "name": p.get("name"), "qty": p.get("qty"), "unit": p.get("unit"),
            "base_unit": label(want), "type": qtype,
            "type_suppliers": sorted(type_sup.values(), key=lambda t: (-t["wins"], -t["offers"]))[:10],
            "status": "found" if won else "asked" if hits else "new",   # брали / только запрашивали / не было
            "last_selected": _offer(won[0], want) if won else None,
            "best_price": priced[0][0] if priced else None,
            "matches": [{"position_id": h["position_id"], "name": h["name"], "qty": h["qty"], "unit": h["unit"],
                         "score": round(h["score"], 2), "date": (h["date"] or "")[:10],
                         "purchase_ids": h["purchase_ids"],
                         "offers": [_offer(o, want) for o in sorted(h["offers"], key=lambda o: (-(o["selected"] or 0),
                                                                                                 o["price"] or 1e18))]}
                        for h in hits]})

    # Поставщики. price_index: в среднем по позициям, во сколько раз дороже лучшей цены (1.0 — самый дешёвый)
    sups = []
    for d in by_sup.values():
        ratios = [v / best_up[k] for k, v in d.pop("_best").items() if best_up.get(k)]
        d["price_index"] = round(sum(ratios) / len(ratios), 2) if ratios else None
        d["relevance"] = round(sum(d.pop("_rel").values()), 2)
        d["last_date"] = d["last"]["date"] if d["last"] else ""
        # итог по закупке: Σ кол-во × цена за ед. по позициям, где цена сопоставима
        total, covered = 0.0, 0
        for p in positions:
            c = d["cells"].get(str(p.get("id")))
            q = to_float(p.get("qty"))
            if c and c["comparable"] and c["unit_price"] is not None and q:
                b, factor = basis(p.get("name"), p.get("unit"), p.get("weight"))
                total += c["unit_price"] * factor * q
                covered += 1
        d["total"] = round(total, 2) if covered else None
        d["total_covered"] = covered
        sups.append(d)
    # Порядок по умолчанию: больше позиций этой закупки → чаще выбирали → ближе по смыслу → свежее
    sups.sort(key=lambda d: (-len(d["position_ids"]), -d["wins"], -d["relevance"],
                             tuple(-int(x or 0) for x in _sort_date(d["last_date"]))))
    # Столбцы матрицы: у каждой позиции — свои лучшие 3 поставщика (кого выбирали, с ценой, свежее),
    # чтобы позиция не оставалась пустой из-за того, что столбцы заняли поставщики других позиций
    pick, ranked = {}, []
    for p in positions:
        key = str(p.get("id"))
        cand = [d for d in sups if key in d["cells"]]
        cand.sort(key=lambda d: (not d["cells"][key]["analog"], d["cells"][key]["selected"],
                                 d["cells"][key]["price"] is not None and d["cells"][key]["comparable"],
                                 _sort_date(d["cells"][key]["date"]), d["wins"]), reverse=True)
        ranked.append(cand[:per_position])
        for d in cand[:per_position]:
            pick.setdefault(d["company_id"], []).append(p.get("id"))
    order = {d["company_id"]: i for i, d in enumerate(sups)}
    columns = []   # по кругу: лучший у каждой позиции, затем вторые, затем третьи
    for rank in range(per_position):
        for cand in ranked:
            if rank < len(cand) and cand[rank]["company_id"] not in columns and len(columns) < max_columns:
                columns.append(cand[rank]["company_id"])
    for d in sups:
        d["top_for"] = pick.get(d["company_id"], [])
    sups = [d for d in sups if d["company_id"] in set(columns)] + [d for d in sups if d["company_id"] not in set(columns)]
    sups.sort(key=lambda d: columns.index(d["company_id"]) if d["company_id"] in columns else len(columns) + order[d["company_id"]])

    # Прошлые закупки: больше совпавших позиций → свежее
    purs = []
    for g in by_pur.values():
        g["suppliers"] = sorted(g.pop("_sup").values(), key=lambda x: (-x["wins"], -len(x["offers"])))
        purs.append(g)
    purs.sort(key=lambda g: (-len(g["position_ids"]), tuple(-int(x or 0) for x in _sort_date(g["date"]))))
    return {"ok": True, "purchase_id": purchase_id,
            "updated": (_state["sync"] or "")[:16].replace("T", " "),
            "sort": {"suppliers": "позиций этой закупки ↓, выбирали ↓, похожесть ↓, дата ↓",
                     "purchases": "совпавших позиций ↓, дата ↓"},
            "columns": columns, "positions": out, "suppliers": sups[:30], "purchases": purs[:15],
            "timing": dict(timing, search=round(t_search, 2), analogs=round(t_analog, 2),
                           total=round(time.time() - t_start, 2))}


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
        price = fmt_price(o["price"]) + (f'/{e(o["unit"])}' if o["unit"] and o["price"] else "")
        if o["unit_price"] and o["base_unit"] and not o["base_unit"] == (o["unit"] or "").rstrip("."):
            price += f' <span class="muted">≈ {o["unit_price"]:,.2f} ₽/{e(o["base_unit"])}</span>'.replace(",", " ")
        if not o["comparable"]:
            price = f'<span class="muted" title="другая единица — не сравнивать напрямую">{price}</span>'
        return (f'{comp} {price}{" ✓" if o["selected"] else ""} '
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

    def purl(ids):
        return ", ".join(f'<a href="{L["purchase"](i)}" target="_blank">{i}</a>' for i in ids[:5])

    sup = "".join(
        f'<tr><td><a href="{L["company"](s["company_id"])}" target="_blank">{e(s["company"])}</a></td>'
        f'<td>{", ".join("№%s" % nums.get(i, i) for i in s["position_ids"])}</td><td>{s["wins"]}</td><td>{s["offers"]}</td>'
        f'<td>{"лучшая" if s["price_index"] == 1 else (s["price_index"] or "нет цены")}</td>'
        f'<td>{off(s["last"])}</td>'
        f'<td>{purl(s["purchase_ids"])}</td></tr>'
        for s in d["suppliers"])
    pur = "".join(
        f'<tr><td><a href="{L["purchase"](g["purchase_id"])}" target="_blank">закупка {g["purchase_id"]}</a></td>'
        f'<td>{e(g["date"])}</td><td>' +
        "<br>".join(f'№{nums.get(i, i)} <span class="muted">как «{e("», «".join(g["matched"].get(i, [])))}»</span>'
                    for i in g["position_ids"]) + "</td><td>" +
        "<br>".join(f'{e(x["company"] or "—")}: ' + "; ".join(
            f'№{nums.get(o["for_position_id"], "")} {fmt_price(o["price"])}{" ✓" if o["selected"] else ""}'
            for o in x["offers"]) for x in g["suppliers"]) + "</td></tr>"
        for g in d.get("purchases", []))
    return (f'<style>{CSS}</style><div class="note">Данные на {e(d["updated"])}. Предложения закупки {d["purchase_id"]} '
            f'исключены.</div><h3>Позиции</h3><table><tr><th>№</th><th>Позиция</th><th>Кол-во</th>'
            f'<th>Последний раз выбрали</th><th>Лучшая цена в истории</th></tr>{"".join(rows)}</table>'
            f'<h3>Кому отправить запросы</h3><div class="note">Порядок: {e(d["sort"]["suppliers"])}. '
            f'«Цена» — во сколько раз дороже лучшей по тем же позициям.</div>'
            f'<table><tr><th>Поставщик</th><th>Позиции</th><th>Выбирали</th><th>Предложений</th><th>Цена</th>'
            f'<th>Последнее предложение</th><th>Закупки</th></tr>{sup}</table>'
            f'<h3>Похожие прошлые закупки</h3><div class="note">Порядок: {e(d["sort"]["purchases"])}.</div>'
            f'<table><tr><th>Закупка</th><th>Дата</th><th>Совпали позиции</th><th>Поставщики (✓ — выбран)</th></tr>{pur}</table>')


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
    ncol = max(1, min(int(body.get("columns") or 8), 15))
    ck = (pid, k, ncol, json.dumps(positions, sort_keys=True, ensure_ascii=False), _state["sync"])
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_SEC and not body.get("refresh"):
        return JSONResponse(hit[1])
    try:
        d = advise(pid, positions, k, max_columns=ncol)
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
