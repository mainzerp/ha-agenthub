"""Bootstrap: entity index creation, priming, periodic sync, WS observers, deferred hidden sync."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiosqlite

from app.bootstrap._tasks import spawn_background
from app.cache.vector_store import COLLECTION_ENTITY_INDEX
from app.db.repository import SettingsRepository
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL
from app.entity.index import EntityIndex
from app.entity.ingest import parse_ha_states, state_to_entity_index_entry

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.ha_client.rest import HARestClient

logger = logging.getLogger(__name__)

# P3-11: tunables for the runtime background loops. Kept module-level
# so they can be inspected / overridden from tests via monkeypatch
# without touching the call sites.
_ENTITY_SYNC_DEFAULT_INTERVAL_MIN = 30
_ENTITY_SYNC_DISABLED_RECHECK_SEC = 300
_ENTITY_UPDATE_FLUSH_INTERVAL_SEC = 0.5
_CACHE_VALIDATOR_DEFAULT_INTERVAL_MIN = 60

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


def _is_dimension_error(exc: BaseException) -> bool:
    """Heuristic: detect sqlite-vec / vec0 dimension-mismatch failures."""
    msg = str(exc).lower()
    return "dimension" in msg or "dimensionality" in msg or "vec0" in msg


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
                    "Dropped vector collection %s before rebuild "
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
                    "Failed to drop vector collection %s before rebuild",
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
                if not _is_dimension_error(exc):
                    raise
                logger.warning(
                    "Entity index populate failed with a vector dimension "
                    "mismatch -- dropping collection %s and retrying once: %s",
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
            _t_end = _time.monotonic()
            logger.info("Entity index prime complete (total: %.1fs)", _t_end - _t0)
        except Exception:
            logger.debug("Entity index prime timing log failed", exc_info=True)
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
    spawn_background(app, _prime_entity_index(app, ha_client, entity_index, vector_store), "entity_index_init_task")
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


async def setup_entity_index(app: FastAPI, source: str, ha_client: HARestClient, vector_store) -> EntityIndex:
    """Create EntityIndex, schedule background prime. Stores on ``app.state.entity_index``."""
    entity_index = getattr(app.state, "entity_index", None)
    if entity_index is None:
        entity_index = EntityIndex(vector_store)
        app.state.entity_index = entity_index

    await schedule_entity_index_prime(app, ha_client, entity_index, vector_store)
    return entity_index


async def setup_entity_observers(
    app: FastAPI,
    source: str,
    ha_client: HARestClient,
    entity_index: EntityIndex,
    cache_manager,
) -> None:
    """Set up WebSocket client, state/registry event handlers, deferred hidden sync, periodic sync.

    Stores ``ws_client``, ``ws_task``, ``flush_task``, ``sync_task`` on ``app.state``.
    Must be called *after* ``setup_cache`` so ``cache_manager`` is available for registry invalidation.
    """
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
        spawn_background(app, ws_client.run(), "ws_task")
        spawn_background(app, _flush_entity_updates(), "flush_task")

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

    spawn_background(app, _deferred_hidden_entity_sync(), "deferred_hidden_sync")

    sync_task = getattr(app.state, "sync_task", None)
    if sync_task is None or sync_task.done():
        spawn_background(app, _periodic_entity_sync(app), "sync_task")
