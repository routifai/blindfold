"""Unit tests for omnigent.superchat.activity's pure derivation logic.

These build Conversation/ConversationItem objects directly (no store, no
DB) so they stay fast and laptop-friendly; store-backed orchestration
(list_chat_family / list_activities / get_activity, owner scoping) is
covered in test_activity_store.py.
"""

from __future__ import annotations

import json

from omnigent.entities import (
    Conversation,
    ConversationItem,
    ErrorData,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
)
from omnigent.superchat.activity import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    _activities_for_conversation,
    _sub_agent_status,
    _turn_status,
    is_superside_chat,
    step_title_for_call,
)


def _conv(
    conv_id: str = "conv_1",
    *,
    kind: str = "default",
    title: str | None = None,
    labels: dict[str, str] | None = None,
    live_status: str | None = None,
    sub_agent_name: str | None = None,
) -> Conversation:
    return Conversation(
        id=conv_id,
        created_at=1_000,
        updated_at=1_000,
        root_conversation_id=conv_id,
        kind=kind,
        title=title,
        labels=labels or {},
        live_status=live_status,
        sub_agent_name=sub_agent_name,
    )


def _msg(
    item_id: str,
    role: str,
    text: str,
    *,
    response_id: str = "resp_1",
    created_at: int = 1,
    agent: str | None = None,
    interrupted: bool = False,
) -> ConversationItem:
    content = [{"type": "input_text" if role == "user" else "output_text", "text": text}]
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id=response_id,
        created_at=created_at,
        data=MessageData(
            role=role,  # type: ignore[arg-type]
            content=content,
            agent=agent or ("assistant-agent" if role == "assistant" else None),
            interrupted=interrupted,
        ),
    )


def _call(
    item_id: str,
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str = "call_1",
    response_id: str = "resp_1",
    created_at: int = 2,
) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="function_call",
        status="completed",
        response_id=response_id,
        created_at=created_at,
        data=FunctionCallData(
            agent="assistant-agent", name=name, arguments=json.dumps(arguments), call_id=call_id
        ),
    )


def _call_output(
    item_id: str,
    output: str,
    *,
    call_id: str = "call_1",
    response_id: str = "resp_1",
    created_at: int = 3,
) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="function_call_output",
        status="completed",
        response_id=response_id,
        created_at=created_at,
        data=FunctionCallOutputData(call_id=call_id, output=output),
    )


def _error(
    item_id: str,
    message: str,
    *,
    response_id: str = "resp_1",
    created_at: int = 4,
) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="error",
        status="completed",
        response_id=response_id,
        created_at=created_at,
        data=ErrorData(source="tool", code="boom", message=message),
    )


class _FakeItemsStore:
    """Minimal ConversationStore stand-in: list_items only."""

    def __init__(self, items: list[ConversationItem]) -> None:
        self._items = items

    def list_items(self, conversation_id: str, limit: int = 1000, order: str = "asc"):
        class _Page:
            def __init__(self, data: list[ConversationItem]) -> None:
                self.data = data

        return _Page(self._items)


# ── is_superside_chat ────────────────────────────────────────────────────


def test_is_superside_chat_true_only_for_exact_value() -> None:
    assert is_superside_chat({"omnigent.context.mode": "superside-chat"}) is True
    assert is_superside_chat({"omnigent.context.mode": "rollover"}) is False
    assert is_superside_chat({}) is False
    assert is_superside_chat(None) is False


# ── Turn with tools -> Activity; turn without tools -> no Activity ──────


def test_turn_with_tool_call_becomes_one_activity_with_a_step() -> None:
    conv = _conv(title="Side chat mechanics")
    items = [
        _msg("u1", "user", "research side chat mechanics", created_at=1),
        _call("fc1", "memory_search", {"query": "side chat"}, created_at=2),
        _call_output("fo1", "found 3 notes", created_at=3),
        _msg("a1", "assistant", "Researched side chat mechanics with quotes", created_at=4),
    ]
    activities = _activities_for_conversation(
        _FakeItemsStore(items), conv, include_step_detail=False
    )
    assert len(activities) == 1
    activity = activities[0]
    assert activity.kind == "turn"
    assert activity.chat_id == "conv_1"
    assert activity.title == "research side chat mechanics", "titled by the user's request"
    assert activity.outcome == "Researched side chat mechanics with quotes"
    assert activity.status == STATUS_DONE
    assert activity.started_at == 1
    assert activity.finished_at == 4
    assert len(activity.steps) == 1
    assert activity.steps[0].title == "Searched memory for 'side chat'"
    assert activity.steps[0].detail is None  # not requested


def test_turn_without_tool_call_is_not_an_activity() -> None:
    conv = _conv()
    items = [
        _msg("u1", "user", "hi", created_at=1),
        _msg("a1", "assistant", "hello", created_at=2),
    ]
    activities = _activities_for_conversation(
        _FakeItemsStore(items), conv, include_step_detail=False
    )
    assert activities == []


def test_turn_step_detail_included_only_when_requested() -> None:
    conv = _conv()
    items = [
        _call("fc1", "memory_search", {"query": "q"}, created_at=1),
        _call_output("fo1", "result text", created_at=2),
    ]
    activities = _activities_for_conversation(
        _FakeItemsStore(items), conv, include_step_detail=True
    )
    [activity] = activities
    [step] = activity.steps
    assert step.detail is not None
    assert step.detail["call"]["name"] == "memory_search"
    assert step.detail["result"]["content"] == "result text"


# ── Sub-agent conversation -> one Activity ──────────────────────────────


def test_sub_agent_conversation_becomes_one_activity() -> None:
    conv = _conv("conv_child", kind="sub_agent", title="researcher:auth-flow", live_status="idle")
    items = [
        _call("fc1", "memory_search", {"query": "auth"}, created_at=5),
        _call_output("fo1", "ok", created_at=6),
        _msg("a1", "assistant", "Found the auth flow docs", created_at=7),
    ]
    [activity] = _activities_for_conversation(
        _FakeItemsStore(items), conv, include_step_detail=False
    )
    assert activity.kind == "sub_agent"
    assert activity.id == "sub_agent:conv_child"
    assert activity.title == "researcher: auth-flow"
    assert activity.status == STATUS_DONE
    assert activity.outcome == "Found the auth flow docs"
    assert activity.started_at == 5
    assert activity.finished_at == 7


# ── Step title templates ─────────────────────────────────────────────────


def test_step_title_memory_search_with_query() -> None:
    assert step_title_for_call("memory_search", json.dumps({"query": "deadline"})) == (
        "Searched memory for 'deadline'"
    )


def test_step_title_session_history_read() -> None:
    assert step_title_for_call("session_history", json.dumps({"action": "read"})) == (
        "Read earlier messages"
    )


def test_step_title_sys_session_create_with_title() -> None:
    assert step_title_for_call("sys_session_create", json.dumps({"title": "auth-flow"})) == (
        "Launched a sub-agent 'auth-flow'"
    )


def test_step_title_fallback_for_unknown_tool() -> None:
    assert step_title_for_call("web_search", "{}") == "Used web_search"


def test_step_title_handles_malformed_arguments() -> None:
    assert step_title_for_call("memory_search", "not json") == "Searched memory"


# ── Status mapping ────────────────────────────────────────────────────────


def test_turn_status_error_item_is_failed() -> None:
    conv = _conv(live_status="idle")
    group = [_call("fc1", "t", {}), _error("e1", "boom")]
    assert _turn_status(conv, group, is_latest_group=True) == STATUS_FAILED


def test_turn_status_interrupted_message_is_cancelled() -> None:
    conv = _conv(live_status="idle")
    group = [
        _call("fc1", "t", {}),
        _msg("a1", "assistant", "partial", interrupted=True),
    ]
    assert _turn_status(conv, group, is_latest_group=True) == STATUS_CANCELLED


def test_turn_status_latest_group_running_is_in_progress() -> None:
    conv = _conv(live_status="running")
    group = [_call("fc1", "t", {})]
    assert _turn_status(conv, group, is_latest_group=True) == STATUS_IN_PROGRESS


def test_turn_status_older_group_ignores_live_status() -> None:
    conv = _conv(live_status="running")
    group = [_call("fc1", "t", {}), _msg("a1", "assistant", "done")]
    assert _turn_status(conv, group, is_latest_group=False) == STATUS_DONE


def test_sub_agent_status_failed() -> None:
    conv = _conv(kind="sub_agent", live_status="failed")
    assert _sub_agent_status(conv) == STATUS_FAILED


def test_sub_agent_status_in_progress() -> None:
    conv = _conv(kind="sub_agent", live_status="running")
    assert _sub_agent_status(conv) == STATUS_IN_PROGRESS


def test_sub_agent_status_cancelled_when_closed_before_any_turn() -> None:
    conv = _conv(
        kind="sub_agent",
        live_status=None,
        labels={"omnigent.closed": "true"},
    )
    assert _sub_agent_status(conv) == STATUS_CANCELLED


def test_sub_agent_status_done_when_closed_after_finishing() -> None:
    conv = _conv(
        kind="sub_agent",
        live_status="idle",
        labels={"omnigent.closed": "true"},
    )
    assert _sub_agent_status(conv) == STATUS_DONE


def test_sub_agent_status_done_when_idle_and_not_closed() -> None:
    conv = _conv(kind="sub_agent", live_status="idle")
    assert _sub_agent_status(conv) == STATUS_DONE


def test_system_started_turn_is_titled_by_the_chat() -> None:
    conv = _conv(title="Side chat mechanics")
    items = [
        _msg("u1", "user", "[System: sub-agent researcher/x finished (completed)]", created_at=1),
        _call("fc1", "sys_read_inbox", {}, created_at=2),
        _call_output("fo1", "delivered", created_at=3),
        _msg("a1", "assistant", "Here is the result", created_at=4),
    ]
    activities = _activities_for_conversation(
        _FakeItemsStore(items), conv, include_step_detail=False
    )
    assert activities[0].title == "Side chat mechanics"


def test_turn_titled_by_request_stored_outside_its_response_group() -> None:
    conv = _conv(title="Main chat")
    items = [
        _msg("u1", "user", "check the Q4 totals", created_at=1, response_id="resp_user"),
        _call("fc1", "memory_search", {"query": "q4"}, created_at=2, response_id="resp_2"),
        _call_output("fo1", "found", created_at=3, response_id="resp_2"),
        _msg("a1", "assistant", "Totals match", created_at=4, response_id="resp_2"),
    ]
    activities = _activities_for_conversation(
        _FakeItemsStore(items), conv, include_step_detail=False
    )
    assert activities[0].title == "check the Q4 totals"
