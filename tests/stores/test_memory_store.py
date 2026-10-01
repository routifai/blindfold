"""Tests for :class:`SqlAlchemyMemoryStore`.

Exercises CRUD, per-user isolation, and the reinforce/supersede/forget
lifecycle against a real SQLite database.
"""

from __future__ import annotations

import uuid

import pytest

from omnigent.entities import MemoryEvidenceLink
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore


def _cid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyMemoryStore:
    """A fresh :class:`SqlAlchemyMemoryStore` backed by the test SQLite DB."""
    return SqlAlchemyMemoryStore(db_uri)


def test_create_returns_claim_with_all_fields(store: SqlAlchemyMemoryStore) -> None:
    claim = store.create(
        _cid("c1"),
        "alice",
        "preference",
        "Prefers figures in CAD",
        quote="I want figures in CAD",
        speaker="alice",
        evidence=[MemoryEvidenceLink(session_id="conv_1", item_id="item_1")],
        explicitness="stated",
        confidence=0.9,
    )
    assert claim.id == _cid("c1")
    assert claim.user_id == "alice"
    assert claim.kind == "preference"
    assert claim.claim_text == "Prefers figures in CAD"
    assert claim.quote == "I want figures in CAD"
    assert claim.speaker == "alice"
    assert claim.evidence == [MemoryEvidenceLink(session_id="conv_1", item_id="item_1")]
    assert claim.explicitness == "stated"
    assert claim.confidence == 0.9
    assert claim.status == "active"
    assert claim.reinforcement_count == 0
    assert claim.reinforced_at is None
    assert claim.first_seen > 0


def test_get_returns_none_for_unknown_claim(store: SqlAlchemyMemoryStore) -> None:
    assert store.get(_cid("missing"), "alice") is None


# ── Per-user isolation ──────────────────────────────────────────────────────


def test_get_is_scoped_to_user(store: SqlAlchemyMemoryStore) -> None:
    """User B can't see user A's claim, even with the right claim id."""
    claim = store.create(_cid("c1"), "alice", "fact", "Works on the payments team")
    assert store.get(claim.id, "alice") is not None
    assert store.get(claim.id, "bob") is None


def test_list_active_is_scoped_to_user(store: SqlAlchemyMemoryStore) -> None:
    store.create(_cid("a1"), "alice", "fact", "Alice fact one")
    store.create(_cid("a2"), "alice", "fact", "Alice fact two")
    store.create(_cid("b1"), "bob", "fact", "Bob fact one")

    alice_claims = store.list_active("alice")
    bob_claims = store.list_active("bob")

    assert {c.id for c in alice_claims} == {_cid("a1"), _cid("a2")}
    assert {c.id for c in bob_claims} == {_cid("b1")}


def test_reinforce_is_scoped_to_user(store: SqlAlchemyMemoryStore) -> None:
    claim = store.create(_cid("c1"), "alice", "fact", "Alice fact")
    assert store.reinforce(claim.id, "bob", confidence_increment=0.1) is None
    reinforced = store.reinforce(claim.id, "alice", confidence_increment=0.1)
    assert reinforced is not None
    assert reinforced.confidence == pytest.approx(claim.confidence + 0.1)
    assert reinforced.reinforcement_count == 1
    assert reinforced.reinforced_at is not None


def test_reinforce_caps_confidence_at_one(store: SqlAlchemyMemoryStore) -> None:
    claim = store.create(_cid("c1"), "alice", "fact", "Alice fact", confidence=0.95)
    reinforced = store.reinforce(claim.id, "alice", confidence_increment=0.5)
    assert reinforced is not None
    assert reinforced.confidence == 1.0


def test_reinforce_returns_none_for_non_active_claim(store: SqlAlchemyMemoryStore) -> None:
    claim = store.create(_cid("c1"), "alice", "fact", "Alice fact")
    store.set_status(claim.id, "alice", "forgotten")
    assert store.reinforce(claim.id, "alice", confidence_increment=0.1) is None


# ── Supersede ────────────────────────────────────────────────────────────────


def test_supersede_marks_old_claim_superseded_and_links_new_one(
    store: SqlAlchemyMemoryStore,
) -> None:
    old = store.create(_cid("old"), "alice", "preference", "Wants figures in CAD")
    new = store.supersede(
        old.id,
        "alice",
        new_claim_id=_cid("new"),
        kind="preference",
        claim_text="Wants figures in USD",
        quote="actually USD now",
    )
    assert new.id == _cid("new")
    assert new.status == "active"
    assert new.supersedes_claim_id == old.id

    reloaded_old = store.get(old.id, "alice")
    assert reloaded_old is not None
    assert reloaded_old.status == "superseded"

    successor = store.find_successor(old.id, "alice")
    assert successor is not None
    assert successor.id == new.id


def test_supersede_does_not_affect_another_users_claim(store: SqlAlchemyMemoryStore) -> None:
    """Superseding with someone else's claim id as old_claim_id leaves it untouched."""
    bobs = store.create(_cid("bob1"), "bob", "fact", "Bob's claim")
    store.supersede(
        bobs.id,
        "alice",
        new_claim_id=_cid("alice1"),
        kind="fact",
        claim_text="Alice's new claim",
    )
    reloaded_bob = store.get(bobs.id, "bob")
    assert reloaded_bob is not None
    assert reloaded_bob.status == "active"


# ── set_status / forget ─────────────────────────────────────────────────────


def test_set_status_transitions_and_is_scoped_to_user(store: SqlAlchemyMemoryStore) -> None:
    claim = store.create(_cid("c1"), "alice", "fact", "Alice fact")
    assert store.set_status(claim.id, "bob", "forgotten") is None
    forgotten = store.set_status(claim.id, "alice", "forgotten")
    assert forgotten is not None
    assert forgotten.status == "forgotten"
    assert forgotten.updated_at is not None


def test_list_active_excludes_forgotten_and_superseded(store: SqlAlchemyMemoryStore) -> None:
    active = store.create(_cid("active"), "alice", "fact", "Still true")
    forgotten = store.create(_cid("forgotten"), "alice", "fact", "Forget this")
    store.set_status(forgotten.id, "alice", "forgotten")
    old = store.create(_cid("old"), "alice", "fact", "Old fact")
    store.supersede(
        old.id, "alice", new_claim_id=_cid("newfact"), kind="fact", claim_text="New fact"
    )

    remaining_ids = {c.id for c in store.list_active("alice")}
    assert active.id in remaining_ids
    assert forgotten.id not in remaining_ids
    assert old.id not in remaining_ids
    assert _cid("newfact") in remaining_ids


def test_list_active_filters_by_kind_and_min_confidence(store: SqlAlchemyMemoryStore) -> None:
    store.create(_cid("pref"), "alice", "preference", "Pref claim", confidence=0.9)
    store.create(_cid("fact"), "alice", "fact", "Fact claim", confidence=0.3)

    prefs = store.list_active("alice", kind="preference")
    assert {c.id for c in prefs} == {_cid("pref")}

    high_conf = store.list_active("alice", min_confidence=0.5)
    assert {c.id for c in high_conf} == {_cid("pref")}


def test_list_all_active_spans_users(store: SqlAlchemyMemoryStore) -> None:
    store.create(_cid("a1"), "alice", "fact", "Alice fact")
    store.create(_cid("b1"), "bob", "fact", "Bob fact")
    all_active = store.list_all_active()
    assert {c.id for c in all_active} == {_cid("a1"), _cid("b1")}
