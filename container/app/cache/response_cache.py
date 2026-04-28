"""Compatibility shim for legacy response-cache imports."""

from app.cache.action_cache import ActionCache as ResponseCache

__all__ = ["ResponseCache"]
