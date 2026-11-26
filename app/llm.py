"""Клиент модели через OpenAI-совместимый API (OpenAI; позже GigaChat/YandexGPT — сменой адреса и ключа в .env).

.env:
  LLM_API_KEY=...                     ключ
  LLM_BASE_URL=https://api.openai.com/v1   адрес API (для прокси/другого провайдера — свой)
  LLM_MODEL=gpt-4o-mini               недорогая модель для разметки
  LLM_PROXY=http://user:pass@host:port      необязательно: прокси, если провайдер недоступен напрямую
"""
import json
import os
import re
import time

import requests


class LLMError(Exception):
    pass


def configured():
    return bool(os.environ.get("LLM_API_KEY"))


def chat_json(system, user, model=None, temperature=0, retries=4, timeout=180):
    """Запрос к модели, ответ — JSON-объект. Повторяет при сетевых ошибках и лимитах."""
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    if not key:
        raise LLMError("Не задан LLM_API_KEY в .env")
    if not key.isascii() or " " in key:
        raise LLMError("LLM_API_KEY в .env похож на заглушку (русские буквы или пробелы) — впишите настоящий ключ")
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    proxy = os.environ.get("LLM_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    body = {"model": model, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{base}/chat/completions", json=body, timeout=timeout, proxies=proxies,
                              headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as e:
            last = f"сеть: {e}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(min(60, 3 * 2 ** attempt))
            continue
        if r.status_code == 400 and "temperature" in r.text and "temperature" in body:
            body.pop("temperature")       # «думающие» модели не принимают temperature — без неё
            continue
        if r.status_code == 400 and "response_format" in r.text and "response_format" in body:
            body.pop("response_format")   # провайдер не умеет JSON-режим — просим JSON словами
            continue
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        text = r.json()["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except ValueError:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                return json.loads(m.group(0))
            raise LLMError(f"Модель вернула не JSON: {text[:200]}")
    raise LLMError(f"Модель недоступна: {last}")


def check(model=None):
    r = chat_json("Ты отвечаешь строго JSON-объектом.",
                  'Верни {"ok": true, "type": "<тип товара>"} для товара «Термопаста STEEL КПТ-8 [КПТ-8_20]».',
                  model=model)
    print(f"Модель {model or os.environ.get('LLM_MODEL', 'gpt-4o-mini')} отвечает:", r)


def list_models():
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    proxy = os.environ.get("LLM_PROXY")
    r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30,
                     proxies={"http": proxy, "https": proxy} if proxy else None)
    ids = sorted(m["id"] for m in r.json().get("data", []))
    chat = [i for i in ids if re.match(r"^(gpt-|o\d|chatgpt)", i) and not re.search(
        r"audio|realtime|transcribe|tts|image|search|embedding|instruct", i)]
    print("Доступные модели для текста:\n  " + "\n  ".join(chat or ids))
