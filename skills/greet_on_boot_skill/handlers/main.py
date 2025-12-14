from __future__ import annotations

from typing import Any, Mapping

from adaos.sdk.core.decorators import tool, subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.scenario.projection_service import ProjectionService
from adaos.services.io_voice_mock import tts_speak


@tool(
    "collect_infra_status",
    summary="collect basic infrastructure status for greet_on_boot",
    stability="experimental",
    examples=["collect_infra_status()"],
)
def collect_infra_status(_: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Collect a minimal snapshot of the current node/subnet status.

    For v0 this is intentionally simple and relies on AgentContext only.
    Scenario/web_desktop data_projections decide where this snapshot is stored.
    """
    ctx = get_ctx()
    node = ctx.node
    subnet = ctx.subnet
    status = {
        "node": {
            "id": getattr(node, "node_id", None),
            "hostname": getattr(node, "hostname", None),
            "roles": getattr(node, "roles", []),
        },
        "subnet": {
            "id": getattr(subnet, "subnet_id", None),
        },
    }

    # Project into configured backends (Yjs, KV, ...) via data_projections.
    proj = ProjectionService.from_ctx(ctx)
    # For infra we use the logical slot (subnet, infra.status); web_desktop scenario
    # defines a Yjs path for this slot in its data_projections.
    ctx.bus.spawn_task(
        proj.apply(
            "subnet",
            "infra.status",
            status,
            webspace_id=None,
        )
    )

    return {"ok": True, "status": status}


@tool(
    "analyze_and_notify",
    summary="analyze infra.status projection and send a short summary via voice/telegram",
    stability="experimental",
    examples=["analyze_and_notify()"],
)
def analyze_and_notify(_: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Read the latest infra.status snapshot and emit a short summary.

    For v0 this uses voice mock (tts_speak) and a generic event for Telegram;
    in production this can be wired to real IO integrations.
    """
    ctx = get_ctx()
    # For now read back from the same logical slot we project into; in a more
    # advanced version this would use ctx.* helpers.
    status = {
        "node": {},
        "subnet": {},
    }
    try:
        # Best-effort fetch from profile/kv or elsewhere can be added here.
        # For MVP we rely on the fact that web_desktop reads data/infra/status
        # directly, so here we only need a voice/telegram message.
        node = ctx.node
        subnet = ctx.subnet
        status["node"] = {
            "id": getattr(node, "node_id", None),
            "hostname": getattr(node, "hostname", None),
            "roles": getattr(node, "roles", []),
        }
        status["subnet"] = {
            "id": getattr(subnet, "subnet_id", None),
        }
    except Exception:
        pass

    node_id = status.get("node", {}).get("id") or "unknown-node"
    hostname = status.get("node", {}).get("hostname") or "unknown-host"
    roles = status.get("node", {}).get("roles") or []
    subnet_id = status.get("subnet", {}).get("id") or "unknown-subnet"

    message = (
        f"Hub OK. Node {hostname} ({node_id}) in subnet {subnet_id}. "
        f"Roles: {', '.join(roles) if roles else 'none'}."
    )

    # Voice: use local mock TTS for now.
    try:
        tts_speak(message)
    except Exception:
        pass

    # Telegram: emit a generic event that can be routed by integrations.
    try:
        ctx.bus.publish(
            "io.notify",
            {
                "channel": "telegram",
                "text": message,
            },
        )
    except Exception:
        pass

    return {"ok": True, "message": message}


def handle(topic: str, payload: dict) -> None:
    """
    Minimal default handler required by the skill runtime.

    greet_on_boot_skill is workflow-driven; regular event handling is not used
    for now, so this is a no-op.
    """
    return None
