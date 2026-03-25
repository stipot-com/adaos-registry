from __future__ import annotations

import logging
from typing import Any, Dict

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import _expand_path, load_config
from adaos.services.root.client import RootHttpClient
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("skills.adaos_connect")


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


def _root_client() -> tuple[RootHttpClient, str]:
    ctx = get_ctx()
    cfg = load_config(ctx=ctx)
    ca = _expand_path(cfg.root_settings.ca_cert, "keys/ca.cert")
    cert = _expand_path(cfg.subnet_settings.hub.cert, "keys/hub_cert.pem")
    key = _expand_path(cfg.subnet_settings.hub.key, "keys/hub_private.pem")
    verify: str | bool = True
    if ca.exists():
        verify = str(ca)
    cert_tuple = (str(cert), str(key)) if cert.exists() and key.exists() else None
    base = cfg.root_settings.base_url or ctx.settings.api_base
    hub_id = str(getattr(cfg, "subnet_id", "") or "").strip()
    if not hub_id:
        hub_id = str(getattr(ctx.settings, "subnet_id", "") or getattr(ctx.settings, "default_hub", "") or "").strip()
    return RootHttpClient(base_url=base, verify=verify, cert=cert_tuple), hub_id


def _app_base_url() -> str:
    ctx = get_ctx()
    app_base = str(getattr(ctx.settings, "app_base", "") or "").strip()
    if app_base:
        return app_base.rstrip("/")
    return "https://app.inimatic.com"


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
        "summary": "Preparing connection data...",
        "summary_language": "text",
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


def _browser_current() -> Dict[str, Any]:
    client, _hub_id = _root_client()
    data = client.request("POST", "/v1/browser/pair/create", json={"ttl": 600})
    code = str((data or {}).get("pair_code") or "").strip()
    if not code:
        raise RuntimeError("browser pair code is empty")
    current = _base_current("browser")
    link = f"{_app_base_url()}/?pair_code={code}"
    current["summary"] = "Scan this QR code to connect another browser to the current web workspace."
    current["qr_text"] = link
    current["link"] = link
    current["code"] = code
    return current


def _telegram_current(client: RootHttpClient, hub_id: str) -> Dict[str, Any]:
    data = client.request("POST", "/io/tg/pair/create", json={"hub_id": hub_id, "ttl": 600})
    code = str((data or {}).get("pair_code") or (data or {}).get("code") or "").strip()
    if not code:
        raise RuntimeError("telegram pair code is empty")
    deep_link = str((data or {}).get("deep_link") or "").strip() or f"https://t.me/adaos_home_bot?start={code}"
    current = _base_current("telegram")
    current["summary"] = "Scan this QR code to open the Telegram join link for the current hub."
    current["qr_text"] = deep_link
    current["link"] = deep_link
    current["code"] = code
    return current


def _node_current(client: RootHttpClient, hub_id: str) -> Dict[str, Any]:
    data = client.request(
        "POST",
        "/v1/subnets/join-code",
        json={"subnet_id": hub_id, "ttl_minutes": 15, "length": 8},
    )
    code = str((data or {}).get("code") or "").strip()
    current = _base_current("node")
    current["summary"] = "Use the generated join code on Linux or Windows to add a node to the current subnet."
    current["code"] = code
    current["linux_command"] = f"curl -fsSL https://app.inimatic.com/assets/linux/init.sh | bash -s -- --join-code {code}"
    current["windows_ps_command"] = f"iwr -UseBasicParsing https://app.inimatic.com/assets/windows/init.ps1 | iex; init.ps1 -JoinCode {code}"
    current["windows_cmd_command"] = f"curl -fsSL -o init.bat https://app.inimatic.com/assets/windows/init.bat && init.bat -JoinCode {code}"
    return current


@subscribe("adaos_connect.prepare")
async def on_prepare(evt: Any) -> None:
    payload = _payload(evt)
    mode = str(payload.get("mode") or "browser").strip().lower()
    webspace_id = _webspace_id(payload)
    try:
        if mode == "browser":
            current = _browser_current()
        else:
            client, hub_id = _root_client()
            if not hub_id:
                raise RuntimeError("hub_id is not available")
            if mode == "telegram":
                current = _telegram_current(client, hub_id)
            elif mode == "node":
                current = _node_current(client, hub_id)
            else:
                raise RuntimeError(f"unsupported mode: {mode}")
    except Exception as exc:
        _log.warning("adaos_connect.prepare failed mode=%s webspace=%s", mode, webspace_id, exc_info=True)
        current = _base_current(mode or "browser")
        current["summary"] = f"Failed to prepare connection data: {exc}"
    await _write_current(webspace_id, current)
