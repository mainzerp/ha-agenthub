"""Entity visibility management API endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.db.repository import EntityVisibilityRepository
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/entity-visibility",
    tags=["admin-entity-visibility"],
    dependencies=[Depends(require_admin_session)],
)


class VisibilityRule(BaseModel):
    rule_type: str
    rule_value: str


class SetRulesRequest(BaseModel):
    rules: list[VisibilityRule]


VALID_RULE_TYPES = {
    "domain_include",
    "domain_exclude",
    "area_include",
    "area_exclude",
    "entity_include",
    "device_class_include",
    "device_class_exclude",
}


@router.get("/{agent_id}")
async def get_visibility_rules(agent_id: str) -> list[dict[str, Any]]:
    """Get visibility rules for an agent."""
    return await EntityVisibilityRepository.get_rules(agent_id)


@router.put("/{agent_id}")
async def set_visibility_rules(agent_id: str, body: SetRulesRequest) -> dict[str, Any]:
    """Set visibility rules for an agent."""
    rules = [r.model_dump() for r in body.rules]
    invalid = [r["rule_type"] for r in rules if r["rule_type"] not in VALID_RULE_TYPES]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid rule_type(s): {', '.join(set(invalid))}. "
            f"Valid types: {', '.join(sorted(VALID_RULE_TYPES))}",
        )
    await EntityVisibilityRepository.set_rules(agent_id, rules)
    return {"agent_id": agent_id, "rules_count": len(rules)}


# Separate router for the entities endpoint (different prefix)
entities_router = APIRouter(
    prefix="/api/admin",
    tags=["admin-entity-visibility"],
    dependencies=[Depends(require_admin_session)],
)


@entities_router.get("/entities")
async def list_all_entities(request: Request) -> dict[str, Any]:
    """List all entities grouped by domain and area."""
    ha_client = request.app.state.ha_client
    if not ha_client:
        return {"domains": []}

    try:
        states = await ha_client.get_states()
    except Exception:
        logger.warning("Failed to fetch HA states for entity listing", exc_info=True)
        return {"domains": []}

    # Group by domain then area
    domain_area_map: dict[str, dict[str, list[dict[str, str]]]] = {}
    for state in states:
        entity_id = state.get("entity_id", "")
        attrs = state.get("attributes", {})
        friendly_name = attrs.get("friendly_name", entity_id)
        area = attrs.get("area_id") or "unassigned"
        domain = entity_id.split(".")[0] if "." in entity_id else "unknown"

        if domain not in domain_area_map:
            domain_area_map[domain] = {}
        if area not in domain_area_map[domain]:
            domain_area_map[domain][area] = []
        domain_area_map[domain][area].append(
            {
                "entity_id": entity_id,
                "friendly_name": friendly_name,
            }
        )

    domains = []
    for domain_name in sorted(domain_area_map.keys()):
        areas = []
        for area_name in sorted(domain_area_map[domain_name].keys()):
            entities = domain_area_map[domain_name][area_name]
            areas.append(
                {
                    "name": area_name,
                    "entities": sorted(entities, key=lambda e: e["entity_id"]),  # type: ignore[index]
                }
            )
        domains.append({"name": domain_name, "areas": areas})

    return {"domains": domains}
