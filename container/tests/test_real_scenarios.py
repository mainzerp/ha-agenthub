"""Real-scenario end-to-end test entry point.

Each YAML file under ``container/tests/data/scenarios/`` is parametrized
into a single test case. The runner wires the production orchestrator
pipeline against deterministic stubs and asserts service calls, routing,
and speech.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock litellm before any app imports (mirrors test_orchestrator_pipeline).
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


class _RateLimitError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
_litellm_mock.RateLimitError = _RateLimitError
sys.modules.setdefault("litellm", _litellm_mock)

from tests.scenarios.loader import list_scenario_files, load_scenario  # noqa: E402
from tests.scenarios.runner import run_scenario  # noqa: E402

pytestmark = pytest.mark.real_scenarios


def _scenario_id(path: Path) -> str:
    rel = path.relative_to(path.parent.parent)
    return rel.as_posix().replace(".yaml", "")


_SCENARIO_FILES = list_scenario_files()


def _params():
    out = []
    for p in _SCENARIO_FILES:
        marks = []
        try:
            sc = load_scenario(p)
            if sc.xfail:
                marks.append(pytest.mark.xfail(strict=False, reason=sc.xfail))
        except Exception:
            pass
        out.append(pytest.param(p, id=_scenario_id(p), marks=marks))
    return out


@pytest.mark.parametrize("scenario_path", _params())
@pytest.mark.asyncio
async def test_real_scenario(scenario_path: Path, tmp_path):
    """Run one YAML-defined real-pipeline scenario end-to-end."""
    scenario = load_scenario(scenario_path)
    db_path = tmp_path / "scenario.db"
    await run_scenario(scenario, db_path)
