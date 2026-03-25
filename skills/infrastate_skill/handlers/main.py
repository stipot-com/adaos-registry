from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from adaos.build_info import BUILD_INFO
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet, skill_memory_get, skill_memory_set
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest, slot_status
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services import node_config as _node_config
from adaos.services.realtime_sidecar import realtime_sidecar_diag_path, realtime_sidecar_enabled
from adaos.services.reliability import assess_transport_diagnostics, reliability_snapshot
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.scenario.webspace_runtime import WebspaceService
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.skill.manager import SkillManager
from adaos.adapters.db import SqliteSkillRegistry

from packaging.version import Version, InvalidVersion

_log = logging.getLogger("skills.infrastate_skill")
_UI_STATE_KEY = "infrastate.ui_state"
_EVENTS_STATE_KEY = "infrastate.events"

def _normalize_node_names(value: Any, *, limit: int = 8) -> list[str]:
    # Local copy for backward/forward compatibility with core.
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item or "").strip() for item in value]
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        token = str(item or "").strip()
        if not token:
            continue
        folded = token.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(token[:80])
        if len(result) >= limit:
            break
    return result


load_config = _node_config.load_config


def persist_node_names(node_names: Any):
    names = _normalize_node_names(node_names)
    setter = getattr(_node_config, "set_node_names", None)
    if callable(setter):
        return setter(names)

    conf = load_config()
    node_settings = getattr(conf, "node_settings", None)
    if node_settings is not None and hasattr(node_settings, "node_names"):
        try:
            setattr(node_settings, "node_names", names)
        except Exception:
            pass
    elif hasattr(conf, "node_names"):
        try:
            setattr(conf, "node_names", names)
        except Exception:
            pass

    save = getattr(_node_config, "save_config", None) or getattr(_node_config, "save_node", None)
    if callable(save):
        save(conf)
    return conf


def lang_res() -> dict[str, str]:
    return {}


def _base_dir() -> Path:
    try:
        ctx = get_ctx()
        base = getattr(ctx.paths, "base_dir", None)
        if callable(base):
            return Path(base()).expanduser().resolve()
        if base:
            return Path(base).expanduser().resolve()
    except Exception:
        pass
    raw = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".adaos").resolve()


def _repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "src" / "adaos" / "build_info.py").exists():
            return parent
    return None


def _skill_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "skill.yaml").exists():
            return parent
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _read_last_jsonl_record(path: Path, *, max_bytes: int = 131072) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in reversed(chunk.splitlines()):
        text = str(line or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _read_recent_jsonl_records(path: Path, *, max_bytes: int = 262144, limit: int = 200) -> list[dict[str, Any]]:
    try:
        if not path.exists() or not path.is_file():
            return []
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for line in chunk.splitlines():
        text = str(line or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items[-limit:]


def _safe_version(v: Any) -> Version | None:
    if v is None:
        return None
    raw = str(v).strip()
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _read_remote_manifest_version(*, skill_id: str) -> str | None:
    """
    Best-effort resolve remote version for a skill without touching the worktree.
    """
    try:
        ctx = get_ctx()
    except Exception:
        return None

    settings = getattr(ctx, "settings", None)
    git = getattr(ctx, "git", None)
    repo = getattr(ctx, "skills_repo", None)
    if git is None or repo is None:
        return None

    meta = repo.get(skill_id)
    if meta is None:
        return None
    local_path = Path(getattr(meta, "path", ctx.paths.skills_dir() / skill_id))

    monorepo_url = getattr(settings, "skills_monorepo_url", None) if settings else None
    monorepo_branch = (getattr(settings, "skills_monorepo_branch", None) if settings else None) or "main"

    if monorepo_url:
        skills_root = Path(ctx.paths.skills_dir())
        if (skills_root / ".git").exists():
            repo_root = skills_root
        elif (skills_root.parent / ".git").exists():
            repo_root = skills_root.parent
        else:
            return None
        try:
            git.fetch(str(repo_root), remote="origin", branch=monorepo_branch)
        except Exception:
            pass
        candidates = [
            f"origin/{monorepo_branch}:skills/{skill_id}/skill.yaml",
            f"origin/{monorepo_branch}:skills/{skill_id}/manifest.yaml",
            f"origin/{monorepo_branch}:skills/{skill_id}/adaos.skill.yaml",
        ]
    else:
        repo_root = local_path
        if not (repo_root / ".git").exists():
            return None
        try:
            git.fetch(str(repo_root), remote="origin")
        except Exception:
            pass
        candidates = [
            "origin/HEAD:skill.yaml",
            "origin/HEAD:manifest.yaml",
            "origin/HEAD:adaos.skill.yaml",
        ]

    for spec in candidates:
        try:
            raw = git.show(str(repo_root), spec)
        except Exception:
            continue
        try:
            data = yaml.safe_load(raw) or {}
        except Exception:
            continue
        ver = data.get("version")
        if ver is None:
            continue
        s = str(ver).strip()
        if s:
            return s
    return None


def _skills_items() -> list[dict[str, Any]]:
    try:
        ctx = get_ctx()
    except Exception:
        return []

    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )

    try:
        metas = ctx.skills_repo.list() or []
    except Exception:
        metas = []

    out: list[dict[str, Any]] = []
    for meta in metas:
        try:
            name = str(getattr(meta, "id", None).value if getattr(meta, "id", None) else getattr(meta, "name", "") or "").strip()
        except Exception:
            name = ""
        if not name:
            continue
        local_version = str(getattr(meta, "version", "") or "").strip()

        slot = ""
        try:
            st = mgr.runtime_status(name)
            slot = str(st.get("active_slot") or "").strip()
        except Exception:
            slot = ""

        remote_version = str(_read_remote_manifest_version(skill_id=name) or "").strip()
        update_available = False
        lv = _safe_version(local_version)
        rv = _safe_version(remote_version)
        if lv is not None and rv is not None and rv > lv:
            update_available = True

        out.append(
            {
                "name": name,
                "version": local_version,
                "slot": slot,
                "remote_version": remote_version,
                "update_available": update_available,
            }
        )

    out.sort(key=lambda x: x.get("name") or "")
    return out


def _update_actions(conf, ui_state: dict[str, Any], reliability: dict[str, Any]) -> list[dict[str, Any]]:
    selected_node_id = str(ui_state.get("selected_node_id") or getattr(conf, "node_id", "") or "").strip()
    local_node_id = str(getattr(conf, "node_id", "") or "").strip()
    role = str(getattr(conf, "role", "") or "").strip().lower()
    target_kind = "local" if not selected_node_id or selected_node_id == local_node_id else "member"
    title = "Update skills & scenarios"
    if target_kind == "member":
        title = f"Update skills & scenarios ({selected_node_id[:8]})"
    description = "Sync workspace sources for skills and scenarios and refresh runtime projections."
    if target_kind == "member" and role != "hub":
        return [
            {
                "id": "adaos_update",
                "title": title,
                "status": "warn",
                "description": "Remote update is available only from hub",
            }
        ]
    return [
        {
            "id": "adaos_update",
            "title": title,
            "status": "ok",
            "description": description,
        }
    ]


def _adaos_update_local(*, dry_run: bool = False) -> dict[str, Any]:
    """
    Best-effort "adaos update" for workspace artifacts:
      - sync skills repo sparse checkout
      - sync scenarios repo sparse checkout
      - refresh runtime projections for installed skills
    """
    try:
        ctx = get_ctx()
    except Exception as exc:
        return {"ok": False, "error": f"no_ctx: {exc}"}

    errors: dict[str, str] = {}
    payload: dict[str, Any] = {"ok": True, "dry_run": bool(dry_run)}

    if dry_run:
        payload["note"] = "dry_run"
        return payload

    skill_mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )
    try:
        from adaos.adapters.db import SqliteScenarioRegistry
        from adaos.services.scenario.manager import ScenarioManager

        scenario_mgr = ScenarioManager(
            repo=ctx.scenarios_repo,
            registry=SqliteScenarioRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
        )
    except Exception as exc:
        scenario_mgr = None
        errors["scenario_mgr"] = f"{type(exc).__name__}: {exc}"

    try:
        skill_mgr.sync()
        payload["skills_synced"] = True
    except Exception as exc:
        payload["skills_synced"] = False
        errors["skills_sync"] = f"{type(exc).__name__}: {exc}"

    if scenario_mgr is not None:
        try:
            scenario_mgr.sync()
            payload["scenarios_synced"] = True
        except Exception as exc:
            payload["scenarios_synced"] = False
            errors["scenarios_sync"] = f"{type(exc).__name__}: {exc}"

    runtime_updated: list[str] = []
    runtime_errors: dict[str, str] = {}
    try:
        metas = ctx.skills_repo.list() or []
    except Exception:
        metas = []
    for meta in metas:
        try:
            name = str(getattr(meta, "id", None).value if getattr(meta, "id", None) else getattr(meta, "name", "") or "").strip()
        except Exception:
            name = ""
        if not name:
            continue
        try:
            res = skill_mgr.runtime_update(name, space="workspace")
            if isinstance(res, dict) and res.get("ok"):
                runtime_updated.append(name)
        except Exception as exc:
            runtime_errors[name] = f"{type(exc).__name__}: {exc}"

    payload["runtime_updated"] = sorted(set(runtime_updated))
    if runtime_errors:
        errors["runtime_update"] = f"{len(runtime_errors)} skills failed"
        payload["runtime_update_errors"] = runtime_errors

    if errors:
        payload["ok"] = False
        payload["errors"] = errors
    return payload


def _route_info(conf) -> tuple[str | None, bool | None]:
    role = str(getattr(conf, "role", "") or "").strip().lower()
    if role == "hub":
        return "hub", None
    if role != "member":
        return None, None
    try:
        from adaos.services.subnet.link_client import get_member_link_client

        connected = bool(get_member_link_client().is_connected())
        return ("ws" if connected else "none"), connected
    except Exception:
        return None, None


def _local_ready() -> bool:
    try:
        from adaos.services.bootstrap import is_ready

        return bool(is_ready())
    except Exception:
        lifecycle = runtime_lifecycle_snapshot()
        return bool(lifecycle.get("accepting_new_work"))


def _safe_json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _tail_text_file(path: Path, *, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-limit:].strip()


def _log_item(log_id: str, title: str, content: Any, *, status: str = "idle", preview_limit: int = 400) -> dict[str, Any]:
    text = _safe_json_text(content).strip()
    return {
        "id": log_id,
        "title": title,
        "status": status,
        "preview": text[-preview_limit:].strip(),
        "content": text[-4000:].strip(),
    }


def _ui_level_from_channel_status(status: str, *, stability_state: str = "") -> str:
    if status == "ready":
        if stability_state in {"flapping", "unstable"}:
            return "warn"
        return "ok"
    if status == "degraded":
        return "warn"
    if status == "down":
        return "error"
    return "idle"


def _effective_channel_view(
    channel_id: str,
    *,
    tree_item: dict[str, Any],
    diag_item: dict[str, Any],
    transport_assessment: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    status = str(tree_item.get("status") or diag_item.get("status") or "unknown")
    stability = diag_item.get("stability") if isinstance(diag_item.get("stability"), dict) else {}
    effective_state = str(stability.get("state") or "unknown")
    if channel_id not in {"root_control", "route"}:
        return status, effective_state, stability
    transport_state = str(transport_assessment.get("state") or "").strip().lower()
    if transport_state not in {"down", "unstable", "flapping"}:
        return status, effective_state, stability
    if status == "ready":
        status = "degraded"
    elif status == "unknown" and transport_state == "down":
        status = "down"
    if effective_state in {"stable", "unknown"} or transport_state == "down":
        effective_state = transport_state
    return status, effective_state, stability


def _transport_diag_snapshot() -> dict[str, Any]:
    sidecar_diag = None
    sidecar_enabled = False
    try:
        sidecar_enabled = bool(realtime_sidecar_enabled())
        sidecar_diag = _read_last_jsonl_record(realtime_sidecar_diag_path())
    except Exception:
        sidecar_diag = None

    hub_diag = None
    hub_diag_recent: list[dict[str, Any]] = []
    try:
        raw_path = str(os.getenv("HUB_NATS_WS_DIAG_FILE", "") or "").strip()
        if raw_path:
            hub_diag_path = Path(raw_path)
            if not hub_diag_path.is_absolute():
                hub_diag_path = Path.cwd() / hub_diag_path
        else:
            hub_diag_path = _base_dir() / "diagnostics" / "nats_ws_diag.jsonl"
        hub_diag = _read_last_jsonl_record(hub_diag_path)
        hub_diag_recent = _read_recent_jsonl_records(hub_diag_path, limit=512)
    except Exception:
        hub_diag = None
        hub_diag_recent = []

    transport_assessment = assess_transport_diagnostics(hub_diag_recent, now_ts=time.time())

    return {
        "sidecar_enabled": sidecar_enabled,
        "sidecar_diag": sidecar_diag,
        "hub_ws_diag": hub_diag,
        "hub_ws_diag_recent": hub_diag_recent[-12:],
        "root_transport_assessment": transport_assessment,
    }


def _hub_root_strategy(reliability: dict[str, Any], transport_diag: dict[str, Any]) -> dict[str, Any]:
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    strategy = (
        runtime.get("hub_root_transport_strategy")
        if isinstance(runtime.get("hub_root_transport_strategy"), dict)
        else {}
    )
    merged = dict(strategy) if isinstance(strategy, dict) else {}
    if not isinstance(merged.get("assessment"), dict):
        merged["assessment"] = (
            transport_diag.get("root_transport_assessment")
            if isinstance(transport_diag.get("root_transport_assessment"), dict)
            else {}
        )
    return merged


def _reliability_snapshot(conf, lifecycle: dict[str, Any]) -> dict[str, Any]:
    route_mode, connected_to_hub = _route_info(conf)
    return reliability_snapshot(
        node_id=str(getattr(conf, "node_id", "") or ""),
        subnet_id=str(getattr(conf, "subnet_id", "") or ""),
        role=str(getattr(conf, "role", "") or ""),
        node_names=list(getattr(conf, "node_names", []) or []),
        local_ready=_local_ready(),
        node_state=str(lifecycle.get("node_state") or "ready"),
        draining=bool(lifecycle.get("draining")),
        route_mode=route_mode,
        connected_to_hub=connected_to_hub,
    )


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        existing = ctx.projections.resolve("subnet", "infrastate.snapshot")
        if existing:
            return
        skill_root = _skill_root()
        if skill_root is None:
            return
        manifest_path = skill_root / "skill.yaml"
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return
        entries = payload.get("data_projections") or []
        if not isinstance(entries, list) or not entries:
            return
        ctx.projections.load_entries(entries)
        _log.debug("loaded infrastate data_projections entries=%d", len(entries))
    except Exception:
        _log.debug("failed to load infrastate data_projections", exc_info=True)


def _git_text(*args: str) -> str:
    repo = _repo_root()
    if repo is None:
        return ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return (completed.stdout or "").strip()
    except Exception:
        return ""


def _build_meta() -> dict[str, Any]:
    active_manifest = active_slot_manifest() or {}
    return {
        "version": BUILD_INFO.version,
        "build_date": BUILD_INFO.build_date,
        "git_sha": _git_text("rev-parse", "HEAD"),
        "git_short_sha": _git_text("rev-parse", "--short", "HEAD"),
        "git_branch": _git_text("rev-parse", "--abbrev-ref", "HEAD"),
        "git_subject": _git_text("show", "-s", "--format=%s", "HEAD"),
        "repo_root": str(_repo_root() or ""),
        "runtime_version": str(active_manifest.get("target_version") or ""),
        "runtime_git_commit": str(active_manifest.get("git_commit") or ""),
        "runtime_git_short_commit": str(active_manifest.get("git_short_commit") or ""),
        "runtime_git_branch": str(active_manifest.get("git_branch") or active_manifest.get("target_rev") or ""),
        "runtime_git_subject": str(active_manifest.get("git_subject") or ""),
    }


def _ui_state() -> dict[str, Any]:
    raw = skill_memory_get(_UI_STATE_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _write_ui_state(**updates: Any) -> dict[str, Any]:
    payload = dict(_ui_state())
    payload.update(updates)
    skill_memory_set(_UI_STATE_KEY, payload)
    return payload


def _hub_member_connection_state(reliability: dict[str, Any]) -> dict[str, Any]:
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    state = runtime.get("hub_member_connection_state")
    return state if isinstance(state, dict) else {}


def _node_label(node_names: Any, *, fallback: str) -> str:
    if isinstance(node_names, list):
        for item in node_names:
            token = str(item or "").strip()
            if token:
                return token
    return fallback


def _node_tabs(conf, ui_state: dict[str, Any], reliability: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected_node_id = str(ui_state.get("selected_node_id") or "").strip()
    local_node_id = str(getattr(conf, "node_id", "") or "")
    role = str(getattr(conf, "role", "") or "").strip().lower()
    local_names = list(getattr(conf, "node_names", []) or [])
    items: list[dict[str, Any]] = [
        {
            "id": local_node_id,
            "label": _node_label(local_names, fallback="hub" if role == "hub" else "member"),
            "title": "Local node",
            "role": role,
            "node_id": local_node_id,
            "node_names": local_names,
            "kind": "local",
        }
    ]
    conn_state = _hub_member_connection_state(reliability)
    members = conn_state.get("known_members") if isinstance(conn_state.get("known_members"), list) else []
    if not members:
        members = conn_state.get("members") if isinstance(conn_state.get("members"), list) else []
    if role == "hub":
        for index, member in enumerate(members, start=1):
            if not isinstance(member, dict):
                continue
            node_id = str(member.get("node_id") or "").strip()
            if not node_id:
                continue
            member_names = member.get("node_names") if isinstance(member.get("node_names"), list) else []
            connected = bool(member.get("connected"))
            observed_via = str(member.get("observed_via") or "").strip()
            items.append(
                {
                    "id": node_id,
                    "label": _node_label(member_names, fallback="member" if index == 1 else f"member {index}"),
                    "title": "Connected member" if connected else ("Observed member" if observed_via == "subnet_directory" else "Member"),
                    "role": "member",
                    "node_id": node_id,
                    "node_names": member_names,
                    "kind": "member",
                    "state": str(member.get("state") or "connected"),
                    "connected": connected,
                    "observed_via": observed_via,
                }
            )
    valid_ids = {str(item.get("id") or "") for item in items}
    if not selected_node_id or selected_node_id not in valid_ids:
        selected_node_id = local_node_id
    selected = next((item for item in items if str(item.get("id") or "") == selected_node_id), items[0])
    tabs: list[dict[str, Any]] = []
    for item in items:
        label = str(item.get("label") or "")
        if str(item.get("id") or "") == selected_node_id:
            label = f"{label} *"
        tabs.append({**item, "label": label, "selected": str(item.get("id") or "") == selected_node_id})
    return tabs, selected


def _selected_node_editor(conf, selected_node: dict[str, Any]) -> dict[str, Any]:
    local_node_id = str(getattr(conf, "node_id", "") or "")
    selected_node_id = str(selected_node.get("node_id") or "")
    is_local = selected_node_id == local_node_id
    names = selected_node.get("node_names") if isinstance(selected_node.get("node_names"), list) else []
    return {
        "names_csv": ", ".join(str(item or "").strip() for item in names if str(item or "").strip()),
        "editable": bool(is_local or str(getattr(conf, "role", "") or "").strip().lower() == "hub"),
        "scope": "local" if is_local else "remote-member",
        "node_id": selected_node_id,
        "label": str(selected_node.get("label") or ""),
    }


def _selected_yjs_webspace_id(ui_state: dict[str, Any], reliability: dict[str, Any]) -> str:
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
    selected = str(ui_state.get("selected_yjs_webspace_id") or "").strip()
    if selected and selected in webspaces:
        return selected
    default_id = default_webspace_id()
    if default_id in webspaces:
        return default_id
    if webspaces:
        return sorted(str(key) for key in webspaces.keys())[0]
    return default_id


def _yjs_webspace_tabs(conf, ui_state: dict[str, Any], reliability: dict[str, Any], selected_node: dict[str, Any]) -> list[dict[str, Any]]:
    if str(selected_node.get("kind") or "local") != "local":
        return []
    if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
        return []
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    sync_runtime = runtime.get("sync_runtime") if isinstance(sync_runtime.get("sync_runtime"), dict) else {}
    webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
    selected_id = _selected_yjs_webspace_id(ui_state, reliability)
    items: list[dict[str, Any]] = []
    for index, webspace_id in enumerate(sorted(str(key) for key in webspaces.keys()), start=1):
        entry = webspaces.get(webspace_id) if isinstance(webspaces.get(webspace_id), dict) else {}
        label = "default" if webspace_id == default_webspace_id() else webspace_id
        if webspace_id == selected_id:
            label = f"{label} *"
        items.append(
            {
                "id": webspace_id,
                "label": label,
                "title": "Selected Yjs webspace" if webspace_id == selected_id else f"Yjs webspace {index}",
                "subtitle": (
                    f"{entry.get('log_mode') or '-'} | "
                    f"replay={entry.get('replay_window_entries') or 0}/"
                    f"{entry.get('replay_window_limit') or 0}"
                ),
                "selected": webspace_id == selected_id,
            }
        )
    return items


def _selected_member_entry(reliability: dict[str, Any], node_id: str) -> dict[str, Any]:
    conn_state = _hub_member_connection_state(reliability)
    members = conn_state.get("known_members") if isinstance(conn_state.get("known_members"), list) else []
    if not members:
        members = conn_state.get("members") if isinstance(conn_state.get("members"), list) else []
    for item in members:
        if isinstance(item, dict) and str(item.get("node_id") or "") == node_id:
            return item
    return {}


def _remote_build_meta(snapshot: dict[str, Any]) -> dict[str, Any]:
    build = snapshot.get("build") if isinstance(snapshot.get("build"), dict) else {}
    return {
        "version": str(build.get("version") or "unknown"),
        "build_date": str(build.get("build_date") or ""),
        "git_sha": "",
        "git_short_sha": "",
        "git_branch": "",
        "git_subject": "",
        "repo_root": "",
        "runtime_version": str(build.get("runtime_version") or build.get("version") or "unknown"),
        "runtime_git_commit": str(build.get("runtime_git_commit") or ""),
        "runtime_git_short_commit": str(build.get("runtime_git_short_commit") or ""),
        "runtime_git_branch": str(build.get("runtime_git_branch") or ""),
        "runtime_git_subject": str(build.get("runtime_git_subject") or ""),
    }


def _remote_status_payload(snapshot: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    update = snapshot.get("update_status") if isinstance(snapshot.get("update_status"), dict) else {}
    state = str(update.get("state") or member.get("snapshot_update_state") or member.get("last_hub_core_update_state") or member.get("state") or "connected")
    phase = str(update.get("phase") or member.get("snapshot_update_phase") or "")
    message = str(update.get("message") or "")
    if not message and snapshot:
        message = "remote member snapshot"
    if not message:
        message = "remote snapshot pending"
    return {
        "state": state,
        "phase": phase,
        "action": str(update.get("action") or member.get("last_hub_core_update_action") or ""),
        "message": message,
        "reason": str(update.get("reason") or ("subnet.member.snapshot" if snapshot else "subnet.member.snapshot.pending")),
        "target_rev": str(update.get("target_rev") or ""),
        "target_version": str(update.get("target_version") or ""),
        "target_slot": str(update.get("target_slot") or ""),
        "scheduled_for": update.get("scheduled_for"),
        "updated_at": update.get("updated_at") or snapshot.get("captured_at"),
        "finished_at": update.get("finished_at"),
    }


def _remote_last_result_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    last = snapshot.get("last_result") if isinstance(snapshot.get("last_result"), dict) else {}
    return {
        "state": str(last.get("state") or ""),
        "phase": str(last.get("phase") or ""),
        "message": str(last.get("message") or ""),
        "target_slot": str(last.get("target_slot") or ""),
        "finished_at": last.get("finished_at"),
        "validated_at": last.get("validated_at"),
    }


def _remote_slots_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), dict) else {}
    active_slot = str(slots.get("active_slot") or "")
    previous_slot = str(slots.get("previous_slot") or "")
    active_manifest = slots.get("active_manifest") if isinstance(slots.get("active_manifest"), dict) else {}
    slot_items: dict[str, Any] = {}
    if active_slot:
        slot_items[active_slot] = {
            "manifest": {
                "slot": str(active_manifest.get("slot") or active_slot),
                "target_rev": str(active_manifest.get("target_rev") or ""),
                "target_version": str(active_manifest.get("target_version") or ""),
                "git_commit": str(active_manifest.get("git_commit") or ""),
                "git_short_commit": str(active_manifest.get("git_short_commit") or ""),
                "git_branch": str(active_manifest.get("git_branch") or ""),
                "git_subject": str(active_manifest.get("git_subject") or ""),
            },
            "path": "",
        }
    if previous_slot and previous_slot not in slot_items:
        slot_items[previous_slot] = {"manifest": {"slot": previous_slot}, "path": ""}
    return {
        "active_slot": active_slot,
        "previous_slot": previous_slot,
        "slots": slot_items,
        "active_manifest": active_manifest,
    }


def _remote_lifecycle_payload(snapshot: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    node_state = str(snapshot.get("node_state") or member.get("snapshot_node_state") or member.get("state") or "connected")
    reason = str(snapshot.get("reason") or ("remote member snapshot" if snapshot else "remote snapshot pending"))
    return {
        "node_state": node_state,
        "reason": reason,
        "draining": bool(snapshot.get("draining")),
    }


def _remote_control_payload(snapshot: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    control = snapshot.get("hub_control_request") if isinstance(snapshot.get("hub_control_request"), dict) else {}
    request = control.get("request") if isinstance(control.get("request"), dict) else {}
    result = control.get("result") if isinstance(control.get("result"), dict) else {}
    member_result = member.get("last_control_result") if isinstance(member.get("last_control_result"), dict) else {}
    effective_result = result or member_result
    ok_value: Any = None
    if isinstance(effective_result, dict) and "ok" in effective_result:
        ok_value = effective_result.get("ok")
    return {
        "request_id": str(
            request.get("request_id")
            or member.get("last_control_request_id")
            or effective_result.get("request_id")
            or ""
        ).strip(),
        "action": str(
            request.get("action")
            or member.get("last_control_action")
            or effective_result.get("action")
            or ""
        ).strip(),
        "reason": str(request.get("reason") or member.get("last_control_reason") or "").strip(),
        "state": str(
            request.get("state")
            or ("completed" if ok_value is not None else ("requested" if request or member.get("last_control_request_id") else ""))
        ).strip(),
        "ok": ok_value,
        "error": str(control.get("error") or effective_result.get("error") or "").strip(),
        "requested_at": control.get("requested_at"),
        "completed_at": control.get("completed_at"),
        "result": effective_result if isinstance(effective_result, dict) else {},
        "request": request if isinstance(request, dict) else {},
    }


def _selected_node_projection(
    selected_node: dict[str, Any],
    *,
    reliability: dict[str, Any],
    status: dict[str, Any],
    last_result: dict[str, Any],
    slots_payload: dict[str, Any],
    lifecycle: dict[str, Any],
    build: dict[str, Any],
) -> dict[str, Any]:
    if str(selected_node.get("kind") or "local") == "local":
        return {
            "status": status,
            "last_result": last_result,
            "slots_payload": slots_payload,
            "lifecycle": lifecycle,
            "build": build,
            "selected_member": {},
            "selected_snapshot": {},
        }
    member = _selected_member_entry(reliability, str(selected_node.get("node_id") or ""))
    snapshot = member.get("node_snapshot") if isinstance(member.get("node_snapshot"), dict) else {}
    return {
        "status": _remote_status_payload(snapshot, member),
        "last_result": _remote_last_result_payload(snapshot),
        "slots_payload": _remote_slots_payload(snapshot),
        "lifecycle": _remote_lifecycle_payload(snapshot, member),
        "build": _remote_build_meta(snapshot),
        "selected_member": member,
        "selected_snapshot": snapshot,
    }


def _event_state() -> list[dict[str, Any]]:
    raw = skill_memory_get(_EVENTS_STATE_KEY, [])
    return raw if isinstance(raw, list) else []


def _append_event(event_type: str, payload: Any) -> list[dict[str, Any]]:
    item = {
        "id": f"{event_type}:{int(time.time() * 1000)}",
        "type": str(event_type or "event"),
        "ts": time.time(),
        "preview": json.dumps(payload, ensure_ascii=False)[:400] if isinstance(payload, (dict, list)) else str(payload or "")[:400],
        "payload": payload if isinstance(payload, (dict, list)) else {"value": str(payload or "")},
    }
    items = list(_event_state())
    items.append(item)
    items = items[-40:]
    skill_memory_set(_EVENTS_STATE_KEY, items)
    return items


def _self_base_url(conf) -> str:
    explicit = str(os.getenv("ADAOS_SELF_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    hub_url = str(getattr(conf, "hub_url", "") or "").strip()
    if hub_url.startswith("http://127.0.0.1:") or hub_url.startswith("http://localhost:"):
        return hub_url.rstrip("/")
    return "http://127.0.0.1:8777"


def _self_headers(conf) -> dict[str, str]:
    token = str(getattr(conf, "token", None) or os.getenv("ADAOS_TOKEN") or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = token
    return headers


def _post_local_admin(conf, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.post(
        _self_base_url(conf) + path,
        headers=_self_headers(conf),
        json=body or {},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"ok": True, "response": payload}


def _extract_action_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "action", "target"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nested_key in ("item", "selected"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                found = _extract_action_id(nested)
                if found:
                    return found
    return ""


def _extract_param(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload.get(key)
        for nested_key in ("item", "selected"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict) and key in nested:
                return nested.get(key)
    return None


def _status_log_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    command = str(status.get("command") or "").strip()
    if command:
        items.append(_log_item("command", "command", command, status="idle"))
    validation_error = status.get("validation_error")
    if validation_error:
        items.append(_log_item("validation-error", "validation-error", validation_error, status="warn"))
    rollback = status.get("rollback")
    if rollback:
        items.append(_log_item("rollback", "rollback", rollback, status="warn"))
    for key, title, state in (
        ("manifest", "manifest", "idle"),
        ("plan", "plan", "idle"),
    ):
        value = status.get(key)
        if value:
            items.append(_log_item(key, title, value, status=state))
    for key, title in (("stdout", "stdout"), ("stderr", "stderr")):
        text = str(status.get(key) or "").strip()
        if not text:
            continue
        items.append(_log_item(key, title, text[-4000:].strip(), status="ok" if key == "stdout" else "warn"))
    for key, title in (("validation_stdout", "validation-stdout"), ("validation_stderr", "validation-stderr")):
        text = str(status.get(key) or "").strip()
        if not text:
            continue
        items.append(_log_item(key, title, text[-4000:].strip(), status="warn"))
    validation_logs = status.get("validation_logs") if isinstance(status.get("validation_logs"), dict) else {}
    for key, title in (("stdout_path", "validation-log-stdout"), ("stderr_path", "validation-log-stderr")):
        raw_path = str(validation_logs.get(key) or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        tail = _tail_text_file(path)
        payload = {"path": str(path), "tail": tail}
        items.append(_log_item(f"validation-{key}", title, payload, status="warn"))
    last_error = str(_ui_state().get("last_error") or "").strip()
    if last_error:
        items.append(_log_item("ui-error", "ui-error", last_error[-4000:].strip(), status="warn"))
    return items


def _effective_update_log_report(current: dict[str, Any], last_result: dict[str, Any]) -> dict[str, Any]:
    current_state = str(current.get("state") or "").strip().lower()
    last_state = str(last_result.get("state") or "").strip().lower()
    if current_state and current_state != "idle":
        return current
    if last_state and last_state != "idle":
        return last_result
    return current or last_result


def _build_items(build: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "runtime_version",
            "title": "Runtime version",
            "status": "ok",
            "description": str(build.get("runtime_version") or build.get("version") or "unknown"),
            "subtitle": str(build.get("runtime_git_short_commit") or ""),
        },
        {
            "id": "runtime_head",
            "title": "Runtime commit",
            "status": "ok",
            "description": str(build.get("runtime_git_commit") or build.get("git_sha") or "unknown"),
            "subtitle": str(build.get("runtime_git_branch") or build.get("git_branch") or ""),
        },
        {
            "id": "runtime_subject",
            "title": "Runtime subject",
            "status": "idle",
            "description": str(build.get("runtime_git_subject") or build.get("git_subject") or "unknown"),
            "subtitle": str(build.get("runtime_git_short_commit") or build.get("git_short_sha") or ""),
        },
        {
            "id": "version",
            "title": "AdaOS version",
            "status": "ok",
            "description": str(build.get("version") or "unknown"),
            "subtitle": str(build.get("build_date") or ""),
        },
        {
            "id": "git_head",
            "title": "Source Git HEAD",
            "status": "idle",
            "description": str(build.get("git_short_sha") or "unknown"),
            "subtitle": str(build.get("git_subject") or build.get("git_branch") or ""),
        },
        {
            "id": "git_full_head",
            "title": "Source commit",
            "status": "idle",
            "description": str(build.get("git_sha") or "unknown"),
            "subtitle": str(build.get("git_branch") or ""),
        },
        {
            "id": "git_branch",
            "title": "Source branch",
            "status": "idle",
            "description": str(build.get("git_branch") or "unknown"),
            "subtitle": str(build.get("repo_root") or ""),
        },
    ]


def _slot_items(slots_payload: dict[str, Any]) -> list[dict[str, Any]]:
    active = str(slots_payload.get("active_slot") or "")
    previous = str(slots_payload.get("previous_slot") or "")
    raw_slots = slots_payload.get("slots") or {}
    items: list[dict[str, Any]] = []
    if not isinstance(raw_slots, dict):
        return items
    for slot_id, slot_meta in sorted(raw_slots.items()):
        meta = slot_meta if isinstance(slot_meta, dict) else {}
        manifest = meta.get("manifest") if isinstance(meta.get("manifest"), dict) else {}
        badges: list[str] = []
        if slot_id == active:
            badges.append("active")
        if slot_id == previous:
            badges.append("previous")
        items.append(
            {
                "id": str(slot_id),
                "title": f"Slot {slot_id}",
                "status": "ok" if slot_id == active else "idle",
                "subtitle": str(manifest.get("git_short_commit") or manifest.get("target_rev") or manifest.get("target_version") or "empty"),
                "description": str(meta.get("path") or ""),
                "version": str(manifest.get("target_version") or ""),
                "target_rev": str(manifest.get("target_rev") or ""),
                "git_commit": str(manifest.get("git_commit") or ""),
                "git_short_commit": str(manifest.get("git_short_commit") or ""),
                "git_branch": str(manifest.get("git_branch") or ""),
                "git_subject": str(manifest.get("git_subject") or ""),
                "badges": badges,
            }
        )
    return items


def _step_items(status: dict[str, Any], slots_payload: dict[str, Any], lifecycle: dict[str, Any], build: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(status.get("state") or "idle")
    phase = str(status.get("phase") or "")
    active = str(slots_payload.get("active_slot") or "--")
    previous = str(slots_payload.get("previous_slot") or "--")
    node_state = str(lifecycle.get("node_state") or "ready")
    return [
        {"id": "lifecycle", "title": "Lifecycle", "status": node_state, "description": str(lifecycle.get("reason") or "ready")},
        {"id": "update_state", "title": "Update state", "status": state, "description": phase or state},
        {"id": "active_slot", "title": "Active slot", "status": "ok", "description": active},
        {"id": "previous_slot", "title": "Previous slot", "status": "idle", "description": previous},
        {"id": "build", "title": "Build", "status": "ok", "description": str(build.get("version") or "unknown")},
        {"id": "commit", "title": "Runtime commit", "status": "idle", "description": str(build.get("runtime_git_short_commit") or build.get("git_short_sha") or "unknown")},
        {"id": "branch", "title": "Runtime branch", "status": "idle", "description": str(build.get("runtime_git_branch") or build.get("git_branch") or "unknown")},
        {"id": "target_rev", "title": "Target rev", "status": "idle", "description": str(status.get("target_rev") or "--")},
        {"id": "drain_timeout", "title": "Drain timeout", "status": "idle", "description": str(status.get("drain_timeout_sec") or "--")},
        {"id": "signal_delay", "title": "Signal delay", "status": "idle", "description": str(status.get("signal_delay_sec") or "--")},
        {"id": "command", "title": "Command", "status": "idle", "description": str(status.get("command") or "--")},
    ]


def _countdown_remaining_sec(status: dict[str, Any]) -> int:
    try:
        scheduled_for = float(status.get("scheduled_for") or 0.0)
    except Exception:
        scheduled_for = 0.0
    if scheduled_for <= 0:
        return 0
    return max(0, int(round(scheduled_for - time.time())))


def _summary_buttons(status: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(status.get("state") or "")
    remaining_sec = _countdown_remaining_sec(status)
    if state not in {"countdown", "draining", "stopping"}:
        return []
    if remaining_sec <= 0 and state == "countdown":
        return []
    label = "Cancel update"
    if remaining_sec > 0:
        label = f"{label} ({remaining_sec}s)"
    buttons = [{"id": "cancel_update", "label": label, "title": label, "kind": "danger"}]
    reason = str(status.get("reason") or "").strip().lower()
    if reason.startswith("github.push:") or reason.startswith("root.release:"):
        buttons.insert(0, {"id": "refuse_update", "label": "Refuse update", "title": "Refuse update", "kind": "danger"})
    return buttons


def _member_summary_buttons(
    status: dict[str, Any],
    lifecycle: dict[str, Any],
    selected_member: dict[str, Any],
    conf,
) -> list[dict[str, Any]]:
    role = str(getattr(conf, "role", "") or "").strip().lower()
    if role != "hub":
        return []
    if not bool(selected_member.get("connected")):
        return []
    state = str(status.get("state") or selected_member.get("snapshot_update_state") or "connected").strip().lower()
    buttons: list[dict[str, Any]] = []
    startable_states = {"idle", "failed", "succeeded", "validated", "cancelled", "rolled_back", "connected"}
    cancelable_states = {"countdown", "draining", "stopping", "restarting", "applying", "validate", "validated"}
    if state in startable_states:
        buttons.append({"id": "member_start_update", "label": "Start update", "title": "Start update"})
    if state in cancelable_states:
        remaining_sec = _countdown_remaining_sec(status)
        label = "Cancel update"
        if remaining_sec > 0 and state == "countdown":
            label = f"{label} ({remaining_sec}s)"
        buttons.append({"id": "member_cancel_update", "label": label, "title": label, "kind": "danger"})
    buttons.append({"id": "member_rollback", "label": "Rollback slot", "title": "Rollback slot", "kind": "danger"})
    if not bool(lifecycle.get("draining")):
        buttons.append({"id": "member_drain", "label": "Drain mode", "title": "Drain mode", "kind": "danger"})
    return buttons


def _reliability_summary_note(reliability: dict[str, Any], transport_diag: dict[str, Any]) -> str:
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
    diagnostics = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
    protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
    hub_member_channels = runtime.get("hub_member_channels") if isinstance(runtime.get("hub_member_channels"), dict) else {}
    hub_member_connection_state = runtime.get("hub_member_connection_state") if isinstance(runtime.get("hub_member_connection_state"), dict) else {}
    sidecar_runtime = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
    root = overview.get("hub_root") if isinstance(overview.get("hub_root"), dict) else {}
    route = overview.get("hub_root_browser") if isinstance(overview.get("hub_root_browser"), dict) else {}
    member_link = overview.get("hub_member") if isinstance(overview.get("hub_member"), dict) else {}
    member_sync = overview.get("member_hub_sync") if isinstance(overview.get("member_hub_sync"), dict) else {}
    root_diag = diagnostics.get("root_control") if isinstance(diagnostics.get("root_control"), dict) else {}
    route_diag = diagnostics.get("route") if isinstance(diagnostics.get("route"), dict) else {}
    root_status = str(root.get("effective_status") or "unknown")
    root_state = str(root.get("effective_state") or "unknown")
    route_status = str(route.get("effective_status") or "unknown")
    route_state = str(route.get("effective_state") or "unknown")
    note = (
        f"realtime hub-root={root_status}/{root_state}"
        f" hub-root-browser={route_status}/{route_state}"
    )
    member_link_status = str(member_link.get("effective_status") or "").strip()
    member_link_state = str(member_link.get("effective_state") or "").strip()
    if member_link_status:
        note += f" hub-member={member_link_status}/{member_link_state or 'unknown'}"
    member_sync_status = str(member_sync.get("effective_status") or "").strip()
    member_sync_state = str(member_sync.get("effective_state") or "").strip()
    if member_sync_status:
        note += f" member-sync={member_sync_status}/{member_sync_state or 'unknown'}"
    root_incident = str(root_diag.get("last_incident_class") or "").strip()
    route_incident = str(route_diag.get("last_incident_class") or "").strip()
    if root_incident:
        note += f" root_incident={root_incident}"
    if route_incident:
        note += f" route_incident={route_incident}"
    protocol_assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
    coverage = protocol.get("hardening_coverage") if isinstance(protocol.get("hardening_coverage"), dict) else {}
    control_authority = protocol.get("control_authority") if isinstance(protocol.get("control_authority"), dict) else {}
    route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
    route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
    route_control_flow = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
    route_frame_flow = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
    outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
    tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
    llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
    streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
    control_lifecycle_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
        ),
        {},
    )
    core_update_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.integration.github_core_update"
        ),
        {},
    )
    protocol_state = str(protocol_assessment.get("state") or "").strip()
    if protocol_state:
        note += f" protocol={protocol_state}"
    if control_authority.get("state"):
        note += f" control_auth={control_authority.get('state')}"
    if coverage:
        note += f" coverage={coverage.get('covered_flows') or 0}/{coverage.get('total_flows') or 0}"
    if route_runtime.get("pending_events"):
        note += f" route_backlog={route_runtime.get('pending_events')}"
    if route_control_flow.get("state"):
        note += f" route_ctrl={route_control_flow.get('state')}"
    if route_frame_flow.get("state"):
        note += f" route_frame={route_frame_flow.get('state')}"
    if tg_outbox.get("size"):
        note += f" tg_outbox={tg_outbox.get('size')}"
    if tg_outbox.get("durable_store") is not None:
        note += f" tg_durable={'yes' if tg_outbox.get('durable_store') else 'no'}"
        note += f" tg_persisted={tg_outbox.get('persisted_size') or 0}"
    if tg_outbox.get("idempotency_mode"):
        note += f" tg_mode={tg_outbox.get('idempotency_mode')}"
    if llm_outbox.get("idempotency_mode"):
        note += f" llm_mode={llm_outbox.get('idempotency_mode')}"
    if llm_outbox.get("cache_hit_total") or llm_outbox.get("cache_miss_total"):
        note += f" llm_cache={llm_outbox.get('cache_hit_total') or 0}/{llm_outbox.get('cache_miss_total') or 0}"
    if protocol.get("pending_ack_streams"):
        note += f" pending_acks={protocol.get('pending_ack_streams')}"
    if control_lifecycle_stream:
        note += (
            f" control_cursor="
            f"{control_lifecycle_stream.get('last_acked_cursor') or 0}/"
            f"{control_lifecycle_stream.get('last_issued_cursor') or 0}"
        )
        if control_lifecycle_stream.get("last_ack_ago_s") is not None:
            note += f" control_ack_age={control_lifecycle_stream.get('last_ack_ago_s')}"
    if core_update_stream:
        note += (
            f" core_update_cursor="
            f"{core_update_stream.get('last_acked_cursor') or 0}/"
            f"{core_update_stream.get('last_issued_cursor') or 0}"
        )
    if sidecar_runtime:
        note += (
            f" sidecar={sidecar_runtime.get('status') or 'unknown'}/"
            f"{sidecar_runtime.get('control_ready') or '-'}"
        )
        process = sidecar_runtime.get("process") if isinstance(sidecar_runtime.get("process"), dict) else {}
        if process.get("listener_pid"):
            note += f" sidecar_pid={process.get('listener_pid')}"
    if hub_member_channels:
        member_assessment = hub_member_channels.get("assessment") if isinstance(hub_member_channels.get("assessment"), dict) else {}
        channels = hub_member_channels.get("channels") if isinstance(hub_member_channels.get("channels"), dict) else {}
        member_command = channels.get("hub_member.command") if isinstance(channels.get("hub_member.command"), dict) else {}
        member_sync = channels.get("hub_member.sync") if isinstance(channels.get("hub_member.sync"), dict) else {}
        note += f" member={member_assessment.get('state') or 'unknown'}"
        if member_command.get("active_path"):
            note += f" member_cmd={member_command.get('active_path')}:{member_command.get('state') or '-'}"
        if member_sync.get("active_path"):
            note += f" member_sync={member_sync.get('active_path')}:{member_sync.get('state') or '-'}"
    if hub_member_connection_state:
        assessment = hub_member_connection_state.get("assessment") if isinstance(hub_member_connection_state.get("assessment"), dict) else {}
        note += f" member_link={assessment.get('state') or 'unknown'}"
        if hub_member_connection_state.get("member_total") is not None:
            note += f" members={hub_member_connection_state.get('member_total') or 0}"
        rollout = hub_member_connection_state.get("update_rollout") if isinstance(hub_member_connection_state.get("update_rollout"), dict) else {}
        rollout_counts = rollout.get("rollout_counts") if isinstance(rollout.get("rollout_counts"), dict) else {}
        snapshot_counts = rollout.get("snapshot_counts") if isinstance(rollout.get("snapshot_counts"), dict) else {}
        if rollout:
            note += f" member_rollout={rollout.get('state') or 'unknown'}"
            note += f" member_fresh={snapshot_counts.get('fresh') or 0}"
            note += f" member_pending={snapshot_counts.get('pending') or 0}"
            note += f" member_stale={snapshot_counts.get('stale') or 0}"
            note += f" member_progress={rollout_counts.get('in_progress') or 0}"
            note += f" member_failed={rollout_counts.get('failed') or 0}"
        if str(hub_member_connection_state.get("role") or "") == "member":
            hub = hub_member_connection_state.get("hub") if isinstance(hub_member_connection_state.get("hub"), dict) else {}
            if hub.get("last_hub_core_update"):
                mirrored = hub.get("last_hub_core_update") if isinstance(hub.get("last_hub_core_update"), dict) else {}
                if mirrored.get("state"):
                    note += f" hub_update={mirrored.get('state')}"
    return note


def _realtime_items(reliability: dict[str, Any], transport_diag: dict[str, Any]) -> list[dict[str, Any]]:
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    tree = runtime.get("readiness_tree") if isinstance(runtime.get("readiness_tree"), dict) else {}
    channel_diag = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
    channel_overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
    protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
    hub_member_channels = runtime.get("hub_member_channels") if isinstance(runtime.get("hub_member_channels"), dict) else {}
    hub_member_connection_state = runtime.get("hub_member_connection_state") if isinstance(runtime.get("hub_member_connection_state"), dict) else {}
    sidecar_runtime = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    signals = runtime.get("signals") if isinstance(runtime.get("signals"), dict) else {}
    strategy = _hub_root_strategy(reliability, transport_diag)
    transport_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}

    def _channel_item(channel_id: str, title: str, note: str = "") -> dict[str, Any]:
        tree_item = tree.get(channel_id) if isinstance(tree.get(channel_id), dict) else {}
        diag_item = channel_diag.get(channel_id) if isinstance(channel_diag.get(channel_id), dict) else {}
        signal_item = signals.get(channel_id) if isinstance(signals.get(channel_id), dict) else {}
        overview_item = None
        for item in channel_overview.values():
            if isinstance(item, dict) and str(item.get("channel_id") or "") == channel_id:
                overview_item = item
                break
        status, effective_state, stability = _effective_channel_view(
            channel_id,
            tree_item=tree_item,
            diag_item=diag_item,
            transport_assessment=transport_assessment,
        )
        if isinstance(overview_item, dict):
            status = str(overview_item.get("effective_status") or status)
            effective_state = str(overview_item.get("effective_state") or effective_state)
        if stability:
            description = (
                f"{status} | {effective_state} "
                f"score={stability.get('score') if stability.get('score') is not None else '?'}"
            )
        else:
            description = status
        subtitle = str(tree_item.get("summary") or diag_item.get("summary") or note or "").strip()
        incident_class = str(diag_item.get("last_incident_class") or "").strip()
        if incident_class:
            subtitle = f"{subtitle} | incident={incident_class}" if subtitle else f"incident={incident_class}"
        if note:
            subtitle = f"{subtitle} | {note}" if subtitle else note
        content = {
            "readiness": tree_item,
            "diagnostics": diag_item,
            "signal": signal_item,
        }
        return {
            "id": channel_id,
            "title": title,
            "status": _ui_level_from_channel_status(status, stability_state=effective_state),
            "description": description,
            "subtitle": subtitle,
            "content": _safe_json_text(content),
        }

    items = [
        _channel_item(
            "root_control",
            "Hub -> Root control",
            note="Repeated disconnects here indicate upstream realtime instability, even when the browser stays connected to the hub.",
        ),
        _channel_item(
            "route",
            "Hub -> Root -> Browser relay",
            note="This path matters for root-proxied browser traffic; direct browser -> hub still keeps this panel visible through local Yjs.",
        ),
        _channel_item(
            "sync",
            "Browser -> Hub sync",
            note="This panel is projected via local Yjs. If it updates while hub -> root is down, the browser-hub realtime path is healthy.",
        ),
        _channel_item(
            "hub_member",
            "Hub <-> Member control",
            note="This path carries member link control, mirrored update signals, node names, and remote runtime snapshots.",
        ),
        _channel_item(
            "member_hub_sync",
            "Member <-> Hub sync",
            note="This path reflects the active sync authority between member and hub instead of assuming fallback transport is healthy.",
        ),
    ]

    if strategy:
        assessment_state = str(transport_assessment.get("state") or "unknown")
        strategy_parts = [
            strategy.get("effective_transport") or strategy.get("requested_transport") or "unknown",
            assessment_state,
        ]
        subtitle_parts = [
            f"server={strategy.get('selected_server')}" if strategy.get("selected_server") else "",
            f"last={strategy.get('last_event')}" if strategy.get("last_event") else "",
            f"error={strategy.get('last_error')}" if strategy.get("last_error") else "",
        ]
        items.append(
            {
                "id": "hub_root_strategy",
                "title": "Hub-root strategy",
                "status": "warn"
                if assessment_state in {"unstable", "flapping", "down"}
                else "ok" if strategy.get("effective_transport") else "idle",
                "description": " | ".join([part for part in strategy_parts if part]),
                "subtitle": " | ".join([part for part in subtitle_parts if part]) or "current transport hypothesis",
                "content": _safe_json_text(strategy),
            }
        )

    if protocol:
        assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
        coverage = protocol.get("hardening_coverage") if isinstance(protocol.get("hardening_coverage"), dict) else {}
        classes = protocol.get("traffic_classes") if isinstance(protocol.get("traffic_classes"), dict) else {}
        control_cls = classes.get("control") if isinstance(classes.get("control"), dict) else {}
        route_cls = classes.get("route") if isinstance(classes.get("route"), dict) else {}
        route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
        route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
        route_control_flow = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
        route_frame_flow = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
        outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
        control_authority = protocol.get("control_authority") if isinstance(protocol.get("control_authority"), dict) else {}
        tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
        llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
        streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
        control_lifecycle_stream = next(
            (
                entry
                for entry in streams.values()
                if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
            ),
            {},
        )
        core_update_stream = next(
            (
                entry
                for entry in streams.values()
                if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.integration.github_core_update"
            ),
            {},
        )
        items.append(
            {
                "id": "hub_root_protocol",
                "title": "Hub-root protocol",
                "status": "warn"
                if str(assessment.get("state") or "") in {"pressure", "degraded"}
                else "ok",
                "description": (
                    f"{assessment.get('state') or 'unknown'} | "
                    f"coverage={coverage.get('covered_flows') or 0}/{coverage.get('total_flows') or 0} | "
                    f"control_subs={control_cls.get('active_subscriptions') or 0} | "
                    f"route_subs={route_cls.get('active_subscriptions') or 0} | "
                    f"control_auth={control_authority.get('state') or '-'} | "
                    f"route_ctrl={route_control_flow.get('state') or '-'} | "
                    f"route_frame={route_frame_flow.get('state') or '-'}"
                ),
                "subtitle": (
                    f"route_backlog={route_runtime.get('pending_events') or 0} | "
                    f"tg_outbox={tg_outbox.get('size') or 0} | "
                    f"tg_durable={'yes' if tg_outbox.get('durable_store') else 'no'} | "
                    f"tg_persisted={tg_outbox.get('persisted_size') or 0} | "
                    f"tg_mode={tg_outbox.get('idempotency_mode') or '-'} | "
                    f"llm_mode={llm_outbox.get('idempotency_mode') or '-'} | "
                    f"llm_cache={llm_outbox.get('cache_hit_total') or 0}/{llm_outbox.get('cache_miss_total') or 0} | "
                    f"pending_acks={protocol.get('pending_ack_streams') or 0} | "
                    f"control_cursor={control_lifecycle_stream.get('last_acked_cursor') or 0}/"
                    f"{control_lifecycle_stream.get('last_issued_cursor') or 0} | "
                    f"control_ack_age={control_lifecycle_stream.get('last_ack_ago_s') if control_lifecycle_stream.get('last_ack_ago_s') is not None else '-'} | "
                    f"core_update_cursor={core_update_stream.get('last_acked_cursor') or 0}/"
                    f"{core_update_stream.get('last_issued_cursor') or 0}"
                ),
                "content": _safe_json_text(protocol),
            }
        )

    if sync_runtime:
        webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
        selected_ws_id = str(sync_runtime.get("selected_webspace_id") or "").strip() or default_webspace_id()
        selected_ws = webspaces.get(selected_ws_id) if isinstance(webspaces.get(selected_ws_id), dict) else {}
        items.append(
            {
                "id": "yjs_sync_runtime",
                "title": "Yjs sync runtime",
                "status": "warn" if str(sync_runtime.get("state") or "") in {"pressure", "degraded"} else "ok",
                "description": (
                    f"{sync_runtime.get('state') or 'unknown'} | "
                    f"webspaces={sync_runtime.get('webspace_total') or 0} | "
                    f"active={sync_runtime.get('active_webspace_total') or 0} | "
                    f"compacted={sync_runtime.get('compacted_webspace_total') or 0}"
                ),
                "subtitle": (
                    f"selected={selected_ws_id}:{selected_ws.get('log_mode') or '-'} | "
                    f"replay={selected_ws.get('replay_window_entries') or 0}/"
                    f"{selected_ws.get('replay_window_limit') or 0} | "
                    f"backups={selected_ws.get('backup_total') or 0} | "
                    f"snapshot={'yes' if selected_ws.get('snapshot_exists') else 'no'} | "
                    f"last_backup_ago={selected_ws.get('last_backup_ago_s') if selected_ws.get('last_backup_ago_s') is not None else '-'}"
                ),
                "content": _safe_json_text(sync_runtime),
            }
        )

    if hub_member_channels:
        assessment = hub_member_channels.get("assessment") if isinstance(hub_member_channels.get("assessment"), dict) else {}
        channels = hub_member_channels.get("channels") if isinstance(hub_member_channels.get("channels"), dict) else {}
        command = channels.get("hub_member.command") if isinstance(channels.get("hub_member.command"), dict) else {}
        event = channels.get("hub_member.event") if isinstance(channels.get("hub_member.event"), dict) else {}
        sync = channels.get("hub_member.sync") if isinstance(channels.get("hub_member.sync"), dict) else {}
        presence = channels.get("hub_member.presence") if isinstance(channels.get("hub_member.presence"), dict) else {}
        route_channel = channels.get("hub_member.route") if isinstance(channels.get("hub_member.route"), dict) else {}
        items.append(
            {
                "id": "hub_member_semantics",
                "title": "Hub-member semantic channels",
                "status": "warn"
                if str(assessment.get("state") or "") in {"degraded", "fallback", "transitioning"}
                else "ok",
                "description": (
                    f"{assessment.get('state') or 'unknown'} | "
                    f"cmd={command.get('active_path') or '-'}:{command.get('state') or '-'} | "
                    f"evt={event.get('active_path') or '-'}:{event.get('state') or '-'} | "
                    f"sync={sync.get('active_path') or '-'}:{sync.get('state') or '-'} | "
                    f"presence={presence.get('active_path') or '-'}:{presence.get('state') or '-'} | "
                    f"route={route_channel.get('active_path') or '-'}:{route_channel.get('state') or '-'}"
                ),
                "subtitle": (
                    f"reason={assessment.get('reason') or '-'} | "
                    f"cmd_freeze={command.get('freeze_remaining_s') or 0}s | "
                    f"sync_freeze={sync.get('freeze_remaining_s') or 0}s | "
                    f"one active authority path per channel"
                ),
                "content": _safe_json_text(hub_member_channels),
            }
        )

    if hub_member_connection_state:
        assessment = hub_member_connection_state.get("assessment") if isinstance(hub_member_connection_state.get("assessment"), dict) else {}
        role = str(hub_member_connection_state.get("role") or "").strip()
        if role == "hub":
            members = hub_member_connection_state.get("members") if isinstance(hub_member_connection_state.get("members"), list) else []
            rollout = hub_member_connection_state.get("update_rollout") if isinstance(hub_member_connection_state.get("update_rollout"), dict) else {}
            rollout_counts = rollout.get("rollout_counts") if isinstance(rollout.get("rollout_counts"), dict) else {}
            snapshot_counts = rollout.get("snapshot_counts") if isinstance(rollout.get("snapshot_counts"), dict) else {}
            member_titles = [
                f"{str(item.get('label') or item.get('node_id') or 'member')}:{str(item.get('snapshot_state') or item.get('state') or 'connected')}/{str(item.get('snapshot_update_state') or '-')}"
                for item in members[:4]
                if isinstance(item, dict)
            ]
            description = (
                f"{assessment.get('state') or 'unknown'} | "
                f"members={hub_member_connection_state.get('member_total') or 0} | "
                f"broadcasts={hub_member_connection_state.get('hub_core_update_broadcast_total') or 0} | "
                f"rollout={rollout.get('state') or '-'} | "
                f"fresh={snapshot_counts.get('fresh') or 0} | "
                f"pending={snapshot_counts.get('pending') or 0} | "
                f"stale={snapshot_counts.get('stale') or 0} | "
                f"in_progress={rollout_counts.get('in_progress') or 0} | "
                f"failed={rollout_counts.get('failed') or 0}"
            )
            subtitle = " | ".join(member_titles) if member_titles else "No connected members"
        else:
            hub = hub_member_connection_state.get("hub") if isinstance(hub_member_connection_state.get("hub"), dict) else {}
            mirrored = hub.get("last_hub_core_update") if isinstance(hub.get("last_hub_core_update"), dict) else {}
            follow = hub.get("last_follow_result") if isinstance(hub.get("last_follow_result"), dict) else {}
            description = (
                f"{assessment.get('state') or 'unknown'} | "
                f"state={hub_member_connection_state.get('state') or '-'} | "
                f"hub_update={mirrored.get('state') or '-'} | "
                f"follow_ok={follow.get('ok') if isinstance(follow, dict) and 'ok' in follow else '-'}"
            )
            subtitle = (
                f"hub={hub.get('hub_node_id') or '-'} | "
                f"last_msg_ago={hub.get('last_message_ago_s') if hub.get('last_message_ago_s') is not None else '-'} | "
                f"follow_err={hub.get('last_follow_error') or '-'}"
            )
        items.append(
            {
                "id": "hub_member_connection_state",
                "title": "Hub-member connections",
                "status": "warn" if str(assessment.get("state") or "") in {"degraded", "fallback"} else "ok",
                "description": description,
                "subtitle": subtitle,
                "content": _safe_json_text(hub_member_connection_state),
            }
        )

    if sidecar_runtime:
        provenance = (
            sidecar_runtime.get("transport_provenance")
            if isinstance(sidecar_runtime.get("transport_provenance"), dict)
            else {}
        )
        process = sidecar_runtime.get("process") if isinstance(sidecar_runtime.get("process"), dict) else {}
        items.append(
            {
                "id": "realtime_sidecar",
                "title": "Realtime sidecar",
                "status": "warn"
                if str(sidecar_runtime.get("status") or "") in {"degraded", "unknown"}
                else "ok"
                if sidecar_runtime.get("enabled")
                else "idle",
                "description": (
                    f"{sidecar_runtime.get('phase') or 'unknown'} | "
                    f"transport={sidecar_runtime.get('local_listener_state') or '-'}"
                    f"/{sidecar_runtime.get('remote_session_state') or '-'} | "
                    f"control={sidecar_runtime.get('control_ready') or '-'} | "
                    f"route={sidecar_runtime.get('route_ready') or '-'}"
                ),
                "subtitle": (
                    f"remote={provenance.get('remote_url') or provenance.get('selected_server') or '-'} | "
                    f"connects={provenance.get('remote_connect_total') or 0}/"
                    f"{provenance.get('remote_connect_fail_total') or 0} | "
                    f"quarantine={provenance.get('remote_quarantine_total') or 0} | "
                    f"superseded={provenance.get('superseded_total') or 0} | "
                    f"pid={process.get('listener_pid') or '-'} | "
                    f"adopted={'yes' if process.get('adopted_listener') else 'no'}"
                ),
                "content": _safe_json_text(sidecar_runtime),
            }
        )

    sidecar_diag = transport_diag.get("sidecar_diag") if isinstance(transport_diag.get("sidecar_diag"), dict) else None
    hub_ws_diag = transport_diag.get("hub_ws_diag") if isinstance(transport_diag.get("hub_ws_diag"), dict) else None
    if sidecar_diag or hub_ws_diag or transport_diag.get("sidecar_enabled"):
        source = "sidecar" if sidecar_diag else "hub"
        record = sidecar_diag or hub_ws_diag or {}
        last_error = str(record.get("last_error") or "").strip()
        description = f"{source} transport"
        if isinstance(record, dict) and record.get("remote_url"):
            description += f" | {record.get('remote_url')}"
        subtitle = last_error or "latest transport snapshot"
        items.append(
            {
                "id": "transport_diag",
                "title": "Realtime transport diag",
                "status": "warn"
                if transport_assessment.get("state") in {"unstable", "flapping"} or last_error
                else "ok"
                if record
                else "idle",
                "description": f"{description} | {transport_assessment.get('state') or 'unknown'}",
                "subtitle": subtitle,
                "content": _safe_json_text(
                    {
                        "sidecar_enabled": bool(transport_diag.get("sidecar_enabled")),
                        "sidecar_diag": sidecar_diag,
                        "hub_ws_diag": hub_ws_diag,
                        "hub_ws_diag_recent": transport_diag.get("hub_ws_diag_recent"),
                        "root_transport_assessment": transport_assessment,
                    }
                ),
            }
        )
    return items


def _summary(
    status: dict[str, Any],
    last_result: dict[str, Any],
    slots_payload: dict[str, Any],
    lifecycle: dict[str, Any],
    conf,
    build: dict[str, Any],
    ui_state: dict[str, Any],
    reliability: dict[str, Any],
    transport_diag: dict[str, Any],
    selected_member: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node_tabs, selected_node = _node_tabs(conf, ui_state, reliability)
    selected_kind = str(selected_node.get("kind") or "local")
    selected_node_id = str(selected_node.get("node_id") or getattr(conf, "node_id", "") or "")
    selected_label = str(selected_node.get("label") or ("hub" if str(getattr(conf, "role", "") or "") == "hub" else "member"))
    active = str(slots_payload.get("active_slot") or "--")
    phase = str(status.get("phase") or "")
    state = str(status.get("state") or "idle")
    message = str(status.get("message") or lifecycle.get("reason") or "No update in progress")
    last_result_state = str(last_result.get("state") or "").strip()
    last_result_phase = str(last_result.get("phase") or "").strip()
    last_result_message = str(
        last_result.get("validation_error_summary")
        or last_result.get("message")
        or ""
    ).strip()
    if state == "idle" and last_result_state and last_result_state != "idle":
        suffix = f"last={last_result_state}"
        if last_result_phase:
            suffix += f"/{last_result_phase}"
        if last_result_message:
            suffix += f": {last_result_message}"
        message += f" | {suffix}"
    validation_summary = str(status.get("validation_error_summary") or "").strip()
    restored_slot = str(status.get("restored_slot") or "").strip()
    if state == "failed" and phase == "validate":
        if validation_summary:
            message += f" | validation: {validation_summary}"
        if restored_slot:
            message += f" | restored slot {restored_slot}"
    last_action = str(ui_state.get("last_action") or "").strip()
    last_action_at = float(ui_state.get("last_action_ts") or 0.0)
    if last_action:
        suffix = f" | action: {last_action}"
        if last_action_at:
            suffix += f" @ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_action_at))}"
        message += suffix
    reliability_note = _reliability_summary_note(reliability, transport_diag)
    if reliability_note:
        message += f" | {reliability_note}"
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    tree = runtime.get("readiness_tree") if isinstance(runtime.get("readiness_tree"), dict) else {}
    channel_diag = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
    protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
    root_tree = tree.get("root_control") if isinstance(tree.get("root_control"), dict) else {}
    route_tree = tree.get("route") if isinstance(tree.get("route"), dict) else {}
    root_diag = channel_diag.get("root_control") if isinstance(channel_diag.get("root_control"), dict) else {}
    route_diag = channel_diag.get("route") if isinstance(channel_diag.get("route"), dict) else {}
    strategy = _hub_root_strategy(reliability, transport_diag)
    strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
    protocol_assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
    coverage = protocol.get("hardening_coverage") if isinstance(protocol.get("hardening_coverage"), dict) else {}
    control_authority = protocol.get("control_authority") if isinstance(protocol.get("control_authority"), dict) else {}
    route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
    route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
    route_control_flow = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
    route_frame_flow = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
    outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
    tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
    llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
    streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
    control_lifecycle_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
        ),
        {},
    )
    core_update_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.integration.github_core_update"
        ),
        {},
    )
    root_status, root_state, root_stability = _effective_channel_view(
        "root_control",
        tree_item=root_tree,
        diag_item=root_diag,
        transport_assessment=strategy_assessment,
    )
    route_status, route_state, route_stability = _effective_channel_view(
        "route",
        tree_item=route_tree,
        diag_item=route_diag,
        transport_assessment=strategy_assessment,
    )
    summary_label = "Core update"
    summary_value = state
    summary_subtitle = f"slot {active} | {build.get('runtime_git_short_commit') or build.get('git_short_sha') or build.get('version') or 'unknown'}"
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    sync_webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
    selected_yjs_webspace_id = _selected_yjs_webspace_id(ui_state, reliability)
    selected_sync_webspace = sync_webspaces.get(selected_yjs_webspace_id) if isinstance(sync_webspaces.get(selected_yjs_webspace_id), dict) else {}
    selected_member = selected_member if isinstance(selected_member, dict) else {}
    if selected_kind == "local" and selected_yjs_webspace_id:
        message += (
            f" | yjs_ws={selected_yjs_webspace_id}"
            f" {selected_sync_webspace.get('log_mode') or '-'}"
            f" replay={selected_sync_webspace.get('replay_window_entries') or 0}/"
            f"{selected_sync_webspace.get('replay_window_limit') or 0}"
        )
    if selected_kind != "local":
        remote_control = _remote_control_payload(
            selected_member.get("node_snapshot") if isinstance(selected_member.get("node_snapshot"), dict) else {},
            selected_member,
        )
        summary_label = "Node state"
        summary_value = str(status.get("state") or lifecycle.get("node_state") or selected_member.get("state") or "connected")
        build_ref = str(build.get("runtime_git_short_commit") or build.get("runtime_version") or build.get("version") or "").strip()
        summary_subtitle = f"{selected_label} | {selected_node_id[:8]}"
        if build_ref:
            summary_subtitle += f" | {build_ref}"
        message = (
            f"hub-member link={selected_member.get('state') or 'connected'}"
            f" last_msg_ago={selected_member.get('last_message_ago_s') if selected_member.get('last_message_ago_s') is not None else '-'}"
            f" snapshot={selected_member.get('snapshot_state') or '-'}"
            f" node={lifecycle.get('node_state') or '-'}"
            f" update={status.get('state') or selected_member.get('snapshot_update_state') or selected_member.get('last_hub_core_update_state') or '-'}"
            f" action={status.get('action') or selected_member.get('last_hub_core_update_action') or '-'}"
            f" rollout={selected_member.get('rollout_state') or '-'}"
        )
        if remote_control.get("action"):
            message += f" control={remote_control.get('action')}:{remote_control.get('ok') if remote_control.get('ok') is not None else '-'}"
        if remote_control.get("error"):
            message += f" control_error={remote_control.get('error')}"
        elif remote_control.get("request_id"):
            message += f" control_req={remote_control.get('request_id')}"
        if selected_member.get("observed_via"):
            message += f" via={selected_member.get('observed_via')}"
        if selected_member.get("last_seen_ago_s") is not None:
            message += f" last_seen_ago={selected_member.get('last_seen_ago_s')}"
        if build_ref:
            message += f" runtime={build_ref}"
        if selected_member.get("last_snapshot_ago_s") is not None:
            message += f" snapshot_ago={selected_member.get('last_snapshot_ago_s')}"
    return {
        "label": summary_label,
        "value": summary_value,
        "subtitle": summary_subtitle,
        "description": message,
        "phase": phase,
        "role": str(getattr(conf, "role", "") or ""),
        "node_id": str(getattr(conf, "node_id", "") or ""),
        "selected_node_id": selected_node_id,
        "selected_node_kind": selected_kind,
        "selected_node_label": selected_label,
        "selected_node_names": selected_node.get("node_names") if isinstance(selected_node.get("node_names"), list) else [],
        "node_tab_total": len(node_tabs),
        "selected_yjs_webspace_id": selected_yjs_webspace_id,
        "selected_yjs_log_mode": str(selected_sync_webspace.get("log_mode") or ""),
        "selected_yjs_replay_window_entries": int(selected_sync_webspace.get("replay_window_entries") or 0),
        "selected_yjs_replay_window_limit": int(selected_sync_webspace.get("replay_window_limit") or 0),
        "selected_yjs_backup_total": int(selected_sync_webspace.get("backup_total") or 0),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "root_url": str(getattr(getattr(conf, "root_settings", None), "base_url", "") or ""),
        "updated_at": float(status.get("updated_at") or time.time()),
        "draining": bool(lifecycle.get("draining")),
        "version": str(build.get("version") or ""),
        "git_short_sha": str(build.get("git_short_sha") or ""),
        "runtime_git_commit": str(build.get("runtime_git_commit") or ""),
        "runtime_git_short_commit": str(build.get("runtime_git_short_commit") or ""),
        "runtime_git_branch": str(build.get("runtime_git_branch") or ""),
        "runtime_git_subject": str(build.get("runtime_git_subject") or ""),
        "target_rev": str(status.get("target_rev") or ""),
        "target_version": str(status.get("target_version") or ""),
        "reason": str(status.get("reason") or ""),
        "last_result_state": last_result_state,
        "last_result_phase": last_result_phase,
        "root_control_status": root_status,
        "root_control_stability": root_state,
        "root_control_score": root_stability.get("score"),
        "root_control_incident_class": str(root_diag.get("last_incident_class") or ""),
        "route_status": route_status,
        "route_stability": route_state,
        "route_incident_class": str(route_diag.get("last_incident_class") or ""),
        "hub_root_transport": str(strategy.get("effective_transport") or strategy.get("requested_transport") or ""),
        "hub_root_transport_state": str(strategy_assessment.get("state") or ""),
        "hub_root_transport_server": str(strategy.get("selected_server") or ""),
        "hub_root_protocol_state": str(protocol_assessment.get("state") or ""),
        "hub_root_protocol_reason": str(protocol_assessment.get("reason") or ""),
        "hub_root_hardening_coverage_state": str(coverage.get("state") or ""),
        "hub_root_hardening_covered_flows": int(coverage.get("covered_flows") or 0),
        "hub_root_hardening_total_flows": int(coverage.get("total_flows") or 0),
        "hub_root_control_authority_state": str(control_authority.get("state") or ""),
        "hub_root_control_authority_reason": str(control_authority.get("reason") or ""),
        "hub_root_route_control_state": str(route_control_flow.get("state") or ""),
        "hub_root_route_control_reason": str(route_control_flow.get("reason") or ""),
        "hub_root_route_frame_state": str(route_frame_flow.get("state") or ""),
        "hub_root_route_frame_reason": str(route_frame_flow.get("reason") or ""),
        "hub_root_route_backlog": int(route_runtime.get("pending_events") or 0),
        "hub_root_tg_outbox": int(tg_outbox.get("size") or 0),
        "hub_root_tg_durable_store": bool(tg_outbox.get("durable_store")),
        "hub_root_tg_persisted_size": int(tg_outbox.get("persisted_size") or 0),
        "hub_root_tg_persist_path": str(tg_outbox.get("persist_path") or ""),
        "hub_root_tg_idempotency_mode": str(tg_outbox.get("idempotency_mode") or ""),
        "hub_root_llm_idempotency_mode": str(llm_outbox.get("idempotency_mode") or ""),
        "hub_root_llm_cache_hit_total": int(llm_outbox.get("cache_hit_total") or 0),
        "hub_root_llm_cache_miss_total": int(llm_outbox.get("cache_miss_total") or 0),
        "hub_root_pending_ack_streams": int(protocol.get("pending_ack_streams") or 0),
        "hub_root_control_issued_cursor": int(control_lifecycle_stream.get("last_issued_cursor") or 0),
        "hub_root_control_acked_cursor": int(control_lifecycle_stream.get("last_acked_cursor") or 0),
        "hub_root_control_duplicate_total": int(control_lifecycle_stream.get("duplicate_total") or 0),
        "hub_root_control_ack_age_s": control_lifecycle_stream.get("last_ack_ago_s"),
        "hub_root_core_update_issued_cursor": int(core_update_stream.get("last_issued_cursor") or 0),
        "hub_root_core_update_acked_cursor": int(core_update_stream.get("last_acked_cursor") or 0),
        "hub_root_core_update_duplicate_total": int(core_update_stream.get("duplicate_total") or 0),
        "scheduled_for": float(status.get("scheduled_for") or 0.0),
        "countdown_remaining_sec": _countdown_remaining_sec(status),
        "drain_timeout_sec": float(status.get("drain_timeout_sec") or 0.0),
        "signal_delay_sec": float(status.get("signal_delay_sec") or 0.0),
        "buttons": (
            _summary_buttons(status)
            if selected_kind == "local"
            else _member_summary_buttons(status, lifecycle, selected_member, conf)
        ),
    }


def _action_items(status: dict[str, Any], ui_state: dict[str, Any], reliability: dict[str, Any]) -> list[dict[str, Any]]:
    last_refresh = float(ui_state.get("last_refresh_ts") or 0.0)
    last_action = str(ui_state.get("last_action") or "").strip()
    state = str(status.get("state") or "idle")
    selected_node_id = str(ui_state.get("selected_node_id") or "").strip()
    selected_yjs_webspace_id = _selected_yjs_webspace_id(ui_state, reliability)
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    sidecar_runtime = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
    local_node_id = str(load_config().node_id or "")
    if selected_node_id and selected_node_id != local_node_id:
        member = _selected_member_entry(reliability, selected_node_id)
        snapshot = member.get("node_snapshot") if isinstance(member.get("node_snapshot"), dict) else {}
        connected = bool(member.get("connected"))
        observed_via = str(member.get("observed_via") or "").strip()
        if not connected:
            last_seen_ago = member.get("last_seen_ago_s")
            return [
                {
                    "id": "refresh",
                    "title": "Refresh snapshot",
                    "status": "idle",
                    "description": "Member is only observed via heartbeat/directory; no active hub-member link yet",
                    "subtitle": (
                        f"{observed_via or 'directory'} | last_seen_ago={last_seen_ago}"
                        if last_seen_ago is not None
                        else (observed_via or "directory")
                    ),
                }
            ]
        remote_status = _remote_status_payload(snapshot, member)
        remote_control = _remote_control_payload(snapshot, member)
        remote_draining = bool(snapshot.get("draining"))
        remote_state = str(remote_status.get("state") or member.get("snapshot_update_state") or "connected").strip().lower()
        control_subtitle = str(remote_control.get("request_id") or remote_control.get("action") or "").strip()
        cancelable_states = {"countdown", "draining", "stopping", "restarting", "applying", "validate", "validated"}
        return [
            {
                "id": "refresh",
                "title": "Refresh snapshot",
                "status": "ok",
                "description": "Request fresh member snapshot from hub link",
                "subtitle": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_refresh)) if last_refresh else "",
            },
            {
                "id": "member_start_update",
                "title": "Start member update",
                "status": "ok" if remote_state in {"idle", "failed", "succeeded", "validated", "cancelled", "rolled_back", "connected"} else "idle",
                "description": "Request core update on selected member via hub link",
                "subtitle": control_subtitle if remote_control.get("action") == "update" else "",
            },
            {
                "id": "member_cancel_update",
                "title": "Cancel member update",
                "status": "warn" if remote_state in cancelable_states else "idle",
                "description": "Request cancel of selected member update countdown",
                "subtitle": control_subtitle if remote_control.get("action") == "cancel" else "",
            },
            {
                "id": "member_rollback",
                "title": "Rollback member",
                "status": "warn",
                "description": "Request slot rollback on selected member",
                "subtitle": control_subtitle if remote_control.get("action") == "rollback" else "",
            },
            {
                "id": "member_drain",
                "title": "Drain member",
                "status": "warn" if not remote_draining else "idle",
                "description": "Enter draining mode and reject new work",
                "subtitle": control_subtitle if remote_control.get("action") == "drain" else "",
            },
        ]
    items = [
        {
            "id": "start_update",
            "title": "Start update",
            "status": "ok" if state in {"idle", "failed", "succeeded", "validated", "cancelled", "rolled_back"} else "idle",
            "description": "Schedule core update with current target rev",
            "subtitle": last_action if last_action == "start_update" else "",
        },
        {
            "id": "refresh",
            "title": "Refresh snapshot",
            "status": "ok",
            "description": "Reload current local update state",
            "subtitle": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_refresh)) if last_refresh else "",
        },
        {
            "id": "cancel_update",
            "title": "Cancel update",
            "status": "warn" if state in {"countdown", "draining", "stopping"} else "idle",
            "description": "Cancel current countdown/update task",
            "subtitle": last_action if last_action == "cancel_update" else "",
        },
        {
            "id": "rollback",
            "title": "Rollback slot",
            "status": "warn",
            "description": "Switch back to previous active core slot",
            "subtitle": last_action if last_action == "rollback" else "",
        },
        {
            "id": "drain",
            "title": "Drain node",
            "status": "warn",
            "description": "Reject new tool calls and enter draining state",
            "subtitle": last_action if last_action == "drain" else "",
        },
    ]
    if str(getattr(load_config(), "role", "") or "").strip().lower() == "hub":
        items.extend(
            [
                {
                    "id": "yjs_backup",
                    "title": "Yjs backup",
                    "status": "ok",
                    "description": f"Persist Yjs snapshot for webspace {selected_yjs_webspace_id}",
                    "subtitle": last_action if last_action == "yjs_backup" else "",
                },
                {
                    "id": "yjs_reload",
                    "title": "Yjs reload",
                    "status": "ok",
                    "description": f"Reseed webspace {selected_yjs_webspace_id} from its current scenario",
                    "subtitle": last_action if last_action == "yjs_reload" else "",
                },
                {
                    "id": "yjs_reset",
                    "title": "Yjs reset",
                    "status": "warn",
                    "description": f"Hard-reset webspace {selected_yjs_webspace_id} from its current scenario",
                    "subtitle": last_action if last_action == "yjs_reset" else "",
                },
            ]
        )
    if bool(sidecar_runtime.get("enabled")):
        items.append(
            {
                "id": "restart_sidecar",
                "title": "Restart sidecar",
                "status": "warn" if str(sidecar_runtime.get("status") or "") in {"degraded", "unknown"} else "ok",
                "description": "Restart realtime sidecar transport runtime",
                "subtitle": last_action if last_action == "restart_sidecar" else "",
            }
        )
    reason = str(status.get("reason") or "").strip().lower()
    if state in {"countdown", "draining", "stopping"} and (
        reason.startswith("github.push:") or reason.startswith("root.release:")
    ):
        items.insert(
            2,
            {
                "id": "refuse_update",
                "title": "Refuse update",
                "status": "warn",
                "description": "Decline root-requested update before restart begins",
                "subtitle": last_action if last_action == "refuse_update" else "",
            },
        )
    return items


def _perform_action(action_id: str, conf, payload: Any | None = None) -> dict[str, Any]:
    status = read_core_update_status()
    selected_node_id = str(_ui_state().get("selected_node_id") or getattr(conf, "node_id", "") or "")
    if (
        action_id in {"start_update", "cancel_update", "refuse_update", "rollback", "drain", "restart_sidecar", "yjs_backup", "yjs_reload", "yjs_reset"}
        and selected_node_id
        and selected_node_id != str(getattr(conf, "node_id", "") or "")
    ):
        raise ValueError("remote member tabs are read-only for update and transport actions")
    if action_id == "refresh":
        if selected_node_id and selected_node_id != str(getattr(conf, "node_id", "") or ""):
            if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
                raise ValueError("remote member snapshot refresh can only be requested from hub")
            try:
                from adaos.services.subnet.link_manager import get_hub_link_manager

                async def _request_remote_snapshot() -> dict[str, Any]:
                    return await get_hub_link_manager().request_member_snapshot(
                        selected_node_id,
                        reason="infrastate.refresh",
                    )

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_request_remote_snapshot())
                    result = {
                        "ok": True,
                        "accepted": True,
                        "node_id": selected_node_id,
                        "reason": "infrastate.refresh",
                    }
                except RuntimeError:
                    result = asyncio.run(_request_remote_snapshot())
            except Exception as exc:
                raise RuntimeError(f"failed to request remote member snapshot for {selected_node_id}: {exc}") from exc
            _write_ui_state(
                selected_node_id=selected_node_id,
                last_action="refresh",
                last_action_ts=time.time(),
                last_refresh_ts=time.time(),
                last_result=result,
                last_error="",
            )
            return result
        _write_ui_state(last_action="refresh", last_action_ts=time.time(), last_refresh_ts=time.time(), last_error="")
        return {"ok": True, "action": action_id}
    if action_id == "select_node":
        node_id = str(_extract_param(payload, "node_id") or "").strip()
        if node_id and node_id != str(getattr(conf, "node_id", "") or "") and str(getattr(conf, "role", "") or "").strip().lower() == "hub":
            try:
                from adaos.services.subnet.link_manager import get_hub_link_manager

                async def _request_remote_snapshot_on_select() -> None:
                    await get_hub_link_manager().request_member_snapshot(
                        node_id,
                        reason="infrastate.select_node",
                    )

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_request_remote_snapshot_on_select())
                except RuntimeError:
                    asyncio.run(_request_remote_snapshot_on_select())
            except Exception:
                _log.debug("failed to request member snapshot on node tab select", exc_info=True)
        _write_ui_state(
            selected_node_id=node_id or str(getattr(conf, "node_id", "") or ""),
            last_action="select_node",
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_error="",
        )
        return {"ok": True, "selected_node_id": node_id}
    if action_id == "select_yjs_webspace":
        selected = str(_extract_param(payload, "webspace_id") or "").strip()
        _write_ui_state(
            selected_yjs_webspace_id=selected or default_webspace_id(),
            last_action="select_yjs_webspace",
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_error="",
        )
        return {"ok": True, "selected_yjs_webspace_id": selected or default_webspace_id()}
    if action_id == "set_node_names":
        node_id = str(_extract_param(payload, "node_id") or _ui_state().get("selected_node_id") or getattr(conf, "node_id", "") or "").strip()
        value = _extract_param(payload, "value")
        node_names = _normalize_node_names(value)
        if not node_id or node_id == str(getattr(conf, "node_id", "") or ""):
            updated = persist_node_names(node_names)
            result = {
                "ok": True,
                "node_id": str(getattr(updated, "node_id", "") or ""),
                "node_names": list(getattr(updated, "node_names", []) or []),
                "scope": "local",
            }
        elif str(getattr(conf, "role", "") or "").strip().lower() == "hub":
            try:
                from adaos.services.subnet.link_manager import get_hub_link_manager

                async def _push_remote_names() -> None:
                    await get_hub_link_manager().set_member_node_names(node_id, node_names=node_names)

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_push_remote_names())
                except RuntimeError:
                    asyncio.run(_push_remote_names())
                result = {"ok": True, "accepted": True, "node_id": node_id, "node_names": node_names, "scope": "remote-member"}
            except Exception as exc:
                raise RuntimeError(f"failed to propagate node names to member {node_id}: {exc}") from exc
        else:
            raise ValueError("remote member names can only be edited from hub")
        _write_ui_state(
            selected_node_id=node_id,
            last_action="set_node_names",
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_result=result,
            last_error="",
        )
        return result
    if action_id == "adaos_update":
        local_node_id = str(getattr(conf, "node_id", "") or "")
        role = str(getattr(conf, "role", "") or "").strip().lower()
        if selected_node_id and selected_node_id != local_node_id:
            if role != "hub":
                raise ValueError("remote update can only be requested from hub")
            try:
                from adaos.services.subnet.link_manager import get_hub_link_manager

                async def _request_remote_update() -> dict[str, Any]:
                    result = await get_hub_link_manager().rpc_tools_call(
                        selected_node_id,
                        tool="infrastate_skill:adaos_update",
                        arguments={"dry_run": False},
                        timeout=180.0,
                        dev=False,
                    )
                    return result if isinstance(result, dict) else {"ok": True, "result": result}

                try:
                    loop = asyncio.get_running_loop()
                    async def _runner() -> None:
                        try:
                            final = await _request_remote_update()
                        except Exception as exc:
                            final = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        _write_ui_state(
                            selected_node_id=selected_node_id,
                            last_action="adaos_update",
                            last_action_ts=time.time(),
                            last_refresh_ts=time.time(),
                            last_result=final,
                            last_error="" if bool(final.get("ok", False)) else str(final.get("error") or ""),
                        )

                    loop.create_task(_runner())
                    result = {"ok": True, "accepted": True, "node_id": selected_node_id, "action": "adaos_update"}
                except RuntimeError:
                    result = asyncio.run(_request_remote_update())
            except Exception as exc:
                raise RuntimeError(f"failed to request remote node update for {selected_node_id}: {exc}") from exc
        else:
            result = _adaos_update_local(dry_run=False)

        _write_ui_state(
            selected_node_id=selected_node_id or local_node_id,
            last_action="adaos_update",
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_result=result,
            last_error="",
        )
        return result
    if action_id in {"member_start_update", "member_cancel_update", "member_rollback", "member_drain"}:
        if not selected_node_id or selected_node_id == str(getattr(conf, "node_id", "") or ""):
            raise ValueError("remote member action requires selected remote member")
        if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
            raise ValueError("remote member control can only be requested from hub")
        current_rev = str(status.get("target_rev") or os.getenv("ADAOS_REV") or "").strip()
        current_version = str(status.get("target_version") or BUILD_INFO.version or "").strip()
        request_action = {
            "member_start_update": "update",
            "member_cancel_update": "cancel",
            "member_rollback": "rollback",
            "member_drain": "drain",
        }[action_id]
        countdown_sec = 60.0 if request_action == "update" else (0.0 if request_action == "drain" else 15.0)
        try:
            from adaos.services.subnet.link_manager import get_hub_link_manager

            async def _request_remote_update() -> dict[str, Any]:
                return await get_hub_link_manager().request_member_update(
                    selected_node_id,
                    action=request_action,
                    target_rev=current_rev if request_action == "update" else "",
                    target_version=current_version if request_action == "update" else "",
                    countdown_sec=countdown_sec,
                    drain_timeout_sec=10.0,
                    signal_delay_sec=0.25,
                    reason=f"infrastate.{action_id}",
                )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_request_remote_update())
                result = {
                    "ok": True,
                    "accepted": True,
                    "node_id": selected_node_id,
                    "action": request_action,
                    "reason": f"infrastate.{action_id}",
                }
            except RuntimeError:
                result = asyncio.run(_request_remote_update())
        except Exception as exc:
            raise RuntimeError(f"failed to request remote member update for {selected_node_id}: {exc}") from exc
        _write_ui_state(
            selected_node_id=selected_node_id,
            last_action=action_id,
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_result=result,
            last_error="",
        )
        return result
    if action_id == "start_update":
        current_rev = str(status.get("target_rev") or os.getenv("ADAOS_REV") or "").strip()
        current_version = str(status.get("target_version") or BUILD_INFO.version or "").strip()
        result = _post_local_admin(
            conf,
            "/api/admin/update/start",
            {
                "reason": "infrastate.start_update",
                "countdown_sec": 60,
                "target_rev": current_rev,
                "target_version": current_version,
                "drain_timeout_sec": 10,
                "signal_delay_sec": 0.25,
            },
        )
    elif action_id in {"cancel_update", "refuse_update"}:
        result = _post_local_admin(conf, "/api/admin/update/cancel", {"reason": "infrastate.cancel"})
    elif action_id == "rollback":
        result = _post_local_admin(conf, "/api/admin/update/rollback", {"reason": "infrastate.rollback"})
    elif action_id == "drain":
        result = _post_local_admin(conf, "/api/admin/drain", {"reason": "infrastate.drain"})
    elif action_id == "restart_sidecar":
        result = _post_local_admin(
            conf,
            "/api/node/sidecar/restart",
            {"reconnect_hub_root": True},
        )
    elif action_id == "yjs_backup":
        selected_webspace = _selected_yjs_webspace_id(_ui_state(), _reliability_snapshot(conf, runtime_lifecycle_snapshot()))
        result = _post_local_admin(
            conf,
            f"/api/node/yjs/webspaces/{selected_webspace}/backup",
            {},
        )
    elif action_id == "yjs_reload":
        selected_webspace = _selected_yjs_webspace_id(_ui_state(), _reliability_snapshot(conf, runtime_lifecycle_snapshot()))
        result = _post_local_admin(
            conf,
            f"/api/node/yjs/webspaces/{selected_webspace}/reload",
            {},
        )
    elif action_id == "yjs_reset":
        selected_webspace = _selected_yjs_webspace_id(_ui_state(), _reliability_snapshot(conf, runtime_lifecycle_snapshot()))
        result = _post_local_admin(
            conf,
            f"/api/node/yjs/webspaces/{selected_webspace}/reset",
            {},
        )
    else:
        raise ValueError(f"unsupported infrastate action: {action_id}")
    _write_ui_state(
        last_action=action_id,
        last_action_ts=time.time(),
        last_refresh_ts=time.time(),
        last_result=result,
        last_error="",
    )
    return result


def _snapshot() -> dict[str, Any]:
    _ensure_skill_data_projections()
    conf = load_config()
    status = read_core_update_status()
    last_result = read_core_update_last_result() or {}
    slots_payload = slot_status()
    lifecycle = runtime_lifecycle_snapshot()
    build = _build_meta()
    ui_state = _ui_state()
    reliability = _reliability_snapshot(conf, lifecycle)
    node_tabs, selected_node = _node_tabs(conf, ui_state, reliability)
    yjs_webspace_tabs = _yjs_webspace_tabs(conf, ui_state, reliability, selected_node)
    node_editor = _selected_node_editor(conf, selected_node)
    selected_projection = _selected_node_projection(
        selected_node,
        reliability=reliability,
        status=status,
        last_result=last_result,
        slots_payload=slots_payload,
        lifecycle=lifecycle,
        build=build,
    )
    display_status = selected_projection["status"] if isinstance(selected_projection.get("status"), dict) else status
    display_last_result = selected_projection["last_result"] if isinstance(selected_projection.get("last_result"), dict) else last_result
    display_slots_payload = selected_projection["slots_payload"] if isinstance(selected_projection.get("slots_payload"), dict) else slots_payload
    display_lifecycle = selected_projection["lifecycle"] if isinstance(selected_projection.get("lifecycle"), dict) else lifecycle
    display_build = selected_projection["build"] if isinstance(selected_projection.get("build"), dict) else build
    selected_member = selected_projection["selected_member"] if isinstance(selected_projection.get("selected_member"), dict) else {}
    transport_diag = _transport_diag_snapshot()
    report = _read_json(_base_dir() / "state" / "core_update" / "status.json") or {}
    effective_report = _effective_update_log_report(report, display_last_result)
    snapshot = {
        "summary": _summary(display_status, display_last_result, display_slots_payload, display_lifecycle, conf, display_build, ui_state, reliability, transport_diag, selected_member=selected_member),
        "actions": _action_items(display_status, ui_state, reliability),
        "update_actions": _update_actions(conf, ui_state, reliability),
        "nodes": node_tabs,
        "yjs_webspaces": yjs_webspace_tabs,
        "node_editor": node_editor,
        "build": _build_items(display_build),
        "steps": _step_items(display_status, display_slots_payload, display_lifecycle, display_build),
        "realtime": _realtime_items(reliability, transport_diag),
        "slots": _slot_items(display_slots_payload),
        "skills": _skills_items(),
        "logs": _status_log_items(effective_report),
        "events": list(reversed(_event_state())),
        "status": display_status,
        "last_result": display_last_result,
        "lifecycle": display_lifecycle,
        "reliability": reliability,
        "transport_diag": transport_diag,
        "build_meta": display_build,
        "ui_state": ui_state,
        "slots_meta": display_slots_payload,
        "last_refresh_ts": time.time(),
    }
    return snapshot


def _projection_webspace_ids(webspace_id: str | None = None) -> list[str]:
    ids: set[str] = set()
    token = str(webspace_id or "").strip()
    if token:
        ids.add(token)
    ids.add(default_webspace_id())
    try:
        for info in WebspaceService().list(mode="mixed"):
            slot = str(getattr(info, "id", "") or "").strip()
            if slot:
                ids.add(slot)
    except Exception:
        _log.debug("failed to enumerate webspaces for infrastate projection", exc_info=True)
    return sorted(ids)


def _project(snapshot: dict[str, Any], webspace_id: str | None = None) -> None:
    for target_ws in _projection_webspace_ids(webspace_id):
        ctx_subnet.set("infrastate.snapshot", snapshot, webspace_id=target_ws)


def _webspace_id_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("webspace_id") or payload.get("workspace_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        token = meta.get("webspace_id") or meta.get("workspace_id")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


@tool("get_snapshot")
def get_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _snapshot()
    _project(snapshot, webspace_id=webspace_id)
    return snapshot


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    _write_ui_state(last_refresh_ts=time.time())
    snapshot = _snapshot()
    _project(snapshot, webspace_id=webspace_id)
    return {"ok": True, **snapshot}


@subscribe("infrastate.refresh")
def on_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("infrastate.action")
def on_action(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    conf = load_config()
    action_id = _extract_action_id(payload)
    webspace_id = _webspace_id_from_payload(payload)
    try:
        if action_id:
            _perform_action(action_id, conf, payload)
    except Exception as exc:
        _write_ui_state(last_action=action_id, last_action_ts=time.time(), last_error=str(exc))
        _log.warning("infrastate action failed: %s", action_id, exc_info=True)
    refresh_snapshot(webspace_id=webspace_id)


@tool("adaos_update")
def adaos_update(dry_run: bool = False) -> dict[str, Any]:
    """
    Update skills and scenarios on the current node.
    This is used by hub->member RPC and can also be called locally.
    """
    return _adaos_update_local(dry_run=bool(dry_run))


@subscribe("skills.activated")
def on_skill_activated(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, dict):
        return
    skill_name = str(payload.get("skill_name") or "")
    if skill_name and skill_name != "infrastate_skill":
        return
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("skills.updated")
def on_skill_updated(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
def on_webspace_reload(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))


@subscribe("sys.ready")
@subscribe("subnet.nats.up")
@subscribe("subnet.nats.down")
@subscribe("subnet.nats.reconnect")
@subscribe("subnet.member.link.up")
@subscribe("subnet.member.link.down")
@subscribe("subnet.member.meta.changed")
@subscribe("subnet.member.snapshot.changed")
@subscribe("subnet.member.snapshot.requested")
@subscribe("subnet.member.update.requested")
@subscribe("subnet.member.update.result")
@subscribe("subnet.stopping")
@subscribe("subnet.stopped")
@subscribe("core.update.status")
@subscribe("hub.core_update.status")
@subscribe("node.names.changed")
def on_runtime_event(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    try:
        event_type = str(getattr(evt, "type", "") or (payload.get("type") if isinstance(payload, dict) else "") or "runtime.event")
        _append_event(event_type, payload)
        refresh_snapshot(webspace_id=_webspace_id_from_payload(payload))
    except Exception:
        _log.debug("failed to refresh infrastate snapshot from runtime event", exc_info=True)
