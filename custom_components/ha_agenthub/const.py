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
HEALTH_PATH = "/api/health"
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
