"""The rollover super chat: token estimate, recent-window selection, and
the compaction item that recycles a resident native pane.

A rollover session is one long-running native CLI session (claude-native /
codex-native) that Omnigent, not the CLI, keeps bounded: once the estimated
context fill crosses a threshold, a ``compaction`` item is written with a
rolling summary plus the last few messages, and the native pane is recycled
so the next turn's resume rebuilder relaunches the CLI from exactly that
checkpoint (``rollover/DESIGN.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnigent.context.labels import (
    DEFAULT_KEEP_MESSAGES,
    ROLLOVER_AT_TOKENS_LABEL,
    ROLLOVER_KEEP_MESSAGES_LABEL,
)
from omnigent.entities import CompactionData
from omnigent.runtime.compaction import count_tokens, summarize_history
from omnigent.server.routes._sessions.common import (
    _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
)

# Fallback threshold when no window is known for the session's model —
# well under the smallest context window Omnigent routes to today.
DEFAULT_ROLLOVER_THRESHOLD_TOKENS = 90_000

# Fraction of the session's context window used as the default threshold,
# leaving headroom for a heavy tool turn before the CLI hits its own limit.
_DEFAULT_THRESHOLD_WINDOW_FRACTION = 0.45

# Marks the synthetic request/summary exchange prepended to a rollover's
# ``compacted_messages`` (and detected by build_summarization_prompt's
# progressive-summarization check) so the rolling summary stays cumulative.
_SUMMARY_REQUEST_TEXT = (
    "[This is an automatically generated summary of the prior conversation "
    "context. The original messages are available but not included in this "
    "prompt for brevity.]\n\nPlease provide a summary of our conversation so far."
)


def _parse_positive_int(raw: str | None) -> int | None:
    """Parse *raw* as a positive int, or ``None`` when unset/invalid."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def estimate_context_tokens(
    items_since_last_compaction: list[dict[str, Any]],
    *,
    model: str,
    labels: Mapping[str, str] | None = None,
) -> int:
    """
    Estimate the model's current context-window fill, in tokens.

    Prefers the last real usage a native forwarder reported
    (:data:`_LAST_CONTEXT_TOKENS_LABEL_KEY`, set from the
    ``external_session_usage`` event) since it reflects the CLI's own
    tokenizer and system-prompt overhead. Falls back to a tiktoken estimate
    over *items_since_last_compaction* when no real usage has been reported
    yet (e.g. right after a rollover, or for a harness that reports none).

    :param items_since_last_compaction: This session's items after the
        latest ``compaction`` item (or the whole record when there is none),
        as flat ``ConversationItem.to_api_dict()`` dicts.
    :param model: LLM model string, used to pick a tokenizer for the
        fallback estimate.
    :param labels: The session's labels, or ``None``.
    :returns: An approximate token count for the model's current context.
    """
    if labels is not None:
        reported = _parse_positive_int(labels.get(_LAST_CONTEXT_TOKENS_LABEL_KEY))
        if reported is not None:
            return reported
    return count_tokens(items_since_last_compaction, model)


def resolve_rollover_threshold(labels: Mapping[str, str] | None) -> int:
    """
    Resolve the token count at which a rollover session rolls over.

    Priority: the :data:`~omnigent.context.labels.ROLLOVER_AT_TOKENS_LABEL`
    label (an explicit positive int) > 45% of the session's last-reported
    context window > :data:`DEFAULT_ROLLOVER_THRESHOLD_TOKENS`.

    :param labels: The session's labels, or ``None``.
    :returns: The threshold, in tokens.
    """
    labels = labels or {}
    explicit = _parse_positive_int(labels.get(ROLLOVER_AT_TOKENS_LABEL))
    if explicit is not None:
        return explicit
    window = _parse_positive_int(labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY))
    if window is not None:
        return int(window * _DEFAULT_THRESHOLD_WINDOW_FRACTION)
    return DEFAULT_ROLLOVER_THRESHOLD_TOKENS


def resolve_keep_messages(labels: Mapping[str, str] | None) -> int:
    """Resolve ``omnigent.context.rollover_keep_messages``, default 20."""
    labels = labels or {}
    value = _parse_positive_int(labels.get(ROLLOVER_KEEP_MESSAGES_LABEL))
    return value if value is not None else DEFAULT_KEEP_MESSAGES


def select_recent(
    items: list[dict[str, Any]],
    keep_messages: int,
) -> list[dict[str, Any]]:
    """
    Select the last *keep_messages* messages, tool items riding along.

    Counts only ``type == "message"`` items (user/assistant turns); a
    message's ``function_call`` / ``function_call_output`` / ``reasoning`` /
    ``native_tool`` items are not counted but ride along with whichever
    message they precede (tool calls/results always appear before the
    assistant message they belong to), so a kept message brings its whole
    tool round-trip with it. The window always ends at the last item in
    *items* — there is no separate "new message" anchor here, unlike
    turn-time context assembly.

    :param items: Chronological (oldest first) flat item dicts.
    :param keep_messages: How many trailing messages to keep. Clamped to
        at least 1.
    :returns: The selected trailing slice of *items*, chronological.
    """
    if not items:
        return []
    message_indices = [i for i, item in enumerate(items) if item.get("type") == "message"]
    if not message_indices:
        # No messages at all is malformed input for a session record;
        # nothing safe to trim, so keep everything.
        return list(items)
    kept_message_indices = message_indices[-max(keep_messages, 1) :]
    start = min(kept_message_indices)
    boundary_response_id = items[start].get("response_id")
    while (
        start > 0
        and items[start - 1].get("type") != "message"
        and items[start - 1].get("response_id") == boundary_response_id
    ):
        start -= 1
    return items[start:]


def _summary_exchange(summary_text: str) -> list[dict[str, Any]]:
    """Build the synthetic user/assistant pair standing in for a summary."""
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": _SUMMARY_REQUEST_TEXT}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": summary_text}],
        },
    ]


async def build_rollover_item(
    items_since_previous_compaction: list[dict[str, Any]],
    *,
    previous_summary: str | None,
    keep_messages: int,
    model: str,
    llm_client: Any = None,
    connection: dict[str, str] | None = None,
    runner_client: Any | None = None,
    conversation_id: str | None = None,
) -> CompactionData:
    """
    Build the next rollover compaction item: a rolling summary + recent tail.

    The summary covers everything in *items_since_previous_compaction*,
    merged with *previous_summary* (progressive summarization — see
    ``summarize_history``/``build_summarization_prompt``) so it stays
    cumulative across rollovers rather than restarting each time.
    ``compacted_messages`` is shaped exactly like other producers'
    (``compaction_to_history_items``, the codex forwarder's
    ``replacement_history``): flat item dicts, summary pair first, so both
    the claude-native and codex-native resume rebuilders accept it directly.

    :param items_since_previous_compaction: This session's items after the
        latest compaction item (or the whole record when there is none),
        chronological, as flat item dicts. Must be non-empty.
    :param previous_summary: The prior rollover's summary text, or ``None``
        for the session's first rollover.
    :param keep_messages: How many trailing messages to keep verbatim.
    :param model: LLM model string for the summarization call and its
        token estimate.
    :param llm_client: LLM client for direct summarization. Ignored when
        *runner_client* is set.
    :param connection: Per-provider connection overrides, forwarded as-is.
    :param runner_client: When set, delegates the summarization LLM call to
        the runner's ``/v1/summarize`` so the runner's own credentials are
        used (the preferred path — see ``summarize_history``).
    :param conversation_id: Session id, forwarded to the runner delegation.
    :returns: A new :class:`CompactionData` ready to be posted as a
        ``compaction`` item.
    :raises ValueError: If *items_since_previous_compaction* is empty.
    """
    if not items_since_previous_compaction:
        raise ValueError("build_rollover_item requires at least one item to summarize")

    messages_to_summarize = list(items_since_previous_compaction)
    if previous_summary:
        messages_to_summarize = _summary_exchange(previous_summary) + messages_to_summarize

    summary = await summarize_history(
        messages_to_summarize,
        llm_client,
        model,
        connection,
        runner_client,
        conversation_id,
    )
    recent = select_recent(items_since_previous_compaction, keep_messages)
    last_item_id = items_since_previous_compaction[-1]["id"]
    compacted_messages = _summary_exchange(summary["text"]) + recent
    return CompactionData(
        summary=summary["text"],
        last_item_id=last_item_id,
        model=model,
        token_count=summary["token_count"],
        compacted_messages=compacted_messages,
    )
