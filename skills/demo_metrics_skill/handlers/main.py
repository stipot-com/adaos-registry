from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core.decorators import tool


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
