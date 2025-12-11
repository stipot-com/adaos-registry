from __future__ import annotations

import logging
from typing import Any, Dict

import os

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.decorators import subscribe
from adaos.services.io_web import WebDesktopService

_log = logging.getLogger("skills.web_desktop")

# During static validation, handlers are imported in a lightweight subprocess
# without a full AdaOS runtime. In that case, avoid requiring AgentContext
# at import time to let validation introspect decorators safely.
if os.environ.get("ADAOS_VALIDATE") == "1":
    _ctx = None  # type: ignore[assignment]
else:
    _ctx = require_ctx("skills.web_desktop_skill")


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
            return str(direct)
        meta = payload.get("_meta")
        if isinstance(meta, dict):
            token = meta.get("webspace_id") or meta.get("workspace_id")
            if token:
                return str(token)
    # Let the underlying service resolve the effective default webspace.
    return ""


def _desktop_service() -> WebDesktopService:
    global _ctx  # type: ignore[global-variable-not-assigned]
    if _ctx is None:
        _ctx = require_ctx("skills.web_desktop_skill")
    return WebDesktopService(_ctx)


@subscribe("desktop.toggleInstall")
def on_toggle_install(evt) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    item_type = payload.get("type")
    target_id = payload.get("id")
    if item_type not in ("app", "widget") or not target_id:
        return
    try:
        _desktop_service().toggle_install_with_live_room(
            str(item_type),
            str(target_id),
            webspace_id=str(webspace_id),
        )
    except Exception:
        _log.warning(
            "desktop.toggleInstall failed webspace=%s type=%s target=%s",
            webspace_id,
            item_type,
            target_id,
            exc_info=True,
        )


@subscribe("desktop.webspace.create")
def on_webspace_create(evt) -> None:  # legacy topic, handled by core runtime
    return


@subscribe("desktop.webspace.rename")
def on_webspace_rename(evt) -> None:  # legacy topic, handled by core runtime
    return


@subscribe("desktop.webspace.delete")
def on_webspace_delete(evt) -> None:  # legacy topic, handled by core runtime
    return


@subscribe("desktop.webspace.refresh")
def on_webspace_refresh(evt) -> None:  # legacy topic, handled by core runtime
    return


@subscribe("desktop.webspace.reload")
def on_webspace_reload(evt) -> None:  # legacy topic, handled by core runtime
    return


@subscribe("desktop.webspace.reset")
def on_webspace_reset(evt) -> None:  # legacy topic, handled by core runtime
    return
