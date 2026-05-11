from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

from adaos.sdk.core.decorators import tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.control_plane import get_reliability_projection, get_self_object
from adaos.services import node_config as _node_config
from adaos.services.agent_context import get_ctx

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"}

_ENV_META: dict[str, dict[str, Any]] = {
    "ENV_TYPE": {
        "title": "ENV_TYPE",
        "group": "general",
        "kind": "enum",
        "description": "Runtime environment profile for this node.",
        "clearable": True,
        "restart": "recommended",
    },
    "GIT_USER": {
        "title": "GIT_USER",
        "group": "git",
        "kind": "text",
        "description": "Git author/identity name used by local workflows.",
        "clearable": True,
        "restart": "none",
    },
    "GIT_EMAIL": {
        "title": "GIT_EMAIL",
        "group": "git",
        "kind": "text",
        "description": "Git author email used by local workflows.",
        "clearable": True,
        "restart": "none",
    },
    "ADAOS_SUBNET_YJS_REPLICATION": {
        "title": "ADAOS_SUBNET_YJS_REPLICATION",
        "group": "subnet",
        "kind": "bool",
        "description": "Enable YJS replication for subnet-level data.",
        "clearable": False,
        "restart": "recommended",
    },
    "ADAOS_CLI_DEBUG": {
        "title": "ADAOS_CLI_DEBUG",
        "group": "diagnostics",
        "kind": "bool",
        "description": "Increase CLI-side debug noise.",
        "clearable": False,
        "restart": "maybe",
    },
    "ADAOS_LOG_LEVEL": {
        "title": "ADAOS_LOG_LEVEL",
        "group": "diagnostics",
        "kind": "log_level",
        "description": "Primary runtime log level.",
        "clearable": False,
        "restart": "recommended",
    },
    "ADAOS_SCENARIO_LOG_LEVEL": {
        "title": "ADAOS_SCENARIO_LOG_LEVEL",
        "group": "diagnostics",
        "kind": "log_level",
        "description": "Scenario runtime log level.",
        "clearable": False,
        "restart": "recommended",
    },
    "HUB_NATS_VERBOSE": {
        "title": "HUB_NATS_VERBOSE",
        "group": "diagnostics",
        "kind": "bool",
        "description": "Verbose NATS transport diagnostics.",
        "clearable": False,
        "restart": "maybe",
    },
    "HUB_NATS_TRACE": {
        "title": "HUB_NATS_TRACE",
        "group": "diagnostics",
        "kind": "bool",
        "description": "Trace NATS traffic at runtime.",
        "clearable": False,
        "restart": "maybe",
    },
    "HUB_NATS_TRACE_INPUT": {
        "title": "HUB_NATS_TRACE_INPUT",
        "group": "diagnostics",
        "kind": "bool",
        "description": "Trace inbound NATS payloads.",
        "clearable": False,
        "restart": "maybe",
    },
    "HUB_ROUTE_VERBOSE": {
        "title": "HUB_ROUTE_VERBOSE",
        "group": "diagnostics",
        "kind": "bool",
        "description": "Verbose routing diagnostics on hub side.",
        "clearable": False,
        "restart": "maybe",
    },
    "ROUTE_PROXY_VERBOSE": {
        "title": "ROUTE_PROXY_VERBOSE",
        "group": "diagnostics",
        "kind": "bool",
        "description": "Verbose route proxy diagnostics.",
        "clearable": False,
        "restart": "maybe",
    },
    "HUB_TG_DEBUG": {
        "title": "HUB_TG_DEBUG",
        "group": "diagnostics",
        "kind": "bool",
        "description": "Telegram integration debug mode.",
        "clearable": False,
        "restart": "maybe",
    },
}

_ALLOWED_ENV_KEYS = set(_ENV_META)
_DIAGNOSTIC_FLAG_KEYS = [key for key, meta in _ENV_META.items() if meta.get("group") == "diagnostics" and meta.get("kind") == "bool"]
_LOG_LEVEL_KEYS = [key for key, meta in _ENV_META.items() if meta.get("kind") == "log_level"]
_TEXT_KEYS = [key for key, meta in _ENV_META.items() if meta.get("kind") == "text"]
_RESTART_RECOMMENDED_KEYS = {key for key, meta in _ENV_META.items() if meta.get("restart") == "recommended"}

_ENV_TYPE_ACTIONS = {
    "env_type_default": ("ENV_TYPE", ""),
    "env_type_dev": ("ENV_TYPE", "dev"),
    "env_type_prod": ("ENV_TYPE", "prod"),
}

_SUBNET_ACTIONS = {
    "subnet_yjs_replication_on": ("ADAOS_SUBNET_YJS_REPLICATION", "1"),
    "subnet_yjs_replication_off": ("ADAOS_SUBNET_YJS_REPLICATION", "0"),
}
_DATA_PROJECTION_ENTRIES = [
    {
        "scope": "subnet",
        "slot": "subnet_env.snapshot",
        "targets": [
            {
                "backend": "yjs",
                "path": "data/subnet_env",
            },
        ],
    },
]

_LOG_LEVEL_ACTIONS = {
    "log_level_info": ("ADAOS_LOG_LEVEL", "INFO"),
    "log_level_debug": ("ADAOS_LOG_LEVEL", "DEBUG"),
    "scenario_log_level_info": ("ADAOS_SCENARIO_LOG_LEVEL", "INFO"),
    "scenario_log_level_debug": ("ADAOS_SCENARIO_LOG_LEVEL", "DEBUG"),
}


def lang_res() -> dict[str, str]:
    return {}


def _search_dotenv_in_parents(start: Path | None, *, name: str = ".env") -> Path | None:
    if start is None:
        return None
    try:
        current = start.resolve()
    except Exception:
        return None
    for candidate in [current, *current.parents]:
        path = candidate / name
        if path.exists():
            return path.resolve()
    return None


def _repo_root() -> Path | None:
    try:
        ctx = get_ctx()
        raw = getattr(ctx.paths, "repo_root", None)
        path = raw() if callable(raw) else raw
        if path:
            return Path(path).expanduser().resolve()
    except Exception:
        pass
    try:
        return Path(__file__).resolve().parents[5]
    except Exception:
        return None


def _dotenv_path() -> Path:
    explicit = str(os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    repo_root = _repo_root()
    if repo_root is not None:
        candidate = (repo_root / ".env").resolve()
        if candidate.exists():
            return candidate
    found = _search_dotenv_in_parents(Path.cwd())
    if found is not None:
        return found
    found = _search_dotenv_in_parents(Path(__file__).resolve().parent)
    if found is not None:
        return found
    if repo_root is not None:
        return (repo_root / ".env").resolve()
    return (Path.cwd() / ".env").resolve()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_dotenv(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    for raw_line in _read_text(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        token = str(key or "").strip()
        if not token:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        payload[token] = value
    return payload


def _meta(key: str) -> dict[str, Any]:
    return dict(_ENV_META.get(key) or {"title": key, "group": "general", "kind": "text", "description": "", "clearable": False, "restart": "none"})


def _normalize_env_value(key: str, value: Any) -> str:
    token = "" if value is None else str(value).strip()
    meta = _meta(key)
    kind = str(meta.get("kind") or "text")
    if kind == "enum":
        lowered = token.lower()
        if lowered not in {"", "dev", "prod"}:
            raise ValueError("allowed values: '', dev, prod")
        return lowered
    if kind == "bool":
        lowered = token.lower()
        if lowered in _BOOL_TRUE:
            return "1"
        if lowered in _BOOL_FALSE or not lowered:
            return "0"
        raise ValueError("expected boolean-like value")
    if kind == "log_level":
        upper = token.upper()
        if not upper:
            return "INFO"
        if upper not in _LOG_LEVELS:
            raise ValueError(f"expected one of: {', '.join(sorted(_LOG_LEVELS))}")
        return upper
    if "\n" in token or "\r" in token:
        raise ValueError("multiline values are not allowed")
    if key == "GIT_EMAIL" and token and "@" not in token:
        raise ValueError("email must contain '@'")
    return token


def _dotenv_line(key: str, value: str) -> str:
    escaped = value.replace('"', '\\"')
    if not value:
        return f"{key}=\n"
    if any(ch.isspace() for ch in value) or "#" in value:
        return f'{key}="{escaped}"\n'
    return f"{key}={value}\n"


def _write_dotenv_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_text(path)
    lines = existing.splitlines(keepends=True) if existing else []
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    replacement = _dotenv_line(key, value)
    updated = False
    next_lines: list[str] = []
    for line in lines:
        if pattern.match(line):
            next_lines.append(replacement)
            updated = True
        else:
            next_lines.append(line)
    if not updated:
        if next_lines and not str(next_lines[-1]).endswith("\n"):
            next_lines[-1] = f"{next_lines[-1]}\n"
        next_lines.append(replacement)
    path.write_text("".join(next_lines), encoding="utf-8")


def _delete_dotenv_value(path: Path, key: str) -> None:
    existing = _read_text(path)
    if not existing:
        return
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    lines = existing.splitlines(keepends=True)
    next_lines = [line for line in lines if not pattern.match(line)]
    path.write_text("".join(next_lines), encoding="utf-8")


def _persist_env_value(path: Path, key: str, value: str) -> None:
    if not value and _meta(key).get("clearable"):
        _delete_dotenv_value(path, key)
        return
    _write_dotenv_value(path, key, value)


def _env_file_value(file_env: dict[str, str], key: str) -> str:
    return str(file_env.get(key) or "").strip()


def _effective_env_value(file_env: dict[str, str], key: str) -> str:
    raw = os.getenv(key)
    if raw is not None:
        return str(raw).strip()
    return _env_file_value(file_env, key)


def _env_flag_enabled(file_env: dict[str, str], key: str) -> bool:
    return _effective_env_value(file_env, key).lower() in _BOOL_TRUE


def _mask_empty(value: str, *, fallback: str = "<unset>") -> str:
    return value if str(value or "").strip() else fallback


def _effective_source(file_env: dict[str, str], key: str) -> str:
    if os.getenv(key) is not None:
        return "process env"
    if key in file_env:
        return ".env"
    return "default"


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        existing = ctx.projections.resolve("subnet", "subnet_env.snapshot")
        if existing:
            return
        entries: list[dict[str, Any]] = []
        manifest_path = Path(__file__).resolve().parents[1] / "skill.yaml"
        if manifest_path.exists():
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            raw_entries = payload.get("data_projections") or []
            if isinstance(raw_entries, list):
                entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        if not entries:
            entries = list(_DATA_PROJECTION_ENTRIES)
        if isinstance(entries, list) and entries:
            ctx.projections.load_entries(entries)
    except Exception:
        pass


def _node_payload() -> dict[str, Any]:
    conf = _node_config.load_config()
    self_obj = get_self_object()
    runtime = self_obj.get("runtime") if isinstance(self_obj.get("runtime"), dict) else {}
    return {
        "node_id": str(getattr(conf, "node_id", "") or self_obj.get("id") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "role": str(getattr(conf, "role", "") or ""),
        "zone_id": str(getattr(conf, "zone_id", "") or ""),
        "node_names": list(getattr(conf, "node_names", []) or []),
        "primary_node_name": str(getattr(conf, "primary_node_name", "") or ""),
        "runtime": dict(runtime) if isinstance(runtime, dict) else {},
    }


def _env_rows(file_env: dict[str, str], *, view: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(_ALLOWED_ENV_KEYS):
        meta = _meta(key)
        file_value = _env_file_value(file_env, key)
        effective_value = _effective_env_value(file_env, key)
        if view == "file":
            shown = file_value
            subtitle = f"{meta['group']} | persisted"
        else:
            shown = effective_value
            subtitle = f"{meta['group']} | source={_effective_source(file_env, key)}"
            if file_value != effective_value and file_value:
                subtitle += f" | file={file_value}"
        rows.append(
            {
                "id": f"{view}:{key}",
                "title": meta["title"],
                "description": _mask_empty(shown),
                "subtitle": subtitle,
            }
        )
    rows.append(
        {
            "id": f"{view}:ADAOS_ZONE_ID",
            "title": "ADAOS_ZONE_ID",
            "description": _mask_empty(_effective_env_value(file_env, "ADAOS_ZONE_ID") if view == "effective" else _env_file_value(file_env, "ADAOS_ZONE_ID")),
            "subtitle": "identity | read-only",
        }
    )
    return rows


def _diagnostic_rows(file_env: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in _DIAGNOSTIC_FLAG_KEYS + _LOG_LEVEL_KEYS:
        meta = _meta(key)
        effective = _effective_env_value(file_env, key)
        rows.append(
            {
                "id": f"diag:{key}",
                "title": key,
                "description": _mask_empty(effective, fallback="0" if meta.get("kind") == "bool" else "INFO"),
                "subtitle": meta.get("description") or "",
            }
        )
    return rows


def _drift_rows(file_env: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(_ALLOWED_ENV_KEYS | {"ADAOS_ZONE_ID"}):
        file_value = _env_file_value(file_env, key)
        effective_value = _effective_env_value(file_env, key)
        if file_value == effective_value or (not file_value and not os.getenv(key)):
            continue
        rows.append(
            {
                "id": f"drift:{key}",
                "title": key,
                "description": f"effective={_mask_empty(effective_value)} | file={_mask_empty(file_value)}",
                "subtitle": "process env overrides persisted value",
            }
        )
    return rows


def _overview_items(file_env: dict[str, str], node: dict[str, Any], reliability: dict[str, Any], dotenv_path: Path, drift_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runtime = node.get("runtime") if isinstance(node.get("runtime"), dict) else {}
    route_mode = str(runtime.get("route_mode") or "").strip()
    connected = runtime.get("connected_to_hub")
    projection = reliability.get("subject") if isinstance(reliability.get("subject"), dict) else {}
    health = projection.get("health") if isinstance(projection.get("health"), dict) else {}
    return [
        {"id": "env-file", "title": "dotenv source", "description": str(dotenv_path), "subtitle": "shared runtime env file"},
        {"id": "env-type", "title": "ENV_TYPE", "description": _mask_empty(_effective_env_value(file_env, "ENV_TYPE"), fallback="<default>"), "subtitle": f"source={_effective_source(file_env, 'ENV_TYPE')}"},
        {"id": "zone-id", "title": "zone_id", "description": node.get("zone_id") or _effective_env_value(file_env, "ADAOS_ZONE_ID") or "<unset>", "subtitle": "node config or ADAOS_ZONE_ID"},
        {
            "id": "node",
            "title": "node",
            "description": f"{node.get('primary_node_name') or node.get('node_id') or '-'} | role={node.get('role') or '-'}",
            "subtitle": f"subnet={node.get('subnet_id') or '-'}",
        },
        {
            "id": "routing",
            "title": "routing",
            "description": f"route_mode={route_mode or '-'} | connected_to_hub={connected}",
            "subtitle": "runtime projection",
        },
        {
            "id": "reliability",
            "title": "reliability",
            "description": f"status={projection.get('status') or '-'} | connectivity={health.get('connectivity') or '-'}",
            "subtitle": f"runtime_freshness={health.get('runtime_freshness') or '-'}",
        },
        {
            "id": "git",
            "title": "git identity",
            "description": f"{_mask_empty(_effective_env_value(file_env, 'GIT_USER'))} <{_mask_empty(_effective_env_value(file_env, 'GIT_EMAIL'))}>",
            "subtitle": "local author identity",
        },
        {
            "id": "subnet-sync",
            "title": "subnet yjs replication",
            "description": "enabled" if _env_flag_enabled(file_env, "ADAOS_SUBNET_YJS_REPLICATION") else "disabled",
            "subtitle": "runtime flag",
        },
        {
            "id": "drift",
            "title": "effective vs file drift",
            "description": str(len(drift_rows)),
            "subtitle": "keys where process env differs from persisted .env",
        },
    ]


def _notice_items(file_env: dict[str, str], drift_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    if drift_rows:
        notices.append(
            {
                "id": "notice:drift",
                "title": "Effective environment differs from .env",
                "description": f"{len(drift_rows)} key(s) are overridden in the current process environment.",
                "subtitle": "The modal shows both views so operators can see drift explicitly.",
            }
        )
    restart_keys = [
        key
        for key in sorted(_RESTART_RECOMMENDED_KEYS)
        if _env_file_value(file_env, key) or os.getenv(key) is not None
    ]
    notices.append(
        {
            "id": "notice:restart",
            "title": "Some changes may need restart",
            "description": "ENV_TYPE, subnet replication and log-level updates can require runtime/service restart.",
            "subtitle": ", ".join(restart_keys) if restart_keys else "No restart-sensitive keys are currently set.",
        }
    )
    notices.append(
        {
            "id": "notice:safety",
            "title": "Secrets are intentionally excluded",
            "description": "subnet_env edits only a small allowlist and does not expose tokens or certificates.",
            "subtitle": "This keeps the SDK-facing skill surface safe for LLM-authored workflows.",
        }
    )
    return notices


def _action_groups(file_env: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    env_type = _effective_env_value(file_env, "ENV_TYPE")
    env_actions = [
        {"id": "env_type_default", "label": "Use default" if not env_type else "Clear ENV_TYPE"},
        {"id": "env_type_dev", "label": "Set dev"},
        {"id": "env_type_prod", "label": "Set prod"},
    ]
    subnet_actions = [
        {"id": "subnet_yjs_replication_on", "label": "Replication on"},
        {"id": "subnet_yjs_replication_off", "label": "Replication off"},
    ]
    diag_actions: list[dict[str, Any]] = []
    for key in _DIAGNOSTIC_FLAG_KEYS:
        enabled = _env_flag_enabled(file_env, key)
        diag_actions.append({"id": f"toggle::{key}", "label": f"{key}: {'on' if enabled else 'off'}"})
    for action_id, (key, value) in _LOG_LEVEL_ACTIONS.items():
        current = _effective_env_value(file_env, key).upper() or "INFO"
        diag_actions.append({"id": action_id, "label": f"{key}: {current} -> {value}"})
    return {
        "env_type": env_actions,
        "subnet": subnet_actions,
        "diagnostics": diag_actions,
        "general": [{"id": "refresh_snapshot", "label": "Refresh snapshot"}],
    }


def _field_forms(file_env: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {
        "git_user": {
            "value": _effective_env_value(file_env, "GIT_USER"),
            "placeholder": "AdaOS operator",
            "hint": "Blank value clears persisted GIT_USER.",
        },
        "git_email": {
            "value": _effective_env_value(file_env, "GIT_EMAIL"),
            "placeholder": "operator@example.com",
            "hint": "Blank value clears persisted GIT_EMAIL.",
        },
    }


def _build_snapshot() -> dict[str, Any]:
    _ensure_skill_data_projections()
    dotenv_path = _dotenv_path()
    file_env = _read_dotenv(dotenv_path)
    node = _node_payload()
    reliability = get_reliability_projection()
    refresh_ts = time.time()
    env_type = _effective_env_value(file_env, "ENV_TYPE") or "default"
    zone_id = node.get("zone_id") or _effective_env_value(file_env, "ADAOS_ZONE_ID") or "unset"
    yjs_enabled = _env_flag_enabled(file_env, "ADAOS_SUBNET_YJS_REPLICATION")
    drift_rows = _drift_rows(file_env)

    snapshot = {
        "summary": {
            "value": env_type,
            "label": f"zone={zone_id} | yjs={'on' if yjs_enabled else 'off'}",
            "subtitle": f"node={node.get('primary_node_name') or node.get('node_id') or '-'}",
        },
        "overview": _overview_items(file_env, node, reliability, dotenv_path, drift_rows),
        "notices": _notice_items(file_env, drift_rows),
        "forms": _field_forms(file_env),
        "actions": _action_groups(file_env),
        "effective_env": _env_rows(file_env, view="effective"),
        "persisted_env": _env_rows(file_env, view="file"),
        "diagnostics": _diagnostic_rows(file_env),
        "drift": drift_rows,
        "env": {
            "dotenv_path": str(dotenv_path),
            "exists": dotenv_path.exists(),
            "effective": {key: _effective_env_value(file_env, key) for key in sorted(_ALLOWED_ENV_KEYS | {"ADAOS_ZONE_ID"})},
            "file": {key: _env_file_value(file_env, key) for key in sorted(_ALLOWED_ENV_KEYS | {"ADAOS_ZONE_ID"})},
        },
        "node": node,
        "state": {
            "zone_id": zone_id,
            "env_type": env_type,
            "drift_count": len(drift_rows),
            "restart_recommended": any(_effective_env_value(file_env, key) for key in _RESTART_RECOMMENDED_KEYS),
        },
        "last_refresh_ts": refresh_ts,
        "safety": {
            "editable_keys": sorted(_ALLOWED_ENV_KEYS),
            "read_only_notes": [
                "ADAOS_ZONE_ID is visible but read-only in subnet_env MVP.",
                "Secrets, tokens, certificates and transport bootstrap parameters stay outside this skill.",
            ],
        },
    }
    return snapshot


def _projection_webspace_id(webspace_id: str | None = None) -> str:
    token = str(
        webspace_id
        or os.environ.get("ADAOS_WEBSPACE_ID")
        or os.environ.get("ADAOS_CURRENT_WEBSPACE_ID")
        or "desktop"
    ).strip()
    return token or "desktop"


def _project_snapshot(snapshot: dict[str, Any], *, webspace_id: str | None = None) -> None:
    _ensure_skill_data_projections()
    ctx_subnet.set("subnet_env.snapshot", snapshot, webspace_id=_projection_webspace_id(webspace_id))


def _refresh(*, webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _build_snapshot()
    _project_snapshot(snapshot, webspace_id=webspace_id)
    return snapshot


@tool("get_snapshot")
def get_snapshot(
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    return _refresh(webspace_id=webspace_id)


@tool("refresh_snapshot")
def refresh_snapshot(
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    snapshot = _refresh(webspace_id=webspace_id)
    return {"ok": True, **snapshot}


@tool("set_env_value")
def set_env_value(
    key: str,
    value: Any = None,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    token = str(key or "").strip()
    if token not in _ALLOWED_ENV_KEYS:
        return {"ok": False, "error": "key_not_allowed", "key": token}
    try:
        normalized = _normalize_env_value(token, value)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_value", "key": token, "message": str(exc)}
    dotenv_path = _dotenv_path()
    _persist_env_value(dotenv_path, token, normalized)
    if normalized:
        os.environ[token] = normalized
    else:
        os.environ.pop(token, None)
    snapshot = _refresh(webspace_id=webspace_id)
    return {"ok": True, "key": token, "value": normalized, **snapshot}


@tool("apply_action")
def apply_action(
    action_id: str,
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    token = str(action_id or "").strip()
    if token == "refresh_snapshot":
        snapshot = _refresh(webspace_id=webspace_id)
        return {"ok": True, "action_id": token, **snapshot}
    if token in _ENV_TYPE_ACTIONS:
        key, value = _ENV_TYPE_ACTIONS[token]
        return set_env_value(key=key, value=value, webspace_id=webspace_id)
    if token in _SUBNET_ACTIONS:
        key, value = _SUBNET_ACTIONS[token]
        return set_env_value(key=key, value=value, webspace_id=webspace_id)
    if token in _LOG_LEVEL_ACTIONS:
        key, value = _LOG_LEVEL_ACTIONS[token]
        return set_env_value(key=key, value=value, webspace_id=webspace_id)
    if token.startswith("toggle::"):
        key = token.split("::", 1)[1].strip()
        if key not in _ALLOWED_ENV_KEYS:
            return {"ok": False, "error": "key_not_allowed", "action_id": token}
        file_env = _read_dotenv(_dotenv_path())
        current = _env_flag_enabled(file_env, key)
        return set_env_value(key=key, value="0" if current else "1", webspace_id=webspace_id)
    return {"ok": False, "error": "unknown_action", "action_id": token}


def handle(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload_map = dict(payload or {})
    if "action_id" in payload_map:
        return apply_action(
            action_id=str(payload_map.get("action_id") or ""),
            webspace_id=str(payload_map.get("webspace_id") or "").strip() or None,
        )
    if "key" in payload_map:
        return set_env_value(
            key=str(payload_map.get("key") or ""),
            value=payload_map.get("value"),
            webspace_id=str(payload_map.get("webspace_id") or "").strip() or None,
        )
    return get_snapshot(webspace_id=str(payload_map.get("webspace_id") or "").strip() or None)
