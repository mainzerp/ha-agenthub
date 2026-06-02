"""A2A Protocol Layer -- JSON-RPC 2.0 based agent communication."""

from app.a2a.dispatcher import (
    _INTERNAL_ERROR,
    _INVALID_PARAMS,
    _INVALID_REQUEST,
    _METHOD_NOT_FOUND,
    _PARSE_ERROR,
    _TIMEOUT_ERROR,
    Dispatcher,
    _error_response,
    _success_response,
)
from app.a2a.protocol import (
    JsonRpcRequest,
)
from app.a2a.registry import AgentRegistry, registry
from app.a2a.transport import InProcessTransport, Transport

# Re-export with original names for backward compatibility
INTERNAL_ERROR = _INTERNAL_ERROR
INVALID_PARAMS = _INVALID_PARAMS
INVALID_REQUEST = _INVALID_REQUEST
METHOD_NOT_FOUND = _METHOD_NOT_FOUND
PARSE_ERROR = _PARSE_ERROR
TIMEOUT_ERROR = _TIMEOUT_ERROR
error_response = _error_response
success_response = _success_response

__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "TIMEOUT_ERROR",
    "AgentRegistry",
    "Dispatcher",
    "InProcessTransport",
    "JsonRpcRequest",
    "Transport",
    "error_response",
    "registry",
    "success_response",
]
