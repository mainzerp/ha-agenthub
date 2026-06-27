"""Entity index admin API endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.cache.embedding import get_embedding_info
from app.cache.vector_store import COLLECTION_ENTITY_INDEX
from app.db.repository import EntityVisibilityRepository, SettingsRepository
from app.entity import deterministic_resolver
from app.entity.ingest import parse_ha_states
from app.runtime_setup import ensure_setup_runtime_initialized
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/entity-index",
    tags=["admin-entity-index"],
    dependencies=[Depends(require_admin_session)],
)


# Mirrors each executor's local _ALLOWED_DOMAINS constant. Keep in sync
# manually if any executor changes its allowed domains.
#   action_executor.py     -> light-agent (and switch/sensor light path)
#   climate_executor.py    -> climate-agent
#   automation_executor.py -> automation-agent
#   media_executor.py      -> media-agent
#   music_executor.py      -> music-agent
#   scene_executor.py      -> scene-agent
#   security_executor.py   -> security-agent
#   timer_executor/        -> timer-agent
AGENT_ALLOWED_DOMAINS: dict[str, frozenset[str]] = {
    "light-agent": frozenset({"light", "switch", "sensor"}),
    "climate-agent": frozenset({"climate", "sensor", "weather"}),
    "automation-agent": frozenset({"automation"}),
    "media-agent": frozenset({"media_player"}),
    "music-agent": frozenset({"media_player"}),
    "scene-agent": frozenset({"scene"}),
    "security-agent": frozenset({"alarm_control_panel", "lock", "camera", "binary_sensor", "sensor"}),
    "timer-agent": frozenset(),
}

# Agents whose execute path runs `_resolve_light_entity` upstream; for
# these the deterministic block's domain gate is the legacy light gate.
_LIGHT_DETERMINISTIC_AGENTS: frozenset[str] = frozenset({"light-agent"})


def _count_allowed_domain_entries(
    entity_index: Any,
    allowed_domains: tuple[str, ...] | list[str] | frozenset[str],
) -> dict[str, int]:
    """Return ``{domain: count}`` for the given domains, in one vector-store scan.

    Always returns a dict that contains *every* requested domain (zero counts
    for absent domains). On any failure returns the requested domains all
    mapped to ``0`` -- the caller relies on the dict shape to decide between
    `no_entities_of_allowed_domains` and `filtered_out`, so a fail-soft
    zero-map is the safe default (UI just won't claim entities exist).
    """
    counts: dict[str, int] = {d: 0 for d in allowed_domains}
    if not counts:
        return counts
    try:
        vector_store = getattr(entity_index, "_store", None)
        if vector_store is None:
            return counts
        where: dict[str, Any] | None
        if len(counts) == 1:
            (only,) = tuple(counts.keys())
            where = {"domain": only}
        else:
            where = {"domain": {"$in": list(counts.keys())}}
        try:
            data = vector_store.get(
                COLLECTION_ENTITY_INDEX,
                where=where,
                include=["metadatas"],
            )
        except Exception:
            logger.debug("Vector store `where` query failed, falling back to full scan", exc_info=True)
            # Backend may not support this `where` shape -- fall back to a
            # full scan and filter client-side.
            data = vector_store.get(
                COLLECTION_ENTITY_INDEX,
                include=["metadatas"],
            )
        for meta in data.get("metadatas", []) or []:
            d = (meta or {}).get("domain")
            if d in counts:
                counts[d] += 1
    except Exception:
        logger.debug(
            "match-preview: domain-count helper failed for domains=%s",
            list(counts.keys()),
            exc_info=True,
        )
    return counts


@router.get("/stats")
async def get_entity_index_stats(request: Request):
    """Entity index stats with per-domain breakdown."""
    await ensure_setup_runtime_initialized(request.app)
    entity_index = request.app.state.entity_index
    if not entity_index:
        return {"count": 0, "status": "not_initialized", "domains": {}, "embedding": None, "sync": {}}

    try:
        stats = entity_index.get_stats()
        count = stats.get("count", 0)

        # Get per-domain breakdown
        domains: dict[str, int] = {}
        if count > 0:
            vector_store = entity_index._store
            data = vector_store.get(
                COLLECTION_ENTITY_INDEX,
                include=["metadatas"],
            )
            for meta in data.get("metadatas", []):
                domain = meta.get("domain", "unknown")
                domains[domain] = domains.get(domain, 0) + 1

        embedding_info = await get_embedding_info()

        sync_stats = stats.get("sync", {})
        sync_interval = await SettingsRepository.get_value("entity_sync.interval_minutes", "30")

        return {
            "count": count,
            "last_refresh": stats.get("last_refresh"),
            "domains": domains,
            "embedding": {
                **embedding_info,
                **stats.get("embedding_status", {}),
            },
            "sync": sync_stats,
            "sync_interval_minutes": int(sync_interval or 30),
        }
    except Exception as exc:
        logger.warning("Failed to get entity index stats", exc_info=True)
        return {"count": 0, "error": str(exc), "embedding": None, "sync": {}}


@router.get("/match-preview")
async def match_preview(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="Entity query"),
    agent_id: str | None = Query(
        default=None,
        description="Optional agent id for visibility/domain gating",
    ),
    domain: str | None = Query(
        default=None,
        max_length=64,
        description="Optional explicit domain hard-filter (e.g. 'climate')",
    ),
) -> dict[str, Any]:
    """Preview how the entity resolver + hybrid matcher handle a query.

    Surfaces exactly what each agent type receives:

        * ``deterministic`` -- the output of the same deterministic-first
            resolver used by the selected executor family. ``light-agent``
            stays on :func:`app.agents.action_executor._resolve_light_entity`
            to preserve its light-only exact-match extras, while non-light
            agents use :func:`app.entity.deterministic_resolver.resolve_entity_deterministic_first`.
            Includes the chosen ``entity_id``, ``friendly_name``,
            ``resolution_path`` and whether the resolved id passes the
            executor-domain gate.
    * ``hybrid`` -- the top candidates from
      :meth:`app.entity.matcher.EntityMatcher.match`, which is what
      non-light executors (climate / media / security / …) use directly.
      Each candidate carries its ``score`` and per-signal scores so the
      operator can see why a result was (or was not) picked.
    * ``visibility`` -- a compact summary of the visibility rules the
      selected ``agent_id`` is bound to, plus the live visible-entity
      count for that agent.

    The endpoint is read-only. No HA service calls are made.
    """
    entity_index = getattr(request.app.state, "entity_index", None)
    entity_matcher = getattr(request.app.state, "entity_matcher", None)
    if entity_index is None:
        raise HTTPException(
            status_code=503,
            detail="Entity index not initialized",
        )

    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query must not be empty")

    agent = (agent_id or "").strip() or None
    domain_filter = (domain or "").strip().lower() or None

    agent_allowed_domains: frozenset[str] = AGENT_ALLOWED_DOMAINS.get(agent or "", frozenset())
    # `preferred_domains` for the matcher: explicit ?domain wins; else
    # the agent's allowed-domain set; else None.
    preferred_domains: tuple[str, ...] | None
    if domain_filter:
        preferred_domains = (domain_filter,)
    elif agent_allowed_domains:
        preferred_domains = tuple(sorted(agent_allowed_domains))
    else:
        preferred_domains = None

    # -----------------------------------------------------------------
    # Deterministic resolver preview: light keeps its legacy light-only
    # path; non-light agents use the shared helper introduced by Directive 4.
    # -----------------------------------------------------------------
    deterministic: dict[str, Any] = {
        "entity_id": None,
        "friendly_name": query,
        "speech": None,
        "metadata": {
            "query": query,
            "resolution_path": "not_attempted",
            "match_count": 0,
        },
        "domain_allowed": False,
        "error": None,
    }
    try:
        from app.agents.light_executor import _validate_domain

        if entity_matcher is None:
            deterministic["error"] = "entity_matcher not initialized"
        else:
            resolution = await deterministic_resolver.resolve_entity_deterministic_first(
                query,
                entity_index,
                entity_matcher,
                agent,
                allowed_domains=agent_allowed_domains or None,
            )
        if deterministic["error"] is None:
            metadata = dict(resolution.get("metadata") or {})
            if agent and agent not in _LIGHT_DETERMINISTIC_AGENTS:
                rp = metadata.get("resolution_path")
                if isinstance(rp, str) and not rp.endswith(":non_light_agent"):
                    metadata["resolution_path"] = f"{rp}:non_light_agent"
            deterministic.update(
                {
                    "entity_id": resolution.get("entity_id"),
                    "friendly_name": resolution.get("friendly_name") or query,
                    "speech": resolution.get("speech"),
                    "metadata": metadata,
                }
            )
            resolved_id = deterministic["entity_id"]
            resolved_domain = (
                resolved_id.split(".", 1)[0] if isinstance(resolved_id, str) and "." in resolved_id else ""
            )
            if not resolved_id:
                deterministic["domain_allowed"] = False
            elif agent is None or agent in _LIGHT_DETERMINISTIC_AGENTS:
                deterministic["domain_allowed"] = bool(_validate_domain(resolved_id))
            elif agent_allowed_domains:
                deterministic["domain_allowed"] = resolved_domain in agent_allowed_domains
            else:
                # Unknown agent: don't lie about gating.
                deterministic["domain_allowed"] = True
            if domain_filter and resolved_domain and resolved_domain != domain_filter:
                metadata["domain_filter_dropped"] = True
                deterministic["domain_allowed"] = False
            elif domain_filter:
                metadata["domain_filter_dropped"] = False
    except Exception as exc:
        logger.warning("match-preview: deterministic resolution failed", exc_info=True)
        deterministic["error"] = str(exc)

    # -----------------------------------------------------------------
    # Hybrid matcher (what every non-light executor sees directly)
    # -----------------------------------------------------------------
    hybrid: list[dict[str, Any]] = []
    hybrid_error: str | None = None
    try:
        if entity_matcher is None:
            hybrid_error = "entity_matcher not initialized"
        else:
            matches = await entity_matcher.match(
                query,
                agent_id=agent,
                preferred_domains=preferred_domains,
            )
            for match in matches:
                entity_id = getattr(match, "entity_id", "") or ""
                domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
                if domain_filter and domain != domain_filter:
                    continue
                entry = None
                try:
                    if entity_index is not None and hasattr(entity_index, "get_by_id"):
                        entry = entity_index.get_by_id(entity_id)
                except Exception:
                    logger.debug("Failed to get entity by id %s", entity_id, exc_info=True)
                    entry = None
                hybrid.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": getattr(match, "friendly_name", "") or entity_id,
                        "domain": domain,
                        "area": getattr(entry, "area", None) if entry else None,
                        "score": round(float(getattr(match, "score", 0.0) or 0.0), 4),
                        "signal_scores": {
                            k: round(float(v), 4) for k, v in (getattr(match, "signal_scores", {}) or {}).items()
                        },
                    }
                )
    except Exception as exc:
        logger.warning("match-preview: hybrid matcher failed", exc_info=True)
        hybrid_error = str(exc)

    # -----------------------------------------------------------------
    # Empty-state diagnostics: only when the hybrid list is empty.
    # Attached regardless of `hybrid_error` -- admins still benefit from
    # knowing whether allowed domains have any entities at all.
    # -----------------------------------------------------------------
    diagnostics: dict[str, Any] | None = None
    if not hybrid:
        allowed_for_diag: tuple[str, ...]
        if domain_filter:
            allowed_for_diag = (domain_filter,)
        elif agent_allowed_domains:
            allowed_for_diag = tuple(sorted(agent_allowed_domains))
        else:
            allowed_for_diag = ()

        if not allowed_for_diag:
            reason = "unknown"
            domain_counts: dict[str, int] = {}
        else:
            domain_counts = _count_allowed_domain_entries(entity_index, allowed_for_diag)
            reason = "no_entities_of_allowed_domains" if all(v == 0 for v in domain_counts.values()) else "filtered_out"

        diagnostics = {
            "reason": reason,
            "allowed_domains": list(allowed_for_diag),
            "domain_counts": domain_counts,
        }

    # -----------------------------------------------------------------
    # Visibility summary for the selected agent
    # -----------------------------------------------------------------
    visibility: dict[str, Any] = {
        "agent_id": agent,
        "rules": [],
        "visible_entity_count": None,
        "total_entity_count": None,
    }
    try:
        if entity_index is not None and hasattr(entity_index, "list_entries"):
            total_entries = entity_index.list_entries()
            visibility["total_entity_count"] = len(total_entries)
            if (
                agent
                and entity_matcher is not None
                and hasattr(
                    entity_matcher,
                    "filter_visible_results",
                )
            ):
                from app.entity.matcher import MatchResult

                probe = [
                    MatchResult(
                        entity_id=e.entity_id,
                        friendly_name=e.friendly_name,
                        score=1.0,
                    )
                    for e in total_entries
                ]
                visible = await entity_matcher.filter_visible_results(agent, probe)
                visibility["visible_entity_count"] = len(visible)
        if agent:
            rules = await EntityVisibilityRepository.get_rules(agent)
            visibility["rules"] = rules
    except Exception:
        logger.debug(
            "match-preview: visibility summary failed for agent_id=%s",
            agent,
            exc_info=True,
        )

    return {
        "query": query,
        "agent_id": agent,
        "domain": domain_filter,
        "agent_allowed_domains": sorted(agent_allowed_domains) if agent_allowed_domains else [],
        "preferred_domains": list(preferred_domains) if preferred_domains else [],
        "deterministic": deterministic,
        "hybrid": hybrid,
        "hybrid_error": hybrid_error,
        "visibility": visibility,
        **({"diagnostics": diagnostics} if diagnostics is not None else {}),
    }


@router.post("/refresh")
async def refresh_entity_index(request: Request):
    """Force refresh entity index from Home Assistant."""
    entity_index = request.app.state.entity_index
    ha_client = request.app.state.ha_client
    if not entity_index or not ha_client:
        return {"status": "error", "detail": "Entity index or HA client not initialized"}

    try:
        states = await ha_client.get_states()
        entities = parse_ha_states(states)
        entity_index.refresh(entities)
        return {
            "status": "ok",
            "count": len(entities),
        }
    except Exception as exc:
        logger.warning("Failed to refresh entity index", exc_info=True)
        return {"status": "error", "detail": str(exc)}
