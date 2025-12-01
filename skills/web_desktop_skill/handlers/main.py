from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List

import asyncio
import os
import re
import secrets
import y_py as Y

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.decorators import subscribe
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc, mutate_live_room
from adaos.apps.workspaces import index as workspace_index
from adaos.apps.yjs.webspace import default_webspace_id

_log = logging.getLogger("skills.web_desktop")

# During static validation, handlers are imported in a lightweight subprocess
# without a full AdaOS runtime. In that case, avoid requiring AgentContext
# at import time to let validation introspect decorators safely.
if os.environ.get("ADAOS_VALIDATE") == "1":
    _ctx = None  # type: ignore[assignment]
else:
    _ctx = require_ctx("skills.web_desktop_skill")
_DEFAULT_SCENARIO_ID = "web_desktop"
_WS_ID_RE = re.compile(r"[^a-z0-9-_]+")


def _payload(evt: Any) -> Dict[str, Any]:
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload") or {}
        if isinstance(data, dict):
            return data
    if isinstance(evt, dict):
        return evt
    return {}


def _webspace_id(payload: Dict[str, Any]) -> str:
    """
    Resolve target webspace id for an event payload.

    Explicit fields on the payload (webspace_id/workspace_id) take
    precedence over metadata injected by the transport (_meta), so
    that callers can override the default/connection webspace.
    """
    if isinstance(payload, dict):
        direct = payload.get("webspace_id") or payload.get("workspace_id")
        if direct:
            return str(direct)
        meta = payload.get("_meta")
        if isinstance(meta, dict):
            token = meta.get("webspace_id") or meta.get("workspace_id")
            if token:
                return str(token)
    return "default"


def _webui_path(skill_name: str, space: str) -> Path:
    paths = _ctx.paths
    base = paths.dev_skills_dir() if space == "dev" else paths.skills_dir()
    return Path(base) / skill_name / "webui.json"


def _load_webui(skill_name: str, space: str) -> Dict[str, Any]:
    path = _webui_path(skill_name, space)
    if not path.exists():
        _log.debug("webui.json missing for %s (%s)", skill_name, space)
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("failed to read webui.json for %s: %s", skill_name, exc)
        return {}

    catalog = raw.get("catalog") or {}
    apps = raw.get("apps") or catalog.get("apps") or []
    widgets = raw.get("widgets") or catalog.get("widgets") or []
    registry = raw.get("registry") or {}
    reg_modals_raw = registry.get("modals") or {}
    reg_widgets_raw = registry.get("widgets") or {}
    ydoc_defaults = raw.get("ydoc_defaults") or {}
    raw_contrib = raw.get("contributions") or []
    contributions = [c for c in raw_contrib if isinstance(c, dict)]

    return {
        "skill": skill_name,
        "space": space,
        "apps": [it for it in apps if isinstance(it, dict)],
        "widgets": [it for it in widgets if isinstance(it, dict)],
        "registry": {
            # Allow both dict-based modality declarations (id -> schema)
            # and legacy list-of-id registries for compatibility.
            "modals": (
                {str(k): v for k, v in reg_modals_raw.items()}
                if isinstance(reg_modals_raw, dict)
                else [str(x) for x in reg_modals_raw if isinstance(x, (str, int))]
            ),
            "widgets": (
                {str(k): v for k, v in reg_widgets_raw.items()}
                if isinstance(reg_widgets_raw, dict)
                else [str(x) for x in reg_widgets_raw if isinstance(x, (str, int))]
            ),
        },
        "ydoc_defaults": ydoc_defaults if isinstance(ydoc_defaults, dict) else {},
        "contributions": contributions,
    }


def _mark_entry(entry: Dict[str, Any], *, source: str, dev: bool) -> Dict[str, Any]:
    """
    Attach provenance / dev flag to a catalog entry without
    overwriting its semantic "source" (which may already contain
    a YDoc path like "y:data/...").

    - If entry["source"] already exists, it is preserved and the
      provenance is stored in "origin".
    - If there is no "source", we keep backward‑compatible behaviour
      and expose the provenance via "source".
    """
    data = dict(entry)
    if "source" in data and data["source"]:
        # Preserve existing source (e.g. "y:data/weather/current") so
        # that frontend widgets can treat it as a data path. Keep
        # provenance separately for debugging/inspection.
        data["origin"] = source
    else:
        data["source"] = source
    data["dev"] = dev
    return data


def _merge_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    merged: List[Dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        merged.append(item)
    return merged


def _merge_registry_lists(base: List[str], extras: List[List[str]]) -> List[str]:
    seen: set[str] = set()
    merged: List[str] = []
    for value in base:
        token = str(value)
        if token and token not in seen:
            seen.add(token)
            merged.append(token)
    for contrib in extras:
        for token in contrib:
            token = str(token)
            if token and token not in seen:
                seen.add(token)
                merged.append(token)
    return merged


def _filter_installed(installed: Dict[str, List[str]], apps: List[Dict[str, Any]], widgets: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    app_ids = {str(item.get("id")) for item in apps if item.get("id")}
    widget_ids = {str(item.get("id")) for item in widgets if item.get("id")}
    current_apps = [a for a in (installed.get("apps") or []) if a in app_ids]
    current_widgets = [w for w in (installed.get("widgets") or []) if w in widget_ids]
    return {"apps": current_apps, "widgets": current_widgets}


def _apply_ydoc_defaults(webspace_id: str, spec: Dict[str, Any]) -> None:
    """
    Ensure that required YDoc paths exist for a given webspace using
    a declarative mapping from webui.json (ydoc_defaults).
    """

    def _mutator(doc, txn) -> None:
        for path, default in spec.items():
            if not isinstance(path, str):
                continue
            segments = [s for s in path.split("/") if s]
            if not segments:
                continue
            # Simple helper: support one-level paths like "data/foo".
            if len(segments) == 2:
                root_name, key = segments
                root = doc.get_map(root_name)
                if root.get(key) is None:
                    try:
                        value = json.loads(json.dumps(default))
                    except Exception:
                        value = default
                    root.set(txn, key, value)

    try:
        applied = mutate_live_room(webspace_id, _mutator)
        if applied:
            _log.debug("ydoc_defaults applied via live room webspace=%s", webspace_id)
            return
        _log.debug("mutate_live_room skipped for ydoc_defaults webspace=%s; falling back to async_get_ydoc", webspace_id)
    except Exception:
        _log.warning("failed to apply ydoc_defaults via live room for webspace=%s", webspace_id, exc_info=True)

    # Fallback: heal the persisted YDoc snapshot so that future rooms
    # start with the correct structure.
    try:
        async def _worker() -> None:
            async with async_get_ydoc(webspace_id) as ydoc:
                with ydoc.begin_transaction() as txn:
                    _mutator(ydoc, txn)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_worker())
        else:
            loop.create_task(_worker(), name=f"web-desktop-ydoc-defaults-{webspace_id}")
        _log.debug("ydoc_defaults async healing scheduled webspace=%s", webspace_id)
    except Exception:
        _log.warning("failed to apply ydoc_defaults via async_get_ydoc for webspace=%s", webspace_id, exc_info=True)


def _rebuild_catalog(webspace_id: str) -> None:
    """
    Deprecated: catalog/registry merging is now handled by the core
    WebspaceScenarioRuntime service. This helper is kept as a no-op to
    avoid accidental calls from legacy code during the MVP migration.
    """
    _log.debug("rebuild_catalog is deprecated; handled by core runtime (webspace=%s)", webspace_id)


def _ensure_weather_seed(webspace_id: str) -> None:
    """
    Legacy helper for seeding data.weather. It is now a no-op so that
    web_desktop_skill stays agnostic of any particular domain widgets.
    """
    return


def _bootstrap_active_skills_from_capacity() -> None:
    """
    Deprecated no-op placeholder kept only to avoid import errors in
    legacy tooling. Active skills for UI are now managed by the core
    WebspaceScenarioRuntime based on capacity and skill events.
    """
    return


def _slugify_webspace_id(raw: str | None) -> str:
    if not raw:
        return ""
    token = _WS_ID_RE.sub("-", str(raw).strip().lower())
    return token.strip("-")


def _allocate_webspace_id(raw: str | None) -> str:
    candidate = _slugify_webspace_id(raw)
    if not candidate:
        candidate = f"space-{secrets.token_hex(2)}"
    base = candidate
    suffix = 1
    while workspace_index.get_workspace(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _webspace_listing() -> List[Dict[str, Any]]:
    rows = workspace_index.list_workspaces()
    return [
        {
            "id": row.workspace_id,
            "title": (row.display_name or row.workspace_id),
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _sync_webspace_listing_async() -> None:
    async def _worker() -> None:
        listing = _webspace_listing()
        rows = workspace_index.list_workspaces()
        for row in rows:
            async with async_get_ydoc(row.workspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                with ydoc.begin_transaction() as txn:
                    data_map.set(txn, "webspaces", {"items": listing})

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_worker())
    else:
        loop.create_task(_worker(), name="webspace-listing-sync")




@subscribe("scenarios.synced")
def on_scenario_synced(evt) -> None:
    """
    Deprecated: catalog/registry rebuild is handled by the core
    WebspaceScenarioRuntime. We keep this handler only to refresh
    webspace listings in data.webspaces.
    """
    _sync_webspace_listing_async()


@subscribe("skills.activated")
def on_skill_activated(evt) -> None:
    """
    Deprecated: skill UI merge is now done in core runtime. Kept as a
    no-op to avoid duplicate work during MVP migration.
    """
    return


@subscribe("skills.rolledback")
def on_skill_rolled_back(evt) -> None:
    """
    Deprecated: skill UI merge is now done in core runtime. Kept as a
    no-op to avoid duplicate work during MVP migration.
    """
    return


def _apply_install_toggle(webspace_id: str, ydoc, txn, item_type: str, target_id: str) -> None:
    data_map = ydoc.get_map("data")
    installed = data_map.get("installed") or {}
    if not isinstance(installed, dict):
        installed = {}
    apps = set(installed.get("apps") or [])
    widgets = set(installed.get("widgets") or [])
    if item_type == "app":
        if target_id in apps:
            apps.remove(target_id)
        else:
            apps.add(target_id)
    else:
        if target_id in widgets:
            widgets.remove(target_id)
        else:
            widgets.add(target_id)
    next_installed = {"apps": list(apps), "widgets": list(widgets)}
    data_map.set(txn, "installed", next_installed)
    desktop_value = data_map.get("desktop") or {}
    if not isinstance(desktop_value, dict):
        desktop_value = {}
    desktop_next = dict(desktop_value)
    desktop_installed = dict(desktop_next.get("installed") or {})
    desktop_installed["apps"] = list(apps)
    desktop_installed["widgets"] = list(widgets)
    desktop_next["installed"] = desktop_installed
    data_map.set(txn, "desktop", desktop_next)
    _log.debug(
        "toggle install webspace=%s type=%s target=%s apps=%s widgets=%s",
        webspace_id,
        item_type,
        target_id,
        sorted(apps),
        sorted(widgets),
    )


def _toggle_install(webspace_id: str, item_type: str, target_id: str) -> None:
    with get_ydoc(webspace_id) as ydoc:
        with ydoc.begin_transaction() as txn:
            _apply_install_toggle(webspace_id, ydoc, txn, item_type, target_id)


def _toggle_install_async(webspace_id: str, item_type: str, target_id: str) -> None:
    async def _worker() -> None:
        async with async_get_ydoc(webspace_id) as ydoc:
            with ydoc.begin_transaction() as txn:
                _apply_install_toggle(webspace_id, ydoc, txn, item_type, target_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_worker())
    else:
        loop.create_task(_worker(), name=f"web-desktop-toggle-{webspace_id}")


@subscribe("desktop.toggleInstall")
def on_toggle_install(evt) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    item_type = payload.get("type")
    target_id = payload.get("id")
    if item_type not in ("app", "widget") or not target_id:
        return
    live_applied = mutate_live_room(webspace_id, lambda doc, txn: _apply_install_toggle(webspace_id, doc, txn, item_type, str(target_id)))
    if not live_applied:
        _log.debug("mutate_live_room skipped for toggle webspace=%s type=%s target=%s", webspace_id, item_type, target_id)
    _toggle_install_async(webspace_id, item_type, str(target_id))


@subscribe("desktop.webspace.create")
def on_webspace_create(evt) -> None:
    """
    Deprecated: webspace creation is now handled by core runtime
    (see WebspaceScenarioRuntime desktop.webspace.create handler).
    """
    return


@subscribe("desktop.webspace.rename")
def on_webspace_rename(evt) -> None:
    """
    Deprecated: webspace rename is now handled by core runtime.
    """
    return


@subscribe("desktop.webspace.delete")
def on_webspace_delete(evt) -> None:
    """
    Deprecated: webspace delete is now handled by core runtime.
    """
    return


@subscribe("desktop.webspace.refresh")
def on_webspace_refresh(evt) -> None:  # noqa: ARG001
    """
    Deprecated: webspace listing refresh is now handled by core runtime.
    """
    return


@subscribe("desktop.webspace.reload")
def on_webspace_reload(evt) -> None:
    """
    Re-seed the current webspace from its scenario, effectively
    rebuilding ui/data/registry for debugging or recovery.
    """
    """
    Deprecated: webspace reload is now handled by core runtime.
    """
    return


@subscribe("desktop.webspace.reset")
def on_webspace_reset(evt) -> None:
    """
    Hard reset of the current webspace from its scenario. For now this
    mirrors desktop.webspace.reload behaviour; it is introduced as a
    separate event so that future versions can differentiate between
    soft reload (updatable-only) and full reset.
    """
    """
    Deprecated: webspace reset is now handled by core runtime.
    """
    return
