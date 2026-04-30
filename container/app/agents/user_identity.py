"""User identity resolution for voice interactions.

Matches spoken names against calendar_user_mappings using phonetic comparison.
Supports resolution by HA user_id (via person entities) and person_entity_id.
"""

from __future__ import annotations

import re
from typing import Any

from app.db.repository import CalendarUserMappingRepository

_SELF_IDENTIFICATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:ich\s+bin|das\s+ist|mein\s+name\s+ist|ich\s+heisse)\s+(.{1,40})", re.IGNORECASE),
    re.compile(r"(?:this\s+is|my\s+name\s+is|i\s+am)\s+(.{1,40})", re.IGNORECASE),
)


class UserIdentityResolver:
    """Resolves the speaking user from utterance text and device context."""

    def __init__(self, ha_client=None):
        self._ha_client = ha_client

    async def resolve_user(
        self,
        utterance: str | None,
        device_id: str | None = None,
        area_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        # 1. Try HA user_id -> person entity -> mapping
        if user_id:
            user = await self._resolve_by_user_id(user_id)
            if user:
                return user

        # 2. Try spoken self-identification (phonetic)
        if utterance:
            spoken_name = self._extract_self_identification(utterance)
            if spoken_name:
                user = await CalendarUserMappingRepository.find_by_name(spoken_name)
                if user:
                    return user

        # 3. Try device-based resolution (future extension)
        if device_id:
            user = await self._resolve_by_device(device_id)
            if user:
                return user

        # 4. Fallback to default user
        return await CalendarUserMappingRepository.find_default_user()

    async def _resolve_by_user_id(self, user_id: str) -> dict[str, Any] | None:
        """Resolve user by HA user_id via person entities."""
        if not self._ha_client:
            return None
        try:
            states = await self._ha_client.get_states()
        except Exception:
            return None

        # Find person entity with matching user_id
        person_entity_id = None
        for state in states:
            eid = state.get("entity_id", "")
            if eid.startswith("person."):
                attrs = state.get("attributes", {})
                if attrs.get("user_id") == user_id:
                    person_entity_id = eid
                    break

        if not person_entity_id:
            return None

        # Find mapping by person_entity_id
        rows = await CalendarUserMappingRepository.list_all()
        for row in rows:
            if row.get("person_entity_id") == person_entity_id:
                return row
        return None

    def _extract_self_identification(self, utterance: str) -> str | None:
        text = str(utterance or "").strip()
        if not text:
            return None
        for pattern in _SELF_IDENTIFICATION_PATTERNS:
            match = pattern.search(text)
            if match:
                name = match.group(1).strip(" .,;:!?")
                if name and len(name) >= 2:
                    return name
        return None

    async def _resolve_by_device(self, device_id: str) -> dict[str, Any] | None:
        return None
