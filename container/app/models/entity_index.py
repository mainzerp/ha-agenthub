"""Entity index entry models."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr


class EntityIndexEntry(BaseModel):
    """A Home Assistant entity stored in the pre-embedded entity index."""

    entity_id: str = Field(..., description="HA entity ID (e.g., light.kitchen_ceiling)")
    friendly_name: str = Field("", description="Human-readable entity name")
    domain: str = Field("", description="HA domain (e.g., light, switch, climate)")
    area: str | None = Field(None, description="HA area/room ID assignment")
    area_name: str | None = Field(
        None,
        description="Human-readable area name resolved from the HA area registry.",
    )
    device_class: str | None = Field(None, description="HA device class")
    aliases: list[str] = Field(default_factory=list, description="HA per-entity aliases or user-defined aliases")
    device_name: str | None = Field(
        None,
        description="Parent device name resolved from the HA device registry.",
    )
    id_tokens: list[str] = Field(
        default_factory=list,
        description="Distinctive tokens parsed from entity_id (split on . and _).",
    )
    state: str | None = Field(None, description="Current runtime state for background readers that need it")
    has_date: bool = Field(False, description="input_datetime runtime flag")
    has_time: bool = Field(False, description="input_datetime runtime flag")
    _content_hash: str | None = PrivateAttr(default=None)

    @property
    def embedding_text(self) -> str:
        """Text representation used for embedding.

        Order favours the most distinctive tokens first so that the
        sentence-transformer can latch onto the human-readable name
        and the originating room before lower-signal fields.
        """
        parts: list[str] = []
        if self.friendly_name:
            parts.append(self.friendly_name)
        if self.area:
            parts.append(self.area)
        if self.area_name:
            parts.append(self.area_name)
        if self.id_tokens:
            parts.append(" ".join(self.id_tokens))
        if self.aliases:
            parts.append(" ".join(self.aliases))
        if self.device_name:
            parts.append(self.device_name)
        if self.domain:
            parts.append(self.domain)
        if self.device_class:
            parts.append(self.device_class)
        return " ".join(p for p in parts if p)

    @property
    def content_hash(self) -> str:
        """Stable SHA-256 of the identity-bearing fields.

        Used by the entity index to short-circuit redundant upserts on
        Home Assistant ``state_changed`` events whose runtime ``state``
        changed but whose identity (name, area, aliases, device, etc.)
        did not. ``None`` is coerced to ``""`` so the hash matches the
        coercion already done by ``EntityIndex._build_metadata``.
        """
        if self._content_hash is not None:
            return self._content_hash
        payload: dict[str, Any] = {
            "entity_id": self.entity_id or "",
            "friendly_name": self.friendly_name or "",
            "domain": self.domain or "",
            "area": self.area or "",
            "area_name": self.area_name or "",
            "device_class": self.device_class or "",
            "aliases": sorted(self.aliases or []),
            "device_name": self.device_name or "",
            "id_tokens": sorted(self.id_tokens or []),
        }
        if self.domain == "input_datetime":
            payload["state"] = self.state or ""
            payload["has_date"] = self.has_date
            payload["has_time"] = self.has_time
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
