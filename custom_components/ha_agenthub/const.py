"""Constants for HA-AgentHub Home Assistant integration."""

import math


def parse_positive_timeout(value: object) -> float | None:
    """Return a finite positive timeout, or ``None`` for invalid input."""
    if isinstance(value, bool):
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timeout) or timeout <= 0:
        return None
    return timeout


DOMAIN = "ha_agenthub"
# Shown in HA integration picker, config entry title, and device registry.
INTEGRATION_TITLE = "HA-AgentHub"
DEFAULT_CONTAINER_URL = "http://localhost:8080"
CONF_NAME = "name"
# PLATFORMS moved to __init__.py using Platform enum
ATTR_CONVERSATION_ID = "conversation_id"
ATTR_LANGUAGE = "language"
WS_PATH = "/ws/conversation"
# Authenticated health endpoint: validates the API key (``/healthz`` does not).
HEALTH_PATH = "/api/health"
# Bound for the one-shot container probe in ``async_setup_entry``.
SETUP_PROBE_TIMEOUT = 10.0
# Response timeout for one turn: bounds every WebSocket receive wait and the
# whole REST fallback request. The option key keeps its historical name.
CONF_WS_RECEIVE_TIMEOUT = "ws_receive_timeout"
DEFAULT_WS_RECEIVE_TIMEOUT = 120
# Mirrors ``ConversationRequest.text`` max_length in the container
# (container/app/models/conversation.py). Longer requests are answered
# locally instead of being rejected by the container's validation.
MAX_REQUEST_TEXT_LENGTH = 500


def resolve_ws_receive_timeout(value: object) -> float:
    """Resolve a stored bridge timeout, falling back for old invalid values."""
    return parse_positive_timeout(value) or float(DEFAULT_WS_RECEIVE_TIMEOUT)


RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
WS_HEARTBEAT_INTERVAL = 15
# uvicorn runs with --ws-ping-interval 30 --ws-ping-timeout 10
# (container/Dockerfile). aiohttp answers server pings only inside
# ``receive()``, so the bridge keeps a background reader on the idle shared
# socket; without it the container closes the socket ~40s after the last
# turn. Before reusing a socket idle for longer than this threshold the
# bridge additionally probes it with a ping write.
WS_IDLE_THRESHOLD = 25

# Opt-in shipping of the integration's own log records to the container's
# log buffer (POST /api/logs/ingest). Default off; entry-scoped lifecycle.
CONF_SHIP_LOGS = "ship_logs"
CONF_SHIP_LOGS_LEVEL = "ship_logs_level"
DEFAULT_SHIP_LOGS = False
DEFAULT_SHIP_LOGS_LEVEL = "DEBUG"
SHIP_LOGS_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
LOG_INGEST_PATH = "/api/logs/ingest"
SHIP_LOGS_QUEUE_MAX = 500
SHIP_LOGS_BATCH_SIZE = 100
SHIP_LOGS_FLUSH_INTERVAL = 5.0
SHIP_LOGS_MAX_MESSAGE = 2000
SHIP_LOGS_MAX_BACKOFF = 60.0

# Optional STT wrapper entity for speaker identification. The wrapper
# forwards audio unchanged to the selected inner STT entity and tees a
# best-effort copy to the container's voice WebSocket.
CONF_STT_ENTITY_ID = "stt_entity_id"
STT_UNIQUE_ID_SUFFIX = "_stt"
VOICE_WS_PATH = "/ws/voice"
# Audio chunks buffered per turn while the container socket is slow or still
# connecting; further chunks are dropped (and the turn is aborted) instead of
# blocking the STT stream.
VOICE_TEE_QUEUE_MAX = 512
VOICE_TEE_CONNECT_TIMEOUT = 2.0
# Bounds the wait for the container's result frame and the socket close.
VOICE_TEE_RESULT_TIMEOUT = 2.0
# Hard cap for one tee session, including the wait for the STT outcome.
VOICE_TEE_MAX_DURATION = 120.0
# At most one "container unreachable" warning per interval; debug otherwise.
VOICE_TEE_WARN_INTERVAL = 300.0
# Mirrors the container's ``start`` frame limit for ``conversation_id``.
VOICE_TEE_MAX_CONVERSATION_ID = 64
# The container only accepts 16 kHz audio; other rates pass through untouched.
VOICE_TEE_SAMPLE_RATE = 16000


def stt_unique_id(entry_id: str) -> str:
    """Return the unique_id of the STT wrapper entity for a config entry."""
    return f"{entry_id}{STT_UNIQUE_ID_SUFFIX}"
