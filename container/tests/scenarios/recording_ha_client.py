"""In-memory recording HA client for real-scenario tests.

Implements the subset of ``HARestClient`` methods used by the orchestrator
pipeline and the routable agents. ``call_service`` records every call and
mutates the in-memory state to a plausible post-call value so subsequent
``get_state`` lookups (and the ``expect_state`` shim) succeed without
hitting any network.
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class CallRecord:
    domain: str
    service: str
    entity_id: str | None
    service_data: dict[str, Any] = field(default_factory=dict)


class RecordingHaClient:
    """Records HA service calls; mutates in-memory state on common actions."""

    def __init__(
        self,
        states: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> None:
        # Deep-copy so test mutations don't leak across scenarios.
        self._states: dict[str, dict[str, Any]] = {s["entity_id"]: copy.deepcopy(s) for s in states}
        self._config: dict[str, Any] = copy.deepcopy(config or {})
        self.calls: list[CallRecord] = []
        # WS observer compatibility: production code may call
        # set_state_observer; we accept and ignore it.

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    def apply_overrides(self, overrides: list) -> None:
        """Apply scenario preconditions.entity_overrides in-place."""
        for ov in overrides:
            ent = self._states.get(ov.entity_id)
            if not ent:
                # Allow scenarios to introduce new entities.
                ent = {
                    "entity_id": ov.entity_id,
                    "state": ov.state or "unknown",
                    "attributes": {},
                    "last_changed": _now_iso(),
                    "last_updated": _now_iso(),
                    "context": {"id": "fixture", "parent_id": None, "user_id": None},
                }
                self._states[ov.entity_id] = ent
            if ov.state is not None:
                ent["state"] = ov.state
            if ov.attributes:
                ent.setdefault("attributes", {}).update(ov.attributes)

    async def get_states(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(s) for s in self._states.values()]

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        s = self._states.get(entity_id)
        return copy.deepcopy(s) if s else None

    async def get_config(self) -> dict[str, Any]:
        return copy.deepcopy(self._config)

    async def get_services(self) -> dict[str, Any]:
        # Minimal -- agents do not depend on this for the scenario suite.
        return {}

    async def fire_event(self, event_type: str, event_data: dict | None = None) -> dict[str, Any]:
        return {"success": True}

    async def render_template(self, template: str, variables: dict | None = None) -> str | None:
        return None

    async def get_history_period(self, *_args, **_kwargs) -> list[list[dict[str, Any]]]:
        return []

    def set_state_observer(self, _ws_client) -> None:
        return None

    # ------------------------------------------------------------------
    # Automation config helpers (used by automation_executor CRUD)
    # ------------------------------------------------------------------

    async def get_automation_config(self, automation_id: str) -> dict[str, Any] | None:
        auts = self._config.get("automations", {})
        cfg = auts.get(automation_id)
        if cfg:
            return copy.deepcopy(cfg)
        # Fallback: synthesize a minimal config from entity state for scenario tests
        for state in self._states.values():
            if state.get("attributes", {}).get("id") == automation_id:
                friendly_name = state.get("attributes", {}).get("friendly_name", automation_id)
                return {
                    "alias": friendly_name,
                    "trigger": [],
                    "condition": [],
                    "action": [],
                }
        return None

    async def save_automation_config(self, automation_id: str, config: dict[str, Any]) -> dict[str, Any]:
        self._config.setdefault("automations", {})[automation_id] = copy.deepcopy(config)
        return {"success": True}

    async def delete_automation_config(self, automation_id: str) -> dict[str, Any]:
        self._config.get("automations", {}).pop(automation_id, None)
        self._states.pop(f"automation.{automation_id}", None)
        return {"success": True}

    # ------------------------------------------------------------------
    # call_service + state mutations
    # ------------------------------------------------------------------

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        service_data: dict[str, Any] | None = None,
        *,
        return_response: bool = False,
    ) -> dict[str, Any]:
        sd = dict(service_data or {})
        # Some agents pass entity_id inside service_data; normalize.
        if entity_id is None and "entity_id" in sd:
            entity_id = sd.get("entity_id")
        rec = CallRecord(domain=domain, service=service, entity_id=entity_id, service_data=sd)
        self.calls.append(rec)
        self._mutate_state(domain, service, entity_id, sd)
        return {"success": True}

    def _mutate_state(
        self,
        domain: str,
        service: str,
        entity_id: str | None,
        sd: dict[str, Any],
    ) -> None:
        if not entity_id or entity_id not in self._states:
            return
        ent = self._states[entity_id]
        attrs = ent.setdefault("attributes", {})
        now = _now_iso()
        ent["last_updated"] = now
        ent["last_changed"] = now
        # Light / switch on/off semantics
        if (domain, service) in {
            ("light", "turn_on"),
            ("switch", "turn_on"),
            ("input_boolean", "turn_on"),
            ("automation", "turn_on"),
            ("camera", "turn_on"),
        }:
            ent["state"] = "on"
        elif (domain, service) in {
            ("light", "turn_off"),
            ("switch", "turn_off"),
            ("input_boolean", "turn_off"),
            ("automation", "turn_off"),
            ("camera", "turn_off"),
        }:
            ent["state"] = "off"
        elif (domain, service) in {("light", "toggle"), ("switch", "toggle")}:
            ent["state"] = "off" if ent.get("state") == "on" else "on"
        elif domain == "light":
            # Brightness/color; ensure on.
            ent["state"] = "on"
            for key in (
                "brightness",
                "color_temp",
                "color_temp_kelvin",
                "rgb_color",
                "hs_color",
                "xy_color",
                "color_name",
                "transition",
                "effect",
            ):
                if key in sd:
                    attrs[key] = sd[key]
        elif domain == "climate":
            if service == "set_temperature":
                if "temperature" in sd:
                    attrs["temperature"] = sd["temperature"]
            elif service == "set_hvac_mode":
                ent["state"] = sd.get("hvac_mode", ent.get("state"))
            elif service == "set_fan_mode":
                attrs["fan_mode"] = sd.get("fan_mode")
            elif service == "set_humidity":
                attrs["humidity"] = sd.get("humidity")
            elif service == "turn_off":
                ent["state"] = "off"
        elif domain == "media_player":
            if service in {"turn_on", "media_play"}:
                ent["state"] = "on" if service == "turn_on" else "playing"
            elif service in {"turn_off"}:
                ent["state"] = "off"
            elif service == "media_pause":
                ent["state"] = "paused"
            elif service == "media_stop":
                ent["state"] = "off"
            elif service == "volume_set" and "volume_level" in sd:
                attrs["volume_level"] = sd["volume_level"]
            elif service == "volume_mute":
                attrs["is_volume_muted"] = bool(sd.get("is_volume_muted"))
            elif service == "select_source":
                attrs["source"] = sd.get("source")
        elif domain == "scene" and service == "turn_on":
            ent["state"] = _now_iso()
        elif domain == "lock":
            ent["state"] = "locked" if service == "lock" else ("unlocked" if service == "unlock" else ent.get("state"))
        elif domain == "alarm_control_panel":
            mapping = {
                "alarm_arm_home": "armed_home",
                "alarm_arm_away": "armed_away",
                "alarm_arm_night": "armed_night",
                "alarm_disarm": "disarmed",
            }
            if service in mapping:
                ent["state"] = mapping[service]
        elif domain == "timer":
            mapping = {"start": "active", "pause": "paused", "cancel": "idle", "finish": "idle"}
            if service in mapping:
                ent["state"] = mapping[service]
        elif domain == "cover":
            if service == "open_cover":
                ent["state"] = "open"
            elif service == "close_cover":
                ent["state"] = "closed"
        elif domain == "input_datetime":
            if "time" in sd:
                attrs["time"] = sd["time"]
            elif "datetime" in sd:
                attrs["datetime"] = sd["datetime"]
        elif domain == "mass" or domain == "music_assistant":
            ent["state"] = "playing"
        # notify / tts / persistent_notification: no state changes, just record.

    # ------------------------------------------------------------------
    # expect_state shim (no WS in tests). Resolves immediately based on
    # the post-mutation state.
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def expect_state(
        self,
        entity_id: str,
        *,
        expected: str | None = None,
        timeout: float = 1.5,
        poll_interval: float = 0.25,
        poll_max: float = 1.0,
    ):
        # Yield a result-collector; resolve on exit.
        result: dict[str, Any] = {"resolved": False, "state": None, "new_state": None}
        try:
            yield result
        finally:
            ent = self._states.get(entity_id)
            new_state = ent.get("state") if ent else None
            result["state"] = new_state
            result["new_state"] = new_state
            if expected is None:
                result["resolved"] = ent is not None
            else:
                result["resolved"] = bool(ent and ent.get("state") == expected)

    # ------------------------------------------------------------------
    # Assertion helpers (used by runner)
    # ------------------------------------------------------------------

    def find_call(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
    ) -> CallRecord | None:
        for c in self.calls:
            if c.domain != domain or c.service != service:
                continue
            if entity_id is not None and c.entity_id != entity_id:
                continue
            return c
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
