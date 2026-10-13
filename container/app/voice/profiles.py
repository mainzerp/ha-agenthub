"""Voice-profile centroids and cosine matching.

A profile's centroid is the mean of its L2-normalised sample embeddings
(only samples computed by the current model). Centroids are cached in
memory and invalidated on every profile or sample change.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.db.repository import VoiceProfileRepository

logger = logging.getLogger(__name__)

_EPS = 1e-12


@dataclass(frozen=True)
class ProfileCentroid:
    profile_id: int
    name: str
    ha_user_id: str | None
    centroid: np.ndarray


@dataclass(frozen=True)
class ProfileScore:
    profile: ProfileCentroid
    score: float


@dataclass(frozen=True)
class MatchResult:
    identified: bool
    best: ProfileCentroid | None
    score: float | None
    margin: float | None
    # Every comparable profile, best first (raw cosine scores).
    ranked: tuple[ProfileScore, ...] = ()

    @property
    def second(self) -> ProfileScore | None:
        return self.ranked[1] if len(self.ranked) > 1 else None


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    vec = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if norm < _EPS:
        return vec
    return vec / norm


def build_centroids(rows: list[dict[str, Any]]) -> list[ProfileCentroid]:
    """Group sample rows by profile into normalised mean centroids.

    Rows whose ``dim`` does not match the stored bytes, or whose dimension
    differs from the first valid sample, are skipped.
    """
    grouped: dict[int, list[np.ndarray]] = {}
    meta: dict[int, tuple[str, str | None]] = {}
    expected_dim: int | None = None
    for row in rows:
        try:
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
        except (TypeError, ValueError):
            continue
        if vec.size == 0 or vec.size != int(row.get("dim") or 0):
            continue
        if expected_dim is None:
            expected_dim = vec.size
        elif vec.size != expected_dim:
            continue
        pid = int(row["profile_id"])
        grouped.setdefault(pid, []).append(l2_normalize(vec))
        meta[pid] = (str(row.get("name") or ""), row.get("ha_user_id"))
    centroids: list[ProfileCentroid] = []
    for pid, vectors in grouped.items():
        mean = np.mean(np.stack(vectors), axis=0)
        name, ha_user_id = meta[pid]
        centroids.append(ProfileCentroid(pid, name, ha_user_id, l2_normalize(mean)))
    return centroids


def score_profiles(embedding: np.ndarray, centroids: list[ProfileCentroid]) -> list[ProfileScore]:
    """Cosine score of ``embedding`` against every comparable centroid, best first."""
    query = l2_normalize(embedding)
    scored = [
        ProfileScore(profile, float(np.dot(query, profile.centroid)))
        for profile in centroids
        if profile.centroid.shape == query.shape
    ]
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def scores_payload(ranked: tuple[ProfileScore, ...] | list[ProfileScore]) -> list[dict[str, Any]]:
    """``[{"name", "score"}]`` for diagnostics (best first, 4 decimals)."""
    return [{"name": item.profile.name, "score": round(item.score, 4)} for item in ranked]


def match_embedding(
    embedding: np.ndarray,
    centroids: list[ProfileCentroid],
    *,
    threshold: float,
    margin: float,
) -> MatchResult:
    """Cosine-match ``embedding`` against ``centroids``.

    Identified when the best score reaches ``threshold`` AND leads the
    second-best by at least ``margin`` (second-best is 0 with one profile).
    """
    ranked = tuple(score_profiles(embedding, centroids))
    if not ranked:
        return MatchResult(identified=False, best=None, score=None, margin=None)
    best_score = ranked[0].score
    second = ranked[1].score if len(ranked) > 1 else 0.0
    gap = best_score - second
    identified = best_score >= threshold and gap >= margin
    return MatchResult(
        identified=identified,
        best=ranked[0].profile,
        score=round(best_score, 4),
        margin=round(gap, 4),
        ranked=ranked,
    )


# Enrollment-sample rejection reasons (see :func:`check_enrollment_sample`).
REJECTED_OTHER_SPEAKER = "rejected_other_speaker"
REJECTED_INCONSISTENT = "rejected_inconsistent"
REJECTED_TOO_SHORT = "rejected_too_short"


@dataclass(frozen=True)
class EnrollmentCheck:
    """Outcome of :func:`check_enrollment_sample`; ``reason`` is None when the sample is accepted."""

    reason: str | None
    own_score: float | None
    ranked: tuple[ProfileScore, ...]


def check_enrollment_sample(
    embedding: np.ndarray,
    profile_id: int,
    centroids: list[ProfileCentroid],
    *,
    threshold: float,
    margin: float,
    min_similarity: float,
) -> EnrollmentCheck:
    """Decide whether an enrollment sample belongs to ``profile_id``.

    With existing samples (the profile has a centroid): reject when the
    sample does not lead the closest other profile by at least half the
    identification ``margin`` -- which includes scoring higher against
    another profile (``rejected_other_speaker``) -- or when its cosine to
    the own centroid is below ``min_similarity`` (``rejected_inconsistent``).
    First sample:
    reject when it would be identified as another profile under the normal
    threshold and margin rule (``rejected_other_speaker``).
    """
    ranked = tuple(score_profiles(embedding, centroids))
    own = next((item for item in ranked if item.profile.profile_id == profile_id), None)
    others = [item for item in ranked if item.profile.profile_id != profile_id]
    reason: str | None = None
    if own is None:
        if others:
            best = others[0].score
            second = others[1].score if len(others) > 1 else 0.0
            if best >= threshold and best - second >= margin:
                reason = REJECTED_OTHER_SPEAKER
    elif others and (others[0].score > own.score or own.score - others[0].score < margin * 0.5):
        # A near-tie sample would pull the two centroids together.
        reason = REJECTED_OTHER_SPEAKER
    elif own.score < min_similarity:
        reason = REJECTED_INCONSISTENT
    own_score = round(own.score, 4) if own is not None else None
    return EnrollmentCheck(reason=reason, own_score=own_score, ranked=ranked)


def sample_diagnostics(
    samples: list[dict[str, Any]],
    other_centroids: list[ProfileCentroid],
) -> list[dict[str, Any]]:
    """Per-sample quality numbers for one profile (CPU work: run in a worker thread).

    For every sample row (``id`` and ``embedding`` bytes of the current
    model): ``loo_score`` is the cosine to the centroid of the profile's
    OTHER samples (None with a single sample) and ``best_other_name`` /
    ``best_other_score`` the closest other profile. A low ``loo_score`` or a
    ``best_other_score`` above it marks a sample that does not fit the
    profile (wrong speaker, noise, silence).
    """
    vectors: list[tuple[int, np.ndarray]] = []
    for row in samples:
        try:
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
        except (TypeError, ValueError):
            continue
        if vec.size == 0 or vec.size != int(row.get("dim") or 0):
            continue
        vectors.append((int(row["id"]), l2_normalize(vec)))
    if vectors:
        dim = vectors[0][1].shape
        vectors = [(sid, vec) for sid, vec in vectors if vec.shape == dim]
    total = np.sum(np.stack([vec for _, vec in vectors]), axis=0) if vectors else None
    results: list[dict[str, Any]] = []
    for sample_id, vec in vectors:
        loo: float | None = None
        if total is not None and len(vectors) > 1:
            rest = l2_normalize(total - vec)
            loo = round(float(np.dot(vec, rest)), 4)
        ranked = score_profiles(vec, other_centroids)
        best_other = ranked[0] if ranked else None
        results.append(
            {
                "id": sample_id,
                "loo_score": loo,
                "best_other_name": best_other.profile.name if best_other else None,
                "best_other_score": round(best_other.score, 4) if best_other else None,
            }
        )
    return results


class ProfileCentroidCache:
    """Caches per-model centroids loaded from the repository."""

    def __init__(self) -> None:
        self._cache: dict[str, list[ProfileCentroid]] = {}
        self._generation = 0

    def invalidate(self) -> None:
        self._cache.clear()
        self._generation += 1

    async def get(self, model: str) -> list[ProfileCentroid]:
        cached = self._cache.get(model)
        if cached is not None:
            return cached
        generation = self._generation
        rows = await VoiceProfileRepository.list_samples_for_model(model)
        centroids = await asyncio.to_thread(build_centroids, rows)
        # A change that landed while loading must not be overwritten by stale data.
        if generation == self._generation:
            self._cache[model] = centroids
        return centroids
