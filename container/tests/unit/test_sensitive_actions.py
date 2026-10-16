"""Sensitive-action policy: classification, approval token, settings and the write backstop."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.automation_executor import _access_cover_problems, _validate_automation_config
from app.agents.pending_store import PendingStore
from app.ha_client.rest import HARestClient
from app.security import sensitive_actions as sa
from app.security.sensitive_actions import (
    ApprovalSettings,
    SensitiveActionBlocked,
    approved_calls,
    classify_sensitive,
    is_call_approved,
    parse_allowlist,
)

# ---------------------------------------------------------------------------
# classify_sensitive matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "service", "device_class", "expected"),
    [
        ("lock", "unlock", None, sa.REASON_LOCK_OPEN),
        ("lock", "open", None, sa.REASON_LOCK_OPEN),
        ("lock", "lock", None, None),
        ("alarm_control_panel", "alarm_disarm", None, sa.REASON_ALARM_DISARM),
        ("alarm_control_panel", "alarm_arm_home", None, sa.REASON_ALARM_ARM),
        ("alarm_control_panel", "alarm_arm_away", None, sa.REASON_ALARM_ARM),
        ("alarm_control_panel", "alarm_arm_night", None, sa.REASON_ALARM_ARM),
        ("alarm_control_panel", "alarm_arm_vacation", None, sa.REASON_ALARM_ARM),
        ("alarm_control_panel", "alarm_arm_custom_bypass", None, sa.REASON_ALARM_ARM),
        ("alarm_control_panel", "alarm_trigger", None, None),
        ("cover", "open_cover", "garage", sa.REASON_ACCESS_COVER_OPEN),
        ("cover", "open_cover", "gate", sa.REASON_ACCESS_COVER_OPEN),
        ("cover", "open_cover", "door", sa.REASON_ACCESS_COVER_OPEN),
        ("cover", "set_cover_position", "garage", sa.REASON_ACCESS_COVER_OPEN),
        ("cover", "toggle", "Gate", sa.REASON_ACCESS_COVER_OPEN),
        ("cover", "close_cover", "garage", None),
        ("cover", "stop_cover", "garage", None),
        ("cover", "open_cover_tilt", "garage", None),
        ("cover", "set_cover_tilt_position", "gate", None),
        ("cover", "open_cover", "blind", None),
        ("cover", "open_cover", "shutter", None),
        ("cover", "open_cover", None, None),
        ("light", "turn_on", None, None),
        ("switch", "turn_on", "garage", None),
    ],
)
def test_classify_sensitive_matrix(domain, service, device_class, expected):
    assert classify_sensitive(domain, service, f"{domain}.x", device_class) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ("", frozenset()),
        ("lock.front_door", frozenset({"lock.front_door"})),
        ("lock.front_door, cover.Garage_Door ,", frozenset({"lock.front_door", "cover.garage_door"})),
        ('["lock.a", "cover.b"]', frozenset({"lock.a", "cover.b"})),
        ("[not json", frozenset()),
        ('{"a": 1}', frozenset({'{"a": 1}'})),
    ],
)
def test_parse_allowlist(raw, expected):
    assert parse_allowlist(raw) == expected


def test_approval_token_is_scoped_to_exact_calls():
    assert not is_call_approved("lock", "unlock", "lock.front_door")
    with approved_calls([("lock", "unlock", "lock.front_door")]):
        assert is_call_approved("lock", "unlock", "lock.front_door")
        assert not is_call_approved("lock", "unlock", "lock.back_door")
        assert not is_call_approved("lock", "open", "lock.front_door")
    assert not is_call_approved("lock", "unlock", "lock.front_door")


async def test_settings_read_failure_fails_closed():
    with patch(
        "app.db.repositories.settings.SettingsRepository.get_value", new=AsyncMock(side_effect=RuntimeError("db"))
    ):
        settings = await sa.load_approval_settings()
    assert settings == ApprovalSettings(enabled=True, allowlist=frozenset())


# ---------------------------------------------------------------------------
# Generic pending store (shared TTL / tombstone logic)
# ---------------------------------------------------------------------------


def test_pending_store_ttl_tombstone_and_bound():
    store: PendingStore[str] = PendingStore(
        0.01, tombstone_ttl_seconds=60, max_entries=2, tombstone=lambda v: v.upper()
    )
    store.put("a", "secret-a")
    assert store.get_entry("a") == ("active", "secret-a")
    time.sleep(0.02)
    assert store.get_entry("a") == ("expired", "SECRET-A")
    assert store.pop("a") is None
    store.put("b", "x")
    store.put("c", "y")
    store.put("d", "z")
    assert store.get_entry("b") == ("none", None)
    assert len(store) == 2


# ---------------------------------------------------------------------------
# Backstop in HARestClient.call_service
# ---------------------------------------------------------------------------


def _rest_client(states: dict[str, dict] | None = None) -> HARestClient:
    client = HARestClient()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=[])
    client._client = MagicMock()
    client._client.post = AsyncMock(return_value=response)
    client._client.get = AsyncMock()
    known = states or {}
    client.get_state = AsyncMock(side_effect=lambda entity_id: known.get(entity_id))  # type: ignore[method-assign]
    return client


def _settings(enabled: bool = True, allowlist: frozenset[str] = frozenset()):
    async def _load() -> ApprovalSettings:
        return ApprovalSettings(enabled=enabled, allowlist=allowlist)

    return patch("app.security.sensitive_actions.load_approval_settings", new=_load)


class TestBackstop:
    async def test_unapproved_unlock_is_blocked_before_sending(self):
        client = _rest_client()
        with _settings(), pytest.raises(SensitiveActionBlocked) as exc:
            await client.call_service("lock", "unlock", "lock.front_door", {"code": "1234"})
        client._client.post.assert_not_awaited()
        assert "lock.front_door" in str(exc.value)
        assert "1234" not in str(exc.value)

    @pytest.mark.parametrize(
        ("domain", "service", "entity_id"),
        [
            ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home"),
            ("alarm_control_panel", "alarm_arm_away", "alarm_control_panel.home"),
            ("lock", "open", "lock.front_door"),
        ],
    )
    async def test_plugin_style_direct_calls_are_blocked(self, domain, service, entity_id):
        client = _rest_client()
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await client.call_service(domain, service, entity_id)
        client._client.post.assert_not_awaited()

    async def test_token_allows_exactly_the_approved_call(self):
        client = _rest_client()
        with _settings(), approved_calls([("lock", "unlock", "lock.front_door")]):
            await client.call_service("lock", "unlock", "lock.front_door")
            with pytest.raises(SensitiveActionBlocked):
                await client.call_service("lock", "unlock", "lock.back_door")
        client._client.post.assert_awaited_once()

    async def test_allowlist_permits(self):
        client = _rest_client()
        with _settings(allowlist=frozenset({"lock.front_door"})):
            await client.call_service("lock", "unlock", "lock.front_door")
            with pytest.raises(SensitiveActionBlocked):
                await client.call_service("lock", "unlock", "lock.back_door")
        client._client.post.assert_awaited_once()

    async def test_disabled_approval_permits_everything(self):
        client = _rest_client()
        with _settings(enabled=False):
            await client.call_service("lock", "unlock", "lock.front_door")
            await client.call_service("lock", "unlock", None, {"area_id": "hallway"})
        assert client._client.post.await_count == 2

    async def test_cover_device_class_from_registered_index(self):
        index = MagicMock()
        entries = {
            "cover.garage_door": SimpleNamespace(device_class="garage"),
            "cover.blinds": SimpleNamespace(device_class="blind"),
        }
        index.get_by_id_async = AsyncMock(side_effect=lambda entity_id: entries.get(entity_id))
        sa.register_entity_index(index)
        client = _rest_client()
        with _settings():
            await client.call_service("cover", "open_cover", "cover.blinds")
            await client.call_service("cover", "close_cover", "cover.garage_door")
            with pytest.raises(SensitiveActionBlocked):
                await client.call_service("cover", "open_cover", "cover.garage_door")
            with pytest.raises(SensitiveActionBlocked):
                await client.call_service("cover", "set_cover_position", "cover.garage_door", {"position": 40})
        assert client._client.post.await_count == 2

    async def test_cover_device_class_falls_back_to_state(self):
        client = _rest_client(
            {
                "cover.gate": {"state": "closed", "attributes": {"device_class": "gate"}},
                "cover.group": {"state": "closed", "attributes": {"entity_id": ["cover.a", "cover.b"]}},
            }
        )
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await client.call_service("cover", "open_cover", "cover.gate")
        # A state read without a device class (cover group): not sensitive.
        with _settings():
            await client.call_service("cover", "open_cover", "cover.group")
        # No state at all: the device class is unknown -> fail closed.
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await client.call_service("cover", "open_cover", "cover.unknown")
        client._client.post.assert_awaited_once()

    async def test_index_entry_without_device_class_falls_back_to_state(self):
        index = MagicMock()
        index.get_by_id_async = AsyncMock(return_value=SimpleNamespace(device_class=None))
        sa.register_entity_index(index)
        client = _rest_client({"cover.garage": {"state": "closed", "attributes": {"device_class": "garage"}}})
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await client.call_service("cover", "open_cover", "cover.garage")
        client._client.post.assert_not_awaited()

    async def test_failed_device_class_lookup_fails_closed(self):
        sa.register_entity_index(None)

        async def _boom(_entity_id):
            raise RuntimeError("HA REST hiccup")

        with _settings(), pytest.raises(SensitiveActionBlocked):
            await sa.enforce_sensitive_call_policy("cover", "open_cover", "cover.garage", None, state_fetcher=_boom)

    async def test_websocket_path_without_state_fetcher_fails_closed(self):
        index = MagicMock()
        index.get_by_id_async = AsyncMock(return_value=None)
        sa.register_entity_index(index)
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await sa.enforce_sensitive_call_policy("cover", "open_cover", "cover.garage", None)

    @pytest.mark.parametrize(("domain", "service"), [("lock", "UNLOCK"), ("LOCK", "unlock"), (" Lock ", "Unlock ")])
    async def test_case_and_whitespace_variants_are_blocked(self, domain, service):
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await sa.enforce_sensitive_call_policy(domain, service, "lock.front", None)
        assert await sa.sensitive_reason_for_entity(domain, service, "lock.front") == sa.REASON_LOCK_OPEN
        assert classify_sensitive(domain, service, "lock.front", None) == sa.REASON_LOCK_OPEN

    async def test_rest_websocket_fallback_is_not_double_blocked(self):
        import httpx

        client = _rest_client({"cover.blinds": {"state": "closed", "attributes": {"device_class": "blind"}}})
        client._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
        ws = MagicMock()
        ws.is_connected = MagicMock(return_value=True)
        ws.call_service = AsyncMock(return_value={"ok": True})
        client._state_observer = ws
        with _settings():
            assert await client.call_service("cover", "open_cover", "cover.blinds") == {"ok": True}
        ws.call_service.assert_awaited_once()

    @pytest.mark.parametrize(
        ("domain", "service", "entity_id", "data"),
        [
            ("lock", "unlock", None, None),
            ("lock", "unlock", "all", None),
            ("lock", "unlock", None, {"area_id": "hallway"}),
            ("cover", "open_cover", "all", None),
            ("cover", "open_cover", None, {"target": {"device_id": "abc"}}),
            ("lock", "unlock", None, {"entity_id": ["lock.a", "lock.b"]}),
        ],
    )
    async def test_indirect_or_missing_targets_fail_closed(self, domain, service, entity_id, data):
        client = _rest_client()
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await client.call_service(domain, service, entity_id, data)
        client._client.post.assert_not_awaited()

    async def test_websocket_service_commands_cannot_bypass(self):
        client = _rest_client()
        with pytest.raises(ValueError):
            await client.send_ws_command("call_service", domain="lock", service="unlock")

    async def test_websocket_client_call_service_is_guarded(self):
        from app.ha_client.websocket import HAWebSocketClient

        ws = HAWebSocketClient.__new__(HAWebSocketClient)
        ws.send_command = AsyncMock()  # type: ignore[method-assign]
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await ws.call_service("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home")
        ws.send_command.assert_not_awaited()


# ---------------------------------------------------------------------------
# Backstop on action-cache replay
# ---------------------------------------------------------------------------


class TestCacheReplayBackstop:
    async def test_cached_unlock_replay_raises_and_is_not_swallowed(self):
        from app.agents.cache_orchestrator import CacheOrchestrator
        from app.models.cache import CachedAction

        orch = CacheOrchestrator.__new__(CacheOrchestrator)
        orch._ha_client = _rest_client()
        cached = CachedAction(service="lock/unlock", entity_id="lock.front_door", service_data={})
        with _settings(), pytest.raises(SensitiveActionBlocked):
            await orch.execute_cached_action(cached)
        orch._ha_client._client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# Automation proposals: access covers may only be closed
# ---------------------------------------------------------------------------


def _cover_index() -> MagicMock:
    index = MagicMock()
    entries = {
        "cover.garage_door": SimpleNamespace(device_class="garage", entity_id="cover.garage_door"),
        "cover.blinds": SimpleNamespace(device_class="blind", entity_id="cover.blinds"),
    }
    index.get_by_id_async = AsyncMock(side_effect=lambda entity_id: entries.get(entity_id))
    return index


def _automation(action: dict) -> dict:
    return {"alias": "A", "triggers": [{"trigger": "time", "at": "07:00:00"}], "actions": [action]}


class TestAutomationAccessCovers:
    @pytest.mark.parametrize(
        "action",
        [
            {"action": "cover.open_cover", "target": {"entity_id": "cover.garage_door"}},
            {"service": "cover.set_cover_position", "entity_id": "cover.garage_door", "data": {"position": 50}},
            {"choose": [{"sequence": [{"action": "cover.toggle", "target": {"entity_id": ["cover.garage_door"]}}]}]},
        ],
    )
    async def test_opening_a_garage_door_is_refused(self, action):
        problems = await _access_cover_problems(_automation(action), _cover_index())
        assert problems and "cover.garage_door" in problems[0]

    @pytest.mark.parametrize(
        "action",
        [
            {"action": "cover.close_cover", "target": {"entity_id": "cover.garage_door"}},
            {"action": "cover.stop_cover", "target": {"entity_id": "cover.garage_door"}},
            {"action": "cover.open_cover", "target": {"entity_id": "cover.blinds"}},
        ],
    )
    async def test_closing_or_blinds_allowed(self, action):
        assert await _access_cover_problems(_automation(action), _cover_index()) == []

    @pytest.mark.parametrize(
        "action",
        [
            {"action": "cover.open_cover", "data_template": {"entity_id": "cover.garage_door"}},
            {"action": "cover.open_cover", "service_data": {"entity_id": "cover.garage_door"}},
            {"action": "cover.open_cover", "data": {"entity_id": "cover.garage_door"}},
            {"action": "cover.open_cover", "entity_id": "cover.garage_door"},
            {"action": "cover.open_cover", "target": {"entity_id": "{{ 'cover.garage_door' }}"}},
            {"action": "cover.open_cover", "target": "{{ {'entity_id': 'cover.garage_door'} }}"},
            {"action": "cover.open_cover", "data": {"options": {"entity_id": "cover.garage_door"}}},
            {"action": "cover.open_cover", "extra": [{"entity_id": "cover.garage_door"}]},
            {"action": "cover.open_cover", "target": {"entity_id": 42}},
        ],
    )
    async def test_every_target_container_and_unresolvable_targets_are_refused(self, action):
        assert await _access_cover_problems(_automation(action), _cover_index())

    async def test_cover_unknown_to_the_index_is_restricted(self):
        action = {"action": "cover.open_cover", "target": {"entity_id": "cover.not_indexed"}}
        assert await _access_cover_problems(_automation(action), _cover_index())
        assert await _access_cover_problems(_automation(action), None)

    async def test_index_entry_without_device_class_is_allowed(self):
        index = MagicMock()
        index.get_by_id_async = AsyncMock(return_value=SimpleNamespace(device_class=None))
        action = {"action": "cover.open_cover", "target": {"entity_id": "cover.group"}}
        assert await _access_cover_problems(_automation(action), index) == []

    def test_automations_may_still_arm_the_alarm(self):
        from app.agents.automation_executor import _service_allowed

        for service in ("alarm_arm_home", "alarm_arm_away", "alarm_arm_night"):
            assert _service_allowed(f"alarm_control_panel.{service}")
        assert not _service_allowed("alarm_control_panel.alarm_disarm")
        assert not _service_allowed("lock.unlock")

    async def test_validation_reports_the_refusal(self):
        config = _automation({"action": "cover.open_cover", "target": {"entity_id": "cover.garage_door"}})
        with patch("app.agents.automation_executor._entity_allowed", new=AsyncMock(return_value=True)):
            problems = await _validate_automation_config(config, _cover_index(), "automation-agent")
        assert any("only be closed" in p for p in problems)


# ---------------------------------------------------------------------------
# Delayed actions: refused at schedule time, blocked at fire time
# ---------------------------------------------------------------------------


async def _schedule(target_entity: str, target_action: str, device_class: str | None):
    from app.agents.timer_executor import execute_timer_action

    sched = MagicMock()
    sched.schedule = AsyncMock(return_value="t1")
    index = MagicMock()
    index.get_by_id_async = AsyncMock(return_value=SimpleNamespace(device_class=device_class))
    resolved = {"entity_id": target_entity, "friendly_name": "Target", "resolution": {}}
    ha = AsyncMock()
    with (
        patch("app.agents.timer_executor._helpers._get_scheduler", return_value=sched),
        patch(
            "app.agents.timer_executor._timers.resolve_and_validate_entity",
            new=AsyncMock(return_value=resolved),
        ),
    ):
        result = await execute_timer_action(
            {
                "action": "delayed_action",
                "entity": "later",
                "parameters": {
                    "delay_duration": "00:10:00",
                    "target_entity": target_entity,
                    "target_action": target_action,
                },
            },
            ha,
            index,
            MagicMock(),
            agent_id="timer-agent",
        )
    return result, sched


class TestDeferredActions:
    @pytest.mark.parametrize(
        ("target_entity", "target_action", "device_class"),
        [
            ("cover.garage_door", "cover/open_cover", "garage"),
            ("cover.gate", "cover/toggle", "gate"),
            ("alarm_control_panel.home", "alarm_control_panel/alarm_arm_away", None),
        ],
    )
    async def test_sensitive_delayed_action_is_refused_at_schedule_time(
        self, target_entity, target_action, device_class
    ):
        with _settings():
            result, sched = await _schedule(target_entity, target_action, device_class)
        assert result["success"] is False
        assert "confirmation" in result["speech"]
        sched.schedule.assert_not_called()

    async def test_blinds_and_closing_are_still_schedulable(self):
        with _settings():
            blinds, sched = await _schedule("cover.blinds", "cover/open_cover", "blind")
            closing, sched2 = await _schedule("cover.garage_door", "cover/close_cover", "garage")
        assert blinds["success"] is True and closing["success"] is True
        sched.schedule.assert_awaited_once()
        sched2.schedule.assert_awaited_once()

    async def test_allowlisted_garage_is_schedulable(self):
        with _settings(allowlist=frozenset({"cover.garage_door"})):
            result, sched = await _schedule("cover.garage_door", "cover/open_cover", "garage")
        assert result["success"] is True
        sched.schedule.assert_awaited_once()

    async def test_fire_time_backstop_blocks_and_announces(self):
        from app.agents import background_actions as ba
        from app.models.agent import BackgroundEvent

        index = MagicMock()
        index.get_by_id_async = AsyncMock(return_value=SimpleNamespace(device_class="garage"))
        sa.register_entity_index(index)
        client = _rest_client()
        event = BackgroundEvent(
            event_type="delayed_action",
            payload={"target_entity": "cover.garage_door", "target_action": "cover/open_cover"},
        )
        with (
            _settings(),
            patch("app.agents.background_actions.entity_is_visible", new=AsyncMock(return_value=True)),
            patch("app.agents.background_actions._announce_deferred_failure", new=AsyncMock()) as announce,
        ):
            result = await ba.handle_background_event(event, ha_client=client)
        client._client.post.assert_not_awaited()
        assert result["error"].startswith("Delayed action rejected")
        assert "action_executed" not in result
        announce.assert_awaited_once()
