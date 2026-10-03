"""``GET /v1/sessions/{id}/items/search`` — the session_history REST search route.

The runner has no in-process ConversationStore, so the native-relay
dispatch for ``session_history``'s ``search`` action calls this endpoint
(see ``omnigent.runner.tool_dispatch._session_history_search_via_rest``).
This is its REST counterpart to ``ConversationStore.search``.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY, SIDE_CHAT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, name: str) -> str:
    agent = await create_test_agent(client, name=name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post_user_message(client: httpx.AsyncClient, session_id: str, text: str) -> None:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
                "response_id": "resp_test",
            },
        },
    )
    assert resp.status_code in (200, 201, 202), resp.text


async def test_search_finds_own_session_item(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "search-route-own")
    await _post_user_message(client, session_id, "findableneedle in this session")

    resp = await client.get(
        f"/v1/sessions/{session_id}/items/search",
        params={"query": "findableneedle"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["type"] == "message"


async def test_search_matches_hyphenated_identifiers(client: httpx.AsyncClient) -> None:
    """Ids like account or reference numbers must not be parsed as FTS syntax."""
    session_id = await _create_session(client, "search-route-hyphen")
    await _post_user_message(client, session_id, "My codeword is CW-CODEX-A7E00A: keep it.")

    resp = await client.get(
        f"/v1/sessions/{session_id}/items/search",
        params={"query": "CW-CODEX-A7E00A:"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1


async def test_search_is_scoped_to_the_session(client: httpx.AsyncClient) -> None:
    session_a = await _create_session(client, "search-route-a")
    session_b = await _create_session(client, "search-route-b")
    await _post_user_message(client, session_a, "onlyinsessionamarker present here")
    await _post_user_message(client, session_b, "onlyinsessionamarker present here too")

    resp = await client.get(
        f"/v1/sessions/{session_a}/items/search",
        params={"query": "onlyinsessionamarker"},
    )
    assert resp.status_code == 200, resp.text
    # Both sessions contain the term, but the search is scoped to session_a —
    # a leak would return 2.
    assert len(resp.json()["data"]) == 1


async def test_search_missing_session_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/v1/sessions/conv_does_not_exist/items/search",
        params={"query": "anything"},
    )
    assert resp.status_code == 404


async def test_search_requires_nonempty_query(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "search-route-empty-query")
    resp = await client.get(f"/v1/sessions/{session_id}/items/search", params={"query": ""})
    assert resp.status_code == 422


async def test_search_limit_is_capped_at_20(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "search-route-limit")
    resp = await client.get(
        f"/v1/sessions/{session_id}/items/search",
        params={"query": "x", "limit": 1000},
    )
    assert resp.status_code == 422


# ── GET /sessions/{id}/related_chats — session_history's list_chats ────
# Same REST-dispatch rationale as items/search above: the runner's
# native-relay handler for session_history's list_chats action calls this
# route (see _session_history_list_chats_via_rest in tool_dispatch.py).


async def test_related_chats_lists_side_chat_children(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    source_id = await _create_session(client, "related-chats-source")
    side_chat_id = await _create_session(client, "related-chats-side-chat")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(side_chat_id, {FORK_SOURCE_LABEL_KEY: source_id, SIDE_CHAT_LABEL_KEY: "1"})
    await _post_user_message(client, side_chat_id, "side chat question")

    resp = await client.get(f"/v1/sessions/{source_id}/related_chats")
    assert resp.status_code == 200, resp.text
    chats = {c["id"]: c for c in resp.json()["data"]}
    assert side_chat_id in chats
    assert chats[side_chat_id]["last_message_preview"] == "side chat question"


async def test_related_chats_keeps_archived_side_chat_flagged(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """An archived Side Chat stays readable by its Super Chat, flagged for the UI to hide."""
    source_id = await _create_session(client, "related-chats-archived-source")
    side_chat_id = await _create_session(client, "related-chats-archived-side-chat")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(side_chat_id, {FORK_SOURCE_LABEL_KEY: source_id, SIDE_CHAT_LABEL_KEY: "1"})
    store.update_conversation(side_chat_id, archived=True)

    resp = await client.get(f"/v1/sessions/{source_id}/related_chats")
    assert resp.status_code == 200, resp.text
    chats = {c["id"]: c for c in resp.json()["data"]}
    assert chats[side_chat_id]["archived"] is True


async def test_related_chats_excludes_fork_without_side_chat_label(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    source_id = await _create_session(client, "related-chats-source-plain")
    plain_fork_id = await _create_session(client, "related-chats-plain-fork")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(plain_fork_id, {FORK_SOURCE_LABEL_KEY: source_id})

    resp = await client.get(f"/v1/sessions/{source_id}/related_chats")
    assert resp.status_code == 200, resp.text
    ids = [chat["id"] for chat in resp.json()["data"]]
    assert plain_fork_id not in ids


async def test_related_chats_excludes_unrelated_session(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    source_id = await _create_session(client, "related-chats-source-2")
    other_id = await _create_session(client, "related-chats-other")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(other_id, {SIDE_CHAT_LABEL_KEY: "1"})  # no fork-source label at all

    resp = await client.get(f"/v1/sessions/{source_id}/related_chats")
    assert resp.status_code == 200, resp.text
    ids = [chat["id"] for chat in resp.json()["data"]]
    assert other_id not in ids


async def test_related_chats_missing_session_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/conv_does_not_exist/related_chats")
    assert resp.status_code == 404
