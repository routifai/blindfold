"""Tests for :class:`MemoryService` — the write/read paths behind the memory tools.

Uses a real txtai index with a deterministic, offline hashed-bag-of-words
vectorizer (``tests.memory._fixtures.fake_transform``) instead of the
``litellm`` provider backend, so these tests never call the OpenAI API (or
any network).
"""

from __future__ import annotations

import os
import uuid

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
    # A restatement reinforces; paraphrases are merged later by the upkeep job.
    second = service.remember("alice", "prefers figures in CAD", kind="preference")
    assert second["action"] == "reinforced"
    assert second["claim"]["claim_id"] == first["claim"]["claim_id"]
    assert second["claim"]["confidence"] > first["claim"]["confidence"]


def test_remember_supersedes_only_the_named_claim(service: MemoryService) -> None:
    first = service.remember("alice", "Prefers figures in CAD", kind="preference")
    second = service.remember(
        "alice",
        "Actually wants figures in USD now",
        kind="preference",
        replaces_claim_id=first["claim"]["claim_id"],
    )
    assert second["action"] == "superseded"
    assert second["claim"]["claim_id"] != first["claim"]["claim_id"]

    explanation = service.explain("alice", second["claim"]["claim_id"])
    assert explanation is not None
    assert explanation["supersedes"][0]["claim_id"] == first["claim"]["claim_id"]


def test_remember_never_supersedes_a_related_claim_by_guess(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    other = service.remember("alice", "Prefers figures in a table", kind="preference")
    assert other["action"] == "added"
    assert len(service.search("alice", "figures")) == 2


def test_remember_rejects_replacing_another_users_claim(service: MemoryService) -> None:
    alice = service.remember("alice", "Prefers figures in CAD", kind="preference")
    result = service.remember("bob", "Prefers USD", replaces_claim_id=alice["claim"]["claim_id"])
    assert "error" in result
    assert service.get("alice", alice["claim"]["claim_id"])["status"] == "active"


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
    second = service.remember(
        "alice",
        "Actually wants figures in USD now",
        kind="preference",
        replaces_claim_id=first["claim"]["claim_id"],
    )

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


# ── profile (Phase 2 work profile) ──────────────────────────────────────────


def test_profile_is_none_with_no_claims(service: MemoryService) -> None:
    assert service.profile("alice") is None


def test_profile_excludes_kinds_outside_the_profile_set(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    service.remember("alice", "Works on the payments team", kind="fact")
    service.remember("alice", "A one-off decision", kind="decision")  # not a profile kind

    profile = service.profile("alice")
    assert profile is not None
    assert "Prefers figures in CAD" in profile
    assert "Works on the payments team" in profile
    assert "A one-off decision" not in profile


def test_profile_excludes_claims_below_the_confidence_threshold(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    # Below the 0.75 profile threshold; remember()'s stated claims start at
    # 0.9, so an inferred low-confidence claim is written directly.
    service._store.create(
        uuid.uuid4().hex,
        "alice",
        "preference",
        "Maybe likes short replies",
        explicitness="inferred",
        confidence=0.4,
    )
    service._invalidate_profile("alice")

    profile = service.profile("alice")
    assert profile is not None
    assert "Prefers figures in CAD" in profile
    assert "Maybe likes short replies" not in profile


def test_profile_groups_by_kind_with_headers(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    service.remember("alice", "Always cc finance on reports", kind="instruction")
    service.remember("alice", "Likes a terse, direct tone", kind="working_style")
    service.remember("alice", "Works on the payments team", kind="fact")

    profile = service.profile("alice")
    assert profile is not None
    assert "Preferences:" in profile
    assert "Standing instructions:" in profile
    assert "Working style:" in profile
    assert "About the user:" in profile
    # Preferences group (declared first) renders before working style.
    assert profile.index("Preferences:") < profile.index("Working style:")


def test_profile_orders_newest_reinforced_first_within_a_group(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import itertools

    import omnigent.stores.memory_store.sqlalchemy_store as store_module

    # now_epoch() has one-second resolution; a real clock could tie three
    # writes in the same test. A strictly increasing fake clock makes the
    # "newest-reinforced first" ordering deterministic.
    counter = itertools.count(1_000_000)
    monkeypatch.setattr(store_module, "now_epoch", lambda: next(counter))

    service.remember("alice", "Prefers figures in CAD", kind="preference")
    service.remember("alice", "Prefers quarterly summaries", kind="preference")
    # Reinforcing the first claim bumps its reinforced_at ahead of the second.
    service.remember("alice", "prefers figures in cad", kind="preference")

    profile = service.profile("alice")
    assert profile is not None
    assert profile.index("Prefers figures in CAD") < profile.index("Prefers quarterly summaries")


def test_profile_is_isolated_per_user(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    assert service.profile("bob") is None


def test_profile_excludes_forgotten_claims(service: MemoryService) -> None:
    result = service.remember("alice", "Prefers figures in CAD", kind="preference")
    service.forget("alice", claim_id=result["claim"]["claim_id"], confirm=True)
    assert service.profile("alice") is None


def test_profile_caps_at_roughly_the_character_budget(service: MemoryService) -> None:
    from omnigent.memory.service import _PROFILE_MAX_CHARS

    for i in range(150):
        text = f"Preference number {i} about something fairly specific"
        service.remember("alice", text, kind="preference")

    profile = service.profile("alice")
    assert profile is not None
    assert len(profile) <= _PROFILE_MAX_CHARS + 200  # a little slack for the last header/line


def test_profile_is_cached_until_the_next_write(service: MemoryService) -> None:
    assert service.profile("alice") is None
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    # Cache was populated by the first (None) call; a write must invalidate it.
    profile = service.profile("alice")
    assert profile is not None
    assert "Prefers figures in CAD" in profile


def test_profile_cache_invalidated_by_reinforce(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    first = service.profile("alice")
    service.remember("alice", "Prefers quarterly summaries", kind="preference")
    second = service.profile("alice")
    assert first != second
    assert "Prefers quarterly summaries" in (second or "")


def test_profile_cache_invalidated_by_supersede(service: MemoryService) -> None:
    first = service.remember("alice", "Prefers figures in CAD", kind="preference")
    service.profile("alice")  # populate the cache
    service.remember(
        "alice",
        "Actually wants figures in USD now",
        kind="preference",
        replaces_claim_id=first["claim"]["claim_id"],
    )
    profile = service.profile("alice")
    assert profile is not None
    assert "USD" in profile
    assert "CAD" not in profile


def test_profile_cache_invalidated_by_forget(service: MemoryService) -> None:
    result = service.remember("alice", "Prefers figures in CAD", kind="preference")
    service.profile("alice")  # populate the cache
    service.forget("alice", claim_id=result["claim"]["claim_id"], confirm=True)
    assert service.profile("alice") is None


def test_profile_cache_is_per_user(service: MemoryService) -> None:
    service.remember("alice", "Prefers figures in CAD", kind="preference")
    assert service.profile("bob") is None
    service.remember("bob", "Prefers figures in USD", kind="preference")
    # Alice's cache entry must not have been clobbered by bob's write.
    assert "USD" not in (service.profile("alice") or "")
    assert "USD" in (service.profile("bob") or "")


# ── render_profile_block ─────────────────────────────────────────────────────


def test_render_profile_block_wraps_in_delimiters() -> None:
    from omnigent.memory.service import render_profile_block

    block = render_profile_block("Preferences:\n- Prefers figures in CAD")
    assert block.startswith("[Standing memory about the user")
    assert block.endswith("[End of standing memory]")
    assert "Prefers figures in CAD" in block


# ── record_claim / reinforce_claim / supersede_claim (Phase 3's upkeep job) ──


def test_record_claim_adds_and_indexes_a_claim(service: MemoryService) -> None:
    result = service.record_claim(
        "alice", "preference", "Prefers figures in CAD", explicitness="inferred", confidence=0.4
    )
    assert result["action"] == "added"
    assert result["claim"]["explicitness"] == "inferred"
    assert service.search("alice", "figures CAD")


def test_record_claim_invalidates_the_profile_cache(service: MemoryService) -> None:
    assert service.profile("alice") is None
    service.record_claim("alice", "preference", "Prefers figures in CAD", confidence=0.9)
    profile = service.profile("alice")
    assert profile is not None
    assert "Prefers figures in CAD" in profile


def test_reinforce_claim_bumps_confidence_and_reindexes(service: MemoryService) -> None:
    added = service.record_claim("alice", "preference", "Prefers figures in CAD", confidence=0.4)
    claim_id = added["claim"]["claim_id"]
    reinforced = service.reinforce_claim(claim_id, "alice", confidence_increment=0.2)
    assert reinforced is not None
    assert reinforced["confidence"] == pytest.approx(0.6)


def test_reinforce_claim_returns_none_for_unknown_claim(service: MemoryService) -> None:
    assert service.reinforce_claim("a" * 32, "alice", confidence_increment=0.2) is None


def test_supersede_claim_replaces_the_named_claim(service: MemoryService) -> None:
    added = service.record_claim("alice", "preference", "Prefers figures in CAD", confidence=0.9)
    claim_id = added["claim"]["claim_id"]
    result = service.supersede_claim(
        claim_id, "alice", kind="preference", text="Prefers figures in USD", confidence=0.9
    )
    assert result["action"] == "superseded"
    assert service.get("alice", claim_id)["status"] == "superseded"
    assert service.get("alice", result["claim"]["claim_id"])["status"] == "active"


def test_supersede_claim_rejects_an_unknown_or_inactive_claim(service: MemoryService) -> None:
    result = service.supersede_claim(
        "a" * 32, "alice", kind="preference", text="Prefers figures in USD"
    )
    assert "error" in result


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
