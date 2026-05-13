from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data import root_mcp as sdk_root_mcp

_CACHE: dict[str, Any] = {"ts": 0.0, "snapshot": None}
_CACHE_TTL_S = 5.0
_log = logging.getLogger("skills.infra_access_skill")
_LAST_ISSUED: dict[str, Any] = {}
_REFRESH_THREAD: threading.Thread | None = None
_REFRESH_LOCK = threading.Lock()
_PROJECTION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="infra-access-projection")
_DATA_PROJECTION_ENTRIES = [
    {
        "scope": "subnet",
        "slot": "infra_access.snapshot",
        "targets": [
            {
                "backend": "yjs",
                "path": "data/infra_access",
            },
        ],
    },
]


def lang_res() -> Dict[str, str]:
    return {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _webspace_id_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    token = str(payload.get("webspace_id") or payload.get("workspace_id") or "").strip()
    if token:
        return token
    meta = payload.get("_meta")
    if isinstance(meta, Mapping):
        token = str(meta.get("webspace_id") or meta.get("workspace_id") or "").strip()
        if token:
            return token
    return None


def _target_id_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    token = str(payload.get("target_id") or payload.get("target_node_id") or payload.get("node_id") or "").strip()
    if token:
        return token
    meta = payload.get("_meta")
    if isinstance(meta, Mapping):
        token = str(meta.get("target_id") or meta.get("target_node_id") or meta.get("node_id") or "").strip()
        if token:
            return token
    return None


def _managed_target_id_for_root_mcp(target_id: str | None) -> str | None:
    token = str(target_id or "").strip()
    if not token:
        return None
    if token.startswith("hub:"):
        return token
    # UI node selectors are not Root MCP managed targets. Let the SDK infer the
    # local hub target instead of forwarding a node UUID to the target registry.
    return None


def _format_when(value: Any) -> str:
    token = str(value or "").strip()
    return token or "unknown"


def _mcp_http_url(root_url: str) -> str:
    token = str(root_url or "").strip()
    return token.rstrip("/") + "/v1/root/mcp" if token else ""


def _prepare_codex_command(*, root_url: str, target_id: str, capability_profile: str = "ProfileOpsRead") -> str:
    resolved_root_url = str(root_url or "").strip()
    resolved_target_id = str(target_id or "").strip()
    if not resolved_root_url or not resolved_target_id:
        return ""
    return (
        f"adaos dev root mcp prepare-codex --target-id {resolved_target_id} "
        f"--root-url {resolved_root_url} --capability-profile {capability_profile} --apply-codex"
    )


def _result_block(payload: Mapping[str, Any]) -> dict[str, Any]:
    response = payload.get("response")
    if isinstance(response, Mapping):
        result = response.get("result")
        if isinstance(result, Mapping):
            return dict(result)
    return {}


def _list_block(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    block = _result_block(payload)
    raw = block.get(key)
    return [dict(item) for item in raw] if isinstance(raw, list) else []


def _activity_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = _result_block(payload).get("events")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        tool_id = str(item.get("tool_id") or item.get("event_id") or "event").strip() or "event"
        status = str(item.get("status") or item.get("outcome") or "observed").strip() or "observed"
        rows.append(
            {
                "id": str(item.get("event_id") or tool_id),
                "title": tool_id,
                "description": f"{status} | {_format_when(item.get('created_at') or item.get('at'))}",
                "content": dict(item),
            }
        )
    return rows


def _credential_rows(
    *,
    root_url: str,
    sessions: list[dict[str, Any]],
    access_tokens: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mcp_http_url = _mcp_http_url(root_url)
    rows: list[dict[str, Any]] = []
    for item in sessions:
        session_id = str(item.get("session_id") or "").strip()
        rows.append(
            {
                "id": f"session:{session_id or len(rows)}",
                "title": f"MCP session {session_id or 'unknown'}",
                "description": " | ".join(
                    bit
                    for bit in [
                        str(item.get("capability_profile") or "custom").strip(),
                        str(item.get("status") or "unknown").strip(),
                        f"expires {_format_when(item.get('expires_at'))}",
                    ]
                    if bit
                ),
                "content": {
                    "kind": "mcp_session",
                    "target_id": item.get("target_id"),
                    "audience": item.get("audience"),
                    "capability_profile": item.get("capability_profile"),
                    "status": item.get("status"),
                    "expires_at": item.get("expires_at"),
                    "last_used_at": item.get("last_used_at"),
                    "use_count": item.get("use_count"),
                    "mcp_http_url": mcp_http_url,
                    "auth": "Bearer <session token is returned only when issuing a fresh session>",
                },
            }
        )
    for item in access_tokens:
        token_id = str(item.get("token_id") or "").strip()
        rows.append(
            {
                "id": f"token:{token_id or len(rows)}",
                "title": f"Access token {token_id or 'unknown'}",
                "description": " | ".join(
                    bit
                    for bit in [
                        str(item.get("audience") or "unknown").strip(),
                        str(item.get("status") or "unknown").strip(),
                        f"expires {_format_when(item.get('expires_at'))}",
                    ]
                    if bit
                ),
                "content": {
                    "kind": "access_token",
                    "primary_target_id": item.get("primary_target_id"),
                    "target_ids": item.get("target_ids"),
                    "status": item.get("status"),
                    "expires_at": item.get("expires_at"),
                    "mcp_http_url": mcp_http_url,
                    "auth": "Bearer <token secret is shown only at issue time>",
                },
            }
        )
    return rows


def _codex_help(*, root_url: str, target_id: str, capability_profile: str = "ProfileOpsRead") -> list[dict[str, Any]]:
    command = _prepare_codex_command(
        root_url=root_url,
        target_id=target_id,
        capability_profile=capability_profile,
    )
    return [
        {
            "id": "codex-bootstrap",
            "title": "Connect Root MCP to VS Code Codex",
            "description": "Issue a fresh MCP Session Lease, set ADAOS_ROOT_MCP_AUTH to the returned bearer, and restart VS Code/Codex if it still uses the previous token.",
            "content": {
                "step_1": f"Issue a new MCP session for target {target_id}",
                "step_2": f"Set the OS env var: setx ADAOS_ROOT_MCP_AUTH \"<fresh bearer>\"",
                "step_3": "If Codex still authenticates with the previous bearer, fully restart VS Code so new MCP helper processes inherit the updated environment.",
                "step_4": f"Configure the MCP URL in VS Code Codex as {_mcp_http_url(root_url)}",
                "step_5": command,
                "note": "Existing sessions and access tokens are listed below without bearer secrets; a fresh bearer is returned only when you issue a new session. `setx` affects new processes only.",
                "root_url": root_url,
                "mcp_http_url": _mcp_http_url(root_url),
                "bearer_env_var": "ADAOS_ROOT_MCP_AUTH",
                "windows_setx_example": "setx ADAOS_ROOT_MCP_AUTH \"mcp_...\"",
            },
        }
    ]


def _summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    sessions = list(snapshot.get("sessions") or [])
    access_tokens = list(snapshot.get("access_tokens") or [])
    target_id = str(snapshot.get("target_id") or "unknown").strip() or "unknown"
    root_url = str(snapshot.get("root_url") or "").strip() or "unknown"
    generated_at = str(snapshot.get("generated_at") or "unknown").strip() or "unknown"
    ok = bool(snapshot.get("ok"))
    return {
        "value": len(sessions) + len(access_tokens),
        "label": "Active MCP credentials",
        "subtitle": target_id,
        "description": f"{root_url} | updated {generated_at} | {'live' if ok else 'fallback'}",
    }


def _summary_items(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    profiles = list(snapshot.get("session_capability_profiles") or [])
    generated_at = str(snapshot.get("generated_at") or "unknown").strip() or "unknown"
    ok = bool(snapshot.get("ok"))
    items = [
        {
            "id": "snapshot-updated-at",
            "title": "Snapshot updated",
            "description": generated_at,
        },
        {
            "id": "snapshot-state",
            "title": "Snapshot state",
            "description": "live" if ok else "fallback",
        },
        {
            "id": "target",
            "title": "Managed target",
            "description": str(snapshot.get("target_id") or "unknown"),
        },
        {
            "id": "root-url",
            "title": "Root URL",
            "description": str(snapshot.get("root_url") or "unknown"),
        },
        {
            "id": "mcp-http-url",
            "title": "MCP HTTP URL",
            "description": _mcp_http_url(str(snapshot.get("root_url") or "")) or "unknown",
        },
        {
            "id": "bootstrap",
            "title": "Preferred bootstrap",
            "description": str(snapshot.get("preferred_bootstrap") or "MCP Session Lease"),
        },
        {
            "id": "profiles",
            "title": "Capability profiles",
            "description": ", ".join(str(item) for item in profiles) if profiles else "No named capability profiles published",
        },
        {
            "id": "codex-comment",
            "title": "VS Code Codex",
            "description": "Issue a fresh session, write the returned bearer to ADAOS_ROOT_MCP_AUTH, and restart VS Code/Codex if the current process still uses the previous token.",
        },
    ]
    return items


def _connection_block(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    root_url = str(snapshot.get("root_url") or "").strip()
    target_id = str(snapshot.get("target_id") or "").strip()
    capability_profiles = list(snapshot.get("session_capability_profiles") or [])
    capability_profile = str(capability_profiles[0] if capability_profiles else "ProfileOpsRead")
    return {
        "root_url": root_url,
        "root_url_language": "text",
        "mcp_http_url": _mcp_http_url(root_url),
        "mcp_http_url_language": "text",
        "target_id": target_id,
        "bootstrap_mode": "mcp_session_lease",
        "generated_at": str(snapshot.get("generated_at") or "").strip(),
        "codex_prepare_command": _prepare_codex_command(
            root_url=root_url,
            target_id=target_id,
            capability_profile=capability_profile,
        ),
        "codex_prepare_language": "bash",
    }


def _issued_connection_block(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    issued = snapshot.get("issued_connection")
    if not isinstance(issued, Mapping):
        return {
            "session_id": "",
            "access_token": "",
            "expires_at": "",
            "capability_profile": "",
            "issued_at": "",
            "target_id": str(snapshot.get("target_id") or ""),
            "root_url": str(snapshot.get("root_url") or ""),
            "mcp_http_url": _mcp_http_url(str(snapshot.get("root_url") or "")),
            "access_token_language": "text",
            "session_id_language": "text",
            "empty": True,
        }
    return {
        "session_id": str(issued.get("session_id") or ""),
        "session_id_language": "text",
        "access_token": str(issued.get("access_token") or ""),
        "access_token_language": "text",
        "expires_at": str(issued.get("expires_at") or ""),
        "capability_profile": str(issued.get("capability_profile") or ""),
        "issued_at": str(issued.get("issued_at") or snapshot.get("generated_at") or ""),
        "target_id": str(issued.get("target_id") or snapshot.get("target_id") or ""),
        "root_url": str(issued.get("root_url") or snapshot.get("root_url") or ""),
        "mcp_http_url": str(issued.get("mcp_http_url") or _mcp_http_url(str(snapshot.get("root_url") or ""))),
        "empty": False,
    }


def _with_last_issued(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(snapshot)
    issued = _LAST_ISSUED.get("payload")
    if isinstance(issued, Mapping):
        result["issued_connection"] = dict(issued)
        summary_items = list(result.get("summary_items") or [])
        summary_items.append(
            {
                "id": "issued-session",
                "title": "Fresh session",
                "description": str(issued.get("session_id") or "available"),
            }
        )
        result["summary_items"] = summary_items
        events = list(result.get("events") or [])
        events.insert(
            0,
            {
                "id": f"issued:{issued.get('session_id') or 'session'}",
                "title": "MCP session issued",
                "description": "A fresh bearer token is available below. Update ADAOS_ROOT_MCP_AUTH and restart VS Code/Codex if the existing process keeps the old token.",
                "content": dict(issued),
            },
        )
        result["events"] = events
    result["issued_connection"] = _issued_connection_block(result)
    return result


def _fallback_snapshot(
    exc: Exception,
    *,
    target_id: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context_block = dict(context or {})
    requested_target_id = str(target_id or context_block.get("target_id") or "").strip()
    root_url = str(context_block.get("root_url") or "").strip()
    subnet_id = str(context_block.get("subnet_id") or "").strip() or None
    zone = str(context_block.get("zone") or "").strip() or None
    error_text = f"{type(exc).__name__}: {exc}"
    snapshot = {
        "ok": False,
        "generated_at": _iso_now(),
        "target_id": requested_target_id or "unknown",
        "root_url": root_url,
        "subnet_id": subnet_id,
        "zone": zone,
        "preferred_bootstrap": "root-issued MCP Session Lease",
        "session_capability_profiles": ["ProfileOpsRead", "ProfileOpsControl"],
        "operational_surface": {},
        "sessions": [],
        "access_tokens": [],
        "errors": [error_text],
    }
    snapshot["summary"] = {
        "value": 0,
        "label": "Active MCP credentials",
        "subtitle": requested_target_id or "unresolved target",
        "description": f"fallback | updated {snapshot['generated_at']} | {error_text}",
    }
    snapshot["tokens"] = []
    snapshot["events"] = [
        {
            "id": "snapshot-error",
            "title": "infra_access snapshot failed",
            "description": error_text,
            "content": {
                "error": error_text,
            },
        }
    ]
    snapshot["summary_items"] = _summary_items(snapshot) + [
        {
            "id": "error",
            "title": "Snapshot error",
            "description": error_text,
        },
        {
            "id": "hint",
            "title": "What to check",
            "description": "Verify Root auth context for this node and then press Refresh.",
        },
    ]
    snapshot["codex_help"] = [
        {
            "id": "codex-bootstrap-error",
            "title": "Connect Root MCP to VS Code Codex",
            "description": "Fix Root access for this node first, then issue a fresh MCP session.",
            "content": {
                "error": error_text,
                "hint": "Issue a fresh MCP session after Root access is restored, store the returned bearer in ADAOS_ROOT_MCP_AUTH, and restart VS Code/Codex if it still uses the previous token. The URL below is already the stable zonal MCP endpoint.",
                "root_url": root_url,
                "mcp_http_url": _mcp_http_url(root_url),
                "bearer_env_var": "ADAOS_ROOT_MCP_AUTH",
                "windows_setx_example": "setx ADAOS_ROOT_MCP_AUTH \"mcp_...\"",
            },
        }
    ]
    snapshot["connection"] = {
        "root_url": root_url,
        "root_url_language": "text",
        "mcp_http_url": _mcp_http_url(root_url),
        "mcp_http_url_language": "text",
        "target_id": requested_target_id,
        "bootstrap_mode": "mcp_session_lease",
        "generated_at": snapshot["generated_at"],
        "codex_prepare_command": _prepare_codex_command(
            root_url=root_url,
            target_id=requested_target_id,
            capability_profile="ProfileOpsRead",
        ),
        "codex_prepare_language": "bash",
        "error": error_text,
    }
    return _with_last_issued(snapshot)


def _build_snapshot(*, target_id: str | None = None) -> dict[str, Any]:
    context = sdk_root_mcp.get_local_target_context(target_id=target_id)
    effective_target_id = str(context.get("target_id") or "").strip()
    if not effective_target_id:
        raise RuntimeError("Unable to infer managed target id for infra_access_skill.")

    surface_payload = sdk_root_mcp.get_local_operational_surface(target_id=effective_target_id, root_url=context.get("root_url"))
    surface_result = _result_block(surface_payload)
    operational_surface = dict(surface_result.get("operational_surface") or {})

    sessions_payload = sdk_root_mcp.list_local_mcp_sessions(
        target_id=effective_target_id,
        root_url=context.get("root_url"),
        limit=50,
        active_only=False,
    )
    access_payload = sdk_root_mcp.list_local_access_tokens(
        target_id=effective_target_id,
        root_url=context.get("root_url"),
        limit=50,
        active_only=False,
    )
    activity_payload = sdk_root_mcp.get_local_activity_log(
        target_id=effective_target_id,
        root_url=context.get("root_url"),
        limit=20,
    )

    sessions = _list_block(sessions_payload, "sessions")
    access_tokens = _list_block(access_payload, "tokens")
    snapshot = {
        "ok": True,
        "generated_at": _iso_now(),
        "target_id": effective_target_id,
        "root_url": str(context.get("root_url") or "").strip(),
        "subnet_id": context.get("subnet_id"),
        "zone": context.get("zone"),
        "preferred_bootstrap": "root-issued MCP Session Lease",
        "session_capability_profiles": list(((operational_surface.get("token_management") or {}) if isinstance(operational_surface.get("token_management"), Mapping) else {}).get("session_capability_profiles") or []),
        "operational_surface": operational_surface,
        "sessions": sessions,
        "access_tokens": access_tokens,
    }
    snapshot["summary"] = _summary(snapshot)
    snapshot["summary_items"] = _summary_items(snapshot)
    snapshot["tokens"] = _credential_rows(
        root_url=str(snapshot["root_url"]),
        sessions=sessions,
        access_tokens=access_tokens,
    )
    snapshot["events"] = _activity_rows(activity_payload)
    snapshot["codex_help"] = _codex_help(
        root_url=str(snapshot["root_url"]),
        target_id=effective_target_id,
    )
    snapshot["connection"] = _connection_block(snapshot)
    return _with_last_issued(snapshot)


def _projection_webspace_ids(webspace_id: str | None = None) -> list[str]:
    from adaos.services.yjs.webspace import default_webspace_id

    ids: set[str] = set()
    token = str(webspace_id or "").strip()
    if token:
        ids.add(token)
    ids.add(default_webspace_id())
    try:
        from adaos.services.scenario.webspace_runtime import WebspaceService

        for info in WebspaceService().list(mode="mixed"):
            current = str(getattr(info, "id", "") or "").strip()
            if current:
                ids.add(current)
    except Exception:
        _log.debug("failed to enumerate webspaces for infra_access projection", exc_info=True)
    return sorted(ids)


def _ensure_skill_data_projections() -> None:
    try:
        from adaos.services.agent_context import get_ctx

        ctx = get_ctx()
        if ctx.projections.resolve("subnet", "infra_access.snapshot"):
            return
        ctx.projections.load_entries(_DATA_PROJECTION_ENTRIES)
    except Exception:
        pass


async def _project_async(snapshot: dict[str, Any], *, webspace_id: str | None = None) -> None:
    _ensure_skill_data_projections()
    for target_ws in _projection_webspace_ids(webspace_id):
        await ctx_subnet.set_async("infra_access.snapshot", snapshot, webspace_id=target_ws)


def _project(snapshot: dict[str, Any], *, webspace_id: str | None = None) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_project_async(snapshot, webspace_id=webspace_id))
        return
    _PROJECTION_EXECUTOR.submit(
        lambda: asyncio.run(_project_async(snapshot, webspace_id=webspace_id))
    ).result()


def _schedule_subscription_refresh(*, webspace_id: str | None = None, target_id: str | None = None) -> None:
    global _REFRESH_THREAD

    def _runner() -> None:
        global _REFRESH_THREAD
        try:
            snapshot = _snapshot_or_cached(force=True, target_id=target_id)
            _project(snapshot, webspace_id=webspace_id)
        except Exception:
            _log.warning("infra_access background refresh subscription failed", exc_info=True)
        finally:
            with _REFRESH_LOCK:
                _REFRESH_THREAD = None

    with _REFRESH_LOCK:
        if _REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive():
            return
        _REFRESH_THREAD = threading.Thread(
            target=_runner,
            name="infra-access-refresh",
            daemon=True,
        )
        _REFRESH_THREAD.start()


def _snapshot_or_cached(*, force: bool = False, target_id: str | None = None) -> dict[str, Any]:
    managed_target_id = _managed_target_id_for_root_mcp(target_id)
    cached = _CACHE.get("snapshot")
    if not force and isinstance(cached, dict) and (time.time() - float(_CACHE.get("ts") or 0.0)) <= _CACHE_TTL_S:
        return _with_last_issued(dict(cached))
    context: dict[str, Any] | None = None
    try:
        try:
            context = sdk_root_mcp.get_local_target_context(target_id=managed_target_id)
        except Exception:
            context = None
        snapshot = _build_snapshot(target_id=managed_target_id)
    except Exception as exc:
        _log.warning("infra_access snapshot failed; projecting fallback snapshot", exc_info=True)
        snapshot = _fallback_snapshot(exc, target_id=managed_target_id, context=context)
    _CACHE["ts"] = time.time()
    _CACHE["snapshot"] = dict(snapshot)
    return _with_last_issued(dict(snapshot))


@tool("get_snapshot")
def get_snapshot(
    webspace_id: str | None = None,
    target_id: str | None = None,
    target_node_id: str | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    effective_target_id = str(target_id or target_node_id or node_id or "").strip() or None
    snapshot = _snapshot_or_cached(force=False, target_id=effective_target_id)
    _project(snapshot, webspace_id=webspace_id)
    return snapshot


@tool("refresh_snapshot")
def refresh_snapshot(
    webspace_id: str | None = None,
    target_id: str | None = None,
    target_node_id: str | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    effective_target_id = str(target_id or target_node_id or node_id or "").strip() or None
    snapshot = _snapshot_or_cached(force=True, target_id=effective_target_id)
    _project(snapshot, webspace_id=webspace_id)
    return snapshot


@tool("issue_codex_connection")
def issue_codex_connection(
    target_id: str | None = None,
    capability_profile: str = "ProfileOpsRead",
    ttl_seconds: int = 28_800,
    webspace_id: str | None = None,
) -> dict[str, Any]:
    managed_target_id = _managed_target_id_for_root_mcp(target_id)
    issued = sdk_root_mcp.issue_local_codex_mcp_session(
        target_id=managed_target_id,
        capability_profile=capability_profile,
        ttl_seconds=int(ttl_seconds),
    )
    result = _result_block(issued)
    context = sdk_root_mcp.get_local_target_context(target_id=managed_target_id)
    payload = {
        "ok": True,
        "root_url": context.get("root_url"),
        "mcp_http_url": _mcp_http_url(str(context.get("root_url") or "")),
        "target_id": context.get("target_id"),
        "session_id": result.get("session_id"),
        "access_token": result.get("access_token"),
        "expires_at": result.get("expires_at"),
        "capability_profile": result.get("capability_profile") or capability_profile,
        "issued_at": _iso_now(),
        "bearer_env_var": "ADAOS_ROOT_MCP_AUTH",
        "windows_setx_example": f"setx ADAOS_ROOT_MCP_AUTH \"{str(result.get('access_token') or '')}\"",
        "restart_note": "setx updates the environment for new processes only; restart VS Code/Codex if it still uses the previous bearer.",
        "codex_prepare_command": _prepare_codex_command(
            root_url=str(context.get("root_url") or ""),
            target_id=str(context.get("target_id") or ""),
            capability_profile=str(result.get("capability_profile") or capability_profile),
        ),
    }
    _LAST_ISSUED["payload"] = dict(payload)
    try:
        snapshot = _snapshot_or_cached(force=True, target_id=managed_target_id)
        snapshot = _with_last_issued(snapshot)
        _project(snapshot, webspace_id=webspace_id)
    except Exception:
        _log.warning("infra_access failed to refresh projected snapshot after issuing MCP session", exc_info=True)
    return payload


@subscribe("infra_access.action")
def _on_action(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    if not isinstance(payload, Mapping):
        return
    action_id = str(payload.get("id") or "").strip().lower()
    webspace_id = _webspace_id_from_payload(payload)
    target_id = _target_id_from_payload(payload)
    if action_id == "refresh":
        try:
            refresh_snapshot(webspace_id=webspace_id, target_id=target_id)
        except Exception:
            _log.warning("infra_access refresh action failed", exc_info=True)
        return
    if action_id == "issue_codex_session":
        capability_profile = str(payload.get("capability_profile") or "ProfileOpsRead").strip() or "ProfileOpsRead"
        ttl_seconds = int(payload.get("ttl_seconds") or 28_800)
        try:
            issue_codex_connection(
                target_id=target_id,
                capability_profile=capability_profile,
                ttl_seconds=ttl_seconds,
                webspace_id=webspace_id,
            )
        except Exception:
            _log.warning("infra_access issue_codex_session action failed", exc_info=True)


@subscribe("sys.ready")
@subscribe("desktop.webspace.refresh")
@subscribe("desktop.webspace.reload")
@subscribe("desktop.webspace.reloaded")
@subscribe("desktop.scenario.set")
@subscribe("skills.activated")
@subscribe("operations.")
def _on_refresh(evt: Any) -> None:
    payload = getattr(evt, "payload", evt)
    event_type = str(getattr(evt, "type", "") or "").strip()
    if event_type == "sys.ready":
        return
    webspace_id = _webspace_id_from_payload(payload)
    target_id = _target_id_from_payload(payload)
    _schedule_subscription_refresh(webspace_id=webspace_id, target_id=target_id)


def handle(topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    topic_token = str(topic or "").strip().lower()
    if topic_token in {"infra_access.refresh", "desktop.webspace.refresh", "sys.ready"}:
        return refresh_snapshot(webspace_id=_webspace_id_from_payload(data), target_id=_target_id_from_payload(data))
    if topic_token == "infra_access.action":
        action_id = str(data.get("id") or "").strip().lower()
        if action_id == "refresh":
            return refresh_snapshot(webspace_id=_webspace_id_from_payload(data), target_id=_target_id_from_payload(data))
        if action_id == "issue_codex_session":
            return issue_codex_connection(
                target_id=_target_id_from_payload(data),
                capability_profile=str(data.get("capability_profile") or "ProfileOpsRead"),
                ttl_seconds=int(data.get("ttl_seconds") or 28_800),
                webspace_id=_webspace_id_from_payload(data),
            )
    return {
        "ok": True,
        "skill": "infra_access_skill",
        "topic": str(topic or ""),
        "handled": topic_token in {"infra_access.refresh", "desktop.webspace.refresh", "sys.ready", "infra_access.action"},
        "message": "infra_access_skill runtime entrypoint is available",
        "payload": data,
    }
