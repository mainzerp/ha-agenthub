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
class MatchResult:
    identified: bool
    best: ProfileCentroid | None
    score: float | None
    margin: float | None


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
    query = l2_normalize(embedding)
    scored: list[tuple[float, ProfileCentroid]] = []
    for profile in centroids:
        if profile.centroid.shape != query.shape:
            continue
        scored.append((float(np.dot(query, profile.centroid)), profile))
    if not scored:
        return MatchResult(identified=False, best=None, score=None, margin=None)
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    gap = best_score - second
    identified = best_score >= threshold and gap >= margin
    return MatchResult(identified=identified, best=best, score=round(best_score, 4), margin=round(gap, 4))


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
