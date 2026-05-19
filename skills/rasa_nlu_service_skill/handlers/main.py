from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_RUNTIME: Any | None = None
_MODEL_PATH: Path | None = None


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except Exception:
        length = 0
    raw = handler.rfile.read(max(length, 0)) if length > 0 else b"{}"
    try:
        payload = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _ensure_rasa_port_importable() -> None:
    try:
        import adaos_rasa_nlu  # noqa: F401

        return
    except Exception:
        pass

    candidates: list[Path] = []
    env_path = os.getenv("ADAOS_RASA_PORT_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))

    package_dir = os.getenv("ADAOS_PACKAGE_DIR", "").strip()
    if package_dir:
        candidates.append(Path(package_dir).expanduser().resolve() / "integrations" / "rasa-port")

    repo_root = os.getenv("ADAOS_REPO_ROOT", "").strip()
    if repo_root:
        candidates.append(Path(repo_root).expanduser().resolve() / "src" / "adaos" / "integrations" / "rasa-port")

    try:
        import adaos

        candidates.append(Path(adaos.__file__).resolve().parent / "integrations" / "rasa-port")
    except Exception:
        pass

    for candidate in candidates:
        if candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            try:
                import adaos_rasa_nlu  # noqa: F401

                return
            except Exception:
                continue


def _default_models_dir() -> Path:
    explicit = os.getenv("ADAOS_MODELS_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve() / "interpreter"
    base_dir = os.getenv("ADAOS_BASE_DIR", "").strip()
    if base_dir:
        return Path(base_dir).expanduser().resolve() / "models" / "interpreter"
    return Path.home() / ".adaos" / "models" / "interpreter"


def _latest_model_path() -> Path | None:
    candidates: list[Path] = []
    env_model = os.getenv("ADAOS_RASA_MODEL_PATH", "").strip()
    if env_model:
        candidates.append(Path(env_model).expanduser().resolve())
    if _MODEL_PATH:
        candidates.append(_MODEL_PATH)

    models_dir = _default_models_dir()
    candidates.append(models_dir / "interpreter_latest.tar.gz")
    try:
        candidates.extend(sorted(models_dir.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True))
    except Exception:
        pass

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _load_runtime(model_path: Path) -> Any:
    global _RUNTIME, _MODEL_PATH

    with _LOCK:
        if _RUNTIME is not None and _MODEL_PATH == model_path:
            return _RUNTIME
        if _RUNTIME is not None and hasattr(_RUNTIME, "close"):
            try:
                _RUNTIME.close()
            except Exception:
                pass
        _ensure_rasa_port_importable()
        from adaos_rasa_nlu import load_model

        _RUNTIME = load_model(model_path)
        _MODEL_PATH = model_path
        return _RUNTIME


def _train(payload: dict[str, Any]) -> dict[str, Any]:
    project_dir = payload.get("project_dir")
    out_dir = payload.get("out_dir")
    fixed_model_name = str(payload.get("fixed_model_name") or "interpreter_latest").strip() or "interpreter_latest"
    if not isinstance(project_dir, str) or not project_dir.strip():
        return {"ok": False, "error": "project_dir_required"}
    if not isinstance(out_dir, str) or not out_dir.strip():
        return {"ok": False, "error": "out_dir_required"}

    _ensure_rasa_port_importable()
    from adaos_rasa_nlu import train_nlu

    result = train_nlu(
        Path(project_dir).expanduser().resolve(),
        Path(out_dir).expanduser().resolve(),
        fixed_model_name=fixed_model_name,
    )
    model_path = result.model_path.resolve()
    _load_runtime(model_path)
    return {"ok": True, "model_path": str(model_path)}


def _parse(payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("text") or payload.get("utterance")
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text_required"}

    requested_model = payload.get("model_path")
    model_path = (
        Path(requested_model).expanduser().resolve()
        if isinstance(requested_model, str) and requested_model.strip()
        else _latest_model_path()
    )
    if not model_path or not model_path.exists():
        return {
            "ok": False,
            "error": "model_not_found",
            "models_dir": str(_default_models_dir()),
        }

    runtime = _load_runtime(model_path)
    result = runtime.parse(text.strip())
    return {"ok": True, "result": result, "model_path": str(model_path)}


class Handler(BaseHTTPRequestHandler):
    server_version = "AdaOSRasaNLU/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        if self.path == "/health":
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "service": "rasa_nlu_service_skill",
                    "model_path": str(_MODEL_PATH) if _MODEL_PATH else None,
                },
            )
            return
        _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        payload = _read_json(self)
        try:
            if self.path == "/train":
                result = _train(payload)
                _json_response(self, 200 if result.get("ok") else 400, result)
                return
            if self.path == "/parse":
                result = _parse(payload)
                status = 200 if result.get("ok") else 409 if result.get("error") == "model_not_found" else 400
                _json_response(self, status, result)
                return
            _json_response(self, 404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _json_response(
                self,
                500,
                {
                    "ok": False,
                    "error": "internal_error",
                    "detail": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )


if __name__ == "__main__":
    host = os.getenv("ADAOS_SERVICE_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("ADAOS_SERVICE_PORT", "18092") or "18092")
    except Exception:
        port = 18092
    ThreadingHTTPServer((host, port), Handler).serve_forever()
