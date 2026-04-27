"""Admin dashboard routes."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import __version__ as _app_version
from app.config import settings as app_settings
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL
from app.security.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    _login_url,
    _rooted_url,
    attach_csrf,
    authenticate_admin,
    create_session_cookie,
    ensure_csrf_token,
    require_admin_session_redirect,
    verify_csrf,
)

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["app_version"] = _app_version
templates.env.globals["default_local_embedding_model"] = DEFAULT_LOCAL_EMBEDDING_MODEL
templates.env.globals["root_url"] = _rooted_url

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _render_dashboard(request: Request, template_name: str, **extra):
    token = ensure_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        template_name,
        context={"csrf_token": token, "root_path": request.scope.get("root_path") or "", **extra},
    )
    attach_csrf(request, response, token)
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard_index(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Dashboard home page."""
    return _render_dashboard(request, "overview.html")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    """Login page."""
    token = ensure_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "login.html",
        context={"title": "Login", "error": error, "csrf_token": token},
    )
    attach_csrf(request, response, token)
    return response


@router.post(
    "/login",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_csrf)],
)
async def login_submit(
    request: Request,
    username: str = Form("admin"),
    password: str = Form(...),
):
    """Handle login form submission."""
    session_data = await authenticate_admin(username, password)
    if session_data is None:
        token = ensure_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "login.html",
            context={"title": "Login", "error": "Invalid credentials", "csrf_token": token},
        )
        attach_csrf(request, response, token)
        return response
    cookie_value = create_session_cookie(session_data)
    response = RedirectResponse(url=_rooted_url(request, "/dashboard/"), status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        httponly=True,
        samesite="lax",
        max_age=86400,
        secure=app_settings.cookie_secure is True,
    )
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request):
    """Clear session and redirect to login."""
    response = RedirectResponse(url=_login_url(request), status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie(CSRF_COOKIE_NAME)
    return response


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Agent configuration page."""
    return _render_dashboard(request, "agents.html")


@router.get("/system-health", response_class=HTMLResponse)
async def system_health_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """System health monitoring page."""
    return _render_dashboard(request, "system_health.html")


@router.get("/health", response_class=RedirectResponse)
async def health_redirect(request: Request):
    """Redirect to API health endpoint."""
    return RedirectResponse(url=_rooted_url(request, "/api/health"))


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Chat test interface."""
    return _render_dashboard(request, "chat.html")


@router.get("/personality", response_class=HTMLResponse)
async def personality_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Personality configuration page."""
    return _render_dashboard(request, "personality.html")


@router.get("/cache", response_class=HTMLResponse)
async def cache_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Cache management page."""
    return _render_dashboard(request, "cache.html")


@router.get("/entity-index", response_class=HTMLResponse)
async def entity_index_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Entity index page."""
    return _render_dashboard(request, "entity_index.html")


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Analytics dashboard page."""
    return _render_dashboard(request, "analytics.html")


@router.get("/traces", response_class=HTMLResponse)
async def traces_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Request traces page."""
    return _render_dashboard(request, "traces.html")


@router.get("/mcp-servers", response_class=HTMLResponse)
async def mcp_servers_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """MCP server management page."""
    return _render_dashboard(request, "mcp_servers.html")


@router.get("/custom-agents", response_class=HTMLResponse)
async def custom_agents_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Custom agents builder page."""
    return _render_dashboard(request, "custom_agents.html")


@router.get("/entity-visibility", response_class=HTMLResponse)
async def entity_visibility_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Redirect to entity index (entity visibility merged into entity index)."""
    from starlette.responses import RedirectResponse

    agent = request.query_params.get("agent", "")
    url = _rooted_url(request, "/dashboard/entity-index")
    if agent:
        url += f"?agent={agent}"
    return RedirectResponse(url=url, status_code=301)


@router.get("/timers", response_class=HTMLResponse)
async def timers_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Timers & alarms dashboard page."""
    return _render_dashboard(request, "timers.html")


@router.get("/plugins", response_class=HTMLResponse)
async def plugins_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Plugin management page."""
    return _render_dashboard(request, "plugins.html")


@router.get("/send-devices", response_class=HTMLResponse)
async def send_devices_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Send device mappings management page."""
    return _render_dashboard(request, "send_devices.html")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    _session: dict = Depends(require_admin_session_redirect),
):
    """Unified settings page for all advanced configuration."""
    return _render_dashboard(request, "settings.html")
