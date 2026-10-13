"""Tests for the STT wrapper entity and the speaker-identification audio tee."""

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

OWN_ENTITY_ID = "stt.ha_agenthub_stt"
INNER_ENTITY_ID = "stt.inner"
CONVERSATION_ID = "01JTESTCONVERSATIONID00000"
CHUNKS = [bytes([i]) * 320 for i in range(1, 6)]


def _stt():
    return sys.modules["homeassistant.components.stt"]


def _metadata(**overrides):
    stt = _stt()
    values = {
        "language": "en",
        "format": stt.AudioFormats.WAV,
        "codec": stt.AudioCodecs.PCM,
        "bit_rate": 16,
        "sample_rate": 16000,
        "channel": 1,
    }
    values.update(overrides)
    return stt.SpeechMetadata(**values)


async def _audio(chunks=CHUNKS):
    for chunk in chunks:
        await asyncio.sleep(0)
        yield chunk


def _make_inner(**kwargs):
    stt = _stt()

    class FakeInnerStt(stt.SpeechToTextEntity):
        """Inner STT double: records what it reads from the stream."""

        def __init__(
            self,
            *,
            read_limit=None,
            aclose_early=False,
            raise_after=None,
            hang=False,
            available=True,
        ):
            self.received: list[bytes] = []
            self.read_limit = read_limit
            self.aclose_early = aclose_early
            self.raise_after = raise_after
            self.hang = hang
            self._available = available
            self.result = stt.SpeechResult(
                "turn on the light", stt.SpeechResultState.SUCCESS
            )
            self.calls = 0
            self.supported_languages = ["en", "de"]
            self.supported_formats = [stt.AudioFormats.WAV]
            self.supported_codecs = [stt.AudioCodecs.PCM, stt.AudioCodecs.OPUS]
            self.supported_bit_rates = [16, 24]
            self.supported_sample_rates = [16000]
            self.supported_channels = [1, 2]

        @property
        def available(self):
            return self._available

        async def async_process_audio_stream(self, metadata, stream):
            self.calls += 1
            async for chunk in stream:
                self.received.append(chunk)
                if (
                    self.raise_after is not None
                    and len(self.received) >= self.raise_after
                ):
                    raise RuntimeError("inner stt failed")
                if (
                    self.read_limit is not None
                    and len(self.received) >= self.read_limit
                ):
                    break
            if self.aclose_early:
                await stream.aclose()
            if self.hang:
                await asyncio.Event().wait()
            return self.result

    return FakeInnerStt(**kwargs)


class _FakeWs:
    def __init__(self, *, receive_hangs=False):
        self.frames: list[tuple[str, object]] = []
        self.closed = False
        self.receive_hangs = receive_hangs

    async def send_json(self, data):
        self.frames.append(("json", data))

    async def send_bytes(self, data):
        self.frames.append(("bytes", data))

    async def receive(self):
        if self.receive_hangs:
            await asyncio.Event().wait()
        return SimpleNamespace(
            type=1, data=json.dumps({"type": "result", "status": "identified"})
        )

    async def close(self):
        self.closed = True

    def json_types(self):
        return [data["type"] for kind, data in self.frames if kind == "json"]

    def audio(self):
        return [data for kind, data in self.frames if kind == "bytes"]


class _FakeSession:
    def __init__(self, ws=None, *, error=None, connect_gate=None):
        self.ws = ws or _FakeWs()
        self.error = error
        self.connect_gate = connect_gate
        self.connect_calls: list[tuple[str, dict]] = []

    async def ws_connect(self, url, **kwargs):
        self.connect_calls.append((url, kwargs))
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        if self.error is not None:
            raise self.error
        return self.ws


@pytest.fixture
def chat_session_id():
    """Simulate the assist pipeline's active chat session."""
    var = sys.modules["homeassistant.helpers.chat_session"].current_session
    token = var.set(SimpleNamespace(conversation_id=CONVERSATION_ID))
    yield
    var.reset(token)


@pytest.fixture(autouse=True)
def _reset_warning_limiter():
    from custom_components.ha_agenthub import voice_tee

    voice_tee.unreachable_warning.reset()
    yield
    voice_tee.unreachable_warning.reset()


def _make_entity(inner, session=None, inner_entity_id=INNER_ENTITY_ID):
    from custom_components.ha_agenthub import stt as stt_platform

    entry = MagicMock()
    entry.entry_id = "e1"
    entry.title = "HA-AgentHub"
    entry.options = {"stt_entity_id": inner_entity_id}

    def _create_task(hass, coro, name=None):
        return asyncio.get_running_loop().create_task(coro, name=name)

    entry.async_create_background_task = MagicMock(side_effect=_create_task)
    entity = stt_platform.HaAgentHubSttEntity(
        entry, "https://container.local:8080/", "secret", inner_entity_id
    )
    entity.hass = MagicMock()
    entity.entity_id = OWN_ENTITY_ID
    patches = [
        patch.object(
            stt_platform.stt, "async_get_speech_to_text_entity", return_value=inner
        ),
        patch.object(
            stt_platform,
            "async_get_clientsession",
            return_value=session or _FakeSession(),
        ),
    ]
    for p in patches:
        p.start()
    return entity, entry, patches


async def _drain_tee_tasks(entity, timeout=2.0):
    tasks = list(entity._tee_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)


@pytest.fixture
def stop_patches():
    started: list = []
    yield started
    for patches in started:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Pass-through and tee
# ---------------------------------------------------------------------------


class TestAudioPassThrough:
    @pytest.mark.asyncio
    async def test_audio_byte_identical_result_unchanged_and_tee_copy(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner()
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        assert result is inner.result
        assert inner.received == CHUNKS
        assert all(a is b for a, b in zip(inner.received, CHUNKS, strict=True))

        url, kwargs = session.connect_calls[0]
        assert url == "wss://container.local:8080/ws/voice"
        assert kwargs["headers"] == {"Authorization": "Bearer secret"}
        # PCM frames are never zlib-compressed on HA's event loop.
        assert kwargs["compress"] == 0
        ws = session.ws
        assert ws.frames[0] == (
            "json",
            {
                "type": "start",
                "conversation_id": CONVERSATION_ID,
                "language": "en",
                "sample_rate": 16000,
                "bit_rate": 16,
                "channels": 1,
                "codec": "pcm",
                "format": "wav",
            },
        )
        assert ws.audio() == CHUNKS
        assert ws.json_types() == ["start", "audio_end", "end"]
        assert ws.frames[-1] == (
            "json",
            {"type": "end", "transcript_chars": 17},
        )
        assert ws.closed is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("text", "state"),
        [
            (None, "error"),
            ("turn on the light", "error"),
            (None, "success"),
            ("", "success"),
            ("   \n", "success"),
        ],
    )
    async def test_unconfirmed_result_sends_abort(
        self, chat_session_id, stop_patches, text, state
    ):
        stt = _stt()
        inner = _make_inner()
        inner.result = stt.SpeechResult(text, stt.SpeechResultState(state))
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        assert result is inner.result
        assert "end" not in session.ws.json_types()
        assert session.ws.frames[-1] == ("json", {"type": "abort"})

    @pytest.mark.asyncio
    async def test_end_frame_carries_length_not_text(
        self, chat_session_id, stop_patches
    ):
        stt = _stt()
        inner = _make_inner()
        long_text = "x" * 20000
        inner.result = stt.SpeechResult(long_text, stt.SpeechResultState.SUCCESS)
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        end_frame = session.ws.frames[-1]
        assert end_frame == ("json", {"type": "end", "transcript_chars": 20000})
        # The container rejects text frames above 4096 bytes.
        assert len(json.dumps(end_frame[1])) < 100

    @pytest.mark.asyncio
    async def test_slow_container_does_not_delay_result(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner()
        gate = asyncio.Event()
        session = _FakeSession(_FakeWs(receive_hangs=True), connect_gate=gate)
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await asyncio.wait_for(
            entity.async_process_audio_stream(_metadata(), _audio()), 1.0
        )
        assert result is inner.result
        assert inner.received == CHUNKS
        # The sender is still waiting for the connection; the turn is done.
        assert entity._tee_tasks

        with patch(
            "custom_components.ha_agenthub.voice_tee.VOICE_TEE_RESULT_TIMEOUT", 0.05
        ):
            gate.set()
            await _drain_tee_tasks(entity)
        assert session.ws.audio() == CHUNKS
        assert session.ws.json_types() == ["start", "audio_end", "end"]


class TestContainerFailures:
    @pytest.mark.asyncio
    async def test_connection_failure_does_not_affect_result(
        self, chat_session_id, stop_patches, caplog
    ):
        inner = _make_inner()
        session = _FakeSession(error=OSError("connection refused"))
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        caplog.set_level(
            logging.DEBUG, logger="custom_components.ha_agenthub.voice_tee"
        )
        first = await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)
        second = await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        assert first is inner.result
        assert second is inner.result
        assert inner.received == CHUNKS + CHUNKS
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "unreachable warning must be rate-limited"
        assert "cannot reach" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_connect_timeout_does_not_affect_result(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner()
        session = _FakeSession(connect_gate=asyncio.Event())
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        with patch(
            "custom_components.ha_agenthub.voice_tee.VOICE_TEE_CONNECT_TIMEOUT", 0.05
        ):
            result = await entity.async_process_audio_stream(_metadata(), _audio())
            await _drain_tee_tasks(entity)

        assert result is inner.result
        assert session.ws.frames == []

    @pytest.mark.asyncio
    async def test_send_error_does_not_affect_result(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner()
        ws = _FakeWs()
        ws.send_bytes = AsyncMock(side_effect=RuntimeError("socket broke"))
        session = _FakeSession(ws)
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        assert result is inner.result
        assert inner.received == CHUNKS
        assert ws.closed is True


class TestQueueOverflow:
    def _tee(self, queue_max=4):
        from custom_components.ha_agenthub.voice_tee import VoiceTee

        return VoiceTee(
            MagicMock(), "ws://x/ws/voice", "k", {"type": "start"}, queue_max=queue_max
        )

    def test_feed_drops_when_full_without_blocking(self):
        tee = self._tee(queue_max=4)
        for chunk in CHUNKS + CHUNKS:
            tee.feed(chunk)
        assert len(tee._chunks) == 4
        assert tee.dropped_chunks == len(CHUNKS) * 2 - 4

    def test_overflow_turns_end_into_abort(self):
        tee = self._tee(queue_max=1)
        tee.feed(b"a")
        tee.feed(b"b")
        tee.finish(5)
        assert tee._input_closed is True
        assert tee._outcome == {"type": "abort"}

    def test_feed_after_close_is_ignored(self):
        tee = self._tee()
        tee.close_input()
        tee.feed(b"late")
        assert len(tee._chunks) == 0

    @pytest.mark.asyncio
    async def test_overflow_while_connecting_sends_abort(self):
        from custom_components.ha_agenthub.voice_tee import VoiceTee

        gate = asyncio.Event()
        session = _FakeSession(connect_gate=gate)
        tee = VoiceTee(session, "ws://x/ws/voice", "k", {"type": "start"}, queue_max=2)
        task = asyncio.get_running_loop().create_task(tee.run())
        for chunk in CHUNKS:
            tee.feed(chunk)
        tee.finish(4)
        gate.set()
        await asyncio.wait_for(task, 1.0)
        # No ``audio_end``: the container must not start an embedding it discards.
        assert session.ws.json_types() == ["start", "abort"]
        assert session.ws.audio() == []

    @pytest.mark.asyncio
    async def test_overflow_before_outcome_skips_audio_end(self):
        from custom_components.ha_agenthub.voice_tee import VoiceTee

        gate = asyncio.Event()
        session = _FakeSession(connect_gate=gate)
        tee = VoiceTee(session, "ws://x/ws/voice", "k", {"type": "start"}, queue_max=2)
        task = asyncio.get_running_loop().create_task(tee.run())
        for chunk in CHUNKS:
            tee.feed(chunk)
        tee.close_input()
        gate.set()
        # The sender drains the input before the STT outcome is known.
        while not session.ws.frames:
            await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)
        assert "audio_end" not in session.ws.json_types()
        tee.finish(4)
        await asyncio.wait_for(task, 1.0)
        assert session.ws.json_types() == ["start", "abort"]
        assert session.ws.audio() == []


class TestEarlyStopAndErrors:
    @pytest.mark.asyncio
    async def test_inner_aclose_early_finalises_sink(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner(read_limit=2, aclose_early=True)
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        assert result is inner.result
        assert inner.received == CHUNKS[:2]
        assert session.ws.audio() == CHUNKS[:2]
        assert session.ws.json_types() == ["start", "audio_end", "end"]

    @pytest.mark.asyncio
    async def test_inner_stops_without_aclose_finalises_sink(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner(read_limit=3)
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        assert result is inner.result
        assert session.ws.audio() == CHUNKS[:3]
        assert session.ws.json_types() == ["start", "audio_end", "end"]

    @pytest.mark.asyncio
    async def test_inner_exception_sends_abort_and_propagates(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner(raise_after=2)
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        with pytest.raises(RuntimeError, match="inner stt failed"):
            await entity.async_process_audio_stream(_metadata(), _audio())
        await _drain_tee_tasks(entity)

        assert session.ws.json_types() == ["start", "abort"]

    @pytest.mark.asyncio
    async def test_cancellation_sends_abort_and_propagates(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner()
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        async def _audio_then_hang():
            async for chunk in _audio():
                yield chunk
            await asyncio.Event().wait()

        turn = asyncio.get_running_loop().create_task(
            entity.async_process_audio_stream(_metadata(), _audio_then_hang())
        )
        while len(inner.received) < len(CHUNKS):
            await asyncio.sleep(0)
        # Cancelled mid-stream: the turn ends in ``abort`` only.
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await _drain_tee_tasks(entity)

        assert session.ws.json_types() == ["start", "abort"]

    @pytest.mark.asyncio
    async def test_cancellation_after_stream_end_still_aborts(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner(hang=True)
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        turn = asyncio.get_running_loop().create_task(
            entity.async_process_audio_stream(_metadata(), _audio())
        )
        # The stream ended cleanly, so ``audio_end`` already went out and the
        # container embeds in parallel with the inner STT; the late
        # cancellation still voids the turn with ``abort``.
        while session.ws.json_types() != ["start", "audio_end"]:
            await asyncio.sleep(0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await _drain_tee_tasks(entity)

        assert session.ws.json_types() == ["start", "audio_end", "abort"]

    @pytest.mark.asyncio
    async def test_will_remove_cancels_pending_tee(self, chat_session_id, stop_patches):
        inner = _make_inner()
        session = _FakeSession(connect_gate=asyncio.Event())
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        await entity.async_process_audio_stream(_metadata(), _audio())
        tasks = list(entity._tee_tasks)
        assert tasks
        await entity.async_will_remove_from_hass()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(task.cancelled() for task in tasks)


class TestNoTee:
    @pytest.mark.asyncio
    async def test_no_tee_without_chat_session(self, stop_patches):
        inner = _make_inner()
        session = _FakeSession()
        entity, entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(_metadata(), _audio())

        assert result is inner.result
        assert inner.received == CHUNKS
        assert session.connect_calls == []
        entry.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tee_when_chat_session_helper_missing(
        self, chat_session_id, stop_patches
    ):
        inner = _make_inner()
        session = _FakeSession()
        entity, entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        with patch.dict(sys.modules, {"homeassistant.helpers.chat_session": None}):
            result = await entity.async_process_audio_stream(_metadata(), _audio())

        assert result is inner.result
        assert session.connect_calls == []
        entry.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            {"codec": "opus"},
            {"bit_rate": 24},
            {"channel": 2},
            {"sample_rate": 44100},
        ],
    )
    async def test_no_tee_for_unsupported_format(
        self, chat_session_id, stop_patches, overrides
    ):
        stt = _stt()
        if overrides.get("codec") == "opus":
            overrides = {"codec": stt.AudioCodecs.OPUS}
        inner = _make_inner()
        # The inner STT accepts 44.1 kHz; only the container is limited to 16 kHz.
        inner.supported_sample_rates = [16000, 44100]
        session = _FakeSession()
        entity, entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(
            _metadata(**overrides), _audio()
        )

        assert result is inner.result
        assert inner.received == CHUNKS
        assert session.connect_calls == []
        entry.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_metadata_returns_error_result(
        self, chat_session_id, stop_patches
    ):
        stt = _stt()
        inner = _make_inner()
        session = _FakeSession()
        entity, _entry, patches = _make_entity(inner, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(
            _metadata(language="fr"), _audio()
        )

        assert result.result == stt.SpeechResultState.ERROR
        assert result.text is None
        assert inner.calls == 0
        assert session.connect_calls == []

    @pytest.mark.asyncio
    async def test_missing_inner_returns_error_result(
        self, chat_session_id, stop_patches
    ):
        stt = _stt()
        session = _FakeSession()
        entity, _entry, patches = _make_entity(None, session)
        stop_patches.append(patches)

        result = await entity.async_process_audio_stream(_metadata(), _audio())

        assert result.result == stt.SpeechResultState.ERROR
        assert session.connect_calls == []


# ---------------------------------------------------------------------------
# Mirrored properties and availability
# ---------------------------------------------------------------------------


class TestMirroring:
    def test_properties_mirror_inner(self, stop_patches):
        inner = _make_inner()
        inner.audio_processing = SimpleNamespace(requires_external_vad=False)
        entity, _entry, patches = _make_entity(inner)
        stop_patches.append(patches)

        assert entity.available is True
        assert entity.supported_languages == inner.supported_languages
        assert entity.supported_formats == inner.supported_formats
        assert entity.supported_codecs == inner.supported_codecs
        assert entity.supported_bit_rates == inner.supported_bit_rates
        assert entity.supported_sample_rates == inner.supported_sample_rates
        assert entity.supported_channels == inner.supported_channels
        assert entity.audio_processing is inner.audio_processing
        assert entity.check_metadata(_metadata()) is True

    def test_inner_unavailable_marks_wrapper_unavailable(self, stop_patches):
        inner = _make_inner(available=False)
        entity, _entry, patches = _make_entity(inner)
        stop_patches.append(patches)

        assert entity.available is False

    def test_missing_inner_empty_lists_and_unavailable(self, stop_patches):
        entity, _entry, patches = _make_entity(None)
        stop_patches.append(patches)

        assert entity.available is False
        assert entity.supported_languages == []
        assert entity.supported_formats == []
        assert entity.supported_codecs == []
        assert entity.supported_bit_rates == []
        assert entity.supported_sample_rates == []
        assert entity.supported_channels == []
        # Never None: HA's pipeline runs ``asdict()`` on it.
        assert entity.audio_processing is _stt().DEFAULT_AUDIO_PROCESSING

    def test_inner_without_audio_processing_uses_the_default(self, stop_patches):
        inner = _make_inner()
        inner.audio_processing = None
        entity, _entry, patches = _make_entity(inner)
        stop_patches.append(patches)

        assert entity.audio_processing is _stt().DEFAULT_AUDIO_PROCESSING

    def test_older_core_without_default_returns_none(self, stop_patches, monkeypatch):
        monkeypatch.delattr(_stt(), "DEFAULT_AUDIO_PROCESSING")
        entity, _entry, patches = _make_entity(None)
        stop_patches.append(patches)

        assert entity.audio_processing is None

    def test_stt_component_not_loaded_is_unavailable(self, stop_patches):
        entity, _entry, patches = _make_entity(None)
        stop_patches.append(patches)
        from custom_components.ha_agenthub import stt as stt_platform

        with patch.object(
            stt_platform.stt,
            "async_get_speech_to_text_entity",
            side_effect=KeyError("stt"),
        ):
            assert entity.available is False

    def test_self_reference_is_unavailable(self, stop_patches):
        inner = _make_inner()
        entity, _entry, patches = _make_entity(inner, inner_entity_id=OWN_ENTITY_ID)
        stop_patches.append(patches)

        assert entity.available is False
        assert entity.supported_languages == []

    def test_wrapping_another_wrapper_is_rejected(self, stop_patches):
        from custom_components.ha_agenthub import stt as stt_platform

        other = stt_platform.HaAgentHubSttEntity(
            MagicMock(entry_id="e2", title="x"), "http://c", "k", "stt.a"
        )
        entity, _entry, patches = _make_entity(other)
        stop_patches.append(patches)

        assert entity.available is False


class TestStateTracking:
    @pytest.mark.asyncio
    async def test_added_to_hass_tracks_inner_entity(self, stop_patches):
        from custom_components.ha_agenthub import stt as stt_platform

        entity, _entry, patches = _make_entity(_make_inner())
        stop_patches.append(patches)

        with (
            patch.object(stt_platform, "async_track_state_change_event") as track,
            patch.object(stt_platform, "async_at_started") as at_started,
        ):
            await entity.async_added_to_hass()

        track.assert_called_once_with(
            entity.hass, [INNER_ENTITY_ID], entity._async_handle_inner_state
        )
        at_started.assert_called_once_with(entity.hass, entity._async_handle_started)
        entity.hass.bus.async_listen.assert_called_once()
        assert len(entity._on_remove) == 3

        entity._async_handle_inner_state(MagicMock())
        entity._async_handle_started(entity.hass)
        assert entity.state_writes == 2

    def test_registry_filter_matches_inner_only(self, stop_patches):
        entity, _entry, patches = _make_entity(_make_inner())
        stop_patches.append(patches)

        assert (
            entity._async_filter_registry_update({"entity_id": INNER_ENTITY_ID}) is True
        )
        assert (
            entity._async_filter_registry_update(
                {"entity_id": "stt.x", "old_entity_id": INNER_ENTITY_ID}
            )
            is True
        )
        assert entity._async_filter_registry_update({"entity_id": "stt.other"}) is False

    def test_registry_rename_updates_option(self, stop_patches):
        entity, entry, patches = _make_entity(_make_inner())
        stop_patches.append(patches)

        event = SimpleNamespace(
            data={
                "action": "update",
                "entity_id": "stt.renamed",
                "old_entity_id": INNER_ENTITY_ID,
            }
        )
        entity._async_handle_registry_update(event)

        entity.hass.config_entries.async_update_entry.assert_called_once_with(
            entry, options={"stt_entity_id": "stt.renamed"}
        )

    def test_registry_remove_writes_state(self, stop_patches):
        entity, _entry, patches = _make_entity(_make_inner())
        stop_patches.append(patches)

        entity._async_handle_registry_update(
            SimpleNamespace(data={"action": "remove", "entity_id": INNER_ENTITY_ID})
        )

        assert entity.state_writes == 1
        entity.hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


class TestPlatformSetup:
    def _hass(self):
        from custom_components.ha_agenthub.const import DOMAIN

        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {"url": "http://c.local", "api_key": "k"}}}
        return hass

    @pytest.mark.asyncio
    async def test_no_entity_without_option(self):
        from custom_components.ha_agenthub import stt as stt_platform

        hass = self._hass()
        entry = MagicMock(entry_id="e1", options={})
        add_entities = MagicMock()
        registry = MagicMock()
        registry.async_get_entity_id.return_value = OWN_ENTITY_ID

        with patch.object(stt_platform.er, "async_get", return_value=registry):
            await stt_platform.async_setup_entry(hass, entry, add_entities)

        add_entities.assert_not_called()
        registry.async_get_entity_id.assert_called_once_with(
            "stt", "ha_agenthub", "e1_stt"
        )
        registry.async_remove.assert_called_once_with(OWN_ENTITY_ID)

    @pytest.mark.asyncio
    async def test_entity_created_with_option(self):
        from custom_components.ha_agenthub import stt as stt_platform

        hass = self._hass()
        entry = MagicMock(
            entry_id="e1",
            title="HA-AgentHub",
            options={"stt_entity_id": INNER_ENTITY_ID},
        )
        add_entities = MagicMock()

        await stt_platform.async_setup_entry(hass, entry, add_entities)

        add_entities.assert_called_once()
        (entities,) = add_entities.call_args.args
        assert len(entities) == 1
        assert isinstance(entities[0], stt_platform.HaAgentHubSttEntity)
        assert entities[0]._attr_unique_id == "e1_stt"
        assert entities[0]._inner_entity_id == INNER_ENTITY_ID

    def test_platforms_include_stt(self):
        from custom_components.ha_agenthub import PLATFORMS

        assert "stt" in PLATFORMS
        assert "conversation" in PLATFORMS


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class TestSttOptionsFlow:
    def _make_flow(self, options=None):
        from custom_components.ha_agenthub.config_flow import HaAgentHubOptionsFlow

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.title = "HA-AgentHub"
        entry.data = {
            "name": "HA-AgentHub",
            "url": "http://old.local",
            "api_key": "stored-token",
        }
        entry.options = options or {}
        entry.unique_id = "http://old.local"
        flow = HaAgentHubOptionsFlow(entry)
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries = MagicMock(return_value=[])
        return flow, entry

    def _input(self, **extra):
        return {
            "url": "http://old.local",
            "api_key": "",
            "name": "",
            "ws_receive_timeout": "45",
            **extra,
        }

    @pytest.mark.asyncio
    async def test_saves_stt_entity_id(self):
        flow, _entry = self._make_flow()
        with (
            patch(
                "custom_components.ha_agenthub.config_flow._validate_connection",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "custom_components.ha_agenthub.config_flow._own_stt_entity_id",
                return_value=OWN_ENTITY_ID,
            ),
        ):
            result = await flow.async_step_init(
                self._input(stt_entity_id=INNER_ENTITY_ID)
            )

        assert result["type"] == "create_entry"
        assert result["data"] == {
            "ws_receive_timeout": 45.0,
            "ship_logs": False,
            "ship_logs_level": "DEBUG",
            "stt_entity_id": INNER_ENTITY_ID,
        }

    @pytest.mark.asyncio
    async def test_cleared_field_removes_stt_entity_id(self):
        flow, _entry = self._make_flow(options={"stt_entity_id": INNER_ENTITY_ID})
        with (
            patch(
                "custom_components.ha_agenthub.config_flow._validate_connection",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "custom_components.ha_agenthub.config_flow._own_stt_entity_id",
                return_value=None,
            ),
        ):
            result = await flow.async_step_init(self._input())

        assert result["type"] == "create_entry"
        assert "stt_entity_id" not in result["data"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [OWN_ENTITY_ID, "sensor.not_stt", "stt."])
    async def test_rejects_self_and_non_stt_entities(self, value):
        flow, _entry = self._make_flow()
        validate = AsyncMock(return_value=None)
        with (
            patch(
                "custom_components.ha_agenthub.config_flow._validate_connection",
                new=validate,
            ),
            patch(
                "custom_components.ha_agenthub.config_flow._own_stt_entity_id",
                return_value=OWN_ENTITY_ID,
            ),
        ):
            result = await flow.async_step_init(self._input(stt_entity_id=value))

        assert result["type"] == "form"
        assert result["errors"] == {"stt_entity_id": "invalid_stt_entity"}
        validate.assert_not_awaited()

    def test_schema_entity_selector_excludes_own_entity(self):
        from custom_components.ha_agenthub import config_flow

        class _Marker:
            def __init__(self, key, **kwargs):
                self.key = key
                self.kwargs = kwargs

        fake_vol = SimpleNamespace(
            Schema=lambda schema: schema, Optional=_Marker, Required=_Marker
        )
        with patch.object(config_flow, "vol", fake_vol):
            schema = config_flow._build_options_schema(
                {"stt_entity_id": INNER_ENTITY_ID}, OWN_ENTITY_ID
            )

        [(marker, selector)] = [
            (k, v) for k, v in schema.items() if k.key == "stt_entity_id"
        ]
        assert marker.kwargs == {"description": {"suggested_value": INNER_ENTITY_ID}}
        assert selector.config == {
            "domain": "stt",
            "exclude_entities": [OWN_ENTITY_ID],
        }

    def test_own_stt_entity_id_lookup(self):
        from custom_components.ha_agenthub import config_flow

        registry = MagicMock()
        registry.async_get_entity_id.return_value = OWN_ENTITY_ID
        entry = MagicMock(entry_id="e1")
        with patch.object(config_flow.er, "async_get", return_value=registry):
            assert config_flow._own_stt_entity_id(MagicMock(), entry) == OWN_ENTITY_ID
        registry.async_get_entity_id.assert_called_once_with(
            "stt", "ha_agenthub", "e1_stt"
        )
