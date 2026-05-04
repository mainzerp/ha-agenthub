import hmac
import logging
import secrets

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings as _app_settings
from app.db.repository import AdminAccountRepository
from app.security.encryption import (
    get_session_signing_key,
    retrieve_secret,
)
from app.security.hashing import verify_password

logger = logging.getLogger(__name__)

API_KEY_HEADER = "Authorization"
API_KEY_SECRET_NAME = "container_api_key"
SESSION_COOKIE_NAME = "agent_assist_session"
SESSION_MAX_AGE = 86400
CSRF_COOKIE_NAME = "agent_assist_csrf"
CSRF_FIELD_NAME = "csrf_token"
CSRF_MAX_AGE = 86400

_session_serializer: URLSafeTimedSerializer | None = None


def _rooted_url(request: Request, path: str) -> str:
    root_path = (request.scope.get("root_path") or "").rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    if not root_path:
        return normalized_path
    return f"{root_path}{normalized_path}"


def _login_url(request: Request) -> str:
    return _rooted_url(request, "/dashboard/login")


def _get_session_serializer() -> URLSafeTimedSerializer:
    global _session_serializer
    if _session_serializer is None:
        signing_key = get_session_signing_key().hex()
        _session_serializer = URLSafeTimedSerializer(signing_key)
    return _session_serializer


async def require_api_key(request: Request) -> str:
    auth_header = request.headers.get(API_KEY_HEADER)
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    provided_key = auth_header[7:]
    stored_key = await retrieve_secret(API_KEY_SECRET_NAME)
    if stored_key is None or not hmac.compare_digest(provided_key, stored_key):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return provided_key


async def require_api_key_ws(websocket: WebSocket) -> str:
    """Authenticate a WebSocket connection.

    Only the ``Authorization: Bearer <token>`` header is accepted. The
    deprecated ``?token=`` query-string fallback was removed in 0.17.0
    (SEC-2): tokens placed on the URL leak into proxy/access logs and
    browser history.
    """
    token = None
    auth_header = websocket.headers.get(API_KEY_HEADER)
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        await websocket.close(code=4001, reason="Unauthorized")
        raise HTTPException(status_code=401, detail="Unauthorized")
    stored_key = await retrieve_secret(API_KEY_SECRET_NAME)
    if stored_key is None or not hmac.compare_digest(token, stored_key):
        await websocket.close(code=4001, reason="Unauthorized")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return token


async def require_admin_session(request: Request) -> dict:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=401, detail="Session expired")
    try:
        data = _get_session_serializer().loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Session expired") from None
    return data


async def require_admin_or_setup_open(request: Request) -> dict | None:
    """Allow anonymous access while the setup wizard is incomplete.

    Once the wizard has been completed every POST to ``/setup/*`` requires
    an authenticated admin session. Returns the session payload when
    authenticated, or ``None`` while setup is still in progress.
    """
    from app.db.repository import SetupStateRepository

    if not await SetupStateRepository.is_complete():
        return None
    return await require_admin_session(request)


async def require_admin_session_redirect(request: Request) -> dict:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    is_htmx = request.headers.get("HX-Request") == "true"
    if not cookie:
        if is_htmx:
            return _htmx_redirect_response(request)
        raise HTTPException(
            status_code=303,
            headers={"Location": _login_url(request)},
            detail="Session expired",
        )
    try:
        data = _get_session_serializer().loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        if is_htmx:
            return _htmx_redirect_response(request)
        raise HTTPException(
            status_code=303,
            headers={"Location": _login_url(request)},
            detail="Session expired",
        ) from None
    return data


def _htmx_redirect_response(request: Request):
    """Return a 401 with HX-Redirect header so HTMX does a full page redirect."""
    raise HTTPException(
        status_code=401,
        headers={"HX-Redirect": _login_url(request)},
        detail="Session expired",
    )


async def authenticate_admin(username: str, password: str) -> dict | None:
    account = await AdminAccountRepository.get(username)
    if account is None:
        return None
    if not verify_password(password, account["password_hash"]):
        return None
    await AdminAccountRepository.update_last_login(username)
    return {"username": username}


def create_session_cookie(session_data: dict) -> str:
    return _get_session_serializer().dumps(session_data)


# ---------------------------------------------------------------------------
# CSRF protection (SEC-1)
# ---------------------------------------------------------------------------


def ensure_csrf_token(request: Request) -> str:
    """Return the existing CSRF token from the request cookie or mint a new one.

    Routes that render forms call this helper, place the returned value in
    the template context as ``csrf_token``, and call ``set_csrf_cookie`` on
    the outgoing response.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and request.cookies.get(SESSION_COOKIE_NAME):
        return secrets.token_urlsafe(32)
    if existing:
        return existing
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    """Persist the CSRF token in a cookie scoped to the current host.

    The cookie is intentionally not ``HttpOnly`` so the rendered template can
    embed the value in a hidden form field (which is then echoed back on
    POST). ``SameSite=Strict`` blocks cross-site form submissions even if a
    user follows an attacker-controlled link.
    """
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=CSRF_MAX_AGE,
        httponly=False,
        samesite="strict",
        secure=_app_settings.cookie_secure is True,
        path="/",
    )


def attach_csrf(request: Request, response: Response, token: str | None = None) -> str:
    """Ensure a response carries the current CSRF token cookie."""
    csrf_token = token or ensure_csrf_token(request)
    set_csrf_cookie(response, csrf_token)
    return csrf_token


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency: enforce CSRF token match on form POSTs.

    Reads the CSRF cookie set by ``set_csrf_cookie`` and compares it with
    the ``csrf_token`` form field using a constant-time comparison. Raises
    HTTP 401 on any mismatch (missing cookie, missing form field, or value
    mismatch). Only used for HTML form endpoints; JSON APIs continue to rely
    on the API key.
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token:
        raise HTTPException(status_code=401, detail="CSRF token missing")
    try:
        form = await request.form()
    except Exception:
        raise HTTPException(status_code=401, detail="CSRF token missing") from None
    form_token = form.get(CSRF_FIELD_NAME)
    if not form_token or not hmac.compare_digest(str(cookie_token), str(form_token)):
        raise HTTPException(status_code=401, detail="CSRF token invalid")
