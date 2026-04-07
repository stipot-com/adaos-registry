from __future__ import annotations

from typing import Any, Dict, Mapping


def lang_res() -> Dict[str, str]:
    return {}


def handle(topic: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Minimal runtime entrypoint for infra_access_skill.

    The skill currently publishes metadata and Web UI, but does not yet expose
    a concrete command surface through the generic skill runtime. Returning a
    bounded no-op response keeps install/activation healthy until explicit
    topic handlers are added.
    """

    data = dict(payload or {})
    return {
        "ok": True,
        "skill": "infra_access_skill",
        "topic": str(topic or ""),
        "handled": False,
        "message": "infra_access_skill runtime entrypoint is available, but no topic handlers are implemented yet",
        "payload": data,
    }
