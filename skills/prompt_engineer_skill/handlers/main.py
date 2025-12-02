from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.decorators import tool

_log = logging.getLogger("skills.prompt_engineer")

# During static validation, handlers are imported in a lightweight subprocess
# without a full AdaOS runtime. In that case, avoid requiring AgentContext
# at import time so that validators can safely introspect decorators.
if os.environ.get("ADAOS_VALIDATE") == "1":
    _CTX = None  # type: ignore[assignment]
else:
    _CTX = require_ctx("skills.prompt_engineer_skill")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_ctx():
    """
    Helper that returns a live AgentContext. During runtime we cache the
    instance in a module-level variable; during validation this is only
    called from tool bodies, which are never executed.
    """
    global _CTX  # type: ignore[global-variable-not-assigned]
    if _CTX is None:
        _CTX = require_ctx("skills.prompt_engineer_skill")
    return _CTX


def _project_root(object_type: str, object_id: str) -> Path:
    """
    Resolve the filesystem root for a prompt project.

    For v0.1 we operate directly on the stable workspace:
      - skills:    <base>/.adaos/workspace/skills/<object_id>/
      - scenarios: <base>/.adaos/workspace/scenarios/<object_id>/
    """
    ctx = _require_ctx()
    kind = (object_type or "").strip().lower()
    if kind == "skill":
        base = ctx.paths.skills_dir()
    elif kind == "scenario":
        base = ctx.paths.scenarios_dir()
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")
    root = (base / object_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _state_path(root: Path) -> Path:
    return root / "prompt_state.json"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _default_state(object_type: str, object_id: str) -> Dict[str, Any]:
    return {
        "object_type": object_type,
        "object_id": object_id,
        "base_tz": "",
        "tz_addenda": [],
        "prepare": {
            "general_prompt": "",
            "iterations": [],
        },
        "generate": {
            "general_prompt": "",
            "iterations": [],
        },
        "llm_profile_id": None,
        "target_node_id": None,
    }


def _load_state_from_fs(object_type: str, object_id: str, root: Path) -> Dict[str, Any]:
    state: Dict[str, Any] = _default_state(object_type, object_id)

    # Base TZ
    tz_base = root / "tz" / "base_tz.md"
    state["base_tz"] = _read_text(tz_base)

    # TZ addenda (append-only blocks)
    addenda_dir = root / "tz" / "addenda"
    add_items: List[Dict[str, Any]] = []
    if addenda_dir.exists():
        for entry in sorted(addenda_dir.glob("*.md")):
            add_id = entry.stem
            try:
                created_at = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc).isoformat()
            except Exception:
                created_at = _now_utc_iso()
            add_items.append(
                {
                    "id": add_id,
                    "created_at": created_at,
                    "text": _read_text(entry),
                    "iteration_ref": None,
                }
            )
    state["tz_addenda"] = add_items

    # Prepare / generate general prompts (iterations will be populated in later stages)
    prepare_prompt = root / "prepare" / "general_prompt.md"
    state["prepare"]["general_prompt"] = _read_text(prepare_prompt)

    generate_prompt = root / "generate" / "general_prompt.md"
    state["generate"]["general_prompt"] = _read_text(generate_prompt)

    return state


def _normalize_state(raw: Any, object_type: str, object_id: str, root: Path) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _load_state_from_fs(object_type, object_id, root)

    # Ensure required top-level keys are present, do not drop existing data.
    if raw.get("object_type") != object_type:
        raw["object_type"] = object_type
    if raw.get("object_id") != object_id:
        raw["object_id"] = object_id

    raw.setdefault("base_tz", "")
    raw.setdefault("tz_addenda", [])
    raw.setdefault("prepare", {})
    raw.setdefault("generate", {})

    prepare = raw["prepare"]
    if not isinstance(prepare, dict):
        prepare = {}
    prepare.setdefault("general_prompt", "")
    prepare.setdefault("iterations", [])
    raw["prepare"] = prepare

    generate = raw["generate"]
    if not isinstance(generate, dict):
        generate = {}
    generate.setdefault("general_prompt", "")
    generate.setdefault("iterations", [])
    raw["generate"] = generate

    raw.setdefault("llm_profile_id", None)
    raw.setdefault("target_node_id", None)

    return raw


def _load_state(object_type: str, object_id: str) -> Dict[str, Any]:
    root = _project_root(object_type, object_id)
    path = _state_path(root)
    data: Any
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            _log.warning("failed to parse prompt_state.json for %s:%s, rebuilding from FS", object_type, object_id, exc_info=True)
            data = None
    else:
        data = None

    if data is None:
        state = _load_state_from_fs(object_type, object_id, root)
        _write_state(root, state)
        return state

    state = _normalize_state(data, object_type, object_id, root)
    _write_state(root, state)
    return state


def _write_state(root: Path, state: Dict[str, Any]) -> None:
    path = _state_path(root)
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2)
    except TypeError:
        # Fallback: coerce non-serializable values to plain types.
        payload = json.dumps(json.loads(json.dumps(state, default=str)), ensure_ascii=False, indent=2)
    _write_text(path, payload)


@tool("prompt_load_state")
def prompt_load_state(object_type: str, object_id: str) -> Dict[str, Any]:
    """
    Load (or lazily initialize) PromptProjectState for a skill or scenario.

    If ``prompt_state.json`` is missing, the state is reconstructed from
    existing files (tz/base_tz.md, tz/addenda/*, prepare/general_prompt.md,
    generate/general_prompt.md) and persisted back to disk.
    """
    state = _load_state(object_type, object_id)
    return {"ok": True, "state": state}


@tool("prompt_save_base_tz")
def prompt_save_base_tz(object_type: str, object_id: str, text: str) -> Dict[str, Any]:
    """
    Replace the editable base TZ text for the target project.

    The value is stored both in tz/base_tz.md and in prompt_state.json.
    """
    root = _project_root(object_type, object_id)
    _write_text(root / "tz" / "base_tz.md", text or "")
    state = _load_state(object_type, object_id)
    state["base_tz"] = text or ""
    _write_state(root, state)
    return {"ok": True, "state": state}


@tool("prompt_append_tz_addendum")
def prompt_append_tz_addendum(
    object_type: str,
    object_id: str,
    text: str,
    iteration_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Append a new addendum block to TZ in an append-only fashion.

    A new file is created under tz/addenda/ and the block is recorded in
    prompt_state.json with timestamp and optional iteration reference.
    """
    root = _project_root(object_type, object_id)
    ts = datetime.now(timezone.utc)
    add_id = ts.strftime("tz_add_%Y%m%dT%H%M%S")
    filename = f"{add_id}.md"
    _write_text(root / "tz" / "addenda" / filename, text or "")

    state = _load_state(object_type, object_id)
    addenda: List[Dict[str, Any]] = state.get("tz_addenda") or []
    if not isinstance(addenda, list):
        addenda = []
    addenda.append(
        {
            "id": add_id,
            "created_at": ts.isoformat(),
            "text": text or "",
            "iteration_ref": iteration_ref,
        }
    )
    state["tz_addenda"] = addenda
    _write_state(root, state)
    return {"ok": True, "state": state}


__all__ = [
    "prompt_load_state",
    "prompt_save_base_tz",
    "prompt_append_tz_addendum",
]


@tool("prompt_get_tz_state")
def prompt_get_tz_state(object_type: str, object_id: str) -> Dict[str, Any]:
    """
    Lightweight helper for Prompt IDE UI: return only the TZ-related
    portion of PromptProjectState for the given object.
    """
    state = _load_state(object_type, object_id)
    return {
        "ok": True,
        "object_type": state.get("object_type"),
        "object_id": state.get("object_id"),
        "base_tz": state.get("base_tz") or "",
        "tz_addenda": state.get("tz_addenda") or [],
    }


def _dev_root() -> Path:
    """
    Resolve dev root (<base>/dev/<subnet_id>) using the shared Settings/PathProvider.
    """
    ctx = _require_ctx()
    return ctx.paths.dev_dir()


def _list_dirs(root: Path) -> List[str]:
    if not root.exists():
        return []
    out: List[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            out.append(entry.name)
    return out


@tool("prompt_list_dev_projects")
def prompt_list_dev_projects() -> Dict[str, Any]:
    """
    List available skills and scenarios in the DEV space for the current
    subnet. This is used by Prompt IDE to populate the project tree.
    """
    ctx = _require_ctx()
    dev_skills = _list_dirs(ctx.paths.dev_skills_dir())
    dev_scenarios = _list_dirs(ctx.paths.dev_scenarios_dir())

    def _scenario_node(name: str) -> Dict[str, Any]:
        base_id = f"scenario:{name}"
        return {
            "id": base_id,
            "label": f"Scenario: {name}",
            "children": [
                {
                    "id": f"{base_id}:tz",
                    "label": "Stage: TZ",
                    "object_type": "scenario",
                    "object_id": name,
                    "stage": "tz",
                },
                {
                    "id": f"{base_id}:prepare",
                    "label": "Stage: Prepare",
                    "object_type": "scenario",
                    "object_id": name,
                    "stage": "prepare",
                },
                {
                    "id": f"{base_id}:generate",
                    "label": "Stage: Generate",
                    "object_type": "scenario",
                    "object_id": name,
                    "stage": "generate",
                },
            ],
        }

    def _skill_node(name: str) -> Dict[str, Any]:
        base_id = f"skill:{name}"
        return {
            "id": base_id,
            "label": f"Skill: {name}",
            "children": [
                {
                    "id": f"{base_id}:tz",
                    "label": "Stage: TZ",
                    "object_type": "skill",
                    "object_id": name,
                    "stage": "tz",
                },
                {
                    "id": f"{base_id}:prepare",
                    "label": "Stage: Prepare",
                    "object_type": "skill",
                    "object_id": name,
                    "stage": "prepare",
                },
                {
                    "id": f"{base_id}:generate",
                    "label": "Stage: Generate",
                    "object_type": "skill",
                    "object_id": name,
                    "stage": "generate",
                },
            ],
        }

    tree: List[Dict[str, Any]] = [
        {
            "id": "project_scenarios",
            "label": "Scenarios",
            "children": [_scenario_node(name) for name in dev_scenarios],
        },
        {
            "id": "project_skills",
            "label": "Skills",
            "children": [_skill_node(name) for name in dev_skills],
        },
    ]

    return {
        "ok": True,
        "tree": tree,
        "skills": dev_skills,
        "scenarios": dev_scenarios,
    }
