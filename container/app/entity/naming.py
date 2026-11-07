"""Entity name derivation for the entity index.

Since Home Assistant 2026.11 the ``friendly_name`` state attribute of an
entity with a name of its own is ``"<device name> <own name>"``. The entity
index matches and speaks such an entity by its own name instead, the name HA's
``entity_name()`` template returns: the user-set registry ``name``, else the
integration's ``original_name``. Every other entity keeps HA's
``friendly_name`` unchanged: a device's main entity (no own name; its
``friendly_name`` is the device name), entities without a device, names HA's
migration froze (``compat_name``), ``homeassistant: customize`` overrides, and
every older HA version, where the own name already is the whole
``friendly_name``.

Pure functions without I/O. Language-neutral: only HA's own names are
compared, no word lists.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from collections.abc import Iterable, Mapping
from typing import Any

from app.entity.tokens import fold_text


def collapse_whitespace(text: str) -> str:
    """Strip ``text`` and collapse inner whitespace runs to single spaces."""
    return " ".join(text.split())


@functools.lru_cache(maxsize=16384)
def name_fold_key(name: str) -> str:
    """Comparison key for name collisions: shared lookup folding plus whitespace collapse.

    Cached: every full sync and registry event re-folds the same names, and
    the folding (NFKD per character) dominates the collision guard's cost.
    """
    return collapse_whitespace(fold_text(name or ""))


def stripped_own_name(friendly_name: Any, user_name: Any, device_name: Any) -> str | None:
    """Return the entity's own name when HA prefixed it with the device name, else None.

    All must hold: ``user_name`` (the own name: registry ``name``, else ``original_name``) is a non-empty string,
    ``device_name`` (``name_by_user`` or ``name`` of the entity's device) is a
    non-empty string, and ``friendly_name`` equals ``f"{device_name} {user_name}"``
    after whitespace normalization. Anything else (no user name, main entity,
    no device, compat name, customize override, older HA) returns None.
    """
    if not isinstance(friendly_name, str) or not isinstance(user_name, str) or not isinstance(device_name, str):
        return None
    own = user_name.strip()
    device = device_name.strip()
    if not own or not device:
        return None
    if collapse_whitespace(friendly_name) != collapse_whitespace(f"{device} {own}"):
        return None
    return own


def choose_entity_name(
    friendly_name: Any,
    user_name: Any,
    device_name: Any,
    *,
    suppressed: bool = False,
) -> str:
    """Name the entity index stores: the own name when it applies, else HA's friendly_name.

    ``suppressed`` (from :func:`compute_name_suppression`) keeps HA's
    friendly_name for an entity whose own name would collide with another
    entity of its domain.
    """
    raw = friendly_name if isinstance(friendly_name, str) else ""
    if suppressed:
        return raw
    own = stripped_own_name(raw, user_name, device_name)
    return own if own is not None else raw


def compute_name_suppression(
    friendly_names: Iterable[tuple[str, str]],
    user_names: Mapping[str, str] | None,
    device_names: Mapping[str, str] | None,
) -> frozenset[str]:
    """Entity ids whose own name must not be used because it would collide.

    ``friendly_names`` yields ``(entity_id, HA friendly_name)`` for every
    indexed entity. When two or more entities of one domain end up with the
    same folded name (:func:`name_fold_key`) and at least one of them got its
    name by stripping the device prefix, every stripped member of that group
    keeps its HA friendly_name, so the entities stay distinguishable. A
    reverted name can collide again, so the check repeats until stable (in
    practice one or two passes; each pass is O(n)).
    """
    user_names = user_names or {}
    device_names = device_names or {}
    # entity_id -> (domain, chosen name, stripped, HA friendly_name)
    rows: dict[str, tuple[str, str, bool, str]] = {}
    for entity_id, friendly_name in friendly_names:
        if not entity_id:
            continue
        raw = friendly_name if isinstance(friendly_name, str) else ""
        own = stripped_own_name(raw, user_names.get(entity_id), device_names.get(entity_id))
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        rows[entity_id] = (domain, own if own is not None else raw, own is not None, raw)
    # Collisions are per domain and only stripped members are suppressed:
    # domains without a stripped name cannot contribute.
    domains = {domain for domain, _name, stripped, _raw in rows.values() if stripped}
    if not domains:
        return frozenset()
    rows = {entity_id: row for entity_id, row in rows.items() if row[0] in domains}

    suppressed: set[str] = set()
    while True:
        groups: dict[tuple[str, str], list[str]] = {}
        for entity_id, (domain, name, _stripped, raw) in rows.items():
            current = raw if entity_id in suppressed else name
            groups.setdefault((domain, name_fold_key(current)), []).append(entity_id)
        added = False
        for members in groups.values():
            if len(members) < 2:
                continue
            for entity_id in members:
                if rows[entity_id][2] and entity_id not in suppressed:
                    suppressed.add(entity_id)
                    added = True
        if not added:
            return frozenset(suppressed)


def entity_display_name(entity_index: Any, entity_id: str, attributes: Mapping[str, Any] | None = None) -> str:
    """Name to speak or list for ``entity_id``.

    Prefers the entity index entry's name (the own name for user-named device
    entities), then the HA ``friendly_name`` attribute, then the entity id.
    Read and list replies that format live HA states use it so they say the
    same name as action replies. A dict lookup under the index lock; no I/O.
    """
    getter = getattr(entity_index, "get_by_id", None) if entity_index is not None and entity_id else None
    if callable(getter):
        entry = None
        with contextlib.suppress(Exception):
            entry = getter(entity_id)
        if inspect.iscoroutine(entry):
            # Async test doubles: never await from this sync helper.
            entry.close()
            entry = None
        name = getattr(entry, "friendly_name", None)
        if isinstance(name, str) and name.strip():
            return name
    if isinstance(attributes, Mapping):
        raw = attributes.get("friendly_name")
        if isinstance(raw, str) and raw.strip():
            return raw
    return entity_id
