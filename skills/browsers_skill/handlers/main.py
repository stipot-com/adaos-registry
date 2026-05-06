from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import access_links as sdk_access_links
from adaos.sdk.data import ctx_subnet
from adaos.services.subnet.link_manager import get_hub_link_manager
from adaos.services.workspaces import index as workspace_index

_log = logging.getLogger("skills.browsers_skill")
_SELECTED_BROWSER_BY_WS: dict[str, str] = {}


def lang_res() -> Dict[str, str]:
    return {}


def _webspace_id_from_payload(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        token = str(payload.get("webspace_id") or payload.get("workspace_id") or "").strip()
        if token:
            return token
        meta = payload.get("_meta")
        if isinstance(meta, Mapping):
            token = str(meta.get("webspace_id") or meta.get("workspace_id") or "").strip()
            if token:
                return token
    return None


def _iso(value: Any) -> str | None:
    try:
        token = float(value)
    except Exception:
        return None
    if token <= 0:
        return None
    return datetime.fromtimestamp(token, tz=timezone.utc).replace(microsecond=0).isoformat()


def _known_webspaces(target_ws: str | None = None) -> list[str]:
    values = {"default"}
    if target_ws:
        values.add(str(target_ws or "").strip() or "default")
    for row in list(workspace_index.list_workspaces() or []):
        token = str(getattr(row, "workspace_id", "") or "").strip()
        if token:
            values.add(token)
    return sorted(values)


def _run_coro(coro: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop.create_task(coro)
    return None


def _browser_title(entry: Mapping[str, Any]) -> str:
    return (
        str(entry.get("display_name") or "").strip()
        or str(entry.get("hostname") or "").strip()
        or str(entry.get("id") or "").strip()
        or "browser"
    )


def _browser_subtitle(entry: Mapping[str, Any]) -> str:
    bits: list[str] = []
    webspace_id = str(entry.get("last_webspace_id") or "").strip()
    if webspace_id:
        bits.append(f"Webspace {webspace_id}")
    bits.append("online" if bool(entry.get("online")) else "offline")
    bits.append(sdk_access_links.lifetime_label(dict(entry)))
    return " | ".join(bits)


def _browser_details(entry: Mapping[str, Any]) -> str:
    rows = [
        f"ID: {str(entry.get('id') or '').strip() or '-'}",
        f"Access: {str(entry.get('access_class') or 'device').strip() or 'device'}",
        f"Lifetime: {sdk_access_links.lifetime_label(dict(entry))}",
        f"Last webspace: {str(entry.get('last_webspace_id') or '').strip() or '-'}",
        f"Last seen: {_iso(entry.get('last_seen_at')) or '-'}",
        f"Status: {'online' if bool(entry.get('online')) else 'offline'}",
    ]
    return "\n".join(rows)


def _browser_tiles(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(entry.get("id") or "").strip(),
            "device_id": str(entry.get("id") or "").strip(),
            "title": _browser_title(entry),
            "subtitle": _browser_subtitle(entry),
            "content": _browser_details(entry),
            "icon": "browsers-outline",
            "uiSubtitle": _browser_subtitle(entry),
        }
        for entry in entries
        if str(entry.get("id") or "").strip()
    ]


def _current_browser_payload(device_id: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entry = sdk_access_links.get_browser_link(str(device_id or "").strip()) if device_id else None
    if not entry:
        return (
            [
                {
                    "title": "No browser selected",
                    "description": "Pick a browser from Devices or Clients.",
                }
            ],
            {"value": ""},
        )
    summary = [
        {"title": "Browser", "description": _browser_title(entry)},
        {"title": "Access", "description": str(entry.get("access_class") or "device").strip() or "device"},
        {"title": "Lifetime", "description": sdk_access_links.lifetime_label(entry)},
        {"title": "Last webspace", "description": str(entry.get("last_webspace_id") or "").strip() or "-"},
        {"title": "Last seen", "description": _iso(entry.get("last_seen_at")) or "-"},
        {"title": "Status", "description": "online" if bool(entry.get("online")) else "offline"},
    ]
    return summary, {"value": _browser_title(entry)}


def _resolve_current_browser_id(entries: list[Mapping[str, Any]], webspace_id: str) -> str | None:
    available_ids = [
        str(entry.get("id") or "").strip()
        for entry in entries
        if str(entry.get("id") or "").strip()
    ]
    selected_id = str(_SELECTED_BROWSER_BY_WS.get(webspace_id) or "").strip() or None
    if selected_id and selected_id in available_ids:
        return selected_id
    if selected_id:
        _SELECTED_BROWSER_BY_WS.pop(webspace_id, None)
    fallback_id = available_ids[0] if available_ids else None
    if fallback_id:
        _SELECTED_BROWSER_BY_WS[webspace_id] = fallback_id
    return fallback_id


def _build_snapshot(target_ws: str | None = None) -> tuple[dict[str, Any], str]:
    all_entries = sdk_access_links.list_browser_links()
    devices = [entry for entry in all_entries if str(entry.get("access_class") or "device").strip() != "client"]
    clients = [entry for entry in all_entries if str(entry.get("access_class") or "").strip() == "client"]
    summary = {
        "title": "Browsers",
        "value": len(all_entries),
        "subtitle": f"{len(devices)} devices | {len(clients)} clients",
        "details": f"{sum(1 for entry in all_entries if bool(entry.get('online')))} online",
    }
    effective_ws = str(target_ws or "default").strip() or "default"
    current_device_id = _resolve_current_browser_id(devices + clients, effective_ws)
    current_summary, current_name = _current_browser_payload(current_device_id)
    return ({
        "summary": summary,
        "devices": _browser_tiles(devices),
        "clients": _browser_tiles(clients),
        "current_summary": current_summary,
        "current_name": current_name,
    }, effective_ws)


async def _publish_snapshot(target_ws: str | None = None) -> dict[str, Any]:
    payload, effective_ws = _build_snapshot(target_ws)
    available_entries = list(payload["devices"]) + list(payload["clients"])
    for ws in _known_webspaces(effective_ws):
        await ctx_subnet.set_async("browsers.summary", payload["summary"], webspace_id=ws)
        await ctx_subnet.set_async("browsers.devices", payload["devices"], webspace_id=ws)
        await ctx_subnet.set_async("browsers.clients", payload["clients"], webspace_id=ws)
        current_id = _resolve_current_browser_id(available_entries, ws)
        ws_current_summary, ws_current_name = _current_browser_payload(current_id)
        await ctx_subnet.set_async("browsers.current_summary", ws_current_summary, webspace_id=ws)
        await ctx_subnet.set_async("browsers.current_name", ws_current_name, webspace_id=ws)
    return payload


def _refresh_snapshot_sync(target_ws: str | None = None) -> dict[str, Any]:
    payload, _ = _build_snapshot(target_ws)
    _run_coro(_publish_snapshot(target_ws))
    return payload


@tool
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    return _refresh_snapshot_sync(webspace_id)


@tool
def select_browser(device_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    token = str(device_id or "").strip()
    target_ws = str(webspace_id or "default").strip() or "default"
    if token:
        _SELECTED_BROWSER_BY_WS[target_ws] = token
    snapshot = _refresh_snapshot_sync(target_ws)
    return {"ok": True, "selected_device_id": token, "snapshot": snapshot}


@tool
def rename_selected_browser(name: str, webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    entry = sdk_access_links.rename_browser_link(device_id, str(name or "").strip())
    _refresh_snapshot_sync(target_ws)
    return {"ok": True, "entry": entry}


@tool
def set_selected_browser_lifetime(preset: str, webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    entry = sdk_access_links.set_browser_lifetime(device_id, str(preset or "permanent"))
    _refresh_snapshot_sync(target_ws)
    return {"ok": True, "entry": entry}


@tool
def detach_selected_browser(webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    entry = sdk_access_links.detach_browser_link(device_id)
    _refresh_snapshot_sync(target_ws)
    return {"ok": True, "entry": entry}


@tool
def rename_link(name: str, node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    effective_node_id = str(target_node_id or node_id or "").strip()
    if not effective_node_id:
        return {"ok": False, "error": "node_id_required"}
    entry = sdk_access_links.rename_member_link(effective_node_id, str(name or "").strip())
    try:
        mgr = get_hub_link_manager()
        if mgr.is_connected(effective_node_id) and str(name or "").strip():
            _run_coro(mgr.set_member_node_names(effective_node_id, node_names=[str(name).strip()]))
    except Exception:
        _log.debug("rename_link runtime update failed node_id=%s", effective_node_id, exc_info=True)
    _refresh_snapshot_sync(webspace_id)
    return {"ok": True, "entry": entry}


@tool
def set_link_lifetime(preset: str, node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    effective_node_id = str(target_node_id or node_id or "").strip()
    if not effective_node_id:
        return {"ok": False, "error": "node_id_required"}
    entry = sdk_access_links.set_member_lifetime(effective_node_id, str(preset or "permanent"))
    _refresh_snapshot_sync(webspace_id)
    return {"ok": True, "entry": entry}


@tool
def detach_link(node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    effective_node_id = str(target_node_id or node_id or "").strip()
    if not effective_node_id:
        return {"ok": False, "error": "node_id_required"}
    entry = sdk_access_links.detach_member_link(effective_node_id)
    try:
        mgr = get_hub_link_manager()
        if mgr.is_connected(effective_node_id):
            _run_coro(mgr.unregister(effective_node_id))
    except Exception:
        _log.debug("detach_link runtime unregister failed node_id=%s", effective_node_id, exc_info=True)
    _refresh_snapshot_sync(webspace_id)
    return {"ok": True, "entry": entry}


@subscribe("sys.ready")
@subscribe("skills.activated")
@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
@subscribe("desktop.webspace.reloaded")
@subscribe("browser.session.changed")
@subscribe("device.registered")
@subscribe("subnet.member.link.up")
@subscribe("subnet.member.link.down")
def _on_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    _refresh_snapshot_sync(_webspace_id_from_payload(payload))


def handle(topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    topic_token = str(topic or "").strip().lower()
    if topic_token in {"browsers.refresh", "desktop.webspace.refresh", "sys.ready"}:
        return refresh_snapshot(webspace_id=_webspace_id_from_payload(data))
    return {
        "ok": True,
        "skill": "browsers_skill",
        "topic": str(topic or ""),
        "handled": topic_token in {"browsers.refresh", "desktop.webspace.refresh", "sys.ready"},
        "payload": data,
    }
