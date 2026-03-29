from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

from adaos.sdk.core.decorators import tool
from adaos.services.agent_context import get_ctx
from adaos.services.root.client import RootHttpClient

_log = logging.getLogger("skills.root_mgmnt")
_CACHE_TTL_S = float(str(os.getenv("ADAOS_ROOT_MGMNT_CACHE_TTL") or "5").strip() or "5")
_SNAPSHOT_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}


def lang_res() -> Dict[str, str]:
    return {}


def _root_base_url() -> str:
    for env_name in (
        "ADAOS_ROOT_MGMNT_BASE_URL",
        "ROOT_MGMNT_BASE_URL",
        "ADAOS_ROOT_API_BASE",
        "ROOT_API_BASE",
    ):
        raw = str(os.getenv(env_name) or "").strip().rstrip("/")
        if raw:
            return raw
    try:
        ctx = get_ctx()
        api_base = str(getattr(getattr(ctx, "settings", None), "api_base", "") or "").strip().rstrip("/")
        if api_base:
            return api_base
    except Exception:
        pass
    proto = str(os.getenv("ROOT_SERVER_PROTO") or os.getenv("SERVER_PROTO") or "http").strip().lower() or "http"
    host = str(os.getenv("ROOT_MGMNT_LOCAL_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = str(os.getenv("PORT") or "3030").strip() or "3030"
    return f"{proto}://{host}:{port}"


def _root_verify(base_url: str) -> str | bool:
    raw = str(os.getenv("ROOT_MGMNT_VERIFY") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    try:
        ctx = get_ctx()
        pki = getattr(getattr(ctx, "settings", None), "pki", None)
        ca_path = str(getattr(pki, "ca", "") or "").strip()
        if ca_path and os.path.exists(ca_path):
            return ca_path
    except Exception:
        pass
    if base_url.startswith("https://127.0.0.1") or base_url.startswith("https://localhost"):
        return False
    return True


def _root_token() -> str:
    return str(os.getenv("ROOT_MGMNT_TOKEN") or os.getenv("ROOT_TOKEN") or "dev-root-token").strip() or "dev-root-token"


def _client() -> RootHttpClient:
    base_url = _root_base_url()
    return RootHttpClient(
        base_url=base_url,
        verify=_root_verify(base_url),
        timeout=30.0,
        default_headers={
            "X-Root-Mgmnt-Token": _root_token(),
            "X-Root-Mgmnt-Actor": "root_mgmnt.skill",
        },
    )


def _invalidate_cache() -> None:
    _SNAPSHOT_CACHE["ts"] = 0.0
    _SNAPSHOT_CACHE["value"] = None


def _snapshot(force: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = _SNAPSHOT_CACHE.get("value")
    cached_ts = float(_SNAPSHOT_CACHE.get("ts") or 0.0)
    if not force and isinstance(cached, dict) and (now - cached_ts) < _CACHE_TTL_S:
        return dict(cached)
    payload = _client().request("GET", "/v1/root_mgmnt/snapshot")
    snapshot = dict(payload) if isinstance(payload, Mapping) else {"ok": False, "error": "invalid_snapshot"}
    _SNAPSHOT_CACHE["ts"] = now
    _SNAPSHOT_CACHE["value"] = snapshot
    return dict(snapshot)


def _snapshot_or_fallback(force: bool = False) -> dict[str, Any]:
    try:
        return _snapshot(force=force)
    except Exception as exc:
        _log.warning("root_mgmnt snapshot failed", exc_info=True)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "overview": {},
            "policy": {},
            "fleet": [],
            "lifecycle_candidates": [],
            "audit": [],
        }


def _fleet(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = snapshot.get("fleet")
    return [dict(item) for item in items] if isinstance(items, list) else []


def _lifecycle_candidates(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = snapshot.get("lifecycle_candidates")
    return [dict(item) for item in items] if isinstance(items, list) else []


def _audit(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = snapshot.get("audit")
    return [dict(item) for item in items] if isinstance(items, list) else []


def _policy(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    policy = snapshot.get("policy")
    return dict(policy) if isinstance(policy, Mapping) else {}


def _overview(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    overview = snapshot.get("overview")
    return dict(overview) if isinstance(overview, Mapping) else {}


def _find_subnet(snapshot: Mapping[str, Any], subnet_id: str | None) -> dict[str, Any] | None:
    target = str(subnet_id or "").strip()
    if not target:
        return None
    for item in _fleet(snapshot):
        if str(item.get("subnet_id") or "").strip() == target:
            return item
    return None


def _metric_value(snapshot: Mapping[str, Any], metric_id: str) -> dict[str, Any]:
    overview = _overview(snapshot)
    policy = _policy(snapshot)
    total = int(overview.get("total_subnets") or 0)
    live = int(overview.get("live_subnets") or 0)
    dormant = int(overview.get("dormant_subnets") or 0)
    retirees = int(overview.get("retire_candidates") or 0)
    archive = int(overview.get("archive_candidates") or 0)
    requests_24h = int(overview.get("llm_requests_24h") or 0)
    denied_30d = int(overview.get("llm_denied_30d") or 0)
    if metric_id == "fleet_total":
        return {
            "value": total,
            "label": "Registered subnets",
            "subtitle": f"{live} live / {dormant} dormant",
            "description": f"{archive} still keep forge artifacts.",
        }
    if metric_id == "retire_candidates":
        return {
            "value": retirees,
            "label": "Retire candidates",
            "subtitle": "Lifecycle queue",
            "description": "Subnets with long inactivity and no recent LLM traffic.",
        }
    if metric_id == "llm_requests_24h":
        return {
            "value": requests_24h,
            "label": "LLM requests / 24h",
            "subtitle": f"mode={policy.get('access_mode') or 'open'}",
            "description": f"default model: {policy.get('default_model') or 'gpt-4o-mini'}",
        }
    if metric_id == "llm_policy":
        enabled = bool(policy.get("llm_enabled", True))
        return {
            "value": "ON" if enabled else "OFF",
            "label": str(policy.get("access_mode") or "open"),
            "subtitle": "Root LLM policy",
            "description": f"{denied_30d} denied requests over the last 30 days.",
        }
    return {
        "value": "n/a",
        "label": metric_id,
        "subtitle": "unknown metric",
        "description": "Metric is not configured.",
    }


def _policy_summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    policy = _policy(snapshot)
    overview = _overview(snapshot)
    return {
        "generated_at": snapshot.get("generated_at"),
        "llm_enabled": bool(policy.get("llm_enabled", True)),
        "access_mode": str(policy.get("access_mode") or "open"),
        "default_model": str(policy.get("default_model") or "gpt-4o-mini"),
        "allowed_models": list(policy.get("allowed_models") or []),
        "allowed_subnets": list(policy.get("allowed_subnets") or []),
        "fleet_overview": overview,
        "top_retire_candidates": _lifecycle_candidates(snapshot)[:5],
    }


def _subnet_details(snapshot: Mapping[str, Any], subnet_id: str | None) -> dict[str, Any]:
    item = _find_subnet(snapshot, subnet_id)
    if not item:
        return {
            "hint": "Select a subnet from Fleet or Lifecycle.",
            "generated_at": snapshot.get("generated_at"),
            "policy": _policy(snapshot),
        }
    return {
        "subnet_id": item.get("subnet_id"),
        "owner_id": item.get("owner_id"),
        "owner_revoked": item.get("owner_revoked"),
        "lifecycle_state": item.get("lifecycle_state"),
        "auto_state": item.get("auto_state"),
        "llm_access": item.get("llm_access"),
        "activity_score": item.get("activity_score"),
        "live_now": item.get("live_now"),
        "last_seen": item.get("last_seen"),
        "last_seen_at": item.get("last_seen_at"),
        "idle_days": item.get("idle_days"),
        "llm": {
            "requests_24h": item.get("llm_requests_24h"),
            "requests_7d": item.get("llm_requests_7d"),
            "requests_30d": item.get("llm_requests_30d"),
            "denied_30d": item.get("llm_denied_30d"),
            "last_model": item.get("llm_last_model"),
            "last_seen_at": item.get("llm_last_seen_at"),
        },
        "forge": {
            "dev_nodes": item.get("dev_nodes"),
            "draft_artifacts": item.get("draft_artifacts"),
            "registry_artifacts": item.get("registry_artifacts"),
            "uploads": item.get("uploads"),
        },
        "candidate_reason": item.get("candidate_reason"),
        "note": item.get("note"),
        "policy_mode": _policy(snapshot).get("access_mode"),
        "generated_at": snapshot.get("generated_at"),
    }


def _subnet_action(subnet_id: str, action: str, note: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": action}
    if note:
        payload["note"] = note
    result = _client().request("POST", f"/v1/root_mgmnt/subnets/{quote(subnet_id.strip(), safe='')}/action", json=payload)
    _invalidate_cache()
    return dict(result) if isinstance(result, Mapping) else {"ok": True, "action": action, "subnet_id": subnet_id}


def _policy_update(**payload: Any) -> dict[str, Any]:
    result = _client().request("POST", "/v1/root_mgmnt/policy", json=payload)
    _invalidate_cache()
    return dict(result) if isinstance(result, Mapping) else {"ok": True, "policy": payload}


@tool("get_snapshot")
def get_snapshot(force: bool = False) -> dict[str, Any]:
    return _snapshot_or_fallback(force=bool(force))


@tool("refresh_snapshot")
def refresh_snapshot() -> dict[str, Any]:
    return _snapshot_or_fallback(force=True)


@tool("get_metric_tile")
def get_metric_tile(metric_id: str) -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=False)
    return _metric_value(snapshot, str(metric_id or "").strip())


@tool("get_policy_summary")
def get_policy_summary() -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=False)
    return _policy_summary(snapshot)


@tool("get_fleet")
def get_fleet() -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=False)
    items = sorted(
        _fleet(snapshot),
        key=lambda item: (
            0 if str(item.get("live_now") or "") == "yes" else 1,
            -(int(item.get("activity_score") or 0)),
            str(item.get("subnet_id") or ""),
        ),
    )
    return {"items": items}


@tool("get_lifecycle_candidates")
def get_lifecycle_candidates() -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=False)
    items = sorted(
        _lifecycle_candidates(snapshot),
        key=lambda item: (-(int(item.get("idle_days") or 0)), str(item.get("subnet_id") or "")),
    )
    return {"items": items}


@tool("get_audit_events")
def get_audit_events() -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=False)
    return {"items": _audit(snapshot)}


@tool("get_subnet_details")
def get_subnet_details(subnet_id: Optional[str] = None) -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=False)
    return _subnet_details(snapshot, subnet_id)


@tool("freeze_subnet_llm")
def freeze_subnet_llm(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "freeze_llm", note=note)


@tool("unfreeze_subnet_llm")
def unfreeze_subnet_llm(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "unfreeze_llm", note=note)


@tool("mark_dormant")
def mark_dormant(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "mark_dormant", note=note)


@tool("reactivate_subnet")
def reactivate_subnet(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "reactivate", note=note)


@tool("archive_dev_space")
def archive_dev_space(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "archive_dev_space", note=note)


@tool("retire_subnet")
def retire_subnet(subnet_id: str, note: str | None = None) -> dict[str, Any]:
    return _subnet_action(subnet_id, "retire_subnet", note=note)


@tool("set_policy_mode")
def set_policy_mode(mode: str) -> dict[str, Any]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"open", "allowlist", "denyall"}:
        raise ValueError("mode must be one of: open, allowlist, denyall")
    return _policy_update(access_mode=normalized)


@tool("set_llm_enabled")
def set_llm_enabled(enabled: bool) -> dict[str, Any]:
    return _policy_update(llm_enabled=bool(enabled))


@tool("allow_subnet")
def allow_subnet(subnet_id: str) -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=True)
    policy = _policy(snapshot)
    allowed = {str(item).strip() for item in policy.get("allowed_subnets") or [] if str(item).strip()}
    allowed.add(str(subnet_id or "").strip())
    return _policy_update(allowed_subnets=sorted(allowed))


@tool("remove_allowed_subnet")
def remove_allowed_subnet(subnet_id: str) -> dict[str, Any]:
    snapshot = _snapshot_or_fallback(force=True)
    policy = _policy(snapshot)
    allowed = [str(item).strip() for item in policy.get("allowed_subnets") or [] if str(item).strip()]
    filtered = [item for item in allowed if item != str(subnet_id or "").strip()]
    return _policy_update(allowed_subnets=filtered)
