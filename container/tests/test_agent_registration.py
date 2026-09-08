"""Regression test: every built-in agent module must be imported by the app.

The @agent decorator only runs at import time. If a module like
app.agents.filler is never imported, install_all_agents silently skips it
(cls_info is None -> continue) and every dispatch fails with
RuntimeError("Agent not found: <id>").
"""

import app.agents  # noqa: F401
from app.agents.decorator import _AGENT_CLASSES

# Mirrors ordered_agent_ids in app.agents.decorator.install_all_agents.
# cancel-interaction is pipeline-level, not a decorated agent.
EXPECTED_AGENT_IDS = {
    "filler-agent",
    "orchestrator",
    "general-agent",
    "light-agent",
    "music-agent",
    "cover-agent",
    "vacuum-agent",
    "timer-agent",
    "climate-agent",
    "media-agent",
    "scene-agent",
    "automation-agent",
    "security-agent",
    "send-agent",
    "calendar-agent",
    "lists-agent",
    "rewrite-agent",
}


def test_all_builtin_agents_imported():
    missing = EXPECTED_AGENT_IDS - set(_AGENT_CLASSES)
    assert not missing, f"agent modules not imported, decorator never ran: {sorted(missing)}"
