"""Shared test fixtures for agent-assist."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio

from tests.helpers import make_entity_state, shutdown_aiosqlite

# Writable paths before any ``app.*`` import (CI runners have no ``/data``).
_test_root = Path(__file__).resolve().parent / ".pytest_runtime"
_test_root.mkdir(exist_ok=True)
# pytest-xdist workers must not share the same SQLite/Chroma paths (file locks / races).
_xdist_wid = os.environ.get("PYTEST_XDIST_WORKER")
if _xdist_wid:
    os.environ["SQLITE_DB_PATH"] = str(_test_root / f"agent_assist_{_xdist_wid}.db")
    os.environ["FERNET_KEY_PATH"] = str(_test_root / f".fernet_key_{_xdist_wid}")
    os.environ["CHROMADB_PERSIST_DIR"] = str(_test_root / f"chromadb_{_xdist_wid}")
else:
    os.environ.setdefault("SQLITE_DB_PATH", str(_test_root / "agent_assist.db"))
    os.environ.setdefault("FERNET_KEY_PATH", str(_test_root / ".fernet_key"))
    os.environ.setdefault("CHROMADB_PERSIST_DIR", str(_test_root / "chromadb"))

# Force cookie_secure to False for tests so CSRF/session cookies work over HTTP.
from app.config import settings as _test_settings

_test_settings.cookie_secure = False

from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL


def build_integration_test_app(
    *,
    setup_complete: bool = True,
    override_api_key: bool = False,
    override_admin_session: bool = False,
    registry: Any | None = None,
    dispatcher: Any | None = None,
    ha_client: Any | None = None,
    mcp_registry: Any | None = None,
    mcp_tool_manager: Any | None = None,
    plugin_loader: Any | None = None,
    state_overrides: dict[str, Any] | None = None,
):
    """Build a FastAPI app for integration tests with lightweight defaults."""
    from app.main import create_app
    from app.security.auth import require_admin_session, require_admin_session_redirect, require_api_key

    app = create_app()

    if override_api_key:
        app.dependency_overrides[require_api_key] = lambda: "test-api-key"
    if override_admin_session:
        app.dependency_overrides[require_admin_session] = lambda: {"username": "admin"}
        app.dependency_overrides[require_admin_session_redirect] = lambda: {"username": "admin"}

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    app.router.lifespan_context = _noop_lifespan

    if registry is None:
        registry = MagicMock()
        registry.list_agents = AsyncMock(return_value=[])
    if dispatcher is None:
        dispatcher = MagicMock()
    if ha_client is None:
        ha_client = AsyncMock()
        ha_client.render_template = AsyncMock(return_value="")
        ha_client.get_states = AsyncMock(return_value=[])
        ha_client.get_area_registry = AsyncMock(return_value={})
        ha_client.get_config = AsyncMock(return_value={})
    if mcp_registry is None:
        mcp_registry = MagicMock()
        mcp_registry.list_servers.return_value = []
    if mcp_tool_manager is None:
        mcp_tool_manager = MagicMock()
    if plugin_loader is None:
        plugin_loader = MagicMock()
        plugin_loader.loaded_plugins = {}

    state = {
        "startup_time": 0,
        "registry": registry,
        "dispatcher": dispatcher,
        "ha_client": ha_client,
        "entity_index": None,
        "cache_manager": None,
        "entity_matcher": None,
        "alias_resolver": None,
        "custom_loader": None,
        "mcp_registry": mcp_registry,
        "mcp_tool_manager": mcp_tool_manager,
        "ws_client": None,
        "plugin_loader": plugin_loader,
        "setup_runtime_initialized": setup_complete,
    }
    if state_overrides:
        state.update(state_overrides)

    for key, value in state.items():
        setattr(app.state, key, value)

    return app


# ---------------------------------------------------------------------------
# 1. db_path -- temporary SQLite file per test
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Return a temporary SQLite database path.

    The file is created lazily by aiosqlite on first connection.
    Cleanup is handled automatically by pytest's tmp_path.
    """
    return tmp_path / "test_agent_assist.db"


@pytest.fixture(autouse=True)
def _reset_rate_limit_store():
    from app.middleware.rate_limit import reset_rate_limit_store

    reset_rate_limit_store()
    yield
    reset_rate_limit_store()


@pytest.fixture(autouse=True)
async def _clear_settings_cache():
    """P3-6: drop the in-memory ``SettingsRepository`` value cache between tests.

    Tests rotate through fresh temporary databases (``db_path``), so a
    cached value keyed by name from one test must not leak into the
    next. The cache is module-level on the repository class.
    """
    from app.db.repository import SettingsRepository

    await SettingsRepository._cache_invalidate()
    yield
    await SettingsRepository._cache_invalidate()


@pytest.fixture(autouse=True)
async def _reset_write_conn():
    """Close the shared global write connection so it never leaks across loops."""
    yield
    try:
        import app.db.schema as _schema
        from tests.helpers import shutdown_aiosqlite

        if _schema._write_conn is not None:
            await shutdown_aiosqlite(_schema._write_conn)
            _schema._write_conn = None
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _patch_aiosqlite_close():
    """Monkey-patch aiosqlite.Connection.close so it always joins the worker thread.

    This prevents pytest-asyncio from closing the event loop while aiosqlite
    background threads are still alive, which was causing hundreds of
    ``RuntimeError: Event loop is closed`` warnings across the test suite.
    """
    _original_close = aiosqlite.Connection.close

    async def _patched_close(self) -> None:
        await _original_close(self)
        try:
            thread = self._thread
            if thread.is_alive():
                thread.join(timeout=1.0)
        except Exception:
            pass

    aiosqlite.Connection.close = _patched_close
    yield
    aiosqlite.Connection.close = _original_close


@pytest.fixture(scope="session", autouse=True)
def _close_vector_store_on_session_end():
    """Close the Chroma singleton explicitly so pytest does not hang on shutdown."""
    yield
    try:
        from app.cache.vector_store import close_vector_store

        close_vector_store()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 2. db_repository -- schema + seed data on a temp DB
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db_repository(db_path: Path):
    """Yield a temporary database initialized with schema and seed data.

    Patches ``app.config.settings.sqlite_db_path`` so that all repository
    classes that call ``get_db()`` use the temporary file.
    """
    import aiosqlite

    from app.db.schema import _create_indexes, _create_tables, _seed_defaults

    with patch("app.db.schema.settings") as mock_settings, patch("app.config.settings") as mock_cfg:
        mock_settings.sqlite_db_path = str(db_path)
        mock_cfg.sqlite_db_path = str(db_path)

        # Initialize schema
        db = await aiosqlite.connect(str(db_path))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await db.commit()
        await shutdown_aiosqlite(db)

        # Patch get_db so repository classes use the temp db
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _temp_get_db():
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

        with (
            patch("app.db.repository.get_db_read", _temp_get_db),
            patch("app.db.repository.get_db_write", _temp_get_db),
            patch("app.db.schema.get_db_read", _temp_get_db),
            patch("app.db.schema.get_db_write", _temp_get_db),
        ):
            yield db_path


# ---------------------------------------------------------------------------
# 3. mock_ha_rest_client -- AsyncMock of HARestClient
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_ha_rest_client() -> AsyncMock:
    """Return an AsyncMock standing in for ``HARestClient``.

    Pre-configures commonly used methods with realistic return values.
    """
    client = AsyncMock()

    sample_states = [
        make_entity_state("light.kitchen_ceiling", "Kitchen Ceiling", state="on", area="kitchen"),
        make_entity_state("light.living_room_lamp", "Living Room Lamp", state="off", area="living_room"),
        make_entity_state("light.bedroom_light", "Bedroom Light", state="off", area="bedroom"),
        make_entity_state("media_player.living_room_speaker", "Living Room Speaker", state="idle", area="living_room"),
        make_entity_state("climate.thermostat", "Thermostat", state="heat", area="hallway"),
    ]

    client.get_states = AsyncMock(return_value=sample_states)
    client.get_state = AsyncMock(return_value=sample_states[0])
    client.call_service = AsyncMock(return_value={"success": True})
    client.initialize = AsyncMock()
    client.close = AsyncMock()
    client.test_connection = AsyncMock(return_value=True)
    client.fire_event = AsyncMock(return_value={"message": "Event fired."})
    return client


# ---------------------------------------------------------------------------
# 4. mock_litellm -- patches litellm.acompletion and litellm.aembedding
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_litellm():
    """Patch ``litellm.acompletion`` and ``litellm.aembedding``.

    Returns a namespace object with ``acompletion`` and ``aembedding``
    mocks so tests can customise return values.
    """
    choice = MagicMock()
    choice.message.content = "Sure, I turned on the light."
    choice.message.role = "assistant"
    choice.finish_reason = "stop"

    completion_response = MagicMock()
    completion_response.choices = [choice]
    completion_response.model = "openrouter/openai/gpt-4o-mini"
    completion_response.usage.prompt_tokens = 40
    completion_response.usage.completion_tokens = 15
    completion_response.usage.total_tokens = 55

    embedding_data = MagicMock()
    embedding_data.embedding = [0.0] * 384

    embedding_response = MagicMock()
    embedding_response.data = [embedding_data]
    embedding_response.model = "local/all-MiniLM-L6-v2"

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, return_value=completion_response) as mock_comp,
        patch("litellm.aembedding", new_callable=AsyncMock, return_value=embedding_response) as mock_emb,
    ):
        ns = MagicMock()
        ns.acompletion = mock_comp
        ns.aembedding = mock_emb
        ns.completion_response = completion_response
        ns.embedding_response = embedding_response
        yield ns


# ---------------------------------------------------------------------------
# 5. mock_chromadb -- MagicMock of a ChromaDB collection
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_chromadb() -> MagicMock:
    """Return a MagicMock simulating a ChromaDB collection.

    ``query()`` returns configurable results with distances and documents.
    """
    collection = MagicMock()
    collection.name = "test_collection"
    collection.count.return_value = 5
    collection.add = MagicMock()
    collection.delete = MagicMock()
    collection.query.return_value = {
        "ids": [["light.kitchen_ceiling", "light.living_room_lamp"]],
        "distances": [[0.05, 0.15]],
        "documents": [["Kitchen Ceiling light kitchen", "Living Room Lamp light living_room"]],
        "metadatas": [
            [
                {"entity_id": "light.kitchen_ceiling", "domain": "light", "area": "kitchen"},
                {"entity_id": "light.living_room_lamp", "domain": "light", "area": "living_room"},
            ]
        ],
    }
    return collection


# ---------------------------------------------------------------------------
# 6. app_client -- async httpx test client against FastAPI app
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def app_client(mock_ha_rest_client, db_repository, mock_chromadb):
    """Create an httpx.AsyncClient wired to the FastAPI test app.

    Overrides heavyweight startup dependencies (HA client, DB, ChromaDB)
    with mocks so the test app boots quickly.

    Marked implicitly for integration use via its dependency chain.
    """
    from unittest.mock import patch as _patch

    import httpx

    # Patch lifespan-level dependencies so the app boots without real HA/LLM
    with (
        _patch("app.main.HARestClient", return_value=mock_ha_rest_client) if True else None,
        _patch("app.main.init_db", new_callable=AsyncMock),
    ):
        from app.main import create_app

        test_app = create_app()

        # Override lifespan to skip real initialization
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _test_lifespan(app):
            app.state.startup_time = 0
            app.state.registry = MagicMock()
            app.state.dispatcher = MagicMock()
            app.state.ha_client = mock_ha_rest_client
            app.state.entity_index = None
            app.state.cache_manager = None
            app.state.entity_matcher = None
            app.state.alias_resolver = None
            app.state.custom_loader = None
            app.state.mcp_registry = MagicMock()
            app.state.mcp_tool_manager = MagicMock()
            app.state.ws_client = None
            app.state.plugin_loader = MagicMock()
            yield

        test_app.router.lifespan_context = _test_lifespan

        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


# ---------------------------------------------------------------------------
# 7. mock_websocket -- AsyncMock of a WebSocket connection
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_websocket() -> AsyncMock:
    """Return an AsyncMock simulating a WebSocket connection."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"type": "auth_ok"})
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# 8. sample_entities -- list of HA entity state dicts
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_entities() -> list[dict[str, Any]]:
    """Return a representative set of HA entity state dicts for cross-module tests."""
    return [
        make_entity_state("light.kitchen_ceiling", "Kitchen Ceiling", state="on", area="kitchen"),
        make_entity_state("light.living_room_lamp", "Living Room Lamp", state="off", area="living_room"),
        make_entity_state("light.bedroom_light", "Bedroom Light", state="off", area="bedroom"),
        make_entity_state("media_player.living_room_speaker", "Living Room Speaker", state="idle", area="living_room"),
        make_entity_state(
            "climate.thermostat",
            "Thermostat",
            state="heat",
            area="hallway",
            attributes={"temperature": 22, "current_temperature": 21},
        ),
        make_entity_state("lock.front_door", "Front Door Lock", state="locked", area="entrance"),
        make_entity_state("scene.movie_night", "Movie Night", state="scening"),
        make_entity_state("automation.morning_routine", "Morning Routine", state="on"),
    ]


# ---------------------------------------------------------------------------
# 9. mock_settings -- default settings dict mirroring seed data
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_settings() -> dict[str, str]:
    """Return a dict of default settings mirroring the seed data from ``app.db.schema``."""
    return {
        "cache.enabled": "true",
        "cache.compound_utterance_bypass": "true",
        "cache.routing.enabled": "true",
        "cache.routing.semantic_threshold": "0.92",
        "cache.routing.max_entries": "50000",
        "cache.action.enabled": "true",
        "cache.action.semantic_threshold": "0.95",
        "cache.action.max_entries": "50000",
        "embedding.provider": "local",
        "embedding.local_model": DEFAULT_LOCAL_EMBEDDING_MODEL,
        "embedding.external_model": "",
        "embedding.dimension": "384",
        "entity_matching.confidence_threshold": "0.60",
        "entity_matching.top_n_candidates": "3",
        "entity_matching.oversample_factor": "20",
        "rewrite.model": "groq/llama-3.1-8b-instant",
        "rewrite.temperature": "0.8",
        "personality.prompt": "",
        "communication.streaming_mode": "websocket",
        "communication.ws_reconnect_interval": "5",
        "communication.stream_buffer_size": "1",
        "a2a.default_timeout": "5",
        "a2a.max_iterations": "3",
        "general.conversation_context_turns": "3",
    }


@pytest.fixture(autouse=True)
def _ensure_voluptuous_mock():
    """Ensure voluptuous is mocked for HA custom_components tests."""
    import sys
    from unittest.mock import MagicMock

    if "voluptuous" not in sys.modules:
        sys.modules["voluptuous"] = MagicMock()
    yield


# ---------------------------------------------------------------------------
# 10. build_scenario_backed_app -- real pipeline with deterministic stubs
# ---------------------------------------------------------------------------


def build_scenario_backed_app(
    scenario,
    db_path: Path,
    *,
    api_key: str = "test-api-key",
):
    """Build a FastAPI app wired to a real orchestrator pipeline for a scenario.

    Uses :func:`tests.scenarios.runner.build_pipeline` and
    :func:`tests.scenarios.runner._temp_db` so the orchestrator, agents,
    entity index, and deterministic LLM stub are all real.
    """
    from app.api.routes import conversation as conversation_routes
    from app.main import create_app

    app = create_app()

    @asynccontextmanager
    async def _scenario_lifespan(_app):
        from tests.scenarios.runner import _temp_db, build_pipeline

        async with _temp_db(db_path):
            # Apply scenario preconditions
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

            # Register orchestrator so the dispatcher can route to it
            await handles.registry.register(handles.orchestrator)

            # Inject real handles into app state
            _app.state.orchestrator = handles.orchestrator
            _app.state.registry = handles.registry
            _app.state.dispatcher = handles.dispatcher
            _app.state.ha_client = handles.ha_client
            _app.state.entity_index = handles.entity_index
            _app.state.entity_matcher = handles.entity_matcher
            _app.state.setup_runtime_initialized = True

            # Wire dispatcher to conversation routes
            conversation_routes.set_dispatcher(handles.dispatcher)

            async def _complete_router(agent_id, messages, **kwargs):
                return await handles.llm.complete(agent_id, messages, **kwargs)

            # Seed API key so auth works end-to-end
            from app.security.encryption import store_secret

            await store_secret("container_api_key", api_key)

            # Mark setup complete so SetupRedirectMiddleware allows requests
            from app.db.repository import SetupStateRepository

            for step in ("admin_password", "ha_connection", "container_api_key", "llm_providers", "review_complete"):
                await SetupStateRepository.set_step_completed(step)

            from unittest.mock import patch

            llm_patcher = patch("app.llm.client.complete", new=_complete_router)
            llm_patcher.start()
            base_patcher = None
            try:
                base_patcher = patch("app.agents.base.complete", new=_complete_router)
                base_patcher.start()
            except Exception:
                base_patcher = None
            try:
                yield
            finally:
                llm_patcher.stop()
                if base_patcher is not None:
                    base_patcher.stop()

    app.router.lifespan_context = _scenario_lifespan
    return app
