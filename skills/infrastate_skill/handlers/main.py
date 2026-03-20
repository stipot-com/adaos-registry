from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from adaos.build_info import BUILD_INFO
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet, skill_memory_get, skill_memory_set
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest, slot_status
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.node_config import load_config
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.scenario.webspace_runtime import WebspaceService
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("skills.infrastate_skill")
_UI_STATE_KEY = "infrastate.ui_state"
_EVENTS_STATE_KEY = "infrastate.events"


def lang_res() -> dict[str, str]:
    return {}


def _base_dir() -> Path:
    raw = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".adaos").resolve()


def _repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "src" / "adaos" / "build_info.py").exists():
            return parent
    return None


def _skill_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "skill.yaml").exists():
            return parent
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        existing = ctx.projections.resolve("subnet", "infrastate.snapshot")
        if existing:
            return
        skill_root = _skill_root()
        if skill_root is None:
            return
        manifest_path = skill_root / "skill.yaml"
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return
        entries = payload.get("data_projections") or []
        if not isinstance(entries, list) or not entries:
            return
        ctx.projections.load_entries(entries)
        _log.debug("loaded infrastate data_projections entries=%d", len(entries))
    except Exception:
        _log.debug("failed to load infrastate data_projections", exc_info=True)


def _git_text(*args: str) -> str:
    repo = _repo_root()
    if repo is None:
        return ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return (completed.stdout or "").strip()
    except Exception:
        return ""


def _build_meta() -> dict[str, Any]:
    active_manifest = active_slot_manifest() or {}
    return {
        "version": BUILD_INFO.version,
        "build_date": BUILD_INFO.build_date,
        "git_sha": _git_text("rev-parse", "HEAD"),
        "git_short_sha": _git_text("rev-parse", "--short", "HEAD"),
        "git_branch": _git_text("rev-parse", "--abbrev-ref", "HEAD"),
        "git_subject": _git_text("show", "-s", "--format=%s", "HEAD"),
        "repo_root": str(_repo_root() or ""),
        "runtime_version": str(active_manifest.get("target_version") or ""),
        "runtime_git_commit": str(active_manifest.get("git_commit") or ""),
        "runtime_git_short_commit": str(active_manifest.get("git_short_commit") or ""),
        "runtime_git_branch": str(active_manifest.get("git_branch") or active_manifest.get("target_rev") or ""),
        "runtime_git_subject": str(active_manifest.get("git_subject") or ""),
    }


def _ui_state() -> dict[str, Any]:
    raw = skill_memory_get(_UI_STATE_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _write_ui_state(**updates: Any) -> dict[str, Any]:
    payload = dict(_ui_state())
    payload.update(updates)
    skill_memory_set(_UI_STATE_KEY, payload)
    return payload


def _event_state() -> list[dict[str, Any]]:
    raw = skill_memory_get(_EVENTS_STATE_KEY, [])
    return raw if isinstance(raw, list) else []


def _append_event(event_type: str, payload: Any) -> list[dict[str, Any]]:
    item = {
        "id": f"{event_type}:{int(time.time() * 1000)}",
        "type": str(event_type or "event"),
        "ts": time.time(),
        "preview": json.dumps(payload, ensure_ascii=False)[:400] if isinstance(payload, (dict, list)) else str(payload or "")[:400],
        "payload": payload if isinstance(payload, (dict, list)) else {"value": str(payload or "")},
    }
    items = list(_event_state())
    items.append(item)
    items = items[-40:]
    skill_memory_set(_EVENTS_STATE_KEY, items)
    return items


def _self_base_url(conf) -> str:
    explicit = str(os.getenv("ADAOS_SELF_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    hub_url = str(getattr(conf, "hub_url", "") or "").strip()
    if hub_url.startswith("http://127.0.0.1:") or hub_url.startswith("http://localhost:"):
        return hub_url.rstrip("/")
    return "http://127.0.0.1:8777"


def _self_headers(conf) -> dict[str, str]:
    token = str(getattr(conf, "token", None) or os.getenv("ADAOS_TOKEN") or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = token
    return headers


def _post_local_admin(conf, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.post(
        _self_base_url(conf) + path,
        headers=_self_headers(conf),
        json=body or {},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"ok": True, "response": payload}


def _extract_action_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "action", "target"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nested_key in ("item", "selected"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                found = _extract_action_id(nested)
                if found:
                    return found
    return ""


def _status_log_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    command = str(status.get("command") or "").strip()
    if command:
        items.append(
            {
                "id": "command",
                "title": "command",
                "status": "idle",
                "preview": command[:400].strip(),
                "content": command,
            }
        )
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
    last_error = str(_ui_state().get("last_error") or "").strip()
    if last_error:
        items.append(
            {
                "id": "ui-error",
                "title": "ui-error",
                "status": "warn",
                "preview": last_error[-400:].strip(),
                "content": last_error[-4000:].strip(),
            }
        )
    return items


def _build_items(build: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "runtime_version",
            "title": "Runtime version",
            "status": "ok",
            "description": str(build.get("runtime_version") or build.get("version") or "unknown"),
            "subtitle": str(build.get("runtime_git_short_commit") or ""),
        },
        {
            "id": "runtime_head",
            "title": "Runtime commit",
            "status": "ok",
            "description": str(build.get("runtime_git_commit") or build.get("git_sha") or "unknown"),
            "subtitle": str(build.get("runtime_git_branch") or build.get("git_branch") or ""),
        },
        {
            "id": "runtime_subject",
            "title": "Runtime subject",
            "status": "idle",
            "description": str(build.get("runtime_git_subject") or build.get("git_subject") or "unknown"),
            "subtitle": str(build.get("runtime_git_short_commit") or build.get("git_short_sha") or ""),
        },
        {
            "id": "version",
            "title": "AdaOS version",
            "status": "ok",
            "description": str(build.get("version") or "unknown"),
            "subtitle": str(build.get("build_date") or ""),
        },
        {
            "id": "git_head",
            "title": "Source Git HEAD",
            "status": "idle",
            "description": str(build.get("git_short_sha") or "unknown"),
            "subtitle": str(build.get("git_subject") or build.get("git_branch") or ""),
        },
        {
            "id": "git_full_head",
            "title": "Source commit",
            "status": "idle",
            "description": str(build.get("git_sha") or "unknown"),
            "subtitle": str(build.get("git_branch") or ""),
        },
        {
            "id": "git_branch",
            "title": "Source branch",
            "status": "idle",
            "description": str(build.get("git_branch") or "unknown"),
            "subtitle": str(build.get("repo_root") or ""),
        },
    ]


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
                "subtitle": str(manifest.get("git_short_commit") or manifest.get("target_rev") or manifest.get("target_version") or "empty"),
                "description": str(meta.get("path") or ""),
                "version": str(manifest.get("target_version") or ""),
                "target_rev": str(manifest.get("target_rev") or ""),
                "git_commit": str(manifest.get("git_commit") or ""),
                "git_short_commit": str(manifest.get("git_short_commit") or ""),
                "git_branch": str(manifest.get("git_branch") or ""),
                "git_subject": str(manifest.get("git_subject") or ""),
                "badges": badges,
            }
        )
    return items


def _step_items(status: dict[str, Any], slots_payload: dict[str, Any], lifecycle: dict[str, Any], build: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(status.get("state") or "idle")
    phase = str(status.get("phase") or "")
    active = str(slots_payload.get("active_slot") or "--")
    previous = str(slots_payload.get("previous_slot") or "--")
    node_state = str(lifecycle.get("node_state") or "ready")
    return [
        {"id": "lifecycle", "title": "Lifecycle", "status": node_state, "description": str(lifecycle.get("reason") or "ready")},
        {"id": "update_state", "title": "Update state", "status": state, "description": phase or state},
        {"id": "active_slot", "title": "Active slot", "status": "ok", "description": active},
        {"id": "previous_slot", "title": "Previous slot", "status": "idle", "description": previous},
        {"id": "build", "title": "Build", "status": "ok", "description": str(build.get("version") or "unknown")},
        {"id": "commit", "title": "Runtime commit", "status": "idle", "description": str(build.get("runtime_git_short_commit") or build.get("git_short_sha") or "unknown")},
        {"id": "branch", "title": "Runtime branch", "status": "idle", "description": str(build.get("runtime_git_branch") or build.get("git_branch") or "unknown")},
        {"id": "target_rev", "title": "Target rev", "status": "idle", "description": str(status.get("target_rev") or "--")},
        {"id": "drain_timeout", "title": "Drain timeout", "status": "idle", "description": str(status.get("drain_timeout_sec") or "--")},
        {"id": "signal_delay", "title": "Signal delay", "status": "idle", "description": str(status.get("signal_delay_sec") or "--")},
        {"id": "command", "title": "Command", "status": "idle", "description": str(status.get("command") or "--")},
    ]


def _countdown_remaining_sec(status: dict[str, Any]) -> int:
    try:
        scheduled_for = float(status.get("scheduled_for") or 0.0)
    except Exception:
        scheduled_for = 0.0
    if scheduled_for <= 0:
        return 0
    return max(0, int(round(scheduled_for - time.time())))


def _summary_buttons(status: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(status.get("state") or "")
    remaining_sec = _countdown_remaining_sec(status)
    if state not in {"countdown", "draining", "stopping"}:
        return []
    if remaining_sec <= 0 and state == "countdown":
        return []
    label = "Cancel update"
    if remaining_sec > 0:
        label = f"{label} ({remaining_sec}s)"
    buttons = [{"id": "cancel_update", "label": label, "title": label, "kind": "danger"}]
    reason = str(status.get("reason") or "").strip().lower()
    if reason.startswith("github.push:") or reason.startswith("root.release:"):
        buttons.insert(0, {"id": "refuse_update", "label": "Refuse update", "title": "Refuse update", "kind": "danger"})
    return buttons


def _summary(status: dict[str, Any], slots_payload: dict[str, Any], lifecycle: dict[str, Any], conf, build: dict[str, Any], ui_state: dict[str, Any]) -> dict[str, Any]:
    active = str(slots_payload.get("active_slot") or "--")
    phase = str(status.get("phase") or "")
    state = str(status.get("state") or "idle")
    message = str(status.get("message") or lifecycle.get("reason") or "No update in progress")
    last_action = str(ui_state.get("last_action") or "").strip()
    last_action_at = float(ui_state.get("last_action_ts") or 0.0)
    if last_action:
        suffix = f" | action: {last_action}"
        if last_action_at:
            suffix += f" @ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_action_at))}"
        message += suffix
    return {
        "label": "Core update",
        "value": state,
        "subtitle": f"slot {active} | {build.get('runtime_git_short_commit') or build.get('git_short_sha') or build.get('version') or 'unknown'}",
        "description": message,
        "phase": phase,
        "role": str(getattr(conf, "role", "") or ""),
        "node_id": str(getattr(conf, "node_id", "") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "root_url": str(getattr(getattr(conf, "root_settings", None), "base_url", "") or ""),
        "updated_at": float(status.get("updated_at") or time.time()),
        "draining": bool(lifecycle.get("draining")),
        "version": str(build.get("version") or ""),
        "git_short_sha": str(build.get("git_short_sha") or ""),
        "runtime_git_commit": str(build.get("runtime_git_commit") or ""),
        "runtime_git_short_commit": str(build.get("runtime_git_short_commit") or ""),
        "runtime_git_branch": str(build.get("runtime_git_branch") or ""),
        "runtime_git_subject": str(build.get("runtime_git_subject") or ""),
        "target_rev": str(status.get("target_rev") or ""),
        "target_version": str(status.get("target_version") or ""),
        "reason": str(status.get("reason") or ""),
        "scheduled_for": float(status.get("scheduled_for") or 0.0),
        "countdown_remaining_sec": _countdown_remaining_sec(status),
        "drain_timeout_sec": float(status.get("drain_timeout_sec") or 0.0),
        "signal_delay_sec": float(status.get("signal_delay_sec") or 0.0),
        "buttons": _summary_buttons(status),
    }


def _action_items(status: dict[str, Any], ui_state: dict[str, Any]) -> list[dict[str, Any]]:
    last_refresh = float(ui_state.get("last_refresh_ts") or 0.0)
    last_action = str(ui_state.get("last_action") or "").strip()
    state = str(status.get("state") or "idle")
    items = [
        {
            "id": "start_update",
            "title": "Start update",
            "status": "ok" if state in {"idle", "failed", "succeeded", "cancelled", "rolled_back"} else "idle",
            "description": "Schedule core update with current target rev",
            "subtitle": last_action if last_action == "start_update" else "",
        },
        {
            "id": "refresh",
            "title": "Refresh snapshot",
            "status": "ok",
            "description": "Reload current local update state",
            "subtitle": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_refresh)) if last_refresh else "",
        },
        {
            "id": "cancel_update",
            "title": "Cancel update",
            "status": "warn" if state in {"countdown", "draining", "stopping"} else "idle",
            "description": "Cancel current countdown/update task",
            "subtitle": last_action if last_action == "cancel_update" else "",
        },
        {
            "id": "rollback",
            "title": "Rollback slot",
            "status": "warn",
            "description": "Switch back to previous active core slot",
            "subtitle": last_action if last_action == "rollback" else "",
        },
        {
            "id": "drain",
            "title": "Drain node",
            "status": "warn",
            "description": "Reject new tool calls and enter draining state",
            "subtitle": last_action if last_action == "drain" else "",
        },
    ]
    reason = str(status.get("reason") or "").strip().lower()
    if state in {"countdown", "draining", "stopping"} and (
        reason.startswith("github.push:") or reason.startswith("root.release:")
    ):
        items.insert(
            2,
            {
                "id": "refuse_update",
                "title": "Refuse update",
                "status": "warn",
                "description": "Decline root-requested update before restart begins",
                "subtitle": last_action if last_action == "refuse_update" else "",
            },
        )
    return items


def _perform_action(action_id: str, conf) -> dict[str, Any]:
    status = read_core_update_status()
    if action_id == "refresh":
        _write_ui_state(last_action="refresh", last_action_ts=time.time(), last_refresh_ts=time.time(), last_error="")
        return {"ok": True, "action": action_id}
    if action_id == "start_update":
        current_rev = str(status.get("target_rev") or os.getenv("ADAOS_REV") or "").strip()
        current_version = str(status.get("target_version") or BUILD_INFO.version or "").strip()
        result = _post_local_admin(
            conf,
            "/api/admin/update/start",
            {
                "reason": "infrastate.start_update",
                "countdown_sec": 60,
                "target_rev": current_rev,
                "target_version": current_version,
                "drain_timeout_sec": 10,
                "signal_delay_sec": 0.25,
            },
        )
    elif action_id in {"cancel_update", "refuse_update"}:
        result = _post_local_admin(conf, "/api/admin/update/cancel", {"reason": "infrastate.cancel"})
    elif action_id == "rollback":
        result = _post_local_admin(conf, "/api/admin/update/rollback", {"reason": "infrastate.rollback"})
    elif action_id == "drain":
        result = _post_local_admin(conf, "/api/admin/drain", {"reason": "infrastate.drain"})
    else:
        raise ValueError(f"unsupported infrastate action: {action_id}")
    _write_ui_state(
        last_action=action_id,
        last_action_ts=time.time(),
        last_refresh_ts=time.time(),
        last_result=result,
        last_error="",
    )
    return result


def _snapshot() -> dict[str, Any]:
    _ensure_skill_data_projections()
    conf = load_config()
    status = read_core_update_status()
    slots_payload = slot_status()
    lifecycle = runtime_lifecycle_snapshot()
    build = _build_meta()
    ui_state = _ui_state()
    report = _read_json(_base_dir() / "state" / "core_update" / "status.json") or {}
    snapshot = {
        "summary": _summary(status, slots_payload, lifecycle, conf, build, ui_state),
        "actions": _action_items(status, ui_state),
        "build": _build_items(build),
        "steps": _step_items(status, slots_payload, lifecycle, build),
        "slots": _slot_items(slots_payload),
        "logs": _status_log_items(report),
        "events": list(reversed(_event_state())),
        "status": status,
        "lifecycle": lifecycle,
        "build_meta": build,
        "ui_state": ui_state,
        "slots_meta": slots_payload,
        "last_refresh_ts": time.time(),
    }
    return snapshot


def _projection_webspace_ids(webspace_id: str | None = None) -> list[str]:
    ids: set[str] = set()
    token = str(webspace_id or "").strip()
    if token:
        ids.add(token)
    ids.add(default_webspace_id())
    try:
        for info in WebspaceService().list(mode="mixed"):
            slot = str(getattr(info, "id", "") or "").strip()
            if slot:
                ids.add(slot)
    except Exception:
        _log.debug("failed to enumerate webspaces for infrastate projection", exc_info=True)
    return sorted(ids)


def _project(snapshot: dict[str, Any], webspace_id: str | None = None) -> None:
    for target_ws in _projection_webspace_ids(webspace_id):
        ctx_subnet.set("infrastate.snapshot", snapshot, webspace_id=target_ws)


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
    _project(snapshot, webspace_id=webspace_id)
    return snapshot


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    _write_ui_state(last_refresh_ts=time.time())
    snapshot = _snapshot()
    _project(snapshot, webspace_id=webspace_id)
    return {"ok": True, **snapshot}


@subscribe("infrastate.refresh")
def on_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("infrastate.action")
def on_action(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    conf = load_config()
    action_id = _extract_action_id(payload)
    webspace_id = _webspace_id_from_payload(payload)
    try:
        if action_id:
            _perform_action(action_id, conf)
    except Exception as exc:
        _write_ui_state(last_action=action_id, last_action_ts=time.time(), last_error=str(exc))
        _log.warning("infrastate action failed: %s", action_id, exc_info=True)
    refresh_snapshot(webspace_id=webspace_id)


@subscribe("skills.activated")
def on_skill_activated(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, dict):
        return
    skill_name = str(payload.get("skill_name") or "")
    if skill_name and skill_name != "infrastate_skill":
        return
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
def on_webspace_reload(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("sys.ready")
@subscribe("subnet.nats.up")
@subscribe("subnet.stopping")
@subscribe("subnet.stopped")
@subscribe("core.update.status")
def on_runtime_event(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    try:
        event_type = str(getattr(evt, "type", "") or (payload.get("type") if isinstance(payload, dict) else "") or "runtime.event")
        _append_event(event_type, payload)
        refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))
    except Exception:
        _log.debug("failed to refresh infrastate snapshot from runtime event", exc_info=True)
