"""Call-site helpers for building A2A JSON-RPC request envelopes.

These helpers centralize the ``message/send`` / ``message/stream`` request
construction (including the ``_span_collector`` smuggle) that call sites
previously hand-rolled. Every request still flows through
``Dispatcher.dispatch`` / ``dispatch_stream`` -> ``Transport``; the A2A
boundary (Prime Directive 6) is preserved.
"""

from __future__ import annotations

from typing import Any

from app.a2a.protocol import JsonRpcRequest


def _build_params(agent_id: str, task: Any, span_collector: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"agent_id": agent_id, "task": task}
    if span_collector is not None:
        params["_span_collector"] = span_collector
    return params


def build_send_request(
    agent_id: str,
    task: Any,
    *,
    request_id: str,
    span_collector: Any = None,
) -> JsonRpcRequest:
    """Build a ``message/send`` JsonRpcRequest envelope."""
    return JsonRpcRequest(
        method="message/send",
        params=_build_params(agent_id, task, span_collector),
        id=request_id,
    )


def build_stream_request(
    agent_id: str,
    task: Any,
    *,
    request_id: str,
    span_collector: Any = None,
) -> JsonRpcRequest:
    """Build a ``message/stream`` JsonRpcRequest envelope."""
    return JsonRpcRequest(
        method="message/stream",
        params=_build_params(agent_id, task, span_collector),
        id=request_id,
    )
