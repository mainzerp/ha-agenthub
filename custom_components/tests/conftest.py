import sys
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum, IntFlag
from pathlib import Path
from types import ModuleType
from typing import ClassVar
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


_UNDEFINED = object()


class _MockConfigFlow:
    # Mirrors ConfigFlow.source; tests set it to "reauth" / "reconfigure".
    source = "user"

    def __init_subclass__(cls, **kwargs):
        pass

    def async_update_reload_and_abort(
        self,
        entry,
        *,
        unique_id=_UNDEFINED,
        title=_UNDEFINED,
        data=_UNDEFINED,
        data_updates=_UNDEFINED,
        options=_UNDEFINED,
        reason=_UNDEFINED,
        reload_even_if_entry_is_unchanged=True,
    ):
        """Same contract as HA core: update, schedule reload, then abort."""
        if data_updates is not _UNDEFINED:
            if data is not _UNDEFINED:
                raise ValueError("Cannot set both data and data_updates")
            data = {**entry.data, **data_updates}
        kwargs = {
            key: value
            for key, value in (
                ("unique_id", unique_id),
                ("title", title),
                ("data", data),
                ("options", options),
            )
            if value is not _UNDEFINED
        }
        self.hass.config_entries.async_update_entry(entry, **kwargs)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        if reason is _UNDEFINED:
            reason = "reauth_successful"
            if self.source == "reconfigure":
                reason = "reconfigure_successful"
        return self.async_abort(reason=reason)

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


def _mock_async_redact_data(data, to_redact):
    """Same semantics as homeassistant.components.diagnostics.async_redact_data."""
    if isinstance(data, list):
        return [_mock_async_redact_data(item, to_redact) for item in data]
    if not isinstance(data, dict):
        return data
    redacted = {**data}
    for key, value in redacted.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if key in to_redact:
            redacted[key] = "**REDACTED**"
        elif isinstance(value, (dict, list)):
            redacted[key] = _mock_async_redact_data(value, to_redact)
    return redacted


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


class _FakeSelectSelectorConfig:
    def __init__(self, options=None, mode=None, **kwargs):
        self.options = options
        self.mode = mode


class _FakeSelectSelector:
    def __init__(self, config=None):
        self.config = config


class _FakeEntitySelectorConfig(dict):
    """Mirrors HA's TypedDict config: a plain dict of the keyword arguments."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class _FakeEntitySelector:
    def __init__(self, config=None):
        self.config = config


class _FakeAudioCodecs(str, Enum):
    PCM = "pcm"
    OPUS = "opus"


class _FakeAudioFormats(str, Enum):
    WAV = "wav"
    OGG = "ogg"


class _FakeSpeechResultState(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class _FakeSpeechMetadata:
    language: str
    format: str
    codec: str
    bit_rate: int
    sample_rate: int
    channel: int


@dataclass
class _FakeSpeechResult:
    text: str | None
    result: _FakeSpeechResultState


@dataclass(frozen=True)
class _FakeSpeechAudioProcessing:
    requires_external_vad: bool
    prefers_auto_gain_enabled: bool
    prefers_noise_reduction_enabled: bool


class _FakeSpeechToTextEntity:
    """Minimal stand-in for HA's SpeechToTextEntity / Entity base."""

    hass = None
    entity_id = None

    @property
    def available(self):
        return True

    def check_metadata(self, metadata):
        # Same exact-membership semantics as HA core.
        return not (
            metadata.language not in self.supported_languages
            or metadata.format not in self.supported_formats
            or metadata.codec not in self.supported_codecs
            or metadata.bit_rate not in self.supported_bit_rates
            or metadata.sample_rate not in self.supported_sample_rates
            or metadata.channel not in self.supported_channels
        )

    def async_on_remove(self, func):
        self.__dict__.setdefault("_on_remove", []).append(func)

    def async_write_ha_state(self):
        self.__dict__["state_writes"] = self.__dict__.get("state_writes", 0) + 1

    async def async_added_to_hass(self):
        return None

    async def async_will_remove_from_hass(self):
        return None


def _fake_async_get_speech_to_text_entity(hass, entity_id):
    return None


class _MockConversationEntityFeature:
    CONTROL = "control"


class _MockConversationEntity:
    # Mirrors HA's Entity base: entity_id is None until the platform assigns it.
    entity_id = None


class _MockConversationResult:
    def __init__(
        self, response=None, conversation_id=None, continue_conversation=False
    ):
        self.response = response
        self.conversation_id = conversation_id
        self.continue_conversation = continue_conversation


class _MockAssistantContent:
    def __init__(self, agent_id=None, content=None, **kwargs):
        self.agent_id = agent_id
        self.content = content


class _MockAddConfigEntryEntitiesCallback:
    pass


@dataclass
class _MockAttachment:
    media_content_id: str
    mime_type: str
    path: Path


class _MockAITaskEntityFeature(IntFlag):
    GENERATE_DATA = 1
    SUPPORT_ATTACHMENTS = 2
    GENERATE_IMAGE = 4


class _MockAITaskEntity:
    """Minimal stand-in for HA's AITaskEntity / Entity base."""

    hass = None
    entity_id = None


@dataclass
class _MockGenDataTask:
    name: str
    instructions: str
    structure: object = None
    attachments: list | None = None
    llm_api: object = None


@dataclass
class _MockGenDataTaskResult:
    conversation_id: str
    data: object


class _MockHomeAssistantView:
    """Mirrors the HomeAssistantView class attributes the bridge sets."""

    url = None
    extra_urls: ClassVar[list] = []
    requires_auth = True
    cors_allowed = False
    name = None


def _mock_websocket_command(schema):
    """Keep the handler callable as-is and record the command schema."""

    def decorator(func):
        func._ws_schema = schema
        return func

    return decorator


def _mock_async_response(func):
    return func


def _mock_event_message(iden, event):
    return {"id": iden, "type": "event", "event": event}


def _mock_selector_serializer(schema):
    return {"selector_serializer": True}


def _mock_voluptuous_openapi_convert(schema, *, custom_serializer=None):
    return {"converted": schema, "custom_serializer": custom_serializer}


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
            "SOURCE_REAUTH": "reauth",
            "SOURCE_RECONFIGURE": "reconfigure",
        },
        "homeassistant.const": {
            "CONF_URL": "url",
            "CONF_API_KEY": "api_key",
            "Platform": type(
                "Platform",
                (),
                {"AI_TASK": "ai_task", "CONVERSATION": "conversation", "STT": "stt"},
            ),
            "MATCH_ALL": "*",
        },
        "homeassistant.core": {
            "HomeAssistant": MagicMock,
            "Event": MagicMock,
            "callback": lambda func: func,
        },
        "homeassistant.exceptions": {
            "HomeAssistantError": Exception,
            "ConfigEntryError": type("ConfigEntryError", (Exception,), {}),
            "ConfigEntryAuthFailed": type("ConfigEntryAuthFailed", (Exception,), {}),
            "ConfigEntryNotReady": type("ConfigEntryNotReady", (Exception,), {}),
        },
        "homeassistant.loader": {"async_get_integration": MagicMock()},
        "homeassistant.helpers": {},
        "homeassistant.helpers.llm": {
            "selector_serializer": _mock_selector_serializer,
        },
        "homeassistant.helpers.aiohttp_client": {
            "async_get_clientsession": MagicMock,
        },
        "homeassistant.helpers.chat_session": {
            "current_session": ContextVar("current_session", default=None),
        },
        "homeassistant.helpers.event": {
            "async_track_state_change_event": MagicMock(),
        },
        "homeassistant.helpers.start": {"async_at_started": MagicMock()},
        "homeassistant.helpers.selector": {
            "TextSelector": _FakeTextSelector,
            "TextSelectorConfig": _FakeTextSelectorConfig,
            "TextSelectorType": _FakeTextSelectorType,
            "SelectSelector": _FakeSelectSelector,
            "SelectSelectorConfig": _FakeSelectSelectorConfig,
            "EntitySelector": _FakeEntitySelector,
            "EntitySelectorConfig": _FakeEntitySelectorConfig,
        },
        "homeassistant.components": {},
        "homeassistant.helpers.config_validation": {
            "config_entry_only_config_schema": lambda domain: MagicMock(),
        },
        "homeassistant.components.websocket_api": {
            "websocket_command": _mock_websocket_command,
            "async_response": _mock_async_response,
            "event_message": _mock_event_message,
            "async_register_command": MagicMock(),
        },
        "homeassistant.components.http": {"HomeAssistantView": _MockHomeAssistantView},
        "homeassistant.components.frontend": {"add_extra_js_url": MagicMock()},
        "homeassistant.components.diagnostics": {
            "REDACTED": "**REDACTED**",
            "async_redact_data": _mock_async_redact_data,
        },
        "homeassistant.components.assist_pipeline": {},
        "homeassistant.components.ai_task": {
            "AITaskEntity": _MockAITaskEntity,
            "AITaskEntityFeature": _MockAITaskEntityFeature,
            "GenDataTask": _MockGenDataTask,
            "GenDataTaskResult": _MockGenDataTaskResult,
        },
        "homeassistant.components.stt": {
            "AudioBitRates": int,
            "AudioChannels": int,
            "AudioCodecs": _FakeAudioCodecs,
            "AudioFormats": _FakeAudioFormats,
            "AudioSampleRates": int,
            "DEFAULT_AUDIO_PROCESSING": _FakeSpeechAudioProcessing(
                requires_external_vad=True,
                prefers_auto_gain_enabled=True,
                prefers_noise_reduction_enabled=True,
            ),
            "SpeechMetadata": _FakeSpeechMetadata,
            "SpeechResult": _FakeSpeechResult,
            "SpeechResultState": _FakeSpeechResultState,
            "SpeechToTextEntity": _FakeSpeechToTextEntity,
            "async_get_speech_to_text_entity": _fake_async_get_speech_to_text_entity,
        },
        "homeassistant.components.conversation": {
            "ConversationEntity": _MockConversationEntity,
            "ConversationEntityFeature": _MockConversationEntityFeature,
            "ConversationResult": _MockConversationResult,
            "AssistantContent": _MockAssistantContent,
            "AssistantContentDeltaDict": dict,
            "Attachment": _MockAttachment,
            "ChatLog": MagicMock,
        },
        "homeassistant.helpers.device_registry": {
            "DeviceInfo": MagicMock,
            "DeviceEntryType": type("DeviceEntryType", (), {"SERVICE": "service"}),
        },
        "homeassistant.helpers.entity_registry": {
            "EVENT_ENTITY_REGISTRY_UPDATED": "entity_registry_updated",
            "async_get": MagicMock(),
            "async_entries_for_config_entry": MagicMock(return_value=[]),
        },
        "homeassistant.helpers.intent": {"IntentResponse": MagicMock},
        "homeassistant.helpers.entity_platform": {
            "AddConfigEntryEntitiesCallback": _MockAddConfigEntryEntitiesCallback,
        },
        "voluptuous_openapi": {"convert": _mock_voluptuous_openapi_convert},
        "voluptuous": {
            "Schema": MagicMock,
            "Required": MagicMock,
            "Optional": MagicMock,
        },
    }
    # Prefer the real aiohttp (a container requirement): the card view tests
    # drive a real aiohttp server. The stand-in is only a fallback.
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        mocks["aiohttp"] = {
            "ClientSession": MagicMock,
            "ClientTimeout": MagicMock,
            "ClientError": Exception,
            "ClientWebSocketResponse": MagicMock,
            "WSMsgType": type("WSMsgType", (), {"TEXT": 1}),
        }
        mocks["aiohttp.web"] = {"FileResponse": MagicMock, "Request": MagicMock}
    created = []
    for mod_name, attrs in mocks.items():
        if mod_name not in sys.modules:
            _make_mock_module(mod_name, attrs)
            created.append(mod_name)

    yield

    for mod_name in created:
        sys.modules.pop(mod_name, None)
