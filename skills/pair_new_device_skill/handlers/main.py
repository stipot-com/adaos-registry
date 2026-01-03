from __future__ import annotations

import time
from typing import Any, Mapping
from uuid import uuid4

from adaos.sdk.core.decorators import tool
from adaos.sdk.data import skill_memory_set


@tool(
    "create_pairing",
    summary="create a short-lived pairing session for a new browser",
    stability="experimental",
    examples=["create_pairing()", "create_pairing({'webspace_id': 'default'})"],
)
def create_pairing(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Create a new pairing session.

    This is a scaffold for the "Pair new device" flow:
    - TV shows pairing_id as QR.
    - Owner scans and approves pairing.
    - Hub issues a device grant and associates selected webspace.

    The current implementation only creates and persists the pairing record.
    """
    webspace_id = None
    if isinstance(payload, Mapping):
        ws = payload.get("webspace_id") or payload.get("workspace_id")
        if isinstance(ws, str) and ws.strip():
            webspace_id = ws.strip()

    pairing_id = f"pair_{uuid4().hex}"
    expires_in_sec = 10 * 60
    record = {
        "pairing_id": pairing_id,
        "created_at": int(time.time()),
        "expires_in_sec": expires_in_sec,
        "webspace_id": webspace_id,
        "status": "pending",
    }

    # Store as skill-local memory for now (can be swapped to a dedicated service later).
    skill_memory_set(f"pairing:{pairing_id}", record)

    return {
        "ok": True,
        "pairing_id": pairing_id,
        "expires_in_sec": expires_in_sec,
        "note": "scaffold: pairing record created; approval/grant flow not wired yet",
    }


def handle(topic: str, payload: dict) -> None:
    """
    Default handler required by the skill runtime.

    This skill is currently tool-driven; event handling is not used.
    """
    return None

