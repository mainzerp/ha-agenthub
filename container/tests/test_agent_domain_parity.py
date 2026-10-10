"""Device agents keep one allowed-domain set across executor, metadata, and admin API.

The executor's ``_ALLOWED_DOMAINS`` constant is the source of truth. The
``@agent`` metadata drives keyword recall (which entities the LLM sees as
candidates) and ``AGENT_ALLOWED_DOMAINS`` drives the admin match preview;
any drift hides entities the executor would accept (issue #132).
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("litellm", MagicMock())

from app.agents import actionable  # noqa: E402
from app.api.routes.entity_index_api import AGENT_ALLOWED_DOMAINS  # noqa: E402

_DEVICE_AGENTS = {
    "light-agent": actionable.LightAgent,
    "climate-agent": actionable.ClimateAgent,
    "cover-agent": actionable.CoverAgent,
    "vacuum-agent": actionable.VacuumAgent,
    "scene-agent": actionable.SceneAgent,
    "security-agent": actionable.SecurityAgent,
    "media-agent": actionable.MediaAgent,
    "music-agent": actionable.MusicAgent,
}


@pytest.mark.parametrize("agent_id", sorted(_DEVICE_AGENTS))
def test_metadata_matches_executor_allowed_domains(agent_id):
    meta = _DEVICE_AGENTS[agent_id]._agent_meta
    executor = importlib.import_module(meta["executor_module"])
    assert meta["allowed_domains"] == executor._ALLOWED_DOMAINS


@pytest.mark.parametrize("agent_id", sorted(_DEVICE_AGENTS))
def test_admin_api_map_matches_executor_allowed_domains(agent_id):
    meta = _DEVICE_AGENTS[agent_id]._agent_meta
    executor = importlib.import_module(meta["executor_module"])
    assert AGENT_ALLOWED_DOMAINS[agent_id] == executor._ALLOWED_DOMAINS


def test_climate_and_security_recall_cover_their_executor_domains():
    assert {"fan", "humidifier"} <= actionable.ClimateAgent._agent_meta["allowed_domains"]
    assert {"camera", "sensor"} <= actionable.SecurityAgent._agent_meta["allowed_domains"]
