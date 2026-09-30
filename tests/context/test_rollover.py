"""Unit tests for omnigent.context.rollover."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.context.rollover import (
    DEFAULT_ROLLOVER_THRESHOLD_TOKENS,
    build_rollover_item,
    estimate_context_tokens,
    resolve_keep_messages,
    resolve_rollover_threshold,
    select_recent,
)
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.server.routes._sessions.common import (
    _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
)


def _msg(item_id: str, role: str, text: str, response_id: str | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "response_id": response_id or f"resp_{item_id}",
        "created_at": 1,
        "role": role,
        "content": [
            {"type": "input_text" if role == "user" else "output_text", "text": text},
        ],
    }


def _tool_pair(call_id: str, response_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"fc_{call_id}",
            "type": "function_call",
            "status": "completed",
            "response_id": response_id,
            "created_at": 1,
            "call_id": call_id,
            "name": "some_tool",
            "arguments": "{}",
        },
        {
            "id": f"fo_{call_id}",
            "type": "function_call_output",
            "status": "completed",
            "response_id": response_id,
            "created_at": 1,
            "call_id": call_id,
            "output": "ok",
        },
    ]


class _ReturnsTextClient:
    """LLM client stub returning a fixed summary, matching the real Response shape."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.call_count = 0
        self.seen_messages: list[dict[str, Any]] | None = None

    class _Responses:
        def __init__(self, outer: _ReturnsTextClient) -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> Response:
            self._outer.call_count += 1
            self._outer.seen_messages = kwargs.get("input")
            return Response(
                output=[MessageOutput(content=[OutputText(text=self._outer._text)])],
                model="test-model",
            )

    @property
    def responses(self) -> _ReturnsTextClient._Responses:
        return self._Responses(self)


# ── select_recent: counting + ride-along rule ──────────────────────────


def test_select_recent_counts_only_messages() -> None:
    items = [_msg("m1", "user", "a"), _msg("m2", "assistant", "b"), _msg("m3", "user", "c")]
    result = select_recent(items, keep_messages=2)
    assert [i["id"] for i in result] == ["m2", "m3"]


def test_select_recent_keeps_tool_items_riding_with_their_message() -> None:
    items = [
        _msg("m1", "user", "a", response_id="r1"),
        *_tool_pair("c1", "r2"),
        _msg("m2", "assistant", "b", response_id="r2"),
        _msg("m3", "user", "c", response_id="r3"),
    ]
    result = select_recent(items, keep_messages=2)
    # m2's tool round-trip rides along even though only messages are counted.
    assert [i["id"] for i in result] == ["fc_c1", "fo_c1", "m2", "m3"]


def test_select_recent_does_not_pull_in_a_different_messages_tools() -> None:
    items = [
        *_tool_pair("c1", "r1"),
        _msg("m1", "assistant", "a", response_id="r1"),
        _msg("m2", "user", "b", response_id="r2"),
    ]
    # keep_messages=1: only m2 is kept, and c1's tool pair belongs to m1's
    # group (a different response_id) so it must not ride along.
    result = select_recent(items, keep_messages=1)
    assert [i["id"] for i in result] == ["m2"]


def test_select_recent_window_always_ends_at_latest_item() -> None:
    items = [_msg("m1", "user", "a"), _msg("m2", "assistant", "b")]
    result = select_recent(items, keep_messages=10)
    assert [i["id"] for i in result] == ["m1", "m2"]


def test_select_recent_empty_items() -> None:
    assert select_recent([], keep_messages=5) == []


def test_select_recent_clamps_keep_messages_to_at_least_one() -> None:
    items = [_msg("m1", "user", "a"), _msg("m2", "assistant", "b")]
    result = select_recent(items, keep_messages=0)
    assert [i["id"] for i in result] == ["m2"]


# ── threshold resolution ────────────────────────────────────────────────


def test_threshold_default_is_90k_when_no_label_or_window() -> None:
    assert resolve_rollover_threshold(None) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS
    assert resolve_rollover_threshold({}) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_threshold_falls_back_to_45pct_of_context_window() -> None:
    labels = {_LAST_CONTEXT_WINDOW_LABEL_KEY: "200000"}
    assert resolve_rollover_threshold(labels) == 90_000


def test_threshold_explicit_label_wins_over_window() -> None:
    labels = {
        "omnigent.context.rollover_at_tokens": "12345",
        _LAST_CONTEXT_WINDOW_LABEL_KEY: "200000",
    }
    assert resolve_rollover_threshold(labels) == 12345


def test_threshold_ignores_invalid_label() -> None:
    labels = {"omnigent.context.rollover_at_tokens": "not-a-number"}
    assert resolve_rollover_threshold(labels) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_resolve_keep_messages_default_and_override() -> None:
    assert resolve_keep_messages(None) == 20
    assert resolve_keep_messages({"omnigent.context.rollover_keep_messages": "5"}) == 5
    assert resolve_keep_messages({"omnigent.context.rollover_keep_messages": "-1"}) == 20


# ── estimate_context_tokens ──────────────────────────────────────────────


def test_estimate_prefers_reported_usage_label() -> None:
    items = [_msg("m1", "user", "hello world")]
    labels = {_LAST_CONTEXT_TOKENS_LABEL_KEY: "4242"}
    assert estimate_context_tokens(items, model="gpt-4o", labels=labels) == 4242


def test_estimate_falls_back_to_count_tokens_without_label() -> None:
    items = [_msg("m1", "user", "hello world")]
    estimate = estimate_context_tokens(items, model="gpt-4o", labels=None)
    assert estimate > 0


def test_estimate_falls_back_when_label_invalid() -> None:
    items = [_msg("m1", "user", "hello world")]
    labels = {_LAST_CONTEXT_TOKENS_LABEL_KEY: "garbage"}
    estimate = estimate_context_tokens(items, model="gpt-4o", labels=labels)
    assert estimate > 0


# ── build_rollover_item + resume-rebuilder acceptance ────────────────────


@pytest.mark.asyncio
async def test_build_rollover_item_shape() -> None:
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(6)]
    client = _ReturnsTextClient("ROLLING SUMMARY")

    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_messages=2,
        model="gpt-4o",
        llm_client=client,
    )

    assert data.summary == "ROLLING SUMMARY"
    assert data.last_item_id == "m5"
    assert data.token_count > 0
    assert data.compacted_messages is not None
    # Summary pair first, then the kept tail (last 2 of 6 messages).
    assert [m.get("type") for m in data.compacted_messages] == [
        "message",
        "message",
        "message",
        "message",
    ]
    assert data.compacted_messages[-2]["id"] == "m4"
    assert data.compacted_messages[-1]["id"] == "m5"


@pytest.mark.asyncio
async def test_build_rollover_item_feeds_previous_summary_for_progressive_summarization() -> None:
    items = [_msg("m1", "user", "new stuff")]
    client = _ReturnsTextClient("NEW SUMMARY")

    await build_rollover_item(
        items,
        previous_summary="OLD SUMMARY of earlier turns",
        keep_messages=20,
        model="gpt-4o",
        llm_client=client,
    )

    assert client.seen_messages is not None
    first_text = client.seen_messages[0]["content"][0]["text"]
    assert "automatically generated summary" in first_text


@pytest.mark.asyncio
async def test_build_rollover_item_requires_nonempty_items() -> None:
    with pytest.raises(ValueError):
        await build_rollover_item(
            [],
            previous_summary=None,
            keep_messages=5,
            model="gpt-4o",
            llm_client=_ReturnsTextClient("x"),
        )


@pytest.mark.asyncio
async def test_rollover_item_accepted_by_claude_native_resume_rebuild(tmp_path: Path) -> None:
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(8)]
    client = _ReturnsTextClient("ROLLING SUMMARY")
    data = await build_rollover_item(
        items, previous_summary=None, keep_messages=2, model="gpt-4o", llm_client=client
    )
    compaction_item = {
        "id": "comp_1",
        "type": "compaction",
        "status": "completed",
        "response_id": "resp_comp_1",
        "created_at": 2,
        "summary": data.summary,
        "last_item_id": data.last_item_id,
        "token_count": data.token_count,
        "compacted_messages": data.compacted_messages,
    }
    records = claude_native._claude_transcript_records_from_session_items(
        [*items, compaction_item],
        session_id="conv_test",
        external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
    )
    # Only the compact boundary + summary exchange + the 2 kept messages.
    assert [r.get("type") for r in records] == ["system", "user", "assistant", "user", "assistant"]
    assert records[0].get("subtype") == "compact_boundary"
    last_texts = [b.get("text") for b in records[-1]["message"]["content"] if isinstance(b, dict)]
    assert last_texts == ["msg 7"]


@pytest.mark.asyncio
async def test_rollover_item_accepted_by_codex_native_resume_rebuild(tmp_path: Path) -> None:
    items = [_msg(f"m{i}", "user" if i % 2 == 0 else "assistant", f"msg {i}") for i in range(8)]
    client = _ReturnsTextClient("ROLLING SUMMARY")
    data = await build_rollover_item(
        items, previous_summary=None, keep_messages=2, model="gpt-4o", llm_client=client
    )
    compaction_item = {
        "id": "comp_1",
        "type": "compaction",
        "status": "completed",
        "response_id": "resp_comp_1",
        "created_at": 2,
        "summary": data.summary,
        "last_item_id": data.last_item_id,
        "token_count": data.token_count,
        "compacted_messages": data.compacted_messages,
    }
    records = codex_native._codex_rollout_records_from_session_items(
        [*items, compaction_item],
        session_id="conv_test",
        external_session_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
        cwd=tmp_path,
        model_provider="omnigent_test",
        cli_version="0.999.0",
    )
    record_types = [r.get("type") for r in records]
    assert record_types == ["session_meta", "compacted"]
    replacement_history = records[1]["payload"]["replacement_history"]
    assert records[1]["payload"]["message"] == "ROLLING SUMMARY"
    kept_ids = [m.get("id") for m in replacement_history if "id" in m]
    assert kept_ids == ["m6", "m7"]
