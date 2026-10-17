"""Bootstrap: entity index creation, priming, periodic sync, WS observers, deferred hidden sync."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import aiosqlite

from app.bootstrap._tasks import spawn_background
from app.cache.vector_store import COLLECTION_ENTITY_INDEX
from app.db.repository import SettingsRepository
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL
from app.entity.index import EntityIndex
from app.entity.ingest import parse_ha_states, state_to_entity_index_entry
from app.ha_client.persons import PERSON_ENTITY_PREFIX, get_person_directory, person_identity_changed
from app.security.sensitive_actions import register_entity_index
from app.util.tasks import spawn

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.ha_client.rest import HARestClient
    from app.models.entity_index import EntityIndexEntry

logger = logging.getLogger(__name__)

# P3-11: tunables for the runtime background loops. Kept module-level
# so they can be inspected / overridden from tests via monkeypatch
# without touching the call sites.
_ENTITY_SYNC_DEFAULT_INTERVAL_MIN = 30
_ENTITY_SYNC_DISABLED_RECHECK_SEC = 300
_ENTITY_UPDATE_FLUSH_INTERVAL_SEC = 0.5
_CACHE_VALIDATOR_DEFAULT_INTERVAL_MIN = 60

# Entity-registry fields whose change can alter what a cached action means or
# whether it may replay: identity (rename), the names and aliases resolution
# matches on, area / device placement, visibility (hidden / disabled) and the
# device class visibility rules filter on. Cosmetic fields (icon, labels,
# ``original_*`` values integrations rewrite on upgrades) are deliberately
# absent: invalidating on them deleted valid action-cache rows.
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
        "disabled_by",
        "aliases",
    }
)

# Device-registry fields that change the entities' resolution inputs: the
# device name (indexed as ``device_name``) and the area entities inherit.
# Disabling a device is reported per entity by HA (entity ``disabled_by``).
RELEVANT_DEVICE_FIELDS = frozenset({"name", "name_by_user", "area_id"})

# Set on the EntityIndex instance created at bootstrap: False until the first
# full HA snapshot is installed. An index that was never tracked counts as
# loaded so tests and ad-hoc indexes keep the fail-closed behaviour.
_SNAPSHOT_LOADED_ATTR = "initial_snapshot_loaded"


def entity_index_snapshot_loaded(entity_index: EntityIndex | None) -> bool:
    """Return True once ``entity_index`` holds a full HA snapshot.

    Readiness signal for the action-cache lookup: a visibility recheck that
    fails because the index has not loaded yet is a metadata gap, not proof
    that the cached row is stale. The replay is still refused.
    """
    if entity_index is None:
        return False
    return getattr(entity_index, _SNAPSHOT_LOADED_ATTR, True) is not False


def _mark_snapshot_loaded(entity_index: EntityIndex | None, loaded: bool) -> None:
    if entity_index is None:
        return
    was_loaded = entity_index_snapshot_loaded(entity_index)
    try:
        setattr(entity_index, _SNAPSHOT_LOADED_ATTR, loaded)
    except Exception:
        logger.debug("Could not set entity index snapshot readiness", exc_info=True)
        return
    if loaded and not was_loaded:
        logger.info("Entity index snapshot loaded; action-cache visibility invalidation enabled")


def _set_entity_index_pending_status(entity_index: EntityIndex, *, state: str, total: int) -> None:
    """Mark entity index as building/syncing before background priming starts."""
    entity_index._status = {
        "state": state,
        "progress": 0,
        "total": total,
        "processed": 0,
        "error": None,
    }


async def _gather_ha_lookups(ha_client: HARestClient, previous: dict | None = None) -> tuple[dict, dict, dict, dict]:
    """Fetch area, alias, device, and entity-area lookups from HA in parallel.

    Each individual fetch is wrapped so a partial outage degrades instead
    of failing the entire entity sync. A fetch that raises falls back to
    the matching lookup in ``previous`` (the last published
    ``app.state.entity_lookups``) rather than an empty dict, so one failed
    registry call never wipes area / alias / device enrichment. The REST
    client itself already returns its last-good lookup on a failed render.
    """
    previous = previous or {}

    async def _safe(coro_factory, previous_key: str):
        try:
            return await coro_factory()
        except Exception:
            logger.warning("HA registry lookup %s failed; keeping previous lookup", previous_key, exc_info=True)
            return previous.get(previous_key)

    area_lookup, alias_lookup, device_lookup, area_id_lookup = await asyncio.gather(
        _safe(ha_client.get_area_registry, "area"),
        _safe(ha_client.get_entity_aliases, "alias"),
        _safe(ha_client.get_device_names, "device"),
        _safe(ha_client.get_entity_areas, "area_id"),
    )
    return (
        area_lookup or {},
        alias_lookup or {},
        device_lookup or {},
        area_id_lookup or {},
    )


def _previous_lookups(app: FastAPI) -> dict:
    lookups = getattr(app.state, "entity_lookups", None)
    return lookups if isinstance(lookups, dict) else {}


def _set_area_assignment_status(
    entity_index: EntityIndex | None, ha_client: HARestClient, area_id_lookup: dict
) -> None:
    """Publish whether HA area assignments are known on the entity index.

    Unknown means the entity-area registry lookup has never succeeded (the
    REST client has no last-good data) and nothing was published before.
    Visibility then fails closed for area-less entities under
    ``area_exclude`` rules instead of treating ``area=None`` as "no area".
    """
    if entity_index is None:
        return
    known = True
    if not area_id_lookup:
        probe = getattr(ha_client, "has_registry_data", None)
        if callable(probe):
            try:
                known = bool(probe("entity_areas"))
            except Exception:
                known = True
    if getattr(entity_index, "area_assignments_known", True) != known:
        logger.warning("HA area assignments known=%s (entity-area registry lookup)", known)
    entity_index.area_assignments_known = known


def _snapshot_generation(entity_index: EntityIndex) -> int | None:
    """Read the index mutation generation before fetching an HA snapshot."""
    getter = getattr(entity_index, "mutation_generation", None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:
        return None
    return value if isinstance(value, int) else None


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


async def _reload_alias_resolver(app: FastAPI) -> None:
    """Reload the shared ``AliasResolver`` cache from the DB (best effort)."""
    from app.entity.aliases import AliasResolver

    resolver = getattr(app.state, "alias_resolver", None)
    if not isinstance(resolver, AliasResolver):
        return
    try:
        await resolver.reload()
    except Exception:
        logger.warning("Alias resolver reload after user alias load failed", exc_info=True)


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
        snapshot_generation = _snapshot_generation(entity_index)
        states = await ha_client.get_states()
        _t1 = _time.monotonic()
        logger.info("Entity index prime: HA states fetched in %.1fs (%d entities)", _t1 - _t0, len(states))
        area_lookup, alias_lookup, device_lookup, area_id_lookup = await _gather_ha_lookups(
            ha_client, _previous_lookups(app)
        )
        _store_entity_lookups(app, area_lookup, alias_lookup, device_lookup, area_id_lookup)
        _set_area_assignment_status(entity_index, ha_client, area_id_lookup)
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
            result = await entity_index.sync_async(entities, snapshot_generation=snapshot_generation)
            logger.info(
                "Entity index synced in background (existing=%d): +%d ~%d -%d =%d",
                existing_count,
                result["added"],
                result["updated"],
                result["removed"],
                result["unchanged"],
            )
            if entities:
                _mark_snapshot_loaded(entity_index, True)
        else:
            _set_entity_index_pending_status(entity_index, state="building", total=len(entities))
            try:
                await entity_index.populate_async(entities, snapshot_generation=snapshot_generation)
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
                    await entity_index.populate_async(entities, snapshot_generation=snapshot_generation)
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
            if entities:
                _mark_snapshot_loaded(entity_index, True)
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

            loaded = await load_user_aliases()
        except Exception:
            loaded = 0
            logger.debug("User alias load failed", exc_info=True)
        # The AliasResolver cache was filled in bootstrap phase 1, before the
        # YAML aliases above were written to the DB: reload so they resolve.
        if loaded:
            await _reload_alias_resolver(app)
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


async def build_entity_snapshot(app: FastAPI, ha_client: HARestClient) -> list[EntityIndexEntry]:
    """Fetch HA states + registry lookups and return enriched index entries.

    Shared between the periodic sync and the admin refresh endpoint so both
    apply the same enrichment (areas/aliases/devices) and hidden-entity
    filtering. Publishes the fresh lookups and ``hidden_entity_ids`` on
    ``app.state`` as a side effect.

    Callers that sync the result into the index read
    ``entity_index.mutation_generation()`` BEFORE calling this and pass it
    as ``snapshot_generation`` so newer WebSocket updates survive the sync.
    """
    states = await ha_client.get_states()
    area_lookup, alias_lookup, device_lookup, area_id_lookup = await _gather_ha_lookups(
        ha_client, _previous_lookups(app)
    )
    _store_entity_lookups(app, area_lookup, alias_lookup, device_lookup, area_id_lookup)
    _set_area_assignment_status(getattr(app.state, "entity_index", None), ha_client, area_id_lookup)
    hidden_ids = await ha_client.get_hidden_entity_ids()
    app.state.hidden_entity_ids = hidden_ids
    return parse_ha_states(
        states,
        area_lookup=area_lookup,
        alias_lookup=alias_lookup,
        device_lookup=device_lookup,
        area_id_lookup=area_id_lookup,
        hidden_ids=hidden_ids,
    )


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
            await resync_entity_index(app, reason="periodic")
        except Exception:
            logger.warning("Periodic entity sync failed", exc_info=True)


async def resync_entity_index(app: FastAPI, *, reason: str) -> dict | None:
    """Full entity-index resync from a fresh HA snapshot.

    Reads the index mutation generation before fetching the snapshot so
    WebSocket updates applied meanwhile are not undone. Returns the sync
    counts, or ``None`` when the HA client / index is not available.
    """
    ha_client = getattr(app.state, "ha_client", None)
    entity_index = getattr(app.state, "entity_index", None)
    if not ha_client or not entity_index:
        return None
    snapshot_generation = _snapshot_generation(entity_index)
    entities = await build_entity_snapshot(app, ha_client)
    result = await entity_index.sync_async(entities, snapshot_generation=snapshot_generation)
    logger.info(
        "Entity sync (%s): +%d ~%d -%d =%d",
        reason,
        result["added"],
        result["updated"],
        result["removed"],
        result["unchanged"],
    )
    # A prime that failed at startup leaves the index unloaded; the first
    # successful full sync (periodic, reconnect, admin refresh) loads it.
    if entities:
        _mark_snapshot_loaded(entity_index, True)
    return result


def _relevant_entity_changes(data: dict, changes: dict) -> list[str]:
    """Return the changed entity-registry fields that affect cached replay.

    ``["remove"]`` for a removal. HA reports the OLD values of changed fields
    in ``changes``; a rename additionally carries ``old_entity_id``. Only
    fields in ``RELEVANT_REGISTRY_FIELDS`` count -- ``create`` events and
    cosmetic updates (icon, labels, ``original_*``) return an empty list.
    """
    if data.get("action") == "remove":
        return ["remove"]
    fields: set[str] = set()
    if data.get("old_entity_id"):
        fields.add("entity_id")
    # Home Assistant may put changed fields in either location depending on event type.
    for source in (data, changes):
        if not isinstance(source, dict):
            continue
        for key in source:
            if key == "entity_id" and source is data:
                continue
            if key in RELEVANT_REGISTRY_FIELDS:
                fields.add(key)
    return sorted(fields)


def _event_changes(data: dict) -> dict:
    changes = data.get("changes") or {}
    return changes if isinstance(changes, dict) else {}


def _event_entity_ids(data: dict, changes: dict) -> set[str]:
    """Entity ids named by an entity-registry event (new and old id on a rename)."""
    entity_ids: set[str] = set()
    for value in (data.get("entity_id"), changes.get("entity_id"), data.get("old_entity_id")):
        if isinstance(value, str) and value:
            entity_ids.add(value)
    for raw in (data.get("entity_ids"), changes.get("entity_ids")):
        if isinstance(raw, list):
            entity_ids.update(str(item) for item in raw if item)
    return entity_ids


def _entities_in_areas(app: FastAPI, entity_index: EntityIndex, area_ids: set[str]) -> set[str]:
    """Indexed entities (or, as fallback, registry-mapped entities) in ``area_ids``."""
    if not area_ids:
        return set()
    entity_ids = {entry.entity_id for entry in entity_index.list_entries() if (entry.area or "") in area_ids}
    if entity_ids:
        return entity_ids
    lookups = getattr(app.state, "entity_lookups", None) or {}
    area_lookup = lookups.get("area_id") or {}
    return {entity_id for entity_id, area_id in area_lookup.items() if area_id in area_ids}


def _entities_with_device_names(entity_index: EntityIndex, names: set[str]) -> set[str]:
    wanted = {name.strip().lower() for name in names if name and name.strip()}
    if not wanted:
        return set()
    return {
        entry.entity_id for entry in entity_index.list_entries() if (entry.device_name or "").strip().lower() in wanted
    }


async def _device_entity_ids(ha_client: HARestClient, device_id: str) -> list[str] | None:
    """Entity ids attached to ``device_id`` via HA's ``device_entities`` template.

    Returns None when the lookup fails so callers can fall back to a broader,
    conservative match instead of skipping the invalidation.
    """
    try:
        rendered = await ha_client.render_template(
            "{{ device_entities(device_id) | tojson }}",
            variables={"device_id": device_id},
        )
    except Exception:
        logger.debug("device_entities lookup failed for %s", device_id, exc_info=True)
        return None
    if not isinstance(rendered, str) or not rendered:
        return None
    try:
        parsed = json.loads(rendered)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed if isinstance(item, str) and item]


async def _fresh_area_name(ha_client: HARestClient, area_id: str) -> str | None:
    """Current name of ``area_id`` from HA, or None when it cannot be read."""
    try:
        registry = await ha_client.get_area_registry()
    except Exception:
        logger.debug("Area registry lookup failed for %s", area_id, exc_info=True)
        return None
    if not isinstance(registry, dict):
        return None
    name = registry.get(area_id)
    return str(name) if name else None


async def _resolve_area_event(
    app: FastAPI, ha_client: HARestClient, entity_index: EntityIndex, data: dict
) -> tuple[list[str], str]:
    """Entities whose cached rows an ``area_registry_updated`` event invalidates.

    HA sends area events without a change set, for cosmetic edits too (icon,
    picture, aliases, floor, labels, sensors). Only a removal or a name change
    alters the indexed ``area_name``; replay visibility keys on area ids and is
    rechecked on every replay, so other area edits keep the rows.
    """
    action = str(data.get("action") or "update")
    area_id = data.get("area_id")
    if not isinstance(area_id, str) or not area_id or action not in ("update", "remove"):
        return [], ""
    if action == "update":
        previous = (_previous_lookups(app).get("area") or {}).get(area_id)
        current = await _fresh_area_name(ha_client, area_id)
        if previous is not None and current is not None and previous == current:
            logger.debug("Area registry update for %s left its name unchanged; cache kept", area_id)
            return [], ""
        detail = f"area_registry_updated action=update area_id={area_id} name={previous!r}->{current!r}"
    else:
        detail = f"area_registry_updated action=remove area_id={area_id}"
    return sorted(_entities_in_areas(app, entity_index, {area_id})), detail


async def _resolve_device_event(
    app: FastAPI, ha_client: HARestClient, entity_index: EntityIndex, data: dict
) -> tuple[list[str], str]:
    """Entities whose cached rows a ``device_registry_updated`` event invalidates.

    Only a device update that changes ``RELEVANT_DEVICE_FIELDS`` counts, and it
    targets the device's own entities -- not every entity sharing its old area.
    Device creation needs nothing; removing or disabling a device makes HA emit
    entity-registry events for each entity, which are handled per entity.
    """
    action = str(data.get("action") or "update")
    changes = _event_changes(data)
    fields = sorted(RELEVANT_DEVICE_FIELDS.intersection(changes))
    if action != "update" or not fields:
        return [], ""
    device_id = data.get("device_id")
    detail = f"device_registry_updated action=update device_id={device_id} fields={','.join(fields)}"
    if isinstance(device_id, str) and device_id:
        device_entities = await _device_entity_ids(ha_client, device_id)
        if device_entities is not None:
            return sorted(set(device_entities)), detail
    # Fallback when the device -> entity lookup fails: over-invalidate rather
    # than keep rows whose resolution inputs may have changed.
    entity_ids: set[str] = set()
    old_names = {str(changes.get(key)) for key in ("name", "name_by_user") if isinstance(changes.get(key), str)}
    entity_ids |= _entities_with_device_names(entity_index, old_names)
    old_area = changes.get("area_id")
    if isinstance(old_area, str) and old_area:
        entity_ids |= _entities_in_areas(app, entity_index, {old_area})
    return sorted(entity_ids), f"{detail} (device lookup failed; broad match)"


async def _resolve_registry_event(
    app: FastAPI,
    ha_client: HARestClient,
    entity_index: EntityIndex,
    event: dict,
    *,
    kind: str,
) -> tuple[list[str], str]:
    """Return ``(entity_ids, detail)`` a registry event must invalidate.

    ``kind`` is the registry the event came from (``entity``, ``device`` or
    ``area``). An empty list means the event does not affect cached replay.
    """
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return [], ""
    changes = _event_changes(data)
    direct_ids = _event_entity_ids(data, changes)
    if kind == "entity" or direct_ids:
        fields = _relevant_entity_changes(data, changes)
        if not direct_ids or not fields:
            return [], ""
        action = data.get("action") or "update"
        return sorted(direct_ids), f"entity_registry_updated action={action} fields={','.join(fields)}"
    if kind == "area":
        return await _resolve_area_event(app, ha_client, entity_index, data)
    if kind == "device":
        return await _resolve_device_event(app, ha_client, entity_index, data)
    return [], ""


async def _refresh_registry_entities(
    app: FastAPI,
    ha_client: HARestClient,
    entity_index: EntityIndex,
    entity_ids: list[str],
) -> None:
    """Refresh or remove entity-index rows affected by a HA registry event."""
    if not entity_ids:
        return

    area_lookup, alias_lookup, device_lookup, area_id_lookup = await _gather_ha_lookups(
        ha_client, _previous_lookups(app)
    )
    _store_entity_lookups(app, area_lookup, alias_lookup, device_lookup, area_id_lookup)
    _set_area_assignment_status(entity_index, ha_client, area_id_lookup)
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
        # The in-memory index is empty until the background prime installs
        # the first HA snapshot (see ``entity_index_snapshot_loaded``).
        _mark_snapshot_loaded(entity_index, False)
        app.state.entity_index = entity_index
    # Device-class source for the sensitive-action approval backstop.
    register_entity_index(entity_index)

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
    # Action-cache rows rejected by the visibility recheck are kept (never
    # replayed) while the entity index has not loaded its first snapshot.
    register_probe = getattr(cache_manager, "set_entity_index_readiness_probe", None)
    if callable(register_probe):
        register_probe(lambda: entity_index_snapshot_loaded(entity_index))

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

            # Person name / linked HA user changed: drop the requester-name cache.
            if entity_id.startswith(PERSON_ENTITY_PREFIX) and person_identity_changed(old_state, new_state):
                get_person_directory().invalidate()

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

        async def _invalidate_registry_event(event: dict, *, kind: str) -> list[str]:
            resolved_entity_ids, detail = await _resolve_registry_event(app, ha_client, entity_index, event, kind=kind)
            if not resolved_entity_ids:
                return []
            event_type = event.get("event_type") or f"{kind}_registry_updated"
            try:
                counts = await cache_manager.invalidate_by_entity_id(
                    resolved_entity_ids, reason="registry_event", detail=detail
                )
                logger.info(
                    "Cache invalidation succeeded for %s: entities=%s action=%d routing=%d (%s)",
                    event_type,
                    sorted(resolved_entity_ids),
                    counts.get("action", 0),
                    counts.get("routing", 0),
                    detail,
                )
            except Exception:
                logger.warning(
                    "Cache invalidation for registry event failed: %s",
                    sorted(resolved_entity_ids),
                    exc_info=True,
                )
            return resolved_entity_ids

        # Every registry handler drops the 5-minute registry cache first:
        # area and device renames otherwise re-ingest stale area / device
        # names from the cached lookups.
        async def on_entity_registry_updated(event: dict) -> None:
            ha_client.clear_area_registry_cache()
            resolved_entity_ids = await _invalidate_registry_event(event, kind="entity")
            await _refresh_registry_entities(app, ha_client, entity_index, resolved_entity_ids)

        async def on_device_registry_updated(event: dict) -> None:
            ha_client.clear_area_registry_cache()
            resolved_entity_ids = await _invalidate_registry_event(event, kind="device")
            await _refresh_registry_entities(app, ha_client, entity_index, resolved_entity_ids)

        async def on_area_registry_updated(event: dict) -> None:
            ha_client.clear_area_registry_cache()
            resolved_entity_ids = await _invalidate_registry_event(event, kind="area")
            await _refresh_registry_entities(app, ha_client, entity_index, resolved_entity_ids)

        # Registry refresh work issues send_command round-trips whose
        # replies only the WS receive loop can deliver, so listeners must
        # offload it to a background task instead of awaiting it inline.
        # One lock serializes refreshes across overlapping registry events.
        registry_refresh_lock = asyncio.Lock()

        def _schedule_registry_refresh(handler):
            async def _run(event: dict) -> None:
                async with registry_refresh_lock:
                    await handler(event)

            def _listener(event: dict) -> asyncio.Task:
                return spawn(_run(event), name="ha-registry-refresh")

            return _listener

        ws_client.on_event("state_changed", on_state_changed)
        ws_client.on_event("entity_registry_updated", _schedule_registry_refresh(on_entity_registry_updated))
        ws_client.on_event("device_registry_updated", _schedule_registry_refresh(on_device_registry_updated))
        ws_client.on_event("area_registry_updated", _schedule_registry_refresh(on_area_registry_updated))

        # Events emitted while the WS was down (deletes, renames, hidden
        # flags) are lost: resync the whole index after each reconnect.
        # The WS client runs this as a background task; overlapping
        # reconnects share one in-flight resync.
        reconnect_resync_task: asyncio.Task | None = None

        async def _resync_after_reconnect() -> None:
            ha_client.clear_area_registry_cache()
            get_person_directory().invalidate()
            async with registry_refresh_lock:
                await resync_entity_index(app, reason="ws_reconnect")

        def on_ws_reconnected() -> None:
            nonlocal reconnect_resync_task
            if reconnect_resync_task is not None and not reconnect_resync_task.done():
                return
            reconnect_resync_task = spawn(_resync_after_reconnect(), name="ha-ws-reconnect-resync")

        ws_client.on_reconnect(on_ws_reconnected)

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
                if hidden_ids is None:
                    logger.info("Deferred hidden-entity sync: registry fetch failed, keeping existing index")
                    return
                logger.info("Deferred hidden-entity sync: hidden_ids=%d", len(hidden_ids))
                if hidden_ids:
                    app.state.hidden_entity_ids = hidden_ids
                    snapshot_generation = _snapshot_generation(entity_index)
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
                    result = await entity_index.sync_async(entities, snapshot_generation=snapshot_generation)
                    if entities:
                        _mark_snapshot_loaded(entity_index, True)
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
