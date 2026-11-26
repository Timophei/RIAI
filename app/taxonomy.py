"""Дерево типов номенклатуры: «Раздел › Группа › Тип».

  taxonomy-propose       модель строит дерево от частых «главных слов» истории и сразу убирает его → data/taxonomy.txt
  taxonomy-consolidate   ещё раз навести порядок в дереве (после ручной правки или если осталось дробно)
                         (файл можно и нужно поправить руками: переименовать, слить, добавить типы)
  taxonomy-classify      раскладывает все названия истории по типам дерева → таблица name_types
                         (продолжает с места остановки; --reset — заново, после правки дерева)
Тип — товары, которые заменяют друг друга или покупаются у одних и тех же поставщиков:
КПТ-8, DEEPCOOL Z3 и Thermalright TF3 — один тип «Термопаста».
"""
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import db
from .llm import chat_json
from .search import head, norm

TREE = os.path.join(db.DATA, "taxonomy.txt")
OTHER = "Прочее › Не определено › Не определено"


# ---------- файл дерева ----------
def load_tree(path=TREE):
    """# Раздел / ## Группа / - Тип  →  ['Раздел › Группа › Тип', ...]"""
    if not os.path.exists(path):
        raise SystemExit(f"Нет {path} — сначала: taxonomy-propose")
    sec = grp = None
    leaves = []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip()
        if line.startswith("## "):
            grp = line[3:].strip()
        elif line.startswith("# "):
            sec, grp = line[2:].strip(), None
        elif line.lstrip().startswith("- ") and sec and grp:
            t = line.lstrip()[2:].split("  //")[0].strip()   # «- Тип  // примеры» — примеры игнорируются
            if t:
                leaves.append(f"{sec} › {grp} › {t}")
    if not leaves:
        raise SystemExit(f"В {path} не нашлось ни одного типа")
    return leaves


def load_tree_examples(path=TREE):
    """[(путь, [примеры])] — для уборки дерева."""
    sec = grp = None
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip()
        if line.startswith("## "):
            grp = line[3:].strip()
        elif line.startswith("# "):
            sec, grp = line[2:].strip(), None
        elif line.lstrip().startswith("- ") and sec and grp:
            body = line.lstrip()[2:]
            t, _, ex = body.partition("  //")
            if t.strip():
                out.append((f"{sec} › {grp} › {t.strip()}", [e.strip() for e in ex.split(";") if e.strip()]))
    return out


def build_tree(pairs):
    """[(путь, примеры)] → структура для save_tree; одинаковые пути склеиваются, порядок разделов — как в SECTIONS."""
    tree = {}
    for path, ex in pairs:
        sec, grp, typ = [x.strip() for x in path.split("›")]
        t = tree.setdefault(sec, {}).setdefault(grp, {}).setdefault(typ, [])
        for e in ex:
            if e not in t and len(t) < 3:
                t.append(e)
    order = [s for s in SECTIONS if s in tree] + sorted(s for s in tree if s not in SECTIONS)
    return [{"name": s, "groups": [{"name": g, "types": [{"name": t, "examples": ex}
                                                       for t, ex in sorted(tree[s][g].items())]}
                                   for g in sorted(tree[s])]} for s in order]


def save_tree(tree, path=TREE):
    lines = ["# Дерево типов номенклатуры. Правьте свободно: # Раздел, ## Группа, - Тип  // примеры (необязательно)",
             "# После правки: taxonomy-classify --reset", ""]
    for s in tree:
        lines.append(f"# {s['name']}")
        for g in s.get("groups", []):
            lines.append(f"## {g['name']}")
            for t in g.get("types", []):
                ex = "; ".join(t.get("examples", [])[:3])
                lines.append(f"- {t['name']}" + (f"  // {ex}" if ex else ""))
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------- названия истории ----------
def history_names(con):
    """{нормализованное название: (пример как написано, сколько раз встречалось)}"""
    cnt, raw = Counter(), {}
    for (name,) in con.execute("SELECT name FROM positions WHERE name IS NOT NULL AND name != ''"):
        n = norm(name)
        if n:
            cnt[n] += 1
            raw.setdefault(n, name.strip())
    return {n: (raw[n], c) for n, c in cnt.items()}


# ---------- 1. предложить дерево (снизу вверх: от реальных «главных слов» истории) ----------
SECTIONS = [
    "Металлопрокат", "Метизы и крепёж", "Сварка", "Стройматериалы", "Бетон, растворы, сухие смеси",
    "Лакокрасочные материалы", "Электротехника и кабель", "Освещение", "Сантехника и трубопроводная арматура",
    "Ручной инструмент", "Электроинструмент и оборудование", "Расходники для инструмента (диски, свёрла, абразив)",
    "Спецодежда и СИЗ", "Химия, ГСМ, смазки", "Запчасти к технике и автотовары", "Подшипники, РТИ, приводы",
    "Оргтехника, ПК и комплектующие", "Связь и электроника", "Канцелярия", "Хозтовары и бытовая химия",
    "Мебель и бытовая техника", "Продукты и вода", "Услуги и работы", "Прочее",
]

PROPOSE_SYS = """Ты — эксперт по закупкам промышленно-строительного предприятия. Тебе дают «главные слова» из названий
позиций заявок на закупку, у каждого — примеры реальных названий. Для каждого слова определи место в дереве номенклатуры:
«Раздел › Группа › Тип».
Раздел — строго один из списка: {sections}.
Группа — более узкое направление внутри раздела (например «Крепёж резьбовой», «Лампы», «Комплектующие ПК»).
Тип — товары, которые заменяют друг друга или покупаются у одних и тех же поставщиков, независимо от марки, размера,
фасовки (например «Термопаста» — и КПТ-8, и DEEPCOOL Z3; «Болты»; «Электроды сварочные»; «Лампы накаливания»).
Группа и тип не должны совпадать: группа шире. Названия — по-русски, как в каталогах, во множественном числе.
Если по примерам слово означает разные товары (например «патрон» — для ламп и для перфоратора), выбери самое частое
значение по примерам.
Ответ — JSON: {{"items":[{{"i":<номер слова>,"path":"Раздел › Группа › Тип"}}]}} — для каждого слова."""


def propose(top=1500, chunk=120, workers=4):
    con = db.connect()
    names = history_names(con)
    if not names:
        raise SystemExit("История пуста — сначала sync")
    by_head = defaultdict(list)
    for n, (raw, c) in names.items():
        h = head(raw)
        if h:
            by_head[h].append((c, raw))
    weight = {h: sum(c for c, _ in v) for h, v in by_head.items()}
    heads = sorted(by_head, key=lambda h: -weight[h])[:top]
    total = sum(weight.values())
    covered = sum(weight[h] for h in heads)
    print(f"Названий: {len(names)}, «главных слов»: {len(by_head)}; беру {len(heads)} самых частых — "
          f"они покрывают {covered * 100 // max(total, 1)}% позиций. Пачек к модели: {len(heads) // chunk + 1}")
    sys_prompt = PROPOSE_SYS.format(sections="; ".join(SECTIONS))
    chunks = [heads[i:i + chunk] for i in range(0, len(heads), chunk)]

    def ask(hs):
        lines = []
        for i, h in enumerate(hs, 1):
            ex = "; ".join(r[:70] for _, r in sorted(by_head[h], reverse=True)[:3])
            lines.append(f"{i}. {h} — {ex}")
        res = chat_json(sys_prompt, "Слова:\n" + "\n".join(lines))
        out = {}
        for it in res.get("items") or []:
            try:
                i, path = int(it["i"]), str(it["path"])
            except (KeyError, TypeError, ValueError):
                continue
            parts = [x.strip() for x in path.split("›")]
            if 1 <= i <= len(hs) and len(parts) == 3 and all(parts):
                if parts[0] not in SECTIONS:
                    parts[0] = "Прочее"
                out[hs[i - 1]] = parts
        return out

    paths, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(ask, chunks):
            paths.update(got)
            done += 1
            print(f"\r  пачек: {done}/{len(chunks)}", end="", flush=True)
    print()
    # одинаковые типы/группы без учёта регистра — склеить
    canon, pairs = {}, []
    for h in heads:
        if h not in paths:
            continue
        sec, grp, typ = paths[h]
        grp = canon.setdefault((sec, grp.lower()), grp)
        typ = canon.setdefault((sec, grp, typ.lower()), typ)
        pairs.append((f"{sec} › {grp} › {typ}", [sorted(by_head[h], reverse=True)[0][1][:60]]))
    tree = build_tree(pairs)
    save_tree(tree)
    n_groups = sum(len(s["groups"]) for s in tree)
    n_types = sum(len(g["types"]) for s in tree for g in s["groups"])
    print(f"Черновик: {len(tree)} разделов, {n_groups} групп, {n_types} типов")
    consolidate(workers=workers)


# ---------- 1б. уборка дерева: синонимы, дубли, мусор, укрупнение групп ----------
CONSOLIDATE_SYS = """Ты наводишь порядок в разделе «{section}» дерева номенклатуры закупок промышленно-строительного предприятия.
Тебе дают типы раздела в виде «N. Группа › Тип // примеры названий». Для каждого выдай новый путь «Раздел › Группа › Тип»:
- объедини в один тип синонимы, опечатки, единственное/множественное число и слишком узкие варианты
  («Круг» и «Круги»; «Сопла» и «Сапла»; «Профтрубы», «Профильные трубы», «Профилированные трубы»);
- в разделе должно быть 5–25 групп; группа заметно шире типа (не «Болты › Болты», а «Крепёж резьбовой › Болты»);
- тип — товары, которые заменяют друг друга или берутся у одних поставщиков; не дроби по маркам и размерам;
- если тип явно из другого раздела — перенеси в подходящий из списка: {sections};
- мусор, тесты, обрывки слов, ссылки, «первая/вторая позиция» → «Прочее › Мусор › Мусор».
Названия — по-русски, во множественном числе, как в каталогах.
Ответ — JSON: {{"items":[{{"i":<номер>,"path":"Раздел › Группа › Тип"}}]}} — для каждого номера."""


def consolidate(workers=4, chunk=220):
    pairs = load_tree_examples()
    by_sec = defaultdict(list)
    for path, ex in pairs:
        by_sec[path.split(" › ")[0]].append((path, ex))
    jobs = []
    for sec, items in by_sec.items():
        for i in range(0, len(items), chunk):
            jobs.append((sec, items[i:i + chunk]))
    print(f"Уборка: типов {len(pairs)}, разделов {len(by_sec)}, запросов к модели: {len(jobs)}")

    def ask(job):
        sec, items = job
        sys_prompt = CONSOLIDATE_SYS.format(section=sec, sections="; ".join(SECTIONS))
        lines = [f"{i}. {p.split(' › ', 1)[1]}" + (f" // {'; '.join(ex[:2])}" if ex else "")
                 for i, (p, ex) in enumerate(items, 1)]
        res = chat_json(sys_prompt, "\n".join(lines))
        out = []
        mapped = {}
        for it in res.get("items") or []:
            try:
                i, path = int(it["i"]), str(it["path"])
            except (KeyError, TypeError, ValueError):
                continue
            parts = [x.strip() for x in path.split("›")]
            if 1 <= i <= len(items) and len(parts) == 3 and all(parts):
                if parts[0] not in SECTIONS:
                    parts[0] = sec
                mapped[i] = " › ".join(parts)
        for i, (p, ex) in enumerate(items, 1):
            out.append((mapped.get(i, p), ex))   # не ответила по типу — оставляем как было
        return out

    result, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(ask, jobs):
            result += got
            done += 1
            print(f"\r  запросов: {done}/{len(jobs)}", end="", flush=True)
    print()
    tree = build_tree(result)
    save_tree(tree)
    n_groups = sum(len(s["groups"]) for s in tree)
    n_types = sum(len(g["types"]) for s in tree for g in s["groups"])
    print(f"После уборки: {len(tree)} разделов, {n_groups} групп, {n_types} типов → {TREE}")


# ---------- 2. разложить названия по дереву ----------
CLASSIFY_SYS = """Ты раскладываешь товары из заявок на закупку по дереву номенклатуры.
Для каждого товара выбери номер самого подходящего типа из списка. Смотри на назначение товара, а не на совпадение слов
(«Патрон Е27» — патрон для ламп, «Патрон для перфоратора» — оснастка инструмента). Если не подходит ни один — 0.
Ответ — JSON: {"items":[{"i":<номер товара>,"t":<номер типа>}]} — для каждого товара из списка.

Типы:
"""


def _classify_batch(leaves, batch):
    sys_prompt = CLASSIFY_SYS + "\n".join(f"{i}. {p}" for i, p in enumerate(leaves, 1))
    user = "Товары:\n" + "\n".join(f"{i}. {raw[:120]}" for i, (_, raw) in enumerate(batch, 1))
    res = chat_json(sys_prompt, user)
    out = {}
    for it in res.get("items") or []:
        try:
            i, t = int(it["i"]), int(it["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= i <= len(batch):
            out[batch[i - 1][0]] = leaves[t - 1] if 1 <= t <= len(leaves) else OTHER
    return out


def classify_names(pairs, con=None, workers=1, size=80, progress=None):
    """pairs: [(нормализованное, как написано)] → {нормализованное: путь}; результат сохраняется в name_types."""
    con = con or db.connect()
    leaves = load_tree()
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    batches = [pairs[i:i + size] for i in range(0, len(pairs), size)]
    done, result = 0, {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(lambda b: _classify_batch(leaves, b), batches):
            now = datetime.now().isoformat(timespec="seconds")
            con.executemany("INSERT OR REPLACE INTO name_types VALUES(?,?,?,?)",
                            [(n, t, model, now) for n, t in got.items()])
            con.commit()
            result.update(got)
            done += 1
            if progress:
                progress(done, len(batches))
    return result


def classify(reset=False, limit=None, workers=4):
    con = db.connect()
    load_tree()   # проверить, что дерево есть
    if reset:
        con.execute("DELETE FROM name_types")
        con.commit()
    have = {r[0] for r in con.execute("SELECT name FROM name_types")}
    names = history_names(con)
    todo = sorted(((n, raw) for n, (raw, c) in names.items() if n not in have), key=lambda x: -names[x[0]][1])
    if limit:
        todo = todo[:limit]
    print(f"Названий всего: {len(names)}, уже разложено: {len(have)}, к разметке: {len(todo)} "
          f"(~{len(todo) // 80 + 1} запросов к модели)")
    if not todo:
        return
    classify_names(todo, con, workers=workers,
                   progress=lambda d, t: print(f"\r  пачек: {d}/{t}", end="", flush=True))
    print()
    top = con.execute("SELECT type_id, COUNT(*) c FROM name_types GROUP BY type_id ORDER BY c DESC LIMIT 15").fetchall()
    print("Самые наполненные типы:")
    for t, c in top:
        print(f"  {c:6}  {t}")
    other = con.execute("SELECT COUNT(*) FROM name_types WHERE type_id=?", (OTHER,)).fetchone()[0]
    print(f"Не подошло ни к одному типу: {other} — если много, добавьте типы в дерево и запустите с --reset")


if __name__ == "__main__":
    classify(reset="--reset" in sys.argv)
