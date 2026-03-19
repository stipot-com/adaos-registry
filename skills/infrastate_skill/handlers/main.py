from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from adaos.build_info import BUILD_INFO
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet, skill_memory_get, skill_memory_set
from adaos.services.core_slots import slot_status
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.node_config import load_config
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot

_log = logging.getLogger("skills.infrastate_skill")
_UI_STATE_KEY = "infrastate.ui_state"


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


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


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
    return {
        "version": BUILD_INFO.version,
        "build_date": BUILD_INFO.build_date,
        "git_sha": _git_text("rev-parse", "HEAD"),
        "git_short_sha": _git_text("rev-parse", "--short", "HEAD"),
        "git_branch": _git_text("rev-parse", "--abbrev-ref", "HEAD"),
        "git_subject": _git_text("show", "-s", "--format=%s", "HEAD"),
        "repo_root": str(_repo_root() or ""),
    }


def _ui_state() -> dict[str, Any]:
    raw = skill_memory_get(_UI_STATE_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _write_ui_state(**updates: Any) -> dict[str, Any]:
    payload = dict(_ui_state())
    payload.update(updates)
    skill_memory_set(_UI_STATE_KEY, payload)
    return payload


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
            "id": "version",
            "title": "AdaOS version",
            "status": "ok",
            "description": str(build.get("version") or "unknown"),
            "subtitle": str(build.get("build_date") or ""),
        },
        {
            "id": "git_head",
            "title": "Git HEAD",
            "status": "idle",
            "description": str(build.get("git_short_sha") or "unknown"),
            "subtitle": str(build.get("git_subject") or build.get("git_branch") or ""),
        },
        {
            "id": "git_branch",
            "title": "Git branch",
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
                "subtitle": str(manifest.get("target_rev") or manifest.get("target_version") or "empty"),
                "description": str(meta.get("path") or ""),
                "version": str(manifest.get("target_version") or ""),
                "target_rev": str(manifest.get("target_rev") or ""),
                "badges": badges,
            }
        )
    return items


def _step_items(status: dict[str, Any], slots_payload: dict[str, Any], lifecycle: dict[str, Any], build: dict[str, Any]) -> list[dict[str, Any]]:
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
        {"id": "build", "title": "Build", "status": "ok", "description": str(build.get("version") or "unknown")},
        {"id": "commit", "title": "Commit", "status": "idle", "description": str(build.get("git_short_sha") or "unknown")},
    ]


def _summary(status: dict[str, Any], slots_payload: dict[str, Any], lifecycle: dict[str, Any], conf, build: dict[str, Any], ui_state: dict[str, Any]) -> dict[str, Any]:
    active = str(slots_payload.get("active_slot") or "—")
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
        "subtitle": f"slot {active} | {build.get('git_short_sha') or build.get('version') or 'unknown'}",
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
    }


def _action_items(status: dict[str, Any], ui_state: dict[str, Any]) -> list[dict[str, Any]]:
    last_refresh = float(ui_state.get("last_refresh_ts") or 0.0)
    last_action = str(ui_state.get("last_action") or "").strip()
    state = str(status.get("state") or "idle")
    return [
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


def _perform_action(action_id: str, conf) -> dict[str, Any]:
    if action_id == "refresh":
        _write_ui_state(last_action="refresh", last_action_ts=time.time(), last_refresh_ts=time.time(), last_error="")
        return {"ok": True, "action": action_id}
    if action_id == "cancel_update":
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
        "status": status,
        "lifecycle": lifecycle,
        "build_meta": build,
        "ui_state": ui_state,
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
