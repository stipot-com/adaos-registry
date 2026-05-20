from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import (
    ProjectionContext,
    ProjectionRuntime,
    ProjectionSlot,
    StreamReceiver,
    StreamRuntime,
)
from adaos.sdk.data import access_links as sdk_access_links
from adaos.sdk.data import device_access as sdk_device_access
from adaos.sdk.data import ctx_subnet
from adaos.sdk.io import stream_publish

try:
    from adaos.services.yjs.webspace import default_webspace_id
except Exception:  # pragma: no cover - tests may run without optional y_py runtime deps.
    def default_webspace_id() -> str:
        return "default"

try:
    from adaos.services.workspaces import index as workspace_index
except Exception:  # pragma: no cover - workspace index is optional in tests/runtime slices.
    workspace_index = None

_LOG = logging.getLogger("adaos.skill.browsers")
_SELECTED_BROWSER_BY_WS: dict[str, str] = {}
_PROJECTION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browsers-projection")
_PENDING_REFRESH_BY_WS: dict[str, Any] = {}
REQUIRES_DATA_PROJECTIONS = [
    "browsers.summary",
]
_DATA_PROJECTION_ENTRIES = [
    {"scope": "subnet", "slot": "browsers.summary", "targets": [{"backend": "yjs", "path": "data/browsers/summary"}]},
]
_PROJECTION_SLOTS = [
    ProjectionSlot("browsers.summary", "data/browsers/summary"),
]
_PROJECTION_SLOT_BY_NAME = {slot.name: slot for slot in _PROJECTION_SLOTS}
_PROJECTION_RUNTIME = ProjectionRuntime(
    "browsers_skill",
    ctx_subnet=ctx_subnet,
    projections=_PROJECTION_SLOTS,
)


def _sdk_stream_publish(
    receiver: str,
    data: Any,
    *,
    ts: float | None = None,
    _meta: Mapping[str, Any] | None = None,
) -> Mapping[str, bool]:
    if ts is None:
        return stream_publish(receiver, data, _meta=_meta)
    return stream_publish(receiver, data, ts=ts, _meta=_meta)


def _build_registered_stream_payload(context: ProjectionContext) -> Any | None:
    return _build_stream_payload(str(context.receiver or ""), context.webspace_id)


_STREAM_RUNTIME = StreamRuntime(
    "browsers_skill",
    receivers=[
        StreamReceiver("browsers.summary", build=_build_registered_stream_payload),
        StreamReceiver("browsers.devices", build=_build_registered_stream_payload),
        StreamReceiver("browsers.clients", build=_build_registered_stream_payload),
        StreamReceiver("browsers.current_summary", build=_build_registered_stream_payload),
        StreamReceiver("browsers.current_name", build=_build_registered_stream_payload),
    ],
    stream_publish=_sdk_stream_publish,
)


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
    seen: list[str] = []

    def _add(value: Any) -> None:
        token = str(value or "").strip()
        if token and token not in seen:
            seen.append(token)

    _add(target_ws)
    _add(default_webspace_id())
    if workspace_index is not None:
        try:
            for entry in workspace_index.list_workspaces():
                _add(getattr(entry, "workspace_id", None) or getattr(entry, "id", None))
        except Exception:
            pass
    return seen or [default_webspace_id()]


def _projection_webspaces(target_ws: str | None = None, *, fanout: bool = False) -> list[str]:
    token = str(target_ws or "").strip()
    if fanout:
        return _target_webspaces(token or None)
    return [token or default_webspace_id()]


def _event_topic(evt: Any) -> str:
    for attr in ("topic", "type", "name"):
        token = str(getattr(evt, attr, "") or "").strip()
        if token:
            return token
    payload = getattr(evt, "payload", evt)
    if isinstance(payload, Mapping):
        for key in ("topic", "type", "event_type"):
            token = str(payload.get(key) or "").strip()
            if token:
                return token
    return ""


def _should_fanout_projection_refresh(topic: str, target_ws: str | None) -> bool:
    if str(target_ws or "").strip():
        return False
    return str(topic or "").strip().lower() in {"sys.ready", "skills.activated"}


def _should_force_projection_refresh(topic: str) -> bool:
    return str(topic or "").strip().lower() in {
        "sys.ready",
        "skills.activated",
        "desktop.webspace.refresh",
        "desktop.webspace.reload",
        "desktop.webspace.reloaded",
    }


async def _set_projection_if_changed(slot: str, value: Any, *, webspace_id: str, force: bool = False) -> bool:
    result = await _PROJECTION_RUNTIME.set_if_changed(
        _PROJECTION_SLOT_BY_NAME.get(str(slot or "").strip()) or slot,
        value,
        webspace_id=webspace_id,
        force=force,
        reason="browsers_snapshot_refresh",
    )
    return bool(result.written)


def _run_coro(coro: Any, *, wait: bool = True, key: str | None = None) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    future = _PROJECTION_EXECUTOR.submit(lambda: asyncio.run(coro))
    if not wait:
        if key:
            _PENDING_REFRESH_BY_WS[key] = future

        def _done(done_future: Any) -> None:
            if key:
                _PENDING_REFRESH_BY_WS.pop(key, None)
            try:
                done_future.result()
            except Exception:
                _LOG.warning("browsers snapshot projection failed", exc_info=True)

        future.add_done_callback(_done)
        return future
    return future.result()


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _schedule_publish_snapshot(target_ws: str | None = None, *, fanout: bool = False, force: bool = False) -> Any:
    key = "*" if fanout else str(target_ws or default_webspace_id()).strip() or default_webspace_id()
    pending = _PENDING_REFRESH_BY_WS.get(key)
    if pending is not None:
        try:
            if not pending.done():
                return pending
        except Exception:
            pass
    return _run_coro(_publish_snapshot(target_ws, fanout=fanout, force=force), wait=False, key=key)


def _ensure_skill_data_projections() -> None:
    try:
        from adaos.services.agent_context import get_ctx

        ctx = get_ctx()
        if ctx.projections.resolve("subnet", "browsers.summary"):
            return
        ctx.projections.load_entries(_DATA_PROJECTION_ENTRIES)
    except Exception:
        pass


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
            "online": bool(entry.get("online")),
            "status": "online" if bool(entry.get("online")) else "offline",
            "uiSubtitle": _browser_subtitle(entry),
        }
        for entry in entries
        if str(entry.get("id") or "").strip()
    ]


def _browser_sort_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _browser_title(entry).casefold(),
        str(entry.get("id") or "").strip().casefold(),
    )


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
    device_id = str(entry.get("id") or "").strip()
    summary = [
        {"title": "Device ID", "description": device_id or "-"},
        {"title": "Browser", "description": _browser_title(entry)},
        {"title": "Access", "description": str(entry.get("access_class") or "device").strip() or "device"},
        {"title": "Lifetime", "description": sdk_access_links.lifetime_label(entry)},
        {"title": "Last webspace", "description": str(entry.get("last_webspace_id") or "").strip() or "-"},
        {"title": "Last seen", "description": _iso(entry.get("last_seen_at")) or "-"},
        {"title": "Status", "description": "online" if bool(entry.get("online")) else "offline"},
    ]
    return summary, {"value": _browser_title(entry), "device_id": device_id}


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
    devices = sorted(
        [entry for entry in all_entries if str(entry.get("access_class") or "device").strip() != "client"],
        key=_browser_sort_key,
    )
    clients = sorted(
        [entry for entry in all_entries if str(entry.get("access_class") or "").strip() == "client"],
        key=_browser_sort_key,
    )
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


async def _publish_snapshot(target_ws: str | None = None, *, fanout: bool = False, force: bool = False) -> dict[str, Any]:
    _ensure_skill_data_projections()
    payload, effective_ws = _build_snapshot(target_ws)
    for ws in _projection_webspaces(effective_ws, fanout=fanout):
        await _set_projection_if_changed("browsers.summary", payload["summary"], webspace_id=ws, force=force)
        _publish_active_streams_from_snapshot(payload, webspace_id=ws)
    return payload


def _build_stream_payload(receiver: str, webspace_id: str | None = None) -> Any | None:
    payload, effective_ws = _build_snapshot(webspace_id)
    return _stream_payloads_from_snapshot(payload, webspace_id=effective_ws).get(receiver)


def _stream_payloads_from_snapshot(payload: Mapping[str, Any], *, webspace_id: str) -> dict[str, Any]:
    available_entries = list(payload.get("devices") or []) + list(payload.get("clients") or [])
    current_id = _resolve_current_browser_id(available_entries, webspace_id)
    current_summary, current_name = _current_browser_payload(current_id)
    data_by_receiver = {
        "browsers.summary": payload.get("summary") or {},
        "browsers.devices": list(payload.get("devices") or []),
        "browsers.clients": list(payload.get("clients") or []),
        "browsers.current_summary": current_summary,
        "browsers.current_name": current_name,
    }
    return data_by_receiver


def _active_stream_receivers(webspace_id: str) -> list[str]:
    target_ws = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    return [
        str(entry.get("receiver") or "").strip()
        for entry in _STREAM_RUNTIME.active_receivers_snapshot()
        if str(entry.get("webspace_id") or "").strip() == target_ws and str(entry.get("receiver") or "").strip()
    ]


def _publish_active_streams_from_snapshot(payload: Mapping[str, Any], *, webspace_id: str) -> None:
    data_by_receiver = _stream_payloads_from_snapshot(payload, webspace_id=webspace_id)
    for receiver in _active_stream_receivers(webspace_id):
        if receiver not in data_by_receiver:
            continue
        result = _STREAM_RUNTIME.publish_snapshot(receiver, data_by_receiver[receiver], webspace_id=webspace_id)
        if result.error:
            _LOG.debug("browsers active stream publish failed receiver=%s error=%s", receiver, result.error)


def _publish_stream_snapshot(receiver: str, webspace_id: str | None = None) -> None:
    effective_ws = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    result = _STREAM_RUNTIME.publish_receiver_snapshot(
        receiver,
        webspace_id=effective_ws,
        force=True,
        context=ProjectionContext(
            skill_id="browsers_skill",
            webspace_id=effective_ws,
            receiver=receiver,
            reason="direct_stream_snapshot",
        ),
    )
    if result is not None and result.error:
        _LOG.debug("browsers stream snapshot failed receiver=%s error=%s", receiver, result.error)


def _refresh_snapshot_sync(target_ws: str | None = None, *, fanout: bool = False, force: bool = False) -> dict[str, Any]:
    if _has_running_loop():
        payload, _ = _build_snapshot(target_ws)
        _schedule_publish_snapshot(target_ws, fanout=fanout, force=force)
        return payload
    else:
        payload = _run_coro(_publish_snapshot(target_ws, fanout=fanout, force=force))
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
    payload = _refresh_snapshot_sync(webspace_id, force=True)
    return {"ok": True, "summary": payload.get("summary") or {}, "delivery": "projection"}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    _STREAM_RUNTIME.handle_snapshot_requested(evt, receiver_prefix="browsers.")


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    _STREAM_RUNTIME.handle_subscription_changed(evt, receiver_prefix="browsers.")


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
    target_ws = _webspace_id_from_payload(payload)
    topic = _event_topic(evt)
    _refresh_snapshot_sync(
        target_ws,
        fanout=_should_fanout_projection_refresh(topic, target_ws),
        force=_should_force_projection_refresh(topic),
    )


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
