"""Entity alias resolution from SQLite."""

from __future__ import annotations

import asyncio
import logging

from app.db.repository import AliasRepository

logger = logging.getLogger(__name__)


class AliasResolver:
    """Resolves entity aliases from the SQLite aliases table."""

    def __init__(self) -> None:
        self._cache: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Load all aliases into an in-memory dict for fast lookup."""
        rows = await AliasRepository.list_all()
        self._cache = {row["alias"].lower(): row["entity_id"] for row in rows}
        logger.info("Loaded %d entity aliases", len(self._cache))

    async def resolve(self, alias: str) -> str | None:
        """Look up an alias. Returns entity_id or None."""
        if self._cache is None:
            await self.load()
        assert self._cache is not None
        return self._cache.get(alias.lower())

    async def substitute(self, text: str) -> str:
        """Replace all known aliases in text with their entity_ids.

        Scans text for alias substrings (longest-first to avoid partial matches)
        and replaces them with the corresponding entity_id.
        """
        if self._cache is None:
            await self.load()
        assert self._cache is not None
        if not self._cache:
            return text
        # Sort by length descending to match longest aliases first
        sorted_aliases = sorted(self._cache.keys(), key=len, reverse=True)
        result = text
        text_lower = text.lower()
        for alias in sorted_aliases:
            idx = text_lower.find(alias)
            if idx != -1:
                result = result[:idx] + self._cache[alias] + result[idx + len(alias) :]
                text_lower = result.lower()
        return result

    async def list_all(self) -> dict[str, str]:
        """Return all alias -> entity_id mappings."""
        if self._cache is None:
            await self.load()
        assert self._cache is not None
        return dict(self._cache)

    async def reload(self) -> None:
        """Reload aliases from DB (e.g. after admin changes)."""
        async with self._lock:
            self._cache = None
            await self.load()
