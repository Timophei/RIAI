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
def offer_row(el, of, om):
    sel_raw = enum_text(om, of.get("selected"), first(el, of.get("selected")))
    return (int(el["ID"]), el.get("NAME"),
            to_id(first(el, of.get("position"))), to_id(first(el, of.get("request"))),
            to_num(first(el, of.get("price"))), 1 if is_yes(sel_raw) else 0,
            first(el, of.get("comment")), first(el, of.get("in_stock")),
            enum_text(om, of.get("vat"), first(el, of.get("vat"))),
            to_id(first(el, of.get("parent"))),
            el.get("DATE_CREATE"), el.get("TIMESTAMP_X"), None)


def position_row(el, pf, pm):
    return (int(el["ID"]), el.get("NAME"),
            first(el, pf.get("qty")),
            enum_text(pm, pf.get("unit"), first(el, pf.get("unit"))),
            first(el, pf.get("weight")),
            to_id(first(el, pf.get("purchase"))),
            enum_text(pm, pf.get("status"), first(el, pf.get("status"))),
            first(el, pf.get("justification")),
            el.get("DATE_CREATE"), el.get("TIMESTAMP_X"), None)


def request_row(it, ptype):
    return (int(it["id"]), it.get("title"), to_id(it.get("companyId")) or None,
            to_id(it.get(f"parentId{ptype}")) if ptype else None,
            it.get("createdTime"), it.get("updatedTime"), it.get("stageId"), None)


def stage(con, b, name, label, method, params, filter_param, id_field, extract, table, ncols, to_row,
          since, since_filter):
    """Одна сущность: окнами по ID, запись в базу сразу, курсор сохраняется после каждого окна."""
    cursor = int(db.get_meta(con, f"{name}_cursor", 0))
    if cursor:
        print(f"  {label}: продолжаю после ID {cursor}")
    base = dict(params)
    if since:
        base[filter_param] = {since_filter: since}
    sql = f"INSERT OR REPLACE INTO {table} VALUES({','.join('?' * ncols)})"

    def pages(base):
        for pg, got, total in b.iter_pages(method, base, filter_param, id_field, extract, start_after=cursor):
            con.executemany(sql, [to_row(x) for x in pg])
            db.set_meta(con, f"{name}_cursor", max(int(x[id_field]) for x in pg))
            con.commit()
            print(f"\r  {label}: {got}/{total}   ", end="", flush=True)
            yield got

    n = 0
    try:
        for n in pages(base):
            pass
    except B24Error as e:
        if not since:
            raise
        print(f"\n  инкремент не сработал ({e}), качаю всё")
        base.pop(filter_param, None)
        for n in pages(base):
            pass
    print(f"\r  {label}: получено {n}            ")
    con.execute("DELETE FROM meta WHERE key=?", (f"{name}_cursor",))
    con.commit()
    return n


def run(full=False, restart=False):
    cfg = load_cfg()
    b = B24()
    con = db.connect()
    in_progress = db.get_meta(con, "full_in_progress")
    if restart or (full and not in_progress):
        for k in ("offers", "positions", "requests"):
            con.execute("DELETE FROM meta WHERE key IN (?,?)", (f"{k}_ts", f"{k}_cursor"))
        db.set_meta(con, "full_in_progress", 1)
        con.commit()
    elif in_progress:
        print("Продолжаю прерванную полную загрузку")

    of, om = cfg["offer_fields"], cfg["offer_meta"]
    pf, pm = cfg["position_fields"], cfg["position_meta"]
    ibt = cfg["iblock_type"]

    def list_params(iblock_id, meta):
        return {"IBLOCK_TYPE_ID": ibt, "IBLOCK_ID": iblock_id, "ELEMENT_ORDER": {"ID": "ASC"},
                "SELECT": ["ID", "NAME", "DATE_CREATE", "TIMESTAMP_X"] +
                          [k for k in meta if k.startswith("PROPERTY_")]}

    counts = {}
    for name, label, iblock, meta, row, table, ncols in (
            ("offers", "Позиции предложения", cfg["offers_iblock_id"], om, lambda e: offer_row(e, of, om), "offers", 13),
            ("positions", "Позиции", cfg["positions_iblock_id"], pm, lambda e: position_row(e, pf, pm), "positions", 11)):
        if db.get_meta(con, "full_in_progress") and db.get_meta(con, f"{name}_ts"):
            continue  # этот этап полной загрузки уже завершён
        since = db.get_meta(con, f"{name}_ts")
        counts[name] = stage(con, b, name, label, "lists.element.get", list_params(iblock, meta),
                             "FILTER", "ID", lambda r: r, table, ncols, row, since, ">=TIMESTAMP_X")
        ts = max_ts(({"t": r[0]} for r in con.execute(f"SELECT ts FROM {table}")), "t")
        if ts:
            db.set_meta(con, f"{name}_ts", ts)
        con.commit()

    ptype = cfg.get("purchase_entity_type_id")
    if not (db.get_meta(con, "full_in_progress") and db.get_meta(con, "requests_ts")):
        since = db.get_meta(con, "requests_ts")
        counts["requests"] = stage(
            con, b, "requests", "Запросы поставщику", "crm.item.list",
            {"entityTypeId": cfg["request_entity_type_id"], "order": {"id": "ASC"},
             "select": ["id", "title", "companyId", "createdTime", "updatedTime", "stageId"] +
                       ([f"parentId{ptype}"] if ptype else [])},
            "filter", "id", lambda r: (r or {}).get("items", []), "requests", 8,
            lambda it: request_row(it, ptype), since, ">=updatedTime")
        ts = con.execute("SELECT MAX(updated) FROM requests").fetchone()[0]
        if ts:
            db.set_meta(con, "requests_ts", ts)
        con.commit()

    # Компании-поставщики: недостающие (при полной загрузке — все)
    q = "SELECT DISTINCT company_id FROM requests WHERE company_id>0"
    if not in_progress and not full:
        q += " AND company_id NOT IN (SELECT id FROM companies)"
    need = [r[0] for r in con.execute(q)]
    for i in range(0, len(need), 500):
        chunk = need[i:i + 500]
        cmds = {f"c{j}": ("crm.company.list", {"filter": {"ID": chunk[j:j + 50]}, "select": ["ID", "TITLE"]})
                for j in range(0, len(chunk), 50)}
        for res in b.batch(cmds).values():
            con.executemany("INSERT OR REPLACE INTO companies VALUES(?,?)",
                            [(int(c["ID"]), c.get("TITLE")) for c in res or []])
        con.commit()

    con.execute("DELETE FROM meta WHERE key='full_in_progress'")
    stats = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("offers", "positions", "requests", "companies")}
    db.set_meta(con, "last_sync", datetime.now().isoformat(timespec="seconds"))
    con.commit()
    print(f"Готово. В базе: {stats}. Получено сейчас: {counts}, компаний {len(need)}. Вызовов API: {b.calls}")


if __name__ == "__main__":
    run(full="--full" in sys.argv, restart="--restart" in sys.argv)
