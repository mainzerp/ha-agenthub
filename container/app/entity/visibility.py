"""Shared entity visibility rule evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.db.repository import EntityVisibilityRepository
from app.entity.index import EntityIndex

# Domains where device_class filtering applies (sensor-like domains).
DEVICE_CLASS_DOMAINS = {"sensor", "binary_sensor", "cover", "number"}


class VisibilityCandidate(Protocol):
    entity_id: str


@dataclass
class _VisibilityRules:
    domain_include: set[str] = field(default_factory=set)
    domain_exclude: set[str] = field(default_factory=set)
    area_include: set[str] = field(default_factory=set)
    area_exclude: set[str] = field(default_factory=set)
    entity_include: set[str] = field(default_factory=set)
    device_class_include: set[str] = field(default_factory=set)
    device_class_exclude: set[str] = field(default_factory=set)


def _domain_for(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _parse_rules(rules: Sequence[Mapping[str, Any]]) -> _VisibilityRules:
    parsed = _VisibilityRules()
    for rule in rules:
        rule_type = rule["rule_type"]
        rule_value = rule["rule_value"]
        if rule_type == "domain_include":
            parsed.domain_include.add(rule_value)
        elif rule_type == "domain_exclude":
            parsed.domain_exclude.add(rule_value)
        elif rule_type == "area_include":
            parsed.area_include.add(rule_value)
        elif rule_type == "area_exclude":
            parsed.area_exclude.add(rule_value)
        elif rule_type == "entity_include":
            parsed.entity_include.add(rule_value)
        elif rule_type == "device_class_include":
            parsed.device_class_include.add(rule_value)
        elif rule_type == "device_class_exclude":
            parsed.device_class_exclude.add(rule_value)
    return parsed


async def _passes_visibility_filters(
    entity_id: str,
    rules: _VisibilityRules,
    entity_index: EntityIndex | None,
    *,
    fail_closed_on_metadata_gap: bool,
) -> bool:
    domain = _domain_for(entity_id)
    if rules.domain_include and domain not in rules.domain_include:
        return False
    if rules.domain_exclude and domain in rules.domain_exclude:
        return False

    entry_loaded = False
    entry = None

    async def get_entry():
        nonlocal entry_loaded, entry
        if not entry_loaded:
            if entity_index is not None:
                import inspect

                unbound = getattr(type(entity_index), "get_by_id_async", None)
                if unbound is not None and inspect.iscoroutinefunction(unbound):
                    entry = await entity_index.get_by_id_async(entity_id)
                else:
                    entry = entity_index.get_by_id(entity_id)
            else:
                entry = None
            entry_loaded = True
        return entry

    if rules.area_include or rules.area_exclude:
        indexed_entry = await get_entry()
        if fail_closed_on_metadata_gap and indexed_entry is None:
            return False
        area = indexed_entry.area if indexed_entry else None
        if rules.area_include and (area is None or area not in rules.area_include):
            return False
        if rules.area_exclude and area is not None and area in rules.area_exclude:
            return False

    if rules.device_class_include and domain in DEVICE_CLASS_DOMAINS:
        indexed_entry = await get_entry()
        if fail_closed_on_metadata_gap and indexed_entry is None:
            return False
        entity_device_class = indexed_entry.device_class if indexed_entry else None
        if not entity_device_class or entity_device_class not in rules.device_class_include:
            return False

    if rules.device_class_exclude:
        indexed_entry = await get_entry()
        if fail_closed_on_metadata_gap and indexed_entry is None:
            return False
        entity_device_class = indexed_entry.device_class if indexed_entry else None
        if entity_device_class and entity_device_class in rules.device_class_exclude:
            return False

    return True


async def filter_visible_results[TVisibilityCandidate](
    agent_id: str,
    results: list[TVisibilityCandidate],
    entity_index: EntityIndex | None,
    repository=EntityVisibilityRepository,
) -> list[TVisibilityCandidate]:
    """Filter match results by the full per-agent entity visibility model."""
    raw_rules = await repository.get_rules(agent_id)
    if not raw_rules:
        return results

    rules = _parse_rules(raw_rules)
    filtered: list[TVisibilityCandidate] = []
    for result in results:
        if await _passes_visibility_filters(
            result.entity_id,
            rules,
            entity_index,
            fail_closed_on_metadata_gap=False,
        ):
            filtered.append(result)

    if rules.entity_include:
        filtered_ids = {result.entity_id for result in filtered}
        for result in results:
            if result.entity_id in rules.entity_include and result.entity_id not in filtered_ids:
                filtered.append(result)

    return filtered


async def entity_is_visible(
    agent_id: str,
    entity_id: str,
    entity_index: EntityIndex | None,
    *,
    fail_closed_on_metadata_gap: bool = False,
    repository=EntityVisibilityRepository,
) -> bool:
    """Return whether a single entity is visible under per-agent rules."""
    if not entity_id:
        return False

    raw_rules = await repository.get_rules(agent_id)
    if not raw_rules:
        return True

    rules = _parse_rules(raw_rules)
    if entity_id in rules.entity_include:
        return True
    return await _passes_visibility_filters(
        entity_id,
        rules,
        entity_index,
        fail_closed_on_metadata_gap=fail_closed_on_metadata_gap,
    )
