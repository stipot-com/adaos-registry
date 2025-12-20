from __future__ import annotations

from typing import Any, Mapping
from pathlib import Path
import logging
import platform
import yaml

from adaos.sdk.core.decorators import tool, subscribe
from adaos.sdk.core.ctx import get_ctx
from adaos.sdk.scenarios.workflow import ScenarioWorkflowRuntime
from adaos.sdk.web.webspace import webspace_list
from adaos.sdk.data.events import publish as publish_event
from adaos.sdk.capacity import get_local_capacity
from adaos.sdk.data import ctx_subnet


_log = logging.getLogger("skills.greet_on_boot_skill")
REQUIRES_DATA_PROJECTIONS = [
    {"scope": "subnet", "slot": "infra.status"},
]
_NOTIFY_SENT = False


def _load_skill_data_projections(ctx) -> None:
    """
    Load skill-level data_projections from skill.yaml into ProjectionRegistry.

    This gives greet_on_boot_skill a default view on where infra.status
    should be stored; desktop/global scenarios can override this later via
    their own data_projections.
    """
    try:
        skills_root = ctx.paths.skills_workspace_dir()
        skills_root = skills_root() if callable(skills_root) else skills_root
        manifest_path = Path(skills_root) / "greet_on_boot_skill" / "skill.yaml"
        if not manifest_path.exists():
            _log.warning(
                "greet_on_boot_skill: skill.yaml not found when loading data_projections (path=%s)",
                manifest_path,
            )
            return
        spec = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        entries = spec.get("data_projections") or []
        if not isinstance(entries, list) or not entries:
            _log.warning(
                "greet_on_boot_skill: skill.yaml has no data_projections; infra.status projections may be misconfigured"
            )
            return
        ctx.projections.load_entries(entries)
        _log.debug("greet_on_boot_skill: loaded %d skill-level data_projections", len(entries))
    except Exception:
        _log.debug("greet_on_boot_skill: failed to load skill data_projections", exc_info=True)


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
    _log.debug("greet_on_boot.collect_infra_status start payload=%r", payload)
    webspace_id = None
    if isinstance(payload, Mapping):
        ws = payload.get("webspace_id") or payload.get("workspace_id")
        if isinstance(ws, str) and ws.strip():
            webspace_id = ws.strip()
    _log.debug("greet_on_boot.collect_infra_status webspace_id=%r", webspace_id)

    # Use config snapshot; AgentContext may not expose node/subnet objects yet.
    conf = getattr(ctx, "config", None)
    node_id = getattr(conf, "node_id", None) if conf is not None else None
    subnet_id = getattr(conf, "subnet_id", None) if conf is not None else None
    role = getattr(conf, "role", None) if conf is not None else None
    hostname = platform.node() or None

    base_status = {
        "node": {
            "id": node_id,
            "hostname": hostname,
            "roles": [role] if role else [],
        },
        "subnet": {
            "id": subnet_id,
        },
    }
    # Shape tailored for visual.metricTile: main value + label
    status_value: dict[str, Any] = {
        "value": "OK",
        "label": f"{hostname or 'unknown-host'} ({node_id or 'unknown-node'})",
        **base_status,
    }

    _log.debug(
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
        # Store the bare status_value; ProjectionService will map
        # (subnet, infra.status) -> data/infra/status so that webui
        # can read it via path "data/infra/status".
        ctx_subnet.set("infra.status", status_value, webspace_id=webspace_id)
        _log.debug("greet_on_boot.collect_infra_status projected ok webspace=%s", webspace_id)
    except Exception as exc:
        _log.warning(
            "greet_on_boot.collect_infra_status projection failed webspace=%s error=%s",
            webspace_id,
            exc,
            exc_info=True,
        )

    return {"ok": True, "status": status_value}


@tool(
    "analyze_and_notify",
    summary="analyze infra.status projection and send a short summary via voice/telegram",
    stability="experimental",
    examples=["analyze_and_notify()"],
)
def analyze_and_notify(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Read the latest infra.status snapshot and emit a short summary.

    For v0 this emits ui.say and ui.notify for router-based delivery.
    """
    ctx = get_ctx()
    webspace_id = None
    force = False
    if isinstance(payload, Mapping):
        ws = payload.get("webspace_id") or payload.get("workspace_id")
        if isinstance(ws, str) and ws.strip():
            webspace_id = ws.strip()
        force = bool(payload.get("force")) if "force" in payload else False
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

    # Avoid spamming notifications for every webspace; speak once by default.
    global _NOTIFY_SENT
    if _NOTIFY_SENT and not force:
        return {"ok": True, "message": message, "skipped": True, "webspace_id": webspace_id}
    _NOTIFY_SENT = True

    # ui.say -> TTS, ui.notify -> stdout/telegram via route_rules.
    try:
        publish_event("ui.say", {"text": message}, source="greet_on_boot_skill")
    except Exception:
        pass
    try:
        publish_event("ui.notify", {"text": message}, source="greet_on_boot_skill")
    except Exception:
        pass


    return {"ok": True, "message": message}


@subscribe("subnet.stopped")
def on_subnet_stopped(evt: Any) -> None:
    """
    Mark infrastructure as OFF in all workspace webspaces when subnet stops.
    """
    ctx = get_ctx()
    try:
        webspaces = webspace_list(mode="workspace")
    except Exception:
        return

    for ws in webspaces:
        webspace_id = ws.id
        try:
            ctx_subnet.set(
                "infra.status",
                {
                    "value": "OFF",
                    "label": "Hub OFF",
                },
                webspace_id=webspace_id,
            )
        except Exception:
            continue


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
    _log.debug("greet_on_boot.on_sys_ready received")
    caps = get_local_capacity() or {}
    skills = caps.get("skills") or []
    scenarios = caps.get("scenarios") or []

    # Scenario gating: allow either active in capacity or presence in workspace.
    scenarios_root = ctx.paths.scenarios_dir()
    scenarios_root = scenarios_root() if callable(scenarios_root) else scenarios_root
    manifest_present = Path(scenarios_root) / "greet_on_boot" / "scenario.json"
    scenario_allowed = _is_active("greet_on_boot", scenarios) or manifest_present.exists()

    if not scenario_allowed:
        _log.debug("greet_on_boot scenario not installed/active; skipping")
        return
    if not _is_active("greet_on_boot_skill", skills):
        _log.debug("greet_on_boot_skill not active in capacity; skipping")
        return

    # Ensure ProjectionRegistry knows how to project infra.status even if
    # desktop scenarios have not been rebuilt yet. First load defaults from
    # the skill manifest, then allow scenarios to override.
    try:
        _load_skill_data_projections(ctx)
        ctx.projections.load_from_scenario("greet_on_boot")
    except Exception:
        _log.debug("greet_on_boot: failed to load data_projections", exc_info=True)

    runtime = ScenarioWorkflowRuntime(ctx)
    webspaces = webspace_list(mode="workspace")

    for ws in webspaces:
        webspace_id = ws.id
        _log.debug(
            "greet_on_boot.ready_start scenario=greet_on_boot webspace=%s node_id=%s",
            webspace_id,
            getattr(ctx.config, "node_id", None),
        )
        try:
            await runtime.apply_action("greet_on_boot", webspace_id, "collect")
            await runtime.apply_action("greet_on_boot", webspace_id, "analyze")
            _log.debug(
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


@subscribe("desktop.webspace.reload")
async def on_webspace_reload(evt: Any) -> None:
    """
    Refresh infra.status for a webspace after YJS reload so that the
    Infrastructure status widget is not empty.
    """
    webspace_id = None
    if isinstance(evt, Mapping):
        meta = evt.get("_meta") or {}
        webspace_id = evt.get("webspace_id") or meta.get("webspace_id")
    if not webspace_id:
        webspace_id = "default"

    try:
        collect_infra_status({"webspace_id": webspace_id})
    except Exception:
        # best-effort: infra widget can be refreshed manually later
        return
