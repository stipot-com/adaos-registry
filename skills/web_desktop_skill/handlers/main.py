from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List

import asyncio
import re
import secrets
import time
from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.decorators import subscribe
from adaos.services.yjs.doc import get_ydoc, async_get_ydoc, mutate_live_room
from adaos.apps.workspaces import index as workspace_index
from adaos.apps.yjs.y_store import ystore_path_for_webspace, get_ystore_for_webspace
from adaos.apps.yjs.y_bootstrap import ensure_webspace_seeded_from_scenario
from adaos.apps.yjs.webspace import default_webspace_id

_log = logging.getLogger("skills.web_desktop")
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
    meta = payload.get("_meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict):
        token = meta.get("webspace_id")
        if token:
            return str(token)
    return str(payload.get("webspace_id") or payload.get("workspace_id") or "default")


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
        loop.create_task(_worker(), name=f"web-desktop-catalog-{webspace_id}")


@subscribe("scenarios.synced")
def on_scenario_synced(evt) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id(payload)
    _log.info("scenario synced for webspace %s", webspace_id)
    _rebuild_async(webspace_id)
    _sync_webspace_listing_async()


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
    _ACTIVE.setdefault(webspace_id, {})[f"{space}:{skill}"] = decl
    _log.info("skill %s activated in webspace %s (%s)", skill, webspace_id, space)
    _rebuild_async(webspace_id)


@subscribe("skills.rolledback")
def on_skill_rolled_back(evt) -> None:
    payload = _payload(evt)
    skill = payload.get("skill_name")
    if not skill:
        return
    space = str(payload.get("space") or "default")
    webspace_id = _webspace_id(payload)
    active = _ACTIVE.get(webspace_id)
    if active and active.pop(f"{space}:{skill}", None) is not None:
        _log.info("skill %s rolled back in webspace %s (%s)", skill, webspace_id, space)
        _rebuild_async(webspace_id)


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

        y_server.rooms.pop(webspace_id, None)
    except Exception:
        pass
    _sync_webspace_listing_async()


@subscribe("desktop.webspace.refresh")
def on_webspace_refresh(evt) -> None:  # noqa: ARG001
    _sync_webspace_listing_async()
