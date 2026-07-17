"""Agent configuration models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

RequestSource = Literal["ha", "chat", "api", "background"]
BackgroundEventType = Literal[
    "alarm_notification",
    "timer_notification",
    "delayed_action",
    "sleep_media_stop",
    "voice_followup",
]

# Canonical agent identifiers used across the orchestration pipeline.
FALLBACK_AGENT = "general-agent"
# cancel-interaction is a pipeline-level directive, not a real agent.
# It signals that the user wants to abort the current voice/chat turn.
CANCEL_INTERACTION_AGENT = "cancel-interaction"
# Agents that are internal to the orchestrator pipeline and should not be
# exposed as routable targets or stored in routing/action caches.
INTERNAL_ONLY_AGENTS: frozenset[str] = frozenset({"orchestrator", "rewrite-agent", "filler-agent"})


class BackgroundEvent(BaseModel):
    """Structured internal event carried by a background orchestrator turn."""

    event_type: BackgroundEventType
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentCard(BaseModel):
    """A2A agent card describing agent capabilities."""

    agent_id: str
    name: str
    description: str
    skills: list[str] = Field(default_factory=list)
    input_types: list[str] = Field(default_factory=lambda: ["text/plain"])
    output_types: list[str] = Field(default_factory=lambda: ["text/plain", "application/json"])
    endpoint: str = Field("", description="Agent endpoint URL (local:// for in-process)")
    expected_latency: str = Field("low", description="Expected response latency: low, medium, high")
    # P2-2 (FLOW-TIMEOUT-1): per-agent dispatch timeout override. ``None``
    # falls back to the orchestrator's ``a2a.default_timeout`` setting.
    # Long-running agents (general/web search, MCP-tool reasoning) should
    # set this to 20-60s; deterministic device agents leave it ``None`` so
    # the 5s default applies. Operators can further override per agent_id
    # via the ``agent.dispatch_timeout.<agent_id>`` settings key.
    timeout_sec: float | None = Field(
        None,
        description="Per-agent dispatch timeout in seconds (None = use orchestrator default).",
    )


class AgentConfig(BaseModel):
    """Agent runtime configuration loaded from SQLite."""

    agent_id: str
    enabled: bool = True
    model: str | None = None
    timeout: int = 5
    max_iterations: int = 3
    temperature: float = 0.2
    max_tokens: int = 1024
    description: str | None = None
    reasoning_effort: str | None = None


class IngressTask(BaseModel):
    """Task received at the orchestrator boundary carrying raw user input."""

    model_config = {"arbitrary_types_allowed": True}

    description: str = Field(..., description="Raw sanitized user input")
    conversation_id: str | None = None
    context: TaskContext | None = None

    # Runtime-only: not serialized, not included in model_dump()
    span_collector: Any = Field(default=None, exclude=True)


class DispatchTask(BaseModel):
    """Task dispatched from orchestrator to a specialized agent via A2A."""

    model_config = {"arbitrary_types_allowed": True}

    description: str = Field(..., description="Condensed task with preserved entity names")
    conversation_id: str | None = None
    context: TaskContext | None = None
    # Entity/room tokens parsed from the classification ``@entities:`` lines
    # (classification_engine.py). The pipeline populates them on every
    # dispatch so the entity matcher can try them before fuzzy matching.
    verbatim_terms: list[str] = Field(
        default_factory=list,
        description="Entity/room tokens from the classification @entities: line, tried first by the entity matcher",
    )

    # Runtime-only: not serialized, not included in model_dump()
    span_collector: Any = Field(default=None, exclude=True)


class BackgroundTask(BaseModel):
    """Background-event envelope dispatched to the orchestrator.

    Carries no text fields; the event payload lives on
    ``context.background_event`` and the background-turn contract is
    enforced by ``OrchestratorAgent._is_background_turn``.
    """

    model_config = {"arbitrary_types_allowed": True}

    conversation_id: str | None = None
    context: TaskContext | None = None

    # Runtime-only: not serialized, not included in model_dump()
    span_collector: Any = Field(default=None, exclude=True)


class TaskContext(BaseModel):
    """Context propagated with an agent task."""

    conversation_turns: list[dict] = Field(default_factory=list)
    device_id: str | None = None
    area_id: str | None = None
    # FLOW-CTX-1 (0.18.6): human-readable counterparts to device_id /
    # area_id for speech + trace UI. IDs remain the authoritative key
    # for any comparison / visibility logic.
    device_name: str | None = None
    area_name: str | None = None
    user_id: str | None = None
    # FLOW-CTX-1 (0.18.6): request origin. "ha" = voice satellite via
    # HA integration, "chat" = dashboard chat UI, "api" = raw REST/WS
    # without the HA wrapper. Agents use this to disambiguate
    # phrasings like "hier" (ambiguous in chat, resolvable for
    # a satellite in a known area).
    source: RequestSource = "api"
    language: str = "en"
    background_event: BackgroundEvent | None = None
    sequential_send: bool = False
    timezone: str = "UTC"
    location_name: str = ""
    local_time: str = ""
    injection_detected: bool = False


class ActionExecuted(BaseModel):
    """Result of a Home Assistant action execution."""

    action: str = Field(..., description="HA action name (e.g. turn_on, turn_off)")
    entity_id: str = Field(..., description="Target entity ID (e.g. light.kitchen)")
    success: bool = Field(True, description="Whether the action succeeded")
    new_state: str | None = Field(None, description="Entity state after action")
    cacheable: bool = Field(True, description="Whether response may be stored in the response cache")
    # P1-5: non-entity service payload parameters (brightness, color_temp,
    # rgb_color, transition, volume_level, ...). The orchestrator
    # replays a whitelisted subset of this on a response-cache hit so
    # that "turn on bedroom light at 30 percent" no longer falls back
    # to a plain ``turn_on`` on the next hit.
    service_data: dict = Field(
        default_factory=dict,
        description="Structured service_data parameters passed to the HA call",
    )


class AgentErrorCode(StrEnum):
    """Structured error codes for agent failures."""

    ENTITY_NOT_FOUND = "entity_not_found"
    ACTION_FAILED = "action_failed"
    HA_UNAVAILABLE = "ha_unavailable"
    LLM_ERROR = "llm_error"
    LLM_EMPTY_RESPONSE = "llm_empty_response"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    AGENT_NOT_FOUND = "agent_not_found"
    INTERNAL = "internal"


class AgentError(BaseModel):
    """Structured error returned by an agent."""

    code: AgentErrorCode
    message: str
    recoverable: bool = True


class TaskResult(BaseModel):
    """Standardized result returned by all agents from handle_task().

    Backward compatible: .model_dump() produces the same dict shape
    that agents previously returned manually.
    """

    speech: str = Field(..., description="Natural language response text")
    action_executed: ActionExecuted | None = Field(None, description="HA action result if an action was performed")
    metadata: dict = Field(default_factory=dict, description="Agent-specific metadata")
    error: AgentError | None = Field(None, description="Structured error if the agent encountered a problem")
    # When True and the request came from HA voice (``source == \"ha\"``), the
    # orchestrator re-opens Assist STT on the origin satellite after TTS.
    voice_followup: bool = Field(
        False,
        description="Ask orchestrator to trigger satellite listen-after-response (HA voice only)",
    )
    directive: str | None = Field(
        None,
        description="Optional transport directive emitted by an agent, such as timer-native delegation.",
    )
    reason: str | None = Field(
        None,
        description="Optional reason paired with a transport directive.",
    )
