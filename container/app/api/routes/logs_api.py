"""Remote logs admin API endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.security.auth import require_admin_session
from app.util.log_buffer import get_log_buffer

router = APIRouter(
    prefix="/api/admin/logs",
    tags=["admin-logs"],
    dependencies=[Depends(require_admin_session)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LogEntryResponse(BaseModel):
    timestamp: str
    level: str
    name: str
    message: str
    module: str
    funcName: str  # noqa: N815
    lineno: int


class LogsListResponse(BaseModel):
    entries: list[dict[str, Any]]
    total: int


class LogLevelUpdateRequest(BaseModel):
    logger_name: str
    level: str = Field(..., pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


class LogLevelsResponse(BaseModel):
    root_level: str
    loggers: dict[str, str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


# Known noisy third-party libraries whose explicit levels are not
# user-configurable and clutter the UI.
_NOISE_LOGGER_PREFIXES = (
    "apscheduler",
    "c10d",
    "httpx",
    "huggingface_hub",
    "numba",
    "strobelight",
    "torch",
    "transformers",
    "triton",
)


def _get_all_logger_levels() -> dict[str, str]:
    """Collect explicit levels for relevant loggers.

    Filters out noisy third-party libraries that set explicit levels at
    import time but are never tuned by the admin.
    """
    loggers: dict[str, str] = {}
    root = logging.getLogger()
    for name in sorted(logging.root.manager.loggerDict):
        if any(name.startswith(p) or name.startswith(p + ".") for p in _NOISE_LOGGER_PREFIXES):
            continue
        logger = logging.getLogger(name)
        if logger.level != logging.NOTSET:
            loggers[name] = logging.getLevelName(logger.level)
    # Always include root if it has an explicit level
    if root.level != logging.NOTSET:
        loggers["root"] = logging.getLevelName(root.level)
    return loggers


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_logs(
    level: str | None = Query(None),
    logger: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None),
) -> LogsListResponse:
    """List recent log entries with optional filtering and pagination."""
    buf = get_log_buffer()
    if buf is None:
        return LogsListResponse(entries=[], total=0)

    if level is not None and level.upper() not in _VALID_LEVELS:
        raise HTTPException(status_code=400, detail=f"Invalid level: {level}")

    result = buf.get_entries(
        level=level.upper() if level else None,
        logger_name=logger,
        since=since,
        limit=limit,
        offset=offset,
        search=search,
    )
    return LogsListResponse(entries=result["entries"], total=result["total"])


@router.get("/levels")
async def get_log_levels() -> LogLevelsResponse:
    """Return the current root log level and all explicitly-set logger levels."""
    root = logging.getLogger()
    root_level = logging.getLevelName(root.level)
    if root_level == "NOTSET":
        root_level = "INFO"
    loggers = _get_all_logger_levels()
    # Ensure root is represented by root_level, not overwritten
    loggers["root"] = root_level
    return LogLevelsResponse(root_level=root_level, loggers=loggers)


@router.post("/levels")
async def update_log_level(payload: LogLevelUpdateRequest) -> dict[str, str]:
    """Update a logger's level at runtime."""
    level_name = payload.level.upper()
    if level_name not in _VALID_LEVELS:
        raise HTTPException(status_code=400, detail=f"Invalid level: {payload.level}")

    level_value = getattr(logging, level_name)
    logger = logging.getLogger(payload.logger_name)
    logger.setLevel(level_value)
    return {"status": "ok", "logger_name": payload.logger_name, "level": level_name}
