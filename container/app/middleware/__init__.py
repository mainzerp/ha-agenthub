"""Middleware and FastAPI dependency wrappers for authentication."""

from app.middleware.auth import apply_exception_handlers

__all__ = ["apply_exception_handlers"]
