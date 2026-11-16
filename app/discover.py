"""Автопоиск ID списков, смарт-процессов и полей. Пишет /data/config.json."""
import json
import os

from .b24 import B24

DATA = os.environ.get("DATA_DIR", "./data")
CONFIG = os.path.join(DATA, "config.json")

# Поле -> варианты названий (в нижнем регистре). Сначала точное совпадение, потом «содержит».
OFFER_FIELDS = {
    "request": ["запрос", "запрос поставщику"],
    "position": ["позиция"],
    "price": ["цена предложения", "цена"],
    "selected": ["позиция выбрана", "выбрано", "выбрана"],
    "comment": ["комментарий или замена", "комментарий"],
    "in_stock": ["в наличии"],
    "vat": ["ндс"],
    "parent": ["родительская позиция предложения", "родительская"],
}
POSITION_FIELDS = {
    "qty": ["количество", "кол-во"],
    "unit": ["единица измерения", "ед. изм.", "ед.изм", "единица"],
    "weight": ["вес"],
    "purchase": ["закупка"],
    "subrequest": ["под. заявка", "подзаявка", "под заявка"],
    "supply_request": ["заявка на закупку", "заявка на снабжение", "заявка"],
    "chosen_request": ["выбранный запрос поставщику", "выжранный запрос поставщику", "выбранный запрос"],
    "status": ["статус"],
    "justification": ["обоснование"],
    "products": ["товары"],
    "delivery_date": ["дата поставки", "срок поставки"],
}


def match_fields(fields, wanted):
    """fields: {FIELD_ID: {NAME,...}} -> {key: FIELD_ID}."""
    names = {fid: (f.get("NAME") or "").strip().lower() for fid, f in fields.items()}
    used, out = set(), {}
    for key, variants in wanted.items():
        hit = None
        for v in variants:
            hit = next((fid for fid, n in names.items() if n == v and fid not in used), None)
            if hit:
                break
        if not hit:
            for v in variants:
                hit = next((fid for fid, n in names.items() if v in n and fid not in used), None)
                if hit:
                    break
        if hit:
            out[key] = hit
            used.add(hit)
    return out


def field_meta(fields):
    meta = {}
    for fid, f in fields.items():
        m = {"name": f.get("NAME"), "type": f.get("TYPE")}
        dv = f.get("DISPLAY_VALUES_FORM")
        if isinstance(dv, dict) and dv:
            m["enum"] = {str(k): v for k, v in dv.items()}
        meta[fid] = m
    return meta


def run():
    b = B24()
    ibt = os.environ.get("LISTS_IBLOCK_TYPE_ID", "lists")
    print("== Смарт-процессы")
    types = b.call("crm.type.list", {}).get("result", {}).get("types", [])
    for t in types:
        print(f"  entityTypeId={t.get('entityTypeId'):>5}  {t.get('title')}")

    def find_type(*words):
        for t in types:
            title = (t.get("title") or "").lower()
            if any(w in title for w in words):
                return t
        return None

    req_type = int(os.environ.get("REQUEST_ENTITY_TYPE_ID") or
                   (find_type("запрос поставщ") or {}).get("entityTypeId") or 183)
    purchase_type = (find_type("закупк") or {}).get("entityTypeId")

    print("== Списки")
    lists = b.call("lists.get", {"IBLOCK_TYPE_ID": ibt}).get("result", [])
    for l in lists:
        print(f"  ID={l.get('ID'):>5}  {l.get('NAME')}")

    def find_list(*words):
        for l in lists:
            n = (l.get("NAME") or "").lower()
            if any(w in n for w in words):
                return int(l["ID"])
        return None

    pos_id = int(os.environ.get("POSITIONS_IBLOCK_ID") or 15)
    off_id = os.environ.get("OFFERS_IBLOCK_ID") or find_list("позиция предлож", "позиции предлож", "предложени")
    if not off_id:
        raise SystemExit("Не нашёл список «Позиция предложения». Задайте OFFERS_IBLOCK_ID в .env")
    off_id = int(off_id)

    off_fields = b.call("lists.field.get", {"IBLOCK_TYPE_ID": ibt, "IBLOCK_ID": off_id}).get("result", {})
    pos_fields = b.call("lists.field.get", {"IBLOCK_TYPE_ID": ibt, "IBLOCK_ID": pos_id}).get("result", {})
    req_fields = b.call("crm.item.fields", {"entityTypeId": req_type}).get("result", {}).get("fields", {})

    cfg = {
        "iblock_type": ibt,
        "offers_iblock_id": off_id,
        "positions_iblock_id": pos_id,
        "request_entity_type_id": req_type,
        "purchase_entity_type_id": purchase_type,
        "offer_fields": match_fields(off_fields, OFFER_FIELDS),
        "position_fields": match_fields(pos_fields, POSITION_FIELDS),
        "offer_meta": field_meta(off_fields),
        "position_meta": field_meta(pos_fields),
        "request_parent_fields": [k for k in req_fields if k.startswith("parentId")],
    }
    os.makedirs(DATA, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"\n== Запрос поставщику: entityTypeId={req_type}; Закупка: {purchase_type}")
    for title, fmap, meta in (("Позиция предложения", cfg["offer_fields"], cfg["offer_meta"]),
                              ("Позиция", cfg["position_fields"], cfg["position_meta"])):
        print(f"== {title}: найденные поля")
        for k, fid in fmap.items():
            print(f"  {k:15} -> {fid:15} «{meta[fid]['name']}» ({meta[fid]['type']})")
    missing = [k for k in ("request", "position", "price", "selected") if k not in cfg["offer_fields"]]
    if missing:
        print(f"!! Не найдены обязательные поля предложения: {missing}. Поправьте offer_fields в {CONFIG}")
    print(f"\nСохранено: {CONFIG}. Вызовов API: {b.calls}")
