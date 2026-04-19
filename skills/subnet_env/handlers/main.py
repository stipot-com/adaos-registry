from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from adaos.sdk.core.decorators import tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.control_plane import get_reliability_projection, get_self_object
from adaos.services.agent_context import get_ctx
from adaos.services import node_config as _node_config

_ALLOWED_ENV_KEYS = {
    "ENV_TYPE",
    "GIT_USER",
    "GIT_EMAIL",
    "ADAOS_SUBNET_YJS_REPLICATION",
    "ADAOS_CLI_DEBUG",
    "ADAOS_LOG_LEVEL",
    "ADAOS_SCENARIO_LOG_LEVEL",
    "HUB_NATS_VERBOSE",
    "HUB_NATS_TRACE",
    "HUB_NATS_TRACE_INPUT",
    "HUB_ROUTE_VERBOSE",
    "ROUTE_PROXY_VERBOSE",
    "HUB_TG_DEBUG",
}

_DIAGNOSTIC_FLAG_KEYS = [
    "ADAOS_CLI_DEBUG",
    "HUB_NATS_VERBOSE",
    "HUB_NATS_TRACE",
    "HUB_NATS_TRACE_INPUT",
    "HUB_ROUTE_VERBOSE",
    "ROUTE_PROXY_VERBOSE",
    "HUB_TG_DEBUG",
]

_LOG_LEVEL_KEYS = [
    "ADAOS_LOG_LEVEL",
    "ADAOS_SCENARIO_LOG_LEVEL",
]

_ENV_TYPE_ACTIONS = {
    "env_type_default": ("ENV_TYPE", ""),
    "env_type_dev": ("ENV_TYPE", "dev"),
    "env_type_prod": ("ENV_TYPE", "prod"),
}

_SUBNET_ACTIONS = {
    "subnet_yjs_replication_on": ("ADAOS_SUBNET_YJS_REPLICATION", "1"),
    "subnet_yjs_replication_off": ("ADAOS_SUBNET_YJS_REPLICATION", "0"),
}

_LOG_LEVEL_ACTIONS = {
    "log_level_info": ("ADAOS_LOG_LEVEL", "INFO"),
    "log_level_debug": ("ADAOS_LOG_LEVEL", "DEBUG"),
    "scenario_log_level_info": ("ADAOS_SCENARIO_LOG_LEVEL", "INFO"),
    "scenario_log_level_debug": ("ADAOS_SCENARIO_LOG_LEVEL", "DEBUG"),
}

_BOOL_TRUE = {"1", "true", "yes", "on"}


def lang_res() -> dict[str, str]:
    return {}


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return Path.cwd().resolve()


def _dotenv_path() -> Path:
    explicit = str(os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (_repo_root() / ".env").resolve()


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


def _normalize_env_value(key: str, value: Any) -> str:
    token = "" if value is None else str(value).strip()
    if key == "ENV_TYPE":
        lowered = token.lower()
        if lowered in {"", "dev", "prod"}:
            return lowered
        return token
    if key in _LOG_LEVEL_KEYS:
        return token.upper() or "INFO"
    if key in _ALLOWED_ENV_KEYS and (
        key.endswith("_DEBUG")
        or key.endswith("_VERBOSE")
        or key.endswith("_TRACE")
        or key.endswith("_TRACE_INPUT")
        or key == "ADAOS_SUBNET_YJS_REPLICATION"
    ):
        lowered = token.lower()
        return "1" if lowered in _BOOL_TRUE else "0"
    return token


def _dotenv_line(key: str, value: Any) -> str:
    normalized = _normalize_env_value(key, value)
    escaped = normalized.replace('"', '\\"')
    if not normalized:
        return f"{key}=\n"
    if any(ch.isspace() for ch in normalized) or "#" in normalized:
        return f'{key}="{escaped}"\n'
    return f"{key}={normalized}\n"


def _write_dotenv_value(path: Path, key: str, value: Any) -> None:
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


def _env_file_value(file_env: dict[str, str], key: str) -> str:
    return str(file_env.get(key) or "").strip()


def _effective_env_value(file_env: dict[str, str], key: str) -> str:
    raw = os.getenv(key)
    if raw is not None:
        return str(raw).strip()
    return _env_file_value(file_env, key)


def _env_flag_enabled(file_env: dict[str, str], key: str) -> bool:
    return _effective_env_value(file_env, key).strip().lower() in _BOOL_TRUE


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        existing = ctx.projections.resolve("subnet", "subnet_env.snapshot")
        if existing:
            return
        manifest_path = Path(__file__).resolve().parents[1] / "skill.yaml"
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        entries = payload.get("data_projections") or []
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


def _overview_items(file_env: dict[str, str], node: dict[str, Any], reliability: dict[str, Any], dotenv_path: Path) -> list[dict[str, Any]]:
    runtime = node.get("runtime") if isinstance(node.get("runtime"), dict) else {}
    route_mode = str(runtime.get("route_mode") or "").strip()
    connected = runtime.get("connected_to_hub")
    projection = reliability.get("subject") if isinstance(reliability.get("subject"), dict) else {}
    health = projection.get("health") if isinstance(projection.get("health"), dict) else {}
    return [
        {
            "id": "env-file",
            "title": "dotenv source",
            "description": str(dotenv_path),
        },
        {
            "id": "env-type",
            "title": "ENV_TYPE",
            "description": _effective_env_value(file_env, "ENV_TYPE") or "<default>",
        },
        {
            "id": "zone-id",
            "title": "zone_id",
            "description": node.get("zone_id") or _effective_env_value(file_env, "ADAOS_ZONE_ID") or "<unset>",
        },
        {
            "id": "node",
            "title": "node",
            "description": (
                f"{node.get('primary_node_name') or node.get('node_id') or '-'} | "
                f"role={node.get('role') or '-'} | subnet={node.get('subnet_id') or '-'}"
            ),
        },
        {
            "id": "routing",
            "title": "routing",
            "description": f"route_mode={route_mode or '-'} | connected_to_hub={connected}",
        },
        {
            "id": "projection",
            "title": "reliability",
            "description": (
                f"status={projection.get('status') or '-'} | "
                f"connectivity={health.get('connectivity') or '-'} | "
                f"runtime_freshness={health.get('runtime_freshness') or '-'}"
            ),
        },
        {
            "id": "git",
            "title": "git identity",
            "description": (
                f"{_effective_env_value(file_env, 'GIT_USER') or '<unset>'} "
                f"<{_effective_env_value(file_env, 'GIT_EMAIL') or '<unset>'}>"
            ),
        },
        {
            "id": "subnet-sync",
            "title": "subnet yjs replication",
            "description": "enabled" if _env_flag_enabled(file_env, "ADAOS_SUBNET_YJS_REPLICATION") else "disabled",
        },
    ]


def _action_groups(file_env: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    env_type = _effective_env_value(file_env, "ENV_TYPE")
    env_label = env_type or "<default>"
    env_actions = [
        {"id": "env_type_default", "label": f"Use default ({env_label})" if not env_type else "Clear ENV_TYPE"},
        {"id": "env_type_dev", "label": "Set ENV_TYPE=dev"},
        {"id": "env_type_prod", "label": "Set ENV_TYPE=prod"},
    ]

    subnet_actions = [
        {
            "id": "subnet_yjs_replication_on",
            "label": f"YJS replication: {'on' if _env_flag_enabled(file_env, 'ADAOS_SUBNET_YJS_REPLICATION') else 'turn on'}",
        },
        {
            "id": "subnet_yjs_replication_off",
            "label": f"YJS replication: {'turn off' if _env_flag_enabled(file_env, 'ADAOS_SUBNET_YJS_REPLICATION') else 'off'}",
        },
    ]

    diag_actions: list[dict[str, Any]] = []
    for key in _DIAGNOSTIC_FLAG_KEYS:
        enabled = _env_flag_enabled(file_env, key)
        diag_actions.append(
            {
                "id": f"toggle::{key}",
                "label": f"{key}: {'on' if enabled else 'off'}",
            }
        )
    for action_id, (key, value) in _LOG_LEVEL_ACTIONS.items():
        current = _effective_env_value(file_env, key).upper() or "INFO"
        diag_actions.append(
            {
                "id": action_id,
                "label": f"{key}: {current} -> {value}",
            }
        )

    general_actions = [
        {"id": "refresh_snapshot", "label": "Refresh snapshot"},
    ]

    return {
        "env_type": env_actions,
        "subnet": subnet_actions,
        "diagnostics": diag_actions,
        "general": general_actions,
    }


def _build_snapshot() -> dict[str, Any]:
    _ensure_skill_data_projections()
    dotenv_path = _dotenv_path()
    file_env = _read_dotenv(dotenv_path)
    node = _node_payload()
    reliability = get_reliability_projection()
    env_type = _effective_env_value(file_env, "ENV_TYPE") or "default"
    zone_id = node.get("zone_id") or _effective_env_value(file_env, "ADAOS_ZONE_ID") or "unset"
    yjs_enabled = _env_flag_enabled(file_env, "ADAOS_SUBNET_YJS_REPLICATION")

    snapshot = {
        "summary": {
            "value": env_type,
            "label": f"zone={zone_id} | yjs={'on' if yjs_enabled else 'off'}",
        },
        "overview": _overview_items(file_env, node, reliability, dotenv_path),
        "forms": {
            "git_user": {"value": _effective_env_value(file_env, "GIT_USER")},
            "git_email": {"value": _effective_env_value(file_env, "GIT_EMAIL")},
        },
        "actions": _action_groups(file_env),
        "env": {
            "dotenv_path": str(dotenv_path),
            "exists": dotenv_path.exists(),
            "effective": {key: _effective_env_value(file_env, key) for key in sorted(_ALLOWED_ENV_KEYS | {"ADAOS_ZONE_ID"})},
            "file": {key: _env_file_value(file_env, key) for key in sorted(_ALLOWED_ENV_KEYS | {"ADAOS_ZONE_ID"})},
        },
        "node": node,
        "safety": {
            "editable_keys": sorted(_ALLOWED_ENV_KEYS),
            "read_only_notes": [
                "Secrets and tokens are intentionally excluded from subnet_env MVP.",
                "Some dotenv changes may require runtime restart to fully apply.",
            ],
        },
    }
    return snapshot


def _project_snapshot(snapshot: dict[str, Any], *, webspace_id: str | None = None) -> None:
    _ensure_skill_data_projections()
    ctx_subnet.set("subnet_env.snapshot", snapshot, webspace_id=webspace_id)


def _refresh(*, webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _build_snapshot()
    _project_snapshot(snapshot, webspace_id=webspace_id)
    return snapshot


@tool("get_snapshot")
def get_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    return _refresh(webspace_id=webspace_id)


@tool("refresh_snapshot")
def refresh_snapshot(webspace_id: str | None = None) -> dict[str, Any]:
    snapshot = _refresh(webspace_id=webspace_id)
    return {"ok": True, **snapshot}


@tool("set_env_value")
def set_env_value(key: str, value: Any = None, webspace_id: str | None = None) -> dict[str, Any]:
    token = str(key or "").strip()
    if token not in _ALLOWED_ENV_KEYS:
        return {"ok": False, "error": "key_not_allowed", "key": token}
    normalized = _normalize_env_value(token, value)
    _write_dotenv_value(_dotenv_path(), token, normalized)
    os.environ[token] = normalized
    snapshot = _refresh(webspace_id=webspace_id)
    return {"ok": True, "key": token, "value": normalized, **snapshot}


@tool("apply_action")
def apply_action(action_id: str, webspace_id: str | None = None) -> dict[str, Any]:
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
