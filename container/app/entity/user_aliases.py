"""Loader for the optional user-supplied entity alias YAML.

The file lives at ``/data/entity_aliases.yaml`` (configurable). It is
intentionally absent / empty by default in fresh installs: the
language-agnostic matcher is expected to handle multilingual queries
without any per-deployment dictionary, and this hook exists only for
power users who want to pin a hard alias.

Schema::

    aliases:
      - alias: "kueche"
        entity_id: light.kitchen
      - alias: "schlafzimmer thermostat"
        entity_id: climate.bedroom_thermostat

All entries land in the existing ``aliases`` table managed by
``AliasRepository`` and are therefore picked up by ``AliasResolver``
and the ``AliasSignal`` matcher.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.db.repository import AliasRepository

logger = logging.getLogger(__name__)


async def load_user_aliases(path: str = "/data/entity_aliases.yaml") -> int:
    """Load aliases from a YAML file. Idempotent.

    Returns the number of aliases inserted (or upserted). Missing or
    empty file returns 0. Failures are logged and swallowed -- aliases
    are an enrichment, not a hard requirement.
    """
    p = Path(path)
    if not p.exists():
        return 0
    try:
        # Accepted cold-path sync file I/O: aliases load during setup, not per-turn handling.
        text = p.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Failed to read user alias file at %s", path, exc_info=True)
        return 0
    if not text.strip():
        return 0
    try:
        import yaml  # type: ignore[import-untyped]
    except Exception:
        logger.warning("PyYAML not available; skipping user alias load")
        return 0
    try:
        data = yaml.safe_load(text) or {}
    except Exception:
        logger.warning("Failed to parse user alias YAML at %s", path, exc_info=True)
        return 0
    if not isinstance(data, dict):
        return 0
    raw_entries = data.get("aliases") or []
    if not isinstance(raw_entries, list):
        return 0
    count = 0
    for row in raw_entries:
        if not isinstance(row, dict):
            continue
        alias = str(row.get("alias", "")).strip()
        entity_id = str(row.get("entity_id", "")).strip()
        if not alias or not entity_id:
            continue
        try:
            await AliasRepository.set(alias, entity_id)
            count += 1
        except Exception:
            logger.debug("Failed to upsert user alias %r -> %r", alias, entity_id, exc_info=True)
    if count:
        logger.info("Loaded %d user aliases from %s", count, path)
    return count
