"""Shared ``homeassistant`` stub installer for container tests that import the bridge.

Several container tests import ``custom_components.ha_agenthub`` (the HA bridge) without
Home Assistant installed. Importing the package runs ``custom_components/ha_agenthub/__init__.py``,
which pulls in every HA module the bridge touches at import time (config entries, exceptions,
config validation, aiohttp client, websocket API, http, frontend, loader). Python's import system
needs each dotted parent in ``sys.modules`` too, so a partial stub tree fails with
``ModuleNotFoundError``.

``install_ha_stubs`` registers the full set (kept in sync with
``custom_components/tests/conftest.py::_mock_homeassistant_deps``) and returns the names it
created so the caller can pop them again on teardown. Modules that already exist in
``sys.modules`` are left alone; tests keep layering their own specific attributes on top.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass
from unittest.mock import MagicMock

# Every module the bridge package imports at import time (directly or via a parent package).
_BRIDGE_IMPORT_MODULES: tuple[str, ...] = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.frontend",
    "homeassistant.components.http",
    "homeassistant.components.websocket_api",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "homeassistant.loader",
)


@dataclass
class _StubStaticPathConfig:
    url_path: str
    path: str
    cache_headers: bool = True


def _websocket_command(schema):
    """Keep the handler callable as-is and record the command schema."""

    def decorator(func):
        func._ws_schema = schema
        return func

    return decorator


def _async_response(func):
    return func


def install_ha_stubs(extra_modules: Iterable[str] = ()) -> list[str]:
    """Register the bridge's import-time ``homeassistant`` stubs; return the names created.

    ``extra_modules`` lists additional modules a specific test needs (e.g. the conversation
    platform or helpers). Missing ones become plain ``MagicMock`` modules. The caller pops the
    returned names and any ``custom_components*`` modules on teardown.
    """
    created: list[str] = []
    for name in (*_BRIDGE_IMPORT_MODULES, *extra_modules):
        if name not in sys.modules:
            sys.modules[name] = MagicMock()
            created.append(name)

    # Real callables where the bridge uses them as decorators or at import time. Attribute
    # assignment is unconditional so a stub leaked by another test still gets them.
    ws_api = sys.modules["homeassistant.components.websocket_api"]
    ws_api.websocket_command = _websocket_command
    ws_api.async_response = _async_response
    sys.modules["homeassistant.components.http"].StaticPathConfig = _StubStaticPathConfig
    sys.modules["homeassistant.components.frontend"].add_extra_js_url = MagicMock()
    sys.modules["homeassistant.loader"].async_get_integration = MagicMock()
    sys.modules["homeassistant.helpers.config_validation"].config_entry_only_config_schema = lambda domain: MagicMock()
    sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = MagicMock()
    exceptions = sys.modules["homeassistant.exceptions"]
    exceptions.HomeAssistantError = Exception
    for exc_name in ("ConfigEntryError", "ConfigEntryAuthFailed", "ConfigEntryNotReady"):
        setattr(exceptions, exc_name, type(exc_name, (Exception,), {}))

    # Wire parent attributes so `from homeassistant.components import websocket_api` and
    # `from homeassistant.helpers import config_validation as cv` resolve to the stubs above.
    components = sys.modules["homeassistant.components"]
    components.websocket_api = ws_api
    components.http = sys.modules["homeassistant.components.http"]
    components.frontend = sys.modules["homeassistant.components.frontend"]
    helpers = sys.modules["homeassistant.helpers"]
    helpers.config_validation = sys.modules["homeassistant.helpers.config_validation"]
    helpers.aiohttp_client = sys.modules["homeassistant.helpers.aiohttp_client"]
    return created
