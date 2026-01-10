from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sanic import Sanic, response
from sanic.request import Request


app = Sanic("rasa_nlu_service")


def _models_dir() -> Path:
    raw = os.getenv("ADAOS_RASA_MODELS_DIR") or ""
    if raw:
        return Path(raw)
    # Default to AdaOS models location (repo-relative): <repo>/.adaos/models/interpreter
    # When run from SkillSupervisor, CWD is skill root, so walk up to find ".adaos".
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".adaos" / "models" / "interpreter"
        if candidate.exists():
            return candidate
    return Path.cwd() / ".adaos" / "models" / "interpreter"


def _model_path() -> Path:
    direct = os.getenv("ADAOS_RASA_MODEL_PATH") or ""
    if direct:
        return Path(direct)
    return _models_dir() / "interpreter_latest.tar.gz"


class RasaModel:
    def __init__(self) -> None:
        self._agent: Any | None = None
        self._path: Path | None = None
        self._mtime: float | None = None

    def load_if_needed(self) -> None:
        path = _model_path()
        mtime = path.stat().st_mtime if path.exists() else None
        if self._agent is not None and self._path == path and self._mtime == mtime:
            return
        if not path.exists():
            self._agent = None
            self._path = path
            self._mtime = None
            return
        from rasa.core.agent import Agent  # type: ignore

        self._agent = Agent.load(str(path))
        self._path = path
        self._mtime = mtime

    async def parse(self, text: str) -> dict:
        self.load_if_needed()
        if self._agent is None:
            raise RuntimeError("no model loaded")
        result = await self._agent.parse_message(text)
        if not isinstance(result, dict):
            raise RuntimeError(f"unexpected result type {type(result)!r}")
        return result


_MODEL = RasaModel()


@app.get("/health")
async def health(_: Request):
    path = _model_path()
    ok = path.exists()
    return response.json(
        {
            "ok": True,
            "model_path": str(path),
            "model_exists": ok,
        }
    )


@app.post("/parse")
async def parse(request: Request):
    payload = request.json or {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return response.json({"ok": False, "error": "text_required"}, status=400)
    try:
        result = await _MODEL.parse(text.strip())
    except Exception as exc:
        return response.json({"ok": False, "error": str(exc)}, status=500)
    return response.json({"ok": True, "result": result})


@app.post("/train")
async def train(request: Request):
    payload = request.json or {}
    project_dir = payload.get("project_dir")
    out_dir = payload.get("out_dir")
    fixed_name = payload.get("fixed_model_name") or "interpreter_latest"
    if not isinstance(project_dir, str) or not project_dir.strip():
        return response.json({"ok": False, "error": "project_dir_required"}, status=400)
    if not isinstance(out_dir, str) or not out_dir.strip():
        return response.json({"ok": False, "error": "out_dir_required"}, status=400)

    cmd = [
        sys.executable,
        "-m",
        "rasa",
        "train",
        "nlu",
        "--fixed-model-name",
        str(fixed_name),
        "--out",
        str(out_dir),
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True)
    except Exception as exc:
        return response.json({"ok": False, "error": f"spawn_failed:{exc}"}, status=500)
    if proc.returncode != 0:
        return response.json(
            {"ok": False, "error": "train_failed", "stdout": proc.stdout, "stderr": proc.stderr},
            status=500,
        )

    # reload model if present
    try:
        _MODEL.load_if_needed()
    except Exception:
        pass

    return response.json({"ok": True, "stdout": proc.stdout, "stderr": proc.stderr})

