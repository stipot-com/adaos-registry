from __future__ import annotations

from typing import Any

from adaos.sdk.core.decorators import tool
from adaos.services.system_model.mappers import coerce_mapping
from adaos.services.system_model.model import CanonicalObject, CanonicalStatus
from adaos.services.system_model.service import (
    current_control_plane_objects,
    current_object_inspector,
    current_overview_projection,
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


def _inventory_kind_token(value: Any) -> str:
    token = str(value or "all").strip().lower() or "all"
    return token if token in _KIND_ALIASES else "all"


def _matches_inventory_kind(obj: CanonicalObject, requested: str) -> bool:
    if requested == "all":
        return True
    return str(getattr(obj, "kind", "") or "").strip().lower() in _KIND_ALIASES.get(requested, set())


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
        key=lambda item: (_status_rank(getattr(item, "status", None)), str(getattr(item, "kind", "") or ""), str(getattr(item, "title", "") or "")),
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
    objects = [
        item
        for item in current_control_plane_objects(webspace_id=webspace_id)
        if _matches_inventory_kind(item, requested)
    ]
    return _sorted_object_rows(objects)


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
        return {
            "label": "object",
            "value": CanonicalStatus.UNKNOWN.value,
            "subtitle": token,
            "description": f"Unknown control-plane object: {token}",
            "warning": f"Object not found: {token}",
            "object": {},
            "incidents": [],
            "actions": [],
            "recent_changes": [],
            "topology": {"edges": []},
            "task_packet": {},
        }

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
    inspector["object_id"] = subject_id
    inspector["object_title"] = subject_title
    return inspector
