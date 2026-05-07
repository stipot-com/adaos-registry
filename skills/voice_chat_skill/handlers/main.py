from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Mapping
import logging
import queue
import threading

from adaos.sdk.core.decorators import tool
from adaos.sdk.io.out import chat_append, say
from adaos.services.agent_context import get_ctx
from adaos.services.scenario.node_data_scope import node_scope_data_path
from adaos.services.yjs.doc import get_ydoc
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


def _voice_chat_data_path(target_node_id: str | None) -> str:
    return node_scope_data_path("data/voice_chat", str(target_node_id or "").strip())


def _read_voice_chat_state(webspace_id: str, target_node_id: str | None = None) -> dict[str, Any]:
    path = _voice_chat_data_path(target_node_id)
    segments = [segment for segment in str(path or "").split("/") if segment]
    with get_ydoc(str(webspace_id or "default")) as ydoc:
        data_map = ydoc.get_map("data")
        current = data_map.to_json() if hasattr(data_map, "to_json") else {}
    if isinstance(current, str):
        try:
            import json

            current = json.loads(current)
        except Exception:
            current = {}
    if not isinstance(current, dict):
        return {"messages": [], "last_refresh_ts": time.time()}
    cursor: Any = current
    for segment in segments[1:]:
        if not isinstance(cursor, dict):
            return {"messages": [], "last_refresh_ts": time.time()}
        cursor = cursor.get(segment)
    state = dict(cursor) if isinstance(cursor, dict) else {}
    messages = state.get("messages")
    if not isinstance(messages, list):
        state["messages"] = []
    state.setdefault("last_refresh_ts", time.time())
    return state


def _read_voice_chat_state_guarded(
    webspace_id: str,
    target_node_id: str | None = None,
    *,
    timeout_s: float = 1.5,
) -> dict[str, Any]:
    result_q: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_q.put((True, _read_voice_chat_state(webspace_id, target_node_id)), block=False)
        except Exception as exc:
            try:
                result_q.put((False, exc), block=False)
            except Exception:
                pass

    thread = threading.Thread(target=_worker, name="voice-chat-snapshot-read", daemon=True)
    thread.start()
    try:
        ok, value = result_q.get(timeout=timeout_s)
    except queue.Empty:
        _log.warning(
            "voice_chat snapshot read timed out webspace=%s target_node_id=%s timeout_s=%.1f",
            webspace_id,
            target_node_id or "",
            timeout_s,
        )
        return {
            "messages": [],
            "last_refresh_ts": time.time(),
            "degraded": True,
            "error": "voice_snapshot_timeout",
        }
    if ok and isinstance(value, dict):
        return value
    _log.warning("voice_chat snapshot read failed webspace=%s error=%s", webspace_id, value)
    return {
        "messages": [],
        "last_refresh_ts": time.time(),
        "degraded": True,
        "error": f"voice_snapshot_failed:{type(value).__name__}",
    }


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

    city_raw = _extract_city(text)
    if not city_raw:
        reply = "Пока я понимаю только запросы про погоду. Скажи: «Какая погода в Москве?»"
        chat_append(reply, from_="hub")
        return {"ok": False, "error": "intent_not_supported"}

    city_for_weather, city_display = _canon_city_for_weather(city_raw)
    if not city_for_weather:
        reply = "Не понял город. Попробуй: «Какая погода в Москве?»"
        chat_append(reply, from_="hub")
        return {"ok": False, "error": "city_required"}

    try:
        result = _call_weather_tool(city_for_weather)
    except Exception as exc:
        reply = f"Ошибка при получении погоды: {exc}"
        chat_append(reply, from_="hub")
        return {"ok": False, "error": str(exc)}

    ok = isinstance(result, dict) and bool(result.get("ok"))
    if not ok:
        err = result.get("error") if isinstance(result, dict) else None
        reply = f"Не удалось получить погоду в {city_display}." + (f" ({err})" if err else "")
        chat_append(reply, from_="hub")
        return {"ok": False, "error": err or "weather_failed"}

    temp = result.get("temp_c") if result.get("temp_c") is not None else result.get("temp")
    desc = result.get("condition") or result.get("description") or ""
    reply = f"Погода в {city_display}: {temp}°C, {desc}".strip().rstrip(",")

    chat_append(reply, from_="hub")
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
    selected_webspace = str(webspace_id or "default").strip() or "default"
    snapshot = _read_voice_chat_state_guarded(selected_webspace, selected_node_id)
    return {
        "voice_chat": snapshot,
        **snapshot,
    }

