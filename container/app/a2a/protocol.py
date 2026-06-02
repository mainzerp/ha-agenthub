"""JSON-RPC 2.0 message types and A2A envelope."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# --- Standard JSON-RPC 2.0 Error Codes ---

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TIMEOUT_ERROR = -32000


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any | None = None


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request."""

    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] | None = None
    id: str


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 response."""

    jsonrpc: str = "2.0"
    result: Any | None = None
    error: JsonRpcError | None = None
    id: str


class JsonRpcStreamChunk(BaseModel):
    """Streaming chunk wrapping a partial result in JSON-RPC envelope."""

    jsonrpc: str = "2.0"
    result: dict[str, Any]
    id: str
    done: bool = False


# --- A2A Envelope Param Types ---


class MessageSendParams(BaseModel):
    """Params for method 'message/send'."""

    agent_id: str
    task: Any


class MessageStreamParams(BaseModel):
    """Params for method 'message/stream'."""

    agent_id: str
    task: Any


class AgentDiscoverParams(BaseModel):
    """Params for method 'agent/discover'."""

    agent_id: str


# --- Helper factory functions ---


def error_response(request_id: str, code: int, message: str, data: Any | None = None) -> JsonRpcResponse:
    """Build a JsonRpcResponse carrying an error."""
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=code, message=message, data=data),
    )


def success_response(request_id: str, result: Any) -> JsonRpcResponse:
    """Build a JsonRpcResponse carrying a successful result."""
    return JsonRpcResponse(id=request_id, result=result)
