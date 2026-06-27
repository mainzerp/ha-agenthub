"""Admin sub-router: agent listing and visibility summary."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.db.repository import AgentConfigRepository, EntityVisibilityRepository

from . import _shared

router = APIRouter()


@router.get("/agents")
async def list_agents() -> dict[str, Any]:
    """List all agents (registered + disabled from DB)."""
    if _shared._registry is None:
        return {"agents": []}
    agents = await _shared._registry.list_agents()
    seen_ids = set()
    result = []
    for a in agents:
        card = a.model_dump()
        config = await AgentConfigRepository.get(a.agent_id)
        if config:
            card.update(config)
        result.append(card)
        seen_ids.add(a.agent_id)

    # Known built-in agent IDs (from seed data)
    builtin_agents = {
        "orchestrator",
        "general-agent",
        "light-agent",
        "music-agent",
        "timer-agent",
        "climate-agent",
        "media-agent",
        "scene-agent",
        "automation-agent",
        "security-agent",
        "rewrite-agent",
        "send-agent",
        "calendar-agent",
        "lists-agent",
    }

    # Include disabled built-in agents from DB that are not yet registered
    all_configs = await AgentConfigRepository.list_all()
    for config in all_configs:
        aid = config["agent_id"]
        if aid not in seen_ids and aid in builtin_agents:
            entry = {
                "agent_id": aid,
                "name": aid.replace("-", " ").title(),
                "description": config.get("description", ""),
                "skills": [],
                "input_types": ["text/plain"],
                "output_types": ["text/plain", "application/json"],
                "endpoint": f"local://{aid}",
            }
            entry.update(config)
            result.append(entry)
    return {"agents": result}


@router.get("/agents/visibility-summary")
async def get_all_agents_visibility_summary():
    """Return a summary of entity visibility domains per agent."""
    all_rules = await EntityVisibilityRepository.list_all()
    agent_rules: dict[str, list[dict]] = {}
    for rule in all_rules:
        agent_id = rule["agent_id"]
        agent_rules.setdefault(agent_id, []).append(rule)

    summary: dict[str, dict] = {}
    for agent_id, rules in agent_rules.items():
        domains: set[str] = set()
        excluded_domains: set[str] = set()
        device_classes: set[str] = set()
        excluded_device_classes: set[str] = set()
        for r in rules:
            if r["rule_type"] == "domain_include":
                domains.add(r["rule_value"])
            elif r["rule_type"] == "domain_exclude":
                excluded_domains.add(r["rule_value"])
            elif r["rule_type"] == "area_include":
                domains.add("area:" + r["rule_value"])
            elif r["rule_type"] == "area_exclude":
                excluded_domains.add("area:" + r["rule_value"])
            elif r["rule_type"] == "entity_include":
                domain_part = r["rule_value"].split(".")[0] if "." in r["rule_value"] else r["rule_value"]
                domains.add(domain_part)
            elif r["rule_type"] == "entity_exclude":
                domain_part = r["rule_value"].split(".")[0] if "." in r["rule_value"] else r["rule_value"]
                excluded_domains.add(domain_part)
            elif r["rule_type"] == "device_class_include":
                device_classes.add(r["rule_value"])
            elif r["rule_type"] == "device_class_exclude":
                excluded_device_classes.add(r["rule_value"])
        summary[agent_id] = {
            "domains": sorted(domains),
            "excluded_domains": sorted(excluded_domains),
            "device_classes": sorted(device_classes),
            "excluded_device_classes": sorted(excluded_device_classes),
            "has_rules": True,
        }
    return {"summary": summary}
