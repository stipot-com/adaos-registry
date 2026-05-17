"""Weather skill handlers.

The UI owns only generic action and data binding mechanics. Weather-specific
state transitions, API calls and payload shape live here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
import yaml

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.bus import emit
from adaos.sdk.data.context import clear_current_skill, get_current_skill, set_current_skill
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.i18n import _
from adaos.sdk.data.skill_memory import get as memory_get, set as memory_set
from adaos.sdk.data.events import publish as publish_event
from adaos.services.agent_context import get_ctx

_log = logging.getLogger("skills.weather_skill")

REQUIRES_DATA_PROJECTIONS = [
    {"scope": "subnet", "slot": "weather.snapshot"},
]

DEFAULT_API_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
DEFAULT_GEOCODING_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
_LEGACY_API_ENDPOINT_HOSTS = ("api.openweathermap.org",)
_CITY_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_GEOCODE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CITY_CACHE_TTL = 300.0
_GEOCODE_CACHE_TTL = 24 * 60 * 60.0
_WEATHER_UNAVAILABLE_TEXT = "\u041d\u0435 \u0443\u0434\u0430\u0435\u0442\u0441\u044f \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0434\u0430\u043d\u043d\u044b\u0435 \u043e \u043f\u043e\u0433\u043e\u0434\u0435."

WEATHER_CURRENT_FIELDS = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "weather_code",
    "cloud_cover",
    "pressure_msl",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "is_day",
]
WEATHER_HOURLY_FIELDS = [
    "temperature_2m",
    "precipitation_probability",
    "weather_code",
]
WEATHER_DAILY_FIELDS = [
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "sunrise",
    "sunset",
    "precipitation_probability_max",
    "precipitation_sum",
    "wind_speed_10m_max",
]

CITY_COORDS: Dict[str, Dict[str, Any]] = {
    "Berlin": {"latitude": 52.52, "longitude": 13.405, "country": "DE", "timezone": "Europe/Berlin"},
    "Moscow": {"latitude": 55.75, "longitude": 37.62, "country": "RU", "timezone": "Europe/Moscow"},
    "New York": {"latitude": 40.7128, "longitude": -74.0060, "country": "US", "timezone": "America/New_York"},
    "Tokyo": {"latitude": 35.6895, "longitude": 139.6917, "country": "JP", "timezone": "Asia/Tokyo"},
    "Paris": {"latitude": 48.8566, "longitude": 2.3522, "country": "FR", "timezone": "Europe/Paris"},
}

DEFAULT_LOCATIONS = [
    {"id": "Moscow", "label": "Moscow"},
    {"id": "Berlin", "label": "Berlin"},
    {"id": "Paris", "label": "Paris"},
    {"id": "New York", "label": "New York"},
    {"id": "Tokyo", "label": "Tokyo"},
]

CONDITIONS: Dict[int, Tuple[str, str]] = {
    0: ("Clear sky", "sunny-outline"),
    1: ("Mainly clear", "partly-sunny-outline"),
    2: ("Partly cloudy", "partly-sunny-outline"),
    3: ("Overcast", "cloud-outline"),
    45: ("Fog", "cloud-outline"),
    48: ("Depositing rime fog", "cloud-outline"),
    51: ("Light drizzle", "rainy-outline"),
    53: ("Drizzle", "rainy-outline"),
    55: ("Dense drizzle", "rainy-outline"),
    56: ("Freezing drizzle", "snow-outline"),
    57: ("Freezing drizzle", "snow-outline"),
    61: ("Light rain", "rainy-outline"),
    63: ("Rain", "rainy-outline"),
    65: ("Heavy rain", "thunderstorm-outline"),
    66: ("Freezing rain", "snow-outline"),
    67: ("Freezing rain", "snow-outline"),
    71: ("Light snow", "snow-outline"),
    73: ("Snow", "snow-outline"),
    75: ("Heavy snow", "snow-outline"),
    77: ("Snow grains", "snow-outline"),
    80: ("Light showers", "rainy-outline"),
    81: ("Showers", "rainy-outline"),
    82: ("Violent showers", "thunderstorm-outline"),
    85: ("Snow showers", "snow-outline"),
    86: ("Snow showers", "snow-outline"),
    95: ("Thunderstorm", "thunderstorm-outline"),
    96: ("Thunderstorm with hail", "thunderstorm-outline"),
    99: ("Thunderstorm with hail", "thunderstorm-outline"),
}

_RU_TRANSLIT = {
    ord("\u0430"): "a",
    ord("\u0431"): "b",
    ord("\u0432"): "v",
    ord("\u0433"): "g",
    ord("\u0434"): "d",
    ord("\u0435"): "e",
    ord("\u0451"): "e",
    ord("\u0436"): "zh",
    ord("\u0437"): "z",
    ord("\u0438"): "i",
    ord("\u0439"): "y",
    ord("\u043a"): "k",
    ord("\u043b"): "l",
    ord("\u043c"): "m",
    ord("\u043d"): "n",
    ord("\u043e"): "o",
    ord("\u043f"): "p",
    ord("\u0440"): "r",
    ord("\u0441"): "s",
    ord("\u0442"): "t",
    ord("\u0443"): "u",
    ord("\u0444"): "f",
    ord("\u0445"): "h",
    ord("\u0446"): "ts",
    ord("\u0447"): "ch",
    ord("\u0448"): "sh",
    ord("\u0449"): "sch",
    ord("\u044a"): "",
    ord("\u044b"): "y",
    ord("\u044c"): "",
    ord("\u044d"): "e",
    ord("\u044e"): "yu",
    ord("\u044f"): "ya",
}

_CITY_ALIASES_ASCII: Dict[str, str] = {
    "moskva": "Moscow",
    "moskve": "Moscow",
    "moskvu": "Moscow",
    "moskvy": "Moscow",
    "parizh": "Paris",
    "parizhe": "Paris",
    "parizha": "Paris",
    "berlin": "Berlin",
    "berline": "Berlin",
    "berlina": "Berlin",
    "tokio": "Tokyo",
    "nyu-york": "New York",
    "nyu york": "New York",
    "nyu-yorke": "New York",
    "nyu yorke": "New York",
}

_PLACE_RE = re.compile(r"(?:\bin|\b\u0432|\b\u043f\u043e)\s+([A-Za-z\u0410-\u042f\u0430-\u044f\u0401\u0451\-\s]+)", re.IGNORECASE)
_CANON_RE = re.compile(r"^[A-Za-z][A-Za-z\-\s]+,\s*[A-Za-z]{2}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output(message: str) -> None:
    print(message)


def _canonical_city_key(city: str) -> str:
    raw = str(city or "").strip()
    if not raw:
        return raw
    lowered = raw.lower()
    translit = lowered.translate(_RU_TRANSLIT)
    translit = re.sub(r"\s+", " ", translit.replace("_", " ").strip())
    if translit in _CITY_ALIASES_ASCII:
        return _CITY_ALIASES_ASCII[translit]
    for known in CITY_COORDS:
        if known.lower() == raw.lower():
            return known
    return raw


def _normalize_city_token(raw: Any | None) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        token = raw.get("city") or raw.get("name") or raw.get("label") or raw.get("value")
        return str(token).strip() if token else None
    text = str(raw).strip()
    return text or None


def _extract_city_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("city", "name", "label", "value"):
        city = _normalize_city_token(payload.get(key))
        if city and not city.startswith("$event"):
            return city
    for key in ("params", "event", "detail", "data", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            city = _extract_city_from_payload(nested)
            if city:
                return city
        else:
            city = _normalize_city_token(nested)
            if city and not city.startswith("$event"):
                return city
    return None


def _extract_request_id(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("request_id", "requestId"):
        token = str(payload.get(key) or "").strip()
        if token:
            return token
    nested = payload.get("payload")
    if isinstance(nested, dict):
        return _extract_request_id(nested)
    return ""


def _extract_location_from_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get("location"), payload]
    nested = payload.get("payload")
    if isinstance(nested, dict):
        candidates.insert(1, nested.get("location"))
        candidates.insert(2, nested)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        lat = candidate.get("latitude", candidate.get("lat"))
        lon = candidate.get("longitude", candidate.get("lon", candidate.get("lng")))
        try:
            latitude = float(lat)
            longitude = float(lon)
        except (TypeError, ValueError):
            continue
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            out = dict(candidate)
            out["latitude"] = latitude
            out["longitude"] = longitude
            return out
    return None


def _event_payload(evt: Any) -> Dict[str, Any]:
    payload = getattr(evt, "payload", None) if hasattr(evt, "payload") else evt
    return payload if isinstance(payload, dict) else {}


def _event_meta(evt: Any) -> Dict[str, Any]:
    payload = _event_payload(evt)
    meta = payload.get("_meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _normalize_api_entry_point(value: Any) -> str:
    endpoint = str(value or "").strip() or DEFAULT_API_ENDPOINT
    endpoint_l = endpoint.lower()
    if any(host in endpoint_l for host in _LEGACY_API_ENDPOINT_HOSTS):
        return DEFAULT_API_ENDPOINT
    return endpoint


def _load_skill_data_projections(ctx) -> None:
    try:
        try:
            existing = ctx.projections.resolve("subnet", "weather.snapshot")
        except Exception:
            existing = []
        if existing:
            return
        skills_root = ctx.paths.skills_workspace_dir()
        skills_root = skills_root() if callable(skills_root) else skills_root
        manifest_path = Path(skills_root) / "weather_skill" / "skill.yaml"
        if not manifest_path.exists():
            _log.warning("weather_skill: skill.yaml not found at %s", manifest_path)
            return
        spec = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        entries = spec.get("data_projections") or []
        if isinstance(entries, list) and entries:
            ctx.projections.load_entries(entries)
    except Exception:
        _log.debug("weather_skill: failed to load skill data_projections", exc_info=True)


def _load_config() -> Tuple[str, Optional[str]]:
    raw_api_entry_point = memory_get("api_entry_point")
    api_entry_point = _normalize_api_entry_point(raw_api_entry_point)
    if raw_api_entry_point and api_entry_point != str(raw_api_entry_point).strip():
        memory_set("api_entry_point", api_entry_point)
    default_city = _normalize_city_token(memory_get("default_city")) or "Moscow"

    try:
        skill = get_current_skill()
        if skill:
            prep_files = (skill.path / "prep" / "prep_result.json", skill.path / "prep_result.json")
            for prep_file in prep_files:
                if not prep_file.exists():
                    continue
                data = json.loads(prep_file.read_text(encoding="utf-8"))
                resources = data.get("resources") or {}
                if not memory_get("default_city") and resources.get("default_city"):
                    default_city = str(resources["default_city"]).strip()
                    memory_set("default_city", default_city)
                if not memory_get("api_entry_point") and resources.get("api_entry_point"):
                    api_entry_point = _normalize_api_entry_point(resources["api_entry_point"])
                    memory_set("api_entry_point", api_entry_point)
                break
    except Exception:
        pass

    return api_entry_point, default_city


def _resolve_city(requested_city: Optional[str]) -> Optional[str]:
    raw = requested_city or memory_get("last_city") or memory_get("default_city")
    city = _normalize_city_token(raw)
    if city:
        city = _canonical_city_key(city)
        memory_set("last_city", city)
    return city


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _condition(weather_code: Any, is_day: Any = None) -> Tuple[str, str]:
    code = _to_int(weather_code)
    text, icon = CONDITIONS.get(code if code is not None else -1, ("Weather", "cloud-outline"))
    if code == 0 and is_day is not None and _to_int(is_day) == 0:
        return ("Clear night", "moon-outline")
    return text, icon


def _known_location(city: str) -> Optional[Dict[str, Any]]:
    canonical = _canonical_city_key(city)
    data = CITY_COORDS.get(canonical)
    if not data:
        return None
    return {
        "city": canonical,
        "label": canonical,
        "latitude": float(data["latitude"]),
        "longitude": float(data["longitude"]),
        "country": data.get("country"),
        "timezone": data.get("timezone"),
        "source": "preset",
    }


def _geocode_city(city: str) -> Optional[Dict[str, Any]]:
    canonical = _canonical_city_key(city)
    known = _known_location(canonical)
    if known:
        return known
    cache_key = canonical.lower()
    now = time.time()
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached and now - cached[0] < _GEOCODE_CACHE_TTL:
        return dict(cached[1])
    try:
        response = requests.get(
            DEFAULT_GEOCODING_ENDPOINT,
            params={"name": canonical, "count": 1, "language": "en", "format": "json"},
            timeout=5,
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None
    item = results[0] if isinstance(results[0], dict) else {}
    lat = _to_float(item.get("latitude"))
    lon = _to_float(item.get("longitude"))
    if lat is None or lon is None:
        return None
    name = str(item.get("name") or canonical).strip() or canonical
    country = str(item.get("country_code") or item.get("country") or "").strip() or None
    label = f"{name}, {country}" if country and country.upper() not in name.upper() else name
    location = {
        "city": name,
        "label": label,
        "latitude": lat,
        "longitude": lon,
        "country": country,
        "timezone": item.get("timezone"),
        "source": "geocoding",
    }
    _GEOCODE_CACHE[cache_key] = (now, dict(location))
    return location


def _location_from_coords(location: Dict[str, Any], city: Optional[str] = None) -> Optional[Dict[str, Any]]:
    lat = _to_float(location.get("latitude", location.get("lat")))
    lon = _to_float(location.get("longitude", location.get("lon", location.get("lng"))))
    if lat is None or lon is None:
        return None
    label = str(location.get("label") or location.get("name") or city or "Current location").strip()
    return {
        "city": str(city or location.get("city") or label).strip() or label,
        "label": label,
        "latitude": lat,
        "longitude": lon,
        "accuracy": _to_float(location.get("accuracy")),
        "timezone": location.get("timezone"),
        "source": "browser",
    }


def _resolve_weather_location(*, city: Optional[str] = None, location: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if isinstance(location, dict):
        resolved = _location_from_coords(location, city)
        if resolved:
            return resolved
    target_city = _normalize_city_token(city)
    if target_city:
        return _geocode_city(target_city)
    return None


def _build_hourly_chart(payload: Dict[str, Any]) -> Dict[str, Any]:
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return {"title": "Next hours", "unit": "C", "points": []}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    temps = hourly.get("temperature_2m") if isinstance(hourly.get("temperature_2m"), list) else []
    precip = hourly.get("precipitation_probability") if isinstance(hourly.get("precipitation_probability"), list) else []
    codes = hourly.get("weather_code") if isinstance(hourly.get("weather_code"), list) else []
    points = []
    for index, raw_time in enumerate(times[:24]):
        temp = _to_float(temps[index] if index < len(temps) else None)
        if temp is None:
            continue
        label = str(raw_time or "")
        if "T" in label:
            label = label.split("T", 1)[1][:5]
        item: Dict[str, Any] = {"x": label, "y": temp}
        if index < len(precip):
            item["precip_pct"] = _to_int(precip[index])
        if index < len(codes):
            condition, icon = _condition(codes[index])
            item["condition"] = condition
            item["icon"] = icon
        points.append(item)
    return {"title": "Next hours", "unit": "C", "points": points}


def _build_daily(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    daily = payload.get("daily") if isinstance(payload, dict) else None
    if not isinstance(daily, dict):
        return []
    times = daily.get("time") if isinstance(daily.get("time"), list) else []
    rows: list[Dict[str, Any]] = []
    for index, raw_day in enumerate(times[:7]):
        code = _array_value(daily, "weather_code", index)
        condition, icon = _condition(code)
        rows.append(
            {
                "day": raw_day,
                "condition": condition,
                "icon": icon,
                "temp_min_c": _to_float(_array_value(daily, "temperature_2m_min", index)),
                "temp_max_c": _to_float(_array_value(daily, "temperature_2m_max", index)),
                "precip_pct": _to_int(_array_value(daily, "precipitation_probability_max", index)),
                "precip_mm": _to_float(_array_value(daily, "precipitation_sum", index)),
                "wind_ms": _to_float(_array_value(daily, "wind_speed_10m_max", index)),
                "sunrise": _array_value(daily, "sunrise", index),
                "sunset": _array_value(daily, "sunset", index),
            }
        )
    return rows


def _array_value(data: Dict[str, Any], key: str, index: int) -> Any:
    value = data.get(key)
    if not isinstance(value, list) or index >= len(value):
        return None
    return value[index]


def _weather_summary(current: Dict[str, Any]) -> str:
    parts = []
    feels = current.get("feels_like_c")
    humidity = current.get("humidity_pct")
    wind = current.get("wind_ms")
    if feels is not None:
        parts.append(f"Feels like {feels:g} C")
    if humidity is not None:
        parts.append(f"Humidity {humidity}%")
    if wind is not None:
        parts.append(f"Wind {wind:g} m/s")
    return " | ".join(parts)


def _fetch_weather_for_location(api_entry_point: str, location: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    lat = _to_float(location.get("latitude"))
    lon = _to_float(location.get("longitude"))
    if lat is None or lon is None:
        return False, {"error_code": "missing_location", "error": _("runtime.weather.errors.missing_city")}
    try:
        response = requests.get(
            _normalize_api_entry_point(api_entry_point).rstrip("/"),
            params={
                "latitude": lat,
                "longitude": lon,
                "current": ",".join(WEATHER_CURRENT_FIELDS),
                "hourly": ",".join(WEATHER_HOURLY_FIELDS),
                "daily": ",".join(WEATHER_DAILY_FIELDS),
                "forecast_days": 5,
                "timezone": location.get("timezone") or "auto",
                "wind_speed_unit": "ms",
            },
            timeout=7,
        )
    except Exception as exc:
        return False, {"error": _("runtime.weather.errors.request", reason=str(exc)), "location": location}

    if response.status_code != 200:
        return False, {"error": _("runtime.weather.errors.status", status=response.status_code), "location": location}

    try:
        payload = response.json()
    except Exception:
        return False, {"error": _("runtime.weather.errors.invalid_json"), "location": location}

    current_raw = payload.get("current") if isinstance(payload, dict) else None
    if not isinstance(current_raw, dict):
        return False, {"error": _("runtime.weather.errors.invalid_response"), "location": location}

    temp = _to_float(current_raw.get("temperature_2m"))
    if temp is None:
        return False, {"error": _("runtime.weather.errors.invalid_response"), "location": location}

    weather_code = _to_int(current_raw.get("weather_code"))
    condition, icon = _condition(weather_code, current_raw.get("is_day"))
    city = str(location.get("city") or location.get("label") or "Current location").strip() or "Current location"
    current = {
        "city": city,
        "label": str(location.get("label") or city),
        "location": {
            "latitude": lat,
            "longitude": lon,
            "accuracy": location.get("accuracy"),
            "country": location.get("country"),
            "source": location.get("source") or "api",
        },
        "temp_c": temp,
        "feels_like_c": _to_float(current_raw.get("apparent_temperature")),
        "humidity_pct": _to_int(current_raw.get("relative_humidity_2m")),
        "wind_ms": _to_float(current_raw.get("wind_speed_10m")),
        "wind_gust_ms": _to_float(current_raw.get("wind_gusts_10m")),
        "wind_direction_deg": _to_int(current_raw.get("wind_direction_10m")),
        "pressure_hpa": _to_float(current_raw.get("pressure_msl")),
        "surface_pressure_hpa": _to_float(current_raw.get("surface_pressure")),
        "cloud_cover_pct": _to_int(current_raw.get("cloud_cover")),
        "precipitation_mm": _to_float(current_raw.get("precipitation")),
        "rain_mm": _to_float(current_raw.get("rain")),
        "snowfall_cm": _to_float(current_raw.get("snowfall")),
        "weather_code": weather_code,
        "condition": condition,
        "description": condition,
        "condition_icon": icon,
        "is_day": bool(_to_int(current_raw.get("is_day"))),
        "updated_at": _now_iso(),
        "source": "api",
        "pending": False,
    }
    current["summary"] = _weather_summary(current)
    hourly_chart = _build_hourly_chart(payload)
    daily = _build_daily(payload)
    data = {
        "city": city,
        "temp": temp,
        "temp_c": temp,
        "description": condition,
        "condition": condition,
        "wind_ms": current.get("wind_ms") or 0.0,
        "current": current,
        "hourly_chart": hourly_chart,
        "daily": daily,
        "location": current["location"],
        "updated_at": current["updated_at"],
        "source": "api",
    }
    return True, data


def _weather_cache_key(city: Optional[str], location: Optional[Dict[str, Any]]) -> str:
    if isinstance(location, dict):
        lat = _to_float(location.get("latitude"))
        lon = _to_float(location.get("longitude"))
        if lat is not None and lon is not None:
            return f"coords:{lat:.4f},{lon:.4f}"
    return f"city:{_canonical_city_key(city or '').lower()}"


def _fetch_weather_by_request(
    api_entry_point: str,
    *,
    city: Optional[str] = None,
    location: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    resolved_location = _resolve_weather_location(city=city, location=location)
    if not resolved_location:
        return False, {"error_code": "missing_city", "error": _("runtime.weather.errors.missing_city"), "city": city}
    cache_key = _weather_cache_key(city or resolved_location.get("city"), resolved_location)
    now = time.time()
    cached = _CITY_CACHE.get(cache_key)
    if cached and now - cached[0] < _CITY_CACHE_TTL:
        return True, dict(cached[1])
    ok, data = _fetch_weather_for_location(api_entry_point, resolved_location)
    if ok:
        _CITY_CACHE[cache_key] = (now, dict(data))
    return ok, data


def _fetch_weather(api_entry_point: str, city: str) -> Tuple[bool, Dict[str, Any]]:
    return _fetch_weather_by_request(api_entry_point, city=city)


async def _fetch_weather_async(
    api_entry_point: str,
    city: Optional[str] = None,
    location: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    loop = asyncio.get_running_loop()

    def _run() -> Tuple[bool, Dict[str, Any]]:
        set_current_skill("weather_skill")
        try:
            return _fetch_weather_by_request(api_entry_point, city=city, location=location)
        finally:
            clear_current_skill()

    return await loop.run_in_executor(None, _run)


async def _emit_weather_failure(text: str, meta: Dict[str, Any], extra: Dict[str, Any]) -> None:
    await emit("ui.notify", {"text": text, "_meta": meta}, **extra)
    route_id = meta.get("route_id") or meta.get("route")
    if isinstance(route_id, str) and route_id.strip():
        return
    await emit("io.out.chat.append", {"text": text, "from": "hub", "ts": time.time(), "_meta": meta}, **extra)


def handle(topic: str, payload: dict) -> None:
    set_current_skill("weather_skill")
    api_entry_point, default_city = _load_config()
    city = _resolve_city((payload or {}).get("city")) or default_city
    if not city:
        _output(_("prep.weather.api_error", city=""))
        return
    ok, data = _fetch_weather(api_entry_point, city)
    if not ok:
        _output(_("prep.weather.api_error", city=city))
        return
    _output(_("prep.weather.success", city=data["city"], temp=data["temp"], description=data["description"]))


def handle_intent(intent: str, entities: dict) -> None:
    city = (entities or {}).get("city")
    handle(intent or "nlp.intent.weather.get", {"city": city} if city else {})


@tool("get_weather")
def get_weather(
    city: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    location: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Dict[str, Any]:
    silent = False
    if isinstance(city, dict):
        payload = city
        silent = bool(payload.get("silent"))
        location = _extract_location_from_payload(payload) or location
        city = _extract_city_from_payload(payload)
    if location is None and latitude is not None and longitude is not None:
        location = {"latitude": latitude, "longitude": longitude}

    api_entry_point, default_city = _load_config()
    target_city = city or (None if location else default_city or memory_get("last_city"))
    ok, data = _fetch_weather_by_request(api_entry_point, city=target_city, location=location)
    if not ok:
        return {"ok": False, **data}

    if not silent:
        try:
            publish_event(
                "ui.notify",
                {
                    "text": _(
                        "prep.weather.success",
                        city=data.get("city"),
                        temp=data.get("temp") or data.get("temp_c"),
                        description=data.get("description") or "",
                    )
                },
                source="weather_skill",
            )
        except Exception:
            pass

    return {"ok": True, **data}


@tool("get_snapshot")
def get_snapshot(
    _payload: Dict[str, Any] | None = None,
    webspace_id: str | None = None,
    target_node_id: str | None = None,
    node_id: str | None = None,
    city: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    payload = dict(_payload or {}) if isinstance(_payload, dict) else {}
    if city:
        payload["city"] = city
    target_city = _extract_city_from_payload(payload)
    location = _extract_location_from_payload(payload)
    result = get_weather({"city": target_city, "location": location, "silent": True})
    ok = bool(isinstance(result, dict) and result.get("ok"))
    if ok:
        current = dict(result.get("current") or {})
        snapshot = _weather_projection_payload(
            current,
            status="ok",
            extra={
                "hourly_chart": result.get("hourly_chart"),
                "daily": result.get("daily"),
            },
        )
    else:
        _, default_city = _load_config()
        current = _weather_current_payload(target_city or default_city or "Moscow", None, ok=False, error=str(result.get("error") or "weather_api_unavailable"))
        snapshot = _weather_projection_payload(current, status="error")
    return {
        "ok": ok,
        "weather": snapshot,
        "current": snapshot["current"],
        "webspace_id": webspace_id,
        "target_node_id": target_node_id or node_id or "",
    }


@subscribe("nlp.intent.weather.get")
async def on_weather_intent(payload) -> None:
    api_entry_point, default_city = _load_config()
    payload = payload if isinstance(payload, dict) else {}
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    extra = {"source": "weather_skill"}
    trace_id = meta.get("trace_id")
    if isinstance(trace_id, str) and trace_id:
        extra["trace_id"] = trace_id

    city = _resolve_city(payload.get("city")) or default_city
    if not city:
        await _emit_weather_failure(_WEATHER_UNAVAILABLE_TEXT, meta, extra)
        return

    ok, data = await _fetch_weather_async(api_entry_point, city)
    if not ok:
        await _emit_weather_failure(_WEATHER_UNAVAILABLE_TEXT, meta, extra)
        return

    text_out = _("prep.weather.success", city=data["city"], temp=data.get("temp") or data.get("temp_c"), description=data.get("description") or "")
    await emit("ui.notify", {"text": text_out, "_meta": meta}, **extra)
    route_id = meta.get("route_id") or meta.get("route")
    if isinstance(route_id, str) and route_id.strip():
        return
    await emit("io.out.chat.append", {"text": text_out, "from": "hub", "ts": time.time(), "_meta": meta}, **extra)


def resolve_location(
    *,
    text: str,
    lang: str = "ru",
    slots: Dict[str, Any] | None = None,
    resources: Dict[str, Any] | None = None,
) -> Optional[Tuple[str, float]]:
    token = (slots or {}).get("place_raw")
    if not token:
        match = _PLACE_RE.search(text or "")
        token = match.group(1) if match else None
    if not token:
        return None
    token = str(token).strip().rstrip("?.!,;")
    if _CANON_RE.match(token):
        return token, 0.95
    mapping = (resources or {}).get("location_map") or {}
    canon = mapping.get(token.lower())
    if canon:
        return canon, 0.9
    city = _canonical_city_key(token)
    if re.fullmatch(r"[A-Za-z][A-Za-z\-\s]+", city):
        return city, 0.6
    return None


_WEATHER_UPDATE_TASKS: Dict[str, asyncio.Task[None]] = {}


def _weather_current_payload(
    city: str,
    live: Dict[str, Any] | None = None,
    *,
    ok: bool = False,
    error: str = "",
    request_id: str = "",
    location: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    live = live or {}
    if ok and isinstance(live.get("current"), dict):
        current = dict(live["current"])
    else:
        current = {
            "city": str(live.get("city") or city or "Current location").strip(),
            "label": str(live.get("label") or live.get("city") or city or "").strip(),
            "temp_c": live.get("temp_c") if live.get("temp_c") is not None else live.get("temp"),
            "condition": live.get("condition") or live.get("description") or "",
            "description": live.get("description") or live.get("condition") or "",
            "wind_ms": live.get("wind_ms"),
            "updated_at": live.get("updated_at") or _now_iso(),
            "source": "api" if ok else "unavailable",
        }
    if location and "location" not in current:
        current["location"] = {
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "accuracy": location.get("accuracy"),
            "source": location.get("source") or "browser",
        }
    current.setdefault("updated_at", _now_iso())
    current["request_id"] = request_id
    current["pending"] = False
    current["source"] = "api" if ok else current.get("source") or "unavailable"
    if error:
        current["error"] = error
    return current


def _weather_pending_payload(city: str = "", *, request_id: str = "", location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    label = city or (str(location.get("label") or "Current location") if isinstance(location, dict) else "Current location")
    current = {
        "city": label,
        "label": label,
        "temp_c": None,
        "condition": "",
        "description": "",
        "wind_ms": None,
        "updated_at": _now_iso(),
        "request_id": request_id,
        "source": "pending",
        "pending": True,
    }
    if isinstance(location, dict):
        current["location"] = {
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "accuracy": location.get("accuracy"),
            "source": location.get("source") or "browser",
        }
    return current


def _weather_projection_payload(
    data: Dict[str, Any],
    *,
    status: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "current": data,
        "locations": list(DEFAULT_LOCATIONS),
        "updated_at": data.get("updated_at") or _now_iso(),
    }
    if status:
        payload["status"] = status
    if extra:
        for key in ("hourly_chart", "daily", "summary"):
            if extra.get(key) is not None:
                payload[key] = extra[key]
    payload.setdefault("hourly_chart", {"title": "Next hours", "unit": "C", "points": []})
    payload.setdefault("daily", [])
    return payload


async def _project_weather_snapshot_async(snapshot: Dict[str, Any], *, webspace_id: Optional[str]) -> None:
    try:
        set_current_skill("weather_skill")
        await ctx_subnet.set_async("weather.snapshot", snapshot, webspace_id=webspace_id)
    except Exception:
        _log.warning("failed to project weather.snapshot via ctx_subnet", exc_info=True)


async def _project_weather_current_async(data: Dict[str, Any], *, webspace_id: Optional[str], status: str = "") -> None:
    await _project_weather_snapshot_async(_weather_projection_payload(data, status=status), webspace_id=webspace_id)


def _project_weather_current(data: Dict[str, Any], *, webspace_id: Optional[str], status: str = "") -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            set_current_skill("weather_skill")
            ctx_subnet.set("weather.snapshot", _weather_projection_payload(data, status=status), webspace_id=webspace_id)
        except Exception:
            _log.warning("failed to project weather.snapshot via ctx_subnet", exc_info=True)
        return
    loop.create_task(_project_weather_current_async(data, webspace_id=webspace_id, status=status))


def _target_context(payload: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    target_node_id = str(
        payload.get("target_node_id")
        or (payload.get("_meta") or {}).get("target_node_id")
        or payload.get("node_id")
        or ""
    ).strip()
    try:
        local_node_id = str(getattr(get_ctx().config, "node_id", "") or "").strip()
    except Exception:
        local_node_id = ""
    if target_node_id and local_node_id and target_node_id != local_node_id:
        return False, target_node_id, None
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    raw_ws = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    webspace_id = str(raw_ws).strip() if raw_ws else None
    return True, target_node_id, webspace_id


async def _refresh_weather_live_snapshot(
    *,
    task_key: str,
    api_entry_point: str,
    city: Optional[str],
    location: Optional[Dict[str, Any]],
    webspace_id: Optional[str],
    request_id: str,
) -> None:
    try:
        ok, live = await _fetch_weather_async(api_entry_point, city, location)
        if _WEATHER_UPDATE_TASKS.get(task_key) is not asyncio.current_task():
            return
        error_text = "" if ok else str(live.get("error") or live.get("error_code") or "weather_api_unavailable")
        current = _weather_current_payload(
            city or str((location or {}).get("label") or "Current location"),
            live if ok else None,
            ok=ok,
            error=error_text,
            request_id=request_id,
            location=location,
        )
        snapshot = _weather_projection_payload(
            current,
            status="ok" if ok else "error",
            extra={
                "hourly_chart": live.get("hourly_chart") if ok else None,
                "daily": live.get("daily") if ok else None,
            },
        )
        _log.info(
            "weather live update webspace=%s city=%s ok=%s temp_c=%s",
            webspace_id or "default",
            current.get("city"),
            ok,
            current.get("temp_c"),
        )
        await _project_weather_snapshot_async(snapshot, webspace_id=webspace_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("weather live refresh failed city=%s webspace=%s", city, webspace_id or "default", exc_info=True)
    finally:
        if _WEATHER_UPDATE_TASKS.get(task_key) is asyncio.current_task():
            _WEATHER_UPDATE_TASKS.pop(task_key, None)


async def _handle_weather_request(evt: Any, *, event_name: str) -> None:
    set_current_skill("weather_skill")
    try:
        ctx = get_ctx()
        _load_skill_data_projections(ctx)
    except Exception:
        pass

    payload = _event_payload(evt)
    if not payload:
        return
    allowed, target_node_id, webspace_id = _target_context(payload)
    if not allowed:
        _log.info("weather request ignored: target_node_mismatch target_node_id=%s", target_node_id)
        return

    city = _extract_city_from_payload(payload)
    location = _extract_location_from_payload(payload)
    request_id = _extract_request_id(payload)
    api_entry_point, default_city = _load_config()
    if not city and not location:
        city = _resolve_city(None) or default_city
    if not city and not location:
        _log.info("weather request ignored: missing city/location payload_keys=%s", sorted(payload.keys()))
        return

    pending = _weather_pending_payload(city or "", request_id=request_id, location=location)
    _log.info(
        "%s accepted webspace=%s city=%s source=pending",
        event_name,
        webspace_id or "default",
        pending.get("city"),
    )
    await _project_weather_current_async(pending, webspace_id=webspace_id, status="refreshing")

    task_key = f"{str(webspace_id or 'default').strip() or 'default'}::{target_node_id or 'local'}"
    previous = _WEATHER_UPDATE_TASKS.get(task_key)
    if previous and not previous.done():
        previous.cancel()
    _WEATHER_UPDATE_TASKS[task_key] = asyncio.create_task(
        _refresh_weather_live_snapshot(
            task_key=task_key,
            api_entry_point=api_entry_point,
            city=city,
            location=location,
            webspace_id=webspace_id,
            request_id=request_id,
        )
    )


@subscribe("weather.location.requested")
async def on_weather_location_requested(evt) -> None:
    await _handle_weather_request(evt, event_name="weather.location.requested")


@subscribe("weather.city_changed")
async def on_weather_city_changed(evt) -> None:
    await _handle_weather_request(evt, event_name="weather.city_changed")


__all__ = ["resolve_location"]
