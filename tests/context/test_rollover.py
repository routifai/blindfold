"""Unit tests for omnigent.context.rollover."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.context.rollover import (
    CHECKPOINT_HEADER,
    DEFAULT_ROLLOVER_THRESHOLD_TOKENS,
    SUMMARIZER_DATE_PLACEHOLDER,
    build_rollover_item,
    estimate_context_tokens,
    resolve_keep_messages,
    resolve_keep_tokens,
    resolve_rollover_threshold,
    select_recent,
    state_file_summarizer_instruction,
)
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.runtime.compaction import count_tokens
from omnigent.server.routes._sessions.common import (
    _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
)

# Generous enough that no test below hits it unless it's specifically
# exercising the token budget.
_HUGE_TOKENS = 1_000_000


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


def _turn(n: int, *, with_tool: bool = False) -> list[dict[str, Any]]:
    """One whole turn: a user message, optionally a tool round-trip, then
    an assistant reply — the unit select_recent now walks in whole."""
    items = [_msg(f"u{n}", "user", f"question {n}", response_id=f"r{n}")]
    if with_tool:
        items += _tool_pair(f"c{n}", f"r{n}")
    items.append(_msg(f"a{n}", "assistant", f"answer {n}", response_id=f"r{n}"))
    return items


class _ReturnsTextClient:
    """LLM client stub returning a fixed summary, matching the real Response shape."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.call_count = 0
        self.seen_messages: list[dict[str, Any]] | None = None
        self.seen_instructions: str | None = None

    class _Responses:
        def __init__(self, outer: _ReturnsTextClient) -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> Response:
            self._outer.call_count += 1
            self._outer.seen_messages = kwargs.get("input")
            self._outer.seen_instructions = kwargs.get("instructions")
            return Response(
                output=[MessageOutput(content=[OutputText(text=self._outer._text)])],
                model="test-model",
            )

    @property
    def responses(self) -> _ReturnsTextClient._Responses:
        return self._Responses(self)


# ── select_recent: whole-turn selection ─────────────────────────────────


def test_select_recent_keeps_whole_trailing_turns() -> None:
    items = _turn(1) + _turn(2) + _turn(3)
    # Each turn is 2 messages; keep_messages=2 fits exactly one whole turn.
    result = select_recent(items, keep_messages=2, keep_tokens=_HUGE_TOKENS, model="gpt-4o")
    assert [i["id"] for i in result] == ["u3", "a3"]


def test_select_recent_keeps_tool_items_riding_within_their_turn() -> None:
    items = _turn(1) + _turn(2, with_tool=True)
    result = select_recent(items, keep_messages=2, keep_tokens=_HUGE_TOKENS, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "fc_c2", "fo_c2", "a2"]


def test_select_recent_tail_always_starts_at_a_user_message() -> None:
    """No configuration of the two budgets can ever start the tail on an
    assistant message — a turn is kept whole or not at all."""
    items = _turn(1) + _turn(2)
    for keep_messages in range(6):
        result = select_recent(
            items, keep_messages=keep_messages, keep_tokens=_HUGE_TOKENS, model="gpt-4o"
        )
        if result:
            assert result[0]["role"] == "user", (
                f"keep_messages={keep_messages} started the tail on {result[0]!r}"
            )


def test_select_recent_never_splits_a_turn_message_budget() -> None:
    items = _turn(1) + _turn(2)
    # The last turn alone is 2 messages; keep_messages=1 can't fit it, so the
    # whole turn (not half of it) is dropped rather than orphaning a message.
    result = select_recent(items, keep_messages=1, keep_tokens=_HUGE_TOKENS, model="gpt-4o")
    assert result == []


def test_select_recent_empty_tail_when_latest_turn_exceeds_token_budget() -> None:
    items = _turn(1) + _turn(2)
    tiny_budget = 1
    result = select_recent(items, keep_messages=100, keep_tokens=tiny_budget, model="gpt-4o")
    assert result == []


def test_select_recent_window_always_ends_at_latest_item() -> None:
    items = _turn(1)
    result = select_recent(items, keep_messages=10, keep_tokens=_HUGE_TOKENS, model="gpt-4o")
    assert [i["id"] for i in result] == ["u1", "a1"]


def test_select_recent_empty_items() -> None:
    assert select_recent([], keep_messages=5, keep_tokens=_HUGE_TOKENS, model="gpt-4o") == []


def test_select_recent_no_user_message_is_empty_tail() -> None:
    # Malformed/partial record with no turn to anchor a tail on.
    items = [_msg("a1", "assistant", "orphan reply")]
    result = select_recent(items, keep_messages=5, keep_tokens=_HUGE_TOKENS, model="gpt-4o")
    assert result == []


def test_select_recent_adds_multiple_whole_turns_when_budget_allows() -> None:
    items = _turn(1) + _turn(2) + _turn(3)
    result = select_recent(items, keep_messages=4, keep_tokens=_HUGE_TOKENS, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "a2", "u3", "a3"]


# ── threshold / keep-budget resolution ──────────────────────────────────


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


def test_resolve_keep_tokens_default_and_override() -> None:
    assert resolve_keep_tokens(None) == 16_000
    assert resolve_keep_tokens({"omnigent.context.rollover_keep_tokens": "5000"}) == 5000
    assert resolve_keep_tokens({"omnigent.context.rollover_keep_tokens": "-1"}) == 16_000


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
    items = _turn(1) + _turn(2) + _turn(3)
    client = _ReturnsTextClient("ROLLING SUMMARY")

    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_messages=2,
        keep_tokens=_HUGE_TOKENS,
        model="gpt-4o",
        llm_client=client,
    )

    assert data.summary == f"{CHECKPOINT_HEADER}\n\nROLLING SUMMARY"
    assert data.last_item_id == "a3"
    # token_count is the summary + kept tail estimate, not just the summary.
    assert data.token_count == count_tokens(data.compacted_messages, "gpt-4o")
    assert data.compacted_messages is not None
    assert [m.get("type") for m in data.compacted_messages] == [
        "message",
        "message",
        "message",
        "message",
    ]
    assert data.compacted_messages[-2]["id"] == "u3"
    assert data.compacted_messages[-1]["id"] == "a3"


@pytest.mark.asyncio
async def test_build_rollover_item_empty_tail_when_latest_turn_too_big() -> None:
    items = _turn(1) + _turn(2)
    client = _ReturnsTextClient("ROLLING SUMMARY")

    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_messages=100,
        keep_tokens=1,
        model="gpt-4o",
        llm_client=client,
    )

    # Only the summary pair — no room for even the latest turn.
    assert len(data.compacted_messages) == 2
    assert data.compacted_messages[0]["role"] == "user"
    assert data.compacted_messages[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_build_rollover_item_passes_state_file_instruction() -> None:
    items = _turn(1)
    client = _ReturnsTextClient("SUMMARY")

    await build_rollover_item(
        items,
        previous_summary=None,
        keep_messages=20,
        keep_tokens=_HUGE_TOKENS,
        model="gpt-4o",
        llm_client=client,
    )

    assert client.seen_instructions is not None
    assert "state file" in client.seen_instructions
    assert "Context checkpoint" in client.seen_instructions


@pytest.mark.asyncio
async def test_build_rollover_item_feeds_previous_summary_for_progressive_summarization() -> None:
    items = _turn(1)
    client = _ReturnsTextClient("NEW SUMMARY")

    await build_rollover_item(
        items,
        previous_summary=f"{CHECKPOINT_HEADER}\n\nOLD SUMMARY of earlier turns",
        keep_messages=20,
        keep_tokens=_HUGE_TOKENS,
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
            keep_tokens=_HUGE_TOKENS,
            model="gpt-4o",
            llm_client=_ReturnsTextClient("x"),
        )


@pytest.mark.asyncio
async def test_rollover_item_accepted_by_claude_native_resume_rebuild(tmp_path: Path) -> None:
    items = _turn(1) + _turn(2) + _turn(3) + _turn(4)
    client = _ReturnsTextClient("ROLLING SUMMARY")
    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_messages=2,
        keep_tokens=_HUGE_TOKENS,
        model="gpt-4o",
        llm_client=client,
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
    # Only the compact boundary + summary exchange + the last whole turn.
    assert [r.get("type") for r in records] == ["system", "user", "assistant", "user", "assistant"]
    assert records[0].get("subtype") == "compact_boundary"
    last_texts = [b.get("text") for b in records[-1]["message"]["content"] if isinstance(b, dict)]
    assert last_texts == ["answer 4"]


@pytest.mark.asyncio
async def test_rollover_item_accepted_by_codex_native_resume_rebuild(tmp_path: Path) -> None:
    items = _turn(1) + _turn(2) + _turn(3) + _turn(4)
    client = _ReturnsTextClient("ROLLING SUMMARY")
    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_messages=2,
        keep_tokens=_HUGE_TOKENS,
        model="gpt-4o",
        llm_client=client,
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
    assert records[1]["payload"]["message"] == f"{CHECKPOINT_HEADER}\n\nROLLING SUMMARY"
    kept_ids = [m.get("id") for m in replacement_history if "id" in m]
    assert kept_ids == ["u4", "a4"]


def test_state_file_summarizer_instruction_defaults_to_utc_today() -> None:
    instruction = state_file_summarizer_instruction()
    assert "## Context checkpoint —" in instruction
    assert SUMMARIZER_DATE_PLACEHOLDER not in instruction


def test_state_file_summarizer_instruction_accepts_a_caller_supplied_date() -> None:
    """The pi-native bridge passes SUMMARIZER_DATE_PLACEHOLDER at launch time
    and substitutes the real date itself, later, at actual compaction time."""
    instruction = state_file_summarizer_instruction(today=SUMMARIZER_DATE_PLACEHOLDER)
    assert f"## Context checkpoint — {SUMMARIZER_DATE_PLACEHOLDER}" in instruction


def test_items_for_summarizer_keeps_only_provider_schema_fields() -> None:
    from omnigent.context.rollover import _items_for_summarizer

    items = [
        {
            "id": "m1",
            "type": "message",
            "role": "user",
            "status": "completed",
            "stream_message_id": "s1",
            "content": [{"type": "input_text", "text": "hi", "extra": 1}],
        },
        {"id": "r1", "type": "reasoning", "summary": []},
        {"id": "e1", "type": "native_tool", "name": "shell"},
        {"id": "f1", "type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
        {"id": "o1", "type": "function_call_output", "call_id": "c1", "output": "ok", "x": 2},
    ]
    assert _items_for_summarizer(items) == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]


@pytest.mark.asyncio
async def test_checkpoint_marker_differs_from_the_summarizer_request() -> None:
    """The CLI must see a marker that's plainly not from the user, while the
    summarizer keeps the request wording it expects for a previous summary."""
    from omnigent.context.rollover import _CHECKPOINT_MARKER_TEXT, _SUMMARY_REQUEST_TEXT

    client = _ReturnsTextClient("ROLLING SUMMARY")
    data = await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary="OLD SUMMARY",
        keep_messages=2,
        keep_tokens=_HUGE_TOKENS,
        model="gpt-4o",
        llm_client=client,
    )
    marker = data.compacted_messages[0]["content"][0]["text"]
    assert marker == _CHECKPOINT_MARKER_TEXT
    assert "not a message from the user" in marker
    assert _SUMMARY_REQUEST_TEXT != _CHECKPOINT_MARKER_TEXT


@pytest.mark.asyncio
async def test_summarizer_gets_one_transcript_message_not_live_turns() -> None:
    """Live chat turns let a weak summarizer continue the chat; a single quoted
    transcript (with the previous summary on top) can only be summarized."""
    client = _ReturnsTextClient("ROLLING SUMMARY")
    await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary="OLD SUMMARY",
        keep_messages=2,
        keep_tokens=_HUGE_TOKENS,
        model="gpt-4o",
        llm_client=client,
    )
    assert client.seen_messages is not None
    user_texts = [
        block["text"]
        for message in client.seen_messages
        if message.get("role") == "user"
        for block in message["content"]
        if isinstance(block, dict) and "text" in block
    ]
    transcript = user_texts[0]
    assert "OLD SUMMARY" in transcript
    assert "<conversation>" in transcript
    assert not any(m.get("role") == "assistant" for m in client.seen_messages)
    assert "automatically generated summary" in client.seen_instructions + transcript
