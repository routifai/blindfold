"""An archived Side Chat unarchives on its next user message.

Decision documented in ``omnigent.superchat.chats.maybe_unarchive_on_user_message``:
Archived only means "hidden from the list, not ended", so a message to an
archived Side Chat resumes it rather than being silently accepted into a
hidden chat. Wired into ``POST /v1/sessions/{id}/events`` (routes_events.py).
"""

from __future__ import annotations

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient, name: str, *, labels: dict[str, str] | None = None
) -> str:
    agent = await create_test_agent(client, name=name)
    body: dict[str, object] = {"agent_id": agent["id"]}
    if labels:
        body["labels"] = labels
    resp = await client.post("/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _archive(client: httpx.AsyncClient, session_id: str) -> None:
    resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True


async def _is_archived(client: httpx.AsyncClient, session_id: str) -> bool:
    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["archived"]


async def _post_message(client: httpx.AsyncClient, session_id: str, text: str) -> None:
    # The unarchive happens before the runner-dispatch step this test's
    # unbound session can't complete, so a 503 "no runner bound" here is
    # expected and irrelevant to what this test checks.
    await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )


async def test_archived_side_chat_unarchives_on_user_message(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(
        client, "side-chat-unarchive", labels={"omnigent.side_chat": "1"}
    )
    await _archive(client, session_id)
    assert await _is_archived(client, session_id) is True

    await _post_message(client, session_id, "resuming this one")

    assert await _is_archived(client, session_id) is False


async def test_archived_non_side_chat_stays_archived_on_message(
    client: httpx.AsyncClient,
) -> None:
    """Only a Side Chat (the ``omnigent.side_chat`` label) unarchives — an
    archived plain session (or, by the same rule, a would-be Super Chat)
    is left exactly as the caller set it."""
    session_id = await _create_session(client, "plain-session-stays-archived")
    await _archive(client, session_id)

    await _post_message(client, session_id, "hello")

    assert await _is_archived(client, session_id) is True
