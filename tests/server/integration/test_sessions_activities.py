"""Integration tests for the Activity Feed routes.

``GET /v1/sessions/{id}/activities`` and
``GET /v1/sessions/{id}/activities/{activity_id}`` — the owner-scoped,
READ-gated wrapper around ``omnigent.superchat.activity`` (derivation
unit-tested directly in tests/superchat/). These tests cover the route
wiring: auth/404 handling and the end-to-end shape over a seeded
Super Chat + Sub-agent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.entities import FunctionCallData, MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_MODE_LABEL = "omnigent.context.mode"
_MODE_VALUE = "superside-chat"


async def _create_session(client: httpx.AsyncClient, agent_name: str) -> dict[str, Any]:
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, f"session create failed: {resp.text}"
    return resp.json()


# ── 404 ──────────────────────────────────────────────────────────────────


async def test_activities_404_for_nonexistent_session(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/activities")
    assert resp.status_code == 404


async def test_activity_detail_404_for_nonexistent_session(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/activities/sub_agent:conv_x"
    )
    assert resp.status_code == 404


# ── Non-superside-chat session: empty, not an error ──────────────────────


async def test_activities_empty_for_plain_session(client: httpx.AsyncClient) -> None:
    session = await _create_session(client, "plain-agent")
    resp = await client.get(f"/v1/sessions/{session['id']}/activities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == []


# ── Full shape over a seeded Super Chat + Sub-agent ───────────────────────


async def test_activities_list_and_detail_for_superside_chat_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session = await _create_session(client, "superside-agent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_labels(session["id"], {_MODE_LABEL: _MODE_VALUE})

    response_id = "resp_1"
    conv_store.append(
        session["id"],
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "research side chat mechanics"}],
                ),
            ),
            NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=FunctionCallData(
                    agent="brain",
                    name="memory_search",
                    arguments=json.dumps({"query": "side chat"}),
                    call_id="call_1",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="brain",
                    content=[{"type": "output_text", "text": "Researched side chat mechanics"}],
                ),
            ),
        ],
    )
    sub_agent = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=session["id"],
        title="researcher:auth-flow",
        labels={_MODE_LABEL: _MODE_VALUE},
    )
    conv_store.set_session_live_status(sub_agent.id, "idle")

    list_resp = await client.get(f"/v1/sessions/{session['id']}/activities")
    assert list_resp.status_code == 200
    rows = list_resp.json()["data"]
    assert len(rows) == 2
    by_kind = {row["kind"]: row for row in rows}

    turn = by_kind["turn"]
    assert turn["chat_id"] == session["id"]
    assert turn["outcome"] == "Researched side chat mechanics"
    assert turn["status"] == "done"
    assert "date" in turn
    assert turn["steps"][0]["title"] == "Searched memory for 'side chat'"
    assert "detail" not in turn["steps"][0]

    sub = by_kind["sub_agent"]
    assert sub["chat_id"] == sub_agent.id
    assert sub["title"] == "researcher: auth-flow"
    assert sub["status"] == "done"

    detail_resp = await client.get(f"/v1/sessions/{session['id']}/activities/{sub['id']}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["id"] == sub["id"]
    assert detail["steps"] == []  # the seeded sub-agent had no tool calls


async def test_activity_detail_404_for_unknown_activity_id(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session = await _create_session(client, "superside-agent-2")
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_labels(session["id"], {_MODE_LABEL: _MODE_VALUE})

    resp = await client.get(f"/v1/sessions/{session['id']}/activities/sub_agent:conv_missing")
    assert resp.status_code == 404
