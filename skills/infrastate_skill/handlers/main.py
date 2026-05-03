from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from adaos.build_info import BUILD_INFO
from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.context import clear_current_skill, set_current_skill
from adaos.sdk.data import ctx_subnet, skill_memory_get, skill_memory_set
from adaos.sdk.io import stream_publish
from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest, slot_status
from adaos.services.core_update import read_last_result as read_core_update_last_result
from adaos.services.core_update import read_status as read_core_update_status
from adaos.services import node_config as _node_config
from adaos.services.realtime_sidecar import realtime_sidecar_diag_path, realtime_sidecar_enabled
from adaos.services.reliability import assess_transport_diagnostics, reliability_snapshot
from adaos.services.runtime_lifecycle import runtime_lifecycle_snapshot
from adaos.services.runtime_refresh import rebuild_webspace_projection_sync, refresh_skill_runtime
from adaos.services.scenario.webspace_runtime import WebspaceService
from adaos.services.operations import get_operation_manager, submit_install_operation
from adaos.services.skill.update import SkillUpdateService
from adaos.services.scenarios.loader import read_manifest
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.workspace_registry import build_registry_entry, list_workspace_registry_entries, rebuild_workspace_registry
from adaos.services.workspace_sync import effective_registry_names, installed_names, sync_workspace_sparse_to_registry
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.skill.manager import SkillManager
from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.services.node_display import node_display_from_config

from packaging.version import Version, InvalidVersion

_log = logging.getLogger("skills.infrastate_skill")
_UI_STATE_KEY = "infrastate.ui_state"
_EVENTS_STATE_KEY = "infrastate.events"
_SUMMARY_RENDER_STATE_KEY = "infrastate.summary_render_state"
_BACKGROUND_REFRESH_DEBOUNCE_S = 0.35
_REMOTE_VERSION_PROBE_ENABLED = str(os.getenv("ADAOS_INFRASTATE_REMOTE_VERSION_PROBE") or "").strip().lower() in {"1", "true", "yes", "on"}
_MARKETPLACE_CACHE_TTL_S = max(0.0, float(os.getenv("ADAOS_INFRASTATE_MARKETPLACE_CACHE_TTL_S") or "30"))
_SNAPSHOT_CACHE_TTL_S = max(0.0, float(os.getenv("ADAOS_INFRASTATE_SNAPSHOT_CACHE_TTL_S") or "1.5"))
_SNAPSHOT_CONTENT_MAX_BYTES = max(0, int(os.getenv("ADAOS_INFRASTATE_SNAPSHOT_CONTENT_MAX_BYTES") or "4096"))
_background_refresh_task: asyncio.Task[Any] | None = None
_background_refresh_thread: threading.Thread | None = None
_background_refresh_pending = False
_background_refresh_webspace_id: str | None = None
_background_refresh_reason = ""
_marketplace_catalog_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_snapshot_cache_guard = threading.Lock()
_snapshot_cache_locks: dict[str, threading.Lock] = {}
_projection_fingerprints: dict[str, str] = {}
_projection_last_applied_at: dict[str, float] = {}
_active_stream_receivers_by_webspace: dict[str, set[str]] = {}
_stream_fingerprints: dict[str, str] = {}
_stream_last_published_at: dict[str, float] = {}
_projection_diag = {
    "apply_total": 0,
    "skip_total": 0,
    "cache_hit_total": 0,
    "rate_limited_total": 0,
}
_MIN_YJS_PROJECTION_INTERVAL_S = 1.0


def _stream_min_interval_s() -> float:
    try:
        raw = str(os.getenv("ADAOS_INFRASTATE_STREAM_MIN_INTERVAL_S") or "2.0").strip()
        return max(0.0, min(float(raw), 30.0))
    except Exception:
        return 2.0


def _eager_stream_publish_enabled() -> bool:
    raw = str(os.getenv("ADAOS_INFRASTATE_EAGER_STREAM_PUBLISH") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _operations_receiver() -> str:
    return "infrastate.operations.active"


def _logs_receiver() -> str:
    return "infrastate.logs.recent"


def _events_receiver() -> str:
    return "infrastate.events.recent"


def _yjs_load_receiver() -> str:
    return "infrastate.yjs.load_mark"


def _build_receiver() -> str:
    return "infrastate.build"


def _steps_receiver() -> str:
    return "infrastate.steps"


def _realtime_receiver() -> str:
    return "infrastate.realtime"


def _slots_receiver() -> str:
    return "infrastate.slots"


def _skills_receiver() -> str:
    return "infrastate.skills"


def _scenarios_receiver() -> str:
    return "infrastate.scenarios"


def _marketplace_skills_receiver() -> str:
    return "infrastate.marketplace.skills"


def _marketplace_scenarios_receiver() -> str:
    return "infrastate.marketplace.scenarios"


def _core_update_diagnostics_receiver() -> str:
    return "infrastate.core_update_diagnostics"


def _stable_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        text = json.dumps(_clone_json_like_for_cache(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def _clone_json_like_for_cache(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        if isinstance(value, dict):
            return {str(k): _clone_json_like_for_cache(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_clone_json_like_for_cache(v) for v in value]
        if isinstance(value, tuple):
            return [_clone_json_like_for_cache(v) for v in value]
        items = getattr(value, "items", None)
        if callable(items):
            try:
                return {str(k): _clone_json_like_for_cache(v) for k, v in items()}
            except Exception:
                return value
        return value


def _sanitize_snapshot_for_fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "")
            folded = key.casefold()
            if (
                folded == "projection_diag"
                or folded == "last_refresh_ts"
                or folded.endswith("_ts")
                or folded.endswith("_at")
                or folded.endswith("_age_s")
                or folded.endswith("_ago_s")
            ):
                continue
            sanitized[key] = _sanitize_snapshot_for_fingerprint(raw_value)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_snapshot_for_fingerprint(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_snapshot_for_fingerprint(item) for item in value]
    return value


def _compact_snapshot_for_yjs(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _compact_snapshot_for_client(snapshot)


def _truncate_text(text: str, limit: int | None = None) -> str:
    limit = _SNAPSHOT_CONTENT_MAX_BYTES if limit is None else max(0, int(limit))
    if limit <= 0 or len(text.encode("utf-8", errors="ignore")) <= limit:
        return text
    head = text.encode("utf-8", errors="ignore")[:limit].decode("utf-8", errors="ignore")
    return f"{head}\n... truncated; full diagnostics available through dedicated runtime endpoints ..."


def _compact_card_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return _cache_copy(item)
    out: dict[str, Any] = {}
    for key in (
        "id",
        "title",
        "status",
        "description",
        "subtitle",
        "preview",
        "kind",
        "display",
        "updated_at",
        "buttons",
        "actions",
    ):
        if key in item:
            out[key] = _cache_copy(item.get(key))
    if "content" in item:
        content = item.get("content")
        text = content if isinstance(content, str) else _safe_json_text(content)
        compact_content = _truncate_text(text)
        out["content"] = compact_content
        out["content_truncated"] = compact_content != text
    return out


def _compact_card_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [_compact_card_item(item) for item in value]


def _compact_mapping(value: Any, *, max_keys: int | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    items = list(value.items())
    if max_keys is not None and max_keys >= 0:
        items = items[:max_keys]
    return {str(key): _cache_copy(item) for key, item in items}


def _compact_snapshot_for_client(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot = snapshot or {}
    compact: dict[str, Any] = {}
    for key in (
        "summary",
        "actions",
        "core_actions",
        "yjs_actions",
        "update_actions",
        "nodes",
        "yjs_webspaces",
        "node_editor",
        "operations",
        "marketplace",
        "ui_state",
        "projection_diag",
        "fallback",
        "errors",
        "last_refresh_ts",
    ):
        if key in snapshot:
            compact[key] = _cache_copy(snapshot.get(key))
    for key in (
        "build",
        "steps",
        "realtime",
        "slots",
        "skills",
        "scenarios",
        "logs",
        "events",
        "core_update_diagnostics",
        "core_update_diag_actions",
    ):
        if key in snapshot:
            compact[key] = _compact_card_list(snapshot.get(key))
    reliability = snapshot.get("reliability") if isinstance(snapshot.get("reliability"), dict) else {}
    if reliability:
        runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
        compact["reliability"] = {
            "ok": reliability.get("ok"),
            "node": _cache_copy(reliability.get("node")),
            "runtime": {
                "assessment": _cache_copy(runtime.get("assessment")),
                "channel_overview": _cache_copy(runtime.get("channel_overview")),
                "readiness_tree": _cache_copy(runtime.get("readiness_tree")),
            },
        }
    return compact


def _stream_payload_for_receiver(snapshot: dict[str, Any], receiver: str) -> Any:
    token = str(receiver or "").strip()
    if token == _operations_receiver():
        operations = snapshot.get("operations") if isinstance(snapshot.get("operations"), dict) else {}
        return list(operations.get("items") or [])
    if token == _logs_receiver():
        return list(snapshot.get("logs") or [])
    if token == _events_receiver():
        return list(snapshot.get("events") or [])
    if token == _yjs_load_receiver():
        yjs_runtime = snapshot.get("yjs_runtime") if isinstance(snapshot.get("yjs_runtime"), dict) else {}
        if not yjs_runtime:
            reliability = snapshot.get("reliability") if isinstance(snapshot.get("reliability"), dict) else {}
            runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
            yjs_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
        load_mark = yjs_runtime.get("load_mark") if isinstance(yjs_runtime.get("load_mark"), dict) else {}
        selected = load_mark.get("selected_webspace") if isinstance(load_mark.get("selected_webspace"), dict) else {}
        rows: list[dict[str, Any]] = []
        for item in list(selected.get("owner_items") or []):
            if not isinstance(item, dict):
                continue
            owner = str(item.get("owner") or "").strip()
            row = dict(item)
            row["kind"] = "owner"
            row["id"] = owner or "unknown"
            row["display"] = owner or "unknown"
            rows.append(row)
        for item in list(selected.get("items") or []):
            if not isinstance(item, dict):
                continue
            root = str(item.get("root") or "").strip()
            row = dict(item)
            row["kind"] = "root"
            row["id"] = root or "unknown"
            row["display"] = root or "unknown"
            rows.append(row)
        rows.sort(
            key=lambda entry: (
                0 if str(entry.get("kind") or "") == "owner" else 1,
                -float(entry.get("peak_bps") or 0.0),
                -float(entry.get("peak_wps") or 0.0),
                -float(entry.get("avg_bps") or 0.0),
                str(entry.get("display") or ""),
            )
        )
        return rows
    if token == _build_receiver():
        return list(snapshot.get("build") or [])
    if token == _steps_receiver():
        return list(snapshot.get("steps") or [])
    if token == _realtime_receiver():
        return list(snapshot.get("realtime") or [])
    if token == _slots_receiver():
        return list(snapshot.get("slots") or [])
    if token == _skills_receiver():
        return list(snapshot.get("skills") or [])
    if token == _scenarios_receiver():
        return list(snapshot.get("scenarios") or [])
    if token == _marketplace_skills_receiver():
        marketplace = snapshot.get("marketplace") if isinstance(snapshot.get("marketplace"), dict) else {}
        return list(marketplace.get("skills") or [])
    if token == _marketplace_scenarios_receiver():
        marketplace = snapshot.get("marketplace") if isinstance(snapshot.get("marketplace"), dict) else {}
        return list(marketplace.get("scenarios") or [])
    if token == _core_update_diagnostics_receiver():
        return list(snapshot.get("core_update_diagnostics") or [])
    return None


def _stream_cache_key(webspace_id: str | None, receiver: str) -> str:
    ws = str(webspace_id or "").strip() or default_webspace_id()
    token = str(receiver or "").strip()
    return f"{ws}\0{token}"


def _remember_stream_receiver(webspace_id: str | None, receiver: str) -> None:
    token = str(receiver or "").strip()
    if not token.startswith("infrastate."):
        return
    ws = str(webspace_id or "").strip() or default_webspace_id()
    _active_stream_receivers_by_webspace.setdefault(ws, set()).add(token)


def _forget_stream_receiver(webspace_id: str | None, receiver: str) -> None:
    token = str(receiver or "").strip()
    if not token:
        return
    ws = str(webspace_id or "").strip() or default_webspace_id()
    receivers = _active_stream_receivers_by_webspace.get(ws)
    if receivers is not None:
        receivers.discard(token)
        if not receivers:
            _active_stream_receivers_by_webspace.pop(ws, None)
    key = _stream_cache_key(ws, token)
    _stream_fingerprints.pop(key, None)
    _stream_last_published_at.pop(key, None)


def _active_stream_receivers(webspace_id: str | None) -> list[str]:
    ws = str(webspace_id or "").strip() or default_webspace_id()
    return sorted(_active_stream_receivers_by_webspace.get(ws) or set())


def _stream_payload_fingerprint(data: Any) -> str:
    return hashlib.sha1(_stable_json_bytes(_sanitize_snapshot_for_fingerprint(data))).hexdigest()


def _publish_stream_payload(*, receiver: str, data: Any, webspace_id: str | None, force: bool = False) -> None:
    if data is None:
        return
    key = _stream_cache_key(webspace_id, receiver)
    fingerprint = _stream_payload_fingerprint(data)
    now = time.monotonic()
    if not force:
        if _stream_fingerprints.get(key) == fingerprint:
            return
        last_at = float(_stream_last_published_at.get(key) or 0.0)
        min_interval_s = _stream_min_interval_s()
        if min_interval_s > 0 and last_at > 0 and now - last_at < min_interval_s:
            return
    _stream_fingerprints[key] = fingerprint
    _stream_last_published_at[key] = now
    stream_publish(
        receiver,
        data,
        _meta={
            "webspace_id": str(webspace_id or "").strip() or default_webspace_id(),
        },
    )


def _publish_snapshot_streams(snapshot: dict[str, Any], *, webspace_id: str | None) -> None:
    receivers = (
        _operations_receiver(),
        _logs_receiver(),
        _events_receiver(),
        _yjs_load_receiver(),
        _build_receiver(),
        _steps_receiver(),
        _realtime_receiver(),
        _slots_receiver(),
        _skills_receiver(),
        _scenarios_receiver(),
        _marketplace_skills_receiver(),
        _marketplace_scenarios_receiver(),
        _core_update_diagnostics_receiver(),
    )
    if not _eager_stream_publish_enabled():
        receivers = tuple(_active_stream_receivers(webspace_id))
    for receiver in receivers:
        _publish_stream_payload(
            receiver=receiver,
            data=_stream_payload_for_receiver(snapshot, receiver),
            webspace_id=webspace_id,
        )


def _snapshot_cache_key(webspace_id: str | None = None) -> str:
    return str(webspace_id or "").strip() or default_webspace_id()


def _snapshot_cache_lock_for(cache_key: str) -> threading.Lock:
    with _snapshot_cache_guard:
        lock = _snapshot_cache_locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _snapshot_cache_locks[cache_key] = lock
        return lock


def _invalidate_runtime_caches(*, webspace_id: str | None = None, marketplace: bool = False) -> None:
    cache_key = _snapshot_cache_key(webspace_id)
    with _snapshot_cache_lock_for(cache_key):
        _snapshot_cache.pop(cache_key, None)
    if marketplace:
        _marketplace_catalog_cache.clear()


def _snapshot_projection_fingerprint(snapshot: dict[str, Any]) -> str:
    payload = _sanitize_snapshot_for_fingerprint(snapshot)
    return hashlib.sha1(_stable_json_bytes(payload)).hexdigest()


def _cache_copy(value: Any) -> Any:
    return _clone_json_like_for_cache(value)


def _projection_diag_snapshot() -> dict[str, Any]:
    return {
        "marketplace_cache_ttl_s": _MARKETPLACE_CACHE_TTL_S,
        "snapshot_cache_ttl_s": _SNAPSHOT_CACHE_TTL_S,
        "min_yjs_projection_interval_s": _MIN_YJS_PROJECTION_INTERVAL_S,
        "apply_total": int(_projection_diag.get("apply_total") or 0),
        "skip_total": int(_projection_diag.get("skip_total") or 0),
        "cache_hit_total": int(_projection_diag.get("cache_hit_total") or 0),
        "rate_limited_total": int(_projection_diag.get("rate_limited_total") or 0),
        "fingerprinted_webspaces": sorted(_projection_fingerprints),
    }

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


def _skill_manifest_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        for name in ("skill.yaml", "resolved.manifest.json"):
            candidate = parent / name
            if candidate.exists():
                return candidate
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


def _read_registry_catalog_version(*, skill_id: str) -> str | None:
    for entry in _marketplace_catalog_entries("skills"):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or entry.get("name") or "").strip()
        if entry_id != skill_id:
            continue
        version = entry.get("version")
        if version is None:
            return None
        token = str(version).strip()
        return token or None
    return None


def _clean_version_text(value: Any) -> str | None:
    token = str(value or "").strip()
    return token or None


def _workspace_registry_entry_map(workspace_root: Path, kind_plural: str) -> dict[str, dict[str, Any]]:
    try:
        entries = list_workspace_registry_entries(workspace_root, kind=kind_plural)
    except Exception:
        entries = []
    mapped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("id") or "").strip()
        if name:
            mapped[name] = dict(entry)
    return mapped


def _read_local_artifact_version(workspace_root: Path, kind_plural: str, name: str) -> str | None:
    artifact_dir = Path(workspace_root) / kind_plural / str(name)
    try:
        entry = build_registry_entry(kind_plural, artifact_dir)
    except Exception:
        entry = None
    if not isinstance(entry, dict):
        return None
    return _clean_version_text(entry.get("version"))


def _registry_payload_from_git_ref(workspace_root: Path) -> dict[str, Any] | None:
    remote = str(os.getenv("ADAOS_WORKSPACE_REGISTRY_REMOTE") or "origin").strip() or "origin"
    branch = str(os.getenv("ADAOS_WORKSPACE_REGISTRY_BRANCH") or "main").strip() or "main"
    ref = f"{remote}/{branch}:registry.json"
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace_root), "show", ref],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _marketplace_catalog_entries(kind_plural: str) -> list[dict[str, Any]]:
    try:
        ctx = get_ctx()
    except Exception:
        return []

    workspace_root = Path(ctx.paths.workspace_dir())
    cache_key = (str(workspace_root), str(kind_plural or "").strip())
    cache_now = time.monotonic()
    cached = _marketplace_catalog_cache.get(cache_key)
    if cached is not None and _MARKETPLACE_CACHE_TTL_S > 0:
        cached_at, cached_items = cached
        if cache_now - cached_at <= _MARKETPLACE_CACHE_TTL_S:
            return [dict(item) for item in cached_items]

    merged: dict[str, dict[str, Any]] = {}

    def _merge(items: list[dict[str, Any]] | Any) -> None:
        if not isinstance(items, list):
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("name") or raw.get("id") or "").strip()
            if not key:
                continue
            merged[key] = dict(raw)

    remote_payload = _registry_payload_from_git_ref(workspace_root)
    if isinstance(remote_payload, dict):
        _merge(remote_payload.get(kind_plural))

    try:
        _merge(list_workspace_registry_entries(workspace_root, kind=kind_plural))
    except Exception:
        pass

    try:
        scanned_payload = rebuild_workspace_registry(workspace_root)
    except Exception:
        scanned_payload = None
    if isinstance(scanned_payload, dict):
        _merge(scanned_payload.get(kind_plural))

    result = [merged[key] for key in sorted(merged, key=str.lower)]
    _marketplace_catalog_cache[cache_key] = (cache_now, [dict(item) for item in result])
    return result


def _skills_items() -> list[dict[str, Any]]:
    try:
        ctx = get_ctx()
    except Exception:
        return []

    workspace_root = Path(ctx.paths.workspace_dir())
    registry = SqliteSkillRegistry(ctx.sql)
    mgr = SkillManager(
        repo=ctx.skills_repo,
        registry=registry,
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )

    try:
        registry_rows = registry.list() or []
    except Exception:
        registry_rows = []

    installed_skill_names, _fallback_used = effective_registry_names(
        ctx,
        installed_names(registry_rows),
        workspace_root,
        "skills",
    )
    registry_rows_by_name = {
        str(getattr(row, "name", "") or "").strip(): row
        for row in registry_rows
        if str(getattr(row, "name", "") or "").strip()
    }
    workspace_registry_by_name = _workspace_registry_entry_map(workspace_root, "skills")

    skill_usage = _skill_usage_by_scenarios()
    out: list[dict[str, Any]] = []
    for name in installed_skill_names:
        row = registry_rows_by_name.get(name)
        if not name:
            continue
        local_version = (
            _read_local_artifact_version(workspace_root, "skills", name)
            or _clean_version_text((workspace_registry_by_name.get(name) or {}).get("version"))
            or _clean_version_text(getattr(row, "active_version", None) if row is not None else None)
            or ""
        )

        slot = ""
        try:
            st = mgr.runtime_status(name)
            slot = str(st.get("active_slot") or "").strip()
        except Exception:
            slot = ""

        remote_version = str(_read_registry_catalog_version(skill_id=name) or "").strip()
        update_available = False
        lv = _safe_version(local_version)
        rv = _safe_version(remote_version)
        if lv is not None and rv is not None and rv > lv:
            update_available = True
        display_name = f"{name} *" if update_available else name
        version_display = local_version
        if update_available and remote_version:
            version_display = f"{local_version} ({remote_version})"

        out.append(
            {
                "name": name,
                "display_name": display_name,
                "version": local_version,
                "version_display": version_display,
                "slot": slot,
                "active": bool(slot),
                "can_activate": True,
                "can_test": True,
                "used_by_scenarios": skill_usage.get(name, []),
                "uninstall_disabled": bool(skill_usage.get(name, [])),
                "remote_version": remote_version,
                "update_available": update_available,
            }
        )

    out.sort(key=lambda x: x.get("name") or "")
    return out


def _scenario_items() -> list[dict[str, Any]]:
    try:
        ctx = get_ctx()
    except Exception:
        return []
    workspace_root = Path(ctx.paths.workspace_dir())

    try:
        registry_rows = SqliteScenarioRegistry(ctx.sql).list() or []
    except Exception:
        registry_rows = []
    installed_scenario_names, _fallback_used = effective_registry_names(
        ctx,
        installed_names(registry_rows),
        workspace_root,
        "scenarios",
    )
    registry_rows_by_name = {
        str(getattr(row, "name", "") or "").strip(): row
        for row in registry_rows
        if str(getattr(row, "name", "") or "").strip()
    }
    workspace_registry_by_name = _workspace_registry_entry_map(workspace_root, "scenarios")

    out: list[dict[str, Any]] = []
    for name in sorted(installed_scenario_names):
        if not name:
            continue
        row = registry_rows_by_name.get(name)
        version = (
            _read_local_artifact_version(workspace_root, "scenarios", name)
            or _clean_version_text((workspace_registry_by_name.get(name) or {}).get("version"))
            or _clean_version_text(getattr(row, "active_version", None) if row is not None else None)
            or ""
        )
        out.append(
            {
                "name": name,
                "version": version,
                "updated_at": getattr(row, "last_updated", None) if row is not None else None,
                "uninstall_disabled": False,
            }
        )

    out.sort(key=lambda x: x.get("name") or "")
    return out


def _skill_usage_by_scenarios() -> dict[str, list[str]]:
    try:
        ctx = get_ctx()
    except Exception:
        return {}

    try:
        scenario_rows = SqliteScenarioRegistry(ctx.sql).list() or []
    except Exception:
        scenario_rows = []

    usage: dict[str, list[str]] = {}
    for row in scenario_rows:
        scenario_name = str(getattr(row, "name", "") or "").strip()
        if not scenario_name:
            continue
        try:
            manifest = read_manifest(scenario_name) or {}
        except Exception:
            manifest = {}
        depends = manifest.get("depends") or []
        if not isinstance(depends, (list, tuple)):
            continue
        for dep in depends:
            skill_name = str(dep or "").strip()
            if not skill_name:
                continue
            bucket = usage.setdefault(skill_name, [])
            if scenario_name not in bucket:
                bucket.append(scenario_name)
    for skill_name in list(usage.keys()):
        usage[skill_name].sort()
    return usage


def _operations_snapshot(*, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        return get_operation_manager().snapshot(webspace_id=webspace_id)
    except Exception:
        return {"by_id": {}, "order": [], "active": [], "active_items": [], "notifications": []}


def _marketplace_items(*, webspace_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    try:
        ctx = get_ctx()
    except Exception:
        return {"skills": [], "scenarios": []}

    operations = _operations_snapshot(webspace_id=webspace_id)
    active_by_target: dict[tuple[str, str], dict[str, Any]] = {}
    for item in operations.get("active_items") or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("target_kind") or ""), str(item.get("target_id") or ""))
        active_by_target[key] = item

    installed_skills = {str(item.get("name") or "").strip() for item in _skills_items() if str(item.get("name") or "").strip()}
    installed_scenarios = {str(item.get("name") or "").strip() for item in _scenario_items() if str(item.get("name") or "").strip()}

    def _rows(kind_plural: str, installed: set[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in _marketplace_catalog_entries(kind_plural):
            artifact = entry if isinstance(entry, dict) else {}
            target_kind = str(artifact.get("kind") or kind_plural[:-1]).strip() or kind_plural[:-1]
            target_id = str(artifact.get("id") or artifact.get("name") or "").strip()
            if not target_id or target_id in installed:
                continue
            op = active_by_target.get((target_kind, target_id))
            rows.append(
                {
                    "kind": target_kind,
                    "id": target_id,
                    "name": str(artifact.get("title") or artifact.get("name") or target_id),
                    "version": str(artifact.get("version") or ""),
                    "description": str(artifact.get("description") or ""),
                    "tags": ", ".join(str(tag) for tag in (artifact.get("tags") or []) if str(tag).strip()),
                    "publisher": str(((artifact.get("publisher") or {}) if isinstance(artifact.get("publisher"), dict) else {}).get("owner_id") or ""),
                    "install_disabled": bool(op),
                    "operation_status": str(op.get("status") or "") if isinstance(op, dict) else "",
                    "operation_step": str(op.get("current_step") or op.get("message") or "") if isinstance(op, dict) else "",
                }
            )
        rows.sort(key=lambda item: str(item.get("id") or ""))
        return rows

    def _scenario_depends(names: list[str]) -> set[str]:
        deps: set[str] = set()
        for scenario_name in names:
            token = str(scenario_name or "").strip()
            if not token:
                continue
            try:
                manifest = read_manifest(token) or {}
            except Exception:
                continue
            raw_depends = manifest.get("depends") or []
            if not isinstance(raw_depends, (list, tuple)):
                continue
            for dep in raw_depends:
                dep_name = str(dep or "").strip()
                if dep_name:
                    deps.add(dep_name)
        return deps
    scenario_rows = _rows("scenarios", installed_scenarios)
    hidden_skill_ids = _scenario_depends([str(item.get("id") or "") for item in scenario_rows])
    skill_rows = [
        item
        for item in _rows("skills", installed_skills)
        if str(item.get("id") or "").strip() not in hidden_skill_ids
    ]

    return {
        "skills": skill_rows,
        "scenarios": scenario_rows,
    }


def _update_actions(conf, ui_state: dict[str, Any], reliability: dict[str, Any]) -> list[dict[str, Any]]:
    selected_node_id = str(ui_state.get("selected_node_id") or getattr(conf, "node_id", "") or "").strip()
    local_node_id = str(getattr(conf, "node_id", "") or "").strip()
    role = str(getattr(conf, "role", "") or "").strip().lower()
    target_kind = "local" if not selected_node_id or selected_node_id == local_node_id else "member"
    title = "Update skills & scenarios"
    if target_kind == "member":
        member = _selected_member_entry(reliability, selected_node_id)
        member_label = str(member.get("node_label") or member.get("label") or "").strip() or "Member"
        title = f"Update skills & scenarios ({member_label})"
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
        },
        {
            "id": "marketplace",
            "title": "Marketplace",
            "status": "ok",
            "description": "Browse registry catalog and install missing skills or scenarios.",
        },
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
        sync_result = sync_workspace_sparse_to_registry(ctx)
        payload["workspace_synced"] = bool(sync_result.get("ok"))
        payload["skills_synced"] = bool(sync_result.get("ok"))
        payload["scenarios_synced"] = bool(sync_result.get("ok"))
        payload["skills"] = [str(name) for name in (sync_result.get("skills") or []) if str(name).strip()]
        payload["scenarios"] = [str(name) for name in (sync_result.get("scenarios") or []) if str(name).strip()]
        fallback_used = sync_result.get("fallback_used") or {}
        if isinstance(fallback_used, dict) and fallback_used:
            payload["fallback_used"] = fallback_used
        if not sync_result.get("ok"):
            errors["workspace_sync"] = str(sync_result.get("error") or sync_result.get("errors") or "workspace sync failed")
    except Exception as exc:
        sync_result = {"skills": [], "scenarios": []}
        payload["workspace_synced"] = False
        payload["skills_synced"] = False
        payload["scenarios_synced"] = False
        errors["workspace_sync"] = f"{type(exc).__name__}: {exc}"

    target_webspace = default_webspace_id()
    runtime_updated: list[str] = []
    runtime_errors: dict[str, str] = {}
    for name in [str(item) for item in (sync_result.get("skills") or []) if str(item).strip()]:
        if not name:
            continue
        try:
            try:
                source_meta = ctx.skills_repo.get(name)
            except Exception:
                source_meta = None
            source_version = str(getattr(source_meta, "version", None) or "").strip()
            result = refresh_skill_runtime(
                skill_mgr,
                name,
                webspace_id=target_webspace,
                source_version=source_version,
                migrate_runtime=True,
                ensure_installed=True,
            )
            if bool(result.get("runtime_updated")) or bool(result.get("runtime_migrated")):
                runtime_updated.append(name)
        except Exception as exc:
            runtime_errors[name] = f"{type(exc).__name__}: {exc}"

    payload["runtime_updated"] = sorted(set(runtime_updated))
    if runtime_errors:
        errors["runtime_update"] = f"{len(runtime_errors)} skills failed"
        payload["runtime_update_errors"] = runtime_errors
    try:
        payload["webspace_refresh"] = rebuild_webspace_projection_sync(
            webspace_id=target_webspace,
            action="infrastate_adaos_update_sync",
            source_of_truth="scenario_projection",
        )
    except Exception as exc:
        errors["webspace_refresh"] = f"{type(exc).__name__}: {exc}"

    if errors:
        payload["ok"] = False
        payload["errors"] = errors
    return payload


def _forget_subnet_local() -> dict[str, Any]:
    conf = load_config()
    role = str(getattr(conf, "role", "") or "").strip().lower()
    try:
        from adaos.services.registry.subnet_directory import get_directory

        directory = get_directory()
        remembered = directory.list_known_nodes()
        remembered_ids = [
            str(item.get("node_id") or "").strip()
            for item in remembered
            if isinstance(item, dict) and str(item.get("node_id") or "").strip()
        ]
        directory.clear_all()
    except Exception as exc:
        raise RuntimeError(f"failed to clear subnet directory: {exc}") from exc

    result: dict[str, Any] = {
        "ok": True,
        "accepted": True,
        "scope": "subnet_directory",
        "forgotten_total": len(remembered_ids),
        "forgotten_node_ids": remembered_ids,
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
    }
    if role != "hub":
        return result

    refresh_requested = 0
    try:
        from adaos.services.subnet.link_manager import get_hub_link_manager

        manager = get_hub_link_manager()
        snapshot = manager.snapshot()
        members = snapshot.get("members") if isinstance(snapshot.get("members"), list) else []
        for item in members:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or "").strip()
            if not node_id:
                continue
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(manager.request_member_snapshot(node_id, reason="infrastate.forget_subnet"))
                refresh_requested += 1
            else:
                loop.create_task(manager.request_member_snapshot(node_id, reason="infrastate.forget_subnet"))
                refresh_requested += 1
    except Exception:
        _log.debug("failed to request member snapshots after subnet forget", exc_info=True)
    result["refresh_requested"] = refresh_requested
    return result


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


def _diagnostic_item(
    item_id: str,
    title: str,
    content: Any,
    *,
    status: str = "idle",
    subtitle: str = "",
    copy_text: str | None = None,
    preview_limit: int = 400,
) -> dict[str, Any]:
    text = _safe_json_text(content).strip()
    rendered_copy = str(copy_text if copy_text is not None else text).strip()
    return {
        "id": item_id,
        "title": title,
        "status": status,
        "subtitle": subtitle,
        "preview": text[-preview_limit:].strip(),
        "content": text[-12000:].strip(),
        "copy_text": rendered_copy,
    }


def _read_text_command(cmd: list[str], *, timeout_sec: float = 8.0) -> str:
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
    except Exception:
        return ""
    output = str(completed.stdout or "").strip()
    if output:
        return output
    stderr = str(completed.stderr or "").strip()
    return stderr


def _slot_tree_text(slot_dir: Path, *, max_depth: int = 2) -> str:
    try:
        root = slot_dir.expanduser().resolve()
    except Exception:
        root = slot_dir
    if not root.exists():
        return ""
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        try:
            rel = path.relative_to(root)
        except Exception:
            continue
        depth = len(rel.parts)
        if depth > max_depth:
            continue
        rendered = rel.as_posix()
        if path.is_dir():
            lines.append(rendered)
        else:
            lines.append(rendered)
    return "\n".join(lines).strip()


def _target_slot_id(status: dict[str, Any], last_result: dict[str, Any], slots_payload: dict[str, Any]) -> str:
    for candidate in (
        last_result.get("target_slot") if isinstance(last_result, dict) else "",
        (last_result.get("plan") or {}).get("target_slot") if isinstance(last_result.get("plan"), dict) else "",
        status.get("target_slot") if isinstance(status, dict) else "",
        (status.get("plan") or {}).get("target_slot") if isinstance(status.get("plan"), dict) else "",
        slots_payload.get("inactive_slot") if isinstance(slots_payload, dict) else "",
    ):
        token = str(candidate or "").strip()
        if token:
            return token
    return ""


def _core_update_required_commands(last_result: dict[str, Any], slots_payload: dict[str, Any]) -> str:
    target_slot = _target_slot_id({}, last_result, slots_payload) or "B"
    slot_dir = _base_dir() / "state" / "core_slots" / "slots" / target_slot
    return "\n".join(
        [
            f"cat {_base_dir() / 'state' / 'core_update' / 'last_result.json'}",
            f"find {slot_dir} -maxdepth 2 -printf '%P\\n' | sort",
            "journalctl -u adaos.service -n 120 --no-pager",
            f"cat {_base_dir() / 'state' / 'supervisor' / 'runtime.json'}",
        ]
    ).strip()


def _core_update_diagnostic_items(
    status: dict[str, Any],
    last_result: dict[str, Any],
    slots_payload: dict[str, Any],
    *,
    local_node: bool = True,
) -> list[dict[str, Any]]:
    target_slot = _target_slot_id(status, last_result, slots_payload)
    items: list[dict[str, Any]] = []
    commands_text = _core_update_required_commands(last_result, slots_payload)
    items.append(
        _diagnostic_item(
            "core-update-diagnostic-commands",
            "Required commands",
            commands_text,
            status="idle",
            subtitle="Exact commands we usually ask to collect during core-update debugging",
        )
    )
    if isinstance(last_result, dict) and last_result:
        items.append(
            _diagnostic_item(
                "core-update-last-result",
                "last_result.json",
                last_result,
                status="warn" if str(last_result.get("state") or "").strip().lower() == "failed" else "idle",
                subtitle=str(_base_dir() / "state" / "core_update" / "last_result.json"),
            )
        )
    if isinstance(status, dict) and status:
        items.append(
            _diagnostic_item(
                "core-update-status",
                "status.json",
                status,
                status="idle",
                subtitle=str(_base_dir() / "state" / "core_update" / "status.json"),
            )
        )
    if not local_node:
        return items

    runtime_path = _base_dir() / "state" / "supervisor" / "runtime.json"
    runtime_payload = _read_json(runtime_path)
    if runtime_payload:
        items.append(
            _diagnostic_item(
                "supervisor-runtime",
                "supervisor/runtime.json",
                runtime_payload,
                status="idle",
                subtitle=str(runtime_path),
            )
        )

    if target_slot:
        slot_dir = _base_dir() / "state" / "core_slots" / "slots" / target_slot
        slot_tree = _slot_tree_text(slot_dir, max_depth=2)
        if slot_tree:
            items.append(
                _diagnostic_item(
                    "target-slot-tree",
                    f"slot {target_slot} tree",
                    slot_tree,
                    status="idle",
                    subtitle=str(slot_dir),
                )
            )

    journal_text = _read_text_command(["journalctl", "-u", "adaos.service", "-n", "120", "--no-pager"])
    if journal_text:
        items.append(
            _diagnostic_item(
                "adaos-service-journal",
                "journalctl -u adaos.service -n 120",
                journal_text,
                status="idle",
                subtitle="Recent systemd service log tail",
            )
        )
    return items


def _core_update_diagnostic_actions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []
    bundle_parts: list[str] = []
    for item in items:
        title = str(item.get("title") or item.get("id") or "diagnostic").strip()
        payload = str(item.get("copy_text") or item.get("content") or "").strip()
        if not payload:
            continue
        bundle_parts.append(f"## {title}\n{payload}")
    bundle_text = "\n\n".join(bundle_parts).strip()
    actions: list[dict[str, Any]] = []
    if bundle_text:
        actions.append(
            {
                "id": "copy_core_update_diag_bundle",
                "label": "Copy all diagnostics",
                "title": "Copy all diagnostics",
                "text": bundle_text,
            }
        )
    commands_item = next((item for item in items if str(item.get("id") or "") == "core-update-diagnostic-commands"), None)
    if isinstance(commands_item, dict):
        commands_text = str(commands_item.get("copy_text") or "").strip()
        if commands_text:
            actions.append(
                {
                    "id": "copy_core_update_diag_commands",
                    "label": "Copy commands",
                    "title": "Copy commands",
                    "text": commands_text,
                }
            )
    return actions


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
        manifest_path = _skill_manifest_path()
        if manifest_path is None:
            return
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return
        entries = payload.get("data_projections") or []
        if not isinstance(entries, list) or not entries:
            return
        ctx.projections.load_entries(entries)
        _log.debug("loaded infrastate data_projections path=%s entries=%d", manifest_path, len(entries))
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
            encoding="utf-8",
            errors="replace",
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


def _validated_runtime_source(status: dict[str, Any], last_result: dict[str, Any]) -> dict[str, Any]:
    for candidate in (status, last_result):
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("state") or "").strip().lower() != "succeeded":
            continue
        if str(candidate.get("phase") or "").strip().lower() != "validate":
            continue
        manifest = candidate.get("manifest")
        if not isinstance(manifest, dict) or not manifest:
            continue
        slot = str(candidate.get("target_slot") or manifest.get("slot") or "").strip().upper()
        if not slot:
            continue
        return {
            "slot": slot,
            "manifest": dict(manifest),
        }
    return {}


def _effective_runtime_projection(
    status: dict[str, Any],
    last_result: dict[str, Any],
    slots_payload: dict[str, Any],
    build: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    validated = _validated_runtime_source(status, last_result)
    if not validated:
        return slots_payload, build

    slot = str(validated.get("slot") or "").strip().upper()
    manifest = validated.get("manifest") if isinstance(validated.get("manifest"), dict) else {}
    if not slot or not manifest:
        return slots_payload, build

    effective_slots = dict(slots_payload or {})
    current_active = str(effective_slots.get("active_slot") or "").strip().upper()
    if current_active and current_active != slot:
        effective_slots["previous_slot"] = current_active
    effective_slots["active_slot"] = slot

    raw_slots = effective_slots.get("slots")
    slot_items = dict(raw_slots) if isinstance(raw_slots, dict) else {}
    current_slot_meta = slot_items.get(slot)
    slot_meta = dict(current_slot_meta) if isinstance(current_slot_meta, dict) else {}
    current_manifest = slot_meta.get("manifest")
    merged_manifest = dict(current_manifest) if isinstance(current_manifest, dict) else {}
    merged_manifest.update(manifest)
    merged_manifest["slot"] = slot
    slot_meta["manifest"] = merged_manifest
    slot_items[slot] = slot_meta
    effective_slots["slots"] = slot_items
    effective_slots["active_manifest"] = merged_manifest

    effective_build = dict(build or {})
    effective_build["runtime_version"] = str(merged_manifest.get("target_version") or effective_build.get("runtime_version") or "")
    effective_build["runtime_git_commit"] = str(merged_manifest.get("git_commit") or effective_build.get("runtime_git_commit") or "")
    effective_build["runtime_git_short_commit"] = str(
        merged_manifest.get("git_short_commit") or effective_build.get("runtime_git_short_commit") or ""
    )
    effective_build["runtime_git_branch"] = str(
        merged_manifest.get("git_branch") or merged_manifest.get("target_rev") or effective_build.get("runtime_git_branch") or ""
    )
    effective_build["runtime_git_subject"] = str(merged_manifest.get("git_subject") or effective_build.get("runtime_git_subject") or "")
    return effective_slots, effective_build


def _ui_state() -> dict[str, Any]:
    raw = skill_memory_get(_UI_STATE_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _write_ui_state(**updates: Any) -> dict[str, Any]:
    payload = dict(_ui_state())
    payload.update(updates)
    skill_memory_set(_UI_STATE_KEY, payload)
    return payload


def _summary_render_state() -> dict[str, Any]:
    raw = skill_memory_get(_SUMMARY_RENDER_STATE_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _write_summary_render_state(payload: dict[str, Any]) -> dict[str, Any]:
    skill_memory_set(_SUMMARY_RENDER_STATE_KEY, payload)
    return payload


def _summary_render_context_key(selected_kind: str, selected_node_id: str, selected_yjs_webspace_id: str) -> str:
    kind = str(selected_kind or "local").strip() or "local"
    node_id = str(selected_node_id or "local").strip() or "local"
    webspace_id = str(selected_yjs_webspace_id or "-").strip() or "-"
    return f"{kind}:{node_id}:{webspace_id}"


def _summary_segment_key(segment: str, index: int) -> str:
    text = str(segment or "").strip()
    if "=" in text:
        head = text.split("=", 1)[0].strip()
        if head:
            return f"eq:{head}"
    if ":" in text:
        head = text.split(":", 1)[0].strip()
        if head and " " not in head and len(head) <= 32:
            return f"colon:{head}"
    return f"index:{index}"


def _unicode_bold_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    upper_src = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lower_src = "abcdefghijklmnopqrstuvwxyz"
    digit_src = "0123456789"
    upper_dst = "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙"
    lower_dst = "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳"
    digit_dst = "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"
    translation = str.maketrans(
        {src: dst for src, dst in zip(upper_src + lower_src + digit_src, upper_dst + lower_dst + digit_dst)}
    )
    return text.translate(translation)


def _highlight_changed_summary_text(current: Any, previous: Any) -> str:
    current_text = str(current or "").strip()
    previous_text = str(previous or "").strip()
    if not current_text:
        return ""
    if not previous_text:
        return current_text
    if " | " not in current_text:
        return current_text if current_text == previous_text else _unicode_bold_text(current_text)
    current_segments = [part.strip() for part in current_text.split(" | ")]
    previous_segments = [part.strip() for part in previous_text.split(" | ")] if previous_text else []
    previous_map = {
        _summary_segment_key(segment, index): segment
        for index, segment in enumerate(previous_segments)
        if segment
    }
    rendered: list[str] = []
    for index, segment in enumerate(current_segments):
        if not segment:
            continue
        prev_segment = previous_map.get(_summary_segment_key(segment, index))
        rendered.append(segment if segment == prev_segment else _unicode_bold_text(segment))
    return " | ".join(rendered)


def _highlight_summary_changes(summary: dict[str, Any], *, context_key: str) -> dict[str, Any]:
    payload = dict(summary or {})
    state = _summary_render_state()
    previous = state.get(context_key) if isinstance(state.get(context_key), dict) else {}
    for field in ("value", "subtitle", "description"):
        payload[field] = _highlight_changed_summary_text(payload.get(field), previous.get(field))
    next_state = dict(state)
    next_state[context_key] = {
        "value": str(summary.get("value") or "").strip(),
        "subtitle": str(summary.get("subtitle") or "").strip(),
        "description": str(summary.get("description") or "").strip(),
    }
    _write_summary_render_state(next_state)
    return payload


def _set_background_refresh_pending(*, webspace_id: str | None, reason: str) -> None:
    global _background_refresh_pending
    global _background_refresh_reason
    global _background_refresh_webspace_id

    token = str(webspace_id or "").strip() or None
    _background_refresh_pending = True
    if token:
        _background_refresh_webspace_id = token
    _background_refresh_reason = str(reason or "runtime.event").strip() or "runtime.event"
    _write_ui_state(
        background_refresh_pending=True,
        background_refresh_running=bool(_background_refresh_task and not _background_refresh_task.done()),
        background_refresh_reason=_background_refresh_reason,
        background_refresh_requested_at=time.time(),
        background_refresh_webspace_id=_background_refresh_webspace_id or "",
        background_refresh_error="",
    )


async def _background_refresh_worker() -> None:
    global _background_refresh_pending
    global _background_refresh_reason
    global _background_refresh_task
    global _background_refresh_webspace_id

    try:
        while True:
            await asyncio.sleep(_BACKGROUND_REFRESH_DEBOUNCE_S)
            webspace_id = _background_refresh_webspace_id
            reason = _background_refresh_reason or "runtime.event"
            _background_refresh_pending = False
            _background_refresh_reason = ""
            _background_refresh_webspace_id = None
            started_at = time.time()
            _write_ui_state(
                background_refresh_pending=False,
                background_refresh_running=True,
                background_refresh_reason=reason,
                background_refresh_started_at=started_at,
                background_refresh_webspace_id=webspace_id or "",
                background_refresh_error="",
            )
            try:
                await _refresh_snapshot_async(webspace_id=webspace_id, allow_cache=True)
            except (asyncio.CancelledError, RuntimeError) as exc:
                if isinstance(exc, RuntimeError) and "Executor shutdown has been called" not in str(exc):
                    raise
                _write_ui_state(
                    background_refresh_running=False,
                    background_refresh_finished_at=time.time(),
                    background_refresh_error="",
                )
                break
            except Exception as exc:
                _write_ui_state(
                    background_refresh_running=False,
                    background_refresh_finished_at=time.time(),
                    background_refresh_error=f"{type(exc).__name__}: {exc}",
                )
                _log.warning("background infrastate refresh failed reason=%s webspace=%s", reason, webspace_id or "-", exc_info=True)
            else:
                _write_ui_state(
                    background_refresh_running=False,
                    background_refresh_finished_at=time.time(),
                    background_refresh_error="",
                )
            await asyncio.sleep(0)
            if not _background_refresh_pending:
                break
    finally:
        _background_refresh_task = None
        if _background_refresh_pending:
            _schedule_snapshot_refresh(reason="background.refresh.retry")


def _background_refresh_done(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        _log.warning("background infrastate refresh task status failed", exc_info=True)
        return
    if exc is None:
        return
    try:
        _write_ui_state(
            background_refresh_running=False,
            background_refresh_finished_at=time.time(),
            background_refresh_error=f"{type(exc).__name__}: {exc}",
        )
    except Exception:
        pass
    _log.warning(
        "background infrastate refresh task failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _run_background_refresh_thread() -> None:
    global _background_refresh_thread
    pushed = False
    try:
        pushed = set_current_skill("infrastate_skill")
        asyncio.run(_background_refresh_worker())
    except Exception:
        _log.warning("background infrastate refresh thread failed", exc_info=True)
    finally:
        if pushed:
            clear_current_skill()
        _background_refresh_thread = None


def _schedule_snapshot_refresh(*, webspace_id: str | None = None, reason: str = "runtime.event") -> None:
    global _background_refresh_thread
    global _background_refresh_pending
    global _background_refresh_reason
    global _background_refresh_task
    global _background_refresh_webspace_id

    _set_background_refresh_pending(webspace_id=webspace_id, reason=reason)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _background_refresh_task is not None and not _background_refresh_task.done():
        return
    _background_refresh_task = loop.create_task(
        _background_refresh_worker(),
        name="infrastate-background-refresh",
    )
    _background_refresh_task.add_done_callback(_background_refresh_done)


def _hub_member_connection_state(reliability: dict[str, Any]) -> dict[str, Any]:
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    state = runtime.get("hub_member_connection_state")
    return state if isinstance(state, dict) else {}


def _node_label(node_names: Any, *, fallback: str) -> str:
    if isinstance(node_names, list):
        for item in node_names:
            if isinstance(item, dict):
                continue
            token = str(item or "").strip()
            if token:
                folded = token.casefold()
                if folded in {"default", "desktop", "webspace", "workspace"}:
                    continue
                if token.startswith("{") and token.endswith("}"):
                    continue
                return token
    return fallback


def _node_tabs(conf, ui_state: dict[str, Any], reliability: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected_node_id = str(ui_state.get("selected_node_id") or "").strip()
    local_node_id = str(getattr(conf, "node_id", "") or "")
    role = str(getattr(conf, "role", "") or "").strip().lower()
    local_names = list(getattr(conf, "node_names", []) or [])
    local_display = node_display_from_config(conf)
    local_display_label = str(local_display.get("node_label") or "").strip() or _node_label(
        local_names,
        fallback="Node 0" if role == "hub" else "Node 1",
    )
    items: list[dict[str, Any]] = [
        {
            "id": local_node_id,
            "label": local_display_label,
            "title": "Local node",
            "role": role,
            "node_id": local_node_id,
            "node_names": local_names,
            "kind": "local",
            "node_compact_label": local_display.get("node_compact_label"),
            "node_color": local_display.get("node_color"),
            "node_index": local_display.get("node_index"),
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
            member_label = str(member.get("node_label") or member.get("label") or "").strip() or _node_label(
                member_names,
                fallback=f"Node {index}",
            )
            items.append(
                {
                    "id": node_id,
                    "label": member_label,
                    "title": "Connected member" if connected else ("Observed member" if observed_via == "subnet_directory" else "Member"),
                    "role": "member",
                    "node_id": node_id,
                    "node_names": member_names,
                    "kind": "member",
                    "state": str(member.get("state") or "connected"),
                    "connected": connected,
                    "observed_via": observed_via,
                    "node_compact_label": member.get("node_compact_label"),
                    "node_color": member.get("node_color"),
                    "node_index": member.get("node_index"),
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
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
    selected_webspace = sync_runtime.get("selected_webspace") if isinstance(sync_runtime.get("selected_webspace"), dict) else {}
    selected_id = _selected_yjs_webspace_id(ui_state, reliability)
    items: list[dict[str, Any]] = []
    for index, webspace_id in enumerate(sorted(str(key) for key in webspaces.keys()), start=1):
        entry = webspaces.get(webspace_id) if isinstance(webspaces.get(webspace_id), dict) else {}
        title = str(selected_webspace.get("title") or "").strip() if webspace_id == selected_id else ""
        label = "default" if webspace_id == default_webspace_id() else webspace_id
        if webspace_id == selected_id:
            label = f"{label} *"
        items.append(
            {
                "id": webspace_id,
                "label": label,
                "title": "Selected Yjs webspace" if webspace_id == selected_id else f"Yjs webspace {index}",
                "subtitle": (
                    f"{title + ' | ' if title else ''}"
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


def _self_supervisor_base_url(conf) -> str | None:
    host = str(
        getattr(conf, "supervisor_host", None)
        or os.getenv("ADAOS_SUPERVISOR_HOST")
        or "127.0.0.1"
    ).strip() or "127.0.0.1"
    port = str(
        getattr(conf, "supervisor_port", None)
        or os.getenv("ADAOS_SUPERVISOR_PORT")
        or "8776"
    ).strip()
    if not port:
        return None
    return f"http://{host}:{port}"


def _post_local_admin(conf, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = _self_headers(conf)
    payload_body = body or {}
    supervisor_path = ""
    if path.startswith("/api/admin/update/"):
        supervisor_path = path.replace("/api/admin/update/", "/api/supervisor/update/")
    if supervisor_path:
        supervisor_base = _self_supervisor_base_url(conf)
        if supervisor_base:
            try:
                response = requests.post(
                    supervisor_base + supervisor_path,
                    headers=headers,
                    json=payload_body,
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
            except Exception:
                pass
    response = requests.post(
        _self_base_url(conf) + path,
        headers=headers,
        json=payload_body,
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


def _skill_runtime_migration_report(status: dict[str, Any], last_result: dict[str, Any]) -> dict[str, Any]:
    for candidate in (status, last_result):
        if not isinstance(candidate, dict):
            continue
        report = candidate.get("skill_runtime_migration")
        if isinstance(report, dict) and report:
            return dict(report)
        manifest = candidate.get("manifest")
        if not isinstance(manifest, dict):
            continue
        report = manifest.get("skill_runtime_migration")
        if isinstance(report, dict) and report:
            return dict(report)
    return {}


def _skill_runtime_rollback_report(status: dict[str, Any], last_result: dict[str, Any]) -> dict[str, Any]:
    for candidate in (status, last_result):
        if not isinstance(candidate, dict):
            continue
        report = candidate.get("skill_runtime_rollback")
        if isinstance(report, dict) and report:
            return dict(report)
    return {}


def _skill_post_commit_checks_report(status: dict[str, Any], last_result: dict[str, Any]) -> dict[str, Any]:
    for candidate in (status, last_result):
        if not isinstance(candidate, dict):
            continue
        report = candidate.get("skill_post_commit_checks")
        if isinstance(report, dict) and report:
            return dict(report)
    return {}


def _skill_runtime_migration_note(report: dict[str, Any]) -> str:
    if not isinstance(report, dict) or not report:
        return ""
    total = int(report.get("total") or 0)
    failed_total = int(report.get("failed_total") or 0)
    lifecycle_failed_total = int(report.get("lifecycle_failed_total") or 0)
    tests_failed_total = int(report.get("tests_failed_total") or 0)
    rollback_total = int(report.get("rollback_total") or 0)
    deactivated_total = int(report.get("deactivated_total") or 0)
    if total <= 0:
        return ""
    parts = [f"skill_migration={total - failed_total}/{total}"]
    if failed_total:
        failed = []
        for item in report.get("skills") or []:
            if not isinstance(item, dict) or bool(item.get("ok")):
                continue
            failure_kind = str(item.get("failure_kind") or "").strip()
            failed_stage = str(item.get("failed_stage") or "failed")
            if failure_kind:
                failed_stage = f"{failure_kind}/{failed_stage}"
            failed.append(
                f"{str(item.get('skill') or 'skill')}:{failed_stage}"
            )
        if failed:
            parts.append("failed=" + ",".join(failed[:3]))
            if len(failed) > 3:
                parts[-1] += f"(+{len(failed) - 3})"
    if lifecycle_failed_total:
        parts.append(f"lifecycle_failed={lifecycle_failed_total}")
    if tests_failed_total:
        parts.append(f"tests_failed={tests_failed_total}")
    if rollback_total:
        parts.append(f"rollback={rollback_total}")
    if deactivated_total:
        parts.append(f"deactivated={deactivated_total}")
    return " | ".join(parts)


def _skill_runtime_rollback_note(report: dict[str, Any]) -> str:
    if not isinstance(report, dict) or not report:
        return ""
    total = int(report.get("total") or 0)
    failed_total = int(report.get("failed_total") or 0)
    rollback_total = int(report.get("rollback_total") or 0)
    skipped_total = int(report.get("skipped_total") or 0)
    if total <= 0:
        return ""
    parts = [f"skill_rollback={rollback_total}/{total}"]
    if failed_total:
        failed = []
        for item in report.get("skills") or []:
            if not isinstance(item, dict) or bool(item.get("ok")):
                continue
            failed.append(str(item.get("skill") or "skill"))
        if failed:
            parts.append("failed=" + ",".join(failed[:3]))
            if len(failed) > 3:
                parts[-1] += f"(+{len(failed) - 3})"
    if skipped_total:
        parts.append(f"skipped={skipped_total}")
    return " | ".join(parts)


def _skill_post_commit_checks_note(report: dict[str, Any]) -> str:
    if not isinstance(report, dict) or not report:
        return ""
    total = int(report.get("total") or 0)
    failed_total = int(report.get("failed_total") or 0)
    lifecycle_failed_total = int(report.get("lifecycle_failed_total") or 0)
    tests_failed_total = int(report.get("tests_failed_total") or 0)
    deactivated_total = int(report.get("deactivated_total") or 0)
    if total <= 0 and not report.get("error"):
        return ""
    parts = []
    if total > 0:
        parts.append(f"skill_post_commit={total - failed_total}/{total}")
    failed = []
    quarantined = []
    for item in report.get("skills") or []:
        if not isinstance(item, dict):
            continue
        if bool(item.get("ok")) and not bool(item.get("deactivated")):
            continue
        deactivation = item.get("deactivation") if isinstance(item.get("deactivation"), dict) else {}
        if bool(item.get("ok")) and not bool(deactivation.get("committed_core_switch")):
            continue
        failure_kind = str(item.get("failure_kind") or deactivation.get("failure_kind") or "").strip()
        failed_stage = str(item.get("failed_stage") or deactivation.get("failed_stage") or "failed")
        if failure_kind:
            failed_stage = f"{failure_kind}/{failed_stage}"
        skill_label = str(item.get("skill") or "skill")
        if not bool(item.get("ok")):
            failed.append(f"{skill_label}:{failed_stage}")
        if bool(item.get("deactivated")) and bool(deactivation.get("committed_core_switch")):
            quarantined.append(f"{skill_label}:{failed_stage}")
    if failed_total:
        if failed:
            parts.append("failed=" + ",".join(failed[:3]))
            if len(failed) > 3:
                parts[-1] += f"(+{len(failed) - 3})"
    if quarantined:
        parts.append("quarantine=" + ",".join(quarantined[:3]))
        if len(quarantined) > 3:
            parts[-1] += f"(+{len(quarantined) - 3})"
    if lifecycle_failed_total:
        parts.append(f"lifecycle_failed={lifecycle_failed_total}")
    if tests_failed_total:
        parts.append(f"tests_failed={tests_failed_total}")
    if deactivated_total:
        parts.append(f"deactivated={deactivated_total}")
    error_text = str(report.get("error") or "").strip()
    if error_text:
        parts.append(f"error={error_text}")
    return " | ".join(parts)


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
    supervisor_note = _supervisor_transition_note(status)
    return [
        {"id": "lifecycle", "title": "Lifecycle", "status": node_state, "description": str(lifecycle.get("reason") or "ready")},
        {"id": "update_state", "title": "Update state", "status": state, "description": phase or state},
        {"id": "supervisor_transition", "title": "Supervisor transition", "status": supervisor_note.get("status"), "description": supervisor_note.get("description")},
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


def _supervisor_transition_note(status: dict[str, Any]) -> dict[str, str]:
    state = str(status.get("state") or "idle").strip().lower()
    phase = str(status.get("phase") or "").strip().lower()
    message = str(status.get("message") or "").strip()
    restored_slot = str(status.get("restored_slot") or "").strip()
    subsequent = bool(status.get("subsequent_transition"))
    scheduled_for = _countdown_remaining_sec(status)
    suffix = ""
    if subsequent:
        suffix = " | subsequent transition queued"
    if state == "planned":
        planned_reason = str(status.get("planned_reason") or "").strip().lower()
        description = message or "core update is scheduled"
        if scheduled_for > 0:
            description += f" | defer {scheduled_for}s"
        if planned_reason == "minimum_update_period":
            description += " | minimum update interval"
        return {
            "status": "warn",
            "description": description + suffix,
        }
    if state == "validated" and phase == "root_promotion_pending":
        return {
            "status": "warn",
            "description": (message or "validated slot is running; root promotion is still pending") + suffix,
        }
    if state == "succeeded" and phase == "root_promoted":
        return {
            "status": "warn",
            "description": (message or "root bootstrap files were promoted; restart adaos.service to activate") + suffix,
        }
    if state == "failed":
        description = message or "update failed"
        if restored_slot:
            description += f" | restored slot {restored_slot}"
        return {"status": "warn", "description": description + suffix}
    if state in {"countdown", "draining", "stopping", "restarting", "applying"}:
        description = message or phase or state
        if state == "countdown" and scheduled_for > 0:
            description += f" | remaining={scheduled_for}s"
        return {
            "status": "ok" if state == "countdown" else "warn",
            "description": description + suffix,
        }
    return {
        "status": "idle",
        "description": (message or phase or state or "idle") + suffix,
    }


def _summary_buttons(status: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(status.get("state") or "")
    remaining_sec = _countdown_remaining_sec(status)
    if state not in {"planned", "countdown", "draining", "stopping"}:
        return []
    if remaining_sec <= 0 and state == "countdown":
        return []
    label = "Cancel update"
    if remaining_sec > 0:
        label = f"{label} ({remaining_sec}s)"
    buttons = [{"id": "cancel_update", "label": label, "title": label, "kind": "danger"}]
    if state in {"planned", "countdown"}:
        buttons.insert(0, {"id": "defer_update_15m", "label": "Delay 15m", "title": "Delay 15m", "kind": "secondary"})
        buttons.insert(0, {"id": "defer_update_5m", "label": "Delay 5m", "title": "Delay 5m", "kind": "secondary"})
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
        assessment = sync_runtime.get("assessment") if isinstance(sync_runtime.get("assessment"), dict) else {}
        recovery_playbook = sync_runtime.get("recovery_playbook") if isinstance(sync_runtime.get("recovery_playbook"), dict) else {}
        recovery_guidance = sync_runtime.get("recovery_guidance") if isinstance(sync_runtime.get("recovery_guidance"), dict) else {}
        webspace_guidance = sync_runtime.get("webspace_guidance") if isinstance(sync_runtime.get("webspace_guidance"), dict) else {}
        selected_webspace = sync_runtime.get("selected_webspace") if isinstance(sync_runtime.get("selected_webspace"), dict) else {}
        recovery_order = recovery_playbook.get("action_order") if isinstance(recovery_playbook.get("action_order"), list) else []
        webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
        selected_ws_id = str(sync_runtime.get("selected_webspace_id") or "").strip() or default_webspace_id()
        selected_ws = webspaces.get(selected_ws_id) if isinstance(webspaces.get(selected_ws_id), dict) else {}
        recommended_action = str(recovery_guidance.get("recommended_action") or "").strip()
        recommended_webspace_action = str(webspace_guidance.get("recommended_action") or "").strip()
        rebuild = selected_webspace.get("rebuild") if isinstance(selected_webspace.get("rebuild"), dict) else {}
        items.append(
            {
                "id": "yjs_sync_runtime",
                "title": "Yjs sync runtime",
                "status": "warn" if str(assessment.get("state") or "") in {"pressure", "degraded"} else "ok",
                "description": (
                    f"{assessment.get('state') or 'unknown'} | "
                    f"webspaces={sync_runtime.get('webspace_total') or 0} | "
                    f"active={sync_runtime.get('active_webspace_total') or 0} | "
                    f"compacted={sync_runtime.get('compacted_webspace_total') or 0}"
                ),
                "subtitle": (
                    f"selected={selected_ws_id}:{selected_ws.get('log_mode') or '-'} | "
                    f"replay={selected_ws.get('replay_window_entries') or 0}/"
                    f"{selected_ws.get('replay_window_limit') or 0} | "
                    f"backups={selected_ws.get('backup_total') or 0} | "
                    f"snapshot={'yes' if selected_ws.get('snapshot_file_exists') else 'no'} | "
                    f"next={recommended_action or '-'} | "
                    f"home={selected_webspace.get('home_scenario') or '-'} | "
                    f"proj_scenario={selected_webspace.get('projection_active_scenario') or '-'} | "
                    f"proj={'match' if selected_webspace.get('projection_matches_home') is True else 'drift' if selected_webspace.get('projection_matches_home') is False else 'unknown'} | "
                    f"ws_next={recommended_webspace_action or '-'} | "
                    f"rebuild={rebuild.get('status') or '-'} | "
                    f"policy={'>'.join(str(item) for item in recovery_order) if recovery_order else '-'} | "
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
    skill_migration_note = _skill_runtime_migration_note(_skill_runtime_migration_report(status, last_result))
    if skill_migration_note:
        message += f" | {skill_migration_note}"
    skill_rollback_note = _skill_runtime_rollback_note(_skill_runtime_rollback_report(status, last_result))
    if skill_rollback_note:
        message += f" | {skill_rollback_note}"
    skill_post_commit_note = _skill_post_commit_checks_note(_skill_post_commit_checks_report(status, last_result))
    if skill_post_commit_note:
        message += f" | {skill_post_commit_note}"
    validation_summary = str(status.get("validation_error_summary") or "").strip()
    restored_slot = str(status.get("restored_slot") or "").strip()
    if state == "failed" and phase == "validate":
        if validation_summary:
            message += f" | validation: {validation_summary}"
        if restored_slot:
            message += f" | restored slot {restored_slot}"
    supervisor_note = _supervisor_transition_note(status)
    supervisor_desc = str(supervisor_note.get("description") or "").strip()
    if supervisor_desc and supervisor_desc not in message:
        message += f" | {supervisor_desc}"
    last_action = str(ui_state.get("last_action") or "").strip()
    last_action_at = float(ui_state.get("last_action_ts") or 0.0)
    last_refresh_at = float(ui_state.get("last_refresh_ts") or 0.0)
    if last_action:
        message += f" | action: {last_action}"
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
    recovery_guidance = sync_runtime.get("recovery_guidance") if isinstance(sync_runtime.get("recovery_guidance"), dict) else {}
    webspace_guidance = sync_runtime.get("webspace_guidance") if isinstance(sync_runtime.get("webspace_guidance"), dict) else {}
    selected_webspace_meta = sync_runtime.get("selected_webspace") if isinstance(sync_runtime.get("selected_webspace"), dict) else {}
    selected_yjs_webspace_id = _selected_yjs_webspace_id(ui_state, reliability)
    selected_sync_webspace = sync_webspaces.get(selected_yjs_webspace_id) if isinstance(sync_webspaces.get(selected_yjs_webspace_id), dict) else {}
    selected_member = selected_member if isinstance(selected_member, dict) else {}
    if selected_kind == "local" and selected_yjs_webspace_id:
        recommended_action = str(recovery_guidance.get("recommended_action") or "").strip()
        recommended_webspace_action = str(webspace_guidance.get("recommended_action") or "").strip()
        message += (
            f" | yjs_ws={selected_yjs_webspace_id}"
            f" {selected_sync_webspace.get('log_mode') or '-'}"
            f" replay={selected_sync_webspace.get('replay_window_entries') or 0}/"
            f"{selected_sync_webspace.get('replay_window_limit') or 0}"
        )
        if selected_sync_webspace:
            message += f" snapshot={'yes' if selected_sync_webspace.get('snapshot_file_exists') else 'no'}"
        if selected_webspace_meta:
            message += (
                f" home={selected_webspace_meta.get('home_scenario') or '-'}"
                f" proj_scenario={selected_webspace_meta.get('projection_active_scenario') or '-'}"
                f" mode={selected_webspace_meta.get('source_mode') or '-'}"
                f" rebuild={(selected_webspace_meta.get('rebuild') if isinstance(selected_webspace_meta.get('rebuild'), dict) else {}).get('status') or '-'}"
            )
        if recommended_action:
            message += f" next={recommended_action}"
        if recommended_webspace_action:
            message += f" ws_next={recommended_webspace_action}"
    if selected_kind != "local":
        remote_control = _remote_control_payload(
            selected_member.get("node_snapshot") if isinstance(selected_member.get("node_snapshot"), dict) else {},
            selected_member,
        )
        summary_label = "Node state"
        summary_value = str(status.get("state") or lifecycle.get("node_state") or selected_member.get("state") or "connected")
        build_ref = str(build.get("runtime_git_short_commit") or build.get("runtime_version") or build.get("version") or "").strip()
        selected_compact = str(selected_node.get("node_compact_label") or "").strip()
        summary_subtitle = f"{selected_label} | {selected_compact or 'N?'}"
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
        message += " yjs=hub-local-only"
    summary_payload = {
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
        "selected_yjs_snapshot_exists": bool(selected_sync_webspace.get("snapshot_file_exists")),
        "selected_yjs_home_scenario": str(selected_webspace_meta.get("home_scenario") or ""),
        "selected_yjs_source_mode": str(selected_webspace_meta.get("source_mode") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "root_url": str(getattr(getattr(conf, "root_settings", None), "base_url", "") or ""),
        "updated_at": float(status.get("updated_at") or time.time()),
        "last_action": last_action,
        "last_action_at": last_action_at or None,
        "last_refresh_at": last_refresh_at or None,
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
    return _highlight_summary_changes(
        summary_payload,
        context_key=_summary_render_context_key(selected_kind, selected_node_id, selected_yjs_webspace_id),
    )


def _action_items(status: dict[str, Any], ui_state: dict[str, Any], reliability: dict[str, Any]) -> list[dict[str, Any]]:
    last_refresh = float(ui_state.get("last_refresh_ts") or 0.0)
    last_action = str(ui_state.get("last_action") or "").strip()
    state = str(status.get("state") or "idle")
    selected_node_id = str(ui_state.get("selected_node_id") or "").strip()
    selected_yjs_webspace_id = _selected_yjs_webspace_id(ui_state, reliability)
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    action_overrides = sync_runtime.get("action_overrides") if isinstance(sync_runtime.get("action_overrides"), dict) else {}
    recovery_guidance = sync_runtime.get("recovery_guidance") if isinstance(sync_runtime.get("recovery_guidance"), dict) else {}
    webspace_guidance = sync_runtime.get("webspace_guidance") if isinstance(sync_runtime.get("webspace_guidance"), dict) else {}
    backup_override = action_overrides.get("backup") if isinstance(action_overrides.get("backup"), dict) else {}
    reload_override = action_overrides.get("reload") if isinstance(action_overrides.get("reload"), dict) else {}
    restore_override = action_overrides.get("restore") if isinstance(action_overrides.get("restore"), dict) else {}
    reset_override = action_overrides.get("reset") if isinstance(action_overrides.get("reset"), dict) else {}
    go_home_override = action_overrides.get("go_home") if isinstance(action_overrides.get("go_home"), dict) else {}
    set_home_current_override = (
        action_overrides.get("set_home_current") if isinstance(action_overrides.get("set_home_current"), dict) else {}
    )
    sync_webspaces = sync_runtime.get("webspaces") if isinstance(sync_runtime.get("webspaces"), dict) else {}
    selected_sync_webspace = sync_webspaces.get(selected_yjs_webspace_id) if isinstance(sync_webspaces.get(selected_yjs_webspace_id), dict) else {}
    recommended_action = str(recovery_guidance.get("recommended_action") or "").strip()
    recommended_webspace_action = str(webspace_guidance.get("recommended_action") or "").strip()
    alternate_webspace_action = str(webspace_guidance.get("alternate_action") or "").strip()
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
                "subtitle": "",
                "updated_at": last_refresh or None,
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
            "subtitle": "",
            "updated_at": last_refresh or None,
        },
        {
            "id": "cancel_update",
            "title": "Cancel update",
            "status": "warn" if state in {"planned", "countdown", "draining", "stopping"} else "idle",
            "description": "Cancel current countdown/update task",
            "subtitle": last_action if last_action == "cancel_update" else "",
        },
        {
            "id": "defer_update_5m",
            "title": "Delay 5m",
            "status": "ok" if state in {"planned", "countdown"} else "idle",
            "description": "Defer current scheduled update by five minutes",
            "subtitle": last_action if last_action == "defer_update_5m" else "",
        },
        {
            "id": "defer_update_15m",
            "title": "Delay 15m",
            "status": "ok" if state in {"planned", "countdown"} else "idle",
            "description": "Defer current scheduled update by fifteen minutes",
            "subtitle": last_action if last_action == "defer_update_15m" else "",
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
                    "status": "ok" if bool(backup_override.get("enabled", True)) else "idle",
                    "description": (
                        "Recommended first step. " if recommended_action == "backup" else ""
                    ) + str(backup_override.get("reason") or f"Persist Yjs snapshot for webspace {selected_yjs_webspace_id}"),
                    "subtitle": last_action if last_action == "yjs_backup" else "",
                },
                {
                    "id": "yjs_reload",
                    "title": "Yjs reload",
                    "status": "ok" if bool(reload_override.get("enabled", True)) else "idle",
                    "description": (
                        "Recommended first step. " if recommended_action == "reload" else ""
                    ) + str(reload_override.get("reason") or f"Reseed webspace {selected_yjs_webspace_id} from its current scenario"),
                    "subtitle": last_action if last_action == "yjs_reload" else "",
                },
                {
                    "id": "yjs_restore",
                    "title": "Yjs restore",
                    "status": "ok" if bool(restore_override.get("enabled")) else "idle",
                    "description": (
                        ("Recommended first step. " if recommended_action == "restore" else "")
                        + str(restore_override.get("reason") or f"Restore webspace {selected_yjs_webspace_id} from its latest disk snapshot")
                        if bool(restore_override.get("enabled"))
                        else str(restore_override.get("reason") or f"No disk snapshot available for webspace {selected_yjs_webspace_id}")
                    ),
                    "subtitle": last_action if last_action == "yjs_restore" else "",
                },
                {
                    "id": "yjs_reset",
                    "title": "Yjs reset",
                    "status": "warn" if bool(reset_override.get("enabled", True)) else "idle",
                    "description": (
                        "Recommended first step. " if recommended_action == "reset" else ""
                    ) + str(reset_override.get("reason") or f"Hard-reset webspace {selected_yjs_webspace_id} from its current scenario"),
                    "subtitle": last_action if last_action == "yjs_reset" else "",
                },
                {
                    "id": "yjs_go_home",
                    "title": "Yjs go home",
                    "status": "ok" if bool(go_home_override.get("enabled")) else "idle",
                    "description": (
                        "Recommended first step. " if recommended_webspace_action == "go_home" else ""
                    ) + str(go_home_override.get("reason") or f"Return webspace {selected_yjs_webspace_id} to its manifest home scenario"),
                    "subtitle": last_action if last_action == "yjs_go_home" else "",
                },
                {
                    "id": "yjs_set_home_current",
                    "title": "Yjs set current as home",
                    "status": "ok" if bool(set_home_current_override.get("enabled")) else "idle",
                    "description": (
                        ("Recommended first step. " if recommended_webspace_action == "set_home_current" else "")
                        + ("Alternative to go home. " if alternate_webspace_action == "set_home_current" else "")
                        + str(
                            set_home_current_override.get("reason")
                            or f"Persist the current projected scenario as home for webspace {selected_yjs_webspace_id}"
                        )
                        if bool(set_home_current_override.get("enabled"))
                        else str(
                            set_home_current_override.get("reason")
                            or f"Current projected scenario is unavailable for webspace {selected_yjs_webspace_id}"
                        )
                    ),
                    "subtitle": last_action if last_action == "yjs_set_home_current" else "",
                },
                {
                    "id": "forget_subnet",
                    "title": "Forget subnet",
                    "status": "warn",
                    "description": "Clear remembered member directory entries and cached remote projections. Connected members will republish themselves on the next snapshot.",
                    "subtitle": last_action if last_action == "forget_subnet" else "",
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


def _core_action_items(status: dict[str, Any], ui_state: dict[str, Any], reliability: dict[str, Any]) -> list[dict[str, Any]]:
    items = list(_action_items(status, ui_state, reliability))
    return [item for item in items if not str(item.get("id") or "").startswith("yjs_")]


def _yjs_action_items(status: dict[str, Any], ui_state: dict[str, Any], reliability: dict[str, Any]) -> list[dict[str, Any]]:
    selected_node_id = str(ui_state.get("selected_node_id") or "").strip()
    local_node_id = str(load_config().node_id or "")
    if selected_node_id and selected_node_id != local_node_id:
        selected_node = _selected_node_entry(reliability, selected_node_id)
        return [
            {
                "id": "yjs_scope",
                "title": "Yjs scope",
                "status": "idle",
                "description": "Yjs recovery and webspace controls run on the hub only; remote member tabs are read-only for sync control.",
                "subtitle": str(selected_node.get("label") or selected_node_id or "remote member"),
            }
        ]
    items = list(_action_items(status, ui_state, reliability))
    return [item for item in items if str(item.get("id") or "").startswith("yjs_")]


def _perform_action(action_id: str, conf, payload: Any | None = None) -> dict[str, Any]:
    status = read_core_update_status()
    selected_node_id = str(_ui_state().get("selected_node_id") or getattr(conf, "node_id", "") or "")
    if (
        action_id in {
            "start_update",
            "cancel_update",
            "refuse_update",
            "rollback",
            "drain",
            "restart_sidecar",
            "yjs_backup",
            "yjs_reload",
            "yjs_restore",
            "yjs_reset",
            "yjs_go_home",
            "yjs_set_home_current",
            "skill_activate",
            "skill_test",
            "skill_update",
            "skill_uninstall",
            "scenario_uninstall",
        }
        and selected_node_id
        and selected_node_id != str(getattr(conf, "node_id", "") or "")
    ):
        raise ValueError("remote member tabs are read-only for update and transport actions")
    if action_id in {"skill_activate", "skill_test", "skill_update", "skill_uninstall"}:
        name = str(_extract_param(payload, "name") or "").strip()
        if not name:
            raise ValueError("skill action requires skill name")
        webspace_id = str(_extract_param(payload, "webspace_id") or default_webspace_id()).strip() or default_webspace_id()
        ctx = get_ctx()
        mgr = SkillManager(
            repo=ctx.skills_repo,
            registry=SqliteSkillRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
            settings=ctx.settings,
        )
        if action_id == "skill_activate":
            prep = mgr.prepare_runtime(name, run_tests=False)
            slot = mgr.activate_for_space(
                name,
                version=getattr(prep, "version", None),
                slot=getattr(prep, "slot", None),
                space="default",
                webspace_id=webspace_id,
            )
            result = {
                "ok": True,
                "action": action_id,
                "name": name,
                "version": getattr(prep, "version", None),
                "slot": slot,
                "prepared": getattr(prep, "slot", None),
                "webspace_id": webspace_id,
            }
        elif action_id == "skill_test":
            try:
                tests = mgr.run_skill_tests(name)
                serialized_tests = {
                    test_name: {
                        "status": getattr(test_result, "status", ""),
                        "detail": getattr(test_result, "detail", None),
                    }
                    for test_name, test_result in (tests or {}).items()
                }
                result = {
                    "ok": True,
                    "action": action_id,
                    "name": name,
                    "mode": "active_runtime",
                    "tests": serialized_tests,
                }
            except Exception:
                prep = mgr.prepare_runtime(name, run_tests=True)
                serialized_tests = {
                    test_name: {
                        "status": getattr(test_result, "status", ""),
                        "detail": getattr(test_result, "detail", None),
                    }
                    for test_name, test_result in getattr(prep, "tests", {}).items()
                }
                result = {
                    "ok": True,
                    "action": action_id,
                    "name": name,
                    "mode": "prepared_runtime",
                    "version": getattr(prep, "version", None),
                    "slot": getattr(prep, "slot", None),
                    "tests": serialized_tests,
                }
        elif action_id == "skill_uninstall":
            used_by = _skill_usage_by_scenarios().get(name, [])
            if used_by:
                raise ValueError(f"skill '{name}' is used by scenarios: {', '.join(used_by)}")
            mgr.uninstall(name)
            result = {
                "ok": True,
                "action": action_id,
                "name": name,
                "webspace_id": webspace_id,
            }
        else:
            service = SkillUpdateService(ctx)
            update_result = service.request_update(name, dry_run=False)
            runtime_status_before = {}
            try:
                runtime_status_before = mgr.runtime_status(name)
            except Exception:
                runtime_status_before = {}
            runtime_version_before = str(runtime_status_before.get("version") or "").strip()
            source_version = str(update_result.version or "").strip()
            runtime_result: dict[str, Any] | None = None
            try:
                runtime_result = mgr.runtime_update(name, space="workspace")
            except Exception:
                _log.exception("runtime_update failed after infrastate skill update: %s", name)
            should_prepare = bool(source_version and source_version != runtime_version_before)
            if isinstance(runtime_result, dict) and not bool(runtime_result.get("ok", True)):
                should_prepare = True
            slot = ""
            prepared_slot = ""
            if should_prepare:
                prep = mgr.prepare_runtime(name, run_tests=False)
                prepared_slot = str(getattr(prep, "slot", None) or "")
                slot = str(
                    mgr.activate_for_space(
                        name,
                        version=getattr(prep, "version", None),
                        slot=getattr(prep, "slot", None),
                        space="default",
                        webspace_id=webspace_id,
                    )
                    or ""
                )
            result = {
                "ok": True,
                "action": action_id,
                "name": name,
                "updated": bool(update_result.updated),
                "version": update_result.version,
                "runtime": runtime_result,
                "slot": slot,
                "prepared": prepared_slot,
                "webspace_id": webspace_id,
            }
        _write_ui_state(
            last_action=action_id,
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_result=result,
            last_error="",
        )
        return result
    if action_id == "scenario_uninstall":
        name = str(_extract_param(payload, "name") or "").strip()
        if not name:
            raise ValueError("scenario action requires scenario name")
        ctx = get_ctx()
        mgr = ScenarioManager(
            repo=ctx.scenarios_repo,
            registry=SqliteScenarioRegistry(ctx.sql),
            git=ctx.git,
            paths=ctx.paths,
            bus=getattr(ctx, "bus", None),
            caps=ctx.caps,
        )
        mgr.uninstall(name)
        result = {
            "ok": True,
            "action": action_id,
            "name": name,
        }
        _write_ui_state(
            last_action=action_id,
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_result=result,
            last_error="",
        )
        return result
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
    if action_id == "forget_subnet":
        if str(getattr(conf, "role", "") or "").strip().lower() != "hub":
            raise ValueError("forget subnet is available only on hub")
        result = _forget_subnet_local()
        _write_ui_state(
            selected_node_id=str(getattr(conf, "node_id", "") or ""),
            last_action="forget_subnet",
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
    if action_id == "marketplace_install":
        value = payload.get("value") if isinstance(payload, dict) else {}
        value_map = value if isinstance(value, dict) else {}
        target_kind = str(value_map.get("kind") or value_map.get("target_kind") or "").strip().lower()
        target_id = str(value_map.get("id") or value_map.get("target_id") or "").strip()
        webspace_id = str(value_map.get("webspace_id") or payload.get("webspace_id") or default_webspace_id()).strip() or default_webspace_id()
        if target_kind not in {"skill", "scenario"} or not target_id:
            raise ValueError("marketplace install requires target kind and id")
        result = submit_install_operation(
            target_kind=target_kind,
            target_id=target_id,
            webspace_id=webspace_id,
            initiator={"kind": "ui", "id": "infrastate"},
        )
        _write_ui_state(
            last_action=action_id,
            last_action_ts=time.time(),
            last_refresh_ts=time.time(),
            last_result=result,
            last_error="",
        )
        return {
            "ok": True,
            "accepted": True,
            "operation_id": result.get("operation_id"),
            "operation": result,
        }
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
    elif action_id in {"defer_update_5m", "defer_update_15m"}:
        delay_sec = 300 if action_id == "defer_update_5m" else 900
        result = _post_local_admin(
            conf,
            "/api/admin/update/defer",
            {"reason": f"infrastate.{action_id}", "delay_sec": delay_sec},
        )
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
    elif action_id == "yjs_restore":
        selected_webspace = _selected_yjs_webspace_id(_ui_state(), _reliability_snapshot(conf, runtime_lifecycle_snapshot()))
        result = _post_local_admin(
            conf,
            f"/api/node/yjs/webspaces/{selected_webspace}/restore",
            {},
        )
    elif action_id == "yjs_reset":
        selected_webspace = _selected_yjs_webspace_id(_ui_state(), _reliability_snapshot(conf, runtime_lifecycle_snapshot()))
        result = _post_local_admin(
            conf,
            f"/api/node/yjs/webspaces/{selected_webspace}/reset",
            {},
        )
    elif action_id == "yjs_go_home":
        selected_webspace = _selected_yjs_webspace_id(_ui_state(), _reliability_snapshot(conf, runtime_lifecycle_snapshot()))
        result = _post_local_admin(
            conf,
            f"/api/node/yjs/webspaces/{selected_webspace}/go-home",
            {},
        )
    elif action_id == "yjs_set_home_current":
        selected_webspace = _selected_yjs_webspace_id(_ui_state(), _reliability_snapshot(conf, runtime_lifecycle_snapshot()))
        result = _post_local_admin(
            conf,
            f"/api/node/yjs/webspaces/{selected_webspace}/set-home-current",
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


def _snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    _ensure_skill_data_projections()
    conf = load_config()
    status = _safe_snapshot_step("read_core_update_status", read_core_update_status, {}) or {}
    last_result = _safe_snapshot_step("read_core_update_last_result", read_core_update_last_result, {}) or {}
    slots_payload = _safe_snapshot_step("slot_status", slot_status, {}) or {}
    lifecycle = _safe_snapshot_step("runtime_lifecycle_snapshot", runtime_lifecycle_snapshot, {}) or {}
    build = _safe_snapshot_step("build_meta", _build_meta, {}) or {}
    slots_payload, build = _safe_snapshot_step(
        "effective_runtime_projection",
        lambda: _effective_runtime_projection(status, last_result, slots_payload, build),
        (slots_payload, build),
    )
    ui_state = _safe_snapshot_step("ui_state", _ui_state, {}) or {}
    reliability = _safe_snapshot_step(
        "reliability_snapshot",
        lambda: _reliability_snapshot(conf, lifecycle),
        {"runtime": {}},
    )
    node_tabs, selected_node = _safe_snapshot_step(
        "node_tabs",
        lambda: _node_tabs(conf, ui_state, reliability),
        (
            [
                {
                    "id": str(getattr(conf, "node_id", "") or "local"),
                    "label": "hub" if str(getattr(conf, "role", "") or "").strip().lower() == "hub" else "member",
                    "title": "Local node",
                    "role": str(getattr(conf, "role", "") or ""),
                    "node_id": str(getattr(conf, "node_id", "") or ""),
                    "node_names": list(getattr(conf, "node_names", []) or []),
                    "kind": "local",
                    "selected": True,
                }
            ],
            {
                "id": str(getattr(conf, "node_id", "") or "local"),
                "label": "hub" if str(getattr(conf, "role", "") or "").strip().lower() == "hub" else "member",
                "title": "Local node",
                "role": str(getattr(conf, "role", "") or ""),
                "node_id": str(getattr(conf, "node_id", "") or ""),
                "node_names": list(getattr(conf, "node_names", []) or []),
                "kind": "local",
            },
        ),
    )
    yjs_webspace_tabs = _safe_snapshot_step(
        "yjs_webspace_tabs",
        lambda: _yjs_webspace_tabs(conf, ui_state, reliability, selected_node),
        [],
    )
    node_editor = _safe_snapshot_step(
        "selected_node_editor",
        lambda: _selected_node_editor(conf, selected_node),
        {"names_csv": "", "editable": False, "scope": "fallback"},
    )
    selected_projection = _safe_snapshot_step(
        "selected_node_projection",
        lambda: _selected_node_projection(
            selected_node,
            reliability=reliability,
            status=status,
            last_result=last_result,
            slots_payload=slots_payload,
            lifecycle=lifecycle,
            build=build,
        ),
        {
            "status": status,
            "last_result": last_result,
            "slots_payload": slots_payload,
            "lifecycle": lifecycle,
            "build": build,
            "selected_member": {},
        },
    )
    display_status = selected_projection["status"] if isinstance(selected_projection.get("status"), dict) else status
    display_last_result = selected_projection["last_result"] if isinstance(selected_projection.get("last_result"), dict) else last_result
    display_slots_payload = selected_projection["slots_payload"] if isinstance(selected_projection.get("slots_payload"), dict) else slots_payload
    display_lifecycle = selected_projection["lifecycle"] if isinstance(selected_projection.get("lifecycle"), dict) else lifecycle
    display_build = selected_projection["build"] if isinstance(selected_projection.get("build"), dict) else build
    selected_member = selected_projection["selected_member"] if isinstance(selected_projection.get("selected_member"), dict) else {}
    transport_diag = _safe_snapshot_step("transport_diag_snapshot", _transport_diag_snapshot, {}) or {}
    report = _safe_snapshot_step(
        "core_update_report",
        lambda: _read_json(_base_dir() / "state" / "core_update" / "status.json") or {},
        {},
    )
    effective_report = _effective_update_log_report(report, display_last_result)
    operations = _safe_snapshot_step(
        "operations_snapshot",
        lambda: _operations_snapshot(webspace_id=webspace_id),
        {"active_items": [], "active": []},
    )
    try:
        build_items = _build_items(display_build)
    except Exception:
        build_items = []
    try:
        step_items = _step_items(display_status, display_slots_payload, display_lifecycle, display_build)
    except Exception:
        step_items = []
    try:
        realtime_items = _realtime_items(reliability, transport_diag)
    except Exception:
        realtime_items = []
    try:
        slot_items = _slot_items(display_slots_payload)
    except Exception:
        slot_items = []
    try:
        skills_items = _skills_items()
    except Exception:
        skills_items = []
    try:
        scenario_items = _scenario_items()
    except Exception:
        scenario_items = []
    try:
        marketplace = _marketplace_items(webspace_id=webspace_id)
    except Exception:
        marketplace = {"skills": [], "scenarios": []}
    selected_is_local = not selected_member or str(selected_member.get("kind") or "local").strip().lower() == "local"
    core_update_diagnostics = _safe_snapshot_step(
        "core_update_diagnostics",
        lambda: _core_update_diagnostic_items(
            display_status,
            display_last_result,
            display_slots_payload,
            local_node=selected_is_local,
        ),
        [],
    )
    core_update_diag_actions = _safe_snapshot_step(
        "core_update_diag_actions",
        lambda: _core_update_diagnostic_actions(core_update_diagnostics),
        [],
    )
    snapshot = {
        "summary": _safe_snapshot_step(
            "summary",
            lambda: _summary(display_status, display_last_result, display_slots_payload, display_lifecycle, conf, display_build, ui_state, reliability, transport_diag, selected_member=selected_member),
            {
                "label": "Infra State",
                "value": str(display_status.get("state") or display_lifecycle.get("node_state") or "ready"),
                "subtitle": f"webspace {str(webspace_id or default_webspace_id()).strip() or default_webspace_id()}",
                "description": "Local snapshot is available with degraded metadata.",
                "updated_at": time.time(),
            },
        ),
        "actions": _safe_snapshot_step("action_items", lambda: _action_items(display_status, ui_state, reliability), []),
        "core_actions": _safe_snapshot_step("core_action_items", lambda: _core_action_items(display_status, ui_state, reliability), []),
        "yjs_actions": _safe_snapshot_step("yjs_action_items", lambda: _yjs_action_items(display_status, ui_state, reliability), []),
        "update_actions": _safe_snapshot_step("update_actions", lambda: _update_actions(conf, ui_state, reliability), []),
        "nodes": node_tabs,
        "yjs_webspaces": yjs_webspace_tabs,
        "node_editor": node_editor,
        "build": build_items,
        "steps": step_items,
        "realtime": realtime_items,
        "slots": slot_items,
        "skills": skills_items,
        "scenarios": scenario_items,
        "operations": {
            "items": operations.get("active_items") or [],
            "active": operations.get("active") or [],
        },
        "marketplace": marketplace,
        "core_update_diagnostics": core_update_diagnostics,
        "core_update_diag_actions": core_update_diag_actions,
        "logs": _safe_snapshot_step("status_log_items", lambda: _status_log_items(effective_report), []),
        "events": _safe_snapshot_step("event_state", lambda: list(reversed(_event_state())), []),
        "status": display_status,
        "last_result": display_last_result,
        "skill_runtime_migration": _skill_runtime_migration_report(display_status, display_last_result),
        "skill_runtime_rollback": _skill_runtime_rollback_report(display_status, display_last_result),
        "skill_post_commit_checks": _skill_post_commit_checks_report(display_status, display_last_result),
        "lifecycle": display_lifecycle,
        "reliability": reliability,
        "transport_diag": transport_diag,
        "projection_diag": _projection_diag_snapshot(),
        "build_meta": display_build,
        "ui_state": ui_state,
        "slots_meta": display_slots_payload,
        "last_refresh_ts": time.time(),
    }
    return snapshot


def _fallback_snapshot(exc: Exception, *, webspace_id: str | None = None) -> dict[str, Any]:
    error_text = f"{type(exc).__name__}: {exc}"
    try:
        lifecycle = runtime_lifecycle_snapshot()
    except Exception:
        lifecycle = {}
    try:
        conf = load_config()
        reliability = _reliability_snapshot(conf, lifecycle if isinstance(lifecycle, dict) else {})
    except Exception:
        reliability = {}
    runtime = reliability.get("runtime") if isinstance(reliability.get("runtime"), dict) else {}
    sync_runtime = runtime.get("sync_runtime") if isinstance(runtime.get("sync_runtime"), dict) else {}
    selected_ws = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    yjs_runtime = dict(sync_runtime) if isinstance(sync_runtime, dict) else {}
    yjs_runtime.setdefault("available", bool(sync_runtime))
    yjs_runtime.setdefault("selected_webspace_id", selected_ws)
    yjs_runtime.setdefault("assessment", {"state": "degraded", "reason": "fallback_snapshot"})
    return {
        "summary": {
            "label": "Infra State",
            "value": str((lifecycle if isinstance(lifecycle, dict) else {}).get("node_state") or "ready"),
            "subtitle": f"webspace {selected_ws}",
            "description": f"fallback snapshot: {error_text}",
            "updated_at": time.time(),
        },
        "actions": [],
        "core_actions": [],
        "yjs_actions": [],
        "update_actions": [],
        "nodes": [],
        "yjs_webspaces": [],
        "node_editor": {
            "names_csv": "",
            "editable": False,
            "scope": "fallback",
        },
        "build": [],
        "steps": [
            {
                "id": "lifecycle",
                "title": "Lifecycle",
                "status": str((lifecycle if isinstance(lifecycle, dict) else {}).get("node_state") or "ready"),
                "description": "runtime fallback snapshot",
            },
            {
                "id": "yjs_runtime",
                "title": "Yjs runtime",
                "status": str((yjs_runtime.get("assessment") if isinstance(yjs_runtime.get("assessment"), dict) else {}).get("state") or "degraded"),
                "description": str((yjs_runtime.get("assessment") if isinstance(yjs_runtime.get("assessment"), dict) else {}).get("reason") or "fallback snapshot"),
            },
        ],
        "realtime": [],
        "slots": [],
        "skills": [],
        "logs": [
            {
                "id": "snapshot-error",
                "title": "snapshot-error",
                "status": "warn",
                "preview": error_text,
                "content": error_text,
            }
        ],
        "operations": {"items": [], "active": []},
        "marketplace": {"skills": [], "scenarios": []},
        "events": list(reversed(_event_state())),
        "lifecycle": lifecycle if isinstance(lifecycle, dict) else {},
        "reliability": reliability,
        "yjs_runtime": yjs_runtime,
        "projection_diag": _projection_diag_snapshot(),
        "last_refresh_ts": time.time(),
        "fallback": True,
        "errors": [error_text],
    }


def _safe_snapshot_step(label: str, fn, default: Any) -> Any:
    try:
        return fn()
    except Exception:
        _log.debug("infrastate snapshot step failed step=%s", label, exc_info=True)
        return default


def _snapshot_or_fallback(*, webspace_id: str | None = None) -> dict[str, Any]:
    try:
        return _snapshot(webspace_id=webspace_id)
    except Exception as exc:
        _log.warning("infrastate snapshot failed; projecting fallback snapshot", exc_info=True)
        return _fallback_snapshot(exc, webspace_id=webspace_id)


def _snapshot_or_fallback_cached(*, webspace_id: str | None = None, allow_cache: bool = True) -> dict[str, Any]:
    cache_key = _snapshot_cache_key(webspace_id)
    if allow_cache and _SNAPSHOT_CACHE_TTL_S > 0:
        # Coalesce concurrent initial stream subscriptions for the same
        # webspace; otherwise every receiver can rebuild the same snapshot.
        with _snapshot_cache_lock_for(cache_key):
            now = time.monotonic()
            cached = _snapshot_cache.get(cache_key)
            if cached is not None:
                cached_at, cached_snapshot = cached
                if now - cached_at <= _SNAPSHOT_CACHE_TTL_S:
                    _projection_diag["cache_hit_total"] = int(_projection_diag.get("cache_hit_total") or 0) + 1
                    return _cache_copy(cached_snapshot)
            snapshot = _snapshot_or_fallback(webspace_id=webspace_id)
            _snapshot_cache[cache_key] = (time.monotonic(), _cache_copy(snapshot))
            return snapshot
    snapshot = _snapshot_or_fallback(webspace_id=webspace_id)
    with _snapshot_cache_lock_for(cache_key):
        _snapshot_cache[cache_key] = (time.monotonic(), _cache_copy(snapshot))
    return snapshot


def _projection_webspace_ids(webspace_id: str | None = None) -> list[str]:
    token = str(webspace_id or "").strip() or default_webspace_id()
    return [token]


async def _project_async(snapshot: dict[str, Any], webspace_id: str | None = None) -> None:
    compact = _compact_snapshot_for_yjs(snapshot)
    fingerprint = _snapshot_projection_fingerprint(compact)
    now = time.monotonic()
    for target_ws in _projection_webspace_ids(webspace_id):
        if _projection_fingerprints.get(target_ws) == fingerprint:
            _projection_diag["skip_total"] = int(_projection_diag.get("skip_total") or 0) + 1
            continue
        last_applied_at = float(_projection_last_applied_at.get(target_ws) or 0.0)
        if last_applied_at > 0 and now - last_applied_at < _MIN_YJS_PROJECTION_INTERVAL_S:
            _projection_diag["rate_limited_total"] = int(_projection_diag.get("rate_limited_total") or 0) + 1
            continue
        await ctx_subnet.set_async("infrastate.snapshot", compact, webspace_id=target_ws)
        _projection_fingerprints[target_ws] = fingerprint
        _projection_last_applied_at[target_ws] = now
        _projection_diag["apply_total"] = int(_projection_diag.get("apply_total") or 0) + 1
    _publish_snapshot_streams(snapshot, webspace_id=webspace_id)


def _project(snapshot: dict[str, Any], webspace_id: str | None = None) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_project_async(snapshot, webspace_id=webspace_id))
        return
    loop.create_task(_project_async(snapshot, webspace_id=webspace_id))


async def _refresh_snapshot_async(*, webspace_id: str | None = None, allow_cache: bool = True) -> dict[str, Any]:
    _write_ui_state(last_refresh_ts=time.time())
    snapshot = await asyncio.to_thread(
        _snapshot_or_fallback_cached,
        webspace_id=webspace_id,
        allow_cache=allow_cache,
    )
    await _project_async(snapshot, webspace_id=webspace_id)
    return {"ok": True, **snapshot}


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


@subscribe("webio.stream.snapshot.requested")
def on_webio_stream_snapshot_requested(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, dict):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if receiver not in {
        _operations_receiver(),
        _logs_receiver(),
        _events_receiver(),
        _yjs_load_receiver(),
        _build_receiver(),
        _steps_receiver(),
        _realtime_receiver(),
        _slots_receiver(),
        _skills_receiver(),
        _scenarios_receiver(),
        _marketplace_skills_receiver(),
        _marketplace_scenarios_receiver(),
        _core_update_diagnostics_receiver(),
    }:
        return
    webspace_id = _webspace_id_from_payload(payload)
    _remember_stream_receiver(webspace_id, receiver)
    # Initial stream subscriptions arrive as a burst; share one fresh-enough
    # snapshot so each receiver does not rebuild the same heavy state.
    snapshot = _snapshot_or_fallback_cached(webspace_id=webspace_id, allow_cache=True)
    _publish_stream_payload(
        receiver=receiver,
        data=_stream_payload_for_receiver(snapshot, receiver),
        webspace_id=webspace_id,
        force=True,
    )


@subscribe("webio.stream.subscription.changed")
def on_webio_stream_subscription_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, dict):
        return
    receiver = str(payload.get("receiver") or "").strip()
    if not receiver.startswith("infrastate."):
        return
    webspace_id = _webspace_id_from_payload(payload)
    action = str(payload.get("action") or "").strip().lower() or "subscribed"
    if action == "unsubscribed":
        _forget_stream_receiver(webspace_id, receiver)
    else:
        _remember_stream_receiver(webspace_id, receiver)


@tool("get_snapshot")
def get_snapshot(webspace_id: str | None = None, project: bool = False) -> dict[str, Any]:
    snapshot = _snapshot_or_fallback_cached(webspace_id=webspace_id, allow_cache=True)
    if project:
        _project(snapshot, webspace_id=webspace_id)
    return _compact_snapshot_for_client(snapshot)


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_refresh_snapshot_async(webspace_id=webspace_id, allow_cache=False))
    _write_ui_state(last_refresh_ts=time.time())
    snapshot = _snapshot_or_fallback_cached(webspace_id=webspace_id, allow_cache=False)
    _project(snapshot, webspace_id=webspace_id)
    return {"ok": True, **snapshot}


@subscribe("infrastate.refresh")
def on_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason="infrastate.refresh",
    )


@subscribe("operations.")
def on_operations_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    _invalidate_runtime_caches(webspace_id=_webspace_id_from_payload(payload))
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason="operations.changed",
    )


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
    _schedule_snapshot_refresh(
        webspace_id=webspace_id,
        reason=f"infrastate.action:{action_id or '-'}",
    )


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
    _invalidate_runtime_caches(
        webspace_id=_webspace_id_from_payload(payload),
        marketplace=True,
    )
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason="skills.activated",
    )


@subscribe("skill.installed")
@subscribe("skill.uninstalled")
@subscribe("scenario.installed")
@subscribe("scenario.removed")
@subscribe("scenarios.synced")
@subscribe("skills.rolledback")
@subscribe("skills.updated")
def on_registry_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "skills.updated")
    _invalidate_runtime_caches(
        webspace_id=_webspace_id_from_payload(payload),
        marketplace=True,
    )
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason=event_type,
    )


@subscribe("device.registered")
@subscribe("browser.session.changed")
@subscribe("webrtc.peer.state.changed")
def on_browser_runtime_changed(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "browser.session.changed")
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason=event_type,
    )


@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
@subscribe("desktop.webspace.reloaded")
@subscribe("desktop.webspace.reset")
@subscribe("desktop.webspace.restored")
@subscribe("desktop.webspace.go_home")
@subscribe("desktop.webspace.set_home")
@subscribe("desktop.webspace.set_home_current")
@subscribe("desktop.webspace.create")
@subscribe("desktop.webspace.rename")
@subscribe("desktop.webspace.update")
@subscribe("desktop.webspace.delete")
@subscribe("desktop.scenario.set")
@subscribe("node.yjs.control.completed")
@subscribe("node.yjs.control.failed")
def on_webspace_reload(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "desktop.webspace.reload")
    _schedule_snapshot_refresh(
        webspace_id=_webspace_id_from_payload(payload),
        reason=event_type,
    )


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
async def on_runtime_event(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    try:
        event_type = str(getattr(evt, "type", "") or (payload.get("type") if isinstance(payload, dict) else "") or "runtime.event")
        if event_type == "sys.ready":
            return
        _invalidate_runtime_caches(webspace_id=_webspace_id_from_payload(payload))
        _append_event(event_type, payload)
        _schedule_snapshot_refresh(
            webspace_id=_webspace_id_from_payload(payload),
            reason=event_type,
        )
    except Exception:
        _log.debug("failed to refresh infrastate snapshot from runtime event", exc_info=True)
