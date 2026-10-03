"""Tests for the memory-upkeep compaction trigger (``rollover/SUPERSIDE-CHAT-PLAN.md`` S6).

Posting a ``compaction`` event to a ``superside-chat`` session must enqueue
a memory upkeep run for the session's owner; a plain or ``rollover``
(native-CLI) session must not — S6 applies only to ``superside-chat``. These
tests never run the upkeep pipeline itself (``coordinator.schedule`` is
monkeypatched to a spy) and never touch txtai — ``MemoryService``'s
constructor and the routes it backs only import txtai lazily, on first
search/write, which this test never triggers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    ROLLOVER_MODE_VALUE,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.db.utils import generate_agent_id
from omnigent.memory.index import MemoryIndex
from omnigent.memory.service import MemoryService
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from omnigent.stores.memory_upkeep_store.sqlalchemy_store import SqlAlchemyMemoryUpkeepStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def trigger_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    memory_service = MemoryService(
        SqlAlchemyMemoryStore(db_uri), MemoryIndex(tmp_path / "memory_index")
    )
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
        memory_upkeep_store=SqlAlchemyMemoryUpkeepStore(db_uri),
    )


@pytest_asyncio.fixture()
async def trigger_client(trigger_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=trigger_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _headers(owner: str = "alice@example.com") -> dict[str, str]:
    return {"X-Forwarded-Email": owner}


async def _make_session(db_uri: str, *, owner: str = "alice@example.com", mode: str | None) -> str:
    """Seed a test agent and conversation, owned by *owner*."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(
        agent_id, name=f"upkeep-trigger-agent-{agent_id}", bundle_location="test:///bundle"
    )
    labels = {CONTEXT_MODE_LABEL: mode} if mode else None
    conv = conv_store.create_conversation(agent_id=agent_id, labels=labels)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(owner)
    perm_store.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _spy(calls: list[str]) -> Any:
    def _schedule(user_id: str) -> None:
        calls.append(user_id)

    return _schedule


async def _post_compaction(
    client: httpx.AsyncClient, session_id: str, owner: str
) -> httpx.Response:
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "compaction",
            "data": {
                "summary": "prior turns summarized",
                "last_item_id": "msg_does_not_need_to_exist",
                "model": "test-model",
                "token_count": 3,
            },
        },
        headers=_headers(owner),
    )


async def test_compaction_on_superside_chat_session_enqueues_upkeep(
    trigger_app: FastAPI, trigger_client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = await _make_session(db_uri, mode=SUPERSIDE_CHAT_MODE_VALUE)
    calls: list[str] = []
    trigger_app.state.memory_upkeep_coordinator.schedule = _spy(calls)

    resp = await _post_compaction(trigger_client, session_id, "alice@example.com")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": True}
    assert calls == ["alice@example.com"]


async def test_compaction_on_plain_session_does_not_enqueue(
    trigger_app: FastAPI, trigger_client: httpx.AsyncClient, db_uri: str
) -> None:
    session_id = await _make_session(db_uri, mode=None)
    calls: list[str] = []
    trigger_app.state.memory_upkeep_coordinator.schedule = _spy(calls)

    resp = await _post_compaction(trigger_client, session_id, "alice@example.com")

    assert resp.status_code == 202, resp.text
    assert calls == []


async def test_compaction_on_native_cli_rollover_session_does_not_enqueue(
    trigger_app: FastAPI, trigger_client: httpx.AsyncClient, db_uri: str
) -> None:
    """S6 applies only to ``superside-chat``; native-CLI ``rollover`` keeps
    today's behaviour (no memory-upkeep trigger from this slice)."""
    session_id = await _make_session(db_uri, mode=ROLLOVER_MODE_VALUE)
    calls: list[str] = []
    trigger_app.state.memory_upkeep_coordinator.schedule = _spy(calls)

    resp = await _post_compaction(trigger_client, session_id, "alice@example.com")

    assert resp.status_code == 202, resp.text
    assert calls == []


async def test_compaction_with_no_memory_upkeep_store_mounts_without_coordinator(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """``memory_upkeep_store=None`` leaves the trigger disabled, not broken."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts2"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache2"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    assert app.state.memory_upkeep_coordinator is None

    session_id = await _make_session(db_uri, mode=SUPERSIDE_CHAT_MODE_VALUE)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _post_compaction(client, session_id, "alice@example.com")
    assert resp.status_code == 202, resp.text
