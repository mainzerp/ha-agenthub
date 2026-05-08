"""Parametrize real YAML scenarios through REST and WebSocket bridge layers."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.scenarios.loader import list_scenario_files, load_scenario

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
                marks.append(pytest.mark.xfail(reason=sc.xfail))
        except Exception:
            pass
        out.append(pytest.param(p, id=_scenario_id(p), marks=marks))
    return out


def _assert_speech(scenario, speech: str) -> None:
    speech_lc = speech.lower()
    for needle in scenario.expected.speech_contains:
        if needle.lower() not in speech_lc:
            raise AssertionError(f"[{scenario.id}] expected speech to contain {needle!r}; got {speech!r}")
    for needle in scenario.expected.speech_excludes:
        if needle.lower() in speech_lc:
            raise AssertionError(f"[{scenario.id}] expected speech to NOT contain {needle!r}; got {speech!r}")


def _assert_service_calls(scenario, ha_client) -> None:
    new_calls = ha_client.calls
    expected = scenario.expected
    if expected.service_calls:
        if not expected.allow_extra_calls and len(new_calls) > len(expected.service_calls):
            extra = [(c.domain, c.service, c.entity_id) for c in new_calls[len(expected.service_calls) :]]
            raise AssertionError(
                f"[{scenario.id}] unexpected extra service calls: {extra}\n"
                f"all calls: {[(c.domain, c.service, c.entity_id) for c in new_calls]}"
            )
        for i, exp in enumerate(expected.service_calls):
            if i >= len(new_calls):
                raise AssertionError(
                    f"[{scenario.id}] missing expected service call #{i}: "
                    f"{exp.domain}.{exp.service} on {exp.target_entity}\n"
                    f"recorded: {[(c.domain, c.service, c.entity_id) for c in new_calls]}"
                )
            actual = new_calls[i]
            if actual.domain != exp.domain or actual.service != exp.service:
                raise AssertionError(
                    f"[{scenario.id}] service call #{i} mismatch: expected "
                    f"{exp.domain}.{exp.service} got {actual.domain}.{actual.service}"
                )
            if exp.target_entity and actual.entity_id != exp.target_entity:
                raise AssertionError(
                    f"[{scenario.id}] service call #{i} target mismatch: expected "
                    f"{exp.target_entity!r} got {actual.entity_id!r}"
                )
            for key in exp.service_data_keys:
                if key not in actual.service_data:
                    raise AssertionError(
                        f"[{scenario.id}] service call #{i} missing expected key {key!r} in service_data={actual.service_data}"
                    )
            for key, val in exp.service_data.items():
                if actual.service_data.get(key) != val:
                    raise AssertionError(
                        f"[{scenario.id}] service call #{i} {key}={actual.service_data.get(key)!r} expected {val!r}"
                    )
    elif new_calls and not expected.allow_extra_calls:
        unexpected = [(c.domain, c.service, c.entity_id) for c in new_calls]
        raise AssertionError(f"[{scenario.id}] expected no service calls, got: {unexpected}")


@pytest.mark.parametrize("scenario_path", _params())
@pytest.mark.integration
@pytest.mark.asyncio
async def test_scenario_rest(scenario_path: Path, tmp_path):
    """Run a YAML scenario through the REST endpoint and assert speech + service calls."""
    from tests.conftest import build_scenario_backed_app
    from tests.helpers import HAMimicClient

    scenario = load_scenario(scenario_path)
    db_path = tmp_path / "scenario.db"
    app = build_scenario_backed_app(scenario, db_path)
    conversation_id = scenario.context.conversation_id or f"scenario-{scenario.id}"

    async with HAMimicClient(app) as client:
        response = await client.rest_turn(scenario.request_text, conversation_id=conversation_id)

    speech = response.get("speech", "")
    _assert_speech(scenario, speech)
    _assert_service_calls(scenario, client.app.state.ha_client)


@pytest.mark.parametrize("scenario_path", _params())
@pytest.mark.integration
@pytest.mark.asyncio
async def test_scenario_ws(scenario_path: Path, tmp_path):
    """Run a YAML scenario through the WebSocket endpoint and assert speech + service calls."""
    from tests.conftest import build_scenario_backed_app
    from tests.helpers import HAMimicClient

    scenario = load_scenario(scenario_path)
    db_path = tmp_path / "scenario.db"
    app = build_scenario_backed_app(scenario, db_path)
    conversation_id = scenario.context.conversation_id or f"scenario-{scenario.id}"

    async with HAMimicClient(app) as client:
        await client.connect_ws()
        tokens = await client.send_turn(scenario.request_text, conversation_id=conversation_id)

    done_frame = tokens[-1]
    speech = done_frame.get("mediated_speech") or done_frame.get("token", "")
    _assert_speech(scenario, speech)
    _assert_service_calls(scenario, client.app.state.ha_client)


@pytest.mark.parametrize("scenario_path", _params())
@pytest.mark.integration
@pytest.mark.asyncio
async def test_scenario_rest_ws_speech_parity(scenario_path: Path, tmp_path):
    """REST and WS must produce identical speech for the same scenario turn."""
    from tests.conftest import build_scenario_backed_app
    from tests.helpers import HAMimicClient

    scenario = load_scenario(scenario_path)
    conversation_id = scenario.context.conversation_id or f"scenario-{scenario.id}"

    # Use separate app instances so the deterministic LLM stub queues are independent.
    db_path_rest = tmp_path / "scenario_rest.db"
    app_rest = build_scenario_backed_app(scenario, db_path_rest)
    async with HAMimicClient(app_rest) as client:
        rest_resp = await client.rest_turn(scenario.request_text, conversation_id=conversation_id)

    db_path_ws = tmp_path / "scenario_ws.db"
    app_ws = build_scenario_backed_app(scenario, db_path_ws)
    async with HAMimicClient(app_ws) as client:
        await client.connect_ws()
        ws_tokens = await client.send_turn(scenario.request_text, conversation_id=conversation_id)

    rest_speech = rest_resp.get("speech", "")
    ws_speech = ws_tokens[-1].get("mediated_speech") or ws_tokens[-1].get("token", "")
    assert rest_speech == ws_speech, (
        f"[{scenario.id}] REST/WS speech mismatch:\n  REST: {rest_speech!r}\n  WS:   {ws_speech!r}"
    )
