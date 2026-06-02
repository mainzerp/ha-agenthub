"""Helpers for initializing setup-dependent runtime services in-process."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING

import aiosqlite

from app.a2a.orchestrator_gateway import OrchestratorGateway
from app.agents.base import preload_prompt_cache
from app.agents.custom_loader import CustomAgentLoader
from app.agents.decorator import install_all_agents
from app.agents.rewrite import RewriteAgent
from app.cache.cache_manager import CacheManager
from app.cache.embedding import get_embedding_engine
from app.cache.vector_store import COLLECTION_ENTITY_INDEX, get_vector_store
from app.db.repository import SettingsRepository, SetupStateRepository
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL
from app.entity.aliases import AliasResolver
from app.entity.index import EntityIndex
from app.entity.ingest import parse_ha_states, state_to_entity_index_entry
from app.entity.matcher import EntityMatcher
from app.ha_client.auth import get_ha_token
from app.ha_client.home_context import home_context_provider
from app.ha_client.rest import HARestClient

_background_tasks: set[asyncio.Task] = set()


def _spawn(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` as a tracked background task.

    The task is stored in a module-level set until completion so it
    cannot be silently dropped by the GC. Exceptions raised inside the
    coroutine are logged with traceback and do not propagate.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Background task %s failed", t.get_name(), exc_info=t.exception())

    task.add_done_callback(_done)
    return task


if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

RELEVANT_REGISTRY_FIELDS = frozenset(
    {
        "entity_id",
        "name",
        "friendly_name",
        "area_id",
        "device_id",
        "device_class",
        "hidden",
        "disabled",
        "hidden_by",
        "aliases",
        "icon",
        "labels",
    }
)


# P3-11: tunables for the runtime background loops. Kept module-level
# so they can be inspected / overridden from tests via monkeypatch
# without touching the call sites.
_ENTITY_SYNC_DEFAULT_INTERVAL_MIN = 30
_ENTITY_SYNC_DISABLED_RECHECK_SEC = 300
_ENTITY_UPDATE_FLUSH_INTERVAL_SEC = 0.5
_CACHE_VALIDATOR_DEFAULT_INTERVAL_MIN = 60


def _set_entity_index_pending_status(entity_index: EntityIndex, *, state: str, total: int) -> None:
    """Mark entity index as building/syncing before background priming starts."""
    entity_index._status = {
        "state": state,
        "progress": 0,
        "total": total,
        "processed": 0,
        "error": None,
    }


async def _gather_ha_lookups(ha_client: HARestClient) -> tuple[dict, dict, dict, dict]:
    """Fetch area, alias, device, and entity-area lookups from HA in parallel.

    Each individual fetch is wrapped so a partial outage degrades to
    empty enrichment instead of failing the entire entity sync.
    """

    async def _safe(coro_factory):
        try:
            return await coro_factory()
        except Exception:
            logger.debug("HA registry lookup failed", exc_info=True)
            return None

    area_lookup, alias_lookup, device_lookup, area_id_lookup = await asyncio.gather(
        _safe(ha_client.get_area_registry),
        _safe(ha_client.get_entity_aliases),
        _safe(ha_client.get_device_names),
        _safe(ha_client.get_entity_areas),
    )
    return (
        area_lookup or {},
        alias_lookup or {},
        device_lookup or {},
        area_id_lookup or {},
    )


def _store_entity_lookups(
    app: FastAPI,
    area_lookup: dict,
    alias_lookup: dict,
    device_lookup: dict,
    area_id_lookup: dict,
) -> None:
    """Atomically publish HA registry lookups on ``app.state``.

    The dict is rebuilt and assigned in a single statement so concurrent
    readers (notably ``on_state_changed``) always observe a consistent
    snapshot of area/alias/device/area_id lookups, never a partially
    populated one.
    """
    app.state.entity_lookups = {
        "area": area_lookup or {},
        "alias": alias_lookup or {},
        "device": device_lookup or {},
        "area_id": area_id_lookup or {},
    }


async def _resolve_active_embedding_model() -> str:
    """Read the embedding model identifier currently configured in settings.

    Returns the empty string if it cannot be determined; callers treat
    an empty string as "do not compare" so a missing setting does not
    cause spurious rebuilds.
    """
    try:
        provider = await SettingsRepository.get_value("embedding.provider", "local")
        if provider == "local":
            return (
                await SettingsRepository.get_value(
                    "embedding.local_model",
                    DEFAULT_LOCAL_EMBEDDING_MODEL,
                )
                or ""
            )
        return await SettingsRepository.get_value("embedding.external_model", "") or ""
    except Exception:
        logger.debug("Could not resolve active embedding model", exc_info=True)
        return ""


def _is_chroma_dimension_error(exc: BaseException) -> bool:
    """Heuristic: detect HNSW dimension / compaction failures from Chroma."""
    msg = str(exc).lower()
    return "compaction" in msg or "hnsw" in msg or "dimension" in msg or "dimensionality" in msg


async def _wait_for_ws_connection(app: FastAPI, timeout: float = 8.0) -> bool:
    """Poll ``app.state.ws_client`` until it reports connected or *timeout* expires.

    Returns ``True`` if the client became connected within the window.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        ws = getattr(app.state, "ws_client", None)
        if ws is not None and ws.is_connected():
            return True
        await asyncio.sleep(0.5)
    return False


async def _prime_entity_index(app: FastAPI, ha_client: HARestClient, entity_index: EntityIndex, vector_store) -> None:
    """Fetch HA states and build/sync the entity index in the background."""
    import time as _time

    _t0 = _time.monotonic()
    try:
        states = await ha_client.get_states()
        _t1 = _time.monotonic()
        logger.info("Entity index prime: HA states fetched in %.1fs (%d entities)", _t1 - _t0, len(states))
        area_lookup, alias_lookup, device_lookup, area_id_lookup = await _gather_ha_lookups(ha_client)
        _store_entity_lookups(app, area_lookup, alias_lookup, device_lookup, area_id_lookup)
        hidden_ids = await ha_client.get_hidden_entity_ids()
        app.state.hidden_entity_ids = hidden_ids
        entities = parse_ha_states(
            states,
            area_lookup=area_lookup,
            alias_lookup=alias_lookup,
            device_lookup=device_lookup,
            area_id_lookup=area_id_lookup,
            hidden_ids=hidden_ids,
        )
        # 0.23.0: detect stale on-disk index built before the
        # EntityIndexEntry shape changed -- or built with a different
        # embedding model whose dimension no longer matches the
        # active one -- and force a rebuild.
        force_rebuild = False
        try:
            from app.entity.index import INDEX_SCHEMA_VERSION

            stored_version = await SettingsRepository.get_value("entity_index.schema_version", "0")
            if int(stored_version or 0) != INDEX_SCHEMA_VERSION:
                force_rebuild = True
        except (ImportError, ValueError, aiosqlite.OperationalError):
            INDEX_SCHEMA_VERSION = 0  # type: ignore[assignment]  # noqa: N806

        active_model = await _resolve_active_embedding_model()
        stored_model = ""
        try:
            stored_model = await SettingsRepository.get_value("entity_index.embedding_model", "") or ""
        except Exception:
            logger.debug("Could not read entity_index.embedding_model", exc_info=True)
        model_changed = bool(active_model) and bool(stored_model) and active_model != stored_model

        try:
            existing_count = vector_store.count(COLLECTION_ENTITY_INDEX)
        except Exception:
            logger.warning(
                "vector_store.count failed for %s -- forcing rebuild",
                COLLECTION_ENTITY_INDEX,
                exc_info=True,
            )
            existing_count = 0
            force_rebuild = True

        # Drop & rebuild trigger: schema bump, model switch, or empty
        # collection while there are entities to index (covers the case
        # where the persisted model setting was never written).
        drop_required = force_rebuild or model_changed or (existing_count == 0 and len(entities) > 0)
        if drop_required:
            try:
                vector_store.delete_collection(COLLECTION_ENTITY_INDEX)
                logger.warning(
                    "Dropped Chroma collection %s before rebuild "
                    "(force_rebuild=%s, model_changed=%s, existing_count=%d, "
                    "active_model=%r, stored_model=%r)",
                    COLLECTION_ENTITY_INDEX,
                    force_rebuild,
                    model_changed,
                    existing_count,
                    active_model,
                    stored_model,
                )
                existing_count = 0
                force_rebuild = True
            except Exception:
                logger.warning(
                    "Failed to drop Chroma collection %s before rebuild",
                    COLLECTION_ENTITY_INDEX,
                    exc_info=True,
                )

        if existing_count > 0 and not force_rebuild:
            _set_entity_index_pending_status(entity_index, state="syncing", total=len(entities))
            result = await entity_index.sync_async(entities)
            logger.info(
                "Entity index synced in background (existing=%d): +%d ~%d -%d =%d",
                existing_count,
                result["added"],
                result["updated"],
                result["removed"],
                result["unchanged"],
            )
        else:
            _set_entity_index_pending_status(entity_index, state="building", total=len(entities))
            try:
                await entity_index.populate_async(entities)
            except Exception as exc:
                if not _is_chroma_dimension_error(exc):
                    raise
                logger.warning(
                    "Entity index populate failed with chroma compaction/HNSW "
                    "error -- dropping collection %s and retrying once: %s",
                    COLLECTION_ENTITY_INDEX,
                    exc,
                )
                try:
                    vector_store.delete_collection(COLLECTION_ENTITY_INDEX)
                except Exception:
                    logger.error(
                        "Failed to drop %s after populate error",
                        COLLECTION_ENTITY_INDEX,
                        exc_info=True,
                    )
                    raise
                try:
                    await entity_index.populate_async(entities)
                except Exception:
                    logger.error(
                        "Entity index populate retry after collection drop also "
                        "failed; entity matching will be degraded until next sync",
                        exc_info=True,
                    )
                    return
            logger.info(
                "Entity index populated in background with %d entities (force_rebuild=%s)",
                len(entities),
                force_rebuild,
            )
        try:
            from app.entity.index import INDEX_SCHEMA_VERSION as _ISV

            await SettingsRepository.set("entity_index.schema_version", str(_ISV))
        except Exception:
            logger.debug("Could not persist entity_index.schema_version", exc_info=True)
        if active_model:
            try:
                await SettingsRepository.set("entity_index.embedding_model", active_model)
            except Exception:
                logger.debug("Could not persist entity_index.embedding_model", exc_info=True)
        # 0.23.0: load any user-supplied alias overrides (empty by default).
        try:
            from app.entity.user_aliases import load_user_aliases

            await load_user_aliases()
        except Exception:
            logger.debug("User alias load failed", exc_info=True)
        try:
            await entity_index.warmup_async()
            _t_end = _time.monotonic()
            logger.info("Entity index HNSW warm-up completed (total prime: %.1fs)", _t_end - _t0)
        except Exception:
            logger.debug("Entity index warm-up failed", exc_info=True)
    except Exception:
        logger.warning("Failed to prime entity index in background", exc_info=True)


async def schedule_entity_index_prime(
    app: FastAPI,
    ha_client: HARestClient,
    entity_index: EntityIndex,
    vector_store,
) -> bool:
    """Ensure a single background task exists to build/sync the entity index."""
    task = getattr(app.state, "entity_index_init_task", None)
    if task is not None and not task.done():
        return False
    app.state.entity_index_init_task = _spawn(_prime_entity_index(app, ha_client, entity_index, vector_store))
    return True


async def _periodic_entity_sync(app: FastAPI) -> None:
    """Periodically sync entity index with Home Assistant state."""
    while True:
        try:
            raw = await SettingsRepository.get_value(
                "entity_sync.interval_minutes", str(_ENTITY_SYNC_DEFAULT_INTERVAL_MIN)
            )
            interval_minutes = int(raw or str(_ENTITY_SYNC_DEFAULT_INTERVAL_MIN))
        except (TypeError, ValueError):
            interval_minutes = _ENTITY_SYNC_DEFAULT_INTERVAL_MIN

        if interval_minutes <= 0:
            await asyncio.sleep(_ENTITY_SYNC_DISABLED_RECHECK_SEC)
            continue

        await asyncio.sleep(interval_minutes * 60)

        try:
            ha_client = app.state.ha_client
            entity_index = app.state.entity_index
            if not ha_client or not entity_index:
                continue

            states = await ha_client.get_states()
            area_lookup, alias_lookup, device_lookup, area_id_lookup = await _gather_ha_lookups(ha_client)
            _store_entity_lookups(app, area_lookup, alias_lookup, device_lookup, area_id_lookup)
            hidden_ids = await ha_client.get_hidden_entity_ids()
            app.state.hidden_entity_ids = hidden_ids
            entities = parse_ha_states(
                states,
                area_lookup=area_lookup,
                alias_lookup=alias_lookup,
                device_lookup=device_lookup,
                area_id_lookup=area_id_lookup,
                hidden_ids=hidden_ids,
            )
            result = await entity_index.sync_async(entities)
            logger.info(
                "Periodic entity sync: +%d ~%d -%d =%d",
                result["added"],
                result["updated"],
                result["removed"],
                result["unchanged"],
            )
        except Exception:
            logger.warning("Periodic entity sync failed", exc_info=True)


async def _purge_stale_response_cache(cache_manager: CacheManager) -> None:
    """One-time startup task: purge stale read-only response cache entries."""
    try:
        count = await cache_manager.purge_readonly_entries()
        if count:
            logger.info("Purged %d stale read-only response cache entries", count)
        else:
            logger.info("No stale read-only response cache entries to purge")
    except Exception:
        logger.warning("Failed to purge stale response cache entries", exc_info=True)


def _has_relevant_changes(data: dict, changes: dict) -> bool:
    """Return True if the event contains changes that affect cache semantics."""
    action = data.get("action")
    if action == "remove":
        return True
    # Check both data and changes for relevant fields.
    # Home Assistant may put changed fields in either location depending on event type.
    for source in (data, changes):
        if not isinstance(source, dict):
            continue
        for key in source:
            if key == "entity_id" and source is data:
                continue
            if key in RELEVANT_REGISTRY_FIELDS:
                return True
    return False


def _resolve_registry_event_entity_ids(app: FastAPI, entity_index: EntityIndex, event: dict) -> list[str]:
    """Resolve affected entity ids for HA registry events."""
    data = event.get("data") or {}
    changes = data.get("changes") or {}
    if not isinstance(changes, dict):
        changes = {}
    entity_ids: set[str] = set()

    direct_entity_id = data.get("entity_id")
    if isinstance(direct_entity_id, str) and direct_entity_id:
        entity_ids.add(direct_entity_id)
    changed_entity_id = changes.get("entity_id")
    if isinstance(changed_entity_id, str) and changed_entity_id:
        entity_ids.add(changed_entity_id)
    raw_entity_ids = data.get("entity_ids")
    if isinstance(raw_entity_ids, list):
        entity_ids.update(str(item) for item in raw_entity_ids if item)
    changed_entity_ids = changes.get("entity_ids")
    if isinstance(changed_entity_ids, list):
        entity_ids.update(str(item) for item in changed_entity_ids if item)
    if entity_ids:
        if _has_relevant_changes(data, changes):
            return sorted(entity_ids)
        return []

    entries = entity_index.list_entries()

    area_ids = {
        str(value)
        for key in ("area_id", "old_area_id", "new_area_id")
        for value in [data.get(key), changes.get(key)]
        if value
    }
    if area_ids:
        entity_ids.update(entry.entity_id for entry in entries if (entry.area or "") in area_ids)

    names = {
        str(value).strip().lower()
        for key in ("name", "old_name", "new_name", "device_name")
        for value in [data.get(key), changes.get(key)]
        if isinstance(value, str) and value.strip()
    }
    if names:
        entity_ids.update(entry.entity_id for entry in entries if (entry.device_name or "").strip().lower() in names)

    if entity_ids:
        return sorted(entity_ids)

    lookups = getattr(app.state, "entity_lookups", None) or {}
    area_lookup = lookups.get("area_id") or {}
    if area_ids:
        entity_ids.update(entity_id for entity_id, area_id in area_lookup.items() if area_id in area_ids)
    return sorted(entity_ids)


async def _refresh_registry_entities(
    app: FastAPI,
    ha_client: HARestClient,
    entity_index: EntityIndex,
    entity_ids: list[str],
) -> None:
    """Refresh or remove entity-index rows affected by a HA registry event."""
    if not entity_ids:
        return

    area_lookup, alias_lookup, device_lookup, area_id_lookup = await _gather_ha_lookups(ha_client)
    _store_entity_lookups(app, area_lookup, alias_lookup, device_lookup, area_id_lookup)
    hidden_ids = await ha_client.get_hidden_entity_ids()
    app.state.hidden_entity_ids = hidden_ids

    for entity_id in entity_ids:
        try:
            state = await ha_client.get_state(entity_id)
        except Exception:
            logger.warning("Registry refresh failed for %s", entity_id, exc_info=True)
            continue

        if state is None or entity_id in hidden_ids:
            await entity_index.remove_async(entity_id)
            continue

        entry = state_to_entity_index_entry(
            state,
            entity_id=entity_id,
            area_lookup=area_lookup,
            alias_lookup=alias_lookup,
            device_lookup=device_lookup,
            area_id_lookup=area_id_lookup,
        )
        await entity_index.add_async(entry)


async def _initialize_setup_dependent_services(app: FastAPI, *, source: str) -> None:
    """Idempotent core of the setup-dependent bootstrap.

    FLOW-SETUP-1 (P1-2): single canonical implementation used by both the
    FastAPI ``lifespan`` on fresh container startup (``source="lifespan"``)
    and by :func:`ensure_setup_runtime_initialized` which runs after the
    user completes the setup wizard in a long-running process
    (``source="post-setup"``). Each step is individually idempotent --
    re-entering after a partial init reuses whatever ``app.state``
    already carries instead of re-instantiating.
    """
    registry = getattr(app.state, "registry", None)
    dispatcher = getattr(app.state, "dispatcher", None)
    mcp_registry = getattr(app.state, "mcp_registry", None)
    mcp_tool_manager = getattr(app.state, "mcp_tool_manager", None)
    if registry is None or dispatcher is None or mcp_registry is None or mcp_tool_manager is None:
        raise RuntimeError("Core runtime state is not ready for setup initialization")
    if getattr(app.state, "orchestrator_gateway", None) is None:
        app.state.orchestrator_gateway = OrchestratorGateway(dispatcher)

    logger.info("Setup init (%s): initializing setup-dependent services", source)

    await get_embedding_engine()
    vector_store = await get_vector_store()

    ha_client = getattr(app.state, "ha_client", None)
    if ha_client is None:
        ha_client = HARestClient()
        await ha_client.initialize()
    else:
        await ha_client.reload()
    app.state.ha_client = ha_client

    entity_index = getattr(app.state, "entity_index", None)
    if entity_index is None:
        entity_index = EntityIndex(vector_store)
        app.state.entity_index = entity_index

    await schedule_entity_index_prime(app, ha_client, entity_index, vector_store)

    try:
        await home_context_provider.refresh(ha_client)
    except Exception:
        logger.warning("Setup init (%s): failed to pre-warm HomeContext cache", source, exc_info=True)

    alias_resolver = getattr(app.state, "alias_resolver", None)
    if alias_resolver is None:
        alias_resolver = AliasResolver()
        await alias_resolver.load()
        app.state.alias_resolver = alias_resolver

    await asyncio.to_thread(preload_prompt_cache)

    entity_matcher = getattr(app.state, "entity_matcher", None)
    if entity_matcher is None:
        entity_matcher = EntityMatcher(entity_index, alias_resolver)
        await entity_matcher.load_config()
        # 0.23.0: wire optional language-agnostic on-demand expansion.
        try:
            from app.entity.expansion import QueryExpansionService, load_query_expansion_prompt_template

            async def _llm_expand(prompt: str) -> str:
                # Use the orchestrator-tier LLM for expansion (cheap,
                # cached). Fail-soft: any error returns empty string so
                # the matcher falls through.
                try:
                    from app.llm.client import complete

                    return await complete(
                        "orchestrator",
                        [{"role": "user", "content": prompt}],
                        max_tokens=200,
                        temperature=0.0,
                    )
                except Exception:
                    return ""

            prompt_template = await asyncio.to_thread(load_query_expansion_prompt_template)
            entity_matcher._expansion_service = QueryExpansionService(
                llm_call=_llm_expand,
                prompt_template=prompt_template,
            )
        except Exception:
            logger.debug("Expansion service wiring skipped", exc_info=True)
        try:
            entity_matcher._index_language = await ha_client.get_user_language()
        except Exception:
            entity_matcher._index_language = None
        app.state.entity_matcher = entity_matcher

    rewrite_agent = getattr(app.state, "rewrite_agent", None)
    if rewrite_agent is None:
        rewrite_agent = RewriteAgent(ha_client=ha_client, entity_index=entity_index)
        app.state.rewrite_agent = rewrite_agent

    cache_manager = getattr(app.state, "cache_manager", None)
    if cache_manager is None:
        cache_manager = CacheManager(vector_store, rewrite_agent=rewrite_agent)
        await cache_manager.initialize()
        app.state.cache_manager = cache_manager

    purge_task = getattr(app.state, "purge_task", None)
    if purge_task is None or purge_task.done():
        app.state.purge_task = _spawn(_purge_stale_response_cache(cache_manager))

    # Ensure LLM client wrapper is available for components that need it
    if getattr(app.state, "llm_client", None) is None:

        class _LLMClientWrapper:
            """Thin wrapper that calls litellm directly without requiring an agent DB config."""

            async def complete(self, agent_id: str, messages: list, **kwargs):
                import litellm

                from app.llm.providers import resolve_provider_params

                model = kwargs.get("model")
                if not model:
                    raise ValueError("model is required")

                provider_params = await resolve_provider_params(model)

                call_kwargs = dict(
                    model=model,
                    messages=messages,
                    max_tokens=kwargs.get("max_tokens", 256),
                    temperature=kwargs.get("temperature", 0.2),
                    timeout=kwargs.get("timeout", 60),
                    **provider_params,
                )
                reasoning_effort = kwargs.get("reasoning_effort")
                if reasoning_effort:
                    call_kwargs["reasoning_effort"] = reasoning_effort
                    call_kwargs["drop_params"] = True

                response = await litellm.acompletion(**call_kwargs)
                if not response.choices:
                    raise RuntimeError("Empty choices from provider")
                content = (response.choices[0].message.content or "").strip() if response.choices[0].message else ""
                return content

        app.state.llm_client = _LLMClientWrapper()

    cache_validator = getattr(app.state, "cache_validator", None)
    if cache_validator is None:
        from app.cache.cache_validator import ActionCacheValidator

        cache_validator = ActionCacheValidator(
            action_cache=cache_manager.action_cache,
            cache_manager=cache_manager,
            entity_index=entity_index,
            ha_client=ha_client,
            llm_client=app.state.llm_client,
        )
        app.state.cache_validator = cache_validator

    validator_task = getattr(app.state, "cache_validator_task", None)
    if validator_task is None or validator_task.done():
        app.state.cache_validator_task = _spawn(cache_validator.run_periodic(), name="cache_validator")

    try:
        await mcp_registry.load_from_db()
    except Exception:
        logger.warning("Setup init (%s): failed to load MCP servers from DB", source, exc_info=True)

    from app.db.repository import AgentMcpToolsRepository, McpServerRepository

    ddg_server = await McpServerRepository.get("duckduckgo-search")
    if ddg_server is None:
        logger.info("Setup init (%s): registering built-in DuckDuckGo MCP server", source)
        connected = await mcp_registry.add_server(
            name="duckduckgo-search",
            transport="stdio",
            command_or_url="python -m app.mcp.servers.duckduckgo_server",
        )
        if connected:
            try:
                tools = await mcp_tool_manager.refresh_server("duckduckgo-search")
                for tool in tools:
                    await AgentMcpToolsRepository.assign_tool(
                        "general-agent",
                        "duckduckgo-search",
                        tool["name"],
                    )
                logger.info("Assigned %d DuckDuckGo tools to general-agent", len(tools))
            except Exception:
                logger.warning(
                    "Setup init (%s): failed to auto-assign DuckDuckGo tools",
                    source,
                    exc_info=True,
                )
        else:
            logger.warning(
                "Setup init (%s): DuckDuckGo MCP server registered but failed to connect",
                source,
            )

    wiki_server = await McpServerRepository.get("wikipedia-search")
    if wiki_server is None:
        logger.info("Setup init (%s): registering built-in Wikipedia MCP server", source)
        connected = await mcp_registry.add_server(
            name="wikipedia-search",
            transport="stdio",
            command_or_url="python -m app.mcp.servers.wikipedia_server",
        )
        if connected:
            try:
                tools = await mcp_tool_manager.refresh_server("wikipedia-search")
                for tool in tools:
                    await AgentMcpToolsRepository.assign_tool(
                        "general-agent",
                        "wikipedia-search",
                        tool["name"],
                    )
                logger.info("Assigned %d Wikipedia tools to general-agent", len(tools))
            except Exception:
                logger.warning(
                    "Setup init (%s): failed to auto-assign Wikipedia tools",
                    source,
                    exc_info=True,
                )
        else:
            logger.warning(
                "Setup init (%s): Wikipedia MCP server registered but failed to connect",
                source,
            )

    ha_url = await SettingsRepository.get_value("ha_url")
    ha_token = await get_ha_token()
    if ha_url and ha_token:
        ha_action_server = await McpServerRepository.get("ha-action")
        if ha_action_server is None:
            logger.info("Setup init (%s): registering built-in HA action MCP server", source)
            connected = await mcp_registry.add_server(
                name="ha-action",
                transport="stdio",
                command_or_url="python -m app.mcp.servers.ha_action_server",
                env_vars={"HA_URL": ha_url, "HA_TOKEN": ha_token},
            )
            if connected:
                try:
                    tools = await mcp_tool_manager.refresh_server("ha-action")
                    for tool in tools:
                        await AgentMcpToolsRepository.assign_tool(
                            "general-agent",
                            "ha-action",
                            tool["name"],
                        )
                    logger.info("Assigned %d HA action tools to general-agent", len(tools))
                except Exception:
                    logger.warning(
                        "Setup init (%s): failed to auto-assign HA action tools",
                        source,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "Setup init (%s): HA action MCP server registered but failed to connect",
                    source,
                )
    else:
        logger.info("Setup init (%s): skipping HA action MCP server -- HA not yet configured", source)

    await install_all_agents(app)

    custom_loader = getattr(app.state, "custom_loader", None)
    if custom_loader is None:
        custom_loader = CustomAgentLoader(
            registry,
            ha_client=ha_client,
            entity_index=entity_index,
            mcp_tool_manager=mcp_tool_manager,
        )
        await custom_loader.load_all()
        app.state.custom_loader = custom_loader
    else:
        custom_loader._ha_client = ha_client
        custom_loader._entity_index = entity_index
        custom_loader._mcp_tool_manager = mcp_tool_manager
        reload_result = custom_loader.reload()
        if inspect.isawaitable(reload_result):
            await reload_result

    ws_client = getattr(app.state, "ws_client", None)
    if ws_client is None:
        from app.ha_client.websocket import HAWebSocketClient

        ws_client = HAWebSocketClient()
        entity_update_queue: asyncio.Queue = asyncio.Queue()

        async def _flush_entity_updates() -> None:
            while True:
                await asyncio.sleep(_ENTITY_UPDATE_FLUSH_INTERVAL_SEC)
                batch = []
                while True:
                    try:
                        batch.append(entity_update_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if batch:
                    try:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, entity_index.batch_add, batch)
                    except Exception:
                        logger.warning("Batch entity index update failed", exc_info=True)

        async def on_state_changed(event: dict) -> None:
            data = event.get("data", {})
            new_state = data.get("new_state")
            old_state = data.get("old_state")
            entity_id = data.get("entity_id", "")

            hidden_ids: set[str] = getattr(app.state, "hidden_entity_ids", None) or set()
            if entity_id in hidden_ids:
                if old_state is not None:
                    await entity_index.remove_async(entity_id)
                return

            if new_state is None and old_state is not None:
                await entity_index.remove_async(entity_id)
            elif new_state is not None:
                lookups = getattr(app.state, "entity_lookups", None) or {}
                entry = state_to_entity_index_entry(
                    new_state,
                    entity_id=entity_id,
                    area_lookup=lookups.get("area") or {},
                    alias_lookup=lookups.get("alias") or {},
                    device_lookup=lookups.get("device") or {},
                    area_id_lookup=lookups.get("area_id") or {},
                )
                entity_update_queue.put_nowait(entry)

        async def _invalidate_registry_event(event: dict) -> list[str]:
            resolved_entity_ids = _resolve_registry_event_entity_ids(app, entity_index, event)
            if not resolved_entity_ids:
                return []
            event_type = event.get("event_type", "unknown")
            try:
                counts = await cache_manager.invalidate_by_entity_id(resolved_entity_ids)
                logger.info(
                    "Cache invalidation succeeded for %s: entities=%s action=%d routing=%d",
                    event_type,
                    sorted(resolved_entity_ids),
                    counts.get("action", 0),
                    counts.get("routing", 0),
                )
            except Exception:
                logger.warning(
                    "Cache invalidation for registry event failed: %s",
                    sorted(resolved_entity_ids),
                    exc_info=True,
                )
            return resolved_entity_ids

        async def on_entity_registry_updated(event: dict) -> None:
            ha_client.clear_area_registry_cache()
            resolved_entity_ids = await _invalidate_registry_event(event)
            await _refresh_registry_entities(app, ha_client, entity_index, resolved_entity_ids)

        async def on_device_registry_updated(event: dict) -> None:
            resolved_entity_ids = await _invalidate_registry_event(event)
            await _refresh_registry_entities(app, ha_client, entity_index, resolved_entity_ids)

        async def on_area_registry_updated(event: dict) -> None:
            resolved_entity_ids = await _invalidate_registry_event(event)
            await _refresh_registry_entities(app, ha_client, entity_index, resolved_entity_ids)

        ws_client.on_event("state_changed", on_state_changed)
        ws_client.on_event("entity_registry_updated", on_entity_registry_updated)
        ws_client.on_event("device_registry_updated", on_device_registry_updated)
        ws_client.on_event("area_registry_updated", on_area_registry_updated)

        app.state.ws_client = ws_client
        app.state.ws_task = _spawn(ws_client.run())
        app.state.flush_task = _spawn(_flush_entity_updates())

    if ha_client is not None and ws_client is not None:
        ha_client.set_state_observer(ws_client)

    # WS-HIDDEN-SYNC: the first entity prime often runs before the WS client
    # has connected.  Spawn a tiny background task that waits for the WS,
    # fetches hidden entities over the WebSocket, and re-syncs the index
    # if any hidden IDs were discovered post-startup.
    async def _deferred_hidden_entity_sync() -> None:
        logger.info("Deferred hidden-entity sync task started")
        try:
            connected = await _wait_for_ws_connection(app, timeout=10.0)
            logger.info("Deferred hidden-entity sync: ws_connected=%s", connected)
            if connected:
                # Give _receive_loop() a moment to start so send_command()
                # does not race into a timeout.
                await asyncio.sleep(1.0)
                # Call the WebSocket client directly to bypass the REST
                # client's empty-set cache from the pre-WS startup sync.
                hidden_ids = await ws_client.get_hidden_entity_ids()
                logger.info("Deferred hidden-entity sync: hidden_ids=%d", len(hidden_ids))
                if hidden_ids:
                    app.state.hidden_entity_ids = hidden_ids
                    states = await ha_client.get_states()
                    lookups = getattr(app.state, "entity_lookups", None) or {}
                    entities = parse_ha_states(
                        states,
                        area_lookup=lookups.get("area") or {},
                        alias_lookup=lookups.get("alias") or {},
                        device_lookup=lookups.get("device") or {},
                        area_id_lookup=lookups.get("area_id") or {},
                        hidden_ids=hidden_ids,
                    )
                    result = await entity_index.sync_async(entities)
                    logger.info(
                        "Deferred hidden-entity re-sync: +%d ~%d -%d =%d (hidden=%d)",
                        result["added"],
                        result["updated"],
                        result["removed"],
                        result["unchanged"],
                        len(hidden_ids),
                    )
        except Exception:
            logger.warning("Deferred hidden-entity sync failed", exc_info=True)

    _spawn(_deferred_hidden_entity_sync())

    sync_task = getattr(app.state, "sync_task", None)
    if sync_task is None or sync_task.done():
        app.state.sync_task = _spawn(_periodic_entity_sync(app))

    alarm_monitor = getattr(app.state, "alarm_monitor", None)
    if alarm_monitor is None:
        from app.agents.alarm_monitor import AlarmMonitor

        alarm_monitor = AlarmMonitor(entity_index, app.state.orchestrator_gateway)
        await alarm_monitor.start()
        app.state.alarm_monitor = alarm_monitor

    timer_scheduler = getattr(app.state, "timer_scheduler", None)
    if timer_scheduler is None:
        from app.agents.timer_scheduler import TimerScheduler
        from app.db.repository import ScheduledTimersRepository

        timer_scheduler = TimerScheduler(
            ScheduledTimersRepository,
            orchestrator_gateway=app.state.orchestrator_gateway,
        )
        await timer_scheduler.start()
        app.state.timer_scheduler = timer_scheduler

    logger.info("Setup init (%s): completed", source)


async def ensure_setup_runtime_initialized(app: FastAPI) -> bool:
    """Initialize setup-dependent runtime services after setup completion.

    Returns ``True`` when initialization work was performed in this call,
    otherwise ``False``.
    """
    if getattr(app.state, "setup_runtime_initialized", False):
        return False

    lock = getattr(app.state, "setup_runtime_init_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.setup_runtime_init_lock = lock

    async with lock:
        if getattr(app.state, "setup_runtime_initialized", False):
            return False
        if not await SetupStateRepository.is_complete():
            return False

        await _initialize_setup_dependent_services(app, source="post-setup")

        app.state.setup_runtime_initialized = True
        logger.info("Setup-dependent runtime initialized in-process")
        return True
