from __future__ import annotations

import logging
from typing import Any, Dict

from adaos.sdk.core.decorators import subscribe
from adaos.sdk.web.desktop import (
    desktop_toggle_install,
)

_log = logging.getLogger("skills.web_desktop")


def _payload(evt: Any) -> Dict[str, Any]:
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload") or {}
        if isinstance(data, dict):
            return data
    if isinstance(evt, dict):
        return evt
    return {}


def _webspace_id(payload: Dict[str, Any]) -> str:
    """
    Resolve target webspace id for an event payload.

    Explicit fields on the payload (webspace_id/workspace_id) take
    precedence over metadata injected by the transport (_meta), so
    that callers can override the default/connection webspace.
    """
    if isinstance(payload, dict):
        direct = payload.get("webspace_id") or payload.get("workspace_id")
        if direct:
            return str(direct).strip()
        meta = payload.get("_meta")
        if isinstance(meta, dict):
            token = meta.get("webspace_id") or meta.get("workspace_id")
            if token:
                return str(token).strip()
    # Let the underlying service resolve the effective default webspace.
    return ""

@subscribe("desktop.toggleInstall")
async def on_toggle_install(evt) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    item_type = payload.get("type")
    target_id = payload.get("id")
    if item_type not in ("app", "widget") or not target_id:
        return
    try:
        desktop_toggle_install(
            str(item_type),
            str(target_id),
            webspace_id=str(webspace_id) or None,
        )
    except Exception:
        _log.warning(
            "desktop.toggleInstall failed webspace=%s type=%s target=%s",
            webspace_id,
            item_type,
            target_id,
            exc_info=True,
        )
