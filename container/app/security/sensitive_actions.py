"""Sensitive Home Assistant actions: classification, approval tokens and the write backstop.

Some service calls open the home to people: unlocking or opening a lock,
disarming or arming an alarm panel, and opening a garage door, gate or
door-type cover. They need an explicit approval from the user before they
run (``app.agents.approval`` asks for it on interactive channels).

This module owns the policy that every layer shares:

- :func:`classify_sensitive` -- pure classification of one
  ``domain/service`` call on one entity.
- :func:`approved_calls` -- a ContextVar approval token scoped to exactly
  the approved ``(domain, service, entity_id)`` tuples.
- :func:`enforce_sensitive_call_policy` -- the fail-closed backstop called
  by ``HARestClient.call_service`` (and the WebSocket client): a sensitive
  call without a matching token, outside the allowlist, while
  ``approval.enabled`` is on, raises :class:`SensitiveActionBlocked`.

Settings: ``approval.enabled`` (default true) and
``approval.allowlist_entities`` (comma-separated or JSON list of entity ids
that may run without approval, default empty). A failed settings read is
treated as "enabled, empty allowlist" (fail closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Reasons returned by ``classify_sensitive``.
REASON_LOCK_OPEN = "lock_open"
REASON_ALARM_DISARM = "alarm_disarm"
REASON_ALARM_ARM = "alarm_arm"
REASON_ACCESS_COVER_OPEN = "access_cover_open"

SENSITIVE_LOCK_SERVICES: frozenset[str] = frozenset({"unlock", "open"})
ALARM_ARM_SERVICES: frozenset[str] = frozenset(
    {"alarm_arm_home", "alarm_arm_away", "alarm_arm_night", "alarm_arm_vacation", "alarm_arm_custom_bypass"}
)
SENSITIVE_ALARM_SERVICES: frozenset[str] = ALARM_ARM_SERVICES | {"alarm_disarm"}
# Tilt services are excluded: tilting slats is not an access risk.
SENSITIVE_COVER_SERVICES: frozenset[str] = frozenset({"open_cover", "set_cover_position", "toggle"})
# Cover device classes that grant physical access when opened.
ACCESS_COVER_DEVICE_CLASSES: frozenset[str] = frozenset({"garage", "gate", "door"})
# Cover services an automation may call on an access cover (closing direction only).
ACCESS_COVER_SAFE_SERVICES: frozenset[str] = frozenset({"close_cover", "stop_cover"})

# Service-data keys that target something other than explicit entity ids.
# Such targets cannot be checked per entity, so a sensitive call using them
# is always treated as needing approval.
_INDIRECT_TARGET_KEYS: tuple[str, ...] = ("area_id", "device_id", "floor_id", "label_id")

SETTING_ENABLED = "approval.enabled"
SETTING_ALLOWLIST = "approval.allowlist_entities"

ApprovedCall = tuple[str, str, str]

_approved_calls: ContextVar[frozenset[ApprovedCall]] = ContextVar("sensitive_approved_calls", default=frozenset())
_policy_enforced: ContextVar[bool] = ContextVar("sensitive_policy_enforced", default=False)

# Process-wide entity index used for device-class lookups (wired at startup).
_entity_index: Any = None


class SensitiveActionBlocked(Exception):  # noqa: N818 -- name is part of the approval contract
    """A sensitive Home Assistant call was refused because it was not approved.

    The message is safe to show to the user; it never contains service data.
    """

    def __init__(self, domain: str, service: str, entity_ids: list[str], reason: str) -> None:
        self.domain = domain
        self.service = service
        self.entity_ids = list(entity_ids)
        self.reason = reason
        targets = ", ".join(self.entity_ids) or "the requested target"
        super().__init__(
            f"{domain}.{service} on {targets} needs a spoken or typed confirmation and was not executed. "
            f"Add the entity to {SETTING_ALLOWLIST} to allow it without confirmation."
        )


@dataclass(frozen=True)
class ApprovalSettings:
    enabled: bool = True
    allowlist: frozenset[str] = frozenset()


def is_potentially_sensitive(domain: str, service: str) -> bool:
    """Cheap pre-filter: can ``domain/service`` be sensitive for some entity?"""
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    if domain == "lock":
        return service in SENSITIVE_LOCK_SERVICES
    if domain == "alarm_control_panel":
        return service in SENSITIVE_ALARM_SERVICES
    if domain == "cover":
        return service in SENSITIVE_COVER_SERVICES
    return False


def classify_sensitive(domain: str, service: str, entity_id: str | None, device_class: str | None) -> str | None:
    """Return why ``domain/service`` on ``entity_id`` needs approval, or ``None``.

    Pure function. Covers are sensitive only for an access device class
    (garage, gate, door); a cover known to have another or no device class is
    not. Callers that cannot determine the device class treat the cover as
    sensitive (see :func:`sensitive_reason_for_entity`).
    ``entity_id`` is accepted for a uniform call shape; the decision depends
    on the domain, service and device class only.
    """
    del entity_id  # the decision is per domain/service/device class
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    if domain == "lock" and service in SENSITIVE_LOCK_SERVICES:
        return REASON_LOCK_OPEN
    if domain == "alarm_control_panel":
        if service == "alarm_disarm":
            return REASON_ALARM_DISARM
        if service in ALARM_ARM_SERVICES:
            return REASON_ALARM_ARM
        return None
    if (
        domain == "cover"
        and service in SENSITIVE_COVER_SERVICES
        and (device_class or "").strip().lower() in ACCESS_COVER_DEVICE_CLASSES
    ):
        return REASON_ACCESS_COVER_OPEN
    return None


# ---------------------------------------------------------------------------
# Approval token
# ---------------------------------------------------------------------------


@contextmanager
def approved_calls(calls: Iterable[ApprovedCall]) -> Iterator[None]:
    """Approve exactly ``calls`` (``(domain, service, entity_id)``) inside the block."""
    normalized = frozenset((d.strip().lower(), s.strip().lower(), e.strip().lower()) for d, s, e in calls)
    token = _approved_calls.set(_approved_calls.get() | normalized)
    try:
        yield
    finally:
        _approved_calls.reset(token)


def is_call_approved(domain: str, service: str, entity_id: str | None) -> bool:
    if not entity_id:
        return False
    return (domain.strip().lower(), service.strip().lower(), entity_id.strip().lower()) in _approved_calls.get()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def parse_allowlist(raw: str | None) -> frozenset[str]:
    """Parse ``approval.allowlist_entities``: a JSON list or a comma-separated string."""
    if not raw or not raw.strip():
        return frozenset()
    text = raw.strip()
    items: list[Any]
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            logger.warning("approval.allowlist_entities is not valid JSON; treating it as empty")
            return frozenset()
        items = parsed if isinstance(parsed, list) else []
    else:
        items = text.split(",")
    return frozenset(str(item).strip().lower() for item in items if isinstance(item, str) and item.strip())


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("false", "0", "no", "off"):
        return False
    if value in ("true", "1", "yes", "on"):
        return True
    return default


async def load_approval_settings() -> ApprovalSettings:
    """Read the approval settings; any failure keeps approval enabled (fail closed)."""
    from app.db.repositories.settings import SettingsRepository

    try:
        enabled_raw = await SettingsRepository.get_value(SETTING_ENABLED, "true")
        allowlist_raw = await SettingsRepository.get_value(SETTING_ALLOWLIST, "")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Approval settings unavailable; approval stays enabled", exc_info=True)
        return ApprovalSettings()
    return ApprovalSettings(enabled=_parse_bool(enabled_raw, True), allowlist=parse_allowlist(allowlist_raw))


# ---------------------------------------------------------------------------
# Device-class lookup
# ---------------------------------------------------------------------------


def register_entity_index(entity_index: Any) -> None:
    """Make the process entity index available for device-class lookups."""
    global _entity_index
    _entity_index = entity_index


async def lookup_device_class(
    entity_id: str,
    *,
    entity_index: Any = None,
    state_fetcher: Callable[[str], Awaitable[Any]] | None = None,
) -> tuple[bool, str | None]:
    """``(known, device_class)`` of ``entity_id``.

    An index entry with a device class answers directly. Otherwise (no
    entry, an entry without a device class, a failed index lookup) the HA
    state is read: a state without ``device_class`` is known to have none
    (``(True, None)``, e.g. a cover group). No fetcher, a failed or empty
    state read means unknown (``(False, None)``); callers treat an unknown
    cover as sensitive (fail closed).
    """
    index = entity_index if entity_index is not None else _entity_index
    getter = getattr(index, "get_by_id_async", None) if index is not None else None
    if getter is not None:
        try:
            entry = await getter(entity_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Device-class index lookup failed for %s", entity_id, exc_info=True)
            entry = None
        value = getattr(entry, "device_class", None) if entry is not None else None
        if isinstance(value, str) and value:
            return True, value
    if state_fetcher is None:
        return False, None
    try:
        state = await state_fetcher(entity_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Device-class state lookup failed for %s", entity_id, exc_info=True)
        return False, None
    if not isinstance(state, dict):
        return False, None
    attributes = state.get("attributes")
    value = attributes.get("device_class") if isinstance(attributes, dict) else None
    return True, (value if isinstance(value, str) and value else None)


@contextmanager
def policy_already_enforced() -> Iterator[None]:
    """Skip the backstop for calls inside the block (the caller has just enforced it).

    Used only by ``HARestClient.call_service`` around its WebSocket fallback,
    which re-sends the exact call the REST backstop already checked.
    """
    token = _policy_enforced.set(True)
    try:
        yield
    finally:
        _policy_enforced.reset(token)


# ---------------------------------------------------------------------------
# Backstop
# ---------------------------------------------------------------------------


def _target_entity_ids(entity_id: str | None, service_data: dict[str, Any] | None) -> tuple[list[str], bool]:
    """Explicit target entity ids of a call, and whether it also targets indirectly."""
    values: list[Any] = [entity_id] if entity_id else []
    indirect = False
    data = service_data or {}
    targets = [data]
    if isinstance(data.get("target"), dict):
        targets.append(data["target"])
    for container in targets:
        raw = container.get("entity_id")
        if raw is not None:
            values.extend(raw if isinstance(raw, list) else [raw])
        if any(container.get(key) for key in _INDIRECT_TARGET_KEYS):
            indirect = True
    ids: list[str] = []
    for value in values:
        if not isinstance(value, str):
            indirect = True
            continue
        for part in value.split(","):
            item = part.strip().lower()
            if not item:
                continue
            if item in ("all", "none") or "." not in item:
                indirect = True
                continue
            if item not in ids:
                ids.append(item)
    return ids, indirect


async def sensitive_reason_for_entity(
    domain: str,
    service: str,
    entity_id: str,
    *,
    device_class: str | None = None,
    device_class_known: bool = False,
    state_fetcher: Callable[[str], Awaitable[Any]] | None = None,
    entity_index: Any = None,
) -> str | None:
    """``classify_sensitive`` with the device class looked up when needed.

    A cover whose device class cannot be determined counts as an access
    cover (fail closed).
    """
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    if not is_potentially_sensitive(domain, service):
        return None
    if domain == "cover" and not device_class_known:
        known, device_class = await lookup_device_class(
            entity_id, entity_index=entity_index, state_fetcher=state_fetcher
        )
        if not known:
            return REASON_ACCESS_COVER_OPEN
    return classify_sensitive(domain, service, entity_id, device_class)


async def enforce_sensitive_call_policy(
    domain: str,
    service: str,
    entity_id: str | None,
    service_data: dict[str, Any] | None,
    *,
    state_fetcher: Callable[[str], Awaitable[Any]] | None = None,
) -> None:
    """Raise :class:`SensitiveActionBlocked` for an unapproved sensitive call.

    A call passes when it is not sensitive, when ``approval.enabled`` is
    off, or when every sensitive target entity carries an approval token or
    is on the allowlist. A sensitive service with a target that cannot be
    checked per entity (``all``, area/device/floor/label targets, no target
    for lock/alarm services) is blocked unless approval is disabled.
    """
    if _policy_enforced.get():
        return
    # HA resolves services case-insensitively; so must the policy.
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    if not is_potentially_sensitive(domain, service):
        return
    entity_ids, indirect = _target_entity_ids(entity_id, service_data)
    needs_approval: list[str] = []
    reason: str | None = None
    for target in entity_ids:
        target_reason = await sensitive_reason_for_entity(domain, service, target, state_fetcher=state_fetcher)
        if target_reason is None or is_call_approved(domain, service, target):
            continue
        needs_approval.append(target)
        reason = reason or target_reason
    domain_level_reason = classify_sensitive(domain, service, None, None)
    unresolved_target = indirect or (not entity_ids and domain_level_reason is not None)
    if unresolved_target:
        reason = reason or domain_level_reason or REASON_ACCESS_COVER_OPEN
    if not needs_approval and not unresolved_target:
        return
    settings = await load_approval_settings()
    if not settings.enabled:
        return
    blocked = [target for target in needs_approval if target not in settings.allowlist]
    if not blocked and not unresolved_target:
        return
    logger.warning(
        "Blocked unapproved sensitive HA call %s.%s targets=%s reason=%s",
        domain,
        service,
        blocked or "indirect",
        reason,
    )
    raise SensitiveActionBlocked(domain, service, blocked, reason or "sensitive")
