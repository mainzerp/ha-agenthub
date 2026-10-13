"""Armed voice-profile enrollment (in-memory, one profile at a time).

While armed, each finished voice turn becomes an enrollment candidate for
the armed profile instead of being identified. Only accepted samples count;
rejected ones (too little speech, another speaker, inconsistent voice) are
counted separately with the last rejection reason for the dashboard.
Enrollment disarms itself when the target sample count is reached or it
expires. When the target is reached, the service may first replace the
worst sample of this session (an outlier, see
:meth:`app.voice.service.SpeakerIdService.commit_enrollment`); the session
then stays armed for one more sample, at most ``MAX_OUTLIER_REPLACEMENTS``
times.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_ENROLL_SAMPLES = 10
DEFAULT_ENROLL_TIMEOUT_S = 600
MIN_ENROLL_SAMPLES = 1
MAX_ENROLL_SAMPLES = 20
MIN_ENROLL_TIMEOUT_S = 30
MAX_ENROLL_TIMEOUT_S = 1800
# Outlier samples one enrollment session may replace before it completes anyway.
MAX_OUTLIER_REPLACEMENTS = 3


@dataclass
class _Armed:
    profile_id: int
    name: str
    target: int
    collected: int
    expires_monotonic: float
    expires_at: str
    rejected: int = 0
    last_rejection: dict[str, Any] | None = None
    # Ids of the samples stored in this session (the only outlier candidates).
    session_sample_ids: list[int] = field(default_factory=list)
    replaced: int = 0
    last_replaced: dict[str, Any] | None = None


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

    def _active(self, profile_id: int | None = None) -> _Armed | None:
        """The armed enrollment (for ``profile_id`` when given), disarming it first when expired."""
        armed = self._armed
        if armed is None:
            return None
        if time.monotonic() >= armed.expires_monotonic:
            self._armed = None
            return None
        if profile_id is not None and armed.profile_id != profile_id:
            return None
        return armed

    def current(self) -> dict[str, Any] | None:
        """Return the armed enrollment, disarming it first when expired."""
        armed = self._active()
        if armed is None:
            return None
        return {
            "profile_id": armed.profile_id,
            "name": armed.name,
            "remaining": max(0, armed.target - armed.collected),
            "collected": armed.collected,
            "rejected": armed.rejected,
            "last_rejection": dict(armed.last_rejection) if armed.last_rejection else None,
            "replaced": armed.replaced,
            "last_replaced": dict(armed.last_replaced) if armed.last_replaced else None,
            "expires_at": armed.expires_at,
        }

    def record_sample(self, profile_id: int, sample_id: int | None = None, *, finish: bool = True) -> int:
        """Count one stored sample. Returns the remaining count.

        At 0 remaining the enrollment disarms, unless ``finish`` is False:
        then it stays armed until :meth:`finish` or :meth:`replace_sample`
        (the outlier review of the target-reaching sample).
        """
        armed = self._active(profile_id)
        if armed is None:
            return 0
        armed.collected += 1
        if sample_id is not None:
            armed.session_sample_ids.append(int(sample_id))
        remaining = max(0, armed.target - armed.collected)
        if remaining == 0 and finish:
            self._armed = None
        return remaining

    def session_samples(self, profile_id: int) -> list[int]:
        """Ids of the samples stored in the armed session of ``profile_id`` (empty when not armed)."""
        armed = self._active(profile_id)
        return list(armed.session_sample_ids) if armed is not None else []

    def replacements_left(self, profile_id: int) -> int:
        """Outlier replacements the armed session of ``profile_id`` may still make (0 when not armed)."""
        armed = self._active(profile_id)
        return max(0, MAX_OUTLIER_REPLACEMENTS - armed.replaced) if armed is not None else 0

    def replace_sample(self, profile_id: int, sample_id: int, score: float | None) -> bool:
        """Record a deleted outlier of this session: it no longer counts and one more sample is needed.

        Keeps the original expiry. Returns False (nothing recorded) when the
        session is no longer armed, ``sample_id`` is not a sample of this
        session, or the replacement cap is reached.
        """
        armed = self._active(profile_id)
        if armed is None or sample_id not in armed.session_sample_ids or armed.replaced >= MAX_OUTLIER_REPLACEMENTS:
            return False
        armed.session_sample_ids.remove(sample_id)
        armed.collected = max(0, armed.collected - 1)
        armed.replaced += 1
        armed.last_replaced = {"sample_id": sample_id, "score": score, "at": datetime.now(UTC).isoformat()}
        return True

    def finish(self, profile_id: int) -> dict[str, Any] | None:
        """Disarm a session of ``profile_id`` that reached its target; returns its final snapshot.

        Returns None (nothing changes) when it is not armed or still needs samples.
        """
        snapshot = self.current()
        if snapshot is None or snapshot["profile_id"] != profile_id or snapshot["remaining"] > 0:
            return None
        self._armed = None
        return snapshot

    def record_rejection(self, profile_id: int, reason: str) -> None:
        """Count one rejected sample (it never counts toward the target)."""
        armed = self._active(profile_id)
        if armed is None:
            return
        armed.rejected += 1
        armed.last_rejection = {"reason": reason, "at": datetime.now(UTC).isoformat()}

    def rename(self, profile_id: int, name: str) -> None:
        """Update the label of the armed enrollment after a profile rename."""
        if self._armed is not None and self._armed.profile_id == profile_id:
            self._armed.name = name

    def disarm_profile(self, profile_id: int) -> None:
        """Disarm when the armed profile is deleted."""
        if self._armed is not None and self._armed.profile_id == profile_id:
            self._armed = None
