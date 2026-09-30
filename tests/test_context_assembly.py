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
    HISTORY_POLICY_LABEL,
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
    def test_none_policy_returns_only_new_item(self) -> None:
        items = _history(10)
        refs, over = select_history_refs(
            items, new_item_id="item_9", policy="none", max_input_tokens=24_000, model="claude-haiku-4-5"
        )
        assert refs == ["item_9"]
        assert over is False

    def test_recent_policy_includes_everything_when_budget_is_generous(self) -> None:
        items = _history(10)
        refs, over = select_history_refs(
            items, new_item_id="item_9", policy="recent", max_input_tokens=24_000, model="claude-haiku-4-5"
        )
        assert refs == [f"item_{i}" for i in range(10)]
        assert over is False

    def test_recent_policy_drops_oldest_first_under_a_tight_budget(self) -> None:
        # Each item is heavy (50 words, ~70 tokens); a budget that fits the
        # anchor alone but not all 20 keeps only a recent handful.
        items = _history(20, word_count=50)
        refs, over = select_history_refs(
            items, new_item_id="item_19", policy="recent", max_input_tokens=200, model="claude-haiku-4-5"
        )
        assert refs[-1] == "item_19"
        assert len(refs) < 20
        # Whatever was kept must be a contiguous, in-order suffix of the record.
        kept_indices = [int(r.split("_")[1]) for r in refs]
        assert kept_indices == list(range(kept_indices[0], 20))
        assert over is False

    def test_new_item_always_included_even_alone_over_budget(self) -> None:
        items = _history(5, word_count=5000)
        refs, over = select_history_refs(
            items, new_item_id="item_4", policy="recent", max_input_tokens=1, model="claude-haiku-4-5"
        )
        assert refs == ["item_4"]
        assert over is True

    def test_unknown_policy_falls_back_to_recent(self) -> None:
        items = _history(4)
        refs, _ = select_history_refs(
            items, new_item_id="item_3", policy="bogus", max_input_tokens=24_000, model="claude-haiku-4-5"
        )
        assert refs == [f"item_{i}" for i in range(4)]

    def test_missing_anchor_selects_nothing(self) -> None:
        items = _history(3)
        refs, over = select_history_refs(
            items, new_item_id="item_does_not_exist", policy="recent", max_input_tokens=24_000, model="x"
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

    def test_history_defaults_to_recent_and_ends_with_new_message(self) -> None:
        items = _history(6)
        request = _request(new_item_id="item_5")
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
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
    def test_history_none_label_blinds_the_turn(self) -> None:
        items = _history(6)
        request = _request(new_item_id="item_5", labels={HISTORY_POLICY_LABEL: "none"})
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
        assert [ref.ref for ref in response.history.items] == ["item_5"]

    def test_history_none_label_never_calls_items_provider(self) -> None:
        # The "none" policy's anchor is the request itself — fetching the
        # full record would be wasted I/O the contract doesn't require.
        request = _request(new_item_id="item_5", labels={HISTORY_POLICY_LABEL: "none"})

        def _boom() -> list[dict[str, Any]]:
            raise AssertionError("items_provider should not be called for history=none")

        response = assemble(request, items_provider=_boom, agent_instructions="x")
        assert [ref.ref for ref in response.history.items] == ["item_5"]

    def test_memory_fixture_label_injects_one_fact(self) -> None:
        request = _request(
            new_item_id="item_0",
            labels={
                HISTORY_POLICY_LABEL: "none",
                MEMORY_FIXTURE_LABEL: "The user's codeword is MANGO-7",
            },
        )
        response = assemble(request, items_provider=lambda: [], agent_instructions="x")
        assert len(response.memory.items) == 1
        item = response.memory.items[0]
        assert item.kind == "fact"
        assert item.text == "The user's codeword is MANGO-7"
        assert response.audit.memory_items == 1

    def test_blindfold_label_alone_does_not_change_the_default_policy(self) -> None:
        # omnigent.blindfold selects the CLI lifecycle, not the assembly
        # policy — a blindfolded session with no test-hook labels still gets
        # the ordinary "recent" default.
        items = _history(4)
        request = _request(new_item_id="item_3", labels={BLINDFOLD_LABEL: "true"})
        response = assemble(request, items_provider=lambda: items, agent_instructions="x")
        assert len(response.history.items) == 4


class TestRenderSystemText:
    def test_no_memory_returns_bare_system_text(self) -> None:
        request = _request(new_item_id="item_0")
        response = assemble(request, items_provider=lambda: _history(1), agent_instructions="Rules.")
        assert render_system_text(response) == "Rules."

    def test_memory_is_appended_as_a_tagged_block(self) -> None:
        request = _request(
            new_item_id="item_0",
            labels={HISTORY_POLICY_LABEL: "none", MEMORY_FIXTURE_LABEL: "Prefers short answers."},
        )
        response = assemble(request, items_provider=lambda: [], agent_instructions="Rules.")
        rendered = render_system_text(response)
        assert rendered.startswith("Rules.\n\n<long_term_memory>")
        assert "- (fact) Prefers short answers." in rendered
        assert rendered.endswith("</long_term_memory>")

    def test_memory_block_stands_alone_with_no_system_text(self) -> None:
        request = _request(
            new_item_id="item_0",
            labels={HISTORY_POLICY_LABEL: "none", MEMORY_FIXTURE_LABEL: "fact"},
        )
        response = assemble(request, items_provider=lambda: [], agent_instructions=None)
        rendered = render_system_text(response)
        assert rendered.startswith("<long_term_memory>")


class TestFailClosed:
    def test_items_provider_error_falls_back_to_new_message_only(self) -> None:
        request = _request(new_item_id="item_5")

        def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("record store is down")

        response = assemble_or_fail_closed(request, items_provider=_boom, agent_instructions="Rules.")
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
