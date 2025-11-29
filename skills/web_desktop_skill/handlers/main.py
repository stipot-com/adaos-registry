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
from adaos.services.capacity import get_local_capacity
from adaos.services.agent_context import get_ctx as get_agent_ctx
from adaos.services.eventbus import emit as bus_emit_sync
from adaos.apps.workspaces import index as workspace_index
from adaos.apps.yjs.y_store import ystore_path_for_webspace, get_ystore_for_webspace
from adaos.apps.yjs.y_bootstrap import ensure_webspace_seeded_from_scenario
from adaos.apps.yjs.webspace import default_webspace_id
from adaos.apps.yjs.seed import SEED

_log = logging.getLogger("skills.web_desktop")

# During static validation, handlers are imported in a lightweight subprocess
# without a full AdaOS runtime. In that case, avoid requiring AgentContext
# at import time to let validation introspect decorators safely.
if os.environ.get("ADAOS_VALIDATE") == "1":
    _ctx = None  # type: ignore[assignment]
else:
    _ctx = require_ctx("skills.web_desktop_skill")
_ACTIVE: Dict[str, Dict[str, Dict[str, Any]]] = {}
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
            # Special-case weather snapshot path to avoid fragile Y-type juggling.
            if path == "data/weather/current":
                data_map = doc.get_map("data")
                weather = data_map.get("weather")
                if isinstance(weather, dict):
                    current = weather.get("current")
                else:
                    current = None
                if current is not None:
                    continue
                base_weather = dict(weather or {})
                # Deep-copy default to detach from the original spec.
                try:
                    snapshot = json.loads(json.dumps(default))
                except Exception:
                    snapshot = dict(default or {})
                base_weather.setdefault("current", snapshot)
                data_map.set(txn, "weather", base_weather)
                continue

            # Fallback: support simple one-level paths like "data/foo".
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


def _preload_weather_for_space(webspace_id: str, defaults: Dict[str, Any] | None) -> None:
    """
    Fire a best-effort weather.city_changed event so that weather_skill
    pre-populates a live snapshot for the given webspace.
    """
    city = None
    try:
        path_spec = (defaults or {}).get("data/weather/current")
        if isinstance(path_spec, dict):
            raw = path_spec.get("city")
            if raw:
                city = str(raw)
    except Exception:
        city = None
    if not city:
        city = "Moscow"
    payload = {"webspace_id": webspace_id, "workspace_id": webspace_id, "city": city}
    try:
        ctx = get_agent_ctx()
        bus_emit_sync(ctx.bus, "weather.city_changed", payload, "web_desktop_skill")
        _log.info("preloaded weather snapshot webspace=%s city=%s", webspace_id, city)
    except Exception:
        _log.warning("failed to preload weather snapshot webspace=%s city=%s", webspace_id, city, exc_info=True)


def _rebuild_catalog(webspace_id: str) -> None:
    with get_ydoc(webspace_id) as ydoc:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        registry_map = ydoc.get_map("registry")

        scenario_id = ui_map.get("current_scenario") or "web_desktop"
        scenarios_data = data_map.get("scenarios") or {}
        scenario_entry = scenarios_data.get(scenario_id) if isinstance(scenarios_data, dict) else {}
        base_catalog = scenario_entry.get("catalog") if isinstance(scenario_entry, dict) else {}
        scenario_apps = [it for it in (base_catalog.get("apps") or []) if isinstance(it, dict)]
        scenario_widgets = [it for it in (base_catalog.get("widgets") or []) if isinstance(it, dict)]

        scenario_registry = registry_map.get("scenarios") or {}
        raw_registry_entry = (
            scenario_registry.get(scenario_id) if isinstance(scenario_registry, dict) else {}
        )
        registry_entry = raw_registry_entry if isinstance(raw_registry_entry, dict) else {}
        registry_entry = registry_entry or {}
        base_registry_modals = [str(x) for x in (registry_entry.get("modals") or [])]
        base_registry_widgets = [str(x) for x in (registry_entry.get("widgets") or [])]

        skill_decls = list((_ACTIVE.get(webspace_id) or {}).values())
        skill_apps: List[Dict[str, Any]] = []
        skill_widgets: List[Dict[str, Any]] = []
        skill_registry_modals: List[List[str]] = []
        skill_registry_widgets: List[List[str]] = []
        for decl in skill_decls:
            skill_name = decl.get("skill") or ""
            space = decl.get("space") or "default"
            source = f"skill:{skill_name}"
            dev_flag = space == "dev"
            for app in decl.get("apps") or []:
                if isinstance(app, dict):
                    skill_apps.append(_mark_entry(app, source=source, dev=dev_flag))
            for widget in decl.get("widgets") or []:
                if isinstance(widget, dict):
                    skill_widgets.append(_mark_entry(widget, source=source, dev=dev_flag))
            reg = decl.get("registry") or {}
            mod_spec = reg.get("modals") or {}
            if isinstance(mod_spec, dict):
                skill_registry_modals.append([str(k) for k in mod_spec.keys()])
            else:
                skill_registry_modals.append([str(x) for x in mod_spec])
            wid_spec = reg.get("widgets") or {}
            if isinstance(wid_spec, dict):
                skill_registry_widgets.append([str(k) for k in wid_spec.keys()])
            else:
                skill_registry_widgets.append([str(x) for x in wid_spec])

        merged_apps = _merge_by_id(
            [_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_apps]
            + skill_apps
        )
        merged_widgets = _merge_by_id(
            [_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_widgets]
            + skill_widgets
        )
        merged_registry = {
            "modals": _merge_registry_lists(base_registry_modals, skill_registry_modals),
            "widgets": _merge_registry_lists(base_registry_widgets, skill_registry_widgets),
        }

        installed_current = data_map.get("installed") or {}
        if not isinstance(installed_current, dict):
            installed_current = {}
        filtered_installed = _filter_installed(installed_current, merged_apps, merged_widgets)

        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "catalog", {"apps": merged_apps, "widgets": merged_widgets})
            data_map.set(txn, "installed", filtered_installed)
            registry_map.set(txn, "merged", merged_registry)


def _ensure_weather_seed(webspace_id: str) -> None:
    """
    Ensure that data.weather has at least a default snapshot for the given webspace.

    This is used as a best-effort bootstrap so that the weather widget has
    something to render immediately after activation of weather_skill.
    """
    seed_data = (SEED.get("data") or {}).get("weather")
    if not isinstance(seed_data, dict):
        return

    def _mutator(doc, txn) -> None:
        data_map = doc.get_map("data")
        existing = data_map.get("weather")
        if existing:
            return
        data_map.set(txn, "weather", seed_data)

    try:
        applied = mutate_live_room(webspace_id, _mutator)
        if applied:
            _log.info("seeded default weather snapshot for webspace %s", webspace_id)
        else:
            _log.info("weather seed skipped (room inactive) webspace=%s", webspace_id)
    except Exception:
        _log.warning("failed to seed default weather snapshot for webspace=%s", webspace_id, exc_info=True)


def _bootstrap_active_skills_from_capacity() -> None:
    """
    Bootstrap _ACTIVE from node.yaml capacity so that skills activated via CLI
    (before the hub/API is running) are visible to the web desktop once the
    runtime starts.
    """
    try:
        cap = get_local_capacity()
        skills = cap.get("skills") or []
    except Exception:
        skills = []
    if not isinstance(skills, list) or not skills:
        return
    try:
        rows = workspace_index.list_workspaces()
        targets = [row.workspace_id for row in rows] or [default_webspace_id()]
    except Exception:
        targets = [default_webspace_id()]
    for rec in skills:
        if not isinstance(rec, dict):
            continue
        if not rec.get("active", True):
            continue
        name = rec.get("name") or rec.get("id")
        if not name:
            continue
        space = "dev" if rec.get("dev") else "default"
        decl = _load_webui(str(name), space)
        if not decl:
            continue
        defaults = decl.get("ydoc_defaults") or {}
        for ws_id in targets:
            _ACTIVE.setdefault(ws_id, {})[f"{space}:{name}"] = decl
            if isinstance(defaults, dict) and defaults:
                _apply_ydoc_defaults(ws_id, defaults)
                if str(name) == "weather_skill":
                    _preload_weather_for_space(ws_id, defaults)


_bootstrap_active_skills_from_capacity()


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


def _seed_webspace_async(
    webspace_id: str,
    scenario_id: str | None = None,
    post: Callable[[], None] | None = None,
) -> None:
    async def _worker() -> None:
        ystore = get_ystore_for_webspace(webspace_id)
        try:
            await ensure_webspace_seeded_from_scenario(
                ystore,
                webspace_id=webspace_id,
                default_scenario_id=scenario_id or _DEFAULT_SCENARIO_ID,
            )
            # Ensure core data namespaces like data.weather are present
            # even if the scenario does not define them explicitly.
            _ensure_weather_seed(webspace_id)
        finally:
            # YStore is process-wide singleton; do not stop it here.
            pass
        if callable(post):
            try:
                post()
            except Exception:
                _log.exception("post-seed hook failed for webspace %s", webspace_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_worker())
    else:
        loop.create_task(_worker(), name=f"webspace-seed-{webspace_id}")


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


def _rebuild_async(webspace_id: str) -> None:
    async def _worker() -> None:
        async with async_get_ydoc(webspace_id) as ydoc:
            ui_map = ydoc.get_map("ui")
            data_map = ydoc.get_map("data")
            registry_map = ydoc.get_map("registry")
            scenario_id = ui_map.get("current_scenario") or "web_desktop"
            scenarios_ui = ui_map.get("scenarios") or {}
            scenario_ui_entry = (
                scenarios_ui.get(scenario_id) if isinstance(scenarios_ui, dict) else {}
            )
            scenario_app_ui = (
                scenario_ui_entry.get("application")
                if isinstance(scenario_ui_entry, dict)
                else {}
            )
            if not isinstance(scenario_app_ui, dict):
                scenario_app_ui = {}
            scenarios_data = data_map.get("scenarios") or {}
            scenario_entry = scenarios_data.get(scenario_id) if isinstance(scenarios_data, dict) else {}
            base_catalog = scenario_entry.get("catalog") if isinstance(scenario_entry, dict) else {}
            scenario_apps = [it for it in (base_catalog.get("apps") or []) if isinstance(it, dict)]
            scenario_widgets = [it for it in (base_catalog.get("widgets") or []) if isinstance(it, dict)]

            scenario_registry = registry_map.get("scenarios") or {}
            registry_entry = scenario_registry.get(scenario_id) if isinstance(scenario_registry, dict) else {}
            registry_entry = registry_entry or {}
            base_registry_modals = [str(x) for x in (registry_entry.get("modals") or [])]
            base_registry_widgets = [str(x) for x in (registry_entry.get("widgets") or [])]

            debug_mode = os.getenv("ADAOS_LOG_LEVEL", "").upper() == "DEBUG"
            skill_decls: List[Dict[str, Any]]
            if debug_mode:
                # In DEBUG mode, bypass the in-memory _ACTIVE cache and
                # reload webui.json for each active skill from disk so
                # changes are reflected without restarting the hub.
                try:
                    cap = get_local_capacity()
                    skills = cap.get("skills") or []
                except Exception:
                    skills = []
                skill_decls = []
                for rec in skills:
                    if not isinstance(rec, dict) or not rec.get("active", True):
                        continue
                    name = rec.get("name") or rec.get("id")
                    if not name:
                        continue
                    space = "dev" if rec.get("dev") else "default"
                    decl = _load_webui(str(name), space)
                    if decl:
                        skill_decls.append(decl)
                _log.debug("rebuild webspace=%s using %d fresh skill declarations (DEBUG mode)", webspace_id, len(skill_decls))
            else:
                # Default: use cached declarations initialised at startup
                # and updated via skills.activated/skills.rolledback.
                skill_decls = list((_ACTIVE.get(webspace_id) or {}).values())
            skill_apps: List[Dict[str, Any]] = []
            skill_widgets: List[Dict[str, Any]] = []
            skill_registry_modals: List[List[str]] = []
            skill_registry_widgets: List[List[str]] = []
            for decl in skill_decls:
                skill_name = decl.get("skill") or ""
                space = decl.get("space") or "default"
                source = f"skill:{skill_name}"
                dev_flag = space == "dev"
                for app in decl.get("apps") or []:
                    if isinstance(app, dict):
                        skill_apps.append(_mark_entry(app, source=source, dev=dev_flag))
                for widget in decl.get("widgets") or []:
                    if isinstance(widget, dict):
                        skill_widgets.append(_mark_entry(widget, source=source, dev=dev_flag))
                reg = decl.get("registry") or {}
                mod_spec = reg.get("modals") or {}
                if isinstance(mod_spec, dict):
                    skill_registry_modals.append([str(k) for k in mod_spec.keys()])
                else:
                    skill_registry_modals.append([str(x) for x in mod_spec])
                wid_spec = reg.get("widgets") or {}
                if isinstance(wid_spec, dict):
                    skill_registry_widgets.append([str(k) for k in wid_spec.keys()])
                else:
                    skill_registry_widgets.append([str(x) for x in wid_spec])

            merged_apps = _merge_by_id([_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_apps] + skill_apps)
            merged_widgets = _merge_by_id([_mark_entry(it, source=f"scenario:{scenario_id}", dev=False) for it in scenario_widgets] + skill_widgets)
            merged_registry = {
                "modals": _merge_registry_lists(base_registry_modals, skill_registry_modals),
                "widgets": _merge_registry_lists(base_registry_widgets, skill_registry_widgets),
            }

            installed_current = data_map.get("installed") or {}
            if not isinstance(installed_current, dict):
                installed_current = {}
            filtered_installed = _filter_installed(installed_current, merged_apps, merged_widgets)

            # Debug trace: compare scenario desktop.topbar with current ui.application
            try:
                current_app = ui_map.get("application") or {}
                scenario_topbar = (scenario_app_ui.get("desktop") or {}).get("topbar")
                current_topbar = (
                    (current_app.get("desktop") or {}).get("topbar")
                    if isinstance(current_app, dict)
                    else None
                )
                _log.debug(
                    "rebuild webspace=%s scenarioTopbar=%s ui.application.desktop.topbar(before)=%s",
                    webspace_id,
                    scenario_topbar,
                    current_topbar,
                )
            except Exception:
                pass

            # Merge scenario-defined modals with skill-provided modal schemas.
            merged_modals_map: Dict[str, Any] = {}
            base_modals_map = {}
            try:
                raw = scenario_app_ui.get("modals") if isinstance(scenario_app_ui, dict) else None
                if isinstance(raw, dict):
                    base_modals_map = raw
            except Exception:
                base_modals_map = {}
            for key, value in (base_modals_map or {}).items():
                merged_modals_map[str(key)] = value
            for decl in skill_decls:
                reg = decl.get("registry") or {}
                mod_spec = reg.get("modals") or {}
                if not isinstance(mod_spec, dict):
                    continue
                for key, value in mod_spec.items():
                    token = str(key)
                    if token and token not in merged_modals_map:
                        merged_modals_map[token] = value
            # Build final application section with merged modals.
            app_with_modals: Dict[str, Any] = dict(scenario_app_ui)
            if merged_modals_map:
                app_with_modals["modals"] = merged_modals_map

            with ydoc.begin_transaction() as txn:
                # Keep ui.application in sync with the current scenario's
                # application section and enrich it with skill-provided
                # modal schemas so that changes in scenario.json and
                # webui.json are reflected in the live UI.
                ui_map.set(txn, "application", app_with_modals)
                data_map.set(txn, "catalog", {"apps": merged_apps, "widgets": merged_widgets})
                data_map.set(txn, "installed", filtered_installed)
                registry_map.set(txn, "merged", merged_registry)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_worker())
    else:
        loop.create_task(_worker(), name=f"web-desktop-catalog-{webspace_id}")


@subscribe("scenarios.synced")
def on_scenario_synced(evt) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    _log.info("scenario synced for webspace %s", webspace_id)
    _rebuild_async(webspace_id)
    _sync_webspace_listing_async()
    # After a scenario is projected into a webspace, ensure that all
    # active skills have their YDoc defaults applied and weather
    # snapshots are preloaded, so widgets/modals have data on first load.
    try:
        cap = get_local_capacity()
        skills = cap.get("skills") or []
    except Exception:
        skills = []
    for rec in skills:
        if not isinstance(rec, dict) or not rec.get("active", True):
            continue
        name = rec.get("name") or rec.get("id")
        if not name:
            continue
        space = "dev" if rec.get("dev") else "default"
        decl = _load_webui(str(name), space)
        if not decl:
            continue
        defaults = decl.get("ydoc_defaults") or {}
        if isinstance(defaults, dict) and defaults:
            _apply_ydoc_defaults(webspace_id, defaults)
            if str(name) == "weather_skill":
                _preload_weather_for_space(webspace_id, defaults)


@subscribe("skills.activated")
def on_skill_activated(evt) -> None:
    payload = _payload(evt)
    skill = payload.get("skill_name")
    if not skill:
        return
    space = str(payload.get("space") or "default")
    webspace_id = _webspace_id(payload)
    decl = _load_webui(str(skill), space)
    if not decl:
        return
    defaults = decl.get("ydoc_defaults") or {}
    try:
        rows = workspace_index.list_workspaces()
        targets = [row.workspace_id for row in rows] or [webspace_id or default_webspace_id()]
    except Exception:
        targets = [webspace_id or default_webspace_id()]
    for ws_id in targets:
        _ACTIVE.setdefault(ws_id, {})[f"{space}:{skill}"] = decl
        _log.info("skill %s activated in webspace %s (%s)", skill, ws_id, space)
        _rebuild_async(ws_id)
        if isinstance(defaults, dict) and defaults:
            _apply_ydoc_defaults(ws_id, defaults)
            if str(skill) == "weather_skill":
                _preload_weather_for_space(ws_id, defaults)


@subscribe("skills.rolledback")
def on_skill_rolled_back(evt) -> None:
    payload = _payload(evt)
    skill = payload.get("skill_name")
    if not skill:
        return
    space = str(payload.get("space") or "default")
    webspace_id = _webspace_id(payload)
    try:
        rows = workspace_index.list_workspaces()
        targets = [row.workspace_id for row in rows] or [webspace_id or default_webspace_id()]
    except Exception:
        targets = [webspace_id or default_webspace_id()]
    for ws_id in targets:
        active = _ACTIVE.get(ws_id)
        if active and active.pop(f"{space}:{skill}", None) is not None:
            _log.info("skill %s rolled back in webspace %s (%s)", skill, ws_id, space)
            _rebuild_async(ws_id)


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
    payload = _payload(evt)
    requested = payload.get("id") or payload.get("webspace_id")
    display_name = payload.get("title")
    scenario_id = payload.get("scenario_id") or _DEFAULT_SCENARIO_ID
    webspace_id = _allocate_webspace_id(requested)
    _log.info("creating webspace %s (requested=%s)", webspace_id, requested)
    workspace_index.ensure_workspace(webspace_id)
    workspace_index.set_display_name(webspace_id, display_name or webspace_id)
    _seed_webspace_async(webspace_id, scenario_id=scenario_id, post=lambda: _rebuild_async(webspace_id))
    _sync_webspace_listing_async()


@subscribe("desktop.webspace.rename")
def on_webspace_rename(evt) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or "")
    title = str(payload.get("title") or "").strip()
    if not webspace_id or not title:
        return
    if not workspace_index.get_workspace(webspace_id):
        _log.warning("cannot rename missing webspace %s", webspace_id)
        return
    workspace_index.set_display_name(webspace_id, title)
    _sync_webspace_listing_async()


@subscribe("desktop.webspace.delete")
def on_webspace_delete(evt) -> None:
    payload = _payload(evt)
    webspace_id = str(payload.get("id") or "")
    if not webspace_id or webspace_id == default_webspace_id():
        return
    _log.info("deleting webspace %s", webspace_id)
    try:
        workspace_index.delete_workspace(webspace_id)
    except Exception as exc:
        _log.warning("failed to delete webspace %s: %s", webspace_id, exc)
        return
    _ACTIVE.pop(webspace_id, None)
    try:
        from adaos.apps.yjs.y_gateway import y_server  # pylint: disable=import-outside-toplevel
        from adaos.apps.yjs.y_store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

        y_server.rooms.pop(webspace_id, None)
        reset_ystore_for_webspace(webspace_id)
    except Exception:
        pass
    _sync_webspace_listing_async()


@subscribe("desktop.webspace.refresh")
def on_webspace_refresh(evt) -> None:  # noqa: ARG001
    _sync_webspace_listing_async()


@subscribe("desktop.webspace.reload")
def on_webspace_reload(evt) -> None:
    """
    Re-seed the current webspace from its scenario, effectively
    rebuilding ui/data/registry for debugging or recovery.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    scenario_id = str(payload.get("scenario_id") or _DEFAULT_SCENARIO_ID)
    if not webspace_id:
        return
    _log.info("reloading webspace %s from scenario %s", webspace_id, scenario_id)
    try:
        from adaos.apps.yjs.y_gateway import y_server  # pylint: disable=import-outside-toplevel
        from adaos.apps.yjs.y_store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

        try:
            y_server.rooms.pop(webspace_id, None)
        except Exception:
            pass
        try:
            reset_ystore_for_webspace(webspace_id)
        except Exception:
            pass
    except Exception:
        _log.warning("failed to reset ystore for webspace=%s", webspace_id, exc_info=True)

    _seed_webspace_async(webspace_id, scenario_id=scenario_id, post=lambda: _rebuild_async(webspace_id))
    _sync_webspace_listing_async()


@subscribe("desktop.webspace.reset")
def on_webspace_reset(evt) -> None:
    """
    Hard reset of the current webspace from its scenario. For now this
    mirrors desktop.webspace.reload behaviour; it is introduced as a
    separate event so that future versions can differentiate between
    soft reload (updatable-only) and full reset.
    """
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    scenario_id = str(payload.get("scenario_id") or _DEFAULT_SCENARIO_ID)
    if not webspace_id:
        return
    _log.info("resetting webspace %s from scenario %s", webspace_id, scenario_id)
    try:
        from adaos.apps.yjs.y_gateway import y_server  # pylint: disable=import-outside-toplevel
        from adaos.apps.yjs.y_store import reset_ystore_for_webspace  # pylint: disable=import-outside-toplevel

        try:
            y_server.rooms.pop(webspace_id, None)
        except Exception:
            pass
        try:
            reset_ystore_for_webspace(webspace_id)
        except Exception:
            pass
    except Exception:
        _log.warning("failed to reset ystore for webspace=%s", webspace_id, exc_info=True)

    _seed_webspace_async(webspace_id, scenario_id=scenario_id, post=lambda: _rebuild_async(webspace_id))
    _sync_webspace_listing_async()
