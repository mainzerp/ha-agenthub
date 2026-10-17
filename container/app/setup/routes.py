"""Setup wizard routes."""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import __version__ as _app_version
from app.dashboard.static_assets import static_url, static_version
from app.db.repository import (
    AdminAccountRepository,
    SettingsRepository,
    SetupStateRepository,
)
from app.ha_client.rest import test_ha_connection
from app.llm import model_catalog
from app.llm.client import _is_timeout_error
from app.llm.providers import (
    normalize_custom_openai_base_url,
    normalize_custom_openai_name,
    save_custom_openai_config,
)
from app.middleware.rate_limit import rate_limit_setup
from app.runtime_setup import ensure_setup_runtime_initialized
from app.security.auth import (
    _rooted_url,
    body_size_limit,
    ensure_csrf_token,
    require_admin_or_setup_open,
    set_csrf_cookie,
    verify_csrf,
)
from app.security.encryption import store_secret
from app.security.hashing import hash_password

logger = logging.getLogger(__name__)

# Hard upper bound for the wizard's LLM "test connection" (a model listing,
# possibly several HTTP requests, each bounded by the catalog's own timeout).
_LLM_TEST_TIMEOUT_S = 30.0
# Providers the wizard can configure and test.
_WIZARD_LLM_PROVIDERS = frozenset({"openrouter", "groq", "ollama", "custom_openai"})
_CUSTOM_PROVIDER_DEFAULT_NAME = "Custom Provider"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["app_version"] = _app_version
templates.env.globals["static_url"] = static_url
templates.env.globals["static_version"] = static_version
templates.env.globals["root_url"] = _rooted_url

router = APIRouter(prefix="/setup", tags=["setup"])

STEP_ORDER = [
    "admin_password",
    "ha_connection",
    "container_api_key",
    "llm_providers",
    "review_complete",
]


@router.get("/", response_class=HTMLResponse)
async def setup_index(request: Request):
    """Redirect to the first incomplete step."""
    steps = await SetupStateRepository.get_all_steps()
    step_map = {s["step"]: s["completed"] for s in steps}
    for i, step_name in enumerate(STEP_ORDER):
        if not step_map.get(step_name, False):
            return RedirectResponse(url=f"/setup/step/{i + 1}", status_code=302)
    return RedirectResponse(url="/dashboard/", status_code=302)


@router.get("/step/{step_num}", response_class=HTMLResponse)
async def render_step(request: Request, step_num: int):
    """Render the appropriate step template."""
    if not 1 <= step_num <= len(STEP_ORDER):
        return RedirectResponse(url="/setup/", status_code=302)
    steps = await SetupStateRepository.get_all_steps()
    step_map = {s["step"]: s["completed"] for s in steps}
    display_steps = {k: v for k, v in step_map.items() if k != "review_complete"} if step_num == 5 else step_map
    token = ensure_csrf_token(request)
    context = {
        "step_num": step_num,
        "total_steps": len(STEP_ORDER),
        "steps": display_steps,
        "csrf_token": token,
    }
    response = templates.TemplateResponse(request, f"step{step_num}.html", context=context)
    set_csrf_cookie(response, token)
    return response


@router.post(
    "/step/1",
    response_class=HTMLResponse,
    dependencies=[
        Depends(verify_csrf),
        Depends(body_size_limit(1 * 1024 * 1024)),
        Depends(require_admin_or_setup_open),
        Depends(rate_limit_setup),
    ],
)
async def save_admin_password(
    request: Request,
    username: str = Form("admin"),
    password: str = Form(...),
):
    """Step 1: Create admin account with bcrypt-hashed password."""
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    hashed = hash_password(password)
    await AdminAccountRepository.create(username, hashed, force_overwrite=True)
    await SetupStateRepository.set_step_completed("admin_password")
    return RedirectResponse(url="/setup/step/2", status_code=303)


@router.post(
    "/step/2",
    response_class=HTMLResponse,
    dependencies=[
        Depends(verify_csrf),
        Depends(body_size_limit(1 * 1024 * 1024)),
        Depends(require_admin_or_setup_open),
        Depends(rate_limit_setup),
    ],
)
async def save_ha_connection(
    request: Request,
    ha_url: str = Form(...),
    ha_token: str = Form(...),
):
    """Step 2: Save HA URL and token (Fernet-encrypted)."""
    ha_url = ha_url.strip().rstrip("/")
    if not ha_url or not ha_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Home Assistant URL must start with http:// or https://")
    await SettingsRepository.set("ha_url", ha_url, "string", "ha", "Home Assistant URL")
    from app.ha_client.auth import set_ha_token

    await set_ha_token(ha_token)
    await SetupStateRepository.set_step_completed("ha_connection")
    return RedirectResponse(url="/setup/step/3", status_code=303)


@router.post(
    "/step/3",
    response_class=HTMLResponse,
    dependencies=[
        Depends(verify_csrf),
        Depends(body_size_limit(1 * 1024 * 1024)),
        Depends(require_admin_or_setup_open),
        Depends(rate_limit_setup),
    ],
)
async def generate_api_key(request: Request):
    """Step 3: Auto-generate container API key, store encrypted, show once."""
    api_key = secrets.token_urlsafe(32)
    await store_secret("container_api_key", api_key)
    await SetupStateRepository.set_step_completed("container_api_key")
    steps = await SetupStateRepository.get_all_steps()
    step_map = {s["step"]: s["completed"] for s in steps}
    return templates.TemplateResponse(
        request,
        "step3.html",
        context={
            "step_num": 3,
            "total_steps": len(STEP_ORDER),
            "steps": step_map,
            "api_key": api_key,
            "generated": True,
        },
    )


@router.post(
    "/step/4",
    response_class=HTMLResponse,
    dependencies=[
        Depends(verify_csrf),
        Depends(body_size_limit(1 * 1024 * 1024)),
        Depends(require_admin_or_setup_open),
        Depends(rate_limit_setup),
    ],
)
async def save_llm_keys(
    request: Request,
    openrouter_key: str = Form(""),
    groq_key: str = Form(""),
    ollama_url: str = Form(""),
    custom_openai_name: str = Form(""),
    custom_openai_url: str = Form(""),
    custom_openai_key: str = Form(""),
):
    """Step 4: Save LLM provider keys (Fernet-encrypted).

    The custom OpenAI-compatible provider is saved like
    ``PUT /api/admin/llm-providers/custom-openai``: filling any of its fields
    requires a valid base URL; a blank name defaults to "Custom Provider".
    """
    custom_name = custom_openai_name.strip()
    custom_url = custom_openai_url.strip()
    custom_key = custom_openai_key.strip()
    custom_config: dict[str, str] | None = None
    if custom_name or custom_url or custom_key:
        # Validate before any write so a rejected submit leaves nothing half-saved.
        try:
            custom_config = {
                "name": normalize_custom_openai_name(custom_name or _CUSTOM_PROVIDER_DEFAULT_NAME),
                "base_url": normalize_custom_openai_base_url(custom_url),
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Custom OpenAI provider: {exc}") from exc

    if openrouter_key:
        await store_secret("openrouter_api_key", openrouter_key)
    if groq_key:
        await store_secret("groq_api_key", groq_key)
    if ollama_url:
        await SettingsRepository.set("ollama_base_url", ollama_url, "string", "llm", "Ollama API URL")
    if custom_config is not None:
        await save_custom_openai_config(**custom_config, api_key=custom_key)
    await SetupStateRepository.set_step_completed("llm_providers")
    return RedirectResponse(url="/setup/step/5", status_code=303)


@router.post(
    "/step/5",
    response_class=HTMLResponse,
    dependencies=[
        Depends(verify_csrf),
        Depends(body_size_limit(1 * 1024 * 1024)),
        Depends(require_admin_or_setup_open),
        Depends(rate_limit_setup),
    ],
)
async def complete_setup(request: Request):
    """Step 5: Trigger post-setup initialization and only mark setup complete on success."""
    logger.info("Setup wizard completed, triggering post-setup initialization")
    steps = await SetupStateRepository.get_all_steps()
    step_map = {s["step"]: s["completed"] for s in steps}
    try:
        await ensure_setup_runtime_initialized(request.app)
    except Exception:
        logger.exception("Post-setup runtime initialization failed")
        return templates.TemplateResponse(
            request,
            "step5.html",
            context={
                "step_num": 5,
                "total_steps": len(STEP_ORDER),
                "steps": {k: v for k, v in step_map.items() if k != "review_complete"},
                "csrf_token": ensure_csrf_token(request),
                "error": "Runtime initialization failed. Check the container logs and try again.",
            },
            status_code=500,
        )

    await SetupStateRepository.set_step_completed("review_complete")
    return RedirectResponse(url="/dashboard/", status_code=303)


@router.post(
    "/test/ha",
    dependencies=[
        Depends(verify_csrf),
        Depends(body_size_limit(1 * 1024 * 1024)),
        Depends(require_admin_or_setup_open),
        Depends(rate_limit_setup),
    ],
)
async def test_ha_endpoint(ha_url: str = Form(...), ha_token: str = Form(...)):
    """Test HA connection with provided URL and token."""
    success = await test_ha_connection(ha_url, ha_token)
    if success:
        return HTMLResponse('<div class="alert alert-success">Connected to Home Assistant!</div>')
    return HTMLResponse('<div class="alert alert-error">Failed to connect to Home Assistant.</div>')


@router.post(
    "/test/llm",
    dependencies=[
        Depends(verify_csrf),
        Depends(body_size_limit(1 * 1024 * 1024)),
        Depends(require_admin_or_setup_open),
        Depends(rate_limit_setup),
    ],
)
async def test_llm_endpoint(
    provider: str = Form(...),
    api_key: str = Form(...),
    base_url: str = Form(""),
):
    """Test an LLM provider's credentials by listing its models (never raises)."""
    try:
        if provider not in _WIZARD_LLM_PROVIDERS:
            return HTMLResponse(f'<div class="alert alert-error">Unknown provider: {html.escape(provider)}</div>')

        key: str | None = api_key
        url: str | None = None
        if provider == "ollama":
            key = None
            url = base_url
        elif provider == "custom_openai":
            url = base_url
        result = await asyncio.wait_for(
            model_catalog.list_provider_models(provider, api_key=key, base_url=url, use_cache=False),
            timeout=_LLM_TEST_TIMEOUT_S,
        )
        if result["status"] == "ok":
            count = len(result["models"])
            return HTMLResponse(
                f'<div class="alert alert-success">Connected to {html.escape(provider)}! '
                f"{count} models available.</div>"
            )
        detail = result["detail"]
        if result["error_kind"] == model_catalog.ERROR_UNSUPPORTED:
            detail += " Finish setup and test it with a model name under Settings > LLM in the dashboard."
        return HTMLResponse(f'<div class="alert alert-error">{html.escape(provider)}: {html.escape(detail)}</div>')
    except Exception as exc:
        if _is_timeout_error(exc):
            logger.warning("LLM provider test timed out in setup wizard for %s", provider)
            return HTMLResponse(
                f'<div class="alert alert-error">Provider test timed out after {_LLM_TEST_TIMEOUT_S:.0f}s.</div>'
            )
        logger.warning("LLM provider test failed in setup wizard", exc_info=True)
        return HTMLResponse('<div class="alert alert-error">Provider test failed. Check server logs.</div>')
