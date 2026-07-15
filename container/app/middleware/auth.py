import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import RedirectResponse as StarletteRedirect

from app.db.repository import SetupStateRepository

logger = logging.getLogger(__name__)


async def _safe_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    method = getattr(request, "method", "WS") if hasattr(request, "method") else "WS"
    path = getattr(getattr(request, "url", None), "path", "unknown")
    logger.warning("HTTP %d: %s %s", exc.status_code, method, path)
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=getattr(exc, "headers", None)
    )


async def _safe_generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def apply_auth_dependencies(app: FastAPI) -> None:
    app.add_exception_handler(Exception, _safe_http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _safe_generic_exception_handler)


class SetupRedirectMiddleware:
    """Pure ASGI middleware that redirects to /setup/ if setup is not complete.

    Implemented as a pure ASGI app (not BaseHTTPMiddleware) so it does not
    buffer streaming responses (SSE/WS); first byte from downstream flushes
    immediately.
    """

    ALLOWED_PREFIXES = ("/setup", "/api/health", "/healthz", "/readyz", "/static", "/dashboard/static")

    def __init__(self, app) -> None:
        self.app = app
        self._setup_complete: bool | None = None

    def invalidate_setup_cache(self) -> None:
        self._setup_complete = None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Cache the completion state -- once complete, never check again
        if self._setup_complete is None or not self._setup_complete:
            self._setup_complete = await SetupStateRepository.is_complete()

        if not self._setup_complete:
            path = scope.get("path", "")
            if not any(path.startswith(p) for p in self.ALLOWED_PREFIXES):
                response = StarletteRedirect(url="/setup/", status_code=302)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
