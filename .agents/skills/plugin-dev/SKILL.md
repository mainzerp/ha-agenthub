---
name: plugin-dev
description: Create a plugin for agent-assist following the BasePlugin pattern. Use when adding optional, hot-loadable functionality that extends agents, registers MCP servers, adds API routes, or reacts to system events.
---

# Creating an agent-assist Plugin

Plugins are single `.py` files placed in the plugin directory (configured at runtime). The `PluginLoader` discovers them automatically and runs them through a 4-phase lifecycle.

## Minimal plugin skeleton

```python
from app.plugins.base import BasePlugin, PluginContext


class MyPlugin(BasePlugin):
    @property
    def name(self) -> str:
        return "my-plugin"          # kebab-case, must be unique

    @property
    def version(self) -> str:
        return "1.0.0"             # semver

    @property
    def description(self) -> str:
        return "One-line description shown in the plugin dashboard."

    # --- Lifecycle hooks (all optional) ---

    async def configure(self, ctx: PluginContext) -> None:
        """Phase 1: Read settings from DB. No heavy I/O yet."""
        pass

    async def startup(self, ctx: PluginContext) -> None:
        """Phase 2: Initialize resources (HTTP clients, DB connections)."""
        pass

    async def ready(self, ctx: PluginContext) -> None:
        """Phase 3: All agents are registered. Safe to call ctx.agent_catalog."""
        pass

    async def shutdown(self) -> None:
        """Phase 4: Clean up resources. No ctx — system is tearing down."""
        pass
```

## PluginContext API

`ctx` is available in `configure`, `startup`, and `ready`. Use only its public interface:

```python
# Register an agent programmatically
ctx.agent_catalog.register_agent(my_agent)

# Add an API route
ctx.add_api_route("/my-plugin/status", status_handler, methods=["GET"])

# Include an APIRouter
from fastapi import APIRouter
router = APIRouter(prefix="/my-plugin", tags=["my-plugin"])
ctx.include_router(router)

# Add an MCP server
await ctx.mcp_registry.add_server(
    name="my-mcp-server",
    transport="stdio",
    command_or_url="python3 /path/to/server.py",
)

# Read a persisted setting
value = await ctx.settings.get("my-plugin.some-key", default="fallback")

# Subscribe to inter-plugin events (available after PluginLoader sets event_bus)
ctx.event_bus.subscribe("some.event", my_async_handler)

# Publish an event
await ctx.event_bus.publish("my-plugin.ready", data={"status": "ok"})
```

**Never** access `ctx.app` directly (removed) or `ctx.agent_registry` (removed — use `ctx.agent_catalog`).

## Lifecycle phase rules

| Phase | What to do | What NOT to do |
|-------|-----------|----------------|
| `configure` | Read DB settings, set instance variables | Start background tasks, call HA |
| `startup` | Connect to external services, start background tasks | Call `ctx.agent_catalog` (agents not yet registered) |
| `ready` | Wire up agent catalog references, publish "ready" events | Register new agents (too late) |
| `shutdown` | Cancel tasks, close connections | Access `ctx` (not passed) |

Each hook has a **30-second timeout**. If a hook exceeds it, the loader logs a warning and continues — it does not crash the system.

## File naming

The filename becomes the candidate plugin name (`my_plugin.py` → candidate name `my-plugin`). The `name` property overrides this. Use only alphanumerics and hyphens in `name`.

Files starting with `_` are ignored by the loader.

## Hot enable/disable

Plugins can be enabled or disabled at runtime through the dashboard without restarting the process. The loader calls the full lifecycle on enable (`configure → startup → ready`) and `shutdown` on disable.

## EventBus patterns

```python
# In plugin A — publish
await ctx.event_bus.publish("calendar.event_added", data={"event_id": "123"})

# In plugin B — subscribe in startup()
async def on_calendar_event(data):
    ...

async def startup(self, ctx: PluginContext) -> None:
    ctx.event_bus.subscribe("calendar.event_added", on_calendar_event)
```

Handlers must be `async def`. Failures in one handler do not affect others.
