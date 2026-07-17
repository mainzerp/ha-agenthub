import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
import pytest

_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def _make_mock_module(name: str, attrs: dict | None = None) -> ModuleType:
    mod = ModuleType(name)
    for key, val in (attrs or {}).items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


class _MockConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, *, step_id=None, data_schema=None, errors=None, **kwargs):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }

    def async_create_entry(self, *, title=None, data=None):
        return {"type": "create_entry", "title": title, "data": data}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}


class _MockConfigFlowResult:
    pass


class _MockOptionsFlow:
    def async_show_form(self, *, step_id=None, data_schema=None, errors=None, **kwargs):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }

    def async_create_entry(self, *, title=None, data=None):
        return {"type": "create_entry", "title": title, "data": data}


class _FakeTextSelectorType:
    PASSWORD = "password"
    URL = "url"


class _FakeTextSelectorConfig:
    def __init__(self, *, type=None):
        self.type = type


class _FakeTextSelector:
    def __init__(self, config=None):
        self.config = config


class _MockConversationEntityFeature:
    CONTROL = "control"


class _MockConversationEntity:
    pass


class _MockConversationResult:
    def __init__(self, response=None, conversation_id=None, continue_conversation=True):
        self.response = response
        self.conversation_id = conversation_id


class _MockAddConfigEntryEntitiesCallback:
    pass


@pytest.fixture(autouse=True, scope="session")
def _mock_homeassistant_deps():
    """Inject minimal mocks for homeassistant imports."""
    mocks = {
        "homeassistant": MagicMock(),
        "homeassistant.config_entries": {
            "ConfigEntry": MagicMock,
            "ConfigFlow": _MockConfigFlow,
            "ConfigFlowResult": _MockConfigFlowResult,
            "OptionsFlow": _MockOptionsFlow,
        },
        "homeassistant.const": {
            "CONF_URL": "url",
            "CONF_API_KEY": "api_key",
            "Platform": type("Platform", (), {"CONVERSATION": "conversation"}),
            "MATCH_ALL": "*",
        },
        "homeassistant.core": {"HomeAssistant": MagicMock},
        "homeassistant.exceptions": {
            "HomeAssistantError": Exception,
            "ConfigEntryError": type("ConfigEntryError", (Exception,), {}),
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.aiohttp_client": {
            "async_get_clientsession": MagicMock,
        },
        "homeassistant.helpers.selector": {
            "TextSelector": _FakeTextSelector,
            "TextSelectorConfig": _FakeTextSelectorConfig,
            "TextSelectorType": _FakeTextSelectorType,
        },
        "homeassistant.components": {},
        "homeassistant.components.assist_pipeline": {},
        "homeassistant.components.conversation": {
            "ConversationEntity": _MockConversationEntity,
            "ConversationEntityFeature": _MockConversationEntityFeature,
            "ConversationResult": _MockConversationResult,
        },
        "homeassistant.helpers.device_registry": {
            "DeviceInfo": MagicMock,
            "DeviceEntryType": type("DeviceEntryType", (), {"SERVICE": "service"}),
        },
        "homeassistant.helpers.entity_registry": MagicMock(),
        "homeassistant.helpers.intent": {"IntentResponse": MagicMock},
        "homeassistant.helpers.entity_platform": {
            "AddConfigEntryEntitiesCallback": _MockAddConfigEntryEntitiesCallback,
        },
        "homeassistant.helpers.event": {
            "async_track_state_change_event": MagicMock,
        },
        "voluptuous": {
            "Schema": MagicMock,
            "Required": MagicMock,
            "Optional": MagicMock,
        },
        "aiohttp": {
            "ClientSession": MagicMock,
            "ClientTimeout": MagicMock,
            "ClientError": Exception,
            "ClientWebSocketResponse": MagicMock,
            "WSMsgType": type("WSMsgType", (), {"TEXT": 1}),
        },
    }
    created = []
    for mod_name, attrs in mocks.items():
        if mod_name not in sys.modules:
            _make_mock_module(mod_name, attrs)
            created.append(mod_name)

    yield

    for mod_name in created:
        sys.modules.pop(mod_name, None)
