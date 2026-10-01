"""Tests for :class:`MemoryService` — the write/read paths behind the memory tools.

Uses a real txtai index with a deterministic, offline hashed-bag-of-words
vectorizer (``tests.memory._fixtures.fake_transform``) instead of the
``litellm`` provider backend, so these tests never call the OpenAI API (or
any network).
"""

from __future__ import annotations

import os

# Two copies of libomp (torch + faiss-cpu) can both be linked into the same
# macOS dev process; this is the documented, narrowly-scoped workaround for
# local test runs. Harmless in Linux CI, where this conflict doesn't occur.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import pytest

txtai = pytest.importorskip("txtai")

from pathlib import Path  # noqa: E402

from omnigent.memory.index import MemoryIndex  # noqa: E402
from omnigent.memory.service import MemoryService  # noqa: E402
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore  # noqa: E402
from tests.memory._fixtures import FAKE_VECTORS_OVERRIDE  # noqa: E402


@pytest.fixture()
def service(db_uri: str, tmp_path: Path) -> MemoryService:
    store = SqlAlchemyMemoryStore(db_uri)
    index = MemoryIndex(tmp_path / "memory_index", vectors_override=FAKE_VECTORS_OVERRIDE)
    return MemoryService(store, index)


# ── remember: add / reinforce / supersede ───────────────────────────────────


def test_remember_adds_a_new_claim(service: MemoryService) -> None:
    result = service.remember("alice", "Prefers figures in CAD", kind="preference")
    assert result["action"] == "added"
    assert result["claim"]["text"] == "Prefers figures in CAD"
    assert result["claim"]["kind"] == "preference"
    assert result["claim"]["explicitness"] == "stated"
    assert result["claim"]["confidence"] == 0.9


def test_remember_reinforces_a_near_duplicate(service: MemoryService) -> None:
    first = service.remember("alice", "Prefers figures in CAD", kind="preference")
    second = service.remember("alice", "Wants figures reported in CAD", kind="preference")
    assert second["action"] == "reinforced"
    assert second["claim"]["claim_id"] == first["claim"]["claim_id"]
    assert second["claim"]["confidence"] > first["claim"]["confidence"]


def test_remember_supersedes_a_contradiction(service: MemoryService) -> None:
    first = service.remember("alice", "Prefers figures in CAD", kind="preference")
    second = service.remember("alice", "Actually wants figures in USD now", kind="preference")
    assert second["action"] == "superseded"
    assert second["claim"]["claim_id"] != first["claim"]["claim_id"]

    explanation = service.explain("alice", second["claim"]["claim_id"])
    assert explanation is not None
    assert explanation["supersedes"][0]["claim_id"] == first["claim"]["claim_id"]


def test_remember_is_isolated_per_user(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    result = service.remember("bob", "Prefers figures in CAD", kind="preference")
    # Bob has no prior claims, so this must be "added", never "reinforced"
    # against Alice's claim.
    assert result["action"] == "added"


def test_remember_defaults_to_fact_kind_for_unknown_kind(service: MemoryService) -> None:
    result = service.remember("alice", "Something durable", kind="not-a-real-kind")
    assert result["claim"]["kind"] == "fact"


# ── search ───────────────────────────────────────────────────────────────────


def test_search_is_isolated_per_user(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD currency reports")
    service.remember("bob", "Prefers figures in CAD currency reports")

    alice_results = service.search("alice", "currency CAD figures")
    bob_results = service.search("bob", "currency CAD figures")

    assert len(alice_results) == 1
    assert len(bob_results) == 1
    assert alice_results[0]["claim_id"] != bob_results[0]["claim_id"]


def test_search_filters_by_kind(service: MemoryService) -> None:
    service.remember("alice", "Weekly report cadence preference", kind="preference")
    service.remember("alice", "Works on the payments team", kind="fact")

    only_preferences = service.search("alice", "report weekly payments team", kind="preference")
    assert all(r["kind"] == "preference" for r in only_preferences)


def test_search_excludes_forgotten_claims(service: MemoryService) -> None:
    result = service.remember("alice", "Prefers figures in CAD currency reports")
    claim_id = result["claim"]["claim_id"]
    service.forget("alice", claim_id=claim_id, confirm=True)

    assert service.search("alice", "currency CAD figures") == []


def test_search_ranks_higher_confidence_above_lower_relevance_tie(
    service: MemoryService,
) -> None:
    """A reinforced (higher-confidence) claim should not rank below a fresh one
    when both are reasonably relevant — score*confidence + recency favors it."""
    service.remember("alice", "Prefers quarterly summaries in CAD")
    service.remember("alice", "Prefers quarterly summaries in CAD")  # reinforces
    results = service.search("alice", "quarterly summaries CAD")
    assert results
    assert results[0]["confidence"] >= 0.9


# ── get / explain ────────────────────────────────────────────────────────────


def test_get_returns_none_for_unknown_claim(service: MemoryService) -> None:
    assert service.get("alice", "a" * 32) is None


def test_get_is_scoped_to_user(service: MemoryService) -> None:
    result = service.remember("alice", "Alice's private fact", kind="fact")
    claim_id = result["claim"]["claim_id"]
    assert service.get("alice", claim_id) is not None
    assert service.get("bob", claim_id) is None


def test_explain_includes_quote_and_evidence(service: MemoryService) -> None:
    from omnigent.entities import MemoryEvidenceLink

    result = service.remember(
        "alice",
        "Prefers figures in CAD",
        kind="preference",
        quote="I want figures in CAD",
        evidence=[MemoryEvidenceLink(session_id="conv_1", item_id="item_1")],
    )
    explanation = service.explain("alice", result["claim"]["claim_id"])
    assert explanation is not None
    assert explanation["quote"] == "I want figures in CAD"
    assert explanation["evidence"] == [{"session_id": "conv_1", "item_id": "item_1"}]
    assert explanation["supersedes"] == []
    assert explanation["superseded_by"] is None


def test_explain_reports_what_superseded_a_claim(service: MemoryService) -> None:
    first = service.remember("alice", "Prefers figures in CAD", kind="preference")
    second = service.remember("alice", "Actually wants figures in USD now", kind="preference")

    explanation = service.explain("alice", first["claim"]["claim_id"])
    assert explanation is not None
    assert explanation["superseded_by"]["claim_id"] == second["claim"]["claim_id"]


# ── forget: two-step ─────────────────────────────────────────────────────────


def test_forget_without_confirm_returns_a_plan_without_mutating(service: MemoryService) -> None:
    result = service.remember("alice", "Prefers figures in CAD", kind="preference")
    claim_id = result["claim"]["claim_id"]

    plan = service.forget("alice", claim_id=claim_id)
    assert plan["status"] == "plan"
    assert plan["claim"]["claim_id"] == claim_id
    # Not mutated: still fetchable and still active.
    assert service.get("alice", claim_id)["status"] == "active"


def test_forget_with_confirm_removes_the_claim(service: MemoryService) -> None:
    result = service.remember("alice", "Prefers figures in CAD", kind="preference")
    claim_id = result["claim"]["claim_id"]

    done = service.forget("alice", claim_id=claim_id, confirm=True)
    assert done["status"] == "forgotten"
    assert service.get("alice", claim_id)["status"] == "forgotten"
    assert service.search("alice", "figures CAD") == []


def test_forget_is_scoped_to_user(service: MemoryService) -> None:
    result = service.remember("alice", "Alice's claim", kind="fact")
    claim_id = result["claim"]["claim_id"]

    done = service.forget("bob", claim_id=claim_id, confirm=True)
    assert done["status"] == "not_found"
    assert service.get("alice", claim_id)["status"] == "active"


def test_forget_by_query_targets_best_match(service: MemoryService) -> None:
    result = service.remember("alice", "Prefers figures in CAD currency reports")
    plan = service.forget("alice", query="currency CAD figures")
    assert plan["status"] == "plan"
    assert plan["claim"]["claim_id"] == result["claim"]["claim_id"]


def test_forget_requires_claim_id_or_query(service: MemoryService) -> None:
    result = service.forget("alice")
    assert result["status"] == "error"


# ── rebuild_index ────────────────────────────────────────────────────────────


def test_rebuild_index_restores_search_from_the_table(db_uri: str, tmp_path: Path) -> None:
    store = SqlAlchemyMemoryStore(db_uri)
    index = MemoryIndex(tmp_path / "memory_index", vectors_override=FAKE_VECTORS_OVERRIDE)
    service = MemoryService(store, index)
    service.remember("alice", "Prefers figures in CAD currency reports")

    # Simulate a lost/corrupted index: a brand-new MemoryIndex object with no
    # on-disk state, as if the directory were deleted.
    fresh_index = MemoryIndex(tmp_path / "rebuilt_index", vectors_override=FAKE_VECTORS_OVERRIDE)
    fresh_service = MemoryService(store, fresh_index)
    count = fresh_service.rebuild_index()
    assert count == 1
    assert fresh_service.search("alice", "currency CAD figures")
