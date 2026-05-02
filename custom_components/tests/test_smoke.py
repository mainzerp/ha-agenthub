"""Smoke tests for the HA-AgentHub custom integration.

These tests mock homeassistant dependencies so the integration can be imported
and exercised in CI without installing the full HA core package.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock
import pytest


def _make_mock_module(name: str, attrs: dict | None = None) -> ModuleType:
    mod = ModuleType(name)
    for key, val in (attrs or {}).items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


@pytest.fixture(autouse=True)
def _mock_homeassistant_deps():
    """Inject minimal mocks for homeassistant imports."""
    mocks = {
        "homeassistant": {},
        "homeassistant.config_entries": {
            "ConfigEntry": MagicMock,
            "ConfigFlow": MagicMock,
            "ConfigFlowResult": MagicMock,
            "OptionsFlow": MagicMock,
        },
        "homeassistant.const": {
            "CONF_URL": "url",
            "CONF_API_KEY": "api_key",
            "Platform": type("Platform", (), {"CONVERSATION": "conversation"}),
            "MATCH_ALL": "*",
        },
        "homeassistant.core": {"HomeAssistant": MagicMock},
        "homeassistant.helpers": {},
        "homeassistant.helpers.selector": {
            "TextSelector": MagicMock,
            "TextSelectorConfig": MagicMock,
            "TextSelectorType": MagicMock,
        },
        "homeassistant.components": {},
        "homeassistant.components.assist_pipeline": {},
        "homeassistant.components.conversation": {
            "ConversationEntityFeature": MagicMock,
        },
        "homeassistant.helpers.device_registry": MagicMock(),
        "homeassistant.helpers.entity_registry": MagicMock(),
        "homeassistant.helpers.intent": MagicMock(),
        "homeassistant.helpers.entity_platform": {
            "AddConfigEntryEntitiesCallback": MagicMock,
        },
        "homeassistant.helpers.event": {
            "async_track_state_change_event": MagicMock,
        },
        "voluptuous": MagicMock(),
        "aiohttp": MagicMock(),
    }
    created = []
    for mod_name, attrs in mocks.items():
        if mod_name not in sys.modules:
            _make_mock_module(mod_name, attrs)
            created.append(mod_name)

    yield

    for mod_name in created:
        sys.modules.pop(mod_name, None)


def test_import_const():
    """Import the constants module."""
    from custom_components.ha_agenthub import const

    assert const.DOMAIN == "ha_agenthub"
    assert const.INTEGRATION_TITLE == "HA-AgentHub"


def test_import_init():
    """Import the package __init__.py."""
    from custom_components import ha_agenthub

    assert ha_agenthub.DOMAIN == "ha_agenthub"
