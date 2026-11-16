"""Текстовый отчёт: что помощник подскажет по заданным позициям + сводка по базе."""
import os
from datetime import datetime

from . import db
from .search import Index, supplier_summary, price_ok, _sort_date

DEFAULT_QUERIES = ["Бумага А4", "Швеллер 40", "Кирпич печной", "Бетон М300"]


def fmt_price(p):
    return f"{p:,.0f} ₽".replace(",", " ") if price_ok(p) else "—"


def run(queries=None):
    queries = queries or DEFAULT_QUERIES
    con = db.connect()
    idx = Index(con)
    L = [f"# Отчёт помощника снабжения — {datetime.now():%d.%m.%Y %H:%M}", ""]

    # Сводка по базе
    n = lambda q: con.execute(q).fetchone()[0]
    total = n("SELECT COUNT(*) FROM offers")
    L += ["## База", "",
          f"- Предложений: {total}; с ценой: {n('SELECT COUNT(*) FROM offers WHERE price>1')}; "
          f"выбранных: {n('SELECT COUNT(*) FROM offers WHERE selected=1')}",
          f"- Позиций: {n('SELECT COUNT(*) FROM positions')}; запросов поставщику: "
          f"{n('SELECT COUNT(*) FROM requests')}; поставщиков: {n('SELECT COUNT(*) FROM companies')}",
          f"- Предложений без найденного запроса: "
          f"{n('SELECT COUNT(*) FROM offers WHERE request_id IS NULL OR request_id NOT IN (SELECT id FROM requests)')}",
          f"- Запросов без компании: {n('SELECT COUNT(*) FROM requests WHERE company_id IS NULL OR company_id=0')}",
          f"- Последняя синхронизация: {db.get_meta(con, 'last_sync', '—')}", ""]

    top = con.execute("""SELECT c.title, COUNT(*) n, SUM(o.selected) w FROM offers o
        JOIN requests r ON r.id=o.request_id JOIN companies c ON c.id=r.company_id
        GROUP BY c.id ORDER BY w DESC LIMIT 15""").fetchall()
    if top:
        L += ["### Топ-15 поставщиков по выигранным позициям", "", "| Поставщик | Предложений | Выиграно |",
              "|---|---|---|"] + [f"| {r['title']} | {r['n']} | {r['w']} |" for r in top] + [""]

    for q in queries:
        hits = idx.search(q, k=10)
        L += [f"## {q}", ""]
        if not hits:
            L += ["**Не закупали** — похожих позиций в истории нет.", ""]
            continue
        L += ["| Похожесть | Позиция в истории | Дата | Предложения (поставщик: цена, ✓ = выбран) |",
              "|---|---|---|---|"]
        for h in hits:
            offs = sorted(h["offers"], key=lambda o: (-(o["selected"] or 0), o["price"] or 1e18))
            cell = "; ".join(f"{o['company'] or ('запрос #%s' % o['request_id'])}: {fmt_price(o['price'])}"
                             f"{' ✓' if o['selected'] else ''}" for o in offs[:6])
            if len(offs) > 6:
                cell += f"; …ещё {len(offs) - 6}"
            unit = f" ({h['qty'] or ''} {h['unit'] or ''})".replace("( )", "") if h["unit"] or h["qty"] else ""
            L.append(f"| {h['score']:.2f} | {h['name']}{unit} | {(h['date'] or '')[:10]} | {cell} |")
        L += ["", "**Кому отправить запросы** (по найденному):", ""]
        for s in supplier_summary(hits)[:5]:
            last = (f"последняя цена {fmt_price(s['last'][0])} от {s['last'][1][:10]} за «{s['last'][2]}»"
                    if s["last"] else "цен нет")
            L.append(f"- {s['supplier']} — предложений {s['offers']}, выбран {s['wins']} раз; {last}")
        L.append("")

    text = "\n".join(L)
    path = os.path.join(db.DATA, f"report_{datetime.now():%Y%m%d_%H%M}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nСохранено: {path}")
