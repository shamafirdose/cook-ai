"""Free dish photos from Pexels. Each dish is looked up once and cached in memory,
so photos cost no AI quota and very few Pexels calls (free tier: 200 per hour).
Without PEXELS_API_KEY the app simply shows no photos.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

import requests

from logic import clean_name, get_secret, normalize_key

_TTL_FOUND = 7 * 24 * 3600   # keep a found photo for a week
_TTL_MISS = 600              # retry a failed lookup after 10 minutes
_MAX_CACHE = 2000

_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def photos_configured() -> bool:
    return bool(get_secret("PEXELS_API_KEY"))


def _lookup(name: str, api_key: str) -> dict | None:
    try:
        response = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": f"{name} dish food", "per_page": 1, "orientation": "landscape"},
            headers={"Authorization": api_key},
            timeout=5,
        )
        response.raise_for_status()
        found = response.json().get("photos") or []
    except Exception:
        return None
    if not found:
        return None
    src = found[0].get("src") or {}
    url = src.get("landscape") or src.get("medium")
    return {"url": url} if url else None


def get_photo(name: str, api_key: str | None = None) -> dict | None:
    """Return {'url': ...} for a dish, or None when there is no photo."""
    key = normalize_key(name)
    api_key = api_key or get_secret("PEXELS_API_KEY")
    if not key or not api_key:
        return None
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    result = _lookup(clean_name(name), api_key)
    with _lock:
        if len(_cache) >= _MAX_CACHE:
            _cache.clear()
        _cache[key] = (now + (_TTL_FOUND if result else _TTL_MISS), result)
    return result


def prefetch(names: Iterable[str]) -> None:
    """Look up several dishes at the same time so the cards appear without waiting one by one."""
    api_key = get_secret("PEXELS_API_KEY")
    unique = list(dict.fromkeys(n for n in names if n))
    if not api_key or not unique:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: get_photo(n, api_key), unique))
