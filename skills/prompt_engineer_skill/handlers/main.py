# Main handler!
# Main handler!
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.decorators import tool
from adaos.sdk.root.developer import RootDeveloperService, TemplateResolutionError, RootServiceError
from adaos.services.eventbus import emit as bus_emit

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
    # For Prompt IDE we operate on DEV artifacts so that code edits and
    # prompts are always applied to the active dev workspace.
    if kind == "skill":
        base = ctx.paths.dev_skills_dir()
    elif kind == "scenario":
        base = ctx.paths.dev_scenarios_dir()
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")
    root = (base / object_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ts_artifact_path(root: Path) -> Path:
    """
    Location for the TS draft artifact used by Prompt IDE (LLM Artifacts panel).
    """
    return root / "artifacts" / "llm_artifacts" / "ts_draft.md"


def _ts_artifact_path(root: Path) -> Path:
    """
    Location for the TS draft artifact used by Prompt IDE (LLM Artifacts panel).
    """
    return root / "artifacts" / "llm_artifacts" / "ts_draft.md"


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
        "workflow_state": "tz",
    }


def _git_log_path(root: Path) -> Path:
    return root / "git" / "log.json"


@tool("prompt_get_git_log")
def prompt_get_git_log(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Return the recent Prompt IDE git actions for the requested project.
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
            data = json.loads(raw)
            if isinstance(data, list):
                items = [e for e in data if isinstance(e, dict)]
        except Exception:
            items = []

    return {"ok": True, "object_type": object_type, "object_id": object_id, "items": items}


def _append_git_log(object_type: str, object_id: str, result: Dict[str, Any]) -> None:
    root = _project_root(object_type, object_id)
    log_path = _git_log_path(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    existing: List[Dict[str, Any]] = []
    if log_path.exists():
        try:
            raw = json.loads(log_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = [e for e in raw if isinstance(e, dict)]
        except Exception:
            existing = []
    entry = {
        "ts": _now_utc_iso(),
        "object_type": object_type,
        "object_id": object_id,
        "result": result,
    }
    existing.append(entry)
    # keep only last 50 entries
    existing = existing[-50:]
    log_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


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
    raw.setdefault("workflow_state", "tz")

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


def _emit_project_changed(object_type: str, object_id: str, *, reason: str) -> None:
    kind = str(object_type or "").strip().lower()
    project_id = str(object_id or "").strip()
    if kind not in {"skill", "scenario"} or not project_id:
        return
    try:
        ctx = _require_ctx()
        bus_emit(
            ctx.bus,
            "prompt.project.changed",
            {
                "object_type": kind,
                "object_id": project_id,
                "reason": str(reason or "").strip() or "changed",
            },
            "skills.prompt_engineer_skill",
        )
    except Exception:
        _log.debug("failed to emit prompt.project.changed for %s:%s", kind, project_id, exc_info=True)


def _build_ts_text(state: Dict[str, Any]) -> str:
    """
    Combine base_tz and tz_addenda into a single Technical Specification text.
    """
    base = str(state.get("base_tz") or "").strip()
    addenda_items: List[Dict[str, Any]] = state.get("tz_addenda") or []
    addenda_texts: List[str] = []
    for item in addenda_items:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                addenda_texts.append(text)
    if not addenda_texts:
        return base
    return base + "\n\n[ADDENDA]\n" + "\n\n---\n\n".join(addenda_texts)


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
    _emit_project_changed(object_type, object_id, reason="base_tz_saved")
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
    _emit_project_changed(object_type, object_id, reason="tz_addendum_appended")
    return {"ok": True, "state": state}


__all__ = [
    "prompt_load_state",
    "prompt_save_base_tz",
    "prompt_append_tz_addendum",
]


@tool("prompt_get_tz_state")
def prompt_get_tz_state(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Lightweight helper for Prompt IDE UI: return only the TZ-related
    portion of PromptProjectState for the given object.

    When called with an empty payload (e.g. before the user selects a
    project in the Prompt IDE), this returns an empty but well-formed
    structure instead of raising an error so that the UI can render.
    """
    payload = payload or {}
    object_type = payload.get("object_type")
    object_id = payload.get("object_id")
    if not object_type or not object_id:
        return {
            "ok": False,
            "object_type": object_type or "",
            "object_id": object_id or "",
            "base_tz": "",
            "tz_addenda": [],
        }

    state = _load_state(str(object_type), str(object_id))
    return {
        "ok": True,
        "object_type": state.get("object_type"),
        "object_id": state.get("object_id"),
        "base_tz": state.get("base_tz") or "",
        "tz_addenda": state.get("tz_addenda") or [],
    }


@tool("tz_execute")
def tz_execute(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Execute TS → detailed implementation brief via LLM for the current project.

    This is invoked from the Prompt IDE workflow state `tz` (tz.execute action).
    It reads current PromptProjectState (base_tz + tz_addenda), builds a combined
    Technical Specification and sends it through request_ts_draft.

    The resulting artifact path is returned so that the IDE can show it under
    LLM Artifacts (draft).
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    state = _load_state(object_type, object_id)
    ts_text = _build_ts_text(state)
    if not ts_text:
        return {
            "ok": False,
            "object_type": object_type,
            "object_id": object_id,
            "error": "technical_spec_missing",
        }

    # Send request via Root LLM proxy and persist TS draft artifact for this project.
    from adaos.sdk.llm.llm_client import request_ts_draft

    root = _project_root(object_type, object_id)
    artifact_path = _ts_artifact_path(root)
    result = request_ts_draft(ts_text, output_path=artifact_path)
    return {
        "ok": True,
        "object_type": object_type,
        "object_id": object_id,
        "output_text": str(result.get("output_text") or ""),
        "output_path": str(result.get("output_path") or artifact_path),
        "request_prompt": result.get("request_prompt"),
        "raw_response": result.get("response"),
    }


@tool("prompt_llm_list_models")
def prompt_llm_list_models(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    List available LLM models from the Root LLM proxy.
    """
    from adaos.sdk.llm.llm_client import list_llm_models

    payload = payload or {}
    timeout = payload.get("timeout")
    try:
        data = list_llm_models(timeout=float(timeout) if timeout is not None else None)
    except Exception as exc:
        _log.warning("prompt_llm_list_models failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "data": data}


@tool("tz_add_reset")
def tz_add_reset(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Reset TZ addenda for the current project (used from tz_add.reset action).
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    state = _load_state(object_type, object_id)
    state["tz_addenda"] = []
    root = _project_root(object_type, object_id)
    _write_state(root, state)
    _emit_project_changed(object_type, object_id, reason="tz_addenda_reset")
    return {
        "ok": True,
        "object_type": object_type,
        "object_id": object_id,
        "tz_addenda": [],
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
def prompt_list_dev_projects(payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    List available skills and scenarios in the DEV space.

    If ``payload`` contains ``object_type``/``object_id`` (skill|scenario),
    return only the three stage entries (TZ/Prepare/Generate) for that
    object. Otherwise return a flat list for all dev projects.
    """
    ctx = _require_ctx()
    dev_skills = _list_dirs(ctx.paths.dev_skills_dir())
    dev_scenarios = _list_dirs(ctx.paths.dev_scenarios_dir())

    def _stages_for(kind: str, name: str) -> List[Dict[str, Any]]:
        base = f"{'Scenario' if kind == 'scenario' else 'Skill'}: {name}"
        prefix = f"{kind}:{name}"
        return [
            {
                "id": f"{prefix}:tz",
                "label": f"{base} / TZ",
                "object_type": kind,
                "object_id": name,
                "stage": "tz",
            },
            {
                "id": f"{prefix}:prepare",
                "label": f"{base} / Prepare",
                "object_type": kind,
                "object_id": name,
                "stage": "prepare",
            },
            {
                "id": f"{prefix}:generate",
                "label": f"{base} / Generate",
                "object_type": kind,
                "object_id": name,
                "stage": "generate",
            },
        ]

    obj_type = (payload or {}).get("object_type")
    obj_id = (payload or {}).get("object_id")
    if obj_type in ("skill", "scenario") and isinstance(obj_id, str) and obj_id:
        return _stages_for(str(obj_type), obj_id)

    items: List[Dict[str, Any]] = []
    for name in dev_scenarios:
        items.extend(_stages_for("scenario", name))
    for name in dev_skills:
        items.extend(_stages_for("skill", name))
    return items


@tool("prompt_list_dev_objects")
def prompt_list_dev_objects(payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:  # noqa: ARG001
    """
    List root DEV objects (skills and scenarios) without stages for
    project selection modals.

    Each item carries minimal metadata resolved from the dev manifest
    (scenario.yaml / skill.yaml) when available so that the UI can show
    name, version and description.
    """
    ctx = _require_ctx()
    dev_skills_root = ctx.paths.dev_skills_dir()
    dev_scenarios_root = ctx.paths.dev_scenarios_dir()

    def _scenario_meta(name: str) -> Dict[str, Any]:
        path = (dev_scenarios_root / name / "scenario.yaml").resolve()
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    data = raw
            except Exception:  # pragma: no cover - best-effort
                _log.warning("failed to read scenario manifest for %s", name, exc_info=True)
        # Per-project workflow state (if any) from PromptProjectState.
        wf_state = "tz"
        try:
            wf_state = _load_state("scenario", name).get("workflow_state") or "tz"
        except Exception:  # pragma: no cover - best-effort
            wf_state = "tz"

        return {
            "name": name,
            "type": data.get("type") or "scenario",
            "title": data.get("title") or name,
            "description": data.get("description") or "",
            "version": data.get("version") or "",
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat() if path.exists() else None,
            "workflow_state": wf_state,
        }

    def _skill_meta(name: str) -> Dict[str, Any]:
        # Reuse the same search logic as SkillManager._load_manifest where possible.
        root = (dev_skills_root / name).resolve()
        candidates = ["skill.yaml", "manifest.yaml", "resolved.manifest.json", "manifest.json", "skill.json"]
        data: Dict[str, Any] = {}
        manifest_path: Optional[Path] = None
        for fname in candidates:
            path = root / fname
            if not path.exists():
                continue
            manifest_path = path
            try:
                if path.suffix in {".yaml", ".yml"}:
                    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                else:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
                break
            except Exception:  # pragma: no cover - best-effort
                _log.warning("failed to read skill manifest for %s", name, exc_info=True)
                break
        updated_at: Optional[str] = None
        if manifest_path and manifest_path.exists():
            try:
                updated_at = datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=timezone.utc).isoformat()
            except Exception:
                updated_at = None
        wf_state = "tz"
        try:
            wf_state = _load_state("skill", name).get("workflow_state") or "tz"
        except Exception:  # pragma: no cover - best-effort
            wf_state = "tz"

        return {
            "name": name,
            "type": data.get("type") or "skill",
            "title": data.get("title") or name,
            "description": data.get("description") or "",
            "version": data.get("version") or "",
            "updated_at": updated_at,
            "workflow_state": wf_state,
        }

    items: List[Dict[str, Any]] = []

    for name in _list_dirs(dev_scenarios_root):
        meta = _scenario_meta(name)
        items.append(
            {
                "id": f"scenario:{name}",
                "label": f"Scenario: {meta['title']}",
                "object_type": "scenario",
                "object_id": name,
                "name": meta["name"],
                "type": meta["type"],
                "title": meta["title"],
                "description": meta["description"],
                "version": meta["version"],
                "updated_at": meta["updated_at"],
                "workflow_state": meta["workflow_state"],
            }
        )

    for name in _list_dirs(dev_skills_root):
        meta = _skill_meta(name)
        items.append(
            {
                "id": f"skill:{name}",
                "label": f"Skill: {meta['title']}",
                "object_type": "skill",
                "object_id": name,
                "name": meta["name"],
                "type": meta["type"],
                "title": meta["title"],
                "description": meta["description"],
                "version": meta["version"],
                "updated_at": meta["updated_at"],
                "workflow_state": meta["workflow_state"],
            }
        )

    return items


@tool("prompt_list_project_objects")
def prompt_list_project_objects(payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    List constituent objects for a Prompt IDE project.

    Projects are defined as:
      - skill project   -> a single skill object;
      - scenario project -> the scenario itself + its ``depends`` skills.
    """
    payload = payload or {}
    project_type = (payload.get("project_type") or payload.get("object_type") or "").strip().lower()
    project_id = (payload.get("project_id") or payload.get("object_id") or "").strip()
    if not project_type or not project_id:
        return []

    ctx = _require_ctx()
    items: List[Dict[str, Any]] = []

    def _project_item(kind: str, name: str) -> Dict[str, Any]:
        # Reuse metadata helper via prompt_list_dev_objects-style logic.
        meta: Dict[str, Any]
        if kind == "scenario":
            root = ctx.paths.dev_scenarios_dir()
            path = (root / name / "scenario.yaml").resolve()
            raw: Dict[str, Any] = {}
            if path.exists():
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    if isinstance(data, dict):
                        raw = data
                except Exception:  # pragma: no cover - best-effort
                    _log.warning("failed to read scenario manifest for %s", name, exc_info=True)
            updated_at = None
            if path.exists():
                try:
                    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
                except Exception:
                    updated_at = None
            meta = {
                "name": name,
                "type": raw.get("type") or "scenario",
                "title": raw.get("title") or name,
                "description": raw.get("description") or "",
                "version": raw.get("version") or "",
                "updated_at": updated_at,
            }
        else:
            root = ctx.paths.dev_skills_dir()
            skill_dir = (root / name).resolve()
            candidates = ["skill.yaml", "manifest.yaml", "resolved.manifest.json", "manifest.json", "skill.json"]
            raw: Dict[str, Any] = {}
            manifest_path: Optional[Path] = None
            for fname in candidates:
                path = skill_dir / fname
                if not path.exists():
                    continue
                manifest_path = path
                try:
                    if path.suffix in {".yaml", ".yml"}:
                        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    else:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        raw = data
                    break
                except Exception:  # pragma: no cover - best-effort
                    _log.warning("failed to read skill manifest for %s", name, exc_info=True)
                    break
            updated_at = None
            if manifest_path and manifest_path.exists():
                try:
                    updated_at = datetime.fromtimestamp(manifest_path.stat().st_mtime, tz=timezone.utc).isoformat()
                except Exception:
                    updated_at = None
            meta = {
                "name": name,
                "type": raw.get("type") or "skill",
                "title": raw.get("title") or name,
                "description": raw.get("description") or "",
                "version": raw.get("version") or "",
                "updated_at": updated_at,
            }

        # Per-project workflow state (if any) from PromptProjectState.
        wf_state = "tz"
        try:
            wf_state = _load_state(kind, name).get("workflow_state") or "tz"
        except Exception:  # pragma: no cover - best-effort
            wf_state = "tz"

        label_prefix = "Scenario" if kind == "scenario" else "Skill"
        return {
            "id": f"{kind}:{name}",
            "label": f"{label_prefix}: {meta['title']}",
            "object_type": kind,
            "object_id": name,
            "name": meta["name"],
            "type": meta["type"],
            "title": meta["title"],
            "description": meta["description"],
            "version": meta["version"],
            "updated_at": meta["updated_at"],
            "workflow_state": wf_state,
        }

    if project_type == "skill":
        items.append(_project_item("skill", project_id))
        return items

    # Scenario project: scenario itself + depends skills (if present).
    items.append(_project_item("scenario", project_id))

    scen_root = ctx.paths.dev_scenarios_dir()
    scen_yaml = (scen_root / project_id / "scenario.yaml").resolve()
    depends: List[str] = []
    if scen_yaml.exists():
        try:
            raw = yaml.safe_load(scen_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                depends_raw = raw.get("depends") or []
                if isinstance(depends_raw, list):
                    depends = [str(x) for x in depends_raw if isinstance(x, (str, bytes))]
        except Exception:  # pragma: no cover - best-effort
            _log.warning("failed to read scenario depends for %s", project_id, exc_info=True)

    for dep in depends:
        try:
            items.append(_project_item("skill", dep))
        except Exception:
            # Missing skill in dev space – skip but keep scenario project usable.
            _log.debug("dependency skill '%s' not present in dev space", dep, exc_info=True)

    return items


def _detect_language_from_suffix(path: Path) -> str:
    """
    Map a file suffix to a lightweight "language" label for syntax
    highlighting in the web UI. This is intentionally small and
    implementation-agnostic – the frontend is free to treat these
    labels as CSS classes or editor modes.
    """
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in (".yml", ".yaml"):
        return "yaml"
    if suffix == ".json":
        return "json"
    if suffix in (".md", ".markdown"):
        return "markdown"
    return "text"


@tool("prompt_list_project_files")
def prompt_list_project_files(payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    List relevant source files for a prompt project.

    For v0.1 we expose a flat list of *.py, *.json, *.yaml, *.yml files
    under the project root (skills/scenarios in the stable workspace).

    Workspace fallback passes a single JSON payload dict, so we accept
    ``payload`` instead of positional arguments.
    """
    payload = payload or {}
    object_type = payload.get("object_type")
    object_id = payload.get("object_id")
    if not object_type or not object_id:
        return []
    root = _project_root(object_type, object_id)
    exts = {".py", ".json", ".yml", ".yaml"}
    items: List[Dict[str, Any]] = []

    if not root.exists():
        return items

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        rel = path.relative_to(root).as_posix()
        items.append(
            {
                "id": rel,
                "label": rel,
                "path": rel,
                "object_type": object_type,
                "object_id": object_id,
                "language": _detect_language_from_suffix(path),
            }
        )
    return items


@tool("prompt_read_project_file")
def prompt_read_project_file(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Read a single project file for code viewer.

    ``payload`` should contain ``object_type``, ``object_id`` and
    project-relative ``path`` as returned by
    ``prompt_list_project_files``.
    """
    payload = payload or {}
    object_type = payload.get("object_type")
    object_id = payload.get("object_id")
    path = payload.get("path")
    if not object_type or not object_id or not path:
        raise ValueError("object_type, object_id and path are required")

    root = _project_root(object_type, object_id)
    rel_path = Path(path)
    full = (root / rel_path).resolve()

    # Basic safety: ensure we do not escape the project root.
    try:
        full.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError("path is outside project root") from exc

    language = _detect_language_from_suffix(full)
    content = _read_text(full)
    return {
        "ok": True,
        "object_type": object_type,
        "object_id": object_id,
        "path": rel_path.as_posix(),
        "language": language,
        "content": content,
    }


@tool("prompt_list_templates")
def prompt_list_templates(payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    List available templates for skills or scenarios, mirroring
    ``adaos dev skill|scenario create --template``.

    Workspace fallback executes tools with a single ``payload`` dict,
    so we accept a dict here instead of individual arguments.
    """
    payload = payload or {}
    raw_type = payload.get("object_type")
    kind = (raw_type or "scenario").strip().lower()
    if kind not in ("skill", "scenario"):
        raise ValueError("object_type must be 'skill' or 'scenario'")

    svc = RootDeveloperService()
    # RootDeveloperService uses pluralized kind identifiers internally.
    dev_kind = "skills" if kind == "skill" else "scenarios"

    # Leverage the same resolution helpers the CLI uses.
    workspace_dir = svc._workspace_templates_dir(dev_kind)  # type: ignore[attr-defined]
    builtin_dir = svc._builtin_templates_dir(dev_kind)  # type: ignore[attr-defined]
    default_name = svc._default_template_name(dev_kind)  # type: ignore[attr-defined]
    collect = svc._collect_templates  # type: ignore[attr-defined]

    def _filter(names: List[str]) -> List[str]:
        return [n for n in names if not n.startswith((".", "_"))]

    user_templates = _filter(collect(workspace_dir))
    builtin_templates = _filter(collect(builtin_dir))

    items: List[Dict[str, Any]] = []

    # Default entry first (points at the builtin default template).
    items.append(
        {
            "id": default_name,
            "label": "Default",
            "source": "builtin",
            "object_type": kind,
        }
    )

    for name in user_templates:
        items.append(
            {
                "id": name,
                "label": f"{name} (workspace)",
                "source": "workspace",
                "object_type": kind,
            }
        )

    for name in builtin_templates:
        if name == default_name:
            continue
        items.append(
            {
                "id": name,
                "label": f"{name} (builtin)",
                "source": "builtin",
                "object_type": kind,
            }
        )

    return items


@tool("prompt_create_dev_project")
def prompt_create_dev_project(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Create a DEV skill or scenario using the same RootDeveloperService
    workflow as ``adaos dev skill|scenario create``.

    The workspace bridge passes a single JSON payload dict, so we
    resolve fields manually instead of relying on positional args.
    """
    def _coerce_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("id", "value", "label", "name", "object_id", "project_id"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    payload = payload or {}
    object_type = _coerce_text(payload.get("object_type") or payload.get("project_type"))
    name = _coerce_text(payload.get("name") or payload.get("object_id") or payload.get("project_id"))
    template_value = _coerce_text(payload.get("template"))

    kind = object_type.lower() or "scenario"
    if kind not in ("skill", "scenario"):
        return {"ok": False, "error": "object_type must be 'skill' or 'scenario'"}

    if not name:
        return {"ok": False, "error": "name must not be empty"}

    template = template_value or None

    svc = RootDeveloperService()

    try:
        if kind == "skill":
            result = svc.create_skill(name, template=template)
        else:
            result = svc.create_scenario(name, template=template)
    except TemplateResolutionError as exc:
        _log.error("template resolution failed for %s '%s': %s", kind, name, exc)
        return {"ok": False, "error": str(exc)}
    except RootServiceError as exc:
        _log.error("dev %s create failed for '%s': %s", kind, name, exc)
        return {"ok": False, "error": str(exc)}
    except (ValueError, FileExistsError) as exc:
        _log.error("invalid prompt create payload for %s '%s': %s", kind, name, exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive normalization for UI
        _log.exception("unexpected dev %s create failure for '%s'", kind, name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    _emit_project_changed(kind, result.name, reason="project_created")

    return {
        "ok": True,
        "object_type": kind,
        "object_id": result.name,
        "project_id": result.name,
        "name": result.name,
        "path": str(result.path),
        "template": template or "default",
    }


@tool("prompt_snapshot_project")
def prompt_snapshot_project(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Persist the current PromptProjectState for a dev project to disk.

    For v0.1 this is effectively a no-op snapshot: we reload the state
    (which already merges file-backed TZ/addenda/general prompts) and
    re-write ``prompt_state.json`` under the project root. This gives
    us an explicit, debuggable checkpoint that the web IDE can trigger
    from the Files section.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    root = _project_root(object_type, object_id)
    state = _load_state(object_type, object_id)
    _write_state(root, state)
    _emit_project_changed(object_type, object_id, reason="project_snapshotted")
    return {"ok": True, "object_type": object_type, "object_id": object_id}


@tool("prompt_set_workflow_state")
def prompt_set_workflow_state(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Persist workflow_state for a project in PromptProjectState.

    Called from the Prompt IDE workflow panel so that per-project last
    stage can be restored when the user switches between projects.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    state_id = (payload.get("state") or "").strip()
    if not object_type or not object_id or not state_id:
        raise ValueError("object_type, object_id and state are required")

    root = _project_root(object_type, object_id)
    state = _load_state(object_type, object_id)
    state["workflow_state"] = state_id
    _write_state(root, state)
    return {"ok": True, "object_type": object_type, "object_id": object_id, "workflow_state": state_id}


@tool("prompt_get_project_meta")
def prompt_get_project_meta(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Return basic metadata (title, description, type, version, updated_at)
    for a dev scenario or skill.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    ctx = _require_ctx()
    if object_type == "scenario":
        root = ctx.paths.dev_scenarios_dir()
        root = root() if callable(root) else root
        yaml_path = (Path(root) / object_id / "scenario.yaml").resolve()
    elif object_type == "skill":
        root = ctx.paths.dev_skills_dir()
        root = root() if callable(root) else root
        yaml_path = (Path(root) / object_id / "skill.yaml").resolve()
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")

    data: Dict[str, Any] = {}
    if yaml_path.exists():
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                data = raw
        except Exception:  # pragma: no cover - best-effort
            _log.warning("prompt_get_project_meta failed to read %s", yaml_path, exc_info=True)

    updated_at = None
    if yaml_path.exists():
        try:
            updated_at = datetime.fromtimestamp(yaml_path.stat().st_mtime, tz=timezone.utc).isoformat()
        except Exception:
            updated_at = None

    return {
        "ok": True,
        "object_type": object_type,
        "object_id": object_id,
        "name": data.get("name") or object_id,
        "type": data.get("type") or object_type,
        "title": data.get("title") or object_id,
        "description": data.get("description") or "",
        "version": data.get("version") or "",
        "updated_at": updated_at,
    }


@tool("prompt_update_project_meta")
def prompt_update_project_meta(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Update editable project metadata (title, description, type) in the
    dev scenario.yaml / skill.yaml.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    proj_type = (payload.get("type") or "").strip() or None

    ctx = _require_ctx()
    if object_type == "scenario":
        root = ctx.paths.dev_scenarios_dir()
        root = root() if callable(root) else root
        yaml_path = (Path(root) / object_id / "scenario.yaml").resolve()
    elif object_type == "skill":
        root = ctx.paths.dev_skills_dir()
        root = root() if callable(root) else root
        yaml_path = (Path(root) / object_id / "skill.yaml").resolve()
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")

    data: Dict[str, Any] = {}
    if yaml_path.exists():
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                data = raw
        except Exception:  # pragma: no cover - best-effort
            _log.warning("prompt_update_project_meta failed to read %s", yaml_path, exc_info=True)

    if title:
        data["title"] = title
    if description or "description" in data:
        data["description"] = description
    if proj_type:
        data["type"] = proj_type

    try:
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - best-effort
        _log.warning("prompt_update_project_meta failed to write %s: %s", yaml_path, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}

    _emit_project_changed(object_type, object_id, reason="project_meta_updated")
    return prompt_get_project_meta({"object_type": object_type, "object_id": object_id})


@tool("prompt_git_push")
def prompt_git_push(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Push DEV skill/scenario drafts via RootDeveloperService.

    For scenarios, also pushes all dependent skills from dev_scenarios/<name>/scenario.yaml.depends.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    ctx = _require_ctx()
    svc = RootDeveloperService()
    pushed: list[Dict[str, Any]] = []

    def _push_skill(name: str) -> None:
        try:
            res = svc.push_skill(name)
            pushed.append(
                {
                    "kind": "skill",
                    "name": res.name,
                    "version": res.version,
                    "updated_at": res.updated_at,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            pushed.append(
                {
                    "kind": "skill",
                    "name": name,
                    "error": str(exc),
                }
            )

    def _push_scenario(name: str) -> None:
        try:
            res = svc.push_scenario(name)
            pushed.append(
                {
                    "kind": "scenario",
                    "name": res.name,
                    "version": res.version,
                    "updated_at": res.updated_at,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            pushed.append(
                {
                    "kind": "scenario",
                    "name": name,
                    "error": str(exc),
                }
            )

    if object_type == "skill":
        _push_skill(object_id)
    elif object_type == "scenario":
        _push_scenario(object_id)
        # Push dependent skills from scenario manifest (if any).
        scen_root = ctx.paths.dev_scenarios_dir()
        scen_root = scen_root() if callable(scen_root) else scen_root
        scen_yaml = (Path(scen_root) / object_id / "scenario.yaml").resolve()
        if scen_yaml.exists():
            try:
                raw = yaml.safe_load(scen_yaml.read_text(encoding="utf-8")) or {}
                depends = raw.get("depends") or []
                if isinstance(depends, list):
                    for dep in depends:
                        if isinstance(dep, str) and dep.strip():
                            _push_skill(dep.strip())
            except Exception:  # pragma: no cover - best-effort
                _log.warning("prompt_git_push: failed to read depends for scenario %s", object_id, exc_info=True)
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")

    ok = not any(isinstance(it, dict) and it.get("error") for it in pushed)
    result = {
        "ok": ok,
        "object_type": object_type,
        "object_id": object_id,
        "action": "push",
        "items": pushed,
    }
    _append_git_log(object_type, object_id, result)
    return result


@tool("prompt_git_update")
def prompt_git_update(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Update DEV skill/scenario drafts from Root Forge (pull latest).

    For scenarios, also updates all dependent skills from scenario.yaml.depends.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    ctx = _require_ctx()
    svc = RootDeveloperService()
    updated: list[Dict[str, Any]] = []

    def _update_skill(name: str) -> None:
        try:
            res = svc.update_skill(name)
            updated.append(
                {
                    "kind": "skill",
                    "name": res.name,
                    "version": res.version,
                    "updated_at": res.updated_at,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            updated.append(
                {
                    "kind": "skill",
                    "name": name,
                    "error": str(exc),
                }
            )

    def _update_scenario(name: str) -> None:
        try:
            res = svc.update_scenario(name)
            updated.append(
                {
                    "kind": "scenario",
                    "name": res.name,
                    "version": res.version,
                    "updated_at": res.updated_at,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            updated.append(
                {
                    "kind": "scenario",
                    "name": name,
                    "error": str(exc),
                }
            )

    if object_type == "skill":
        _update_skill(object_id)
    elif object_type == "scenario":
        _update_scenario(object_id)
        scen_root = ctx.paths.dev_scenarios_dir()
        scen_root = scen_root() if callable(scen_root) else scen_root
        scen_yaml = (Path(scen_root) / object_id / "scenario.yaml").resolve()
        if scen_yaml.exists():
            try:
                raw = yaml.safe_load(scen_yaml.read_text(encoding="utf-8")) or {}
                depends = raw.get("depends") or []
                if isinstance(depends, list):
                    for dep in depends:
                        if isinstance(dep, str) and dep.strip():
                            _update_skill(dep.strip())
            except Exception:  # pragma: no cover - best-effort
                _log.warning("prompt_git_update: failed to read depends for scenario %s", object_id, exc_info=True)
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")

    ok = not any(isinstance(it, dict) and it.get("error") for it in updated)
    result = {
        "ok": ok,
        "object_type": object_type,
        "object_id": object_id,
        "action": "update",
        "items": updated,
    }
    _append_git_log(object_type, object_id, result)
    _emit_project_changed(object_type, object_id, reason="git_updated")
    return result


@tool("prompt_git_publish")
def prompt_git_publish(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Publish DEV skill or scenario to the registry via RootDeveloperService.

    For scenarios, also publishes dependent skills listed in scenario.yaml.depends.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    bump = (payload.get("bump") or "patch").strip().lower() or "patch"
    force = bool(payload.get("force") or False)
    dry_run = bool(payload.get("dry_run") or False)

    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")
    if bump not in {"patch", "minor", "major"}:
        raise ValueError("bump must be one of patch, minor, major")

    ctx = _require_ctx()
    svc = RootDeveloperService()
    published: list[Dict[str, Any]] = []

    def _publish_skill(name: str) -> None:
        try:
            res = svc.publish_skill(name, bump=bump, force=force, dry_run=dry_run)
            published.append(
                {
                    "kind": "skill",
                    "name": res.name,
                    "version": res.version,
                    "previous_version": res.previous_version,
                    "updated_at": res.updated_at,
                    "dry_run": res.dry_run,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            published.append(
                {
                    "kind": "skill",
                    "name": name,
                    "error": str(exc),
                }
            )

    def _publish_scenario(name: str) -> None:
        try:
            res = svc.publish_scenario(name, bump=bump, force=force, dry_run=dry_run)
            published.append(
                {
                    "kind": "scenario",
                    "name": res.name,
                    "version": res.version,
                    "previous_version": res.previous_version,
                    "updated_at": res.updated_at,
                    "dry_run": res.dry_run,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            published.append(
                {
                    "kind": "scenario",
                    "name": name,
                    "error": str(exc),
                }
            )

    if object_type == "skill":
        _publish_skill(object_id)
    elif object_type == "scenario":
        _publish_scenario(object_id)
        scen_root = ctx.paths.dev_scenarios_dir()
        scen_root = scen_root() if callable(scen_root) else scen_root
        scen_yaml = (Path(scen_root) / object_id / "scenario.yaml").resolve()
        if scen_yaml.exists():
            try:
                raw = yaml.safe_load(scen_yaml.read_text(encoding="utf-8")) or {}
                depends = raw.get("depends") or []
                if isinstance(depends, list):
                    for dep in depends:
                        if isinstance(dep, str) and dep.strip():
                            _publish_skill(dep.strip())
            except Exception:  # pragma: no cover - best-effort
                _log.warning("prompt_git_publish: failed to read depends for scenario %s", object_id, exc_info=True)
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")

    ok = not any(isinstance(it, dict) and it.get("error") for it in published)
    result = {
        "ok": ok,
        "object_type": object_type,
        "object_id": object_id,
        "action": "publish",
        "bump": bump,
        "items": published,
    }
    _append_git_log(object_type, object_id, result)
    _emit_project_changed(object_type, object_id, reason="git_published")
    return result


@tool("prompt_git_delete")
def prompt_git_delete(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Delete DEV skill/scenario drafts and registry artifacts via RootDeveloperService.

    For scenarios, only the scenario is deleted; dependent skills are kept.
    """
    payload = payload or {}
    object_type = (payload.get("object_type") or "").strip().lower()
    object_id = (payload.get("object_id") or "").strip()
    if not object_type or not object_id:
        raise ValueError("object_type and object_id are required")

    svc = RootDeveloperService()
    deleted: list[Dict[str, Any]] = []

    def _delete_skill(name: str) -> None:
        try:
            res = svc.delete_skill(name)
            deleted.append(
                {
                    "kind": "skill",
                    "name": res.name,
                    "version": res.version,
                    "updated_at": res.updated_at,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            deleted.append(
                {
                    "kind": "skill",
                    "name": name,
                    "error": str(exc),
                }
            )

    def _delete_scenario(name: str) -> None:
        try:
            res = svc.delete_scenario(name)
            deleted.append(
                {
                    "kind": "scenario",
                    "name": res.name,
                    "version": res.version,
                    "updated_at": res.updated_at,
                }
            )
        except Exception as exc:  # pragma: no cover - best-effort
            deleted.append(
                {
                    "kind": "scenario",
                    "name": name,
                    "error": str(exc),
                }
            )

    ctx = _require_ctx()
    if object_type == "skill":
        _delete_skill(object_id)
        # Also remove DEV skill folder.
        dev_root = ctx.paths.dev_skills_dir()
        dev_root = dev_root() if callable(dev_root) else dev_root
        skill_path = Path(dev_root) / object_id
        if skill_path.exists():
            shutil.rmtree(skill_path, ignore_errors=True)
    elif object_type == "scenario":
        _delete_scenario(object_id)
        # Also remove DEV scenario folder (dependent skills are kept).
        dev_root = ctx.paths.dev_scenarios_dir()
        dev_root = dev_root() if callable(dev_root) else dev_root
        scen_path = Path(dev_root) / object_id
        if scen_path.exists():
            shutil.rmtree(scen_path, ignore_errors=True)
    else:
        raise ValueError("object_type must be 'skill' or 'scenario'")

    ok = not any(isinstance(it, dict) and it.get("error") for it in deleted)
    result = {
        "ok": ok,
        "object_type": object_type,
        "object_id": object_id,
        "action": "delete",
        "items": deleted,
    }
    _append_git_log(object_type, object_id, result)
    _emit_project_changed(object_type, object_id, reason="git_deleted")
    return result
