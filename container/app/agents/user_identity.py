"""User identity resolution for voice interactions.

Matches spoken names against calendar_user_mappings using phonetic comparison.
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

    async def resolve_user(
        self,
        utterance: str | None,
        device_id: str | None = None,
        area_id: str | None = None,
    ) -> dict[str, Any] | None:
        if utterance:
            spoken_name = self._extract_self_identification(utterance)
            if spoken_name:
                user = await CalendarUserMappingRepository.find_by_name(spoken_name)
                if user:
                    return user

        if device_id:
            user = await self._resolve_by_device(device_id)
            if user:
                return user

        return await CalendarUserMappingRepository.find_default_user()

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
