from __future__ import annotations

from typing import Any, Mapping
from pathlib import Path
import logging
import platform
import requests

from adaos.sdk.core.decorators import tool, subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.scenario.workflow_runtime import ScenarioWorkflowRuntime
from adaos.services.scenario.webspace_runtime import WebspaceService
from adaos.services.io_voice_mock import tts_speak
from adaos.services.capacity import get_local_capacity
from adaos.sdk.data import ctx_subnet


_log = logging.getLogger("skills.greet_on_boot_skill")


@tool(
    "collect_infra_status",
    summary="collect basic infrastructure status for greet_on_boot",
    stability="experimental",
    examples=["collect_infra_status()"],
)
def collect_infra_status(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Collect a minimal snapshot of the current node/subnet status.

    For v0 this is intentionally simple and relies on AgentContext only.
    Scenario/web_desktop data_projections decide where this snapshot is stored.
    """
    ctx = get_ctx()
    _log.info("greet_on_boot.collect_infra_status start payload=%r", payload)
    webspace_id = None
    if isinstance(payload, Mapping):
        ws = payload.get("webspace_id") or payload.get("workspace_id")
        if isinstance(ws, str) and ws.strip():
            webspace_id = ws.strip()
    _log.info("greet_on_boot.collect_infra_status webspace_id=%r", webspace_id)

    # Use config snapshot; AgentContext may not expose node/subnet objects yet.
    conf = getattr(ctx, "config", None)
    node_id = getattr(conf, "node_id", None) if conf is not None else None
    subnet_id = getattr(conf, "subnet_id", None) if conf is not None else None
    role = getattr(conf, "role", None) if conf is not None else None
    hostname = platform.node() or None

    status = {
        "node": {
            "id": node_id,
            "hostname": hostname,
            "roles": [role] if role else [],
        },
        "subnet": {
            "id": subnet_id,
        },
    }

    _log.info(
        "greet_on_boot.collect_infra_status status webspace=%s node_id=%s subnet_id=%s",
        webspace_id,
        node_id,
        subnet_id,
    )

    # Project into configured backends (Yjs, KV, ...) via data_projections.
    # For infra we use the logical slot (subnet, infra.status); web_desktop or
    # greet_on_boot scenarios define a Yjs path for this slot in their
    # data_projections.
    try:
        ctx_subnet.set("infra.status", status, webspace_id=webspace_id)
        _log.info("greet_on_boot.collect_infra_status projected ok webspace=%s", webspace_id)
    except Exception as exc:
        _log.warning(
            "greet_on_boot.collect_infra_status projection failed webspace=%s error=%s",
            webspace_id,
            exc,
            exc_info=True,
        )

    return {"ok": True, "status": status}


@tool(
    "analyze_and_notify",
    summary="analyze infra.status projection and send a short summary via voice/telegram",
    stability="experimental",
    examples=["analyze_and_notify()"],
)
def analyze_and_notify(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Read the latest infra.status snapshot and emit a short summary.

    For v0 this uses voice mock (tts_speak) and a generic event for Telegram;
    in production this can be wired to real IO integrations.
    """
    ctx = get_ctx()
    # For now reuse the same config snapshot as in collect_infra_status.
    conf = getattr(ctx, "config", None)
    node_id = getattr(conf, "node_id", None) if conf is not None else None
    subnet_id = getattr(conf, "subnet_id", None) if conf is not None else None
    role = getattr(conf, "role", None) if conf is not None else None
    hostname = platform.node() or None

    node_id = node_id or "unknown-node"
    hostname = hostname or "unknown-host"
    roles = [role] if role else []
    subnet_id = subnet_id or "unknown-subnet"

    message = (
        f"Hub OK. Node {hostname} ({node_id}) in subnet {subnet_id}. "
        f"Roles: {', '.join(roles) if roles else 'none'}."
    )

    # Voice: use local mock TTS for now.
    try:
        tts_speak(message)
    except Exception:
        pass

    # Telegram: reuse Root API /io/tg/send (same path, что и subnet.started).
    try:
        conf = ctx.config
        api_base = getattr(ctx.settings, "api_base", "https://api.inimatic.com")
        hub_id = getattr(conf, "subnet_id", None)
        if hub_id:
            requests.post(
                f"{api_base.rstrip('/')}/io/tg/send",
                json={"hub_id": hub_id, "text": message},
                timeout=3.0,
            )
    except Exception:
        pass

    return {"ok": True, "message": message}


def _is_active(name: str, items: list[dict[str, Any]]) -> bool:
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") == name and bool(item.get("active", True)):
            return True
    return False


@subscribe("sys.ready")
async def on_sys_ready(evt: Any) -> None:
    """
    Trigger greet_on_boot workflow on node boot, if installed and active.
    """
    ctx = get_ctx()
    _log.info("greet_on_boot.on_sys_ready received")
    caps = get_local_capacity() or {}
    skills = caps.get("skills") or []
    scenarios = caps.get("scenarios") or []

    # Scenario gating: allow either active in capacity or presence in workspace.
    scenarios_root = ctx.paths.scenarios_dir()
    scenarios_root = scenarios_root() if callable(scenarios_root) else scenarios_root
    manifest_present = Path(scenarios_root) / "greet_on_boot" / "scenario.json"
    scenario_allowed = _is_active("greet_on_boot", scenarios) or manifest_present.exists()

    if not scenario_allowed:
        _log.info("greet_on_boot scenario not installed/active; skipping")
        return
    if not _is_active("greet_on_boot_skill", skills):
        _log.info("greet_on_boot_skill not active in capacity; skipping")
        return

    # Ensure ProjectionRegistry knows how to project infra.status even if
    # desktop scenarios have not been rebuilt yet.
    try:
        ctx.projections.load_from_scenario("greet_on_boot")
    except Exception:
        _log.debug("greet_on_boot: failed to load data_projections", exc_info=True)

    runtime = ScenarioWorkflowRuntime(ctx)
    webspaces = WebspaceService(ctx).list(mode="workspace")

    for ws in webspaces:
        webspace_id = ws.id
        _log.info(
            "greet_on_boot.ready_start scenario=greet_on_boot webspace=%s node_id=%s",
            webspace_id,
            getattr(ctx.config, "node_id", None),
        )
        try:
            await runtime.apply_action("greet_on_boot", webspace_id, "collect")
            await runtime.apply_action("greet_on_boot", webspace_id, "analyze")
            _log.info(
                "greet_on_boot.workflow_completed scenario=greet_on_boot webspace=%s",
                webspace_id,
            )
        except Exception as exc:  # defensive
            _log.warning(
                "greet_on_boot.workflow_failed scenario=greet_on_boot webspace=%s error=%s",
                webspace_id,
                exc,
                exc_info=True,
            )

def handle(topic: str, payload: dict) -> None:
    """
    Minimal default handler required by the skill runtime.

    greet_on_boot_skill is workflow-driven; regular event handling is not used
    for now, so this is a no-op.
    """
    return None
