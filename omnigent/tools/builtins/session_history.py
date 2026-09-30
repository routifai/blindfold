"""Built-in tool: read-only recall over the calling session's own record.

The rollover super chat compacts older messages into a summary — "the
summary is a pointer, not the truth" (``rollover/DESIGN.md``). This tool
lets the model page backward through, or full-text search, the exact items
that summary was built from, and check how much context headroom is left.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

# Mirrors ``_ACTIVITY_MAX_CHARS`` in ``spawn.py`` — the same compact-preview
# budget ``sys_session_get_history`` uses for its per-field truncation.
_CONTENT_MAX_CHARS = 2000

_READ_DEFAULT_LIMIT = 5
_READ_MAX_LIMIT = 20

# Safety cap on total items scanned while grouping into turns, across
# batches, so a conversation with an unusually long tool-call run (or
# malformed data with no user-message boundaries) can't loop unbounded.
_READ_MAX_ITEMS_SCANNED = 2000
_READ_BATCH_SIZE = 100

_SEARCH_DEFAULT_LIMIT = 10
_SEARCH_MAX_LIMIT = 20

_ACTIONS = frozenset({"read", "search", "status"})

# Mirror the private label keys in ``server/routes/_sessions/common.py``
# (``_LAST_CONTEXT_TOKENS_LABEL_KEY`` / ``_LAST_CONTEXT_WINDOW_LABEL_KEY``):
# every harness's per-turn usage report lands here regardless of rollover
# mode, so it is the always-available fallback for ``status``.
_LAST_CONTEXT_TOKENS_LABEL_KEY = "omnigent.last_context_tokens"
_LAST_CONTEXT_WINDOW_LABEL_KEY = "omnigent.last_context_window"

# Rollover's own threshold label + documented default (``rollover/PLAN.md``
# "Shared contract"), duplicated here only as the fallback used when
# ``omnigent.context.rollover.resolve_rollover_threshold`` isn't importable
# yet (see ``_status``).
_ROLLOVER_AT_TOKENS_LABEL = "omnigent.context.rollover_at_tokens"
_DEFAULT_ROLLOVER_THRESHOLD_TOKENS = 90_000
_DEFAULT_THRESHOLD_WINDOW_FRACTION = 0.45


class SessionHistoryTool(Tool):
    """
    Page, search, and check token headroom for the calling session only.

    Scoped by construction: the session id always comes from
    :attr:`ToolContext.conversation_id` (set by the runtime from the turn
    that invoked the tool), never from the model-supplied arguments — so
    there is no argument shape that reaches another session's record.
    Read-only: no action writes or mutates anything.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"session_history"``."""
        return "session_history"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Recall the exact record of THIS session — the summary shown in "
            "context is a pointer, not the truth, and may omit details. "
            "action='read': read earlier turns of this conversation to "
            "ground yourself before answering. Returns full turns (a user "
            "message plus everything that answered it), newest first. "
            "limit caps turns per call (default 5, maximum 20). Pass "
            "next_cursor as cursor to read the next older page. Reading "
            "never changes the chat. action='search' full-text searches "
            "this session's own items. action='status' reports tokens "
            "used, the context window, and tokens left before the next "
            "rollover. Always scoped to the session that called this tool."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SessionHistoryTool.name(),
                "description": SessionHistoryTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": sorted(_ACTIONS),
                            "description": (
                                "'read' returns full turns of this session, newest "
                                "first; 'search' full-text searches its items; "
                                "'status' reports token usage and headroom before "
                                "the next rollover."
                            ),
                        },
                        "cursor": {
                            "type": "string",
                            "description": (
                                "read only. Opaque pagination cursor from a previous "
                                "call's next_cursor. Omit to read the newest turns; "
                                "pass next_cursor back to read the next OLDER page."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                f"read: turns per call, default {_READ_DEFAULT_LIMIT}, "
                                f"max {_READ_MAX_LIMIT}. search: max results, default "
                                f"{_SEARCH_DEFAULT_LIMIT}, max {_SEARCH_MAX_LIMIT}."
                            ),
                        },
                        "query": {
                            "type": "string",
                            "description": "search only. The full-text search query.",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Dispatch to the requested action, scoped to ``ctx.conversation_id``.

        :param arguments: JSON-encoded arguments from the LLM, e.g.
            ``{"action": "read", "cursor": "item_abc123"}``.
        :param ctx: Server-side execution context. ``ctx.conversation_id``
            is the ONLY source of the session to read — never taken from
            *arguments*.
        :returns: JSON string result, or ``{"error": ...}`` on failure.
        """
        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        if not ctx.conversation_id:
            return json.dumps({"error": "session_history has no calling session in context"})

        action = args.get("action")
        if action not in _ACTIONS:
            return json.dumps({"error": f"action must be one of {sorted(_ACTIONS)}"})

        from omnigent.runtime import get_conversation_store

        conv_store = get_conversation_store()
        if action == "read":
            return _read(conv_store, ctx.conversation_id, args)
        if action == "search":
            return _search(conv_store, ctx.conversation_id, args)
        return _status(conv_store, ctx.conversation_id)


def _clamp_limit(raw: Any, *, default: int, maximum: int) -> int | str:
    """Coerce + clamp a ``limit`` argument to ``[1, maximum]``, default when absent."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return json.dumps({"error": f"limit must be an integer, got {raw!r}"})
    if raw < 1:
        return json.dumps({"error": "limit must be >= 1"})
    return min(raw, maximum)


def _read(conv_store: Any, conversation_id: str, args: dict[str, Any]) -> str:
    """Read full turns backward from ``cursor`` (or the newest), newest turn first."""
    cursor = args.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        return json.dumps({"error": "cursor must be a string"})
    limit = _clamp_limit(args.get("limit"), default=_READ_DEFAULT_LIMIT, maximum=_READ_MAX_LIMIT)
    if isinstance(limit, str):
        return limit

    from omnigent.errors import StaleCursorError

    def fetch_page(cursor_item_id: str | None) -> tuple[list[dict[str, Any]], bool]:
        # ``after`` (not ``before``) is correct here: in desc order, "after"
        # means "further in sort direction" — i.e. older, continuing the
        # newest-first walk — while "before" would mean newer (see
        # ConversationStore.list_items).
        page = conv_store.list_items(
            conversation_id, limit=_READ_BATCH_SIZE, after=cursor_item_id, order="desc"
        )
        return [_project_item(item) for item in page.data], page.has_more

    try:
        turns, next_cursor = group_into_turns(fetch_page, limit=limit, start_cursor=cursor)
    except StaleCursorError:
        return json.dumps({"error": "stale_cursor", "cursor": cursor})

    return json.dumps(
        {
            "turns": [{"messages": turn} for turn in turns],
            "next_cursor": next_cursor,
        }
    )


def group_into_turns(
    fetch_page: Callable[[str | None], tuple[list[dict[str, Any]], bool]],
    *,
    limit: int,
    start_cursor: str | None = None,
) -> tuple[list[list[dict[str, Any]]], str | None]:
    """
    Group newest-first projected items into full turns.

    A turn is a user message plus everything that answered it (assistant
    messages, tool calls/results, reasoning) — everything up to but not
    including the next user message walking backward. Shared by the
    in-process tool and the runner's REST-based relay dispatch
    (``omnigent.runner.tool_dispatch``), which both provide their own
    *fetch_page* over the same already-projected item shape
    (``_project_item`` / its REST-shape counterpart).

    :param fetch_page: Called with the previous batch's oldest item id
        (``None`` for the first call, or *start_cursor*); returns
        ``(items, has_more)`` for one batch, newest-first.
    :param limit: Number of complete turns to collect.
    :param start_cursor: Resume point from a prior call's ``next_cursor``.
    :returns: ``(turns, next_cursor)`` — *turns* newest-first, each turn
        chronological (oldest item — the user message — first);
        *next_cursor* resumes with the next older page, or ``None`` when
        the record is exhausted.
    """
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    cursor = start_cursor
    scanned = 0
    while len(turns) < limit and scanned < _READ_MAX_ITEMS_SCANNED:
        items, has_more = fetch_page(cursor)
        if not items:
            break
        hit_limit_at: int | None = None
        for idx, item in enumerate(items):
            current.append(item)
            scanned += 1
            if item.get("type") == "text" and item.get("role") == "user":
                turns.append(list(reversed(current)))
                current = []
                if len(turns) >= limit:
                    hit_limit_at = idx
                    break
        if hit_limit_at is not None:
            # A boundary mid-page always has more items behind it (at least
            # the rest of this page); only a boundary on the page's LAST
            # item needs has_more to know whether the record is exhausted.
            if hit_limit_at == len(items) - 1 and not has_more:
                return turns, None
            return turns, items[hit_limit_at]["id"]
        cursor = items[-1]["id"]
        if not has_more:
            if current:
                turns.append(list(reversed(current)))
            return turns, None
    # Scan cap reached mid-turn: flush what was fetched rather than drop it.
    if current:
        turns.append(list(reversed(current)))
    return turns, cursor


def _search(conv_store: Any, conversation_id: str, args: dict[str, Any]) -> str:
    """Full-text search, scoped to ``conversation_id`` only."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"error": "search requires a non-empty 'query' string"})
    limit = _clamp_limit(
        args.get("limit"), default=_SEARCH_DEFAULT_LIMIT, maximum=_SEARCH_MAX_LIMIT
    )
    if isinstance(limit, str):
        return limit

    items = conv_store.search(query.strip(), conversation_id=conversation_id, limit=limit)
    return json.dumps({"results": [_project_item(item) for item in items]})


def _status(conv_store: Any, conversation_id: str) -> str:
    """Report tokens used, the context window, and headroom before rollover."""
    conversation = conv_store.get_conversation(conversation_id)
    labels = conversation.labels if conversation is not None else {}
    return json.dumps(status_from_labels(labels))


def status_from_labels(labels: dict[str, str]) -> dict[str, Any]:
    """
    Compute the ``status`` action's fields from a session's raw labels.

    Pure function of *labels* so both the in-process tool (labels read from
    :class:`~omnigent.entities.conversation.Conversation`) and the runner's
    REST-based relay dispatch (labels read from ``GET .../labels``) share
    one implementation — see ``omnigent.runner.tool_dispatch``.

    :param labels: The session's label mapping (possibly empty).
    :returns: Dict with ``rollover_trigger_tokens`` always present, and
        ``context_window_tokens`` / ``current_context_tokens`` / ``source``
        / ``tokens_remaining_to_rollover`` / ``context_used_percent`` only
        when they can actually be computed from known values.
    """
    current_tokens = _parse_positive_int(labels.get(_LAST_CONTEXT_TOKENS_LABEL_KEY))
    context_window = _parse_positive_int(labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY))

    # Prefer rollover's own threshold resolver once it exists (same policy,
    # single source of truth); fall back to the documented default here so
    # ``status`` still works before that module lands.
    try:
        from omnigent.context.rollover import resolve_rollover_threshold

        threshold = resolve_rollover_threshold(labels)
    except ImportError:
        explicit = _parse_positive_int(labels.get(_ROLLOVER_AT_TOKENS_LABEL))
        if explicit is not None:
            threshold = explicit
        elif context_window is not None:
            threshold = int(context_window * _DEFAULT_THRESHOLD_WINDOW_FRACTION)
        else:
            threshold = _DEFAULT_ROLLOVER_THRESHOLD_TOKENS

    # Only report what is actually known — a harness that hasn't posted a
    # usage report yet (e.g. right after a rollover) has no current_tokens,
    # so every field derived from it is omitted rather than guessed.
    result: dict[str, Any] = {"rollover_trigger_tokens": threshold}
    if context_window is not None:
        result["context_window_tokens"] = context_window
    if current_tokens is not None:
        result["current_context_tokens"] = current_tokens
        result["source"] = "reported"
        result["tokens_remaining_to_rollover"] = max(threshold - current_tokens, 0)
        if context_window is not None:
            result["context_used_percent"] = round(100 * current_tokens / context_window, 1)
    return result


def _parse_positive_int(raw: str | None) -> int | None:
    """Parse a label's string value as a positive int, or ``None``."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _project_item(item: Any) -> dict[str, str | None]:
    """
    Project a conversation item into a compact dict with its id.

    Same shape and truncation rule as ``spawn._project_activity_item``:
    messages, tool calls, and tool results collapse to role/type/content,
    each content field capped at ``_CONTENT_MAX_CHARS``.

    :param item: A :class:`ConversationItem` from the calling session.
    :returns: A compact dict with ``id``, ``role``, ``type``, and content.
    """
    data = item.data.model_dump()
    if item.type == "function_call":
        return {
            "id": item.id,
            "created_at": item.created_at,
            "role": "assistant",
            "type": "tool_call",
            "name": data.get("name"),
            "args": _truncate(data.get("arguments", "")),
        }
    if item.type == "function_call_output":
        return {
            "id": item.id,
            "created_at": item.created_at,
            "role": "tool",
            "type": "tool_result",
            "name": data.get("name"),
            "content": _truncate(data.get("output", "")),
        }
    role = data.get("role", "unknown")
    text_parts: list[str] = []
    for block in data.get("content", []):
        if isinstance(block, dict):
            text = block.get("text") or block.get("output_text")
            if text:
                text_parts.append(text)
        elif isinstance(block, str):
            text_parts.append(block)
    return {
        "id": item.id,
        "created_at": item.created_at,
        "role": role,
        "type": "text",
        "content": _truncate("\n".join(text_parts)),
    }


def _truncate(text: str, *, max_chars: int = _CONTENT_MAX_CHARS) -> str:
    """Return ``text`` capped at ``max_chars``, with a truncation marker when clipped."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " [truncated]"
