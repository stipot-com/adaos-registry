from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Mapping
import logging

from adaos.sdk.core.decorators import tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.io.out import chat_append, say
from adaos.services.agent_context import get_ctx
from adaos.services.yjs.webspace import default_webspace_id
from adaos.skills.runtime_runner import execute_tool


_WEATHER_RE = re.compile(
    r"(?:какая\s+)?погода\w*\s+(?:в|во)\s+(.+)$",
    re.IGNORECASE | re.UNICODE,
)
_WEATHER_PREFIXES = (
    "какая погода в ",
    "какая погода во ",
    "погода в ",
    "погода во ",
    "weather in ",
)

_CITY_ALIASES: dict[str, tuple[str, str]] = {
    # ru (cases) -> (weather_skill city key, display)
    "москва": ("Moscow", "Москва"),
    "москве": ("Moscow", "Москва"),
    "москву": ("Moscow", "Москва"),
    "москвы": ("Moscow", "Москва"),
    "берлин": ("Berlin", "Berlin"),
    "берлине": ("Berlin", "Berlin"),
    "берлина": ("Berlin", "Berlin"),
    "париж": ("Paris", "Paris"),
    "париже": ("Paris", "Paris"),
    "парижа": ("Paris", "Paris"),
    "токио": ("Tokyo", "Tokyo"),
    "нью-йорк": ("New York", "New York"),
    "нью йорк": ("New York", "New York"),
    "нью-йорке": ("New York", "New York"),
    "нью йорке": ("New York", "New York"),
}

_log = logging.getLogger("adaos.voice_chat_skill")
REQUIRES_DATA_PROJECTIONS = ["voice_chat.state"]
_DATA_PROJECTION_ENTRIES = [
    {
        "scope": "subnet",
        "slot": "voice_chat.state",
        "targets": [
            {
                "backend": "yjs",
                "path": "data/voice_chat",
            },
        ],
    },
]
_MAX_MESSAGES = 80
_STATE_BY_KEY: dict[str, dict[str, Any]] = {}


def _webspace_id_from_meta(meta: Mapping[str, Any] | None) -> str:
    if isinstance(meta, Mapping):
        token = str(meta.get("webspace_id") or meta.get("workspace_id") or "").strip()
        if token:
            return token
    return default_webspace_id()


def _state_key(webspace_id: str, target_node_id: str | None = None) -> str:
    return f"{str(webspace_id or default_webspace_id()).strip() or default_webspace_id()}\0{str(target_node_id or '').strip()}"


def _state_for(webspace_id: str, target_node_id: str | None = None) -> dict[str, Any]:
    key = _state_key(webspace_id, target_node_id)
    state = _STATE_BY_KEY.get(key)
    if not isinstance(state, dict):
        state = {"messages": [], "last_refresh_ts": time.time()}
        _STATE_BY_KEY[key] = state
    if not isinstance(state.get("messages"), list):
        state["messages"] = []
    return state


def _message(from_: str, text: str) -> dict[str, Any]:
    ts = time.time()
    return {
        "id": f"m.{int(ts * 1000)}.{from_}",
        "from": str(from_ or "hub").strip() or "hub",
        "text": str(text or ""),
        "ts": ts,
    }


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        if ctx.projections.resolve("subnet", "voice_chat.state"):
            return
        ctx.projections.load_entries(_DATA_PROJECTION_ENTRIES)
    except Exception:
        pass


def _project_state(webspace_id: str, target_node_id: str | None = None) -> None:
    _ensure_skill_data_projections()
    state = _state_for(webspace_id, target_node_id)
    payload = {
        "messages": list(state.get("messages") or [])[-_MAX_MESSAGES:],
        "last_refresh_ts": state.get("last_refresh_ts") or time.time(),
    }
    try:
        ctx_subnet.set("voice_chat.state", payload, webspace_id=webspace_id)
    except Exception as exc:
        _log.warning("voice_chat projection failed webspace=%s error=%s", webspace_id, exc)


def _append_projected_message(
    webspace_id: str,
    target_node_id: str | None,
    *,
    from_: str,
    text: str,
) -> None:
    state = _state_for(webspace_id, target_node_id)
    messages = list(state.get("messages") or [])
    messages.append(_message(from_, text))
    state["messages"] = messages[-_MAX_MESSAGES:]
    state["last_refresh_ts"] = time.time()
    _project_state(webspace_id, target_node_id)


def _append_reply(reply: str, *, webspace_id: str, target_node_id: str | None) -> None:
    chat_append(reply, from_="hub", _meta={"webspace_id": webspace_id, "target_node_id": target_node_id})
    _append_projected_message(webspace_id, target_node_id, from_="hub", text=reply)


def _normalize_city_key(text: str) -> str:
    return (
        str(text or "")
        .strip()
        .lower()
        .replace("ё", "е")
        .replace("‑", "-")
    )


def _canon_city_for_weather(raw_city: str) -> tuple[str, str]:
    """
    Return (city_for_weather_skill, city_for_display).

    `weather_skill` currently supports a small built-in catalog, so for common
    Russian city names we map them to the canonical keys used by that skill.
    """
    cleaned = str(raw_city or "").strip()
    if not cleaned:
        return ("", "")
    key = _normalize_city_key(cleaned)
    if key in _CITY_ALIASES:
        return _CITY_ALIASES[key]
    return (cleaned, cleaned)


def _extract_city(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _WEATHER_RE.search(raw)
    if not m:
        lowered = raw.lower()
        city_from_prefix = None
        for prefix in _WEATHER_PREFIXES:
            if lowered.startswith(prefix):
                city_from_prefix = raw[len(prefix) :]
                break
        if city_from_prefix is None:
            return None
        city = city_from_prefix.strip().strip("?.!,;:()[]{}\"'")
    else:
        city = m.group(1).strip().strip("?.!,;:()[]{}\"'")
    if not city:
        return None
    city = re.sub(r"^(город|г\.)\s+", "", city, flags=re.IGNORECASE).strip()
    if not city:
        return None
    return city


def _call_weather_tool(city: str) -> dict:
    ctx = get_ctx()
    weather_dir = Path(ctx.paths.skills_workspace_dir()) / "weather_skill"

    prev = ctx.skill_ctx.get()
    try:
        ctx.skill_ctx.set("weather_skill", weather_dir)
        return execute_tool(
            weather_dir,
            module="handlers.main",
            attr="get_weather",
            payload={"city": city, "silent": True},
        )
    finally:
        if prev is None:
            try:
                ctx.skill_ctx.clear()
            except Exception:
                pass
        else:
            try:
                ctx.skill_ctx.set(prev.name, prev.path)
            except Exception:
                pass


@tool("handle_text")
def handle_text(text: str, _meta: Mapping[str, Any] | None = None, **_: Any) -> Mapping[str, Any]:
    """
    Web voice-chat MVP pipeline:
      text in -> derive weather request -> publish chat reply + TTS request.
    """
    _log.debug("voice_chat_skill.handle_text text=%r meta=%r", text, _meta)
    meta = dict(_meta or {})
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text_required"}
    text = text.strip()
    webspace_id = _webspace_id_from_meta(meta)
    target_node_id = str(meta.get("target_node_id") or meta.get("node_id") or "").strip() or None
    _append_projected_message(webspace_id, target_node_id, from_="user", text=text)

    city_raw = _extract_city(text)
    if not city_raw:
        reply = "Пока я понимаю только запросы про погоду. Скажи: «Какая погода в Москве?»"
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        return {"ok": False, "error": "intent_not_supported"}

    city_for_weather, city_display = _canon_city_for_weather(city_raw)
    if not city_for_weather:
        reply = "Не понял город. Попробуй: «Какая погода в Москве?»"
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        return {"ok": False, "error": "city_required"}

    try:
        result = _call_weather_tool(city_for_weather)
    except Exception as exc:
        reply = f"Ошибка при получении погоды: {exc}"
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        return {"ok": False, "error": str(exc)}

    ok = isinstance(result, dict) and bool(result.get("ok"))
    if not ok:
        err = result.get("error") if isinstance(result, dict) else None
        reply = f"Не удалось получить погоду в {city_display}." + (f" ({err})" if err else "")
        _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
        return {"ok": False, "error": err or "weather_failed"}

    temp = result.get("temp_c") if result.get("temp_c") is not None else result.get("temp")
    desc = result.get("condition") or result.get("description") or ""
    reply = f"Погода в {city_display}: {temp}°C, {desc}".strip().rstrip(",")

    _append_reply(reply, webspace_id=webspace_id, target_node_id=target_node_id)
    say(reply, lang=meta.get("lang") or "ru-RU")
    return {"ok": True, "reply": reply, "ts": time.time()}


@tool("get_snapshot")
def get_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    target_node_id: str | None = None,
    node_id: str | None = None,
    **_: Any,
) -> Mapping[str, Any]:
    selected_node_id = str(target_node_id or node_id or "").strip() or None
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    snapshot = dict(_state_for(selected_webspace, selected_node_id))
    _project_state(selected_webspace, selected_node_id)
    return {
        "voice_chat": snapshot,
        **snapshot,
    }

