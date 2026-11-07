"""Helpers for converting Home Assistant state payloads into entity index entries."""

from __future__ import annotations

from typing import Any

from app.entity.naming import choose_entity_name, compute_name_suppression
from app.models.entity_index import EntityIndexEntry

# Domain prefixes and other generic noise tokens that carry no
# room/device discrimination value when extracted from an entity_id.
# Keeping this list small and language-agnostic: every token is an
# English HA structural term, never a room name in any language.
_ID_TOKEN_STOPWORDS: frozenset[str] = frozenset(
    {
        "sensor",
        "binary",
        "binary_sensor",
        "light",
        "switch",
        "climate",
        "media",
        "media_player",
        "scene",
        "automation",
        "weather",
        "lock",
        "cover",
        "fan",
        "humidifier",
        "vacuum",
        "camera",
        "button",
        "number",
        "input",
        "select",
        "state",
        "mode",
        "temperature",
        "humidity",
        "pressure",
        "moisture",
        "battery",
        "power",
        "energy",
    }
)


def _tokenize_entity_id(entity_id: str) -> list[str]:
    """Split an entity_id into distinctive tokens.

    Splits on ``.`` and ``_``, lowercases, drops empty tokens and
    structural HA stopwords. Order is preserved; duplicates are
    removed while keeping the first occurrence.
    """
    if not entity_id:
        return []
    raw_parts = entity_id.lower().replace(".", "_").split("_")
    seen: set[str] = set()
    out: list[str] = []
    for part in raw_parts:
        if not part or part in _ID_TOKEN_STOPWORDS or part in seen:
            continue
        seen.add(part)
        out.append(part)
    return out


def state_friendly_name(state: dict[str, Any]) -> str:
    """HA's ``friendly_name`` attribute of a state payload ("" when absent or not a string)."""
    attrs = state.get("attributes") or {}
    value = attrs.get("friendly_name") if isinstance(attrs, dict) else None
    return value if isinstance(value, str) else ""


def snapshot_name_suppression(
    states: list[dict[str, Any]],
    *,
    user_name_lookup: dict[str, str] | None,
    device_lookup: dict[str, str] | None,
    hidden_ids: set[str] | None = None,
) -> frozenset[str]:
    """Collision suppression set (see :func:`compute_name_suppression`) for a full states snapshot."""
    if not user_name_lookup:
        return frozenset()
    hidden = hidden_ids or set()
    return compute_name_suppression(
        (
            (eid, state_friendly_name(state))
            for state in states
            if (eid := state.get("entity_id") or "") and eid not in hidden
        ),
        user_name_lookup,
        device_lookup,
    )


def state_to_entity_index_entry(
    state: dict[str, Any],
    *,
    entity_id: str | None = None,
    area_lookup: dict[str, str] | None = None,
    alias_lookup: dict[str, list[str]] | None = None,
    device_lookup: dict[str, str] | None = None,
    area_id_lookup: dict[str, str] | None = None,
    user_name_lookup: dict[str, str] | None = None,
    name_suppressed: frozenset[str] | set[str] | None = None,
) -> EntityIndexEntry:
    """Convert a Home Assistant state payload into an EntityIndexEntry.

    ``area_lookup`` maps HA ``area_id`` -> human-readable area name
    (from the HA area registry). ``alias_lookup`` maps ``entity_id`` ->
    list of HA per-entity aliases. ``device_lookup`` maps
    ``entity_id`` -> parent device name. ``area_id_lookup`` maps
    ``entity_id`` -> ``area_id`` resolved through the HA registry
    (entity area or inherited from parent device); HA ``/api/states``
    never carries ``area_id`` inside attributes, so this lookup is
    consulted first and the attrs ``area_id`` is used only as a
    fallback (kept for snapshot-fixture compatibility).
    ``user_name_lookup`` maps ``entity_id`` -> own name (registry name, else original_name) and
    ``name_suppressed`` holds the ids whose own name collides (see
    :mod:`app.entity.naming`): together with ``device_lookup`` they decide
    whether ``friendly_name`` carries the entity's own name or HA's
    friendly_name, which is always kept in ``ha_friendly_name``. All
    lookups are optional.
    """
    resolved_entity_id = entity_id or state.get("entity_id", "")
    attrs = state.get("attributes", {}) or {}
    domain = resolved_entity_id.split(".")[0] if "." in resolved_entity_id else ""
    area: str | None = None
    if area_id_lookup:
        area = area_id_lookup.get(resolved_entity_id) or None
    if not area:
        area = attrs.get("area_id") or None
    area_name: str | None = None
    if area and area_lookup:
        area_name = area_lookup.get(area) or None
    aliases: list[str] = []
    if alias_lookup:
        aliases = list(alias_lookup.get(resolved_entity_id, []) or [])
    # HA also exposes per-entity aliases in attributes for some
    # integrations; merge whatever the state already carries.
    state_aliases = attrs.get("aliases")
    if isinstance(state_aliases, list):
        for raw in state_aliases:
            if isinstance(raw, str) and raw and raw not in aliases:
                aliases.append(raw)
    device_name: str | None = None
    if device_lookup:
        device_name = device_lookup.get(resolved_entity_id) or None
    ha_friendly_name = state_friendly_name(state)
    name = choose_entity_name(
        ha_friendly_name,
        (user_name_lookup or {}).get(resolved_entity_id),
        device_name,
        suppressed=resolved_entity_id in (name_suppressed or ()),
    )
    return EntityIndexEntry(
        entity_id=resolved_entity_id,
        friendly_name=name,
        ha_friendly_name=ha_friendly_name,
        domain=domain,
        area=area,
        area_name=area_name,
        device_class=attrs.get("device_class"),
        aliases=aliases,
        device_name=device_name,
        id_tokens=_tokenize_entity_id(resolved_entity_id),
        state=state.get("state"),
        has_date=bool(attrs.get("has_date", False)),
        has_time=bool(attrs.get("has_time", False)),
    )


def parse_ha_states(
    states: list[dict[str, Any]],
    *,
    area_lookup: dict[str, str] | None = None,
    alias_lookup: dict[str, list[str]] | None = None,
    device_lookup: dict[str, str] | None = None,
    area_id_lookup: dict[str, str] | None = None,
    hidden_ids: set[str] | None = None,
    user_name_lookup: dict[str, str] | None = None,
    name_suppressed: frozenset[str] | set[str] | None = None,
) -> list[EntityIndexEntry]:
    """Convert a Home Assistant states snapshot into index entries.

    Entries whose ``entity_id`` appears in ``hidden_ids`` (entities that
    HA has marked as hidden or disabled) are silently skipped. When
    ``name_suppressed`` is None the collision suppression set is computed
    from this snapshot (:func:`snapshot_name_suppression`).
    """
    hidden = hidden_ids or set()
    if name_suppressed is None:
        name_suppressed = snapshot_name_suppression(
            states, user_name_lookup=user_name_lookup, device_lookup=device_lookup, hidden_ids=hidden
        )
    return [
        state_to_entity_index_entry(
            state,
            area_lookup=area_lookup,
            alias_lookup=alias_lookup,
            device_lookup=device_lookup,
            area_id_lookup=area_id_lookup,
            user_name_lookup=user_name_lookup,
            name_suppressed=name_suppressed,
        )
        for state in states
        if (state.get("entity_id") or "") not in hidden
    ]
