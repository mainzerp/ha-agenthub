"""Conversation request and response models."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class ConversationRequest(BaseModel):
    """Incoming conversation request from HA integration."""

    text: str = Field(..., description="User input text", max_length=500)
    conversation_id: str | None = Field(None, description="Conversation ID for multi-turn", max_length=64)
    language: str = Field("en", description="User language code", max_length=10)
    device_id: str | None = Field(
        None, description="Device registry ID of the originating satellite/device", max_length=64
    )
    area_id: str | None = Field(None, description="Area ID resolved from device registry", max_length=64)
    # FLOW-CTX-1 (0.18.6): human-readable names for the originating
    # satellite + its area. Optional because not every client
    # resolves them (dashboard chat has neither, older HA releases
    # might not populate device registry nicely). Pure metadata --
    # agents use IDs for comparisons and names for speech/traces.
    device_name: str | None = Field(
        None, description="Human-readable name of the originating device/satellite", max_length=128
    )
    area_name: str | None = Field(None, description="Human-readable name of the originating area/room", max_length=128)
    user_id: str | None = Field(None, description="Home Assistant user ID of the speaking user", max_length=64)


class ConversationResponse(BaseModel):
    """Full conversation response sent back to HA integration."""

    speech: str = Field(..., description="Response text for TTS or display")
    conversation_id: str | None = None
    action_executed: ActionResult | None = None
    routed_agent: str | None = None
    voice_followup: bool = Field(
        False,
        description="True when the container will re-open Assist listening on the satellite (HA voice)",
    )
    # P3-1: backend already strips Markdown for TTS output via
    # ``app.agents.sanitize.strip_markdown`` before populating ``speech``.
    # Older container versions (< 0.18.35) did not advertise this, so the
    # HA integration kept its own ``_strip_markdown`` copy as a fallback.
    # When this flag is True, the integration may skip that re-sanitize
    # step and trust the backend as the single source of truth.
    sanitized: bool = Field(
        True,
        description="True when speech has already been sanitized for TTS by the backend",
    )
    # Optional transport directive emitted by an agent (e.g. routing hint).
    directive: str | None = Field(
        None,
        description="Optional bridge routing directive.",
    )
    reason: str | None = Field(
        None,
        description="Timer-agent reason code paired with ``directive``.",
    )


class ActionResult(BaseModel):
    """Result of an HA action execution."""

    service: str = Field(..., description="HA service called (e.g., light/turn_on)")
    entity_id: str = Field(..., description="Target entity ID")
    result: str = Field("success", description="Execution result: success or error message")
    service_data: dict | None = Field(None, description="Additional service data sent")


class StreamToken(BaseModel):
    """Single token in a streaming response."""

    token: str
    done: bool = False
    conversation_id: str | None = None
    mediated_speech: str | None = None
    is_filler: bool = False
    error: str | None = None
    voice_followup: bool = False
    # P3-1: True when ``token`` / ``mediated_speech`` has already been
    # sanitized by the backend. The orchestrator strips Markdown before
    # emitting accumulated/mediated speech, so the HA integration can
    # skip its defensive ``_strip_markdown`` re-pass when this flag is set.
    # Filler tokens are NOT sanitized backend-side; the HA integration
    # still strips them in ``_speak_filler``.
    sanitized: bool = True
    # Same directive carrier as ``ConversationResponse`` so a ``done=True``
    # frame can short-circuit the stream if needed.
    directive: str | None = None
    reason: str | None = None
    # Container-directed filler text pushed outside the Assist pipeline
    # via assist_satellite.announce.  When present the integration must
    # play it immediately and continue reading the stream.
    filler_push: str | None = None
    action_executed: ActionResult | None = None
    routed_agent: str | None = None
    # Multi-agent / sequential-send progress markers (only set on
    # non-terminal ``done=False`` frames).
    status: str | None = None
    agents: list[str] | None = None
    # P0 first-frame observability: per-turn timings populated on the
    # terminal (done=True) frame by the streaming routes. Keys:
    # ``first_frame_ms`` (request start -> first frame sent) and
    # ``total_ms`` (request start -> done frame). The HA bridge parses only
    # known keys, so this extra field is safe for older integrations.
    timings: dict[str, float | None] | None = None

    @field_validator("error", mode="before")
    @classmethod
    def _coerce_error_to_str(cls, value: Any) -> str | None:
        """Coerce legacy dict-shaped errors to a string (defensive second line).

        The Phase-1 contract is ``error: str | None``; producers normalize at
        chunk-entry points. Any dict that still slips through is coerced to
        its ``message`` (fallback ``code``) and logged — never silently.
        """
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, dict):
            logger.warning("StreamToken coerced dict-shaped error to string: %s", value)
            message = value.get("message") or value.get("code")
            return str(message) if message else "unknown error"
        logger.warning("StreamToken coerced non-string error %r to string", value)
        return str(value)

    @field_validator("token", mode="before")
    @classmethod
    def _coerce_token_none_to_empty(cls, value: Any) -> Any:
        """Coerce ``token=None`` to ``""`` so a None speech cannot crash framing."""
        if value is None:
            logger.warning("StreamToken coerced token=None to empty string")
            return ""
        return value

    @model_validator(mode="after")
    def _force_unsanitized_filler(self) -> StreamToken:
        """Keep filler chunks marked as unsanitized for downstream stripping."""
        if self.is_filler:
            self.sanitized = False
        return self
