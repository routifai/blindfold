"""Tests for the runner-side ``memory_*`` dispatch (native-relay path).

Native harnesses (claude-native / codex-native) call the memory tools
through the runner's MCP relay, which has no in-process ``MemoryService`` —
see ``_execute_memory_tool`` in ``omnigent.runner.tool_dispatch``. These
tests mock the Omnigent server's REST endpoints it calls instead, mirroring
``tests/runner/test_session_history_tool_dispatch.py``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _execute_memory_tool,
    _granted_tool_names,
    _ungranted_tool_reason,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import AgentSpec

CONV = "conv_native_memory_test"

_MEMORY_TOOL_NAMES = (
    "memory_remember",
    "memory_search",
    "memory_get",
    "memory_explain",
    "memory_forget",
)


def _make_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1, skills=[], mcp_servers=[], local_tools=[], skills_filter="none"
    )


def _client(handler: httpx.MockTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler) if handler else None,
        base_url="http://server",
    )


# ── Missing server access / session id ──────────────────────────────────────


@pytest.mark.asyncio
async def test_requires_server_client() -> None:
    result = json.loads(
        await _execute_memory_tool(
            "memory_search", {"query": "x"}, conversation_id=CONV, server_client=None
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_requires_conversation_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_search", {"query": "x"}, conversation_id=None, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_remember ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remember_posts_to_remember_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1/sessions/{CONV}/memory/remember"
        body = json.loads(request.content)
        assert body == {
            "text": "Prefers CAD",
            "kind": "preference",
            "quote": None,
            "replaces_claim_id": None,
        }
        return httpx.Response(
            200, json={"action": "added", "claim": {"claim_id": "c1", "text": "Prefers CAD"}}
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_remember",
                {"text": "Prefers CAD", "kind": "preference"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert result["action"] == "added"


@pytest.mark.asyncio
async def test_remember_requires_non_empty_text() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_remember", {"text": "  "}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_search ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_gets_from_search_endpoint_with_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v1/sessions/{CONV}/memory/search"
        assert request.url.params["query"] == "currency"
        assert request.url.params["kind"] == "preference"
        return httpx.Response(200, json={"results": [{"claim_id": "c1"}]})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_search",
                {"query": "currency", "kind": "preference"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_search_requires_non_empty_query() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_search", {"query": ""}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_get / memory_explain ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_fetches_claim_by_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/memory/claims/c1"
        return httpx.Response(200, json={"claim_id": "c1", "text": "hello"})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_get", {"claim_id": "c1"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result["claim_id"] == "c1"


@pytest.mark.asyncio
async def test_explain_fetches_explain_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/memory/claims/c1/explain"
        return httpx.Response(200, json={"claim": {"claim_id": "c1"}, "supersedes": []})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_explain", {"claim_id": "c1"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result["claim"]["claim_id"] == "c1"


@pytest.mark.asyncio
async def test_get_requires_claim_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_get", {}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_forget ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forget_posts_to_forget_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1/sessions/{CONV}/memory/forget"
        body = json.loads(request.content)
        assert body == {"claim_id": "c1", "query": None, "confirm": True}
        return httpx.Response(200, json={"status": "forgotten", "claim": {"claim_id": "c1"}})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_forget",
                {"claim_id": "c1", "confirm": True},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert result["status"] == "forgotten"


@pytest.mark.asyncio
async def test_forget_requires_claim_id_or_query() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_forget", {}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── Error passthrough ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_2xx_response_surfaces_as_a_clean_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"error": "Claim not found"})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_get", {"claim_id": "c1"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "Claim not found" in result["error"]


# ── Grant gate: advertised only where actually callable ──────────────────────


@pytest.mark.parametrize("name", _MEMORY_TOOL_NAMES)
def test_granted_tool_names_excludes_memory_tools_without_rollover(name: str) -> None:
    spec = _make_spec()
    assert name not in _granted_tool_names(spec, "claude-native")


@pytest.mark.parametrize("name", _MEMORY_TOOL_NAMES)
def test_granted_tool_names_includes_memory_tools_for_rollover(name: str) -> None:
    spec = _make_spec()
    granted = _granted_tool_names(
        spec, "claude-native", labels={"omnigent.context.mode": "rollover"}
    )
    assert name in granted


@pytest.mark.parametrize("name", _MEMORY_TOOL_NAMES)
def test_ungranted_tool_reason_blocks_memory_tools_without_rollover_label(name: str) -> None:
    spec = _make_spec()
    assert _ungranted_tool_reason(name, spec, "claude-native") is not None


@pytest.mark.parametrize("name", _MEMORY_TOOL_NAMES)
def test_ungranted_tool_reason_allows_memory_tools_with_rollover_label(name: str) -> None:
    spec = _make_spec()
    reason = _ungranted_tool_reason(
        name, spec, "claude-native", labels={"omnigent.context.mode": "rollover"}
    )
    assert reason is None


# ── Native relay advertisement ────────────────────────────────────────────────


def test_relay_schemas_omit_memory_tools_without_rollover() -> None:
    spec = _make_spec()
    names = {s["name"] for s in build_native_relay_tool_schemas(spec)}
    for name in _MEMORY_TOOL_NAMES:
        assert name not in names


def test_relay_schemas_include_memory_tools_for_rollover() -> None:
    spec = _make_spec()
    names = {
        s["name"]
        for s in build_native_relay_tool_schemas(
            spec, labels={"omnigent.context.mode": "rollover"}
        )
    }
    for name in _MEMORY_TOOL_NAMES:
        assert name in names


def test_relay_schemas_no_spec_fallback_respects_rollover_gate() -> None:
    """The spec-less fallback surface still gates memory_* on the label."""
    names_off = {s["name"] for s in build_native_relay_tool_schemas(None)}
    names_on = {
        s["name"]
        for s in build_native_relay_tool_schemas(
            None, labels={"omnigent.context.mode": "rollover"}
        )
    }
    for name in _MEMORY_TOOL_NAMES:
        assert name not in names_off
        assert name in names_on


@pytest.mark.parametrize("name", _MEMORY_TOOL_NAMES)
def test_relay_lists_memory_tools_as_always_loaded(name: str) -> None:
    """Claude Code keeps MCP tools behind tool search unless the server marks
    one always-loaded; memory must be checked before answering, every turn."""
    from omnigent.harnesses.claude_native.bridge import _mcp_tool_schema_from_spec

    schema = _mcp_tool_schema_from_spec({"name": name, "parameters": {}})
    assert schema["_meta"] == {"anthropic/alwaysLoad": True}
