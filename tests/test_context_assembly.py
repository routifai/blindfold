"""Unit tests for omnigent.context_assembly (context-assembly contract v0.2).

No network, no DB: `items_provider` is a plain callable over in-memory item
dicts, matching how the assembler is actually invoked (contract §1: "Both run
on the server ... in-process").
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.context_assembly import (
    AssembleRequest,
    Budget,
    HarnessCapabilities,
    HarnessInfo,
    ObserveRequest,
    RecordInfo,
    SessionRef,
    assemble,
    assemble_or_fail_closed,
    observe,
    render_system_text,
    select_history_refs,
)
from omnigent.context_assembly.labels import (
    BLINDFOLD_LABEL,
    DEFAULT_MAX_MESSAGES,
    MAX_MESSAGES_LABEL,
    MEMORY_FIXTURE_LABEL,
)


def _message_item(item_id: str, *, role: str, text: str, created_at: int) -> dict[str, Any]:
    """Build a flat item dict shaped like ``ConversationItem.to_api_dict()``."""
    api_type = "input_text" if role == "user" else "output_text"
    return {
        "id": item_id,
        "response_id": f"resp_{item_id}",
        "type": "message",
        "status": "completed",
        "created_at": created_at,
        "role": role,
        "content": [{"type": api_type, "text": text}],
    }


def _history(n: int, *, word_count: int = 1) -> list[dict[str, Any]]:
    """n alternating user/assistant items, each with the given word count."""
    items = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        text = " ".join([f"word{i}"] * word_count)
        items.append(_message_item(f"item_{i}", role=role, text=text, created_at=i))
    return items


def _turn_with_tool_call(prefix: str, *, response_id: str) -> list[dict[str, Any]]:
    """One assistant turn: a function_call + its output, then the assistant
    text message — all sharing *response_id*, matching real record order
    (tool round-trip precedes the final assistant message it belongs to)."""
    return [
        {
            "id": f"{prefix}_call",
            "response_id": response_id,
            "type": "function_call",
            "status": "completed",
            "created_at": 0,
            "name": "read_file",
            "call_id": f"{prefix}_call_id",
            "arguments": "{}",
        },
        {
            "id": f"{prefix}_output",
            "response_id": response_id,
            "type": "function_call_output",
            "status": "completed",
            "created_at": 0,
            "call_id": f"{prefix}_call_id",
            "output": "file contents",
        },
        {
            "id": f"{prefix}_msg",
            "response_id": response_id,
            "type": "message",
            "status": "completed",
            "created_at": 0,
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]


def _request(
    *,
    new_item_id: str,
    labels: dict[str, str] | None = None,
    max_input_tokens: int = 24_000,
    item_count: int = 1,
) -> AssembleRequest:
    return AssembleRequest(
        turn_id="turn_1",
        session=SessionRef(id="conv_1", owner="local", labels=labels or {}),
        harness=HarnessInfo(
            name="claude-native",
            model="claude-haiku-4-5-20251001",
            context_window_tokens=200_000,
            capabilities=HarnessCapabilities(history_format="claude_jsonl", images=True),
        ),
        new_item_id=new_item_id,
        record=RecordInfo(item_count=item_count, last_item_id=new_item_id),
        budget=Budget(max_input_tokens=max_input_tokens),
    )


class TestSelectHistoryRefs:
    def test_max_messages_one_returns_only_new_item(self) -> None:
        items = _history(10)
        refs, over = select_history_refs(
            items,
            new_item_id="item_9",
            max_messages=1,
            max_input_tokens=24_000,
            model="claude-haiku-4-5",
        )
        assert refs == ["item_9"]
        assert over is False

    def test_max_messages_covers_everything_when_the_window_is_bigger_than_history(self) -> None:
        items = _history(10)
        refs, over = select_history_refs(
            items,
            new_item_id="item_9",
            max_messages=20,
            max_input_tokens=24_000,
            model="claude-haiku-4-5",
        )
        assert refs == [f"item_{i}" for i in range(10)]
        assert over is False

    def test_max_messages_keeps_the_last_n_messages_ending_with_the_anchor(self) -> None:
        items = _history(10)
        refs, _ = select_history_refs(
            items,
            new_item_id="item_9",
            max_messages=3,
            max_input_tokens=24_000,
            model="claude-haiku-4-5",
        )
        assert refs == ["item_7", "item_8", "item_9"]

    def test_tool_calls_and_results_ride_with_their_assistant_message(self) -> None:
        # user -> [assistant turn with a tool round-trip] -> new user message.
        # max_messages=2 keeps the last 2 *messages* (the tool-turn's assistant
        # message + the new one), which must pull the tool call/output with it.
        items = [
            _message_item("u0", role="user", text="hi", created_at=0),
            *_turn_with_tool_call("t0", response_id="resp_t0"),
            _message_item("u1", role="user", text="new message", created_at=10),
        ]
        refs, _ = select_history_refs(
            items,
            new_item_id="u1",
            max_messages=2,
            max_input_tokens=24_000,
            model="claude-haiku-4-5",
        )
        assert refs == ["t0_call", "t0_output", "t0_msg", "u1"]

    def test_tool_round_trip_is_dropped_as_a_whole_group_when_out_of_window(self) -> None:
        items = [
            _message_item("u0", role="user", text="hi", created_at=0),
            *_turn_with_tool_call("t0", response_id="resp_t0"),
            _message_item("u1", role="user", text="new message", created_at=10),
        ]
        # Window of 1 -> only the new message; the whole earlier tool group
        # (call + output + its message) is dropped together, never split.
        refs, _ = select_history_refs(
            items,
            new_item_id="u1",
            max_messages=1,
            max_input_tokens=24_000,
            model="claude-haiku-4-5",
        )
        assert refs == ["u1"]

    def test_budget_drops_whole_message_groups_oldest_first(self) -> None:
        # Each message is heavy (50 words, ~70 tokens); a budget that fits the
        # anchor alone but not all 20 keeps only a recent handful.
        items = _history(20, word_count=50)
        refs, over = select_history_refs(
            items,
            new_item_id="item_19",
            max_messages=20,
            max_input_tokens=200,
            model="claude-haiku-4-5",
        )
        assert refs[-1] == "item_19"
        assert len(refs) < 20
        kept_indices = [int(r.split("_")[1]) for r in refs]
        assert kept_indices == list(range(kept_indices[0], 20))
        assert over is False

    def test_new_message_group_always_included_even_alone_over_budget(self) -> None:
        items = _history(5, word_count=5000)
        refs, over = select_history_refs(
            items,
            new_item_id="item_4",
            max_messages=5,
            max_input_tokens=1,
            model="claude-haiku-4-5",
        )
        assert refs == ["item_4"]
        assert over is True

    def test_non_positive_max_messages_is_clamped_to_one(self) -> None:
        items = _history(4)
        refs, _ = select_history_refs(
            items,
            new_item_id="item_3",
            max_messages=0,
            max_input_tokens=24_000,
            model="claude-haiku-4-5",
        )
        assert refs == ["item_3"]

    def test_missing_anchor_selects_nothing(self) -> None:
        items = _history(3)
        refs, over = select_history_refs(
            items,
            new_item_id="item_does_not_exist",
            max_messages=20,
            max_input_tokens=24_000,
            model="x",
        )
        assert refs == []
        assert over is False


class TestAssembleDefaultPolicy:
    def test_system_text_comes_from_agent_instructions(self) -> None:
        request = _request(new_item_id="item_0", item_count=1)
        response = assemble(
            request, items_provider=lambda: _history(1), agent_instructions="Be terse."
        )
        assert response.system.mode == "append"
        assert response.system.text == "Be terse."
        assert response.system.digest.startswith("sha256:")

    def test_missing_instructions_yields_empty_system_text(self) -> None:
        request = _request(new_item_id="item_0")
        response = assemble(request, items_provider=lambda: _history(1), agent_instructions=None)
        assert response.system.text == ""

    def test_memory_is_empty_by_default(self) -> None:
        request = _request(new_item_id="item_0")
        response = assemble(request, items_provider=lambda: _history(1), agent_instructions="x")
        assert response.memory.items == []
        assert response.audit.memory_items == 0

    def test_history_defaults_to_server_default_window(self) -> None:
        items = _history(6)
        request = _request(new_item_id="item_5")
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
        # DEFAULT_MAX_MESSAGES (20) comfortably covers 6 messages.
        assert DEFAULT_MAX_MESSAGES > 6
        assert [ref.ref for ref in response.history.items] == [f"item_{i}" for i in range(6)]
        assert response.history.summary is None
        assert response.audit.history_items == 6
        assert response.audit.estimated_tokens > 0

    def test_response_is_deterministic_for_the_same_input(self) -> None:
        items = _history(6)
        request = _request(new_item_id="item_5")
        first = assemble(request, items_provider=lambda: items, agent_instructions="x")
        second = assemble(request, items_provider=lambda: items, agent_instructions="x")
        assert first.model_dump() == second.model_dump()


class TestTestHookLabels:
    def test_max_messages_one_blinds_the_turn(self) -> None:
        items = _history(6)
        request = _request(new_item_id="item_5", labels={MAX_MESSAGES_LABEL: "1"})
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
        assert [ref.ref for ref in response.history.items] == ["item_5"]

    def test_max_messages_one_never_calls_items_provider(self) -> None:
        # A 1-message window's anchor is the request itself — fetching the
        # full record would be wasted I/O the contract doesn't require.
        request = _request(new_item_id="item_5", labels={MAX_MESSAGES_LABEL: "1"})

        def _boom() -> list[dict[str, Any]]:
            raise AssertionError("items_provider should not be called for max_messages=1")

        response = assemble(request, items_provider=_boom, agent_instructions="x")
        assert [ref.ref for ref in response.history.items] == ["item_5"]

    def test_max_messages_three_quotes_an_injected_earlier_message(self) -> None:
        # The E2E "injected" shape: codeword message, its reply, new message.
        items = [
            _message_item("codeword", role="user", text="my codeword is PAPAYA-42", created_at=0),
            _message_item("reply", role="assistant", text="Got it.", created_at=1),
            _message_item(
                "ask", role="user", text="what was the last message I sent?", created_at=2
            ),
        ]
        request = _request(new_item_id="ask", labels={MAX_MESSAGES_LABEL: "3"})
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
        assert [ref.ref for ref in response.history.items] == ["codeword", "reply", "ask"]

    def test_invalid_max_messages_label_falls_back_to_the_default(self) -> None:
        items = _history(6)
        request = _request(new_item_id="item_5", labels={MAX_MESSAGES_LABEL: "not-a-number"})
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
        assert len(response.history.items) == 6

    def test_memory_fixture_label_injects_one_fact(self) -> None:
        request = _request(
            new_item_id="item_0",
            labels={
                MAX_MESSAGES_LABEL: "1",
                MEMORY_FIXTURE_LABEL: "The user's codeword is MANGO-7",
            },
        )
        response = assemble(request, items_provider=list, agent_instructions="x")
        assert len(response.memory.items) == 1
        item = response.memory.items[0]
        assert item.kind == "fact"
        assert item.text == "The user's codeword is MANGO-7"
        assert response.audit.memory_items == 1

    def test_blindfold_label_alone_does_not_change_the_default_policy(self) -> None:
        # omnigent.blindfold selects the CLI lifecycle, not the assembly
        # policy — a blindfolded session with no test-hook labels still gets
        # the ordinary server-default window.
        items = _history(4)
        request = _request(new_item_id="item_3", labels={BLINDFOLD_LABEL: "true"})
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
        assert len(response.history.items) == 4


class TestRenderSystemText:
    def test_no_memory_returns_bare_system_text(self) -> None:
        request = _request(new_item_id="item_0")
        response = assemble(
            request, items_provider=lambda: _history(1), agent_instructions="Rules."
        )
        assert render_system_text(response) == "Rules."

    def test_memory_is_appended_as_a_tagged_block(self) -> None:
        request = _request(
            new_item_id="item_0",
            labels={MAX_MESSAGES_LABEL: "1", MEMORY_FIXTURE_LABEL: "Prefers short answers."},
        )
        response = assemble(request, items_provider=list, agent_instructions="Rules.")
        rendered = render_system_text(response)
        assert rendered.startswith("Rules.\n\n<long_term_memory>")
        assert "- (fact) Prefers short answers." in rendered
        assert rendered.endswith("</long_term_memory>")

    def test_memory_block_stands_alone_with_no_system_text(self) -> None:
        request = _request(
            new_item_id="item_0",
            labels={MAX_MESSAGES_LABEL: "1", MEMORY_FIXTURE_LABEL: "fact"},
        )
        response = assemble(request, items_provider=list, agent_instructions=None)
        rendered = render_system_text(response)
        assert rendered.startswith("<long_term_memory>")


class TestFailClosed:
    def test_items_provider_error_falls_back_to_new_message_only(self) -> None:
        request = _request(new_item_id="item_5")

        def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("record store is down")

        response = assemble_or_fail_closed(
            request, items_provider=_boom, agent_instructions="Rules."
        )
        assert [ref.ref for ref in response.history.items] == ["item_5"]
        assert response.memory.items == []
        assert response.system.text == "Rules."
        assert response.audit.fallback is True

    def test_success_path_does_not_set_the_fallback_flag(self) -> None:
        request = _request(new_item_id="item_0")
        response = assemble_or_fail_closed(
            request, items_provider=lambda: _history(1), agent_instructions="x"
        )
        assert response.audit.fallback is False

    def test_never_falls_back_to_all_history(self) -> None:
        # The one fallback behavior the contract explicitly forbids (§7).
        request = _request(new_item_id="item_9")

        def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        response = assemble_or_fail_closed(request, items_provider=_boom, agent_instructions=None)
        assert len(response.history.items) == 1


class TestObserve:
    def test_observe_accepts_a_well_formed_request_and_returns_none(self) -> None:
        request = ObserveRequest(
            turn_id="turn_1",
            session_id="conv_1",
            outcome="completed",
            new_item_ids=["item_9", "item_10"],
        )
        assert observe(request) is None

    @pytest.mark.parametrize("outcome", ["completed", "failed", "interrupted"])
    def test_observe_accepts_every_documented_outcome(self, outcome: str) -> None:
        request = ObserveRequest(turn_id="turn_1", session_id="conv_1", outcome=outcome)
        assert observe(request) is None
