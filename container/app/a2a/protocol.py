"""JSON-RPC 2.0 message types and A2A envelope."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request."""

    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] | None = None
    id: str
