"""Tests for the runner-side ``session_history`` dispatch (native-relay path).

Native harnesses (claude-native / codex-native) call ``session_history``
through the runner's MCP relay, which has no in-process ConversationStore —
see ``_execute_session_history_tool`` in ``omnigent.runner.tool_dispatch``.
These tests mock the Omnigent server's REST endpoints it calls instead.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _execute_session_history_tool,
    _granted_tool_names,
    _ungranted_tool_reason,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import AgentSpec

CONV = "conv_native_test"


def _make_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1, skills=[], mcp_servers=[], local_tools=[], skills_filter="none"
    )


def _client(handler: httpx.MockTransport | None = None, **kwargs: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler) if handler else None,
        base_url="http://server",
        **kwargs,  # type: ignore[arg-type]
    )


def _item(item_id: str, role: str, text: str, created_at: int = 0) -> dict[str, object]:
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "content": [
            {"type": "output_text" if role == "assistant" else "input_text", "text": text}
        ],
        "created_at": created_at,
    }


# ── Missing server access / session id ──────────────────────


@pytest.mark.asyncio
async def test_requires_server_client() -> None:
    result = json.loads(
        await _execute_session_history_tool(
            {"action": "status"}, conversation_id=CONV, server_client=None
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_requires_conversation_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "status"}, conversation_id=None, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── read ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_via_rest_groups_turns_and_ignores_conversation_id_arg() -> None:
    """The dispatch-context conversation_id is used for the URL; any
    ``conversation_id`` in args is inert (the function signature doesn't
    even read it)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/items"
        assert request.url.params["order"] == "desc"
        return httpx.Response(
            200,
            json={
                "data": [
                    _item("2", "assistant", "answer"),
                    _item("1", "user", "question"),
                ],
                "has_more": False,
            },
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "read", "conversation_id": "conv_someone_else"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert len(result["turns"]) == 1
    assert [m["content"] for m in result["turns"][0]["messages"]] == ["question", "answer"]
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_read_uses_after_cursor_not_before() -> None:
    """Continuing a desc-order walk must use ``after=``, not ``before=``
    (see the identically-named regression this caught in the in-process
    tool's ``group_into_turns``)."""
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json={"data": [_item("1", "user", "q")], "has_more": False})

    client = _client(handler)
    try:
        await _execute_session_history_tool(
            {"action": "read", "cursor": "item_5"},
            conversation_id=CONV,
            server_client=client,
        )
    finally:
        await client.aclose()
    assert seen_params[0].get("after") == "item_5"
    assert "before" not in seen_params[0]


# ── search ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_via_rest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/items/search"
        assert request.url.params["query"] == "hello"
        return httpx.Response(200, json={"data": [_item("1", "user", "hello there")]})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "search", "query": "hello"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_search_requires_query() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "search"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── status ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_via_rest_reads_labels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/labels"
        return httpx.Response(
            200,
            json={
                "labels": {
                    "omnigent.last_context_tokens": "100",
                    "omnigent.last_context_window": "1000",
                }
            },
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "status"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result["current_context_tokens"] == 100
    assert result["context_window_tokens"] == 1000


# ── Grant gate: advertised only where actually callable ──────


def test_granted_tool_names_excludes_session_history_without_rollover() -> None:
    spec = _make_spec()
    assert "session_history" not in _granted_tool_names(spec, "claude-native")


def test_granted_tool_names_includes_session_history_for_rollover() -> None:
    spec = _make_spec()
    granted = _granted_tool_names(
        spec, "claude-native", labels={"omnigent.context.mode": "rollover"}
    )
    assert "session_history" in granted


def test_ungranted_tool_reason_blocks_without_rollover_label() -> None:
    spec = _make_spec()
    assert _ungranted_tool_reason("session_history", spec, "claude-native") is not None


def test_ungranted_tool_reason_allows_with_rollover_label() -> None:
    spec = _make_spec()
    reason = _ungranted_tool_reason(
        "session_history", spec, "claude-native", labels={"omnigent.context.mode": "rollover"}
    )
    assert reason is None


# ── Native relay advertisement ────────────────────────────────


def test_relay_schemas_omit_session_history_without_rollover() -> None:
    spec = _make_spec()
    names = {s["name"] for s in build_native_relay_tool_schemas(spec)}
    assert "session_history" not in names


def test_relay_schemas_include_session_history_for_rollover() -> None:
    spec = _make_spec()
    names = {
        s["name"]
        for s in build_native_relay_tool_schemas(
            spec, labels={"omnigent.context.mode": "rollover"}
        )
    }
    assert "session_history" in names


def test_relay_schemas_no_spec_fallback_respects_rollover_gate() -> None:
    """The spec-less fallback surface (no resolved AgentSpec yet) still
    gates session_history on the label, not just the (nonexistent) spec."""
    names_off = {s["name"] for s in build_native_relay_tool_schemas(None)}
    names_on = {
        s["name"]
        for s in build_native_relay_tool_schemas(
            None, labels={"omnigent.context.mode": "rollover"}
        )
    }
    assert "session_history" not in names_off
    assert "session_history" in names_on


def test_relay_lists_session_history_as_always_loaded() -> None:
    """Claude Code keeps MCP tools behind tool search unless the server marks
    one always-loaded; recall must be in context right after a rollover."""
    from omnigent.harnesses.claude_native.bridge import _mcp_tool_schema_from_spec

    recall = _mcp_tool_schema_from_spec({"name": "session_history", "parameters": {}})
    other = _mcp_tool_schema_from_spec({"name": "sys_session_get_info", "parameters": {}})

    assert recall["_meta"] == {"anthropic/alwaysLoad": True}
    assert "_meta" not in other
