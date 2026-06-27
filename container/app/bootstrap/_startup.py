"""Application startup sequence.

:func:`setup_application` contains the full startup logic that previously
lived inline in ``app.main.lifespan``. Splitting it out lets ``lifespan``
become a short orchestrator that delegates to :func:`setup_application` for
startup and :func:`app.bootstrap._shutdown.teardown` for shutdown.

The heavy/deferred imports remain local to the function body (unchanged from
the inline implementation) to avoid circular imports and to keep module
import time low.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from app.a2a.dispatcher import Dispatcher
from app.a2a.registry import registry
from app.a2a.transport import InProcessTransport
from app.agents.custom_loader import CustomAgentLoader
from app.agents.decorator import install_all_agents
from app.api.routes import admin as admin_routes
from app.api.routes import conversation as conversation_routes
from app.api.routes import dashboard_api as dashboard_api_routes
from app.api.routes.conversation import reset_active_ws_connections
from app.bootstrap._logging import _configure_logging, start_log_buffer_guard
from app.config import settings
from app.db.repository import SettingsRepository, SetupStateRepository
from app.db.schema import init_db

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def setup_application(app: FastAPI) -> None:
    """Run the full application startup sequence (behavior preserved)."""
    _configure_logging()
    logger.info("Starting agent-assist container")
    reset_active_ws_connections()
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
    conversation_routes.set_dispatcher(dispatcher)
    dashboard_api_routes.set_chat_dispatcher(dispatcher)
    admin_routes.set_registry(registry)

    from app.mcp.registry import MCPServerRegistry
    from app.mcp.tools import MCPToolManager

    mcp_registry = MCPServerRegistry()
    mcp_tool_manager = MCPToolManager(mcp_registry)

    app.state.registry = registry
    app.state.dispatcher = dispatcher
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
        await install_all_agents(app)

        custom_loader = app.state.custom_loader = CustomAgentLoader(
            registry,
            ha_client=None,
            entity_index=None,
            mcp_tool_manager=mcp_tool_manager,
        )
        await custom_loader.load_all()

    # Populate allowed WebSocket origins from HA URL
    ha_client = getattr(app.state, "ha_client", None)
    if ha_client is not None and getattr(ha_client, "_base_url", None):
        from urllib.parse import urlparse

        parsed = urlparse(ha_client._base_url)
        app.state.allowed_ws_origins = {f"{parsed.scheme}://{parsed.netloc}"}
        logger.info("Allowed WebSocket origins: %s", sorted(app.state.allowed_ws_origins))
    else:
        app.state.allowed_ws_origins = set()
        logger.warning(
            "No allowed WebSocket origins configured; WebSocket connections will be rejected until setup is complete"
        )

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
        agent_registry=registry,
        dispatcher=dispatcher,
        mcp_registry=mcp_registry,
        settings_repo=SettingsRepository,
        app=app,
    )
    # This module lives at app/bootstrap/_startup.py -- three parents up
    # reaches the repository root (container/), where the plugins/ dir lives.
    plugin_dir = str(Path(__file__).resolve().parent.parent.parent / "plugins")
    plugin_loader = PluginLoader(plugin_dir, plugin_context)
    await plugin_loader.discover_and_load()

    try:
        await plugin_loader.run_lifecycle(LifecyclePhase.CONFIGURE)
    except Exception:
        logger.warning("Plugin CONFIGURE phase crashed for one or more plugins (continuing startup)", exc_info=True)

    try:
        await plugin_loader.run_lifecycle(LifecyclePhase.STARTUP)
    except Exception:
        logger.warning("Plugin STARTUP phase crashed for one or more plugins (continuing startup)", exc_info=True)

    try:
        await plugin_loader.run_lifecycle(LifecyclePhase.READY)
    except Exception:
        logger.warning("Plugin READY phase crashed for one or more plugins (continuing startup)", exc_info=True)

    app.state.plugin_loader = plugin_loader

    # Wire pipeline EventBus to OrchestratorAgent
    orchestrator_agent = await registry._get_handler_for_transport("orchestrator")
    if orchestrator_agent is not None:
        orchestrator_agent._event_bus = plugin_loader.event_bus
        if plugin_context.pipeline_strategies:
            orchestrator_agent.apply_pipeline_strategies(plugin_context.pipeline_strategies)

    if not settings.cookie_secure:
        logger.warning(
            "COOKIE_SECURE is disabled. Admin session and CSRF cookies "
            "will be sent over plain HTTP. Enable COOKIE_SECURE for production."
        )
    logger.info("Startup complete (setup_complete=%s)", setup_complete)

    # Re-ensure the log buffer handler after all startup code and start the
    # periodic re-attach guard task (registered via spawn_background).
    start_log_buffer_guard(app)

    # Start SSE tickers for live dashboard updates
    from app.api.routes.sse import register_sse_tickers

    register_sse_tickers(app)
