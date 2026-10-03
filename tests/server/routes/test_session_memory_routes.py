"""Tests for the session memory routes.

Routes: ``/v1/sessions/{session_id}/memory/*``

The session memory router is only mounted when ``create_app`` receives a
``memory_service``. These tests provide their own app/client that include
it, backed by a real SQLite ``MemoryStore`` and a deterministic, offline
txtai index (no OpenAI calls).
"""

from __future__ import annotations

import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

pytest.importorskip("txtai")

from omnigent.db.utils import generate_agent_id
from omnigent.memory.index import MemoryIndex
from omnigent.memory.service import MemoryService
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.memory._fixtures import FAKE_VECTORS_OVERRIDE


@pytest.fixture()
def memory_service(db_uri: str, tmp_path: Path) -> MemoryService:
    store = SqlAlchemyMemoryStore(db_uri)
    index = MemoryIndex(tmp_path / "memory_index", vectors_override=FAKE_VECTORS_OVERRIDE)
    return MemoryService(store, index)


@pytest.fixture()
def memory_app(
    runtime_init: None, db_uri: str, tmp_path: Path, memory_service: MemoryService
) -> FastAPI:
    """Build a FastAPI app that includes the memory service, with permission
    enforcement active so owner-scoping is exercised for real."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        memory_service=memory_service,
    )


@pytest.fixture()
def no_memory_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """Same app, but with no memory_service — the routes must not exist."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts2"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache2"),
        comment_store=SqlAlchemyCommentStore(db_uri),
    )


@pytest_asyncio.fixture()
async def client(memory_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=memory_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture()
async def no_memory_client(no_memory_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=no_memory_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_session(db_uri: str, owner: str = "alice@example.com") -> str:
    """Seed a test agent and conversation, owned by *owner*."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(
        agent_id, name=f"memory-test-agent-{agent_id}", bundle_location="test:///bundle"
    )
    conv = conv_store.create_conversation(agent_id=agent_id)
    from omnigent.server.auth import LEVEL_OWNER

    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(owner)
    perm_store.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _headers(owner: str = "alice@example.com") -> dict[str, str]:
    return {"X-Forwarded-Email": owner}


# ── remember / search ────────────────────────────────────────────────────────


async def test_remember_then_search_round_trips(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = await _make_session(db_uri)
    resp = await client.post(
        f"/v1/sessions/{session_id}/memory/remember",
        json={"text": "Prefers figures in CAD currency reports", "kind": "preference"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "added"

    search_resp = await client.get(
        f"/v1/sessions/{session_id}/memory/search",
        params={"query": "currency CAD figures"},
        headers=_headers(),
    )
    assert search_resp.status_code == 200
    results = search_resp.json()["results"]
    assert len(results) == 1
    assert results[0]["kind"] == "preference"


async def test_search_is_isolated_per_owner(client: httpx.AsyncClient, db_uri: str) -> None:
    alice_session = await _make_session(db_uri, owner="alice@example.com")
    bob_session = await _make_session(db_uri, owner="bob@example.com")

    await client.post(
        f"/v1/sessions/{alice_session}/memory/remember",
        json={"text": "Alice likes quasar widgets", "kind": "fact"},
        headers=_headers("alice@example.com"),
    )
    await client.post(
        f"/v1/sessions/{bob_session}/memory/remember",
        json={"text": "Bob likes quasar widgets", "kind": "fact"},
        headers=_headers("bob@example.com"),
    )

    alice_results = (
        await client.get(
            f"/v1/sessions/{alice_session}/memory/search",
            params={"query": "quasar widgets"},
            headers=_headers("alice@example.com"),
        )
    ).json()["results"]
    bob_results = (
        await client.get(
            f"/v1/sessions/{bob_session}/memory/search",
            params={"query": "quasar widgets"},
            headers=_headers("bob@example.com"),
        )
    ).json()["results"]

    assert len(alice_results) == 1
    assert len(bob_results) == 1
    assert alice_results[0]["claim_id"] != bob_results[0]["claim_id"]
    # Cross-session search is impossible by construction: each request is
    # scoped by its own session's owner, never a client-supplied user id.
    assert alice_results[0]["text"] == "Alice likes quasar widgets"
    assert bob_results[0]["text"] == "Bob likes quasar widgets"


async def test_remember_rejects_unknown_fields(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = await _make_session(db_uri)
    resp = await client.post(
        f"/v1/sessions/{session_id}/memory/remember",
        json={"text": "hello", "user_id": "someone-else"},
        headers=_headers(),
    )
    assert resp.status_code == 422


async def test_remember_nonexistent_session_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/memory/remember",
        json={"text": "hello"},
        headers=_headers(),
    )
    assert resp.status_code == 404


# ── get / explain ────────────────────────────────────────────────────────────


async def test_get_and_explain_claim(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = await _make_session(db_uri)
    remember_resp = await client.post(
        f"/v1/sessions/{session_id}/memory/remember",
        json={"text": "Prefers CAD", "kind": "preference", "quote": "I want CAD"},
        headers=_headers(),
    )
    claim_id = remember_resp.json()["claim"]["claim_id"]

    get_resp = await client.get(
        f"/v1/sessions/{session_id}/memory/claims/{claim_id}", headers=_headers()
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["claim_id"] == claim_id

    explain_resp = await client.get(
        f"/v1/sessions/{session_id}/memory/claims/{claim_id}/explain", headers=_headers()
    )
    assert explain_resp.status_code == 200
    assert explain_resp.json()["quote"] == "I want CAD"


async def test_get_unknown_claim_returns_404(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = await _make_session(db_uri)
    resp = await client.get(
        f"/v1/sessions/{session_id}/memory/claims/{'a' * 32}", headers=_headers()
    )
    assert resp.status_code == 404


async def test_get_is_isolated_per_owner(client: httpx.AsyncClient, db_uri: str) -> None:
    alice_session = await _make_session(db_uri, owner="alice@example.com")
    bob_session = await _make_session(db_uri, owner="bob@example.com")
    remember_resp = await client.post(
        f"/v1/sessions/{alice_session}/memory/remember",
        json={"text": "Alice's private fact"},
        headers=_headers("alice@example.com"),
    )
    claim_id = remember_resp.json()["claim"]["claim_id"]

    resp = await client.get(
        f"/v1/sessions/{bob_session}/memory/claims/{claim_id}",
        headers=_headers("bob@example.com"),
    )
    assert resp.status_code == 404


# ── forget ───────────────────────────────────────────────────────────────────


async def test_forget_two_step(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = await _make_session(db_uri)
    remember_resp = await client.post(
        f"/v1/sessions/{session_id}/memory/remember",
        json={"text": "Prefers CAD"},
        headers=_headers(),
    )
    claim_id = remember_resp.json()["claim"]["claim_id"]

    plan_resp = await client.post(
        f"/v1/sessions/{session_id}/memory/forget",
        json={"claim_id": claim_id},
        headers=_headers(),
    )
    assert plan_resp.status_code == 200
    assert plan_resp.json()["status"] == "plan"

    confirm_resp = await client.post(
        f"/v1/sessions/{session_id}/memory/forget",
        json={"claim_id": claim_id, "confirm": True},
        headers=_headers(),
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["status"] == "forgotten"


async def test_forget_requires_claim_id_or_query(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = await _make_session(db_uri)
    resp = await client.post(
        f"/v1/sessions/{session_id}/memory/forget", json={}, headers=_headers()
    )
    assert resp.status_code == 400


# ── profile (S6 per-turn delivery seam) ──────────────────────────────────────


async def test_profile_is_none_with_no_claims(client: httpx.AsyncClient, db_uri: str) -> None:
    session_id = await _make_session(db_uri)
    resp = await client.get(f"/v1/sessions/{session_id}/memory/profile", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"profile": None}


async def test_profile_wraps_active_claims_in_the_delimiter_block(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = await _make_session(db_uri)
    await client.post(
        f"/v1/sessions/{session_id}/memory/remember",
        json={"text": "Prefers figures in CAD", "kind": "preference"},
        headers=_headers(),
    )
    resp = await client.get(f"/v1/sessions/{session_id}/memory/profile", headers=_headers())
    assert resp.status_code == 200
    profile = resp.json()["profile"]
    assert profile is not None
    assert profile.startswith("[Standing memory about the user")
    assert profile.endswith("[End of standing memory]")
    assert "Prefers figures in CAD" in profile


async def test_profile_is_isolated_per_owner(client: httpx.AsyncClient, db_uri: str) -> None:
    alice_session = await _make_session(db_uri, owner="alice@example.com")
    bob_session = await _make_session(db_uri, owner="bob@example.com")
    await client.post(
        f"/v1/sessions/{alice_session}/memory/remember",
        json={"text": "Prefers figures in CAD", "kind": "preference"},
        headers=_headers("alice@example.com"),
    )

    bob_resp = await client.get(
        f"/v1/sessions/{bob_session}/memory/profile", headers=_headers("bob@example.com")
    )
    assert bob_resp.json() == {"profile": None}


# ── not mounted without a memory_service ─────────────────────────────────────


async def test_routes_absent_without_memory_service(
    no_memory_client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = await _make_session(db_uri)
    resp = await no_memory_client.get(
        f"/v1/sessions/{session_id}/memory/search", params={"query": "x"}
    )
    assert resp.status_code == 404
