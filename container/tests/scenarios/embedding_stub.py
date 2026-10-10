"""Deterministic embedding stub.

Produces a stable 384-dim float vector for any text using BLAKE2b. Same
input -> same vector; different inputs differ enough that ChromaDB
cosine distances rank inputs predictably for the small fixture corpus.
"""

from __future__ import annotations

import hashlib
import math

_DIM = 384


def deterministic_embedding(text: str) -> list[float]:
    """Return a 384-dim deterministic float vector for ``text``."""
    norm = (text or "").strip().lower()
    # Generate enough bytes to fill 384 floats (2 bytes per float).
    needed = _DIM * 2
    out = bytearray()
    counter = 0
    while len(out) < needed:
        h = hashlib.blake2b(
            f"{norm}|{counter}".encode(),
            digest_size=64,
        ).digest()
        out.extend(h)
        counter += 1
    vec: list[float] = []
    for i in range(_DIM):
        b = out[i * 2 : i * 2 + 2]
        # Map two bytes to a float in [-1, 1].
        raw = int.from_bytes(b, "big", signed=False)
        v = (raw / 65535.0) * 2.0 - 1.0
        vec.append(v)
    # L2-normalize so cosine distances behave consistently.
    norm_val = math.sqrt(sum(v * v for v in vec))
    if norm_val > 0:
        vec = [v / norm_val for v in vec]
    return vec


class StubEmbeddingEngine:
    """Drop-in for the ``app.cache.embedding`` singleton in scenario runs.

    Serves :func:`deterministic_embedding` vectors so no scenario ever loads
    the real sentence-transformers model (a per-worker multi-second load that
    starves the CPU under pytest-xdist and trips dispatch timeouts).
    """

    _MODEL = "scenario-stub"

    async def initialize(self) -> None:
        return None

    def get_info(self) -> dict:
        return {"provider": "local", "model": self._MODEL, "dimensions": _DIM, "is_multilingual": True}

    async def embed(self, text: str) -> list[float]:
        return deterministic_embedding(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [deterministic_embedding(text) for text in texts]
