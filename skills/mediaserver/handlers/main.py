from __future__ import annotations

from typing import Any

from adaos.sdk.core.decorators import tool
from adaos.sdk.data import ctx_subnet
from adaos.services.media_library import media_snapshot
from adaos.services.yjs.webspace import default_webspace_id

REQUIRES_DATA_PROJECTIONS = ["mediaserver.library"]


def _webspace_id(webspace_id: str | None = None, payload: dict[str, Any] | None = None) -> str:
    token = str(webspace_id or "").strip()
    if token:
        return token
    if isinstance(payload, dict):
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        token = str(payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or "").strip()
        if token:
            return token
    return default_webspace_id()


def _summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
    capabilities = snapshot.get("capabilities") if isinstance(snapshot.get("capabilities"), dict) else {}
    return {
        "title": "Media Server",
        "value": len(items),
        "subtitle": f"{len(items)} media files",
        "details": str(capabilities.get("state") or capabilities.get("status") or "ready"),
    }


def _publish_snapshot(snapshot: dict[str, Any], *, webspace_id: str) -> None:
    payload = {**snapshot, "summary": _summary(snapshot)}
    ctx_subnet.set("mediaserver.library", payload, webspace_id=webspace_id)


@tool(
    "get_snapshot",
    summary="return mediaserver library snapshot and channel capability diagnostics",
    stability="experimental",
)
def get_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    snapshot = media_snapshot()
    _publish_snapshot(snapshot, webspace_id=_webspace_id(webspace_id, _payload))
    return snapshot


@tool(
    "refresh_snapshot",
    summary="publish mediaserver library snapshot and return lightweight ack",
    stability="experimental",
)
def refresh_snapshot(
    _payload: dict[str, Any] | None = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    snapshot = media_snapshot()
    _publish_snapshot(snapshot, webspace_id=_webspace_id(webspace_id, _payload))
    return {"ok": True, "summary": _summary(snapshot), "delivery": "yjs_projection"}


def handle(_topic: str, _payload: dict[str, Any]) -> None:
    return None
