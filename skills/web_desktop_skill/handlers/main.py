from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import asyncio

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.decorators import subscribe
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc

_log = logging.getLogger("skills.web_desktop")
_ctx = require_ctx("skills.web_desktop_skill")
_ACTIVE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _payload(evt: Any) -> Dict[str, Any]:
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload") or {}
        if isinstance(data, dict):
            return data
    if isinstance(evt, dict):
        return evt
    return {}


def _workspace_id(payload: Dict[str, Any]) -> str:
    return str(payload.get("workspace_id") or "default")


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
    reg_modals = registry.get("modals") or []
    reg_widgets = registry.get("widgets") or []

    return {
        "skill": skill_name,
        "space": space,
        "apps": [it for it in apps if isinstance(it, dict)],
        "widgets": [it for it in widgets if isinstance(it, dict)],
        "registry": {
            "modals": [str(x) for x in reg_modals if isinstance(x, (str, int))],
            "widgets": [str(x) for x in reg_widgets if isinstance(x, (str, int))],
        },
    }


def _mark_entry(entry: Dict[str, Any], *, source: str, dev: bool) -> Dict[str, Any]:
    data = dict(entry)
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


def _rebuild_catalog(workspace_id: str) -> None:
    with get_ydoc(workspace_id) as ydoc:
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
        registry_entry = scenario_registry.get(scenario_id) if isinstance(scenario_registry, dict) else {}
        base_registry_modals = [str(x) for x in (registry_entry.get("modals") or [])]
        base_registry_widgets = [str(x) for x in (registry_entry.get("widgets") or [])]

        skill_decls = list((_ACTIVE.get(workspace_id) or {}).values())
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
            skill_registry_modals.append([str(x) for x in (reg.get("modals") or [])])
            skill_registry_widgets.append([str(x) for x in (reg.get("widgets") or [])])

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

        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "catalog", {"apps": merged_apps, "widgets": merged_widgets})
            data_map.set(txn, "installed", filtered_installed)
            registry_map.set(txn, "merged", merged_registry)


def _rebuild_async(workspace_id: str) -> None:
    async def _worker() -> None:
        async with async_get_ydoc(workspace_id) as ydoc:
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
            registry_entry = scenario_registry.get(scenario_id) if isinstance(scenario_registry, dict) else {}
            base_registry_modals = [str(x) for x in (registry_entry.get("modals") or [])]
            base_registry_widgets = [str(x) for x in (registry_entry.get("widgets") or [])]

            skill_decls = list((_ACTIVE.get(workspace_id) or {}).values())
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
                skill_registry_modals.append([str(x) for x in (reg.get("modals") or [])])
                skill_registry_widgets.append([str(x) for x in (reg.get("widgets") or [])])

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

            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "catalog", {"apps": merged_apps, "widgets": merged_widgets})
                data_map.set(txn, "installed", filtered_installed)
                registry_map.set(txn, "merged", merged_registry)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_worker())
    else:
        loop.create_task(_worker(), name=f"web-desktop-catalog-{workspace_id}")


@subscribe("scenarios.synced")
def on_scenario_synced(evt) -> None:
    payload = _payload(evt)
    workspace_id = _workspace_id(payload)
    _log.info("scenario synced for workspace %s", workspace_id)
    _rebuild_async(workspace_id)


@subscribe("skills.activated")
def on_skill_activated(evt) -> None:
    payload = _payload(evt)
    skill = payload.get("skill_name")
    if not skill:
        return
    space = str(payload.get("space") or "default")
    workspace_id = _workspace_id(payload)
    decl = _load_webui(str(skill), space)
    if not decl:
        return
    _ACTIVE.setdefault(workspace_id, {})[f"{space}:{skill}"] = decl
    _log.info("skill %s activated in workspace %s (%s)", skill, workspace_id, space)
    _rebuild_async(workspace_id)


@subscribe("skills.rolledback")
def on_skill_rolled_back(evt) -> None:
    payload = _payload(evt)
    skill = payload.get("skill_name")
    if not skill:
        return
    space = str(payload.get("space") or "default")
    workspace_id = _workspace_id(payload)
    active = _ACTIVE.get(workspace_id)
    if active and active.pop(f"{space}:{skill}", None) is not None:
        _log.info("skill %s rolled back in workspace %s (%s)", skill, workspace_id, space)
        _rebuild_async(workspace_id)


def _toggle_install(workspace_id: str, item_type: str, target_id: str) -> None:
    with get_ydoc(workspace_id) as ydoc:
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
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "installed", {"apps": list(apps), "widgets": list(widgets)})


def _toggle_install_async(workspace_id: str, item_type: str, target_id: str) -> None:
    async def _worker() -> None:
        async with async_get_ydoc(workspace_id) as ydoc:
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
            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "installed", {"apps": list(apps), "widgets": list(widgets)})

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_worker())
    else:
        loop.create_task(_worker(), name=f"web-desktop-toggle-{workspace_id}")


@subscribe("desktop.toggleInstall")
def on_toggle_install(evt) -> None:
    payload = _payload(evt)
    workspace_id = _workspace_id(payload)
    item_type = payload.get("type")
    target_id = payload.get("id")
    if item_type not in ("app", "widget") or not target_id:
        return
    _toggle_install_async(workspace_id, item_type, str(target_id))
