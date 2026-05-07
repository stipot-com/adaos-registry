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
    if metric_id and metric_id != str(snap["chart"].get("metric_id") or ""):
        title = metric_id.upper()
        snap["chart"] = {
            "metric_id": metric_id,
            "title": title,
            "x_key": "ts",
            "y_key": "value",
            "points": [
                {"ts": "10:00", "value": 10},
                {"ts": "10:05", "value": 20},
                {"ts": "10:10", "value": 15},
                {"ts": "10:15", "value": 25},
            ],
        }
    return {"ok": True, "series": snap["chart"]}
