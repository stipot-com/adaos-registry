from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any, Mapping

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.context import clear_current_skill, set_current_skill
from adaos.sdk.data.events import publish
from adaos.sdk.io import stream_publish
from adaos.services.agent_context import get_ctx
from adaos.services.yjs.webspace import default_webspace_id

def _get_engine_class():
    from service.engine import NewFaceVisionEngine
    return NewFaceVisionEngine


SKILL_NAME = "new_face_vision_skill"
FRAME_RECEIVER = "newface_vision_frame"
METRICS_RECEIVER = "newface_vision_metrics"
PROGRESS_RECEIVER = "newface_vision_progress"
REQUIRES_DATA_PROJECTIONS = ["new_face_vision.current", "new_face_vision.history"]
_DATA_PROJECTION_ENTRIES = [
    {
        "scope": "subnet",
        "slot": "new_face_vision.current",
        "targets": [{"backend": "yjs", "path": "data/new_face_vision/current"}],
    },
    {
        "scope": "subnet",
        "slot": "new_face_vision.history",
        "targets": [{"backend": "yjs", "path": "data/new_face_vision/history"}],
    },
]
_log = logging.getLogger("skills.new_face_vision_skill")
_engine: Any = None


def _state_dir() -> Path:
    try:
        ctx = get_ctx()
        return Path(ctx.paths.state_dir()) / "skills" / SKILL_NAME
    except Exception:
        return Path(__file__).resolve().parents[1] / ".state"


def _engine_instance() -> Any:
    global _engine
    if _engine is None:
        engine_class = _get_engine_class()
        _engine = engine_class(_state_dir())
    return _engine


def _payload(evt_or_payload: Any) -> dict[str, Any]:
    payload = getattr(evt_or_payload, "payload", evt_or_payload)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _webspace_id_from_payload(payload: Mapping[str, Any] | None = None) -> str:
    if isinstance(payload, Mapping):
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
        token = str(
            payload.get("webspace_id")
            or payload.get("workspace_id")
            or meta.get("webspace_id")
            or meta.get("workspace_id")
            or ""
        ).strip()
        if token:
            return token
    return default_webspace_id()


def _ensure_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        if ctx.projections.resolve("subnet", "new_face_vision.current") and ctx.projections.resolve(
            "subnet", "new_face_vision.history"
        ):
            return
        ctx.projections.load_entries(_DATA_PROJECTION_ENTRIES)
    except Exception:
        _log.debug("projection entries are not available yet", exc_info=True)


def _project(webspace_id: str | None = None) -> None:
    _ensure_skill_data_projections()
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    snapshot = _engine_instance().snapshot()
    pushed = False
    try:
        try:
            pushed = bool(set_current_skill(SKILL_NAME))
        except Exception:
            pushed = False
        ctx_subnet.set("new_face_vision.current", snapshot, webspace_id=selected_webspace)
        ctx_subnet.set("new_face_vision.history", list(snapshot.get("history") or []), webspace_id=selected_webspace)
    except Exception:
        _log.debug("new_face_vision projection failed", exc_info=True)
    finally:
        if pushed:
            try:
                clear_current_skill()
            except Exception:
                pass


def _publish_event(topic: str, payload: dict[str, Any]) -> None:
    try:
        publish(topic, payload, source=SKILL_NAME)
    except Exception:
        _log.debug("failed to publish %s", topic, exc_info=True)


def _publish_stream(receiver: str, data: Any, *, webspace_id: str | None = None) -> None:
    selected_webspace = str(webspace_id or default_webspace_id()).strip() or default_webspace_id()
    try:
        stream_publish(receiver, data, _meta={"webspace_id": selected_webspace})
    except Exception:
        _log.debug("failed to publish stream receiver=%s", receiver, exc_info=True)


def _normalize_error_payload(
    error: Any,
    *,
    code: str = "skill_error",
    retryable: bool = False,
) -> dict[str, Any]:
    if isinstance(error, Mapping):
        message = str(error.get("message") or error.get("error") or error.get("code") or code)
        out: dict[str, Any] = {
            "code": str(error.get("code") or code),
            "message": message,
            "retryable": bool(error.get("retryable", retryable)),
            "ts": float(error.get("ts")) if isinstance(error.get("ts"), (int, float)) else time.time(),
        }
        if "details" in error:
            out["details"] = error.get("details")
        return out
    return {
        "code": code,
        "message": str(error or code),
        "retryable": retryable,
        "ts": time.time(),
    }


def _set_engine_error(error: Mapping[str, Any]) -> None:
    engine = _engine_instance()
    normalized = dict(error)
    engine.last_error = normalized
    operation = getattr(engine, "_operation", None)
    if isinstance(operation, dict):
        engine._operation = {**operation, "error": normalized}


def _progress_payload(
    snapshot: Mapping[str, Any],
    *,
    ok: bool,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    operation = snapshot.get("operation") if isinstance(snapshot.get("operation"), Mapping) else {}
    return {
        "ok": ok,
        "status": snapshot.get("status"),
        "operation": dict(operation),
        "error": dict(error) if isinstance(error, Mapping) else snapshot.get("error"),
        "ts": time.time(),
    }


def _artifact_path(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in (
            "path",
            "local_path",
            "file_path",
            "stored_path",
            "abs_path",
            "absolute_path",
        ):
            nested = value.get(key)
            if nested:
                return _artifact_path(nested)
        nested_ref = value.get("artifact_ref") or value.get("file") or value.get("value")
        if nested_ref:
            return _artifact_path(nested_ref)
        uri = str(value.get("uri") or value.get("url") or "").strip()
        if uri.startswith("file://"):
            return uri[len("file://") :]
        return ""
    text = str(value or "").strip()
    if text.startswith("file://"):
        return text[len("file://") :]
    return text


def _resolve_path(path: Any = None, artifact_ref: Any = None, file: Any = None, **payload: Any) -> str:
    candidates = [
        path,
        artifact_ref,
        file,
        payload.get("artifact"),
        payload.get("ref"),
        payload.get("value"),
    ]
    for candidate in candidates:
        resolved = _artifact_path(candidate)
        if resolved:
            return resolved
    return ""


def _source_ref(path: Any = None, artifact_ref: Any = None, file: Any = None, **payload: Any) -> dict[str, Any] | None:
    for candidate in (artifact_ref, file, payload.get("artifact"), payload.get("ref"), payload.get("value"), path):
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return None


def _result_with_snapshot(result: dict[str, Any], *, webspace_id: str | None = None) -> dict[str, Any]:
    ok = bool(result.get("ok", True))
    if not ok:
        result = {
            **result,
            "error": _normalize_error_payload(result.get("error"), code="operation_failed"),
        }
        _set_engine_error(result["error"])
    _project(webspace_id=webspace_id)
    snapshot = _engine_instance().snapshot()
    operation = snapshot.get("operation") if isinstance(snapshot.get("operation"), Mapping) else {}
    if operation.get("id") or not ok:
        _publish_stream(
            PROGRESS_RECEIVER,
            _progress_payload(snapshot, ok=ok, error=result.get("error") if not ok else None),
            webspace_id=webspace_id,
        )
    if not ok:
        _publish_event(
            "new_face_vision.error",
            {"ok": False, "error": result["error"], "ts": time.time()},
        )
    return {"ok": ok, **result, "current": snapshot}


def _handle_error(exc: Exception, *, webspace_id: str | None = None) -> dict[str, Any]:
    error = _normalize_error_payload(exc, code="handler_exception")
    _set_engine_error(error)
    _project(webspace_id=webspace_id)
    snapshot = _engine_instance().snapshot()
    payload = {"ok": False, "error": error, "current": snapshot, "ts": time.time()}
    _publish_event("new_face_vision.error", payload)
    _publish_stream(PROGRESS_RECEIVER, _progress_payload(snapshot, ok=False, error=error), webspace_id=webspace_id)
    return payload


@tool("new_face_vision_status")
def new_face_vision_status(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    _project(webspace_id=webspace_id)
    return {"ok": True, "current": _engine_instance().snapshot()}


@tool("new_face_vision_configure")
def new_face_vision_configure(
    model_path: str | None = None,
    frames_path: str | None = None,
    masks_path: str | None = None,
    metadata_path: str | None = None,
    threshold: float | None = None,
    warning_threshold: float | None = None,
    alarm_threshold: float | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        result = _engine_instance().configure(
            model_path=model_path,
            frames_path=frames_path,
            masks_path=masks_path,
            metadata_path=metadata_path,
            threshold=threshold,
            warning_threshold=warning_threshold,
            alarm_threshold=alarm_threshold,
        )
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_model")
def new_face_vision_load_model(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        result = _engine_instance().load_model(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_frames")
def new_face_vision_load_frames(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        result = _engine_instance().load_frames(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_masks")
def new_face_vision_load_masks(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        result = _engine_instance().load_masks(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_load_metadata")
def new_face_vision_load_metadata(
    path: Any = None,
    artifact_ref: Any = None,
    file: Any = None,
    webspace_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    try:
        resolved_path = _resolve_path(path, artifact_ref, file, **payload)
        result = _engine_instance().load_metadata(resolved_path, source_ref=_source_ref(path, artifact_ref, file, **payload))
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_process_frame")
def new_face_vision_process_frame(
    frame_idx: int | None = None,
    webspace_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    try:
        engine = _engine_instance()
        result = engine.process_frame(frame_idx)
        if result.get("ok"):
            _publish_stream(FRAME_RECEIVER, engine.frame_stream_payload(result), webspace_id=webspace_id)
            _publish_stream(METRICS_RECEIVER, engine.metrics_stream_payload(result), webspace_id=webspace_id)
        _publish_event("new_face_vision.frame", {k: v for k, v in result.items() if k != "preview_base64"})
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_play_step")
def new_face_vision_play_step(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    return new_face_vision_process_frame(frame_idx=None, webspace_id=webspace_id)


@tool("new_face_vision_reset")
def new_face_vision_reset(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        result = _engine_instance().reset()
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


@tool("new_face_vision_clear")
def new_face_vision_clear(webspace_id: str | None = None, **_: Any) -> dict[str, Any]:
    try:
        result = _engine_instance().clear()
        return _result_with_snapshot(result, webspace_id=webspace_id)
    except Exception as exc:
        return _handle_error(exc, webspace_id=webspace_id)


def _action_id(payload: Mapping[str, Any]) -> str:
    return str(payload.get("id") or payload.get("action") or "").strip()


def _action_value(payload: Mapping[str, Any]) -> str:
    value = payload.get("value")
    if value is None:
        value = payload.get("path")
    if isinstance(value, Mapping):
        for key in ("value", "path", "text"):
            nested = value.get(key)
            if nested:
                return str(nested).strip()
        return ""
    return str(value or "").strip()


@tool("new_face_vision_action")
def new_face_vision_action(id: str | None = None, value: Any = None, webspace_id: str | None = None, **payload: Any) -> dict[str, Any]:
    merged: dict[str, Any] = dict(payload)
    if id is not None:
        merged["id"] = id
    if value is not None:
        merged["value"] = value
    selected_webspace = webspace_id or _webspace_id_from_payload(merged)
    action = _action_id(merged)
    try:
        if action in {"", "refresh", "status"}:
            return new_face_vision_status(webspace_id=selected_webspace)
        if action in {"process_next", "play", "step"}:
            return new_face_vision_play_step(webspace_id=selected_webspace)
        if action in {"replay", "reset", "stop"}:
            return new_face_vision_reset(webspace_id=selected_webspace)
        if action == "clear":
            return new_face_vision_clear(webspace_id=selected_webspace)
        if action == "load_model":
            return new_face_vision_load_model(_action_value(merged), webspace_id=selected_webspace)
        if action == "load_frames":
            return new_face_vision_load_frames(_action_value(merged), webspace_id=selected_webspace)
        if action == "load_masks":
            return new_face_vision_load_masks(_action_value(merged), webspace_id=selected_webspace)
        if action == "load_metadata":
            return new_face_vision_load_metadata(_action_value(merged), webspace_id=selected_webspace)
        if action == "set_threshold":
            return new_face_vision_configure(threshold=float(_action_value(merged)), webspace_id=selected_webspace)
        return _result_with_snapshot(
            {
                "ok": False,
                "error": {
                    "code": "unknown_action",
                    "message": f"Unknown action: {action}",
                    "details": {"action": action},
                },
            },
            webspace_id=selected_webspace,
        )
    except Exception as exc:
        return _handle_error(exc, webspace_id=selected_webspace)


@subscribe("new_face_vision.action")
def on_new_face_vision_action(evt: Any) -> None:
    payload = _payload(evt)
    new_face_vision_action(**payload)


@subscribe("new_face_vision.status.refresh")
def on_new_face_vision_status_refresh(evt: Any) -> None:
    payload = _payload(evt)
    new_face_vision_status(webspace_id=_webspace_id_from_payload(payload))


@subscribe("sys.ready")
def on_sys_ready(evt: Any) -> None:
    payload = _payload(evt)
    webspace_id = _webspace_id_from_payload(payload)
    _project(webspace_id=webspace_id)
    _publish_event("new_face_vision.ready", {"ok": True, "webspace_id": webspace_id, "ts": time.time()})
