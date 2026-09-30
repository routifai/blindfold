"""Unit tests for :mod:`omnigent.tools.builtins.session_history`.

Builds a real :class:`SqlAlchemyConversationStore` over a temp SQLite DB
(same pattern as ``test_sys_session.py``) so the FTS-backed ``search``
action and the item-store-backed ``read`` action run against real store
behavior, not mocks.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from omnigent.context.labels import CONTEXT_MODE_LABEL
from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.session_history import (
    SessionHistoryTool,
    group_into_turns,
    status_from_labels,
)


@dataclass
class _Fixture:
    """Bundle of store + two independent sessions + a ctx scoped to the first."""

    conv_store: SqlAlchemyConversationStore
    conv_id: str
    other_conv_id: str
    ctx: ToolContext


def _user_msg(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp_1",
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _assistant_msg(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp_1",
        data=MessageData(
            role="assistant", content=[{"type": "output_text", "text": text}], agent="test-agent"
        ),
    )


@pytest.fixture()
def session_fixture(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Fixture]:
    """Two independent top-level conversations; ``ctx`` is scoped to the first."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(kind="default")
    other_conv = conv_store.create_conversation(kind="default")

    conv_store.append(other_conv.id, [_user_msg("this belongs to the OTHER session")])

    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: conv_store)

    ctx = ToolContext(task_id="task_test", agent_id="agent_test", conversation_id=conv.id)
    yield _Fixture(conv_store=conv_store, conv_id=conv.id, other_conv_id=other_conv.id, ctx=ctx)


# ── Scoping: id comes from context only ─────────────────────


def test_read_ignores_conversation_id_in_arguments(session_fixture: _Fixture) -> None:
    """A ``conversation_id`` smuggled into arguments never redirects the read."""
    session_fixture.conv_store.append(
        session_fixture.conv_id, [_user_msg("my own session's first message")]
    )
    tool = SessionHistoryTool()
    result = json.loads(
        tool.invoke(
            json.dumps({"action": "read", "conversation_id": session_fixture.other_conv_id}),
            session_fixture.ctx,
        )
    )
    texts = [m["content"] for turn in result["turns"] for m in turn["messages"]]
    assert any("my own session's first message" in t for t in texts)
    assert not any("OTHER session" in t for t in texts)


def test_search_is_scoped_to_calling_session(session_fixture: _Fixture) -> None:
    """search() never returns hits from a different conversation."""
    session_fixture.conv_store.append(
        session_fixture.conv_id, [_user_msg("uniquemarker in own session")]
    )
    session_fixture.conv_store.append(
        session_fixture.other_conv_id, [_user_msg("uniquemarker in other session")]
    )
    tool = SessionHistoryTool()
    result = json.loads(
        tool.invoke(json.dumps({"action": "search", "query": "uniquemarker"}), session_fixture.ctx)
    )
    assert len(result["results"]) == 1
    assert "own session" in result["results"][0]["content"]


def test_invoke_without_conversation_id_in_context_errors() -> None:
    """No ``ctx.conversation_id`` at all — refuse rather than guess a session."""
    tool = SessionHistoryTool()
    ctx = ToolContext(task_id="task_test", agent_id="agent_test", conversation_id=None)
    result = json.loads(tool.invoke(json.dumps({"action": "read"}), ctx))
    assert "error" in result


# ── read: turns, paging, limits ─────────────────────────────


def test_read_groups_into_full_turns_newest_first(session_fixture: _Fixture) -> None:
    """Each turn is a user message plus its reply; turns come back newest first."""
    session_fixture.conv_store.append(
        session_fixture.conv_id,
        [
            _user_msg("first question"),
            _assistant_msg("first answer"),
            _user_msg("second question"),
            _assistant_msg("second answer"),
        ],
    )
    tool = SessionHistoryTool()
    result = json.loads(
        tool.invoke(json.dumps({"action": "read", "limit": 5}), session_fixture.ctx)
    )
    assert len(result["turns"]) == 2
    newest, oldest = result["turns"]
    assert [m["content"] for m in newest["messages"]] == ["second question", "second answer"]
    assert [m["content"] for m in oldest["messages"]] == ["first question", "first answer"]
    assert result["next_cursor"] is None


def test_read_pages_backward_with_cursor(session_fixture: _Fixture) -> None:
    """limit=1 pages one turn at a time; next_cursor resumes at the next-older turn."""
    session_fixture.conv_store.append(
        session_fixture.conv_id,
        [
            _user_msg("q1"),
            _assistant_msg("a1"),
            _user_msg("q2"),
            _assistant_msg("a2"),
        ],
    )
    tool = SessionHistoryTool()
    page1 = json.loads(
        tool.invoke(json.dumps({"action": "read", "limit": 1}), session_fixture.ctx)
    )
    assert [m["content"] for m in page1["turns"][0]["messages"]] == ["q2", "a2"]
    assert page1["next_cursor"] is not None

    page2 = json.loads(
        tool.invoke(
            json.dumps({"action": "read", "limit": 1, "cursor": page1["next_cursor"]}),
            session_fixture.ctx,
        )
    )
    assert [m["content"] for m in page2["turns"][0]["messages"]] == ["q1", "a1"]
    assert page2["next_cursor"] is None


def test_read_tool_call_rides_along_with_its_turn(session_fixture: _Fixture) -> None:
    """A tool call/result between a user message and the reply stays in that turn."""
    session_fixture.conv_store.append(
        session_fixture.conv_id,
        [
            _user_msg("run a search"),
            NewConversationItem(
                type="function_call",
                response_id="resp_1",
                data=FunctionCallData(
                    agent="test-agent", name="web_search", arguments="{}", call_id="call_1"
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id="resp_1",
                data=FunctionCallOutputData(call_id="call_1", output="results"),
            ),
            _assistant_msg("here is what I found"),
        ],
    )
    tool = SessionHistoryTool()
    result = json.loads(tool.invoke(json.dumps({"action": "read"}), session_fixture.ctx))
    assert len(result["turns"]) == 1
    kinds = [m["type"] for m in result["turns"][0]["messages"]]
    assert kinds == ["text", "tool_call", "tool_result", "text"]


def test_read_default_and_max_limit() -> None:
    """Default limit is 5 turns; an oversized limit is clamped to 20."""
    schema = SessionHistoryTool().get_schema()
    description = schema["function"]["parameters"]["properties"]["limit"]["description"]
    assert "default 5" in description
    assert "max 20" in description


@pytest.mark.parametrize("limit", [0, -1, "3", True])
def test_read_rejects_invalid_limit(session_fixture: _Fixture, limit: object) -> None:
    tool = SessionHistoryTool()
    result = json.loads(
        tool.invoke(json.dumps({"action": "read", "limit": limit}), session_fixture.ctx)
    )
    assert "error" in result


def test_read_empty_session_returns_no_turns(session_fixture: _Fixture) -> None:
    tool = SessionHistoryTool()
    result = json.loads(tool.invoke(json.dumps({"action": "read"}), session_fixture.ctx))
    assert result["turns"] == []
    assert result["next_cursor"] is None


# ── group_into_turns: paging algorithm ──────────────────────


def test_group_into_turns_caps_at_limit_and_resumes() -> None:
    """Unit-level check of the shared pagination algorithm (no store involved)."""
    chrono = [
        {"id": "1", "type": "text", "role": "user"},
        {"id": "2", "type": "text", "role": "assistant"},
        {"id": "3", "type": "text", "role": "user"},
        {"id": "4", "type": "text", "role": "assistant"},
        {"id": "5", "type": "text", "role": "user"},
        {"id": "6", "type": "text", "role": "assistant"},
    ]
    desc = list(reversed(chrono))

    def fetch_page(before: str | None) -> tuple[list[dict], bool]:
        start = (
            0 if before is None else next(i for i, it in enumerate(desc) if it["id"] == before) + 1
        )
        batch = desc[start : start + 2]
        return batch, (start + 2) < len(desc)

    turns, next_cursor = group_into_turns(fetch_page, limit=2)
    assert [t[0]["id"] for t in turns] == ["5", "3"]
    assert next_cursor == "3"

    turns2, next_cursor2 = group_into_turns(fetch_page, limit=2, start_cursor=next_cursor)
    assert [t[0]["id"] for t in turns2] == ["1"]
    assert next_cursor2 is None


# ── search: limits ───────────────────────────────────────────


def test_search_requires_query(session_fixture: _Fixture) -> None:
    tool = SessionHistoryTool()
    result = json.loads(tool.invoke(json.dumps({"action": "search"}), session_fixture.ctx))
    assert "error" in result


def test_search_caps_limit(session_fixture: _Fixture) -> None:
    tool = SessionHistoryTool()
    result = json.loads(
        tool.invoke(
            json.dumps({"action": "search", "query": "x", "limit": 1000}), session_fixture.ctx
        )
    )
    assert "error" not in result


# ── status: fallback + computed fields ───────────────────────


def test_status_from_labels_omits_unknown_fields() -> None:
    """No usage labels at all -> only the (documented-default) trigger is reported."""
    result = status_from_labels({})
    assert result == {"rollover_trigger_tokens": 90_000}


def test_status_from_labels_computes_headroom_and_percent() -> None:
    result = status_from_labels(
        {
            "omnigent.last_context_tokens": "5000",
            "omnigent.last_context_window": "10000",
            "omnigent.context.rollover_at_tokens": "8000",
        }
    )
    assert result["current_context_tokens"] == 5000
    assert result["context_window_tokens"] == 10000
    assert result["rollover_trigger_tokens"] == 8000
    assert result["tokens_remaining_to_rollover"] == 3000
    assert result["context_used_percent"] == 50.0
    assert result["source"] == "reported"


def test_status_action_reads_session_labels(session_fixture: _Fixture) -> None:
    session_fixture.conv_store.set_labels(
        session_fixture.conv_id, {"omnigent.last_context_tokens": "42"}
    )
    tool = SessionHistoryTool()
    result = json.loads(tool.invoke(json.dumps({"action": "status"}), session_fixture.ctx))
    assert result["current_context_tokens"] == 42


# ── Schema shape ─────────────────────────────────────────────


def test_schema_shape() -> None:
    schema = SessionHistoryTool().get_schema()
    func = schema["function"]
    assert func["name"] == "session_history"
    props = func["parameters"]["properties"]
    assert set(props) == {"action", "cursor", "limit", "query"}
    assert props["action"]["enum"] == ["read", "search", "status"]
    assert func["parameters"]["required"] == ["action"]


def test_invoke_rejects_unknown_action(session_fixture: _Fixture) -> None:
    tool = SessionHistoryTool()
    result = json.loads(tool.invoke(json.dumps({"action": "delete"}), session_fixture.ctx))
    assert "error" in result


def test_invoke_rejects_malformed_arguments(session_fixture: _Fixture) -> None:
    tool = SessionHistoryTool()
    result = json.loads(tool.invoke("{", session_fixture.ctx))
    assert "error" in result


# ── CONTEXT_MODE_LABEL sanity ────────────────────────────────


def test_context_mode_label_constant() -> None:
    assert CONTEXT_MODE_LABEL == "omnigent.context.mode"
