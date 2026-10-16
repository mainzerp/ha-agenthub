"""Tests for the AI Task entity platform."""

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ENTRY_ID = "entry-1"
BASE_URL = "http://container:8080"
API_KEY = "secret-key"
CONVERSATION_ID = "01JAITASKCONVERSATION0000"


def _ai_task_module():
    from custom_components.ha_agenthub import ai_task

    return ai_task


def _client_module():
    from custom_components.ha_agenthub import client

    return client


def _ha_error():
    return sys.modules["homeassistant.exceptions"].HomeAssistantError


def _task(**kwargs):
    values = {"name": "Describe", "instructions": "Describe the image."}
    values.update(kwargs)
    return sys.modules["homeassistant.components.ai_task"].GenDataTask(**values)


def _attachment(path, mime_type):
    return sys.modules["homeassistant.components.conversation"].Attachment(
        media_content_id=f"media-source://test/{path.name}",
        mime_type=mime_type,
        path=path,
    )


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body


class _FakePost:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, status=200, body=None, exc=None):
        if body is None:
            body = {"conversation_id": "container-conv", "data": "ok", "text": "ok"}
        self._status = status
        self._body = body if isinstance(body, str) else json.dumps(body)
        self._exc = exc
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakePost(_FakeResponse(self._status, self._body), self._exc)


@pytest.fixture
def hass():
    hass = MagicMock()
    hass.config.language = "de"
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    return hass


@pytest.fixture
def entity(hass):
    entry = SimpleNamespace(entry_id=ENTRY_ID, title="HA-AgentHub", options={})
    ent = _ai_task_module().HaAgentHubAITaskEntity(entry, BASE_URL + "/", API_KEY)
    ent.hass = hass
    ent.entity_id = "ai_task.ha_agenthub_ai_task"
    return ent


@pytest.fixture
def chat_log():
    return SimpleNamespace(
        conversation_id=CONVERSATION_ID,
        llm_api=None,
        async_add_assistant_content_without_tools=MagicMock(),
    )


def _patch_session(session):
    return patch.object(
        _client_module(), "async_get_clientsession", return_value=session
    )


def _patch_timeout():
    return patch.object(
        _client_module().aiohttp,
        "ClientTimeout",
        side_effect=lambda total: ("timeout", total),
    )


# ---------------------------------------------------------------------------
# Platform and entity setup
# ---------------------------------------------------------------------------


class TestPlatformSetup:
    def test_platforms_include_ai_task(self):
        from custom_components.ha_agenthub import PLATFORMS

        assert "ai_task" in PLATFORMS
        assert "conversation" in PLATFORMS
        assert "stt" in PLATFORMS

    async def test_unload_unloads_ai_task_platform(self):
        from custom_components.ha_agenthub import DOMAIN, PLATFORMS, async_unload_entry

        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        hass.data = {DOMAIN: {ENTRY_ID: {"log_shipper": None}}}
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        assert await async_unload_entry(hass, entry) is True
        hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            entry, PLATFORMS
        )
        assert DOMAIN not in hass.data

    async def test_setup_entry_adds_one_entity(self, hass):
        ai_task = _ai_task_module()
        hass.data = {"ha_agenthub": {ENTRY_ID: {"url": BASE_URL, "api_key": API_KEY}}}
        entry = SimpleNamespace(entry_id=ENTRY_ID, title="HA-AgentHub", options={})
        added = []

        await ai_task.async_setup_entry(hass, entry, added.extend)

        assert len(added) == 1
        assert isinstance(added[0], ai_task.HaAgentHubAITaskEntity)
        assert added[0]._url == BASE_URL
        assert added[0]._api_key == API_KEY

    def test_entity_attributes(self, entity):
        features = sys.modules["homeassistant.components.ai_task"].AITaskEntityFeature

        assert entity._attr_unique_id == f"{ENTRY_ID}_ai_task"
        assert entity._attr_has_entity_name is True
        assert entity._attr_translation_key == "ai_task"
        assert entity._attr_supported_features == (
            features.GENERATE_DATA | features.SUPPORT_ATTACHMENTS
        )

    def test_device_info_matches_conversation_entity(self):
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
        )

        entry = SimpleNamespace(entry_id=ENTRY_ID, title="HA-AgentHub", options={})
        dr = sys.modules["homeassistant.helpers.device_registry"]
        with patch.object(dr, "DeviceInfo", dict):
            ai_entity = _ai_task_module().HaAgentHubAITaskEntity(
                entry, BASE_URL, API_KEY
            )
            conv_entity = HaAgentHubConversationEntity(entry, BASE_URL, API_KEY)

        assert ai_entity._attr_device_info == conv_entity._attr_device_info
        assert ai_entity._attr_device_info["identifiers"] == {("ha_agenthub", ENTRY_ID)}

    def test_translations_define_entity_name(self):
        base = Path(__file__).resolve().parent.parent / "ha_agenthub"
        names = {}
        for rel in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads((base / rel).read_text(encoding="utf-8"))
            names[rel] = data["entity"]["ai_task"]["ai_task"]["name"]
        assert names["strings.json"] == "AI Task"
        assert names["translations/en.json"] == "AI Task"
        assert names["translations/de.json"] == "KI-Aufgabe"


# ---------------------------------------------------------------------------
# Request building and results
# ---------------------------------------------------------------------------


class TestGenerateData:
    async def test_structured_request_with_attachments(
        self, entity, chat_log, hass, tmp_path
    ):
        jpg = tmp_path / "snapshot.jpg"
        jpg.write_bytes(b"\xff\xd8jpeg-bytes")
        png = tmp_path / "frame.png"
        png.write_bytes(b"\x89PNGpng-bytes")
        structure = object()
        session = _FakeSession(
            body={
                "conversation_id": "container-conv",
                "data": {"count": 2, "label": "Katze"},
                "text": "2 cats",
                "routed_agent": "general-agent",
                "action_executed": False,
            }
        )
        task = _task(
            structure=structure,
            attachments=[_attachment(jpg, "image/jpg"), _attachment(png, None)],
        )

        with _patch_session(session) as get_session, _patch_timeout():
            result = await entity._async_generate_data(task, chat_log)

        get_session.assert_called_once_with(hass)
        assert len(session.calls) == 1
        url, kwargs = session.calls[0]
        assert url == f"{BASE_URL}/api/ai_task"
        assert kwargs["headers"] == {"Authorization": f"Bearer {API_KEY}"}
        assert kwargs["timeout"] == ("timeout", 180.0)
        payload = kwargs["json"]
        assert payload["task_name"] == "Describe"
        assert payload["instructions"] == "Describe the image."
        assert payload["structure"] == {
            "converted": structure,
            "custom_serializer": sys.modules[
                "homeassistant.helpers.llm"
            ].selector_serializer,
        }
        assert payload["attachments"] == [
            {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(b"\xff\xd8jpeg-bytes").decode("ascii"),
            },
            {
                "mime_type": "image/png",
                "data": base64.b64encode(b"\x89PNGpng-bytes").decode("ascii"),
            },
        ]
        assert payload["conversation_id"] == CONVERSATION_ID
        assert payload["language"] == "de"
        assert payload["user_id"] is None
        assert payload["device_id"] is None
        # Files are read in the executor, never on the event loop.
        hass.async_add_executor_job.assert_awaited_once()

        assert result.conversation_id == CONVERSATION_ID
        assert result.data == {"count": 2, "label": "Katze"}
        chat_log.async_add_assistant_content_without_tools.assert_called_once()
        content = chat_log.async_add_assistant_content_without_tools.call_args.args[0]
        assert content.agent_id == entity.entity_id
        assert json.loads(content.content) == {"count": 2, "label": "Katze"}

    async def test_structure_uses_llm_api_serializer(self, entity, chat_log):
        serializer = object()
        chat_log.llm_api = SimpleNamespace(custom_serializer=serializer)
        session = _FakeSession(body={"conversation_id": "c", "data": {"a": 1}})

        with _patch_session(session), _patch_timeout():
            await entity._async_generate_data(_task(structure=object()), chat_log)

        payload = session.calls[0][1]["json"]
        assert payload["structure"]["custom_serializer"] is serializer

    async def test_text_request_without_structure(self, entity, chat_log, hass):
        session = _FakeSession(
            body={"conversation_id": "c", "data": "All quiet.", "text": "All quiet."}
        )

        with _patch_session(session), _patch_timeout():
            result = await entity._async_generate_data(_task(), chat_log)

        payload = session.calls[0][1]["json"]
        assert payload["structure"] is None
        assert payload["attachments"] == []
        hass.async_add_executor_job.assert_not_awaited()
        assert result.data == "All quiet."
        assert result.conversation_id == CONVERSATION_ID
        content = chat_log.async_add_assistant_content_without_tools.call_args.args[0]
        assert content.content == "All quiet."

    async def test_structured_data_returned_as_json_string_is_parsed(
        self, entity, chat_log
    ):
        session = _FakeSession(body={"conversation_id": "c", "data": '{"on": true}'})

        with _patch_session(session), _patch_timeout():
            result = await entity._async_generate_data(
                _task(structure=object()), chat_log
            )

        assert result.data == {"on": True}

    async def test_structured_data_invalid_json_string_raises(self, entity, chat_log):
        session = _FakeSession(body={"conversation_id": "c", "data": "not json"})

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match="invalid structured data"),
        ):
            await entity._async_generate_data(_task(structure=object()), chat_log)
        chat_log.async_add_assistant_content_without_tools.assert_not_called()

    async def test_response_without_data_raises(self, entity, chat_log):
        session = _FakeSession(body={"conversation_id": "c", "text": "x"})

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match="does not contain data"),
        ):
            await entity._async_generate_data(_task(), chat_log)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_errors(self, entity, chat_log, status):
        session = _FakeSession(status=status, body={"detail": "Invalid API key"})

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match="API key rejected") as exc_info,
        ):
            await entity._async_generate_data(_task(), chat_log)
        assert API_KEY not in str(exc_info.value)
        chat_log.async_add_assistant_content_without_tools.assert_not_called()

    async def test_502_includes_container_detail(self, entity, chat_log):
        session = _FakeSession(status=502, body={"detail": "LLM provider unavailable"})

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match=r"HTTP 502\): LLM provider unavailable"),
        ):
            await entity._async_generate_data(_task(), chat_log)

    async def test_400_with_non_json_body(self, entity, chat_log):
        session = _FakeSession(status=400, body="Bad Request")

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match=r"HTTP 400\): Bad Request"),
        ):
            await entity._async_generate_data(_task(), chat_log)

    async def test_connection_error(self, entity, chat_log):
        aiohttp = _client_module().aiohttp
        session = _FakeSession(exc=aiohttp.ClientError("refused"))

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match="Cannot reach the HA-AgentHub container"),
        ):
            await entity._async_generate_data(_task(), chat_log)

    async def test_timeout(self, entity, chat_log):
        session = _FakeSession(exc=TimeoutError())

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match="timed out after 180s"),
        ):
            await entity._async_generate_data(_task(), chat_log)

    async def test_non_object_response(self, entity, chat_log):
        session = _FakeSession(body="[1, 2]")

        with (
            _patch_session(session),
            _patch_timeout(),
            pytest.raises(_ha_error(), match="not a JSON object"),
        ):
            await entity._async_generate_data(_task(), chat_log)


# ---------------------------------------------------------------------------
# Validation before sending
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_instructions_too_long(self, entity, chat_log):
        session = _FakeSession()
        task = _task(instructions="x" * 4001)

        with (
            _patch_session(session),
            pytest.raises(_ha_error(), match="instructions are too long"),
        ):
            await entity._async_generate_data(task, chat_log)
        assert session.calls == []

    async def test_instructions_at_limit_are_sent(self, entity, chat_log):
        session = _FakeSession()

        with _patch_session(session), _patch_timeout():
            await entity._async_generate_data(_task(instructions="x" * 4000), chat_log)
        assert len(session.calls) == 1

    async def test_too_many_attachments(self, entity, chat_log, hass, tmp_path):
        attachments = []
        for index in range(5):
            path = tmp_path / f"img{index}.png"
            path.write_bytes(b"png")
            attachments.append(_attachment(path, "image/png"))
        session = _FakeSession()

        with (
            _patch_session(session),
            pytest.raises(_ha_error(), match="Too many attachments"),
        ):
            await entity._async_generate_data(_task(attachments=attachments), chat_log)
        assert session.calls == []
        hass.async_add_executor_job.assert_not_awaited()

    async def test_non_image_attachment(self, entity, chat_log, hass, tmp_path):
        path = tmp_path / "manual.pdf"
        path.write_bytes(b"%PDF")
        session = _FakeSession()

        with (
            _patch_session(session),
            pytest.raises(_ha_error(), match="only images are supported"),
        ):
            await entity._async_generate_data(
                _task(attachments=[_attachment(path, "application/pdf")]), chat_log
            )
        assert session.calls == []
        hass.async_add_executor_job.assert_not_awaited()

    async def test_oversize_attachment(self, entity, chat_log, tmp_path):
        path = tmp_path / "huge.jpg"
        with path.open("wb") as handle:
            handle.truncate(_ai_task_module().MAX_ATTACHMENT_BYTES + 1)
        session = _FakeSession()

        with (
            _patch_session(session),
            pytest.raises(_ha_error(), match="too large"),
        ):
            await entity._async_generate_data(
                _task(attachments=[_attachment(path, "image/jpeg")]), chat_log
            )
        assert session.calls == []

    async def test_missing_attachment_file(self, entity, chat_log, tmp_path):
        session = _FakeSession()

        with (
            _patch_session(session),
            pytest.raises(_ha_error(), match="cannot be read"),
        ):
            await entity._async_generate_data(
                _task(attachments=[_attachment(tmp_path / "gone.png", "image/png")]),
                chat_log,
            )
        assert session.calls == []
