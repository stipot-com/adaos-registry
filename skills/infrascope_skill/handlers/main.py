from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.io import stream_publish
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services.operations.manager import get_operation_manager
from adaos.services.scenario.webspace_runtime import WebspaceService
from adaos.services.system_model.mappers import coerce_mapping
from adaos.services.system_model.model import CanonicalObject, CanonicalStatus
from adaos.services.system_model.service import (
    current_control_plane_objects,
    current_object_inspector,
    current_overview_projection,
)
from adaos.services.yjs.doc import try_read_live_map_value
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("skills.infrascope_skill")

_DEFAULT_TASK_GOAL = "assist operator in Infrascope"
_SCENARIO_ID = "infrascope"
_OVERVIEW_SECTIONS = (
    "health_strip",
    "active_incidents",
    "quota_summary",
    "active_runtimes",
    "recent_changes",
)
_INVENTORY_KINDS = (
    "all",
    "hubs",
    "members",
    "browsers",
    "devices",
    "skills",
    "scenarios",
    "runtimes",
    "quotas",
)

_ICON_BY_KIND = {
    "root": "cloud-outline",
    "hub": "server-outline",
    "member": "git-branch-outline",
    "browser_session": "globe-outline",
    "device": "phone-portrait-outline",
    "skill": "extension-puzzle-outline",
    "scenario": "layers-outline",
    "runtime": "pulse-outline",
    "quota": "speedometer-outline",
    "workspace": "desktop-outline",
    "profile": "person-outline",
    "connection": "swap-horizontal-outline",
    "capacity": "bar-chart-outline",
    "io_endpoint": "radio-outline",
}

_KIND_ALIASES = {
    "all": set(),
    "hubs": {"hub"},
    "members": {"member"},
    "browsers": {"browser_session"},
    "devices": {"device"},
    "skills": {"skill"},
    "scenarios": {"scenario"},
    "runtimes": {"runtime"},
    "quotas": {"quota"},
}

_background_refresh_task: asyncio.Task[None] | None = None
_background_refresh_pending_all = False
_background_refresh_webspace_ids: set[str] = set()
_background_refresh_reason = ""
_last_projected_fingerprints: dict[str, str] = {}
_last_good_snapshots: dict[str, dict[str, Any]] = {}
_last_refresh_at_mono = 0.0


def _refresh_debounce_s() -> float:
    try:
        raw = str(os.getenv("INFRASCOPE_REFRESH_DEBOUNCE_S", "") or "").strip()
        if raw:
            value = float(raw)
            return max(0.0, min(value, 10.0))
    except Exception:
        pass
    return 0.25


def _event_webspace_fallback() -> str:
    # Many runtime events do not carry a webspace_id; refreshing "all webspaces"
    # for each such event can become very expensive and cause CPU spikes.
    raw = str(os.getenv("INFRASCOPE_EVENT_WEBSPACE", "") or "").strip()
    return raw or default_webspace_id()


def lang_res() -> dict[str, str]:
    return {}


def _status_token(value: Any) -> str:
    if isinstance(value, CanonicalStatus):
        return value.value
    token = str(value or "").strip().lower()
    return token or CanonicalStatus.UNKNOWN.value


def _status_rank(value: Any) -> int:
    token = _status_token(value)
    order = {
        CanonicalStatus.OFFLINE.value: 5,
        CanonicalStatus.DEGRADED.value: 4,
        CanonicalStatus.WARNING.value: 3,
        CanonicalStatus.UNKNOWN.value: 2,
        CanonicalStatus.ONLINE.value: 1,
    }
    return int(order.get(token) or 0)


def _icon_for_kind(kind: Any) -> str:
    token = str(kind or "").strip().lower()
    return str(_ICON_BY_KIND.get(token) or "ellipse-outline")


def _inventory_kind_token(value: Any) -> str:
    token = str(value or "all").strip().lower() or "all"
    return token if token in _KIND_ALIASES else "all"


def _matches_inventory_kind(obj: CanonicalObject, requested: str) -> bool:
    if requested == "all":
        return True
    return str(getattr(obj, "kind", "") or "").strip().lower() in _KIND_ALIASES.get(requested, set())


def _projection_object_index(projection: Any) -> dict[str, CanonicalObject]:
    out: dict[str, CanonicalObject] = {}
    subject = getattr(projection, "subject", None)
    subject_id = str(getattr(subject, "id", "") or "").strip()
    if subject_id:
        out[subject_id] = subject
    for item in list(getattr(projection, "objects", []) or []):
        item_id = str(getattr(item, "id", "") or "").strip()
        if item_id:
            out[item_id] = item
    return out


def _decorate_row(
    raw: Any,
    *,
    object_index: dict[str, CanonicalObject] | None = None,
    fallback_icon: str | None = None,
) -> dict[str, Any]:
    item = coerce_mapping(raw)
    object_index = object_index or {}
    object_id = str(item.get("object_id") or item.get("id") or "").strip()
    obj = object_index.get(object_id)
    title = str(item.get("title") or (getattr(obj, "title", None) if obj else "") or object_id or "Item").strip() or "Item"
    subtitle = str(item.get("subtitle") or item.get("summary") or "").strip()
    kind = str(item.get("kind") or (getattr(obj, "kind", None) if obj else "") or "").strip()
    status = _status_token(item.get("status") or (getattr(obj, "status", None) if obj else None))
    details = item.get("details")
    if details is None and obj is not None:
        details = obj.to_dict()
    return {
        **item,
        "id": str(item.get("id") or object_id or title),
        "object_id": object_id or (str(getattr(obj, "id", "") or "").strip() if obj else ""),
        "object_title": str(item.get("object_title") or title),
        "title": title,
        "subtitle": subtitle,
        "kind": kind,
        "status": status,
        "icon": str(item.get("icon") or fallback_icon or _icon_for_kind(kind)),
        "details": details if details is not None else item,
    }


def _incident_rows(items: list[Any], *, object_index: dict[str, CanonicalObject]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in items:
        item = coerce_mapping(raw)
        object_id = str(item.get("object_id") or "").strip()
        obj = object_index.get(object_id)
        severity = str(item.get("severity") or "medium").strip().lower()
        status = _status_token(item.get("status") or (getattr(obj, "status", None) if obj else None))
        title = str(item.get("title") or (getattr(obj, "title", None) if obj else "") or object_id or "Incident").strip() or "Incident"
        out.append(
            {
                "id": str(item.get("id") or f"incident:{object_id or title}"),
                "object_id": object_id,
                "object_title": str(getattr(obj, "title", "") or title),
                "title": title,
                "subtitle": f"{severity} | {status}",
                "summary": str(item.get("summary") or "").strip(),
                "severity": severity,
                "status": status,
                "icon": _icon_for_kind(getattr(obj, "kind", None) if obj else ""),
                "details": {
                    "incident": item,
                    "object": obj.to_dict() if obj is not None else {},
                },
            }
        )
    return out


def _sorted_object_rows(items: list[CanonicalObject]) -> list[dict[str, Any]]:
    ordered = sorted(
        items,
        key=lambda item: (
            _status_rank(getattr(item, "status", None)),
            str(getattr(item, "kind", "") or ""),
            str(getattr(item, "title", "") or ""),
        ),
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    for obj in ordered:
        summary = str(getattr(obj, "summary", None) or "").strip()
        rows.append(
            {
                "id": obj.id,
                "object_id": obj.id,
                "object_title": obj.title,
                "title": obj.title,
                "subtitle": " | ".join(bit for bit in [obj.kind, _status_token(obj.status), summary] if bit),
                "kind": obj.kind,
                "status": _status_token(obj.status),
                "icon": _icon_for_kind(obj.kind),
                "details": obj.to_dict(),
            }
        )
    return rows


def _fallback_local_inventory_row(*, webspace_id: str | None = None) -> dict[str, Any]:
    token = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    return {
        "id": "local",
        "object_id": "local",
        "object_title": "Local node",
        "title": "Local node",
        "subtitle": token,
        "kind": "hub",
        "status": CanonicalStatus.UNKNOWN.value,
        "icon": _icon_for_kind("hub"),
        "details": {
            "id": "local",
            "kind": "hub",
            "title": "Local node",
            "status": CanonicalStatus.UNKNOWN.value,
        },
    }


def _operation_status_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"running", "waiting_input", "accepted", "queued"}:
        return CanonicalStatus.WARNING.value
    if token in {"succeeded", "completed", "validated"}:
        return CanonicalStatus.ONLINE.value
    if token in {"failed", "cancelled", "aborted"}:
        return CanonicalStatus.OFFLINE.value
    return CanonicalStatus.UNKNOWN.value


def _operations_snapshot(*, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        return get_operation_manager().snapshot(webspace_id=webspace_id)
    except Exception:
        return {"by_id": {}, "order": [], "active": [], "active_items": [], "notifications": []}


def _active_operation_rows(*, webspace_id: str | None = None) -> list[dict[str, Any]]:
    operations = _operations_snapshot(webspace_id=webspace_id)
    rows: list[dict[str, Any]] = []
    for raw in list(operations.get("active_items") or []):
        item = raw if isinstance(raw, dict) else {}
        target_id = str(item.get("target_id") or item.get("operation_id") or "operation").strip() or "operation"
        message = str(item.get("message") or item.get("current_step") or "").strip()
        status = _operation_status_token(item.get("status"))
        subtitle_bits = [
            bit
            for bit in [str(item.get("status") or "").strip().lower(), message]
            if bit
        ]
        rows.append(
            {
                "id": str(item.get("operation_id") or target_id),
                "title": target_id,
                "subtitle": " | ".join(subtitle_bits),
                "status": status,
                "icon": "swap-horizontal-outline",
                "details": item,
            }
        )
    return rows


def _skill_runtime_migration_report() -> dict[str, Any]:
    status = read_core_update_status()
    last_result = read_core_update_last_result() or {}
    for candidate in (status, last_result):
        if not isinstance(candidate, dict):
            continue
        report = candidate.get("skill_runtime_migration")
        if isinstance(report, dict) and report:
            return dict(report)
        manifest = candidate.get("manifest")
        if not isinstance(manifest, dict):
            continue
        report = manifest.get("skill_runtime_migration")
        if isinstance(report, dict) and report:
            return dict(report)
    return {}


def _skill_runtime_rollback_report() -> dict[str, Any]:
    status = read_core_update_status()
    last_result = read_core_update_last_result() or {}
    for candidate in (status, last_result):
        if not isinstance(candidate, dict):
            continue
        report = candidate.get("skill_runtime_rollback")
        if isinstance(report, dict) and report:
            return dict(report)
    return {}


def _skill_post_commit_checks_report() -> dict[str, Any]:
    status = read_core_update_status()
    last_result = read_core_update_last_result() or {}
    for candidate in (status, last_result):
        if not isinstance(candidate, dict):
            continue
        report = candidate.get("skill_post_commit_checks")
        if isinstance(report, dict) and report:
            return dict(report)
    return {}


def _skill_migration_operation_rows() -> list[dict[str, Any]]:
    report = _skill_runtime_migration_report()
    if not report:
        return []
    total = int(report.get("total") or 0)
    failed_total = int(report.get("failed_total") or 0)
    rollback_total = int(report.get("rollback_total") or 0)
    deactivated_total = int(report.get("deactivated_total") or 0)
    if total <= 0:
        return []
    failed = []
    for item in report.get("skills") or []:
        if not isinstance(item, dict) or bool(item.get("ok")):
            continue
        failed.append(f"{str(item.get('skill') or 'skill')}:{str(item.get('failed_stage') or 'failed')}")
    if failed_total <= 0 and rollback_total <= 0 and deactivated_total <= 0:
        return []
    subtitle_bits = [f"{total - failed_total}/{total} migrated"]
    if failed:
        subtitle_bits.append(", ".join(failed[:3]) + (f" (+{len(failed) - 3})" if len(failed) > 3 else ""))
    if rollback_total:
        subtitle_bits.append(f"rollback={rollback_total}")
    if deactivated_total:
        subtitle_bits.append(f"deactivated={deactivated_total}")
    return [
        {
            "id": "core-update-skill-runtime-migration",
            "title": "core.update.skill_runtime_migration",
            "subtitle": " | ".join(subtitle_bits),
            "status": CanonicalStatus.OFFLINE.value if failed_total else CanonicalStatus.WARNING.value,
            "icon": "warning-outline",
            "details": report,
        }
    ]


def _skill_rollback_operation_rows() -> list[dict[str, Any]]:
    report = _skill_runtime_rollback_report()
    if not report:
        return []
    total = int(report.get("total") or 0)
    failed_total = int(report.get("failed_total") or 0)
    rollback_total = int(report.get("rollback_total") or 0)
    skipped_total = int(report.get("skipped_total") or 0)
    if total <= 0:
        return []
    failed = []
    for item in report.get("skills") or []:
        if not isinstance(item, dict) or bool(item.get("ok")):
            continue
        failed.append(str(item.get("skill") or "skill"))
    if failed_total <= 0 and rollback_total <= 0:
        return []
    subtitle_bits = [f"{rollback_total}/{total} rolled back"]
    if failed:
        subtitle_bits.append(", ".join(failed[:3]) + (f" (+{len(failed) - 3})" if len(failed) > 3 else ""))
    if skipped_total:
        subtitle_bits.append(f"skipped={skipped_total}")
    return [
        {
            "id": "core-update-skill-runtime-rollback",
            "title": "core.update.skill_runtime_rollback",
            "subtitle": " | ".join(subtitle_bits),
            "status": CanonicalStatus.OFFLINE.value if failed_total else CanonicalStatus.WARNING.value,
            "icon": "refresh-outline",
            "details": report,
        }
    ]


def _skill_post_commit_operation_rows() -> list[dict[str, Any]]:
    report = _skill_post_commit_checks_report()
    if not report:
        return []
    total = int(report.get("total") or 0)
    failed_total = int(report.get("failed_total") or 0)
    deactivated_total = int(report.get("deactivated_total") or 0)
    error_text = str(report.get("error") or "").strip()
    if total <= 0 and not error_text:
        return []
    failed = []
    for item in report.get("skills") or []:
        if not isinstance(item, dict) or bool(item.get("ok")):
            continue
        failed.append(f"{str(item.get('skill') or 'skill')}:{str(item.get('failed_stage') or 'failed')}")
    subtitle_bits = []
    if total > 0:
        subtitle_bits.append(f"{total - failed_total}/{total} passed")
    if failed:
        subtitle_bits.append(", ".join(failed[:3]) + (f" (+{len(failed) - 3})" if len(failed) > 3 else ""))
    if deactivated_total:
        subtitle_bits.append(f"deactivated={deactivated_total}")
    if error_text:
        subtitle_bits.append(error_text)
    return [
        {
            "id": "core-update-skill-post-commit-checks",
            "title": "core.update.skill_post_commit_checks",
            "subtitle": " | ".join(subtitle_bits),
            "status": CanonicalStatus.OFFLINE.value if failed_total or error_text else CanonicalStatus.WARNING.value,
            "icon": "shield-half-outline",
            "details": report,
        }
    ]


def _skill_manifest_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        for name in ("skill.yaml", "resolved.manifest.json"):
            candidate = parent / name
            if candidate.exists():
                return candidate
    return None


def _load_manifest_payload(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        _log.debug("failed to load infrascope manifest path=%s", path, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        existing = ctx.projections.resolve("subnet", "infrascope.snapshot")
        if existing:
            return
        manifest_path = _skill_manifest_path()
        if manifest_path is None:
            return
        payload = _load_manifest_payload(manifest_path)
        entries = payload.get("data_projections") or []
        if not isinstance(entries, list) or not entries:
            return
        ctx.projections.load_entries(entries)
        _log.debug("loaded infrascope data_projections path=%s entries=%d", manifest_path, len(entries))
    except Exception:
        _log.debug("failed to load infrascope data_projections", exc_info=True)


def _projection_webspace_ids(webspace_id: str | None = None) -> list[str]:
    ids: set[str] = set()
    token = str(webspace_id or "").strip()
    if token:
        ids.add(token)
    ids.add(default_webspace_id())
    try:
        for info in WebspaceService().list(mode="mixed"):
            slot = str(getattr(info, "id", "") or getattr(info, "webspace_id", "") or "").strip()
            if slot:
                ids.add(slot)
    except Exception:
        _log.debug("failed to enumerate webspaces for infrascope projection", exc_info=True)
    return sorted(ids)


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


def _json_fingerprint(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _compact_snapshot_for_yjs(snapshot: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "summary": coerce_mapping(snapshot.get("summary")),
        "overview": coerce_mapping(snapshot.get("overview")),
        "inspectors": coerce_mapping(snapshot.get("inspectors")),
        "meta": coerce_mapping(snapshot.get("meta")),
    }
    errors = list(snapshot.get("errors") or [])
    if errors:
        compact["errors"] = errors
    return compact


def _inventory_receiver(kind: str) -> str:
    token = _inventory_kind_token(kind)
    return f"infrascope.inventory.{token}"


def _operations_receiver() -> str:
    return "infrascope.operations.active"


def _inspector_receiver(object_id: str) -> str:
    token = str(object_id or "local").strip() or "local"
    return f"infrascope.inspector.{token}"


def _publish_stream_payload(receiver: str, data: Any, *, webspace_id: str) -> None:
    stream_publish(
        receiver,
        data,
        _meta={"webspace_id": str(webspace_id or "").strip() or default_webspace_id()},
    )


def _stream_payload_for_receiver(snapshot: dict[str, Any], receiver: str) -> Any:
    token = str(receiver or "").strip()
    if not token:
        return None
    if token == _operations_receiver():
        operations = coerce_mapping(snapshot.get("operations"))
        return list(operations.get("items") or [])
    inventory_prefix = "infrascope.inventory."
    if token.startswith(inventory_prefix):
        kind = _inventory_kind_token(token[len(inventory_prefix):])
        inventory = coerce_mapping(snapshot.get("inventory"))
        return list(inventory.get(kind) or [])
    inspector_prefix = "infrascope.inspector."
    if token.startswith(inspector_prefix):
        object_id = str(token[len(inspector_prefix):] or "local").strip() or "local"
        inspectors = coerce_mapping(snapshot.get("inspectors"))
        payload = inspectors.get(object_id)
        if payload is None and object_id != "local":
            payload = inspectors.get("local")
        return coerce_mapping(payload)
    return None


def _publish_snapshot_streams(snapshot: dict[str, Any], *, webspace_id: str) -> None:
    for kind in _INVENTORY_KINDS:
        _publish_stream_payload(
            _inventory_receiver(kind),
            _stream_payload_for_receiver(snapshot, _inventory_receiver(kind)),
            webspace_id=webspace_id,
        )
    _publish_stream_payload(
        _operations_receiver(),
        _stream_payload_for_receiver(snapshot, _operations_receiver()),
        webspace_id=webspace_id,
    )
    inspectors = coerce_mapping(snapshot.get("inspectors"))
    for object_id in inspectors.keys():
        token = str(object_id or "").strip()
        if not token:
            continue
        _publish_stream_payload(
            _inspector_receiver(token),
            _stream_payload_for_receiver(snapshot, _inspector_receiver(token)),
            webspace_id=webspace_id,
        )


def _project_snapshot(snapshot: dict[str, Any], *, webspace_id: str) -> bool:
    compact = _compact_snapshot_for_yjs(snapshot)
    fingerprint = _json_fingerprint(compact)
    if _last_projected_fingerprints.get(webspace_id) == fingerprint:
        return False
    ctx_subnet.set("infrascope.snapshot", compact, webspace_id=webspace_id)
    _last_projected_fingerprints[webspace_id] = fingerprint
    return True


def _snapshot_cache_key(*, webspace_id: str | None = None) -> str:
    return str(webspace_id or default_webspace_id()).strip() or default_webspace_id()


def _remember_good_snapshot(snapshot: dict[str, Any], *, webspace_id: str | None = None) -> None:
    _last_good_snapshots[_snapshot_cache_key(webspace_id=webspace_id)] = deepcopy(snapshot)


def _record_snapshot_error(errors: list[str], section: str, exc: Exception) -> None:
    errors.append(f"{section}: {type(exc).__name__}: {exc}")


def _summary_warning_fallback(*, webspace_id: str | None = None, errors: list[str]) -> dict[str, Any]:
    token = _snapshot_cache_key(webspace_id=webspace_id)
    warning = errors[0] if errors else "snapshot section unavailable"
    return {
        "label": "Infrascope",
        "value": CanonicalStatus.WARNING.value,
        "subtitle": token,
        "description": "Infrascope is showing the latest available control-plane data.",
        "warning": warning,
        "buttons": [
            {
                "id": "inspect_local",
                "label": "Inspect Local node",
                "object_id": "local",
                "object_title": "Local node",
            }
        ],
        "object_id": "local",
        "object_title": "Local node",
    }


def _snapshot_from_cache(cache_key: str, *, task_goal: str | None, error_text: str) -> dict[str, Any] | None:
    cached = _last_good_snapshots.get(cache_key)
    if not isinstance(cached, dict):
        return None
    snapshot = deepcopy(cached)
    meta = coerce_mapping(snapshot.get("meta"))
    meta["task_goal"] = str(task_goal or meta.get("task_goal") or _DEFAULT_TASK_GOAL).strip() or _DEFAULT_TASK_GOAL
    meta["webspace_id"] = str(meta.get("webspace_id") or cache_key).strip() or cache_key
    meta["stale"] = True
    meta["stale_reason"] = error_text
    snapshot["meta"] = meta
    summary = coerce_mapping(snapshot.get("summary"))
    summary.setdefault("label", "Infrascope")
    summary.setdefault("object_id", "local")
    summary.setdefault("object_title", "Local node")
    summary["warning"] = error_text
    summary.setdefault("description", "Infrascope is showing the latest available control-plane data.")
    snapshot["summary"] = summary
    errors = list(snapshot.get("errors") or [])
    if error_text not in errors:
        errors.append(error_text)
    snapshot["errors"] = errors
    return snapshot


def _object_ids(*, webspace_id: str | None = None) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for item in current_control_plane_objects(webspace_id=webspace_id):
        object_id = str(getattr(item, "id", "") or "").strip()
        if object_id and object_id not in seen:
            seen.add(object_id)
            ids.append(object_id)
    return sorted(ids)


def _fallback_inspector(object_id: str, *, warning: str) -> dict[str, Any]:
    return {
        "label": "object",
        "value": CanonicalStatus.UNKNOWN.value,
        "subtitle": object_id,
        "description": warning,
        "warning": warning,
        "object": {},
        "incidents": [],
        "actions": [],
        "recent_changes": [],
        "topology": {"edges": []},
        "task_packet": {},
        "subnet_planning": {},
        "object_id": object_id,
        "object_title": object_id,
    }


def _snapshot(*, webspace_id: str | None = None, task_goal: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    try:
        summary = get_overview_summary(webspace_id=webspace_id)
    except Exception as exc:
        _record_snapshot_error(errors, "summary", exc)
        summary = _summary_warning_fallback(webspace_id=webspace_id, errors=errors)
    goal = str(task_goal or _DEFAULT_TASK_GOAL).strip() or _DEFAULT_TASK_GOAL

    overview: dict[str, list[dict[str, Any]]] = {}
    for section in _OVERVIEW_SECTIONS:
        try:
            overview[section] = list_overview_collection(section, webspace_id=webspace_id)
        except Exception as exc:
            _record_snapshot_error(errors, f"overview:{section}", exc)
            overview[section] = []

    inventory: dict[str, list[dict[str, Any]]] = {}
    for kind in _INVENTORY_KINDS:
        try:
            inventory[kind] = list_inventory(kind, webspace_id=webspace_id)
        except Exception as exc:
            _record_snapshot_error(errors, f"inventory:{kind}", exc)
            inventory[kind] = []
    if not any(inventory.get(kind) for kind in _INVENTORY_KINDS):
        fallback_row = _fallback_local_inventory_row(webspace_id=webspace_id)
        inventory["all"] = [dict(fallback_row)]
        inventory["hubs"] = [dict(fallback_row)]

    inspectors: dict[str, dict[str, Any]] = {}
    try:
        local_inspector = get_object_inspector("local", task_goal=goal, webspace_id=webspace_id)
    except Exception as exc:
        _record_snapshot_error(errors, "inspector:local", exc)
        local_inspector = _fallback_inspector("local", warning=f"{type(exc).__name__}: {exc}")
    local_actual_id = str(local_inspector.get("object_id") or summary.get("object_id") or "local").strip() or "local"
    inspectors["local"] = dict(local_inspector)
    inspectors[local_actual_id] = dict(local_inspector)
    try:
        object_ids = _object_ids(webspace_id=webspace_id)
    except Exception as exc:
        _record_snapshot_error(errors, "object_ids", exc)
        object_ids = []
    for object_id in object_ids:
        if object_id in inspectors:
            continue
        try:
            inspectors[object_id] = get_object_inspector(object_id, task_goal=goal, webspace_id=webspace_id)
        except Exception as exc:
            _record_snapshot_error(errors, f"inspector:{object_id}", exc)
            inspectors[object_id] = _fallback_inspector(object_id, warning=f"{type(exc).__name__}: {exc}")

    operations = _operations_snapshot(webspace_id=webspace_id)
    operation_rows = (
        _active_operation_rows(webspace_id=webspace_id)
        + _skill_migration_operation_rows()
        + _skill_rollback_operation_rows()
        + _skill_post_commit_operation_rows()
    )
    snapshot = {
        "summary": summary,
        "overview": {
            **overview,
            "active_operations": operation_rows,
        },
        "inventory": inventory,
        "inspectors": inspectors,
        "operations": {
            "items": operation_rows,
            "active": list(operations.get("active") or []),
        },
        "meta": {
            "task_goal": goal,
            "webspace_id": str(webspace_id or "").strip() or default_webspace_id(),
            "object_total": len(inspectors),
            "partial": bool(errors),
            "error_total": len(errors),
        },
    }
    if errors:
        snapshot["errors"] = errors
        snapshot["summary"] = {
            **coerce_mapping(snapshot.get("summary")),
            "warning": errors[0],
        }
    return snapshot


def _fallback_snapshot(exc: Exception, *, webspace_id: str | None = None, task_goal: str | None = None) -> dict[str, Any]:
    error_text = f"{type(exc).__name__}: {exc}"
    goal = str(task_goal or _DEFAULT_TASK_GOAL).strip() or _DEFAULT_TASK_GOAL
    empty_inventory = {kind: [] for kind in _INVENTORY_KINDS}
    fallback_row = _fallback_local_inventory_row(webspace_id=webspace_id)
    empty_inventory["all"] = [dict(fallback_row)]
    empty_inventory["hubs"] = [dict(fallback_row)]
    return {
        "summary": {
            "label": "Infrascope",
            "value": CanonicalStatus.DEGRADED.value,
            "subtitle": str(webspace_id or default_webspace_id()).strip() or default_webspace_id(),
            "description": "Infrascope is waiting for a stable control-plane snapshot.",
            "warning": error_text,
            "buttons": [],
            "object_id": "local",
            "object_title": "Local node",
        },
        "overview": {
            **{section: [] for section in _OVERVIEW_SECTIONS},
            "active_operations": [],
        },
        "inventory": empty_inventory,
        "inspectors": {
            "local": _fallback_inspector("local", warning=error_text),
        },
        "operations": {"items": [], "active": []},
        "meta": {
            "task_goal": goal,
            "webspace_id": str(webspace_id or default_webspace_id()).strip() or default_webspace_id(),
            "object_total": 1,
        },
        "errors": [error_text],
    }


def _snapshot_or_fallback(*, webspace_id: str | None = None, task_goal: str | None = None) -> dict[str, Any]:
    cache_key = _snapshot_cache_key(webspace_id=webspace_id)
    try:
        snapshot = _snapshot(webspace_id=webspace_id, task_goal=task_goal)
        _remember_good_snapshot(snapshot, webspace_id=webspace_id)
        return snapshot
    except Exception as exc:
        _log.warning("infrascope snapshot failed; projecting fallback snapshot", exc_info=True)
        error_text = f"{type(exc).__name__}: {exc}"
        cached = _snapshot_from_cache(cache_key, task_goal=task_goal, error_text=error_text)
        if cached is not None:
            return cached
        return _fallback_snapshot(exc, webspace_id=webspace_id, task_goal=task_goal)


def _set_refresh_pending(*, webspace_id: str | None = None, reason: str) -> None:
    global _background_refresh_pending_all
    global _background_refresh_reason
    token = str(webspace_id or "").strip() or _event_webspace_fallback()
    if token:
        _background_refresh_webspace_ids.add(token)
    _background_refresh_reason = str(reason or "runtime.event")


def _refresh_projection_targets(*, webspace_id: str | None = None) -> list[str]:
    token = str(webspace_id or "").strip()
    if token:
        return [token]
    return _projection_webspace_ids()


def _is_infrascope_live_in_webspace(webspace_id: str) -> bool | None:
    live_hit, raw_current = try_read_live_map_value(webspace_id, "ui", "current_scenario")
    if not live_hit:
        return None
    return str(raw_current or "").strip() == _SCENARIO_ID


async def _is_infrascope_active_webspace(webspace_id: str) -> bool:
    token = str(webspace_id or "").strip() or default_webspace_id()
    live_match = _is_infrascope_live_in_webspace(token)
    return bool(live_match)


def _is_infrascope_active_webspace_sync(webspace_id: str) -> bool:
    token = str(webspace_id or "").strip() or default_webspace_id()
    live_match = _is_infrascope_live_in_webspace(token)
    return bool(live_match)


async def _filter_active_targets(targets: list[str]) -> list[str]:
    active: list[str] = []
    for target_ws in targets:
        if await _is_infrascope_active_webspace(target_ws):
            active.append(target_ws)
    return active


def _refresh_snapshot_targets(targets: list[str]) -> dict[str, Any]:
    _ensure_skill_data_projections()
    projected = 0
    last_snapshot: dict[str, Any] | None = None
    for target_ws in targets:
        snapshot = _snapshot_or_fallback(webspace_id=target_ws)
        last_snapshot = snapshot
        if _project_snapshot(snapshot, webspace_id=target_ws):
            projected += 1
        _publish_snapshot_streams(snapshot, webspace_id=target_ws)
    return {
        "ok": True,
        "projected": projected,
        "webspaces": targets,
        "snapshot": last_snapshot or {},
    }


async def _background_refresh_worker() -> None:
    global _background_refresh_task
    global _background_refresh_pending_all
    global _background_refresh_reason
    global _last_refresh_at_mono
    try:
        while True:
            pending_all = _background_refresh_pending_all
            pending_ids = sorted(_background_refresh_webspace_ids)
            reason = _background_refresh_reason or "runtime.event"
            _background_refresh_pending_all = False
            _background_refresh_webspace_ids.clear()
            _background_refresh_reason = ""
            targets = _refresh_projection_targets() if pending_all or not pending_ids else _refresh_projection_targets(webspace_id=pending_ids[0]) if len(pending_ids) == 1 else pending_ids
            try:
                debounce_s = _refresh_debounce_s()
                if debounce_s > 0:
                    now = time.monotonic()
                    wait_s = debounce_s - (now - float(_last_refresh_at_mono or 0.0))
                    if wait_s > 0:
                        await asyncio.sleep(wait_s)
                _last_refresh_at_mono = time.monotonic()
                targets = await _filter_active_targets(targets)
                if not targets:
                    continue
                await asyncio.to_thread(_refresh_snapshot_targets, targets)
            except (asyncio.CancelledError, RuntimeError) as exc:
                if isinstance(exc, RuntimeError) and "Executor shutdown has been called" not in str(exc):
                    raise
                break
            except Exception:
                _log.warning("background infrascope refresh failed reason=%s", reason, exc_info=True)
            await asyncio.sleep(0)
            if not _background_refresh_pending_all and not _background_refresh_webspace_ids:
                break
    finally:
        _background_refresh_task = None
        if _background_refresh_pending_all or _background_refresh_webspace_ids:
            _schedule_snapshot_refresh(reason="background.refresh.retry")


def _schedule_snapshot_refresh(*, webspace_id: str | None = None, reason: str = "runtime.event") -> None:
    global _background_refresh_task
    global _background_refresh_pending_all
    global _background_refresh_reason
    global _last_refresh_at_mono
    _set_refresh_pending(webspace_id=webspace_id, reason=reason)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.debug("no running loop for infrascope refresh; running inline reason=%s", reason)
        target_ws = str(webspace_id or "").strip() or _event_webspace_fallback()
        if not _is_infrascope_active_webspace_sync(target_ws):
            return
        debounce_s = _refresh_debounce_s()
        if debounce_s > 0:
            now = time.monotonic()
            if now - float(_last_refresh_at_mono or 0.0) < debounce_s:
                return
            _last_refresh_at_mono = now
        _refresh_snapshot_targets(_refresh_projection_targets(webspace_id=target_ws))
        _background_refresh_pending_all = False
        _background_refresh_reason = ""
        _background_refresh_webspace_ids.clear()
        return
    if _background_refresh_task is not None and not _background_refresh_task.done():
        return
    _background_refresh_task = loop.create_task(
        _background_refresh_worker(),
        name="infrascope-background-refresh",
    )


@tool("get_overview_summary")
def get_overview_summary(webspace_id: str | None = None) -> dict[str, Any]:
    projection = current_overview_projection(webspace_id=webspace_id)
    context = coerce_mapping(getattr(projection, "context", {}))
    summary_tile = coerce_mapping(context.get("summary_tile"))
    subject = getattr(projection, "subject", None)
    subject_id = str(getattr(subject, "id", "") or "").strip()
    subject_title = str(getattr(subject, "title", "") or subject_id).strip() or "local object"
    buttons = list(summary_tile.get("buttons") or [])
    buttons.append(
        {
            "id": "inspect_local",
            "label": f"Inspect {subject_title}",
            "object_id": subject_id,
            "object_title": subject_title,
        }
    )
    return {
        **summary_tile,
        "buttons": buttons,
        "object_id": subject_id,
        "object_title": subject_title,
    }


@tool("list_overview_collection")
def list_overview_collection(section: str, webspace_id: str | None = None) -> list[dict[str, Any]]:
    projection = current_overview_projection(webspace_id=webspace_id)
    context = coerce_mapping(getattr(projection, "context", {}))
    object_index = _projection_object_index(projection)
    items = list(context.get(section) or [])
    if section == "active_incidents":
        return _incident_rows(items, object_index=object_index)
    return [_decorate_row(item, object_index=object_index) for item in items if isinstance(item, dict)]


@tool("list_inventory")
def list_inventory(kind: str = "all", webspace_id: str | None = None) -> list[dict[str, Any]]:
    requested = _inventory_kind_token(kind)
    try:
        objects = [
            item
            for item in current_control_plane_objects(webspace_id=webspace_id)
            if _matches_inventory_kind(item, requested)
        ]
    except Exception:
        objects = []
    rows = _sorted_object_rows(objects)
    if rows:
        return rows
    if requested in {"all", "hubs"}:
        return [_fallback_local_inventory_row(webspace_id=webspace_id)]
    return []


@tool("get_object_inspector")
def get_object_inspector(
    object_id: str | None = None,
    task_goal: str | None = None,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    token = str(object_id or "").strip() or "local"
    try:
        projection = current_object_inspector(token, task_goal=task_goal, webspace_id=webspace_id)
    except KeyError:
        return _fallback_inspector(token, warning=f"Object not found: {token}")

    context = coerce_mapping(getattr(projection, "context", {}))
    inspector = coerce_mapping(context.get("inspector"))
    subject = getattr(projection, "subject", None)
    subject_id = str(getattr(subject, "id", "") or "").strip()
    subject_title = str(getattr(subject, "title", "") or subject_id).strip() or subject_id
    if not inspector:
        inspector = {}
    inspector.setdefault("label", str(getattr(subject, "kind", "") or "object"))
    inspector.setdefault("value", _status_token(getattr(subject, "status", None)))
    inspector.setdefault("subtitle", subject_title)
    inspector.setdefault("description", str(getattr(projection, "summary", None) or ""))
    inspector.setdefault("object", subject.to_dict() if subject is not None else {})
    inspector.setdefault("incidents", list(context.get("incidents") or getattr(projection, "incidents", []) or []))
    inspector.setdefault("actions", list(context.get("actions") or []))
    inspector.setdefault("recent_changes", list(context.get("recent_changes") or []))
    inspector.setdefault("topology", coerce_mapping(context.get("topology")))
    inspector.setdefault("task_packet", coerce_mapping(context.get("task_packet")))
    inspector.setdefault(
        "subnet_planning",
        coerce_mapping(context.get("subnet_planning"))
        or coerce_mapping(coerce_mapping(context.get("task_packet")).get("context")).get("subnet_planning")
        or {},
    )
    inspector["object_id"] = subject_id
    inspector["object_title"] = subject_title
    return inspector


@tool("get_snapshot")
def get_snapshot(webspace_id: str | None = None, task_goal: str | None = None) -> dict[str, Any]:
    _ensure_skill_data_projections()
    return _snapshot_or_fallback(webspace_id=webspace_id, task_goal=task_goal)


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None, task_goal: str | None = None) -> dict[str, Any]:
    if task_goal:
        targets = _refresh_projection_targets(webspace_id=webspace_id)
        projected = 0
        last_snapshot: dict[str, Any] | None = None
        for target_ws in targets:
            snapshot = _snapshot_or_fallback(webspace_id=target_ws, task_goal=task_goal)
            last_snapshot = snapshot
            if _project_snapshot(snapshot, webspace_id=target_ws):
                projected += 1
            _publish_snapshot_streams(snapshot, webspace_id=target_ws)
        return {"ok": True, "projected": projected, "webspaces": targets, "snapshot": last_snapshot or {}}
    return _refresh_snapshot_targets(_refresh_projection_targets(webspace_id=webspace_id))


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, dict):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if not receiver.startswith("infrascope."):
        return
    webspace_id = _webspace_id_from_payload(payload) or default_webspace_id()
    snapshot = _last_good_snapshots.get(_snapshot_cache_key(webspace_id=webspace_id))
    if not isinstance(snapshot, dict):
        snapshot = _snapshot_or_fallback(webspace_id=webspace_id)
    stream_payload = _stream_payload_for_receiver(snapshot, receiver)
    if stream_payload is None:
        return
    _publish_stream_payload(receiver, stream_payload, webspace_id=webspace_id)


@subscribe("infrascope.refresh")
def on_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload) or _event_webspace_fallback())


@subscribe("operations.")
def on_operations_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason="operations.changed",
    )


@subscribe("device.registered")
@subscribe("browser.session.changed")
@subscribe("webrtc.peer.state.changed")
def on_browser_runtime_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "webrtc.peer.state.changed")
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason=event_type,
    )


@subscribe("skill.installed")
@subscribe("skill.uninstalled")
@subscribe("scenario.installed")
@subscribe("scenario.removed")
@subscribe("scenarios.synced")
@subscribe("skills.activated")
@subscribe("skills.updated")
@subscribe("skills.rolledback")
@subscribe("skill.service.ready")
@subscribe("workspace.")
@subscribe("user.profile.changed")
@subscribe("capacity.changed")
def on_registry_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "skills.updated")
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason=event_type,
    )


@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
@subscribe("desktop.webspace.reloaded")
@subscribe("desktop.webspace.reset")
@subscribe("desktop.webspace.restored")
@subscribe("desktop.webspace.go_home")
@subscribe("desktop.webspace.set_home")
@subscribe("desktop.webspace.set_home_current")
@subscribe("desktop.webspace.create")
@subscribe("desktop.webspace.rename")
@subscribe("desktop.webspace.update")
@subscribe("desktop.webspace.delete")
@subscribe("desktop.scenario.set")
@subscribe("node.yjs.control.completed")
@subscribe("node.yjs.control.failed")
def on_webspace_event(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "desktop.webspace.refresh")
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason=event_type,
    )


@subscribe("sys.ready")
@subscribe("subnet.nats.up")
@subscribe("subnet.nats.down")
@subscribe("subnet.nats.reconnect")
@subscribe("subnet.member.link.up")
@subscribe("subnet.member.link.down")
@subscribe("subnet.member.meta.changed")
@subscribe("subnet.member.snapshot.changed")
@subscribe("subnet.member.snapshot.requested")
@subscribe("subnet.member.update.requested")
@subscribe("subnet.member.update.result")
@subscribe("subnet.stopping")
@subscribe("subnet.stopped")
@subscribe("core.update.status")
@subscribe("hub.core_update.status")
@subscribe("node.names.changed")
def on_runtime_event(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "runtime.event")
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason=event_type,
    )
