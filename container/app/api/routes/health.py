"""Health check endpoint."""

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health():
    """Return container health status."""
    return {
        "status": "ok",
        "log_level": logging.getLevelName(logger.getEffectiveLevel()).lower(),
    }
