from __future__ import annotations

from typing import Any

from adaos.sdk.core.decorators import tool
from adaos.services.media_library import media_snapshot


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
    return media_snapshot()


def handle(_topic: str, _payload: dict[str, Any]) -> None:
    return None
