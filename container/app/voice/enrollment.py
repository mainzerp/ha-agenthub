"""Armed voice-profile enrollment (in-memory, one profile at a time).

While armed, each finished voice turn long enough becomes an enrollment
sample for the armed profile instead of being identified. Enrollment
disarms itself when the target sample count is reached or it expires.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_ENROLL_SAMPLES = 5
DEFAULT_ENROLL_TIMEOUT_S = 300
MIN_ENROLL_SAMPLES = 1
MAX_ENROLL_SAMPLES = 20
MIN_ENROLL_TIMEOUT_S = 30
MAX_ENROLL_TIMEOUT_S = 1800


@dataclass
class _Armed:
    profile_id: int
    name: str
    target: int
    collected: int
    expires_monotonic: float
    expires_at: str


class EnrollmentState:
    """Holds at most one armed enrollment."""

    def __init__(self) -> None:
        self._armed: _Armed | None = None

    def arm(
        self,
        profile_id: int,
        name: str,
        samples: int = DEFAULT_ENROLL_SAMPLES,
        timeout_s: int = DEFAULT_ENROLL_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Arm enrollment for ``profile_id``, replacing any armed enrollment."""
        expires_at = datetime.now(UTC) + timedelta(seconds=timeout_s)
        self._armed = _Armed(
            profile_id=profile_id,
            name=name,
            target=samples,
            collected=0,
            expires_monotonic=time.monotonic() + timeout_s,
            expires_at=expires_at.isoformat(),
        )
        snapshot = self.current()
        assert snapshot is not None
        return snapshot

    def cancel(self) -> bool:
        """Disarm. Returns True when an enrollment was armed."""
        was_armed = self.current() is not None
        self._armed = None
        return was_armed

    def current(self) -> dict[str, Any] | None:
        """Return the armed enrollment, disarming it first when expired."""
        armed = self._armed
        if armed is None:
            return None
        if time.monotonic() >= armed.expires_monotonic:
            self._armed = None
            return None
        return {
            "profile_id": armed.profile_id,
            "name": armed.name,
            "remaining": max(0, armed.target - armed.collected),
            "expires_at": armed.expires_at,
        }

    def record_sample(self, profile_id: int) -> int:
        """Count one stored sample. Returns the remaining count (disarms at 0)."""
        armed = self._armed
        if armed is None or armed.profile_id != profile_id:
            return 0
        armed.collected += 1
        remaining = max(0, armed.target - armed.collected)
        if remaining == 0:
            self._armed = None
        return remaining

    def rename(self, profile_id: int, name: str) -> None:
        """Update the label of the armed enrollment after a profile rename."""
        if self._armed is not None and self._armed.profile_id == profile_id:
            self._armed.name = name

    def disarm_profile(self, profile_id: int) -> None:
        """Disarm when the armed profile is deleted."""
        if self._armed is not None and self._armed.profile_id == profile_id:
            self._armed = None
