"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.a2a.dispatcher import Dispatcher
from app.a2a.orchestrator_gateway import AgentCatalog, OrchestratorGateway
from app.a2a.registry import registry
from app.a2a.transport import InProcessTransport
from app.agents.cover import CoverAgent
from app.agents.custom_loader import CustomAgentLoader
from app.agents.filler import FillerAgent
from app.agents.general import GeneralAgent
from app.agents.light import LightAgent
from app.agents.music import MusicAgent
from app.agents.orchestrator import OrchestratorAgent
from app.agents.vacuum import VacuumAgent
from app.api.routes import admin as admin_routes
from app.api.routes import conversation as conversation_routes
from app.api.routes import dashboard_api as dashboard_api_routes
from app.api.routes import health as health_routes
from app.config import settings
from app.dashboard.routes import router as dashboard_router
from app.db.repository import SettingsRepository, SetupStateRepository
from app.db.schema import init_db
from app.entity.ingest import parse_ha_states
from app.middleware.auth import SetupRedirectMiddleware, apply_auth_dependencies
from app.middleware.rate_limit import rate_limit_admin
from app.middleware.tracing import TracingMiddleware
from app.models.entity_index import EntityIndexEntry
from app.setup.routes import router as setup_router
from app.util.log_buffer import LogBuffer, LogBufferHandler, get_log_buffer, set_log_buffer

logger = logging.getLogger(__name__)


# P3-11: entity-sync loop tunables. The interval can be overridden at
# runtime via the ``entity_sync.interval_minutes`` setting; these are
# the defaults / fallbacks used when the setting is missing or 0.
_ENTITY_SYNC_DEFAULT_INTERVAL_MIN = 30
_ENTITY_SYNC_DISABLED_RECHECK_SEC = 300


def _ensure_log_buffer_handler() -> None:
    """Ensure root logger has the correct level and log buffer handler.

    Uvicorn or other libraries may reconfigure logging after our lifespan
    starts, wiping handlers or changing the root level.  This helper re-
    attaches the buffer handler and restores the configured level whenever
    it is called.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()

    root.setLevel(level)

    # Re-attach buffer handler if missing.
    has_buffer = any(isinstance(h, LogBufferHandler) for h in root.handlers)
    if not has_buffer:
        log_buffer = get_log_buffer()
        if log_buffer is None:
            log_buffer = LogBuffer(capacity=10000)
            set_log_buffer(log_buffer)
        buffer_handler = LogBufferHandler(log_buffer)
        root.addHandler(buffer_handler)


def _configure_logging() -> None:
    """Configure structured logging based on settings."""
    log_format = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Only add StreamHandler if root has no handlers yet.
    # Avoid force=True which wipes handlers that uvicorn or other
    # libraries may have already configured.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter(log_format))
        root.addHandler(stream_handler)

    _ensure_log_buffer_handler()


def _parse_ha_states(states: list[dict[str, Any]]) -> list[EntityIndexEntry]:
    """Parse HA GET /api/states response into EntityIndexEntry list."""
    return parse_ha_states(states)


async def _periodic_entity_sync(app: FastAPI) -> None:
    """Periodically sync entity index with Home Assistant state."""
    while True:
        try:
            raw = await SettingsRepository.get_value(
                "entity_sync.interval_minutes", str(_ENTITY_SYNC_DEFAULT_INTERVAL_MIN)
            )
            interval_minutes = int(raw)
        except (TypeError, ValueError):
            interval_minutes = _ENTITY_SYNC_DEFAULT_INTERVAL_MIN

        if interval_minutes <= 0:
            # Disabled -- check again later in case the setting changes.
            await asyncio.sleep(_ENTITY_SYNC_DISABLED_RECHECK_SEC)
            continue

        await asyncio.sleep(interval_minutes * 60)

        try:
            ha_client = app.state.ha_client
            entity_index = app.state.entity_index
            if not ha_client or not entity_index:
                continue

            states = await ha_client.get_states()
            hidden_ids = await ha_client.get_hidden_entity_ids()
            app.state.hidden_entity_ids = hidden_ids
            entities = parse_ha_states(states, hidden_ids=hidden_ids)
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


async def _purge_stale_response_cache(cache_manager) -> None:
    """One-time startup task: purge stale read-only response cache entries."""
    try:
        count = await cache_manager.purge_readonly_entries()
        if count:
            logger.info("Purged %d stale read-only response cache entries", count)
        else:
            logger.info("No stale read-only response cache entries to purge")
    except Exception:
        logger.warning("Failed to purge stale response cache entries", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # --- Startup ---
    _configure_logging()
    logger.info("Starting agent-assist container")
    await init_db()

    from app.security.encryption import get_fernet, is_fernet_key_present

    if is_fernet_key_present():
        logger.warning(
            "IMPORTANT: Back up your Fernet key at /data/.fernet_key. "
            "Loss of this file makes all encrypted secrets (HA token, LLM keys, API key) unrecoverable."
        )

    # Eager-load Fernet key off the event loop so the cold-path sync
    # file I/O happens during startup instead of on the first request.
    await asyncio.to_thread(get_fernet)

    # Register default sync interval setting if not already set
    existing = await SettingsRepository.get_value("entity_sync.interval_minutes")
    if existing is None:
        await SettingsRepository.set(
            "entity_sync.interval_minutes",
            "30",
            value_type="number",
            category="sync",
            description="Minutes between periodic entity index syncs (0 = disabled)",
        )

    # Register default filler settings if not already set
    if await SettingsRepository.get_value("filler.enabled") is None:
        await SettingsRepository.set(
            "filler.enabled",
            "false",
            value_type="bool",
            category="filler",
            description="Enable interim filler responses for slow agents",
        )
    if await SettingsRepository.get_value("filler.threshold_ms") is None:
        await SettingsRepository.set(
            "filler.threshold_ms",
            "1000",
            value_type="number",
            category="filler",
            description="Milliseconds to wait before sending filler",
        )

    # Register default mediation settings if not already set
    if await SettingsRepository.get_value("mediation.model") is None:
        await SettingsRepository.set(
            "mediation.model",
            "",
            value_type="string",
            category="mediation",
            description="LLM model for mediation/merge (empty = use orchestrator model)",
        )
    if await SettingsRepository.get_value("mediation.temperature") is None:
        await SettingsRepository.set(
            "mediation.temperature",
            "0.3",
            value_type="number",
            category="mediation",
            description="Temperature for mediation/merge LLM calls",
        )
    if await SettingsRepository.get_value("mediation.max_tokens") is None:
        await SettingsRepository.set(
            "mediation.max_tokens",
            "8192",
            value_type="number",
            category="mediation",
            description="Max tokens for mediation/merge LLM calls (increase for reasoning models)",
        )

    # Register default language setting if not already set
    if await SettingsRepository.get_value("language") is None:
        await SettingsRepository.set(
            "language",
            "auto",
            value_type="string",
            category="general",
            description="Response language: 'auto' = detect from user input, or a specific ISO code like 'de', 'en'",
        )

    # Check if setup is complete before initializing HA-dependent components
    setup_complete = await SetupStateRepository.is_complete()
    app.state.setup_runtime_init_lock = asyncio.Lock()
    app.state.setup_runtime_initialized = False

    # FLOW-SETUP-1 (P1-2): core A2A + MCP primitives must exist regardless
    # of setup-completion so setup-wizard requests can still be dispatched.
    # These also back the shared ``_initialize_setup_dependent_services``
    # helper used by both this lifespan and the post-wizard re-init path.
    transport = InProcessTransport(registry)
    dispatcher = Dispatcher(registry, transport)
    orchestrator_gateway = OrchestratorGateway(dispatcher)
    conversation_routes.set_dispatcher(dispatcher)
    dashboard_api_routes.set_chat_dispatcher(dispatcher)
    admin_routes.set_registry(registry)

    from app.mcp.registry import MCPServerRegistry
    from app.mcp.tools import MCPToolManager

    mcp_registry = MCPServerRegistry()
    mcp_tool_manager = MCPToolManager(mcp_registry)

    app.state.registry = registry
    app.state.dispatcher = dispatcher
    app.state.orchestrator_gateway = orchestrator_gateway
    app.state.mcp_registry = mcp_registry
    app.state.mcp_tool_manager = mcp_tool_manager

    if setup_complete:
        # Delegate to the shared helper -- populates ha_client,
        # entity_index, cache_manager, registers all agents, wires
        # the HA WebSocket client, and starts background tasks.
        from app.runtime_setup import _initialize_setup_dependent_services

        await _initialize_setup_dependent_services(app, source="lifespan")
        app.state.setup_runtime_initialized = True
    else:
        # Setup wizard path: register the core agents with None HA deps
        # so the A2A surface is usable enough to serve the wizard.
        filler_agent = FillerAgent(ha_client=None, entity_index=None)
        await registry.register(filler_agent)
        orchestrator_agent = OrchestratorAgent(
            dispatcher=dispatcher,
            registry=registry,
            cache_manager=None,
            ha_client=None,
            entity_index=None,
            filler_agent=filler_agent,
        )
        await registry.register(orchestrator_agent)

        general_agent = GeneralAgent(ha_client=None, entity_index=None, mcp_tool_manager=mcp_tool_manager)
        await registry.register(general_agent)

        light_agent = LightAgent(ha_client=None, entity_index=None, entity_matcher=None)
        await registry.register(light_agent)

        music_agent = MusicAgent(ha_client=None, entity_index=None, entity_matcher=None)
        await registry.register(music_agent)

        cover_agent = CoverAgent(ha_client=None, entity_index=None, entity_matcher=None)
        await registry.register(cover_agent)

        vacuum_agent = VacuumAgent(ha_client=None, entity_index=None, entity_matcher=None)
        await registry.register(vacuum_agent)

        custom_loader = CustomAgentLoader(
            registry,
            ha_client=None,
            entity_index=None,
            mcp_tool_manager=mcp_tool_manager,
        )
        await custom_loader.load_all()
        app.state.custom_loader = custom_loader

        await orchestrator_agent.initialize()

    # Populate allowed WebSocket origins from HA URL
    ha_client = getattr(app.state, "ha_client", None)
    if ha_client is not None and getattr(ha_client, "_base_url", None):
        from urllib.parse import urlparse

        parsed = urlparse(ha_client._base_url)
        app.state.allowed_ws_origins = {f"{parsed.scheme}://{parsed.netloc}"}
    else:
        app.state.allowed_ws_origins = set()

    # Register default notification profile if not set
    existing_notif = await SettingsRepository.get_value("notification.profile")
    if existing_notif is None:
        import json as _json

        await SettingsRepository.set(
            "notification.profile",
            _json.dumps(
                {
                    "tts_enabled": True,
                    "tts_engine": "tts.google_translate_say",
                    "persistent_enabled": True,
                    "push_enabled": False,
                    "push_targets": [],
                    "voice_followup_enabled": True,
                    "tts_to_listen_delay": 4.0,
                    "chime_enabled": True,
                    "chime_url": "media-source://media_source/local/notification.mp3",
                }
            ),
            value_type="json",
            category="notification",
            description="Timer/alarm notification profile: channels and targets",
        )

    # Store on app.state for access elsewhere if needed. The
    # ``_initialize_setup_dependent_services`` helper already wrote
    # ``ha_client``/``entity_index``/``cache_manager``/``entity_matcher``/
    # ``alias_resolver``/``custom_loader``/``ws_client``/
    # ``sync_task``/``alarm_monitor`` when setup was complete. For the
    # non-setup path those stay ``None``/absent which downstream routes
    # already tolerate.
    app.state.startup_time = time.time()
    app.state.entity_index_init_task = getattr(app.state, "entity_index_init_task", None)
    for _attr in (
        "ha_client",
        "entity_index",
        "cache_manager",
        "entity_matcher",
        "alias_resolver",
        "ws_client",
        "sync_task",
        "alarm_monitor",
        "timer_scheduler",
    ):
        if not hasattr(app.state, _attr):
            setattr(app.state, _attr, None)

    # --- Plugin System (Batch F) ---
    from app.plugins.base import PluginContext
    from app.plugins.hooks import LifecyclePhase
    from app.plugins.loader import PluginLoader

    plugin_context = PluginContext(
        agent_catalog=AgentCatalog(registry),
        orchestrator_gateway=orchestrator_gateway,
        mcp_registry=mcp_registry,
        settings_repo=SettingsRepository,
        app=app,
    )
    plugin_dir = str(Path(__file__).resolve().parent.parent / "plugins")
    plugin_loader = PluginLoader(plugin_dir, plugin_context)
    await plugin_loader.discover_and_load()
    await plugin_loader.run_lifecycle(LifecyclePhase.CONFIGURE)
    await plugin_loader.run_lifecycle(LifecyclePhase.STARTUP)
    await plugin_loader.run_lifecycle(LifecyclePhase.READY)
    app.state.plugin_loader = plugin_loader

    if not settings.cookie_secure:
        logger.warning(
            "COOKIE_SECURE is disabled. Admin session and CSRF cookies "
            "will be sent over plain HTTP. Enable COOKIE_SECURE for production."
        )
    logger.info("Startup complete (setup_complete=%s)", setup_complete)

    # Re-ensure log buffer handler after all startup code -- uvicorn or
    # other libraries may have reconfigured logging during startup.
    _ensure_log_buffer_handler()

    # Start a lightweight guard task that re-attaches the buffer handler
    # if something removes it at runtime (e.g. a library calling
    # logging.config.dictConfig).
    async def _log_buffer_guard() -> None:
        while True:
            await asyncio.sleep(10)
            _ensure_log_buffer_handler()

    app.state.log_buffer_guard_task = asyncio.create_task(_log_buffer_guard(), name="log_buffer_guard")

    # Start SSE tickers for live dashboard updates
    from app.api.routes.sse import register_sse_tickers

    register_sse_tickers(app)

    yield

    # --- Shutdown ---
    logger.info("Shutting down agent-assist container")

    # Plugin shutdown (isolated -- errors must not block remaining cleanup)
    try:
        await plugin_loader.run_lifecycle(LifecyclePhase.SHUTDOWN)
    except Exception:
        logger.warning("Plugin shutdown error (continuing cleanup)", exc_info=True)

    purge_task = getattr(app.state, "purge_task", None)
    flush_task = getattr(app.state, "flush_task", None)
    ws_task = getattr(app.state, "ws_task", None)
    sync_task = getattr(app.state, "sync_task", None)
    alarm_monitor = getattr(app.state, "alarm_monitor", None)
    ws_client = getattr(app.state, "ws_client", None)
    ha_client = getattr(app.state, "ha_client", None)
    cache_manager = getattr(app.state, "cache_manager", None)
    entity_index_init_task = getattr(app.state, "entity_index_init_task", None)
    timer_scheduler = getattr(app.state, "timer_scheduler", None)

    if alarm_monitor:
        await alarm_monitor.stop()

    if timer_scheduler:
        try:
            await timer_scheduler.stop()
        except Exception:
            logger.warning("TimerScheduler.stop failed", exc_info=True)

    if ws_client:
        await ws_client.disconnect()

    # Cancel background tasks and await them to ensure cleanup completes
    tasks_to_cancel = []
    if purge_task and not purge_task.done():
        purge_task.cancel()
        tasks_to_cancel.append(purge_task)
    if flush_task and not flush_task.done():
        flush_task.cancel()
        tasks_to_cancel.append(flush_task)
    if ws_task and not ws_task.done():
        ws_task.cancel()
        tasks_to_cancel.append(ws_task)
    if sync_task and not sync_task.done():
        sync_task.cancel()
        tasks_to_cancel.append(sync_task)
    if entity_index_init_task and not entity_index_init_task.done():
        entity_index_init_task.cancel()
        tasks_to_cancel.append(entity_index_init_task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

    # Cancel SSE ticker tasks with timeout
    sse_ticker_tasks = getattr(app.state, "sse_ticker_tasks", [])
    for task in sse_ticker_tasks:
        if not task.done():
            task.cancel()
    if sse_ticker_tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*sse_ticker_tasks, return_exceptions=True), timeout=5.0)
        except TimeoutError:
            logger.warning("SSE ticker tasks did not shut down within 5 seconds")

    # Flush buffered cache hit-count updates before closing stores
    try:
        if cache_manager:
            cache_manager.flush_pending()
    except Exception:
        logger.warning("Cache flush_pending failed at shutdown", exc_info=True)

    from app.cache.vector_store import close_vector_store

    mcp_tool_manager.invalidate_all()
    await mcp_registry.disconnect_all()
    if ha_client:
        await ha_client.close()
    close_vector_store()
    from app.db.schema import close_db

    await close_db()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Application factory."""
    from app import __version__

    app = FastAPI(
        title="agent-assist",
        version=__version__,
        lifespan=lifespan,
    )

    # Exception handlers
    apply_auth_dependencies(app)

    # Setup redirect middleware (redirects to /setup/ if unconfigured)
    app.add_middleware(SetupRedirectMiddleware)

    # Tracing middleware (trace ID + request logging)
    app.add_middleware(TracingMiddleware)

    # CORS middleware
    _cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=bool(_cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(health_routes.router)
    app.include_router(setup_router)
    app.include_router(conversation_routes.router)
    app.include_router(admin_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(dashboard_api_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(dashboard_router)

    # SSE router
    from app.api.routes import sse as sse_routes

    app.include_router(sse_routes.router, dependencies=[Depends(rate_limit_admin)])

    # Batch C routers
    from app.api.routes import cache_api as cache_api_routes
    from app.api.routes import conversations_api as conversations_api_routes
    from app.api.routes import entity_index_api as entity_index_api_routes

    app.include_router(conversations_api_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(cache_api_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(entity_index_api_routes.router, dependencies=[Depends(rate_limit_admin)])

    # Batch D routers
    from app.api.routes import analytics_api as analytics_api_routes
    from app.api.routes import traces_api as traces_api_routes

    app.include_router(analytics_api_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(traces_api_routes.router, dependencies=[Depends(rate_limit_admin)])

    # Batch E routers
    from app.api.routes import custom_agents_api as custom_agents_api_routes
    from app.api.routes import domain_agent_map_api as domain_agent_map_api_routes
    from app.api.routes import entity_visibility_api as entity_visibility_api_routes
    from app.api.routes import mcp_api as mcp_api_routes

    app.include_router(mcp_api_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(custom_agents_api_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(entity_visibility_api_routes.router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(entity_visibility_api_routes.entities_router, dependencies=[Depends(rate_limit_admin)])
    app.include_router(domain_agent_map_api_routes.router, dependencies=[Depends(rate_limit_admin)])

    # Batch F routers
    from app.api.routes import plugins_api as plugins_api_routes

    app.include_router(plugins_api_routes.router, dependencies=[Depends(rate_limit_admin)])

    # Calendar admin router
    from app.api.routes.calendar_admin import router as calendar_admin_router

    app.include_router(calendar_admin_router, dependencies=[Depends(rate_limit_admin)])

    # Logs admin router
    from app.api.routes import logs_api as logs_api_routes

    app.include_router(logs_api_routes.router, dependencies=[Depends(rate_limit_admin)])

    # Redirect root to dashboard
    from starlette.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/dashboard")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_redirect():
        return RedirectResponse(url="/dashboard/static/favicon.svg")

    # Try to mount static files (may not exist yet in dev)
    try:
        from pathlib import Path

        static_dir = Path(__file__).parent / "dashboard" / "static"
        if static_dir.is_dir():
            app.mount("/dashboard/static", StaticFiles(directory=str(static_dir)), name="dashboard-static")
    except Exception:
        logger.debug("Static files directory not found, skipping mount")

    return app


app = create_app()
