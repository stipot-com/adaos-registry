from __future__ import annotations

import time
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.io.out import stream_publish

_RECEIVER_ID = "demo_metrics.events"


def _webspace_id_from_payload(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return "desktop"
    raw = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    meta = payload.get("_meta")
    if isinstance(meta, Mapping):
        nested = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return "desktop"


def _publish_demo_event(
    *,
    webspace_id: str,
    title: str,
    description: str,
    source: str,
    severity: str = "info",
) -> dict[str, Any]:
    item = {
        "id": f"demo:{source}:{int(time.time() * 1000)}",
        "title": title,
        "description": description,
        "source": source,
        "severity": severity,
        "ts": time.time(),
    }
    stream_publish(_RECEIVER_ID, item, _meta={"webspace_id": webspace_id})
    return item


def _snapshot() -> dict[str, Any]:
    rows = [
        {
            "id": "cpu",
            "title": "CPU Load",
            "status": "healthy",
            "value": 42,
            "unit": "%",
            "updated_at": "2026-05-07T10:00:00Z",
            "group": "compute",
        },
        {
            "id": "memory",
            "title": "Memory Pressure",
            "status": "warning",
            "value": 76,
            "unit": "%",
            "updated_at": "2026-05-07T10:00:00Z",
            "group": "compute",
        },
        {
            "id": "queue",
            "title": "Queue Depth",
            "status": "healthy",
            "value": 7,
            "unit": "jobs",
            "updated_at": "2026-05-07T10:00:00Z",
            "group": "runtime",
        },
    ]
    series = {
        "metric_id": "cpu",
        "title": "CPU Load",
        "x_key": "ts",
        "y_key": "value",
        "series_by_metric": {
            "cpu": {
                "metric_id": "cpu",
                "title": "CPU Load",
                "points": [
                    {"ts": "10:00", "value": 31},
                    {"ts": "10:05", "value": 34},
                    {"ts": "10:10", "value": 39},
                    {"ts": "10:15", "value": 42},
                ],
            },
            "memory": {
                "metric_id": "memory",
                "title": "Memory Pressure",
                "points": [
                    {"ts": "10:00", "value": 62},
                    {"ts": "10:05", "value": 68},
                    {"ts": "10:10", "value": 74},
                    {"ts": "10:15", "value": 76},
                ],
            },
            "queue": {
                "metric_id": "queue",
                "title": "Queue Depth",
                "points": [
                    {"ts": "10:00", "value": 4},
                    {"ts": "10:05", "value": 6},
                    {"ts": "10:10", "value": 5},
                    {"ts": "10:15", "value": 7},
                ],
            },
        },
        "points": [
            {"ts": "10:00", "value": 31},
            {"ts": "10:05", "value": 34},
            {"ts": "10:10", "value": 39},
            {"ts": "10:15", "value": 42},
        ],
    }
    events = {
        "items": [
            {
                "id": "evt-1",
                "title": "Initial demo snapshot",
                "description": "Shared demo metrics payload seeded for the current webspace.",
            },
            {
                "id": "evt-2",
                "title": "Chart selection linked",
                "description": "The selected table row drives the chart series payload.",
            },
        ]
    }
    chat = {
        "messages": [
            {
                "id": "chat-1",
                "from": "hub",
                "text": "Semantic chat_panel is now part of the demo surface.",
                "ts": "2026-05-07T10:00:00Z",
            },
            {
                "id": "chat-2",
                "from": "operator",
                "text": "The first rollout keeps chat read-only and shared-state backed.",
                "ts": "2026-05-07T10:01:00Z",
            },
        ]
    }
    return {
        "summary": {
            "value": "3",
            "label": "Demo metrics",
            "description": "Neutral semantic Web UI control task",
            "buttons": [
                {"id": "open-demo", "label": "Open modal"},
                {"id": "emit-skill", "label": "Skill event"},
                {"id": "emit-host", "label": "Host event"},
            ],
        },
        "table": {"items": rows},
        "chart": series,
        "selection": {
            "metric_id": "cpu",
            "status_filter": "all",
            "group_filter": "all",
        },
        "events": events,
        "chat": chat,
    }


@tool(
    "get_demo_snapshot",
    summary="Return the current static snapshot for the demo metrics browser surfaces.",
    stability="experimental",
)
def get_demo_snapshot(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _ = payload
    return {"ok": True, "snapshot": _snapshot()}


@tool(
    "list_demo_series",
    summary="Return one chart payload for the demo metrics skill.",
    stability="experimental",
)
def list_demo_series(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    metric_id = ""
    if isinstance(payload, Mapping):
        metric_id = str(payload.get("metric_id") or "").strip()
    snap = _snapshot()
    if metric_id:
        series = snap["chart"].get("series_by_metric", {}).get(metric_id)
        if isinstance(series, Mapping):
            snap["chart"] = {
                **snap["chart"],
                **series,
                "metric_id": metric_id,
            }
    return {"ok": True, "series": snap["chart"]}


@tool(
    "emit_demo_event",
    summary="Publish one live demo event into the browser event stream.",
    stability="experimental",
)
def emit_demo_event(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    body = payload if isinstance(payload, Mapping) else {}
    webspace_id = _webspace_id_from_payload(body)
    action_id = str(body.get("action_id") or "skill_action").strip() or "skill_action"
    metric_id = str(body.get("metric_id") or "").strip() or "current"
    item = _publish_demo_event(
        webspace_id=webspace_id,
        title=f"Skill action: {action_id}",
        description=f"demo_metrics_skill emitted a live event for metric `{metric_id}`.",
        source="skill",
        severity="success",
    )
    return {"ok": True, "event": item}


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver != _RECEIVER_ID:
        return
    webspace_id = _webspace_id_from_payload(payload)
    _publish_demo_event(
        webspace_id=webspace_id,
        title="Stream attached",
        description="The demo event stream is now subscribed for this browser session.",
        source="stream.snapshot",
    )


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver != _RECEIVER_ID:
        return
    action = str(payload.get("action") or "").strip().lower() or "subscribed"
    if action == "unsubscribed":
        return
    webspace_id = _webspace_id_from_payload(payload)
    _publish_demo_event(
        webspace_id=webspace_id,
        title="Subscription changed",
        description="A browser consumer subscribed to the demo metrics event feed.",
        source="stream.subscription",
    )


@subscribe("demo_metrics.host_action")
def on_demo_metrics_host_action(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    webspace_id = _webspace_id_from_payload(payload)
    metric_id = str(payload.get("metric_id") or "").strip() or "current"
    action_id = str(payload.get("action_id") or "host_action").strip() or "host_action"
    _publish_demo_event(
        webspace_id=webspace_id,
        title=f"Host action: {action_id}",
        description=f"Host event accepted for metric `{metric_id}` and mirrored into the live stream.",
        source="host",
        severity="warning",
    )
