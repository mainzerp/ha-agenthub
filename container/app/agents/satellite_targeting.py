"""Helpers for resolving an explicitly named assist-satellite target."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.entity.visibility import filter_visible_results


@dataclass(frozen=True)
class ResolvedSatelliteTarget:
    """Resolved assist-satellite target details for effective timer context."""

    requested_name: str
    entity_id: str
    device_id: str
    area_id: str | None
    area_name: str | None


@dataclass(frozen=True)
class SatelliteResolutionError:
    """Structured explicit-target resolution failure."""

    code: str
    message: str
    candidates: list[str]


async def resolve_satellite_target_name(
    name: str,
    *,
    entity_index: Any,
    ha_client: Any,
    agent_id: str,
) -> tuple[ResolvedSatelliteTarget | None, SatelliteResolutionError | None]:
    """Resolve an explicit satellite name to assist-satellite entity + device context.

    Only satellites visible to ``agent_id`` (required, passed explicitly by
    the calling agent) under the per-agent entity visibility rules are
    considered (Directive 5); an invisible satellite resolves exactly like a
    non-existent one.
    """
    normalized_target = _normalize_name(name)
    if not normalized_target:
        return None, SatelliteResolutionError(
            code="not_found",
            message="No explicit satellite name was provided.",
            candidates=[],
        )

    entries: list[Any] = []
    if _supports_method(entity_index, "list_entries_async"):
        entries = await entity_index.list_entries_async(domains={"assist_satellite"})
    elif _supports_method(entity_index, "list_entries"):
        entries = entity_index.list_entries(domains={"assist_satellite"})
    entries = [e for e in entries if str(getattr(e, "entity_id", "") or "").startswith("assist_satellite.")]
    entries = await filter_visible_results(agent_id, entries, entity_index)

    matches: list[Any] = []
    for entry in entries:
        labels = _candidate_labels(entry)
        if normalized_target in labels:
            matches.append(entry)

    if not matches:
        return None, SatelliteResolutionError(
            code="not_found",
            message=f"I could not find an assist satellite named '{name}'.",
            candidates=[],
        )

    deduped: dict[str, Any] = {str(getattr(entry, "entity_id", "")): entry for entry in matches}
    if len(deduped) > 1:
        candidate_names = sorted(
            {
                str(getattr(entry, "friendly_name", "") or getattr(entry, "entity_id", "") or "").strip()
                for entry in deduped.values()
            }
        )
        return None, SatelliteResolutionError(
            code="ambiguous",
            message=(
                f"Multiple satellites match '{name}': {', '.join(candidate_names)}. "
                "Please use a more specific satellite name."
            ),
            candidates=candidate_names,
        )

    resolved_entry = next(iter(deduped.values()))
    resolved_entity_id = str(getattr(resolved_entry, "entity_id", "") or "")
    device_id = await _resolve_device_id_for_entity(ha_client, resolved_entity_id)
    if not device_id:
        return None, SatelliteResolutionError(
            code="missing_device_id",
            message=(
                f"Satellite '{name}' was found, but its device id could not be resolved. Please try another satellite."
            ),
            candidates=[],
        )

    return (
        ResolvedSatelliteTarget(
            requested_name=name,
            entity_id=resolved_entity_id,
            device_id=device_id,
            area_id=getattr(resolved_entry, "area", None),
            area_name=getattr(resolved_entry, "area_name", None),
        ),
        None,
    )


def _candidate_labels(entry: Any) -> set[str]:
    labels: set[str] = set()
    friendly_name = str(getattr(entry, "friendly_name", "") or "")
    if friendly_name:
        labels.add(_normalize_name(friendly_name))
    device_name = str(getattr(entry, "device_name", "") or "")
    if device_name:
        labels.add(_normalize_name(device_name))

    entity_id = str(getattr(entry, "entity_id", "") or "")
    if entity_id.startswith("assist_satellite."):
        entity_slug = entity_id.split(".", 1)[1]
        labels.add(_normalize_name(entity_slug.replace("_", " ")))

    for alias in getattr(entry, "aliases", []) or []:
        alias_text = str(alias or "")
        if alias_text:
            labels.add(_normalize_name(alias_text))

    return {label for label in labels if label}


def _normalize_name(value: str) -> str:
    lowered = str(value or "").strip().casefold()
    if not lowered:
        return ""
    decomposed = unicodedata.normalize("NFKD", lowered)
    ascii_like = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    collapsed = re.sub(r"[^a-z0-9]+", "", ascii_like)
    return collapsed


def _supports_method(obj: Any, method_name: str) -> bool:
    spec_class = getattr(obj, "_spec_class", None)
    if spec_class and hasattr(spec_class, method_name):
        return callable(getattr(obj, method_name, None))
    return callable(getattr(obj, method_name, None))


async def _resolve_device_id_for_entity(ha_client: Any, entity_id: str) -> str | None:
    if not entity_id:
        return None
    template = "{{ device_id('" + entity_id + "') }}"
    rendered: str | None = None
    try:
        if hasattr(ha_client, "render_template"):
            rendered = await ha_client.render_template(template)
        else:
            client = getattr(ha_client, "_client", None)
            if client is None:
                return None
            resp = await client.post("/api/template", json={"template": template})
            resp.raise_for_status()
            rendered = (resp.text or "").strip()
    except Exception:
        return None
    if not rendered:
        return None
    if str(rendered).strip().casefold() == "none":
        return None
    return str(rendered).strip()
