from __future__ import annotations

import logging
from typing import Any, Dict, List

from adaos.sdk.core.decorators import subscribe
from adaos.sdk.web.desktop import (
    desktop_toggle_install,
    desktop_get_installed_async,
    desktop_set_installed,
)
from adaos.sdk.data import skill_memory_get, skill_memory_set

_log = logging.getLogger("skills.web_desktop")
_INSTALLED_KEY_PREFIX = "desktop.installed"


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


def _installed_key(webspace_id: str) -> str:
    token = (webspace_id or "").strip()
    return f"{_INSTALLED_KEY_PREFIX}:{token}"


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
        # Persist the current installed set in skill-local memory so it
        # can be restored after a YJS reload or reseeding.
        snapshot = await desktop_get_installed_async(webspace_id or None)
        try:
            skill_memory_set(_installed_key(webspace_id), snapshot)
        except Exception:  # pragma: no cover - defensive, best-effort persistence
            _log.debug("failed to persist installed snapshot for webspace=%s", webspace_id, exc_info=True)
    except Exception:
        _log.warning(
            "desktop.toggleInstall failed webspace=%s type=%s target=%s",
            webspace_id,
            item_type,
            target_id,
            exc_info=True,
        )


@subscribe("desktop.webspace.reload")
def on_webspace_reload(evt) -> None:
    """
    After a YJS reload the installed apps/widgets set in the webspace
    is rebuilt from scenario defaults. Restore the last known installed
    snapshot from skill-local memory so user choices survive reseeding.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    key = _installed_key(webspace_id)
    snapshot = skill_memory_get(key, None)
    if not isinstance(snapshot, dict):
        return
    apps: List[str] = list(snapshot.get("apps") or [])
    widgets: List[str] = list(snapshot.get("widgets") or [])
    if not apps and not widgets:
        return
    try:
        desktop_set_installed(apps, widgets, webspace_id=webspace_id or None, live=True)
    except Exception:
        _log.warning(
            "failed to restore installed after reload webspace=%s",
            webspace_id,
            exc_info=True,
        )
