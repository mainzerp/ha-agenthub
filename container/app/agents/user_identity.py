"""User identity resolution for voice interactions.

Matches spoken names against calendar_user_mappings using phonetic comparison.
Supports resolution by HA user_id (via person entities) and person_entity_id.
"""

from __future__ import annotations

import re
from typing import Any

from app.db.repository import CalendarUserMappingRepository
from app.ha_client.persons import find_person_by_user_id

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
        identity_source: str = "ha",
    ) -> dict[str, Any] | None:
        """Resolve the calendar user mapping for this turn.

        Callers pass the personalization identity as ``user_id`` and its
        ``identity_source`` (see
        :func:`app.agents.personalization.personalization_user_id`): the
        request's Home Assistant user (``"ha"``), else the HA user linked to
        a voice-identified speaker (``"voice"``). A request user wins over a
        spoken self-identification; an unverified voice match does not, so
        for ``"voice"`` the spoken name is tried first. Calendar visibility
        filtering is unaffected.
        """
        voice_identity = identity_source == "voice"

        # 1. Try HA user_id -> person entity -> mapping (request user only)
        if user_id and not voice_identity:
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

        # 3. Voice-identified speaker's HA user -> person entity -> mapping
        if user_id and voice_identity:
            user = await self._resolve_by_user_id(user_id)
            if user:
                return user

        # 4. Try device-based resolution (future extension)
        if device_id:
            user = await self._resolve_by_device(device_id)
            if user:
                return user

        # 5. Fallback to default user
        return await CalendarUserMappingRepository.find_default_user()

    async def _resolve_by_user_id(self, user_id: str) -> dict[str, Any] | None:
        """Resolve user by HA user_id via person entities."""
        if not self._ha_client:
            return None
        try:
            states = await self._ha_client.get_states()
        except Exception:
            return None

        # Find person entity with matching user_id (shared matcher)
        person = find_person_by_user_id(states, user_id)
        if person is None:
            return None
        person_entity_id = person.entity_id

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
