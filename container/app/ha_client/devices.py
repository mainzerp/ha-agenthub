"""Home Assistant device directory: device id -> area and display name.

HA voice satellites send only their ``device_id`` with a turn. The container
derives the satellite's area from HA's device registry so every downstream
consumer (actionable agents, action-cache origin binding, traces) sees the
origin area without the bridge resolving anything (Prime Directive 1).

The map lives in memory and is refreshed by the entity-index registry sync
(startup prime, periodic / reconnect resync, device and area registry
events). Turns only read it: there is no Home Assistant round trip per turn.

Origin metadata only: the area is the turn's preferred location; entity
visibility is still enforced on every resolution path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceOrigin:
    """Registry placement of one HA device."""

    # ``name_by_user`` or ``name``; None when the device has neither.
    name: str | None
    # Area the device is assigned to; None when it has no area.
    area_id: str | None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def parse_device_rows(rows: Any) -> dict[str, DeviceOrigin]:
    """Build ``{device_id: DeviceOrigin}`` from ``{device_id: {"name", "area_id"}}`` rows."""
    devices: dict[str, DeviceOrigin] = {}
    if not isinstance(rows, dict):
        return devices
    for device_id, row in rows.items():
        if not isinstance(device_id, str) or not device_id or not isinstance(row, dict):
            continue
        devices[device_id] = DeviceOrigin(name=_clean(row.get("name")), area_id=_clean(row.get("area_id")))
    return devices


class DeviceDirectory:
    """In-memory device id -> :class:`DeviceOrigin` map plus area names."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceOrigin] = {}
        self._area_names: dict[str, str] = {}

    def replace(self, devices: dict[str, DeviceOrigin], area_names: dict[str, str] | None = None) -> None:
        """Install a fresh device map (and area names when given) atomically."""
        self._devices = dict(devices)
        if area_names is not None:
            self._area_names = {str(k): str(v) for k, v in area_names.items() if k and v}

    def reset(self) -> None:
        """Drop all cached data (tests)."""
        self._devices = {}
        self._area_names = {}

    def __len__(self) -> int:
        return len(self._devices)

    def lookup(self, device_id: str | None) -> DeviceOrigin | None:
        """Return the cached placement of ``device_id`` (None when unknown)."""
        if not device_id:
            return None
        return self._devices.get(device_id)

    def area_name(self, area_id: str | None) -> str | None:
        if not area_id:
            return None
        return self._area_names.get(area_id)

    async def refresh(self, ha_client: Any, area_names: dict[str, str] | None = None) -> bool:
        """Re-read the device registry through ``ha_client.get_device_registry``.

        ``area_names`` (``{area_id: name}``) is fetched from the area
        registry when not supplied. A failed or malformed fetch keeps the
        previous map and returns False; refresh never raises for HA errors.
        """
        getter = getattr(ha_client, "get_device_registry", None)
        if not callable(getter):
            return False
        try:
            rows = await getter()
            if area_names is None:
                area_getter = getattr(ha_client, "get_area_registry", None)
                area_names = await area_getter() if callable(area_getter) else None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Device registry refresh failed; keeping previous device map", exc_info=True)
            return False
        if not isinstance(rows, dict):
            return False
        self.replace(parse_device_rows(rows), area_names if isinstance(area_names, dict) else None)
        logger.debug("Device directory refreshed: %d devices", len(self._devices))
        return True


_device_directory = DeviceDirectory()


def get_device_directory() -> DeviceDirectory:
    """Process-wide device directory."""
    return _device_directory


def apply_device_origin(context: Any, directory: DeviceDirectory | None = None) -> bool:
    """Fill a turn context's origin area / device name from its ``device_id``.

    An explicit ``area_id`` on the request wins and is never overwritten;
    ``device_name`` / ``area_name`` are only filled when empty. Returns True
    when the device's area was applied to the context.
    """
    if context is None:
        return False
    device_id = getattr(context, "device_id", None)
    if not isinstance(device_id, str) or not device_id:
        return False
    directory = directory or get_device_directory()
    origin = directory.lookup(device_id)
    if origin is None:
        return False
    if origin.name and not getattr(context, "device_name", None):
        context.device_name = origin.name
    if getattr(context, "area_id", None) or not origin.area_id:
        return False
    context.area_id = origin.area_id
    if not getattr(context, "area_name", None):
        context.area_name = directory.area_name(origin.area_id)
    return True
