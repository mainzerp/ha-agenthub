"""Health check endpoint."""

import logging

from fastapi import APIRouter, Depends

from app.security.auth import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"], dependencies=[Depends(require_api_key)])


@router.get("/health")
async def health():
    """Return container health status."""
    return {
        "status": "ok",
        "log_level": logging.getLevelName(logger.getEffectiveLevel()).lower(),
    }
