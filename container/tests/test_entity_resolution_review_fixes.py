"""Regression tests for the entity-resolution / HA-client review findings (#132, theme T6).

Each test fails on the code before the fix. Resolution tests use a real
``EntityIndex`` and a real ``EntityMatcher`` where the finding was hidden
by matchers mocked to return ``[]``.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from app.entity.aliases import AliasResolver
from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.index import EntityIndex
from app.entity.matcher import EntityMatcher, MatchResult
from app.entity.visibility import _VisibilityRules, entity_is_visible
from app.models.entity_index import EntityIndexEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeVectorStore:
    """Dict-backed stand-in for the sqlite-vec VectorStore (entity collection only)."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, dict]] = {}

    def upsert(self, _collection, ids, documents, metadatas):
        for eid, doc, meta in zip(ids, documents, metadatas, strict=True):
            self.rows[eid] = (doc, dict(meta))

    def update_metadata(self, _collection, ids, metadatas):
        for eid, meta in zip(ids, metadatas, strict=True):
            doc = self.rows.get(eid, ("", {}))[0]
            self.rows[eid] = (doc, dict(meta))

    def get(self, _collection, ids=None, include=None):
        selected = [eid for eid in (ids if ids is not None else list(self.rows)) if eid in self.rows]
        return {
            "ids": selected,
            "documents": [self.rows[eid][0] for eid in selected],
            "metadatas": [self.rows[eid][1] for eid in selected],
        }

    def delete(self, _collection, ids):
        for eid in ids:
            self.rows.pop(eid, None)

    def count(self, _collection):
        return len(self.rows)


def _entry(entity_id: str, friendly_name: str, *, area: str | None = None, area_name: str | None = None):
    return EntityIndexEntry(
        entity_id=entity_id,
        friendly_name=friendly_name,
        domain=entity_id.split(".", 1)[0],
        area=area,
        area_name=area_name,
    )


def _index(entries: list[EntityIndexEntry]) -> EntityIndex:
    index = EntityIndex(_FakeVectorStore())
    index.populate(entries)
    return index


def _alias_resolver(aliases: dict[str, str] | None = None) -> AliasResolver:
    resolver = AliasResolver()
    resolver._cache = {k.lower(): v for k, v in (aliases or {}).items()}
    return resolver


def _matcher(index: EntityIndex, aliases: dict[str, str] | None = None, *, threshold: float = 0.60) -> EntityMatcher:
    matcher = EntityMatcher(index, _alias_resolver(aliases))
    matcher._weights = {"levenshtein": 0.25, "jaro_winkler": 0.25, "phonetic": 0.25, "alias": 0.25}
    matcher._confidence_threshold = threshold
    matcher._top_n = 3
    matcher._log_misses = False
    return matcher


# ---------------------------------------------------------------------------
# Item 1: ambiguity is never overridden by the hybrid matcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_friendly_name_asks_instead_of_hybrid_pick():
    index = _index(
        [
            _entry("light.deckenlampe_kueche", "Deckenlampe", area="kuche"),
            _entry("light.deckenlampe_bad", "Deckenlampe", area="bad"),
            _entry("switch.garage", "Garage"),
            _entry("sensor.outdoor", "Outdoor Temperature"),
            _entry("media_player.tv", "Fernseher"),
        ]
    )
    matcher = _matcher(index)
    # The hybrid matcher alone would happily return a pick for this query.
    assert len(await matcher.match("Deckenlampe")) == 2

    result = await resolve_entity_deterministic_first(
        "Deckenlampe", index, matcher, None, allowed_domains=frozenset({"light"})
    )

    assert result["entity_id"] is None
    assert result["metadata"]["resolution_path"] == "exact_friendly_name_ambiguous"
    assert "Multiple entities match" in (result["speech"] or "")


@pytest.mark.asyncio
async def test_hybrid_near_tie_asks_for_clarification():
    index = _index(
        [
            _entry("light.deckenlampe_links", "Deckenlampe Links"),
            _entry("light.deckenlampe_rechts", "Deckenlampe Rechts"),
            _entry("sensor.outdoor", "Outdoor Temperature"),
            _entry("switch.garage", "Garage"),
            _entry("media_player.tv", "Fernseher"),
        ]
    )
    matcher = _matcher(index, threshold=0.40)
    matches = await matcher.match("Deckenlampe Mitte")
    assert len(matches) >= 2
    assert abs(matches[0].score - matches[1].score) < 0.02

    result = await resolve_entity_deterministic_first(
        "Deckenlampe Mitte", index, matcher, None, allowed_domains=frozenset({"light"})
    )

    assert result["entity_id"] is None
    assert result["metadata"]["resolution_path"] == "hybrid_matcher_ambiguous"
    assert len(result["metadata"]["candidate_entities"]) >= 2


@pytest.mark.asyncio
async def test_hybrid_clear_winner_is_still_selected():
    index = _index([_entry("light.kitchen", "Kitchen Light"), _entry("light.bedroom", "Bedroom Light")])
    matcher = MagicMock()
    matcher.match = AsyncMock(
        return_value=[
            MatchResult(entity_id="light.kitchen", friendly_name="Kitchen Light", score=0.90),
            MatchResult(entity_id="light.bedroom", friendly_name="Bedroom Light", score=0.70),
        ]
    )

    result = await resolve_entity_deterministic_first(
        "kitchn lite", index, matcher, None, allowed_domains=frozenset({"light"})
    )

    assert result["entity_id"] == "light.kitchen"
    assert result["metadata"]["resolution_path"] == "hybrid_matcher"


# ---------------------------------------------------------------------------
# Item 8: area rerank reads the area from the index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_area_rerank_uses_index_area_for_match_results():
    index = _index(
        [
            _entry("light.spot_kitchen", "Spot", area="kitchen"),
            _entry("light.spot_bath", "Spot", area="bath"),
        ]
    )
    matcher = MagicMock()
    matcher.match = AsyncMock(
        return_value=[
            MatchResult(entity_id="light.spot_kitchen", friendly_name="Spot", score=0.80),
            MatchResult(entity_id="light.spot_bath", friendly_name="Spot", score=0.77),
        ]
    )

    result = await resolve_entity_deterministic_first(
        "spott",
        index,
        matcher,
        None,
        allowed_domains=frozenset({"light"}),
        preferred_area_id="bath",
    )

    assert result["entity_id"] == "light.spot_bath"
    assert result["metadata"]["area_rerank_reason"] == "preferred_area_match"


# ---------------------------------------------------------------------------
# Item 3: user / DB aliases resolve deterministically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_alias_resolves_as_exact_alias():
    index = _index([_entry("light.kitchen_ceiling", "Kitchen Ceiling"), _entry("light.hall", "Hall")])
    matcher = _matcher(index, aliases={"Zauberlicht": "light.kitchen_ceiling"})

    result = await resolve_entity_deterministic_first(
        "zauberlicht", index, matcher, None, allowed_domains=frozenset({"light"})
    )

    assert result["entity_id"] == "light.kitchen_ceiling"
    assert result["metadata"]["resolution_path"] == "exact_alias"
    assert result["friendly_name"] == "Kitchen Ceiling"


@pytest.mark.asyncio
async def test_user_alias_respects_allowed_domains_and_visibility():
    index = _index([_entry("switch.kitchen_plug", "Kitchen Plug"), _entry("light.hall", "Hall")])
    matcher = _matcher(index, aliases={"Zauberlicht": "switch.kitchen_plug"})

    out_of_domain = await resolve_entity_deterministic_first(
        "zauberlicht", index, matcher, None, allowed_domains=frozenset({"light"})
    )
    assert out_of_domain["entity_id"] is None

    rules = _VisibilityRules(domain_exclude={"switch"})
    with patch("app.entity.visibility._get_cached_rules", new=AsyncMock(return_value=rules)):
        hidden = await resolve_entity_deterministic_first("zauberlicht", index, matcher, "light-agent")
    assert hidden["entity_id"] is None


@pytest.mark.asyncio
async def test_matcher_alias_hit_gets_friendly_name_and_passes_threshold():
    index = _index([_entry("light.kitchen_ceiling", "Kitchen Ceiling"), _entry("light.hall", "Hall")])
    matcher = _matcher(index, aliases={"Zauberlicht": "light.kitchen_ceiling"})

    matches = await matcher.match("Zauberlicht")

    assert matches
    assert matches[0].entity_id == "light.kitchen_ceiling"
    assert matches[0].friendly_name == "Kitchen Ceiling"
    assert matches[0].score >= 0.60


@pytest.mark.asyncio
async def test_prime_reloads_alias_resolver_after_user_aliases():
    from app.bootstrap import _entity as entity_bootstrap

    resolver = AliasResolver()
    resolver.reload = AsyncMock()
    app = SimpleNamespace(state=SimpleNamespace(alias_resolver=resolver))
    ha = MagicMock()
    ha.get_states = AsyncMock(return_value=[])
    ha.get_hidden_entity_ids = AsyncMock(return_value=set())
    ei = MagicMock()
    ei.mutation_generation = MagicMock(return_value=0)
    ei.populate_async = AsyncMock()
    vs = MagicMock()
    vs.count = MagicMock(return_value=0)

    with (
        patch("app.bootstrap._entity._gather_ha_lookups", new=AsyncMock(return_value=({}, {}, {}, {}))),
        patch("app.bootstrap._entity.SettingsRepository.get_value", new=AsyncMock(return_value="")),
        patch("app.bootstrap._entity.SettingsRepository.set", new=AsyncMock()),
        patch("app.entity.user_aliases.load_user_aliases", new=AsyncMock(return_value=2)),
    ):
        await entity_bootstrap._prime_entity_index(app, ha, ei, vs)

    resolver.reload.assert_awaited_once()


# ---------------------------------------------------------------------------
# Item 12: shared folding, area_name fallback, allowed_domains on entity_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "friendly_name"),
    [("Kueche", "Küche"), ("Strasse Licht", "Straße Licht"), ("Buero", "Büro")],
)
async def test_exact_stage_folds_umlaut_digraphs_and_sharp_s(query, friendly_name):
    index = _index([_entry("light.target", friendly_name), _entry("light.other", "Garage")])

    result = await resolve_entity_deterministic_first(query, index, None, None, allowed_domains=frozenset({"light"}))

    assert result["entity_id"] == "light.target"
    assert result["metadata"]["resolution_path"] == "exact_friendly_name"


@pytest.mark.asyncio
async def test_area_fallback_matches_area_name():
    index = _index(
        [
            _entry("light.ceiling", "Deckenlicht", area="living_room", area_name="Wohnzimmer"),
            _entry("light.garage", "Garage", area="garage", area_name="Garage"),
        ]
    )

    result = await resolve_entity_deterministic_first(
        "Wohnzimmer",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
        enable_area_fallback=True,
    )

    assert result["entity_id"] == "light.ceiling"
    assert result["metadata"]["resolution_path"] == "exact_area"


@pytest.mark.asyncio
async def test_exact_entity_id_stage_honors_allowed_domains():
    index = _index([_entry("switch.kitchen", "Kitchen Switch")])

    result = await resolve_entity_deterministic_first(
        "switch.kitchen", index, None, None, allowed_domains=frozenset({"light"})
    )

    assert result["entity_id"] is None


# ---------------------------------------------------------------------------
# Item 10: sync never undoes newer incremental updates
# ---------------------------------------------------------------------------


def test_sync_keeps_updates_and_removals_newer_than_snapshot():
    index = _index(
        [
            _entry("light.a", "Alpha"),
            _entry("light.b", "Bravo"),
            _entry("light.c", "Charlie"),
        ]
    )
    generation = index.mutation_generation()
    stale_snapshot = [_entry("light.a", "Alpha"), _entry("light.b", "Bravo"), _entry("light.c", "Charlie")]

    # WebSocket events applied while the snapshot was in flight.
    index.add(_entry("light.a", "Renamed"))
    index.remove("light.c")
    index.batch_add([_entry("light.d", "Delta")])

    index.sync(stale_snapshot, snapshot_generation=generation)

    assert index.get_by_id("light.a").friendly_name == "Renamed"
    assert index.get_by_id("light.c") is None
    assert index.get_by_id("light.d") is not None
    assert index.get_by_id("light.b") is not None
    assert [e.entity_id for e in index.find_by_tokens({"renamed"}, max_df_ratio=1.0)] == ["light.a"]
    assert index.find_by_tokens({"charlie"}, max_df_ratio=1.0) == []
    assert index.find_by_tokens({"alpha"}, max_df_ratio=1.0) == []


def test_incremental_update_only_touches_own_postings():
    index = _index([_entry("light.a", "Alpha Lamp"), _entry("light.b", "Bravo Lamp")])
    bravo_postings = index._token_postings["bravo"]

    index.add(_entry("light.a", "Gamma Lamp"))

    assert "alpha" not in index._token_postings
    assert index._token_postings["gamma"] == {"light.a"}
    assert index._token_postings["lamp"] == {"light.a", "light.b"}
    assert index._token_postings["bravo"] is bravo_postings
    assert index._entity_tokens["light.a"] >= {"gamma", "lamp"}


# ---------------------------------------------------------------------------
# Item 2: failed registry lookups are not cached / published; fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_area_exclude_fails_closed_while_area_assignments_unknown():
    index = _index([_entry("light.unassigned", "Lamp", area=None)])
    rules = _VisibilityRules(area_exclude={"bedroom"})

    with patch("app.entity.visibility._get_cached_rules", new=AsyncMock(return_value=rules)):
        assert await entity_is_visible("light-agent", "light.unassigned", index) is True
        index.area_assignments_known = False
        assert await entity_is_visible("light-agent", "light.unassigned", index) is False


@pytest.mark.asyncio
async def test_gather_lookups_keeps_previous_lookup_when_fetch_raises():
    from app.bootstrap._entity import _gather_ha_lookups

    ha = MagicMock()
    ha.get_area_registry = AsyncMock(return_value={"kitchen": "Kitchen"})
    ha.get_entity_aliases = AsyncMock(return_value={})
    ha.get_device_names = AsyncMock(return_value={})
    ha.get_entity_areas = AsyncMock(side_effect=RuntimeError("template failed"))

    _, _, _, area_ids = await _gather_ha_lookups(ha, {"area_id": {"light.a": "kitchen"}})

    assert area_ids == {"light.a": "kitchen"}


@pytest.mark.asyncio
async def test_area_status_unknown_when_area_lookup_never_succeeded():
    from app.bootstrap._entity import _set_area_assignment_status
    from app.ha_client.rest import HARestClient

    client = HARestClient()
    client.render_template = AsyncMock(return_value=None)
    index = _index([])

    lookup = await client.get_entity_areas()
    _set_area_assignment_status(index, client, lookup)
    assert lookup == {}
    assert index.area_assignments_known is False

    client.render_template = AsyncMock(return_value='[{"id": "light.a", "area": "kitchen"}]')
    lookup = await client.get_entity_areas()
    _set_area_assignment_status(index, client, lookup)
    assert index.area_assignments_known is True


@pytest.mark.asyncio
async def test_area_registry_template_escapes_names_with_tojson():
    from app.ha_client.rest import HARestClient

    client = HARestClient()
    client.render_template = AsyncMock(return_value='[{"id": "kids", "name": "Kid\\"s Room"}]')

    result = await client.get_area_registry()

    template = client.render_template.call_args.args[0]
    assert "tojson" in template
    assert '"{{' not in template
    assert result == {"kids": 'Kid"s Room'}


# ---------------------------------------------------------------------------
# Items 4 and 6: registry cache clearing and reconnect resync wiring
# ---------------------------------------------------------------------------


class _FakeWsClient:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.reconnect_callbacks: list = []

    def on_event(self, event_type, callback):
        self.handlers[event_type] = callback

    def on_reconnect(self, callback):
        self.reconnect_callbacks.append(callback)

    async def run(self):
        return None

    def is_connected(self):
        return False


async def _setup_observers():
    from app.bootstrap import _entity as entity_bootstrap

    ws = _FakeWsClient()
    ha = MagicMock()
    ha.clear_area_registry_cache = MagicMock()
    ha.set_state_observer = MagicMock()
    ha.get_hidden_entity_ids = AsyncMock(return_value=set())
    entity_index = MagicMock()
    entity_index.list_entries.return_value = [SimpleNamespace(entity_id="light.a", area="kitchen", device_name="Lamp")]
    cache_manager = MagicMock()
    cache_manager.invalidate_by_entity_id = AsyncMock(return_value={})
    app = SimpleNamespace(state=SimpleNamespace(ws_client=None, sync_task=MagicMock(done=lambda: False)))

    def _close(_app, coro, _name):
        with contextlib.suppress(Exception):
            coro.close()

    with (
        patch("app.ha_client.websocket.HAWebSocketClient", return_value=ws),
        patch.object(entity_bootstrap, "spawn_background", side_effect=_close),
    ):
        await entity_bootstrap.setup_entity_observers(app, "test", ha, entity_index, cache_manager)
    return ws, ha, entity_index


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["area_registry_updated", "device_registry_updated"])
async def test_area_and_device_registry_events_clear_registry_cache(event_type):
    from app.bootstrap import _entity as entity_bootstrap

    ws, ha, _ = await _setup_observers()
    with patch.object(entity_bootstrap, "_refresh_registry_entities", new=AsyncMock()):
        task = ws.handlers[event_type]({"event_type": event_type, "data": {"area_id": "kitchen"}})
        await task

    ha.clear_area_registry_cache.assert_called_once()


@pytest.mark.asyncio
async def test_ws_reconnect_triggers_entity_resync():
    from app.bootstrap import _entity as entity_bootstrap

    ws, ha, _ = await _setup_observers()
    assert len(ws.reconnect_callbacks) == 1
    resync = AsyncMock(return_value={"added": 0, "updated": 0, "removed": 0, "unchanged": 0})
    with patch.object(entity_bootstrap, "resync_entity_index", new=resync):
        ws.reconnect_callbacks[0]()
        ws.reconnect_callbacks[0]()  # overlapping reconnect shares the in-flight resync
        for _ in range(5):
            await asyncio.sleep(0)

    resync.assert_awaited_once()
    assert resync.call_args.kwargs == {"reason": "ws_reconnect"}
    ha.clear_area_registry_cache.assert_called()


@pytest.mark.asyncio
async def test_ws_client_notifies_reconnect_listeners_after_reconnect():
    from app.ha_client.websocket import HAWebSocketClient

    client = HAWebSocketClient()
    called = []
    client.on_reconnect(lambda: called.append("reconnected"))
    client._safe_connect = AsyncMock(return_value=True)
    client._close_session = AsyncMock()

    receive_calls = 0

    async def _receive_loop():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls >= 2:
            client._running = False

    client._receive_loop = _receive_loop
    client._reconnect_loop = AsyncMock(return_value=True)

    await client.run()

    assert called == ["reconnected"]


# ---------------------------------------------------------------------------
# Item 7: no receive-level idle timeout on quiet instances
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_loop_has_no_idle_timeout():
    from app.ha_client import websocket as ws_module

    client = ws_module.HAWebSocketClient()
    client._running = True
    seen = []
    client.on_event("state_changed", lambda event: seen.append(event))

    text = MagicMock()
    text.type = aiohttp.WSMsgType.TEXT
    text.data = '{"type": "event", "event": {"event_type": "state_changed", "data": {}}}'
    closed = MagicMock()
    closed.type = aiohttp.WSMsgType.CLOSED
    ws_mock = MagicMock()
    ws_mock.closed = False
    ws_mock.receive = AsyncMock(side_effect=[text, closed])
    client._ws = ws_mock

    # A quiet period must not end the loop: simulate the old idle timeout firing.
    with patch.object(ws_module.asyncio, "wait_for", new=AsyncMock(side_effect=TimeoutError)):
        await client._receive_loop()

    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Item 9: LLM expansion failures are not cached
# ---------------------------------------------------------------------------


def _cache_repo():
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.touch = AsyncMock()
    repo.put = AsyncMock()
    repo.purge_expired = AsyncMock()
    repo.evict_lru = AsyncMock()
    return repo


@pytest.mark.asyncio
async def test_expansion_failure_is_not_cached():
    from app.entity.expansion import QueryExpansionService

    repo = _cache_repo()
    service = QueryExpansionService(
        cache_repo=repo,
        llm_call=AsyncMock(side_effect=RuntimeError("llm down")),
        prompt_template="{token}",
    )
    with patch("app.entity.expansion.SettingsRepository.get_value", new=AsyncMock(return_value="true")):
        assert await service.expand("kueche", source_language="de", index_language="en") == []
    repo.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_expansion_valid_empty_answer_is_cached():
    from app.entity.expansion import QueryExpansionService

    repo = _cache_repo()
    service = QueryExpansionService(
        cache_repo=repo,
        llm_call=AsyncMock(return_value='{"expansions": []}'),
        prompt_template="{token}",
    )
    with patch("app.entity.expansion.SettingsRepository.get_value", new=AsyncMock(return_value="true")):
        assert await service.expand("kueche", source_language="de", index_language="en") == []
    repo.put.assert_awaited_once_with("kueche", "de", [])


# ---------------------------------------------------------------------------
# Item 11: home timezone retry and override precedence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_context_retries_soon_after_ha_failure():
    from app.ha_client.home_context import HomeContextProvider

    ha = MagicMock()
    ha.get_config = AsyncMock(side_effect=[{}, {"time_zone": "Europe/Berlin", "location_name": "Home"}])
    provider = HomeContextProvider()
    provider._failure_retry_seconds = 0

    with patch.object(HomeContextProvider, "_load_overrides", new=AsyncMock(return_value=None)):
        first = await provider.get(ha)
        second = await provider.get(ha)

    assert first.timezone == "UTC"
    assert second.timezone == "Europe/Berlin"


@pytest.mark.asyncio
async def test_home_context_db_override_wins_over_ha():
    from app.ha_client.home_context import HomeContext, HomeContextProvider

    ha = MagicMock()
    ha.get_config = AsyncMock(return_value={"time_zone": "Europe/Berlin", "location_name": "Home"})
    provider = HomeContextProvider()
    overrides = HomeContext(timezone="Europe/London", location_name="")

    with patch.object(HomeContextProvider, "_load_overrides", new=AsyncMock(return_value=overrides)):
        ctx = await provider.refresh(ha)

    assert ctx.timezone == "Europe/London"
    assert ctx.location_name == "Home"


# ---------------------------------------------------------------------------
# Item 13: history speech carries the unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_speech_uses_unit_from_current_state():
    from app.ha_client.history_query import execute_recorder_history_query

    ha = MagicMock()
    ha.get_history_period = AsyncMock(
        return_value=[
            [
                {"state": "20.5", "last_changed": "2026-10-09T10:00:00+00:00"},
                {"state": "21.5", "last_changed": "2026-10-09T11:00:00+00:00"},
            ]
        ]
    )
    ha.get_state = AsyncMock(return_value={"state": "21.5", "attributes": {"unit_of_measurement": "°C"}})

    result = await execute_recorder_history_query(
        "sensor.living_temperature",
        "Living Temperature",
        {"period": "last_24_hours"},
        ha,
        allowed_domains=frozenset({"sensor"}),
    )

    assert result["success"] is True
    assert "20.50 °C" in result["speech"]
    assert "21.50 °C" in result["speech"]
