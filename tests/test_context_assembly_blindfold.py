"""Unit tests for omnigent.context_assembly.blindfold (runner-side glue).

No live server: httpx.MockTransport stands in for the Omnigent server, same
pattern as tests/test_pi_native_resume.py.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.context_assembly.blindfold import (
    BlindfoldTurnContext,
    fetch_blindfold_turn_context,
    is_blindfolded,
)

_LAST_ITEM = {"id": "item_new", "type": "message", "role": "user", "content": []}


def _handler(
    *,
    items_status: int = 200,
    items_body: dict[str, Any] | None = None,
    context_status: int = 200,
    context_body: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    def _route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(
                items_status,
                json=items_body if items_body is not None else {"data": [_LAST_ITEM]},
            )
        if request.url.path.endswith("/context"):
            body = json.loads(request.content)
            assert body["new_item_id"] == "item_new"
            assert body["session"]["labels"] == {"omnigent.blindfold": "true"}
            return httpx.Response(
                context_status,
                json=context_body
                if context_body is not None
                else {
                    "contract_version": "0.2",
                    "turn_id": body["turn_id"],
                    "system": {"mode": "append", "text": "Rules.", "digest": "sha256:x"},
                    "memory": {"items": [], "digest": "sha256:y"},
                    "history": {
                        "summary": None,
                        "items": [{"ref": "item_a"}, {"ref": "item_b"}, {"ref": "item_new"}],
                        "digest": "sha256:z",
                    },
                    "audit": {
                        "memory_items": 0,
                        "history_items": 3,
                        "summary": False,
                        "estimated_tokens": 10,
                        "fallback": False,
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(_route)


class TestIsBlindfolded:
    def test_true_only_for_the_exact_label_value(self) -> None:
        assert is_blindfolded({"omnigent.blindfold": "true"}) is True
        assert is_blindfolded({"omnigent.blindfold": "false"}) is False
        assert is_blindfolded({}) is False
        assert is_blindfolded(None) is False
        assert is_blindfolded({"omnigent.blindfold": "TRUE"}) is False


class TestFetchBlindfoldTurnContext:
    async def _call(self, transport: httpx.MockTransport, **overrides: Any) -> BlindfoldTurnContext:
        async with httpx.AsyncClient(base_url="http://server", transport=transport) as client:
            kwargs: dict[str, Any] = {
                "session_id": "conv_1",
                "labels": {"omnigent.blindfold": "true"},
                "harness_name": "claude-native",
                "model": "claude-haiku-4-5-20251001",
                "context_window_tokens": 200_000,
                "history_format": "claude_jsonl",
                "fallback_instructions": "fallback text",
            }
            kwargs.update(overrides)
            return await fetch_blindfold_turn_context(client, **kwargs)

    @pytest.mark.asyncio
    async def test_happy_path_drops_the_new_message_from_prior_history(self) -> None:
        ctx = await self._call(_handler())
        assert ctx.prior_history_item_ids == ["item_a", "item_b"]
        assert ctx.system_prompt == "Rules."
        assert ctx.fallback is False

    @pytest.mark.asyncio
    async def test_memory_block_is_rendered_into_the_system_prompt(self) -> None:
        body = {
            "contract_version": "0.2",
            "turn_id": "t",
            "system": {"mode": "append", "text": "Rules.", "digest": "sha256:x"},
            "memory": {
                "items": [{"id": "m1", "kind": "fact", "text": "MANGO-7", "updated_at": 0}],
                "digest": "sha256:y",
            },
            "history": {"summary": None, "items": [{"ref": "item_new"}], "digest": "sha256:z"},
            "audit": {
                "memory_items": 1,
                "history_items": 1,
                "summary": False,
                "estimated_tokens": 0,
                "fallback": False,
            },
        }
        ctx = await self._call(_handler(context_body=body))
        assert "<long_term_memory>" in ctx.system_prompt
        assert "MANGO-7" in ctx.system_prompt
        assert ctx.prior_history_item_ids == []

    @pytest.mark.asyncio
    async def test_server_side_fallback_flag_is_propagated(self) -> None:
        body = {
            "contract_version": "0.2",
            "turn_id": "t",
            "system": {"mode": "append", "text": "Rules.", "digest": "sha256:x"},
            "memory": {"items": [], "digest": "sha256:y"},
            "history": {"summary": None, "items": [{"ref": "item_new"}], "digest": "sha256:z"},
            "audit": {
                "memory_items": 0,
                "history_items": 1,
                "summary": False,
                "estimated_tokens": 0,
                "fallback": True,
            },
        }
        ctx = await self._call(_handler(context_body=body))
        assert ctx.fallback is True

    @pytest.mark.asyncio
    async def test_network_failure_fails_closed_locally(self) -> None:
        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with httpx.AsyncClient(
            base_url="http://server", transport=httpx.MockTransport(_boom)
        ) as client:
            ctx = await fetch_blindfold_turn_context(
                client,
                session_id="conv_1",
                labels={"omnigent.blindfold": "true"},
                harness_name="claude-native",
                model="m",
                context_window_tokens=1000,
                history_format="claude_jsonl",
                fallback_instructions="fallback text",
            )
        assert ctx.system_prompt == "fallback text"
        assert ctx.prior_history_item_ids == []
        assert ctx.fallback is True

    @pytest.mark.asyncio
    async def test_server_error_status_fails_closed_locally(self) -> None:
        ctx = await self._call(_handler(context_status=500, context_body={"detail": "boom"}))
        assert ctx.fallback is True
        assert ctx.system_prompt == "fallback text"

    @pytest.mark.asyncio
    async def test_empty_record_fails_closed_locally(self) -> None:
        ctx = await self._call(_handler(items_body={"data": []}))
        assert ctx.fallback is True
        assert ctx.prior_history_item_ids == []

    @pytest.mark.asyncio
    async def test_missing_fallback_instructions_yields_empty_system_prompt(self) -> None:
        ctx = await self._call(_handler(items_body={"data": []}), fallback_instructions=None)
        assert ctx.system_prompt == ""
