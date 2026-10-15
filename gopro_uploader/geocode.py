"""Reverse/forward geocoding via OpenStreetMap Nominatim (free, no API key).

Nominatim policy: max 1 request/second and a descriptive User-Agent.
Results are cached on disk so a folder is only looked up once.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from . import config

_UA = "gopro-yt-uploader/0.1 (personal desktop tool)"
_BASE = "https://nominatim.openstreetmap.org"
_lock = threading.Lock()
_last_call = 0.0
_cache: dict | None = None

# Only these OSM categories make a good place name; roads, houses etc. do not.
_POI_CATEGORIES = {"tourism", "amenity", "leisure", "historic", "railway", "aeroway",
                   "natural", "shop", "man_made", "waterway"}
_POI_KEYS = ("tourism", "attraction", "historic", "leisure", "amenity", "railway", "aeroway",
             "natural", "shop")
_LOCAL_KEYS = ("neighbourhood", "quarter", "hamlet")
_DISTRICT_KEYS = ("suburb", "city_district", "borough")
_CITY_KEYS = ("city", "town", "village", "municipality", "county")


def _cache_file():
    return config.path("geocode_cache.json")


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_cache_file(), encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache():
    with open(_cache_file(), "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False)


def _key(lat, lon, lang):
    return f"{lat:.4f},{lon:.4f},{lang}"


def _get(endpoint: str, params: dict):
    global _last_call
    with _lock:
        wait = 1.1 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        url = f"{_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        finally:
            _last_call = time.time()


def _first(addr, keys):
    return next((addr[k] for k in keys if addr.get(k)), "")


def format_place(result: dict) -> str:
    """e.g. '範例寺, 範例区, 範例市' or '範例一丁目, 範例区, 範例市'."""
    addr = result.get("address", {})
    poi = ""
    if result.get("category") in _POI_CATEGORIES and result.get("name"):
        poi = result["name"]
    poi = (poi or _first(addr, _POI_KEYS) or _first(addr, _LOCAL_KEYS)).split(";")[0]
    city = _first(addr, _CITY_KEYS)
    parts = [poi, _first(addr, _DISTRICT_KEYS), city]
    if not city:
        parts.append(addr.get("state") or addr.get("province") or "")
    out = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return ", ".join(out) or result.get("display_name", "")


def cached_place(lat: float, lon: float, lang: str) -> str | None:
    # raw results are cached so changing format_place never needs a re-query
    raw = _load_cache().get(_key(lat, lon, lang))
    return format_place(raw) if isinstance(raw, dict) else None


def reverse(lat: float, lon: float, lang: str) -> str:
    cache = _load_cache()
    k = _key(lat, lon, lang)
    if not isinstance(cache.get(k), dict):
        data = _get("reverse", {"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                                "zoom": 17, "addressdetails": 1, "accept-language": lang})
        cache[k] = {key: data.get(key) for key in ("name", "category", "address", "display_name")} \
            if "error" not in data else {}
        _save_cache()
    return format_place(cache[k]) if cache[k] else ""


def search(query: str, lang: str, near: tuple[float, float] | None = None,
           limit: int = 8) -> list[dict]:
    params = {"format": "jsonv2", "q": query, "limit": limit,
              "addressdetails": 1, "accept-language": lang}
    if near:  # prefer results around the current map area, but don't exclude others
        lat, lon = near
        params["viewbox"] = f"{lon - 1.5},{lat + 1.5},{lon + 1.5},{lat - 1.5}"
    data = _get("search", params)
    return [{"lat": float(r["lat"]), "lon": float(r["lon"]), "place": format_place(r),
             "full": r.get("display_name", "")} for r in data]
