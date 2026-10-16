"""STT wrapper entity for HA-AgentHub speaker identification (I/O bridge).

The entity wraps a user-selected STT entity (option ``stt_entity_id``): it
mirrors the inner entity's capabilities, forwards the audio stream unchanged
and returns the inner result unchanged. A best-effort copy of the audio is
teed to the container (``voice_tee.py``), correlated with the conversation
turn through HA's chat session id. All speaker identification happens in the
container; the tee never fails or delays the STT turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from typing import TYPE_CHECKING, Any

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started

from . import AgentHubConfigEntry, async_report_auth_failure
from .const import (
    CONF_STT_ENTITY_ID,
    DOMAIN,
    VOICE_TEE_MAX_CONVERSATION_ID,
    VOICE_TEE_SAMPLE_RATE,
    stt_unique_id,
)
from .voice_tee import VoiceTee, build_voice_ws_url

if TYPE_CHECKING:
    from homeassistant.core import Event

logger = logging.getLogger(__name__)

STT_DOMAIN = "stt"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AgentHubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the STT wrapper entity when an inner STT entity is configured."""
    inner_entity_id = (entry.options.get(CONF_STT_ENTITY_ID) or "").strip()
    if not inner_entity_id:
        # Option cleared: drop a stale registry entry instead of leaving an
        # orphaned, permanently unavailable entity behind.
        registry = er.async_get(hass)
        stale = registry.async_get_entity_id(
            STT_DOMAIN, DOMAIN, stt_unique_id(entry.entry_id)
        )
        if isinstance(stale, str) and stale:
            registry.async_remove(stale)
        return

    runtime = entry.runtime_data
    async_add_entities(
        [HaAgentHubSttEntity(entry, runtime.url, runtime.api_key, inner_entity_id)]
    )


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _metadata_supports_tee(metadata: stt.SpeechMetadata) -> bool:
    """The container accepts 16 kHz, 16-bit mono PCM only."""
    try:
        return (
            _enum_value(metadata.codec) == _enum_value(stt.AudioCodecs.PCM)
            and int(_enum_value(metadata.sample_rate)) == VOICE_TEE_SAMPLE_RATE
            and int(_enum_value(metadata.bit_rate)) == 16
            and int(_enum_value(metadata.channel)) == 1
        )
    except (TypeError, ValueError):
        return False


def _current_conversation_id() -> str | None:
    """Return the chat session id of the running voice turn, if any.

    ``chat_session.current_session`` is a Home Assistant helper internal set
    by the assist pipeline around the whole run; the conversation agent later
    receives the same id. Any lookup failure disables the tee for the turn.
    """
    try:
        from homeassistant.helpers import chat_session
    except ImportError:
        return None
    try:
        session = chat_session.current_session.get()
    except (AttributeError, LookupError):
        return None
    conversation_id = getattr(session, "conversation_id", None)
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    if len(conversation_id) > VOICE_TEE_MAX_CONVERSATION_ID:
        return None
    return conversation_id


def _confirmed_transcript(result: Any) -> str | None:
    """Return the transcript of a successful STT result with non-empty text, else None."""
    if _enum_value(getattr(result, "result", None)) != _enum_value(
        stt.SpeechResultState.SUCCESS
    ):
        return None
    text = getattr(result, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    return text


async def _tee_stream(
    stream: AsyncIterable[bytes], tee: VoiceTee
) -> AsyncIterator[bytes]:
    """Yield the inner stream unchanged while copying each chunk to the tee.

    No awaits besides the upstream iteration: the inner STT may stop early
    and ``aclose()`` this generator (``GeneratorExit`` at the ``yield``), or
    the pipeline may cancel the turn; ``finally`` only flags the tee.
    """
    try:
        async for chunk in stream:
            tee.feed(chunk)
            yield chunk
    finally:
        tee.close_input()


class HaAgentHubSttEntity(stt.SpeechToTextEntity):
    """Pass-through STT entity that tees audio to the container."""

    _attr_has_entity_name = True
    _attr_name = "STT"
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, url: str, api_key: str, inner_entity_id: str
    ) -> None:
        self._entry = entry
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._inner_entity_id = inner_entity_id
        self._tee_tasks: set[asyncio.Task[None]] = set()
        self._attr_unique_id = stt_unique_id(entry.entry_id)
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HA-AgentHub",
            model="Conversation bridge",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    # -- Inner entity ------------------------------------------------------

    def _inner(self) -> stt.SpeechToTextEntity | None:
        """Return the loaded inner STT entity, or None (also on self-reference)."""
        if self.hass is None or self._inner_entity_id == self.entity_id:
            return None
        try:
            inner = stt.async_get_speech_to_text_entity(
                self.hass, self._inner_entity_id
            )
        except (KeyError, AttributeError):
            # The stt component is not set up (yet).
            return None
        if inner is None or inner is self or isinstance(inner, HaAgentHubSttEntity):
            return None
        return inner

    @property
    def available(self) -> bool:
        inner = self._inner()
        return inner is not None and bool(inner.available)

    @property
    def supported_languages(self) -> list[str]:
        inner = self._inner()
        return inner.supported_languages if inner is not None else []

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        inner = self._inner()
        return inner.supported_formats if inner is not None else []

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        inner = self._inner()
        return inner.supported_codecs if inner is not None else []

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        inner = self._inner()
        return inner.supported_bit_rates if inner is not None else []

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        inner = self._inner()
        return inner.supported_sample_rates if inner is not None else []

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        inner = self._inner()
        return inner.supported_channels if inner is not None else []

    @property
    def audio_processing(self) -> Any:
        """Mirror the inner entity's audio processing needs (HA cores that
        define it use ``requires_external_vad`` to decide pipeline VAD).

        Never None on cores that define it: the pipeline runs ``asdict()`` on
        the value, so a missing inner entity (or one without a value) falls
        back to HA's ``DEFAULT_AUDIO_PROCESSING``. Older cores have neither
        the constant nor the property and never read it.
        """
        inner = self._inner()
        value = getattr(inner, "audio_processing", None) if inner is not None else None
        if value is None:
            value = getattr(stt, "DEFAULT_AUDIO_PROCESSING", None)
        return value

    # -- Lifecycle ---------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._inner_entity_id == self.entity_id:
            logger.warning(
                "HA-AgentHub STT entity %s is configured to wrap itself; select another STT entity",
                self.entity_id,
            )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._inner_entity_id], self._async_handle_inner_state
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED,
                self._async_handle_registry_update,
                event_filter=self._async_filter_registry_update,
            )
        )
        self.async_on_remove(async_at_started(self.hass, self._async_handle_started))

    async def async_will_remove_from_hass(self) -> None:
        for task in list(self._tee_tasks):
            task.cancel()
        self._tee_tasks.clear()
        await super().async_will_remove_from_hass()

    @callback
    def _async_handle_inner_state(self, event: Event) -> None:
        self.async_write_ha_state()

    @callback
    def _async_handle_started(self, hass: HomeAssistant) -> None:
        self.async_write_ha_state()

    @callback
    def _async_filter_registry_update(self, event_data: Any) -> bool:
        return self._inner_entity_id in (
            event_data.get("entity_id"),
            event_data.get("old_entity_id"),
        )

    @callback
    def _async_handle_registry_update(self, event: Event) -> None:
        data = event.data
        new_entity_id = data.get("entity_id")
        if (
            data.get("action") == "update"
            and data.get("old_entity_id") == self._inner_entity_id
            and isinstance(new_entity_id, str)
            and new_entity_id
        ):
            # Follow a rename of the inner entity; the option update reloads
            # the entry, which recreates this entity with the new id.
            logger.info(
                "Inner STT entity renamed from %s to %s; updating the HA-AgentHub option",
                self._inner_entity_id,
                new_entity_id,
            )
            self.hass.config_entries.async_update_entry(
                self._entry,
                options={**self._entry.options, CONF_STT_ENTITY_ID: new_entity_id},
            )
            return
        self.async_write_ha_state()

    # -- Audio -------------------------------------------------------------

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Forward the stream to the inner STT entity and return its result."""
        inner = self._inner()
        if inner is None:
            logger.warning(
                "HA-AgentHub STT: inner STT entity %s is not available",
                self._inner_entity_id,
            )
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        if not inner.check_metadata(metadata):
            logger.warning(
                "HA-AgentHub STT: inner STT entity %s does not support %s",
                self._inner_entity_id,
                metadata,
            )
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        tee = self._start_tee(metadata)
        if tee is None:
            return await inner.async_process_audio_stream(metadata, stream)

        try:
            result = await inner.async_process_audio_stream(
                metadata, _tee_stream(stream, tee)
            )
        except BaseException:
            tee.abort()
            raise
        text = _confirmed_transcript(result)
        if text is None:
            # No usable transcript: the turn must not become an enrollment
            # sample, so the container discards it.
            tee.abort()
        else:
            tee.finish(len(text))
        return result

    def _report_auth_failure(self) -> None:
        """Start reauth when the container rejects the tee's API key."""
        async_report_auth_failure(self.hass, self._entry)

    def _start_tee(self, metadata: stt.SpeechMetadata) -> VoiceTee | None:
        """Start the background sender for this turn; None means no tee."""
        if not _metadata_supports_tee(metadata):
            logger.debug(
                "Speaker identification skipped: unsupported audio format %s", metadata
            )
            return None
        conversation_id = _current_conversation_id()
        if conversation_id is None:
            logger.debug(
                "Speaker identification skipped: no chat session for this audio stream"
            )
            return None
        try:
            tee = VoiceTee(
                async_get_clientsession(self.hass),
                build_voice_ws_url(self._url),
                self._api_key,
                {
                    "type": "start",
                    "conversation_id": conversation_id,
                    "language": metadata.language,
                    "sample_rate": int(metadata.sample_rate),
                    "bit_rate": int(metadata.bit_rate),
                    "channels": int(metadata.channel),
                    "codec": str(_enum_value(metadata.codec)),
                    "format": str(_enum_value(metadata.format)),
                },
                on_auth_failure=self._report_auth_failure,
            )
        except Exception:
            logger.debug(
                "Speaker identification skipped: could not create the voice tee",
                exc_info=True,
            )
            return None
        sender = tee.run()
        try:
            task = self._entry.async_create_background_task(
                self.hass, sender, name="ha_agenthub_voice_tee"
            )
        except Exception:
            sender.close()
            logger.debug(
                "Speaker identification skipped: could not start the voice tee",
                exc_info=True,
            )
            return None
        self._tee_tasks.add(task)
        task.add_done_callback(self._tee_tasks.discard)
        return tee
