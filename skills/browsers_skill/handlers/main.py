from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import access_links as sdk_access_links
from adaos.sdk.data import device_access as sdk_device_access
from adaos.sdk.data import ctx_subnet
from adaos.sdk.io import stream_publish
from adaos.services.yjs.webspace import default_webspace_id

_SELECTED_BROWSER_BY_WS: dict[str, str] = {}
REQUIRES_DATA_PROJECTIONS = [
    "browsers.summary",
    "browsers.devices",
    "browsers.clients",
    "browsers.current_summary",
    "browsers.current_name",
]


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


def _receiver_from_payload(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("receiver") or "").strip()


def _iso(value: Any) -> str | None:
    try:
        token = float(value)
    except Exception:
        return None
    if token <= 0:
        return None
    return datetime.fromtimestamp(token, tz=timezone.utc).replace(microsecond=0).isoformat()


def _target_webspaces(target_ws: str | None = None) -> list[str]:
    token = str(target_ws or "").strip()
    if token:
        return [token]
    return [default_webspace_id()]


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
    effective_ws = str(target_ws or default_webspace_id()).strip() or default_webspace_id()
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
    for ws in _target_webspaces(effective_ws):
        await ctx_subnet.set_async("browsers.summary", payload["summary"], webspace_id=ws)
        await ctx_subnet.set_async("browsers.devices", payload["devices"], webspace_id=ws)
        await ctx_subnet.set_async("browsers.clients", payload["clients"], webspace_id=ws)
        current_id = _resolve_current_browser_id(available_entries, ws)
        ws_current_summary, ws_current_name = _current_browser_payload(current_id)
        await ctx_subnet.set_async("browsers.current_summary", ws_current_summary, webspace_id=ws)
        await ctx_subnet.set_async("browsers.current_name", ws_current_name, webspace_id=ws)
        _publish_streams(
            {
                **payload,
                "current_summary": ws_current_summary,
                "current_name": ws_current_name,
            },
            webspace_id=ws,
        )
    return payload


def _publish_stream(receiver: str, data: Any, *, webspace_id: str | None = None) -> None:
    stream_publish(
        receiver,
        data,
        _meta={
            "webspace_id": str(webspace_id or "default").strip() or "default",
        },
    )


def _publish_streams(payload: Mapping[str, Any], *, webspace_id: str | None = None) -> None:
    _publish_stream("browsers.summary", payload.get("summary"), webspace_id=webspace_id)
    _publish_stream("browsers.devices", payload.get("devices"), webspace_id=webspace_id)
    _publish_stream("browsers.clients", payload.get("clients"), webspace_id=webspace_id)
    _publish_stream("browsers.current_summary", payload.get("current_summary"), webspace_id=webspace_id)
    _publish_stream("browsers.current_name", payload.get("current_name"), webspace_id=webspace_id)


def _publish_stream_snapshot(receiver: str, webspace_id: str | None = None) -> None:
    payload, effective_ws = _build_snapshot(webspace_id)
    available_entries = list(payload["devices"]) + list(payload["clients"])
    current_id = _resolve_current_browser_id(available_entries, effective_ws)
    current_summary, current_name = _current_browser_payload(current_id)
    data_by_receiver = {
        "browsers.summary": payload["summary"],
        "browsers.devices": payload["devices"],
        "browsers.clients": payload["clients"],
        "browsers.current_summary": current_summary,
        "browsers.current_name": current_name,
    }
    if receiver in data_by_receiver:
        _publish_stream(receiver, data_by_receiver[receiver], webspace_id=effective_ws)


def _refresh_snapshot_sync(target_ws: str | None = None) -> dict[str, Any]:
    payload, _ = _build_snapshot(target_ws)
    _run_coro(_publish_snapshot(target_ws))
    return payload


def _selected_browser_ref(webspace_id: str | None = None) -> str | None:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return None
    return f"browser:{device_id}"


def _coerce_device_ref(
    *,
    device_ref: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    browser_device_id: str | None = None,
    device_id: str | None = None,
    webspace_id: str | None = None,
) -> str | None:
    token = str(device_ref or "").strip()
    if token:
        return token
    member_id = str(target_node_id or node_id or "").strip()
    if member_id:
        return f"member:{member_id}"
    browser_id = str(browser_device_id or device_id or "").strip()
    if browser_id:
        return f"browser:{browser_id}"
    return _selected_browser_ref(webspace_id)


@tool
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    payload = _refresh_snapshot_sync(webspace_id)
    return {"ok": True, "summary": payload.get("summary") or {}, "delivery": "stream"}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    receiver = _receiver_from_payload(payload)
    if not receiver.startswith("browsers."):
        return
    _publish_stream_snapshot(receiver, _webspace_id_from_payload(payload))


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    if str(payload.get("action") or "").strip().lower() == "unsubscribed":
        return
    receiver = _receiver_from_payload(payload)
    if not receiver.startswith("browsers."):
        return
    _publish_stream_snapshot(receiver, _webspace_id_from_payload(payload))


@tool
def select_browser(device_id: str, webspace_id: str | None = None) -> dict[str, Any]:
    token = str(device_id or "").strip()
    target_ws = str(webspace_id or "default").strip() or "default"
    if token:
        _SELECTED_BROWSER_BY_WS[target_ws] = token
    _refresh_snapshot_sync(target_ws)
    return {"ok": True, "selected_device_id": token}


@tool
def rename_selected_browser(name: str, webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    result = sdk_device_access.rename_device(f"browser:{device_id}", str(name or "").strip())
    _refresh_snapshot_sync(target_ws)
    return result


@tool
def set_selected_browser_lifetime(preset: str, webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    result = sdk_device_access.set_device_lifetime(f"browser:{device_id}", str(preset or "permanent"))
    _refresh_snapshot_sync(target_ws)
    return result


@tool
def detach_selected_browser(webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    result = sdk_device_access.detach_device(f"browser:{device_id}")
    _refresh_snapshot_sync(target_ws)
    return result


@tool
def get_selected_browser_settings(webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    return get_device_settings(device_ref=f"browser:{device_id}", webspace_id=target_ws)


@tool
def adopt_selected_browser(name: str | None = None, preset: str = "permanent", webspace_id: str | None = None) -> dict[str, Any]:
    target_ws = str(webspace_id or "default").strip() or "default"
    device_id = str(_SELECTED_BROWSER_BY_WS.get(target_ws) or "").strip()
    if not device_id:
        return {"ok": False, "error": "browser_not_selected"}
    return adopt_device(
        device_ref=f"browser:{device_id}",
        name=name,
        preset=preset,
        webspace_id=target_ws,
    )


@tool
def rename_device(
    name: str,
    device_ref: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    browser_device_id: str | None = None,
    device_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    resolved = _coerce_device_ref(
        device_ref=device_ref,
        node_id=node_id,
        target_node_id=target_node_id,
        browser_device_id=browser_device_id,
        device_id=device_id,
        webspace_id=webspace_id,
    )
    if not resolved:
        return {"ok": False, "error": "device_ref_required"}
    result = sdk_device_access.rename_device(resolved, str(name or "").strip())
    _refresh_snapshot_sync(webspace_id)
    return result


@tool
def set_device_lifetime(
    preset: str,
    device_ref: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    browser_device_id: str | None = None,
    device_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    resolved = _coerce_device_ref(
        device_ref=device_ref,
        node_id=node_id,
        target_node_id=target_node_id,
        browser_device_id=browser_device_id,
        device_id=device_id,
        webspace_id=webspace_id,
    )
    if not resolved:
        return {"ok": False, "error": "device_ref_required"}
    result = sdk_device_access.set_device_lifetime(resolved, str(preset or "permanent"))
    _refresh_snapshot_sync(webspace_id)
    return result


@tool
def detach_device(
    device_ref: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    browser_device_id: str | None = None,
    device_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    resolved = _coerce_device_ref(
        device_ref=device_ref,
        node_id=node_id,
        target_node_id=target_node_id,
        browser_device_id=browser_device_id,
        device_id=device_id,
        webspace_id=webspace_id,
    )
    if not resolved:
        return {"ok": False, "error": "device_ref_required"}
    result = sdk_device_access.detach_device(resolved)
    _refresh_snapshot_sync(webspace_id)
    return result


@tool
def get_device_settings(
    device_ref: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    browser_device_id: str | None = None,
    device_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    resolved = _coerce_device_ref(
        device_ref=device_ref,
        node_id=node_id,
        target_node_id=target_node_id,
        browser_device_id=browser_device_id,
        device_id=device_id,
        webspace_id=webspace_id,
    )
    if not resolved:
        return {"ok": False, "error": "device_ref_required"}
    settings = sdk_device_access.get_device_settings(resolved)
    if settings is None:
        return {"ok": False, "error": "device_not_found", "device_ref": resolved}
    return settings


@tool
def adopt_device(
    name: str | None = None,
    preset: str = "permanent",
    device_ref: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    browser_device_id: str | None = None,
    device_id: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    resolved = _coerce_device_ref(
        device_ref=device_ref,
        node_id=node_id,
        target_node_id=target_node_id,
        browser_device_id=browser_device_id,
        device_id=device_id,
        webspace_id=webspace_id,
    )
    if not resolved:
        return {"ok": False, "error": "device_ref_required"}
    result = sdk_device_access.adopt_device(
        resolved,
        str(name or "").strip() or None,
        str(preset or "permanent"),
    )
    _refresh_snapshot_sync(webspace_id)
    return result


@tool
def rename_link(name: str, node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    return rename_device(
        name=name,
        node_id=node_id,
        target_node_id=target_node_id,
        webspace_id=webspace_id,
    )


@tool
def set_link_lifetime(preset: str, node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    return set_device_lifetime(
        preset=preset,
        node_id=node_id,
        target_node_id=target_node_id,
        webspace_id=webspace_id,
    )


@tool
def detach_link(node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    return detach_device(
        node_id=node_id,
        target_node_id=target_node_id,
        webspace_id=webspace_id,
    )


@tool
def get_link_settings(node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    return get_device_settings(
        node_id=node_id,
        target_node_id=target_node_id,
        webspace_id=webspace_id,
    )


@tool
def adopt_link(name: str | None = None, preset: str = "permanent", node_id: str | None = None, target_node_id: str | None = None, webspace_id: str | None = None) -> dict[str, Any]:
    return adopt_device(
        name=name,
        preset=preset,
        node_id=node_id,
        target_node_id=target_node_id,
        webspace_id=webspace_id,
    )


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
