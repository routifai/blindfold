"""Tests for :class:`MemoryIndex` — the txtai search layer.

Uses the deterministic, offline fake vectorizer (no OpenAI calls). The
dotted-path form of the override (vs. a bare function reference) is needed
for the persistence test: txtai's ``external`` vectors backend only survives
a save/load round trip when its ``transform`` is a re-importable dotted path.
"""

from __future__ import annotations

import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import pytest

pytest.importorskip("txtai")

from pathlib import Path

from omnigent.entities import MemoryClaim
from omnigent.memory.index import MemoryIndex
from tests.memory._fixtures import FAKE_VECTORS_OVERRIDE_DOTTED


def _claim(claim_id: str, user_id: str, text: str, **overrides: object) -> MemoryClaim:
    fields: dict[str, object] = {
        "id": claim_id,
        "user_id": user_id,
        "kind": "fact",
        "claim_text": text,
        "quote": None,
        "speaker": None,
        "confidence": 0.9,
        "first_seen": 1,
        "created_at": 1,
    }
    fields.update(overrides)
    return MemoryClaim(**fields)  # type: ignore[arg-type]


@pytest.fixture()
def index(tmp_path: Path) -> MemoryIndex:
    return MemoryIndex(tmp_path / "idx", vectors_override=FAKE_VECTORS_OVERRIDE_DOTTED)


def test_upsert_then_search_finds_the_claim(index: MemoryIndex) -> None:
    index.upsert(_claim("a" * 32, "alice", "Prefers figures in CAD currency reports"))
    results = index.search("alice", "currency CAD figures")
    assert len(results) == 1
    assert results[0]["id"] == "a" * 32


def test_search_is_scoped_to_user(index: MemoryIndex) -> None:
    index.upsert(_claim("a" * 32, "alice", "Prefers figures in CAD currency reports"))
    index.upsert(_claim("b" * 32, "bob", "Prefers figures in CAD currency reports"))
    assert [r["id"] for r in index.search("alice", "currency CAD figures")] == ["a" * 32]
    assert [r["id"] for r in index.search("bob", "currency CAD figures")] == ["b" * 32]


def test_search_filters_by_kind(index: MemoryIndex) -> None:
    index.upsert(_claim("a" * 32, "alice", "Weekly report cadence", kind="preference"))
    index.upsert(_claim("b" * 32, "alice", "Weekly report cadence", kind="fact"))
    results = index.search("alice", "weekly report cadence", kind="preference")
    assert [r["id"] for r in results] == ["a" * 32]


def test_upsert_replaces_existing_document(index: MemoryIndex) -> None:
    index.upsert(_claim("a" * 32, "alice", "Prefers figures in CAD"))
    index.upsert(_claim("a" * 32, "alice", "Prefers figures in USD now"))
    results = index.search("alice", "figures USD")
    assert len(results) == 1
    assert results[0]["text"] == "Prefers figures in USD now"


def test_delete_removes_the_claim(index: MemoryIndex) -> None:
    index.upsert(_claim("a" * 32, "alice", "Prefers figures in CAD currency reports"))
    index.delete("a" * 32)
    assert index.search("alice", "currency CAD figures") == []


def test_rebuild_replaces_the_whole_index(index: MemoryIndex) -> None:
    index.upsert(_claim("a" * 32, "alice", "Stale claim mentions zephyr widgets"))
    count = index.rebuild([_claim("b" * 32, "alice", "Fresh claim mentions quasar gadgets")])
    assert count == 1
    assert index.search("alice", "zephyr widgets") == []
    assert [r["id"] for r in index.search("alice", "quasar gadgets")] == ["b" * 32]


def test_rebuild_with_no_claims_leaves_an_empty_index(index: MemoryIndex) -> None:
    assert index.rebuild([]) == 0
    assert index.search("alice", "anything") == []


def test_index_persists_across_instances(tmp_path: Path) -> None:
    """A new MemoryIndex pointed at the same path loads the saved index."""
    path = tmp_path / "persisted"
    first = MemoryIndex(path, vectors_override=FAKE_VECTORS_OVERRIDE_DOTTED)
    first.upsert(_claim("a" * 32, "alice", "Prefers figures in CAD currency reports"))

    second = MemoryIndex(path, vectors_override=FAKE_VECTORS_OVERRIDE_DOTTED)
    results = second.search("alice", "currency CAD figures")
    assert [r["id"] for r in results] == ["a" * 32]


def test_missing_extra_raises_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing the index doesn't require txtai; using it does, with a clear error."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "txtai.embeddings" or name.startswith("txtai"):
            raise ImportError("No module named 'txtai'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    index = MemoryIndex(tmp_path / "idx2")
    with pytest.raises(ImportError, match="omnigent\\[memory\\]"):
        index.search("alice", "anything")
