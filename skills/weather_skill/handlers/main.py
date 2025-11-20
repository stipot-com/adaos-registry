"""Weather skill handlers for the runtime reference implementation."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Dict, Optional, Tuple, Any

import requests
import re
from datetime import datetime, timezone

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.bus import emit
from adaos.sdk.data.context import get_current_skill, set_current_skill
from adaos.sdk.data.i18n import _, I18n
from adaos.sdk.data.skill_memory import get as memory_get, set as memory_set
from adaos.sdk.data.events import publish as publish_event
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit_sync
from adaos.services.yjs.doc import async_get_ydoc, mutate_live_room

_log = logging.getLogger("skills.weather_skill")

DEFAULT_API_ENDPOINT = "https://wttr.in"
_PLACE_RE = re.compile(r"(?:\bв|\bпо)\s+([A-Za-zА-Яа-яЁё\-]+)")
_CANON_RE = re.compile(r"^[A-Za-z][A-Za-z\-\s]+,\s*[A-Za-z]{2}$")


def _output(message: str) -> None:
    print(message)


def _load_config() -> Tuple[str, Optional[str]]:
    """Load runtime configuration from the SDK stores."""

    api_entry_point = memory_get("api_entry_point") or DEFAULT_API_ENDPOINT
    default_city = memory_get("default_city")

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
    city = requested_city or memory_get("last_city") or memory_get("default_city")
    if city:
        memory_set("last_city", city)
    return city


def _fetch_weather(api_entry_point: str, city: str) -> Tuple[bool, Dict]:
    try:
        response = requests.get(
            f"{api_entry_point.rstrip('/')}/{city}",
            params={"format": "j1"},
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

    current = (payload.get("current_condition") or [{}])[0]
    temp = current.get("temp_C")
    description = (current.get("weatherDesc") or [{}])[0].get("value", "")
    try:
        wind_kmph = float(current.get("windspeedKmph") or 0.0)
    except Exception:
        wind_kmph = 0.0
    wind_ms = wind_kmph / 3.6
    if temp is None:
        return False, {"error": _("runtime.weather.errors.invalid_response")}

    try:
        temp_value = float(temp)
    except Exception:
        temp_value = None

    if temp_value is None:
        return False, {"error": _("runtime.weather.errors.invalid_response")}

    return True, {
        "city": city,
        "temp": temp_value,
        "temp_c": temp_value,
        "description": description,
        "wind_ms": wind_ms,
    }


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
    if ok:
        text = f"Погода: {data['city']}: {data['temp']}°C, {data['description']}"
        bus_emit_sync(get_ctx().bus, "ui.notify", {"text": text}, "weather_skill")

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
    payload = getattr(evt, "payload", None) if hasattr(evt, "payload") else evt
    if not isinstance(payload, dict):
        return
    meta = payload.get("_meta") if isinstance(payload, dict) else None
    webspace_id = str((meta or {}).get("webspace_id") or payload.get("webspace_id") or payload.get("workspace_id") or "default")
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
    _log.info("weather_city_changed webspace=%s city=%s payload=%s ok=%s", webspace_id, city, payload, ok)
    live_applied = mutate_live_room(webspace_id, lambda doc, txn: _write_weather_snapshot(doc, txn, data))
    if not live_applied:
        _log.info("mutate_live_room skipped (room inactive) webspace=%s", webspace_id)

    async with async_get_ydoc(webspace_id) as ydoc:
        with ydoc.begin_transaction() as txn:
            _write_weather_snapshot(ydoc, txn, data)
    _log.info("weather snapshot persisted webspace=%s city=%s", webspace_id, city)
    try:
        bus_emit_sync(
            get_ctx().bus,
            "weather.snapshot.updated",
            {
                "webspace_id": webspace_id,
                "city": city,
                "source": "api" if ok else "snapshot",
                "ok": ok,
                "temp_c": data.get("temp_c"),
                "condition": data.get("condition"),
                "wind_ms": data.get("wind_ms"),
                "updated_at": data.get("updated_at"),
            },
            "weather_skill",
        )
    except Exception:
        _log.warning("failed to emit weather.snapshot.updated webspace=%s", webspace_id, exc_info=True)


def _write_weather_snapshot(ydoc, txn, data: dict) -> None:
    data_map = ydoc.get_map("data")
    current_weather = data_map.get("weather")
    next_weather = _coerce_weather_mapping(current_weather)
    next_weather["current"] = data
    data_map.set(txn, "weather", next_weather)


def _coerce_weather_mapping(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    try:
        return dict(value)
    except Exception:
        pass
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            json_value = to_json()
            if isinstance(json_value, dict):
                return dict(json_value)
        except Exception:
            pass
    return {}


CITY_SNAPSHOTS = {
    "Berlin": {"temp_c": 7.5, "condition": "cloudy", "wind_ms": 3.1},
    "Moscow": {"temp_c": -3.0, "condition": "snow", "wind_ms": 5.2},
    "New York": {"temp_c": 12.4, "condition": "sunny", "wind_ms": 2.8},
    "Tokyo": {"temp_c": 18.0, "condition": "clear", "wind_ms": 1.9},
    "Paris": {"temp_c": 10.6, "condition": "rain", "wind_ms": 4.5},
}
