"""Store-backed orchestration tests for omnigent.superchat.activity.

Covers list_chat_family / list_activities / get_activity against a real
SqlAlchemyConversationStore (sqlite, per-test file) — the wiring between
the Super Chat, its Side Chats, and their Sub-agents, plus owner/family
scoping. Pure derivation logic is covered in test_activity.py.
"""

from __future__ import annotations

import json

from omnigent.entities import FunctionCallData, MessageData, NewConversationItem
from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY, SIDE_CHAT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.activity import (
    STATUS_DONE,
    get_activity,
    list_activities,
    list_chat_family,
    resolve_super_chat_id,
)

_MODE_LABEL = "omnigent.context.mode"
_MODE_VALUE = "superside-chat"


def _superside_chat_labels(**extra: str) -> dict[str, str]:
    return {_MODE_LABEL: _MODE_VALUE, **extra}


def _add_turn_with_tool_call(
    conv_store: SqlAlchemyConversationStore,
    conversation_id: str,
    *,
    response_id: str = "resp_1",
) -> None:
    conv_store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "research side chats"}]
                ),
            ),
            NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=FunctionCallData(
                    agent="brain",
                    name="memory_search",
                    arguments=json.dumps({"query": "side chat"}),
                    call_id="call_1",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="brain",
                    content=[{"type": "output_text", "text": "Researched side chats"}],
                ),
            ),
        ],
    )


def _build_super_chat_family(conv_store: SqlAlchemyConversationStore, *, suffix: str) -> dict:
    super_chat = conv_store.create_conversation(
        kind="default",
        title=f"Super Chat {suffix}",
        labels=_superside_chat_labels(),
    )
    side_chat = conv_store.create_conversation(
        kind="default",
        title=f"Side Chat {suffix}",
        labels=_superside_chat_labels(
            **{SIDE_CHAT_LABEL_KEY: "true", FORK_SOURCE_LABEL_KEY: super_chat.id}
        ),
    )
    sub_agent = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=super_chat.id,
        title=f"researcher:{suffix}",
        labels=_superside_chat_labels(),
    )
    _add_turn_with_tool_call(conv_store, super_chat.id)
    conv_store.append(
        sub_agent.id,
        [
            NewConversationItem(
                type="function_call",
                response_id="resp_sub",
                data=FunctionCallData(
                    agent="researcher",
                    name="memory_search",
                    arguments=json.dumps({"query": suffix}),
                    call_id="call_sub",
                ),
            ),
        ],
    )
    conv_store.set_session_live_status(sub_agent.id, "idle")
    return {"super_chat": super_chat, "side_chat": side_chat, "sub_agent": sub_agent}


def test_resolve_super_chat_id_from_side_chat(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    assert (
        resolve_super_chat_id(conversation_store, family["side_chat"].id)
        == family["super_chat"].id
    )
    assert (
        resolve_super_chat_id(conversation_store, family["super_chat"].id)
        == family["super_chat"].id
    )


def test_resolve_super_chat_id_rejects_sub_agent_id(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    assert resolve_super_chat_id(conversation_store, family["sub_agent"].id) is None


def test_list_chat_family_includes_super_chat_side_chat_and_sub_agent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    chats = list_chat_family(conversation_store, family["super_chat"].id)
    ids = {chat.id for chat in chats}
    assert ids == {family["super_chat"].id, family["side_chat"].id, family["sub_agent"].id}


def test_list_chat_family_empty_for_non_superside_chat_session(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    plain = conversation_store.create_conversation(kind="default", title="plain chat")
    assert list_chat_family(conversation_store, plain.id) == []


def test_list_activities_covers_turn_and_sub_agent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    activities = list_activities(conversation_store, family["super_chat"].id)
    kinds_by_chat = {(a.kind, a.chat_id) for a in activities}
    assert ("turn", family["super_chat"].id) in kinds_by_chat
    assert ("sub_agent", family["sub_agent"].id) in kinds_by_chat
    # List view never carries per-step detail — only the detail route does.
    assert all(step.detail is None for activity in activities for step in activity.steps)


def test_list_activities_from_side_chat_id_resolves_to_same_feed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    from_super = list_activities(conversation_store, family["super_chat"].id)
    from_side = list_activities(conversation_store, family["side_chat"].id)
    assert {a.id for a in from_super} == {a.id for a in from_side}


def test_get_activity_returns_full_step_detail(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    activity_id = f"sub_agent:{family['sub_agent'].id}"
    activity = get_activity(conversation_store, family["super_chat"].id, activity_id)
    assert activity is not None
    assert activity.status == STATUS_DONE
    [step] = activity.steps
    assert step.detail is not None
    assert step.detail["call"]["name"] == "memory_search"


def test_get_activity_unknown_id_returns_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    assert (
        get_activity(conversation_store, family["super_chat"].id, "sub_agent:conv_missing") is None
    )


# ── Owner / family scoping ───────────────────────────────────────────────


def test_list_activities_never_leaks_an_unrelated_super_chats_activities(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family_a = _build_super_chat_family(conversation_store, suffix="a")
    family_b = _build_super_chat_family(conversation_store, suffix="b")

    activities_a = list_activities(conversation_store, family_a["super_chat"].id)
    chat_ids_a = {a.chat_id for a in activities_a}
    assert family_b["super_chat"].id not in chat_ids_a
    assert family_b["side_chat"].id not in chat_ids_a
    assert family_b["sub_agent"].id not in chat_ids_a

    # And family B's sub-agent id is not resolvable through family A's feed.
    cross_activity_id = f"sub_agent:{family_b['sub_agent'].id}"
    assert get_activity(conversation_store, family_a["super_chat"].id, cross_activity_id) is None
