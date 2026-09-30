"""Unit tests for omnigent.context_assembly.blindfold (runner-side glue).

No live server: httpx.MockTransport stands in for the Omnigent server, same
pattern as tests/test_pi_native_resume.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context_assembly.blindfold import (
    BlindfoldTurnContext,
    fetch_blindfold_turn_context,
    is_blindfolded,
    post_oneshot_items,
)
from omnigent.context_assembly.oneshot_events import OneShotItem


def _item(item_id: str, *, role: str = "user") -> dict[str, Any]:
    return {"id": item_id, "type": "message", "role": role, "content": []}


# Desc order (newest first), matching what `order=desc` returns — item_new is
# the just-persisted message, item_a/item_b are its two most recent
# predecessors.
_RECENT_ITEMS_DESC = [_item("item_new"), _item("item_b", role="assistant"), _item("item_a")]


def _handler(
    *,
    items_status: int = 200,
    items_body: dict[str, Any] | None = None,
    context_status: int = 200,
    context_body: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    def _route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            assert request.url.params.get("order") == "desc"
            return httpx.Response(
                items_status,
                json=items_body if items_body is not None else {"data": _RECENT_ITEMS_DESC},
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
    async def _call(
        self, transport: httpx.MockTransport, **overrides: Any
    ) -> BlindfoldTurnContext:
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
        assert [i["id"] for i in ctx.prior_history_items] == ["item_a", "item_b"]
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
        assert ctx.prior_history_items == []

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
        assert ctx.prior_history_items == []
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
        assert ctx.prior_history_items == []

    @pytest.mark.asyncio
    async def test_missing_fallback_instructions_yields_empty_system_prompt(self) -> None:
        ctx = await self._call(_handler(items_body={"data": []}), fallback_instructions=None)
        assert ctx.system_prompt == ""


class TestServerConnectionFile:
    def test_round_trips_base_url_and_headers(self, tmp_path: Path) -> None:
        from omnigent.context_assembly.blindfold import (
            read_server_connection,
            write_server_connection,
        )

        write_server_connection(
            tmp_path, base_url="http://host:8780", headers={"Authorization": "Bearer x"}
        )
        conn = read_server_connection(tmp_path)
        assert conn is not None
        assert conn.base_url == "http://host:8780"
        assert conn.headers == {"Authorization": "Bearer x"}
        assert conn.blindfolded is None

    def test_persists_blindfolded_flag_when_labels_given(self, tmp_path: Path) -> None:
        from omnigent.context_assembly.blindfold import (
            read_server_connection,
            write_server_connection,
        )

        write_server_connection(
            tmp_path,
            base_url="http://host:8780",
            headers={},
            labels={"omnigent.blindfold": "true"},
        )
        conn = read_server_connection(tmp_path)
        assert conn is not None
        assert conn.blindfolded is True

    def test_unlabelled_session_leaves_no_file(self, tmp_path: Path) -> None:
        from omnigent.context_assembly.blindfold import (
            read_server_connection,
            write_server_connection,
        )

        write_server_connection(
            tmp_path, base_url="http://host:8780", headers={"Authorization": "Bearer x"}, labels={}
        )
        # OFF path: no auth headers on disk and nothing for a turn to read.
        assert read_server_connection(tmp_path) is None
        assert not any(tmp_path.iterdir())

    def test_labelless_write_never_overwrites_a_known_state(self, tmp_path: Path) -> None:
        from omnigent.context_assembly.blindfold import (
            read_server_connection,
            write_server_connection,
        )

        write_server_connection(
            tmp_path, base_url="http://a:1", headers={}, labels={"omnigent.blindfold": "true"}
        )
        write_server_connection(tmp_path, base_url="http://b:2", headers={})
        conn = read_server_connection(tmp_path)
        assert conn is not None
        assert conn.blindfolded is True
        assert conn.base_url == "http://a:1"

    def test_blindfolded_file_is_owner_only(self, tmp_path: Path) -> None:
        from omnigent.context_assembly.blindfold import write_server_connection

        write_server_connection(
            tmp_path, base_url="http://a:1", headers={}, labels={"omnigent.blindfold": "true"}
        )
        (path,) = list(tmp_path.iterdir())
        assert path.stat().st_mode & 0o777 == 0o600

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        from omnigent.context_assembly.blindfold import read_server_connection

        assert read_server_connection(tmp_path) is None

    def test_malformed_file_returns_none(self, tmp_path: Path) -> None:
        from omnigent.context_assembly.blindfold import (
            _CONNECTION_FILE,
            read_server_connection,
        )

        (tmp_path / _CONNECTION_FILE).write_text("not json", encoding="utf-8")
        assert read_server_connection(tmp_path) is None


class TestOneShotEnv:
    def test_keeps_only_the_listed_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnigent.context_assembly.blindfold import one_shot_env

        monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
        monkeypatch.setenv("OPENAI_API_KEY", "b")
        monkeypatch.setenv("OPENROUTER_API_KEY", "c")
        monkeypatch.setenv("GITHUB_TOKEN", "d")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = one_shot_env(keep=frozenset({"ANTHROPIC_API_KEY"}))
        assert env["ANTHROPIC_API_KEY"] == "a"
        assert "OPENAI_API_KEY" not in env
        assert "OPENROUTER_API_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert env["PATH"] == "/usr/bin"


class TestPostOneShotItems:
    """Posting a one-shot turn's parsed items — see maybe_run_blindfold_turn's
    callers, which already deliver the turn's final answer via
    ``TurnComplete.response``; this must never post that answer again."""

    def _capturing_transport(self, posted: list[dict[str, Any]]) -> httpx.MockTransport:
        def _route(request: httpx.Request) -> httpx.Response:
            posted.append(json.loads(request.content))
            return httpx.Response(200)

        return httpx.MockTransport(_route)

    async def test_skips_the_last_assistant_message_but_posts_everything_else(
        self,
    ) -> None:
        posted: list[dict[str, Any]] = []
        items = [
            OneShotItem("reasoning", {"agent": "X", "summary": [], "content": []}),
            OneShotItem(
                "function_call",
                {"agent": "X", "name": "read", "arguments": "{}", "call_id": "c1"},
            ),
            OneShotItem("function_call_output", {"call_id": "c1", "output": "ok"}),
            OneShotItem(
                "message",
                {
                    "role": "assistant",
                    "agent": "X",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ),
        ]
        async with httpx.AsyncClient(
            base_url="http://server", transport=self._capturing_transport(posted)
        ) as client:
            await post_oneshot_items(
                client, session_id="conv_1", response_id="turn_1", items=items, final_text="done"
            )
        posted_types = [entry["data"]["item_type"] for entry in posted]
        assert posted_types == ["reasoning", "function_call", "function_call_output"]
        assert all(entry["data"]["response_id"] == "turn_1" for entry in posted)

    async def test_only_the_last_of_several_assistant_messages_is_skipped(self) -> None:
        posted: list[dict[str, Any]] = []
        first = OneShotItem(
            "message",
            {
                "role": "assistant",
                "agent": "X",
                "content": [{"type": "output_text", "text": "first"}],
            },
        )
        last = OneShotItem(
            "message",
            {
                "role": "assistant",
                "agent": "X",
                "content": [{"type": "output_text", "text": "last"}],
            },
        )
        async with httpx.AsyncClient(
            base_url="http://server", transport=self._capturing_transport(posted)
        ) as client:
            await post_oneshot_items(
                client,
                session_id="conv_1",
                response_id="turn_1",
                items=[first, last],
                final_text="last",
            )
        assert len(posted) == 1
        assert posted[0]["data"]["item_data"]["content"][0]["text"] == "first"

    async def test_a_last_message_that_is_not_the_final_answer_is_kept(self) -> None:
        posted: list[dict[str, Any]] = []
        last = OneShotItem(
            "message",
            {
                "role": "assistant",
                "agent": "X",
                "content": [{"type": "output_text", "text": "partial"}],
            },
        )
        async with httpx.AsyncClient(
            base_url="http://server", transport=self._capturing_transport(posted)
        ) as client:
            await post_oneshot_items(
                client,
                session_id="conv_1",
                response_id="turn_1",
                items=[last],
                final_text="something else",
            )
        assert len(posted) == 1

    async def test_no_items_posts_nothing(self) -> None:
        posted: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            base_url="http://server", transport=self._capturing_transport(posted)
        ) as client:
            await post_oneshot_items(
                client, session_id="conv_1", response_id="turn_1", items=[], final_text=None
            )
        assert posted == []

    async def test_a_post_failure_is_swallowed_and_does_not_block_later_items(self) -> None:
        posted: list[dict[str, Any]] = []
        calls = 0

        def _flaky_route(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(500)
            posted.append(json.loads(request.content))
            return httpx.Response(200)

        items = [
            OneShotItem("reasoning", {"agent": "X", "summary": [], "content": []}),
            OneShotItem("function_call_output", {"call_id": "c1", "output": "ok"}),
        ]
        async with httpx.AsyncClient(
            base_url="http://server", transport=httpx.MockTransport(_flaky_route)
        ) as client:
            await post_oneshot_items(
                client, session_id="conv_1", response_id="turn_1", items=items, final_text=None
            )
        assert len(posted) == 1
        assert posted[0]["data"]["item_type"] == "function_call_output"
