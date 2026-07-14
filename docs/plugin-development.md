# Plugin Development Guide

This document covers how to develop plugins for HA-AgentHub.

## Overview

Plugins extend HA-AgentHub without modifying core code. They can inspect
registered agents through `ctx.agent_registry`, dispatch work through
the A2A dispatcher, subscribe to events, read/write settings,
interact with MCP servers, and add API routes.

Plugins live as individual `.py` files in the `container/plugins/` directory.
They are discovered and loaded automatically at container startup.

See also [`container/plugins/README.md`](../container/plugins/README.md) for
the quickstart kept next to the plugins directory itself.

## Getting Started

### 1. Create a Plugin File

Create a new `.py` file in `container/plugins/`:

```
container/plugins/my_plugin.py
```

### 2. Subclass BasePlugin

Every plugin must subclass `BasePlugin` and implement the `name` and `version`
properties:

```python
from app.plugins.base import BasePlugin, PluginContext


class MyPlugin(BasePlugin):

    @property
    def name(self) -> str:
        return "my-plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "A short description of what this plugin does"

    async def configure(self, ctx: PluginContext) -> None:
        # Read settings, validate configuration
        pass

    async def startup(self, ctx: PluginContext) -> None:
        # Initialize resources (DB connections, external APIs)
        pass

    async def ready(self, ctx: PluginContext) -> None:
        # System is fully initialized; subscribe to events or dispatch work
        pass

    async def shutdown(self) -> None:
        # Clean up resources
        pass
```

### 3. Restart the Container

Plugins are discovered at container startup. After creating or modifying a
plugin file, restart the container for changes to take effect.

Plugins can also be enabled/disabled from the admin dashboard without
modifying files.

## Lifecycle Hooks

Hooks are called in this order during startup:

| Phase       | When                                    | Use For                              |
|-------------|-----------------------------------------|--------------------------------------|
| `configure` | After plugin discovery                  | Reading settings, validating config  |
| `startup`   | After all plugins are configured        | Initializing resources               |
| `ready`     | After all agents registered, system up  | Event subscriptions, routes, orchestrator-bound work |

On shutdown:

| Phase      | When                   | Use For                  |
|------------|------------------------|--------------------------|
| `shutdown` | Container shutting down | Cleanup, close connections|

All hooks are optional. Only implement the ones you need.

## PluginContext

The `PluginContext` object is passed to `configure`, `startup`, and `ready`
hooks. It provides access to:

| Attribute              | Type                  | Description                                   |
|------------------------|-----------------------|-----------------------------------------------|
| `agent_registry`       | `AgentRegistry`       | Read-only discovery of registered agents      |
| `mcp_registry`         | `MCPServerRegistry`   | Access MCP server connections                 |
| `settings`             | `SettingsRepository` (class) | Read/write system settings. The attribute holds the SettingsRepository class; methods such as get_value are classmethods, so await ctx.settings.get_value(...) works as documented. |
| `event_bus`            | `EventBus`            | Subscribe/publish plugin events               |

> **Note:** `event_bus` is initialized to `None` in `PluginContext.__init__` and assigned by the `PluginLoader` after construction. It is always available by the time the `configure`, `startup`, or `ready` hooks run.
>
> To add API routes, use the `add_api_route(path, endpoint, **kwargs)` or
`include_router(router, **kwargs)` methods on `PluginContext`. Direct access
to the FastAPI application object is not supported. Legacy direct registry
access is also no longer supported and now raises `AttributeError`.

### Discovering Agents And Dispatching Through The Orchestrator

```python
from app.plugins.base import BasePlugin, PluginContext


class WeatherPlugin(BasePlugin):
    @property
    def name(self) -> str:
        return "weather"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def ready(self, ctx: PluginContext) -> None:
        agents = await ctx.agent_registry.list_agents()
        if not any(card.agent_id == "general-agent" for card in agents):
            return

        async def on_weather_request(data: dict) -> None:
            # Plugin-originated work can be dispatched back through the A2A
            # dispatcher (ctx.dispatcher). Construct a proper AgentTask and
            # dispatch to the desired agent, or publish an event for another
            # component to handle.
            pass

        ctx.event_bus.subscribe("weather.request", on_weather_request)
```

Use `ctx.agent_registry` when you need to inspect what the runtime has
registered, and use the A2A dispatcher (`ctx.dispatcher`) when plugin-
originated work should re-enter the normal A2A/orchestrator flow.

### Reading/Writing Settings

```python
async def configure(self, ctx: PluginContext) -> None:
    api_key = await ctx.settings.get_value("my_plugin.api_key")
    if not api_key:
        await ctx.settings.set(
            "my_plugin.api_key", "", value_type="string",
            category="plugins", description="API key for my plugin"
        )
```

## Error Isolation

Each plugin runs in isolation:

- If a plugin raises an exception during any lifecycle phase, it is logged
  and the system continues loading other plugins.
- One plugin failure does not crash the container or prevent other plugins
  from running.
- If a plugin fails during `configure` or `startup`, the system still
  proceeds to the `ready` phase for other plugins.

## Event Bus

Plugins can communicate via the `EventBus` available on the plugin loader:

```python
async def ready(self, ctx: PluginContext) -> None:
    # Access event bus directly from the plugin context
    event_bus = ctx.event_bus

    async def on_custom_event(data):
        print(f"Received: {data}")

    event_bus.subscribe("my.custom.event", on_custom_event)

    # Later, publish from anywhere
    await event_bus.publish("my.custom.event", {"key": "value"})
```

Event handlers are also error-isolated: one handler failing does not affect
other handlers subscribed to the same event.

## Trust Model

Plugins are **fully trusted code** running in the same Python process as the
HA-AgentHub container. There is no sandbox, filesystem isolation, or resource
limiting.

What this means in practice:

- A plugin can import any Python module, read any file on disk, and make
  arbitrary network calls.
- The `PluginContext` API surface is a **convention** for clean integration,
  not a security boundary.
- Plugin files should be reviewed before deployment, just like any other code
  you deploy into your container.
- The admin dashboard allows enabling and disabling plugins. A disabled plugin
  is never imported or instantiated.

If you are distributing plugins to others, document the permissions your
plugin requires and any external services it contacts.

## Database Tracking

Plugin metadata is stored in the `plugins` table:

| Column      | Description                          |
|-------------|--------------------------------------|
| `name`      | Unique plugin name                   |
| `file_path` | Path to the plugin `.py` file        |
| `enabled`   | Whether the plugin should be loaded  |
| `version`   | Plugin version string                |
| `description` | Plugin description                 |
| `loaded_at` | Timestamp of last load               |

The admin dashboard shows all discovered plugins and allows
enabling/disabling them.

## File Naming

- Plugin files must be `.py` files in `container/plugins/`
- Files starting with `_` (underscore) are ignored
- Each file should contain exactly one `BasePlugin` subclass
- The plugin `name` property is used as the identifier (not the filename)

## Example: Minimal Plugin

```python
"""Minimal plugin that logs at each lifecycle phase."""

import logging
from app.plugins.base import BasePlugin, PluginContext

logger = logging.getLogger(__name__)


class MinimalPlugin(BasePlugin):

    @property
    def name(self) -> str:
        return "minimal-example"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "A minimal example plugin"

    async def configure(self, ctx: PluginContext) -> None:
        logger.info("MinimalPlugin: configure phase")

    async def startup(self, ctx: PluginContext) -> None:
        logger.info("MinimalPlugin: startup phase")

    async def ready(self, ctx: PluginContext) -> None:
        logger.info("MinimalPlugin: ready phase -- system is up")

    async def shutdown(self) -> None:
        logger.info("MinimalPlugin: shutdown phase")
```
