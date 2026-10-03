"""Tests for the runner-side ``side_chat_open`` dispatch.

``side_chat_open`` needs server_client (fork/create a session, bind a
host/runner) — see ``_execute_side_chat_open`` in
``omnigent.runner.tool_dispatch``. These tests mock the Omnigent server's
REST endpoints it calls instead of running a live server.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _execute_side_chat_open,
    _granted_tool_names,
    _ungranted_tool_reason,
)
from omnigent.spec.types import AgentSpec

CALLER = "conv_super_chat"
_SUPERSIDE = {"omnigent.context.mode": "superside-chat"}
_SIDE_CHAT = {**_SUPERSIDE, "omnigent.side_chat": "1"}


def _make_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1, skills=[], mcp_servers=[], local_tools=[], skills_filter="none"
    )


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


def _caller_session(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": CALLER,
        "kind": "default",
        "parent_session_id": None,
        "host_id": None,
        "workspace": None,
        "agent_id": "ag_abc123",
    }
    base.update(overrides)
    return base


# ── Missing server access / session id ──────────────────────────────────


@pytest.mark.asyncio
async def test_requires_server_client() -> None:
    result = json.loads(
        await _execute_side_chat_open(
            {"title": "t", "start": "blank"},
            server_client=None,
            conversation_id=CALLER,
            labels=_SUPERSIDE,
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_requires_conversation_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "blank"},
                server_client=client,
                conversation_id=None,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── Argument validation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requires_nonempty_title() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "", "start": "blank"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


@pytest.mark.asyncio
async def test_requires_valid_start() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "sideways"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── Refusals (caller eligibility) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_refuses_outside_superside_chat_mode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_caller_session())

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "blank"},
                server_client=client,
                conversation_id=CALLER,
                labels={},
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


@pytest.mark.asyncio
async def test_refuses_from_a_side_chat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_caller_session())

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "blank"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SIDE_CHAT,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result
    assert "Side Chat" in result["error"]


@pytest.mark.asyncio
async def test_refuses_from_a_sub_agent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_caller_session(kind="sub_agent", parent_session_id="conv_parent")
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "blank"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result
    assert "Sub-agent" in result["error"]


@pytest.mark.asyncio
async def test_caller_lookup_http_error_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "blank"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── with_context: fork ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_context_forks_with_side_chat_flag() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CALLER}":
            return httpx.Response(200, json=_caller_session())
        if request.url.path == f"/v1/sessions/{CALLER}/fork":
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(201, json={"id": "conv_fork_1"})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "Continuing the topic", "start": "with_context"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert result == {
        "conversation_id": "conv_fork_1",
        "title": "Continuing the topic",
        "start": "with_context",
    }
    assert seen == [
        (f"/v1/sessions/{CALLER}/fork", {"side_chat": True, "title": "Continuing the topic"})
    ]


@pytest.mark.asyncio
async def test_with_context_binds_the_super_chats_host() -> None:
    bind_calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CALLER}":
            return httpx.Response(
                200, json=_caller_session(host_id="host_abc", workspace="/work/dir")
            )
        if request.url.path == f"/v1/sessions/{CALLER}/fork":
            return httpx.Response(201, json={"id": "conv_fork_1"})
        if request.url.path == "/v1/hosts/host_abc/runners":
            bind_calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"runner_id": "runner_1", "status": "launching"})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _client(handler)
    try:
        await _execute_side_chat_open(
            {"title": "t", "start": "with_context"},
            server_client=client,
            conversation_id=CALLER,
            labels=_SUPERSIDE,
        )
    finally:
        await client.aclose()
    assert bind_calls == [
        ("/v1/hosts/host_abc/runners", {"session_id": "conv_fork_1", "workspace": "/work/dir"})
    ]


@pytest.mark.asyncio
async def test_host_bind_failure_is_best_effort() -> None:
    """A failed bind must not fail the whole call — the Side Chat is simply
    unbound, same as an unbound fork made through the web UI."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CALLER}":
            return httpx.Response(
                200, json=_caller_session(host_id="host_abc", workspace="/work/dir")
            )
        if request.url.path == f"/v1/sessions/{CALLER}/fork":
            return httpx.Response(201, json={"id": "conv_fork_1"})
        if request.url.path == "/v1/hosts/host_abc/runners":
            return httpx.Response(500)
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "with_context"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert result["conversation_id"] == "conv_fork_1"
    assert "error" not in result


@pytest.mark.asyncio
async def test_fork_error_response_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CALLER}":
            return httpx.Response(200, json=_caller_session())
        return httpx.Response(400, text="bad fork")

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "with_context"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── blank: create ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blank_creates_with_discovery_labels() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CALLER}":
            return httpx.Response(200, json=_caller_session(agent_id="ag_super"))
        if request.url.path == "/v1/sessions":
            seen.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "conv_blank_1"})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "Unrelated thing", "start": "blank"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert result == {
        "conversation_id": "conv_blank_1",
        "title": "Unrelated thing",
        "start": "blank",
    }
    assert seen == [
        {
            "agent_id": "ag_super",
            "title": "Unrelated thing",
            "labels": {
                "omnigent.context.mode": "superside-chat",
                "omnigent.side_chat": "1",
                "omnigent.fork.source_id": CALLER,
            },
        }
    ]


@pytest.mark.asyncio
async def test_blank_requires_caller_agent_binding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_caller_session(agent_id=None))

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "blank"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── first_message ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_message_is_queued_after_create() -> None:
    posted_events: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CALLER}":
            return httpx.Response(200, json=_caller_session())
        if request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "conv_blank_1"})
        if request.url.path == "/v1/sessions/conv_blank_1/events":
            posted_events.append((request.url.path, json.loads(request.content)))
            return httpx.Response(202, json={"queued": True})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_side_chat_open(
                {"title": "t", "start": "blank", "first_message": "let's dig into this"},
                server_client=client,
                conversation_id=CALLER,
                labels=_SUPERSIDE,
            )
        )
    finally:
        await client.aclose()
    assert result["conversation_id"] == "conv_blank_1"
    assert len(posted_events) == 1
    _, body = posted_events[0]
    assert body["data"]["content"][0]["text"] == "let's dig into this"


# ── Grant gate: advertised only where actually callable ─────────────────


def test_granted_tool_names_excludes_side_chat_open_without_superside_chat() -> None:
    spec = _make_spec()
    assert "side_chat_open" not in _granted_tool_names(spec, "claude-sdk")


def test_granted_tool_names_excludes_side_chat_open_for_plain_rollover() -> None:
    spec = _make_spec()
    granted = _granted_tool_names(spec, "claude-sdk", labels={"omnigent.context.mode": "rollover"})
    assert "side_chat_open" not in granted


def test_granted_tool_names_includes_side_chat_open_for_superside_chat() -> None:
    spec = _make_spec()
    granted = _granted_tool_names(spec, "claude-sdk", labels=_SUPERSIDE)
    assert "side_chat_open" in granted


def test_ungranted_tool_reason_blocks_without_superside_chat_label() -> None:
    spec = _make_spec()
    assert _ungranted_tool_reason("side_chat_open", spec, "claude-sdk") is not None


def test_ungranted_tool_reason_allows_with_superside_chat_label() -> None:
    spec = _make_spec()
    reason = _ungranted_tool_reason("side_chat_open", spec, "claude-sdk", labels=_SUPERSIDE)
    assert reason is None


def test_granted_cache_distinguishes_rollover_from_superside_chat() -> None:
    """Regression: the cache key must vary on BOTH label gates, or a spec
    object reused across a rollover session and a superside-chat session
    would share (and leak) a cached grant."""
    spec = _make_spec()
    rollover_granted = _granted_tool_names(
        spec, "claude-sdk", labels={"omnigent.context.mode": "rollover"}
    )
    superside_granted = _granted_tool_names(spec, "claude-sdk", labels=_SUPERSIDE)
    assert "side_chat_open" not in rollover_granted
    assert "session_history" in rollover_granted
    assert "side_chat_open" in superside_granted
