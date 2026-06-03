"""Custom agents CRUD API endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.db.repository import CustomAgentRepository
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/custom-agents",
    tags=["admin-custom-agents"],
    dependencies=[Depends(require_admin_session)],
)


class CustomAgentCreate(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    model_override: str | None = None
    timeout_sec: float | None = None
    mcp_tools: list[dict[str, str]] | None = None
    entity_visibility: list[dict[str, str]] | None = None
    intent_patterns: list[str] | None = None


class CustomAgentUpdate(BaseModel):
    description: str | None = None
    system_prompt: str | None = None
    model_override: str | None = None
    timeout_sec: float | None = None
    mcp_tools: list[dict[str, str]] | None = None
    entity_visibility: list[dict[str, str]] | None = None
    intent_patterns: list[str] | None = None
    enabled: bool | None = None


async def _reload_custom_loader(request: Request) -> None:
    custom_loader = getattr(request.app.state, "custom_loader", None)
    if custom_loader is not None:
        await custom_loader.reload()


def _http_422_from_value_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("")
async def list_custom_agents() -> list[dict[str, Any]]:
    """List all custom agents."""
    return await CustomAgentRepository.list_all()


@router.post("", status_code=201)
async def create_custom_agent(request: Request, body: CustomAgentCreate) -> dict[str, Any]:
    """Create a new custom agent."""
    try:
        name = CustomAgentRepository.normalize_name(body.name)
    except ValueError as exc:
        raise _http_422_from_value_error(exc) from exc

    existing = await CustomAgentRepository.get(name)
    if existing:
        raise HTTPException(status_code=409, detail="Agent with this name already exists")

    try:
        created_name = await CustomAgentRepository.create_with_runtime(
            name=name,
            system_prompt=body.system_prompt,
            description=body.description,
            model_override=body.model_override,
            mcp_tools=body.mcp_tools,
            entity_visibility=body.entity_visibility,
            intent_patterns=body.intent_patterns,
            enabled=True,
        )
    except ValueError as exc:
        raise _http_422_from_value_error(exc) from exc
    await _reload_custom_loader(request)
    return await CustomAgentRepository.get(created_name) or {}


@router.get("/{name}")
async def get_custom_agent(name: str) -> dict[str, Any]:
    """Get a single custom agent."""
    try:
        agent = await CustomAgentRepository.get(name)
    except ValueError as exc:
        raise _http_422_from_value_error(exc) from exc
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.put("/{name}")
async def update_custom_agent(request: Request, name: str, body: CustomAgentUpdate) -> dict[str, Any]:
    """Update a custom agent."""
    try:
        normalized_name = CustomAgentRepository.normalize_name(name)
        existing = await CustomAgentRepository.get(normalized_name)
    except ValueError as exc:
        raise _http_422_from_value_error(exc) from exc
    if not existing:
        raise HTTPException(status_code=404, detail="Agent not found")

    update_data = {field: getattr(body, field) for field in body.model_fields_set}
    if update_data.get("system_prompt") is None and "system_prompt" in update_data:
        raise HTTPException(status_code=422, detail="system_prompt cannot be null")
    try:
        await CustomAgentRepository.update_with_runtime(normalized_name, **update_data)
    except ValueError as exc:
        raise _http_422_from_value_error(exc) from exc

    await _reload_custom_loader(request)
    return await CustomAgentRepository.get(normalized_name) or {}


@router.delete("/{name}")
async def delete_custom_agent(request: Request, name: str) -> dict[str, str]:
    """Delete a custom agent."""
    try:
        normalized_name = CustomAgentRepository.normalize_name(name)
        existing = await CustomAgentRepository.get(normalized_name)
    except ValueError as exc:
        raise _http_422_from_value_error(exc) from exc
    if not existing:
        raise HTTPException(status_code=404, detail="Agent not found")

    await CustomAgentRepository.delete_with_runtime(normalized_name)
    await _reload_custom_loader(request)
    return {"status": "deleted", "name": normalized_name}
