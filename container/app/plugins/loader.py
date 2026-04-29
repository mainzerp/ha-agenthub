"""Plugin discovery, loading, and lifecycle management."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
from pathlib import Path

from app.db.repository import PluginRepository
from app.plugins.base import BasePlugin, PluginContext
from app.plugins.hooks import EventBus, LifecyclePhase

logger = logging.getLogger(__name__)

PLUGIN_IMPORT_TIMEOUT = 10.0


class PluginLoader:
    """Discovers, loads, and manages plugin lifecycle.

    Plugins are Python files in ``plugin_dir``. Each file is expected to
    contain exactly one subclass of :class:`BasePlugin`. The loader
    tracks enabled/disabled state in the database via
    :class:`PluginRepository`.
    """

    def __init__(self, plugin_dir: str, context: PluginContext) -> None:
        self._plugin_dir = Path(plugin_dir)
        self._context = context
        self._loaded: dict[str, BasePlugin] = {}
        self._file_map: dict[str, Path] = {}
        self.event_bus = EventBus()
        self._context.event_bus = self.event_bus

    @property
    def loaded_plugins(self) -> dict[str, BasePlugin]:
        return dict(self._loaded)

    async def discover_and_load(self) -> None:
        """Scan plugin directory for .py files, import, and load enabled plugins."""
        if not self._plugin_dir.is_dir():
            logger.info("Plugin directory does not exist: %s", self._plugin_dir)
            return

        plugin_files = sorted(self._plugin_dir.glob("*.py"))
        if plugin_files:
            names = [f.name for f in plugin_files if not f.name.startswith("_")]
            if names:
                logger.warning(
                    "Loading %d plugin file(s) from %s: %s",
                    len(names),
                    self._plugin_dir,
                    ", ".join(names),
                )
        for py_file in plugin_files:
            if py_file.name.startswith("_"):
                continue
            try:
                # Derive candidate name from filename for pre-import DB check
                candidate_name = py_file.stem.replace("_", "-")
                self._file_map[candidate_name] = py_file

                # Check DB-backed enabled state BEFORE importing
                db_record = await PluginRepository.get(candidate_name)
                if db_record is not None and not bool(db_record.get("enabled", 1)):
                    logger.info("Plugin '%s' is disabled, skipping import", candidate_name)
                    continue

                plugin_cls = await self._import_plugin_class(py_file)
                if plugin_cls is None:
                    continue
                instance = plugin_cls()
                name = instance.name
                # Update file map with actual name if different
                if name != candidate_name:
                    del self._file_map[candidate_name]
                    self._file_map[name] = py_file
                    # Re-check DB with actual name
                    db_record = await PluginRepository.get(name)
                    if db_record is not None and not bool(db_record.get("enabled", 1)):
                        logger.info("Plugin '%s' is disabled, skipping", name)
                        await PluginRepository.upsert(
                            name=name,
                            file_path=str(py_file),
                            version=instance.version,
                            description=instance.description,
                            enabled=0,
                        )
                        continue

                enabled = True
                if db_record is not None:
                    enabled = bool(db_record.get("enabled", 1))

                # Upsert plugin metadata
                await PluginRepository.upsert(
                    name=name,
                    file_path=str(py_file),
                    version=instance.version,
                    description=instance.description,
                    enabled=1 if enabled else 0,
                )

                if enabled:
                    self._loaded[name] = instance
                    logger.info("Loaded plugin '%s' v%s from %s", name, instance.version, py_file.name)
                else:
                    logger.info("Plugin '%s' is disabled, skipping", name)

            except Exception:
                logger.exception("Failed to load plugin from %s", py_file)

        logger.info(
            "Plugin discovery complete: %d loaded, %d discovered",
            len(self._loaded),
            len(self._file_map),
        )

    async def _import_plugin_class(self, py_file: Path) -> type[BasePlugin] | None:
        """Safely import a single Python file and find a BasePlugin subclass.

        The synchronous ``spec.loader.exec_module`` call is offloaded to a
        worker thread and wrapped in ``asyncio.wait_for`` with a
        ``PLUGIN_IMPORT_TIMEOUT`` second cap (PERF-5) so a misbehaving
        plugin that hangs at import time cannot block application startup.
        """
        module_name = f"plugin_{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, str(py_file))
        if spec is None or spec.loader is None:
            logger.warning("Cannot create module spec for %s", py_file)
            return None

        module = importlib.util.module_from_spec(spec)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(spec.loader.exec_module, module),
                timeout=PLUGIN_IMPORT_TIMEOUT,
            )
        except TimeoutError:
            logger.error(
                "Plugin import for %s exceeded %.1fs timeout; skipping",
                py_file,
                PLUGIN_IMPORT_TIMEOUT,
            )
            return None

        # Find the first concrete subclass of BasePlugin
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BasePlugin) and obj is not BasePlugin:
                return obj

        logger.debug("No BasePlugin subclass found in %s", py_file)
        return None

    async def run_lifecycle(self, phase: LifecyclePhase) -> None:
        """Run a lifecycle phase for all loaded plugins with error isolation."""
        for name, plugin in self._loaded.items():
            try:
                handler = getattr(plugin, phase.value)
                if phase == LifecyclePhase.SHUTDOWN:
                    await asyncio.wait_for(handler(), timeout=30.0)
                else:
                    await asyncio.wait_for(handler(self._context), timeout=30.0)
            except TimeoutError:
                logger.warning("Plugin '%s' %s hook timed out after 30s", name, phase.value)
            except Exception:
                logger.exception("Plugin '%s' failed during %s phase", name, phase.value)

    async def enable_plugin(self, name: str) -> bool:
        """Enable a plugin by name. Loads and runs configure+startup+ready."""
        if name in self._loaded:
            return True

        file_path = self._file_map.get(name)
        if not file_path:
            db_record = await PluginRepository.get(name)
            if db_record and db_record.get("file_path"):
                file_path = Path(db_record["file_path"])
            if not file_path or not file_path.exists():
                logger.error("Cannot enable plugin '%s': file not found", name)
                return False

        # Validate path is strictly inside the plugin directory
        resolved = file_path.resolve()
        plugin_dir = self._plugin_dir.resolve()
        sep = os.sep
        if not str(resolved).startswith(str(plugin_dir) + sep) and resolved != plugin_dir:
            logger.error("Plugin file %s must reside in %s", resolved, plugin_dir)
            raise ValueError(f"Plugin file {resolved} must reside in {plugin_dir}")

        try:
            plugin_cls = await self._import_plugin_class(file_path)
            if plugin_cls is None:
                return False
            instance = plugin_cls()
            self._loaded[name] = instance

            await PluginRepository.upsert(
                name=name,
                file_path=str(file_path),
                version=instance.version,
                description=instance.description,
                enabled=1,
            )

            # Run lifecycle phases for the newly enabled plugin
            for phase in (LifecyclePhase.CONFIGURE, LifecyclePhase.STARTUP, LifecyclePhase.READY):
                try:
                    handler = getattr(instance, phase.value)
                    await asyncio.wait_for(handler(self._context), timeout=30.0)
                except TimeoutError:
                    logger.warning("Plugin '%s' %s hook timed out after 30s", name, phase.value)
                except Exception:
                    logger.exception("Plugin '%s' failed during %s on enable", name, phase.value)

            logger.info("Enabled plugin '%s'", name)
            return True

        except Exception:
            logger.exception("Failed to enable plugin '%s'", name)
            return False

    async def disable_plugin(self, name: str) -> bool:
        """Disable a plugin. Calls shutdown, then removes from loaded."""
        plugin = self._loaded.pop(name, None)
        if plugin:
            try:
                await plugin.shutdown()
            except Exception:
                logger.exception("Plugin '%s' failed during shutdown on disable", name)

        await PluginRepository.upsert(
            name=name,
            file_path=str(self._file_map.get(name, "")),
            enabled=0,
        )
        logger.info("Disabled plugin '%s'", name)
        return True
