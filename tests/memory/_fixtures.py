"""Deterministic, offline embeddings for memory tests — no OpenAI calls.

A tiny hashed bag-of-words vectorizer plugged into txtai's ``method="external"``
vectors backend (see ``omnigent.memory.index.MemoryIndex``'s
``vectors_override``), so tests exercise real txtai indexing/search without a
network call or provider API key. Module-level (not a closure) so it's also
importable by dotted path for txtai's save/load round trip.
"""

from __future__ import annotations

import hashlib

import numpy as np

DIM = 256


def _stable_bucket(token: str) -> int:
    """A process-independent token hash (Python's ``hash()`` is salted per
    run, which made bucket collisions — and this fixture's test outcomes —
    nondeterministic across test runs)."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % DIM


def fake_transform(data: list[str]) -> np.ndarray:
    """Hash each lowercased token into a fixed-size, L2-normalized vector."""
    out = np.zeros((len(data), DIM), dtype=np.float32)
    for i, text in enumerate(data):
        for token in text.lower().split():
            out[i, _stable_bucket(token)] += 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return out / norms


FAKE_VECTORS_OVERRIDE = {"method": "external", "transform": fake_transform}
FAKE_VECTORS_OVERRIDE_DOTTED = {
    "method": "external",
    "transform": "tests.memory._fixtures.fake_transform",
}
