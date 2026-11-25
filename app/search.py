"""Поиск похожих позиций по истории (этап 1: текстовая похожесть по символам).
Этап 2 добавит разбор параметров и проверку совпадений моделью."""
import re
from collections import defaultdict

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from . import db

import os

NOT_FOUND = 0.45  # ниже — считаем, что ничего похожего не закупали
# совпадения слабее RELATIVE × лучшее отбрасываем: «Соль техническая» не тянет за собой «Соль пищевую» с 0.5
RELATIVE = float(os.environ.get("MATCH_RELATIVE", "0.6"))


def norm(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\bразделенн\w*\b|\(со склада\)", " ", s)
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    s = re.sub(r"(?<=\d)\s*[*хx×]\s*(?=\d)", " x ", s)
    s = re.sub(r"[^\w.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def price_ok(p):
    return p is not None and p > 1  # 0 и 1 ₽ — заглушки «нет цены»


class Index:
    """В памяти — только уникальные нормализованные названия; предложения читаются из базы для найденного."""

    def __init__(self, con=None):
        self.con = con or db.connect()
        groups = defaultdict(list)
        for pid, name in self.con.execute("""
                SELECT o.position_id, COALESCE(p.name, MAX(o.name)) FROM offers o
                LEFT JOIN positions p ON p.id = o.position_id
                WHERE o.position_id IS NOT NULL GROUP BY o.position_id"""):
            n = norm(name)
            if n:
                groups[n].append(pid)
        self.names = list(groups)
        self.pids = [groups[n] for n in self.names]
        del groups
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1, sublinear_tf=True,
                                   dtype=np.float32)
        self.X = self.vec.fit_transform(self.names) if self.names else None

    def _load(self, pids):
        q = ",".join("?" * len(pids))
        rows = self.con.execute(f"""
            SELECT o.id, o.position_id, COALESCE(p.name, o.name) AS name, o.price, o.selected,
                   o.comment, o.date_create, o.request_id, r.company_id, c.title AS company,
                   r.purchase_id AS purchase_id, r.title AS request_title, p.unit, p.qty, p.weight, p.status
            FROM offers o
            LEFT JOIN positions p ON p.id = o.position_id
            LEFT JOIN requests r ON r.id = o.request_id
            LEFT JOIN companies c ON c.id = r.company_id
            WHERE o.position_id IN ({q})""", pids).fetchall()
        pos = {}
        for r in rows:
            p = pos.setdefault(r["position_id"], {
                "position_id": r["position_id"], "name": r["name"], "unit": r["unit"], "qty": r["qty"],
                "weight": r["weight"], "status": r["status"], "offers": []})
            p["offers"].append(dict(r))
        for p in pos.values():
            p["date"] = min((o["date_create"] or "" for o in p["offers"]), key=_sort_date)
            purchases = {o["purchase_id"] for o in p["offers"] if o["purchase_id"]}
            p["purchase_ids"] = sorted(purchases)
        return pos

    def search(self, query, k=10, exclude_purchase=None, exclude_pids=()):
        if self.X is None:
            return []
        sims = (self.X @ self.vec.transform([norm(query)]).T).toarray().ravel()
        out = []
        order = sims.argsort()[::-1]
        floor = max(NOT_FOUND, RELATIVE * float(sims[order[0]])) if len(order) else NOT_FOUND
        for i in order:
            if sims[i] < floor or len(out) >= k:
                break
            pos = self._load(self.pids[i])
            for p in sorted(pos.values(), key=lambda p: _sort_date(p["date"]), reverse=True):
                if p["position_id"] in exclude_pids:
                    continue
                if exclude_purchase:  # предложения текущей закупки — не история
                    p["offers"] = [o for o in p["offers"] if o["purchase_id"] != exclude_purchase]
                    if not p["offers"]:
                        continue
                if len(out) < k:
                    out.append(dict(p, score=float(sims[i])))
        return out


def _sort_date(s):
    m = re.match(r"(\d\d)\.(\d\d)\.(\d{4})", s or "")
    return (m.group(3), m.group(2), m.group(1)) if m else ("", "", "")


def supplier_summary(hits):
    """Кто поставлял найденное: число предложений, побед, последняя и минимальная цена."""
    agg = defaultdict(lambda: {"company_id": None, "offers": 0, "wins": 0, "prices": [], "last": None, "rank": 0.0})
    for h in hits:
        for o in h["offers"]:
            name = o["company"] or (f"компания #{o['company_id']}" if o["company_id"] else
                                    f"запрос #{o['request_id']} (без компании)")
            a = agg[name]
            a["company_id"] = o["company_id"]
            a["offers"] += 1
            a["wins"] += o["selected"] or 0
            a["rank"] += h["score"] * (1 + 2 * (o["selected"] or 0) + (1 if price_ok(o["price"]) else 0))
            if price_ok(o["price"]):
                a["prices"].append(o["price"])
                if a["last"] is None or _sort_date(o["date_create"]) > _sort_date(a["last"][1]):
                    a["last"] = (o["price"], o["date_create"], h["name"])
    res = [dict(supplier=k, **v) for k, v in agg.items()]
    res.sort(key=lambda a: -a["rank"])
    return res
