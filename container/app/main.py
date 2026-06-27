"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import admin as admin_routes
from app.api.routes import conversation as conversation_routes
from app.api.routes import dashboard_api as dashboard_api_routes
from app.api.routes import health as health_routes
from app.bootstrap._logging import _configure_logging, _ensure_log_buffer_handler
from app.bootstrap._shutdown import teardown
from app.bootstrap._startup import setup_application
from app.config import settings
from app.dashboard.routes import router as dashboard_router
from app.middleware.auth import SetupRedirectMiddleware, apply_auth_dependencies
from app.middleware.rate_limit import rate_limit_admin
from app.middleware.tracing import TracingMiddleware
from app.setup.routes import router as setup_router

# ``_configure_logging`` and ``_ensure_log_buffer_handler`` moved to
# ``app.bootstrap._logging``; they are re-exported here (via ``__all__``) so
# existing callers and the unit tests that do
# ``from app.main import _configure_logging, _ensure_log_buffer_handler``
# keep working unchanged.
__all__ = ["_configure_logging", "_ensure_log_buffer_handler"]

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events.

    Startup is delegated to :func:`app.bootstrap._startup.setup_application`
    and shutdown to :func:`app.bootstrap._shutdown.teardown`. Both preserve
    the previous inline behavior and ordering exactly; they were extracted so
    this function is a short orchestrator rather than a ~300-line method.
    """
    await setup_application(app)
    yield
    await teardown(app)


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
