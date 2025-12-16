"""Weather skill handlers for the runtime reference implementation.

TODO: в будущей итерации перевести работу с состоянием погоды на ctx,
а Yjs использовать только на уровне сценариев / runtime webspace.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from typing import Dict, Optional, Tuple, Any
from pathlib import Path

import requests
import re
from datetime import datetime, timezone
import yaml

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.bus import emit
from adaos.sdk.data.context import get_current_skill, set_current_skill
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.i18n import _, I18n
from adaos.sdk.data.skill_memory import get as memory_get, set as memory_set
from adaos.sdk.data.events import publish as publish_event
from adaos.services.agent_context import get_ctx

_log = logging.getLogger("skills.weather_skill")
REQUIRES_DATA_PROJECTIONS = [
    {"scope": "subnet", "slot": "weather.snapshot"},
]

DEFAULT_API_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_PLACE_RE = re.compile(r"(?:\bв|\bпо)\s+([A-Za-zА-Яа-яЁё\-]+)")
_CANON_RE = re.compile(r"^[A-Za-z][A-Za-z\-\s]+,\s*[A-Za-z]{2}$")
_CITY_CACHE: Dict[str, Tuple[float, Dict]] = {}
_CITY_CACHE_TTL = 300.0  # seconds


def _output(message: str) -> None:
    print(message)


def _load_skill_data_projections(ctx) -> None:
    """
    Load skill-level data_projections from skill.yaml into ProjectionRegistry.

    This gives weather_skill a default view on where weather.snapshot should
    be stored. If a scenario has already configured projections for this slot,
    its data_projections remain authoritative and skill-level defaults are
    skipped.
    """
    try:
        try:
            existing = ctx.projections.resolve("subnet", "weather.snapshot")
        except Exception:
            existing = []
        if existing:
            _log.debug(
                "weather_skill: projections already configured for subnet/weather.snapshot; skipping skill defaults"
            )
            return

        skills_root = ctx.paths.skills_workspace_dir()
        skills_root = skills_root() if callable(skills_root) else skills_root
        manifest_path = Path(skills_root) / "weather_skill" / "skill.yaml"
        if not manifest_path.exists():
            _log.warning(
                "weather_skill: skill.yaml not found when loading data_projections (path=%s)",
                manifest_path,
            )
            return
        spec = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        entries = spec.get("data_projections") or []
        if not isinstance(entries, list) or not entries:
            _log.warning(
                "weather_skill: skill.yaml has no data_projections; weather.snapshot projections may be misconfigured"
            )
            return
        ctx.projections.load_entries(entries)
        _log.debug("weather_skill: loaded %d skill-level data_projections", len(entries))
    except Exception:
        _log.debug("weather_skill: failed to load skill data_projections", exc_info=True)


def _normalize_city_token(raw: Any | None) -> Optional[str]:
    """
    Accept either a plain string or a small mapping like {"city": "Moscow"}
    and return a normalized city name, or None if it cannot be resolved.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        token = raw.get("city") or raw.get("name") or raw.get("value")
        if not token:
            return None
        return str(token).strip()
    text = str(raw).strip()
    return text or None


def _load_config() -> Tuple[str, Optional[str]]:
    """Load runtime configuration from the SDK stores."""

    api_entry_point = DEFAULT_API_ENDPOINT
    default_city = "Moscow"

    # Legacy support: migrate values from the local prep cache if present.
    try:
        skill = get_current_skill()
        if skill:
            prep_file = skill.path / "prep" / "prep_result.json"
            if prep_file.exists():
                data = json.loads(prep_file.read_text(encoding="utf-8"))
                resources = data.get("resources") or {}
                if not default_city and resources.get("default_city"):
                    default_city = resources["default_city"]
                    memory_set("default_city", default_city)
                if resources.get("api_entry_point"):
                    api_entry_point = resources["api_entry_point"]
                    memory_set("api_entry_point", api_entry_point)
    except Exception:
        # Prep artefacts are optional; swallow errors to keep runtime resilient.
        pass

    return api_entry_point, default_city


def _resolve_city(requested_city: Optional[str]) -> Optional[str]:
    raw = requested_city or memory_get("last_city") or memory_get("default_city")
    city = _normalize_city_token(raw)
    if city:
        memory_set("last_city", city)
    return city


def _fetch_weather(api_entry_point: str, city: str) -> Tuple[bool, Dict]:
    # Map known cities to coordinates for Open-Meteo API.
    city_key = _normalize_city_token(city)
    if not city_key:
        return False, {"error": _("runtime.weather.errors.missing_city")}
    cache_key = city_key.lower()
    now = time.time()
    cached = _CITY_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CITY_CACHE_TTL:
        return True, dict(cached[1])
    CITY_COORDS = {
        "Berlin": (52.52, 13.405),
        "Moscow": (55.75, 37.62),
        "New York": (40.7128, -74.0060),
        "Tokyo": (35.6895, 139.6917),
        "Paris": (48.8566, 2.3522),
    }
    coords = CITY_COORDS.get(city_key) or next(
        (v for k, v in CITY_COORDS.items() if k.lower() == city_key.lower()),
        None,
    )
    if not coords:
        return False, {"error": _("runtime.weather.errors.missing_city")}

    lat, lon = coords
    try:
        response = requests.get(
            api_entry_point.rstrip("/"),
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,wind_speed_10m",
                "timezone": "auto",
                "windspeed_unit": "ms",
            },
            timeout=6,
        )
    except Exception as exc:  # pragma: no cover - network error surface only
        return False, {"error": _("runtime.weather.errors.request", reason=str(exc))}

    if response.status_code != 200:
        return False, {"error": _("runtime.weather.errors.status", status=response.status_code)}

    try:
        payload = response.json()
    except Exception:
        return False, {"error": _("runtime.weather.errors.invalid_json")}

    current = payload.get("current") or {}
    temp = current.get("temperature_2m")
    wind_ms_value = current.get("wind_speed_10m")

    if temp is None:
        return False, {"error": _("runtime.weather.errors.invalid_response")}

    try:
        temp_value = float(temp)
    except Exception:
        temp_value = None

    if temp_value is None:
        return False, {"error": _("runtime.weather.errors.invalid_response")}

    try:
        wind_ms = float(wind_ms_value) if wind_ms_value is not None else 0.0
    except Exception:
        wind_ms = 0.0

    # description оставляем пустой – в on_weather_city_changed при пустой строке
    # возьмётся условие из CITY_SNAPSHOTS.
    data = {
        "city": city,
        "temp": temp_value,
        "temp_c": temp_value,
        "description": "",
        "wind_ms": wind_ms,
    }
    _CITY_CACHE[cache_key] = (now, dict(data))
    return True, data


async def _fetch_weather_async(api_entry_point: str, city: str) -> Tuple[bool, Dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(_fetch_weather, api_entry_point, city))


def handle(topic: str, payload: dict) -> None:
    """Local development entrypoint for the skill."""

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
def get_weather(city: Optional[str] = None) -> Dict:
    api_entry_point, default_city = _load_config()

    target_city = city or default_city or memory_get("last_city")
    if not target_city:
        return {"ok": False, "error": _("runtime.weather.errors.missing_city")}

    ok, data = _fetch_weather(api_entry_point, target_city)
    if not ok:
        return {"ok": False, **data}

    # Emit ui.notify for router (stdout routing) in tools/call path as well
    try:
        _city = data.get("city")
        if isinstance(_city, dict):
            _city = _city.get("city") or _city.get("name") or str(_city)
        text = _(
            "prep.weather.success",
            city=_city,
            temp=data.get("temp") or data.get("temp_c"),
            description=data.get("description"),
        )
        publish_event("ui.notify", {"text": text}, source="weather_skill")
    except Exception:
        pass

    return {"ok": True, **data}


@subscribe("nlp.intent.weather.get")
async def on_weather_intent(evt) -> None:
    api_entry_point, default_city = _load_config()

    city = _resolve_city((evt.payload or {}).get("city")) or default_city
    if not city:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.api_error", city="")},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )
        return

    ok, data = await _fetch_weather_async(api_entry_point, city)
    if not ok:
        await emit(
            "ui.notify",
            {"text": _("prep.weather.api_error", city=city)},
            actor=evt.actor,
            source="weather_skill",
            trace_id=evt.trace_id,
        )
        return

    await emit(
        "ui.notify",
        {
            "text": _(
                "prep.weather.success",
                city=data["city"],
                temp=data.get("temp") or data.get("temp_c"),
                description=data["description"],
            )
        },
        actor=evt.actor,
        source="weather_skill",
        trace_id=evt.trace_id,
    )


def resolve_location(*, text: str, lang: str = "ru", slots: Dict[str, Any] | None = None, resources: Dict[str, Any] | None = None) -> Optional[Tuple[str, float]]:
    token = (slots or {}).get("place_raw")

    if not token:
        m = _PLACE_RE.search(text or "")
        token = m.group(1) if m else None

    if not token:
        return None

    token = str(token).strip().rstrip("?.!,;")

    # 1) Если уже "City, CC" — принимаем как есть
    if _CANON_RE.match(token):
        return (token, 0.95)

    # 2) Иначе — пробуем карту синонимов из ресурсов
    mapping = (resources or {}).get("location_map") or {}
    canon = mapping.get(token.lower())
    if canon:
        return (canon, 0.9)

    # 3) Мягкий фолбэк: если токен латиницей без кода страны — можно принять как есть
    #    (или верни None, если хочешь требовать только "City, CC")
    if re.fullmatch(r"[A-Za-z][A-Za-z\-\s]+", token):
        return (token, 0.6)

    return None


__all__ = [*__all__, "resolve_location"] if "__all__" in globals() else ["resolve_location"]


@subscribe("weather.city_changed")
async def on_weather_city_changed(evt) -> None:
    """
    Handle city changes by fetching a fresh snapshot and projecting it
    via ctx.subnet into configured targets (Yjs / storage).
    """
    set_current_skill("weather_skill")
    try:
        ctx = get_ctx()
        _load_skill_data_projections(ctx)
    except Exception:
        # Best-effort: missing projections will be logged by the loader or ctx.subnet.set
        ctx = None  # type: ignore[assignment]
    payload = getattr(evt, "payload", None) if hasattr(evt, "payload") else evt
    if not isinstance(payload, dict):
        return
    meta = payload.get("_meta") if isinstance(payload, dict) else None
    raw_ws = (
        (payload.get("webspace_id") or payload.get("workspace_id"))
        or (meta or {}).get("webspace_id")
        or (meta or {}).get("workspace_id")
        or None
    )
    webspace_id: Optional[str] = None
    if isinstance(raw_ws, str) and raw_ws.strip():
        webspace_id = raw_ws.strip()
    city = payload.get("city")
    if not city:
        return
    api_entry_point, _ = _load_config()
    ok, live = await _fetch_weather_async(api_entry_point, city)
    snapshot = CITY_SNAPSHOTS.get(str(city), CITY_SNAPSHOTS["Berlin"])
    temp_value = (live.get("temp") or live.get("temp_c")) if ok else None
    condition_value = live.get("description") if ok else ""
    wind_value = live.get("wind_ms") if ok else None
    data = {
        "city": city,
        "temp_c": temp_value if temp_value is not None else snapshot["temp_c"],
        "condition": condition_value or snapshot["condition"],
        "wind_ms": wind_value if wind_value is not None else snapshot["wind_ms"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _log.info(
        "weather_city_changed webspace=%s city=%s ok=%s temp_c=%s source=%s",
        webspace_id or "default",
        city,
        ok,
        data.get("temp_c"),
        "api" if ok else "snapshot",
    )
    # Route snapshot via ProjectionRegistry: subnet/weather.snapshot
    try:
        # Wrap into {"current": ...} so that data.weather.current is
        # available for widgets reading data/weather/current.
        ctx_subnet.set("weather.snapshot", {"current": data}, webspace_id=webspace_id)
    except Exception:
        _log.warning("failed to project weather.snapshot via ctx_subnet", exc_info=True)


CITY_SNAPSHOTS = {
    "Berlin": {"temp_c": 7.5, "condition": "cloudy", "wind_ms": 3.1},
    "Moscow": {"temp_c": -3.0, "condition": "snow", "wind_ms": 5.2},
    "New York": {"temp_c": 12.4, "condition": "sunny", "wind_ms": 2.8},
    "Tokyo": {"temp_c": 18.0, "condition": "clear", "wind_ms": 1.9},
    "Paris": {"temp_c": 10.6, "condition": "rain", "wind_ms": 4.5},
}
