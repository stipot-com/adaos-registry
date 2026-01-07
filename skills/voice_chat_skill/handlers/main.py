from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import tool
from adaos.sdk.io.out import chat_append, say
from adaos.services.agent_context import get_ctx
from adaos.skills.runtime_runner import execute_tool


_WEATHER_RE = re.compile(
    r"(?:какая\s+)?погода\w*\s+(?:в|во)\s+(.+)$",
    re.IGNORECASE | re.UNICODE,
)


def _extract_city(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _WEATHER_RE.search(raw)
    if not m:
        return None
    city = m.group(1).strip().strip("?.!,;:()[]{}\"'")
    if not city:
        return None
    city = re.sub(r"^(город|г\.)\s+", "", city, flags=re.IGNORECASE).strip()
    if not city:
        return None
    return city[:1].upper() + city[1:]


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

    No direct Yjs writes: outputs are emitted via io.out.* topics, and RouterService
    projects them to the proper webspace based on `_meta.webspace_id`.
    """
    meta = dict(_meta or {})
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text_required"}
    text = text.strip()

    city = _extract_city(text)
    if not city:
        reply = 'Пока я понимаю только запросы про погоду. Скажи: «Какая погода в Москве?»'
        chat_append(reply, from_="hub", _meta=meta)
        return {"ok": False, "error": "intent_not_supported"}

    try:
        result = _call_weather_tool(city)
    except Exception as exc:
        reply = f"Ошибка при получении погоды: {exc}"
        chat_append(reply, from_="hub", _meta=meta)
        return {"ok": False, "error": str(exc)}

    ok = isinstance(result, dict) and bool(result.get("ok"))
    if not ok:
        err = result.get("error") if isinstance(result, dict) else None
        reply = f"Не удалось получить погоду в {city}." + (f" ({err})" if err else "")
        chat_append(reply, from_="hub", _meta=meta)
        return {"ok": False, "error": err or "weather_failed"}

    temp = result.get("temp_c") if result.get("temp_c") is not None else result.get("temp")
    desc = result.get("condition") or result.get("description") or ""
    resolved_city = result.get("city") or city
    reply = f"Погода в {resolved_city}: {temp}°C, {desc}".strip().rstrip(",")

    chat_append(reply, from_="hub", _meta=meta)
    say(reply, lang=meta.get("lang") or "ru-RU", _meta=meta)
    return {"ok": True, "reply": reply, "ts": time.time()}
