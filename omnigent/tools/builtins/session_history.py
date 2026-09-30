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


def clamp_limit(raw: Any, *, default: int, maximum: int) -> int | str:
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
    limit = clamp_limit(args.get("limit"), default=_READ_DEFAULT_LIMIT, maximum=_READ_MAX_LIMIT)
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

    return read_response(turns, next_cursor)


class TurnCollector:
    """Group newest-first projected items into full turns, page by page.

    A turn is a user message plus everything that answered it. Shared by the
    in-process read and the runner's async REST read, which differ only in
    how they fetch a page.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.turns: list[list[dict[str, Any]]] = []
        self._current: list[dict[str, Any]] = []
        self._scanned = 0

    @property
    def wants_more(self) -> bool:
        return len(self.turns) < self._limit and self._scanned < _READ_MAX_ITEMS_SCANNED

    def feed(
        self, items: list[dict[str, Any]], has_more: bool
    ) -> tuple[list[list[dict[str, Any]]], str | None] | None:
        """Add one newest-first page; return ``(turns, next_cursor)`` once done."""
        for idx, item in enumerate(items):
            self._current.append(item)
            self._scanned += 1
            if item.get("type") == "text" and item.get("role") == "user":
                self.turns.append(list(reversed(self._current)))
                self._current = []
                if len(self.turns) >= self._limit:
                    # A cut on the page's last item only has more behind it if has_more.
                    if idx == len(items) - 1 and not has_more:
                        return self.turns, None
                    return self.turns, items[idx]["id"]
        if not has_more:
            return self.finish(None)
        return None

    def finish(self, cursor: str | None) -> tuple[list[list[dict[str, Any]]], str | None]:
        """Flush a partial turn (end of record or scan cap) rather than drop it."""
        if self._current:
            self.turns.append(list(reversed(self._current)))
            self._current = []
        return self.turns, cursor


def group_into_turns(
    fetch_page: Callable[[str | None], tuple[list[dict[str, Any]], bool]],
    *,
    limit: int,
    start_cursor: str | None = None,
) -> tuple[list[list[dict[str, Any]]], str | None]:
    """Collect *limit* full turns newest-first, paging backward via *fetch_page*."""
    collector = TurnCollector(limit)
    cursor = start_cursor
    while collector.wants_more:
        items, has_more = fetch_page(cursor)
        if not items:
            break
        done = collector.feed(items, has_more)
        if done is not None:
            return done
        cursor = items[-1]["id"]
    return collector.finish(cursor)


def read_response(turns: list[list[dict[str, Any]]], next_cursor: str | None) -> str:
    """The ``read`` action's JSON payload."""
    return json.dumps(
        {"turns": [{"messages": turn} for turn in turns], "next_cursor": next_cursor}
    )


def _search(conv_store: Any, conversation_id: str, args: dict[str, Any]) -> str:
    """Full-text search, scoped to ``conversation_id`` only."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"error": "search requires a non-empty 'query' string"})
    limit = clamp_limit(
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
    from omnigent.context.rollover import (
        reported_context_tokens,
        reported_context_window,
        resolve_rollover_threshold,
    )

    current_tokens = reported_context_tokens(labels)
    context_window = reported_context_window(labels)
    threshold = resolve_rollover_threshold(labels)

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


def _project_item(item: Any) -> dict[str, Any]:
    """Project a :class:`ConversationItem` via its flat API dict."""
    return project_api_item(item.to_api_dict())


def project_api_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Project a flat API item into a compact dict with its id.

    Messages, tool calls and tool results collapse to role/type/content, each
    content field capped at ``_CONTENT_MAX_CHARS``.

    :param item: A ``ConversationItem.to_api_dict()`` dict.
    :returns: A compact dict with ``id``, ``created_at``, ``role``, ``type``
        and content.
    """
    base = {"id": item.get("id"), "created_at": item.get("created_at")}
    kind = item.get("type")
    if kind == "function_call":
        return {
            **base,
            "role": "assistant",
            "type": "tool_call",
            "name": item.get("name"),
            "args": _truncate(str(item.get("arguments") or "")),
        }
    if kind == "function_call_output":
        output = item.get("output")
        return {
            **base,
            "role": "tool",
            "type": "tool_result",
            "name": item.get("name"),
            "content": _truncate(output if isinstance(output, str) else json.dumps(output)),
        }
    text_parts: list[str] = []
    for block in item.get("content") or []:
        if isinstance(block, dict):
            text = block.get("text") or block.get("output_text")
            if text:
                text_parts.append(text)
        elif isinstance(block, str):
            text_parts.append(block)
    return {
        **base,
        "role": item.get("role") or "unknown",
        "type": "text",
        "content": _truncate("\n".join(text_parts)),
    }


def _truncate(text: str, *, max_chars: int = _CONTENT_MAX_CHARS) -> str:
    """Return ``text`` capped at ``max_chars``, with a truncation marker when clipped."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " [truncated]"
