from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from adaos.sdk.core.decorators import tool
from .main import _project_root, _git_log_path


@tool("prompt_get_git_log")
def prompt_get_git_log(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Return recent git operations for a project as recorded by
    prompt_git_push/update/publish/delete.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    root = _project_root(object_type, object_id)
    path = _git_log_path(root)
    items: List[Dict[str, Any]] = []
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            data = __import__("json").loads(raw)
            if isinstance(data, list):
                items = [e for e in data if isinstance(e, dict)]
        except Exception:
            items = []

    return {"ok": True, "object_type": object_type, "object_id": object_id, "items": items}

