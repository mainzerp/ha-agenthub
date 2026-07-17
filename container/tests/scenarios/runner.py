"""Build a real-pipeline test harness and execute scenarios.

The runner wires:
- real ``OrchestratorAgent`` (cache disabled),
- real ``Dispatcher`` + ``InProcessTransport`` + ``AgentRegistry``,
- real domain agents (LightAgent, ClimateAgent, ...) registered with the
  registry,
- real ``EntityIndex`` over an ephemeral in-memory ``VectorStore``,
- real ``EntityMatcher``,
- ``RecordingHaClient`` in place of ``HARestClient``,
- a deterministic LLM stub patched into ``app.llm.client.complete``,
- a temp SQLite DB seeded with defaults,
- a fresh ``HomeContextProvider`` (singleton state reset each scenario).
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite

from tests.helpers import shutdown_aiosqlite

from .deterministic_llm import DeterministicLlmStub
from .embedding_stub import deterministic_embedding
from .loader import load_snapshot
from .recording_ha_client import RecordingHaClient
from .types import Expected, Scenario

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stub vector store: keeps the EntityIndex contract without ChromaDB.
# ---------------------------------------------------------------------------


class _StubCollection:
    def __init__(self) -> None:
        self.docs: dict[str, str] = {}
        self.metas: dict[str, dict] = {}
        self.embeds: dict[str, list[float]] = {}


class StubVectorStore:
    """Minimal in-memory VectorStore replacement used by the test runner.

    Implements only the methods ``EntityIndex`` calls
    (``upsert``, ``query``, ``get_collection``).
    """

    def __init__(self) -> None:
        self._collections: dict[str, _StubCollection] = {}

    def _coll(self, name: str) -> _StubCollection:
        if name not in self._collections:
            self._collections[name] = _StubCollection()
        return self._collections[name]

    def get_collection(self, name: str):
        return self._coll(name)

    def upsert(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        coll = self._coll(collection_name)
        for i, eid in enumerate(ids):
            doc = documents[i] if documents else ""
            coll.docs[eid] = doc
            coll.metas[eid] = (metadatas[i] if metadatas else {}) or {}
            coll.embeds[eid] = embeddings[i] if embeddings else deterministic_embedding(doc)

    def add(self, *args, **kwargs) -> None:  # pragma: no cover - parity stub
        return self.upsert(*args, **kwargs)

    def get(
        self,
        collection_name: str,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
    ) -> dict:
        coll = self._coll(collection_name)
        target_ids = list(coll.docs.keys()) if ids is None else [eid for eid in ids if eid in coll.docs]
        if where:
            target_ids = [eid for eid in target_ids if _matches_where(coll.metas.get(eid, {}), where)]
        return {
            "ids": target_ids,
            "metadatas": [coll.metas.get(eid, {}) for eid in target_ids],
            "documents": [coll.docs.get(eid, "") for eid in target_ids],
        }

    def delete(self, collection_name: str, ids: list[str] | None = None) -> None:
        coll = self._coll(collection_name)
        for eid in ids or []:
            coll.docs.pop(eid, None)
            coll.metas.pop(eid, None)
            coll.embeds.pop(eid, None)

    def count(self, collection_name: str) -> int:
        return len(self._coll(collection_name).docs)

    def query(
        self,
        collection_name: str,
        query_texts: list[str] | None = None,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 5,
        include: list[str] | None = None,
        where: dict | None = None,
    ) -> dict:
        coll = self._coll(collection_name)
        if not coll.docs:
            return {"ids": [[]], "metadatas": [[]], "distances": [[]], "documents": [[]]}
        # Token-overlap distance: deterministic and rank-meaningful for
        # the small fixture corpus. Pure hash-derived embeddings would
        # be near-orthogonal and would rank entities randomly.
        qtext = (query_texts or [""])[0]
        qtokens = _tokenize(qtext)
        ranked = []
        for eid, doc in coll.docs.items():
            dtokens = _tokenize(doc)
            if not qtokens or not dtokens:
                sim = 0.0
            else:
                inter = len(qtokens & dtokens)
                union = len(qtokens | dtokens)
                sim = inter / union if union else 0.0
            distance = 1.0 - sim
            ranked.append((distance, eid))
        ranked.sort(key=lambda x: x[0])
        ranked = ranked[:n_results]
        ids = [eid for _, eid in ranked]
        metas = [coll.metas.get(eid, {}) for eid in ids]
        docs = [coll.docs.get(eid, "") for eid in ids]
        dists = [d for d, _ in ranked]
        return {
            "ids": [ids],
            "metadatas": [metas],
            "distances": [dists],
            "documents": [docs],
        }


def _tokenize(text: str) -> set[str]:
    import re
    import unicodedata

    if not text:
        return set()
    norm = unicodedata.normalize("NFKD", text.lower())
    norm = "".join(c for c in norm if unicodedata.category(c) != "Mn")
    norm = norm.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return {t for t in re.findall(r"[a-z0-9]+", norm) if len(t) >= 2}


def _matches_where(meta: dict, where: dict) -> bool:
    """Tiny subset of ChromaDB's where-clause filtering."""
    for key, val in where.items():
        if isinstance(val, dict):
            if "$in" in val and meta.get(key) not in set(val["$in"]):
                return False
            if "$eq" in val and meta.get(key) != val["$eq"]:
                return False
        else:
            if meta.get(key) != val:
                return False
    return True


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------


@dataclass
class PipelineHandles:
    orchestrator: Any
    registry: Any
    dispatcher: Any
    ha_client: RecordingHaClient
    llm: DeterministicLlmStub
    entity_index: Any
    entity_matcher: Any
    db_path: Any


_AGENT_FACTORIES = {}


def _build_agent_classes():
    """Lazy import of agent classes to avoid heavy imports at module load."""
    from app.agents.actionable import (
        AutomationAgent,
        ClimateAgent,
        LightAgent,
        MediaAgent,
        MusicAgent,
        SceneAgent,
        SecurityAgent,
    )
    from app.agents.calendar import CalendarAgent
    from app.agents.general import GeneralAgent
    from app.agents.lists import ListsAgent
    from app.agents.send import SendAgent
    from app.agents.timer import TimerAgent

    return {
        "light-agent": LightAgent,
        "climate-agent": ClimateAgent,
        "media-agent": MediaAgent,
        "music-agent": MusicAgent,
        "scene-agent": SceneAgent,
        "security-agent": SecurityAgent,
        "automation-agent": AutomationAgent,
        "timer-agent": TimerAgent,
        "general-agent": GeneralAgent,
        "send-agent": SendAgent,
        "calendar-agent": CalendarAgent,
        "lists-agent": ListsAgent,
    }


async def build_pipeline(scenario: Scenario, db_path) -> PipelineHandles:
    from app.a2a.dispatcher import Dispatcher
    from app.a2a.registry import AgentRegistry
    from app.a2a.transport import InProcessTransport
    from app.agents.orchestrator import OrchestratorAgent
    from app.entity.aliases import AliasResolver
    from app.entity.index import EntityIndex
    from app.entity.ingest import parse_ha_states
    from app.entity.matcher import EntityMatcher
    from app.ha_client.home_context import HomeContext, home_context_provider

    snapshot = load_snapshot(scenario.snapshot)
    states = snapshot["states"]
    config = snapshot["config"]

    # HA client
    ha_client = RecordingHaClient(states=states, config=config)
    if scenario.preconditions.entity_overrides:
        ha_client.apply_overrides(scenario.preconditions.entity_overrides)

    # Reset HomeContextProvider singleton; pre-seed with config so no DB hit
    # is required for timezone / location.
    home_context_provider._context = HomeContext(
        timezone=config.get("time_zone", "UTC") or "UTC",
        location_name=config.get("location_name", "") or "",
    )
    import time as _time

    home_context_provider._last_fetched = _time.monotonic()

    # Vector store + entity index + matcher
    vector_store = StubVectorStore()
    entity_index = EntityIndex(vector_store)
    entries = parse_ha_states(await ha_client.get_states())
    entity_index.populate(entries)
    alias_resolver = AliasResolver()
    matcher = EntityMatcher(entity_index, alias_resolver)
    # load_config touches the DB; we provide simple defaults manually.
    matcher._weights = {
        "levenshtein": 0.20,
        "jaro_winkler": 0.20,
        "phonetic": 0.15,
        "embedding": 0.30,
        "alias": 0.15,
    }
    matcher._confidence_threshold = 0.45
    matcher._top_n = 3

    # Registry + agents
    registry = AgentRegistry()
    agent_classes = _build_agent_classes()
    for agent_id, cls in agent_classes.items():
        try:
            if agent_id == "general-agent":
                # GeneralAgent has a different constructor; build with mcp tools=[]
                from unittest.mock import MagicMock

                mcp = MagicMock()
                mcp.get_tools_for_agent = AsyncMock(return_value=[])
                inst = cls(ha_client=ha_client, entity_index=entity_index, mcp_tool_manager=mcp)
            elif agent_id == "send-agent":
                inst = cls(ha_client=ha_client)
            else:
                inst = cls(ha_client=ha_client, entity_index=entity_index, entity_matcher=matcher)
        except TypeError:
            # Fallback to minimal kwargs
            inst = cls(ha_client=ha_client, entity_index=entity_index)
        await registry.register(inst)

    transport = InProcessTransport(registry)
    dispatcher = Dispatcher(registry, transport)

    orchestrator = OrchestratorAgent(
        dispatcher=dispatcher,
        registry=registry,
        cache_manager=None,
        ha_client=ha_client,
        entity_index=entity_index,
    )

    return PipelineHandles(
        orchestrator=orchestrator,
        registry=registry,
        dispatcher=dispatcher,
        ha_client=ha_client,
        llm=DeterministicLlmStub(),
        entity_index=entity_index,
        entity_matcher=matcher,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _temp_db(db_path):
    """Initialise a fresh SQLite DB and patch the repository getters."""
    from app.db.schema import _create_indexes, _create_tables, _seed_defaults

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await _create_tables(db)
    await _create_indexes(db)
    await _seed_defaults(db)
    await db.commit()
    await shutdown_aiosqlite(db)

    @asynccontextmanager
    async def _get_db():
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()
        finally:
            await shutdown_aiosqlite(conn)

    _repo_modules = [
        "app.db.repository",
        "app.db.schema",
        "app.db.repositories.admin",
        "app.db.repositories.agent_config",
        "app.db.repositories.alias",
        "app.db.repositories.analytics",
        "app.db.repositories.calendar",
        "app.db.repositories.conversation",
        "app.db.repositories.custom_agent",
        "app.db.repositories.entity_matching_config",
        "app.db.repositories.entity_visibility",
        "app.db.repositories.mcp",
        "app.db.repositories.plugin",
        "app.db.repositories.query_synonym_cache",
        "app.db.repositories.scheduled_timers",
        "app.db.repositories.secrets",
        "app.db.repositories.send_device_mapping",
        "app.db.repositories.settings",
        "app.db.repositories.trace",
    ]
    with contextlib.ExitStack() as stack:
        for mod in _repo_modules:
            stack.enter_context(patch(f"{mod}.get_db_read", _get_db))
            stack.enter_context(patch(f"{mod}.get_db_write", _get_db))
        yield


async def _build_task(scenario: Scenario, text: str, conversation_id: str | None):
    from app.models.agent import IngressTask, TaskContext

    ctx = scenario.context
    context = TaskContext(
        source=ctx.source if ctx.source in ("ha", "chat", "api") else "ha",
        area_id=ctx.area_id,
        area_name=ctx.area_name,
        device_id=ctx.device_id,
        device_name=ctx.device_name,
        language=scenario.language if scenario.language != "auto" else "en",
    )
    return IngressTask(
        description=text,
        conversation_id=conversation_id,
        context=context,
    )


async def run_scenario(scenario: Scenario, db_path) -> None:
    """Build the pipeline and run the scenario; raise on assertion failure."""
    from app.db.repository import SettingsRepository

    await SettingsRepository._cache_invalidate()
    async with _temp_db(db_path):
        # Apply settings overrides
        if scenario.preconditions.settings:
            from app.db.schema import get_db_write

            async with get_db_write() as db:
                for k, v in scenario.preconditions.settings.items():
                    await db.execute(
                        "INSERT OR REPLACE INTO settings (key, value, value_type, category, description) "
                        "VALUES (?, ?, 'string', 'test', '')",
                        (k, v),
                    )
                await db.commit()

        if scenario.preconditions.send_device_mappings:
            from app.db.schema import get_db_write

            async with get_db_write() as db:
                for m in scenario.preconditions.send_device_mappings:
                    await db.execute(
                        "INSERT INTO send_device_mappings (display_name, device_type, ha_service_target, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            m.get("display_name") or m.get("alias") or "device",
                            m.get("device_type", "notify"),
                            m.get("ha_service_target") or m.get("entity_id") or "",
                            "2026-04-22T00:00:00+00:00",
                        ),
                    )
                await db.commit()

        handles = await build_pipeline(scenario, db_path)
        handles.llm.feed_from_scenario(scenario)

        # Patch app.llm.client.complete to route through the stub.
        async def _complete_router(agent_id, messages, **kwargs):
            return await handles.llm.complete(agent_id, messages, **kwargs)

        with (
            patch("app.llm.client.complete", new=_complete_router),
            patch("app.agents.base.complete", new=_complete_router)
            if _has_attr("app.agents.base", "complete")
            else contextlib.nullcontext(),
        ):
            conversation_id = scenario.context.conversation_id or f"scenario-{scenario.id}"

            task = await _build_task(scenario, scenario.request_text, conversation_id)
            try:
                response = await handles.orchestrator.handle_task(task)
            except Exception as exc:
                raise AssertionError(
                    f"[{scenario.id}] orchestrator.handle_task raised: {type(exc).__name__}: {exc}"
                ) from exc

            _assert_outcome(scenario, scenario.expected, response, handles)

            # Follow-up turns reuse the same orchestrator (memory carries through).
            for turn in scenario.follow_up:
                # Feed extra LLM replies for this turn.
                if turn.llm.classify:
                    handles.llm.feed("orchestrator", turn.llm.classify)
                for agent_id, replies in turn.llm.agents.items():
                    handles.llm.feed(agent_id, replies)
                # Reset call list for per-turn assertions.
                pre_count = len(handles.ha_client.calls)
                t2 = await _build_task(scenario, turn.text, conversation_id)
                response = await handles.orchestrator.handle_task(t2)
                _assert_outcome(
                    scenario,
                    turn.expected,
                    response,
                    handles,
                    new_call_offset=pre_count,
                )


def _has_attr(modname, attr):
    try:
        import importlib

        m = importlib.import_module(modname)
        return hasattr(m, attr)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Assertion contract
# ---------------------------------------------------------------------------


def _assert_outcome(
    scenario: Scenario,
    expected: Expected,
    response: dict,
    handles: PipelineHandles,
    *,
    new_call_offset: int = 0,
) -> None:
    sid = scenario.id
    speech = (response or {}).get("speech", "") or ""
    routed_to = (response or {}).get("routed_to") or (response or {}).get("agent_id")

    # 1. Routing
    if expected.routed_agent and routed_to and routed_to != expected.routed_agent:
        raise AssertionError(
            f"[{sid}] routing mismatch: expected agent={expected.routed_agent!r} "
            f"got={routed_to!r} response_keys={list((response or {}).keys())}"
        )

    # 2. Service calls (in order)
    new_calls = handles.ha_client.calls[new_call_offset:]
    if expected.service_calls:
        if not expected.allow_extra_calls and len(new_calls) > len(expected.service_calls):
            extra = [(c.domain, c.service, c.entity_id) for c in new_calls[len(expected.service_calls) :]]
            raise AssertionError(
                f"[{sid}] unexpected extra service calls: {extra}\n"
                f"all calls: {[(c.domain, c.service, c.entity_id) for c in new_calls]}"
            )
        for i, exp in enumerate(expected.service_calls):
            if i >= len(new_calls):
                raise AssertionError(
                    f"[{sid}] missing expected service call #{i}: "
                    f"{exp.domain}.{exp.service} on {exp.target_entity}\n"
                    f"recorded: {[(c.domain, c.service, c.entity_id) for c in new_calls]}"
                )
            actual = new_calls[i]
            if actual.domain != exp.domain or actual.service != exp.service:
                raise AssertionError(
                    f"[{sid}] service call #{i} mismatch: expected "
                    f"{exp.domain}.{exp.service} got {actual.domain}.{actual.service}"
                )
            if exp.target_entity and actual.entity_id != exp.target_entity:
                raise AssertionError(
                    f"[{sid}] service call #{i} target mismatch: expected "
                    f"{exp.target_entity!r} got {actual.entity_id!r}"
                )
            for key in exp.service_data_keys:
                if key not in actual.service_data:
                    raise AssertionError(
                        f"[{sid}] service call #{i} missing expected key {key!r} in service_data={actual.service_data}"
                    )
            for key, val in exp.service_data.items():
                if actual.service_data.get(key) != val:
                    raise AssertionError(
                        f"[{sid}] service call #{i} {key}={actual.service_data.get(key)!r} expected {val!r}"
                    )
    elif new_calls and not expected.allow_extra_calls:
        unexpected = [(c.domain, c.service, c.entity_id) for c in new_calls]
        raise AssertionError(f"[{sid}] expected no service calls, got: {unexpected}")

    # 3. Speech
    speech_lc = speech.lower()
    for needle in expected.speech_contains:
        if needle.lower() not in speech_lc:
            raise AssertionError(f"[{sid}] expected speech to contain {needle!r}; got {speech!r}")
    for needle in expected.speech_excludes:
        if needle.lower() in speech_lc:
            raise AssertionError(f"[{sid}] expected speech to NOT contain {needle!r}; got {speech!r}")

    # 4. action_executed subset
    if expected.action_executed is not None:
        actual_action = (response or {}).get("action_executed") or {}
        for key, val in expected.action_executed.items():
            if actual_action.get(key) != val:
                raise AssertionError(
                    f"[{sid}] action_executed.{key} mismatch: got {actual_action.get(key)!r} expected {val!r}"
                )

    # 5. Error
    if expected.error is not None:
        err = (response or {}).get("error") or {}
        actual_code = err.get("code") if isinstance(err, dict) else getattr(err, "code", None)
        if actual_code != expected.error.code:
            raise AssertionError(
                f"[{sid}] expected error code {expected.error.code!r} got {actual_code!r}; response={response!r}"
            )
