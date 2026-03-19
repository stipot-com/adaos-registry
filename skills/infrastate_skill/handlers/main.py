from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.services.core_slots import slot_status
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.node_config import load_config
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot

_log = logging.getLogger("skills.infrastate_skill")


def lang_res() -> dict[str, str]:
    return {}


def _base_dir() -> Path:
    raw = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".adaos").resolve()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _status_log_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key, title in (("stdout", "stdout"), ("stderr", "stderr")):
        text = str(status.get(key) or "").strip()
        if not text:
            continue
        items.append(
            {
                "id": key,
                "title": title,
                "status": "ok" if key == "stdout" else "warn",
                "preview": text[-400:].strip(),
                "content": text[-4000:].strip(),
            }
        )
    return items


def _slot_items(slots_payload: dict[str, Any]) -> list[dict[str, Any]]:
    active = str(slots_payload.get("active_slot") or "")
    previous = str(slots_payload.get("previous_slot") or "")
    raw_slots = slots_payload.get("slots") or {}
    items: list[dict[str, Any]] = []
    if not isinstance(raw_slots, dict):
        return items
    for slot_id, slot_meta in sorted(raw_slots.items()):
        meta = slot_meta if isinstance(slot_meta, dict) else {}
        manifest = meta.get("manifest") if isinstance(meta.get("manifest"), dict) else {}
        badges: list[str] = []
        if slot_id == active:
            badges.append("active")
        if slot_id == previous:
            badges.append("previous")
        items.append(
            {
                "id": str(slot_id),
                "title": f"Slot {slot_id}",
                "status": "ok" if slot_id == active else "idle",
                "subtitle": str(manifest.get("target_rev") or manifest.get("target_version") or "empty"),
                "description": str(meta.get("path") or ""),
                "version": str(manifest.get("target_version") or ""),
                "target_rev": str(manifest.get("target_rev") or ""),
                "badges": badges,
            }
        )
    return items


def _step_items(status: dict[str, Any], slots_payload: dict[str, Any], lifecycle: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(status.get("state") or "idle")
    phase = str(status.get("phase") or "")
    active = str(slots_payload.get("active_slot") or "—")
    previous = str(slots_payload.get("previous_slot") or "—")
    node_state = str(lifecycle.get("node_state") or "ready")
    return [
        {"id": "lifecycle", "title": "Lifecycle", "status": node_state, "description": str(lifecycle.get("reason") or "ready")},
        {"id": "update_state", "title": "Update state", "status": state, "description": phase or state},
        {"id": "active_slot", "title": "Active slot", "status": "ok", "description": active},
        {"id": "previous_slot", "title": "Previous slot", "status": "idle", "description": previous},
    ]


def _summary(status: dict[str, Any], slots_payload: dict[str, Any], lifecycle: dict[str, Any], conf) -> dict[str, Any]:
    active = str(slots_payload.get("active_slot") or "—")
    phase = str(status.get("phase") or "")
    state = str(status.get("state") or "idle")
    message = str(status.get("message") or lifecycle.get("reason") or "No update in progress")
    return {
        "label": "Core update",
        "value": state,
        "subtitle": f"slot {active}",
        "description": message,
        "phase": phase,
        "role": str(getattr(conf, "role", "") or ""),
        "node_id": str(getattr(conf, "node_id", "") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "root_url": str(getattr(getattr(conf, "root_settings", None), "base_url", "") or ""),
        "updated_at": float(status.get("updated_at") or time.time()),
        "draining": bool(lifecycle.get("draining")),
    }


def _snapshot() -> dict[str, Any]:
    conf = load_config()
    status = read_core_update_status()
    slots_payload = slot_status()
    lifecycle = runtime_lifecycle_snapshot()
    report = _read_json(_base_dir() / "state" / "core_update" / "status.json") or {}
    steps = _step_items(status, slots_payload, lifecycle)
    snapshot = {
        "summary": _summary(status, slots_payload, lifecycle, conf),
        "steps": steps,
        "slots": _slot_items(slots_payload),
        "logs": _status_log_items(report),
        "status": status,
        "lifecycle": lifecycle,
        "slots_meta": slots_payload,
        "last_refresh_ts": time.time(),
    }
    return snapshot


def _project(snapshot: dict[str, Any], webspace_id: str | None = None) -> None:
    ctx_subnet.set("infrastate.snapshot", snapshot, webspace_id=webspace_id)


def _webspace_id_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        token = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


@tool("get_snapshot")
def get_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _snapshot()
    if webspace_id:
        _project(snapshot, webspace_id=webspace_id)
    return snapshot


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _snapshot()
    _project(snapshot, webspace_id=webspace_id)
    return {"ok": True, **snapshot}


@subscribe("infrastate.refresh")
def on_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("skills.activated")
def on_skill_activated(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, dict):
        return
    skill_name = str(payload.get("skill_name") or "")
    if skill_name and skill_name != "infrastate_skill":
        return
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("desktop.webspace.reload")
def on_webspace_reload(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("subnet.nats.up")
@subscribe("subnet.stopping")
@subscribe("subnet.stopped")
def on_runtime_event(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    try:
        refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))
    except Exception:
        _log.debug("failed to refresh infrastate snapshot from runtime event", exc_info=True)
