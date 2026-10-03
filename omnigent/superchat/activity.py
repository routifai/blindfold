"""Activity Feed derivation for ``superside-chat`` sessions.

An **Activity** is never stored: it is derived on read from existing
conversations and items — a chat turn (Super Chat / Side Chat) that
included at least one tool call, or a whole Sub-agent session. See
``rollover/CONTEXT.md`` (Activity, Activity Feed, Step) and
``rollover/SUPERSIDE-CHAT-PLAN.md`` (slice S5).

Reuses ``_project_activity_item`` from ``omnigent.tools.builtins.spawn``
for step/detail projection, and ``list_related_chats`` from
``omnigent.context.rollover`` for Side Chat discovery, rather than
re-implementing either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from omnigent.context.labels import is_superside_chat
from omnigent.entities import Conversation, ConversationItem
from omnigent.stores.conversation_store import ConversationStore
from omnigent.tools.builtins.spawn import _project_activity_item
from omnigent.util.session_lifecycle import is_session_closed, title_without_closed_marker

STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

KIND_TURN = "turn"
KIND_SUB_AGENT = "sub_agent"

# A feed covering more items than this per chat is silently truncated —
# generous enough for ordinary use while keeping one page-fetch's cost bounded.
_ITEMS_SCAN_LIMIT = 1000
_SIDE_CHATS_SCAN_LIMIT = 200

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_OUTCOME_MAX_CHARS = 140
_TITLE_MAX_CHARS = 80


@dataclass(frozen=True)
class Step:
    """One action within an Activity, in plain language.

    :param item_id: The underlying ``function_call`` item's id.
    :param title: Plain-language description, e.g. ``"Searched memory for
        'deadline'"``.
    :param created_at: Unix epoch timestamp of the call.
    :param tool: The tool name invoked, e.g. ``"memory_search"``.
    :param detail: The call's arguments and matching output, capped —
        populated only when the Activity is read in full (not on the
        list/feed view).
    """

    item_id: str
    title: str
    created_at: int
    tool: str | None = None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class Activity:
    """One unit of multi-step work: a chat turn or a Sub-agent session.

    :param id: Opaque id, e.g. ``"turn:conv_abc:resp_xyz"`` or
        ``"sub_agent:conv_child"``.
    :param kind: ``"turn"`` or ``"sub_agent"``.
    :param chat_id: The conversation this Activity happened in (the Super
        Chat / Side Chat for a turn; the sub-agent's own conversation for a
        sub-agent Activity).
    :param title: One-line title.
    :param outcome: One-line outcome, or ``None`` when nothing to report yet.
    :param status: One of :data:`STATUS_IN_PROGRESS`, :data:`STATUS_DONE`,
        :data:`STATUS_FAILED`, :data:`STATUS_CANCELLED`.
    :param started_at: Unix epoch timestamp of the first item.
    :param finished_at: Unix epoch timestamp of the last item, or ``None``
        while still in progress.
    :param steps: The Activity's Steps, chronological.
    """

    id: str
    kind: str
    chat_id: str
    title: str
    outcome: str | None
    status: str
    started_at: int
    finished_at: int | None
    steps: list[Step] = field(default_factory=list)


def step_title_for_call(tool_name: str, raw_arguments: str) -> str:
    """Deterministic, plain-language title for one tool call.

    A small set of named templates covers the tools called out in the
    plan; everything else falls back to ``"Used <tool>"``. No LLM call.

    :param tool_name: The invoked tool's name, e.g. ``"memory_search"``.
    :param raw_arguments: The call's JSON-encoded arguments string.
    :returns: A one-line, user-facing description.
    """
    args = _args_dict(raw_arguments)
    if tool_name == "memory_search":
        query = args.get("query") or args.get("q")
        return f"Searched memory for '{query}'" if query else "Searched memory"
    if tool_name == "session_history":
        action = args.get("action")
        if action == "read":
            return "Read earlier messages"
        if action == "list_chats":
            return "Listed side chats"
        return "Used session_history"
    if tool_name == "sys_session_create":
        title = args.get("title")
        return f"Launched a sub-agent '{title}'" if title else "Launched a sub-agent"
    if tool_name == "sys_session_send":
        title = args.get("title")
        return (
            f"Sent a message to sub-agent '{title}'" if title else "Sent a message to a sub-agent"
        )
    return f"Used {tool_name}"


def _args_dict(raw_arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_arguments)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _truncate(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


def _message_text(item: ConversationItem) -> str | None:
    projected = _project_activity_item(item, max_chars=_OUTCOME_MAX_CHARS * 4)
    text = projected.get("content")
    return text or None


def _first_user_text(items: list[ConversationItem]) -> str | None:
    for item in items:
        if item.type == "message" and getattr(item.data, "role", None) == "user":
            text = _message_text(item)
            if text:
                return text
    return None


def _last_assistant_text(items: list[ConversationItem]) -> str | None:
    for item in reversed(items):
        if item.type == "message" and getattr(item.data, "role", None) == "assistant":
            text = _message_text(item)
            if text:
                return text
    return None


def _first_error_message(items: list[ConversationItem]) -> str | None:
    for item in items:
        if item.type == "error":
            return item.data.message  # type: ignore[union-attr]
    return None


def _group_items_by_response(items: list[ConversationItem]) -> list[list[ConversationItem]]:
    """Group a chronological item list into per-turn (``response_id``) runs."""
    groups: dict[str, list[ConversationItem]] = {}
    order: list[str] = []
    for item in items:
        if item.response_id not in groups:
            groups[item.response_id] = []
            order.append(item.response_id)
        groups[item.response_id].append(item)
    return [groups[response_id] for response_id in order]


def _step_detail(
    call: ConversationItem,
    output: ConversationItem | None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"call": _project_activity_item(call)}
    if output is not None:
        detail["result"] = _project_activity_item(output)
    return detail


def _steps_for_calls(
    calls: list[ConversationItem],
    outputs_by_call_id: dict[str, ConversationItem],
    *,
    include_detail: bool,
) -> list[Step]:
    steps: list[Step] = []
    for call in calls:
        name = call.data.name  # type: ignore[union-attr]
        arguments = call.data.arguments  # type: ignore[union-attr]
        call_id = call.data.call_id  # type: ignore[union-attr]
        steps.append(
            Step(
                item_id=call.id,
                title=step_title_for_call(name, arguments),
                created_at=call.created_at,
                tool=name,
                detail=(
                    _step_detail(call, outputs_by_call_id.get(call_id)) if include_detail else None
                ),
            )
        )
    return steps


def _turn_status(
    conversation: Conversation,
    group: list[ConversationItem],
    *,
    is_latest_group: bool,
) -> str:
    """Map a turn's items (+ live session status) to an Activity status.

    An ``error`` item in the turn is Failed; an interrupted assistant
    message (the engine's own durable partial-response marker) is
    Cancelled; the latest turn still running/waiting on the live session
    is In Progress; everything else is Done.
    """
    if any(item.type == "error" for item in group):
        return STATUS_FAILED
    if any(item.type == "message" and getattr(item.data, "interrupted", False) for item in group):
        return STATUS_CANCELLED
    if is_latest_group and conversation.live_status in ("running", "waiting"):
        return STATUS_IN_PROGRESS
    if is_latest_group and conversation.live_status == "failed":
        return STATUS_FAILED
    return STATUS_DONE


def _turn_outcome(group: list[ConversationItem], *, status: str) -> str | None:
    if status == STATUS_FAILED:
        return _first_error_message(group) or "Could not complete the request"
    if status == STATUS_CANCELLED:
        return "Cancelled before finishing"
    last_assistant = _last_assistant_text(group)
    return _truncate(last_assistant, _OUTCOME_MAX_CHARS) if last_assistant else None


def _turn_title(conversation: Conversation, group: list[ConversationItem]) -> str:
    # The user's request names the work; a system-started turn (a Result
    # wake) falls back to the chat's title.
    first_user = _first_user_text(group)
    if first_user and not first_user.startswith("[System"):
        return _truncate(first_user, _TITLE_MAX_CHARS)
    if conversation.title:
        return _truncate(conversation.title, _TITLE_MAX_CHARS)
    return "Activity"


def _turn_activity(
    conversation: Conversation,
    group: list[ConversationItem],
    *,
    is_latest_group: bool,
    include_step_detail: bool,
) -> Activity | None:
    """Build the Activity for one turn's items, or ``None`` if it had no tool calls."""
    calls = [item for item in group if item.type == "function_call"]
    if not calls:
        return None
    outputs_by_call_id = {
        item.data.call_id: item  # type: ignore[union-attr]
        for item in group
        if item.type == "function_call_output"
    }
    status = _turn_status(conversation, group, is_latest_group=is_latest_group)
    return Activity(
        id=f"{KIND_TURN}:{conversation.id}:{group[0].response_id}",
        kind=KIND_TURN,
        chat_id=conversation.id,
        title=_turn_title(conversation, group),
        outcome=_turn_outcome(group, status=status),
        status=status,
        started_at=group[0].created_at,
        finished_at=None if status == STATUS_IN_PROGRESS else group[-1].created_at,
        steps=_steps_for_calls(calls, outputs_by_call_id, include_detail=include_step_detail),
    )


def _split_sub_agent_title(conversation: Conversation) -> tuple[str, str]:
    """Best-effort ``(agent, display_title)`` split of a sub-agent's title.

    Named sub-agents (``sys_session_send`` / ``sys_session_close``) persist
    ``"<agent>:<title>"``; ``sys_session_create`` children may carry any
    title. Both are handled leniently (no raise) since an Activity read
    must never fail on an odd title.
    """
    title = title_without_closed_marker(conversation.title)
    if title and ":" in title:
        agent, _, rest = title.partition(":")
        return agent, rest
    return (conversation.sub_agent_name or "sub-agent"), (title or "Sub-agent")


def _sub_agent_status(conversation: Conversation) -> str:
    """Map a sub-agent conversation's live status (+ close marker) to an Activity status.

    ``live_status`` is authoritative when the runtime has reported one.
    Flagged ambiguity: the store has no distinct "cancelled" marker
    separate from a normal close, so a sub-agent tombstoned before ever
    reporting a turn (``live_status`` still unset) reads as Cancelled;
    one tombstoned after finishing at least one turn reads as Done.
    """
    if conversation.live_status == "failed":
        return STATUS_FAILED
    if conversation.live_status in ("running", "waiting"):
        return STATUS_IN_PROGRESS
    if conversation.live_status is None and is_session_closed(
        conversation.labels, conversation.title
    ):
        return STATUS_CANCELLED
    return STATUS_DONE


def _sub_agent_activity(
    conversation: Conversation,
    items: list[ConversationItem],
    *,
    include_step_detail: bool,
) -> Activity:
    agent, display_title = _split_sub_agent_title(conversation)
    status = _sub_agent_status(conversation)
    calls = [item for item in items if item.type == "function_call"]
    outputs_by_call_id = {
        item.data.call_id: item  # type: ignore[union-attr]
        for item in items
        if item.type == "function_call_output"
    }
    if status == STATUS_FAILED:
        outcome = _first_error_message(items) or "Could not complete the task"
    elif status == STATUS_CANCELLED:
        outcome = "Cancelled before finishing"
    else:
        last_assistant = _last_assistant_text(items)
        outcome = _truncate(last_assistant, _OUTCOME_MAX_CHARS) if last_assistant else None
    started_at = items[0].created_at if items else conversation.created_at
    finished_at = items[-1].created_at if items and status != STATUS_IN_PROGRESS else None
    return Activity(
        id=f"{KIND_SUB_AGENT}:{conversation.id}",
        kind=KIND_SUB_AGENT,
        chat_id=conversation.id,
        title=_truncate(f"{agent}: {display_title}" if display_title else agent, _TITLE_MAX_CHARS),
        outcome=outcome,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        steps=_steps_for_calls(calls, outputs_by_call_id, include_detail=include_step_detail),
    )


# ── Store-backed orchestration ─────────────────────────────────────────


def resolve_super_chat_id(conv_store: ConversationStore, session_id: str) -> str | None:
    """Resolve ``session_id`` to its Super Chat id.

    A Super Chat resolves to itself; a Side Chat resolves to the Super
    Chat it was forked from (one level only — Side Chats never fork from
    another Side Chat, per ``rollover/CONTEXT.md``).

    :param conv_store: Store to query.
    :param session_id: A Super Chat or Side Chat conversation id.
    :returns: The Super Chat's conversation id, or ``None`` if
        ``session_id`` does not exist.
    """
    conversation = conv_store.get_conversation(session_id)
    # Sub-agent ids never carry FORK_SOURCE_LABEL_KEY, so without this guard
    # one would otherwise resolve to itself as a (wrong) "Super Chat".
    if conversation is None or conversation.kind == "sub_agent":
        return None
    from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY

    source_id = conversation.labels.get(FORK_SOURCE_LABEL_KEY)
    return source_id or session_id


def list_chat_family(conv_store: ConversationStore, super_chat_id: str) -> list[Conversation]:
    """The Super Chat, its Side Chats, and every Sub-agent descendant of either.

    Returns ``[]`` when ``super_chat_id`` does not exist or is not a
    ``superside-chat`` session — the Activity Feed only ever covers that
    mode (``rollover/SUPERSIDE-CHAT-PLAN.md`` S5).

    :param conv_store: Store to query.
    :param super_chat_id: The Super Chat's conversation id.
    :returns: ``[super_chat, *side_chats, *sub_agents]``, each a full
        :class:`Conversation`.
    """
    from omnigent.context.rollover import list_related_chats

    root = conv_store.get_conversation(super_chat_id)
    if root is None or not is_superside_chat(root.labels):
        return []
    chats = [root]
    side_chat_ids = [
        chat["id"]
        for chat in list_related_chats(conv_store, super_chat_id, limit=_SIDE_CHATS_SCAN_LIMIT)
    ]
    if side_chat_ids:
        side_chats_by_id = conv_store.get_conversations(side_chat_ids)
        chats.extend(
            conv
            for conv_id in side_chat_ids
            if (conv := side_chats_by_id.get(conv_id)) is not None
            and is_superside_chat(conv.labels)
        )

    sub_agents: list[Conversation] = []
    frontier = [chat.id for chat in chats]
    seen = set(frontier)
    while frontier:
        child_map = conv_store.list_child_conversation_ids_by_parent(frontier)
        next_frontier = [
            child_id
            for parent_id in frontier
            for child_id in child_map.get(parent_id, [])
            if child_id not in seen
        ]
        seen.update(next_frontier)
        if next_frontier:
            fetched = conv_store.get_conversations(next_frontier)
            sub_agents.extend(
                fetched[child_id] for child_id in next_frontier if child_id in fetched
            )
        frontier = next_frontier
    return chats + sub_agents


def _activities_for_conversation(
    conv_store: ConversationStore,
    conversation: Conversation,
    *,
    include_step_detail: bool,
) -> list[Activity]:
    items = conv_store.list_items(conversation.id, limit=_ITEMS_SCAN_LIMIT, order="asc").data
    if conversation.kind == "sub_agent":
        return [_sub_agent_activity(conversation, items, include_step_detail=include_step_detail)]
    groups = _group_items_by_response(items)
    activities: list[Activity] = []
    for index, group in enumerate(groups):
        activity = _turn_activity(
            conversation,
            group,
            is_latest_group=(index == len(groups) - 1),
            include_step_detail=include_step_detail,
        )
        if activity is not None:
            activities.append(activity)
    return activities


def list_activities(
    conv_store: ConversationStore,
    session_id: str,
    *,
    before: int | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[Activity]:
    """The user's Activity Feed: every Activity under one Super Chat's family.

    :param conv_store: Store to query.
    :param session_id: The Super Chat (or one of its Side Chats) id.
    :param before: When set, only Activities that started strictly before
        this epoch timestamp.
    :param limit: Maximum Activities to return, newest-first (clamped to
        :data:`_MAX_LIMIT`).
    :returns: Activities newest-first; ``[]`` when ``session_id`` doesn't
        resolve to a ``superside-chat`` Super Chat.
    """
    super_chat_id = resolve_super_chat_id(conv_store, session_id)
    if super_chat_id is None:
        return []
    chats = list_chat_family(conv_store, super_chat_id)
    activities: list[Activity] = []
    for conversation in chats:
        activities.extend(
            _activities_for_conversation(conv_store, conversation, include_step_detail=False)
        )
    activities.sort(key=lambda activity: activity.started_at, reverse=True)
    if before is not None:
        activities = [activity for activity in activities if activity.started_at < before]
    bounded_limit = max(1, min(limit, _MAX_LIMIT))
    return activities[:bounded_limit]


def get_activity(
    conv_store: ConversationStore,
    session_id: str,
    activity_id: str,
) -> Activity | None:
    """One Activity, in full (steps include capped call/result detail).

    Scoped to ``session_id``'s Super Chat family — an id for an Activity
    outside that family returns ``None``, matching the owner-scoping the
    route layer already enforces on ``session_id`` itself.

    :param conv_store: Store to query.
    :param session_id: The Super Chat (or one of its Side Chats) id.
    :param activity_id: An id previously returned by :func:`list_activities`.
    :returns: The :class:`Activity` with full step detail, or ``None``.
    """
    super_chat_id = resolve_super_chat_id(conv_store, session_id)
    if super_chat_id is None:
        return None
    kind, _, rest = activity_id.partition(":")
    if kind == KIND_SUB_AGENT:
        chat_id = rest
    elif kind == KIND_TURN:
        chat_id, _, _response_id = rest.partition(":")
    else:
        return None
    family = {chat.id: chat for chat in list_chat_family(conv_store, super_chat_id)}
    conversation = family.get(chat_id)
    if conversation is None:
        return None
    for activity in _activities_for_conversation(
        conv_store, conversation, include_step_detail=True
    ):
        if activity.id == activity_id:
            return activity
    return None


def iso_date(epoch_seconds: int) -> str:
    """UTC calendar-day string for grouping Activities by day.

    :param epoch_seconds: Unix epoch timestamp.
    :returns: ``"YYYY-MM-DD"`` in UTC.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).date().isoformat()


def step_to_dict(step: Step) -> dict[str, Any]:
    """JSON-safe projection of one Step."""
    payload: dict[str, Any] = {
        "item_id": step.item_id,
        "title": step.title,
        "created_at": step.created_at,
    }
    if step.tool is not None:
        payload["tool"] = step.tool
    if step.detail is not None:
        payload["detail"] = step.detail
    return payload


def activity_to_dict(activity: Activity) -> dict[str, Any]:
    """JSON-safe projection of one Activity, for either route."""
    return {
        "id": activity.id,
        "kind": activity.kind,
        "chat_id": activity.chat_id,
        "title": activity.title,
        "outcome": activity.outcome,
        "status": activity.status,
        "started_at": activity.started_at,
        "finished_at": activity.finished_at,
        "date": iso_date(activity.started_at),
        "steps": [step_to_dict(step) for step in activity.steps],
    }
