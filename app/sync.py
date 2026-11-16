"""Синхронизация предложений, позиций, запросов поставщику и компаний в SQLite.
По умолчанию — только изменённое с прошлого запуска; --full — всё заново."""
import json
import os
import re
import sys
from datetime import datetime

from . import db
from .b24 import B24, B24Error
from .discover import CONFIG


def load_cfg():
    if not os.path.exists(CONFIG):
        raise SystemExit("Нет config.json — сначала запустите: discover")
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


# ---------- разбор значений ----------
def values(el, fid):
    if not fid:
        return []
    v = el.get(fid)
    if v is None or v == "" or v is False:
        return []
    if isinstance(v, dict):
        return [x for x in v.values() if x not in (None, "")]
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")]
    return [v]


def first(el, fid):
    vs = values(el, fid)
    v = vs[0] if vs else None
    if isinstance(v, dict):  # {"TEXT": ..., "TYPE": ...} у HTML-полей
        v = v.get("TEXT") or v.get("VALUE")
    return v


def to_num(v):
    if v is None:
        return None
    s = str(v).split("|")[0]
    s = s.replace("\xa0", "").replace(" ", "").replace("₽", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def to_id(v):
    """'Tb7_64730', 'DYNAMIC_183_64730', '64730' -> 64730."""
    if v is None:
        return None
    m = re.search(r"(\d+)\s*$", str(v))
    return int(m.group(1)) if m else None


def enum_text(meta, fid, v):
    if v is None:
        return None
    enum = (meta.get(fid) or {}).get("enum")
    if enum and str(v) in enum:
        return enum[str(v)]
    return str(v)


def is_yes(s):
    return str(s or "").strip().lower() in ("да", "y", "yes", "1", "true")


def parse_dt(s):
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(s), fmt)
        except (ValueError, TypeError):
            pass
    return None


def max_ts(rows, key):
    best = None
    for r in rows:
        d = parse_dt(r.get(key))
        if d and (best is None or d > best[0]):
            best = (d, r.get(key))
    return best[1] if best else None


# ---------- выгрузка ----------
def fetch_list(b, cfg, iblock_id, meta, since, label):
    params = {
        "IBLOCK_TYPE_ID": cfg["iblock_type"],
        "IBLOCK_ID": iblock_id,
        "ELEMENT_ORDER": {"ID": "ASC"},
        "SELECT": ["ID", "NAME", "DATE_CREATE", "TIMESTAMP_X"] + [k for k in meta if k.startswith("PROPERTY_")],
    }
    if since:
        params["FILTER"] = {">=TIMESTAMP_X": since}
    prog = lambda n, t: print(f"\r  {label}: {n}/{t}", end="", flush=True)
    try:
        items = b.list_all("lists.element.get", params, progress=prog)
    except B24Error as e:
        if not since:
            raise
        print(f"\n  инкремент не сработал ({e}), качаю всё")
        params.pop("FILTER")
        items = b.list_all("lists.element.get", params, progress=prog)
    print()
    return items


def run(full=False):
    cfg = load_cfg()
    b = B24()
    con = db.connect()
    if full:
        for k in ("offers_ts", "positions_ts", "requests_ts"):
            con.execute("DELETE FROM meta WHERE key=?", (k,))

    of, om = cfg["offer_fields"], cfg["offer_meta"]
    pf, pm = cfg["position_fields"], cfg["position_meta"]

    # Позиции предложения
    since = db.get_meta(con, "offers_ts")
    offers = fetch_list(b, cfg, cfg["offers_iblock_id"], om, since, "Позиции предложения")
    for el in offers:
        sel_raw = enum_text(om, of.get("selected"), first(el, of.get("selected")))
        con.execute("INSERT OR REPLACE INTO offers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            int(el["ID"]), el.get("NAME"),
            to_id(first(el, of.get("position"))), to_id(first(el, of.get("request"))),
            to_num(first(el, of.get("price"))), 1 if is_yes(sel_raw) else 0,
            first(el, of.get("comment")), first(el, of.get("in_stock")),
            enum_text(om, of.get("vat"), first(el, of.get("vat"))),
            to_id(first(el, of.get("parent"))),
            el.get("DATE_CREATE"), el.get("TIMESTAMP_X"),
            json.dumps(el, ensure_ascii=False)))
    ts = max_ts(offers, "TIMESTAMP_X")
    if ts:
        db.set_meta(con, "offers_ts", ts)
    con.commit()

    # Позиции
    since = db.get_meta(con, "positions_ts")
    positions = fetch_list(b, cfg, cfg["positions_iblock_id"], pm, since, "Позиции")
    for el in positions:
        con.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            int(el["ID"]), el.get("NAME"),
            first(el, pf.get("qty")),
            enum_text(pm, pf.get("unit"), first(el, pf.get("unit"))),
            first(el, pf.get("weight")),
            to_id(first(el, pf.get("purchase"))),
            enum_text(pm, pf.get("status"), first(el, pf.get("status"))),
            first(el, pf.get("justification")),
            el.get("DATE_CREATE"), el.get("TIMESTAMP_X"),
            json.dumps(el, ensure_ascii=False)))
    ts = max_ts(positions, "TIMESTAMP_X")
    if ts:
        db.set_meta(con, "positions_ts", ts)
    con.commit()

    # Запросы поставщику (смарт-процесс)
    etid = cfg["request_entity_type_id"]
    ptype = cfg.get("purchase_entity_type_id")
    params = {"entityTypeId": etid, "order": {"id": "ASC"}, "select": ["*"]}
    since = db.get_meta(con, "requests_ts")
    if since:
        params["filter"] = {">=updatedTime": since}
    reqs = b.list_all("crm.item.list", params, extract=lambda r: (r or {}).get("items", []),
                      progress=lambda n, t: print(f"\r  Запросы поставщику: {n}/{t}", end="", flush=True))
    print()
    for it in reqs:
        con.execute("INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?,?,?,?)", (
            int(it["id"]), it.get("title"), to_id(it.get("companyId")) or None,
            to_id(it.get(f"parentId{ptype}")) if ptype else None,
            it.get("createdTime"), it.get("updatedTime"), it.get("stageId"),
            json.dumps(it, ensure_ascii=False)))
    if reqs:
        db.set_meta(con, "requests_ts", max(r.get("updatedTime") or "" for r in reqs))
    con.commit()

    # Компании-поставщики
    need = [r[0] for r in con.execute(
        "SELECT DISTINCT company_id FROM requests WHERE company_id>0 "
        "AND company_id NOT IN (SELECT id FROM companies)")]
    if since is None:  # при полной синхронизации обновляем и старые названия
        need = [r[0] for r in con.execute("SELECT DISTINCT company_id FROM requests WHERE company_id>0")]
    cmds = {f"c{i}": ("crm.company.list", {"filter": {"ID": need[i:i + 50]}, "select": ["ID", "TITLE"]})
            for i in range(0, len(need), 50)}
    if cmds:
        for res in b.batch(cmds).values():
            for c in res or []:
                con.execute("INSERT OR REPLACE INTO companies VALUES(?,?)", (int(c["ID"]), c.get("TITLE")))
    con.commit()

    stats = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("offers", "positions", "requests", "companies")}
    db.set_meta(con, "last_sync", datetime.now().isoformat(timespec="seconds"))
    con.commit()
    print(f"Готово. В базе: {stats}. Получено сейчас: предложений {len(offers)}, позиций {len(positions)}, "
          f"запросов {len(reqs)}, компаний {len(need)}. Вызовов API: {b.calls}")


if __name__ == "__main__":
    run(full="--full" in sys.argv)
