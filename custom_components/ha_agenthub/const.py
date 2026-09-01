"""Constants for HA-AgentHub Home Assistant integration."""

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
CONF_WS_RECEIVE_TIMEOUT = "ws_receive_timeout"
DEFAULT_WS_RECEIVE_TIMEOUT = 120
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
WS_HEARTBEAT_INTERVAL = 15
# Must stay below the container's idle kill: uvicorn runs with
# --ws-ping-interval 30 --ws-ping-timeout 10 (container/Dockerfile), which
# closes an idle /ws/conversation socket 40s after connect. Probing only
# after 60s meant every request following >40s of silence hit a dead
# socket and fell back to REST.
WS_IDLE_THRESHOLD = 25
