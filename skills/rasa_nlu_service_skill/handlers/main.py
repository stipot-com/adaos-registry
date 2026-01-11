from __future__ import annotations

"""
Service-skill placeholder + self-managed hooks.

The platform currently expects `handlers/main.py` to exist during installation
and runtime slot preparation. Service skills are started by
`adaos.services.skill.service_supervisor.ServiceSkillSupervisor` and do not
expose in-process tools by default.
"""


def on_issue(payload: dict) -> dict:
    # Called by the platform when it detects an issue (crash loop, healthcheck failure, etc).
    # Keep it side-effect free by default; implement custom behavior in this skill if needed.
    return {"ok": True, "received": payload.get("issue", {}).get("type")}


def on_self_heal(payload: dict) -> dict:
    # Called by the platform when it decides to attempt self-heal.
    # The platform currently does not interpret the return value; it is stored for observability.
    return {"ok": True, "action": "noop", "reason": payload.get("reason")}

