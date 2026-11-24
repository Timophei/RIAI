"""Единицы и фасовка: приводим цену к базовой единице (кг, л, м), чтобы сравнивать «за кг» с «за мешок 50 кг»."""
import re

# единица позиции -> (вид, множитель к базовой)
UNITS = [
    (r"^(кг|килограмм\w*|kg)$", ("kg", 1.0)),
    (r"^(т|тн|тонн\w*)$", ("kg", 1000.0)),
    (r"^(г|гр|грамм\w*)$", ("kg", 0.001)),
    (r"^(л|литр\w*)$", ("l", 1.0)),
    (r"^(м3|куб\.?\s*м|м\.?\s*куб|кубометр\w*)$", ("m3", 1.0)),
    (r"^(м2|кв\.?\s*м|м\.?\s*кв)$", ("m2", 1.0)),
    (r"^(м|метр\w*|пог\.?\s*м|п\.?\s*м|м\.?\s*п)$", ("m", 1.0)),
]
PACKS = r"^(шт|штук\w*|меш\w*|упак\w*|уп|пач\w*|рул\w*|короб\w*|кор|поддон\w*|бух\w*|бут\w*|банк\w*|канистр\w*|компл\w*|набор\w*)$"

# фасовка в названии: «50 кг», «20кг», «20 литров». Тонны — только из единицы (в «Строп 10т» это грузоподъёмность).
# «г» и «л» сами по себе не берём: в «Бумага 80г 500л» это плотность и листы.
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|kg|литр\w*)(?![а-яa-z])", re.I)
SIZE_UNIT = {"кг": ("kg", 1.0), "kg": ("kg", 1.0), "т": ("kg", 1000.0), "тн": ("kg", 1000.0)}
BASE_LABEL = {"kg": "кг", "l": "л", "m": "м", "m2": "м²", "m3": "м³"}


def _clean(u):
    return re.sub(r"\s+", " ", (u or "").strip().lower().rstrip("."))


def unit_kind(unit):
    u = _clean(unit)
    for rx, kind in UNITS:
        if re.match(rx, u):
            return kind
    if re.match(PACKS, u):
        return ("pack", 1.0)
    return (None, 1.0)


def pack_size(name):
    """Сколько базовых единиц в одной штуке/мешке по названию: «Соль 50 кг» -> ('kg', 50)."""
    m = SIZE_RE.search(name or "")
    if not m:
        return None
    u = m.group(2).lower()
    kind, mult = ("l", 1.0) if u.startswith("литр") else SIZE_UNIT[u]
    val = float(m.group(1).replace(",", "."))
    return (kind, val * mult) if val > 0 else None


def to_float(v):
    try:
        return float(str(v).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def basis(name, unit, weight=None):
    """Во что пересчитывается 1 единица позиции: ('kg', 50.0) — одна ед. = 50 кг; ('pack:меш', 1) — неизвестно."""
    kind, mult = unit_kind(unit)
    if kind and kind != "pack":
        return kind, mult
    ps = pack_size(name)
    if ps:
        return ps
    w = to_float(weight)
    if w and w > 0:
        return "kg", w
    return (f"pack:{_clean(unit) or '?'}", 1.0)


def unit_price(price, name, unit, weight=None):
    """(цена за базовую единицу, базовая единица) или (цена, своя единица), если пересчитать нельзя."""
    if not price:
        return None, None
    kind, factor = basis(name, unit, weight)
    return price / factor, kind


def label(kind):
    if not kind:
        return ""
    if kind.startswith("pack:"):
        return kind[5:]
    return BASE_LABEL.get(kind, kind)
