"""Unit tests for omnigent.context.rollover."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.context.rollover import (
    CHECKPOINT_HEADER,
    DEFAULT_ROLLOVER_THRESHOLD_TOKENS,
    MAX_DEFAULT_ROLLOVER_THRESHOLD_TOKENS,
    MIN_ROLLOVER_THRESHOLD_TOKENS,
    SUMMARIZER_DATE_PLACEHOLDER,
    build_rollover_item,
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


def test_select_recent_always_keeps_the_last_turn() -> None:
    """Rollover runs right after a turn finishes; that turn stays verbatim
    even when it alone is over budget."""
    items = _turn(1) + _turn(2)
    result = select_recent(items, keep_tokens=1, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "a2"]


def test_select_recent_keeps_tool_items_riding_within_their_turn() -> None:
    items = _turn(1) + _turn(2, with_tool=True)
    result = select_recent(items, keep_tokens=1, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "fc_c2", "fo_c2", "a2"]


def test_select_recent_adds_earlier_whole_turns_within_budget() -> None:
    items = _turn(1) + _turn(2) + _turn(3)
    two_turns = count_tokens(items[2:], "gpt-4o")
    result = select_recent(items, keep_tokens=two_turns, model="gpt-4o")
    assert [i["id"] for i in result] == ["u2", "a2", "u3", "a3"]


def test_select_recent_tail_always_starts_at_a_user_message() -> None:
    items = _turn(1) + _turn(2, with_tool=True) + _turn(3)
    for budget in (1, 50, 200, _HUGE_TOKENS):
        result = select_recent(items, keep_tokens=budget, model="gpt-4o")
        assert result[0]["role"] == "user", f"budget={budget} started on {result[0]!r}"


def test_select_recent_empty_items() -> None:
    assert select_recent([], keep_tokens=_HUGE_TOKENS, model="gpt-4o") == []


def test_select_recent_no_user_message_is_empty_tail() -> None:
    # Malformed/partial record with no turn to anchor a tail on.
    items = [_msg("a1", "assistant", "orphan reply")]
    assert select_recent(items, keep_tokens=_HUGE_TOKENS, model="gpt-4o") == []


# ── threshold / keep-budget resolution ──────────────────────────────────


def test_threshold_default_when_no_label_or_window() -> None:
    assert resolve_rollover_threshold(None) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS
    assert resolve_rollover_threshold({}) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_threshold_falls_back_to_60pct_of_context_window() -> None:
    labels = {_LAST_CONTEXT_WINDOW_LABEL_KEY: "300000"}
    assert resolve_rollover_threshold(labels) == 180_000


def test_threshold_default_caps_at_max_for_a_huge_window() -> None:
    # 60% of 1M would be 600,000; the default is capped at 200,000.
    labels = {_LAST_CONTEXT_WINDOW_LABEL_KEY: "1000000"}
    assert resolve_rollover_threshold(labels) == MAX_DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_threshold_explicit_label_is_not_capped() -> None:
    # The cap applies only to the derived default, never to an explicit label.
    labels = {
        "omnigent.context.rollover_at_tokens": "250000",
        _LAST_CONTEXT_WINDOW_LABEL_KEY: "1000000",
    }
    assert resolve_rollover_threshold(labels) == 250_000


def test_threshold_uses_model_window_when_no_window_label() -> None:
    assert resolve_rollover_threshold(None, model_window=300_000) == 180_000


def test_threshold_window_label_wins_over_model_window() -> None:
    # 60% of the labeled 100,000 window is below the floor, so the 80%-of-window
    # floor applies (80,000) — proof the much larger model_window is ignored.
    labels = {_LAST_CONTEXT_WINDOW_LABEL_KEY: "100000"}
    assert resolve_rollover_threshold(labels, model_window=1_000_000) == 80_000


def test_threshold_ignores_non_positive_model_window() -> None:
    assert resolve_rollover_threshold(None, model_window=0) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_threshold_explicit_label_wins_over_window() -> None:
    labels = {
        "omnigent.context.rollover_at_tokens": "150000",
        _LAST_CONTEXT_WINDOW_LABEL_KEY: "400000",
    }
    assert resolve_rollover_threshold(labels) == 150_000


def test_threshold_never_drops_below_the_floor() -> None:
    assert (
        resolve_rollover_threshold({"omnigent.context.rollover_at_tokens": "12345"})
        == MIN_ROLLOVER_THRESHOLD_TOKENS
    )
    # A small window caps the floor so the CLI still compacts before its limit.
    labels = {
        "omnigent.context.rollover_at_tokens": "5000",
        _LAST_CONTEXT_WINDOW_LABEL_KEY: "100000",
    }
    assert resolve_rollover_threshold(labels) == 80_000


def test_threshold_ignores_invalid_label() -> None:
    labels = {"omnigent.context.rollover_at_tokens": "not-a-number"}
    assert resolve_rollover_threshold(labels) == DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def test_resolve_keep_tokens_default_and_override() -> None:
    assert resolve_keep_tokens(None) == 16_000
    assert resolve_keep_tokens({"omnigent.context.rollover_keep_tokens": "5000"}) == 5000
    assert resolve_keep_tokens({"omnigent.context.rollover_keep_tokens": "-1"}) == 16_000


# ── build_rollover_item + resume-rebuilder acceptance ────────────────────


@pytest.mark.asyncio
async def test_build_rollover_item_shape() -> None:
    items = _turn(1) + _turn(2) + _turn(3)
    client = _ReturnsTextClient("ROLLING SUMMARY")

    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_tokens=1,
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
async def test_build_rollover_item_keeps_the_last_turn_even_over_budget() -> None:
    items = _turn(1) + _turn(2)
    client = _ReturnsTextClient("ROLLING SUMMARY")

    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_tokens=1,
        model="gpt-4o",
        llm_client=client,
    )

    # Only the summary pair — no room for even the latest turn.
    assert len(data.compacted_messages) == 4
    assert [m.get("id") for m in data.compacted_messages[2:]] == ["u2", "a2"]
    assert data.compacted_messages[0]["role"] == "user"
    assert data.compacted_messages[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_build_rollover_item_passes_state_file_instruction() -> None:
    items = _turn(1)
    client = _ReturnsTextClient("SUMMARY")

    await build_rollover_item(
        items,
        previous_summary=None,
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
        keep_tokens=1,
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
        keep_tokens=1,
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
    # The system marker is re-roled to developer; real turns keep their roles.
    assert replacement_history[0]["role"] == "developer"
    assert [m["role"] for m in replacement_history if "id" in m] == ["user", "assistant"]


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
    from omnigent.context.rollover import _SUMMARY_REQUEST_TEXT

    client = _ReturnsTextClient("ROLLING SUMMARY")
    data = await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary="OLD SUMMARY",
        keep_tokens=_HUGE_TOKENS,
        model="gpt-4o",
        llm_client=client,
    )
    marker = data.compacted_messages[0]["content"][0]["text"]
    assert marker == CHECKPOINT_HEADER
    assert "not a message from the user" in marker
    assert _SUMMARY_REQUEST_TEXT != CHECKPOINT_HEADER


@pytest.mark.asyncio
async def test_summarizer_gets_one_transcript_message_not_live_turns() -> None:
    """Live chat turns let a weak summarizer continue the chat; a single quoted
    transcript (with the previous summary on top) can only be summarized."""
    client = _ReturnsTextClient("ROLLING SUMMARY")
    await build_rollover_item(
        _turn(1) + _turn(2),
        previous_summary="OLD SUMMARY",
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


@pytest.mark.asyncio
async def test_kept_tool_output_is_capped_so_one_turn_cannot_loop_rollovers() -> None:
    """A huge tool result in the always-kept last turn is cut, so the relaunched
    context can drop under the threshold instead of rolling over every turn."""
    from omnigent.context.rollover import _KEPT_OUTPUT_MAX_CHARS

    items = _turn(1) + _turn(2, with_tool=True)
    output_item = next(i for i in items if i["type"] == "function_call_output")
    output_item["output"] = "x" * (_KEPT_OUTPUT_MAX_CHARS * 5)
    data = await build_rollover_item(
        items,
        previous_summary=None,
        keep_tokens=1,
        model="gpt-4o",
        llm_client=_ReturnsTextClient("S"),
    )
    kept = next(m for m in data.compacted_messages if m.get("type") == "function_call_output")
    assert len(kept["output"]) < _KEPT_OUTPUT_MAX_CHARS + 200
    assert "session_history" in kept["output"]
    assert len(output_item["output"]) == _KEPT_OUTPUT_MAX_CHARS * 5  # record untouched


@pytest.mark.asyncio
async def test_previous_header_is_not_fed_back_so_it_never_nests() -> None:
    client = _ReturnsTextClient("NEW SUMMARY")
    data = await build_rollover_item(
        _turn(1),
        previous_summary=f"{CHECKPOINT_HEADER}\n\nOLD SUMMARY",
        keep_tokens=1,
        model="gpt-4o",
        llm_client=client,
    )
    transcript = client.seen_messages[0]["content"][0]["text"]
    assert "OLD SUMMARY" in transcript
    assert CHECKPOINT_HEADER not in transcript
    assert data.summary.count(CHECKPOINT_HEADER) == 1


def test_checkpoint_marker_as_developer_only_touches_the_marker() -> None:
    """A user message that merely mentions the marker later is left alone."""
    from omnigent.context.rollover import CHECKPOINT_MARKER

    def msg(role: str, text: str) -> dict:
        return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}

    history = [
        msg("user", f"{CHECKPOINT_MARKER} rest"),
        msg("assistant", "summary"),
        msg("user", f"please explain {CHECKPOINT_MARKER}"),
    ]
    out = codex_native._checkpoint_marker_as_developer(history)
    assert [m["role"] for m in out] == ["developer", "assistant", "user"]
    assert history[0]["role"] == "user"
