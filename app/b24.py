"""Минимальный клиент REST Битрикс24 через входящий вебхук."""
import os
import time
from urllib.parse import quote

import requests


class B24Error(Exception):
    pass


class B24Transient(B24Error):
    """Сервер не ответил нормально (пустой ответ, таймаут, 5xx) — можно повторить меньшей пачкой."""


def php_query(params, prefix=None):
    """Собирает строку запроса в стиле PHP http_build_query (для batch)."""
    parts = []
    if isinstance(params, dict):
        items = params.items()
    else:
        items = enumerate(params)
    for k, v in items:
        key = f"{prefix}[{k}]" if prefix is not None else str(k)
        if isinstance(v, (dict, list, tuple)):
            sub = php_query(v, key)
            if sub:
                parts.append(sub)
        else:
            if v is None:
                v = ""
            elif isinstance(v, bool):
                v = "Y" if v else "N"
            parts.append(f"{quote(key, safe='[]')}={quote(str(v), safe='')}")
    return "&".join(parts)


class B24:
    def __init__(self, webhook=None, verify=None):
        self.url = (webhook or os.environ.get("B24_WEBHOOK", "")).strip()
        if not self.url:
            raise B24Error("Не задан B24_WEBHOOK в .env")
        if not self.url.endswith("/"):
            self.url += "/"
        if verify is None:
            verify = os.environ.get("B24_VERIFY_SSL", "1") != "0"
        self.verify = verify
        self.s = requests.Session()
        self.calls = 0

    def call(self, method, params=None, retries=5):
        params = params or {}
        for attempt in range(retries):
            try:
                r = self.s.post(self.url + method + ".json", json=params,
                                timeout=120, verify=self.verify)
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise B24Transient(f"{method}: сеть: {e}")
                time.sleep(2 ** attempt)
                continue
            self.calls += 1
            try:
                data = r.json()
            except ValueError:
                # Пустой или обрезанный ответ — обычно сервер упёрся в лимит памяти/времени PHP
                if attempt < 1:
                    time.sleep(3)
                    continue
                raise B24Transient(f"{method}: HTTP {r.status_code}, не JSON: {r.text[:300]!r}")
            err = data.get("error")
            if err in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT") or r.status_code == 503:
                time.sleep(2 ** attempt)
                continue
            if err:
                raise B24Error(f"{method}: {err}: {data.get('error_description')}")
            return data
        raise B24Transient(f"{method}: превышено число попыток")

    def batch(self, cmds):
        """cmds: dict key -> (method, params). Не больше 50 за вызов."""
        out, errors = {}, {}
        keys = list(cmds)
        for i in range(0, len(keys), 50):
            chunk = {k: f"{cmds[k][0]}?{php_query(cmds[k][1])}" for k in keys[i:i + 50]}
            data = self.call("batch", {"halt": 0, "cmd": chunk})
            res = data.get("result", {})
            out.update(res.get("result") or {})
            errors.update(res.get("result_error") or {})
        if errors:
            k, v = next(iter(errors.items()))
            raise B24Error(f"batch: {len(errors)} ошибок, например {k}: {v}")
        return out

    def list_all(self, method, params, extract=lambda r: r, page=50, progress=None, pages_per_batch=None):
        """Все страницы списочного метода: первый вызов узнаёт total, остальные — пачками через batch.
        Если сервер не справляется с пачкой, она автоматически уменьшается вплоть до одиночных вызовов."""
        size = pages_per_batch or int(os.environ.get("B24_PAGES_PER_BATCH", "10"))
        first = self.call(method, dict(params, start=0))
        items = list(extract(first.get("result")) or [])
        total = int(first.get("total") or len(items))
        if progress:
            progress(len(items), total)
        starts = list(range(page, total, page))
        i = 0
        while i < len(starts):
            chunk = starts[i:i + size]
            try:
                if len(chunk) == 1:
                    res = {f"p{chunk[0]}": self.call(method, dict(params, start=chunk[0])).get("result")}
                else:
                    res = self.batch({f"p{s}": (method, dict(params, start=s)) for s in chunk})
            except B24Transient as e:
                if size == 1:
                    raise
                size = max(1, size // 2)
                print(f"\n  сервер не справился с пачкой ({e.__class__.__name__}), уменьшаю до {size} стр.")
                continue
            for s in chunk:
                items.extend(extract(res.get(f"p{s}")) or [])
            i += len(chunk)
            if progress:
                progress(len(items), total)
        return items

    def iter_pages(self, method, params, filter_param, id_field, extract=lambda r: r,
                   start_after=0, pause=None):
        """Постраничная выгрузка «окнами» по курсору ID (>ID): в памяти — только текущее окно,
        после каждого окна можно сохранить курсор и продолжить после сбоя.
        Выдаёт (страница_элементов, получено_всего, примерно_всего)."""
        size = int(os.environ.get("B24_PAGES_PER_BATCH", "10"))
        pause = float(os.environ.get("B24_PAUSE", "0.3")) if pause is None else pause
        id_key = id_field.upper() if filter_param == "FILTER" else id_field
        last, got = start_after, 0
        while True:
            p = dict(params)
            p[filter_param] = dict(params.get(filter_param) or {}, **{f">{id_key}": last})
            try:
                first = self.call(method, dict(p, start=0))
            except B24Transient:
                time.sleep(5)
                first = self.call(method, dict(p, start=0))
            page0 = list(extract(first.get("result")) or [])
            remaining = int(first.get("total") or len(page0))
            if not page0:
                return
            pages = [page0]
            starts = list(range(50, min(remaining, size * 50), 50))
            while starts:
                try:
                    if len(starts) == 1:
                        res = {f"p{starts[0]}": self.call(method, dict(p, start=starts[0])).get("result")}
                    else:
                        res = self.batch({f"p{s}": (method, dict(p, start=s)) for s in starts})
                except B24Transient as e:
                    if len(starts) == 1:
                        raise
                    size = max(2, size // 2) if size > 2 else 1
                    starts = starts[:max(1, size - 1)]
                    print(f"\n  портал не справился с пачкой, уменьшаю до {size} стр.", flush=True)
                    continue
                pages += [list(extract(res.get(f"p{s}")) or []) for s in starts]
                break
            for pg in pages:
                if pg:
                    got += len(pg)
                    last = max(last, max(int(x[id_field]) for x in pg))
                    yield pg, got, got + max(0, remaining - sum(len(q) for q in pages))
            if remaining <= sum(len(q) for q in pages):
                return
            if pause:
                time.sleep(pause)
