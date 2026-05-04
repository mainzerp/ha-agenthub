"""Shared static asset helpers for dashboard and setup."""

from app import __version__ as _app_version

# Build counter: bump this whenever static files change significantly.
_STATIC_BUILD = "13"

static_version = f"{_app_version}.{_STATIC_BUILD}"


def static_url(request, path: str) -> str:
    """Return a versioned static URL path."""
    from app.security.auth import _rooted_url

    sep = "&" if "?" in path else "?"
    return _rooted_url(request, path) + f"{sep}v={static_version}"
