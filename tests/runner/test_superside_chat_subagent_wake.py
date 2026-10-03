"""Tests for the superside-chat sub-agent wake (slice S3).

Two behaviors on top of the plain-mode wake (``[System: ... waiting in
inbox]``, covered by ``test_stranded_wake_retry_on_reconnect.py``):

- a ``superside_chat`` work entry's wake notice inlines the sub-agent's
  Result itself instead of only announcing "N results waiting";
- when the Originating Chat is an archived Side Chat, the wake is
  redirected to its Super Chat (resolved from ``omnigent.fork.source_id``),
  with a one-line note naming the archived chat.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY, SIDE_CHAT_LABEL_KEY
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _RecordingServerClient(NullServerClient):
    """Fake server client: serves fixed session snapshots, records every POST."""

    def __init__(self, *, snapshots: dict[str, dict[str, Any]] | None = None) -> None:
        self._snapshots = snapshots or {}
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        for session_id, snapshot in self._snapshots.items():
            if url == f"/v1/sessions/{session_id}":
                request = httpx.Request("GET", f"http://runner.test{url}")
                return httpx.Response(200, request=request, json=snapshot)
        return await super().get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        body = kwargs.get("json")
        if isinstance(body, dict):
            self.posts.append((url, body))
        request = httpx.Request("POST", f"http://runner.test{url}")
        return httpx.Response(200, request=request, json={})


async def _complete_child(client: httpx.AsyncClient, child_id: str, output: str) -> None:
    resp = await client.post(
        f"/v1/sessions/{child_id}/events",
        json={"type": "external_session_status", "data": {"status": "idle", "output": output}},
    )
    assert resp.status_code == 204, resp.text


def _notice_text(post: tuple[str, dict[str, Any]]) -> str:
    _url, body = post
    content = body["data"]["content"]
    return str(content[0]["text"])


@pytest.mark.asyncio
async def test_superside_chat_wake_inlines_result(_no_wake_backoff: list[float]) -> None:
    """A ``superside_chat`` entry's wake carries the Result, not 'waiting in inbox'."""
    from omnigent.runner import app as runner_app

    parent_id = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    child_id = "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _RecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="auth",
        superside_chat=True,
    )
    try:
        async with _runner_client(app) as client:
            await _complete_child(client, child_id, "FINDINGS: the vault key rotated on March 3.")
            deadline = asyncio.get_running_loop().time() + 5.0
            while not server_client.posts:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("no wake POST observed within 5s")
                await asyncio.sleep(0.01)

        assert len(server_client.posts) == 1
        url, _body = server_client.posts[0]
        assert url == f"/v1/sessions/{parent_id}/events"
        text = _notice_text(server_client.posts[0])
        assert "FINDINGS: the vault key rotated on March 3." in text
        assert "waiting in inbox" not in text

        entry = runner_app.get_subagent_work(child_id)
        assert entry is not None
        assert entry.wake_inlined is True
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_plain_mode_wake_is_unchanged(_no_wake_backoff: list[float]) -> None:
    """A non-superside-chat entry keeps the plain 'N results waiting' notice."""
    from omnigent.runner import app as runner_app

    parent_id = "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
    child_id = "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _RecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="auth",
    )
    try:
        async with _runner_client(app) as client:
            await _complete_child(client, child_id, "FINDINGS: should not be inlined.")
            deadline = asyncio.get_running_loop().time() + 5.0
            while not server_client.posts:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("no wake POST observed within 5s")
                await asyncio.sleep(0.01)

        text = _notice_text(server_client.posts[0])
        assert "waiting in inbox" in text
        assert "FINDINGS: should not be inlined." not in text

        entry = runner_app.get_subagent_work(child_id)
        assert entry is not None
        assert entry.wake_inlined is False
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_superside_chat_wake_redirects_archived_side_chat_to_super_chat(
    _no_wake_backoff: list[float],
) -> None:
    """An archived Side Chat's wake is redirected to its Super Chat, with a note."""
    from omnigent.runner import app as runner_app

    side_chat_id = "e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    super_chat_id = "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3"
    child_id = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _RecordingServerClient(
        snapshots={
            side_chat_id: {
                "archived": True,
                "labels": {
                    SIDE_CHAT_LABEL_KEY: "1",
                    FORK_SOURCE_LABEL_KEY: super_chat_id,
                },
            },
        }
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    # The entry's bookkeeping (inbox, wake-pending dedup) stays keyed by the
    # Side Chat — only the wake POST's destination is redirected.
    runner_app._session_inboxes_ref[side_chat_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=side_chat_id,
        child_session_id=child_id,
        agent="researcher",
        title="auth",
        superside_chat=True,
    )
    try:
        async with _runner_client(app) as client:
            await _complete_child(client, child_id, "FINDINGS: redirected result.")
            deadline = asyncio.get_running_loop().time() + 5.0
            while not server_client.posts:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("no wake POST observed within 5s")
                await asyncio.sleep(0.01)

        assert len(server_client.posts) == 1
        url, _body = server_client.posts[0]
        assert url == f"/v1/sessions/{super_chat_id}/events"
        text = _notice_text(server_client.posts[0])
        assert "archived Side Chat" in text
        assert side_chat_id in text
        assert "FINDINGS: redirected result." in text
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(side_chat_id, None)
