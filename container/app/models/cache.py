"""Cache entry models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CachedAction(BaseModel):
    """A cached HA service call for direct execution on cache hit.

    Represents static unconditional service calls only. Conditional
    actions (marked ``cacheable=False`` by the executor) are never
    stored in the action cache.
    """

    service: str = Field(..., description="HA service (e.g., light/turn_on)")
    entity_id: str
    service_data: dict = Field(default_factory=dict)


class ActionCacheEntry(BaseModel):
    """Entry in the action replay cache tier."""

    query_text: str
    language: str
    agent_id: str
    condensed_task: str | None = None
    confidence: float = 0.0
    response_text: str
    cached_action: CachedAction
    entity_ids: list[str] = Field(default_factory=list)
    origin_area_id: str | None = None
    origin_device_id: str | None = None
    created_at: str | None = None
    last_accessed: str | None = None
    executed_at: str | None = None
    hit_count: int = 0
    schema_version: int = 4
    # 1.12.4: preserve original agent response so rewrite agent can re-variate
    # from the raw text on every cache hit instead of rewriting an already
    # rewritten phrase.
    original_response_text: str | None = None
    rewrite_applied: bool = False
    rewrite_latency_ms: float | None = None
    validated_at: str | None = None


class RoutingCacheEntry(BaseModel):
    """Entry in the routing cache tier."""

    query_text: str
    language: str
    agent_id: str
    condensed_task: str | None = None
    confidence: float = 0.0
    entity_ids: list[str] = Field(default_factory=list)
    created_at: str | None = None
    last_accessed: str | None = None
    hit_count: int = 0
    schema_version: int = 4
