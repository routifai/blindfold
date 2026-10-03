"""Tests for Phase 3's memory upkeep pipeline (``omnigent/memory/upkeep.py``).

Pure pipeline logic (window/gate/verify/apply/run) is tested here against
fakes — no txtai, no network, no real LLM call. The run-record store is the
real :class:`SqlAlchemyMemoryUpkeepStore` (SQLite, no extra dependency)
since exercising it for real is cheap and catches lease/watermark bugs a
fake store could hide.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.memory import upkeep
from omnigent.stores.memory_upkeep_store.sqlalchemy_store import SqlAlchemyMemoryUpkeepStore


def _rid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


# ── fakes ────────────────────────────────────────────────────────────────


class _FakePagedList:
    def __init__(self, data: list[Any], *, has_more: bool, last_id: str | None) -> None:
        self.data = data
        self.has_more = has_more
        self.last_id = last_id


def _fake_item(item_id: str, created_at: int, role: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id,
        created_at=created_at,
        data=SimpleNamespace(role=role, content=[{"type": "input_text", "text": text}]),
    )


class FakeConversationStore:
    """Duck-typed stand-in for the two ``ConversationStore`` methods
    ``gather_window`` calls. Conversations and items must already be
    sorted newest-first (matches real store's ``order="desc"`` contract)."""

    def __init__(
        self,
        conversations: list[SimpleNamespace],
        items_by_session: dict[str, list[SimpleNamespace]],
    ) -> None:
        self._conversations = conversations
        self._items_by_session = items_by_session
        self.list_items_calls: list[str] = []

    def list_conversations(
        self,
        *,
        owned_by: str | None = None,
        kind: str | None = None,
        limit: int = 20,
        after: str | None = None,
        order: str = "desc",
        sort_by: str = "updated_at",
    ) -> _FakePagedList:
        del owned_by, kind, order, sort_by  # fake assumes the caller already filtered
        start = 0
        if after is not None:
            idx = next((i for i, c in enumerate(self._conversations) if c.id == after), None)
            if idx is not None:
                start = idx + 1
        page = self._conversations[start : start + limit]
        has_more = start + limit < len(self._conversations)
        return _FakePagedList(page, has_more=has_more, last_id=page[-1].id if page else None)

    def list_items(
        self,
        session_id: str,
        *,
        limit: int = 100,
        before: str | None = None,
        order: str = "desc",
        type: str | None = None,
    ) -> _FakePagedList:
        del order, type
        self.list_items_calls.append(session_id)
        items = self._items_by_session.get(session_id, [])
        start = 0
        if before is not None:
            idx = next((i for i, it in enumerate(items) if it.id == before), None)
            if idx is not None:
                start = idx + 1
        page = items[start : start + limit]
        has_more = start + limit < len(items)
        return _FakePagedList(page, has_more=has_more, last_id=page[-1].id if page else None)


class FakeMemory:
    """Duck-typed stand-in for :class:`~omnigent.memory.service.MemoryService`."""

    def __init__(self, seed_claims: list[dict[str, Any]] | None = None) -> None:
        self.claims: dict[str, dict[str, Any]] = {}
        self._next_id = 0
        for claim in seed_claims or []:
            self.claims[claim["claim_id"]] = dict(claim)

    def _new_id(self) -> str:
        self._next_id += 1
        return f"claim{self._next_id}"

    def search(
        self, user_id: str, query: str, *, kind: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        query_words = set(query.lower().split())
        results = []
        for claim in self.claims.values():
            if claim["user_id"] != user_id or claim["status"] != "active":
                continue
            if kind is not None and claim["kind"] != kind:
                continue
            if query_words & set(claim["text"].lower().split()):
                results.append(dict(claim))
        return results[:limit]

    def record_claim(
        self,
        user_id: str,
        kind: str,
        text: str,
        *,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: Any = None,
        explicitness: str = "stated",
        confidence: float = 0.9,
        run_id: str | None = None,
        valid_until: int | None = None,
    ) -> dict[str, Any]:
        del quote, speaker, evidence, run_id, valid_until
        claim_id = self._new_id()
        claim = {
            "claim_id": claim_id,
            "user_id": user_id,
            "kind": kind,
            "text": text,
            "status": "active",
            "confidence": confidence,
            "explicitness": explicitness,
        }
        self.claims[claim_id] = claim
        return dict(claim)

    def reinforce_claim(
        self, claim_id: str, user_id: str, *, confidence_increment: float
    ) -> dict[str, Any] | None:
        claim = self.claims.get(claim_id)
        if claim is None or claim["user_id"] != user_id or claim["status"] != "active":
            return None
        claim["confidence"] = min(1.0, claim["confidence"] + confidence_increment)
        return dict(claim)

    def supersede_claim(
        self,
        old_claim_id: str,
        user_id: str,
        *,
        kind: str,
        text: str,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: Any = None,
        explicitness: str = "stated",
        confidence: float = 0.9,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        del quote, speaker, evidence, run_id
        old = self.claims.get(old_claim_id)
        if old is None or old["user_id"] != user_id or old["status"] != "active":
            return {"error": f"no active claim {old_claim_id}"}
        old["status"] = "superseded"
        new_id = self._new_id()
        new_claim = {
            "claim_id": new_id,
            "user_id": user_id,
            "kind": kind,
            "text": text,
            "status": "active",
            "confidence": confidence,
            "explicitness": explicitness,
        }
        self.claims[new_id] = new_claim
        return {"claim": dict(new_claim)}


class StubLLM:
    """Stubbed LLM caller: canned extraction JSON, queued classify JSON."""

    def __init__(
        self, extraction_response: str, classify_responses: list[str] | None = None
    ) -> None:
        self.extraction_response = extraction_response
        self.classify_responses = list(classify_responses or [])
        self.extraction_calls: list[str] = []
        self.classify_calls: list[str] = []

    async def __call__(self, *, instructions: str, input_text: str) -> str:
        if instructions == upkeep.MEMORY_UPKEEP_EXTRACTION_PROMPT:
            self.extraction_calls.append(input_text)
            return self.extraction_response
        self.classify_calls.append(input_text)
        if self.classify_responses:
            return self.classify_responses.pop(0)
        return '{"relation": "new", "claim_id": null}'


# ── window / watermark ───────────────────────────────────────────────────


def test_gather_window_only_returns_items_after_the_watermark() -> None:
    items = [
        _fake_item("i3", 30, "user", "third"),
        _fake_item("i2", 20, "assistant", "second (assistant)"),
        _fake_item("i1", 10, "user", "first"),
    ]
    store = FakeConversationStore(
        conversations=[SimpleNamespace(id="conv1", updated_at=30)],
        items_by_session={"conv1": items},
    )
    window = upkeep.gather_window(store, "alice", 10)
    assert [w.item_id for w in window] == ["i2", "i3"]
    assert [w.created_at for w in window] == [20, 30]  # ascending (chronological)


def test_gather_window_prunes_sessions_untouched_since_watermark() -> None:
    store = FakeConversationStore(
        conversations=[
            SimpleNamespace(id="active", updated_at=100),
            SimpleNamespace(id="stale", updated_at=5),
        ],
        items_by_session={
            "active": [_fake_item("i1", 50, "user", "hi")],
            "stale": [_fake_item("i2", 50, "user", "should never be scanned")],
        },
    )
    window = upkeep.gather_window(store, "alice", since=10)
    assert [w.item_id for w in window] == ["i1"]
    # The pruned (stale) session's items must never even be fetched.
    assert "stale" not in store.list_items_calls


def test_gather_window_excludes_non_message_roles() -> None:
    items = [_fake_item("i1", 10, "system", "framework notice")]
    store = FakeConversationStore(
        conversations=[SimpleNamespace(id="conv1", updated_at=10)],
        items_by_session={"conv1": items},
    )
    assert upkeep.gather_window(store, "alice", 0) == []


# ── gate ─────────────────────────────────────────────────────────────────


def test_gate_requires_minimum_substantive_user_messages() -> None:
    def _items(n: int) -> list[upkeep.WindowItem]:
        return [
            upkeep.WindowItem(
                session_id="s", item_id=f"i{i}", role="user", text=f"msg {i}", created_at=i
            )
            for i in range(n)
        ]

    assert upkeep.gate(_items(2)) is False
    assert upkeep.gate(_items(3)) is True


def test_gate_ignores_assistant_items_and_blank_user_items() -> None:
    items = [
        upkeep.WindowItem(session_id="s", item_id="a1", role="assistant", text="hi", created_at=1),
        upkeep.WindowItem(session_id="s", item_id="u1", role="user", text="  ", created_at=2),
        upkeep.WindowItem(session_id="s", item_id="u2", role="user", text="real", created_at=3),
    ]
    assert upkeep.gate(items, min_user_messages=2) is False


# ── verify ───────────────────────────────────────────────────────────────


def _user_item(item_id: str, text: str) -> upkeep.WindowItem:
    return upkeep.WindowItem(
        session_id="conv1", item_id=item_id, role="user", text=text, created_at=1
    )


def _raw_candidate(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "preference",
        "claim_text": "Prefers figures in CAD",
        "quote": "I want figures in CAD please",
        "item_id": "i1",
        "explicitness": "stated",
        "valid_until": None,
    }
    base.update(overrides)
    return base


def test_verify_accepts_a_verbatim_quote() -> None:
    items_by_id = {"i1": _user_item("i1", "I want figures in CAD please, thanks")}
    candidate, reason = upkeep.verify_candidate(_raw_candidate(), items_by_id)
    assert reason is None
    assert candidate is not None
    assert candidate.kind == "preference"
    assert candidate.session_id == "conv1"


def test_verify_rejects_a_non_verbatim_quote() -> None:
    items_by_id = {"i1": _user_item("i1", "I'd like CAD figures, if possible")}
    candidate, reason = upkeep.verify_candidate(_raw_candidate(), items_by_id)
    assert candidate is None
    assert reason == "quote_not_verbatim"


def test_verify_rejects_an_unknown_item_id() -> None:
    """Covers the non-user-source guard: a quote from a tool result, a
    pasted document, or assistant text never lands in ``items_by_id``
    (only ``role == "user"`` window items do), so citing it is rejected."""
    items_by_id = {"i1": _user_item("i1", "something else entirely")}
    candidate, reason = upkeep.verify_candidate(
        _raw_candidate(item_id="not_in_window"), items_by_id
    )
    assert candidate is None
    assert reason == "unknown_item"


@pytest.mark.parametrize(
    "quote",
    [
        "my password: hunter2hunter2",
        "api_key: sk-abcdefghijklmnopqrstuvwx",
        "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
def test_verify_rejects_secrets(quote: str) -> None:
    items_by_id = {"i1": _user_item("i1", quote)}
    candidate, reason = upkeep.verify_candidate(_raw_candidate(quote=quote), items_by_id)
    assert candidate is None
    assert reason == "secret"


def test_verify_rejects_one_time_markers() -> None:
    quote = "just use CAD for this report"
    items_by_id = {"i1": _user_item("i1", quote)}
    candidate, reason = upkeep.verify_candidate(_raw_candidate(quote=quote), items_by_id)
    assert candidate is None
    assert reason == "one_time"


def test_verify_rejects_malformed_candidates() -> None:
    items_by_id = {"i1": _user_item("i1", "text")}
    candidate, reason = upkeep.verify_candidate({"kind": "preference"}, items_by_id)
    assert candidate is None
    assert reason == "malformed"
    candidate, reason = upkeep.verify_candidate("not a dict", items_by_id)
    assert reason == "malformed"


def test_verify_rejects_invalid_kind_and_explicitness() -> None:
    items_by_id = {"i1": _user_item("i1", "I want figures in CAD please")}
    _, reason = upkeep.verify_candidate(_raw_candidate(kind="nope"), items_by_id)
    assert reason == "invalid_kind"
    _, reason = upkeep.verify_candidate(_raw_candidate(explicitness="definitely"), items_by_id)
    assert reason == "invalid_explicitness"


def test_verify_rejects_overlong_claim_text() -> None:
    long_text = "x" * (upkeep.MAX_CLAIM_TEXT_CHARS + 1)
    items_by_id = {"i1": _user_item("i1", "I want figures in CAD please")}
    _, reason = upkeep.verify_candidate(_raw_candidate(claim_text=long_text), items_by_id)
    assert reason == "overlong"


# ── apply ────────────────────────────────────────────────────────────────


def _verified(**overrides: Any) -> upkeep.VerifiedCandidate:
    base: dict[str, Any] = {
        "kind": "preference",
        "claim_text": "Prefers figures in CAD",
        "quote": "I want figures in CAD",
        "item_id": "i1",
        "session_id": "conv1",
        "explicitness": "stated",
        "valid_until": None,
    }
    base.update(overrides)
    return upkeep.VerifiedCandidate(**base)


async def test_apply_inserts_when_no_matches() -> None:
    memory = FakeMemory()
    llm = StubLLM(extraction_response="{}")
    outcome = await upkeep.apply_candidate(memory, "alice", _verified(), "run1", llm)
    assert outcome == "inserted"
    assert len(memory.claims) == 1
    assert not llm.classify_calls  # no matches -> no classify call needed


async def test_apply_reinforces_on_same() -> None:
    memory = FakeMemory(
        seed_claims=[
            {
                "claim_id": "existing",
                "user_id": "alice",
                "kind": "preference",
                "text": "Prefers figures in CAD",
                "status": "active",
                "confidence": 0.9,
            }
        ]
    )
    llm = StubLLM(
        extraction_response="{}",
        classify_responses=['{"relation": "same", "claim_id": "existing"}'],
    )
    outcome = await upkeep.apply_candidate(memory, "alice", _verified(), "run1", llm)
    assert outcome == "reinforced"
    assert memory.claims["existing"]["confidence"] == pytest.approx(0.95)


async def test_apply_supersedes_on_named_contradiction() -> None:
    memory = FakeMemory(
        seed_claims=[
            {
                "claim_id": "existing",
                "user_id": "alice",
                "kind": "preference",
                "text": "Prefers figures in USD",
                "status": "active",
                "confidence": 0.9,
            }
        ]
    )
    llm = StubLLM(
        extraction_response="{}",
        classify_responses=['{"relation": "contradicts", "claim_id": "existing"}'],
    )
    outcome = await upkeep.apply_candidate(
        memory, "alice", _verified(claim_text="Prefers figures in CAD"), "run1", llm
    )
    assert outcome == "superseded"
    assert memory.claims["existing"]["status"] == "superseded"
    assert any(
        c["status"] == "active" and c["text"] == "Prefers figures in CAD"
        for c in memory.claims.values()
    )


async def test_apply_never_acts_on_an_unnamed_contradiction() -> None:
    """The classifier saying "contradicts" with no (or an unmatched)
    claim_id must change nothing — supersession only ever targets a claim
    the classifier named explicitly."""
    memory = FakeMemory(
        seed_claims=[
            {
                "claim_id": "existing",
                "user_id": "alice",
                "kind": "preference",
                "text": "Prefers figures in USD",
                "status": "active",
                "confidence": 0.9,
            }
        ]
    )
    llm = StubLLM(
        extraction_response="{}",
        classify_responses=['{"relation": "contradicts", "claim_id": null}'],
    )
    outcome = await upkeep.apply_candidate(
        memory, "alice", _verified(claim_text="Prefers figures in CAD"), "run1", llm
    )
    assert outcome == "contradiction_not_named"
    assert memory.claims["existing"]["status"] == "active"
    assert len(memory.claims) == 1


# ── run_upkeep_once ──────────────────────────────────────────────────────


@pytest.fixture()
def upkeep_store(db_uri: str) -> SqlAlchemyMemoryUpkeepStore:
    return SqlAlchemyMemoryUpkeepStore(db_uri)


def _conv_store_with_messages(user_texts: list[str]) -> FakeConversationStore:
    items = [_fake_item(f"u{i}", (i + 1) * 10, "user", text) for i, text in enumerate(user_texts)]
    return FakeConversationStore(
        conversations=[SimpleNamespace(id="conv1", updated_at=len(items) * 10)],
        items_by_session={"conv1": items},
    )


async def test_run_upkeep_once_gate_skip_preserves_watermark(
    upkeep_store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    conv_store = _conv_store_with_messages(["one message only"])
    run = await upkeep.run_upkeep_once(
        user_id="alice",
        conversation_store=conv_store,
        memory=FakeMemory(),
        upkeep_store=upkeep_store,
        llm_caller=StubLLM(extraction_response="{}"),
        now=1000,
    )
    assert run is not None
    assert run.state == "skipped"
    assert run.disposition == "no_new_signal"
    assert run.counts["seen"] == 1
    assert upkeep_store.get_last_succeeded_run("alice") is None


async def test_run_upkeep_once_skips_when_no_llm_configured(
    upkeep_store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    conv_store = _conv_store_with_messages(["aa", "bb", "cc"])
    run = await upkeep.run_upkeep_once(
        user_id="alice",
        conversation_store=conv_store,
        memory=FakeMemory(),
        upkeep_store=upkeep_store,
        llm_caller=None,
        now=1000,
    )
    assert run is not None
    assert run.state == "skipped"
    assert run.disposition == "no_llm_configured"


async def test_run_upkeep_once_records_counts_and_advances_watermark(
    upkeep_store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    conv_store = _conv_store_with_messages(
        ["I prefer figures in CAD", "please always use CAD", "one more message"]
    )
    extraction = (
        '{"candidates": [{"kind": "preference", '
        '"claim_text": "Prefers figures in CAD", '
        '"quote": "I prefer figures in CAD", "item_id": "u0", '
        '"explicitness": "stated", "valid_until": null}]}'
    )
    llm = StubLLM(extraction_response=extraction)
    memory = FakeMemory()

    run = await upkeep.run_upkeep_once(
        user_id="alice",
        conversation_store=conv_store,
        memory=memory,
        upkeep_store=upkeep_store,
        llm_caller=llm,
        now=1000,
    )
    assert run is not None
    assert run.state == "succeeded"
    assert run.disposition == "ok"
    assert run.counts["seen"] == 3
    assert run.counts["candidates"] == 1
    assert run.counts["inserted"] == 1
    assert run.counts["rejected"] == 0
    assert len(memory.claims) == 1

    watermark = upkeep_store.get_last_succeeded_run("alice")
    assert watermark is not None
    assert watermark.window_until == 1000


async def test_run_upkeep_once_rejects_candidates_citing_non_user_sources(
    upkeep_store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    """A candidate citing an item outside the window (e.g. a pasted
    document surfaced only in a tool result, or assistant text) must
    create nothing, and must be counted as rejected."""
    conv_store = _conv_store_with_messages(["aa", "bb", "cc"])
    extraction = (
        '{"candidates": [{"kind": "fact", "claim_text": "The user prefers X", '
        '"quote": "the user prefers X", "item_id": "not_a_user_item", '
        '"explicitness": "inferred", "valid_until": null}]}'
    )
    memory = FakeMemory()
    run = await upkeep.run_upkeep_once(
        user_id="alice",
        conversation_store=conv_store,
        memory=memory,
        upkeep_store=upkeep_store,
        llm_caller=StubLLM(extraction_response=extraction),
        now=1000,
    )
    assert run is not None
    assert run.state == "succeeded"
    assert run.counts["rejected"] == 1
    assert run.counts["rejected_reasons"] == {"unknown_item": 1}
    assert memory.claims == {}


async def test_run_upkeep_once_single_run_lock(
    upkeep_store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    started = upkeep_store.start_run(_rid("already-running"), "alice", 0, 500)
    assert started is not None

    run = await upkeep.run_upkeep_once(
        user_id="alice",
        conversation_store=_conv_store_with_messages(["aa", "bb", "cc"]),
        memory=FakeMemory(),
        upkeep_store=upkeep_store,
        llm_caller=StubLLM(extraction_response="{}"),
        now=1000,
    )
    assert run is None


async def test_run_upkeep_once_records_a_failed_run_on_unexpected_error(
    upkeep_store: SqlAlchemyMemoryUpkeepStore,
) -> None:
    class ExplodingLLM:
        async def __call__(self, *, instructions: str, input_text: str) -> str:
            raise RuntimeError("boom")

    conv_store = _conv_store_with_messages(["aa", "bb", "cc"])
    with pytest.raises(RuntimeError, match="boom"):
        await upkeep.run_upkeep_once(
            user_id="alice",
            conversation_store=conv_store,
            memory=FakeMemory(),
            upkeep_store=upkeep_store,
            llm_caller=ExplodingLLM(),
            now=1000,
        )

    runs = upkeep_store.list_runs("alice")
    assert len(runs) == 1
    assert runs[0].state == "failed"
    # The lease is released even on failure — a later run can proceed.
    assert upkeep_store.has_running_run("alice") is False
