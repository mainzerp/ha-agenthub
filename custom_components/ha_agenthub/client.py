"""Authenticated one-shot REST calls to the HA-AgentHub container."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# Upper bound for the error detail echoed back to the caller.
_MAX_DETAIL_LENGTH = 300


class AgentHubApiError(Exception):
    """Base error for a failed container REST call."""


class AgentHubAuthError(AgentHubApiError):
    """The container rejected the API key (HTTP 401/403)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class AgentHubResponseError(AgentHubApiError):
    """The container answered with a non-success status or an invalid body."""

    def __init__(self, status: int, detail: str | None) -> None:
        super().__init__(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        self.status = status
        self.detail = detail


class AgentHubConnectionError(AgentHubApiError):
    """The container could not be reached or did not answer in time."""


@dataclass(slots=True)
class _RawResponse:
    status: int
    body: str


def _extract_detail(body: str) -> str | None:
    """Return the container's ``detail`` message, bounded in length."""
    if not body:
        return None
    detail: Any = body
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = parsed["detail"]
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    detail = detail.strip()
    if len(detail) > _MAX_DETAIL_LENGTH:
        detail = detail[:_MAX_DETAIL_LENGTH] + "..."
    return detail or None


async def async_post_json(
    hass: HomeAssistant,
    base_url: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    """POST ``payload`` to the container and return the JSON object answer.

    Raises ``AgentHubAuthError`` for 401/403, ``AgentHubResponseError`` for
    any other non-200 status or a body that is not a JSON object, and
    ``AgentHubConnectionError`` for transport errors and timeouts.
    """
    session = async_get_clientsession(hass)
    url = f"{base_url.rstrip('/')}{path}"
    # Only the transport is guarded here; status handling happens outside the
    # try block so its errors are never mistaken for transport failures.
    try:
        async with session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            raw = _RawResponse(status=resp.status, body=await resp.text())
    except TimeoutError as err:
        raise AgentHubConnectionError(f"timed out after {timeout:g}s") from err
    except aiohttp.ClientError as err:
        raise AgentHubConnectionError(type(err).__name__) from err

    if raw.status in (401, 403):
        raise AgentHubAuthError(raw.status)
    if raw.status != 200:
        raise AgentHubResponseError(raw.status, _extract_detail(raw.body))
    try:
        data = json.loads(raw.body)
    except ValueError as err:
        raise AgentHubResponseError(raw.status, "response is not valid JSON") from err
    if not isinstance(data, dict):
        raise AgentHubResponseError(raw.status, "response is not a JSON object")
    return data
