"""Unit tests for the ``memory_*`` built-in tools.

Uses a real :class:`MemoryService` (SQLite store + a deterministic, offline
txtai index — no OpenAI calls) and a minimal stub conversation store so the
tools' user-scoping (``resolve_memory_user``) is exercised against real
owner-resolution behavior without the full session/permission store stack.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import pytest

pytest.importorskip("txtai")

from pathlib import Path

from omnigent.memory.index import MemoryIndex
from omnigent.memory.service import MemoryService
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.memory import (
    MemoryExplainTool,
    MemoryForgetTool,
    MemoryGetTool,
    MemoryRememberTool,
    MemorySearchTool,
    resolve_memory_user,
)
from tests.memory._fixtures import FAKE_VECTORS_OVERRIDE


class _StubConversationStore:
    """Minimal stand-in: maps conversation_id -> owner user_id."""

    def __init__(self, owners: dict[str, str]) -> None:
        self._owners = owners

    def get_session_owner(self, conversation_id: str, *, owner_only: bool = False) -> str | None:
        del owner_only
        return self._owners.get(conversation_id)


@dataclass
class _Fixture:
    service: MemoryService
    ctx_alice: ToolContext
    ctx_bob: ToolContext
    ctx_no_owner: ToolContext
    ctx_no_session: ToolContext


@pytest.fixture()
def fixture(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    store = SqlAlchemyMemoryStore(db_uri)
    index = MemoryIndex(tmp_path / "idx", vectors_override=FAKE_VECTORS_OVERRIDE)
    service = MemoryService(store, index)

    conv_store = _StubConversationStore({"conv_alice": "alice", "conv_bob": "bob"})
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: conv_store)
    monkeypatch.setattr("omnigent.runtime.get_memory_service", lambda: service)

    return _Fixture(
        service=service,
        ctx_alice=ToolContext(task_id="t", agent_id="a", conversation_id="conv_alice"),
        ctx_bob=ToolContext(task_id="t", agent_id="a", conversation_id="conv_bob"),
        ctx_no_owner=ToolContext(task_id="t", agent_id="a", conversation_id="conv_unowned"),
        ctx_no_session=ToolContext(task_id="t", agent_id="a", conversation_id=None),
    )


# ── resolve_memory_user ──────────────────────────────────────────────────────


def test_resolve_memory_user_requires_a_session(fixture: _Fixture) -> None:
    user_id, error = resolve_memory_user(fixture.ctx_no_session)
    assert user_id is None
    assert error is not None


def test_resolve_memory_user_falls_back_to_local_without_an_owner_grant(
    fixture: _Fixture,
) -> None:
    """No permission store / no owner grant (single-user server) still resolves."""
    user_id, error = resolve_memory_user(fixture.ctx_no_owner)
    assert error is None
    assert user_id == "local"


def test_resolve_memory_user_returns_the_session_owner(fixture: _Fixture) -> None:
    user_id, error = resolve_memory_user(fixture.ctx_alice)
    assert error is None
    assert user_id == "alice"


# ── registration: framework-owned, not spec-instantiable ────────────────────


@pytest.mark.parametrize(
    "name", ["memory_remember", "memory_search", "memory_get", "memory_explain", "memory_forget"]
)
def test_memory_tools_are_not_instantiable_via_spec(name: str) -> None:
    """Like session_history, memory_* is reserved but not a spec-enablable builtin."""
    assert get_builtin_tool(name) is None


# ── memory_remember ──────────────────────────────────────────────────────────


def test_remember_writes_a_claim_scoped_to_the_session_owner(fixture: _Fixture) -> None:
    tool = MemoryRememberTool()
    args = json.dumps({"text": "Prefers figures in CAD", "kind": "preference"})
    result = json.loads(tool.invoke(args, fixture.ctx_alice))
    assert result["action"] == "added"
    assert fixture.service.get("alice", result["claim"]["claim_id"]) is not None
    assert fixture.service.get("bob", result["claim"]["claim_id"]) is None


def test_remember_requires_non_empty_text(fixture: _Fixture) -> None:
    tool = MemoryRememberTool()
    result = json.loads(tool.invoke(json.dumps({"text": "   "}), fixture.ctx_alice))
    assert "error" in result


def test_remember_fails_cleanly_with_no_session(fixture: _Fixture) -> None:
    tool = MemoryRememberTool()
    result = json.loads(tool.invoke(json.dumps({"text": "hello"}), fixture.ctx_no_session))
    assert "error" in result


# ── memory_search ─────────────────────────────────────────────────────────────


def test_search_is_scoped_to_the_calling_sessions_owner(fixture: _Fixture) -> None:
    fixture.service.remember("alice", "Prefers figures in CAD currency reports")
    fixture.service.remember("bob", "Prefers figures in CAD currency reports")

    tool = MemorySearchTool()
    alice_result = json.loads(
        tool.invoke(json.dumps({"query": "currency CAD figures"}), fixture.ctx_alice)
    )
    bob_result = json.loads(
        tool.invoke(json.dumps({"query": "currency CAD figures"}), fixture.ctx_bob)
    )
    assert len(alice_result["results"]) == 1
    assert len(bob_result["results"]) == 1
    assert alice_result["results"][0]["claim_id"] != bob_result["results"][0]["claim_id"]


def test_search_requires_non_empty_query(fixture: _Fixture) -> None:
    tool = MemorySearchTool()
    result = json.loads(tool.invoke(json.dumps({"query": ""}), fixture.ctx_alice))
    assert "error" in result


def test_search_rejects_invalid_kind(fixture: _Fixture) -> None:
    tool = MemorySearchTool()
    result = json.loads(
        tool.invoke(json.dumps({"query": "x", "kind": "not-a-kind"}), fixture.ctx_alice)
    )
    assert "error" in result


# ── memory_get / memory_explain ──────────────────────────────────────────────


def test_get_is_scoped_to_the_calling_sessions_owner(fixture: _Fixture) -> None:
    claim_id = fixture.service.remember("alice", "Alice's fact")["claim"]["claim_id"]

    tool = MemoryGetTool()
    own = json.loads(tool.invoke(json.dumps({"claim_id": claim_id}), fixture.ctx_alice))
    other = json.loads(tool.invoke(json.dumps({"claim_id": claim_id}), fixture.ctx_bob))
    assert own["claim_id"] == claim_id
    assert "error" in other


def test_explain_reports_evidence(fixture: _Fixture) -> None:
    claim_id = fixture.service.remember("alice", "Alice's fact", quote="exact words")["claim"][
        "claim_id"
    ]

    tool = MemoryExplainTool()
    result = json.loads(tool.invoke(json.dumps({"claim_id": claim_id}), fixture.ctx_alice))
    assert result["quote"] == "exact words"


# ── memory_forget ─────────────────────────────────────────────────────────────


def test_forget_two_step_plan_then_confirm(fixture: _Fixture) -> None:
    claim_id = fixture.service.remember("alice", "Alice's fact")["claim"]["claim_id"]

    tool = MemoryForgetTool()
    plan = json.loads(tool.invoke(json.dumps({"claim_id": claim_id}), fixture.ctx_alice))
    assert plan["status"] == "plan"
    assert fixture.service.get("alice", claim_id)["status"] == "active"

    done = json.loads(
        tool.invoke(json.dumps({"claim_id": claim_id, "confirm": True}), fixture.ctx_alice)
    )
    assert done["status"] == "forgotten"
    assert fixture.service.get("alice", claim_id)["status"] == "forgotten"


def test_forget_requires_claim_id_or_query(fixture: _Fixture) -> None:
    tool = MemoryForgetTool()
    result = json.loads(tool.invoke(json.dumps({}), fixture.ctx_alice))
    assert "error" in result


# ── service not configured ──────────────────────────────────────────────────


def test_tools_fail_cleanly_when_memory_not_configured(
    fixture: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.runtime.get_memory_service", lambda: None)
    tool = MemorySearchTool()
    result = json.loads(tool.invoke(json.dumps({"query": "x"}), fixture.ctx_alice))
    assert "error" in result
