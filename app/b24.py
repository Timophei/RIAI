"""Минимальный клиент REST Битрикс24 через входящий вебхук."""
import os
import time
from urllib.parse import quote

import requests


class B24Error(Exception):
    pass


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
                    raise B24Error(f"{method}: сеть: {e}")
                time.sleep(2 ** attempt)
                continue
            self.calls += 1
            try:
                data = r.json()
            except ValueError:
                raise B24Error(f"{method}: HTTP {r.status_code}, не JSON: {r.text[:300]}")
            err = data.get("error")
            if err in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT") or r.status_code == 503:
                time.sleep(2 ** attempt)
                continue
            if err:
                raise B24Error(f"{method}: {err}: {data.get('error_description')}")
            return data
        raise B24Error(f"{method}: превышено число попыток")

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

    def list_all(self, method, params, extract=lambda r: r, page=50, progress=None):
        """Все страницы списочного метода: первый вызов узнаёт total, остальные — пачками через batch."""
        first = self.call(method, dict(params, start=0))
        items = list(extract(first.get("result")) or [])
        total = int(first.get("total") or len(items))
        if progress:
            progress(len(items), total)
        starts = list(range(page, total, page))
        for i in range(0, len(starts), 50):
            cmds = {f"p{s}": (method, dict(params, start=s)) for s in starts[i:i + 50]}
            res = self.batch(cmds)
            for s in starts[i:i + 50]:
                items.extend(extract(res.get(f"p{s}")) or [])
            if progress:
                progress(len(items), total)
        return items
