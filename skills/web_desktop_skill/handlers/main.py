from __future__ import annotations

import logging
from typing import Any, Dict

import asyncio
import os
import re

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.decorators import subscribe
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc, mutate_live_room

_log = logging.getLogger("skills.web_desktop")

# During static validation, handlers are imported in a lightweight subprocess
# without a full AdaOS runtime. In that case, avoid requiring AgentContext
# at import time to let validation introspect decorators safely.
if os.environ.get("ADAOS_VALIDATE") == "1":
    _ctx = None  # type: ignore[assignment]
else:
    _ctx = require_ctx("skills.web_desktop_skill")
_DEFAULT_SCENARIO_ID = "web_desktop"
_WS_ID_RE = re.compile(r"[^a-z0-9-_]+")


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
    return "default"


def _apply_ydoc_defaults(webspace_id: str, spec: Dict[str, Any]) -> None:
    """
    Deprecated helper; YDoc defaults are now applied by the core
    WebspaceScenarioRuntime. Kept as a no-op for backwards compatibility.
    """
    return


def _apply_install_toggle(webspace_id: str, ydoc, txn, item_type: str, target_id: str) -> None:
    data_map = ydoc.get_map("data")
    installed = data_map.get("installed") or {}
    if not isinstance(installed, dict):
        installed = {}
    apps = set(installed.get("apps") or [])
    widgets = set(installed.get("widgets") or [])
    if item_type == "app":
        if target_id in apps:
            apps.remove(target_id)
        else:
            apps.add(target_id)
    else:
        if target_id in widgets:
            widgets.remove(target_id)
        else:
            widgets.add(target_id)
    next_installed = {"apps": list(apps), "widgets": list(widgets)}
    data_map.set(txn, "installed", next_installed)
    desktop_value = data_map.get("desktop") or {}
    if not isinstance(desktop_value, dict):
        desktop_value = {}
    desktop_next = dict(desktop_value)
    desktop_installed = dict(desktop_next.get("installed") or {})
    desktop_installed["apps"] = list(apps)
    desktop_installed["widgets"] = list(widgets)
    desktop_next["installed"] = desktop_installed
    data_map.set(txn, "desktop", desktop_next)
    _log.debug(
        "toggle install webspace=%s type=%s target=%s apps=%s widgets=%s",
        webspace_id,
        item_type,
        target_id,
        sorted(apps),
        sorted(widgets),
    )


def _toggle_install(webspace_id: str, item_type: str, target_id: str) -> None:
    with get_ydoc(webspace_id) as ydoc:
        with ydoc.begin_transaction() as txn:
            _apply_install_toggle(webspace_id, ydoc, txn, item_type, target_id)


def _toggle_install_async(webspace_id: str, item_type: str, target_id: str) -> None:
    async def _worker() -> None:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                _apply_install_toggle(webspace_id, ydoc, txn, item_type, target_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_worker())
    else:
        loop.create_task(_worker(), name=f"web-desktop-toggle-{webspace_id}")


@subscribe("desktop.toggleInstall")
def on_toggle_install(evt) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    item_type = payload.get("type")
    target_id = payload.get("id")
    if item_type not in ("app", "widget") or not target_id:
        return
    live_applied = mutate_live_room(webspace_id, lambda doc, txn: _apply_install_toggle(webspace_id, doc, txn, item_type, str(target_id)))
    if not live_applied:
        _log.debug("mutate_live_room skipped for toggle webspace=%s type=%s target=%s", webspace_id, item_type, target_id)
    _toggle_install_async(webspace_id, item_type, str(target_id))


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
