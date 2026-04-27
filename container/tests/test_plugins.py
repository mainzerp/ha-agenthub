"""Tests for app.plugins -- base, hooks (EventBus), and loader."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.a2a.registry import AgentRegistry
from app.a2a.orchestrator_gateway import AgentCatalog, OrchestratorGateway
from app.agents.custom_loader import CustomAgentLoader
from app.ha_client.rest import (
    HARestClient,
    allow_internal_ha_service_calls,
    get_direct_ha_write_warning_count,
    reset_direct_ha_write_warning_count,
)
from app.plugins.base import BasePlugin, PluginContext
from app.plugins.hooks import EventBus, LifecyclePhase
from app.plugins.loader import PluginLoader

# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class TestEventBus:
    async def test_subscribe_and_publish(self):
        bus = EventBus()
        handler = AsyncMock()
        bus.subscribe("test_event", handler)
        await bus.publish("test_event", {"key": "val"})
        handler.assert_awaited_once_with({"key": "val"})

    async def test_publish_no_subscribers(self):
        bus = EventBus()
        # Should not raise
        await bus.publish("nonexistent_event", None)

    async def test_multiple_handlers_called(self):
        bus = EventBus()
        h1 = AsyncMock()
        h2 = AsyncMock()
        bus.subscribe("event", h1)
        bus.subscribe("event", h2)
        await bus.publish("event", "data")
        h1.assert_awaited_once_with("data")
        h2.assert_awaited_once_with("data")

    async def test_handler_failure_does_not_block_others(self):
        bus = EventBus()
        failing = AsyncMock(side_effect=Exception("boom"))
        ok = AsyncMock()
        bus.subscribe("event", failing)
        bus.subscribe("event", ok)
        await bus.publish("event", None)
        ok.assert_awaited_once()

    def test_clear_removes_all_handlers(self):
        bus = EventBus()
        bus.subscribe("a", AsyncMock())
        bus.subscribe("b", AsyncMock())
        bus.clear()
        assert len(bus._handlers) == 0


# ---------------------------------------------------------------------------
# LifecyclePhase
# ---------------------------------------------------------------------------


class TestLifecyclePhase:
    def test_lifecycle_phases_exist(self):
        assert LifecyclePhase.CONFIGURE.value == "configure"
        assert LifecyclePhase.STARTUP.value == "startup"
        assert LifecyclePhase.READY.value == "ready"
        assert LifecyclePhase.SHUTDOWN.value == "shutdown"


# ---------------------------------------------------------------------------
# BasePlugin
# ---------------------------------------------------------------------------


class TestBasePlugin:
    def test_base_plugin_is_abstract(self):
        with pytest.raises(TypeError):
            BasePlugin()  # type: ignore[abstract]

    def test_concrete_plugin_instantiable(self):
        class MyPlugin(BasePlugin):
            @property
            def name(self) -> str:
                return "my-plugin"

            @property
            def version(self) -> str:
                return "1.0.0"

        p = MyPlugin()
        assert p.name == "my-plugin"
        assert p.version == "1.0.0"
        assert p.description == ""

    async def test_default_lifecycle_hooks_are_noops(self):
        class NoOpPlugin(BasePlugin):
            @property
            def name(self) -> str:
                return "noop"

            @property
            def version(self) -> str:
                return "0.1.0"

        p = NoOpPlugin()
        ctx = MagicMock(spec=PluginContext)
        # These should not raise
        await p.configure(ctx)
        await p.startup(ctx)
        await p.ready(ctx)
        await p.shutdown()


# ---------------------------------------------------------------------------
# PluginContext
# ---------------------------------------------------------------------------


class TestPluginContext:
    def test_context_provides_registries(self):
        agent_catalog = MagicMock(spec=AgentCatalog)
        gateway = MagicMock(spec=OrchestratorGateway)
        mcp_reg = MagicMock()
        settings = MagicMock()
        app = MagicMock()
        ctx = PluginContext(
            agent_catalog=agent_catalog,
            orchestrator_gateway=gateway,
            mcp_registry=mcp_reg,
            settings_repo=settings,
            app=app,
        )
        assert ctx.agent_catalog is agent_catalog
        assert ctx.orchestrator_gateway is gateway
        assert ctx.mcp_registry is mcp_reg
        assert ctx.settings is settings
        assert not hasattr(ctx, "_app")

    def test_ctx_agent_registry_raises_attribute_error(self):
        app = MagicMock()
        ctx = PluginContext(
            agent_catalog=MagicMock(spec=AgentCatalog),
            orchestrator_gateway=MagicMock(spec=OrchestratorGateway),
            mcp_registry=MagicMock(),
            settings_repo=MagicMock(),
            app=app,
        )
        with pytest.raises(AttributeError, match="agent_registry has been removed"):
            _ = ctx.agent_registry

    def test_ctx_app_raises_attribute_error(self):
        """PluginContext.app must raise AttributeError (escape hatch removed)."""
        app = MagicMock()
        ctx = PluginContext(
            agent_catalog=MagicMock(spec=AgentCatalog),
            orchestrator_gateway=MagicMock(spec=OrchestratorGateway),
            mcp_registry=MagicMock(),
            settings_repo=MagicMock(),
            app=app,
        )
        with pytest.raises(AttributeError, match="has been removed"):
            _ = ctx.app

    def test_ctx_add_api_route_delegates_to_app(self):
        """add_api_route should still work after app property removal."""
        app = MagicMock()
        ctx = PluginContext(
            agent_catalog=MagicMock(spec=AgentCatalog),
            orchestrator_gateway=MagicMock(spec=OrchestratorGateway),
            mcp_registry=MagicMock(),
            settings_repo=MagicMock(),
            app=app,
        )
        ctx.add_api_route("/test", lambda: None, methods=["GET"])
        app.add_api_route.assert_called_once()

    def test_ctx_include_router_delegates_to_app(self):
        """include_router should still work after app property removal."""
        app = MagicMock()
        router = MagicMock()
        ctx = PluginContext(
            agent_catalog=MagicMock(spec=AgentCatalog),
            orchestrator_gateway=MagicMock(spec=OrchestratorGateway),
            mcp_registry=MagicMock(),
            settings_repo=MagicMock(),
            app=app,
        )
        ctx.include_router(router)
        app.include_router.assert_called_once_with(router)

    def test_gateway_surfaces_do_not_expose_raw_handler_access(self):
        catalog = AgentCatalog(MagicMock())
        gateway = OrchestratorGateway(MagicMock())
        assert not hasattr(catalog, "get_handler")
        assert not hasattr(catalog, "_get_handler_for_transport")
        assert not hasattr(gateway, "get_handler")


class TestD8Hardening:
    async def test_registry_rejects_duplicate_agent_registration(self):
        registry = AgentRegistry()
        first = MagicMock()
        first.agent_card.agent_id = "dup-agent"
        second = MagicMock()
        second.agent_card.agent_id = "dup-agent"

        await registry.register(first)

        with pytest.raises(ValueError, match="Agent ID already registered: dup-agent"):
            await registry.register(second)

    async def test_custom_agent_loader_rejects_name_conflicts(self):
        registry = AgentRegistry()
        loader = CustomAgentLoader(registry=registry)

        row = {
            "name": "Weather Bot",
            "description": "Weather helper",
            "system_prompt": "You are a weather assistant.",
            "intent_patterns": ["weather"],
        }

        await loader._load_one(row)

        with pytest.raises(ValueError, match="Custom agent name conflict"):
            await loader._load_one(dict(row))

    async def test_direct_ha_write_detector_warns_only_without_internal_context(self, caplog):
        client = HARestClient()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"ok": True}
        client._client = MagicMock(post=AsyncMock(return_value=response))

        reset_direct_ha_write_warning_count()
        caplog.set_level("WARNING")

        await client.call_service("light", "turn_on", entity_id="light.kitchen")

        assert "Direct HA service write without verified/internal context" in caplog.text
        assert get_direct_ha_write_warning_count() == 1

        caplog.clear()
        reset_direct_ha_write_warning_count()

        with allow_internal_ha_service_calls("test"):
            await client.call_service("light", "turn_on", entity_id="light.kitchen")

        assert "Direct HA service write without verified/internal context" not in caplog.text
        assert get_direct_ha_write_warning_count() == 0


# ---------------------------------------------------------------------------
# PluginLoader
# ---------------------------------------------------------------------------


class TestPluginLoader:
    def test_loaded_plugins_empty_initially(self):
        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir="/nonexistent", context=ctx)
        assert loader.loaded_plugins == {}

    @patch("app.plugins.loader.PluginRepository")
    async def test_discover_and_load_no_dir(self, mock_repo):
        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir="/nonexistent", context=ctx)
        await loader.discover_and_load()
        assert len(loader.loaded_plugins) == 0

    @patch("app.plugins.loader.PluginRepository")
    async def test_run_lifecycle_calls_phase_method(self, mock_repo):
        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir="/tmp", context=ctx)

        plugin = MagicMock()
        plugin.configure = AsyncMock()
        loader._loaded = {"test-plugin": plugin}

        await loader.run_lifecycle(LifecyclePhase.CONFIGURE)
        plugin.configure.assert_awaited_once_with(ctx)

    @patch("app.plugins.loader.PluginRepository")
    async def test_run_lifecycle_shutdown_no_ctx_arg(self, mock_repo):
        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir="/tmp", context=ctx)

        plugin = MagicMock()
        plugin.shutdown = AsyncMock()
        loader._loaded = {"test-plugin": plugin}

        await loader.run_lifecycle(LifecyclePhase.SHUTDOWN)
        plugin.shutdown.assert_awaited_once_with()

    @patch("app.plugins.loader.PluginRepository")
    async def test_run_lifecycle_isolates_errors(self, mock_repo):
        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir="/tmp", context=ctx)

        failing = MagicMock()
        failing.startup = AsyncMock(side_effect=Exception("plugin crash"))
        ok = MagicMock()
        ok.startup = AsyncMock()
        loader._loaded = {"failing": failing, "ok": ok}

        await loader.run_lifecycle(LifecyclePhase.STARTUP)
        ok.startup.assert_awaited_once()

    @patch("app.plugins.loader.PluginRepository")
    async def test_disable_plugin_calls_shutdown(self, mock_repo):
        mock_repo.upsert = AsyncMock()
        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir="/tmp", context=ctx)

        plugin = MagicMock()
        plugin.shutdown = AsyncMock()
        loader._loaded = {"test": plugin}

        result = await loader.disable_plugin("test")
        assert result is True
        plugin.shutdown.assert_awaited_once()
        assert "test" not in loader._loaded

    @patch("app.plugins.loader.PluginRepository")
    async def test_disabled_plugin_module_not_imported(self, mock_repo, tmp_path):
        """Disabled plugins should not be imported at all."""
        sentinel = tmp_path / "_sentinel.txt"
        plugin_file = tmp_path / "test_sentinel.py"
        plugin_file.write_text(
            "from pathlib import Path\n"
            f"Path(r'{sentinel}').write_text('imported')\n"
            "from app.plugins.base import BasePlugin\n"
            "class SentinelPlugin(BasePlugin):\n"
            "    @property\n"
            "    def name(self): return 'test-sentinel'\n"
            "    @property\n"
            "    def version(self): return '1.0.0'\n"
        )

        mock_repo.get = AsyncMock(return_value={"enabled": 0})
        mock_repo.upsert = AsyncMock()

        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir=str(tmp_path), context=ctx)
        await loader.discover_and_load()

        assert not sentinel.exists(), "Module-level code executed for disabled plugin"
        assert "test-sentinel" not in loader.loaded_plugins

    @patch("app.plugins.loader.PluginRepository")
    async def test_disabled_plugin_constructor_not_called(self, mock_repo, tmp_path):
        """Disabled plugins should not have their constructor called."""
        sentinel = tmp_path / "_ctor_sentinel.txt"
        plugin_file = tmp_path / "test_ctor.py"
        plugin_file.write_text(
            "from pathlib import Path\n"
            "from app.plugins.base import BasePlugin\n"
            "class CtorPlugin(BasePlugin):\n"
            "    def __init__(self):\n"
            "        super().__init__()\n"
            f"        Path(r'{sentinel}').write_text('constructed')\n"
            "    @property\n"
            "    def name(self): return 'test-ctor'\n"
            "    @property\n"
            "    def version(self): return '1.0.0'\n"
        )

        mock_repo.get = AsyncMock(return_value={"enabled": 0})
        mock_repo.upsert = AsyncMock()

        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir=str(tmp_path), context=ctx)
        await loader.discover_and_load()

        assert not sentinel.exists(), "Constructor ran for disabled plugin"
        assert "test-ctor" not in loader.loaded_plugins

    @patch("app.plugins.loader.PluginRepository")
    async def test_enabled_plugin_loaded_normally(self, mock_repo, tmp_path):
        """Enabled plugins should be fully imported and loaded."""
        plugin_file = tmp_path / "test_good.py"
        plugin_file.write_text(
            "from app.plugins.base import BasePlugin\n"
            "class GoodPlugin(BasePlugin):\n"
            "    @property\n"
            "    def name(self): return 'test-good'\n"
            "    @property\n"
            "    def version(self): return '2.0.0'\n"
        )

        mock_repo.get = AsyncMock(return_value=None)
        mock_repo.upsert = AsyncMock()

        ctx = MagicMock(spec=PluginContext)
        loader = PluginLoader(plugin_dir=str(tmp_path), context=ctx)
        await loader.discover_and_load()

        assert "test-good" in loader.loaded_plugins
