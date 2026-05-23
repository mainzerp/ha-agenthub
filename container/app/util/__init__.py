"""Utility helpers."""

from typing import NoReturn

from fastapi import HTTPException


def raise_api_error(detail: str, status_code: int = 400) -> NoReturn:
    """Raise a standardized HTTPException.

    Use this instead of returning JSONResponse with error content so
    that all error responses flow through the same exception handler.
    """
    raise HTTPException(status_code=status_code, detail=detail)
