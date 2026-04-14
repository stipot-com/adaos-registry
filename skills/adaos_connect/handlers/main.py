from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import shlex
import time
from itertools import count
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import _expand_path, load_config
from adaos.services.root.client import RootHttpClient, RootHttpError
from adaos.services.root.service import RootAuthError, RootAuthService
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.zone_hosts import canonical_zone_id, zone_public_base_url

_log = logging.getLogger("skills.adaos_connect")

_APP_BASE_DEFAULT = "https://myinimatic.web.app"
_DEFAULT_ROOT_BASES = {"https://api.inimatic.com", "http://api.inimatic.com"}
_BROWSER_PAIR_TTL_S = 600
_TELEGRAM_PAIR_TTL_S = 600
_NODE_JOIN_CODE_TTL_S = 15 * 60
_prepare_request_counter = count(1)
_prepare_latest_request: dict[str, str] = {}
_prepare_tasks: dict[str, asyncio.Task[None]] = {}
_prepare_cache: dict[tuple[str, str], dict[str, Any]] = {}


def _payload(evt: Any) -> Dict[str, Any]:
    if hasattr(evt, "payload"):
        value = getattr(evt, "payload") or {}
        if isinstance(value, dict):
            return value
    if isinstance(evt, dict):
        return evt
    return {}


def _webspace_id(payload: Dict[str, Any]) -> str:
    direct = str(payload.get("webspace_id") or payload.get("workspace_id") or "").strip()
    if direct:
        return direct
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        hinted = str(meta.get("webspace_id") or meta.get("workspace_id") or "").strip()
        if hinted:
            return hinted
    return default_webspace_id()


def _zone_label(zone_id: str | None) -> str:
    token = str(zone_id or "").strip().upper()
    return token or "current zone"


def _zone_id_from_url(url: str | None) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        host = str(parsed.hostname or "").strip().lower()
    except Exception:
        return None
    if host == "ru.api.inimatic.com":
        return "ru"
    try:
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.strip().lower() != "zone":
                continue
            zone_id = canonical_zone_id(value)
            if zone_id:
                return zone_id
    except Exception:
        return None
    return None


def _current_zone_id(ctx: Any, cfg: Any) -> str | None:
    for candidate in (
        os.getenv("ADAOS_ZONE_ID"),
        os.getenv("ZONE_ID"),
        getattr(cfg, "zone_id", None),
        _zone_id_from_url(getattr(getattr(cfg, "root_settings", None), "base_url", None)),
        _zone_id_from_url(getattr(getattr(ctx, "settings", None), "api_base", None)),
    ):
        zone_id = canonical_zone_id(candidate)
        if zone_id:
            return zone_id
    return None


def _resolve_root_base_url(*, ctx: Any, cfg: Any, zone_id: str | None) -> str:
    base = str(getattr(getattr(cfg, "root_settings", None), "base_url", "") or getattr(ctx.settings, "api_base", "") or "").strip()
    if not base:
        base = "https://api.inimatic.com"
    base = base.rstrip("/")
    if zone_id and base in _DEFAULT_ROOT_BASES:
        return zone_public_base_url(zone_id)
    return base


def _app_base_url(ctx: Any) -> str:
    app_base = str(getattr(ctx.settings, "app_base", "") or "").strip().rstrip("/")
    if not app_base:
        return _APP_BASE_DEFAULT
    try:
        host = str(urlsplit(app_base).hostname or "").strip().lower()
    except Exception:
        host = ""
    if host == "app.inimatic.com":
        return _APP_BASE_DEFAULT
    return app_base


def _resolve_context() -> Dict[str, Any]:
    ctx = get_ctx()
    cfg = load_config(ctx=ctx)
    ca = _expand_path(cfg.root_settings.ca_cert, "keys/ca.cert")
    cert = _expand_path(cfg.subnet_settings.hub.cert, "keys/hub_cert.pem")
    key = _expand_path(cfg.subnet_settings.hub.key, "keys/hub_private.pem")
    verify: str | bool = True
    if ca.exists():
        verify = str(ca)
    cert_tuple = (str(cert), str(key)) if cert.exists() and key.exists() else None
    hub_id = str(getattr(cfg, "subnet_id", "") or "").strip()
    if not hub_id:
        hub_id = str(getattr(ctx.settings, "subnet_id", "") or getattr(ctx.settings, "default_hub", "") or "").strip()
    zone_id = _current_zone_id(ctx, cfg)
    root_base_url = _resolve_root_base_url(ctx=ctx, cfg=cfg, zone_id=zone_id)
    return {
        "cfg": cfg,
        "hub_id": hub_id,
        "zone_id": zone_id,
        "verify": verify,
        "cert_tuple": cert_tuple,
        "root_base_url": root_base_url,
        "app_base_url": _app_base_url(ctx),
    }


def _parse_expiry_epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        return stamp if stamp > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = float(text)
    except ValueError:
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    return stamp if stamp > 0 else None


def _format_expiry_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_expiry_display(epoch: float) -> str:
    return f"{datetime.fromtimestamp(epoch, tz=timezone.utc):%Y-%m-%d %H:%M:%S} UTC"


def _apply_expiry_fields(
    current: Dict[str, Any],
    *,
    expires_at: Any = None,
    fallback_ttl_seconds: int | None = None,
) -> Dict[str, Any]:
    epoch = _parse_expiry_epoch(expires_at)
    if epoch is None and fallback_ttl_seconds:
        epoch = time.time() + max(1, int(fallback_ttl_seconds))
    if epoch is None:
        current["expires_at"] = ""
        current["expires_at_display"] = ""
        current["expires_at_epoch"] = 0
        current["expires_in_seconds"] = 0
        return current
    current["expires_at"] = _format_expiry_iso(epoch)
    current["expires_at_display"] = _format_expiry_display(epoch)
    current["expires_at_epoch"] = int(epoch)
    current["expires_in_seconds"] = max(0, int(epoch - time.time()))
    return current


def _context_signature(context: Dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(context.get("hub_id") or ""),
        str(context.get("zone_id") or ""),
        str(context.get("root_base_url") or ""),
        str(context.get("app_base_url") or ""),
    )


def _cache_key(webspace_id: str, mode: str) -> tuple[str, str]:
    return (str(webspace_id or "").strip() or default_webspace_id(), str(mode or "browser").strip().lower() or "browser")


def _cache_current(webspace_id: str, mode: str, context: Dict[str, Any], current: Dict[str, Any]) -> None:
    expires_at_epoch = _parse_expiry_epoch(current.get("expires_at_epoch") or current.get("expires_at"))
    key = _cache_key(webspace_id, mode)
    if expires_at_epoch is None or expires_at_epoch <= time.time():
        _prepare_cache.pop(key, None)
        return
    _prepare_cache[key] = {
        "context_signature": _context_signature(context),
        "expires_at_epoch": expires_at_epoch,
        "current": dict(current),
    }


def _cached_current(webspace_id: str, mode: str, context: Dict[str, Any], *, request_id: str) -> Dict[str, Any] | None:
    key = _cache_key(webspace_id, mode)
    entry = _prepare_cache.get(key)
    if not isinstance(entry, dict):
        return None
    if entry.get("context_signature") != _context_signature(context):
        _prepare_cache.pop(key, None)
        return None
    expires_at_epoch = _parse_expiry_epoch(entry.get("expires_at_epoch"))
    if expires_at_epoch is None or expires_at_epoch <= time.time():
        _prepare_cache.pop(key, None)
        return None
    current = _base_current(mode)
    cached_payload = entry.get("current")
    if isinstance(cached_payload, dict):
        current.update(cached_payload)
    cached_zone_id = str(current.get("zone_id") or "").strip()
    _apply_expiry_fields(current, expires_at=expires_at_epoch)
    current = _decorate_current(current, context, request_id=request_id, status="ready", busy=False)
    if cached_zone_id:
        current["zone_id"] = cached_zone_id
    return current


def _root_client(context: Dict[str, Any], *, use_cert: bool) -> RootHttpClient:
    cert_tuple = context.get("cert_tuple") if use_cert else None
    return RootHttpClient(
        base_url=str(context.get("root_base_url") or "https://api.inimatic.com"),
        verify=context.get("verify", True),
        cert=cert_tuple if isinstance(cert_tuple, tuple) else None,
    )


def _browser_link(*, app_base_url: str, code: str, zone_id: str | None) -> str:
    parsed = urlsplit(app_base_url)
    query = {"pair_code": code}
    if zone_id:
        query["zone"] = zone_id
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def _browser_registration_link(
    *,
    verification_uri_complete: str | None,
    verification_uri: str | None,
    user_code: str,
    zone_id: str | None,
) -> str:
    complete = str(verification_uri_complete or "").strip()
    if complete:
        return complete
    base = str(verification_uri or "").strip()
    if not base:
        raise RuntimeError("browser registration link is empty")
    parsed = urlsplit(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if user_code and not str(query.get("user_code") or "").strip():
        query["user_code"] = user_code
    if zone_id and not str(query.get("zone") or "").strip():
        query["zone"] = zone_id
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def _linux_bootstrap_command(*, asset_base_url: str, code: str, root_base_url: str, zone_id: str | None) -> str:
    parts = [
        "curl -fsSL",
        shlex.quote(f"{asset_base_url}/assets/linux/init.sh"),
        "| bash -s --",
        "--join-code",
        shlex.quote(code),
    ]
    if root_base_url:
        parts.extend(["--root-url", shlex.quote(root_base_url)])
    if zone_id:
        parts.extend(["--zone", shlex.quote(zone_id)])
    return " ".join(parts)


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _cmd_quote(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _windows_ps_bootstrap_command(*, asset_base_url: str, code: str, root_base_url: str, zone_id: str | None) -> str:
    parts = [
        f"iwr -UseBasicParsing {_powershell_quote(f'{asset_base_url}/assets/windows/init.ps1')} -OutFile init.ps1;",
        f".\\init.ps1 -JoinCode {_powershell_quote(code)}",
    ]
    if root_base_url:
        parts.append(f"-RootUrl {_powershell_quote(root_base_url)}")
    if zone_id:
        parts.append(f"-ZoneId {_powershell_quote(zone_id)}")
    return " ".join(parts)


def _windows_cmd_bootstrap_command(*, asset_base_url: str, code: str, root_base_url: str, zone_id: str | None) -> str:
    ps_download = (
        "$ProgressPreference='SilentlyContinue'; "
        f"Invoke-WebRequest -UseBasicParsing {_powershell_quote(f'{asset_base_url}/assets/windows/init.ps1')} "
        "-OutFile '.\\init.ps1'"
    )
    parts = [
        "powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command",
        _cmd_quote(ps_download),
        "&&",
        "powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\\init.ps1",
        "-JoinCode",
        _cmd_quote(code),
    ]
    if root_base_url:
        parts.extend(["-RootUrl", _cmd_quote(root_base_url)])
    if zone_id:
        parts.extend(["-ZoneId", _cmd_quote(zone_id)])
    return " ".join(parts)


async def _write_current(webspace_id: str, current: Dict[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        current_root = data_map.get("adaos_connect") or {}
        if isinstance(current_root, dict):
            next_root = dict(current_root)
        else:
            items = getattr(current_root, "items", None)
            next_root = dict(items()) if callable(items) else {}
        next_root["current"] = current
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "adaos_connect", next_root)


def _base_current(mode: str) -> Dict[str, Any]:
    return {
        "mode": mode,
        "status": "idle",
        "busy": False,
        "request_id": "",
        "zone_id": "",
        "subnet_id": "",
        "root_base_url": "",
        "app_base_url": "",
        "summary": "Preparing connection data...",
        "summary_language": "text",
        "expires_at": "",
        "expires_at_display": "",
        "expires_at_language": "text",
        "expires_at_epoch": 0,
        "expires_in_seconds": 0,
        "qr_text": "",
        "link": "",
        "link_language": "text",
        "code": "",
        "code_language": "text",
        "linux_command": "",
        "linux_language": "bash",
        "windows_ps_command": "",
        "windows_ps_language": "powershell",
        "windows_cmd_command": "",
        "windows_cmd_language": "bat",
    }


def _decorate_current(current: Dict[str, Any], context: Dict[str, Any], *, request_id: str, status: str, busy: bool) -> Dict[str, Any]:
    current["request_id"] = request_id
    current["status"] = status
    current["busy"] = busy
    current["zone_id"] = str(context.get("zone_id") or "")
    current["subnet_id"] = str(context.get("hub_id") or "")
    current["root_base_url"] = str(context.get("root_base_url") or "")
    current["app_base_url"] = str(context.get("app_base_url") or "")
    return current


def _pending_current(mode: str, context: Dict[str, Any], *, request_id: str) -> Dict[str, Any]:
    current = _decorate_current(_base_current(mode), context, request_id=request_id, status="pending", busy=True)
    hub_id = str(context.get("hub_id") or "").strip()
    zone_id = str(context.get("zone_id") or "").strip()
    if mode == "browser":
        zone_text = f" for zone {_zone_label(zone_id)}" if zone_id else ""
        current["summary"] = f"Preparing browser pairing data{zone_text}. This can take a few seconds."
    elif mode == "telegram":
        subnet_text = f" for subnet {hub_id}" if hub_id else ""
        zone_text = f" in zone {_zone_label(zone_id)}" if zone_id else ""
        current["summary"] = f"Preparing Telegram connection data{subnet_text}{zone_text}. This can take a few seconds."
    elif mode == "node":
        subnet_text = f" for subnet {hub_id}" if hub_id else ""
        zone_text = f" in zone {_zone_label(zone_id)}" if zone_id else ""
        current["summary"] = f"Generating a node join code{subnet_text}{zone_text}. This can take a few seconds."
    else:
        current["summary"] = "Preparing connection data. This can take a few seconds."
    return current


def _browser_current(context: Dict[str, Any], *, request_id: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "owner_id": str(context.get("hub_id") or "").strip() or "local-owner",
    }
    if context.get("zone_id"):
        payload["zone_id"] = context["zone_id"]
    data: Dict[str, Any] | None = None
    try:
        data = _root_client(context, use_cert=False).device_authorize(payload=payload)
    except RootHttpError as exc:
        if exc.status_code not in (401, 403) or not context.get("cert_tuple"):
            raise
    if data is None:
        data = _root_client(context, use_cert=True).device_authorize(payload=payload)
    code = str((data or {}).get("user_code") or (data or {}).get("user_code_short") or "").strip()
    if not code:
        raise RuntimeError("browser registration code is empty")
    current = _decorate_current(_base_current("browser"), context, request_id=request_id, status="ready", busy=False)
    effective_zone_id = (
        canonical_zone_id((data or {}).get("zone_id"))
        or _zone_id_from_url((data or {}).get("verification_uri_complete"))
        or _zone_id_from_url((data or {}).get("verify_uri") or (data or {}).get("verification_uri"))
        or str(context.get("zone_id") or "").strip()
        or None
    )
    if effective_zone_id:
        current["zone_id"] = effective_zone_id
    _apply_expiry_fields(
        current,
        expires_at=(data or {}).get("expires_at"),
        fallback_ttl_seconds=(data or {}).get("expires_in") or _BROWSER_PAIR_TTL_S,
    )
    link = _browser_registration_link(
        verification_uri_complete=(data or {}).get("verification_uri_complete"),
        verification_uri=(data or {}).get("verify_uri") or (data or {}).get("verification_uri"),
        user_code=code,
        zone_id=effective_zone_id,
    )
    subnet_text = f" for subnet {payload['owner_id']}" if payload["owner_id"] else ""
    zone_text = f" in zone {_zone_label(effective_zone_id)}" if effective_zone_id else ""
    current["summary"] = (
        "Open the link or scan the QR code to register a new browser"
        f"{subnet_text}{zone_text}. Use the code below if the registration page asks for it."
    )
    current["qr_text"] = link
    current["link"] = link
    current["code"] = code
    return current


def _telegram_current(context: Dict[str, Any], *, request_id: str) -> Dict[str, Any]:
    hub_id = str(context.get("hub_id") or "").strip()
    if not hub_id:
        raise RuntimeError("hub_id is not available")
    data = _root_client(context, use_cert=False).request(
        "POST",
        "/io/tg/pair/create",
        json={"hub_id": hub_id, "ttl": _TELEGRAM_PAIR_TTL_S},
    )
    code = str((data or {}).get("pair_code") or (data or {}).get("code") or "").strip()
    if not code:
        raise RuntimeError("telegram pair code is empty")
    deep_link = str((data or {}).get("deep_link") or "").strip() or f"https://t.me/adaos_home_bot?start={code}"
    current = _decorate_current(_base_current("telegram"), context, request_id=request_id, status="ready", busy=False)
    _apply_expiry_fields(current, expires_at=(data or {}).get("expires_at"), fallback_ttl_seconds=_TELEGRAM_PAIR_TTL_S)
    zone_text = f" in zone {_zone_label(context.get('zone_id'))}" if context.get("zone_id") else ""
    current["summary"] = f"Scan this QR code to open the Telegram join link for subnet {hub_id}{zone_text}."
    current["qr_text"] = deep_link
    current["link"] = deep_link
    current["code"] = code
    return current


def _root_token_value() -> str:
    return (
        os.getenv("HUB_ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_OWNER_TOKEN")
        or ""
    ).strip()


def _request_join_code(context: Dict[str, Any]) -> Dict[str, Any]:
    hub_id = str(context.get("hub_id") or "").strip()
    if not hub_id:
        raise RuntimeError("hub_id is not available")
    payload = {"subnet_id": hub_id, "ttl_minutes": _NODE_JOIN_CODE_TTL_S // 60, "length": 8}
    mtls_error: RootHttpError | None = None
    data: Any | None = None

    if context.get("cert_tuple"):
        try:
            data = _root_client(context, use_cert=True).request("POST", "/v1/subnets/join-code", json=payload)
        except RootHttpError as exc:
            if exc.status_code in (401, 403):
                mtls_error = exc
            else:
                raise

    if data is None:
        root_token = _root_token_value()
        if root_token:
            try:
                data = _root_client(context, use_cert=False).request(
                    "POST",
                    "/v1/subnets/join-code",
                    json=payload,
                    headers={"X-Root-Token": root_token},
                )
            except RootHttpError as exc:
                if exc.status_code not in (401, 403):
                    raise
        if data is None:
            try:
                access_token = RootAuthService(http=_root_client(context, use_cert=False)).get_access_token(context["cfg"])
            except RootAuthError as exc:
                if mtls_error is not None:
                    raise RuntimeError(
                        "Root rejected hub authentication for join-code generation. Configure ROOT_TOKEN or run adaos dev root login."
                    ) from exc
                raise
            data = _root_client(context, use_cert=False).request(
                "POST",
                "/v1/subnets/join-code",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )

    if not isinstance(data, dict):
        raise RuntimeError("join-code response is invalid")
    return data


def _node_current(context: Dict[str, Any], *, request_id: str) -> Dict[str, Any]:
    hub_id = str(context.get("hub_id") or "").strip()
    data = _request_join_code(context)
    code = str((data or {}).get("code") or "").strip()
    if not code:
        raise RuntimeError("join code is empty")
    current = _decorate_current(_base_current("node"), context, request_id=request_id, status="ready", busy=False)
    _apply_expiry_fields(
        current,
        expires_at=(data or {}).get("expires_at_utc") or (data or {}).get("expires_at"),
        fallback_ttl_seconds=_NODE_JOIN_CODE_TTL_S,
    )
    zone_id = str(context.get("zone_id") or "").strip() or None
    zone_text = f" in zone {_zone_label(zone_id)}" if zone_id else ""
    current["summary"] = f"Use the generated join code on Linux or Windows to add a node to subnet {hub_id}{zone_text}."
    current["code"] = code
    current["linux_command"] = _linux_bootstrap_command(
        asset_base_url=str(context.get("app_base_url") or _APP_BASE_DEFAULT),
        code=code,
        root_base_url=str(context.get("root_base_url") or ""),
        zone_id=zone_id,
    )
    current["windows_ps_command"] = _windows_ps_bootstrap_command(
        asset_base_url=str(context.get("app_base_url") or _APP_BASE_DEFAULT),
        code=code,
        root_base_url=str(context.get("root_base_url") or ""),
        zone_id=zone_id,
    )
    current["windows_cmd_command"] = _windows_cmd_bootstrap_command(
        asset_base_url=str(context.get("app_base_url") or _APP_BASE_DEFAULT),
        code=code,
        root_base_url=str(context.get("root_base_url") or ""),
        zone_id=zone_id,
    )
    return current


def _prepare_current(mode: str, context: Dict[str, Any], *, request_id: str) -> Dict[str, Any]:
    if mode == "browser":
        return _browser_current(context, request_id=request_id)
    if mode == "telegram":
        return _telegram_current(context, request_id=request_id)
    if mode == "node":
        return _node_current(context, request_id=request_id)
    raise RuntimeError(f"unsupported mode: {mode}")


def _next_request_id(webspace_id: str) -> str:
    request_id = f"{webspace_id}:{next(_prepare_request_counter)}"
    _prepare_latest_request[webspace_id] = request_id
    return request_id


def _is_current_request(webspace_id: str, request_id: str) -> bool:
    return _prepare_latest_request.get(webspace_id) == request_id


async def _finish_prepare(mode: str, webspace_id: str, request_id: str, context: Dict[str, Any]) -> None:
    try:
        current = await asyncio.to_thread(_prepare_current, mode, context, request_id=request_id)
    except Exception as exc:
        _log.warning("adaos_connect.prepare failed mode=%s webspace=%s", mode, webspace_id, exc_info=True)
        current = _decorate_current(_base_current(mode or "browser"), context, request_id=request_id, status="error", busy=False)
        current["summary"] = f"Failed to prepare connection data: {exc}"
    else:
        if current.get("status") == "ready":
            _cache_current(webspace_id, mode, context, current)
    if not _is_current_request(webspace_id, request_id):
        return
    await _write_current(webspace_id, current)


@subscribe("adaos_connect.prepare")
async def on_prepare(evt: Any) -> None:
    payload = _payload(evt)
    mode = str(payload.get("mode") or "browser").strip().lower() or "browser"
    webspace_id = _webspace_id(payload)
    context = _resolve_context()
    request_id = _next_request_id(webspace_id)
    refresh = bool(payload.get("refresh") or payload.get("force_new") or payload.get("renew"))
    if not refresh:
        cached = _cached_current(webspace_id, mode, context, request_id=request_id)
        if cached is not None:
            await _write_current(webspace_id, cached)
            return
    await _write_current(webspace_id, _pending_current(mode, context, request_id=request_id))
    task = asyncio.create_task(
        _finish_prepare(mode, webspace_id, request_id, context),
        name=f"adaos-connect-prepare:{webspace_id}:{mode}",
    )
    _prepare_tasks[webspace_id] = task

    def _cleanup(done: asyncio.Task[None], *, ws: str) -> None:
        if _prepare_tasks.get(ws) is done:
            _prepare_tasks.pop(ws, None)

    task.add_done_callback(lambda done, ws=webspace_id: _cleanup(done, ws=ws))
